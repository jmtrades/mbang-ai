# make dev        install runtime and dev dependencies into .venv, and the web app's
# make test       the Python suite without browser tests (parallel)
# make gates      only the architecture gates
# make baselines  tighten the ratchet baselines after fixing violations
# make ci-local   what CI runs, offline
PY := .venv/bin/python

.PHONY: dev test gates baselines web ci-local
dev:
	python3.14 -m venv .venv
	$(PY) -m pip install -r requirements.lock -r requirements-dev.txt
	cd web && npm ci --no-audit --no-fund

test:
	$(PY) -m pytest -n auto -m "not ui"

gates:
	$(PY) -m pytest -q tests/test_arch.py

baselines:
	$(PY) scripts/update_arch_baselines.py

web:
	cd web && npm run build

ci-local: web
	.venv/bin/ruff check operator_ai scripts tests --select F821,F822,F823,E9
	$(PY) -m pytest -n auto -m "not ui"
