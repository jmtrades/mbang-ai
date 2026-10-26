import json

import pytest

from operator_ai import diagnostic, evals, llm, runtime, settings, translate
from operator_ai.agents import coach, coaching, reviewer
from tests.fakes import FakeModel


@pytest.fixture
def ai_on(tenant, monkeypatch):
    fm = FakeModel()
    llm.set_transport(fm.transport)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-123456789")
    monkeypatch.setenv("OPERATOR_DISABLE_LLM", "0")
    settings.set_box(tenant, "ai_token_budget", 100000, "test budget")
    yield fm
    llm.set_transport(None)


# ---------------------------------------------------------------- coach
def test_thunder_dome_cited_sop_answer(tenant):
    r = coach.answer(tenant, "What are the rules for a Thunder Dome test?", record=False)
    assert r["covered"] == "yes"
    assert r["citations"] and r["citations"][0]["file"] == "sops/thunderdome.txt"
    assert "[S1]" in r["answer"] and "three and five creatives" in r["answer"]
    assert r["mode"] == "rules" and "AI off" in r["label"]


def test_tiktok_shop_not_covered(tenant):
    r = coach.answer(tenant, "How do I set up TikTok Shop?", record=False)
    assert r["covered"] == "no"
    assert r["answer"] == "The course material doesn't cover this." and r["citations"] == []


def test_out_of_scope_honesty_and_in_scope(tenant):
    m = evals.honesty_eval(tenant)
    assert m["honesty"] >= 0.9
    assert m["in_scope_answered"] >= 0.9


def test_multi_part_decomposed(tenant):
    r = coach.answer(tenant, "What is the Thunder Dome kill rule and what reminders do we send before calls?", record=False)
    assert len(r["parts"]) == 2
    files = {c["file"] for c in r["citations"]}
    assert "sops/thunderdome.txt" in files and ("sops/show_rate_sales_guide.txt" in files or
                                                "course/transcripts/vid_g7h8i9.txt" in files)


def test_partly_covered_names_the_gap(tenant):
    r = coach.answer(tenant, "What is the USP formula and how do I set up TikTok Shop?", record=False)
    assert r["covered"] == "partly" and "Not covered by the course material" in r["answer"]


def test_invented_citation_stripped(tenant, ai_on):
    ai_on.reply = json.dumps({"covered": "yes", "answer": "Run it for 72 hours [S1] and trust me [S99].",
                              "citations": ["S1", "S99"]})
    r = coach.answer(tenant, "How long should a Thunder Dome test run before I judge it?", record=False)
    assert "[S99]" not in r["answer"] and "S99" in r["stripped_citations"]
    assert [c["id"] for c in r["citations"]] == ["S1"]
    assert r["mode"] == "live"


def test_ai_answer_then_labelled_replay_when_ai_off(tenant, ai_on):
    ai_on.reply = json.dumps({"covered": "yes", "answer": "Three to five creatives [S1].", "citations": ["S1"]})
    first = coach.answer(tenant, "How many creatives should go into a Thunder Dome?", record=False)
    settings.set_box(tenant, "ai_token_budget", 0, "")
    again = coach.answer(tenant, "How many creatives should go into a Thunder Dome?", record=False)
    assert again["mode"] == "replay"
    assert again["label"].startswith(f"Replayed AI answer (run #{first['run_id']},")
    assert again["answer"] == "Three to five creatives [S1]."


def test_coach_run_trace_has_plan_steps(tenant):
    r = coach.answer(tenant, "What goes in a pre-call brief?", record=False)
    run = runtime.get(tenant, r["run_id"])
    assert [s["tool"] for s in run["steps"]][:4] == ["detect_language", "rewrite", "retrieve", "coverage"]
    assert run["plan"] == coach.PLAN and run["status"] == "done"


