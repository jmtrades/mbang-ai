"""Mock channels: Telegram (link code, ask, check-in, group), Slack (signature check, DM,
mention), SMS and phone (opt-in first, fictional numbers), inbound email parsing (drafts only)
and web voice (consent, minutes quota, transcript, recap). Every outbound reply is a queue item;
approved replies go to a mock transport."""
from __future__ import annotations

import email
import email.policy
import hashlib
import hmac
import re
import secrets
import time

from . import audit, db, queue, retrieval, vault

SECRETS = {"telegram": "telegram_webhook_secret", "slack": "slack_signing_secret", "sms": "sms_webhook_secret",
           "email": "email_webhook_secret", "phone": "sms_webhook_secret"}


class ChannelError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


def _log(tenant, channel: str, direction: str, ext_id: str, body: str, meta: dict | None = None) -> None:
    with tenant.db() as c:
        c.execute("INSERT INTO channel_messages(channel,direction,ext_id,body,meta,created) VALUES(?,?,?,?,?,?)",
                  (channel, direction, ext_id, body[:4000], db.jdump(meta or {}), db.now()))


def _answer_draft(tenant, channel: str, ext_id: str, question: str, user: dict | None = None) -> dict:
    from .agents import coach

    res = coach.answer(tenant, question, user=user, record=True)
    cites = "; ".join(f"[{c['id']}] {c['title']}" for c in res["citations"][:3])
    body = res["answer"] + (f"\nSources: {cites}" if cites else "")
    refs = coach.external_refs(res["answer"], res.get("external", []), tenant)
    body += f"\n\n{refs}" if refs else ""
    item = queue.add(tenant, "channel_reply", body, channel=channel, recipient_ref=ext_id,
                     title=f"{channel.title()} reply to {ext_id}", meta={"question": question[:300], "covered": res["covered"],
                                                                        "run_id": res["run_id"]},
                     run_id=res["run_id"], actor=f"{channel}-bot", demo=tenant.is_demo)
    return {"queued": item["id"], "covered": res["covered"], "status": item["status"]}


# --------------------------------------------------------------------------- telegram
def link_code(tenant, user_id: int, channel: str = "telegram") -> str:
    code = secrets.token_hex(3).upper()
    with tenant.db() as c:
        c.execute("INSERT INTO channel_links(channel,user_id,code,status,created) VALUES(?,?,?,?,?)",
                  (channel, user_id, code, "pending", db.now()))
    return code


def telegram(tenant, secret_header: str | None, update: dict) -> dict:
    expected = vault.ensure_practice_secret(tenant, SECRETS["telegram"])
    if not secret_header or not hmac.compare_digest(secret_header, expected):
        raise ChannelError(401, "Bad Telegram secret token")
    msg = update.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    text = (msg.get("text") or "").strip()
    if not chat_id or not text:
        return {"ok": True, "ignored": "empty"}
    _log(tenant, "telegram", "in", chat_id, text)
    if chat.get("type") in ("group", "supergroup") and not (text.startswith("/") or "@" in text):
        return {"ok": True, "ignored": "group message without a mention"}
    text = re.sub(r"@\w+\s*", "", text).strip()
    if text.startswith("/link"):
        code = text.split()[-1].upper() if len(text.split()) > 1 else ""
        with tenant.db() as c:
            r = c.execute("SELECT id FROM channel_links WHERE channel='telegram' AND code=? AND status='pending'", (code,)).fetchone()
            if r is None:
                return {"ok": True, "linked": False}
            c.execute("UPDATE channel_links SET ext_id=?, status='linked' WHERE id=?", (chat_id, r["id"]))
        return {"ok": True, "linked": True}
    user = _linked_user(tenant, "telegram", chat_id)
    if text.startswith("/checkin"):
        if not user or not user.get("student_id"):
            return {"ok": True, "error": "link your account first (/link CODE)"}
        nums = [int(x) for x in re.findall(r"\d+", text)[:4]] + [0, 0, 0, 3]
        from .agents import accountability

        accountability.submit_checkin(tenant, user["student_id"], {"outreach": nums[0], "calls_booked": nums[1],
                                                                   "calls_showed": nums[2], "mood": nums[3] or 3})
        return {"ok": True, "checkin": True}
    q = text[4:].strip() if text.startswith("/ask") else text
    return {"ok": True, **_answer_draft(tenant, "telegram", chat_id, q, user)}


