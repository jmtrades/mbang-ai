"""Channels: Telegram (link code, ask, check-in, group), Slack (signature check, DM, mention), SMS and phone
(opt-in first, fictional numbers in practice), inbound email parsing (drafts only) and web voice (consent,
minutes quota, transcript, recap).

Every inbound message goes through inbound.route first: it is matched to a prospect, lead, client or
student, sorted, and answered with a draft a human approves. Students keep the cited Coach; unknown
senders never get course material. Provider retries and replays are dropped by event id (Telegram
update_id, Slack event_id, SMS/email message ids); signed SMS/email requests may carry a timestamp that
must be within five minutes (and must carry one outside the practice workspace). Every outbound reply is a
queue item; approved replies go to a mock transport unless a live switch is on."""
from __future__ import annotations

import email
import email.policy
import hashlib
import hmac
import re
import secrets
import time

from . import audit, db, inbound, retrieval, vault

SECRETS = {"telegram": "telegram_webhook_secret", "slack": "slack_signing_secret", "sms": "sms_webhook_secret",
           "email": "email_webhook_secret", "phone": "sms_webhook_secret"}
TOLERANCE = 300  # seconds a signed timestamp may be away from now
CHECKIN_LIMITS = {"outreach": 100000, "calls_booked": 10000, "calls_showed": 10000}
LINK_CODE_TTL = 3600  # a link code works for an hour
LINK_TRIES_PER_HOUR = 10  # link attempts one chat / number / Slack user may make in an hour (no guessing codes)
_LINK_TEXT = re.compile(r"^/?link\s+([A-Za-z0-9]{4,12})\s*$", re.I)


class ChannelError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


def _log(tenant, channel: str, direction: str, ext_id: str, body: str, meta: dict | None = None) -> int:
    with tenant.db() as c:
        return c.execute("INSERT INTO channel_messages(channel,direction,ext_id,body,meta,created) VALUES(?,?,?,?,?,?)",
                         (channel, direction, ext_id, (body or "")[:4000], db.jdump(meta or {}), db.now())).lastrowid


def _dup(provider: str, event_id) -> dict:
    return {"ok": True, "duplicate": True, "provider": provider, "event_id": str(event_id)[:80]}


# --------------------------------------------------------------------------- telegram
def link_code(tenant, user_id: int, channel: str = "telegram") -> str:
    code = secrets.token_hex(3).upper()
    with tenant.db() as c:
        c.execute("INSERT INTO channel_links(channel,user_id,code,status,created) VALUES(?,?,?,?,?)",
                  (channel, user_id, code, "pending", db.now()))
    return code


def _too_many_link_tries(tenant, channel: str, ext_id: str) -> bool:
    """Every inbound message is logged first, so recent 'link ...' messages from this sender are the tries."""
    chans = ("sms", "phone") if channel in ("sms", "phone") else (channel,)
    with tenant.db() as c:
        n = c.execute(f"SELECT COUNT(*) FROM channel_messages WHERE channel IN ({','.join('?' * len(chans))}) AND "
                      "direction='in' AND ext_id=? AND created>=? AND lower(ltrim(trim(body),'/')) LIKE 'link%'",
                      (*chans, str(ext_id), db.now() - 3600)).fetchone()[0]
    return n > LINK_TRIES_PER_HOUR


def _pending_code(c, code: str):
    """A link code someone generated in the last hour and has not used yet."""
    return c.execute("SELECT id, user_id, channel FROM channel_links WHERE code=? AND status='pending' AND "
                     "user_id IS NOT NULL AND created>=? ORDER BY id DESC LIMIT 1",
                     (str(code or "").upper()[:12], db.now() - LINK_CODE_TTL)).fetchone()


