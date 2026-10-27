"""2026 Playbooks: researched, dated, sourced public best practice.

This is a separate knowledge library. It is NOT the programme owner's course material and is never
mixed into the course index, its [S#] citations or its coverage judge.

* Data: one JSON file per domain in ``operator_ai/knowledge/playbooks/`` shaped
  ``{domain, title, researched, entries: [{id, title, category, guidance, steps, benchmarks, do, dont,
  as_of, sources, confidence}], open_questions, note}``.
* ``load()`` validates every file and every entry. A bad file or entry is skipped and reported in
  ``Library.problems``; nothing is half-loaded. Source links must be http(s) (they open in a new tab).
* A separate hybrid index (the same BM25 + local dense machinery as the course KB in ``index.py``),
  one chunk per entry, kind ``playbook``.
* ``search()`` returns citations ``X1..Xn`` with entry id, domain, title, as_of, confidence and sources.
* The tenant switch ``third_party_kb`` (default off; on in the demo) gates every use: agents and
  non-owner readers see nothing while it is off. The owner may preview the library to decide.

The library is teaching material about compliance too, so it quotes phrases such as "guaranteed" as
examples of what not to say. It is shown verbatim on the Playbooks page; anything an agent picks out
of it for an answer is run through ``guardrails.check_for`` and dropped if blocked.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np

from . import config, corpus, embeddings, guardrails, textnorm
from .index import Index

log = logging.getLogger("operator.playbooks")

ROOT = Path(__file__).resolve().parent / "knowledge" / "playbooks"
SWITCH = "third_party_kb"
LIBRARY_NAME = "2026 Playbooks"
BANNER = "Researched public best practice, dated; your course material always comes first."
DISCLAIMER = ("Researched from public sources and written in our own words. Dated; check the sources before acting. "
              "Not the programme's course material.")
CONFIDENCE = ("high", "medium", "low")

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,95}$")
_DATE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
_AS_OF = re.compile(r"^\d{4}-(0[1-9]|1[0-2])(-(0[1-9]|[12]\d|3[01]))?$")
_URL = re.compile(r"^https?://[^\s<>\"']+$", re.I)
_TRUE = {"1", "true", "on", "yes"}

# relevance gate for agents: an entry must hold most of the question's weighted terms and match strongly
# (tuned on tests/not_covered_questions.json and in-scope playbook questions, see tests/test_playbooks.py)
MIN_MATCH = 0.6
MIN_BM25 = 4.0
KEEP_FRACTION = 0.4


@dataclass(frozen=True)
class Entry:
    id: str
    domain: str
    domain_title: str
    researched: str
    title: str
    category: str
    guidance: str
    as_of: str
    confidence: str
    steps: tuple[str, ...] = ()
    do: tuple[str, ...] = ()
    dont: tuple[str, ...] = ()
    benchmarks: tuple[dict, ...] = ()
    sources: tuple[dict, ...] = ()
    original_id: str = ""

    def index_text(self) -> str:
        # the title is repeated once (the index adds it again) so an entry is found by what it is about
        bits = [self.title, self.category.replace("-", " "), self.guidance, *self.steps, *self.do, *self.dont]
        bits += [f"{b['metric']}: {b['value']}" + (f" ({b['context']})" if b.get("context") else "") for b in self.benchmarks]
        return "\n".join(bits)

    def summary(self) -> dict:
        return {"id": self.id, "title": self.title, "category": self.category, "domain": self.domain,
                "domain_title": self.domain_title, "as_of": self.as_of, "confidence": self.confidence,
                "snippet": _clip(self.guidance, 220), "n_sources": len(self.sources), "n_benchmarks": len(self.benchmarks)}

    def to_dict(self) -> dict:
        return {**self.summary(), "researched": self.researched, "guidance": self.guidance, "steps": list(self.steps),
                "do": list(self.do), "dont": list(self.dont), "benchmarks": [dict(b) for b in self.benchmarks],
                "sources": [dict(s) for s in self.sources], "external": True, "library": LIBRARY_NAME,
                "disclaimer": DISCLAIMER}


@dataclass
class Domain:
    domain: str
    title: str
    researched: str
    note: str
    open_questions: list[str]
    file: str
    entry_ids: list[str] = field(default_factory=list)


class Library:
    def __init__(self, root: Path, domains: dict[str, Domain], entries: dict[str, Entry], problems: list[str], key: str):
        self.root = root
        self.domains = domains
        self.entries = entries
        self.problems = problems
        self.key = key
        self._index: Index | None = None
        self._lock = threading.Lock()

    @property
    def order(self) -> list[str]:
        return [eid for d in self.domains.values() for eid in d.entry_ids]

    @property
    def index(self) -> Index:
        with self._lock:
            if self._index is None or self._index.embedder_name != embeddings.get().name:
                self._index = _build_index(self)
            return self._index


# --------------------------------------------------------------------------- validation
def _clip(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rsplit(" ", 1)[0] + "…"


def _str_list(v, where: str, name: str, problems: list[str]) -> tuple[str, ...]:
    if v is None:
        return ()
    if not isinstance(v, list):
        problems.append(f"{where}: '{name}' must be a list; ignored")
        return ()
    out = []
    for x in v:
        if isinstance(x, str) and x.strip():
            out.append(x.strip())
        else:
            problems.append(f"{where}: a '{name}' item is not text; ignored")
    return tuple(out)


def _benchmarks(v, where: str, problems: list[str]) -> tuple[dict, ...]:
    if v is None:
        return ()
    if not isinstance(v, list):
        problems.append(f"{where}: 'benchmarks' must be a list; ignored")
        return ()
    out = []
    for b in v:
        if not isinstance(b, dict) or not str(b.get("metric", "")).strip() or not str(b.get("value", "")).strip():
            problems.append(f"{where}: a benchmark has no metric/value; ignored")
            continue
        out.append({"metric": str(b["metric"]).strip(), "value": str(b["value"]).strip(),
                    "context": str(b.get("context") or "").strip()})
    return tuple(out)


def _sources(v, where: str, problems: list[str]) -> tuple[dict, ...]:
    if not isinstance(v, list):
        return ()
    out = []
    for s in v:
        if not isinstance(s, dict):
            problems.append(f"{where}: a source is not an object; ignored")
            continue
        url = str(s.get("url") or "").strip()
        title = str(s.get("title") or "").strip()
        if not title or not _URL.match(url):
            problems.append(f"{where}: source '{title[:40] or '?'}' has no title or a non-http(s) link; ignored")
            continue
        out.append({"title": title, "url": url, "publisher": str(s.get("publisher") or "").strip(),
                    "date": str(s.get("date") or "").strip()})
    return tuple(out)


def validate_entry(raw, domain: str, domain_title: str, researched: str, where: str,
                   problems: list[str]) -> Entry | None:
    """One entry, or None (with the reason in problems) when it can't be used safely."""
    if not isinstance(raw, dict):
        problems.append(f"{where}: entry is not an object; skipped")
        return None
    eid = str(raw.get("id") or "").strip()
    where = f"{where}/{eid or '?'}"
    if not _SLUG.match(eid):
        problems.append(f"{where}: id must be a lowercase slug; skipped")
        return None
    for k in ("title", "category", "guidance"):
        if not isinstance(raw.get(k), str) or not raw[k].strip():
            problems.append(f"{where}: missing '{k}'; skipped")
            return None
    as_of = str(raw.get("as_of") or "").strip()
    if not _AS_OF.match(as_of):
        problems.append(f"{where}: 'as_of' must be YYYY-MM or YYYY-MM-DD; skipped")
        return None
    conf = str(raw.get("confidence") or "").strip().lower()
    if conf not in CONFIDENCE:
        problems.append(f"{where}: 'confidence' must be high, medium or low; skipped")
        return None
    sources = _sources(raw.get("sources"), where, problems)
    if not sources:
        problems.append(f"{where}: no usable source; skipped (every entry must be sourced)")
        return None
    return Entry(id=eid, domain=domain, domain_title=domain_title, researched=researched, title=raw["title"].strip(),
                 category=raw["category"].strip(), guidance=raw["guidance"].strip(), as_of=as_of, confidence=conf,
                 steps=_str_list(raw.get("steps"), where, "steps", problems),
                 do=_str_list(raw.get("do"), where, "do", problems),
                 dont=_str_list(raw.get("dont"), where, "dont", problems),
                 benchmarks=_benchmarks(raw.get("benchmarks"), where, problems), sources=sources, original_id=eid)


