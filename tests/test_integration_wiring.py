"""Cross-feature wiring after the ten feature branches were merged: each test runs the REAL modules together (no fakes)
and covers one contract no single feature could close on its own."""
from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

import pytest

from operator_ai import (auth, config, db, delivery, escalations, events, guardrails, inbound, queue, rubrics, scheduler,
                         settings, skills, tenancy)
from operator_ai import prospects as P
from tests.conftest import demo_login, login

PW = "wiring-pass-123456"
ROOT = Path(__file__).resolve().parents[1]


def _user(t, username: str, role: str = "student") -> dict:
    uid = auth.create_user(t, username, PW, role)
    if role == "student":
        with t.db() as c:
            sid = c.execute("INSERT INTO students(name,email) VALUES(?,?)", (username.title(), f"{username}@example.invalid")).lastrowid
            c.execute("UPDATE users SET student_id=? WHERE id=?", (sid, uid))
    with t.db() as c:
        return auth.user_public(dict(c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()))


def _prospect(t, owner_id: int, name="Pat Prospect", email=None, handle=None, stage="contacted") -> int:
    with t.db() as c:
        return c.execute("INSERT INTO prospects(owner_user_id,name,email,handle,stage,created,updated) VALUES(?,?,?,?,?,?,?)",
                         (owner_id, name, email, handle, stage, time.time(), time.time())).lastrowid


def _events(t, name):
    return [e["payload"] for e in events.recent(t, name)]


# ================================================================ UI wiring that only shows in the source
def test_academy_page_renders_the_real_role_play_panel():
    academy = (ROOT / "web/src/pages/Academy.tsx").read_text()
    panel = (ROOT / "web/src/pages/academy/RolePlay.tsx").read_text()
    assert 'import { RolePlayPanel } from "./academy/RolePlay"' in academy and "<RolePlayPanel />" in academy
    assert "export function RolePlayPanel" in panel and "Role-play is coming" not in panel  # the stub was replaced
    # non-owners get an inbox without a new nav tab: inside Prospects and Queue
    assert "<InboxPanel />" in (ROOT / "web/src/pages/Prospects.tsx").read_text()
    assert "<InboxPanel />" in (ROOT / "web/src/pages/Queue.tsx").read_text()
    app = (ROOT / "web/src/App.tsx").read_text()
    assert 'href="#/join?forgot=1"' in app and "logo={me.brand?.logo}" in app and "logo={pub?.brand?.logo}" in app


# ================================================================ inbound <-> prospects (real functions, no guesses)
def test_inbound_moves_the_prospect_through_the_prospects_feature_and_emits_dnc_once(tenant):
    a = _user(tenant, "op-a")
    P.save_icp(tenant, a, {"sender": {"name": "Op A", "business": "Op Growth", "postal_address": "2 Test Lane, Testville"}})
    pid = _prospect(tenant, a["id"], email="pat@example.invalid", handle="@pat_x")
    res = inbound.route(tenant, "email", "Pat <pat@example.invalid>", "Yes, interested. How does it work?", {"subject": "Re: hi"})
    assert res["party_type"] == "prospect" and res["party_id"] == pid
    row = P._row(tenant, pid)
    assert row["stage"] == "replied"
    tl = P.timeline(tenant, pid)
    assert any(e["kind"] == "stage" and e["detail"]["to"] == "replied" for e in tl)  # prospects.set_stage, not a raw UPDATE
    assert any(e["kind"] == "reply" for e in tl)  # prospects recorded the reply from inbound's reply_received
    # an auto-reply is not a reply: no stage move, no reply recorded
    pid2 = _prospect(tenant, a["id"], name="Away Person", email="away@example.invalid")
    inbound.route(tenant, "email", "away@example.invalid", "Thanks for your email.", {"auto_reply": True})
    assert P._row(tenant, pid2)["stage"] == "contacted"
    assert not any(e["kind"] == "reply" for e in P.timeline(tenant, pid2))
    # STOP: do-not-contact through prospects.set_dnc, and dnc_set emitted exactly once
    before = len(_events(tenant, "dnc_set"))
    out = inbound.route(tenant, "email", "pat@example.invalid", "STOP", {})
    assert out["dnc"] is True
    mine = [e for e in _events(tenant, "dnc_set")[: len(_events(tenant, "dnc_set")) - before]
            if e.get("party_type") == "prospect" and e.get("party_id") == pid]
    assert len(mine) == 1
    row = P._row(tenant, pid)
    assert row["do_not_contact"] == 1 and row["stage"] == "dnc"
    assert any(e["kind"] == "dnc" for e in P.timeline(tenant, pid))


