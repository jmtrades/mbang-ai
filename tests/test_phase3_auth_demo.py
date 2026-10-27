import time

import pytest

from operator_ai import auth, config, demo, tenancy
from tests.conftest import demo_login, login, owner_password


# ---------------------------------------------------------------- roles / RBAC
def test_five_roles_and_tab_matrix():
    assert auth.ROLES == ("owner", "coach", "setter", "closer", "student")
    assert set(auth.tabs_for("owner")) >= {"queue", "audit", "golive", "kb", "builder"}
    assert "audit" not in auth.tabs_for("coach") and "queue" in auth.tabs_for("coach")
    assert "students" not in auth.tabs_for("setter") and "outreach" in auth.tabs_for("setter")
    assert "calls" in auth.tabs_for("closer") and "outreach" not in auth.tabs_for("closer")
    student = set(auth.tabs_for("student"))
    assert {"academy", "missions", "prospects", "coach", "checkin", "queue"} <= student
    assert not ({"audit", "kb", "golive", "students", "team", "setup"} & student)


@pytest.mark.parametrize("role,path,status", [
    ("student", "/audit", 403), ("coach", "/audit", 403), ("owner", "/audit", 200),
    ("student", "/queue", 200), ("setter", "/queue", 200), ("coach", "/queue", 200),
    ("student", "/settings", 200),
])
def test_rbac_api(client, role, path, status):
    demo_login(client, role)
    assert client.get(f"/t/demo/api{path}").status_code == status


def test_non_owner_cannot_change_settings(client):
    demo_login(client, "coach")
    r = client.put("/t/demo/api/settings", json={"key": "banned_names", "value": []})
    assert r.status_code == 403


def test_setter_sees_only_decidable_queue_kinds(client):
    demo_login(client, "setter")
    items = client.get("/t/demo/api/queue").json()["items"]
    assert items and all(i["kind"] in auth.ROLE_DECIDE["setter"] for i in items)


def test_me_lists_role_tabs(client):
    demo_login(client, "closer")
    me = client.get("/t/demo/api/me").json()
    assert me["user"]["role"] == "closer" and set(me["tabs"]) == auth.ROLE_TABS["closer"]
    assert me["is_demo"] is True


# ---------------------------------------------------------------- login throttle / sessions
def test_login_throttle_per_ip(client):
    for _ in range(8):
        assert client.post("/t/owner/api/auth/login", json={"username": "owner", "password": "wrong"}).status_code == 401
    r = client.post("/t/owner/api/auth/login", json={"username": "owner", "password": owner_password()})
    assert r.status_code == 429


def test_login_throttle_per_user(env):
    tenancy.ensure_defaults()
    t = tenancy.get("owner")
    for i in range(8):
        assert auth.login(t, "owner", "wrong", ip=f"10.0.0.{i}") is None
    with pytest.raises(auth.Throttled):
        auth.login(t, "owner", owner_password(), ip="10.0.0.99")


def test_login_ok_and_logout(client):
    login(client)
    assert client.get("/t/owner/api/me").status_code == 200
    client.post("/t/owner/api/auth/logout")
    assert client.get("/t/owner/api/me").status_code == 401


def test_session_expiry(client):
    login(client)
    t = tenancy.get("owner")
    with t.db() as c:
        c.execute("UPDATE sessions SET expires=?", (time.time() - 1,))
    assert client.get("/t/owner/api/me").status_code == 401


def test_cookie_scoped_to_tenant_and_httponly(client):
    r = login(client)
    sc = r.headers["set-cookie"]
    assert "Path=/t/owner" in sc and "HttpOnly" in sc and "Secure" not in sc


def test_secure_cookie_when_hosted(client, monkeypatch):
    monkeypatch.setenv("OPERATOR_COOKIE_SECURE", "1")
    r = login(client)
    assert "Secure" in r.headers["set-cookie"]


def test_audit_logs_logins(client):
    login(client)
    client.post("/t/owner/api/auth/login", json={"username": "owner", "password": "nope"})
    from operator_ai import audit

    kinds = [a["kind"] for a in audit.recent(tenancy.get("owner"), 10)]
    assert "login" in kinds and "login.fail" in kinds


