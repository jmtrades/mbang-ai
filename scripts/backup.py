#!/usr/bin/env python3
"""Online backup of every database (registry, every tenant, demo) plus vault files and tenant
KB folders into one .tgz. Uses SQLite's online backup API (safe while the app runs). Keeps the
newest 20 archives; every archive is mode 0600.

  scripts/backup.py [--dest backups/] [--keep 20]
"""
import argparse
import os
import sqlite3
import sys
import tarfile
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from operator_ai import config  # noqa: E402


def _dbs(data: Path) -> list[Path]:
    out = []
    for p in sorted(data.rglob("*.db")):
        if "backups" in p.parts:
            continue
        out.append(p)
    return out


def backup(dest: Path, keep: int = 20) -> Path:
    data = config.get().data_dir
    dest.mkdir(parents=True, exist_ok=True)
    os.chmod(dest, 0o700)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = dest / f"operator-{stamp}.tgz"
    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        for db in _dbs(data):
            rel = db.relative_to(data)
            target = tmpd / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            src = sqlite3.connect(str(db))
            dst = sqlite3.connect(str(target))
            try:
                src.backup(dst)
            finally:
                src.close()
                dst.close()
        for extra in list(data.rglob("vault.*")) + list(data.rglob("kb/*.txt")) + [data / "kb_exclusions.json"]:
            if extra.exists() and extra.is_file() and "backups" not in extra.parts:
                rel = extra.relative_to(data)
                (tmpd / rel).parent.mkdir(parents=True, exist_ok=True)
                (tmpd / rel).write_bytes(extra.read_bytes())
        fd = os.open(str(out), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f, tarfile.open(fileobj=f, mode="w:gz") as tar:
            tar.add(tmpd, arcname="data")
    os.chmod(out, 0o600)
    archives = sorted(dest.glob("operator-*.tgz"))
    for old in archives[:-keep]:
        old.unlink()
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", default=str(config.ROOT / "backups"))
    ap.add_argument("--keep", type=int, default=20)
    a = ap.parse_args(argv)
    out = backup(Path(a.dest), a.keep)
    print(f"backup written: {out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
