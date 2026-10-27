"""Coach and manager views (A13): cohort table with at-risk flags, skill heatmap, misconceptions, the role-play
sign-off queue, assignments (which draft a coach_note for approval) and a training-impact panel that shows
correlations with their sample size, never promises. Coaches only ever see their own learners."""
from __future__ import annotations

import math
import time

from .. import audit, db, guardrails, queue
from . import certify, common, quiz, srs

INACTIVE_DAYS = 7


def _learners(tenant, viewer: dict) -> list[dict]:
    ids = common.learner_ids(tenant, viewer)
    with tenant.db() as c:
        if ids is None:
            rows = db.rows(c.execute("SELECT id, username, display, role, coach_id, student_id FROM users "
                                     "WHERE active=1 AND role!='owner' ORDER BY role, display"))
        elif not ids:
            rows = []
        else:
            rows = db.rows(c.execute(f"SELECT id, username, display, role, coach_id, student_id FROM users WHERE active=1 "
                                     f"AND id IN ({','.join('?' * len(ids))}) ORDER BY role, display", ids))
    return rows


def _last_active(tenant, uid: int) -> float | None:
    with tenant.db() as c:
        a = c.execute("SELECT MAX(created) m FROM academy_attempts WHERE user_id=?", (uid,)).fetchone()["m"]
        p = c.execute("SELECT MAX(updated) m FROM academy_progress WHERE user_id=?", (uid,)).fetchone()["m"]
    vals = [v for v in (a, p) if v]
    return max(vals) if vals else None


def _failed_roleplays(tenant, uid: int, days: int = 14) -> int:
    since = time.time() - days * common.DAY
    n = 0
    for r in certify._roleplays(tenant, uid, since):
        sc = r["score"]
        if float(sc.get("total") or 0) < 75 or int(sc.get("compliance_flags") or 0) > 0:
            n += 1
    return n


def _leeches(tenant, uid: int) -> int:
    with tenant.db() as c:
        return c.execute("SELECT COUNT(*) FROM academy_review WHERE user_id=? AND lapses>=?", (uid, srs.LEECH_LAPSES)).fetchone()[0]


def _level(tenant, uid: int) -> dict | None:
    best = None
    for r in certify._cert_rows(tenant, uid):
        if r["status"] != "active":
            continue
        rank = (common.LEVELS.index(r["level"]), r["path_key"] != "compliance_core")
        if best is None or rank > (common.LEVELS.index(best["level"]), best["path_key"] != "compliance_core"):
            best = r
    if best is None:
        return None
    return {"path": best["path_key"], "path_title": common.PATHS.get(best["path_key"], {}).get("title", best["path_key"]),
            "level": best["level"], "name": common.LEVEL_NAMES[best["level"]]}


def cohort(tenant, viewer: dict) -> dict:
    now = time.time()
    people = []
    skills_seen: set[str] = set()
    for u in _learners(tenant, viewer):
        m = srs.mastery(tenant, u["id"])
        skills_seen |= set(m)
        last = _last_active(tenant, u["id"])
        drop = min([v["week_change"] for v in m.values() if v["week_change"] is not None], default=0.0)
        failed = _failed_roleplays(tenant, u["id"])
        leech = _leeches(tenant, u["id"])
        risk = []
        if last is None or now - last > INACTIVE_DAYS * common.DAY:
            risk.append("No practice in 7 days")
        if drop is not None and drop < -0.1:
            risk.append("Mastery dropped this week")
        if failed >= 2:
            risk.append(f"{failed} role-plays below the bar")
        if leech:
            risk.append(f"{leech} question{'s' if leech != 1 else ''} keep slipping")
        overall = [v["chance"] for v in m.values()]
        people.append({"id": u["id"], "name": u["display"] or u["username"], "role": u["role"],
                       "level": _level(tenant, u["id"]), "last_active": last,
                       "mastery": round(sum(overall) / len(overall), 3) if overall else None,
                       "spark": _overall_spark(m), "skills": {k: v["chance"] for k, v in m.items()},
                       "at_risk": risk, "demo": "(DEMO)" in (u["display"] or "")})
    skills = [s for s in common.SKILLS if s in skills_seen]
    return {"people": people, "skills": [{"key": s, "label": common.skill_label(s)} for s in skills],
            "scope": "everyone" if viewer["role"] == "owner" else "your learners"}


