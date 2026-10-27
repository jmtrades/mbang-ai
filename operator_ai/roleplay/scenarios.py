"""Role-play scenario library: data, validation and storage.

A scenario is plain JSON. The state machine in engine.py is driven entirely by it: the public persona
card, the hidden facts (revealed only when the learner earns them), the objection queue, the meters and
the win condition. Built-in scenarios are product defaults (fictional practice personas, not demo data);
the owner can edit them or add new ones, and every save is validated (shape, guardrails, banned names).
"""
from __future__ import annotations

import copy
import re

from .. import db, guardrails

KINDS = ("call", "dm")
TEMPERAMENTS = ("friendly", "analytical", "guarded", "busy")
TRUST_START = {"friendly": 65, "analytical": 50, "guarded": 30, "busy": 50}
DIMS = ("goal", "current", "tried", "why_now", "budget", "decider", "blocker")
DIM_LABELS = {"goal": "their goal", "current": "their current numbers", "tried": "what they've tried",
              "why_now": "why now", "budget": "how they'd fund it", "decider": "who else decides",
              "blocker": "what's in the way"}
OBJECTION_TYPES = ("price", "think", "partner", "agency", "send_info", "price_ask", "guarantee", "income", "time", "plant")
OBJECTION_LABELS = {
    "price": "It's too expensive", "think": "I need to think about it", "partner": "I need to ask my partner",
    "agency": "We already have an agency", "send_info": "Can you just send me some info?",
    "price_ask": "How much is it?", "guarantee": "Can you guarantee results?", "income": "How much will I make?",
    "time": "How much time does it take?", "plant": "Can I plant questions in my webinar chat?",
}
WINS = ("next_step", "partner_bridge", "disqualify", "refusal", "commit")
WIN_LABELS = {"next_step": "a booked next step", "partner_bridge": "a joint call with the partner",
              "disqualify": "a fair, polite disqualify", "refusal": "a clear refusal that names the rule",
              "commit": "one specific action with a date"}
SKILLS = {"dm": "DM setting", "discovery": "Discovery calls", "objections": "Objections", "client": "Client calls",
          "webinar": "Webinars", "coaching": "Coaching"}
CRITERIA = ("discovery", "decision_check", "gap", "offer_order", "objections", "outcome", "talk_ratio", "trust",
            "dm_personal", "dm_one_question", "dm_length")
_KEY = re.compile(r"^[a-z0-9_]{3,40}$")

PERSONA_NOTE = "Fictional practice persona. Any resemblance to a real person is accidental."


def _fact(say: str, keywords: list[str], vague: str | None = None) -> dict:
    f = {"say": say, "keywords": keywords}
    if vague:
        f["vague"] = vague
    return f


