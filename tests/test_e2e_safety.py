"""End-to-end approval, consent and claim safety across features (hostile review, 2026-10-27).

Each test walks a real feature path and checks that nothing reaches a prospect or a student that a human did not
approve, that nothing is marked sent before it really went out, that persona styling follows consent and the
person's own choice, and that no income or result claim slips through a feature the guardrails don't see."""
from __future__ import annotations

import time

from operator_ai import console, guardrails, persona, queue, tenancy
from operator_ai import prospects as P
from operator_ai.agents import accountability, coach
from tests.test_persona import activate, stu_user
from tests.test_persona import world as pworld  # noqa: F401 - the persona world fixture, under its own name
from tests.test_prospects import API, _import, _proposal
from tests.test_prospects import world as pros  # noqa: F401 - the first-client world fixture, under its own name

CLAIMS_SOP = (
    "Talking about results\n"
    "Most of our students make $10k a month within ninety days of joining the programme.\n"
    "We guarantee every student books five sales calls in their first week on the programme.\n"
    "If the lead is warm and qualified you can mention the range: let me make your brand fifty to a hundred K a month "
    "through our system.\n"

    "Ask about their current numbers before you talk about results on a sales call.\n"
    "Confirm the call in writing and send a reminder the day before.\n"
)
RESULT_Q = "What results do students make within ninety days of joining the programme?"
QUESTIONS = (RESULT_Q, "What do we guarantee students in their first week on the programme?")


def _claims_corpus(env) -> None:
    (env / "inputs" / "sops").mkdir(parents=True, exist_ok=True)
    (env / "inputs" / "sops" / "results.txt").write_text(CLAIMS_SOP)


def _no_claim(text: str) -> None:
    low = text.lower()
    assert "$10k" not in low and "guarantee every student" not in low and "hundred k" not in low, text


# --------------------------------------------------------------------------- public minis (prospects, no approval)
def test_public_mini_never_quotes_income_claims_or_figures_to_a_stranger(env, client):
    _claims_corpus(env)
    t = tenancy.get("owner")
    console.create_mini(t, "open", "Ask the course", [], 10, "I agree")
    console.create_mini(t, "scoped", "Results helper", ["sops/results.txt"], 10, "I agree")
    for slug in ("open", "scoped"):
        tok = client.post(f"/m/owner/{slug}/start", json={"email": f"{slug}@example.invalid", "consent": True}).json()["token"]
        for q in QUESTIONS:
            r = client.post(f"/m/owner/{slug}/ask", json={"token": tok, "question": q})
            assert r.status_code == 200, r.text
            out = r.json()
            _no_claim(out["answer"])
            assert guardrails.money_hit(guardrails._money_norm(guardrails._norm(out["answer"]))) is None, out["answer"]
            if out["covered"] == "no":
                assert out["citations"] == []


def test_public_mini_never_shows_the_range_figure_even_from_teaching_material(env, client):
    # the fixture course teaches setters the range line ("fifty to a hundred K a month"): fine for a student in the
    # Coach, never for a stranger on a public page that nobody approves
    t = tenancy.get("owner")
    q = "When can I mention the range to a warm lead?"
    assert "hundred K a month" in coach.answer(t, q, record=False)["answer"]
    console.create_mini(t, "open", "Ask the course", [], 10, "I agree")
    console.create_mini(t, "scoped", "Lead helper", ["course/transcripts/vid_d4e5f6.txt"], 10, "I agree")
    for slug in ("open", "scoped"):
        tok = client.post(f"/m/owner/{slug}/start", json={"email": f"{slug}@example.invalid", "consent": True}).json()["token"]
        out = client.post(f"/m/owner/{slug}/ask", json={"token": tok, "question": q}).json()
        assert "hundred k" not in out["answer"].lower() and guardrails.money_figure(out["answer"]) is None, out


def test_student_coach_never_quotes_an_income_claim_from_the_material(env, tenant):
    _claims_corpus(env)
    for q in QUESTIONS[:2]:
        res = coach.answer(tenant, q, record=False)
        _no_claim(res["answer"])
    # objection drills are lines a rep says to a prospect: never a claim from the material
    from operator_ai import call_review

    _no_claim(call_review.make_drill(tenant, "Do students really make money within ninety days?", RESULT_Q, "test")["answer"])
    # the honest teaching lines still answer
    res = coach.answer(tenant, "What should I ask about before I talk about results on a sales call?", record=False)
    assert res["covered"] != "no" and "current numbers" in res["answer"]


