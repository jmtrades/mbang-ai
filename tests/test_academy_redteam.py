"""Academy red-team regressions: ways to game certification evidence, leak exam answers or crash the API.
Each test was written against a real hole and fails on the code before the fix."""
from __future__ import annotations

import time

from operator_ai import db, queue, tenancy
from operator_ai.academy import certify, common, quiz, srs, team
from operator_ai.academy import text as T
from tests.conftest import demo_login


def _demo_user(t, username: str) -> dict:
    with t.db() as c:
        r = c.execute("SELECT id, username, display, role, coach_id, student_id FROM users WHERE username=?", (username,)).fetchone()
    return dict(r)


def _items(t, pool: str = "practice", kind: str | None = None) -> list[dict]:
    q = ("SELECT i.* FROM academy_quiz_items i JOIN academy_lessons l ON l.id=i.lesson_id WHERE i.status='published' "
         "AND l.status='published' AND i.pool=?")
    args = [pool]
    if kind:
        q += " AND i.kind=?"
        args.append(kind)
    with t.db() as c:
        return [quiz._row(dict(r)) for r in c.execute(q + " ORDER BY i.id", args)]


def _numeric_cloze(t, pool: str = "practice") -> dict:
    return next(it for it in _items(t, pool, "cloze") if T.find_quantities(T.words_to_digits(str(it["answer"]))))


def _term_cloze(t, pool: str = "practice") -> dict:
    return next(it for it in _items(t, pool, "cloze") if not T.find_quantities(T.words_to_digits(str(it["answer"]))))


# --------------------------------------------------------------------------- grading cannot be shotgunned
def test_shotgun_answers_never_grade_as_right(demo_tenant):
    t = demo_tenant
    num = _numeric_cloze(t)
    every_number = " ".join(str(n) for n in range(0, 400)) + " hours"
    assert not quiz.grade(t, num, every_number)["correct"]
    assert not T.same_answer(every_number, str(num["answer"]), num["accept"])
    term = _term_cloze(t)
    with t.db() as c:
        lesson = c.execute("SELECT key_points FROM academy_lessons WHERE id=?", (term["lesson_id"],)).fetchone()
    whole_lesson = " ".join(k["text"] for k in db.jload(lesson["key_points"], []))
    blob = whole_lesson + " " + term["explanation"]
    assert not quiz.grade(t, term, blob)["correct"]
    # honest answers still pass: the number alone, the number with its unit, the term itself
    assert quiz.grade(t, num, str(num["answer"]))["correct"]
    assert quiz.grade(t, term, str(term["answer"]))["correct"]
    assert quiz.grade(t, term, f"the {term['answer']}")["correct"]


def test_exam_cannot_be_passed_with_shotgun_answers(client):
    demo_login(client, "student")
    t = tenancy.get("demo")
    student = _demo_user(t, "demo-student")
    for lv in ("L1", "L2"):
        certify.award(t, student, "growth_operator", lv, [], "test")
    ex = client.post("/t/demo/api/academy/exams", json={"path": "growth_operator"}).json()
    blob = " ".join(str(n) for n in range(0, 300)) + " " + " ".join(
        (quiz.load(t, i["id"])["explanation"] or "") for i in ex["items"])
    answers = {}
    for i in ex["items"]:
        it = quiz.load(t, i["id"])
        answers[str(i["id"])] = blob[:3900] if it["kind"] == "cloze" else _right(it)
    res = client.post(f"/t/demo/api/academy/exams/{ex['id']}/submit", json={"answers": answers}).json()
    n_cloze = sum(1 for i in ex["items"] if i["kind"] == "cloze")
    assert res["right"] == len(ex["items"]) - n_cloze


def _right(it: dict):
    return list(it["answer"]) if it["kind"] == "order" else it["answer"]


# --------------------------------------------------------------------------- exam cooldown cannot be dodged
def test_an_abandoned_exam_counts_as_a_failed_attempt(client):
    demo_login(client, "student")
    t = tenancy.get("demo")
    student = _demo_user(t, "demo-student")
    for lv in ("L1", "L2"):
        certify.award(t, student, "growth_operator", lv, [], "test")
    ex = client.post("/t/demo/api/academy/exams", json={"path": "growth_operator"}).json()
    assert len(ex["items"]) == 30
    with t.db() as c:  # read every question, never submit, let the clock run out
        c.execute("UPDATE academy_exams SET started=?, deadline=? WHERE id=?", (time.time() - 3600, time.time() - 120, ex["id"]))
    assert not certify.exam_open(t, student["id"])
    r = client.post("/t/demo/api/academy/exams", json={"path": "growth_operator"})
    assert r.status_code == 429, r.text  # the 24h cooldown applies, a fresh sample is not handed out
    with t.db() as c:
        row = c.execute("SELECT status, submitted, score FROM academy_exams WHERE id=?", (ex["id"],)).fetchone()
    assert row["status"] == "expired" and row["submitted"] is not None and row["score"] == 0
    assert client.get(f"/t/demo/api/academy/exams/{ex['id']}").json().get("items") is None
    assert client.post(f"/t/demo/api/academy/exams/{ex['id']}/submit", json={"answers": {}}).status_code == 409


