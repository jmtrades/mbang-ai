"""Offline style mining (numpy only, no downloads, no AI).

Input: lines the subject said (attribution.py) from licensed documents, minus content-policy and owner exclusions,
guardrail-flagged lines, garbled documents, and lines that contain names, numbers or money.
Background: other speakers' lines from the same calls (topic-matched) plus the tenant's written SOPs.

Signature phrases: 1-4-grams within sentences (stopwords kept) and short repeated sentences, scored with weighted
log-odds under an informative Dirichlet prior (Monroe, Colaresi & Quinn, "Fightin' Words"). Kept when z > 3,
count >= 5, in >= 3 documents and z > 3 in >= 70% of 20 document-bootstrap resamples. Topical phrases (content words
above the 75th idf percentile or programme compounds), names, numbers and anything that trips the guardrails are
dropped. Each phrase gets a position class (opener, closer, transition, tag or signature) from P(position | phrase).

Also: stylometric targets and word preferences. Disfluency is measured but never reproduced. Every phrase stores its
evidence (file, lines, count, z). The result is a versioned draft profile whose phrases wait for the subject's
approve / reject / never-use decision. A draft needs 5,000 attributed words; it is "stable" from 20,000.
"""
from __future__ import annotations

import hashlib
import re
import statistics
from collections import Counter, defaultdict

import numpy as np

from .. import audit, content_policy, corpus, db, guardrails, index, runtime, textnorm
from . import attribution, consent

PLAN = ["Collect attributed lines", "Count phrases", "Weighted log-odds vs background", "Bootstrap stability",
        "Drop topical, names, numbers, guardrail hits", "Stylometrics and word preferences", "Save draft profile"]
N_MAX = 4
LINE_MAX = 8
MIN_COUNT = 5
MIN_DOCS = 3
Z_MIN = 3.0
BOOTSTRAPS = 20
STABILITY = 0.7
EDGE = 8
MIN_BACKGROUND = 1500
MAX_PHRASES = 40

FUNCTION_WORDS = """the a an and or but if of to in on at for from by with about into over under is are was were be been
being do does did have has had i you he she it we they me him her us them my your his its our their this that these those
what which who when where why how all any both each more most other some such no not only so than too very can will just
should could would may might must shall now then there here also up down out off again once like well really just
because while until as after before still even never always often maybe quite much many few every own same let's i'm
you're it's don't that's we're they're can't won't isn't""".split()
FUNCTION_SET = set(FUNCTION_WORDS)
TRIVIAL = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "is", "it", "that", "this", "for", "with", "be", "as",
           "at", "by", "are", "was"}
CERTAIN = {"always", "never", "definitely", "clearly", "must", "every", "exactly", "certainly", "obviously", "simply"}
HEDGE = {"maybe", "might", "perhaps", "probably", "could", "usually", "often", "possibly", "sometimes", "seems"}
DISFLUENT = {"um", "uh", "er", "erm", "uhh", "umm", "hmm"}
IMPERATIVE = {"ask", "write", "send", "pick", "track", "read", "stop", "start", "keep", "make", "do", "don't", "go", "try",
              "use", "be", "show", "spend", "plan", "find", "change", "cut", "let's", "focus", "get", "take", "book",
              "confirm", "qualify", "follow", "listen", "never", "always", "check", "look", "think", "remember", "protect",
              "offer", "tell", "give", "build", "fix", "test", "write", "stay", "drop", "call"}
NUMBER_WORDS = {"zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
                "fifteen", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety", "hundred",
                "thousand", "million", "billion", "percent", "k", "grand", "dollars", "bucks", "half", "double", "triple"}
LEXICON_PAIRS = [("clients", "customers"), ("leads", "prospects"), ("book", "schedule"), ("call", "meeting"),
                 ("message", "dm"), ("offer", "package"), ("students", "members"), ("results", "outcomes"),
                 ("team", "staff"), ("price", "cost"), ("follow-up", "nudge"), ("reply", "response")]
NOT_NAMES = {"I", "I'm", "I've", "I'll", "I'd", "OK", "Okay", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
             "Saturday", "Sunday", "English"}
