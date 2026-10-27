"""Mission engine: goal -> DAG of typed steps -> durable execution with waits.

* Steps run through the durable job queue (handler "missions.step", idempotency key
  mission:{id}:step:{sid}:try:{n}); each run goes through runtime so it leaves a trace.
* Every transition is written to mission_events and missions.next_check is recomputed.
* Steps wake on queue decisions (on_approve hook, and a poll for rejections), on domain events
  (reply_received, item_sent, client_won, certification_awarded, booking_made ...) and on time.
* Outbound steps always wait for approval; marking a step done never sends anything.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import asdict

from .. import audit, auth, db, events, jobs, queue, runtime
from ..redact import redact
from . import registry, templates
from .registry import DAY, STATUS_LABEL, TERMINAL, WAITING, StepResult, add_event, clock, events_of, q_states

log = logging.getLogger("operator.missions")
JOB = "missions.step"
MAX_FAILS = 3
MAX_OPEN = 20  # running or paused missions per person; each one drafts into the approval queue
RECHECK = 15 * 60
APPROVAL_POLL = 10 * 60
ACTIVE = "active"
MISSION_STATUSES = ("active", "paused", "done", "cancelled")
_tls = threading.local()


class MissionError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


# --------------------------------------------------------------------------- reading
def _user(tenant, uid: int | None) -> dict | None:
    if not uid:
        return None
    with tenant.db() as c:
        r = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return auth.user_public(dict(r)) if r else None


def _parse_step(r: dict) -> dict:
    s = dict(r)
    s["params"] = db.jload(s.get("params"), {}) or {}
    s["depends"] = db.jload(s.get("depends"), []) or []
    s["output"] = db.jload(s.get("output"), {}) or {}
    s["queue_ids"] = db.jload(s.get("queue_ids"), []) or []
    s["wait"] = db.jload(s.get("wait"), {}) or {}
    s["cycles"] = s.get("cycles") or 0
    s["fails"] = s.get("fails") or 0
    return s


def _parse_mission(r: dict) -> dict:
    m = dict(r)
    for k, d in (("plan", {}), ("context", {}), ("progress", {}), ("metrics", {})):
        m[k] = db.jload(m.get(k), d) or d
    return m


def get(tenant, mid: int, steps: bool = True) -> dict | None:
    with tenant.db() as c:
        r = db.row(c.execute("SELECT * FROM missions WHERE id=?", (mid,)).fetchone())
        if r is None:
            return None
        m = _parse_mission(r)
        if steps:
            m["steps"] = [_parse_step(s) for s in db.rows(c.execute(
                "SELECT * FROM mission_steps WHERE mission_id=? ORDER BY ord, id", (mid,)))]
    return m


def _coach_student_uids(tenant, coach_id: int) -> set[int]:
    with tenant.db() as c:
        return {r["id"] for r in c.execute(
            "SELECT u.id FROM users u JOIN students s ON s.id=u.student_id WHERE s.coach_id=?", (coach_id,))}


def can_view(tenant, user: dict, m: dict) -> bool:
    if user["role"] == "owner" or m["user_id"] == user["id"]:
        return True
    return user["role"] == "coach" and m["user_id"] in _coach_student_uids(tenant, user["id"])


def can_act(user: dict, m: dict) -> bool:
    return user["role"] == "owner" or m["user_id"] == user["id"]


def list_for(tenant, user: dict, scope: str = "mine") -> list[dict]:
    q, args = "SELECT * FROM missions WHERE 1=1", []
    if scope == "all" and user["role"] == "owner":
        pass
    elif scope == "students" and user["role"] in ("coach", "owner"):
        uids = sorted(_coach_student_uids(tenant, user["id"])) if user["role"] == "coach" else None
        if uids is not None:
            if not uids:
                return []
            q += f" AND user_id IN ({','.join('?' * len(uids))})"
            args += uids
        else:
            q += " AND user_id IN (SELECT id FROM users WHERE role='student')"
    else:
        q += " AND user_id=?"
        args.append(user["id"])
    q += " ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'paused' THEN 1 ELSE 2 END, id DESC LIMIT 200"
    with tenant.db() as c:
        rows = [_parse_mission(r) for r in db.rows(c.execute(q, args))]
        names = {r["id"]: r["display"] for r in c.execute("SELECT id, display FROM users")}
        counts: dict[int, dict] = {}
        if rows:
            ids = [r["id"] for r in rows]
            for r in c.execute(f"SELECT mission_id, status, COUNT(*) n FROM mission_steps WHERE mission_id IN "
                               f"({','.join('?' * len(ids))}) GROUP BY mission_id, status", ids):
                counts.setdefault(r["mission_id"], {})[r["status"]] = r["n"]
    return [summary(tenant, m, counts.get(m["id"], {}), names.get(m["user_id"])) for m in rows]


def summary(tenant, m: dict, counts: dict | None = None, owner_name: str | None = None) -> dict:
    if counts is None:
        counts = {}
        for s in m.get("steps") or []:
            counts[s["status"]] = counts.get(s["status"], 0) + 1
    total = sum(counts.values())
    done = sum(counts.get(s, 0) for s in TERMINAL)
    tpl = templates.get(m["template"])
    from . import evaluator

    head = evaluator.headline(tenant, m)
    return {"id": m["id"], "title": m.get("title") or m["goal"], "goal": m["goal"], "template": m["template"],
            "template_label": tpl.label if tpl else m["template"], "status": m["status"], "created": m["created"],
            "updated": m["updated"], "user_id": m["user_id"], "owner_name": owner_name, "demo": bool(m.get("demo")),
            "parent_id": m.get("parent_id"), "plan_source": m.get("plan_source") or "rules",
            "progress": {"done": done, "total": total, "pct": round(100 * done / total) if total else 0,
                         "needs_you": counts.get("blocked_human", 0), "waiting_approval": counts.get("waiting_approval", 0),
                         "failed": counts.get("failed", 0)},
            "headline": head}


# --------------------------------------------------------------------------- create
def create(tenant, user: dict, goal: str, template: str | None = None, answers: dict | None = None,
           parent_id: int | None = None, start: bool = True, created: float | None = None) -> dict:
    goal = " ".join((goal or "").split())[:300]
    if not goal:
        raise MissionError(422, "Describe the goal in a few words")
    clean = templates.clean_answers(answers)
    matched = True
    if not template or template == templates.CUSTOM:
        template, matched = templates.classify(goal, user["role"])
        for k, v in templates.extract_clean(goal).items():
            clean.setdefault(k, v)
    tpl = templates.get(template)
    if tpl is None:
        raise MissionError(422, "Unknown mission template")
    if user["role"] not in tpl.roles:
        raise MissionError(403, "Your role can't start this kind of mission")
    if clean.get("client_id") and not _client_ok(tenant, user, clean["client_id"]):
        raise MissionError(404, "Client not found")
    with tenant.db() as c:
        n_open = c.execute("SELECT COUNT(*) FROM missions WHERE user_id=? AND status IN ('active','paused')",
                           (user["id"],)).fetchone()[0]
    if n_open >= MAX_OPEN:
        raise MissionError(409, f"You already have {n_open} missions running or paused. Finish or cancel one first.")
    specs = tpl.build(clean)
    now = created or clock()
    ctx = {"answers": clean, "decisions": [], "what_worked": [], "matched_template": matched}
    plan = {"template": template, "version": 1, "steps": [s["skey"] for s in specs]}
    with tenant.db() as c, db.tx(c):
        mid = c.execute("INSERT INTO missions(user_id,goal,template,status,plan,context,progress,metrics,created,updated,"
                        "next_check,parent_id,title,demo,plan_source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (user["id"], goal, template, ACTIVE, db.jdump(plan), db.jdump(ctx), "{}", "{}", now, now, now,
                         parent_id, goal[:120], 1 if tenant.is_demo else 0, "rules")).lastrowid
        _insert_steps(c, mid, specs, 0, now)
    eid = add_event(tenant, mid, "created", {"template": template, "goal": goal, "by": user.get("username"),
                                             "matched": matched, "parent_id": parent_id})
    if created:
        with tenant.db() as c:
            c.execute("UPDATE mission_events SET created=? WHERE id=?", (created, eid))
    audit.log(tenant, f"{user['username']}({user['role']})", "run", {"mission": mid, "template": template})
    if start:
        advance(tenant, mid)
        pump(tenant, mid)
    return get(tenant, mid)


def user_client_ids(tenant, user: dict) -> set[int] | None:
    """Clients this person may plan for: None means every client (the owner). Otherwise the clients linked to their
    student record, plus clients they won through one of their own missions (a won client's row may have no
    student_id yet; the win event names who won it)."""
    if user["role"] == "owner":
        return None
    ids: set[int] = set()
    with tenant.db() as c:
        if user.get("student_id"):
            ids |= {r[0] for r in c.execute("SELECT id FROM clients WHERE student_id=?", (user["student_id"],))}
        ids |= {int(r[0]) for r in c.execute(
            "SELECT DISTINCT json_extract(context,'$.client_id') FROM missions WHERE user_id=? AND "
            "json_extract(context,'$.won')=1 AND json_type(context,'$.client_id')='integer'", (user["id"],))}
        ids |= {int(r[0]) for r in c.execute(
            "SELECT DISTINCT json_extract(payload,'$.client_id') FROM app_events WHERE name='client_won' AND "
            "json_extract(payload,'$.owner_user_id')=? AND json_type(payload,'$.client_id')='integer'", (user["id"],))}
    return ids


def _client_ok(tenant, user: dict, cid: int) -> bool:
    with tenant.db() as c:
        r = c.execute("SELECT 1 FROM clients WHERE id=?", (int(cid),)).fetchone()
    if r is None:
        return False
    allowed = user_client_ids(tenant, user)
    return allowed is None or int(cid) in allowed


def _insert_steps(c, mid: int, specs: list[dict], start_ord: int, now: float) -> list[int]:
    ids = []
    for i, s in enumerate(specs):
        t = registry.get_type(s["type"])
        if t is None:
            raise MissionError(422, f"Unknown step type {s['type']}")
        ids.append(c.execute(
            "INSERT INTO mission_steps(mission_id,ord,title,agent,params,depends,status,output,queue_ids,created,updated,"
            "skey,wait,attempts,fails,cycles,message) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mid, start_ord + i, s.get("title") or t.label, t.key, db.jdump(s.get("params") or {}),
             db.jdump(s.get("depends") or []), "pending", "{}", "[]", now, now, s["skey"], "{}", 0, 0, 0,
             "Not started")).lastrowid)
    return ids


# --------------------------------------------------------------------------- scheduling
def _dep_ok(st: dict | None) -> bool:
    if st is None:
        return True
    if st["status"] in TERMINAL:
        return True
    t = registry.get_type(st["agent"])
    return bool(t and t.recurring and st["cycles"] >= 1)


def _enqueue(tenant, mid: int, step: dict, reason: str) -> int:
    with tenant.db() as c:
        r = c.execute("UPDATE mission_steps SET status='ready', attempts=COALESCE(attempts,0)+1, updated=? WHERE id=? "
                      "AND status NOT IN ('ready','running','done','skipped') RETURNING attempts", (clock(), step["id"])).fetchone()
    if r is None:
        return 0
    n = r["attempts"]
    jobs.enqueue(tenant, JOB, {"mission_id": mid, "step_id": step["id"], "try": n}, key=f"mission:{mid}:step:{step['id']}:try:{n}")
    add_event(tenant, mid, "step", {"step": step["skey"], "title": step["title"], "from": step["status"], "to": "ready",
                                    "reason": reason})
    return 1


def advance(tenant, mid: int, now: float | None = None) -> int:
    """Promote pending steps whose dependencies are met, wake waits whose time has come, finish the mission when
    every step is done. Only enqueues; pump() runs the jobs."""
    now = now or clock()
    m = get(tenant, mid)
    if m is None or m["status"] != ACTIVE:
        return 0
    by = {s["skey"]: s for s in m["steps"]}
    n = 0
    for s in m["steps"]:
        if s["status"] == "pending":
            if all(_dep_ok(by.get(d)) for d in s["depends"]) and (not s.get("due") or s["due"] <= now):
                n += _enqueue(tenant, mid, s, "dependencies met")
        elif s["status"] in WAITING and s.get("due") and s["due"] <= now:
            n += _enqueue(tenant, mid, s, "time reached")
    m = get(tenant, mid)
    if m["steps"] and all(s["status"] in TERMINAL for s in m["steps"]):
        with tenant.db() as c:
            c.execute("UPDATE missions SET status='done', updated=?, next_check=NULL WHERE id=? AND status='active'",
                      (now, mid))
        add_event(tenant, mid, "mission_done", {"message": "Every step is done."}, key="done")
        return n
    _next_check(tenant, m, now)
    return n


def _next_check(tenant, m: dict, now: float) -> None:
    cands = [s["due"] for s in m["steps"] if s["status"] in ("pending",) + WAITING and s.get("due")]
    if any(s["status"] == "waiting_approval" for s in m["steps"]):
        cands.append(now + APPROVAL_POLL)
    cands.append(registry.next_morning(now, 0) + 60)  # daily measurement
    with tenant.db() as c:
        c.execute("UPDATE missions SET next_check=?, updated=? WHERE id=?", (max(now, min(cands)), now, m["id"]))


def wake(tenant, mid: int, sid: int | None = None, event: str | None = None, reason: str = "", all_waiting: bool = False) -> int:
    m = get(tenant, mid)
    if m is None or m["status"] != ACTIVE:
        return 0
    n = 0
    for s in m["steps"]:
        if s["status"] not in WAITING:
            continue
        if (sid and s["id"] == sid) or all_waiting or (event and event in (s["wait"].get("events") or [])):
            n += _enqueue(tenant, mid, s, reason or (f"event {event}" if event else "woken"))
    n += advance(tenant, mid)
    pump(tenant, mid)
    return n


def pump(tenant, mid: int | None = None, max_jobs: int = 60) -> int:
    """Run this mission's queued step jobs now (the background workers would run them too; claims are atomic)."""
    if getattr(_tls, "pumping", False):
        return 0
    _tls.pumping = True
    ran = 0
    try:
        like = "mission:%"  # children spawned by a step run here too; bounded by max_jobs
        for _ in range(max_jobs):
            t = db.now()
            with tenant.db() as c:
                job = c.execute(
                    "UPDATE jobs SET status='running', updated=? WHERE id=(SELECT id FROM jobs WHERE status='queued' AND "
                    "kind=? AND key LIKE ? AND next_at<=? ORDER BY next_at, id LIMIT 1) AND status='queued' RETURNING *",
                    (t, JOB, like, t)).fetchone()
            if job is None:
                break
            jobs.run_one(tenant, dict(job))
            ran += 1
    finally:
        _tls.pumping = False
    return ran