# --------------------------------------------------------------------------- mastery cannot be pumped
def test_answering_the_same_question_again_and_again_does_not_pump_mastery(client):
    demo_login(client, "closer")
    t = tenancy.get("demo")
    closer = _demo_user(t, "demo-closer")
    it = _numeric_cloze(t)
    url = f"/t/demo/api/academy/items/{it['id']}/answer"
    first = client.post(url, json={"answer": str(it["answer"]), "ms": 4000}).json()
    for _ in range(25):
        r = client.post(url, json={"answer": str(it["answer"]), "ms": 4000}).json()
        assert r["correct"]
    after = srs.mastery(t, closer["id"])[it["skill"]]
    assert after["chance"] == first["mastery"]["chance"], "repeat answers moved mastery"
    assert r["mastery"]["counted"] is False and "already" in r["mastery"]["note"]


# --------------------------------------------------------------------------- L2 write-it evidence
def test_five_copies_of_one_write_it_answer_are_one_answer(demo_tenant):
    t = demo_tenant
    setter = _demo_user(t, "demo-setter")
    w = next(it for it in _items(t, kind="write") if it["answer"]["asset"] == "dm")
    base = "Loved your post about referral nights at your clinic. What's working best for new bookings right now?"
    variants = [base, base + " ", base.replace("Loved", "loved"), base + " :)", base.replace("right now", "right now ")]
    with t.db() as c:
        for v in variants:
            c.execute("INSERT INTO academy_attempts(user_id,quiz_item_id,answer,correct,created,context,score) "
                      "VALUES(?,?,?,?,?,?,?)", (setter["id"], w["id"], db.jdump(v), 1, db.now(), "quiz", 92.0))
    crit = next(c for c in certify.criteria(t, setter, "setter", "L2") if c["key"] == "write_items")
    assert crit["have"] == 1 and not crit["met"]


# --------------------------------------------------------------------------- exam answers stay with the owner
def test_coaches_never_see_exam_answers(client):
    demo_login(client, "coach")
    t = tenancy.get("demo")
    exam_ids = {it["id"] for it in _items(t, "exam")}
    assert exam_ids
    data = client.get("/t/demo/api/academy/admin/drafts?status=published").json()
    leaked = [i for L in data["lessons"] for i in L["items"] if i["id"] in exam_ids and i.get("answer") is not None]
    assert not leaked
    # exam attempts by the coach's learners never surface an exam answer in the misconceptions panel
    student = _demo_user(t, "demo-student")
    ex_item = next(it for it in _items(t, "exam") if it["kind"] != "order")
    with t.db() as c:
        for _ in range(3):
            c.execute("INSERT INTO academy_attempts(user_id,quiz_item_id,answer,correct,created,context,chosen_option) "
                      "VALUES(?,?,?,?,?,?,?)", (student["id"], ex_item["id"], '"x"', 0, db.now(), "exam", "x"))
    mis = client.get("/t/demo/api/academy/team/misconceptions").json()["items"]
    assert all(m["id"] not in exam_ids for m in mis)
    demo_login(client, "owner")
    data = client.get("/t/demo/api/academy/admin/drafts?status=published").json()
    assert any(i["id"] in exam_ids and i.get("answer") is not None for L in data["lessons"] for i in L["items"])


# --------------------------------------------------------------------------- blind role-play grading
def test_signoff_queue_is_blind_so_calibration_means_something(client):
    demo_login(client, "coach")
    t = tenancy.get("demo")
    student = _demo_user(t, "demo-student")
    score = {"total": 63, "compliance_flags": 0, "scenario_key": "think_call", "mode": "rules", "seed": 1, "signoff": None,
             "criteria": [{"key": "discovery", "label": "Discovery", "points": 20, "max": 30, "evidence": "asked 2 questions"}]}
    with t.db() as c:
        rp = c.execute("INSERT INTO roleplays(user_id,scenario_key,transcript,state,score,status,created,updated) "
                       "VALUES(?,?,?,?,?,?,?,?)", (student["id"], "think_call", "[]", "{}", db.jdump(score), "complete",
                                                   db.now(), db.now())).lastrowid
    row = next(x for x in client.get("/t/demo/api/academy/team/signoffs").json()["items"] if x["id"] == rp)
    assert row.get("total") is None and all("points" not in c for c in row["criteria"])
    assert "63" not in str(row)
    r = client.post(f"/t/demo/api/academy/team/roleplays/{rp}/signoff", json={"score": 90}).json()
    assert r["rules_score"] == 63 and r["agreement"] is False  # revealed after grading


