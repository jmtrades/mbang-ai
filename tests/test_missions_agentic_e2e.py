"""Missions doing the real work on a REAL (non-demo) workspace with AI off and every feature installed (prospects,
inbound, proposals, client ops, webinars, studio, academy). Each test walks one part of a mission the way a person would
and checks the output is specific, uses the person's own data, follows the playbooks, waits and wakes on the right
events, re-plans for real and survives a restart."""
from __future__ import annotations

import time

import pytest

from operator_ai import auth, db, events, handlers, jobs, queue
from operator_ai import prospects as P
from operator_ai.missions import engine, evaluator, registry

BIOS = ["Online fitness coach for busy parents. Runs a monthly group programme.",
        "Strength coach helping women over forty lift with confidence. Online coaching.",
        "Marathon coach. Training plans and weekly check-ins for first-time runners.",
        "Postpartum fitness coach. Gentle return-to-exercise programme for new mums."]
SENDER = {"name": "Sam Student", "business": "Sam Growth", "postal_address": "5 Side Street, Samtown",
          "reply_email": "sam@sam-growth.example"}
ANS = {"niche": "fitness coaches", "offer": "done-for-you DM setting", "price": 1500}


@pytest.fixture(autouse=True)
def wired():
    handlers.load()
    yield


@pytest.fixture
def clock(monkeypatch):
    now = [time.time()]
    for mod in (registry, engine, evaluator):
        monkeypatch.setattr(mod, "clock", lambda: now[0])
    return now


def mk_user(t, role: str, name: str) -> dict:
    uid = auth.create_user(t, name, "pw-correct-horse-1", role)
    if role == "student":
        with t.db() as c:
            sid = c.execute("INSERT INTO students(name,email,joined,stage,demo) VALUES(?,?,?,?,0)",
                            (name, f"{name}@example.invalid", time.time(), "outreach")).lastrowid
            c.execute("UPDATE users SET student_id=? WHERE id=?", (sid, uid))
    with t.db() as c:
        return auth.user_public(dict(c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()))


def owner_of(t) -> dict:
    with t.db() as c:
        return auth.user_public(dict(c.execute("SELECT * FROM users WHERE role='owner' ORDER BY id LIMIT 1").fetchone()))


def certify(t, u: dict) -> None:
    from operator_ai.academy import certify as cert

    for path in ("growth_operator", "setter", "compliance_core"):
        cert.award(t, u, path, "L1", [], "test")


def import_prospects(t, u: dict, n: int = 32, email: bool = False) -> list[int]:
    P.save_icp(t, u, {"niche": "fitness coaches", "offer": "done-for-you DM setting", "sender": SENDER})
    rows = ["Full Name,IG Handle,E-mail Address,Category,Follower Count,Bio"]
    for i in range(n):
        rows.append(f"Casey Coach{i},{'' if email else f'@casey_{u['id']}_{i}'},casey{u['id']}.{i}@example.com,"
                    f"fitness coaching,{1200 + i},\"{BIOS[i % len(BIOS)]}\"")
    P.import_csv(t, u, "\n".join(rows) + "\n", "leads.csv", "", "legitimate_interest", "From their public profiles")
    with t.db() as c:
        return [r[0] for r in c.execute("SELECT id FROM prospects WHERE owner_user_id=? ORDER BY id", (u["id"],))]


def S(m: dict) -> dict:
    return {s["skey"]: s for s in m["steps"]}


def actor(u: dict) -> str:
    return f"{u['username']}({u['role']})"


def approve(t, u: dict, qids, confirm: bool = True) -> None:
    for q in qids:
        if queue.get(t, q)["status"] not in queue.OPEN:
            continue
        r = queue.approve(t, q, actor(u), u["role"], user_id=u["id"])
        if confirm and r["status"] == "awaiting_manual_send":
            queue.mark_sent(t, q, actor(u), u["role"], user_id=u["id"])