def validate_file(data, name: str, problems: list[str]) -> tuple[Domain, list[Entry]] | None:
    if not isinstance(data, dict):
        problems.append(f"{name}: not a JSON object; skipped")
        return None
    domain = str(data.get("domain") or "").strip()
    title = str(data.get("title") or "").strip()
    researched = str(data.get("researched") or "").strip()
    if not _SLUG.match(domain):
        problems.append(f"{name}: 'domain' must be a lowercase slug; skipped")
        return None
    if not title:
        problems.append(f"{name}: missing 'title'; skipped")
        return None
    if not _DATE.match(researched):
        problems.append(f"{name}: 'researched' must be YYYY-MM-DD; skipped")
        return None
    if not isinstance(data.get("entries"), list):
        problems.append(f"{name}: 'entries' must be a list; skipped")
        return None
    if Path(name).stem != domain:
        problems.append(f"{name}: file name differs from domain '{domain}' (loaded as '{domain}')")
    oq = _str_list(data.get("open_questions"), name, "open_questions", problems)
    note = str(data.get("note") or "").strip() or DISCLAIMER
    entries = [e for i, raw in enumerate(data["entries"])
               if (e := validate_entry(raw, domain, title, researched, f"{name}#{i + 1}", problems)) is not None]
    return Domain(domain=domain, title=title, researched=researched, note=note, open_questions=list(oq), file=name), entries


