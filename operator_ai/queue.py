"""Approval queue. Every outbound item (DM, email, SMS, post, calendar invite, ad change,
CRM stage move ...) lands here. Guardrails run when an item is queued, again when it is
approved (after edits) and again right before a scheduled item goes out.

Delivery truth (what the status means):

    pending | held | needs_human      open: waiting for a person (held = guardrails block it)
        --approve-->  sending          claimed atomically; only one approval can ever win (a send interrupted by a
                                       crash becomes send_failed after 15 minutes, never retried by itself)
            scheduled                  approved, meta.send_after is in the future; the dispatcher sends it
            awaiting_manual_send       approved on a channel the app cannot send on (Instagram/LinkedIn DMs,
                                       phone, or any channel without a live sender in a real workspace):
                                       a person sends it and confirms with "I sent it"
            sent                       really went out: a live provider accepted it, a person confirmed it,
                                       or (demo workspace only) the mock transport took it
            send_failed                the live send or the action failed after approval (with a reason)
            approved                   approved, nothing to send (internal notes, assets, applied actions)
        --reject-->   rejected         with a reason code
    scheduled | awaiting_manual_send | send_failed  --cancel-->  cancelled (with a reason)

Mock transports run ONLY in demo workspaces. Nothing reaches a real person unless a per-tenant
server-only live switch is on for that channel AND the item was approved by a person."""
from __future__ import annotations

import difflib
import logging
import math
import re
import time

from . import audit, auth, db, guardrails

log = logging.getLogger("operator.queue")

OPEN = ("pending", "held", "needs_human")
OUTBOX = ("scheduled", "awaiting_manual_send")
# needs_attention: a send was interrupted mid-way (the server stopped while it was 'sending'), so nobody knows
# whether it went out. A person checks and confirms it was sent, sends it again, or cancels it; never re-sent blindly.
STATUSES = OPEN + ("approved", "rejected") + OUTBOX + ("sending", "sent", "send_failed", "cancelled", "needs_attention")
CANCELLABLE = OUTBOX + ("send_failed", "needs_attention")

# channel -> mock transport label. Used ONLY in demo workspaces ("mock:*" writes to channel_messages only).
MOCK_CHANNELS = {
    "instagram_dm": "mock:instagram", "email": "mock:email", "sms": "mock:sms", "telegram": "mock:telegram",
    "slack": "mock:slack", "calendar": "mock:calendar", "ads": "mock:ads", "crm": "mock:crm", "web": "mock:web",
    "phone": "mock:phone",
}
# Channels no API can send on: a person always sends these from their own account and confirms.
MANUAL_CHANNELS = {"instagram_dm", "linkedin", "phone", "whatsapp", "facebook_dm", "tiktok_dm", "x_dm"}
MESSAGE_CHANNELS = MANUAL_CHANNELS | {"email", "sms", "telegram", "slack", "calendar"}
# Approval itself is the effect (an action hook runs, or the item is an internal asset/note).
INTERNAL_CHANNELS = {"manual", "ads", "crm"}
REJECT_CODES = ("tone", "wrong_fact", "too_long", "off_brand", "compliance", "timing", "wrong_person", "other")
# Block codes that mean "never send this to this person", not "edit the wording".
_STOP_CODES = {"never_invite", "unsubscribed", "do_not_contact"}


class QueueError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


class HookFailed(Exception):
    """Raised by an on_approve hook whose effect IS the delivery (an ad change, a CRM move) when it
    failed. The item is marked send_failed with this message."""

    def __init__(self, msg: str, status: int = 409):
        super().__init__(msg)
        self.status = status


# --------------------------------------------------------------------------- addresses
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_HANDLE = re.compile(r"^@?([A-Za-z0-9._]{1,64})$")
_PROFILE_URL = re.compile(r"(?:instagram\.com|linkedin\.com/in|tiktok\.com/@?|x\.com|twitter\.com)/([A-Za-z0-9._\-]{1,64})", re.I)


def canonical_email(addr: str) -> str:
    a = (addr or "").strip().lower()
    if "@" not in a:
        return a
    local, _, domain = a.rpartition("@")
    if domain in ("gmail.com", "googlemail.com"):
        local = local.split("+", 1)[0].replace(".", "")
        domain = "gmail.com"
    else:
        local = local.split("+", 1)[0]
    return f"{local}@{domain}"


def phone_digits(s: str) -> str | None:
    """Digits of a phone number written with + - ( ) . or spaces; None if it isn't one."""
    raw = (s or "").strip()
    if not raw or not re.fullmatch(r"\+?[\d\s\-().]{7,24}", raw):
        return None
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("00"):
        digits = digits[2:]
    return digits if 7 <= len(digits) <= 15 else None


def e164(s: str) -> str | None:
    """E.164 form ("+15550142") of a number written with a country code; None otherwise."""
    raw = (s or "").strip()
    d = phone_digits(raw)
    if d is None or not (raw.startswith("+") or raw.startswith("00")) or len(d) < 8:
        return None
    return "+" + d


