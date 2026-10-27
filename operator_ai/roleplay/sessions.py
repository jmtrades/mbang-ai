"""Role-play sessions: storage (roleplays + roleplay_turns), who may see what, traces, scoring, sign-off.

Visibility: a learner sees their own sessions; a coach also sees the sessions of the students they coach;
the owner sees everything. Only the learner plays their own session. A coach (for their students) or the
owner signs off a completed session; nobody signs off their own.
"""
from __future__ import annotations

import secrets
import threading

from .. import audit, db, events, guardrails, llm, runtime
from . import acts as acts_mod
from . import ai, engine, rubric, scenarios
from .scenarios import OBJECTION_LABELS
from .score import MOVE_HINT, score_session

_locks: dict[tuple, threading.Lock] = {}
_locks_guard = threading.Lock()
SOURCE_LABEL = {"template": "Template line", "live": "AI phrased", "replay": "Replayed AI line"}
DRILL_QUERY = {"think": "need to think about it ask what specifically", "price": "too expensive go back to the gap cost of staying",
               "partner": "ask my partner follow-up call with both"}


class NotVisible(Exception):
    """The session doesn't exist for this viewer (404)."""


class Forbidden(Exception):
    """Visible, but this viewer can't do that (403)."""


class Conflict(Exception):
    """Wrong state for that action (409)."""


def _lock(tenant, rid: int) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault((tenant.id, rid), threading.Lock())


def _actor(user: dict) -> str:
    return f"{user['username']}({user['role']})"


# --------------------------------------------------------------------------- visibility
def coached_user_ids(tenant, coach_id: int) -> set[int]:
    with tenant.db() as c:
        ids = {r["id"] for r in c.execute("SELECT id FROM users WHERE coach_id=?", (coach_id,))}
        ids |= {r["id"] for r in c.execute("SELECT u.id FROM users u JOIN students s ON s.id=u.student_id WHERE s.coach_id=?",
                                           (coach_id,))}
    ids.discard(coach_id)
    return ids


def visible_user_ids(tenant, viewer: dict) -> set[int] | None:
    """None means everyone (owner)."""
    if viewer["role"] == "owner":
        return None
    ids = {viewer["id"]}
    if viewer["role"] == "coach":
        ids |= coached_user_ids(tenant, viewer["id"])
    return ids


def _load_row(tenant, rid: int, viewer: dict) -> dict:
    with tenant.db() as c:
        r = db.row(c.execute("SELECT * FROM roleplays WHERE id=?", (rid,)).fetchone())
    if r is None:
        raise NotVisible(rid)
    vis = visible_user_ids(tenant, viewer)
    if vis is not None and r["user_id"] not in vis:
        raise NotVisible(rid)
    return r


def _load_turns(tenant, rid: int) -> list[dict]:
    with tenant.db() as c:
        rows = db.rows(c.execute("SELECT * FROM roleplay_turns WHERE roleplay_id=? ORDER BY idx", (rid,)))
    out = []
    for r in rows:
        meta = db.jload(r["meta"], {}) or {}
        t = {"idx": r["idx"], "speaker": r["speaker"], "text": r["text"], "acts": db.jload(r["acts"], []) or [],
             "flags": db.jload(r["flags"], []) or [], **meta}
        out.append(t)
    return out


def _session(tenant, r: dict) -> engine.Session:
    return engine.Session.from_state(db.jload(r["state"], {}), _load_turns(tenant, r["id"]))


def _save_turns(c, rid: int, turns: list[dict]) -> None:
    for t in turns:
        meta = {k: v for k, v in t.items() if k not in ("idx", "speaker", "text", "acts", "flags")}
        c.execute("INSERT INTO roleplay_turns(roleplay_id,idx,speaker,text,acts,flags,meta,created) VALUES(?,?,?,?,?,?,?,?)",
                  (rid, t["idx"], t["speaker"], t["text"], db.jdump(t.get("acts", [])), db.jdump(t.get("flags", [])),
                   db.jdump(meta), db.now()))