def _fingerprint(root: Path) -> str:
    return corpus.fingerprint(root) if root.exists() else "missing"


_cache: dict[str, Library] = {}
_cache_lock = threading.Lock()


def load(root: Path | None = None) -> Library:
    """Load + validate every playbook file (cached until a file changes)."""
    root = Path(root or ROOT)
    key = f"{root}|{_fingerprint(root)}"
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    problems: list[str] = []
    domains: dict[str, Domain] = {}
    entries: dict[str, Entry] = {}
    files = sorted(root.glob("*.json")) if root.exists() else []
    if not files:
        problems.append(f"no playbook files in {root.name}/")
    for p in files:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            problems.append(f"{p.name}: unreadable JSON ({type(e).__name__}); skipped")
            continue
        res = validate_file(data, p.name, problems)
        if res is None:
            continue
        dom, items = res
        if dom.domain in domains:
            problems.append(f"{p.name}: domain '{dom.domain}' already loaded from {domains[dom.domain].file}; skipped")
            continue
        for e in items:
            eid = e.id
            if eid in entries:
                # ids must be unique across the library: keep the first, qualify the repeat with its domain
                eid = f"{e.domain}--{e.id}"
                problems.append(f"{p.name}: id '{e.id}' already used in {entries[e.id].domain}; loaded as '{eid}'")
                if eid in entries:
                    problems.append(f"{p.name}: id '{eid}' repeated; skipped")
                    continue
                e = replace(e, id=eid)
            entries[eid] = e
            dom.entry_ids.append(eid)
        domains[dom.domain] = dom
    lib = Library(root, domains, entries, problems, key)
    if problems:
        log.info("playbooks: %d note(s) while loading", len(problems))
    with _cache_lock:
        _cache[key] = lib
    return lib


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


# --------------------------------------------------------------------------- index
def _build_index(lib: Library) -> Index:
    """One chunk per entry, kind 'playbook'; vectors cached on disk like the course index."""
    docs, chunks = [], []
    for n, eid in enumerate(lib.order):
        e = lib.entries[eid]
        text = e.index_text()
        lines = text.split("\n")
        file = f"playbooks/{eid}"
        docs.append(corpus.Doc(file=file, kind="playbook", title=e.title, lines=lines, licence_status="third_party",
                               provenance="researched_public", corpus_id="playbooks"))
        chunks.append(corpus.Chunk(id=n, file=file, title=e.title, kind="playbook", start=1, end=len(lines), text=text,
                                   ts_start=None, ts_end=None, quality="ok", weight=1.0, licence_status="third_party",
                                   provenance="researched_public", corpus_id="playbooks"))
    emb = embeddings.get()
    fp = lib.key.rsplit("|", 1)[-1]
    cache = config.get().index_dir / f"playbooks-{fp}-{emb.name}"
    vecs = None
    try:
        if (cache / "vectors.npy").exists() and json.loads((cache / "meta.json").read_text()).get("n") == len(chunks):
            vecs = np.load(cache / "vectors.npy")
    except (OSError, ValueError):
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
    return Index(f"playbooks-{fp}", docs, chunks, [], vecs, emb.name)


# --------------------------------------------------------------------------- switch
def enabled(tenant) -> bool:
    """The tenant's third_party_kb switch. Unset -> the server default (off unless
    OPERATOR_THIRD_PARTY_KB=1). Strings like "false" are read as off."""
    v = tenant.get(SWITCH)
    if v is None:
        return bool(config.get().third_party_kb)
    if isinstance(v, str):
        return v.strip().lower() in _TRUE
    if isinstance(v, (list, dict)):
        return False
    return bool(v)


