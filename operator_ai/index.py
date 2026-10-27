"""Hybrid index: our own BM25 plus local dense vectors, fused with reciprocal-rank fusion."""
from __future__ import annotations

import json
import logging
import math
import threading
from collections import Counter
from pathlib import Path

import numpy as np

from . import config, corpus, embeddings, textnorm

log = logging.getLogger("operator.index")
RRF_K = 60
K1, B = 1.5, 0.75
_lock = threading.Lock()
_cache: dict[str, "Index"] = {}


class Index:
    def __init__(self, key: str, docs: list[corpus.Doc], chunks: list[corpus.Chunk], excluded: list[corpus.Excluded],
                 vectors: np.ndarray | None, embedder_name: str):
        self.key = key
        self.docs = {d.file: d for d in docs}
        self.chunks = chunks
        self.excluded = excluded
        self.embedder_name = embedder_name
        self.tfs: list[Counter] = []
        df: Counter = Counter()
        for c in chunks:
            toks = textnorm.tokens(c.title + "\n" + c.text)
            tf = Counter(toks)
            self.tfs.append(tf)
            df.update(tf.keys())
        self.df = df
        self.N = len(chunks)
        self.lens = np.array([sum(tf.values()) for tf in self.tfs], dtype=np.float32) if chunks else np.zeros(0)
        self.avgdl = float(self.lens.mean()) if chunks else 0.0
        self.vectors = vectors
        self.postings: dict[str, list[int]] = {}
        for i, tf in enumerate(self.tfs):
            for t in tf:
                self.postings.setdefault(t, []).append(i)

    # ------------------------------------------------------------------ scoring
    def idf(self, term: str) -> float:
        n = self.df.get(term, 0)
        return math.log(1 + (self.N - n + 0.5) / (n + 0.5))

    def bm25(self, q: list[tuple[str, float]]) -> np.ndarray:
        scores = np.zeros(self.N, dtype=np.float32)
        for term, w in q:
            ids = self.postings.get(term)
            if not ids:
                continue
            idf = self.idf(term)
            for i in ids:
                f = self.tfs[i][term]
                scores[i] += w * idf * f * (K1 + 1) / (f + K1 * (1 - B + B * self.lens[i] / self.avgdl))
        return scores

    def dense(self, query: str) -> np.ndarray:
        if self.vectors is None or not len(self.vectors):
            return np.zeros(self.N, dtype=np.float32)
        qv = embeddings.get().embed([query])[0]
        return self.vectors @ qv

    def search(self, query: str, k: int = 10, priority: dict[str, float] | None = None) -> list[dict]:
        if not self.N:
            return []
        q = textnorm.expand(textnorm.tokens(query))
        bm = self.bm25(q)
        de = self.dense(query)
        # Quality / kind weights (garbled x0.5, partial x0.85, SOP x1.1) and owner priority scale the
        # raw scores before ranking, so they move a passage up or down without swamping relevance.
        weights = np.array([c.weight for c in self.chunks])
        if priority:
            weights = weights * np.array([priority.get(c.file, 1.0) for c in self.chunks])
        bmw = bm * weights
        dew = (de + 1.0) * weights  # cosine in [-1, 1] -> positive before weighting
        fused = np.zeros(self.N, dtype=np.float64)
        for r, i in enumerate(np.argsort(-bmw)[:100]):
            if bm[i] > 0:
                fused[i] += 1.0 / (RRF_K + r + 1)
        # the hashed fallback embedding is a weaker signal than a real model, so it counts for less
        dw = 1.0 if self.embedder_name.startswith("bge") else 0.3
        for r, i in enumerate(np.argsort(-dew)[:100]):
            fused[i] += dw / (RRF_K + r + 1)
        final = fused
        order = np.argsort(-final)[:k]
        out = []
        for i in order:
            if final[i] <= 0:
                continue
            c = self.chunks[int(i)]
            out.append({"chunk": c, "score": float(final[i]), "bm25": float(bm[i]), "dense": float(de[i])})
        return out

    def known_terms(self, query: str) -> tuple[list[str], list[str]]:
        """A term is known if the corpus uses it or one of its synonyms."""
        toks = [t for t in dict.fromkeys(textnorm.tokens(query)) if len(t) > 1]
        syn = textnorm._syn()
        known, unknown = [], []
        for t in toks:
            if t in self.df or any(s in self.df for s in syn.get(t, [])):
                known.append(t)
            else:
                unknown.append(t)
        return known, unknown

    def status(self) -> dict:
        return {"ok": True, "key": self.key, "docs": len(self.docs), "chunks": self.N, "excluded": len(self.excluded),
                "embeddings": self.embedder_name}


