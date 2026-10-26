"""Brief builder: turn a few inputs plus the business profile into a complete brief, and say
which fields are still missing. Every Studio line, the webinar chain and the builder read it."""
from __future__ import annotations

from . import db

FIELDS = ["business", "niche", "audience", "offer", "price", "outcome", "timeframe", "mechanism", "pain", "channels",
          "faq", "date"]
DEFAULTS = {
    "business": "Your business", "niche": "independent clinics", "audience": "clinic owners", "offer": "done-with-you growth programme",
    "price": 3000, "outcome": "20 new bookings a month", "timeframe": "90 days", "mechanism": "referral-first system",
    "pain": "discounting", "channels": "Instagram DMs, Meta ads, webinars", "faq": "", "date": "next Thursday 7pm",
}


def profile(tenant) -> dict:
    with tenant.db() as c:
        r = c.execute("SELECT data FROM profiles WHERE scope='business'").fetchone()
    return db.jload(r["data"], {}) if r else {}


def from_profile(p: dict) -> dict:
    """Map the 8-section business profile onto brief fields."""
    out = {}
    ident = p.get("identity", {}) or {}
    offer = p.get("offer", {}) or {}
    aud = p.get("audience", {}) or {}
    out["business"] = ident.get("name")
    out["niche"] = aud.get("niche")
    out["audience"] = aud.get("who")
    out["offer"] = offer.get("name")
    out["price"] = offer.get("price")
    out["outcome"] = offer.get("outcome")
    out["mechanism"] = offer.get("mechanism")
    out["pain"] = aud.get("pain")
    out["timeframe"] = offer.get("timeframe")
    out["channels"] = (p.get("channels", {}) or {}).get("list")
    out["faq"] = (p.get("faq", {}) or {}).get("text")
    return {k: v for k, v in out.items() if v}


def build(tenant, partial: dict | None = None) -> dict:
    partial = {k: v for k, v in (partial or {}).items() if k in FIELDS and v not in (None, "")}
    prof = from_profile(profile(tenant)) if tenant is not None else {}
    brief = {**DEFAULTS, **prof, **partial}
    missing = [k for k in FIELDS if k not in partial and k not in prof and k not in ("faq", "date")]
    try:
        brief["price"] = float(brief["price"])
    except (TypeError, ValueError):
        brief["price"] = float(DEFAULTS["price"])
    usp = (f"I help {brief['audience']} get {brief['outcome']} in {brief['timeframe']} through our {brief['mechanism']} "
           f"without {brief['pain']}.")
    return {"brief": brief, "missing": missing, "defaults_used": missing, "usp": usp}