def parse_checkin(text: str) -> dict:
    """'/checkin 40 3 2 4' -> outreach, calls booked, calls showed, mood. Absurd numbers are refused, not stored."""
    raw = re.findall(r"\d+", text)[:4]
    if any(len(x) > 7 for x in raw):
        raise ChannelError(422, "Those numbers look too big. Send /checkin OUTREACH BOOKED SHOWED MOOD, e.g. /checkin 40 3 2 4")
    nums = [int(x) for x in raw] + [None] * (4 - len(raw))
    out = {"outreach": nums[0] or 0, "calls_booked": nums[1] or 0, "calls_showed": nums[2] or 0,
           "mood": 3 if nums[3] is None else nums[3]}
    for k, top in CHECKIN_LIMITS.items():
        if out[k] > top:
            raise ChannelError(422, f"{k.replace('_', ' ').capitalize()} can be at most {top:,}.")
    if out["calls_showed"] > out["calls_booked"] and nums[1] is not None:
        raise ChannelError(422, "Calls showed can't be more than calls booked.")
    if not 1 <= out["mood"] <= 5:
        raise ChannelError(422, "Mood is a number from 1 to 5.")
    return out


def telegram(tenant, secret_header: str | None, update: dict) -> dict:
    expected = vault.ensure_practice_secret(tenant, SECRETS["telegram"])
    if not secret_header or not hmac.compare_digest(str(secret_header), expected):
        raise ChannelError(401, "Bad Telegram secret token")
    uid = update.get("update_id")
    if uid is not None and inbound.seen(tenant, "telegram", uid):
        return _dup("telegram", uid)
    return _released_on_error(tenant, "telegram", uid, lambda: _telegram_update(tenant, update, uid))


def _released_on_error(tenant, provider: str, event_id, fn):
    """Run the handler; if it fails, forget the event id so the provider's retry is processed, not dropped."""
    try:
        return fn()
    except Exception:
        inbound.forget(tenant, provider, event_id)
        raise


def _telegram_update(tenant, update: dict, uid) -> dict:
    msg = update.get("message") if isinstance(update.get("message"), dict) else {}
    chat = msg.get("chat") if isinstance(msg.get("chat"), dict) else {}
    chat_id = str(chat.get("id", "") or "")[:64]
    text = msg.get("text") if isinstance(msg.get("text"), str) else ""
    text = text.strip()[:4000]
    if not chat_id or not text:
        return {"ok": True, "ignored": "empty"}
    sent_at = msg.get("date")
    if isinstance(sent_at, (int, float)) and not isinstance(sent_at, bool) and time.time() - sent_at > 86400:
        return {"ok": True, "ignored": "stale (older than a day)"}
    log_id = _log(tenant, "telegram", "in", chat_id, text)
    if chat.get("type") in ("group", "supergroup") and not (text.startswith("/") or "@" in text):
        return {"ok": True, "ignored": "group message without a mention"}
    text = re.sub(r"@\w+\s*", "", text).strip()
    if text.lower().startswith("/link"):
        code = text.split()[-1].upper() if len(text.split()) > 1 else ""
        if chat.get("type") not in (None, "private"):
            # a linked group would let everyone in it ask, and check in, as this person
            return {"ok": True, "linked": False, "error": "Link in a private chat with the bot, not in a group."}
        if _too_many_link_tries(tenant, "telegram", chat_id):
            return {"ok": True, "linked": False, "error": "Too many tries. Get a new code and try again in an hour."}
        with tenant.db() as c:
            r = _pending_code(c, code)
            if r is None or r["channel"] != "telegram":
                return {"ok": True, "linked": False}
            c.execute("UPDATE channel_links SET ext_id=?, status='linked' WHERE id=?", (chat_id, r["id"]))
        audit.log(tenant, f"telegram:{chat_id}", "action", {"channel_link": "telegram"})
        return {"ok": True, "linked": True}
    user = _linked_user(tenant, "telegram", chat_id)
    if text.lower().startswith("/checkin"):
        if not user or not user.get("student_id"):
            return {"ok": True, "error": "link your account first (/link CODE)"}
        try:
            data = parse_checkin(text[len("/checkin"):])
        except ChannelError as e:
            # answered with 200 so Telegram does not retry; nothing is stored
            return {"ok": False, "checkin": False, "status": e.status, "error": str(e)}
        from .agents import accountability

        accountability.submit_checkin(tenant, user["student_id"], data)
        return {"ok": True, "checkin": True}
    kw = inbound.opt_keyword(text)
    if kw == "in":
        inbound.reopt_in(tenant, "telegram", chat_id)
        return {"ok": True, "opted_in": True}
    q = text[4:].strip() if text.lower().startswith("/ask") else text
    res = inbound.route(tenant, "telegram", chat_id, q or text, {"user": user, "log_id": log_id, "provider_id": uid,
                                                                  "opt_out": kw == "out"})
    if kw == "out":
        res["opted_out"] = True
    return {"ok": True, **res}