def to_openers(t, u: dict, email: bool = False, answers: dict | None = None) -> dict:
    """A certified student with a real imported list, a first_client mission and the offer/profile approved."""
    certify(t, u)
    import_prospects(t, u, email=email)
    m = engine.create(t, u, "Get my first client", "first_client", answers or ANS)
    for key in ("offer_doc", "profile"):
        approve(t, u, S(engine.get(t, m["id"]))[key]["queue_ids"])
    return engine.get(t, m["id"])


def open_items_to(t, pid: int, kinds=("prospect_dm", "prospect_email", "booking_dm", "dm_opener", "followup")) -> list[dict]:
    with t.db() as c:
        ids = [r[0] for r in c.execute(
            f"SELECT id FROM queue WHERE json_extract(meta,'$.prospect_id')=? AND status IN ('pending','held','needs_human') "
            f"AND kind IN ({','.join('?' * len(kinds))}) ORDER BY id", (pid, *kinds))]
    return [queue.get(t, i) for i in ids]


# ============================================================================ outreach that reads like a person wrote it
def test_a_block_of_openers_is_personal_not_the_same_message_five_times(tenant):
    stu = mk_user(tenant, "student", "sam")
    m = to_openers(tenant, stu)
    st = S(m)
    assert st["openers"]["status"] == "waiting_approval" and st["openers"]["output"]["source"] == "prospects"
    items = [queue.get(tenant, q) for q in st["openers"]["queue_ids"]]
    bodies = [i["body"] for i in items]
    assert len(items) >= 3
    # the playbook: one verifiable observation beats merge fields, and near-identical DMs get accounts restricted
    assert len({b.split(",", 1)[1] for b in bodies}) >= 2, bodies
    assert any(any(phrase in b for phrase in ("busy parents", "lift with confidence", "first-time runners", "new mums"))
               for b in bodies), bodies
    assert all(len(b.split()) <= 80 and "http" not in b for b in bodies)  # short, link-free first touches


def test_the_first_client_plan_starts_with_the_warm_network_per_the_playbook(tenant):
    stu = mk_user(tenant, "student", "wren")
    m = to_openers(tenant, stu)
    warm = S(m)["warm"]
    assert warm["status"] == "blocked_human" and warm["depends"] == ["offer_doc"]
    assert "fitness coaches with done-for-you DM setting" in warm["message"] and "{" not in warm["message"]
    assert "No promised results" in warm["message"] and len(warm["message"]) < 600
    assert any(a["kind"] == "do" and a["step_id"] == warm["id"] for a in engine.next_actions(tenant, stu))
    assert S(m)["openers"]["status"] == "waiting_approval"  # cold outreach runs alongside, not after
    assert "warm network" in m["context"]["targets"]["summary"] and "not a promise" in S(m)["intake"]["message"]
    m = engine.mark_done(tenant, m["id"], warm["id"], actor(stu))
    assert S(m)["warm"]["status"] == "done"


def test_the_research_step_tells_the_truth_about_what_can_be_personalised(tenant):
    stu = mk_user(tenant, "student", "rae")
    certify(tenant, stu)
    import_prospects(tenant, stu)
    with tenant.db() as c:  # one prospect added by hand with no research at all
        c.execute("INSERT INTO prospects(owner_user_id,name,handle,stage,lawful_basis,do_not_contact,demo,created,updated) "
                  "VALUES(?,?,?,?,?,0,0,?,?)", (stu["id"], "Robin Bare", "@robin_bare", "new", "legitimate_interest",
                                                time.time(), time.time()))
    m = engine.create(tenant, stu, "Get my first client", "first_client", ANS)
    r = S(m)["research"]
    assert r["status"] == "done"
    assert r["output"]["researched"] == 1  # the one with no facts yet
    assert r["output"]["personal"] == 32 and r["output"]["total"] == 33
    assert "Researched 0" not in r["message"] and "32 of 33" in r["message"], r["message"]


