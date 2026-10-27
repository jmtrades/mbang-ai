"""Review fixes for the inbound feature: each test failed before its fix (see the commit message)."""
from __future__ import annotations

import json
import sys
import time
import types

import pytest

from operator_ai import channels, escalations, guardrails, inbound, queue, vault
from tests.conftest import login
from tests.test_inbound import PW, _email, _post_email, _prospect, _prospect_email_ready, _user  # noqa: F401 (autouse)

BIG = 99999999999999999999  # past SQLite's 64-bit integers


def _tg(client, t, update, tid="demo"):
    secret = vault.ensure_practice_secret(t, "telegram_webhook_secret")
    return client.post(f"/t/{tid}/hooks/telegram", headers={"x-telegram-bot-api-secret-token": secret}, json=update)


def _sms(client, t, text, mid, number="+1-555-0177", tid="demo"):
    secret = vault.ensure_practice_secret(t, "sms_webhook_secret")
    body = json.dumps({"from": number, "body": text, "message_id": mid}).encode()
    now = str(int(time.time()))
    return client.post(f"/t/{tid}/hooks/sms", content=body, headers={
        "content-type": "application/json", "x-signature": channels.sign_body_ts(secret, now, body),
        "x-signature-timestamp": now})


def _slack(client, t, event, event_id, tid="demo"):
    secret = vault.ensure_practice_secret(t, "slack_signing_secret")
    body = json.dumps({"type": "event_callback", "event_id": event_id, "event": event}).encode()
    ts = str(int(time.time()))
    return client.post(f"/t/{tid}/hooks/slack", content=body, headers={
        "content-type": "application/json", "x-slack-request-timestamp": ts,
        "x-slack-signature": channels.slack_sign(secret, ts, body)})


# ---------------------------------------------------------------- malformed input: 422, never 500
def test_huge_ids_are_rejected_with_422_not_a_crash(client, tenant):
    login(client)
    calls = [
        client.post(f"/t/owner/api/inbox/{BIG}/dismiss"),
        client.post(f"/t/owner/api/inbox/{BIG}/classify", json={"classification": "positive"}),
        client.post(f"/t/owner/api/inbox/{BIG}/assign", json={"party_type": "prospect", "party_id": 1}),
        client.post("/t/owner/api/inbox/1/assign", json={"party_type": "prospect", "party_id": BIG}),
        client.post(f"/t/owner/api/escalations/{BIG}/ack"),
        client.post(f"/t/owner/api/escalations/{BIG}/resolve"),
        client.post("/t/owner/api/inbox/capture", json={"handle": "@x", "body": "hi", "prospect_id": BIG}),
        client.post("/t/owner/api/inbox/0/dismiss"),
    ]
    assert [r.status_code for r in calls] == [422] * len(calls)
    # in range but unknown: a plain 404
    assert client.post("/t/owner/api/inbox/123456/dismiss").status_code == 404
    assert client.post("/t/owner/api/escalations/123456/ack").status_code == 404


@pytest.mark.parametrize("raw,status", [
    (b'From: "\nSubject: x\nContent-Type: text/plain\n\nhi\n', 422),  # the stdlib parser raises on this header
    (b'From: "Jane <jane.broken@example.invalid>\nSubject: =?bogus?q?x?=\n\nhello\n', 200),
])
def test_malformed_email_headers_never_crash_the_webhook(client, tenant, raw, status):
    r = _post_email(client, tenant, raw)
    assert r.status_code == status, r.text
    if status == 200:
        assert r.json()["parsed"]["from"] == "jane.broken@example.invalid"


def test_blank_ai_tiebreak_answer_falls_back_to_the_rules(tenant, monkeypatch):
    from operator_ai import llm

    a = _user(tenant, "op-a")
    _prospect(tenant, a, email="vague@example.invalid")
    for blank in ("", "   \n", None):
        monkeypatch.setattr(llm, "complete_or_replay", lambda *x, b=blank, **k: {"text": b, "mode": "live"})
        out = inbound.classify_reply("hmm ok then", tenant)
        assert out["label"] == "question" and out["unclear"] and out["mode"] == "rules"
    res = inbound.route(tenant, "email", "vague@example.invalid", "hmm ok then", {})  # crashed with IndexError before
    assert res["classification"] == "question" and queue.get(tenant, res["queued"])["status"] == "needs_human"


