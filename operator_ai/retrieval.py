"""Retrieval for a tenant: hybrid search -> citation-ready evidence, scrubbed of banned names."""
from __future__ import annotations

from . import db, guardrails, index


def priorities(tenant) -> dict[str, float]:
    try:
        with tenant.db() as c:
            return {r["file"]: float(r["priority"] or 1.0) for r in c.execute("SELECT file, priority FROM kb_notes")}
    except Exception:  # noqa: BLE001
        return {}


def provenance_overrides(tenant) -> dict[str, dict]:
    with tenant.db() as c:
        return {r["file"]: dict(r) for r in c.execute("SELECT * FROM kb_notes")}


def _persona_boost(tenant, idx, query: str) -> dict[int, float] | None:
    """+10% for passages mostly said by the persona subject, only while a valid persona consent is active."""
    try:
        from .persona import apply as persona_apply
    except ImportError:
        return None
    return persona_apply.retrieval_boost(tenant, idx, query)


def search(tenant, query: str, k: int = 10, prefix: str = "S") -> list[dict]:
    idx = index.for_tenant(tenant)
    hits = idx.search(query, k=k, priority=priorities(tenant), chunk_boost=_persona_boost(tenant, idx, query))
    names = guardrails.tenant_banned_names(tenant)
    notes = provenance_overrides(tenant)
    out = []
    for n, h in enumerate(hits, start=1):
        c = h["chunk"]
        note = notes.get(c.file, {})
        out.append({
            "id": f"{prefix}{n}",
            "file": c.file,
            "lines": c.lines_label,
            "ts": c.ts_start,
            "title": guardrails.scrub(c.title, names),
            "snippet": guardrails.scrub(c.text[:400], names),
            "text": guardrails.scrub(c.text, names),
            "kind": c.kind,
            "quality": c.quality,
            "provenance": note.get("provenance") or c.provenance,
            "licence_status": note.get("licence") or c.licence_status,
            "score": round(h["score"], 5),
            "bm25": round(h["bm25"], 3),
            "dense": round(h["dense"], 3),
        })
    return out


def cite(h: dict) -> dict:
    """The public citation shape used by every agent output."""
    return {k: h[k] for k in ("id", "file", "lines", "ts", "title", "snippet", "kind", "quality") if k in h}


def coverage(tenant, query: str, hits: list[dict], ignore_unknown: bool = False) -> dict:
    """Lexical coverage judge: what share of the question's key terms (idf-weighted) does the
    corpus know at all, and how strongly do the top passages match?"""
    idx = index.for_tenant(tenant)
    known, unknown = idx.known_terms(query)
    if ignore_unknown:
        # translated questions keep some untranslated words; judge on the mapped terms only
        unknown = [] if len(known) >= 2 else unknown
    if not known and not unknown:
        return {"covered": "no", "ratio": 0.0, "known": [], "unknown": [], "top_bm25": 0.0}
    from . import textnorm

    w_known = sum(max(idx.idf(t), 0.8) for t in known)
    # unseen rare words (tiktok, shopify, dns) weigh heavily; unseen common words barely count
    w_unknown = sum(0.5 if t in textnorm.COMMON else 2.5 for t in unknown)
    ratio = w_known / (w_known + w_unknown) if (w_known + w_unknown) else 0.0
    top = max((h["bm25"] for h in hits[:5]), default=0.0)
    if ratio >= 0.7 and top >= 3.0:
        covered = "yes"
    elif ratio >= 0.5 and top >= 1.5:
        covered = "partly"
    else:
        covered = "no"
    return {"covered": covered, "ratio": round(ratio, 3), "known": known, "unknown": unknown, "top_bm25": round(top, 3)}


def source_view(tenant, file: str, lines: str | None) -> dict | None:
    """Serve only indexed corpus files (lookup by exact indexed path: no traversal possible)."""
    idx = index.for_tenant(tenant)
    doc = idx.docs.get(file)
    if doc is None:
        return None
    names = guardrails.tenant_banned_names(tenant)
    start = end = 0
    if lines:
        a, _, b = lines.partition("-")
        try:
            start, end = int(a), int(b or a)
        except ValueError:
            start = end = 0
    excluded = {int(e.lines.split("-")[0]) for e in idx.excluded if e.file == file}
    out_lines = []
    for n, text in enumerate(doc.lines, start=1):
        if n in excluded:
            text = "[Not used: excluded by the content policy]"
        out_lines.append({"n": n, "text": guardrails.scrub(text, names), "hit": start <= n <= end if start else False,
                          "ts": doc.ts[n - 1] if doc.ts else None})
    return {"file": file, "title": guardrails.scrub(doc.title, names), "kind": doc.kind, "quality": doc.quality,
            "start": start, "end": end, "lines": out_lines}


def record_query(tenant, user_id: int | None, question: str, covered: str, lang: str, run_id: int | None) -> None:
    with tenant.db() as c:
        c.execute("INSERT INTO queries(user_id,question,covered,lang,run_id,created) VALUES(?,?,?,?,?,?)",
                  (user_id, question[:2000], covered, lang, run_id, db.now()))
