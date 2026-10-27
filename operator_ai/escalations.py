"""Escalations: things a person must look at, with an owner, a due time and a status.

    from . import escalations
    eid = escalations.raise_(tenant, "speed_to_lead", "high", "A positive reply has waited 60 minutes",
                             assignee_user_id=7, ref="inbound:12")

raise_ is idempotent per (kind, ref) while the escalation is still open or acknowledged, so SLA checks
that run every 30 seconds never stack duplicates. Each new escalation is written here (the in-app
notice), emits escalation_raised, and - when the owner switched it on and the person has linked their own
Telegram - drafts an internal alert to that chat. The alert is a queue item like any other: nothing
leaves without approval, and it never goes to a prospect.

Also here: crash recovery for background runs (runs still "running" ten minutes after a restart become
"interrupted") and the helper the missions engine calls when a step keeps failing.
"""
from __future__ import annotations

import logging
import time

from . import audit, db

log = logging.getLogger("operator.escalations")

KINDS = {
    "speed_to_lead": "Positive reply waiting for an answer",
    "held_item": "Draft held by guardrails for over a day",
    "step_failed": "Work keeps failing",
    "kpi_below_plan": "Goal well behind plan",
    "legal": "Legal threat",
    "refund": "Refund request",
    "complaint": "Complaint",
    "dnc_request": "Asked not to be contacted",
    "runs_interrupted": "Work interrupted by a restart",
    "other": "Needs a person",
}
SEVERITIES = ("low", "medium", "high")
STATUSES = ("open", "acked", "resolved")
OPEN_STATES = ("open", "acked")
DUE_IN = {"high": 3600, "medium": 4 * 3600, "low": 24 * 3600}
# Legal threats, refund requests and complaints are about the programme itself: whoever they are assigned to
# can mark them as seen, but only the owner can close them (so they never vanish from the owner's list).
OWNER_RESOLVES = {"legal", "refund", "complaint"}


class EscalationError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


# --------------------------------------------------------------------------- core
def raise_(tenant, kind: str, severity: str, detail: str, assignee_user_id: int | None = None,
           mission_id: int | None = None, ref: str | None = None) -> int:
    """Create (or return the still-open) escalation for this kind + ref. Returns its id."""
    kind = kind if kind in KINDS else "other"
    severity = severity if severity in SEVERITIES else "medium"
    detail = (detail or KINDS[kind]).strip()[:1000]
    ref = (str(ref)[:120] if ref is not None else None)
    now = db.now()
    with tenant.db() as c:
        if ref is not None:
            r = c.execute("SELECT id FROM escalations WHERE kind=? AND ref=? AND status IN ('open','acked') "
                          "ORDER BY id DESC LIMIT 1", (kind, ref)).fetchone()
            if r is not None:
                return int(r["id"])
        eid = c.execute(
            "INSERT INTO escalations(mission_id,kind,severity,detail,assignee_user_id,status,due,ref,created,demo) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (mission_id, kind, severity, detail, assignee_user_id, "open", now + DUE_IN[severity], ref, now,
             1 if tenant.is_demo else 0)).lastrowid
    audit.log(tenant, "escalations", "escalation", {"id": eid, "kind": kind, "severity": severity})
    from . import events

    events.emit(tenant, "escalation_raised", {"id": eid, "kind": kind, "severity": severity,
                                              "assignee_user_id": assignee_user_id, "mission_id": mission_id})
    try:
        _telegram_alert(tenant, eid, kind, severity, detail, assignee_user_id)
    except Exception:  # noqa: BLE001 - an alert draft must never stop the escalation itself
        from .redact import redact

        log.warning("escalation alert draft failed: %s", redact(f"{tenant.id} {eid}"))
    return int(eid)


def get(tenant, eid: int) -> dict | None:
    with tenant.db() as c:
        r = db.row(c.execute("SELECT * FROM escalations WHERE id=?", (eid,)).fetchone())
    return _public(r) if r else None


def _public(r: dict) -> dict:
    r = dict(r)
    r["label"] = KINDS.get(r["kind"], KINDS["other"])
    r["overdue"] = bool(r["status"] in OPEN_STATES and r["due"] and r["due"] < db.now())
    return r


def visible_user_ids(tenant, user: dict) -> set[int] | None:
    """None = everything (owner). A coach also sees escalations for their students' accounts."""
    if user["role"] == "owner":
        return None
    ids = {int(user["id"])}
    if user["role"] == "coach":
        with tenant.db() as c:
            ids |= {int(r["id"]) for r in c.execute(
                "SELECT u.id FROM users u JOIN students s ON s.id=u.student_id WHERE s.coach_id=?", (user["id"],))}
    return ids


def can_resolve(user: dict, esc: dict) -> bool:
    return user["role"] == "owner" or esc.get("kind") not in OWNER_RESOLVES


def list_for(tenant, user: dict, status: str = "open", limit: int = 200) -> list[dict]:
    q, args = "SELECT * FROM escalations WHERE 1=1", []
    if status == "open":
        q += " AND status IN ('open','acked')"
    elif status in STATUSES:
        q += " AND status=?"
        args.append(status)
    ids = visible_user_ids(tenant, user)
    if ids is not None:
        q += f" AND assignee_user_id IN ({','.join('?' * len(ids))})"
        args += sorted(ids)
    q += " ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, id DESC LIMIT ?"
    args.append(limit)
    with tenant.db() as c:
        out = [_public(r) for r in db.rows(c.execute(q, args))]
    for e in out:
        e["can_resolve"] = can_resolve(user, e)
    return out


def counts(tenant, user: dict) -> dict:
    items = list_for(tenant, user, "open", 2000)
    return {"open": len(items), "high": sum(1 for i in items if i["severity"] == "high"),
            "overdue": sum(1 for i in items if i["overdue"])}