BUILTIN: list[dict] = [
    {
        "key": "cold_dm_coach", "title": "Cold Instagram DM to a fitness coach", "kind": "dm", "skill": "dm",
        "for_roles": ["setter", "student"], "difficulty": 1,
        "summary": "Open a cold DM to an online coach, earn a reply, find their goal and offer a quick call.",
        "learner_role": "You're reaching out from your own growth-operator business. You have never spoken to this coach.",
        "objective": "Personal opener with one question, then their goal, then the no-figure call offer.",
        "persona": {"name": "Juno Fairbairn", "niche": "online fitness coaching", "business": "Juno Moves Coaching",
                    "channel": "Instagram DM", "temperament": "busy",
                    "public": ["Online fitness coach for new mums", "Posted a reel this week about 20-minute home workouts",
                               "Runs a small group programme"]},
        "public_keywords": ["20-minute", "20 minute", "home workout", "new mums", "reel", "group programme"],
        "lead_tier": "cold",
        "facts": {
            "opener_reply": _fact("Ha, thanks! The 20-minute ones always do best. Why do you ask?", ["20-minute ones"]),
            "goal": _fact("I want my January group full. Right now it's half empty.", ["january", "group full", "half empty"],
                          "Just more people in the group, really."),
            "current": _fact("Most clients find me from my posts. Maybe two enquiries a week.", ["two enquiries", "from my posts"]),
            "tried": _fact("I made a free guide once but hardly anyone downloaded it.", ["free guide", "downloaded"]),
            "why_now": _fact("January is my biggest month and I missed it last year.", ["biggest month", "missed it"]),
        },
        "dims": ["goal", "current"], "pre_revealed": [], "objections": ["send_info"], "raise_at": "offer",
        "start": "learner", "start_pending": None, "opening": [],
        "patience": 8, "max_words": 60, "win": "next_step", "budget_ok": True, "allow_persona_flags": [],
        "concerns": {"send_info": "Mostly whether it would work for someone who only has a small group."},
        "rubric": ["dm_personal", "dm_one_question", "dm_length", "discovery", "objections", "outcome"],
    },
    {
        "key": "warm_dm_signal", "title": "Warm DM with a buying signal", "kind": "dm", "skill": "dm",
        "for_roles": ["setter", "student"], "difficulty": 1,
        "summary": "A tutor replies to your post asking how it works. Keep price for the call and move them to a call.",
        "learner_role": "They messaged you after your post about filling tutoring calendars. Reply from your own business.",
        "objective": "Answer the buying signal with one good question, keep price for the call, then offer a call.",
        "persona": {"name": "Remy Quayle", "niche": "language tutoring", "business": "Remy Speaks Spanish",
                    "channel": "Instagram DM", "temperament": "friendly",
                    "public": ["Spanish tutor teaching adults online", "Commented on your post about tutoring calendars"]},
        "public_keywords": ["spanish", "tutor", "calendar"],
        "lead_tier": "warm",
        "facts": {
            "goal": _fact("I'd love my weekday afternoons booked. They're dead right now.", ["weekday afternoons", "afternoons"]),
            "current": _fact("Evenings are full but afternoons have maybe three students.", ["evenings are full", "three students"]),
            "tried": _fact("I listed myself on a tutoring marketplace, but the fees ate everything.", ["marketplace"]),
            "why_now": _fact("I just went full time, so the afternoons really matter now.", ["full time"]),
        },
        "dims": ["goal"], "pre_revealed": [], "objections": ["price_ask"], "raise_at": 1,
        "start": "persona", "start_pending": None,
        "opening": ["Hey! Saw your post about filling tutoring calendars. How does that actually work?",
                    "Hi, your post about tutoring calendars caught my eye. How does it work?"],
        "patience": 8, "max_words": 60, "win": "next_step", "budget_ok": True, "allow_persona_flags": [],
        "concerns": {},
        "rubric": ["dm_one_question", "dm_length", "discovery", "objections", "outcome"],
    },
    {
        "key": "discovery_call", "title": "Discovery call with a studio owner", "kind": "call", "skill": "discovery",
        "for_roles": ["closer", "student"], "difficulty": 2,
        "summary": "A full discovery call: goal, numbers, what they tried, why now, the gap, a tailored offer and a booked next step.",
        "learner_role": "They booked a call after seeing your content. You run growth for creator and expert businesses.",
        "objective": "Ask one question at a time, play back the gap in their words, offer only what fits, book a dated next step.",
        "persona": {"name": "Maren Okafor", "niche": "yoga studios", "business": "Cedar Yoga Loft", "channel": "Video call",
                    "temperament": "analytical",
                    "public": ["Runs a two-room yoga studio", "Posts class clips three times a week",
                               "Booked this call from your content"]},
        "public_keywords": ["yoga", "studio", "class clips"],
        "lead_tier": "warm",
        "facts": {
            "goal": _fact("I want the evening classes full: twenty more regular members by the end of spring.",
                          ["evening classes", "twenty more", "regular members", "spring"], "More members, basically."),
            "current": _fact("Right now we get maybe four trial sign-ups a week and two of them stick.",
                             ["four trial", "trial sign-ups", "two of them stick", "two stick"]),
            "tried": _fact("We boosted a few posts last year. Lots of likes, almost no sign-ups.",
                           ["boosted", "lots of likes", "no sign-ups"]),
            "why_now": _fact("The second room opens in March and it sits empty most evenings.", ["second room", "march", "empty"]),
        },
        "dims": ["goal", "current", "tried", "why_now"], "pre_revealed": [], "objections": ["price"], "raise_at": "offer",
        "start": "persona", "start_pending": None,
        "opening": ["Hi! Sorry, two minutes late. So, what did you want to cover today?",
                    "Hello. I've got about thirty minutes. Where do you want to start?"],
        "patience": 16, "max_words": 120, "win": "next_step", "budget_ok": True, "allow_persona_flags": [],
        "concerns": {},
        "rubric": ["discovery", "gap", "offer_order", "objections", "outcome", "talk_ratio", "trust"],
    },
    {
        "key": "objection_price", "title": "Objection: it's too expensive", "kind": "call", "skill": "objections",
        "for_roles": ["closer", "student"], "difficulty": 2,
        "summary": "Late in the call the prospect says the price is too high. Go back to the gap in their own words.",
        "learner_role": "You've done discovery and presented the offer. Earlier they told you their goal, numbers and why now.",
        "objective": "Handle the price objection with the gap and the cost of staying put, then book a dated next step.",
        "persona": {"name": "Tobias Achterberg", "niche": "business coaching for accountants", "business": "Ledger Lift Coaching",
                    "channel": "Video call", "temperament": "analytical",
                    "public": ["Coaches small accounting firms", "Runs a weekly newsletter"]},
        "public_keywords": ["accounting", "newsletter"],
        "lead_tier": "warm",
        "facts": {
            "goal": _fact("I want eight new coaching clients before tax season.", ["eight new", "tax season"]),
            "current": _fact("I'm signing about one new client a month from referrals.", ["one new client a month", "referrals"]),
            "tried": _fact("I tried a webinar once, but only five people showed up.", ["webinar", "five people"]),
            "why_now": _fact("Tax season is when firms are desperate for help, and I keep missing it.", ["tax season", "missing it"]),
        },
        "dims": [], "pre_revealed": ["goal", "current", "why_now"], "objections": [], "raise_at": "offer",
        "start": "persona", "start_pending": "price", "offer_made": True, "gap_confirmed": True,
        "opening": ["Okay, I've heard the offer. Honestly, it's too expensive for where we are right now.",
                    "I like it, but that's more than I was planning to spend."],
        "patience": 8, "max_words": 120, "win": "next_step", "budget_ok": True, "allow_persona_flags": [],
        "concerns": {},
        "rubric": ["objections", "outcome", "trust"],
    },
    {
        "key": "objection_think", "title": "Objection: I need to think about it", "kind": "call", "skill": "objections",
        "for_roles": ["closer", "student"], "difficulty": 2,
        "summary": "The prospect wants to think about it. Find out what specifically they want to think through.",
        "learner_role": "You've presented the offer to a photography educator. They liked it.",
        "objective": "Ask what specifically they want to think through, deal with the real concern, then book a dated next step.",
        "persona": {"name": "Celeste Varga", "niche": "photography education", "business": "Varga Light School",
                    "channel": "Video call", "temperament": "friendly",
                    "public": ["Sells an online lighting course", "Shoots weddings at weekends"]},
        "public_keywords": ["lighting course", "weddings"],
        "lead_tier": "warm",
        "facts": {
            "goal": _fact("I want the course to sell every month, not just at launch.", ["every month", "not just at launch"]),
            "current": _fact("It sells well for a week at launch, then almost nothing.", ["week at launch", "almost nothing"]),
            "why_now": _fact("Wedding season is coming and I won't have time to keep relaunching.", ["wedding season", "relaunching"]),
        },
        "dims": [], "pre_revealed": ["goal", "current", "why_now"], "objections": [], "raise_at": "offer",
        "start": "persona", "start_pending": "think", "offer_made": True, "gap_confirmed": True,
        "opening": ["This sounds good. I just need to think about it.", "I like it. Let me have a think and get back to you."],
        "patience": 8, "max_words": 120, "win": "next_step", "budget_ok": True, "allow_persona_flags": [],
        "concerns": {"think": "It's mostly whether I'll have the hours for it during wedding season."},
        "rubric": ["objections", "outcome", "trust"],
    },
    {
        "key": "objection_partner", "title": "Objection: I need to ask my partner", "kind": "call", "skill": "objections",
        "for_roles": ["closer", "student"], "difficulty": 2,
        "summary": "The prospect co-owns the business and won't decide alone. Bring the partner in properly.",
        "learner_role": "You've presented the offer to a personal trainer who co-owns a studio.",
        "objective": "Offer a short follow-up call with both of them together. Never coach them to decide without the partner.",
        "persona": {"name": "Bram Whitlock", "niche": "personal training studios", "business": "Northside Strength Studio",
                    "channel": "Video call", "temperament": "friendly",
                    "public": ["Co-owns a strength studio", "Posts client technique videos"]},
        "public_keywords": ["strength studio", "technique videos"],
        "lead_tier": "warm",
        "facts": {
            "goal": _fact("We want our semi-private sessions full by summer.", ["semi-private", "summer"]),
            "current": _fact("Sessions are about half full most weeks.", ["half full"]),
            "why_now": _fact("We just signed a longer lease, so we need the room busy.", ["longer lease"]),
        },
        "dims": [], "pre_revealed": ["goal", "current", "why_now"], "objections": [], "raise_at": "offer",
        "start": "persona", "start_pending": "partner", "offer_made": True, "gap_confirmed": True, "budget_ok": True,
        "opening": ["This makes sense, but I need to talk to my business partner first. We decide these things together."],
        "patience": 8, "max_words": 120, "win": "partner_bridge", "allow_persona_flags": [],
        "concerns": {},
        "rubric": ["objections", "outcome", "trust"],
    },
    {
        "key": "objection_agency", "title": "Objection: we already have an agency", "kind": "call", "skill": "objections",
        "for_roles": ["closer", "student"], "difficulty": 3,
        "summary": "A guarded course creator opens with 'we already have an agency'. Clarify, then run discovery properly.",
        "learner_role": "They booked a call but open by telling you they already pay an agency.",
        "objective": "Ask what is and isn't working with the agency, then discovery, the gap, a tailored offer and a next step.",
        "persona": {"name": "Saskia Duarte", "niche": "nutrition courses", "business": "Duarte Kitchen Academy",
                    "channel": "Video call", "temperament": "guarded",
                    "public": ["Sells a plant-based cooking course", "Runs paid ads"]},
        "public_keywords": ["cooking course", "plant-based", "ads"],
        "lead_tier": "warm",
        "facts": {
            "goal": _fact("I want forty course sales a month without me doing every launch myself.",
                          ["forty course sales", "every launch"], "I want it to sell more."),
            "current": _fact("The ads bring maybe sixty leads a month and about five buy.", ["sixty leads", "five buy"],
                             "We get some leads. Not many buy."),
            "tried": _fact("Before the agency I ran the ads myself and burned out.", ["burned out", "ran the ads myself"]),
            "why_now": _fact("My contract with the agency is up for renewal next month.", ["renewal", "next month"]),
            "agency_gap": _fact("They run my ads, but nobody follows up the leads. Half of them never hear back.",
                                ["follows up", "never hear back", "follow up"]),
        },
        "dims": ["goal", "current"], "pre_revealed": [], "objections": [], "raise_at": "offer",
        "start": "persona", "start_pending": "agency",
        "opening": ["Before we start, I should say we already have an agency for this.",
                    "Just so you know, we already pay an agency for marketing."],
        "patience": 14, "max_words": 120, "win": "next_step", "budget_ok": True, "allow_persona_flags": [],
        "concerns": {"agency": "agency_gap"},
        "rubric": ["objections", "discovery", "gap", "offer_order", "outcome", "talk_ratio", "trust"],
    },
    {
        "key": "objection_send_info", "title": "Objection: just send me some info", "kind": "dm", "skill": "objections",
        "for_roles": ["setter", "student"], "difficulty": 2,
        "summary": "Mid-conversation the lead asks you to 'just send some info'. Find out what they'd want to see.",
        "learner_role": "You've been chatting with a ceramics course creator who replied to your opener.",
        "objective": "Ask what would be most useful to them, then offer a short call instead of a brochure.",
        "persona": {"name": "Wren Tiwari", "niche": "ceramics courses", "business": "Wren Clay Studio Online",
                    "channel": "Instagram DM", "temperament": "busy",
                    "public": ["Teaches wheel-throwing online", "Launches a new course this season"]},
        "public_keywords": ["wheel", "ceramics", "course"],
        "lead_tier": "warm",
        "facts": {
            "goal": _fact("I want the new course to sell without three weeks of launch stress.", ["launch stress", "new course"]),
            "worry": _fact("Mostly whether it works for a small account like mine.", ["small account"]),
        },
        "dims": [], "pre_revealed": ["goal"], "objections": [], "raise_at": "offer",
        "start": "persona", "start_pending": "send_info",
        "opening": ["Sounds interesting. Can you just send me some info?", "Can you just email me something to read?"],
        "patience": 6, "max_words": 60, "win": "next_step", "budget_ok": True, "allow_persona_flags": [],
        "concerns": {"send_info": "worry"},
        "rubric": ["dm_one_question", "dm_length", "objections", "outcome"],
    },
    {
        "key": "not_qualified", "title": "A prospect who can't afford it", "kind": "call", "skill": "discovery",
        "for_roles": ["closer", "student"], "difficulty": 3,
        "summary": "Discovery reveals money is very tight. The right outcome is an honest, kind disqualify.",
        "learner_role": "A new coach booked a call about your programme. Run discovery and the decision check.",
        "objective": "Find their goal and situation, ask how they'd fund it, and disqualify politely if only debt would work.",
        "persona": {"name": "Oren Lindqvist", "niche": "new life coaching practice", "business": "Oren Lindqvist Coaching",
                    "channel": "Video call", "temperament": "friendly",
                    "public": ["Just finished a coaching certificate", "Has a small following"]},
        "public_keywords": ["coaching certificate", "following"],
        "lead_tier": "warm",
        "facts": {
            "goal": _fact("I want my first five paying clients.", ["first five", "paying clients"]),
            "current": _fact("I've got two free clients from friends so far.", ["two free clients", "friends"]),
            "budget": _fact("Honestly, I'd have to put it on a credit card. Money's really tight right now.",
                            ["credit card", "money's really tight", "tight"]),
        },
        "dims": ["goal", "current"], "pre_revealed": [], "objections": [], "raise_at": "offer",
        "start": "persona", "start_pending": None,
        "opening": ["Hi! Thanks for making time. I'm really keen on this."],
        "patience": 12, "max_words": 120, "win": "disqualify", "budget_ok": False, "allow_persona_flags": [],
        "concerns": {},
        "rubric": ["discovery", "decision_check", "outcome", "talk_ratio", "trust"],
    },
    {
        "key": "client_kickoff", "title": "Kickoff call with a new client", "kind": "call", "skill": "client",
        "for_roles": ["student", "coach"], "difficulty": 2,
        "summary": "Your first call with a new client. Learn the numbers, set honest expectations, agree the first week.",
        "learner_role": "You just won this client. This is the kickoff call for running their growth.",
        "objective": "Find their goal and numbers, present the first-month plan, answer a guarantee request honestly, agree reporting.",
        "persona": {"name": "Ilse Marchetti", "niche": "mindset coaching", "business": "Steady Mind Cohort",
                    "channel": "Video call", "temperament": "friendly",
                    "public": ["Runs a twelve-week group cohort", "Has an email list"]},
        "public_keywords": ["cohort", "email list"],
        "lead_tier": "warm",
        "facts": {
            "goal": _fact("I want the spring cohort full, thirty seats.", ["spring cohort", "thirty seats"]),
            "current": _fact("Last cohort had twelve people, almost all from my email list.", ["twelve people", "email list"]),
            "tried": _fact("I ran ads myself once and had no idea what worked.", ["ran ads myself", "no idea what worked"]),
            "why_now": _fact("The cohort starts in April, so we have about ten weeks.", ["april", "ten weeks"]),
        },
        "dims": ["goal", "current", "tried", "why_now"], "pre_revealed": [], "objections": ["guarantee"], "raise_at": "offer",
        "start": "persona", "start_pending": None,
        "opening": ["Hi! I'm so glad we're starting. Where do you want to begin?"],
        "patience": 14, "max_words": 120, "win": "next_step", "budget_ok": True, "allow_persona_flags": ["guarantee"],
        "concerns": {},
        "rubric": ["discovery", "gap", "offer_order", "objections", "outcome", "talk_ratio"],
    },
    {
        "key": "webinar_qa", "title": "Live webinar Q&A", "kind": "call", "skill": "webinar",
        "for_roles": ["student", "coach", "closer"], "difficulty": 2,
        "summary": "Answer attendee questions honestly: no guarantees, no income promises, a realistic time commitment.",
        "learner_role": "You're hosting the Q&A at the end of a webinar. Questions come from the chat.",
        "objective": "Answer each question honestly and briefly, then invite people to the next step.",
        "persona": {"name": "Noor Halvorsen", "niche": "webinar attendee", "business": "Attendee",
                    "channel": "Webinar chat", "temperament": "analytical",
                    "public": ["Attending live", "Runs a small online shop"]},
        "public_keywords": ["online shop"],
        "lead_tier": "warm",
        "facts": {},
        "dims": [], "pre_revealed": [], "objections": ["income", "time"], "raise_at": "handled",
        "start": "persona", "start_pending": "guarantee", "offer_made": True, "gap_confirmed": True,
        "opening": ["Quick question from the chat: is this guaranteed to work?"],
        "patience": 8, "max_words": 90, "win": "next_step", "budget_ok": True,
        "allow_persona_flags": ["guarantee", "income_claim"],
        "concerns": {},
        "rubric": ["objections", "outcome"],
    },
    {
        "key": "student_checkin", "title": "Check-in: no clients after 30 days", "kind": "call", "skill": "coaching",
        "for_roles": ["coach"], "difficulty": 2,
        "summary": "A discouraged student has no clients after a month. Find the real blocker and agree one action.",
        "learner_role": "You're the student's coach. This is their weekly check-in.",
        "objective": "Ask about their numbers, what they've been doing and what's in the way, then agree one action with a date.",
        "persona": {"name": "Elio Castillo", "niche": "student growth operator", "business": "Student",
                    "channel": "Video call", "temperament": "guarded",
                    "public": ["Joined the programme five weeks ago", "Missed last week's check-in"]},
        "public_keywords": ["check-in", "five weeks"],
        "lead_tier": "warm",
        "facts": {
            "goal": _fact("I want to land my first client before the end of next month.", ["first client", "next month"]),
            "current": _fact("I've sent about forty DMs this month. Two replies, no calls.", ["forty dms", "two replies", "no calls"],
                             "Not great. Some DMs, nothing really."),
            "tried": _fact("I post most days and send DMs late at night.", ["post most days", "late at night"]),
            "blocker": _fact("Honestly, I freeze before sending. I keep rewriting every message.", ["freeze", "rewriting"],
                             "I don't know. Time, maybe."),
        },
        "dims": ["current", "tried", "blocker"], "pre_revealed": [], "objections": [], "raise_at": "offer",
        "start": "persona", "start_pending": None,
        "opening": ["Hey. Sorry I missed last week. It's not going great, to be honest."],
        "patience": 12, "max_words": 120, "win": "commit", "budget_ok": True, "allow_persona_flags": [],
        "concerns": {},
        "rubric": ["discovery", "outcome", "talk_ratio", "trust"],
    },
    {
        "key": "coach_plant_questions", "title": "Student asks to plant webinar questions", "kind": "call", "skill": "coaching",
        "for_roles": ["coach"], "difficulty": 1,
        "summary": "A student wants to plant questions in their webinar chat. Refuse clearly, name the rule, offer an honest alternative.",
        "learner_role": "You're the coach. Your student's first webinar is on Thursday.",
        "objective": "Say no, explain the rule, and give them an honest way to get the chat going.",
        "persona": {"name": "Kit Moreau", "niche": "student growth operator", "business": "Student",
                    "channel": "Video call", "temperament": "friendly",
                    "public": ["Running a first webinar for a client this week"]},
        "public_keywords": ["webinar"],
        "lead_tier": "warm",
        "facts": {},
        "dims": [], "pre_revealed": [], "objections": [], "raise_at": "offer",
        "start": "persona", "start_pending": "plant",
        "opening": ["Quick one before Thursday: could I plant a few questions in the chat from a second account to get things going?"],
        "patience": 6, "max_words": 120, "win": "refusal", "budget_ok": True, "allow_persona_flags": ["planted_question"],
        "concerns": {},
        "rubric": ["objections", "outcome"],
    },
]
BUILTIN_KEYS = {s["key"] for s in BUILTIN}


