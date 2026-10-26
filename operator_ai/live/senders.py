"""Real channel senders: Telegram Bot API, Slack chat.postMessage, Twilio-style SMS and SMTP email.

Used by queue.deliver ONLY for an approved item when the tenant's server-only switch
`live_channels_enabled` is on and the channel's login is in the vault. Otherwise delivery stays
on the mock transport. POSTs are never blindly retried (a duplicate message is worse than a
missed one); a failure leaves the item approved-but-undelivered with a redacted reason.
"""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from .. import vault
from . import net

NEEDS = {
    "telegram": ["telegram_bot_token"],
    "slack": ["slack_bot_token"],
    "sms": ["sms_account_sid", "sms_auth_token", "sms_from_number"],
    "email": ["smtp_host", "smtp_user", "smtp_password", "smtp_from"],
}


class SendError(Exception):
    pass


def ready(tenant, channel: str) -> bool:
    if channel not in NEEDS or not tenant.get("live_channels_enabled", False):
        return False
    return all(v == "set" for v in vault.status(tenant, NEEDS[channel]).values())


def send(tenant, channel: str, recipient: str, body: str, subject: str | None = None) -> str:
    """Returns the provider's message id."""
    if not ready(tenant, channel):
        raise SendError("live channel not enabled or login missing")
    v = lambda k: vault.get(tenant, k)
    if channel == "telegram":
        r = net.request(tenant.id, "POST", f"https://api.telegram.org/bot{v('telegram_bot_token')}/sendMessage",
                        json_body={"chat_id": recipient, "text": body}, idempotent=False)
        d = net.ok_json(r)
        if not d.get("ok"):
            raise SendError("Telegram refused the message")
        return str(d["result"]["message_id"])
    if channel == "slack":
        r = net.request(tenant.id, "POST", "https://slack.com/api/chat.postMessage",
                        headers={"authorization": f"Bearer {v('slack_bot_token')}"},
                        json_body={"channel": recipient, "text": body}, idempotent=False)
        d = net.ok_json(r)
        if not d.get("ok"):
            raise SendError(f"Slack refused the message: {d.get('error', 'unknown')}")
        return str(d.get("ts"))
    if channel == "sms":
        import base64

        sid = v("sms_account_sid")
        auth = base64.b64encode(f"{sid}:{v('sms_auth_token')}".encode()).decode()
        r = net.request(tenant.id, "POST", f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                        headers={"authorization": f"Basic {auth}"},
                        data={"From": v("sms_from_number"), "To": recipient, "Body": body}, idempotent=False)
        return str(net.ok_json(r).get("sid"))
    if channel == "email":
        msg = EmailMessage()
        msg["From"] = v("smtp_from")
        msg["To"] = recipient
        msg["Subject"] = subject or "A note from the team"
        msg.set_content(body)
        host = v("smtp_host")
        port = int(vault.get(tenant, "smtp_port") or 587)
        try:
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(v("smtp_user"), v("smtp_password"))
                s.send_message(msg)
        except (OSError, smtplib.SMTPException) as e:
            raise SendError(net.redact(f"SMTP failed: {type(e).__name__}"))
        return msg.get("Message-ID") or "sent"
    raise SendError(f"no live sender for {channel}")
