"""Client acquisition plan: "how do I get more clients?" answered with the programme's own method, worked out on the
asker's numbers.

The method (from the programme's lessons, in our words):
1. Money math first. Work back from the monthly goal: clients needed, sales calls (close rate), booked calls (show
   rate), and what those booked calls cost. If the numbers look impossible, the price is usually the lever.
2. Pipeline videos. 10-25 minute videos made about the bottleneck a specific prospect has, sent to them directly as
   follow-up. Pitch at the start, a free resource and paid options in the description.
3. Be everywhere. Daily short clips with subtitles, a title that anchors a problem or outcome, and a "comment
   KEYWORD" lead magnet on screen and on the first line of the caption. On-site clips filmed in the moment.
4. The washing machine. Everyone who engages gets a conversation, goes into the CRM and keeps receiving videos and
   free resources about their exact problem until they are ready. Follow up by giving value, never by chasing.
5. Close on the plan. Show the prospect their own numbers and the plan on the call; on a verbal yes, ask for a
   refundable deposit.

Pure rules, no AI needed: every figure in the plan comes from the asker's message or a labelled assumption.
"""
from __future__ import annotations

import math
import re

SOURCES = [
    {"title": "Recurring high-ticket model: masterclass, week 1", "section": "The money math"},
    {"title": "Group call: client acquisition and follow-up",
     "section": "Pipeline videos, short-form, the washing machine"},
]

ASK = re.compile(
    r"\b(more|new|high[- ]ticket|bigger|better)\b[^.?!\n]{0,20}\b(clients?|customers?|deals?)\b"
    r"|\bclient acquisition\b|\b(get|find|land|sign) (clients|customers)\b", re.I)
# what the asker's own numbers look like in a message
_MONEY = r"\$?\s*(\d+(?:[.,]\d+)?)\s*(k|m|grand|thousand)?"
_PATTERNS = {
    "price": [rf"(?:charge|charging|price|priced at|ticket|offer is|sell (?:it )?for|retainer(?: of)?|package(?: is)?)"
              rf"\D{{0,12}}{_MONEY}", rf"{_MONEY}\s*(?:/|per|a)\s*(?:client|deal|sale|package|month retainer)"],
    "goal": [rf"(?:hit|reach|get to|make|goal(?: is)?(?: of)?|target(?: of)?|to)\s*{_MONEY}"
             rf"\s*(?:/|per|a)\s*(?:month|mo)\b",
             rf"{_MONEY}\s*(?:/|per|a)\s*(?:month|mo)\b"],
    "clients": [r"(?:have|got|at|with|currently)\s*(\d+)\s*(?:paying\s+)?clients?",
                r"(\d+)\s*(?:paying\s+)?clients? (?:now|right now|currently)"],
    "close_rate": [r"clos(?:e|ing) rate\D{0,10}(\d+(?:\.\d+)?)\s*%", r"(\d+(?:\.\d+)?)\s*%\s*close"],
    "show_rate": [r"show(?:[- ]up)? rate\D{0,10}(\d+(?:\.\d+)?)\s*%", r"(\d+(?:\.\d+)?)\s*%\s*show"],
    "cost_per_call": [rf"(?:cost per (?:booked )?call|per booked call|cpbc)\D{{0,8}}{_MONEY}",
                      rf"{_MONEY}\s*(?:per|a|/)\s*booked call"],
}
_CHANNELS = {"ads": r"\b(ads?|meta|facebook|paid)\b", "outreach": r"\b(outreach|dms?|cold|email)\b",
             "youtube": r"\byoutube\b", "instagram": r"\b(instagram|ig|reels?)\b", "webinar": r"\bwebinars?\b",
             "referrals": r"\breferrals?\b"}
DEFAULTS = {"close_rate": 20.0, "show_rate": 50.0, "cost_per_call": 150.0}


def wants_plan(text: str) -> bool:
    """A question about getting clients, not something pasted for review (that is an asset, even if it says
    'more clients')."""
    t = (text or "").strip()
    if not t or len(t) > 600 or "\n" in t or re.match(r"\s*(review|check|critique|roast|feedback)\b", t, re.I):
        return False
    return bool(ASK.search(t))


def _money(num: str, unit: str | None) -> float:
    v = float(num.replace(",", ""))
    u = (unit or "").lower()
    return v * 1000 if u in ("k", "grand", "thousand") else v * 1_000_000 if u == "m" else v


def parse(text: str) -> dict:
    """The asker's situation, as far as their message says it."""
    low = (text or "").lower()
    out: dict = {}
    for key, pats in _PATTERNS.items():
        for p in pats:
            m = re.search(p, low)
            if not m:
                continue
            if key in ("price", "goal", "cost_per_call"):
                out[key] = _money(m.group(1), m.group(2) if m.lastindex and m.lastindex >= 2 else None)
            else:
                out[key] = float(m.group(1))
            break
    if out.get("goal") and out.get("price") == out["goal"]:
        out.pop("price")  # "$30k a month" read twice: it is the goal, not the price
    out["channels"] = [c for c, p in _CHANNELS.items() if re.search(p, low)]
    return out


