import os
import re
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from operator_ai import config, queue, reports, scorecard, tenancy
from tests.conftest import demo_login

ROOT = config.ROOT


# ---------------------------------------------------------------- backup / restore (temp dir)
def test_backup_restore_roundtrip(env, tmp_path):
    from scripts import backup, restore

    tenancy.ensure_defaults()
    t, _ = tenancy.create("acme", "Acme")
    queue.add(t, "note", "before backup")
    from operator_ai import vault

    vault.put(t, {"meta_app_id": "123"})
    dest = tmp_path / "bk"
    arcs = [backup.backup(dest, keep=20) for _ in range(1)]
    a = arcs[0]
    assert stat.S_IMODE(os.stat(a).st_mode) == 0o600
    ok, notes = restore.check(a)
    assert ok and any("acme/tenant.db: ok" in n for n in notes) and any("registry.db" in n for n in notes)
    assert any("demo.db" in n for n in notes)  # every tenant + demo + registry
    queue.add(t, "note", "after backup")
    aside = restore.restore(a)
    assert aside.exists()
    tenancy.clear_cache()
    from operator_ai import db

    db._initialised.clear()
    t2 = tenancy.get("acme")
    bodies = [i["body"] for i in queue.list_items(t2)]
    assert bodies == ["before backup"]
    assert vault.get(t2, "meta_app_id") == "123"


def test_backup_keeps_20(env, tmp_path):
    from scripts import backup

    tenancy.ensure_defaults()
    dest = tmp_path / "bk"
    for i in range(22):
        (dest).mkdir(exist_ok=True)
        (dest / f"operator-2000010{i:02d}-000000.tgz").write_bytes(b"x")
    backup.backup(dest, keep=20)
    assert len(list(dest.glob("operator-*.tgz"))) == 20


def test_restore_check_rejects_bad_archive(tmp_path):
    import tarfile

    from scripts import restore

    bad = tmp_path / "bad.tgz"
    with tarfile.open(bad, "w:gz") as tar:
        p = tmp_path / "data"
        p.mkdir()
        (p / "x.db").write_bytes(b"not a database")
        tar.add(p, arcname="data")
    with pytest.raises(Exception):
        ok, _ = restore.check(bad)
        assert ok


def test_backup_sh_and_restore_sh_cli(env, tmp_path):
    tenancy.ensure_defaults()
    envv = {**os.environ, "PY": sys.executable}
    r = subprocess.run(["bash", str(ROOT / "scripts" / "backup.sh"), str(tmp_path / "bk")], capture_output=True, text=True, env=envv)
    assert r.returncode == 0, r.stderr
    arc = next((tmp_path / "bk").glob("operator-*.tgz"))
    r = subprocess.run(["bash", str(ROOT / "scripts" / "restore.sh"), str(arc), "--check"], capture_output=True, text=True, env=envv)
    assert r.returncode == 0 and "CHECK OK" in r.stdout


# ---------------------------------------------------------------- watchdog + log rotation
FAKE_APP = """
import http.server, sys
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'{"ok":true}')
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
"""