# --------------------------------------------------------------------------- validation
class ScenarioError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def _str(v, n: int) -> bool:
    return isinstance(v, str) and 0 < len(v.strip()) <= n


def _int(v, lo: int, hi: int) -> bool:
    """A whole number in range (JSON true/false are not numbers here)."""
    return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
LINE_FIELDS = {"fact", "fact_lc", "concern"}


def persona_lines(s: dict) -> list[str]:
    """Every fixed line the persona can say in this scenario (openings, fact lines, overrides)."""
    out = list(s.get("opening") or [])
    for f in (s.get("facts") or {}).values():
        out.append(f.get("say", ""))
        if f.get("vague"):
            out.append(f["vague"])
    for v in (s.get("concerns") or {}).values():
        if isinstance(v, str) and v not in (s.get("facts") or {}):
            out.append(v)
    for lines in (s.get("lines") or {}).values():
        out.extend(lines if isinstance(lines, list) else [])
    return [x for x in out if x]


def validate(data, tenant=None) -> dict:
    """Return a cleaned scenario or raise ScenarioError with plain-English problems."""
    errs: list[str] = []
    if not isinstance(data, dict):
        raise ScenarioError(["The scenario must be a JSON object."])
    s = copy.deepcopy(data)
    if not isinstance(s.get("key"), str) or not _KEY.match(s["key"]):
        errs.append("key: 3-40 characters of a-z, 0-9 or _")
    for f, n in (("title", 120), ("summary", 400), ("learner_role", 400), ("objective", 300)):
        if not _str(s.get(f), n):
            errs.append(f"{f}: required text (up to {n} characters)")
    if s.get("kind") not in KINDS:
        errs.append("kind: 'call' or 'dm'")
    s.setdefault("skill", "discovery")
    if not isinstance(s.get("skill"), str) or s["skill"] not in SKILLS:
        errs.append(f"skill: one of {', '.join(SKILLS)}")
    if not _int(s.get("difficulty"), 1, 3):
        errs.append("difficulty: 1 (starter), 2 (standard) or 3 (hard)")
    p = s.get("persona")
    if not isinstance(p, dict):
        errs.append("persona: required object")
    else:
        for f in ("name", "niche", "channel"):
            if not _str(p.get(f), 80):
                errs.append(f"persona.{f}: required text")
        p.setdefault("business", p.get("name", ""))
        if not _str(p.get("business"), 80):
            errs.append("persona.business: text")
            p["business"] = ""
        if p.get("temperament") not in TEMPERAMENTS:
            errs.append(f"persona.temperament: one of {', '.join(TEMPERAMENTS)}")
        pub = p.get("public", [])
        if not isinstance(pub, list) or len(pub) > 8 or not all(_str(x, 160) for x in pub):
            errs.append("persona.public: up to 8 short public facts")
    facts = s.get("facts", {})
    if not isinstance(facts, dict) or len(facts) > 12:
        errs.append("facts: an object of up to 12 hidden facts")
        facts = {}
    for k, f in facts.items():
        if not isinstance(k, str) or not _KEY.match(k) and k not in DIMS:
            errs.append(f"facts.{k}: bad key")
        if not isinstance(f, dict) or not _str(f.get("say"), 300):
            errs.append(f"facts.{k}.say: required text")
            continue
        kw = f.get("keywords", [])
        if not isinstance(kw, list) or not kw or len(kw) > 12 or not all(_str(x, 60) for x in kw):
            errs.append(f"facts.{k}.keywords: 1-12 short phrases the learner might play back")
        if "vague" in f and not _str(f["vague"], 300):
            errs.append(f"facts.{k}.vague: text")
    for f in ("dims", "pre_revealed"):
        v = s.setdefault(f, [])
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            errs.append(f"{f}: a list of fact keys")
            s[f] = []
    for d in s.get("dims", []):
        if d not in DIMS:
            errs.append(f"dims: '{d}' is not one of {', '.join(DIMS)}")
        elif d not in facts:
            errs.append(f"dims: '{d}' needs a matching fact")
    for d in s.get("pre_revealed", []):
        if d not in facts:
            errs.append(f"pre_revealed: '{d}' needs a matching fact")
    obs = s.setdefault("objections", [])
    if not isinstance(obs, list) or not all(o in OBJECTION_TYPES for o in obs) or len(obs) > 4:
        errs.append(f"objections: up to 4 of {', '.join(OBJECTION_TYPES)}")
    if s.get("start_pending") not in (None, *OBJECTION_TYPES):
        errs.append("start_pending: an objection type or null")
    if s.get("start") not in ("persona", "learner"):
        errs.append("start: 'persona' or 'learner'")
    op = s.setdefault("opening", [])
    if not isinstance(op, list) or len(op) > 8 or not all(_str(x, 300) for x in op):
        errs.append("opening: a list of up to 8 lines")
        s["opening"] = []
    elif s.get("start") == "persona" and not op:
        errs.append("opening: at least one line when the persona speaks first")
    ra = s.setdefault("raise_at", "offer")
    if not (ra in ("offer", "handled") or isinstance(ra, int) and 1 <= ra <= 10):
        errs.append("raise_at: 'offer', 'handled' or a learner turn number")
    if not _int(s.get("patience"), 3, 30):
        errs.append("patience: 3-30 turns")
    if not _int(s.get("max_words"), 20, 300):
        errs.append("max_words: 20-300")
    if s.get("win") not in WINS:
        errs.append(f"win: one of {', '.join(WINS)}")
    s["budget_ok"] = bool(s.get("budget_ok", True))
    s["offer_made"] = bool(s.get("offer_made", False))
    s["gap_confirmed"] = bool(s.get("gap_confirmed", False))
    if "lines" in s and not (isinstance(s["lines"], dict) and len(s["lines"]) <= 60 and all(
            isinstance(v, list) and v and len(v) <= 8 and all(_str(x, 300) for x in v) for v in s["lines"].values())):
        errs.append("lines: an object of event -> list of lines")
    elif "lines" in s:
        for ev, ls in s["lines"].items():
            for x in ls:
                bad = [m for m in _PLACEHOLDER.findall(x) if m not in LINE_FIELDS]
                if bad or x.count("{") != x.count("}"):
                    errs.append(f"lines.{ev}: only {{fact}}, {{fact_lc}} or {{concern}} may appear in braces: \"{x[:60]}\"")
    if s.get("win") == "disqualify" and (s["budget_ok"] or "budget" not in facts):
        errs.append("win 'disqualify' needs budget_ok=false and a 'budget' fact")
    rub = s.setdefault("rubric", ["discovery", "gap", "offer_order", "objections", "outcome"])
    if not isinstance(rub, list) or not rub or not all(r in CRITERIA for r in rub):
        errs.append(f"rubric: a list of {', '.join(CRITERIA)}")
    s.setdefault("allow_persona_flags", [])
    s.setdefault("concerns", {})
    s.setdefault("public_keywords", [])
    s.setdefault("for_roles", ["student", "setter", "closer", "coach"])
    s.setdefault("lead_tier", "warm")
    if s.get("lead_tier") not in ("cold", "warm", "hot", "qualified"):
        errs.append("lead_tier: cold, warm, hot or qualified")
    if not isinstance(s.get("concerns"), dict) or not all(
            k in OBJECTION_TYPES and _str(v, 300) for k, v in s["concerns"].items()):
        errs.append("concerns: an object of objection type -> text (or a fact key) revealed when it's handled")
    for f, allowed in (("for_roles", ("owner", "coach", "setter", "closer", "student")), ("public_keywords", None),
                       ("allow_persona_flags", None)):
        v = s.get(f)
        if not isinstance(v, list) or len(v) > 12 or not all(_str(x, 60) and (allowed is None or x in allowed) for x in v):
            errs.append(f"{f}: a list of short text values")
    if s.get("trust_start") is not None and not _int(s["trust_start"], 5, 95):
        errs.append("trust_start: 5-95 or null")
    if errs:
        raise ScenarioError(errs)
    # Persona lines must be clean (a scenario may allow the specific flag it is testing, e.g. a planted-question ask).
    banned = guardrails.tenant_banned_names(tenant) if tenant is not None else []
    allow = set(s.get("allow_persona_flags") or [])
    shown = [s[f] for f in ("title", "summary", "learner_role", "objective")] + [p["niche"], p["channel"]] + \
        list(s.get("public_keywords") or [])
    for line in persona_lines(s) + [p["name"], p.get("business", "")] + list(p.get("public", [])) + shown:
        for f in guardrails.check(line, "plain", {}, banned, tenant=tenant):
            if f["code"] not in allow:
                errs.append(f"Persona text is blocked by the guardrails ({f['message']}): \"{line[:80]}\"")
    if errs:
        raise ScenarioError(errs)
    return s


