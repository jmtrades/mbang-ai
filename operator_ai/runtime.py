"""Shared agent runtime: Run(plan) -> steps (tool, input, output, ms, status) -> result.

Every step is written to SQLite as it happens so the UI can poll the run (~600 ms) and show
steps ticking. Outputs are scrubbed of banned names and redacted of secrets before storage.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from . import db, guardrails
from .redact import redact

log = logging.getLogger("operator.runtime")
_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="run")
MAX_FIELD = 6000


def _clean(tenant, obj: Any) -> Any:
    """Scrub + redact + truncate any JSON-able value for the trace."""
    names = guardrails.tenant_banned_names(tenant)
    try:
        s = json.dumps(obj, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        s = json.dumps(str(obj))
    s = redact(guardrails.scrub(s, names)) if names else redact(s)
    if len(s) > MAX_FIELD:
        return {"truncated": True, "preview": s[:MAX_FIELD]}
    try:
        return json.loads(s)
    except ValueError:
        return s


class Run:
    def __init__(self, tenant, agent: str, title: str, plan: list[str], actor: str = "system", run_id: int | None = None):
        self.tenant = tenant
        self.agent = agent
        self.title = title
        self.plan = plan
        self.steps: list[dict] = []
        self.ai_mode = "rules"
        self.t0 = time.time()
        self._lock = threading.Lock()
        if run_id is None:
            with tenant.db() as c:
                self.id = c.execute(
                    "INSERT INTO runs(agent,title,plan,steps,result,status,ms,ai_mode,created,actor) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (agent, title[:300], db.jdump(plan), "[]", None, "running", 0, "rules", db.now(), actor),
                ).lastrowid
        else:
            self.id = run_id

    def _save(self, status: str = "running", result: Any = None) -> None:
        with self.tenant.db() as c:
            c.execute("UPDATE runs SET steps=?, status=?, ms=?, ai_mode=?, result=COALESCE(?, result) WHERE id=?",
                      (db.jdump(self.steps), status, int((time.time() - self.t0) * 1000), self.ai_mode,
                       db.jdump(_clean(self.tenant, result)) if result is not None else None, self.id))

    def use_ai(self, mode: str) -> None:
        """Record that a step used live AI or a replay ('live' beats 'replay' beats 'rules')."""
        order = {"rules": 0, "replay": 1, "live": 2}
        if order.get(mode, 0) > order.get(self.ai_mode, 0):
            self.ai_mode = mode

    def step(self, tool: str, input: Any, fn: Callable[[], Any], label: str | None = None, allow_fail: bool = False) -> Any:
        entry = {"tool": tool, "label": label or tool, "input": _clean(self.tenant, input), "output": None, "ms": None,
                 "status": "running"}
        with self._lock:
            self.steps.append(entry)
            self._save()
        t = time.time()
        try:
            out = fn()
        except Exception as e:  # noqa: BLE001
            entry.update(status="error", ms=int((time.time() - t) * 1000), output={"error": redact(f"{type(e).__name__}: {e}")})
            with self._lock:
                self._save()
            if allow_fail:
                return None
            raise
        entry.update(status="ok", ms=int((time.time() - t) * 1000), output=_clean(self.tenant, out))
        with self._lock:
            self._save()
        return out

    def note(self, tool: str, input: Any, output: Any, status: str = "ok", label: str | None = None, ms: int = 0) -> None:
        with self._lock:
            self.steps.append({"tool": tool, "label": label or tool, "input": _clean(self.tenant, input),
                               "output": _clean(self.tenant, output), "ms": ms, "status": status})
            self._save()

    def finish(self, result: Any) -> Any:
        with self._lock:
            self._save("done", result)
        return result

    def fail(self, err: Exception | str) -> None:
        msg = redact(str(err) if isinstance(err, str) else f"{type(err).__name__}: {err}")
        with self._lock:
            self._save("error", {"error": msg})


def get(tenant, run_id: int) -> dict | None:
    with tenant.db() as c:
        r = db.row(c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
    if r is None:
        return None
    for k in ("plan", "steps", "result"):
        r[k] = db.jload(r[k], [] if k != "result" else None)
    return r


def recent(tenant, limit: int = 50, agent: str | None = None) -> list[dict]:
    with tenant.db() as c:
        if agent:
            rows = db.rows(c.execute("SELECT id,agent,title,status,ms,ai_mode,created,actor FROM runs WHERE agent=? "
                                     "ORDER BY id DESC LIMIT ?", (agent, limit)))
        else:
            rows = db.rows(c.execute("SELECT id,agent,title,status,ms,ai_mode,created,actor FROM runs ORDER BY id DESC LIMIT ?",
                                     (limit,)))
    return rows


def launch(tenant, agent: str, title: str, plan: list[str], fn: Callable[["Run"], Any], actor: str = "system") -> int:
    """Create the run row now and execute fn(run) in the background. Returns the run id."""
    run = Run(tenant, agent, title, plan, actor)

    def _go():
        try:
            res = fn(run)
            if res is not None:
                run.finish(res)
        except Exception as e:  # noqa: BLE001
            log.warning("run %s failed: %s", run.id, redact(e))
            log.debug(redact(traceback.format_exc()))
            run.fail(e)

    _pool.submit(_go)
    return run.id


def execute(tenant, agent: str, title: str, plan: list[str], fn: Callable[["Run"], Any], actor: str = "system") -> dict:
    """Run synchronously; return {run_id, result}."""
    run = Run(tenant, agent, title, plan, actor)
    try:
        res = fn(run)
        run.finish(res)
        return {"run_id": run.id, "result": res, "ai_mode": run.ai_mode}
    except Exception as e:
        run.fail(e)
        raise
