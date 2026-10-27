"""Student Coach: cited Q&A grounded only in the owner's material.

Plan: detect language -> rewrite/decompose -> hybrid retrieval per part -> coverage judge
-> answer only from retrieved passages -> verify every [S#] (strip invented ones).
Output: {covered: yes|partly|no, answer, citations, ...}. With no AI: an extractive answer
from the top passages, or a labelled replay of a saved AI answer to the same question.
"""
from __future__ import annotations

import re

from .. import guardrails, index, llm, playbooks, retrieval, runtime, textnorm, threads, translate

PLAN = ["Detect language", "Rewrite / decompose question", "Hybrid retrieval per part", "Coverage judge",
        "Answer from passages only", "Verify citations"]
CITE_RE = re.compile(r"\[(S\d+)\]")
XCITE_RE = re.compile(r"\[(X\d+)\]")
SYSTEM = ("You are the programme's coach. Answer ONLY from the numbered course passages. Cite every claim with "
          "[S#] markers that exist in the list. If the passages don't cover the question, say exactly: "
          "\"The course material doesn't cover this.\" Never invent results, numbers or names. "
          "Reply as JSON: {\"covered\": \"yes|partly|no\", \"answer\": \"...\", \"citations\": [\"S1\", ...]}")
# External best practice (the 2026 Playbooks): a separate, clearly labelled list [X#], never course material.
X_SYSTEM = ("You summarise EXTERNAL researched best practice from the numbered entries [X#]. They are dated public "
            "sources, NOT the programme's course material: never call them the course or the programme. Write 2 to 4 "
            "short bullets, each ending with the [X#] marker of the entry it comes from. Use only what the entries "
            "say. No income claims, guarantees, invented numbers or names. "
            "Reply as JSON: {\"bullets\": [\"... [X1]\", \"... [X2]\"]}")
X_HEADER = "External best practice (researched {as_of}, not the course material):"
X_PURPOSE = "coach-external"
X_MAX_BULLET = 280
# an external bullet must never present itself as the programme's own material
COURSE_CLAIM = re.compile(r"\b(?:the|this|our|your|my) (?:course|programme|program|curriculum)\b|"
                          r"\bcourse (?:material|passages?|content|says)\b|\b(?:module|lesson) \d", re.I)


def decompose(question: str) -> list[str]:
    """Rules fallback: split clear multi-part questions; otherwise keep the original."""
    q = question.strip()
    parts = [p.strip() for p in re.split(r"\?\s+(?=[A-Z])", q) if p.strip()]
    if len(parts) > 1:
        return [p if p.endswith("?") else p + "?" for p in parts][:4]
    m = re.split(r"\s+and\s+(?=(?:what'?s|what|how|when|which|who|why|where|should|can|do|is|are)\b)", q,
                 maxsplit=1, flags=re.I)
    if len(m) == 2:
        a, b = m[0].strip().rstrip("?"), m[1].strip()
        return [a + "?", b if b.endswith("?") else b + "?"]
    return [q]


def _sentences(text: str) -> list[str]:
    """Full sentences only (headings without end punctuation are skipped)."""
    out = []
    for line in text.split("\n"):
        for s in re.split(r"(?<=[.!?])\s+", line):
            s = s.strip()
            if len(s.split()) >= 4 and (s[-1] in ".!?" or ":" in s):
                out.append(s)
    return out


def extractive(question: str, hits: list[dict], max_sentences: int = 4, idf=None) -> str:
    """Pick the passage sentences that best match the question (idf-weighted), keep their [S#]."""
    qw = dict(textnorm.expand(textnorm.tokens(question)))
    idf = idf or (lambda t: 1.0)
    scored = []
    for rank, h in enumerate(hits[:6]):
        title_t = set(textnorm.tokens(h.get("title", "")))
        for i, s in enumerate(_sentences(h["text"])):
            st = set(textnorm.tokens(s))
            overlap = sum(qw[t] * idf(t) for t in st if t in qw)
            if overlap <= 0:
                continue
            # terms only in the document title count for half (a sentence from the USP SOP is about the USP)
            overlap += 0.5 * sum(qw[t] * idf(t) for t in title_t - st if t in qw)
            if overlap <= 0:
                continue
            scored.append((overlap + 0.05 * (6 - rank) - 0.01 * i, h["id"], s, rank, i))
    scored.sort(key=lambda x: -x[0])
    if scored:
        floor = scored[0][0] * 0.6  # drop weak matches that only share a common word
        scored = [x for x in scored if x[0] >= floor]
    chosen = []
    seen = set()
    for _, sid, s, rank, i in scored:
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        chosen.append((rank, i, sid, s))
        if len(chosen) >= max_sentences:
            break
    chosen.sort()
    return "\n".join(f"• {s} [{sid}]" for _, _, sid, s in chosen)


