"""Inbound replies: every message that arrives on a channel is matched to a person we know, sorted, and
answered with a draft a human approves.

    inbound.route(tenant, "email", "lee@example.invalid", "How much is it?", {"subject": "Re: hello"})

1. Match the sender to a prospect, lead, client, student/member or team account by canonical email,
   handle or phone (an explicit channel link or a person's earlier triage wins; the delivery feature's
   contact_points table is read when it exists).
2. Sort the reply with keyword rules into a fixed set of labels (LABELS). When AI is allowed and the rules
   find nothing, the model may only pick one of those labels; anything else is ignored.
3. Prospects and leads: move the pipeline stage, then draft the next setter message from the sales playbook
   (template, or AI when the budget allows) as a queue item with meta.owner_user_id, so the operator approves
   their own. Emits reply_received so a waiting mission step wakes up.
4. Opt-outs (STOP, UNSUBSCRIBE, "stop please" ...) set do-not-contact, add the sender to the never-contact
   list, hold anything still waiting to go to them, emit dnc_set and never get an auto-drafted reply.
5. Legal threats, refund requests and complaints are escalated to a person; the only draft is a neutral
   holding line marked "needs a person".
6. Students, members and team accounts keep the cited Coach answer they had before.
7. Unknown senders never get course material: a neutral holding draft (needs a person) and a triage entry.

Every route runs through runtime.execute, so each inbound message leaves a trace.
"""
from __future__ import annotations

import email.utils
import json
import logging
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from . import audit, auth, db, events, guardrails, jobs, queue, runtime

log = logging.getLogger("operator.inbound")

LABELS = {
    "positive": "Interested",
    "question": "Asked a question",
    "price_ask": "Asked about price",
    "objection_not_now": "Not right now",
    "objection_cost": "Worried about cost",
    "objection_partner": "Needs to check with someone",
    "not_interested": "Not interested",
    "unsubscribe": "Asked to stop",
    "out_of_office": "Auto-reply / away",
    "legal_complaint": "Complaint or legal",
}
PARTY_LABELS = {"prospect": "Prospect", "lead": "Lead", "client": "Client", "student": "Student", "member": "Member",
                "staff": "Team", "unknown": "Unknown sender"}
CHANNEL_LABELS = {"email": "Email", "sms": "Text message", "phone": "Phone", "telegram": "Telegram", "slack": "Slack",
                  "instagram_dm": "Instagram DM", "linkedin": "LinkedIn", "facebook": "Facebook", "manual": "Pasted in"}
CAPTURE_CHANNELS = ("instagram_dm", "linkedin", "facebook", "manual")
SLA_SECONDS = 60 * 60
HELD_SECONDS = 24 * 3600
PLAN = ["Match the sender", "Sort the reply", "Update the pipeline", "Draft the next message", "Notify"]


class InboundError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


# --------------------------------------------------------------------------- canonical addresses
def _nfkc(s: str) -> str:
    t = unicodedata.normalize("NFKC", s or "")
    return t.replace("’", "'").replace("‘", "'")


def parse_address(raw: str) -> tuple[str, str]:
    """('Jane Doe <jane@x.com>') -> ('Jane Doe', 'jane@x.com'). The address is '' when there is none."""
    name, addr = email.utils.parseaddr(str(raw or ""))
    addr = addr.strip()
    if not re.fullmatch(r"[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+", addr):
        m = queue._EMAIL.search(str(raw or ""))
        addr = m.group(0) if m else ""
    return name.strip()[:120], addr[:254]


def canonical_phone(raw: str) -> str:
    s = str(raw or "").strip()
    digits = re.sub(r"\D", "", s)
    if not digits:
        return ""
    if s.startswith("00"):
        digits = digits[2:]
    elif not s.startswith("+") and len(digits) == 10:
        digits = "1" + digits  # a bare 10-digit number is read as North American
    return "+" + digits


def canonical_handle(raw: str) -> str:
    s = str(raw or "").strip().lower()
    s = re.sub(r"^https?://(www\.)?(instagram\.com|linkedin\.com/in|facebook\.com|x\.com|twitter\.com)/", "", s)
    return s.strip("/").lstrip("@").strip()


def canonical(channel: str, ext_id: str) -> str:
    s = str(ext_id or "").strip()
    if channel == "email" or (("@" in s) and not s.startswith("@") and "." in s.rsplit("@", 1)[-1]):
        _, addr = parse_address(s)
        return queue.canonical_email(addr) if addr else s.lower()
    if channel in ("sms", "phone", "whatsapp"):
        return canonical_phone(s)
    if channel in CAPTURE_CHANNELS:
        return canonical_handle(s)
    return s  # telegram chat ids, slack user ids: exact


# --------------------------------------------------------------------------- opt-out keywords
OPT_OUT_WORDS = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT", "OPTOUT", "REVOKE"}
OPT_IN_WORDS = {"START", "UNSTOP"}
_FILLERS = {"PLEASE", "PLS", "PLZ", "NOW", "ME", "US", "IT", "THANKS", "THANK", "YOU", "ALL", "THIS", "THAT", "OK", "OKAY",
            "MESSAGES", "MESSAGE", "MESSAGING", "TEXT", "TEXTS", "TEXTING", "EMAIL", "EMAILS", "EMAILING", "CONTACTING",
            "SENDING", "CALLING", "CALLS", "SPAM", "SPAMMING", "THEM", "AND", "FROM", "LIST", "THE", "SUBSCRIPTION", "JUST",
            "KINDLY", "ASAP", "RIGHT", "AWAY", "IMMEDIATELY", "HERE", "AGAIN"}
_OPT_OUT_PHRASE = re.compile(
    r"\b(unsubscribe|opt[\s-]?out|remove me from (your|the|this) (mailing )?(list|emails?|messages|texts|database)|"
    r"take me off (your|the|this) (mailing )?list|stop (messaging|emailing|texting|contacting|sending|calling|spamming) (me|us)|"
    r"(do not|don'?t) (contact|message|email|text|call) (me|us)|no more (messages|emails|texts))\b", re.I)


def _words(text: str) -> list[str]:
    t = _nfkc(text).upper().replace("'", "")
    t = re.sub(r"[^A-Z0-9 ]+", " ", t)
    return t.split()


def opt_keyword(text: str) -> str | None:
    """'out' for an opt-out (STOP, STOPALL, UNSUBSCRIBE, CANCEL, END, QUIT, OPT OUT, REVOKE and natural phrasings
    such as "stop please", "Unsubscribe me", "Stop."), 'in' for START / UNSTOP, else None. Case- and
    punctuation-insensitive. CANCEL / END only count when the rest of the message is filler words, so
    "Cancel my call" or "end of the month works" are not opt-outs."""
    words = _words(text)
    if not words:
        return None
    compact = "".join(words)
    if compact in OPT_OUT_WORDS:
        return "out"
    if compact in OPT_IN_WORDS:
        return "in"
    keys = [w for w in words if w in OPT_OUT_WORDS or w == "OPT"]
    rest = [w for w in words if w not in OPT_OUT_WORDS and w not in _FILLERS and w not in ("OPT", "OUT")]
    if keys and not rest and len(words) <= 8:  # "stop please", "Unsubscribe me", "stop texting me", "OPT OUT"
        return "out"
    if len(words) <= 12 and _OPT_OUT_PHRASE.search(_nfkc(text)):
        return "out"
    if words[0] in OPT_IN_WORDS and all(w in _FILLERS for w in words[1:]) and len(words) <= 3:
        return "in"
    return None


# --------------------------------------------------------------------------- classification
_RULES: list[tuple[str, float, re.Pattern]] = [
    ("legal_complaint", 0.9, re.compile(
        r"\b(lawyers?|attorneys?|solicitors?|legal action|sue (you|them|your)|suing|lawsuit|court|report(ed)? (you|this|your)|"
        r"harass\w*|this is spam|spamming|scam\w*|gdpr|data protection|\bico\b|ftc|trading standards|chargeback|"
        r"refund\w*|money back|complain\w*|cease and desist)\b", re.I)),
    ("out_of_office", 0.9, re.compile(
        r"\b(out of (the )?office|ooo|on (annual )?leave|on (vacation|holiday)|away until|auto[- ]?reply|"
        r"automatic reply|currently (away|out)|limited access to (my )?email)\b", re.I)),
    ("not_interested", 0.85, re.compile(
        r"\b(not interested|no thanks|no thank you|not for (me|us)|we'?re (good|all set|fine)|i'?m (good|all set) thanks|"
        r"not a (good )?fit|i'?ll pass|pass on this|not looking|no need|don'?t need (this|it|help)|do not need)\b|"
        r"^\s*(no|nope|nah)[.!]?\s*$", re.I)),
    ("objection_partner", 0.8, re.compile(
        r"\b(my (partner|wife|husband|spouse|co-?founder|business partner|boss|accountant)|talk (to|with) my|run it by|"
        r"check with (my|the)|ask my|discuss (it |this )?with)\b", re.I)),
    ("objection_cost", 0.8, re.compile(
        r"\b(too (expensive|pricey|much money)|can'?t afford|cannot afford|no budget|out of (my|our) budget|"
        r"money'?s tight|money is tight|tight on (cash|money)|not in the budget|costs? too much)\b", re.I)),
    ("objection_not_now", 0.8, re.compile(
        r"\b(not (right )?now|maybe later|later this year|next (month|quarter|year)|after the (holidays|summer|new year)|"
        r"busy (right now|at the moment|this month)|bad time|not the right time|in a few (weeks|months)|circle back|"
        r"reach out (again )?(later|in)|bad timing|timing'?s? (isn'?t|is not|not) (right|great|good))\b", re.I)),
    ("price_ask", 0.85, re.compile(
        r"\b(how much|what'?s the (price|cost|investment)|prices?|pricing|cost|costs|what do you charge|your (rates|fees)|"
        r"fees?|investment)\b", re.I)),
    ("positive", 0.75, re.compile(
        r"\b(yes|yeah|yep|sure|interested|sounds (good|great|interesting)|let'?s (talk|chat|do it|go)|love to|"
        r"i'?d like to|keen|tell me more|count me in|absolutely|definitely|book (a|me|in)|happy to (chat|talk|jump)|"
        r"when are you free|what times|send (me )?(a|the) (link|times)|i'?m in|go ahead)\b", re.I)),
    ("question", 0.6, re.compile(r"\?|^\s*(what|how|why|when|who|where|can|could|do|does|is|are|will|would)\b", re.I)),
]
_THANKS = re.compile(r"^\s*(thanks|thank you|cheers|great|perfect|ok|okay)[.!]*\s*$", re.I)
_NEGATED = re.compile(r"\b(not|never|no longer|n't)\s+(\w+\s+)?$", re.I)
_AI_SYSTEM = ("You sort one reply from a sales prospect into exactly one label. Answer with the label only, one of: "
              + ", ".join(LABELS) + ".")