def address_keys(s: str) -> set[str]:
    """Comparable keys for an address: canonical emails, phone digits, social handles, and the raw
    lower-cased string. Used for the never-invite list and the suppression list."""
    s = (s or "").strip()
    if not s:
        return set()
    emails = {"email:" + canonical_email(m) for m in _EMAIL.findall(s)}
    if emails:
        return emails
    keys = {"raw:" + s.lower()}
    d = phone_digits(s)
    if d:
        keys.add("tel:" + d)
        return keys
    m = _PROFILE_URL.search(s)
    if m:
        keys.add("h:" + m.group(1).lower().lstrip("@"))
        return keys
    m = _HANDLE.match(s)
    if m:
        keys.add("h:" + m.group(1).lower())
    return keys


def blocked_list(tenant) -> set[str]:
    out: set[str] = set()
    for x in tenant.get("blocked_attendees", []) or []:
        out |= address_keys(str(x))
    return out


def blocked_recipients(tenant, recipient_ref: str | None, meta: dict | None, extra: list[str] | None = None) -> list[str]:
    """Matches against the never-invite list: emails (Gmail canonicalised), @handles and phone numbers."""
    bl = blocked_list(tenant)
    if not bl:
        return []
    cands: set[str] = set()
    for s in [recipient_ref or ""] + [str(x) for x in (meta or {}).get("attendees", []) or []] + list(extra or []):
        cands |= address_keys(s)
    return sorted(cands & bl)


# --------------------------------------------------------------------------- core
def _status_for(flags: list[dict]) -> str:
    if guardrails.blocked(flags):
        return "held"
    if guardrails.needs_review(flags):
        return "needs_human"
    return "pending"


def _num(v) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None


def _wants_footer(channel: str, meta: dict) -> bool:
    return channel == "email" and bool(meta.get("prospect_facing")) and meta.get("compliance_footer", True) is not False


def _check(tenant, title: str, body: str, meta: dict, recipient_ref: str | None, channel: str = "manual") -> list[dict]:
    from . import delivery

    text = f"Subject: {meta['subject']}\n{body}" if meta.get("subject") else body
    flags = guardrails.check_for(tenant, text, {**meta, "_channel": channel})
    if title and meta.get("check_title", True):
        flags += [f for f in guardrails.check_for(tenant, title, {**meta, "prospect_facing": False}) if f["code"] == "banned_name"]
    address = delivery.resolve_address(tenant, channel, recipient_ref, meta)
    if blocked_recipients(tenant, recipient_ref, meta, [address] if address else None):
        flags.append({"code": "never_invite", "severity": "block", "message": "Recipient is on the never-invite list",
                      "match": "[blocked attendee]"})
    party = delivery.party_of(meta)
    if party and delivery.is_dnc(tenant, party):
        flags.append({"code": "do_not_contact", "severity": "block",
                      "message": "This contact is marked do-not-contact", "match": ""})
    if channel in MESSAGE_CHANNELS and delivery.suppressed(tenant, [recipient_ref or "", address or ""]):
        flags.append({"code": "unsubscribed", "severity": "block",
                      "message": "This person unsubscribed or asked not to be contacted", "match": ""})
    if channel in ("sms", "phone"):
        if not delivery.opted_in(tenant, channel, recipient_ref or "", meta):
            flags.append({"code": "not_opted_in", "severity": "block",
                          "message": "SMS and phone need the recipient's opt-in first (they reply START)", "match": ""})
    missing = delivery.missing_live_address(tenant, channel, recipient_ref, meta)
    if missing:
        flags.append({"code": "no_address", "severity": "block", "message": missing, "match": ""})
    return guardrails._dedupe(flags)


def add(tenant, kind: str, body: str, *, channel: str = "manual", recipient_ref: str | None = None, title: str = "",
        meta: dict | None = None, run_id: int | None = None, actor: str = "system", demo: bool = False) -> dict:
    meta = dict(meta or {})
    channel = channel or "manual"
    if _wants_footer(channel, meta):
        from . import delivery

        body = delivery.with_footer(tenant, body, meta)
    flags = _check(tenant, title, body, meta, recipient_ref, channel)
    status = _status_for(flags)
    names = guardrails.tenant_banned_names(tenant)
    with tenant.db() as c:
        qid = c.execute(
            "INSERT INTO queue(kind,channel,recipient_ref,title,body,meta,status,flags,created,run_id,demo,send_after) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (kind, channel, recipient_ref, guardrails.scrub(title, names), body, db.jdump(meta), status, db.jdump(flags),
             db.now(), run_id, 1 if demo or tenant.is_demo else 0, _num(meta.get("send_after"))),
        ).lastrowid
    audit.log(tenant, actor, "queue.add", {"id": qid, "kind": kind, "channel": channel, "status": status})
    if status == "held":
        audit.log(tenant, actor, "guardrail.block", {"id": qid, "codes": [f["code"] for f in flags if f["severity"] == "block"]})
    return get(tenant, qid)


