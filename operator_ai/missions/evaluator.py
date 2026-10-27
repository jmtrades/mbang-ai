"""Measure missions and correct course, rules first.

metrics()        leading indicators (prospects, openers confirmed sent, replies) and lagging ones (calls booked and
                 showed, proposals, wins), rates and time-to-first-client. Revenue is only ever the user's own
                 recorded figure.
evaluate()       rules from the agentic audit:
                   * reply rate under 4% after 80 confirmed sends -> switch the opener move, re-research the top 20,
                     a just-in-time opener lesson
                   * show rate under 50% -> add confirmation and reminder steps
                   * zero calls booked by day 14 -> escalate to a coach with a diagnostic, add a diagnose step
                 Each produces a diff the user confirms (apply_replan); escalations are internal and go straight away.
"""
from __future__ import annotations

import time

from .. import db, diagnostic
from . import registry, templates
from .registry import DAY, add_event, clock, events_of

REPLY_FLOOR, REPLY_MIN_SENDS = 0.04, 80
SHOW_FLOOR, SHOW_MIN_CALLS = 0.50, 4
ZERO_CALLS_DAY = 14
STALL_DAYS = 3  # a step waiting this long on its person (an answer, an approval, a certification) is escalated once
# steps whose wait is not the person's to end: an optional review, a call booked for later, a prospect's reply or yes,
# a date or a checkpoint
NOT_STALLABLE = ("call_review", "take_call", "reply_watch", "await_win", "checkpoint", "wait_until")


def _distinct(evs: list[dict], pred=lambda d: True) -> int:
    return len({(e["detail"].get("party_type"), e["detail"].get("party_id")) for e in evs if pred(e["detail"])})


def metrics(tenant, m: dict, now: float | None = None) -> dict:
    now = now or clock()
    ctx = m.get("context") or {}
    evs = events_of(tenant, m["id"], "sent", "reply", "booked", "showed", "no_show", "proposal", "won")
    by: dict[str, list] = {}
    for e in evs:
        by.setdefault(e["kind"], []).append(e)
    sent = by.get("sent", [])
    openers = sum(1 for e in sent if e["detail"].get("first_touch"))
    followups = sum(1 for e in sent if e["detail"].get("kind") == "followup")
    replies = _distinct(by.get("reply", []))
    positive = _distinct(by.get("reply", []), lambda d: d.get("positive"))
    booked = len(by.get("booked", []))
    showed, no_show = len(by.get("showed", [])), len(by.get("no_show", []))
    proposals = len(by.get("proposal", []))
    wins = max(1 if ctx.get("won") else 0, len(by.get("won", [])))
    n_prospects = researched = None
    if m["template"] in templates.OUTREACH_TEMPLATES:
        with tenant.db() as c:
            r = c.execute("SELECT COUNT(*) n, SUM(CASE WHEN research IS NOT NULL THEN 1 ELSE 0 END) r FROM prospects "
                          "WHERE owner_user_id=? AND COALESCE(do_not_contact,0)=0", (m["user_id"],)).fetchone()
        n_prospects, researched = r["n"] or 0, r["r"] or 0
    with tenant.db() as c:
        drafts = {r["status"]: r["n"] for r in c.execute(
            "SELECT status, COUNT(*) n FROM queue WHERE json_extract(meta,'$.mission_id')=? GROUP BY status", (m["id"],))}
    created = m.get("created") or now
    won_at = ctx.get("won_at")
    return {
        "leading": {"prospects": n_prospects, "researched": researched, "openers_sent": openers,
                    "followups_sent": followups, "replies": replies, "positive_replies": positive},
        "lagging": {"calls_booked": booked, "calls_showed": showed, "no_shows": no_show, "proposals_sent": proposals,
                    "wins": wins},
        "rates": {"reply_rate": round(replies / openers, 4) if openers else None,
                  "show_rate": round(showed / (showed + no_show), 4) if (showed + no_show) else None},
        "drafts": {"waiting": sum(drafts.get(s, 0) for s in registry.QUEUE_OPEN), "approved": drafts.get("approved", 0)
                   + drafts.get("sent", 0) + drafts.get("scheduled", 0) + drafts.get("awaiting_manual_send", 0),
                   "rejected": drafts.get("rejected", 0)},
        "days_active": round(max(0.0, ((won_at or now) if ctx.get("won") else now) - created) / DAY, 1),
        "time_to_first_client_days": round((won_at - created) / DAY, 1) if won_at else None,
        "recorded_revenue": ctx.get("recorded_revenue"),
        "revenue_note": "Your own recorded figure, not a projection." if ctx.get("recorded_revenue") is not None else None,
    }