# ---------------------------------------------------------------- SLA escalations a person closed stay closed
def test_a_resolved_sla_escalation_is_not_raised_again_every_tick(tenant):
    owner_id = 1
    with tenant.db() as c:
        c.execute("INSERT INTO channel_links(channel,ext_id,user_id,status,created) VALUES('telegram','7001',?,'linked',?)",
                  (owner_id, time.time()))
    tenant.set("escalation_telegram_alerts", True)
    item = queue.add(tenant, "dm_opener", "We guarantee results in 30 days", channel="instagram_dm", recipient_ref="@x",
                     meta={"prospect_facing": True})
    assert item["status"] == "held"
    with tenant.db() as c:
        c.execute("UPDATE queue SET created=? WHERE id=?", (time.time() - 30 * 3600, item["id"]))
    inbound.check_slas(tenant)
    owner = {"role": "owner", "id": owner_id}
    held = [e for e in escalations.list_for(tenant, owner) if e["kind"] == "held_item"]
    assert len(held) == 1
    alerts_before = len(queue.list_items(tenant, kind="channel_reply"))
    escalations.resolve(tenant, held[0]["id"], owner, "owner(owner)", "Leaving it held on purpose")
    for _ in range(3):
        out = inbound.check_slas(tenant)
        assert out["held_item"] == 0
    assert [e for e in escalations.list_for(tenant, owner) if e["kind"] == "held_item"] == []
    assert len(queue.list_items(tenant, kind="channel_reply")) == alerts_before  # no new Telegram alert drafts


def test_speed_to_lead_resolved_by_a_person_stays_closed(tenant):
    a = _user(tenant, "op-a")
    _prospect(tenant, a, email="keen@example.invalid")
    res = inbound.route(tenant, "email", "keen@example.invalid", "Yes, sounds good", {})
    with tenant.db() as c:
        c.execute("UPDATE inbound_messages SET created=? WHERE id=?", (time.time() - 2 * 3600, res["inbound_id"]))
    assert inbound.check_slas(tenant)["speed_to_lead"] == 1
    esc = escalations.list_for(tenant, {"role": "owner", "id": 1})[0]
    escalations.resolve(tenant, esc["id"], {"role": "owner", "id": 1}, "owner(owner)", "Called them instead")
    assert inbound.check_slas(tenant)["speed_to_lead"] == 0
    assert [e for e in escalations.list_for(tenant, {"role": "owner", "id": 1}) if e["kind"] == "speed_to_lead"] == []


def test_a_rejected_or_cancelled_draft_does_not_count_as_an_answer(tenant):
    a = _user(tenant, "op-a")
    _prospect(tenant, a, email="waiting@example.invalid")
    res = inbound.route(tenant, "email", "waiting@example.invalid", "Yes please, tell me more", {})
    with tenant.db() as c:
        c.execute("UPDATE inbound_messages SET created=? WHERE id=?", (time.time() - 2 * 3600, res["inbound_id"]))
        c.execute("UPDATE queue SET status='cancelled', decided_at=? WHERE id=?", (time.time(), res["queued"]))
    assert inbound.check_slas(tenant)["speed_to_lead"] == 1


# ---------------------------------------------------------------- crash recovery: the recent runs too
def test_the_tick_interrupts_runs_left_over_from_before_the_restart(tenant, monkeypatch):
    now = time.time()
    boot = now - 300
    monkeypatch.setattr(inbound, "_process_started", lambda: boot)
    with tenant.db() as c:
        old = c.execute("INSERT INTO runs(agent,title,status,created) VALUES('x','before restart, 15 min',"
                        "'running',?)", (now - 900,)).lastrowid
        young = c.execute("INSERT INTO runs(agent,title,status,created) VALUES('x','before restart, 6 min',"
                          "'running',?)", (now - 400,)).lastrowid
        mine = c.execute("INSERT INTO runs(agent,title,status,created) VALUES('x','this process',"
                         "'running',?)", (now - 60,)).lastrowid
    # the startup check (10-minute rule) left the 6-minute-old run alone; the tick picks it up once it is old enough
    out = inbound.check_slas(tenant)
    assert out["runs_interrupted"] == 1
    monkeypatch.setattr(inbound.db, "now", lambda: now + 400)
    inbound.check_slas(tenant)
    with tenant.db() as c:
        st = {r["id"]: r["status"] for r in c.execute("SELECT id, status FROM runs WHERE id IN (?,?,?)", (old, young, mine))}
    assert st == {old: "interrupted", young: "interrupted", mine: "running"}  # never cut a run this process started