# --------------------------------------------------------------------------- running a step
@jobs.handler(JOB)
def _job(tenant, payload: dict) -> dict:
    mid, sid, n = int(payload["mission_id"]), int(payload["step_id"]), int(payload.get("try") or 0)
    with tenant.db() as c:
        r = db.row(c.execute("UPDATE mission_steps SET status='running', updated=? WHERE id=? AND mission_id=? AND "
                             "status='ready' AND attempts=? RETURNING *", (clock(), sid, mid, n)).fetchone())
    if r is None:
        return {"skipped": "stale"}
    m = get(tenant, mid, steps=False)
    if m is None or m["status"] != ACTIVE:
        with tenant.db() as c:
            c.execute("UPDATE mission_steps SET status='pending', updated=? WHERE id=?", (clock(), sid))
        return {"skipped": "mission not active"}
    step = _parse_step(r)
    try:
        res, run_id = _execute(tenant, m, step)
    except Exception as e:  # noqa: BLE001 - recorded on the step, retried, then failed with an escalation
        _fail(tenant, m, step, e)
    else:
        _apply(tenant, m, step, res, run_id)
    advance(tenant, mid)
    return {"ok": True}


def _execute(tenant, m: dict, step: dict) -> tuple[StepResult, int | None]:
    t = registry.get_type(step["agent"])
    if t is None:
        raise RuntimeError("unknown step type")
    user = _user(tenant, m["user_id"])
    now = clock()
    if not t.allowed((user or {}).get("role")):
        return StepResult("skipped", f"'{t.label}' isn't available to your role, so this step was skipped."), None
    if t.needs_cert:
        path, level = t.needs_cert
        if registry.cert_state(tenant, m["user_id"], path, level) == "missing":
            label = f"{registry.PATH_LABEL.get(path, path)} {level}"
            assigned = step["output"].get("cert_assigned")
            if not assigned:
                sc0 = registry.StepCtx(tenant, m, step, {}, user, None, now)  # type: ignore[arg-type]
                assigned = registry.assign_path(sc0, path, level)
            return StepResult("waiting_event", f"Needs the {label} certification first: complete it in the Academy.",
                              output={"cert_assigned": bool(assigned), "href": "#/academy"},
                              wait={"events": ["certification_awarded"]}), None
    params = {k: p.default for k, p in t.params.items() if p.default is not None}
    params.update(step["params"] or {})
    holder: dict = {}
    actor = f"{user['username']}({user['role']})" if user else "system"

    def fn(run: runtime.Run):
        sc = registry.StepCtx(tenant, m, step, params, user, run, now)
        res = t.run(sc)
        holder["res"] = res
        run.note("result", {"step": step["skey"]}, {"status": res.status, "message": res.message,
                                                    "queue_ids": res.queue_ids or []}, label="Result")
        return {k: v for k, v in asdict(res).items() if k not in ("context",)}

    out = runtime.execute(tenant, "missions", f"Mission #{m['id']}: {step['title']}"[:200], [t.label, "Result"], fn, actor)
    return holder["res"], out["run_id"]