def classify_reply(text: str, tenant=None, meta: dict | None = None) -> dict:
    """Keyword rules first. Returns {label, label_text, confidence, matched, mode, unclear}. With AI allowed and
    no rule matching, the model may pick a label from LABELS only; anything else falls back to the rules."""
    meta = meta or {}
    t = _nfkc(text or "")
    if meta.get("auto_reply"):
        return _cls("out_of_office", 0.95, "auto-reply header", "rules")
    if opt_keyword(t) == "out" or _OPT_OUT_PHRASE.search(t):
        m = _OPT_OUT_PHRASE.search(t)
        return _cls("unsubscribe", 0.95, m.group(0) if m else t.strip()[:40], "rules")
    for label, conf, pat in _RULES:
        for m in pat.finditer(t):
            if label == "positive" and _NEGATED.search(t[:m.start()]):
                continue
            return _cls(label, conf, m.group(0), "rules")
    if _THANKS.match(t):
        return _cls("positive", 0.5, t.strip()[:40], "rules")
    if tenant is not None:
        ai = _ai_label(tenant, t)
        if ai:
            return ai
    return {**_cls("question", 0.3, "", "rules"), "unclear": True}


def _cls(label: str, conf: float, matched: str, mode: str) -> dict:
    return {"label": label, "label_text": LABELS[label], "confidence": conf, "matched": (matched or "")[:80],
            "mode": mode, "unclear": False}


def _ai_label(tenant, text: str) -> dict | None:
    from . import llm

    try:
        out = llm.complete_or_replay(tenant, _AI_SYSTEM, f"Reply:\n{text[:1500]}", "inbound_classify", max_tokens=12)
    except llm.AIUnavailable:
        return None
    except Exception:  # noqa: BLE001 - the rules answer stands
        return None
    words = str((out or {}).get("text") or "").strip().lower().split()
    guess = re.sub(r"[^a-z_]", "", words[0]) if words else ""  # an empty or whitespace answer is ignored
    if guess not in LABELS:
        return None
    return {**_cls(guess, 0.6, "", "ai" if out.get("mode") == "live" else "replay"), "unclear": False}


def legal_kind(text: str) -> str:
    t = _nfkc(text).lower()
    if re.search(r"\b(lawyer|attorney|solicitor|legal|sue|suing|lawsuit|court|cease and desist|gdpr|data protection|"
                 r"\bico\b|ftc|trading standards)", t):
        return "legal"
    if re.search(r"\b(refund|chargeback|money back)", t):
        return "refund"
    return "complaint"


# --------------------------------------------------------------------------- webhook idempotency
def seen(tenant, provider: str, event_id) -> bool:
    """Record a provider event id. True when it was already recorded (a retry or a replay)."""
    eid = str(event_id if event_id is not None else "").strip()[:200]
    if not eid:
        return False
    with tenant.db() as c:
        n = c.execute("INSERT OR IGNORE INTO webhook_events(provider,event_id,created) VALUES(?,?,?)",
                      (str(provider)[:40], eid, db.now())).rowcount
    return n == 0


def forget(tenant, provider: str, event_id) -> None:
    """Processing failed after seen() recorded the id: drop it so the provider's retry is handled, not lost
    (a lost STOP would be worse than a second draft a person can reject)."""
    eid = str(event_id if event_id is not None else "").strip()[:200]
    if not eid:
        return
    try:
        with tenant.db() as c:
            c.execute("DELETE FROM webhook_events WHERE provider=? AND event_id=?", (str(provider)[:40], eid))
    except Exception:  # noqa: BLE001 - never mask the original error
        log.warning("could not release a webhook event id")


# --------------------------------------------------------------------------- matching
def _cols(c, table: str) -> set[str]:
    try:
        return {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
    except Exception:  # noqa: BLE001
        return set()


def _user_party(c, u) -> dict:
    u = dict(u)
    if u["role"] == "student" and u.get("student_id"):
        s = c.execute("SELECT id, name FROM students WHERE id=?", (u["student_id"],)).fetchone()
        if s is not None:
            return {"party_type": "student", "party_id": s["id"], "owner_user_id": u["id"], "name": s["name"],
                    "user": _pub_user(u), "how": "linked account"}
    ptype = "member" if u["role"] == "student" else "staff"
    return {"party_type": ptype, "party_id": u["id"], "owner_user_id": u["id"], "name": u.get("display") or u["username"],
            "user": _pub_user(u), "how": "linked account"}


def _pub_user(u: dict) -> dict:
    return {"id": u["id"], "username": u["username"], "role": u["role"], "student_id": u.get("student_id")}


def _party_row(c, ptype: str, pid: int) -> dict | None:
    if ptype == "prospect":
        r = c.execute("SELECT * FROM prospects WHERE id=?", (pid,)).fetchone()
        if r is None:
            return None
        r = dict(r)
        return {"party_type": "prospect", "party_id": r["id"], "owner_user_id": r.get("owner_user_id"), "name": r.get("name"),
                "stage": r.get("stage"), "tier": r.get("tier") or r.get("lead_tier"),
                "dnc": bool(r.get("do_not_contact")) or r.get("stage") == "dnc",
                "email": r.get("email"), "handle": r.get("handle"), "demo": r.get("demo")}
    if ptype == "lead":
        r = c.execute("SELECT * FROM leads WHERE id=?", (pid,)).fetchone()
        if r is None:
            return None
        r = dict(r)
        return {"party_type": "lead", "party_id": r["id"], "owner_user_id": None, "name": r.get("name"), "stage": r.get("stage"),
                "tier": r.get("tier"), "dnc": bool(r.get("do_not_contact")) or r.get("stage") == "dnc", "email": r.get("email"),
                "handle": r.get("handle"), "demo": r.get("demo")}
    if ptype == "client":
        r = c.execute("SELECT * FROM clients WHERE id=?", (pid,)).fetchone()
        if r is None:
            return None
        r = dict(r)
        u = c.execute("SELECT id FROM users WHERE student_id=? AND role='student' ORDER BY id LIMIT 1",
                      (r.get("student_id"),)).fetchone() if r.get("student_id") else None
        return {"party_type": "client", "party_id": r["id"], "owner_user_id": u["id"] if u else None, "name": r.get("name"),
                "email": r.get("email") or r.get("contact_email"), "demo": r.get("demo")}
    if ptype == "student":
        r = c.execute("SELECT * FROM students WHERE id=?", (pid,)).fetchone()
        if r is None:
            return None
        u = c.execute("SELECT * FROM users WHERE student_id=? ORDER BY id LIMIT 1", (pid,)).fetchone()
        return {"party_type": "student", "party_id": r["id"], "owner_user_id": u["id"] if u else None, "name": r["name"],
                "email": r["email"], "user": _pub_user(dict(u)) if u else None, "demo": r["demo"]}
    if ptype in ("member", "staff"):
        u = c.execute("SELECT * FROM users WHERE id=?", (pid,)).fetchone()
        return _user_party(c, u) if u else None
    return None


def match_sender(tenant, channel: str, ext_id: str, meta: dict | None = None) -> dict:
    """Who sent this? Returns a party dict; party_type 'unknown' when nobody matches.
    meta.scope_user_id limits matches to that user's own prospects (a pasted-in reply)."""
    meta = meta or {}
    key = canonical(channel, ext_id)
    scope = meta.get("scope_user_id")
    unknown = {"party_type": "unknown", "party_id": None, "owner_user_id": scope, "name": meta.get("display_name") or None,
               "how": "no match"}
    with tenant.db() as c:
        # an explicit prospect chosen by the person pasting the reply (already ownership-checked)
        if meta.get("prospect_id"):
            p = _party_row(c, "prospect", int(meta["prospect_id"]))
            if p and (scope is None or p["owner_user_id"] == scope):
                return {**p, "how": "chosen"}
        # 1. a linked account (Telegram /link, Slack, SMS LINK)
        u = meta.get("user")
        if u and u.get("id"):
            r = c.execute("SELECT * FROM users WHERE id=? AND active=1", (u["id"],)).fetchone()
            if r is not None:
                return _user_party(c, r)
        if channel in ("telegram", "slack", "sms", "phone"):
            chans = ("sms", "phone") if channel in ("sms", "phone") else (channel,)
            refs = {str(ext_id), key}
            q = (f"SELECT u.* FROM channel_links l JOIN users u ON u.id=l.user_id WHERE l.channel IN "
                 f"({','.join('?' * len(chans))}) AND l.ext_id IN ({','.join('?' * len(refs))}) AND l.status='linked' "
                 f"AND u.active=1 ORDER BY l.id DESC LIMIT 1")
            r = c.execute(q, (*chans, *refs)).fetchone()
            if r is not None:
                return _user_party(c, r)
        # 2. a person linked this address before (triage)
        conv = c.execute("SELECT party_type, party_id FROM conversations WHERE channel=? AND ext_id=? AND scope=? "
                         "AND party_type IS NOT NULL AND party_type!='unknown' AND party_id IS NOT NULL",
                         (channel, key, int(scope or 0))).fetchone()
        if conv is not None:
            p = _party_row(c, conv["party_type"], conv["party_id"])
            if p and (scope is None or p.get("owner_user_id") == scope):
                return {**p, "how": "earlier triage"}
        # 3. address on a record
        is_email = "@" in key and not key.startswith("@")
        if is_email:
            domain = key.rsplit("@", 1)[-1]
            like = "%@" + ("g%mail.com" if domain == "gmail.com" else domain)
            q = "SELECT id, email FROM prospects WHERE email IS NOT NULL AND lower(email) LIKE ?"
            args: list = [like]
            if scope is not None:
                q += " AND owner_user_id=?"
                args.append(scope)
            for r in c.execute(q + " ORDER BY updated DESC, id DESC", args):
                if queue.canonical_email(r["email"]) == key:
                    return {**_party_row(c, "prospect", r["id"]), "how": "email"}
            if scope is None or meta.get("scope_leads"):
                for r in c.execute("SELECT id, email FROM leads WHERE email IS NOT NULL AND lower(email) LIKE ? ORDER BY id DESC",
                                   (like,)):
                    if queue.canonical_email(r["email"]) == key:
                        return {**_party_row(c, "lead", r["id"]), "how": "email"}
            if scope is None:
                ccols = _cols(c, "clients") & {"email", "contact_email"}
                for col in sorted(ccols):
                    for r in c.execute(f"SELECT id, {col} AS e FROM clients WHERE {col} IS NOT NULL AND lower({col}) LIKE ?",
                                       (like,)):
                        if queue.canonical_email(r["e"]) == key:
                            return {**_party_row(c, "client", r["id"]), "how": "email"}
                for r in c.execute("SELECT id, email FROM students WHERE email IS NOT NULL AND lower(email) LIKE ?", (like,)):
                    if queue.canonical_email(r["email"]) == key:
                        return {**_party_row(c, "student", r["id"]), "how": "email"}
                for r in c.execute("SELECT * FROM users WHERE lower(username) LIKE ? AND active=1", (like,)):
                    if queue.canonical_email(r["username"]) == key:
                        return {**_user_party(c, r), "how": "email"}
        elif channel in CAPTURE_CHANNELS and key:
            q = "SELECT id FROM prospects WHERE lower(ltrim(trim(handle),'@'))=?"
            args = [key]
            if scope is not None:
                q += " AND owner_user_id=?"
                args.append(scope)
            r = c.execute(q + " ORDER BY updated DESC, id DESC LIMIT 1", args).fetchone()
            if r is not None:
                return {**_party_row(c, "prospect", r["id"]), "how": "handle"}
            if scope is None or meta.get("scope_leads"):
                r = c.execute("SELECT id FROM leads WHERE lower(ltrim(trim(handle),'@'))=? ORDER BY id DESC LIMIT 1",
                              (key,)).fetchone()
                if r is not None:
                    return {**_party_row(c, "lead", r["id"]), "how": "handle"}
        # 4. the delivery feature's contact points, when that table exists
        cp = _cols(c, "contact_points")
        addr_col = next((x for x in ("address", "value", "ext_id", "handle") if x in cp), None)
        if addr_col and {"party_type", "party_id"} <= cp:
            for r in c.execute(f"SELECT party_type, party_id, {addr_col} AS a FROM contact_points WHERE {addr_col} IS NOT NULL"):
                if canonical(channel, r["a"]) == key and r["party_type"] in ("prospect", "lead", "client", "student"):
                    p = _party_row(c, r["party_type"], r["party_id"])
                    if p and (scope is None or p.get("owner_user_id") == scope):
                        return {**p, "how": "contact point"}
    return unknown


# --------------------------------------------------------------------------- conversations
def _upsert_conversation(tenant, channel: str, key: str, party: dict, display: str | None, scope: int = 0) -> int:
    """One conversation per (channel, address); a reply pasted in by a non-owner gets its own scope so two
    operators who happen to message the same handle never share a thread. The first known party sticks."""
    now = db.now()
    ptype = party["party_type"]
    with tenant.db() as c:
        r = c.execute("SELECT * FROM conversations WHERE channel=? AND ext_id=? AND scope=?", (channel, key, scope)).fetchone()
        if r is None:
            c.execute(
                "INSERT OR IGNORE INTO conversations(party_type,party_id,channel,ext_id,scope,display,owner_user_id,state,last_in,"
                "created,demo) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (ptype, party.get("party_id"), channel, key, scope, (display or "")[:120], party.get("owner_user_id"), "open",
                 now, now, 1 if tenant.is_demo else 0))
            r = c.execute("SELECT * FROM conversations WHERE channel=? AND ext_id=? AND scope=?", (channel, key, scope)).fetchone()
            return r["id"]
        upd_party = ptype != "unknown" and r["party_type"] in (None, "unknown")
        c.execute("UPDATE conversations SET last_in=?, display=COALESCE(NULLIF(?,''),display)" +
                  (", party_type=?, party_id=?, owner_user_id=?" if upd_party else "") + " WHERE id=?",
                  (now, (display or "")[:120], *((ptype, party.get("party_id"), party.get("owner_user_id")) if upd_party else ()),
                   r["id"]))
        return r["id"]