# ---------------------------------------------------------------- guardrails on drafts to strangers
def test_a_holding_reply_to_a_stranger_gets_the_outreach_rules(client, tenant):
    _user(tenant, "op-a")
    login(client, "owner", "op-a", PW)
    d = client.post("/t/owner/api/inbox/capture", json={"handle": "@stranger_x", "body": "hey what is this"}).json()
    item = queue.get(tenant, d["queued"])
    assert d["party_type"] == "unknown" and item["meta"]["prospect_facing"] is True and item["meta"]["lead_tier"] == "cold"
    assert item["meta"]["owner_user_id"]  # the operator approves their own holding reply ...
    for text, code in (("The programme investment is $5,000, book now", "outreach_price"),
                       ("Let me make you $50k-$100k a month through ads", "cold_lead_figure")):
        r = client.put(f"/t/owner/api/queue/{item['id']}", json={"body": text})
        assert r.status_code == 200 and r.json()["status"] == "held", text  # ... but cannot edit a price into it
        assert code in [f["code"] for f in r.json()["flags"]]
        assert client.post(f"/t/owner/api/queue/{item['id']}/approve").status_code == 409
    # the same for a stranger who emails in
    raw = _email("Somebody <somebody@example.invalid>", "Hello", "What do you do?")
    hold = queue.get(tenant, _post_email(client, tenant, raw).json()["queued"])
    assert hold["meta"]["prospect_facing"] is True and not guardrails.blocked(hold["flags"])


def test_one_holding_draft_per_stranger_not_one_per_message(client, demo_tenant):
    ids = set()
    for i in range(4):
        upd = {"update_id": 880000 + i, "message": {"chat": {"id": 4242, "type": "private"}, "text": f"hello {i}?"}}
        ids.add(_tg(client, demo_tenant, upd).json()["queued"])
    assert len(ids) == 1
    item = queue.get(demo_tenant, ids.pop())
    assert item["meta"]["holding"] and item["status"] == "needs_human"
    queue.approve(demo_tenant, item["id"], "owner(owner)")
    with demo_tenant.db() as c:
        st = [r[0] for r in c.execute("SELECT status FROM inbound_messages WHERE conversation_id=?",
                                      (item["meta"]["conversation_id"],))]
    assert st == ["replied"] * 4  # one answer covers all four messages
    # a legal threat still gets its own (legal) holding line and escalation
    upd = {"update_id": 880010, "message": {"chat": {"id": 4242, "type": "private"}, "text": "I will call my lawyer"}}
    res = _tg(client, demo_tenant, upd).json()
    assert res["escalation_id"] and queue.get(demo_tenant, res["queued"])["body"] == inbound.HOLDING_LEGAL


# ---------------------------------------------------------------- pipeline edge cases
def test_a_dnc_stage_prospect_never_gets_a_draft_even_without_the_flag(tenant):
    a = _user(tenant, "op-a")
    _prospect(tenant, a, email="dnc@example.invalid", stage="dnc")
    res = inbound.route(tenant, "email", "dnc@example.invalid", "Yes, tell me more", {})
    assert res["party_type"] == "prospect" and res["queued"] is None


def test_a_won_prospect_reply_goes_to_a_person_not_the_setter_script(tenant):
    a = _user(tenant, "op-a")
    pid = _prospect(tenant, a, name="Wren Client", email="won@example.invalid", stage="won")
    res = inbound.route(tenant, "email", "won@example.invalid", "Yes, sounds good. Quick question about the report?", {})
    item = queue.get(tenant, res["queued"])
    assert item["kind"] == "channel_reply" and item["meta"]["holding"] and item["status"] == "needs_human"
    assert item["body"] == inbound.HOLDING_CLIENT.format(first="Wren") and "good fit" not in item["body"]
    assert item["meta"]["owner_user_id"] == a
    with tenant.db() as c:
        assert c.execute("SELECT stage FROM prospects WHERE id=?", (pid,)).fetchone()[0] == "won"