def _overall_spark(m: dict) -> list[float]:
    sparks = [v["spark"] for v in m.values() if v["spark"]]
    if not sparks:
        return []
    n = min(len(s) for s in sparks)
    return [round(sum(s[-n + i] for s in sparks) / len(sparks), 3) for i in range(n)]


def misconceptions(tenant, viewer: dict, limit: int = 8) -> list[dict]:
    """Questions your learners miss most, with the most-chosen wrong answer."""
    ids = common.learner_ids(tenant, viewer)
    stats = quiz.item_stats(tenant)
    if viewer["role"] != "owner":
        # exam questions stay out of a coach's view: their answers and cited lines are the exam's answer key
        stats = [s for s in stats if s["pool"] != "exam"]
    if ids is not None:
        # recompute on this coach's learners only
        allowed = set(ids)
        with tenant.db() as c:
            rows = c.execute("SELECT user_id, quiz_item_id, correct, chosen_option FROM academy_attempts "
                             "WHERE quiz_item_id IS NOT NULL").fetchall()
        per: dict[int, list] = {}
        for r in rows:
            if r["user_id"] in allowed:
                per.setdefault(r["quiz_item_id"], []).append(r)
        out = []
        for s in stats:
            rs = per.get(s["id"], [])
            if len(rs) < 2:
                continue
            p = sum(1 for r in rs if r["correct"]) / len(rs)
            wrong = [r["chosen_option"] for r in rs if not r["correct"] and r["chosen_option"]]
            out.append({**s, "attempts": len(rs), "p_value": round(p, 3),
                        "top_wrong": max(set(wrong), key=wrong.count) if wrong else None})
        stats = out
    stats = [s for s in stats if s["attempts"] >= 2 and s["p_value"] is not None]
    stats.sort(key=lambda s: (s["p_value"], -s["attempts"]))
    out = []
    for s in stats[:limit]:
        it = quiz.load(tenant, s["id"])
        out.append({**s, "citation": it["citation"] if it else None,
                    "answer": it["answer"] if it and it["kind"] != "write" and it.get("quotable", 1) else None})
    return out


def signoff_queue(tenant, viewer: dict) -> list[dict]:
    ids = common.learner_ids(tenant, viewer)
    try:
        with tenant.db() as c:
            rows = db.rows(c.execute("SELECT r.id, r.user_id, r.scenario_key, r.score, r.transcript, r.updated, u.display "
                                     "FROM roleplays r JOIN users u ON u.id=r.user_id WHERE r.status='complete' "
                                     "ORDER BY r.updated DESC LIMIT 200"))
    except Exception:  # noqa: BLE001 - role-play tables may not exist yet
        return []
    out = []
    for r in rows:
        if r["user_id"] == viewer["id"] or (ids is not None and r["user_id"] not in ids):
            continue
        sc = db.jload(r["score"], {}) or {}
        if sc.get("signoff"):
            continue
        tr = db.jload(r["transcript"], []) or []
        # Blind grading: the rules score (and the per-criterion points that add up to it) is revealed only after
        # the grader gives their own score. Showing it first would make "agrees within 10 points" (the coach's
        # L3/L4 calibration evidence) a matter of copying a number.
        crit = [{"key": c.get("key"), "label": c.get("label")} for c in (sc.get("criteria") or []) if isinstance(c, dict)]
        out.append({"id": r["id"], "user_id": r["user_id"], "name": r["display"], "scenario": r["scenario_key"],
                    "compliance_flags": sc.get("compliance_flags", 0), "criteria": crit, "mode": sc.get("mode"),
                    "updated": r["updated"], "has_rules_score": sc.get("total") is not None,
                    "transcript": [{k: t.get(k) for k in ("speaker", "role", "text", "content") if k in t}
                                   for t in tr[-40:] if isinstance(t, dict)] if isinstance(tr, list) else str(tr)[:4000]})
    return out[:50]


