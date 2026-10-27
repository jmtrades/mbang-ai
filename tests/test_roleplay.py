"""Role-play simulator: turn-aware scoring (A1), rubric rules as data (A2), the rules-mode state machine (A7),
AI phrasing with post-checks (A8), sessions, sign-off, RBAC, malformed input, demo vs real workspace."""
from __future__ import annotations

import json

import pytest

from operator_ai import audit, auth, db, events, guardrails, llm, rubrics, runtime, settings, tenancy
from operator_ai.roleplay import acts, ai, engine, replies, rubric, scenarios, score, sessions
from tests.conftest import demo_login
from tests.fakes import FakeModel

STUFFED = "goal current tried why now gap offer next step. think about it? what specifically. too expensive, the gap."

GOOD = {
    "cold_dm_coach": [
        "Loved your reel on 20-minute home workouts for new mums. Do those get the most saves?",
        "Makes sense. What would you like your January group to look like?",
        "Got it. Where do most of your clients come from right now?",
        "Let me help Juno Moves Coaching grow through a simple enquiry system. Would a quick call be useful?",
        "Happy to. So I send the right thing, what would be most useful for you to see?",
        "Small groups are exactly where this works best. Shall we grab a time for a quick call on Thursday?",
    ],
    "warm_dm_signal": [
        "Thanks for asking! It depends a bit on your setup. What would you like your tutoring week to look like?",
        "Fair question. It depends on what you need, so I'd rather walk you through it on a quick call. Would that be useful?",
    ],
    "discovery_call": [
        "Thanks for making the time. Love the class clips you post. Before I share anything, what would you like the "
        "studio to look like by the end of spring?",
        "That's a clear goal. Where are the numbers right now, roughly per week?",
        "What have you already tried to fill those evening classes?",
        "And what made you look at this now rather than in six months?",
        "So if I've got this right: you want twenty more regular members by spring, and right now four trial sign-ups a "
        "week turn into two that stick. Is that the gap?",
        "Would it be useful if I showed you how we'd fill the second room? Here's how we work: we follow up every trial "
        "sign-up within the hour and run a simple referral night each month.",
        "Fair. You said the second room sits empty most evenings. How does the cost of staying where you are look next to that?",
        "Shall we book a short call on Thursday to map out the first month?",
    ],
    "objection_price": [
        "Fair. You said you want eight new coaching clients before tax season, and right now you're signing about one a "
        "month. Compared with another tax season like that, how does the cost of staying where you are look?",
        "Shall we book a short call on Friday to map out the first month?",
    ],
    "objection_think": [
        "Totally fair. When you say you want to think about it, what specifically do you want to think through?",
        "That makes sense. Realistically it's about five hours a week, and we can plan around your wedding weekends. "
        "Shall we book a short call next Tuesday to map it out?",
    ],
    "objection_partner": [
        "That makes sense. Shall we book a short call with both of you together this week, so your partner can ask "
        "their own questions?",
    ],
    "objection_agency": [
        "Good to know. What's working well with them, and what would you change?",
        "That's tough. What would you like the course to be doing each month?",
        "Where are the numbers right now, roughly per month?",
        "So if I've got this right: you want forty course sales a month, the ads bring about sixty leads, and half of "
        "them never hear back. Is that the gap?",
        "Would it be useful if I showed you how we'd follow up every lead within the hour, alongside your agency?",
        "Shall we book a short call on Thursday to map it out?",
    ],
    "objection_send_info": [
        "Happy to. So I send the right thing, what would be most useful for you to see?",
        "Small accounts are where this works best. Would a quick call on Thursday be useful?",
    ],
    "not_qualified": [
        "Thanks for coming on. What would you like your coaching practice to look like by the summer?",
        "Where are you right now with clients?",
        "If this turned out to be a fit, how would you fund it from what you have now?",
        "Thanks for being honest. I don't think this is the right time, and I don't want you to put it on a credit card. "
        "Start with our free resources and come back when it feels comfortable.",
    ],
    "client_kickoff": [
        "Great to meet you properly. What would you like the spring cohort to look like?",
        "How many people joined the last cohort?",
        "What have you tried before to fill it?",
        "What made you want to start this now?",
        "So if I've got this right: you want thirty seats in the spring cohort, and last time twelve people joined, "
        "mostly from your email list. Is that the gap?",
        "Here's the plan for the first 30 days: we warm up your email list, add a simple webinar and follow up every registrant.",
        "Honest answer: no one can guarantee results. It depends on the work and your market.",
        "Shall we book our weekly check-in for Mondays at 10?",
    ],
    "webinar_qa": [
        "Great question. Honestly, no one can guarantee results. It depends on the work you put in and your market.",
        "I can't give you a number, and anyone who does is guessing. Results vary with effort and the market.",
        "Realistically, plan for about five to eight hours a week for the first two months.",
        "If that sounds right for you, book a call using the link below and we'll see if it's a fit.",
    ],
    "student_checkin": [
        "Thanks for being straight with me. How many DMs have you sent this month?",
        "No judgement at all. Roughly how many DMs have you sent this month?",
        "What have you been doing day to day?",
        "What happens when you sit down to send them?",
        "That's really common. Let's make it simple: send ten DMs by Friday without rewriting them, and tell me how it "
        "went. Can you commit to that?",
    ],
    "coach_plant_questions": [
        "I can't let you do that. Our rule is real questions from real attendees, never planted ones. Instead, ask the "
        "room to type one word in the chat in the first two minutes.",
    ],
}
PW = "pw-roleplay-test-123"


def mk(t, name: str, role: str, coach_id: int | None = None) -> dict:
    uid = auth.create_user(t, name, PW, role, display=f"{name} display", coach_id=coach_id)
    return {"id": uid, "username": name, "role": role, "display": f"{name} display"}


def play(t, user, key: str, lines: list[str], seed: int = 5, end: bool = True, **kw) -> dict:
    v = sessions.start(t, user, key, seed=seed, **kw)
    for line in lines:
        if v["status"] != "active":
            break
        v = sessions.turn(t, user, v["id"], line)
    if end and v["status"] == "active":
        v = sessions.end(t, user, v["id"])
    return v


def crit(v: dict, key: str) -> dict:
    return next(c for c in v["score"]["criteria"] if c["key"] == key)


def as_user(client, name: str, tid: str = "owner"):
    r = client.post(f"/t/{tid}/api/auth/login", json={"username": name, "password": PW})
    assert r.status_code == 200, r.text


@pytest.fixture
def stu(tenant):
    return mk(tenant, "learner1", "student")


# ======================================================================= A1: turn-aware, order-aware scoring
def test_regression_stuffed_string_scores_under_20():
    # the old keyword rubric gave this nonsense full marks; the turn-aware scorer must not
    assert rubrics.score(STUFFED, "sales_call")["score"] == 100
    res = score.score_text(STUFFED)
    assert res["total"] < 20
    assert res["integrity_flags"] and res["integrity_flags"][0]["code"] == "keyword_stuffing"
    assert acts.is_stuffing(STUFFED)


def test_stuffed_turn_in_a_live_session_scores_under_20(tenant, stu):
    v = play(tenant, stu, "discovery_call", [STUFFED])
    assert v["score"]["total"] < 20
    assert v["score"]["integrity_flags"]
    learner = [t for t in v["turns"] if t["speaker"] == "learner"][0]
    assert learner["color"] == "red"
    assert any("stuffing" in n.lower() for n in learner["notes"])
    # repeating the stuffing across turns doesn't help either
    v2 = play(tenant, stu, "discovery_call", [STUFFED, "goal? current? tried? why now?", STUFFED])
    assert v2["score"]["total"] < 20


