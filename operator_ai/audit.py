"""Append-only audit log per tenant. Every line passes through redact()."""
from __future__ import annotations

import json
import logging

from . import db
from .redact import redact

log_ = logging.getLogger("operator.audit")

KINDS = {
    "login", "login.fail", "logout", "queue.add", "guardrail.block", "queue.approve", "queue.reject",
    "queue.edit", "action", "action.rollback", "settings", "switch", "vault.save", "kb.scope", "tenant.create",
    "demo.reset", "run", "payment", "consent", "mcp.token", "kb.admin", "drive.stage", "builder",
}


def log(tenant, actor: str, kind: str, detail) -> None:
    text = redact(json.dumps(detail, default=str) if not isinstance(detail, str) else detail)
    with tenant.db() as c:
        c.execute("INSERT INTO audit(at,actor,kind,detail) VALUES(?,?,?,?)", (db.now(), actor, kind, text))
    log_.info("audit %s %s %s", tenant.id, kind, text[:200])


def recent(tenant, limit: int = 200, kind: str | None = None) -> list[dict]:
    with tenant.db() as c:
        if kind:
            cur = c.execute("SELECT * FROM audit WHERE kind LIKE ? ORDER BY id DESC LIMIT ?", (kind + "%", limit))
        else:
            cur = c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
        return db.rows(cur)
