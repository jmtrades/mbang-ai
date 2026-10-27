import time

import pytest

from operator_ai import calendar, db, guardrails, jobs, queue, workers
from operator_ai.agents import accountability, fulfilment, onboarding, sales


def _lead(tenant, answers, source="referral", demo=1):
    with tenant.db() as c:
        return c.execute("INSERT INTO leads(name,handle,email,source,answers,stage,created,demo,anon_id) VALUES(?,?,?,?,?,?,?,?,?)",
                         ("Test Lead (DEMO)", "@test_demo", "test.lead@example.invalid", source, db.jdump(answers), "new",
                          db.now(), demo, "L-x")).lastrowid


HOT = {"monthly_revenue": "100k+", "can_invest": "yes", "timeline": "now", "decision_maker": True, "goal": "fix show rate",
       "business": "Peak Physio (DEMO)"}
COLD = {"monthly_revenue": "<10k", "can_invest": "no", "timeline": "later", "decision_maker": False,
        "business": "Cedar Books (DEMO)"}


# ---------------------------------------------------------------- scoring
def test_transparent_scoring():
    w = sales.DEFAULT_WEIGHTS
    hot = sales.score(HOT, "referral", w)
    assert hot["tier"] == "hot" and hot["route"] == "closer" and hot["score"] == 100
    assert sum(f["points"] for f in hot["factors"]) >= 100
    cold = sales.score(COLD, "instagram_dm", w)
    assert cold["tier"] == "cold" and cold["route"] == "nurture"
    warm = sales.score({**COLD, "can_invest": "yes", "timeline": "now"}, "referral", w)
    assert warm["tier"] == "warm" and warm["route"] == "setter"


def test_owner_editable_weights(tenant):
    tenant.set("lead_weights", {"source": {"referral": 60}})
    assert sales.score(COLD, "referral", sales.weights(tenant))["tier"] == "warm"


def test_reply_line_follows_money_rule(tenant):
    from operator_ai import claims

    lead = {"answers": HOT}
    # no proof record (the default): even a hot lead gets the no-figure line
    assert sales.reply_line(lead, "hot", tenant).startswith("Let me help") and "$" not in sales.reply_line(lead, "hot", tenant)
    rec = claims.add(tenant, "outreach_range", "Typical results are much lower; this is the best quartile of clients.",
                     90, "owner")
    claims.add_evidence(tenant, rec["id"], "summary.csv", b"month,revenue\n1,1\n", "owner")
    hot_line = sales.reply_line(lead, "hot", tenant)
    assert "$50k-$100k a month" in hot_line and hot_line.startswith("Let me make")
    assert guardrails.check(hot_line, "outreach", {"lead_tier": "hot"}, tenant=tenant) == []
    cold_line = sales.reply_line({"answers": COLD}, "cold", tenant)
    assert "$" not in cold_line and cold_line.startswith("Let me help")


def test_followups_are_value_only():
    for t in sales.followups({"answers": HOT}):
        assert not guardrails.blocked(guardrails.check(t, "outreach", {"lead_tier": "cold"}))


# ---------------------------------------------------------------- full lead processing
def test_process_hot_lead_queues_everything_sends_nothing(tenant):
    # Sales sequencing (2026-10 wave): the whole funnel is no longer queued up front. process_lead queues the reply
    # only; follow-ups are a cadence that starts once the reply is confirmed sent; booking times are offered after
    # a positive reply (fresh), the invite after they pick a time, and the reminder ladder when the invite is approved.
    lid = _lead(tenant, HOT)
    r = sales.process_lead(tenant, lid)
    assert r["tier"] == "hot" and "slots" not in r and r["cadence_id"]
    items = [queue.get(tenant, q) for q in r["queue_ids"]]
    assert [i["kind"] for i in items] == ["setter_reply"] and items[0]["status"] == "pending"
    with tenant.db() as c:
        assert c.execute("SELECT COUNT(*) FROM channel_messages").fetchone()[0] == 0  # nothing delivered
    # positive reply -> booking DM with fresh times; pick -> invite; approve invite -> ladder with send_after
    rep = sales.record_reply(tenant, lid, "Yes, keen to chat")
    assert rep["classification"] == "positive" and len(rep["booking"]["slots"]) == 3
    bdm = queue.get(tenant, rep["booking"]["booking_dm"])
    assert bdm["kind"] == "booking_dm" and bdm["meta"]["slots_valid_until"] > time.time()
    with pytest.raises(sales.BookingError):  # they can only pick from times they were actually sent
        sales.pick_slot(tenant, lid, index=1)
    queue.approve(tenant, bdm["id"], "owner(owner)")
    inv = queue.get(tenant, sales.pick_slot(tenant, lid, index=1)["invite"])
    assert inv["kind"] == "calendar_invite" and inv["meta"]["slot"] == rep["booking"]["slots"][1]
    queue.approve(tenant, inv["id"], "owner(owner)")
    ladder = [i for i in queue.list_items(tenant, kind="reminder") if i["meta"].get("lead_id") == lid]
    assert sorted(i["meta"]["ladder"] for i in ladder) == ["10min", "24h", "2h"]
    assert all(i["meta"]["send_after"] and i["meta"]["cancel_if"] == ["booking_cancelled"] for i in ladder)
    assert all(i["recipient_ref"] == "@test_demo" for i in ladder)