def _linked_user(tenant, channel: str, ext_id: str) -> dict | None:
    with tenant.db() as c:
        r = c.execute("SELECT u.* FROM channel_links l JOIN users u ON u.id=l.user_id WHERE l.channel=? AND l.ext_id=? "
                      "AND l.status='linked' AND u.active=1 ORDER BY l.id DESC LIMIT 1", (channel, ext_id)).fetchone()
    return {"id": r["id"], "username": r["username"], "role": r["role"], "student_id": r["student_id"]} if r else None


# --------------------------------------------------------------------------- slack
def slack_sign(secret: str, ts: str, body: bytes) -> str:
    return "v0=" + hmac.new(secret.encode(), b"v0:" + ts.encode() + b":" + body, hashlib.sha256).hexdigest()


def slack(tenant, headers: dict, body: bytes, payload: dict) -> dict:
    secret = vault.ensure_practice_secret(tenant, SECRETS["slack"])
    ts = str(headers.get("x-slack-request-timestamp", ""))
    sig = str(headers.get("x-slack-signature", ""))
    try:
        fresh = abs(time.time() - int(ts)) < TOLERANCE
    except ValueError:
        fresh = False
    if not fresh or not hmac.compare_digest(sig, slack_sign(secret, ts, body)):
        raise ChannelError(401, "Bad Slack signature")
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge")}
    ev = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    event_id = payload.get("event_id") or ev.get("client_msg_id")
    if event_id and inbound.seen(tenant, "slack", event_id):
        return _dup("slack", event_id)  # Slack retries anything not answered within 3 s
    return _released_on_error(tenant, "slack", event_id, lambda: _slack_event(tenant, ev, event_id))


def _slack_event(tenant, ev: dict, event_id) -> dict:
    if ev.get("bot_id") or ev.get("subtype") == "bot_message":
        return {"ok": True, "ignored": "bot message"}
    if ev.get("type") == "app_mention" or (ev.get("type") == "message" and ev.get("channel_type") == "im"):
        text = re.sub(r"<@[A-Z0-9]+>", "", str(ev.get("text", ""))).strip()[:4000]
        sender = str(ev.get("user") or "?")[:64]
        reply_to = str(ev.get("channel") or "?")[:64]
        log_id = _log(tenant, "slack", "in", sender, text)
        if not text:
            return {"ok": True, "ignored": "empty"}
        m = _LINK_TEXT.match(text)
        if m and sender != "?":  # "link CODE" in a DM ties this Slack user to their account (students get the Coach)
            return {"ok": True, **_link_with_code(tenant, "slack", sender, m.group(1))}
        meta = {"reply_ref": reply_to, "log_id": log_id, "provider_id": event_id}
        from . import jobs, llm

        if llm.unavailable_reason(tenant) is None:
            # an AI draft can take longer than Slack waits: acknowledge now, draft in the background
            jid = jobs.enqueue(tenant, "inbound_route", {"channel": "slack", "ext_id": sender, "body": text, "meta": meta},
                               key=f"slack:{event_id}" if event_id else None)
            return {"ok": True, "accepted": True, "job": jid}
        return {"ok": True, **inbound.route(tenant, "slack", sender, text, meta)}
    return {"ok": True, "ignored": ev.get("type")}


