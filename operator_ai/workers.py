"""Background worker threads for the job queue, plus the schedule ticker for workers/triggers."""
from __future__ import annotations

import logging
import threading

from . import config, jobs, tenancy

log = logging.getLogger("operator.workers")

_threads: list[threading.Thread] = []
_stop = threading.Event()
_watched: dict[str, tenancy.Tenant] = {}
_lock = threading.Lock()
_wake = threading.Event()


def watch(tenant) -> None:
    with _lock:
        _watched[tenant.id] = tenant
    _wake.set()


def running() -> int:
    return sum(1 for t in _threads if t.is_alive())


def _loop() -> None:
    while not _stop.is_set():
        did = False
        with _lock:
            tenants = list(_watched.values())
        for t in tenants:
            try:
                job = jobs.claim(t)
            except Exception:  # noqa: BLE001 - tenant DB may be mid-reset
                continue
            if job:
                jobs.run_one(t, job)
                did = True
        if not did:
            _wake.wait(0.25)
            _wake.clear()


def _ticker() -> None:
    from . import scheduler

    while not _stop.is_set():
        try:
            scheduler.tick()
        except Exception as e:  # noqa: BLE001
            log.warning("scheduler tick failed: %s", type(e).__name__)
        _stop.wait(30)


def start(n: int | None = None) -> None:
    if running():
        return
    _stop.clear()
    for t in tenancy.list_all():
        jobs.requeue_orphans(t)
        watch(t)
    n = n or config.get().workers
    for i in range(n):
        th = threading.Thread(target=_loop, name=f"worker-{i}", daemon=True)
        th.start()
        _threads.append(th)
    try:
        from . import scheduler  # noqa: F401

        th = threading.Thread(target=_ticker, name="ticker", daemon=True)
        th.start()
        _threads.append(th)
    except ModuleNotFoundError:
        pass


def stop() -> None:
    _stop.set()
    _wake.set()
    for th in _threads:
        th.join(timeout=2)
    _threads.clear()
    with _lock:
        _watched.clear()
