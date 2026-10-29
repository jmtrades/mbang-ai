"""One chat that does the work. Paste a question, your numbers, a script / ad / DM, a screenshot
or an Ads Manager export — the router sends it to the right agent and answers in the thread:

  question          -> Student Coach (cited, honest, follow-ups read in context)
  numbers           -> Diagnostic (bottleneck, money impact, cited benchmarks, next best action)
  script / ad / DM  -> Work Reviewer (score, line fixes, SOP citations)
  Ads export (CSV)  -> Ads audit (kill / winner rules on real data, read-only)
  a goal            -> a mission draft ("get my first client", "book 20 calls", "launch a webinar"); the missing
                       answers are asked for one at a time, and "Start this mission" creates it
  screenshot        -> local OCR, then whichever of the above fits the text
"""
from __future__ import annotations

import csv
import math
import re

from .. import ads_import, diagnostic, guardrails, runtime, threads, webinar_launch
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
MAX_METRIC = 1e9  # no funnel has more DMs, leads or dollars than this; bigger is a typo or a probe


def _n(v: str, k: str | None) -> float:
    try:
        x = float(v.replace(",", "")[:40])
    except (ValueError, OverflowError):
        return float("nan")
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
    return {k: v for k, v in out.items() if math.isfinite(v) and 0 <= v <= MAX_METRIC}


def looks_like_ads_export(text: str) -> bool:
    """An Ads Manager export: two or more lines, and a header row of three or more short delimited cells that
    include an impressions column and a spend column. A sentence about impressions and spend is a question."""
    lines = [ln for ln in (text or "").strip().splitlines() if ln.strip()]
    if len(lines) < 2:
        return False
    delim = "\t" if "\t" in lines[0] else ","
    try:
        head = [c.strip().lower() for c in next(csv.reader([lines[0]], delimiter=delim)) if c.strip()]
        second = next(csv.reader([lines[1]], delimiter=delim))
    except (csv.Error, StopIteration):
        return False
    if len(head) < 3 or len(second) < 2 or any(len(c.split()) > 8 for c in head):
        return False
    has_imp = any("impression" in c for c in head)
    has_spend = any(re.search(r"spen[dt]|cost|amount", c) for c in head)
    return has_imp and has_spend and ads_import.looks_like_ads_export(text)


_QUESTION = re.compile(r"\?|^\s*(how|what|why|when|where|which|who|should|could|can|is|are|do|does|did|any)\b", re.I)
_RATE_PCT = re.compile(r"\d+(?:\.\d+)?\s?%")


# "how do I improve my close rate?", "what should I fix?": a real question asked about the numbers, not a bare
# "here are my numbers?"
_ASKS = re.compile(r"\b(how|what|why|which|where|should|can|could|would|is it|any tips|advice)\b[^?]{0,200}\?", re.I)


def asks_about_numbers(text: str) -> bool:
    return bool(_ASKS.search(text or ""))


def is_metrics(text: str, metrics: dict) -> bool:
    """Numbers are a diagnosis request when there are three or more of them, or two with no question asked (a
    measured rate like "4%" counts as data). Numbers inside a real question ("I booked 3 calls and 2 showed, how do I
    improve my close rate?") are context for the question: it is answered by the coach, with the diagnosis of those
    numbers alongside, never replaced by the bare diagnostic."""
    if asks_about_numbers(text) and not _RATE_PCT.search(text):  # a measured rate ("4% on 500 DMs") is data
        return False
    if len(metrics) >= 3:
        return True
    if len(metrics) == 2:
        return not _QUESTION.search(text) or bool(_RATE_PCT.search(text))
    return False


_DESIRE = re.compile(r"\b(i want|i'd like|i would like|i need|help me|get me|let'?s|my goal|goal is|i'?m trying|"
                     r"i am trying|start a mission|set up a mission|make a plan|plan (for|to)|can you|could you|please)\b",
                     re.I)
_GOAL_START = re.compile(r"^\s*(please\s+)?(get|win|land|sign|book|fill|launch|run|scale|fix|onboard|start|grow|find|"
                         r"plan|send)\b", re.I)
_INTERROGATIVE = re.compile(r"^\s*(how|what|why|when|where|which|who|whom|should|is|are|do|does|did|was|were|will|would)\b",
                            re.I)


def looks_like_question(text: str) -> bool:
    return bool(_QUESTION.search(text or ""))


_CANCEL = re.compile(r"^\s*(cancel|stop|never ?mind|forget it|no thanks|not now)\W*$", re.I)