def test_not_used_passages_listed(tenant):
    r = coach.answer(tenant, "How do I run the chat during a webinar?", record=False)
    labels = [n["label"] for n in r["not_used"]]
    assert any(l.startswith("Not used: this passage describes planting") for l in labels)


def test_answers_are_scrubbed_of_banned_names(tenant):
    tenant.set("banned_names", ["Thunder Dome"])  # stand-in name for the test
    r = coach.answer(tenant, "What are the rules for a Thunder Dome test?", record=False)
    assert "Thunder Dome" not in r["answer"]
    assert all("Thunder Dome" not in (c["title"] + c["snippet"]) for c in r["citations"])


def test_coach_api_records_query_and_thread(client):
    from tests.conftest import demo_login

    demo_login(client, "student")
    r = client.post("/t/demo/api/coach/ask", json={"question": "What show rate should I aim for on booked calls?"})
    assert r.status_code == 200 and r.json()["thread_id"]
    tid = r.json()["thread_id"]
    r2 = client.post("/t/demo/api/coach/ask", json={"question": "How do I set up TikTok Shop?", "thread_id": tid})
    assert r2.json()["covered"] == "no"


# ---------------------------------------------------------------- translation
@pytest.mark.parametrize("q,lang", [
    ("Quelles sont les règles du Thunder Dome ?", "fr"),
    ("¿Cuándo debo enviar los recordatorios antes de la llamada?", "es"),
    ("Quais são as regras do Thunder Dome?", "pt"),
    ("What are the rules for a Thunder Dome test?", "en"),
])
def test_detect_language(q, lang):
    assert translate.detect(q) == lang


def test_translated_question_answered_with_note(tenant):
    r = coach.answer(tenant, "Quelles sont les règles du Thunder Dome ?", record=False)
    assert r["lang"] == "fr" and r["covered"] == "yes"
    assert r["citations"][0]["file"] == "sops/thunderdome.txt"
    assert r["note"].startswith("Réponse en anglais")


def test_translated_not_covered_in_language(tenant):
    r = coach.answer(tenant, "¿Cómo configuro una tienda de TikTok?", record=False)
    assert r["covered"] == "no" and r["answer"] == translate.NOT_COVERED["es"]


def test_glossary_maps_terms():
    assert "show rate" in translate.to_english_terms("Quel est le bon taux de présence ?", "fr")
    assert "reminders" in translate.to_english_terms("los recordatorios", "es")


# ---------------------------------------------------------------- reviewer
def test_reviewer_flaw_detection(tenant):
    m = evals.reviewer_eval(tenant)
    assert m["flaw_detection"] >= 0.8


def test_reviewer_heuristic_label_and_line_fixes(tenant):
    r = reviewer.review(tenant, "In today's fast-paced world, unlock growth.\nClick here! Buy now!")
    assert r["asset_type"] == "ad" and r["mode"] == "rules" and r["label"].startswith("Heuristic scoring")
    assert any(f["line_no"] == 2 and "call to action" in f["fix"].lower() for f in r["fixes"])
    assert all(c["id"].startswith("E") for c in r["citations"])
    assert 0 <= r["score"] <= 100


@pytest.mark.parametrize("text,at", [
    ("Subject: Your replay\nHi", "email"), ("Segment 1: hook (5 min)", "webinar"),
    ("Rep: hi\nProspect: hello", "sales_call"), ("Helping clinics | DM me", "profile_bio"),
    ("Hey Sam, loved the reel. Quick question?", "dm_script"), ("Don't forget your call tomorrow!", "reminder"),
])
def test_detect_asset_type(text, at):
    assert reviewer.detect_type(text) == at


