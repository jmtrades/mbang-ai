#!/usr/bin/env bash
# Smoke test a running instance (default http://127.0.0.1:8850). Exits non-zero on any failure.
set -euo pipefail
BASE=${1:-http://127.0.0.1:8850}
fail=0
check() { code=$(curl -s -o /dev/null -w "%{http_code}" "$BASE$1"); if [ "$code" = "$2" ]; then echo "ok   $1 ($code)"; else echo "FAIL $1 ($code, want $2)"; fail=1; fi; }
check /health 200
check /ready 200
check /t/owner/ 200
check /t/demo/api/public 200
check /t/owner/api/me 401
check /manifest.webmanifest 200
exit $fail