def _apply(tenant, m: dict, step: dict, res: StepResult, run_id: int | None) -> None:
    t = registry.get_type(step["agent"])
    now = clock()
    status = res.status if res.status in registry.STATUSES and res.status not in ("ready", "running", "pending") else "failed"
    qids = list(dict.fromkeys((step["queue_ids"] or []) + [int(q) for q in (res.queue_ids or [])]))
    if t and t.outbound and status in ("done", "skipped") and q_states(tenant, qids)["open"]:
        status = "waiting_approval"  # outbound work never finishes while one of its drafts still waits for a decision
    output = {**step["output"], **(res.output or {})}
    wait = res.wait or {}
    due = res.due
    if due is None and wait.get("recheck") and status in WAITING:
        due = now + RECHECK
    if status not in WAITING:
        due = None
    cycles = step["cycles"] + (1 if res.cycle else 0)
    msg = (res.message or STATUS_LABEL.get(status, status))[:600]
    with tenant.db() as c:
        c.execute("UPDATE mission_steps SET status=?, message=?, output=?, queue_ids=?, wait=?, due=?, cycles=?, "
                  "run_id=COALESCE(?, run_id), fails=0, updated=? WHERE id=?",
                  (status, msg, db.jdump(output), db.jdump(qids), db.jdump(wait), due, cycles, run_id, now, step["id"]))
    if res.context:
        _patch_context(tenant, m["id"], res.context)
    add_event(tenant, m["id"], "step", {"step": step["skey"], "title": step["title"], "from": "running", "to": status,
                                        "message": msg, "run_id": run_id, "queue_ids": res.queue_ids or []})
    if status == "done":
        events.emit(tenant, "mission_step_done", {"mission_id": m["id"], "step_id": step["id"]})
    for sp in res.spawn or []:
        _spawn(tenant, m, step, sp)


