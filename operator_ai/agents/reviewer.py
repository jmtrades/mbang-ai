"""Work Reviewer: ads, funnels, VSLs, Looms, DM scripts, sales calls, bios, content posts.

Detect asset type -> load rubric (each rule tied to an SOP) -> deterministic checks (hook
length, CTA count, AI-sounding words, guardrails) -> retrieve the SOP passage behind each
rule -> (AI: score each criterion 0-10 with weak quote, exact fix, [E#]) -> weighted score
/100 plus line-level fixes. Heuristic-only when the AI is off, and it says so.
"""
from __future__ import annotations

import re

from .. import critique, guardrails, llm, rubrics, runtime

PLAN = ["Detect asset type", "Load rubric", "Deterministic checks", "Retrieve SOP evidence", "Score criteria",
        "Line-level fixes"]
TYPES = sorted(set(rubrics.RUBRICS) - {"generic"})


def detect_type(text: str) -> str:
    t = text.strip()
    low = t.lower()
    if re.search(r"^\s*subject\s*:", t, re.I | re.M):
        return "email"
    if re.search(r"\bsegment\s*\d", low):
        return "webinar"
    if re.search(r"^\s*(rep|closer|setter|prospect|lead)\s*:", t, re.I | re.M):
        return "sales_call"
    if "vsl" in low or "video sales letter" in low:
        return "vsl"
    if "loom" in low:
        return "loom"
    if re.search(r"\b(headline|sign up|register now|landing page|opt[- ]in)\b", low):
        return "funnel"
    if re.search(r"\b(hook|primary text|ad copy)\s*:", low) or re.search(r"\b(click here|learn more|shop now)\b", low):
        return "ad"
    if "|" in t and len(t.split()) <= 30:
        return "profile_bio"
    if t.count("#") >= 2:
        return "content_post"
    if re.search(r"don'?t forget|reminder|your call (tomorrow|today|in)", low):
        return "reminder"
    if len(t.split()) <= 70 and ("?" in t or low.startswith(("hey", "hi", "hello"))):
        return "dm_script"
    return "script"


AI_SYSTEM = ("You review marketing and sales assets against a programme rubric. For each criterion give a score 0-10, "
             "quote the weakest line exactly, give an exact fix, and cite evidence ids [E#] from the list. "
             "Reply as JSON: {\"criteria\": [{\"key\": \"...\", \"score\": 0, \"quote\": \"...\", \"fix\": \"...\", "
             "\"citations\": [\"E1\"]}]}")


def _line_fixes(text: str, crit: list[dict], flags: list[dict]) -> list[dict]:
    lines = text.splitlines() or [text]
    fixes = []

    def locate(quote: str) -> tuple[int, str]:
        q = (quote or "").strip().lower()
        if not q:
            return 0, ""
        first = q.split(",")[0].strip()[:40]
        for i, ln in enumerate(lines, start=1):
            if first and first in ln.lower():
                return i, ln.strip()
        return 0, ""

    for c in crit:
        if c["score"] >= 10 or not c["fix"] or c["key"] == "guardrails":
            continue  # guardrail problems are listed one by one from the flags below
        n, ln = locate(c["quote"])
        fixes.append({"line_no": n, "line": ln or c["quote"], "problem": c["label"], "fix": c["fix"],
                      "citations": c.get("citations", []), "score": c["score"]})
    for f in flags:
        n, ln = locate(f["match"])
        fixes.append({"line_no": n, "line": ln or f["match"], "problem": f["message"],
                      "fix": "Remove or rewrite this line so it is true and verifiable.", "citations": [],
                      "severity": f["severity"], "guardrail": f["code"]})
    fixes.sort(key=lambda x: (x["line_no"] or 9999))
    return fixes


def review(tenant, text: str, asset_type: str | None = None, user: dict | None = None, meta: dict | None = None,
           run: runtime.Run | None = None) -> dict:
    actor = f"{user['username']}({user['role']})" if user else "system"
    own = run is None
    if own:
        run = runtime.Run(tenant, "reviewer", f"Review: {text[:60]}", PLAN, actor)
    try:
        at = asset_type if asset_type in rubrics.RUBRICS else run.step(
            "detect_type", {"chars": len(text)}, lambda: detect_type(text), "Detect asset type")
        if asset_type in rubrics.RUBRICS:
            run.note("detect_type", {"given": asset_type}, at, label="Detect asset type")
        rules = run.step("load_rubric", {"asset_type": at},
                         lambda: [{"key": r.key, "label": r.label, "weight": r.weight, "sop": r.sop} for r in rubrics.get(at)],
                         "Load rubric")
        res = run.step("checks", {"asset_type": at}, lambda: critique.critique(tenant, text, at, meta),
                       "Deterministic checks")
        run.note("evidence", {"rules": len(rules)}, {"citations": [c["id"] + " " + c["file"] for c in res["citations"]]},
                 label="Retrieve SOP evidence")
        mode, label = "rules", "Heuristic scoring (AI off)"
        reason = llm.unavailable_reason(tenant)
        if reason is None:
            ev = "\n".join(f"[{c['id']}] {c['snippet']}" for c in res["citations"])
            crit = "\n".join(f"- {c['key']}: {c['label']}" for c in res["criteria"])
            prompt = f"Asset type: {at}\nCriteria:\n{crit}\nEvidence:\n{ev}\n\nASSET:\n{text}"

            def _ai():
                out = llm.complete(tenant, AI_SYSTEM, prompt, purpose=f"review:{at}", run_id=run.id, max_tokens=900)
                return llm.parse_json(out["text"])

            data = run.step("score_ai", {"criteria": len(res["criteria"])}, _ai, "Score criteria", allow_fail=True)
            if data and isinstance(data.get("criteria"), list):
                allowed = {c["id"] for c in res["citations"]}
                by = {c["key"]: c for c in res["criteria"]}
                for item in data["criteria"]:
                    c = by.get(item.get("key"))
                    if not c or c["key"] == "guardrails":
                        continue  # guardrail scores stay deterministic
                    try:
                        c["score"] = max(0.0, min(10.0, float(item.get("score", c["score"]))))
                    except (TypeError, ValueError):
                        continue
                    c["quote"] = str(item.get("quote") or c["quote"])[:300]
                    fix = str(item.get("fix") or c["fix"])[:400]
                    if guardrails.blocked(guardrails.check_for(tenant, fix, rubrics.meta_for(at, meta))):
                        fix = c["fix"]  # never suggest a fix that breaks the rules
                    c["fix"] = fix
                    c["citations"] = [x for x in item.get("citations", []) if x in allowed] or c["citations"]
                tw = sum(c["weight"] for c in res["criteria"])
                res["score"] = round(sum(c["score"] * c["weight"] for c in res["criteria"]) / tw * 10)
                mode, label = "live", "AI-scored against the rubric"
                run.use_ai("live")
        else:
            run.note("score", {"mode": "heuristic"}, {"score": res["score"], "reason": reason}, label="Score criteria")
            label = f"Heuristic scoring ({reason})"
        fixes = run.step("line_fixes", {}, lambda: _line_fixes(text, res["criteria"], res["flags"]), "Line-level fixes")
        out = {"asset_type": at, "score": res["score"], "passes": res["passes"], "criteria": res["criteria"],
               "citations": res["citations"], "flags": res["flags"], "detected": res["detected"], "fixes": fixes,
               "mode": mode, "label": label, "run_id": run.id}
        if own:
            run.finish(out)
        return out
    except Exception as e:
        if own:
            run.fail(e)
        raise
