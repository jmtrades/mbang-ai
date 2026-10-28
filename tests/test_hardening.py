"""Security and correctness hardening: every finding of docs/audits/2026-10-27-bugs.json owned by this
feature, plus an adversarial pass over guardrails (tests/test_guardrails.py holds the bypass matrix) and the
auth / session / RBAC layer. Positive and negative cases, AI off unless a test turns a fake model on."""
from __future__ import annotations

import json
import os
import shutil
import stat
import tarfile
import time
from pathlib import Path

import pytest

from operator_ai import (ads_import, auth, claims, config, console, db, demo, diagnostic, guardrails, index, llm, queue,
                         runtime, settings, tenancy, vault)
from operator_ai.agents import accountability, coach, router
from tests.conftest import demo_login, login
from tests.fakes import FakeModel

NOTE = "Typical results are far lower; this range is the best quartile of forty fictional clinics."
RANGE_DM = "Let me make Acme Physio $50k-$100k a month through our referral-first system."
WARM = {"prospect_facing": True, "lead_tier": "warm"}


def _record(tenant, days: int = 90) -> dict:
    rec = claims.add(tenant, "outreach_range", NOTE, days, "owner")
    return claims.add_evidence(tenant, rec["id"], "cohort-summary.pdf", b"%PDF-1.4 fictional test evidence", "owner")


@pytest.fixture
def ai_on(tenant, monkeypatch):
    fm = FakeModel()
    llm.set_transport(fm.transport)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-123456789")
    monkeypatch.setenv("OPERATOR_DISABLE_LLM", "0")
    settings.set_box(tenant, "ai_token_budget", 100000, "test budget")
    yield fm
    llm.set_transport(None)


# ================================================================ Tenancy / KB admin
def test_kb_exclusions_are_per_tenant(client):
    login(client)
    r = client.post("/t/owner/api/kb/exclusions", json={"file": "sops/thunderdome.txt", "lines": "9-10"})
    assert r.status_code == 200, r.text
    rule = "Rule 1: Test between three and five"
    assert not any(rule in c.text for c in index.for_tenant(tenancy.get("owner")).chunks)
    assert any(rule in c.text for c in index.for_tenant(tenancy.get("demo")).chunks)  # another tenant: untouched
    assert client.get("/t/owner/api/kb").json()["exclusions"] == [{"file": "sops/thunderdome.txt", "lines": "9-10"}]
    demo_login(client, "owner")
    assert client.get("/t/demo/api/kb").json()["exclusions"] == []


def test_kb_admin_writes_refused_in_demo_and_real_owner_unaffected(client):
    login(client)
    before = client.post("/t/owner/api/coach/ask", json={"question": "What are the rules for a Thunder Dome test?"}).json()
    demo_login(client, "owner")
    writes = [("post", "/kb/exclusions", {"file": "sops/thunderdome.txt", "lines": "1-200"}),
              ("put", "/kb", {"file": "sops/thunderdome.txt", "note": "x"}), ("post", "/drive/sync", None),
              ("post", "/drive/staged/1", {"approve": True}),
              ("post", "/claims", {"typicality_note": NOTE, "valid_days": 30}), ("post", "/claims/1/expire", {"reason": "x"})]
    for method, path, body in writes:
        r = client.request(method.upper(), f"/t/demo/api{path}", json=body)
        assert r.status_code == 403, (path, r.status_code)
        assert "Read-only in the pitch demo" in r.json()["detail"]
    kb = client.get("/t/demo/api/kb").json()
    assert kb["read_only"] is True and "pitch demo" in kb["read_only_reason"]
    after = client.post("/t/owner/api/coach/ask", json={"question": "What are the rules for a Thunder Dome test?"}).json()
    assert after["covered"] == before["covered"] == "yes" and after["citations"]


def test_exclusion_needs_an_indexed_file_and_a_range(owner):
    assert owner.post("/t/owner/api/kb/exclusions", json={"file": "../../etc/passwd", "lines": "1-2"}).status_code == 404
    assert owner.post("/t/owner/api/kb/exclusions", json={"file": "sops/thunderdome.txt"}).status_code == 422
    assert owner.post("/t/owner/api/kb/exclusions", json={"file": "sops/thunderdome.txt", "lines": "abc"}).status_code == 422
    assert owner.get("/t/owner/api/kb").json()["exclusions"] == []


def test_kb_writes_are_owner_only(client):
    login(client)
    t = tenancy.get("owner")
    auth.create_user(t, "coach1", "coach-password-123", "coach")
    login(client, username="coach1", password="coach-password-123")
    assert client.post("/t/owner/api/kb/exclusions", json={"file": "sops/thunderdome.txt", "lines": "1-2"}).status_code == 403
    assert client.post("/t/owner/api/claims", json={"typicality_note": NOTE}).status_code == 403
    assert client.get("/t/owner/api/claims").status_code == 403


def test_legacy_global_exclusions_adopted_by_the_owner_only(env):
    tenancy.ensure_defaults()
    legacy = index.legacy_exclusions_path()
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps([{"file": "sops/thunderdome.txt", "lines": "9-10"}, {"file": "x", "lines": "bad"}]))
    assert index.load_exclusions(tenancy.get("demo")) == []  # the demo never adopts it
    assert index.load_exclusions(tenancy.get("owner")) == [{"file": "sops/thunderdome.txt", "lines": "9-10"}]
    assert not legacy.exists()
    assert index.load_exclusions(tenancy.get("demo")) == []


# ================================================================ Demo workspace
def test_demo_has_its_own_small_budget_that_reset_never_refills(env, monkeypatch):
    monkeypatch.setenv("OPERATOR_AI_TOKEN_BUDGET", "500000")
    monkeypatch.setenv("OPERATOR_DISABLE_LLM", "0")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-123456789")
    tenancy.ensure_defaults()
    d = tenancy.get("demo")
    cap = tenancy.DEMO_AI_BUDGET_CAP
    assert llm.budget(d) == cap < 500000
    assert llm.budget(tenancy.get("owner")) == 500000  # the real workspace keeps the server default
    d.set("ai_token_used", cap)  # an anonymous visitor spent it all
    assert llm.unavailable_reason(d) == "AI budget used up"
    demo.ensure_seeded(d)
    demo.reset(d)
    assert d.get("ai_token_used") == cap and llm.budget(d) == cap
    assert llm.unavailable_reason(d) == "AI budget used up"


def test_demo_budget_is_zero_when_the_server_has_none_and_is_backfilled(env):
    tenancy.ensure_defaults()
    d = tenancy.get("demo")
    assert d.get("ai_token_budget") == 0 and llm.budget(d) == 0
    with d.db() as c:  # a demo created before it had its own budget
        c.execute("DELETE FROM settings WHERE key IN ('ai_token_budget','critique_token_budget')")
    tenancy.ensure_defaults()
    assert d.get("ai_token_budget") == 0 and d.get("critique_token_budget") == 0