def test_openers_never_go_twice_to_someone_already_messaged_from_the_prospects_page(tenant):
    stu = mk_user(tenant, "student", "kai")
    certify(tenant, stu)
    ids = import_prospects(tenant, stu)
    with tenant.db() as c:
        top = [r[0] for r in c.execute("SELECT id FROM prospects WHERE owner_user_id=? ORDER BY COALESCE(fit_score,0) DESC, id "
                                       "LIMIT 6", (stu["id"],))]
    for pid in top:  # the student already wrote first messages by hand to the best-fit prospects
        P.draft_opener(tenant, stu, pid, "instagram_dm", "question")
    m = engine.create(tenant, stu, "Get my first client", "first_client", ANS)
    for key in ("offer_doc", "profile"):
        approve(tenant, stu, S(engine.get(tenant, m["id"]))[key]["queue_ids"])
    st = S(engine.get(tenant, m["id"]))
    items = [queue.get(tenant, q) for q in st["openers"]["queue_ids"]]
    assert items and not {i["meta"]["prospect_id"] for i in items} & set(top)
    assert all(i["kind"] == "prospect_dm" for i in items)  # never the generic rules fallback beside the feature's drafts
    for pid in ids:
        firsts = [i for i in open_items_to(tenant, pid) if i["kind"] in ("prospect_dm", "dm_opener")]
        assert len(firsts) <= 1, (pid, [f["id"] for f in firsts])


def test_an_applied_opener_change_really_changes_the_next_block(tenant, clock):
    stu = mk_user(tenant, "student", "lou")
    m = to_openers(tenant, stu)
    mid = m["id"]
    approve(tenant, stu, S(m)["openers"]["queue_ids"])
    m = engine.get(tenant, mid)
    met = evaluator.metrics(tenant, m)
    met["leading"]["openers_sent"], met["leading"]["replies"] = 90, 1  # the numbers say the openers aren't landing
    f = next(x for x in evaluator.rules(tenant, m, met, clock[0]) if x["rule"] == "low_reply_rate")
    to = f["diff"]["context"]["opener_move"]["to"]
    assert to in P.MOVES and to != "question"  # a style the prospects feature can actually write
    rid = evaluator.store_replan(tenant, mid, "rules", f["rule"], {**f["diff"], "why": f["why"]})
    evaluator.apply_replan(tenant, mid, rid, actor(stu))
    clock[0] += registry.DAY + 3600
    engine.tick(tenant, clock[0])
    st = S(engine.get(tenant, mid))
    assert st["openers"]["output"]["block"] == 2
    moves = [queue.get(tenant, q)["meta"]["move"] for q in st["openers"]["output"]["block_qids"]]
    assert moves and all(mv == to for mv in moves), (to, moves)


# ============================================================================ replies, booking, the call, the proposal
def test_a_positive_reply_gets_one_answer_with_two_real_times_in_the_same_thread(tenant, monkeypatch):
    from operator_ai import inbound

    monkeypatch.setenv("OPERATOR_PUBLIC_URL", "https://acme.example.invalid")  # prospect emails need a working unsubscribe

    stu = mk_user(tenant, "student", "jo")
    m = to_openers(tenant, stu, email=True)
    mid = m["id"]
    block = S(m)["openers"]["queue_ids"]
    approve(tenant, stu, block)
    first = queue.get(tenant, block[0])
    pid = first["meta"]["prospect_id"]
    p = P._row(tenant, pid)
    res = inbound.route(tenant, "email", f"Casey <{p['email']}>", "Yes, sounds good. When are you free for a call?",
                        {"subject": "Re: " + first["meta"]["subject"], "provider_id": "<r1@example.com>"})
    assert res["classification"] == "positive"
    st = S(engine.get(tenant, mid))
    assert st["replies"]["message"].startswith("1 reply so far, 1 positive"), st["replies"]["message"]
    replies = open_items_to(tenant, pid, ("prospect_dm", "prospect_email", "booking_dm"))
    assert len(replies) == 1, [(r["kind"], r["body"][:60]) for r in replies]  # one answer, not two
    one = replies[0]
    slots = one["meta"]["slots"]
    assert len(slots) == 2 and slots[0]["label"] in one["body"] and slots[1]["label"] in one["body"]
    assert one["meta"]["subject"].lower().startswith("re:") and one["meta"]["in_reply_to"] == "<r1@example.com>"
    assert one["body"].startswith("Hi Casey") and "STOP" in one["body"]
    assert one["meta"]["mission_id"] == mid and one["id"] in st["booking"]["queue_ids"]
    assert st["booking"]["status"] == "waiting_approval"
    approve(tenant, stu, [one["id"]])
    assert S(engine.get(tenant, mid))["booking"]["status"] == "waiting_event"


