"""Local SQLite calendar (default). Google freeBusy is used only when the tenant's live
calendar switch is on (see live/gcal.py)."""
from __future__ import annotations

import time

from . import db

SLOT_MIN = 45
DAY_START, DAY_END = 9, 17


def busy(tenant, start: float, end: float) -> list[tuple[float, float]]:
    with tenant.db() as c:
        return [(r["start"], r["end"]) for r in c.execute(
            "SELECT start, end FROM calendar_events WHERE end > ? AND start < ? AND status != 'cancelled'", (start, end))]


def free_slots(tenant, n: int = 3, duration_min: int = SLOT_MIN, after: float | None = None,
               busy_fn=None) -> list[dict]:
    """Next n free slots on weekdays between 09:00 and 17:00, starting tomorrow."""
    after = after or time.time()
    lt = time.localtime(after + 86400)
    day0 = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    window_end = day0 + 14 * 86400
    blocks = (busy_fn or (lambda s, e: busy(tenant, s, e)))(day0, window_end)
    out = []
    for d in range(14):
        day = day0 + d * 86400
        if time.localtime(day).tm_wday >= 5:
            continue
        t = time.mktime(time.localtime(day)[:3] + (DAY_START, 0, 0, 0, 0, -1))
        end_of_day = time.mktime(time.localtime(day)[:3] + (DAY_END, 0, 0, 0, 0, -1))
        while t + duration_min * 60 <= end_of_day:
            s, e = t, t + duration_min * 60
            if not any(bs < e and be > s for bs, be in blocks):
                out.append({"start": s, "end": e, "label": time.strftime("%a %d %b %H:%M", time.localtime(s))})
                if len(out) >= n:
                    return out
                t = e + 3600  # spread proposals out
                continue
            t += 30 * 60
    return out


def add_event(tenant, title: str, start: float, end: float, attendees: list[str], status: str = "confirmed",
              ext_id: str | None = None, demo: bool = False) -> int:
    with tenant.db() as c:
        return c.execute("INSERT INTO calendar_events(title,start,end,attendees,status,ext_id,demo,created) VALUES(?,?,?,?,?,?,?,?)",
                         (title, start, end, db.jdump(attendees), status, ext_id, 1 if demo else 0, db.now())).lastrowid
