"""OpenAI-compatible endpoints (OpenRouter, Gemini, local servers) via OPERATOR_LLM_BASE_URL."""
import httpx

from operator_ai import llm


def _capture(seen):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}],
                                         "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
    return httpx.MockTransport(handler)


def test_openai_provider_uses_custom_base_url(monkeypatch):
    seen = []
    monkeypatch.setenv("OPERATOR_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPERATOR_LLM_MODEL", "google/gemini-2.5-pro")
    monkeypatch.setenv("OPERATOR_LLM_BASE_URL", "https://openrouter.ai/api/v1/")
    monkeypatch.setenv("OPERATOR_LLM_API_KEY", "test-key")
    llm.set_transport(_capture(seen))
    try:
        text, _ = llm._request("sys", "hi", max_tokens=10, timeout=5)
    finally:
        llm.set_transport(None)
    assert text == "ok"
    assert seen == ["https://openrouter.ai/api/v1/chat/completions"]


def test_openai_provider_defaults_to_openai(monkeypatch):
    seen = []
    monkeypatch.setenv("OPERATOR_LLM_PROVIDER", "openai")
    monkeypatch.delenv("OPERATOR_LLM_BASE_URL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    llm.set_transport(_capture(seen))
    try:
        llm._request("sys", "hi", max_tokens=10, timeout=5)
    finally:
        llm.set_transport(None)
    assert seen == ["https://api.openai.com/v1/chat/completions"]