# --------------------------------------------------------------------------- managers for setters and closers
def test_owner_links_setters_and_closers_to_a_coach(client):
    demo_login(client, "owner")
    t = tenancy.get("demo")
    coach, closer, student = (_demo_user(t, u) for u in ("demo-coach", "demo-closer", "demo-student"))
    assert closer["id"] not in (common.learner_ids(t, coach) or [])
    r = client.post("/t/demo/api/academy/team/managers", json={"manager_id": coach["id"], "member_id": closer["id"]})
    assert r.status_code == 200, r.text
    assert closer["id"] in common.learner_ids(t, coach)
    listing = client.get("/t/demo/api/academy/team/managers").json()
    assert any(x["manager_id"] == coach["id"] and x["member_id"] == closer["id"] for x in listing["links"])
    assert {u["id"] for u in listing["managers"]} >= {coach["id"]}
    # nonsense is refused: yourself, a non-coach manager, an owner as member, unknown users
    for body in ({"manager_id": coach["id"], "member_id": coach["id"]},
                 {"manager_id": closer["id"], "member_id": student["id"]},
                 {"manager_id": coach["id"], "member_id": 1}):
        assert client.post("/t/demo/api/academy/team/managers", json=body).status_code == 422, body
    assert client.post("/t/demo/api/academy/team/managers", json={"manager_id": coach["id"], "member_id": 99999}).status_code == 404
    assert client.post("/t/demo/api/academy/team/managers", json={"manager_id": "x"}).status_code == 422
    demo_login(client, "coach")
    assert client.post("/t/demo/api/academy/team/managers", json={"manager_id": coach["id"], "member_id": student["id"]}).status_code == 403
    assert client.delete(f"/t/demo/api/academy/team/managers/{coach['id']}/{closer['id']}").status_code == 403
    demo_login(client, "owner")
    assert client.delete(f"/t/demo/api/academy/team/managers/{coach['id']}/{closer['id']}").status_code == 200
    assert closer["id"] not in common.learner_ids(t, coach)


# --------------------------------------------------------------------------- malformed input is 422/404, never 500
def test_huge_ids_and_refs_never_crash(client):
    demo_login(client, "owner")
    t = tenancy.get("demo")
    student = _demo_user(t, "demo-student")
    big = "9" * 30
    r = client.post("/t/demo/api/academy/assign", json={"user_id": student["id"], "kind": "lesson", "ref": big})
    assert r.status_code == 422, r.text
    for path in (f"/academy/lessons/{big}", f"/academy/team/{big}", f"/academy/exams/{big}", f"/academy/lessons/{big}/quiz"):
        assert client.get(f"/t/demo/api{path}").status_code in (404, 422), path
    for path, body in ((f"/academy/items/{big}/answer", {"answer": "x"}), (f"/academy/lessons/{big}/complete", None),
                       (f"/academy/review/{big}/grade", {"grade": 3}), (f"/academy/certs/{big}/revoke", {"reason": "abc"}),
                       (f"/academy/admin/lessons/{big}/decide", {"decision": "publish"})):
        assert client.post(f"/t/demo/api{path}", json=body).status_code in (404, 422), path
    r = client.post("/t/demo/api/academy/assign", json={"user_id": int(big), "kind": "path", "ref": "setter"})
    assert r.status_code in (404, 422)


# --------------------------------------------------------------------------- closer evidence by name only
def test_closer_evidence_never_matches_an_empty_rep(demo_tenant):
    t = demo_tenant
    closer = dict(_demo_user(t, "demo-closer"), display="")
    with t.db() as c:
        for _ in range(12):
            c.execute("INSERT INTO calls(title,rep,parties,transcript,status,review,created) VALUES(?,?,?,?,?,?,?)",
                      ("Discovery (DEMO)", "", "[]", "", "reviewed", db.jdump({"overall": 95}), db.now()))
    th = common.path_def(t, "closer")["thresholds"]["L3"]
    assert certify._real_work(t, closer, "closer", th, 0)["have"].startswith("0 calls")



