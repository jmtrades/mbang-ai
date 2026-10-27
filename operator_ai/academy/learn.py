"""Learner side: today's plan, paths, the lesson reader, lesson completion, answering questions (auto-graded,
spaced repetition and mastery updated) and reporting a bad question."""
from __future__ import annotations

import time

from .. import config, db, guardrails, llm
from . import certify, common, quiz, srs, team


MASTERY_REPEAT_S = 20 * 3600


def _names(tenant) -> list[str]:
    return guardrails.tenant_banned_names(tenant)


def _scrub(tenant, s):
    return guardrails.scrub(s, _names(tenant)) if isinstance(s, str) else s


def built(tenant) -> dict:
    common.ensure_extras(tenant)
    with tenant.db() as c:
        b = c.execute("SELECT id, run_id, status, stats, created, finished FROM academy_builds ORDER BY id DESC LIMIT 1").fetchone()
        n = c.execute("SELECT COUNT(*) FROM academy_lessons WHERE status='published'").fetchone()[0]
        drafts = c.execute("SELECT COUNT(*) FROM academy_lessons WHERE status='draft'").fetchone()[0]
    return {"published": n, "drafts": drafts, "last_build": dict(b) if b else None}


def _lesson_row(r) -> dict:
    d = dict(r)
    for k in ("key_points", "citations", "example", "mistakes", "skills", "checks"):
        d[k] = db.jload(d.get(k), [] if k != "checks" else {})
    return d


def lesson_view(tenant, viewer: dict, lesson_id: int) -> dict:
    with tenant.db() as c:
        r = c.execute("SELECT * FROM academy_lessons WHERE id=?", (lesson_id,)).fetchone()
    if r is None:
        raise KeyError(lesson_id)
    L = _lesson_row(r)
    staff = viewer["role"] in ("owner", "coach")
    if L["status"] not in ("published", "stale") and not staff:
        raise KeyError(lesson_id)
    with tenant.db() as c:
        st = ("published",) if L["status"] in ("published", "stale") or not staff else ("published", "draft")
        items = [quiz._row(dict(x)) for x in c.execute(
            f"SELECT * FROM academy_quiz_items WHERE lesson_id=? AND pool='practice' AND status IN ({','.join('?' * len(st))}) "
            "ORDER BY id", (lesson_id, *st))]
        prog = c.execute("SELECT status FROM academy_progress WHERE user_id=? AND lesson_id=?", (viewer["id"], lesson_id)).fetchone()
    done = bool(prog and prog["status"] == "complete") or L["lesson_key"] in certify.done_keys(tenant, viewer["id"])
    state = common.material_state(tenant)
    names = _names(tenant)
    checks = [quiz.public(i) for i in items if i["kind"] != "write"][:4]
    writes = [quiz.public(i) for i in items if i["kind"] == "write"]
    return {
        "id": L["id"], "title": guardrails.scrub(L["title"], names), "status": L["status"], "skill": L["skill"],
        "skill_label": common.skill_label(L["skill"]), "skills": L["skills"], "est_minutes": L["est_minutes"],
        "gen_mode": L["gen_mode"], "gen_label": "Written by AI from the cited lines, then checked" if L["gen_mode"] in ("live", "ai")
        else "Replayed AI lesson, checked" if L["gen_mode"] == "replay" else "Built by rules from the course (AI not used)",
        "body": guardrails.scrub(L["body"] or "", names) if L["gen_mode"] in ("live", "ai", "replay") else "",
        "key_points": [{**k, "text": guardrails.scrub(k["text"], names)} for k in L["key_points"]],
        "example": [{**k, "text": guardrails.scrub(k["text"], names)} for k in L["example"]],
        "mistakes": [{**k, "text": guardrails.scrub(k["text"], names)} for k in L["mistakes"]],
        "citations": [{**c, "title": guardrails.scrub(c.get("title") or "", names),
                       "snippet": guardrails.scrub(c.get("snippet") or "", names)} for c in L["citations"]],
        "checks": checks, "write_tasks": writes, "done": done, "demo_material": bool(L.get("demo")) and state["demo_material"],
        "practise": {"coaching_kind": common.COACHING_KIND.get(L["skill"]), "roleplay_skill": L["skill"],
                     "questions": len(items)},
        "stale": L["status"] == "stale", "source": {"file": L["source_file"], "lines": L["source_lines"]},
        # [persona] an approved opener/closer around the lesson, only while the subject's signed consent covers
        # lesson_framing and this learner hasn't chosen a neutral voice; None = neutral. The cited lines are untouched.
        "framing": _framing(tenant, viewer, L["id"]),
    }


def _framing(tenant, viewer: dict, lesson_id: int) -> dict | None:
    try:
        from .. import persona

        return persona.framing(tenant, "lesson_framing", f"lesson-{lesson_id}", user=viewer)
    except Exception:  # noqa: BLE001 - framing is optional; the lesson stands on its own
        return None