def signoff_roleplay(tenant, roleplay_id: int, viewer: dict, score: float, note: str, actor: str) -> dict:
    """A coach or the owner signs off a completed role-play with their own score (used for L3 sign-off and
    for the grader's calibration)."""
    with tenant.db() as c:
        r = c.execute("SELECT id, user_id, score, status FROM roleplays WHERE id=?", (roleplay_id,)).fetchone()
    if r is None or r["user_id"] == viewer["id"] or not common.can_see(tenant, viewer, r["user_id"]):
        raise KeyError(roleplay_id)
    if r["status"] != "complete":
        raise common.Refused("Only finished role-plays can be signed off.")
    sc = db.jload(r["score"], {}) or {}
    if sc.get("signoff"):
        raise common.Refused("This role-play is already signed off.")
    sc["signoff"] = {"by": viewer.get("display") or viewer["username"], "at": time.time(), "user_id": viewer["id"],
                     "score": round(float(score), 1), "note": note.strip()[:500]}
    with tenant.db() as c:
        c.execute("UPDATE roleplays SET score=? WHERE id=?", (db.jdump(sc), roleplay_id))
    audit.log(tenant, actor, "settings", {"academy_roleplay_signoff": roleplay_id})
    learner = common.user_row(tenant, r["user_id"])
    awarded = certify.refresh(tenant, learner, actor) if learner else []
    certify.refresh(tenant, viewer, actor)
    total = sc.get("total")
    return {"id": roleplay_id, "signoff": sc["signoff"], "rules_score": total,
            "agreement": None if total is None else abs(float(total) - float(score)) <= 10, "awarded": awarded}


def assign(tenant, viewer: dict, user_id: int, kind: str, ref: str, due_days: int | None, note: str, actor: str) -> dict:
    """Assign a lesson, a role-play scenario or a whole path; drafts a coach_note to the learner for approval."""
    learner = common.user_row(tenant, user_id)
    if learner is None or not learner["active"] or user_id == viewer["id"] or not common.can_see(tenant, viewer, user_id):
        raise KeyError(user_id)
    free_text = " ".join(x for x in (note.strip(), ref.strip() if kind == "scenario" else "") if x)
    if free_text:
        # the note shows in the learner's Today tab straight away, so it passes the guardrails here, not only
        # in the approval queue (a held coach_note must not reach the learner another way)
        flags = guardrails.check_for(tenant, free_text, {})
        if guardrails.blocked(flags):
            raise common.Refused("The note breaks a rule: " + "; ".join(f["message"] for f in flags if f["severity"] == "block"),
                                 422)
    due = time.time() + due_days * common.DAY if due_days else None
    label = ref
    if kind == "lesson":
        with tenant.db() as c:
            r = c.execute("SELECT id, title FROM academy_lessons WHERE id=? AND status='published'", (int(ref),)).fetchone()
        if r is None:
            raise common.Refused("Pick a published lesson.", 422)
        label = f"the lesson “{r['title']}”"
    elif kind == "path":
        if ref not in common.PATHS:
            raise common.Refused("Unknown path.", 422)
        label = f"the {common.PATHS[ref]['title']} path"
        with tenant.db() as c:
            c.execute("INSERT INTO academy_enrolments(user_id,path_key,assigned_by,due,status,created) VALUES(?,?,?,?,?,?) "
                      "ON CONFLICT(user_id,path_key) DO UPDATE SET due=excluded.due, status='active', assigned_by=excluded.assigned_by",
                      (user_id, ref, viewer["id"], due, "active", db.now()))
    elif kind == "scenario":
        label = f"the role-play “{ref[:80]}”"
    else:
        raise common.Refused("Assign a lesson, a role-play or a path.", 422)
    when = f" by {time.strftime('%d %b', time.localtime(due))}" if due else ""
    body = f"Hi {(learner['display'] or learner['username']).replace(' (DEMO)', '')}, I've assigned you {label}{when} in the Academy."
    if note.strip():
        body += " " + note.strip()[:400]
    item = queue.add(tenant, "coach_note", body, channel="manual", recipient_ref=learner["display"] or learner["username"],
                     title=f"Academy assignment for {learner['display'] or learner['username']}",
                     meta={"academy_assignment": True, "learner_id": user_id}, actor=actor, demo=tenant.is_demo)
    with tenant.db() as c:
        aid = c.execute("INSERT INTO academy_assignments(user_id,kind,ref,note,due,assigned_by,status,queue_id,created) "
                        "VALUES(?,?,?,?,?,?,?,?,?)", (user_id, kind, str(ref), note.strip()[:400], due, viewer["id"], "open",
                                                      item["id"], db.now())).lastrowid
    audit.log(tenant, actor, "settings", {"academy_assign": aid, "user": user_id, "kind": kind})
    return {"id": aid, "queue_id": item["id"], "queue_status": item["status"], "flags": item["flags"]}