def verify_citations(answer: str, cited: list[str], allowed: set[str]) -> tuple[str, list[str], list[str]]:
    """Strip any [S#] not in the retrieved set. Returns (clean answer, kept ids, stripped ids)."""
    stripped = sorted({c for c in CITE_RE.findall(answer) if c not in allowed} | {c for c in cited if c not in allowed})
    clean = CITE_RE.sub(lambda m: m.group(0) if m.group(1) in allowed else "", answer)
    clean = re.sub(r"\s{2,}", " ", clean).strip()
    kept = [c for c in dict.fromkeys(CITE_RE.findall(clean)) if c in allowed]
    return clean, kept, stripped


def _split_claims(line: str) -> list[str]:
    """Sentences of one line; a trailing run of [S#] markers ("... hours. [S1]") stays with its sentence."""
    out: list[str] = []
    for s in re.split(r"(?<=[.!?])\s+", line.strip()):
        if out and re.fullmatch(r"(?:\[S\d+\]\s*)+[.!?]?", s):
            out[-1] = f"{out[-1]} {s}"
        elif s:
            out.append(s)
    return out


def ground_sentences(text: str, allowed: set[str]) -> tuple[str, list[str]]:
    """An AI answer is served as course-grounded only claim by claim: every sentence must carry a valid [S#]
    (short connectors without numbers, like "In short:", may stay). Uncited sentences are dropped. Returns
    (kept text, kept ids); ("", []) when no valid citation is left."""
    lines = []
    for line in (text or "").split("\n"):
        keep = []
        for s in _split_claims(line):
            if any(c in allowed for c in CITE_RE.findall(s)):
                keep.append(s)
            elif ((s.endswith(":") and len(s.split()) <= 6) or len(s.split()) <= 2) and not re.search(r"\d", s):
                # a connector ("In short:", "Yes.") may stay; "Raise your prices." is advice and needs a citation
                keep.append(s)
        if keep:
            lines.append(" ".join(keep))
    clean = "\n".join(lines).strip()
    kept = [c for c in dict.fromkeys(CITE_RE.findall(clean)) if c in allowed]
    return (clean, kept) if kept else ("", [])


def guard_sentences(tenant, text: str) -> tuple[str, list[dict]]:
    """Run the guardrails (plain mode) on every sentence of an AI answer and drop the blocked ones. Banned names
    are not a reason to drop (they are scrubbed instead). Returns (clean text, dropped [{sentence, codes}])."""
    lines, dropped = [], []
    for line in (text or "").split("\n"):
        keep = []
        for s in _split_claims(line):
            flags = [f for f in guardrails.check(CITE_RE.sub("", s), "plain", None, None, tenant=tenant)
                     if f["severity"] == "block"]
            if flags:
                dropped.append({"sentence": s[:200], "codes": sorted({f["code"] for f in flags})})
            else:
                keep.append(s)
        if keep:
            lines.append(" ".join(keep))
    return "\n".join(lines).strip(), dropped


def _extractive_text(tenant, per_part: list[dict], judged: list[dict], usable: list[dict]) -> str:
    bits = []
    for pp, j in zip(per_part, judged):
        if j["covered"] == "no":
            continue
        part_hits = [h for h in usable if h["id"] in set(pp["ids"][:5])]
        bits.append(extractive(pp["part"], part_hits, max_sentences=3 if len(per_part) == 1 else 2,
                               idf=index.for_tenant(tenant).idf))
    bits = [b for b in bits if b]
    return ("From the course material:\n" + "\n".join(bits)) if bits else ""


