#!/usr/bin/env python3
"""Prune caches: index vector caches no longer used by the current corpus, the Python bytecode
caches, and backups beyond the newest 20. Prints what it removed (names only)."""
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from operator_ai import config, embeddings, index  # noqa: E402


def prune(keep_backups: int = 20) -> list[str]:
    removed = []
    cfg = config.get()
    current = index.course_index().key + "-" + embeddings.get().name
    if cfg.index_dir.exists():
        for d in cfg.index_dir.iterdir():
            if d.is_dir() and d.name.startswith("course-") and d.name != current:
                shutil.rmtree(d)
                removed.append(f"index cache {d.name}")
    backups = sorted((config.ROOT / "backups").glob("operator-*.tgz")) if (config.ROOT / "backups").exists() else []
    for b in backups[:-keep_backups]:
        b.unlink()
        removed.append(f"backup {b.name}")
    for pyc in config.ROOT.joinpath("operator_ai").rglob("__pycache__"):
        shutil.rmtree(pyc, ignore_errors=True)
    return removed


if __name__ == "__main__":
    for r in prune():
        print("removed", r)
