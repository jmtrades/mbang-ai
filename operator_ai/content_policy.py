"""Knowledge-base content safety. Lines that teach deceptive tactics, promote drugs or
stimulants, explain how to evade platform bans, or stereotype groups are dropped at index
time and listed as "Not used: this passage describes {tactic}"."""
from __future__ import annotations

import re

from .guardrails import DECEPTION_RULES, negated

TACTIC_NAMES = {
    "guarantee": "guaranteed results",
    "income_claim": "income claims",
    "fake_testimonial": "inventing testimonials or results",
    "unverified_result": "unverified results about a named person",
    "fake_shoutout": "fake booking shout-outs",
    "planted_question": "planting audience questions or fake chat accounts",
    "invented_credential": "inventing credentials",
    "fake_scarcity": "fake scarcity",
}

EXTRA_RULES: list[tuple[str, re.Pattern]] = [
    ("using drugs or stimulants", re.compile(
        r"\b(stimulants?|adderall|modafinil|cocaine|amphetamines?|speed pills|nootropic stack)\b|"
        r"\b(grab|take|pop) (some )?pills\b", re.I)),
    ("evading platform bans", re.compile(
        r"\b(evade|get around|bypass|dodge|beat)\b[^.!?\n]{0,30}\b(bans?|banned|restrictions?|ad review|policy review)\b|"
        r"\bcloak(ing|ed)?\b|\bnew (ad )?accounts? (after|when) (a |you get |being )?ban", re.I)),
    ("stereotyping a group", re.compile(
        r"\b(all|those|these|typical) (women|men|boomers|millennials|immigrants|foreigners|old people|young people|"
        r"indians|africans|asians|europeans|americans)\b[^.!?\n]{0,30}\b(are|can'?t|never|always)\b", re.I)),
]

# Teaching passages should not be dropped just for quoting a banned phrase as an example
# of what not to do; negation handles "never plant questions". Some rules are too broad for
# teaching material (deadline wording, income figures inside benchmark tables).
SKIP_IN_KB = {"guarantee", "income_claim", "unverified_result"}


def classify(text: str) -> str | None:
    """Return the tactic a line teaches, or None if it is fine to use."""
    for code, pat, _ in DECEPTION_RULES:
        if code in SKIP_IN_KB:
            continue
        for m in pat.finditer(text):
            if not negated(text, m.start()):
                return TACTIC_NAMES.get(code, code)
    for name, pat in EXTRA_RULES:
        for m in pat.finditer(text):
            if not negated(text, m.start()):
                return name
    return None


def not_used_label(tactic: str) -> str:
    return f"Not used: this passage describes {tactic}"