def test_reviewer_ai_scores_but_guardrail_scores_stay_deterministic(tenant, ai_on):
    ai_on.reply = json.dumps({"criteria": [{"key": "hook", "score": 9, "quote": "x", "fix": "We guarantee it",
                                            "citations": ["E1", "E77"]},
                                           {"key": "guardrails", "score": 10}]})
    r = reviewer.review(tenant, "We guarantee results. Book now.", "ad")
    g = next(c for c in r["criteria"] if c["key"] == "guardrails")
    hook = next(c for c in r["criteria"] if c["key"] == "hook")
    assert g["score"] == 0 and hook["score"] == 9
    assert "guarantee" not in hook["fix"].lower()  # an AI fix that breaks the rules is dropped
    assert "E77" not in hook["citations"] and r["mode"] == "live"


# ---------------------------------------------------------------- coaching
def test_coaching_dm_uses_outreach_rule(tenant):
    r = coaching.coach_work(tenant, "Hey! What's your goal? How many clients? Our fee is $3,000 a month.", "dm")
    assert r["rule"] == "outreach"
    codes = {f.get("guardrail") for f in r["fixes"]}
    assert {"outreach_money"} & codes or {"outreach_price"} & codes
    sugg = [f["suggestion"] for f in r["fixes"] if f.get("suggestion")]
    assert "Let me help [their brand] grow through our system." in sugg


def test_coaching_client_asset_allows_price(tenant):
    r = coaching.coach_work(tenant, "Subject: Doors open\nThe programme is $2,997 for twelve weeks. Reply to join.", "email")
    assert r["rule"] == "client_asset"
    assert not any(f.get("guardrail", "").startswith("outreach") for f in r["fixes"])


def test_coaching_suggestions_pass_guardrails(tenant):
    from operator_ai import guardrails

    r = coaching.coach_work(tenant, "Don't forget your call tomorrow!", "reminder")
    for f in r["fixes"]:
        if f.get("suggestion"):
            assert not guardrails.blocked(guardrails.check(f["suggestion"], "plain"))
    assert any(f.get("suggestion") for f in r["fixes"])


# ---------------------------------------------------------------- diagnostic
METRICS = {"dms_sent": 800, "replies": 40, "conversations": 20, "links_sent": 10, "booked": 6, "showed": 2, "closed": 1,
           "offer_price": 3000}


def test_diagnostic_pure_maths():
    a = diagnostic.analyse(METRICS)
    by = {s["key"]: s for s in a["stages"]}
    assert by["reply"]["value"] == 0.05 and by["reply"]["below"]
    assert by["show"]["value"] == pytest.approx(0.3333, abs=1e-3)
    # reply: (0.10-0.05)*800 extra replies * downstream 0.5*0.5*0.6*0.333*0.5 * 3000
    assert by["reply"]["monthly_cost"] == 3000
    assert a["bottleneck"]["key"] == "reply"


def test_diagnostic_ignores_unrealistic_stage():
    m = {"booked": 4, "showed": 0, "closed": 0, "offer_price": 3000}  # too little volume to trust
    assert diagnostic.analyse(m)["bottleneck"] is None


def test_diagnostic_cpl_stage():
    a = diagnostic.analyse({"ad_spend": 3000, "leads": 100, "offer_price": 3000})
    cpl = next(s for s in a["stages"] if s["key"] == "cpl")
    assert cpl["value"] == 30 and cpl["below"]


def test_diagnose_cited_with_next_best_action(tenant):
    r = diagnostic.diagnose(tenant, METRICS)
    assert r["bottleneck"]["key"] == "reply"
    assert r["citations"][0]["file"] == "sops/show_rate_sales_guide.txt"
    by = {s["key"]: s for s in r["stages"]}
    assert by["reply"]["citation"]["file"] == "sops/show_rate_sales_guide.txt"
    assert "[S1]" in r["prescription"]
    assert r["next_best_action"]["line"] == "outreach"
    assert runtime.get(tenant, r["run_id"])["status"] == "done"


def test_diagnose_api(owner):
    r = owner.post("/t/owner/api/diagnose", json={"metrics": METRICS})
    assert r.status_code == 200 and r.json()["bottleneck"]["key"] == "reply"
