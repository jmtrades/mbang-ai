"""Academy (audit 2026-10-27 training findings A0, A3-A6, A9-A15): cited lessons from the owner's material,
rules-based questions, one spaced-repetition store with Elo mastery, certificates with real-work evidence,
the optional approval gate, exams, team and admin views. Offline, AI off unless a test turns on a fake model."""
from __future__ import annotations

import shutil
import time

import pytest

from operator_ai import auth, call_review, config, db, guardrails, index, llm, queue, runtime, settings, tenancy
from operator_ai.academy import build, certify, common, quiz, seed, srs
from operator_ai.academy import text as T
from tests.conftest import demo_login, login
from tests.fakes import FakeModel

EXTRA_SOP = """Prospect Follow-up SOP (DEMO)
FICTIONAL TRAINING MATERIAL FOR TESTS

The follow-up rules
Rule 1: Send the first follow-up after forty-eight hours, never sooner.
Rule 2: Every follow-up must give value: a tip, a short video or one question.
Never send a follow-up that only says "just checking in".
Keep each follow-up under sixty words so it reads well on a phone.
My students make $10k a month in 30 days, guaranteed.

Handling objections in the inbox
When someone says they are too busy right now, ask which day next week suits them better.
When a lead says send me some info, ask what they want the info to help them decide.
Always log the objection in the sheet so the setter can see patterns over a month.
"""
UNSAFE = "My students make $10k a month in 30 days, guaranteed."


# --------------------------------------------------------------------------- helpers
def _corpus(env, extra: str = EXTRA_SOP):
    """Real-workspace material: the fictional fixture copied into inputs/ plus one extra SOP."""
    inputs = env / "inputs"
    shutil.copytree(config.FIXTURE_CORPUS, inputs, dirs_exist_ok=True)
    (inputs / "sops" / "followup_sop.txt").write_text(extra)
    index.invalidate()
    assert not config.get().using_fixture_corpus
    return inputs


def _user(t, username: str, role: str, **kw) -> dict:
    uid = auth.create_user(t, username, "pw-" + username + "-123", role, display=f"{username.title()} (DEMO)", **kw)
    return common.user_row(t, uid)


def _login_as(client, tid: str, username: str):
    login(client, tid, username, "pw-" + username + "-123")


def _demo_user(t, username: str) -> dict:
    with t.db() as c:
        r = c.execute("SELECT id, username, display, role, coach_id, student_id FROM users WHERE username=?", (username,)).fetchone()
    return dict(r)


def _wait_run(t, run_id: int, timeout: float = 30.0) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        r = runtime.get(t, run_id)
        if r and r["status"] in ("done", "error"):
            return r
        time.sleep(0.05)
    raise AssertionError("build did not finish")


def _correct_answer(it: dict):
    if it["kind"] == "order":
        return list(it["answer"])
    return it["answer"]


def _published_items(t, pool: str = "practice", kind: str | None = None) -> list[dict]:
    q = ("SELECT i.* FROM academy_quiz_items i JOIN academy_lessons l ON l.id=i.lesson_id WHERE i.status='published' "
         "AND l.status='published' AND i.pool=?")
    args = [pool]
    if kind:
        q += " AND i.kind=?"
        args.append(kind)
    with t.db() as c:
        return [quiz._row(dict(r)) for r in c.execute(q + " ORDER BY i.id", args)]


# --------------------------------------------------------------------------- A0: no fictional material in a real workspace
def test_real_workspace_refuses_fixture_corpus_and_stays_empty(client):
    login(client)
    r = client.post("/t/owner/api/academy/build")
    assert r.status_code == 409 and "Load your course material first" in r.json()["detail"]
    t = tenancy.get("owner")
    with pytest.raises(common.Refused):
        build.start(t, "test", sync=True)
    view = client.get("/t/owner/api/academy").json()
    assert view["built"]["published"] == 0 and view["material"]["ok"] is False
    assert view["material"]["demo_material"] is False
    # the demo seeder never writes to a real workspace
    assert seed.seed_demo(t).get("skipped")
    with t.db() as c:
        for table in ("academy_lessons", "academy_quiz_items", "certifications", "academy_builds", "drills"):
            assert c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table


def test_empty_knowledge_base_tenant_refuses(env):
    tenancy.ensure_defaults()
    t, _ = tenancy.create("clientco", "Client Co", kb_scope="empty")
    st = common.material_state(t)
    assert not st["ok"] and st["reason"].startswith("Load your course material first")
    with pytest.raises(common.Refused):
        build.start(t, "test", sync=True)


# --------------------------------------------------------------------------- A5: demo lessons are cited and badged
def test_demo_lessons_are_published_cited_to_exact_lines_and_badged(client):
    demo_login(client, "student")
    view = client.get("/t/demo/api/academy").json()
    assert view["material"]["demo_material"] is True and view["built"]["published"] >= 10
    t = tenancy.get("demo")
    idx = index.for_tenant(t)
    with t.db() as c:
        ids = [r["id"] for r in c.execute("SELECT id FROM academy_lessons WHERE status='published'")]
    checked = 0
    for lid in ids:
        L = client.get(f"/t/demo/api/academy/lessons/{lid}").json()
        assert L["demo_material"] is True and L["gen_mode"] == "rules" and "AI not used" in L["gen_label"]
        cites = {c["id"]: c for c in L["citations"]}
        assert 2 <= len(L["key_points"]) <= 7
        for kp in L["key_points"] + L["mistakes"] + L["example"]:
            c = cites[kp["cite"]]
            a, _, b = c["lines"].partition("-")
            assert a == b, "every point cites one exact line"
            line = idx.docs[c["file"]].lines[int(a) - 1]
            assert T.norm_cmp(kp["text"]) in T.norm_cmp(T.clean_spoken(line)) or T.norm_cmp(kp["text"]) in T.norm_cmp(line)
            assert not guardrails.blocked(guardrails.check(kp["text"], "plain"))
            checked += 1
        assert all("answer" not in it and "explanation" not in it for it in L["checks"])
    assert checked > 40
    # the citation chip opens the exact line in the source viewer
    first = client.get(f"/t/demo/api/academy/lessons/{ids[0]}").json()["citations"][0]
    src = client.get("/t/demo/api/source", params={"file": first["file"], "lines": first["lines"]}).json()
    assert [ln["n"] for ln in src["lines"] if ln["hit"]] == [int(first["lines"].split("-")[0])]