def test_stuffing_detector_lets_a_natural_playback_through():
    assert not acts.is_stuffing("So your goal is twenty more members and you're currently at four trials a week: that's the gap.")
    assert not acts.is_stuffing("What's your goal for the studio this spring?")
    assert acts.is_stuffing("goal? current? tried? why now?")


def test_discovery_only_counts_real_questions():
    sc = scenarios.builtin("discovery_call")
    c = acts.classify("Your goal matters. Your current numbers matter. What you tried matters.", sc, {"turn_no": 1})
    assert c["dims"] == []
    c = acts.classify("goal?", sc, {"turn_no": 1})
    assert c["dims"] == [] and "too_short" in c["acts"]
    c = acts.classify("What's your goal for the studio this spring?", sc, {"turn_no": 1})
    assert c["dims"] == ["goal"]


def test_discovery_one_dimension_per_turn(tenant, stu):
    v = play(tenant, stu, "discovery_call", ["What's your goal, where are the numbers currently, what have you tried and why now?"])
    d = crit(v, "discovery")
    assert d["points"] == pytest.approx(d["max"] / 4)
    assert "Missing" in d["evidence"]


def test_discovery_after_the_offer_earns_nothing(tenant, stu):
    v = play(tenant, stu, "discovery_call", [
        "Here's how we work: we follow up every trial sign-up within the hour.",
        "What would you like the studio to look like by spring?",
    ])
    assert crit(v, "discovery")["points"] == 0
    assert crit(v, "offer_order")["points"] == 0


def test_gap_needs_a_fact_the_persona_revealed(tenant, stu):
    sc = scenarios.builtin("discovery_call")
    c = acts.classify("So if I've got this right, you want twenty more regular members. Is that the gap?", sc,
                      {"revealed": [], "turn_no": 1})
    assert "gap_unsupported" in c["acts"] and "gap_playback" not in c["acts"]
    c = acts.classify("So if I've got this right, you want twenty more regular members. Is that the gap?", sc,
                      {"revealed": ["goal"], "turn_no": 2})
    assert "gap_playback" in c["acts"]
    lines = GOOD["discovery_call"]
    v = play(tenant, stu, "discovery_call", ["So the gap is clear: you want more members, right?"] + lines[:5])
    assert crit(v, "gap")["points"] == crit(v, "gap")["max"]
    first = [t for t in v["turns"] if t["speaker"] == "learner"][0]
    assert first["color"] == "amber"


def test_offer_before_the_gap_costs_trust_and_raises_price(tenant, stu):
    s = engine.new_session(scenarios.builtin("discovery_call"), 3)
    t0 = s.st["trust"]
    learner, persona = engine.step(s, "Here's how we work: we run your follow-up and referral nights.", tenant)
    assert ["offer_early", None] in persona["events"]
    assert ["objection", "price"] in persona["events"]
    assert s.st["pending"] == "price"
    assert s.st["trust"] == t0 - 15


def test_objection_credit_only_in_the_next_turn_with_the_matching_move(tenant, stu):
    good = GOOD["objection_price"][0]
    # wrong move first: they repeat it, then the right move earns half
    v = play(tenant, stu, "objection_price", ["Let's just book the next call and figure it out.", good,
                                              GOOD["objection_price"][1]])
    c = crit(v, "objection_price")
    assert c["points"] == pytest.approx(c["max"] / 2)
    # the right words without their facts don't count
    v = play(tenant, stu, "objection_price", ["Think about the cost of staying where you are.", "Okay."])
    assert crit(v, "objection_price")["points"] == 0
    # the right move straight away earns full credit
    v = play(tenant, stu, "objection_price", GOOD["objection_price"])
    c = crit(v, "objection_price")
    assert c["points"] == c["max"] and v["score"]["outcome"] == "won"


def test_unhandled_objection_twice_ends_the_session(tenant, stu):
    v = play(tenant, stu, "objection_think", ["No problem at all.", "Sure, no pressure."])
    assert v["status"] == "complete" and v["score"]["outcome"] == "lost"
    assert crit(v, "objection_think")["points"] == 0
    assert crit(v, "objection_think")["model_line"]


@pytest.mark.parametrize("key", sorted(GOOD))
def test_every_scenario_is_winnable_by_following_the_sop(tenant, stu, key):
    v = play(tenant, stu, key, GOOD[key])
    sc = v["score"]
    assert sc["outcome"] == "won", (key, sc["end_reason"])
    assert sc["compliance_flags"] == 0
    assert sc["total"] >= 70, (key, sc["total"])
    assert sc["passed"]


def test_every_criterion_cites_the_sop_line_it_comes_from(tenant, stu):
    v = play(tenant, stu, "discovery_call", GOOD["discovery_call"][:3])
    sc = v["score"]
    ids = {c["id"] for c in sc["citations"]}
    for c in sc["criteria"]:
        assert c["citations"] and set(c["citations"]) <= ids
        cite = c["cite"]
        assert cite["file"] and cite["lines"]
        assert cite["snippet"], c["key"]
        rule = rubric.rules(tenant)[c["rule_key"]]
        assert any(term.lower() in cite["snippet"].lower() for term in rule["must_contain"] if not term.isdigit())
    assert sc["compliance_citation"] in ids


def test_compliance_gate_scores_zero(tenant, stu):
    cases = [
        ("discovery_call", ["We guarantee you'll fill the second room in a month."], "guarantee"),
        ("discovery_call", ["Honestly, you'll make $10k a month extra from this."], "earnings_promise"),
        ("not_qualified", ["You could just put it on a credit card and pay it off later."], "debt_steer"),
        ("objection_partner", ["You don't need to tell your partner, just decide today."], "hide_from_partner"),
        ("cold_dm_coach", ["Loved your reel on home workouts. Our programme is $3,000 a month, interested?"], "outreach_money"),
    ]
    for key, lines, code in cases:
        v = play(tenant, stu, key, lines)
        sc = v["score"]
        assert v["status"] == "complete" and sc["outcome"] == "lost" and sc["end_reason"] == "compliance", key
        assert sc["total"] == 0 and sc["compliance_flags"] >= 1 and sc["gate"] == "compliance"
        flags = [f["code"] for t in v["turns"] if t["speaker"] == "learner" for f in t["flags"]]
        assert code in flags, (key, flags)
    # the hedged range is still blocked for a cold lead
    v = play(tenant, stu, "cold_dm_coach", GOOD["cold_dm_coach"][:3] + [
        "Let me make Juno Moves Coaching $50k-$100k a month through our system. Would a quick call be useful?"])
    assert v["score"]["gate"] == "compliance"
    assert any(f["code"] == "cold_lead_figure" for t in v["turns"] for f in t.get("flags", []))
    # negation passes: an honest "no guarantee" is fine
    assert not acts.classify("No one can guarantee results.", scenarios.builtin("webinar_qa"), {"turn_no": 1})["blocked"]


def test_discounting_is_penalised(tenant, stu):
    v = play(tenant, stu, "objection_price", ["I can do it for half if that helps.", GOOD["objection_price"][0],
                                              GOOD["objection_price"][1]])
    pen = {p["key"]: p for p in v["score"]["penalties"]}
    assert "no_discount" in pen and pen["no_discount"]["points"] > 0
    assert [t for t in v["turns"] if t["speaker"] == "learner"][0]["color"] == "red"


