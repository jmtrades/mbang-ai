"""When the course doesn't cover a question and the AI is on: try harder before saying no.

1. Rewrite: the AI fixes spelling and gives a few plain phrasings ("convice my cleints to scale adsoend" ->
   "how to get clients to increase their ad budget"), and the course and the playbooks are searched again with them.
2. Method answer: if nothing still matches, answer from the programme's own method notes (below), labelled
   plainly as not from the course material, instead of a dead end.

Without the AI nothing here runs and the honest "not covered" answer stands.
"""
from __future__ import annotations

from .. import guardrails, llm, runtime

REWRITE_SYSTEM = ("Rewrite a student's question for a search engine. Fix the spelling and give 3 short, plain "
                  "alternative phrasings using the most common words for the topic. Reply as JSON: "
                  "{\"queries\": [\"...\", \"...\", \"...\"]}")

# The programme's method in our own words (from its masterclass and group-call lessons). Plain facts for the AI to
# answer from; never presented as quotes or course citations.
METHOD_NOTES = """
- Money math first: work back from the monthly goal to clients needed (goal / price), sales calls (/ close rate),
  booked calls (/ show rate) and the spend to get those calls (x cost per booked call). Use the client's real stats,
  kept conservative.
- Price is the biggest lever: at a higher price the same goal needs far fewer clients and calls, which makes the
  plan realistic.
- Getting a client to spend more on ads: show them their own financial model. Reverse-engineer their revenue goal to
  the booked calls and ad spend it takes, show what the current spend can and can't reach, and agree a plan with a
  clear target. Frame risk honestly: no promises, but the numbers show what it takes. Push them to risk more where
  the data supports it, without breaking the bank, and track every lead's source so you double down on what works.
  Under-funding a launch is a common reason results stall (spending a fraction of what the goal needs).
- On sales calls: no script needed; show the prospect their numbers, the plan and what it takes, and ask if they
  are ready. On a verbal yes, ask for a small refundable deposit.
- Rich (B2B) buyers have money but low urgency; build urgency with pre-sell assets and nurture.
- Pipeline videos: 10-25 minute videos about one prospect's bottleneck, sent to them directly as follow-up, with a
  pitch at the start and a free resource plus paid options in the description.
- Be everywhere: daily short clips with subtitles, a title that anchors a problem or outcome, and a comment-keyword
  lead magnet on screen and on the caption's first line; film quick on-the-spot clips when something happens.
- Follow-up: everyone who engages gets a conversation, goes into the CRM and keeps receiving useful videos and
  resources about their exact problem until they are ready. Follow up with value, never "any update?".
- Targeting: put the same effort into high-ticket prospects only; one big client outweighs many small ones.
- Fulfilment and results matter more than chasing volume; sell the outcome, set standards, ask clients for blunt
  feedback.
"""
METHOD_SYSTEM = (
    "You are the programme's coach. The course material doesn't cover this question, so answer from the programme's "
    "method notes below and general good practice consistent with them. Plain, friendly, simple words; explain any "
    "term. Give a direct answer, then 3-5 practical steps as bullets starting with '• '. No markdown, no asterisks. "
    "No income claims, guarantees, invented results, numbers or names. Never claim this is quoted from the course. "
    "Under 220 words.\n\nMethod notes:\n" + METHOD_NOTES)
LABEL = "Not in the course material yet: answered from the programme's method (AI)"
PREFIX = "This isn't covered in the course material yet, so here's the programme's approach:\n\n"


def rewrites(tenant, run: runtime.Run, question: str) -> list[str]:
    if llm.unavailable_reason(tenant) is not None:
        return []

    def _ai() -> list[str]:
        out = llm.complete(tenant, REWRITE_SYSTEM, question, purpose="coach-rewrite", run_id=run.id, max_tokens=300)
        qs = llm.parse_json(out["text"]).get("queries") or []
        return [q.strip() for q in qs if isinstance(q, str) and q.strip() and q.strip().lower() != question.lower()][:3]

    return run.step("rewrite_ai", {"question": question}, _ai, "Rewrite (AI): spelling and plain phrasings",
                    allow_fail=True) or []


def method_answer(tenant, run: runtime.Run, question: str) -> dict | None:
    if llm.unavailable_reason(tenant) is not None:
        return None

    def _ai() -> str:
        out = llm.complete(tenant, METHOD_SYSTEM, f"Student question: {question}", purpose="coach-method",
                           run_id=run.id, max_tokens=1500)
        return out["text"].strip().replace("**", "")

    text = run.step("method_answer", {"question": question}, _ai, "Answer from the programme's method (AI)",
                    allow_fail=True)
    if not text or guardrails.blocked(guardrails.check_for(tenant, text, {"source": "method"})):
        return None
    run.use_ai("live")
    return {"answer": guardrails.scrub(PREFIX + text, guardrails.tenant_banned_names(tenant)), "mode": "live",
            "label": LABEL}


def recover(tenant, run: runtime.Run, question: str, res: dict, retry) -> dict:
    """`res` is the honest not-covered result; `retry(q)` answers another phrasing the normal way. Returns the
    first rewrite that the course or the playbooks cover, else a method answer, else `res` unchanged."""
    if res.get("covered") != "no" or llm.unavailable_reason(tenant) is not None:
        return res
    for q in rewrites(tenant, run, question):
        alt = retry(q)
        if alt.get("covered") != "no" or alt.get("external_mode") or "\n\n" in (alt.get("answer") or ""):
            alt["note"] = f"Searched as: \"{q}\""
            return alt
    m = method_answer(tenant, run, question)
    if m is None:
        return res
    return {**res, "answer": m["answer"], "mode": m["mode"], "label": m["label"], "method": True}