def test_watchdog_restarts_dead_process(tmp_path):
    from tests.server import free_port

    port = free_port()
    app = tmp_path / "fake_app.py"
    app.write_text(FAKE_APP)
    logs = tmp_path / "logs"
    env = {**os.environ, "OPERATOR_PORT": str(port), "OPERATOR_WD_INTERVAL": "1", "OPERATOR_LOG_DIR": str(logs),
           "OPERATOR_PID_FILE": str(logs / "op.pid"), "OPERATOR_START_CMD": f'"{sys.executable}" "{app}" {port}',
           "OPERATOR_WD_BACKUP_SECONDS": "999999"}
    wd = subprocess.Popen(["bash", str(ROOT / "scripts" / "watchdog.sh")], cwd=ROOT, env=env, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        for _ in range(50):
            if (logs / "op.pid").exists():
                break
            time.sleep(0.1)
        pid1 = int((logs / "op.pid").read_text())
        os.kill(pid1, signal.SIGKILL)  # the app dies
        pid2 = pid1
        for _ in range(60):
            time.sleep(0.2)
            try:
                pid2 = int((logs / "op.pid").read_text())
            except ValueError:
                continue
            if pid2 != pid1:
                break
        assert pid2 != pid1, "watchdog did not restart the dead process"
        assert "process dead, restarting now" in (logs / "watchdog.log").read_text()
    finally:
        os.killpg(wd.pid, signal.SIGTERM)
        wd.wait(timeout=10)
        try:
            os.kill(int((logs / "op.pid").read_text()), signal.SIGKILL)
        except (OSError, ValueError):
            pass


def test_watchdog_only_kills_its_own_process():
    text = (ROOT / "scripts" / "watchdog.sh").read_text()
    assert "pkill" not in text and "killall" not in text and 'kill "$(cat "$PID_FILE")"' in text
    assert "FAILS_MAX=${OPERATOR_WD_FAILS:-4}" in text and "21600" in text


def test_log_rotation(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "app.log").write_bytes(b"x" * 2000)
    r = subprocess.run(["bash", str(ROOT / "scripts" / "rotate_logs.sh")], env={**os.environ, "OPERATOR_LOG_DIR": str(logs),
                                                                               "OPERATOR_LOG_MAX": "1000"}, cwd=ROOT)
    assert r.returncode == 0
    assert (logs / "app.log.1").stat().st_size == 2000 and (logs / "app.log").stat().st_size == 0


def test_prune_cache(env):
    from scripts import prune_cache

    stale = config.get().index_dir / "course-deadbeef-hash-384"
    stale.mkdir(parents=True, exist_ok=True)
    removed = prune_cache.prune()
    assert any("course-deadbeef" in r for r in removed) and not stale.exists()


# ---------------------------------------------------------------- deploy bundle lint
def test_deploy_bundle_lint():
    d = ROOT / "deploy"
    need = ["Dockerfile", "docker-compose.yml", "Caddyfile", ".env.template", "healthcheck.sh", "offsite_backup.sh",
            "smoke_local.sh", "README.md", "systemd/operator-ai.service", "systemd/operator-ai-health.timer",
            "systemd/operator-ai-health.service", "systemd/operator-ai-backup.timer", "systemd/operator-ai-backup.service",
            "systemd/operator-ai-logrotate.timer", "systemd/operator-ai-logrotate.service"]
    for n in need:
        assert (d / n).exists(), n
    df = (d / "Dockerfile").read_text()
    assert "HEALTHCHECK" in df and "USER operator" in df and "--proxy-headers" in df and "tesseract-ocr" in df
    assert "OPERATOR_COOKIE_SECURE=1" in df
    compose = (d / "docker-compose.yml").read_text()
    assert "../inputs:/app/inputs:ro" in compose and "caddy" in compose and "healthcheck" in compose
    assert "reverse_proxy app:8850" in (d / "Caddyfile").read_text()
    svc = (d / "systemd" / "operator-ai.service").read_text()
    assert "Restart=always" in svc and "--proxy-headers" in svc
    assert "OnUnitActiveSec=6h" in (d / "systemd" / "operator-ai-backup.timer").read_text()
    env = (d / ".env.template").read_text()
    for line in env.splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if any(s in k for s in ("KEY", "SECRET", "TOKEN", "PASSWORD")) and "BUDGET" not in k:
            assert v == "", f"{k} must be blank in the template"
    for sh in list(d.glob("*.sh")) + list((ROOT / "scripts").glob("*.sh")):
        assert subprocess.run(["bash", "-n", str(sh)]).returncode == 0, sh
        assert os.access(sh, os.X_OK), f"{sh} not executable"


def test_env_template_not_gitignored():
    r = subprocess.run(["git", "check-ignore", "deploy/.env.template"], cwd=ROOT, capture_output=True)
    assert r.returncode == 1  # not ignored
    r = subprocess.run(["git", "check-ignore", "deploy/.env"], cwd=ROOT, capture_output=True)
    assert r.returncode == 0  # the filled file is ignored


# ---------------------------------------------------------------- Command Centre / Morning brief / Go-live
def test_golive_scorecard(client):
    demo_login(client, "owner")
    r = client.get("/t/demo/api/golive").json()
    ids = [x["id"] for x in r["rows"]]
    assert len([i for i in ids if i.startswith("A")]) == 38 and len([i for i in ids if i.startswith("B")]) == 13
    assert len([i for i in ids if i.startswith("C")]) == 12 and len([i for i in ids if i.startswith("D")]) == 12
    assert len([i for i in ids if i.startswith("E")]) == 6
    assert all(x["status"] in ("REAL_OK", "NEEDS_LOGIN", "NEEDS_AI_BUDGET") and x["evidence"] for x in r["rows"])
    a16 = next(x for x in r["rows"] if x["id"] == "A16")
    assert a16["status"] == "NEEDS_LOGIN" and a16["missing"] == ["telegram_bot_token"]


def test_golive_flips_when_login_added(demo_tenant):
    from operator_ai import vault

    vault.put(demo_tenant, {"telegram_bot_token": "123:abc"})
    row = next(x for x in scorecard.rows_for(demo_tenant) if x["id"] == "A16")
    assert row["status"] == "NEEDS_LOGIN" and row["missing"] == []  # login present, switch still off
    from operator_ai import settings

    settings.set_box(demo_tenant, "live_channels_enabled", True, "test")
    row = next(x for x in scorecard.rows_for(demo_tenant) if x["id"] == "A16")
    assert row["status"] == "REAL_OK"


def test_scorecard_evidence_exists():
    """Every test node id cited as evidence exists (no invented evidence)."""
    for r in scorecard.ROWS:
        for ev in r["evidence"]:
            if not ev.startswith("tests/"):
                continue
            path, _, name = ev.partition("::")
            src = (ROOT / path).read_text()
            assert re.search(rf"def {re.escape(name)}\(", src), f"{r['id']}: {ev} not found"


def test_morning_brief_ranked_read_only(demo_tenant):
    before = _snapshot(demo_tenant)
    b = reports.morning_brief(demo_tenant)
    assert b["read_only"] and b["items"] and [i["rank"] for i in b["items"]] == list(range(1, len(b["items"]) + 1))
    scores = [i["score"] for i in b["items"]]
    assert scores == sorted(scores, reverse=True)
    assert b["items"][0]["title"].startswith("Fix") and "held" in b["items"][0]["title"]
    assert b["text"].startswith("Morning brief")
    assert _snapshot(demo_tenant) == before


def _snapshot(t):
    with t.db() as c:
        return tuple(c.execute(f"SELECT COUNT(*) FROM {x}").fetchone()[0] for x in ("queue", "runs", "audit", "actions", "jobs"))


def test_command_centre_and_day_one(client):
    demo_login(client, "owner")
    cc = client.get("/t/demo/api/command-centre").json()
    assert cc["kpis"]["students"] == 24 and cc["is_demo"] and cc["recent_runs"]
    d1 = client.get("/t/demo/api/day-one").json()
    assert len(d1["cards"]) == 6 and all(isinstance(c["count"], int) for c in d1["cards"])
    demo_login(client, "student")
    assert client.get("/t/demo/api/command-centre").status_code == 403
    assert client.get("/t/demo/api/day-one").status_code == 200
