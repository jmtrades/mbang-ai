#!/usr/bin/env bash
# Run by operator-ai-health.timer every minute. Restarts the service after 4 consecutive failures.
set -uo pipefail
STATE=${OPERATOR_HEALTH_STATE:-/var/lib/operator-ai/health.fails}
PORT=${OPERATOR_PORT:-8850}
mkdir -p "$(dirname "$STATE")"
fails=$(cat "$STATE" 2>/dev/null || echo 0)
if curl -fsS -m 5 "http://127.0.0.1:$PORT/health" > /dev/null; then
  echo 0 > "$STATE"
  exit 0
fi
fails=$((fails+1))
echo "$fails" > "$STATE"
if [ "$fails" -ge 4 ]; then
  systemctl restart operator-ai.service
  echo 0 > "$STATE"
fi
