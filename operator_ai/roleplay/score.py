"""Turn-aware, order-aware role-play scorer (replaces keyword counting for practice and exams).

Credit rules (each criterion cites the SOP line its rule comes from):
* Discovery: only learner questions (a sentence ending in '?', 3+ words), one dimension per turn, before the offer.
* Gap: a playback that cites a fact the persona actually revealed, confirmed by the persona, before the offer.
* Offer: only after the gap is confirmed.
* Objections: only in the learner turn right after the persona raises it, and only with the matching SOP move
  (a clarifying question for "think about it", the gap / cost of staying for price, a joint call for a partner ...).
  Handling it after the persona repeats it earns partial credit.
* Keyword stuffing (3+ stage cues crammed into one turn) earns nothing, costs points and raises a flag.
* Compliance gate: any blocked guardrail flag makes the total 0.

The score JSON follows the shared roleplays contract: {total, compliance_flags, criteria[{key,label,points,max,
evidence}], scenario_key, mode, seed, signoff} plus extras (citations, annotations, model lines).
"""
from __future__ import annotations

import re

from .. import guardrails
from . import acts as A
from .scenarios import DIM_LABELS, OBJECTION_LABELS, WIN_LABELS

PASS_SCORE = 70

MOVES = {"think": {"objection_probe"}, "price": {"price_reframe"}, "partner": {"partner_bridge"},
         "agency": {"objection_probe"}, "send_info": {"info_bridge", "objection_probe"}, "price_ask": {"defer_price"},
         "guarantee": {"honest_answer"}, "income": {"honest_answer"}, "time": {"honest_answer"},
         "plant": {"refusal_cited", "refusal"}}
MOVE_HINT = {
    "think": "Ask what specifically they want to think through.",
    "price": "Go back to the gap: compare the price with the cost of staying where they are, in their words.",
    "partner": "Offer a short follow-up call with both of them together.",
    "agency": "Ask what is and isn't working with the agency before you say anything about yourself.",
    "send_info": "Ask what would be most useful for them to see, then offer a short call.",
    "price_ask": "Keep price for the call: say it depends on what they need and offer a quick call.",
    "guarantee": "Answer honestly: no one can guarantee results. Explain what it depends on.",
    "income": "Don't give a number. Explain that results vary and talk about the work involved.",
    "time": "Give a realistic weekly time commitment in hours.",
    "plant": "Say no, name the rule (real questions from real attendees only), and offer an honest alternative.",
}
MODEL_Q = {
    "goal": "What would you like this to look like by the end of the next three months?",
    "current": "Where are the numbers right now, roughly per week?",
    "tried": "What have you already tried, and what happened?",
    "why_now": "What made you look at this now rather than in six months?",
    "budget": "If this turned out to be a fit, how would you fund it from what you have now?",
    "decider": "Is anyone else involved in a decision like this?",
    "blocker": "What usually happens right before you stop?",
}
MODEL_OBJ = {
    "think": "Totally fair. When you say you want to think about it, what specifically do you want to think through?",
    "price": "Fair. You said {q}. Compared with another year of that, how does the cost of staying where you are look?",
    "partner": "Makes sense. Shall we book a short call with both of you together, so your partner can ask their own questions?",
    "agency": "Good to know. What's working well with them, and what would you change?",
    "send_info": "Happy to. So I send the right thing, what would be most useful for you to see?",
    "price_ask": "Fair question. It depends on what you need, so I'd rather walk you through it on a quick call. Would that be useful?",
    "guarantee": "Honest answer: no one can guarantee results. It depends on the work and your market. What I can show you is exactly what we'd do each week.",
    "income": "I can't give you a number, and anyone who does is guessing. Results vary with effort and market. What I can tell you is what the work involves.",
    "time": "Realistically, plan for about five to eight hours a week for the first two months.",
    "plant": "I can't let you do that. Our rule is real questions from real attendees, never planted ones. Instead, ask the room to type one word in the chat in the first two minutes.",
}
MODEL_OUTCOME = {
    "next_step": "Shall we put a short call in the diary for Thursday at 10 to map out the first month?",
    "partner_bridge": "Shall we book a short call with both of you together this week?",
    "disqualify": "Honestly, I don't think this is the right time, and I don't want you to put it on a credit card. Start with the free resources and come back when it feels comfortable.",
    "refusal": "Instead, ask the room to type one word in the chat in the first two minutes. Real questions follow.",
    "commit": "So what's the one thing you'll do before our next check-in? Let's say ten DMs by Friday, sent without rewriting.",
}
OUTCOME_RULE = {"next_step": "outcome", "partner_bridge": "objection_partner", "disqualify": "decision_check",
                "refusal": "objection_plant", "commit": "commit"}
