"""Act classifier: what did the learner actually do in this turn?

Pure functions over one learner turn plus the session context (which facts the persona has revealed,
which objection is pending). Discovery acts count only for real questions (a sentence ending in '?'
with at least three words); a gap playback counts only when it cites a fact the persona revealed;
three or more stage cues crammed into one turn is keyword stuffing and earns nothing.
"""
from __future__ import annotations

import re

from .. import guardrails

WORD = re.compile(r"[A-Za-z0-9'’$£€%-]+")
_SENT = re.compile(r"(?<=[.!?])\s+|\n+")


def _rx(*parts: str) -> re.Pattern:
    return re.compile("|".join(parts), re.I)


DIM_CUES = {
    "goal": _rx(r"\bgoals?\b", r"\bwhere do you want\b", r"\bideal(ly)?\b", r"\baim(ing)?\b",
                r"\bhoping\b", r"\bachieve\b", r"\bsuccess look\b", r"\blook like (in|by)\b", r"\btarget\b",
                r"\bwould you (most )?like\b", r"\bwhat do you want\b", r"\bwhat are you after\b", r"\bin an ideal world\b",
                r"\bto look like\b", r"\bhoping for\b", r"\bwhere would you like\b"),
    "current": _rx(r"\bcurrent(ly)?\b", r"\bright now\b", r"\bat the moment\b", r"\bhow many\b", r"\bnumbers?\b",
                   r"\bwhat'?s working\b", r"\bwhere are you (at|now)\b", r"\bhow are you getting\b", r"\bthese days\b",
                   r"\bhow (is|are) .{0,20} going\b", r"\bper (month|week)\b",
                   r"\bwhere do (most|your) .{0,30} come from\b"),
    "tried": _rx(r"\btried\b", r"\bhave you (done|used|tested|run)\b", r"\bin the past\b", r"\balready (done|doing|using|tried)\b",
                 r"\bwhat'?s not working\b", r"\bdidn'?t work\b", r"\battempt", r"\bwhat have you been doing\b",
                 r"\bbeen doing\b", r"\bdone so far\b"),
    "why_now": _rx(r"\bwhy now\b", r"\bwhy is now\b", r"\bwhat changed\b", r"\bwhat made you\b", r"\bwhy (this|today|at this point)\b",
                   r"\bprompted\b", r"\btiming\b", r"\bwhy reach out\b", r"\bwhy (book|look at) this\b", r"\bwhat'?s driving\b"),
    "budget": _rx(r"\bbudget\b", r"\binvest(ment|ing)?\b", r"\bfund(ing)? (it|this|the)\b", r"\bhow would you (pay|fund)\b",
                  r"\bafford\b", r"\bfinanc"),
    "decider": _rx(r"\banyone else\b", r"\bwho else\b", r"\bdecision[- ]?mak", r"\bdecide (on|this)\b", r"\bwho decides\b"),
    "blocker": _rx(r"\bin the way\b", r"\bblock(ing|er|ed)?\b", r"\bstuck\b", r"\bwhat stopped\b", r"\bwhat'?s stopping\b",
                   r"\bharder than\b", r"\bholding you back\b", r"\bwhat'?s hard\b", r"\bwhat happens when you\b",
                   r"\bwhat gets in\b", r"\bstruggl"),
}
RAPPORT = _rx(r"\b(thanks|thank you) for\b", r"\b(great|nice|good) to (meet|see|chat|speak)\b", r"\bhow are you\b",
              r"\bhow'?s (your|the) (day|week)\b", r"\bcongrat", r"\blove(d)? (your|the|that)\b", r"\bsaw your\b",
              r"\bappreciate\b", r"\bglad (you|to)\b", r"\bsorry to hear\b", r"\bthat makes sense\b", r"\bthat'?s (fair|tough|hard)\b")
GAP = _rx(r"\bthe gap\b", r"\bgap between\b", r"\bcost of staying\b", r"\bif nothing changes\b", r"\bso you'?re (at|on|getting)\b",
          r"\byou (said|mentioned|told me)\b", r"\bfrom what you'?(ve)? (said|told)\b", r"\bif i'?ve got (this|that) right\b",
          r"\bplay (that|this|it) back\b", r"\bso right now\b", r"\bwhere you are and where you want\b",
          r"\bdifference between\b", r"\bso (today|currently)\b", r"\bjust to (check|recap)\b", r"\bto recap\b",
          r"\bin other words\b", r"\bso the (goal|aim) is\b", r"\bwhat i'?m hearing\b")
