"""Missions: goal-driven, multi-step execution. Approval gate, waits and wakes, events, rules-first re-plans,
escalations, the optional AI planner, the chat goal intent, RBAC and isolation, demo vs real, AI off."""
from __future__ import annotations

import json
import sys
import time
import types

import pytest

from operator_ai import auth, db, events, guardrails, handlers, jobs, llm, queue, settings, tenancy
from operator_ai.agents import router
from operator_ai.missions import ai_planner, engine, evaluator, registry, templates
from tests.conftest import demo_login, login
from tests.fakes import FakeModel

FEATURES = ("prospects", "proposals", "academy", "delivery", "client_ops", "webinars", "escalations")
ANS = {"niche": "fitness coaches", "offer": "Done-for-you DM setting", "price": 2000}


@pytest.fixture(autouse=True)
def bare(monkeypatch):
    """Each test decides which feature modules exist (they're being built in parallel). Other features' guard-rule
    plug-ins are left out so these tests exercise the missions engine with the core guardrails."""
    handlers.load()
    for f in FEATURES:
        monkeypatch.setitem(sys.modules, f"operator_ai.{f}", None)
    monkeypatch.setattr(guardrails, "_plugins", [])
    yield


@pytest.fixture
def clock(monkeypatch):
    now = [time.time()]

    def fake():
        return now[0]

    for mod in (registry, engine, evaluator):
        monkeypatch.setattr(mod, "clock", fake)
    return now


def mk_user(t, role: str, name: str, coach_uid: int | None = None) -> dict:
    uid = auth.create_user(t, name, "pw-correct-horse-1", role)
    if role == "student":
        with t.db() as c:
            sid = c.execute("INSERT INTO students(name,email,coach_id,joined,stage,demo) VALUES(?,?,?,?,?,0)",
                            (name, f"{name}@example.invalid", coach_uid, time.time(), "outreach")).lastrowid
            c.execute("UPDATE users SET student_id=? WHERE id=?", (sid, uid))
    with t.db() as c:
        return auth.user_public(dict(c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()))


def owner_of(t) -> dict:
    with t.db() as c:
        return auth.user_public(dict(c.execute("SELECT * FROM users WHERE role='owner' ORDER BY id LIMIT 1").fetchone()))


def add_prospects(t, uid: int, n: int, start: int = 0, email: bool = False) -> list[int]:
    ids = []
    with t.db() as c:
        for i in range(start, start + n):
            ids.append(c.execute(
                "INSERT INTO prospects(owner_user_id,name,handle,email,niche,stage,fit_score,lawful_basis,do_not_contact,demo,"
                "created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (uid, f"Casey Prospect{i}", None if email else f"@prospect_{uid}_{i}",
                 f"p{uid}.{i}@example.invalid" if email else None, "fitness coaches", "new", 90 - i, "legitimate_interest",
                 0, 0, time.time(), time.time())).lastrowid)
    return ids


def S(m) -> dict:
    return {s["skey"]: s for s in m["steps"]}


def actor(u) -> str:
    return f"{u['username']}({u['role']})"


def approve_all(t, u, qids):
    for q in qids:
        queue.approve(t, q, actor(u), u["role"], user_id=u["id"])


def out_count(t) -> int:
    with t.db() as c:
        return c.execute("SELECT COUNT(*) FROM channel_messages WHERE direction='out'").fetchone()[0]


# =========================================================================== registry & templates
def test_templates_are_valid_dags_over_the_registry():
    assert set(templates.TEMPLATES) == {"first_client", "fill_calendar", "launch_webinar", "fix_bottleneck", "scale_ads",
                                        "client_onboarding", "client_weekly_ops", "student_90_day"}
    for key, tpl in templates.TEMPLATES.items():
        seen = set()
        assert tpl.steps[0]["type"] == "intake", key
        for s in tpl.steps:
            t = registry.get_type(s["type"])
            assert t is not None, (key, s["type"])
            assert all(d in seen for d in s["depends"]), (key, s["skey"])
            for p in s["params"]:
                assert p in t.params, (key, s["skey"], p)
            seen.add(s["skey"])
    for t in registry.REGISTRY.values():
        assert t.label and t.description and t.wait_for in ("none", "approval", "event", "time")
        if t.outbound:
            assert t.wait_for == "approval", t.key  # outbound always waits for approval
    first = [s["skey"] for s in templates.get("first_client").steps]
    for k in ("offer_lesson", "offer_doc", "profile", "prospects", "research", "dm_cert", "openers", "followups", "replies",
              "booking", "prep", "call", "review", "proposal", "win", "onboarding", "day7", "day14", "day30"):
        assert k in first


def test_classify_extract_and_answer_validation():
    assert templates.classify("Get my first client", "student") == ("first_client", True)
    assert templates.classify("book 20 calls this month", "setter")[0] == "fill_calendar"
    assert templates.classify("launch a webinar", "owner")[0] == "launch_webinar"
    assert templates.classify("scale my ads", "student") == ("first_client", False)  # ads plan is owner-only: nearest
    assert templates.classify("something vague", "coach") == ("fix_bottleneck", False)
    assert templates.extract("Book 20 calls for fitness coaches at $3k") == {"calls_target": 20, "price": 3000.0,
                                                                            "niche": "fitness coaches"}
    assert templates.clean_answers({"price": "$2k", "niche": "  dentists  ", "unknown": 5}) == {"price": 2000.0,
                                                                                               "niche": "dentists"}
    for bad in ({"price": "lots"}, {"price": float("nan")}, {"price": float("inf")}, {"price": True}, {"price": -5},
                {"channels": "Carrier pigeon"}, {"event_date": "next week"}, {"client_id": "x"}, {"niche": ["a"]}):
        with pytest.raises(templates.AnswerError):
            templates.clean_answers(bad)
    with pytest.raises(templates.AnswerError):
        templates.clean_answers(["not", "a", "dict"])


def test_targets_are_a_labelled_model_not_a_promise(tenant):
    t = templates.targets_for(tenant, "first_client", {"price": 2000})
    assert t["openers"] > t["replies"] > t["calls_booked"] >= t["calls_showed"] >= 1 and t["wins"] == 1
    assert t["openers_per_day"] <= t["daily_cap"] and "not a promise" in t["summary"]
    cal = templates.targets_for(tenant, "fill_calendar", {"calls_target": 20})
    assert cal["calls_booked"] == 20 and cal["wins"] is None