# --------------------------------------------------------------------------- exam questions are not practice questions
def test_exam_pool_is_separate_from_practice_and_one_question_per_line(client):
    demo_login(client, "student")
    t = tenancy.get("demo")
    student = _demo_user(t, "demo-student")
    practice_lines = {quiz.source_key(it["citation"]) for it in _items(t, "practice")}
    def shown(it):  # what a learner sees: the prompt and the options
        return T.norm_cmp(it["prompt"]) + "|" + "|".join(sorted(T.norm_cmp(o) for o in it.get("options") or []))

    practice_prompts = {shown(it) for it in _items(t, "practice")}
    exam = _items(t, "exam")
    assert exam
    assert not [it for it in exam if quiz.source_key(it["citation"]) in practice_lines]
    assert not [it for it in exam if shown(it) in practice_prompts]
    for lv in ("L1", "L2"):
        certify.award(t, student, "growth_operator", lv, [], "test")
    ex = client.post("/t/demo/api/academy/exams", json={"path": "growth_operator"}).json()
    lines = [quiz.source_key(quiz.load(t, i["id"])["citation"]) for i in ex["items"]]
    prompts = [shown(i) for i in ex["items"]]
    assert len(set(lines)) == len(lines) == 30 and len(set(prompts)) == 30


# --------------------------------------------------------------------------- closed-book while the exam is open
def test_lessons_practice_and_drill_answers_pause_during_an_exam(client):
    demo_login(client, "student")
    t = tenancy.get("demo")
    student = _demo_user(t, "demo-student")
    lid = client.get("/t/demo/api/academy").json()["next_lesson"]["id"]
    item = _items(t, "practice", "cloze")[0]
    drill = client.get("/t/demo/api/academy/drills").json()["cards"][0]
    assert "answer" not in drill
    assert client.get(f"/t/demo/api/academy/drills/{drill['card_id']}/answer").status_code == 200
    for lv in ("L1", "L2"):
        certify.award(t, student, "growth_operator", lv, [], "test")
    ex = client.post("/t/demo/api/academy/exams", json={"path": "growth_operator"}).json()
    assert client.get(f"/t/demo/api/academy/lessons/{lid}").status_code == 423
    assert client.get(f"/t/demo/api/academy/lessons/{lid}/quiz").status_code == 423
    assert client.post(f"/t/demo/api/academy/items/{item['id']}/answer", json={"answer": "x"}).status_code == 423
    assert client.get(f"/t/demo/api/academy/drills/{drill['card_id']}/answer").status_code == 423
    page = client.get("/t/demo/api/drills").json()["cards"]
    assert all(c["citation"] is None and "paused" in c["answer"] for c in page)
    assert client.get("/t/demo/api/academy").status_code == 200 and client.get("/t/demo/api/academy").json()["exam_open"]
    big = {str(i["id"]): "x" * 5000 for i in ex["items"]}
    assert client.post(f"/t/demo/api/academy/exams/{ex['id']}/submit", json={"answers": big}).status_code == 422
    client.post(f"/t/demo/api/academy/exams/{ex['id']}/submit", json={"answers": {}})
    assert client.get(f"/t/demo/api/academy/lessons/{lid}").status_code == 200


# --------------------------------------------------------------------------- training impact reads real outreach
def test_training_impact_includes_reply_rate_from_real_sends(demo_tenant):
    from operator_ai import events

    t = demo_tenant
    owner = {"id": 0, "role": "owner"}
    people = [u for u in team._learners(t, owner) if srs.mastery(t, u["id"])]
    assert len(people) >= 2
    for k, u in enumerate(people):
        for i in range(6):
            events.emit(t, "item_sent", {"queue_id": 1000 + i, "kind": "dm_opener", "channel": "manual", "owner_user_id": u["id"]})
        for i in range(k + 1):
            events.emit(t, "reply_received", {"party_type": "prospect", "party_id": i, "owner_user_id": u["id"],
                                              "classification": "positive"})
    m = next(x for x in team.impact(t, owner)["metrics"] if x["key"] == "reply_rate")
    assert m["n"] == len(people) and "Reply rate" in m["label"]
    assert "caused" in m["note"] or "Not enough data" in m["note"]


