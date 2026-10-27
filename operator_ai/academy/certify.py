"""Certification L1-L4 per path (A9), timed exams, expiry and recertification, the is_certified() helper and
the optional approval gate for a person's own outreach.

L1 Foundations  every published lesson in the path done; mastery >= 0.8 overall and >= 0.7 per skill;
                Compliance core passed (for every other path).
L2 Practitioner 3 role-plays on distinct scenarios scoring 75+ with zero compliance flags; 5 write-it answers 80+.
L3 Certified    a timed 30-question exam from the approved exam pool at 85%+ (answers never sent to the browser,
                24h retake cooldown, the coach pauses while it is open); one role-play signed off by a coach or
                the owner; real work read from the system, never self-reported: setter 50+ approved own DMs with
                at most 5% guardrail holds; closer 10+ consented, reviewed calls averaging 70+; growth operator a
                first client signed; coach 10 graded learner role-plays within +-10 of the rules score.
L4 Lead         calibration: 20+ graded role-plays with 80%+ agreement (within +-10).

Certificates expire after 180 days. 3+ guardrail blocks on a person's own items within 30 days expire their
Compliance core certificate; evidence for a renewal only counts after the old certificate ended."""
from __future__ import annotations

import random
import time

from .. import audit, auth, db, events, guardrails, queue, textnorm
from . import common, quiz, srs

DM_KINDS = ("dm", "dm_opener", "prospect_dm", "followup", "booking_dm", "setter_reply")
SENT_STATUSES = ("approved", "scheduled", "awaiting_manual_send", "sent", "delivered")
PROSPECT_OWN_KINDS = auth.OWN_DECIDE - {"client_report"}


# --------------------------------------------------------------------------- reading certificates
def _cert_rows(tenant, user_id: int) -> list[dict]:
    common.ensure_extras(tenant)
    with tenant.db() as c:
        rows = db.rows(c.execute("SELECT * FROM certifications WHERE user_id=? ORDER BY path_key, level", (user_id,)))
    for r in rows:
        r["evidence"] = db.jload(r["evidence"], {})
        r["criteria_snapshot"] = db.jload(r.get("criteria_snapshot"), {})
        r["status"] = r.get("status") or (r["evidence"] or {}).get("status") or "active"
        r["expires"] = r.get("expires") or (r["evidence"] or {}).get("expires") or (r["awarded"] or 0) + common.CERT_DAYS * common.DAY
        if r["status"] == "active" and r["expires"] < time.time():
            r["status"] = "expired"
    return rows


def is_certified(tenant, user_id: int, path_key: str, level: str) -> bool:
    """True when the person holds an active, unexpired certificate for this path at this level or higher."""
    if level not in common.LEVELS:
        return False
    want = common.LEVELS.index(level)
    now = time.time()
    try:
        common.ensure_extras(tenant)
        with tenant.db() as c:
            rows = c.execute("SELECT level, status, expires, awarded, evidence FROM certifications WHERE user_id=? AND path_key=?",
                             (user_id, path_key)).fetchall()
    except Exception:  # noqa: BLE001 - a missing table means nobody is certified
        return False
    for r in rows:
        if r["level"] not in common.LEVELS or common.LEVELS.index(r["level"]) < want:
            continue
        ev = db.jload(r["evidence"], {}) or {}
        status = r["status"] or ev.get("status") or "active"
        expires = r["expires"] or ev.get("expires") or (r["awarded"] or 0) + common.CERT_DAYS * common.DAY
        if status == "active" and expires > now:
            return True
    return False


def _ended_at(tenant, user_id: int, path_key: str, level: str) -> float:
    """When an earlier certificate for this path and level ended (expired or revoked): renewal evidence must be
    newer than this. 0 when there was none."""
    for r in _cert_rows(tenant, user_id):
        if r["path_key"] == path_key and r["level"] == level and r["status"] in ("expired", "revoked"):
            return float((r["evidence"] or {}).get("ended_at") or r["expires"] or 0)
    return 0.0


# --------------------------------------------------------------------------- recertification trigger
def _own_item_ids(tenant, user_id: int, kinds: tuple | set | None = None) -> set[int]:
    with tenant.db() as c:
        rows = c.execute("SELECT id, kind FROM queue WHERE json_extract(meta,'$.owner_user_id')=?", (user_id,)).fetchall()
    return {r["id"] for r in rows if kinds is None or r["kind"] in kinds}


def recent_blocks(tenant, user_id: int, days: int = common.RECERT_WINDOW_DAYS) -> int:
    own = _own_item_ids(tenant, user_id)
    if not own:
        return 0
    since = time.time() - days * common.DAY
    n = 0
    with tenant.db() as c:
        for r in c.execute("SELECT detail FROM audit WHERE kind='guardrail.block' AND at>?", (since,)):
            d = db.jload(r["detail"], {}) or {}
            if isinstance(d, dict) and d.get("id") in own:
                n += 1
    return n