def _transcript(s: engine.Session) -> list[dict]:
    return [{"speaker": t["speaker"], "text": t["text"]} for t in s.turns]


def _reopen_run(tenant, run_id: int | None, actor: str, title: str) -> runtime.Run:
    r = runtime.get(tenant, run_id) if run_id else None
    if r is None:
        return runtime.Run(tenant, "roleplay", title, PLAN, actor)
    run = runtime.Run(tenant, "roleplay", r["title"], r["plan"], actor, run_id=run_id)
    run.steps = list(r.get("steps") or [])
    run.ai_mode = r.get("ai_mode") or "rules"
    run.t0 = r.get("created") or run.t0
    return run


PLAN = ["Start the scenario (persona card, seed, meters)", "Each learner turn: classify acts, update meters, persona reply",
        "Score the session (turn-aware rules, cited)"]


def _turn_out(t: dict, exam: bool = False) -> dict:
    """A turn as recorded in the run trace. The learner can read their own traces, so during an exam the trace
    carries no live scoring (acts, events, flags): that would be a hint channel the exam is meant to remove. The
    full scoring lands in the trace when the session is scored."""
    keys = ("idx", "speaker", "text", "source") if exam else ("idx", "speaker", "text", "acts", "events", "flags", "source",
                                                              "note", "stuffing")
    return {k: t.get(k) for k in keys if t.get(k) is not None}


# --------------------------------------------------------------------------- lifecycle
def start(tenant, user: dict, scenario_key: str, seed: int | None = None, mode: str = "rules", exam: bool = False,
          hints: bool = True) -> dict:
    sc = scenarios.get(tenant, scenario_key)
    if sc is None or not sc.get("_active", True):
        raise KeyError(scenario_key)
    if exam:
        mode, hints, seed = "rules", False, None  # certification attempts: rules only, no hints, server-chosen seed
    if seed is None:
        seed = secrets.randbelow(1_000_000)
    s = engine.new_session(sc, seed, mode, exam, hints, tenant)
    if s.mode == "ai":
        s.st["ai_cap"] = ai.session_cap(tenant)
        s.st["ai_note"] = llm.unavailable_reason(tenant)
    actor = _actor(user)
    title = f"Role-play: {sc['title']}"
    run = runtime.Run(tenant, "roleplay", title, PLAN, actor)
    run.step("start", {"scenario": sc["key"], "seed": seed, "mode": s.mode, "exam": s.exam},
             lambda: ({"opening": [_turn_out(t, True) for t in s.turns]} if s.exam else
                      {"opening": [_turn_out(t) for t in s.turns], "trust": s.st["trust"], "patience": s.st["patience"]}),
             label="Start the scenario")
    with tenant.db() as c, db.tx(c):
        rid = c.execute("INSERT INTO roleplays(user_id,scenario_key,transcript,state,score,status,created,updated,seed,mode,exam,"
                        "hints,demo,run_id,total) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (user["id"], sc["key"], db.jdump(_transcript(s)), db.jdump(s.to_state()), None, "active", db.now(),
                         db.now(), seed, s.mode, 1 if s.exam else 0, 1 if s.hints else 0, 1 if tenant.is_demo else 0, run.id,
                         None)).lastrowid
        _save_turns(c, rid, s.turns)
    run.finish({"roleplay_id": rid, "status": "active"})
    return view(tenant, user, rid)