# --------------------------------------------------------------------------- the coach pauses on every way in
def test_coach_chat_and_upload_pause_during_an_exam_too(client):
    demo_login(client, "student")
    t = tenancy.get("demo")
    student = _demo_user(t, "demo-student")
    for lv in ("L1", "L2"):
        certify.award(t, student, "growth_operator", lv, [], "test")
    ex = client.post("/t/demo/api/academy/exams", json={"path": "growth_operator"}).json()
    assert client.post("/t/demo/api/coach/ask", json={"question": "What is the kill rule?"}).status_code == 423
    assert client.post("/t/demo/api/chat", json={"text": "What is the kill rule?"}).status_code == 423
    r = client.post("/t/demo/api/chat/upload", files={"file": ("notes.txt", b"What is the kill rule?", "text/plain")})
    assert r.status_code == 423
    client.post(f"/t/demo/api/academy/exams/{ex['id']}/submit", json={"answers": {}})
    assert client.post("/t/demo/api/chat", json={"text": "What is the kill rule?"}).status_code == 200


# --------------------------------------------------------------------------- assignment notes pass the guardrails
def test_assignment_note_that_breaks_a_rule_never_reaches_the_learner(client):
    demo_login(client, "coach")
    t = tenancy.get("demo")
    student = _demo_user(t, "demo-student")
    with t.db() as c:
        before = c.execute("SELECT COUNT(*) FROM academy_assignments").fetchone()[0]
    r = client.post("/t/demo/api/academy/assign", json={"user_id": student["id"], "kind": "path", "ref": "setter",
                                                         "note": "Tell them we guarantee they will make $10k a month."})
    assert r.status_code == 422 and "rule" in r.json()["detail"]
    with t.db() as c:
        assert c.execute("SELECT COUNT(*) FROM academy_assignments").fetchone()[0] == before
    demo_login(client, "student")
    assert all("guarantee" not in (a.get("note") or "") for a in client.get("/t/demo/api/academy").json()["assignments"])


def test_a_coach_sitting_an_exam_cannot_read_lessons_through_admin(client):
    demo_login(client, "owner")
    t = tenancy.get("demo")
    coach = _demo_user(t, "demo-coach")
    assert client.put("/t/demo/api/academy/admin/paths/coach", json={"levels": {"L3": {"exam_items": 10}}}).status_code == 200
    for lv in ("L1", "L2"):
        certify.award(t, coach, "coach", lv, [], "test")
    demo_login(client, "coach")
    assert client.get("/t/demo/api/academy/admin/drafts?status=published").status_code == 200
    ex = client.post("/t/demo/api/academy/exams", json={"path": "coach"}).json()
    assert len(ex["items"]) == 10
    assert client.get("/t/demo/api/academy/admin/drafts?status=published").status_code == 423
    assert client.get("/t/demo/api/academy/team/misconceptions").status_code == 423
    client.post(f"/t/demo/api/academy/exams/{ex['id']}/submit", json={"answers": {}})
    assert client.get("/t/demo/api/academy/admin/drafts?status=published").status_code == 200


# --------------------------------------------------------------------------- A9: setter decide rights need Setter L1
def test_setter_decide_rights_gate_is_off_by_default_and_needs_setter_l1_when_on(client):
    demo_login(client, "owner")
    t = tenancy.get("demo")
    setter, student = _demo_user(t, "demo-setter"), _demo_user(t, "demo-student")

    def team_dm():
        return queue.add(t, "dm_opener", "Saw your post about referral nights. How are they going?", channel="instagram_dm",
                         meta={"prospect_facing": True, "lead_tier": "cold"})["id"]

    q1, q2, q3 = team_dm(), team_dm(), team_dm()
    demo_login(client, "setter")
    assert client.post(f"/t/demo/api/queue/{q1}/approve").status_code == 200  # off: unchanged behaviour
    assert client.put("/t/demo/api/academy/admin/settings", json={"require_setter_cert_for_decisions": True}).status_code == 403
    demo_login(client, "owner")
    assert client.put("/t/demo/api/academy/admin/settings", json={}).status_code == 422
    assert client.put("/t/demo/api/academy/admin/settings", json={"require_setter_cert_for_decisions": True}).json() == \
        {"require_setter_cert_for_decisions": True}
    assert client.get("/t/demo/api/academy/admin/settings").json()["require_setter_cert_for_decisions"] is True
    assert certify.is_certified(t, setter["id"], "setter", "L1")  # the demo setter holds it
    demo_login(client, "setter")
    assert client.post(f"/t/demo/api/queue/{q2}/approve").status_code == 200
    with t.db() as c:
        c.execute("UPDATE certifications SET status='revoked' WHERE user_id=? AND path_key='setter'", (setter["id"],))
    r = client.post(f"/t/demo/api/queue/{q3}/approve")
    assert r.status_code == 403 and "Setter L1" in r.json()["detail"]
    assert queue.get(t, q3)["status"] == "pending"
    assert certify.approval_gate(t, student["id"], "dm_opener", {}, "student") is None  # students have no setter rights
