"""Google Calendar: OAuth refresh token (scopes calendar.events + calendar.freebusy),
freeBusy for real free slots, and event inserts with a deterministic event id so a retry can
never create a duplicate (a 409 is read back)."""
from __future__ import annotations

import base64
import hashlib
import threading
import time

from .. import calendar, vault
from . import net

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/calendar/v3"
SCOPES = ["https://www.googleapis.com/auth/calendar.events", "https://www.googleapis.com/auth/calendar.freebusy"]
_cache: dict[str, tuple[str, float]] = {}
_lock = threading.Lock()


def access_token(tenant) -> str:
    with _lock:
        tok = _cache.get(tenant.id)
        if tok and tok[1] > time.time() + 60:
            return tok[0]
    cid, secret, refresh = (vault.get(tenant, k) for k in ("google_client_id", "google_client_secret", "google_refresh_token"))
    if not (cid and secret and refresh):
        raise net.LiveError("Google login missing (client id / secret / refresh token)")
    r = net.request(tenant.id, "POST", TOKEN_URL, data={"grant_type": "refresh_token", "refresh_token": refresh,
                                                        "client_id": cid, "client_secret": secret},
                    idempotent=True)  # refreshing a token is safe to repeat
    d = net.ok_json(r)
    with _lock:
        _cache[tenant.id] = (d["access_token"], time.time() + int(d.get("expires_in", 3600)))
    return d["access_token"]


def _cal(tenant) -> str:
    return vault.get(tenant, "google_calendar_id") or "primary"


def busy(tenant, start: float, end: float) -> list[tuple[float, float]]:
    tok = access_token(tenant)
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
    r = net.request(tenant.id, "POST", f"{API}/freeBusy", headers={"authorization": f"Bearer {tok}"},
                    json_body={"timeMin": iso(start), "timeMax": iso(end), "items": [{"id": _cal(tenant)}]},
                    idempotent=True)  # a read, even though it's a POST
    d = net.ok_json(r)
    out = []
    for b in (d.get("calendars", {}).get(_cal(tenant), {}) or {}).get("busy", []):
        out.append((_parse(b["start"]), _parse(b["end"])))
    return out


def _parse(s: str) -> float:
    import calendar as stdcal

    return float(stdcal.timegm(time.strptime(s.replace("Z", "")[:19], "%Y-%m-%dT%H:%M:%S")))


def free_slots(tenant, n: int = 3) -> list[dict]:
    return calendar.free_slots(tenant, n, busy_fn=lambda s, e: busy(tenant, s, e))


def event_id(*parts) -> str:
    """Deterministic id in Google's allowed alphabet (base32hex, lowercase)."""
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
    return base64.b32hexencode(h).decode().lower().rstrip("=")[:40]


def insert_event(tenant, eid: str, summary: str, start: float, end: float, attendees: list[str]) -> dict:
    tok = access_token(tenant)
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
    body = {"id": eid, "summary": summary, "start": {"dateTime": iso(start)}, "end": {"dateTime": iso(end)},
            "attendees": [{"email": a} for a in attendees]}
    r = net.request(tenant.id, "POST", f"{API}/calendars/{_cal(tenant)}/events", headers={"authorization": f"Bearer {tok}"},
                    params={"sendUpdates": "all"}, json_body=body, idempotent=True)  # deterministic id: retry-safe
    if r.status_code == 409:
        g = net.request(tenant.id, "GET", f"{API}/calendars/{_cal(tenant)}/events/{eid}",
                        headers={"authorization": f"Bearer {tok}"})
        return {**net.ok_json(g), "existing": True}
    return net.ok_json(r)