# ---------------------------------------------------------------- escalations RBAC
def test_only_the_owner_closes_a_legal_threat(client, tenant):
    a = _user(tenant, "op-a")
    _prospect(tenant, a, email="law@example.invalid")
    res = inbound.route(tenant, "email", "law@example.invalid", "My lawyer will be in touch about this", {})
    login(client, "owner", "op-a", PW)
    items = client.get("/t/owner/api/escalations").json()["items"]
    legal = next(e for e in items if e["id"] == res["escalation_id"])
    assert legal["kind"] == "legal" and legal["can_resolve"] is False
    assert client.post(f"/t/owner/api/escalations/{legal['id']}/ack").status_code == 200  # they can mark it seen
    r = client.post(f"/t/owner/api/escalations/{legal['id']}/resolve", json={"note": "ignore it"})
    assert r.status_code == 403
    dnc = inbound.route(tenant, "email", "law@example.invalid", "STOP", {})
    assert client.post(f"/t/owner/api/escalations/{dnc['escalation_id']}/resolve").status_code == 200  # info-only kinds: fine
    login(client)
    mine = client.get("/t/owner/api/escalations").json()["items"]
    assert next(e for e in mine if e["id"] == legal["id"])["can_resolve"] is True
    assert client.post(f"/t/owner/api/escalations/{legal['id']}/resolve", json={"note": "Spoke to them"}).status_code == 200


def test_thread_says_who_may_decide_each_draft(client, tenant):
    coach = _user(tenant, "coach1", "coach")
    a = _user(tenant, "op-a", coach_id=coach)
    with tenant.db() as c:
        c.execute("UPDATE students SET coach_id=? WHERE id=(SELECT student_id FROM users WHERE id=?)", (coach, a))
    pid = _prospect(tenant, a, email="pat@example.invalid")
    inbound.route(tenant, "email", "pat@example.invalid", "Sure, let's talk", {})
    login(client, "owner", "coach1", PW)
    out = [m for m in client.get(f"/t/owner/api/inbox/thread?key=prospect:{pid}").json()["messages"] if m["direction"] == "out"]
    assert out and out[0]["can_decide"] is False
    login(client, "owner", "op-a", PW)
    out = [m for m in client.get(f"/t/owner/api/inbox/thread?key=prospect:{pid}").json()["messages"] if m["direction"] == "out"]
    assert out[0]["can_decide"] is True


# ---------------------------------------------------------------- linking Slack / SMS, codes expire
def test_a_slack_student_links_with_a_code_and_keeps_the_coach(client, demo_tenant):
    from operator_ai import auth

    with demo_tenant.db() as c:
        uid = c.execute("SELECT id FROM users WHERE role='student' AND student_id IS NOT NULL ORDER BY id LIMIT 1").fetchone()[0]
    code = channels.link_code(demo_tenant, uid)
    q = "What are the rules for a Thunder Dome test?"
    before = _slack(client, demo_tenant, {"type": "message", "channel_type": "im", "user": "U0STUDENT", "channel": "D1",
                                          "text": q}, "EvA").json()
    assert before["party_type"] == "unknown"  # not linked yet: a holding reply, no course material
    assert queue.get(demo_tenant, before["queued"])["body"] == inbound.HOLDING
    linked = _slack(client, demo_tenant, {"type": "message", "channel_type": "im", "user": "U0STUDENT", "channel": "D1",
                                          "text": f"link {code.lower()}"}, "EvB").json()
    assert linked["linked"] is True
    after = _slack(client, demo_tenant, {"type": "message", "channel_type": "im", "user": "U0STUDENT", "channel": "D1",
                                         "text": q}, "EvC").json()
    assert after["party_type"] == "student" and after["covered"] == "yes"
    item = queue.get(demo_tenant, after["queued"])
    assert item["kind"] == "channel_reply" and item["recipient_ref"] == "D1" and not item["meta"].get("holding")
    assert auth.can_decide("student", item["kind"], item["meta"].get("owner_user_id"), uid) is False  # owner approves
    # a used code can't link anyone else
    again = _slack(client, demo_tenant, {"type": "message", "channel_type": "im", "user": "U0OTHER", "channel": "D2",
                                         "text": f"link {code}"}, "EvD").json()
    assert again["linked"] is False