def _spawn(tenant, m: dict, step: dict, sp: dict) -> None:
    user = _user(tenant, m["user_id"])
    tpl = templates.get(sp.get("template"))
    if user is None or tpl is None:
        return
    if sp["template"] == "first_client" and user["role"] != "student" and m["template"] == "student_90_day":
        with tenant.db() as c:
            c.execute("UPDATE mission_steps SET status='skipped', message=? WHERE id=?",
                      ("The student starts their first-client mission from their own account.", step["id"]))
        return
    if user["role"] not in tpl.roles:
        return
    try:
        child = create(tenant, user, sp.get("goal") or tpl.label, sp["template"], sp.get("answers") or {}, parent_id=m["id"])
    except (MissionError, templates.AnswerError) as e:
        add_event(tenant, m["id"], "note", {"message": f"Couldn't start the follow-on mission: {e}"})
        return
    with tenant.db() as c:
        c.execute("UPDATE mission_steps SET output=json_set(COALESCE(output,'{}'),'$.child_id',?) WHERE id=?",
                  (child["id"], step["id"]))
    add_event(tenant, m["id"], "spawned", {"child_id": child["id"], "template": sp["template"]})


def _fail(tenant, m: dict, step: dict, e: Exception) -> None:
    fails = step["fails"] + 1
    err = redact(f"{type(e).__name__}: {e}")[:300]
    now = clock()
    log.warning("mission %s step %s failed (%s): %s", m["id"], step["skey"], fails, err)
    if fails >= MAX_FAILS:
        status, due, msg = "failed", None, f"This step failed {fails} times ({err}). Retry it or skip it."
    else:
        status, due, msg = "pending", now + 30 * (2 ** fails), f"Hit a problem ({err}); trying again shortly."
    with tenant.db() as c:
        c.execute("UPDATE mission_steps SET status=?, fails=?, due=?, message=?, updated=? WHERE id=?",
                  (status, fails, due, msg, now, step["id"]))
    add_event(tenant, m["id"], "step", {"step": step["skey"], "title": step["title"], "from": "running", "to": status,
                                        "message": msg})
    if status == "failed":
        from . import evaluator

        evaluator.escalate(tenant, m, "mission_step_failed", f"Step '{step['title']}' failed {fails} times: {err}",
                           key=f"failed:{step['id']}")


def _patch_context(tenant, mid: int, patch: dict) -> dict:
    with tenant.db() as c, db.tx(c):
        r = c.execute("SELECT context FROM missions WHERE id=?", (mid,)).fetchone()
        ctx = db.jload(r["context"] if r else None, {}) or {}
        ctx.update(patch)
        c.execute("UPDATE missions SET context=?, updated=? WHERE id=?", (db.jdump(ctx), clock(), mid))
    return ctx


# --------------------------------------------------------------------------- sync, tick
def sync(tenant, mid: int, now: float | None = None) -> int:
    """Wake steps whose drafts were all decided (a rejection has no hook) or whose recheck time has come."""
    now = now or clock()
    m = get(tenant, mid)
    if m is None or m["status"] != ACTIVE:
        return 0
    n = 0
    for s in m["steps"]:
        if s["status"] == "waiting_approval" and s["queue_ids"] and not q_states(tenant, s["queue_ids"])["open"]:
            n += _enqueue(tenant, mid, s, "drafts decided")
        elif s["status"] in ("waiting_event", "blocked_human") and s["wait"].get("recheck") and (not s.get("due") or s["due"] <= now):
            n += _enqueue(tenant, mid, s, "recheck")
    return n


def tick(tenant, now: float | None = None) -> int:
    """Per-tenant tick (scheduler.TENANT_TICKS): advance due missions, measure daily, suggest re-plans."""
    from . import evaluator

    now = now or clock()
    with tenant.db() as c:
        mids = [r["id"] for r in c.execute("SELECT id FROM missions WHERE status='active' AND (next_check IS NULL OR "
                                           "next_check<=?) ORDER BY next_check LIMIT 200", (now,))]
    for mid in mids:
        try:
            sync(tenant, mid, now)
            advance(tenant, mid, now)
            m = get(tenant, mid)
            if m and m["status"] == ACTIVE:
                evaluator.daily(tenant, m, now)
        except Exception:  # noqa: BLE001 - one mission must not stop the others
            log.exception("mission tick failed: %s", mid)
    pump(tenant, None, max_jobs=200)
    return len(mids)


tick.demo_too = True  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- hooks & events
@queue.on_approve("*")
def _on_queue_decided(tenant, item: dict, actor: str) -> None:
    try:
        meta = item.get("meta") or {}
        if meta.get("mission_id"):
            wake(tenant, int(meta["mission_id"]), sid=int(meta["step_id"]) if meta.get("step_id") else None,
                 reason="draft decided")
    except Exception:  # noqa: BLE001 - an approval must never fail because of a mission
        log.exception("mission wake on approval failed")


if hasattr(queue, "on_reject"):
    queue.on_reject("*")(_on_queue_decided)  # type: ignore[attr-defined]


def _active_for_user(tenant, uid: int | None, templates_only: tuple | None = None) -> list[int]:
    if not uid:
        return []
    q, args = "SELECT id FROM missions WHERE status='active' AND user_id=?", [uid]
    if templates_only:
        q += f" AND template IN ({','.join('?' * len(templates_only))})"
        args += list(templates_only)
    with tenant.db() as c:
        return [r["id"] for r in c.execute(q + " ORDER BY id", args)]


def _missions_for_party(tenant, p: dict) -> list[int]:
    pt = p.get("party_type") or "prospect"
    pid = p.get("party_id") or p.get("prospect_id")
    uid = p.get("owner_user_id")
    if pt == "prospect" and pid:
        with tenant.db() as c:
            r = c.execute("SELECT owner_user_id, mission_id FROM prospects WHERE id=?", (pid,)).fetchone()
        if r:
            uid = uid or r["owner_user_id"]
            if r["mission_id"]:
                with tenant.db() as c:
                    ok = c.execute("SELECT 1 FROM missions WHERE id=? AND status='active'", (r["mission_id"],)).fetchone()
                if ok:
                    return [r["mission_id"]]
    return _active_for_user(tenant, uid, templates.OUTREACH_TEMPLATES)


