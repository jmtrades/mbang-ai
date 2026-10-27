"""Does the Academy genuinely train people? A hostile end-to-end pass with the training lens.

A fresh, real (non-demo) workspace gets its own small course, written for this test in our own words (fictional:
"Harbor Lab"), and the pitch demo is checked alongside it:

* lessons teach the owner's own lines, each with an exact file:line citation, on the right skill and path, and no line
  is taught twice in one lesson;
* every question is answerable from its cited line and graded right, and objections are quoted the way a prospect
  would say them; write-it tasks never contradict the owner's material;
* write-it answers that are gibberish, a keyword list, the task or the rubric pasted back, a 500-word "DM" or a DM
  with no question never pass, so they never count towards a certificate;
* role-play evidence for a certificate is a real, unaided pass: not a session quit before its outcome, not one played
  with live hints telling the learner each move, and a coach's sign-off with a failing grade is not a sign-off;
* spaced repetition can't be skipped by answering the same card again and again;
* the coach's mastery number is the one the learner sees and the certificate uses.
"""
from __future__ import annotations

import json
import re
import time

import pytest

from operator_ai import auth, config, db, index, tenancy
from operator_ai.academy import build, certify, common, learn, quiz, srs, team
from operator_ai.academy import text as T
from tests.conftest import login
from tests.test_roleplay import GOOD, play

TID = "harbor"
API = f"/t/{TID}/api"

# The owner's own course: four short SOPs, fictional, written for this test.
HARBOR = {
    "sops/dm_playbook.txt": """Harbor Lab DM Playbook

Opening a conversation
Open every new conversation with one question about their business, never a pitch.
Keep the first message under forty words so it reads on a phone without scrolling.
Mention one specific thing from their profile so the message could only have been sent to them.
Never put a price, a link or a calendar in the first message.

Following up
Send the first follow-up after three days if there is no reply.
Every follow-up must add something useful: a short tip, a quick example or one fresh question.
Stop after four follow-ups and move the lead to the monthly newsletter list.
Log every reply in the tracker the same day so the setter sees the whole thread.

Handling replies
When someone says they are not interested, thank them and ask if you can check back in ninety days.
When a lead says they have no budget, ask what they currently spend on getting new customers.
When a prospect asks how much it costs, explain that the price depends on fit and offer a short call to find out.
Always answer in the same channel the lead used, so the conversation never splits.
""",
    "sops/discovery_call.txt": """Harbor Lab Discovery Call Script

The five call stages
Run every call through the same five stages: rapport, discovery, gap, offer and next step.
Spend the first five minutes on rapport and let the prospect do most of the talking.
In discovery, ask about their current monthly revenue, their goal and what has stopped them so far.
The gap is the distance between where they are today and where they want to be in twelve months.
Play the gap back in their own words before you mention the offer.

Presenting the offer
Present the offer only after the prospect agrees the gap is worth closing.
Explain who the programme is for and who it is not for in plain language.
State the price once, then stop talking and let them respond.

Common objections on calls
When someone says they need to think about it, ask what exactly they want to think through.
When a prospect says they need to ask a partner, offer to book a short call with both of them.
When the client says it is too expensive, compare the price with the cost of the gap staying open for a year.
Never lower the price on the call to win the deal.
""",
    "sops/booking_sop.txt": """Harbor Lab Booking and Show-Up SOP

Confirming the booking
Send a confirmation within ten minutes of the booking with the date, the time and the link.
Ask the prospect to reply yes to confirm, because a prospect who replies is more likely to attend.
Book every call within seventy-two hours of the first yes; a longer wait lowers attendance.

The reminder ladder
Send reminders twenty-four hours before, three hours before and fifteen minutes before the call.
Each reminder carries value, such as a short case question or a two-minute video to watch first.
Never send a reminder that only says "see you soon".

When someone misses the call
Message a no-show within twenty minutes with two new times to choose from.
Keep the tone kind; people miss calls for real reasons and shaming them loses the deal.
After two missed calls, move the lead to the nurture sequence instead of chasing.
""",
    "sops/honest_marketing.txt": """Harbor Lab Honest Marketing Rules

Results and proof
Only share client results that are real, recent and shared with the client's written permission.
Never promise a specific income or a guaranteed outcome to a prospect.
Describe what the programme teaches, not what someone will earn.

Deadlines and urgency
Only mention a deadline when it is real, and always say why it exists.
Never invent scarcity such as fake spots left or a countdown that resets.

Subject lines and messages
Write subject lines that match what is inside the message.
Never pretend a message is a reply to a conversation that did not happen.
""",
}
PW = "pw-harbor-learner-77"
GENUINE_DM = "Hey Sam, saw your clinic just opened a second room in Leeds. How are you filling the new evening slots?"