def test_proposed_times_that_go_by_are_refreshed_in_the_waiting_draft(tenant, clock):
    stu = mk_user(tenant, "student", "pia")
    m = to_openers(tenant, stu)
    mid = m["id"]
    approve(tenant, stu, S(m)["openers"]["queue_ids"])
    pid = queue.get(tenant, S(m)["openers"]["queue_ids"][0])["meta"]["prospect_id"]
    P.record_reply(tenant, pid, actor(stu), "Yes please, tell me more", "interested", user=stu)
    st = S(engine.get(tenant, mid))
    item = queue.get(tenant, st["booking"]["queue_ids"][0])
    old = item["meta"]["slots"]
    assert item["body"].startswith("Hi Casey, thanks for getting back to me.") and old[0]["label"] in item["body"]
    assert st["booking"]["due"] == old[0]["start"]  # the step wakes when the first time goes by
    clock[0] = old[0]["start"] + 60
    engine.tick(tenant, clock[0])
    item = queue.get(tenant, item["id"])
    new = item["meta"]["slots"]
    assert new[0]["start"] > clock[0] and new[0]["label"] in item["body"] and new[1]["label"] in item["body"]
    assert old[0]["label"] not in item["body"] and item["body"].startswith("Hi Casey, thanks for getting back to me.")
    assert item["body"].count(" or ") == 1 and item["status"] == "pending"


def test_what_they_said_on_the_call_is_in_the_proposal(tenant):
    stu = mk_user(tenant, "student", "ash")
    certify(tenant, stu)
    p = P.add_prospect(tenant, stu, {"name": "Lee Client", "email": "lee@example.invalid", "niche": "fitness coaches"},
                       "legitimate_interest", "From their public website")["prospect"]
    P.save_icp(tenant, stu, {"niche": "fitness coaches", "offer": "done-for-you DM setting", "sender": SENDER})
    m = engine.create(tenant, stu, "Get my first client", "first_client", ANS)
    mid = m["id"]
    events.emit(tenant, "booking_made", {"party_type": "prospect", "party_id": p["id"], "starts_at": time.time() + 3600,
                                         "owner_user_id": stu["id"]})
    for s in engine.get(tenant, mid)["steps"]:  # the call is the next thing (the earlier steps aren't the point here)
        if s["skey"] in ("offer_doc", "profile", "openers", "followups", "replies", "booking", "prep"):
            with tenant.db() as c:
                c.execute("UPDATE mission_steps SET status='skipped' WHERE id=?", (s["id"],))
    engine.advance(tenant, mid)
    engine.pump(tenant, mid)
    call = S(engine.get(tenant, mid))["call"]
    assert call["status"] == "blocked_human"
    note = "We get about 30 enquiries a month but I only book 3 calls. I want my calendar full by spring."
    m = engine.mark_done(tenant, mid, call["id"], actor(stu), outcome="showed", note=note,
                         numbers={"leads": 30, "booked": 3, "offer_price": 900})
    st = S(m)
    assert st["proposal"]["status"] == "waiting_approval" and st["proposal"]["output"]["source"] == "proposals"
    body = queue.get(tenant, st["proposal"]["queue_ids"][0])["body"]
    assert "We get about 30 enquiries a month but I only book 3 calls" in body
    assert "Add your call notes" not in body and "No numbers were shared yet" not in body