# =========================================================================== create, AI off, real tenant
def test_create_first_client_real_tenant_ai_off(tenant):
    o = owner_of(tenant)
    before = llm.REQUESTS_SENT
    m = engine.create(tenant, o, "Get my first client", None, {**ANS, "price": "$2k"})
    st = S(m)
    assert m["template"] == "first_client" and m["status"] == "active" and m["demo"] == 0
    assert st["intake"]["status"] == "done" and "not a promise" in st["intake"]["message"]
    assert st["offer_lesson"]["status"] == "skipped" and "Academy isn't set up" in st["offer_lesson"]["message"]
    assert st["dm_cert"]["status"] == "skipped"
    assert st["offer_doc"]["status"] == "waiting_approval"
    item = queue.get(tenant, st["offer_doc"]["queue_ids"][0])
    assert item["kind"] == "mission_draft" and item["status"] == "pending"
    assert item["meta"]["owner_user_id"] == o["id"] and item["meta"]["mission_id"] == m["id"]
    assert st["prospects"]["status"] == "blocked_human" and "Prospects page" in st["prospects"]["message"]
    assert st["day7"]["status"] == "waiting_time" and st["openers"]["status"] == "pending"
    # durable jobs with idempotency keys, and a trace per step run
    with tenant.db() as c:
        keys = [r["key"] for r in c.execute("SELECT key FROM jobs WHERE kind='missions.step'")]
        runs = c.execute("SELECT COUNT(*) FROM runs WHERE agent='missions'").fetchone()[0]
    assert f"mission:{m['id']}:step:{st['intake']['id']}:try:1" in keys and runs >= 5
    assert st["intake"]["run_id"] and out_count(tenant) == 0 and llm.REQUESTS_SENT == before
    # the real workspace has no demo data and no demo label
    assert "(DEMO)" not in json.dumps(m)


def test_missing_answers_block_intake_then_unblock(tenant):
    o = owner_of(tenant)
    m = engine.create(tenant, o, "Win my first client", "first_client", {})
    st = S(m)
    assert st["intake"]["status"] == "blocked_human" and "Answer 3 question(s)" in st["intake"]["message"]
    assert all(s["status"] == "pending" for k, s in st.items() if k not in ("intake",))
    acts = engine.next_actions(tenant, o)
    assert acts[0]["kind"] == "answer" and acts[0]["mission_id"] == m["id"]
    with pytest.raises(templates.AnswerError):
        engine.update_answers(tenant, m["id"], {"price": "a lot"}, "o")
    m = engine.update_answers(tenant, m["id"], ANS, "o")
    assert S(m)["intake"]["status"] == "done" and S(m)["offer_doc"]["status"] == "waiting_approval"
    assert m["context"]["targets"]["openers"] > 0 and m["context"]["icp"]["niche"] == "fitness coaches"


# =========================================================================== end-to-end with the approval gate
def test_first_client_end_to_end_student_approves_own_drafts(tenant, clock):
    coach = mk_user(tenant, "coach", "coach1")
    stu = mk_user(tenant, "student", "stu1", coach_uid=coach["id"])
    pids = add_prospects(tenant, stu["id"], 32)
    m = engine.create(tenant, stu, "Get my first client", None, ANS)
    mid = m["id"]
    st = S(m)
    assert st["prospects"]["status"] == "done" and st["research"]["status"] == "skipped"  # no research feature: honest
    # nothing goes out until the student approves their own offer doc and profile
    assert out_count(tenant) == 0
    approve_all(tenant, stu, st["offer_doc"]["queue_ids"])
    st = S(engine.get(tenant, mid))
    assert st["offer_doc"]["status"] == "done" and st["profile"]["status"] == "waiting_approval"
    approve_all(tenant, stu, st["profile"]["queue_ids"])
    st = S(engine.get(tenant, mid))
    block = st["openers"]["queue_ids"]
    assert st["openers"]["status"] == "waiting_approval" and 0 < len(block) <= 5  # daily cap
    items = [queue.get(tenant, q) for q in block]
    assert all(i["kind"] == "dm_opener" and i["status"] == "pending" and i["meta"]["prospect_facing"] and
               i["meta"]["lead_tier"] == "cold" and i["meta"]["owner_user_id"] == stu["id"] for i in items)
    assert out_count(tenant) == 0  # drafted, not sent
    # another student can't approve these
    other = mk_user(tenant, "student", "stu2")
    with pytest.raises(queue.QueueError) as e:
        queue.approve(tenant, block[0], actor(other), "student", user_id=other["id"])
    assert e.value.status == 403
    approve_all(tenant, stu, block)
    st = S(engine.get(tenant, mid))
    assert st["openers"]["status"] == "waiting_time" and st["openers"]["cycles"] == 1
    assert st["followups"]["status"] in ("waiting_time", "waiting_approval") and st["replies"]["status"] == "waiting_event"
    met = evaluator.metrics(tenant, engine.get(tenant, mid))
    assert met["leading"]["openers_sent"] == len(block) and out_count(tenant) == len(block)
    # day 2: follow-ups drafted for the sent openers, scheduled and cancellable
    clock[0] += 2 * registry.DAY
    engine.tick(tenant)
    st = S(engine.get(tenant, mid))
    fu = [queue.get(tenant, q) for q in st["followups"]["queue_ids"]]
    assert fu and all(f["kind"] == "followup" and f["meta"]["prior_contact"] and f["meta"]["send_after"] and
                      "lead_replied_since" in f["meta"]["cancel_if"] for f in fu)
    # a positive reply wakes the reply step and the booking step drafts two fresh times
    first_pid = queue.get(tenant, block[0])["meta"]["prospect_id"]
    events.emit(tenant, "reply_received", {"party_type": "prospect", "party_id": first_pid, "owner_user_id": stu["id"],
                                           "classification": "positive"})
    st = S(engine.get(tenant, mid))
    assert st["replies"]["cycles"] == 1
    bq = [queue.get(tenant, q) for q in st["booking"]["queue_ids"]]
    assert len(bq) == 1 and bq[0]["kind"] == "booking_dm" and len(bq[0]["meta"]["slots"]) == 2
    assert bq[0]["meta"]["lead_tier"] == "warm" and bq[0]["meta"]["prospect_id"] == first_pid
    approve_all(tenant, stu, [bq[0]["id"]])
    events.emit(tenant, "booking_made", {"party_type": "prospect", "party_id": first_pid, "starts_at": clock[0] + 2 * registry.DAY})
    st = S(engine.get(tenant, mid))
    assert st["prep"]["status"] == "done" and st["prep"]["output"]["brief"]["ask"]
    assert st["call"]["status"] == "blocked_human" and "Take your call" in st["call"]["message"]
    acts = engine.next_actions(tenant, stu)
    assert any(a["kind"] == "call" and a["step_id"] == st["call"]["id"] for a in acts)
    # the call happened: a proposal is drafted (template, client asset) and waits for approval
    engine.mark_done(tenant, mid, st["call"]["id"], actor(stu), outcome="showed")
    st = S(engine.get(tenant, mid))
    assert st["call"]["status"] == "done" and st["proposal"]["status"] == "waiting_approval"
    prop = queue.get(tenant, st["proposal"]["queue_ids"][0])
    assert prop["kind"] == "proposal" and prop["meta"]["client_asset"] and prop["status"] == "pending"
    assert "don't guarantee" in prop["body"] and not guardrails.blocked(prop["flags"])
    approve_all(tenant, stu, [prop["id"]])
    st = S(engine.get(tenant, mid))
    assert st["win"]["status"] == "waiting_event"
    # the prospects feature inserts a client on a win and emits client_won
    with tenant.db() as c:
        cid = c.execute("INSERT INTO clients(student_id,name,niche,offer,price,metrics,demo,created) VALUES(?,?,?,?,?,?,0,?)",
                        (stu.get("student_id"), "Casey Co", "fitness coaches", "DM setting", 2000, "{}", time.time())).lastrowid
    clock[0] += registry.DAY
    events.emit(tenant, "client_won", {"client_id": cid, "prospect_id": first_pid, "owner_user_id": stu["id"]})
    m = engine.get(tenant, mid)
    st = S(m)
    assert m["context"]["won"] and m["context"]["client_id"] == cid
    assert st["win"]["status"] == "done" and st["onboarding"]["status"] == "done"
    assert st["openers"]["status"] == "done" and st["day7"]["status"] == "skipped"
    with tenant.db() as c:
        child = db.row(c.execute("SELECT * FROM missions WHERE parent_id=?", (mid,)).fetchone())
    assert child and child["template"] == "client_onboarding" and child["user_id"] == stu["id"]
    met = evaluator.metrics(tenant, m)
    assert met["lagging"] == {"calls_booked": 1, "calls_showed": 1, "no_shows": 0, "proposals_sent": 1, "wins": 1}
    assert met["leading"]["replies"] == 1 and met["time_to_first_client_days"] >= 3
    assert met["recorded_revenue"] is None  # revenue only ever the user's own recorded figure
    log = [e["kind"] for e in registry.events_of(tenant, mid)]
    assert {"created", "step", "sent", "reply", "booked", "showed", "proposal", "won", "spawned"} <= set(log)
    assert st["review"]["status"] == "skipped" and m["status"] == "done"  # every step finished: the mission is complete
    assert all(s["status"] in ("done", "skipped") for s in m["steps"])


