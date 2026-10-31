"""When the course doesn't cover a question and the AI is on: try harder before saying no.

1. Rewrite: the AI fixes spelling and gives a few plain phrasings ("convice my cleints to scale adsoend" ->
   "how to get clients to increase their ad budget"), and the course and the playbooks are searched again with them.
2. Method answer: if no lesson matches, answer in the programme's voice from its method notes
   (knowledge/programme_method.md), labelled as coming from the method rather than a specific lesson.

Without the AI nothing here runs and the honest "not covered" answer stands.
"""
from __future__ import annotations

import re
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
        "lessons don't fully answer this, so answer from the programme's method notes below. Answer exactly what "
        "was asked, starting with a direct answer to their actual question (if they ask whose fault it is or "
        "whether to hire someone, say so plainly first). If course passages are given, use one only where it truly "
        "supports a point and put its [S#] marker right after that point; never cite a passage for anything it "
        "doesn't say, and ignore passages that are off-topic. Voice: direct, "
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


def method_answer(tenant, run: runtime.Run, question: str, ext: list[dict] | None = None,
                  cites: list[dict] | None = None) -> dict | None:
    """The answer in the programme's voice from its method notes, using course passages (cited) only where they truly
    fit. None when the AI is off or the text is blocked."""
    if llm.unavailable_reason(tenant) is not None:
        return None
    cites = cites or []
    passages = "\n".join(f"[{c['id']}] {c.get('title', '')}: {str(c.get('snippet', ''))[:700]}" for c in cites)
    prompt = f"Student question: {question}" + (f"\n\nCourse passages:\n{passages}" if passages else "") + \
        _background(ext)

    def _ai() -> str:
        out = llm.complete(tenant, method_system(), prompt, purpose="coach-method", run_id=run.id, max_tokens=1500)
        return out["text"].strip().replace("**", "")

    text = run.step("method_answer", {"question": question, "passages": [c["id"] for c in cites]}, _ai,
                    "Answer in the programme's voice from its method (AI)", allow_fail=True)
    if not text or guardrails.blocked(guardrails.check_for(tenant, text, {"source": "method"})):
        return None
    ids = {c["id"] for c in cites}
    text = re.sub(r"\[(S\d+)\]", lambda m: m.group(0) if m.group(1) in ids else "", text)  # no invented markers
    used = [c for c in cites if f"[{c['id']}]" in text]
    run.use_ai("live")
    label = LABEL if not used else "The programme's method, with course passages where they fit · AI"
    return {"answer": guardrails.scrub(text, guardrails.tenant_banned_names(tenant)), "mode": "live",
            "label": label, "citations": used}


def recover(tenant, run: runtime.Run, question: str, res: dict, retry) -> dict:
    """With the AI on, any question the course doesn't FULLY cover is answered in the programme's voice from its
    method (the student's actual question first), citing course passages only where they truly fit. A typo-fixed
    rewrite that a lesson fully covers wins as a normal cited course answer. AI off: `res` stands unchanged."""
    if res.get("covered") == "yes" or llm.unavailable_reason(tenant) is not None:
        return res
    base, note = res, None
    if res.get("covered") == "no":
        for q in rewrites(tenant, run, question):
            alt = retry(q)
            if alt.get("covered") == "yes":
                alt["note"] = f"Searched as: \"{q}\""
                return alt
            if alt.get("covered") == "partly" and base is res:
                base, note = alt, f"Searched as: \"{q}\""
    m = method_answer(tenant, run, question, res.get("external"), base.get("citations") or [])
    if m is None:
        return res
    return {**base, "answer": m["answer"], "mode": m["mode"], "label": m["label"], "citations": m["citations"],
            "covered": "partly" if m["citations"] else "no", "method": True, "external": [], "external_mode": None,
            "note": note}