def test_cold_lead_goes_to_nurture_no_booking(tenant):
    r = sales.process_lead(tenant, _lead(tenant, COLD, source="instagram_dm"))
    assert r["route"] == "nurture" and "slots" not in r
    # reply only; the two value follow-ups are drafted by the cadence at their due time (was 3 items up front)
    assert len(r["queue_ids"]) == 1 and r["cadence_id"]


def test_calendar_invite_owner_only_and_creates_local_event(tenant):
    lid = _lead(tenant, HOT)
    sales.process_lead(tenant, lid)
    rep = sales.record_reply(tenant, lid, "Sounds good, send me the times")
    queue.approve(tenant, rep["booking"]["booking_dm"], "owner(owner)")
    inv = queue.get(tenant, sales.pick_slot(tenant, lid, index=0)["invite"])
    with pytest.raises(queue.QueueError):
        queue.approve(tenant, inv["id"], "s", role="setter")
    with pytest.raises(queue.QueueError):
        queue.approve(tenant, inv["id"], "c", role="closer")
    done = queue.approve(tenant, inv["id"], "owner(owner)")
    # real workspace, live calendar off: the call is on the local calendar and the owner sends the invite themself
    # (mock transports are demo-only now; was delivered == "mock:calendar")
    assert done["status"] == "awaiting_manual_send"
    with tenant.db() as c:
        ev = c.execute("SELECT * FROM calendar_events WHERE ext_id=?", (f"local-q{inv['id']}",)).fetchone()
    assert ev is not None


def test_free_slots_skip_busy_and_weekends(tenant):
    slots = calendar.free_slots(tenant, 3)
    assert len(slots) == 3
    for s in slots:
        assert time.localtime(s["start"]).tm_wday < 5 and 9 <= time.localtime(s["start"]).tm_hour < 17
    calendar.add_event(tenant, "busy", slots[0]["start"], slots[0]["end"], [])
    assert calendar.free_slots(tenant, 3)[0]["start"] != slots[0]["start"]


# ---------------------------------------------------------------- burst + job queue
def test_burst_500_zero_failures(tenant):
    out = sales.start_burst(tenant, 500, seed=1)
    t0 = time.time()
    jobs.drain(tenant, timeout=120)
    st = sales.burst_status(tenant, out["batch"])
    assert st["total"] == 500 and st["done"] == 500 and st["failed"] == 0 and st["complete"]
    assert sum(st["tiers"].values()) == 500 and "unscored" not in st["tiers"]
    assert time.time() - t0 < 60
    with tenant.db() as c:
        n = c.execute("SELECT COUNT(*) FROM queue WHERE json_extract(meta,'$.tag')=?", (f"burst:{out['batch']}",)).fetchone()[0]
    assert n == 500


def test_burst_with_worker_threads(tenant):
    workers.start(4)
    try:
        out = sales.start_burst(tenant, 120, seed=2)
        end = time.time() + 60
        while time.time() < end and not sales.burst_status(tenant, out["batch"])["complete"]:
            time.sleep(0.1)
        st = sales.burst_status(tenant, out["batch"])
        assert st["done"] == 120 and st["failed"] == 0
    finally:
        workers.stop()


def test_job_idempotency_backoff_and_orphans(tenant):
    calls = {"n": 0}

    @jobs.handler("test.flaky")
    def flaky(t, p):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("transient sk-abcdefghijklmnop")
        return {"ok": True}

    old = jobs.BACKOFF_BASE
    jobs.BACKOFF_BASE = 0.01
    try:
        a = jobs.enqueue(tenant, "test.flaky", {}, key="k1")
        b = jobs.enqueue(tenant, "test.flaky", {}, key="k1")
        assert a == b
        jobs.drain(tenant, timeout=10)
        with tenant.db() as c:
            r = c.execute("SELECT * FROM jobs WHERE id=?", (a,)).fetchone()
        assert r["status"] == "done" and r["attempts"] == 2 and "sk-abc" not in (r["error"] or "")

        @jobs.handler("test.bad")
        def bad(t, p):
            raise ValueError("nope")

        j = jobs.enqueue(tenant, "test.bad", {})
        jobs.drain(tenant, timeout=10)
        with tenant.db() as c:
            r = c.execute("SELECT * FROM jobs WHERE id=?", (j,)).fetchone()
        assert r["status"] == "failed" and r["attempts"] == 4  # first try + 3 retries
        with tenant.db() as c:
            c.execute("INSERT INTO jobs(key,kind,payload,status,next_at,created,updated) VALUES('o','test.flaky','{}','running',0,0,0)")
        assert jobs.requeue_orphans(tenant) == 1
    finally:
        jobs.BACKOFF_BASE = old