# --------------------------------------------------------------------------- A3 + A5: build on real material, traced, drafts
def test_build_screens_unsafe_sentences_and_starts_as_drafts(client, env):
    _corpus(env)
    login(client)
    t = tenancy.get("owner")
    r = client.post("/t/owner/api/academy/build")
    assert r.status_code == 200, r.text
    run = _wait_run(t, r.json()["run_id"])
    assert run["status"] == "done" and run["agent"] == "academy_build" and run["ai_mode"] == "rules"
    assert len(run["steps"]) >= len(build.PLAN)
    again = client.post("/t/owner/api/academy/build")
    assert again.status_code in (200, 409)  # never a 500, even back to back
    if again.status_code == 200:
        _wait_run(t, again.json()["run_id"])  # never leave a background build running into the next test
    with t.db() as c:
        lessons = db.rows(c.execute("SELECT * FROM academy_lessons"))
        items = db.rows(c.execute("SELECT * FROM academy_quiz_items"))
    assert lessons and all(L["status"] == "draft" and not L["demo"] for L in lessons)
    blob = " ".join([L["key_points"] + L["mistakes"] + L["example"] + L["body"] for L in lessons] +
                    [f"{i['prompt']} {i['answer']} {i['explanation']} {i['options']}" for i in items])
    # the unsafe course line never becomes a key point, a question, an answer or an explanation
    assert UNSAFE not in blob and "in 30 days, guaranteed" not in blob
    drafts = client.get("/t/owner/api/academy/admin/drafts").json()
    unsafe = [s for s in drafts["unsafe"] if "10k" in s["text"]]
    assert unsafe and unsafe[0]["label"].startswith("Skipped:") and unsafe[0]["file"] == "sops/followup_sop.txt"
    line_no = EXTRA_SOP.splitlines().index(UNSAFE) + 1
    assert unsafe[0]["line"] == line_no and f"(line {line_no})" in unsafe[0]["label"]
    assert drafts["lessons"] and drafts["lessons"][0]["source"]["passage"]
    assert any(n["file"] == "course/transcripts/vid_j1k2l3.txt" for n in drafts["too_noisy"])


def test_draft_lessons_hidden_until_the_owner_publishes(client, env):
    _corpus(env)
    login(client)
    t = tenancy.get("owner")
    _user(t, "stu1", "student")
    _user(t, "coach1", "coach")
    build.start(t, "test", sync=True)
    with t.db() as c:
        lid = c.execute("SELECT id FROM academy_lessons WHERE status='draft' ORDER BY id LIMIT 1").fetchone()["id"]
    _login_as(client, "owner", "coach1")
    assert client.post("/t/owner/api/academy/build").status_code == 403
    assert client.post(f"/t/owner/api/academy/admin/lessons/{lid}/decide", json={"decision": "publish"}).status_code == 403
    assert client.get(f"/t/owner/api/academy/lessons/{lid}").status_code == 200  # coaches may preview drafts
    _login_as(client, "owner", "stu1")
    assert client.get(f"/t/owner/api/academy/lessons/{lid}").status_code == 404
    assert client.post(f"/t/owner/api/academy/admin/lessons/{lid}/decide", json={"decision": "publish"}).status_code == 403
    login(client)
    r = client.post(f"/t/owner/api/academy/admin/lessons/{lid}/decide", json={"decision": "publish"})
    assert r.status_code == 200 and r.json()["status"] == "published"
    assert client.post(f"/t/owner/api/academy/admin/lessons/{lid}/decide", json={"decision": "publish"}).status_code == 409
    _login_as(client, "owner", "stu1")
    L = client.get(f"/t/owner/api/academy/lessons/{lid}").json()
    assert L["status"] == "published" and L["demo_material"] is False
    # exam-pool questions are never answerable as practice, and need their own approval
    with t.db() as c:
        ex = c.execute("SELECT id, status FROM academy_quiz_items WHERE pool='exam' AND lesson_id=?", (lid,)).fetchall()
        any_exam = c.execute("SELECT id FROM academy_quiz_items WHERE pool='exam' LIMIT 1").fetchone()
    assert all(r["status"] == "draft" for r in ex)
    assert client.post(f"/t/owner/api/academy/items/{any_exam['id']}/answer", json={"answer": "x"}).status_code == 404


def test_stale_detection_by_source_sha(env):
    inputs = _corpus(env)
    tenancy.ensure_defaults()
    t = tenancy.get("owner")
    build.start(t, "test", sync=True)
    build.publish_all(t, "test")
    with t.db() as c:
        old = dict(c.execute("SELECT id, lesson_key, title FROM academy_lessons WHERE source_file='sops/followup_sop.txt' "
                             "AND title='The follow-up rules'").fetchone())
        c.execute("INSERT INTO academy_progress(user_id,lesson_id,status,updated) VALUES(?,?,'complete',?)", (1, old["id"], db.now()))
    assert build.check_sources(t)["up_to_date"]
    (inputs / "sops" / "followup_sop.txt").write_text(EXTRA_SOP.replace("forty-eight hours", "seventy-two hours"))
    index.invalidate()
    chk = build.check_sources(t)
    assert [x["id"] for x in chk["changed"]] == [old["id"]]
    res = build.start(t, "test", sync=True)["result"]
    assert res["marked_stale"] == 1 and res["created"] == 1
    with t.db() as c:
        rows = db.rows(c.execute("SELECT id, status, gen_version FROM academy_lessons WHERE lesson_key=? ORDER BY id",
                                 (old["lesson_key"],)))
    assert [r["status"] for r in rows] == ["stale", "draft"] and rows[1]["gen_version"] == 2
    build.decide_lesson(t, rows[1]["id"], "publish", "test")
    with t.db() as c:
        assert c.execute("SELECT status FROM academy_lessons WHERE id=?", (old["id"],)).fetchone()["status"] == "retired"
    # progress follows the lesson, not the version
    assert old["lesson_key"] in certify.done_keys(t, 1)


