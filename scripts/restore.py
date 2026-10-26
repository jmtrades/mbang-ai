#!/usr/bin/env python3
"""Restore a backup archive.  --check verifies it (every DB passes PRAGMA integrity_check)
without touching the live data. Without --check the current data dir is moved aside first."""
import argparse
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from operator_ai import config  # noqa: E402


def _safe_members(tar: tarfile.TarFile):
    for m in tar.getmembers():
        p = Path(m.name)
        if p.is_absolute() or ".." in p.parts or m.issym() or m.islnk():
            raise SystemExit(f"refusing unsafe archive member: {m.name}")
        yield m


def check(archive: Path) -> tuple[bool, list[str]]:
    notes = []
    with tempfile.TemporaryDirectory() as tmp, tarfile.open(archive, "r:gz") as tar:
        tar.extractall(tmp, members=list(_safe_members(tar)))
        dbs = list(Path(tmp).rglob("*.db"))
        if not dbs:
            return False, ["no databases in archive"]
        ok = True
        for db in dbs:
            c = sqlite3.connect(str(db))
            res = c.execute("PRAGMA integrity_check").fetchone()[0]
            c.close()
            notes.append(f"{db.relative_to(tmp)}: {res}")
            ok = ok and res == "ok"
    return ok, notes


def restore(archive: Path) -> Path:
    data = config.get().data_dir
    ok, notes = check(archive)
    if not ok:
        raise SystemExit("archive failed the integrity check:\n" + "\n".join(notes))
    aside = data.with_name(f"{data.name}.pre-restore-{time.strftime('%Y%m%d-%H%M%S')}")
    if data.exists():
        shutil.move(str(data), str(aside))
    with tempfile.TemporaryDirectory() as tmp, tarfile.open(archive, "r:gz") as tar:
        tar.extractall(tmp, members=list(_safe_members(tar)))
        shutil.copytree(Path(tmp) / "data", data)
    return aside


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("archive")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    if a.check:
        ok, notes = check(Path(a.archive))
        print("\n".join(notes))
        print("CHECK OK" if ok else "CHECK FAILED")
        return 0 if ok else 1
    aside = restore(Path(a.archive))
    print(f"restored; previous data kept at {aside}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
