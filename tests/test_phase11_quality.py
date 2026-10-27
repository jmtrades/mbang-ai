import re
import subprocess
from pathlib import Path

import pytest

from operator_ai import auth, config, evals, index, tenancy
from tests.conftest import demo_login

ROOT = config.ROOT


# ---------------------------------------------------------------- API sweep (D10)
def test_api_sweep(app):
    from fastapi.testclient import TestClient

    from scripts.api_sweep import sweep

    with TestClient(app, raise_server_exceptions=False) as client:
        res = sweep(client, lambda c: demo_login(c, "owner"))
    assert res["checked"] > 150
    assert res["problems"] == [], res["problems"]


def test_malformed_bodies_get_422(client):
    demo_login(client, "owner")
    for url in ("/t/demo/api/coach/ask", "/t/demo/api/review", "/t/demo/api/studio/run", "/t/demo/api/builder/generate",
                "/t/demo/api/outreach/goal", "/t/demo/api/ads/build-test", "/t/demo/api/queue/1/reject"):
        r = client.post(url, content=b"{broken", headers={"content-type": "application/json"})
        assert r.status_code == 422, (url, r.status_code)
    assert client.put("/t/demo/api/settings", json={"key": 5}).status_code == 422


# ---------------------------------------------------------------- RBAC matrix (A28)
TAB_PROBE = {
    "home": "/day-one", "missions": "/missions", "academy": "/academy", "playbooks": "/playbooks", "prospects": "/prospects",
    "team": "/team", "setup": "/setup", "command": "/command-centre", "brief": "/brief", "coach": "/voice-note/deck", "threads": "/threads",
    "review": "/review/types", "coaching": "/coaching", "sales": "/leads", "outreach": "/outreach", "calls": "/calls",
    "drills": "/drills", "students": "/students", "onboarding": "/onboarding", "checkin": "/checkin/mine",
    "clients": "/clients", "diagnostic": "/diagnose/stages", "webinar": "/webinar/chains", "ads": "/ads",
    "studio": "/studio/lines", "builder": "/builder/presets", "queue": "/queue", "runs": None, "audit": "/audit",
    "kb": "/kb", "evals": "/evals", "golive": "/golive", "console": "/personality", "integrations": "/integrations",
    "channels": None, "mcp": "/mcp/tokens", "crm": "/crm", "minis": "/minis", "drive": "/drive", "skills": "/skills",
    "profile": "/profile", "projects": "/projects", "settings": "/settings",
}
POST_ONLY = {"coach": ("/voice-note/deck", {"transcript": "Thunder Dome rules and reminders before calls."}),
             "coaching": ("/coaching", {"text": "Hi there, quick question?", "kind": "dm"}),
             "onboarding": ("/onboarding", {"name": "RBAC Probe (DEMO)"})}


@pytest.mark.parametrize("role", auth.ROLES)
def test_rbac_matrix_every_tab(client, role):
    demo_login(client, role)
    for tab, path in TAB_PROBE.items():
        if path is None:
            continue
        allowed = auth.has_tab(role, tab)
        if tab in POST_ONLY:
            p, body = POST_ONLY[tab]
            r = client.post(f"/t/demo/api{p}", json=body)
        else:
            r = client.get(f"/t/demo/api{path}")
        if allowed:
            assert r.status_code == 200, (role, tab, r.status_code, r.text[:200])
        else:
            assert r.status_code == 403, (role, tab, r.status_code)
    assert set(client.get("/t/demo/api/me").json()["tabs"]) == auth.ROLE_TABS[role]


def test_every_tab_has_a_probe_or_reason():
    assert set(TAB_PROBE) == set(auth.ALL_TABS)


# ---------------------------------------------------------------- name leak sweep (C2, §8)
def _grep(paths: list[Path], names: list[str]) -> list[str]:
    hits = []
    pats = [re.compile(re.escape(n), re.I) for n in names]
    for root in paths:
        for f in root.rglob("*"):
            if f.is_file() and f.suffix in (".py", ".ts", ".tsx", ".css", ".html", ".json", ".md", ".txt", ".js") and \
                    "node_modules" not in f.parts and "dist" not in f.parts and "__pycache__" not in f.parts:
                text = f.read_text(errors="ignore")
                for p in pats:
                    if p.search(text):
                        hits.append(f"{f.relative_to(ROOT)}: {p.pattern}")
    return hits


def test_name_leak_sweep(client):
    """A sample banned list: the fictional byline name in the fixture SOP and a sample name.
    Source must not contain them; generated output must scrub them."""
    sample = ["Dana Example", "Pat Sample"]
    assert _grep([ROOT / "web" / "src", ROOT / "operator_ai"], sample) == []
    demo_login(client, "owner")
    client.put("/t/demo/api/settings", json={"key": "banned_names", "value": sample})
    outs = []
    outs.append(client.get("/t/demo/api/source", params={"file": "sops/thunderdome.txt", "lines": "1-5"}).text)
    outs.append(client.post("/t/demo/api/coach/ask", json={"question": "What are the rules for a Thunder Dome test?"}).text)
    outs.append(client.get("/t/demo/api/search", params={"q": "Thunder Dome presented by"}).text)
    outs.append(client.post("/t/demo/api/studio/run", json={"line": "ads", "brief": {"business": "Pat Sample Physio"}}).text)
    outs.append(client.get("/t/demo/api/queue", params={"status": "all"}).text)
    outs.append(client.get("/t/demo/api/runs").text)
    for o in outs:
        for n in sample:
            assert n.lower() not in o.lower(), (n, o[:300])