def test_transcripts_are_cleaned_and_split_by_texttiling():
    import random

    assert T.clean_spoken("Um so the the the gap, uh, is where you play it back.") == "so the gap, is where you play it back."
    rng = random.Random(1)
    topics = [["reminder", "ladder", "booking", "confirm", "show", "rate", "hours", "before", "call", "value", "video"],
              ["creative", "budget", "audience", "winner", "kill", "rule", "impressions", "lead", "cost", "test", "ring"],
              ["niche", "offer", "usp", "mechanism", "outcome", "timeframe", "pain", "stranger", "repeat", "formula"]]
    lines, n = [], 1
    for topic in topics:
        for _ in range(14):
            lines.append((n, "So the " + " ".join(rng.choice(topic) for _ in range(12)) + "."))
            n += 1
    segs = build.texttile(lines, lambda t: 1.0)
    assert len(segs) == 3 and all(sum(len(t.split()) for _, t in s) >= build.MIN_SEGMENT_WORDS for s in segs)
    assert abs(segs[1][0][0] - 15) <= 2 and abs(segs[2][0][0] - 29) <= 2
    assert build.texttile(lines[:10], lambda t: 1.0) == [lines[:10]]  # short transcripts stay one lesson


# --------------------------------------------------------------------------- A6: question generators and quality gates
def test_question_generators_and_quality_gates(demo_tenant):
    t = demo_tenant
    with t.db() as c:
        rows = [quiz._row(dict(r)) for r in c.execute("SELECT * FROM academy_quiz_items")]
    kinds = {r["kind"] for r in rows}
    assert {"cloze", "mc_numeric", "order", "scenario", "spot", "tf", "write"} <= kinds
    assert len({r["norm_key"] for r in rows}) == len(rows)
    for r in rows:
        if r["kind"] in quiz.CHOICE_KINDS:
            assert quiz._unique_options(r["options"]) and r["answer"] in r["options"]
        if r["kind"] == "mc_numeric":
            units = {T.find_quantities(T.words_to_digits(o))[0]["unit"] for o in r["options"]}
            assert len(units) == 1, r["options"]
        if r["kind"] == "scenario":
            blocked = [o for o in r["options"] if guardrails.blocked(guardrails.check(o, "outreach", {"lead_tier": "warm"}))]
            assert len(blocked) == 1 and r["answer"] not in blocked
        if r["kind"] == "spot":
            bad = [o for o in r["options"] if guardrails.blocked(guardrails.check(o, "outreach", {"lead_tier": "warm"}))
                   or guardrails.blocked(guardrails.check(o, "plain"))]
            assert bad == [r["answer"]]
        if r["kind"] == "tf" and r["answer"] == "False":
            assert r["quotable"] == 0
        if r["kind"] == "order":
            assert sorted(r["options"]) == sorted(r["answer"]) and r["options"] != r["answer"]
        if r["kind"] != "write":
            assert r["checks"].get("answer_in_source", True) is not False
            assert not guardrails.blocked(guardrails.check(str(r["answer"]), "plain")) or r["kind"] == "spot"
        assert r["pool"] in ("practice", "exam") and (r["kind"] != "write" or r["pool"] == "practice")
    assert any(r["pool"] == "exam" for r in rows)


def test_answer_normalisation_and_grading_shapes():
    assert T.same_answer("seventy two hours", "72 hours")
    assert T.same_answer("72h", "seventy-two hours") and T.same_answer("72 hrs", "72 hours") and T.same_answer("72", "72 hours")
    assert not T.same_answer("72 minutes", "72 hours") and not T.same_answer("48 hours", "72 hours")
    assert T.same_answer("cost per lead", "CPL") and T.same_answer("the show rate", "show rate")
    assert T.same_answer("one and a half", "1.5 times") and T.same_answer("ten percent", "10%")
    assert T.words_to_digits("between three and five creatives") == "between 3 and 5 creatives"
    assert not T.same_answer("", "72 hours")
    order = {"kind": "order", "answer": ["Rapport", "Discovery", "Gap"], "options": ["Gap", "Rapport", "Discovery"]}
    assert quiz.grade(None, order, ["rapport", "discovery", "gap"])["correct"]
    assert not quiz.grade(None, order, ["Gap", "Rapport", "Discovery"])["correct"]
    choice = {"kind": "tf", "answer": "False", "options": ["True", "False"]}
    assert quiz.grade(None, choice, "false")["correct"]
    for bad_item, bad in ((order, "Rapport"), (choice, ["True"]), (choice, "Maybe")):
        with pytest.raises(ValueError):
            quiz.grade(None, bad_item, bad)


