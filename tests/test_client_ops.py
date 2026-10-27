"""Client delivery after the win, webinar execution and niche-neutral templates.

Covers: client_won -> onboarding (checklist, intake link, kickoff invite for approval, fulfilment from the client's
brief), the public intake form, metrics (manual, Ads CSV, Meta read gated by the live switch), the weekly loop
(report draft, monthly review, churn risk -> escalation), RBAC and cross-user isolation, webinars (approved page,
consent + honeypot + rate limit, approve-once fan-out with send_after / cancel_if, unsubscribe, attendance import,
post-event follow-ups and replays, rates -> diagnostic), demo vs real workspace, malformed input, AI off and on, and
a five-niche regression over every outreach, studio, fulfilment and webinar template."""
from __future__ import annotations

import re
import sys
import time
import types

import pytest

from operator_ai import (auth, brief, client_ops, db, events, guardrails, outreach_loop, queue, settings, skills, studio,
                         webinar_chain, webinars)
from operator_ai.agents import fulfilment, sales
from tests.conftest import demo_login, login


@pytest.fixture(autouse=True)
def _clear_rate_limits():
    webinars._rl.clear()
    yield
    webinars._rl.clear()


def _client(t, name="Acme Studio", niche="online fitness coaches", offer="12-week coaching programme", price=1800,
            brief_json=None, owner=None, email="owner@acme.example.com"):
    with t.db() as c:
        return c.execute("INSERT INTO clients(student_id,name,niche,offer,price,metrics,demo,created,brief,owner_user_id,email) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (None, name, niche, offer, price, "{}", 0, db.now(),
                                                         db.jdump(brief_json) if brief_json else None, owner, email)).lastrowid


def _user(t, username, role="student", pw="pw-test-123456"):
    return auth.create_user(t, username, pw, role)


def _sent(t) -> int:
    with t.db() as c:
        return c.execute("SELECT COUNT(*) FROM channel_messages WHERE direction='out'").fetchone()[0]


def _items(t, client_id):
    return [i for i in queue.list_items(t, limit=500) if i["meta"].get("client_id") == client_id]


# =============================================================================== onboarding after the win
def test_client_won_starts_onboarding_with_everything_gated(tenant):
    uid = _user(tenant, "stu1")
    cid = _client(tenant, owner=None)
    events.emit(tenant, "client_won", {"client_id": cid, "prospect_id": None, "owner_user_id": uid})
    cl = client_ops.get_client(tenant, cid)
    assert cl["status"] == "onboarding" and cl["owner_user_id"] == uid and cl["intake_token"] and cl["start_date"]
    keys = [t["key"] for t in client_ops.tasks(tenant, cid)]
    assert keys == client_ops.CHECK_KEYS and all(t["status"] == "open" for t in client_ops.tasks(tenant, cid))
    items = _items(tenant, cid)
    kick = [i for i in items if i["meta"].get("purpose") == "client_kickoff"]
    assert len(kick) == 1 and kick[0]["kind"] == "booking_dm" and kick[0]["status"] == "pending"
    assert kick[0]["meta"]["owner_user_id"] == uid and kick[0]["kind"] in auth.OWN_DECIDE
    assert f"/c/{tenant.id}/{cl['intake_token']}" in kick[0]["body"]
    ful = [i for i in items if i["meta"].get("fulfilment")]
    assert {i["kind"] for i in ful} == {"client_report", "ad_hooks", "vsl_outline", "launch_plan"}
    assert all(i["meta"]["owner_user_id"] == uid and i["meta"]["client_asset"] for i in ful)
    assert all(i["status"] in queue.OPEN for i in items)
    assert _sent(tenant) == 0  # nothing left the building
    with tenant.db() as c:
        assert c.execute("SELECT COUNT(*) FROM runs WHERE agent='client_ops'").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM runs WHERE agent='fulfilment'").fetchone()[0] == 1


def test_onboarding_is_idempotent_and_ignores_bad_events(tenant):
    cid = _client(tenant)
    events.emit(tenant, "client_won", {"client_id": cid})
    tok = client_ops.get_client(tenant, cid)["intake_token"]
    n_ful = len([i for i in _items(tenant, cid) if i["meta"].get("fulfilment")])
    kick = [i for i in _items(tenant, cid) if i["meta"].get("purpose") == "client_kickoff"][0]
    queue.approve(tenant, kick["id"], "owner(owner)")
    events.emit(tenant, "client_won", {"client_id": cid})  # a repeated win event drafts nothing new
    assert len([i for i in _items(tenant, cid) if i["meta"].get("fulfilment")]) == n_ful == 4
    client_ops.start_onboarding(tenant, cid, run_fulfilment=False)
    events.emit(tenant, "client_won", {"client_id": "not-a-number"})
    events.emit(tenant, "client_won", {})
    assert len(client_ops.tasks(tenant, cid)) == 7
    assert client_ops.get_client(tenant, cid)["intake_token"] == tok
    kicks = [i for i in _items(tenant, cid) if i["meta"].get("purpose") == "client_kickoff"]
    assert len(kicks) == 1


def test_owner_of_client_approves_own_kickoff_others_cannot(tenant):
    a, b = _user(tenant, "stuA"), _user(tenant, "stuB")
    cid = _client(tenant)
    client_ops.start_onboarding(tenant, cid, owner_user_id=a, run_fulfilment=False)
    kick = [i for i in _items(tenant, cid) if i["meta"].get("purpose") == "client_kickoff"][0]
    with pytest.raises(queue.QueueError) as e:
        queue.approve(tenant, kick["id"], "stuB(student)", role="student", user_id=b)
    assert e.value.status == 403
    done = queue.approve(tenant, kick["id"], "stuA(student)", role="student", user_id=a)
    assert done["status"] == "approved"


def test_checklist_ticks_and_completes_onboarding(tenant):
    cid = _client(tenant)
    client_ops.start_onboarding(tenant, cid, run_fulfilment=False)
    for k in client_ops.CHECK_KEYS[:-1]:
        r = client_ops.set_task(tenant, cid, k, True, "owner")
    assert r["left"] == 1 and client_ops.get_client(tenant, cid)["status"] == "onboarding"
    r = client_ops.set_task(tenant, cid, client_ops.CHECK_KEYS[-1], True, "owner")
    assert r["left"] == 0 and client_ops.get_client(tenant, cid)["status"] == "active"
    with pytest.raises(client_ops.ClientOpsError):
        client_ops.set_task(tenant, cid, "not_a_key", True, "owner")
    other = _client(tenant, name="No checklist")
    with pytest.raises(client_ops.ClientOpsError) as e:
        client_ops.set_task(tenant, other, "crm", True, "owner")
    assert e.value.status == 404


