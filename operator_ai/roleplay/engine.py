"""Deterministic rules-mode state machine for role-plays, driven entirely by scenario data.

Meters: trust (start set by temperament: guarded 30, analytical/busy 50, friendly 65) and patience (turns).
Each learner turn is classified into acts (acts.py); the transition updates the meters and the stage,
decides which hidden facts to reveal, and picks the persona's reply from the template bank with a seeded
RNG, so the same seed and the same learner lines replay the same session exactly. Presenting the offer
before the gap costs 15 trust and triggers an objection; a guarantee or income claim ends the session as a
compliance fail. AI mode (ai.py) may only re-word the persona line the rules already chose.
"""
from __future__ import annotations

import copy
import hashlib
import random

from .. import guardrails
from . import acts as A
from . import ai, replies
from .scenarios import DIM_LABELS, DIMS, TRUST_START, WIN_LABELS
from .score import MOVE_HINT, MOVES

MIN_TRUST_NEXT = {"call": 40, "dm": 35}
RAPPORT_MAX = 2
# How a hint names an objection without quoting it (a quoted "guarantee?" would trip the guardrails on the hint itself).
OBJECTION_SHORT = {"price": "the price feels too high", "think": "they want to think about it",
                   "partner": "they want to ask their partner", "agency": "they already have an agency",
                   "send_info": "they want you to send info", "price_ask": "they asked what it costs",
                   "guarantee": "they want a promise of results", "income": "they asked what they'll earn",
                   "time": "they asked how much time it takes", "plant": "they want to fake audience questions"}


class SessionOver(Exception):
    pass


def _seg(event: str, sub: str | None = None, fact: str | None = None, silent: bool = False) -> dict:
    return {"e": event, "s": sub, "f": fact, "silent": silent}


class Session:
    def __init__(self, scenario: dict, seed: int, mode: str = "rules", exam: bool = False, hints: bool = True):
        self.scenario = copy.deepcopy({k: v for k, v in scenario.items() if not k.startswith("_")})
        self.seed = int(seed)
        self.mode = "ai" if mode == "ai" else "rules"
        self.exam = bool(exam)
        self.hints = bool(hints) and not self.exam
        self.turns: list[dict] = []
        sc = self.scenario
        t0 = sc.get("trust_start") or TRUST_START[sc["persona"]["temperament"]]
        self.st = {
            "trust": t0, "trust_start": t0, "patience": sc["patience"], "revealed": list(sc.get("pre_revealed", [])),
            "vague_given": [], "gap_confirmed": bool(sc.get("gap_confirmed")), "offer_made": bool(sc.get("offer_made")),
            "offer_turn": None, "queue": list(sc.get("objections", [])), "pending": None, "attempts": 0, "handled": [],
            "failed": [], "outcome": None, "end_reason": None, "learner_turns": 0, "rapport": 0, "ignored": 0,
            "replied": sc["start"] == "persona" or sc["kind"] != "dm", "ai_tokens": 0, "ai_cap": 0, "ai_note": None,
        }

    # ------------------------------------------------------------------ persistence helpers
    def to_state(self) -> dict:
        return {"scenario": self.scenario, "seed": self.seed, "mode": self.mode, "exam": self.exam, "hints": self.hints,
                "st": self.st}

    @classmethod
    def from_state(cls, data: dict, turns: list[dict]) -> "Session":
        s = cls.__new__(cls)
        s.scenario = data["scenario"]
        s.seed = int(data["seed"])
        s.mode = data.get("mode", "rules")
        s.exam = bool(data.get("exam"))
        s.hints = bool(data.get("hints")) and not s.exam
        s.st = data["st"]
        s.turns = turns
        return s

    @property
    def over(self) -> bool:
        return self.st["outcome"] is not None

    def _next_idx(self) -> int:
        return len(self.turns) + 1


def new_session(scenario: dict, seed: int, mode: str = "rules", exam: bool = False, hints: bool = True,
                tenant=None) -> Session:
    s = Session(scenario, seed, mode, exam, hints)
    sc = s.scenario
    if sc["start"] == "persona":
        rng = random.Random(int(hashlib.sha256(f"{s.seed}:open".encode()).hexdigest()[:12], 16))
        line = rng.choice(sc["opening"])
        events = [["open", None]]
        if sc.get("start_pending"):
            s.st["pending"] = sc["start_pending"]
            events.append(["objection", sc["start_pending"]])
        s.turns.append(_persona_turn(s, line, events, [], "template", "", tenant))
    return s