def _decorate(tenant, r: dict, names: list[str]) -> dict:
    r["meta"] = db.jload(r["meta"], {})
    r["flags"] = db.jload(r["flags"], [])
    r["body_display"] = guardrails.scrub(r["body"], names)
    path = r["meta"].get("unsubscribe_path")
    if path and "{unsubscribe_link}" in r["body_display"]:
        from . import delivery

        r["send_text"] = r["body_display"].replace("{unsubscribe_link}", delivery.public_base() + path)
    else:
        r["send_text"] = r["body_display"]
    return r


def get(tenant, qid: int, scrub: bool = True) -> dict | None:
    with tenant.db() as c:
        r = db.row(c.execute("SELECT * FROM queue WHERE id=?", (qid,)).fetchone())
    if r is None:
        return None
    if scrub:
        return _decorate(tenant, r, guardrails.tenant_banned_names(tenant))
    r["meta"] = db.jload(r["meta"], {})
    r["flags"] = db.jload(r["flags"], [])
    return r


def _scope_sql(kinds: set[str] | None, own_user_id: int | None) -> tuple[str, list] | None:
    """WHERE fragment for what a non-owner may see: kinds their role decides plus their own OWN_DECIDE items,
    never an item another person owns (meta.owner_user_id). None = nothing visible."""
    if kinds is None:
        return "", []
    own = (f"(json_extract(meta,'$.owner_user_id')=? AND kind IN ({','.join('?' * len(auth.OWN_DECIDE))}))"
           if own_user_id is not None else None)
    if not kinds and own is None:
        return None
    parts, pargs = [], []
    if kinds:
        parts.append(f"kind IN ({','.join('?' * len(kinds))})")
        pargs += sorted(kinds)
    if own:
        parts.append(own)
        pargs += [own_user_id, *sorted(auth.OWN_DECIDE)]
    q = " AND (" + " OR ".join(parts) + ")"
    # a draft someone wrote for their OWN prospect/client is theirs: other staff never see it by role
    if own_user_id is not None:
        q += " AND (json_extract(meta,'$.owner_user_id') IS NULL OR json_extract(meta,'$.owner_user_id')=?)"
        pargs.append(own_user_id)
    else:
        q += " AND json_extract(meta,'$.owner_user_id') IS NULL"
    return q, pargs


def list_items(tenant, status: str | None = None, kind: str | None = None, run_id: int | None = None,
               chain_id: str | None = None, ids: list[int] | None = None, kinds: set[str] | None = None,
               limit: int = 200, tag: str | None = None, own_user_id: int | None = None) -> list[dict]:
    q = "SELECT * FROM queue WHERE 1=1"
    args: list = []
    if status == "open":
        q += " AND status IN ('pending','held','needs_human')"
    elif status == "outbox":
        q += " AND status IN ('scheduled','awaiting_manual_send')"
    elif status:
        q += " AND status=?"
        args.append(status)
    if kind:
        q += " AND kind=?"
        args.append(kind)
    if run_id:
        q += " AND run_id=?"
        args.append(run_id)
    if chain_id:
        q += " AND json_extract(meta,'$.chain_id')=?"
        args.append(chain_id)
    if tag:
        q += " AND json_extract(meta,'$.tag')=?"
        args.append(tag)
    if ids:
        q += f" AND id IN ({','.join('?' * len(ids))})"
        args += ids
    scope = _scope_sql(kinds, own_user_id)
    if scope is None:
        return []
    q += scope[0]
    args += scope[1]
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    names = guardrails.tenant_banned_names(tenant)
    with tenant.db() as c:
        out = db.rows(c.execute(q, args))
    return [_decorate(tenant, r, names) for r in out]


def _foreign(role: str, item: dict, user_id: int | None) -> bool:
    """An item another person drafted for their own prospect/client: only they (and the owner) may see it."""
    owner = (item.get("meta") or {}).get("owner_user_id")
    return role != "owner" and owner is not None and owner != user_id


def _may(role: str, item: dict, user_id: int | None) -> bool:
    if _foreign(role, item, user_id):
        return False
    return auth.can_decide(role, item["kind"], item["meta"].get("owner_user_id"), user_id)


def visible(role: str, item: dict | None, user_id: int | None) -> bool:
    """Whether this user may see this item at all (the same rule list_items applies)."""
    if item is None:
        return False
    if role == "owner":
        return True
    if _foreign(role, item, user_id):
        return False
    own = item["meta"].get("owner_user_id") == user_id and item["kind"] in auth.OWN_DECIDE
    return own or item["kind"] in (decidable_kinds(role) or set())