def test_live_verify_needs_switch_and_allowlist(tenant):
    cid = _client(tenant)
    client_ops.start_onboarding(tenant, cid, run_fulfilment=False)
    r = client_ops.verify_task(tenant, cid, "meta_partner_access", "owner")
    assert r["verified"] is False and "off" in r["reason"]
    assert client_ops.verify_task(tenant, cid, "crm", "owner")["verified"] is False
    settings.set_box(tenant, "live_meta_enabled", True, "test yes")
    client_ops.update(tenant, cid, {"access": {"ad_account_id": "act_123"}}, "owner")
    r = client_ops.verify_task(tenant, cid, "meta_partner_access", "owner")
    assert r["verified"] is False and "allowlist" in r["reason"]  # never calls out without the allowlist
    assert client_ops.meta_metrics(tenant, client_ops.get_client(tenant, cid)) is None
    settings.set_box(tenant, "live_meta_enabled", False, "")


# =============================================================================== intake form (public)
def test_intake_form_updates_brief_and_reruns_fulfilment(client, tenant):
    cid = _client(tenant, niche="", offer="")
    client_ops.start_onboarding(tenant, cid)
    before = [i for i in _items(tenant, cid) if i["meta"].get("fulfilment")]
    tok = client_ops.get_client(tenant, cid)["intake_token"]
    page = client.get(f"/c/owner/{tok}")
    assert page.status_code == 200 and "Acme Studio" in page.text and 'name="website"' in page.text
    body = {"offer": "Strength coaching for desk workers", "audience": "remote workers over 35",
            "niche": "online strength coaches", "outcome": "a pain-free back in 8 weeks",
            "mechanism": "15-minute daily routine", "pain": "long gym sessions", "timeframe": "8 weeks",
            "proof": "Three clients said their back pain eased.", "proof_permission": True,
            "assets": ["https://example.com/brand"], "email": "hello@acme.example.com", "report_weekday": 2}
    r = client.post(f"/c/owner/{tok}", json=body)
    assert r.status_code == 200, r.text
    cl = client_ops.get_client(tenant, cid)
    assert cl["brief"]["outcome"] == "a pain-free back in 8 weeks" and cl["report_weekday"] == 2
    assert cl["brief"]["proof"]["permission"] is True and cl["intake_at"]
    assert next(t for t in client_ops.tasks(tenant, cid) if t["key"] == "intake_form")["verified_by"] == "client"
    for it in before:
        assert queue.get(tenant, it["id"])["status"] == "rejected"  # superseded by the client's own words
    new_hooks = [i for i in _items(tenant, cid) if i["kind"] == "ad_hooks" and i["status"] in queue.OPEN]
    assert new_hooks and "pain-free back" in new_hooks[0]["body"]
    vsl = [i for i in _items(tenant, cid) if i["kind"] == "vsl_outline" and i["status"] in queue.OPEN][0]
    assert "Three clients said" in vsl["body"] and "permission" in vsl["body"]


@pytest.mark.parametrize("bad,code", [
    ({"offer": "x"}, 422),                                                        # no audience
    ({"offer": "x", "audience": "y", "assets": ["javascript:alert(1)"]}, 422),   # not a web address
    ({"offer": "x", "audience": "y", "email": "nope"}, 422),
    ({"offer": "x", "audience": "y", "report_weekday": 9}, 422),
    ({"offer": "x", "audience": "access_token=abcdef123"}, 422),                 # secrets refused
    ({"offer": ["list"], "audience": "y"}, 422),
    ({"offer": "x" * 400, "audience": "y"}, 422),
])
def test_intake_rejects_bad_input(client, tenant, bad, code):
    cid = _client(tenant)
    client_ops.start_onboarding(tenant, cid, run_fulfilment=False)
    tok = client_ops.get_client(tenant, cid)["intake_token"]
    assert client.post(f"/c/owner/{tok}", json=bad).status_code == code


def test_intake_token_honeypot_rate_limit_and_malformed(client, tenant):
    cid = _client(tenant)
    client_ops.start_onboarding(tenant, cid, run_fulfilment=False)
    tok = client_ops.get_client(tenant, cid)["intake_token"]
    assert client.get("/c/owner/not-a-real-token-123").status_code == 404
    assert client.get(f"/c/nobody/{tok}").status_code == 404
    assert client.post(f"/c/owner/{tok}", content=b"{broken", headers={"content-type": "application/json"}).status_code == 422
    assert client.post(f"/c/owner/{tok}", json=[1, 2]).status_code == 422
    r = client.post(f"/c/owner/{tok}", json={"offer": "x", "audience": "y", "website": "spam.example"})
    assert r.status_code == 200 and client_ops.get_client(tenant, cid)["intake_at"] is None  # bot: nothing stored
    codes = [client.post(f"/c/owner/{tok}", json={"offer": "", "audience": ""}).status_code for _ in range(11)]
    assert codes[:10] == [422] * 10 and codes[10] == 429
    client_ops.update(tenant, cid, {"status": "ended"}, "owner")
    webinars._rl.clear()
    assert client.get(f"/c/owner/{tok}").status_code == 404  # an ended client's link stops working


def test_proof_without_permission_is_never_used(tenant):
    cid = _client(tenant)
    client_ops.start_onboarding(tenant, cid, run_fulfilment=False)
    tok = client_ops.get_client(tenant, cid)["intake_token"]
    client_ops.submit_intake(tenant, tok, {"offer": "o", "audience": "a", "proof": "A testimonial from Sam.",
                                           "proof_permission": False})
    vsl = [i for i in _items(tenant, cid) if i["kind"] == "vsl_outline" and i["status"] in queue.OPEN][0]
    assert "Sam" not in vsl["body"] and "none on file" in vsl["body"]


# =============================================================================== metrics and the weekly loop
CSV = ("Ad name,Amount spent (USD),Results,Impressions,Link clicks\n"
       "Hook A,120.50,6,4000,60\nHook B,80,2,2500,20\n")


def test_metrics_manual_csv_and_validation(tenant):
    cid = _client(tenant)
    r = client_ops.record_metrics(tenant, cid, {"leads": 30, "booked": 8}, source="manual")
    client_ops.record_metrics(tenant, cid, {"closed": 2}, source="manual")  # same week updates the row
    r2 = client_ops.import_ads_csv(tenant, cid, CSV)
    assert r2["ads"] == 2 and r2["metrics"]["ad_spend"] == 200.5 and r2["metrics"]["leads"] == 8
    hist = client_ops.history(tenant, cid)
    assert len([h for h in hist if h["source"] == "manual"]) == 1 and hist[0]["period_start"] == r["period_start"]
    assert client_ops.get_client(tenant, cid)["metrics"]["closed"] == 2
    for bad in ({}, {"nonsense": 1}, {"leads": -1}, {"leads": "many"}, {"leads": float("inf")}):
        with pytest.raises(client_ops.ClientOpsError) as e:
            client_ops.record_metrics(tenant, cid, bad)
        assert e.value.status == 422
    with pytest.raises(client_ops.ClientOpsError):
        client_ops.import_ads_csv(tenant, cid, "just,some\nwords,here")
    with pytest.raises(client_ops.ClientOpsError) as e:
        client_ops.record_metrics(tenant, 9999, {"leads": 1})
    assert e.value.status == 404