def test_a_won_proposal_records_the_client_and_onboards_it_once_through_client_ops(tenant):
    stu = mk_user(tenant, "student", "max")
    certify(tenant, stu)
    p = P.add_prospect(tenant, stu, {"name": "Win Coach", "email": "win@example.invalid", "niche": "fitness coaches"},
                       "legitimate_interest", "From their public website")["prospect"]
    m = engine.create(tenant, stu, "Get my first client", "first_client", ANS)
    mid = m["id"]
    P.mark_won(tenant, p["id"], actor(stu), via="proposal")  # what accepting the proposal page does
    m = engine.get(tenant, mid)
    with tenant.db() as c:
        cid = c.execute("SELECT client_id FROM prospects WHERE id=?", (p["id"],)).fetchone()[0]
        kids = c.execute("SELECT COUNT(*) FROM missions WHERE parent_id=?", (mid,)).fetchone()[0]
    assert m["context"]["won"] and m["context"]["client_id"] == cid
    onb = S(m)["onboarding"]
    assert onb["status"] == "done" and onb["output"]["source"] == "client_ops" and onb["output"]["client_id"] == cid
    assert kids == 0  # no second, client-less onboarding mission
    kick = [i for i in queue.list_items(tenant, limit=500) if (i["meta"] or {}).get("purpose") == "client_kickoff"]
    assert len(kick) == 1 and kick[0]["recipient_ref"] == "win@example.invalid" and kick[0]["body"].startswith("Hi Win")


# ============================================================================ fix my bottleneck
def test_fix_bottleneck_drafts_the_fix_for_the_stage_it_found(tenant):
    o = owner_of(tenant)
    P.save_icp(tenant, o, {"niche": "physio clinics", "offer": "patient reactivation"})
    m = engine.create(tenant, o, "Fix my show rate", None, {"dms_sent": 600, "replies": 54, "conversations": 30,
                                                             "links_sent": 18, "booked": 10, "showed": 3, "closed": 1,
                                                             "offer_price": 2500})
    st = S(m)
    assert m["context"]["diagnosis"]["bottleneck"]["key"] == "show"
    items = [queue.get(tenant, q) for q in st["fix"]["queue_ids"]]
    titles = " | ".join(i["title"] for i in items).lower()
    for want in ("confirmation", "24", "2 hours", "no-show"):  # the reminder ladder and the rebook, per the playbook
        assert want in titles, titles
    assert not any(i["kind"] in ("dm_opener", "prospect_dm") or (i["meta"] or {}).get("lead_tier") == "cold" for i in items)
    assert all("[their brand]" not in i["body"] for i in items)
    assert all(i["status"] in queue.OPEN for i in items) and st["fix"]["status"] == "waiting_approval"


def test_fix_bottleneck_reply_rate_uses_the_persons_own_niche(tenant):
    o = owner_of(tenant)
    P.save_icp(tenant, o, {"niche": "physio clinics", "offer": "patient reactivation"})
    m = engine.create(tenant, o, "Fix my bottleneck", None, {"dms_sent": 800, "replies": 24, "conversations": 12,
                                                              "booked": 3, "offer_price": 2500})
    assert m["context"]["diagnosis"]["bottleneck"]["key"] == "reply"
    bodies = " ".join(queue.get(tenant, q)["body"] for q in S(m)["fix"]["queue_ids"])
    assert "physio clinics" in bodies and "business owners" not in bodies


# ============================================================================ launch a webinar
def test_webinar_assets_and_promotion_speak_to_the_niche_and_promote_the_event(tenant, clock):
    o = owner_of(tenant)
    day = time.strftime("%Y-%m-%d", time.localtime(clock[0] + 10 * registry.DAY))
    m = engine.create(tenant, o, "Launch a webinar", None, {"offer": "The 30-day client sprint", "niche": "fitness coaches",
                                                            "price": 997, "event_date": day})
    st = S(m)
    usp = queue.get(tenant, st["chain"]["queue_ids"][0])["body"]
    assert "fitness coaches" in usp and "business owners" not in usp
    approve(tenant, o, st["chain"]["queue_ids"])
    st = S(engine.get(tenant, m["id"]))
    ads = [queue.get(tenant, q) for q in st["promote"]["queue_ids"]]
    assert ads and st["promote"]["status"] == "waiting_approval"
    when = registry.when(m["context"]["answers"]["event_date"])
    for a in ads:
        assert "Book a call" not in a["body"] and "regist" in a["body"].lower(), a["body"]
        assert "The 30-day client sprint" in a["body"] and when.split(" ")[0] in a["body"], a["body"]
        assert "fitness coaches" in a["body"].lower()