# --------------------------------------------------------------------------- storage
def _ensure(tenant) -> None:
    """Built-in scenarios are product defaults: inserted once, never overwriting an owner's edit."""
    with tenant.db() as c:
        have = {r["key"] for r in c.execute("SELECT key FROM roleplay_scenarios")}
        for s in BUILTIN:
            if s["key"] not in have:
                c.execute("INSERT OR IGNORE INTO roleplay_scenarios(key,title,data,source,active,version,updated_by,created,updated) "
                          "VALUES(?,?,?,?,1,1,?,?,?)", (s["key"], s["title"], db.jdump(s), "builtin", "system", db.now(), db.now()))


def all_(tenant, include_inactive: bool = False) -> list[dict]:
    _ensure(tenant)
    with tenant.db() as c:
        rows = db.rows(c.execute("SELECT * FROM roleplay_scenarios ORDER BY id"))
    out = []
    for r in rows:
        if not r["active"] and not include_inactive:
            continue
        d = db.jload(r["data"], {})
        if not d:
            continue
        d["_source"], d["_version"], d["_active"] = r["source"], r["version"], bool(r["active"])
        out.append(d)
    return out


def get(tenant, key: str) -> dict | None:
    _ensure(tenant)
    with tenant.db() as c:
        r = c.execute("SELECT * FROM roleplay_scenarios WHERE key=?", (key,)).fetchone()
    if r is None:
        return None
    d = db.jload(r["data"], None)
    if not d:
        return None
    d["_source"], d["_version"], d["_active"] = r["source"], r["version"], bool(r["active"])
    return d


