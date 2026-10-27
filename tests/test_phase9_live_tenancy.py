import json
import os
import stat
import subprocess
import sys
import time

import httpx
import pytest

from operator_ai import config, index, jobs, queue, retrieval, settings, skills, tenancy, uploads, vault
from operator_ai.live import chain, gcal, meta, net
from tests.conftest import login, owner_password
from tests.fakes import FakeWorld


@pytest.fixture
def world(monkeypatch):
    w = FakeWorld()
    net.set_transport(w.transport)
    monkeypatch.setattr(net, "_sleep", lambda s: None)  # no real waiting in tests
    gcal._cache.clear()
    yield w
    net.set_transport(None)
    gcal._cache.clear()
    assert w.stray == [], f"stray hosts contacted: {w.stray}"


def _connect(t):
    vault.put(t, {"meta_app_secret": "app-secret-" + t.id, "meta_access_token": "EAAfaketoken" + t.id * 3,
                  "meta_verify_token": "verify-" + t.id, "google_client_id": "cid", "google_client_secret": "csec",
                  "google_refresh_token": "1//fakerefresh" + t.id, "google_calendar_id": "cal-" + t.id})
    settings.set_box(t, "live_meta_enabled", True, "test: owner yes recorded")
    settings.set_box(t, "live_calendar_enabled", True, "test: owner yes recorded")


def _webhook(leadgen_id):
    return json.dumps({"object": "page", "entry": [{"id": "p1", "changes": [{"field": "leadgen", "value": {
        "leadgen_id": leadgen_id, "form_id": "f1", "page_id": "p1"}}]}]}).encode()


def _lead_payload(name, email, hot=True):
    vals = {"full_name": name, "email": email, "monthly_revenue": "100k+" if hot else "<10k",
            "can_invest": "yes" if hot else "no", "timeline": "now" if hot else "later", "decision_maker": "true",
            "business": "Peak Physio (DEMO)"}
    return {"field_data": [{"name": k, "values": [v]} for k, v in vals.items()], "ad_id": "a1", "form_id": "f1"}


# ---------------------------------------------------------------- tenancy
def test_licence_error_on_course_for_clients(env):
    tenancy.ensure_defaults()
    with pytest.raises(tenancy.LicenceError):
        tenancy.create("cli", "Client", kb_scope="course", kind="client")
    t, _ = tenancy.create("cli2", "Client 2")
    assert t.kb_scope == "empty" and t.get("ai_token_budget") == 0
    with pytest.raises(tenancy.LicenceError):
        tenancy.set_kb_scope("cli2", "course")
    assert tenancy.set_kb_scope("cli2", "own").kb_scope == "own"
    from operator_ai import audit

    assert any(a["kind"] == "kb.scope" for a in audit.recent(tenancy.get("cli2"), 10))


def test_client_tenant_starts_empty(env):
    tenancy.ensure_defaults()
    t, _ = tenancy.create("fresh", "Fresh Client")
    assert retrieval.search(t, "Thunder Dome rules") == []
    from operator_ai.agents import coach

    assert coach.answer(t, "What are the rules for a Thunder Dome test?", record=False)["covered"] == "no"


def test_third_party_switch_off_by_default(env):
    assert config.get().third_party_kb is False


def test_cross_tenant_404(client):
    tenancy.create("other", "Other Co")
    login(client)  # owner session cookie
    sess = client.cookies.get("op_s")
    client.cookies.clear()
    client.cookies.set("op_s", sess, path="/t/other")
    assert client.get("/t/other/api/me").status_code == 404
    assert client.get("/t/other/api/queue").status_code == 404
    assert client.get("/t/nonexistent/api/me").status_code == 404


def test_cookie_lookup_only_in_own_tenant(client):
    tenancy.create("other2", "Other 2")
    r = login(client)
    assert "Path=/t/owner" in r.headers["set-cookie"]
    # the browser would never send this cookie to /t/other2; and if forced it doesn't authenticate there
    assert client.get("/t/other2/api/me", cookies={"op_s": client.cookies.get("op_s")}).status_code == 404


def test_tenant_create_script_mode_600_never_prints(env):
    r = subprocess.run([sys.executable, str(config.ROOT / "scripts" / "tenant_create.py"), "newco", "New Co"],
                       capture_output=True, text=True, env={**os.environ})
    assert r.returncode == 0, r.stderr
    pw_file = config.get().tenants_dir / "newco" / "owner_password.txt"
    pw = pw_file.read_text().strip()
    assert stat.S_IMODE(os.stat(pw_file).st_mode) == 0o600
    assert pw not in r.stdout and pw not in r.stderr
    r2 = subprocess.run([sys.executable, str(config.ROOT / "scripts" / "tenant_create.py"), "newco2", "X", "--kb-scope",
                         "course"], capture_output=True, text=True)
    assert r2.returncode == 3 and "LicenceError" in r2.stderr


