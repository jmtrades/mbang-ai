import socket

import pytest

from tests.conftest import StrayHostError


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_ready_reports_components(client):
    r = client.get("/ready")
    assert r.status_code == 200
    checks = r.json()["checks"]
    for k in ("db", "index", "workers", "embeddings", "llm", "ads_adapter", "allowlist_size", "live_switches",
              "tenants"):
        assert k in checks
    assert checks["llm"]["enabled"] is False
    assert checks["allowlist_size"] == 0


def test_default_tenants_exist(client):
    from operator_ai import tenancy

    ids = {t.id for t in tenancy.list_all()}
    assert {"owner", "demo"} <= ids


def test_owner_password_file_mode_600(client):
    import os
    import stat

    from operator_ai import config

    p = config.get().tenants_dir / "owner" / "owner_password.txt"
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_stray_host_blocked(env):
    with pytest.raises(StrayHostError):
        socket.create_connection(("93.184.216.34", 80), timeout=1)
    with pytest.raises(StrayHostError):
        socket.getaddrinfo("example.com", 443)


def test_unknown_tenant_404(client):
    assert client.get("/t/nope/api/public").status_code == 404
    assert client.get("/t/nope/").status_code == 404