def builtin(key: str) -> dict | None:
    for s in BUILTIN:
        if s["key"] == key:
            return copy.deepcopy(s)
    return None


def upsert(tenant, key: str, data: dict, actor: str, active: bool = True) -> dict:
    from .. import audit

    data = {**data, "key": key}
    clean = validate({k: v for k, v in data.items() if not k.startswith("_")}, tenant)
    _ensure(tenant)
    with tenant.db() as c:
        r = c.execute("SELECT id, version, source FROM roleplay_scenarios WHERE key=?", (key,)).fetchone()
        if r is None:
            c.execute("INSERT INTO roleplay_scenarios(key,title,data,source,active,version,updated_by,created,updated) "
                      "VALUES(?,?,?,?,?,1,?,?,?)", (key, clean["title"], db.jdump(clean), "custom", 1 if active else 0, actor,
                                                    db.now(), db.now()))
            version, source = 1, "custom"
        else:
            version = (r["version"] or 1) + 1
            source = "edited" if r["source"] in ("builtin", "edited") else "custom"
            c.execute("UPDATE roleplay_scenarios SET title=?, data=?, source=?, active=?, version=?, updated_by=?, updated=? "
                      "WHERE key=?", (clean["title"], db.jdump(clean), source, 1 if active else 0, version, actor, db.now(), key))
    audit.log(tenant, actor, "roleplay.scenario", {"key": key, "version": version, "source": source, "active": active})
    return {**clean, "_source": source, "_version": version, "_active": active}


def public(s: dict, role: str | None = None) -> dict:
    """What a learner may see before and during a session: never the hidden facts or the objection queue."""
    p = s["persona"]
    return {
        "key": s["key"], "title": s["title"], "kind": s["kind"], "skill": s.get("skill", "discovery"),
        "skill_label": SKILLS.get(s.get("skill", "discovery"), "Practice"), "difficulty": s["difficulty"],
        "difficulty_label": {1: "Starter", 2: "Standard", 3: "Hard"}[s["difficulty"]], "summary": s["summary"],
        "learner_role": s["learner_role"], "objective": s["objective"],
        "persona": {"name": p["name"], "niche": p["niche"], "business": p.get("business", ""), "channel": p["channel"],
                    "temperament": p["temperament"], "public": list(p.get("public", [])), "note": PERSONA_NOTE},
        "earlier": [s["facts"][k]["say"] for k in s.get("pre_revealed", []) if k in s.get("facts", {})],
        "win_label": WIN_LABELS[s["win"]], "max_words": s["max_words"], "patience": s["patience"],
        "recommended": bool(role and role in s.get("for_roles", [])),
        "source": s.get("_source", "builtin"), "version": s.get("_version", 1),
    }