WORD = re.compile(r"[a-z]+(?:'[a-z]+)?")
SENT = re.compile(r"[^.!?]+[.!?]*")
_DIGIT = re.compile(r"\d|\$|£|€")
_CAP = re.compile(r"(?<![.!?]\s)(?<!^)\b([A-Z][a-z]+)\b")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("(DEMO)", " ")).strip()


def _sentences(text: str) -> list[tuple[str, str]]:
    out = []
    for m in SENT.finditer(text):
        s = m.group(0).strip()
        if s:
            out.append((s, s[-1] if s[-1] in ".!?" else ""))
    return out


def _words(s: str) -> list[str]:
    return WORD.findall(s.lower().replace("’", "'"))


def _has_number(text: str) -> bool:
    return bool(_DIGIT.search(text)) or any(w in NUMBER_WORDS for w in _words(text))


def _has_name(text: str, name_tokens: set[str]) -> bool:
    words = set(_words(text))
    if words & name_tokens:
        return True
    for s, _ in _sentences(text):
        for m in re.finditer(r"\b([A-Z][a-z]+)\b", s):
            if m.start() == 0 or m.group(1) in NOT_NAMES:
                continue
            return True
    return False


def _flagged(text: str) -> bool:
    return bool(content_policy.classify(text)) or bool(guardrails.check(text, "plain", {"prior_contact": True}))


# --------------------------------------------------------------------------- collection
def collect(tenant) -> dict:
    keys = attribution.subject_keys(tenant)
    if not keys:
        raise consent.PersonaError(409, "Choose the subject and their name first.")
    docs = attribution.source_docs(tenant)
    notes = attribution.note_speakers(tenant)
    idx = index.for_tenant(tenant)
    excluded = {(e.file, int(e.lines.split("-")[0])) for e in idx.excluded}
    name_tokens = set()
    for n in consent.names(tenant):
        name_tokens |= {w for w in _words(n) if len(w) > 1}
    turns: list[dict] = []          # subject turns: {file, lines: [(n, text)]}
    background: list[tuple[str, str]] = []  # (file, text)
    dropped = Counter()
    for d in docs:
        if d.quality == "garbled":
            dropped["garbled document lines"] += len(d.lines)
            continue
        written = d.kind in ("sop", "pdf_sop") and not notes.get(d.file)
        sp = [None] * len(d.lines) if written else attribution.doc_speakers(d, notes.get(d.file))
        doc_names = set(name_tokens)
        for s in {x for x in sp if x}:
            doc_names |= {w for w in _words(s.replace("(DEMO)", "")) if len(w) > 1 and w not in ("student", "coach")}
        cur = None
        for i, (ln, s) in enumerate(zip(d.lines, sp), start=1):
            text = _clean(corpus.speaker_text(ln))
            if not text:
                cur = None
                continue
            if (d.file, i) in excluded or _flagged(text):
                dropped["content policy or guardrails"] += 1
                cur = None
                continue
            if _has_number(text):
                dropped["numbers or money"] += 1
                cur = None
                continue
            if _has_name(text, doc_names):
                dropped["names"] += 1
                cur = None
                continue
            if written:
                background.append((d.file, text))
                continue
            if s is None:
                dropped["unattributed"] += 1
                cur = None
                continue
            if corpus.speaker_key(s) in keys:
                if cur is None or cur["file"] != d.file or cur["last"] != i - 1:
                    cur = {"file": d.file, "lines": [], "last": i}
                    turns.append(cur)
                cur["lines"].append((i, text))
                cur["last"] = i
            else:
                background.append((d.file, text))
                cur = None
    words = sum(len(_words(t)) for tr in turns for _, t in tr["lines"])
    bg_words = sum(len(_words(t)) for _, t in background)
    return {"turns": turns, "background": background, "words": words, "background_words": bg_words,
            "dropped": dict(dropped), "files": sorted({t["file"] for t in turns}), "name_tokens": sorted(name_tokens)}


# --------------------------------------------------------------------------- AI style excerpts
def excerpt_candidates(collected: dict) -> list[str]:
    """Subject turns of 15-80 words (names, numbers and flagged lines already removed by collect())."""
    out = []
    for t in collected["turns"]:
        text = " ".join(x for _, x in t["lines"])
        if 15 <= len(text.split()) <= 80:
            out.append(text)
    return out