def is_goal(text: str) -> bool:
    """A goal the missions engine can plan: it matches a mission template and is phrased as a goal ("get my first
    client", "I want to book 20 calls"), not a how-to question ("How do I get my first client?") or an asset."""
    from ..missions import templates as mtemplates

    t = (text or "").strip()
    if not t or len(t) > 400 or len(t.splitlines()) > 2 or ASSET_MARK.search(t):
        return False
    if REVIEW_ASK.search(t) and (len(t.split()) >= 12 or "\n" in t):
        return False
    if mtemplates.match(t) is None:
        return False
    if _INTERROGATIVE.match(t) and not _DESIRE.search(t):
        return False
    return bool(_DESIRE.search(t) or _GOAL_START.match(t))


def classify(text: str, filename: str | None = None) -> str:
    if looks_like_ads_export(text):
        return "ads_csv"
    if webinar_launch.wants_launch(text):
        return "launch"
    if is_goal(text):
        return "goal"
    words = len(text.split())
    metrics = parse_metrics(text)
    if is_metrics(text, metrics):
        return "metrics"
    # a review ask with something pasted after it; "how do I improve my close rate?" is a question, not an asset
    if REVIEW_ASK.search(text) and ("\n" in text.strip() or (words >= 12 and (":" in text or not text.rstrip().endswith("?")))):
        return "asset"
    if ASSET_MARK.search(text) and words >= 6:
        return "asset"
    return "question"


def _numbers_context(nums: dict) -> tuple[str, dict]:
    """What the numbers in a question say, from the same pure maths as the diagnostic (no run, nothing stored)."""
    try:
        an = diagnostic.analyse(nums)
    except Exception:  # noqa: BLE001 - context is a bonus; the coach answer stands on its own
        return "", {}
    rows = [r for r in an.get("stages", an.get("rows", [])) or [] if isinstance(r, dict) and r.get("kind") != "cost"]
    if not rows:
        return "", {}
    bits = []
    for r in rows[:3]:
        v, b = r.get("value"), r.get("benchmark")
        if isinstance(v, (int, float)) and isinstance(b, (int, float)):
            bits.append(f"{r['label'].lower()} {v:.0%} (benchmark {b:.0%})")
    if not bits:
        return "", {}
    bn = an.get("bottleneck") or {}
    line = "From your numbers: " + "; ".join(bits) + "."
    if bn.get("label"):
        line += f" The weakest step is {bn['label'].lower()}."
    return line, {"rates": bits, "bottleneck": bn.get("label")}


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
    draft = None
    if thread_id:
        t = threads.get(tenant, thread_id, user["id"])
        if t:
            prev_user = [m for m in t["messages"] if m["role"] == "user" and (m.get("meta") or {}).get("intent", "question") == "question"]
            previous = prev_user[-1]["content"] if prev_user else None
            last = next((m for m in reversed(t["messages"]) if m["role"] == "assistant"), None)
            lm = (last or {}).get("meta") or {}
            if intent == "question" and not filename and lm.get("intent") == "goal" and \
                    (lm.get("mission_draft") or {}).get("missing") and not looks_like_question(text):
                # the answer to the question the mission intake asked; a new question still goes to the coach
                intent, draft = "goal", lm["mission_draft"]
    shown = text if not filename else (f"[{filename}] " + (text[:200] + ("…" if len(text) > 200 else "")))
    if intent == "question":
        res = coach.answer(tenant, text, user=user, record=True, thread=False, context=previous)
        # course citations [S#] and external 2026 Playbooks entries [X#] stay in separate lists
        reply, meta = res["answer"], {"citations": res["citations"], "external": res.get("external", []),
                                      "covered": res["covered"], "run_id": res["run_id"],
                                      "label": res.get("label"), "mode": res.get("mode"), "note": res.get("note"),
                                      "not_used": res.get("not_used", []), "followup": bool(res["parts"] and previous
                                                                                            and res["parts"][0]["part"].startswith(previous.rstrip("?. ")))}
        nums = parse_metrics(text) if not filename else {}
        if len(nums) >= 2:  # numbers inside a question: answer it, with what those numbers say, and offer the diagnosis
            ctx_line, ctx = _numbers_context(nums)
            if ctx_line:
                reply = reply.rstrip() + "\n\n" + ctx_line
                meta["diagnostic_context"] = ctx
            reply = (reply.rstrip() + "\n\nWant a full diagnosis? Send your numbers on their own (for example "
                     "'800 DMs, 40 replies, 6 booked, 2 showed') and I'll find the bottleneck.")
            meta["offer_diagnostic"] = True
        payload = res
    elif intent == "metrics":
        metrics = parse_metrics(text)
        res = diagnostic.diagnose(tenant, metrics, user=user, label="from chat")
        bn = res["bottleneck"]
        reply = res["prescription"]  # with no bottleneck it says why (low volume, no data or all at benchmark)
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
    elif intent == "launch":
        reply, meta = webinar_launch.from_chat(tenant, text, user)
        payload = {"launch": meta.get("launch")}
    elif intent == "goal":
        reply, meta = goal_reply(tenant, text, user, draft)
        payload = {"mission_draft": meta["mission_draft"]}
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
    meta["remembered"] = __import__("operator_ai.memory", fromlist=["hint"]).hint(tenant, user, intent)  # [memory] not stored
    return {"intent": intent, "thread_id": tid, "answer": reply, "meta": meta, "payload": payload}


