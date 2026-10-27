"""Approval queue. Every outbound item (DM, email, SMS, post, calendar invite, ad change,
CRM stage move ...) lands here. Guardrails run when an item is queued and again when it is
approved (after edits). Approving only marks the item "approved / ready to send manually"
or delivers it on a mock transport; nothing reaches a real person unless a per-tenant live
switch is on for that channel and the item was approved."""
from __future__ import annotations

import re

from . import audit, auth, db, guardrails

STATUSES = ("pending", "held", "approved", "rejected", "needs_human")
OPEN = ("pending", "held", "needs_human")

# channel -> how an approved item is delivered. "mock:*" writes to channel_messages only.
MOCK_CHANNELS = {
    "instagram_dm": "mock:instagram", "email": "mock:email", "sms": "mock:sms", "telegram": "mock:telegram",
    "slack": "mock:slack", "calendar": "mock:calendar", "ads": "mock:ads", "crm": "mock:crm", "web": "mock:web",
    "phone": "mock:phone",
}


class QueueError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


# --------------------------------------------------------------------------- never-invite list
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


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


def blocked_list(tenant) -> set[str]:
    return {canonical_email(x) for x in (tenant.get("blocked_attendees", []) or [])}


def blocked_recipients(tenant, recipient_ref: str | None, meta: dict | None) -> list[str]:
    bl = blocked_list(tenant)
    if not bl:
        return []
    cands = set()
    for s in [recipient_ref or ""] + [str(x) for x in (meta or {}).get("attendees", [])]:
        cands |= {canonical_email(m) for m in _EMAIL.findall(s)}
        if s and "@" not in s:
            cands.add(s.strip().lower())
    return sorted(c for c in cands if c in bl)


# --------------------------------------------------------------------------- core
def _status_for(flags: list[dict]) -> str:
    if guardrails.blocked(flags):
        return "held"
    if guardrails.needs_review(flags):
        return "needs_human"
    return "pending"


def _check(tenant, title: str, body: str, meta: dict, recipient_ref: str | None, channel: str = "manual") -> list[dict]:
    text = f"Subject: {meta['subject']}\n{body}" if meta.get("subject") else body
    flags = guardrails.check_for(tenant, text, meta)
    if title and meta.get("check_title", True):
        flags += [f for f in guardrails.check_for(tenant, title, {**meta, "prospect_facing": False}) if f["code"] == "banned_name"]
    for addr in blocked_recipients(tenant, recipient_ref, meta):
        flags.append({"code": "never_invite", "severity": "block", "message": "Recipient is on the never-invite list",
                      "match": "[blocked attendee]"})
    if channel in ("sms", "phone"):
        from . import channels

        if not channels.opted_in(tenant, channel, (recipient_ref or "").split(" ")[0]):
            flags.append({"code": "not_opted_in", "severity": "block",
                          "message": "SMS and phone need the recipient's opt-in first (they reply START)", "match": ""})
    return guardrails._dedupe(flags)


def add(tenant, kind: str, body: str, *, channel: str = "manual", recipient_ref: str | None = None, title: str = "",
        meta: dict | None = None, run_id: int | None = None, actor: str = "system", demo: bool = False) -> dict:
    meta = dict(meta or {})
    flags = _check(tenant, title, body, meta, recipient_ref, channel)
    status = _status_for(flags)
    names = guardrails.tenant_banned_names(tenant)
    with tenant.db() as c:
        qid = c.execute(
            "INSERT INTO queue(kind,channel,recipient_ref,title,body,meta,status,flags,created,run_id,demo) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (kind, channel, recipient_ref, guardrails.scrub(title, names), body, db.jdump(meta), status, db.jdump(flags),
             db.now(), run_id, 1 if demo or tenant.is_demo else 0),
        ).lastrowid
    audit.log(tenant, actor, "queue.add", {"id": qid, "kind": kind, "channel": channel, "status": status})
    if status == "held":
        audit.log(tenant, actor, "guardrail.block", {"id": qid, "codes": [f["code"] for f in flags if f["severity"] == "block"]})
    return get(tenant, qid)


