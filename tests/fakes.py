"""In-process fakes for every outside service. Only the HTTP transport is replaced; the
product code paths are the real ones."""
from __future__ import annotations

import json
import threading

import httpx


class FakeModel:
    """A reachable fake model server (Anthropic- and OpenAI-shaped replies)."""

    def __init__(self, reply: str = "Fake AI reply.", status: int = 200, delay: float = 0.0):
        self.reply = reply
        self.status = status
        self.delay = delay
        self.calls: list[dict] = []
        self.lock = threading.Lock()
        self.replies: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        import time

        with self.lock:
            body = json.loads(request.content or b"{}")
            self.calls.append({"url": str(request.url), "body": body})
            reply = self.replies.pop(0) if self.replies else self.reply
        if self.delay:
            time.sleep(self.delay)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "fake"})
        if "openai" in request.url.host:
            return httpx.Response(200, json={"choices": [{"message": {"content": reply}}],
                                             "usage": {"total_tokens": 120}})
        return httpx.Response(200, json={"content": [{"type": "text", "text": reply}],
                                         "usage": {"input_tokens": 80, "output_tokens": 40}})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)