def test_dm_rules_one_question_short_and_personal(tenant, stu):
    long_q = ("Hi there! I help coaches grow. What are you working on? How many clients do you have? What have you "
              "tried? " + "We do a lot of things for a lot of people and it works really well for most of them. " * 3)
    v = play(tenant, stu, "cold_dm_coach", [long_q, "Hey, how are you?"])
    sc = v["score"]
    assert sc["outcome"] == "lost" and sc["end_reason"] == "ignored"
    assert crit(v, "dm_one_question")["points"] < crit(v, "dm_one_question")["max"]
    assert crit(v, "dm_length")["points"] < crit(v, "dm_length")["max"]
    assert crit(v, "dm_personal")["points"] == 0
    # pitching in the first message gets no reply
    s = engine.new_session(scenarios.builtin("cold_dm_coach"), 2)
    _, persona = engine.step(s, "Loved your reel on home workouts. Let me help you grow, want to book a quick call?", tenant)
    assert persona["events"][0][0] == "pitch_early"


def test_disqualify_wins_and_pushing_unaffordable_loses_trust(tenant, stu):
    lines = GOOD["not_qualified"]
    v = play(tenant, stu, "not_qualified", lines[:3] + ["Great, shall we book a call on Thursday to get you started?"],
             end=False)
    persona = [t for t in v["turns"] if t["speaker"] == "persona"][-1]
    assert "pressure" in persona["text"].lower() or "tight" in persona["text"].lower()
    v = sessions.turn(tenant, stu, v["id"], lines[3])
    assert v["score"]["outcome"] == "won"
    assert crit(v, "decision_check")["points"] == crit(v, "decision_check")["max"]


def test_reproducible_with_a_seed(tenant, stu):
    a = play(tenant, stu, "discovery_call", GOOD["discovery_call"], seed=42)
    b = play(tenant, stu, "discovery_call", GOOD["discovery_call"], seed=42)
    assert [t["text"] for t in a["turns"]] == [t["text"] for t in b["turns"]]
    assert a["score"]["total"] == b["score"]["total"]
    texts = {tuple(t["text"] for t in play(tenant, stu, "discovery_call", GOOD["discovery_call"], seed=s)["turns"])
             for s in range(1, 7)}
    assert len(texts) > 1


def test_reply_bank_and_builtin_scenarios_pass_the_guardrails():
    allowed_subs = {"guarantee", "income", "plant"}
    for key, lines in replies.BANK.items():
        for line in lines:
            fl = guardrails.check(line.replace("{fact}", "x").replace("{concern}", "x").replace("{fact_lc}", "x"), "plain", {})
            if len(key) > 1 and key[1] in allowed_subs:
                continue
            assert not guardrails.blocked(fl), (key, line, fl)
    for s in scenarios.BUILTIN:
        clean = scenarios.validate(s)
        assert clean["key"] == s["key"]
        assert "(DEMO)" not in json.dumps(s)
    assert len(scenarios.BUILTIN) >= 12
    kinds = {s["key"] for s in scenarios.BUILTIN}
    assert {"cold_dm_coach", "warm_dm_signal", "discovery_call", "objection_price", "objection_think", "objection_partner",
            "objection_agency", "objection_send_info", "client_kickoff", "webinar_qa", "student_checkin"} <= kinds


def test_hidden_facts_stay_hidden_until_revealed(tenant, stu):
    v = sessions.start(tenant, stu, "discovery_call", seed=9)
    blob = json.dumps(v).lower()
    for phrase in ("second room", "boosted", "four trial", "twenty more"):
        assert phrase not in blob
    v = sessions.turn(tenant, stu, v["id"], GOOD["discovery_call"][0])
    blob = json.dumps(v).lower()
    assert "twenty more" in blob and "second room" not in blob and "boosted" not in blob
    v = sessions.end(tenant, stu, v["id"])
    reveals = {r["key"]: r for r in v["reveals"]}
    assert reveals["goal"]["revealed"] and not reveals["why_now"]["revealed"]


def test_hints_are_guardrail_clean_and_off_in_exam_mode(tenant, stu):
    for s in scenarios.BUILTIN:
        v = sessions.start(tenant, stu, s["key"], seed=1)
        assert v["hint"], s["key"]
        assert not guardrails.blocked(v["hint_flags"])
        sessions.abandon(tenant, stu, v["id"])
    v = sessions.start(tenant, stu, "discovery_call", seed=5, mode="ai", exam=True, hints=True)
    assert v["exam"] and v["mode"] == "rules" and not v["hints"]
    assert v.get("hint") is None and "meters" not in v
    v = sessions.turn(tenant, stu, v["id"], GOOD["discovery_call"][0])
    assert "acts" not in [t for t in v["turns"] if t["speaker"] == "learner"][0]
    v = sessions.end(tenant, stu, v["id"])
    assert v["score"]["exam"] is True
    assert v["score"]["certifiable"] is False  # default rules aren't confirmed yet, and the score is low
    assert v["score"]["unconfirmed_rules"]


def test_score_follows_the_shared_contract(tenant, stu):
    v = play(tenant, stu, "discovery_call", GOOD["discovery_call"])
    sc = v["score"]
    assert isinstance(sc["total"], int) and 0 <= sc["total"] <= 100
    assert isinstance(sc["compliance_flags"], int)
    assert sc["scenario_key"] == "discovery_call" and sc["mode"] in ("rules", "ai") and sc["seed"] == 5
    assert sc["signoff"] is None
    for c in sc["criteria"]:
        assert {"key", "label", "points", "max", "evidence"} <= set(c)
    with tenant.db() as c:
        row = c.execute("SELECT * FROM roleplays WHERE id=?", (v["id"],)).fetchone()
    assert row["status"] == "complete" and row["total"] == sc["total"]
    assert json.loads(row["score"])["total"] == sc["total"]
    assert json.loads(row["transcript"])[0]["speaker"] == "persona"


# ======================================================================= A2: rubric rules as data
def test_rubric_seeded_from_the_old_values_marked_to_confirm(tenant):
    rules = rubric.rules(tenant)
    assert set(rubric.DEFAULTS) <= set(rules)
    assert all(r["status"] == "default" and r["note"] == "default: confirm against your SOP" for r in rules.values())
    assert rules["dm_length"]["params"]["max_words"] == 60  # rubrics.c_dm_length
    assert rules["objection_think"]["cite_file"] == "course/transcripts/vid_p7q8r9.txt"


def test_validator_flags_rules_whose_cited_lines_drifted(tenant):
    res = {r["rule_key"]: r for r in rubric.validate(tenant)}
    assert res["discovery"]["ok"] and res["objection_think"]["ok"] and res["talk_ratio"]["ok"]
    assert not res["dm_length"]["ok"] and '"60"' in res["dm_length"]["issues"][0]  # the cited DM lesson never says 60
    rubric.update(tenant, "discovery", {"cite_lines": "1"}, "owner(owner)")
    assert not rubric.validate_rule(tenant, rubric.rules(tenant)["discovery"])["ok"]
    rubric.update(tenant, "gap", {"cite_lines": "900-901"}, "owner(owner)")
    assert "no longer exist" in rubric.validate_rule(tenant, rubric.rules(tenant)["gap"])["issues"][0]
    rubric.update(tenant, "offer_order", {"cite_file": "course/missing.txt"}, "owner(owner)")
    assert "isn't in your knowledge base" in rubric.validate_rule(tenant, rubric.rules(tenant)["offer_order"])["issues"][0]
    rubric.update(tenant, "trust", {"cite_file": "playbook:training-science/nope"}, "owner(owner)")
    assert not rubric.validate_rule(tenant, rubric.rules(tenant)["trust"])["ok"]


