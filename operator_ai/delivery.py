"""Delivery: truth, scheduling, cadences, addressing and email compliance.

* Addressing. resolve_address(tenant, channel, recipient_ref, meta) turns what a queue item says
  ("Jane Doe <jane@x>", "+1 (555) 0142", "@handle", a student's display name) into an address the
  channel can use, falling back to the party's contact points (contact_points table) and the party's own
  row (prospect, lead, student, chain lead). Telegram nudges go to the student's linked chat, or by email
  when there is none. A live channel with no valid address holds the item ("no_address").
* Scheduling. Approved items with meta.send_after in the future wait as "scheduled". dispatch_due(tenant)
  (scheduler tick, demo too) re-runs guardrails and the never-invite check, evaluates meta.cancel_if
  (lead_replied_since, booking_cancelled, prospect_dnc, unsubscribed, stage_beyond:<stage>) and then sends,
  hands the item to a person, or cancels it with a plain-English reason.
* Cadences. A follow-up sequence starts only when its first touch is confirmed sent. Each next step is
  drafted (template, AI off) at its due time, only when nothing came in since the last outbound and the
  contact is not do-not-contact; the per-person cap counts confirmed sends per local day.
* Email compliance. Prospect-facing email carries a footer with the business postal address and an
  unsubscribe line; live sends add List-Unsubscribe + List-Unsubscribe-Post (one click) headers. /u/{token}
  sets do-not-contact, adds the address to the suppression list and emits dnc_set + unsubscribed. Per-mailbox
  daily cap with a warm-up ramp. guard_rules blocks prospect email without the unsubscribe line or address.

Nothing here sends anything by itself: every message is a queue item a person approved."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import time
from email.utils import parseaddr

from . import audit, db, events, guardrails, queue, runtime

log = logging.getLogger("operator.delivery")

CHANNEL_LABEL = {
    "instagram_dm": "Instagram DM", "linkedin": "LinkedIn message", "phone": "Phone call", "whatsapp": "WhatsApp message",
    "facebook_dm": "Facebook message", "tiktok_dm": "TikTok message", "x_dm": "X message", "email": "Email",
    "sms": "Text message", "telegram": "Telegram message", "slack": "Slack message", "calendar": "Calendar invite",
}
PARTY_META = {"prospect": "prospect_id", "lead": "lead_id", "chain_lead": "chain_lead_id", "student": "student_id",
              "client": "client_id"}
PROSPECT_STAGES = ["new", "researched", "contacted", "replied", "call_booked", "proposal", "won", "lost", "nurture", "dnc"]
LEAD_STAGES = ["new", "replied", "quality_conversation", "link_sent", "booking", "booked", "showed", "no_show", "closed",
               "lost", "nurture"]
CHAIN_STAGES = ["qualified", "replied", "booking", "booked", "showed", "no_show", "closed", "lost"]
CANCEL_PREDICATES = ("lead_replied_since", "booking_cancelled", "prospect_dnc", "unsubscribed")
FOOTER_MARK = "\n\n-- \n"
UNSUB_PLACEHOLDER = "{unsubscribe_link}"

DEFAULT_SETTINGS = {
    "email_daily_cap": 40,       # live emails per mailbox per local day
    "warmup_enabled": True,      # a new mailbox ramps up: first-day limit, then + step a day, up to the cap
    "warmup_first_day": 10,
    "warmup_step": 5,
    "warmup_started": None,      # set on the first live email
    "daily_touch_cap": 40,       # confirmed prospect-facing sends per person per local day (follow-up drafting)
    "cadence_days": [2, 4, 7, 37, 67],  # follow-up days after the first touch was confirmed sent
}


# --------------------------------------------------------------------------- settings
def get_settings(tenant) -> dict:
    s = dict(DEFAULT_SETTINGS)
    raw = tenant.get("delivery_settings") or {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k in s:
                s[k] = v
    return s


def put_settings(tenant, data: dict, actor: str) -> dict:
    cur = get_settings(tenant)
    for k, v in data.items():
        if k in DEFAULT_SETTINGS and k != "warmup_started" and v is not None:
            cur[k] = v
    days = sorted({int(d) for d in cur.get("cadence_days") or [] if 1 <= int(d) <= 365})[:8]
    cur["cadence_days"] = days or list(DEFAULT_SETTINGS["cadence_days"])
    tenant.set("delivery_settings", cur)
    audit.log(tenant, actor, "settings", {"key": "delivery_settings"})
    return cur


def local_day_start(ts: float | None = None) -> float:
    return queue.local_day_start(ts)


def next_morning(now: float | None = None, hour: int = 9) -> float:
    now = now or time.time()
    lt = time.localtime(local_day_start(now) + 86400 + 3600)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, 0, 0, 0, 0, -1))


def fmt_time(ts: float) -> str:
    return time.strftime("%a %d %b %H:%M", time.localtime(ts))


# --------------------------------------------------------------------------- parties
def party_of(meta: dict | None) -> tuple[str, int] | None:
    meta = meta or {}
    pt, pid = meta.get("party_type"), meta.get("party_id")
    if pt in PARTY_META and _int(pid) is not None:
        return pt, _int(pid)
    if meta.get("practice"):
        return None  # practice prospects live in a separate demo table
    for t, key in PARTY_META.items():
        v = _int(meta.get(key))
        if v is not None:
            return t, v
    return None


def _int(v) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None


def _party_row(tenant, party: tuple[str, int]) -> dict | None:
    t, pid = party
    table = {"prospect": "prospects", "lead": "leads", "student": "students", "client": "clients",
             "chain_lead": "chain_leads"}.get(t)
    if not table:
        return None
    with tenant.db() as c:
        r = db.row(c.execute(f"SELECT * FROM {table} WHERE id=?", (pid,)).fetchone())
    if r and t == "chain_lead":
        fields = (db.jload(r.get("data"), {}) or {}).get("fields", {}) or {}
        r["name"] = fields.get("full_name") or f"Lead {pid}"
        r["email"] = fields.get("email")
        ig = str(fields.get("instagram") or "").strip()
        r["handle"] = ("@" + ig.lstrip("@")) if ig else None  # same form the chain's own DMs use
    return r


def party_label(tenant, party: tuple[str, int] | None) -> str:
    if not party:
        return "Contact"
    r = _party_row(tenant, party)
    return (r or {}).get("name") or f"{party[0].replace('_', ' ').title()} {party[1]}"


def is_dnc(tenant, party: tuple[str, int]) -> bool:
    t, pid = party
    with tenant.db() as c:
        if t == "prospect":
            r = c.execute("SELECT do_not_contact, stage FROM prospects WHERE id=?", (pid,)).fetchone()
            return bool(r and (r["do_not_contact"] or r["stage"] == "dnc"))
        if t == "lead":
            r = c.execute("SELECT do_not_contact FROM leads WHERE id=?", (pid,)).fetchone()
            return bool(r and r["do_not_contact"])
        if t == "chain_lead":
            r = c.execute("SELECT do_not_contact FROM chain_leads WHERE id=?", (pid,)).fetchone()
            return bool(r and r["do_not_contact"])
    return False


def set_dnc(tenant, party: tuple[str, int], actor: str, reason: str = "") -> bool:
    t, pid = party
    with tenant.db() as c:
        if t == "prospect":
            n = c.execute("UPDATE prospects SET do_not_contact=1, stage='dnc', updated=? WHERE id=?", (db.now(), pid)).rowcount
        elif t == "lead":
            n = c.execute("UPDATE leads SET do_not_contact=1 WHERE id=?", (pid,)).rowcount
        elif t == "chain_lead":
            n = c.execute("UPDATE chain_leads SET do_not_contact=1 WHERE id=?", (pid,)).rowcount
        else:
            n = 0
    if n:
        audit.log(tenant, actor, "consent", {"dnc": True, "party_type": t, "party_id": pid, "reason": reason[:120]})
        events.emit(tenant, "dnc_set", {"party_type": t, "party_id": pid})
    return bool(n)


# --------------------------------------------------------------------------- addresses
_TELEGRAM_ID = re.compile(r"^-?\d{1,20}$")
_TELEGRAM_NAME = re.compile(r"^@[A-Za-z][A-Za-z0-9_]{4,31}$")
_SLACK_ID = re.compile(r"^(?:[CDGU][A-Z0-9]{2,}|#[a-z0-9_\-]{1,80})$")
_HANDLE = re.compile(r"^@[A-Za-z0-9._]{1,64}$")
_URL = re.compile(r"^https?://\S{3,300}$", re.I)


def email_from(s: str | None) -> str | None:
    s = (s or "").strip()
    if not s:
        return None
    _, addr = parseaddr(s)
    if addr and queue._EMAIL.fullmatch(addr):
        return addr
    m = queue._EMAIL.search(s)
    return m.group(0) if m else None


def _contact(tenant, meta: dict, channel: str) -> str | None:
    party = party_of(meta)
    if not party:
        return None
    with tenant.db() as c:
        r = c.execute("SELECT address FROM contact_points WHERE party_type=? AND party_id=? AND channel=? "
                      "ORDER BY verified DESC, id DESC LIMIT 1", (party[0], party[1], channel)).fetchone()
    if r:
        return r["address"]
    row = _party_row(tenant, party) or {}
    if channel == "email":
        return email_from(row.get("email"))
    if channel in ("instagram_dm",):
        h = (row.get("handle") or "").strip()
        return h if _HANDLE.match(h) or _URL.match(h) else None
    return None


def _telegram_chat(tenant, meta: dict) -> str | None:
    sid, uid = _int(meta.get("student_id")), _int(meta.get("user_id"))
    with tenant.db() as c:
        if uid is None and sid is not None:
            r = c.execute("SELECT id FROM users WHERE student_id=? ORDER BY id LIMIT 1", (sid,)).fetchone()
            uid = r["id"] if r else None
        if uid is None:
            return None
        r = c.execute("SELECT ext_id FROM channel_links WHERE channel='telegram' AND user_id=? AND status='linked' "
                      "AND ext_id IS NOT NULL ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
    return r["ext_id"] if r else None


def resolve_address(tenant, channel: str, recipient_ref: str | None, meta: dict | None) -> str | None:
    """The address `channel` can actually deliver to, or None."""
    meta = meta or {}
    ref = (recipient_ref or "").strip()
    if channel == "email":
        return email_from(ref) or next((email_from(str(a)) for a in meta.get("attendees") or [] if email_from(str(a))),
                                       None) or _contact(tenant, meta, "email")
    if channel == "calendar":
        return email_from(ref) or next((email_from(str(a)) for a in meta.get("attendees") or [] if email_from(str(a))),
                                       None)
    if channel in ("sms", "phone", "whatsapp"):
        return queue.e164(ref) or (queue.e164(_contact(tenant, meta, channel) or "")) or (
            queue.e164(_contact(tenant, meta, "sms") or "") if channel != "sms" else None)
    if channel == "telegram":
        if _TELEGRAM_ID.match(ref) or _TELEGRAM_NAME.match(ref):
            return ref
        return _telegram_chat(tenant, meta) or _contact(tenant, meta, "telegram")
    if channel == "slack":
        return ref if _SLACK_ID.match(ref) else _contact(tenant, meta, "slack")
    if channel in queue.MANUAL_CHANNELS:
        if _HANDLE.match(ref) or _URL.match(ref) or (channel == "phone" and queue.e164(ref)):
            return ref
        return _contact(tenant, meta, channel)
    return ref or None


def route_for(tenant, channel: str, recipient_ref: str | None, meta: dict | None) -> tuple[str, str | None]:
    """(channel, address) to deliver on. Telegram falls back to email when the person has no linked chat."""
    addr = resolve_address(tenant, channel, recipient_ref, meta)
    if addr:
        return channel, addr
    if channel == "telegram":
        from .live import senders

        meta = meta or {}
        email = _contact(tenant, meta, "email")
        if email and senders.ready(tenant, "email"):
            return "email", email
    return channel, None


def no_address_message(channel: str) -> str:
    return (f"No valid {CHANNEL_LABEL.get(channel, channel).lower()} address for this recipient. Add one to their "
            "contact details, or send it yourself.")


def missing_live_address(tenant, channel: str, recipient_ref: str | None, meta: dict | None) -> str | None:
    """A message for the 'no_address' flag when a live sender is on for this channel but the recipient has
    no address it can use; None otherwise."""
    from .live import senders

    if channel not in senders.NEEDS or not senders.ready(tenant, channel):
        return None
    rch, addr = route_for(tenant, channel, recipient_ref, meta)
    if addr and senders.ready(tenant, rch):
        return None
    return no_address_message(channel)


def opted_in(tenant, channel: str, recipient: str, meta: dict | None = None) -> bool:
    """SMS/phone opt-in: the latest START/STOP for that number (any formatting), or an opted-in contact point."""
    m = re.search(r"\+?\d[\d\s\-().]{5,22}\d", recipient or "")
    want = queue.phone_digits(m.group(0)) if m else None
    if want is None:
        addr = _contact(tenant, meta or {}, channel)
        want = queue.phone_digits(addr or "")
    if want is None:
        return False
    with tenant.db() as c:
        for r in c.execute("SELECT ext_id, opted_in FROM channel_links WHERE channel=? ORDER BY id DESC", (channel,)):
            if queue.phone_digits(r["ext_id"] or "") == want:
                return bool(r["opted_in"])
        party = party_of(meta)
        if party:
            for r in c.execute("SELECT address FROM contact_points WHERE party_type=? AND party_id=? AND channel=? "
                               "AND opted_in=1", (party[0], party[1], channel)):
                if queue.phone_digits(r["address"]) == want:
                    return True
    return False


# --------------------------------------------------------------------------- contact points
def add_contact_point(tenant, party_type: str, party_id: int, channel: str, address: str, *, verified: bool = False,
                      opted_in_: bool = False, source: str = "manual", actor: str = "system") -> dict:
    if party_type not in PARTY_META:
        raise ValueError("unknown party type")
    addr = (address or "").strip()
    if channel == "email":
        addr = email_from(addr) or ""
    elif channel in ("sms", "phone", "whatsapp"):
        addr = queue.e164(addr) or ""
    elif channel == "instagram_dm" and addr and not addr.startswith(("@", "http")):
        addr = "@" + addr
    if not addr or (channel in ("instagram_dm", "linkedin") and not (_HANDLE.match(addr) or _URL.match(addr))):
        raise ValueError(f"That isn't a valid {CHANNEL_LABEL.get(channel, channel).lower()} address")
    now = db.now()
    with tenant.db() as c:
        c.execute("INSERT INTO contact_points(party_type,party_id,channel,address,verified,opted_in,source,demo,created,updated) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(party_type,party_id,channel,address) DO UPDATE SET "
                  "verified=excluded.verified, opted_in=excluded.opted_in, updated=excluded.updated",
                  (party_type, party_id, channel, addr, int(verified), int(opted_in_), source[:40],
                   1 if tenant.is_demo else 0, now, now))
        r = db.row(c.execute("SELECT * FROM contact_points WHERE party_type=? AND party_id=? AND channel=? AND address=?",
                             (party_type, party_id, channel, addr)).fetchone())
    audit.log(tenant, actor, "settings", {"contact_point": r["id"], "party_type": party_type, "channel": channel})
    return r


def contact_points(tenant, party_type: str, party_id: int) -> list[dict]:
    with tenant.db() as c:
        return db.rows(c.execute("SELECT * FROM contact_points WHERE party_type=? AND party_id=? ORDER BY channel, id",
                                 (party_type, party_id)))


def delete_contact_point(tenant, cp_id: int, actor: str) -> bool:
    with tenant.db() as c:
        n = c.execute("DELETE FROM contact_points WHERE id=?", (cp_id,)).rowcount
    if n:
        audit.log(tenant, actor, "settings", {"contact_point_deleted": cp_id})
    return bool(n)


# --------------------------------------------------------------------------- suppression list
def suppress(tenant, address: str, channel: str | None, reason: str, party: tuple[str, int] | None = None) -> int:
    keys = {k for k in queue.address_keys(address) if not k.startswith("raw:")} or queue.address_keys(address)
    n = 0
    with tenant.db() as c:
        for k in keys:
            n += c.execute("INSERT OR IGNORE INTO suppressions(address_key,channel,reason,party_type,party_id,demo,created) "
                           "VALUES(?,?,?,?,?,?,?)", (k, channel, reason[:120], party[0] if party else None,
                                                     party[1] if party else None, 1 if tenant.is_demo else 0,
                                                     db.now())).rowcount
    return n


def suppressed(tenant, refs: list[str]) -> bool:
    keys: set[str] = set()
    for r in refs:
        keys |= {k for k in queue.address_keys(r or "") if not k.startswith("raw:")}
    if not keys:
        return False
    marks = ",".join("?" * len(keys))
    with tenant.db() as c:
        return c.execute(f"SELECT 1 FROM suppressions WHERE address_key IN ({marks}) LIMIT 1", tuple(keys)).fetchone() is not None


def suppression_list(tenant, limit: int = 200) -> list[dict]:
    with tenant.db() as c:
        return db.rows(c.execute("SELECT id, address_key, channel, reason, party_type, party_id, created FROM suppressions "
                                 "ORDER BY id DESC LIMIT ?", (limit,)))


# --------------------------------------------------------------------------- email compliance
def postal_address(tenant) -> str:
    from . import brief

    p = brief.profile(tenant) if tenant is not None else {}
    return " ".join(str(((p.get("identity") or {}).get("postal_address")) or "").split())


def brand_name(tenant) -> str:
    from . import brief

    p = brief.profile(tenant)
    name = ((p.get("identity") or {}).get("name") or "").strip()
    return name or ((tenant.get("brand", {}) or {}).get("name") or "").strip() or "Our team"


def sender_identity(tenant, meta: dict | None = None) -> dict:
    """Who a prospect email is legally from, for its compliance footer. A person emailing their OWN prospects
    (meta.prospect_id + meta.owner_user_id: the prospects feature's sender details) is the sender, with their own
    business and postal address, never the programme's. The owner's own outreach uses their sender details when
    set, else the Business profile; everything else is the business itself."""
    meta = meta or {}
    biz = {"name": brand_name(tenant), "postal_address": postal_address(tenant), "own": False}
    uid = _int(meta.get("owner_user_id"))
    if tenant is None or uid is None or not meta.get("prospect_id"):
        return biz
    try:
        from . import prospects

        s = prospects.sender_of(tenant, uid)
        with tenant.db() as c:
            r = c.execute("SELECT role FROM users WHERE id=?", (uid,)).fetchone()
    except Exception:  # noqa: BLE001 - no sender details: the business is the sender
        return biz
    name = str(s.get("business") or s.get("name") or "").strip()
    addr = " ".join(str(s.get("postal_address") or "").split())
    if r is None or r["role"] == "owner":
        return {"name": name or biz["name"], "postal_address": addr or biz["postal_address"], "own": False}
    return {"name": name or "Sent by", "postal_address": addr, "own": True}


def with_footer(tenant, body: str, meta: dict | None = None) -> str:
    """Prospect email body with the compliance footer (replacing any earlier one), naming the real sender."""
    base = (body or "").split(FOOTER_MARK)[0].rstrip()
    who = sender_identity(tenant, meta)
    first, addr = who["name"], who["postal_address"]
    if addr:
        first = f"{first} · {addr}"
    return f"{base}{FOOTER_MARK}{first}\nDon't want these emails? Unsubscribe here: {UNSUB_PLACEHOLDER}"


def _norm_space(s: str) -> str:
    return " ".join((s or "").lower().split())


def guard_rules(text: str, mode: str, meta: dict, tenant) -> list[dict]:
    """Prospect-facing email must carry an unsubscribe line and the business postal address."""
    queued = (meta or {}).get("_channel")  # set by the queue, which adds the footer before checking
    channel = queued or (meta or {}).get("channel")
    if mode != "outreach" or channel != "email":
        return []
    flags = []
    low = (text or "").lower()
    # a draft previewed before it is queued has no footer yet (queue.add adds it), so only the queue's own
    # check can judge the footer's text; a business postal address must be set up either way
    if queued and ("unsubscribe" not in low or (UNSUB_PLACEHOLDER not in text and "/u/" not in text)):
        flags.append(guardrails._flag("missing_unsubscribe", "block",
                                      "Prospect emails need an unsubscribe line with a working link", ""))
    who = sender_identity(tenant, meta) if tenant is not None else {"postal_address": "", "own": False}
    addr = who["postal_address"]
    if not addr:
        flags.append(guardrails._flag("missing_postal_address", "block",
                                      "Add your postal address (Prospects, Ideal client, Sender details) before emailing "
                                      "your prospects" if who["own"] else
                                      "Add your business postal address (Business profile, Identity) before emailing "
                                      "prospects", ""))
    elif queued and _norm_space(addr) not in _norm_space(text):
        flags.append(guardrails._flag("missing_postal_address", "block",
                                      "Prospect emails must show your business postal address", ""))
    return flags


def public_base() -> str:
    from . import config

    return os.environ.get("OPERATOR_PUBLIC_URL", "").strip().rstrip("/") or f"http://127.0.0.1:{config.get().port}"


def _is_local(base: str) -> bool:
    return bool(re.match(r"^https?://(localhost|127\.|0\.0\.0\.0|\[::1\]|[^/]*\.localhost)", base, re.I))


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def unsubscribe_path(tenant, item: dict) -> str:
    """Create (once per item) the one-click unsubscribe link for a prospect email; stored in meta."""
    meta = item["meta"]
    if meta.get("unsubscribe_path"):
        return meta["unsubscribe_path"]
    token = f"{tenant.id}.{secrets.token_urlsafe(18)}"
    party = party_of(meta)
    addr = resolve_address(tenant, item.get("channel") or "email", item.get("recipient_ref"), meta)
    with tenant.db() as c:
        c.execute("INSERT INTO unsubscribe_tokens(token_hash,party_type,party_id,channel,address,queue_id,created) "
                  "VALUES(?,?,?,?,?,?,?)", (_token_hash(token), party[0] if party else None, party[1] if party else None,
                                            item.get("channel"), addr, item["id"], db.now()))
    meta["unsubscribe_path"] = f"/u/{token}"
    with tenant.db() as c:
        c.execute("UPDATE queue SET meta=? WHERE id=?", (db.jdump(meta), item["id"]))
    return meta["unsubscribe_path"]


def _is_prospect_email(item: dict) -> bool:
    return (item.get("channel") == "email" and bool((item.get("meta") or {}).get("prospect_facing"))
            and UNSUB_PLACEHOLDER in (item.get("body") or ""))


def final_body(tenant, item: dict) -> str:
    if _is_prospect_email(item):
        return item["body"].replace(UNSUB_PLACEHOLDER, public_base() + unsubscribe_path(tenant, item))
    return item["body"]


def lookup_token(token: str):
    """(tenant, token row) for a /u/ token, or None."""
    from . import tenancy

    tid, _, rest = (token or "").partition(".")
    if not tid or not rest or len(token) > 200:
        return None
    try:
        t = tenancy.get(tid)
    except Exception:  # noqa: BLE001 - unknown tenant: the link is simply not valid
        return None
    with t.db() as c:
        r = db.row(c.execute("SELECT * FROM unsubscribe_tokens WHERE token_hash=?", (_token_hash(token),)).fetchone())
    return (t, r) if r else None


def unsubscribe(token: str) -> dict | None:
    """One-click unsubscribe: do-not-contact on the prospect/lead, the address on the suppression list,
    pending sends to them cancelled, dnc_set + unsubscribed emitted. Idempotent."""
    found = lookup_token(token)
    if not found:
        return None
    t, row = found
    party = (row["party_type"], row["party_id"]) if row["party_type"] and row["party_id"] is not None else None
    with t.db() as c:  # claim: two clicks at once (or a mail scanner plus a person) apply it once
        first = c.execute("UPDATE unsubscribe_tokens SET used=? WHERE token_hash=? AND used IS NULL",
                          (db.now(), row["token_hash"])).rowcount == 1
    if first:
        if row["address"]:
            suppress(t, row["address"], row["channel"], "unsubscribed", party)
        audit.log(t, "unsubscribe-link", "consent", {"unsubscribed": True, "party_type": row["party_type"],
                                                     "party_id": row["party_id"], "queue_id": row["queue_id"]})
        if party:
            if not set_dnc(t, party, "unsubscribe-link", "unsubscribed"):
                events.emit(t, "dnc_set", {"party_type": party[0], "party_id": party[1]})
        events.emit(t, "unsubscribed", {"party_type": row["party_type"], "party_id": row["party_id"],
                                        "channel": row["channel"], "queue_id": row["queue_id"]})
        _cancel_for_address(t, row["address"], "They unsubscribed, so it was not sent.")
    return {"ok": True, "brand": brand_name(t), "already": not first}


def _cancel_for_address(tenant, address: str | None, reason: str) -> int:
    keys = {k for k in queue.address_keys(address or "") if not k.startswith("raw:")}
    if not keys:
        return 0
    n = 0
    with tenant.db() as c:
        rows = db.rows(c.execute("SELECT id, recipient_ref FROM queue WHERE status IN ('scheduled','awaiting_manual_send')"))
    for r in rows:
        if queue.address_keys(r["recipient_ref"] or "") & keys:
            n += queue.cancel_system(tenant, r["id"], reason)
    return n


# --------------------------------------------------------------------------- caps
def email_cap_today(tenant, now: float | None = None) -> int:
    s = get_settings(tenant)
    cap = int(s["email_daily_cap"] or 0)
    if s.get("warmup_enabled"):
        start = s.get("warmup_started")
        days = 0 if not start else max(0, int(round((local_day_start(now) - local_day_start(float(start))) / 86400)))
        cap = min(cap, int(s["warmup_first_day"]) + int(s["warmup_step"]) * days)
    return max(0, cap)


def live_emails_today(tenant, now: float | None = None) -> int:
    with tenant.db() as c:
        return c.execute("SELECT COUNT(*) FROM channel_messages WHERE channel='email' AND direction='out' AND "
                         "json_extract(meta,'$.live')=1 AND created>=?", (local_day_start(now),)).fetchone()[0]


def sends_today(tenant, owner_user_id: int | None, now: float | None = None) -> int:
    """Confirmed prospect-facing sends by one person (or by the business when None) since local midnight."""
    q = ("SELECT COUNT(*) FROM queue WHERE status='sent' AND sent_at>=? AND json_extract(meta,'$.prospect_facing')=1 AND ")
    with tenant.db() as c:
        if owner_user_id is None:
            return c.execute(q + "json_extract(meta,'$.owner_user_id') IS NULL", (local_day_start(now),)).fetchone()[0]
        return c.execute(q + "json_extract(meta,'$.owner_user_id')=?", (local_day_start(now), owner_user_id)).fetchone()[0]


# --------------------------------------------------------------------------- sending
def manual_reason(tenant, item: dict) -> str:
    ch = item.get("channel") or "manual"
    if _is_prospect_email(item):
        unsubscribe_path(tenant, item)
    label = CHANNEL_LABEL.get(ch, ch.replace("_", " "))
    if ch in queue.MANUAL_CHANNELS:
        return f"{label}s can't be sent automatically. Copy the message, send it from your own account, then press I sent it."
    if ch == "calendar":
        return "Send the calendar invite from your own calendar, then press I sent it."
    return f"{label} sending isn't connected for this workspace. Copy the message, send it yourself, then press I sent it."


def send_live(tenant, item: dict, channel: str, address: str, actor: str) -> dict:
    """Send through a live provider. Returns the queue outcome; raises QueueError(502) on a provider failure."""
    from .live import senders
    from .redact import redact

    meta = item["meta"]
    body = item["body"]
    headers = None
    if channel == "email":
        used, cap = live_emails_today(tenant), email_cap_today(tenant)
        if used >= cap:
            at = next_morning()
            return {"status": "scheduled", "send_after": at,
                    "reason": f"This mailbox reached its daily limit ({used} of {cap}). It sends {fmt_time(at)}."}
        if _is_prospect_email(item):
            base = public_base()
            if _is_local(base):
                raise queue.QueueError(409, "Prospect emails need the server's public web address so the unsubscribe "
                                            "link works. Ask whoever runs the server to set OPERATOR_PUBLIC_URL.")
            url = base + unsubscribe_path(tenant, item)
            body = body.replace(UNSUB_PLACEHOLDER, url)
            headers = {"List-Unsubscribe": f"<{url}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}
    try:
        mid = senders.send(tenant, channel, address, body, subject=meta.get("subject"), headers=headers,
                           idempotency_key=f"q{item['id']}")
    except Exception as e:  # noqa: BLE001 - nothing is marked sent; the reason is redacted and audited
        audit.log(tenant, actor, "action", {"queue_id": item["id"], "live_send": "failed", "channel": channel})
        raise queue.QueueError(502, redact(f"Live send failed ({type(e).__name__}); nothing was marked sent. Try again."))
    with tenant.db() as c:
        c.execute("INSERT INTO channel_messages(channel,direction,ext_id,body,meta,created) VALUES(?,?,?,?,?,?)",
                  (channel, "out", address, body[:4000], db.jdump({"queue_id": item["id"], "live": True,
                                                                   "provider_id": mid}), db.now()))
    audit.log(tenant, actor, "action", {"queue_id": item["id"], "live_send": "ok", "channel": channel})
    if channel == "email":
        s = get_settings(tenant)
        if s.get("warmup_enabled") and not s.get("warmup_started"):
            tenant.set("delivery_settings", {**(tenant.get("delivery_settings") or {}), "warmup_started": db.now()})
    note = f" (by email: no linked {CHANNEL_LABEL.get(item['channel'], item['channel'])} chat)" \
        if channel != (item.get("channel") or channel) else ""
    return {"status": "sent", "delivered": f"live:{channel}:{mid}", "reason": note.strip() or None}


def before_send(tenant, item: dict, actor: str) -> str | None:
    """Last look before an approved item leaves. A booking DM whose times have expired is redrafted with
    fresh times and goes back to a person (never sent with stale times)."""
    meta = item["meta"]
    valid = queue._num(meta.get("slots_valid_until"))
    if item["kind"] != "booking_dm" or not valid or valid >= db.now():
        return None
    from .agents import sales

    try:
        body, new_meta = sales.refresh_booking_draft(tenant, item)
    except sales.NoSlots:
        queue._set(tenant, item["id"], only_if="sending", status="needs_human",
                   status_reason="The times offered expired and there are no free times in the next two weeks. "
                                 "Free up the calendar or ask them for their availability.")
        return "The times in this message expired and there are no free times to offer instead."
    flags = queue._check(tenant, item["title"] or "", body, new_meta, item["recipient_ref"], item["channel"] or "manual")
    queue._set(tenant, item["id"], only_if="sending", body=body, meta=new_meta, flags=flags,
               status=queue._status_for(flags), status_reason="The times offered had expired, so fresh times were drafted.")
    audit.log(tenant, actor, "queue.edit", {"id": item["id"], "refreshed": "slots"})
    return "The times in this message had expired, so fresh times were drafted. Check them and approve again."


# --------------------------------------------------------------------------- cancel_if
def inbound_since(tenant, party: tuple[str, int] | None, recipient_ref: str | None, since: float) -> bool:
    with tenant.db() as c:
        if party:
            for r in c.execute("SELECT payload FROM app_events WHERE name='reply_received' AND created>?", (since,)):
                p = db.jload(r["payload"], {}) or {}
                if p.get("party_type") == party[0] and _int(p.get("party_id")) == party[1]:
                    return True
            if party[0] == "chain_lead" and c.execute("SELECT 1 FROM chain_events WHERE lead_id=? AND kind='reply' AND created>?",
                                                      (party[1], since)).fetchone():
                return True
        keys = {k for k in queue.address_keys(recipient_ref or "") if not k.startswith("raw:")}
        if keys:
            for r in c.execute("SELECT ext_id FROM channel_messages WHERE direction='in' AND created>?", (since,)):
                if queue.address_keys(r["ext_id"] or "") & keys:
                    return True
    return False


def booking_cancelled(tenant, meta: dict, party: tuple[str, int] | None, since: float) -> bool:
    ev = _int(meta.get("calendar_event_id"))
    with tenant.db() as c:
        if ev is not None:
            r = c.execute("SELECT status FROM calendar_events WHERE id=?", (ev,)).fetchone()
            if r is None or r["status"] == "cancelled":
                return True
        for r in c.execute("SELECT payload FROM app_events WHERE name='booking_cancelled' AND created>?", (since,)):
            p = db.jload(r["payload"], {}) or {}
            pev = _int(p.get("event_id"))
            if ev is not None and pev is not None:
                if pev == ev:  # this reminder's own call; another (older) booking being cancelled says nothing
                    return True
                continue
            if party and p.get("party_type") == party[0] and _int(p.get("party_id")) == party[1]:
                return True
        if ev is None and party and party[0] == "chain_lead":
            r = c.execute("SELECT status FROM chain_bookings WHERE lead_id=? ORDER BY id DESC LIMIT 1", (party[1],)).fetchone()
            if r and r["status"] == "cancelled":
                return True
    return False


def stage_beyond(tenant, party: tuple[str, int], stage: str) -> bool:
    order = {"prospect": PROSPECT_STAGES, "lead": LEAD_STAGES, "chain_lead": CHAIN_STAGES}.get(party[0])
    if not order or stage not in order:
        return False
    row = _party_row(tenant, party) or {}
    cur = row.get("stage")
    return cur in order and order.index(cur) > order.index(stage)


def cancel_reason(tenant, item: dict, now: float | None = None) -> str | None:
    """Why an approved, due item must not go out now (None = send it). meta.send_before is a hard deadline: a
    time-bound message (e.g. a webinar's "starts in an hour") whose moment passed while it waited (the server was
    down) is cancelled, never sent late."""
    meta = item["meta"]
    party = party_of(meta)
    since = float(item.get("created") or 0)
    try:
        before = float(meta.get("send_before")) if meta.get("send_before") is not None else None
    except (TypeError, ValueError):
        before = None
    if before is not None and (now or db.now()) > before:
        return "Its moment passed before it could go out, so it was not sent late."
    if party and is_dnc(tenant, party):
        return "This contact is marked do-not-contact, so it was not sent."
    preds = meta.get("cancel_if") or []
    if isinstance(preds, str):
        preds = [preds]
    for p in [str(x) for x in preds if x]:
        if p == "lead_replied_since" and inbound_since(tenant, party, item.get("recipient_ref"), since):
            return "They replied after this was drafted, so it was not sent."
        if p == "booking_cancelled" and booking_cancelled(tenant, meta, party, since):
            return "The call was cancelled, so this reminder was not sent."
        if p == "prospect_dnc" and party and is_dnc(tenant, party):
            return "This contact is marked do-not-contact, so it was not sent."
        if p == "unsubscribed":
            addr = resolve_address(tenant, item.get("channel") or "", item.get("recipient_ref"), meta)
            if suppressed(tenant, [item.get("recipient_ref") or "", addr or ""]) or (
                    item.get("channel") in ("sms", "phone") and not opted_in(tenant, item["channel"], item.get("recipient_ref") or "", meta)):
                return "They unsubscribed, so it was not sent."
        if p.startswith("stage_beyond:") and party and stage_beyond(tenant, party, p.split(":", 1)[1]):
            return f"They have moved past the {p.split(':', 1)[1].replace('_', ' ')} stage, so this was not sent."
    return None


def dispatch_due(tenant, now: float | None = None, limit: int = 200) -> dict:
    """Scheduler tick: deliver approved items whose send_after has come (or cancel them with a reason)."""
    now = now or db.now()
    recovered = queue.recover_stuck(tenant, now)
    with tenant.db() as c:
        ids = [r["id"] for r in c.execute("SELECT id FROM queue WHERE status='scheduled' AND COALESCE(send_after,0)<=? "
                                          "ORDER BY send_after, id LIMIT ?", (now, limit))]
    if not ids:
        return {"due": 0, "results": [], **({"recovered": recovered} if recovered else {})}

    def fn(run: runtime.Run):
        results = []
        for qid in ids:
            try:
                it = queue.dispatch(tenant, qid, "scheduler", now=now)
            except Exception as e:  # noqa: BLE001 - one bad item must not hold up every other due item
                from .redact import redact

                log.warning("scheduled send %s could not be checked: %s", qid, redact(f"{type(e).__name__}: {e}")[:300])
                queue._set(tenant, qid, only_if="scheduled", status="send_failed",
                           status_reason=redact(f"It couldn't be checked before sending ({type(e).__name__}); nothing "
                                                "was sent. Try again or cancel it.")[:500])
                it = queue.get(tenant, qid, scrub=False)
            if it is None:
                continue
            results.append({"id": qid, "status": it["status"], "reason": it.get("status_reason")})
        by = {}
        for r in results:
            by[r["status"]] = by.get(r["status"], 0) + 1
        run.note("dispatch", {"due": len(ids)}, by, label="Send, hand over or cancel each due item")
        return {"due": len(ids), "results": results, "by_status": by, **({"recovered": recovered} if recovered else {})}

    res = runtime.execute(tenant, "delivery", f"Scheduled sends ({len(ids)} due)",
                          ["Re-check guardrails and cancel rules", "Send, hand over or cancel each due item"], fn, "scheduler")
    return {**res["result"], "run_id": res["run_id"]}


dispatch_due.demo_too = True


# --------------------------------------------------------------------------- cadences
OPENER_KINDS = {"dm_opener", "prospect_dm", "prospect_email"}
LIVE_CADENCE = ("waiting_first_send", "active", "drafting", "waiting_send")
TEMPLATES = {
    "value_1": "Thought this might be useful: a one-page checklist on {topic}. Want me to send it over?",
    "value_2": "One idea that tends to help {niche}: reply to every new enquiry within five minutes. How quickly do "
               "new enquiries hear back on your side today?",
    "last_note": "I'll leave it here for now so I'm not crowding your inbox. If {topic} becomes a priority, just reply "
                 "and I'll share what has worked.",
    "nurture": "Sharing one useful idea on {topic} this month: a short checklist you can use straight away. Want it?",
}


def playbook(tenant, template: str, context: dict | None = None) -> list[dict]:
    if template == "lead":
        texts = list((context or {}).get("followups") or [])[:2]
        steps = [{"day": d, "key": f"lead_{i + 1}", "text": t} for i, (d, t) in enumerate(zip((2, 4), texts))]
        return steps + [{"day": 7, "key": "last_note"}]
    days = get_settings(tenant)["cadence_days"]
    keys = ["value_1", "value_2", "last_note"] + ["nurture"] * 5
    return [{"day": int(d), "key": keys[i]} for i, d in enumerate(days[:8])]


def render_step(step: dict, context: dict) -> str:
    if step.get("text"):
        return step["text"]
    topic = context.get("topic") or "getting more booked calls"
    niche = context.get("niche") or "businesses like yours"
    return TEMPLATES[step["key"]].format(topic=topic, niche=niche)


def _cadence(tenant, cid: int) -> dict | None:
    with tenant.db() as c:
        r = db.row(c.execute("SELECT * FROM cadences WHERE id=?", (cid,)).fetchone())
    if r:
        r["context"] = db.jload(r["context"], {}) or {}
    return r


def _cad_update(tenant, cid: int, **fields) -> None:
    fields["updated"] = db.now()
    if "context" in fields and not isinstance(fields["context"], str):
        fields["context"] = db.jdump(fields["context"])
    with tenant.db() as c:
        c.execute(f"UPDATE cadences SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), cid))


def register_cadence(tenant, party: tuple[str, int], first_queue_id: int, channel: str, recipient_ref: str | None, *,
                     owner_user_id: int | None = None, template: str = "prospect", lead_tier: str | None = None,
                     context: dict | None = None, demo: bool = False) -> int:
    """Attach a follow-up sequence to a first touch. It starts only when that first touch is confirmed sent."""
    with tenant.db() as c:
        r = c.execute(f"SELECT id FROM cadences WHERE party_type=? AND party_id=? AND status IN "
                      f"({','.join('?' * len(LIVE_CADENCE))}) ORDER BY id DESC LIMIT 1", (party[0], party[1], *LIVE_CADENCE)).fetchone()
        if r:
            return r["id"]
        now = db.now()
        cid = c.execute("INSERT INTO cadences(party_type,party_id,owner_user_id,channel,recipient_ref,template,lead_tier,step,"
                        "status,first_queue_id,context,demo,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (party[0], party[1], owner_user_id, channel, recipient_ref, template, lead_tier, 0,
                         "waiting_first_send", first_queue_id, db.jdump(context or {}), 1 if demo or tenant.is_demo else 0,
                         now, now)).lastrowid
    first = queue.get(tenant, first_queue_id, scrub=False)
    if first and first["status"] == "sent":
        _start(tenant, _cadence(tenant, cid), first.get("sent_at") or db.now())
    return cid


def _start(tenant, cad: dict, sent_at: float) -> None:
    pb = playbook(tenant, cad["template"], cad["context"])
    if not pb:
        _cad_update(tenant, cad["id"], status="done", reason="No follow-up steps are set", first_sent_at=sent_at,
                    last_outbound_at=sent_at)
        return
    _cad_update(tenant, cad["id"], status="active", step=0, first_sent_at=sent_at, last_outbound_at=sent_at,
                due_at=sent_at + pb[0]["day"] * 86400, queue_id=None)


def _advance(tenant, cad: dict, sent_at: float) -> None:
    pb = playbook(tenant, cad["template"], cad["context"])
    step = (cad["step"] or 0) + 1
    if step >= len(pb):
        _cad_update(tenant, cad["id"], status="done", step=step, last_outbound_at=sent_at, queue_id=None,
                    reason="Sequence finished with no reply")
        if cad["party_type"] == "lead":
            with tenant.db() as c:
                c.execute("UPDATE leads SET stage='nurture' WHERE id=? AND stage IN ('new','replied')", (cad["party_id"],))
        events.emit(tenant, "cadence_done", {"cadence_id": cad["id"], "party_type": cad["party_type"],
                                             "party_id": cad["party_id"], "reason": "no_reply"})
        return
    first = cad["first_sent_at"] or sent_at
    due = max(first + pb[step]["day"] * 86400, sent_at + 86400)
    _cad_update(tenant, cad["id"], status="active", step=step, last_outbound_at=sent_at, due_at=due, queue_id=None)


def stop_cadences(tenant, party: tuple[str, int], status: str, reason: str) -> int:
    with tenant.db() as c:
        n = c.execute(f"UPDATE cadences SET status=?, reason=?, updated=? WHERE party_type=? AND party_id=? AND status IN "
                      f"({','.join('?' * len(LIVE_CADENCE))})", (status, reason, db.now(), party[0], party[1],
                                                                  *LIVE_CADENCE)).rowcount
    return n


def stop_cadence(tenant, cid: int, actor: str) -> bool:
    """A person stops a sequence: its next step, if already drafted and not yet sent, is cancelled too."""
    with tenant.db() as c:
        n = c.execute(f"UPDATE cadences SET status='stopped', reason=?, updated=? WHERE id=? AND status IN "
                      f"({','.join('?' * len(LIVE_CADENCE))})", (f"Stopped by {actor}", db.now(), cid, *LIVE_CADENCE)).rowcount
    if n:
        audit.log(tenant, actor, "settings", {"cadence_stopped": cid})
        cad = _cadence(tenant, cid)
        it = queue.get(tenant, cad["queue_id"], scrub=False) if cad and cad.get("queue_id") else None
        if it and it["status"] in queue.OPEN + queue.OUTBOX:
            queue.cancel_system(tenant, it["id"], f"The follow-up sequence was stopped by {actor}, so this was not sent.",
                                actor=actor, from_statuses=(it["status"],))
    return bool(n)


@queue.on_sent("*")
def _cadence_on_sent(tenant, item: dict, actor: str) -> None:
    meta = item.get("meta") or {}
    sent_at = item.get("sent_at") or db.now()
    cid = _int(meta.get("cadence_id"))
    if cid is not None:
        cad = _cadence(tenant, cid)
        if cad and cad["queue_id"] == item["id"] and cad["status"] == "waiting_send":
            _advance(tenant, cad, sent_at)
        return
    with tenant.db() as c:
        rows = db.rows(c.execute("SELECT id FROM cadences WHERE first_queue_id=? AND status='waiting_first_send'", (item["id"],)))
    for r in rows:
        _start(tenant, _cadence(tenant, r["id"]), sent_at)
    if rows or item["kind"] not in OPENER_KINDS or meta.get("cadence") is False:
        return
    party = party_of(meta)
    if party and party[0] == "prospect":
        row = _party_row(tenant, party) or {}
        register_cadence(tenant, party, item["id"], item["channel"], item["recipient_ref"],
                         owner_user_id=_int(meta.get("owner_user_id")), template="prospect",
                         lead_tier=meta.get("lead_tier"), context={"niche": row.get("niche"), "topic": meta.get("topic")},
                         demo=bool(item.get("demo")))


def _cadence_closed(tenant, item: dict, actor: str, why: str) -> None:
    cid = _int((item.get("meta") or {}).get("cadence_id"))
    if cid is None:
        return
    cad = _cadence(tenant, cid)
    if cad and cad["queue_id"] == item["id"] and cad["status"] == "waiting_send":
        _cad_update(tenant, cid, status="stopped", reason=why)


@queue.on_reject("followup")
def _cadence_rejected(tenant, item: dict, actor: str) -> None:
    _cadence_closed(tenant, item, actor, "A follow-up was rejected, so the sequence stopped")


@queue.on_cancel("followup")
def _cadence_cancelled(tenant, item: dict, actor: str) -> None:
    _cadence_closed(tenant, item, actor, "A follow-up was cancelled, so the sequence stopped")


def run_cadences(tenant, now: float | None = None, limit: int = 200) -> dict:
    """Scheduler tick: draft the next follow-up of every sequence that is due."""
    now = now or db.now()
    with tenant.db() as c:
        due = [r["id"] for r in c.execute("SELECT id FROM cadences WHERE status='active' AND due_at<=? ORDER BY due_at LIMIT ?",
                                          (now, limit))]
    if not due:
        return {"due": 0, "drafted": []}
    cap = int(get_settings(tenant)["daily_touch_cap"] or 0)

    def one(run: runtime.Run, cid: int) -> tuple[str, int] | None:
        with tenant.db() as c:  # claim: two ticks never draft the same step
            if c.execute("UPDATE cadences SET status='drafting' WHERE id=? AND status='active'", (cid,)).rowcount != 1:
                return None
        cad = _cadence(tenant, cid)
        party = (cad["party_type"], cad["party_id"])
        if is_dnc(tenant, party) or suppressed(tenant, [cad["recipient_ref"] or ""]):
            _cad_update(tenant, cid, status="stopped", reason="The contact asked not to be contacted")
            return "stopped", cid
        if inbound_since(tenant, party, cad["recipient_ref"], cad["last_outbound_at"] or 0):
            _cad_update(tenant, cid, status="replied", reason="They replied, so the sequence stopped")
            return "stopped", cid
        if sends_today(tenant, cad["owner_user_id"], now) >= cap:
            _cad_update(tenant, cid, status="active", due_at=next_morning(now))
            return "deferred", cid
        pb = playbook(tenant, cad["template"], cad["context"])
        if (cad["step"] or 0) >= len(pb):
            _cad_update(tenant, cid, status="done", reason="Sequence finished with no reply")
            return None
        st = pb[cad["step"] or 0]
        body = render_step(st, cad["context"])
        key = PARTY_META.get(cad["party_type"])
        meta = {"prospect_facing": True, "lead_tier": cad["lead_tier"] or "cold", "prior_contact": True,
                "cadence_id": cid, "cadence_step": (cad["step"] or 0) + 1, "template_key": st["key"],
                "ai_mode": "rules", "cancel_if": ["lead_replied_since", "prospect_dnc", "unsubscribed"]}
        if key:
            meta[key] = cad["party_id"]
        if cad["owner_user_id"] is not None:
            meta["owner_user_id"] = cad["owner_user_id"]
        if (cad["channel"] or "") == "email":  # keep the thread: same subject as the first message
            first = queue.get(tenant, cad["first_queue_id"], scrub=False) if cad.get("first_queue_id") else None
            subj = str(((first or {}).get("meta") or {}).get("subject") or "").strip()
            if subj:
                meta["subject"] = subj if subj.lower().startswith("re:") else f"Re: {subj}"
        item = queue.add(tenant, "followup", body, channel=cad["channel"] or "instagram_dm",
                         recipient_ref=cad["recipient_ref"],
                         title=f"Follow-up {(cad['step'] or 0) + 1}: {party_label(tenant, party)}", meta=meta,
                         run_id=run.id, actor="cadence", demo=bool(cad["demo"]))
        _cad_update(tenant, cid, status="waiting_send", queue_id=item["id"])
        return "drafted", item["id"]

    def fn(run: runtime.Run):
        out: dict[str, list[int]] = {"drafted": [], "stopped": [], "deferred": []}
        for cid in due:
            try:
                res = one(run, cid)
            except Exception as e:  # noqa: BLE001 - one broken sequence must not stop every other one
                from .redact import redact

                log.warning("follow-up sequence %s could not draft its step: %s", cid, redact(f"{type(e).__name__}: {e}")[:300])
                with tenant.db() as c:  # try again in an hour instead of staying stuck in 'drafting'
                    c.execute("UPDATE cadences SET status='active', due_at=?, updated=? WHERE id=? AND status='drafting'",
                              (now + 3600, db.now(), cid))
                continue
            if res:
                out[res[0]].append(res[1])
        drafted, stopped, deferred = out["drafted"], out["stopped"], out["deferred"]
        run.note("draft", {"due": len(due)}, {"drafted": len(drafted), "stopped": len(stopped), "deferred": len(deferred)},
                 label="Draft the next step (template, AI off)")
        return {"due": len(due), "drafted": drafted, "stopped": stopped, "deferred": deferred}

    res = runtime.execute(tenant, "cadence", f"Follow-up sequences ({len(due)} due)",
                          ["Check replies, do-not-contact and today's cap", "Draft the next step (template, AI off)"], fn,
                          "scheduler")
    return {**res["result"], "run_id": res["run_id"]}


run_cadences.demo_too = True


CADENCE_STATUS = {"waiting_first_send": "Waiting for the first message to be sent", "active": "Next step scheduled",
                  "drafting": "Drafting", "waiting_send": "Next step waiting for approval or sending",
                  "replied": "Stopped: they replied", "stopped": "Stopped", "done": "Finished"}


def list_cadences(tenant, *, owner_user_ids: list[int] | None = None, party_types: list[str] | None = None,
                  limit: int = 200) -> list[dict]:
    """owner_user_ids/party_types None = everything (owner). Otherwise rows that match either filter."""
    q, args = "SELECT * FROM cadences", []
    conds = []
    if owner_user_ids is not None:
        conds.append(f"owner_user_id IN ({','.join('?' * len(owner_user_ids))})" if owner_user_ids else "0")
        args += owner_user_ids
    if party_types is not None and party_types:
        conds.append(f"party_type IN ({','.join('?' * len(party_types))})")
        args += party_types
    if conds:
        q += " WHERE " + " OR ".join(conds)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with tenant.db() as c:
        rows = db.rows(c.execute(q, args))
    out = []
    for r in rows:
        pb = playbook(tenant, r["template"], db.jload(r["context"], {}) or {})
        out.append({"id": r["id"], "party_type": r["party_type"], "party_id": r["party_id"],
                    "contact": party_label(tenant, (r["party_type"], r["party_id"])), "channel": r["channel"],
                    "channel_label": CHANNEL_LABEL.get(r["channel"], r["channel"]), "step": r["step"] or 0,
                    "steps": len(pb), "status": r["status"], "status_label": CADENCE_STATUS.get(r["status"], r["status"]),
                    "due_at": r["due_at"] if r["status"] == "active" else None, "queue_id": r["queue_id"],
                    "reason": r["reason"], "owner_user_id": r["owner_user_id"], "demo": r["demo"],
                    "last_outbound_at": r["last_outbound_at"]})
    return out


# --------------------------------------------------------------------------- events
def _party_items(tenant, party: tuple[str, int], statuses: tuple[str, ...]) -> list[dict]:
    key = PARTY_META.get(party[0])
    if not key:
        return []
    marks = ",".join("?" * len(statuses))
    with tenant.db() as c:
        rows = db.rows(c.execute(f"SELECT id, meta, status FROM queue WHERE status IN ({marks}) AND "
                                 f"json_extract(meta,'$.{key}')=? AND COALESCE(json_extract(meta,'$.practice'),0)=0",
                                 (*statuses, party[1])))
    for r in rows:
        r["meta"] = db.jload(r["meta"], {}) or {}
    return rows


def _event_party(payload: dict) -> tuple[str, int] | None:
    pt, pid = payload.get("party_type"), _int(payload.get("party_id"))
    return (pt, pid) if pt in PARTY_META and pid is not None else None


@events.on("reply_received")
def _on_reply(tenant, name: str, payload: dict) -> None:
    party = _event_party(payload)
    if not party:
        return
    stop_cadences(tenant, party, "replied", "They replied, so the sequence stopped")
    for it in _party_items(tenant, party, queue.OPEN + queue.OUTBOX):
        preds = it["meta"].get("cancel_if") or []
        if "lead_replied_since" in preds or it["meta"].get("cadence_id"):
            queue.cancel_system(tenant, it["id"], "They replied before this went out, so it was not sent.",
                                from_statuses=(it["status"],))


@events.on("dnc_set")
def _on_dnc(tenant, name: str, payload: dict) -> None:
    party = _event_party(payload)
    if not party:
        return
    stop_cadences(tenant, party, "stopped", "The contact asked not to be contacted")
    for it in _party_items(tenant, party, queue.OPEN + queue.OUTBOX):
        if it["meta"].get("prospect_facing") or it["meta"].get("cadence_id"):
            queue.cancel_system(tenant, it["id"], "This contact is marked do-not-contact, so it was not sent.",
                                from_statuses=(it["status"],))


@events.on("booking_cancelled")
def _on_booking_cancelled(tenant, name: str, payload: dict) -> None:
    party = _event_party(payload)
    ev = _int(payload.get("event_id"))
    rows: list[dict] = []
    if party:
        rows = _party_items(tenant, party, queue.OPEN + queue.OUTBOX)
    if ev is not None:
        with tenant.db() as c:
            extra = db.rows(c.execute("SELECT id, meta, status FROM queue WHERE status IN ('pending','held','needs_human',"
                                      "'scheduled','awaiting_manual_send') AND json_extract(meta,'$.calendar_event_id')=?",
                                      (ev,)))
        for r in extra:
            r["meta"] = db.jload(r["meta"], {}) or {}
        rows += [r for r in extra if r["id"] not in {x["id"] for x in rows}]
    for it in rows:
        if "booking_cancelled" not in (it["meta"].get("cancel_if") or []):
            continue
        own_ev = _int(it["meta"].get("calendar_event_id"))
        if ev is not None and own_ev is not None and own_ev != ev:
            continue  # a reminder for a different (rescheduled) call with the same person
        queue.cancel_system(tenant, it["id"], "The call was cancelled, so this reminder was not sent.",
                            from_statuses=(it["status"],))


@queue.on_cancel("calendar_invite")
def _invite_cancelled(tenant, item: dict, actor: str) -> None:
    """An approved invite that is cancelled before it went out: the call it booked (and its reminder
    ladder) is cancelled too, so nobody gets 'see you in two hours' for a call that never existed."""
    with tenant.db() as c:
        evs = [r["id"] for r in c.execute("SELECT id FROM calendar_events WHERE queue_id=? AND status!='cancelled'",
                                          (item["id"],))]
    for ev in evs:
        cancel_booking(tenant, ev, actor)


def cancel_booking(tenant, event_id: int, actor: str) -> dict:
    """Cancel a call on the app's calendar: its reminders are cancelled and booking_cancelled is emitted."""
    with tenant.db() as c:
        ev = db.row(c.execute("SELECT * FROM calendar_events WHERE id=?", (event_id,)).fetchone())
        if ev is None:
            raise KeyError(event_id)
        if ev["status"] == "cancelled":
            return {"ok": True, "already": True}
        c.execute("UPDATE calendar_events SET status='cancelled' WHERE id=?", (event_id,))
        if ev.get("party_type") == "chain_lead" and ev.get("party_id"):
            c.execute("UPDATE chain_bookings SET status='cancelled' WHERE lead_id=? AND status IN ('proposed','booked')",
                      (ev["party_id"],))
    audit.log(tenant, actor, "action", {"booking_cancelled": event_id})
    events.emit(tenant, "booking_cancelled", {"party_type": ev.get("party_type"), "party_id": ev.get("party_id"),
                                              "event_id": event_id, "starts_at": ev["start"]})
    return {"ok": True}


# --------------------------------------------------------------------------- demo seed
def seed_demo(tenant) -> None:
    """Fictional outbox examples for the pitch demo (demo database only)."""
    from . import tenancy

    if not tenancy.is_demo_db(tenant):
        return
    with tenant.db() as c:
        leads = db.rows(c.execute("SELECT id, name, handle, email FROM leads WHERE demo=1 AND batch IS NULL ORDER BY id LIMIT 4"))
    if len(leads) < 4:
        return
    for ld in leads:
        add_contact_point(tenant, "lead", ld["id"], "email", ld["email"], verified=True, source="demo seed")
        if ld["handle"]:
            add_contact_point(tenant, "lead", ld["id"], "instagram_dm", ld["handle"], source="demo seed")
    now = db.now()
    actor = "Owner (DEMO)"
    a, b, c_, d = leads
    # scheduled: a reminder that waits for its time
    r = queue.add(tenant, "reminder", "Looking forward to our call. One question to think about before we talk: what "
                  "would ten more booked calls a month change for you?", channel="email", recipient_ref=a["email"],
                  title=f"Reminder before the call: {a['name']}",
                  meta={"lead_id": a["id"], "send_after": next_morning(now, 10), "cancel_if": ["booking_cancelled"],
                        "subject": "Our call", "prior_contact": True, "tag": "seed"}, actor="system", demo=True)
    queue.approve(tenant, r["id"], actor)
    # waiting for a person: Instagram can't be sent by any API
    w = queue.add(tenant, "followup", "Here's the 2-minute video on the reminder ladder I mentioned. Want me to send "
                  "the checklist too?", channel="instagram_dm", recipient_ref=b["handle"], title=f"Value follow-up: {b['name']}",
                  meta={"lead_id": b["id"], "prospect_facing": True, "lead_tier": "warm", "prior_contact": True, "tag": "seed"},
                  actor="system", demo=True)
    queue.approve(tenant, w["id"], actor)
    # sent by hand and confirmed, which starts a follow-up sequence
    s = queue.add(tenant, "setter_reply", "Thanks for the reply. What's the main thing holding new bookings back right "
                  "now?", channel="instagram_dm", recipient_ref=c_["handle"], title=f"Reply to {c_['name']}",
                  meta={"lead_id": c_["id"], "prospect_facing": True, "lead_tier": "warm", "prior_contact": True, "tag": "seed"},
                  actor="system", demo=True)
    register_cadence(tenant, ("lead", c_["id"]), s["id"], "instagram_dm", c_["handle"], template="lead", lead_tier="warm",
                     context={"followups": ["Here's a 2-minute video on confirming every call within five minutes. "
                                            "Want me to send it?",
                                            "Quick tip: a short reminder the day before lifts show rates. What do "
                                            "your reminders look like today?"]}, demo=True)
    queue.approve(tenant, s["id"], actor)
    queue.mark_sent(tenant, s["id"], actor, permalink="https://www.instagram.com/direct/t/demo-thread/")
    # cancelled: the call was cancelled before the reminder went out
    x = queue.add(tenant, "reminder", "See you in two hours. Have your current numbers to hand so we can use the time "
                  "well.", channel="email", recipient_ref=d["email"], title=f"2h reminder: {d['name']}",
                  meta={"lead_id": d["id"], "send_after": now + 3 * 86400, "cancel_if": ["booking_cancelled"],
                        "subject": "See you soon", "prior_contact": True, "tag": "seed"}, actor="system", demo=True)
    queue.approve(tenant, x["id"], actor)
    queue.cancel_system(tenant, x["id"], "The call was cancelled, so this reminder was not sent.")
    suppress(tenant, "unsubscribed.demo@example.invalid", "email", "unsubscribed")
