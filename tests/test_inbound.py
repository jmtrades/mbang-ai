"""Inbound replies, escalations and webhook safety (operator_ai/inbound.py, escalations.py, channels.py)."""
from __future__ import annotations

import json
import time

import pytest

from operator_ai import auth, channels, console, db, escalations, events, guardrails, inbound, queue, vault
from tests.conftest import demo_login, login

PW = "correct-horse-battery-9"


@pytest.fixture(autouse=True)
def _prospect_email_ready(request, monkeypatch):
    """Delivery's compliance rules: a prospect-facing email needs the business postal address (Business profile) and,
    in a real workspace, a public URL for its unsubscribe link before it can be approved. Without them the drafts are
    held (tested in tests/test_delivery.py); these tests exercise inbound routing on a workspace that is set up."""
    monkeypatch.setenv("OPERATOR_PUBLIC_URL", "https://ops.example.invalid")
    if "tenant" in request.fixturenames:
        from operator_ai import skills

        skills.put_profile(request.getfixturevalue("tenant"),
                           {"identity": {"name": "Peak Growth", "postal_address": "1 Example Street, Testville"}}, "test")


# ---------------------------------------------------------------- helpers
def _user(t, username, role="student", student_email=None, coach_id=None):
    uid = auth.create_user(t, username, PW, role, coach_id=coach_id)
    if role == "student":
        with t.db() as c:
            sid = c.execute("INSERT INTO students(name,email,coach_id) VALUES(?,?,?)",
                            (f"{username.title()} Learner", student_email, coach_id)).lastrowid
            c.execute("UPDATE users SET student_id=? WHERE id=?", (sid, uid))
    return uid


def _prospect(t, owner_id, name="Pat Prospect", email=None, handle=None, stage="contacted", demo=0):
    from operator_ai import prospects

    # the operator emails their own prospects, so their sender details carry their postal address (delivery's footer)
    if not prospects.sender_of(t, owner_id).get("postal_address"):
        prospects.save_icp(t, {"id": owner_id, "username": f"user{owner_id}", "role": "student"},
                           {"sender": {"name": "Op Sender", "business": "Op Growth", "postal_address": "2 Test Lane, Testville"}})
    with t.db() as c:
        return c.execute("INSERT INTO prospects(owner_user_id,name,email,handle,stage,demo,created,updated) VALUES(?,?,?,?,?,?,?,?)",
                         (owner_id, name, email, handle, stage, demo, time.time(), time.time())).lastrowid


def _lead(t, name="Lena Lead", email=None, handle=None, stage="new", tier="warm"):
    with t.db() as c:
        return c.execute("INSERT INTO leads(name,email,handle,stage,tier,created) VALUES(?,?,?,?,?,?)",
                         (name, email, handle, stage, tier, time.time())).lastrowid


def _email(from_, subject, body, mid=None, extra=""):
    h = f"From: {from_}\nSubject: {subject}\n" + (f"Message-ID: {mid}\n" if mid else "") + extra
    return (h + "Content-Type: text/plain\n\n" + body + "\n").encode()


def _post_email(client, t, raw, tid="owner", ts=True, sig=None):
    secret = vault.ensure_practice_secret(t, "email_webhook_secret")
    now = str(int(time.time()))
    if ts:
        h = {"x-signature": sig or channels.sign_body_ts(secret, now, raw), "x-signature-timestamp": now}
    else:
        h = {"x-signature": sig or channels.sign_body(secret, raw)}
    return client.post(f"/t/{tid}/hooks/email", content=raw, headers=h)


def _post_sms(client, t, payload, tid="owner", ts=True, stale=False, path="sms"):
    secret = vault.ensure_practice_secret(t, "sms_webhook_secret")
    body = json.dumps(payload).encode()
    now = str(int(time.time()) - (3600 if stale else 0))
    h = {"content-type": "application/json"}
    if ts:
        h.update({"x-signature": channels.sign_body_ts(secret, now, body), "x-signature-timestamp": now})
    else:
        h["x-signature"] = channels.sign_body(secret, body)
    return client.post(f"/t/{tid}/hooks/{path}", content=body, headers=h)


def _events(t, name):
    return [e["payload"] for e in events.recent(t, name)]


def _escalations(t, kind=None):
    with t.db() as c:
        q = "SELECT * FROM escalations" + (" WHERE kind=?" if kind else "") + " ORDER BY id"
        return db.rows(c.execute(q, (kind,) if kind else ()))


# ---------------------------------------------------------------- opt-out keywords and sorting
@pytest.mark.parametrize("text", ["STOP", "stop", "Stop.", "stop please", "Please stop", "Unsubscribe me", "UNSUBSCRIBE",
                                  "STOPALL", "END", "quit", "OPT OUT", "opt-out", "Revoke", "cancel", "STOP!!!",
                                  "stop texting me", "please unsubscribe me from this", "remove me from your list",
                                  "stop spamming me", "Don't text me again"])
def test_opt_out_keywords(text):
    assert inbound.opt_keyword(text) == "out"


@pytest.mark.parametrize("text", ["Cancel my call tomorrow", "Don't stop believing", "When does it end?", "Sure, let's talk",
                                  "end of the month works for me", "Stop by the clinic tomorrow?", "remove me from the group chat",
                                  "", "   "])
def test_not_opt_out(text):
    assert inbound.opt_keyword(text) is None


@pytest.mark.parametrize("text", ["START", "unstop", "/start", "Start please"])
def test_opt_in_keywords(text):
    assert inbound.opt_keyword(text) == "in"