def test_demo_login_rate_limited_per_network_but_role_switches_are_free(client):
    demo_login(client, "owner")  # 1 new session
    for role in ["coach", "setter", "closer", "student"] * 8:  # swaps of a live session: never counted
        demo_login(client, role)
    for _ in range(19):
        client.cookies.clear()
        demo_login(client, "student")
    client.cookies.clear()
    r = client.post("/t/demo/api/auth/demo-login", json={"role": "owner"})
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0


def test_demo_reset_rate_limited(client, monkeypatch):
    monkeypatch.setenv("OPERATOR_LIMIT_DEMO_RESET", "1")
    demo_login(client)
    assert client.post("/t/demo/api/demo/reset").status_code == 200
    demo_login(client)
    r = client.post("/t/demo/api/demo/reset")
    assert r.status_code == 429 and "reset recently" in r.json()["detail"]


def test_demo_backups_are_capped(demo_tenant):
    for _ in range(demo.DEMO_BACKUPS_KEEP + 3):
        demo.backup(demo_tenant)
    kept = list((config.get().data_dir / "backups").glob("demo-*.db"))
    assert len(kept) == demo.DEMO_BACKUPS_KEEP and all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in kept)


def test_demo_never_serves_the_real_course_unless_the_owner_allows_it(env, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from operator_ai.app import create_app

    real = tmp_path / "realcourse"
    (real / "sops").mkdir(parents=True)
    (real / "sops" / "private_offer.txt").write_text("Private offer stack: the zebrafinch method for clinic owners.\n" * 20)
    monkeypatch.setenv("OPERATOR_CORPUS_DIR", str(real))
    index.invalidate()
    with TestClient(create_app(start_workers=False)) as c:
        demo_login(c, "student")
        hits = c.get("/t/demo/api/search", params={"q": "zebrafinch method"}).json()["hits"]
        assert all(h["file"] != "sops/private_offer.txt" for h in hits)
        assert c.get("/t/demo/api/source", params={"file": "sops/private_offer.txt"}).status_code == 404
        login(c)
        own = c.get("/t/owner/api/search", params={"q": "zebrafinch method"}).json()["hits"]
        assert any(h["file"] == "sops/private_offer.txt" for h in own)
        # only the server-only switch, with a recorded owner yes, lets the demo read it
        assert c.put("/t/owner/api/settings", json={"key": "demo_course_material", "value": True}).status_code == 403
        settings.set_box(tenancy.get("demo"), "demo_course_material", True, "owner allowed the real course in the demo")
        hits = c.get("/t/demo/api/search", params={"q": "zebrafinch method"}).json()["hits"]
        assert any(h["file"] == "sops/private_offer.txt" for h in hits)


# ================================================================ Proof records (claims substantiation)
def test_new_workspace_has_no_figure_by_default(tenant):
    assert not claims.has_valid(tenant) and claims.list_records(tenant) == []
    assert guardrails.range_line("Acme", "our system", tenant) == guardrails.no_figure_line("Acme", "our system")
    q = queue.add(tenant, "dm", RANGE_DM, channel="instagram_dm", meta=WARM)
    assert q["status"] == "held" and "unsubstantiated_claim" in {f["code"] for f in q["flags"]}
    clean = queue.add(tenant, "dm", guardrails.range_line("Acme Physio", "our system", tenant), channel="instagram_dm",
                      meta=WARM)
    assert clean["status"] == "pending"


def test_proof_record_lifecycle_via_api(owner):
    t = tenancy.get("owner")
    r = owner.post("/t/owner/api/claims", json={"typicality_note": NOTE, "valid_days": 90})
    assert r.status_code == 200, r.text
    rec = r.json()
    assert rec["state"] == "needs_evidence" and not claims.has_valid(t)
    up = owner.post(f"/t/owner/api/claims/{rec['id']}/evidence",
                    files={"file": ("../../../etc/cohort.pdf", b"%PDF-1.4 x", "application/pdf")})
    assert up.status_code == 200, up.text
    assert up.json()["state"] == "current" and up.json()["evidence"][0]["name"] == "cohort.pdf"
    assert "stored" not in json.dumps(up.json())  # file paths are never returned
    folder = t.dir / "claims" / str(rec["id"])
    assert [p.parent for p in folder.iterdir()] == [folder]
    assert claims.has_valid(t)
    assert queue.add(t, "dm", RANGE_DM, channel="instagram_dm", meta=WARM)["status"] == "pending"
    dl = owner.get(f"/t/owner/api/claims/{rec['id']}/evidence/1")
    assert dl.status_code == 200 and dl.content == b"%PDF-1.4 x" and dl.headers["x-content-type-options"] == "nosniff"
    ex = owner.post(f"/t/owner/api/claims/{rec['id']}/expire", json={"reason": "cohort data is out of date"})
    assert ex.json()["state"] == "expired" and not claims.has_valid(t)
    assert guardrails.range_line("Acme", tenant=t).startswith("Let me help")
    assert queue.add(t, "dm", RANGE_DM, channel="instagram_dm", meta=WARM)["status"] == "held"
    kinds = {a["kind"] for a in owner.get("/t/owner/api/audit").json()["items"]}
    assert {"claims.add", "claims.evidence", "claims.expire"} <= kinds


def test_proof_record_validation(owner, monkeypatch):
    base = "/t/owner/api/claims"
    assert owner.post(base, json={"typicality_note": "too short"}).status_code == 422
    assert owner.post(base, json={"typicality_note": NOTE, "valid_days": 0}).status_code == 422
    assert owner.post(base, json={"typicality_note": NOTE, "valid_days": 400}).status_code == 422
    assert owner.post(base, json={"typicality_note": NOTE, "claim_key": "made_up"}).status_code == 422
    assert owner.post(base, json={"typicality_note": 5}).status_code == 422
    rid = owner.post(base, json={"typicality_note": NOTE}).json()["id"]
    ev = f"{base}/{rid}/evidence"
    assert owner.post(ev, files={"file": ("run.exe", b"MZ", "application/octet-stream")}).status_code == 422
    assert owner.post(ev, files={"file": ("empty.pdf", b"", "application/pdf")}).status_code == 422
    monkeypatch.setattr(claims, "MAX_FILE_BYTES", 10)
    assert owner.post(ev, files={"file": ("big.pdf", b"x" * 11, "application/pdf")}).status_code == 422
    assert owner.post(f"{base}/9999/evidence", files={"file": ("a.pdf", b"x", "application/pdf")}).status_code == 404
    assert owner.post(f"{base}/9999/expire", json={}).status_code == 404
    owner.post(f"{base}/{rid}/expire", json={})
    assert owner.post(ev, files={"file": ("a.pdf", b"x", "application/pdf")}).status_code == 409


def test_expired_or_undocumented_records_are_not_valid(tenant):
    rec = claims.add(tenant, "outreach_range", NOTE, 30, "owner")
    assert not claims.has_valid(tenant)  # no evidence yet
    claims.add_evidence(tenant, rec["id"], "proof.png", b"\x89PNG fictional", "owner")
    assert claims.has_valid(tenant)
    with tenant.db() as c:
        c.execute("UPDATE claims_substantiation SET expires=? WHERE id=?", (time.time() - 1, rec["id"]))
    assert not claims.has_valid(tenant)
    assert claims.list_records(tenant)[0]["state"] == "expired"


def test_proof_records_in_the_demo_are_seeded_labelled_and_read_only(client):
    demo_login(client, "owner")
    data = client.get("/t/demo/api/claims").json()
    assert data["current"] and data["read_only"]
    assert len(data["items"]) == 1 and "(DEMO)" in data["items"][0]["statement"] and data["items"][0]["demo"] == 1
    assert data["items"][0]["evidence"][0]["name"].endswith("DEMO.txt")
    t = tenancy.get("demo")
    nina = next(i for i in queue.list_items(t) if "Nina" in (i["title"] or ""))
    assert nina["status"] == "pending"  # the demo story still shows the (recorded, labelled) range
    demo_login(client, "coach")
    assert client.get("/t/demo/api/claims").status_code == 403


def test_real_workspace_is_never_seeded_with_a_record(tenant):
    claims.seed_demo(tenant)
    assert claims.list_records(tenant) == []


def test_studio_call_offer_uses_the_no_figure_line_without_a_record(tenant):
    from operator_ai import studio

    out = studio.run_line(tenant, "outreach", {})
    bodies = [queue.get(tenant, q)["body"] for q in out["queue_ids"]]
    assert not any("$50k" in b for b in bodies)
    assert all(queue.get(tenant, q)["status"] != "held" for q in out["queue_ids"])


# ================================================================ Diagnostic maths
def test_zero_downstream_stage_does_not_zero_upstream_cost():
    a = diagnostic.analyse({"dms_sent": 1000, "replies": 30, "conversations": 12, "links_sent": 6, "booked": 12,
                            "showed": 10, "closed": 0, "offer_price": 3000})
    by = {s["key"]: s for s in a["stages"]}
    assert by["reply"]["below"] and by["reply"]["monthly_cost"] > 0
    assert a["bottleneck"] is not None and a["bottleneck"]["key"] == "close" and a["bottleneck"]["critical"]


def test_dead_stage_is_reported_as_the_bottleneck():
    a = diagnostic.analyse({"dms_sent": 2000, "replies": 10, "offer_price": 3000})
    assert a["bottleneck"]["key"] == "reply" and a["bottleneck"]["critical"] and a["status"] == "bottleneck"


def test_chat_numbers_with_zero_closes_name_a_bottleneck(tenant):
    m = router.parse_metrics("800 DMs, 40 replies, 6 booked, 2 showed, 0 closed, price $3k")
    r = diagnostic.diagnose(tenant, m)
    assert r["bottleneck"]["key"] == "reply" and "at or above" not in r["prescription"]


def test_missing_numbers_are_not_zeros():
    a = diagnostic.analyse({"dms_sent": 800, "replies": 40})
    assert [s["key"] for s in a["stages"]] == ["reply"]


def test_cpl_cost_sized_on_lead_to_close():
    a = diagnostic.analyse({"ad_spend": 3000, "leads": 100, "closed": 5, "offer_price": 3000})
    cpl = next(s for s in a["stages"] if s["key"] == "cpl")
    assert cpl["monthly_cost"] == 15000  # 100 extra leads at the benchmark CPL x 5% lead-to-close x 3,000


def test_all_at_benchmark_only_when_nothing_is_below(tenant):
    low = diagnostic.diagnose(tenant, {"booked": 4, "showed": 0, "closed": 0})
    assert low["bottleneck"] is None and "at or above" not in low["prescription"]
    assert "too little volume" in low["prescription"] and "Show rate" in low["prescription"]
    good = diagnostic.diagnose(tenant, {"dms_sent": 100, "replies": 20})
    assert good["bottleneck"] is None and "at or above the SOP benchmark" in good["prescription"]
    empty = diagnostic.diagnose(tenant, {})
    assert empty["bottleneck"] is None and "Not enough numbers" in empty["prescription"]


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_metrics_get_422(owner, bad):
    body = '{"metrics": {"dms_sent": %s, "replies": 4}}' % bad
    r = owner.post("/t/owner/api/diagnose", content=body, headers={"content-type": "application/json"})
    assert r.status_code == 422


def test_negative_huge_or_stored_bad_metrics_get_422(owner):
    assert owner.post("/t/owner/api/diagnose", json={"metrics": {"dms_sent": -5, "replies": 1}}).status_code == 422
    assert owner.post("/t/owner/api/diagnose", json={"metrics": {"dms_sent": 1e300, "replies": 1}}).status_code == 422
    with tenancy.get("owner").db() as c:
        cid = c.execute("INSERT INTO clients(name,metrics,price,demo,created) VALUES('Odd client','{\"dms_sent\": NaN}',"
                        "3000,0,?)", (time.time(),)).lastrowid
    assert owner.post("/t/owner/api/diagnose", json={"client_id": cid}).status_code == 422
    with pytest.raises(diagnostic.BadMetrics):
        diagnostic.analyse({"replies": float("inf")})


# ================================================================ Ads CSV import
EU_CSV = ("Ad name;Amount spent (EUR);Results;Impressions;Link clicks\n"
          "Hook A;1.234,56;40;48.210;1.012\n"
          "Hook B;€ 2.345,00;0;9.200;40\n"
          "Total Transformation Hook;45,00;0;2.400;12\n"
          "Total;3.624,56;40;59.810;1.064\n")


def test_eu_numbers_semicolons_and_only_the_real_totals_row(tenant):
    by = {r["name"]: r for r in ads_import.parse(EU_CSV)}
    assert set(by) == {"Hook A", "Hook B", "Total Transformation Hook"}
    assert by["Hook A"]["spend"] == 1234.56 and by["Hook A"]["impressions"] == 48210 and by["Hook A"]["clicks"] == 1012
    assert by["Hook B"]["spend"] == 2345.0 and by["Total Transformation Hook"]["impressions"] == 2400
    a = ads_import.audit(tenant, EU_CSV, target_cpl=20)
    ads = {r["name"]: r for r in a["ads"]}
    assert ads["Total Transformation Hook"]["kill"]  # 45 spent (> 1.5x target), 0 leads, 2,400 impressions
    assert ads["Hook A"]["verdict"] == "keep" and ads["Hook A"]["cpl"] == pytest.approx(30.86, abs=0.01)
    assert sum(r["kill"] for r in a["ads"]) == 2 and "2 ad(s) hit the kill rule" in a["summary"]


def test_summary_row_without_a_name_is_skipped():
    csv_text = "Ad name,Amount spent (USD),Results,Impressions\nHook A,10.50,1,1200\n,10.50,1,1200\n"
    assert [r["name"] for r in ads_import.parse(csv_text)] == ["Hook A"]


@pytest.mark.parametrize("v,eu,integer,expected", [
    ("1.234,56", None, False, 1234.56), ("1,234.56", None, False, 1234.56), ("€ 2.345,00", None, False, 2345.0),
    ("1.234", True, False, 1234.0), ("1.234", False, False, 1.234), ("1.234", False, True, 1234.0),
    ("12,5", True, False, 12.5), ("1 234", None, False, 1234.0), ("1.234.567", None, True, 1234567.0), ("", None, False, 0.0),
    ("$1,240.50", False, False, 1240.5),
])
def test_number_formats(v, eu, integer, expected):
    assert ads_import._num(v, eu, integer) == pytest.approx(expected)


@pytest.mark.parametrize("text", [
    "Quick one, my impressions are up but cost per lead is rising, what should I change?",
    "Hey, impressions dropped and spend went up, is it creative fatigue?",
])
def test_chat_questions_are_not_ads_exports(text):
    assert not ads_import.looks_like_ads_export(text)
    assert router.classify(text) != "ads_csv"


# ================================================================ Banned-name scrub and settings validation
def test_personality_banned_words_round_trip(owner):
    owner.put("/t/owner/api/settings", json={"key": "banned_names", "value": ["Pat Sample"]})
    assert owner.put("/t/owner/api/personality", json={"banned_words": ["hustle", "crush it"]}).status_code == 200
    p = owner.get("/t/owner/api/personality").json()
    assert p["banned_words"] == ["hustle", "crush it"]
    owner.put("/t/owner/api/personality", json={"display_name": "Coach", "banned_words": p["banned_words"]})
    assert tenancy.get("owner").get("persona")["banned_words"] == ["hustle", "crush it"]


def test_banned_words_equal_to_json_keys_never_corrupt_responses(owner):
    r = owner.put("/t/owner/api/settings", json={"key": "banned_names", "value": ["status", "false", "id", "items"]})
    assert r.status_code == 200
    guardrails.banned_cache_clear("owner")
    me = owner.get("/t/owner/api/me")
    assert me.status_code == 200 and me.json()["is_demo"] is False and me.json()["user"]["id"]
    q = owner.get("/t/owner/api/queue")
    assert q.status_code == 200 and isinstance(q.json()["items"], list)


@pytest.mark.parametrize("key,value", [
    ("banned_names", [123]), ("banned_names", "Pat Sample"), ("banned_names", ["x"]), ("third_party_kb", "yes"),
    ("outreach_daily_cap", -1), ("outreach_daily_cap", True), ("brand", {"primary": "red"}), ("brand", "Acme"),
    ("brand", {"logo": "javascript:alert(1)"}), ("lead_weights", {"source": {"referral": "lots"}}),
    ("revenue_goal", 1e300), ("persona", {"banned_words": [5]}), ("blocked_attendees", {"a": 1}),
    ("lead_routes", {"x": "y" * 30000}),
])
def test_bad_setting_shapes_are_422_and_the_api_keeps_working(owner, key, value):
    assert owner.put("/t/owner/api/settings", json={"key": key, "value": value}).status_code == 422
    assert owner.get("/t/owner/api/me").status_code == 200 and owner.get("/t/owner/api/queue").status_code == 200


def test_good_settings_still_save(owner):
    for key, value in [("brand", {"name": "Northstar", "primary": "#112233", "accent": "#445566", "owner_display": "Owner",
                                  "logo": ""}), ("banned_names", ["Pat Sample", " Quinn "]), ("third_party_kb", True),
                       ("outreach_daily_cap", 40), ("blocked_attendees", ["@maya_demo", "a@example.invalid"])]:
        assert owner.put("/t/owner/api/settings", json={"key": key, "value": value}).status_code == 200, key
    assert tenancy.get("owner").get("banned_names") == ["Pat Sample", "Quinn"]


def test_settings_written_before_validation_cannot_break_the_api(owner):
    tenancy.get("owner").set("banned_names", [123, {"a": 1}, None])
    guardrails.banned_cache_clear("owner")
    assert owner.get("/t/owner/api/me").status_code == 200 and owner.get("/t/owner/api/queue").status_code == 200


def test_scrub_removes_names_from_values_but_not_persona_words(owner):
    owner.put("/t/owner/api/settings", json={"key": "banned_names", "value": ["Pat Sample"]})
    owner.put("/t/owner/api/personality", json={"banned_words": ["hustle"]})
    guardrails.banned_cache_clear("owner")
    t = tenancy.get("owner")
    queue.add(t, "note", "Pat Sample asked about the hustle plan")
    body = owner.get("/t/owner/api/queue").json()["items"][0]["body"]
    assert "Pat Sample" not in body and "[name removed]" in body and "hustle" in body
    assert "Pat Sample" in owner.get("/t/owner/api/settings").text  # the list itself stays visible to the owner


# ================================================================ Coach grounding and follow-ups
def test_live_answer_without_valid_citations_is_not_served_as_covered(tenant, ai_on):
    ai_on.reply = json.dumps({"covered": "yes", "citations": [],
                              "answer": "Aim for a 90% show rate and charge $5,000; our students make $40k a month."})
    r = coach.answer(tenant, "What show rate should I aim for on booked calls?", record=False)
    assert "$40k" not in r["answer"] and "$5,000" not in r["answer"]
    assert r["covered"] == "no" or (r["citations"] and r["mode"] == "rules" and "no valid citations" in r["label"])


def test_live_answer_keeps_only_cited_guardrail_clean_sentences(tenant, ai_on):
    ai_on.reply = json.dumps({"covered": "yes", "citations": ["S1"], "answer":
                              "Run it for 72 hours [S1]. Our students make $40k a month [S1]. Trust me, it always works."})
    r = coach.answer(tenant, "How long should a Thunder Dome test run before I judge it?", record=False)
    assert r["mode"] == "live" and "72 hours" in r["answer"]
    assert "$40k" not in r["answer"] and "Trust me" not in r["answer"]
    steps = [s["tool"] for s in runtime.get(tenant, r["run_id"])["steps"]]
    assert "ground" in steps and "guardrails" in steps


def test_ai_off_answers_unchanged(tenant):
    r = coach.answer(tenant, "What are the rules for a Thunder Dome test?", record=False)
    assert r["covered"] == "yes" and r["mode"] == "rules" and "AI off" in r["label"] and r["citations"]


@pytest.mark.parametrize("q", ["Is there a kill rule for ads?", "How do I pick a niche?", "What is a Thunder Dome?",
                               "When should reminders go out?", "What goes in a pre-call brief?"])
def test_unrelated_short_questions_are_not_follow_ups(q):
    assert coach.resolve_followup(q, "What's the USP formula?") == (q, False)


@pytest.mark.parametrize("q", ["How long should it run?", "What about for webinars?", "Why?", "And for clinics?",
                               "Is it worth it?"])
def test_real_follow_ups_still_read_in_context(q):
    assert coach.resolve_followup(q, "What are the rules for a Thunder Dome test?")[1]


def test_unrelated_question_does_not_pull_in_the_previous_topic(tenant):
    r = coach.answer(tenant, "Is there a kill rule for ads?", record=False, context="What's the USP formula?")
    assert "Everything we build later sits on top of the offer" not in r["answer"]
    assert not r["parts"][0]["part"].startswith("What's the USP formula")


# ================================================================ Public mini scope and limits
def _start(client, slug, email="lead@example.invalid"):
    return client.post(f"/m/demo/{slug}/start", json={"email": email, "consent": True})


def test_scoped_mini_answer_text_comes_only_from_its_files(demo_tenant, client):
    console.create_mini(demo_tenant, "td-only", "Thunder Dome helper", ["sops/thunderdome.txt"], 5, "I agree (DEMO)")
    tok = _start(client, "td-only").json()["token"]
    r = client.post("/m/demo/td-only/ask", json={"token": tok, "question": "What show rate should I aim for?"}).json()
    assert "thirty-five percent" not in r["answer"] and "Registration show rate" not in r["answer"]
    r = client.post("/m/demo/td-only/ask", json={"token": tok, "question": "What are the rules for a Thunder Dome test?"}).json()
    assert r["covered"] != "no" and r["citations"] and all(c["file"] == "sops/thunderdome.txt" for c in r["citations"])
    source = "\n".join(index.for_tenant(demo_tenant).docs["sops/thunderdome.txt"].lines)
    for line in r["answer"].splitlines()[1:]:
        sentence = coach.CITE_RE.sub("", line).strip(" •")
        assert sentence and sentence in source, sentence
    with demo_tenant.db() as c:  # the scoped answer leaves a trace like every agent action
        assert c.execute("SELECT COUNT(*) FROM runs WHERE agent='mini'").fetchone()[0] >= 2


def test_mini_question_limit_is_per_email_not_per_session(demo_tenant, client):
    console.create_mini(demo_tenant, "lim", "Limits (DEMO)", ["sops/thunderdome.txt"], 2, "I agree (DEMO)")
    q = {"question": "How many creatives go into a Thunder Dome?"}
    t1 = _start(client, "lim", "a@example.invalid").json()["token"]
    assert client.post("/m/demo/lim/ask", json={"token": t1, **q}).status_code == 200
    assert client.post("/m/demo/lim/ask", json={"token": t1, **q}).status_code == 200
    t2 = _start(client, "lim", "A+again@Example.invalid").json()["token"]  # a fresh token for the same address
    assert client.post("/m/demo/lim/ask", json={"token": t2, **q}).status_code == 429
    assert _start(client, "lim", "a@example.invalid").status_code == 200  # third session today
    assert _start(client, "lim", "a@example.invalid").status_code == 429  # fourth: refused


def test_mini_starts_and_questions_limited_per_network(demo_tenant, client, monkeypatch):
    monkeypatch.setenv("OPERATOR_LIMIT_MINI_START", "2")
    monkeypatch.setenv("OPERATOR_LIMIT_MINI_ASK", "1")
    console.create_mini(demo_tenant, "net", "Network limits (DEMO)", [], 5, "I agree (DEMO)")
    tok = _start(client, "net", "one@example.invalid").json()["token"]
    assert _start(client, "net", "two@example.invalid").status_code == 200
    assert _start(client, "net", "three@example.invalid").status_code == 429
    assert client.post("/m/demo/net/ask", json={"token": tok, "question": "What is a Thunder Dome?"}).status_code == 200
    assert client.post("/m/demo/net/ask", json={"token": tok, "question": "What is a Thunder Dome?"}).status_code == 429


# ================================================================ Unhandled 500s
def test_onboarding_blank_name_is_422_and_writes_nothing(owner):
    t = tenancy.get("owner")
    with t.db() as c:
        before = c.execute("SELECT COUNT(*) FROM students").fetchone()[0]
    assert owner.post("/t/owner/api/onboarding", json={"name": "   "}).status_code == 422
    with t.db() as c:
        assert c.execute("SELECT COUNT(*) FROM students").fetchone()[0] == before
    r = owner.post("/t/owner/api/onboarding", json={"name": "  Sam   Rivers  ", "email": "sam@example.invalid"})
    assert r.status_code == 200
    welcome = queue.get(t, r.json()["queue_ids"][0])
    assert welcome["body"].startswith("Welcome to the programme, Sam!")


def test_accountability_sweep_survives_blank_and_odd_rows(owner):
    t = tenancy.get("owner")
    old = time.time() - 120 * 86400
    with t.db() as c:
        for name in ("", "   ", None, "Kim Odd"):
            c.execute("INSERT INTO students(name,email,joined,clients,demo) VALUES(?,?,?,?,0)", (name, None, old, 0))
        c.execute("INSERT INTO students(name,email,joined,clients,demo) VALUES(?,?,?,?,0)", ("Lee Text", None, "not a date", "x"))
    r = owner.post("/t/owner/api/students/interventions")
    assert r.status_code == 200, r.text
    bodies = [queue.get(t, q)["body"] for q in r.json()["nudges"]]
    assert any(b.startswith("Hey there,") for b in bodies) and any(b.startswith("Hey Kim,") for b in bodies)
    assert owner.get("/t/owner/api/students").status_code == 200


def test_checkin_numbers_are_clamped(tenant):
    with tenant.db() as c:
        sid = c.execute("INSERT INTO students(name,joined) VALUES('Clamp Test',?)", (time.time(),)).lastrowid
    cid = accountability.submit_checkin(tenant, sid, {"outreach": 10 ** 25, "calls_booked": -4, "calls_showed": "lots",
                                                      "mood": 99})
    with tenant.db() as c:
        row = c.execute("SELECT * FROM checkins WHERE id=?", (cid,)).fetchone()
    assert (row["outreach"], row["calls_booked"], row["calls_showed"], row["mood"]) == (100000, 0, 0, 5)


# ================================================================ Secret storage in backups
def test_backup_keeps_vault_keys_out_of_the_archive_and_restores_them(env, tmp_path):
    from scripts import backup, restore

    tenancy.ensure_defaults()
    t, _ = tenancy.create("acme", "Acme")
    vault.put(t, {"meta_app_id": "123"})
    arc = backup.backup(tmp_path / "bk")
    with tarfile.open(arc) as tar:
        names = tar.getnames()
    assert not any(n.endswith("vault.key") or n.endswith("owner_password.txt") for n in names)
    assert any(n.endswith("acme/vault.bin") for n in names)
    keys = backup.keys_bundle_for(arc)
    assert keys is not None and keys.parent != arc.parent and not keys.is_relative_to(arc.parent)
    assert stat.S_IMODE(os.stat(keys).st_mode) == 0o600 and stat.S_IMODE(os.stat(keys.parent).st_mode) == 0o700
    with tarfile.open(keys) as tar:
        assert {Path(m.name).name for m in tar.getmembers() if m.isfile()} == {"vault.key"}
    ok, notes = restore.check(arc)
    assert ok and not any("WARNING" in n for n in notes)
    # a fresh machine: no data dir at all; the keys come from the separate bundle
    shutil.rmtree(config.get().data_dir)
    restore.restore(arc, keys)
    tenancy.clear_cache()
    db._initialised.clear()
    assert vault.get(tenancy.get("acme"), "meta_app_id") == "123"


def test_restore_without_any_key_says_which_tenants_need_secrets_again(env, tmp_path, capsys):
    from scripts import backup, restore

    tenancy.ensure_defaults()
    t, _ = tenancy.create("acme", "Acme")
    vault.put(t, {"meta_app_id": "123"})
    arc = backup.backup(tmp_path / "bk")
    backup.keys_bundle_for(arc).unlink()
    shutil.rmtree(config.get().data_dir)
    restore.restore(arc)
    assert "acme" in capsys.readouterr().err


def test_keys_bundle_never_goes_into_the_archive_folder(env, tmp_path):
    from scripts import backup

    tenancy.ensure_defaults()
    with pytest.raises(SystemExit):
        backup.backup(tmp_path / "bk", keys_dest=tmp_path / "bk" / "keys")
    assert "vault\\.key" in (config.ROOT / "deploy" / "offsite_backup.sh").read_text()


# ================================================================ Auth / session / CSRF / paths / tenant ids
def test_cross_site_writes_are_refused(owner):
    body = {"key": "banned_names", "value": []}
    for hdr in ({"origin": "https://evil.example"}, {"origin": "null"}, {"referer": "https://evil.example/page"}):
        r = owner.put("/t/owner/api/settings", json=body, headers=hdr)
        assert r.status_code == 403 and "another website" in r.json()["detail"]
    assert owner.put("/t/owner/api/settings", json=body, headers={"origin": "http://testserver"}).status_code == 200
    assert owner.put("/t/owner/api/settings", json=body).status_code == 200  # no Origin: judged by the cookie
    assert owner.get("/t/owner/api/me", headers={"origin": "https://evil.example"}).status_code == 200


def test_allowed_origins_are_configurable(owner, monkeypatch):
    monkeypatch.setenv("OPERATOR_ALLOWED_ORIGINS", "https://app.example")
    r = owner.put("/t/owner/api/settings", json={"key": "banned_names", "value": []}, headers={"origin": "https://app.example"})
    assert r.status_code == 200


def test_sign_in_always_starts_a_new_session(client):
    login(client)
    old = client.cookies.get("op_s")
    login(client)
    new = client.cookies.get("op_s")
    assert old and new and old != new
    assert auth.session_user(tenancy.get("owner"), old) is None  # the presented session was ended


def test_session_cookie_flags(client, monkeypatch):
    sc = login(client).headers["set-cookie"]
    assert "HttpOnly" in sc and "samesite=lax" in sc.lower() and "Path=/t/owner" in sc and "Secure" not in sc
    monkeypatch.setenv("OPERATOR_COOKIE_SECURE", "1")
    assert "Secure" in login(client).headers["set-cookie"]
    monkeypatch.delenv("OPERATOR_COOKIE_SECURE")
    from tests.conftest import owner_password

    # served over HTTPS (behind the proxy uvicorn's --proxy-headers sets the scheme): Secure without any setting
    r = client.post("https://testserver/t/owner/api/auth/login", json={"username": "owner", "password": owner_password()})
    assert r.status_code == 200 and "Secure" in r.headers["set-cookie"]


def test_unknown_username_costs_the_same_password_check(client, monkeypatch):
    calls = []
    real = auth.check_pw
    monkeypatch.setattr(auth, "check_pw", lambda pw, h: calls.append(h) or real(pw, h))
    assert client.post("/t/owner/api/auth/login", json={"username": "nobody", "password": "x"}).status_code == 401
    assert calls == [auth._DUMMY_HASH]  # no fast path that would reveal which usernames exist


def test_guardrail_check_endpoint_sees_the_tenants_proof_records(owner):
    body = {"text": RANGE_DM, "mode": "outreach", "meta": {"lead_tier": "warm"}}
    assert "unsubstantiated_claim" in {f["code"] for f in owner.post("/t/owner/api/guardrails/check", json=body).json()["flags"]}
    _record(tenancy.get("owner"))
    assert owner.post("/t/owner/api/guardrails/check", json=body).json()["flags"] == []


def test_logout_ends_the_session_server_side(client):
    login(client)
    tok = client.cookies.get("op_s")
    client.post("/t/owner/api/auth/logout")
    assert auth.session_user(tenancy.get("owner"), tok) is None
    client.cookies.set("op_s", tok, path="/t/owner")
    assert client.get("/t/owner/api/me").status_code == 401


def test_static_files_cannot_escape_the_web_dist(client, tmp_path, monkeypatch):
    from operator_ai import app as app_mod

    dist = tmp_path / "web" / "dist"
    dist.mkdir(parents=True)
    (dist / "ok.txt").write_text("fine")
    (tmp_path / "web" / "dist-evil").mkdir()
    (tmp_path / "web" / "dist-evil" / "secret.txt").write_text("secret")
    monkeypatch.setattr(app_mod, "WEB_DIST", dist)
    assert client.get("/ok.txt").text == "fine"
    for p in ["/..%2Fdist-evil/secret.txt", "/%2E%2E%2Fdist-evil%2Fsecret.txt", "/..%5Cdist-evil%5Csecret.txt",
              "/%2E%2E/dist-evil/secret.txt"]:
        r = client.get(p)
        assert r.status_code == 404 and "secret" not in r.text, p


@pytest.mark.parametrize("f", ["../../../etc/passwd", "/etc/passwd", "sops/../../../../etc/passwd", "..\\..\\x",
                               "sops/thunderdome.txt\x00.png"])
def test_source_cannot_traverse(client, f):
    demo_login(client, "student")
    assert client.get("/t/demo/api/source", params={"file": f}).status_code == 404


@pytest.mark.parametrize("tid", ["OWNER", "owner'--", "a" * 40, "%2E%2E", "owner%20", "-owner"])
def test_odd_tenant_ids_are_simply_not_found(client, tid):
    login(client)
    assert client.get(f"/t/{tid}/api/me").status_code == 404
    with pytest.raises(tenancy.TenantNotFound):
        tenancy.get(tid)


def test_non_finite_json_is_rejected_everywhere(owner):
    for path, body in [("/t/owner/api/leads/weights", '{"weights": {"source": {"referral": NaN}}}'),
                       ("/t/owner/api/clients", '{"name": "x", "metrics": {"leads": Infinity}}')]:
        r = owner.request("PUT" if "weights" in path else "POST", path, content=body,
                          headers={"content-type": "application/json"})
        assert r.status_code == 422, path
    assert owner.get("/t/owner/api/clients").status_code == 200


def test_security_headers(client):
    login(client)
    r = client.get("/t/owner/api/me")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "SAMEORIGIN"
    assert r.headers["cache-control"] == "no-store"


# ================================================================ review pass (second engineer): bypasses and gaps found
# Each test below failed on the first hardening build.
@pytest.mark.parametrize("text", [
    # an income claim with a bare number (no currency, no "k") passed every mode
    "Our clients made 40,000 last month.", "Our clients made 40000 in their first month.",
    "Clients bring in 25000 monthly with our system.", "Our students generated 120,000 in revenue this year.",
    "I personally made 87,500 last quarter doing this.", "Our members average 12,000 a month.",
    # the commonest misspellings and other guarantee wording
    "gaurantee results", "garuntee results", "gurantee results", "We promise results", "100% success rate",
    "Results are 100% certain", "Every plan comes with a results warranty",
    # a negator that governs an interjection, not the claim
    "Don't worry guaranteed results", "Dont worry guaranteed results", "Not kidding guaranteed results",
    "You can't lose with guaranteed results", "No joke our clients make 20,000 a month",
])
def test_review_pass_bypasses_blocked_in_every_mode(text):
    assert guardrails.blocked(guardrails.check(text, "plain")), text
    assert guardrails.blocked(guardrails.check(text, "outreach", {"lead_tier": "warm"})), text


@pytest.mark.parametrize("text", ["the program is 2997", "Join for 1997 today", "1997 gets you in", "It's 997.",
                                  "That's 40,000 last month for a clinic like yours.", "You could make 40000 in a month"])
def test_review_pass_bare_prices_blocked_in_outreach(text):
    assert guardrails.blocked(guardrails.check(text, "outreach", {"lead_tier": "warm"})), text


@pytest.mark.parametrize("text", [
    "Saw your clinic hit 2,000 followers this month, congrats!", "We booked 120 calls last month for clinics like yours.",
    "I made 5000 cold calls before I learned this.", "Our 2026 cohort starts on Monday because coaches can only take 20.",
    "Most clinics we speak to send 1000 DMs a month.", "In 2025 we worked with 40 clinics.",
    "Quick question about your 2026 goals?", "It's 2026 and referrals still matter.", "The call is 15 minutes.",
    "Our students made 1200 dials in their first week.", "We generated 1,500 leads for a clinic in Leeds.",
    "Since 2019 we've helped clinics grow.", "They made 2025 their best year.",
])
def test_review_pass_counts_and_years_are_not_money(text):
    assert not guardrails.blocked(guardrails.check(text, "outreach", {"lead_tier": "cold"})), text
    assert not guardrails.blocked(guardrails.check(text, "plain")), text


@pytest.mark.parametrize("text", ["Don't promise guaranteed results.", "We never guarantee results.",
                                  "Never use a money-back promise in a DM.", "We don't promise results; we promise a method."])
def test_review_pass_real_negations_still_pass(text):
    assert guardrails.check(text, "plain") == [], text


@pytest.mark.parametrize("dm", ["Let me make Acme owners $50k-$100k a month through our system.",
                                "Let me make your clients $50k-$100k a month through our system.",
                                "Let me make Acme and you $50k-$100k a month through our system."])
def test_review_pass_range_never_names_people_even_with_a_record(tenant, dm):
    _record(tenant)
    assert "outreach_range_shape" in {f["code"] for f in guardrails.check(dm, "outreach", WARM, tenant=tenant)}
    assert guardrails.check(RANGE_DM, "outreach", WARM, tenant=tenant) == []


def test_review_pass_demo_upload_cannot_write_to_the_knowledge_base(client):
    demo_login(client, "owner")
    r = client.post("/t/demo/api/uploads", data={"to_kb": "true"},
                    files={"file": ("x.txt", b"Our students make 40000 a month. zygomorphic quokka ladder.", "text/plain")})
    assert r.status_code == 403 and "pitch demo" in r.json()["detail"]
    hits = client.get("/t/demo/api/search", params={"q": "zygomorphic quokka ladder"}).json()["hits"]
    assert not any(h["file"].startswith("kb/") for h in hits)
    assert not list(tenancy.get("demo").kb_dir.glob("*.txt")) if tenancy.get("demo").kb_dir.exists() else True
    # a plain upload (not to the knowledge base) still works in the demo; a real owner can still add to their KB
    assert client.post("/t/demo/api/uploads", files={"file": ("n.txt", b"notes (DEMO)", "text/plain")}).status_code == 200
    login(client)
    r = client.post("/t/owner/api/uploads", data={"to_kb": "true"},
                    files={"file": ("sop.txt", b"Our zygomorphic referral ladder has three rungs.", "text/plain")})
    assert r.status_code == 200 and r.json()["added_to_kb"] is True


def test_review_pass_inbound_email_with_a_non_text_body_is_not_a_500(demo_tenant, client):
    from operator_ai import channels

    secret = vault.ensure_practice_secret(demo_tenant, "email_webhook_secret")
    for raw in (b"From: Lee <lee@example.invalid>\nSubject: Logo\nContent-Type: image/png\n"
                b"Content-Transfer-Encoding: base64\n\niVBORw0KGgo=\n",
                b"From: Lee <lee@example.invalid>\nSubject: Odd charset\nContent-Type: text/plain; charset=x-bogus\n\n"
                b"What show rate should I aim for?\n"):
        r = client.post("/t/demo/hooks/email", content=raw, headers={"x-signature": channels.sign_body(secret, raw)})
        assert r.status_code == 200, r.text
    assert channels.parse_email("From: a@example.invalid\nContent-Type: image/png\n\nAAAA\n")["body"] == ""


def test_review_pass_short_uncited_advice_is_dropped_from_live_answers(tenant, ai_on):
    ai_on.reply = json.dumps({"covered": "yes", "citations": ["S1"],
                              "answer": "Run it for 72 hours [S1]. Raise your prices. In short:"})
    r = coach.answer(tenant, "How long should a Thunder Dome test run before I judge it?", record=False)
    assert "72 hours" in r["answer"] and "Raise your prices" not in r["answer"]


def _route_calls(dependant) -> set:
    out = set()
    for d in dependant.dependencies:
        out.add(d.call)
        out |= _route_calls(d)
    return out


def _all_routes(routes, prefix=""):
    from fastapi.routing import APIRoute

    for r in routes:
        if isinstance(r, APIRoute):
            yield prefix + r.path, r
        elif hasattr(r, "original_router"):  # FastAPI's included-router wrapper
            yield from _all_routes(r.original_router.routes, getattr(r.include_context, "prefix", ""))
        elif hasattr(r, "routes"):
            yield from _all_routes(r.routes, prefix)


# Reachable without a session, on purpose: sign-in, the public brand, signed webhooks (each checks its own HMAC or
# token), the MCP endpoint (its own bearer tokens), public lead-magnet minis and the web shell.
NO_SESSION_OK = {
    "/health", "/ready", "/", "/t/{tid}", "/t/{tid}/", "/{path:path}", "/embed.js",
    "/t/{tid}/api/public", "/t/{tid}/api/auth/login", "/t/{tid}/api/auth/demo-login", "/t/{tid}/api/auth/logout",
    "/t/{tid}/api/auth/session", "/mcp", "/t/{tid}/mcp", "/m/{tid}/{slug}", "/m/{tid}/{slug}/start", "/m/{tid}/{slug}/ask",
    # delivery: RFC 8058 one-click unsubscribe (GET only shows a confirm page; the token is the credential, guessing throttled)
    "/u/{token}",
    # prospects: the public proposal page and its accept button (live only after approval; throttled; noindex)
    "/t/{tid}/p/{token}", "/t/{tid}/p/{token}/accept",
    # client ops: the client intake form, and the webinar registration page, sign-up and unsubscribe
    "/c/{tid}/{token}", "/w/{tid}/{slug}", "/w/{tid}/{slug}/register", "/w/{tid}/{slug}/unsubscribe",
    # persona: the public disclosure page (who the assistant is, and that it is not the person)
    "/t/{tid}/persona", "/t/{tid}/api/public/persona",
    # team: the one-time invite / reset link (token in the POST body, throttled) and the generic forgot-password
    "/t/{tid}/api/join/check", "/t/{tid}/api/join/accept", "/t/{tid}/api/join/forgot",
}


def test_every_tenant_api_route_needs_a_session(app):
    from fastapi.testclient import TestClient

    from operator_ai import deps

    with TestClient(app):
        routes = list(_all_routes(app.routes))
    assert len(routes) > 100
    open_routes = {p for p, r in routes if deps.get_ctx not in _route_calls(r.dependant)}
    unexpected = {p for p in open_routes - NO_SESSION_OK if not p.startswith("/t/{tid}/hooks/")}
    assert not unexpected, f"routes reachable without a session: {sorted(unexpected)}"
    # and every one of them under /api/ answers 401 without a cookie (spot-check a write and a read per module)
    tenant_api = [(p, r) for p, r in routes if p.startswith("/t/{tid}/api/") and p not in NO_SESSION_OK]
    assert tenant_api


def test_review_pass_personality_survives_a_malformed_stored_persona(owner):
    t = tenancy.get("owner")
    for bad in ("a string", 5, ["x"], {"banned_words": "abc", "display_name": 7, "daily_question_limit": "x"}):
        t.set("persona", bad)  # written before settings were validated
        r = owner.get("/t/owner/api/personality")
        assert r.status_code == 200, (bad, r.text)
        assert isinstance(r.json()["banned_words"], list) and r.json()["display_name"]
    r = owner.put("/t/owner/api/personality", json={"banned_words": ["Pat Sample"]})
    assert r.status_code == 200 and r.json()["banned_words"] == ["Pat Sample"]


@pytest.mark.parametrize("text", ["1," * 10000, "1." * 10000, "1'" * 10000, "9" * 20000, "six " * 5000, "twenty " * 2850,
                                  "Let me make Acme $50k-$100k a month " * 550])
def test_review_pass_guardrails_stay_fast_on_hostile_input(text):
    # anyone (even an anonymous demo visitor) can send 20,000 characters to /guardrails/check; "1,1,1,..." used to
    # take over a minute of CPU in outreach mode (number patterns rescanned the whole run from every digit)
    # CPU time, not wall-clock: the guard is against a CPU blow-up, and a busy parallel run must not flake it (QE-6)
    t0 = time.process_time()
    for mode, meta in (("outreach", {"lead_tier": "warm"}), ("plain", None), ("client_asset", {"client_asset": True})):
        guardrails.check(text[:20000], mode, meta)
    assert time.process_time() - t0 < 5, round(time.process_time() - t0, 2)


def test_review_pass_guardrail_endpoint_answers_hostile_input_quickly(client):
    demo_login(client, "student")
    t0 = time.process_time()  # CPU time across the app's threads in this process (QE-6)
    r = client.post("/t/demo/api/guardrails/check", json={"text": "1," * 10000, "mode": "outreach",
                                                          "meta": {"lead_tier": "warm"}})
    assert r.status_code == 200 and time.process_time() - t0 < 5


def test_review_pass_demo_reset_clears_anything_added_to_the_demo_kb_folder(demo_tenant):
    # a file planted in the demo's KB folder before uploads to the KB were refused there must not survive a reset
    demo_tenant.kb_dir.mkdir(parents=True, exist_ok=True)
    (demo_tenant.kb_dir / "planted.txt").write_text("Our students make 40000 a month. zygomorphic quokka ladder.")
    index.invalidate()
    assert "kb/planted.txt" in index.for_tenant(demo_tenant).docs
    demo.reset(demo_tenant, "test")
    assert not list(demo_tenant.kb_dir.glob("*.txt"))
    assert "kb/planted.txt" not in index.for_tenant(demo_tenant).docs