# --------------------------------------------------------------------------- helpers
@pytest.fixture
def harbor(env):
    """A fresh real workspace with the Harbor Lab course in inputs/, built and published by the owner."""
    inputs = env / "inputs"
    for rel, text in HARBOR.items():
        p = inputs / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    index.invalidate()
    assert not config.get().using_fixture_corpus
    tenancy.ensure_defaults()
    t, _ = tenancy.create(TID, "Harbor Lab", kb_scope="course", kind="owner")
    assert not t.is_demo and not tenancy.is_demo_db(t)
    res = build.start(t, "owner(owner)", sync=True)["result"]
    assert res["lessons_built"] >= 8 and res["skipped"] == 0 and not res["demo_material"]
    build.publish_all(t, "owner(owner)", include_exam=True)
    return t


def _user(t, username: str, role: str, coach_id: int | None = None) -> dict:
    uid = auth.create_user(t, username, PW, role, display=username.title(), coach_id=coach_id)
    return common.user_row(t, uid)


def _lessons(t) -> list[dict]:
    with t.db() as c:
        return [learn._lesson_row(r) for r in c.execute("SELECT * FROM academy_lessons WHERE status='published' ORDER BY id")]


def _items(t, where: str = "1=1", args: tuple = ()) -> list[dict]:
    with t.db() as c:
        return [quiz._row(dict(r)) for r in c.execute(
            "SELECT i.*, l.skill lesson_skill FROM academy_quiz_items i JOIN academy_lessons l ON l.id=i.lesson_id "
            f"WHERE i.status='published' AND l.status='published' AND {where} ORDER BY i.id", args)]


def _source(idx, cite: dict) -> str:
    a, _, b = cite["lines"].partition("-")
    return " ".join(idx.docs[cite["file"]].lines[n - 1] for n in range(int(a), int(b or a) + 1))


# --------------------------------------------------------------------------- lessons
def test_lessons_teach_the_owners_own_lines_with_exact_citations_on_the_right_skill(harbor):
    t = harbor
    idx = index.for_tenant(t)
    lessons = _lessons(t)
    assert lessons and "Thunder Dome" not in json.dumps(lessons)  # nothing from the fictional fixture material
    for L in lessons:
        cites = {c["id"]: c for c in L["citations"]}
        taught = []
        for block in ("key_points", "example", "mistakes"):
            for k in L[block]:
                c = cites[k["cite"]]
                assert c["lines"] == f"{k['line']}-{k['line']}"
                assert k["text"] in _source(idx, c), (L["title"], k)  # the exact line, word for word
                taught.append(k["text"])
        assert len(taught) == len(set(taught)), f"“{L['title']}” teaches the same line twice: {taught}"
    # every lesson from the DM playbook is a DM-setting lesson, so the Setter path teaches all of it
    dm = [L for L in lessons if L["source_file"] == "sops/dm_playbook.txt"]
    assert len(dm) == 3 and all(L["skill"] == "dms" for L in dm), [(L["title"], L["skill"]) for L in dm]
    setter = {L["id"] for L in certify.path_lessons(t, common.path_def(t, "setter"))}
    assert {L["id"] for L in dm} <= setter