# --------------------------------------------------------------------------- the router
def route(tenant, channel: str, ext_id: str, body: str, meta: dict | None = None) -> dict:
    """Handle one inbound message. Returns {inbound_id, conversation_id, party_type, party_id, classification,
    label, queued, status, dnc, escalation_id, run_id, ...}."""
    meta = dict(meta or {})
    channel = re.sub(r"[^a-z_]", "", str(channel or "manual").lower())[:30] or "manual"
    ext_id = str(ext_id or "").strip()[:254]
    body = body if isinstance(body, str) else ""
    body = _nfkc(body).replace("\x00", "").strip()[:4000]
    if not ext_id:
        raise InboundError(422, "The message has no sender")
    if not body:
        body = str(meta.get("subject") or "").strip()[:300] or "(empty message)"
    actor = str(meta.pop("actor", "") or f"{channel}-inbound")[:80]
    out = runtime.execute(tenant, "inbound", f"Inbound {CHANNEL_LABELS.get(channel, channel)} message",
                          PLAN, lambda run: _route(tenant, channel, ext_id, body, meta, run), actor)
    return {**out["result"], "run_id": out["run_id"]}


def _route(tenant, channel: str, ext_id: str, body: str, meta: dict, run: runtime.Run) -> dict:
    key = canonical(channel, ext_id)
    party = run.step("match", {"channel": channel}, lambda: match_sender(tenant, channel, ext_id, meta), "Match the sender")
    run_party = {k: party.get(k) for k in ("party_type", "party_id", "how")}
    run.note("party", {}, run_party, label="Sender")
    display = meta.get("display_name") or ""
    conv_id = _upsert_conversation(tenant, channel, key, party, display, int(meta.get("scope_user_id") or 0))
    cls = run.step("classify", {"chars": len(body)}, lambda: classify_reply(body, tenant if party["party_type"] in (
        "prospect", "lead", "client") else None, meta), "Sort the reply")
    if cls["mode"] in ("ai", "replay"):
        run.use_ai("live" if cls["mode"] == "ai" else "replay")
    reply_ref = str(meta.get("reply_ref") or (parse_address(ext_id)[1] if channel == "email" else ext_id) or ext_id)[:254]
    with tenant.db() as c:
        inbound_id = c.execute(
            "INSERT INTO inbound_messages(conversation_id,channel,ext_id,reply_ref,provider_id,party_type,party_id,owner_user_id,"
            "subject,body,classification,confidence,class_mode,matched,status,run_id,created,demo) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (conv_id, channel, key, reply_ref, str(meta.get("provider_id") or "")[:200] or None, party["party_type"],
             party.get("party_id"), party.get("owner_user_id"), str(meta.get("subject") or "")[:300], body,
             cls["label"], cls["confidence"], cls["mode"], cls["matched"], "new", run.id, db.now(),
             1 if tenant.is_demo else 0)).lastrowid
        c.execute("UPDATE conversations SET last_class=? WHERE id=?", (cls["label"], conv_id))
        if meta.get("log_id"):
            c.execute("UPDATE channel_messages SET conversation_id=? WHERE id=?", (conv_id, meta["log_id"]))
    inbound = {"id": inbound_id, "conversation_id": conv_id, "channel": channel, "ext_id": key, "reply_ref": reply_ref,
               "body": body, "subject": str(meta.get("subject") or ""), "provider_id": meta.get("provider_id")}
    return _process(tenant, inbound, party, cls, meta, run)


def _process(tenant, inbound: dict, party: dict, cls: dict, meta: dict, run: runtime.Run) -> dict:
    """Act on a stored inbound message for a known (or unknown) party. Shared by route, triage and re-sorting."""
    ptype = party["party_type"]
    label = cls["label"]
    res = {"inbound_id": inbound["id"], "conversation_id": inbound["conversation_id"], "party_type": ptype,
           "party_id": party.get("party_id"), "classification": label, "label": LABELS[label], "class_mode": cls["mode"],
           "queued": None, "status": None, "dnc": False, "escalation_id": None}
    sales = ptype in ("prospect", "lead")

    # opt-out: stop everything, never auto-draft a reply
    if label == "unsubscribe" or meta.get("opt_out"):
        bounce = meta.get("bounce") if meta.get("bounce") in ("bounce", "complaint") else None
        d = run.step("dnc", {"party": ptype}, lambda: apply_dnc(tenant, party, inbound, reason="asked to stop",
                                                                kind=bounce or "stop"), "Stop all contact")
        res.update(dnc=True, classification="unsubscribe", label=LABELS["unsubscribe"], escalation_id=d["escalation_id"])
        if _RULES[0][2].search(inbound["body"]):  # "stop or I'll report you": a person must also see the threat
            from . import escalations

            who = party.get("name") or PARTY_LABELS.get(ptype, "Someone")
            res["escalation_id"] = escalations.raise_(
                tenant, legal_kind(inbound["body"]), "high", f"{who} asked to stop and wrote: \"{inbound['body'][:200]}\"",
                assignee_user_id=party.get("owner_user_id"), ref=f"inbound:{inbound['id']}")
            _set_inbound(tenant, inbound["id"], escalation_id=res["escalation_id"])
        _set_inbound(tenant, inbound["id"], status="handled", classification="unsubscribe")
        if not bounce:  # a bounce or complaint is not a reply from the person
            _emit_reply(tenant, party, inbound, "unsubscribe", None)
        return res

    # legal threats, refunds and complaints: a person handles these
    if label == "legal_complaint":
        from . import escalations

        kind = legal_kind(inbound["body"])
        who = party.get("name") or PARTY_LABELS.get(ptype, "Someone")
        eid = escalations.raise_(tenant, kind, "high",
                                 f"{who} ({PARTY_LABELS.get(ptype, ptype).lower()}) wrote: \"{inbound['body'][:200]}\"",
                                 assignee_user_id=party.get("owner_user_id"), ref=f"inbound:{inbound['id']}")
        item = _holding_draft(tenant, party, inbound, run, legal=True)
        res.update(escalation_id=eid, queued=item and item["id"], status=item and item["status"])
        _set_inbound(tenant, inbound["id"], status="escalated", queue_id=item and item["id"], escalation_id=eid)
        if sales or ptype == "client":
            _emit_reply(tenant, party, inbound, label, item and item["id"])
        return res

    # students, members and the team keep the cited Coach
    if ptype in ("student", "member", "staff"):
        out = _coach_reply(tenant, party, inbound, meta, run)
        res.update(out)
        _set_inbound(tenant, inbound["id"], status="drafted" if out.get("queued") else "answered", queue_id=out.get("queued"))
        return res

    # a prospect who was already won is a client now: a person answers, never the setter script
    if ptype == "prospect" and party.get("stage") == "won":
        item = None if party.get("dnc") else _holding_draft(tenant, party, inbound, run)
        res.update(queued=item and item["id"], status=item and item["status"])
        _set_inbound(tenant, inbound["id"], status="drafted" if item else "handled", queue_id=item and item["id"])
        _emit_reply(tenant, party, inbound, label, item and item["id"])
        return res

    if sales:
        new_stage = run.step("stage", {"label": label}, lambda: update_stage(tenant, party, label), "Update the pipeline")
        res["stage"] = new_stage
        item = None
        if label != "out_of_office" and not party.get("dnc"):
            item = run.step("draft", {"label": label}, lambda: draft_reply(tenant, party, inbound, cls, run), "Draft the next message")
        res.update(queued=item and item["id"], status=item and item["status"])
        _set_inbound(tenant, inbound["id"], status="auto_reply" if label == "out_of_office" else
                     ("drafted" if item else "handled"), queue_id=item and item["id"])
        run.step("notify", {}, lambda: _emit_reply(tenant, party, inbound, label, item and item["id"]), "Notify")
        return res

    if ptype == "client":
        item = _holding_draft(tenant, party, inbound, run)
        res.update(queued=item and item["id"], status=item and item["status"])
        _set_inbound(tenant, inbound["id"], status="drafted", queue_id=item and item["id"])
        _emit_reply(tenant, party, inbound, label, item and item["id"])
        return res

    # unknown sender: never course material; a neutral holding draft for a person, and a triage entry
    item = None if meta.get("no_draft") else _holding_draft(tenant, party, inbound, run)
    res.update(queued=item and item["id"], status=item and item["status"])
    _set_inbound(tenant, inbound["id"], status="triage", queue_id=item and item["id"])
    return res