def test_outbound_steps_never_finish_without_approval(tenant):
    o = owner_of(tenant)
    add_prospects(tenant, o["id"], 31)
    m = engine.create(tenant, o, "Get my first client", None, ANS)
    st = S(m)
    with pytest.raises(engine.MissionError) as e:
        engine.mark_done(tenant, m["id"], st["offer_doc"]["id"], "o")
    assert e.value.status == 409 and "queue" in str(e.value)
    approve_all(tenant, o, st["offer_doc"]["queue_ids"])
    approve_all(tenant, o, S(engine.get(tenant, m["id"]))["profile"]["queue_ids"])
    st = S(engine.get(tenant, m["id"]))
    with pytest.raises(engine.MissionError) as e:
        engine.mark_done(tenant, m["id"], st["openers"]["id"], "o")
    assert e.value.status == 409
    # a forged "done" from a step's code is overridden while a draft is still open
    item = queue.get(tenant, st["openers"]["queue_ids"][0])
    engine._apply(tenant, engine.get(tenant, m["id"]), st["openers"], registry.StepResult("done", "x", queue_ids=[item["id"]]), None)
    assert S(engine.get(tenant, m["id"]))["openers"]["status"] == "waiting_approval"
    # skipping is allowed and sends nothing
    before = out_count(tenant)
    engine.skip_step(tenant, m["id"], st["openers"]["id"], "o")
    assert S(engine.get(tenant, m["id"]))["openers"]["status"] == "skipped" and out_count(tenant) == before
    with pytest.raises(engine.MissionError):
        engine.skip_step(tenant, m["id"], st["intake"]["id"], "o")


def test_rejected_draft_needs_retry_which_redrafts(tenant):
    o = owner_of(tenant)
    m = engine.create(tenant, o, "Get my first client", None, ANS)
    q1 = S(m)["offer_doc"]["queue_ids"][0]
    queue.reject(tenant, q1, "o", "owner", "too long")
    st = S(engine.get(tenant, m["id"]))
    assert st["offer_doc"]["status"] == "waiting_approval"  # rejections have no hook: picked up by sync
    acts = engine.next_actions(tenant, o)
    st = S(engine.get(tenant, m["id"]))
    assert st["offer_doc"]["status"] == "blocked_human" and "rejected" in st["offer_doc"]["message"]
    assert any(a["kind"] == "do" and a["step_id"] == st["offer_doc"]["id"] for a in acts)
    engine.retry_step(tenant, m["id"], st["offer_doc"]["id"], "o")
    st = S(engine.get(tenant, m["id"]))
    assert st["offer_doc"]["status"] == "waiting_approval" and len(st["offer_doc"]["queue_ids"]) == 2


def test_guardrails_run_on_generated_drafts(tenant):
    o = owner_of(tenant)
    tenant.set("banned_names", ["Pat Sample"])
    m = engine.create(tenant, o, "Get my first client", None, {**ANS, "offer": "The Pat Sample method"})
    item = queue.get(tenant, S(m)["offer_doc"]["queue_ids"][0])
    assert item["status"] == "held" and any(f["code"] == "banned_name" for f in item["flags"])
    acts = engine.next_actions(tenant, o)
    assert any(a["kind"] == "fix" and "held by the guardrails" in a["title"] for a in acts)
    with pytest.raises(queue.QueueError):
        queue.approve(tenant, item["id"], "o")
    assert "Pat Sample" not in item["body_display"]


def test_openers_respect_dnc_and_prospect_email_channel(tenant):
    o = owner_of(tenant)
    ids = add_prospects(tenant, o["id"], 33)
    add_prospects(tenant, o["id"], 2, start=100, email=True)
    with tenant.db() as c:
        c.execute(f"UPDATE prospects SET do_not_contact=1 WHERE id IN ({','.join(map(str, ids[:3]))})")
        c.execute("UPDATE prospects SET fit_score=200 WHERE handle IS NULL")
    m = engine.create(tenant, o, "Get my first client", None, ANS)
    approve_all(tenant, o, S(m)["offer_doc"]["queue_ids"])
    approve_all(tenant, o, S(engine.get(tenant, m["id"]))["profile"]["queue_ids"])
    st = S(engine.get(tenant, m["id"]))
    items = [queue.get(tenant, q) for q in st["openers"]["queue_ids"]]
    assert {i["meta"]["prospect_id"] for i in items}.isdisjoint(ids[:3])
    emails = [i for i in items if i["channel"] == "email"]
    assert emails and all(i["kind"] == "prospect_email" and i["meta"]["subject"] for i in emails)


