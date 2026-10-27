"""AI mode: the rules decide, the AI only phrases the persona's line.

Each turn the state machine emits a directive (events, the facts it may use, tone from the trust band,
a word limit) and a template line with the same meaning. The model rewrites that line in character.
Every AI line is post-checked and the template line is used instead when it fails:
  * no hidden fact outside the allowed list (keyword and number match),
  * at most max_words,
  * passes the guardrails in plain mode (and the tenant's banned names),
  * no "as an AI" meta talk.
Replays are keyed by (scenario, seed, turn, hash of the learner's text), so a session replays exactly
with the AI off. Each session has a token cap drawn from the tenant's ai_token_budget.
"""
from __future__ import annotations

import hashlib
import re

from .. import guardrails, llm
from . import acts as A
from .replies import band

SESSION_TOKEN_CAP = 2500
SYSTEM = ("You voice one line for a FICTIONAL practice persona in a sales-training simulator. "
          "Write only the persona's next spoken line, plain and natural, in character. "
          "Keep the meaning of the reference line. Use only the facts listed as allowed; never add numbers, "
          "names, results or details that are not there. Never mention being an AI, a model, a simulation "
          "or a role-play. No stage directions, no quotes around the line.")
META_TALK = re.compile(r"\b(as an ai|an ai\b|language model|i'?m an ai|i am an ai|ai assistant|chatgpt|openai|anthropic|"
                       r"claude|simulation|simulator|role-?play|as a persona|fictional)\b", re.I)
NUM_WORDS = ("two three four five six seven eight nine ten eleven twelve fifteen twenty thirty forty fifty sixty "
             "hundred thousand").split()  # "one" is too common in ordinary speech to treat as a leaked number
TONE = {"low": "short, guarded, a little sceptical", "mid": "neutral and businesslike", "high": "warm and open"}
EVENT_HELP = {
    "reveal": "answer their question with the allowed fact", "vague": "give a vague, partial answer",
    "already": "remind them you already said this", "objection": "raise this objection",
    "reraise": "repeat the objection, unconvinced", "handled": "accept their answer to your objection",
    "gap_agree": "agree that their summary is right", "gap_thin": "say their summary is only partly right",
    "gap_unsupported": "say you never said that", "offer_early": "push back: they pitched too early",
    "next_step_accept": "agree to the next step", "neutral": "acknowledge briefly",
}


def session_cap(tenant) -> int:
    if llm.unavailable_reason(tenant):
        return 0
    return max(0, min(SESSION_TOKEN_CAP, llm.budget(tenant) - llm.used(tenant)))


def purpose_key(scenario_key: str, seed: int, turn: int, learner_text: str) -> str:
    h = hashlib.sha256((learner_text or "").encode()).hexdigest()[:16]
    return f"roleplay:{scenario_key}:{seed}:{turn}:{h}"


def _numbers(text: str) -> set[str]:
    low = (text or "").lower()
    out = set(re.findall(r"\d+", low))
    out |= {w for w in NUM_WORDS if re.search(rf"\b{w}\b", low)}
    return out


def leaks(line: str, scenario: dict, allowed: list[str]) -> list[str]:
    """Hidden facts (not in `allowed`) whose keywords or numbers appear in the line."""
    facts = scenario.get("facts", {})
    allowed_text = " ".join(facts[k]["say"] for k in allowed if k in facts)
    allowed_nums = _numbers(allowed_text)
    line_nums = _numbers(line)
    out = []
    for k, f in facts.items():
        if k in allowed:
            continue
        if A.fact_cited(line, f):
            out.append(k)
            continue
        if (_numbers(f.get("say", "")) - allowed_nums) & line_nums:
            out.append(k)
    return out


