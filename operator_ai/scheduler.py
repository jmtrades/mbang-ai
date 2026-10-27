"""Schedule ticker: daily/weekly workers, on-source-change triggers (drafts only), the daily
Drive inbox sync (staged for the owner's OK) and each feature's per-tenant tick."""
from __future__ import annotations

import importlib
import logging

log = logging.getLogger("operator.scheduler")

# "module:function" called as fn(tenant) for every non-demo tenant on each tick (and for the demo tenant
# when the function sets fn.demo_too = True). A missing module is skipped, so features can land one by one.
TENANT_TICKS = [
    "operator_ai.console:drive_auto_sync",
    "operator_ai.delivery:dispatch_due",       # approved items whose send_after has come; cancel_if checks
    "operator_ai.delivery:run_cadences",       # time-gated follow-ups after a confirmed send
    "operator_ai.missions:tick",               # advance goal-driven missions, re-measure, re-plan
    "operator_ai.inbound:check_slas",          # speed-to-lead and stuck-item escalations
    "operator_ai.webinars:tick",               # per-registrant reminders around each event
    "operator_ai.client_ops:tick",             # weekly client ops and reports
    "operator_ai.academy:tick",                # daily: published lessons whose source changed go stale
]


def _resolve(spec: str):
    mod, _, fn = spec.partition(":")
    try:
        return getattr(importlib.import_module(mod), fn, None)
    except ModuleNotFoundError as e:
        if e.name == mod:
            return None
        raise


def tick() -> None:
    from . import skills, tenancy

    skills.run_due_workers()
    fns = [f for f in (_resolve(s) for s in TENANT_TICKS) if f is not None]
    for t in tenancy.list_all():
        for fn in fns:
            if t.is_demo and not getattr(fn, "demo_too", False):
                continue
            try:
                fn(t)
            except Exception:  # noqa: BLE001 - one tenant or feature must not stop the others
                from .redact import redact

                log.exception("tick failed: %s", redact(f"{t.id} {getattr(fn, '__module__', '')}.{fn.__name__}"))