EMPTY = None


def _empty() -> Index:
    global EMPTY
    if EMPTY is None:
        EMPTY = Index("empty", [], [], [], None, "none")
    return EMPTY


def exclusions_path() -> Path:
    return config.get().data_dir / "kb_exclusions.json"


def load_exclusions() -> list[dict]:
    p = exclusions_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except ValueError:
            return []
    return []


def save_exclusions(items: list[dict]) -> None:
    p = exclusions_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(items, indent=2))
    invalidate()


def _build(key: str, docs: list[corpus.Doc], exclusions: list[dict]) -> Index:
    chunks, excluded = corpus.chunk_docs(docs, exclusions)
    emb = embeddings.get()
    cache = config.get().index_dir / f"{key}-{emb.name}"
    vecs = None
    if (cache / "vectors.npy").exists() and (cache / "meta.json").exists():
        try:
            meta = json.loads((cache / "meta.json").read_text())
            if meta.get("n") == len(chunks):
                vecs = np.load(cache / "vectors.npy")
        except (ValueError, OSError):
            vecs = None
    if vecs is None:
        texts = [f"{c.title}\n{c.text}" for c in chunks]
        vecs = emb.embed(texts) if texts else np.zeros((0, embeddings.DIM), dtype=np.float32)
        try:
            cache.mkdir(parents=True, exist_ok=True)
            np.save(cache / "vectors.npy", vecs)
            (cache / "meta.json").write_text(json.dumps({"n": len(chunks), "embedder": emb.name}))
        except OSError:
            pass
    return Index(key, docs, chunks, excluded, vecs, emb.name)


def course_index(additions: Path | None = None) -> Index:
    """The course corpus, plus owner-approved additions (Drive inbox, uploads) when given."""
    cfg = config.get()
    root = cfg.effective_corpus_dir
    exclusions = load_exclusions()
    key = "course-" + corpus.fingerprint(root, json.dumps(exclusions, sort_keys=True))
    if additions is not None and additions.exists() and any(additions.glob("*.txt")):
        key += "-plus-" + corpus.fingerprint(additions)
    else:
        additions = None
    with _lock:
        if key in _cache:
            return _cache[key]
        docs = corpus.load(root)
        if additions is not None:
            docs += corpus.load_uploads(additions, corpus_id="course-additions")
        idx = _build(key, docs, exclusions)
        _cache[key] = idx
        return idx


def own_index(tenant) -> Index:
    kb = tenant.kb_dir
    key = f"own-{tenant.id}-" + corpus.fingerprint(kb)
    with _lock:
        if key in _cache:
            return _cache[key]
        idx = _build(key, corpus.load_uploads(kb, corpus_id=f"own:{tenant.id}"), [])
        _cache[key] = idx
        return idx


def for_tenant(tenant) -> Index:
    """The knowledge base a tenant may search. The course KB is owner-only (licence)."""
    from .tenancy import check_licence

    scope = tenant.kb_scope
    check_licence(tenant.kind, scope)
    if scope == "course":
        return course_index(tenant.kb_dir)
    if scope == "own":
        return own_index(tenant)
    return _empty()


def corpus_root_for(tenant) -> Path:
    if tenant.kb_scope == "own":
        return tenant.kb_dir.parent
    return config.get().effective_corpus_dir


def invalidate() -> None:
    with _lock:
        _cache.clear()


_warming = threading.Event()


def warm_async() -> None:
    """Build the course index in the background at startup so the first visitor never waits."""
    def _go():
        _warming.set()
        try:
            course_index()
        except Exception:  # noqa: BLE001 - a failed warm-up just means the first request builds it
            pass
        finally:
            _warming.clear()
        try:
            from . import playbooks  # the separate 2026 Playbooks index (its vectors are cached on disk too)

            playbooks.load().index
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_go, name="index-warm", daemon=True).start()


def status() -> dict:
    if _warming.is_set():
        return {"ok": False, "building": True, "embeddings": embeddings.get().name,
                "note": "Indexing the course material (first start with a new corpus can take a few minutes)."}
    idx = course_index()
    st = idx.status()
    st["corpus"] = "fixture" if config.get().using_fixture_corpus else "inputs"
    return st
