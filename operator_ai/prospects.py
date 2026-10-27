"""First-client engine: prospects, research, personalised outreach drafts and the pipeline to a won client.

* Every user keeps their own ideal-client profile (ICP), lists and pipeline. The owner sees everyone's; other
  roles see only their own rows.
* Prospects come from a CSV export of any lead tool (flexible headers) or are added by hand. A lawful basis is
  required for every import. Duplicates are found by canonical email, handle and website domain.
* Cross-cohort claims: a prospect one person is working cannot be added by a teammate in the same workspace
  (they only see "already being worked by a teammate"). The owner can release a claim.
* Do-not-contact is permanent and honoured everywhere: imports skip suppressed contacts, open drafts are
  withdrawn and the guardrail plug-in below blocks any draft for that prospect at queue and approval time.
* Research is a list of facts, each with its source: built offline from the provided fields, plus facts a person
  adds with a source, plus (only when the server switch "prospect_web_research" is on) one page of the
  prospect's own website, allowlisted to that domain and only where robots.txt allows it.
* Openers may only cite researched facts: guard_rules() blocks numbers, named offers and quoted phrases that
  are not in the research ("unverified_personalisation").
* Every message is a queue item (kinds prospect_dm, prospect_email, followup, booking_dm) carrying
  meta.owner_user_id so its author can approve it. Nothing here sends anything.
* Daily caps count confirmed sends (queue.on_sent) plus first messages still waiting; bulk drafting stops there.
"""
from __future__ import annotations

import csv
import io
import math
import random
import re
import time
from urllib.parse import urlparse

from . import audit, db, events, guardrails, llm, queue, runtime

FEATURE = "prospects"
STAGES = ["new", "researched", "contacted", "replied", "call_booked", "proposal", "won", "lost", "nurture", "dnc"]
STAGE_LABELS = {"new": "New", "researched": "Researched", "contacted": "Contacted", "replied": "Replied",
                "call_booked": "Call booked", "proposal": "Proposal sent", "won": "Won", "lost": "Lost",
                "nurture": "Nurture", "dnc": "Do not contact"}
EARLY = ("new", "researched")
RELEASED_STAGES = ("lost", "nurture")  # all a holder may still do with a prospect the owner released
MOVES = ["question", "compliment", "resource", "mutual"]
MOVE_LABELS = {"question": "Question", "compliment": "Compliment with a specific", "resource": "Resource offer",
               "mutual": "Mutual connection"}
CHANNELS = {"instagram_dm": "dm", "email": "email"}
CHANNEL_LABELS = {"instagram_dm": "Instagram DM", "email": "Email"}
DEFAULT_CAPS = {"dm": 20, "email": 30}
DEFAULT_WEIGHTS = {"niche": 25, "audience": 20, "ads": 20, "price": 10, "must_haves": 15, "signals": 10}
WEIGHT_LABELS = {"niche": "Niche match", "audience": "Audience size in your band", "ads": "Runs ads (buying signal)",
                 "price": "Their offer price in your band", "must_haves": "Must-haves present",
                 "signals": "Buying signals present"}
MIN_SAMPLE_DEFAULT = 20
FOLLOWUP_DAYS = [2, 4, 7]
CANCEL_IF = ["lead_replied_since", "prospect_dnc"]
LAWFUL_BASES = {
    "legitimate_interest": "Legitimate interest: business contact, assessment recorded",
    "public_business_contact": "Published business contact details (B2B)",
    "consent": "They gave consent to be contacted",
    "existing_relationship": "Existing relationship or they contacted you first",
}
REPLY_KINDS = {"interested": "Interested", "question": "Asked a question", "not_now": "Not right now",
               "not_interested": "Not interested", "unsubscribe": "Asked not to be contacted", "other": "Other"}
OPEN_QUEUE = ("pending", "held", "needs_human", "scheduled", "awaiting_manual_send")
MAX_IMPORT_ROWS = 5000
MAX_COUNT = 10_000_000_000  # follower counts above this are treated as unknown (and never overflow SQLite)


class ProspectError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


def _actor(user: dict | None) -> str:
    return f"{user['username']}({user['role']})" if user else "system"


def _today_start(now: float | None = None) -> float:
    lt = time.localtime(now or time.time())
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def _clean(s, n: int = 500) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()[:n]


# --------------------------------------------------------------------------- canonical identifiers
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_HANDLE_RE = re.compile(r"^[a-z0-9._]{1,30}$")
_PLATFORM_HOSTS = ("instagram.com", "facebook.com", "fb.com", "linkedin.com", "youtube.com", "youtu.be", "tiktok.com",
                   "twitter.com", "x.com", "linktr.ee", "beacons.ai", "calendly.com", "gmail.com", "google.com", "bit.ly",
                   "stan.store", "wa.me", "threads.net", "linkin.bio", "taplink.cc", "lnk.bio")


def canon_email(s: str | None) -> str:
    s = (s or "").strip()
    return queue.canonical_email(s) if _EMAIL_RE.match(s) else ""


def canon_handle(s: str | None) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    if "instagram.com" in s.lower():
        u = urlparse(s if "://" in s else "https://" + s)
        parts = [p for p in u.path.split("/") if p]
        s = parts[0] if parts else ""
    s = s.lstrip("@").strip().lower().rstrip("/")
    return s if _HANDLE_RE.match(s) else ""


def canon_domain(s: str | None) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    try:
        u = urlparse(s if "://" in s else "http://" + s)
        host = (u.hostname or "").lower()
    except ValueError:
        return ""
    if host.startswith("www."):
        host = host[4:]
    if "." not in host or not re.match(r"^[a-z0-9.\-]+$", host):
        return ""
    if any(host == p or host.endswith("." + p) for p in _PLATFORM_HOSTS):
        return ""
    return host


def claim_keys(email: str, handle: str, domain: str) -> list[str]:
    return [k for k in (f"email:{email}" if email else "", f"handle:{handle}" if handle else "",
                        f"domain:{domain}" if domain else "") if k]