def recert_check(tenant, user_id: int, actor: str = "system") -> bool:
    """3+ guardrail blocks on this person's own items in 30 days end their Compliance core certificate."""
    if not is_certified(tenant, user_id, "compliance_core", "L1"):
        return False
    n = recent_blocks(tenant, user_id)
    if n < common.RECERT_BLOCKS:
        return False
    _end(tenant, user_id, "compliance_core", "L1", "expired",
         f"Recertification needed: {n} guardrail blocks on your own drafts in {common.RECERT_WINDOW_DAYS} days", actor)
    return True


def _end(tenant, user_id: int, path_key: str, level: str, status: str, reason: str, actor: str) -> None:
    now = time.time()
    with tenant.db() as c:
        r = c.execute("SELECT id, evidence FROM certifications WHERE user_id=? AND path_key=? AND level=?",
                      (user_id, path_key, level)).fetchone()
        if r is None:
            return
        ev = db.jload(r["evidence"], {}) or {}
        ev.update(status=status, ended_at=now, reason=reason)
        c.execute("UPDATE certifications SET status=?, revoked_reason=?, evidence=? WHERE id=?",
                  (status, reason, db.jdump(ev), r["id"]))
    audit.log(tenant, actor, "settings", {"academy_certificate": f"{path_key}/{level}", "status": status, "user": user_id})


def expire_due(tenant) -> int:
    """Mark certificates past their 180 days as expired (also computed on read)."""
    now = time.time()
    n = 0
    with tenant.db() as c:
        rows = c.execute("SELECT id, evidence FROM certifications WHERE status='active' AND expires IS NOT NULL AND expires<?",
                         (now,)).fetchall()
        for r in rows:
            ev = db.jload(r["evidence"], {}) or {}
            ev.update(status="expired", ended_at=now, reason="Expired after 180 days")
            c.execute("UPDATE certifications SET status='expired', evidence=? WHERE id=?", (db.jdump(ev), r["id"]))
            n += 1
    return n


# --------------------------------------------------------------------------- evidence
def path_lessons(tenant, path: dict) -> list[dict]:
    with tenant.db() as c:
        rows = db.rows(c.execute("SELECT id, title, skill, skills, lesson_key, ord, est_minutes FROM academy_lessons "
                                 "WHERE status='published' ORDER BY ord, id"))
    out = []
    for r in rows:
        skills = db.jload(r["skills"], []) or []
        if r["skill"] in path["skills"] or (path["key"] == "compliance_core" and "compliance" in skills):
            out.append(r)
    order = {s: i for i, s in enumerate(path["skills"])}
    out.sort(key=lambda r: (order.get(r["skill"], 99), r["ord"] or 0, r["id"]))
    return out


def done_keys(tenant, user_id: int) -> set[str]:
    """Lesson keys this person completed (any version of a lesson counts)."""
    with tenant.db() as c:
        return {r["lesson_key"] for r in c.execute(
            "SELECT l.lesson_key FROM academy_progress p JOIN academy_lessons l ON l.id=p.lesson_id "
            "WHERE p.user_id=? AND p.status='complete'", (user_id,))}


def _roleplays(tenant, user_id: int, since: float = 0.0) -> list[dict]:
    try:
        with tenant.db() as c:
            rows = db.rows(c.execute("SELECT id, scenario_key, score, status, updated, created FROM roleplays "
                                     "WHERE user_id=? AND status='complete'", (user_id,)))
    except Exception:  # noqa: BLE001
        return []
    out = []
    for r in rows:
        if (r["updated"] or r["created"] or 0) < since:
            continue
        r["score"] = db.jload(r["score"], {}) or {}
        out.append(r)
    return out


def _graded_by(tenant, grader: dict) -> list[dict]:
    """Role-plays this person signed off with their own score: [{total, given, within}]."""
    try:
        with tenant.db() as c:
            rows = c.execute("SELECT user_id, score FROM roleplays WHERE status='complete' AND score LIKE '%signoff%'").fetchall()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for r in rows:
        sc = db.jload(r["score"], {}) or {}
        so = sc.get("signoff") or {}
        if not isinstance(so, dict) or so.get("user_id") != grader["id"] or r["user_id"] == grader["id"]:
            continue
        if so.get("score") is None or sc.get("total") is None:
            continue
        out.append({"total": float(sc["total"]), "given": float(so["score"])})
    return out


SAME_ANSWER_JACCARD = 0.6


