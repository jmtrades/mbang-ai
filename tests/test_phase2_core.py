import threading

import pytest

from operator_ai import audit, critique, llm, queue, runtime, settings
from operator_ai.redact import redact
from tests.fakes import FakeModel


@pytest.fixture
def fake_model(monkeypatch):
    fm = FakeModel()
    llm.set_transport(fm.transport)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key-123456789")
    monkeypatch.setenv("OPERATOR_DISABLE_LLM", "0")
    yield fm
    llm.set_transport(None)


# ---------------------------------------------------------------- AI budget gate (C10)
def test_budget_zero_fails_closed_from_any_thread(tenant, fake_model):
    assert llm.budget(tenant) == 0
    before = llm.REQUESTS_SENT
    errors = []

    def call():
        try:
            llm.complete(tenant, "sys", "hello", purpose="t")
        except llm.AIUnavailable as e:
            errors.append(e.reason)

    threads = [threading.Thread(target=call) for _ in range(5)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    call()
    assert len(errors) == 6 and all(e == "AI off (budget 0)" for e in errors)
    assert fake_model.calls == [] and llm.REQUESTS_SENT == before
    assert llm.state(tenant)["label"] == "AI off (budget 0)"


def test_budget_positive_calls_model_and_charges(tenant, fake_model):
    settings.set_box(tenant, "ai_token_budget", 1000, "owner approved test budget")
    out = llm.complete(tenant, "sys", "hello", purpose="t")
    assert out["mode"] == "live" and out["text"] == "Fake AI reply."
    assert llm.used(tenant) == 120
    assert len(fake_model.calls) == 1


def test_budget_exhausted_stops(tenant, fake_model):
    settings.set_box(tenant, "ai_token_budget", 100, "test")
    llm.complete(tenant, "sys", "a", purpose="t")  # uses 120 > 100
    with pytest.raises(llm.AIUnavailable, match="used up"):
        llm.complete(tenant, "sys", "b", purpose="t")
    assert len(fake_model.calls) == 1


def test_disable_llm_env_forces_fallback(tenant, fake_model, monkeypatch):
    settings.set_box(tenant, "ai_token_budget", 1000, "test")
    monkeypatch.setenv("OPERATOR_DISABLE_LLM", "1")
    with pytest.raises(llm.AIUnavailable):
        llm.complete(tenant, "sys", "a", purpose="t")
    assert fake_model.calls == []


def test_replay_store_labelled(tenant, fake_model):
    settings.set_box(tenant, "ai_token_budget", 1000, "test")
    llm.complete(tenant, "sys", "what is x", purpose="coach", run_id=7)
    settings.set_box(tenant, "ai_token_budget", 0, "")
    out = llm.complete_or_replay(tenant, "sys", "what is x", purpose="coach")
    assert out["mode"] == "replay" and out["text"] == "Fake AI reply."
    assert out["label"].startswith("Replayed AI answer (run #7,")


def test_model_errors_are_redacted(tenant, monkeypatch):
    fm = FakeModel(status=401)
    llm.set_transport(fm.transport)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-should-not-leak-999")
    monkeypatch.setenv("OPERATOR_DISABLE_LLM", "0")
    settings.set_box(tenant, "ai_token_budget", 1000, "test")
    try:
        with pytest.raises(llm.AIUnavailable) as ei:
            llm.complete(tenant, "s", "p", purpose="t")
        assert "sk-ant" not in ei.value.reason
    finally:
        llm.set_transport(None)


# ---------------------------------------------------------------- redaction (C6)
@pytest.mark.parametrize("secret", [
    "sk-ant-api03-ABCDEFGHIJKLMNOP", "sk-proj-1234567890abcdef", "EAABwzLixnjYBO1234567890abcdef",
    "ya29.a0AfH6SMBx1234567890", "1//0gLongRefreshToken123456",
])
def test_redact_token_patterns(secret):
    out = redact(f"error calling api with {secret} now")
    assert secret not in out and "[REDACTED]" in out


def test_redact_query_and_bearer():
    out = redact("GET /x?access_token=abc123secret&x=1 Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload")
    assert "abc123secret" not in out and "eyJhbGci" not in out
    assert "access_token=[REDACTED]" in out


def test_audit_lines_redacted(tenant):
    audit.log(tenant, "x", "settings", {"note": "token sk-abcdefghijklmnop leaked?"})
    assert "sk-abcdefghij" not in audit.recent(tenant, 1)[0]["detail"]


# ---------------------------------------------------------------- runtime traces
def test_run_records_steps_and_result(tenant):
    def fn(run):
        a = run.step("search", {"q": "x"}, lambda: [1, 2])
        run.step("draft", {"n": len(a)}, lambda: "hello")
        return {"ok": True}

    out = runtime.execute(tenant, "test", "t", ["search", "draft"], fn)
    r = runtime.get(tenant, out["run_id"])
    assert r["status"] == "done" and [s["tool"] for s in r["steps"]] == ["search", "draft"]
    assert all(s["status"] == "ok" and s["ms"] is not None for s in r["steps"])
    assert r["result"] == {"ok": True} and r["ai_mode"] == "rules"


def test_run_scrubs_banned_names_and_secrets(tenant):
    tenant.set("banned_names", ["Pat Sample"])
    out = runtime.execute(tenant, "test", "t", ["x"],
                          lambda run: run.step("x", {"q": "Pat Sample"}, lambda: "key sk-abcdefghijklmnop") and {"r": 1})
    r = runtime.get(tenant, out["run_id"])
    blob = str(r["steps"])
    assert "Pat Sample" not in blob and "sk-abcdefghij" not in blob


def test_run_error_marks_step(tenant):
    def fn(run):
        run.step("boom", {}, lambda: 1 / 0)

    with pytest.raises(ZeroDivisionError):
        runtime.execute(tenant, "test", "t", ["boom"], fn)
    r = runtime.recent(tenant, 1)[0]
    assert r["status"] == "error"


# ---------------------------------------------------------------- approval queue (C1)
def test_queue_block_edit_approve_never_sends(tenant):
    item = queue.add(tenant, "dm", "We guarantee results!", channel="instagram_dm", recipient_ref="@lead_demo",
                     meta={"prospect_facing": True, "lead_tier": "warm"})
    assert item["status"] == "held" and any(f["code"] == "guarantee" for f in item["flags"])
    with pytest.raises(queue.QueueError) as ei:
        queue.approve(tenant, item["id"], "owner(owner)")
    assert ei.value.status == 409
    item = queue.edit(tenant, item["id"], "Here's a short video on fixing show rates. Want me to send it over?",
                      "owner(owner)")
    assert item["status"] == "pending" and item["flags"] == []
    done = queue.approve(tenant, item["id"], "owner(owner)")
    assert done["status"] == "approved" and done["delivered"] == "mock:instagram"
    with tenant.db() as c:
        msg = c.execute("SELECT * FROM channel_messages").fetchone()
    assert msg["direction"] == "out" and '"mock": true' in msg["meta"]


def test_queue_manual_channel_ready_to_send_manually(tenant):
    item = queue.add(tenant, "note", "Plain internal note.")
    assert queue.approve(tenant, item["id"], "o")["delivered"] == "ready to send manually"


def test_queue_needs_human_for_unreasoned_deadline(tenant):
    item = queue.add(tenant, "email", "Enrollment closes on Friday.", channel="email", meta={"client_asset": True})
    assert item["status"] == "needs_human"
    assert queue.approve(tenant, item["id"], "o")["status"] == "approved"


def test_queue_approve_rechecks_after_edit(tenant):
    item = queue.add(tenant, "dm", "Want a quick video on show rates?", channel="instagram_dm",
                     meta={"prospect_facing": True})
    with tenant.db() as c:  # simulate a body changed behind the API's back
        c.execute("UPDATE queue SET body='Our students make $100k a month' WHERE id=?", (item["id"],))
    with pytest.raises(queue.QueueError):
        queue.approve(tenant, item["id"], "o")
    assert queue.get(tenant, item["id"])["status"] == "held"


def test_queue_role_permissions(tenant):
    inv = queue.add(tenant, "calendar_invite", "Call with lead (DEMO)", channel="calendar")
    with pytest.raises(queue.QueueError) as ei:
        queue.approve(tenant, inv["id"], "setter", role="setter")
    assert ei.value.status == 403
    dm = queue.add(tenant, "dm", "Want a quick video on show rates?", channel="instagram_dm", meta={"prospect_facing": True})
    assert queue.approve(tenant, dm["id"], "setter", role="setter")["status"] == "approved"


def test_reject_and_double_decide(tenant):
    item = queue.add(tenant, "note", "x")
    queue.reject(tenant, item["id"], "o", reason="no")
    with pytest.raises(queue.QueueError):
        queue.approve(tenant, item["id"], "o")


def test_never_invite_checked_on_propose_and_send(tenant):
    tenant.set("blocked_attendees", ["Blocked.Person+x@gmail.com"])
    item = queue.add(tenant, "calendar_invite", "Call", channel="calendar", recipient_ref="blockedperson@googlemail.com")
    assert item["status"] == "held" and any(f["code"] == "never_invite" for f in item["flags"])
    ok = queue.add(tenant, "calendar_invite", "Call", channel="calendar", recipient_ref="fine@example.invalid")
    tenant.set("blocked_attendees", ["Blocked.Person+x@gmail.com", "fine@example.invalid"])
    with pytest.raises(queue.QueueError):
        queue.approve(tenant, ok["id"], "o")


def test_canonical_email():
    assert queue.canonical_email("J.Doe+promo@GoogleMail.com") == "jdoe@gmail.com"
    assert queue.canonical_email("a.b+x@example.invalid") == "a.b@example.invalid"


def test_audit_covers_queue_lifecycle(tenant):
    item = queue.add(tenant, "dm", "guaranteed results", channel="instagram_dm")
    queue.edit(tenant, item["id"], "Quick question about your clinic?", "o")
    queue.approve(tenant, item["id"], "o")
    kinds = {a["kind"] for a in audit.recent(tenant, 50)}
    assert {"queue.add", "guardrail.block", "queue.edit", "queue.approve"} <= kinds


# ---------------------------------------------------------------- queue over the API
def test_queue_api_flow(owner):
    from operator_ai import tenancy

    t = tenancy.get("owner")
    item = queue.add(t, "dm", "Last chance to join!", channel="instagram_dm", meta={"prospect_facing": True})
    r = owner.post(f"/t/owner/api/queue/{item['id']}/approve")
    assert r.status_code == 409
    r = owner.put(f"/t/owner/api/queue/{item['id']}", json={"body": "Want the short checklist we use for reminders?"})
    assert r.status_code == 200 and r.json()["status"] == "pending"
    r = owner.post(f"/t/owner/api/queue/{item['id']}/approve")
    assert r.json()["status"] == "approved"


# ---------------------------------------------------------------- settings: BOX_ONLY (C8)
@pytest.mark.parametrize("key", ["live_meta_enabled", "meta_allowlist", "live_calendar_enabled", "ai_token_budget",
                                 "critique_token_budget"])
def test_settings_api_refuses_box_only(owner, key):
    r = owner.put("/t/owner/api/settings", json={"key": key, "value": True})
    assert r.status_code == 403


def test_settings_api_allows_ordinary(owner):
    r = owner.put("/t/owner/api/settings", json={"key": "banned_names", "value": ["Pat Sample"]})
    assert r.status_code == 200


def test_switch_on_needs_recorded_yes(tenant):
    with pytest.raises(ValueError):
        settings.set_box(tenant, "live_meta_enabled", True, "")
    settings.set_box(tenant, "live_meta_enabled", True, "Owner said yes on call 2026-10-26")
    a = audit.recent(tenant, 5, "switch")[0]
    assert "Owner said yes" in a["detail"]
    settings.set_box(tenant, "live_meta_enabled", False, "")  # turning off needs nothing
    assert tenant.get("live_meta_enabled") is False


def test_tenant_admin_cli(env):
    import subprocess
    import sys

    from operator_ai import config, tenancy

    tenancy.ensure_defaults()
    run = lambda *a: subprocess.run([sys.executable, str(config.ROOT / "scripts" / "tenant_admin.py"), *a],
                                    capture_output=True, text=True)
    assert run("switch", "owner", "live_meta_enabled", "on").returncode == 4
    r = run("switch", "owner", "live_meta_enabled", "on", "--owner-yes", "recorded yes")
    assert r.returncode == 0, r.stderr
    tenancy.clear_cache()
    assert tenancy.get("owner").get("live_meta_enabled") is True
    assert run("switch", "owner", "live_meta_enabled", "off").returncode == 0


# ---------------------------------------------------------------- self-critique (B9)
def test_critique_without_budget_sets_needs_human(tenant):
    res = critique.loop(tenant, "Hey! What's your goal? How many clients? What's your budget?", "dm_script")
    assert res["needs_human"] and res["mode"] == "rules"
    assert res["reason"] == "Critique off (critique budget 0)"
    assert len(res["rounds"]) == 1


def test_critique_rewrites_with_budget_max_two(tenant, fake_model):
    settings.set_box(tenant, "critique_token_budget", 5000, "test")
    fake_model.replies = ["Still bad? Really? Yes?", "Still bad? Really? Yes?", "never reached"]
    res = critique.loop(tenant, "Hey! What's your goal? How many clients? What's your budget?", "dm_script")
    assert len(res["rounds"]) == 3 and res["needs_human"]
    assert len(fake_model.calls) == 2


def test_critique_rewrite_passes(tenant, fake_model):
    settings.set_box(tenant, "critique_token_budget", 5000, "test")
    fake_model.reply = "Loved your post on referral nights. Here's a quick idea for filling them: want me to send it?"
    res = critique.loop(tenant, "Hey! What's your goal? How many clients? What's your budget?", "dm_script")
    assert res["passes"] and not res["needs_human"] and res["mode"] == "live"


def test_critique_cites_sop(tenant):
    res = critique.critique(tenant, "In today's fast-paced world unlock growth. Click here! Buy now!", "ad")
    assert res["citations"] and all(c["id"].startswith("E") for c in res["citations"])
    assert "multiple_ctas" in res["detected"] and "ai_words" in res["detected"]
