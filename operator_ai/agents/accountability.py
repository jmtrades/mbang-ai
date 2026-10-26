"""Accountability and follow-ups: check-ins, rule-based risk score and trend, an intervention
per signal (cited), nudges (queued), coach escalation tasks, per-coach lists, an owner digest
and the Retention Star (5-point community health). Built to run over 2,000+ students."""
from __future__ import annotations

import time
from collections import defaultdict

from .. import db, queue, retrieval, runtime

DAY = 86400
SIGNALS = {
    "checkin_gap": ("Missed check-ins", "check-in accountability weekly community thread coach review", 30),
    "low_outreach": ("Outreach volume dropped", "send twenty openers a day log replies volume", 20),
    "low_show_rate": ("Show rate under 50%", "show rate reminders twenty-four hours two hours ten minutes", 15),
    "low_mood": ("Mood trending down", "kind no-show recovery life happens be kind", 15),
    "no_clients": ("No clients after 60 days", "pick a niche go narrow first offer USP", 10),
}
NUDGES = {
    "checkin_gap": "Hey {first}, we missed your check-in this week. What's one small win from the last few days?",
    "low_outreach": "Hey {first}, outreach dipped this week. Want the 20-openers-a-day checklist from the DM module?",
    "low_show_rate": "Hey {first}, your show rate dipped. Here's the reminder ladder from the booking module: 24h, 2h, 10 min, each with value.",
    "low_mood": "Hey {first}, how are you doing this week? Your coach has a slot if you want to talk it through.",
    "no_clients": "Hey {first}, let's sharpen your niche and USP this week; it's the fastest route to client one.",
}


def _load(tenant, student_ids: list[int] | None = None) -> tuple[list[dict], dict[int, list[dict]]]:
    with tenant.db() as c:
        if student_ids:
            q = f"SELECT * FROM students WHERE id IN ({','.join('?' * len(student_ids))})"
            students = db.rows(c.execute(q, student_ids))
        else:
            students = db.rows(c.execute("SELECT id,name,email,coach_id,joined,niche,stage,clients,demo FROM students"))
        since = time.time() - 70 * DAY
        rows = c.execute("SELECT student_id, at, outreach, calls_booked, calls_showed, mood, wins FROM checkins "
                         "WHERE at > ? ORDER BY at", (since,)).fetchall()
    by: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        by[r[0]].append({"at": r[1], "outreach": r[2] or 0, "booked": r[3] or 0, "showed": r[4] or 0, "mood": r[5] or 0,
                         "wins": r[6] or ""})
    return students, by


def risk(student: dict, cks: list[dict], now: float | None = None) -> dict:
    now = now or time.time()
    signals = []
    last = cks[-1]["at"] if cks else student.get("joined") or now
    gap_days = (now - last) / DAY
    if gap_days > 17:
        signals.append(("checkin_gap", 45, f"{int(gap_days)} days since last check-in"))
    elif gap_days > 10:
        signals.append(("checkin_gap", 30, f"{int(gap_days)} days since last check-in"))
    if len(cks) >= 2:
        recent = cks[-1]["outreach"]
        avg = sum(c["outreach"] for c in cks[:-1]) / (len(cks) - 1)
        if recent < 20 or (avg > 0 and recent < 0.5 * avg):
            signals.append(("low_outreach", 20, f"{recent} DMs last check-in vs {avg:.0f} average"))
    booked = sum(c["booked"] for c in cks[-3:])
    showed = sum(c["showed"] for c in cks[-3:])
    if booked >= 3 and showed / booked < 0.5:
        signals.append(("low_show_rate", 15, f"{showed}/{booked} calls showed"))
    moods = [c["mood"] for c in cks[-2:] if c["mood"]]
    if moods and sum(moods) / len(moods) < 3:
        signals.append(("low_mood", 15, f"mood {sum(moods) / len(moods):.1f}/5"))
    if (student.get("clients") or 0) == 0 and now - (student.get("joined") or now) > 60 * DAY:
        signals.append(("no_clients", 10, "no clients after 60 days"))
    score = min(100, sum(s[1] for s in signals))
    return {"score": score, "level": "high" if score >= 50 else "medium" if score >= 25 else "low",
            "signals": [{"key": k, "label": SIGNALS[k][0], "points": p, "detail": d} for k, p, d in signals]}