def _guard(fn):
    def inner(tenant, name, payload):
        try:
            fn(tenant, name, payload or {})
        except Exception:  # noqa: BLE001
            log.exception("mission event handler failed: %s", name)

    inner.__name__ = fn.__name__
    return inner


@events.on("item_sent")
@_guard
def _ev_sent(tenant, name, p):
    qid = p.get("queue_id")
    item = queue.get(tenant, int(qid), scrub=False) if qid else None
    if not item or not (item["meta"] or {}).get("mission_id"):
        return
    meta = item["meta"]
    mid = int(meta["mission_id"])
    add_event(tenant, mid, "sent", {"queue_id": item["id"], "kind": item["kind"], "channel": item["channel"],
                                    "prospect_id": meta.get("prospect_id"), "first_touch": bool(meta.get("first_touch"))},
              key=f"q:{item['id']}")
    if item["kind"] == "proposal":
        add_event(tenant, mid, "proposal", {"queue_id": item["id"], "party_id": meta.get("prospect_id")}, key=f"q:{item['id']}")
    wake(tenant, mid, event="item_sent", reason="item sent")


@events.on("reply_received")
@_guard
def _ev_reply(tenant, name, p):
    cls = (p.get("classification") or "").lower()
    positive = cls in registry.POSITIVE_REPLIES
    pt, pid = p.get("party_type") or "prospect", p.get("party_id")
    if pt not in ("prospect", "lead"):
        return
    for mid in _missions_for_party(tenant, p):
        add_event(tenant, mid, "reply", {"party_type": pt, "party_id": pid, "classification": cls or None,
                                         "positive": positive, "queue_id": p.get("queue_id")},
                  key=f"{pt}:{pid}:{'pos' if positive else 'other'}")
        wake(tenant, mid, event="reply_received", reason="reply received")


@events.on("prospect_stage")
@_guard
def _ev_stage(tenant, name, p):
    stage, pid = p.get("stage"), p.get("prospect_id")
    for mid in _missions_for_party(tenant, {"party_type": "prospect", "party_id": pid, "owner_user_id": p.get("owner_user_id")}):
        if stage == "replied":
            add_event(tenant, mid, "reply", {"party_type": "prospect", "party_id": pid, "positive": False},
                      key=f"prospect:{pid}:other")
        elif stage == "call_booked":
            add_event(tenant, mid, "booked", {"party_type": "prospect", "party_id": pid}, key=f"prospect:{pid}")
        elif stage == "won":
            mark_won(tenant, mid, client_id=None, source="prospect stage")
            continue
        elif stage in ("lost", "dnc"):
            add_event(tenant, mid, "lost" if stage == "lost" else "dnc", {"party_type": "prospect", "party_id": pid},
                      key=f"prospect:{pid}")
        wake(tenant, mid, event="prospect_stage", reason=f"prospect moved to {stage}")


@events.on("booking_made")
@_guard
def _ev_booking(tenant, name, p):
    pt, pid, starts = p.get("party_type") or "prospect", p.get("party_id"), registry.epoch(p.get("starts_at"))
    for mid in _missions_for_party(tenant, p):
        key = f"{pt}:{pid}"
        if add_event(tenant, mid, "booked", {"party_type": pt, "party_id": pid, "starts_at": starts}, key=key) is None and starts:
            with tenant.db() as c:  # a stage move recorded the booking first: add the call time
                c.execute("UPDATE mission_events SET detail=json_set(detail,'$.starts_at',?) WHERE mission_id=? AND "
                          "kind='booked' AND json_extract(detail,'$.key')=?", (starts, mid, key))
        wake(tenant, mid, event="booking_made", reason="call booked")


@events.on("booking_cancelled")
@_guard
def _ev_cancel(tenant, name, p):
    for mid in _missions_for_party(tenant, p):
        add_event(tenant, mid, "booking_cancelled", {"party_type": p.get("party_type"), "party_id": p.get("party_id")})
        wake(tenant, mid, event="booking_cancelled", reason="booking cancelled")


@events.on("client_won")
@_guard
def _ev_won(tenant, name, p):
    mids = _missions_for_party(tenant, {"party_type": "prospect", "party_id": p.get("prospect_id"),
                                        "owner_user_id": p.get("owner_user_id")}) if p.get("prospect_id") \
        else _active_for_user(tenant, p.get("owner_user_id"), templates.OUTREACH_TEMPLATES)
    for mid in mids:
        mark_won(tenant, mid, client_id=p.get("client_id"), source="client won")
    for mid in _active_for_user(tenant, p.get("owner_user_id"), ("client_onboarding", "client_weekly_ops")):
        m = get(tenant, mid, steps=False)
        if m and not (m["context"].get("answers") or {}).get("client_id") and p.get("client_id"):
            ans = {**(m["context"].get("answers") or {}), "client_id": p["client_id"]}
            _patch_context(tenant, mid, {"answers": ans, "client_id": p["client_id"]})
            wake(tenant, mid, event="client_won", reason="client added")


@events.on("certification_awarded")
@_guard
def _ev_cert(tenant, name, p):
    for mid in _active_for_user(tenant, p.get("user_id")):
        add_event(tenant, mid, "certified", {"path_key": p.get("path_key"), "level": p.get("level")},
                  key=f"{p.get('path_key')}:{p.get('level')}")
        wake(tenant, mid, event="certification_awarded", reason="certification awarded")


WIN_GOAL_TEMPLATES = ("first_client",)  # missions whose goal IS a won client; others just count the win


def mark_won(tenant, mid: int, client_id: int | None = None, value: float | None = None, source: str = "", by: str = "") -> None:
    now = clock()
    m = get(tenant, mid, steps=False)
    if m is None:
        return
    if m["template"] not in WIN_GOAL_TEMPLATES:
        # e.g. a fill-my-calendar mission: a client win is good news, but the goal is the booked calls
        add_event(tenant, mid, "won", {"client_id": client_id, "source": source, "by": by, "value": value,
                                       "goal": False}, key=f"won:{client_id or source}")
        return
    if m["context"].get("won"):
        if client_id and not m["context"].get("client_id"):
            _patch_context(tenant, mid, {"client_id": client_id})
        return
    patch = {"won": True, "won_at": now, "client_id": client_id or m["context"].get("client_id")}
    if value is not None:
        patch["recorded_revenue"] = value
    _patch_context(tenant, mid, patch)
    add_event(tenant, mid, "won", {"client_id": client_id, "source": source, "by": by, "value": value}, key="won")
    marks = ",".join("?" * len(PRE_WIN))
    with tenant.db() as c:  # checkpoints and not-yet-needed steps have nothing left to do after a win
        c.execute("UPDATE mission_steps SET status='skipped', message='Goal won before this check.', due=NULL "
                  "WHERE mission_id=? AND agent='checkpoint' AND status IN ('pending','waiting_time','ready')", (mid,))
        c.execute(f"UPDATE mission_steps SET status='skipped', message='Goal won before this step was needed.', due=NULL "
                  f"WHERE mission_id=? AND agent IN ({marks}) AND status IN ('pending','blocked_human','waiting_event')",
                  (mid, *PRE_WIN))
    wake(tenant, mid, all_waiting=True, reason="goal won")