def test_no_hardcoded_brand_names():
    # no real creator's name or the reference product's name anywhere in the app or UI
    assert _grep([ROOT / "web" / "src", ROOT / "operator_ai"], ["Jeremy"]) == []


def test_brand_is_configuration(client, monkeypatch):
    monkeypatch.setenv("OPERATOR_BRAND", "Northstar Coaching")
    tenancy.create("brandco", "Brand Co")
    assert client.get("/t/brandco/api/public").json()["brand"]["name"] == "Northstar Coaching"


# ---------------------------------------------------------------- audit coverage (C12)
def test_audit_coverage_all_kinds(client):
    from operator_ai import actions, audit, queue, settings, vault
    from operator_ai.agents import ads_ops
    from tests.conftest import login

    login(client)
    t = tenancy.get("owner")
    client.post("/t/owner/api/auth/login", json={"username": "owner", "password": "wrong"})
    a = queue.add(t, "dm", "guaranteed results", channel="instagram_dm")
    queue.edit(t, a["id"], "Quick question about your clinic?", "o")
    queue.approve(t, a["id"], "o")
    b = queue.add(t, "note", "x")
    queue.reject(t, b["id"], "o")
    client.put("/t/owner/api/settings", json={"key": "banned_names", "value": []})
    settings.set_box(t, "live_calendar_enabled", True, "recorded owner yes")
    vault.put(t, {"meta_app_id": "1"})
    r = ads_ops.build_test(t, "Audit test", ["Hook"])
    actions.rollback(t, r["action_id"], "o")
    tenancy.create("auditco", "Audit Co")
    tenancy.set_kb_scope("auditco", "own")
    kinds = {x["kind"] for x in audit.recent(t, 500)}
    assert {"login", "login.fail", "queue.add", "guardrail.block", "queue.edit", "queue.approve", "queue.reject", "settings",
            "switch", "vault.save", "action", "action.rollback", "tenant.create"} <= kinds
    assert any(x["kind"] == "kb.scope" for x in audit.recent(tenancy.get("auditco"), 50))


# ---------------------------------------------------------------- every agent with AI off (D4)
def test_every_agent_runs_with_ai_off(demo_tenant):
    from operator_ai import call_review, departments, diagnostic, skills, studio, webinar_chain
    from operator_ai.agents import (accountability, ads_ops, coach, coaching, fulfilment, onboarding, reviewer,
                                    sales)

    t = demo_tenant
    assert coach.answer(t, "What goes in a pre-call brief?", record=False)["label"].startswith("Extractive")
    assert reviewer.review(t, "Book now! Click here!")["label"].startswith("Heuristic scoring")
    assert coaching.coach_work(t, "Hey? Hi? Price?", "dm")["mode"] == "rules"
    assert diagnostic.diagnose(t, {"dms_sent": 500, "replies": 20})["mode"] == "rules"
    with t.db() as c:
        lid = c.execute("SELECT id FROM leads LIMIT 1").fetchone()[0]
        cid = c.execute("SELECT id FROM clients LIMIT 1").fetchone()[0]
    assert sales.process_lead(t, lid)["queue_ids"]
    assert onboarding.onboard(t, {"name": "AI Off (DEMO)"})["queue_ids"]
    assert accountability.run_interventions(t)["digest"]
    assert fulfilment.fulfil(t, cid)["queue_ids"]
    assert webinar_chain.run_chain(t, {})["queue_ids"]
    assert studio.run_line(t, "outreach", {})["queue_ids"]
    assert ads_ops.diagnose(t)["writes"] == 0
    gen = departments.generate(departments.PRESETS["physio"])
    bid = departments.save_build(t, "physio", gen["business"], gen)
    r = departments.run_agent(t, bid, "dm_setter", background=False)
    assert all(c["source"] == "Rules" and c["label"].startswith("Rules draft (AI not used)") for c in r["cards"])
    assert skills.run_skill(t, "usp_builder")["status"] == "drafted"
    cid2 = call_review.create_call(t, "c", "rep", ["rep"], "Rep: what's your goal? Prospect: more clients.")
    call_review.record_consent(t, cid2, "rep", "verbal", "o")
    assert call_review.review(t, cid2)["stages"]
    from operator_ai import runtime

    modes = {r["ai_mode"] for r in runtime.recent(t, 200)}
    assert modes == {"rules"}


# ---------------------------------------------------------------- zero invented citations (E3)
def test_zero_invented_citations_on_gold_set(tenant):
    from operator_ai.agents import coach

    idx = index.for_tenant(tenant)
    for g in evals.load("gold_questions.json"):
        r = coach.answer(tenant, g["question"], record=False)
        ids = {c["id"] for c in r["citations"]}
        for sid in re.findall(r"\[(S\d+)\]", r["answer"]):
            assert sid in ids, (g["id"], sid)
        for c in r["citations"]:
            doc = idx.docs[c["file"]]
            a, _, b = c["lines"].partition("-")
            assert 1 <= int(a) <= int(b) <= len(doc.lines)