def distinct_answers(texts: list) -> int:
    """How many genuinely different write-it answers there are: an answer whose words overlap an already
    counted one by 60% or more (Jaccard) is the same answer again, so a case change, an emoji or one swapped
    word never turns one good draft into five."""
    kept: list[set[str]] = []
    for t in texts:
        toks = set(textnorm.tokens(t if isinstance(t, str) else str(t or ""), keep_stop=True))
        if not toks:
            continue
        if any(len(toks & k) / len(toks | k) >= SAME_ANSWER_JACCARD for k in kept):
            continue
        kept.append(toks)
    return len(kept)


def _crit(key: str, label: str, met: bool, have, need, detail: str = "", link: str | None = None) -> dict:
    if need == "yes":
        progress = "done" if have == "yes" else "not yet"
    elif isinstance(have, (int, float)) and isinstance(need, (int, float)) and not isinstance(have, bool):
        progress = f"{have} of {need}"
    else:
        progress = f"{'none yet' if have in (None, '') else have} (needs {need})"
    return {"key": key, "label": label, "met": bool(met), "have": have, "need": need, "progress": progress,
            "detail": detail, "link": link}


def criteria(tenant, user: dict, path_key: str, level: str) -> list[dict]:
    """The exact criteria for one level, each with what the person has and what is needed."""
    path = common.path_def(tenant, path_key)
    if path is None or level not in path["levels"]:
        return []
    th = path["thresholds"][level]
    uid = user["id"]
    since = _ended_at(tenant, uid, path_key, level)
    out: list[dict] = []
    if level != "L1":
        prev = common.LEVELS[common.LEVELS.index(level) - 1]
        out.append(_crit("previous", f"Hold {prev} ({common.LEVEL_NAMES[prev]})", is_certified(tenant, uid, path_key, prev),
                         "yes" if is_certified(tenant, uid, path_key, prev) else "no", "yes"))
    if level == "L1":
        lessons = path_lessons(tenant, path)
        done = done_keys(tenant, uid)
        n_done = sum(1 for L in lessons if L["lesson_key"] in done)
        out.append(_crit("lessons", "Finish every lesson in the path", lessons and n_done == len(lessons),
                         n_done, len(lessons), "" if lessons else "No published lessons for this path yet.", "paths"))
        pm = srs.path_mastery(tenant, uid, path["skills"])
        overall = pm["overall"]
        out.append(_crit("mastery_overall", f"Overall chance of a right answer at least {int(th['mastery_overall'] * 100)}%",
                         overall is not None and overall >= th["mastery_overall"],
                         None if overall is None else f"{round(overall * 100)}%", f"{int(th['mastery_overall'] * 100)}%",
                         "" if overall is not None else "No questions published for this path yet.", "practice"))
        weak = [s for s, v in pm["per_skill"].items() if v < th["mastery_skill"]]
        out.append(_crit("mastery_skill", f"Every skill at least {int(th['mastery_skill'] * 100)}%",
                         bool(pm["per_skill"]) and not weak, len(pm["per_skill"]) - len(weak), len(pm["per_skill"]),
                         ("Below the bar: " + ", ".join(common.skill_label(s) for s in weak)) if weak else "", "practice"))
        if path_key != "compliance_core":
            ok = is_certified(tenant, uid, "compliance_core", "L1")
            out.append(_crit("compliance_core", "Compliance core passed", ok, "yes" if ok else "no", "yes", "", "certificates"))
        if since:
            with tenant.db() as c:
                n = c.execute("SELECT COUNT(*) FROM academy_attempts a JOIN academy_quiz_items i ON i.id=a.quiz_item_id "
                              "WHERE a.user_id=? AND a.correct=1 AND a.created>? AND i.skill IN (%s)" %
                              ",".join("?" * len(path["skills"])), (uid, since, *path["skills"])).fetchone()[0]
            out.append(_crit("fresh_answers", "10 right answers since your last certificate ended", n >= 10, n, 10, "", "practice"))
    elif level == "L2":
        rps = [r for r in _roleplays(tenant, uid, since)
               if float(r["score"].get("total") or 0) >= th["roleplay_score"] and int(r["score"].get("compliance_flags") or 0) == 0]
        distinct = len({r["scenario_key"] for r in rps})
        out.append(_crit("roleplays", f"{th['roleplays']} role-plays on different scenarios scoring {th['roleplay_score']}+ "
                                      "with no compliance flags", distinct >= th["roleplays"], distinct, th["roleplays"], "", "roleplay"))
        with tenant.db() as c:
            rows = c.execute("SELECT a.answer FROM academy_attempts a JOIN academy_quiz_items i ON i.id=a.quiz_item_id "
                             "WHERE a.user_id=? AND i.kind='write' AND a.score>=? AND a.created>?",
                             (uid, th["write_score"], since)).fetchall()
        n = distinct_answers([db.jload(r["answer"], r["answer"]) for r in rows])
        out.append(_crit("write_items", f"{th['write_items']} write-it answers scoring {th['write_score']}+", n >= th["write_items"],
                         n, th["write_items"], "", "practice"))
    elif level == "L3":
        with tenant.db() as c:
            ex = c.execute("SELECT MAX(score) s FROM academy_exams WHERE user_id=? AND path_key=? AND level='L3' AND status='passed' "
                           "AND submitted>?", (uid, path_key, since)).fetchone()["s"]
        out.append(_crit("exam", f"Timed exam of {th['exam_items']} questions at {int(th['exam_pass'] * 100)}%+",
                         ex is not None and ex >= th["exam_pass"], None if ex is None else f"{round(ex * 100)}%",
                         f"{int(th['exam_pass'] * 100)}%", "", "exam"))
        signed = [r for r in _roleplays(tenant, uid, since) if isinstance(r["score"].get("signoff"), dict)
                  and r["score"]["signoff"].get("by")]
        out.append(_crit("signoff", "One role-play signed off by a coach or the owner", bool(signed), len(signed), 1, "", "roleplay"))
        out.append(_real_work(tenant, user, path_key, th, since))
    elif level == "L4":
        graded = _graded_by(tenant, user)
        tol = th["grade_tolerance"]
        agree = sum(1 for g in graded if abs(g["given"] - g["total"]) <= tol)
        rate = agree / len(graded) if graded else 0.0
        out.append(_crit("calibration", f"{th['calibration_items']}+ graded role-plays with {int(th['calibration_agreement'] * 100)}% "
                                        f"within ±{tol} of the rules score",
                         len(graded) >= th["calibration_items"] and rate >= th["calibration_agreement"],
                         f"{len(graded)} graded, {round(rate * 100)}% agree", f"{th['calibration_items']} graded, "
                         f"{int(th['calibration_agreement'] * 100)}%", "Only coaches and the owner grade role-plays." if
                         user["role"] not in ("coach", "owner") else "", "team"))
    return out


