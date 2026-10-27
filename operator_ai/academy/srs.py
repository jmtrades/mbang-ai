"""One spaced-repetition store (academy_review, item_kind quiz | drill) on call_review.sm2, automatic grading
from correctness and speed, Elo mastery per person x skill, adaptive item choice and the daily session (A10).

Grade mapping: wrong -> 1; right but slower than 2x the item's median time -> 3; right -> 4;
right, fast and with 2+ earlier right answers -> 5. A card with 4+ lapses is a leech: the coach is told and
the lesson is assigned for a re-read.

Mastery: p = 1 / (1 + e^-(theta - b)); theta += K (outcome - p); the item's b moves the other way; K decays
from 0.4 to 0.1 as answers build up. People see theta as "chance of getting a typical question right"."""
from __future__ import annotations

import math
import statistics
import time
from collections import defaultdict

from .. import call_review, db
from . import common, quiz

DAILY_REVIEWS = 20
LEECH_LAPSES = 4
TARGET_P = 0.7
DEFAULT_MS = {"cloze": 15000, "mc_numeric": 10000, "mc_term": 10000, "order": 20000, "scenario": 12000, "tf": 6000,
              "spot": 15000, "write": 90000}
FIRST_REVIEW_DAYS = 1.0


# --------------------------------------------------------------------------- Elo mastery
def k_factor(n: int) -> float:
    return 0.1 + 0.3 * math.exp(-n / 20.0)


def p_correct(theta: float, b: float) -> float:
    return common.sigmoid(theta - b)


def update_mastery(tenant, user_id: int, skill: str, item_id: int | None, outcome: float) -> dict:
    """outcome in [0, 1] (write-it uses score / 100)."""
    skill = skill or "general"
    with tenant.db() as c, db.tx(c):
        m = c.execute("SELECT * FROM academy_mastery WHERE user_id=? AND skill=?", (user_id, skill)).fetchone()
        theta, n = (m["theta"], m["n"]) if m else (0.0, 0)
        hist = db.jload(m["history"], []) if m else []
        b, exposures = 0.0, 0
        if item_id:
            it = c.execute("SELECT difficulty_b, exposures FROM academy_quiz_items WHERE id=?", (item_id,)).fetchone()
            if it:
                b, exposures = float(it["difficulty_b"] or 0.0), int(it["exposures"] or 0)
        p = p_correct(theta, b)
        theta += k_factor(n) * (outcome - p)
        if item_id:
            c.execute("UPDATE academy_quiz_items SET difficulty_b=?, exposures=? WHERE id=?",
                      (b - k_factor(exposures) * 0.5 * (outcome - p), exposures + 1, item_id))
        hist = (hist + [[round(time.time()), round(theta, 3)]])[-60:]
        if m:
            c.execute("UPDATE academy_mastery SET theta=?, n=?, history=?, updated=? WHERE id=?",
                      (theta, n + 1, db.jdump(hist), db.now(), m["id"]))
        else:
            c.execute("INSERT INTO academy_mastery(user_id,skill,theta,n,history,updated) VALUES(?,?,?,?,?,?)",
                      (user_id, skill, theta, 1, db.jdump(hist), db.now()))
    return {"skill": skill, "theta": round(theta, 3), "chance": round(common.sigmoid(theta), 3), "n": n + 1}


def mastery(tenant, user_id: int) -> dict[str, dict]:
    with tenant.db() as c:
        rows = c.execute("SELECT * FROM academy_mastery WHERE user_id=?", (user_id,)).fetchall()
    out = {}
    for r in rows:
        hist = db.jload(r["history"], [])
        week_ago = time.time() - 7 * common.DAY
        old = [h for h in hist if h[0] <= week_ago]
        prev = common.sigmoid(old[-1][1]) if old else None
        out[r["skill"]] = {"skill": r["skill"], "label": common.skill_label(r["skill"]), "theta": round(r["theta"], 3),
                           "chance": round(common.sigmoid(r["theta"]), 3), "n": r["n"],
                           "week_change": None if prev is None else round(common.sigmoid(r["theta"]) - prev, 3),
                           "spark": [round(common.sigmoid(h[1]), 3) for h in hist[-12:]]}
    return out


