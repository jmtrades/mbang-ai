"""Domain events: one module tells the others that something happened, without importing them.

    from . import events
    events.emit(tenant, "reply_received", {"prospect_id": 7, "classification": "positive"})

    @events.on("reply_received")
    def wake_mission(tenant, name, payload): ...

Every event is written to app_events first (so a mission engine or report can read history), then
subscribers run in process. A failing subscriber is logged and never stops the emitter or the others.
Known names (add yours here): item_sent, reply_received, prospect_stage, client_won, booking_made,
booking_cancelled, certification_awarded, mission_step_done, dnc_set (reason: bounce | complaint when it came from
an email report), unsubscribed, cadence_done, escalation_raised, roleplay_completed, roleplay_signed_off.
"""
from __future__ import annotations

import logging

from . import db

log = logging.getLogger("operator.events")
_SUBS: dict[str, list] = {}


def on(name: str):
    """Subscribe to an event name ("*" for all)."""
    def deco(fn):
        _SUBS.setdefault(name, []).append(fn)
        return fn

    return deco


def emit(tenant, name: str, payload: dict | None = None) -> int:
    payload = dict(payload or {})
    with tenant.db() as c:
        eid = c.execute("INSERT INTO app_events(name,payload,created) VALUES(?,?,?)",
                        (name, db.jdump(payload), db.now())).lastrowid
    from . import handlers

    handlers.load()
    for fn in _SUBS.get(name, []) + _SUBS.get("*", []):
        try:
            fn(tenant, name, payload)
        except Exception:  # noqa: BLE001 - one subscriber must not break the emitter
            from .redact import redact

            log.exception("event subscriber failed: %s", redact(f"{name} -> {getattr(fn, '__name__', fn)}"))
    return eid


def recent(tenant, name: str | None = None, since: float = 0, limit: int = 200) -> list[dict]:
    q, args = "SELECT * FROM app_events WHERE created>=?", [since]
    if name:
        q += " AND name=?"
        args.append(name)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with tenant.db() as c:
        return [{"id": r["id"], "name": r["name"], "payload": db.jload(r["payload"], {}), "created": r["created"]}
                for r in c.execute(q, args)]