# ---------------------------------------------------------------- demo workspace (C5)
def test_demo_db_is_separate_file(client):
    demo_login(client)
    d, o = tenancy.get("demo"), tenancy.get("owner")
    assert d.db_path == config.get().demo_db_path and d.db_path != o.db_path


def test_demo_seed_counts_and_labels(demo_tenant):
    with demo_tenant.db() as c:
        n_students = c.execute("SELECT COUNT(*) FROM students").fetchone()[0]
        n_leads = c.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        n_queue = c.execute("SELECT COUNT(*) FROM queue").fetchone()[0]
        weeks = c.execute("SELECT MAX(cnt) FROM (SELECT COUNT(*) cnt FROM checkins GROUP BY student_id)").fetchone()[0]
        names = [r[0] for r in c.execute("SELECT name FROM students UNION ALL SELECT name FROM leads")]
        emails = [r[0] for r in c.execute("SELECT email FROM students UNION ALL SELECT email FROM leads")]
        ads = c.execute("SELECT COUNT(*) FROM mock_ads").fetchone()[0]
    assert n_students == 24 and n_leads == 37 and n_queue >= 18 and weeks == 6 and ads > 0
    assert all(n.endswith("(DEMO)") for n in names)
    assert all(e.endswith("@example.invalid") for e in emails)


def test_demo_queue_has_guardrail_examples(demo_tenant):
    with demo_tenant.db() as c:
        st = {r[0] for r in c.execute("SELECT status FROM queue")}
    assert {"pending", "held", "needs_human"} <= st


def test_real_workspace_has_no_demo_records(client):
    demo_login(client)
    o = tenancy.get("owner")
    with o.db() as c:
        for table in ("students", "leads", "clients", "queue", "checkins", "crm_contacts", "members"):
            assert c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table


def test_demo_reset_refused_on_real(env):
    tenancy.ensure_defaults()
    with pytest.raises(demo.DemoResetRefused):
        demo.reset(tenancy.get("owner"))
    with pytest.raises(demo.DemoResetRefused):
        demo.seed(tenancy.get("owner"))


def test_demo_reset_refused_when_flags_disagree(env):
    tenancy.ensure_defaults()
    d = tenancy.get("demo")
    d.set("is_demo", False)  # the database itself no longer says it is the demo DB
    with pytest.raises(demo.DemoResetRefused):
        demo.reset(d)


def test_demo_reset_api_refused_on_real(owner):
    assert owner.post("/t/owner/api/demo/reset").status_code == 403


def test_demo_reset_backs_up_and_reseeds(client):
    demo_login(client)
    t = tenancy.get("demo")
    with t.db() as c:
        c.execute("DELETE FROM students WHERE id > 3")
    r = client.post("/t/demo/api/demo/reset")
    assert r.status_code == 200
    bk = config.get().data_dir / "backups" / r.json()["backup"]
    assert bk.exists() and oct(bk.stat().st_mode)[-3:] == "600"
    with t.db() as c:
        assert c.execute("SELECT COUNT(*) FROM students").fetchone()[0] == 24


def test_demo_login_only_on_demo_tenant(client):
    assert client.post("/t/owner/api/auth/demo-login", json={"role": "owner"}).status_code == 403


def test_demo_whoami_label(client):
    demo_login(client, "owner")
    me = client.get("/t/demo/api/me").json()
    assert me["user"]["display"] == "Owner (DEMO)" and me["is_demo"]



def test_student_queue_shows_only_own_outreach(client):
    from operator_ai import queue, tenancy

    demo_login(client, "student")
    me = client.get("/t/demo/api/me").json()["user"]["id"]
    t = tenancy.get("demo")
    mine = queue.add(t, "prospect_dm", "Quick question about your clinic's bookings?", channel="instagram_dm",
                     meta={"prospect_facing": True, "owner_user_id": me})
    other = queue.add(t, "prospect_dm", "Quick question?", channel="instagram_dm", meta={"prospect_facing": True, "owner_user_id": 999})
    ids = {i["id"] for i in client.get("/t/demo/api/queue").json()["items"]}
    assert mine["id"] in ids and other["id"] not in ids
    assert client.post(f"/t/demo/api/queue/{other['id']}/approve").status_code in (403, 404)
    assert client.post(f"/t/demo/api/queue/{mine['id']}/approve").json()["status"] == "approved"