def parse_count(v) -> int | None:
    """"12.4k" -> 12400, "1.2M" -> 1200000, "12,400" -> 12400; None when not a count."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v) if 0 <= v <= MAX_COUNT and math.isfinite(v) else None
    m = re.match(r"^\s*([\d,]*\.?\d+)\s*([kKmM])?\+?\s*$", str(v)[:60])
    if not m:
        return None
    try:
        n = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    mult = {"k": 1e3, "m": 1e6}.get((m.group(2) or "").lower(), 1)
    out = n * mult
    return int(round(out)) if math.isfinite(out) and out <= MAX_COUNT else None


def safe_url(s: str | None) -> str | None:
    """An http(s) link to the given website, or None. Anything else (javascript:, data:, ftp: ...) is never
    turned into a link."""
    s = (s or "").strip()
    if not s or not canon_domain(s):
        return None
    if "://" not in s:
        return f"https://{s}"
    return s if re.match(r"^https?://", s, re.I) else None


def ads_active(v) -> bool | None:
    s = _clean(v, 200).lower()
    if not s:
        return None
    if s in ("no", "none", "0", "false", "n", "inactive", "not running", "no ads", "-"):
        return False
    n = parse_count(s)
    if n is not None:
        return n > 0
    if re.search(r"\b(active|yes|running|live|true|y)\b", s) or re.search(r"\d+\s*(active )?ads?\b", s):
        return True
    return None


_PRICE_SIG = re.compile(r"[$£€]\s?(\d[\d,]*(?:\.\d+)?)\s?([kK])?")


def price_signals(text: str) -> list[float]:
    out = []
    for m in _PRICE_SIG.finditer(text or ""):
        try:
            v = float(m.group(1).replace(",", "")) * (1000 if m.group(2) else 1)
        except ValueError:
            continue
        if 0 < v < 10_000_000:
            out.append(v)
    return out


# --------------------------------------------------------------------------- settings
def weights(tenant) -> dict:
    w = dict(DEFAULT_WEIGHTS)
    for k, v in (tenant.get("prospect_fit_weights") or {}).items():
        if k in w:
            try:
                w[k] = max(0, min(100, int(v)))
            except (TypeError, ValueError):
                pass
    return w


def caps(tenant) -> dict:
    c = dict(DEFAULT_CAPS)
    for k, v in (tenant.get("prospect_daily_caps") or {}).items():
        if k in c:
            try:
                c[k] = max(0, min(500, int(v)))
            except (TypeError, ValueError):
                pass
    return c


def min_sample(tenant) -> int:
    try:
        return max(1, min(1000, int(tenant.get("prospect_variant_min_sample", MIN_SAMPLE_DEFAULT))))
    except (TypeError, ValueError):
        return MIN_SAMPLE_DEFAULT


def web_research_enabled(tenant) -> bool:
    return bool(tenant.get("prospect_web_research", False))


def set_web_research(tenant, on: bool, reason: str = "", actor: str = "server-cli") -> None:
    """Server-side only (no API route): turning website research on needs a recorded owner yes."""
    if on and not (reason or "").strip():
        raise ValueError("Turning website research on needs a recorded owner yes")
    tenant.set("prospect_web_research", bool(on))
    audit.log(tenant, actor, "switch", {"key": "prospect_web_research", "to": bool(on), **({"owner_yes": reason} if on else {})})


def save_config(tenant, actor: str, w: dict | None = None, c: dict | None = None, sample: int | None = None) -> dict:
    if w is not None:
        tenant.set("prospect_fit_weights", {k: max(0, min(100, int(v))) for k, v in w.items() if k in DEFAULT_WEIGHTS})
    if c is not None:
        tenant.set("prospect_daily_caps", {k: max(0, min(500, int(v))) for k, v in c.items() if k in DEFAULT_CAPS})
    if sample is not None:
        tenant.set("prospect_variant_min_sample", max(1, min(1000, int(sample))))
    audit.log(tenant, actor, "settings", {"prospects": [k for k, v in (("weights", w), ("caps", c), ("min_sample", sample))
                                                         if v is not None]})
    if w is not None:
        rescore_all(tenant)
    return config_view(tenant)


def config_view(tenant) -> dict:
    return {"weights": weights(tenant), "weight_labels": WEIGHT_LABELS, "caps": caps(tenant), "min_sample": min_sample(tenant),
            "web_research": web_research_enabled(tenant)}


# --------------------------------------------------------------------------- ICP + sender details (per user)
ICP_DEFAULT = {"niche": "", "offer": "", "price_min": None, "price_max": None, "audience_min": None, "audience_max": None,
               "must_haves": [], "disqualifiers": [], "signals": [], "persona": "",
               "sender": {"name": "", "business": "", "postal_address": "", "reply_email": ""}}


def get_icp(tenant, user_id: int) -> dict:
    with tenant.db() as c:
        r = c.execute("SELECT data, updated FROM prospect_icps WHERE user_id=?", (user_id,)).fetchone()
    data = db.jload(r["data"], {}) if r else {}
    out = {**ICP_DEFAULT, **{k: v for k, v in data.items() if k in ICP_DEFAULT}}
    out["sender"] = {**ICP_DEFAULT["sender"], **(data.get("sender") or {})}
    out["updated"] = r["updated"] if r else None
    return out


def _num_or_none(v) -> float | None:
    if v in (None, ""):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ProspectError(422, "Numbers in the ideal client profile must be numbers")
    if f < 0 or not math.isfinite(f):
        raise ProspectError(422, "Numbers in the ideal client profile can't be negative")
    return f


def _words_list(v, n: int = 20) -> list[str]:
    if isinstance(v, str):
        v = re.split(r"[,\n]", v)
    return [_clean(x, 80) for x in (v or []) if _clean(x, 80)][:n]


def save_icp(tenant, user: dict, data: dict) -> dict:
    clean = {
        "niche": _clean(data.get("niche"), 120), "offer": _clean(data.get("offer"), 200),
        "price_min": _num_or_none(data.get("price_min")), "price_max": _num_or_none(data.get("price_max")),
        "audience_min": _num_or_none(data.get("audience_min")), "audience_max": _num_or_none(data.get("audience_max")),
        "must_haves": _words_list(data.get("must_haves")), "disqualifiers": _words_list(data.get("disqualifiers")),
        "signals": _words_list(data.get("signals")), "persona": _clean(data.get("persona"), 600),
        "sender": {k: _clean((data.get("sender") or {}).get(k), 300) for k in ICP_DEFAULT["sender"]},
    }
    for lo, hi in (("price_min", "price_max"), ("audience_min", "audience_max")):
        if clean[lo] is not None and clean[hi] is not None and clean[lo] > clean[hi]:
            raise ProspectError(422, "The minimum can't be bigger than the maximum")
    with tenant.db() as c:
        c.execute("INSERT INTO prospect_icps(user_id,data,updated) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                  "data=excluded.data, updated=excluded.updated", (user["id"], db.jdump(clean), db.now()))
    audit.log(tenant, _actor(user), "settings", {"prospects": "icp"})
    rescore_all(tenant, user["id"])
    return get_icp(tenant, user["id"])


def sender_of(tenant, user_id: int) -> dict:
    s = get_icp(tenant, user_id)["sender"]
    if not s.get("name"):
        with tenant.db() as c:
            r = c.execute("SELECT display, username FROM users WHERE id=?", (user_id,)).fetchone()
        s = {**s, "name": (r["display"] or r["username"]) if r else ""}
    return s


# --------------------------------------------------------------------------- facts (research)
def field_facts(p: dict, source: str, at: float) -> list[dict]:
    """Facts built offline from the fields a person provided (import or manual add)."""
    f = []

    def add(claim, quote, url=None, kind="field"):
        f.append({"claim": claim, "source": source, "source_url": url, "quote": _clean(quote, 400), "fetched_at": at,
                  "kind": kind})

    if p.get("business"):
        add(f"Business name: {p['business']}", p["business"])
    if p.get("niche"):
        add(f"Works in: {p['niche']}", p["niche"])
    if p.get("followers") is not None:
        add(f"Followers: {int(p['followers']):,}", f"{int(p['followers']):,}")
    if p.get("handle"):
        h = canon_handle(p["handle"])
        add(f"Instagram: @{h or p['handle']}", p["handle"], f"https://instagram.com/{h}" if h else None)
    if p.get("website"):
        add(f"Website: {canon_domain(p['website']) or p['website']}", p["website"], safe_url(p["website"]))
    if p.get("offer"):
        add(f"Offer: {p['offer']}", p["offer"])
    if p.get("ad_activity"):
        act = ads_active(p["ad_activity"])
        add("Ad activity: " + ("running ads" if act else "no ads seen" if act is False else str(p["ad_activity"])),
            p["ad_activity"])
    if p.get("notes"):
        add("Bio / notes", p["notes"])
    return f


def _facts(p: dict) -> list[dict]:
    return list((p.get("research") or {}).get("facts") or [])


def facts_corpus(tenant, p: dict, icp: dict | None = None) -> str:
    """Everything a message may reference: the prospect's own fields, the research facts, what they told us in
    their replies, the author's own ICP / sender details (name, business, postal address, reply email) and the
    workspace brand."""
    bits = [p.get(k) or "" for k in ("name", "business", "handle", "website", "niche", "offer", "notes", "ad_activity")]
    if p.get("followers") is not None:
        bits += [str(p["followers"]), f"{int(p['followers']):,}"]
    for f in _facts(p):
        bits += [f.get("claim") or "", f.get("quote") or ""]
    if tenant is not None and p.get("id"):
        bits += [e["detail"].get("text") or "" for e in timeline(tenant, p["id"], 100) if e["kind"] == "reply"]
    if tenant is not None and p.get("owner_user_id"):
        icp = icp or get_icp(tenant, p["owner_user_id"])
        s = sender_of(tenant, p["owner_user_id"])
        bits += [icp.get("niche") or "", icp.get("offer") or ""]
        bits += [s.get(k) or "" for k in ("name", "business", "postal_address", "reply_email")]
    if tenant is not None:
        bits.append(((tenant.get("brand") or {}) or {}).get("name") or "")
    return "\n".join(str(b) for b in bits)


# --------------------------------------------------------------------------- fit score (transparent)
_STOP = {"and", "the", "for", "with", "who", "that", "their", "your", "our", "are", "from", "this", "into", "of", "to", "a",
         "an", "in", "on", "or", "at", "by", "is", "be", "as"}


def _stem(w: str) -> str:
    """Tiny suffix stripper so "coaches", "coaching" and "coach" match."""
    if len(w) > 4:
        for suf in ("ings", "ing", "ers", "er", "es", "s"):
            if w.endswith(suf) and len(w) - len(suf) >= 3:
                w = w[: -len(suf)]
                break
    return w[:-1] if len(w) > 4 and w.endswith("e") else w


def _tokens(s: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(w) >= 3 and w not in _STOP}


def _has_term(text_tokens: set[str], text_low: str, term: str) -> bool:
    t = term.strip().lower()
    if not t:
        return False
    if t in text_low:
        return True
    tt = _tokens(t)
    return bool(tt) and tt <= text_tokens


def score(p: dict, icp: dict, w: dict) -> tuple[int, list[dict]]:
    text = " ".join(str(x) for x in [p.get("niche"), p.get("notes"), p.get("offer"), p.get("business"), p.get("ad_activity")]
                    + [f"{f.get('claim', '')} {f.get('quote', '')}" for f in _facts(p)] if x)
    low, toks = text.lower(), _tokens(text)
    rows = []

    def row(key, frac, why):
        mx = w.get(key, 0)
        rows.append({"key": key, "label": WEIGHT_LABELS[key], "points": round(mx * max(0.0, min(1.0, frac))), "max": mx,
                     "why": why})

    # niche
    nwords = [w for w in re.findall(r"[a-z0-9]+", (icp.get("niche") or "").lower()) if _tokens(w)]
    if not nwords:
        row("niche", 0, "Set your niche in the ideal client profile")
    else:
        ptoks = _tokens(f"{p.get('niche') or ''} {p.get('notes') or ''} {p.get('business') or ''}")
        hit = [w for w in dict.fromkeys(nwords) if _tokens(w) <= ptoks]
        row("niche", len(hit) / len(dict.fromkeys(nwords)), f"Matched: {', '.join(hit)}" if hit else "No niche words in common")
    # audience band
    fo = p.get("followers")
    lo, hi = icp.get("audience_min"), icp.get("audience_max")
    if fo is None:
        row("audience", 0, "Follower count unknown")
    elif lo is None and hi is None:
        row("audience", 0, "Set an audience size band in the ideal client profile")
    else:
        lo_v, hi_v = lo or 0, hi or float("inf")
        if lo_v <= fo <= hi_v:
            row("audience", 1, f"{fo:,} followers is inside your band")
        elif (lo_v * 0.5) <= fo <= (hi_v * 1.5):
            row("audience", 0.5, f"{fo:,} followers is just outside your band")
        else:
            row("audience", 0, f"{fo:,} followers is outside your band")
    # ads
    act = ads_active(p.get("ad_activity"))
    if act is None:
        act = True if re.search(r"\b(running ads|ads running|active ads|ad library)\b", low) else None
    row("ads", 1 if act else 0, "Running ads" if act else "No ads seen" if act is False else "Ad activity unknown")
    # their offer price
    prices = price_signals(f"{p.get('offer') or ''} {p.get('notes') or ''} " + " ".join(f.get("quote") or "" for f in _facts(p)))
    plo, phi = icp.get("price_min"), icp.get("price_max")
    if not prices:
        row("price", 0, "No offer price seen")
    elif plo is None and phi is None:
        row("price", 0, "Set their offer price band in the ideal client profile")
    else:
        top = max(prices)
        inside = (plo or 0) <= top <= (phi or float("inf"))
        row("price", 1 if inside else 0.25, f"Offer priced around {top:,.0f}" + (" (in your band)" if inside else " (outside your band)"))
    # must-haves and signals
    for key, terms, empty in (("must_haves", icp.get("must_haves") or [], "Add must-haves to the ideal client profile"),
                              ("signals", icp.get("signals") or [], "Add buying signals to the ideal client profile")):
        if not terms:
            row(key, 0, empty)
            continue
        hit = [t for t in terms if _has_term(toks, low, t)]
        row(key, len(hit) / len(terms), f"Found: {', '.join(hit)}" if hit else "None found in the research")
    total_max = sum(r["max"] for r in rows) or 1
    total = round(100 * sum(r["points"] for r in rows) / total_max)
    dq = [t for t in (icp.get("disqualifiers") or []) if _has_term(toks, low, t)]
    if dq:
        rows.append({"key": "disqualified", "label": "Disqualifier", "points": -total, "max": 0,
                     "why": f"Matches a disqualifier: {', '.join(dq)}"})
        total = 0
    return max(0, min(100, total)), rows


def _rescore_row(tenant, c, p: dict, icp_cache: dict, w: dict) -> None:
    uid = p["owner_user_id"]
    if uid not in icp_cache:
        icp_cache[uid] = get_icp(tenant, uid)
    s, bd = score(p, icp_cache[uid], w)
    c.execute("UPDATE prospects SET fit_score=?, fit_breakdown=? WHERE id=?", (s, db.jdump(bd), p["id"]))


def rescore_all(tenant, user_id: int | None = None) -> int:
    w = weights(tenant)
    with tenant.db() as c:
        q, args = "SELECT * FROM prospects", []
        if user_id is not None:
            q += " WHERE owner_user_id=?"
            args.append(user_id)
        rows = [_decode(dict(r)) for r in c.execute(q, args)]
        cache: dict = {}
        with db.tx(c):
            for p in rows:
                _rescore_row(tenant, c, p, cache, w)
    return len(rows)


# --------------------------------------------------------------------------- rows, visibility, timeline
def _decode(p: dict) -> dict:
    p["research"] = db.jload(p.get("research"), {}) or {}
    p["fit_breakdown"] = db.jload(p.get("fit_breakdown"), []) or []
    return p


def _row(tenant, pid: int) -> dict | None:
    with tenant.db() as c:
        r = c.execute("SELECT * FROM prospects WHERE id=?", (pid,)).fetchone()
    return _decode(dict(r)) if r else None


def visible(tenant, user: dict, pid: int) -> dict:
    p = _row(tenant, pid)
    if p is None or (user["role"] != "owner" and p["owner_user_id"] != user["id"]):
        raise ProspectError(404, "Not found")
    return p


def _event(tenant, pid: int, kind: str, detail: dict | None = None, actor: str = "system", c=None) -> None:
    args = (pid, kind, db.jdump(detail or {}), actor, db.now())
    sql = "INSERT INTO prospect_events(prospect_id,kind,detail,actor,created) VALUES(?,?,?,?,?)"
    if c is not None:
        c.execute(sql, args)
        return
    with tenant.db() as cc:
        cc.execute(sql, args)


def timeline(tenant, pid: int, limit: int = 200) -> list[dict]:
    with tenant.db() as c:
        rows = db.rows(c.execute("SELECT * FROM prospect_events WHERE prospect_id=? ORDER BY id DESC LIMIT ?", (pid, limit)))
    for r in rows:
        r["detail"] = db.jload(r["detail"], {})
    return rows


def public(p: dict, owner_names: dict | None = None) -> dict:
    out = {k: p.get(k) for k in ("id", "owner_user_id", "list_name", "name", "business", "handle", "email", "website", "niche",
                                 "followers", "offer", "notes", "ad_activity", "fit_score", "stage", "source", "lawful_basis",
                                 "lawful_note", "do_not_contact", "demo", "created", "updated", "claim_released", "client_id",
                                 "lost_reason", "stage_changed")}
    out["stage_label"] = STAGE_LABELS.get(p.get("stage") or "new", p.get("stage"))
    out["lawful_basis_label"] = LAWFUL_BASES.get(p.get("lawful_basis") or "", p.get("lawful_basis") or "")
    out["fit_breakdown"] = p.get("fit_breakdown") or []
    out["facts"] = [{**f, "source_url": f.get("source_url") if re.match(r"^https?://", f.get("source_url") or "", re.I)
                     else None} for f in _facts(p)]
    out["web"] = (p.get("research") or {}).get("web")
    if owner_names is not None:
        out["owner_name"] = owner_names.get(p.get("owner_user_id"), "")
    return out


def _owner_names(tenant) -> dict:
    with tenant.db() as c:
        return {r["id"]: (r["display"] or r["username"]) for r in c.execute("SELECT id, display, username FROM users")}


def list_prospects(tenant, user: dict, list_name: str | None = None, stage: str | None = None, min_fit: int | None = None,
                   q: str | None = None, owner_id: int | None = None, limit: int = 500) -> list[dict]:
    sql, args = "SELECT * FROM prospects WHERE 1=1", []
    if user["role"] != "owner":
        sql += " AND owner_user_id=?"
        args.append(user["id"])
    elif owner_id:
        sql += " AND owner_user_id=?"
        args.append(owner_id)
    if list_name:
        sql += " AND list_name=?"
        args.append(list_name)
    if stage:
        sql += " AND stage=?"
        args.append(stage)
    if min_fit is not None:
        sql += " AND COALESCE(fit_score,0)>=?"
        args.append(min_fit)
    if q:
        sql += " AND (name LIKE ? OR handle LIKE ? OR email LIKE ? OR business LIKE ? OR niche LIKE ?)"
        args += [f"%{q}%"] * 5
    sql += " ORDER BY COALESCE(fit_score,0) DESC, id DESC LIMIT ?"
    args.append(limit)
    with tenant.db() as c:
        rows = [_decode(dict(r)) for r in c.execute(sql, args)]
    names = _owner_names(tenant) if user["role"] == "owner" else None
    return [public(p, names) for p in rows]


def lists(tenant, user: dict) -> list[dict]:
    sql = "SELECT owner_user_id, COALESCE(NULLIF(list_name,''),'My prospects') ln, COUNT(*) n FROM prospects"
    args: list = []
    if user["role"] != "owner":
        sql += " WHERE owner_user_id=?"
        args.append(user["id"])
    sql += " GROUP BY owner_user_id, ln ORDER BY ln"
    names = _owner_names(tenant)
    with tenant.db() as c:
        return [{"name": r["ln"], "count": r["n"], "owner_user_id": r["owner_user_id"],
                 "owner_name": names.get(r["owner_user_id"], "")} for r in c.execute(sql, args)]


# --------------------------------------------------------------------------- adding prospects (dedupe + claims)
FIELDS = ("name", "business", "handle", "email", "website", "niche", "followers", "offer", "notes", "ad_activity")


def _norm_fields(d: dict) -> dict:
    out = {}
    for k in FIELDS:
        v = d.get(k)
        if k == "followers":
            out[k] = parse_count(v)
        else:
            out[k] = _clean(v, 2000 if k == "notes" else 300) or None
    if out.get("handle"):
        h = canon_handle(out["handle"])
        out["handle"] = f"@{h}" if h else out["handle"]
    if out.get("email"):
        out["email"] = out["email"].strip()
    return out


class _Gate:
    """Claims, suppression and never-invite checks against one open connection."""

    def __init__(self, tenant, c):
        self.claims = {r["key"]: r["owner_user_id"] for r in c.execute("SELECT key, owner_user_id FROM prospect_claims")}
        self.suppressed = {r["key"] for r in c.execute("SELECT key FROM prospect_suppression")}
        self.never = queue.blocked_list(tenant)
        # prospects known only by name: duplicates are checked within the same person's list
        self.names = {(r["owner_user_id"], _clean(r["name"], 300).lower()) for r in c.execute(
            "SELECT owner_user_id, name FROM prospects WHERE canon_email IS NULL AND canon_handle IS NULL AND "
            "canon_domain IS NULL")}

    def verdict(self, uid: int, keys: list[str], email: str, handle: str) -> str | None:
        if any(k in self.suppressed for k in keys):
            return "dnc"
        if (email and email in self.never) or (handle and (handle in self.never or f"@{handle}" in self.never)):
            return "never_invite"
        owners = {self.claims[k] for k in keys if k in self.claims}
        if owners - {uid}:
            return "claimed"
        if uid in owners:
            return "duplicate"
        return None


REASONS = {"dnc": "On the do-not-contact list", "never_invite": "On the never-invite list",
           "claimed": "Already being worked by a teammate", "duplicate": "Already in your prospects",
           "no_identity": "No name, handle, email or website", "dupe_in_file": "Duplicate row in this file",
           "bad_email_only": "The only contact detail is an email that isn't valid"}


def _insert(tenant, c, gate: _Gate, uid: int, f: dict, source: str, source_label: str, lawful_basis: str,
            lawful_note: str, list_name: str, import_id: int | None, actor: str) -> tuple[str, int | None]:
    email, handle, domain = canon_email(f.get("email")), canon_handle(f.get("handle")), canon_domain(f.get("website"))
    if not (f.get("name") or f.get("business") or email or handle or domain):
        return ("bad_email_only" if f.get("email") else "no_identity"), None
    if f.get("email") and not email:
        f = {**f, "email": None}
    keys = claim_keys(email, handle, domain)
    name = f.get("name") or f.get("business") or (f"@{handle}" if handle else email or domain)
    if keys:
        v = gate.verdict(uid, keys, email, handle)
    else:
        v = "duplicate" if (uid, _clean(name, 300).lower()) in gate.names else None
    if v:
        return v, None
    if not keys:
        gate.names.add((uid, _clean(name, 300).lower()))
    now = db.now()
    research = {"facts": field_facts(f, source_label, now), "updated": now}
    pid = c.execute(
        "INSERT INTO prospects(owner_user_id,list_name,name,business,handle,email,website,niche,followers,offer,notes,"
        "ad_activity,research,stage,source,lawful_basis,lawful_note,do_not_contact,demo,created,updated,canon_email,"
        "canon_handle,canon_domain,import_id,stage_changed) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,?,?,?)",
        (uid, list_name or "", name, f.get("business"), f.get("handle"), f.get("email"), f.get("website"), f.get("niche"),
         f.get("followers"), f.get("offer"), f.get("notes"), f.get("ad_activity"), db.jdump(research), "new", source,
         lawful_basis, lawful_note, 1 if tenant.is_demo else 0, now, now, email or None, handle or None, domain or None,
         import_id, now)).lastrowid
    for k in keys:
        c.execute("INSERT INTO prospect_claims(key,prospect_id,owner_user_id,created) VALUES(?,?,?,?)", (k, pid, uid, now))
        gate.claims[k] = uid
    _event(tenant, pid, "added", {"source": source_label, "lawful_basis": lawful_basis}, actor, c=c)
    return "ok", pid


def _check_basis(lawful_basis: str, lawful_note: str) -> None:
    if lawful_basis not in LAWFUL_BASES:
        raise ProspectError(422, "Choose the lawful basis for contacting these people")
    if len(_clean(lawful_note)) < 5:
        raise ProspectError(422, "Add a short note on why that lawful basis applies (where the contact details came from)")


def add_prospect(tenant, user: dict, fields: dict, lawful_basis: str, lawful_note: str, list_name: str = "") -> dict:
    _check_basis(lawful_basis, lawful_note)
    f = _norm_fields(fields)
    actor = _actor(user)

    def fn(run: runtime.Run):
        with tenant.db() as c, db.tx(c):
            gate = _Gate(tenant, c)
            status, pid = _insert(tenant, c, gate, user["id"], f, "manual", "Added by hand", lawful_basis,
                                  _clean(lawful_note), _clean(list_name, 80), None, actor)
        run.note("dedupe", {"has_email": bool(f.get("email")), "has_handle": bool(f.get("handle"))},
                 {"result": REASONS.get(status, "added")}, label="Check duplicates, claims and do-not-contact")
        if status != "ok":
            return {"status": status, "message": REASONS[status]}
        _rescore_one(tenant, pid)
        run.note("score", {}, {"prospect_id": pid}, label="Research from the provided fields and score fit")
        return {"status": "ok", "prospect_id": pid}

    out = runtime.execute(tenant, FEATURE, "Add a prospect", ["Check duplicates, claims and do-not-contact",
                                                                  "Research from the provided fields and score fit"], fn, actor)
    res = out["result"]
    if res["status"] != "ok":
        raise ProspectError(409, res["message"])
    audit.log(tenant, actor, "prospects", {"add": res["prospect_id"]})
    return {"prospect": public(_row(tenant, res["prospect_id"])), "run_id": out["run_id"]}


def _rescore_one(tenant, pid: int) -> None:
    p = _row(tenant, pid)
    if p is None:
        return
    with tenant.db() as c:
        _rescore_row(tenant, c, p, {}, weights(tenant))


def update_prospect(tenant, user: dict, pid: int, fields: dict) -> dict:
    p = visible(tenant, user, pid)
    if p["do_not_contact"]:
        raise ProspectError(409, "This prospect asked not to be contacted; their record is locked")
    f = _norm_fields({**{k: p.get(k) for k in FIELDS}, **{k: v for k, v in fields.items() if k in FIELDS}})
    email, handle, domain = canon_email(f.get("email")), canon_handle(f.get("handle")), canon_domain(f.get("website"))
    if f.get("email") and not email:
        raise ProspectError(422, "That email address isn't valid")
    keys = claim_keys(email, handle, domain)
    now = db.now()
    actor = _actor(user)
    with tenant.db() as c, db.tx(c):
        gate = _Gate(tenant, c)
        mine = {r["key"] for r in c.execute("SELECT key FROM prospect_claims WHERE prospect_id=?", (pid,))}
        new = [k for k in keys if k not in mine]
        if any(k in gate.suppressed for k in new):
            raise ProspectError(409, REASONS["dnc"])
        for k in new:
            if k in gate.claims:
                raise ProspectError(409, REASONS["claimed"] if gate.claims[k] != p["owner_user_id"] else
                                    "Another of your prospects already has that contact detail")
        for k in mine - set(keys):
            c.execute("DELETE FROM prospect_claims WHERE key=? AND prospect_id=?", (k, pid))
        if not p.get("claim_released"):
            for k in new:
                c.execute("INSERT INTO prospect_claims(key,prospect_id,owner_user_id,created) VALUES(?,?,?,?)",
                          (k, pid, p["owner_user_id"], now))
        research = p.get("research") or {}
        kept = [x for x in research.get("facts", []) if x.get("kind") != "field"]
        old_field = [x for x in research.get("facts", []) if x.get("kind") == "field"]
        src = old_field[0]["source"] if old_field else "Added by hand"
        research = {**research, "facts": field_facts(f, src, now) + kept, "updated": now}
        c.execute("UPDATE prospects SET name=?, business=?, handle=?, email=?, website=?, niche=?, followers=?, offer=?, notes=?, "
                  "ad_activity=?, research=?, canon_email=?, canon_handle=?, canon_domain=?, list_name=COALESCE(?,list_name), "
                  "updated=? WHERE id=?",
                  (f.get("name") or p["name"], f.get("business"), f.get("handle"), f.get("email"), f.get("website"),
                   f.get("niche"), f.get("followers"), f.get("offer"), f.get("notes"), f.get("ad_activity"), db.jdump(research),
                   email or None, handle or None, domain or None,
                   _clean(fields["list_name"], 80) if "list_name" in fields else None, now, pid))
        _event(tenant, pid, "updated", {"fields": sorted(k for k in fields if k in FIELDS)}, actor, c=c)
    _rescore_one(tenant, pid)
    return public(_row(tenant, pid))


# --------------------------------------------------------------------------- CSV import
HEADER_MAP = {
    "name": ["name", "full name", "fullname", "contact name", "contact", "person", "owner", "founder", "display name"],
    "first_name": ["first name", "firstname", "given name"],
    "last_name": ["last name", "lastname", "surname", "family name"],
    "business": ["business", "business name", "company", "company name", "brand", "account name", "organisation",
                 "organization", "page name"],
    "handle": ["handle", "instagram", "ig", "instagram handle", "ig handle", "username", "instagram username", "ig username",
               "instagram url", "instagram profile", "profile url", "profile", "insta"],
    "email": ["email", "e mail", "email address", "work email", "business email", "contact email", "public email"],
    "website": ["website", "url", "site", "domain", "web", "company website", "website url", "homepage", "link in bio",
                "bio link", "external url"],
    "niche": ["niche", "category", "industry", "vertical", "business category", "sector", "market"],
    "followers": ["followers", "follower count", "followers count", "audience", "audience size", "ig followers",
                  "instagram followers", "subscribers"],
    "notes": ["bio", "biography", "notes", "note", "description", "about", "summary", "headline", "comments"],
    "ad_activity": ["ads", "ad activity", "active ads", "running ads", "ads running", "ad library", "ad count",
                    "meta ads", "facebook ads", "has ads"],
    "offer": ["offer", "product", "programme", "program", "service", "price", "offer price", "pricing"],
}


def _norm_header(h: str) -> str:
    h = (h or "").replace("﻿", "").strip().lower()
    h = re.sub(r"[_\-./]+", " ", h)
    return re.sub(r"\s+", " ", h).strip()


def map_headers(headers: list[str]) -> tuple[list[dict], dict[str, int]]:
    lookup = {syn: field for field, syns in HEADER_MAP.items() for syn in syns}
    cols, used = [], {}
    for i, h in enumerate(headers):
        nh = _norm_header(h)
        field = lookup.get(nh)
        if field is None:
            for syn, fld in sorted(lookup.items(), key=lambda x: -len(x[0])):
                if len(syn) > 3 and re.search(rf"\b{re.escape(syn)}\b", nh):
                    field = fld
                    break
        if field in used:
            field = None
        if field:
            used[field] = i
        cols.append({"header": (h or "").replace("﻿", "").strip()[:80], "used_as": field})
    return cols, used


def parse_csv(text: str) -> tuple[list[str], list[list[str]]]:
    text = (text or "").lstrip("﻿")
    if not text.strip():
        raise ProspectError(422, "The file is empty")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    try:
        rows = list(csv.reader(io.StringIO(text), dialect))
    except csv.Error:
        raise ProspectError(422, "I couldn't read that as a CSV file")
    rows = [r for r in rows if any((x or "").strip() for x in r)]
    if not rows:
        raise ProspectError(422, "The file is empty")
    return rows[0], rows[1:]


def import_csv(tenant, user: dict, text: str, filename: str, list_name: str, lawful_basis: str, lawful_note: str) -> dict:
    _check_basis(lawful_basis, lawful_note)
    headers, rows = parse_csv(text)
    if len(rows) > MAX_IMPORT_ROWS:
        raise ProspectError(422, f"That file has {len(rows):,} rows; import up to {MAX_IMPORT_ROWS:,} at a time")
    cols, used = map_headers(headers)
    if not ({"name", "first_name", "business", "handle", "email", "website"} & set(used)):
        raise ProspectError(422, "I couldn't find a name, handle, email or website column. Rename one of your column "
                                 "headers (for example \"Name\", \"Instagram\", \"Email\" or \"Website\") and try again.")
    actor = _actor(user)
    filename = _clean(filename, 120) or "upload.csv"
    list_name = _clean(list_name, 80) or filename.rsplit(".", 1)[0][:80]
    label = f"Imported list: {filename} ({time.strftime('%d %b %Y')})"

    def cell(r: list[str], field: str) -> str:
        i = used.get(field)
        return (r[i] if i is not None and i < len(r) else "").strip()

    def fn(run: runtime.Run):
        run.note("read", {"filename": filename, "rows": len(rows)},
                 {"columns": [c for c in cols if c["used_as"]], "ignored": [c["header"] for c in cols if not c["used_as"]]},
                 label="Read the file and map the columns")
        imported, skipped, reasons = [], [], {}
        seen: set[str] = set()
        with tenant.db() as c, db.tx(c):
            imp = c.execute("INSERT INTO prospect_imports(owner_user_id,filename,list_name,lawful_basis,lawful_note,summary,"
                            "run_id,demo,created) VALUES(?,?,?,?,?,?,?,?,?)",
                            (user["id"], filename, list_name, lawful_basis, _clean(lawful_note), "{}", run.id,
                             1 if tenant.is_demo else 0, db.now())).lastrowid
            gate = _Gate(tenant, c)
            for n, r in enumerate(rows, start=2):
                name = cell(r, "name") or " ".join(x for x in (cell(r, "first_name"), cell(r, "last_name")) if x)
                f = _norm_fields({"name": name, **{k: cell(r, k) for k in FIELDS if k != "name"}})
                keys = claim_keys(canon_email(f.get("email")), canon_handle(f.get("handle")), canon_domain(f.get("website")))
                if keys and any(k in seen for k in keys):
                    status, pid = "dupe_in_file", None
                else:
                    seen.update(keys)
                    status, pid = _insert(tenant, c, gate, user["id"], f, "csv", label, lawful_basis, _clean(lawful_note),
                                          list_name, imp, actor)
                if status == "ok":
                    imported.append(pid)
                else:
                    reasons[status] = reasons.get(status, 0) + 1
                    if len(skipped) < 100:
                        skipped.append({"row": n, "reason": REASONS[status]})
        run.note("dedupe", {"rows": len(rows)}, {"imported": len(imported), "skipped": reasons},
                 label="Skip duplicates, teammates' claims and do-not-contact")
        w, cache = weights(tenant), {}
        with tenant.db() as c, db.tx(c):
            for pid in imported:
                p = _decode(dict(c.execute("SELECT * FROM prospects WHERE id=?", (pid,)).fetchone()))
                _rescore_row(tenant, c, p, cache, w)
        run.note("score", {"prospects": len(imported)}, "scored", label="Research from the provided fields and score fit")
        warnings = []
        if "email" not in used:
            warnings.append("No email column: email drafts won't be possible for these prospects.")
        if "handle" not in used:
            warnings.append("No Instagram column: DM drafts won't be possible for these prospects.")
        if "niche" not in used and "notes" not in used:
            warnings.append("No niche or bio column: fit scores and personalisation will be thin. Add facts by hand.")
        summary = {"filename": filename, "list_name": list_name, "rows_total": len(rows), "imported": len(imported),
                   "skipped_total": len(rows) - len(imported), "columns": cols, "skipped": skipped,
                   "reasons": [{"reason": REASONS[k], "count": v} for k, v in sorted(reasons.items(), key=lambda x: -x[1])],
                   "warnings": warnings, "lawful_basis": LAWFUL_BASES[lawful_basis], "import_id": imp}
        with tenant.db() as c:
            c.execute("UPDATE prospect_imports SET summary=? WHERE id=?", (db.jdump(summary), imp))
        return summary

    out = runtime.execute(tenant, FEATURE, f"Import prospects: {filename}",
                          ["Read the file and map the columns", "Skip duplicates, teammates' claims and do-not-contact",
                           "Research from the provided fields and score fit"], fn, actor)
    audit.log(tenant, actor, "prospects", {"import": out["result"]["import_id"], "imported": out["result"]["imported"],
                                           "lawful_basis": lawful_basis})
    return {**out["result"], "run_id": out["run_id"]}


def imports(tenant, user: dict, limit: int = 20) -> list[dict]:
    sql, args = "SELECT * FROM prospect_imports", []
    if user["role"] != "owner":
        sql += " WHERE owner_user_id=?"
        args.append(user["id"])
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with tenant.db() as c:
        rows = db.rows(c.execute(sql, args))
    for r in rows:
        r["summary"] = db.jload(r["summary"], {})
        r["lawful_basis_label"] = LAWFUL_BASES.get(r["lawful_basis"], r["lawful_basis"])
    return rows


# --------------------------------------------------------------------------- research
def add_fact(tenant, user: dict, pid: int, claim: str, source: str, source_url: str | None, quote: str) -> dict:
    p = visible(tenant, user, pid)
    claim, source, quote = _clean(claim, 300), _clean(source, 120), _clean(quote, 400)
    if len(claim) < 3 or len(source) < 2:
        raise ProspectError(422, "A fact needs what you saw and where you saw it")
    url = _clean(source_url, 400) or None
    if url and not re.match(r"^https?://", url):
        raise ProspectError(422, "The source link must start with http:// or https://")
    fact = {"claim": claim, "source": source, "source_url": url, "quote": quote or claim, "fetched_at": db.now(),
            "kind": "manual", "added_by": _actor(user)}
    research = p.get("research") or {}
    research = {**research, "facts": _facts(p) + [fact], "updated": db.now()}
    with tenant.db() as c:
        c.execute("UPDATE prospects SET research=?, updated=? WHERE id=?", (db.jdump(research), db.now(), pid))
    _event(tenant, pid, "fact_added", {"claim": claim, "source": source}, _actor(user))
    _rescore_one(tenant, pid)
    return public(_row(tenant, pid))


def research(tenant, user: dict, pid: int, web: bool = False) -> dict:
    p = visible(tenant, user, pid)
    actor = _actor(user)

    def fn(run: runtime.Run):
        now = db.now()
        facts = _facts(p)
        field = [x for x in facts if x.get("kind") == "field"]
        src = field[0]["source"] if field else "Added by hand"
        keep = [x for x in facts if x.get("kind") not in ("field", "web")]
        web_facts = [x for x in facts if x.get("kind") == "web"]
        new_field = run.step("fields", {"fields": [k for k in FIELDS if p.get(k)]},
                             lambda: field_facts(p, src, now), "Facts from the provided fields")
        web_state = (p.get("research") or {}).get("web")
        if web:
            if not web_research_enabled(tenant):
                web_state = {"status": "off", "message": "Website research is switched off for this workspace."}
                run.note("web", {}, web_state, status="skipped", label="Website research (one page, robots.txt honoured)")
            else:
                res = run.step("web", {"domain": canon_domain(p.get("website"))}, lambda: web_research(tenant, p),
                               "Website research (one page, robots.txt honoured)")
                web_state = {k: v for k, v in res.items() if k != "facts"}
                if res.get("facts"):
                    web_facts = res["facts"]
        research_ = {"facts": new_field + web_facts + keep, "updated": now, "web": web_state}
        with tenant.db() as c:
            c.execute("UPDATE prospects SET research=?, updated=? WHERE id=?", (db.jdump(research_), now, pid))
        _rescore_one(tenant, pid)
        q = _row(tenant, pid)
        run.note("score", {}, {"fit": q["fit_score"]}, label="Score fit")
        if q["stage"] == "new":
            set_stage(tenant, pid, "researched", actor)
        _event(tenant, pid, "researched", {"facts": len(research_["facts"]), "web": (web_state or {}).get("status")}, actor)
        return {"facts": len(research_["facts"]), "web": web_state, "fit_score": q["fit_score"]}

    out = runtime.execute(tenant, FEATURE, f"Research: {p['name']}", ["Facts from the provided fields",
                                                                      "Website research (one page, robots.txt honoured)",
                                                                      "Score fit"], fn, actor)
    return {**out["result"], "run_id": out["run_id"], "prospect": public(_row(tenant, pid))}


_UA = "OperatorAI-Research/1.0 (one page, robots.txt honoured)"
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa", ".localdomain", ".intranet", ".corp")


def public_host(domain: str) -> bool:
    """Only public websites are read: never an IP literal that isn't public, a local or internal name, or (on the
    real network) a name that resolves to a private, loopback or link-local address."""
    import ipaddress
    import socket

    host = (domain or "").strip("[]").lower()
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        pass
    if host == "localhost" or host.endswith(_LOCAL_SUFFIXES):
        return False
    from .live import net

    if net._transport is not None:  # an in-process fake transport: nothing real is resolved or reached
        return True
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return False
    try:
        return bool(infos) and all(ipaddress.ip_address(i[4][0].split("%")[0]).is_global for i in infos)
    except ValueError:
        return False


def web_research(tenant, p: dict) -> dict:
    """One page of the prospect's own website. Only the prospect's domain is ever requested; robots.txt is read
    first and a disallow (or an unreadable robots.txt) stops the fetch."""
    domain = canon_domain(p.get("website"))
    if not domain:
        return {"status": "no_website", "message": "No website on record for this prospect.", "facts": []}
    if not public_host(domain):
        return {"status": "blocked", "message": "That address isn't a public website; nothing fetched.", "facts": []}
    from urllib.robotparser import RobotFileParser

    from .live import net

    base = f"https://{domain}"
    try:
        r = net.request(tenant.id, "GET", f"{base}/robots.txt", headers={"user-agent": _UA}, timeout=10)
    except Exception as e:  # noqa: BLE001 - any failure means we don't fetch
        from .redact import redact

        return {"status": "error", "message": redact(f"Couldn't read robots.txt ({type(e).__name__}); nothing fetched."),
                "facts": []}
    rp = RobotFileParser()
    if r.status_code == 200:
        rp.parse(r.text.splitlines())
    elif r.status_code in (404, 410):
        rp.parse([])
    else:
        return {"status": "blocked", "message": f"robots.txt answered {r.status_code}; nothing fetched.", "facts": []}
    url = f"{base}/"
    if not rp.can_fetch(_UA, url):
        return {"status": "blocked", "message": "robots.txt asks automated tools not to read this page; nothing fetched.",
                "facts": []}
    try:
        page = net.request(tenant.id, "GET", url, headers={"user-agent": _UA}, timeout=15)
    except Exception as e:  # noqa: BLE001
        from .redact import redact

        return {"status": "error", "message": redact(f"Couldn't fetch the page ({type(e).__name__})."), "facts": []}
    if page.status_code in (301, 302, 303, 307, 308):
        loc = page.headers.get("location", "")
        target = loc if "://" in loc else base + loc
        if canon_domain(target) != domain:
            return {"status": "blocked", "message": "The page redirects to another site; not followed.", "facts": []}
        if not rp.can_fetch(_UA, target):
            return {"status": "blocked", "message": "robots.txt disallows the redirected page; nothing fetched.", "facts": []}
        url = target
        page = net.request(tenant.id, "GET", target, headers={"user-agent": _UA}, timeout=15)
    if page.status_code != 200:
        return {"status": "error", "message": f"The page answered {page.status_code}.", "facts": []}
    return {"status": "ok", "message": f"Read {url}", "url": url, "facts": extract_page_facts(page.text[:500_000], url)}


def _strip_tags(s: str) -> str:
    import html as _html

    s = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", s)
    return _clean(_html.unescape(re.sub(r"(?s)<[^>]+>", " ", s)), 100_000)


def extract_page_facts(html_text: str, url: str) -> list[dict]:
    import html as _html

    now = db.now()
    facts = []

    def add(claim, quote):
        facts.append({"claim": claim, "source": "Website", "source_url": url, "quote": _clean(quote, 200),
                      "fetched_at": now, "kind": "web"})

    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html_text)
    if m and _clean(_html.unescape(m.group(1))):
        add("Page title", _html.unescape(m.group(1)))
    m = re.search(r'(?is)<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)', html_text)
    if m:
        add("Site description", _html.unescape(m.group(1)))
    m = re.search(r"(?is)<h1[^>]*>(.*?)</h1>", html_text)
    if m and _strip_tags(m.group(1)):
        add("Main headline", _strip_tags(m.group(1)))
    text = _strip_tags(html_text)
    low = text.lower() + " " + html_text.lower()
    for pat, claim in ((r"calendly\.com|book a call|book your call|apply now|schedule a call", "Has a booking or application step"),
                       (r"webinar|masterclass|free training|live training", "Mentions a webinar or free training"),
                       (r"watch the video|watch now|vsl", "Has a video sales page"),
                       (r"fbq\(|connect\.facebook\.net|facebook\.com/tr", "Meta pixel present (likely runs or plans ads)")):
        mm = re.search(pat, low)
        if mm:
            i = text.lower().find(mm.group(0))
            add(claim, text[max(0, i - 60): i + 100] if i >= 0 else mm.group(0))
    prices = price_signals(text)
    if prices:
        mm = _PRICE_SIG.search(text)
        add(f"Shows a price on the page (around {max(prices):,.0f})", text[max(0, mm.start() - 60): mm.end() + 60])
    return facts[:12]


# --------------------------------------------------------------------------- stages, DNC, release, won
def _emit_stage(tenant, p: dict, stage: str) -> None:
    events.emit(tenant, "prospect_stage", {"prospect_id": p["id"], "stage": stage, "owner_user_id": p["owner_user_id"]})


def set_stage(tenant, pid: int, stage: str, actor: str, reason: str = "", user: dict | None = None) -> dict:
    if stage not in STAGES:
        raise ProspectError(422, "Unknown stage")
    p = visible(tenant, user, pid) if user else _row(tenant, pid)
    if p is None:
        raise ProspectError(404, "Not found")
    if stage == "dnc":
        return set_dnc(tenant, pid, actor, reason or "Marked do not contact")
    if p["stage"] == "dnc" or p["do_not_contact"]:
        raise ProspectError(409, "This prospect asked not to be contacted; the stage can't change")
    if user is not None and p.get("claim_released") and stage not in RELEASED_STAGES and stage != p["stage"]:
        raise ProspectError(409, "The owner released this prospect to the team. From this list it can only be marked "
                                 "lost or nurture.")
    if stage == "won":
        return mark_won(tenant, pid, actor, via="manual", note=reason)
    if p["stage"] == stage:
        return public(p)
    with tenant.db() as c:
        c.execute("UPDATE prospects SET stage=?, stage_changed=?, updated=?, lost_reason=? WHERE id=?",
                  (stage, db.now(), db.now(), _clean(reason, 300) if stage == "lost" else p.get("lost_reason"), pid))
    _event(tenant, pid, "stage", {"from": p["stage"], "to": stage, "reason": _clean(reason, 300)}, actor)
    _emit_stage(tenant, p, stage)
    return public(_row(tenant, pid))


def _withdraw_open_drafts(tenant, pid: int, actor: str, reason: str, touches: tuple[str, ...] | None = None) -> list[int]:
    with tenant.db() as c:
        rows = c.execute("SELECT id, meta FROM queue WHERE status IN ('pending','held','needs_human') AND "
                         "json_extract(meta,'$.prospect_id')=? AND json_extract(meta,'$.feature')=?", (pid, FEATURE)).fetchall()
    out = []
    for r in rows:
        m = db.jload(r["meta"], {})
        if touches is not None and m.get("touch") not in touches:
            continue
        try:
            queue.reject(tenant, r["id"], actor, role="owner", reason=reason)
            out.append(r["id"])
        except queue.QueueError:
            pass
    return out


def set_dnc(tenant, pid: int, actor: str, reason: str = "", emit: bool = True) -> dict:
    p = _row(tenant, pid)
    if p is None:
        raise ProspectError(404, "Not found")
    if p["do_not_contact"]:
        return public(p)
    keys = claim_keys(p.get("canon_email") or "", p.get("canon_handle") or "", p.get("canon_domain") or "")
    with tenant.db() as c, db.tx(c):
        c.execute("UPDATE prospects SET do_not_contact=1, stage='dnc', stage_changed=?, updated=? WHERE id=?",
                  (db.now(), db.now(), pid))
        for k in keys:
            c.execute("INSERT OR IGNORE INTO prospect_suppression(key,prospect_id,reason,created) VALUES(?,?,?,?)",
                      (k, pid, _clean(reason, 200), db.now()))
        _event(tenant, pid, "dnc", {"reason": _clean(reason, 300)}, actor, c=c)
    withdrawn = _withdraw_open_drafts(tenant, pid, actor, "Prospect asked not to be contacted")
    audit.log(tenant, actor, "prospects", {"dnc": pid, "withdrawn": withdrawn})
    _emit_stage(tenant, p, "dnc")
    if emit:
        events.emit(tenant, "dnc_set", {"party_type": "prospect", "party_id": pid, "source": FEATURE})
    return public(_row(tenant, pid))


def release_claim(tenant, pid: int, actor: str) -> dict:
    p = _row(tenant, pid)
    if p is None:
        raise ProspectError(404, "Not found")
    with tenant.db() as c, db.tx(c):
        n = c.execute("DELETE FROM prospect_claims WHERE prospect_id=?", (pid,)).rowcount
        c.execute("UPDATE prospects SET claim_released=1, updated=? WHERE id=?", (db.now(), pid))
        _event(tenant, pid, "released", {"claims": n}, actor, c=c)
    withdrawn = _withdraw_open_drafts(tenant, pid, actor, "Claim released by the owner")
    audit.log(tenant, actor, "prospects", {"release": pid, "withdrawn": withdrawn})
    return public(_row(tenant, pid))


def mark_won(tenant, pid: int, actor: str, price: float | None = None, offer: str | None = None, via: str = "manual",
             note: str = "") -> dict:
    p = _row(tenant, pid)
    if p is None:
        raise ProspectError(404, "Not found")
    if p["do_not_contact"]:
        raise ProspectError(409, "This prospect asked not to be contacted")
    if p.get("client_id"):
        return public(p)
    with tenant.db() as c:
        u = c.execute("SELECT student_id FROM users WHERE id=?", (p["owner_user_id"],)).fetchone()
    with tenant.db() as c, db.tx(c):
        cid = c.execute("INSERT INTO clients(student_id,name,niche,offer,price,metrics,demo,created) VALUES(?,?,?,?,?,?,?,?)",
                        (u["student_id"] if u else None, p.get("business") or p["name"], p.get("niche"),
                         _clean(offer, 200) or p.get("offer"), price, "{}", 1 if (tenant.is_demo or p.get("demo")) else 0,
                         db.now())).lastrowid
        c.execute("UPDATE prospects SET stage='won', client_id=?, stage_changed=?, updated=? WHERE id=?",
                  (cid, db.now(), db.now(), pid))
        _event(tenant, pid, "won", {"client_id": cid, "via": via, "note": _clean(note, 300)}, actor, c=c)
    audit.log(tenant, actor, "prospects", {"won": pid, "client_id": cid, "via": via})
    _emit_stage(tenant, p, "won")
    events.emit(tenant, "client_won", {"client_id": cid, "prospect_id": pid, "owner_user_id": p["owner_user_id"]})
    return public(_row(tenant, pid))


# --------------------------------------------------------------------------- drafting
def tier_for(stage: str) -> str:
    return "warm" if stage in ("replied", "call_booked", "proposal", "won") else "cold"


def _first_name(p: dict) -> str:
    n = re.sub(r"\(DEMO\)", "", p.get("name") or "").strip()
    if not n or n == (p.get("business") or "").strip() or n.startswith("@") or "@" in n:
        return ""
    first = n.split()[0]
    return first if re.match(r"^[A-Za-z][A-Za-z'\-]{1,20}$", first) else ""


def _biz(p: dict) -> str:
    return re.sub(r"\s*\(DEMO\)", "", p.get("business") or "").strip() or "your business"


def _quote_candidates(p: dict) -> list[str]:
    """Short phrases taken word for word from the research, usable as a specific (no digits or money)."""
    out = []
    for f in _facts(p):
        if f.get("kind") == "field" and not f.get("claim", "").startswith(("Bio", "Offer")):
            continue
        for sent in re.split(r"(?<=[.!?|•])\s+|\n", f.get("quote") or ""):
            s = sent.strip().strip("\"'“”").rstrip(".!?|• ").strip()
            words = s.split()
            if 3 <= len(words) <= 14 and not re.search(r"[\d$£€@\"]", s) and "http" not in s.lower():
                out.append(s)
    return out


_MUTUAL = re.compile(r"\b(mutual|both in|same group|same community|introduced|referred by|met at)\b", re.I)


def _mutual_fact(p: dict) -> str | None:
    """The sentence (word for word) that records a real connection in common, if one is on record."""
    for f in _facts(p):
        for part in (f.get("quote") or "", f.get("claim") or ""):
            for sent in re.split(r"(?<=[.!?])\s+|\n", part):
                if _MUTUAL.search(sent):
                    q = _clean(sent, 120).rstrip(".!? ")
                    if q and not re.search(r"[\d$£€\"()]", q):
                        return q
    return None


def _ads_fact(p: dict) -> bool:
    if ads_active(p.get("ad_activity")):
        return True
    return any(f.get("kind") in ("web", "manual") and re.search(r"\b(pixel|ads? running|running ads|ad library)\b",
                                                                   f"{f.get('claim')} {f.get('quote')}", re.I) for f in _facts(p))


def _what_you_do(icp: dict) -> str:
    niche, offer = (icp.get("niche") or "").strip(), (icp.get("offer") or "").strip()
    if niche and offer:
        return f"I help {niche} with {offer}."
    if offer:
        return f"I help businesses like yours with {offer}."
    return "I help businesses like yours turn attention into booked calls."


def opener_variants(tenant, p: dict, channel: str, icp: dict | None = None) -> list[dict]:
    """Template openers by move, using only researched facts (rules; AI not used)."""
    icp = icp or get_icp(tenant, p["owner_user_id"])
    first, biz = _first_name(p), _biz(p)
    hi = f"Hi {first}," if first else "Hi there,"
    niche = (p.get("niche") or icp.get("niche") or "your space").strip().lower()
    quotes = _quote_candidates(p)
    mutual = _mutual_fact(p)
    out = []

    def add(move, obs, available=True, reason=""):
        out.append({"move": move, "label": MOVE_LABELS[move], "obs": obs, "available": available, "reason": reason})

    add("question", "Quick question: when someone new finds you, what happens next? A DM, a call, or a link? "
                    f"Curious how you handle that for {niche}.")
    if quotes:
        add("compliment", f"Your line \"{quotes[0]}\" stood out. Most accounts at your stage lose people between the first "
                          "message and a booked call. Is that something you're working on?")
    elif _ads_fact(p):
        add("compliment", "Saw you have ads running at the moment. When ads are live, the leak is usually between the click "
                          "and a booked call. How's that part going?")
    else:
        add("compliment", "", False, "Add a researched fact (a bio line, a launch or an ad you saw) to personalise this one")
    add("resource", f"I can send over a short checklist on turning {niche} followers into booked calls, no strings. "
                    "Would that be useful?")
    if mutual:
        add("mutual", f"We have a connection in common ({_lower_first(mutual)}), so I wanted to say hello. How are you "
                      f"finding growth on the {niche} side at the moment?")
    else:
        add("mutual", "", False, "No mutual connection on record. Add one as a fact (where you know them from) to use this")
    for v in out:
        if not v["available"]:
            v["text"] = ""
            continue
        if channel == "email":
            v["subject"] = f"Question about {biz}" if biz != "your business" else "A quick question"
            v["text"] = email_body(tenant, p, f"{hi}\n\n{v['obs']}\n\n{_what_you_do(icp)} Would a short conversation be useful?")
        else:
            v["text"] = f"{hi} {_lower_first(v['obs'])}"
    return out


def _lower_first(s: str) -> str:
    return s if re.match(r"^I\b", s) else s[:1].lower() + s[1:]


def email_body(tenant, p: dict, main: str) -> str:
    s = sender_of(tenant, p["owner_user_id"])
    sig = "\n".join(x for x in (s.get("name"), s.get("business"), s.get("reply_email")) if x)
    postal = s.get("postal_address") or "[[postal address needed: add it under Prospects > Ideal client > Sender details]]"
    return (f"{main}\n\n{sig}\n--\n{postal}\nIf this isn't relevant, reply \"no\" and I won't contact you again. "
            "Unsubscribe: {{unsubscribe_url}}")


FOLLOWUP_TEXTS = [
    "One idea for {niche}: answer every new enquiry within five minutes and offer two specific times instead of a link. "
    "It's one of the simplest fixes for slow bookings. Want the short checklist?",
    "A question that helps spot leaks: of your last ten enquiries, how many became a booked call? Happy to share how I'd "
    "look at that if it's useful.",
    "I'll leave it here so I'm not cluttering your inbox. If more booked calls for {biz} becomes a priority later, just "
    "reply and I'll pick it up.",
]


def _meta(p: dict, channel: str, touch: str, **extra) -> dict:
    return {"feature": FEATURE, "owner_user_id": p["owner_user_id"], "prospect_id": p["id"], "prospect_facing": True,
            "lead_tier": tier_for(p["stage"]), "touch": touch, "channel_family": CHANNELS.get(channel, "dm"), **extra}


def _recipient(p: dict, channel: str) -> str | None:
    if channel == "email":
        return p.get("email") if canon_email(p.get("email")) else None
    h = canon_handle(p.get("handle"))
    return f"@{h}" if h else None


def never_invited(tenant, p: dict) -> bool:
    """True when the prospect's email or Instagram handle is on the workspace's never-invite list (checked
    live, so a name added after the import still counts)."""
    bl = queue.blocked_list(tenant)
    if not bl:
        return False
    email = p.get("canon_email") or canon_email(p.get("email"))
    handle = p.get("canon_handle") or canon_handle(p.get("handle"))
    return bool((email and email in bl) or (handle and (handle in bl or f"@{handle}" in bl)))


def _check_contactable(p: dict, channel: str, tenant=None) -> None:
    if channel not in CHANNELS:
        raise ProspectError(422, "Choose Instagram DM or email")
    if p["do_not_contact"]:
        raise ProspectError(409, "This prospect asked not to be contacted")
    if tenant is not None and never_invited(tenant, p):
        raise ProspectError(409, REASONS["never_invite"])
    if p.get("claim_released"):
        raise ProspectError(409, "The owner released this prospect to the team; it can't be messaged from this list")
    if p["stage"] in ("won", "lost"):
        raise ProspectError(409, f"This prospect is marked {STAGE_LABELS[p['stage']].lower()}")
    if not _recipient(p, channel):
        raise ProspectError(409, "No Instagram handle on record" if channel != "email" else "No valid email on record")


def suggestions(tenant, user: dict, pid: int, channel: str = "instagram_dm", use_ai: bool = False) -> dict:
    p = visible(tenant, user, pid)
    _check_contactable(p, channel, tenant)
    actor = _actor(user)

    def fn(run: runtime.Run):
        variants = run.step("templates", {"channel": channel, "facts": len(_facts(p))},
                            lambda: opener_variants(tenant, p, channel), "Opener variants from researched facts")
        mode, label = "rules", "Templates from the research (AI not used)"
        reason = llm.unavailable_reason(tenant)
        if use_ai and reason is None:
            mode, label = _ai_polish(tenant, run, p, channel, variants)
        elif use_ai:
            label = f"Templates from the research ({reason})"
        shown, hidden = [], 0
        for v in variants:
            if not v["available"]:
                shown.append({**v, "flags": []})
                continue
            meta = _meta(p, channel, "opener", move=v["move"], **({"subject": v["subject"]} if v.get("subject") else {}))
            text = f"Subject: {v['subject']}\n{v['text']}" if v.get("subject") else v["text"]
            flags = guardrails.check_for(tenant, text, meta)
            if guardrails.blocked(flags):
                hidden += 1
                continue
            shown.append({**v, "flags": flags})
        run.note("guardrails", {}, {"shown": len(shown), "hidden": hidden}, label="Guardrails on every suggestion")
        return {"variants": shown, "hidden": hidden, "mode": mode, "label": label, "channel": channel,
                "recommended": choose_move(tenant, p["owner_user_id"], [v["move"] for v in shown if v["available"]],
                                           CHANNELS[channel])}

    out = runtime.execute(tenant, FEATURE, f"Opener ideas: {p['name']}", ["Opener variants from researched facts",
                                                                         "Guardrails on every suggestion"], fn, actor)
    return {**out["result"], "run_id": out["run_id"]}


def _ai_polish(tenant, run, p: dict, channel: str, variants: list[dict]) -> tuple[str, str]:
    facts = "\n".join(f"- {f.get('claim')}: {f.get('quote')}" for f in _facts(p))[:3000]
    used = "rules"
    for v in variants:
        if not v["available"]:
            continue
        prompt = (f"Rewrite this first message so it reads naturally. Under 60 words, one question, no links, no prices, no "
                  f"numbers, no claims about results. Use only these researched facts:\n{facts}\n\nMessage:\n{v['obs']}")
        try:
            r = llm.complete_or_replay(tenant, "You edit cold outreach openers. Only use facts given. Plain text only.",
                                       prompt, purpose=f"prospect_opener:{v['move']}", max_tokens=200, run_id=run.id)
        except llm.AIUnavailable:
            continue
        txt = _clean(r.get("text"), 600)
        if not txt:
            continue
        first = _first_name(p)
        body = f"Hi {first}, {txt}" if first and not txt.lower().startswith("hi") else txt
        meta = _meta(p, channel, "opener", move=v["move"])
        if channel == "email":
            body = email_body(tenant, p, f"{body}\n\n{_what_you_do(get_icp(tenant, p['owner_user_id']))}")
        if guardrails.blocked(guardrails.check_for(tenant, body, meta)):
            continue  # the AI version made something up or broke a rule: keep the template
        v["text"] = body
        used = r.get("mode", "live")
        run.use_ai("replay" if used == "replay" else "live")
    if used == "rules":
        return "rules", "Templates from the research (the AI versions didn't pass the guardrails)"
    return used, "AI-polished from the research" if used == "live" else "Replayed AI draft (labelled)"


def usage(tenant, user_id: int) -> dict:
    t0 = _today_start()
    cp = caps(tenant)
    out = {}
    with tenant.db() as c:
        for fam in cp:
            sent = c.execute("SELECT COUNT(*) FROM prospect_touches WHERE owner_user_id=? AND family=? AND sent_at>=?",
                             (user_id, fam, t0)).fetchone()[0]
            waiting = c.execute(
                "SELECT COUNT(*) FROM prospect_touches t JOIN queue q ON q.id=t.queue_id WHERE t.owner_user_id=? AND "
                f"t.family=? AND t.touch='opener' AND t.sent_at IS NULL AND q.status IN ({','.join('?' * len(OPEN_QUEUE))})",
                (user_id, fam, *OPEN_QUEUE)).fetchone()[0]
            out[fam] = {"cap": cp[fam], "sent_today": sent, "waiting": waiting, "remaining": max(0, cp[fam] - sent - waiting)}
    return out


def _queue_touch(tenant, p: dict, channel: str, touch: str, body: str, title: str, actor: str, run_id: int | None,
                 move: str | None = None, step: int | None = None, extra_meta: dict | None = None) -> dict:
    meta = _meta(p, channel, touch, **({"move": move} if move else {}), **({"step": step} if step else {}), **(extra_meta or {}))
    item = queue.add(tenant, {"opener": "prospect_email" if channel == "email" else "prospect_dm", "followup": "followup",
                              "booking": "booking_dm"}[touch], body, channel=channel, recipient_ref=_recipient(p, channel),
                     title=title, meta=meta, run_id=run_id, actor=actor)
    with tenant.db() as c:
        c.execute("INSERT INTO prospect_touches(prospect_id,owner_user_id,queue_id,touch,move,step,channel,family,demo,created) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?)", (p["id"], p["owner_user_id"], item["id"], touch, move, step, channel,
                                                  CHANNELS.get(channel, "dm"), 1 if tenant.is_demo else 0, db.now()))
    _event(tenant, p["id"], "drafted", {"queue_id": item["id"], "touch": touch, "move": move, "channel": channel,
                                        "status": item["status"]}, actor)
    return item


def _has_open_opener(tenant, pid: int) -> bool:
    with tenant.db() as c:
        return c.execute(
            "SELECT 1 FROM prospect_touches t JOIN queue q ON q.id=t.queue_id WHERE t.prospect_id=? AND t.touch='opener' AND "
            f"(t.sent_at IS NOT NULL OR q.status IN ({','.join('?' * len(OPEN_QUEUE))}) OR q.status='approved') LIMIT 1",
            (pid, *OPEN_QUEUE)).fetchone() is not None


def had_contact(tenant, pid: int) -> bool:
    """True when there really was contact: a message to them was confirmed sent, or they replied. Only then may a
    message say "following up" or carry a "Re:" subject."""
    with tenant.db() as c:
        return c.execute(
            "SELECT 1 FROM prospect_touches WHERE prospect_id=? AND sent_at IS NOT NULL UNION ALL "
            "SELECT 1 FROM prospect_events WHERE prospect_id=? AND kind='reply' LIMIT 1", (pid, pid)).fetchone() is not None


def draft_opener(tenant, user: dict, pid: int, channel: str, move: str, text: str | None = None,
                 subject: str | None = None) -> dict:
    p = visible(tenant, user, pid)
    _check_contactable(p, channel, tenant)
    if move not in MOVES:
        raise ProspectError(422, "Unknown opener style")
    if p["stage"] not in EARLY + ("nurture",):
        raise ProspectError(409, "This prospect has already been contacted; use follow-ups or record their reply")
    if _has_open_opener(tenant, pid):
        raise ProspectError(409, "There's already a first message for this prospect")
    if usage(tenant, p["owner_user_id"])[CHANNELS[channel]]["remaining"] <= 0:
        raise ProspectError(409, "You've reached today's cap for new messages on this channel")
    actor = _actor(user)

    def fn(run: runtime.Run):
        v = next((x for x in opener_variants(tenant, p, channel) if x["move"] == move), None)
        if text is None and (v is None or not v["available"]):
            raise ProspectError(409, (v or {}).get("reason") or "That opener style isn't available for this prospect")
        body = _clean_body(text) if text is not None else v["text"]
        subj = _clean(subject, 150) if subject is not None else (v or {}).get("subject")
        run.note("draft", {"move": move, "edited": text is not None}, {"chars": len(body)}, label="Draft the opener")
        item = _queue_touch(tenant, p, channel, "opener", body, f"{MOVE_LABELS[move]} opener to {p['name']}", actor, run.id,
                            move=move, extra_meta={"subject": subj} if channel == "email" and subj else None)
        run.note("queue", {}, {"queue_id": item["id"], "status": item["status"], "flags": [f["code"] for f in item["flags"]]},
                 label="Queue for approval")
        return {"queue_id": item["id"], "status": item["status"], "flags": item["flags"]}

    out = runtime.execute(tenant, FEATURE, f"Opener: {p['name']}", ["Draft the opener", "Queue for approval"], fn, actor)
    return {**out["result"], "run_id": out["run_id"]}


def _clean_body(text: str | None) -> str:
    t = (text or "").replace("\r\n", "\n").strip()
    if not t:
        raise ProspectError(422, "The message is empty")
    return t[:5000]


def draft_openers_bulk(tenant, user: dict, channel: str = "instagram_dm", ids: list[int] | None = None,
                       list_name: str | None = None, limit: int = 20, seed: int | None = None) -> dict:
    if channel not in CHANNELS:
        raise ProspectError(422, "Choose Instagram DM or email")
    actor = _actor(user)
    rng = random.Random(seed)

    def fn(run: runtime.Run):
        # Bulk drafting always works on the caller's own list, the owner included: it must never fill a
        # teammate's approval queue or use up their daily cap.
        sql, args = "SELECT * FROM prospects WHERE stage IN ('new','researched') AND do_not_contact=0 AND " \
                    "COALESCE(claim_released,0)=0 AND owner_user_id=?", [user["id"]]
        if ids:
            sql += f" AND id IN ({','.join('?' * len(ids))})"
            args += ids
        if list_name:
            sql += " AND list_name=?"
            args.append(list_name)
        sql += " ORDER BY COALESCE(fit_score,0) DESC, id LIMIT 1000"
        with tenant.db() as c:
            rows = [_decode(dict(r)) for r in c.execute(sql, args)]
        run.note("pick", {"channel": channel, "limit": limit}, {"candidates": len(rows)}, label="Pick prospects by fit")
        queued, skipped = [], []
        for p in rows:
            if len(queued) >= limit:
                break
            if not _recipient(p, channel):
                skipped.append({"prospect_id": p["id"], "name": p["name"], "reason": "No contact detail for this channel"})
                continue
            if never_invited(tenant, p):
                skipped.append({"prospect_id": p["id"], "name": p["name"], "reason": REASONS["never_invite"]})
                continue
            if _has_open_opener(tenant, p["id"]):
                skipped.append({"prospect_id": p["id"], "name": p["name"], "reason": "Already has a first message"})
                continue
            if usage(tenant, p["owner_user_id"])[CHANNELS[channel]]["remaining"] <= 0:
                skipped.append({"prospect_id": p["id"], "name": p["name"], "reason": "Daily cap reached"})
                continue
            variants = [v for v in opener_variants(tenant, p, channel) if v["available"]]
            pick = choose_move(tenant, p["owner_user_id"], [v["move"] for v in variants], CHANNELS[channel], rng)
            v = next(x for x in variants if x["move"] == pick["move"])
            item = _queue_touch(tenant, p, channel, "opener", v["text"], f"{MOVE_LABELS[v['move']]} opener to {p['name']}",
                                actor, run.id, move=v["move"],
                                extra_meta={"subject": v["subject"]} if v.get("subject") else None)
            queued.append({"prospect_id": p["id"], "queue_id": item["id"], "move": v["move"], "status": item["status"]})
        run.note("queue", {}, {"queued": len(queued), "skipped": len(skipped)}, label="Queue openers within the daily cap")
        return {"queued": queued, "skipped": skipped, "usage": usage(tenant, user["id"])}

    out = runtime.execute(tenant, FEATURE, f"Draft openers ({CHANNEL_LABELS[channel]})",
                          ["Pick prospects by fit", "Queue openers within the daily cap"], fn, actor)
    return {**out["result"], "run_id": out["run_id"]}


def draft_followups(tenant, pid: int, base_time: float, actor: str = "system") -> list[int]:
    """Value-only follow-ups on day 2/4/7 after the first message really went out. Each one waits for approval,
    carries send_after and is cancelled if the prospect replies or opts out."""
    p = _row(tenant, pid)
    if p is None or p["do_not_contact"] or p.get("claim_released"):
        return []
    with tenant.db() as c:
        if c.execute("SELECT 1 FROM prospect_touches WHERE prospect_id=? AND touch='followup' LIMIT 1", (pid,)).fetchone():
            return []
        opener = c.execute("SELECT channel, queue_id FROM prospect_touches WHERE prospect_id=? AND touch='opener' "
                           "ORDER BY sent_at DESC, id DESC LIMIT 1", (pid,)).fetchone()
    channel = opener["channel"] if opener else ("email" if _recipient(p, "email") else "instagram_dm")
    if not _recipient(p, channel):
        return []
    subj = None
    if opener and channel == "email":
        q = queue.get(tenant, opener["queue_id"], scrub=False)
        subj = (q or {}).get("meta", {}).get("subject")
    icp = get_icp(tenant, p["owner_user_id"])
    niche = (p.get("niche") or icp.get("niche") or "businesses like yours").strip().lower()
    prior = had_contact(tenant, pid)  # "following up" wording only when a first message really went out
    ids = []

    def fn(run: runtime.Run):
        for i, (days, tpl) in enumerate(zip(FOLLOWUP_DAYS, FOLLOWUP_TEXTS), start=1):
            first = _first_name(p)
            line = tpl.format(niche=niche, biz=_biz(p))
            text = f"Hi {first}, {_lower_first(line)}" if first else line
            body = email_body(tenant, p, text) if channel == "email" else text
            item = _queue_touch(tenant, p, channel, "followup", body, f"Day {days} follow-up to {p['name']}", actor, run.id,
                                step=i, extra_meta={"prior_contact": prior, "send_after": base_time + days * 86400,
                                                    "cancel_if": list(CANCEL_IF), "followup_day": days,
                                                    **({"subject": subj} if subj else {})})
            ids.append(item["id"])
        run.note("followups", {"days": FOLLOWUP_DAYS}, {"queued": ids}, label="Queue value-only follow-ups (day 2/4/7)")
        return {"queued": ids}

    runtime.execute(tenant, FEATURE, f"Follow-ups: {p['name']}", ["Queue value-only follow-ups (day 2/4/7)"], fn, actor)
    return ids


# --------------------------------------------------------------------------- opener stats + chooser
def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    from .outreach_loop import wilson as _w

    return _w(k, n, z)


def move_stats(tenant, owner_id: int | None = None, family: str | None = None) -> list[dict]:
    sql = ("SELECT move, COUNT(*) n, SUM(CASE WHEN replied_at IS NOT NULL THEN 1 ELSE 0 END) r, "
           "SUM(CASE WHEN booked_at IS NOT NULL THEN 1 ELSE 0 END) b FROM prospect_touches "
           "WHERE touch='opener' AND sent_at IS NOT NULL")
    args: list = []
    if owner_id is not None:
        sql += " AND owner_user_id=?"
        args.append(owner_id)
    if family:
        sql += " AND family=?"
        args.append(family)
    sql += " GROUP BY move"
    with tenant.db() as c:
        got = {r["move"]: (r["n"], r["r"] or 0, r["b"] or 0) for r in c.execute(sql, args)}
    out = []
    for m in MOVES:
        n, r, b = got.get(m, (0, 0, 0))
        lo, hi = wilson(r, n)
        blo, bhi = wilson(b, n)
        out.append({"move": m, "label": MOVE_LABELS[m], "sent": n, "replied": r, "booked": b,
                    "reply_rate": round(r / n, 3) if n else 0.0, "reply_ci95": [round(lo, 3), round(hi, 3)],
                    "book_rate": round(b / n, 3) if n else 0.0, "book_ci95": [round(blo, 3), round(bhi, 3)]})
    return out


def choose_move(tenant, owner_id: int, available: list[str], family: str = "dm", rng: random.Random | None = None) -> dict:
    """Explore until every available style has the minimum number of sends, then Thompson sampling on reply rate."""
    if not available:
        return {"move": None, "how": "none", "why": "No opener style is available for this prospect yet"}
    stats = {s["move"]: s for s in move_stats(tenant, owner_id, family)}
    need = min_sample(tenant)
    thin = [m for m in available if stats[m]["sent"] < need]
    if thin:
        pick = min(thin, key=lambda m: (stats[m]["sent"], MOVES.index(m)))
        return {"move": pick, "how": "explore",
                "why": f"Still learning: each style needs {need} sends before the picker favours the best one"}
    rng = rng or random.Random()
    draws = {m: rng.betavariate(1 + stats[m]["replied"], 1 + stats[m]["sent"] - stats[m]["replied"]) for m in available}
    pick = max(draws, key=draws.get)
    return {"move": pick, "how": "thompson",
            "why": "Picked by Thompson sampling on reply rates (styles that reply more get picked more often)"}


# --------------------------------------------------------------------------- send / reply / booking hooks
@queue.on_sent("prospect_dm")
@queue.on_sent("prospect_email")
@queue.on_sent("followup")
@queue.on_sent("booking_dm")
def _on_sent(tenant, item: dict, actor: str) -> None:
    meta = item.get("meta") or {}
    if meta.get("feature") != FEATURE or not meta.get("prospect_id"):
        return
    now = db.now()
    with tenant.db() as c:
        n = c.execute("UPDATE prospect_touches SET sent_at=? WHERE queue_id=? AND sent_at IS NULL", (now, item["id"])).rowcount
    if not n:
        return
    pid = int(meta["prospect_id"])
    _event(tenant, pid, "sent", {"queue_id": item["id"], "touch": meta.get("touch"), "channel": item.get("channel")}, actor)
    p = _row(tenant, pid)
    if p is None:
        return
    if meta.get("touch") == "opener":
        if p["stage"] in EARLY + ("nurture",):
            set_stage(tenant, pid, "contacted", actor)
        draft_followups(tenant, pid, now, actor)


def _apply_reply(tenant, p: dict, actor: str, text: str, kind: str) -> None:
    now = db.now()
    with tenant.db() as c:
        t = c.execute("SELECT id FROM prospect_touches WHERE prospect_id=? AND touch='opener' AND sent_at IS NOT NULL "
                      "AND replied_at IS NULL ORDER BY sent_at DESC LIMIT 1", (p["id"],)).fetchone()
        if t:
            c.execute("UPDATE prospect_touches SET replied_at=? WHERE id=?", (now, t["id"]))
    _event(tenant, p["id"], "reply", {"text": _clean(text, 1000), "classification": kind,
                                      "classification_label": REPLY_KINDS.get(kind, kind)}, actor)
    _withdraw_open_drafts(tenant, p["id"], actor, "The prospect replied", touches=("followup",))


def record_reply(tenant, pid: int, actor: str, text: str = "", kind: str = "interested", user: dict | None = None,
                 emit: bool = True) -> dict:
    if kind not in REPLY_KINDS:
        raise ProspectError(422, "Unknown reply type")
    p = visible(tenant, user, pid) if user else _row(tenant, pid)
    if p is None:
        raise ProspectError(404, "Not found")
    if p["do_not_contact"]:
        _event(tenant, pid, "reply", {"text": _clean(text, 1000), "classification": kind, "after_dnc": True}, actor)
        return public(p)
    _apply_reply(tenant, p, actor, text, kind)
    if kind == "unsubscribe":
        set_dnc(tenant, pid, actor, "Asked not to be contacted (reply)")
    elif kind == "not_interested":
        set_stage(tenant, pid, "lost", actor, "Not interested (reply)")
    elif kind == "not_now":
        set_stage(tenant, pid, "nurture", actor, "Not right now (reply)")
    elif p["stage"] in EARLY + ("contacted", "nurture"):
        set_stage(tenant, pid, "replied", actor)
    if emit:
        events.emit(tenant, "reply_received", {"party_type": "prospect", "party_id": pid, "owner_user_id": p["owner_user_id"],
                                               "classification": kind, "source": FEATURE})
    return public(_row(tenant, pid))


_INBOUND_KIND = {"unsubscribe": "unsubscribe", "opt_out": "unsubscribe", "stop": "unsubscribe", "remove": "unsubscribe",
                 "not_interested": "not_interested", "negative": "not_interested", "wrong_person": "not_interested",
                 "not_now": "not_now", "later": "not_now", "objection": "question", "question": "question",
                 "interested": "interested", "positive": "interested"}


@events.on("reply_received")
def _on_reply_event(tenant, name, payload) -> None:
    if payload.get("party_type") != "prospect" or payload.get("source") == FEATURE:
        return
    try:
        pid = int(payload.get("party_id"))
    except (TypeError, ValueError):
        return
    kind = _INBOUND_KIND.get(str(payload.get("classification") or "").lower(), "other")
    if _row(tenant, pid) is not None:
        record_reply(tenant, pid, "inbound", "", kind, emit=False)


@events.on("dnc_set")
def _on_dnc_event(tenant, name, payload) -> None:
    if payload.get("party_type") != "prospect" or payload.get("source") == FEATURE:
        return
    try:
        pid = int(payload.get("party_id"))
    except (TypeError, ValueError):
        return
    if _row(tenant, pid) is not None:
        set_dnc(tenant, pid, "system", "Opted out (another channel)", emit=False)


@events.on("booking_made")
def _on_booking_event(tenant, name, payload) -> None:
    if payload.get("party_type") != "prospect" or payload.get("source") == FEATURE:
        return
    try:
        pid = int(payload.get("party_id"))
    except (TypeError, ValueError):
        return
    p = _row(tenant, pid)
    if p and not p["do_not_contact"] and p["stage"] in EARLY + ("contacted", "replied", "nurture"):
        _mark_booked(tenant, p)
        set_stage(tenant, pid, "call_booked", "system")


@events.on("booking_cancelled")
def _on_booking_cancelled(tenant, name, payload) -> None:
    if payload.get("party_type") != "prospect":
        return
    try:
        pid = int(payload.get("party_id"))
    except (TypeError, ValueError):
        return
    p = _row(tenant, pid)
    if p and p["stage"] == "call_booked":
        set_stage(tenant, pid, "replied", "system", "Call cancelled")


def _mark_booked(tenant, p: dict) -> None:
    with tenant.db() as c:
        c.execute("UPDATE prospect_touches SET booked_at=? WHERE id=(SELECT id FROM prospect_touches WHERE prospect_id=? AND "
                  "touch='opener' AND sent_at IS NOT NULL ORDER BY sent_at DESC LIMIT 1) AND booked_at IS NULL",
                  (db.now(), p["id"]))


def slots(tenant, n: int = 3) -> list[dict]:
    """Fresh slots every time (never cached); an empty list is a normal answer, not an error."""
    try:
        from .agents import sales

        got = sales._slots(tenant) or []
    except Exception:  # noqa: BLE001 - a calendar problem must not break booking; the person can type a time
        got = []
    return [s for s in got if isinstance(s, dict) and s.get("start")][:n]


def book(tenant, user: dict, pid: int, mode: str, start: float | None = None, minutes: int = 45,
         channel: str | None = None) -> dict:
    p = visible(tenant, user, pid)
    if p["do_not_contact"]:
        raise ProspectError(409, "This prospect asked not to be contacted")
    actor = _actor(user)
    if mode == "offer":
        channel = channel or ("instagram_dm" if _recipient(p, "instagram_dm") else "email")
        _check_contactable(p, channel, tenant)
        fresh = slots(tenant, 3)
        if not fresh:
            raise ProspectError(409, "No free times in the next two weeks. Free up some time in the calendar, or agree a "
                                     "time with them and record it.")

        def fn(run: runtime.Run):
            run.note("slots", {}, fresh, label="Find three fresh times")
            first = _first_name(p)
            times = ", ".join(s["label"] for s in fresh[:-1]) + f" or {fresh[-1]['label']}" if len(fresh) > 1 else fresh[0]["label"]
            text = (f"Hi {first}, " if first else "") + f"would any of these suit a short call? {times}. If none work, " \
                                                         "send me a time that does."
            body = email_body(tenant, p, text) if channel == "email" else text
            item = _queue_touch(tenant, p, channel, "booking", body, f"Call times for {p['name']}", actor, run.id,
                                extra_meta={"prior_contact": had_contact(tenant, pid), "slots": fresh,
                                            **({"subject": "Times for a short call"} if channel == "email" else {})})
            run.note("queue", {}, {"queue_id": item["id"], "status": item["status"]}, label="Queue the times for approval")
            return {"queue_id": item["id"], "status": item["status"], "slots": fresh}

        out = runtime.execute(tenant, FEATURE, f"Offer call times: {p['name']}", ["Find three fresh times",
                                                                                  "Queue the times for approval"], fn, actor)
        return {**out["result"], "run_id": out["run_id"]}
    if mode != "record":
        raise ProspectError(422, "Choose to offer times or record a booked call")
    now = time.time()
    if start is None or not (now - 300 <= start <= now + 90 * 86400):
        raise ProspectError(422, "Pick a time in the next 90 days")
    end = start + max(15, min(180, int(minutes))) * 60
    from . import calendar

    if any(bs < end and be > start for bs, be in calendar.busy(tenant, start, end)):
        raise ProspectError(409, "That time is no longer free. Pick another.")

    def fn2(run: runtime.Run):
        eid = calendar.add_event(tenant, f"Call: {p['name']}", start, end, [], status="confirmed",
                                 ext_id=f"prospect-{pid}", demo=tenant.is_demo)
        run.note("calendar", {"start": start}, {"event_id": eid}, label="Hold the time in the calendar (no invite sent)")
        brief = run.step("brief", {}, lambda: precall_brief(tenant, _row(tenant, pid)), "Pre-call brief")
        _mark_booked(tenant, p)
        if p["stage"] != "call_booked":
            set_stage(tenant, pid, "call_booked", actor)
        _event(tenant, pid, "booked", {"start": start, "end": end, "event_id": eid}, actor)
        _withdraw_open_drafts(tenant, pid, actor, "Call booked", touches=("followup",))
        events.emit(tenant, "booking_made", {"party_type": "prospect", "party_id": pid, "starts_at": start, "source": FEATURE})
        return {"event_id": eid, "start": start, "end": end, "brief": brief}

    out = runtime.execute(tenant, FEATURE, f"Book call: {p['name']}", ["Hold the time in the calendar (no invite sent)",
                                                                       "Pre-call brief"], fn2, actor)
    return {**out["result"], "run_id": out["run_id"], "prospect": public(_row(tenant, pid))}


def _playbook_refs(ids: list[str]) -> list[dict]:
    import json as _json

    from . import config

    out = []
    d = config.ROOT / "operator_ai" / "knowledge" / "playbooks"
    for f in sorted(d.glob("*.json")) if d.exists() else []:
        try:
            data = _json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        for e in data.get("entries", []):
            if e.get("id") in ids:
                out.append({"id": e["id"], "title": e.get("title"), "playbook": data.get("title")})
    return out


def precall_brief(tenant, p: dict) -> dict:
    icp = get_icp(tenant, p["owner_user_id"])
    replies = [e["detail"].get("text") for e in timeline(tenant, p["id"]) if e["kind"] == "reply" and e["detail"].get("text")]
    dq = [b for b in p.get("fit_breakdown") or [] if b.get("key") == "disqualified"]
    return {
        "who": f"{p['name']}" + (f" ({p['business']})" if p.get("business") else ""),
        "what_we_know": [{"fact": f.get("claim"), "quote": f.get("quote"), "source": f.get("source"),
                          "source_url": f.get("source_url")} for f in _facts(p)][:12],
        "their_words": replies[:5],
        "fit": {"score": p.get("fit_score"), "breakdown": p.get("fit_breakdown")},
        "questions": ["What made you reply now? What's changed?",
                      "Where do new clients come from today, and roughly how many a month?",
                      "What have you already tried, and what happened?",
                      "Who else is involved in a decision like this?",
                      "If we worked together, what would need to be true in 90 days for it to be worth it?"],
        "red_flags": [b["why"] for b in dq] + [f"Disqualifier to check: {d}" for d in icp.get("disqualifiers") or []][:5],
        "do_not_say": ["No guarantees or promised results", "No income claims or other clients' numbers without proof",
                       "No invented deadlines or scarcity", "Price only with every term stated clearly"],
        "playbook": _playbook_refs(["setter-qualification-criteria", "closing-call-structure-discovery-gap-offer",
                                    "price-presentation-and-material-terms"]),
        "mode": "rules", "label": "Built from the research and their replies (rules; AI not used)",
    }


# --------------------------------------------------------------------------- stats
def stats(tenant, user: dict) -> dict:
    own = None if user["role"] == "owner" else user["id"]
    sql, args = "SELECT stage, COUNT(*) n FROM prospects", []
    if own is not None:
        sql += " WHERE owner_user_id=?"
        args.append(own)
    sql += " GROUP BY stage"
    with tenant.db() as c:
        pipe = {r["stage"]: r["n"] for r in c.execute(sql, args)}
        psql, pargs = "SELECT status, COUNT(*) n, SUM(COALESCE(views,0)) v FROM proposals", []
        if own is not None:
            psql += " WHERE owner_user_id=?"
            pargs.append(own)
        props = {r["status"]: {"count": r["n"], "views": r["v"] or 0} for r in c.execute(psql + " GROUP BY status", pargs)}
    return {"pipeline": [{"stage": s, "label": STAGE_LABELS[s], "count": pipe.get(s, 0)} for s in STAGES],
            "usage": usage(tenant, user["id"]), "caps": caps(tenant),
            "moves": {fam: move_stats(tenant, own, fam) for fam in DEFAULT_CAPS},
            "min_sample": min_sample(tenant), "proposals": props}


# --------------------------------------------------------------------------- guardrail plug-in
_PLACEHOLDER = re.compile(r"\[\[[^\]\n]{1,160}\]\]")
_TIME = re.compile(r"\b\d{1,2}:\d{2}\b(\s?[ap]\.?m\.?)?|\b\d{1,2}\s?[ap]\.?m\.?\b", re.I)
_DURATION = re.compile(r"\b\d+\s?-?\s?(minutes?|mins?|hours?|hrs?|seconds?|secs?)\b", re.I)
_MONTHS = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
_DATE = re.compile(rf"\b\d{{1,2}}(st|nd|rd|th)?\s+{_MONTHS}\b|\b{_MONTHS}\s+\d{{1,2}}(st|nd|rd|th)?\b", re.I)
_NUMTOK = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?\s?[kKmM]?(?![\w])|(?<![\w.])\d[\d,]*(?:\.\d+)?(?=[a-zA-Z%-])")
_QUOTED = re.compile(r"\"([^\"\n]{3,240})\"")
_WORD = re.compile(r"[A-Za-z][\w'&.\-]*")
_CAP_STOP = {"hi", "hey", "hello", "dear", "loved", "love", "saw", "noticed", "quick", "just", "i", "i'm", "i've", "i'd",
             "i'll", "your", "you", "thanks", "thank", "would", "want", "happy", "no", "not", "if", "this", "that", "here",
             "is", "are", "do", "does", "what", "how", "why", "when", "who", "where", "great", "congrats", "congratulations",
             "best", "cheers", "subject", "question", "idea", "one", "a", "an", "the", "it", "it's", "we", "we're", "our",
             "my", "most", "curious", "since", "also", "ps", "p.s", "re", "fwd", "and", "or", "but", "so", "as", "on", "in",
             "at", "for", "with", "unsubscribe", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
             "instagram", "facebook", "linkedin", "youtube", "tiktok", "google", "meta", "whatsapp", "zoom", "dm", "dms",
             "ok", "okay", "yes", "hope", "sorry", "worth", "any", "some", "there", "they", "their", "he", "she", "me",
             "january", "february", "march", "april", "june", "july", "august", "september", "october", "november",
             "december", "mon", "tue", "wed", "thu", "fri", "sat", "sun"}
_CONNECT = {"of", "the", "and", "&", "for", "in", "to"}
_OFFER_NOUNS = {"programme", "program", "course", "challenge", "webinar", "masterclass", "academy", "podcast", "book",
                "membership", "accelerator", "bootcamp", "mastermind", "summit", "workshop", "method", "framework", "system",
                "community", "retreat", "intensive", "newsletter", "app", "launch", "club", "blueprint", "formula"}


def _num_value(tok: str) -> float | None:
    t = tok.strip().replace(",", "")
    mult = 1.0
    if t[-1:].lower() in ("k", "m"):
        mult = 1e3 if t[-1].lower() == "k" else 1e6
        t = t[:-1].strip()
    try:
        return float(t) * mult
    except ValueError:
        return None


def _corpus_numbers(corpus: str) -> list[float]:
    out = []
    for m in _NUMTOK.finditer(corpus):
        v = _num_value(m.group(0))
        if v is not None:
            out.append(v)
    return out


def _num_ok(v: float, known: list[float]) -> bool:
    for k in known:
        if v == k or (v >= 1000 and k >= 1000 and abs(v - k) <= 0.05 * k):
            return True
    return False


def _norm_phrase(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s&'-]", " ", s.lower())).strip()


def _cap_phrases(text: str) -> list[str]:
    """Named things: runs of two or more Capitalised words, or one Capitalised word before an offer noun."""
    out = []
    for sent in re.split(r"(?<=[.!?:\n])\s+|\n", text):
        words = [(m.group(0), m.start()) for m in _WORD.finditer(sent)]
        run: list[str] = []

        def flush():
            while run and (run[-1].lower() in _CONNECT or run[-1].lower() in _CAP_STOP):
                run.pop()
            while run and (run[0].lower() in _CAP_STOP or run[0].lower() in _CONNECT):
                run.pop(0)
            caps_ = [w for w in run if w[0].isupper()]
            if len(caps_) >= 2:
                out.append(" ".join(run))
            run.clear()

        for i, (w, _) in enumerate(words):
            is_cap = w[0].isupper() and not (w.isupper() and len(w) <= 4) and w.lower() not in _CAP_STOP
            if is_cap:
                run.append(w)
                nxt = words[i + 1][0] if i + 1 < len(words) else ""
                if len([x for x in run if x[0].isupper()]) == 1 and nxt.lower() in _OFFER_NOUNS and not nxt[0].isupper():
                    out.append(f"{w} {nxt}")
            elif run and w.lower() in _CONNECT:
                run.append(w)
            else:
                flush()
        flush()
    return out


_TEMPLATE_TOKEN = re.compile(r"\{\{[a-z_]{1,40}\}\}")


def _unverified(text: str, corpus: str) -> list[str]:
    # The whole text is checked, footer included: the sender's own details (name, business, postal address,
    # reply email) are part of the corpus, so a real footer passes and anything slipped in after "--" doesn't.
    # Placeholders are flagged on their own (unfilled_placeholder), so they are left out here.
    body = _TEMPLATE_TOKEN.sub(" ", _PLACEHOLDER.sub(" ", text))
    body = guardrails._ALLOWED_RANGE.sub(" ", body)
    body = _DATE.sub(" ", _DURATION.sub(" ", _TIME.sub(" ", body)))
    corpus_low = _norm_phrase(corpus)
    known = _corpus_numbers(corpus)
    bad: list[str] = []
    for m in _NUMTOK.finditer(body):
        v = _num_value(m.group(0))
        if v is not None and not _num_ok(v, known):
            bad.append(m.group(0).strip())
    for q in _QUOTED.findall(body):
        if _norm_phrase(q) and _norm_phrase(q) not in corpus_low:
            bad.append(f"\"{q.strip()[:60]}\"")
    for ph in _cap_phrases(body):
        if _norm_phrase(ph) not in corpus_low:
            bad.append(ph)
    seen, out = set(), []
    for b in bad:
        if b.lower() not in seen:
            seen.add(b.lower())
            out.append(b)
    return out[:3]


def guard_rules(text: str, mode: str, meta: dict, tenant) -> list[dict]:
    """Plugged into guardrails.check (guardrails.RULE_PLUGINS). Codes:
    unfilled_placeholder (block): a [[...]] placeholder is still in a prospects draft or proposal.
    do_not_contact (block): the prospect opted out. claim_released (block): the owner released this prospect.
    unverified_personalisation (block): an opener, follow-up or call-times message names a number, a quoted
    phrase or a named offer that isn't in the prospect's research facts (or their replies).
    never_invite (block): the prospect's email or handle is on the never-invite list (checked live).
    daily_cap (block): a first message or follow-up that would go out now, after today's cap of confirmed sends on
    that channel is used up."""
    flags: list[dict] = []
    meta = meta or {}
    ours = meta.get("feature") == FEATURE or bool(meta.get("proposal_id"))
    if ours:
        m = _PLACEHOLDER.search(text or "")
        if m:
            flags.append(guardrails._flag("unfilled_placeholder", "block",
                                          "Fill in the placeholder before this can go out", m.group(0)))
    pid = meta.get("prospect_id")
    if tenant is None or not pid or meta.get("practice") or meta.get("lead_id"):
        return flags
    try:
        p = _row(tenant, int(pid))
    except (TypeError, ValueError, OverflowError):
        return flags
    if p is not None and p.get("do_not_contact"):
        flags.append(guardrails._flag("do_not_contact", "block", "This prospect asked not to be contacted", p.get("name") or ""))
    if p is not None and ours and p.get("claim_released"):
        flags.append(guardrails._flag("claim_released", "block",
                                      "The owner released this prospect to the team; it can't be messaged from this list"))
    if p is not None and never_invited(tenant, p):
        flags.append(guardrails._flag("never_invite", "block", "Recipient is on the never-invite list", "[blocked attendee]"))
    touch = meta.get("touch")
    if p is not None and ours and touch in ("opener", "followup"):
        flags += _cap_flags(tenant, p, meta)
    applies = touch in ("opener", "followup", "booking") or (touch is None and meta.get("prospect_facing") and not ours)
    if applies and mode == "outreach":
        corpus = facts_corpus(tenant, p) if p is not None else ""
        if touch == "booking":  # the offered times are part of what this message may say
            corpus += "\n" + "\n".join(str(s.get("label") or "") for s in (meta.get("slots") or []) if isinstance(s, dict))
        for item in _unverified(text or "", corpus):
            flags.append(guardrails._flag(
                "unverified_personalisation", "block",
                "Mentions something that isn't in this prospect's research. Add it as a researched fact with its source, "
                "or take it out", item))
    return flags


def _cap_flags(tenant, p: dict, meta: dict) -> list[dict]:
    """Caps are checked again when a message would actually go out (at approval), not only when it is drafted:
    a draft written earlier can't push today's confirmed sends past the cap. A follow-up that isn't due yet is
    left to its own day."""
    due = meta.get("send_after")
    try:
        if due is not None and float(due) > time.time():
            return []
    except (TypeError, ValueError):
        pass
    fam = meta.get("channel_family") if meta.get("channel_family") in DEFAULT_CAPS else "dm"
    u = usage(tenant, p["owner_user_id"]).get(fam)
    if not u or u["sent_today"] < u["cap"]:
        return []
    what = "emails" if fam == "email" else "Instagram DMs"
    return [guardrails._flag("daily_cap", "block",
                             f"Today's cap of {u['cap']} {what} is used up. Approve this tomorrow, or ask the owner to "
                             "change the cap.", f"{u['sent_today']} sent today")]


# --------------------------------------------------------------------------- demo seed
def seed_demo(tenant) -> dict:
    """Fictional ICP, 12 prospects at varied stages and one proposal for the demo student. Demo DB only."""
    from . import tenancy

    if not tenancy.is_demo_db(tenant):
        return {"seeded": 0}
    with tenant.db() as c:
        u = c.execute("SELECT id, username, role FROM users WHERE username='demo-student'").fetchone()
        if u is None or c.execute("SELECT 1 FROM prospects WHERE owner_user_id=? LIMIT 1", (u["id"],)).fetchone():
            return {"seeded": 0}
    user = {"id": u["id"], "username": u["username"], "role": u["role"]}
    icp = {"niche": "fitness coaches", "offer": "a booked-call system for online coaching offers",
           "price_min": 500, "price_max": 5000, "audience_min": 5000, "audience_max": 80000,
           "must_haves": ["coaching", "programme"], "disqualifiers": ["no offer yet"], "signals": ["webinar", "ads", "launch"],
           "persona": "Founder-led online coach, posts daily, sells a group programme, short on time (DEMO).",
           "sender": {"name": "Ava (DEMO)", "business": "Hartley Growth (DEMO)",
                      "postal_address": "1 Example Street, Example City (DEMO)", "reply_email": "ava@example.invalid"}}
    with tenant.db() as c:
        c.execute("INSERT OR REPLACE INTO prospect_icps(user_id,data,updated) VALUES(?,?,?)",
                  (user["id"], db.jdump(icp), db.now()))
    people = [
        ("Mara Quill (DEMO)", "Strong Mums Club (DEMO)", "@strongmums_demo", "mara@example.invalid", 18400,
         "Helping busy mums get strong at home. 12-week coaching programme. Free training every month.", "yes, 3 active ads",
         "new", "question"),
        ("Jonas Pell (DEMO)", "Pell Performance (DEMO)", "@pellperformance_demo", "jonas@example.invalid", 42100,
         "Strength coaching for desk workers. Group programme, launch twice a year.", "no", "new", None),
        ("Rhea Okoro (DEMO)", "Lift With Rhea (DEMO)", "@liftwithrhea_demo", "rhea@example.invalid", 9300,
         "Online coaching for first-time lifters. Weekly live webinar.", "active", "researched", None),
        ("Tobias Wren (DEMO)", "Wren Mobility (DEMO)", "@wrenmobility_demo", "tobias@example.invalid", 27600,
         "Mobility coaching programme for runners. Met at the spring coaching summit, both in the same mastermind group.",
         "", "researched", None),
        ("Selma Ardent (DEMO)", "Ardent Nutrition (DEMO)", "@ardentnutrition_demo", "selma@example.invalid", 61000,
         "Nutrition coaching for busy professionals. Programme opens each quarter.", "yes", "contacted", "compliment"),
        ("Caleb Moss (DEMO)", "Moss Method (DEMO)", "@mossmethod_demo", "caleb@example.invalid", 15800,
         "Kettlebell coaching programme. Runs a free 5-day challenge.", "running ads", "contacted", "resource"),
        ("Ines Varro (DEMO)", "Varro Strength (DEMO)", "@varrostrength_demo", "ines@example.invalid", 33300,
         "Strength coaching for women over forty. Group programme and webinar.", "yes", "replied", "question"),
        ("Dario Fenn (DEMO)", "Fenn Fitness (DEMO)", "@fennfitness_demo", "dario@example.invalid", 7200,
         "Hybrid coaching programme for new runners.", "no", "replied", "resource"),
        ("Noor Halden (DEMO)", "Halden Health (DEMO)", "@haldenhealth_demo", "noor@example.invalid", 54800,
         "Postnatal coaching programme. Launch every spring.", "3 active ads", "call_booked", "compliment"),
        ("Felix Arbor (DEMO)", "Arbor Athletics (DEMO)", "@arborathletics_demo", "felix@example.invalid", 22900,
         "Online coaching programme for teen athletes and their parents.", "yes", "call_booked", "question"),
        ("Greta Lund (DEMO)", "Lund Yoga (DEMO)", "@lundyoga_demo", "greta@example.invalid", 12100,
         "Yoga teacher, free classes only, no offer yet.", "no", "nurture", "question"),
        ("Omar Castell (DEMO)", "Castell Coaching (DEMO)", "@castellcoaching_demo", "omar@example.invalid", 98000,
         "Coaching programme for gym owners.", "yes", "lost", "compliment"),
    ]
    now = db.now()
    actor = "demo-student(student)"
    ids = {}
    with tenant.db() as c, db.tx(c):
        gate = _Gate(tenant, c)
        for i, (name, biz, handle, email, followers, notes, ads, stage, move) in enumerate(people):
            slug = handle.strip("@").replace("_demo", "")
            f = _norm_fields({"name": name, "business": biz, "handle": handle, "email": email,
                              "website": f"https://{slug}.example.invalid", "niche": "fitness coaching",
                              "followers": followers, "notes": notes, "ad_activity": ads})
            status, pid = _insert(tenant, c, gate, user["id"], f, "csv", "Imported list: coaches-oct.csv (DEMO)",
                                  "legitimate_interest", "Public business contact details from their own sites (DEMO)",
                                  "Fitness coaches (DEMO)", None, actor)
            if status != "ok":
                continue
            ids[pid] = (stage, move, i)
            ago = (12 - i) * 86400
            c.execute("UPDATE prospects SET stage=?, created=?, updated=?, stage_changed=?, lost_reason=? WHERE id=?",
                      (stage, now - ago - 3 * 86400, now - ago, now - ago,
                       "Chose to keep it in-house (DEMO)" if stage == "lost" else None, pid))
            if move and stage != "new":
                c.execute("INSERT INTO prospect_touches(prospect_id,owner_user_id,queue_id,touch,move,step,channel,family,sent_at,"
                          "replied_at,booked_at,demo,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?)",
                          (pid, user["id"], None, "opener", move, None, "instagram_dm", "dm", now - ago,
                           now - ago + 7200 if stage in ("replied", "call_booked", "proposal", "lost", "nurture") else None,
                           now - ago + 86400 if stage in ("call_booked", "proposal") else None, now - ago))
                _event(tenant, pid, "sent", {"touch": "opener", "channel": "instagram_dm"}, actor, c=c)
            if stage in ("replied", "call_booked", "proposal", "lost", "nurture"):
                _event(tenant, pid, "reply", {"text": "Thanks for reaching out, tell me more about how this works (DEMO).",
                                              "classification": "interested", "classification_label": "Interested"}, actor, c=c)
        # practice history so the opener stats have something to show
        rng = random.Random(11)
        for m_i, m in enumerate(MOVES):
            for k in range(14 + 3 * m_i):
                c.execute("INSERT INTO prospect_touches(prospect_id,owner_user_id,queue_id,touch,move,step,channel,family,"
                          "sent_at,replied_at,booked_at,demo,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?)",
                          (None, user["id"], None, "opener", m, None, "instagram_dm", "dm", now - (30 + k) * 86400,
                           now - (30 + k) * 86400 + 3600 if rng.random() < (0.08 + 0.03 * m_i) else None,
                           None, now - (30 + k) * 86400))
    rescore_all(tenant, user["id"])
    # one new prospect has a first message waiting for the student's own approval
    first_new = next((pid for pid, (st, mv, _) in ids.items() if st == "new" and mv), None)
    if first_new:
        p = _row(tenant, first_new)
        v = next(x for x in opener_variants(tenant, p, "instagram_dm") if x["move"] == "question")
        _queue_touch(tenant, p, "instagram_dm", "opener", v["text"], f"Question opener to {p['name']}", actor, None,
                     move="question")
    # the proposal waits for the student's own approval; approving it moves this prospect to "Proposal sent"
    prop_pid = next((pid for pid, (st, _, i) in ids.items() if st == "call_booked" and i == 9), None)
    if prop_pid:
        try:
            from . import proposals

            pr = proposals.build(tenant, user, prop_pid,
                                 call_notes="We get plenty of enquiries from parents but most never book a call. "
                                            "I spend my evenings answering DMs by hand. "
                                            "We want a simple way to turn interest into booked consultations (DEMO).",
                                 numbers={"leads": 120, "booked": 14, "showed": 9, "closed": 3, "offer_price": 900},
                                 pricing={"currency": "USD", "retainer": 2000, "performance_pct": 10, "pilot_fee": 1200,
                                          "pilot_days": 30, "term_months": 3})
            proposals.send(tenant, user, pr["proposal"]["id"], channel="email")
        except Exception:  # noqa: BLE001 - the demo must still load if the proposal can't be built
            import logging

            logging.getLogger("operator.prospects").exception("demo proposal seed failed")
    return {"seeded": len(ids)}