def test_link_codes_expire_and_guessing_is_limited(client, demo_tenant):
    with demo_tenant.db() as c:
        uid = c.execute("SELECT id FROM users WHERE role='owner' ORDER BY id LIMIT 1").fetchone()[0]
    stale = channels.link_code(demo_tenant, uid)
    with demo_tenant.db() as c:
        c.execute("UPDATE channel_links SET created=? WHERE code=?", (time.time() - 2 * 3600, stale))
    upd = {"update_id": 990001, "message": {"chat": {"id": 31337, "type": "private"}, "text": f"/link {stale}"}}
    assert _tg(client, demo_tenant, upd).json()["linked"] is False  # an hour-old code no longer works
    fresh = channels.link_code(demo_tenant, uid)
    for i in range(channels.LINK_TRIES_PER_HOUR):
        upd = {"update_id": 990100 + i, "message": {"chat": {"id": 31337, "type": "private"}, "text": f"/link ZZZZ{i:02d}"}}
        assert _tg(client, demo_tenant, upd).json()["linked"] is False
    upd = {"update_id": 990200, "message": {"chat": {"id": 31337, "type": "private"}, "text": f"/link {fresh}"}}
    r = _tg(client, demo_tenant, upd).json()
    assert r["linked"] is False and "Too many tries" in r["error"]  # even the right code, after too many guesses
    upd = {"update_id": 990201, "message": {"chat": {"id": 4040, "type": "private"}, "text": f"/link {fresh}"}}
    assert _tg(client, demo_tenant, upd).json()["linked"] is True
    # SMS LINK obeys the same expiry
    with demo_tenant.db() as c:
        c.execute("UPDATE channel_links SET created=? WHERE code=?", (time.time() - 2 * 3600, stale))
    assert _sms(client, demo_tenant, f"LINK {stale}", "lk1").json()["linked"] is False


# ---------------------------------------------------------------- opt-out compliance
def test_yes_after_stop_does_not_opt_a_number_back_in(client, demo_tenant):
    assert _sms(client, demo_tenant, "YES", "y0", number="+1-555-0178").json()["ok"]
    assert channels.opted_in(demo_tenant, "sms", "+1-555-0178")  # YES still confirms a first opt-in
    _sms(client, demo_tenant, "START", "y1")
    _sms(client, demo_tenant, "STOP", "y2")
    _sms(client, demo_tenant, "Yes", "y3")
    assert channels.opted_in(demo_tenant, "sms", "+1-555-0177") is False
    _sms(client, demo_tenant, "START", "y4")
    assert channels.opted_in(demo_tenant, "sms", "+1-555-0177") is True


def test_stop_pulls_back_scheduled_and_send_by_hand_items(tenant):
    a = _user(tenant, "op-a")
    pid = _prospect(tenant, a, email="later@example.invalid", handle="@later_x")
    ids = []
    with tenant.db() as c:
        for st, ch, ref in (("scheduled", "email", "later@example.invalid"), ("awaiting_manual_send", "instagram_dm", "@later_x")):
            ids.append(c.execute("INSERT INTO queue(kind,channel,recipient_ref,title,body,meta,status,flags,created) "
                                 "VALUES('followup',?,?,?,?,?,?,?,?)",
                                 (ch, ref, "Follow-up", "Checking in", json.dumps({"prospect_id": pid}), st, "[]",
                                  time.time())).lastrowid)
    res = inbound.route(tenant, "email", "later@example.invalid", "Please stop emailing me", {})
    assert res["dnc"]
    for qid in ids:
        it = queue.get(tenant, qid)
        assert it["status"] == "held" and any(f["code"] == "do_not_contact" for f in it["flags"])