# steps that only lead up to a win; drafts already waiting for approval are left for the person to decide
PRE_WIN = ("cert_gate", "offer_doc", "profile_assets", "prospect_list", "research_fit", "precall_prep", "take_call",
           "call_review", "human_task", "diagnose")


# --------------------------------------------------------------------------- user actions
def _step(m: dict, sid: int) -> dict:
    s = next((s for s in m["steps"] if s["id"] == sid), None)
    if s is None:
        raise MissionError(404, "Step not found")
    return s


def set_status(tenant, mid: int, action: str, actor: str) -> dict:
    m = get(tenant, mid)
    trans = {"pause": (("active",), "paused"), "resume": (("paused",), "active"), "cancel": (("active", "paused"), "cancelled")}
    allowed, to = trans[action]
    if m["status"] not in allowed:
        raise MissionError(409, f"The mission is {m['status']}")
    with tenant.db() as c:
        c.execute("UPDATE missions SET status=?, updated=? WHERE id=?", (to, clock(), mid))
        if to == "paused":
            c.execute("UPDATE mission_steps SET status='pending' WHERE mission_id=? AND status='ready'", (mid,))
    add_event(tenant, mid, action, {"by": actor})
    audit.log(tenant, actor, "run", {"mission": mid, "action": action})
    if to == "active":
        sync(tenant, mid)
        wake(tenant, mid, all_waiting=True, reason="resumed")
    return get(tenant, mid)


def update_answers(tenant, mid: int, raw: dict, actor: str, after: dict | None = None) -> dict:
    m = get(tenant, mid, steps=False)
    if m["status"] in ("done", "cancelled"):
        raise MissionError(409, f"The mission is {m['status']}")
    clean = templates.clean_answers(raw)
    if clean.get("client_id"):
        u = _user(tenant, m["user_id"]) or {}
        if not _client_ok(tenant, u, clean["client_id"]):
            raise MissionError(404, "Client not found")
    ans = {**(m["context"].get("answers") or {}), **clean}
    patch: dict = {"answers": ans}
    if clean.get("client_id"):
        patch["client_id"] = clean["client_id"]
    if after:
        patch["after_numbers"] = after
    _patch_context(tenant, mid, patch)
    add_event(tenant, mid, "context_updated", {"by": actor, "keys": sorted(clean) + (["after_numbers"] if after else [])})
    wake(tenant, mid, event="context_updated", reason="answers updated")
    return get(tenant, mid)


def retry_step(tenant, mid: int, sid: int, actor: str) -> dict:
    m = get(tenant, mid)
    s = _step(m, sid)
    if s["status"] in ("ready", "running", "pending"):
        raise MissionError(409, "This step is already queued to run")
    if s["queue_ids"] and q_states(tenant, s["queue_ids"])["open"]:
        raise MissionError(409, "Approve or reject this step's drafts in the queue first")
    t = registry.get_type(s["agent"])
    keep = {k: v for k, v in s["output"].items() if k in ("approved_total", "block", "started")} if t and t.recurring else {}
    if s["status"] in TERMINAL and not (t and t.recurring):
        raise MissionError(409, "This step is already finished")
    with tenant.db() as c:
        c.execute("UPDATE mission_steps SET status='pending', fails=0, due=NULL, output=?, message='Retrying', updated=? "
                  "WHERE id=?", (db.jdump(keep), clock(), sid))
    add_event(tenant, mid, "step", {"step": s["skey"], "title": s["title"], "from": s["status"], "to": "pending",
                                    "message": f"Retried by {actor}"})
    advance(tenant, mid)
    pump(tenant, mid)
    return get(tenant, mid)


def skip_step(tenant, mid: int, sid: int, actor: str, role: str = "owner") -> dict:
    m = get(tenant, mid)
    s = _step(m, sid)
    if s["agent"] == "intake":
        raise MissionError(409, "The intake can't be skipped: the plan needs its answers")
    if s["status"] in TERMINAL:
        raise MissionError(409, "This step is already finished")
    if s["agent"] == "cert_gate" and role != "owner":
        raise MissionError(403, "A certification gate can't be skipped: complete it in the Academy, or ask the owner")
    with tenant.db() as c:
        c.execute("UPDATE mission_steps SET status='skipped', due=NULL, message=?, updated=? WHERE id=?",
                  ("Skipped by you.", clock(), sid))
    add_event(tenant, mid, "step", {"step": s["skey"], "title": s["title"], "from": s["status"], "to": "skipped",
                                    "message": f"Skipped by {actor}"})
    advance(tenant, mid)
    pump(tenant, mid)
    return get(tenant, mid)


