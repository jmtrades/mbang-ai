"""Programme rubrics. Every rule is tied to an SOP passage (file + search phrase) so scores
come with [E#] citations. Deterministic checks give a 0-10 score per criterion; the AI path
(when budget allows) can refine them, but the rules path always works."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from . import guardrails

CTA_WORDS = re.compile(
    r"\b(click( here)?|book( now| a call| your)?|buy( now)?|download|sign up|register|dm me|reply|call us|"
    r"apply( now)?|join( now)?|grab|get (it|yours|started)|tap|subscribe|watch now|schedule)\b", re.I)
GENERIC_OPENERS = re.compile(
    r"^\s*(in today'?s|hi,? i'?m|hey,? i'?m|are you|have you ever|i'?m going to show you|welcome|hello|"
    r"did you know|imagine|let me tell you|so,? )", re.I)
VALUE_MARKERS = re.compile(
    r"\b(tip|video|question to think|breakdown|here'?s|quick idea|resource|checklist|guide|example|template|"
    r"one thing|takeaway|lesson|insight|case)\b", re.I)
GENERIC_WHO = re.compile(r"\b(businesses|business owners|entrepreneurs|anyone|everyone|people|companies)\b", re.I)
SEGMENT = re.compile(r"segment\s*(\d)\s*[:\-]?\s*([^\n(]*)\(?\s*(\d{1,3})\s*min", re.I)
SOP_SEGMENT_MIN = {1: 5, 2: 7, 3: 10, 4: 15, 5: 10, 6: 12, 7: 6}


def sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text or "") if s.strip()]


def first_sentence(text: str) -> str:
    s = sentences(text)
    return s[0] if s else ""


@dataclass
class Rule:
    key: str
    label: str
    weight: float
    check: Callable[[str, dict], tuple[float, str, str]]  # -> (score 0-10, weak quote, fix)
    sop: str              # SOP file the rule comes from
    cite: str             # phrase used to retrieve the SOP passage
    flaw: str             # flaw code reported when score < 6


# --------------------------------------------------------------------------- checks
def c_hook(text, ctx):
    fs = first_sentence(text)
    n = len(fs.split())
    generic = bool(GENERIC_OPENERS.search(fs)) or bool(guardrails.ai_words(fs))
    if generic:
        return 3, fs, "Open with the specific outcome or the mistake your audience is making, in under 12 words."
    if n <= 12:
        return 10, "", ""
    if n <= 20:
        return 6, fs, "Cut the first line to 12 words or fewer; lead with the outcome."
    return 2, fs, "Your hook is too long. Say exactly what they'll walk away with in one short line."


def c_cta(text, ctx):
    ctas = {m.group(0).lower() for m in CTA_WORDS.finditer(text)}
    n = len(ctas)
    if n == 1:
        return 10, "", ""
    if n == 0:
        return 4, "", "Add one clear call to action."
    return max(0, 10 - 5 * (n - 1)), ", ".join(sorted(ctas)), "Keep exactly one call to action; two calls to action halve the clicks."


def c_ai_words(text, ctx):
    found = guardrails.ai_words(text)
    if not found:
        return 10, "", ""
    return max(0, 10 - 3 * len(found)), ", ".join(found), "Replace AI-sounding words with plain, specific language."


def c_guardrails(text, ctx):
    # with the tenant, its plug-in rules apply too (proof records: the outreach range is substantiated or not)
    flags = guardrails.check(text, ctx.get("mode", "plain"), ctx.get("meta"), ctx.get("banned", []), tenant=ctx.get("tenant"))
    ctx["flags"] = flags
    if guardrails.blocked(flags):
        f = next(f for f in flags if f["severity"] == "block")
        return 0, f["match"], f["message"]
    if guardrails.needs_review(flags):
        f = flags[0]
        return 6, f["match"], f["message"]
    return 10, "", ""


def c_one_question(text, ctx):
    n = text.count("?")
    if n <= 1:
        return 10, "", ""
    qs = [s for s in sentences(text) if s.endswith("?")]
    return 2, " ".join(qs[:3]), "Ask one question per message. If you ask three questions, they answer none."


def c_dm_length(text, ctx):
    n = len(text.split())
    if n <= 60:
        return 10, "", ""
    return 4, f"{n} words", "Keep DMs under 60 words; the goal is to start a conversation, not to sell."


def c_value(text, ctx):
    low = text.lower()
    if re.search(r"don'?t forget|just (bumping|checking in|following up)", low) and not VALUE_MARKERS.search(text):
        return 1, first_sentence(text), "Every reminder or follow-up must give value: a short video, a tip or a question to think about."
    if VALUE_MARKERS.search(text):
        return 10, "", ""
    return 4, first_sentence(text), "Add something useful: a tip, a short video or a question to think about."


def c_specific_who(text, ctx):
    m = GENERIC_WHO.search(text)
    if not m:
        return 9, "", ""
    return 3, m.group(0), "Name a specific niche (e.g. 'independent physio clinics'), not 'businesses'."


def c_usp_parts(text, ctx):
    low = text.lower()
    parts = sum(1 for p in ("i help", " get ", " in ", " through ", " without ") if p in f" {low} ")
    if parts >= 5:
        return 10, "", ""
    return 2 * parts, text[:120], "Use the formula: I help [who] get [outcome] in [timeframe] through [mechanism] without [pain]."


def c_segments(text, ctx):
    found = {int(n): int(m) for n, _, m in SEGMENT.findall(text)}
    if not found:
        return 5, "", "Lay the webinar out in the seven segments with timings."
    bad = []
    for n, mins in found.items():
        target = SOP_SEGMENT_MIN.get(n)
        if target and not (target * 0.6 <= mins <= target * 1.4):
            bad.append(f"Segment {n}: {mins} min (SOP {target} min)")
    missing = 7 - len(found)
    score = 10 - 3 * len(bad) - min(4, missing)
    if not bad and missing <= 0:
        return 10, "", ""
    return max(0, score), "; ".join(bad) or f"{missing} segments missing", "Match the seven-segment timings: 5/7/10/15/10/12/6 minutes."


def c_readability(text, ctx):
    ss = sentences(text)
    if not ss:
        return 5, "", ""
    avg = sum(len(s.split()) for s in ss) / len(ss)
    if avg <= 18:
        return 10, "", ""
    long = max(ss, key=lambda s: len(s.split()))
    return 5, long[:140], "Shorten sentences to under 18 words on average."


def c_subject(text, ctx):
    m = re.search(r"^\s*subject\s*:\s*(.+)$", text, re.I | re.M)
    if not m:
        return 8, "", ""
    subj = m.group(1)
    if re.match(r"\s*(re|fwd?)\s*:", subj, re.I) or "following up" in subj.lower():
        return 0, subj, "Write an honest subject line; never 'Re:' or 'following up' on a first email."
    if len(subj.split()) > 9:
        return 6, subj, "Keep subject lines under 9 words."
    return 10, "", ""


def c_objections(text, ctx):
    low = text.lower()
    hits = sum(1 for k in ("think about it", "too expensive", "partner", "spouse", "afford") if k in low)
    handled = sum(1 for k in ("what specifically", "the gap", "cost of staying", "both of them", "follow-up call") if k in low)
    if hits == 0:
        return 8, "", ""
    if handled >= hits:
        return 10, "", ""
    return 4, "objection raised but not handled", "When an objection comes up, go back to the gap and ask what specifically they want to think through."


def c_discount(text, ctx):
    m = re.search(r"\b(discount|knock (some|it) off|special price|lower the price|i can do it for)\b", text, re.I)
    if m:
        return 1, m.group(0), "Never discount on the call to close; it teaches the lead your price isn't real."
    return 10, "", ""


def c_stages(text, ctx):
    low = text.lower()
    st = sum(1 for k in ("goal", "current", "tried", "why now", "gap", "offer", "next step") if k in low)
    if st >= 5:
        return 10, "", ""
    return 3 + st, "", "Cover discovery (goal, current numbers, what they tried, why now) and play back the gap before the offer."


R = Rule
COMMON = [
    R("guardrails", "Guardrails (no fake proof, honest claims)", 2.0, c_guardrails, "sops/mastering_webinars.txt",
      "only use real verified results with permission", "guardrail"),
    R("ai_words", "Plain language, no AI-sounding words", 1.0, c_ai_words, "sops/usp_creation.txt",
      "read your USP out loud stranger repeat", "ai_words"),
]

RUBRICS: dict[str, list[Rule]] = {
    "ad": [R("hook", "Hook is short and specific", 2.0, c_hook, "sops/thunderdome.txt", "test the hook visual offer line", "weak_hook"),
           R("cta", "Exactly one call to action", 1.5, c_cta, "course/sop_email_matrix.txt", "one call to action two calls halve the clicks", "multiple_ctas"),
           R("who", "Specific audience", 1.0, c_specific_who, "sops/usp_creation.txt", "who must be a specific niche", "vague_who"),
           *COMMON],
    "vsl": [R("hook", "Hook states the promise", 2.0, c_hook, "sops/mastering_webinars.txt", "hook and promise say exactly what they will walk away with", "weak_hook"),
            R("cta", "One call to action", 1.0, c_cta, "course/sop_email_matrix.txt", "one call to action", "multiple_ctas"),
            R("read", "Readable sentences", 1.0, c_readability, "sops/usp_creation.txt", "read out loud stranger", "readability"),
            *COMMON],
    "webinar": [R("segments", "Seven segments with SOP timings", 2.5, c_segments, "sops/mastering_webinars.txt", "seven segment webinar timings", "segment_timing"),
                R("hook", "Hook and promise", 1.0, c_hook, "sops/mastering_webinars.txt", "hook and promise five minutes", "weak_hook"),
                *COMMON],
    "funnel": [R("hook", "Headline is the outcome", 2.0, c_hook, "sops/usp_creation.txt", "outcome must be measurable", "weak_hook"),
               R("cta", "One call to action", 1.5, c_cta, "course/sop_email_matrix.txt", "one call to action", "multiple_ctas"),
               R("who", "Specific audience", 1.0, c_specific_who, "sops/usp_creation.txt", "who must be a specific niche", "vague_who"),
               *COMMON],
    "loom": [R("hook", "Opens with their problem", 2.0, c_hook, "sops/mastering_webinars.txt", "name the mistake the audience is making", "weak_hook"),
             R("cta", "One next step", 1.0, c_cta, "course/sop_email_matrix.txt", "one call to action", "multiple_ctas"),
             *COMMON],
    "dm_script": [R("one_q", "One question per message", 2.0, c_one_question, "course/transcripts/vid_d4e5f6.txt", "one question per message", "multiple_questions"),
                  R("length", "Short", 1.0, c_dm_length, "course/transcripts/vid_d4e5f6.txt", "goal of a DM is to start a real conversation", "too_long"),
                  *COMMON],
    "followup": [R("value", "Gives value", 2.0, c_value, "course/transcripts/vid_d4e5f6.txt", "every follow-up must add value", "no_value_followup"),
                 R("one_q", "One question", 1.0, c_one_question, "course/transcripts/vid_d4e5f6.txt", "one question per message", "multiple_questions"),
                 *COMMON],
    "reminder": [R("value", "Reminder gives value", 2.5, c_value, "sops/show_rate_sales_guide.txt", "each reminder gives value never just say don't forget", "no_value_reminder"),
                 *COMMON],
    "sales_call": [R("stages", "Discovery and gap before offer", 2.0, c_stages, "course/transcripts/vid_p7q8r9.txt", "call has five stages rapport discovery gap offer next step", "skipped_discovery"),
                   R("objections", "Objections handled", 1.5, c_objections, "course/transcripts/vid_p7q8r9.txt", "need to think about it ask what specifically", "objection_unhandled"),
                   R("discount", "No discounting", 1.5, c_discount, "course/transcripts/vid_p7q8r9.txt", "never discount on the call to close", "discounting"),
                   *COMMON],
    "profile_bio": [R("who", "Says who you help", 2.0, c_specific_who, "sops/usp_creation.txt", "who must be a specific niche", "vague_who"),
                    R("usp", "Follows the USP formula", 1.0, c_usp_parts, "sops/usp_creation.txt", "USP formula I help who get outcome", "no_usp"),
                    *COMMON],
    "content_post": [R("hook", "Strong first line", 2.0, c_hook, "sops/thunderdome.txt", "test the hook", "weak_hook"),
                     R("cta", "One call to action", 1.0, c_cta, "course/sop_email_matrix.txt", "one call to action", "multiple_ctas"),
                     *COMMON],
    "email": [R("subject", "Honest subject line", 2.0, c_subject, "course/sop_email_matrix.txt", "subject lines must be honest never Re", "deceptive_subject"),
              R("cta", "One call to action", 1.5, c_cta, "course/sop_email_matrix.txt", "every email has one call to action", "multiple_ctas"),
              *COMMON],
    "usp": [R("usp", "USP formula", 2.5, c_usp_parts, "sops/usp_creation.txt", "USP formula I help who get outcome in timeframe", "no_usp"),
            R("who", "Specific who", 1.0, c_specific_who, "sops/usp_creation.txt", "who must be a specific niche", "vague_who"),
            *COMMON],
    "script": [R("hook", "Strong first line", 2.0, c_hook, "sops/thunderdome.txt", "test the hook", "weak_hook"),
               R("read", "Readable", 1.0, c_readability, "sops/usp_creation.txt", "read out loud", "readability"),
               *COMMON],
}
RUBRICS["caption"] = RUBRICS["content_post"]
RUBRICS["cold_video_pitch"] = RUBRICS["loom"]
RUBRICS["dm"] = RUBRICS["dm_script"]
RUBRICS["generic"] = [*COMMON, R("read", "Readable", 1.0, c_readability, "sops/usp_creation.txt", "read out loud", "readability")]

GUARD_TO_FLAW = {
    "guarantee": "guarantee", "income_claim": "income_claim", "fake_testimonial": "fake_testimonial",
    "unverified_result": "unverified_result", "fake_shoutout": "fake_shoutout", "planted_question": "planted_questions",
    "invented_credential": "invented_credential", "fake_scarcity": "fake_scarcity", "deceptive_subject": "deceptive_subject",
    "outreach_money": "price_in_dm", "outreach_price": "price_in_dm", "cold_lead_figure": "price_in_dm",
    "outreach_definite": "price_in_dm", "outreach_unhedged": "price_in_dm", "banned_name": "banned_name",
    "deadline_no_reason": "deadline_no_reason", "never_invite": "never_invite",
}

PROSPECT_TYPES = {"dm_script", "dm", "followup", "cold_video_pitch"}
CLIENT_ASSET_TYPES = {"vsl", "webinar", "funnel", "email"}


def meta_for(asset_type: str, meta: dict | None = None) -> dict:
    m = dict(meta or {})
    if asset_type in PROSPECT_TYPES:
        m.setdefault("prospect_facing", True)
    if asset_type in CLIENT_ASSET_TYPES:
        m.setdefault("client_asset", True)
    if asset_type == "email":
        m.setdefault("prior_contact", False)
    return m


def get(asset_type: str) -> list[Rule]:
    return RUBRICS.get(asset_type, RUBRICS["generic"])


def score(text: str, asset_type: str, meta: dict | None = None, banned: list[str] | None = None, tenant=None) -> dict:
    """Deterministic rubric score. Returns weighted score /100, per-criterion results,
    detected flaw codes and the guardrail flags. Pass the tenant so its rule plug-ins (proof records, persona,
    prospect research) apply exactly as they do in the approval queue."""
    meta = meta_for(asset_type, meta)
    ctx = {"mode": guardrails.mode_for(meta), "meta": meta, "banned": banned or [], "tenant": tenant}
    crit = []
    detected: set[str] = set()
    total_w = 0.0
    acc = 0.0
    for rule in get(asset_type):
        s, quote, fix = rule.check(text, ctx)
        crit.append({"key": rule.key, "label": rule.label, "score": round(s, 1), "weight": rule.weight, "quote": quote,
                     "fix": fix, "sop": rule.sop, "cite": rule.cite})
        acc += s * rule.weight
        total_w += rule.weight
        if s < 6 and rule.flaw != "guardrail":
            detected.add(rule.flaw)
    flags = ctx.get("flags", [])
    for f in flags:
        if f["code"] in GUARD_TO_FLAW:
            detected.add(GUARD_TO_FLAW[f["code"]])
    critical = [c["key"] for c in crit if c["weight"] >= 2.0 and c["score"] < 5]
    return {"score": round(acc / total_w * 10) if total_w else 0, "criteria": crit, "detected": sorted(detected),
            "flags": flags, "mode": ctx["mode"], "critical": critical}