def excerpt_key(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- counting
def _turn_units(turn: dict) -> list[dict]:
    """Sentences of one turn with their words, end punctuation, line number and turn offsets."""
    units, off = [], 0
    for n, text in turn["lines"]:
        for s, end in _sentences(text):
            w = _words(s)
            if not w:
                continue
            units.append({"line": n, "text": s, "end": end, "words": w, "off": off})
            off += len(w)
    for u in units:
        u["turn_len"] = off
    return units


def _count(collected: dict) -> dict:
    subj = Counter()                       # phrase -> count (subject)
    per_doc: dict[str, Counter] = defaultdict(Counter)
    doc_tot: dict[str, Counter] = defaultdict(Counter)  # file -> order -> total units
    pos: dict[tuple, Counter] = defaultdict(Counter)
    evid: dict[tuple, list] = defaultdict(list)
    display: dict[tuple, str] = {}
    for turn in collected["turns"]:
        units = _turn_units(turn)
        for si, u in enumerate(units):
            w, L = u["words"], u["turn_len"]
            for n in range(1, N_MAX + 1):
                for i in range(len(w) - n + 1):
                    g = ("g", tuple(w[i:i + n]))
                    subj[g] += 1
                    per_doc[turn["file"]][g] += 1
                    p = u["off"] + i
                    if p < EDGE:
                        pos[g]["opener"] += 1
                    if p + n > L - EDGE:
                        pos[g]["closer"] += 1
                    if i == 0 and si > 0:
                        pos[g]["transition"] += 1
                    if i + n == len(w) and u["end"] == "?" and len(w) <= 4:
                        pos[g]["tag"] += 1
                    pos[g]["all"] += 1
                    if len(evid[g]) < 40:
                        evid[g].append((turn["file"], u["line"], u["text"]))
                doc_tot[turn["file"]][n] += max(0, len(w) - n + 1)
            if len(w) <= LINE_MAX:
                g = ("s", tuple(w))
                subj[g] += 1
                per_doc[turn["file"]][g] += 1
                display.setdefault(g, u["text"])
                if si == 0:
                    pos[g]["opener"] += 1
                if si == len(units) - 1:
                    pos[g]["tag" if u["end"] == "?" else "closer"] += 1
                if 0 < si < len(units) - 1:
                    pos[g]["transition"] += 1
                pos[g]["all"] += 1
                if len(evid[g]) < 40:
                    evid[g].append((turn["file"], u["line"], u["text"]))
            doc_tot[turn["file"]]["s"] += 1
    bg = Counter()
    bg_tot = Counter()
    for _, text in collected["background"]:
        for s, _ in _sentences(text):
            w = _words(s)
            if not w:
                continue
            for n in range(1, N_MAX + 1):
                for i in range(len(w) - n + 1):
                    bg[("g", tuple(w[i:i + n]))] += 1
                bg_tot[n] += max(0, len(w) - n + 1)
            if len(w) <= LINE_MAX:
                bg[("s", tuple(w))] += 1
            bg_tot["s"] += 1
    return {"subj": subj, "per_doc": per_doc, "doc_tot": doc_tot, "pos": pos, "evid": evid, "bg": bg, "bg_tot": bg_tot,
            "display": display}


def _order(g: tuple) -> object:
    return "s" if g[0] == "s" else len(g[1])


def logodds(ys, yb, ns, nb):
    """Weighted log-odds with an informative Dirichlet prior (pooled frequencies, alpha0 = min(n_s + n_b, 500))."""
    ys, yb, ns, nb = (np.asarray(x, dtype=np.float64) for x in (ys, yb, ns, nb))
    a0 = np.minimum(ns + nb, 500.0)
    a = a0 * (ys + yb) / np.maximum(ns + nb, 1.0)
    a = np.maximum(a, 1e-3)
    d = np.log((ys + a) / np.maximum(ns + a0 - ys - a, 1e-9)) - np.log((yb + a) / np.maximum(nb + a0 - yb - a, 1e-9))
    var = 1.0 / (ys + a) + 1.0 / (yb + a)
    return d / np.sqrt(var)


# --------------------------------------------------------------------------- filters
def _idf_threshold(collected: dict) -> tuple[dict[str, float], float]:
    df = Counter()
    groups: dict[str, set[str]] = defaultdict(set)
    for t in collected["turns"]:
        for _, text in t["lines"]:
            groups[t["file"]] |= set(_words(text))
    for f, text in collected["background"]:
        groups["bg:" + f] |= set(_words(text))
    for ws in groups.values():
        df.update(ws)
    N = max(len(groups), 1)
    idf = {w: float(np.log((N + 1) / (c + 0.5))) for w, c in df.items()}
    content = [v for w, v in idf.items() if w not in textnorm.STOP and w not in FUNCTION_SET]
    p75 = float(np.percentile(content, 75)) if content else 99.0
    return idf, p75


FRAGMENT_END = {"and", "or", "but", "the", "a", "an", "to", "of", "in", "on", "for", "with", "so", "that", "is", "it",
                "your", "my", "our", "their", "this", "you're", "be", "at", "by", "as", "if"}
FRAGMENT_START = {"and", "or", "but", "of", "to", "it", "is", "be", "s", "t"}


def _drop_reason(words: tuple, idf: dict, p75: float, name_tokens: set[str], kind: str = "g") -> str | None:
    phrase = " ".join(words)
    if len(words) == 1 and (words[0] in FUNCTION_SET or words[0] in textnorm.STOP or len(words[0]) < 4) and kind == "g":
        return "function word"
    if all(w in TRIVIAL for w in words):
        return "function words only"
    if kind == "g" and len(words) >= 2 and (words[-1] in FRAGMENT_END or words[0] in FRAGMENT_START):
        return "fragment"
    if set(words) & name_tokens:
        return "name"
    if any(w in NUMBER_WORDS for w in words):
        return "number"
    if textnorm.fold(phrase).split() != phrase.split():
        return "programme term"
    for w in words:
        if w not in textnorm.STOP and w not in FUNCTION_SET and idf.get(w, 0.0) > p75:
            return "topical"
    if _flagged(phrase) or guardrails.ai_words(phrase):
        return "guardrails"
    return None


def _display(g: tuple, counted: dict) -> str:
    if g[0] == "s":
        return counted["display"].get(g, " ".join(g[1]))
    words = g[1]
    pat = re.compile(r"\b" + r"[\s,;:\-–—]+".join(re.escape(w) for w in words) + r"\b", re.I)
    for _, _, text in counted["evid"].get(g, []):
        m = pat.search(text.replace("’", "'"))
        if m:
            return m.group(0)
    return " ".join(words)


def _classify(g: tuple, counted: dict) -> tuple[str, dict]:
    p = counted["pos"][g]
    total = max(p["all"], 1)
    shares = {k: round(p[k] / total, 3) for k in ("opener", "closer", "transition", "tag")}
    for k in ("tag", "opener", "closer", "transition"):
        if shares[k] >= 0.5 and not (k in ("opener", "closer") and shares["opener"] >= 0.5 and shares["closer"] >= 0.5
                                     and g[0] != "s"):
            return k, shares
    return "signature", shares


# --------------------------------------------------------------------------- stylometrics
def stylometrics(collected: dict) -> dict:
    sents, turn_lens, words_all = [], [], []
    q = ex = imp = 0
    for t in collected["turns"]:
        tl = 0
        for _, text in t["lines"]:
            for s, end in _sentences(text):
                w = _words(s)
                if not w:
                    continue
                sents.append(len(w))
                words_all += w
                tl += len(w)
                q += end == "?"
                ex += end == "!"
                imp += w[0] in IMPERATIVE
        turn_lens.append(tl)
    nw = max(len(words_all), 1)
    ns = max(len(sents), 1)
    c = Counter(words_all)
    you = sum(c[w] for w in ("you", "your", "you're", "yours", "yourself", "you've", "you'll"))
    we = sum(c[w] for w in ("we", "us", "our", "we're", "let's", "ours"))
    i_ = sum(c[w] for w in ("i", "me", "my", "i'm", "i've", "mine"))
    certain = sum(c[w] for w in CERTAIN)
    hedge = sum(c[w] for w in HEDGE)
    win = 50
    if len(words_all) > win:
        ttrs = [len(set(words_all[i:i + win])) / win for i in range(0, len(words_all) - win, 10)]
        mattr = float(np.mean(ttrs))
    else:
        mattr = len(set(words_all)) / nw
    return {
        "sentence_words_mean": round(float(np.mean(sents)) if sents else 0.0, 1),
        "sentence_words_median": round(float(statistics.median(sents)) if sents else 0.0, 1),
        "sentence_words_p90": round(float(np.percentile(sents, 90)) if sents else 0.0, 1),
        "turn_words_mean": round(float(np.mean(turn_lens)) if turn_lens else 0.0, 1),
        "question_rate": round(q / ns, 3), "exclamation_rate": round(ex / ns, 3), "imperative_rate": round(imp / ns, 3),
        "you_per_100": round(100 * you / nw, 2), "we_i_ratio": round(we / max(i_, 1), 2),
        "contraction_rate": round(sum(v for w, v in c.items() if "'" in w) / nw, 3), "mattr": round(mattr, 3),
        "certainty_hedge_ratio": round(certain / max(hedge, 1), 2),
        "disfluency_per_100": round(100 * sum(c[w] for w in DISFLUENT) / nw, 2),  # measured, never reproduced
        "function_words": {w: round(1000 * c[w] / nw, 2) for w in FUNCTION_WORDS[:100]},
    }


def lexicon(collected: dict) -> list[dict]:
    c = Counter()
    where: dict[str, dict[str, dict]] = defaultdict(dict)  # word -> file -> {count, first line}
    for t in collected["turns"]:
        for n, text in t["lines"]:
            ws = _words(text.replace("-", ""))
            c.update(ws)
            for w in ws:
                e = where[w].setdefault(t["file"], {"file": t["file"], "lines": f"{n}-{n}", "count": 0})
                e["count"] += 1

    def _evidence(word: str) -> list[dict]:
        rows: dict[str, dict] = {}
        for w in (word, word + "s"):
            for f, e in where.get(w, {}).items():
                r = rows.setdefault(f, {**e, "count": 0})
                r["count"] += e["count"]
        return sorted(rows.values(), key=lambda e: -e["count"])[:3]

    out = []
    # curated true synonyms only: textnorm.SYNONYMS is a retrieval expansion list (reminder ~ ladder), not word choices.
    # Both variants must appear at least once, so "prefers X" means he picked X over a word he also uses.
    for a, b in LEXICON_PAIRS:
        a_, b_ = a.replace("-", ""), b.replace("-", "")
        na = c[a_] + c[a_ + "s"]
        nb = c[b_] + c[b_ + "s"]
        n = na + nb
        if n >= 10 and na and nb:
            if na / n >= 0.7:
                out.append({"prefer": a, "over": b, "share": round(na / n, 2), "n": n, "evidence": _evidence(a_)})
            elif nb / n >= 0.7:
                out.append({"prefer": b, "over": a, "share": round(nb / n, 2), "n": n, "evidence": _evidence(b_)})
    return out


# --------------------------------------------------------------------------- the miner
def analyse(collected: dict, seed: int = 7) -> dict:
    counted = _count(collected)
    subj, bg, per_doc, doc_tot, bg_tot = counted["subj"], counted["bg"], counted["per_doc"], counted["doc_tot"], counted["bg_tot"]
    files = sorted(per_doc)
    sub_tot = Counter()
    for f in files:
        sub_tot.update(doc_tot[f])
    doc_freq = Counter()
    for f in files:
        doc_freq.update(set(per_doc[f]))
    idf, p75 = _idf_threshold(collected)
    name_tokens = set(collected["name_tokens"])
    cands =[g for g, n in subj.items() if n >= MIN_COUNT and doc_freq[g] >= MIN_DOCS]
    report = {"candidates": len(cands), "dropped": Counter()}
    if not cands or collected["background_words"] < MIN_BACKGROUND:
        report["note"] = ("Not enough other-speaker or written material to tell style from topic, so no phrases were "
                          "proposed." if collected["background_words"] < MIN_BACKGROUND else "No repeated phrases yet.")
        return {"phrases": [], "report": report, "counted": counted}
    ys = np.array([subj[g] for g in cands], dtype=np.float64)
    yb = np.array([bg.get(g, 0) for g in cands], dtype=np.float64)
    ns = np.array([sub_tot[_order(g)] for g in cands], dtype=np.float64)
    nb = np.array([bg_tot[_order(g)] for g in cands], dtype=np.float64)
    z = logodds(ys, yb, ns, nb)
    keep = [i for i in range(len(cands)) if z[i] > Z_MIN]
    report["z_pass"] = len(keep)
    if not keep:
        return {"phrases": [], "report": report, "counted": counted}
    kc = [cands[i] for i in keep]
    C = np.array([[per_doc[f].get(g, 0) for g in kc] for f in files], dtype=np.float64)
    orders = [_order(g) for g in kc]
    T = np.array([[doc_tot[f][o] for o in orders] for f in files], dtype=np.float64)
    rng = np.random.default_rng(seed)
    hits = np.zeros(len(kc))
    for _ in range(BOOTSTRAPS):
        pick = rng.integers(0, len(files), len(files))
        hits += logodds(C[pick].sum(0), yb[keep], T[pick].sum(0), nb[keep]) > Z_MIN
    stab = hits / BOOTSTRAPS
    out = []
    for j, g in enumerate(kc):
        if stab[j] < STABILITY:
            report["dropped"]["unstable"] += 1
            continue
        why = _drop_reason(g[1], idf, p75, name_tokens, g[0])
        if not why and g[0] == "g" and len(g[1]) == 1 and z[keep[j]] <= Z_MIN + 1:
            why = "single word below the stricter bar"
        if why:
            report["dropped"][why] += 1
            continue
        cls, shares = _classify(g, counted)
        ev: dict[str, dict] = {}
        for f, ln, text in counted["evid"][g]:
            e = ev.setdefault(f, {"file": f, "lines": f"{ln}-{ln}", "count": 0, "quote": text[:160]})
            e["count"] += 1
        evidence = sorted(ev.values(), key=lambda e: -e["count"])[:5]
        out.append({"phrase": " ".join(g[1]), "display": _display(g, counted), "kind": g[0], "cls": cls,
                    "n": len(g[1]), "z": round(float(z[keep[j]]), 2), "count": int(subj[g]), "docs": int(doc_freq[g]),
                    "stability": round(float(stab[j]), 2), "positions": shares, "evidence": evidence})
    # redundancy: a phrase inside a longer kept phrase with about the same count adds nothing
    out.sort(key=lambda p: (-p["n"], -p["z"]))
    kept: list[dict] = []
    for p in out:
        inner = f" {p['phrase']} "
        # a single word inside a kept phrase is that phrase's echo, whatever its count
        if any(inner in f" {k['phrase']} " and (p["n"] == 1 or p["count"] <= 1.25 * k["count"]) for k in kept):
            report["dropped"]["inside a longer phrase"] += 1
            continue
        kept.append(p)
    kept.sort(key=lambda p: -p["z"])
    report["dropped"] = dict(report["dropped"])
    return {"phrases": kept[:MAX_PHRASES], "report": report, "counted": counted}


def _save(tenant, collected: dict, result: dict, actor: str) -> dict:
    ident = consent.identity(tenant)
    words = collected["words"]
    maturity = "stable" if words >= attribution.STABLE_WORDS else "draft"
    profile = {"maturity": maturity, "words": words, "background_words": collected["background_words"],
               "files": len(collected["files"]), "dropped_lines": collected["dropped"],
               "stylometrics": stylometrics(collected), "lexicon": lexicon(collected), "report": result["report"],
               "thresholds": {"draft": attribution.DRAFT_WORDS, "stable": attribution.STABLE_WORDS},
               # fingerprints of the turns AI-mode excerpts may come from (only under external_ai_processing)
               "excerpt_keys": sorted({excerpt_key(x) for x in excerpt_candidates(collected)}),
               "demo": bool(tenant.is_demo)}
    now = db.now()
    with tenant.db() as c, db.tx(c):
        prev = {r["phrase"]: r["decision"] for r in c.execute(
            "SELECT phrase, decision FROM persona_phrases WHERE decision IN ('never_use','reject')")}
        v = (c.execute("SELECT MAX(version) FROM persona_profiles").fetchone()[0] or 0) + 1
        c.execute("UPDATE persona_profiles SET status='superseded', updated=? WHERE status IN ('draft','stable')", (now,))
        pid = c.execute("INSERT INTO persona_profiles(person,status,consent,profile,created,updated,version,profile_hash,"
                        "subject_user_id,words,demo) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (ident["name"], maturity, None, db.jdump(profile), now, now, v, "", ident["subject_user_id"], words,
                         1 if tenant.is_demo else 0)).lastrowid
        for p in result["phrases"]:
            dec = prev.get(p["phrase"], "pending")
            c.execute("INSERT INTO persona_phrases(profile_id,version,phrase,display,cls,n,z,count,docs,stability,positions,"
                      "evidence,decision,decided_by,decided_at,demo,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (pid, v, p["phrase"], p["display"], p["cls"], p["n"], p["z"], p["count"], p["docs"], p["stability"],
                       db.jdump(p["positions"]), db.jdump(p["evidence"]), dec, "carried over" if dec != "pending" else None,
                       now if dec != "pending" else None, 1 if tenant.is_demo else 0, now))
    h = consent.refresh_hash(tenant, pid)
    consent.cache_clear(tenant)
    audit.log(tenant, actor, "persona.mine", {"version": v, "words": words, "phrases": len(result["phrases"]),
                                              "maturity": maturity})
    return {"version": v, "profile_id": pid, "maturity": maturity, "words": words, "phrases": len(result["phrases"]),
            "profile_hash": h, "report": result["report"]}


