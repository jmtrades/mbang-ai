#!/usr/bin/env bash
# Optional: copy the newest backup offsite. Uses rclone if RCLONE_REMOTE is set, else rsync to OFFSITE_DEST.
set -euo pipefail
cd "$(dirname "$0")/.."
latest=$(ls -1t backups/operator-*.tgz 2>/dev/null | head -1 || true)
[ -n "$latest" ] || { echo "no backups yet" >&2; exit 0; }
if [ -n "${RCLONE_REMOTE:-}" ]; then
  rclone copy "$latest" "$RCLONE_REMOTE"
elif [ -n "${OFFSITE_DEST:-}" ]; then
  rsync -a "$latest" "$OFFSITE_DEST"
else
  echo "set RCLONE_REMOTE or OFFSITE_DEST" >&2
  exit 1
fi
