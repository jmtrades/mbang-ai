"""Static ads: three finished image ads from one brief, each waiting for approval with its picture."""
import base64
import io

import httpx
from tests.conftest import demo_login

from operator_ai import llm, static_ads

BRIEF = {"business": "Apex Trading Academy", "niche": "trading education", "audience": "new traders",
         "outcome": "consistent results", "mechanism": "3-step trade plan", "pain": "blowing up their account",
         "timeframe": "90 days", "offer": "6-week mentorship", "price": 4997}


def test_designed_ads_without_image_ai(owner):
    r = owner.post("/t/owner/api/static-ads", json={"brief": BRIEF})
    assert r.status_code == 200, r.text
    d = r.json()
    assert len(d["ads"]) == 3 and not d["image_ai"] and {a["mode"] for a in d["ads"]} == {"design"}
    assert any(a["headline"] == "Tired of blowing up your account?" for a in d["ads"])
    img = owner.get(d["ads"][0]["image"])
    assert img.status_code == 200 and img.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert img.headers["content-type"] == "image/png"
    q = owner.get(f"/t/owner/api/queue/{d['queue_ids'][0]}").json()
    item = q.get("item", q)
    assert item["kind"] == "static_ad" and item["meta"]["image"] == d["ads"][0]["image"]
    assert owner.get("/t/owner/api/static-ads/..%2F..%2Fsecret.png").status_code == 404
    assert owner.get("/t/owner/api/static-ads/nothere1234.png").status_code == 404


def test_image_ai_paints_the_background(tenant, monkeypatch):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (30, 120, 200)).save(buf, "PNG")
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(buf.getvalue()).decode()}]})

    llm.set_transport(httpx.MockTransport(handler))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123456789")
    monkeypatch.setenv("OPERATOR_DISABLE_LLM", "0")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-123456789")
    tenant.set("ai_token_budget", 50_000)
    try:
        d = static_ads.make(tenant, BRIEF)
    finally:
        llm.set_transport(None)
    assert d["image_ai"] and [a["mode"] for a in d["ads"]] == ["ai", "ai", "ai"] and len(calls) == 3
    assert "no text" in calls[0].content.decode().lower()


def test_students_cannot_make_static_ads(client, env):
    demo_login(client, "student")
    assert client.post("/t/demo/api/static-ads", json={"brief": BRIEF}).status_code in (401, 403)
