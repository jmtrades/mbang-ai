"""Guardrails: one pure function family.

    check(text, mode, meta=None, banned_names=None) -> [flags]

Modes: "outreach" (prospect-facing: money rule applies), "client_asset" (a student's own
VSL/pages/emails: their price may appear), "plain" (everything else). A flag is
{"code", "severity": "block"|"review", "message", "match"}. Any "block" flag stops an item
from being approved until it is edited clean. "review" flags are non-blocking: the item is
marked "needs a person".

The same function runs when an item is queued, again on approval (after edits), inside
self-critique and inside coaching line-fixes.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable

MODES = ("outreach", "client_asset", "plain")

_SENT_SPLIT = re.compile(r"(?<=[.!?\n])\s+")
_NEG = re.compile(
    r"\b(never|not|no|don'?t|do not|doesn'?t|does not|won'?t|will not|avoid|avoiding|without|stop|nobody|"
    r"refuse|ban|banned|isn'?t|aren'?t|can'?t|cannot|shouldn'?t|should not|must not|mustn'?t)\b[^.!?\n]{0,40}$",
    re.I,
)


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "")
    return t.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')


def negated(text: str, start: int) -> bool:
    """True when the match at `start` is preceded (same sentence, within ~40 chars) by a negator,
    e.g. "never plant questions", "we don't guarantee"."""
    prefix = text[max(0, start - 60):start]
    cut = max(prefix.rfind("."), prefix.rfind("!"), prefix.rfind("?"), prefix.rfind("\n"))
    if cut >= 0:
        prefix = prefix[cut + 1:]
    return bool(_NEG.search(prefix))


def _sentence_at(text: str, start: int, end: int) -> str:
    s = max(text.rfind(".", 0, start), text.rfind("!", 0, start), text.rfind("?", 0, start), text.rfind("\n", 0, start))
    e_candidates = [i for i in (text.find(".", end), text.find("!", end), text.find("?", end), text.find("\n", end)) if i >= 0]
    e = min(e_candidates) if e_candidates else len(text)
    return text[s + 1:e + 1]


def _flag(code: str, severity: str, message: str, match: str = "") -> dict:
    return {"code": code, "severity": severity, "message": message, "match": match.strip()[:120]}


# --------------------------------------------------------------------------- fake proof / deception
# (code, pattern, message). All block, all negation-aware.
DECEPTION_RULES: list[tuple[str, re.Pattern, str]] = [
    ("guarantee", re.compile(r"\b(guarantee[ds]?|guaranteeing)\b|\bmoney[- ]back\b|\brisk[- ]free\b", re.I),
     "Guarantees and money-back promises aren't allowed"),
    ("income_claim", re.compile(
        r"\b(our |my |the )?(students?|clients?|members?|customers?|people|graduates?|they|we|i)\b[^.!?\n]{0,40}?"
        r"\b(make|made|makes|earn|earns|earned|earning|making|generate[ds]?|generating|hit|hits|hitting|clear(s|ed)?|bank(s|ed)?|pull(s|ed)? in)\b"
        r"[^.!?\n]{0,25}?(\$\s?\d[\d,.]*\s?[kKmM]?|\b\d[\d,.]*\s?[kK]\b)", re.I),
     "Income claims about students, clients or yourself aren't allowed"),
    ("income_claim", re.compile(r"\b\d{2,4}\s?[kK]\s?\+", re.I), "Income claims like \"100K+\" aren't allowed"),
    ("fake_testimonial", re.compile(
        r"\b(fake|invent(ed|ing)?|made[- ]up|make up|fabricat\w*|write (a|some) (fake )?)\b[^.!?\n]{0,30}"
        r"\b(testimonials?|reviews?|case stud(y|ies)|results?|screenshots?|success stor(y|ies))\b", re.I),
     "Invented testimonials, reviews, case studies or results aren't allowed"),
    ("unverified_result", re.compile(
        r"\b(student|client|member|customer)s? (named |called )?[A-Z][a-z]+ (went from|made|hit|closed|doubled|tripled|10x'?d|grew)\b"),
     "A result about a named person needs verified proof and permission; remove it or attach the proof"),
    ("fake_shoutout", re.compile(
        r"\b(?i:shout[- ]?outs?) (?i:to )?[A-Z][a-z]+\b[^.!?\n]{0,40}\b(booked|joined|signed|closed|bought)\b|"
        r"\b[A-Z][a-z]+ (just|has just) (booked|joined|signed up|bought|enrolled)\b"),
     "Booking or sign-up shout-outs with names can't be verified here"),
    ("planted_question", re.compile(
        r"\bplant(ed|ing|s)?\b[^.!?\n]{0,25}\bquestions?\b|\bseed(ed|ing)? the chat\b|"
        r"\b(fake|dummy|second|burner|sock ?puppet|alt) (chat |zoom |ig |instagram )?accounts?\b|"
        r"\bpretend(ing)? to be an? (attendee|customer|student|viewer|lead|buyer)\b|\binvent(ed|ing)? attendees?\b", re.I),
     "Planted audience questions or fake chat accounts aren't allowed"),
    ("invented_credential", re.compile(
        r"\b(say|tell (them|people|the lead|prospects)|claim|mention) (that )?(you'?ve|you have|you) "
        r"(closed|made|generated|done|sold|managed|scaled)\b|\bpretend (you|to have)\b", re.I),
     "Invented credentials aren't allowed; only state what is true"),
    ("fake_scarcity", re.compile(
        r"\bdoors? (are |is )?(clos(e|es|ing)|shutting)\b|\bwon'?t last\b|\blast chance\b|\bselling out fast\b|"
        r"\bbefore it'?s gone\b|\bonly a (few|handful of) (spots|places|seats) left\b|\bhurry\b", re.I),
     "Fake scarcity (\"doors closing\", \"won't last\", \"last chance\") isn't allowed"),
]

_DEADLINE = re.compile(
    r"\b(deadline|clos(es|ing) (on|at|this|tonight|tomorrow|friday|monday|sunday|saturday)|"
    r"enrol(l)?ment (closes|ends)|registration (closes|ends)|ends (tonight|tomorrow|on|this)|until (friday|midnight|sunday)|"
    r"cap(ped)? (it |the cohort |numbers )?at \d+|limited to \d+|only \d+ (spots|places|seats)( left)?|"
    r"starts on the \w+|offer (ends|expires))\b",
    re.I,
)
_REASON = re.compile(
    r"\b(because|since|so that|so (our|the|each|every) |due to|as (we|our|the)|in order to|which (is|means)|"
    r"the reason|to keep|to make sure|so coaches|so we can)\b",
    re.I,
)

_SUBJECT_REPLY = re.compile(r"^\s*subject\s*:\s*(re|fwd?|fw)\s*:", re.I | re.M)
_FIRST_TOUCH_LIE = re.compile(
    r"\b(following up|just following up|as promised|per our (call|conversation|chat)|as we discussed|"
    r"circling back|bumping (this|my last)|my last (message|email))\b",
    re.I,
)

# --------------------------------------------------------------------------- outreach money rule
_ALLOWED_RANGE = re.compile(
    r"\$\s?50\s?[kK]\s?(?:-|–|—|to)\s?\$\s?100\s?[kK]\s?(?:a|per|/)\s?(?:month|mo)\b", re.I)
_MONEY = re.compile(
    r"\$\s?\d[\d,]*(?:\.\d+)?\s?(?:[kKmM]\b)?|\b\d[\d,]*(?:\.\d+)?\s?(?:k|K|grand|dollars|usd|USD|bucks)\b|"
    r"\b(?:one|two|three|four|five|ten|twenty|fifty|hundred)\s?(?:hundred\s)?(?:k|grand|thousand|dollars)\b",
)
_PRICE_WORDS = re.compile(
    r"\b(price[sd]?|pricing|fees?|retainers?|investment|payment plans?|deposit|discount(ed|s)?|per month|"
    r"monthly fee|charge[sd]?|costs? (you|just|only)|it costs?|the cost of (the|our|my) (programme|program|service|offer))\b",
    re.I,
)
_HEDGE = re.compile(r"\b(let me|let us|let's|could|can|help|aim|potentially|might|would)\b", re.I)
_DEFINITE = re.compile(
    r"\b(will (add|make|get|bring|generate|deliver)|adds|guaranteed?|you'?ll (make|get|add|hit)|is going to|"
    r"are going to)\b", re.I)


def mode_for(meta: dict | None) -> str:
    """The outreach rule wins when an item is both prospect-facing and a client asset."""
    meta = meta or {}
    if meta.get("prospect_facing"):
        return "outreach"
    if meta.get("client_asset"):
        return "client_asset"
    return meta.get("mode") if meta.get("mode") in MODES else "plain"


def _name_patterns(names: Iterable[str]) -> list[tuple[str, re.Pattern]]:
    out = []
    for n in names or []:
        n = (n or "").strip()
        if len(n) < 2:
            continue
        out.append((n, re.compile(r"(?<![\w])" + re.escape(n) + r"(?![\w])", re.I)))
    return out


def scrub(text: str, names: Iterable[str]) -> str:
    """Remove every banned name from text shown in the UI or generated output."""
    s = text or ""
    for _, pat in _name_patterns(names):
        s = pat.sub("[name removed]", s)
    return s


def check(text: str, mode: str = "plain", meta: dict | None = None, banned_names: Iterable[str] | None = None) -> list[dict]:
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode}")
    meta = meta or {}
    t = _norm(text)
    flags: list[dict] = []

    # banned names: block and the UI scrubs them
    for name, pat in _name_patterns(banned_names or []):
        m = pat.search(t)
        if m:
            flags.append(_flag("banned_name", "block", "Mentions a name on the banned list", "[name removed]"))

    # in outreach, the allowed hedged range is judged by the money rule, not the income rule
    scan = _ALLOWED_RANGE.sub("the agreed range", t) if mode == "outreach" else t
    for code, pat, msg in DECEPTION_RULES:
        for m in pat.finditer(scan):
            if negated(scan, m.start()):
                continue
            flags.append(_flag(code, "block", msg, m.group(0)))
            break

    # deadlines and capacity limits: a stated reason passes, none -> needs a person
    for m in _DEADLINE.finditer(t):
        if negated(t, m.start()):
            continue
        sent = _sentence_at(t, m.start(), m.end())
        if not _REASON.search(sent):
            flags.append(_flag("deadline_no_reason", "review",
                               "A deadline or capacity limit needs a stated reason; a person should check it", m.group(0)))
            break

    # deceptive subject lines / first-touch lies
    if not meta.get("prior_contact"):
        m = _SUBJECT_REPLY.search(t)
        if m:
            flags.append(_flag("deceptive_subject", "block", "\"Re:\"/\"Fwd:\" on a first touch is deceptive", m.group(0)))
        m = _FIRST_TOUCH_LIE.search(t)
        if m and not negated(t, m.start()):
            flags.append(_flag("deceptive_subject", "block",
                               "\"Following up\" on a first touch pretends there was earlier contact", m.group(0)))

    if mode == "outreach":
        flags.extend(_outreach_money(t, meta))
    return _dedupe(flags)


def _outreach_money(t: str, meta: dict) -> list[dict]:
    flags = []
    tier = (meta.get("lead_tier") or meta.get("tier") or "cold").lower()
    ranges = list(_ALLOWED_RANGE.finditer(t))
    stripped = _ALLOWED_RANGE.sub(" ", t)
    for m in _MONEY.finditer(stripped):
        flags.append(_flag("outreach_money", "block",
                           "Prospect outreach may only mention \"$50k-$100k a month\" (hedged); no other amounts", m.group(0)))
        break
    for m in _PRICE_WORDS.finditer(stripped):
        if negated(stripped, m.start()):
            continue
        flags.append(_flag("outreach_price", "block", "No price, fee or retainer wording in prospect outreach", m.group(0)))
        break
    for m in ranges:
        sent = _sentence_at(t, m.start(), m.end())
        if tier in ("cold", "unqualified", "unknown", "nurture"):
            flags.append(_flag("cold_lead_figure", "block",
                               "Cold or unqualified leads get the no-figure line (\"Let me help [Brand] grow through ...\")",
                               m.group(0)))
        elif _DEFINITE.search(sent):
            flags.append(_flag("outreach_definite", "block", "The range must be hedged, never definite (\"will add\", \"adds\")",
                               _DEFINITE.search(sent).group(0)))
        elif not _HEDGE.search(sent):
            flags.append(_flag("outreach_unhedged", "block",
                               "Hedge the range: \"Let me make [Brand] $50k-$100k a month through ...\"", m.group(0)))
    return flags


def _dedupe(flags: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for f in flags:
        k = (f["code"], f["match"].lower())
        if k in seen:
            continue
        seen.add(k)
        out.append(f)
    return out


def blocked(flags: list[dict]) -> bool:
    return any(f["severity"] == "block" for f in flags)


def needs_review(flags: list[dict]) -> bool:
    return any(f["severity"] == "review" for f in flags)


# --------------------------------------------------------------------------- helpers used by drafting code
AI_WORDS = [
    "delve", "unlock", "elevate", "game-changer", "game changer", "in today's fast-paced", "leverage", "synergy",
    "revolutionize", "revolutionise", "seamless", "cutting-edge", "tapestry", "embark", "supercharge", "skyrocket",
    "unleash", "dive deep", "harness the power", "look no further", "rest assured", "navigate the complexities",
]


def ai_words(text: str) -> list[str]:
    low = (text or "").lower()
    return [w for w in AI_WORDS if w in low]


def no_figure_line(brand: str, mechanism: str = "our system") -> str:
    return f"Let me help {brand} grow through {mechanism}."


def range_line(brand: str, mechanism: str = "our system") -> str:
    return f"Let me make {brand} $50k-$100k a month through {mechanism}."


def tenant_banned_names(tenant) -> list[str]:
    import os

    names = list(tenant.get("banned_names", []) or []) if tenant is not None else []
    env = os.environ.get("OPERATOR_BANNED_NAMES", "")
    names += [n.strip() for n in env.split(",") if n.strip()]
    persona = tenant.get("persona", {}) if tenant is not None else {}
    return [n for n in names if n] + list((persona or {}).get("banned_words", []) or [])


def check_for(tenant, text: str, meta: dict | None = None) -> list[dict]:
    """check() with the tenant's banned names and banned words, mode chosen from meta."""
    return check(text, mode_for(meta), meta, tenant_banned_names(tenant))


_banned_cache: dict[str, tuple[float, list[str]]] = {}


def banned_cached(tid: str) -> list[str]:
    """Banned names for a tenant id, cached for two seconds (used by the response scrub)."""
    import time

    from . import tenancy

    hit = _banned_cache.get(tid)
    if hit and hit[0] > time.time():
        return hit[1]
    try:
        names = tenant_banned_names(tenancy.get(tid))
    except Exception:  # noqa: BLE001 - unknown tenant: the route itself answers 404
        names = []
    _banned_cache[tid] = (time.time() + 2.0, names)
    return names


def banned_cache_clear(tid: str) -> None:
    _banned_cache.pop(tid, None)