def verify_external(text: str, allowed: set[str]) -> tuple[str, list[str], list[str]]:
    """Strip any [X#] that isn't in the external list. Returns (clean text, kept ids, stripped ids)."""
    stripped = sorted({c for c in XCITE_RE.findall(text) if c not in allowed})
    clean = XCITE_RE.sub(lambda m: m.group(0) if m.group(1) in allowed else "", text)
    clean = re.sub(r"[ \t]{2,}", " ", clean)
    clean = re.sub(r"[ \t]+([.,;:!?])", r"\1", clean).strip()
    kept = [c for c in dict.fromkeys(XCITE_RE.findall(clean)) if c in allowed]
    return clean, kept, stripped


def external_refs(answer_text: str, ext: list[dict], tenant=None) -> str:
    """A plain-text key for the [X#] markers an answer actually uses, for surfaces without chips (Telegram /
    Slack / email reply drafts, MCP), so a reader never gets a bare [X1] with no source. "" when none.
    These drafts go through the approval queue and its guardrails, so a title the guardrails would block
    (compliance entries name what not to do: "Refund and guarantee design") is replaced by its domain."""
    used = set(XCITE_RE.findall(answer_text or ""))
    rows = [c for c in ext or [] if c.get("id") in used]
    if not rows:
        return ""
    lines = []
    for c in rows:
        title = c.get("title") or ""
        if not title or not _x_ok(tenant, title):
            title = f"{c.get('domain_title') or playbooks.LIBRARY_NAME} entry"
        lines.append(f"[{c['id']}] {title} (researched {c.get('as_of') or '?'}, {c.get('confidence') or '?'} confidence)"
                     + (f" {c['url']}" if c.get("url") else ""))
    return f"External, not the course material ({playbooks.LIBRARY_NAME}):\n" + "\n".join(lines)


def _x_ok(tenant, text: str) -> bool:
    """Never show external text that the guardrails would block (the library quotes banned phrases as
    examples of what not to say; those sentences are skipped, not shown)."""
    return not guardrails.blocked(guardrails.check_for(tenant, text, {"source": "playbooks", "external": True}))


def _clip(text: str) -> str:
    text = " ".join(text.split())
    if len(text) > X_MAX_BULLET:
        return text[: X_MAX_BULLET - 1].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return text if not text or text[-1] in ".!?…)\"'" else text + "."


def external_bullets(tenant, question: str, ext: list[dict], max_bullets: int = 4) -> list[str]:
    """Rules path: 2-4 bullets picked from the external entries (best-matching statements first, one per
    entry, a second from the top entry if only one is relevant), each ending with its [X#]."""
    lib = playbooks.load()
    idx = lib.index if lib.entries else None
    qw = dict(textnorm.expand(textnorm.tokens(question)))

    def score(s: str) -> float:
        st = set(textnorm.tokens(s))
        return sum(qw[t] * (idx.idf(t) if idx else 1.0) for t in st if t in qw)

    queues: list[tuple[str, list[str]]] = []
    for c in ext:
        e = playbooks.entry(c["entry_id"])
        if e is None:
            continue
        cands = [(score(s), i, s) for i, (_, s) in enumerate(playbooks.sentences(e))]
        cands.sort(key=lambda x: (-x[0], x[1]))
        queues.append((c["id"], [s for _, _, s in cands] + [e.title]))
    bullets: list[str] = []
    used: set[str] = set()
    for rnd in range(2):
        for xid, cands in queues:
            if len(bullets) >= max_bullets or (rnd == 1 and len(bullets) >= 2):
                break
            while cands:
                s = _clip(cands.pop(0))
                if s.lower() in used or not _x_ok(tenant, s):
                    continue
                used.add(s.lower())
                bullets.append(f"• {s} [{xid}]")
                break
    return bullets


