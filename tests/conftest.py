"""Offline test harness.

* AI disabled, temporary data directory, local hashed embeddings.
* Any socket connection to a non-loopback host raises, so a test that tries to reach a
  real host fails.
"""
from __future__ import annotations

import ipaddress
import socket
from pathlib import Path

import pytest

_real_connect = socket.socket.connect
_real_getaddrinfo = socket.getaddrinfo


class StrayHostError(RuntimeError):
    pass


def _is_local(host) -> bool:
    if host in ("localhost", "testserver", "", None):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return str(host).endswith(".localhost")


def _guard_connect(self, address):
    if isinstance(address, tuple) and not _is_local(address[0]):
        raise StrayHostError(f"tests must not reach real hosts: {address[0]}")
    return _real_connect(self, address)


def _guard_getaddrinfo(host, *a, **kw):
    if not _is_local(host):
        raise StrayHostError(f"tests must not resolve real hosts: {host}")
    return _real_getaddrinfo(host, *a, **kw)


socket.socket.connect = _guard_connect
socket.getaddrinfo = _guard_getaddrinfo

_SESSION_INDEX: Path | None = None


@pytest.fixture(scope="session")
def index_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("index")


@pytest.fixture
def env(tmp_path, monkeypatch, index_dir):
    data = tmp_path / "data"
    empty_inputs = tmp_path / "inputs"
    empty_inputs.mkdir()
    monkeypatch.setenv("OPERATOR_DATA_DIR", str(data))
    monkeypatch.setenv("OPERATOR_CORPUS_DIR", str(empty_inputs))
    monkeypatch.setenv("OPERATOR_INDEX_DIR", str(index_dir))
    monkeypatch.setenv("OPERATOR_DISABLE_LLM", "1")
    monkeypatch.setenv("OPERATOR_EMBEDDINGS", "hash")
    monkeypatch.setenv("OPERATOR_BUILDER_AI", "0")
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPERATOR_LLM_API_KEY", "OPERATOR_AI_TOKEN_BUDGET",
              "OPERATOR_CRITIQUE_TOKEN_BUDGET", "OPERATOR_BRAND"):
        monkeypatch.delenv(k, raising=False)
    from operator_ai import db, tenancy

    tenancy.clear_cache()
    db._initialised.clear()
    yield tmp_path
    tenancy.clear_cache()
    db._initialised.clear()


@pytest.fixture
def app(env):
    from operator_ai.app import create_app

    return create_app(start_workers=False)


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c


def owner_password(tid: str = "owner") -> str:
    from operator_ai import config

    return (config.get().tenants_dir / tid / "owner_password.txt").read_text().strip()


def login(client, tid: str = "owner", username: str = "owner", password: str | None = None):
    pw = password or owner_password(tid)
    r = client.post(f"/t/{tid}/api/auth/login", json={"username": username, "password": pw})
    assert r.status_code == 200, r.text
    return r


def demo_login(client, role: str = "owner"):
    r = client.post("/t/demo/api/auth/demo-login", json={"role": role})
    assert r.status_code == 200, r.text
    return r


@pytest.fixture
def owner(client):
    login(client)
    return client


@pytest.fixture
def tenant(env):
    from operator_ai import tenancy

    tenancy.ensure_defaults()
    return tenancy.get("owner")


@pytest.fixture
def demo_tenant(env):
    from operator_ai import demo, tenancy

    tenancy.ensure_defaults()
    t = tenancy.get("demo")
    demo.ensure_seeded(t)
    return t
