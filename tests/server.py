"""Start a real uvicorn server (temp data dir, AI off) for browser tests."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def ensure_web_built() -> None:
    if not (ROOT / "web" / "dist" / "index.html").exists():
        subprocess.run(["npm", "run", "build"], cwd=ROOT / "web", check=True, capture_output=True)


@contextmanager
def running_server(data_dir: Path, extra_env: dict | None = None):
    ensure_web_built()
    port = free_port()
    inputs = data_dir / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "OPERATOR_DATA_DIR": str(data_dir / "data"), "OPERATOR_CORPUS_DIR": str(inputs),
           "OPERATOR_DISABLE_LLM": "1", "OPERATOR_EMBEDDINGS": "hash", "OPERATOR_BUILDER_AI": "0",
           "OPERATOR_INDEX_DIR": str(data_dir / "index"), **(extra_env or {})}
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPERATOR_LLM_API_KEY"):
        env.pop(k, None)
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "operator_ai.app:app", "--host", "127.0.0.1", "--port",
                             str(port), "--log-level", "warning"], cwd=ROOT, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(150):
            try:
                if httpx.get(base + "/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if proc.poll() is not None:
                raise RuntimeError(proc.stdout.read().decode()[-2000:])
            time.sleep(0.1)
        else:
            raise RuntimeError("server did not start")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
