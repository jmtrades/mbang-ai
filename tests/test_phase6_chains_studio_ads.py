import pytest

from operator_ai import actions, brief, db, guardrails, outreach_loop as ol, queue, studio, webinar_chain as wc
from operator_ai.adapters.ads import MockAdsAdapter, adapter_for
from operator_ai.agents import ads_ops


# ---------------------------------------------------------------- brief builder
def test_brief_builder_reports_missing_and_reads_profile(tenant):
    b = brief.build(tenant, {"niche": "dental clinics"})
    assert b["brief"]["niche"] == "dental clinics" and "offer" in b["missing"]
    with tenant.db() as c:
        c.execute("INSERT INTO profiles(scope,data,updated) VALUES('business',?,?)",
                  (db.jdump({"offer": {"name": "Diary Builder", "price": 4000}}), db.now()))
    b2 = brief.build(tenant, {})
    assert b2["brief"]["offer"] == "Diary Builder" and b2["brief"]["price"] == 4000 and "offer" not in b2["missing"]
    assert b2["usp"].startswith("I help ")


# ---------------------------------------------------------------- webinar chain
def test_webinar_chain_produces_linked_queue_items(tenant):
    r = wc.run_chain(tenant, {"niche": "physio clinics"})
    assert len(r["queue_ids"]) == len(wc.STEPS) == 10
    items = queue.list_items(tenant, chain_id=r["chain_id"])
    assert len(items) == 10 and {i["meta"]["step"] for i in items} == set(wc.STEPS)
    script = next(i for i in items if i["meta"]["step"] == "slide_script")
    assert script["body"].count("Segment ") == 7 and "(15 min)" in script["body"]
    reg = next(i for i in items if i["meta"]["step"] == "reg_page")
    assert reg["body"].startswith("<!doctype html>") and "Preview only" in reg["body"]
    handoff = next(i for i in items if i["meta"]["step"] == "sales_handoff")
    assert handoff["meta"]["prospect_facing"] and handoff["status"] == "pending"
    assert all(i["status"] in ("pending", "needs_human") for i in items)


def test_chain_feeds_forward(tenant):
    r = wc.run_chain(tenant, {"niche": "roofing contractors", "pain": "cold calling"})
    st = wc.state(tenant, r["chain_id"])
    title = next(s for s in st["steps"] if s["step"] == "title_hooks")["output"]["data"]["title"]
    script = next(s for s in st["steps"] if s["step"] == "slide_script")["output"]["body"]
    assert title in script and "cold calling" in title


def test_descendants():
    assert set(wc.descendants("slide_script")) == {"run_of_show", "chat_prompts"}
    assert set(wc.descendants("reg_page")) == {"thankyou_page"}
    assert "sales_handoff" in wc.descendants("offer") and len(wc.descendants("offer")) == 9


def test_rerun_only_dependents(tenant):
    r = wc.run_chain(tenant, {})
    before = {s["step"]: s["version"] for s in wc.state(tenant, r["chain_id"])["steps"]}
    rr = wc.rerun_step(tenant, r["chain_id"], "slide_script")
    assert set(rr["rerun"]) == {"slide_script", "run_of_show", "chat_prompts"}
    after = {s["step"]: s for s in wc.state(tenant, r["chain_id"])["steps"]}
    for s in wc.STEPS:
        if s in rr["rerun"]:
            assert after[s]["version"] == rr["version"] > before[s]
        else:
            assert after[s]["version"] == before[s]
    old = queue.get(tenant, r["queue_ids"][wc.STEPS.index("slide_script")])
    assert old["status"] == "rejected"  # superseded draft


def test_rehearsal_scorer(tenant):
    res = wc.rehearsal_score(tenant, "Segment 1: hook (5 min)\nSegment 4: mechanism (30 min)", {"1": 5, "4": 30})
    seg = {s["segment"]: s for s in res["segments"]}
    assert seg[1]["ok"] is True and seg[4]["ok"] is False


# ---------------------------------------------------------------- studio + launch kit
@pytest.mark.parametrize("line", list(studio.LINES))
def test_each_studio_line_queues_reviewed_drafts(tenant, line):
    r = studio.run_line(tenant, line, {})
    assert r["queue_ids"] and r["sent"] == 0
    for d in r["drafts"]:
        item = queue.get(tenant, d["queue_id"])
        assert item["meta"]["studio_line"] == line and "score" in item["meta"]
        assert item["status"] in ("pending", "needs_human", "held")
    with tenant.db() as c:
        assert c.execute("SELECT COUNT(*) FROM channel_messages").fetchone()[0] == 0


def test_studio_outreach_respects_money_rule(tenant):
    r = studio.run_line(tenant, "outreach", {})
    bodies = [queue.get(tenant, q) for q in r["queue_ids"]]
    for it in bodies:
        flags = guardrails.check(it["body"], "outreach", it["meta"])
        assert not guardrails.blocked(flags), (it["body"], flags)


