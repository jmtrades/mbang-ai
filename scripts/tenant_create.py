#!/usr/bin/env python3
"""Create a tenant. The generated owner password is written to a mode-600 file and never printed.

  scripts/tenant_create.py <id> "<Name>" [--kb-scope empty|own]

Client tenants start with an empty knowledge base and an AI budget of 0. The course knowledge
base can't be provisioned to a client tenant (LicenceError)."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from operator_ai import tenancy  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("id")
    ap.add_argument("name")
    ap.add_argument("--kb-scope", default="empty", choices=list(tenancy.KB_SCOPES))
    a = ap.parse_args(argv)
    tenancy.ensure_defaults()
    try:
        t, pw_file = tenancy.create(a.id, a.name, kb_scope=a.kb_scope, kind="client")
    except tenancy.LicenceError as e:
        print(f"LicenceError: {e}", file=sys.stderr)
        return 3
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(f"tenant {t.id} created (kb_scope={t.kb_scope}). Owner password written to {pw_file} (mode 600).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