def _persona_turn(s: Session, text: str, events: list, revealed_now: list, source: str, note: str, tenant,
                  template: str | None = None) -> dict:
    allow = set(s.scenario.get("allow_persona_flags") or [])
    fl = guardrails.check_for(tenant, text, {}) if tenant is not None else guardrails.check(text, "plain", {})
    return {"idx": s._next_idx(), "speaker": "persona", "text": text, "events": events, "revealed_now": revealed_now,
            "trust": s.st["trust"], "patience": s.st["patience"], "source": source, "note": note,
            "flags": [dict(f, scenario_test=f["code"] in allow) for f in fl], "template": template}


def _decide(s: Session, cls: dict) -> list[dict]:
    sc, st = s.scenario, s.st
    acts = set(cls["acts"])
    facts = sc.get("facts", {})
    st["revealed_now"] = []

    def trust(d: int) -> None:
        st["trust"] = max(0, min(100, st["trust"] + d))

    def reveal(key: str) -> None:
        if key not in st["revealed"]:
            st["revealed"].append(key)
            st["revealed_now"].append(key)

    def win() -> dict:
        st["outcome"], st["end_reason"] = "won", "won"
        return _seg("win", silent=True)

    if cls["blocked"]:
        st["outcome"], st["end_reason"] = "lost", "compliance"
        return [_seg("violation_end")]
    if cls["stuffing"]:
        trust(-5)
        return [_seg("stuffing")]
    if "monologue" in acts:
        trust(-3)
    if "multi_question" in acts:
        trust(-2)
    if "discount" in acts:
        trust(-10)
        return [_seg("discount")]

    # A cold DM: the opener decides whether they reply at all.
    if sc["kind"] == "dm" and not st["replied"]:
        ok = "personalised" in acts and cls["questions"] == 1 and cls["words"] <= sc["max_words"] and "pitch_early" not in acts
        if ok:
            st["replied"] = True
            trust(5)
            if "opener_reply" in facts:
                reveal("opener_reply")
                return [_seg("reveal_plain", fact=facts["opener_reply"]["say"])]
            return [_seg("neutral")]
        st["ignored"] += 1
        trust(-20 if "pitch_early" in acts else -5)
        if st["ignored"] >= 2:
            st["outcome"], st["end_reason"] = "lost", "ignored"
        return [_seg("pitch_early" if "pitch_early" in acts else "dm_ignore")]

    segs: list[dict] = []
    # A pending objection comes first; only the matching SOP move resolves it.
    p = st["pending"]
    if p:
        ok = bool(MOVES[p] & acts) and not (p == "price" and "price_reframe" not in acts)
        if ok:
            st["pending"], st["attempts"] = None, 0
            st["handled"].append(p)
            trust(5)
            concern = (sc.get("concerns") or {}).get(p)
            ctext = ""
            if concern and concern in facts:
                reveal(concern)
                ctext = facts[concern]["say"]
            elif concern:
                ctext = concern
            if p == "plant" and "alternative" in acts:
                return [_seg("refusal_done"), win()]
            segs.append(_seg("handled", p, ctext) if (ctext or p not in ("think", "agency", "send_info")) else _seg("handled_plain"))
            if p == "partner" and sc["win"] == "partner_bridge":
                return segs + [win()]
            if p == "plant":
                return segs
            if st["queue"] and (st["offer_made"] or sc.get("raise_at") == "handled"):
                nxt = st["queue"].pop(0)
                st["pending"] = nxt
                return segs + [_seg("objection", nxt)]
            acts -= {"objection_probe", "price_reframe", "partner_bridge", "info_bridge", "defer_price", "honest_answer", "offer"}
            cls = dict(cls, dims=[])  # the question in this turn was about the objection, not discovery
        else:
            st["attempts"] += 1
            trust(-5)
            if st["attempts"] >= 2:
                st["failed"].append(p)
                st["outcome"], st["end_reason"] = "lost", "unconvinced"
                return [_seg("end_unconvinced")]
            if "next_step" in acts:
                return [_seg("next_step_blocked"), _seg("reraise", p)]
            return [_seg("reraise", p)]

    if sc["win"] == "refusal" and "plant" in st["handled"] and "alternative" in acts:
        return segs + [_seg("refusal_done"), win()]
    if "disqualify" in acts:
        if not sc.get("budget_ok", True) and "budget" in st["revealed"]:
            return segs + [_seg("disqualify_ok"), win()]
        trust(-10)
        return segs + [_seg("disqualify_wrong")]
    if sc["win"] == "commit" and "commit_action" in acts:
        if "blocker" in st["revealed"]:
            return segs + [_seg("commit_ok"), win()]
        trust(-3)
        return segs + [_seg("commit_early")]

    # The gap: a playback that uses what they said, once at least two things are known.
    if "gap_playback" in acts and not st["gap_confirmed"] and not st["offer_made"]:
        known = [d for d in st["revealed"] if d in DIMS]
        if len(known) >= 2:
            st["gap_confirmed"] = True
            trust(5)
            segs.append(_seg("gap_agree"))
        else:
            trust(1)
            segs.append(_seg("gap_thin"))
    elif "gap_unsupported" in acts and not st["offer_made"]:
        trust(-5)
        segs.append(_seg("gap_unsupported"))

    # Discovery: answer the first question asked; low trust gets a vague answer first.
    dims = [d for d in cls["dims"] if d in facts]
    is_offer = "offer" in acts or (sc["kind"] == "dm" and "next_step" in acts)
    if dims and not is_offer:
        d = dims[0]
        if d in st["revealed"]:
            trust(-2)
            segs.append(_seg("already", fact=facts[d]["say"]))
        elif st["trust"] < 35 and facts[d].get("vague") and d not in st["vague_given"]:
            st["vague_given"].append(d)
            trust(3)
            segs.append(_seg("vague", fact=facts[d]["vague"]))
        else:
            reveal(d)
            trust(3 + (1 if "rapport" in acts and st["rapport"] < RAPPORT_MAX else 0))
            segs.append(_seg("reveal_first" if len(cls["dims"]) > 1 else "reveal", fact=facts[d]["say"]))
    elif cls["dims"] and not is_offer and not segs:
        segs.append(_seg("no_answer"))

    min_trust = MIN_TRUST_NEXT[sc["kind"]]
    # The offer (calls). Before the gap: -15 trust and an objection.
    if sc["kind"] == "call" and "offer" in acts and not st["offer_made"]:
        st["offer_made"] = True
        st["offer_turn"] = st["learner_turns"]
        if not st["gap_confirmed"] and "gap" in sc.get("rubric", []):
            trust(-15)
            nxt = st["queue"].pop(0) if st["queue"] else "price"
            st["pending"] = nxt
            segs.append(_seg("offer_early"))
            segs.append(_seg("objection", nxt, silent=(nxt == "price")))
            return segs
        if st["queue"] and sc.get("raise_at", "offer") in ("offer", "handled"):
            nxt = st["queue"].pop(0)
            st["pending"] = nxt
            segs += [_seg("offer_ok"), _seg("objection", nxt)]
            return segs
        if "next_step" not in acts:
            segs.append(_seg("offer_ready"))

    # The next step.
    if ("next_step" in acts or (sc["kind"] == "dm" and "offer" in acts)) and not st["pending"]:
        if sc["kind"] == "dm":
            need = [d for d in sc.get("dims", []) if d not in st["revealed"]]
            if need or st["trust"] < min_trust:
                trust(-3)
                segs.append(_seg("next_step_early"))
            elif st["queue"]:
                st["offer_made"] = True
                nxt = st["queue"].pop(0)
                st["pending"] = nxt
                segs.append(_seg("objection", nxt))
            else:
                st["offer_made"] = True
                segs.append(_seg("next_step_accept"))
                if sc["win"] == "next_step":
                    segs.append(win())
        elif sc["win"] == "disqualify":
            if "budget" in st["revealed"]:
                trust(-15)
                segs.append(_seg("pushed_unaffordable"))
            elif "budget" in facts:
                reveal("budget")
                segs.append(_seg("reveal", fact=facts["budget"]["say"]))
        elif sc["win"] in ("commit", "refusal"):
            if not segs:
                segs.append(_seg("neutral"))
        elif not st["offer_made"]:
            trust(-3)
            segs.append(_seg("next_step_early"))
        elif st["queue"]:
            nxt = st["queue"].pop(0)
            st["pending"] = nxt
            segs.append(_seg("objection", nxt))
        elif st["trust"] >= min_trust:
            segs.append(_seg("next_step_accept"))
            if sc["win"] == "next_step":
                segs.append(win())
        else:
            segs.append(_seg("next_step_unsure"))

    if not segs:
        if "rapport" in acts and st["rapport"] < RAPPORT_MAX:
            st["rapport"] += 1
            trust(4)
            segs.append(_seg("rapport"))
        elif "too_short" in acts:
            segs.append(_seg("too_short"))
        elif "monologue" in acts:
            segs.append(_seg("monologue"))
        elif "multi_question" in acts:
            segs.append(_seg("multi_question"))
        else:
            segs.append(_seg("neutral"))
    elif "rapport" in acts and st["rapport"] < RAPPORT_MAX and st["learner_turns"] <= 3:
        st["rapport"] += 1
        trust(2)

    ra = sc.get("raise_at")
    if isinstance(ra, int) and st["learner_turns"] >= ra and st["queue"] and not st["pending"] and not st["outcome"]:
        nxt = st["queue"].pop(0)
        st["pending"] = nxt
        segs.append(_seg("objection", nxt))
    return segs


