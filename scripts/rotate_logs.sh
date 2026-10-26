#!/usr/bin/env bash
# Rotate logs/*.log when larger than OPERATOR_LOG_MAX bytes (default 10 MB); keeps 5 generations.
set -euo pipefail
cd "$(dirname "$0")/.."
MAX=${OPERATOR_LOG_MAX:-10485760}
LOG_DIR=${OPERATOR_LOG_DIR:-logs}
mkdir -p "$LOG_DIR"
for f in "$LOG_DIR"/*.log; do
  [ -f "$f" ] || continue
  size=$(wc -c < "$f" | tr -d ' ')
  if [ "$size" -gt "$MAX" ]; then
    for i in 4 3 2 1; do [ -f "$f.$i" ] && mv -f "$f.$i" "$f.$((i+1))"; done
    cp "$f" "$f.1" && : > "$f"
    chmod 600 "$f.1" || true
  fi
done
