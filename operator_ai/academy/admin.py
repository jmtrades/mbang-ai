"""Owner admin (A14): build runs, the draft review list (source passage beside each lesson, machine checks,
skipped unsafe sentences), the coverage map with gaps in the material, item statistics with auto-flags,
per-path certification thresholds and the outreach certificate switch."""
from __future__ import annotations

from collections import Counter

from .. import audit, db, guardrails, index, textnorm
from . import common, quiz

SETTING = "require_cert_for_own_outreach"
SETTER_SETTING = "require_setter_cert_for_decisions"   # A9: setter decide rights need Setter L1 (off by default)
SETTINGS = (SETTING, SETTER_SETTING)


def builds(tenant, limit: int = 10) -> list[dict]:
    common.ensure_extras(tenant)
    with tenant.db() as c:
        rows = db.rows(c.execute("SELECT * FROM academy_builds ORDER BY id DESC LIMIT ?", (limit,)))
    for r in rows:
        r["stats"] = db.jload(r["stats"], {})
        sk = db.jload(r["skipped"], {}) or {}
        r["skipped_count"] = {k: len(v) for k, v in sk.items()} if isinstance(sk, dict) else {}
        r.pop("skipped", None)
    return rows


def _source_lines(tenant, file: str, a: int, b: int) -> list[dict]:
    try:
        doc = index.for_tenant(tenant).docs.get(file)
    except Exception:  # noqa: BLE001
        doc = None
    if doc is None:
        return []
    names = guardrails.tenant_banned_names(tenant)
    lo, hi = max(1, a), min(len(doc.lines), b)
    return [{"n": n, "text": guardrails.scrub(doc.lines[n - 1], names)} for n in range(lo, hi + 1) if doc.lines[n - 1].strip()]


def drafts(tenant, status: str = "draft", owner: bool = True) -> dict:
    """Lessons in one status with their questions. Exam-pool answers are only for the owner: coaches may
    read the drafts, and a coach (or anyone they pass it to) must never hold the answer key to an exam."""
    common.ensure_extras(tenant)
    names = guardrails.tenant_banned_names(tenant)
    with tenant.db() as c:
        lessons = db.rows(c.execute("SELECT * FROM academy_lessons WHERE status=? ORDER BY skill, ord, id LIMIT 300", (status,)))
        item_rows = db.rows(c.execute("SELECT * FROM academy_quiz_items WHERE lesson_id IN (SELECT id FROM academy_lessons "
                                      "WHERE status=?) ORDER BY id", (status,)))
        exam_drafts = c.execute("SELECT COUNT(*) FROM academy_quiz_items i JOIN academy_lessons l ON l.id=i.lesson_id "
                                "WHERE i.pool='exam' AND i.status='draft' AND l.status='published'").fetchone()[0]
        stale = db.rows(c.execute("SELECT id, title, skill FROM academy_lessons WHERE status='stale' ORDER BY id"))
        last = c.execute("SELECT skipped FROM academy_builds WHERE status='done' ORDER BY id DESC LIMIT 1").fetchone()
    items_by: dict[int, list] = {}
    for it in item_rows:
        it = quiz._row(it)
        items_by.setdefault(it["lesson_id"], []).append({
            "id": it["id"], "kind": it["kind"], "kind_label": quiz.KIND_LABEL.get(it["kind"], it["kind"]),
            "prompt": guardrails.scrub(it["prompt"], names), "options": it["options"],
            "answer": it["answer"] if it["kind"] != "write" and (owner or it["pool"] != "exam") else None,
            "pool": it["pool"], "status": it["status"], "checks": it["checks"]})
    out = []
    for L in lessons:
        a, _, b = (L["source_lines"] or "1-1").partition("-")
        out.append({"id": L["id"], "title": guardrails.scrub(L["title"], names), "skill": L["skill"],
                    "skill_label": common.skill_label(L["skill"]), "status": L["status"], "gen_mode": L["gen_mode"],
                    "gen_version": L["gen_version"], "key_points": db.jload(L["key_points"], []),
                    "mistakes": db.jload(L["mistakes"], []), "example": db.jload(L["example"], []),
                    "citations": db.jload(L["citations"], []), "checks": db.jload(L["checks"], {}),
                    "source": {"file": L["source_file"], "lines": L["source_lines"],
                               "passage": _source_lines(tenant, L["source_file"], int(a or 1), int(b or a or 1))[:40]},
                    "items": items_by.get(L["id"], [])})
    skipped = db.jload(last["skipped"], {}) if last else {}
    try:
        not_used = [e.to_dict() for e in index.for_tenant(tenant).excluded][:100]
    except Exception:  # noqa: BLE001
        not_used = []
    unsafe = [{**s, "text": guardrails.scrub(s.get("text", ""), names),
               "citation": {"id": "X", "file": s["file"], "lines": f"{s['line']}-{s['line']}", "title": s["file"]}}
              for s in (skipped or {}).get("unsafe", [])]
    return {"lessons": out, "exam_items_awaiting_approval": exam_drafts, "stale": stale, "unsafe": unsafe,
            "too_noisy": (skipped or {}).get("too_noisy", []), "too_thin": (skipped or {}).get("too_thin", []),
            "not_used": not_used}


