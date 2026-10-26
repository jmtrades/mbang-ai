"""Schedule ticker: daily/weekly workers, on-source-change triggers (drafts only) and the daily
Drive inbox sync (staged for the owner's OK)."""
from __future__ import annotations


def tick() -> None:
    from . import console, skills, tenancy

    skills.run_due_workers()
    for t in tenancy.list_all():
        if t.is_demo:
            continue
        try:
            console.drive_auto_sync(t)
        except Exception:  # noqa: BLE001 - one tenant must not stop the others
            continue