def test_answer_api_grades_without_leaking_and_rejects_malformed(client):
    demo_login(client, "student")
    t = tenancy.get("demo")
    lid = client.get("/t/demo/api/academy").json()["next_lesson"]["id"]
    qz = client.get(f"/t/demo/api/academy/lessons/{lid}/quiz").json()["items"]
    assert qz and all(not ({"answer", "explanation", "citation", "accept"} & set(i)) for i in qz)
    choice = next(it for it in _published_items(t) if it["kind"] in quiz.CHOICE_KINDS)
    cloze = next(it for it in _published_items(t, kind="cloze"))
    url = f"/t/demo/api/academy/items/{choice['id']}/answer"
    for body in ({"answer": 5}, {"answer": ["x"]}, {"answer": "not an option"}, {"answer": "x" * 5000}, {},
                 {"answer": choice["options"][0], "ms": -1}, {"answer": choice["options"][0], "context": "exam"}):
        assert client.post(url, json=body).status_code == 422, body
    assert client.post(url, content=b"{broken", headers={"content-type": "application/json"}).status_code == 422
    assert client.post("/t/demo/api/academy/items/999999/answer", json={"answer": "x"}).status_code == 404
    before = srs.mastery(t, _demo_user(t, "demo-student")["id"]).get(choice["skill"], {}).get("chance", 0.5)
    r = client.post(url, json={"answer": choice["answer"], "ms": 4000}).json()
    assert r["correct"] and r["citation"]["lines"] and r["mastery"]["chance"] > before and r["mode"] == "rules"
    spoken = T.fmt_quantity(*[(q["value"], q["unit"]) for q in T.find_quantities(T.words_to_digits(cloze["answer"]))][0]) \
        if T.find_quantities(T.words_to_digits(cloze["answer"])) else cloze["answer"].upper()
    r = client.post(f"/t/demo/api/academy/items/{cloze['id']}/answer", json={"answer": spoken}).json()
    assert r["correct"], (cloze["answer"], spoken)
    w = next(it for it in _published_items(t, kind="write") if it["answer"]["asset"] == "dm")
    r = client.post(f"/t/demo/api/academy/items/{w['id']}/answer",
                    json={"answer": "We guarantee results. Our price is $3,000 a month. Want in? Ready?"}).json()
    assert not r["correct"] and r["score"] <= 40 and any(f["severity"] == "block" for f in r["feedback"]["flags"])
    r = client.post(f"/t/demo/api/academy/items/{w['id']}/answer",
                    json={"answer": "Loved your post about referral nights at your clinic. What's working best for new bookings right now?"}).json()
    assert r["feedback"]["criteria"] and r["score"] >= 60


# --------------------------------------------------------------------------- A10: spaced repetition and mastery
def test_spaced_repetition_cards_auto_grading_and_leech(client):
    demo_login(client, "student")
    t = tenancy.get("demo")
    student = _demo_user(t, "demo-student")
    lid = client.get("/t/demo/api/academy").json()["next_lesson"]["id"]
    r = client.post(f"/t/demo/api/academy/lessons/{lid}/complete").json()
    assert r["done"] and r["new_review_cards"] >= 1
    with t.db() as c:
        cards = db.rows(c.execute("SELECT r.* FROM academy_review r JOIN academy_quiz_items i ON i.id=r.item_id "
                                  "WHERE r.user_id=? AND r.item_kind='quiz' AND i.lesson_id=?", (student["id"], lid)))
        assert all(cd["due"] > time.time() + 0.9 * common.DAY for cd in cards)  # first review tomorrow, not today
        c.execute("UPDATE academy_review SET due=? WHERE id=?", (time.time() - 60, cards[0]["id"]))
    due = client.get("/t/demo/api/academy/review/due").json()
    card = next(cd for cd in due["cards"] if cd["card_id"] == cards[0]["id"])
    item = quiz.load(t, card["item"]["id"])
    res = client.post(f"/t/demo/api/academy/items/{item['id']}/answer",
                      json={"answer": _correct_answer(item), "context": "review", "ms": 3000}).json()
    assert res["review"]["grade"] in (4, 5) and res["review"]["interval_days"] >= 1
    assert srs.auto_grade(False, 1000, 10000, 5) == 1
    assert srs.auto_grade(True, 25000, 10000, 0) == 3
    assert srs.auto_grade(True, 12000, 10000, 0) == 4
    assert srs.auto_grade(True, 5000, 10000, 2) == 5
    # a card that keeps lapsing tells the coach and assigns the lesson again
    cid = cards[-1]["id"]
    srs.schedule(t, student["id"], cid, 4)
    for _ in range(4):
        out = srs.schedule(t, student["id"], cid, 4)
        out = srs.schedule(t, student["id"], cid, 1)
    assert out["lapses"] >= 4
    with t.db() as c:
        assert c.execute("SELECT COUNT(*) FROM coach_tasks WHERE kind='academy_leech'").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM academy_assignments WHERE user_id=? AND kind='lesson'",
                         (student["id"],)).fetchone()[0] >= 1
    with pytest.raises(KeyError):
        srs.schedule(t, student["id"] + 99, cid, 4)  # someone else's card
    assert client.post(f"/t/demo/api/academy/review/{cid}/grade", json={"grade": 4}).status_code == 404  # questions grade themselves
    assert client.post(f"/t/demo/api/academy/review/{cid}/grade", json={"grade": 9}).status_code == 422


def test_elo_mastery_and_adaptive_choice(demo_tenant):
    t = demo_tenant
    uid = _demo_user(t, "demo-closer")["id"]
    item = _published_items(t, kind="cloze")[0]
    a = srs.update_mastery(t, uid, item["skill"], item["id"], 1.0)
    b = srs.update_mastery(t, uid, item["skill"], item["id"], 1.0)
    c_ = srs.update_mastery(t, uid, item["skill"], item["id"], 0.0)
    assert a["theta"] > 0 and b["theta"] > a["theta"] and c_["theta"] < b["theta"]
    assert srs.k_factor(0) == pytest.approx(0.4) and srs.k_factor(200) < 0.11
    items = srs.adaptive_items(t, uid, None, 6)
    gaps = [abs(i["predicted_chance"] - srs.TARGET_P) for i in items]
    assert items and gaps[0] <= max(gaps)
    assert srs.mastery(t, uid)[item["skill"]]["chance"] == pytest.approx(common.sigmoid(c_["theta"]), abs=1e-3)