def _weeks(t, cid, series):
    now = time.time()
    for i, m in enumerate(series):
        client_ops.record_metrics(t, cid, m, now - (len(series) - 1 - i) * client_ops.WEEK)


def test_weekly_ops_drafts_report_and_flags_churn_without_escalations_module(tenant, monkeypatch):
    uid = _user(tenant, "stuW")
    cid = _client(tenant, owner=uid)
    client_ops.start_onboarding(tenant, cid, run_fulfilment=False)
    _weeks(tenant, cid, [{"leads": 50, "booked": 12}, {"leads": 38, "booked": 9}, {"leads": 20, "booked": 4}])
    import operator_ai

    monkeypatch.setitem(sys.modules, "operator_ai.escalations", None)  # module missing: falls back to a task
    monkeypatch.delattr(operator_ai, "escalations", raising=False)
    r = client_ops.weekly_ops(tenant, cid)
    rep = queue.get(tenant, r["report_queue_id"])
    assert rep["kind"] == "client_report" and rep["meta"]["client_asset"] and rep["meta"]["owner_user_id"] == uid
    assert rep["status"] in queue.OPEN and rep["channel"] == "email" and rep["recipient_ref"] == "owner@acme.example.com"
    assert "Leads: 20" in rep["body"] and "down 47% on last week" in rep["body"] and "not a forecast" in rep["body"]
    assert r["mode"] == "rules" and "AI off" in r["mode_label"]
    codes = {s["code"] for s in r["signals"]}
    assert {"falling_leads", "falling_booked"} <= codes
    assert next(s for s in r["signals"] if s["code"] == "falling_leads")["severity"] == "high"  # two drops in a row
    assert any(t["kind"] == "risk" for t in client_ops.tasks(tenant, cid))
    assert client_ops.get_client(tenant, cid)["risk"]["severity"] == "high"
    assert _sent(tenant) == 0


def test_churn_risk_raises_escalation_when_module_exists(tenant, monkeypatch):
    calls = []
    fake = types.ModuleType("operator_ai.escalations")
    fake.raise_ = lambda t, kind, severity, detail, assignee_user_id=None, mission_id=None, ref=None: (
        calls.append((kind, severity, ref, assignee_user_id)) or 77)
    monkeypatch.setitem(sys.modules, "operator_ai.escalations", fake)
    import operator_ai

    monkeypatch.setattr(operator_ai, "escalations", fake, raising=False)
    cid = _client(tenant)
    for _ in range(2):
        client_ops.log_call(tenant, cid, "missed", "", "owner")
    r = client_ops.weekly_ops(tenant, cid)
    assert any(s["code"] == "missed_calls" and s["severity"] == "high" for s in r["signals"])
    assert calls and calls[0][0] == "client_churn_risk" and calls[0][2] == f"client:{cid}"
    client_ops.weekly_ops(tenant, cid)
    assert len(calls) == 1  # same signals the same week: not raised twice
    assert client_ops.get_client(tenant, cid)["risk"]["escalation_id"] == 77


def test_monthly_review_and_ad_proposals_only_when_due(demo_tenant):
    cid = _client(demo_tenant, name="Monthly Co (DEMO)")
    with demo_tenant.db() as c:
        c.execute("UPDATE clients SET start_date=?, status='active', access=? WHERE id=?",
                  (time.time() - 40 * 86400, db.jdump({"ad_account_id": "act_DEMO_practice"}), cid))
        c.execute("UPDATE mock_ads SET metrics=? WHERE id='ad_demo_2_1'",
                  (db.jdump({"spend": 200, "leads": 1, "impressions": 5000, "clicks": 40}),))
    for it in queue.list_items(demo_tenant, status="open", kind="ad_change"):  # the seeded demo proposal
        queue.reject(demo_tenant, it["id"], "owner")
    r = client_ops.weekly_ops(demo_tenant, cid)
    assert r["monthly_queue_id"] and queue.get(demo_tenant, r["monthly_queue_id"])["meta"]["report"] == "monthly"
    assert r["proposals"] and all(queue.get(demo_tenant, p["queue_id"])["kind"] == "ad_change" for p in r["proposals"])
    assert all(queue.get(demo_tenant, p["queue_id"])["status"] in queue.OPEN for p in r["proposals"])
    r2 = client_ops.weekly_ops(demo_tenant, cid)
    assert r2["monthly_queue_id"] is None  # once a month
    assert r2["proposals"] == []  # last week's proposals still wait for approval: no duplicates


def test_weekly_ops_ai_on_adds_checked_summary(tenant, monkeypatch):
    from operator_ai import llm
    from tests.fakes import FakeModel

    fm = FakeModel("Leads held steady this week while calls booked rose.")
    llm.set_transport(fm.transport)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-123456789")
    monkeypatch.setenv("OPERATOR_DISABLE_LLM", "0")
    settings.set_box(tenant, "ai_token_budget", 100000, "test budget")
    try:
        cid = _client(tenant)
        client_ops.record_metrics(tenant, cid, {"leads": 30})
        r = client_ops.weekly_ops(tenant, cid)
        assert r["mode"] == "live" and fm.calls
        rep = queue.get(tenant, r["report_queue_id"])
        assert rep["body"].splitlines()[1].startswith("Leads held steady") and rep["meta"]["written_by"] == "live"
        assert rep["meta"]["subject"].endswith("your weekly numbers") and not rep["body"].startswith("Subject:")
    finally:
        llm.set_transport(None)
        settings.set_box(tenant, "ai_token_budget", 0, "")
    fm.calls.clear()
    r = client_ops.weekly_ops(tenant, cid)  # budget back to 0: no request, replay of the saved line
    assert not fm.calls and r["mode"] in ("rules", "replay")


def test_tick_runs_on_the_chosen_weekday_once(tenant):
    cid = _client(tenant)
    client_ops.start_onboarding(tenant, cid, run_fulfilment=False)
    now = time.time()
    wd = time.localtime(now).tm_wday
    client_ops.update(tenant, cid, {"report_weekday": (wd + 1) % 7}, "owner")
    assert client_ops.tick(tenant, now)["ran"] == []
    client_ops.update(tenant, cid, {"report_weekday": wd}, "owner")
    assert len(client_ops.tick(tenant, now)["ran"]) == 1
    assert client_ops.tick(tenant, now + 60)["ran"] == []  # once a day
    client_ops.update(tenant, cid, {"status": "paused"}, "owner")
    with tenant.db() as c:
        c.execute("UPDATE clients SET last_report_at=NULL WHERE id=?", (cid,))
    assert client_ops.tick(tenant, now)["ran"] == []
    assert client_ops.tick.demo_too is True