def test_lessons_teach_the_rules_the_role_plays_grade(harbor):
    """A lesson picks its key points from the section's sentences. The SOP's own rules have to make it in: a learner
    is graded on "offer only after the gap" in every sales role-play, so the sales lesson has to teach it."""
    taught = " ".join(k["text"] for L in _lessons(harbor) for b in ("key_points", "mistakes", "example") for k in L[b])
    for rule in ("Play the gap back in their own words before you mention the offer.",
                 "Present the offer only after the prospect agrees the gap is worth closing.",
                 "State the price once, then stop talking and let them respond.",
                 "Log every reply in the tracker the same day so the setter sees the whole thread.",
                 "Describe what the programme teaches, not what someone will earn."):
        assert rule in taught, rule


def test_write_it_tasks_fit_their_lesson_and_never_contradict_the_material(harbor):
    t = harbor
    writes = _items(t, "i.kind='write'")
    assert {w["answer"]["asset"] for w in writes} >= {"dm", "followup", "reminder"}
    for w in writes:
        allowed = {a for a, _, _ in quiz.WRITE_TASKS.get(w["lesson_skill"], [])} | (
            {quiz.FOLLOWUP_TASK[0]} if w["lesson_skill"] == "dms" else set())
        assert w["answer"]["asset"] in allowed, (w["prompt"], w["lesson_skill"])
        # Harbor Lab's reminder ladder is 24 hours, 3 hours and 15 minutes: no task asks for a "two hours before" one
        assert "two hours" not in w["prompt"].lower(), w["prompt"]


# --------------------------------------------------------------------------- questions
def test_every_question_is_answerable_from_its_cited_line_and_graded_right(harbor):
    t = harbor
    idx = index.for_tenant(t)
    items = _items(t, "i.kind!='write'")
    assert {i["kind"] for i in items} >= {"cloze", "mc_numeric", "tf", "scenario", "order", "spot"}
    for it in items:
        src = _source(idx, it["citation"])
        right = list(it["answer"]) if it["kind"] == "order" else it["answer"]
        assert quiz.grade(t, it, right)["correct"], it["prompt"]
        if it["kind"] == "order":
            assert not quiz.grade(t, it, list(reversed(right)))["correct"]
        elif it["kind"] in quiz.CHOICE_KINDS:
            assert [o for o in it["options"] if quiz.grade(t, it, o)["correct"]] == [right], it["prompt"]
        else:
            assert not quiz.grade(t, it, "banana")["correct"]
        if it["kind"] == "cloze":
            filled = it["prompt"].replace("_____", it["accept"][0])
            assert T.norm_cmp(filled) in T.norm_cmp(src), (it["prompt"], src)
        if it["kind"] == "mc_numeric":
            # a distractor one word off "under 40 words" (39, 41) tests nothing the lesson taught
            want = T.find_quantities(T.words_to_digits(right))[0]["value"]
            for o in it["options"]:
                v = T.find_quantities(T.words_to_digits(o))[0]["value"]
                assert v == want or abs(v - want) / want >= 0.1, (it["prompt"], it["options"])
        if it["kind"] == "spot":
            # the explanation cites the course rule the injected line breaks, never an unrelated lesson line
            assert re.match(r"^(never|don'?t|do not|no|only)\b", it["citation"]["snippet"], re.I), it["citation"]


def test_objections_are_quoted_the_way_a_prospect_says_them(harbor):
    t = harbor
    for it in _items(t, "i.kind='scenario'"):
        said = re.search(r"“(.+?)”", it["prompt"]).group(1)
        assert not re.match(r"(they|them|their|he|she)\b", said, re.I), it["prompt"]
    with t.db() as c:
        drills = [r["objection"] for r in c.execute("SELECT objection FROM drills")]
    assert {"I'm not interested", "I have no budget", "I need to think about it", "It is too expensive",
            "Can you tell me how much it costs?"} <= set(drills), drills
    assert not any(re.match(r"(they|them|their)\b", d, re.I) for d in drills)
    # a workspace built before this fix gets the spoken wording on its next build (same drill, same cards)
    with t.db() as c:
        did = c.execute("SELECT id FROM drills WHERE objection=?", ("I'm not interested",)).fetchone()["id"]
        c.execute("UPDATE drills SET objection='They are not interested' WHERE id=?", (did,))
    build.start(t, "owner(owner)", sync=True)
    with t.db() as c:
        assert c.execute("SELECT objection FROM drills WHERE id=?", (did,)).fetchone()["objection"] == "I'm not interested"


