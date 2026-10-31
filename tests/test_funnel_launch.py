"""One sentence -> a whole VSL funnel (script, VSL page, application page, thank-you page, follow-ups) waiting on one
approval; approving puts the pages live; applications are stored; students see only their own."""
import json

from tests.conftest import demo_login
from tests.fakes import FakeModel

from operator_ai import funnel_launch, llm
from operator_ai.agents.router import classify


def test_routes_funnel_asks_but_not_webinars():
    assert classify("Build a VSL funnel for my section 8 client") == "funnel"
    assert classify("write me a sales page for my trading client") == "funnel"
    assert classify("Launch a webinar for my trading client on November 12 at 7pm ET") == "launch"


def test_chat_builds_approve_publishes_apply_and_script(owner):
    owner.post("/t/owner/api/clients", json={"name": "Keys To Cashflow", "niche": "Section 8 real estate",
                                             "offer": "Section 8 investor mentorship", "price": 7500})
    d = owner.post("/t/owner/api/chat", json={"text": "Build a VSL funnel for my section 8 client "
                                                      "https://calendly.com/keys/strategy"}).json()
    assert d["intent"] == "funnel", d
    f = d["meta"]["funnel"]
    assert not f["live"] and f["booking_url"] == "https://calendly.com/keys/strategy"
    assert "Section 8" in f["title"] and len(f["copy"]["script"]) == 8
    slug = f["page_url"].rstrip("/").split("/")[-1]
    assert owner.get(f"/f/owner/{slug}").status_code == 404  # nothing public before approval

    assert owner.post(f"/t/owner/api/queue/{f['pack']['id']}/approve").status_code == 200
    page = owner.get(f"/f/owner/{slug}")
    assert page.status_code == 200 and "video is coming soon" in page.text and "Keys To Cashflow" in page.text
    s = owner.patch(f"/t/owner/api/funnels/{f['funnel_id']}",
                    json={"video_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}).json()
    assert s["video_url"].endswith("dQw4w9WgXcQ")
    assert "youtube.com/embed/dQw4w9WgXcQ" in owner.get(f"/f/owner/{slug}").text
    bad_link = owner.patch(f"/t/owner/api/funnels/{f['funnel_id']}", json={"booking_url": "javascript:x"})
    assert bad_link.status_code == 422

    assert owner.get(f"/f/owner/{slug}/apply").status_code == 200
    bad = owner.post(f"/f/owner/{slug}/apply", json={"name": "Pat", "email": "pat@x.example.com"})
    assert bad.status_code == 422  # consent required
    ok = owner.post(f"/f/owner/{slug}/apply", json={"name": "Pat", "email": "pat@x.example.com", "consent": True,
                                                    "revenue": "$20k", "goal": "$50k"})
    assert ok.status_code == 200 and ok.json()["next"].endswith("/thanks")
    thanks = owner.get(f"/f/owner/{slug}/thanks")
    assert thanks.status_code == 200 and "calendly.com/keys/strategy" in thanks.text
    assert owner.get(f"/t/owner/api/funnels/{f['funnel_id']}").json()["applications"] == 1
    script = owner.get(f["script_url"])
    assert script.status_code == 200 and "VSL SCRIPT" in script.text and "CALL TO ACTION" in script.text


def test_ai_writes_the_copy_and_script(tenant, monkeypatch):
    sections = ["Hook", "The problem", "The big mistake", "The mechanism", "Proof", "The offer",
                "Who it's for (and not for)", "Call to action"]
    fm = FakeModel(reply=json.dumps({"headline": "Your first Section 8 deal, without the guesswork",
                                     "subhead": "A short video.", "bullets": ["One", "Two", "Three"], "cta": "Apply",
                                     "script": [{"section": s, "text": f"{s} text."} for s in sections],
                                     "followups": ["a", "b", "c"]}))
    llm.set_transport(fm.transport)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-123456789")
    monkeypatch.setenv("OPERATOR_DISABLE_LLM", "0")
    tenant.set("ai_token_budget", 50_000)
    try:
        s = funnel_launch.launch(tenant, {"niche": "Section 8 real estate"})
    finally:
        llm.set_transport(None)
    assert s["words"] == "ai" and s["title"] == "Your first Section 8 deal, without the guesswork"
    assert s["copy"]["script"][0]["text"] == "Hook text." and "Method notes" in fm.calls[-1]["body"]["system"]


def test_student_sees_and_approves_only_their_own(client, env):
    demo_login(client, "student")
    mine = client.post("/t/demo/api/funnels", json={"niche": "fitness coaching"}).json()
    assert client.post(f"/t/demo/api/queue/{mine['pack']['id']}/approve").status_code == 200
    demo_login(client, "owner")
    other = client.post("/t/demo/api/funnels", json={"niche": "dating"}).json()
    demo_login(client, "student")
    assert client.get(f"/t/demo/api/funnels/{other['funnel_id']}").status_code == 404
    ids = {x["funnel_id"] for x in client.get("/t/demo/api/funnels").json()["items"]}
    assert mine["funnel_id"] in ids and other["funnel_id"] not in ids
    slug = mine["page_url"].rstrip("/").split("/")[-1]
    real = client.post(f"/f/demo/{slug}/apply", json={"name": "R", "email": "real@gmail.com", "consent": True})
    assert real.status_code == 422  # the demo takes only fictional addresses


def test_ai_copy_that_trips_a_guardrail_falls_back_to_the_template(tenant, monkeypatch):
    sections = ["Hook", "The problem", "The big mistake", "The mechanism", "Proof", "The offer",
                "Who it's for (and not for)", "Call to action"]
    fm = FakeModel(reply=json.dumps({"headline": "Last chance, doors closing", "subhead": "x", "bullets": ["a"],
                                     "cta": "Act now", "script": [{"section": s, "text": "Act now, last chance!"}
                                                                  for s in sections], "followups": ["a"]}))
    llm.set_transport(fm.transport)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-123456789")
    monkeypatch.setenv("OPERATOR_DISABLE_LLM", "0")
    tenant.set("ai_token_budget", 50_000)
    try:
        s = funnel_launch.launch(tenant, {"niche": "fitness coaching"})
    finally:
        llm.set_transport(None)
    assert s["words"] == "template" and s["pack"]["status"] == "pending"
