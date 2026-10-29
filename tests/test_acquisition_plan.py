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
    assert "8 clients away" in text and "deposit" in " ".join(plan["close"])


def test_chat_asks_for_numbers_then_sharpens_the_same_plan(client, env):
    demo_login(client, "student")
    first = client.post("/t/demo/api/chat", json={"text": "How do I get more clients?"}).json()
    assert first["intent"] == "acquisition" and "what you charge" in first["answer"]
    second = client.post("/t/demo/api/chat", json={"text": "I charge $5k and want $50k a month",
                                                     "thread_id": first["thread_id"]}).json()
    assert second["intent"] == "acquisition" and "10 clients" in second["answer"]