def _external_ai(tenant, run: runtime.Run, question: str, ext: list[dict]) -> dict | None:
    """AI path (live or a labelled replay of a saved AI run). None when neither is available."""
    blocks = []
    for c in ext:
        e = playbooks.entry(c["entry_id"])
        if e is None:
            continue
        steps = "\n".join(f"- {s}" for s in e.steps[:5])
        blocks.append(f"[{c['id']}] {e.title} (researched {e.as_of}, confidence {e.confidence})\n{e.guidance}"
                      + (f"\nSteps:\n{steps}" if steps else ""))
    prompt = f"Question: {question}\n\nExternal entries (researched public best practice, not course material):\n\n" + \
        "\n\n".join(blocks)
    if llm.unavailable_reason(tenant) is not None and llm.replay(tenant, X_PURPOSE, prompt) is None:
        return None

    def _ai():
        out = llm.complete_or_replay(tenant, X_SYSTEM, prompt, X_PURPOSE, run_id=run.id, max_tokens=500)
        data = llm.parse_json(out["text"])
        raw = data.get("bullets") if isinstance(data, dict) else None
        if isinstance(raw, str):
            raw = [ln for ln in raw.splitlines() if ln.strip()]
        if not isinstance(raw, list):
            raw = []
        # only plain text bullets; anything else (objects, numbers) is dropped rather than shown as "{...}"
        return {"bullets": [b for b in raw if isinstance(b, str) and b.strip()], "mode": out.get("mode", "live"),
                "label": out.get("label", "AI")}

    return run.step("external_answer", {"entries": [c["id"] for c in ext], "mode": "ai"}, _ai,
                    "External best practice (AI)", allow_fail=True)


def _external_section(tenant, run: runtime.Run, question: str, parts: list[str], ext: list[dict]) -> dict:
    """The "External best practice (researched <as_of>, not the course material)" block for a question the
    course doesn't cover: 2-4 bullets, each cited [X#]; invented [X#] stripped; blocked text never shown."""
    allowed = {c["id"] for c in ext}
    mode, label, stripped = "rules", "Extractive bullets from the 2026 Playbooks", []
    bullets: list[str] = []
    res = _external_ai(tenant, run, question, ext)
    if res and res.get("bullets"):
        for b in res["bullets"][:6]:
            stripped += CITE_RE.findall(b)  # course markers never belong in the external list
            b = CITE_RE.sub("", b)
            clean, kept, gone = verify_external(b, allowed)
            stripped += gone
            body = XCITE_RE.sub("", clean).strip(" •-*\t")
            if not kept or not body or COURSE_CLAIM.search(body) or not _x_ok(tenant, body):
                continue
            bullets.append(f"• {_clip(body)} " + " ".join(f"[{k}]" for k in kept))
            if len(bullets) >= 4:
                break
        if len(bullets) >= 2:
            mode, label = res["mode"], res["label"]
            run.use_ai(mode)
        else:
            bullets = []
    if not bullets:
        bullets = run.step("external_answer", {"entries": sorted(allowed), "mode": "extractive"},
                           lambda: external_bullets(tenant, " ".join(parts) or question, ext),
                           "External best practice (extractive)")
    if len(bullets) < 2:
        return {"text": "", "cited": [], "mode": mode, "label": label, "stripped": sorted(set(stripped))}
    cited = [c for c in dict.fromkeys(XCITE_RE.findall("\n".join(bullets))) if c in allowed]
    as_of = playbooks.as_of_label([c["as_of"] for c in ext if c["id"] in cited])
    text = X_HEADER.format(as_of=as_of) + "\n" + "\n".join(bullets)
    return {"text": text, "cited": cited, "mode": mode, "label": label, "stripped": sorted(set(stripped))}


def _external(tenant, run: runtime.Run, parts: list[str], allowed: bool = True) -> list[dict]:
    """Relevant 2026 Playbooks entries as X1..Xn, only when the tenant's third_party_kb switch is on and
    the caller allows external material (a scoped lead-magnet mini never does)."""
    if not allowed or not playbooks.enabled(tenant):
        return []
    return run.step("external", {"parts": parts, "library": playbooks.LIBRARY_NAME},
                    lambda: playbooks.relevant(tenant, parts, k=4),
                    "External playbooks (researched, not course)", allow_fail=True) or []


def _not_used(tenant, hits: list[dict]) -> list[dict]:
    idx = index.for_tenant(tenant)
    out = []
    for h in hits:
        a, _, b = h["lines"].partition("-")
        for e in idx.excluded:
            ln = int(e.lines.split("-")[0])
            if e.file == h["file"] and int(a) <= ln <= int(b or a):
                out.append({"file": e.file, "lines": e.lines, "label": e.label})
    return out


FOLLOWUP_LEAD = re.compile(r"^\s*(and|what about|how about|also|same for|what if|ok|okay|so|then|but|why|how come|"
                           r"and if|and for|what else)\b", re.I)
