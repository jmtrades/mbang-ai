"""Architecture gates from docs/ENTERPRISE_STANDARD.md.

Hard gates must hold now. Ratchets compare a measurement with the committed baseline in tests/arch_baselines/: a new
violation, or a worse one, fails; a fully fixed entry must be dropped from the baseline (scripts/update_arch_baselines.py)
so it cannot come back. Each ratchet becomes a hard gate once its baseline is empty."""
from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

from scripts import arch_metrics as am

ROOT = am.ROOT


def _ratchet(name: str) -> None:
    try:
        current = am.METRICS[name]()
    except FileNotFoundError as e:
        pytest.skip(f"{e} is not installed here (pip install -r requirements-dev.txt); CI runs this gate")
    regressions, fixed = am.compare(current, am.load_baseline(name))
    assert not regressions, (f"{name}: {len(regressions)} new or worse violations. Fix them; the baseline only shrinks.\n  "
                             + "\n  ".join(regressions[:25]))
    assert not fixed, (f"{name}: {len(fixed)} entries are fixed. Lock that in: python scripts/update_arch_baselines.py {name}\n  "
                       + "\n  ".join(fixed[:25]))


# --------------------------------------------------------------------------- ratchets
def test_ruff_ratchet():
    """QE-10: lint findings never grow."""
    _ratchet("ruff")


def test_mypy_ratchet():
    """QE-10: type errors never grow."""
    _ratchet("mypy")


def test_module_and_function_size_ratchet():
    """ARCH-3: no module over 800 lines, no function over 80 (ratcheted)."""
    _ratchet("python_sizes")


def test_tsx_size_ratchet():
    """ARCH-3: no TSX file over 600 lines (ratcheted)."""
    _ratchet("tsx_sizes")


def test_web_any_ratchet():
    """FE-2: `any` in web/src never grows."""
    _ratchet("web_any")


def test_raw_status_write_ratchet():
    """ARCH-4: raw INSERT/UPDATE/DELETE on stateful tables never spreads to new modules."""
    _ratchet("raw_status_writes")


def test_no_new_sleeps_in_tests():
    """QE-6: no new time.sleep in tests."""
    _ratchet("test_sleeps")


# --------------------------------------------------------------------------- hard gates
VOICE_CLONE_PACKAGES = re.compile(r"^(elevenlabs|resemble|resemble-ai|tts|coqui-tts|coqui|xtts|openvoice|bark|tortoise-tts|"
                                  r"rvc|so-vits-svc|playht|pyht|cartesia)\b", re.I)
VOICE_CLONE_CODE = re.compile(r"\b(speaker_wav|voice_sample|clone_voice|voice_clone|voices\.add|instant_voice_clone)\b")


def _requirement_names() -> list[str]:
    names = []
    for f in ("requirements.in", "requirements.lock", "requirements-dev.txt"):
        p = ROOT / f
        if p.exists():
            names += [re.split(r"[=<>\[;\s]", ln.strip(), maxsplit=1)[0] for ln in p.read_text().splitlines()
                      if ln.strip() and not ln.startswith("#")]
    pkg = json.loads((ROOT / "web" / "package.json").read_text())
    names += list(pkg.get("dependencies", {})) + list(pkg.get("devDependencies", {}))
    return names


def test_no_voice_clone_dependencies():
    """INV-3: no package or code path that clones a real person's voice."""
    bad = [n for n in _requirement_names() if VOICE_CLONE_PACKAGES.match(n)]
    assert not bad, f"voice-cloning packages are banned (INV-3): {bad}"
    hits = []
    for p in list((ROOT / "operator_ai").rglob("*.py")) + list((ROOT / "web" / "src").rglob("*.ts*")):
        comment = "#" if p.suffix == ".py" else "//"
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            m = VOICE_CLONE_CODE.search(line)
            # denial text in a comment ("a voice_sample field is refused") is allowed by INV-3
            if m and not (comment in line and line.index(comment) < m.start()):
                hits.append(f"{p.relative_to(ROOT)}:{i}")
    assert not hits, f"voice-cloning parameters are banned (INV-3): {hits}"


def test_interpreter_pins_agree():
    """QE-3: one Python minor in .python-version, pyproject (requires-python, ruff, mypy) and the Dockerfile."""
    pinned = (ROOT / ".python-version").read_text().strip()
    assert re.fullmatch(r"3\.\d+", pinned), ".python-version must hold a minor version like 3.14"
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text())
    req = cfg["project"]["requires-python"]
    assert req == f">={pinned},<3.{int(pinned.split('.')[1]) + 1}", f"requires-python {req!r} does not pin {pinned}"
    assert cfg["tool"]["mypy"]["python_version"] == pinned
    assert cfg["tool"]["ruff"]["target-version"] == "py" + pinned.replace(".", "")
    bases = re.findall(r"^FROM python:(\d+\.\d+)", (ROOT / "deploy" / "Dockerfile").read_text(), re.M)
    assert bases == [pinned], f"deploy/Dockerfile Python base {bases} != {pinned}"


def test_version_is_single_sourced_and_on_health(client=None):
    """QE-4: the package version is the release version and /health reports it."""
    from fastapi.testclient import TestClient

    from operator_ai import __version__
    from operator_ai.app import app

    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)
    assert f"## {__version__}" in (ROOT / "CHANGELOG.md").read_text(), "CHANGELOG.md has no entry for this version"
    body = TestClient(app).get("/health").json()
    assert body["version"] == __version__ and "commit" in body


def test_ci_runs_the_gates():
    """QE-1: CI exists and runs the suite (which includes this file), lint and the web build."""
    wf = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    for needle in ("pytest", "requirements-dev.txt", "npm ci", "npm run build"):
        assert needle in wf, f"ci.yml does not run {needle!r}"


def test_baselines_are_valid_json_maps():
    for p in sorted(am.BASELINES.glob("*.json")):
        data = json.loads(p.read_text())
        assert isinstance(data, dict) and all(isinstance(v, int) and v > 0 for v in data.values()), p.name
        assert p.stem in am.METRICS, f"{p.name} has no metric"


def test_course_material_is_never_tracked():
    """REBUILD_PLAN decision: inputs/ and data/ stay out of git."""
    ignore = (ROOT / ".gitignore").read_text().split()
    assert "inputs/" in ignore and "data/" in ignore
