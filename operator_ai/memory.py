"""Per-user business memory: "What I know about you".

Each person keeps a short, editable record of their offer, niche, price, current goal, key numbers and the
decisions they have made. It belongs to them: they see it, edit it, export it and delete it. The Student Coach and
the chat router read it through for_user() / hint() so advice can use last week's numbers instead of starting cold.
Suggestions come from the person's own records (student record, check-ins, clients, prospects) by rules; the AI is
never used here and nothing is sent anywhere.
"""
from __future__ import annotations

import re
import time

from . import audit, db

PROFILE_FIELDS = {
    "offer": ("What you sell", 300),
    "niche": ("Who you sell to", 200),
    "goal": ("Your current goal", 300),
    "notes": ("Anything else to remember", 2000),
}
MAX_NUMBERS = 30
MAX_DECISIONS = 100
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_SOURCE = re.compile(r"^(you|found:[a-z_]{1,30}|demo)$")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
LABEL = "Saved by you, or found in your own records by rules (AI not used)"


class MemoryError_(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


def _clean(s, limit: int) -> str:
    return _CTRL.sub(" ", str(s or "")).strip()[:limit]


def _rows(tenant, user_id: int) -> list[dict]:
    with tenant.db() as c:
        return db.rows(c.execute("SELECT * FROM user_memory WHERE user_id=? ORDER BY kind, id", (user_id,)))


def get(tenant, user_id: int) -> dict:
    rows = _rows(tenant, user_id)
    profile: dict = {k: "" for k in PROFILE_FIELDS}
    profile.update({"price": None, "currency": "USD"})
    numbers, decisions = [], []
    updated = 0.0
    for r in rows:
        updated = max(updated, r["updated"] or 0)
        if r["kind"] in PROFILE_FIELDS:
            profile[r["kind"]] = r["value"] or ""
        elif r["kind"] == "price":
            try:
                profile["price"] = float(r["value"]) if r["value"] not in (None, "") else None
            except ValueError:
                profile["price"] = None
        elif r["kind"] == "currency":
            profile["currency"] = r["value"] or "USD"
        elif r["kind"] == "number":
            numbers.append({"id": r["id"], "label": r["label"], "value": r["value"], "source": r["source"],
                            "updated": r["updated"]})
        elif r["kind"] == "decision":
            decisions.append({"id": r["id"], "text": r["value"], "at": r["created"], "source": r["source"]})
    decisions.sort(key=lambda d: d["at"] or 0, reverse=True)
    return {"profile": profile, "numbers": numbers, "decisions": decisions, "updated": updated or None,
            "empty": not rows, "label": LABEL, "used_by": ["Student Coach", "the one chat (router)"]}


def validate(data: dict) -> dict:
    """Normalise a full replacement payload; raises MemoryError_(422) with a plain message."""
    if not isinstance(data, dict):
        raise MemoryError_(422, "Send the whole memory as an object.")
    prof_in = data.get("profile") or {}
    if not isinstance(prof_in, dict):
        raise MemoryError_(422, "profile must be an object")
    prof = {}
    for k, (_, limit) in PROFILE_FIELDS.items():
        v = prof_in.get(k, "")
        if v is not None and not isinstance(v, str):
            raise MemoryError_(422, f"{k} must be text")
        if v and len(v) > limit:
            raise MemoryError_(422, f"{k} is too long (max {limit} characters)")
        prof[k] = _clean(v, limit)
    price = prof_in.get("price")
    if price in ("", None):
        prof["price"] = None
    else:
        try:
            price = float(price)
        except (TypeError, ValueError):
            raise MemoryError_(422, "price must be a number")
        if not (0 <= price <= 10_000_000):
            raise MemoryError_(422, "price must be between 0 and 10,000,000")
        prof["price"] = round(price, 2)
    cur = (prof_in.get("currency") or "USD")
    if not isinstance(cur, str) or not _CURRENCY.match(cur.upper()):
        raise MemoryError_(422, "currency must be a 3-letter code like USD")
    prof["currency"] = cur.upper()
    nums_in = data.get("numbers") or []
    if not isinstance(nums_in, list) or len(nums_in) > MAX_NUMBERS:
        raise MemoryError_(422, f"Keep at most {MAX_NUMBERS} numbers")
    nums, seen = [], set()
    for n in nums_in:
        if not isinstance(n, dict):
            raise MemoryError_(422, "each number needs a label and a value")
        label, value = _clean(n.get("label"), 60), _clean(n.get("value"), 40)
        if not label or not value:
            raise MemoryError_(422, "each number needs a label and a value")
        if label.lower() in seen:
            continue
        seen.add(label.lower())
        src = str(n.get("source") or "you")
        nums.append({"label": label, "value": value, "source": src if _SOURCE.match(src) else "you"})
    dec_in = data.get("decisions") or []
    if not isinstance(dec_in, list) or len(dec_in) > MAX_DECISIONS:
        raise MemoryError_(422, f"Keep at most {MAX_DECISIONS} decisions")
    decs = []
    now = time.time()
    for d in dec_in:
        if not isinstance(d, dict):
            raise MemoryError_(422, "each decision needs text")
        text = _clean(d.get("text"), 300)
        if not text:
            raise MemoryError_(422, "each decision needs text")
        at = d.get("at")
        try:
            at = float(at) if at is not None else now
        except (TypeError, ValueError):
            raise MemoryError_(422, "decision date must be a timestamp")
        if not (946684800 <= at <= now + 86400):
            at = now
        src = str(d.get("source") or "you")
        decs.append({"text": text, "at": at, "source": src if _SOURCE.match(src) else "you"})
    return {"profile": prof, "numbers": nums, "decisions": decs}


def put(tenant, user_id: int, data: dict, actor: str) -> dict:
    """Replace this person's memory with the edited version."""
    v = validate(data)
    now = time.time()
    demo = 1 if tenant.is_demo else 0
    with tenant.db() as c, db.tx(c):
        c.execute("DELETE FROM user_memory WHERE user_id=?", (user_id,))
        for k in list(PROFILE_FIELDS) + ["price", "currency"]:
            val = v["profile"].get(k)
            if val in (None, ""):
                continue
            if k == "currency" and v["profile"].get("price") is None:
                continue
            c.execute("INSERT INTO user_memory(user_id,kind,label,value,source,demo,created,updated) VALUES(?,?,?,?,?,?,?,?)",
                      (user_id, k, k, str(val), "you", demo, now, now))
        for n in v["numbers"]:
            c.execute("INSERT INTO user_memory(user_id,kind,label,value,source,demo,created,updated) VALUES(?,?,?,?,?,?,?,?)",
                      (user_id, "number", n["label"], n["value"], n["source"], demo, now, now))
        for d in v["decisions"]:
            c.execute("INSERT INTO user_memory(user_id,kind,label,value,source,demo,created,updated) VALUES(?,?,?,?,?,?,?,?)",
                      (user_id, "decision", None, d["text"], d["source"], demo, d["at"], now))
    filled = sum(1 for k in PROFILE_FIELDS if v["profile"].get(k)) + (1 if v["profile"].get("price") is not None else 0)
    audit.log(tenant, actor, "memory.update", {"user_id": user_id, "fields": filled, "numbers": len(v["numbers"]),
                                               "decisions": len(v["decisions"])})
    return get(tenant, user_id)


def delete_all(tenant, user_id: int, actor: str) -> int:
    with tenant.db() as c:
        n = c.execute("DELETE FROM user_memory WHERE user_id=?", (user_id,)).rowcount
    audit.log(tenant, actor, "memory.delete", {"user_id": user_id, "items": n})
    return n


def export(tenant, user_id: int, actor: str) -> dict:
    audit.log(tenant, actor, "memory.export", {"user_id": user_id})
    m = get(tenant, user_id)
    return {"exported_at": time.time(), "workspace": tenant.id, "user_id": user_id,
            "what_i_know_about_you": {k: m[k] for k in ("profile", "numbers", "decisions", "updated")}}


# --------------------------------------------------------------------------- readers for the coach and router
def for_user(tenant, user_id: int | None) -> dict:
    """Compact memory for agents: only filled fields. {} when there is nothing (or on any error)."""
    if not user_id:
        return {}
    try:
        m = get(tenant, int(user_id))
    except Exception:  # noqa: BLE001 - memory must never break an agent
        return {}
    out = {k: v for k, v in m["profile"].items() if v not in (None, "") and k != "currency"}
    if "price" in out:
        out["currency"] = m["profile"]["currency"]
    if m["numbers"]:
        out["numbers"] = {n["label"]: n["value"] for n in m["numbers"]}
    if m["decisions"]:
        out["decisions"] = [d["text"] for d in m["decisions"][:5]]
    return out


def money(price: float | None, currency: str = "USD") -> str:
    if price is None:
        return ""
    sym = {"USD": "$", "GBP": "£", "EUR": "€", "CAD": "CA$", "AUD": "A$"}.get(currency, "")
    amount = f"{price:,.0f}" if float(price).is_integer() else f"{price:,.2f}"
    return f"{sym}{amount}" if sym else f"{amount} {currency}"


def hint(tenant, user: dict | None, intent: str = "question") -> dict | None:
    """What the chat router shows next to an answer: the remembered facts that matter for this kind of message.
    Never raises; None when there is nothing remembered."""
    try:
        m = for_user(tenant, (user or {}).get("id"))
        if not m:
            return None
        facts = []
        if m.get("offer"):
            facts.append({"label": "Your offer", "value": m["offer"]})
        if m.get("niche"):
            facts.append({"label": "Your niche", "value": m["niche"]})
        if m.get("price") is not None:
            facts.append({"label": "Your price", "value": money(m["price"], m.get("currency", "USD"))})
        if intent == "metrics":
            facts += [{"label": k, "value": v} for k, v in list((m.get("numbers") or {}).items())[:4]]
        elif m.get("goal"):
            facts.append({"label": "Your goal", "value": m["goal"]})
        if not facts:
            return None
        return {"facts": facts[:6], "label": "From “What I know about you” (you can edit or delete it in Settings)"}
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- suggestions (rules)
def suggestions(tenant, user: dict) -> list[dict]:
    """Facts found in the person's own records that memory doesn't hold yet. Rules only."""
    uid = user["id"]
    cur = get(tenant, uid)
    have_nums = {n["label"].lower(): n["value"] for n in cur["numbers"]}
    out: list[dict] = []
    with tenant.db() as c:
        u = db.row(c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()) or {}
        sid = u.get("student_id")
        if sid:
            s = db.row(c.execute("SELECT * FROM students WHERE id=?", (sid,)).fetchone()) or {}
            if s.get("niche") and not cur["profile"]["niche"]:
                out.append({"key": "niche", "kind": "profile", "field": "niche", "value": s["niche"],
                            "source": "found:student_record", "why": "From your student record"})
            ck = db.row(c.execute("SELECT * FROM checkins WHERE student_id=? ORDER BY at DESC LIMIT 1", (sid,)).fetchone())
            if ck:
                when = time.strftime("%d %b", time.localtime(ck["at"]))
                for label, val in ((f"Outreach in the week to {when}", ck["outreach"]),
                                   (f"Calls booked in the week to {when}", ck["calls_booked"]),
                                   (f"Calls that showed in the week to {when}", ck["calls_showed"])):
                    if val is not None and have_nums.get(label.lower()) != str(val):
                        out.append({"key": f"num:{label}", "kind": "number", "label": label, "value": str(val),
                                    "source": "found:checkin", "why": "From your latest check-in"})
            cl = db.row(c.execute("SELECT * FROM clients WHERE student_id=? ORDER BY id LIMIT 1", (sid,)).fetchone())
            if cl:
                if cl.get("offer") and not cur["profile"]["offer"]:
                    out.append({"key": "offer", "kind": "profile", "field": "offer", "value": cl["offer"],
                                "source": "found:client", "why": "From your first client's record"})
                if cl.get("price") and cur["profile"]["price"] is None:
                    out.append({"key": "price", "kind": "profile", "field": "price", "value": cl["price"],
                                "source": "found:client", "why": "From your first client's record"})
        try:
            n = c.execute("SELECT COUNT(*) FROM prospects WHERE owner_user_id=? AND COALESCE(do_not_contact,0)=0",
                          (uid,)).fetchone()[0]
        except Exception:  # noqa: BLE001
            n = 0
        if n and have_nums.get("prospects in my list") != str(n):
            out.append({"key": "num:Prospects in my list", "kind": "number", "label": "Prospects in my list",
                        "value": str(n), "source": "found:prospects", "why": "From your prospect list"})
    return out[:10]


# --------------------------------------------------------------------------- demo
def seed_demo(tenant) -> None:
    from . import tenancy

    if not tenancy.is_demo_db(tenant):
        return
    with tenant.db() as c:
        r = c.execute("SELECT id FROM users WHERE username='demo-student'").fetchone()
    if r is None:
        return
    now = time.time()
    put(tenant, r["id"], {
        "profile": {"offer": "Done-with-you patient acquisition for physio clinics (DEMO)", "niche": "physio clinics (DEMO)",
                    "goal": "Sign a first clinic client by week 6 (DEMO)", "price": 2500, "currency": "USD",
                    "notes": "Prefers voice notes for feedback (DEMO)"},
        "numbers": [{"label": "Openers sent last week (DEMO)", "value": "60", "source": "demo"},
                    {"label": "Calls booked last week (DEMO)", "value": "3", "source": "demo"}],
        "decisions": [{"text": "Chose physio clinics over dental after three discovery calls (DEMO)", "at": now - 20 * 86400,
                       "source": "demo"},
                      {"text": "Moved follow-ups to day 3 and day 7 (DEMO)", "at": now - 6 * 86400, "source": "demo"}],
    }, "system")