# ---------------------------------------------------------------- webhook HMAC + handshake
def test_meta_webhook_signature_and_size(client, world):
    t = tenancy.get("owner")
    body = _webhook("L1")
    good = meta.sign(meta.app_secret(t), body)
    assert client.post("/t/owner/hooks/meta", content=body).status_code == 401
    assert client.post("/t/owner/hooks/meta", content=body, headers={"x-hub-signature-256": "sha256=00"}).status_code == 401
    r = client.post("/t/owner/hooks/meta", content=body, headers={"x-hub-signature-256": good})
    assert r.status_code == 200 and r.json()["accepted"] == 1
    r = client.post("/t/owner/hooks/meta", content=body, headers={"x-hub-signature-256": good})
    assert r.json()["duplicates"] == 1  # UNIQUE dedupe key
    big = b"{" + b" " * (256 * 1024) + b"}"
    assert client.post("/t/owner/hooks/meta", content=big,
                       headers={"x-hub-signature-256": meta.sign(meta.app_secret(t), big)}).status_code == 413


def test_practice_secret_before_connection_is_never_unsigned(env):
    tenancy.ensure_defaults()
    t = tenancy.get("owner")
    s1 = meta.app_secret(t)
    assert len(s1) == 64 and s1 == meta.app_secret(t)
    with pytest.raises(meta.WebhookRejected):
        meta.verify_signature(t, b"{}", None)


def test_meta_handshake(client):
    t = tenancy.get("owner")
    vault.put(t, {"meta_verify_token": "my-verify"})
    ok = client.get("/t/owner/hooks/meta", params={"hub.mode": "subscribe", "hub.verify_token": "my-verify",
                                                   "hub.challenge": "12345"})
    assert ok.status_code == 200 and ok.text == "12345"
    bad = client.get("/t/owner/hooks/meta", params={"hub.mode": "subscribe", "hub.verify_token": "nope",
                                                    "hub.challenge": "1"})
    assert bad.status_code == 403


# ---------------------------------------------------------------- retries / backoff / token bucket
def test_get_retries_429_and_5xx_and_meta_rate_codes(world):
    world.fail_next = [429, 503, 400]  # 400 with Meta code 17 = rate limit
    world.leads["L9"] = _lead_payload("A (DEMO)", "a@example.invalid")
    r = net.request("t", "GET", f"{meta.GRAPH}/L9", params={"access_token": "x", "appsecret_proof": "y"})
    assert r.status_code == 200 and len(world.calls) == 4


def test_post_not_blindly_retried(world):
    world.fail_next = [500]
    r = net.request("t", "POST", f"{meta.GRAPH}/act_1/campaigns", params={"access_token": "x", "appsecret_proof": "y"},
                    json_body={"name": "x"})
    assert r.status_code == 500 and len(world.calls) == 1
    world.fail_next = [500]
    r = net.request("t", "POST", f"{meta.GRAPH}/act_1/campaigns", params={"access_token": "x", "appsecret_proof": "y"},
                    json_body={"name": "x"}, idempotent=True)
    assert r.status_code == 200 and len(world.calls) == 3


def test_retry_after_honoured(monkeypatch, world):
    waits = []
    monkeypatch.setattr(net, "_sleep", lambda s: waits.append(s))
    world.fail_next = [429]
    world.leads["L8"] = _lead_payload("B (DEMO)", "b@example.invalid")
    net.request("t2", "GET", f"{meta.GRAPH}/L8", params={"access_token": "x", "appsecret_proof": "y"})
    assert 0.0 in waits  # Retry-After: 0 was used instead of the backoff


def test_token_bucket_limits_rate(monkeypatch):
    waits = []
    monkeypatch.setattr(net, "_sleep", lambda s: waits.append(s))
    b = net.TokenBucket(rate=10, burst=2)
    b.take(), b.take()
    b.take()
    assert waits and waits[0] > 0


def test_stray_host_returns_599(world):
    r = net.request("t", "GET", "https://example.com/x")
    assert r.status_code == 599
    world.stray.clear()  # this test intends the stray call


def test_errors_redacted(world):
    world.fail_next = [500, 500, 500, 500]
    r = net.request("t", "GET", f"{meta.GRAPH}/x", params={"access_token": "EAAsecret123456789", "appsecret_proof": "y"})
    assert r.status_code == 500 and len(world.calls) == 4  # GET retried up to the limit, then surfaced
    with pytest.raises(net.LiveError) as ei:
        net.ok_json(r)
    assert "EAAsecret" not in str(ei.value)
    assert "EAAsecret" not in str(net.LiveError("GET graph?access_token=EAAsecret123456789 failed"))