def _compose(s: Session, segs: list[dict], turn: int) -> str:
    sc = s.scenario
    temp = sc["persona"]["temperament"]
    parts = []
    for g in segs:
        if g["silent"]:
            continue
        line = replies.pick(g["e"], temp, s.st["trust"], s.seed, turn, sub=g["s"] if g["e"] in ("objection", "reraise", "handled") else None,
                            overrides=sc.get("lines"), fact=g["f"] or "", concern=g["f"] or "")
        fact = (g["f"] or "").strip()
        if fact and line != fact and line.endswith(fact):
            prefix = line[: -len(fact)].strip()
            # "Honestly? Honestly, I'd ..." reads badly: drop a prefix that starts with the fact's first word
            if prefix and prefix.split()[0].strip("?,.:!").lower() == fact.split()[0].strip("?,.:!").lower():
                line = fact
        if line:
            parts.append(line)
    return " ".join(parts).strip() or "Okay."


def step(s: Session, text: str, tenant=None) -> list[dict]:
    """Play one learner turn. Returns the new turns (learner, persona)."""
    if s.over:
        raise SessionOver("This session has ended")
    st = s.st
    st["learner_turns"] += 1
    n = st["learner_turns"]
    cls = A.classify(text, s.scenario, {"revealed": st["revealed"], "pending": st["pending"], "turn_no": n,
                                        "offer_made": st["offer_made"]}, tenant)
    learner = {"idx": s._next_idx(), "speaker": "learner", "text": text, **cls, "trust_before": st["trust"]}
    s.turns.append(learner)
    st["patience"] -= 1 + (1 if {"monologue", "multi_question"} & set(cls["acts"]) else 0)
    segs = _decide(s, cls)
    if not s.over:
        if st["trust"] <= 0:
            st["outcome"], st["end_reason"] = "lost", "trust"
            segs.append(_seg("trust_zero"))
        elif st["patience"] <= 0:
            st["outcome"], st["end_reason"] = "lost", "patience"
            segs.append(_seg("patience_out"))
    template = _compose(s, segs, n)
    text_out, source, note = template, "template", ""
    if s.mode == "ai" and tenant is not None and not cls["blocked"]:
        d = ai.directive(s.scenario, [(g["e"], g["s"]) for g in segs if not g["silent"]], st["trust"], list(st["revealed"]),
                         template, list(st.get("revealed_now", [])))
        res = ai.phrase(tenant, s.scenario, s.seed, n, text, d, st)
        st["ai_tokens"] = st.get("ai_tokens", 0) + res["tokens"]
        text_out, source, note = res["text"], res["source"], res["note"]
        st["ai_note"] = note
    persona = _persona_turn(s, text_out, [[g["e"], g["s"]] for g in segs], list(st.get("revealed_now", [])), source, note,
                            tenant, template if source != "template" else None)
    s.turns.append(persona)
    return [learner, persona]


