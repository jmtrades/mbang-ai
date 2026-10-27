"""End to end, through the API only, with AI off: a student goes from nothing to a first client on a REAL (non-demo)
workspace created in the test, with every feature running for real (team, academy, role-play, prospects, missions,
delivery, inbound, proposals, client ops). At each step: nothing leaves without a person's approval and confirmation,
the guardrails ran, another student sees none of it, and the audit log has it. A second test walks the pitch demo's
story so the demo keeps working on top of the same features."""
from __future__ import annotations

import re
import shutil
import time

import pytest
from fastapi.testclient import TestClient

from operator_ai import audit, channels, config, db, events, queue, tenancy, vault
from operator_ai.academy import quiz
from tests.conftest import demo_login
from tests.test_roleplay import GOOD

TID = "acme"
API = f"/t/{TID}/api"
STRONG = "a-Long-and-Unusual-pass-77"
LAWFUL = {"lawful_basis": "legitimate_interest", "lawful_note": "Business contact details from their own public profiles"}


def _ok(r, code: int = 200):
    assert r.status_code == code, (r.request.method, r.request.url.path, r.status_code, r.text[:400])
    return r.json()


def _csv(n: int) -> str:
    rows = ["Full Name,IG Handle,E-mail Address,Category,Follower Count,Bio"]
    for i in range(n):
        rows.append(f"Casey Coach{i},@casey_coach_{i},casey.coach{i}@example.com,fitness coaching,{1200 + i},"
                    f"Online fitness coach for busy parents. Runs a monthly group programme.")
    return "\n".join(rows) + "\n"


def _sent_without_a_person(t) -> list[dict]:
    """Queue items that went out without a person: a transport send in a real workspace (never allowed) or a send
    nobody approved."""
    with t.db() as c:
        rows = db.rows(c.execute("SELECT id, kind, status, delivered, decided_by FROM queue WHERE status='sent'"))
    return [r for r in rows if not str(r["delivered"] or "").startswith("manual:") or not r["decided_by"]]


def _wait_run(t, run_id: int, timeout: float = 60.0) -> None:
    from operator_ai import runtime

    end = time.time() + timeout
    while time.time() < end:
        r = runtime.get(t, run_id)
        if r and r["status"] in ("done", "error"):
            assert r["status"] == "done", r
            return
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def _steps(m: dict) -> dict:
    return {s["skey"]: s for s in m["steps"]}


def _email(from_: str, subject: str, body: str, mid: str) -> bytes:
    return (f"From: {from_}\nTo: sam@sam-growth.example\nSubject: {subject}\nMessage-ID: {mid}\n"
            f"Content-Type: text/plain\n\n{body}\n").encode()


