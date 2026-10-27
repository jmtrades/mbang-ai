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


def test_manual_prospect_email_copy_text_has_the_link(tenant):
    _address(tenant)
    it = queue.add(tenant, "prospect_email", "Quick idea. Worth a look?", channel="email", recipient_ref="pat.example@example.invalid",
                   meta={"prospect_facing": True, "lead_tier": "cold"})
    r = queue.approve(tenant, it["id"], "o")
    assert r["status"] == "awaiting_manual_send" and "/u/" in r["send_text"] and "{unsubscribe_link}" not in r["send_text"]


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