# =========================================================================== feature modules when present
def test_certification_gate_with_academy_and_needs_cert(tenant, monkeypatch):
    certified: set = set()
    assigned: list = []
    fake = types.ModuleType("operator_ai.academy")
    fake.is_certified = lambda t, uid, path, level: (uid, path, level) in certified
    fake.assign = lambda t, user_id, path_key, level=None: assigned.append((user_id, path_key)) or 1
    monkeypatch.setitem(sys.modules, "operator_ai.academy", fake)
    o = owner_of(tenant)
    add_prospects(tenant, o["id"], 31)
    m = engine.create(tenant, o, "Get my first client", None, ANS)
    st = S(m)
    assert st["offer_lesson"]["status"] == "waiting_event" and (o["id"], "growth_operator") in assigned
    assert st["offer_doc"]["status"] == "pending"
    certified.add((o["id"], "growth_operator", "L1"))
    events.emit(tenant, "certification_awarded", {"user_id": o["id"], "path_key": "growth_operator", "level": "L1"})
    st = S(engine.get(tenant, m["id"]))
    assert st["offer_lesson"]["status"] == "done" and st["offer_doc"]["status"] == "waiting_approval"
    assert st["dm_cert"]["status"] == "waiting_event"
    # skipping the visible gate doesn't get round it: the opener step itself needs the certification
    engine.skip_step(tenant, m["id"], st["dm_cert"]["id"], "o")
    approve_all(tenant, o, st["offer_doc"]["queue_ids"])
    approve_all(tenant, o, S(engine.get(tenant, m["id"]))["profile"]["queue_ids"])
    st = S(engine.get(tenant, m["id"]))
    assert st["openers"]["status"] == "waiting_event" and "certification first" in st["openers"]["message"]
    assert st["openers"]["queue_ids"] == []
    certified.add((o["id"], "setter", "L1"))
    events.emit(tenant, "certification_awarded", {"user_id": o["id"], "path_key": "setter", "level": "L1"})
    assert S(engine.get(tenant, m["id"]))["openers"]["status"] == "waiting_approval"


def test_prospects_feature_is_used_when_installed(tenant, monkeypatch, clock):
    calls = {"research": [], "draft": []}
    fake = types.ModuleType("operator_ai.prospects")

    def research(t, prospect_id, user=None):
        calls["research"].append(prospect_id)
        with t.db() as c:
            c.execute("UPDATE prospects SET research=?, stage='researched' WHERE id=?",
                      (json.dumps({"facts": [{"claim": "Runs a 6-week programme", "source_url": "https://example.invalid/a"}]}),
                       prospect_id))

    def draft_openers(t, owner_user_id, prospect_ids, mission_id=None):
        calls["draft"].append(list(prospect_ids))
        return {"queue_ids": [queue.add(t, "dm_opener", "Quick question about your 6-week programme: how is it filling?",
                                        channel="instagram_dm", recipient_ref=f"@p{pid}",
                                        meta={"prospect_facing": True, "lead_tier": "cold", "prospect_id": pid,
                                              "owner_user_id": owner_user_id})["id"] for pid in prospect_ids]}

    fake.research, fake.draft_openers = research, draft_openers
    monkeypatch.setitem(sys.modules, "operator_ai.prospects", fake)
    o = owner_of(tenant)
    add_prospects(tenant, o["id"], 30)
    m = engine.create(tenant, o, "Get my first client", None, ANS)
    assert S(m)["research"]["status"] == "done" and len(calls["research"]) == 30
    approve_all(tenant, o, S(m)["offer_doc"]["queue_ids"])
    approve_all(tenant, o, S(engine.get(tenant, m["id"]))["profile"]["queue_ids"])
    st = S(engine.get(tenant, m["id"]))
    assert calls["draft"] and st["openers"]["output"]["source"] == "prospects"
    item = queue.get(tenant, st["openers"]["queue_ids"][0])
    assert item["meta"]["mission_id"] == m["id"] and item["meta"]["step_id"] == st["openers"]["id"]
    # a broken feature function falls back to the rules openers instead of failing the mission
    fake.draft_openers = lambda t, owner_user_id, prospect_ids: 1 / 0
    approve_all(tenant, o, st["openers"]["queue_ids"])
    clock[0] += 2 * registry.DAY
    engine.tick(tenant)
    st = S(engine.get(tenant, m["id"]))
    assert st["openers"]["status"] == "waiting_approval" and st["openers"]["output"]["source"] == "rules"
    assert st["openers"]["output"]["block"] == 2


def test_followups_hand_over_to_delivery_cadence_when_installed(tenant, monkeypatch):
    fake = types.ModuleType("operator_ai.delivery")
    fake.run_cadences = lambda t: 0
    monkeypatch.setitem(sys.modules, "operator_ai.delivery", fake)
    o = owner_of(tenant)
    add_prospects(tenant, o["id"], 30)
    m = engine.create(tenant, o, "Get my first client", None, ANS)
    approve_all(tenant, o, S(m)["offer_doc"]["queue_ids"])
    approve_all(tenant, o, S(engine.get(tenant, m["id"]))["profile"]["queue_ids"])
    approve_all(tenant, o, S(engine.get(tenant, m["id"]))["openers"]["queue_ids"])
    st = S(engine.get(tenant, m["id"]))
    assert st["followups"]["output"]["handled_by"] == "cadence" and st["followups"]["queue_ids"] == []


# =========================================================================== evaluator & re-plans
def _sent(t, mid, n, replies=0):
    for i in range(n):
        registry.add_event(t, mid, "sent", {"kind": "dm_opener", "first_touch": True, "prospect_id": 10_000 + i}, key=f"t:{i}")
    for i in range(replies):
        registry.add_event(t, mid, "reply", {"party_type": "prospect", "party_id": 10_000 + i, "positive": False},
                           key=f"r:{i}")


def test_low_reply_rate_proposes_a_diff_the_user_confirms(tenant):
    o = owner_of(tenant)
    m = engine.create(tenant, o, "Get my first client", None, ANS)
    _sent(tenant, m["id"], 79, replies=1)
    assert evaluator.evaluate(tenant, engine.get(tenant, m["id"]))["findings"] == []  # under 80 sends: no verdict yet
    _sent(tenant, m["id"], 90, replies=2)
    res = evaluator.evaluate(tenant, engine.get(tenant, m["id"]))
    assert [f["rule"] for f in res["findings"]] == ["low_reply_rate"] and len(res["replan_ids"]) == 1
    assert evaluator.evaluate(tenant, engine.get(tenant, m["id"]))["replan_ids"] == []  # not proposed twice
    rid = res["replan_ids"][0]
    with tenant.db() as c:
        diff = json.loads(c.execute("SELECT diff FROM mission_replans WHERE id=?", (rid,)).fetchone()[0])
    assert diff["context"]["opener_move"] == {"from": "question", "to": "resource"}
    assert [a["type"] for a in diff["add"]] == ["research_fit", "human_task"]
    assert engine.get(tenant, m["id"])["context"]["opener_move"] == "question"  # nothing changes until confirmed
    m2 = evaluator.apply_replan(tenant, m["id"], rid, "o")
    assert m2["context"]["opener_move"] == "resource" and m2["context"]["decisions"][-1]["rule"] == "low_reply_rate"
    assert "reresearch1" in S(m2) and "opener_lesson1" in S(m2)
    with pytest.raises(engine.MissionError) as e:
        evaluator.apply_replan(tenant, m["id"], rid, "o")
    assert e.value.status == 409
    with pytest.raises(engine.MissionError):
        evaluator.apply_replan(tenant, m["id"], 99999, "o")