def _real_work(tenant, user: dict, path_key: str, th: dict, since: float) -> dict:
    """Real-work evidence from the system (never self-reported)."""
    uid = user["id"]
    if path_key == "setter":
        actor_prefix = f"{user['username']}("
        with tenant.db() as c:
            rows = c.execute(f"SELECT id, status, decided_by, kind, json_extract(meta,'$.owner_user_id') owner FROM queue "
                             f"WHERE kind IN ({','.join('?' * len(DM_KINDS))}) AND created>?", (*DM_KINDS, since)).fetchall()
            blocks = set()
            for r in c.execute("SELECT detail FROM audit WHERE kind='guardrail.block' AND at>?", (since,)):
                d = db.jload(r["detail"], {}) or {}
                if isinstance(d, dict) and d.get("id"):
                    blocks.add(d["id"])
        mine = [r for r in rows if r["owner"] == uid or (r["decided_by"] or "").startswith(actor_prefix)]
        approved = [r for r in mine if r["status"] in SENT_STATUSES]
        holds = sum(1 for r in mine if r["id"] in blocks)
        rate = holds / len(mine) if mine else 0.0
        need = th["setter_dms"]
        return _crit("real_work", f"{need}+ approved DMs of your own with at most {int(th['setter_hold_rate'] * 100)}% held by "
                                  "guardrails", len(approved) >= need and rate <= th["setter_hold_rate"],
                     f"{len(approved)} approved, {round(rate * 100)}% held", f"{need} approved, ≤{int(th['setter_hold_rate'] * 100)}%",
                     "Read from the approval queue.", "queue")
    if path_key == "closer":
        from .. import call_review

        names = {n.strip() for n in (user["username"], user.get("display") or "") if n and n.strip()}
        with tenant.db() as c:
            rows = c.execute("SELECT id, rep, review, created FROM calls WHERE status='reviewed' AND created>?", (since,)).fetchall()
        scores = []
        for r in rows:
            if (r["rep"] or "").strip() not in names:
                continue
            try:
                if not call_review.consent_status(tenant, r["id"])["complete"]:
                    continue
            except KeyError:
                continue
            scores.append(float((db.jload(r["review"], {}) or {}).get("overall") or 0))
        avg = sum(scores) / len(scores) if scores else 0.0
        need = th["closer_calls"]
        return _crit("real_work", f"{need}+ consented, reviewed calls averaging {th['closer_call_score']}+",
                     len(scores) >= need and avg >= th["closer_call_score"], f"{len(scores)} calls, average {round(avg)}",
                     f"{need} calls, {th['closer_call_score']}+", "Read from Call review.", "calls")
    if path_key == "growth_operator":
        n = _clients_won(tenant, user, since)
        return _crit("real_work", "A first client signed", n >= th["operator_clients"], n, th["operator_clients"],
                     "Read from your clients, prospects and proposals.", "prospects")
    if path_key == "coach":
        graded = _graded_by(tenant, user)
        tol = th["grade_tolerance"]
        ok = sum(1 for g in graded if abs(g["given"] - g["total"]) <= tol)
        return _crit("real_work", f"{th['coach_graded']} learner role-plays graded within ±{tol} of the rules score",
                     ok >= th["coach_graded"], ok, th["coach_graded"], "Grade role-plays in the Team tab.", "team")
    return _crit("real_work", "Real work evidence", False, 0, 1)