# --------------------------------------------------------------------------- atomic state changes
def _claim(tenant, qid: int, from_statuses: tuple[str, ...] | list[str], to: str = "sending") -> bool:
    """Move an item from one of from_statuses to `to` in a single UPDATE. Exactly one caller wins."""
    marks = ",".join("?" * len(from_statuses))
    with tenant.db() as c:
        n = c.execute(f"UPDATE queue SET status=?, claimed_at=? WHERE id=? AND status IN ({marks})",
                      (to, db.now(), qid, *from_statuses)).rowcount
    return n == 1


STUCK_AFTER = 15 * 60  # a send that is still 'sending' after this long was interrupted (the server stopped mid-send)


def recover_stuck(tenant, now: float | None = None) -> list[int]:
    """Items left in 'sending' by a crash mid-send (claimed_at older than STUCK_AFTER) become needs_attention: a
    person checks whether it went out, then marks it sent, sends it again or cancels it. Never retried automatically:
    a duplicate message is worse than a missed one."""
    now = now or db.now()
    with tenant.db() as c:
        ids = [r["id"] for r in c.execute("SELECT id FROM queue WHERE status='sending' AND claimed_at IS NOT NULL "
                                          "AND claimed_at < ?", (now - STUCK_AFTER,))]
    out = []
    for qid in ids:
        if _set(tenant, qid, only_if="sending", status="needs_attention",
                status_reason="Sending was interrupted, so it may or may not have gone out. Check first: if it went out, "
                              "mark it sent; if not, send it again or cancel it."):
            audit.log(tenant, "scheduler", "queue.needs_attention", {"id": qid, "at": "recover_stuck"})
            out.append(qid)
    return out


def _set(tenant, qid: int, only_if: str | None = None, **fields) -> bool:
    if not fields:
        return False
    cols = ", ".join(f"{k}=?" for k in fields)
    q = f"UPDATE queue SET {cols} WHERE id=?"
    args = [db.jdump(v) if k in ("meta", "flags") and not isinstance(v, str) else v for k, v in fields.items()] + [qid]
    if only_if:
        q += " AND status=?"
        args.append(only_if)
    with tenant.db() as c:
        return c.execute(q, args).rowcount == 1


def _restore(tenant, qid: int, prev: str) -> None:
    """Undo a claim that did not finish (the item is open again and can be retried)."""
    _set(tenant, qid, only_if="sending", status=prev)


_LABEL = {"pending": "waiting for approval", "held": "held by guardrails", "needs_human": "waiting for a person",
          "approved": "approved", "rejected": "rejected", "scheduled": "scheduled", "sending": "being sent right now",
          "awaiting_manual_send": "waiting for someone to send it", "sent": "sent", "send_failed": "marked as failed",
          "cancelled": "cancelled", "needs_attention": "waiting for someone to check whether it went out"}


def _already(item: dict) -> QueueError:
    return QueueError(409, f"Item is already {item['status']}" if item["status"] in ("approved", "rejected")
                      else f"This item is {_LABEL.get(item['status'], item['status'])}")


def _lookup(tenant, qid: int, role: str, user_id: int | None, verb: str) -> dict:
    item = get(tenant, qid, scrub=False)
    if item is None or _foreign(role, item, user_id):
        raise QueueError(404, "Not found")
    if not _may(role, item, user_id):
        raise QueueError(403, f"Your role can't {verb} this kind of item")
    return item


def edit(tenant, qid: int, body: str, actor: str, role: str = "owner", meta_patch: dict | None = None,
         user_id: int | None = None) -> dict:
    item = get(tenant, qid, scrub=False)
    if item is None or _foreign(role, item, user_id):
        raise QueueError(404, "Not found")
    if item["status"] not in OPEN:
        raise _already(item)
    if not _may(role, item, user_id):
        raise QueueError(403, "Your role can't edit this item")
    meta = {**item["meta"], **(meta_patch or {})}
    if _wants_footer(item["channel"] or "manual", meta):
        from . import delivery

        body = delivery.with_footer(tenant, body, meta)
    flags = _check(tenant, item["title"] or "", body, meta, item["recipient_ref"], item["channel"] or "manual")
    status = _status_for(flags)
    with tenant.db() as c:
        # original_body keeps the first draft (SET expressions read the old row), for the learning loop
        n = c.execute("UPDATE queue SET original_body=COALESCE(original_body, body), body=?, flags=?, status=?, meta=?, "
                      "send_after=? WHERE id=? AND status IN ('pending','held','needs_human')",
                      (body, db.jdump(flags), status, db.jdump(meta), _num(meta.get("send_after")), qid)).rowcount
    if n != 1:
        raise QueueError(409, "Someone else decided this item while you were editing it")
    audit.log(tenant, actor, "queue.edit", {"id": qid, "status": status})
    return get(tenant, qid)