def test_low_show_rate_adds_reminders_and_dismiss(tenant):
    o = owner_of(tenant)
    m = engine.create(tenant, o, "Get my first client", None, ANS)
    registry.add_event(tenant, m["id"], "showed", {})
    for i in range(3):
        registry.add_event(tenant, m["id"], "no_show", {})
    res = evaluator.evaluate(tenant, engine.get(tenant, m["id"]))
    assert [f["rule"] for f in res["findings"]] == ["low_show_rate"]
    evaluator.apply_replan(tenant, m["id"], res["replan_ids"][0], "o", accept=False)
    with tenant.db() as c:
        assert c.execute("SELECT status FROM mission_replans WHERE id=?", (res["replan_ids"][0],)).fetchone()[0] == "dismissed"
    assert "reminders" not in S(engine.get(tenant, m["id"]))


def test_zero_calls_by_day14_escalates_once_with_a_diagnostic(tenant, clock):
    coach = mk_user(tenant, "coach", "coachz")
    stu = mk_user(tenant, "student", "stuz", coach_uid=coach["id"])
    m = engine.create(tenant, stu, "Get my first client", None, ANS, created=clock[0] - 15 * registry.DAY)
    _sent(tenant, m["id"], 40, replies=1)
    engine.tick(tenant, clock[0])
    engine.tick(tenant, clock[0] + 60)
    evaluator.evaluate(tenant, engine.get(tenant, m["id"]))
    esc = registry.events_of(tenant, m["id"], "escalated")
    assert len(esc) == 1 and "No calls booked" in esc[0]["detail"]["message"]
    with tenant.db() as c:
        tasks = db.rows(c.execute("SELECT * FROM coach_tasks WHERE kind='mission_escalation'"))
    assert len(tasks) == 1 and tasks[0]["coach_id"] == coach["id"]
    with tenant.db() as c:
        assert c.execute("SELECT COUNT(*) FROM mission_replans WHERE rule='zero_calls_day14'").fetchone()[0] == 1


def test_escalations_feature_used_when_installed(tenant, monkeypatch):
    raised = []
    fake = types.ModuleType("operator_ai.escalations")
    fake.raise_ = lambda t, kind, severity, detail, assignee_user_id=None, mission_id=None, ref=None: raised.append(
        (kind, mission_id)) or 77
    monkeypatch.setitem(sys.modules, "operator_ai.escalations", fake)
    o = owner_of(tenant)
    m = engine.create(tenant, o, "Get my first client", None, ANS)
    assert evaluator.escalate(tenant, m, "mission_off_track", "x") == 77 and raised == [("mission_off_track", m["id"])]
    assert evaluator.escalate(tenant, m, "mission_off_track", "x") == 77 and len(raised) == 1


def test_checkpoint_runs_on_its_day_and_tick_moves_the_demo_too(demo_tenant, clock):
    from operator_ai import scheduler

    fn = scheduler._resolve("operator_ai.missions:tick")
    assert fn is engine.tick and fn.demo_too is True
    with demo_tenant.db() as c:
        mid = c.execute("SELECT id FROM missions ORDER BY id LIMIT 1").fetchone()[0]
    clock[0] += 3 * registry.DAY
    engine.tick(demo_tenant)
    st = S(engine.get(demo_tenant, mid))
    assert st["day7"]["status"] == "done" and st["day7"]["message"].startswith("Day 7 check")
    with demo_tenant.db() as c:
        assert c.execute("SELECT COUNT(*) FROM mission_metrics WHERE mission_id=?", (mid,)).fetchone()[0] >= 1


# =========================================================================== engine mechanics
def test_job_idempotency_stale_jobs_and_failure_escalation(tenant, monkeypatch, clock):
    o = owner_of(tenant)
    m = engine.create(tenant, o, "Fix my bottleneck", "fix_bottleneck", {"dms_sent": 500, "replies": 10})
    step = S(m)["intake"]
    assert jobs.enqueue(tenant, engine.JOB, {}, key=f"mission:{m['id']}:step:{step['id']}:try:1") is not None
    with tenant.db() as c:
        assert c.execute("SELECT COUNT(*) FROM jobs WHERE key=?", (f"mission:{m['id']}:step:{step['id']}:try:1",)).fetchone()[0] == 1
    assert engine._job(tenant, {"mission_id": m["id"], "step_id": step["id"], "try": 1}) == {"skipped": "stale"}
    # a step that keeps failing is retried with backoff, then failed and escalated
    def boom(sc):
        raise RuntimeError("token sk-ant-secret-123456789 leaked?")

    monkeypatch.setattr(registry.REGISTRY["studio_line"], "run", boom)
    m = engine.create(tenant, o, "Fix my bottleneck", "fix_bottleneck", {"dms_sent": 500, "replies": 10})
    for _ in range(4):
        clock[0] += 3600
        engine.tick(tenant)
    fix = S(engine.get(tenant, m["id"]))["fix"]
    assert fix["status"] == "failed" and fix["fails"] == 3 and "sk-ant-secret" not in fix["message"]
    assert registry.events_of(tenant, m["id"], "escalated")
    assert any(a["kind"] == "retry" for a in engine.next_actions(tenant, o))


def test_pause_resume_cancel(tenant):
    o = owner_of(tenant)
    m = engine.create(tenant, o, "Get my first client", None, ANS)
    engine.set_status(tenant, m["id"], "pause", "o")
    with pytest.raises(engine.MissionError):
        engine.set_status(tenant, m["id"], "pause", "o")
    assert engine.next_actions(tenant, o) == []
    approve_all(tenant, o, S(engine.get(tenant, m["id"]))["offer_doc"]["queue_ids"])
    assert S(engine.get(tenant, m["id"]))["offer_doc"]["status"] == "waiting_approval"  # paused: nothing moves
    engine.set_status(tenant, m["id"], "resume", "o")
    assert S(engine.get(tenant, m["id"]))["offer_doc"]["status"] == "done"
    engine.set_status(tenant, m["id"], "cancel", "o")
    with pytest.raises(engine.MissionError):
        engine.update_answers(tenant, m["id"], {"niche": "x"}, "o")
    assert engine.get(tenant, m["id"])["status"] == "cancelled"