def _linked_user(tenant, channel: str, ext_id: str) -> dict | None:
    with tenant.db() as c:
        r = c.execute("SELECT u.* FROM channel_links l JOIN users u ON u.id=l.user_id WHERE l.channel=? AND l.ext_id=? "
                      "AND l.status='linked'", (channel, ext_id)).fetchone()
    return {"id": r["id"], "username": r["username"], "role": r["role"], "student_id": r["student_id"]} if r else None


# --------------------------------------------------------------------------- slack
def slack_sign(secret: str, ts: str, body: bytes) -> str:
    return "v0=" + hmac.new(secret.encode(), b"v0:" + ts.encode() + b":" + body, hashlib.sha256).hexdigest()


def slack(tenant, headers: dict, body: bytes, payload: dict) -> dict:
    secret = vault.ensure_practice_secret(tenant, SECRETS["slack"])
    ts = headers.get("x-slack-request-timestamp", "")
    sig = headers.get("x-slack-signature", "")
    try:
        fresh = abs(time.time() - int(ts)) < 300
    except ValueError:
        fresh = False
    if not fresh or not hmac.compare_digest(sig, slack_sign(secret, ts, body)):
        raise ChannelError(401, "Bad Slack signature")
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge")}
    ev = payload.get("event") or {}
    if ev.get("bot_id"):
        return {"ok": True, "ignored": "bot message"}
    if ev.get("type") == "app_mention" or (ev.get("type") == "message" and ev.get("channel_type") == "im"):
        text = re.sub(r"<@[A-Z0-9]+>", "", ev.get("text", "")).strip()
        _log(tenant, "slack", "in", ev.get("user", "?"), text)
        return {"ok": True, **_answer_draft(tenant, "slack", ev.get("channel", "?"), text)}
    return {"ok": True, "ignored": ev.get("type")}


# --------------------------------------------------------------------------- sms / phone
FICTIONAL = re.compile(r"^\+1-?555-?01\d\d$")


