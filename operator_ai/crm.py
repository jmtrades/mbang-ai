"""Mock GoHighLevel-style CRM: contacts, notes, tasks and stages. Ordinary stage moves apply
directly; terminal moves (closed_won / closed_lost) go through the action gate and need the
owner's approval, with rollback."""
from __future__ import annotations

from . import actions, db

STAGES = ["new", "contacted", "booked", "showed", "closed_won", "closed_lost"]
TERMINAL = {"closed_won", "closed_lost"}


def contacts(tenant) -> list[dict]:
    with tenant.db() as c:
        rows = db.rows(c.execute("SELECT * FROM crm_contacts ORDER BY id DESC LIMIT 500"))
    for r in rows:
        r["tags"] = db.jload(r["tags"], [])
    return rows


def add_contact(tenant, name: str, email: str | None, tags: list[str] | None = None) -> int:
    with tenant.db() as c:
        return c.execute("INSERT INTO crm_contacts(name,email,stage,tags,demo,created) VALUES(?,?,?,?,?,?)",
                         (name, email, "new", db.jdump(tags or []), 1 if tenant.is_demo else 0, db.now())).lastrowid


def move(tenant, contact_id: int, stage: str, actor: str) -> dict:
    if stage not in STAGES:
        raise ValueError(stage)
    with tenant.db() as c:
        r = c.execute("SELECT name, stage FROM crm_contacts WHERE id=?", (contact_id,)).fetchone()
    if r is None:
        raise KeyError(contact_id)
    if stage in TERMINAL:
        p = actions.propose(tenant, "crm_move", {"ops": [{"op": "crm_move", "contact_id": contact_id, "stage": stage}],
                                                 "why": f"Terminal move for {r['name']}"}, actor,
                            title=f"CRM: move {r['name']} to {stage}")
        return {**p, "queue_status": p["status"], "status": "needs_approval"}
    with tenant.db() as c:
        c.execute("UPDATE crm_contacts SET stage=? WHERE id=?", (stage, contact_id))
    return {"status": "moved", "from": r["stage"], "to": stage}


def apply_ops(tenant, ops: list[dict]) -> tuple[list[dict], list[dict], bool]:
    prior, results, ok = [], [], True
    with tenant.db() as c:
        for o in ops:
            r = c.execute("SELECT stage FROM crm_contacts WHERE id=?", (o["contact_id"],)).fetchone()
            if r is None:
                results.append({"contact_id": o["contact_id"], "error": "not found"})
                ok = False
                continue
            prior.append({"contact_id": o["contact_id"], "stage": r["stage"]})
            c.execute("UPDATE crm_contacts SET stage=? WHERE id=?", (o["stage"], o["contact_id"]))
            results.append({"contact_id": o["contact_id"], "to": o["stage"]})
    return prior, results, ok


def restore(tenant, prior: list[dict]) -> None:
    with tenant.db() as c:
        for p in prior:
            c.execute("UPDATE crm_contacts SET stage=? WHERE id=?", (p["stage"], p["contact_id"]))


def add_note(tenant, contact_id: int, body: str) -> int:
    with tenant.db() as c:
        return c.execute("INSERT INTO crm_notes(contact_id,body,created) VALUES(?,?,?)",
                         (contact_id, body[:2000], db.now())).lastrowid


def add_task(tenant, contact_id: int, title: str, due: float | None = None) -> int:
    with tenant.db() as c:
        return c.execute("INSERT INTO crm_tasks(contact_id,title,status,due,created) VALUES(?,?,?,?,?)",
                         (contact_id, title[:200], "open", due, db.now())).lastrowid
