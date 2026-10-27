"""Review fixes for client ops and webinars: each test failed before its fix.

* Non-finite numbers (NaN, infinity) or impossible weeks no longer reach the database and turn every later read
  into a 500; they are refused with 422.
* Only the owner links a client's ad account (the weekly loop reads it and, with the live switch, the Meta API).
* The public intake form refuses secrets in the proof and asset links too.
* Public registrant names can't put links into emails that go out on a template's approval.
* Oversized CSV cells and unknown webinar chains are 422 / 404, not 500 / a dangling webinar.
* Without the delivery feature, timed webinar emails wait until they are due instead of going out the moment the
  template is approved; moving the webinar or changing a link redrafts every email that hasn't gone out.
* Unsubscribing withdraws items waiting for a manual send; every per-registrant email keeps its unsubscribe line.
* Attendance imported after the follow-up already ran still gets the people who stayed a setter follow-up.
"""
from __future__ import annotations

import time

import pytest

from operator_ai import auth, client_ops, db, queue, settings, webinars
from tests.conftest import login

OWNER = {"id": 1, "username": "owner", "role": "owner"}


@pytest.fixture(autouse=True)
def _clear_rate_limits():
    webinars._rl.clear()
    yield
    webinars._rl.clear()


def _sent(t) -> int:
    with t.db() as c:
        return c.execute("SELECT COUNT(*) FROM channel_messages WHERE direction='out'").fetchone()[0]


def _webinar(t, starts_in=3 * 86400, **kw):
    data = {"title": "Five numbers every coach should track", "starts_at": time.time() + starts_in, "tz": "Europe/London",
            "join_url": "https://zoom.example.com/j/1", "replay_url": "https://video.example.com/r/1", **kw}
    w = webinars.create(t, data, OWNER)
    queue.approve(t, w["page_queue_id"], "owner(owner)")
    return {**w, **webinars.get(t, w["id"])}


def _tpl_qid(w, key):
    return w["template_queue_ids"][[k[0] for k in webinars.TEMPLATES].index(key)]


def _emails(t, key=None):
    return [i for i in queue.list_items(t, kind="webinar_email", limit=500)
            if key is None or i["meta"]["template_key"] == key]


def _token(t, rid):
    with t.db() as c:
        return c.execute("SELECT token FROM webinar_registrants WHERE id=?", (rid,)).fetchone()[0]


# =============================================================================== numbers that break reads
def test_non_finite_numbers_are_refused_not_stored(client, tenant):
    login(client)
    raw = lambda body: client.post("/t/owner/api/clients", content=body, headers={"content-type": "application/json"})
    assert raw(b'{"name":"N","metrics":{"leads":NaN},"onboard":false}').status_code == 422
    assert raw(b'{"name":"N","metrics":{"leads":"Infinity"},"onboard":false}').status_code == 422
    assert raw(b'{"name":"N","metrics":{"leads":-5},"onboard":false}').status_code == 422
    ok = client.post("/t/owner/api/clients", json={"name": "Numbers Co", "metrics": {"leads": 40, "booked": 9},
                                                   "onboard": False})
    assert ok.status_code == 200
    cid = ok.json()["id"]
    for bad in ('{"kpi_targets":{"leads":"NaN"}}', '{"kpi_targets":{"leads":NaN}}', '{"kpi_targets":{"leads":-1}}'):
        r = client.put(f"/t/owner/api/clients/{cid}", content=bad.encode(), headers={"content-type": "application/json"})
        assert r.status_code == 422, (bad, r.status_code)
    assert client.get("/t/owner/api/clients").status_code == 200
    d = client.get(f"/t/owner/api/clients/{cid}")
    assert d.status_code == 200 and d.json()["metrics"] == {"leads": 40.0, "booked": 9.0}