def approve(tenant, qid: int, actor: str, role: str = "owner", user_id: int | None = None) -> dict:
    item = _lookup(tenant, qid, role, user_id, "approve")
    if item["status"] not in OPEN:
        raise _already(item)
    prev = item["status"]
    if not _claim(tenant, qid, OPEN):
        raise QueueError(409, "Someone else is deciding this item right now")
    from . import delivery

    try:
        item = get(tenant, qid, scrub=False)  # exactly what was claimed: later edits are refused
        # guardrails again, on the current (possibly edited) body
        flags = _check(tenant, item["title"] or "", item["body"], item["meta"], item["recipient_ref"], item["channel"] or "manual")
        if guardrails.blocked(flags):
            _set(tenant, qid, status="held", flags=flags)
            audit.log(tenant, actor, "guardrail.block", {"id": qid, "at": "approve",
                                                         "codes": [f["code"] for f in flags if f["severity"] == "block"]})
            raise QueueError(409, "Blocked by guardrails. Edit it clean before approving.")
        stale = delivery.before_send(tenant, item, actor)
        if stale:
            raise QueueError(409, stale)
        now = db.now()
        sa = _num(item["meta"].get("send_after"))
        if sa and sa > now + 60:
            out = {"status": "scheduled", "send_after": sa, "reason": None}
        else:
            out = _route(tenant, item, actor)
    except BaseException:
        _restore(tenant, qid, prev)
        raise
    status = out["status"]
    fields = {"status": status, "flags": flags, "decided_by": actor, "decided_at": now, "delivered": out.get("delivered"),
              "status_reason": out.get("reason"), "send_after": out.get("send_after") or sa}
    if status == "sent":
        fields["sent_at"] = now
    _set(tenant, qid, **fields)
    _record_edit_feedback(tenant, item, actor)
    audit.log(tenant, actor, "queue.approve", {"id": qid, "kind": item["kind"], "status": status,
                                               "delivered": out.get("delivered")})
    done = get(tenant, qid, scrub=False)
    try:
        _after_approve(tenant, done, actor)
    except HookFailed as e:
        _set(tenant, qid, status="send_failed", status_reason=str(e)[:500])
        audit.log(tenant, actor, "queue.send_failed", {"id": qid, "kind": item["kind"], "at": "approve"})
        raise QueueError(e.status, str(e))
    except Exception as e:  # noqa: BLE001 - see below
        from .redact import redact

        log.warning("on_approve hook failed for queue item %s: %s", qid, redact(f"{type(e).__name__}: {e}")[:300])
        why = redact(f"Approved, but a follow-on step failed ({type(e).__name__}). It has been logged.")
        if status == "approved" and (item["channel"] or "manual") in ("ads", "crm"):
            # the hook IS the effect (an ad change, a CRM move): it did not happen
            _set(tenant, qid, status="send_failed", status_reason=why[:500])
            audit.log(tenant, actor, "queue.send_failed", {"id": qid, "kind": item["kind"], "at": "approve"})
            raise QueueError(502, why)
        # the message itself was sent / scheduled / handed to a person: a follow-on error must not undo that
        _set(tenant, qid, status_reason=why[:500])
        audit.log(tenant, actor, "queue.hook_failed", {"id": qid, "kind": item["kind"], "at": "approve"})
    if status == "sent":
        _after_sent(tenant, get(tenant, qid, scrub=False), actor)
    return get(tenant, qid)


def reject(tenant, qid: int, actor: str, role: str = "owner", reason: str = "", user_id: int | None = None,
           reason_code: str | None = None) -> dict:
    code = reason_code or "other"
    if code not in REJECT_CODES:
        raise QueueError(422, f"Unknown reason. Pick one of: {', '.join(REJECT_CODES)}")
    item = _lookup(tenant, qid, role, user_id, "reject")
    if item["status"] not in OPEN:
        raise _already(item)
    marks = ",".join("?" * len(OPEN))
    with tenant.db() as c:
        n = c.execute(f"UPDATE queue SET status='rejected', decided_by=?, decided_at=?, reject_code=?, status_reason=? "
                      f"WHERE id=? AND status IN ({marks})",
                      (actor, db.now(), code, (reason or "")[:500] or None, qid, *OPEN)).rowcount
    if n != 1:
        raise QueueError(409, "Someone else decided this item first")
    _feedback(tenant, item, actor, code, reason, None)
    audit.log(tenant, actor, "queue.reject", {"id": qid, "reason_code": code, "reason": (reason or "")[:200]})
    _run_hooks(_REJECT_HOOKS, tenant, get(tenant, qid, scrub=False), actor)
    return get(tenant, qid)