@pytest.mark.parametrize("text,label", [
    ("Yes, sounds good. When are you free?", "positive"),
    ("Sure, let's chat", "positive"),
    ("What does the first month look like?", "question"),
    ("How much is it?", "price_ask"),
    ("What's the investment?", "price_ask"),
    ("Not right now, maybe after the holidays", "objection_not_now"),
    ("Honestly it's too expensive for us", "objection_cost"),
    ("I need to run it by my wife first", "objection_partner"),
    ("Not interested, thanks", "not_interested"),
    ("No", "not_interested"),
    ("Please unsubscribe me", "unsubscribe"),
    ("I'm out of the office until Monday", "out_of_office"),
    ("This is spam, I'm reporting you", "legal_complaint"),
    ("I want a refund", "legal_complaint"),
    ("I'm not sure what you mean", "question"),
])
def test_classify_reply_rules(text, label):
    out = inbound.classify_reply(text)
    assert out["label"] == label and out["mode"] == "rules" and out["label_text"] == inbound.LABELS[label]


def test_classify_unclear_falls_back_without_ai(tenant):
    from operator_ai import llm

    before = llm.REQUESTS_SENT
    out = inbound.classify_reply("hmm ok then", tenant)
    assert out["label"] == "question" and out["unclear"] and out["mode"] == "rules"
    assert llm.REQUESTS_SENT == before  # AI budget 0: nothing sent anywhere


def test_ai_tiebreak_may_only_pick_a_known_label(tenant, monkeypatch):
    from operator_ai import llm

    monkeypatch.setattr(llm, "complete_or_replay", lambda *a, **k: {"text": "positive", "mode": "live"})
    assert inbound.classify_reply("hmm ok then", tenant)["label"] == "positive"
    assert inbound.classify_reply("hmm ok then", tenant)["mode"] == "ai"
    monkeypatch.setattr(llm, "complete_or_replay", lambda *a, **k: {"text": "send_them_a_discount", "mode": "live"})
    out = inbound.classify_reply("hmm ok then", tenant)
    assert out["label"] == "question" and out["mode"] == "rules"  # off-list answers are ignored

    def boom(*a, **k):
        raise llm.AIUnavailable("AI off (budget 0)")

    monkeypatch.setattr(llm, "complete_or_replay", boom)
    assert inbound.classify_reply("hmm ok then", tenant)["mode"] == "rules"


def test_canonical_addresses():
    assert inbound.canonical("email", "Pat <Pat.Prospect+news@GoogleMail.com>") == "patprospect@gmail.com"
    assert inbound.canonical("sms", "+1-555-0142") == "+15550142"
    assert inbound.canonical("sms", "(415) 555-0100") == "+14155550100"
    assert inbound.canonical("instagram_dm", "https://instagram.com/Some.Handle/") == "some.handle"
    assert inbound.canonical("instagram_dm", "@Some.Handle") == "some.handle"
    assert inbound.parse_address("Jane Doe <jane@example.invalid>") == ("Jane Doe", "jane@example.invalid")


# ---------------------------------------------------------------- prospects: the full loop
def test_prospect_email_reply_drafts_own_approval_and_moves_stage(client, tenant):
    a = _user(tenant, "op-a")
    b = _user(tenant, "op-b")
    pid = _prospect(tenant, a, email="pat.prospect@gmail.com")
    raw = _email("Pat Prospect <Pat.Prospect+news@gmail.com>", "Re: your clinic", "Yes, sounds good. When are you free for a call?",
                 mid="<m1@example.invalid>")
    r = _post_email(client, tenant, raw)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["party_type"] == "prospect" and d["party_id"] == pid and d["classification"] == "positive"
    assert d["parsed"]["from"] == "Pat.Prospect+news@gmail.com"
    item = queue.get(tenant, d["queued"])
    assert item["kind"] == "prospect_email" and item["status"] == "pending" and not guardrails.blocked(item["flags"])
    assert item["meta"]["owner_user_id"] == a and item["meta"]["prospect_facing"] is True and item["meta"]["lead_tier"]
    assert item["recipient_ref"] == "Pat.Prospect+news@gmail.com"  # the parsed address, never "Name <addr>"
    assert item["meta"]["subject"] == "Re: your clinic" and "STOP" in item["body"]
    assert item["meta"]["source"].startswith("Template") and item["meta"]["playbook"]["id"]
    with tenant.db() as c:
        assert c.execute("SELECT stage FROM prospects WHERE id=?", (pid,)).fetchone()[0] == "replied"
        run = c.execute("SELECT agent, status, steps FROM runs WHERE id=?", (d["run_id"],)).fetchone()
    assert run["agent"] == "inbound" and run["status"] == "done" and "Match the sender" in run["steps"]
    rr = _events(tenant, "reply_received")
    assert rr and rr[0]["party_type"] == "prospect" and rr[0]["owner_user_id"] == a and rr[0]["queue_id"] == item["id"]
    assert any(e["prospect_id"] == pid and e["stage"] == "replied" for e in _events(tenant, "prospect_stage"))
    # the other student can neither see nor approve it
    login(client, "owner", "op-b", PW)
    assert client.get("/t/owner/api/inbox").json()["items"] == []
    assert client.get(f"/t/owner/api/inbox/thread?key=prospect:{pid}").status_code == 404
    # someone else's own outreach is invisible to them (delivery's rule: 404, never a hint that it exists)
    assert client.post(f"/t/owner/api/queue/{item['id']}/approve").status_code in (403, 404)
    assert client.post(f"/t/owner/api/inbox/{d['inbound_id']}/dismiss").status_code == 404
    # the operator sees their conversation and approves their own reply
    login(client, "owner", "op-a", PW)
    inbox = client.get("/t/owner/api/inbox").json()
    assert [g["key"] for g in inbox["items"]] == [f"prospect:{pid}"] and inbox["items"][0]["draft"]["id"] == item["id"]
    assert "rules" in inbox["ai"]["label"]
    th = client.get(f"/t/owner/api/inbox/thread?key=prospect:{pid}").json()
    assert [m["direction"] for m in th["messages"]] == ["in", "out"]
    assert client.post(f"/t/owner/api/queue/{item['id']}/approve").status_code == 200
    assert b != a