_DEMO_MEMO: dict = {}


def mine(tenant, actor: str = "system") -> dict:
    """Build a new draft profile version. Raises PersonaError(409) on the fixture corpus outside the demo and when
    there are fewer than 5,000 attributed words."""
    if attribution.uses_fixture(tenant) and not attribution.source_docs(tenant):
        raise consent.PersonaError(409, "Load the owner's material first. The knowledge base is still the fictional "
                                        "sample corpus, so there is nothing of the subject's to learn from.")

    def go(run: runtime.Run):
        memo_key = None
        if tenant.is_demo:  # the demo material is fixed and fictional: mine it once per process
            memo_key = (tuple(consent.names(tenant)), index.for_tenant(tenant).key)
        hit = _DEMO_MEMO.get(memo_key) if memo_key else None
        col = hit[0] if hit else collect(tenant)
        run.note("collect", {"aliases": len(consent.names(tenant))},
                 {"words": col["words"], "background_words": col["background_words"], "files": len(col["files"]),
                  "dropped_lines": col["dropped"]}, label="Collect attributed lines")
        if col["words"] < attribution.DRAFT_WORDS:
            msg = (f"Not enough attributed material yet: {col['words']:,} of {attribution.DRAFT_WORDS:,} words said by the "
                   "subject. Add labelled transcripts or mark single-speaker lessons.")
            if attribution.uses_fixture(tenant):
                msg = "Load the owner's material first. " + msg
            run.note("refused", {}, {"reason": msg}, status="error", label="Save draft profile")
            raise consent.PersonaError(409, msg)
        seed = int(hashlib.sha256("|".join(col["files"]).encode() + str(col["words"]).encode()).hexdigest()[:8], 16)
        res = run.step("analyse", {"words": col["words"], "background_words": col["background_words"]},
                       lambda: hit[1] if hit else _strip(analyse(col, seed=seed)), "Weighted log-odds, bootstrap and filters")
        if memo_key and not hit:
            _DEMO_MEMO.clear()
            _DEMO_MEMO[memo_key] = (col, res)
        return run.step("save", {"phrases": len(res["phrases"])}, lambda: _save(tenant, col, res, actor),
                        "Save draft profile")

    try:
        out = runtime.execute(tenant, "persona", "Persona: build style profile", PLAN, go, actor)
    except consent.PersonaError:
        raise
    return {"run_id": out["run_id"], **out["result"]}


def _strip(res: dict) -> dict:
    return {"phrases": res["phrases"], "report": res["report"]}