# ================================================================ prospects <-> delivery
def test_a_prospect_opener_gets_one_set_of_follow_ups_and_a_sender_footer(tenant, monkeypatch):
    monkeypatch.setenv("OPERATOR_PUBLIC_URL", "https://ops.example.invalid")
    stu = _user(tenant, "stu-f")
    P.save_icp(tenant, stu, {"niche": "fitness coaches", "sender": {
        "name": "Sam Sender", "business": "Sender Growth Co", "postal_address": "12 High Street, Example City"}})
    p = P.add_prospect(tenant, stu, {"name": "Mara Quill", "handle": "@mara_q", "email": "mara@example.invalid",
                                     "niche": "fitness coaching"}, "legitimate_interest", "From their public website")["prospect"]
    pid = p["id"]
    d = P.draft_opener(tenant, stu, pid, "instagram_dm", "question")
    assert d["status"] == "pending"
    it = queue.approve(tenant, d["queue_id"], "stu-f(student)", "student", user_id=stu["id"])
    assert it["status"] == "awaiting_manual_send"  # Instagram: a person sends it
    queue.mark_sent(tenant, d["queue_id"], "stu-f(student)", "student", user_id=stu["id"])
    with tenant.db() as c:
        n_fu = c.execute("SELECT COUNT(*) FROM queue WHERE kind='followup' AND json_extract(meta,'$.prospect_id')=?",
                         (pid,)).fetchone()[0]
        cad = c.execute("SELECT COUNT(*) FROM cadences WHERE party_type='prospect' AND party_id=?", (pid,)).fetchone()[0]
    assert n_fu == 3 and cad == 0  # prospects' day 2/4/7 follow-ups, and no second (delivery) sequence
    delivery.run_cadences(tenant, now=time.time() + 10 * 86400)
    with tenant.db() as c:
        assert c.execute("SELECT COUNT(*) FROM queue WHERE kind='followup' AND json_extract(meta,'$.prospect_id')=?",
                         (pid,)).fetchone()[0] == 3
    # the student's own prospect email names the student as the sender (never the programme)
    skills.put_profile(tenant, {"identity": {"name": "Programme HQ", "postal_address": "99 Owner Road, Ownertown"}}, "test")
    e = P.draft_opener(tenant, stu, P.add_prospect(tenant, stu, {"name": "Jo Email", "email": "jo@example.invalid"},
                                                    "legitimate_interest", "From their public website")["prospect"]["id"],
                       "email", "question")
    body = queue.get(tenant, e["queue_id"], scrub=False)["body"]
    assert "Sender Growth Co · 12 High Street, Example City" in body and "Owner Road" not in body
    assert "{unsubscribe_link}" in body and e["status"] == "pending", e
    # a person emailing their own prospects with no postal address of their own is held, with the right pointer
    stu2 = _user(tenant, "stu-g")
    q = queue.add(tenant, "prospect_email", "Hi there, quick question?", channel="email", recipient_ref="x@example.invalid",
                  meta={"prospect_facing": True, "lead_tier": "cold", "prospect_id": _prospect(tenant, stu2["id"]),
                        "owner_user_id": stu2["id"], "subject": "A question"})
    flag = next(f for f in q["flags"] if f["code"] == "missing_postal_address")
    assert q["status"] == "held" and "Sender details" in flag["message"]


# ================================================================ inbound email bounces & complaints -> do-not-contact
def _bounce(rcpt: str) -> str:
    return ("From: Mail Delivery Subsystem <mailer-daemon@mx.example.invalid>\nTo: ops@example.invalid\n"
            "Subject: Undelivered Mail Returned to Sender\nMessage-ID: <b1@mx.example.invalid>\n"
            "Content-Type: multipart/report; report-type=delivery-status; boundary=\"XX\"\n\n--XX\n"
            "Content-Type: text/plain\n\nYour message could not be delivered.\n--XX\nContent-Type: message/delivery-status\n\n"
            f"Reporting-MTA: dns; mx.example.invalid\n\nFinal-Recipient: rfc822; {rcpt}\nAction: failed\nStatus: 5.1.1\n--XX--\n")