def test_rubric_on_a_client_workspace_with_no_course_material(env):
    tenancy.ensure_defaults()
    t, _ = tenancy.create("clientco", "Client Co")
    res = {r["rule_key"]: r for r in rubric.validate(t)}
    assert "isn't in your knowledge base" in res["discovery"]["issues"][0]
    assert res["talk_ratio"]["ok"]  # playbook citations ship with the product
    u = mk(t, "clientlearner", "student")
    v = play(t, u, "discovery_call", GOOD["discovery_call"])
    assert v["score"]["outcome"] == "won"
    assert crit(v, "discovery")["cite"]["problem"]


def test_rubric_api_owner_edits_and_scoring_uses_them(client, tenant):
    mk(tenant, "learner2", "student")
    as_user(client, "learner2")
    r = client.get("/t/owner/api/academy/roleplays/rubric")
    assert r.status_code == 200 and r.json()["flagged"] >= 1 and not r.json()["can_edit"]
    assert client.put("/t/owner/api/academy/roleplays/rubric/discovery", json={"weight": 50}).status_code == 403
    assert client.post("/t/owner/api/academy/roleplays/rubric/validate").status_code == 403
    from tests.conftest import login

    login(client)
    r = client.put("/t/owner/api/academy/roleplays/rubric/discovery", json={"weight": 40, "confirmed": True})
    assert r.status_code == 200 and r.json()["rule"]["status"] == "confirmed" and r.json()["check"]["ok"]
    assert client.put("/t/owner/api/academy/roleplays/rubric/nope", json={"weight": 4}).status_code == 404
    assert client.put("/t/owner/api/academy/roleplays/rubric/discovery", json={"weight": 500}).status_code == 422
    assert client.put("/t/owner/api/academy/roleplays/rubric/discovery", json={"cite_lines": "a;b"}).status_code == 422
    r = client.post("/t/owner/api/academy/roleplays/rubric/validate")
    assert r.status_code == 200 and "dm_length" in r.json()["flagged"]
    learner = mk(tenant, "learner3", "student")
    v = play(tenant, learner, "discovery_call", GOOD["discovery_call"])
    d = crit(v, "discovery")
    assert d["max"] == 40 and d["rule_status"] == "confirmed"
    assert "discovery" not in v["score"]["unconfirmed_rules"]


# ======================================================================= A7: scenarios as data, owner editing, test runs
def test_scenario_list_never_exposes_hidden_facts(client, tenant):
    mk(tenant, "learner4", "student")
    as_user(client, "learner4")
    r = client.get("/t/owner/api/academy/scenarios")
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) >= 12 and not body["can_edit"]
    blob = json.dumps(body).lower()
    assert "boosted a few posts" not in blob and "second room opens" not in blob
    for s in body["items"]:
        assert "facts" not in s and "objections" not in s and s["difficulty_label"] in ("Starter", "Standard", "Hard")
        assert s["persona"]["note"].startswith("Fictional")
    one = client.get("/t/owner/api/academy/scenarios/discovery_call").json()
    assert "data" not in one
    assert client.get("/t/owner/api/academy/scenarios/nope_nope").status_code == 404


def test_owner_edits_scenarios_with_validation(client, tenant):
    from tests.conftest import login

    mk(tenant, "learner5", "student")
    as_user(client, "learner5")
    base = scenarios.builtin("discovery_call")
    assert client.put("/t/owner/api/academy/scenarios/discovery_call", json={"data": base}).status_code == 403
    login(client)
    full = client.get("/t/owner/api/academy/scenarios/discovery_call").json()
    assert full["data"]["facts"]["goal"]["say"] and full["builtin"]
    edited = {**base, "title": "Discovery call with a studio owner (edited)"}
    r = client.put("/t/owner/api/academy/scenarios/discovery_call", json={"data": edited})
    assert r.status_code == 200 and r.json()["version"] == 2 and r.json()["source"] == "edited"
    bad = {**base, "persona": {**base["persona"], "temperament": "furious"}}
    r = client.put("/t/owner/api/academy/scenarios/discovery_call", json={"data": bad})
    assert r.status_code == 422 and "temperament" in r.text
    claims = {**base, "opening": ["We guarantee results, by the way."]}
    r = client.put("/t/owner/api/academy/scenarios/discovery_call", json={"data": claims})
    assert r.status_code == 422 and "guardrails" in r.text
    client.put("/t/owner/api/settings", json={"key": "banned_names", "value": ["Maren Okafor"]})
    r = client.put("/t/owner/api/academy/scenarios/discovery_call", json={"data": base})
    assert r.status_code == 422
    client.put("/t/owner/api/settings", json={"key": "banned_names", "value": []})
    custom = {**base, "key": "custom_studio", "title": "My own studio call"}
    r = client.put("/t/owner/api/academy/scenarios/custom_studio", json={"data": custom})
    assert r.status_code == 200 and r.json()["source"] == "custom"
    assert any(s["key"] == "custom_studio" for s in client.get("/t/owner/api/academy/scenarios").json()["items"])
    assert client.post("/t/owner/api/academy/scenarios/custom_studio/reset").status_code == 404
    r = client.post("/t/owner/api/academy/scenarios/discovery_call/reset")
    assert r.status_code == 200
    assert scenarios.get(tenant, "discovery_call")["title"] == base["title"]
    assert client.put("/t/owner/api/academy/scenarios/BAD-KEY", json={"data": base}).status_code == 422
    assert client.put("/t/owner/api/academy/scenarios/x1y", json={"data": "not an object"}).status_code == 422
    for broken in ({"for_roles": 5}, {"concerns": {"think": {"x": 1}}}, {"facts": ["a"]}, {"objections": ["shouting"]},
                   {"persona": "nobody"}, {"patience": True}, {"lines": {"neutral": "not a list"}}, {"trust_start": 500},
                   {"win": "disqualify"}, {"dims": ["mood"]}):
        r = client.put("/t/owner/api/academy/scenarios/discovery_call", json={"data": {**base, **broken}})
        assert r.status_code == 422, (broken, r.status_code)
        assert isinstance(r.json()["detail"], list) and r.json()["detail"][0]["msg"]


