"""Provider-pluggable model client behind a fail-closed budget gate.

* ai_token_budget defaults to 0, which means no model call from any thread.
* Budgets can only be changed by the server environment or scripts/tenant_admin.py.
* Every successful output is saved so it can be replayed (labelled) when the AI is off.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time

import httpx

from . import config, db
from .redact import redact

_lock = threading.Lock()
# Tests may swap the HTTP transport for an in-process fake model server.
_transport: httpx.BaseTransport | None = None
REQUESTS_SENT = 0  # counter used by the budget-0 test


class AIUnavailable(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def set_transport(t: httpx.BaseTransport | None) -> None:
    global _transport
    _transport = t


def budget(tenant, key: str = "ai_token_budget") -> int:
    cfg = config.get()
    default = cfg.ai_token_budget_default if key == "ai_token_budget" else cfg.critique_token_budget_default
    if tenant is None:
        return default
    v = tenant.get(key, None)
    if v is None:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def used(tenant, key: str = "ai_token_budget") -> int:
    if tenant is None:
        return 0
    return int(tenant.get(key.replace("_budget", "_used"), 0) or 0)


def unavailable_reason(tenant, key: str = "ai_token_budget") -> str | None:
    cfg = config.get()
    b = budget(tenant, key)
    if b <= 0:
        return "AI off (budget 0)" if key == "ai_token_budget" else "Critique off (critique budget 0)"
    if cfg.disable_llm:
        return "AI disabled on this server (OPERATOR_DISABLE_LLM=1)"
    if used(tenant, key) >= b:
        return "AI budget used up"
    if not config.llm_key():
        return "AI offline: no model key set"
    return None


def state(tenant=None) -> dict:
    reason = unavailable_reason(tenant)
    cfg = config.get()
    return {
        "enabled": reason is None,
        "label": "AI connected" if reason is None else reason,
        "pill": "AI connected" if reason is None else "AI offline: saved AI runs replay (labelled)",
        "provider": cfg.llm_provider,
        "model": cfg.llm_model,
        "budget": budget(tenant),
        "used": used(tenant),
        "critique_budget": budget(tenant, "critique_token_budget"),
    }


def _key(purpose: str, prompt: str) -> str:
    return hashlib.sha256(f"{purpose}\n{prompt}".encode()).hexdigest()


def save_replay(tenant, purpose: str, prompt: str, output: str, run_id: int | None = None) -> None:
    with tenant.db() as c:
        c.execute("INSERT OR REPLACE INTO ai_replays(key,purpose,output,run_id,created) VALUES(?,?,?,?,?)",
                  (_key(purpose, prompt), purpose, output, run_id, db.now()))


def replay(tenant, purpose: str, prompt: str) -> dict | None:
    with tenant.db() as c:
        r = c.execute("SELECT * FROM ai_replays WHERE key=?", (_key(purpose, prompt),)).fetchone()
    if r is None:
        return None
    when = time.strftime("%Y-%m-%d", time.localtime(r["created"]))
    return {"text": r["output"], "mode": "replay", "run_id": r["run_id"], "date": when,
            "label": f"Replayed AI answer (run #{r['run_id'] or '?'}, {when})"}


def _charge(tenant, key: str, tokens: int) -> None:
    ukey = key.replace("_budget", "_used")
    with _lock:
        tenant.set(ukey, used(tenant, key) + tokens)


def _request(system: str, prompt: str, max_tokens: int, timeout: float) -> tuple[str, int]:
    global REQUESTS_SENT
    cfg = config.get()
    key = config.llm_key()
    if cfg.llm_provider == "openai":
        # Any OpenAI-compatible endpoint (OpenRouter, Gemini, a local server) via OPERATOR_LLM_BASE_URL.
        base = (cfg.llm_base_url or "https://api.openai.com/v1").rstrip("/")
        url = f"{base}/chat/completions"
        headers = {"authorization": f"Bearer {key}", "content-type": "application/json"}
        # OpenAI's own API wants max_completion_tokens (newer models reject max_tokens); compatible ones max_tokens
        cap = "max_completion_tokens" if "api.openai.com" in base else "max_tokens"
        body = {"model": cfg.llm_model, cap: max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
    else:
        url = "https://api.anthropic.com/v1/messages"
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        body = {"model": cfg.llm_model, "max_tokens": max_tokens, "system": system,
                "messages": [{"role": "user", "content": prompt}]}
    last = None
    for attempt in range(3):
        try:
            with httpx.Client(transport=_transport, timeout=timeout) as client:
                REQUESTS_SENT += 1
                r = client.post(url, headers=headers, json=body)
            if r.status_code in (429, 500, 502, 503, 504, 529):
                last = f"HTTP {r.status_code}"
                time.sleep(min(4.0, 0.5 * 2 ** attempt))
                continue
            if r.status_code >= 400:
                raise AIUnavailable(f"model error HTTP {r.status_code}")
            data = r.json()
            if cfg.llm_provider == "openai":
                text = data["choices"][0]["message"]["content"]
                toks = int(data.get("usage", {}).get("total_tokens", 0))
            else:
                text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
                u = data.get("usage", {})
                toks = int(u.get("input_tokens", 0)) + int(u.get("output_tokens", 0))
            return text, toks or (len(prompt) + len(text)) // 4
        except httpx.TimeoutException:
            last = "timeout"
        except httpx.HTTPError as e:
            last = redact(type(e).__name__)
    raise AIUnavailable(f"AI call failed ({last})")


def complete(tenant, system: str, prompt: str, purpose: str, max_tokens: int = 800,
             budget_key: str = "ai_token_budget", timeout: float = 25.0, run_id: int | None = None) -> dict:
    """Live model call. Raises AIUnavailable (never sends) when the gate is closed."""
    reason = unavailable_reason(tenant, budget_key)
    if reason:
        raise AIUnavailable(reason)
    try:
        text, toks = _request(system, prompt, max_tokens, timeout)
    except AIUnavailable:
        raise
    except Exception as e:  # noqa: BLE001
        raise AIUnavailable(redact(f"AI call failed ({type(e).__name__})"))
    _charge(tenant, budget_key, toks)
    save_replay(tenant, purpose, prompt, text, run_id)
    return {"text": text, "tokens": toks, "mode": "live", "label": "AI"}


def complete_or_replay(tenant, system: str, prompt: str, purpose: str, **kw) -> dict:
    try:
        return complete(tenant, system, prompt, purpose, **kw)
    except AIUnavailable as e:
        rp = replay(tenant, purpose, prompt)
        if rp:
            rp["reason"] = e.reason
            return rp
        raise


def parse_json(text: str):
    """Pull the first JSON object out of a model reply."""
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        s = s[s.find("\n") + 1:] if "\n" in s else s
    start = s.find("{")
    end = s.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object")
    return json.loads(s[start:end + 1])