DRILL_SCENARIO = {"discovery": "discovery_call", "gap": "discovery_call", "offer_order": "discovery_call",
                  "outcome": "discovery_call", "talk_ratio": "discovery_call", "trust": "student_checkin",
                  "decision_check": "not_qualified", "dm_personal": "cold_dm_coach", "dm_one_question": "cold_dm_coach",
                  "dm_length": "cold_dm_coach", "objection_think": "objection_think", "objection_price": "objection_price",
                  "objection_partner": "objection_partner", "objection_agency": "objection_agency",
                  "objection_send_info": "objection_send_info", "objection_price_ask": "warm_dm_signal",
                  "objection_guarantee": "webinar_qa", "objection_income": "webinar_qa", "objection_time": "webinar_qa",
                  "objection_plant": "coach_plant_questions"}


def _q(text: str, n: int = 140) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def _events(t: dict) -> list[tuple]:
    return [tuple(e) if isinstance(e, (list, tuple)) else (e, None) for e in t.get("events") or [(t.get("event"), None)]]


def _has_event(t: dict | None, name: str) -> bool:
    return bool(t) and any(e[0] == name for e in _events(t))


def score_session(scenario: dict, turns: list[dict], state: dict, rules: dict[str, dict], tenant=None,
                  mode: str = "rules", seed: int = 0) -> dict:
    sc = scenario
    rub = set(sc.get("rubric", []))
    L = [t for t in turns if t["speaker"] == "learner"]
    pos = {id(t): i for i, t in enumerate(turns)}

    def after(t):  # the persona turn that answered learner turn t
        i = pos[id(t)] + 1
        return turns[i] if i < len(turns) and turns[i]["speaker"] == "persona" else None

    clean = [t for t in L if not t.get("stuffing") and not t.get("blocked")]
    credit: dict[int, list[str]] = {t["idx"]: [] for t in L}
    notes: dict[int, list[str]] = {t["idx"]: [] for t in L}
    criteria: list[dict] = []

    def rule(k):
        return rules.get(k) or {"rule_key": k, "label": k, "weight": 0, "params": {}, "status": "missing"}

    def add(key, rule_key, points, mx, evidence, turns_=(), tip="", model_line="", label=None):
        r = rule(rule_key)
        criteria.append({"key": key, "label": label or r["label"], "points": round(max(0.0, min(points, mx)), 1),
                         "max": round(mx, 1), "evidence": evidence, "turns": list(turns_), "rule_key": rule_key,
                         "rule_status": r.get("status", "default"), "tip": tip, "model_line": model_line})

    # ---- offer position (pre-made offers in objection drills count as before turn 1)
    offer_turn = None
    if sc.get("offer_made"):
        offer_turn = -1
    else:
        for t in clean:
            if "offer" in t["acts"] or (sc["kind"] == "dm" and "next_step" in t["acts"] and "pitch_early" not in t["acts"]):
                offer_turn = t["idx"]
                break

    def before_offer(t):
        return offer_turn is None or t["idx"] < offer_turn

    # ---- discovery
    pre = set(sc.get("pre_revealed", []))
    dims = [d for d in sc.get("dims", []) if d not in pre]
    if "discovery" in rub and dims:
        r = rule("discovery")
        got: dict[str, dict] = {}
        for t in clean:
            if not before_offer(t):
                continue
            for d in t.get("dims", []):
                if d in dims and d not in got:
                    got[d] = t
                    credit[t["idx"]].append(f"Asked about {DIM_LABELS[d]}")
                    break  # one dimension per turn
        late = [d for t in clean if not before_offer(t) for d in t.get("dims", []) if d in dims and d not in got]
        missing = [d for d in dims if d not in got]
        ev = ("Asked about " + "; ".join(f"{DIM_LABELS[d]} (turn {t['idx']}: \"{_q(t['text'], 80)}\")" for d, t in got.items())
              if got else "No discovery questions")
        if missing:
            ev += f". Missing: {', '.join(DIM_LABELS[d] for d in missing)}"
        if late:
            ev += ". Asked after the offer (no credit): " + ", ".join(sorted({DIM_LABELS[d] for d in late}))
        add("discovery", "discovery", r["weight"] * len(got) / len(dims), r["weight"], ev, [t["idx"] for t in got.values()],
            tip="One question per turn, ending with '?', before you offer anything." if missing else "",
            model_line=MODEL_Q.get(missing[0], "") if missing else "")

    # ---- decision check (how they'd fund it / who decides)
    if "decision_check" in rub:
        r = rule("decision_check")
        hit = next((t for t in clean if {"ask_budget", "ask_decider"} & set(t["acts"])), None)
        steer = any(f["code"] == "debt_steer" for t in L for f in t.get("flags", []))
        pts = r["weight"] if hit and not steer else 0
        if hit:
            credit[hit["idx"]].append("Decision check")
        add("decision_check", "decision_check", pts, r["weight"],
            (f"Turn {hit['idx']}: \"{_q(hit['text'], 90)}\"" if hit else "Never asked how they'd fund it or who decides")
            + (". Suggested new debt" if steer else ""), [hit["idx"]] if hit else [],
            tip="" if pts else "Ask how they'd fund it from what they have now. Never suggest new credit.",
            model_line="" if pts else MODEL_Q["budget"])

    # ---- gap
    gap_turn = None
    if "gap" in rub and not sc.get("gap_confirmed"):
        r = rule("gap")
        best, pts = None, 0.0
        for t in clean:
            if not before_offer(t) or "gap_playback" not in t["acts"]:
                continue
            p = after(t)
            if _has_event(p, "gap_agree"):
                best, pts = t, r["weight"]
                break
            if best is None:
                best, pts = t, r["weight"] / 2
        if best is not None and pts == r["weight"]:
            gap_turn = best["idx"]
        if best is not None:
            credit[best["idx"]].append("Played back the gap" if pts == r["weight"] else "Partial gap playback")
        unsupported = [t["idx"] for t in L if "gap_unsupported" in t["acts"]]
        for i in unsupported:
            notes[i].append("Gap playback didn't use anything they told you")
        revealed_q = [sc["facts"][k]["say"] for k in state.get("revealed", []) if k in sc.get("facts", {})][:2]
        add("gap", "gap", pts, r["weight"],
            (f"Turn {best['idx']}: \"{_q(best['text'], 100)}\"" + ("" if pts == r["weight"] else " (they only partly agreed)"))
            if best else ("Gap claims without their facts on turns " + ", ".join(map(str, unsupported)) if unsupported
                          else "No gap playback before the offer"), [best["idx"]] if best else [],
            tip="" if pts == r["weight"] else "Play back their own words: where they are, where they want to be.",
            model_line="" if pts == r["weight"] or not revealed_q else
            "So if I've got this right: " + " And: ".join(f"\"{x}\"" for x in revealed_q) + " Is that the gap?")
    elif sc.get("gap_confirmed"):
        gap_turn = -1

    # ---- offer order
    if "offer_order" in rub and not sc.get("offer_made"):
        r = rule("offer_order")
        if offer_turn is None:
            add("offer_order", "offer_order", 0, r["weight"], "No offer made", tip="When the gap is clear, ask permission and offer only what fits.",
                model_line="Would it be useful if I showed you how we'd close that gap?")
        elif gap_turn is not None and gap_turn < offer_turn:
            credit[offer_turn].append("Offer after the gap")
            add("offer_order", "offer_order", r["weight"], r["weight"], f"Offer on turn {offer_turn}, after the gap on turn {gap_turn}",
                [offer_turn])
        else:
            notes[offer_turn].append("Offer came before the gap was clear")
            add("offer_order", "offer_order", 0, r["weight"], f"Offer on turn {offer_turn} before the gap was confirmed", [offer_turn],
                tip="Only when the gap is clear do you present the offer.",
                model_line="Before I show you anything: would it be useful if I played back what I've heard?")

    # ---- objections: credit only in the learner turn right after the raise, only with the matching move
    if "objections" in rub:
        raised: dict[str, list[tuple[dict, bool]]] = {}
        for i, t in enumerate(turns):
            if t["speaker"] != "persona":
                continue
            for ev, sub in _events(t):
                if ev in ("objection", "reraise") and sub:
                    raised.setdefault(sub, []).append((t, ev == "reraise"))
        expected = list(dict.fromkeys(([sc["start_pending"]] if sc.get("start_pending") else []) + list(sc.get("objections", []))))
        for typ in list(dict.fromkeys(expected + list(raised))):
            rk = f"objection_{typ}"
            r = rule(rk)
            mx = r["weight"]
            pts, ev_txt, tt = 0.0, "", []
            if typ not in raised:
                ev_txt = "Not reached in this session"
            else:
                for persona_t, is_retry in raised[typ]:
                    i = pos[id(persona_t)] + 1
                    nxt = turns[i] if i < len(turns) and turns[i]["speaker"] == "learner" else None
                    if nxt is None:
                        ev_txt = ev_txt or "Session ended right after the objection"
                        continue
                    ok = not nxt.get("stuffing") and not nxt.get("blocked") and bool(MOVES[typ] & set(nxt["acts"]))
                    factor = (r["params"].get("retry_credit", 0.5) if is_retry else 1.0)
                    if typ == "plant" and ok and "refusal_cited" not in nxt["acts"]:
                        factor *= 0.5  # refused without naming the rule
                    if ok:
                        pts = mx * factor
                        tt = [nxt["idx"]]
                        credit[nxt["idx"]].append(f"Handled \"{OBJECTION_LABELS[typ]}\"")
                        ev_txt = (f"Turn {nxt['idx']}: \"{_q(nxt['text'], 100)}\""
                                  + (" (after they repeated it)" if is_retry else "")
                                  + (" (refused, but didn't name the rule)" if typ == "plant" and factor < 1 and not is_retry else ""))
                        break
                    notes[nxt["idx"]].append(f"Didn't use the SOP move for \"{OBJECTION_LABELS[typ]}\"")
                    tt.append(nxt["idx"])
                    ev_txt = f"Turn {nxt['idx']} didn't use the SOP move: \"{_q(nxt['text'], 90)}\""
            q = ""
            if typ == "price":
                cur = sc.get("facts", {}).get("current", {}).get("say") or "where you are now"
                q = cur[:1].lower() + cur[1:].rstrip(".")
            model = MODEL_OBJ[typ].format(q=q) if pts < mx else ""
            add(rk, rk, pts, mx, ev_txt, tt, tip=MOVE_HINT[typ] if pts < mx else "", model_line=model,
                label=f"Objection: \"{OBJECTION_LABELS[typ]}\"")

    # ---- outcome
    if "outcome" in rub:
        r = rule("outcome")
        rk = OUTCOME_RULE.get(sc["win"], "outcome")
        won = state.get("outcome") == "won"
        win_t = next((t for t in reversed(L) if _has_event(after(t), "win")), None) if won else None
        attempted = [t for t in clean if "next_step" in t["acts"]] if sc["win"] == "next_step" else []
        pts = r["weight"] if won else (r["weight"] * 0.2 if attempted else 0)
        if win_t is not None:
            credit[win_t["idx"]].append("Reached the right outcome")
        reason = {"lost": "They ended it", "compliance": "Ended on a compliance problem", "ended": "You ended the session",
                  "patience": "They ran out of patience", "trust": "They lost trust", "ignored": "No reply",
                  "abandoned": "Abandoned"}.get(state.get("end_reason") or "", "Not reached")
        add("outcome", rk, pts, r["weight"],
            (f"Won on turn {win_t['idx']}: \"{_q(win_t['text'], 90)}\"" if win_t else "Won")
            if won else f"{reason}" + (f"; asked for a next step on turn {attempted[0]['idx']}" if attempted else ""),
            [win_t["idx"]] if win_t else [t["idx"] for t in attempted[:1]],
            tip="" if won else f"Aim for {WIN_LABELS.get(sc['win'], 'the right next step')}.",
            model_line="" if won else MODEL_OUTCOME.get(sc["win"], ""), label=f"Outcome: {WIN_LABELS.get(sc['win'], 'next step')}")

    # ---- talk ratio (calls)
    if "talk_ratio" in rub and sc["kind"] == "call":
        r = rule("talk_ratio")
        mx_share = float(r["params"].get("max_share", 0.6))
        lw = sum(t.get("words", len(A.words(t["text"]))) for t in L)
        pw = sum(len(A.words(t["text"])) for t in turns if t["speaker"] == "persona")
        share = lw / (lw + pw) if (lw + pw) else 1.0
        if len(L) < int(r["params"].get("min_turns", 3)):
            add("talk_ratio", "talk_ratio", 0, r["weight"], f"Too short to judge ({len(L)} turns)",
                tip="Have a real conversation: ask, then listen.")
        else:
            pts = r["weight"] if share <= mx_share else r["weight"] * max(0.0, 1 - (share - mx_share) / 0.25)
            add("talk_ratio", "talk_ratio", pts, r["weight"], f"You spoke {round(share * 100)}% of the words",
                tip="" if share <= mx_share else "Ask one short question, then let them talk.")

    # ---- trust built
    if "trust" in rub:
        r = rule("trust")
        gain = (state.get("trust", 0) - state.get("trust_start", 0))
        full = float(r["params"].get("full_gain", 20)) or 20
        pts = 0 if state.get("end_reason") in ("trust", "compliance") else r["weight"] * max(0.0, min(1.0, gain / full))
        add("trust", "trust", pts, r["weight"], f"Trust moved from {state.get('trust_start', 0)} to {state.get('trust', 0)}",
            tip="" if pts >= r["weight"] else "Use their words back to them, ask before you tell, and never push.")

    # ---- DM craft
    if sc["kind"] == "dm":
        if "dm_personal" in rub and sc.get("start") == "learner":
            r = rule("dm_personal")
            first = L[0] if L else None
            ok = bool(first) and "personalised" in first["acts"] and not first.get("stuffing")
            if ok:
                credit[first["idx"]].append("Personal opener")
            kw = (sc.get("public_keywords") or ["their latest post"])[0]
            add("dm_personal", "dm_personal", r["weight"] if ok else 0, r["weight"],
                f"Turn 1: \"{_q(first['text'], 90)}\"" if first else "No opener", [first["idx"]] if first else [],
                tip="" if ok else "Mention something specific they posted.",
                model_line="" if ok else f"Loved your recent post about {kw}. Is that the main thing you're focused on right now?")
        if "dm_one_question" in rub and L:
            r = rule("dm_one_question")
            mq = int(r["params"].get("max_questions", 1))
            good = [t for t in L if t["text"].count("?") <= mq and not t.get("stuffing")]
            add("dm_one_question", "dm_one_question", r["weight"] * len(good) / len(L), r["weight"],
                f"{len(good)} of {len(L)} messages had at most {mq} question", [t["idx"] for t in L if t not in good],
                tip="" if len(good) == len(L) else "One question per message. If you ask three, they answer none.")
        if "dm_length" in rub and L:
            r = rule("dm_length")
            mw = int(r["params"].get("max_words", sc.get("max_words", 60)))
            good = [t for t in L if t.get("words", 0) <= mw]
            add("dm_length", "dm_length", r["weight"] * len(good) / len(L), r["weight"],
                f"{len(good)} of {len(L)} messages were {mw} words or fewer", [t["idx"] for t in L if t not in good],
                tip="" if len(good) == len(L) else f"Keep DMs under {mw} words: one line of context, one question.")

    # ---- penalties and integrity
    penalties = []
    stuffed = [t for t in L if t.get("stuffing")]
    for t in stuffed:
        notes[t["idx"]].append("Keyword stuffing: no credit for this turn")
    if stuffed:
        penalties.append({"key": "stuffing", "label": "Keyword stuffing", "points": 10 * len(stuffed),
                          "evidence": f"Turns {', '.join(str(t['idx']) for t in stuffed)}"})
    disc = [t for t in L if "discount" in t["acts"]]
    if disc:
        r = rule("no_discount")
        per, cap = float(r["params"].get("per_occurrence", 10)), float(r["params"].get("cap", 20))
        penalties.append({"key": "no_discount", "label": r["label"], "points": min(cap, per * len(disc)),
                          "evidence": f"Turn {disc[0]['idx']}: \"{_q(disc[0]['text'], 80)}\"", "rule_key": "no_discount"})
        for t in disc:
            notes[t["idx"]].append("Discounted to close")
    if sc["kind"] == "call":
        r = rule("monologue")
        mono = [t for t in L if "monologue" in t["acts"]]
        if mono:
            penalties.append({"key": "monologue", "label": r["label"], "points": float(r["params"].get("per_occurrence", 3)) * len(mono),
                              "evidence": f"Turns {', '.join(str(t['idx']) for t in mono)}", "rule_key": "monologue"})
            for t in mono:
                notes[t["idx"]].append("Monologue")

    # ---- compliance gate
    block_flags = [f for t in L for f in t.get("flags", []) if f.get("severity") == "block"]
    for t in L:
        if t.get("blocked"):
            notes[t["idx"]].append("Compliance: " + "; ".join(f["message"] for f in t.get("flags", []) if f["severity"] == "block"))

    earned = sum(c["points"] for c in criteria)
    possible = sum(c["max"] for c in criteria)
    raw = round(100 * earned / possible) if possible else 0
    total = max(0, min(100, raw - round(sum(p["points"] for p in penalties))))
    gate = None
    if block_flags:
        total, gate = 0, "compliance"

    # ---- annotations: green (earned credit), amber (no credit or a weak move), red (violation, stuffing, discount)
    annotations = []
    for t in L:
        i = t["idx"]
        red = t.get("blocked") or t.get("stuffing") or "discount" in t["acts"]
        color = "red" if red else ("green" if credit[i] else "amber")
        weak = [A.ACT_LABELS[a] for a in t["acts"] if a in ("multi_question", "monologue", "too_short", "gap_unsupported",
                                                            "price_unsupported", "pitch_early")]
        annotations.append({"idx": i, "color": color, "credit": credit[i], "notes": notes[i] + weak,
                            "acts": [A.ACT_LABELS.get(a, a) for a in t["acts"] if a != "statement"]})

    # ---- citations: every criterion and penalty points at the SOP line its rule comes from
    from . import rubric as RB

    cites: list[dict] = []
    seen: dict[tuple, str] = {}
    for c in criteria + [p for p in penalties if p.get("rule_key")] + [{"rule_key": "compliance"}]:
        r = rule(c["rule_key"])
        k = (r.get("cite_file"), r.get("cite_lines"))
        if k not in seen:
            cid = f"E{len(cites) + 1}"
            seen[k] = cid
            cites.append(RB.citation(tenant, r, cid) if tenant is not None else
                         {"id": cid, "file": r.get("cite_file", ""), "lines": r.get("cite_lines", ""), "title": r.get("cite_file", ""),
                          "snippet": "", "kind": "sop", "rule_key": r["rule_key"], "rule_status": r.get("status", "default"),
                          "problem": ""})
        c["citations"] = [seen[k]]
        c["cite"] = next(x for x in cites if x["id"] == seen[k])
    compliance_cite = seen.get((rule("compliance").get("cite_file"), rule("compliance").get("cite_lines")))

    # ---- model lines must pass the guardrails too
    meta = A.guard_meta(sc, 2)
    for c in criteria:
        c["drill_scenario"] = DRILL_SCENARIO.get(c["key"]) if c["points"] < c["max"] else None
        if c.get("model_line"):
            fl = guardrails.check_for(tenant, c["model_line"], meta) if tenant is not None else \
                guardrails.check(c["model_line"], guardrails.mode_for(meta), meta)
            c["model_flags"] = fl
            if guardrails.blocked(fl):
                c["model_line"] = ""
    focus = sorted([c for c in criteria if c["points"] < c["max"]], key=lambda c: c["points"] - c["max"])[:3]
    unconfirmed = sorted({c["rule_key"] for c in criteria if c.get("rule_status") != "confirmed"})
    return {
        "total": int(total), "raw": raw, "compliance_flags": len(block_flags), "gate": gate,
        "compliance_citation": compliance_cite,
        "criteria": criteria, "penalties": penalties, "scenario_key": sc["key"], "mode": "ai" if mode == "ai" else "rules",
        "seed": seed, "signoff": None, "outcome": state.get("outcome") or "ended", "end_reason": state.get("end_reason"),
        "passed": total >= PASS_SCORE and not block_flags, "pass_mark": PASS_SCORE,
        "integrity_flags": [{"turn": t["idx"], "code": "keyword_stuffing"} for t in stuffed],
        "annotations": annotations, "citations": cites, "focus": [c["key"] for c in focus],
        "unconfirmed_rules": unconfirmed, "scorer": "turn-aware rules v1",
    }


