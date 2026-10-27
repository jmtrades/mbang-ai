"""Hostile isolation review: who can see whose run traces, the coach's student scope, the public demo's heavy
actions and public forms, the MCP batch size and other people's call holds. Everything runs offline."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from operator_ai import auth, client_ops, console, db, mcp_server, tenancy
from tests.conftest import demo_login, owner_password

API = "/t/owner/api"
LB = {"lawful_basis": "legitimate_interest", "lawful_note": "Business contact details from their own website"}


@pytest.fixture
def world(app, tenant):
    """stu_a (coached by coach_a) and stu_b (coached by coach_b), a setter and the owner, each signed in."""
    users = {}
    with tenant.db() as c:
        users["owner"] = c.execute("SELECT id FROM users WHERE username='owner'").fetchone()["id"]
    for name, role in [("stu_a", "student"), ("stu_b", "student"), ("coach_a", "coach"), ("coach_b", "coach"),
                       ("setter1", "setter")]:
        users[name] = auth.create_user(tenant, name, f"pw-{name}-123456", role)
    with tenant.db() as c:
        for stu, coach in (("stu_a", "coach_a"), ("stu_b", "coach_b")):
            sid = c.execute("INSERT INTO students(name,email,coach_id,joined,stage,clients,demo) VALUES(?,?,?,?,?,0,0)",
                            (f"Student {stu}", f"{stu}@example.invalid", users[coach], db.now() - 90 * 86400,
                             "foundations")).lastrowid
            c.execute("UPDATE users SET student_id=? WHERE id=?", (sid, users[stu]))
    clients: dict = {}

    def as_(name: str) -> TestClient:
        if name not in clients:
            cl = TestClient(app)
            pw = owner_password() if name == "owner" else f"pw-{name}-123456"
            r = cl.post(f"{API}/auth/login", json={"username": name, "password": pw})
            assert r.status_code == 200, r.text
            clients[name] = cl
        return clients[name]

    return SimpleNamespace(t=tenant, users=users, as_=as_)


def _student_activity(w) -> dict:
    """stu_a asks the coach privately and drafts an opener to their own prospect."""
    a = w.as_("stu_a")
    assert a.post(f"{API}/coach/ask", json={"question": "How do I price my first retainer offer?"}).status_code == 200
    p = a.post(f"{API}/prospects", json={**LB, "name": "Quinn Hollow", "business": "Hollow Fitness",
                                          "email": "quinn.hollow@example.invalid", "handle": "@hollowfit"})
    assert p.status_code == 200, p.text
    pid = p.json()["prospect"]["id"]
    d = a.post(f"{API}/prospects/{pid}/drafts", json={"channel": "instagram_dm", "move": "question"})
    assert d.status_code == 200, d.text
    return {"pid": pid, "run_id": d.json()["run_id"]}


# ------------------------------------------------------------------ run traces
def test_a_coach_never_reads_run_traces_of_people_they_do_not_coach(world):
    act = _student_activity(world)
    with world.t.db() as c:
        a_runs = [r["id"] for r in c.execute("SELECT id FROM runs WHERE actor LIKE 'stu_a(%' ORDER BY id")]
    assert len(a_runs) >= 3 and act["run_id"] in a_runs
    coach_b = world.as_("coach_b")
    listed = coach_b.get(f"{API}/runs").json()["runs"]
    assert not [r for r in listed if r["id"] in a_runs], listed
    for rid in a_runs:
        assert coach_b.get(f"{API}/runs/{rid}").status_code == 404
    text = coach_b.get(f"{API}/runs?limit=500").text
    for secret in ("Quinn Hollow", "How do I price my first retainer offer"):
        assert secret not in text
    # another student and a setter don't see them either
    for who in ("stu_b", "setter1"):
        cl = world.as_(who)
        assert not [r for r in cl.get(f"{API}/runs").json()["runs"] if r["id"] in a_runs]
        assert cl.get(f"{API}/runs/{act['run_id']}").status_code == 404


def test_a_coach_still_reads_their_own_students_runs_and_the_owner_reads_all(world):
    act = _student_activity(world)
    coach_a = world.as_("coach_a")
    assert coach_a.get(f"{API}/runs/{act['run_id']}").status_code == 200
    assert act["run_id"] in {r["id"] for r in coach_a.get(f"{API}/runs").json()["runs"]}
    # their own runs too
    assert coach_a.post(f"{API}/coach/ask", json={"question": "What is a good show rate?"}).status_code == 200
    mine = [r for r in coach_a.get(f"{API}/runs").json()["runs"] if r["actor"].startswith("coach_a(")]
    assert mine and coach_a.get(f"{API}/runs/{mine[0]['id']}").status_code == 200
    owner = world.as_("owner")
    assert owner.get(f"{API}/runs/{act['run_id']}").status_code == 200
    assert act["run_id"] in {r["id"] for r in owner.get(f"{API}/runs").json()["runs"]}
    # the student sees their own
    assert world.as_("stu_a").get(f"{API}/runs/{act['run_id']}").status_code == 200


def test_a_coach_reads_the_trace_of_a_reply_in_their_students_inbox_but_not_anyone_elses(world):
    act = _student_activity(world)
    with world.t.db() as c:  # a reply that arrived by webhook: the run was started by the system, not a person
        rid = c.execute("INSERT INTO runs(agent,title,plan,steps,result,status,ms,ai_mode,created,actor) "
                        "VALUES('inbound','Inbound reply','[]','[]',NULL,'done',0,'rules',?,'system')", (db.now(),)).lastrowid
        c.execute("INSERT INTO inbound_messages(channel,party_type,party_id,owner_user_id,body,status,run_id,created) "
                  "VALUES('email','prospect',?,?,'yes please','new',?,?)", (act["pid"], world.users["stu_a"], rid, db.now()))
        other = c.execute("INSERT INTO runs(agent,title,plan,steps,result,status,ms,ai_mode,created,actor) "
                          "VALUES('fulfilment','Fulfil a client','[]','[]',NULL,'done',0,'rules',?,'system')",
                          (db.now(),)).lastrowid
    assert world.as_("coach_a").get(f"{API}/runs/{rid}").status_code == 200
    assert world.as_("coach_b").get(f"{API}/runs/{rid}").status_code == 404
    # a system run nobody links to a coached person stays with the owner
    assert world.as_("coach_a").get(f"{API}/runs/{other}").status_code == 404
    assert world.as_("owner").get(f"{API}/runs/{other}").status_code == 200


def test_run_actors_are_matched_exactly_not_by_prefix(world):
    # a run started by "stu_a(student)x" is not stu_a's run, although its actor starts with "stu_a("
    auth.create_user(world.t, "stu_a(student)x", "pw-lookalike-123456", "student")
    cl = TestClient(world.as_("owner").app)
    assert cl.post(f"{API}/auth/login", json={"username": "stu_a(student)x", "password": "pw-lookalike-123456"}).status_code == 200
    assert cl.post(f"{API}/coach/ask", json={"question": "What is a good opener?"}).status_code == 200
    with world.t.db() as c:
        rid = c.execute("SELECT id FROM runs WHERE actor='stu_a(student)x(student)'").fetchone()["id"]
    a = world.as_("stu_a")
    assert a.get(f"{API}/runs/{rid}").status_code == 404
    assert rid not in {r["id"] for r in a.get(f"{API}/runs").json()["runs"]}
    assert cl.get(f"{API}/runs/{rid}").status_code == 200


# ------------------------------------------------------------------ coach scope on the accountability board
def test_a_coach_digest_and_sweep_cover_only_their_own_students(world):
    coach_b = world.as_("coach_b")
    d = coach_b.get(f"{API}/students/digest")
    assert d.status_code == 200
    assert "Student stu_a" not in d.text and "Student stu_b" in d.text
    sweep = coach_b.post(f"{API}/students/interventions")
    assert sweep.status_code == 200, sweep.text
    assert "Student stu_a" not in sweep.text
    with world.t.db() as c:
        sa = c.execute("SELECT student_id FROM users WHERE id=?", (world.users["stu_a"],)).fetchone()[0]
        nudged = {db.jload(r["meta"], {}).get("student_id") for r in c.execute("SELECT meta FROM queue WHERE kind='nudge'")}
        tasks = {r["student_id"] for r in c.execute("SELECT student_id FROM coach_tasks")}
    assert sa not in nudged and sa not in tasks
    # the owner still sees and sweeps everyone
    od = world.as_("owner").get(f"{API}/students/digest")
    assert "Student stu_a" in od.text and "Student stu_b" in od.text
    star = coach_b.get(f"{API}/students/star")
    assert star.status_code == 200 and star.json()["points"]


def test_a_coach_morning_brief_names_only_their_students_and_no_clients(world):
    with world.t.db() as c:  # stu_a's own client, with numbers that have a clear bottleneck
        c.execute("INSERT INTO clients(name,price,metrics,demo,created,owner_user_id) VALUES(?,?,?,0,?,?)",
                  ("Hollow Fitness Ltd", 2000, db.jdump({"ad_spend": 3000, "leads": 300, "booked": 2, "showed": 1, "closed": 0}),
                   db.now(), world.users["stu_a"]))
    owner_brief = world.as_("owner").get(f"{API}/brief").text
    assert "Student stu_a" in owner_brief  # the owner's brief is unchanged
    b = world.as_("coach_b").get(f"{API}/brief")
    assert b.status_code == 200
    assert "Student stu_a" not in b.text and "Hollow Fitness" not in b.text
    assert "Student stu_b" in b.text


# ------------------------------------------------------------------ the public demo
def test_demo_heavy_owner_actions_are_throttled_per_network(client, demo_tenant, monkeypatch):
    monkeypatch.setenv("OPERATOR_LIMIT_DEMO_HEAVY", "2")
    demo_login(client, "owner")
    codes = [client.post("/t/demo/api/evals/run", json={"kind": "reviewer"}).status_code for _ in range(3)]
    assert codes == [200, 200, 429], codes
    assert client.post("/t/demo/api/burst", json={"n": 5}).status_code == 429  # one shared budget
    assert client.post("/t/demo/api/academy/build").status_code == 429


def test_demo_burst_is_capped(client, demo_tenant):
    demo_login(client, "owner")
    assert client.post("/t/demo/api/burst", json={"n": 2000}).status_code == 422
    r = client.post("/t/demo/api/burst", json={"n": 20})
    assert r.status_code == 200 and r.json()["leads"] == 20


def test_real_workspace_heavy_actions_are_not_demo_throttled(owner, monkeypatch):
    monkeypatch.setenv("OPERATOR_LIMIT_DEMO_HEAVY", "1")
    for _ in range(3):
        assert owner.post(f"{API}/evals/run", json={"kind": "reviewer"}).status_code == 200
    assert owner.post(f"{API}/burst", json={"n": 600}).status_code == 200


def _demo_webinar_slug(t) -> str:
    with t.db() as c:
        return c.execute("SELECT slug FROM webinars WHERE status<>'draft' AND page IS NOT NULL AND starts_at>? "
                         "ORDER BY id DESC LIMIT 1", (db.now(),)).fetchone()["slug"]


def test_demo_webinar_page_takes_only_fictional_addresses(client, demo_tenant):
    slug = _demo_webinar_slug(demo_tenant)
    page = client.get(f"/w/demo/{slug}")
    assert page.status_code == 200 and "DEMO" in page.text and "example.invalid" in page.text
    real = client.post(f"/w/demo/{slug}/register", json={"name": "Real Person", "email": "real.person@gmail.com",
                                                          "consent": True})
    assert real.status_code == 422 and "example.invalid" in real.json()["detail"]
    with demo_tenant.db() as c:
        assert not c.execute("SELECT 1 FROM webinar_registrants WHERE email='real.person@gmail.com'").fetchone()
    ok = client.post(f"/w/demo/{slug}/register", json={"name": "Pat Visitor", "email": "pat@example.invalid", "consent": True})
    assert ok.status_code == 200, ok.text


def test_demo_intake_and_mini_take_only_fictional_addresses(client, demo_tenant):
    demo_login(client, "owner")
    r = client.post("/t/demo/api/clients", json={"name": "Visitor Client (DEMO)", "onboard": True})
    assert r.status_code == 200, r.text
    tok = client_ops.get_client(demo_tenant, r.json()["id"])["intake_token"]
    page = client.get(f"/c/demo/{tok}")
    assert page.status_code == 200 and "DEMO" in page.text
    body = {"offer": "Coaching", "audience": "busy parents"}
    bad = client.post(f"/c/demo/{tok}", json={**body, "email": "someone.real@outlook.com"})
    assert bad.status_code == 422 and "example.invalid" in bad.json()["detail"]
    assert client.post(f"/c/demo/{tok}", json={**body, "email": "client@example.invalid"}).status_code == 200
    console.create_mini(demo_tenant, "ads-helper", "Ads helper", [], 2, "I agree (DEMO)")
    mini = client.get("/m/demo/ads-helper")
    assert mini.status_code == 200 and "DEMO" in mini.text
    assert client.post("/m/demo/ads-helper/start", json={"email": "lead@gmail.com", "consent": True}).status_code == 422
    assert client.post("/m/demo/ads-helper/start", json={"email": "lead@example.invalid", "consent": True}).status_code == 200


def test_real_workspace_public_forms_take_real_addresses(owner, tenant):
    with tenant.db() as c:
        cid = c.execute("INSERT INTO clients(name,price,metrics,demo,created) VALUES('Acme Studio',3000,'{}',0,?)",
                        (db.now(),)).lastrowid
    client_ops.start_onboarding(tenant, cid, run_fulfilment=False)
    tok = client_ops.get_client(tenant, cid)["intake_token"]
    page = owner.get(f"/c/owner/{tok}")
    assert page.status_code == 200 and "DEMO" not in page.text
    r = owner.post(f"/c/owner/{tok}", json={"offer": "Coaching", "audience": "busy parents", "email": "hello@acme.example.com"})
    assert r.status_code == 200, r.text


def test_demo_brand_logo_cannot_point_at_another_site(client, demo_tenant):
    demo_login(client, "owner")
    r = client.post("/t/demo/api/setup/brand", json={"logo": "https://tracker.example.com/pixel.png"})
    assert r.status_code == 422, r.text
    r = client.put("/t/demo/api/settings", json={"key": "brand", "value": {"name": "Demo", "logo": "https://tracker.example.com/p.png"}})
    assert r.status_code == 422, r.text
    assert "tracker.example.com" not in client.get("/t/demo/api/public").text
    assert client.post("/t/demo/api/setup/brand", json={"logo": "/logo.png"}).status_code == 200


def test_real_workspace_brand_logo_may_be_an_https_link(owner):
    r = owner.post(f"{API}/setup/brand", json={"logo": "https://cdn.example.com/logo.png"})
    assert r.status_code == 200, r.text


# ------------------------------------------------------------------ MCP
def test_mcp_batch_size_is_bounded(owner):
    t = tenancy.get("owner")
    tok = mcp_server.mint(t, "laptop", "owner")
    H = {"authorization": f"Bearer {tok}"}
    one = {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "search_library", "arguments": {"query": "show rate"}}}
    small = owner.post("/mcp", headers=H, json=[{**one, "id": i} for i in range(5)])
    assert small.status_code == 200 and len(small.json()) == 5
    big = owner.post("/mcp", headers=H, json=[{**one, "id": i} for i in range(500)])
    assert big.status_code == 200 and big.json()["error"]["code"] == -32600


def test_mcp_calls_are_rate_limited_per_network(owner, monkeypatch):
    monkeypatch.setenv("OPERATOR_LIMIT_MCP", "3")
    tok = mcp_server.mint(tenancy.get("owner"), "laptop", "owner")
    H = {"authorization": f"Bearer {tok}"}
    one = {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "list_skills", "arguments": {}}}
    assert owner.post("/mcp", headers=H, json=[{**one, "id": i} for i in range(3)]).status_code == 200
    r = owner.post("/mcp", headers=H, json={**one, "id": 9})
    assert r.status_code == 429 and r.headers.get("retry-after")


# ------------------------------------------------------------------ call holds
def test_a_setter_cannot_cancel_a_students_private_prospect_call(world):
    act = _student_activity(world)
    a = world.as_("stu_a")
    slot = a.get(f"{API}/prospects/{act['pid']}/slots").json()["slots"][0]
    r = a.post(f"{API}/prospects/{act['pid']}/book", json={"mode": "record", "start": slot["start"]})
    assert r.status_code == 200, r.text
    eid = r.json()["event_id"]
    assert world.as_("setter1").post(f"{API}/bookings/{eid}/cancel").status_code == 404
    with world.t.db() as c:
        assert c.execute("SELECT status FROM calendar_events WHERE id=?", (eid,)).fetchone()["status"] != "cancelled"
    assert world.as_("owner").post(f"{API}/bookings/{eid}/cancel").status_code == 200


# ------------------------------------------------------------------ public webinar unsubscribe page
def test_webinar_unsubscribe_page_needs_a_real_registrant_token(client, tenant):
    import time

    from operator_ai import webinars

    w = webinars.create(tenant, {"title": "Unannounced pricing masterclass", "starts_at": time.time() + 3 * 86400,
                                 "tz": "Europe/London"}, {"id": 1, "username": "owner", "role": "owner"})
    slug = webinars.get(tenant, w["id"])["slug"]
    # a draft (unpublished) event's title is not readable with any made-up token
    r = client.get(f"/w/owner/{slug}/unsubscribe?r=madeuptoken123")
    assert r.status_code == 404 and "masterclass" not in r.text
    with tenant.db() as c:
        c.execute("INSERT INTO webinar_registrants(webinar_id,name,email,source,consent,registered_at,token,demo) "
                  "VALUES(?,?,?,?,?,?,?,0)", (w["id"], "Una", "una@x.example.com", "manual", 1, db.now(), "realtoken-abc123"))
    assert client.get(f"/w/owner/{slug}/unsubscribe?r=realtoken-abc123").status_code == 200