def test_fix_bottleneck_diagnose_fix_and_remeasure(tenant, clock):
    o = owner_of(tenant)
    m = engine.create(tenant, o, "Fix my bottleneck", None, {"dms_sent": 800, "replies": 40, "conversations": 20,
                                                             "links_sent": 10, "booked": 6, "showed": 2, "closed": 1,
                                                             "offer_price": 3000})
    st = S(m)
    assert m["template"] == "fix_bottleneck" and st["diagnose"]["status"] == "done"
    assert m["context"]["diagnosis"]["bottleneck"]["key"] == "reply" and st["diagnose"]["output"]["run_id"]
    assert st["fix"]["status"] == "waiting_approval" and st["fix"]["queue_ids"]
    approve_all(tenant, o, [q for q in st["fix"]["queue_ids"] if queue.get(tenant, q)["status"] == "pending"])
    for q in st["fix"]["queue_ids"]:
        if queue.get(tenant, q)["status"] in queue.OPEN:
            queue.reject(tenant, q, "o")
    engine.sync(tenant, m["id"])
    engine.pump(tenant)
    st = S(engine.get(tenant, m["id"]))
    assert st["remeasure"]["status"] == "waiting_time"
    clock[0] += 15 * registry.DAY
    engine.tick(tenant)
    st = S(engine.get(tenant, m["id"]))
    assert st["remeasure"]["status"] == "blocked_human"
    with pytest.raises(engine.MissionError):
        engine.mark_done(tenant, m["id"], st["remeasure"]["id"], "o", numbers={"dms_sent": float("nan"), "replies": 1})
    with pytest.raises(engine.MissionError):
        engine.mark_done(tenant, m["id"], st["remeasure"]["id"], "o", numbers={"dms_sent": 100})
    m = engine.mark_done(tenant, m["id"], st["remeasure"]["id"], "o", numbers={"dms_sent": 800, "replies": 96})
    st = S(m)
    assert st["remeasure"]["status"] == "done" and "better" in st["remeasure"]["message"]
    assert m["status"] == "done" and m["context"]["what_worked"]


def test_weekly_client_report_and_onboarding_templates(tenant):
    o = owner_of(tenant)
    with tenant.db() as c:
        cid = c.execute("INSERT INTO clients(student_id,name,niche,offer,price,metrics,demo,created) VALUES(?,?,?,?,?,?,0,?)",
                        (None, "Northwind Fitness", "fitness coaches", "DM setting", 2500,
                         json.dumps({"dms_sent": 500, "replies": 20, "booked": 4, "showed": 2}), time.time())).lastrowid
    m = engine.create(tenant, o, "Send my clients a weekly report", "client_weekly_ops", {"client_id": cid, "weekday": "Monday"})
    wk = S(m)["weekly"]
    item = queue.get(tenant, wk["queue_ids"][0])
    assert wk["status"] == "waiting_approval" and item["kind"] == "client_report" and item["meta"]["client_asset"]
    approve_all(tenant, o, wk["queue_ids"])
    wk = S(engine.get(tenant, m["id"]))["weekly"]
    assert wk["status"] == "waiting_time" and time.localtime(wk["due"]).tm_wday == 0
    m2 = engine.create(tenant, o, "Onboard my new client", "client_onboarding", {"client_id": cid})
    plan = S(m2)["plan"]
    assert plan["status"] == "waiting_approval" and len(plan["queue_ids"]) == 4
    with pytest.raises(engine.MissionError) as e:
        engine.create(tenant, o, "Onboard", "client_onboarding", {"client_id": 999999})
    assert e.value.status == 404


def test_launch_webinar_template(tenant, clock):
    o = owner_of(tenant)
    when = time.strftime("%Y-%m-%d", time.localtime(clock[0] + 10 * registry.DAY))
    m = engine.create(tenant, o, "Launch a webinar", None, {"offer": "Growth sprint", "niche": "coaches", "price": 500,
                                                            "event_date": when})
    st = S(m)
    assert m["template"] == "launch_webinar" and st["chain"]["status"] == "waiting_approval" and len(st["chain"]["queue_ids"]) == 10
    for q in st["chain"]["queue_ids"]:
        if queue.get(tenant, q)["status"] in queue.OPEN:
            queue.reject(tenant, q, "o")
    engine.sync(tenant, m["id"])
    engine.pump(tenant)
    st = S(engine.get(tenant, m["id"]))
    assert st["chain"]["status"] == "blocked_human"  # every asset rejected: retry or skip
    engine.skip_step(tenant, m["id"], st["chain"]["id"], "o")
    st = S(engine.get(tenant, m["id"]))
    assert st["schedule"]["status"] == "done" and st["event"]["status"] == "waiting_time"
    clock[0] += 11 * registry.DAY
    engine.tick(tenant)
    assert S(engine.get(tenant, m["id"]))["after"]["status"] == "blocked_human"


def test_student_90_day_and_role_limits(tenant):
    o = owner_of(tenant)
    stu = mk_user(tenant, "student", "stu90")
    with pytest.raises(engine.MissionError) as e:
        engine.create(tenant, stu, "Scale my ads", "scale_ads", {})
    assert e.value.status == 403
    m = engine.create(tenant, stu, "Plan my first 90 days", "student_90_day", {"niche": "coaches"})
    st = S(m)
    assert st["onboard"]["status"] == "skipped" and st["first"]["status"] == "done"
    with tenant.db() as c:
        assert c.execute("SELECT template FROM missions WHERE parent_id=?", (m["id"],)).fetchone()[0] == "first_client"
    m2 = engine.create(tenant, o, "New student onboarding", "student_90_day", {"name": "Robin Learner", "niche": "coaches"})
    assert S(m2)["onboard"]["status"] == "waiting_approval"


# =========================================================================== AI planner (optional)
def test_ai_planner_off_sends_nothing(tenant):
    o = owner_of(tenant)
    m = engine.create(tenant, o, "Get my first client", None, ANS)
    before = llm.REQUESTS_SENT
    assert ai_planner.availability(tenant)["enabled"] is False
    res = ai_planner.propose(tenant, engine.get(tenant, m["id"]))
    assert res["available"] is False and res["replan_id"] is None and llm.REQUESTS_SENT == before
    assert engine.get(tenant, m["id"])["plan_source"] == "rules"


@pytest.fixture
def fake_model(monkeypatch):
    fm = FakeModel()
    llm.set_transport(fm.transport)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key-123456789")
    monkeypatch.setenv("OPERATOR_DISABLE_LLM", "0")
    yield fm
    llm.set_transport(None)