# --------------------------------------------------------------------------- public API
def _names(tenant) -> list[str]:
    return guardrails.tenant_banned_names(tenant) if tenant is not None else []


def _scrub(obj, names: list[str]):
    if not names:
        return obj
    if isinstance(obj, str):
        return guardrails.scrub(obj, names)
    if isinstance(obj, list):
        return [_scrub(x, names) for x in obj]
    if isinstance(obj, dict):
        return {k: _scrub(v, names) for k, v in obj.items()}
    return obj


def cite(e: Entry, xid: str, score: float | None = None) -> dict:
    """The external citation shape: never carries a course file path, always kind 'external'."""
    out = {"id": xid, "entry_id": e.id, "domain": e.domain, "domain_title": e.domain_title, "title": e.title,
           "category": e.category, "as_of": e.as_of, "researched": e.researched, "confidence": e.confidence,
           "snippet": _clip(e.guidance, 300), "sources": [dict(s) for s in e.sources], "url": e.sources[0]["url"],
           "file": f"playbooks/{e.id}", "kind": "external", "library": LIBRARY_NAME}
    if score is not None:
        out["score"] = round(score, 5)
    return out


def _hits(query: str, k: int, lib: Library) -> list[tuple[Entry, dict]]:
    q = (query or "").strip()
    if not q or not lib.entries:
        return []
    idx = lib.index
    out = []
    for h in idx.search(q, k=max(k * 3, 12)):
        if h["bm25"] <= 0:  # dense-only matches are noise for a keyword library
            continue
        out.append((lib.entries[h["chunk"].file.split("/", 1)[1]], h))
    return out


def search(tenant, query: str, k: int = 8, prefix: str = "X", respect_switch: bool = True,
           domain: str | None = None) -> list[dict]:
    """Ranked entries as citations X1..Xn. Empty while the tenant's switch is off (unless the caller
    is the owner previewing with respect_switch=False)."""
    if respect_switch and (tenant is None or not enabled(tenant)):
        return []
    lib = load()
    k = max(1, min(int(k), 30))
    hits = _hits(query, k if not domain else 30, lib)
    if domain:
        hits = [(e, h) for e, h in hits if e.domain == domain]
    names = _names(tenant)
    return [_scrub({**cite(e, f"{prefix}{n}", h["score"]), "bm25": round(h["bm25"], 3)}, names)
            for n, (e, h) in enumerate(hits[:k], start=1)]


def _term_weight(idx: Index, t: str, unseen: float) -> float:
    """Common words barely count; a word the library never uses counts as much as its rarest word."""
    if t in textnorm.COMMON:
        return 0.3
    syn = textnorm._syn()
    if t not in idx.df and not any(s in idx.df for s in syn.get(t, [])):
        return unseen
    return max(idx.idf(t), 0.8)


def match_fraction(idx: Index, query: str, chunk_id: int) -> tuple[float, list[str]]:
    """The idf-weighted share of the question's terms that this one entry actually contains."""
    toks = [t for t in dict.fromkeys(textnorm.tokens(query)) if len(t) > 1]
    if not toks or not idx.N:
        return 0.0, toks
    unseen = getattr(idx, "_max_idf", None)
    if unseen is None:  # computed once per index
        unseen = idx._max_idf = max((idx.idf(t) for t in idx.df), default=1.0)
    syn = textnorm._syn()
    have = idx.tfs[chunk_id]
    total = got = 0.0
    missing = []
    for t in toks:
        w = _term_weight(idx, t, unseen)
        total += w
        if t in have or any(s in have for s in syn.get(t, [])):
            got += w
        else:
            missing.append(t)
    return (got / total if total else 0.0), missing


