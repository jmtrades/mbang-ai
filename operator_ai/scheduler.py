"""Schedule ticker: runs daily/weekly workers and on-source-change triggers (drafts only)."""
from __future__ import annotations


def tick() -> None:
    try:
        from . import skills
    except ImportError:
        return
    skills.run_due_workers()