# =============================================================================== API, RBAC, isolation
def test_clients_api_rbac_and_isolation(client, tenant):
    login(client)
    a = _user(tenant, "stu_api_a")
    b = _user(tenant, "stu_api_b")
    _user(tenant, "coach_api", role="coach")
    _user(tenant, "setter_api", role="setter")
    mine_owner = client.post("/t/owner/api/clients", json={"name": "Owner Client", "niche": "B2B consultants"}).json()["id"]
    cid_a = _client(tenant, name="A's client", owner=a)
    client_ops.start_onboarding(tenant, cid_a, run_fulfilment=False)
    _client(tenant, name="B's client", owner=b)
    assert {c["name"] for c in client.get("/t/owner/api/clients").json()["items"]} >= {"Owner Client", "A's client", "B's client"}
    assert client.get(f"/t/owner/api/clients/{mine_owner}").json()["status"] == "onboarding"

    login(client, username="stu_api_a", password="pw-test-123456")
    items = client.get("/t/owner/api/clients").json()["items"]
    assert [c["name"] for c in items] == ["A's client"] and "intake_token" not in items[0]
    d = client.get(f"/t/owner/api/clients/{cid_a}").json()
    assert d["intake_url"].endswith(client_ops.get_client(tenant, cid_a)["intake_token"]) and len(d["tasks"]) == 7
    assert client.get(f"/t/owner/api/clients/{mine_owner}").status_code == 404
    assert client.put(f"/t/owner/api/clients/{mine_owner}", json={"status": "ended"}).status_code == 404
    assert client.post(f"/t/owner/api/clients/{mine_owner}/fulfil").status_code == 404
    assert client.post(f"/t/owner/api/clients/{mine_owner}/metrics", json={"metrics": {"leads": 1}}).status_code == 404
    assert client.post(f"/t/owner/api/clients/{cid_a}/tasks/crm", json={"done": True}).status_code == 200
    new = client.post("/t/owner/api/clients", json={"name": "A's second client"}).json()["id"]
    assert client_ops.get_client(tenant, new)["owner_user_id"] == a
    assert client.get("/t/owner/api/webinars").status_code == 403

    login(client, username="stu_api_b", password="pw-test-123456")
    assert client.get(f"/t/owner/api/clients/{cid_a}").status_code == 404
    assert [c["name"] for c in client.get("/t/owner/api/clients").json()["items"]] == ["B's client"]
    for role_user in ("coach_api", "setter_api"):
        login(client, username=role_user, password="pw-test-123456")
        assert client.get("/t/owner/api/clients").status_code == 403
        assert client.get(f"/t/owner/api/clients/{cid_a}").status_code == 403


def test_clients_api_malformed_and_secret_guard(client, tenant):
    login(client)
    cid = client.post("/t/owner/api/clients", json={"name": "Shape Co", "onboard": False}).json()["id"]
    assert client_ops.get_client(tenant, cid)["status"] == "active" and not client_ops.tasks(tenant, cid)
    bad = [("post", "/clients", {"name": ""}), ("post", "/clients", {"name": "x", "email": "nope"}),
           ("post", f"/clients/{cid}/metrics", {"metrics": "lots"}), ("post", f"/clients/{cid}/metrics", {"metrics": {"zzz": 1}}),
           ("post", f"/clients/{cid}/metrics/csv", {"csv": "short"}), ("post", f"/clients/{cid}/calls", {"outcome": "maybe"}),
           ("post", f"/clients/{cid}/tasks/crm", {}), ("put", f"/clients/{cid}", {"status": "exploded"}),
           ("put", f"/clients/{cid}", {"report_weekday": 12}), ("put", f"/clients/{cid}", {"access": {"password": "x"}}),
           ("put", f"/clients/{cid}", {"access": {"notes": "token is EAAB1234567890abcdef"}}),
           ("put", f"/clients/{cid}", {"access": {"crm": "password hunter2"}})]
    for method, url, body in bad:
        r = getattr(client, method)(f"/t/owner/api{url}", json=body)
        assert r.status_code == 422, (url, body, r.status_code, r.text)
    for url in ("/clients", f"/clients/{cid}/metrics", f"/clients/{cid}/weekly", f"/clients/{cid}/onboard"):
        r = client.post(f"/t/owner/api{url}", content=b"{broken", headers={"content-type": "application/json"})
        assert r.status_code in (200, 422), (url, r.status_code)
    assert client.get("/t/owner/api/clients/99999").status_code == 404
    ok = client.put(f"/t/owner/api/clients/{cid}", json={"access": {"ad_account_id": "act_42"}, "kpi_targets": {"leads": 50},
                                                          "brief": {"outcome": "more demo calls"}})
    assert ok.status_code == 200 and ok.json()["access"]["ad_account_id"] == "act_42"
    assert ok.json()["brief"]["outcome"] == "more demo calls" and "outcome" not in ok.json()["brief_missing"]
    r = client.post(f"/t/owner/api/clients/{cid}/weekly")
    assert r.status_code == 200 and r.json()["report_queue_id"]


# =============================================================================== demo vs real workspace
def test_demo_seed_and_real_workspace_untouched(client, tenant):
    demo_login(client, "owner")
    items = client.get("/t/demo/api/clients").json()["items"]
    won = [c for c in items if c["status"] == "onboarding"]
    assert won and all("(DEMO)" in c["name"] for c in items) and won[0]["checklist"]["total"] == 7
    assert 0 < won[0]["checklist"]["done"] < 7
    assert all((c["email"] or "").endswith("@example.invalid") for c in items)
    ws = client.get("/t/demo/api/webinars").json()["items"]
    past = [w for w in ws if w["status"] == "done"]
    assert past and past[0]["rates"]["registrants"] > 0 and past[0]["rates"]["attended"] > 0
    d = client.get(f"/t/demo/api/webinars/{past[0]['id']}").json()
    assert all(r["email"].endswith("@example.invalid") and "(DEMO)" in r["name"] for r in d["registrants"])
    assert any(t["status"] != "approved" and t["phase"] == "replay" for t in d["templates"])
    from operator_ai import tenancy

    assert _sent(tenancy.get("demo")) == 0  # seeding the demo sends nothing, not even to the mock transport
    demo_login(client, "student")
    mine = client.get("/t/demo/api/clients").json()["items"]
    assert [c["status"] for c in mine] == ["onboarding"]
    with tenant.db() as c:
        assert c.execute("SELECT COUNT(*) FROM clients").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM webinars").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM client_tasks").fetchone()[0] == 0
    client_ops.seed_demo(tenant)
    webinars.seed_demo(tenant)
    with tenant.db() as c:  # never seeds the real workspace
        assert c.execute("SELECT COUNT(*) FROM clients").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM webinars").fetchone()[0] == 0


def test_real_workspace_empty_states(client):
    login(client)
    assert client.get("/t/owner/api/clients").json()["items"] == []
    assert client.get("/t/owner/api/webinars").json()["items"] == []


# =============================================================================== webinars
def _webinar(t, starts_in=3 * 86400, **kw):
    data = {"title": "Five numbers every coach should track", "starts_at": time.time() + starts_in, "tz": "Europe/London",
            "join_url": "https://zoom.example.com/j/1", "replay_url": "https://video.example.com/r/1", **kw}
    return webinars.create(t, data, {"id": 1, "username": "owner", "role": "owner"})


def _approve_page(t, w):
    queue.approve(t, w["page_queue_id"], "owner(owner)")
    return webinars.get(t, w["id"])