def path_mastery(tenant, user_id: int, skills: list[str]) -> dict:
    """Overall = mean chance across the path's skills that have material; skills never practised count 0.5."""
    m = mastery(tenant, user_id)
    have = skills_with_items(tenant)
    per = {s: (m[s]["chance"] if s in m else 0.5) for s in skills if s in have}
    overall = round(sum(per.values()) / len(per), 3) if per else None
    return {"overall": overall, "per_skill": per}


def skills_with_items(tenant) -> set[str]:
    with tenant.db() as c:
        return {r["skill"] for r in c.execute(
            "SELECT DISTINCT i.skill FROM academy_quiz_items i JOIN academy_lessons l ON l.id=i.lesson_id "
            "WHERE i.status='published' AND l.status='published'")}


# --------------------------------------------------------------------------- grading -> SM-2
def median_ms(tenant, item_id: int, kind: str) -> float:
    with tenant.db() as c:
        ms = [r["ms"] for r in c.execute("SELECT ms FROM academy_attempts WHERE quiz_item_id=? AND correct=1 AND ms>0 "
                                         "ORDER BY id DESC LIMIT 200", (item_id,))]
    return float(statistics.median(ms)) if len(ms) >= 5 else float(DEFAULT_MS.get(kind, 12000))


def auto_grade(correct: bool, ms: int | None, median: float, prior_correct: int) -> int:
    if not correct:
        return 1
    if ms and ms > 2 * median:
        return 3
    if ms and ms < median and prior_correct >= 2:
        return 5
    return 4


def add_cards(tenant, user_id: int, item_kind: str, item_ids: list[int], due: float | None = None) -> int:
    common.ensure_extras(tenant)
    due = due if due is not None else time.time() + FIRST_REVIEW_DAYS * common.DAY
    n = 0
    with tenant.db() as c:
        for iid in item_ids:
            cur = c.execute("INSERT OR IGNORE INTO academy_review(user_id,quiz_item_id,item_kind,item_id,due) VALUES(?,?,?,?,?)",
                            (user_id, iid if item_kind == "quiz" else None, item_kind, iid, due))
            n += cur.rowcount
    return n


def schedule(tenant, user_id: int, card_id: int, grade: int) -> dict:
    """SM-2 on one card. Raises KeyError when the card isn't this person's."""
    with tenant.db() as c, db.tx(c):
        card = c.execute("SELECT * FROM academy_review WHERE id=? AND user_id=?", (card_id, user_id)).fetchone()
        if card is None:
            raise KeyError(card_id)
        ease, interval, reps = call_review.sm2(card["ease"] or 2.5, card["interval"] or 0, card["reps"] or 0, grade)
        lapses = (card["lapses"] or 0) + (1 if grade < 3 and (card["reps"] or 0) > 0 else 0)
        due = time.time() + interval * common.DAY
        c.execute("UPDATE academy_review SET ease=?, interval=?, reps=?, lapses=?, due=?, last_grade=?, last_reviewed=? "
                  "WHERE id=?", (ease, interval, reps, lapses, due, grade, time.time(), card_id))
    out = {"card_id": card_id, "ease": round(ease, 2), "interval_days": interval, "reps": reps, "lapses": lapses,
           "due": due}
    if lapses >= LEECH_LAPSES and (card["lapses"] or 0) < LEECH_LAPSES:
        out["leech"] = _leech(tenant, user_id, dict(card))
    return out


def card_for(tenant, user_id: int, item_kind: str, item_id: int) -> dict | None:
    common.ensure_extras(tenant)
    with tenant.db() as c:
        r = c.execute("SELECT * FROM academy_review WHERE user_id=? AND item_kind=? AND item_id=?",
                      (user_id, item_kind, item_id)).fetchone()
    return dict(r) if r else None


