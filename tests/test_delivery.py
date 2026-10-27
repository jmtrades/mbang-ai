"""Delivery truth, scheduling, cadences, addressing and email compliance (2026-10 wave).

Positive and negative paths: the approval gate (atomic claims, concurrency), scheduled sends and cancel_if
rules, manual-channel confirmation, mock transports only in the demo, contact points and address parsing,
the never-invite list for handles and phones, prospect-email compliance and one-click unsubscribe, caps and
warm-up, reject reason codes and edit feedback, action-gate partial failure, the event-driven booking flow
(no free slots, expired times, no duplicate calendar events), RBAC and isolation, malformed input, AI off,
empty data and demo vs real workspaces. Everything offline: live providers are fakes."""
from __future__ import annotations

import json
import threading
import time

import pytest

from operator_ai import actions, auth, db, delivery, events, queue, scheduler, settings, skills, tenancy, vault
from operator_ai import calendar as cal
from operator_ai.adapters.ads import MockAdsAdapter
from operator_ai.agents import sales
from operator_ai.live import chain, net, senders
from tests.conftest import demo_login
from tests.fakes import FakeWorld

DAY = 86400


# ---------------------------------------------------------------- helpers
@pytest.fixture
def world(monkeypatch):
    w = FakeWorld()
    net.set_transport(w.transport)
    monkeypatch.setattr(net, "_sleep", lambda s: None)
    yield w
    net.set_transport(None)
    assert w.stray == []