def _tpl_qid(w, key):
    return w["template_queue_ids"][[k[0] for k in webinars.TEMPLATES].index(key)]


def test_webinar_page_served_only_after_approval(client, tenant):
    w = _webinar(tenant)
    slug = webinars.get(tenant, w["id"])["slug"]
    assert webinars.get(tenant, w["id"])["status"] == "draft"
    assert client.get(f"/w/owner/{slug}").status_code == 404
    assert client.post(f"/w/owner/{slug}/register", json={"name": "A", "email": "a@x.example.com",
                                                          "consent": True}).status_code == 404
    items = [queue.get(tenant, q) for q in [w["page_queue_id"], *w["template_queue_ids"]]]
    assert len(items) == 8 and all(i["status"] == "pending" for i in items)
    assert {i["kind"] for i in items} == {"webinar_page", "webinar_template"}
    _approve_page(tenant, w)
    assert _sent(tenant) == 0  # approving a page or template sends nothing by itself
    page = client.get(f"/w/owner/{slug}")
    assert page.status_code == 200 and 'type="checkbox" name="consent"' in page.text and 'name="website"' in page.text
    assert "Five numbers every coach should track" in page.text and "(Europe/London)" in page.text
    assert webinars.get(tenant, w["id"])["visits"] == 1
    assert client.get("/w/owner/zz").status_code == 404 and client.get(f"/w/nobody/{slug}").status_code == 404


def test_registration_consent_honeypot_rate_limit(client, tenant):
    w = _webinar(tenant)
    slug = _approve_page(tenant, w)["slug"]
    url = f"/w/owner/{slug}/register"
    assert client.post(url, json={"name": "Ana", "email": "ana@x.example.com", "consent": False}).status_code == 422
    assert client.post(url, json={"name": "Ana", "email": "not-an-email", "consent": True}).status_code == 422
    assert client.post(url, json={"name": "", "email": "ana@x.example.com", "consent": True}).status_code == 422
    assert client.post(url, json={"name": "Ana", "email": "ana@x.example.com", "consent": "yes"}).status_code == 422
    assert client.post(url, content=b"{nope", headers={"content-type": "application/json"}).status_code == 422
    r = client.post(url, json={"name": "Bot", "email": "bot@x.example.com", "consent": True, "website": "http://spam"})
    assert r.status_code == 200 and not webinars.registrants(tenant, w["id"])
    webinars._rl.clear()  # the rejected attempts above count toward the limit too
    ok = client.post(url, json={"name": "Ana Lee", "email": "Ana@X.example.com", "consent": True, "source": "paid_social"})
    assert ok.status_code == 200 and "registered" in ok.json()["message"]
    again = client.post(url, json={"name": "Ana Lee", "email": "ana@x.example.com", "consent": True})
    assert again.json()["message"] == ok.json()["message"]  # no hint about who already signed up
    regs = webinars.registrants(tenant, w["id"])
    assert len(regs) == 1 and regs[0]["source"] == "paid_social" and regs[0]["consent"] == 1
    webinars._rl.clear()
    codes = [client.post(url, json={"name": f"P{i}", "email": f"p{i}@x.example.com", "consent": True}).status_code
             for i in range(6)]
    assert codes == [200] * 5 + [429]


@pytest.fixture
def delivery_holds(monkeypatch):
    """Simulate the delivery feature: queue.approve holds items whose send_after is in the future, so the fan-out
    creates every per-registrant item at once. (Without it, webinars holds them itself: tests/test_client_ops_review.py.)"""
    monkeypatch.setattr(webinars, "queue_holds_timed", lambda: True)


def test_approve_template_once_fans_out_per_registrant(tenant, delivery_holds):
    w = _webinar(tenant)
    wd = _approve_page(tenant, w)
    for i in range(3):
        webinars.register(tenant, wd["slug"], f"Person {i}", f"p{i}@x.example.com", True, ip=f"10.0.0.{i}")
    webinars.import_attendance(tenant, w["id"], "Name,Email,Duration (Minutes)\nWalk In,walkin@x.example.com,40\n")
    assert queue.list_items(tenant, kind="webinar_email") == []  # nothing until a template is approved
    queue.approve(tenant, _tpl_qid(w, "day_before"), "owner(owner)")
    sent = queue.list_items(tenant, kind="webinar_email")
    assert len(sent) == 3  # consented registrants only; the walk-in has no consent on file
    for it in sent:
        assert it["meta"]["send_after"] == pytest.approx(wd["starts_at"] - 86400)
        assert it["meta"]["cancel_if"] == ["unsubscribed"] and it["meta"]["template_key"] == "day_before"
        assert it["status"] == "approved" and "via approved template" in it["decided_by"]
        assert "{first_name}" not in it["body"] and "Person" in it["body"] and "/unsubscribe?r=" in it["body"]
        assert it["meta"]["subject"].startswith("Tomorrow:") and not it["body"].lower().startswith("subject:")
    queue.approve(tenant, _tpl_qid(w, "replay_1"), "owner(owner)")
    assert len(queue.list_items(tenant, kind="webinar_email")) == 3  # replays wait for the event to end
    webinars.fan_out(tenant, w["id"], "day_before")
    assert len(queue.list_items(tenant, kind="webinar_email")) == 3  # idempotent
    webinars.register(tenant, wd["slug"], "Late Comer", "late@x.example.com", True, ip="10.0.1.1")
    assert len(queue.list_items(tenant, kind="webinar_email")) == 4  # new registrants get approved emails too


def test_big_lists_fan_out_in_the_job_queue(tenant, monkeypatch, delivery_holds):
    from operator_ai import jobs

    monkeypatch.setattr(webinars, "FAN_INLINE_MAX", 1)
    w = _webinar(tenant)
    wd = _approve_page(tenant, w)
    for i in range(3):
        webinars.register(tenant, wd["slug"], f"Big {i}", f"big{i}@x.example.com", True, ip=f"10.9.0.{i}")
    queue.approve(tenant, _tpl_qid(w, "day_before"), "owner(owner)")
    assert queue.list_items(tenant, kind="webinar_email") == []  # handed to the background queue
    assert jobs.drain(tenant, timeout=20) >= 1
    assert len(queue.list_items(tenant, kind="webinar_email")) == 3


def test_flagged_fan_out_waits_for_a_person(tenant):
    settings.update_from_api(tenant, "banned_names", ["Zedoary"], "owner")
    w = _webinar(tenant)
    wd = _approve_page(tenant, w)
    webinars.register(tenant, wd["slug"], "Zedoary Quill", "zq@x.example.com", True, ip="1.1.1.1")
    queue.approve(tenant, _tpl_qid(w, "confirm"), "owner(owner)")
    it = queue.list_items(tenant, kind="webinar_email")[0]
    assert it["status"] == "held" and any(f["code"] == "banned_name" for f in it["flags"])


