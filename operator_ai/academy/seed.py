"""Demo seeding: build the Academy from the demo material, publish it (lessons and exam questions), and give
two fictional demo people some progress. Runs only against the demo database."""
from __future__ import annotations

import random
import time

from .. import db, tenancy
from . import build, certify, common, quiz, srs


def _user(tenant, username: str) -> dict | None:
    with tenant.db() as c:
        r = c.execute("SELECT id, username, display, role, coach_id, student_id FROM users WHERE username=?", (username,)).fetchone()
    return dict(r) if r else None


def _practice(tenant, user: dict, skills: list[str], rng: random.Random, accuracy: float, rounds: int) -> None:
    """Answer published practice questions in these skills (no write-it), updating attempts and mastery."""
    with tenant.db() as c:
        items = [quiz._row(dict(r)) for r in c.execute(
            "SELECT i.* FROM academy_quiz_items i JOIN academy_lessons l ON l.id=i.lesson_id WHERE i.status='published' "
            "AND i.pool='practice' AND l.status='published' AND i.kind!='write' AND i.skill IN (%s) ORDER BY i.id" %
            ",".join("?" * len(skills)), skills)]
    now = time.time()
    for r in range(rounds):
        for it in items:
            ok = rng.random() < accuracy
            with tenant.db() as c:
                c.execute("INSERT INTO academy_attempts(user_id,quiz_item_id,answer,correct,created,ms,context,chosen_option,score) "
                          "VALUES(?,?,?,?,?,?,?,?,?)", (user["id"], it["id"], db.jdump(it["answer"] if ok else "(DEMO) guess"),
                                                        1 if ok else 0, now - (rounds - r) * 2 * common.DAY + rng.randint(0, 3600),
                                                        rng.randint(4000, 20000), "quiz", None if ok else "(DEMO) guess",
                                                        100.0 if ok else 0.0))
            srs.update_mastery(tenant, user["id"], it["skill"], it["id"], 1.0 if ok else 0.0)


def _complete(tenant, user: dict, lessons: list[dict], due_now: int, rng: random.Random) -> None:
    now = time.time()
    for L in lessons:
        with tenant.db() as c:
            c.execute("INSERT OR IGNORE INTO academy_progress(user_id,lesson_id,status,attempts,updated) VALUES(?,?,'complete',1,?)",
                      (user["id"], L["id"], now - rng.randint(1, 6) * common.DAY))
            ids = [r["id"] for r in c.execute("SELECT id FROM academy_quiz_items WHERE lesson_id=? AND status='published' "
                                              "AND pool='practice' AND kind!='write'", (L["id"],))]
        srs.add_cards(tenant, user["id"], "quiz", ids)
    # some reviews are due today so the daily session has something in it
    with tenant.db() as c:
        cards = [r["id"] for r in c.execute("SELECT id FROM academy_review WHERE user_id=? AND item_kind='quiz' ORDER BY id",
                                            (user["id"],))]
        for cid in cards[:due_now]:
            c.execute("UPDATE academy_review SET due=?, reps=1, interval=1 WHERE id=?", (now - 3600, cid))


def seed_demo(tenant) -> dict:
    if not tenant.is_demo or not tenancy.is_demo_db(tenant):
        return {"skipped": "not the demo database"}
    state = common.material_state(tenant)
    if not state["ok"]:
        return {"skipped": state["reason"]}
    res = build.start(tenant, "system", sync=True)
    build.publish_all(tenant, "system", include_exam=True)
    rng = random.Random(7)
    setter = _user(tenant, "demo-setter")
    student = _user(tenant, "demo-student")
    coach = _user(tenant, "demo-coach")
    out = {"build": res["result"]}
    if setter:
        path = common.path_def(tenant, "setter")
        comp = common.path_def(tenant, "compliance_core")
        lessons = certify.path_lessons(tenant, comp) + certify.path_lessons(tenant, path)
        _complete(tenant, setter, lessons, 6, rng)
        _practice(tenant, setter, path["skills"], rng, 0.95, 4)
        out["setter_awarded"] = [f"{a['path']}/{a['level']}" for a in certify.refresh(tenant, setter, "system")]
    if student:
        path = common.path_def(tenant, "growth_operator")
        lessons = certify.path_lessons(tenant, path)
        _complete(tenant, student, lessons[:4], 8, rng)
        _practice(tenant, student, ["offer", "dms", "compliance"], rng, 0.6, 1)
        certify.refresh(tenant, student, "system")
    if coach and setter:
        with tenant.db() as c:
            c.execute("INSERT OR IGNORE INTO team_assignments(manager_id,member_id,created) VALUES(?,?,?)",
                      (coach["id"], setter["id"], db.now()))
    return out