def test_launch_kit_queues_every_line(tenant):
    r = studio.launch_kit(tenant, {"niche": "dental clinics"})
    assert set(r["lines"]) == set(studio.LINES) and r["sent"] == 0
    lines = {queue.get(tenant, q)["meta"]["studio_line"] for q in r["queue_ids"]}
    assert lines == set(studio.LINES)
    assert all(queue.get(tenant, q)["meta"]["kit_id"] == r["kit_id"] for q in r["queue_ids"])


def test_next_best_action_opens_studio_line(tenant):
    from operator_ai import diagnostic

    r = diagnostic.diagnose(tenant, {"dms_sent": 800, "replies": 40, "conversations": 20, "links_sent": 10, "booked": 6,
                                     "showed": 2, "closed": 1, "offer_price": 3000})
    nba = r["next_best_action"]
    out = studio.run_line(tenant, nba["line"], {})
    assert out["line"] == "outreach" and out["queue_ids"]


# ---------------------------------------------------------------- outreach loop
def test_opener_cap_per_sender(demo_tenant):
    r = ol.queue_openers(demo_tenant, sender="demo-setter", limit=12)
    assert len(r["queued"]) == 5 and len(r["deferred"]) == 7
    assert all("daily cap" in d["reason"] for d in r["deferred"])
    for q in r["queued"]:
        it = queue.get(demo_tenant, q)
        assert it["kind"] == "dm_opener" and it["meta"]["prospect_facing"]


def test_opener_approve_goes_to_mock_instagram_and_tracks_stage(demo_tenant):
    r = ol.queue_openers(demo_tenant, sender="demo-setter", limit=1)
    done = queue.approve(demo_tenant, r["queued"][0], "setter", role="setter")
    # Instagram DMs are a manual channel everywhere (no API can send them): approval hands it to the setter and the
    # stage moves only when they confirm the send (was delivered == "mock:instagram" and stage 'sent' on approval).
    assert done["status"] == "awaiting_manual_send"
    pid = done["meta"]["prospect_id"]
    with demo_tenant.db() as c:
        assert c.execute("SELECT stage FROM outreach_practice_prospects WHERE id=?", (pid,)).fetchone()[0] == "queued"
    done = queue.mark_sent(demo_tenant, done["id"], "setter", role="setter")
    assert done["status"] == "sent" and done["delivered"] == "manual:instagram_dm"
    with demo_tenant.db() as c:
        assert c.execute("SELECT stage FROM outreach_practice_prospects WHERE id=?", (pid,)).fetchone()[0] == "sent"
    f = ol.queue_followups(demo_tenant)
    assert f["queued"] and queue.get(demo_tenant, f["queued"][0])["kind"] == "followup"
    ol.record_reply(demo_tenant, pid, "sounds interesting")
    ol.advance(demo_tenant, pid, "booked")
    lab = ol.opener_lab(demo_tenant)
    assert lab and all(0 <= x["ci95"][0] <= x["rate"] <= x["ci95"][1] <= 1 for x in lab)
    assert ol.weekly_funnel(demo_tenant)[-1]["booked"] >= 1
    assert ol.league(demo_tenant)[0]["booked"] >= 1


def test_wilson_interval():
    lo, hi = ol.wilson(10, 100)
    assert lo == pytest.approx(0.0552, abs=1e-3) and hi == pytest.approx(0.1744, abs=1e-3)
    assert ol.wilson(0, 0) == (0.0, 0.0)


def test_goal_calculator_capped():
    r = ol.goal_calculator(30000, 3000)
    assert r["sales_needed"] == 10 and r["capped"] and r["senders_needed"] > 1
    assert r["dms_per_day_capped"] == 5


# ---------------------------------------------------------------- ads ops
def _state(t):
    with t.db() as c:
        return [tuple(r) for r in c.execute("SELECT id,status,daily_budget FROM mock_ads ORDER BY id")]


def test_diagnose_is_read_only(demo_tenant):
    before = _state(demo_tenant)
    r = ads_ops.diagnose(demo_tenant, target_cpl=10)
    assert r["writes"] == 0 and r["adapter"] == "mock" and _state(demo_tenant) == before
    assert r["ads"] and r["citations"][0]["file"] == "sops/thunderdome.txt"