# --------------------------------------------------------------------------- write-it can't be gamed
def _write_item(t, asset: str) -> dict:
    return next(w for w in _items(t, "i.kind='write'") if w["answer"]["asset"] == asset)


def test_write_it_answers_cannot_be_gamed_into_a_certificate(client, harbor):
    t = harbor
    stu = _user(t, "nia", "setter")
    login(client, TID, "nia", PW)
    dm, reminder = _write_item(t, "dm"), _write_item(t, "reminder")
    filler = " ".join(["We also run a weekly newsletter with tips for clinic owners and their front desk teams."] * 30)
    gamed = {
        dm["id"]: ["zzz qqq www",
                   "blah blah blah blah blah blah blah blah blah blah",
                   "question opener profile niche specific business owner value reply",  # a keyword list
                   dm["prompt"],                                                       # the task pasted back
                   "Ask one question per message. If you ask three questions, they answer none.",  # the rubric's fix
                   "Open every new conversation with one question about their business, never a pitch.",  # the SOP
                   GENUINE_DM + " " + filler,                                          # one giant message
                   "Hey Sam, saw your clinic just opened a second room in Leeds and the photos look great."],  # no question
        reminder["id"]: ["tip tip tip tip tip tip tip tip",
                         "Add something useful: a tip, a short video or a question to think about.",
                         "video tip example case question value reminder booked"],
    }
    for iid, texts in gamed.items():
        for text in texts:
            r = client.post(f"{API}/academy/items/{iid}/answer", json={"answer": text}).json()
            assert not r["correct"] and r["score"] < 80, (text[:60], r["score"])
            assert r["feedback"]["criteria"] and r["feedback"]["criteria"][0]["fix"], text[:60]  # says why
    # five different gamed answers are not five pieces of L2 evidence
    crit = next(c for c in certify.criteria(t, stu, "setter", "L2") if c["key"] == "write_items")
    assert crit["have"] == 0
    # real drafts still pass
    r = client.post(f"{API}/academy/items/{dm['id']}/answer", json={"answer": GENUINE_DM}).json()
    assert r["correct"] and r["score"] >= 80, r
    r = client.post(f"{API}/academy/items/{reminder['id']}/answer", json={
        "answer": "Hi Priya, looking forward to our call at 3pm today. Here's a two-minute video on how one studio filled "
                  "its evening classes; worth a look before we talk."}).json()
    assert r["correct"], r
    # the other write-it shapes: a USP needs every part of the formula filled in, an email needs a subject and a body
    usp = {"kind": "write", "prompt": "Write your USP using the course formula.", "answer": {"asset": "usp", "meta": {}},
           "citation": None}
    assert not quiz.grade(t, usp, "I help get in through without")["correct"]
    assert quiz.grade(t, usp, "I help independent dog groomers get twelve new regular clients a month in ninety days "
                              "through a referral card system without discounting their grooms.")["correct"]
    email = {"kind": "write", "prompt": "Write the subject line and opening lines of a weekly client report email.",
             "answer": {"asset": "email", "meta": {"client_asset": True}}, "citation": None}
    assert not quiz.grade(t, email, "reply reply hello world")["correct"]
    assert quiz.grade(t, email, "Subject: Your week 6 numbers: bookings up, cost per lead down\n"
                                "Hi Jo, here is your weekly report. Bookings rose to 14 this week and the new ad brought the "
                                "cost per lead under target. Reply with any questions.")["correct"]


def test_write_it_gaming_fails_on_the_demo_too(demo_tenant):
    t = demo_tenant
    w = next(i for i in _items(t, "i.kind='write'") if i["answer"]["asset"] == "dm")
    for text in ("zzz qqq www", "question opener niche value reply follow-up conversation message", w["prompt"]):
        assert not quiz.grade(t, w, text)["correct"], text
    assert quiz.grade(t, w, "Loved your post about referral nights at your clinic. What's working best for new "
                            "bookings right now?")["correct"]


