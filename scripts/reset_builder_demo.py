#!/usr/bin/env python3
"""Archive every builder build in the demo workspace and clear its undecided DEMO drafts.
Refuses to touch any database other than the demo DB."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from operator_ai import departments, tenancy  # noqa: E402

tenancy.ensure_defaults()
try:
    out = departments.reset_all(tenancy.get("demo"))
except PermissionError as e:
    print(e, file=sys.stderr)
    raise SystemExit(3)
print(f"builder demo reset: {out['archived']} builds archived, {out['cleared_drafts']} drafts cleared")
