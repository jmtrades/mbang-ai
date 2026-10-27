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

from .certify import approval_gate, approval_gate_for_item, exam_open, is_certified
from .seed import seed_demo

__all__ = ["is_certified", "approval_gate", "approval_gate_for_item", "exam_open", "seed_demo"]