def test_ai_wording_is_used_only_when_it_passes_the_guardrails(tenant, fake_model):
    o = owner_of(tenant)
    settings.set_box(tenant, "ai_token_budget", 5000, "owner approved test budget")
    fake_model.reply = "We guarantee you'll make $10k in a month or your money back."
    m = engine.create(tenant, o, "Get my first client", None, ANS)
    item = queue.get(tenant, S(m)["offer_doc"]["queue_ids"][0])
    assert fake_model.calls and "guarantee" not in item["body"] and item["meta"]["mode"] == "rules"
    assert S(m)["offer_doc"]["output"]["label"].startswith("Rules draft")
    fake_model.reply = "Offer: done-for-you DM setting for fitness coaches. Who it's for: coaches with an audience."
    m2 = engine.create(tenant, o, "Get my first client", None, {**ANS, "niche": "yoga teachers"})
    item2 = queue.get(tenant, S(m2)["offer_doc"]["queue_ids"][0])
    assert item2["meta"]["mode"] == "live" and item2["body"].startswith("Offer: done-for-you")
    with tenant.db() as c:
        assert c.execute("SELECT ai_mode FROM runs WHERE id=?", (S(m2)["offer_doc"]["run_id"],)).fetchone()[0] == "live"


def test_ai_planner_validates_against_registry(tenant, fake_model):
    o = owner_of(tenant)
    m = engine.create(tenant, o, "Get my first client", None, ANS)
    settings.set_box(tenant, "ai_token_budget", 5000, "owner approved test budget")
    fake_model.reply = json.dumps({"steps": [
        {"key": "intake", "type": "intake"},
        {"key": "openers", "type": "daily_openers", "title": "Daily openers", "depends": ["intake"], "params": {"per_day": 3}},
        {"key": "blast", "type": "send_everyone_now", "title": "Send to everyone"},
        {"key": "dm_push", "type": "booking", "title": "We guarantee results in 30 days", "params": {"target": "lots"}},
        {"key": "check", "type": "checkpoint", "params": {"day": 10}, "depends": ["nonexistent"]},
    ]})
    res = ai_planner.propose(tenant, engine.get(tenant, m["id"]), "o")
    assert res["available"] and res["replan_id"] and fake_model.calls
    d = res["diff"]
    added = {a["skey"]: a for a in d["add"]}
    assert set(added) == {"dm_push", "check"}
    assert added["dm_push"]["title"] == "Booking" and added["dm_push"]["params"] == {}  # blocked title, bad param dropped
    assert added["dm_push"]["outbound"] and added["dm_push"]["wait_for"] == "approval"
    assert added["check"]["depends"] == [] and added["check"]["params"] == {"day": 10}
    assert any(r.get("type") == "send_everyone_now" for r in d["rejected"])
    assert "offer_doc" not in d["remove"]  # started steps are never removed
    assert "research" in d["remove"] and "intake" not in d["remove"] and "day7" not in d["remove"]
    m2 = evaluator.apply_replan(tenant, m["id"], res["replan_id"], "o")
    assert m2["plan_source"] == "ai+rules" and S(m2)["dm_push"]["agent"] == "booking"
    # unreadable AI output keeps the template
    fake_model.reply = "not json at all"
    assert ai_planner.propose(tenant, engine.get(tenant, m["id"]))["replan_id"] is None


# =========================================================================== chat router
@pytest.mark.parametrize("text,intent", [
    ("Get my first client", "goal"),
    ("I want to book 20 calls this month", "goal"),
    ("Launch a webinar for my offer", "goal"),
    ("Can you help me get my first client?", "goal"),
    ("How do I get my first client?", "question"),
    ("What are the rules for a Thunder Dome test?", "question"),
    ("Quick one, my impressions are up but cost per lead is rising, what should I change?", "question"),
    ("Hey, impressions dropped and spend went up, is it creative fatigue?", "question"),
    ("I booked 3 calls and 2 showed, how do I improve my close rate?", "question"),
    ("800 DMs, 40 replies, 6 booked, 2 showed", "metrics"),
    ("My reply rate is 4% on 500 DMs, what should I fix?", "metrics"),
    ("Booked 3, showed 2", "metrics"),
    ("Review this DM:\nLet's get you more clients this month. Want a call?", "asset"),
    ("Ad name,Amount spent,Impressions\nA,10,1000\n", "ads_csv"),
    ("Ad name,Amount spent\nA,10\n", "question"),
])
def test_router_intents_and_bug_fixes(text, intent):
    assert router.classify(text) == intent


def test_chat_goal_asks_missing_answers_one_at_a_time_then_starts(client):
    demo_login(client, "student")
    r = client.post("/t/demo/api/chat", json={"text": "Get my first client"}).json()
    assert r["intent"] == "goal" and r["meta"]["mission_draft"]["missing"] == ["niche", "offer", "price"]
    assert "Which niche" in r["answer"] and "approval" in r["answer"] and r["meta"]["label"].startswith("Rules")
    tid = r["thread_id"]
    r = client.post("/t/demo/api/chat", json={"text": "fitness coaches", "thread_id": tid}).json()
    assert r["intent"] == "goal" and r["meta"]["mission_draft"]["missing"] == ["offer", "price"]
    r = client.post("/t/demo/api/chat", json={"text": "Done-for-you DM setting", "thread_id": tid}).json()
    r = client.post("/t/demo/api/chat", json={"text": "lots", "thread_id": tid}).json()
    assert r["meta"]["mission_draft"]["missing"] == ["price"] and "enter a number" in r["answer"]
    r = client.post("/t/demo/api/chat", json={"text": "$2k", "thread_id": tid}).json()
    d = r["meta"]["mission_draft"]
    assert d["missing"] == [] and d["answers"]["price"] == 2000 and "Start this mission" in r["answer"]
    # after the draft is complete, a new question goes back to the coach
    r2 = client.post("/t/demo/api/chat", json={"text": "What are the rules for a Thunder Dome test?", "thread_id": tid}).json()
    assert r2["intent"] == "question"
    m = client.post("/t/demo/api/missions", json={"goal": d["goal"], "template": d["template"], "answers": d["answers"]}).json()
    assert m["template"] == "first_client" and m["answers"]["niche"] == "fitness coaches"
    with tenancy.get("demo").db() as c:
        assert c.execute("SELECT COUNT(*) FROM channel_messages WHERE direction='out'").fetchone()[0] == 0


