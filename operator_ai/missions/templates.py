"""Deterministic mission templates (DAGs of registry steps). They work with AI off; the optional AI planner can
only suggest changes to them, which a person confirms.

Targets come from outreach_loop.goal_calculator with the SOP benchmark rates (reply 10%, conversation 40%,
link 50%, booked 30%, show 60%, close 20%). They are a model, never a promise.
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field

from .registry import WEEKDAYS

RATES = {"reply": 0.10, "conversation": 0.40, "link": 0.50, "booked": 0.30, "show": 0.60, "close": 0.20}


@dataclass
class Q:
    key: str
    label: str
    kind: str = "text"            # text | number | money | date | select | client
    required: bool = False
    help: str = ""
    options: tuple = ()
    min: float = 0
    max: float = 10_000_000
    placeholder: str = ""

    def public(self) -> dict:
        return {"key": self.key, "label": self.label, "kind": self.kind, "required": self.required, "help": self.help,
                "options": list(self.options), "min": self.min, "max": self.max, "placeholder": self.placeholder}


@dataclass
class Template:
    key: str
    label: str
    description: str
    outcome: str
    roles: tuple
    questions: list[Q]
    steps: list[dict]
    pattern: str
    examples: tuple = ()
    leading: tuple = ()
    lagging: tuple = ()
    builder: object = None
    extra: dict = field(default_factory=dict)

    def build(self, answers: dict) -> list[dict]:
        specs = [dict(s, params=dict(s.get("params") or {})) for s in self.steps]
        if callable(self.builder):
            specs = self.builder(specs, answers)
        return specs

    def public(self) -> dict:
        return {"key": self.key, "label": self.label, "description": self.description, "outcome": self.outcome,
                "questions": [q.public() for q in self.questions], "examples": list(self.examples),
                "steps": [{"skey": s["skey"], "type": s["type"], "title": s["title"]} for s in self.steps]}


def S(skey: str, type_: str, title: str, depends: tuple | list = (), **params) -> dict:
    return {"skey": skey, "type": type_, "title": title, "depends": list(depends), "params": params}


# --------------------------------------------------------------------------- questions
NICHE = Q("niche", "Which niche do you serve?", required=True, placeholder="e.g. fitness coaches")
OFFER = Q("offer", "What do you offer them?", required=True, placeholder="e.g. done-for-you DM setting")
PRICE = Q("price", "Your price (USD)", "money", required=True, min=1,
          help="Your own offer price. Used for targets and proposals, never in cold messages.")
HOURS = Q("hours_per_day", "Hours a day you can give this", "number", min=0.5, max=16)
CHANNELS = Q("channels", "Where will you reach people?", "select", options=("Instagram DMs", "Email", "LinkedIn", "Phone"))
OUTCOME = Q("outcome", "The result your clients want", placeholder="e.g. 20 booked calls a month")
CALLS = Q("calls_target", "How many calls do you want booked?", "number", required=True, min=1, max=500)
EVENT_DATE = Q("event_date", "Event date", "date", required=True)
TARGET_CPL = Q("target_cpl", "Target cost per lead (USD)", "money", min=0.5, max=10_000)
CLIENT = Q("client_id", "Which client?", "client")
WEEKDAY = Q("weekday", "Report day", "select", options=tuple(d.capitalize() for d in WEEKDAYS))
NAME = Q("name", "Student's name (leave empty if this plan is for you)")
EMAIL = Q("email", "Student's email")
NUMBERS = [Q("dms_sent", "DMs sent (last 30 days)", "number", max=1_000_000),
           Q("replies", "Replies", "number", max=1_000_000),
           Q("conversations", "Quality conversations", "number", max=1_000_000),
           Q("links_sent", "Booking links sent", "number", max=1_000_000),
           Q("booked", "Calls booked", "number", max=1_000_000),
           Q("showed", "Calls that showed", "number", max=1_000_000),
           Q("closed", "Sales closed", "number", max=1_000_000),
           Q("offer_price", "Your price (USD)", "money", max=10_000_000)]

ALL_Q: dict[str, Q] = {q.key: q for q in [NICHE, OFFER, PRICE, HOURS, CHANNELS, OUTCOME, CALLS, EVENT_DATE, TARGET_CPL,
                                           CLIENT, WEEKDAY, NAME, EMAIL, *NUMBERS]}


# --------------------------------------------------------------------------- templates
_OUTREACH_CORE = [
    S("prospects", "prospect_list", "Build your prospect list", ["intake"], min=30),
    S("research", "research_fit", "Research and fit-score the best 50", ["prospects"], top=50),
]


def _fill_builder(specs: list[dict], answers: dict) -> list[dict]:
    for s in specs:
        if s["skey"] == "booking" and answers.get("calls_target"):
            s["params"]["target"] = int(answers["calls_target"])
    return specs


def _student_builder(specs: list[dict], answers: dict) -> list[dict]:
    return specs


TEMPLATES: dict[str, Template] = {}


def _add(t: Template) -> None:
    TEMPLATES[t.key] = t


_add(Template(
    "first_client", "Win my first client",
    "The full path to a first paying client: offer, profile, prospect list, research, daily capped openers, "
    "follow-ups, replies, booking, call prep, the call, the proposal and onboarding the client.",
    "A first client says yes", ("owner", "coach", "setter", "closer", "student"),
    [NICHE, OFFER, PRICE, HOURS, CHANNELS],
    [S("intake", "intake", "Answer the planning questions"),
     S("offer_lesson", "cert_gate", "Offer and USP lesson and quiz", ["intake"], path_key="growth_operator", level="L1",
       label="the offer and USP lesson and quiz"),
     S("offer_doc", "offer_doc", "Write your offer document", ["offer_lesson"]),
     S("profile", "profile_assets", "Profile and bio", ["offer_doc"]),
     *_OUTREACH_CORE,
     S("dm_cert", "cert_gate", "DM-setting certification", ["offer_lesson"], path_key="setter", level="L1",
       label="the DM-setting certification"),
     S("openers", "daily_openers", "Daily opener blocks (capped)", ["profile", "research", "dm_cert"]),
     S("followups", "followups", "Follow-ups on days 2, 4 and 7", ["openers"]),
     S("replies", "reply_watch", "Handle replies", ["openers"]),
     S("booking", "booking", "Book the call", ["replies"]),
     S("prep", "precall_prep", "Pre-call brief and role-play", ["booking"]),
     S("call", "take_call", "Take the discovery call", ["prep"]),
     S("review", "call_review", "Review the call (optional)", ["call"]),
     S("proposal", "proposal", "Send the proposal", ["call"]),
     S("win", "await_win", "Win the client", ["proposal"]),
     S("onboarding", "client_onboarding_start", "Start client onboarding", ["win"]),
     S("day7", "checkpoint", "Day 7 check", ["intake"], day=7),
     S("day14", "checkpoint", "Day 14 check", ["intake"], day=14),
     S("day30", "checkpoint", "Day 30 check", ["intake"], day=30)],
    r"\bfirst (paying )?(client|customer)\b|\b(get|win|land|sign|close|find)\b[^.?!]{0,20}\b(clients?|customers?)\b",
    ("Get my first client", "Help me win my first paying client"),
    leading=("prospects", "openers_sent", "replies"), lagging=("calls_booked", "calls_showed", "proposals_sent", "wins")))

_add(Template(
    "fill_calendar", "Fill my calendar",
    "Book a set number of calls: prospect list, research, capped daily openers, follow-ups, replies, booking, "
    "confirmations and reminders.", "The calls target is booked", ("owner", "coach", "setter", "closer", "student"),
    [CALLS, NICHE, OFFER, PRICE, HOURS],
    [S("intake", "intake", "Answer the planning questions"),
     *_OUTREACH_CORE,
     S("dm_cert", "cert_gate", "DM-setting certification", ["intake"], path_key="setter", level="L1",
       label="the DM-setting certification"),
     S("openers", "daily_openers", "Daily opener blocks (capped)", ["research", "dm_cert"]),
     S("followups", "followups", "Follow-ups on days 2, 4 and 7", ["openers"]),
     S("replies", "reply_watch", "Handle replies", ["openers"]),
     S("booking", "booking", "Book the calls", ["replies"]),
     S("reminders", "reminder_ladder", "Confirmations and reminders", ["booking"]),
     S("day7", "checkpoint", "Day 7 check", ["intake"], day=7),
     S("day14", "checkpoint", "Day 14 check", ["intake"], day=14),
     S("day30", "checkpoint", "Day 30 check", ["intake"], day=30)],
    r"\bbook(ed)?\b[^.?!]{0,15}\b\d+\b[^.?!]{0,15}\bcalls?\b|\b\d+\s+(more\s+)?(sales\s+|discovery\s+|strategy\s+)?calls?\b|"
    r"\bfill (up )?(my |the )?(calendar|diary)\b|\bmore (booked |sales )?calls\b",
    ("Book 20 calls this month", "Fill my calendar"),
    leading=("prospects", "openers_sent", "replies"), lagging=("calls_booked", "calls_showed"),
    builder=_fill_builder))

_add(Template(
    "launch_webinar", "Launch a webinar",
    "Webinar assets from one brief (offer, hooks, 7-segment script, pages, show-up and replay emails, run-of-show, "
    "sales hand-off), scheduled, promoted and followed up.", "The webinar runs and attendees are followed up",
    ("owner", "coach", "student"), [OFFER, NICHE, PRICE, EVENT_DATE],
    [S("intake", "intake", "Answer the planning questions"),
     S("chain", "webinar_chain", "Draft the webinar assets", ["intake"]),
     S("schedule", "webinar_schedule", "Schedule the webinar", ["chain"]),
     S("promote", "studio_line", "Draft the promotion ads", ["chain"], line="ads"),
     S("event", "wait_until", "Webinar day", ["schedule"], at_key="event_date", label="the webinar"),
     S("after", "human_task", "After the event", ["event"],
       task="After the event: import attendance, send the replay and hand engaged attendees to sales.",
       href="#/webinar"),
     S("day14", "checkpoint", "Day 14 check", ["intake"], day=14)],
    r"\bwebinar\b|\bmasterclass\b|\blive (training|workshop)\b",
    ("Launch a webinar", "Run a masterclass next month")))

_add(Template(
    "fix_bottleneck", "Fix my bottleneck",
    "Find the one stage costing the most against the SOP benchmarks, draft the fix, then re-measure.",
    "The bottleneck stage improves", ("owner", "coach", "setter", "closer", "student"), NUMBERS,
    [S("intake", "intake", "Enter your current numbers"),
     S("diagnose", "diagnose", "Find the bottleneck", ["intake"]),
     S("fix", "studio_line", "Draft the fix", ["diagnose"]),
     S("remeasure", "remeasure", "Re-measure after 14 days", ["fix"], days=14)],
    r"\bbottleneck\b|\bfix (my |the )?(funnel|numbers|pipeline|conversion)\b|\b(reply|show|close|closing|booking) rate\b",
    ("Fix my bottleneck", "Fix my show rate")))

_add(Template(
    "scale_ads", "Scale my ads",
    "Read the ad account (read-only), propose pauses for owner approval, build a paused creative test and re-read "
    "after 72 hours.", "Wasted spend stopped and a winner found", ("owner",), [TARGET_CPL],
    [S("intake", "intake", "Set the target cost per lead"),
     S("read", "ads_diagnose", "Read the ad account", ["intake"]),
     S("propose", "ads_propose", "Propose ad changes", ["read"]),
     S("test", "ads_test", "Build a new creative test", ["propose"]),
     S("day3", "checkpoint", "72-hour check", ["intake"], day=3),
     S("reread", "ads_diagnose", "Read the account again", ["day3"])],
    r"\bscale (my |the )?ads?\b|\bads? (are|is)n?'?t? (not )?working\b|\bcost per lead\b|\bcpl\b|\bthunder ?dome\b",
    ("Scale my ads",)))

_add(Template(
    "client_onboarding", "Onboard a client",
    "Money model, bottleneck, launch plan, hooks, VSL outline and the first report for a new client, then the "
    "kickoff and the weekly reports.", "The client is onboarded", ("owner", "coach", "student"), [CLIENT],
    [S("intake", "intake", "Choose the client"),
     S("plan", "fulfil_client", "Draft the client plan", ["intake"]),
     S("kickoff", "human_task", "Hold the kickoff call", ["plan"],
       task="Hold the kickoff call with the client and agree the first 30 days."),
     S("weekly", "spawn_mission", "Start the weekly client reports", ["kickoff"], template="client_weekly_ops")],
    r"\bonboard(ing)? (my |a |the )?(new )?client\b|\bclient onboarding\b|\bnew client\b",
    ("Onboard my new client",)))

_add(Template(
    "client_weekly_ops", "Weekly client reports",
    "Every week on your chosen day: each client's numbers, the bottleneck and a client report, drafted for approval.",
    "A report goes out every week", ("owner", "coach", "student"), [CLIENT, WEEKDAY],
    [S("intake", "intake", "Choose the client and the day"),
     S("weekly", "weekly_client_ops", "Weekly client report", ["intake"])],
    r"\bweekly (client )?reports?\b|\bclient reports?\b|\bweekly ops\b|\breport(ing)? to (my )?clients?\b",
    ("Send my clients a weekly report",)))

_add(Template(
    "student_90_day", "First 90 days",
    "Onboard (intake, gaps, cited lessons, a 30/60/90 plan, a coach), pass the growth-operator basics, start the "
    "first-client mission and check in on day 30, 60 and 90.", "Day 90 reached with the plan followed",
    ("owner", "coach", "student"), [NAME, EMAIL, NICHE],
    [S("intake", "intake", "Answer the planning questions"),
     S("onboard", "onboard_student", "Onboard the student", ["intake"]),
     S("basics", "cert_gate", "Growth-operator basics", ["onboard"], path_key="growth_operator", level="L1",
       label="the growth-operator basics"),
     S("first", "spawn_mission", "Start the first-client mission", ["basics"], template="first_client"),
     S("day30", "checkpoint", "Day 30 check", ["intake"], day=30),
     S("day60", "checkpoint", "Day 60 check", ["intake"], day=60),
     S("day90", "checkpoint", "Day 90 check", ["intake"], day=90)],
    r"\b90[- ]?days?\b|\bnew student\b|\bonboard (a |the |my )?(new )?student\b",
    ("Plan my first 90 days",), builder=_student_builder))

CUSTOM = "custom"
CUSTOM_LABEL = "Custom goal (closest plan)"
OUTREACH_TEMPLATES = ("first_client", "fill_calendar")


def get(key: str | None) -> Template | None:
    return TEMPLATES.get(key or "")


def for_role(role: str) -> list[Template]:
    return [t for t in TEMPLATES.values() if role in t.roles]


# --------------------------------------------------------------------------- classify a goal
_ORDER = ["fill_calendar", "first_client", "launch_webinar", "scale_ads", "client_weekly_ops", "client_onboarding",
          "student_90_day", "fix_bottleneck"]
_DEFAULT = {"student": "first_client", "setter": "fill_calendar", "closer": "fill_calendar", "coach": "fix_bottleneck",
            "owner": "fix_bottleneck"}


def match(text: str) -> str | None:
    low = (text or "").lower()
    for k in _ORDER:
        if re.search(TEMPLATES[k].pattern, low):
            return k
    return None


def classify(text: str, role: str = "student") -> tuple[str, bool]:
    """(template key, matched). Unmatched goals get the nearest plan for the role."""
    k = match(text)
    if k and role in TEMPLATES[k].roles:
        return k, True
    d = _DEFAULT.get(role, "first_client")
    return d, False


_MONEY = re.compile(r"^\s*\$?\s*(\d[\d,]*(?:\.\d+)?)\s*([km])?\s*(usd|dollars)?\s*$", re.I)


def parse_money(v) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = _MONEY.match(str(v or ""))
    if not m:
        return None
    x = float(m.group(1).replace(",", ""))
    return x * (1000 if (m.group(2) or "").lower() == "k" else 1_000_000 if (m.group(2) or "").lower() == "m" else 1)


def extract(text: str) -> dict:
    """Answers a goal sentence already gives: "book 20 calls", "at $3k", "for fitness coaches"."""
    low = (text or "").lower()
    out: dict = {}
    m = re.search(r"\b(\d{1,3})\s+(?:more\s+)?(?:sales\s+|discovery\s+|strategy\s+)?calls?\b", low)
    if m:
        out["calls_target"] = int(m.group(1))
    m = re.search(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s?(k)?\b", low)
    if m:
        out["price"] = parse_money(m.group(0))
    m = re.search(r"\b(?:for|serving|with)\s+((?:[a-z][a-z\-]*\s){0,3}(?:coaches|creators|experts|consultants|agencies|"
                  r"clinics|founders|trainers|therapists|brands|businesses|owners|practitioners|course creators))\b", low)
    if m:
        out["niche"] = m.group(1).strip()
    return out


# --------------------------------------------------------------------------- answers
class AnswerError(ValueError):
    pass


def _date(v) -> float:
    if isinstance(v, bool):
        raise AnswerError("Event date: use a date like 2026-11-20")
    if isinstance(v, (int, float)):
        if not math.isfinite(v) or v <= 0:
            raise AnswerError("Event date: use a date like 2026-11-20")
        return float(v)
    try:
        t = time.strptime(str(v).strip()[:10], "%Y-%m-%d")
    except ValueError:
        raise AnswerError("Event date: use a date like 2026-11-20")
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 19, 0, 0, 0, 0, -1))


def clean_one(q: Q, v):
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if q.kind in ("number", "money"):
        x = parse_money(v) if not isinstance(v, (int, float)) or isinstance(v, bool) else float(v)
        if x is None or not math.isfinite(x):
            raise AnswerError(f"{q.label}: enter a number")
        if x < q.min or x > q.max:
            raise AnswerError(f"{q.label}: enter a number between {q.min:g} and {q.max:,.0f}")
        return x
    if q.kind == "date":
        return _date(v)
    if q.kind == "client":
        if isinstance(v, bool) or not str(v).strip().isdigit() or int(str(v).strip()) <= 0:
            raise AnswerError(f"{q.label}: choose a client")
        return int(str(v).strip())
    if not isinstance(v, (str, int, float)) or isinstance(v, bool):
        raise AnswerError(f"{q.label}: enter text")
    s = " ".join(str(v).split())[:200]
    if q.kind == "select" and q.options:
        hit = next((o for o in q.options if o.lower() == s.lower()), None)
        if hit is None:
            raise AnswerError(f"{q.label}: choose one of {', '.join(q.options)}")
        return hit
    return s


def clean_answers(raw: dict | None) -> dict:
    """Validate answers against the question bank. Unknown keys are dropped; bad values raise AnswerError."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise AnswerError("Answers must be a set of named values")
    out = {}
    for k, v in raw.items():
        q = ALL_Q.get(str(k))
        if q is None:
            continue
        val = clean_one(q, v)
        if val is not None:
            out[q.key] = val
    return out