def _clients_won(tenant, user: dict, since: float) -> int:
    n = 0
    with tenant.db() as c:
        if user.get("student_id"):
            n += c.execute("SELECT COUNT(*) FROM clients WHERE student_id=? AND COALESCE(created,0)>?",
                           (user["student_id"], since)).fetchone()[0]
        n += c.execute("SELECT COUNT(*) FROM prospects WHERE owner_user_id=? AND stage='won' AND COALESCE(updated,created,0)>?",
                       (user["id"], since)).fetchone()[0]
        for r in c.execute("SELECT payload FROM app_events WHERE name='client_won' AND created>?", (since,)):
            p = db.jload(r["payload"], {}) or {}
            if p.get("owner_user_id") == user["id"] and not p.get("prospect_id"):
                n += 1
    return n


# --------------------------------------------------------------------------- ladder, awarding
def ladder(tenant, user: dict, path_key: str) -> dict:
    path = common.path_def(tenant, path_key)
    if path is None:
        raise KeyError(path_key)
    certs = {r["level"]: r for r in _cert_rows(tenant, user["id"]) if r["path_key"] == path_key}
    levels = []
    for lv in path["levels"]:
        cr = criteria(tenant, user, path_key, lv)
        held = certs.get(lv)
        status = held["status"] if held else ("ready" if cr and all(x["met"] for x in cr) else "in_progress")
        levels.append({"level": lv, "name": common.LEVEL_NAMES[lv], "status": status,
                       "status_label": {"active": "Certified", "expired": "Expired: renew it", "revoked": "Revoked",
                                        "ready": "Ready to claim", "in_progress": "In progress"}.get(status, status),
                       "cert": _cert_public(held) if held else None, "criteria": cr,
                       "missing": [x for x in cr if not x["met"]]})
    return {"path": path_key, "title": path["title"], "blurb": path["blurb"], "levels": levels}


def _cert_public(r: dict) -> dict:
    ev = r.get("evidence") or {}
    return {"id": r["id"], "level": r["level"], "path": r["path_key"], "status": r["status"], "awarded": r["awarded"],
            "expires": r["expires"], "signed_off_by": r.get("signed_off_by"), "reason": r.get("revoked_reason"),
            "text": ev.get("text")}


def certificate_text(tenant, user: dict, path_key: str, level: str) -> str:
    brand = ((tenant.get("brand", {}) or {}).get("name")) or "the programme"
    title = common.PATHS[path_key]["title"]
    who = user.get("display") or user["username"]
    text = (f"{who} holds the {title} certificate, level {level} ({common.LEVEL_NAMES[level]}), from {brand}. "
            "It is an internal training credential. It shows lessons completed and skills practised and checked; "
            "it says nothing about income or results.")
    if guardrails.blocked(guardrails.check_for(tenant, text, {})):
        text = (f"Internal training credential: {title}, level {level}. It shows training completed; it says nothing "
                "about income or results.")
    return text


def award(tenant, user: dict, path_key: str, level: str, crit: list[dict], actor: str = "system") -> dict:
    now = time.time()
    expires = now + common.CERT_DAYS * common.DAY
    path = common.path_def(tenant, path_key)
    text = certificate_text(tenant, user, path_key, level)
    snapshot = {"thresholds": path["thresholds"][level], "criteria": crit}
    with tenant.db() as c:
        old = c.execute("SELECT id, evidence FROM certifications WHERE user_id=? AND path_key=? AND level=?",
                        (user["id"], path_key, level)).fetchone()
        history = ((db.jload(old["evidence"], {}) or {}).get("history") or []) if old else []
        if old:
            prev = db.jload(old["evidence"], {}) or {}
            history = (history + [{"status": prev.get("status"), "ended_at": prev.get("ended_at"),
                                   "reason": prev.get("reason")}])[-10:]
        ev = {"status": "active", "expires": expires, "criteria": {x["key"]: {"have": x["have"], "need": x["need"]}
                                                                    for x in crit},
              "text": text, "history": history}
        if old:
            c.execute("UPDATE certifications SET awarded=?, evidence=?, status='active', expires=?, revoked_reason=NULL, "
                      "signed_off_by=NULL, criteria_snapshot=? WHERE id=?",
                      (now, db.jdump(ev), expires, db.jdump(snapshot), old["id"]))
            cid = old["id"]
        else:
            cid = c.execute("INSERT INTO certifications(user_id,path_key,level,awarded,evidence,status,expires,criteria_snapshot) "
                            "VALUES(?,?,?,?,?,?,?,?)", (user["id"], path_key, level, now, db.jdump(ev), "active", expires,
                                                         db.jdump(snapshot))).lastrowid
    audit.log(tenant, actor, "settings", {"academy_certificate": f"{path_key}/{level}", "status": "awarded", "user": user["id"]})
    events.emit(tenant, "certification_awarded", {"user_id": user["id"], "path_key": path_key, "level": level})
    return {"id": cid, "path": path_key, "level": level, "expires": expires, "text": text}


