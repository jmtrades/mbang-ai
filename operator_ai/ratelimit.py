"""A small in-process rate limiter for endpoints anyone can reach: the pitch demo's one-click sign-in and
reset, and the public lead-magnet minis.

Sliding window per (bucket, key). Counters are namespaced by the data directory, so every workspace (and
every test) starts clean. The app runs as one process (docs/ARCHITECTURE.md), so in-memory state is enough;
a restart clears it, which is acceptable for abuse limits (the persistent per-email limits for minis live in
the database). Client IPs come from uvicorn's --proxy-headers (the deploy bundle sets it)."""
from __future__ import annotations

import os
import threading
import time
from collections import deque

from . import config

_lock = threading.Lock()
_hits: dict[tuple[str, str, str], deque] = {}
MAX_KEYS = 50_000


class Limited(Exception):
    def __init__(self, retry_after: int, message: str = "Too many requests. Try again later."):
        super().__init__(message)
        self.retry_after = max(1, int(retry_after))


def limit(name: str, default: int) -> int:
    """Limits can be tuned on the server: OPERATOR_LIMIT_<NAME> (e.g. OPERATOR_LIMIT_DEMO_LOGIN=40)."""
    try:
        return max(1, int(os.environ.get(f"OPERATOR_LIMIT_{name.upper().replace('-', '_')}", default)))
    except ValueError:
        return default


def _prune(now: float) -> None:
    for k in [k for k, dq in _hits.items() if not dq or dq[-1] < now - 86400]:
        _hits.pop(k, None)


def hit(bucket: str, key: str, max_hits: int, window: float, message: str | None = None) -> None:
    """Count one hit; raise Limited when the key already has max_hits inside the window."""
    now = time.time()
    k = (str(config.get().data_dir), bucket, str(key or "?"))
    with _lock:
        dq = _hits.get(k)
        if dq is None:
            if len(_hits) >= MAX_KEYS:
                _prune(now)
            dq = _hits[k] = deque()
        while dq and dq[0] <= now - window:
            dq.popleft()
        if len(dq) >= max_hits:
            raise Limited(dq[0] + window - now, message or "Too many requests. Try again later.")
        dq.append(now)


def clear() -> None:
    with _lock:
        _hits.clear()