def _set_inbound(tenant, iid: int, **fields) -> None:
    fields = {k: v for k, v in fields.items() if k in ("status", "queue_id", "escalation_id", "classification", "class_mode",
                                                        "party_type", "party_id", "owner_user_id", "note")}
    if "status" in fields and fields["status"] in ("handled", "dismissed", "replied"):
        fields["handled_at"] = db.now()
    if not fields:
        return
    with tenant.db() as c:
        c.execute(f"UPDATE inbound_messages SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), iid))


def _emit_reply(tenant, party: dict, inbound: dict, label: str, qid: int | None) -> None:
    if party["party_type"] not in ("prospect", "lead", "client"):
        return
    events.emit(tenant, "reply_received", {"party_type": party["party_type"], "party_id": party.get("party_id"),
                                           "owner_user_id": party.get("owner_user_id"), "classification": label,
                                           "queue_id": qid, "inbound_id": inbound["id"], "channel": inbound["channel"]})


# --------------------------------------------------------------------------- pipeline stages
PROSPECT_STAGE = {"positive": "replied", "question": "replied", "price_ask": "replied", "objection_cost": "replied",
                  "objection_partner": "replied", "objection_not_now": "nurture", "not_interested": "lost",
                  "unsubscribe": "dnc"}
_EARLY = (None, "", "new", "researched", "contacted")


def _next_stage(current: str | None, label: str) -> str | None:
    target = PROSPECT_STAGE.get(label)
    if target is None:
        return None
    if target == "dnc":
        return "dnc" if current != "dnc" else None
    if current in ("won", "dnc"):
        return None
    if target == "replied":
        return "replied" if current in _EARLY else None
    # nurture / lost: from an early stage or straight after a reply
    return target if current in _EARLY + ("replied", "nurture") and current != target else None


def update_stage(tenant, party: dict, label: str) -> str | None:
    """Move a prospect or lead forward on a reply (never backwards, never out of won). Returns the new stage."""
    ptype, pid = party["party_type"], party.get("party_id")
    if ptype not in ("prospect", "lead") or pid is None:
        return None
    with tenant.db() as c:
        r = c.execute(f"SELECT stage FROM {'prospects' if ptype == 'prospect' else 'leads'} WHERE id=?", (pid,)).fetchone()
    if r is None:
        return None
    stage = _next_stage(r["stage"], label)
    if stage is None:
        return None
    if ptype == "prospect":
        _set_prospect_stage(tenant, pid, stage, party.get("owner_user_id"))
    else:
        with tenant.db() as c:
            c.execute("UPDATE leads SET stage=? WHERE id=?", (stage, pid))
    return stage


def _set_prospect_stage(tenant, pid: int, stage: str, owner_user_id: int | None) -> None:
    """The prospects feature owns its pipeline: stage moves go through prospects.set_stage (which records the
    timeline and emits prospect_stage) and do-not-contact through prospects.set_dnc. dnc_set is emitted once, by
    apply_dnc below, so set_dnc runs with emit=False."""
    from . import prospects

    try:
        if stage == "dnc":
            prospects.set_dnc(tenant, pid, "inbound", "Asked not to be contacted (reply)", emit=False)
        else:
            prospects.set_stage(tenant, pid, stage, "inbound")
    except prospects.ProspectError as e:  # their rules refused the move (e.g. already do-not-contact): leave it
        from .redact import redact

        log.info("prospect stage move refused: %s", redact(f"{pid} -> {stage}: {e}"))


# --------------------------------------------------------------------------- do-not-contact
def apply_dnc(tenant, party: dict, inbound: dict, reason: str = "asked to stop", kind: str = "stop") -> dict:
    """STOP / unsubscribe: do-not-contact on the record, the address on the never-contact list, an SMS/Telegram
    opt-out row, open drafts to them held, dnc_set emitted and a low-priority escalation so a person knows."""
    ptype, pid = party["party_type"], party.get("party_id")
    channel, key = inbound["channel"], inbound["ext_id"]
    if ptype == "prospect" and pid:
        _set_prospect_stage(tenant, pid, "dnc", party.get("owner_user_id"))
        with tenant.db() as c:
            c.execute("UPDATE prospects SET do_not_contact=1 WHERE id=?", (pid,))
    elif ptype == "lead" and pid:
        with tenant.db() as c:
            c.execute("UPDATE leads SET do_not_contact=1, stage='dnc' WHERE id=?", (pid,))
    addrs = {key, str(inbound.get("reply_ref") or "").strip().lower()}
    for a in (party.get("email"), party.get("handle")):
        if a:
            addrs.add(queue.canonical_email(a) if "@" in a and not a.startswith("@") else str(a).strip().lower())
            if str(a).startswith("@"):
                addrs.add(str(a).strip().lower().lstrip("@"))
    addrs = {a for a in addrs if a}
    if channel in ("sms", "phone", "telegram"):
        with tenant.db() as c:
            for ch in (("sms", "phone") if channel in ("sms", "phone") else (channel,)):
                c.execute("INSERT INTO channel_links(channel,ext_id,opted_in,status,created) VALUES(?,?,0,'opted_out',?)",
                          (ch, key, db.now()))
    _block(tenant, addrs)
    _suppress_for_delivery(tenant, addrs, channel, party)
    held = _hold_open_items(tenant, party, addrs)
    with tenant.db() as c:
        c.execute("UPDATE conversations SET state='opted_out' WHERE id=?", (inbound["conversation_id"],))
    audit.log(tenant, f"{channel}-inbound", "consent", {"channel": channel, "opt_in": False, "party": ptype, "held": held})
    events.emit(tenant, "dnc_set", {"party_type": ptype, "party_id": pid, "channel": channel,
                                    "owner_user_id": party.get("owner_user_id"),
                                    **({"reason": kind} if kind != "stop" else {})})
    from . import escalations

    who = party.get("name") or PARTY_LABELS.get(ptype, "Someone")
    held_txt = ("No drafts to them were waiting." if not held else
                f"{held} waiting draft{'s were' if held > 1 else ' was'} held.")
    if kind in ("bounce", "complaint"):
        what = "reported an email as spam" if kind == "complaint" else "has an email address that bounced (hard bounce)"
        eid = escalations.raise_(tenant, "spam_complaint" if kind == "complaint" else "email_bounce",
                                 "medium" if kind == "complaint" else "low",
                                 f"{who} {what}. The address is now on do-not-contact and outreach to them has stopped. "
                                 f"{held_txt}", assignee_user_id=party.get("owner_user_id"),
                                 ref=f"{kind}:{ptype}:{pid or key}")
    else:
        eid = escalations.raise_(tenant, "dnc_request", "low",
                                 f"{who} asked not to be contacted ({CHANNEL_LABELS.get(channel, channel)}). Outreach to them "
                                 f"has stopped. {held_txt}", assignee_user_id=party.get("owner_user_id"),
                                 ref=f"dnc:{ptype}:{pid or key}")
    _set_inbound(tenant, inbound["id"], escalation_id=eid)
    return {"held": held, "blocked": len(addrs), "escalation_id": eid}


def _block(tenant, addrs: set[str]) -> None:
    cur = list(tenant.get("blocked_attendees", []) or [])
    have = {str(x).strip().lower() for x in cur}
    new = [a for a in sorted(addrs) if a not in have]
    if new:
        tenant.set("blocked_attendees", cur + new)


def _unblock(tenant, addrs: set[str]) -> None:
    cur = list(tenant.get("blocked_attendees", []) or [])
    keep = [x for x in cur if str(x).strip().lower() not in addrs]
    if len(keep) != len(cur):
        tenant.set("blocked_attendees", keep)


def _suppress_for_delivery(tenant, addrs: set[str], channel: str, party: dict) -> None:
    """Also record the opt-out on the delivery feature's suppression list when that feature is installed."""
    try:
        from . import delivery  # noqa: PLC0415 - built in parallel; may not exist yet
    except ImportError:
        return
    fn = getattr(delivery, "suppress", None)
    if fn is None:
        return
    ref = (party["party_type"], party["party_id"]) if party.get("party_id") is not None and \
        party["party_type"] in ("prospect", "lead", "client", "student") else None
    for a in sorted(addrs):
        try:
            fn(tenant, a, channel, "asked to stop (inbound reply)", ref)
        except Exception:  # noqa: BLE001 - the never-contact list above already blocks them
            log.warning("delivery suppression skipped for one address")


# Waiting, scheduled or "send it by hand" items to a person who opted out are all pulled back.
HOLD_STATUSES = ("pending", "held", "needs_human", "scheduled", "awaiting_manual_send")


def _hold_open_items(tenant, party: dict, addrs: set[str]) -> int:
    pkey = {"prospect": "prospect_id", "lead": "lead_id", "client": "client_id"}.get(party["party_type"])
    flag = {"code": "do_not_contact", "severity": "block", "message": "They asked not to be contacted", "match": ""}
    n = 0
    with tenant.db() as c:
        rows = db.rows(c.execute(f"SELECT id, recipient_ref, meta, flags FROM queue WHERE status IN "
                                 f"({','.join('?' * len(HOLD_STATUSES))})", HOLD_STATUSES))
        for r in rows:
            meta = db.jload(r["meta"], {})
            ref = str(r["recipient_ref"] or "").strip()
            hit = ref.lower() in addrs or canonical("email", ref) in addrs or canonical_phone(ref) in addrs \
                or canonical_handle(ref) in addrs
            if pkey and party.get("party_id") is not None and meta.get(pkey) == party.get("party_id"):
                hit = True
            if not hit:
                continue
            flags = db.jload(r["flags"], [])
            if not any(f.get("code") == "do_not_contact" for f in flags):
                flags.append(flag)
            c.execute("UPDATE queue SET status='held', flags=? WHERE id=?", (db.jdump(flags), r["id"]))
            n += 1
    return n


def reopt_in(tenant, channel: str, ext_id: str) -> None:
    """START / UNSTOP: the number (or chat) may get messages again. The record's do-not-contact flag stays
    until a person clears it."""
    key = canonical(channel, ext_id)
    with tenant.db() as c:
        for ch in (("sms", "phone") if channel in ("sms", "phone") else (channel,)):
            c.execute("INSERT INTO channel_links(channel,ext_id,opted_in,status,created) VALUES(?,?,1,'opted_in',?)",
                      (ch, key, db.now()))
        conv = c.execute("SELECT id, state FROM conversations WHERE channel=? AND ext_id=? AND scope=0",
                         (channel, key)).fetchone()
        if conv is not None and conv["state"] == "opted_out":
            c.execute("UPDATE conversations SET state='open' WHERE id=?", (conv["id"],))
    if conv is not None and conv["state"] == "opted_out":
        _unblock(tenant, {key, str(ext_id).strip().lower()})
    audit.log(tenant, f"{channel}:{key}", "consent", {"channel": channel, "opt_in": True})


# --------------------------------------------------------------------------- drafting
_PLAYBOOK = Path(__file__).parent / "knowledge" / "playbooks" / "sales-setting-closing.json"
PLAYBOOK_FOR = {
    "positive": "short-booking-window-pre-call-priming",
    "question": "setter-qualification-criteria",
    "price_ask": "price-presentation-and-material-terms",
    "objection_cost": "objection-handling-pause-question-isolate",
    "objection_partner": "objection-handling-pause-question-isolate",
    "objection_not_now": "follow-up-after-no-close",
    "not_interested": "follow-up-after-no-close",
}
COURSE_QUERY = {
    "positive": "booking the call after a positive reply",
    "question": "answering a prospect question in DMs",
    "price_ask": "prospect asks how much it costs before a call",
    "objection_cost": "handling the cost objection",
    "objection_partner": "handling the partner or spouse objection",
    "objection_not_now": "follow up when a prospect says not now",
    "not_interested": "prospect not interested follow up",
}
TEMPLATES = {
    "positive": "Hi {first}, thanks for getting back to me. A short call is the easiest way to see if this is a good fit. "
                "Would earlier or later in the week suit you better?",
    "question": "Hi {first}, good question, thanks for asking. The honest answer depends a little on your situation, so I'd "
                "rather not guess. Could you tell me a bit more about where things are right now? I'm also happy to explain "
                "properly on a short call if that's easier.",
    "price_ask": "Hi {first}, fair question. It depends on what you need, so I'd rather not throw out a number that doesn't "
                 "fit. A short call is the quickest way to work out whether this makes sense for you at all. Would earlier "
                 "or later in the week suit you better?",
    "objection_cost": "Hi {first}, that's completely fair, and thanks for being straight with me. It may simply not be the "
                      "right time, and that's fine. If it would help, I can share a couple of free resources you can use on "
                      "your own in the meantime. Would that be useful?",
    "objection_partner": "Hi {first}, that makes sense; it's a decision worth making together. Would it help if I sent a "
                         "short summary you can share, or set up a call you can both join?",
    "objection_not_now": "Hi {first}, no problem at all, thanks for letting me know. Would it be alright if I checked back in "
                         "with you in a couple of months?",
    "not_interested": "Hi {first}, thanks for letting me know, I appreciate the reply. I won't message you about this again. "
                      "All the best.",
}
HOLDING = "Thanks for your message. A member of the team will read it and get back to you soon."
HOLDING_LEGAL = "Thanks for your message. I've passed it to the right person, who will get back to you directly."
HOLDING_CLIENT = "Hi {first}, thanks for your message. I've seen it and will come back to you shortly."
EMAIL_FOOTER = "\n\nIf you'd rather not hear from me again, just reply STOP."
_BUSINESS_WORDS = {"physio", "dental", "clinic", "clinics", "roofing", "books", "bookkeeping", "fitness", "studio", "agency",
                   "ltd", "llc", "inc", "co", "group", "media", "spa", "solar", "law", "hvac"}


@lru_cache(maxsize=1)
def _playbook_entries() -> dict:
    try:
        data = json.loads(_PLAYBOOK.read_text())
        return {e["id"]: e for e in data.get("entries", [])}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def playbook_tip(label: str) -> dict | None:
    e = _playbook_entries().get(PLAYBOOK_FOR.get(label, ""))
    if not e:
        return None
    tip = (e.get("do") or e.get("steps") or [""])[0]
    return {"id": e["id"], "title": e.get("title", ""), "tip": tip, "avoid": (e.get("dont") or [""])[0],
            "source": "2026 playbook: appointment setting & closing (researched public sources)"}


def first_name(name: str | None) -> str:
    n = re.sub(r"\s*\(DEMO\)\s*", " ", str(name or "")).strip()
    toks = n.split()
    if len(toks) >= 2 and toks[0].isalpha() and toks[0][:1].isupper() and toks[-1].lower() not in _BUSINESS_WORDS \
            and toks[0].lower() not in _BUSINESS_WORDS:
        return toks[0]
    return "there"


def _reply_route(tenant, party: dict, inbound: dict) -> tuple[str, str]:
    """Reply on the channel they used, unless it is SMS/phone without opt-in: then email or DM when we have one."""
    ch, ref = inbound["channel"], inbound["reply_ref"]
    if ch in ("sms", "phone"):
        from . import channels

        if not channels.opted_in(tenant, ch, ref):
            if party.get("email"):
                return "email", party["email"]
            if party.get("handle"):
                return "instagram_dm", party["handle"]
    if ch == "facebook":
        return "facebook_dm", ref  # sent by a person from the page's inbox, then confirmed with "I sent it"
    if ch == "manual":
        return "manual", ref
    return ch, ref


def _subject(inbound: dict) -> str:
    s = (inbound.get("subject") or "").strip()
    if not s:
        return "Re: your message"
    return s if s.lower().startswith("re:") else f"Re: {s}"


def _citations(tenant, label: str) -> list[dict]:
    try:
        from . import retrieval

        return [retrieval.cite(h) for h in retrieval.search(tenant, COURSE_QUERY.get(label, "setter reply"), k=3)]
    except Exception:  # noqa: BLE001 - an empty or private knowledge base is fine
        return []


def _ai_body(tenant, party: dict, label: str, text: str, tip: dict | None, meta: dict) -> tuple[str | None, str]:
    from . import llm

    # complete_or_replay checks the AI budget first: with the gate closed nothing is sent anywhere
    system = ("You write one short reply (under 80 words) from a setter to a prospect who answered an outreach message. "
              "Plain, warm English. No prices, fees or money figures. No income claims, guarantees, urgency or deadlines. "
              "No names except the recipient's first name. End with one simple question.")
    prompt = (f"Their reply was sorted as: {LABELS[label]}.\nTheir message:\n{text[:800]}\n"
              f"Recipient first name: {first_name(party.get('name'))}\n"
              f"Setter guidance: {(tip or {}).get('tip', '')}\nAvoid: {(tip or {}).get('avoid', '')}")
    try:
        out = llm.complete_or_replay(tenant, system, prompt, "inbound_reply", max_tokens=220)
    except llm.AIUnavailable:
        return None, "rules"
    except Exception:  # noqa: BLE001
        return None, "rules"
    body = (out.get("text") or "").strip()
    if not body or guardrails.blocked(guardrails.check_for(tenant, body, meta)) or guardrails.ai_words(body):
        return None, "rules"
    return body[:1200], "live" if out.get("mode") == "live" else "replay"


def draft_reply(tenant, party: dict, inbound: dict, cls: dict, run: runtime.Run | None = None) -> dict | None:
    """The next setter message for a prospect or lead, as an approval-gated queue item."""
    label = cls["label"]
    if label not in TEMPLATES:
        return None
    ptype = party["party_type"]
    ch, ref = _reply_route(tenant, party, inbound)
    tip = playbook_tip(label)
    tier = (party.get("tier") or "cold")
    meta = {"prospect_facing": True, "lead_tier": tier, "prior_contact": True, "inbound_id": inbound["id"],
            "conversation_id": inbound["conversation_id"], "party_type": ptype, "party_id": party.get("party_id"),
            "classification": label, "playbook": tip, "citations": _citations(tenant, label)}
    if ptype == "prospect":
        meta["prospect_id"] = party.get("party_id")
        meta["owner_user_id"] = party.get("owner_user_id")
        kind = "prospect_email" if ch == "email" else "prospect_dm"
    else:
        meta["lead_id"] = party.get("party_id")
        kind = "setter_reply"
    if ch == "email":
        meta["subject"] = _subject(inbound)
    body, mode = _ai_body(tenant, party, label, inbound["body"], tip, meta)
    if body is None:
        body = TEMPLATES[label].format(first=first_name(party.get("name")))
        meta["source"] = "Template (AI off or not used)"
    else:
        meta["source"] = "AI draft" if mode == "live" else "Replayed AI draft"
        if run is not None:
            run.use_ai(mode)
    if kind == "prospect_email" and "STOP" not in body:
        body += EMAIL_FOOTER
    if inbound.get("provider_id"):
        meta["in_reply_to"] = str(inbound["provider_id"])[:200]
    who = party.get("name") or PARTY_LABELS[ptype]
    item = queue.add(tenant, kind, body, channel=ch, recipient_ref=ref, title=f"Reply to {who}: {LABELS[label].lower()}",
                     meta=meta, run_id=run.id if run else None, actor="inbound", demo=tenant.is_demo)
    if cls.get("unclear") and item["status"] == "pending":
        _needs_human(tenant, item)
    return item


def _needs_human(tenant, item: dict) -> None:
    with tenant.db() as c:
        c.execute("UPDATE queue SET status='needs_human' WHERE id=? AND status='pending'", (item["id"],))
    item["status"] = "needs_human" if item["status"] == "pending" else item["status"]


def _open_holding(tenant, conversation_id: int, legal: bool) -> dict | None:
    """The holding reply already waiting for this conversation, so a sender who writes five times gets one
    draft, not five (a legal holding line is never replaced by a plain one)."""
    with tenant.db() as c:
        r = c.execute("SELECT id FROM queue WHERE status IN ('pending','held','needs_human') AND "
                      "json_extract(meta,'$.holding')=1 AND json_extract(meta,'$.conversation_id')=? AND "
                      "COALESCE(json_extract(meta,'$.legal'),0)>=? ORDER BY id DESC LIMIT 1",
                      (conversation_id, 1 if legal else 0)).fetchone()
    return queue.get(tenant, r["id"], scrub=False) if r is not None else None


def _holding_draft(tenant, party: dict, inbound: dict, run: runtime.Run | None, legal: bool = False) -> dict | None:
    """A neutral line for a person to review. Never course material."""
    ptype = party["party_type"]
    ch, ref = _reply_route(tenant, party, inbound)
    if ch in ("sms", "phone"):
        from . import channels

        if not channels.opted_in(tenant, ch, ref):
            return None  # we may not text them; the inbox entry is enough
    existing = _open_holding(tenant, inbound["conversation_id"], legal)
    if existing is not None:
        return existing
    client_like = ptype == "client" or (ptype == "prospect" and party.get("stage") == "won")
    if legal:
        body = HOLDING_LEGAL
    elif client_like:
        body = HOLDING_CLIENT.format(first=first_name(party.get("name")))
    else:
        body = HOLDING
    meta = {"needs_human": True, "holding": True, "legal": bool(legal), "inbound_id": inbound["id"],
            "conversation_id": inbound["conversation_id"], "party_type": ptype, "party_id": party.get("party_id"),
            "prior_contact": True, "source": "Holding reply (no AI, no course material)"}
    if party.get("owner_user_id") is not None:
        meta["owner_user_id"] = party["owner_user_id"]
    if ptype in ("prospect", "lead", "unknown") and not client_like:
        # a stranger or a prospect gets the strictest (outreach) rules, so an edit can't add a price or a figure
        meta.update(prospect_facing=True, lead_tier=party.get("tier") or "cold")
    if ptype in ("prospect", "lead"):
        meta["prospect_id" if ptype == "prospect" else "lead_id"] = party.get("party_id")
    if ptype == "client":
        meta["client_id"] = party.get("party_id")
    if ch == "email":
        meta["subject"] = _subject(inbound)
    who = party.get("name") or inbound.get("ext_id") or "unknown sender"
    item = queue.add(tenant, "channel_reply", body, channel=ch, recipient_ref=ref,
                     title=f"{'Holding reply' if not legal else 'Escalated'}: {who}", meta=meta,
                     run_id=run.id if run else None, actor="inbound", demo=tenant.is_demo)
    _needs_human(tenant, item)
    return item


def _coach_reply(tenant, party: dict, inbound: dict, meta: dict, run: runtime.Run) -> dict:
    """Students, members and the team: the cited Coach answer, drafted for approval (or spoken on the voice line).
    Channel replies and the phone line are outbound messages, so they are never persona-styled (persona.neutral)."""
    from . import persona
    from .agents import coach

    user = party.get("user")
    with persona.neutral():
        res = coach.answer(tenant, inbound["body"][:1000], user=user, record=True, run=run)
    if meta.get("voice"):
        return {"answer": res["answer"], "covered": res["covered"], "spoken": True}
    cites = "; ".join(f"[{c['id']}] {c['title']}" for c in res["citations"][:3])
    # surfaces without citation chips get a plain key for any external playbook [X#] marker the answer uses
    refs = coach.external_refs(res["answer"], res.get("external", []), tenant)
    ch, ref = inbound["channel"], inbound["reply_ref"]
    qmeta = {"question": inbound["body"][:300], "covered": res["covered"], "run_id": run.id, "inbound_id": inbound["id"],
             "conversation_id": inbound["conversation_id"], "party_type": party["party_type"], "party_id": party.get("party_id")}
    if ch == "email":
        item = queue.add(tenant, "email_reply", res["answer"] + (f"\n\n{refs}" if refs else ""), channel="email", recipient_ref=ref,
                         title=f"Email reply to {party.get('name') or ref}",
                         meta={**qmeta, "subject": _subject(inbound), "prior_contact": True}, run_id=run.id,
                         actor="email-inbound", demo=tenant.is_demo)
    else:
        body = res["answer"] + (f"\nSources: {cites}" if cites else "")
        body += f"\n\n{refs}" if refs else ""
        item = queue.add(tenant, "channel_reply", body, channel=ch, recipient_ref=meta.get("reply_ref") or ref,
                         title=f"{CHANNEL_LABELS.get(ch, ch)} reply to {party.get('name') or ref}", meta=qmeta,
                         run_id=run.id, actor=f"{ch}-bot", demo=tenant.is_demo)
    return {"queued": item["id"], "status": item["status"], "covered": res["covered"]}


# --------------------------------------------------------------------------- sent hook: keep conversations current
@queue.on_sent("*")
def _on_sent(tenant, item: dict, actor: str) -> None:
    meta = item.get("meta") or {}
    if not meta.get("conversation_id"):
        return
    with tenant.db() as c:
        c.execute("UPDATE conversations SET last_out=? WHERE id=?", (db.now(), meta["conversation_id"]))
        if meta.get("inbound_id"):
            # the message this draft answered, and any later ones that share the same (holding) draft
            c.execute("UPDATE inbound_messages SET status='replied', handled_at=? WHERE (id=? OR queue_id=?) AND status IN "
                      "('new','drafted','triage','escalated')", (db.now(), meta["inbound_id"], item.get("id")))


@queue.on_approve("*")
def _on_approved(tenant, item: dict, actor: str) -> None:
    """An approved answer to a reply closes its speed-to-lead escalation straight away."""
    iid = (item.get("meta") or {}).get("inbound_id")
    if iid:
        from . import escalations

        escalations.auto_resolve(tenant, "speed_to_lead", f"inbound:{iid}", "An answer was approved")


# --------------------------------------------------------------------------- fast-ack job (Slack)
@jobs.handler("inbound_route")
def _route_job(tenant, payload: dict) -> dict:
    res = route(tenant, payload.get("channel", "manual"), payload.get("ext_id", ""), payload.get("body", ""),
                payload.get("meta") or {})
    return {"inbound_id": res.get("inbound_id"), "queued": res.get("queued")}


# --------------------------------------------------------------------------- SLA checks (scheduler tick)
# An answer counts once a person approved it (sent, scheduled or waiting to be sent by hand). Rejected,
# cancelled or failed sends do not.
ANSWERED = ("approved", "scheduled", "awaiting_manual_send", "sending", "sent")


def _responded(c, r) -> bool:
    ph = ",".join("?" * len(ANSWERED))
    if r["queue_id"]:
        q = c.execute("SELECT status FROM queue WHERE id=?", (r["queue_id"],)).fetchone()
        if q is not None and q["status"] in ANSWERED:
            return True
    q = c.execute(f"SELECT 1 FROM queue WHERE json_extract(meta,'$.conversation_id')=? AND status IN ({ph}) "
                  "AND COALESCE(decided_at,0)>=? LIMIT 1", (r["conversation_id"], *ANSWERED, r["created"])).fetchone()
    return q is not None


def check_slas(tenant) -> dict:
    """Speed-to-lead (a positive reply with no approved answer within 60 minutes), drafts held by guardrails for
    over 24 hours, and background work that failed after its retries. Each becomes one escalation; escalations
    whose cause went away are closed."""
    from . import escalations

    now = db.now()
    out = {"speed_to_lead": 0, "held_item": 0, "step_failed": 0, "resolved": 0, "runs_interrupted": 0}
    # a person who resolved one of these decided it; the next tick must not raise it (or alert) again
    closed = {k: escalations.closed_by_person(tenant, k) for k in ("speed_to_lead", "held_item", "step_failed")}
    with tenant.db() as c:
        rows = db.rows(c.execute(
            "SELECT i.*, cv.display FROM inbound_messages i LEFT JOIN conversations cv ON cv.id=i.conversation_id "
            "WHERE i.classification='positive' AND i.party_type IN ('prospect','lead') AND i.created<=? AND i.created>=? "
            "AND i.status NOT IN ('dismissed')", (now - SLA_SECONDS, now - 7 * 86400)))
        answered = {r["id"]: _responded(c, r) for r in rows}
        names = {}
        for r in rows:
            p = _party_row(c, r["party_type"], r["party_id"]) if r["party_id"] else None
            names[r["id"]] = (p or {}).get("name") or r.get("display") or "A prospect"
    for r in rows:
        ref = f"inbound:{r['id']}"
        if answered[r["id"]]:
            out["resolved"] += escalations.auto_resolve(tenant, "speed_to_lead", ref, "An answer was approved")
            continue
        if ref in closed["speed_to_lead"]:
            continue
        mins = int((now - r["created"]) // 60)
        eid = escalations.raise_(tenant, "speed_to_lead", "high",
                                 f"{names[r['id']]} replied positively {mins} minutes ago and has no approved answer yet.",
                                 assignee_user_id=r["owner_user_id"], ref=ref)
        if not r["escalation_id"]:
            _set_inbound(tenant, r["id"], escalation_id=eid)
        out["speed_to_lead"] += 1
    with tenant.db() as c:
        held = db.rows(c.execute("SELECT id, title, meta, created FROM queue WHERE status='held' AND created<=? AND created>=?",
                                 (now - HELD_SECONDS, now - 30 * 86400)))
        open_held = db.rows(c.execute("SELECT e.id, e.ref, q.status FROM escalations e LEFT JOIN queue q "
                                      "ON 'queue:' || q.id = e.ref WHERE e.kind='held_item' AND e.status IN ('open','acked')"))
        failed = db.rows(c.execute("SELECT id, kind, attempts FROM jobs WHERE status='failed' AND updated>=?", (now - 7 * 86400,)))
        c.execute("DELETE FROM webhook_events WHERE created<?", (now - 30 * 86400,))
    for q in held:
        if f"queue:{q['id']}" in closed["held_item"]:
            continue
        hours = int((now - q["created"]) // 3600)
        escalations.raise_(tenant, "held_item", "medium",
                           f"\"{(q['title'] or 'A draft')[:80]}\" has been held by guardrails for {hours} hours. "
                           "Edit it clean or reject it.", assignee_user_id=db.jload(q["meta"], {}).get("owner_user_id"),
                           ref=f"queue:{q['id']}")
        out["held_item"] += 1
    for e in open_held:
        if e["status"] != "held":
            out["resolved"] += escalations.auto_resolve(tenant, "held_item", e["ref"], "The draft is no longer held")
    for j in failed:
        if f"job:{j['id']}" in closed["step_failed"]:
            continue
        escalations.raise_(tenant, "step_failed", "medium",
                           f"A background task ({j['kind']}) failed after {j['attempts']} tries.", ref=f"job:{j['id']}")
        out["step_failed"] += 1
    # runs that were still "running" when this process started cannot be alive (the run pool is in memory);
    # the startup check only catches the ones already ten minutes old, this catches the rest once they are
    boot = _process_started()
    out["runs_interrupted"] = escalations.recover_runs(tenant, started_before=boot) if boot else 0
    return out


def _process_started() -> float | None:
    try:
        from .app import STARTED  # noqa: PLC0415 - the web process's start time

        return float(STARTED)
    except Exception:  # noqa: BLE001
        return None


check_slas.demo_too = True


# --------------------------------------------------------------------------- inbox views
# Visibility is per message: each inbound row keeps the owner it was matched to, so two operators who
# contact the same person never see each other's threads. Owners see everything; a coach also sees
# their students' rows.
def _visible_ids(tenant, user: dict) -> set[int] | None:
    from . import escalations

    return escalations.visible_user_ids(tenant, user)


def _owner_clause(ids: set[int] | None, col: str = "i.owner_user_id") -> tuple[str, list]:
    if ids is None:
        return "", []
    return f" AND {col} IN ({','.join('?' * len(ids))})", sorted(ids)


def _group_key(r: dict) -> str:
    if r.get("party_type") not in (None, "unknown") and r.get("party_id") is not None:
        return f"{r['party_type']}:{r['party_id']}"
    return f"unknown:{r['conversation_id']}"


def _rows_for_key(c, key: str, ids: set[int] | None) -> list[dict]:
    ptype, _, pid = key.partition(":")
    if not pid.isdigit():
        return []
    oc, oargs = _owner_clause(ids)
    base = ("SELECT i.*, cv.display, cv.state, cv.last_out FROM inbound_messages i JOIN conversations cv "
            "ON cv.id=i.conversation_id WHERE ")
    if ptype == "unknown":
        return db.rows(c.execute(base + "i.conversation_id=? AND (i.party_type IS NULL OR i.party_type='unknown')" + oc
                                 + " ORDER BY i.id", (int(pid), *oargs)))
    if ptype not in PARTY_LABELS:
        return []
    return db.rows(c.execute(base + "i.party_type=? AND i.party_id=?" + oc + " ORDER BY i.id", (ptype, int(pid), *oargs)))


def inbox(tenant, user: dict, view: str = "all", limit: int = 100) -> dict:
    """Conversations grouped by the person they are with, newest first, with the drafted next message."""
    ids = _visible_ids(tenant, user)
    oc, oargs = _owner_clause(ids)
    with tenant.db() as c:
        rows = db.rows(c.execute(
            "SELECT i.*, cv.display, cv.state, cv.last_out, cv.ext_id AS address FROM inbound_messages i "
            "JOIN conversations cv ON cv.id=i.conversation_id WHERE 1=1" + oc + " ORDER BY i.id DESC LIMIT 5000", oargs))
        groups: dict[str, dict] = {}
        for r in rows:
            k = _group_key(r)
            g = groups.get(k)
            if g is None:
                known = not k.startswith("unknown:")
                p = _party_row(c, r["party_type"], r["party_id"]) if known else None
                g = groups[k] = {
                    "key": k, "party_type": r["party_type"] if known else "unknown",
                    "party_label": PARTY_LABELS.get(r["party_type"] if known else "unknown", "Contact"),
                    "party_id": r["party_id"] if known else None, "conversation_id": r["conversation_id"],
                    "name": (p or {}).get("name") or r["display"] or r["address"], "stage": (p or {}).get("stage"),
                    "dnc": bool((p or {}).get("dnc")), "owner_user_id": r["owner_user_id"], "channels": [],
                    "last_in": r["created"], "last_out": r["last_out"], "inbound_ids": [], "escalation_ids": [],
                    "last_message": {"id": r["id"], "body": r["body"][:240], "created": r["created"],
                                     "channel": r["channel"], "classification": r["classification"],
                                     "label": LABELS.get(r["classification"], ""), "class_mode": r["class_mode"],
                                     "status": r["status"]},
                    "demo": bool(r["demo"]),
                }
            g["inbound_ids"].append(r["id"])
            if r["escalation_id"]:
                g["escalation_ids"].append(r["escalation_id"])
            if r["state"] == "opted_out":
                g["dnc"] = True
            ch = {"channel": r["channel"], "label": CHANNEL_LABELS.get(r["channel"], r["channel"]), "address": r["address"]}
            if ch not in g["channels"]:
                g["channels"].append(ch)
        out = []
        for g in groups.values():
            iids = g.pop("inbound_ids")
            eids = g.pop("escalation_ids")
            ph = ",".join("?" * len(iids))
            draft = db.row(c.execute(
                f"SELECT id, kind, status, body, title, channel, flags FROM queue WHERE json_extract(meta,'$.inbound_id') IN ({ph}) "
                "AND status IN ('pending','held','needs_human') ORDER BY id DESC LIMIT 1", iids).fetchone())
            if draft:
                draft["flags"] = db.jload(draft["flags"], [])
                draft["body"] = draft["body"][:600]
            n_esc = c.execute(f"SELECT COUNT(*) FROM escalations WHERE id IN ({','.join('?' * len(eids))}) "
                              "AND status IN ('open','acked')", eids).fetchone()[0] if eids else 0
            g.update(draft=draft, open_escalations=n_esc, messages=len(iids),
                     needs_reply=bool(draft) or g["last_message"]["status"] in ("new", "triage", "escalated"))
            out.append(g)
    counts = {"conversations": len(out), "needs_reply": sum(1 for g in out if g["needs_reply"]),
              "triage": sum(1 for g in out if g["party_type"] == "unknown"),
              "escalated": sum(1 for g in out if g["open_escalations"]), "opted_out": sum(1 for g in out if g["dnc"])}
    if view == "needs_reply":
        out = [g for g in out if g["needs_reply"]]
    elif view == "triage":
        out = [g for g in out if g["party_type"] == "unknown"]
    elif view == "escalated":
        out = [g for g in out if g["open_escalations"]]
    out.sort(key=lambda g: -(g["last_in"] or 0))
    return {"items": out[:limit], "view": view, "counts": counts}


def thread(tenant, user: dict, key: str) -> dict:
    """Every message with this person that the viewer may see, in and out, oldest first."""
    ids = _visible_ids(tenant, user)
    with tenant.db() as c:
        ins = _rows_for_key(c, key, ids)
        if not ins:
            raise InboundError(404, "Not found")
        known = not key.startswith("unknown:")
        party = _party_row(c, ins[0]["party_type"], ins[0]["party_id"]) if known else None
        iids = [m["id"] for m in ins]
        ph = ",".join("?" * len(iids))
        outs = db.rows(c.execute(f"SELECT id, kind, channel, recipient_ref, status, body, title, meta, flags, created, decided_at, "
                                 f"delivered FROM queue WHERE json_extract(meta,'$.inbound_id') IN ({ph}) ORDER BY id", iids))
        eids = [m["escalation_id"] for m in ins if m["escalation_id"]]
        escs = db.rows(c.execute(f"SELECT * FROM escalations WHERE id IN ({','.join('?' * len(eids))}) ORDER BY id DESC",
                                 eids)) if eids else []
        conv_ids = sorted({m["conversation_id"] for m in ins})
        convs = db.rows(c.execute(f"SELECT id, channel, ext_id, state, display FROM conversations WHERE id IN "
                                  f"({','.join('?' * len(conv_ids))})", conv_ids))
    names = guardrails.tenant_banned_names(tenant)
    traces = user["role"] in ("owner", "coach")  # runs started by a webhook are not the viewer's own runs
    msgs = [{"direction": "in", "id": m["id"], "channel": m["channel"], "channel_label": CHANNEL_LABELS.get(m["channel"], m["channel"]),
             "body": guardrails.scrub(m["body"], names), "subject": m["subject"], "classification": m["classification"],
             "label": LABELS.get(m["classification"], ""), "class_mode": m["class_mode"], "status": m["status"],
             "created": m["created"], "queue_id": m["queue_id"], "run_id": m["run_id"] if traces else None} for m in ins]
    for o in outs:
        meta = db.jload(o["meta"], {})
        msgs.append({"direction": "out", "id": o["id"], "channel": o["channel"],
                     "channel_label": CHANNEL_LABELS.get(o["channel"], o["channel"]), "kind": o["kind"],
                     "body": guardrails.scrub(o["body"], names), "status": o["status"], "title": o["title"],
                     "flags": db.jload(o["flags"], []), "created": o["created"], "decided_at": o["decided_at"],
                     "source": meta.get("source"), "playbook": meta.get("playbook"), "citations": meta.get("citations") or [],
                     "owner_user_id": meta.get("owner_user_id"), "inbound_id": meta.get("inbound_id"),
                     "holding": bool(meta.get("holding")),
                     # the viewer may approve, edit or reject it (a coach reading a student's thread may not)
                     "can_decide": auth.can_decide(user["role"], o["kind"], meta.get("owner_user_id"), user["id"])})
    msgs.sort(key=lambda m: (m["created"] or 0, 0 if m["direction"] == "in" else 1))
    from . import escalations as _e

    ptype = ins[0]["party_type"] if known else "unknown"
    return {"key": key, "party_type": ptype, "party_label": PARTY_LABELS.get(ptype, "Contact"),
            "name": (party or {}).get("name") or convs[0]["display"] or convs[0]["ext_id"],
            "stage": (party or {}).get("stage"),
            "dnc": bool((party or {}).get("dnc")) or any(x["state"] == "opted_out" for x in convs),
            "channels": [{"channel": x["channel"], "label": CHANNEL_LABELS.get(x["channel"], x["channel"]), "address": x["ext_id"],
                          "state": x["state"]} for x in convs],
            "messages": msgs, "escalations": [_e._public(e) for e in escs], "latest_inbound_id": ins[-1]["id"]}


def _load_inbound(tenant, user: dict, inbound_id: int) -> dict:
    ids = _visible_ids(tenant, user)
    with tenant.db() as c:
        m = db.row(c.execute("SELECT * FROM inbound_messages WHERE id=?", (inbound_id,)).fetchone())
    if m is None or (ids is not None and (m["owner_user_id"] is None or int(m["owner_user_id"]) not in ids)):
        raise InboundError(404, "Not found")
    return m


def _inbound_dict(m: dict) -> dict:
    return {"id": m["id"], "conversation_id": m["conversation_id"], "channel": m["channel"], "ext_id": m["ext_id"],
            "reply_ref": m["reply_ref"] or m["ext_id"], "body": m["body"], "subject": m["subject"] or "",
            "provider_id": m["provider_id"]}


def _retire_draft(tenant, m: dict, user: dict, actor: str, reason: str) -> None:
    if not m.get("queue_id"):
        return
    item = queue.get(tenant, m["queue_id"], scrub=False)
    if item and item["status"] in queue.OPEN:
        try:
            queue.reject(tenant, item["id"], actor, user["role"], reason, user_id=user["id"])
        except queue.QueueError:
            pass


def assign(tenant, user: dict, inbound_id: int, party_type: str, party_id: int, actor: str) -> dict:
    """Triage (owner): say who an unknown sender is. Their later messages from that address match too."""
    m = _load_inbound(tenant, user, inbound_id)
    if m["party_type"] not in (None, "unknown"):
        raise InboundError(409, "This message is already linked to someone")
    with tenant.db() as c:
        party = _party_row(c, party_type, party_id) if party_type in ("prospect", "lead", "client", "student") else None
    if party is None:
        raise InboundError(404, "That record doesn't exist")
    with tenant.db() as c:
        c.execute("UPDATE conversations SET party_type=?, party_id=?, owner_user_id=? WHERE id=?",
                  (party["party_type"], party["party_id"], party.get("owner_user_id"), m["conversation_id"]))
        c.execute("UPDATE inbound_messages SET party_type=?, party_id=?, owner_user_id=? WHERE conversation_id=? "
                  "AND (party_type IS NULL OR party_type='unknown')",
                  (party["party_type"], party["party_id"], party.get("owner_user_id"), m["conversation_id"]))
    _retire_draft(tenant, m, user, actor, "Replaced after triage")
    audit.log(tenant, actor, "action", {"inbox_assign": inbound_id, "party": party["party_type"]})
    cls = classify_reply(m["body"], tenant if party["party_type"] in ("prospect", "lead", "client") else None)
    out = runtime.execute(tenant, "inbound", "Inbound message linked by a person", PLAN[1:],
                          lambda run: _process(tenant, _inbound_dict(m), party, cls, {}, run), actor)
    return {**out["result"], "run_id": out["run_id"]}


def reclassify(tenant, user: dict, inbound_id: int, label: str, actor: str) -> dict:
    """A person corrects the sorting; the pipeline stage and the draft follow."""
    if label not in LABELS:
        raise InboundError(422, "Unknown label")
    m = _load_inbound(tenant, user, inbound_id)
    with tenant.db() as c:
        party = _party_row(c, m["party_type"], m["party_id"]) if m["party_type"] not in (None, "unknown") \
            and m["party_id"] is not None else None
        cv = db.row(c.execute("SELECT display FROM conversations WHERE id=?", (m["conversation_id"],)).fetchone()) or {}
        c.execute("UPDATE inbound_messages SET classification=?, class_mode='person' WHERE id=?", (label, inbound_id))
        c.execute("UPDATE conversations SET last_class=? WHERE id=?", (label, m["conversation_id"]))
    party = party or {"party_type": "unknown", "party_id": None, "owner_user_id": m["owner_user_id"], "name": cv.get("display")}
    _retire_draft(tenant, m, user, actor, "Re-sorted by a person")
    audit.log(tenant, actor, "action", {"inbox_reclassify": inbound_id, "label": label})
    cls = _cls(label, 1.0, "set by a person", "person")
    out = runtime.execute(tenant, "inbound", "Inbound message re-sorted by a person", PLAN[1:],
                          lambda run: _process(tenant, _inbound_dict(m), party, cls, {}, run), actor)
    return {**out["result"], "run_id": out["run_id"]}


def dismiss(tenant, user: dict, inbound_id: int, actor: str) -> dict:
    m = _load_inbound(tenant, user, inbound_id)
    _retire_draft(tenant, m, user, actor, "Dismissed from the inbox")
    _set_inbound(tenant, inbound_id, status="dismissed")
    audit.log(tenant, actor, "action", {"inbox_dismiss": inbound_id})
    return {"ok": True, "id": inbound_id, "status": "dismissed"}


def capture(tenant, user: dict, channel: str, handle: str, body: str, prospect_id: int | None, actor: str) -> dict:
    """Paste a reply that arrived where there is no inbox API (Instagram, LinkedIn ...)."""
    if channel not in CAPTURE_CHANNELS:
        raise InboundError(422, "Unknown channel")
    if not canonical_handle(handle):
        raise InboundError(422, "Add their handle or profile link")
    owner = user["role"] == "owner"
    if prospect_id is not None:
        with tenant.db() as c:
            p = c.execute("SELECT id, owner_user_id FROM prospects WHERE id=?", (prospect_id,)).fetchone()
        if p is None or (not owner and p["owner_user_id"] != user["id"]):
            raise InboundError(404, "Prospect not found")
    meta = {"actor": actor, "prospect_id": prospect_id, "scope_user_id": None if owner else user["id"],
            "scope_leads": user["role"] in ("owner", "setter", "closer"), "display_name": handle.strip()[:80],
            "captured_by": user["id"]}
    return route(tenant, channel, handle, body, meta)


def parties(tenant, q: str, limit: int = 20) -> list[dict]:
    """Owner triage search across prospects, leads, students and clients."""
    like = f"%{q.strip().lower()}%"
    out = []
    with tenant.db() as c:
        for ptype, table, cols in (("prospect", "prospects", ("name", "email", "handle")), ("lead", "leads", ("name", "email", "handle")),
                                   ("student", "students", ("name", "email")), ("client", "clients", ("name",))):
            where = " OR ".join(f"lower(COALESCE({x},'')) LIKE ?" for x in cols)
            for r in c.execute(f"SELECT id, name FROM {table} WHERE {where} ORDER BY id DESC LIMIT ?", (*([like] * len(cols)), limit)):
                out.append({"party_type": ptype, "party_label": PARTY_LABELS[ptype], "party_id": r["id"], "name": r["name"]})
    return out[:limit]


# --------------------------------------------------------------------------- demo data
def seed_demo(tenant) -> dict:
    """Fictional inbound replies for the pitch demo (demo database only)."""
    from . import tenancy

    if not tenant.is_demo or not tenancy.is_demo_db(tenant):
        return {"seeded": 0}
    with tenant.db() as c:
        leads = db.rows(c.execute("SELECT id, name, email, handle FROM leads WHERE demo=1 AND email LIKE '%@example.invalid' "
                                  "ORDER BY id LIMIT 6"))
        # a student without a sign-in, so the demo student's own daily question count is untouched
        student = db.row(c.execute("SELECT s.id, s.name, s.email FROM students s WHERE s.demo=1 AND NOT EXISTS "
                                   "(SELECT 1 FROM users u WHERE u.student_id=s.id) ORDER BY s.id LIMIT 1").fetchone())
        prospect = db.row(c.execute("SELECT id, name, email, handle FROM prospects WHERE demo=1 AND owner_user_id IS NOT NULL "
                                    "AND COALESCE(do_not_contact,0)=0 ORDER BY id LIMIT 1").fetchone())
    msgs: list[tuple] = []
    if len(leads) >= 4:
        msgs += [
            ("email", leads[0]["email"], "Yes, sounds good. When are you free for a quick call? (DEMO)", "Re: quick question", 7200),
            ("instagram_dm", leads[1]["handle"], "How much is it? (DEMO)", "", 1800),
            ("email", leads[2]["email"], "Not right now, maybe after the holidays. (DEMO)", "Re: hello", 900),
            ("email", leads[3]["email"], "Please unsubscribe me. (DEMO)", "Re: hello", 600),
        ]
    if prospect and (prospect.get("email") or prospect.get("handle")):
        ch, addr = ("email", prospect["email"]) if prospect.get("email") else ("instagram_dm", prospect["handle"])
        msgs.append((ch, addr, "Sounds interesting, tell me more. (DEMO)", "Re: your clinic", 300))
    msgs += [
        ("email", "New Enquiry (DEMO) <new.enquiry@example.invalid>", "Hi, I saw your post. What do you do exactly? (DEMO)",
         "Question", 1200),
        ("email", "Unhappy Visitor (DEMO) <unhappy.visitor@example.invalid>",
         "Stop emailing me or I will report this as spam. (DEMO)", "Re: hello", 3000),
        ("email", "Refund Request (DEMO) <refund.request@example.invalid>",
         "I want a refund, this isn't what I signed up for. (DEMO)", "Refund", 2400),
    ]
    if student and student.get("email"):
        msgs.append(("email", student["email"], "What are the rules for a Thunder Dome test?", "Thunder Dome question", 400))
    n = 0
    for i, (ch, addr, body, subject, ago) in enumerate(msgs):
        name, _ = parse_address(addr) if ch == "email" else ("", "")
        try:
            res = route(tenant, ch, addr, body, {"subject": subject, "provider_id": f"demo-{i}", "display_name": name,
                                                 "actor": "demo-seed"})
        except Exception:  # noqa: BLE001 - a demo seed must never break the demo reset
            log.warning("inbound demo seed message %s failed", i)
            continue
        with tenant.db() as c:
            t = db.now() - ago
            c.execute("UPDATE inbound_messages SET created=? WHERE id=?", (t, res["inbound_id"]))
            c.execute("UPDATE conversations SET last_in=?, created=MIN(created, ?) WHERE id=?", (t, t, res["conversation_id"]))
        n += 1
    check_slas(tenant)
    return {"seeded": n}
