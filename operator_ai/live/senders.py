"""Real channel senders: Telegram Bot API, Slack chat.postMessage, Twilio-style SMS and SMTP email.

Used by the approval queue (delivery.send_live) ONLY for an approved item when the tenant's server-only
switch `live_channels_enabled` is on and the channel's login is in the vault. Otherwise the item waits for
a person to send it (or, in the demo workspace only, goes to a mock transport). POSTs are never blindly
retried (a duplicate message is worse than a missed one); a failure leaves the item unsent with a
redacted reason. Recipients are normalised here too: email "Name <addr>" -> addr, SMS -> E.164.
"""
from __future__ import annotations

import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr

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


_EMAIL = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
# Headers a caller may add to an email (compliance headers only; nothing that changes routing).
EMAIL_HEADERS = {"List-Unsubscribe", "List-Unsubscribe-Post"}


def email_address(recipient: str) -> str:
    """'Jane Doe <jane@example.com>' -> 'jane@example.com'. Raises SendError when there is no valid address."""
    _, addr = parseaddr(recipient or "")
    if not addr or not _EMAIL.match(addr):
        raise SendError("no valid email address for this recipient")
    return addr


def e164(recipient: str) -> str:
    raw = (recipient or "").strip()
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("00"):
        digits = digits[2:]
    if not (raw.startswith("+") or raw.startswith("00")) or not 8 <= len(digits) <= 15:
        raise SendError("the phone number needs a country code (E.164, e.g. +44...)")
    return "+" + digits


def send(tenant, channel: str, recipient: str, body: str, subject: str | None = None, headers: dict | None = None,
         idempotency_key: str | None = None) -> str:
    """Returns the provider's message id. idempotency_key (the queue item) becomes the email Message-ID."""
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
                        data={"From": v("sms_from_number"), "To": e164(recipient), "Body": body}, idempotent=False)
        return str(net.ok_json(r).get("sid"))
    if channel == "email":
        to = email_address(recipient)
        sender = v("smtp_from")
        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = to
        msg["Subject"] = subject or "A note from the team"
        domain = (parseaddr(sender)[1].rpartition("@")[2] or "localhost")
        msg["Message-ID"] = make_msgid(idstring=(idempotency_key or None), domain=domain)
        for k, val in (headers or {}).items():
            if k in EMAIL_HEADERS and val:
                msg[k] = str(val).replace("\r", " ").replace("\n", " ")
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