# --------------------------------------------------------------------------- client assets and proposals (prospect-facing)
def test_client_assets_get_the_range_rules_and_no_personal_income_promise(tenant):
    def codes(text):
        return {f["code"] for f in guardrails.check_for(tenant, text, {"client_asset": True})}

    # a person's income, phrased as the range line: an income claim in every mode
    assert "income_claim" in codes("Let me make you $50k-$100k a month through our system.")
    assert "income_claim" in {f["code"] for f in guardrails.check("Let me make you $40k a month.", "plain")}
    # the range itself needs a current proof record, and is never definite
    assert "unsubstantiated_claim" in codes("Let me make Acme $50k-$100k a month through our system.")
    assert guardrails.blocked(guardrails.check_for(tenant, "We will add $50k-$100k a month to your revenue.",
                                                   {"client_asset": True}))
    # prices and the brand line without a figure stay fine
    assert codes("The programme is $2,997 and includes twelve weeks of coaching.") == set()
    assert codes("Let me help Acme grow through our referral system.") == set()
    assert codes("For clinics that make $1M a year and want more referrals.") == set()


def test_proposal_cannot_promise_the_range_or_a_personal_income(pros):  # noqa: F811
    _import(pros, "stu_a")
    _, pr = _proposal(pros)
    a = pros.as_("stu_a")
    for text in ("Let me make you $50k-$100k a month through our system.",
                 "Let me make Mara's studio $50k-$100k a month through our system.",
                 "We will add $50k-$100k a month to your revenue."):
        r = a.put(f"{API}/prospects/proposals/{pr['id']}", json={"sections": [{"key": "plan", "text": text}]})
        assert r.status_code == 200 and r.json()["proposal"]["blocked"], text
        s = a.post(f"{API}/prospects/proposals/{pr['id']}/send", json={"channel": "email"})
        assert s.status_code == 409, text


# --------------------------------------------------------------------------- delivery truth: approved is not sent
def test_approving_a_proposal_does_not_mark_it_sent_until_the_person_confirms(pros):  # noqa: F811
    t = pros.t
    _import(pros, "stu_a")
    pid, pr = _proposal(pros)
    a = pros.as_("stu_a")
    before = P._row(t, pid)["stage"]
    s = a.post(f"{API}/prospects/proposals/{pr['id']}/send", json={"channel": "instagram_dm"}).json()
    r = a.post(f"{API}/queue/{s['queue_id']}/approve")
    assert r.status_code == 200 and r.json()["status"] == "awaiting_manual_send"
    got = a.get(f"{API}/prospects/proposals/{pr['id']}").json()["proposal"]
    assert got["url"] and not got["sent_at"]  # the link works, so the person can paste it
    assert "sent" not in got["status_label"].lower().split(":")[0]
    assert P._row(t, pid)["stage"] == before  # not "Proposal sent" yet
    with t.db() as c:
        assert c.execute("SELECT COUNT(*) FROM prospect_events WHERE prospect_id=? AND kind='proposal_sent'",
                         (pid,)).fetchone()[0] == 0
    # "I sent it": now it counts
    assert a.post(f"{API}/queue/{s['queue_id']}/sent", json={}).status_code == 200
    got = a.get(f"{API}/prospects/proposals/{pr['id']}").json()["proposal"]
    assert got["sent_at"] and got["status_label"].startswith("Sent")
    assert P._row(t, pid)["stage"] == "proposal"
    with t.db() as c:
        assert c.execute("SELECT COUNT(*) FROM prospect_events WHERE prospect_id=? AND kind='proposal_sent'",
                         (pid,)).fetchone()[0] == 1