def _complaint(rcpt: str) -> str:
    return ("From: abuse@isp.example.invalid\nTo: ops@example.invalid\nSubject: Complaint about message\n"
            "Message-ID: <c1@isp.example.invalid>\n"
            "Content-Type: multipart/report; report-type=feedback-report; boundary=\"YY\"\n\n--YY\nContent-Type: text/plain\n\n"
            "This is an abuse report.\n--YY\nContent-Type: message/feedback-report\n\nFeedback-Type: abuse\n"
            f"Original-Rcpt-To: {rcpt}\n--YY--\n")


def test_hard_bounces_and_spam_complaints_set_do_not_contact(client, tenant):
    from tests.test_inbound import _post_email

    a = _user(tenant, "op-b")
    pid = _prospect(tenant, a["id"], email="gone@example.invalid")
    waiting = queue.add(tenant, "followup", "One more idea for you?", channel="instagram_dm", recipient_ref="@gone",
                        meta={"prospect_facing": True, "prospect_id": pid, "owner_user_id": a["id"], "prior_contact": True})
    r = _post_email(client, tenant, _bounce("gone@example.invalid").encode())
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["parsed"]["bounce"]["kind"] == "bounce" and d["dnc"] is True and d["queued"] is None  # never a reply
    row = P._row(tenant, pid)
    assert row["do_not_contact"] == 1 and row["stage"] == "dnc"
    assert delivery.suppressed(tenant, ["gone@example.invalid"])
    assert queue.get(tenant, waiting["id"])["status"] in ("held", "cancelled")
    later = queue.add(tenant, "prospect_email", "Hello again", channel="email", recipient_ref="gone@example.invalid",
                      meta={"prospect_facing": True, "prior_contact": True})
    assert later["status"] == "held"
    esc = [e for e in escalations.list_for(tenant, {"role": "owner", "id": 0}, "open") if e["kind"] == "email_bounce"]
    assert esc and esc[0]["assignee_user_id"] == a["id"]
    assert not any(e.get("party_id") == pid for e in _events(tenant, "reply_received"))  # a bounce is not a reply
    # a temporary failure (4.x.x) changes nothing
    pid2 = _prospect(tenant, a["id"], name="Busy Box", email="full@example.invalid")
    soft = _bounce("full@example.invalid").replace("Action: failed\nStatus: 5.1.1", "Action: delayed\nStatus: 4.2.2")
    soft = soft.replace("<b1@", "<b2@")
    assert _post_email(client, tenant, soft.encode()).json()["ignored"]
    assert P._row(tenant, pid2)["do_not_contact"] == 0
    # a spam complaint
    pid3 = _prospect(tenant, a["id"], name="Cross Person", email="angry@example.invalid")
    d3 = _post_email(client, tenant, _complaint("angry@example.invalid").encode()).json()
    assert d3["parsed"]["bounce"]["kind"] == "complaint" and d3["dnc"] is True
    assert P._row(tenant, pid3)["do_not_contact"] == 1
    assert any(e["kind"] == "spam_complaint" and e["severity"] == "medium"
               for e in escalations.list_for(tenant, {"role": "owner", "id": 0}, "open"))