# words that point back at the previous question; "there" is left out ("Is there a kill rule?" is a new question)
FOLLOWUP_REF = re.compile(r"\b(it|its|it's|that|this|those|them|these|they|one|ones)\b", re.I)
FOLLOWUP = FOLLOWUP_LEAD  # kept for callers that imported the old name


def resolve_followup(question: str, previous: str | None) -> tuple[str, bool]:
    """A short follow-up ("what about for webinars?", "how long should it run?") is read in the context of the
    previous question in the thread. Only a question that really refers back counts: one with no subject of its
    own, a pronoun standing in for the earlier subject, or a continuation opener ("what about ...") with at
    most a couple of its own content words. "Is there a kill rule for ads?" after a USP question is a new
    question. Returns (question to search with, used_context)."""
    if not previous:
        return question, False
    words = question.split()
    if len(words) > 9:
        return question, False
    content = [t for t in textnorm.tokens(question) if len(t) > 1]
    lead = bool(FOLLOWUP_LEAD.search(question))
    ref = bool(FOLLOWUP_REF.search(question))
    if not content or (ref and len(content) <= 2) or (lead and len(content) <= 2):
        return f"{previous.rstrip('?. ')} — {question}", True
    return question, False


def answer(tenant, question: str, user: dict | None = None, record: bool = True, thread: bool = False,
           thread_id: int | None = None, run: runtime.Run | None = None, k: int = 6, context: str | None = None,
           external: bool = True) -> dict:
    """external=False: never add the 2026 Playbooks (for surfaces that must stay inside the course, such as
    a scoped, public lead-magnet mini), whatever the tenant's third_party_kb switch says."""
    own_run = run is None
    actor = f"{user['username']}({user['role']})" if user else "system"
    if own_run:
        run = runtime.Run(tenant, "coach", f"Coach: {question[:80]}", PLAN, actor)
    # [memory] what this person told the assistant about their business ("What I know about you"): with the AI on it
    # helps pick which course passages apply to them; the answer still comes only from the course, with citations
    about = None
    if user and user.get("id"):
        from .. import memory

        mem = memory.for_user(tenant, user["id"])
        about = "; ".join(f"{k}: {str(mem[k])[:120]}" for k in ("niche", "offer", "goal") if mem.get(k)) or None
    try:
        res = _answer(tenant, question, run, k, context, external, about=about)
    except Exception as e:
        run.fail(e)
        raise
    res["run_id"] = run.id
    res["ai_mode"] = run.ai_mode
    if user and user.get("id"):
        from .. import memory

        res["remembered"] = memory.hint(tenant, user, "question")
    # [persona] consent-gated framing: returns res unchanged unless a valid, subject-signed persona consent covers this
    # user and surface (operator_ai/persona/apply.py). Also answers "are you <name>?" honestly. Never raises.
    from ..persona import apply as persona_apply
    res = persona_apply.frame(tenant, question, res, user=user, run=run)
    if own_run:
        run.finish(res)
    if record:
        retrieval.record_query(tenant, user["id"] if user else None, question, res["covered"], res["lang"], run.id)
    if thread and user:
        tid = thread_id or threads.create(tenant, user["id"], question)
        threads.add_message(tenant, tid, "user", question)
        threads.add_message(tenant, tid, "assistant", res["answer"], {"citations": res["citations"], "covered": res["covered"],
                                                                      "external": res.get("external", []),
                                                                      "run_id": run.id, "label": res.get("label"),
                                                                      "mode": res.get("mode")})
        res["thread_id"] = tid
    return res


