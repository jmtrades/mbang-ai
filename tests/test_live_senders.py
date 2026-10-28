"""Real channel senders, tested against fakes: only behind the server-only switch + vault login,
only for approved items, never blindly retried, opt-in still enforced for SMS."""
import pytest

from operator_ai import queue, settings, vault
from operator_ai.live import net, senders
from tests.fakes import FakeWorld


@pytest.fixture
def world(monkeypatch):
    w = FakeWorld()
    net.set_transport(w.transport)
    monkeypatch.setattr(net, "_sleep", lambda s: None)
    yield w
    net.set_transport(None)
    assert w.stray == []


def _creds(t):
    vault.put(t, {"telegram_bot_token": "123:fake", "slack_bot_token": "xoxb-fake", "sms_account_sid": "ACfake",
                  "sms_auth_token": "tok", "sms_from_number": "+1-555-0100", "smtp_host": "smtp.example.invalid",
                  "smtp_user": "u", "smtp_password": "p", "smtp_from": "team@example.invalid"})


def test_switch_off_means_mock_even_with_logins(tenant, world):
    _creds(tenant)
    it = queue.add(tenant, "channel_reply", "Here is the checklist.", channel="telegram", recipient_ref="4242")
    # Mock transports run only in the demo workspace (2026-10 delivery truth): with the switch off, a real
    # workspace hands the message to a person instead of pretending it was delivered.
    done = queue.approve(tenant, it["id"], "owner(owner)")
    assert done["status"] == "awaiting_manual_send" and done["delivered"] is None
    assert world.sent == []


def test_switch_is_server_only(owner):
    r = owner.put("/t/owner/api/settings", json={"key": "live_channels_enabled", "value": True})
    assert r.status_code == 403


def test_telegram_slack_sms_live_after_approval(tenant, world):
    _creds(tenant)
    settings.set_box(tenant, "live_channels_enabled", True, "owner yes recorded (test)")
    tg = queue.add(tenant, "channel_reply", "Telegram answer", channel="telegram", recipient_ref="4242")
    assert world.sent == []  # queuing sends nothing
    d = queue.approve(tenant, tg["id"], "owner(owner)")["delivered"]
    assert d == "live:telegram:777" and world.sent[0][0] == "api.telegram.org" and '"chat_id":"4242"' in world.sent[0][2]
    sl = queue.add(tenant, "channel_reply", "Slack answer", channel="slack", recipient_ref="C123")
    assert queue.approve(tenant, sl["id"], "owner(owner)")["delivered"].startswith("live:slack:")
    assert world.sent[1][3] == "Bearer xoxb-fake"
    # SMS: opt-in still required, even live
    sms = queue.add(tenant, "reminder", "See you at 3pm", channel="sms", recipient_ref="+1-555-0142")
    assert sms["status"] == "held"
    with tenant.db() as c:
        c.execute("INSERT INTO channel_links(channel,ext_id,opted_in,status,created) VALUES('sms','+1-555-0142',1,'opted_in',0)")
    sms = queue.edit(tenant, sms["id"], "See you at 3pm", "owner")
    assert queue.approve(tenant, sms["id"], "owner(owner)")["delivered"] == "live:sms:SMfake123"
    assert "To=%2B15550142" in world.sent[2][2]  # numbers are normalised to E.164 before sending (was raw "+1-555-0142")


def test_failed_live_send_leaves_item_open_and_is_not_retried(tenant, world):
    _creds(tenant)
    vault.put(tenant, {"telegram_bot_token": "BADTOKEN"})
    settings.set_box(tenant, "live_channels_enabled", True, "test")
    it = queue.add(tenant, "channel_reply", "hello", channel="telegram", recipient_ref="1")
    with pytest.raises(queue.QueueError) as ei:
        queue.approve(tenant, it["id"], "owner(owner)")
    assert ei.value.status == 502 and "BADTOKEN" not in str(ei.value)
    assert queue.get(tenant, it["id"])["status"] == "pending"
    assert len(world.sent) == 1  # the POST was not retried


def test_guardrails_still_block_before_any_live_send(tenant, world):
    _creds(tenant)
    settings.set_box(tenant, "live_channels_enabled", True, "test")
    it = queue.add(tenant, "channel_reply", "We guarantee you'll double revenue", channel="telegram", recipient_ref="1")
    with pytest.raises(queue.QueueError):
        queue.approve(tenant, it["id"], "owner(owner)")
    assert world.sent == []


def test_email_via_smtp(tenant, monkeypatch):
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            assert host == "smtp.example.invalid" and port == 587

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context=None):
            pass

        def login(self, u, p):
            assert (u, p) == ("u", "p")

        def send_message(self, msg):
            sent.append(msg)

    monkeypatch.setattr(senders.smtplib, "SMTP", FakeSMTP)
    _creds(tenant)
    settings.set_box(tenant, "live_channels_enabled", True, "test")
    it = queue.add(tenant, "email_reply", "Here's your replay link.", channel="email", recipient_ref="lee@example.invalid",
                   meta={"subject": "Your replay", "client_asset": True})
    assert queue.approve(tenant, it["id"], "owner(owner)")["delivered"].startswith("live:email:")
    assert sent[0]["Subject"] == "Your replay" and sent[0]["To"] == "lee@example.invalid"


def test_never_invite_blocks_live_send(tenant, world):
    _creds(tenant)
    settings.set_box(tenant, "live_channels_enabled", True, "test")
    tenant.set("blocked_attendees", ["no.one@gmail.com"])
    it = queue.add(tenant, "email_reply", "hi", channel="email", recipient_ref="noone+x@googlemail.com")
    with pytest.raises(queue.QueueError):
        queue.approve(tenant, it["id"], "owner(owner)")
