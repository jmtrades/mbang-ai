"""Student Coach: cited Q&A grounded only in the owner's material.

Plan: detect language -> rewrite/decompose -> hybrid retrieval per part -> coverage judge
-> answer only from retrieved passages -> verify every [S#] (strip invented ones).
Output: {covered: yes|partly|no, answer, citations, ...}. With no AI: an extractive answer
from the top passages, or a labelled replay of a saved AI answer to the same question.
"""
from __future__ import annotations

import re

from .. import guardrails, index, llm, retrieval, runtime, textnorm, threads, translate

PLAN = ["Detect language", "Rewrite / decompose question", "Hybrid retrieval per part", "Coverage judge",
        "Answer from passages only", "Verify citations"]
CITE_RE = re.compile(r"\[(S\d+)\]")
XCITE_RE = re.compile(r"\[(X\d+)\]")
SYSTEM = ("You are the programme's coach. Answer ONLY from the numbered course passages. Cite every claim with "
          "[S#] markers that exist in the list. If the passages don't cover the question, say exactly: "
          "\"The course material doesn't cover this.\" Never invent results, numbers or names. "
          "Reply as JSON: {\"covered\": \"yes|partly|no\", \"answer\": \"...\", \"citations\": [\"S1\", ...]}")


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
    return " ".join(f"{s} [{sid}]" for _, _, sid, s in chosen)


def verify_citations(answer: str, cited: list[str], allowed: set[str]) -> tuple[str, list[str], list[str]]:
    """Strip any [S#] not in the retrieved set. Returns (clean answer, kept ids, stripped ids)."""
    stripped = sorted({c for c in CITE_RE.findall(answer) if c not in allowed} | {c for c in cited if c not in allowed})
    clean = CITE_RE.sub(lambda m: m.group(0) if m.group(1) in allowed else "", answer)
    clean = re.sub(r"\s{2,}", " ", clean).strip()
    kept = [c for c in dict.fromkeys(CITE_RE.findall(clean)) if c in allowed]
    return clean, kept, stripped


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


def answer(tenant, question: str, user: dict | None = None, record: bool = True, thread: bool = False,
           thread_id: int | None = None, run: runtime.Run | None = None, k: int = 6) -> dict:
    own_run = run is None
    actor = f"{user['username']}({user['role']})" if user else "system"
    if own_run:
        run = runtime.Run(tenant, "coach", f"Coach: {question[:80]}", PLAN, actor)
    try:
        res = _answer(tenant, question, run, k)
    except Exception as e:
        run.fail(e)
        raise
    res["run_id"] = run.id
    res["ai_mode"] = run.ai_mode
    if own_run:
        run.finish(res)
    if record:
        retrieval.record_query(tenant, user["id"] if user else None, question, res["covered"], res["lang"], run.id)
    if thread and user:
        tid = thread_id or threads.create(tenant, user["id"], question)
        threads.add_message(tenant, tid, "user", question)
        threads.add_message(tenant, tid, "assistant", res["answer"], {"citations": res["citations"], "covered": res["covered"],
                                                                      "run_id": run.id, "label": res.get("label")})
        res["thread_id"] = tid
    return res


def _answer(tenant, question: str, run: runtime.Run, k: int) -> dict:
    lang = run.step("detect_language", {"question": question}, lambda: translate.detect(question), "Detect language")
    q_en = translate.to_english_terms(question, lang) if lang != "en" else question

    # rewrite / decompose (AI when available, else rules keep the original or split clear multi-part)
    def _rewrite():
        parts = decompose(q_en)
        return {"parts": parts, "by": "rules"}

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
        return {"covered": "no", "answer": translate.NOT_COVERED[lang], "citations": [], "external": [], "parts": judged,
                "lang": lang, "note": None, "mode": "rules", "label": "Honest: not covered by the course material",
                "not_used": []}

    passages = "\n\n".join(f"[{h['id']}] ({h['title']}, lines {h['lines']})\n{h['text']}" for h in usable)
    prompt = f"Question: {question}\n\nCourse passages:\n{passages}"
    names = guardrails.tenant_banned_names(tenant)
    text, cited, mode, label = "", [], "rules", "Extractive answer from course passages (AI off)"
    reason = llm.unavailable_reason(tenant)

    def _ai():
        out = llm.complete(tenant, SYSTEM, prompt, purpose="coach", run_id=run.id, max_tokens=700)
        data = llm.parse_json(out["text"])
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
                return " ".join(b for b in bits if b)

            text = run.step("answer", {"passages": sorted(allowed), "mode": "extractive"}, _extract,
                            "Answer from passages only")
            if reason:
                label = f"Extractive answer from course passages ({reason})"

    clean, kept, stripped = run.step("verify_citations", {"cited": cited, "allowed": sorted(allowed)},
                                     lambda: verify_citations(text, cited, allowed), "Verify citations")
    if covered != "no" and not kept and usable and mode != "live":
        # extractive answers always carry their passages
        kept = [usable[0]["id"]]
        clean = f"{clean} [{usable[0]['id']}]".strip()
    if covered == "no" or not clean:
        clean, kept, covered = translate.NOT_COVERED[lang], [], "no"
    cites = [retrieval.cite(h) for h in usable if h["id"] in kept]
    note = translate.NOTE.get(lang) if lang != "en" and mode != "live" else None
    if covered == "partly":
        missing = [j["part"] for j in judged if j["covered"] == "no"]
        clean += " " + " ".join(f"(Not covered by the course material: \"{m}\")" for m in missing)
    return {"covered": covered, "answer": guardrails.scrub(clean, names), "citations": cites, "external": [],
            "stripped_citations": stripped, "parts": judged, "lang": lang, "note": note, "mode": mode, "label": label,
            "not_used": _not_used(tenant, usable)}


def _norm_q(q: str) -> str:
    return " ".join(textnorm.tokens(q, keep_stop=True))