def headline(tenant, m: dict) -> dict:
    met = metrics(tenant, m)
    return {"openers_sent": met["leading"]["openers_sent"], "replies": met["leading"]["replies"],
            "calls_booked": met["lagging"]["calls_booked"], "wins": met["lagging"]["wins"],
            "days_active": met["days_active"], "time_to_first_client_days": met["time_to_first_client_days"]}


_PVA = [("openers_sent", "Openers sent", "leading", "openers"), ("replies", "Replies", "leading", "replies"),
        ("calls_booked", "Calls booked", "lagging", "calls_booked"), ("calls_showed", "Calls that showed", "lagging",
                                                                       "calls_showed"),
        ("wins", "Clients won", "lagging", "wins")]


def plan_vs_actual(m: dict, met: dict) -> list[dict]:
    t = (m.get("context") or {}).get("targets") or {}
    if not t.get("openers"):
        return []
    wd = max(1, t.get("working_days") or 1)
    elapsed = met["days_active"] * 5 / 7
    frac = min(1.0, elapsed / wd)
    rows = []
    for key, label, kind, tkey in _PVA:
        target = t.get(tkey)
        if not target:
            continue
        actual = met["leading"].get(key) if kind == "leading" else met["lagging"].get(key)
        expected = round(target * frac, 1)
        rows.append({"key": key, "label": label, "kind": kind, "target": target, "expected_by_now": expected,
                     "actual": actual or 0, "pct_of_expected": round(100 * (actual or 0) / expected) if expected >= 1 else None})
    return rows


# --------------------------------------------------------------------------- rules
def _next_move(ctx: dict) -> str:
    """The next opener style to try, in the vocabulary of whoever drafts the openers (the prospects feature's styles
    when it is installed, so the change really reaches the next block)."""
    order = registry.opener_moves()
    cur = ctx.get("opener_move") or "question"
    tried = list(ctx.get("moves_tried") or [cur])
    for mv in order:
        if mv not in tried:
            return mv
    i = order.index(cur) if cur in order else 0
    return order[(i + 1) % len(order)]


def rules(tenant, m: dict, met: dict, now: float) -> list[dict]:
    ctx = m.get("context") or {}
    marks = ctx.get("rule_marks") or {}
    steps = m.get("steps") or []
    skeys = {s["skey"] for s in steps}
    out = []
    sent, replies = met["leading"]["openers_sent"], met["leading"]["replies"]
    if sent >= REPLY_MIN_SENDS and replies / sent < REPLY_FLOOR and sent >= (marks.get("low_reply_rate") or 0) + REPLY_MIN_SENDS:
        cur, nxt = ctx.get("opener_move") or "question", _next_move(ctx)
        n = sum(1 for k in skeys if k.startswith("reresearch")) + 1
        out.append({"rule": "low_reply_rate", "mark": sent,
                    "why": f"Reply rate is {replies / sent:.1%} after {sent} confirmed sends (the floor is {REPLY_FLOOR:.0%}).",
                    "diff": {"context": {"opener_move": {"from": cur, "to": nxt}},
                             "add": [{"skey": f"reresearch{n}", "type": "research_fit", "title": "Re-research the top 20 prospects",
                                      "params": {"top": 20, "force": True}, "depends": []},
                                     {"skey": f"opener_lesson{n}", "type": "human_task", "title": "Opener lesson and drill",
                                      "params": {"task": "Read the opener lesson and do one opener drill before the next block.",
                                                 "href": "#/academy"}, "depends": []}],
                             "remove": []}})
    decided = met["lagging"]["calls_showed"] + met["lagging"]["no_shows"]
    if decided >= SHOW_MIN_CALLS and met["lagging"]["calls_showed"] / decided < SHOW_FLOOR and \
            not any(s["agent"] == "reminder_ladder" for s in steps) and not marks.get("low_show_rate"):
        out.append({"rule": "low_show_rate", "mark": decided,
                    "why": f"Show rate is {met['lagging']['calls_showed'] / decided:.0%} over {decided} calls (the floor is {SHOW_FLOOR:.0%}).",
                    "diff": {"context": {}, "remove": [],
                             "add": [{"skey": "reminders", "type": "reminder_ladder", "title": "Confirmations and reminders",
                                      "params": {}, "depends": ["booking"] if "booking" in skeys else []}]}})
    stuck = []
    for s in steps:
        st = s["status"]
        if st in ("ready", "running"):  # a recheck about to run: judge the wait it is in
            st = (s.get("output") or {}).get("_st") or st
        waits_on_person = st in ("blocked_human", "waiting_approval") or (s["agent"] == "cert_gate" and st == "waiting_event")
        if not waits_on_person or s["agent"] in NOT_STALLABLE or marks.get(f"stalled:{s['id']}"):
            continue
        since = (s.get("output") or {}).get("_since") or s.get("updated") or now
        if now - since >= STALL_DAYS * DAY:
            what = {"waiting_approval": "drafts waiting for approval",
                    "blocked_human": "waiting for an answer or a task"}.get(st, "certification not done yet")
            stuck.append((s, f"'{s['title']}' ({what}, {int((now - since) // DAY)} days)"))
    if stuck:
        why = "Stuck on its person for days: " + "; ".join(t for _, t in stuck) + "."
        out.append({"rule": "stalled:" + ",".join(str(s["id"]) for s, _ in stuck), "kind": "mission_stalled", "mark": 1,
                    "marks": {f"stalled:{s['id']}": 1 for s, _ in stuck}, "why": why,
                    "escalate": (why + " Next: " + (stuck[0][0].get("message") or ""))[:600],
                    "diff": {"context": {}, "remove": [], "add": []}})
    days = (now - (m.get("created") or now)) / DAY
    if m["template"] in templates.OUTREACH_TEMPLATES and days >= ZERO_CALLS_DAY and met["lagging"]["calls_booked"] == 0 \
            and not ctx.get("won") and not marks.get("zero_calls_day14"):
        an = diagnostic.analyse(registry.mission_numbers(tenant, m))
        bn = an.get("bottleneck")
        why = f"No calls booked after {int(days)} days."
        detail = why + (f" Bottleneck: {bn['label']} ({bn['value']} against {bn['benchmark']})." if bn else
                        " Not enough numbers yet to name a bottleneck: check the daily blocks are being approved and sent.")
        out.append({"rule": "zero_calls_day14", "mark": 1, "why": why, "escalate": detail,
                    "diff": {"context": {}, "remove": [],
                             "add": [] if "diagnose14" in skeys else
                             [{"skey": "diagnose14", "type": "diagnose", "title": "Find the bottleneck", "params": {},
                               "depends": []}]}})
    return out