def _may_decide(tenant, user: dict, esc: dict) -> bool:
    ids = visible_user_ids(tenant, user)
    return ids is None or (esc.get("assignee_user_id") is not None and int(esc["assignee_user_id"]) in ids)


def ack(tenant, eid: int, user: dict, actor: str) -> dict:
    e = get(tenant, eid)
    if e is None or not _may_decide(tenant, user, e):
        raise EscalationError(404, "Not found")
    if e["status"] != "open":
        raise EscalationError(409, f"Already {'acknowledged' if e['status'] == 'acked' else 'resolved'}")
    with tenant.db() as c:
        c.execute("UPDATE escalations SET status='acked', acked_by=?, acked_at=? WHERE id=?", (actor, db.now(), eid))
    audit.log(tenant, actor, "escalation.ack", {"id": eid})
    return get(tenant, eid)


def resolve(tenant, eid: int, user: dict | None, actor: str, note: str = "") -> dict:
    e = get(tenant, eid)
    if e is None or (user is not None and not _may_decide(tenant, user, e)):
        raise EscalationError(404, "Not found")
    if e["status"] == "resolved":
        raise EscalationError(409, "Already resolved")
    if user is not None and not can_resolve(user, e):
        raise EscalationError(403, "Only the owner can close a legal threat, refund request or complaint. "
                                   "Mark it as seen; the owner has it too.")
    with tenant.db() as c:
        c.execute("UPDATE escalations SET status='resolved', resolved_by=?, resolved=?, note=? WHERE id=?",
                  (actor, db.now(), (note or "").strip()[:500], eid))
    audit.log(tenant, actor, "escalation.resolve", {"id": eid})
    return get(tenant, eid)


def closed_by_person(tenant, kind: str) -> set[str]:
    """Refs of escalations of this kind that a person resolved (not the system). Rule checks skip them."""
    with tenant.db() as c:
        return {r["ref"] for r in c.execute("SELECT DISTINCT ref FROM escalations WHERE kind=? AND status='resolved' AND "
                                            "ref IS NOT NULL AND COALESCE(resolved_by,'')!='system'", (kind,))}


def auto_resolve(tenant, kind: str, ref: str, why: str) -> int:
    """Close open escalations whose cause went away (used by the SLA check)."""
    with tenant.db() as c:
        return c.execute("UPDATE escalations SET status='resolved', resolved_by='system', resolved=?, note=? "
                         "WHERE kind=? AND ref=? AND status IN ('open','acked')", (db.now(), why[:500], kind, ref)).rowcount


# --------------------------------------------------------------------------- missions + crash recovery
def step_failed(tenant, mission_id: int | None, step_id: int | str, attempts: int, detail: str = "",
                assignee_user_id: int | None = None) -> int | None:
    """For the missions engine: escalate once a step has failed three times. Returns the id or None."""
    try:
        attempts = int(attempts)
    except (TypeError, ValueError):
        return None
    if attempts < 3:
        return None
    return raise_(tenant, "step_failed", "medium", detail or f"A step failed {attempts} times in a row",
                  assignee_user_id=assignee_user_id, mission_id=mission_id, ref=f"step:{mission_id}:{step_id}")


def recover_runs(tenant, older_than: float = 600.0, started_before: float | None = None) -> int:
    """Runs still marked "running" more than ten minutes after they started cannot be alive after a restart
    (the run pool is in memory): mark them "interrupted" so nothing waits on them forever. With started_before
    (this process's start time, used by the scheduler tick) only runs begun before the restart are touched,
    once they too are ten minutes old; runs this process started are never cut short."""
    cutoff = db.now() - older_than
    if started_before is not None:
        cutoff = min(cutoff, float(started_before))
    with tenant.db() as c:
        n = c.execute("UPDATE runs SET status='interrupted' WHERE status='running' AND created<?", (cutoff,)).rowcount
    if n:
        audit.log(tenant, "system", "action", {"recovered_runs": n})
        raise_(tenant, "runs_interrupted", "low",
               f"{n} background run(s) were interrupted by a restart. Start them again if you still need them.",
               ref=f"recover:{int(time.time() // 3600)}")
    return n


# --------------------------------------------------------------------------- internal Telegram alert
def _telegram_alert(tenant, eid: int, kind: str, severity: str, detail: str, assignee_user_id: int | None) -> int | None:
    """Draft an internal alert to the assignee's own linked Telegram (or the owner's), only when the owner
    switched alerts on. Still a queue item; the recipient approves it; never prospect-facing."""
    if not tenant.get("escalation_telegram_alerts", False):
        return None
    if severity == "low":  # for-your-info items (a stop request already honoured, a restart) stay in the app
        return None
    with tenant.db() as c:
        if assignee_user_id is not None:
            uids = [assignee_user_id]
        else:
            uids = [r["id"] for r in c.execute("SELECT id FROM users WHERE role='owner' AND active=1 ORDER BY id")]
        link = None
        for uid in uids:
            link = c.execute("SELECT ext_id, user_id FROM channel_links WHERE channel='telegram' AND user_id=? "
                             "AND status='linked' AND ext_id IS NOT NULL ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
            if link:
                break
    if link is None:
        return None
    from . import queue

    body = f"Alert ({severity}): {KINDS.get(kind, KINDS['other'])}. {detail[:400]} Open the Inbox to act on it."
    item = queue.add(tenant, "channel_reply", body, channel="telegram", recipient_ref=str(link["ext_id"]),
                     title=f"Alert to your own Telegram: {KINDS.get(kind, 'escalation')}",
                     meta={"internal_alert": True, "escalation_id": eid, "owner_user_id": link["user_id"],
                           "prospect_facing": False}, actor="escalations", demo=tenant.is_demo)
    return item["id"]