def test_rescue_desk_anonymous(demo_tenant):
    with demo_tenant.db() as c:
        c.execute("UPDATE leads SET created=? WHERE stage IN ('replied','link_sent')", (db.now() - 20 * 86400,))
    items = sales.rescue_desk(demo_tenant)
    assert items and all(set(i) == {"lead_id", "anon_id", "stage", "tier", "days_stalled", "suggestion"} for i in items)
    assert all(i["anon_id"].startswith("L-") for i in items)


# ---------------------------------------------------------------- onboarding
def test_onboarding_flow(demo_tenant):
    r = onboarding.onboard(demo_tenant, {"name": "Rowan Test (DEMO)", "email": "rowan@example.invalid", "niche": "dental",
                                         "usp": "", "clients": 0, "skills": {"ads": 1, "sales": 4}})
    keys = [g["key"] for g in r["gaps"]]
    assert "ads" in keys and "offer" in keys and "dms" in keys
    assert all(g["lessons"] for g in r["gaps"])
    assert set(r["plan"]) == {"30", "60", "90"} and r["coach"]["display"].endswith("(DEMO)")
    assert len(r["tasks"]) == 3 * len(r["gaps"])
    kinds = [queue.get(demo_tenant, q)["kind"] for q in r["queue_ids"]]
    assert kinds == ["welcome", "kickoff"]


# ---------------------------------------------------------------- accountability
def test_risk_signals():
    now = time.time()
    s = {"joined": now - 90 * 86400, "clients": 0}
    cks = [{"at": now - 25 * 86400, "outreach": 80, "booked": 4, "showed": 1, "mood": 2, "wins": ""},
           {"at": now - 20 * 86400, "outreach": 10, "booked": 2, "showed": 0, "mood": 2, "wins": ""}]
    r = accountability.risk(s, cks, now)
    keys = {x["key"] for x in r["signals"]}
    assert keys == {"checkin_gap", "low_outreach", "low_show_rate", "low_mood", "no_clients"} and r["level"] == "high"
    ok = accountability.risk({"joined": now - 10 * 86400, "clients": 1},
                             [{"at": now - 86400, "outreach": 60, "booked": 4, "showed": 3, "mood": 4, "wins": "x"}], now)
    assert ok["level"] == "low"


def test_board_2000_students_fast(demo_tenant):
    accountability.seed_bulk(demo_tenant, 2000)
    times = []
    for _ in range(5):
        t0 = time.time()
        b = accountability.board(demo_tenant)
        times.append(time.time() - t0)
    assert b["total"] >= 2000
    assert sorted(times)[-1] < 1.0  # p95 of 5 runs = max


def test_interventions_queue_nudges_and_escalate(demo_tenant):
    r = accountability.run_interventions(demo_tenant)
    assert r["nudges"] and all(queue.get(demo_tenant, q)["kind"] == "nudge" for q in r["nudges"])
    assert r["citations"] and "per_coach" in r["digest"]
    with demo_tenant.db() as c:
        assert c.execute("SELECT COUNT(*) FROM coach_tasks WHERE kind='escalation'").fetchone()[0] == len(r["tasks"])


def test_retention_star(demo_tenant):
    st = accountability.retention_star(demo_tenant)
    assert len(st["points"]) == 5 and all(0 <= p["value"] <= 1 for p in st["points"])


def test_checkin_api_and_per_coach_list(client):
    from tests.conftest import demo_login

    demo_login(client, "student")
    r = client.post("/t/demo/api/checkin", json={"outreach": 40, "calls_booked": 2, "calls_showed": 2, "mood": 4})
    assert r.status_code == 200
    demo_login(client, "coach")
    b = client.get("/t/demo/api/students").json()
    me = client.get("/t/demo/api/me").json()["user"]["id"]
    assert b["students"] and all(s["coach_id"] == me for s in b["students"])


# ---------------------------------------------------------------- fulfilment
def test_fulfilment_everything_queued(demo_tenant):
    with demo_tenant.db() as c:
        cid = c.execute("SELECT id FROM clients LIMIT 1").fetchone()[0]
    r = fulfilment.fulfil(demo_tenant, cid)
    assert len(r["money_model"]["scenarios"]) == 3 and r["money_model"]["labels"]
    assert all(p["citation"] for p in r["launch_plan"])
    assert len(r["hooks"]["hooks"]) == 5
    items = [queue.get(demo_tenant, q) for q in r["queue_ids"]]
    assert {i["kind"] for i in items} == {"client_report", "ad_hooks", "vsl_outline", "launch_plan"}
    assert all(i["meta"]["client_asset"] for i in items)
    assert "model, not a promise" in r["report"]


def test_hooks_regenerated_once_when_weak(demo_tenant, monkeypatch):
    monkeypatch.setattr(fulfilment, "HOOK_TEMPLATES", ["In today's fast-paced world, businesses can unlock and elevate everything with synergy and leverage."] * 5)
    with demo_tenant.db() as c:
        cid = c.execute("SELECT id FROM clients LIMIT 1").fetchone()[0]
    r = fulfilment.fulfil(demo_tenant, cid)
    assert r["hooks"]["regenerated_once"] is True