def _leech(tenant, user_id: int, card: dict) -> dict:
    """4+ lapses: tell the person's coach and assign the lesson again."""
    lesson_id, title = None, "this question"
    if card.get("item_kind") == "quiz":
        it = quiz.load(tenant, card["item_id"])
        if it:
            lesson_id = it["lesson_id"]
            with tenant.db() as c:
                r = c.execute("SELECT title FROM academy_lessons WHERE id=?", (lesson_id,)).fetchone()
            title = r["title"] if r else title
    coach_id = _coach_of(tenant, user_id)
    with tenant.db() as c:
        if lesson_id:
            c.execute("INSERT INTO academy_assignments(user_id,kind,ref,note,due,assigned_by,status,created) "
                      "VALUES(?,?,?,?,?,?,?,?)", (user_id, "lesson", str(lesson_id), "Re-read: a question keeps slipping",
                                                  time.time() + 3 * common.DAY, None, "open", db.now()))
        if coach_id:
            u = common.user_row(tenant, user_id) or {}
            c.execute("INSERT INTO coach_tasks(coach_id,student_id,kind,detail,status,due,created,demo) VALUES(?,?,?,?,?,?,?,?)",
                      (coach_id, u.get("student_id"), "academy_leech",
                       f"{u.get('display') or 'A learner'} keeps missing a question from “{title}”. A short call on it may help.",
                       "open", time.time() + 2 * common.DAY, db.now(), common.demo_flag(tenant)))
    return {"lesson_id": lesson_id, "coach_told": bool(coach_id)}


def _coach_of(tenant, user_id: int) -> int | None:
    with tenant.db() as c:
        r = c.execute("SELECT u.coach_id, s.coach_id scoach FROM users u LEFT JOIN students s ON s.id=u.student_id "
                      "WHERE u.id=?", (user_id,)).fetchone()
        if r and (r["coach_id"] or r["scoach"]):
            return r["coach_id"] or r["scoach"]
        r = c.execute("SELECT manager_id FROM team_assignments WHERE member_id=? ORDER BY id LIMIT 1", (user_id,)).fetchone()
    return r["manager_id"] if r else None


# --------------------------------------------------------------------------- due cards and the daily session
def due(tenant, user_id: int, limit: int = DAILY_REVIEWS, now: float | None = None) -> list[dict]:
    """Due cards, interleaved across skills (round robin), quiz cards only for published practice items."""
    common.ensure_extras(tenant)
    now = now or time.time()
    with tenant.db() as c:
        quiz_cards = c.execute(
            "SELECT r.*, i.skill, i.kind FROM academy_review r JOIN academy_quiz_items i ON i.id=r.item_id "
            "JOIN academy_lessons l ON l.id=i.lesson_id WHERE r.user_id=? AND r.item_kind='quiz' AND r.due<=? "
            "AND i.status='published' AND i.pool='practice' AND l.status='published' ORDER BY r.due",
            (user_id, now)).fetchall()
        drill_cards = c.execute(
            "SELECT r.*, d.objection, d.answer, d.citation, d.skill FROM academy_review r JOIN drills d ON d.id=r.item_id "
            "WHERE r.user_id=? AND r.item_kind='drill' AND r.due<=? ORDER BY r.due", (user_id, now)).fetchall()
    by_skill: dict[str, list[dict]] = defaultdict(list)
    for r in quiz_cards:
        it = quiz.load(tenant, r["item_id"])
        if it is None:
            continue
        by_skill[r["skill"] or "general"].append({"card_id": r["id"], "item_kind": "quiz", "due": r["due"],
                                                  "reps": r["reps"], "item": quiz.public(it)})
    for r in drill_cards:
        by_skill[r["skill"] or "sales"].append({"card_id": r["id"], "item_kind": "drill", "due": r["due"], "reps": r["reps"],
                                                "drill": {"id": r["item_id"], "objection": r["objection"]}})
    out: list[dict] = []
    queues = [q for q in by_skill.values() if q]
    while queues and len(out) < limit:
        for q in list(queues):
            if q and len(out) < limit:
                out.append(q.pop(0))
            if not q:
                queues.remove(q)
    return out


def due_count(tenant, user_id: int) -> int:
    return len(due(tenant, user_id, limit=500))


def weakest_skill(tenant, user_id: int, skills: list[str]) -> str | None:
    m = mastery(tenant, user_id)
    have = skills_with_items(tenant)
    cands = [s for s in skills if s in have]
    if not cands:
        return None
    return min(cands, key=lambda s: (m[s]["chance"] if s in m else 0.5, s))


