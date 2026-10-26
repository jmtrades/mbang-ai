"""Durable SQLite job queue: idempotency keys, exponential backoff (max 3 retries),
orphaned running jobs re-queued on start."""
from __future__ import annotations

import logging
import time
import traceback
from typing import Callable

from . import db
from .redact import redact

log = logging.getLogger("operator.jobs")

MAX_RETRIES = 3
BACKOFF_BASE = 0.5  # seconds; attempt n waits BACKOFF_BASE * 2**n

HANDLERS: dict[str, Callable] = {}


def handler(kind: str):
    def deco(fn):
        HANDLERS[kind] = fn
        return fn

    return deco


def enqueue(tenant, kind: str, payload: dict, key: str | None = None, batch: str | None = None,
            delay: float = 0.0) -> int | None:
    """Insert a job. A repeated idempotency key returns the existing job id."""
    from . import workers

    t = db.now()
    with tenant.db() as c:
        if key:
            r = c.execute("SELECT id FROM jobs WHERE key=?", (key,)).fetchone()
            if r:
                return r["id"]
        cur = c.execute(
            "INSERT OR IGNORE INTO jobs(key,kind,payload,status,attempts,next_at,created,updated,batch) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (key, kind, db.jdump(payload), "queued", 0, t + delay, t, t, batch),
        )
        jid = cur.lastrowid
    workers.watch(tenant)
    return jid


def enqueue_many(tenant, kind: str, items: list[tuple[str | None, dict]], batch: str) -> int:
    from . import workers

    t = db.now()
    with tenant.db() as c, db.tx(c):
        c.executemany(
            "INSERT OR IGNORE INTO jobs(key,kind,payload,status,attempts,next_at,created,updated,batch) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            [(k, kind, db.jdump(p), "queued", 0, t, t, t, batch) for k, p in items],
        )
    workers.watch(tenant)
    return len(items)


def claim(tenant) -> dict | None:
    t = db.now()
    with tenant.db() as c:
        r = c.execute(
            "UPDATE jobs SET status='running', updated=? WHERE id=(SELECT id FROM jobs WHERE status='queued' "
            "AND next_at<=? ORDER BY next_at, id LIMIT 1) AND status='queued' RETURNING *",
            (t, t),
        ).fetchone()
        return db.row(r)


def run_one(tenant, job: dict) -> None:
    from . import handlers

    handlers.load()
    fn = HANDLERS.get(job["kind"])
    try:
        if fn is None:
            raise RuntimeError(f"no handler for {job['kind']}")
        result = fn(tenant, db.jload(job["payload"], {}))
        with tenant.db() as c:
            c.execute("UPDATE jobs SET status='done', result=?, updated=? WHERE id=?",
                      (db.jdump(result), db.now(), job["id"]))
    except Exception as e:  # noqa: BLE001 - every failure is recorded and retried
        attempts = job["attempts"] + 1
        err = redact(f"{type(e).__name__}: {e}")
        log.warning("job %s failed (attempt %s): %s", job["id"], attempts, err)
        log.debug(redact(traceback.format_exc()))
        with tenant.db() as c:
            if attempts > MAX_RETRIES:
                c.execute("UPDATE jobs SET status='failed', attempts=?, error=?, updated=? WHERE id=?",
                          (attempts, err, db.now(), job["id"]))
            else:
                c.execute("UPDATE jobs SET status='queued', attempts=?, error=?, next_at=?, updated=? WHERE id=?",
                          (attempts, err, db.now() + BACKOFF_BASE * (2 ** attempts), db.now(), job["id"]))


def drain(tenant, timeout: float = 60.0) -> int:
    """Run queued jobs inline until none are due (used by tests and CLI scripts)."""
    n = 0
    end = time.time() + timeout
    while time.time() < end:
        job = claim(tenant)
        if job is None:
            with tenant.db() as c:
                pending = c.execute("SELECT MIN(next_at) FROM jobs WHERE status='queued'").fetchone()[0]
            if pending is None:
                break
            time.sleep(max(0.0, min(0.2, pending - db.now())))
            continue
        run_one(tenant, job)
        n += 1
    return n


def requeue_orphans(tenant) -> int:
    with tenant.db() as c:
        return c.execute("UPDATE jobs SET status='queued', next_at=? WHERE status='running'", (db.now(),)).rowcount


def batch_status(tenant, batch: str) -> dict:
    with tenant.db() as c:
        counts = {r["status"]: r["n"] for r in c.execute(
            "SELECT status, COUNT(*) n FROM jobs WHERE batch=? GROUP BY status", (batch,))}
    total = sum(counts.values())
    return {"batch": batch, "total": total, "done": counts.get("done", 0), "failed": counts.get("failed", 0),
            "queued": counts.get("queued", 0), "running": counts.get("running", 0)}