# --------------------------------------------------------------------------- persona: the person's own choice
def test_student_nudges_respect_the_students_neutral_voice(client, pworld, monkeypatch):  # noqa: F811
    from operator_ai.persona import mine

    # the subject prefers "call" over "meeting" (a mined word preference, signed for with the profile)
    monkeypatch.setattr(mine, "lexicon", lambda col: [{"prefer": "call", "over": "meeting", "share": 0.9, "n": 20,
                                                        "evidence": []}])
    activate(client, pworld, scopes=("coach_answers", "student_nudges"))
    plain = "Hey {first}, book a meeting with your coach this week."
    assert persona.style_template(pworld.t, plain, "student_nudges") != plain  # the lexicon styles this text
    monkeypatch.setitem(accountability.NUDGES, "checkin_gap", plain)
    now = time.time()
    with pworld.t.db() as c:
        sid = c.execute("INSERT INTO students(name,email,joined,clients,demo) VALUES(?,?,?,?,0)",
                        ("Student One", "stu@example.invalid", now - 100 * 86400, 0)).lastrowid
        c.execute("UPDATE users SET student_id=? WHERE id=?", (sid, pworld.stu))
    persona.set_neutral_voice(pworld.t, pworld.stu, True)
    out = accountability.run_interventions(pworld.t)
    bodies = [queue.get(pworld.t, q, scrub=False) for q in out["nudges"]]
    mine = [b for b in bodies if b["meta"].get("student_id") == sid]
    assert mine and mine[0]["body"] == plain.format(first="Student"), mine[0]["body"]
    # a student who keeps the styled voice still gets it
    persona.set_neutral_voice(pworld.t, pworld.stu, False)
    assert persona.style_template(pworld.t, plain, "student_nudges", user=stu_user(pworld)) != plain


def test_withdrawing_consent_unstyles_nudges_still_waiting_for_approval(client, pworld, monkeypatch):  # noqa: F811
    from operator_ai.persona import mine
    from tests.test_persona import as_kofi

    monkeypatch.setattr(mine, "lexicon", lambda col: [{"prefer": "call", "over": "meeting", "share": 0.9, "n": 20,
                                                        "evidence": []}])
    activate(client, pworld, scopes=("coach_answers", "student_nudges"))
    plain = "Hey {first}, book a meeting with your coach this week."
    monkeypatch.setitem(accountability.NUDGES, "checkin_gap", plain)
    with pworld.t.db() as c:
        sid = c.execute("INSERT INTO students(name,email,joined,clients,demo) VALUES(?,?,?,?,0)",
                        ("Student One", "stu@example.invalid", time.time() - 100 * 86400, 0)).lastrowid
    out = accountability.run_interventions(pworld.t)
    qid = next(q for q in out["nudges"] if queue.get(pworld.t, q, scrub=False)["meta"].get("student_id") == sid)
    assert "book a call" in queue.get(pworld.t, qid, scrub=False)["body"]  # styled under the signed consent
    as_kofi(client)
    assert client.post("/t/owner/api/persona/withdraw", json={}).status_code == 200
    item = queue.get(pworld.t, qid, scrub=False)
    assert item["status"] in queue.OPEN and item["body"] == plain.format(first="Student"), item["body"]
    assert "persona_neutral" not in item["meta"]


def test_a_reply_to_a_pasted_facebook_message_waits_for_i_sent_it(pros):  # noqa: F811
    from operator_ai import inbound

    t = pros.t
    _import(pros, "stu_a")
    pid = P._row(t, next(p["id"] for p in pros.as_("stu_a").get(f"{API}/prospects").json()["prospects"]
                         if p["name"] == "Mara Quill"))["id"]
    res = inbound.capture(t, pros.users["stu_a"], "facebook", "strongmums_x", "Sounds interesting, tell me more.", pid,
                          "stu_a(student)")
    assert res["queued"]
    a = pros.as_("stu_a")
    r = a.post(f"{API}/queue/{res['queued']}/approve")
    assert r.status_code == 200 and r.json()["status"] == "awaiting_manual_send", r.json()
    with t.db() as c:
        assert c.execute("SELECT status FROM inbound_messages WHERE id=?", (res["inbound_id"],)).fetchone()[0] != "replied"
    assert a.post(f"{API}/queue/{res['queued']}/sent", json={}).status_code == 200
    with t.db() as c:
        assert c.execute("SELECT status FROM inbound_messages WHERE id=?", (res["inbound_id"],)).fetchone()[0] == "replied"


def test_webinar_title_cannot_carry_a_claim_onto_the_public_page(client, tenant):
    # the title is printed on the public page and in every email (outside the page copy a person approves and edits),
    # so a claim in it is refused up front
    from operator_ai import webinars

    owner = {"id": 1, "username": "owner", "role": "owner"}
    base = {"starts_at": time.time() + 3 * 86400, "tz": "Europe/London"}
    for title in ("Guaranteed $10k months for coaches", "How our students make $30k a month"):
        try:
            webinars.create(tenant, {**base, "title": title}, owner)
        except webinars.WebinarError as e:
            assert e.status == 422
        else:
            raise AssertionError(f"accepted {title!r}")
    w = webinars.create(tenant, {**base, "title": "Five numbers every coach should track"}, owner)
    assert w["page_queue_id"]