def get(tenant, qid: int, scrub: bool = True) -> dict | None:
    with tenant.db() as c:
        r = db.row(c.execute("SELECT * FROM queue WHERE id=?", (qid,)).fetchone())
    if r is None:
        return None
    r["meta"] = db.jload(r["meta"], {})
    r["flags"] = db.jload(r["flags"], [])
    if scrub:
        names = guardrails.tenant_banned_names(tenant)
        r["body_display"] = guardrails.scrub(r["body"], names)
    return r


def list_items(tenant, status: str | None = None, kind: str | None = None, run_id: int | None = None,
               chain_id: str | None = None, ids: list[int] | None = None, kinds: set[str] | None = None,
               limit: int = 200, tag: str | None = None, own_user_id: int | None = None) -> list[dict]:
    q = "SELECT * FROM queue WHERE 1=1"
    args: list = []
    if status == "open":
        q += " AND status IN ('pending','held','needs_human')"
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
    if kinds is not None:
        own = (f"(json_extract(meta,'$.owner_user_id')=? AND kind IN ({','.join('?' * len(auth.OWN_DECIDE))}))"
               if own_user_id is not None else None)
        if not kinds and own is None:
            return []
        parts, pargs = [], []
        if kinds:
            parts.append(f"kind IN ({','.join('?' * len(kinds))})")
            pargs += sorted(kinds)
        if own:
            parts.append(own)
            pargs += [own_user_id, *sorted(auth.OWN_DECIDE)]
        q += " AND (" + " OR ".join(parts) + ")"
        args += pargs
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    names = guardrails.tenant_banned_names(tenant)
    with tenant.db() as c:
        out = db.rows(c.execute(q, args))
    for r in out:
        r["meta"] = db.jload(r["meta"], {})
        r["flags"] = db.jload(r["flags"], [])
        r["body_display"] = guardrails.scrub(r["body"], names)
    return out


def _may(role: str, item: dict, user_id: int | None) -> bool:
    return auth.can_decide(role, item["kind"], item["meta"].get("owner_user_id"), user_id)


def edit(tenant, qid: int, body: str, actor: str, role: str = "owner", meta_patch: dict | None = None,
         user_id: int | None = None) -> dict:
    item = get(tenant, qid, scrub=False)
    if item is None:
        raise QueueError(404, "Not found")
    if item["status"] not in OPEN:
        raise QueueError(409, f"Item is already {item['status']}")
    if not _may(role, item, user_id):
        raise QueueError(403, "Your role can't edit this item")
    meta = {**item["meta"], **(meta_patch or {})}
    flags = _check(tenant, item["title"] or "", body, meta, item["recipient_ref"], item["channel"])
    status = _status_for(flags)
    with tenant.db() as c:
        c.execute("UPDATE queue SET body=?, flags=?, status=?, meta=? WHERE id=?",
                  (body, db.jdump(flags), status, db.jdump(meta), qid))
    audit.log(tenant, actor, "queue.edit", {"id": qid, "status": status})
    return get(tenant, qid)


def approve(tenant, qid: int, actor: str, role: str = "owner", user_id: int | None = None) -> dict:
    item = get(tenant, qid, scrub=False)
    if item is None:
        raise QueueError(404, "Not found")
    if not _may(role, item, user_id):
        raise QueueError(403, "Your role can't approve this kind of item")
    if item["status"] not in OPEN:
        raise QueueError(409, f"Item is already {item['status']}")
    # guardrails again, on the current (possibly edited) body
    flags = _check(tenant, item["title"] or "", item["body"], item["meta"], item["recipient_ref"], item["channel"])
    if guardrails.blocked(flags):
        with tenant.db() as c:
            c.execute("UPDATE queue SET flags=?, status='held' WHERE id=?", (db.jdump(flags), qid))
        audit.log(tenant, actor, "guardrail.block", {"id": qid, "at": "approve",
                                                     "codes": [f["code"] for f in flags if f["severity"] == "block"]})
        raise QueueError(409, "Blocked by guardrails. Edit it clean before approving.")
    delivered = deliver(tenant, item, actor)
    with tenant.db() as c:
        c.execute("UPDATE queue SET status='approved', flags=?, decided_by=?, decided_at=?, delivered=? WHERE id=?",
                  (db.jdump(flags), actor, db.now(), delivered, qid))
    audit.log(tenant, actor, "queue.approve", {"id": qid, "kind": item["kind"], "delivered": delivered})
    _after_approve(tenant, item, actor)
    return get(tenant, qid)