# =========================================================================== API: RBAC, isolation, malformed input
def test_api_rbac_isolation_and_malformed_input(client):
    login(client)
    t = tenancy.get("owner")
    coach = mk_user(t, "coach", "coachapi")
    a = mk_user(t, "student", "stua", coach_uid=coach["id"])
    b = mk_user(t, "student", "stub")
    setter = mk_user(t, "setter", "setterapi")
    m = engine.create(t, a, "Get my first client", None, ANS)
    mid = m["id"]

    def as_user(u):
        client.cookies.clear()
        r = client.post("/t/owner/api/auth/login", json={"username": u["username"], "password": "pw-correct-horse-1"})
        assert r.status_code == 200

    as_user(b)
    assert client.get(f"/t/owner/api/missions/{mid}").status_code == 404
    assert client.post(f"/t/owner/api/missions/{mid}/pause").status_code == 404
    assert all(x["id"] != mid for x in client.get("/t/owner/api/missions").json()["missions"])
    assert client.get("/t/owner/api/missions?scope=all").status_code == 403
    assert client.get("/t/owner/api/missions/next-actions").json()["items"] == []
    as_user(coach)
    assert [x["id"] for x in client.get("/t/owner/api/missions?scope=students").json()["missions"]] == [mid]
    d = client.get(f"/t/owner/api/missions/{mid}").json()
    assert d["can_act"] is False and d["next_actions"] == []
    assert client.post(f"/t/owner/api/missions/{mid}/pause").status_code == 403
    assert client.post(f"/t/owner/api/missions/{mid}/answers", json={"answers": {"niche": "x"}}).status_code == 403
    as_user(setter)
    assert client.get("/t/owner/api/missions?scope=students").status_code == 403
    assert client.get(f"/t/owner/api/missions/{mid}").status_code == 404
    as_user(a)
    d = client.get(f"/t/owner/api/missions/{mid}").json()
    assert d["can_act"] and d["steps"][0]["status_label"] == "Done" and d["plan_vs_actual"]
    assert {x["kind"] for x in client.get("/t/owner/api/missions/next-actions").json()["items"]} >= {"approve"}
    assert client.get("/t/owner/api/missions/templates").json()["templates"]
    assert all(x["key"] != "scale_ads" for x in client.get("/t/owner/api/missions/templates").json()["templates"])
    # malformed input is 422, never 500
    bad = [
        ("post", "/missions", {"goal": ""}), ("post", "/missions", {"goal": "x", "template": "nope"}),
        ("post", "/missions", {"goal": "x", "template": "BAD-KEY"}), ("post", "/missions", {"goal": "x", "answers": {"price": "lots"}}),
        ("post", "/missions", {"goal": "x", "answers": [1, 2]}), ("post", f"/missions/{mid}/answers", {"answers": {"price": -1}}),
        ("post", f"/missions/{mid}/steps/1/mark-done", {"value": "NaN"}), ("post", f"/missions/{mid}/replan", {"source": "magic"}),
        ("post", f"/missions/{mid}/replan/confirm", {"replan_id": 0}), ("get", "/missions/abc", None),
        ("get", "/missions?scope=everyone", None),
    ]
    for method, path, body in bad:
        r = getattr(client, method)(f"/t/owner/api{path}", **({"json": body} if body is not None else {}))
        assert r.status_code == 422, (path, body, r.status_code, r.text[:200])
    r = client.post(f"/t/owner/api/missions/{mid}/steps/1/mark-done", content=b"{broken", headers={"content-type": "application/json"})
    assert r.status_code == 422
    assert client.get("/t/owner/api/missions/999999").status_code == 404
    assert client.post(f"/t/owner/api/missions/{mid}/steps/999999/skip").status_code == 404
    st = {s["skey"]: s for s in d["steps"]}
    r = client.post(f"/t/owner/api/missions/{mid}/steps/{st['call']['id']}/mark-done", json={"outcome": "teleported"})
    assert r.status_code == 422
    assert client.post(f"/t/owner/api/missions/{mid}/steps/{st['offer_doc']['id']}/mark-done", json={}).status_code == 409
    r = client.post(f"/t/owner/api/missions/{mid}/replan", json={"source": "ai"}).json()
    assert r["available"] is False and "rules template" in r["label"]
    assert client.post(f"/t/owner/api/missions/{mid}/replan", json={}).json()["findings"] == []
    assert client.post(f"/t/owner/api/missions/{mid}/cancel").json()["status"] == "cancelled"
    assert client.post(f"/t/owner/api/missions/{mid}/resume").status_code == 409
    # the owner sees every mission
    client.cookies.clear()
    login(client)
    assert mid in [x["id"] for x in client.get("/t/owner/api/missions?scope=all").json()["missions"]]


def test_empty_workspace_has_no_missions_and_never_seeds(client):
    login(client)
    r = client.get("/t/owner/api/missions").json()
    assert r["missions"] == [] and r["ai"]["enabled"] is False
    assert client.get("/t/owner/api/missions/next-actions").json() == {"items": []}
    from operator_ai.missions import seed_demo

    assert seed_demo(tenancy.get("owner")) is None
    with tenancy.get("owner").db() as c:
        assert c.execute("SELECT COUNT(*) FROM missions").fetchone()[0] == 0


# =========================================================================== demo
def test_demo_seed_mission_midway(client):
    demo_login(client, "student")
    r = client.get("/t/demo/api/missions").json()
    assert len(r["missions"]) == 1
    m = r["missions"][0]
    assert m["demo"] and "(DEMO)" in m["title"] and m["template"] == "first_client"
    d = client.get(f"/t/demo/api/missions/{m['id']}").json()
    st = {s["skey"]: s for s in d["steps"]}
    assert all(st[k]["status"] == "done" for k in ("intake", "offer_doc", "profile", "prospects", "research"))
    assert st["openers"]["status"] == "waiting_approval" and len(st["openers"]["queue_ids"]) == 3
    acts = client.get("/t/demo/api/missions/next-actions").json()["items"]
    assert acts and acts[0]["kind"] == "approve" and acts[0]["demo"]
    q = client.get(f"/t/demo/api/queue?ids={','.join(map(str, st['openers']['queue_ids']))}").json()["items"]
    assert len(q) == 3 and all("(DEMO)" in i["title"] and i["meta"]["owner_user_id"] for i in q)
    assert d["metrics"]["leading"]["openers_sent"] == 5 and d["events"]
    # the student approves their own demo openers; the mission moves on
    for i in q:
        assert client.post(f"/t/demo/api/queue/{i['id']}/approve").status_code == 200
    d = client.get(f"/t/demo/api/missions/{m['id']}").json()
    assert {s["skey"]: s for s in d["steps"]}["openers"]["status"] == "waiting_time"
    # other roles don't see the student's mission as theirs; the owner does with scope=all
    demo_login(client, "setter")
    assert client.get("/t/demo/api/missions").json()["missions"] == []
    assert client.get(f"/t/demo/api/missions/{m['id']}").status_code == 404
    demo_login(client, "owner")
    assert [x["id"] for x in client.get("/t/demo/api/missions?scope=all").json()["missions"]] == [m["id"]]