# --------------------------------------------------------------------------- scoring a plain transcript
LEARNER_TAGS = re.compile(r"^\s*(me|you|learner|rep|closer|setter|coach|host|operator)\s*:\s*", re.I)
PERSONA_TAGS = re.compile(r"^\s*(prospect|them|lead|client|persona|student|attendee|buyer)\s*:\s*", re.I)
_OBJ_RX = {"think": re.compile(r"\b(think about it|think it over|sleep on it)\b", re.I),
           "price": re.compile(r"\b(too expensive|can'?t afford|out of (my|our) budget|costs? too much|a lot of money)\b", re.I),
           "partner": re.compile(r"\b(ask|talk to|check with) my (partner|wife|husband|spouse|business partner)\b", re.I),
           "agency": re.compile(r"\balready (have|pay|use) an agency\b", re.I),
           "send_info": re.compile(r"\bsend me (some )?info\b|\bjust (send|email) me\b", re.I),
           "guarantee": re.compile(r"\bguarantee", re.I)}
_AGREE = re.compile(r"\b(yes|yeah|exactly|that'?s (it|right)|spot on|correct)\b", re.I)
_ACCEPT = re.compile(r"\b(let'?s (book|do it)|book it|send (me )?a time|sounds good|works for me|see you then)\b", re.I)
_STOP = set("the and that this with have from your what when they them just about would could really there their been "
            "were will into some much very more most than then also over like only".split())