def test_stop_is_also_recorded_on_the_delivery_suppression_list_when_installed(tenant, monkeypatch):
    calls = []
    fake = types.ModuleType("operator_ai.delivery")
    fake.suppress = lambda t, address, channel, reason, party=None: calls.append((address, channel, party)) or 1
    monkeypatch.setitem(sys.modules, "operator_ai.delivery", fake)
    import operator_ai

    monkeypatch.setattr(operator_ai, "delivery", fake, raising=False)
    a = _user(tenant, "op-a")
    pid = _prospect(tenant, a, email="quiet@example.invalid")
    inbound.route(tenant, "email", "quiet@example.invalid", "Unsubscribe me", {})
    assert ("quiet@example.invalid", "email", ("prospect", pid)) in calls
    # and a broken suppression list never stops the opt-out itself
    fake.suppress = lambda *x, **k: (_ for _ in ()).throw(RuntimeError("down"))
    res = inbound.route(tenant, "email", "other@example.invalid", "STOP", {})
    assert res["dnc"] and "other@example.invalid" in tenant.get("blocked_attendees")


def test_a_crash_while_handling_a_webhook_lets_the_providers_retry_through(demo_tenant, monkeypatch):
    secret = vault.ensure_practice_secret(demo_tenant, "telegram_webhook_secret")
    upd = {"update_id": 770001, "message": {"chat": {"id": 5151, "type": "private"}, "text": "STOP"}}
    real = inbound.route

    def boom(*a, **k):
        raise RuntimeError("database briefly locked")

    monkeypatch.setattr(inbound, "route", boom)
    with pytest.raises(RuntimeError):
        channels.telegram(demo_tenant, secret, upd)
    monkeypatch.setattr(inbound, "route", real)
    res = channels.telegram(demo_tenant, secret, upd)  # the retry is handled, not dropped as a duplicate
    assert res.get("duplicate") is not True and res["dnc"] is True
    assert channels.telegram(demo_tenant, secret, upd)["duplicate"] is True  # and a later replay is still dropped
    # the same for SMS and email message ids
    sms_secret = vault.ensure_practice_secret(demo_tenant, "sms_webhook_secret")
    body = json.dumps({"from": "+1-555-0190", "body": "START", "message_id": "SMretry"}).encode()
    ts = str(int(time.time()))
    real_opt_in = inbound.reopt_in
    monkeypatch.setattr(inbound, "reopt_in", boom)
    with pytest.raises(RuntimeError):
        channels.sms(demo_tenant, channels.sign_body_ts(sms_secret, ts, body), body, json.loads(body), "sms", ts)
    monkeypatch.setattr(inbound, "reopt_in", real_opt_in)
    assert channels.sms(demo_tenant, channels.sign_body_ts(sms_secret, ts, body), body, json.loads(body), "sms", ts)["opted_in"]


def test_a_telegram_group_can_not_be_linked_to_one_person(client, demo_tenant):
    with demo_tenant.db() as c:
        uid = c.execute("SELECT id FROM users WHERE role='student' AND student_id IS NOT NULL ORDER BY id LIMIT 1").fetchone()[0]
    code = channels.link_code(demo_tenant, uid)
    grp = {"update_id": 660001, "message": {"chat": {"id": -100777, "type": "group"}, "text": f"/link {code}"}}
    r = _tg(client, demo_tenant, grp).json()
    assert r["linked"] is False and "private chat" in r["error"]
    # so a group member's check-in is never booked to that student
    ci = {"update_id": 660002, "message": {"chat": {"id": -100777, "type": "group"}, "text": "/checkin 40 3 2 4"}}
    assert _tg(client, demo_tenant, ci).json().get("checkin") is not True
    priv = {"update_id": 660003, "message": {"chat": {"id": 777, "type": "private"}, "text": f"/link {code}"}}
    assert _tg(client, demo_tenant, priv).json()["linked"] is True  # the same code still works in private


def test_for_your_info_escalations_do_not_draft_telegram_alerts(tenant):
    with tenant.db() as c:
        c.execute("INSERT INTO channel_links(channel,ext_id,user_id,status,created) VALUES('telegram','7002',1,'linked',?)",
                  (time.time(),))
    tenant.set("escalation_telegram_alerts", True)
    escalations.raise_(tenant, "dnc_request", "low", "Someone asked to stop", ref="fyi:1")
    assert queue.list_items(tenant, kind="channel_reply") == []
    escalations.raise_(tenant, "refund", "high", "Someone wants a refund", ref="urgent:1")
    alerts = queue.list_items(tenant, kind="channel_reply")
    assert len(alerts) == 1 and alerts[0]["meta"]["internal_alert"] and alerts[0]["recipient_ref"] == "7002"