# --------------------------------------------------------------------------- role-play evidence
def test_role_play_evidence_for_a_certificate_is_a_real_unaided_pass(tenant):
    t = tenant
    coach = _user(t, "cora", "coach")
    stu = _user(t, "leo", "student", coach_id=coach["id"])

    def l2() -> dict:
        return next(c for c in certify.criteria(t, stu, "growth_operator", "L2") if c["key"] == "roleplays")

    def signoff() -> dict:
        return next(c for c in certify.criteria(t, stu, "growth_operator", "L3") if c["key"] == "signoff")

    # 1. quit before the outcome: 80+ points, but the scorecard says "Not yet" (no next step was agreed)
    quit_early = []
    for key in ("discovery_call", "objection_agency", "client_kickoff"):
        v = play(t, stu, key, GOOD[key][:-1], seed=5)
        assert v["score"]["total"] >= 75 and not v["score"]["passed"], key
        quit_early.append(v["id"])
    assert l2()["have"] == 0
    # 2. full passes, but with live hints telling the learner what to do next
    for key in ("discovery_call", "objection_agency", "client_kickoff"):
        v = play(t, stu, key, GOOD[key], seed=5)
        assert v["score"]["passed"] and v["hints"], key
    assert l2()["have"] == 0
    # ... or practice with the hint line off: the learner's own run trace still shows every turn's live scoring
    from operator_ai import runtime

    v = play(t, stu, "objection_think", GOOD["objection_think"], seed=5, hints=False)
    assert v["score"]["passed"] and not v["hints"] and not v["exam"]
    assert '"acts"' in json.dumps(runtime.get(t, v["run_id"])["steps"][1])
    assert l2()["have"] == 0
    # 3. unaided passes (exam mode: no hints, a seed the server picks) are the evidence
    for key in ("objection_partner", "objection_send_info", "warm_dm_signal"):
        v = play(t, stu, key, GOOD[key], exam=True)
        assert v["score"]["passed"] and v["exam"] and not v["hints"], key
    assert l2()["have"] == 3 and l2()["met"]
    # a coach who grades a session 20/100 has not signed it off
    team.signoff_roleplay(t, quit_early[0], coach, 20, "Not there yet: no next step.", "cora(coach)")
    assert not signoff()["met"]
    team.signoff_roleplay(t, quit_early[1], coach, 82, "Strong discovery.", "cora(coach)")
    assert signoff()["met"]


def test_the_persona_reacts_to_nonsense_like_a_person_would(tenant):
    from operator_ai.roleplay import replies, sessions

    stu = _user(tenant, "kit", "student")
    confused = set(replies.BANK[("unclear",)])
    v = sessions.start(tenant, stu, "discovery_call", seed=3)
    trust0 = v["meters"]["trust_value"]
    for nonsense in ("banana", "asdf qwer", "banana banana banana"):
        v = sessions.turn(tenant, stu, v["id"], nonsense)
        assert v["turns"][-1]["text"] in confused, (nonsense, v["turns"][-1]["text"])  # never "Go on." to gibberish
    assert v["meters"]["trust_value"] < trust0
    # short real replies are not nonsense
    for ok in ("Thanks!", "Okay.", "Sure, that makes sense."):
        v = sessions.turn(tenant, stu, v["id"], ok)
        assert v["turns"][-1]["text"] not in confused, ok


def test_a_first_client_for_the_certificate_is_never_self_reported(tenant):
    from operator_ai import prospects as P

    t = tenant
    stu = _user(t, "max", "student")
    th = common.path_def(t, "growth_operator")["thresholds"]["L3"]
    lawful = ("legitimate_interest", "Business contact details from their own public profile")

    def add(name: str) -> int:
        return P.add_prospect(t, stu, {"name": name, "email": f"{name.split()[0].lower()}@example.invalid"}, *lawful)[
            "prospect"]["id"]

    def real_work() -> dict:
        return certify._real_work(t, stu, "growth_operator", th, 0)

    # the learner marks their own prospect "won": one click, no one else involved
    mine = add("Dana Clinic")
    P.set_stage(t, mine, "won", "max(student)", user=stu)
    assert real_work()["have"] == 0 and not real_work()["met"]
    # the client accepted a proposal on its page (the system records the prospect as the actor)
    accepted = add("Eli Studio")
    P.mark_won(t, accepted, "prospect", price=1500, via="proposal")
    assert real_work()["have"] == 1 and real_work()["met"]
    # the owner records a win for the learner: evidence from someone else, counted once with its client record
    by_owner = add("Fern Physio")
    P.set_stage(t, by_owner, "won", "owner(owner)")
    assert real_work()["have"] == 2