def test_price_question_draft_never_mentions_price(tenant):
    a = _user(tenant, "op-a")
    _prospect(tenant, a, handle="@clinic_owner")
    res = inbound.route(tenant, "instagram_dm", "@Clinic_Owner", "How much is it?", {})
    item = queue.get(tenant, res["queued"])
    assert res["classification"] == "price_ask" and item["kind"] == "prospect_dm" and item["meta"]["prospect_facing"]
    flags = guardrails.check(item["body"], "outreach", item["meta"])
    assert not guardrails.blocked(flags) and "$" not in item["body"] and "price" not in item["body"].lower()


def test_unsubscribe_sets_dnc_holds_waiting_drafts_and_blocks_future(tenant):
    a = _user(tenant, "op-a")
    pid = _prospect(tenant, a, email="dana@example.invalid", handle="@dana_x")
    waiting = queue.add(tenant, "prospect_dm", "Quick follow-up idea for your clinic", channel="instagram_dm",
                        recipient_ref="@dana_x", meta={"prospect_facing": True, "prospect_id": pid, "owner_user_id": a,
                                                       "prior_contact": True})
    res = inbound.route(tenant, "email", "Dana <dana@example.invalid>", "Please unsubscribe me.", {"subject": "Re: hi"})
    assert res["dnc"] is True and res["queued"] is None and res["classification"] == "unsubscribe"
    with tenant.db() as c:
        p = c.execute("SELECT stage, do_not_contact FROM prospects WHERE id=?", (pid,)).fetchone()
    assert p["stage"] == "dnc" and p["do_not_contact"] == 1
    held = queue.get(tenant, waiting["id"])
    # inbound holds it, then delivery's dnc_set subscriber cancels every prospect-facing item to that party for good
    assert held["status"] == "cancelled" and "do-not-contact" in (held["status_reason"] or "")
    with pytest.raises(queue.QueueError):
        queue.approve(tenant, waiting["id"], "owner(owner)")
    assert "dana@example.invalid" in tenant.get("blocked_attendees")
    later = queue.add(tenant, "prospect_email", "One more thing", channel="email", recipient_ref="dana@example.invalid",
                      meta={"prospect_facing": True, "prior_contact": True})
    assert later["status"] == "held" and any(f["code"] == "never_invite" for f in later["flags"])
    assert _events(tenant, "dnc_set")[0]["party_type"] == "prospect"
    assert _escalations(tenant, "dnc_request")[0]["assignee_user_id"] == a
    # a later "yes" from the same address never drafts outreach again
    again = inbound.route(tenant, "email", "dana@example.invalid", "Actually yes, tell me more", {})
    assert again["queued"] is None


