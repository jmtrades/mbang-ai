"""Coach with the AI on: a question no lesson covers gets a typo-proof second search, then an answer in the
programme's voice from its method notes (labelled as such), never loose playbook bullets or a dead end. With the AI
off, the honest 'not covered' stands."""
import json

from tests.fakes import FakeModel

from operator_ai import llm
from operator_ai.agents import coach

Q = "how do i convice my cleints to scale adsoend"


def test_ai_off_keeps_the_honest_not_covered(tenant):
    res = coach.answer(tenant, Q, record=False)
    assert res["covered"] == "no" and "doesn't cover" in res["answer"] and not res.get("method")


def test_ai_on_answers_from_the_method_with_a_clear_label(tenant, monkeypatch):
    fm = FakeModel()
    fm.replies = [json.dumps({"queries": ["zzqx unmatched phrasing"]}),
                  "Show your client their own numbers.\n• Work back from their revenue goal to the ad spend it takes."]
    llm.set_transport(fm.transport)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-123456789")
    monkeypatch.setenv("OPERATOR_DISABLE_LLM", "0")
    tenant.set("ai_token_budget", 50_000)
    tenant.set("third_party_kb", False)
    try:
        res = coach.answer(tenant, Q, record=False)
    finally:
        llm.set_transport(None)
    assert res.get("method") and res["mode"] == "live" and res["external"] == []
    assert res["answer"].startswith("Show your client their own numbers") and "programme's method" in res["label"]
    system = fm.calls[-1]["body"]["system"]
    assert "Method notes" in system and "You don't \"convince\" anyone" in system and "Example of the voice" in system
