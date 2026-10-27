"""Speaker attribution: who said which line, never guessed.

Inline labels come from ingest (corpus.parse_speakers: VTT voices, "Name:" prefixes, Zoom and SRT styles). The owner
can mark a whole document as said by one speaker (kb_notes.speaker), for monologue lessons with no labels. A line
counts as the subject's only when its speaker label matches the persona name or one of its aliases. Unattributed
transcripts are excluded from mining by default.
"""
from __future__ import annotations

import time

from .. import config, corpus, db, index
from . import consent

DRAFT_WORDS = 5000
STABLE_WORDS = 20000
LICENSED = ("course", "own")


def subject_keys(tenant) -> set[str]:
    return {corpus.speaker_key(n) for n in consent.names(tenant)}


def note_speakers(tenant) -> dict[str, str]:
    try:
        with tenant.db() as c:
            return {r["file"]: r["speaker"] for r in c.execute("SELECT file, speaker FROM kb_notes WHERE speaker IS NOT NULL "
                                                               "AND speaker != ''")}
    except Exception:  # noqa: BLE001 - kb_notes without the speaker column (not migrated yet)
        return {}


def licences(tenant) -> dict[str, str]:
    try:
        with tenant.db() as c:
            return {r["file"]: r["licence"] for r in c.execute("SELECT file, licence FROM kb_notes WHERE licence IS NOT NULL")}
    except Exception:  # noqa: BLE001
        return {}


def doc_speakers(doc: corpus.Doc, note_speaker: str | None) -> list[str | None]:
    if doc.speakers and any(doc.speakers):
        return list(doc.speakers)
    if note_speaker:
        return [note_speaker if ln.strip() else None for ln in doc.lines]
    return [None] * len(doc.lines)


def uses_fixture(tenant) -> bool:
    return tenant.kb_scope == "course" and config.get().using_fixture_corpus and not tenant.is_demo


def source_docs(tenant) -> list[corpus.Doc]:
    """Documents the miner may read: the demo's fictional material in the demo workspace; otherwise the tenant's
    licensed knowledge base (never the fictional fixture corpus outside the demo)."""
    if tenant.is_demo:
        from . import demo_material

        return demo_material.docs() + [d for d in index.for_tenant(tenant).docs.values() if d.kind in ("sop", "pdf_sop")]
    idx = index.for_tenant(tenant)
    lic = licences(tenant)
    out = []
    for d in idx.docs.values():
        if uses_fixture(tenant) and d.corpus_id == "course":
            continue  # the fictional sample corpus is never mined outside the demo
        if (lic.get(d.file) or d.licence_status) not in LICENSED:
            continue
        out.append(d)
    return out


def coverage(tenant) -> dict:
    """Attributed words, documents and calls, against the 5k (draft) and 20k (stable) thresholds."""
    def _f():
        keys = subject_keys(tenant)
        notes = note_speakers(tenant)
        docs = []
        tot = {"subject_words": 0, "other_words": 0, "unattributed_words": 0, "subject_docs": 0, "calls": 0,
               "unattributed_docs": 0, "docs": 0}
        for d in source_docs(tenant):
            if d.kind in ("sop", "pdf_sop", "summary") and not notes.get(d.file):
                continue  # written material: background for mining, not attribution
            sp = doc_speakers(d, notes.get(d.file))
            sw = ow = uw = 0
            speakers: dict[str, int] = {}
            for ln, s in zip(d.lines, sp):
                w = len(corpus.speaker_text(ln).split())
                if not w:
                    continue
                if s is None:
                    uw += w
                elif corpus.speaker_key(s) in keys:
                    sw += w
                else:
                    ow += w
                if s:
                    speakers[s] = speakers.get(s, 0) + w
            tot["docs"] += 1
            tot["subject_words"] += sw
            tot["other_words"] += ow
            tot["unattributed_words"] += uw
            if sw:
                tot["subject_docs"] += 1
                if len(speakers) > 1:
                    tot["calls"] += 1
            if not speakers:
                tot["unattributed_docs"] += 1
            docs.append({"file": d.file, "title": d.title, "kind": d.kind, "quality": d.quality,
                         "subject_words": sw, "other_words": ow, "unattributed_words": uw,
                         "speakers": sorted(speakers, key=lambda k: -speakers[k])[:5], "marked_speaker": notes.get(d.file),
                         "demo": d.file.startswith("demo/")})
        tot["draft_at"] = DRAFT_WORDS
        tot["stable_at"] = STABLE_WORDS
        tot["level"] = ("stable" if tot["subject_words"] >= STABLE_WORDS else "draft" if tot["subject_words"] >= DRAFT_WORDS
                        else "not_enough")
        tot["fixture_refused"] = uses_fixture(tenant)
        return {"totals": tot, "docs": sorted(docs, key=lambda x: (-x["subject_words"], x["file"]))[:300]}

    return consent.cached(tenant, "coverage", _f, ttl=5.0)


def set_doc_speaker(tenant, file: str, speaker: str | None, actor: str) -> dict:
    """Mark a single-speaker document (or clear the mark). Only indexed files can be marked."""
    from .. import audit

    if file not in index.for_tenant(tenant).docs:
        raise consent.PersonaError(404, "Not an indexed file")
    sp = " ".join((speaker or "").split())[:80] or None
    with tenant.db() as c:
        cur = c.execute("SELECT id FROM kb_notes WHERE file=?", (file,)).fetchone()
        if cur is None:
            c.execute("INSERT INTO kb_notes(file,priority,note,provenance,licence,updated,speaker) VALUES(?,?,?,?,?,?,?)",
                      (file, 1.0, "", None, None, db.now(), sp))
        else:
            c.execute("UPDATE kb_notes SET speaker=?, updated=? WHERE file=?", (sp, db.now(), file))
    consent.cache_clear(tenant)
    audit.log(tenant, actor, "persona.attribution", {"file": file, "speaker_set": bool(sp)})
    return {"ok": True, "file": file, "speaker": sp}


# --------------------------------------------------------------------------- retrieval boost
_shares: dict[str, tuple[float, dict[int, float]]] = {}


def chunk_shares(tenant, idx) -> dict[int, float]:
    """Share of each chunk's attributed words said by the subject (chunks with no subject words are left out)."""
    keys = subject_keys(tenant)
    if not keys:
        return {}
    notes = note_speakers(tenant)
    sig = f"{tenant.db_path}|{idx.key}|{sorted(keys)}|{sorted(notes.items())}"
    hit = _shares.get(sig)
    if hit and hit[0] > time.time():
        return hit[1]
    out: dict[int, float] = {}
    for c in idx.chunks:
        marked = notes.get(c.file)
        if c.speaker_words:
            total = sum(c.speaker_words.values()) or 1
            mine = sum(v for k, v in c.speaker_words.items() if corpus.speaker_key(k) in keys)
            if mine:
                out[c.id] = mine / total
        elif marked and corpus.speaker_key(marked) in keys:
            out[c.id] = 1.0
    if len(_shares) > 64:
        _shares.clear()
    _shares[sig] = (time.time() + 30.0, out)
    return out