# --------------------------------------------------------------------------- goal -> mission draft
def _ask(q) -> str:
    hint = q.placeholder or ""
    if q.kind == "money":
        hint = "for example $2,000"
    elif q.kind == "date":
        hint = "for example 2026-11-20"
    elif q.options:
        hint = ", ".join(q.options)
    if q.label.endswith("?"):
        return f"{q.label[:-1]}" + (f" ({hint})?" if hint else "?")
    return f"{q.label}" + (f", {hint}?" if hint else "?")


def _missing(tpl, answers: dict) -> list:
    from ..missions import templates as mtemplates

    if tpl.key == "fix_bottleneck":
        have = [q for q in mtemplates.NUMBERS if q.key != "offer_price" and isinstance(answers.get(q.key), (int, float))]
        if len(have) >= 2:
            return []
        return [q for q in mtemplates.NUMBERS if q.key != "offer_price" and q.key not in answers][:2 - len(have)]
    return mtemplates.missing(tpl, answers)


def goal_reply(tenant, text: str, user: dict, draft: dict | None = None) -> tuple[str, dict]:
    """Turn a goal into a mission draft, asking for the missing answers one at a time. Nothing is created until the
    person presses "Start this mission". Rules only; AI is never needed."""
    from ..missions import templates as mtemplates

    actor = f"{user['username']}({user['role']})"

    def fn(run: runtime.Run):
        d = dict(draft) if draft else None
        problem, first = "", d is None
        if d is None:
            key, matched = mtemplates.classify(text, user["role"])
            answers = mtemplates.extract_clean(text)
            for k, v in parse_metrics(text).items():
                if k in mtemplates.ALL_Q:
                    try:
                        val = mtemplates.clean_one(mtemplates.ALL_Q[k], v)
                    except mtemplates.AnswerError:
                        continue
                    if val is not None:
                        answers.setdefault(k, val)
            d = {"template": key, "goal": text[:300], "answers": answers, "matched": matched}
        elif _CANCEL.match(text):
            run.note("draft", {"template": d.get("template")}, {"dropped": True}, label="Mission draft")
            return {"reply": "OK, I've dropped that mission. Ask me anything, or tell me a goal when you're ready.",
                    "draft": None}
        else:
            d["answers"] = dict(d.get("answers") or {})
            tpl0 = mtemplates.get(d.get("template"))
            todo0 = _missing(tpl0, d["answers"]) if tpl0 else []
            if todo0:
                try:
                    val = mtemplates.clean_one(todo0[0], text)
                except mtemplates.AnswerError as e:
                    val, problem = None, str(e)
                if val is not None:
                    d["answers"][todo0[0].key] = val
        tpl = mtemplates.get(d["template"]) or mtemplates.get("first_client")
        d["template"] = tpl.key
        todo = _missing(tpl, d["answers"])
        d.update(missing=[q.key for q in todo], label=tpl.label, steps=[s["title"] for s in tpl.steps],
                 questions=[q.public() for q in tpl.questions])
        intro = ""
        if first:
            intro = (f"I can run this as a mission: {tpl.label}. It has {len(tpl.steps)} steps and is done when "
                     f"{tpl.outcome[:1].lower() + tpl.outcome[1:]}. Nothing goes out without your approval. ")
        if problem:
            reply = f"{problem}. {_ask(todo[0])}" if todo else problem
        elif todo:
            reply = intro + ("To plan it I need a few answers. " if first else "Thanks. ") + _ask(todo[0])
        else:
            reply = intro + ("" if first else "Thanks, I have what I need. ") + \
                "Press 'Start this mission' and I'll take the first steps; every draft waits for your approval."
        run.note("draft", {"template": d["template"]}, {"missing": d["missing"]}, label="Mission draft")
        return {"reply": reply, "draft": d}

    out = runtime.execute(tenant, "missions", "Goal from chat", ["Mission draft"], fn, actor)
    res = out["result"]
    return res["reply"], {"mission_draft": res["draft"], "run_id": out["run_id"], "label": "Rules plan (works with AI off)",
                          "mode": "rules", "citations": []}