def _answer(tenant, question: str, run: runtime.Run, k: int, context: str | None = None, external: bool = True,
            about: str | None = None) -> dict:
    lang = run.step("detect_language", {"question": question}, lambda: translate.detect(question), "Detect language")
    q_en = translate.to_english_terms(question, lang) if lang != "en" else question

    # rewrite / decompose (AI when available, else rules keep the original or split clear multi-part);
    # short follow-ups are read in the context of the previous question in the thread
    def _rewrite():
        q, used = resolve_followup(q_en, context)
        parts = [q] if used else decompose(q)
        return {"parts": parts, "by": "rules (follow-up in context)" if used else "rules", "followup": used}

    rw = run.step("rewrite", {"question": q_en}, _rewrite, "Rewrite / decompose question")
    parts = rw["parts"]

    # retrieval per part; merge with stable S# numbering
    def _retrieve():
        merged: list[dict] = []
        seen = {}
        per_part = []
        for p in parts:
            hits = retrieval.search(tenant, p, k=k)
            ids = []
            for h in hits:
                key = (h["file"], h["lines"])
                if key not in seen:
                    h = {**h, "id": f"S{len(merged) + 1}"}
                    seen[key] = h["id"]
                    merged.append(h)
                ids.append(seen[key])
            per_part.append({"part": p, "hits": hits, "ids": ids})
        return merged, per_part

    merged, per_part = run.step("retrieve", {"parts": parts}, _retrieve, "Hybrid retrieval per part")

    def _judge():
        out = []
        for pp in per_part:
            cov = retrieval.coverage(tenant, pp["part"], pp["hits"], ignore_unknown=lang != "en")
            out.append({"part": pp["part"], **cov})
        return out

    judged = run.step("coverage", {"parts": parts}, _judge, "Coverage judge")
    states = [j["covered"] for j in judged]
    covered = "yes" if all(s == "yes" for s in states) else "no" if all(s == "no" for s in states) else "partly"
    # passages that belong to covered parts only
    use_ids = set()
    for pp, j in zip(per_part, judged):
        if j["covered"] != "no":
            use_ids |= set(pp["ids"][:5])
    usable = [h for h in merged if h["id"] in use_ids]
    allowed = {h["id"] for h in usable}

    if covered == "no":
        run.note("answer", {"covered": "no"}, {"answer": translate.NOT_COVERED[lang]}, label="Answer from passages only")
        run.note("verify_citations", {}, {"kept": [], "stripped": []}, label="Verify citations")
        return _not_covered(tenant, run, question, parts, judged, lang, [], external=external)

    passages = "\n\n".join(f"[{h['id']}] ({h['title']}, lines {h['lines']})\n{h['text']}" for h in usable)
    prompt = f"Question: {question}\n\nCourse passages:\n{passages}"
    if about:
        prompt += (f"\n\nAbout the person asking (their own notes; use them only to choose which passages apply, never as "
                   f"a source, never quoted): {about}")
    names = guardrails.tenant_banned_names(tenant)
    text, cited, mode, label = "", [], "rules", "Extractive answer from course passages (AI off)"
    reason = llm.unavailable_reason(tenant)

    def _ai():
        out = llm.complete(tenant, SYSTEM, prompt, purpose="coach", run_id=run.id, max_tokens=700)
        data = llm.parse_json(out["text"])
        if not about:  # an answer shaped by one person's notes is never replayed to someone else
            llm.save_replay(tenant, "coach-q", _norm_q(question), out["text"], run.id)
        return data

    data = None
    if reason is None:
        data = run.step("answer", {"passages": sorted(allowed), "mode": "ai"}, _ai, "Answer from passages only",
                        allow_fail=True)
    if data:
        text, cited, mode, label = str(data.get("answer", "")), list(data.get("citations", []) or []), "live", "AI answer"
        if str(data.get("covered", "")).lower() == "no":
            covered = "no"
        run.use_ai("live")
    else:
        rp = llm.replay(tenant, "coach-q", _norm_q(question))
        if rp:
            try:
                d = llm.parse_json(rp["text"])
                text, cited, mode, label = str(d.get("answer", "")), list(d.get("citations", []) or []), "replay", rp["label"]
                run.use_ai("replay")
            except ValueError:
                rp = None
        if not rp:
            def _extract():
                bits = []
                for pp, j in zip(per_part, judged):
                    if j["covered"] == "no":
                        continue
                    part_hits = [h for h in usable if h["id"] in set(pp["ids"][:5])]
                    bits.append(extractive(pp["part"], part_hits, max_sentences=3 if len(per_part) == 1 else 2,
                                           idf=index.for_tenant(tenant).idf))
                bits = [b for b in bits if b]
                if not bits:
                    return ""
                return "From the course material:\n" + "\n".join(bits)

            text = run.step("answer", {"passages": sorted(allowed), "mode": "extractive"}, _extract,
                            "Answer from passages only")
            if reason:
                label = f"Extractive answer from course passages ({reason})"

    clean, kept, stripped = run.step("verify_citations", {"cited": cited, "allowed": sorted(allowed)},
                                     lambda: verify_citations(text, cited, allowed), "Verify citations")
    # the course answer never carries external markers: any [X#] here was invented
    if XCITE_RE.search(clean):
        stripped = sorted(set(stripped) | set(XCITE_RE.findall(clean)))
        clean = re.sub(r"[ \t]{2,}", " ", XCITE_RE.sub("", clean)).strip()
    if covered != "no" and mode in ("live", "replay"):
        # AI text (live or replayed) is served only claim by claim with valid citations, and only the sentences
        # that pass the guardrails; nothing cited left -> the extractive answer from the same passages instead
        clean, kept = run.step("ground", {"kept": kept}, lambda: ground_sentences(clean, allowed),
                               "Keep only cited sentences")
        clean, dropped = run.step("guardrails", {}, lambda: guard_sentences(tenant, clean), "Guardrail check")
        kept = [c for c in dict.fromkeys(CITE_RE.findall(clean)) if c in allowed]
        if not kept:
            text = run.step("answer_fallback", {"why": "AI answer had no valid cited, guardrail-clean sentence"},
                            lambda: _extractive_text(tenant, per_part, judged, usable), "Extractive answer instead")
            clean, kept, _ = verify_citations(text, [], allowed)
            mode, label = "rules", "Extractive answer from course passages (the AI answer had no valid citations)"
    if covered != "no" and not kept and usable and mode not in ("live", "replay"):
        # extractive answers always carry their passages
        kept = [usable[0]["id"]]
        clean = f"{clean} [{usable[0]['id']}]".strip()
    if covered == "no" or not clean:
        return _not_covered(tenant, run, question, parts, judged, lang, stripped, base_mode=mode, external=external)
    cites = [retrieval.cite(h) for h in usable if h["id"] in kept]
    note = translate.NOTE.get(lang) if lang != "en" and mode != "live" else None
    if covered == "partly":
        missing = [j["part"] for j in judged if j["covered"] == "no"]
        clean += " " + " ".join(f"(Not covered by the course material: \"{m}\")" for m in missing)
    # after the course answer: a clearly separate list of external [X#] entries (never in citations)
    ext = _external(tenant, run, parts, external)
    return {"covered": covered, "answer": guardrails.scrub(clean, names), "citations": cites, "external": ext,
            "stripped_citations": stripped, "parts": judged, "lang": lang, "note": note, "mode": mode, "label": label,
            "not_used": _not_used(tenant, usable)}


