#!/usr/bin/env bash
# Restore a backup.  Usage: scripts/restore.sh <archive.tgz> [--check]
# Stop the app before a real restore (the watchdog restarts it).
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
[ $# -ge 1 ] || { echo "usage: $0 <archive.tgz> [--check]" >&2; exit 2; }
exec "$PY" scripts/restore.py "$@"