def test_late_registrant_skips_reminders_already_past(tenant, delivery_holds):
    w = _webinar(tenant, starts_in=20 * 60)  # starts in 20 minutes
    wd = _approve_page(tenant, w)
    for key in ("confirm", "day_before", "hour_before", "live"):
        queue.approve(tenant, _tpl_qid(w, key), "owner(owner)")
    webinars.register(tenant, wd["slug"], "Last Minute", "lm@x.example.com", True, ip="2.2.2.2")
    keys = {i["meta"]["template_key"] for i in queue.list_items(tenant, kind="webinar_email")}
    assert keys == {"confirm", "live"}


def test_unsubscribe_cancels_and_stops_future_sends(client, tenant):
    w = _webinar(tenant)
    wd = _approve_page(tenant, w)
    webinars.register(tenant, wd["slug"], "Una Sub", "una@x.example.com", True, ip="3.3.3.3")
    reg = webinars.registrants(tenant, w["id"])[0]
    with tenant.db() as c:
        tok = c.execute("SELECT token FROM webinar_registrants WHERE id=?", (reg["id"],)).fetchone()[0]
    settings.update_from_api(tenant, "banned_names", ["Una"], "owner")  # makes her confirm email held (open)
    queue.approve(tenant, _tpl_qid(w, "confirm"), "owner(owner)")
    held = queue.list_items(tenant, kind="webinar_email")[0]
    assert held["status"] == "held"
    assert client.get(f"/w/owner/{wd['slug']}/unsubscribe?r={tok}").status_code == 200
    assert client.get(f"/w/owner/{wd['slug']}/unsubscribe?r=bad").status_code == 404
    r = client.post(f"/w/owner/{wd['slug']}/unsubscribe", content=f"r={tok}",
                    headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 200 and "won't get any more emails" in r.text
    assert queue.get(tenant, held["id"])["status"] == "rejected"
    queue.approve(tenant, _tpl_qid(w, "day_before"), "owner(owner)")
    assert [i["meta"]["template_key"] for i in queue.list_items(tenant, kind="webinar_email")] == ["confirm"]
    assert client.post(f"/w/owner/{wd['slug']}/unsubscribe", content="r=wrong-token-123",
                       headers={"content-type": "application/x-www-form-urlencoded"}).status_code == 404


ZOOM = """Attendee Report
Report Generated:,"11/05/2026 08:10 PM"
Topic,Webinar ID,Actual Start Time
Five numbers,123,"11/05/2026 07:00 PM"
Attended,User Name (Original Name),First Name,Email,Join Time,Leave Time,Time in Session (minutes)
Yes,Stay Er,Stay,stay@x.example.com,11/05/2026 07:01:00 PM,11/05/2026 07:30:00 PM,29
Yes,Stay Er,Stay,stay@x.example.com,11/05/2026 07:32:00 PM,11/05/2026 07:58:00 PM,26
Yes,Left Early,Left,left@x.example.com,11/05/2026 07:00:00 PM,11/05/2026 07:20:00 PM,20
Yes,Walk In,Walk,walkin@x.example.com,11/05/2026 07:00:00 PM,11/05/2026 08:00:00 PM,60
"""


def test_attendance_import_post_event_followups_and_replays(tenant, delivery_holds):
    w = _webinar(tenant, starts_in=-2 * 3600, client_id=None)
    wd = _approve_page(tenant, w)
    with tenant.db() as c:
        for n, e in (("Stay Er", "stay@x.example.com"), ("Left Early", "left@x.example.com"), ("No Show", "ns@x.example.com")):
            c.execute("INSERT INTO webinar_registrants(webinar_id,name,email,source,consent,registered_at,token) "
                      "VALUES(?,?,?,?,?,?,?)", (w["id"], n, e, "email", 1, time.time() - 86400, f"tok-{e[:4]}-1234567"))
    queue.approve(tenant, _tpl_qid(w, "replay_1"), "owner(owner)")
    with pytest.raises(webinars.WebinarError):
        webinars.post_event(tenant, w["id"], now=wd["starts_at"] + 600)  # not finished yet
    res = webinars.import_attendance(tenant, w["id"], ZOOM, "owner", now=time.time())
    assert res["matched"] == 2 and res["added_without_consent"] == 1
    regs = {r["email"]: r for r in webinars.registrants(tenant, w["id"])}
    assert regs["stay@x.example.com"]["minutes_watched"] == 55 and regs["stay@x.example.com"]["stayed_past_offer"] == 1
    assert regs["left@x.example.com"]["attended"] == 1 and regs["left@x.example.com"]["stayed_past_offer"] == 0
    assert regs["ns@x.example.com"]["attended"] == 0 and regs["walkin@x.example.com"]["consent"] == 0
    pe = res["post_event"]
    assert len(pe["setter_drafts"]) == 1
    fu = queue.get(tenant, pe["setter_drafts"][0])
    assert fu["kind"] == "followup" and fu["status"] in queue.OPEN and fu["recipient_ref"] == "stay@x.example.com"
    assert fu["meta"]["prospect_facing"] and fu["meta"]["lead_tier"] == "warm" and fu["meta"]["owner_user_id"] == 1
    assert fu["kind"] in auth.ROLE_DECIDE["setter"]  # a setter can approve it
    codes = {f["code"] for f in guardrails.check_for(tenant, fu["body"], fu["meta"])}
    assert not codes & {"outreach_money", "outreach_price", "cold_lead_figure", "guarantee", "income_claim",
                        "deceptive_subject", "fake_scarcity", "fake_testimonial"}
    replays = queue.list_items(tenant, kind="webinar_email")
    assert sorted(i["recipient_ref"] for i in replays) == ["left@x.example.com", "ns@x.example.com"]
    assert all(i["meta"]["send_after"] == pytest.approx(wd["starts_at"] + 3600 + 7200) for i in replays)
    queue.approve(tenant, _tpl_qid(w, "replay_2"), "owner(owner)")  # approved later: fans out to the same people
    assert len(queue.list_items(tenant, kind="webinar_email")) == 4
    again = webinars.post_event(tenant, w["id"])
    assert again["setter_drafts"] == [] and again["replay_items"] == []  # each registrant handled once
    assert webinars.get(tenant, w["id"])["status"] == "done"
    r = webinars.rates(tenant, w["id"])
    assert r["registrants"] == 3 and r["attended"] == 2 and r["show_rate"] == pytest.approx(0.6667, abs=1e-3)
    assert r["attended_without_registering"] == 1 and r["stayed_past_offer"] == 1


def test_leave_time_on_the_day_decides_who_saw_the_offer(tenant):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    w = _webinar(tenant, starts_in=-3 * 3600)
    wd = _approve_page(tenant, w)
    webinars.add_registrant(tenant, w["id"], "Late Joiner", "late@x.example.com", True, "manual", "owner")
    start = datetime.fromtimestamp(wd["starts_at"], ZoneInfo("Europe/London"))
    fmt = lambda mins: (start + timedelta(minutes=mins)).strftime("%Y-%m-%d %H:%M:%S")
    csv_text = f"Email,Join Time,Leave Time,Duration (Minutes)\nlate@x.example.com,{fmt(30)},{fmt(50)},20\n"
    webinars.import_attendance(tenant, w["id"], csv_text, now=wd["starts_at"] + 30 * 60)
    reg = webinars.registrants(tenant, w["id"])[0]
    assert reg["minutes_watched"] == 20 and reg["stayed_past_offer"] == 1  # joined late but was there at minute 35


def test_attendance_csv_rejects_garbage(tenant):
    w = _webinar(tenant)
    for bad in ("no email column here\n1,2,3", "Email,Minutes\nnot-an-email,3\n"):
        with pytest.raises(webinars.WebinarError) as e:
            webinars.import_attendance(tenant, w["id"], bad)
        assert e.value.status == 422
    with pytest.raises(webinars.WebinarError):
        webinars.import_attendance(tenant, 999, ZOOM)


def test_rates_feed_diagnostic_and_client_metrics(tenant):
    cid = _client(tenant)
    w = _webinar(tenant, starts_in=-3 * 3600, client_id=cid)
    _approve_page(tenant, w)
    with tenant.db() as c:
        c.execute("UPDATE webinars SET visits=100 WHERE id=?", (w["id"],))
        for i in range(20):
            c.execute("INSERT INTO webinar_registrants(webinar_id,name,email,source,consent,registered_at,attended,"
                      "stayed_past_offer,booked,token) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (w["id"], f"R{i}", f"r{i}@x.example.com", "email", 1, time.time(), 1 if i < 5 else 0,
                       1 if i < 2 else 0, 1 if i == 0 else 0, f"tok{i:04d}abcdefgh"))
    r = webinars.rates(tenant, w["id"])
    assert r["reg_rate"] == 0.2 and r["show_rate"] == 0.25 and r["booked_rate"] == 0.2 and r["definitions"]
    d = webinars.diagnose(tenant, w["id"])
    assert {s["key"] for s in d["stages"]} >= {"wreg", "wshow"} and d["run_id"]
    webinars.tick(tenant, now=time.time())  # ended more than two hours ago: follows up and writes client numbers
    hist = client_ops.history(tenant, cid)
    assert hist and hist[-1]["source"] == "webinar" and hist[-1]["metrics"]["registrants"] == 20
    fm = webinars.funnel_metrics(tenant)
    assert fm["webinars"] == 1 and fm["registrants"] == 20 and fm["show_rate"] == 0.25
    assert webinars.tick.demo_too is True