def sign_body(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _check_sig(tenant, channel: str, sig: str | None, body: bytes) -> None:
    secret = vault.ensure_practice_secret(tenant, SECRETS[channel])
    if not sig or not hmac.compare_digest(sig, sign_body(secret, body)):
        raise ChannelError(401, f"Bad {channel} signature")


def opted_in(tenant, channel: str, number: str) -> bool:
    with tenant.db() as c:
        r = c.execute("SELECT opted_in FROM channel_links WHERE channel=? AND ext_id=? ORDER BY id DESC LIMIT 1",
                      (channel, number)).fetchone()
    return bool(r and r["opted_in"])


def sms(tenant, sig: str | None, body: bytes, payload: dict, channel: str = "sms") -> dict:
    _check_sig(tenant, channel, sig, body)
    number = str(payload.get("from", ""))
    text = str(payload.get("body", "")).strip()
    if tenant.is_demo and not FICTIONAL.match(number):
        raise ChannelError(422, "Practice mode only accepts fictional numbers (+1-555-01xx)")
    _log(tenant, channel, "in", number, text)
    word = text.upper()
    if word in ("START", "YES", "UNSTOP"):
        with tenant.db() as c:
            c.execute("INSERT INTO channel_links(channel,ext_id,opted_in,status,created) VALUES(?,?,1,'opted_in',?)",
                      (channel, number, db.now()))
        audit.log(tenant, f"{channel}:{number}", "consent", {"channel": channel, "opt_in": True})
        return {"ok": True, "opted_in": True}
    if word in ("STOP", "UNSUBSCRIBE", "CANCEL"):
        with tenant.db() as c:
            c.execute("INSERT INTO channel_links(channel,ext_id,opted_in,status,created) VALUES(?,?,0,'opted_out',?)",
                      (channel, number, db.now()))
        audit.log(tenant, f"{channel}:{number}", "consent", {"channel": channel, "opt_in": False})
        return {"ok": True, "opted_in": False}
    if not opted_in(tenant, channel, number):
        return {"ok": True, "ignored": "not opted in (reply START first)"}
    return {"ok": True, **_answer_draft(tenant, channel, number, text)}


# --------------------------------------------------------------------------- inbound email
QUOTE_START = re.compile(r"^(On .+ wrote:|-----Original Message-----|From: .+|>+.*)$", re.M)


def strip_quotes(body: str) -> str:
    out = []
    for line in body.splitlines():
        if QUOTE_START.match(line.strip()):
            break
        out.append(line)
    return "\n".join(out).strip()


def parse_email(raw: str) -> dict:
    msg = email.message_from_string(raw, policy=email.policy.default)
    part = msg.get_body(preferencelist=("plain", "html")) if msg.is_multipart() else msg
    body = part.get_content() if part is not None else ""
    if part is not None and part.get_content_type() == "text/html":
        from .uploads import _html

        body = _html(body)
    return {"from": str(msg.get("From", "")), "subject": str(msg.get("Subject", "")), "body": strip_quotes(body)}


def inbound_email(tenant, sig: str | None, raw: bytes) -> dict:
    _check_sig(tenant, "email", sig, raw)
    p = parse_email(raw.decode("utf-8", errors="replace"))
    _log(tenant, "email", "in", p["from"], p["body"], {"subject": p["subject"]})
    from .agents import coach

    res = coach.answer(tenant, p["body"][:1000] or p["subject"], record=True)
    subj = p["subject"] if p["subject"].lower().startswith("re:") else f"Re: {p['subject']}"
    refs = coach.external_refs(res["answer"], res.get("external", []), tenant)
    item = queue.add(tenant, "email_reply", res["answer"] + (f"\n\n{refs}" if refs else ""), channel="email", recipient_ref=p["from"],
                     title=f"Email reply to {p['from']}", meta={"subject": subj, "prior_contact": True, "covered": res["covered"]},
                     run_id=res["run_id"], actor="email-inbound", demo=tenant.is_demo)
    return {"ok": True, "parsed": p, "queued": item["id"], "status": item["status"]}


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


def _gather(action: str, prompt: str) -> str:
    return (f'<Gather input="speech" action="{_xml(action)}" method="POST" speechTimeout="auto" language="en-US">'
            f'<Say voice="Polly.Joanna">{_xml(prompt)}</Say></Gather>')


def voice_call(tenant, url: str, signature: str | None, params: dict) -> str:
    """Answer a member's spoken question with the cited coach, in a neutral system voice (never a
    cloned person). Only with the server-only live channels switch, a valid provider signature and an
    opted-in caller. Returns TwiML."""
    token = vault.get(tenant, "sms_auth_token")
    if not tenant.get("live_channels_enabled", False) or not token:
        return _twiml("<Say>This line is not switched on yet.</Say><Hangup/>")
    if not signature or not hmac.compare_digest(signature, twilio_signature(token, url, params)):
        raise ChannelError(401, "Bad voice signature")
    caller = str(params.get("From", ""))
    if not opted_in(tenant, "phone", caller) and not opted_in(tenant, "sms", caller):
        return _twiml("<Say>Sorry, this number isn't registered. Text START to opt in first.</Say><Hangup/>")
    speech = str(params.get("SpeechResult", "")).strip()
    if not speech:
        return _twiml(_gather(url, "Hi, ask me anything about the programme."))
    _log(tenant, "phone", "in", caller, speech)
    from .agents import coach

    res = coach.answer(tenant, speech[:500], record=True)
    spoken = re.sub(r"\s*\[[SX]\d+\]", "", res["answer"])[:900]
    _log(tenant, "phone", "out", caller, spoken, {"covered": res["covered"], "run_id": res["run_id"]})
    return _twiml(f'<Say voice="Polly.Joanna">{_xml(spoken)}</Say>', _gather(url, "Anything else?"))