def evaluate(tenant, m: dict, now: float | None = None, actor: str = "system") -> dict:
    from . import engine

    now = now or clock()
    if "steps" not in m:
        m = engine.get(tenant, m["id"])
    met = metrics(tenant, m, now)
    fs = rules(tenant, m, met, now)
    ids = []
    marks = dict((m.get("context") or {}).get("rule_marks") or {})
    for f in fs:
        if f.get("escalate"):
            escalate(tenant, m, f.get("kind") or f["rule"], f["escalate"], key=f["rule"])
        marks[f["rule"]] = f["mark"]
        marks.update(f.get("marks") or {})
        if f["diff"].get("add") or f["diff"].get("remove") or f["diff"].get("context"):
            rid = store_replan(tenant, m["id"], "rules", f["rule"], {**f["diff"], "why": f["why"]})
            if rid:
                ids.append(rid)
    if fs:
        engine._patch_context(tenant, m["id"], {"rule_marks": marks})
    return {"metrics": met, "findings": [{"rule": f["rule"], "why": f["why"]} for f in fs], "replan_ids": ids}


def daily(tenant, m: dict, now: float) -> bool:
    """Snapshot today's metrics once a day and run the rules on the first snapshot of the day."""
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    met = metrics(tenant, m, now)
    with tenant.db() as c:
        n = c.execute("INSERT OR IGNORE INTO mission_metrics(mission_id,day,metrics,created) VALUES(?,?,?,?)",
                      (m["id"], day, db.jdump(met), now)).rowcount
    if n:
        evaluate(tenant, m, now)
    return bool(n)


def store_replan(tenant, mid: int, source: str, rule: str, diff: dict) -> int | None:
    with tenant.db() as c:
        if c.execute("SELECT 1 FROM mission_replans WHERE mission_id=? AND rule=? AND status='proposed'", (mid, rule)).fetchone():
            return None
        rid = c.execute("INSERT INTO mission_replans(mission_id,source,rule,diff,status,created) VALUES(?,?,?,?,?,?)",
                        (mid, source, rule, db.jdump(diff), "proposed", clock())).lastrowid
    add_event(tenant, mid, "replan_proposed", {"replan_id": rid, "rule": rule, "message": diff.get("why", "")})
    return rid