def test_tick_closes_then_follows_up_after_grace(tenant):
    w = _webinar(tenant, starts_in=-65 * 60, duration_min=60)  # ended five minutes ago
    _approve_page(tenant, w)
    webinars.tick(tenant)
    assert webinars.get(tenant, w["id"])["status"] == "ended"
    webinars.tick(tenant, now=time.time() + 3 * 3600)
    assert webinars.get(tenant, w["id"])["status"] == "done"


def test_cancel_closes_registration(client, tenant):
    w = _webinar(tenant)
    wd = _approve_page(tenant, w)
    webinars.update(tenant, w["id"], {"status": "cancelled"}, "owner")
    assert client.post(f"/w/owner/{wd['slug']}/register", json={"name": "A", "email": "a@x.example.com",
                                                                "consent": True}).status_code == 409
    assert "cancelled" in client.get(f"/w/owner/{wd['slug']}").text


def test_webinar_api_rbac_and_malformed(client, tenant):
    login(client)
    good = {"title": "API webinar", "starts_at": "2026-12-03T19:00", "tz": "Europe/London",
            "join_url": "https://zoom.example.com/j/2"}
    r = client.post("/t/owner/api/webinars", json=good)
    assert r.status_code == 200, r.text
    wid = r.json()["id"]
    d = client.get(f"/t/owner/api/webinars/{wid}").json()
    assert d["status"] == "draft" and len(d["templates"]) == 7 and "(Europe/London)" in d["when"]
    for bad in ({**good, "tz": "Mars/Olympus"}, {**good, "starts_at": "next tuesday"}, {**good, "starts_at": "2099-01-01T10:00"},
                {**good, "join_url": "ftp://x"}, {**good, "offer_at_min": 90, "duration_min": 60}, {**good, "title": ""},
                {"title": "x"}, {**good, "client_id": 0}):
        assert client.post("/t/owner/api/webinars", json=bad).status_code == 422, bad
    assert client.post("/t/owner/api/webinars", json={**good, "client_id": 999}).status_code == 404
    assert client.post("/t/owner/api/webinars", json={**good, "chain_id": "wc-missing", "title": ""}).status_code == 404
    assert client.post(f"/t/owner/api/webinars/{wid}/registrants", json={"name": "A", "email": "a@x.example.com",
                                                                        "consent": False}).status_code == 422
    assert client.post(f"/t/owner/api/webinars/{wid}/registrants", json={"name": "A", "email": "a@x.example.com",
                                                                        "consent": True}).status_code == 200
    assert client.post(f"/t/owner/api/webinars/{wid}/registrants", json={"name": "A", "email": "a@x.example.com",
                                                                        "consent": True}).status_code == 409
    rid = client.get(f"/t/owner/api/webinars/{wid}").json()["registrants"][0]["id"]
    assert client.put(f"/t/owner/api/webinars/{wid}/registrants/{rid}", json={"minutes_watched": 900}).status_code == 422
    assert client.put(f"/t/owner/api/webinars/{wid}/registrants/{rid}", json={}).status_code == 422
    assert client.put(f"/t/owner/api/webinars/{wid}/registrants/{rid}", json={"minutes_watched": 50}).status_code == 200
    assert client.put(f"/t/owner/api/webinars/{wid}", json={"status": "done"}).status_code == 422
    assert client.post(f"/t/owner/api/webinars/{wid}/attendance", json={"csv": "x"}).status_code == 422
    assert client.post(f"/t/owner/api/webinars/{wid}/post-event").status_code == 409
    assert client.get("/t/owner/api/webinars/9999").status_code == 404
    assert client.get(f"/t/owner/api/webinars/{wid}/rates").status_code == 200
    for role in ("coach", "setter", "closer", "student"):
        _user(tenant, f"{role}_w", role=role)
        login(client, username=f"{role}_w", password="pw-test-123456")
        assert client.get("/t/owner/api/webinars").status_code == 403
        assert client.post("/t/owner/api/webinars", json=good).status_code == 403


def test_webinar_from_chain_uses_its_title_and_hooks(tenant):
    ch = webinar_chain.run_chain(tenant, {"niche": "online course creators", "outcome": "a full cohort every launch",
                                          "pain": "launch burnout", "mechanism": "evergreen webinar funnel"})
    w = webinars.create(tenant, {"chain_id": ch["chain_id"], "starts_at": time.time() + 86400, "tz": "UTC"}, None)
    wd = webinars.get(tenant, w["id"])
    assert "full cohort every launch" in wd["title"] and wd["chain_id"] == ch["chain_id"]
    page = queue.get(tenant, w["page_queue_id"])["body"]
    assert "evergreen webinar funnel" in page and webinars.parse_page(page)["bullets"]