def judge(query: str, lib: Library | None = None, k: int = 4) -> dict:
    """Does the library really answer this question? An entry qualifies when it holds most of the
    question's weighted terms (MIN_MATCH) and matches strongly (MIN_BM25). Ranked by bm25 x match."""
    lib = lib or load()
    if not lib.entries or not (query or "").strip():
        return {"covered": False, "matches": [], "best_match": 0.0, "top_bm25": 0.0}
    idx = lib.index
    rows = []
    for e, h in _hits(query, max(k, 6), lib):
        frac, missing = match_fraction(idx, query, h["chunk"].id)
        rows.append({"entry": e, "hit": h, "match": frac, "missing": missing})
    good = [r for r in rows if r["match"] >= MIN_MATCH and r["hit"]["bm25"] >= MIN_BM25]
    if good:
        top = max(r["hit"]["bm25"] for r in good)
        good = [r for r in good if r["hit"]["bm25"] >= top * KEEP_FRACTION]
    good.sort(key=lambda r: -(r["hit"]["score"] * r["match"]))
    return {"covered": bool(good), "matches": good[:k], "best_match": round(max((r["match"] for r in rows), default=0.0), 3),
            "top_bm25": round(max((r["hit"]["bm25"] for r in rows), default=0.0), 3)}


def relevant(tenant, parts: list[str], k: int = 4, prefix: str = "X") -> list[dict]:
    """Entries that genuinely answer one of the question parts (for agents). Respects the switch.
    Merged across parts with stable X numbering, strongest first, at most k."""
    if tenant is None or not enabled(tenant):
        return []
    lib = load()
    best: dict[str, tuple[float, Entry, dict, float]] = {}
    for part in parts:
        for r in judge(part, lib, k)["matches"]:
            e, h = r["entry"], r["hit"]
            strength = h["score"] * r["match"]
            if e.id not in best or strength > best[e.id][0]:
                best[e.id] = (strength, e, h, r["match"])
    ranked = sorted(best.values(), key=lambda x: -x[0])[:k]
    names = _names(tenant)
    return [_scrub({**cite(e, f"{prefix}{n}", h["score"]), "bm25": round(h["bm25"], 3), "match": round(m, 3)}, names)
            for n, (_, e, h, m) in enumerate(ranked, start=1)]


def get(entry_id: str, tenant=None) -> dict | None:
    e = load().entries.get(str(entry_id or ""))
    if e is None:
        return None
    return _scrub(e.to_dict(), _names(tenant))


def entry(entry_id: str) -> Entry | None:
    return load().entries.get(str(entry_id or ""))


def domains(tenant=None) -> list[dict]:
    lib = load()
    out = []
    for d in lib.domains.values():
        items = [lib.entries[i] for i in d.entry_ids]
        conf = {c: sum(1 for e in items if e.confidence == c) for c in CONFIDENCE}
        as_of = sorted({e.as_of for e in items})
        out.append({"domain": d.domain, "title": d.title, "researched": d.researched, "count": len(items),
                    "categories": sorted({e.category for e in items}), "confidence": conf,
                    "as_of_range": [as_of[0], as_of[-1]] if as_of else [], "note": d.note,
                    "open_questions": len(d.open_questions)})
    return _scrub(out, _names(tenant))


def domain_detail(domain: str, tenant=None) -> dict | None:
    lib = load()
    d = lib.domains.get(str(domain or ""))
    if d is None:
        return None
    base = next(x for x in domains(None) if x["domain"] == d.domain)
    return _scrub({**base, "open_questions": list(d.open_questions),
                   "entries": [lib.entries[i].summary() for i in d.entry_ids]}, _names(tenant))


def overview(tenant=None) -> dict:
    lib = load()
    ds = domains(tenant)
    researched = sorted({d["researched"] for d in ds})
    return {"library": LIBRARY_NAME, "banner": BANNER, "disclaimer": DISCLAIMER, "switch": SWITCH,
            "enabled": enabled(tenant) if tenant is not None else False, "domains": ds,
            "total": sum(d["count"] for d in ds), "researched": researched,
            "latest_researched": researched[-1] if researched else None,
            "confidence": {c: sum(d["confidence"][c] for d in ds) for c in CONFIDENCE},
            "problems": list(lib.problems)}


# --------------------------------------------------------------------------- answer helpers (agents)
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9'\"(])")


def sentences(e: Entry) -> list[tuple[str, str]]:
    """Candidate statements from an entry, labelled by where they came from."""
    out: list[tuple[str, str]] = []
    for s in _SENT.split(e.guidance):
        s = s.strip()
        if len(s.split()) >= 6:
            out.append(("guidance", s))
    for s in e.steps:
        out.append(("step", s))
    for s in e.do:
        out.append(("do", s))
    return out


def as_of_label(dates: list[str]) -> str:
    ds = sorted({d for d in dates if d})
    if not ds:
        return "undated"
    return ds[0] if len(ds) == 1 else f"{ds[0]} to {ds[-1]}"