@pytest.mark.timeout(600)
def test_a_student_goes_from_zero_to_a_first_client_on_a_real_workspace(env, app, monkeypatch):
    monkeypatch.setenv("OPERATOR_PUBLIC_URL", "https://acme.example.invalid")
    # the programme owner's own course material, loaded into inputs/ (a real workspace never trains on the fixture)
    shutil.copytree(config.FIXTURE_CORPUS, env / "inputs", dirs_exist_ok=True)
    from operator_ai import index

    index.invalidate()
    tenancy.ensure_defaults()
    t, pw_file = tenancy.create(TID, "Acme Growth Programme", kb_scope="course", kind="owner")
    assert not t.is_demo and not tenancy.is_demo_db(t)
    owner = TestClient(app)
    _ok(owner.post(f"{API}/auth/login", json={"username": "owner", "password": pw_file.read_text().strip()}))
    _ok(owner.put(f"{API}/profile", json={"profile": {"identity": {"name": "Acme Growth",
                                                                   "postal_address": "1 Market Street, Acmeville"}}}))

    # ------------------------------------------------------------------ 1. the owner invites the student (team)
    inv = _ok(owner.post(f"{API}/team/invites", json={"email": "sam.student@example.com", "role": "student",
                                                       "display": "Sam Student", "origin": "https://acme.example.invalid"}))
    token = re.search(r"token=([A-Za-z0-9_\-]+)", inv["link"]).group(1)
    mail = inv["queue_item"]
    assert mail["kind"] == "invite" and mail["status"] == "pending"  # the invite email itself waits for approval
    a = _ok(owner.post(f"{API}/queue/{mail['id']}/approve"))
    assert a["status"] == "awaiting_manual_send"  # no live email sender here: the owner sends it and confirms
    _ok(owner.post(f"{API}/queue/{mail['id']}/sent", json={}))
    assert token not in queue.get(t, mail["id"])["body"]  # the one-time link is scrubbed once it really went out

    # ------------------------------------------------------------------ 2. the student accepts and signs in
    stu = TestClient(app)
    assert _ok(stu.post(f"{API}/join/check", json={"token": token}))["role"] == "student"
    me = _ok(stu.post(f"{API}/join/accept", json={"token": token, "password": STRONG}))["user"]
    assert me["role"] == "student"
    assert _ok(stu.get(f"{API}/auth/session"))["signed_in"]
    assert stu.post(f"{API}/join/accept", json={"token": token, "password": STRONG}).status_code in (409, 410, 422)
    # a second student, to prove isolation at every step
    inv2 = _ok(owner.post(f"{API}/team/invites", json={"email": "ola.other@example.com", "role": "student",
                                                        "display": "Ola Other", "origin": "https://acme.example.invalid"}))
    other = TestClient(app)
    _ok(other.post(f"{API}/join/accept", json={"token": re.search(r"token=([A-Za-z0-9_\-]+)", inv2["link"]).group(1),
                                                "password": STRONG + "x"}))

    # ------------------------------------------------------------------ 3. the student sets an ICP and imports prospects
    _ok(stu.put(f"{API}/prospects/icp", json={"niche": "fitness coaches", "offer": "done-for-you DM setting",
                                              "sender": {"name": "Sam Student", "business": "Sam Growth",
                                                         "postal_address": "5 Side Street, Samtown",
                                                         "reply_email": "sam@sam-growth.example"}}))
    assert stu.post(f"{API}/prospects/import", json={"csv": _csv(3), "filename": "x.csv"}).status_code == 422  # basis needed
    imp = _ok(stu.post(f"{API}/prospects/import", json={"csv": _csv(34), "filename": "leads.csv", **LAWFUL}))
    assert imp["imported"] == 34
    prospects = _ok(stu.get(f"{API}/prospects"))["prospects"]
    assert len(prospects) == 34 and all(p["lawful_basis"] == "legitimate_interest" for p in prospects)
    assert _ok(other.get(f"{API}/prospects"))["prospects"] == []  # isolation
    assert other.get(f"{API}/prospects/{prospects[0]['id']}").status_code == 404

    # the owner requires the compliance certificate before anyone approves their own outreach (academy gate)
    _ok(owner.put(f"{API}/academy/admin/settings", json={"require_cert_for_own_outreach": True}))
    early = _ok(stu.post(f"{API}/prospects/{prospects[-1]['id']}/drafts", json={"move": "question"}))
    gate = stu.post(f"{API}/queue/{early['queue_id']}/approve")
    assert gate.status_code == 403 and "Compliance core" in gate.json()["detail"]
    _ok(stu.post(f"{API}/queue/{early['queue_id']}/reject", json={"reason_code": "timing"}))

    # ------------------------------------------------------------------ 4. certification evidence (academy + role-play)
    run = _ok(owner.post(f"{API}/academy/build"))
    _wait_run(t, run["run_id"])
    _ok(owner.post(f"{API}/academy/admin/publish-all", json={"include_exam": False}))
    for path in ("compliance_core", "setter", "growth_operator"):  # the owner's chosen L1 bar for this programme
        _ok(owner.put(f"{API}/academy/admin/paths/{path}", json={"levels": {"L1": {"mastery_overall": 0.7,
                                                                                    "mastery_skill": 0.6}}}))
    # the owner puts the student on the DM-setting path too (a student's own path is Growth operator); the note to
    # the student is itself a draft that waits for approval
    asg = _ok(owner.post(f"{API}/academy/assign", json={"user_id": me["id"], "kind": "path", "ref": "setter",
                                                        "note": "Before your first DMs."}))
    assert queue.get(t, asg["queue_id"])["status"] == "pending"
    for path in ("compliance_core", "setter", "growth_operator"):
        detail = _ok(stu.get(f"{API}/academy/paths/{path}"))
        for L in detail["lesson_list"]:
            _ok(stu.get(f"{API}/academy/lessons/{L['id']}"))
            _ok(stu.post(f"{API}/academy/lessons/{L['id']}/complete"))
    with t.db() as c:
        items = [quiz._row(dict(r)) for r in c.execute(
            "SELECT i.* FROM academy_quiz_items i JOIN academy_lessons l ON l.id=i.lesson_id WHERE i.status='published' "
            "AND l.status='published' AND i.pool='practice' AND i.kind!='write' ORDER BY i.id")]
    for it in items:
        ans = [str(x) for x in it["answer"]] if it["kind"] == "order" else str(it["answer"])
        _ok(stu.post(f"{API}/academy/items/{it['id']}/answer", json={"answer": ans, "ms": 5000}))
    # one role-play practice, following the SOP, scored turn by turn
    rp = _ok(stu.post(f"{API}/academy/roleplays", json={"scenario_key": "cold_dm_coach", "seed": 5}))
    for line in GOOD["cold_dm_coach"]:
        if rp["status"] != "active":
            break
        rp = _ok(stu.post(f"{API}/academy/roleplays/{rp['id']}/turn", json={"text": line}))
    if rp["status"] == "active":
        rp = _ok(stu.post(f"{API}/academy/roleplays/{rp['id']}/end"))
    assert rp["status"] == "complete" and rp["score"]["total"] >= 70 and rp["score"]["compliance_flags"] == 0
    assert other.get(f"{API}/academy/roleplays/{rp['id']}").status_code == 404  # isolation
    certs = _ok(stu.post(f"{API}/academy/certs/check"))
    from operator_ai.academy import is_certified

    for path in ("compliance_core", "setter", "growth_operator"):
        assert is_certified(t, me["id"], path, "L1"), (path, certs)
    assert _ok(stu.get(f"{API}/academy/certs/me"))["ladders"]
    assert any(e["user_id"] == me["id"] for e in [x["payload"] for x in events.recent(t, "certification_awarded")])

    # ------------------------------------------------------------------ 5. the first_client mission
    m = _ok(stu.post(f"{API}/missions", json={"goal": "Get my first client", "template": "first_client",
                                               "answers": {"niche": "fitness coaches", "offer": "done-for-you DM setting",
                                                           "price": 1500, "hours_per_day": 2,
                                                           "channels": "Instagram DMs"}}))
    mid = m["id"]
    st = _steps(m)
    assert st["offer_lesson"]["status"] == "done" and st["prospects"]["status"] == "done"
    assert other.get(f"{API}/missions/{mid}").status_code in (403, 404)
    for key in ("offer_doc", "profile"):  # the student approves their own planning documents
        for q in _steps(_ok(stu.get(f"{API}/missions/{mid}")))[key]["queue_ids"]:
            assert _ok(stu.post(f"{API}/queue/{q}/approve"))["status"] in ("approved", "awaiting_manual_send")
    st = _steps(_ok(stu.get(f"{API}/missions/{mid}")))
    assert st["dm_cert"]["status"] == "done" and st["openers"]["status"] == "waiting_approval", st["openers"]
    openers = [queue.get(t, q) for q in st["openers"]["queue_ids"]]
    assert openers and all(o["kind"] == "prospect_dm" and o["status"] == "pending" and o["meta"]["owner_user_id"] == me["id"]
                           and o["meta"]["feature"] == "prospects" for o in openers)
    assert all(isinstance(o["flags"], list) for o in openers)  # the guardrails ran on every draft
    assert not any(o["id"] in {i["id"] for i in _ok(other.get(f"{API}/queue"))["items"]} for o in openers)
    assert other.post(f"{API}/queue/{openers[0]['id']}/approve").status_code == 404
    # guardrails: an edit that promises results is held and can't be approved
    held = _ok(stu.put(f"{API}/queue/{openers[0]['id']}", json={"body": "Hi! I guarantee you 10 new clients in 30 days."}))
    assert held["status"] == "held" and stu.post(f"{API}/queue/{openers[0]['id']}/approve").status_code == 409
    _ok(stu.put(f"{API}/queue/{openers[0]['id']}", json={"body": openers[0]["body"]}))

    # ------------------------------------------------------------------ 6. approve own drafts -> send by hand -> "I sent it"
    for o in openers:
        r = _ok(stu.post(f"{API}/queue/{o['id']}/approve"))
        assert r["status"] == "awaiting_manual_send"  # Instagram: a person sends it from their own account
    assert _sent_without_a_person(t) == []
    with t.db() as c:
        assert c.execute("SELECT COUNT(*) FROM channel_messages WHERE direction='out' AND "
                         "json_extract(meta,'$.manual') IS NULL").fetchone()[0] == 0  # no transport sent anything
    for o in openers:
        assert _ok(stu.post(f"{API}/queue/{o['id']}/sent", json={}))["status"] == "sent"
    first = openers[0]["meta"]["prospect_id"]
    p1 = _ok(stu.get(f"{API}/prospects/{first}"))
    assert p1["prospect"]["stage"] == "contacted"
    fus = [d for d in p1["drafts"] if d["touch"] == "followup"]
    assert len(fus) == 3 and all(f["status"] == "pending" for f in fus)  # day 2/4/7, each waits for approval

    # ------------------------------------------------------------------ 7. the prospect replies by email (inbound webhook)
    secret = vault.ensure_practice_secret(t, "email_webhook_secret")
    raw = _email(f"Casey Coach0 <{p1['prospect']['email']}>", "Re: quick question",
                 "Yes, sounds good. When are you free for a call?", "<r1@example.com>")
    now = str(int(time.time()))
    rr = TestClient(app).post(f"/t/{TID}/hooks/email", content=raw, headers={
        "x-signature": channels.sign_body_ts(secret, now, raw), "x-signature-timestamp": now})
    reply = _ok(rr)
    assert reply["party_type"] == "prospect" and reply["party_id"] == first and reply["classification"] == "positive"
    nxt = queue.get(t, reply["queued"])
    assert nxt["kind"] == "prospect_email" and nxt["status"] == "pending" and nxt["meta"]["owner_user_id"] == me["id"]
    assert "Sam Growth · 5 Side Street, Samtown" in nxt["body"]  # the student is the sender, with their own address
    assert _ok(stu.get(f"{API}/prospects/{first}"))["prospect"]["stage"] == "replied"
    assert {f["status"] for f in fus} and all(queue.get(t, f["id"])["status"] in ("rejected", "cancelled") for f in fus)
    assert _ok(other.get(f"{API}/inbox"))["items"] == []
    assert [g["key"] for g in _ok(stu.get(f"{API}/inbox"))["items"]] == [f"prospect:{first}"]
    _ok(stu.post(f"{API}/queue/{nxt['id']}/approve"))
    _ok(stu.post(f"{API}/queue/{nxt['id']}/sent", json={}))

    # ------------------------------------------------------------------ 8. booking, the call, the proposal
    start = time.time() + 2 * 86400
    _ok(stu.post(f"{API}/prospects/{first}/book", json={"mode": "record", "start": start}))
    st = _steps(_ok(stu.get(f"{API}/missions/{mid}")))
    assert st["prep"]["status"] == "done" and st["call"]["status"] == "blocked_human"
    _ok(stu.post(f"{API}/missions/{mid}/steps/{st['call']['id']}/mark-done", json={"outcome": "showed"}))
    st = _steps(_ok(stu.get(f"{API}/missions/{mid}")))
    assert st["proposal"]["status"] == "waiting_approval" and st["proposal"]["output"]["source"] == "proposals"
    prop_item = queue.get(t, st["proposal"]["queue_ids"][0])
    assert prop_item["kind"] == "proposal" and prop_item["meta"]["owner_user_id"] == me["id"]
    with t.db() as c:
        prop = db.row(c.execute("SELECT id, token, status FROM proposals WHERE prospect_id=?", (first,)).fetchone())
    page = f"/t/{TID}/p/{prop['token']}"
    anon = TestClient(app)
    assert anon.get(page).status_code == 404  # not live before approval
    _ok(stu.post(f"{API}/queue/{prop_item['id']}/approve"))
    _ok(stu.post(f"{API}/queue/{prop_item['id']}/sent", json={}))
    assert anon.get(page).status_code == 200

    # ------------------------------------------------------------------ 9. accepted on the public page -> client won
    acc = _ok(anon.post(page + "/accept", json={"name": "Casey Coach0", "agree": True, "option": "retainer"}))
    assert acc["option"] == "retainer"
    assert _ok(stu.get(f"{API}/prospects/{first}"))["prospect"]["stage"] == "won"
    won = [e["payload"] for e in events.recent(t, "client_won")]
    assert won and won[0]["prospect_id"] == first
    cid = won[0]["client_id"]
    cl = _ok(stu.get(f"{API}/clients/{cid}"))
    assert cl["status"] == "onboarding" and len(cl["tasks"]) == 7  # client ops: onboarding and its access checklist
    assert other.get(f"{API}/clients/{cid}").status_code in (403, 404)
    kick = [i for i in queue.list_items(t, limit=500) if (i["meta"] or {}).get("purpose") == "client_kickoff"]
    assert len(kick) == 1 and kick[0]["status"] == "pending" and kick[0]["meta"]["owner_user_id"] == me["id"]

    # ------------------------------------------------------------------ 10. the mission reaches its client checkpoint
    from operator_ai.missions import engine

    st = _steps(_ok(stu.get(f"{API}/missions/{mid}")))
    assert st["win"]["status"] == "done" and st["onboarding"]["status"] == "done"
    m = engine.get(t, mid)
    assert m["context"]["won"] and m["context"]["client_id"] == cid

    # ------------------------------------------------------------------ nothing left without a person, and it's all audited
    assert _sent_without_a_person(t) == []
    with t.db() as c:
        assert c.execute("SELECT COUNT(*) FROM queue WHERE delivered LIKE 'mock:%'").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM queue WHERE status='sent' AND decided_by IS NULL").fetchone()[0] == 0
    kinds = {r["kind"] for r in audit.recent(t, 2000)}
    for k in ("login", "team.invite", "queue.add", "queue.approve", "queue.sent", "queue.reject", "queue.edit"):
        assert k in kinds, (k, sorted(kinds))
    assert audit.recent(t, 2000, "guardrail.block")  # the guarantee edit was blocked and logged
    assert _ok(other.get(f"{API}/queue"))["items"] == [] or all(
        (i["meta"] or {}).get("owner_user_id") != me["id"] for i in _ok(other.get(f"{API}/queue"))["items"])


