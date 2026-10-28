"""Measurements behind the architecture gates (docs/ENTERPRISE_STANDARD.md). Each metric returns {key: value} where a
key names one place in the code (a file, a file::rule, a file::function) and the value is how bad it is there (a count
or a size). tests/test_arch.py compares them with the committed baselines in tests/arch_baselines/; the baselines are
written by scripts/update_arch_baselines.py and may only shrink."""
from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINES = ROOT / "tests" / "arch_baselines"

PY_MODULE_MAX = 800    # ARCH-3
PY_FUNCTION_MAX = 80   # ARCH-3
TSX_MAX = 600          # ARCH-3
ANY_RE = re.compile(r":\s*any\b|<any>|\bas any\b|\bany\[\]")  # FE-2
RAW_STATUS_RE = re.compile(r"\b(?:UPDATE|DELETE FROM|INSERT INTO)\s+(?:queue|jobs|cadences|actions|mission_steps|pipeline_stages)\b")  # ARCH-4
SLEEP_RE = re.compile(r"\btime\.sleep\(")  # QE-6


def _rel(p: Path) -> str:
    return p.resolve().relative_to(ROOT).as_posix()


def _py_files(*dirs: str) -> list[Path]:
    return sorted(p for d in dirs for p in (ROOT / d).rglob("*.py") if "__pycache__" not in p.parts)


def tool(name: str) -> str | None:
    """The tool from the project venv when present, else from PATH."""
    venv = ROOT / ".venv" / "bin" / name
    return str(venv) if venv.exists() else shutil.which(name)


# --------------------------------------------------------------------------- metrics
def ruff_findings() -> dict[str, int]:
    exe = tool("ruff")
    if not exe:
        raise FileNotFoundError("ruff")
    out = subprocess.run([exe, "check", "--output-format", "json", "--no-cache", "operator_ai", "scripts", "tests"],
                         cwd=ROOT, capture_output=True, text=True, check=False).stdout
    counts: dict[str, int] = {}
    for f in json.loads(out or "[]"):
        key = f"{_rel(Path(f['filename']))}::{f['code']}"
        counts[key] = counts.get(key, 0) + 1
    return counts


_MYPY_LINE = re.compile(r"^(?P<file>[^:]+\.py):\d+(?::\d+)?: error: .*?(?:\[(?P<code>[\w-]+)\])?$")


def mypy_errors() -> dict[str, int]:
    exe = tool("mypy")
    if not exe:
        raise FileNotFoundError("mypy")
    out = subprocess.run([exe, "operator_ai", "--no-error-summary", "--no-pretty", "--show-error-codes"],
                         cwd=ROOT, capture_output=True, text=True, check=False).stdout
    counts: dict[str, int] = {}
    for line in out.splitlines():
        m = _MYPY_LINE.match(line.strip())
        if m:
            key = f"{m.group('file')}::{m.group('code') or 'misc'}"
            counts[key] = counts.get(key, 0) + 1
    return counts


def python_sizes() -> dict[str, int]:
    """Modules over PY_MODULE_MAX lines and functions over PY_FUNCTION_MAX lines, keyed file or file::qualname."""
    out: dict[str, int] = {}
    for p in _py_files("operator_ai"):
        src = p.read_text(encoding="utf-8")
        n = src.count("\n") + (0 if src.endswith("\n") else 1)
        if n > PY_MODULE_MAX:
            out[_rel(p)] = n

        def walk(node: ast.AST, prefix: str) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    q = f"{prefix}{child.name}"
                    size = (child.end_lineno or child.lineno) - child.lineno + 1
                    if size > PY_FUNCTION_MAX:
                        out[f"{_rel(p)}::{q}"] = size
                    walk(child, q + ".")
                elif isinstance(child, ast.ClassDef):
                    walk(child, f"{prefix}{child.name}.")

        walk(ast.parse(src), "")
    return out


def tsx_sizes() -> dict[str, int]:
    out = {}
    for p in sorted((ROOT / "web" / "src").rglob("*.tsx")):
        n = p.read_text(encoding="utf-8").count("\n") + 1
        if n > TSX_MAX:
            out[_rel(p)] = n
    return out


def _count(pattern: re.Pattern, files: list[Path]) -> dict[str, int]:
    out = {}
    for p in files:
        n = len(pattern.findall(p.read_text(encoding="utf-8")))
        if n:
            out[_rel(p)] = n
    return out


def web_any() -> dict[str, int]:
    files = sorted(p for ext in ("*.ts", "*.tsx") for p in (ROOT / "web" / "src").rglob(ext))
    return _count(ANY_RE, files)


def raw_status_writes() -> dict[str, int]:
    return _count(RAW_STATUS_RE, _py_files("operator_ai"))


def sleeps_in_tests() -> dict[str, int]:
    return _count(SLEEP_RE, [p for p in _py_files("tests") if "perf" not in p.parts])


METRICS = {
    "ruff": ruff_findings,
    "mypy": mypy_errors,
    "python_sizes": python_sizes,
    "tsx_sizes": tsx_sizes,
    "web_any": web_any,
    "raw_status_writes": raw_status_writes,
    "test_sleeps": sleeps_in_tests,
}


# --------------------------------------------------------------------------- baselines
def load_baseline(name: str) -> dict[str, int]:
    p = BASELINES / f"{name}.json"
    return json.loads(p.read_text()) if p.exists() else {}


def save_baseline(name: str, values: dict[str, int]) -> None:
    BASELINES.mkdir(parents=True, exist_ok=True)
    (BASELINES / f"{name}.json").write_text(json.dumps(dict(sorted(values.items())), indent=1) + "\n")


def compare(current: dict[str, int], baseline: dict[str, int]) -> tuple[list[str], list[str]]:
    """(regressions, fixed). A regression is a new key or a value above its baseline. Fixed lists baseline keys that
    are gone entirely; they must be removed so the gate cannot later re-admit them."""
    regressions = [f"{k}: {v} (baseline {baseline.get(k, 0)})" for k, v in sorted(current.items()) if v > baseline.get(k, 0)]
    fixed = [k for k in sorted(baseline) if k not in current]
    return regressions, fixed