# --------------------------------------------------------------------------- A9: certificates
def test_certificates_awarded_with_event_text_and_expiry(demo_tenant):
    t = demo_tenant
    setter = _demo_user(t, "demo-setter")
    assert certify.is_certified(t, setter["id"], "compliance_core", "L1")
    assert certify.is_certified(t, setter["id"], "setter", "L1")
    assert not certify.is_certified(t, setter["id"], "setter", "L2")
    from operator_ai.academy import is_certified

    assert is_certified(t, setter["id"], "setter", "L1")
    with t.db() as c:
        evs = [db.jload(r["payload"]) for r in c.execute("SELECT payload FROM app_events WHERE name='certification_awarded'")]
        cert = dict(c.execute("SELECT * FROM certifications WHERE user_id=? AND path_key='setter'", (setter["id"],)).fetchone())
    assert {"user_id": setter["id"], "path_key": "setter", "level": "L1"} in evs
    ev = db.jload(cert["evidence"])
    assert ev["status"] == "active" and ev["expires"] == pytest.approx(cert["awarded"] + 180 * common.DAY, abs=5)
    assert "internal training credential" in ev["text"] and "nothing about income" in ev["text"]
    assert not guardrails.check(ev["text"], "plain") and db.jload(cert["criteria_snapshot"])["thresholds"]["mastery_overall"] == 0.8
    lad = certify.ladder(t, setter, "setter")
    l2 = next(lv for lv in lad["levels"] if lv["level"] == "L2")
    assert {m["key"] for m in l2["missing"]} == {"roleplays", "write_items"}
    with t.db() as c:
        c.execute("UPDATE certifications SET expires=? WHERE id=?", (time.time() - 10, cert["id"]))
    assert not certify.is_certified(t, setter["id"], "setter", "L1")
    assert next(lv for lv in certify.ladder(t, setter, "setter")["levels"] if lv["level"] == "L1")["status"] == "expired"


def test_three_guardrail_blocks_trigger_recertification(demo_tenant):
    t = demo_tenant
    setter = _demo_user(t, "demo-setter")
    assert certify.is_certified(t, setter["id"], "compliance_core", "L1")
    for i in range(3):
        it = queue.add(t, "dm_opener", f"We guarantee you results, number {i}.", channel="instagram_dm",
                       meta={"owner_user_id": setter["id"], "prospect_facing": True, "lead_tier": "warm"})
        assert it["status"] == "held"
    assert certify.recent_blocks(t, setter["id"]) == 3
    assert certify.recert_check(t, setter["id"])
    assert not certify.is_certified(t, setter["id"], "compliance_core", "L1")
    l1 = certify.ladder(t, setter, "compliance_core")["levels"][0]
    assert l1["status"] == "expired" and "Recertification" in l1["cert"]["reason"]
    assert any(c["key"] == "fresh_answers" and not c["met"] for c in l1["criteria"])
    certify.refresh(t, setter)  # old evidence does not count again
    assert not certify.is_certified(t, setter["id"], "compliance_core", "L1")


def test_real_work_evidence_is_read_from_the_system(demo_tenant):
    t = demo_tenant
    setter, closer, student = (_demo_user(t, u) for u in ("demo-setter", "demo-closer", "demo-student"))
    th = common.path_def(t, "setter")["thresholds"]["L3"]
    for i in range(3):
        it = queue.add(t, "dm_opener", f"Saw your post about clinic {i}. What's working best right now?", channel="manual",
                       meta={"owner_user_id": setter["id"], "prospect_facing": True})
        queue.approve(t, it["id"], "demo-setter(setter)", "setter", user_id=setter["id"])
    rw = certify._real_work(t, setter, "setter", th, 0)
    assert rw["have"].startswith("3 approved") and not rw["met"]
    cid = call_review.create_call(t, "Discovery (DEMO)", closer["username"], ["Closer", "Lead (DEMO)"],
                                  "Thanks for joining. What's your goal? What's the gap? Book the next step.")
    for p in ("Closer", "Lead (DEMO)"):
        call_review.record_consent(t, cid, p, "verbal", "test")
    call_review.review(t, cid)
    assert certify._real_work(t, closer, "closer", th, 0)["have"].startswith("1 calls")
    assert certify._real_work(t, student, "growth_operator", th, 0)["met"]  # the demo student has a client on file
    assert not certify._real_work(t, closer, "growth_operator", th, 0)["met"]
    with t.db() as c:
        c.execute("INSERT INTO prospects(owner_user_id,name,stage,demo,created,updated) VALUES(?,?,?,?,?,?)",
                  (closer["id"], "Harbour Physio (DEMO)", "won", 1, db.now(), db.now()))
    assert certify._real_work(t, closer, "growth_operator", th, 0)["met"]


# --------------------------------------------------------------------------- the approval gate
def test_compliance_certificate_gate_on_own_outreach(client):
    demo_login(client, "owner")
    t = tenancy.get("demo")
    student, setter = _demo_user(t, "demo-student"), _demo_user(t, "demo-setter")

    def own(user):
        return queue.add(t, "dm_opener", "Saw your post about referral nights. How are they going?", channel="instagram_dm",
                         meta={"owner_user_id": user["id"], "prospect_facing": True, "lead_tier": "cold"})["id"]

    q1, q2, q3, q4 = own(student), own(student), own(setter), own(setter)
    demo_login(client, "student")
    assert client.post(f"/t/demo/api/queue/{q1}/approve").status_code == 200  # switch off: unchanged behaviour
    assert client.put("/t/demo/api/academy/admin/settings", json={"require_cert_for_own_outreach": True}).status_code == 403
    demo_login(client, "owner")
    assert client.put("/t/demo/api/academy/admin/settings", json={"require_cert_for_own_outreach": True}).json() == \
        {"require_cert_for_own_outreach": True}
    demo_login(client, "student")
    r = client.post(f"/t/demo/api/queue/{q2}/approve")
    assert r.status_code == 403 and "Compliance core" in r.json()["detail"]
    assert queue.get(t, q2)["status"] == "pending"
    assert client.post(f"/t/demo/api/queue/{q4}/approve").status_code == 403  # not theirs: the queue's own answer
    demo_login(client, "setter")  # holds Compliance core L1 from the demo seed
    assert client.post(f"/t/demo/api/queue/{q3}/approve").status_code == 200
    demo_login(client, "owner")
    assert client.post(f"/t/demo/api/queue/{q2}/approve").status_code == 200
    nudge = queue.add(t, "nudge", "Hey, what's one small win from this week?", channel="telegram")["id"]
    demo_login(client, "coach")
    assert client.post(f"/t/demo/api/queue/{nudge}/approve").status_code == 200  # other kinds are not gated
    assert certify.approval_gate(t, student["id"], "dm_opener", {"prospect_facing": True}) is not None
    assert certify.approval_gate(t, student["id"], "client_report", {}) is None


