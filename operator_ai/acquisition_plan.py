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
        "Make 2 short videos (10-25 minutes), each one fixing a problem a specific lead told you about, and send it "
        "to them: \"You gave me the idea for this one.\"",
        "Post 3 short clips a day. Add subtitles, a title about a problem or result, and \"comment GUIDE and I'll "
        "send it to you\" for a free resource.",
        "Message everyone who comments, likes or asks for the free resource, and add them to your list of leads.",
    ]
    if m and s.get("clients"):
        have = int(s["clients"])
        m["gap_clients"] = max(0, m["clients"] - have)
    if m:
        week.insert(0, f"Book {m['booked_per_week']} sales calls this week. That's the pace that gets you to "
                       f"{_fmt(m['goal'])} a month.")
    if "ads" in ch:
        week.append("If you run ads, judge them by how much each booked call costs, not by clicks or likes.")
    if "outreach" in ch or not ch:
        week.append("Reach out only to people who can afford a high price. Send a short personal video that shows a "
                    "result first and the offer second.")
    follow = ["Keep giving every lead something useful at least once a week: a video or free resource about the "
              "exact problem they have.",
              "Never follow up with \"any update?\". Send value instead: \"I made a video about the issue you "
              "mentioned.\"",
              "When something happens on a call or a launch, film a quick clip about it right away. Those get the "
              "most views."]
    close_steps = ["On the sales call, show them their own numbers and the plan to hit their goal. You don't need a "
                   "script.",
                   "When they say yes, ask for a small refundable deposit right then: \"When people decide to move "
                   "forward, we take a small refundable deposit so we both know you're in. Are you open to doing "
                   "that now?\""]
    return {"situation": s, "assumed": assumed, "math": m, "higher_price": alt, "this_week": week,
            "follow_up": follow, "close": close_steps, "ask": ask, "sources": SOURCES}


def reply_text(plan: dict) -> str:
    m, alt = plan["math"], plan["higher_price"]
    lines = ["Here's your plan to get more clients."]
    if m:
        lines += ["", f"First, the math. To make {_fmt(m['goal'])} a month at {_fmt(m['price'])} per client, you need "
                      f"{m['clients']} clients. That means about {m['calls']} sales calls, and {m['booked']} calls on "
                      f"the calendar each month, because some people won't show up. That's about "
                      f"{m['booked_per_week']} booked calls a week."]
        if m.get("gap_clients") is not None:
            have = int(plan["situation"]["clients"])
            lines.append(f"You have {have} clients now, so you need {m['gap_clients']} more.")
        if alt:
            lines.append(f"Here's the shortcut: at {_fmt(alt['price'])} per client you'd only need {alt['clients']} "
                         f"clients and {alt['booked']} booked calls a month. Raising your price is the biggest lever.")
    else:
        lines += ["", "Start with the math: your monthly goal divided by your price tells you how many clients you "
                      "need. From there you can work out how many calls to book each week."]
    lines += ["", "What to do this week:"] + [f"• {w}" for w in plan["this_week"][:4]]
    if plan["ask"]:
        lines += ["", "Tell me " + " and ".join(plan["ask"]) + ", and I'll work out your exact numbers."]
    elif plan["assumed"]:
        lines += ["", "I guessed your " + " and ".join(k.replace("_", " ") for k in plan["assumed"])
                  + " using typical numbers. Send me yours and I'll make it exact."]
    return "\n".join(lines)


AI_SYSTEM = (
    "You are a friendly, direct coach for a high-ticket growth programme. Answer the student's question about getting "
    "more clients using ONLY the method and the numbers given. Use plain, simple words a beginner understands; explain "
    "any term you use. Keep every number exactly as given and never invent results, income claims or guarantees. "
    "Never mention other companies, tools you were not given, or partnerships. Structure: one line on their "
    "situation, the math in 2-3 sentences, 'What to do this week' as 3-5 bullets starting with '• ', then one line "
    "asking for anything missing. Plain text only: no markdown, no asterisks, no headings symbols. Under 220 words. "
    "If their numbers are missing, still give the method and this week's steps, then ask for the numbers."
)


def _ai_prompt(text: str, plan: dict) -> str:
    m, alt = plan["math"], plan["higher_price"]
    facts = [f"Student wrote: {text}"]
    if m:
        facts.append(f"Math: goal {_fmt(m['goal'])}/month, price {_fmt(m['price'])}, clients needed {m['clients']}, "
                     f"sales calls {m['calls']}, booked calls {m['booked']}/month ({m['booked_per_week']}/week), "
                     f"cost if buying calls {_fmt(m['spend'])}" + (f", clients still needed {m['gap_clients']}"
                                                                    if m.get("gap_clients") is not None else ""))
    if alt:
        facts.append(f"At {_fmt(alt['price'])}/client: {alt['clients']} clients, {alt['booked']} booked calls/month.")
    facts.append("Assumed from typical numbers: " + (", ".join(plan["assumed"]) or "nothing"))
    facts.append("Still to ask for: " + (", ".join(plan["ask"]) or "nothing"))
    facts.append("Method steps this week: " + " | ".join(plan["this_week"]))
    facts.append("Follow-up: " + " | ".join(plan["follow_up"]) + " Close: " + " | ".join(plan["close"]))
    return "\n".join(facts)


def from_chat(text: str, previous: dict | None = None, tenant=None) -> tuple[str, dict]:
    """Chat entry point; `previous` is the situation from an earlier plan in this thread, so a reply with numbers
    sharpens the same plan. With AI available the plan is written in a coach's voice from the same facts; without
    it, the plain template below."""
    s = {**(previous or {}), **{k: v for k, v in parse(text).items() if v not in (None, [], "")}}
    plan = build(s)
    reply, mode = reply_text(plan), "rules"
    if tenant is not None:
        from . import llm

        if llm.unavailable_reason(tenant) is None:
            try:
                out = llm.complete(tenant, AI_SYSTEM, _ai_prompt(text, plan), purpose="acquisition_plan",
                                   max_tokens=2000, timeout=45.0)
                text_out = out["text"].strip().replace("**", "")
                if text_out:
                    reply, mode = text_out, "live"
            except llm.AIUnavailable:
                pass
    return reply, {"acquisition": plan, "mode": mode}