# --------------------------------------------------------------------------- after approval
def mark_sent(tenant, qid: int, actor: str, role: str = "owner", user_id: int | None = None,
              permalink: str | None = None) -> dict:
    """A person confirms they sent an approved manual-channel item from their own account, or that an interrupted
    send (needs_attention) did go out."""
    item = _lookup(tenant, qid, role, user_id, "confirm")
    if item["status"] not in ("awaiting_manual_send", "needs_attention"):
        raise QueueError(409, "Only approved messages waiting for you to send can be marked as sent")
    if not _claim(tenant, qid, (item["status"],)):
        raise QueueError(409, "Someone else just updated this item")
    meta = dict(item["meta"])
    if permalink:
        meta["permalink"] = permalink[:500]
    now = db.now()
    ch = item["channel"] or "manual"
    with tenant.db() as c:
        c.execute("INSERT INTO channel_messages(channel,direction,ext_id,body,meta,created) VALUES(?,?,?,?,?,?)",
                  (ch, "out", item["recipient_ref"], item["body"][:4000],
                   db.jdump({"queue_id": qid, "manual": True, "confirmed_by": actor}), now))
    _set(tenant, qid, status="sent", sent_at=now, delivered=f"manual:{ch}", meta=meta,
         status_reason=f"Sent by hand and confirmed by {actor}")
    audit.log(tenant, actor, "queue.sent", {"id": qid, "kind": item["kind"], "channel": ch, "manual": True})
    _after_sent(tenant, get(tenant, qid, scrub=False), actor)
    return get(tenant, qid)


def cancel(tenant, qid: int, actor: str, role: str = "owner", user_id: int | None = None, reason: str = "") -> dict:
    item = _lookup(tenant, qid, role, user_id, "cancel")
    if item["status"] not in CANCELLABLE:
        raise QueueError(409, "Only scheduled, waiting or failed items can be cancelled")
    why = (reason or "").strip()[:300] or f"Cancelled by {actor}"
    if not _claim(tenant, qid, CANCELLABLE, "cancelled"):
        raise QueueError(409, "Someone else just updated this item")
    _set(tenant, qid, status_reason=why)
    audit.log(tenant, actor, "queue.cancel", {"id": qid, "kind": item["kind"], "reason": why[:200]})
    _run_hooks(_CANCEL_HOOKS, tenant, get(tenant, qid, scrub=False), actor)
    return get(tenant, qid)


def cancel_system(tenant, qid: int, reason: str, actor: str = "system",
                  from_statuses: tuple[str, ...] = CANCELLABLE) -> bool:
    """Cancel without a person (a booking was cancelled, the contact unsubscribed, they replied ...)."""
    if not _claim(tenant, qid, from_statuses, "cancelled"):
        return False
    _set(tenant, qid, status_reason=reason[:300])
    audit.log(tenant, actor, "queue.cancel", {"id": qid, "reason": reason[:200]})
    _run_hooks(_CANCEL_HOOKS, tenant, get(tenant, qid, scrub=False), actor)
    return True


def send_now(tenant, qid: int, actor: str, role: str = "owner", user_id: int | None = None) -> dict:
    """Send a scheduled item now, or try a failed send again. The same checks as the dispatcher run."""
    item = _lookup(tenant, qid, role, user_id, "send")
    if item["status"] not in ("scheduled", "send_failed", "needs_attention"):
        raise QueueError(409, "Only scheduled or failed items can be sent now")
    if item["status"] in ("send_failed", "needs_attention") and (item["channel"] or "manual") in INTERNAL_CHANNELS:
        raise QueueError(409, "A change that failed part-way can't be retried here. Roll it back from its page and "
                              "propose it again.")
    out = dispatch(tenant, qid, actor, force=True, from_statuses=(item["status"],))
    if out is None:
        raise QueueError(409, "Someone else just updated this item")
    return get(tenant, qid)


