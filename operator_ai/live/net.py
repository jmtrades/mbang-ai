"""Outbound HTTP for the live layer.

* Backoff with jitter on 429, 5xx and connection errors, honouring Retry-After.
* Meta rate-limit error codes (4, 17, 32, 613) are retried.
* POSTs are never blindly retried: only when the caller marks them idempotent (e.g. an
  event insert with a deterministic id) or the request provably never reached the server.
* A client-side token bucket per (tenant, host).
* Every error is redacted. Tests swap the transport for in-process fakes.
"""
from __future__ import annotations

import json
import random
import threading
import time
from urllib.parse import urlparse

import httpx

from ..redact import redact

redact = redact  # re-exported: live code imports redact from here
_transport: httpx.BaseTransport | None = None
META_RATE_CODES = {4, 17, 32, 613}
MAX_ATTEMPTS = 4
_sleep = time.sleep


def set_transport(t: httpx.BaseTransport | None) -> None:
    global _transport
    _transport = t


class LiveError(Exception):
    def __init__(self, msg: str, status: int | None = None, body: dict | None = None):
        super().__init__(redact(msg))
        self.status = status
        self.body = body or {}


class TokenBucket:
    def __init__(self, rate: float = 5.0, burst: int = 10):
        self.rate, self.burst = rate, burst
        self.tokens = float(burst)
        self.t = time.monotonic()
        self.lock = threading.Lock()

    def take(self) -> float:
        """Take a token; returns how long the caller waited."""
        with self.lock:
            now = time.monotonic()
            self.tokens = min(self.burst, self.tokens + (now - self.t) * self.rate)
            self.t = now
            if self.tokens >= 1:
                self.tokens -= 1
                return 0.0
            wait = (1 - self.tokens) / self.rate
            self.tokens = 0.0
            self.t = now + wait
        _sleep(wait)
        return wait


_buckets: dict[tuple[str, str], TokenBucket] = {}
_blk = threading.Lock()


def bucket(tenant_id: str, host: str) -> TokenBucket:
    with _blk:
        return _buckets.setdefault((tenant_id, host), TokenBucket())


def _retry_after(r: httpx.Response) -> float | None:
    v = r.headers.get("retry-after")
    if not v:
        return None
    try:
        return min(60.0, float(v))
    except ValueError:
        return None


def _meta_code(r: httpx.Response) -> int | None:
    try:
        return int((r.json().get("error") or {}).get("code"))
    except (ValueError, TypeError, AttributeError):
        return None


def request(tenant_id: str, method: str, url: str, *, params: dict | None = None, json_body: dict | None = None,
            data: dict | None = None, headers: dict | None = None, idempotent: bool | None = None,
            timeout: float = 20.0) -> httpx.Response:
    method = method.upper()
    retry_ok = idempotent if idempotent is not None else method in ("GET", "HEAD", "PUT", "DELETE")
    host = urlparse(url).netloc
    last: str = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        bucket(tenant_id, host).take()
        try:
            with httpx.Client(transport=_transport, timeout=timeout) as c:
                r = c.request(method, url, params=params, json=json_body, data=data, headers=headers)
        except httpx.ConnectError as e:
            # the request never reached the server, so even a POST is safe to retry
            last = f"connection error {type(e).__name__}"
            if attempt == MAX_ATTEMPTS:
                break
            _sleep(_backoff(attempt))
            continue
        except httpx.TransportError as e:
            last = f"transport error {type(e).__name__}"
            if not retry_ok or attempt == MAX_ATTEMPTS:
                break
            _sleep(_backoff(attempt))
            continue
        retryable = r.status_code == 429 or r.status_code >= 500 or (
            r.status_code in (400, 403) and _meta_code(r) in META_RATE_CODES)
        if retryable and attempt < MAX_ATTEMPTS and (retry_ok or r.status_code == 429):
            last = f"HTTP {r.status_code}"
            ra = _retry_after(r)
            _sleep(ra if ra is not None else _backoff(attempt))
            continue
        return r
    raise LiveError(f"{method} {host} failed after retries: {last}")


def _backoff(attempt: int) -> float:
    return min(8.0, 0.25 * 2 ** (attempt - 1)) * (0.5 + random.random())  # full jitter


def ok_json(r: httpx.Response) -> dict:
    if r.status_code >= 400:
        try:
            body = r.json()
        except ValueError:
            body = {}
        raise LiveError(f"HTTP {r.status_code}: {json.dumps(body)[:300]}", r.status_code, body)
    try:
        return r.json()
    except ValueError:
        raise LiveError("Non-JSON response", r.status_code)
