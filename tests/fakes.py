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


class FakeWorld:
    """Fake Meta Graph + Google OAuth/Calendar servers behind one transport. Any other host
    gets a 599 and is recorded as a stray call (tests assert there are none)."""

    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []
        self.stray: list[str] = []
        self.leads: dict[str, dict] = {}
        self.events: dict[str, dict] = {}
        self.busy: list[dict] = []
        self.fail_next: list[int] = []  # status codes to return before succeeding
        self.lock = threading.Lock()
        self.ads: dict[str, dict] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        host, path, method = request.url.host, request.url.path, request.method
        with self.lock:
            self.calls.append((method, host, path))
            if self.fail_next:
                code = self.fail_next.pop(0)
                hdr = {"retry-after": "0"} if code == 429 else {}
                body = {"error": {"code": 17, "message": "rate"}} if code == 400 else {"error": "x"}
                return httpx.Response(code, json=body, headers=hdr)
        if host == "graph.facebook.com":
            return self._meta(request)
        if host == "oauth2.googleapis.com" and path == "/token":
            form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            if form.get("grant_type") != "refresh_token" or not form.get("refresh_token"):
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "ya29.fake-access-token-123", "expires_in": 3600,
                                             "scope": "calendar.events calendar.freebusy"})
        if host == "www.googleapis.com":
            return self._gcal(request)
        with self.lock:
            self.stray.append(f"{method} {host}{path}")
        return httpx.Response(599, json={"error": "stray host"})

    def _meta(self, request):
        q = dict(request.url.params)
        if not q.get("access_token") or not q.get("appsecret_proof"):
            return httpx.Response(400, json={"error": {"code": 190, "message": "missing token/proof"}})
        parts = request.url.path.strip("/").split("/")
        if request.method == "GET" and len(parts) == 2 and parts[1] in self.leads:
            return httpx.Response(200, json=self.leads[parts[1]])
        if request.method == "POST":
            body = json.loads(request.content or b"{}")
            oid = f"fb_{len(self.ads) + 1}"
            self.ads[oid] = body
            return httpx.Response(200, json={"id": oid})
        return httpx.Response(404, json={"error": {"code": 100, "message": "not found"}})

    def _gcal(self, request):
        if request.headers.get("authorization") != "Bearer ya29.fake-access-token-123":
            return httpx.Response(401, json={"error": "unauthenticated"})
        path = request.url.path
        if path.endswith("/freeBusy"):
            body = json.loads(request.content)
            cal = body["items"][0]["id"]
            return httpx.Response(200, json={"calendars": {cal: {"busy": self.busy}}})
        if request.method == "POST" and path.endswith("/events"):
            body = json.loads(request.content)
            if body["id"] in self.events:
                return httpx.Response(409, json={"error": {"code": 409, "message": "duplicate"}})
            self.events[body["id"]] = body
            return httpx.Response(200, json={**body, "status": "confirmed"})
        if request.method == "GET" and "/events/" in path:
            eid = path.rsplit("/", 1)[1]
            if eid in self.events:
                return httpx.Response(200, json={**self.events[eid], "status": "confirmed"})
            return httpx.Response(404, json={"error": "nf"})
        return httpx.Response(404, json={"error": "nf"})

    @property
    def transport(self):
        return httpx.MockTransport(self.handler)