def dispatch(tenant, qid: int, actor: str = "scheduler", *, force: bool = False,
             from_statuses: tuple[str, ...] = ("scheduled",), now: float | None = None) -> dict | None:
    """Deliver one approved, scheduled item: claim it, re-run guardrails and the never-invite check,
    evaluate its cancel_if predicates, then send, hand to a person, or cancel with a reason."""
    from . import delivery

    now = now or db.now()
    if not _claim(tenant, qid, from_statuses):
        return None
    prev = from_statuses[0] if len(from_statuses) == 1 else "scheduled"
    try:
        item = get(tenant, qid, scrub=False)
        if not force and (_num(item.get("send_after")) or 0) > now:
            _restore(tenant, qid, prev)
            return get(tenant, qid)
        flags = _check(tenant, item["title"] or "", item["body"], item["meta"], item["recipient_ref"], item["channel"] or "manual")
        codes = {f["code"] for f in flags if f["severity"] == "block"}
        if codes & _STOP_CODES:
            why = {"never_invite": "The recipient is on the never-invite list, so it was not sent.",
                   "unsubscribed": "They unsubscribed, so it was not sent.",
                   "do_not_contact": "This contact is marked do-not-contact, so it was not sent."}[sorted(codes & _STOP_CODES)[0]]
            _set(tenant, qid, status="cancelled", flags=flags, status_reason=why)
            audit.log(tenant, actor, "queue.cancel", {"id": qid, "reason": why, "at": "dispatch"})
            _run_hooks(_CANCEL_HOOKS, tenant, get(tenant, qid, scrub=False), actor)
            return get(tenant, qid)
        if codes:
            _set(tenant, qid, status="held", flags=flags,
                 status_reason="Guardrails flagged it after approval. Edit it and approve it again.")
            audit.log(tenant, actor, "guardrail.block", {"id": qid, "at": "dispatch", "codes": sorted(codes)})
            return get(tenant, qid)
        reason = delivery.cancel_reason(tenant, item, now)
        if reason:
            _set(tenant, qid, status="cancelled", status_reason=reason)
            audit.log(tenant, actor, "queue.cancel", {"id": qid, "reason": reason, "at": "dispatch"})
            _run_hooks(_CANCEL_HOOKS, tenant, get(tenant, qid, scrub=False), actor)
            return get(tenant, qid)
        stale = delivery.before_send(tenant, item, actor)
        if stale:
            return get(tenant, qid)
        try:
            out = _route(tenant, item, actor)
        except Exception as e:  # noqa: BLE001 - never left 'sending' or retried blindly: a person decides
            if isinstance(e, QueueError):
                why = str(e)
            else:
                from .redact import redact

                log.warning("dispatch failed for queue item %s: %s", qid, redact(f"{type(e).__name__}: {e}")[:300])
                why = redact(f"Sending failed ({type(e).__name__}); nothing was marked sent. Try again or cancel it.")
            _set(tenant, qid, status="send_failed", status_reason=why[:500])
            audit.log(tenant, actor, "queue.send_failed", {"id": qid, "kind": item["kind"], "at": "dispatch"})
            return get(tenant, qid)
    except BaseException:
        _restore(tenant, qid, prev)
        raise
    fields = {"status": out["status"], "delivered": out.get("delivered"), "status_reason": out.get("reason"), "flags": flags}
    if out.get("send_after"):
        fields["send_after"] = out["send_after"]
    if out["status"] == "sent":
        fields["sent_at"] = now
    _set(tenant, qid, **fields)
    audit.log(tenant, actor, "queue.dispatch", {"id": qid, "kind": item["kind"], "status": out["status"]})
    if out["status"] == "sent":
        _after_sent(tenant, get(tenant, qid, scrub=False), actor)
    return get(tenant, qid)


# --------------------------------------------------------------------------- delivery
def _route(tenant, item: dict, actor: str) -> dict:
    """Decide how an approved item leaves, and send it if a live or (demo only) mock transport can.
    Returns {"status", "delivered"?, "reason"?, "send_after"?}. A failed live send raises QueueError(502)."""
    from . import delivery

    ch = item["channel"] or "manual"
    meta = item["meta"]
    if ch == "calendar" and meta.get("live") and tenant.get("live_calendar_enabled", False):
        from .live import chain
        from .redact import redact

        try:
            return {"status": "sent", "delivered": chain.deliver_calendar_invite(tenant, item)}
        except Exception as e:  # noqa: BLE001 - nothing is marked sent; the reason is redacted
            log.warning("live calendar invite failed for queue item %s: %s", item["id"], redact(type(e).__name__))
            raise QueueError(502, redact(f"The calendar invite could not be created ({type(e).__name__}); nothing was "
                                         "sent. Check the Google calendar connection and try again."))
    if (ch == "email" and not tenant.is_demo and delivery._is_prospect_email(item)
            and delivery._is_local(delivery.public_base())):
        # live OR copied by hand, the unsubscribe link in a prospect email must work for the person who gets it
        raise QueueError(409, "Prospect emails need the server's public web address so the unsubscribe link works. "
                              "Ask whoever runs the server to set OPERATOR_PUBLIC_URL.")
    if ch in INTERNAL_CHANNELS:
        return {"status": "approved", "delivered": "action" if ch in ("ads", "crm") else "ready to send manually"}
    if ch in MANUAL_CHANNELS:
        return {"status": "awaiting_manual_send", "reason": delivery.manual_reason(tenant, item)}
    from .live import senders

    route_ch, address = delivery.route_for(tenant, ch, item["recipient_ref"], meta)
    if senders.ready(tenant, route_ch):
        if not address:
            raise QueueError(409, delivery.no_address_message(ch))
        return delivery.send_live(tenant, item, route_ch, address, actor)
    if tenant.is_demo and ch in MOCK_CHANNELS:
        body = delivery.final_body(tenant, item)
        with tenant.db() as c:
            c.execute("INSERT INTO channel_messages(channel,direction,ext_id,body,meta,created) VALUES(?,?,?,?,?,?)",
                      (ch, "out", item["recipient_ref"], body, db.jdump({"queue_id": item["id"], "mock": True}), db.now()))
        return {"status": "sent", "delivered": MOCK_CHANNELS[ch]}
    if ch in MESSAGE_CHANNELS or ch in MOCK_CHANNELS:
        return {"status": "awaiting_manual_send", "reason": delivery.manual_reason(tenant, item)}
    return {"status": "approved", "delivered": "ready to send manually"}