@pytest.mark.timeout(300)
def test_the_pitch_demo_story_still_works(client, demo_tenant):
    # 1. Day One for the owner
    demo_login(client, "owner")
    day = _ok(client.get("/t/demo/api/day-one"))
    assert len(day["cards"]) >= 5 and all(isinstance(c["count"], int) for c in day["cards"])
    # 2. the Student Coach: a cited answer, and an honest "not covered"
    demo_login(client, "student")
    ans = _ok(client.post("/t/demo/api/coach/ask", json={"question": "What are the rules for a Thunder Dome test?"}))
    assert ans["covered"] in ("yes", "partly") and ans["citations"]
    nc = _ok(client.post("/t/demo/api/coach/ask", json={"question": "How do I set up TikTok Shop?"}))
    assert nc["covered"] == "no"
    # 3. the approval queue: a held draft can't be approved; edited clean, approved, then sent by hand (delivery truth)
    demo_login(client, "owner")
    items = _ok(client.get("/t/demo/api/queue?status=open"))["items"]
    held = next(i for i in items if i["status"] == "held" and i["channel"] == "instagram_dm")
    assert client.post(f"/t/demo/api/queue/{held['id']}/approve").status_code == 409
    fixed = _ok(client.put(f"/t/demo/api/queue/{held['id']}", json={"body": "Would a short call this week be useful?"}))
    assert fixed["status"] == "pending"
    assert _ok(client.post(f"/t/demo/api/queue/{held['id']}/approve"))["status"] == "awaiting_manual_send"
    assert _ok(client.post(f"/t/demo/api/queue/{held['id']}/sent", json={}))["status"] == "sent"
    # 4. the bottleneck diagnosis with money impact and cited benchmarks
    d = _ok(client.post("/t/demo/api/diagnose", json={"metrics": {"dms_sent": 800, "replies": 40, "booked": 6, "showed": 2,
                                                                   "closed": 1, "offer_price": 3000}}))
    assert d["bottleneck"] and d["citations"]
    # 5. the 90-second builder: departments, a rules run, drafts queued, nothing sent
    gen = _ok(client.post("/t/demo/api/builder/generate", json={"preset": "physio"}))
    build_id = gen.get("build_id") or gen.get("id")
    b = _ok(client.get(f"/t/demo/api/builder/builds/{build_id}"))
    depts = b.get("departments") or (b.get("build") or {}).get("departments") or []
    assert len(depts) == 5
    # 6. the student's first-client story is seeded part-way: prospects, a mission with next actions, the Academy
    demo_login(client, "student")
    assert all("(DEMO)" in p["name"] for p in _ok(client.get("/t/demo/api/prospects"))["prospects"])
    assert _ok(client.get("/t/demo/api/missions/next-actions"))["items"]
    assert _ok(client.get("/t/demo/api/academy"))["built"]["published"] >= 10
    assert [s for s in _ok(client.get("/t/demo/api/academy/scenarios"))["items"]]
    # the persona framing is live in the demo (signed demo consent) and labelled
    styled = _ok(client.post("/t/demo/api/coach/ask", json={"question": "How should I follow up after a no-show?"}))
    assert styled.get("persona", {}).get("styled") in (True, False)
    # 7. reset brings the clean story back
    demo_login(client, "owner")
    _ok(client.post("/t/demo/api/demo/reset"))
    demo_login(client, "owner")
    again = _ok(client.get("/t/demo/api/queue?status=open"))["items"]
    assert any(i["status"] == "held" for i in again)
