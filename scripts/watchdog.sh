#!/usr/bin/env bash
# Watchdog: starts the app, checks /health every OPERATOR_WD_INTERVAL seconds, restarts after
# 4 consecutive failures (immediately if the process is dead). It only ever kills the process
# it started (PID file). Also rotates logs and runs a backup every 6 hours.
set -uo pipefail
cd "$(dirname "$0")/.."
PORT=${OPERATOR_PORT:-8850}
INTERVAL=${OPERATOR_WD_INTERVAL:-15}
FAILS_MAX=${OPERATOR_WD_FAILS:-4}
BACKUP_EVERY=${OPERATOR_WD_BACKUP_SECONDS:-21600}
LOG_DIR=${OPERATOR_LOG_DIR:-logs}
PID_FILE=${OPERATOR_PID_FILE:-$LOG_DIR/operator.pid}
START_CMD=${OPERATOR_START_CMD:-scripts/run.sh}
mkdir -p "$LOG_DIR"
last_backup=$(date +%s)
fails=0

start() {
  nohup bash -c "exec $START_CMD" >> "$LOG_DIR/app.log" 2>&1 &
  echo $! > "$PID_FILE"
  echo "$(date -u +%FT%TZ) watchdog: started pid $(cat "$PID_FILE")" >> "$LOG_DIR/watchdog.log"
}

alive() { [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; }

stop_own() {
  if alive; then
    kill "$(cat "$PID_FILE")" 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do alive || break; sleep 1; done
    alive && kill -9 "$(cat "$PID_FILE")" 2>/dev/null || true
  fi
}

trap 'stop_own; exit 0' TERM INT
alive || start
while true; do
  sleep "$INTERVAL"
  if ! alive; then
    echo "$(date -u +%FT%TZ) watchdog: process dead, restarting now" >> "$LOG_DIR/watchdog.log"
    start; fails=0; continue
  fi
  if curl -fsS -m 5 "http://127.0.0.1:$PORT/health" > /dev/null 2>&1; then
    fails=0
  else
    fails=$((fails+1))
    echo "$(date -u +%FT%TZ) watchdog: health check failed ($fails/$FAILS_MAX)" >> "$LOG_DIR/watchdog.log"
    if [ "$fails" -ge "$FAILS_MAX" ]; then
      stop_own; start; fails=0
    fi
  fi
  scripts/rotate_logs.sh >/dev/null 2>&1 || true
  now=$(date +%s)
  if [ $((now - last_backup)) -ge "$BACKUP_EVERY" ]; then
    scripts/backup.sh >> "$LOG_DIR/backup.log" 2>&1 || true
    last_backup=$now
  fi
done
