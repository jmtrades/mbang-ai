"""Coaching mode: a student pastes their own DM, follow-up, cold video pitch, bio, caption,
short script or reminder; line-level fixes with citations come back. Reuses the reviewer
and self-critique scoring; the outreach money rule applies to DMs, the client-asset rule to
the student's own assets. Every suggested line is guardrail-checked before it is shown."""
from __future__ import annotations

import re

from .. import guardrails, runtime
from . import reviewer

KINDS = {
    "dm": "dm_script", "followup": "followup", "cold_video_pitch": "cold_video_pitch", "bio": "profile_bio",
    "caption": "caption", "script": "script", "reminder": "reminder", "vsl": "vsl", "email": "email", "ad": "ad",
}
OUTREACH_KINDS = {"dm", "followup", "cold_video_pitch"}
CLIENT_ASSET_KINDS = {"vsl", "email", "ad"}


def suggest_line(line: str, problem: str, kind: str, brand: str) -> str | None:
    """Deterministic rewrite suggestions for the common problems."""
    low = line.lower()
    if "question" in problem.lower() and line.count("?") > 1:
        first = line.split("?")[0].strip() + "?"
        return first
    if "price" in problem.lower() or "$50k-$100k" in problem or "figure" in problem.lower() or "amount" in problem.lower():
        return guardrails.no_figure_line(brand, "our system")
    if re.search(r"guarantee|money back|risk[- ]free", low):
        return re.sub(r"(?i)\s*(and )?(we )?guarantee(d)?( results)?|or your money back|risk[- ]free", "", line).strip() or None
    if "don't forget" in low or "dont forget" in low or "value" in problem.lower():
        return "Looking forward to our call. One question to think about before we talk: what would 10 more booked calls a month change for you?"
    if "following up" in low or "re:" in low:
        return re.sub(r"(?i)^(subject:\s*)?(re:\s*)|just following up( on my last message)?[.!]?", r"\1", line).strip() or None
    if "ai-sounding" in problem.lower():
        for w in guardrails.AI_WORDS:
            line = re.sub(re.escape(w), "", line, flags=re.I)
        return re.sub(r"\s{2,}", " ", line).strip()
    return None


def coach_work(tenant, text: str, kind: str, user: dict | None = None, lead_tier: str | None = None) -> dict:
    at = KINDS.get(kind, "script")
    meta = {}
    if kind in OUTREACH_KINDS:
        meta = {"prospect_facing": True, "lead_tier": lead_tier or "cold"}
    elif kind in CLIENT_ASSET_KINDS:
        meta = {"client_asset": True}
    actor = f"{user['username']}({user['role']})" if user else "system"
    run = runtime.Run(tenant, "coaching", f"Coaching ({kind}): {text[:50]}", reviewer.PLAN + ["Suggest line fixes"], actor)
    try:
        res = reviewer.review(tenant, text, asset_type=at, user=user, meta=meta, run=run)
        brand = "[their brand]"  # the prospect's brand, filled in by the student

        def _suggest():
            out = []
            for f in res["fixes"]:
                s = suggest_line(f["line"], f["problem"], kind, brand)
                if s:
                    flags = guardrails.check_for(tenant, s, meta)
                    if guardrails.blocked(flags):
                        s = None  # a suggestion must itself pass the guardrails
                out.append({**f, "suggestion": s})
            return out

        fixes = run.step("suggest", {"fixes": len(res["fixes"])}, _suggest, "Suggest line fixes")
        res = {**res, "fixes": fixes, "kind": kind, "rule": guardrails.mode_for(meta)}
        run.finish(res)
        return res
    except Exception as e:
        run.fail(e)
        raise