# ============================================================================ client weekly ops
def test_a_client_gets_one_weekly_report_a_week_not_two(tenant, clock):
    from operator_ai import client_ops

    o = owner_of(tenant)
    with tenant.db() as c:
        cid = c.execute("INSERT INTO clients(name,niche,offer,price,metrics,demo,created,status,email,contact_name) "
                        "VALUES(?,?,?,?,?,0,?,?,?,?)", ("Northwind Fitness", "fitness coaches", "DM setting", 2500, "{}",
                                                        time.time(), "active", "nw@example.invalid", "Nora West")).lastrowid
    client_ops.record_metrics(tenant, cid, {"leads": 42, "booked": 6, "showed": 2, "closed": 1}, actor="o")
    lt = time.localtime(time.time() + 7 * 86400)
    clock[0] = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday - lt.tm_wday, 10, 0, 0, 0, 0, -1))  # a Monday, 10:00
    m = engine.create(tenant, o, "Send my client a weekly report", "client_weekly_ops", {"client_id": cid,
                                                                                        "weekday": "Wednesday"})
    with tenant.db() as c:
        assert c.execute("SELECT report_weekday FROM clients WHERE id=?", (cid,)).fetchone()[0] == 2  # one report day
    approve(tenant, o, S(m)["weekly"]["queue_ids"])  # this week's report, asked for on Monday
    wed = S(engine.get(tenant, m["id"]))["weekly"]["due"]
    assert time.localtime(wed).tm_wday == 2 and wed - clock[0] < 3 * 86400
    for _ in range(2):  # this week's Wednesday, then next week's
        day = S(engine.get(tenant, m["id"]))["weekly"]["due"]
        client_ops.tick(tenant, day - 3600)  # the client ops loop runs first on report day (the scheduler's order)
        clock[0] = day + 60
        engine.tick(tenant, clock[0])
    with tenant.db() as c:
        weeks = [r[0] for r in c.execute("SELECT json_extract(meta,'$.period_start') FROM queue WHERE kind='client_report' "
                                         "AND json_extract(meta,'$.client_id')=? AND json_extract(meta,'$.report')='weekly'",
                                         (cid,))]
    assert len(weeks) == 2 and len(set(weeks)) == 2, weeks  # one report a week
    wk = S(engine.get(tenant, m["id"]))["weekly"]
    latest = client_ops.week_report(tenant, cid, clock[0])
    assert wk["status"] == "waiting_approval" and latest in wk["queue_ids"]
    body = queue.get(tenant, latest)["body"]
    assert "Hi Nora" in body and "Calls booked: 6" in body


def test_the_first_weekly_report_with_no_numbers_says_so_honestly(tenant):
    stu = mk_user(tenant, "student", "wes")
    certify(tenant, stu)
    p = P.add_prospect(tenant, stu, {"name": "Jamie Trainer", "email": "jamie@example.invalid", "niche": "fitness coaches"},
                       "legitimate_interest", "From their public website")["prospect"]
    engine.create(tenant, stu, "Get my first client", "first_client", ANS)
    P.mark_won(tenant, p["id"], actor(stu), via="proposal")
    with tenant.db() as c:
        cid = c.execute("SELECT client_id FROM prospects WHERE id=?", (p["id"],)).fetchone()[0]
    m = engine.create(tenant, stu, "Send my client a weekly report", "client_weekly_ops", {"client_id": cid})
    body = queue.get(tenant, S(m)["weekly"]["queue_ids"][0])["body"]
    assert body.startswith("Hi Jamie") and "No numbers recorded this week yet" in body
    assert "Reviewed this week's numbers" not in body and "No stage is below its benchmark" not in body
    assert "Get this week's numbers in" in body