def deliver(tenant, item: dict, actor: str) -> str:
    """Back-compat helper: route an item and return the delivery label (no status change)."""
    if blocked_recipients(tenant, item["recipient_ref"], item["meta"]):
        raise QueueError(409, "Recipient is on the never-invite list")
    out = _route(tenant, item, actor)
    return out.get("delivered") or out["status"]


# --------------------------------------------------------------------------- learning signal
def _diff(a: str, b: str) -> str:
    d = difflib.unified_diff((a or "").splitlines(), (b or "").splitlines(), "draft", "approved", lineterm="", n=1)
    return "\n".join(d)[:4000]


def _feedback(tenant, item: dict, actor: str, code: str, reason: str | None, diff: str | None) -> None:
    meta = item.get("meta") or {}
    with tenant.db() as c:
        c.execute("INSERT INTO draft_feedback(queue_id,kind,template_key,persona,reason_code,reason,edit_diff,actor,created) "
                  "VALUES(?,?,?,?,?,?,?,?,?)",
                  (item["id"], item["kind"], str(meta.get("template_key") or meta.get("opener_move") or item["kind"])[:80],
                   str(meta.get("persona") or "")[:80], code, (reason or "")[:500] or None, diff, actor, db.now()))


def _record_edit_feedback(tenant, item: dict, actor: str) -> None:
    orig = item.get("original_body")
    if orig is not None and orig != item["body"]:
        _feedback(tenant, item, actor, "edited", None, _diff(orig, item["body"]))


def feedback(tenant, limit: int = 100) -> list[dict]:
    with tenant.db() as c:
        return db.rows(c.execute("SELECT * FROM draft_feedback ORDER BY id DESC LIMIT ?", (limit,)))


# --------------------------------------------------------------------------- hooks
_HOOKS: dict[str, list] = {}
_SENT_HOOKS: dict[str, list] = {}
_REJECT_HOOKS: dict[str, list] = {}
_CANCEL_HOOKS: dict[str, list] = {}


def _register(reg: dict, kind: str):
    def deco(fn):
        reg.setdefault(kind, []).append(fn)
        return fn

    return deco


def on_approve(kind: str):
    """Runs once when a person approves an item (whatever happens to delivery next)."""
    return _register(_HOOKS, kind)


def on_sent(kind: str):
    """Runs when an item has really gone out (live provider accepted it, a mock transport took it in a demo,
    or a person confirmed a manual send). Use this, not on_approve, for anything that counts sends."""
    return _register(_SENT_HOOKS, kind)


def on_reject(kind: str):
    """Runs when a person rejects an item (item['reject_code'] says why)."""
    return _register(_REJECT_HOOKS, kind)


def on_cancel(kind: str):
    """Runs when an approved item is cancelled before it went out (by a person or by a cancel_if rule)."""
    return _register(_CANCEL_HOOKS, kind)


def _after_approve(tenant, item: dict, actor: str) -> None:
    from . import handlers

    handlers.load()
    for fn in _HOOKS.get(item["kind"], []) + _HOOKS.get("*", []):
        fn(tenant, item, actor)


def _run_hooks(reg: dict, tenant, item: dict | None, actor: str) -> None:
    if item is None:
        return
    from . import handlers

    handlers.load()
    for fn in reg.get(item["kind"], []) + reg.get("*", []):
        try:
            fn(tenant, item, actor)
        except Exception:  # noqa: BLE001 - a follow-on hook must not undo a decision a person already made
            from .redact import redact

            log.exception("queue hook failed: %s", redact(getattr(fn, "__name__", fn)))


def _after_sent(tenant, item: dict, actor: str) -> None:
    from . import events, handlers

    handlers.load()
    for fn in _SENT_HOOKS.get(item["kind"], []) + _SENT_HOOKS.get("*", []):
        try:
            fn(tenant, item, actor)
        except Exception:  # noqa: BLE001 - it really went out; a follow-on hook must not turn that into an error
            from .redact import redact

            log.exception("on_sent hook failed: %s", redact(getattr(fn, "__name__", fn)))
    events.emit(tenant, "item_sent", {"queue_id": item["id"], "kind": item["kind"], "channel": item["channel"],
                                      "owner_user_id": (item.get("meta") or {}).get("owner_user_id"),
                                      "delivered": item.get("delivered")})


def counts(tenant, kinds: set[str] | None = None, own_user_id: int | None = None) -> dict:
    """Items per status, over exactly what this person may see (kinds=None: everything, for the owner)."""
    scope = _scope_sql(kinds, own_user_id)
    if scope is None:
        return {}
    with tenant.db() as c:
        return {r["status"]: r["n"] for r in c.execute(f"SELECT status, COUNT(*) n FROM queue WHERE 1=1{scope[0]} "
                                                       "GROUP BY status", scope[1])}


def decidable_kinds(role: str) -> set[str] | None:
    """None = all kinds (owner)."""
    if role == "owner":
        return None
    return set(auth.ROLE_DECIDE.get(role, set()))


def local_day_start(ts: float | None = None) -> float:
    lt = time.localtime(ts if ts is not None else time.time())
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