def post_check(tenant, line: str, scenario: dict, allowed: list[str], max_words: int,
               must_reveal: list[str] | None = None) -> list[str]:
    problems = []
    if not line.strip():
        problems.append("empty line")
    facts = scenario.get("facts", {})
    if any(k in facts and not A.fact_cited(line, facts[k]) for k in must_reveal or []):
        problems.append("left out the fact the rules revealed this turn")
    if META_TALK.search(line):
        problems.append("talked about being an AI or a simulation")
    if len(A.words(line)) > max_words:
        problems.append(f"longer than {max_words} words")
    lk = leaks(line, scenario, allowed)
    if lk:
        problems.append("revealed a hidden fact")
    allow = set(scenario.get("allow_persona_flags") or [])
    fl = guardrails.check_for(tenant, line, {})
    if any(f["severity"] == "block" and f["code"] not in allow for f in fl):
        problems.append("blocked by the guardrails")
    return problems


def directive(scenario: dict, segs: list[tuple], trust: int, allowed: list[str], template: str,
              reveal: list[str] | None = None) -> dict:
    return {"events": [[e, s] for e, s, *_ in segs], "allowed_facts": list(allowed), "reveal": list(reveal or []),
            "tone": TONE[band(trust)], "max_words": max(25, min(70, len(A.words(template)) * 2 + 8)), "template": template}


def _prompt(scenario: dict, d: dict, learner_text: str) -> str:
    p = scenario["persona"]
    facts = scenario.get("facts", {})
    allowed = "\n".join(f"- {facts[k]['say']}" for k in d["allowed_facts"] if k in facts) or "- (none)"
    must = "; ".join(facts[k]["say"] for k in d.get("reveal", []) if k in facts)
    if must:
        allowed += f"\nYOU MUST SAY THIS NOW (in your own words, keep the key details): {must}"
    what = "; ".join(EVENT_HELP.get(e, e.replace("_", " ")) + (f" ({s})" if s else "") for e, s in d["events"])
    return (f"PERSONA: {p['name']}, {p['niche']} ({p.get('business', '')}). Temperament: {p['temperament']}. "
            f"Channel: {p['channel']}.\nTONE NOW: {d['tone']}.\nWHAT TO DO: {what}.\n"
            f"ALLOWED FACTS:\n{allowed}\nTHEY JUST SAID: {learner_text[:600]}\n"
            f"REFERENCE LINE (keep this meaning): {d['template']}\n"
            f"Write the persona's line in at most {d['max_words']} words.")


def phrase(tenant, scenario: dict, seed: int, turn: int, learner_text: str, d: dict, state: dict) -> dict:
    """-> {text, source: template|live|replay, note, tokens}. Never raises: any problem falls back to the template."""
    template = d["template"]
    reason = llm.unavailable_reason(tenant)
    if reason is None and state.get("ai_tokens", 0) >= state.get("ai_cap", 0):
        return {"text": template, "source": "template", "note": "AI token cap for this session reached", "tokens": 0}
    prompt = _prompt(scenario, d, learner_text)
    purpose = purpose_key(scenario["key"], seed, turn, learner_text)
    try:
        out = llm.complete_or_replay(tenant, SYSTEM, prompt, purpose, max_tokens=120)
    except llm.AIUnavailable as e:
        return {"text": template, "source": "template", "note": e.reason, "tokens": 0}
    except Exception:  # noqa: BLE001 - any model problem: the template line is always valid
        return {"text": template, "source": "template", "note": "AI call failed", "tokens": 0}
    text = " ".join((out.get("text") or "").strip().strip('"').split())
    problems = post_check(tenant, text, scenario, d["allowed_facts"], d["max_words"], d.get("reveal"))
    tokens = int(out.get("tokens", 0) or 0)
    if problems:
        return {"text": template, "source": "template", "note": "AI line not used: " + "; ".join(problems), "tokens": tokens}
    src = "replay" if out.get("mode") == "replay" else "live"
    return {"text": text, "source": src, "note": out.get("label", "AI") if src == "replay" else "AI phrased", "tokens": tokens}