# --------------------------------------------------------------------------- spaced repetition
def test_spaced_repetition_cannot_be_skipped_by_answering_a_card_again(client, harbor):
    t = harbor
    stu = _user(t, "ola", "setter")
    login(client, TID, "ola", PW)
    lesson = next(L for L in _lessons(t) if L["title"] == "Following up")
    assert client.post(f"{API}/academy/lessons/{lesson['id']}/complete").json()["new_review_cards"] >= 2
    with t.db() as c:
        cards = db.rows(c.execute("SELECT r.id, r.item_id, r.due FROM academy_review r WHERE r.user_id=? AND "
                                  "r.item_kind='quiz' ORDER BY r.id", (stu["id"],)))
        c.execute("UPDATE academy_review SET due=? WHERE id=?", (time.time() - 60, cards[0]["id"]))
    first, later = cards[0], cards[1]
    it = quiz.load(t, first["item_id"])
    wrong = "banana" if it["kind"] == "cloze" else next(o for o in (it["options"] or []) if o != it["answer"]) \
        if it["kind"] != "order" else list(reversed(it["answer"]))
    right = list(it["answer"]) if it["kind"] == "order" else it["answer"]
    url = f"{API}/academy/items/{first['item_id']}/answer"
    r = client.post(url, json={"answer": wrong, "context": "review", "ms": 4000}).json()
    assert r["review"]["grade"] == 1 and r["review"]["interval_days"] == 1.0
    due_after_miss = srs.card_for(t, stu["id"], "quiz", first["item_id"])["due"]
    # the answer is on screen now; answering it again and again must not push the card out of review
    for _ in range(6):
        r = client.post(url, json={"answer": right, "context": "review", "ms": 2000}).json()
        assert r["correct"] and r["review"] is None
    card = srs.card_for(t, stu["id"], "quiz", first["item_id"])
    assert card["due"] == due_after_miss and card["reps"] == 0 and card["interval"] == 1.0
    # a card that isn't due yet can't be reviewed early to push it further away either
    it2 = quiz.load(t, later["item_id"])
    r = client.post(f"{API}/academy/items/{it2['id']}/answer",
                    json={"answer": list(it2["answer"]) if it2["kind"] == "order" else it2["answer"], "context": "review"}).json()
    assert r["review"] is None
    assert srs.card_for(t, stu["id"], "quiz", it2["id"])["due"] == later["due"]


# --------------------------------------------------------------------------- coach dashboard
def test_the_coach_sees_the_mastery_the_learner_and_the_certificate_use(harbor):
    t = harbor
    coach = _user(t, "cal", "coach")
    stu = _user(t, "ivy", "setter")
    team.link_manager(t, coach["id"], stu["id"], "owner(owner)")
    # Ivy only practises DM questions, and gets them all right
    for it in _items(t, "i.pool='practice' AND i.kind!='write' AND i.skill='dms'"):
        learn.answer(t, stu, it["id"], list(it["answer"]) if it["kind"] == "order" else it["answer"], 4000)
    path = learn.path_summary(t, stu, "setter")
    row = next(p for p in team.cohort(t, coach)["people"] if p["id"] == stu["id"])
    assert row["mastery"] == path["mastery"], (row["mastery"], path["mastery"])
    l1 = {c["key"]: c for c in certify.criteria(t, stu, "setter", "L1")}
    assert l1["mastery_overall"]["have"] == f"{round(path['mastery'] * 100)}%"
    assert row["mastery"] < srs.mastery(t, stu["id"])["dms"]["chance"]  # unpractised skills are not hidden