def complete_lesson(tenant, user: dict, lesson_id: int) -> dict:
    with tenant.db() as c:
        r = c.execute("SELECT id, status FROM academy_lessons WHERE id=?", (lesson_id,)).fetchone()
    if r is None or r["status"] not in ("published", "stale"):
        raise KeyError(lesson_id)
    now = db.now()
    with tenant.db() as c:
        c.execute("INSERT INTO academy_progress(user_id,lesson_id,status,attempts,updated) VALUES(?,?,'complete',1,?) "
                  "ON CONFLICT(user_id,lesson_id) DO UPDATE SET status='complete', attempts=attempts+1, updated=excluded.updated",
                  (user["id"], lesson_id, now))
        ids = [x["id"] for x in c.execute("SELECT id FROM academy_quiz_items WHERE lesson_id=? AND status='published' AND "
                                          "pool='practice' AND kind!='write'", (lesson_id,))]
    cards = srs.add_cards(tenant, user["id"], "quiz", ids)
    awarded = certify.refresh(tenant, user)
    return {"lesson_id": lesson_id, "done": True, "new_review_cards": cards, "first_review_in_days": srs.FIRST_REVIEW_DAYS,
            "awarded": awarded}


def answer(tenant, user: dict, item_id: int, given, ms: int | None = None, context: str = "quiz",
           session_id: str | None = None) -> dict:
    it = quiz.load(tenant, item_id)
    if it is None or it["pool"] != "practice" or it["status"] != "published":
        raise KeyError(item_id)
    with tenant.db() as c:
        les = c.execute("SELECT status, lesson_key FROM academy_lessons WHERE id=?", (it["lesson_id"],)).fetchone()
    if les is None or les["status"] not in ("published", "stale"):
        raise KeyError(item_id)
    g = quiz.grade(tenant, it, given)   # ValueError -> 422
    with tenant.db() as c:
        prior = c.execute("SELECT COUNT(*) FROM academy_attempts WHERE user_id=? AND quiz_item_id=? AND correct=1",
                          (user["id"], item_id)).fetchone()[0]
        last = c.execute("SELECT MAX(created) FROM academy_attempts WHERE user_id=? AND quiz_item_id=? AND "
                         "COALESCE(context,'quiz')!='exam'", (user["id"], item_id)).fetchone()[0]
        c.execute("INSERT INTO academy_attempts(user_id,quiz_item_id,answer,correct,created,ms,context,session_id,chosen_option,score) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (user["id"], item_id, db.jdump(given)[:4000], 1 if g["correct"] else 0, db.now(), ms, context,
                   (session_id or "")[:80] or None, g["chosen_option"], g["score"]))
    outcome = g["score"] / 100.0 if it["kind"] == "write" else (1.0 if g["correct"] else 0.0)
    # Mastery moves once per question per day: the answer is shown after the first try, so answering the same
    # question again straight away measures nothing (and would let anyone pump their way to a certificate).
    if last is None or time.time() - float(last) >= MASTERY_REPEAT_S:
        mast = {**srs.update_mastery(tenant, user["id"], it["skill"], item_id, outcome), "counted": True}
    else:
        cur = srs.mastery(tenant, user["id"]).get(it["skill"] or "general")
        mast = {"skill": it["skill"], "theta": cur["theta"] if cur else 0.0, "chance": cur["chance"] if cur else 0.5,
                "n": cur["n"] if cur else 0, "counted": False,
                "note": "You already answered this one today, so it doesn't change your skill score until tomorrow."}
    review = None
    if it["kind"] != "write":
        card = srs.card_for(tenant, user["id"], "quiz", item_id)
        if card is None and les["lesson_key"] in certify.done_keys(tenant, user["id"]):
            srs.add_cards(tenant, user["id"], "quiz", [item_id])
            card = srs.card_for(tenant, user["id"], "quiz", item_id)
        if card is not None and (context == "review" or card["due"] <= time.time()):
            grade = srs.auto_grade(g["correct"], ms, srs.median_ms(tenant, item_id, it["kind"]), prior)
            review = srs.schedule(tenant, user["id"], card["id"], grade)
            review["grade"] = grade
    awarded = certify.refresh(tenant, user, skill=it["skill"])
    names = _names(tenant)
    reveal = None
    if it["kind"] not in ("write",):
        reveal = it["answer"]
    cite = it.get("citation")
    if cite:
        cite = {**cite, "snippet": guardrails.scrub(cite.get("snippet") or "", names),
                "title": guardrails.scrub(cite.get("title") or "", names)}
    return {"correct": g["correct"], "score": g["score"], "answer": reveal, "explanation": guardrails.scrub(it["explanation"] or "", names),
            "citation": cite, "feedback": g["feedback"], "mastery": mast, "review": review, "awarded": awarded,
            "mode": "rules"}