def missing(tpl: Template, answers: dict) -> list[Q]:
    if tpl.key == "fix_bottleneck":
        have = [q for q in NUMBERS if q.key != "offer_price" and isinstance(answers.get(q.key), (int, float))]
        return [] if len(have) >= 2 else [Q("numbers", "at least two of your current numbers (for example DMs sent and replies)")]
    return [q for q in tpl.questions if q.required and answers.get(q.key) in (None, "")]


# --------------------------------------------------------------------------- targets
def targets_for(tenant, template: str, answers: dict) -> dict:
    """Measurable targets from the goal calculator at the SOP benchmark rates. A model, not a promise."""
    from .. import outreach_loop

    cap = outreach_loop.cap(tenant)
    if template not in OUTREACH_TEMPLATES:
        return {"summary": "Plan set. Progress is measured at each step and each checkpoint.",
                "note": "Targets for this plan are the steps themselves."}
    price = float(answers.get("price") or answers.get("offer_price") or 3000)
    per_day = cap
    if answers.get("hours_per_day"):
        per_day = max(1, min(cap, int(float(answers["hours_per_day"]) * 12)))
    funnel_to_call = RATES["reply"] * RATES["conversation"] * RATES["link"] * RATES["booked"]
    if template == "fill_calendar":
        calls = int(answers.get("calls_target") or 20)
        openers = math.ceil(calls / funnel_to_call)
        wins = None
        goal_txt = f"{calls} booked calls"
    else:
        gc = outreach_loop.goal_calculator(price, price, close_rate=RATES["close"], show_rate=RATES["show"],
                                           book_rate=RATES["booked"], reply_rate=RATES["reply"],
                                           convo_rate=RATES["conversation"], link_rate=RATES["link"], per_sender_cap=cap)
        openers = int(gc["dms_per_month"])
        wins = 1
        goal_txt = "one client"
    replies = round(openers * RATES["reply"])
    convos = round(replies * RATES["conversation"])
    links = round(convos * RATES["link"])
    booked = max(1, round(links * RATES["booked"]))
    showed = max(1, round(booked * RATES["show"]))
    working_days = math.ceil(openers / max(1, per_day))
    t = {"openers": openers, "replies": replies, "conversations": convos, "links": links, "calls_booked": booked,
         "calls_showed": showed, "proposals_sent": showed if wins else None, "wins": wins, "openers_per_day": per_day,
         "daily_cap": cap, "working_days": working_days, "calendar_days": math.ceil(working_days * 7 / 5),
         "rates": RATES, "price": price,
         "note": "A model at the programme's benchmark rates, not a promise. Your own numbers replace it as they come in."}
    t["summary"] = (f"At the programme's benchmark rates, {goal_txt} takes about {openers:,} openers, which lead to about "
                    f"{replies:,} replies and {booked:,} booked calls. At {per_day} openers a day that's about "
                    f"{working_days:,} working days. This is a model, not a promise.")
    return t