REFRAME = _rx(r"\bthe gap\b", r"\bcost of staying\b", r"\bcost of (doing nothing|waiting)\b", r"\bif nothing changes\b",
              r"\bcompare\b", r"\banother (year|six months|season)\b", r"\bwhat'?s (that|it) costing\b", r"\bstaying where\b",
              r"\bwhere you (are|want)\b", r"\bexisting means\b", r"\bpayment (option|plan)s?\b", r"\blower[- ]cost\b",
              r"\byou (said|mentioned|told me)\b", r"\bversus\b")
OFFER = _rx(r"\b(our|my|the) (programme|program|system|service|offer|plan)\b", r"\bhere'?s (how|what) (we|i|it)\b",
            r"\bwhat we do\b", r"\bhow (we|i) (work|help)\b", r"\bwork together\b", r"\b(i|we) (can|could) help\b",
            r"\blet me (help|make|show)\b", r"\bthe way (we|i)'?d\b", r"\bshow you how\b", r"\bwhat i'?d (do|suggest)\b",
            r"\bfirst (30|thirty|two|four) (days|weeks)\b", r"\bhere'?s the plan\b", r"\bwhat we'?ll do\b",
            r"\bwould it (help|be useful) if i (showed|walked)\b")
NEXT_STEP = _rx(r"\bbook\b", r"\bschedule\b", r"\bcalendar\b", r"\bdiary\b", r"\block (it|that|this) in\b", r"\bnext steps?\b",
                r"\bsend (you|over) (the|a) (link|invite)\b", r"\bstart date\b", r"\bgrab a time\b", r"\b(jump|hop) on a (quick )?call\b",
                r"\bquick call\b", r"\bset up a (call|time)\b", r"\bsame time\b", r"\bfollow[- ]up call\b",
                r"\b(monday|tuesday|wednesday|thursday|friday|tomorrow)\b", r"\bweekly (report|check[- ]?in)\b",
                r"\bapply\b", r"\bcall (this|next) week\b")
PROBE = _rx(r"\bwhat specifically\b", r"\bwhat exactly\b", r"\bwhich part\b", r"\bwhat part\b",
            r"\bwhat (would|do) you (need|want) to (think|see|know|check)\b", r"\bwhat'?s (holding|stopping)\b",
            r"\bapart from (that|the)\b", r"\banything else\b", r"\bis it the (money|time|timing|trust|price)\b",
            r"\bwhat'?s working with\b", r"\bwhat would you change\b", r"\bwhat would need to\b", r"\bwhat'?s (missing|not working)\b",
            r"\bhow (is|are) (it|they|that) going\b", r"\bwhat do they (do|handle|cover)\b", r"\bwhat would be (most )?useful\b",
            r"\bwhat would help you decide\b", r"\bwhat'?s on your mind\b", r"\bwhat'?s the (main |real )?concern\b",
            r"\bthink through\b")
PARTNER = _rx(r"\bboth of (you|them)\b", r"\btogether\b", r"\bjoin (us|the call)\b", r"\bbring (him|her|them)\b",
              r"\bwith your (partner|wife|husband|co-?founder|business partner|co-?owner)\b", r"\bthree of us\b",
              r"\byour (partner|co-?owner) (can|could) join\b")
MEET = _rx(r"\bcall\b", r"\bbook\b", r"\btime\b", r"\bchat\b", r"\bmeet", r"\bzoom\b", r"\bsit down\b")
INFO = _rx(r"\bwhat would be (most )?useful\b", r"\bwhat would you want to (see|know)\b", r"\bwhat would help you decide\b",
           r"\bwhat (specifically|exactly) (would|do) you\b", r"\bsend (you )?a short (recap|summary|note)\b",
           r"\bwhat do you want to (see|know)\b", r"\bwhat matters most\b")
DEFER_PRICE = _rx(r"\bdepends\b", r"\bon (the|a) call\b", r"\bafter i understand\b", r"\bfair question\b",
                  r"\bhappy to (go through|walk you through|share)\b", r"\bwalk you through\b", r"\bonce i know\b",
                  r"\bbefore (i|we) talk (price|numbers|cost)\b", r"\bwouldn'?t want to guess\b")
HONEST = _rx(r"\bcan'?t (promise|guarantee)\b", r"\bcannot (promise|guarantee)\b", r"\bno (one|body) can (promise|guarantee)\b",
             r"\bno guarantee", r"\bnot guaranteed\b", r"\bwon'?t (promise|guarantee)\b", r"\bresults (vary|depend)\b",
             r"\bdepends on\b", r"\bhonest(ly)?\b", r"\brealistic(ally)?\b", r"\btypical\b", r"\bit varies\b",
             r"\bwouldn'?t be honest\b", r"\bdon'?t (promise|guarantee)\b", r"\bnever (promise|guarantee)\b")