def test_impossible_week_is_422_not_500(client, tenant):
    login(client)
    cid = client.post("/t/owner/api/clients", json={"name": "Week Co", "onboard": False}).json()["id"]
    csv_text = "Ad name,Amount spent (USD),Results,Impressions,Link clicks\nHook A,120.50,6,4000,60\n"
    for ps in ("1e300", "1e12", "Infinity", "12"):
        body = ('{"metrics":{"leads":1},"period_start":%s}' % ps).encode()
        r = client.post(f"/t/owner/api/clients/{cid}/metrics", content=body, headers={"content-type": "application/json"})
        assert r.status_code == 422, (ps, r.status_code)
        r = client.post(f"/t/owner/api/clients/{cid}/metrics/csv",
                        content=('{"csv":%s,"period_start":%s}' % (repr(csv_text).replace("'", '"'), ps)).encode(),
                        headers={"content-type": "application/json"})
        assert r.status_code == 422, (ps, r.status_code)
    last_week = time.time() - 7 * 86400
    r = client.post(f"/t/owner/api/clients/{cid}/metrics", json={"metrics": {"leads": 3}, "period_start": last_week})
    assert r.status_code == 200 and r.json()["period_start"] <= last_week
    assert client.get(f"/t/owner/api/clients/{cid}").status_code == 200


# =============================================================================== ad account: owner only
def test_only_the_owner_links_a_clients_ad_account(client, tenant):
    auth.create_user(tenant, "stu_ads", "pw-test-123456", "student")
    login(client, username="stu_ads", password="pw-test-123456")
    cid = client.post("/t/owner/api/clients", json={"name": "Student Co", "onboard": False}).json()["id"]
    # a student pointing their client at someone else's (e.g. the owner's allowlisted) account is refused
    r = client.put(f"/t/owner/api/clients/{cid}", json={"access": {"ad_account_id": "act_practice"}})
    assert r.status_code == 403 and "owner" in r.json()["detail"]
    assert not (client_ops.get_client(tenant, cid)["access"] or {}).get("ad_account_id")
    # the rest of the access form still saves, and an empty ad account field is not a change
    r = client.put(f"/t/owner/api/clients/{cid}", json={"access": {"ad_account_id": "", "crm": "their pipeline tool"}})
    assert r.status_code == 200 and r.json()["access"]["crm"] == "their pipeline tool"
    weekly = client.post(f"/t/owner/api/clients/{cid}/weekly")
    assert weekly.status_code == 200 and weekly.json()["proposals"] == []
    # the owner links it; the student can save the form again with the same value but not change it
    client_ops.update(tenant, cid, {"access": {"ad_account_id": "act_student_client"}}, "owner(owner)", "owner")
    assert client.put(f"/t/owner/api/clients/{cid}", json={"access": {"ad_account_id": "act_student_client",
                                                                       "notes": "granted on the call"}}).status_code == 200
    assert client.put(f"/t/owner/api/clients/{cid}", json={"access": {"ad_account_id": "act_other"}}).status_code == 403
    assert client_ops.get_client(tenant, cid)["access"]["ad_account_id"] == "act_student_client"
    with pytest.raises(client_ops.ClientOpsError) as e:
        client_ops.update(tenant, cid, {"access": {"ad_account_id": "act_x"}}, "stu_ads(student)", "student")
    assert e.value.status == 403


# =============================================================================== intake secrets
@pytest.mark.parametrize("bad", [
    {"offer": "x", "audience": "y", "proof": "Our ads token is EAAB1234567890abcdefghij, results below."},
    {"offer": "x", "audience": "y", "assets": ["https://drive.example.com/f?access_token=abcdef1234567"]},
])
def test_intake_refuses_secrets_in_proof_and_links(client, tenant, bad):
    with tenant.db() as c:
        cid = c.execute("INSERT INTO clients(name,niche,offer,price,metrics,demo,created) VALUES(?,?,?,?,?,?,?)",
                        ("Secret Co", "", "", 1000, "{}", 0, db.now())).lastrowid
    client_ops.start_onboarding(tenant, cid, run_fulfilment=False)
    tok = client_ops.get_client(tenant, cid)["intake_token"]
    r = client.post(f"/c/owner/{tok}", json=bad)
    assert r.status_code == 422 and "token" in r.json()["detail"]
    assert client_ops.get_client(tenant, cid)["intake_at"] is None