def turn(tenant, user: dict, rid: int, text: str) -> dict:
    text = " ".join((text or "").split())
    if not text:
        raise ValueError("Say something first")
    with _lock(tenant, rid):
        r = _load_row(tenant, rid, user)
        if r["user_id"] != user["id"]:
            raise Forbidden("Only the learner can play this session")
        if r["status"] != "active":
            raise Conflict("This session has ended")
        s = _session(tenant, r)
        actor = _actor(user)
        run = _reopen_run(tenant, r["run_id"], actor, f"Role-play: {s.scenario['title']}")
        if s.over:
            # The rules already ended it but scoring didn't finish (for example the server stopped mid-turn):
            # score it now instead of failing every later request.
            _finish(tenant, rid, s, run, user)
            return view(tenant, user, rid)
        n = s.st["learner_turns"] + 1
        new = run.step("turn", {"turn": n, "learner": text},
                       lambda: [_turn_out(t, s.exam) for t in engine.step(s, text, tenant)], label=f"Turn {n}")
        new_turns = s.turns[-len(new):]
        if any(t.get("source") == "live" for t in new_turns):
            run.use_ai("live")
        elif any(t.get("source") == "replay" for t in new_turns):
            run.use_ai("replay")
        with tenant.db() as c, db.tx(c):
            _save_turns(c, rid, new_turns)
            c.execute("UPDATE roleplays SET transcript=?, state=?, updated=? WHERE id=?",
                      (db.jdump(_transcript(s)), db.jdump(s.to_state()), db.now(), rid))
        if s.over:
            _finish(tenant, rid, s, run, user)
        else:
            run.finish({"roleplay_id": rid, "status": "active", "turns": len(s.turns)})
    return view(tenant, user, rid)


def end(tenant, user: dict, rid: int) -> dict:
    with _lock(tenant, rid):
        r = _load_row(tenant, rid, user)
        if r["user_id"] != user["id"]:
            raise Forbidden("Only the learner can end this session")
        if r["status"] != "active":
            raise Conflict("This session has already ended")
        s = _session(tenant, r)
        if s.st["outcome"] is None:
            s.st["outcome"], s.st["end_reason"] = "ended", "ended"
        run = _reopen_run(tenant, r["run_id"], _actor(user), f"Role-play: {s.scenario['title']}")
        _finish(tenant, rid, s, run, user)
    return view(tenant, user, rid)


def abandon(tenant, user: dict, rid: int) -> dict:
    with _lock(tenant, rid):
        r = _load_row(tenant, rid, user)
        if r["user_id"] != user["id"]:
            raise Forbidden("Only the learner can abandon this session")
        if r["status"] != "active":
            raise Conflict("This session has already ended")
        with tenant.db() as c:
            c.execute("UPDATE roleplays SET status='abandoned', updated=? WHERE id=?", (db.now(), rid))
    return view(tenant, user, rid)


def _finish(tenant, rid: int, s: engine.Session, run: runtime.Run, user: dict) -> dict:
    rules = rubric.rules(tenant)
    sc = run.step("score", {"turns": len(s.turns), "outcome": s.st["outcome"]},
                  lambda: score_session(s.scenario, s.turns, s.st, rules, tenant, s.mode, s.seed),
                  label="Score the session (turn-aware rules, cited)")
    sc["exam"] = s.exam
    sc["flagged_rules"] = []
    if s.exam:
        # Certification only counts rules the owner confirmed AND whose SOP citation still checks out today
        # (the cited file could have changed since the owner confirmed the rule).
        used = {c["rule_key"] for c in sc["criteria"]} | {p["rule_key"] for p in sc["penalties"] if p.get("rule_key")}
        sc["flagged_rules"] = sorted(k for k in used if k in rules and not rubric.validate_rule(tenant, rules[k])["ok"])
    sc["certifiable"] = bool(s.exam and sc["passed"] and not sc["unconfirmed_rules"] and not sc["flagged_rules"])
    ann = {a["idx"]: a for a in sc["annotations"]}
    with tenant.db() as c, db.tx(c):
        c.execute("UPDATE roleplays SET score=?, status='complete', total=?, state=?, updated=? WHERE id=?",
                  (db.jdump(sc), sc["total"], db.jdump(s.to_state()), db.now(), rid))
        for idx, a in ann.items():
            row = c.execute("SELECT meta FROM roleplay_turns WHERE roleplay_id=? AND idx=?", (rid, idx)).fetchone()
            if row is not None:
                meta = db.jload(row["meta"], {}) or {}
                meta.update(color=a["color"], credit=a["credit"], notes=a["notes"], act_labels=a["acts"])
                c.execute("UPDATE roleplay_turns SET meta=? WHERE roleplay_id=? AND idx=?", (db.jdump(meta), rid, idx))
    run.finish({"roleplay_id": rid, "status": "complete", "total": sc["total"], "outcome": sc["outcome"],
                "compliance_flags": sc["compliance_flags"]})
    # Missed objections become spaced-repetition cards in the learner's drill bank.
    for crit in sc["criteria"]:
        if crit["key"].startswith("objection_") and crit["points"] < crit["max"] and crit["turns"]:
            try:
                add_drill_card(tenant, user["id"], crit["key"][len("objection_"):], rid)
            except Exception:  # noqa: BLE001 - a drill card must never break scoring
                pass
    try:
        events.emit(tenant, "roleplay_completed", {"roleplay_id": rid, "user_id": user["id"], "scenario_key": s.scenario["key"],
                                                   "total": sc["total"], "outcome": sc["outcome"],
                                                   "compliance_flags": sc["compliance_flags"], "exam": s.exam})
    except Exception:  # noqa: BLE001
        pass
    return sc


