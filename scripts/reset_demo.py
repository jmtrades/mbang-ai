#!/usr/bin/env python3
"""Reset the pitch demo workspace to the clean story (backs up first). Refuses any non-demo DB."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from operator_ai import demo, tenancy  # noqa: E402

tenancy.ensure_defaults()
try:
    out = demo.reset(tenancy.get(sys.argv[1] if len(sys.argv) > 1 else "demo"), "server-cli")
except demo.DemoResetRefused as e:
    print(e, file=sys.stderr)
    raise SystemExit(3)
print(f"demo reset ok; backup {out['backup']}")