# =============================================================================== registrant names in emails
@pytest.mark.parametrize("name,first", [
    ("http://evil.example/login now", "there"), ("evil.example", "there"), ("www.win-now.example", "there"),
    ("Ana Lee", "Ana"), ("Jean-Luc Picard", "Jean-Luc"), ("O'Neil Smith", "O'Neil"), ("Zoë Brandt", "Zoë"),
    ("R2D2 Unit", "there"), ("<b>Bold</b>", "there"), ("J. Doe", "J."),
])
def test_public_names_cannot_put_links_in_auto_approved_emails(tenant, name, first):
    w = _webinar(tenant)
    queue.approve(tenant, _tpl_qid(w, "confirm"), "owner(owner)")
    webinars.register(tenant, w["slug"], name, "victim@x.example.com", True, ip="9.9.9.9")
    it = _emails(tenant, "confirm")[0]
    assert it["body"].startswith(f"Hi {first},")
    assert "evil" not in it["body"] and "win-now" not in it["body"] and "evil" not in it["title"]


def test_setter_followup_uses_a_safe_first_name(tenant):
    w = _webinar(tenant, starts_in=-3 * 3600)
    with tenant.db() as c:
        c.execute("INSERT INTO webinar_registrants(webinar_id,name,email,source,consent,registered_at,attended,"
                  "stayed_past_offer,token) VALUES(?,?,?,?,?,?,?,?,?)",
                  (w["id"], "bit.ly/claim-prize Now", "p@x.example.com", "email", 1, time.time() - 86400, 1, 1,
                   "tok-prize-123456789"))
    res = webinars.post_event(tenant, w["id"], "owner")
    fu = queue.get(tenant, res["setter_drafts"][0])
    assert fu["body"].startswith("Hi there,") and "bit.ly" not in fu["body"] and "bit.ly" not in fu["title"]


# =============================================================================== malformed input
def test_attendance_csv_with_a_huge_cell_is_422(client, tenant):
    login(client)
    w = _webinar(tenant, starts_in=-3 * 3600)
    big = 'Email,Name\na@x.example.com,"' + "x" * 200_000 + '"\n'
    r = client.post(f"/t/owner/api/webinars/{w['id']}/attendance", json={"csv": big})
    assert r.status_code == 422 and "CSV" in r.json()["detail"]


def test_unknown_webinar_chain_is_404(client, tenant):
    login(client)
    r = client.post("/t/owner/api/webinars", json={"title": "Has a title", "starts_at": "2026-12-03T19:00",
                                                   "tz": "Europe/London", "chain_id": "no-such-chain"})
    assert r.status_code == 404
    assert client.get("/t/owner/api/webinars").json()["items"] == []


# =============================================================================== timed sends without the delivery feature
def test_timed_emails_wait_until_due_when_the_queue_cannot_hold_them(tenant):
    assert webinars.queue_holds_timed() is False  # this branch's queue sends on approval
    w = _webinar(tenant, starts_in=3 * 86400)
    for i in range(2):
        webinars.register(tenant, w["slug"], f"Person {i}", f"p{i}@x.example.com", True, ip=f"10.0.0.{i}")
    for key in ("confirm", "day_before", "hour_before", "live"):
        queue.approve(tenant, _tpl_qid(w, key), "owner(owner)")
    # the confirmation is due now; the reminders must not exist (let alone be sent) days early
    assert {i["meta"]["template_key"] for i in _emails(tenant)} == {"confirm"}
    assert _sent(tenant) == 2
    d = webinars.detail(tenant, w["id"])
    tpl = {t["key"]: t for t in d["templates"]}
    assert tpl["day_before"]["fanned_out"] == 2 and tpl["day_before"]["waiting"] == 2 and tpl["confirm"]["waiting"] == 0
    assert webinars.tick(tenant, now=time.time())["released"] == []
    # unsubscribing drops that person's held reminders
    regs = webinars.registrants(tenant, w["id"])
    gone = next(r for r in regs if r["email"] == "p1@x.example.com")
    webinars.unsubscribe(tenant, w["slug"], _token(tenant, gone["id"]))
    day_before = w["starts_at"] - 86400
    released = webinars.tick(tenant, now=day_before + 5)["released"]
    items = _emails(tenant, "day_before")
    assert len(released) == 1 == len(items)
    it = items[0]
    assert it["recipient_ref"] == "p0@x.example.com" and it["status"] == "approved"
    assert it["meta"]["send_after"] == pytest.approx(day_before) and "via approved template" in it["decided_by"]
    assert webinars.tick(tenant, now=day_before + 10)["released"] == []  # once only
    # a reminder whose moment passed hours ago (the server was down) is dropped, not sent late
    assert webinars.tick(tenant, now=w["starts_at"] + 3 * 3600)["released"] == []
    assert _emails(tenant, "hour_before") == [] and _emails(tenant, "live") == []
    with tenant.db() as c:
        assert c.execute("SELECT COUNT(*) FROM webinar_sends WHERE queue_id IS NULL").fetchone()[0] == 0