def mark_done(tenant, mid: int, sid: int, actor: str, outcome: str | None = None, note: str = "",
              value: float | None = None, numbers: dict | None = None, starts_at: float | None = None) -> dict:
    """The thing this step waits for happened (a reply came in, a call was booked or taken, the client said yes).
    Never sends anything and never stands in for an approval."""
    m = get(tenant, mid)
    s = _step(m, sid)
    t = registry.get_type(s["agent"])
    if s["status"] in TERMINAL:
        raise MissionError(409, "This step is already finished")
    if t is None:
        raise MissionError(409, "Unknown step")
    if t.outbound and not t.human_action:
        raise MissionError(409, "Drafts in this step go out only through the approval queue. Approve or reject them "
                                "there, or skip the step.")
    if t.recurring and not t.human_action:
        raise MissionError(409, "This step repeats until the goal is won. Skip it to stop it.")
    if not t.human_action:
        # gates, drafts and automatic steps finish on their own; "done" by hand would fake a certification or a result
        raise MissionError(409, "This step finishes when its drafts are decided in the approval queue. You can skip it "
                                "instead." if t.wait_for == "approval" else
                           "This step finishes on its own when its work is done. You can skip it instead.")
    if t.outcomes and outcome is not None and outcome not in t.outcomes:
        raise MissionError(422, f"Outcome must be one of: {', '.join(t.outcomes)}")
    outcome = outcome or (t.outcomes[0] if t.outcomes else "done")
    n = len(events_of(tenant, mid)) + 1
    detail = {"by": actor, "note": note[:300], "manual": True}
    key = s["agent"]
    if key == "reply_watch":
        add_event(tenant, mid, "reply", {**detail, "party_type": "manual", "party_id": f"manual-{n}",
                                         "positive": outcome == "positive"})
        wake(tenant, mid, event="reply_received", reason="reply recorded")
        return get(tenant, mid)
    if key == "booking":
        starts = registry.epoch(starts_at) or (value if value and value > 1e9 else None)
        add_event(tenant, mid, "booked", {**detail, "party_type": "manual", "party_id": f"manual-{n}",
                                          "starts_at": starts})
        wake(tenant, mid, event="booking_made", reason="call booked")
        return get(tenant, mid)
    if key == "take_call":
        booked = events_of(tenant, mid, "booked")
        party = {k: booked[-1]["detail"].get(k) for k in ("party_type", "party_id")} if booked else {}
        add_event(tenant, mid, {"showed": "showed", "no_show": "no_show", "rescheduled": "rescheduled"}[outcome],
                  {**detail, **party})
        wake(tenant, mid, sid=sid, reason=f"call {outcome}")
        return get(tenant, mid)
    if key == "await_win":
        if outcome == "lost":
            add_event(tenant, mid, "lost", detail)
            wake(tenant, mid, sid=sid, reason="marked lost")
        else:
            mark_won(tenant, mid, value=value, source="marked by hand", by=actor)
        return get(tenant, mid)
    if key == "remeasure":
        clean = clean_numbers(numbers)
        if len([k for k in clean if k != "offer_price"]) < 2:
            raise MissionError(422, "Enter at least two numbers to compare")
        return update_answers(tenant, mid, {}, actor, after=clean)
    if s["status"] not in WAITING:
        raise MissionError(409, "This step isn't waiting for you yet")
    if s["queue_ids"] and q_states(tenant, s["queue_ids"])["open"]:
        raise MissionError(409, "Approve or reject this step's drafts in the queue first")
    with tenant.db() as c:
        c.execute("UPDATE mission_steps SET status='done', due=NULL, message=?, updated=? WHERE id=?",
                  ("Marked done by you." + (f" {note[:200]}" if note else ""), clock(), sid))
    add_event(tenant, mid, "step", {"step": s["skey"], "title": s["title"], "from": s["status"], "to": "done",
                                    "message": f"Marked done by {actor}"})
    events.emit(tenant, "mission_step_done", {"mission_id": mid, "step_id": sid})
    advance(tenant, mid)
    pump(tenant, mid)
    return get(tenant, mid)


def clean_numbers(numbers: dict | None) -> dict:
    import math

    out = {}
    for k, v in (numbers or {}).items():
        if k not in registry.NUMBER_KEYS:
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 or v > 10_000_000:
            raise MissionError(422, f"{k.replace('_', ' ')}: enter a number of 0 or more")
        out[k] = float(v)
    return out


# --------------------------------------------------------------------------- next actions
_ORDER = {"replan": 0, "answer": 1, "fix": 2, "approve": 3, "send_by_hand": 4, "call": 5, "do": 6, "retry": 7, "waiting_owner": 8}


def next_actions(tenant, user: dict, mission_ids: list[int] | None = None) -> list[dict]:
    """"Your next actions" across the user's active missions: approve X, send Y by hand, take call Z ..."""
    mids = mission_ids if mission_ids is not None else _active_for_user(tenant, user["id"])
    out: list[dict] = []
    for mid in mids:
        sync(tenant, mid)
        pump(tenant, mid)
        m = get(tenant, mid)
        if m is None or m["status"] != ACTIVE:
            continue
        title = m.get("title") or m["goal"]
        base = {"mission_id": mid, "mission": title, "demo": bool(m.get("demo"))}
        for s in m["steps"]:
            t = registry.get_type(s["agent"])
            if s["status"] == "blocked_human":
                kind = "answer" if s["agent"] == "intake" else "call" if s["agent"] == "take_call" else "do"
                out.append({**base, "kind": kind, "step_id": s["id"], "title": s["message"], "step": s["title"],
                            "cta": "Answer" if kind == "answer" else (t.human_action if t and t.human_action else "Open"),
                            "href": s["output"].get("href"), "outcomes": list(t.outcomes) if t else []})
            elif s["status"] == "failed":
                out.append({**base, "kind": "retry", "step_id": s["id"], "title": f"{s['title']}: {s['message']}",
                            "step": s["title"], "cta": "Retry"})
            elif s["status"] == "waiting_event" and t and t.human_action and s["agent"] in ("await_win",):
                out.append({**base, "kind": "do", "step_id": s["id"], "title": s["message"], "step": s["title"],
                            "cta": t.human_action, "outcomes": list(t.outcomes)})
        with tenant.db() as c:
            items = db.rows(c.execute("SELECT id, kind, title, status, meta FROM queue WHERE json_extract(meta,'$.mission_id')=? "
                                      "AND status IN ('pending','held','needs_human','awaiting_manual_send') ORDER BY id",
                                      (mid,)))
        groups: dict[tuple, list] = {}
        for it in items:
            meta = db.jload(it["meta"], {}) or {}
            if it["status"] == "awaiting_manual_send":
                out.append({**base, "kind": "send_by_hand", "queue_id": it["id"], "title": f"Send by hand: {it['title']}",
                            "cta": "I sent it", "href": f"#/queue?ids={it['id']}"})
                continue
            mine = auth.can_decide(user["role"], it["kind"], meta.get("owner_user_id"), user["id"])
            k = ("fix" if it["status"] == "held" else "approve" if mine else "waiting_owner", meta.get("step_id"))
            groups.setdefault(k, []).append(it)
        for (kind, sid), its in groups.items():
            n = len(its)
            what = its[0]["title"] if n == 1 else f"{n} drafts from '{next((s['title'] for s in m['steps'] if s['id'] == sid), 'this mission')}'"
            title = {"fix": f"Edit and recheck: {what} (held by the guardrails)", "approve": f"Approve: {what}",
                     "waiting_owner": f"Waiting for the owner to approve: {what}"}[kind]
            out.append({**base, "kind": kind, "step_id": sid, "queue_ids": [i["id"] for i in its], "title": title,
                        "cta": None if kind == "waiting_owner" else "Open in queue",
                        "href": "#/queue?ids=" + ",".join(str(i["id"]) for i in its)})
        with tenant.db() as c:
            for r in c.execute("SELECT id, rule FROM mission_replans WHERE mission_id=? AND status='proposed'", (mid,)):
                out.append({**base, "kind": "replan", "replan_id": r["id"], "title": "Review a suggested plan change",
                            "cta": "Review"})
    out.sort(key=lambda a: (_ORDER.get(a["kind"], 9), a["mission_id"]))
    return out