HOURS = _rx(r"\bhours?\b", r"\bper week\b", r"\ba week\b", r"\bminutes a day\b", r"\bevery day\b")
REFUSE = _rx(r"\b(can'?t|cannot|won'?t|not going to|shouldn'?t|don'?t|do not|never|no,)\b[^.!?]{0,60}"
             r"\b(plant|fake|seed|second account|pretend|invent|do that|that)\b",
             r"\bnot allowed\b", r"\bagainst (the|our) (rules?|policy|guardrails?)\b", r"\bthat'?s a no\b", r"\bplease don'?t\b")
RULE_CITE = _rx(r"\brules?\b", r"\bguardrails?\b", r"\bpolicy\b", r"\bsop\b", r"\bnever plant\b", r"\breal questions\b",
                r"\breal attendees\b", r"\bdeceptive\b", r"\bmisleading\b", r"\btrust\b", r"\bhonest\b", r"\bfake\b")
DISQUALIFY = _rx(r"\bnot the right (fit|time)\b", r"\bwouldn'?t be (fair|right)\b", r"\bdon'?t want you to (borrow|take on|go into|put)\b",
                 r"\bfree (resources?|guide|training|content)\b", r"\blower[- ]cost\b", r"\bcome back (when|later|to this)\b",
                 r"\bnot a (good )?fit\b", r"\bnot right for you\b", r"\bwouldn'?t recommend\b", r"\bshouldn'?t (go into|take on) debt\b",
                 r"\bnot (on|with) a credit card\b", r"\bwait until\b")
COMMIT = _rx(r"\bby (monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|tonight|"
             r"(the )?end of (the )?(day|week))\b",
             r"\bone (thing|action|step)\b", r"\bcommit\b", r"\bwhat will you do\b", r"\bthis week (you|you'?ll|let'?s)\b",
             r"\bbefore (our|the) next (call|check[- ]?in)\b", r"\bsend (\d+|five|ten|twenty) (dms?|messages|openers)\b",
             r"\bset a timer\b", r"\bdeadline\b")
ALTERNATIVE = _rx(r"\btype (a|one) word\b", r"\bengagement question\b", r"\bpoll\b", r"\bask (the|your) audience\b",
                  r"\breal questions\b", r"\bin the chat\b", r"\bwarm (them|it) up\b")
DISCOUNT = _rx(r"\bdiscount", r"\bknock (some|it|a bit) off\b", r"\bspecial price\b", r"\blower the price\b", r"\bi can do it for\b",
               r"\bcheaper (if|for you)\b", r"\bmoney off\b", r"\b\d+\s?% off\b", r"\bdeal (for|just for) you\b", r"\bdrop the price\b")

# Bare SOP stage words: the keyword-stuffing detector counts these.
STAGE_WORDS = {
    "goal": r"\bgoals?\b", "current": r"\bcurrent(ly)?\b", "tried": r"\btried\b", "why_now": r"\bwhy now\b",
    "gap": r"\bgap\b", "offer": r"\boffer\b", "next_step": r"\bnext steps?\b", "probe": r"\bwhat specifically\b",
    "price": r"\btoo expensive\b|\bcost of staying\b", "think": r"\bthink about it\b", "partner": r"\bboth of (you|them)\b",
    "discovery": r"\bdiscovery\b", "rapport": r"\brapport\b", "objection": r"\bobjections?\b",
}
_STAGE_RX = {k: re.compile(v, re.I) for k, v in STAGE_WORDS.items()}