# ============================================================================ escalate when stuck
def test_a_mission_waiting_days_on_its_person_is_escalated_to_their_coach_once(tenant, clock):
    from operator_ai import escalations

    coach = mk_user(tenant, "coach", "cora")
    stu = mk_user(tenant, "student", "sid")
    with tenant.db() as c:
        c.execute("UPDATE students SET coach_id=? WHERE id=?", (coach["id"], stu["student_id"]))
    certify(tenant, stu)
    P.save_icp(tenant, stu, {"niche": "fitness coaches", "offer": "done-for-you DM setting", "sender": SENDER})
    m = engine.create(tenant, stu, "Get my first client", "first_client", ANS)  # no prospects: the list step waits
    mid = m["id"]
    assert S(m)["prospects"]["status"] == "blocked_human"
    for _ in range(8):  # the list step rechecks every 15 minutes and finds nothing new: still the same wait
        clock[0] += 12 * 3600
        engine.tick(tenant, clock[0])
    with tenant.db() as c:
        rows = db.rows(c.execute("SELECT * FROM escalations WHERE mission_id=? AND kind='mission_stalled'", (mid,)))
    assert len(rows) == 1 and rows[0]["assignee_user_id"] == coach["id"], rows
    assert "Build your prospect list" in rows[0]["detail"] and "Write your offer document" in rows[0]["detail"]
    assert "3 days" in rows[0]["detail"]
    clock[0] += 2 * 86400
    engine.tick(tenant, clock[0])
    with tenant.db() as c:
        assert c.execute("SELECT COUNT(*) FROM escalations WHERE mission_id=? AND kind='mission_stalled'",
                         (mid,)).fetchone()[0] == 1
    assert any(a["kind"] == "escalation" for a in engine.next_actions(tenant, coach))
    assert escalations.get(tenant, rows[0]["id"])["kind"] == "mission_stalled"


# ============================================================================ restart
def test_a_step_interrupted_by_a_restart_runs_again(tenant):
    o = owner_of(tenant)
    m = engine.create(tenant, o, "Fix my bottleneck", "fix_bottleneck",
                      {"dms_sent": 600, "replies": 54, "booked": 10, "showed": 3}, start=False)
    mid = m["id"]
    engine.advance(tenant, mid)  # the intake job is queued
    with tenant.db() as c:  # a worker claims it and starts the step... then the server stops
        job = c.execute("UPDATE jobs SET status='running' WHERE kind='missions.step' AND status='queued' RETURNING id").fetchone()
        c.execute("UPDATE mission_steps SET status='running' WHERE mission_id=? AND skey='intake'", (mid,))
    assert job
    jobs.requeue_orphans(tenant)  # what the workers do on start
    engine.pump(tenant, mid)
    st = S(engine.get(tenant, mid))
    assert st["intake"]["status"] == "done" and st["diagnose"]["status"] == "done", st["intake"]


def test_a_step_whose_job_was_lost_is_picked_up_by_the_tick(tenant, clock):
    o = owner_of(tenant)
    m = engine.create(tenant, o, "Fix my bottleneck", "fix_bottleneck",
                      {"dms_sent": 600, "replies": 54, "booked": 10, "showed": 3}, start=False)
    mid = m["id"]
    engine.advance(tenant, mid)
    with tenant.db() as c:  # the job row ended (failed outside the step) while the step still says it's starting
        c.execute("UPDATE jobs SET status='failed' WHERE kind='missions.step'")
    assert S(engine.get(tenant, mid))["intake"]["status"] == "ready"
    clock[0] += 20 * 60
    engine.tick(tenant, clock[0])
    st = S(engine.get(tenant, mid))
    assert st["intake"]["status"] == "done" and st["diagnose"]["status"] == "done"
    assert any(e["kind"] == "step" and "picked up again" in (e["detail"].get("reason") or "")
               for e in registry.events_of(tenant, mid))