def detail(tenant, m: dict, user: dict) -> dict:
    from . import ai_planner, evaluator

    steps = []
    for s in m["steps"]:
        t = registry.get_type(s["agent"])
        steps.append({"id": s["id"], "skey": s["skey"], "title": s["title"], "type": s["agent"],
                      "type_label": t.label if t else s["agent"], "status": s["status"],
                      "status_label": STATUS_LABEL.get(s["status"], s["status"]), "message": s.get("message"),
                      "depends": s["depends"], "run_id": s.get("run_id"), "queue_ids": s["queue_ids"][-50:],
                      "due": s.get("due"), "cycles": s["cycles"], "recurring": bool(t and t.recurring),
                      "outbound": bool(t and t.outbound), "wait_for": t.wait_for if t else "none",
                      "human_action": t.human_action if t else None, "outcomes": list(t.outcomes) if t else [],
                      "success": t.success if t else "", "href": s["output"].get("href"),
                      "output": _public_output(s["output"]), "updated": s.get("updated")})
    met = evaluator.metrics(tenant, m)
    with tenant.db() as c:
        reps = db.rows(c.execute("SELECT * FROM mission_replans WHERE mission_id=? ORDER BY id DESC LIMIT 20", (m["id"],)))
        kids = db.rows(c.execute("SELECT id, title, template, status FROM missions WHERE parent_id=? ORDER BY id", (m["id"],)))
    for r in reps:
        r["diff"] = db.jload(r["diff"], {})
    tpl = templates.get(m["template"])
    ctx = m["context"]
    mine = can_act(user, m)
    client = None
    cid = (ctx.get("answers") or {}).get("client_id") or ctx.get("client_id")
    if isinstance(cid, int) and not isinstance(cid, bool):
        with tenant.db() as c:
            client = db.row(c.execute("SELECT id, name FROM clients WHERE id=?", (cid,)).fetchone())
    return {**summary(tenant, m), "steps": steps, "metrics": met, "plan_vs_actual": evaluator.plan_vs_actual(m, met),
            "targets": ctx.get("targets"), "answers": ctx.get("answers") or {}, "client": client,
            "memory": {k: ctx.get(k) for k in ("icp", "offer", "opener_move", "decisions", "what_worked", "diagnosis",
                                               "next_best_action") if ctx.get(k)},
            "questions": [q.public() for q in tpl.questions] if tpl else [], "replans": reps, "children": kids,
            "events": _public_events(events_of(tenant, m["id"])[-150:]),
            "next_actions": next_actions(tenant, user, [m["id"]]) if mine and m["status"] == ACTIVE else [],
            "can_act": mine, "ai": ai_planner.availability(tenant)}


_HIDE = {"block_qids", "qids"}


def _public_output(o: dict) -> dict:
    return {k: v for k, v in (o or {}).items() if k not in _HIDE}


_EVENT_LABEL = {"created": "Mission started", "step": "Step", "sent": "Sent", "reply": "Reply", "booked": "Call booked",
                "showed": "Call showed", "no_show": "No-show", "won": "Won", "lost": "Marked lost",
                "context_updated": "Answers updated", "replan_proposed": "Plan change suggested",
                "replan_applied": "Plan change applied", "replan_dismissed": "Plan change dismissed",
                "escalated": "Escalated to a coach", "pause": "Paused", "resume": "Resumed", "cancel": "Cancelled",
                "mission_done": "Mission complete", "spawned": "Follow-on mission started", "certified": "Certified",
                "proposal": "Proposal sent", "booking_cancelled": "Booking cancelled", "note": "Note", "dnc": "Do not contact",
                "rescheduled": "Call rescheduled"}


_KIND_TEXT = {"dm_opener": "Opener", "prospect_email": "Opener email", "prospect_dm": "Message", "followup": "Follow-up",
              "booking_dm": "Booking message", "reminder": "Reminder", "proposal": "Proposal", "client_report": "Client report",
              "mission_draft": "Draft"}


def _event_text(kind: str, d: dict) -> str:
    if kind == "step":
        return f"{d.get('title', '')}: {STATUS_LABEL.get(d.get('to'), d.get('to'))}. {d.get('message') or ''}".strip()
    if kind == "sent":
        return f"{_KIND_TEXT.get(d.get('kind'), 'Message')} sent" + (" (first message to this prospect)" if d.get("first_touch") else "")
    if kind == "reply":
        return ("Positive reply" if d.get("positive") else "Reply received") + (" (recorded by hand)" if d.get("manual") else "")
    if kind == "booked":
        return "Call booked" + (f" for {registry.when(d['starts_at'])}" if d.get("starts_at") else "")
    if kind in ("showed", "no_show", "rescheduled", "lost"):
        return {"showed": "They showed up for the call", "no_show": "They didn't show", "rescheduled": "Call rescheduled",
                "lost": "Marked as lost"}[kind] + (f": {d['note']}" if d.get("note") else "")
    if kind == "won":
        return "Client won" + (f" (your recorded deal value: ${d['value']:,.0f})" if d.get("value") else "")
    if kind == "certified":
        return f"Certified: {registry.PATH_LABEL.get(d.get('path_key'), d.get('path_key'))} {d.get('level') or ''}".strip()
    if kind in ("pause", "resume", "cancel"):
        return f"By {d.get('by') or 'you'}"
    if kind == "context_updated":
        return "Answers updated"
    if kind == "spawned":
        return f"Started mission #{d.get('child_id')}"
    return d.get("message") or d.get("goal") or ""


def _public_events(evs: list[dict]) -> list[dict]:
    out = []
    for e in sorted(evs, key=lambda x: (x["created"], x["id"]), reverse=True):
        d = e["detail"]
        if e["kind"] == "step" and d.get("to") == "ready":
            continue  # internal scheduling noise
        text = _event_text(e["kind"], d)
        out.append({"id": e["id"], "kind": e["kind"], "label": _EVENT_LABEL.get(e["kind"], e["kind"].replace("_", " ")),
                    "text": text[:400], "at": e["created"], "run_id": d.get("run_id"), "queue_ids": d.get("queue_ids") or []})
    return out