def coverage(tenant, viewer: dict | None = None) -> dict:
    """Material sections -> lessons and questions, plus student questions the material did not cover, grouped.
    The owner sees every uncovered question; a coach only their own learners' (people's questions to the coach
    are theirs, not every coach's)."""
    names = guardrails.tenant_banned_names(tenant)
    ids = None if viewer is None else common.learner_ids(tenant, viewer)
    with tenant.db() as c:
        les = db.rows(c.execute("SELECT l.source_file, l.status, COUNT(i.id) n FROM academy_lessons l LEFT JOIN "
                                "academy_quiz_items i ON i.lesson_id=l.id AND i.status IN ('published','draft') "
                                "WHERE l.status IN ('published','draft','stale') GROUP BY l.id"))
        if ids is None:
            qs = [r["question"] for r in c.execute("SELECT question FROM queries WHERE covered IN ('no','partly') "
                                                   "ORDER BY id DESC LIMIT 500")]
        elif ids:
            qs = [r["question"] for r in c.execute(
                "SELECT question FROM queries WHERE covered IN ('no','partly') AND user_id IN (%s) ORDER BY id DESC LIMIT 500"
                % ",".join("?" * len(ids)), ids)]
        else:
            qs = []
    by_file: dict[str, dict] = {}
    for r in les:
        f = by_file.setdefault(r["source_file"], {"file": r["source_file"], "lessons": 0, "published": 0, "questions": 0})
        f["lessons"] += 1
        f["published"] += 1 if r["status"] == "published" else 0
        f["questions"] += r["n"]
    try:
        idx = index.for_tenant(tenant)
        docs = [{"file": d.file, "title": guardrails.scrub(d.title, names), "kind": d.kind, "quality": d.quality}
                for d in idx.docs.values() if d.kind != "summary"]
    except Exception:  # noqa: BLE001
        docs = []
    rows = []
    for d in docs:
        f = by_file.get(d["file"], {"lessons": 0, "published": 0, "questions": 0})
        rows.append({**d, "lessons": f["lessons"], "published": f["published"], "questions": f["questions"],
                     "covered": f["lessons"] > 0})
    # group uncovered questions by their most distinctive shared term
    groups: dict[str, list[str]] = {}
    df = Counter(t for q in qs for t in set(textnorm.tokens(q)))
    for q in qs:
        toks = [t for t in set(textnorm.tokens(q)) if len(t) > 2]
        if not toks:
            continue
        key = max(toks, key=lambda t: (df[t], t))
        groups.setdefault(key, []).append(guardrails.scrub(q, names))
    gaps = sorted(({"topic": k, "count": len(v), "examples": v[:3]} for k, v in groups.items()), key=lambda g: -g["count"])
    return {"documents": rows, "gaps": gaps[:20], "uncovered_questions": len(qs)}


def stats(tenant) -> dict:
    rows = quiz.item_stats(tenant)
    names = guardrails.tenant_banned_names(tenant)
    for r in rows:
        r["prompt"] = guardrails.scrub(r["prompt"], names)
    rows.sort(key=lambda r: (-len(r["flags"]), -(r["attempts"] or 0), r["id"]))
    return {"items": rows[:300], "flagged": sum(1 for r in rows if r["flags"]), "min_attempts": quiz.FLAG_MIN_ATTEMPTS}


def settings_view(tenant) -> dict:
    return {SETTING: bool(tenant.get(SETTING, False)), SETTER_SETTING: bool(tenant.get(SETTER_SETTING, False)),
            "paths": [{"key": k, "title": p["title"], "levels": p["levels"],
                       "thresholds": {lv: v for lv, v in common.path_def(tenant, k)["thresholds"].items() if lv in p["levels"]}}
                      for k, p in common.PATHS.items()]}


def set_setting(tenant, value: bool, actor: str, key: str = SETTING) -> dict:
    if key not in SETTINGS:
        raise ValueError(f"Unknown setting {key}")
    tenant.set(key, bool(value))
    audit.log(tenant, actor, "settings", {"key": key, "value": bool(value)})
    return {key: bool(value)}


_BOUNDS = {"mastery_overall": (0.5, 1.0), "mastery_skill": (0.3, 1.0), "roleplays": (1, 20), "roleplay_score": (50, 100),
           "write_items": (1, 50), "write_score": (50, 100), "exam_items": (5, 100), "exam_pass": (0.5, 1.0),
           "exam_minutes": (5, 240), "cooldown_hours": (0, 168), "setter_dms": (1, 1000), "setter_hold_rate": (0.0, 0.5),
           "closer_calls": (1, 200), "closer_call_score": (40, 100), "operator_clients": (1, 20), "coach_graded": (1, 200),
           "grade_tolerance": (1, 30), "calibration_items": (5, 200), "calibration_agreement": (0.5, 1.0)}


def set_thresholds(tenant, path_key: str, levels: dict, actor: str) -> dict:
    """Owner edits per-path thresholds. Values are validated against sane bounds; unknown keys are refused.
    Awarded certificates keep the snapshot they were awarded under."""
    if path_key not in common.PATHS:
        raise KeyError(path_key)
    cur = common.path_def(tenant, path_key)["thresholds"]
    for lv, vals in levels.items():
        if lv not in common.PATHS[path_key]["levels"] or not isinstance(vals, dict):
            raise ValueError(f"Unknown level {lv}")
        for k, v in vals.items():
            if k not in cur[lv]:
                raise ValueError(f"Unknown setting {k}")
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError(f"{k} must be a number")
            lo, hi = _BOUNDS.get(k, (0, 1e9))
            if not lo <= float(v) <= hi:
                raise ValueError(f"{k} must be between {lo} and {hi}")
            cur[lv][k] = type(cur[lv][k])(v) if not isinstance(cur[lv][k], float) else float(v)
    with tenant.db() as c:
        c.execute("UPDATE academy_paths SET levels=?, updated=? WHERE key=?", (db.jdump(cur), db.now(), path_key))
    audit.log(tenant, actor, "settings", {"academy_thresholds": path_key})
    return {"path": path_key, "thresholds": cur}