def report_item(tenant, user: dict, item_id: int, reason: str) -> dict:
    it = quiz.load(tenant, item_id)
    if it is None or it["status"] not in ("published", "draft"):
        raise KeyError(item_id)
    with tenant.db() as c:
        cur = c.execute("INSERT OR IGNORE INTO academy_reports(item_id,user_id,reason,created) VALUES(?,?,?,?)",
                        (item_id, user["id"], reason.strip()[:500], db.now()))
        if cur.rowcount:
            c.execute("UPDATE academy_quiz_items SET reported=COALESCE(reported,0)+1 WHERE id=?", (item_id,))
        n = c.execute("SELECT reported FROM academy_quiz_items WHERE id=?", (item_id,)).fetchone()["reported"]
    return {"id": item_id, "reported": n, "flagged": n >= 3}


# --------------------------------------------------------------------------- paths and today
def path_summary(tenant, user: dict, key: str) -> dict:
    path = common.path_def(tenant, key)
    lessons = certify.path_lessons(tenant, path)
    done = certify.done_keys(tenant, user["id"])
    pm = srs.path_mastery(tenant, user["id"], path["skills"])
    lv = None
    for L in path["levels"]:
        if certify.is_certified(tenant, user["id"], key, L):
            lv = L
    return {"key": key, "title": path["title"], "blurb": path["blurb"], "lessons": len(lessons),
            "done": sum(1 for L in lessons if L["lesson_key"] in done), "mastery": pm["overall"],
            "level": lv, "level_name": common.LEVEL_NAMES.get(lv) if lv else None,
            "minutes": sum(L["est_minutes"] or 0 for L in lessons)}


def paths(tenant, user: dict) -> list[dict]:
    return [path_summary(tenant, user, k) for k in common.user_paths(tenant, user)]


def path_detail(tenant, user: dict, key: str) -> dict:
    path = common.path_def(tenant, key)
    if path is None:
        raise KeyError(key)
    lessons = certify.path_lessons(tenant, path)
    done = certify.done_keys(tenant, user["id"])
    names = _names(tenant)
    m = srs.mastery(tenant, user["id"])
    have = srs.skills_with_items(tenant)
    skills = [{"key": s, "label": common.skill_label(s), "chance": m[s]["chance"] if s in m else None,
               "answered": m[s]["n"] if s in m else 0} for s in path["skills"] if s in have]
    return {**path_summary(tenant, user, key), "skills": skills,
            "lesson_list": [{"id": L["id"], "title": guardrails.scrub(L["title"], names), "skill": L["skill"],
                             "skill_label": common.skill_label(L["skill"]), "est_minutes": L["est_minutes"],
                             "done": L["lesson_key"] in done} for L in lessons],
            "ladder": certify.ladder(tenant, user, key)}


def today(tenant, user: dict) -> dict:
    common.ensure_extras(tenant)
    state = common.material_state(tenant)
    b = built(tenant)
    keys = common.user_paths(tenant, user)
    primary = common.ROLE_PATH.get(user["role"], "growth_operator" if user["role"] == "owner" else "compliance_core")
    nxt = None
    done = certify.done_keys(tenant, user["id"])
    names = _names(tenant)
    for k in (["compliance_core", primary] + [x for x in keys if x not in ("compliance_core", primary)]):
        if k not in keys:
            continue
        for L in certify.path_lessons(tenant, common.path_def(tenant, k)):
            if L["lesson_key"] not in done:
                nxt = {"id": L["id"], "title": guardrails.scrub(L["title"], names), "path": k,
                       "path_title": common.PATHS[k]["title"], "est_minutes": L["est_minutes"],
                       "skill_label": common.skill_label(L["skill"])}
                break
        if nxt:
            break
    skills = sorted({s for k in keys for s in common.PATHS[k]["skills"]})
    sess = srs.session(tenant, user, skills)
    progress = []
    for k in keys:
        lad = certify.ladder(tenant, user, k)
        nxt_level = next((lv for lv in lad["levels"] if lv["status"] != "active"), None)
        progress.append({"path": k, "title": lad["title"],
                         "held": [lv["level"] for lv in lad["levels"] if lv["status"] == "active"],
                         "next": None if nxt_level is None else {"level": nxt_level["level"], "name": nxt_level["name"],
                                                                 "missing": [x["label"] for x in nxt_level["missing"]][:4]}})
    ai = llm.state(tenant)
    return {
        "material": {"ok": state["ok"], "reason": state["reason"], "demo_material": state["demo_material"],
                     "fixture_on_server": config.get().using_fixture_corpus},
        "built": b, "next_lesson": nxt, "reviews_due": sess["review_count"], "session": sess,
        "suggested_roleplay": sess["roleplay"], "certificates": progress,
        "assignments": team.assignments_for(tenant, user["id"]),
        "exam_open": certify.exam_open(tenant, user["id"]),
        "ai_label": "AI optional: lessons and grading here run on rules" if not ai["enabled"] else
        "AI connected: lessons may carry a checked AI explainer",
        "require_cert_for_own_outreach": bool(tenant.get("require_cert_for_own_outreach", False)),
        "compliance_certified": certify.is_certified(tenant, user["id"], "compliance_core", "L1"),
    }
