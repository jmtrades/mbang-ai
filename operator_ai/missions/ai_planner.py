"""Optional AI planning layer.

Given the goal, the answers and the step registry schema, the model may suggest a step list. The suggestion is
validated against the registry (unknown step types and parameters are rejected, titles are guardrail-checked,
outbound steps keep waiting for approval because the registry, not the model, decides how a step waits), diffed
against the current plan and stored as a plan change the user confirms. With AI off the rules template stays the
plan and nothing is sent to any AI provider.
"""
from __future__ import annotations

import json
import math
import re

from .. import guardrails, llm
from . import evaluator, registry, templates

SYSTEM = ("You plan multi-step missions for a coaching programme's growth operators. Reply with JSON only, shaped "
          '{"steps": [{"key": "short_key", "type": "<step type>", "title": "...", "depends": ["key"], "params": {}}]}. '
          "Use only the step types listed. Keep the keys of existing steps you keep. Everything sent to a prospect or "
          "client is drafted for a person to approve. No guarantees, no income claims, no invented results.")
MAX_STEPS = 25
_KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_HREF = re.compile(r"^#/[a-z][a-z0-9_\-/]{0,40}(\?[a-z0-9_=&\-]{0,60})?$")
NEVER_REMOVE = ("intake", "checkpoint", "cert_gate")  # the AI never drops the plan's answers, checks or training gates


def availability(tenant) -> dict:
    reason = llm.unavailable_reason(tenant)
    return {"enabled": reason is None,
            "label": "AI can suggest plan changes (you confirm them)" if reason is None else
            f"{reason}: the rules template is the plan"}


def _coerce(p: registry.Param, v, tenant=None, name: str = ""):
    if p.type == "bool":
        return v if isinstance(v, bool) else None
    if p.type in ("int", "float"):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            return None
        v = int(v) if p.type == "int" else float(v)
        if (p.min is not None and v < p.min) or (p.max is not None and v > p.max):
            return None
        return v
    if p.type == "enum":
        return v if isinstance(v, str) and v in p.choices else None
    if isinstance(v, str):
        v = " ".join(v.split())[:200]
        if name == "href":
            return v if _HREF.match(v) else None  # only links inside the app
        if tenant is not None and v and guardrails.blocked(guardrails.check_for(tenant, v, {})):
            return None  # AI text that fails the guardrails is never shown
        return v
    return None


def validate(tenant, steps, existing: set[str], role: str | None = None) -> tuple[list[dict], list[dict]]:
    """Keep only what the registry allows for the mission user's role. Returns (specs, rejected)."""
    specs, rejected, seen = [], [], set(existing)
    if not isinstance(steps, list):
        return [], [{"reason": "No step list in the AI reply"}]
    for i, s in enumerate(steps[:MAX_STEPS]):
        if not isinstance(s, dict):
            rejected.append({"index": i, "reason": "Not a step"})
            continue
        t = registry.get_type(str(s.get("type") or ""))
        if t is None or not t.planner:
            rejected.append({"index": i, "type": str(s.get("type"))[:40], "reason": "Not a step type this workspace has"})
            continue
        if role is not None and not t.allowed(role):
            rejected.append({"index": i, "type": t.key, "reason": "Not a step this person's role can run"})
            continue
        key = str(s.get("key") or "").strip().lower()
        if not _KEY.match(key):
            key = f"{t.key}_{i + 1}"
        params, dropped = {}, []
        for k, v in (s.get("params") or {}).items() if isinstance(s.get("params"), dict) else []:
            spec = t.params.get(k)
            val = _coerce(spec, v, tenant, str(k)) if spec else None
            if val is None:
                dropped.append(str(k)[:40])
            else:
                params[k] = val
        title = str(s.get("title") or "")[:80].strip()
        if not title or guardrails.blocked(guardrails.check_for(tenant, title, {})):
            title = t.label
        deps = [d for d in (s.get("depends") or []) if isinstance(d, str) and d in seen] if isinstance(s.get("depends"), list) else []
        seen.add(key)
        specs.append({"skey": key, "type": t.key, "title": title, "params": params, "depends": deps,
                      **({"dropped_params": dropped} if dropped else {})})
    if len(steps) > MAX_STEPS:
        rejected.append({"reason": f"Only the first {MAX_STEPS} steps are considered"})
    return specs, rejected


def diff(m: dict, specs: list[dict]) -> dict:
    cur = {s["skey"]: s for s in m.get("steps") or []}
    keys = {s["skey"] for s in specs}
    add = [s for s in specs if s["skey"] not in cur]
    remove = [k for k, s in cur.items() if k not in keys and s["status"] == "pending" and s["agent"] not in NEVER_REMOVE]
    return {"add": add, "remove": remove, "context": {}}


def _role_of(tenant, uid: int) -> str | None:
    with tenant.db() as c:
        r = c.execute("SELECT role FROM users WHERE id=?", (uid,)).fetchone()
    return r["role"] if r else None


def propose(tenant, m: dict, actor: str = "system") -> dict:
    tpl = templates.get(m["template"])
    role = _role_of(tenant, m["user_id"]) or "student"
    cur = [{"key": s["skey"], "type": s["agent"], "title": s["title"], "status": s["status"]} for s in m.get("steps") or []]
    schema = [t.schema() for t in registry.REGISTRY.values() if t.planner and t.allowed(role)]
    prompt = json.dumps({"goal": m["goal"], "template": tpl.label if tpl else m["template"],
                         "answers": (m.get("context") or {}).get("answers") or {}, "current_plan": cur,
                         "step_types": schema}, default=str)[:24000]
    try:
        r = llm.complete_or_replay(tenant, SYSTEM, prompt, "missions:plan", max_tokens=1500)
    except llm.AIUnavailable as e:
        return {"available": False, "reason": e.reason, "label": f"{e.reason}: the rules template stays the plan",
                "replan_id": None}
    try:
        data = llm.parse_json(r["text"])
    except (ValueError, TypeError):
        return {"available": True, "mode": r.get("mode"), "replan_id": None,
                "label": "The AI plan couldn't be read, so the rules template stays the plan"}
    specs, rejected = validate(tenant, data.get("steps") if isinstance(data, dict) else None,
                               {s["skey"] for s in m.get("steps") or []}, role)
    d = diff(m, specs)
    for a in d["add"]:
        t = registry.get_type(a["type"])
        a["outbound"] = bool(t and t.outbound)
        a["wait_for"] = t.wait_for if t else "none"  # from the registry: outbound steps always wait for approval
    if not d["add"] and not d["remove"]:
        return {"available": True, "mode": r.get("mode"), "replan_id": None, "rejected": rejected,
                "label": "The AI suggestion matches the current plan: nothing to change"}
    d["why"] = "Suggested by AI and checked against the step list" + (" (replayed)" if r.get("mode") == "replay" else "")
    d["rejected"] = rejected
    rid = evaluator.store_replan(tenant, m["id"], "ai", "ai_plan", d)
    return {"available": True, "mode": r.get("mode"), "replan_id": rid, "diff": d, "rejected": rejected,
            "label": "AI suggestion ready for review" if rid else "An AI suggestion is already waiting for review"}
