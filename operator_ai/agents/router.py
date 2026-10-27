"""One chat that does the work. Paste a question, your numbers, a script / ad / DM, a screenshot
or an Ads Manager export — the router sends it to the right agent and answers in the thread:

  question          -> Student Coach (cited, honest, follow-ups read in context)
  numbers           -> Diagnostic (bottleneck, money impact, cited benchmarks, next best action)
  script / ad / DM  -> Work Reviewer (score, line fixes, SOP citations)
  Ads export (CSV)  -> Ads audit (kill / winner rules on real data, read-only)
  screenshot        -> local OCR, then whichever of the above fits the text
"""
from __future__ import annotations

import re

from .. import ads_import, diagnostic, guardrails, threads
from . import coach, reviewer

REVIEW_ASK = re.compile(r"\b(review|feedback|critique|roast|rate (this|my)|thoughts on|fix (this|my)|improve (this|my)|"
                        r"what'?s wrong with|grade|score this|check (this|my))\b", re.I)
ASSET_MARK = re.compile(r"^\s*(subject|hook|headline|cta|primary text|segment \d|rep|prospect|dm|caption|script)\s*[:\-]",
                        re.I | re.M)

# metric words -> diagnostic keys (counts)
METRIC_WORDS = [
    (r"dms?( sent)?|messages sent|outreach", "dms_sent"), (r"repl(y|ies)", "replies"),
    (r"(quality )?conversations?|convos?", "conversations"), (r"links? sent|booking links?", "links_sent"),
    (r"(calls? )?booked|bookings?", "booked"), (r"show(ed|s)?|attended calls", "showed"),
    (r"closed|clos(es|ings)|sales|deals", "closed"), (r"(ad )?spend|spent", "ad_spend"), (r"leads", "leads"),
    (r"visitors|page views|landing visits", "visitors"), (r"registrants|registrations|regs", "registrants"),
    (r"attendees|attended", "attendees"), (r"(offer )?price|ticket", "offer_price"),
]
RATE_WORDS = [
    (r"reply rate", ("replies", "dms_sent")), (r"show(-| )?(up )?rate", ("showed", "booked")),
    (r"close rate|closing rate", ("closed", "showed")), (r"book(ing)? rate", ("booked", "links_sent")),
]
NUM = r"\$?\s?(?P<n>\d[\d,]*(?:\.\d+)?)\s?(?P<k>k)?"


def _n(v: str, k: str | None) -> float:
    x = float(v.replace(",", ""))
    return x * 1000 if k else x


def parse_metrics(text: str) -> dict[str, float]:
    """Pull pipeline numbers out of free text: "800 DMs, 40 replies, 6 booked, 2 showed, price $3k",
    "spent $1,200 for 40 leads", "show rate 45% from 20 booked"."""
    low = text.lower()
    out: dict[str, float] = {}
    for words, key in METRIC_WORDS:
        m = re.search(rf"{NUM}\s*(?:{words})\b", low) or re.search(rf"\b(?:{words})\s*(?:of|:|=|is|was|were|at)?\s*{NUM}(?!\s*%)", low)
        if m:
            out.setdefault(key, _n(m.group("n"), m.group("k")))
    for words, (num, den) in RATE_WORDS:
        m = re.search(rf"(?:{words})\s*(?:of|:|=|is|was|at)?\s*(?P<r>\d+(?:\.\d+)?)\s?%", low) or \
            re.search(rf"(?P<r>\d+(?:\.\d+)?)\s?%\s*(?:{words})", low)
        if m:
            rate = float(m.group("r")) / 100
            if den not in out:
                out[den] = 100.0  # a rate with no base: model it on 100
            out.setdefault(num, round(out[den] * rate))
    cpl = re.search(rf"(?:cpl|cost per lead)\s*(?:of|:|=|is|was|at)?\s*{NUM}", low)
    if cpl and "ad_spend" not in out:
        out["leads"] = out.get("leads", 100.0)
        out["ad_spend"] = _n(cpl.group("n"), cpl.group("k")) * out["leads"]
    return out