def test_legal_complaint_escalates_and_never_answers_from_the_course(tenant, monkeypatch):
    from operator_ai.agents import coach

    monkeypatch.setattr(coach, "answer", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no course answer")))
    res = inbound.route(tenant, "email", "angry@example.invalid", "I'm talking to my lawyer about these emails.", {})
    assert res["classification"] == "legal_complaint" and res["escalation_id"]
    esc = escalations.get(tenant, res["escalation_id"])
    assert esc["kind"] == "legal" and esc["severity"] == "high"
    item = queue.get(tenant, res["queued"])
    # an email reply carries delivery's compliance footer (postal address + unsubscribe) after the holding text
    assert item["status"] == "needs_human" and item["body"].startswith(inbound.HOLDING_LEGAL)
    refund = inbound.route(tenant, "email", "buyer@example.invalid", "I want a refund please", {})
    assert escalations.get(tenant, refund["escalation_id"])["kind"] == "refund"


def test_unknown_sender_gets_a_holding_reply_and_a_triage_entry(client, tenant, monkeypatch):
    from operator_ai.agents import coach

    monkeypatch.setattr(coach, "answer", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no course answer")))
    raw = _email("Stranger <stranger@example.invalid>", "Question", "What are the rules for a Thunder Dome test?")
    d = _post_email(client, tenant, raw).json()
    assert d["party_type"] == "unknown"
    item = queue.get(tenant, d["queued"])
    assert item["kind"] == "channel_reply" and item["status"] == "needs_human" and item["body"].startswith(inbound.HOLDING)
    assert "Unsubscribe" in item["body"]  # delivery's footer: the holding reply is a prospect-facing email
    assert "Sources" not in item["body"] and item["meta"]["holding"] and not item["meta"].get("citations")
    login(client)
    tri = client.get("/t/owner/api/inbox?view=triage").json()
    assert len(tri["items"]) == 1 and tri["items"][0]["party_type"] == "unknown" and tri["counts"]["triage"] == 1


def test_student_email_keeps_the_cited_coach(client, tenant):
    _user(tenant, "learner", student_email="learner@example.invalid")
    raw = _email("Learner <learner@example.invalid>", "Thunder Dome", "What are the rules for a Thunder Dome test?")
    d = _post_email(client, tenant, raw).json()
    item = queue.get(tenant, d["queued"])
    assert d["party_type"] == "student" and d["covered"] == "yes"
    assert item["kind"] == "email_reply" and item["status"] == "pending" and item["recipient_ref"] == "learner@example.invalid"


def test_out_of_office_is_recorded_without_a_draft(client, tenant):
    a = _user(tenant, "op-a")
    pid = _prospect(tenant, a, email="away@example.invalid")
    raw = _email("away@example.invalid", "Automatic reply", "Thanks for your email.", extra="Auto-Submitted: auto-replied\n")
    d = _post_email(client, tenant, raw).json()
    assert d["classification"] == "out_of_office" and d["queued"] is None
    with tenant.db() as c:
        assert c.execute("SELECT stage FROM prospects WHERE id=?", (pid,)).fetchone()[0] == "contacted"


def test_email_with_no_text_body_does_not_crash(client, tenant):
    raw = (b"From: Pic <pic@example.invalid>\nSubject: photo\nMIME-Version: 1.0\nContent-Type: multipart/mixed; boundary=XX\n\n"
           b"--XX\nContent-Type: image/png\nContent-Transfer-Encoding: base64\n\niVBORw0KGgo=\n--XX--\n")
    r = _post_email(client, tenant, raw)
    assert r.status_code == 200 and r.json()["parsed"]["body"] == ""
    assert _post_email(client, tenant, _email("", "no sender", "hi")).status_code == 422


# ---------------------------------------------------------------- leads + pasted replies
def test_lead_reply_pasted_in_by_a_setter(client, tenant):
    lid = _lead(tenant, handle="@lena_lead", email="lena@example.invalid")
    _user(tenant, "setter1", "setter")
    login(client, "owner", "setter1", PW)
    r = client.post("/t/owner/api/inbox/capture", json={"channel": "instagram_dm", "handle": "@Lena_Lead", "body": "Sure, let's talk"})
    assert r.status_code == 200, r.text
    d = r.json()
    item = queue.get(tenant, d["queued"])
    assert d["party_type"] == "lead" and item["kind"] == "setter_reply" and item["meta"]["lead_id"] == lid
    with tenant.db() as c:
        assert c.execute("SELECT stage FROM leads WHERE id=?", (lid,)).fetchone()[0] == "replied"
    assert client.post(f"/t/owner/api/queue/{item['id']}/approve").status_code == 200  # setters decide setter replies


def test_capture_rbac_and_validation(client, tenant):
    a = _user(tenant, "op-a")
    b = _user(tenant, "op-b")
    pa = _prospect(tenant, a, handle="@shared_handle")
    pb = _prospect(tenant, b, handle="@shared_handle")
    _lead(tenant, handle="@programme_lead")
    _user(tenant, "coach1", "coach")
    login(client, "owner", "coach1", PW)
    assert client.post("/t/owner/api/inbox/capture", json={"handle": "@x", "body": "hi"}).status_code == 403
    login(client, "owner", "op-a", PW)
    assert client.post("/t/owner/api/inbox/capture", json={"handle": "@x", "body": "hi", "prospect_id": pb}).status_code == 404
    assert client.post("/t/owner/api/inbox/capture", json={"handle": "@x", "body": "   "}).status_code == 422
    assert client.post("/t/owner/api/inbox/capture", json={"handle": "@x", "body": "hi", "channel": "carrier_pigeon"}).status_code == 422
    assert client.post("/t/owner/api/inbox/capture", json={"handle": "", "body": "hi"}).status_code == 422
    assert client.post("/t/owner/api/inbox/capture", content=b"{not json", headers={"content-type": "application/json"}).status_code == 422
    # a student never attaches a reply to the programme's own leads
    d = client.post("/t/owner/api/inbox/capture", json={"handle": "@programme_lead", "body": "Sure"}).json()
    assert d["party_type"] == "unknown"
    # two operators with the same handle: each sees only their own thread
    da = client.post("/t/owner/api/inbox/capture", json={"handle": "@shared_handle", "body": "Interested, tell me more"}).json()
    assert da["party_type"] == "prospect" and da["party_id"] == pa
    login(client, "owner", "op-b", PW)
    db_ = client.post("/t/owner/api/inbox/capture", json={"handle": "@shared_handle", "body": "Not right now"}).json()
    assert db_["party_id"] == pb and db_["conversation_id"] != da["conversation_id"]
    keys = [g["key"] for g in client.get("/t/owner/api/inbox").json()["items"]]
    assert keys == [f"prospect:{pb}"]
    assert client.get(f"/t/owner/api/inbox/thread?key=prospect:{pa}").status_code == 404


# ---------------------------------------------------------------- webhooks: idempotency, signatures, opt-out
def test_webhook_retries_are_dropped(client, demo_tenant):
    tg = vault.ensure_practice_secret(demo_tenant, "telegram_webhook_secret")
    H = {"x-telegram-bot-api-secret-token": tg}
    upd = {"update_id": 555001, "message": {"chat": {"id": 9001, "type": "private"}, "text": "hello there?"}}
    first = client.post("/t/demo/hooks/telegram", headers=H, json=upd).json()
    second = client.post("/t/demo/hooks/telegram", headers=H, json=upd).json()
    assert first["inbound_id"] and second["duplicate"] is True
    ss = vault.ensure_practice_secret(demo_tenant, "slack_signing_secret")
    body = json.dumps({"type": "event_callback", "event_id": "Ev123", "event": {"type": "app_mention", "text": "<@U1> hi?",
                                                                                 "channel": "C1", "user": "U9"}}).encode()
    ts = str(int(time.time()))
    sh = {"x-slack-request-timestamp": ts, "x-slack-signature": channels.slack_sign(ss, ts, body), "content-type": "application/json"}
    assert client.post("/t/demo/hooks/slack", content=body, headers=sh).json()["queued"]
    assert client.post("/t/demo/hooks/slack", content=body, headers={**sh, "x-slack-retry-num": "1"}).json()["duplicate"]
    sms = {"from": "+1-555-0177", "body": "hi there?", "message_id": "SM1"}
    assert "duplicate" not in _post_sms(client, demo_tenant, sms, tid="demo").json()
    assert _post_sms(client, demo_tenant, sms, tid="demo").json()["duplicate"]
    raw = _email("dup@example.invalid", "dup", "hello?", mid="<dup-1@example.invalid>")
    assert _post_email(client, demo_tenant, raw, tid="demo").json()["inbound_id"]
    assert _post_email(client, demo_tenant, raw, tid="demo").json()["duplicate"]
    with demo_tenant.db() as c:
        assert c.execute("SELECT COUNT(*) FROM inbound_messages WHERE ext_id='dup@example.invalid'").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM inbound_messages WHERE channel='telegram' AND ext_id='9001'").fetchone()[0] == 1


def test_slack_acks_fast_and_drafts_in_the_background_when_ai_is_on(client, demo_tenant, monkeypatch):
    from operator_ai import jobs, llm

    monkeypatch.setattr(llm, "unavailable_reason", lambda *a, **k: None)
    monkeypatch.setattr(llm, "complete_or_replay", lambda *a, **k: (_ for _ in ()).throw(llm.AIUnavailable("test")))
    ss = vault.ensure_practice_secret(demo_tenant, "slack_signing_secret")
    body = json.dumps({"type": "event_callback", "event_id": "EvFast", "event": {"type": "message", "channel_type": "im",
                                                                                  "text": "hello?", "channel": "D1", "user": "U7"}}).encode()
    ts = str(int(time.time()))
    h = {"x-slack-request-timestamp": ts, "x-slack-signature": channels.slack_sign(ss, ts, body), "content-type": "application/json"}
    r = client.post("/t/demo/hooks/slack", content=body, headers=h).json()
    assert r["accepted"] is True and r["job"] and "queued" not in r
    jobs.drain(demo_tenant, timeout=20)
    with demo_tenant.db() as c:
        row = c.execute("SELECT party_type, queue_id FROM inbound_messages WHERE channel='slack' AND ext_id='U7'").fetchone()
    assert row["party_type"] == "unknown" and queue.get(demo_tenant, row["queue_id"])["recipient_ref"] == "D1"


def test_signed_timestamps_required_outside_practice(client, tenant):
    payload = {"from": "+14155550100", "body": "hello?"}
    assert _post_sms(client, tenant, payload, ts=False).status_code == 401  # body-only signature: practice only
    assert _post_sms(client, tenant, payload, stale=True).status_code == 401
    secret = vault.ensure_practice_secret(tenant, "sms_webhook_secret")
    body = json.dumps(payload).encode()
    ts = str(int(time.time()))
    bad = {"x-signature": channels.sign_body_ts(secret, ts, b"other"), "x-signature-timestamp": ts, "content-type": "application/json"}
    assert client.post("/t/owner/hooks/sms", content=body, headers=bad).status_code == 401
    junk = {"x-signature": "x", "x-signature-timestamp": "yesterday", "content-type": "application/json"}
    assert client.post("/t/owner/hooks/sms", content=body, headers=junk).status_code == 401
    r = _post_sms(client, tenant, payload)
    assert r.status_code == 200 and r.json()["ignored"].startswith("not opted in")
    assert _post_email(client, tenant, _email("a@example.invalid", "s", "b"), ts=False).status_code == 401
    assert _post_sms(client, tenant, {"body": "no sender"}).status_code == 422
    assert client.post("/t/owner/hooks/sms", content=b"[1,2]", headers={**bad}).status_code in (401, 422)


@pytest.mark.parametrize("phrase", ["Stop.", "stop please", "Unsubscribe me", "STOPALL", "END", "QUIT", "OPT OUT", "REVOKE", "cancel"])
def test_sms_opt_out_phrases(client, demo_tenant, phrase):
    num = "+1-555-0163"
    assert _post_sms(client, demo_tenant, {"from": num, "body": "START"}, tid="demo").json()["opted_in"] is True
    assert channels.opted_in(demo_tenant, "sms", num)
    before = queue.counts(demo_tenant)
    r = _post_sms(client, demo_tenant, {"from": num, "body": phrase}, tid="demo").json()
    assert r["opted_in"] is False and r["dnc"] is True and r["queued"] is None
    assert not channels.opted_in(demo_tenant, "sms", num)
    assert queue.counts(demo_tenant).get("pending", 0) == before.get("pending", 0)  # never an auto-drafted reply
    # outbound SMS to them is blocked until they opt back in
    assert queue.add(demo_tenant, "reminder", "See you", channel="sms", recipient_ref=num)["status"] == "held"
    assert _post_sms(client, demo_tenant, {"from": num, "body": "START"}, tid="demo").json()["opted_in"] is True
    assert channels.opted_in(demo_tenant, "sms", num) and num.lower() not in (demo_tenant.get("blocked_attendees") or [])


def test_cancel_my_call_is_not_an_opt_out(client, demo_tenant):
    num = "+1-555-0164"
    _post_sms(client, demo_tenant, {"from": num, "body": "START"}, tid="demo")
    r = _post_sms(client, demo_tenant, {"from": num, "body": "Cancel my call tomorrow please"}, tid="demo").json()
    assert not r.get("dnc") and channels.opted_in(demo_tenant, "sms", num)


def test_telegram_checkin_validation_never_crashes(client):
    from operator_ai import tenancy

    demo_login(client, "student")
    code = client.post("/t/demo/api/channels/link-code").json()["code"]
    t = tenancy.get("demo")
    H = {"x-telegram-bot-api-secret-token": vault.ensure_practice_secret(t, "telegram_webhook_secret")}
    send = lambda text: client.post("/t/demo/hooks/telegram", headers=H, json={"message": {"chat": {"id": 4343, "type": "private"},
                                                                                           "text": text}})
    assert send(f"/link {code}").json()["linked"] is True
    with t.db() as c:
        n0 = c.execute("SELECT COUNT(*) FROM checkins").fetchone()[0]
    for bad in ("/checkin 99999999999999999999999", "/checkin 40 3 2 9", "/checkin 200000 1 1 3", "/checkin 10 2 5 3"):
        r = send(bad)
        assert r.status_code == 200 and r.json()["status"] == 422 and r.json()["checkin"] is False, bad
    assert send("/checkin 40 3 2 4").json()["checkin"] is True
    with t.db() as c:
        assert c.execute("SELECT COUNT(*) FROM checkins").fetchone()[0] == n0 + 1
    assert client.post("/t/demo/hooks/telegram", headers=H, content=b"{bad", ).status_code == 422
    assert client.post("/t/demo/hooks/telegram", headers=H, json={"message": {"chat": {"id": 1}, "text": 5}}).json()["ignored"] == "empty"
    # STOP on Telegram opts the chat out; /start brings it back
    assert send("stop").json()["opted_out"] is True
    assert send("/start").json()["opted_in"] is True


# ---------------------------------------------------------------- payment webhook account control
def _pay(client, t, ev, tid="owner"):
    secret = vault.ensure_practice_secret(t, console.PAYMENT_SECRET)
    body = json.dumps(ev).encode()
    return client.post(f"/t/{tid}/hooks/payments", content=body, headers={"x-signature": console.sign_payment(secret, body)})


def test_payment_webhook_only_changes_student_accounts(client, tenant):
    cid = auth.create_user(tenant, "coach@example.invalid", PW, "coach")
    with tenant.db() as c:
        c.execute("UPDATE users SET active=0 WHERE id=?", (cid,))
    r = _pay(client, tenant, {"id": "e1", "type": "payment.succeeded", "data": {"email": "coach@example.invalid"}}).json()
    assert r["account"].startswith("unchanged")
    with tenant.db() as c:
        assert c.execute("SELECT active FROM users WHERE id=?", (cid,)).fetchone()[0] == 0
    # "owner" is not an email, so nothing happens and the owner stays signed in
    login(client)
    assert _pay(client, tenant, {"id": "e2", "type": "payment.failed", "data": {"email": "owner"}}).status_code == 422
    assert client.get("/t/owner/api/me").status_code == 200
    assert _pay(client, tenant, {"id": "e3", "type": "payment.failed", "data": {"email": "not an email"}}).status_code == 422
    # a student linked by their student record is paused on failure and restored on payment
    sid_user = _user(tenant, "paying-student", student_email="paying@example.invalid")
    assert _pay(client, tenant, {"id": "e4", "type": "payment.failed", "data": {"email": "PAYING@example.invalid"}}).json()["access"] is False
    with tenant.db() as c:
        assert c.execute("SELECT active FROM users WHERE id=?", (sid_user,)).fetchone()[0] == 0
    _pay(client, tenant, {"id": "e5", "type": "payment.succeeded", "data": {"email": "paying@example.invalid"}})
    with tenant.db() as c:
        assert c.execute("SELECT active FROM users WHERE id=?", (sid_user,)).fetchone()[0] == 1
        owner_active = c.execute("SELECT active FROM users WHERE role='owner'").fetchone()[0]
    assert owner_active == 1


# ---------------------------------------------------------------- escalations
def test_escalation_api_rbac_and_lifecycle(client, tenant):
    a = _user(tenant, "op-a")
    _user(tenant, "op-b")
    eid = escalations.raise_(tenant, "speed_to_lead", "high", "Reply waiting", assignee_user_id=a, ref="t:1")
    assert escalations.raise_(tenant, "speed_to_lead", "high", "Reply waiting", assignee_user_id=a, ref="t:1") == eid
    login(client, "owner", "op-b", PW)
    assert client.get("/t/owner/api/escalations").json()["items"] == []
    assert client.post(f"/t/owner/api/escalations/{eid}/ack").status_code == 404
    assert client.post("/t/owner/api/escalations/check").status_code == 403
    assert client.put("/t/owner/api/inbox/settings", json={"telegram_alerts": True}).status_code == 403
    login(client, "owner", "op-a", PW)
    items = client.get("/t/owner/api/escalations").json()["items"]
    assert [e["id"] for e in items] == [eid] and items[0]["label"]
    assert client.post(f"/t/owner/api/escalations/{eid}/ack").json()["status"] == "acked"
    assert client.post(f"/t/owner/api/escalations/{eid}/ack").status_code == 409
    assert client.post(f"/t/owner/api/escalations/{eid}/resolve", json={"note": "Called them"}).json()["status"] == "resolved"
    assert client.post(f"/t/owner/api/escalations/{eid}/resolve").status_code == 409
    assert client.post("/t/owner/api/escalations/abc/ack").status_code == 422
    assert client.get("/t/owner/api/escalations?status=bogus").status_code == 422
    assert client.post(f"/t/owner/api/escalations/{eid}/resolve", json={"note": "x" * 600}).status_code == 422
    assert escalations.raise_(tenant, "speed_to_lead", "high", "Again", assignee_user_id=a, ref="t:1") != eid
    login(client)
    assert len(client.get("/t/owner/api/escalations?status=all").json()["items"]) == 2


def test_sla_checks_raise_and_close_escalations(tenant):
    a = _user(tenant, "op-a")
    _prospect(tenant, a, email="fast@example.invalid")
    res = inbound.route(tenant, "email", "fast@example.invalid", "Yes please, let's talk", {})
    fresh = inbound.route(tenant, "email", "fast@example.invalid", "Sounds good!", {})
    with tenant.db() as c:
        c.execute("UPDATE inbound_messages SET created=? WHERE id=?", (time.time() - 61 * 60, res["inbound_id"]))
        c.execute("UPDATE inbound_messages SET created=? WHERE id=?", (time.time() - 30 * 60, fresh["inbound_id"]))
    out = inbound.check_slas(tenant)
    assert out["speed_to_lead"] == 1
    esc = _escalations(tenant, "speed_to_lead")
    assert len(esc) == 1 and esc[0]["assignee_user_id"] == a and esc[0]["ref"] == f"inbound:{res['inbound_id']}"
    inbound.check_slas(tenant)
    assert len(_escalations(tenant, "speed_to_lead")) == 1  # no duplicates
    queue.approve(tenant, res["queued"], "op-a(student)", "student", user_id=a)
    assert _escalations(tenant, "speed_to_lead")[0]["status"] == "resolved"  # closed the moment an answer is approved
    assert inbound.check_slas(tenant)["speed_to_lead"] == 0
    # the SLA check also closes one whose answer was approved some other way (e.g. a person wrote their own)
    _prospect(tenant, a, name="Second Prospect", email="second@example.invalid")
    other = inbound.route(tenant, "email", "second@example.invalid", "Yes, keen to talk", {})
    with tenant.db() as c:
        c.execute("UPDATE inbound_messages SET created=? WHERE id=?", (time.time() - 90 * 60, other["inbound_id"]))
    assert inbound.check_slas(tenant)["speed_to_lead"] == 1
    with tenant.db() as c:
        c.execute("UPDATE queue SET status='approved', decided_at=? WHERE id=?", (time.time(), other["queued"]))
    assert inbound.check_slas(tenant)["resolved"] == 1
    # a draft held by guardrails for over a day
    held = queue.add(tenant, "prospect_dm", "We guarantee results", channel="instagram_dm", recipient_ref="@x",
                     meta={"prospect_facing": True, "owner_user_id": a})
    with tenant.db() as c:
        c.execute("UPDATE queue SET created=? WHERE id=?", (time.time() - 25 * 3600, held["id"]))
    assert inbound.check_slas(tenant)["held_item"] == 1
    queue.edit(tenant, held["id"], "Would a short call help?", "owner(owner)")
    inbound.check_slas(tenant)
    assert _escalations(tenant, "held_item")[0]["status"] == "resolved"
    # background work that failed after its retries, and the missions helper
    with tenant.db() as c:
        c.execute("INSERT INTO jobs(key,kind,payload,status,attempts,created,updated) VALUES('k','demo_kind','{}','failed',4,?,?)",
                  (time.time(), time.time()))
    assert inbound.check_slas(tenant)["step_failed"] == 1
    assert escalations.step_failed(tenant, 7, 3, attempts=2) is None
    assert escalations.step_failed(tenant, 7, 3, attempts=3) == escalations.step_failed(tenant, 7, 3, attempts=4)


def test_check_slas_is_registered_as_a_tenant_tick():
    from operator_ai import scheduler

    assert "operator_ai.inbound:check_slas" in scheduler.TENANT_TICKS
    fn = scheduler._resolve("operator_ai.inbound:check_slas")
    assert fn is inbound.check_slas and fn.demo_too is True


def test_crash_recovery_marks_stale_runs_interrupted(env):
    from fastapi.testclient import TestClient

    from operator_ai import tenancy
    from operator_ai.app import create_app

    tenancy.ensure_defaults()
    t = tenancy.get("owner")
    with t.db() as c:
        old = c.execute("INSERT INTO runs(agent,title,status,created) VALUES('x','old','running',?)", (time.time() - 700,)).lastrowid
        new = c.execute("INSERT INTO runs(agent,title,status,created) VALUES('x','new','running',?)", (time.time() - 60,)).lastrowid
    with TestClient(create_app(start_workers=False)):
        pass
    with t.db() as c:
        assert c.execute("SELECT status FROM runs WHERE id=?", (old,)).fetchone()[0] == "interrupted"
        assert c.execute("SELECT status FROM runs WHERE id=?", (new,)).fetchone()[0] == "running"
    assert _escalations(t, "runs_interrupted")
    assert escalations.recover_runs(t) == 0


def test_alert_drafted_to_own_telegram_only_when_switched_on(client, tenant):
    login(client)
    with tenant.db() as c:
        owner_id = c.execute("SELECT id FROM users WHERE role='owner'").fetchone()[0]
        c.execute("INSERT INTO channel_links(channel,ext_id,user_id,status,created) VALUES('telegram','777',?, 'linked', ?)",
                  (owner_id, time.time()))
    escalations.raise_(tenant, "complaint", "high", "Off by default", ref="a:0")
    with tenant.db() as c:
        assert c.execute("SELECT COUNT(*) FROM queue WHERE channel='telegram'").fetchone()[0] == 0
    s = client.put("/t/owner/api/inbox/settings", json={"telegram_alerts": True}).json()
    assert s["telegram_alerts"] is True and s["telegram_linked"] is True
    assert client.put("/t/owner/api/inbox/settings", json={"telegram_alerts": "maybe"}).status_code == 422
    escalations.raise_(tenant, "complaint", "high", "Someone complained", ref="a:1")
    items = queue.list_items(tenant, kind="channel_reply")
    assert len(items) == 1 and items[0]["recipient_ref"] == "777" and items[0]["status"] == "pending"
    assert items[0]["meta"]["internal_alert"] and items[0]["meta"]["owner_user_id"] == owner_id
    assert not items[0]["meta"]["prospect_facing"]


# ---------------------------------------------------------------- inbox views, triage and re-sorting
def test_coach_sees_their_students_conversations(client, tenant):
    c1 = auth.create_user(tenant, "coach1", PW, "coach")
    auth.create_user(tenant, "coach2", PW, "coach")
    _user(tenant, "learner", student_email="learner2@example.invalid", coach_id=c1)
    inbound.route(tenant, "email", "learner2@example.invalid", "How long should a Thunder Dome test run?", {})
    login(client, "owner", "coach1", PW)
    assert len(client.get("/t/owner/api/inbox").json()["items"]) == 1
    login(client, "owner", "coach2", PW)
    assert client.get("/t/owner/api/inbox").json()["items"] == []
    assert client.get("/t/owner/api/inbox?view=bogus").status_code == 422
    assert client.get("/t/owner/api/inbox/thread?key=nonsense").status_code == 422
    assert client.get("/t/owner/api/inbox?limit=0").status_code == 422


def test_triage_links_an_unknown_sender_and_reclassify(client, tenant):
    a = _user(tenant, "op-a")
    pid = _prospect(tenant, a, name="Sam Prospect", email="sam.work@example.invalid")
    d = inbound.route(tenant, "email", "Sam <sam.personal@example.invalid>", "Interested! When can we talk?", {"subject": "hi"})
    assert d["party_type"] == "unknown"
    holding = d["queued"]
    login(client, "owner", "op-a", PW)
    assert client.post(f"/t/owner/api/inbox/{d['inbound_id']}/assign", json={"party_type": "prospect", "party_id": pid}).status_code == 403
    login(client)
    assert client.get("/t/owner/api/inbox/parties?q=sam").json()["items"][0]["party_id"] == pid
    assert client.post(f"/t/owner/api/inbox/{d['inbound_id']}/assign", json={"party_type": "prospect", "party_id": 999}).status_code == 404
    assert client.post(f"/t/owner/api/inbox/{d['inbound_id']}/assign", json={"party_type": "wizard", "party_id": pid}).status_code == 422
    r = client.post(f"/t/owner/api/inbox/{d['inbound_id']}/assign", json={"party_type": "prospect", "party_id": pid})
    assert r.status_code == 200, r.text
    new = queue.get(tenant, r.json()["queued"])
    assert new["kind"] == "prospect_email" and new["meta"]["owner_user_id"] == a
    assert queue.get(tenant, holding)["status"] == "rejected"
    assert client.post(f"/t/owner/api/inbox/{d['inbound_id']}/assign", json={"party_type": "prospect", "party_id": pid}).status_code == 409
    # the next message from that address matches the prospect straight away
    again = inbound.route(tenant, "email", "sam.personal@example.invalid", "Thursday works", {})
    assert again["party_type"] == "prospect" and again["party_id"] == pid
    # the operator corrects the sorting: stage and draft follow
    login(client, "owner", "op-a", PW)
    rc = client.post(f"/t/owner/api/inbox/{again['inbound_id']}/classify", json={"classification": "not_interested"}).json()
    assert rc["classification"] == "not_interested" and rc["class_mode"] == "person"
    with tenant.db() as c:
        assert c.execute("SELECT stage FROM prospects WHERE id=?", (pid,)).fetchone()[0] == "lost"
    assert queue.get(tenant, again["queued"])["status"] == "rejected"
    assert client.post(f"/t/owner/api/inbox/{again['inbound_id']}/classify", json={"classification": "buy_now"}).status_code == 422
    assert client.post(f"/t/owner/api/inbox/{again['inbound_id']}/dismiss").json()["status"] == "dismissed"


def test_demo_data_only_in_the_demo_workspace(client, demo_tenant, tenant):
    with demo_tenant.db() as c:
        rows = db.rows(c.execute("SELECT * FROM inbound_messages"))
        convs = db.rows(c.execute("SELECT ext_id FROM conversations"))
    assert rows and all("(DEMO)" in r["body"] or r["party_type"] == "student" for r in rows)
    assert all("@" not in cv["ext_id"] or cv["ext_id"].endswith("@example.invalid") for cv in convs)
    assert _escalations(demo_tenant, "speed_to_lead")  # a positive reply waiting over an hour in the pitch demo
    assert inbound.seed_demo(tenant) == {"seeded": 0}
    with tenant.db() as c:
        assert c.execute("SELECT COUNT(*) FROM inbound_messages").fetchone()[0] == 0
    login(client)
    empty = client.get("/t/owner/api/inbox").json()
    assert empty["items"] == [] and empty["counts"]["conversations"] == 0
    assert client.get("/t/owner/api/escalations").json()["items"] == []
    demo_login(client, "owner")
    assert client.get("/t/demo/api/inbox").json()["counts"]["conversations"] >= 5