def test_test_run_endpoint(client, tenant):
    from tests.conftest import login

    coach = mk(tenant, "coach1", "coach")
    mk(tenant, "learner6", "student")
    as_user(client, "learner6")
    body = {"lines": GOOD["objection_partner"], "seed": 3}
    assert client.post("/t/owner/api/academy/scenarios/objection_partner/test-run", json=body).status_code == 403
    as_user(client, "coach1")
    r = client.post("/t/owner/api/academy/scenarios/objection_partner/test-run", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["score"]["outcome"] == "won" and out["run_id"]
    assert runtime.get(tenant, out["run_id"])["agent"] == "roleplay_test"
    draft = {**scenarios.builtin("objection_partner"), "title": "Draft"}
    assert client.post("/t/owner/api/academy/scenarios/objection_partner/test-run",
                       json={**body, "data": draft}).status_code == 403  # drafts: owner only
    login(client)
    r = client.post("/t/owner/api/academy/scenarios/draft_one/test-run", json={**body, "data": draft})
    assert r.status_code == 200 and r.json()["scenario_key"] == "draft_one"
    r = client.post("/t/owner/api/academy/scenarios/draft_one/test-run", json={**body, "data": {"title": "x"}})
    assert r.status_code == 422
    assert client.post("/t/owner/api/academy/scenarios/objection_partner/test-run",
                       json={"lines": ["x"] * 31}).status_code == 422
    with tenant.db() as c:
        assert c.execute("SELECT COUNT(*) FROM roleplays").fetchone()[0] == 0  # test runs never save a session
    del coach


# ======================================================================= A8: AI mode
@pytest.fixture
def fake_model(monkeypatch):
    fm = FakeModel()
    llm.set_transport(fm.transport)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key-123456789")
    monkeypatch.setenv("OPERATOR_DISABLE_LLM", "0")
    yield fm
    llm.set_transport(None)


def test_ai_mode_with_ai_off_uses_templates_and_says_why(tenant, stu, fake_model):
    assert llm.budget(tenant) == 0
    v = play(tenant, stu, "discovery_call", GOOD["discovery_call"][:2], mode="ai", end=False)
    assert v["mode"] == "ai"
    assert "AI off (budget 0)" in v["mode_label"]
    assert all(t["source"] == "Template line" for t in v["turns"] if t["speaker"] == "persona")
    assert fake_model.calls == []


def test_ai_phrasing_with_post_checks_and_template_fallback(tenant, stu, fake_model):
    settings.set_box(tenant, "ai_token_budget", 100000, "owner approved test budget")
    fake_model.replies = [
        "Sure, happy to talk it through.",                                   # fine
        "We boosted posts last year and the second room opens in March.",    # leaks hidden facts
        "As an AI, I can't really say.",                                     # meta talk
        "Honestly we guarantee every member stays.",                         # guardrails
        " ".join(["word"] * 120),                                            # too long
    ]
    v = sessions.start(tenant, stu, "discovery_call", seed=5, mode="ai")
    notes = []
    for line in ["Thanks for the time today, really glad we could talk.",
                 "What would you like the studio to look like by spring?",
                 "Where are the numbers right now, roughly per week?",
                 "What have you already tried to fill those evening classes?",
                 "And what made you look at this now rather than in six months?"]:
        v = sessions.turn(tenant, stu, v["id"], line)
        p = v["turns"][-1]
        notes.append((p["source"], p["note"], p["text"]))
    assert notes[0][0] == "AI phrased" and notes[0][2] == "Sure, happy to talk it through."
    assert notes[1][0] == "Template line" and "hidden fact" in notes[1][1]
    assert notes[2][0] == "Template line" and "being an AI" in notes[2][1]
    assert "left out the fact" in notes[2][1]  # the rules revealed a fact this turn; the AI line must carry it
    assert notes[3][0] == "Template line" and "guardrails" in notes[3][1]
    assert notes[4][0] == "Template line" and "longer than" in notes[4][1]
    assert len(fake_model.calls) == 5
    # rules decide: the AI line never changed what was revealed or how the turn is scored
    v = sessions.end(tenant, stu, v["id"])
    assert v["score"]["mode"] == "ai"
    rules_v = play(tenant, stu, "discovery_call", ["Thanks for the time today, really glad we could talk.",
                                                  "What would you like the studio to look like by spring?",
                                                  "Where are the numbers right now, roughly per week?",
                                                  "What have you already tried to fill those evening classes?",
                                                  "And what made you look at this now rather than in six months?"])
    assert rules_v["score"]["total"] == v["score"]["total"]
    run = runtime.get(tenant, v["run_id"])
    assert run["agent"] == "roleplay" and run["ai_mode"] == "live"


def test_ai_lines_replay_exactly_with_ai_off(tenant, stu, fake_model, monkeypatch):
    settings.set_box(tenant, "ai_token_budget", 100000, "owner approved test budget")
    fake_model.reply = "Okay, tell me more about what you mean."
    lines = ["Thanks for the time today, really glad we could talk.", "What would you like the studio to look like by spring?"]
    live = play(tenant, stu, "discovery_call", lines, seed=77, mode="ai", end=False)
    assert [t["source"] for t in live["turns"][1:] if t["speaker"] == "persona"] == ["AI phrased", "Template line"]
    monkeypatch.setenv("OPERATOR_DISABLE_LLM", "1")
    calls = len(fake_model.calls)
    again = play(tenant, stu, "discovery_call", lines, seed=77, mode="ai", end=False)
    assert len(fake_model.calls) == calls
    assert [t["text"] for t in again["turns"]] == [t["text"] for t in live["turns"]]
    assert again["turns"][2]["source"] == "Replayed AI line"


def test_ai_session_token_cap(tenant, stu, fake_model, monkeypatch):
    settings.set_box(tenant, "ai_token_budget", 100000, "owner approved test budget")
    monkeypatch.setattr(ai, "SESSION_TOKEN_CAP", 200)
    fake_model.reply = "Okay, go on then."
    v = play(tenant, stu, "discovery_call", ["Thanks for the time today.", "Good to meet you properly.",
                                              "Really glad we could talk."], mode="ai", end=False)
    persona = [t for t in v["turns"] if t["speaker"] == "persona"][1:]
    assert len(fake_model.calls) == 2  # 120 tokens each: the third turn would go over the 200-token session cap
    assert persona[2]["source"] == "Template line" and "cap" in persona[2]["note"]


def test_leak_check_unit():
    sc = scenarios.builtin("discovery_call")
    assert ai.leaks("The second room is empty.", sc, ["goal"]) == ["why_now"]
    assert ai.leaks("We get four trial sign-ups.", sc, ["current"]) == []
    assert "current" in ai.leaks("Maybe four a week.", sc, ["goal"])
    assert ai.post_check(None, "As an AI model I think so.", sc, [], 30)


# ======================================================================= sessions: RBAC, isolation, sign-off, drills
def test_rbac_and_cross_user_isolation(client, tenant):
    from tests.conftest import login

    c1 = mk(tenant, "coachA", "coach")
    mk(tenant, "coachB", "coach")
    a = mk(tenant, "stuA", "student", coach_id=c1["id"])
    mk(tenant, "stuB", "student")
    mk(tenant, "setter1", "setter")
    as_user(client, "stuA")
    r = client.post("/t/owner/api/academy/roleplays", json={"scenario_key": "objection_partner", "seed": 4})
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    assert r.json()["can_play"]
    as_user(client, "stuB")
    assert client.get(f"/t/owner/api/academy/roleplays/{rid}").status_code == 404
    assert client.post(f"/t/owner/api/academy/roleplays/{rid}/turn", json={"text": "hi there"}).status_code == 404
    assert client.post(f"/t/owner/api/academy/roleplays/{rid}/end").status_code == 404
    assert client.get("/t/owner/api/academy/roleplays").json()["items"] == []
    as_user(client, "coachB")
    assert client.get(f"/t/owner/api/academy/roleplays/{rid}").status_code == 404
    assert client.post(f"/t/owner/api/academy/roleplays/{rid}/signoff", json={}).status_code == 404
    as_user(client, "coachA")
    r = client.get(f"/t/owner/api/academy/roleplays/{rid}")
    assert r.status_code == 200 and not r.json()["can_play"]
    assert client.post(f"/t/owner/api/academy/roleplays/{rid}/turn", json={"text": "hi there"}).status_code == 403
    assert client.post(f"/t/owner/api/academy/roleplays/{rid}/signoff", json={}).status_code == 409  # not complete yet
    assert [i["id"] for i in client.get("/t/owner/api/academy/roleplays").json()["items"]] == [rid]
    as_user(client, "stuA")
    r = client.post(f"/t/owner/api/academy/roleplays/{rid}/turn", json={"text": GOOD["objection_partner"][0]})
    assert r.status_code == 200 and r.json()["status"] == "complete"
    assert client.post(f"/t/owner/api/academy/roleplays/{rid}/signoff", json={}).status_code == 403  # no self sign-off
    as_user(client, "coachA")
    assert client.get("/t/owner/api/academy/roleplays", params={"pending_signoff": True}).json()["items"][0]["id"] == rid
    r = client.post(f"/t/owner/api/academy/roleplays/{rid}/signoff", json={"note": "Clean partner bridge."})
    assert r.status_code == 200 and r.json()["score"]["signoff"]["by_user_id"] == c1["id"]
    assert client.post(f"/t/owner/api/academy/roleplays/{rid}/signoff", json={}).status_code == 409
    as_user(client, "setter1")
    r = client.post("/t/owner/api/academy/roleplays", json={"scenario_key": "cold_dm_coach"})
    assert r.status_code == 200
    assert [i["id"] for i in client.get("/t/owner/api/academy/roleplays").json()["items"]] == [r.json()["id"]]
    login(client)
    ids = {i["id"] for i in client.get("/t/owner/api/academy/roleplays").json()["items"]}
    assert {rid, r.json()["id"]} <= ids
    assert client.get(f"/t/owner/api/academy/roleplays/{rid}").json()["score"]["signoff"]["note"] == "Clean partner bridge."
    assert any(x["kind"] == "roleplay.signoff" for x in audit.recent(tenant, 50))
    assert a["id"] in sessions.coached_user_ids(tenant, c1["id"])


def test_malformed_input_gets_422_never_500(client, tenant):
    mk(tenant, "learner7", "student")
    as_user(client, "learner7")
    base = "/t/owner/api/academy/roleplays"
    for body in (b"{broken", b"[1,2]", b'{"scenario_key": 5}', b'""'):
        assert client.post(base, content=body, headers={"content-type": "application/json"}).status_code == 422
    assert client.post(base, json={"scenario_key": "BAD KEY"}).status_code == 422
    assert client.post(base, json={"scenario_key": "discovery_call", "mode": "gpt"}).status_code == 422
    assert client.post(base, json={"scenario_key": "discovery_call", "seed": -1}).status_code == 422
    assert client.post(base, json={"scenario_key": "no_such_one"}).status_code == 404
    rid = client.post(base, json={"scenario_key": "objection_think"}).json()["id"]
    assert client.post(f"{base}/{rid}/turn", json={"text": "   "}).status_code == 422
    assert client.post(f"{base}/{rid}/turn", json={"text": "x" * 1501}).status_code == 422
    assert client.post(f"{base}/{rid}/turn", json={"words": "hi"}).status_code == 422
    assert client.post(f"{base}/{rid}/turn", content=b"{nope", headers={"content-type": "application/json"}).status_code == 422
    assert client.get(f"{base}/not-a-number").status_code == 422
    assert client.get(f"{base}/999999").status_code == 404
    assert client.get(base, params={"status": "weird"}).status_code == 422
    assert client.get(base, params={"limit": 0}).status_code == 422
    assert client.post(f"{base}/{rid}/drill", json={"criterion": "discovery"}).status_code == 409  # not finished
    assert client.post(f"{base}/{rid}/end").status_code == 200
    assert client.post(f"{base}/{rid}/turn", json={"text": "hello again"}).status_code == 409
    assert client.post(f"{base}/{rid}/end").status_code == 409
    assert client.post(f"{base}/{rid}/drill", json={"criterion": "not_on_card"}).status_code == 404
    assert client.post(f"{base}/{rid}/drill", json={"criterion": "BAD!"}).status_code == 422
    assert client.post(f"{base}/score-text", json={"text": ""}).status_code == 422
    assert client.post(f"{base}/score-text", json={"text": "x", "scenario_key": "nope_x"}).status_code == 404


def test_score_text_endpoint(client, tenant):
    mk(tenant, "learner8", "student")
    as_user(client, "learner8")
    r = client.post("/t/owner/api/academy/roleplays/score-text", json={"text": STUFFED})
    assert r.status_code == 200 and r.json()["total"] < 20
    transcript = "\n".join([
        "Rep: Thanks for the time. What would you like the business to look like by spring?",
        "Prospect: I want twenty more regular members by spring.",
        "Rep: Where are the numbers right now, roughly per week?",
        "Prospect: About four trial sign-ups a week and two of them stick.",
        "Rep: So if I've got this right, you want twenty more members and right now two trials stick each week. Is that the gap?",
        "Prospect: Yes, exactly.",
        "Rep: Would it be useful if I showed you how we'd close that? Here's how we work: we follow up every trial within the hour.",
        "Prospect: Okay. I need to think about it.",
        "Rep: Totally fair. What specifically do you want to think through?",
        "Prospect: Mostly time.",
        "Rep: Shall we book a short call on Thursday to map it out?",
        "Prospect: Sounds good, let's book it.",
    ])
    r = client.post("/t/owner/api/academy/roleplays/score-text", json={"text": transcript})
    assert r.status_code == 200
    assert r.json()["total"] >= 60 and r.json()["outcome"] == "won"


def test_abandon_and_trace(tenant, stu):
    v = sessions.start(tenant, stu, "discovery_call", seed=2)
    v = sessions.turn(tenant, stu, v["id"], GOOD["discovery_call"][0])
    run = runtime.get(tenant, v["run_id"])
    assert run["agent"] == "roleplay" and [s["tool"] for s in run["steps"]] == ["start", "turn"]
    v = sessions.abandon(tenant, stu, v["id"])
    assert v["status"] == "abandoned" and v["score"] is None
    with pytest.raises(sessions.Conflict):
        sessions.turn(tenant, stu, v["id"], "still there?")
    v = play(tenant, stu, "objection_partner", GOOD["objection_partner"])
    steps = [s["tool"] for s in runtime.get(tenant, v["run_id"])["steps"]]
    assert steps == ["start", "turn", "score"]


def test_missed_objections_become_drill_cards(client, tenant):
    u = mk(tenant, "learner9", "student")
    v = play(tenant, u, "objection_think", ["No problem at all.", "Sure, no pressure."])
    assert crit(v, "objection_think")["points"] == 0
    with tenant.db() as c:
        cards = c.execute("SELECT d.objection FROM drill_cards k JOIN drills d ON d.id=k.drill_id WHERE k.user_id=?",
                          (u["id"],)).fetchall()
    assert "I need to think about it" in [r["objection"] for r in cards]
    as_user(client, "learner9")
    r = client.post(f"/t/owner/api/academy/roleplays/{v['id']}/drill", json={"criterion": "objection_think"})
    assert r.status_code == 200 and r.json()["scenario_key"] == "objection_think" and r.json()["card"]["card_id"]
    r = client.post(f"/t/owner/api/academy/roleplays/{v['id']}/drill", json={"criterion": "outcome"})
    assert r.status_code == 200 and r.json()["card"] is None
    due = client.get("/t/owner/api/drills").json()["cards"]
    assert any(c["objection"] == "I need to think about it" and c["is_due"] for c in due)
    other = mk(tenant, "learner10", "student")
    del other
    as_user(client, "learner10")
    assert client.post(f"/t/owner/api/academy/roleplays/{v['id']}/drill", json={"criterion": "objection_think"}).status_code == 404


def test_completion_emits_an_event(tenant, stu):
    v = play(tenant, stu, "objection_partner", GOOD["objection_partner"])
    evs = events.recent(tenant, "roleplay_completed")
    assert evs and evs[0]["payload"]["roleplay_id"] == v["id"] and evs[0]["payload"]["outcome"] == "won"


# ======================================================================= demo vs real workspace
def test_demo_seed_two_completed_roleplays(client, demo_tenant):
    with demo_tenant.db() as c:
        stu_id = c.execute("SELECT id FROM users WHERE username='demo-student'").fetchone()["id"]
        rows = db.rows(c.execute("SELECT * FROM roleplays WHERE user_id=?", (stu_id,)))
    assert len(rows) == 2 and all(r["status"] == "complete" and r["demo"] == 1 and r["total"] is not None for r in rows)
    scores = [json.loads(r["score"]) for r in rows]
    assert any(s["signoff"] and "(DEMO)" in s["signoff"]["by"] for s in scores)
    assert max(s["total"] for s in scores) > min(s["total"] for s in scores)
    demo_login(client, "student")
    items = client.get("/t/demo/api/academy/roleplays").json()["items"]
    assert len(items) == 2 and all(i["demo"] for i in items)
    one = client.get(f"/t/demo/api/academy/roleplays/{items[0]['id']}").json()
    assert one["score"]["criteria"] and one["demo"]
    demo_login(client, "coach")
    assert len(client.get("/t/demo/api/academy/roleplays").json()["items"]) == 2
    demo_login(client, "setter")
    assert client.get("/t/demo/api/academy/roleplays").json()["items"] == []
    assert client.get(f"/t/demo/api/academy/roleplays/{items[0]['id']}").status_code == 404


def test_real_workspace_is_never_seeded(client, tenant):
    from operator_ai import roleplay

    assert roleplay.seed_demo(tenant) == 0
    with tenant.db() as c:
        assert c.execute("SELECT COUNT(*) FROM roleplays").fetchone()[0] == 0
    from tests.conftest import login

    login(client)
    assert client.get("/t/owner/api/academy/roleplays").json()["items"] == []
    items = client.get("/t/owner/api/academy/scenarios").json()["items"]
    assert items and "(DEMO)" not in json.dumps(items)
    r = client.post("/t/owner/api/academy/roleplays", json={"scenario_key": "discovery_call"})
    assert r.status_code == 200 and r.json()["demo"] is False


# ======================================================================= review fixes: gaming, crashes, exam integrity
KITCHEN_SINK = ("Totally fair. What specifically is on your mind? You said {f}. Compared with the cost of staying where you are, "
                "and honestly no one can guarantee results, shall we book a short call with both of you together, so I can "
                "walk you through what would be most useful?")


@pytest.mark.parametrize("key", ["objection_think", "objection_price", "objection_partner", "objection_send_info", "webinar_qa"])
def test_a_kitchen_sink_line_answering_every_objection_earns_nothing(tenant, stu, key):
    # One line bundling the SOP move for every objection used to win every objection drill (80+, "passed").
    sc = scenarios.builtin(key)
    f = next((sc["facts"][k]["keywords"][0] for k in sc.get("pre_revealed", [])), "that")
    line = KITCHEN_SINK.format(f=f)
    assert acts.is_hedging(line)
    v = play(tenant, stu, key, [line, line, line], seed=2)
    s = v["score"]
    assert not s["passed"] and s["outcome"] != "won"
    assert all(c["points"] == 0 for c in s["criteria"] if c["key"].startswith("objection_"))
    assert s["integrity_flags"] and s["integrity_flags"][0]["code"] == "kitchen_sink"
    learner = [t for t in v["turns"] if t["speaker"] == "learner"]
    assert learner[0]["color"] == "red" and any("several objections" in n for n in learner[0]["notes"])
    persona = [t for t in v["turns"] if t["speaker"] == "persona"]
    assert any("which" in t["text"].lower() or "answer" in t["text"].lower() for t in persona[1:])


def test_real_objection_answers_are_not_mistaken_for_a_kitchen_sink():
    for key, lines in GOOD.items():
        for line in lines:
            assert not acts.is_hedging(line), (key, line)
    assert not acts.is_hedging("Fair. Compared with the cost of staying where you are, what specifically worries you?")


def test_a_pass_needs_the_scenarios_outcome(tenant, stu):
    # Discovery, gap and offer done well, then the learner ends the session as soon as the price objection lands.
    v = play(tenant, stu, "discovery_call", GOOD["discovery_call"][:6], seed=3, exam=True)
    s = v["score"]
    assert s["outcome"] == "ended" and s["total"] >= 70
    assert s["passed"] is False and s["certifiable"] is False
    assert crit(v, "objection_price")["points"] == 0


def _raw_client(app):
    from fastapi.testclient import TestClient

    return TestClient(app, raise_server_exceptions=False)


def test_malformed_scenario_json_gets_422_never_500(app, tenant):
    from tests.conftest import login

    base = scenarios.builtin("cold_dm_coach")
    bad = [
        {**base, "dims": 5}, {**base, "dims": "goal"}, {**base, "pre_revealed": [{"a": 1}]}, {**base, "opening": 5},
        {**base, "opening": [1, 2]}, {**base, "skill": ["dm"]}, {**base, "skill": {"a": 1}},
        {**base, "persona": {**base["persona"], "business": 5}}, {**base, "difficulty": True}, {**base, "patience": False},
        {**base, "lines": {"neutral": ["Hi {fact.__class__}"]}}, {**base, "lines": {"neutral": ["{0} there"]}},
        {**base, "title": "Make $100k a month, guaranteed results"}, {**base, "summary": "x" * 401},
        {**base, "notes": "y" * 70_000},
    ]
    with _raw_client(app) as client:
        login(client)
        for d in bad:
            r = client.put("/t/owner/api/academy/scenarios/probe_bad", json={"data": d})
            assert r.status_code == 422, (d.get("title"), r.status_code, r.text[:200])
            r = client.post("/t/owner/api/academy/scenarios/cold_dm_coach/test-run", json={"lines": ["hi there"], "data": d})
            assert r.status_code == 422, r.text[:200]
        ok = {**base, "lines": {"neutral": ["Okay. {concern}"]}}
        assert client.put("/t/owner/api/academy/scenarios/probe_ok", json={"data": ok}).status_code == 200


def test_bad_rule_parameters_are_refused_and_scoring_survives_old_values(app, tenant, stu):
    from tests.conftest import login

    with _raw_client(app) as client:
        login(client)
        put = lambda k, body: client.put(f"/t/owner/api/academy/roleplays/rubric/{k}", json=body)  # noqa: E731
        for k, p in (("objection_think", {"retry_credit": "abc"}), ("objection_think", {"retry_credit": 3}),
                     ("talk_ratio", {"max_share": "x"}), ("trust", {"full_gain": None}), ("dm_length", {"max_words": 4.5}),
                     ("dm_length", {"max_words": True}), ("discovery", {"min_question_words": 3}),
                     ("dm_length", {"nope": 1})):
            r = put(k, {"params": p})
            assert r.status_code == 422, (k, p, r.text)
        assert put("talk_ratio", {"params": {"max_share": 0.55}}).status_code == 200
        assert client.put("/t/owner/api/academy/roleplays/rubric/discovery", content=b'{"weight": NaN}',
                          headers={"content-type": "application/json"}).status_code == 422
    # A malformed value already in the database (an older build, a hand edit) can't break scoring.
    with tenant.db() as c:
        c.execute("UPDATE roleplay_rubrics SET params=? WHERE rule_key='objection_think'", (json.dumps({"retry_credit": "abc"}),))
        c.execute("UPDATE roleplay_rubrics SET params=? WHERE rule_key='trust'", (json.dumps({"full_gain": "x"}),))
    v = play(tenant, stu, "objection_think", ["No problem at all.", GOOD["objection_think"][0], GOOD["objection_think"][1]])
    assert v["status"] == "complete" and crit(v, "objection_think")["points"] == crit(v, "objection_think")["max"] / 2


def test_a_session_the_rules_ended_but_scoring_missed_recovers(tenant, stu):
    v = sessions.start(tenant, stu, "objection_think", seed=4)
    with tenant.db() as c:
        row = c.execute("SELECT state FROM roleplays WHERE id=?", (v["id"],)).fetchone()
        st = json.loads(row["state"])
        st["st"]["outcome"], st["st"]["end_reason"] = "lost", "unconvinced"
        c.execute("UPDATE roleplays SET state=? WHERE id=?", (json.dumps(st), v["id"]))
    v = sessions.turn(tenant, stu, v["id"], "Hello again?")
    assert v["status"] == "complete" and v["score"] is not None


def test_validator_checks_the_rules_own_numbers(tenant):
    res = {r["rule_key"]: r for r in rubric.validate(tenant)}
    assert res["talk_ratio"]["ok"] and res["dm_one_question"]["ok"]
    rubric.update(tenant, "talk_ratio", {"params": {"max_share": 0.7}}, "owner(owner)")
    v = rubric.validate_rule(tenant, rubric.rules(tenant)["talk_ratio"])
    assert not v["ok"] and "70%" in v["issues"][0]
    rubric.update(tenant, "dm_one_question", {"params": {"max_questions": 2}}, "owner(owner)")
    assert not rubric.validate_rule(tenant, rubric.rules(tenant)["dm_one_question"])["ok"]
    with pytest.raises(rubric.RuleError):
        rubric.update(tenant, "dm_length", {"params": {"max_words": "sixty"}}, "owner(owner)")


def test_exam_traces_carry_no_live_scoring_until_scored(client, tenant):
    mk(tenant, "examiner1", "student")
    as_user(client, "examiner1")
    v = client.post("/t/owner/api/academy/roleplays", json={"scenario_key": "discovery_call", "exam": True}).json()
    r = client.post(f"/t/owner/api/academy/roleplays/{v['id']}/turn", json={"text": STUFFED})
    assert r.status_code == 200
    assert all(f["severity"] == "block" for t in r.json()["turns"] for f in t.get("flags", []))  # no "no credit" hint
    run = client.get(f"/t/owner/api/runs/{v['run_id']}").json()
    blob = json.dumps(run["steps"])
    assert '"acts"' not in blob and '"events"' not in blob and "keyword_stuffing" not in blob and '"trust"' not in blob
    client.post(f"/t/owner/api/academy/roleplays/{v['id']}/end")
    run = client.get(f"/t/owner/api/runs/{v['run_id']}").json()
    assert run["steps"][-1]["tool"] == "score"
    # practice sessions keep the full trace
    p = client.post("/t/owner/api/academy/roleplays", json={"scenario_key": "discovery_call"}).json()
    client.post(f"/t/owner/api/academy/roleplays/{p['id']}/turn", json={"text": GOOD["discovery_call"][0]})
    assert '"acts"' in json.dumps(client.get(f"/t/owner/api/runs/{p['run_id']}").json()["steps"])


def test_certifiable_needs_confirmed_rules_whose_citations_still_check_out(tenant, stu):
    for k in ("objection_partner", "trust"):
        rubric.update(tenant, k, {"confirmed": True}, "owner(owner)")
    v = play(tenant, stu, "objection_partner", GOOD["objection_partner"], exam=True)
    assert v["score"]["passed"] and not v["score"]["unconfirmed_rules"] and not v["score"]["flagged_rules"]
    assert v["score"]["certifiable"] is True
    # the owner confirmed it, but the cited playbook step no longer exists: not certifiable any more
    rubric.update(tenant, "trust", {"cite_lines": "steps:99", "confirmed": True}, "owner(owner)")
    v = play(tenant, stu, "objection_partner", GOOD["objection_partner"], exam=True)
    assert v["score"]["flagged_rules"] == ["trust"] and v["score"]["certifiable"] is False


def test_score_text_refuses_very_long_pastes(client, tenant):
    mk(tenant, "learner11", "student")
    as_user(client, "learner11")
    text = "\n".join(f"Rep: what is your goal number {i}?" for i in range(121))
    r = client.post("/t/owner/api/academy/roleplays/score-text", json={"text": text})
    assert r.status_code == 422
    assert client.post("/t/owner/api/academy/roleplays/score-text", json={"text": "\n".join(text.splitlines()[:120])}).status_code == 200


SPOKEN_VIOLATIONS = [
    ("You'll 10x your revenue in 90 days.", "earnings_promise"),
    ("I promise you'll fill the second room by spring.", "earnings_promise"),
    ("You'll definitely get 20 new members.", "earnings_promise"),
    ("Most of our clients hit six figures within a year.", "results_claim"),
    ("Every single client of ours has doubled their revenue.", "results_claim"),
    ("There are only two spots left this month.", "false_urgency"),
    ("The price goes up tomorrow, so decide now.", "false_urgency"),
    ("Just put it on your Amex.", "debt_steer"),
    ("Max out your credit card, it's worth it.", "debt_steer"),
    ("Borrow from family if you have to.", "debt_steer"),
    ("Don't worry about your partner, this is your decision.", "hide_from_partner"),
    ("Leave your husband out of this one.", "hide_from_partner"),
]
SPOKEN_FINE = [
    "No one can guarantee results.", "I can't promise you'll fill the room.", "I promise I'll send the recap tonight.",
    "Please don't put this on a credit card.", "I would never tell you to borrow from family.",
    "Would it help if your partner joined the next call?", "Most of our clients are coaches like you.",
    "Where do most of your members come from right now?", "We have space for new clients this month.",
]


def test_spoken_compliance_rules_catch_common_call_violations(tenant, stu):
    sc = scenarios.builtin("discovery_call")
    for line, code in SPOKEN_VIOLATIONS:
        c = acts.classify(line, sc, {"turn_no": 2})
        assert c["blocked"] and code in [f["code"] for f in c["flags"]], (line, c["flags"])
    for line in SPOKEN_FINE:
        assert not acts.classify(line, sc, {"turn_no": 2})["blocked"], line
    v = play(tenant, stu, "discovery_call", GOOD["discovery_call"][:5] + ["There are only two spots left this month."])
    assert v["score"]["total"] == 0 and v["score"]["gate"] == "compliance"
    # model lines and hints are held to the same rules
    lines = list(score.MODEL_Q.values()) + list(score.MODEL_OBJ.values()) + list(score.MODEL_OUTCOME.values()) + \
        list(score.MOVE_HINT.values())
    for line in lines:
        assert not acts.spoken_flags(line.format(q="you get four trial sign-ups a week")), line