def apply_replan(tenant, mid: int, rid: int, actor: str, accept: bool = True) -> dict:
    from . import engine

    with tenant.db() as c:
        r = db.row(c.execute("SELECT * FROM mission_replans WHERE id=? AND mission_id=?", (rid, mid)).fetchone())
    if r is None:
        raise engine.MissionError(404, "Plan change not found")
    if r["status"] != "proposed":
        raise engine.MissionError(409, f"This plan change was already {r['status']}")
    diff = db.jload(r["diff"], {}) or {}
    now = clock()
    if not accept:
        with tenant.db() as c:
            c.execute("UPDATE mission_replans SET status='dismissed', decided_by=?, decided_at=? WHERE id=?", (actor, now, rid))
        add_event(tenant, mid, "replan_dismissed", {"replan_id": rid, "rule": r["rule"], "by": actor})
        return engine.get(tenant, mid)
    m = engine.get(tenant, mid)
    if m["status"] in ("done", "cancelled"):
        raise engine.MissionError(409, f"The mission is {m['status']}")
    have = {s["skey"] for s in m["steps"]}
    specs = []
    with tenant.db() as c:
        ur = c.execute("SELECT role FROM users WHERE id=?", (m["user_id"],)).fetchone()
    role = ur["role"] if ur else None
    for a in diff.get("add") or []:
        t = registry.get_type(a.get("type"))
        if t is None or not t.allowed(role):
            continue  # a step this person's role can't run is never added, whatever the suggestion said
        key, i = a.get("skey") or a["type"], 2
        while key in have:
            key, i = f"{a.get('skey') or a['type']}_{i}", i + 1
        have.add(key)
        specs.append({"skey": key, "type": t.key, "title": (a.get("title") or t.label)[:80], "params": a.get("params") or {},
                      "depends": [d for d in (a.get("depends") or []) if d in have]})
    with tenant.db() as c, db.tx(c):
        start = (c.execute("SELECT MAX(ord) FROM mission_steps WHERE mission_id=?", (mid,)).fetchone()[0] or 0) + 1
        engine._insert_steps(c, mid, specs, start, now)
        for key in diff.get("remove") or []:
            c.execute("UPDATE mission_steps SET status='skipped', message='Removed by a plan change.' WHERE mission_id=? "
                      "AND skey=? AND status='pending'", (mid, key))
        c.execute("UPDATE mission_replans SET status='applied', decided_by=?, decided_at=? WHERE id=?", (actor, now, rid))
    ctx = m["context"]
    patch: dict = {}
    for k, ch in (diff.get("context") or {}).items():
        if k in ("opener_move",) and isinstance(ch, dict) and (ch.get("to") in registry.OPENER_MOVES
                                                            or ch.get("to") in registry.opener_moves()):
            patch[k] = ch["to"]
            patch["moves_tried"] = list(dict.fromkeys(list(ctx.get("moves_tried") or [ch.get("from") or "question"]) + [ch["to"]]))
    patch["decisions"] = (list(ctx.get("decisions") or []) + [{"at": now, "rule": r["rule"], "source": r["source"],
                                                               "why": diff.get("why", ""), "by": actor}])[-50:]
    if r["source"] == "ai":
        with tenant.db() as c:
            c.execute("UPDATE missions SET plan_source='ai+rules' WHERE id=?", (mid,))
    engine._patch_context(tenant, mid, patch)
    add_event(tenant, mid, "replan_applied", {"replan_id": rid, "rule": r["rule"], "by": actor, "message": diff.get("why", ""),
                                              "added": [s["skey"] for s in specs]})
    engine.advance(tenant, mid)
    engine.pump(tenant, mid)
    return engine.get(tenant, mid)


# --------------------------------------------------------------------------- escalation
def escalate(tenant, m: dict, kind: str, detail: str, key: str | None = None) -> int | None:
    """Raise an internal escalation for the mission user's coach (escalations feature when installed, otherwise a
    coach task). Never prospect-facing. Written once per key."""
    key = key or kind
    prior = [e for e in events_of(tenant, m["id"], "escalated") if e["detail"].get("key") == key]
    if prior:
        return prior[0]["detail"].get("escalation_id") or prior[0]["detail"].get("coach_task_id")
    with tenant.db() as c:
        r = c.execute("SELECT s.coach_id, s.id sid FROM users u JOIN students s ON s.id=u.student_id WHERE u.id=?",
                      (m["user_id"],)).fetchone()
    coach_id, sid = (r["coach_id"], r["sid"]) if r else (None, None)
    eid = None
    fn = registry.feature_fn("escalations", "raise_")
    if fn is not None:
        try:
            eid = fn(tenant, kind, "high" if kind == "mission_step_failed" else "medium", detail, assignee_user_id=coach_id,
                     mission_id=m["id"], ref=f"mission:{m['id']}")
        except Exception:  # noqa: BLE001 - fall back to a coach task
            eid = None
    task_id = None
    if eid is None:
        with tenant.db() as c:
            task_id = c.execute("INSERT INTO coach_tasks(coach_id,student_id,kind,detail,status,due,created,demo) "
                                "VALUES(?,?,?,?,?,?,?,?)", (coach_id, sid, "mission_escalation",
                                                            f"Mission #{m['id']}: {detail}"[:500], "open", clock() + DAY,
                                                            clock(), 1 if tenant.is_demo else 0)).lastrowid
    add_event(tenant, m["id"], "escalated", {"kind": kind, "message": detail[:400], "escalation_id": eid,
                                             "coach_task_id": task_id}, key=key)
    return eid or task_id