# ================================================================ missions run the real feature modules
def test_missions_use_the_real_prospects_proposals_client_ops_and_academy(tenant, monkeypatch):
    from operator_ai.missions import engine, evaluator

    monkeypatch.setenv("OPERATOR_PUBLIC_URL", "https://ops.example.invalid")
    o = _user(tenant, "mission-owner", role="owner")
    for i in range(32):  # the plan's prospect-list step waits for a full first list
        P.add_prospect(tenant, o, {"name": f"Casey Prospect{i}", "handle": f"@casey_{i}", "niche": "fitness coaches"},
                       "legitimate_interest", "From their public profile")
    # certification gates: academy.enrol puts the person on the path (no guessed function)
    m = engine.create(tenant, o, "Get my first client", None, {"niche": "fitness coaches", "offer": "DM setting",
                                                               "price": 2000})
    with tenant.db() as c:
        paths = {r["path_key"] for r in c.execute("SELECT path_key FROM academy_enrolments WHERE user_id=?", (o["id"],))}
    assert "growth_operator" in paths
    st = {s["skey"]: s for s in engine.get(tenant, m["id"])["steps"]}
    # prospects.research ran (not "skipped: no research feature"); prospects added through the feature already carry
    # their field facts, so there was nothing new to research
    assert st["research"]["status"] == "done" and "researched" in st["research"]["output"]
    # openers come from prospects.draft_openers_bulk (once the gates are passed)
    for path, lv in (("growth_operator", "L1"), ("setter", "L1"), ("compliance_core", "L1")):
        from operator_ai.academy import certify

        certify.award(tenant, o, path, lv, [], "test")
        events.emit(tenant, "certification_awarded", {"user_id": o["id"], "path_key": path, "level": lv})
    for key in ("offer_doc", "profile"):
        for q in {s["skey"]: s for s in engine.get(tenant, m["id"])["steps"]}[key]["queue_ids"]:
            queue.approve(tenant, q, "o", "owner", user_id=o["id"])
    st = {s["skey"]: s for s in engine.get(tenant, m["id"])["steps"]}
    assert st["openers"]["output"]["source"] == "prospects", st["openers"]
    item = queue.get(tenant, st["openers"]["queue_ids"][0])
    assert item["kind"] == "prospect_dm" and item["meta"]["feature"] == "prospects" and item["meta"]["mission_id"] == m["id"]
    # escalations raised by missions land in the real escalations table
    eid = evaluator.escalate(tenant, engine.get(tenant, m["id"]), "mission_off_track", "No calls booked by day 14")
    assert escalations.get(tenant, eid)["kind"] == "mission_off_track"
    # a speed-to-lead escalation assigned to the person shows in their mission next-actions
    escalations.raise_(tenant, "speed_to_lead", "high", "Pat replied an hour ago and is waiting", assignee_user_id=o["id"],
                       ref="inbound:test")
    acts = engine.next_actions(tenant, o)
    assert acts[0]["kind"] == "escalation" and "waiting" in acts[0]["title"]


def test_mission_proposal_step_builds_and_sends_through_proposals(tenant, monkeypatch):
    from operator_ai.missions import engine

    stu = _user(tenant, "prop-stu")
    p = P.add_prospect(tenant, stu, {"name": "Lee Client", "email": "lee@example.invalid", "niche": "coaches"},
                       "legitimate_interest", "From their public website")["prospect"]
    m = engine.create(tenant, stu, "Get my first client", None, {"niche": "coaches", "offer": "DM setting", "price": 1500})
    step = next(s for s in engine.get(tenant, m["id"])["steps"] if s["skey"] == "proposal")
    events.emit(tenant, "booking_made", {"party_type": "prospect", "party_id": p["id"], "starts_at": time.time() + 3600,
                                         "owner_user_id": stu["id"]})
    with tenant.db() as c:
        c.execute("UPDATE mission_steps SET status='pending' WHERE id=?", (step["id"],))
    from operator_ai.missions import registry

    m2 = engine.get(tenant, m["id"])
    registry.add_event(tenant, m["id"], "showed", {"party_id": p["id"], "party_type": "prospect"})
    res, _ = engine._execute(tenant, m2, next(s for s in m2["steps"] if s["skey"] == "proposal"))
    assert res.status == "waiting_approval" and res.output["source"] == "proposals", res
    with tenant.db() as c:
        props = c.execute("SELECT id, status FROM proposals WHERE prospect_id=?", (p["id"],)).fetchall()
        q = c.execute("SELECT id, kind FROM queue WHERE kind='proposal' AND json_extract(meta,'$.prospect_id')=?",
                      (p["id"],)).fetchall()
    assert props and q, (res, props, q)  # proposals.build + proposals.send: a real proposal, queued for approval