GENERIC = {"key": "transcript", "title": "Transcript", "kind": "call", "skill": "discovery", "difficulty": 2,
           "persona": {"name": "Prospect", "niche": "", "channel": "call", "temperament": "analytical", "public": []},
           "facts": {}, "dims": ["goal", "current", "tried", "why_now"], "pre_revealed": [], "objections": [],
           "start": "learner", "start_pending": None, "patience": 30, "max_words": 120, "win": "next_step",
           "rubric": ["discovery", "gap", "offer_order", "objections", "outcome", "talk_ratio"], "public_keywords": [],
           "lead_tier": "warm", "concerns": {}}


def score_text(text: str, scenario: dict | None = None, rules: dict | None = None, tenant=None) -> dict:
    """Score a finished transcript. Lines tagged "Me:/Rep:/Closer:" are the learner, "Prospect:/Them:/Client:" the
    other side; untagged text is one learner turn. The same order-aware rules apply, so a keyword-stuffed line scores
    near zero."""
    from . import rubric as RB

    sc = dict(scenario or GENERIC)
    sc["facts"] = dict(sc.get("facts") or {})
    if rules is None:
        rules = RB.rules(tenant) if tenant is not None else {
            k: {"rule_key": k, "label": v[0], "weight": v[1], "params": v[2], "cite_file": v[3], "cite_lines": v[4],
                "status": "default"} for k, v in RB.DEFAULTS.items()}
    raw_turns: list[tuple[str, str]] = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        if PERSONA_TAGS.match(line):
            raw_turns.append(("persona", PERSONA_TAGS.sub("", line).strip()))
        elif LEARNER_TAGS.match(line):
            raw_turns.append(("learner", LEARNER_TAGS.sub("", line).strip()))
        elif raw_turns:
            raw_turns[-1] = (raw_turns[-1][0], raw_turns[-1][1] + " " + line.strip())
        else:
            raw_turns.append(("learner", line.strip()))
    turns: list[dict] = []
    revealed: list[str] = []
    pending = None
    state = {"trust": 0, "trust_start": 0, "revealed": revealed, "outcome": None, "end_reason": "ended"}
    last_dims: list[str] = []
    n = 0
    for who, t in raw_turns:
        if who == "learner":
            n += 1
            cls = A.classify(t, sc, {"revealed": revealed, "pending": pending, "turn_no": n}, tenant)
            turns.append({"idx": len(turns) + 1, "speaker": "learner", "text": t, **cls})
            last_dims = cls["dims"]
        else:
            evs = []
            prev = turns[-1] if turns and turns[-1]["speaker"] == "learner" else None
            for d in last_dims[:1]:
                key = f"said_{len(sc['facts'])}"
                kws = [w for w in re.findall(r"[a-z0-9']{4,}", t.lower()) if w not in _STOP][:8]
                if kws:
                    sc["facts"][key] = {"say": t, "keywords": kws}
                    revealed.append(key)
                    if d not in revealed:
                        revealed.append(d)
            if prev is not None and "gap_playback" in prev["acts"] and _AGREE.search(t):
                evs.append(("gap_agree", None))
            if prev is not None and "next_step" in prev["acts"] and _ACCEPT.search(t):
                evs.append(("win", None))
                state["outcome"] = "won"
            if pending and prev is not None and MOVES.get(pending, set()) & set(prev["acts"]):
                evs.append(("handled", pending))
                pending = None
            for typ, rx in _OBJ_RX.items():
                if rx.search(t):
                    evs.append(("objection", typ))
                    pending = typ
                    break
            turns.append({"idx": len(turns) + 1, "speaker": "persona", "text": t, "events": evs or [("neutral", None)]})
            last_dims = []
    sc["objections"] = []
    return score_session(sc, turns, state, rules, tenant, "rules", 0)