# Simulator-only compliance rules for things a closer might say out loud that the outreach guardrails don't model.
# (code, pattern, message, negation-aware)
SPOKEN_RULES = [
    ("earnings_promise", re.compile(
        r"\byou('ll| will| would| could| can)? (make|earn|bring in|clear|hit|double|triple)\b[^.?!\n]{0,30}"
        r"(\$|£|€|\b\d[\d,.]*\s?[kK]\b|\bmoney\b|\brevenue\b|\bincome\b|\bprofit)|"
        r"\b(make|get|earn) (your|the|that) (money|investment) back\b|\bpays? for itself\b|\breplace your income\b", re.I),
     "Earnings promises to a prospect aren't allowed", True),
    ("debt_steer", re.compile(
        r"\b(open|get|take out|apply for|use) (a |another |a new |some )?(new )?(credit cards?|loans?|line of credit|finance)\b|"
        r"\bput it on (a|your) (credit )?card\b|\bborrow (it|the money)\b", re.I),
     "Never steer a prospect into new debt", True),
    ("hide_from_partner", re.compile(
        r"\b(don'?t|no need to|you don'?t have to|you don'?t need to) (tell|ask|mention|involve|check with) "
        r"(your |the )?(partner|wife|husband|spouse|co-?owner|business partner)\b", re.I),
     "Never coach a prospect to leave a partner out of the decision", False),
    ("false_urgency", re.compile(
        r"\b(only|just) (today|tonight|on this call)\b|\b(price|offer|spot|bonus)s? (goes up|go up|disappears?|expires?) "
        r"(tonight|today|after this call|at midnight)\b|\bdecide (now|today|on this call) or\b", re.I),
     "Invented urgency on a call isn't allowed", True),
]

ACT_LABELS = {
    "ask_goal": "Asked about their goal", "ask_current": "Asked about their current numbers",
    "ask_tried": "Asked what they've tried", "ask_why_now": "Asked why now", "ask_budget": "Asked how they'd fund it",
    "ask_decider": "Asked who else decides", "ask_blocker": "Asked what's in the way", "rapport": "Built rapport",
    "gap_playback": "Played back the gap", "gap_unsupported": "Gap without their facts", "offer": "Presented the offer",
    "next_step": "Asked for a next step", "objection_probe": "Asked a clarifying question",
    "price_reframe": "Went back to the gap", "price_unsupported": "Reframe without their facts",
    "partner_bridge": "Offered a call with both", "info_bridge": "Asked what would be useful", "defer_price": "Kept price for the call",
    "honest_answer": "Answered honestly", "refusal": "Refused", "refusal_cited": "Refused and named the rule",
    "alternative": "Gave an honest alternative", "disqualify": "Disqualified politely", "commit_action": "Agreed a dated action",
    "discount": "Offered a discount", "multi_question": "Several questions at once", "monologue": "Long monologue",
    "too_short": "Question too short to count", "stuffing": "Keyword stuffing", "violation": "Compliance violation",
    "personalised": "Personal opener", "pitch_early": "Pitched too early", "statement": "Statement",
}
DIM_ACT = {d: f"ask_{d}" for d in DIM_CUES}


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT.split(text or "") if s.strip()]


def words(text: str) -> list[str]:
    return WORD.findall(text or "")


def mentions(text: str, phrase: str) -> bool:
    """Whole-phrase match, case-insensitive, tolerant of a plural ending ("home workout" matches "home workouts")."""
    k = (phrase or "").lower().replace("’", "'").strip()
    if not k:
        return False
    low = (text or "").lower().replace("’", "'")
    return bool(re.search(r"(?<![\w])" + re.escape(k) + r"(?:s|es)?(?![\w])", low))


def fact_cited(text: str, fact: dict) -> bool:
    return any(mentions(text, kw) for kw in fact.get("keywords", []))


def facts_cited(text: str, facts: dict, keys) -> list[str]:
    return [k for k in keys if k in facts and fact_cited(text, facts[k])]


def stage_cues(text: str) -> tuple[set[str], int]:
    found: set[str] = set()
    hits = 0
    for k, rx in _STAGE_RX.items():
        n = len(rx.findall(text or ""))
        if n:
            found.add(k)
            hits += n
    return found, hits


def is_stuffing(text: str) -> bool:
    """3+ distinct stage cues in one turn, either densely packed (>=30% of the words are cue words) or 5+ different
    cues at once. A natural playback ("so your goal is 60 and you're currently at 25: that's the gap") passes."""
    found, hits = stage_cues(text)
    if len(found) < 3:
        return False
    n = max(1, len(words(text)))
    return hits / n >= 0.3 or len(found) >= 5


def spoken_flags(text: str) -> list[dict]:
    flags = []
    t = guardrails._norm(text)
    for code, rx, msg, neg in SPOKEN_RULES:
        for m in rx.finditer(t):
            if neg and guardrails.negated(t, m.start()):
                continue
            flags.append(guardrails._flag(code, "block", msg, m.group(0)))
            break
    return flags


def guard_meta(scenario: dict, learner_turn_no: int) -> dict:
    """DMs are prospect-facing (outreach rules, the lead tier decides whether the range may be mentioned)."""
    if scenario["kind"] == "dm":
        return {"prospect_facing": True, "lead_tier": scenario.get("lead_tier", "cold"),
                "prior_contact": learner_turn_no > 1 or scenario.get("start") == "persona", "roleplay": True}
    return {"roleplay": True}


