#!/usr/bin/env bash
# Online backup of every tenant DB, the registry, the demo DB, vaults and KB folders.
# Keeps 20 archives at mode 0600.  Usage: scripts/backup.sh [dest-dir]
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
umask 077
exec "$PY" scripts/backup.py --dest "${1:-${OPERATOR_BACKUP_DIR:-backups}}" --keep "${OPERATOR_BACKUP_KEEP:-20}"