# --------------------------------------------------------------------------- exams
def test_exam_needs_l2_hides_answers_pauses_the_coach_and_has_a_cooldown(client):
    demo_login(client, "student")
    t = tenancy.get("demo")
    student = _demo_user(t, "demo-student")
    r = client.post("/t/demo/api/academy/exams", json={"path": "growth_operator", "level": "L3"})
    assert r.status_code == 409 and "L2" in r.json()["detail"]
    assert client.post("/t/demo/api/academy/exams", json={"path": "nope"}).status_code == 422
    assert client.post("/t/demo/api/academy/exams", json={"path": "setter", "level": "L2"}).status_code == 422
    for lv in ("L1", "L2"):
        certify.award(t, student, "growth_operator", lv, [], "test")
    ex = client.post("/t/demo/api/academy/exams", json={"path": "growth_operator"}).json()
    assert len(ex["items"]) == 30 and all("answer" not in i and "citation" not in i for i in ex["items"])
    assert client.post("/t/demo/api/academy/exams", json={"path": "growth_operator"}).json()["id"] == ex["id"]  # resumes
    assert client.post("/t/demo/api/coach/ask", json={"question": "What is the USP formula?"}).status_code == 423
    demo_login(client, "setter")
    assert client.get(f"/t/demo/api/academy/exams/{ex['id']}").status_code == 404
    assert client.post(f"/t/demo/api/academy/exams/{ex['id']}/submit", json={"answers": {}}).status_code == 404
    demo_login(client, "student")
    res = client.post(f"/t/demo/api/academy/exams/{ex['id']}/submit", json={"answers": {}}).json()
    assert res["status"] == "failed" and res["score"] == 0
    assert client.post(f"/t/demo/api/academy/exams/{ex['id']}/submit", json={"answers": {}}).status_code == 409
    assert client.post("/t/demo/api/coach/ask", json={"question": "What is the USP formula?"}).status_code == 200
    r = client.post("/t/demo/api/academy/exams", json={"path": "growth_operator"})
    assert r.status_code == 429  # 24h retake cooldown
    with t.db() as c:
        c.execute("UPDATE academy_exams SET submitted=? WHERE id=?", (time.time() - 25 * 3600, ex["id"]))
    ex2 = client.post("/t/demo/api/academy/exams", json={"path": "growth_operator"}).json()
    answers = {str(i["id"]): _correct_answer(quiz.load(t, i["id"])) for i in ex2["items"]}
    res = client.post(f"/t/demo/api/academy/exams/{ex2['id']}/submit", json={"answers": answers}).json()
    assert res["passed"] and res["score"] >= 0.85
    crit = certify.criteria(t, student, "growth_operator", "L3")
    assert next(c for c in crit if c["key"] == "exam")["met"]
    assert not next(c for c in crit if c["key"] == "signoff")["met"]


# --------------------------------------------------------------------------- team: scope, sign-off, assignment
def test_team_views_are_scoped_to_a_coachs_own_learners(client):
    demo_login(client, "coach")
    t = tenancy.get("demo")
    closer, student, setter = (_demo_user(t, u) for u in ("demo-closer", "demo-student", "demo-setter"))
    team = client.get("/t/demo/api/academy/team").json()
    ids = {p["id"] for p in team["people"]}
    assert {student["id"], setter["id"]} <= ids and closer["id"] not in ids and team["scope"] == "your learners"
    assert client.get(f"/t/demo/api/academy/team/{student['id']}").status_code == 200
    assert client.get(f"/t/demo/api/academy/team/{closer['id']}").status_code == 404
    for path in ("/academy/team/misconceptions", "/academy/team/signoffs", "/academy/team/impact", "/academy/admin/drafts",
                 "/academy/admin/coverage", "/academy/admin/item-stats", "/academy/admin/settings", "/academy/admin/builds"):
        assert client.get(f"/t/demo/api{path}").status_code == 200, path
    assert client.post("/t/demo/api/academy/assign", json={"user_id": closer["id"], "kind": "path", "ref": "closer"}).status_code == 404
    r = client.post("/t/demo/api/academy/assign", json={"user_id": student["id"], "kind": "path", "ref": "setter",
                                                         "due_days": 7, "note": "Worth a look before Friday."})
    assert r.status_code == 200
    item = queue.get(t, r.json()["queue_id"])
    assert item["kind"] == "coach_note" and item["status"] == "pending" and not item["delivered"]
    assert "setter" in common.user_paths(t, student)
    demo_login(client, "student")
    for path in ("/academy/team", f"/academy/team/{setter['id']}", "/academy/team/signoffs", "/academy/admin/drafts",
                 "/academy/admin/settings"):
        assert client.get(f"/t/demo/api{path}").status_code == 403, path
    assert client.post("/t/demo/api/academy/assign", json={"user_id": setter["id"], "kind": "path", "ref": "setter"}).status_code == 403
    demo_login(client, "owner")
    assert closer["id"] in {p["id"] for p in client.get("/t/demo/api/academy/team").json()["people"]}