def refresh(tenant, user: dict, actor: str | None = None, skill: str | None = None) -> list[dict]:
    """Expire what is due, apply the recertification trigger, then award every level whose criteria are all met
    (in order: a level needs the one below it). With `skill`, only paths that teach that skill are checked
    (an answer can only move those), plus Compliance core, which every other path needs."""
    actor = actor or f"{user['username']}({user['role']})"
    expire_due(tenant)
    recert_check(tenant, user["id"], actor)
    awarded = []
    keys = common.user_paths(tenant, user)
    if skill:
        keys = [k for k in keys if k == "compliance_core" or skill in common.PATHS[k]["skills"]]
    if "compliance_core" in keys:
        keys = ["compliance_core"] + [k for k in keys if k != "compliance_core"]
    for pk in keys:
        path = common.path_def(tenant, pk)
        for lv in path["levels"]:
            if is_certified(tenant, user["id"], pk, lv):
                continue
            cr = criteria(tenant, user, pk, lv)
            if cr and all(x["met"] for x in cr):
                awarded.append(award(tenant, user, pk, lv, cr, actor))
            else:
                break
    return awarded


def signoff_cert(tenant, cert_id: int, viewer: dict, actor: str) -> dict:
    with tenant.db() as c:
        r = c.execute("SELECT * FROM certifications WHERE id=?", (cert_id,)).fetchone()
    if r is None or not common.can_see(tenant, viewer, r["user_id"]) or r["user_id"] == viewer["id"]:
        raise KeyError(cert_id)
    if (r["status"] or "active") != "active":
        raise common.Refused("Only an active certificate can be signed off.")
    who = viewer.get("display") or viewer["username"]
    with tenant.db() as c:
        c.execute("UPDATE certifications SET signed_off_by=? WHERE id=?", (who, cert_id))
    audit.log(tenant, actor, "settings", {"academy_certificate_signoff": cert_id})
    return {"id": cert_id, "signed_off_by": who}


def revoke_cert(tenant, cert_id: int, viewer: dict, reason: str, actor: str) -> dict:
    with tenant.db() as c:
        r = c.execute("SELECT * FROM certifications WHERE id=?", (cert_id,)).fetchone()
    if r is None or not common.can_see(tenant, viewer, r["user_id"]) or r["user_id"] == viewer["id"]:
        raise KeyError(cert_id)
    if (r["status"] or "active") == "revoked":
        raise common.Refused("This certificate is already revoked.")
    _end(tenant, r["user_id"], r["path_key"], r["level"], "revoked", reason.strip() or "Revoked by a coach", actor)
    return {"id": cert_id, "status": "revoked"}


# --------------------------------------------------------------------------- exams
def exam_open(tenant, user_id: int) -> bool:
    """True while this person has a certification exam open (the coach answers 423 meanwhile)."""
    try:
        with tenant.db() as c:
            r = c.execute("SELECT 1 FROM academy_exams WHERE user_id=? AND submitted IS NULL AND deadline>?",
                          (user_id, time.time())).fetchone()
    except Exception:  # noqa: BLE001
        return False
    return r is not None


def exam_pool(tenant, path: dict) -> list[int]:
    return [i for g in exam_groups(tenant, path) for i in g]


def exam_groups(tenant, path: dict) -> list[list[int]]:
    """Approved exam questions for a path, grouped by the source line they test. An exam takes at most one
    question per group, so two questions never give each other away."""
    with tenant.db() as c:
        rows = c.execute(
            "SELECT i.id, i.citation, i.norm_key FROM academy_quiz_items i JOIN academy_lessons l ON l.id=i.lesson_id "
            "WHERE i.pool='exam' AND i.status='published' AND l.status='published' AND i.kind!='write' AND i.skill IN (%s) "
            "ORDER BY i.id" % ",".join("?" * len(path["skills"])), path["skills"]).fetchall()
    groups: dict[str, list[int]] = {}
    for r in rows:
        key = quiz.source_key(db.jload(r["citation"], None), r["norm_key"] or str(r["id"]))
        groups.setdefault(key, []).append(r["id"])
    return list(groups.values())