def classify(text: str, filename: str | None = None) -> str:
    if filename and filename.lower().endswith((".csv", ".tsv")) and ads_import.looks_like_ads_export(text):
        return "ads_csv"
    if ads_import.looks_like_ads_export(text):
        return "ads_csv"
    words = len(text.split())
    metrics = parse_metrics(text)
    if len(metrics) >= 2:
        return "metrics"
    if REVIEW_ASK.search(text) and (words >= 12 or "\n" in text.strip()):
        return "asset"
    if ASSET_MARK.search(text) and words >= 6:
        return "asset"
    return "question"


def _strip_instruction(text: str) -> str:
    lines = text.strip().splitlines()
    if len(lines) > 1 and REVIEW_ASK.search(lines[0]) and len(lines[0].split()) <= 14:
        return "\n".join(lines[1:]).strip()
    m = re.match(r"^(.{0,80}?\b(?:review|feedback on|critique|roast|thoughts on|check)\b[^:\n]{0,40}:)\s*(.+)$", text.strip(), re.I | re.S)
    return m.group(2).strip() if m else text.strip()


def handle(tenant, text: str, user: dict, thread_id: int | None = None, filename: str | None = None,
           extracted: dict | None = None) -> dict:
    """Route one chat message. Returns {intent, thread_id, answer, ...agent payload}."""
    text = (text or "").strip()
    intent = classify(text, filename)
    previous = None
    if thread_id:
        t = threads.get(tenant, thread_id, user["id"])
        if t:
            prev_user = [m for m in t["messages"] if m["role"] == "user" and (m.get("meta") or {}).get("intent", "question") == "question"]
            previous = prev_user[-1]["content"] if prev_user else None
    shown = text if not filename else (f"[{filename}] " + (text[:200] + ("…" if len(text) > 200 else "")))
    if intent == "question":
        res = coach.answer(tenant, text, user=user, record=True, thread=False, context=previous)
        # course citations [S#] and external 2026 Playbooks entries [X#] stay in separate lists
        reply, meta = res["answer"], {"citations": res["citations"], "external": res.get("external", []),
                                      "covered": res["covered"], "run_id": res["run_id"],
                                      "label": res.get("label"), "mode": res.get("mode"), "note": res.get("note"),
                                      "not_used": res.get("not_used", []), "followup": bool(res["parts"] and previous
                                                                                            and res["parts"][0]["part"].startswith(previous.rstrip("?. ")))}
        payload = res
    elif intent == "metrics":
        metrics = parse_metrics(text)
        res = diagnostic.diagnose(tenant, metrics, user=user, label="from chat")
        bn = res["bottleneck"]
        reply = res["prescription"] if bn else "Every stage with enough data is at or above the SOP benchmark."
        meta = {"citations": res["citations"], "run_id": res["run_id"], "metrics": metrics,
                "next_best_action": res.get("next_best_action"), "bottleneck": bn, "mode": res.get("mode")}
        payload = res
    elif intent == "asset":
        asset = _strip_instruction(text)
        res = reviewer.review(tenant, asset, user=user)
        top = [f for f in res["fixes"] if f.get("severity") == "block"][:2] + [f for f in res["fixes"] if f.get("severity") != "block"][:3]
        reply = (f"{res['asset_type'].replace('_', ' ').title()} scored {res['score']}/100. "
                 + (" ".join(f"Fix: {f['fix']}" for f in top[:3]) if top else "No fixes needed."))
        meta = {"citations": res["citations"], "run_id": res["run_id"], "review": {k: res[k] for k in ("asset_type", "score", "passes",
                                                                                                      "fixes", "flags", "label")}}
        payload = res
    else:  # ads_csv
        try:
            res = ads_import.audit(tenant, text)
        except ads_import.ImportError_ as e:
            res = {"error": str(e)}
        reply = res.get("summary") or res.get("error")
        meta = {"citations": res.get("citations", []), "ads_audit": res}
        payload = res
    names = guardrails.tenant_banned_names(tenant)
    reply = guardrails.scrub(reply, names)
    meta["intent"] = intent
    if extracted:
        meta["receipt"] = extracted
    tid = thread_id or threads.create(tenant, user["id"], shown if intent == "question" else f"{intent}: {shown}")
    threads.add_message(tenant, tid, "user", shown, {"intent": intent, "filename": filename})
    threads.add_message(tenant, tid, "assistant", reply, meta)
    return {"intent": intent, "thread_id": tid, "answer": reply, "meta": meta, "payload": payload}