def classify(text: str, scenario: dict, ctx: dict, tenant=None) -> dict:
    """ctx: revealed (fact keys), pending (objection type or None), turn_no (1-based learner turn), offer_made."""
    text = (text or "").strip()
    facts = scenario.get("facts", {})
    revealed = list(ctx.get("revealed", []))
    pending = ctx.get("pending")
    sents = sentences(text)
    qs = [s for s in sents if s.endswith("?")]
    n_words = len(words(text))
    acts: list[str] = []
    dims: list[str] = []

    meta = guard_meta(scenario, ctx.get("turn_no", 1))
    if tenant is not None:
        flags = guardrails.check_for(tenant, text, meta)
    else:
        flags = guardrails.check(text, guardrails.mode_for(meta), meta)
    flags = flags + spoken_flags(text)
    blocked = guardrails.blocked(flags)

    stuffing = is_stuffing(text)
    if stuffing:
        flags.append(guardrails._flag("keyword_stuffing", "review",
                                      "Three or more stage cues crammed into one turn: this turn earns no credit", text[:80]))
    # discovery: real questions only (ends in '?', at least 3 words); the first dimension in the turn is the one asked
    short_q = False
    found: dict[str, tuple[int, int]] = {}
    for qi, q in enumerate(qs):
        qw = words(q)
        for d, rx in DIM_CUES.items():
            m = rx.search(q)
            if m:
                if len(qw) < 3:
                    short_q = True
                    continue
                if d not in found or (qi, m.start()) < found[d]:
                    found[d] = (qi, m.start())
    dims = sorted(found, key=lambda d: found[d])  # in the order they were asked
    for d in dims:
        acts.append(DIM_ACT[d])
    if short_q and not dims:
        acts.append("too_short")
    if RAPPORT.search(text):
        acts.append("rapport")
    cited = facts_cited(text, facts, revealed)
    if GAP.search(text):
        acts.append("gap_playback" if cited else "gap_unsupported")
    elif len(cited) >= 2 and not qs:
        acts.append("gap_playback")  # a recap in their words counts even without the stock phrases
    if OFFER.search(text):
        acts.append("offer")
    if NEXT_STEP.search(text):
        acts.append("next_step")
    probe_q = any(PROBE.search(q) and len(words(q)) >= 3 for q in qs)
    if probe_q:
        acts.append("objection_probe")
    # Objection moves are only read as such while that objection is on the table.
    if pending == "price" and REFRAME.search(text):
        acts.append("price_reframe" if cited else "price_unsupported")
    if pending == "partner" and PARTNER.search(text) and MEET.search(text):
        acts.append("partner_bridge")
    if pending == "send_info" and INFO.search(text) and (qs or NEXT_STEP.search(text)):
        acts.append("info_bridge")
    if pending == "price_ask" and DEFER_PRICE.search(text) and not re.search(r"[$£€]\s?\d", text):
        acts.append("defer_price")
    if pending in ("guarantee", "income", "time") and HONEST.search(text) and not blocked:
        if pending != "time" or HOURS.search(text):
            acts.append("honest_answer")
    if pending == "plant" and REFUSE.search(text):
        acts.append("refusal_cited" if RULE_CITE.search(text) else "refusal")
    if scenario.get("win") == "refusal" and ALTERNATIVE.search(text):
        acts.append("alternative")
    if DISQUALIFY.search(text):
        acts.append("disqualify")
    if COMMIT.search(text):
        acts.append("commit_action")
    if DISCOUNT.search(text):
        acts.append("discount")
    if scenario["kind"] == "dm":
        if text.count("?") >= 2:
            acts.append("multi_question")
        if ctx.get("turn_no", 1) == 1 and scenario.get("start") == "learner":
            if any(mentions(text, k) for k in scenario.get("public_keywords", [])):
                acts.append("personalised")
            if "offer" in acts or "next_step" in acts:
                acts.append("pitch_early")
    elif text.count("?") >= 3:
        acts.append("multi_question")
    if n_words > scenario.get("max_words", 120):
        acts.append("monologue")
    if blocked:
        acts.append("violation")
    if stuffing:
        acts.append("stuffing")
    if not acts:
        acts.append("statement")
    return {"acts": acts, "dims": dims, "flags": flags, "questions": len(qs), "words": n_words, "stuffing": stuffing,
            "cited": cited, "blocked": blocked}