def test_propose_queues_only(demo_tenant):
    with demo_tenant.db() as c:  # make one active ad an obvious kill
        c.execute("UPDATE mock_ads SET metrics=? WHERE id='ad_demo_2_1'",
                  (db.jdump({"spend": 200, "leads": 1, "impressions": 5000, "clicks": 40}),))
    before = _state(demo_tenant)
    r = ads_ops.propose_changes(demo_tenant, target_cpl=10)
    assert r["proposals"] and _state(demo_tenant) == before
    item = queue.get(demo_tenant, r["proposals"][0]["queue_id"])
    assert item["kind"] == "ad_change"
    with pytest.raises(queue.QueueError):
        queue.approve(demo_tenant, item["id"], "s", role="setter")  # owner only
    queue.approve(demo_tenant, item["id"], "owner(owner)")
    assert MockAdsAdapter(demo_tenant).get("ad_demo_2_1")["status"] == "PAUSED"
    hist = actions.history(demo_tenant)
    assert hist[0]["status"] == "done" and hist[0]["prior_state"][0]["status"] == "ACTIVE"
    actions.rollback(demo_tenant, hist[0]["id"], "owner")
    assert MockAdsAdapter(demo_tenant).get("ad_demo_2_1")["status"] == "ACTIVE"


def test_build_test_paused_zero_budget_and_rollback(demo_tenant):
    r = ads_ops.build_test(demo_tenant, "Round 5 (DEMO)", ["Hook A", "Hook B", "Hook C"])
    assert r["status"] == "done"
    created = r["results"][0]["created"]
    ad = MockAdsAdapter(demo_tenant)
    assert len(created) == 5 and all(ad.get(i)["status"] == "PAUSED" and ad.get(i)["daily_budget"] == 0 for i in created)
    actions.rollback(demo_tenant, r["action_id"], "owner")
    assert all(ad.get(i)["status"] == "ARCHIVED" for i in created)


def test_create_forced_back_to_paused_and_failed(demo_tenant, monkeypatch):
    orig = MockAdsAdapter.create

    def sneaky(self, kind, parent, name, data=None, daily_budget=0.0, status="PAUSED"):
        return orig(self, kind, parent, name, data, 50.0, "ACTIVE")  # an API that ignores PAUSED

    monkeypatch.setattr(MockAdsAdapter, "create", sneaky)
    r = ads_ops.build_test(demo_tenant, "Sneaky (DEMO)", ["Hook A"])
    assert r["status"] == "failed"
    ad = MockAdsAdapter(demo_tenant)
    for i in r["results"][0]["created"]:
        assert ad.get(i)["status"] == "PAUSED" and ad.get(i)["daily_budget"] == 0


def test_practice_go_live_four_steps(demo_tenant):
    built = ads_ops.build_test(demo_tenant, "Go-live practice (DEMO)", ["Hook A", "Hook B", "Hook C"])  # 1
    cmp_id = built["results"][0]["created"][0]
    p = ads_ops.propose_go_live(demo_tenant, cmp_id, 30)  # 2
    assert MockAdsAdapter(demo_tenant).get(cmp_id)["status"] == "PAUSED"  # nothing changed before approval
    queue.approve(demo_tenant, p["queue_id"], "owner(owner)")  # 3
    assert MockAdsAdapter(demo_tenant).get(cmp_id)["status"] == "ACTIVE"
    actions.rollback(demo_tenant, p["action_id"], "owner")  # 4
    assert MockAdsAdapter(demo_tenant).get(cmp_id)["status"] == "PAUSED"


def test_no_activate_path_without_approval(demo_tenant):
    with pytest.raises(actions.ActionError):
        actions.run_direct(demo_tenant, "ad_change", {"account": "act_DEMO_practice",
                                                      "ops": [{"op": "activate", "id": "cmp_demo_1", "daily_budget": 999}]},
                           "owner")


def test_real_adapter_needs_switch_and_allowlist(demo_tenant):
    assert adapter_for(demo_tenant, "act_1").name == "mock"
    demo_tenant.set("live_meta_enabled", True)
    assert adapter_for(demo_tenant, "act_1").name == "mock"  # empty allowlist
    demo_tenant.set("meta_allowlist", ["act_2"])
    assert adapter_for(demo_tenant, "act_1").name == "mock"  # not on the allowlist
    demo_tenant.set("live_meta_enabled", False)


def test_reset_practice_account(demo_tenant):
    ads_ops.build_test(demo_tenant, "Temp (DEMO)", ["Hook"])
    ads_ops.reset_practice(demo_tenant)
    ids = [r[0] for r in _state(demo_tenant)]
    assert "act_DEMO_practice" in ids and not any(i.startswith("cam_") for i in ids)


def test_audit_has_actions_and_rollbacks(demo_tenant):
    from operator_ai import audit

    r = ads_ops.build_test(demo_tenant, "Audit (DEMO)", ["Hook"])
    actions.rollback(demo_tenant, r["action_id"], "owner")
    kinds = {a["kind"] for a in audit.recent(demo_tenant, 20)}
    assert {"action", "action.rollback"} <= kinds
