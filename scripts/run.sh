#!/usr/bin/env bash
# Start Operator AI locally on :8850 (builds the web app if needed).
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
if [ ! -f web/dist/index.html ]; then
  (cd web && npm install --no-audit --no-fund && npm run build)
fi
exec "$PY" -m uvicorn operator_ai.app:app --host "${OPERATOR_HOST:-127.0.0.1}" --port "${OPERATOR_PORT:-8850}" --proxy-headers ${UVICORN_EXTRA:-}