def test_cancelling_drops_held_emails(tenant):
    w = _webinar(tenant)
    webinars.register(tenant, w["slug"], "Cara Cancel", "cara@x.example.com", True, ip="10.1.0.1")
    queue.approve(tenant, _tpl_qid(w, "day_before"), "owner(owner)")
    webinars.update(tenant, w["id"], {"status": "cancelled"}, "owner")
    assert webinars.tick(tenant, now=w["starts_at"] - 86400 + 5)["released"] == []
    assert _emails(tenant) == []


def test_moving_the_webinar_redrafts_emails_that_have_not_gone_out(tenant):
    settings.update_from_api(tenant, "banned_names", ["Quillon"], "owner")
    w = _webinar(tenant, starts_in=3 * 86400)
    webinars.register(tenant, w["slug"], "Maya Move", "maya@x.example.com", True, ip="10.2.0.1")
    webinars.register(tenant, w["slug"], "Quillon Held", "q@x.example.com", True, ip="10.2.0.2")  # her email is held
    queue.approve(tenant, _tpl_qid(w, "confirm"), "owner(owner)")
    queue.approve(tenant, _tpl_qid(w, "day_before"), "owner(owner)")
    sent_confirm = next(i for i in _emails(tenant, "confirm") if i["recipient_ref"] == "maya@x.example.com")
    held = next(i for i in _emails(tenant, "confirm") if i["recipient_ref"] == "q@x.example.com")
    assert sent_confirm["status"] == "approved" and held["status"] == "held"
    new_start = w["starts_at"] + 86400
    webinars.update(tenant, w["id"], {"starts_at": new_start, "join_url": "https://zoom.example.com/j/NEW"}, "owner")
    # the email already sent is left alone; the held one is withdrawn and redrafted with the new link
    assert queue.get(tenant, sent_confirm["id"])["status"] == "approved"
    assert queue.get(tenant, held["id"])["status"] == "rejected"
    redrafted = [i for i in _emails(tenant, "confirm") if i["recipient_ref"] == "q@x.example.com" and i["id"] != held["id"]]
    assert len(redrafted) == 1 and "j/NEW" in redrafted[0]["body"]
    # held reminders follow the new time
    assert webinars.tick(tenant, now=w["starts_at"] - 86400 + 5)["released"] == []
    webinars.tick(tenant, now=new_start - 86400 + 5)
    rem = next(i for i in _emails(tenant, "day_before") if i["recipient_ref"] == "maya@x.example.com")
    assert rem["meta"]["send_after"] == pytest.approx(new_start - 86400) and "j/NEW" in rem["body"]
    assert webinars.when_label(new_start, "Europe/London").split(" (")[0] in rem["body"]