def _fmt(v: float) -> str:
    return f"${v:,.0f}"


def math_rows(price: float, goal: float, close: float, show: float, cpc: float) -> dict:
    clients = max(1, math.ceil(goal / price))
    calls = math.ceil(clients / (close / 100))
    booked = math.ceil(calls / (show / 100))
    return {"price": price, "goal": goal, "clients": clients, "calls": calls, "booked": booked,
            "booked_per_week": math.ceil(booked / 4.3), "spend": booked * cpc}


def build(situation: dict) -> dict:
    """The plan for this situation: money math (theirs plus a higher-price comparison), weekly actions, follow-up and
    the close, with what we assumed and what to tell us to sharpen it."""
    s = dict(situation)
    assumed = [k for k in ("close_rate", "show_rate", "cost_per_call") if k not in s]
    close, show, cpc = (s.get(k, DEFAULTS[k]) for k in ("close_rate", "show_rate", "cost_per_call"))
    price, goal = s.get("price"), s.get("goal")
    ask = [q for k, q in (("price", "what you charge per client"), ("goal", "your monthly revenue goal")) if k not in s]
    m = alt = None
    if price and goal:
        m = math_rows(price, goal, close, show, cpc)
        if price < 10_000:  # the programme's lever: the same goal at a higher price
            alt = math_rows(max(10_000.0, price * 3), goal, close, show, cpc * 1.5)
    ch = s.get("channels") or []
    week = [
        "Make 2 pipeline videos (10-25 min), each about one prospect's bottleneck, and send them to that prospect: "
        "'You inspired this one.'",
        "Post 3 short clips a day: subtitles, a title that anchors a problem or outcome, and 'comment KEYWORD' for a "
        "free resource, on screen and on the first line of the caption.",
        "Start a conversation with everyone who comments, likes or asks for the resource, and log them in the CRM.",
    ]
    if m and s.get("clients"):
        have = int(s["clients"])
        m["gap_clients"] = max(0, m["clients"] - have)
    if m:
        week.insert(0, f"Book {m['booked_per_week']} calls this week ({m['booked']} a month gets you to "
                       f"{_fmt(m['goal'])}).")
    if "ads" in ch:
        week.append("Ads: fund the booked-call target above and judge creatives on cost per booked call.")
    if "outreach" in ch or not ch:
        week.append("Outreach: send short personal video pitches to high-ticket prospects only, opening with a result "
                    "or case study before any offer.")
    follow = ["Every lead goes into the washing machine: a relevant video or free resource at least once a week.",
              "Follow up by sending value ('new video on the exact issue you mentioned'), never 'any update?'.",
              "Film on-site clips when something happens on a call or a launch; those are the ones that travel."]
    close_steps = ["On the call, show their own numbers and the plan to hit their goal; no script needed.",
                   "On a verbal yes, ask for a refundable deposit now: 'When people agree to move forward we take a "
                   "small refundable deposit, so we both know you're in. Are you open to doing that now?'"]
    return {"situation": s, "assumed": assumed, "math": m, "higher_price": alt, "this_week": week,
            "follow_up": follow, "close": close_steps, "ask": ask, "sources": SOURCES}


def reply_text(plan: dict) -> str:
    m, alt = plan["math"], plan["higher_price"]
    lines = ["Here's your client acquisition plan, built on the programme's method."]
    if m:
        lines += ["", f"The money math: {_fmt(m['goal'])} a month at {_fmt(m['price'])} a client is {m['clients']} "
                      f"clients, so {m['calls']} sales calls and {m['booked']} booked calls a month "
                      f"(about {m['booked_per_week']} a week), roughly {_fmt(m['spend'])} if you buy those calls."]
        if m.get("gap_clients") is not None:
            have = int(plan["situation"]["clients"])
            lines.append(f"You have {have} now, so you're {m['gap_clients']} clients away.")
        if alt:
            lines.append(f"At {_fmt(alt['price'])} a client the same goal needs only {alt['clients']} clients and "
                         f"{alt['booked']} booked calls. Price is the biggest lever you have.")
    else:
        lines += ["", "Start with the money math: goal ÷ price = clients needed, ÷ close rate = calls, ÷ show rate = "
                      "booked calls. Then you know exactly what to do each week."]
    lines += ["", "This week:"] + [f"• {w}" for w in plan["this_week"][:4]]
    if plan["ask"]:
        lines += ["", "Tell me " + ", ".join(plan["ask"]) + " and I'll put your exact numbers in."]
    elif plan["assumed"]:
        lines += ["", "I assumed " + ", ".join(k.replace("_", " ") for k in plan["assumed"])
                  + " from typical numbers; send yours and I'll tighten it."]
    return "\n".join(lines)


def from_chat(text: str, previous: dict | None = None) -> tuple[str, dict]:
    """Chat entry point; `previous` is the situation from an earlier plan in this thread, so a reply with numbers
    sharpens the same plan."""
    s = {**(previous or {}), **{k: v for k, v in parse(text).items() if v not in (None, [], "")}}
    plan = build(s)
    return reply_text(plan), {"acquisition": plan}
