"""When the course doesn't cover a question and the AI is on: try harder before saying no.

1. Rewrite: the AI fixes spelling and gives a few plain phrasings ("convice my cleints to scale adsoend" ->
   "how to get clients to increase their ad budget"), and the course and the playbooks are searched again with them.
2. Method answer: if no lesson matches, answer in the programme's voice from its method notes
   (knowledge/programme_method.md), labelled as coming from the method rather than a specific lesson.

Without the AI nothing here runs and the honest "not covered" answer stands.
"""
from __future__ import annotations

from pathlib import Path

from .. import guardrails, llm, runtime

REWRITE_SYSTEM = ("Rewrite a student's question for a search engine. Fix the spelling and give 3 short, plain "
                  "alternative phrasings using the most common words for the topic. Reply as JSON: "
                  "{\"queries\": [\"...\", \"...\", \"...\"]}")

METHOD_FILE = Path(__file__).resolve().parent.parent / "knowledge" / "programme_method.md"


def method_notes() -> str:
    try:
        return METHOD_FILE.read_text(encoding="utf-8")
    except OSError:
        return ""


VOICE_EXAMPLE = """Student: My client doesn't want to scale up ad spend. How do I convince him?
Coach: Bro, you don't convince anybody. You show them the math. If you're trying to talk your client into spending
more, you've already lost the frame.

Sit down with your client and build their financial model with their real numbers, kept conservative. Say they want
$100K a month at $10K a client (example numbers). That's 10 clients. At a 20% close rate that's 50 calls. At a 50%
show rate that's 100 booked calls. At $200 a booked call, that's $20K in ad spend. Put that next to what they spend
today. If it's $5K, the numbers tell them straight up: you won't hit your goal. It's not my opinion, it's math.

Then frame it right: "I'm not here to promise you anything. Here's the plan, here's what it takes, here's the risk.
Are you okay with that, yes or no?" Rich people respect that. They don't want dreams sold to them.

And hold the standard. Most offer owners stay stuck because they're slow and scared to take the next risk. If they
want to go from 100K to a million, they need a different version of themselves. Push them to risk more, not to break
the bank, but enough to actually test what works.

Last thing: track every lead's source. When you can show which channel brought in which booked calls and cash,
scaling spend isn't a debate anymore. You double down on what's working. Data, then action."""


def method_system() -> str:
    return (
        "You are the programme's coach, answering a student one-on-one in the programme owner's style. The course "
        "lessons don't answer this directly, so answer from the programme's method notes below. Voice: direct, "
        "confident, casual, short punchy sentences, no fluff. Reframe a weak framing when there is one. When numbers "
        "help, walk through the money math with clearly marked example numbers (never invent the student's own "
        "numbers). Honest over hype: no promises, guarantees, income claims or invented results or names. Hold high "
        "standards and end with the next action. Use 'bro' at most once. Plain text only: no markdown, no asterisks, "
        "no headings; short paragraphs, bullets starting with '• ' only if they really help. 150-300 words. Don't "
        "claim to be a real person and don't say the answer is quoted from the course.\n\n"
        f"Method notes:\n{method_notes()}\n\nExample of the voice and depth wanted:\n{VOICE_EXAMPLE}")


LABEL = "From the programme's method (not a specific lesson yet) · AI"


def rewrites(tenant, run: runtime.Run, question: str) -> list[str]:
    if llm.unavailable_reason(tenant) is not None:
        return []

    def _ai() -> list[str]:
        out = llm.complete(tenant, REWRITE_SYSTEM, question, purpose="coach-rewrite", run_id=run.id, max_tokens=300)
        qs = llm.parse_json(out["text"]).get("queries") or []
        return [q.strip() for q in qs if isinstance(q, str) and q.strip() and q.strip().lower() != question.lower()][:3]

    return run.step("rewrite_ai", {"question": question}, _ai, "Rewrite (AI): spelling and plain phrasings",
                    allow_fail=True) or []


def _background(ext: list[dict] | None) -> str:
    """Researched playbook entries that matched the original question, as extra facts (use only if relevant)."""
    from .. import playbooks

    out = []
    for c in (ext or [])[:3]:
        e = playbooks.entry(c.get("entry_id", ""))
        if e is not None:
            out.append(f"- {e.title}: {e.guidance[:900]}")
    return ("\n\nExtra researched practice (use only if it truly answers the question; say it in the same voice):\n"
            + "\n".join(out)) if out else ""


def method_answer(tenant, run: runtime.Run, question: str, ext: list[dict] | None = None) -> dict | None:
    if llm.unavailable_reason(tenant) is not None:
        return None
    prompt = f"Student question: {question}{_background(ext)}"

    def _ai() -> str:
        out = llm.complete(tenant, method_system(), prompt, purpose="coach-method",
                           run_id=run.id, max_tokens=1500)
        return out["text"].strip().replace("**", "")

    text = run.step("method_answer", {"question": question}, _ai, "Answer from the programme's method (AI)",
                    allow_fail=True)
    if not text or guardrails.blocked(guardrails.check_for(tenant, text, {"source": "method"})):
        return None
    run.use_ai("live")
    return {"answer": guardrails.scrub(text, guardrails.tenant_banned_names(tenant)), "mode": "live",
            "label": LABEL}


def recover(tenant, run: runtime.Run, question: str, res: dict, retry) -> dict:
    """`res` is the honest not-covered result; `retry(q)` answers another phrasing the normal way. A rewrite that a
    course lesson covers wins (it cites the lesson). Otherwise the answer comes from the programme's method in its
    voice, replacing loosely related playbook entries. Without the AI, `res` stands unchanged."""
    if res.get("covered") != "no" or llm.unavailable_reason(tenant) is not None:
        return res
    for q in rewrites(tenant, run, question):
        alt = retry(q)
        if alt.get("covered") != "no":
            alt["note"] = f"Searched as: \"{q}\""
            return alt
    m = method_answer(tenant, run, question, res.get("external"))
    if m is None:
        return res
    return {**res, "answer": m["answer"], "mode": m["mode"], "label": m["label"], "method": True, "external": [],
            "external_mode": None, "note": None}