def add_drill_card(tenant, user_id: int, typ: str, rid: int) -> dict:
    from .. import call_review

    label = OBJECTION_LABELS[typ]
    d = call_review.make_drill(tenant, label, DRILL_QUERY.get(typ, MOVE_HINT[typ]), f"roleplay:{rid}")
    with tenant.db() as c:
        card = c.execute("SELECT id FROM drill_cards WHERE drill_id=? AND user_id=?", (d["id"], user_id)).fetchone()
        if card is None:
            cid = c.execute("INSERT INTO drill_cards(drill_id,user_id,due) VALUES(?,?,?)", (d["id"], user_id, db.now())).lastrowid
        else:
            cid = card["id"]
            c.execute("UPDATE drill_cards SET due=? WHERE id=?", (db.now(), cid))
    return {"drill_id": d["id"], "card_id": cid, "objection": label}


# --------------------------------------------------------------------------- reading
def _display(tenant, uid: int) -> str:
    with tenant.db() as c:
        r = c.execute("SELECT display, username FROM users WHERE id=?", (uid,)).fetchone()
    return (r["display"] or r["username"]) if r else "Unknown"


def mode_label(r: dict, s: engine.Session) -> str:
    if r["mode"] != "ai":
        return "Rules mode: the other person's lines come from a fixed template bank. Scores are rules-based."
    note = s.st.get("ai_note")
    base = "AI phrasing: the rules decide what happens and the AI only words the other person's line. Scores are rules-based."
    return base + (f" ({note}; template lines are used.)" if note and note not in ("AI phrased",) and "Replayed" not in note else "")