def _not_covered(tenant, run: runtime.Run, question: str, parts: list[str], judged: list[dict], lang: str,
                 stripped: list[str], base_mode: str = "rules", external: bool = True) -> dict:
    """Honest "not covered" first. With the third_party_kb switch on and a playbook that does cover it, add
    the clearly labelled external section after that sentence (never as course material)."""
    text = translate.NOT_COVERED[lang]
    label = "Honest: not covered by the course material"
    ext = _external(tenant, run, parts, external)
    xmode, xlabel = None, None
    if ext:
        sec = _external_section(tenant, run, question, parts, ext)
        stripped = sorted(set(stripped) | set(sec["stripped"]))
        if sec["text"]:
            text = f"{text}\n\n{sec['text']}"
            label += f" · external best practice added ({playbooks.LIBRARY_NAME}, switch on)"
            xmode, xlabel = sec["mode"], sec["label"]
        # final safety pass: only X ids from the external list survive anywhere in the answer
        text, _, gone = verify_external(text, {c["id"] for c in ext})
        stripped = sorted(set(stripped) | set(gone))
    names = guardrails.tenant_banned_names(tenant)
    mode = xmode if xmode in ("live", "replay") else base_mode
    return {"covered": "no", "answer": guardrails.scrub(text, names), "citations": [], "external": ext,
            "external_mode": xmode, "external_label": xlabel, "stripped_citations": stripped, "parts": judged,
            "lang": lang, "note": None, "mode": mode, "label": label, "not_used": []}


def _norm_q(q: str) -> str:
    return " ".join(textnorm.tokens(q, keep_stop=True))