# =============================================================================== unsubscribe and compliance
def test_unsubscribe_withdraws_items_waiting_for_a_manual_send(client, tenant, monkeypatch):
    monkeypatch.setattr(webinars, "queue_holds_timed", lambda: True)  # as with the delivery feature
    w = _webinar(tenant)
    webinars.register(tenant, w["slug"], "Wes Wait", "wes@x.example.com", True, ip="10.3.0.1")
    queue.approve(tenant, _tpl_qid(w, "day_before"), "owner(owner)")
    it = _emails(tenant, "day_before")[0]
    with tenant.db() as c:  # what the delivery feature does in a workspace with no live email sender
        c.execute("UPDATE queue SET status='awaiting_manual_send' WHERE id=?", (it["id"],))
    reg = webinars.registrants(tenant, w["id"])[0]
    r = client.post(f"/w/owner/{w['slug']}/unsubscribe", content=f"r={_token(tenant, reg['id'])}",
                    headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 200
    assert queue.get(tenant, it["id"])["status"] not in ("awaiting_manual_send", "approved", "scheduled")


def test_every_registrant_email_keeps_a_complete_unsubscribe_line(tenant, monkeypatch):
    w = _webinar(tenant)
    webinars.register(tenant, w["slug"], "Una Line", "una@x.example.com", True, ip="10.4.0.1")
    qid = _tpl_qid(w, "confirm")
    body = queue.get(tenant, qid)["body"]
    queue.edit(tenant, qid, "\n".join(l for l in body.splitlines() if "unsubscribe" not in l.lower()), "owner")
    queue.approve(tenant, qid, "owner(owner)")
    it = _emails(tenant, "confirm")[0]
    assert "Unsubscribe: [your workspace address]/w/owner/" in it["body"] and "/unsubscribe?r=" in it["body"]
    monkeypatch.setenv("OPERATOR_PUBLIC_URL", "https://app.example.com")
    webinars.register(tenant, w["slug"], "Una Two", "una2@x.example.com", True, ip="10.4.0.2")
    it2 = next(i for i in _emails(tenant, "confirm") if i["recipient_ref"] == "una2@x.example.com")
    assert "Unsubscribe: https://app.example.com/w/owner/" in it2["body"]


# =============================================================================== late attendance
def test_attendance_after_the_follow_up_still_reaches_people_who_stayed(tenant, monkeypatch):
    monkeypatch.setattr(webinars, "queue_holds_timed", lambda: True)
    w = _webinar(tenant, starts_in=-4 * 3600)  # ended three hours ago
    with tenant.db() as c:
        for n, e in (("Stay Late", "stay@x.example.com"), ("No Show", "ns@x.example.com")):
            c.execute("INSERT INTO webinar_registrants(webinar_id,name,email,source,consent,registered_at,token) "
                      "VALUES(?,?,?,?,?,?,?)", (w["id"], n, e, "email", 1, time.time() - 86400, f"tok-{e[:4]}-1234567"))
    queue.approve(tenant, _tpl_qid(w, "replay_2"), "owner(owner)")  # due a day after the end: not sent yet
    webinars.tick(tenant)  # no attendance yet: everyone is treated as a no-show
    assert webinars.get(tenant, w["id"])["status"] == "done"
    replays = {i["recipient_ref"]: i for i in _emails(tenant, "replay_2")}
    assert set(replays) == {"stay@x.example.com", "ns@x.example.com"}
    with tenant.db() as c:  # still waiting to go out (held by the delivery feature)
        c.execute("UPDATE queue SET status='scheduled' WHERE id=?", (replays["stay@x.example.com"]["id"],))
    res = webinars.import_attendance(tenant, w["id"], "Email,Duration (Minutes)\nstay@x.example.com,58\n", "owner")
    drafts = res["post_event"]["setter_drafts"]
    assert len(drafts) == 1 and queue.get(tenant, drafts[0])["recipient_ref"] == "stay@x.example.com"
    assert queue.get(tenant, replays["stay@x.example.com"]["id"])["status"] == "rejected"
    assert queue.get(tenant, replays["ns@x.example.com"]["id"])["status"] == "approved"
    again = webinars.post_event(tenant, w["id"], "owner")
    assert again["setter_drafts"] == []  # one follow-up per person


# =============================================================================== Meta checks against a fake Graph
@pytest.fixture
def graph(monkeypatch, tenant):
    import httpx

    from operator_ai import vault
    from operator_ai.live import net

    state = {"account": "visible", "calls": [], "stray": []}

    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        state["calls"].append((request.method, url.path, dict(url.params)))
        if url.host != "graph.facebook.com":
            state["stray"].append(str(url))
            return httpx.Response(599, json={"error": "stray"})
        if request.method != "GET":
            return httpx.Response(400, json={"error": {"code": 1, "message": "no writes in this test"}})
        if url.path == "/v21.0/act_777":
            if state["account"] == "visible":
                return httpx.Response(200, json={"id": "act_777", "name": "Client ads", "account_status": 1})
            code = 190 if state["account"] == "bad_login" else 100
            return httpx.Response(400, json={"error": {"code": code, "message": "nope"}})
        if url.path == "/v21.0/act_777/ads":
            return httpx.Response(200, json={"data": [{"id": "ad1", "name": "Hook A", "status": "ACTIVE", "insights": {
                "data": [{"spend": "120.5", "impressions": "4000", "clicks": "60",
                          "actions": [{"action_type": "lead", "value": "6"}]}]}}]})
        return httpx.Response(404, json={"error": {"code": 803, "message": "unknown"}})

    net.set_transport(httpx.MockTransport(handler))
    monkeypatch.setattr(net, "_sleep", lambda s: None)
    vault.put(tenant, {"meta_app_secret": "app-secret-test", "meta_access_token": "EAAfaketokenclientops12345"})
    settings.set_box(tenant, "live_meta_enabled", True, "test: owner yes recorded")
    settings.set_box(tenant, "meta_allowlist", ["act_777"], "test: owner yes recorded")
    yield state
    net.set_transport(None)
    settings.set_box(tenant, "live_meta_enabled", False, "")
    assert state["stray"] == []


def test_partner_access_check_and_meta_numbers_against_a_fake_graph(tenant, graph):
    with tenant.db() as c:
        cid = c.execute("INSERT INTO clients(name,niche,offer,price,metrics,demo,created,email) VALUES(?,?,?,?,?,?,?,?)",
                        ("Graph Co", "online fitness coaches", "coaching", 1800, "{}", 0, db.now(),
                         "hi@graph.example.com")).lastrowid
    client_ops.start_onboarding(tenant, cid, run_fulfilment=False)
    client_ops.update(tenant, cid, {"access": {"ad_account_id": "act_777"}}, "owner(owner)")
    # pixel / CAPI has no automatic check: seeing the ad account proves nothing about the pixel
    assert client_ops.verify_task(tenant, cid, "pixel_capi", "owner")["verified"] is False
    assert not [t for t in client_ops.tasks(tenant, cid) if t["key"] == "pixel_capi" and t["live_check"]]
    graph["account"] = "hidden"
    r = client_ops.verify_task(tenant, cid, "meta_partner_access", "owner")
    assert r["verified"] is False and "isn't visible" in r["reason"]
    graph["account"] = "bad_login"
    r = client_ops.verify_task(tenant, cid, "meta_partner_access", "owner")
    assert r["verified"] is False and "didn't go through" in r["reason"] and "EAA" not in r["reason"]
    graph["account"] = "visible"
    r = client_ops.verify_task(tenant, cid, "meta_partner_access", "owner")
    assert r["verified"] is True
    item = next(t for t in client_ops.tasks(tenant, cid) if t["key"] == "meta_partner_access")
    assert item["status"] == "done" and item["verified_by"] == "live"
    # the weekly loop reads the last 7 days through the read API, records them and never writes to the account
    w = client_ops.weekly_ops(tenant, cid)
    assert w["source"] == "meta" and w["latest"]["ad_spend"] == 120.5 and w["latest"]["leads"] == 6
    reads = [p for m, path, p in graph["calls"] if path.endswith("/ads")]
    assert any("insights.date_preset(last_7d)" in p.get("fields", "") for p in reads)
    assert all(m == "GET" for m, _, _ in graph["calls"])
    rep = queue.get(tenant, w["report_queue_id"])
    assert "Meta Ads read connection" in rep["body"] and rep["status"] in queue.OPEN
    assert client_ops.history(tenant, cid)[-1]["source"] == "meta"