# =============================================================================== niche-neutral templates
FIXTURES = {
    "fitness": ({"business": "Peak Form Coaching", "niche": "online fitness coaches", "audience": "fitness coaches",
                 "offer": "client accelerator", "outcome": "five new coaching clients a month", "timeframe": "60 days",
                 "mechanism": "content-to-DM system", "pain": "posting every day with no enquiries"},
                ["fitness", "workout", "gym"]),
    "b2b": ({"business": "Northline Advisory", "niche": "B2B consultants", "audience": "independent consultants",
             "offer": "authority programme", "outcome": "two new enterprise projects a quarter", "timeframe": "six months",
             "mechanism": "LinkedIn authority engine", "pain": "waiting on word of mouth"},
            ["consultant", "linkedin", "enterprise"]),
    "ecommerce": ({"business": "Glow Theory", "niche": "e-commerce skincare brands", "audience": "DTC founders",
                   "offer": "retention sprint", "outcome": "more repeat orders every month", "timeframe": "90 days",
                   "mechanism": "post-purchase email flow", "pain": "paying for every sale with ads"},
                  ["e-commerce", "skincare", "repeat order", "cart"]),
    "course": ({"business": "Learnloop", "niche": "online course creators", "audience": "course creators",
                "offer": "launch partner programme", "outcome": "a full cohort every launch", "timeframe": "one launch cycle",
                "mechanism": "evergreen webinar funnel", "pain": "launch burnout"},
               ["course", "cohort", "lesson"]),
    "physio": ({"business": "Harbour Physio", "niche": "independent physio clinics", "audience": "clinic owners",
                "offer": "patient growth programme", "outcome": "20 new patient bookings a month", "timeframe": "90 days",
                "mechanism": "referral-first system", "pain": "discounting"},
               ["clinic", "patient", "diary", "physio"]),
}
CLINIC_LEGACY = ["filling the diary", "fill your diary", "20 new bookings", "referral-first", "reminder ladder", "patients",
                 "clinics like yours", "front desk"]


def _set_profile(t, b):
    prof = {"identity": {"name": b["business"]}, "offer": {"name": b["offer"], "price": 2000, "outcome": b["outcome"],
                                                           "mechanism": b["mechanism"], "timeframe": b["timeframe"]},
            "audience": {"niche": b["niche"], "who": b["audience"], "pain": b["pain"]}}
    with t.db() as c:
        c.execute("DELETE FROM profiles WHERE scope='business'")
        c.execute("INSERT INTO profiles(scope,data,updated) VALUES('business',?,?)", (db.jdump(prof), db.now()))


def _render_everything(t, key: str) -> list[str]:
    b_in, _ = FIXTURES[key]
    _set_profile(t, b_in)
    b = brief.build(t, {})["brief"]
    texts = []
    for line in studio.LINES:
        texts += [d["body"] + " " + d["title"] for d in studio._drafts(line, b)]
    outs: dict = {}
    for step in webinar_chain.STEPS:
        outs[step] = webinar_chain._make(step, b, outs)
        texts.append(outs[step]["body"] + " " + outs[step]["title"])
    texts += [outreach_loop._fill(t, m, b["niche"]) for m in [*outreach_loop.MOVES.values(), *outreach_loop.FOLLOWUPS]]
    lead = {"answers": {"business": b["business"], "niche": b["niche"], "goal": b["outcome"]}}
    texts += sales.followups(lead) + [sales.reply_line(lead, "warm"), sales.reply_line(lead, "cold")]
    texts += fulfilment._hooks(b, fulfilment.HOOK_TEMPLATES + fulfilment.HOOK_RETRY)
    texts += [sk.fn({}, skills.get_profile(t)) for sk in skills.SKILLS.values()]  # the skills library (workers, chat)
    cid = _client(t, name=b["business"], niche=b["niche"], offer=b["offer"], price=2000,
                  brief_json={k: b[k] for k in ("audience", "outcome", "mechanism", "pain", "timeframe")})
    f = fulfilment.fulfil(t, cid)
    texts += [f["report"], *f["vsl_outline"], *[h["hook"] for h in f["hooks"]["hooks"]], *[p["step"] for p in f["launch_plan"]]]
    cl = client_ops.get_client(t, cid)
    texts.append(client_ops.kickoff_body(t, cl, "/c/x/y"))
    texts.append(client_ops.report_body(cl, client_ops.brief_mod.for_client(cl)["brief"], {"leads": 10}, {}, {},
                                        {"bottleneck": None}, [], "manual", False))
    w = webinars.create(t, {"title": f"How {b['niche']} get {b['outcome']}", "starts_at": time.time() + 86400,
                            "tz": "UTC"}, None)
    wd = webinars.get(t, w["id"])
    reg = {"name": "Sam Doe", "email": "s@x.example.com", "token": "t" * 16}
    texts += [webinars.render(tpl[4], wd, reg, t) for tpl in webinars.TEMPLATES]
    texts += [queue.get(t, w["page_queue_id"])["body"], webinars.SETTER_FOLLOWUP]
    lid = _lead(t, b)
    res = sales.process_lead(t, lid)
    texts += [queue.get(t, q)["body"] for q in res["queue_ids"]]
    return texts


def _lead(t, b):
    with t.db() as c:
        return c.execute("INSERT INTO leads(name,handle,email,source,answers,stage,created) VALUES(?,?,?,?,?,?,?)",
                         ("Lead Person", "@lead", "lead@x.example.com", "referral",
                          db.jdump({"monthly_revenue": "100k+", "can_invest": "yes", "timeline": "now", "decision_maker": True,
                                    "business": b["business"], "niche": b["niche"], "goal": b["outcome"]}), "new",
                          db.now())).lastrowid


@pytest.mark.parametrize("key", list(FIXTURES))
def test_no_foreign_niche_words_in_any_template(tenant, key):
    texts = _render_everything(tenant, key)
    assert len(texts) > 60
    foreign = [n for k, (_, nouns) in FIXTURES.items() if k != key for n in nouns]
    if key != "physio":
        foreign += CLINIC_LEGACY
    own = " ".join(FIXTURES[key][0].values()).lower()
    foreign = [n for n in foreign if n.lower() not in own]
    hits = []
    for text in texts:
        for n in foreign:
            if re.search(rf"\b{re.escape(n)}(s|es)?\b", text, re.I):
                hits.append((n, text[:160]))
    assert hits == [], hits[:5]
    joined = "\n".join(texts)
    assert FIXTURES[key][0]["outcome"] in joined and FIXTURES[key][0]["mechanism"] in joined


def test_defaults_are_niche_neutral(tenant):
    b = brief.build(tenant, {})
    assert set(b["missing"]) >= {"niche", "audience", "outcome", "mechanism", "pain"}
    texts = [d["body"] for line in studio.LINES for d in studio._drafts(line, b["brief"])]
    texts += fulfilment._hooks({}, fulfilment.HOOK_TEMPLATES)
    texts += [sk.fn({}, skills.get_profile(tenant)) for sk in skills.SKILLS.values()]  # empty profile: neutral fallbacks
    for text in texts:
        assert not re.search(r"clinic|diary|patient|physio|booking", text, re.I), text