EXAM_GRACE_S = 60


def close_expired_exams(tenant, user_id: int) -> int:
    """An exam left open past its deadline (plus a minute's grace) is a failed attempt: score 0, status 'expired',
    submitted at the deadline. Without this, opening an exam, reading every question and walking away would
    hand out a fresh sample with no retake cooldown."""
    now = time.time()
    with tenant.db() as c:
        rows = c.execute("SELECT id, deadline FROM academy_exams WHERE user_id=? AND submitted IS NULL AND deadline<?",
                         (user_id, now - EXAM_GRACE_S)).fetchall()
        for r in rows:
            c.execute("UPDATE academy_exams SET submitted=?, score=0, status='expired', answers=? WHERE id=? AND submitted IS NULL",
                      (r["deadline"], db.jdump({"answered": 0, "expired": True}), r["id"]))
    for r in rows:
        audit.log(tenant, "system", "run", {"academy_exam": r["id"], "status": "expired"})
    return len(rows)


def start_exam(tenant, user: dict, path_key: str, level: str = "L3") -> dict:
    path = common.path_def(tenant, path_key)
    if path is None or level not in path["levels"] or level != "L3":
        raise common.Refused("Only the L3 certificate has a timed exam.", 422)
    th = path["thresholds"]["L3"]
    uid = user["id"]
    close_expired_exams(tenant, uid)
    with tenant.db() as c:
        open_ = c.execute("SELECT id FROM academy_exams WHERE user_id=? AND submitted IS NULL AND deadline>? ORDER BY id DESC",
                          (uid, time.time() - EXAM_GRACE_S)).fetchone()
    if open_:
        return get_exam(tenant, user, open_["id"])
    if not is_certified(tenant, uid, path_key, "L2"):
        raise common.Refused(f"Earn {path['title']} L2 before the L3 exam.")
    with tenant.db() as c:
        last = c.execute("SELECT submitted, status FROM academy_exams WHERE user_id=? AND path_key=? AND level=? AND "
                         "submitted IS NOT NULL ORDER BY submitted DESC LIMIT 1", (uid, path_key, level)).fetchone()
    if last and last["status"] != "passed":
        wait_until = last["submitted"] + th["cooldown_hours"] * 3600
        if wait_until > time.time():
            hrs = max(1, round((wait_until - time.time()) / 3600))
            raise common.Refused(f"You can retake this exam in about {hrs} hours.", 429)
    groups = exam_groups(tenant, path)
    need = int(th["exam_items"])
    if len(groups) < need:
        raise common.Refused(f"The exam needs {need} approved exam questions on different lines of the material for this "
                             f"path; there are {len(groups)}. Ask the owner to approve more in Academy admin.")
    seed = random.SystemRandom().randrange(1, 2**31)
    rng = random.Random(seed)
    ids = [rng.choice(g) for g in rng.sample(groups, need)]
    now = time.time()
    with tenant.db() as c:
        eid = c.execute("INSERT INTO academy_exams(user_id,path_key,level,item_ids,seed,started,deadline,status) "
                        "VALUES(?,?,?,?,?,?,?,'open')", (uid, path_key, level, db.jdump(ids), seed, now,
                                                         now + th["exam_minutes"] * 60)).lastrowid
    audit.log(tenant, f"{user['username']}({user['role']})", "run", {"academy_exam": eid, "path": path_key})
    return get_exam(tenant, user, eid)


def get_exam(tenant, user: dict, exam_id: int) -> dict:
    close_expired_exams(tenant, user["id"])
    with tenant.db() as c:
        r = c.execute("SELECT * FROM academy_exams WHERE id=?", (exam_id,)).fetchone()
    if r is None or r["user_id"] != user["id"]:
        raise KeyError(exam_id)
    ids = db.jload(r["item_ids"], [])
    out = {"id": r["id"], "path": r["path_key"], "level": r["level"], "started": r["started"], "deadline": r["deadline"],
           "status": r["status"], "submitted": r["submitted"], "score": r["score"], "total": len(ids)}
    if r["submitted"] is None:
        items = []
        for iid in ids:
            it = quiz.load(tenant, iid)
            if it:
                items.append(quiz.public(it))
        out["items"] = items
    return out