def test_client_won_from_a_proposal_starts_onboarding_once(tenant):
    from operator_ai import client_ops

    stu = _user(tenant, "won-stu")
    p = P.add_prospect(tenant, stu, {"name": "Win Co", "email": "win@example.invalid"}, "legitimate_interest",
                       "From their public website")["prospect"]
    out = P.mark_won(tenant, p["id"], "won-stu(student)", via="manual")
    with tenant.db() as c:
        cl = c.execute("SELECT * FROM clients WHERE prospect_id=?", (p["id"],)).fetchone()
    assert cl is not None and out
    assert len(client_ops.tasks(tenant, cl["id"])) == 7  # client_ops reacted to client_won: the access checklist
    kick = [i for i in queue.list_items(tenant, limit=500) if (i["meta"] or {}).get("purpose") == "client_kickoff"]
    assert len(kick) == 1 and kick[0]["meta"]["owner_user_id"] == stu["id"]
    client_ops.start_onboarding(tenant, cl["id"], owner_user_id=stu["id"])  # what the mission step calls: idempotent
    assert len([i for i in queue.list_items(tenant, limit=500) if (i["meta"] or {}).get("purpose") == "client_kickoff"]) == 1
    # only the student and the owner decide it: never a setter or closer
    for role in ("setter", "closer"):
        u = _user(tenant, f"{role}-k", role=role)
        with pytest.raises(queue.QueueError):
            queue.approve(tenant, kick[0]["id"], f"{role}-k({role})", role, user_id=u["id"])


# ================================================================ persona: framing on the surfaces it was consented for
def test_persona_framing_reaches_lessons_debriefs_and_nudges(client, demo_tenant, monkeypatch):
    from operator_ai import persona

    assert persona.active(demo_tenant)  # the demo consent covers lesson_framing, roleplay_debriefs, student_nudges
    demo_login(client, "student")
    lessons = client.get("/t/demo/api/academy/paths/growth_operator").json()["lesson_list"]
    assert lessons
    v = client.get(f"/t/demo/api/academy/lessons/{lessons[0]['id']}").json()
    assert v["framing"] and v["framing"]["badge"].startswith("Styled after") and (v["framing"]["opener"] or v["framing"]["closer"])
    # the learner's neutral-voice choice turns it off
    assert client.put("/t/demo/api/persona/me", json={"neutral_voice": True}).status_code == 200
    assert client.get(f"/t/demo/api/academy/lessons/{lessons[0]['id']}").json()["framing"] is None
    client.put("/t/demo/api/persona/me", json={"neutral_voice": False})
    # a completed (non-exam) role-play debrief
    rps = client.get("/t/demo/api/academy/roleplays", params={"status": "complete"}).json()
    rp = next(r for r in rps["items"] if not r.get("exam"))
    assert client.get(f"/t/demo/api/academy/roleplays/{rp['id']}").json()["framing"]
    # student nudges go through persona.style_template on the student_nudges scope
    seen = []
    real = persona.style_template
    monkeypatch.setattr(persona, "style_template", lambda t, text, surface, user=None: seen.append(surface) or real(t, text, surface, user))
    from operator_ai.agents import accountability

    accountability.run_interventions(demo_tenant)
    assert "student_nudges" in seen


def test_persona_guard_rules_hold_a_prospect_draft_that_names_the_persona(demo_tenant):
    flags = guardrails.check_for(demo_tenant, "Hi! Avery Lumen asked me to reach out about your coaching.",
                                 {"prospect_facing": True, "lead_tier": "cold", "feature": "prospects"})
    assert "persona_in_outreach" in {f["code"] for f in flags if f["severity"] == "block"}


# ================================================================ knowledge-base changes reach the Academy and the role-play rubric
def test_a_source_change_marks_lessons_stale_and_rechecks_the_rubric(env):
    from operator_ai import index
    from operator_ai.academy import build

    inputs = env / "inputs"
    shutil.copytree(config.FIXTURE_CORPUS, inputs, dirs_exist_ok=True)
    index.invalidate()
    tenancy.ensure_defaults()
    t = tenancy.get("owner")
    build.start(t, "test", sync=True)
    with t.db() as c:
        c.execute("UPDATE academy_lessons SET status='published' WHERE status='draft'")
        published = c.execute("SELECT COUNT(*) FROM academy_lessons WHERE status='published'").fetchone()[0]
        src = c.execute("SELECT source_file FROM academy_lessons WHERE status='published' AND source_file LIKE 'sops/%' "
                        "LIMIT 1").fetchone()["source_file"]
    assert published
    f = inputs / src
    f.write_text(f.read_text().replace("\n", "\nAn added line that changes this section.\n", 3))
    index.invalidate()
    skills.on_source_change(t, f"drive:{src}")  # what Drive sync (and an upload to the knowledge base) calls
    with t.db() as c:
        stale = c.execute("SELECT COUNT(*) FROM academy_lessons WHERE status='stale'").fetchone()[0]
    assert stale >= 1
    chk = t.get("roleplay_rubric_check")
    assert chk and chk["source"].startswith("drive:") and isinstance(chk["flagged"], list)
    from operator_ai import academy

    assert academy.tick(t)["checked"] is True and academy.tick(t)["skipped"]  # once a day
    assert "operator_ai.academy:tick" in scheduler.TENANT_TICKS


