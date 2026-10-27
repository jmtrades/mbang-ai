"""Missions: goal-driven, multi-step agentic execution.

A goal ("win my first client", "book 20 calls", "launch a webinar") becomes a mission: a DAG of typed steps from a
deterministic template (works with AI off), run through the durable job queue with a trace for every step, waiting
for approvals, replies, bookings and time, measured against SOP-benchmark targets and corrected by rules-first
re-plans the user confirms. Nothing outbound leaves without approval.

    registry.py    typed step types wrapping existing functions and guarded feature modules
    templates.py   first_client, fill_calendar, launch_webinar, fix_bottleneck, scale_ads, client_onboarding,
                   client_weekly_ops, student_90_day (+ custom -> nearest template)
    engine.py      create / advance / pump / job handler / hooks / events / tick / user actions / next actions
    evaluator.py   metrics, plan vs actual, rules-first re-plans, escalations
    ai_planner.py  optional AI suggestions, validated against the registry
    seed.py        demo seed
"""
from __future__ import annotations

from . import ai_planner, engine, evaluator, registry, templates  # noqa: F401 - importing registers hooks and handlers
from .engine import create, get, next_actions, tick  # noqa: F401
from .seed import seed_demo  # noqa: F401

__all__ = ["tick", "seed_demo", "create", "get", "next_actions", "engine", "registry", "templates", "evaluator",
           "ai_planner"]