def reject(tenant, qid: int, actor: str, role: str = "owner", reason: str = "", user_id: int | None = None) -> dict:
    item = get(tenant, qid, scrub=False)
    if item is None:
        raise QueueError(404, "Not found")
    if not _may(role, item, user_id):
        raise QueueError(403, "Your role can't reject this kind of item")
    if item["status"] not in OPEN:
        raise QueueError(409, f"Item is already {item['status']}")
    with tenant.db() as c:
        c.execute("UPDATE queue SET status='rejected', decided_by=?, decided_at=? WHERE id=?", (actor, db.now(), qid))
    audit.log(tenant, actor, "queue.reject", {"id": qid, "reason": reason[:200]})
    return get(tenant, qid)


# --------------------------------------------------------------------------- delivery
def deliver(tenant, item: dict, actor: str) -> str:
    """Approved items go to a mock transport or are marked ready to send manually.
    Real delivery happens only behind a server-only switch AND a vault login: calendar invites
    (live_calendar_enabled, live/chain.py) and Telegram / Slack / SMS / email messages
    (live_channels_enabled, live/senders.py). A failed live send leaves the item undecided."""
    # never-invite list, second check at send time
    if blocked_recipients(tenant, item["recipient_ref"], item["meta"]):
        raise QueueError(409, "Recipient is on the never-invite list")
    ch = item["channel"] or "manual"
    if ch == "calendar" and item["meta"].get("live") and tenant.get("live_calendar_enabled", False):
        from .live import chain

        return chain.deliver_calendar_invite(tenant, item)
    from .live import senders

    if senders.ready(tenant, ch):
        try:
            mid = senders.send(tenant, ch, (item["recipient_ref"] or "").split(" ")[0], item["body"],
                               subject=item["meta"].get("subject"))
        except Exception as e:  # noqa: BLE001 - the item stays open; the reason is redacted and audited
            from .redact import redact

            audit.log(tenant, actor, "action", {"queue_id": item["id"], "live_send": "failed", "channel": ch})
            raise QueueError(502, redact(f"Live send failed ({type(e).__name__}); nothing was marked sent. Try again."))
        with tenant.db() as c:
            c.execute("INSERT INTO channel_messages(channel,direction,ext_id,body,meta,created) VALUES(?,?,?,?,?,?)",
                      (ch, "out", item["recipient_ref"], item["body"], db.jdump({"queue_id": item["id"], "live": True,
                                                                                  "provider_id": mid}), db.now()))
        audit.log(tenant, actor, "action", {"queue_id": item["id"], "live_send": "ok", "channel": ch})
        return f"live:{ch}:{mid}"
    if ch in MOCK_CHANNELS:
        with tenant.db() as c:
            c.execute("INSERT INTO channel_messages(channel,direction,ext_id,body,meta,created) VALUES(?,?,?,?,?,?)",
                      (ch, "out", item["recipient_ref"], item["body"], db.jdump({"queue_id": item["id"], "mock": True}),
                       db.now()))
        return MOCK_CHANNELS[ch]
    return "ready to send manually"


_HOOKS: dict[str, list] = {}


def on_approve(kind: str):
    def deco(fn):
        _HOOKS.setdefault(kind, []).append(fn)
        return fn

    return deco


def _after_approve(tenant, item: dict, actor: str) -> None:
    from . import handlers

    handlers.load()
    for fn in _HOOKS.get(item["kind"], []) + _HOOKS.get("*", []):
        fn(tenant, item, actor)


def counts(tenant) -> dict:
    with tenant.db() as c:
        return {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) n FROM queue GROUP BY status")}


def decidable_kinds(role: str) -> set[str] | None:
    """None = all kinds (owner)."""
    if role == "owner":
        return None
    return set(auth.ROLE_DECIDE.get(role, set()))