# ================================================================ role-play: exam attempts and the AI allowance
def test_abandoned_role_play_exams_count_and_ai_tokens_are_capped_per_learner(tenant):
    from operator_ai.roleplay import sessions

    stu = _user(tenant, "rp-stu")
    for _ in range(sessions.EXAM_ATTEMPTS_PER_DAY):
        v = sessions.start(tenant, stu, "objection_price", exam=True)
        sessions.abandon(tenant, stu, v["id"])
    with tenant.db() as c:
        sc = db.jload(c.execute("SELECT score FROM roleplays WHERE id=?", (v["id"],)).fetchone()["score"], {})
    assert sc["outcome"] == "abandoned" and sc["passed"] is False
    with pytest.raises(sessions.Conflict) as e:
        sessions.start(tenant, stu, "objection_price", exam=True)
    assert "abandoned" in str(e.value)
    assert sessions.start(tenant, stu, "objection_price")["status"] == "active"  # practice is never limited
    # AI phrasing: the learner's daily allowance caps each new session
    tenant.set("roleplay_ai_daily_tokens", 100)
    v = sessions.start(tenant, stu, "objection_price", mode="ai")
    with tenant.db() as c:
        st = db.jload(c.execute("SELECT state FROM roleplays WHERE id=?", (v["id"],)).fetchone()["state"], {})
        st["st"]["ai_tokens"] = 100
        c.execute("UPDATE roleplays SET state=? WHERE id=?", (db.jdump(st), v["id"]))
    v2 = sessions.start(tenant, stu, "objection_price", mode="ai")
    with tenant.db() as c:
        st2 = db.jload(c.execute("SELECT state FROM roleplays WHERE id=?", (v2["id"],)).fetchone()["state"], {})
    assert st2["st"]["ai_cap"] == 0 and sessions.ai_tokens_today(tenant, stu["id"]) == 100


# ================================================================ switches, rubrics, router
def test_web_research_is_a_server_only_switch_and_rubrics_see_proof_records(tenant):
    from operator_ai import claims

    assert "prospect_web_research" in config.BOX_ONLY
    with pytest.raises(settings.BoxOnly):
        settings.update_from_api(tenant, "prospect_web_research", True, "owner")
    line = "Let me make Acme $50k-$100k a month through our growth system. Would a quick call help?"
    meta = {"prospect_facing": True, "lead_tier": "hot", "prior_contact": True}
    codes = {f["code"] for f in rubrics.score(line, "dm", meta, [], tenant=tenant)["flags"]}
    assert "unsubstantiated_claim" in codes  # no proof record yet
    rec = claims.add(tenant, "outreach_range", "Typical results are far lower; this is the best quarter of clients.", 90, "owner")
    claims.add_evidence(tenant, rec["id"], "summary.csv", b"month,revenue\n1,1\n", "owner")
    codes = {f["code"] for f in rubrics.score(line, "dm", meta, [], tenant=tenant)["flags"]}
    assert "unsubstantiated_claim" not in codes  # the reviewer, critique and Builder now see the record too


def test_a_question_with_numbers_is_answered_with_diagnostic_context(client):
    from operator_ai.agents import router

    q = "I booked 3 calls and 2 showed, how do I improve my close rate?"
    assert router.classify(q) == "question"
    assert router.classify("I had 200 leads, 20 booked, 10 showed and 2 closed. How do I improve my close rate?") == "question"
    assert router.classify("200 leads, 20 booked, 10 showed, 2 closed") == "metrics"
    login(client)
    r = client.post("/t/owner/api/chat", json={"text": q}).json()
    assert r["intent"] == "question" and r["meta"]["covered"]
    assert "From your numbers: show rate 67%" in r["answer"] and r["meta"]["diagnostic_context"]["rates"]
    assert r["meta"]["offer_diagnostic"] is True