def test_roleplay_signoff_feeds_l3_and_coach_calibration(client):
    demo_login(client, "coach")
    t = tenancy.get("demo")
    student, closer, coach = (_demo_user(t, u) for u in ("demo-student", "demo-closer", "demo-coach"))
    score = {"total": 80, "compliance_flags": 0, "criteria": [], "scenario_key": "price_call", "mode": "rules", "seed": 3,
             "signoff": None}
    with t.db() as c:
        rp = c.execute("INSERT INTO roleplays(user_id,scenario_key,transcript,state,score,status,created,updated) "
                       "VALUES(?,?,?,?,?,?,?,?)", (student["id"], "price_call", db.jdump([{"speaker": "learner", "text": "Hi"}]),
                                                   "{}", db.jdump(score), "complete", db.now(), db.now())).lastrowid
        other = c.execute("INSERT INTO roleplays(user_id,scenario_key,transcript,state,score,status,created,updated) "
                          "VALUES(?,?,?,?,?,?,?,?)", (closer["id"], "price_call", "[]", "{}", db.jdump(score), "complete",
                                                      db.now(), db.now())).lastrowid
    queue_ids = [x["id"] for x in client.get("/t/demo/api/academy/team/signoffs").json()["items"]]
    assert rp in queue_ids and other not in queue_ids
    assert client.post(f"/t/demo/api/academy/team/roleplays/{other}/signoff", json={"score": 80}).status_code == 404
    assert client.post(f"/t/demo/api/academy/team/roleplays/{rp}/signoff", json={"score": 500}).status_code == 422
    r = client.post(f"/t/demo/api/academy/team/roleplays/{rp}/signoff", json={"score": 74, "note": "Good gap work"}).json()
    assert r["agreement"] is True and r["signoff"]["by"] == coach["display"]
    assert client.post(f"/t/demo/api/academy/team/roleplays/{rp}/signoff", json={"score": 74}).status_code == 409
    with t.db() as c:
        stored = db.jload(c.execute("SELECT score FROM roleplays WHERE id=?", (rp,)).fetchone()["score"])
    assert stored["total"] == 80 and stored["signoff"]["score"] == 74  # the rules score is untouched
    assert next(c for c in certify.criteria(t, student, "growth_operator", "L3") if c["key"] == "signoff")["met"]
    assert certify._graded_by(t, coach) == [{"total": 80.0, "given": 74.0}]
    demo_login(client, "student")
    assert client.post(f"/t/demo/api/academy/team/roleplays/{rp}/signoff", json={"score": 90}).status_code == 403


def test_cert_signoff_and_revoke_by_staff_only(client):
    demo_login(client, "coach")
    t = tenancy.get("demo")
    setter = _demo_user(t, "demo-setter")
    with t.db() as c:
        cid = c.execute("SELECT id FROM certifications WHERE user_id=? AND path_key='setter'", (setter["id"],)).fetchone()["id"]
    assert client.post(f"/t/demo/api/academy/certs/{cid}/signoff").json()["signed_off_by"] == "Coach Rivera (DEMO)"
    assert client.post(f"/t/demo/api/academy/certs/{cid}/revoke", json={"reason": "x"}).status_code == 422
    demo_login(client, "setter")
    assert client.post(f"/t/demo/api/academy/certs/{cid}/revoke", json={"reason": "my own"}).status_code == 403
    demo_login(client, "coach")
    assert client.post(f"/t/demo/api/academy/certs/{cid}/revoke", json={"reason": "Practised on a live lead"}).json()["status"] == "revoked"
    assert not certify.is_certified(t, setter["id"], "setter", "L1")
    assert client.post(f"/t/demo/api/academy/certs/{cid}/revoke", json={"reason": "again please"}).status_code == 409


# --------------------------------------------------------------------------- analytics, drills, AI, admin
def test_item_analytics_flags_easy_and_reported_questions(demo_tenant):
    t = demo_tenant
    easy, hard, rep = _published_items(t, kind="cloze")[:3]
    with t.db() as c:
        for u in range(1000, 1031):
            c.execute("INSERT INTO academy_attempts(user_id,quiz_item_id,correct,created) VALUES(?,?,1,?)", (u, easy["id"], db.now()))
            c.execute("INSERT INTO academy_attempts(user_id,quiz_item_id,correct,created,chosen_option) VALUES(?,?,?,?,?)",
                      (u, hard["id"], 1 if u % 2 else 0, db.now(), "wrong guess"))
    for uid in (1, 2, 3):
        user = {"id": uid}
        from operator_ai.academy import learn

        learn.report_item(t, user, rep["id"], "The answer looks wrong")
    learn.report_item(t, {"id": 1}, rep["id"], "again")  # one report per person
    stats = {s["id"]: s for s in quiz.item_stats(t)}
    assert stats[easy["id"]]["p_value"] > 0.95 and any("too easy" in f for f in stats[easy["id"]]["flags"])
    assert stats[hard["id"]]["discrimination"] is not None and stats[hard["id"]]["top_wrong"] == "wrong guess"
    assert stats[rep["id"]]["reported"] == 3 and any("reported 3" in f for f in stats[rep["id"]]["flags"])


def test_drill_bank_seeded_at_build_not_on_read(client):
    login(client)
    t = tenancy.get("owner")
    assert call_review.due_cards(t, 1) == []
    with t.db() as c:
        assert c.execute("SELECT COUNT(*) FROM drills").fetchone()[0] == 0  # reading never seeds
    demo_login(client, "closer")
    d = tenancy.get("demo")
    cards = client.get("/t/demo/api/academy/drills").json()["cards"]
    think = next(c for c in cards if c["objection"] == "I need to think about it")
    assert "answer" not in think  # recall first: the list never carries the answer
    assert call_review.make_drill(d, "I need to ask my partner", "partner", "test")["answer"].endswith("both of them together.")
    a = client.get(f"/t/demo/api/academy/drills/{think['card_id']}/answer").json()
    assert a["answer"].startswith("Ask what specifically") and a["citation"]["lines"] == "8-8"
    g = client.post(f"/t/demo/api/academy/review/{think['card_id']}/grade", json={"grade": 5}).json()
    assert g["interval_days"] == 1.0 and g["reps"] == 1
    assert client.get("/t/demo/api/drills").json()["cards"][0]["card_id"] in {c["card_id"] for c in cards}  # one store


@pytest.fixture
def fake_model(monkeypatch):
    fm = FakeModel()
    llm.set_transport(fm.transport)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key-123456789")
    monkeypatch.setenv("OPERATOR_DISABLE_LLM", "0")
    yield fm
    llm.set_transport(None)