# ---------------------------------------------------------------- inbox dead after 8 attempts
def test_inbox_gives_up_after_8_attempts(env, world, monkeypatch):
    tenancy.ensure_defaults()
    t = tenancy.get("owner")
    _connect(t)
    monkeypatch.setattr(chain, "INBOX_BACKOFF", 0.0)
    raw = _webhook("LDEAD")  # the fake Graph doesn't know this lead -> 404 every time
    chain.receive_webhook(t, raw, meta.sign(meta.app_secret(t), raw))
    jobs.drain(t, timeout=30)
    with t.db() as c:
        row = c.execute("SELECT status, attempts FROM chain_inbox WHERE dedupe_key='LDEAD'").fetchone()
    assert row["status"] == "dead" and row["attempts"] == 8


# ---------------------------------------------------------------- full chain, 3 tenants, over HTTP
def _make_tenants():
    tenancy.ensure_defaults()
    own, _ = tenancy.create("client-own", "Client Own", kb_scope="own")
    uploads.save(own, 1, "playbook.txt", b"Qualify before you send the link: ask about goal, timeline and whether they "
                                        b"can invest. Our own client playbook.", to_kb=True)
    empty, _ = tenancy.create("client-empty", "Client Empty", kb_scope="empty")
    return [tenancy.get("owner"), empty, own]


def test_chain_end_to_end_three_tenants(client, world):
    tenants = _make_tenants()
    for n, t in enumerate(tenants):
        _connect(t)
        # these leads only gave an email, so replies go by email: prospect email needs the business postal address
        skills.put_profile(t, {"identity": {"name": "Peak Growth (DEMO)", "postal_address": "1 Example Street, Testtown"}}, "t")
        lid = f"LEAD-{t.id}"
        world.leads[lid] = _lead_payload(f"Robin {n} (DEMO)", f"robin{n}@example.invalid", hot=True)
        raw = _webhook(lid)
        r = client.post(f"/t/{t.id}/hooks/meta", content=raw, headers={"x-hub-signature-256": meta.sign(meta.app_secret(t), raw)})
        assert r.status_code == 200 and r.json()["accepted"] == 1
        jobs.drain(t, timeout=30)
        with t.db() as c:
            lead = c.execute("SELECT * FROM chain_leads WHERE leadgen_id=?", (lid,)).fetchone()
        assert lead is not None and lead["tier"] == "hot"
        cites = json.loads(lead["citations"])
        if t.kb_scope == "course":
            assert cites and all(not c["file"].startswith("kb/") for c in cites)
        elif t.kb_scope == "own":
            assert cites and all(c["file"].startswith("kb/") for c in cites)  # only its own uploads
        else:
            assert cites == []  # empty KB: no course material leaks into a client tenant
        # the lead fetch used the tenant's own token + appsecret_proof
        assert any(m == "GET" and p == f"/v21.0/{lid}" for m, h, p in world.calls)
        # Outreach cadence (2026-10 wave): only the first reply is queued (to the lead's real email, not a placeholder);
        # the value follow-up is drafted by the cadence once that reply is confirmed sent (was 2 follow-ups up front).
        fus = queue.list_items(t, kind="followup")
        assert len(fus) == 1 and all(f["status"] in ("pending", "needs_human") for f in fus)
        assert fus[0]["channel"] == "email" and fus[0]["recipient_ref"] == f"robin{n}@example.invalid"
        queue.approve(t, fus[0]["id"], "owner(owner)")
        # recorded reply -> booking DM with real free slots from Google freeBusy
        b = chain.record_reply(t, lead["id"], "Yes, keen to chat")
        assert len(b["slots"]) == 3 and b["invite"] is None  # the invite waits until they pick a time
        queue.approve(t, b["booking_dm"], "owner(owner)")  # they can only pick from times they were sent
        b = chain.pick_slot(t, lead["id"], 0)
        inv = queue.get(t, b["invite"])
        assert inv["kind"] == "calendar_invite" and inv["meta"]["live"]
        with pytest.raises(queue.QueueError):
            queue.approve(t, inv["id"], "closer", role="closer")  # owner approval only
        done = queue.approve(t, inv["id"], "owner(owner)")
        assert done["delivered"].startswith("live:google_calendar:")
        eid = done["delivered"].rsplit(":", 1)[1]
        assert eid in world.events and world.events[eid]["attendees"][0]["email"] == f"robin{n}@example.invalid"
        # retry with the same deterministic id: 409 is read back, no duplicate
        again = gcal.insert_event(t, eid, "x", time.time(), time.time() + 60, [])
        assert again["existing"] and len([e for e in world.events if e == eid]) == 1
        ladder = [i for i in queue.list_items(t, kind="reminder") if i["meta"].get("chain_lead_id") == lead["id"]]
        assert sorted(i["meta"]["ladder"] for i in ladder) == ["10min", "24h", "2h"]
        # real address (was the placeholder 'lead-{id}'), timed, and cancelled with the booking
        assert all(i["recipient_ref"] == f"robin{n}@example.invalid" and i["meta"]["send_after"] for i in ladder)
        # exactly one calendar event for the live invite (the approval hook reuses the live mirror)
        with t.db() as c:
            assert c.execute("SELECT COUNT(*) FROM calendar_events WHERE title LIKE 'Invite:%'").fetchone()[0] == 1
        chain.record_outcome(t, lead["id"], "showed")
        # per-tenant report over HTTP (session-scoped)
        if t.id == "owner":
            login(client)
            rep = client.get("/t/owner/report").json()
        else:
            pw = owner_password(t.id)
            client.post(f"/t/{t.id}/api/auth/login", json={"username": "owner", "password": pw})
            rep = client.get(f"/t/{t.id}/report").json()
        assert rep["tenant"] == t.id and rep["leads_by_stage"] == {"showed": 1}
    # isolation: each tenant only sees its own lead
    for t in tenants:
        with t.db() as c:
            assert c.execute("SELECT COUNT(*) FROM chain_leads").fetchone()[0] == 1


