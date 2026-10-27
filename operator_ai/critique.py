"""Self-critique: score a draft against the programme rubric. With a critique budget, rewrite
until it passes (at most 2 rewrites within critique_token_budget); otherwise mark needs_human."""
from __future__ import annotations

from . import guardrails, llm, retrieval, rubrics

PASS_SCORE = 70
MAX_REWRITES = 2


def evidence(tenant, criteria: list[dict], only_failing: bool = True, prefix: str = "E") -> tuple[list[dict], dict]:
    """Retrieve the SOP passage behind each rule. Returns (citations, criterion_key -> [E#])."""
    cites: list[dict] = []
    by_key: dict[str, list[str]] = {}
    seen: dict[tuple, str] = {}
    for c in criteria:
        if only_failing and c["score"] >= 10:
            continue
        hits = retrieval.search(tenant, c["cite"], k=5)
        hit = next((h for h in hits if h["file"] == c["sop"]), hits[0] if hits else None)
        if not hit:
            continue
        key = (hit["file"], hit["lines"])
        if key not in seen:
            cid = f"{prefix}{len(cites) + 1}"
            seen[key] = cid
            cites.append({**retrieval.cite(hit), "id": cid})
        by_key.setdefault(c["key"], []).append(seen[key])
    return cites, by_key


def critique(tenant, text: str, asset_type: str, meta: dict | None = None) -> dict:
    res = rubrics.score(text, asset_type, meta, guardrails.tenant_banned_names(tenant), tenant=tenant)
    cites, by_key = evidence(tenant, res["criteria"])
    for c in res["criteria"]:
        c["citations"] = by_key.get(c["key"], [])
    res["citations"] = cites
    res["passes"] = res["score"] >= PASS_SCORE and not guardrails.blocked(res["flags"]) and not res["critical"]
    return res


def _rewrite_prompt(text: str, res: dict, brief: str) -> str:
    fixes = "\n".join(f"- {c['label']}: {c['fix']}" for c in res["criteria"] if c["fix"])
    flags = "\n".join(f"- {f['message']}" for f in res["flags"])
    return (f"Rewrite this draft so it passes the programme rubric. Keep it short and plain.\n"
            f"Brief: {brief}\nFixes needed:\n{fixes}\nGuardrail problems:\n{flags or '- none'}\n"
            f"Rules: no guarantees, no income claims, no invented proof, no fake scarcity, one call to action, "
            f"in prospect DMs no price and the only figure allowed is a hedged \"$50k-$100k a month\".\n"
            f"Return only the rewritten text.\n\nDRAFT:\n{text}")


def loop(tenant, draft: str, asset_type: str, meta: dict | None = None, brief: str = "", run=None) -> dict:
    rounds = []
    res = critique(tenant, draft, asset_type, meta)
    rounds.append({"text": draft, "score": res["score"], "passes": res["passes"], "by": "draft"})
    text = draft
    mode = "rules"
    reason = None
    while not res["passes"] and len(rounds) <= MAX_REWRITES:
        why = llm.unavailable_reason(tenant, "critique_token_budget")
        if why:
            reason = why
            break
        try:
            out = llm.complete(tenant, "You rewrite marketing drafts to pass a strict, honest rubric.",
                               _rewrite_prompt(text, res, brief), purpose=f"critique:{asset_type}",
                               budget_key="critique_token_budget", max_tokens=600)
        except llm.AIUnavailable as e:
            reason = e.reason
            break
        mode = "live"
        if run is not None:
            run.use_ai("live")
        text = out["text"].strip()
        res = critique(tenant, text, asset_type, meta)
        rounds.append({"text": text, "score": res["score"], "passes": res["passes"], "by": "ai_rewrite"})
    return {"final": text, "score": res["score"], "passes": res["passes"], "needs_human": not res["passes"],
            "rounds": rounds, "critique": res, "mode": mode,
            "reason": reason if not res["passes"] else None}