def test_ai_off_sends_nothing_and_ai_explainers_must_pass_every_check(env, fake_model):
    _corpus(env)
    tenancy.ensure_defaults()
    t = tenancy.get("owner")
    before = llm.REQUESTS_SENT
    out = build.start(t, "test", sync=True)
    assert out["result"]["ai_mode"] == "rules" and llm.REQUESTS_SENT == before and fake_model.calls == []
    # with a budget, a rewrite that invents a number is refused and the extractive lesson is kept
    settings.set_box(t, "ai_token_budget", 100000, "test budget")
    fake_model.reply = "Send the follow-up after 99 hours [S1]. Always do it [S9]."
    with t.db() as c:
        c.execute("DELETE FROM academy_quiz_items")
        c.execute("DELETE FROM academy_lessons")
    out = build.start(t, "test", sync=True)
    assert fake_model.calls, "the AI was asked"
    with t.db() as c:
        modes = {r["gen_mode"] for r in c.execute("SELECT gen_mode FROM academy_lessons")}
        checks = [db.jload(r["checks"]) for r in c.execute("SELECT checks FROM academy_lessons")]
    assert modes == {"rules"} and all(ck["ai"].startswith("kept the extractive version") for ck in checks)
    # a faithful explainer passes: every sentence cited, numbers from the lines, 150-300 words
    lesson = {"citations": [{"id": "S1", "snippet": "Send the first follow-up after forty-eight hours, never sooner."},
                            {"id": "S2", "snippet": "Every follow-up must give value: a tip, a short video or one question."}]}
    good = " ".join(["Send the first follow-up after forty-eight hours, never sooner [S1].",
                     "Every follow-up must give value: a tip, a short video or one question [S2]."] * 8)
    ok, problems = build.check_explainer(good, lesson)
    assert ok, problems
    bad, problems = build.check_explainer(good + " Students earn more [S7].", lesson)
    assert not bad and any("invented" in p for p in problems)


def test_thresholds_editor_validation_and_snapshots(client):
    demo_login(client, "owner")
    t = tenancy.get("demo")
    setter = _demo_user(t, "demo-setter")
    url = "/t/demo/api/academy/admin/paths/setter"
    assert client.put(url, json={"levels": {"L1": {"mastery_overall": 0.75}}}).status_code == 200
    assert common.path_def(t, "setter")["thresholds"]["L1"]["mastery_overall"] == 0.75
    assert client.put(url, json={"levels": {"L1": {"made_up": 1}}}).status_code == 422
    assert client.put(url, json={"levels": {"L1": {"mastery_overall": 7}}}).status_code == 422
    assert client.put(url, json={"levels": {"L9": {"mastery_overall": 0.8}}}).status_code == 422
    assert client.put(url, json={"levels": "nope"}).status_code == 422
    assert client.put("/t/demo/api/academy/admin/paths/nope", json={"levels": {}}).status_code == 404
    with t.db() as c:
        snap = db.jload(c.execute("SELECT criteria_snapshot FROM certifications WHERE user_id=? AND path_key='setter' AND "
                                  "level='L1'", (setter["id"],)).fetchone()["criteria_snapshot"])
    assert snap["thresholds"]["mastery_overall"] == 0.8  # awarded under the old rule, unchanged
    demo_login(client, "coach")
    assert client.put(url, json={"levels": {"L1": {"mastery_overall": 0.9}}}).status_code == 403
    assert client.post("/t/demo/api/academy/admin/publish-all", json={"include_exam": True}).status_code == 403


def test_every_learner_route_works_for_every_role_with_and_without_data(client):
    for role in auth.ROLES:
        demo_login(client, role)
        for path in ("/academy", "/academy/paths", "/academy/paths/compliance_core", "/academy/certs/me", "/academy/practice",
                     "/academy/review/due", "/academy/session", "/academy/drills"):
            r = client.get(f"/t/demo/api{path}")
            assert r.status_code == 200, (role, path, r.text[:200])
        assert client.get("/t/demo/api/academy/paths/nope").status_code == 404
        assert client.get("/t/demo/api/academy/practice", params={"skill": "nope"}).status_code == 422
        assert client.post("/t/demo/api/academy/certs/check").status_code == 200
        staff = role in ("owner", "coach")
        assert client.get("/t/demo/api/academy/team").status_code == (200 if staff else 403)
    # a real workspace with no material and no data still renders every learner route
    login(client)
    for path in ("/academy", "/academy/paths", "/academy/certs/me", "/academy/practice", "/academy/review/due",
                 "/academy/session", "/academy/team", "/academy/admin/drafts", "/academy/admin/coverage",
                 "/academy/admin/item-stats", "/academy/admin/builds", "/academy/admin/settings"):
        assert client.get(f"/t/owner/api{path}").status_code == 200, path
    assert client.post("/t/owner/api/academy/admin/check-sources").status_code == 409


def test_malformed_bodies_get_422_never_500(client):
    demo_login(client, "owner")
    bad = [("post", "/academy/exams", {"level": "L3"}), ("post", "/academy/assign", {"user_id": 3, "kind": "nope", "ref": "x"}),
           ("post", "/academy/assign", {"user_id": 0, "kind": "path", "ref": "setter"}),
           ("post", "/academy/assign", {"user_id": 3, "kind": "lesson", "ref": "not-a-number"}),
           ("post", "/academy/team/roleplays/1/signoff", {"score": -1}), ("post", "/academy/admin/lessons/1/decide", {"decision": "x"}),
           ("post", "/academy/admin/items/1/decide", {"decision": "delete"}), ("put", "/academy/admin/settings", {"x": 1}),
           ("put", "/academy/admin/settings", {"require_cert_for_own_outreach": "maybe"}),
           ("post", "/academy/items/1/report", {"reason": "x" * 600}), ("post", "/academy/review/1/grade", {"grade": "a"}),
           ("post", "/academy/exams/1/submit", {"answers": [1, 2]}), ("post", "/academy/certs/1/revoke", {})]
    for method, path, body in bad:
        r = getattr(client, method)(f"/t/demo/api{path}", json=body)
        assert r.status_code == 422, (path, body, r.status_code, r.text[:200])
    for path in ("/academy/items/1/answer", "/academy/exams", "/academy/assign", "/academy/admin/publish-all"):
        r = client.post(f"/t/demo/api{path}", content=b"{broken", headers={"content-type": "application/json"})
        assert r.status_code == 422, path
    assert client.post("/t/demo/api/academy/admin/lessons/999999/decide", json={"decision": "publish"}).status_code == 404
    assert client.post("/t/demo/api/academy/admin/items/999999/decide", json={"decision": "publish"}).status_code == 404
    assert client.get("/t/demo/api/academy/lessons/999999").status_code == 404