def assignments_for(tenant, user_id: int) -> list[dict]:
    with tenant.db() as c:
        rows = db.rows(c.execute("SELECT a.*, u.display assigned_by_name FROM academy_assignments a LEFT JOIN users u "
                                 "ON u.id=a.assigned_by WHERE a.user_id=? AND a.status='open' ORDER BY a.id DESC LIMIT 50",
                                 (user_id,)))
    return rows


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


MIN_SENT_FOR_RATE = 5


def _outreach_counts(tenant) -> tuple[dict[int, int], dict[int, int]]:
    """Real sends (item_sent) and prospect or lead replies (reply_received) per owner_user_id, from the event log."""
    sent: dict[int, int] = {}
    replies: dict[int, int] = {}
    with tenant.db() as c:
        for r in c.execute("SELECT name, payload FROM app_events WHERE name IN ('item_sent','reply_received')"):
            p = db.jload(r["payload"], {}) or {}
            uid = p.get("owner_user_id") if isinstance(p, dict) else None
            if not isinstance(uid, int):
                continue
            if r["name"] == "item_sent":
                sent[uid] = sent.get(uid, 0) + 1
            elif p.get("party_type") in ("prospect", "lead"):
                replies[uid] = replies.get(uid, 0) + 1
    return sent, replies


def impact(tenant, viewer: dict) -> dict:
    """Mastery next to outcomes already in the database. A correlation with its n; never a promise."""
    rows = []
    sent, replies = _outreach_counts(tenant)
    for u in _learners(tenant, viewer):
        m = srs.mastery(tenant, u["id"])
        if not m:
            continue
        mast = sum(v["chance"] for v in m.values()) / len(m)
        out = {"id": u["id"], "mastery": mast}
        if sent.get(u["id"], 0) >= MIN_SENT_FOR_RATE:
            out["reply_rate"] = min(1.0, replies.get(u["id"], 0) / sent[u["id"]])
        with tenant.db() as c:
            if u.get("student_id"):
                r = c.execute("SELECT SUM(calls_booked) b, SUM(calls_showed) s FROM checkins WHERE student_id=?",
                              (u["student_id"],)).fetchone()
                if r and r["b"]:
                    out["show_rate"] = (r["s"] or 0) / r["b"]
            names = (u["username"], u["display"] or u["username"])  # never match calls with an empty rep
            revs = [float((db.jload(x["review"], {}) or {}).get("overall") or 0) for x in c.execute(
                "SELECT review FROM calls WHERE status='reviewed' AND rep IN (?,?)", names)]
            if revs:
                out["call_review"] = sum(revs) / len(revs)
        rows.append(out)
    metrics = []
    for key, label in (("reply_rate", "Reply rate on their own outreach"), ("show_rate", "Show rate on booked calls"),
                       ("call_review", "Call review score")):
        pts = [(r["mastery"], r[key]) for r in rows if key in r]
        r = _pearson([p[0] for p in pts], [p[1] for p in pts])
        metrics.append({"key": key, "label": label, "n": len(pts), "r": None if r is None else round(r, 2),
                        "note": "Not enough data yet" if len(pts) < 5 or r is None else
                        "Correlation only: it does not show that training caused the result."})
    return {"metrics": metrics, "learners": len(rows)}


