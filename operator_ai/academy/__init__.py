"""Academy: trains everyone (owner, coaches, setters, closers, students) from the owner's own material.

    build.py    cited lessons from the course material (traced run "academy_build", AI optional, owner approval)
    quiz.py     rules-based questions, robust grading, item analytics
    srs.py      one spaced-repetition store, Elo mastery, adaptive practice, the daily session
    certify.py  certificates L1-L4 per path, timed exams, expiry, recertification, the approval gate
    team.py     coach and manager dashboards
    admin.py    owner admin: drafts, coverage, item stats, thresholds
    learn.py    the learner side
    seed.py     demo seeding (demo database only)

Other features use:
    is_certified(tenant, user_id, path_key, level) -> bool
    approval_gate(tenant, user_id, kind, meta=None) -> reason or None   (owner switch require_cert_for_own_outreach)
    exam_open(tenant, user_id) -> bool                                 (the coach pauses during a certification exam)
"""
from __future__ import annotations

import time

from .certify import approval_gate, approval_gate_for_item, exam_open, is_certified
from .seed import seed_demo

__all__ = ["is_certified", "approval_gate", "approval_gate_for_item", "exam_open", "seed_demo", "enrol",
           "on_source_change", "tick"]


def enrol(tenant, user_id: int, path_key: str, actor: str = "system", due_days: int | None = None) -> bool:
    """Put a learner on a path themselves (a mission's certification step): an enrolment row, no coach note to
    approve. Returns False for an unknown path or user."""
    from .. import audit, db
    from . import common

    if path_key not in common.PATHS:
        return False
    with tenant.db() as c:
        if c.execute("SELECT 1 FROM users WHERE id=? AND active=1", (int(user_id),)).fetchone() is None:
            return False
        c.execute("INSERT INTO academy_enrolments(user_id,path_key,assigned_by,due,status,created) VALUES(?,?,?,?,?,?) "
                  "ON CONFLICT(user_id,path_key) DO UPDATE SET status='active'",
                  (int(user_id), path_key, int(user_id), time.time() + due_days * 86400 if due_days else None, "active",
                   db.now()))
    audit.log(tenant, actor, "settings", {"academy_enrol": path_key, "user": int(user_id)})
    return True


def on_source_change(tenant, source: str = "") -> dict:
    """skills.on_source_change calls this when the knowledge base changes: lessons built from changed sections go
    stale (they are rebuilt and re-approved, never edited silently)."""
    from .build import mark_stale_sources

    return mark_stale_sources(tenant, source, "source-change")


def tick(tenant) -> dict:
    """Scheduler tick (TENANT_TICKS): once a day, check published lessons against the current material, in case it
    changed outside the app (files copied into inputs/)."""
    last = float(tenant.get("academy_stale_check", 0) or 0)
    if time.time() - last < 86400:
        return {"skipped": "checked in the last day"}
    tenant.set("academy_stale_check", time.time())
    from .build import mark_stale_sources

    return mark_stale_sources(tenant, "daily check", "scheduler")