def view(tenant, viewer: dict, rid: int) -> dict:
    r = _load_row(tenant, rid, viewer)
    s = _session(tenant, r)
    sc = s.scenario
    score = db.jload(r["score"], None)
    complete = r["status"] == "complete"
    is_learner = r["user_id"] == viewer["id"]
    show_acts = complete or (s.hints and not s.exam)
    turns = []
    for t in s.turns:
        item = {"idx": t["idx"], "speaker": t["speaker"], "text": t["text"]}
        if t["speaker"] == "persona":
            item["source"] = SOURCE_LABEL.get(t.get("source", "template"), "Template line")
            item["note"] = t.get("note") or ""
            item["flags"] = [f for f in t.get("flags", [])]
        else:
            # During an exam only blocking problems show (they end the session anyway); review flags such as
            # "keyword stuffing: no credit" would be live scoring hints.
            item["flags"] = [f for f in t.get("flags", []) if not (s.exam and not complete and f.get("severity") != "block")]
            if show_acts:
                item["acts"] = t.get("act_labels") or [acts_mod.ACT_LABELS.get(a, a) for a in t.get("acts", [])
                                                       if a != "statement"]
            if complete:
                item["color"] = t.get("color", "amber")
                item["credit"] = t.get("credit", [])
                item["notes"] = t.get("notes", [])
        turns.append(item)
    active = r["status"] == "active"
    out = {
        "id": r["id"], "user_id": r["user_id"], "user": _display(tenant, r["user_id"]), "status": r["status"],
        "scenario": scenarios.public(sc, viewer["role"]), "mode": r["mode"], "mode_label": mode_label(r, s),
        "exam": bool(r["exam"]), "hints": bool(s.hints), "seed": r["seed"], "created": r["created"], "updated": r["updated"],
        "demo": bool(r["demo"]), "turns": turns, "score": score, "run_id": r["run_id"],
        "can_play": active and is_learner, "is_learner": is_learner,
        "can_signoff": complete and not is_learner and viewer["role"] in ("owner", "coach") and not (score or {}).get("signoff"),
    }
    if active and not s.exam:
        b = "low" if s.st["trust"] < 35 else "mid" if s.st["trust"] < 65 else "high"
        out["meters"] = {"trust": {"low": "Guarded", "mid": "Neutral", "high": "Warm"}[b], "trust_value": s.st["trust"],
                         "patience": max(0, s.st["patience"]), "patience_max": sc["patience"]}
    if active and is_learner:
        h, fl = engine.hint_checked(s, tenant)
        out["hint"], out["hint_flags"] = h, fl
    if complete:
        out["reveals"] = [{"key": k, "said": f["say"], "revealed": k in s.st["revealed"]} for k, f in sc.get("facts", {}).items()
                          if k != "opener_reply"]
    return out


def list_for(tenant, viewer: dict, user_id: int | None = None, status: str | None = None, pending_signoff: bool = False,
             limit: int = 50) -> list[dict]:
    vis = visible_user_ids(tenant, viewer)
    q = "SELECT r.id, r.user_id, r.scenario_key, r.status, r.mode, r.exam, r.total, r.score, r.created, r.updated, r.demo, " \
        "u.display, u.username FROM roleplays r LEFT JOIN users u ON u.id=r.user_id WHERE 1=1"
    args: list = []
    if vis is not None:
        q += f" AND r.user_id IN ({','.join('?' * len(vis))})"
        args += sorted(vis)
    if user_id is not None:
        q += " AND r.user_id=?"
        args.append(user_id)
    if status:
        q += " AND r.status=?"
        args.append(status)
    q += " ORDER BY r.id DESC LIMIT ?"
    args.append(max(1, min(200, limit)))
    with tenant.db() as c:
        rows = db.rows(c.execute(q, args))
    titles = {s["key"]: s["title"] for s in scenarios.all_(tenant, include_inactive=True)}
    out = []
    for r in rows:
        sc = db.jload(r["score"], None) or {}
        item = {"id": r["id"], "user_id": r["user_id"], "user": r["display"] or r["username"] or "Unknown",
                "scenario_key": r["scenario_key"], "title": titles.get(r["scenario_key"], r["scenario_key"]),
                "status": r["status"], "mode": r["mode"], "exam": bool(r["exam"]), "total": r["total"],
                "outcome": sc.get("outcome"), "compliance_flags": sc.get("compliance_flags", 0), "passed": sc.get("passed"),
                "signoff": sc.get("signoff"), "created": r["created"], "updated": r["updated"], "demo": bool(r["demo"])}
        if pending_signoff and (r["status"] != "complete" or item["signoff"] or r["user_id"] == viewer["id"]):
            continue
        out.append(item)
    return out