def test_invite_blocked_at_send_time(env, world):
    tenancy.ensure_defaults()
    t = tenancy.get("owner")
    _connect(t)
    world.leads["LB"] = _lead_payload("Blocked (DEMO)", "Blocked.Person+x@gmail.com")
    raw = _webhook("LB")
    chain.receive_webhook(t, raw, meta.sign(meta.app_secret(t), raw))
    jobs.drain(t, timeout=30)
    with t.db() as c:
        lead_id = c.execute("SELECT id FROM chain_leads").fetchone()[0]
    skills.put_profile(t, {"identity": {"postal_address": "1 Example Street, Testtown"}}, "t")  # email footer address
    r = chain.record_reply(t, lead_id, "Yes please, happy to talk")  # positive reply -> times; the invite needs a pick
    queue.approve(t, r["booking_dm"], "owner(owner)")
    b = chain.pick_slot(t, lead_id, 0)
    t.set("blocked_attendees", ["blockedperson@googlemail.com"])  # added after the proposal
    with pytest.raises(queue.QueueError):
        queue.approve(t, b["invite"], "owner(owner)")
    assert world.events == {}  # no Google event was created


def test_switch_off_means_no_outbound_calls(env, world):
    tenancy.ensure_defaults()
    t = tenancy.get("owner")
    raw = json.dumps({"entry": [{"changes": [{"field": "leadgen", "value": {
        "leadgen_id": "P1", "practice_fields": {"full_name": "Practice (DEMO)", "monthly_revenue": "30k-100k",
                                                "can_invest": "yes", "timeline": "now"}}}]}]}).encode()
    chain.receive_webhook(t, raw, meta.sign(meta.app_secret(t), raw))
    jobs.drain(t, timeout=30)
    r = chain.record_reply(t, 1, "Yes, sounds good")
    queue.approve(t, r["booking_dm"], "owner(owner)")
    b = chain.pick_slot(t, 1, 0)
    queue.approve(t, b["invite"], "owner(owner)")
    assert world.calls == []  # nothing left the building
    # real workspace with the switch off: local calendar only, the owner sends the invite (no mock in real workspaces)
    inv = queue.get(t, b["invite"])
    assert inv["status"] == "awaiting_manual_send" and inv["delivered"] is None


def test_meta_ads_adapter_forces_paused(env, world):
    tenancy.ensure_defaults()
    t = tenancy.get("owner")
    _connect(t)
    settings.set_box(t, "meta_allowlist", ["act_123"], "test")
    ad = meta.MetaAdsAdapter(t, "act_123")
    ad.create("campaign", "act_123", "x", {}, 500.0, "ACTIVE")
    sent = list(world.ads.values())[0]
    assert sent["status"] == "PAUSED"
    with pytest.raises(PermissionError):
        meta.MetaAdsAdapter(t, "act_not_allowed")


def test_preflight_lists_missing_without_values(env):
    r = subprocess.run([sys.executable, str(config.ROOT / "scripts" / "preflight_live.py")], capture_output=True, text=True,
                       env={**os.environ, "ANTHROPIC_API_KEY": "sk-ant-secret-value-xyz"})
    assert "MISSING" in r.stdout and "sk-ant-secret" not in r.stdout