def board(tenant, coach_id: int | None = None) -> dict:
    """Risk for every student (fast path, no retrieval)."""
    t0 = time.time()
    students, by = _load(tenant)
    now = time.time()
    out = []
    for s in students:
        if coach_id and s["coach_id"] != coach_id:
            continue
        cks = by.get(s["id"], [])
        r = risk(s, cks, now)
        prev = risk(s, [c for c in cks if c["at"] < now - 14 * DAY], now - 14 * DAY)
        out.append({"id": s["id"], "name": s["name"], "coach_id": s["coach_id"], "niche": s["niche"], "demo": s["demo"],
                    "risk": r["score"], "level": r["level"], "signals": r["signals"],
                    "trend": "rising" if r["score"] > prev["score"] else "falling" if r["score"] < prev["score"] else "flat",
                    "last_checkin": cks[-1]["at"] if cks else None})
    out.sort(key=lambda x: -x["risk"])
    levels = defaultdict(int)
    for x in out:
        levels[x["level"]] += 1
    return {"students": out, "counts": dict(levels), "total": len(out), "ms": int((time.time() - t0) * 1000)}


def retention_star(tenant) -> dict:
    """Five-point community health (each 0-1): check-in rate, outreach consistency, show-rate
    health, mood, progress (clients/wins)."""
    students, by = _load(tenant)
    if not students:
        return {"points": [], "overall": 0}
    now = time.time()
    n = len(students)
    checked = sum(1 for s in students if by.get(s["id"]) and now - by[s["id"]][-1]["at"] < 10 * DAY)
    consistent = 0
    show_ok = show_n = 0
    moods = []
    progress = 0
    for s in students:
        cks = by.get(s["id"], [])
        last3 = cks[-3:]
        if len(last3) >= 2 and all(c["outreach"] >= 20 for c in last3):
            consistent += 1
        b = sum(c["booked"] for c in last3)
        if b:
            show_n += 1
            show_ok += 1 if sum(c["showed"] for c in last3) / b >= 0.6 else 0
        moods += [c["mood"] for c in last3 if c["mood"]]
        if (s.get("clients") or 0) > 0 or any(c["wins"] for c in last3):
            progress += 1
    pts = [
        {"key": "checkins", "label": "Checking in", "value": round(checked / n, 2)},
        {"key": "outreach", "label": "Outreach consistency", "value": round(consistent / n, 2)},
        {"key": "show_rate", "label": "Show-rate health", "value": round(show_ok / show_n, 2) if show_n else 0},
        {"key": "mood", "label": "Mood", "value": round((sum(moods) / len(moods) - 1) / 4, 2) if moods else 0},
        {"key": "progress", "label": "Progress (clients / wins)", "value": round(progress / n, 2)},
    ]
    return {"points": pts, "overall": round(sum(p["value"] for p in pts) / 5, 2), "students": n}


PLAN = ["Load check-ins", "Score risk + trend", "Interventions per signal (cited)", "Queue nudges",
        "Coach escalation tasks", "Owner digest"]


def run_interventions(tenant, user: dict | None = None, limit: int = 50) -> dict:
    actor = f"{user['username']}({user['role']})" if user else "system"

    def fn(run: runtime.Run):
        b = run.step("board", {}, lambda: board(tenant), "Load check-ins")
        at_risk = [s for s in b["students"] if s["level"] != "low"][:limit]
        run.note("risk", {"students": b["total"]}, {"at_risk": len(at_risk), "counts": b["counts"]}, label="Score risk + trend")

        def _cites():
            out = {}
            for i, (k, (label, q, _)) in enumerate(SIGNALS.items()):
                hits = retrieval.search(tenant, q, k=1)
                if hits:
                    out[k] = retrieval.cite({**hits[0], "id": f"S{i + 1}"})
            return out

        cites = run.step("interventions", {}, _cites, "Interventions per signal (cited)")
        nudges = []
        tasks = []

        def _nudges():
            for s in at_risk:
                top = s["signals"][0]["key"] if s["signals"] else None
                if not top:
                    continue
                first = s["name"].split()[0]
                q = queue.add(tenant, "nudge", NUDGES[top].format(first=first), channel="telegram", recipient_ref=s["name"],
                              title=f"Nudge: {s['name']} — {SIGNALS[top][0]}",
                              meta={"student_id": s["id"], "signal": top, "citation": cites.get(top)}, run_id=run.id,
                              actor=actor, demo=bool(s["demo"]))
                nudges.append(q["id"])
            return nudges

        run.step("nudges", {"n": len(at_risk)}, _nudges, "Queue nudges")

        def _tasks():
            with tenant.db() as c:
                for s in at_risk:
                    if s["level"] != "high":
                        continue
                    detail = "; ".join(f"{x['label']} ({x['detail']})" for x in s["signals"])
                    tasks.append(c.execute(
                        "INSERT INTO coach_tasks(coach_id,student_id,kind,detail,status,due,created,demo) VALUES(?,?,?,?,?,?,?,?)",
                        (s["coach_id"], s["id"], "escalation", f"Book a 15-minute call: {detail}", "open", db.now() + 2 * DAY,
                         db.now(), s["demo"])).lastrowid)
            return tasks

        run.step("escalations", {}, _tasks, "Coach escalation tasks")
        dg = run.step("digest", {}, lambda: digest(tenant, b), "Owner digest")
        return {"at_risk": len(at_risk), "nudges": nudges, "tasks": tasks, "digest": dg, "citations": cites}

    out = runtime.execute(tenant, "accountability", "Accountability sweep", PLAN, fn, actor)
    return {**out["result"], "run_id": out["run_id"]}