def learner_detail(tenant, viewer: dict, user_id: int) -> dict:
    u = common.user_row(tenant, user_id)
    if u is None or not common.can_see(tenant, viewer, user_id):
        raise KeyError(user_id)
    ladders = [certify.ladder(tenant, u, pk) for pk in common.user_paths(tenant, u)]
    with tenant.db() as c:
        recent = db.rows(c.execute("SELECT a.created, a.correct, a.context, i.kind, i.prompt, i.skill FROM academy_attempts a "
                                   "LEFT JOIN academy_quiz_items i ON i.id=a.quiz_item_id WHERE a.user_id=? "
                                   "ORDER BY a.id DESC LIMIT 20", (user_id,)))
    for r in recent:
        if r.get("prompt"):
            r["prompt"] = guardrails.scrub(r["prompt"], guardrails.tenant_banned_names(tenant))
    return {"user": {"id": u["id"], "name": u["display"] or u["username"], "role": u["role"]},
            "mastery": list(srs.mastery(tenant, user_id).values()), "ladders": ladders, "recent": recent,
            "assignments": assignments_for(tenant, user_id)}


# --------------------------------------------------------------------------- managers (team_assignments)
MANAGER_ROLES = ("coach",)
MEMBER_ROLES = ("setter", "closer", "student", "coach")


def managers(tenant) -> dict:
    """Who manages whom: setters and closers have no students.coach_id, so the owner links them to a coach
    here (team_assignments). The coach then sees their training in the Team tab, and only theirs."""
    with tenant.db() as c:
        links = db.rows(c.execute(
            "SELECT t.manager_id, t.member_id, t.created, m.display manager_name, u.display member_name, u.role member_role "
            "FROM team_assignments t JOIN users m ON m.id=t.manager_id JOIN users u ON u.id=t.member_id "
            "WHERE m.active=1 AND u.active=1 ORDER BY m.display, u.display"))
        people = db.rows(c.execute("SELECT id, display, username, role FROM users WHERE active=1 AND role!='owner' "
                                   "ORDER BY role, display"))
    return {"links": links,
            "managers": [{"id": p["id"], "name": p["display"] or p["username"]} for p in people if p["role"] in MANAGER_ROLES],
            "members": [{"id": p["id"], "name": p["display"] or p["username"], "role": p["role"]} for p in people
                        if p["role"] in MEMBER_ROLES]}


def link_manager(tenant, manager_id: int, member_id: int, actor: str) -> dict:
    mgr, mem = common.user_row(tenant, manager_id), common.user_row(tenant, member_id)
    if mgr is None or mem is None or not mgr["active"] or not mem["active"]:
        raise KeyError(member_id)
    if manager_id == member_id:
        raise common.Refused("Someone can't manage themselves.", 422)
    if mgr["role"] not in MANAGER_ROLES:
        raise common.Refused("Only a coach can manage people's training.", 422)
    if mem["role"] not in MEMBER_ROLES:
        raise common.Refused("The owner already sees everyone; pick a coach, setter, closer or student.", 422)
    with tenant.db() as c:
        c.execute("INSERT OR IGNORE INTO team_assignments(manager_id,member_id,created) VALUES(?,?,?)",
                  (manager_id, member_id, db.now()))
    audit.log(tenant, actor, "settings", {"academy_manager": manager_id, "member": member_id, "linked": True})
    return {"manager_id": manager_id, "member_id": member_id, "linked": True}


def unlink_manager(tenant, manager_id: int, member_id: int, actor: str) -> dict:
    with tenant.db() as c:
        n = c.execute("DELETE FROM team_assignments WHERE manager_id=? AND member_id=?", (manager_id, member_id)).rowcount
    if not n:
        raise KeyError(member_id)
    audit.log(tenant, actor, "settings", {"academy_manager": manager_id, "member": member_id, "linked": False})
    return {"manager_id": manager_id, "member_id": member_id, "linked": False}
