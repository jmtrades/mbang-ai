#!/usr/bin/env python3
"""Restore a backup archive.  --check verifies it (every DB passes PRAGMA integrity_check)
without touching the live data. Without --check the current data dir is moved aside first.

Vault keys are not in the data archive (see scripts/backup.py). On restore they come from, in order:
  1. --keys <keys-YYYYmmdd-HHMMSS.tgz> (the bundle written with that archive), else
  2. the matching bundle in the default keys folder, else
  3. the keys of the data directory being replaced (same machine, keys never changed).
A tenant whose key is found nowhere keeps its encrypted vault.bin; re-enter its integration secrets in the
app (Owner -> Integrations). The output says which tenants that applies to. Full procedure: docs/SECURITY.md.

  scripts/restore.py <archive.tgz> [--check] [--keys <keys.tgz>]
"""
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


def _safe_members(tar: tarfile.TarFile, only_names: tuple[str, ...] | None = None):
    for m in tar.getmembers():
        p = Path(m.name)
        if p.is_absolute() or ".." in p.parts or m.issym() or m.islnk() or not (m.isfile() or m.isdir()):
            raise SystemExit(f"refusing unsafe archive member: {m.name}")
        if only_names and m.isfile() and p.name not in only_names:
            raise SystemExit(f"unexpected file in the keys bundle: {m.name}")
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
        keys = list(Path(tmp).rglob("vault.key"))
        if keys:
            notes.append(f"WARNING: {len(keys)} vault key file(s) inside the data archive (made by an older backup)")
    return ok, notes


def _restore_keys(data: Path, aside: Path, keys: Path | None) -> list[str]:
    """Put each tenant's vault.key next to its vault.bin. Returns the tenants left without a key."""
    if keys is not None:
        with tempfile.TemporaryDirectory() as tmp, tarfile.open(keys, "r:gz") as tar:
            tar.extractall(tmp, members=list(_safe_members(tar, only_names=("vault.key",))))
            for k in (Path(tmp) / "data").rglob("vault.key"):
                rel = k.relative_to(Path(tmp) / "data")
                (data / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(k, data / rel)
                (data / rel).chmod(0o600)
    missing = []
    for blob in data.rglob("vault.bin"):
        key = blob.with_name("vault.key")
        if key.exists():
            continue
        old = aside / blob.relative_to(data).with_name("vault.key")
        if old.exists():
            shutil.copy2(old, key)
            key.chmod(0o600)
        else:
            missing.append(blob.parent.name)
    return missing


def restore(archive: Path, keys: Path | None = None) -> Path:
    data = config.get().data_dir
    ok, notes = check(archive)
    if not ok:
        raise SystemExit("archive failed the integrity check:\n" + "\n".join(notes))
    if keys is None:
        from scripts.backup import keys_bundle_for

        keys = keys_bundle_for(archive)
    aside = data.with_name(f"{data.name}.pre-restore-{time.strftime('%Y%m%d-%H%M%S')}")
    if data.exists():
        shutil.move(str(data), str(aside))
    with tempfile.TemporaryDirectory() as tmp, tarfile.open(archive, "r:gz") as tar:
        tar.extractall(tmp, members=list(_safe_members(tar)))
        shutil.copytree(Path(tmp) / "data", data)
    missing = _restore_keys(data, aside, keys)
    if missing:
        print("vault key not found for: " + ", ".join(sorted(missing)) +
              " (their integration secrets must be re-entered in the app)", file=sys.stderr)
    return aside


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("archive")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--keys", default=None, help="the keys bundle written with this archive")
    a = ap.parse_args(argv)
    if a.check:
        ok, notes = check(Path(a.archive))
        print("\n".join(notes))
        print("CHECK OK" if ok else "CHECK FAILED")
        return 0 if ok else 1
    aside = restore(Path(a.archive), Path(a.keys) if a.keys else None)
    print(f"restored; previous data kept at {aside}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