# --------------------------------------------------------------------------- sms / phone
FICTIONAL = re.compile(r"^\+1-?555-?01\d\d$")


def sign_body(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def sign_body_ts(secret: str, ts: str, body: bytes) -> str:
    """Signature over '<unix timestamp>.<body>' (send the timestamp in X-Signature-Timestamp)."""
    return hmac.new(secret.encode(), str(ts).encode() + b"." + body, hashlib.sha256).hexdigest()


def _check_sig(tenant, channel: str, sig: str | None, body: bytes, ts: str | None = None) -> None:
    """With a timestamp: HMAC over 'ts.body', within five minutes (a captured request can't be replayed later).
    Without one: only the practice workspace accepts the older body-only signature; message ids still dedupe."""
    secret = vault.ensure_practice_secret(tenant, SECRETS[channel])
    if ts:
        try:
            fresh = abs(time.time() - int(str(ts).strip())) < TOLERANCE
        except ValueError:
            fresh = False
        if not fresh:
            raise ChannelError(401, f"Stale or malformed {channel} signature timestamp")
        if not sig or not hmac.compare_digest(str(sig), sign_body_ts(secret, str(ts).strip(), body)):
            raise ChannelError(401, f"Bad {channel} signature")
        return
    if not tenant.is_demo:
        raise ChannelError(401, f"The {channel} webhook needs a signed timestamp (X-Signature-Timestamp)")
    if not sig or not hmac.compare_digest(str(sig), sign_body(secret, body)):
        raise ChannelError(401, f"Bad {channel} signature")


def opted_in(tenant, channel: str, number: str) -> bool:
    refs = {str(number or "").strip(), inbound.canonical_phone(number)} - {""}
    if not refs:
        return False
    with tenant.db() as c:
        r = c.execute(f"SELECT opted_in FROM channel_links WHERE channel=? AND ext_id IN ({','.join('?' * len(refs))}) "
                      "ORDER BY id DESC LIMIT 1", (channel, *sorted(refs))).fetchone()
    return bool(r and r["opted_in"])


def _link_with_code(tenant, channel: str, ext_id: str, code: str) -> dict:
    """'LINK CODE' sent by text or as a Slack DM: ties that number / Slack user to the account that made the
    code (codes last an hour; ten tries an hour per sender). Linked students get the cited Coach there."""
    key = inbound.canonical_phone(ext_id) if channel in ("sms", "phone") else str(ext_id)[:64]
    if not key:
        return {"linked": False}
    if _too_many_link_tries(tenant, channel, ext_id):
        return {"linked": False, "error": "Too many tries. Get a new code and try again in an hour."}
    was_in = opted_in(tenant, channel, ext_id) if channel in ("sms", "phone") else True
    with tenant.db() as c:
        r = _pending_code(c, code)
        if r is None:
            return {"linked": False}
        c.execute("UPDATE channel_links SET status='used' WHERE id=?", (r["id"],))
        c.execute("INSERT INTO channel_links(channel,ext_id,user_id,opted_in,status,created) VALUES(?,?,?,?,?,?)",
                  (channel, key, r["user_id"], 1 if was_in else 0, "linked", db.now()))
    audit.log(tenant, f"{channel}:{key}", "action", {"channel_link": channel})
    return {"linked": True}


def _ever_opted_out(tenant, channel: str, number: str) -> bool:
    refs = {str(number or "").strip(), inbound.canonical_phone(number)} - {""}
    with tenant.db() as c:
        return c.execute(f"SELECT 1 FROM channel_links WHERE channel=? AND status='opted_out' AND ext_id IN "
                         f"({','.join('?' * len(refs))}) LIMIT 1", (channel, *sorted(refs))).fetchone() is not None


def sms(tenant, sig: str | None, body: bytes, payload: dict, channel: str = "sms", ts: str | None = None) -> dict:
    _check_sig(tenant, channel, sig, body, ts)
    number = str(payload.get("from") or payload.get("From") or "").strip()[:40]
    text = payload.get("body") if payload.get("body") is not None else payload.get("Body", "")
    text = (text if isinstance(text, str) else str(text)).strip()[:1600]
    if not number:
        raise ChannelError(422, "The message has no sender number")
    if tenant.is_demo and not FICTIONAL.match(number):
        raise ChannelError(422, "Practice mode only accepts fictional numbers (+1-555-01xx)")
    mid = payload.get("message_id") or payload.get("MessageSid") or payload.get("id")
    if mid and inbound.seen(tenant, channel, mid):
        return _dup(channel, mid)
    return _released_on_error(tenant, channel, mid, lambda: _sms_message(tenant, channel, number, text, mid))


def _sms_message(tenant, channel: str, number: str, text: str, mid) -> dict:
    log_id = _log(tenant, channel, "in", number, text)
    m = _LINK_TEXT.match(text)
    if m:
        return {"ok": True, **_link_with_code(tenant, channel, number, m.group(1))}
    kw = inbound.opt_keyword(text)
    if kw == "in":
        inbound.reopt_in(tenant, channel, number)
        return {"ok": True, "opted_in": True}
    if kw == "out":
        res = inbound.route(tenant, channel, number, text, {"opt_out": True, "log_id": log_id, "provider_id": mid,
                                                             "no_draft": True})
        return {"ok": True, **res, "opted_in": False}
    if re.sub(r"[^A-Za-z]", "", text).upper() == "YES" and not opted_in(tenant, channel, number) \
            and not _ever_opted_out(tenant, channel, number):
        # YES confirms a first opt-in; after a STOP only START / UNSTOP opt back in. The reply is still routed.
        inbound.reopt_in(tenant, channel, number)
    is_in = opted_in(tenant, channel, number)
    res = inbound.route(tenant, channel, number, text, {"log_id": log_id, "provider_id": mid, "no_draft": not is_in})
    if not is_in and res.get("party_type") == "unknown":
        res["ignored"] = "not opted in (reply START first); saved to the inbox for a person"
    return {"ok": True, **res}


# --------------------------------------------------------------------------- inbound email
QUOTE_START = re.compile(r"^(On .+ wrote:|-----Original Message-----|From: .+|>+.*)$", re.M)


def strip_quotes(body: str) -> str:
    if not isinstance(body, str):
        return ""
    out = []
    for line in body.splitlines():
        if QUOTE_START.match(line.strip()):
            break
        out.append(line)
    return "\n".join(out).strip()


def parse_email(raw: str) -> dict:
    """Headers and the first text part. The sender is stored as the bare address (never 'Name <addr>') so a
    reply goes to the right place; non-text bodies (an image, an attachment) are ignored, never passed on."""
    msg = email.message_from_string(raw if isinstance(raw, str) else "", policy=email.policy.default)
    body = ""
    try:
        part = msg.get_body(preferencelist=("plain", "html")) if msg.is_multipart() else msg
        if part is not None and part.get_content_maintype() == "text":
            try:
                content = part.get_content()
            except (LookupError, ValueError, KeyError):  # an unknown charset or a broken part: read it as bytes
                payload = part.get_payload(decode=True)
                content = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else ""
            body = content if isinstance(content, str) else ""
            if part.get_content_type() == "text/html" and body:
                from .uploads import _html

                body = _html(body)
    except Exception:  # noqa: BLE001 - a malformed body part is ignored, never a 500
        body = ""

    def hdr(name: str) -> str:
        # the stdlib header parser raises on some malformed headers (e.g. 'From: "'); treat those as missing
        try:
            return str(msg.get(name, "") or "")
        except Exception:  # noqa: BLE001
            try:  # fall back to the unparsed text of the header
                return next((str(v) for k, v in msg.raw_items() if str(k).lower() == name.lower()), "")
            except Exception:  # noqa: BLE001
                return ""

    raw_from = hdr("From")
    name, addr = inbound.parse_address(raw_from)
    auto = hdr("Auto-Submitted").lower()
    return {"from": addr, "from_name": name, "subject": hdr("Subject")[:300],
            "body": strip_quotes(body), "message_id": hdr("Message-ID").strip()[:200],
            "auto_reply": bool((auto and auto != "no") or hdr("X-Autoreply") or hdr("X-Autorespond"))}


def inbound_email(tenant, sig: str | None, raw: bytes, ts: str | None = None) -> dict:
    _check_sig(tenant, "email", sig, raw, ts)
    p = parse_email(raw.decode("utf-8", errors="replace"))
    if not p["from"]:
        raise ChannelError(422, "The email has no sender address")
    if p["message_id"] and inbound.seen(tenant, "email", p["message_id"]):
        return _dup("email", p["message_id"])

    def handle() -> dict:
        log_id = _log(tenant, "email", "in", p["from"], p["body"], {"subject": p["subject"]})
        res = inbound.route(tenant, "email", p["from"], p["body"][:4000] or p["subject"],
                            {"subject": p["subject"], "provider_id": p["message_id"] or None, "display_name": p["from_name"],
                             "auto_reply": p["auto_reply"], "log_id": log_id})
        return {"ok": True, "parsed": p, **res}

    return _released_on_error(tenant, "email", p["message_id"], handle)


# --------------------------------------------------------------------------- web voice
def voice_start(tenant, user_id: int, consent: bool) -> dict:
    if not consent:
        raise ChannelError(422, "Voice sessions need your consent to record a transcript.")
    used = voice_minutes_used(tenant, user_id)
    quota = float(tenant.get("voice_minutes_quota", 30) or 30)
    if used >= quota:
        raise ChannelError(429, f"Monthly voice quota of {quota:.0f} minutes used")
    with tenant.db() as c:
        sid = c.execute("INSERT INTO voice_sessions(user_id,consent,minutes,transcript,created) VALUES(?,?,?,?,?)",
                        (user_id, 1, 0.0, "", db.now())).lastrowid
    audit.log(tenant, str(user_id), "consent", {"voice_session": sid})
    return {"session_id": sid, "quota_minutes": quota, "used_minutes": round(used, 2)}


def voice_minutes_used(tenant, user_id: int) -> float:
    since = time.time() - 30 * 86400
    with tenant.db() as c:
        return float(c.execute("SELECT COALESCE(SUM(minutes),0) FROM voice_sessions WHERE user_id=? AND created>?",
                               (user_id, since)).fetchone()[0])


def voice_chunk(tenant, user_id: int, sid: int, text: str, seconds: float) -> dict:
    quota = float(tenant.get("voice_minutes_quota", 30) or 30)
    with tenant.db() as c:
        s = c.execute("SELECT * FROM voice_sessions WHERE id=? AND user_id=?", (sid, user_id)).fetchone()
        if s is None or not s["consent"]:
            raise ChannelError(404, "No consented voice session")
        if s["recap"]:
            raise ChannelError(409, "Session ended")
    if voice_minutes_used(tenant, user_id) + seconds / 60 > quota:
        raise ChannelError(429, "Voice minutes quota reached")
    with tenant.db() as c:
        c.execute("UPDATE voice_sessions SET transcript=transcript || ?, minutes=minutes + ? WHERE id=?",
                  (text.strip() + "\n", seconds / 60, sid))
    return {"ok": True}


def voice_end(tenant, user_id: int, sid: int) -> dict:
    with tenant.db() as c:
        s = c.execute("SELECT * FROM voice_sessions WHERE id=? AND user_id=?", (sid, user_id)).fetchone()
    if s is None:
        raise ChannelError(404, "Not found")
    qs = [l.strip() for l in s["transcript"].splitlines() if l.strip().endswith("?")]
    from .agents import coach

    answers = [coach.answer(tenant, q, record=False) for q in qs[:5]]
    recap = {"questions": len(qs), "minutes": round(s["minutes"], 2),
             "items": [{"q": q, "covered": a["covered"], "answer": a["answer"][:300], "citations": a["citations"][:2]}
                       for q, a in zip(qs, answers)]}
    with tenant.db() as c:
        c.execute("UPDATE voice_sessions SET recap=? WHERE id=?", (db.jdump(recap), sid))
    return {"transcript": s["transcript"], "recap": recap}


# --------------------------------------------------------------------------- voice note -> deck outline
def voice_note_deck(tenant, transcript: str) -> dict:
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", transcript) if len(s.split()) >= 3]
    slides = []
    for i in range(0, len(sents), 3):
        chunk = sents[i:i + 3]
        title = " ".join(chunk[0].split()[:7]).rstrip(".,!?")
        hits = retrieval.search(tenant, " ".join(chunk), k=1)
        slides.append({"n": len(slides) + 1, "title": title, "bullets": chunk,
                       "citation": retrieval.cite({**hits[0], "id": f"S{len(slides) + 1}"}) if hits else None})
    return {"slides": slides[:12], "source": "voice note transcript",
            "note": "Audio needs a transcription model; paste or upload the transcript (TXT/SRT/VTT)."}


# --------------------------------------------------------------------------- phone line (Twilio-style voice webhook)
def twilio_signature(auth_token: str, url: str, params: dict) -> str:
    import base64

    data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    return base64.b64encode(hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()).decode()


def _xml(s: str) -> str:
    import html as _h

    return _h.escape(s, quote=False)


def _twiml(*parts: str) -> str:
    return '<?xml version="1.0" encoding="UTF-8"?><Response>' + "".join(parts) + "</Response>"


# The phone line speaks in one fixed neutral system voice from a constant allowlist. No setting, persona field or
# request value can choose it: a cloned or person-like voice is never used.
NEUTRAL_TTS_VOICES = ("Polly.Joanna", "Polly.Matthew", "alice")
TTS_VOICE = NEUTRAL_TTS_VOICES[0]


def _say(text: str) -> str:
    return f'<Say voice="{TTS_VOICE}">{_xml(text)}</Say>'


def _gather(action: str, prompt: str) -> str:
    return (f'<Gather input="speech" action="{_xml(action)}" method="POST" speechTimeout="auto" language="en-US">'
            f'{_say(prompt)}</Gather>')


VOICE_HOLDING = "Thanks for calling. I've passed your message to the team, and a person will get back to you."


def voice_call(tenant, url: str, signature: str | None, params: dict) -> str:
    """Answer a member's spoken question with the cited coach, in a neutral system voice (never a
    cloned person). Only with the server-only live channels switch, a valid provider signature and an
    opted-in caller. The caller is matched first: only students, members and the team hear course
    material; anyone else is thanked, and their message goes to the inbox for a person. Returns TwiML."""
    token = vault.get(tenant, "sms_auth_token")
    if not tenant.get("live_channels_enabled", False) or not token:
        return _twiml("<Say>This line is not switched on yet.</Say><Hangup/>")
    if not signature or not hmac.compare_digest(signature, twilio_signature(token, url, params)):
        raise ChannelError(401, "Bad voice signature")
    caller = str(params.get("From", ""))[:40]
    if not opted_in(tenant, "phone", caller) and not opted_in(tenant, "sms", caller):
        return _twiml("<Say>Sorry, this number isn't registered. Text START to opt in first.</Say><Hangup/>")
    speech = str(params.get("SpeechResult", "")).strip()[:1000]
    if not speech:
        return _twiml(_gather(url, "Hi, ask me anything about the programme."))
    log_id = _log(tenant, "phone", "in", caller, speech)
    res = inbound.route(tenant, "phone", caller, speech[:500], {"voice": True, "log_id": log_id})
    if not res.get("spoken"):
        return _twiml(_say(VOICE_HOLDING) + "<Hangup/>")
    spoken = re.sub(r"\s*\[[SX]\d+\]", "", res["answer"])[:900]  # neither course [S#] nor playbook [X#] markers are read aloud
    _log(tenant, "phone", "out", caller, spoken, {"covered": res["covered"], "run_id": res["run_id"]})
    return _twiml(_say(spoken), _gather(url, "Anything else?"))