# --------------------------------------------------------------------------- sign-off and drills
def signoff(tenant, viewer: dict, rid: int, note: str = "") -> dict:
    with _lock(tenant, rid):
        r = _load_row(tenant, rid, viewer)
        if viewer["role"] not in ("owner", "coach"):
            raise Forbidden("Only a coach or the owner can sign off a role-play")
        if r["user_id"] == viewer["id"]:
            raise Forbidden("You can't sign off your own role-play")
        if r["status"] != "complete":
            raise Conflict("Only a completed role-play can be signed off")
        sc = db.jload(r["score"], {}) or {}
        if sc.get("signoff"):
            raise Conflict("Already signed off")
        clean_note = guardrails.scrub((note or "").strip()[:500], guardrails.tenant_banned_names(tenant))
        sc["signoff"] = {"by": viewer.get("display") or viewer["username"], "by_user_id": viewer["id"], "at": db.now(),
                         "note": clean_note}
        with tenant.db() as c:
            c.execute("UPDATE roleplays SET score=?, updated=? WHERE id=?", (db.jdump(sc), db.now(), rid))
    audit.log(tenant, _actor(viewer), "roleplay.signoff", {"roleplay": rid, "learner": r["user_id"], "total": sc.get("total")})
    try:
        events.emit(tenant, "roleplay_signed_off", {"roleplay_id": rid, "user_id": r["user_id"], "by_user_id": viewer["id"],
                                                    "scenario_key": r["scenario_key"], "total": sc.get("total")})
    except Exception:  # noqa: BLE001
        pass
    return view(tenant, viewer, rid)


def drill(tenant, viewer: dict, rid: int, criterion: str) -> dict:
    r = _load_row(tenant, rid, viewer)
    if r["user_id"] != viewer["id"]:
        raise Forbidden("Only the learner can drill their own session")
    if r["status"] != "complete":
        raise Conflict("Finish the session first")
    sc = db.jload(r["score"], {}) or {}
    crit = next((c for c in sc.get("criteria", []) if c["key"] == criterion), None)
    if crit is None:
        raise KeyError(criterion)
    out = {"criterion": criterion, "scenario_key": crit.get("drill_scenario") or r["scenario_key"], "card": None}
    if criterion.startswith("objection_") and criterion[len("objection_"):] in OBJECTION_LABELS:
        out["card"] = add_drill_card(tenant, viewer["id"], criterion[len("objection_"):], rid)
    if scenarios.get(tenant, out["scenario_key"]) is None:
        out["scenario_key"] = r["scenario_key"]
    return out


# --------------------------------------------------------------------------- owner/coach test run
def test_run(tenant, user: dict, scenario: dict, lines: list[str], seed: int = 1, mode: str = "rules") -> dict:
    """Play scripted learner lines against a scenario (saved or a draft) without saving a session."""
    clean = scenarios.validate({k: v for k, v in scenario.items() if not k.startswith("_")}, tenant)
    rules = rubric.rules(tenant)

    def fn(run: runtime.Run):
        s = engine.new_session(clean, seed, "rules", False, True, tenant)
        for i, line in enumerate(lines, start=1):
            if s.over:
                break
            line = " ".join((line or "").split())
            if not line:
                continue
            run.step("turn", {"turn": i, "learner": line}, lambda line=line: [_turn_out(t) for t in engine.step(s, line, tenant)],
                     label=f"Turn {i}")
        if s.st["outcome"] is None:
            s.st["outcome"], s.st["end_reason"] = "ended", "ended"
        sc = run.step("score", {}, lambda: score_session(clean, s.turns, s.st, rules, tenant, "rules", seed),
                      label="Score the session (turn-aware rules, cited)")
        return {"transcript": [{"idx": t["idx"], "speaker": t["speaker"], "text": t["text"],
                                "acts": [a for a in t.get("acts", []) if a != "statement"],
                                "events": t.get("events"), "flags": t.get("flags", [])} for t in s.turns],
                "score": sc, "seed": seed, "scenario_key": clean["key"]}

    out = runtime.execute(tenant, "roleplay_test", f"Role-play test run: {clean['title']}",
                          ["Play the scripted lines", "Score the session"], fn, _actor(user))
    return {**out["result"], "run_id": out["run_id"]}
