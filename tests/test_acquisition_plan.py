"""'How do I get more clients?' -> the programme's acquisition method, worked out on the asker's own numbers."""
from tests.conftest import demo_login

from operator_ai import acquisition_plan as ap
from operator_ai.agents.router import classify


def test_routes_client_questions_but_not_others():
    assert classify("How do I get more clients?") == "acquisition"
    assert classify("how can I land more high ticket clients") == "acquisition"
    assert classify("What show rate should I aim for?") == "question"
    assert classify("800 DMs, 40 replies, 6 booked, 2 showed") == "metrics"


def test_plan_uses_their_numbers_and_shows_the_price_lever():
    s = ap.parse("I charge $3k, want to hit $30k a month, close rate 25%, 2 clients now")
    assert s["price"] == 3000 and s["goal"] == 30000 and s["close_rate"] == 25 and s["clients"] == 2
    plan = ap.build(s)
    assert plan["math"]["clients"] == 10 and plan["math"]["calls"] == 40 and plan["math"]["booked"] == 80
    assert plan["math"]["gap_clients"] == 8 and plan["higher_price"]["clients"] == 3
    text = ap.reply_text(plan)
    assert "you need 8 more" in text and "deposit" in " ".join(plan["close"])


def test_chat_asks_for_numbers_then_sharpens_the_same_plan(client, env):
    demo_login(client, "student")
    first = client.post("/t/demo/api/chat", json={"text": "How do I get more clients?"}).json()
    assert first["intent"] == "acquisition" and "what you charge" in first["answer"]
    second = client.post("/t/demo/api/chat", json={"text": "I charge $5k and want $50k a month",
                                                     "thread_id": first["thread_id"]}).json()
    assert second["intent"] == "acquisition" and "10 clients" in second["answer"]


def test_with_ai_on_the_plan_is_written_by_the_model_from_the_same_numbers(tenant, monkeypatch):
    from operator_ai import llm
    from tests.fakes import FakeModel

    fm = FakeModel(reply="Here's your plan, in plain words.")
    llm.set_transport(fm.transport)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key-123456789")
    monkeypatch.setenv("OPERATOR_DISABLE_LLM", "0")
    tenant.set("ai_token_budget", 50_000)
    try:
        reply, meta = ap.from_chat("I charge $3k and want $30k a month", None, tenant)
    finally:
        llm.set_transport(None)
    assert meta["mode"] == "live" and reply == "Here's your plan, in plain words."
    sent = fm.calls[-1]["body"]["messages"][-1]["content"]
    assert "clients needed 10" in sent and "booked calls 100" in sent  # the model writes from our computed numbers