def digest(tenant, b: dict | None = None) -> dict:
    b = b or board(tenant)
    with tenant.db() as c:
        coaches = {r["id"]: r["display"] for r in c.execute("SELECT id, display FROM users WHERE role='coach'")}
        open_tasks = c.execute("SELECT COUNT(*) FROM coach_tasks WHERE status='open'").fetchone()[0]
    per_coach = defaultdict(lambda: {"students": 0, "high": 0, "medium": 0})
    for s in b["students"]:
        pc = per_coach[coaches.get(s["coach_id"], "Unassigned")]
        pc["students"] += 1
        if s["level"] in ("high", "medium"):
            pc[s["level"]] += 1
    top = [{"name": s["name"], "risk": s["risk"], "why": s["signals"][0]["label"] if s["signals"] else ""}
           for s in b["students"][:5]]
    text = (f"{b['total']} students: {b['counts'].get('high', 0)} high risk, {b['counts'].get('medium', 0)} medium. "
            f"{open_tasks} open coach tasks. Top risks: " + "; ".join(f"{t['name']} ({t['why']})" for t in top))
    return {"text": text, "per_coach": dict(per_coach), "top": top, "open_tasks": open_tasks}


def submit_checkin(tenant, student_id: int, data: dict) -> int:
    with tenant.db() as c:
        return c.execute("INSERT INTO checkins(student_id,at,outreach,calls_booked,calls_showed,mood,wins,blockers,demo) "
                         "VALUES(?,?,?,?,?,?,?,?,?)",
                         (student_id, db.now(), int(data.get("outreach") or 0), int(data.get("calls_booked") or 0),
                          int(data.get("calls_showed") or 0), int(data.get("mood") or 3), str(data.get("wins") or "")[:500],
                          str(data.get("blockers") or "")[:500], 1 if tenant.is_demo else 0)).lastrowid


def seed_bulk(tenant, n: int = 2000, weeks: int = 6) -> int:
    """DEMO students for the scale test (demo DB or a test DB only)."""
    import random

    rng = random.Random(7)
    now = time.time()
    with tenant.db() as c, db.tx(c):
        coaches = [r["id"] for r in c.execute("SELECT id FROM users WHERE role='coach'")] or [None]
        base = c.execute("SELECT COALESCE(MAX(id),0) FROM students").fetchone()[0]
        c.executemany("INSERT INTO students(id,name,email,coach_id,joined,niche,stage,clients,demo) VALUES(?,?,?,?,?,?,?,?,1)",
                      [(base + i + 1, f"Scale Student {i + 1} (DEMO)", f"scale{i + 1}@example.invalid", coaches[i % len(coaches)],
                        now - rng.randint(30, 200) * DAY, "physio clinics", "outreach", rng.choice([0, 0, 1, 2]))
                       for i in range(n)])
        rows = []
        for i in range(n):
            for w in range(weeks, 0, -1):
                if rng.random() < 0.1:
                    continue
                o = rng.randint(0, 120)
                bk = rng.randint(0, 6)
                rows.append((base + i + 1, now - w * 7 * DAY, o, bk, rng.randint(0, bk), rng.randint(1, 5), "", "", 1))
        c.executemany("INSERT INTO checkins(student_id,at,outreach,calls_booked,calls_showed,mood,wins,blockers,demo) "
                      "VALUES(?,?,?,?,?,?,?,?,?)", rows)
    return n