def adaptive_items(tenant, user_id: int, skill: str | None = None, n: int = 5, exclude: list[int] | None = None) -> list[dict]:
    """Practice items whose predicted chance is closest to 0.7, preferring ones this person has seen least."""
    m = mastery(tenant, user_id)
    q = ("SELECT i.* FROM academy_quiz_items i JOIN academy_lessons l ON l.id=i.lesson_id WHERE i.status='published' "
         "AND i.pool='practice' AND l.status='published'")
    args: list = []
    if skill:
        q += " AND i.skill=?"
        args.append(skill)
    with tenant.db() as c:
        rows = [dict(r) for r in c.execute(q, args)]
        seen = {r["quiz_item_id"]: r["n"] for r in c.execute(
            "SELECT quiz_item_id, COUNT(*) n FROM academy_attempts WHERE user_id=? GROUP BY quiz_item_id", (user_id,))}
    ex = set(exclude or [])
    scored = []
    for r in rows:
        if r["id"] in ex:
            continue
        theta = m.get(r["skill"], {}).get("theta", 0.0)
        p = p_correct(theta, float(r["difficulty_b"] or 0.0))
        scored.append((abs(p - TARGET_P) + 0.15 * seen.get(r["id"], 0), r["id"], r))
    scored.sort(key=lambda x: (x[0], x[1]))
    out = []
    for _, _, r in scored[:n]:
        it = quiz._row(r)
        pub = quiz.public(it)
        theta = m.get(r["skill"], {}).get("theta", 0.0)
        pub["predicted_chance"] = round(p_correct(theta, float(r["difficulty_b"] or 0.0)), 2)
        out.append(pub)
    return out


def session(tenant, user: dict, skills: list[str]) -> dict:
    """Today: up to 20 interleaved reviews, then one role-play on the weakest skill."""
    cards = due(tenant, user["id"], DAILY_REVIEWS)
    weak = weakest_skill(tenant, user["id"], skills)
    return {"reviews": cards, "review_count": len(cards), "roleplay": {"skill": weak, "label": common.skill_label(weak)}
            if weak else None}


# --------------------------------------------------------------------------- objection drill cards (merged store)
def drill_cards(tenant, user_id: int) -> list[dict]:
    """The Drills page: one card per drill in academy_review (item_kind 'drill'). Older drill_cards rows are
    copied in once so nobody loses their schedule. The drill bank itself is seeded at Academy build time or
    by call review, never here."""
    common.ensure_extras(tenant)
    now = time.time()
    with tenant.db() as c:
        drills = db.rows(c.execute("SELECT * FROM drills ORDER BY id"))
        have = {r["item_id"]: dict(r) for r in c.execute(
            "SELECT * FROM academy_review WHERE user_id=? AND item_kind='drill'", (user_id,))}
        old = {r["drill_id"]: dict(r) for r in c.execute("SELECT * FROM drill_cards WHERE user_id=?", (user_id,))}
        for d in drills:
            if d["id"] in have:
                continue
            o = old.get(d["id"])
            c.execute("INSERT OR IGNORE INTO academy_review(user_id,quiz_item_id,item_kind,item_id,ease,interval,reps,due,"
                      "last_grade) VALUES(?,?,?,?,?,?,?,?,?)",
                      (user_id, None, "drill", d["id"], (o or {}).get("ease", 2.5), (o or {}).get("interval", 0),
                       (o or {}).get("reps", 0), (o or {}).get("due") or now, (o or {}).get("last_grade")))
        cards = {r["item_id"]: dict(r) for r in c.execute(
            "SELECT * FROM academy_review WHERE user_id=? AND item_kind='drill'", (user_id,))}
    out = []
    for d in drills:
        card = cards.get(d["id"])
        if card is None:
            continue
        out.append({"card_id": card["id"], "drill_id": d["id"], "objection": d["objection"], "answer": d["answer"],
                    "citation": db.jload(d["citation"], None), "due": card["due"], "is_due": card["due"] <= now,
                    "interval_days": card["interval"], "reps": card["reps"]})
    return sorted(out, key=lambda x: x["due"])


def grade_drill(tenant, user_id: int, card_id: int, g: int) -> dict:
    with tenant.db() as c:
        r = c.execute("SELECT item_kind FROM academy_review WHERE id=? AND user_id=?", (card_id, user_id)).fetchone()
    if r is None or r["item_kind"] != "drill":
        raise KeyError(card_id)
    res = schedule(tenant, user_id, card_id, g)
    return {k: res[k] for k in ("card_id", "ease", "interval_days", "reps", "due")}