class FakeSMTP:
    sent: list = []

    def __init__(self, host, port, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        pass

    def login(self, u, p):
        pass

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.sent = []
    monkeypatch.setattr(senders.smtplib, "SMTP", FakeSMTP)
    return FakeSMTP.sent


def _live(t, *channels):
    vault.put(t, {"telegram_bot_token": "123:fake", "slack_bot_token": "xoxb-fake", "sms_account_sid": "ACfake",
                  "sms_auth_token": "tok", "sms_from_number": "+1-555-0100", "smtp_host": "smtp.example.invalid",
                  "smtp_user": "u", "smtp_password": "p", "smtp_from": "Team <team@example.invalid>"})
    settings.set_box(t, "live_channels_enabled", True, "test: owner yes recorded")


def _address(t, addr="1 Example Street, Testtown"):
    skills.put_profile(t, {"identity": {"name": "Peak Growth (DEMO)", "postal_address": addr}}, "test")


def _prospect(t, owner_user_id=None, stage="contacted", email="pat.example@example.invalid", handle="@pat_demo") -> int:
    with t.db() as c:
        return c.execute("INSERT INTO prospects(owner_user_id,name,handle,email,niche,stage,do_not_contact,demo,created,updated) "
                         "VALUES(?,?,?,?,?,?,0,1,?,?)", (owner_user_id, "Pat Example (DEMO)", handle, email, "physio clinics",
                                                         stage, db.now(), db.now())).lastrowid


def _lead(t, handle="@test_demo", email="test.lead@example.invalid") -> int:
    with t.db() as c:
        return c.execute("INSERT INTO leads(name,handle,email,source,answers,stage,created,demo,anon_id) VALUES(?,?,?,?,?,?,?,?,?)",
                         ("Robin Test (DEMO)", handle, email, "referral",
                          db.jdump({"monthly_revenue": "100k+", "can_invest": "yes", "timeline": "now", "decision_maker": True,
                                    "business": "Peak Physio (DEMO)"}), "new", db.now(), 1, "L-x")).lastrowid


def _user(t, username, role, **kw) -> int:
    uid = auth.create_user(t, username, "pw-" + username + "-123456", role)
    if kw:
        with t.db() as c:
            for k, v in kw.items():
                c.execute(f"UPDATE users SET {k}=? WHERE id=?", (v, uid))
    return uid


def _events(t, name):
    return [e["payload"] for e in events.recent(t, name)]


# ---------------------------------------------------------------- atomic approval (the gate)
def test_concurrent_approvals_send_exactly_once(tenant, monkeypatch):
    calls = []

    def slow_send(t, ch, rcpt, body, subject=None, headers=None, idempotency_key=None):
        time.sleep(0.3)
        calls.append((ch, rcpt, idempotency_key))
        return "m1"

    monkeypatch.setattr(senders, "ready", lambda t, ch: ch == "telegram")
    monkeypatch.setattr(senders, "send", slow_send)
    it = queue.add(tenant, "channel_reply", "Here is the checklist.", channel="telegram", recipient_ref="4242")
    results = []

    def go():
        try:
            results.append(queue.approve(tenant, it["id"], "owner(owner)")["status"])
        except queue.QueueError as e:
            results.append(e.status)

    threads = [threading.Thread(target=go) for _ in range(5)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert results.count("sent") == 1 and sorted(r for r in results if r != "sent") == [409] * 4
    assert calls == [("telegram", "4242", f"q{it['id']}")]  # one provider call, idempotency key = the queue item
    assert len(_events(tenant, "item_sent")) == 1


def test_concurrent_http_approvals_send_once(app, monkeypatch):
    from fastapi.testclient import TestClient

    from tests.conftest import login

    calls = []
    monkeypatch.setattr(senders, "ready", lambda t, ch: ch == "telegram")
    monkeypatch.setattr(senders, "send", lambda *a, **k: (time.sleep(0.3), calls.append(1), "m1")[2])
    clients = []
    for _ in range(3):
        c = TestClient(app)
        c.__enter__()
        login(c)
        clients.append(c)
    t = tenancy.get("owner")
    it = queue.add(t, "channel_reply", "Here is the checklist.", channel="telegram", recipient_ref="4242")
    codes = []
    threads = [threading.Thread(target=lambda c=c: codes.append(c.post(f"/t/owner/api/queue/{it['id']}/approve").status_code))
               for c in clients]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    for c in clients:
        c.__exit__(None, None, None)
    assert sorted(codes) == [200, 409, 409] and len(calls) == 1


def test_reject_and_edit_lose_to_an_approval_in_flight(tenant, monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(senders, "ready", lambda t, ch: ch == "telegram")
    monkeypatch.setattr(senders, "send", lambda *a, **k: (gate.wait(5), "m1")[1])
    it = queue.add(tenant, "channel_reply", "Here is the checklist.", channel="telegram", recipient_ref="4242")
    th = threading.Thread(target=lambda: queue.approve(tenant, it["id"], "owner(owner)"))
    th.start()
    for _ in range(100):
        if queue.get(tenant, it["id"])["status"] == "sending":
            break
        time.sleep(0.02)
    assert queue.get(tenant, it["id"])["status"] == "sending"
    with pytest.raises(queue.QueueError) as e1:
        queue.reject(tenant, it["id"], "owner(owner)", reason_code="tone")
    with pytest.raises(queue.QueueError) as e2:
        queue.edit(tenant, it["id"], "changed under the approver", "owner(owner)")
    gate.set()
    th.join()
    assert e1.value.status == 409 and e2.value.status == 409
    done = queue.get(tenant, it["id"])
    assert done["status"] == "sent" and done["body"] == "Here is the checklist."


def test_failed_live_send_restores_the_open_status(tenant, world):
    _live(tenant)
    vault.put(tenant, {"telegram_bot_token": "BADTOKEN"})
    it = queue.add(tenant, "channel_reply", "hello", channel="telegram", recipient_ref="12345")
    with pytest.raises(queue.QueueError) as ei:
        queue.approve(tenant, it["id"], "owner(owner)")
    assert ei.value.status == 502 and "BADTOKEN" not in str(ei.value)
    assert queue.get(tenant, it["id"])["status"] == "pending"  # can be retried; nothing was marked sent


# ---------------------------------------------------------------- delivery truth: statuses
def test_mock_transport_only_in_demo(tenant, demo_tenant):
    real = queue.add(tenant, "welcome", "Welcome aboard.", channel="email", recipient_ref="new.member@example.invalid")
    r = queue.approve(tenant, real["id"], "owner(owner)")
    assert r["status"] == "awaiting_manual_send" and r["delivered"] is None and "isn't connected" in r["status_reason"]
    demo = queue.add(demo_tenant, "welcome", "Welcome aboard (DEMO).", channel="email", recipient_ref="new.member@example.invalid")
    d = queue.approve(demo_tenant, demo["id"], "owner(owner)")
    assert d["status"] == "sent" and d["delivered"] == "mock:email" and d["sent_at"]


@pytest.mark.parametrize("channel", ["instagram_dm", "linkedin", "phone"])
def test_manual_channels_wait_for_a_person_even_in_demo(demo_tenant, channel):
    rcpt = "+15550142" if channel == "phone" else "@lead_demo"
    if channel == "phone":
        with demo_tenant.db() as c:
            c.execute("INSERT INTO channel_links(channel,ext_id,opted_in,status,created) VALUES('phone',?,1,'opted_in',0)", (rcpt,))
    it = queue.add(demo_tenant, "dm", "Quick question about your clinic?", channel=channel, recipient_ref=rcpt)
    r = queue.approve(demo_tenant, it["id"], "owner(owner)")
    assert r["status"] == "awaiting_manual_send" and "can't be sent automatically" in r["status_reason"]
    before = len(_events(demo_tenant, "item_sent"))
    with pytest.raises(queue.QueueError):
        queue.approve(demo_tenant, it["id"], "owner(owner)")  # already decided
    s = queue.mark_sent(demo_tenant, it["id"], "owner(owner)", permalink="https://example.invalid/thread/1")
    assert s["status"] == "sent" and s["meta"]["permalink"] == "https://example.invalid/thread/1"
    assert len(_events(demo_tenant, "item_sent")) == before + 1
    with pytest.raises(queue.QueueError) as ei:
        queue.mark_sent(demo_tenant, it["id"], "owner(owner)")  # twice: refused
    assert ei.value.status == 409


def test_on_sent_fires_only_for_real_sends(tenant):
    seen = []
    queue.on_sent("test_truth")(lambda t, item, actor: seen.append(item["id"]))
    it = queue.add(tenant, "test_truth", "Hi there", channel="instagram_dm", recipient_ref="@x_demo")
    queue.approve(tenant, it["id"], "o")
    assert seen == []  # approved is not sent
    queue.mark_sent(tenant, it["id"], "o")
    assert seen == [it["id"]]


def test_on_sent_hook_failure_does_not_undo_a_send(tenant):
    def boom(t, item, actor):
        raise RuntimeError("a feature's hook has a bug")

    queue.on_sent("test_boom")(boom)
    it = queue.add(tenant, "test_boom", "Hi there", channel="instagram_dm", recipient_ref="@x_demo")
    queue.approve(tenant, it["id"], "o")
    assert queue.mark_sent(tenant, it["id"], "o")["status"] == "sent"
    assert _events(tenant, "item_sent")  # the event still goes out


def test_mark_sent_refused_for_open_items(tenant):
    it = queue.add(tenant, "dm", "Hi", channel="instagram_dm", recipient_ref="@x_demo")
    with pytest.raises(queue.QueueError) as ei:
        queue.mark_sent(tenant, it["id"], "o")
    assert ei.value.status == 409 and queue.get(tenant, it["id"])["status"] == "pending"


# ---------------------------------------------------------------- scheduling + dispatch
def test_scheduled_item_waits_then_sends(demo_tenant):
    t = demo_tenant
    due = time.time() + 2 * 3600
    it = queue.add(t, "reminder", "See you soon (DEMO).", channel="email", recipient_ref="lee@example.invalid",
                   meta={"send_after": due, "subject": "Our call"})
    r = queue.approve(t, it["id"], "owner(owner)")
    assert r["status"] == "scheduled" and r["send_after"] == pytest.approx(due) and r["delivered"] is None
    with t.db() as c:
        assert c.execute("SELECT COUNT(*) FROM channel_messages WHERE json_extract(meta,'$.queue_id')=?",
                         (it["id"],)).fetchone()[0] == 0
    assert delivery.dispatch_due(t)["due"] == 0 or queue.get(t, it["id"])["status"] == "scheduled"
    out = delivery.dispatch_due(t, now=due + 1)
    assert any(x["id"] == it["id"] and x["status"] == "sent" for x in out["results"]) and out["run_id"]
    assert queue.get(t, it["id"])["delivered"] == "mock:email"


def test_scheduled_live_send_goes_out_when_due(tenant, world):
    _live(tenant)
    it = queue.add(tenant, "channel_reply", "Your replay link is ready.", channel="telegram", recipient_ref="4242",
                   meta={"send_after": time.time() + 600})
    assert queue.approve(tenant, it["id"], "owner(owner)")["status"] == "scheduled"
    assert world.sent == []
    delivery.dispatch_due(tenant, now=time.time() + 601)
    assert queue.get(tenant, it["id"])["status"] == "sent" and len(world.sent) == 1


def test_send_now_and_cancel(demo_tenant):
    t = demo_tenant
    a = queue.add(t, "reminder", "See you soon (DEMO).", channel="email", recipient_ref="lee@example.invalid",
                  meta={"send_after": time.time() + DAY})
    b = queue.add(t, "reminder", "See you soon (DEMO).", channel="email", recipient_ref="lee@example.invalid",
                  meta={"send_after": time.time() + DAY})
    for x in (a, b):
        queue.approve(t, x["id"], "owner(owner)")
    assert queue.send_now(t, a["id"], "owner(owner)")["status"] == "sent"
    c = queue.cancel(t, b["id"], "owner(owner)", reason="They asked to move it")
    assert c["status"] == "cancelled" and c["status_reason"] == "They asked to move it"
    delivery.dispatch_due(t, now=time.time() + 2 * DAY)
    assert queue.get(t, b["id"])["status"] == "cancelled"  # the dispatcher never revives it
    with pytest.raises(queue.QueueError):
        queue.send_now(t, b["id"], "owner(owner)")  # cancelled stays cancelled
    p = queue.add(t, "note", "x")
    with pytest.raises(queue.QueueError) as ei:
        queue.cancel(t, p["id"], "owner(owner)")  # open items are rejected, not cancelled
    assert ei.value.status == 409


def test_dispatch_failure_marks_send_failed_then_retry(tenant, world):
    _live(tenant)
    it = queue.add(tenant, "channel_reply", "hello", channel="telegram", recipient_ref="4242",
                   meta={"send_after": time.time() + 600})
    queue.approve(tenant, it["id"], "owner(owner)")
    vault.put(tenant, {"telegram_bot_token": "BADTOKEN"})
    delivery.dispatch_due(tenant, now=time.time() + 601)
    f = queue.get(tenant, it["id"])
    assert f["status"] == "send_failed" and "Live send failed" in f["status_reason"] and "BADTOKEN" not in f["status_reason"]
    vault.put(tenant, {"telegram_bot_token": "123:fake"})
    assert queue.send_now(tenant, it["id"], "owner(owner)")["status"] == "sent"


def test_dispatch_reruns_guardrails(demo_tenant):
    t = demo_tenant
    it = queue.add(t, "reminder", "Quick note from Morgan Fictional (DEMO).", channel="email", recipient_ref="lee@example.invalid",
                   meta={"send_after": time.time() + 600})
    queue.approve(t, it["id"], "owner(owner)")
    t.set("banned_names", ["Morgan Fictional"])
    delivery.dispatch_due(t, now=time.time() + 601)
    held = queue.get(t, it["id"])
    assert held["status"] == "held" and any(f["code"] == "banned_name" for f in held["flags"])
    assert "after approval" in held["status_reason"]


def test_dispatch_rechecks_never_invite(demo_tenant):
    t = demo_tenant
    it = queue.add(t, "reminder", "See you soon (DEMO).", channel="email", recipient_ref="Lee <lee@example.invalid>",
                   meta={"send_after": time.time() + 600})
    queue.approve(t, it["id"], "owner(owner)")
    t.set("blocked_attendees", ["LEE@example.invalid"])
    delivery.dispatch_due(t, now=time.time() + 601)
    c = queue.get(t, it["id"])
    assert c["status"] == "cancelled" and "never-invite" in c["status_reason"]


def test_cancel_if_lead_replied_since(tenant):
    lid = _lead(tenant)
    it = queue.add(tenant, "followup", "Here's the 2-minute video. Want it?", channel="email",
                   recipient_ref="Robin <test.lead@example.invalid>",
                   meta={"lead_id": lid, "send_after": time.time() + DAY, "cancel_if": ["lead_replied_since"]})
    queue.approve(tenant, it["id"], "o")
    assert delivery.cancel_reason(tenant, queue.get(tenant, it["id"], scrub=False)) is None
    with tenant.db() as c:  # an inbound email from the same address (display name, other case)
        c.execute("INSERT INTO channel_messages(channel,direction,ext_id,body,meta,created) VALUES('email','in',?,?,?,?)",
                  ("ROBIN <Test.Lead@example.invalid>", "Thanks, will read it", "{}", db.now() + 1))
    delivery.dispatch_due(tenant, now=time.time() + DAY + 10)
    c = queue.get(tenant, it["id"])
    assert c["status"] == "cancelled" and "replied" in c["status_reason"]


def test_reply_event_cancels_pending_followups_immediately(tenant):
    lid = _lead(tenant)
    it = queue.add(tenant, "followup", "Here's the 2-minute video. Want it?", channel="instagram_dm", recipient_ref="@test_demo",
                   meta={"lead_id": lid, "send_after": time.time() + DAY, "cancel_if": ["lead_replied_since"]})
    queue.approve(tenant, it["id"], "o")
    events.emit(tenant, "reply_received", {"party_type": "lead", "party_id": lid, "classification": "neutral"})
    assert queue.get(tenant, it["id"])["status"] == "cancelled"


def test_cancel_if_stage_beyond_and_dnc(tenant):
    pid = _prospect(tenant, stage="contacted")
    a = queue.add(tenant, "followup", "One idea for your clinic. Want it?", channel="instagram_dm", recipient_ref="@pat_demo",
                  meta={"prospect_id": pid, "send_after": time.time() + DAY, "cancel_if": ["stage_beyond:replied"]})
    b = queue.add(tenant, "followup", "Another idea. Want it?", channel="instagram_dm", recipient_ref="@pat_demo",
                  meta={"prospect_id": pid, "send_after": time.time() + DAY, "cancel_if": ["prospect_dnc"]})
    for x in (a, b):
        queue.approve(tenant, x["id"], "o")
    with tenant.db() as c:
        c.execute("UPDATE prospects SET stage='call_booked' WHERE id=?", (pid,))
    assert "moved past the replied stage" in delivery.cancel_reason(tenant, queue.get(tenant, a["id"], scrub=False))
    with tenant.db() as c:
        c.execute("UPDATE prospects SET do_not_contact=1 WHERE id=?", (pid,))
    delivery.dispatch_due(tenant, now=time.time() + DAY + 10)
    assert queue.get(tenant, a["id"])["status"] == "cancelled"
    assert "do-not-contact" in queue.get(tenant, b["id"])["status_reason"]
    # and a new draft to a do-not-contact prospect is held at queue time
    n = queue.add(tenant, "followup", "Hi again", channel="instagram_dm", recipient_ref="@pat_demo", meta={"prospect_id": pid})
    assert n["status"] == "held" and any(f["code"] == "do_not_contact" for f in n["flags"])


def test_scheduler_ticks_are_wired():
    for spec in ("operator_ai.delivery:dispatch_due", "operator_ai.delivery:run_cadences"):
        assert spec in scheduler.TENANT_TICKS
        fn = scheduler._resolve(spec)
        assert fn is not None and fn.demo_too is True


def test_empty_tenant_ticks_do_nothing(tenant):
    assert delivery.dispatch_due(tenant) == {"due": 0, "results": []}
    assert delivery.run_cadences(tenant) == {"due": 0, "drafted": []}


# ---------------------------------------------------------------- addressing + never-invite
def test_address_parsing_and_normalising():
    assert delivery.email_from("Jane Doe <jane@example.invalid>") == "jane@example.invalid"
    assert delivery.email_from("no address here") is None
    assert queue.e164("+1 (555) 0142") == "+15550142" and queue.e164("0044 20 7946 0000") == "+442079460000"
    assert queue.e164("555 0142") is None and queue.e164("call me") is None
    assert "h:maya_demo" in queue.address_keys("@Maya_Demo") and "h:maya_demo" in queue.address_keys("maya_demo")
    assert "h:maya_demo" in queue.address_keys("https://www.instagram.com/maya_demo/")
    assert "tel:15550123" in queue.address_keys("+1-555-0123")


@pytest.mark.parametrize("blocked,rcpt", [("@maya_demo", "@maya_demo"), ("@maya_demo", "maya_demo"),
                                          ("maya_demo", "@MAYA_DEMO"), ("+15550123", "+1-555-0123"),
                                          ("Blocked.Person+x@gmail.com", "Someone <blockedperson@googlemail.com>")])
def test_never_invite_matches_handles_phones_and_emails(tenant, blocked, rcpt):
    tenant.set("blocked_attendees", [blocked])
    ch = "sms" if rcpt.startswith("+") else "email" if "@" in rcpt and "." in rcpt.split("@")[-1] else "instagram_dm"
    it = queue.add(tenant, "dm", "Quick question?", channel=ch, recipient_ref=rcpt)
    assert it["status"] == "held" and any(f["code"] == "never_invite" for f in it["flags"])


def test_never_invite_negative_case(tenant):
    tenant.set("blocked_attendees", ["@maya_demo", "+15550123"])
    it = queue.add(tenant, "dm", "Quick question?", channel="instagram_dm", recipient_ref="@owen_demo")
    assert not any(f["code"] == "never_invite" for f in it["flags"])


def test_live_email_uses_the_parsed_address(tenant, smtp):
    _live(tenant)
    it = queue.add(tenant, "email_reply", "Here's your replay link.", channel="email", recipient_ref="Jane Doe <jane@example.invalid>",
                   meta={"subject": "Re: your question", "prior_contact": True})
    assert queue.approve(tenant, it["id"], "o")["status"] == "sent"
    assert smtp[0]["To"] == "jane@example.invalid" and f"q{it['id']}" in smtp[0]["Message-ID"]


def test_telegram_nudge_goes_to_the_linked_chat(tenant, world):
    _live(tenant)
    with tenant.db() as c:
        sid = c.execute("INSERT INTO students(name,email,demo) VALUES('Zara Example (DEMO)','zara@example.invalid',1)").lastrowid
    uid = _user(tenant, "zara", "student", student_id=sid)
    with tenant.db() as c:
        c.execute("INSERT INTO channel_links(channel,ext_id,user_id,status,created) VALUES('telegram','777001',?,'linked',0)", (uid,))
    it = queue.add(tenant, "nudge", "What's one small win this week?", channel="telegram", recipient_ref="Zara Example (DEMO)",
                   meta={"student_id": sid})
    assert it["status"] == "pending"
    assert queue.approve(tenant, it["id"], "o")["status"] == "sent"
    assert '"chat_id":"777001"' in world.sent[0][2]


def test_telegram_nudge_falls_back_to_email_or_is_held(tenant, world, smtp):
    _live(tenant)
    with tenant.db() as c:
        sid = c.execute("INSERT INTO students(name,email,demo) VALUES('Luca Example (DEMO)','luca@example.invalid',1)").lastrowid
        sid2 = c.execute("INSERT INTO students(name,demo) VALUES('No Email Example (DEMO)',1)").lastrowid
    it = queue.add(tenant, "nudge", "Here's the reminder ladder checklist.", channel="telegram",
                   recipient_ref="Luca Example (DEMO)", meta={"student_id": sid})
    r = queue.approve(tenant, it["id"], "o")
    assert r["status"] == "sent" and r["delivered"].startswith("live:email:") and world.sent == []
    assert smtp[0]["To"] == "luca@example.invalid"
    held = queue.add(tenant, "nudge", "Checking in.", channel="telegram", recipient_ref="No Email Example (DEMO)",
                     meta={"student_id": sid2})
    assert held["status"] == "held" and any(f["code"] == "no_address" for f in held["flags"])


def test_contact_points_resolve_and_validate(tenant):
    lid = _lead(tenant, handle=None, email=None)
    with pytest.raises(ValueError):
        delivery.add_contact_point(tenant, "lead", lid, "email", "not an email")
    with pytest.raises(ValueError):
        delivery.add_contact_point(tenant, "lead", lid, "sms", "555")
    cp = delivery.add_contact_point(tenant, "lead", lid, "sms", "+44 20 7946 0000", opted_in_=True)
    assert cp["address"] == "+442079460000"
    delivery.add_contact_point(tenant, "lead", lid, "email", "Robin <robin.cp@example.invalid>", verified=True)
    assert delivery.resolve_address(tenant, "email", "Robin Test (DEMO)", {"lead_id": lid}) == "robin.cp@example.invalid"
    assert delivery.resolve_address(tenant, "sms", None, {"lead_id": lid}) == "+442079460000"
    assert delivery.opted_in(tenant, "sms", "", {"lead_id": lid})  # opted in through the contact point
    assert delivery.resolve_address(tenant, "email", "nobody", {}) is None


def test_sms_opt_in_matches_any_formatting(tenant):
    with tenant.db() as c:
        c.execute("INSERT INTO channel_links(channel,ext_id,opted_in,status,created) VALUES('sms','+1 555 0142',1,'opted_in',0)")
    ok = queue.add(tenant, "reminder", "See you at 3pm", channel="sms", recipient_ref="+1-555-0142")
    assert not any(f["code"] == "not_opted_in" for f in ok["flags"])
    with tenant.db() as c:
        c.execute("INSERT INTO channel_links(channel,ext_id,opted_in,status,created) VALUES('sms','+15550142',0,'opted_out',1)")
    stopped = queue.add(tenant, "reminder", "See you at 3pm", channel="sms", recipient_ref="+1 (555) 0142")
    assert any(f["code"] == "not_opted_in" for f in stopped["flags"])


# ---------------------------------------------------------------- email compliance
def test_prospect_email_footer_and_guard_rules(tenant):
    pid = _prospect(tenant)
    it = queue.add(tenant, "prospect_email", "Quick idea for your clinic's bookings. Worth a look?", channel="email",
                   recipient_ref="pat.example@example.invalid", meta={"prospect_facing": True, "lead_tier": "cold",
                                                                     "prospect_id": pid, "subject": "An idea for your clinic"})
    assert "{unsubscribe_link}" in it["body"] and "Unsubscribe" in it["body"]
    assert it["status"] == "held" and any(f["code"] == "missing_postal_address" for f in it["flags"])
    _address(tenant)
    it = queue.edit(tenant, it["id"], "Quick idea for your clinic's bookings. Worth a look?", "o")
    assert it["status"] == "pending" and "1 Example Street, Testtown" in it["body"]
    with tenant.db() as c:  # someone strips the unsubscribe line behind the API
        c.execute("UPDATE queue SET body=? WHERE id=?", ("Quick idea. 1 Example Street, Testtown", it["id"]))
    with pytest.raises(queue.QueueError):
        queue.approve(tenant, it["id"], "o")
    assert any(f["code"] == "missing_unsubscribe" for f in queue.get(tenant, it["id"])["flags"])


def test_guard_rules_only_for_prospect_email(tenant):
    _address(tenant)
    assert delivery.guard_rules("hello", "outreach", {"_channel": "instagram_dm"}, tenant) == []
    assert delivery.guard_rules("hello", "plain", {"_channel": "email"}, tenant) == []
    codes = {f["code"] for f in delivery.guard_rules("hello", "outreach", {"_channel": "email"}, tenant)}
    assert codes == {"missing_unsubscribe", "missing_postal_address"}
    ok = "hello\n-- \nPeak · 1 Example Street, Testtown\nUnsubscribe: {unsubscribe_link}"
    assert delivery.guard_rules(ok, "outreach", {"_channel": "email"}, tenant) == []


def test_live_prospect_email_headers_link_and_unsubscribe(client, tenant, smtp, monkeypatch):
    monkeypatch.setenv("OPERATOR_PUBLIC_URL", "https://app.example.invalid")
    _live(tenant)
    _address(tenant)
    pid = _prospect(tenant)
    meta = {"prospect_facing": True, "lead_tier": "cold", "prospect_id": pid, "subject": "An idea for your clinic"}
    it = queue.add(tenant, "prospect_email", "Quick idea for your clinic's bookings. Worth a look?", channel="email",
                   recipient_ref="Pat <pat.example@example.invalid>", meta=meta)
    later = queue.add(tenant, "prospect_email", "One more idea. Want it?", channel="email",
                      recipient_ref="pat.example@example.invalid", meta={**meta, "send_after": time.time() + DAY})
    queue.approve(tenant, later["id"], "o")
    sent = queue.approve(tenant, it["id"], "o")
    assert sent["status"] == "sent"
    msg = smtp[0]
    url = msg["List-Unsubscribe"].strip("<>")
    assert url.startswith("https://app.example.invalid/u/") and msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert url in msg.get_content() and "{unsubscribe_link}" not in msg.get_content()
    path = url.replace("https://app.example.invalid", "")
    page = client.get(path)
    assert page.status_code == 200 and "Unsubscribe" in page.text and "<form" in page.text
    with tenant.db() as c:  # GET never unsubscribes (link scanners)
        assert c.execute("SELECT do_not_contact FROM prospects WHERE id=?", (pid,)).fetchone()[0] == 0
    r = client.post(path, content=b"List-Unsubscribe=One-Click", headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 200 and "unsubscribed" in r.text
    with tenant.db() as c:
        row = c.execute("SELECT do_not_contact, stage FROM prospects WHERE id=?", (pid,)).fetchone()
    assert row["do_not_contact"] == 1 and row["stage"] == "dnc"
    assert {"party_type": "prospect", "party_id": pid} in [{k: p.get(k) for k in ("party_type", "party_id")}
                                                           for p in _events(tenant, "dnc_set")]
    assert _events(tenant, "unsubscribed")
    assert queue.get(tenant, later["id"])["status"] == "cancelled"  # the scheduled one never goes
    again = queue.add(tenant, "prospect_email", "Hello again", channel="email", recipient_ref="pat.example@example.invalid",
                      meta={**meta, "prospect_id": None})
    assert again["status"] == "held" and any(f["code"] == "unsubscribed" for f in again["flags"])
    assert client.post(path).status_code == 200  # idempotent
    assert len(_events(tenant, "unsubscribed")) == 1


def test_unsubscribe_bad_tokens(client):
    for tok in ("x", "owner.nope", "missing-tenant.abc", "a" * 300):
        assert client.get(f"/u/{tok}").status_code == 404
        assert client.post(f"/u/{tok}").status_code == 404


def test_live_prospect_email_needs_a_public_address(tenant, smtp, monkeypatch):
    monkeypatch.delenv("OPERATOR_PUBLIC_URL", raising=False)
    _live(tenant)
    _address(tenant)
    it = queue.add(tenant, "prospect_email", "Quick idea. Worth a look?", channel="email", recipient_ref="pat.example@example.invalid",
                   meta={"prospect_facing": True, "lead_tier": "cold"})
    with pytest.raises(queue.QueueError) as ei:
        queue.approve(tenant, it["id"], "o")
    assert ei.value.status == 409 and "OPERATOR_PUBLIC_URL" in str(ei.value) and smtp == []
    assert queue.get(tenant, it["id"])["status"] == "pending"


def test_manual_prospect_email_copy_text_has_the_link(tenant, monkeypatch):
    monkeypatch.setenv("OPERATOR_PUBLIC_URL", "https://operator.example.invalid")
    _address(tenant)
    it = queue.add(tenant, "prospect_email", "Quick idea. Worth a look?", channel="email", recipient_ref="pat.example@example.invalid",
                   meta={"prospect_facing": True, "lead_tier": "cold"})
    r = queue.approve(tenant, it["id"], "o")
    assert r["status"] == "awaiting_manual_send" and "{unsubscribe_link}" not in r["send_text"]
    assert "https://operator.example.invalid/u/" in r["send_text"]


def test_manual_prospect_email_never_carries_a_localhost_link(tenant, monkeypatch):
    # review fix: the copy-and-send path used to hand a person an unsubscribe link pointing at 127.0.0.1
    monkeypatch.delenv("OPERATOR_PUBLIC_URL", raising=False)
    _address(tenant)
    it = queue.add(tenant, "prospect_email", "Quick idea. Worth a look?", channel="email", recipient_ref="pat.example@example.invalid",
                   meta={"prospect_facing": True, "lead_tier": "cold"})
    with pytest.raises(queue.QueueError) as ei:
        queue.approve(tenant, it["id"], "o")
    assert ei.value.status == 409 and "OPERATOR_PUBLIC_URL" in str(ei.value)
    assert queue.get(tenant, it["id"])["status"] == "pending"
    with tenant.db() as c:
        assert c.execute("SELECT COUNT(*) FROM unsubscribe_tokens").fetchone()[0] == 0


def test_mailbox_daily_cap_and_warmup(tenant, smtp):
    _live(tenant)
    delivery.put_settings(tenant, {"email_daily_cap": 1, "warmup_enabled": False}, "o")
    a = queue.add(tenant, "email_reply", "One", channel="email", recipient_ref="a@example.invalid", meta={"prior_contact": True})
    b = queue.add(tenant, "email_reply", "Two", channel="email", recipient_ref="b@example.invalid", meta={"prior_contact": True})
    assert queue.approve(tenant, a["id"], "o")["status"] == "sent"
    r = queue.approve(tenant, b["id"], "o")
    assert r["status"] == "scheduled" and "daily limit" in r["status_reason"] and len(smtp) == 1
    assert r["send_after"] == pytest.approx(delivery.next_morning())
    delivery.put_settings(tenant, {"email_daily_cap": 40, "warmup_enabled": True, "warmup_first_day": 10, "warmup_step": 5}, "o")
    tenant.set("delivery_settings", {**tenant.get("delivery_settings"), "warmup_started": time.time() - 3 * DAY})
    assert delivery.email_cap_today(tenant) == 25


# ---------------------------------------------------------------- reject reasons + learning signal
def test_reject_reason_codes_hooks_and_feedback(tenant):
    seen = []
    queue.on_reject("test_rej")(lambda t, item, actor: seen.append(item["reject_code"]))
    it = queue.add(tenant, "test_rej", "A long draft")
    with pytest.raises(queue.QueueError) as ei:
        queue.reject(tenant, it["id"], "o", reason_code="nonsense")
    assert ei.value.status == 422
    r = queue.reject(tenant, it["id"], "o", reason="Too chatty", reason_code="too_long")
    assert r["status"] == "rejected" and r["reject_code"] == "too_long" and seen == ["too_long"]
    fb = queue.feedback(tenant)[0]
    assert fb["reason_code"] == "too_long" and fb["reason"] == "Too chatty" and fb["queue_id"] == it["id"]
    plain = queue.reject(tenant, queue.add(tenant, "note", "x")["id"], "o")
    assert plain["reject_code"] == "other"  # back-compat: no code given


def test_approve_with_edits_records_the_diff(tenant):
    it = queue.add(tenant, "dm", "Hey there, I wanted to reach out about your clinic.", channel="instagram_dm",
                   recipient_ref="@x_demo", meta={"template_key": "opener_question"})
    queue.edit(tenant, it["id"], "Quick question about your clinic?", "o")
    queue.edit(tenant, it["id"], "Quick question about your clinic's bookings?", "o")
    assert queue.get(tenant, it["id"])["original_body"] == "Hey there, I wanted to reach out about your clinic."
    queue.approve(tenant, it["id"], "o")
    fb = queue.feedback(tenant)[0]
    assert fb["reason_code"] == "edited" and fb["template_key"] == "opener_question"
    assert "-Hey there" in fb["edit_diff"] and "+Quick question about your clinic's bookings?" in fb["edit_diff"]


def test_reject_api_codes_and_422(client):
    demo_login(client, "owner")
    t = tenancy.get("demo")
    it = queue.add(t, "note", "x")
    assert client.post(f"/t/demo/api/queue/{it['id']}/reject", json={"reason_code": "made_up"}).status_code == 422
    r = client.post(f"/t/demo/api/queue/{it['id']}/reject", json={"reason_code": "off_brand", "reason": "Not our voice"})
    assert r.status_code == 200 and r.json()["reject_code"] == "off_brand"


# ---------------------------------------------------------------- action gate: partial failure
def test_action_partial_failure_is_recorded_and_rolls_back(demo_tenant, monkeypatch):
    t = demo_tenant
    ad = MockAdsAdapter(t)
    before = {i: (ad.get(i)["status"], ad.get(i)["daily_budget"]) for i in ("ad_demo_2_1", "ad_demo_1_1")}
    assert before["ad_demo_2_1"][0] == "ACTIVE"
    p = actions.propose(t, "ad_change", {"account": "act_DEMO_practice", "ops": [{"op": "pause", "id": "ad_demo_2_1"},
                                                                                  {"op": "set_budget", "id": "ad_demo_1_1",
                                                                                   "daily_budget": 5}]}, "owner")
    orig = MockAdsAdapter.update
    n = {"calls": 0}

    def flaky(self, obj_id, **fields):
        n["calls"] += 1
        if n["calls"] == 2:
            raise TimeoutError("Graph timed out access_token=EAAsecretsecretsecret")
        return orig(self, obj_id, **fields)

    monkeypatch.setattr(MockAdsAdapter, "update", flaky)
    with pytest.raises(queue.QueueError) as ei:
        queue.approve(t, p["queue_id"], "owner(owner)")
    assert ei.value.status == 502 and "EAAsecret" not in str(ei.value) and "Roll it back" in str(ei.value)
    item = queue.get(t, p["queue_id"])
    assert item["status"] == "send_failed" and "part of this change" in item["status_reason"]
    a = actions._row(t, p["action_id"])
    assert a["status"] == "failed_partial" and a["prior_state"][0]["id"] == "ad_demo_2_1" and a["result"]["applied"] == 1
    assert "(1 of 2 steps)" in item["status_reason"]
    assert ad.get("ad_demo_2_1")["status"] == "PAUSED" and ad.get("ad_demo_1_1")["daily_budget"] == before["ad_demo_1_1"][1]
    with pytest.raises(queue.QueueError):
        queue.approve(t, p["queue_id"], "owner(owner)")  # not re-runnable
    with pytest.raises(queue.QueueError):
        queue.send_now(t, p["queue_id"], "owner(owner)")
    monkeypatch.setattr(MockAdsAdapter, "update", orig)
    actions.rollback(t, p["action_id"], "owner")
    assert {i: (ad.get(i)["status"], ad.get(i)["daily_budget"]) for i in before} == before


def test_action_runs_once_even_if_executed_twice(demo_tenant):
    t = demo_tenant
    p = actions.propose(t, "ad_change", {"account": "act_DEMO_practice", "ops": [{"op": "pause", "id": "ad_demo_2_1"}]}, "owner")
    queue.approve(t, p["queue_id"], "owner(owner)")
    with pytest.raises(actions.ActionError):
        actions.execute(t, p["action_id"], "owner")


# ---------------------------------------------------------------- event-driven booking
def test_booking_flow_no_duplicate_events_and_ladder(tenant):
    lid = _lead(tenant)
    sales.process_lead(tenant, lid)
    rep = sales.record_reply(tenant, lid, "Yes, let's talk")
    assert rep["label"] == "Classified by rules (AI off)"
    queue.approve(tenant, rep["booking"]["booking_dm"], "o")
    inv = sales.pick_slot(tenant, lid, index=0)
    done = queue.approve(tenant, inv["invite"], "o")
    with tenant.db() as c:
        evs = db.rows(c.execute("SELECT * FROM calendar_events WHERE ext_id=?", (f"local-q{inv['invite']}",)))
    assert len(evs) == 1 and evs[0]["party_type"] == "lead" and evs[0]["party_id"] == lid
    sales._invite_approved(tenant, queue.get(tenant, inv["invite"], scrub=False), "o")  # a hook re-run is idempotent
    with tenant.db() as c:
        assert c.execute("SELECT COUNT(*) FROM calendar_events WHERE ext_id=?", (f"local-q{inv['invite']}",)).fetchone()[0] == 1
    assert done["status"] == "awaiting_manual_send"
    assert _events(tenant, "booking_made")[0]["party_id"] == lid


def test_reminders_for_a_cancelled_booking_never_go(tenant):
    lid = _lead(tenant)
    sales.process_lead(tenant, lid)
    rep = sales.record_reply(tenant, lid, "Yes please")
    queue.approve(tenant, rep["booking"]["booking_dm"], "o")
    inv = sales.pick_slot(tenant, lid, index=2)
    queue.approve(tenant, inv["invite"], "o")
    ladder = [i for i in queue.list_items(tenant, kind="reminder") if i["meta"].get("lead_id") == lid]
    assert ladder and all(i["meta"]["cancel_if"] == ["booking_cancelled"] for i in ladder)
    for i in ladder:
        queue.approve(tenant, i["id"], "o")
    scheduled = [i for i in ladder if queue.get(tenant, i["id"])["status"] == "scheduled"]
    assert scheduled  # the 2h and 10 min reminders wait for their time
    ev = ladder[0]["meta"]["calendar_event_id"]
    delivery.cancel_booking(tenant, ev, "o")
    delivery.dispatch_due(tenant, now=inv["slot"]["start"])
    for i in ladder:
        it = queue.get(tenant, i["id"])
        assert it["status"] == "cancelled" and "cancelled" in it["status_reason"]
    assert _events(tenant, "booking_cancelled")[0]["event_id"] == ev


def test_cancel_if_booking_cancelled_by_predicate(tenant):
    """Even without the event (the calendar row changed elsewhere), the dispatcher cancels at send time."""
    ev = cal.add_event(tenant, "Call", time.time() + DAY, time.time() + DAY + 2700, [])
    it = queue.add(tenant, "reminder", "See you soon.", channel="instagram_dm", recipient_ref="@test_demo",
                   meta={"send_after": time.time() + 3600, "cancel_if": ["booking_cancelled"], "calendar_event_id": ev})
    queue.approve(tenant, it["id"], "o")
    with tenant.db() as c:
        c.execute("UPDATE calendar_events SET status='cancelled' WHERE id=?", (ev,))
    delivery.dispatch_due(tenant, now=time.time() + 3601)
    assert queue.get(tenant, it["id"])["status"] == "cancelled"


def test_no_free_slots_is_a_clear_409_never_a_crash(client, monkeypatch):
    demo_login(client, "owner")
    t = tenancy.get("demo")
    monkeypatch.setattr(sales, "_slots", lambda tenant: [])
    with t.db() as c:
        lid = c.execute("SELECT id FROM leads WHERE batch IS NULL ORDER BY id LIMIT 1").fetchone()[0]
    r = client.post(f"/t/demo/api/leads/{lid}/offer-times")
    assert r.status_code == 409 and "No free" in r.json()["detail"]
    r = client.post(f"/t/demo/api/leads/{lid}/reply", json={"text": "Yes, keen", "classification": "positive"})
    assert r.status_code == 200 and r.json()["booking"]["no_slots"] is True
    assert client.post(f"/t/demo/api/leads/{lid}/pick-slot", json={"index": 0}).status_code == 409


def test_chain_no_free_slots(tenant, monkeypatch):
    raw = json.dumps({"entry": [{"changes": [{"field": "leadgen", "value": {
        "leadgen_id": "NS1", "practice_fields": {"full_name": "Practice (DEMO)", "instagram": "practice_demo",
                                                 "monthly_revenue": "30k-100k", "can_invest": "yes", "timeline": "now"}}}]}]}).encode()
    from operator_ai import jobs
    from operator_ai.live import meta

    chain.receive_webhook(tenant, raw, meta.sign(meta.app_secret(tenant), raw))
    jobs.drain(tenant, timeout=30)
    fu = queue.list_items(tenant, kind="followup")[0]
    assert fu["recipient_ref"] == "@practice_demo" and fu["channel"] == "instagram_dm"  # never a 'lead-{id}' placeholder
    monkeypatch.setattr(sales, "_slots", lambda t: [])
    out = chain.record_reply(tenant, 1, "Yes please")
    assert out["no_slots"] and out["booking_dm"] is None and out["run_id"]  # traced like every agent action
    with pytest.raises(sales.NoSlots):
        chain.offer_times(tenant, 1)


def test_expired_booking_times_are_redrafted_not_sent(tenant):
    lid = _lead(tenant)
    rep = sales.record_reply(tenant, lid, "Sure, happy to chat")
    bid = rep["booking"]["booking_dm"]
    item = queue.get(tenant, bid, scrub=False)
    with tenant.db() as c:
        c.execute("UPDATE queue SET meta=? WHERE id=?", (db.jdump({**item["meta"], "slots_valid_until": time.time() - 60}), bid))
    with pytest.raises(queue.QueueError) as ei:
        queue.approve(tenant, bid, "o")
    assert ei.value.status == 409 and "expired" in str(ei.value)
    fresh = queue.get(tenant, bid)
    assert fresh["status"] == "pending" and fresh["meta"]["slots_valid_until"] > time.time()
    assert queue.approve(tenant, bid, "o")["status"] == "awaiting_manual_send"


def test_pick_slot_validation(tenant):
    lid = _lead(tenant)
    with pytest.raises(sales.BookingError) as ei:
        sales.pick_slot(tenant, lid, index=0)
    assert ei.value.status == 409  # nothing offered yet
    rep = sales.record_reply(tenant, lid, "yes")
    queue.approve(tenant, rep["booking"]["booking_dm"], "o")
    with pytest.raises(sales.BookingError) as ei:
        sales.pick_slot(tenant, lid, index=7)
    assert ei.value.status == 422
    s = rep["booking"]["slots"][0]
    cal.add_event(tenant, "Busy", s["start"], s["end"], [])
    with pytest.raises(sales.BookingError) as ei:
        sales.pick_slot(tenant, lid, start=s["start"])
    assert ei.value.status == 409 and "taken" in str(ei.value)


def test_reply_classification_rules():
    assert sales.classify_reply("Please stop messaging me") == "stop"
    assert sales.classify_reply("No thanks, not interested") == "negative"
    assert sales.classify_reply("Yes, sounds good") == "positive"
    assert sales.classify_reply("What does it involve?") == "question"
    assert sales.classify_reply("hmm") == "neutral"


def test_stop_reply_sets_dnc_and_suppresses(tenant):
    lid = _lead(tenant)
    sales.record_reply(tenant, lid, "Please stop messaging me")
    with tenant.db() as c:
        assert c.execute("SELECT do_not_contact FROM leads WHERE id=?", (lid,)).fetchone()[0] == 1
    it = queue.add(tenant, "followup", "Hi again", channel="instagram_dm", recipient_ref="@test_demo", meta={"lead_id": lid})
    codes = {f["code"] for f in it["flags"]}
    assert it["status"] == "held" and {"do_not_contact", "unsubscribed"} <= codes


# ---------------------------------------------------------------- cadences
def test_cadence_starts_only_on_a_confirmed_send_and_steps(tenant):
    uid = _user(tenant, "op1", "student")
    pid = _prospect(tenant, owner_user_id=uid)
    opener = queue.add(tenant, "prospect_dm", "Quick question: where do new patients come from right now?", channel="instagram_dm",
                       recipient_ref="@pat_demo", meta={"prospect_facing": True, "lead_tier": "cold", "prospect_id": pid,
                                                        "owner_user_id": uid})
    queue.approve(tenant, opener["id"], "op1(student)", role="student", user_id=uid)
    with tenant.db() as c:
        assert c.execute("SELECT COUNT(*) FROM cadences").fetchone()[0] == 0  # approved is not sent
    queue.mark_sent(tenant, opener["id"], "op1(student)", role="student", user_id=uid)
    cads = delivery.list_cadences(tenant)
    assert len(cads) == 1 and cads[0]["status"] == "active" and cads[0]["owner_user_id"] == uid
    sent_at = queue.get(tenant, opener["id"])["sent_at"]
    assert cads[0]["due_at"] == pytest.approx(sent_at + 2 * DAY)
    assert delivery.run_cadences(tenant, now=sent_at + DAY)["due"] == 0  # not due yet
    out = delivery.run_cadences(tenant, now=sent_at + 2 * DAY + 5)
    assert len(out["drafted"]) == 1 and out["run_id"]
    fu = queue.get(tenant, out["drafted"][0])
    assert fu["kind"] == "followup" and fu["meta"]["cadence_id"] == cads[0]["id"] and fu["meta"]["owner_user_id"] == uid
    assert fu["meta"]["ai_mode"] == "rules" and fu["meta"]["prospect_facing"] and fu["status"] == "pending"
    assert delivery.run_cadences(tenant, now=sent_at + 3 * DAY)["due"] == 0  # waits for this step to go out
    queue.approve(tenant, fu["id"], "op1(student)", role="student", user_id=uid)
    queue.mark_sent(tenant, fu["id"], "op1(student)", role="student", user_id=uid)
    cad = delivery.list_cadences(tenant)[0]
    assert cad["step"] == 1 and cad["status"] == "active"
    events.emit(tenant, "reply_received", {"party_type": "prospect", "party_id": pid, "classification": "positive"})
    assert delivery.list_cadences(tenant)[0]["status"] == "replied"


def test_cadence_stops_on_inbound_dnc_cap_and_reject(tenant):
    def make(handle):
        lid = _lead(tenant, handle=handle, email=None)
        first = queue.add(tenant, "setter_reply", "Thanks for the reply. Would a quick call help?", channel="instagram_dm",
                          recipient_ref=handle, meta={"prospect_facing": True, "lead_tier": "warm", "lead_id": lid})
        cid = delivery.register_cadence(tenant, ("lead", lid), first["id"], "instagram_dm", handle, template="lead",
                                        context={"followups": ["Value one. Want it?", "Value two. Want it?"]})
        queue.approve(tenant, first["id"], "o")
        queue.mark_sent(tenant, first["id"], "o")
        return lid, cid

    l1, c1 = make("@one_demo")
    l2, c2 = make("@two_demo")
    l3, c3 = make("@three_demo")
    l4, c4 = make("@four_demo")
    with tenant.db() as c:  # an inbound DM from lead 1 after our message
        c.execute("INSERT INTO channel_messages(channel,direction,ext_id,body,meta,created) VALUES('instagram_dm','in','@one_demo','hi','{}',?)",
                  (db.now() + 1,))
        c.execute("UPDATE leads SET do_not_contact=1 WHERE id=?", (l2,))
    out = delivery.run_cadences(tenant, now=time.time() + 2 * DAY + 10)
    st = {r["id"]: r["status"] for r in delivery.list_cadences(tenant)}
    assert st[c1] == "replied" and st[c2] == "stopped" and st[c3] == "waiting_send" and st[c4] == "waiting_send"
    assert len(out["drafted"]) == 2
    # a rejected step stops its sequence
    fu3 = next(i for i in queue.list_items(tenant, kind="followup") if i["meta"].get("cadence_id") == c3)
    queue.reject(tenant, fu3["id"], "o", reason_code="timing")
    assert {r["id"]: r["status"] for r in delivery.list_cadences(tenant)}[c3] == "stopped"
    # the cap counts confirmed sends today: with a cap of 1 and one send already today, the next step waits
    fu4 = next(i for i in queue.list_items(tenant, kind="followup") if i["meta"].get("cadence_id") == c4)
    queue.approve(tenant, fu4["id"], "o")
    queue.mark_sent(tenant, fu4["id"], "o")
    delivery.put_settings(tenant, {"daily_touch_cap": 1}, "o")
    with tenant.db() as c:
        c.execute("UPDATE cadences SET due_at=? WHERE id=?", (time.time() - 5, c4))
    out = delivery.run_cadences(tenant)
    assert out["deferred"] == [c4] and out["drafted"] == []
    cad4 = next(r for r in delivery.list_cadences(tenant) if r["id"] == c4)
    assert cad4["status"] == "active" and cad4["due_at"] == pytest.approx(delivery.next_morning())


def test_lead_cadence_finishes_into_nurture(tenant):
    lid = _lead(tenant, email=None)
    r = sales.process_lead(tenant, lid)
    reply = r["queue_ids"][0]
    queue.approve(tenant, reply, "o")
    queue.mark_sent(tenant, reply, "o")
    now = time.time()
    for step in range(3):
        now += 8 * DAY
        out = delivery.run_cadences(tenant, now=now)
        assert len(out["drafted"]) == 1
        queue.approve(tenant, out["drafted"][0], "o")
        queue.mark_sent(tenant, out["drafted"][0], "o")
    cad = [c for c in delivery.list_cadences(tenant) if c["party_id"] == lid][0]
    assert cad["status"] == "done"
    with tenant.db() as c:
        assert c.execute("SELECT stage FROM leads WHERE id=?", (lid,)).fetchone()[0] == "nurture"
    assert _events(tenant, "cadence_done")


# ---------------------------------------------------------------- API: RBAC, isolation, malformed input
def test_delivery_api_rbac_and_isolation(client):
    demo_login(client, "student")
    me = client.get("/t/demo/api/me").json()["user"]["id"]
    t = tenancy.get("demo")
    mine = queue.add(t, "prospect_dm", "Quick question about your clinic?", channel="instagram_dm",
                     meta={"prospect_facing": True, "owner_user_id": me})
    other = queue.add(t, "prospect_dm", "Quick question?", channel="instagram_dm", meta={"prospect_facing": True, "owner_user_id": 999})
    queue.approve(t, other["id"], "owner(owner)")
    assert client.post(f"/t/demo/api/queue/{other['id']}/sent").status_code == 404
    assert client.post(f"/t/demo/api/queue/{other['id']}/cancel").status_code == 404
    assert client.post(f"/t/demo/api/queue/{mine['id']}/approve").json()["status"] == "awaiting_manual_send"
    assert client.post(f"/t/demo/api/queue/{mine['id']}/sent", json={"permalink": "javascript:alert(1)"}).status_code == 422
    assert client.post(f"/t/demo/api/queue/{mine['id']}/sent", json={"permalink": "https://example.invalid/m/1"}).json()["status"] == "sent"
    assert client.get("/t/demo/api/delivery/settings").status_code == 403
    assert client.put("/t/demo/api/delivery/settings", json={"email_daily_cap": 5}).status_code == 403
    assert client.post("/t/demo/api/delivery/run-now").status_code == 403
    ov = client.get("/t/demo/api/delivery/overview").json()
    assert "settings" not in ov and "tone" in ov["reject_codes"]
    assert all(c["owner_user_id"] == me for c in client.get("/t/demo/api/delivery/cadences").json()["items"])
    assert client.post("/t/demo/api/leads/1/reply", json={"text": "yes"}).status_code == 403  # students have no sales tab
    pid = _prospect(t, owner_user_id=999)
    assert client.get("/t/demo/api/delivery/contacts", params={"party_type": "prospect", "party_id": pid}).status_code == 404
    assert client.post("/t/demo/api/delivery/contacts", json={"party_type": "prospect", "party_id": pid, "channel": "email",
                                                              "address": "x@example.invalid"}).status_code == 404
    mine_p = _prospect(t, owner_user_id=me)
    r = client.post("/t/demo/api/delivery/contacts", json={"party_type": "prospect", "party_id": mine_p, "channel": "email",
                                                           "address": "Pat <pat2@example.invalid>"})
    assert r.status_code == 200 and r.json()["address"] == "pat2@example.invalid"
    assert client.post("/t/demo/api/delivery/contacts", json={"party_type": "prospect", "party_id": mine_p, "channel": "sms",
                                                               "address": "123"}).status_code == 422


def test_setter_cannot_touch_owner_only_items(client):
    demo_login(client, "setter")
    t = tenancy.get("demo")
    inv = queue.add(t, "calendar_invite", "Call (DEMO)", channel="calendar", recipient_ref="x@example.invalid")
    queue.approve(t, inv["id"], "owner(owner)")
    assert client.post(f"/t/demo/api/queue/{inv['id']}/sent").status_code == 404
    assert client.post(f"/t/demo/api/queue/{inv['id']}/send-now").status_code == 404
    assert client.post(f"/t/demo/api/queue/{inv['id']}/approve").status_code in (403, 409)


def test_delivery_api_malformed_input_is_422(client):
    demo_login(client, "owner")
    for url in ("/t/demo/api/queue/1/sent", "/t/demo/api/queue/1/cancel", "/t/demo/api/leads/1/reply",
                "/t/demo/api/leads/1/pick-slot", "/t/demo/api/delivery/contacts"):
        assert client.post(url, content=b"{broken", headers={"content-type": "application/json"}).status_code == 422, url
    assert client.post("/t/demo/api/queue/abc/cancel").status_code == 422
    assert client.put("/t/demo/api/delivery/settings", json={"email_daily_cap": "lots"}).status_code == 422
    assert client.put("/t/demo/api/delivery/settings", json={"cadence_days": [0]}).status_code == 422
    assert client.put("/t/demo/api/delivery/settings", json={"email_daily_cap": -1}).status_code == 422
    assert client.post("/t/demo/api/leads/1/pick-slot", json={}).status_code == 422
    assert client.post("/t/demo/api/leads/999999/reply", json={"text": "yes"}).status_code == 404
    assert client.post("/t/demo/api/leads/1/reply", json={"text": "yes", "classification": "maybe"}).status_code == 422
    assert client.get("/t/demo/api/delivery/contacts", params={"party_type": "alien", "party_id": 1}).status_code == 422
    assert client.post("/t/demo/api/bookings/999999/cancel").status_code == 404
    assert client.post("/t/demo/api/queue/999999/sent").status_code == 404
    assert client.post("/t/demo/api/delivery/cadences/999999/stop").status_code == 404


def test_owner_settings_roundtrip(client):
    demo_login(client, "owner")
    r = client.put("/t/demo/api/delivery/settings", json={"email_daily_cap": 30, "cadence_days": [3, 1, 3, 10]})
    assert r.status_code == 200 and r.json()["settings"]["cadence_days"] == [1, 3, 10]
    ov = client.get("/t/demo/api/delivery/overview").json()
    assert ov["settings"]["email_daily_cap"] == 30 and ov["public_url_set"] is False
    assert client.post("/t/demo/api/delivery/run-now").status_code == 200


# ---------------------------------------------------------------- demo vs real, AI off
def test_demo_seed_is_fictional_and_demo_only(tenant, demo_tenant):
    items = [i for i in queue.list_items(demo_tenant, tag="seed") if i["meta"].get("lead_id")]
    statuses = {i["status"] for i in items}
    assert {"scheduled", "awaiting_manual_send", "sent", "cancelled"} <= statuses
    assert all("(DEMO)" in i["title"] and i["demo"] == 1 for i in items)
    assert all(i["recipient_ref"].endswith("@example.invalid") or i["recipient_ref"].endswith("_demo") for i in items)
    assert delivery.list_cadences(demo_tenant) and delivery.suppression_list(demo_tenant)
    delivery.seed_demo(tenant)  # refused on the real workspace
    with tenant.db() as c:
        for table in ("cadences", "contact_points", "suppressions", "unsubscribe_tokens", "draft_feedback"):
            assert c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table


def test_real_workspace_works_with_no_data(owner):
    assert owner.get("/t/owner/api/delivery/cadences").json() == {"items": []}
    ov = owner.get("/t/owner/api/delivery/overview").json()
    assert ov["emails_sent_today"] == 0 and ov["postal_address"] == "" and ov["suppressed"] == 0
    assert owner.get("/t/owner/api/queue", params={"status": "outbox"}).json()["items"] == []
    assert owner.post("/t/owner/api/delivery/run-now").json()["dispatch"]["due"] == 0


def test_everything_runs_with_ai_off(tenant):
    from operator_ai import llm

    assert llm.unavailable_reason(tenant)  # no key, no budget: AI is off in tests
    lid = _lead(tenant)
    r = sales.process_lead(tenant, lid)
    assert r["queue_ids"] and r["cadence_id"]
    rep = sales.record_reply(tenant, lid, "Yes please")
    assert rep["mode"] == "rules" and rep["booking"]["booking_dm"]


# ---------------------------------------------------------------- review fixes (hostile review, 2026-10-27)
def _chain_lead(t, fields: dict, leadgen_id: str = "lg-review") -> int:
    with t.db() as c:
        return c.execute("INSERT INTO chain_leads(leadgen_id,data,tier,score,citations,stage,created) VALUES(?,?,?,?,?,?,?)",
                         (leadgen_id, db.jdump({"fields": fields}), "warm", 50, "[]", "qualified", db.now())).lastrowid


def test_staff_never_see_or_decide_a_students_own_followups(client):
    # a cadence follow-up for a student's own prospect is kind 'followup', which setters/closers decide by role:
    # it must stay the student's (and the owner's), not leak to every setter
    demo_login(client, "setter")
    t = tenancy.get("demo")
    with t.db() as c:
        student = c.execute("SELECT id FROM users WHERE role='student' ORDER BY id LIMIT 1").fetchone()[0]
    theirs = queue.add(t, "followup", "Here's the checklist I mentioned.", channel="instagram_dm", recipient_ref="@pat_demo",
                       meta={"prospect_facing": True, "lead_tier": "cold", "prior_contact": True, "owner_user_id": student})
    business = queue.add(t, "followup", "Here's the checklist I mentioned.", channel="instagram_dm", recipient_ref="@lee_demo",
                         meta={"prospect_facing": True, "lead_tier": "cold", "prior_contact": True})
    ids = [i["id"] for i in client.get("/t/demo/api/queue").json()["items"]]
    assert theirs["id"] not in ids and business["id"] in ids  # role access to business follow-ups is unchanged
    assert client.get(f"/t/demo/api/queue/{theirs['id']}").status_code == 404
    for verb in ("approve", "reject", "sent", "cancel", "send-now"):
        assert client.post(f"/t/demo/api/queue/{theirs['id']}/{verb}").status_code == 404, verb
    assert client.put(f"/t/demo/api/queue/{theirs['id']}", json={"body": "edited by someone else"}).status_code == 404
    assert queue.get(t, theirs["id"])["status"] == "pending"
    # the student still decides their own; the owner sees everything
    assert queue.approve(t, theirs["id"], "student(student)", role="student", user_id=student)["status"] == "awaiting_manual_send"
    assert queue.visible("owner", queue.get(t, theirs["id"]), None)


def test_queue_counts_cover_only_what_the_viewer_may_see(client):
    demo_login(client, "student")
    me = client.get("/t/demo/api/me").json()["user"]["id"]
    t = tenancy.get("demo")
    queue.add(t, "prospect_dm", "Quick question about your clinic?", channel="instagram_dm",
              meta={"prospect_facing": True, "lead_tier": "cold", "owner_user_id": me})
    r = client.get("/t/demo/api/queue").json()
    assert sum(r["counts"].values()) == len(r["items"]) >= 1  # no business-wide totals for a student
    assert sum(queue.counts(t).values()) > sum(r["counts"].values())  # the owner's view is the whole workspace


def test_only_the_owner_can_record_sms_opt_in(client):
    demo_login(client, "student")
    me = client.get("/t/demo/api/me").json()["user"]["id"]
    t = tenancy.get("demo")
    pid = _prospect(t, owner_user_id=me)
    body = {"party_type": "prospect", "party_id": pid, "channel": "sms", "address": "+1 555 0142 000", "opted_in": True}
    assert client.post("/t/demo/api/delivery/contacts", json=body).status_code == 403
    sms = queue.add(t, "prospect_dm", "Quick question?", channel="sms", recipient_ref="+15550142000",
                    meta={"prospect_facing": True, "lead_tier": "cold", "owner_user_id": me, "prospect_id": pid})
    assert sms["status"] == "held" and any(f["code"] == "not_opted_in" for f in sms["flags"])
    assert client.post("/t/demo/api/delivery/contacts", json={**body, "opted_in": False}).status_code == 200
    demo_login(client, "owner")
    assert client.post("/t/demo/api/delivery/contacts", json=body).json()["opted_in"] == 1
    consent = [a for a in client.get("/t/demo/api/audit", params={"kind": "consent"}).json()["items"]
               if "opted_in" in json.dumps(a)]
    assert consent  # recorded, with who did it


def test_a_crashing_follow_on_hook_never_turns_a_real_send_into_an_error(demo_tenant):
    @queue.on_approve("review_probe")
    def _boom(t, item, actor):
        raise RuntimeError("follow-on step broke")

    try:
        it = queue.add(demo_tenant, "review_probe", "Here is the replay link.", channel="email", recipient_ref="a@example.invalid")
        r = queue.approve(demo_tenant, it["id"], "owner(owner)")
        assert r["status"] == "sent" and "follow-on step failed" in r["status_reason"]
        assert [e for e in _events(demo_tenant, "item_sent") if e["queue_id"] == it["id"]]  # on_sent still fired
    finally:
        queue._HOOKS.pop("review_probe", None)


def test_live_calendar_failure_is_a_clean_502_and_the_invite_stays_open(tenant):
    tenant.set("live_calendar_enabled", True)  # switch on, but no Google login in the vault
    start = db.now() + 3 * DAY
    it = queue.add(tenant, "calendar_invite", "Strategy call", channel="calendar", recipient_ref="x@example.invalid",
                   meta={"slot": {"start": start, "end": start + 2700, "label": "Thu"}, "live": True,
                         "attendees": ["x@example.invalid"]})
    with pytest.raises(queue.QueueError) as ei:
        queue.approve(tenant, it["id"], "owner(owner)")
    assert ei.value.status == 502 and "nothing was sent" in str(ei.value)
    assert queue.get(tenant, it["id"])["status"] == "pending"
    with tenant.db() as c:
        assert c.execute("SELECT COUNT(*) FROM calendar_events").fetchone()[0] == 0


def test_one_broken_due_item_does_not_hold_up_the_others(demo_tenant, monkeypatch):
    a = queue.add(demo_tenant, "reminder", "First reminder.", channel="email", recipient_ref="a@example.invalid",
                  meta={"send_after": db.now() + 3600})
    b = queue.add(demo_tenant, "reminder", "Second reminder.", channel="email", recipient_ref="b@example.invalid",
                  meta={"send_after": db.now() + 7200})
    queue.approve(demo_tenant, a["id"], "o")
    queue.approve(demo_tenant, b["id"], "o")
    real_route, real_reason = queue._route, delivery.cancel_reason

    def route(t, item, actor):
        if item["id"] == a["id"]:
            raise RuntimeError("provider exploded token=abc123secret")
        return real_route(t, item, actor)

    monkeypatch.setattr(queue, "_route", route)
    delivery.dispatch_due(demo_tenant, now=db.now() + 8000)
    fa, fb = queue.get(demo_tenant, a["id"]), queue.get(demo_tenant, b["id"])
    assert fb["status"] == "sent"
    assert fa["status"] == "send_failed" and "abc123secret" not in fa["status_reason"]
    # an error before the send (in the checks) is isolated too, never left 'sending' or retried every tick
    c_ = queue.add(demo_tenant, "reminder", "Third reminder.", channel="email", recipient_ref="c@example.invalid",
                   meta={"send_after": db.now() + 3600})
    queue.approve(demo_tenant, c_["id"], "o")
    monkeypatch.setattr(delivery, "cancel_reason", lambda t, item, *a: (_ for _ in ()).throw(ValueError("bad data")))
    delivery.dispatch_due(demo_tenant, now=db.now() + 8000)
    assert queue.get(demo_tenant, c_["id"])["status"] == "send_failed"
    monkeypatch.setattr(delivery, "cancel_reason", real_reason)


def test_a_send_interrupted_mid_way_is_recovered_not_stuck(demo_tenant):
    it = queue.add(demo_tenant, "reminder", "Hello.", channel="email", recipient_ref="a@example.invalid")
    assert queue._claim(demo_tenant, it["id"], queue.OPEN)  # as if the server died right after claiming it
    assert "recovered" not in delivery.dispatch_due(demo_tenant)  # a slow send is left alone
    r = delivery.dispatch_due(demo_tenant, now=db.now() + queue.STUCK_AFTER + 60)
    assert r["recovered"] == [it["id"]]
    got = queue.get(demo_tenant, it["id"])
    # needs_attention: a person checks whether it went out; nothing re-sends it by itself
    assert got["status"] == "needs_attention" and "may or may not have gone out" in got["status_reason"]
    assert delivery.dispatch_due(demo_tenant, now=db.now() + 2 * queue.STUCK_AFTER)["due"] == 0
    assert queue.get(demo_tenant, it["id"])["status"] == "needs_attention"
    assert queue.cancel(demo_tenant, it["id"], "o")["status"] == "cancelled"
    # ... or confirms it did go out (on_sent runs once), or sends it again on purpose
    two = queue.add(demo_tenant, "reminder", "Hello again.", channel="email", recipient_ref="b@example.invalid")
    assert queue._claim(demo_tenant, two["id"], queue.OPEN)
    delivery.dispatch_due(demo_tenant, now=db.now() + queue.STUCK_AFTER + 60)
    assert queue.mark_sent(demo_tenant, two["id"], "o")["status"] == "sent"
    three = queue.add(demo_tenant, "reminder", "Hello a third time.", channel="email", recipient_ref="c@example.invalid")
    assert queue._claim(demo_tenant, three["id"], queue.OPEN)
    delivery.dispatch_due(demo_tenant, now=db.now() + queue.STUCK_AFTER + 60)
    assert queue.send_now(demo_tenant, three["id"], "o")["status"] == "sent"  # the demo's mock transport


def test_cancelling_an_approved_invite_cancels_the_call_and_its_reminders(tenant):
    lid = _lead(tenant)
    start = db.now() + 3 * DAY
    inv = queue.add(tenant, "calendar_invite", "Strategy call", channel="calendar", recipient_ref="test.lead@example.invalid",
                    meta={"lead_id": lid, "slot": {"start": start, "end": start + 2700, "label": "Thu 10:00"},
                          "attendees": ["test.lead@example.invalid"], "owner_only": True})
    assert queue.approve(tenant, inv["id"], "o")["status"] == "awaiting_manual_send"
    ladder = queue.list_items(tenant, kind="reminder")
    assert len(ladder) == 3
    queue.cancel(tenant, inv["id"], "owner(owner)", reason="They asked to move it")
    assert {queue.get(tenant, i["id"])["status"] for i in ladder} == {"cancelled"}
    with tenant.db() as c:
        assert c.execute("SELECT status FROM calendar_events WHERE queue_id=?", (inv["id"],)).fetchone()[0] == "cancelled"
    assert _events(tenant, "booking_cancelled")


def test_stopping_a_sequence_cancels_its_drafted_step(tenant):
    lid = _lead(tenant)
    first = queue.add(tenant, "setter_reply", "Thanks for reaching out.", channel="instagram_dm", recipient_ref="@test_demo",
                      meta={"lead_id": lid, "prospect_facing": True, "lead_tier": "warm", "prior_contact": True})
    cid = delivery.register_cadence(tenant, ("lead", lid), first["id"], "instagram_dm", "@test_demo", template="lead",
                                    context={"followups": ["A short video. Want it?", "A quick tip. What do you do today?"]})
    queue.approve(tenant, first["id"], "o")
    queue.mark_sent(tenant, first["id"], "o")
    step = delivery.run_cadences(tenant, now=db.now() + 3 * DAY)["drafted"][0]
    assert delivery.stop_cadence(tenant, cid, "owner(owner)")
    got = queue.get(tenant, step)
    assert got["status"] == "cancelled" and "stopped" in got["status_reason"]


def test_one_broken_sequence_does_not_stop_the_others(tenant, monkeypatch):
    lids = []
    for n in range(2):
        lid = _lead(tenant, handle=f"@seq{n}_demo", email=f"seq{n}@example.invalid")
        first = queue.add(tenant, "setter_reply", "Thanks for reaching out.", channel="instagram_dm", recipient_ref=f"@seq{n}_demo",
                          meta={"lead_id": lid, "prospect_facing": True, "lead_tier": "warm", "prior_contact": True})
        delivery.register_cadence(tenant, ("lead", lid), first["id"], "instagram_dm", f"@seq{n}_demo", template="lead",
                                  context={"followups": ["A short video. Want it?", "A quick tip?"]})
        queue.approve(tenant, first["id"], "o")
        queue.mark_sent(tenant, first["id"], "o")
        lids.append(lid)
    real = delivery.party_label
    monkeypatch.setattr(delivery, "party_label",
                        lambda t, p: (_ for _ in ()).throw(RuntimeError("x")) if p[1] == lids[0] else real(t, p))
    res = delivery.run_cadences(tenant, now=db.now() + 3 * DAY)
    monkeypatch.setattr(delivery, "party_label", real)
    assert len(res["drafted"]) == 1
    assert sorted(r["status"] for r in delivery.list_cadences(tenant)) == ["active", "waiting_send"]  # retried later


def test_unsubscribe_or_stop_from_a_chain_lead_blocks_every_channel(tenant, monkeypatch):
    monkeypatch.setenv("OPERATOR_PUBLIC_URL", "https://operator.example.invalid")
    _address(tenant)
    cl = _chain_lead(tenant, {"full_name": "Pat Example (DEMO)", "email": "pat.chain@example.invalid", "instagram": "pat_chain_demo"})
    it = queue.add(tenant, "followup", "Quick idea. Worth a look?", channel="email", recipient_ref="pat.chain@example.invalid",
                   meta={"prospect_facing": True, "lead_tier": "warm", "chain_lead_id": cl, "prior_contact": True})
    queue.approve(tenant, it["id"], "o")
    token = queue.get(tenant, it["id"])["meta"]["unsubscribe_path"].split("/u/")[1]
    assert delivery.unsubscribe(token)["already"] is False
    assert delivery.is_dnc(tenant, ("chain_lead", cl))
    dm = queue.add(tenant, "booking_dm", "Here are three times. Which suits?", channel="instagram_dm",
                   recipient_ref="@pat_chain_demo", meta={"prospect_facing": True, "lead_tier": "warm", "chain_lead_id": cl,
                                                          "prior_contact": True})
    assert dm["status"] == "held" and any(f["code"] == "do_not_contact" for f in dm["flags"])
    cl2 = _chain_lead(tenant, {"full_name": "Sam Example (DEMO)", "instagram": "sam_chain_demo"}, "lg-review-2")
    chain.record_reply(tenant, cl2, "Please stop messaging me")
    assert delivery.is_dnc(tenant, ("chain_lead", cl2)) and len(_events(tenant, "dnc_set")) == 2


def test_unsubscribe_clicked_twice_at_once_applies_once(client, tenant, monkeypatch):
    monkeypatch.setenv("OPERATOR_PUBLIC_URL", "https://operator.example.invalid")
    _address(tenant)
    pid = _prospect(tenant)
    it = queue.add(tenant, "prospect_email", "Quick idea. Worth a look?", channel="email", recipient_ref="pat.example@example.invalid",
                   meta={"prospect_facing": True, "lead_tier": "cold", "prospect_id": pid})
    queue.approve(tenant, it["id"], "o")
    token = queue.get(tenant, it["id"])["meta"]["unsubscribe_path"].split("/u/")[1]
    out = []
    ths = [threading.Thread(target=lambda: out.append(delivery.unsubscribe(token)["already"])) for _ in range(4)]
    for th in ths:
        th.start()
    for th in ths:
        th.join()
    assert sorted(out) == [False, True, True, True]
    assert len(_events(tenant, "unsubscribed")) == 1 and len(_events(tenant, "dnc_set")) == 1


def test_unsubscribe_guessing_is_throttled_but_real_unsubscribes_never_are(client, monkeypatch):
    monkeypatch.setenv("OPERATOR_PUBLIC_URL", "https://operator.example.invalid")
    codes = [client.post("/u/owner.not-a-real-token").status_code for _ in range(40)]
    assert codes[0] == 404 and codes[-1] == 429
    assert client.get("/u/owner.not-a-real-token").status_code == 429
    # the same address (say, a mail provider's one-click POSTs) can still unsubscribe with a real link
    t = tenancy.get("owner")
    _address(t)
    it = queue.add(t, "prospect_email", "Quick idea. Worth a look?", channel="email", recipient_ref="pat.example@example.invalid",
                   meta={"prospect_facing": True, "lead_tier": "cold", "prospect_id": _prospect(t)})
    queue.approve(t, it["id"], "o")
    path = queue.get(t, it["id"])["meta"]["unsubscribe_path"]
    assert client.get(path).status_code == 200
    r = client.post(path, content=b"List-Unsubscribe=One-Click",
                    headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 200 and "unsubscribed" in r.text


def test_times_can_only_be_picked_from_a_booking_message_that_went_out(tenant):
    lid = _lead(tenant)
    off = sales.offer_times(tenant, lid)
    for status in ("send_failed", "scheduled", "cancelled"):
        queue._set(tenant, off["booking_dm"], status=status)
        with pytest.raises(sales.BookingError) as ei:
            sales.pick_slot(tenant, lid, 0)
        assert ei.value.status == 409, status
    queue._set(tenant, off["booking_dm"], status="awaiting_manual_send")
    assert sales.pick_slot(tenant, lid, 0)["invite"]


def test_coach_sees_their_students_sequences(client):
    demo_login(client, "coach")
    me = client.get("/t/demo/api/me").json()["user"]
    t = tenancy.get("demo")
    with t.db() as c:
        sid = c.execute("INSERT INTO students(name,email,coach_id,demo) VALUES(?,?,?,1)",
                        ("Kit Example (DEMO)", "kit@example.invalid", me["id"])).lastrowid
        other = c.execute("INSERT INTO students(name,email,coach_id,demo) VALUES(?,?,?,1)",
                          ("Lou Example (DEMO)", "lou@example.invalid", 999999)).lastrowid
    mine = _user(t, "kit_review", "student", student_id=sid)
    theirs = _user(t, "lou_review", "student", student_id=other)
    a = delivery.register_cadence(t, ("prospect", _prospect(t, owner_user_id=mine)), 0, "instagram_dm", "@pat_demo",
                                  owner_user_id=mine)
    b = delivery.register_cadence(t, ("prospect", _prospect(t, owner_user_id=theirs, handle="@lou_p_demo")), 0, "instagram_dm",
                                  "@lou_p_demo", owner_user_id=theirs)
    ids = {r["id"] for r in client.get("/t/demo/api/delivery/cadences").json()["items"]}
    assert a in ids and b not in ids
    assert client.post(f"/t/demo/api/delivery/cadences/{b}/stop").status_code == 404


def test_previewed_email_drafts_are_not_blocked_for_the_footer_the_queue_adds(tenant):
    _address(tenant)
    # another feature previewing a prospect email before queueing it: the footer is added by queue.add, so only the
    # postal-address rule can apply to the preview; the queue's own check still enforces the unsubscribe line
    assert delivery.guard_rules("Quick idea?", "outreach", {"channel": "email"}, tenant) == []
    codes = {f["code"] for f in delivery.guard_rules("Quick idea?", "outreach", {"_channel": "email"}, tenant)}
    assert codes == {"missing_unsubscribe", "missing_postal_address"}
    skills.put_profile(tenant, {"identity": {"postal_address": ""}}, "test")
    assert {f["code"] for f in delivery.guard_rules("Quick idea?", "outreach", {"channel": "email"}, tenant)} == {
        "missing_postal_address"}  # still needs the address set up before any prospect email


def test_chain_reminders_use_the_handle_form_the_chain_dms_use(tenant):
    cl = _chain_lead(tenant, {"full_name": "Pat Example (DEMO)", "instagram": "pat_chain_demo"})
    assert delivery._party_row(tenant, ("chain_lead", cl))["handle"] == "@pat_chain_demo"


def test_cancelling_an_old_call_keeps_the_rescheduled_calls_reminders(tenant):
    lid = _lead(tenant)
    calls = []
    for days in (3, 5):  # first booking, then a second one (rescheduled) with the same lead
        start = db.now() + days * DAY
        inv = queue.add(tenant, "calendar_invite", "Strategy call", channel="calendar", recipient_ref="test.lead@example.invalid",
                        meta={"lead_id": lid, "slot": {"start": start, "end": start + 2700, "label": f"day {days}"},
                              "attendees": ["test.lead@example.invalid"], "owner_only": True})
        queue.approve(tenant, inv["id"], "o")
        with tenant.db() as c:
            ev = c.execute("SELECT id FROM calendar_events WHERE queue_id=?", (inv["id"],)).fetchone()[0]
        calls.append((ev, [i["id"] for i in queue.list_items(tenant, kind="reminder")
                           if i["meta"].get("calendar_event_id") == ev]))
    (old_ev, old_rem), (new_ev, new_rem) = calls
    for qid in old_rem + new_rem:
        queue.approve(tenant, qid, "o")  # approved: they wait as scheduled
    delivery.cancel_booking(tenant, old_ev, "owner(owner)")
    assert {queue.get(tenant, q)["status"] for q in old_rem} == {"cancelled"}
    assert {queue.get(tenant, q)["status"] for q in new_rem} == {"scheduled"}
    # and the send-time predicate agrees: the new call's reminders still go out
    for q in new_rem:
        assert delivery.cancel_reason(tenant, queue.get(tenant, q, scrub=False)) is None


def test_out_of_range_ids_are_422_never_a_database_overflow(client):
    demo_login(client, "owner")
    big = 2 ** 70
    calls = [
        ("get", f"/t/demo/api/queue/{big}", None), ("put", f"/t/demo/api/queue/{big}", {"body": "x"}),
        ("post", f"/t/demo/api/queue/{big}/approve", None), ("post", f"/t/demo/api/queue/{big}/reject", None),
        ("post", f"/t/demo/api/queue/{big}/sent", None), ("post", f"/t/demo/api/queue/{big}/cancel", None),
        ("post", f"/t/demo/api/queue/{big}/send-now", None), ("post", f"/t/demo/api/delivery/cadences/{big}/stop", None),
        ("delete", f"/t/demo/api/delivery/contacts/{big}", None), ("post", f"/t/demo/api/leads/{big}/reply", {"text": "yes"}),
        ("post", f"/t/demo/api/leads/{big}/offer-times", None), ("post", f"/t/demo/api/leads/{big}/pick-slot", {"index": 0}),
        ("get", f"/t/demo/api/leads/{big}/booking", None), ("post", f"/t/demo/api/chain/leads/{big}/offer-times", None),
        ("post", f"/t/demo/api/chain/leads/{big}/pick-slot", {"index": 0}), ("post", f"/t/demo/api/bookings/{big}/cancel", None),
        ("post", f"/t/demo/api/chain/leads/{big}/reply", {"text": "yes"}),
        ("post", f"/t/demo/api/chain/leads/{big}/outcome", {"outcome": "showed"}),
        ("post", "/t/demo/api/delivery/contacts", {"party_type": "lead", "party_id": big, "channel": "email",
                                                   "address": "a@example.invalid"}),
        ("post", "/t/demo/api/leads/1/pick-slot", {"start": 1e308}),
    ]
    for method, url, body in calls:
        r = getattr(client, method)(url, **({"json": body} if body is not None else {}))
        assert r.status_code == 422, (method, url, r.status_code)
    assert client.get("/t/demo/api/delivery/contacts", params={"party_type": "lead", "party_id": big}).status_code == 422
    assert client.get("/t/demo/api/queue", params={"ids": str(big)}).status_code == 200
    assert client.get("/t/demo/api/queue", params={"run_id": big}).status_code == 422