def hint(s: Session) -> str | None:
    """A live coaching hint for practice mode (never in exam mode)."""
    if not s.hints or s.over:
        return None
    sc, st = s.scenario, s.st
    if sc["kind"] == "dm" and not st["replied"]:
        kw = (sc.get("public_keywords") or ["their latest post"])[0]
        return f"Open with something specific from their profile (for example their post about {kw}), one question, under {sc['max_words']} words."
    if st["pending"]:
        return f"They raised an objection ({OBJECTION_SHORT[st['pending']]}). {MOVE_HINT[st['pending']]}"
    if sc["win"] == "refusal" and "plant" in st["handled"]:
        return "Give them an honest way to get the chat going, like asking the room to type one word."
    if sc["win"] == "disqualify" and "budget" in st["revealed"]:
        return "Money is tight and they'd need a credit card. Be honest about whether this is the right time."
    missing = [d for d in sc.get("dims", []) if d not in st["revealed"]]
    if missing and not st["offer_made"]:
        if st["trust"] < 35:
            return f"They're guarded. Build a little rapport, then ask about {DIM_LABELS[missing[0]]} as one clear question."
        return f"Ask about {DIM_LABELS[missing[0]]} as one clear question ending with '?'."
    if sc["win"] == "commit":
        return "Agree one specific action with a date."
    if sc["win"] == "disqualify":
        return "Ask how they'd fund it from what they have now. Never suggest new credit."
    if "gap" in sc.get("rubric", []) and not st["gap_confirmed"] and not st["offer_made"]:
        return "Play back the gap in their own words before you offer anything."
    if not st["offer_made"] and sc["kind"] == "call":
        return "The gap is clear. Ask permission, then offer only the parts that fit what they told you."
    return f"Ask for {WIN_LABELS[sc['win']]}."


def hint_checked(s: Session, tenant=None) -> tuple[str | None, list[dict]]:
    h = hint(s)
    if not h:
        return None, []
    fl = guardrails.check_for(tenant, h, {}) if tenant is not None else guardrails.check(h, "plain", {})
    if guardrails.blocked(fl):
        return None, fl
    return h, fl
