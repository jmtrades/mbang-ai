#!/usr/bin/env python3
"""Go-live preflight: lists every check as OK or MISSING. Offline, read-only, never prints a value.
The checks live in operator_ai.setup.preflight() so the Workspace setup screen shows the same list.

  scripts/preflight_live.py [--tenant owner]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from operator_ai import config, setup, tenancy  # noqa: E402


def checks(tid: str) -> list[tuple[str, bool, str]]:
    tenancy.ensure_defaults()
    return [(r["name"], r["ok"], r["need"]) for r in setup.preflight(tenancy.get(tid))]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", default=config.get().default_tenant)
    a = ap.parse_args(argv)
    rows = checks(a.tenant)
    for name, ok, need in rows:
        print(f"{'OK     ' if ok else 'MISSING'}  {name}" + ("" if ok or not need else f"  ->  {need}"))
    missing = sum(1 for _, ok, _ in rows if not ok)
    print(f"\n{len(rows) - missing}/{len(rows)} OK, {missing} MISSING")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
