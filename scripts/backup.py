#!/usr/bin/env python3
"""Online backup of every database (registry, every tenant, demo), the encrypted vault blobs and the tenant
KB folders into one .tgz. Uses SQLite's online backup API (safe while the app runs). Keeps the newest 20
archives; every archive is mode 0600.

The vault KEYS never go into that archive. They are written to a separate keys bundle in a separate folder
(default: backup-keys/ next to the archives, mode 0700; override with --keys-dest or OPERATOR_BACKUP_KEYS_DIR).
deploy/offsite_backup.sh copies only the data archive, so an offsite copy holds ciphertext without the keys
that open it. Keep the keys bundle somewhere else (a password manager, a second machine, an encrypted USB
drive). Restore procedure: docs/SECURITY.md.

  scripts/backup.py [--dest backups/] [--keys-dest backup-keys/] [--keep 20]
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

KEY_NAMES = ("vault.key",)
# never in the data archive: key material and first-start secrets
SECRET_NAMES = KEY_NAMES + ("owner_password.txt",)


def _dbs(data: Path) -> list[Path]:
    out = []
    for p in sorted(data.rglob("*.db")):
        if "backups" in p.parts:
            continue
        out.append(p)
    return out


def _copy(src: Path, data: Path, into: Path) -> None:
    rel = src.relative_to(data)
    (into / rel).parent.mkdir(parents=True, exist_ok=True)
    (into / rel).write_bytes(src.read_bytes())


def _tar(src_dir: Path, out: Path) -> None:
    fd = os.open(str(out), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f, tarfile.open(fileobj=f, mode="w:gz") as tar:
        tar.add(src_dir, arcname="data")
    os.chmod(out, 0o600)


def default_keys_dest(dest: Path) -> Path:
    env = os.environ.get("OPERATOR_BACKUP_KEYS_DIR")
    return Path(env) if env else dest.resolve().parent / "backup-keys"


def backup(dest: Path, keep: int = 20, keys_dest: Path | None = None) -> Path:
    data = config.get().data_dir
    dest.mkdir(parents=True, exist_ok=True)
    os.chmod(dest, 0o700)
    keys_dest = keys_dest or default_keys_dest(dest)
    if keys_dest.resolve() == dest.resolve() or keys_dest.resolve().is_relative_to(dest.resolve()):
        raise SystemExit("the keys bundle must not go into the archive folder (that folder is what gets copied offsite)")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = dest / f"operator-{stamp}.tgz"
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as ktmp:
        tmpd, keyd = Path(tmp), Path(ktmp)
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
        extras = list(data.rglob("vault.*")) + list(data.rglob("kb/*.txt")) + list(data.rglob("kb_exclusions.json"))
        n_keys = 0
        for extra in extras:
            if not extra.is_file() or "backups" in extra.parts or extra.suffix == ".tmp":
                continue
            if extra.name in KEY_NAMES:
                _copy(extra, data, keyd)
                n_keys += 1
            elif extra.name not in SECRET_NAMES:
                _copy(extra, data, tmpd)
        _tar(tmpd, out)
        if n_keys:
            keys_dest.mkdir(parents=True, exist_ok=True)
            os.chmod(keys_dest, 0o700)
            _tar(keyd, keys_dest / f"keys-{stamp}.tgz")
    archives = sorted(dest.glob("operator-*.tgz"))
    for old in archives[:-keep]:
        old.unlink()
    if keys_dest.exists():
        for old in sorted(keys_dest.glob("keys-*.tgz"))[:-keep]:
            old.unlink()
    return out


def keys_bundle_for(archive: Path, keys_dest: Path | None = None) -> Path | None:
    """The keys bundle written with an archive (same timestamp), if it is where backup() put it."""
    stamp = archive.name.removeprefix("operator-").removesuffix(".tgz")
    p = (keys_dest or default_keys_dest(archive.parent)) / f"keys-{stamp}.tgz"
    return p if p.exists() else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", default=str(config.ROOT / "backups"))
    ap.add_argument("--keys-dest", default=None)
    ap.add_argument("--keep", type=int, default=20)
    a = ap.parse_args(argv)
    out = backup(Path(a.dest), a.keep, Path(a.keys_dest) if a.keys_dest else None)
    kb = keys_bundle_for(out, Path(a.keys_dest) if a.keys_dest else None)
    print(f"backup written: {out.name}" + (f" (vault keys kept separately: {kb.parent.name}/{kb.name})" if kb else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
