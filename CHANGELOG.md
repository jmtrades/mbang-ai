# Changelog

All notable changes, newest first. Versions follow SemVer; each release is a git tag `vX.Y.Z`.

## 0.2.0 — Phase 0: foundations

- `pyproject.toml` with ruff and mypy configuration; Python pinned to 3.14 in `.python-version`, `pyproject.toml`
  and `deploy/Dockerfile` (was 3.12 in the image).
- `/health` reports `version` and `commit` (stamped at build time with `OPERATOR_COMMIT`).
- Architecture gates in `tests/test_arch.py`: hard gates for the voice-cloning ban (INV-3) and interpreter pins
  (QE-3); ratchets for lint findings, mypy errors, module and function size (ARCH-3), TSX size, `any` in the web app
  (FE-2), raw status writes (ARCH-4) and `time.sleep` in tests (QE-6). Ratchet baselines only shrink.
- CI on every push and pull request (`.github/workflows/ci.yml`), pre-commit hooks and `make ci-local`.
- Course importer: `scripts/import_course.py` turns the owner's own exported course files into the corpus layout,
  transcribing videos locally and recording the rights statement.
- Design system in `design-system/`: tokens, brand book, component renditions.
- `docs/REBUILD_PLAN.md`: the phased plan from this commit to every requirement in `docs/ENTERPRISE_STANDARD.md`.
- Safe lint fixes: unused imports, f-strings without placeholders, a duplicated set value.

## 0.1.0

- The original build (Phases 0–12 in `PROGRESS.md`).