def submit_exam(tenant, user: dict, exam_id: int, answers: dict) -> dict:
    with tenant.db() as c:
        r = c.execute("SELECT * FROM academy_exams WHERE id=?", (exam_id,)).fetchone()
    if r is None or r["user_id"] != user["id"]:
        raise KeyError(exam_id)
    if r["submitted"] is not None:
        if r["status"] == "expired":
            raise common.Refused("Time ran out on this exam before it was submitted, so it counts as a failed attempt.")
        raise common.Refused("This exam is already submitted.")
    path = common.path_def(tenant, r["path_key"])
    th = path["thresholds"]["L3"]
    ids = db.jload(r["item_ids"], [])
    late = time.time() > (r["deadline"] or 0) + EXAM_GRACE_S
    right = 0
    now = db.now()
    with tenant.db() as c:
        for iid in ids:
            it = quiz.load(tenant, iid)
            given = answers.get(str(iid), answers.get(iid))
            ok = False
            chosen = None
            if it is not None and given is not None and not late:
                try:
                    g = quiz.grade(tenant, it, given)
                    ok, chosen = g["correct"], g["chosen_option"]
                except ValueError:
                    ok = False
            right += 1 if ok else 0
            c.execute("INSERT INTO academy_attempts(user_id,quiz_item_id,answer,correct,created,context,session_id,chosen_option,score) "
                      "VALUES(?,?,?,?,?,?,?,?,?)", (user["id"], iid, None if given is None else db.jdump(given)[:2000],
                                                    1 if ok else 0, now, "exam", f"exam:{exam_id}", chosen, 100.0 if ok else 0.0))
    score = right / len(ids) if ids else 0.0
    status = "late" if late else ("passed" if score >= th["exam_pass"] else "failed")
    with tenant.db() as c:
        c.execute("UPDATE academy_exams SET submitted=?, score=?, status=?, answers=? WHERE id=?",
                  (time.time(), score, status, db.jdump({"answered": len([a for a in answers.values() if a is not None])}), exam_id))
    audit.log(tenant, f"{user['username']}({user['role']})", "run", {"academy_exam": exam_id, "status": status})
    awarded = refresh(tenant, user)
    return {"id": exam_id, "score": round(score, 3), "right": right, "total": len(ids), "status": status,
            "passed": status == "passed", "pass_mark": th["exam_pass"], "awarded": awarded,
            "retake_after": None if status == "passed" else time.time() + th["cooldown_hours"] * 3600}


# --------------------------------------------------------------------------- the approval gate
def _setting(tenant, key: str) -> bool:
    try:
        return bool(tenant.get(key, False))
    except Exception:  # noqa: BLE001
        return False


def approval_gate(tenant, user_id: int, kind: str, meta: dict | None = None, role: str | None = None) -> str | None:
    """None when approval may go ahead, else the reason it may not. Two owner switches, both off by default:

    require_cert_for_own_outreach      a non-owner approving prospect-facing own-decide items (DMs, emails,
                                       proposals, booking messages ...) needs the Compliance core certificate.
    require_setter_cert_for_decisions  a setter using their setter decide rights on someone else's items
                                       (the team's DMs, follow-ups, reminders) needs Setter L1 (A9)."""
    own_switch = _setting(tenant, "require_cert_for_own_outreach")
    setter_switch = _setting(tenant, "require_setter_cert_for_decisions")
    if not own_switch and not setter_switch:
        return None
    if role is None:
        u = common.user_row(tenant, user_id)
        role = u["role"] if u else "student"
    if role == "owner":
        return None
    meta = meta or {}
    own = meta.get("owner_user_id") == user_id
    if setter_switch and role == "setter" and not own and kind in auth.ROLE_DECIDE.get("setter", set()):
        if not is_certified(tenant, user_id, "setter", "L1"):
            return ("The owner requires the Setter L1 certificate before you approve the team's messages. "
                    "Finish it in the Academy (Certificates tab).")
    if not own_switch:
        return None
    if kind not in auth.OWN_DECIDE:
        return None
    if kind not in PROSPECT_OWN_KINDS and not meta.get("prospect_facing"):
        return None
    recert_check(tenant, user_id)
    if is_certified(tenant, user_id, "compliance_core", "L1"):
        return None
    return ("The owner requires the Compliance core certificate before you approve your own outreach. "
            "Finish it in the Academy (Certificates tab).")


def approval_gate_for_item(tenant, user_id: int, qid: int, role: str | None = None) -> str | None:
    item = queue.get(tenant, qid, scrub=False)
    if item is None:
        return None
    meta = item.get("meta") or {}
    if role is not None and not auth.can_decide(role, item["kind"], meta.get("owner_user_id"), user_id):
        return None  # the queue itself answers "your role can't approve this"
    return approval_gate(tenant, user_id, item["kind"], meta, role)