def test_the_coach_reads_what_the_person_told_it(client, tenant):
    from operator_ai import memory
    from operator_ai.agents import coach

    stu = _user(tenant, "mem-stu")
    memory.put(tenant, stu["id"], {"profile": {"offer": "DM setting for gyms", "niche": "gym owners", "goal": "first client"}},
               "mem-stu(student)")
    res = coach.answer(tenant, "How should I follow up after a no-show?", user=stu, record=False)
    assert res["remembered"] and {"label": "Your niche", "value": "gym owners"} in res["remembered"]["facts"]
    assert "remembered" not in coach.answer(tenant, "How should I follow up after a no-show?", record=False)


def test_logo_rules_agree_between_setup_and_settings(tenant):
    """The brand logo (shown in the shell and on sign-in) passes the same rule in the Setup editor and the settings
    shape check: an https:// link or a path on this site, never //host, data: or javascript:."""
    from operator_ai import setup

    assert setup.save_brand(tenant, {"logo": "/icon.svg"}, "owner")
    assert setup.save_brand(tenant, {"logo": "https://cdn.example.com/l.png"}, "owner")
    for bad in ("//evil.example/x.png", "data:image/png;base64,AAAA", "javascript:alert(1)"):
        with pytest.raises(setup.SetupError):
            setup.save_brand(tenant, {"logo": bad}, "owner")
        with pytest.raises(settings.InvalidSetting):
            settings.update_from_api(tenant, "brand", {**(tenant.get("brand") or {}), "logo": bad}, "owner")


# ================================================================ setup reads every feature's real state
def test_setup_checklist_reads_the_real_tables(client, tenant):
    from operator_ai import setup

    items = {i["key"]: i for i in setup.checklist(tenant)["items"]}
    assert items["academy"]["status"] == "todo" and "published lesson" in items["academy"]["detail"]
    assert items["persona"]["status"] == "optional"
    stu = _user(tenant, "fr-stu")
    with tenant.db() as c:
        c.execute("INSERT INTO academy_lessons(path_key,title,status,created) VALUES('setter','A lesson','draft',?)",
                  (time.time(),))
        lid = c.execute("SELECT id FROM academy_lessons").fetchone()[0]
        c.execute("INSERT INTO academy_progress(user_id,lesson_id,status,updated) VALUES(?,?,'complete',?)",
                  (stu["id"], lid, time.time()))
    assert {i["key"]: i for i in setup.checklist(tenant)["items"]}["academy"]["status"] == "todo"  # a draft is not published
    with tenant.db() as c:
        c.execute("UPDATE academy_lessons SET status='published'")
    assert {i["key"]: i for i in setup.checklist(tenant)["items"]}["academy"]["status"] == "done"
    fr = {i["key"]: i for i in setup.first_run(tenant, stu)["items"]}
    assert fr["lesson"]["done"] is True and fr["outreach"]["done"] is False
    q = queue.add(tenant, "prospect_dm", "Hi, quick question?", channel="instagram_dm", recipient_ref="@someone",
                  meta={"prospect_facing": True, "lead_tier": "cold", "owner_user_id": stu["id"]})
    queue.approve(tenant, q["id"], "fr-stu(student)", "student", user_id=stu["id"])
    assert {i["key"]: i for i in setup.first_run(tenant, stu)["items"]}["outreach"]["done"] is False  # approved != sent
    queue.mark_sent(tenant, q["id"], "fr-stu(student)", "student", user_id=stu["id"])
    assert {i["key"]: i for i in setup.first_run(tenant, stu)["items"]}["outreach"]["done"] is True


# ================================================================ every scheduler tick runs on a real and a demo tenant
def test_scheduler_tick_runs_every_tenant_tick_on_real_and_demo(demo_tenant, caplog):
    real = tenancy.get("owner")
    fns = [scheduler._resolve(s) for s in scheduler.TENANT_TICKS]
    assert all(f is not None for f in fns), [s for s, f in zip(scheduler.TENANT_TICKS, fns) if f is None]
    for fn in fns:  # each one directly, so an error fails the test (scheduler.tick only logs it)
        fn(real)
        if getattr(fn, "demo_too", False):
            fn(demo_tenant)
    caplog.set_level(logging.ERROR)
    scheduler.tick()
    assert not [r for r in caplog.records if "tick failed" in r.getMessage()]
