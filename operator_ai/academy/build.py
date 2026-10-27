"""Academy build (A5): course material -> cited lessons -> quiz items -> objection drills, as one traced
runtime Run "academy_build" that works with AI off.

1. Segment: SOP-like documents split at heading lines; transcripts are cleaned of disfluencies and split
   with TextTiling. Garbled transcripts are skipped and listed as "too noisy".
2. Tag skills: BM25 of each section against each skill's seed query; a skill is tagged at >= 0.6 x best.
3. Key points: scored sentences (rule wording, number + unit, idf mass, position, hedges). Every candidate
   sentence passes guardrails.check(text, "plain") or is skipped and listed for the owner (A3).
4. Order: definitions before use inside a skill (topological sort), path skill order across skills.
5. Assemble: title, 3-7 key points each with an exact [S#] file:line citation, Example and Common mistakes.
6. AI mode (optional): a 150-300 word explainer that must pass citation, overlap, number and guardrail
   checks, or the extractive version is kept.
7. Lessons start as drafts; the owner or a coach publishes them. source_sha marks lessons stale when the
   cited material changes; a rebuild writes a fresh draft next to the stale lesson.
"""
from __future__ import annotations

import math
import re
import statistics
import threading
from collections import Counter
from dataclasses import dataclass, field

from .. import audit, content_policy, db, guardrails, index, llm, runtime, textnorm
from . import common
from . import text as T

PLAN = ["Check the course material", "Split the material into sections", "Tag skills", "Pick key points (guardrail-screened)",
        "Order lessons", "Write lessons (rules; AI optional)", "Generate quiz items", "Extract objection drills",
        "Save drafts and mark stale lessons"]
GEN_VERSION = 1
MIN_SECTION_WORDS = 40
MIN_SEGMENT_WORDS = 150
MAX_KEY_POINTS = 7
MIN_KEY_POINTS = 3

_build_lock = threading.Lock()


# --------------------------------------------------------------------------- data
@dataclass
class Section:
    file: str
    kind: str
    doc_title: str
    title: str
    heading_line: int | None
    lines: list[tuple[int, str]]          # (1-based line number, cleaned text)
    doc_order: int
    example_lines: list[tuple[int, str]] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    skill: str = "general"

    @property
    def text(self) -> str:
        return "\n".join(t for _, t in self.lines)

    @property
    def words(self) -> int:
        return len(self.text.split())

    @property
    def start(self) -> int:
        nums = [n for n, _ in self.lines + self.example_lines]
        return min(nums) if nums else (self.heading_line or 1)

    @property
    def end(self) -> int:
        nums = [n for n, _ in self.lines + self.example_lines]
        return max(nums) if nums else (self.heading_line or 1)

    @property
    def source_sha(self) -> str:
        return common.sha(self.title + "\n" + "\n".join(f"{n}:{t}" for n, t in self.lines + self.example_lines))

    @property
    def key(self) -> str:
        return common.sha(f"{self.file}|{T.norm_cmp(self.title)}")


# --------------------------------------------------------------------------- 1. segment
_HEAD_PUNCT = tuple(".!?:;,\"'”)")
# notices such as "FICTIONAL TRAINING MATERIAL FOR TESTS" or "CONFIDENTIAL" are neither headings nor content
MARKER = re.compile(r"^(fictional|confidential|internal only|for internal|draft|copyright|do not (share|distribute))\b", re.I)


def _heading_like(s: str) -> bool:
    s = s.strip()
    if not s:
        return False
    if s.startswith("#"):
        return True
    letters = [ch for ch in s if ch.isalpha()]
    if letters and s.upper() == s and len(s.split()) <= 12:
        return True
    return len(s.split()) <= 8 and not s.endswith(_HEAD_PUNCT[:-1]) and not s.endswith(".")


def _heading_text(s: str) -> str:
    return s.strip().lstrip("#").strip()


def segment_doc(doc, excluded_lines: set[int], doc_order: int) -> list[Section]:
    lines = [(i, ln) for i, ln in enumerate(doc.lines, start=1)]
    if doc.kind == "transcript":
        return _segment_transcript(doc, lines, excluded_lines, doc_order)
    secs: list[Section] = []
    cur = Section(doc.file, doc.kind, doc.title, doc.title, None, [], doc_order)
    nonempty = [(i, ln) for i, ln in lines if ln.strip()]
    # the document's own title line is the lesson outline's title, not content
    if nonempty and T.norm_cmp(_heading_text(nonempty[0][1])) == T.norm_cmp(doc.title):
        nonempty = nonempty[1:]
    for pos, (i, ln) in enumerate(nonempty):
        nxt = nonempty[pos + 1][1] if pos + 1 < len(nonempty) else ""
        s = ln.strip()
        if MARKER.match(s) and s.upper() == s:
            continue
        caps = bool([ch for ch in s if ch.isalpha()]) and s.upper() == s
        is_head = s.startswith("#") or caps or (_heading_like(s) and nxt and not _heading_like(nxt))
        if is_head:
            secs.append(cur)
            title = _heading_text(s)
            cur = Section(doc.file, doc.kind, doc.title, title.capitalize() if caps else title, i, [], doc_order)
            continue
        if i in excluded_lines:
            continue
        cur.lines.append((i, s))
    secs.append(cur)
    secs = [x for x in secs if x.lines]
    # "Example ..." sections become the Example block of the section before them
    merged: list[Section] = []
    for x in secs:
        if merged and re.match(r"^(an? )?examples?\b", x.title, re.I):
            merged[-1].example_lines.extend(x.lines)
            continue
        merged.append(x)
    # short sections fold into the previous one (or the next one when they come first)
    out: list[Section] = []
    for x in merged:
        if out and x.words < MIN_SECTION_WORDS:
            out[-1].lines.extend(x.lines)
            out[-1].example_lines.extend(x.example_lines)
            continue
        out.append(x)
    if len(out) > 1 and out[0].words < MIN_SECTION_WORDS:
        first = out.pop(0)
        out[0].lines = first.lines + out[0].lines
        out[0].example_lines = first.example_lines + out[0].example_lines
    return out


def _idf_vec(tokens: list[str], idf) -> Counter:
    v: Counter = Counter()
    for t in tokens:
        v[t] += idf(t)
    return v


def _cos(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    num = sum(a[t] * b.get(t, 0.0) for t in a)
    da = math.sqrt(sum(x * x for x in a.values()))
    dbb = math.sqrt(sum(x * x for x in b.values()))
    return num / (da * dbb) if da and dbb else 0.0


def texttile(lines: list[tuple[int, str]], idf, w: int = 20, k: int = 6, min_words: int = MIN_SEGMENT_WORDS) -> list[list[tuple[int, str]]]:
    """TextTiling: 20-token pseudo-sentences, block size 6, cosine of idf-weighted vectors, boundaries where
    the depth score exceeds mean - sd/2, segments of at least `min_words` words. Boundaries snap to lines."""
    toks: list[tuple[str, int]] = []
    for n, t in lines:
        for tok in textnorm.tokens(t):
            toks.append((tok, n))
    total_words = sum(len(t.split()) for _, t in lines)
    pseudo = [toks[i:i + w] for i in range(0, len(toks), w)]
    if len(pseudo) < 2 * k or total_words < 2 * min_words:
        return [lines]
    gaps = []
    for g in range(1, len(pseudo)):
        left = [t for ps in pseudo[max(0, g - k):g] for t, _ in ps]
        right = [t for ps in pseudo[g:g + k] for t, _ in ps]
        gaps.append(_cos(_idf_vec(left, idf), _idf_vec(right, idf)))
    depth = []
    for i, s in enumerate(gaps):
        lp = s
        j = i
        while j > 0 and gaps[j - 1] >= lp:
            lp = gaps[j - 1]
            j -= 1
        rp = s
        j = i
        while j < len(gaps) - 1 and gaps[j + 1] >= rp:
            rp = gaps[j + 1]
            j += 1
        depth.append((lp - s) + (rp - s))
    mean = statistics.mean(depth)
    sd = statistics.pstdev(depth) if len(depth) > 1 else 0.0
    cut = mean - sd / 2
    cands = sorted([i for i, d in enumerate(depth) if d > cut and d > 0], key=lambda i: -depth[i])
    line_words = {n: len(t.split()) for n, t in lines}
    order = [n for n, _ in lines]
    chosen: list[int] = []  # boundary = first line number of the new segment
    for gi in cands:
        # depth[gi] is the gap before pseudo-sentence gi + 1: the new segment starts at its first token's line
        if gi + 1 >= len(pseudo) or not pseudo[gi + 1]:
            continue
        b = pseudo[gi + 1][0][1]
        if b == order[0]:
            continue
        trial = sorted(set(chosen + [b]))
        # every segment must keep at least min_words words
        bounds = [order[0]] + trial + [order[-1] + 1]
        ok = True
        for a, z in zip(bounds, bounds[1:]):
            if sum(line_words[n] for n in order if a <= n < z) < min_words:
                ok = False
                break
        if ok:
            chosen = trial
    if not chosen:
        return [lines]
    segs: list[list[tuple[int, str]]] = []
    bounds = [order[0]] + chosen + [order[-1] + 1]
    for a, z in zip(bounds, bounds[1:]):
        seg = [(n, t) for n, t in lines if a <= n < z]
        if seg:
            segs.append(seg)
    return segs


DISPLAY_TERM = {"thunderdome": "Thunder Dome", "showrate": "show rate", "noshow": "no-shows", "followup": "follow-ups",
                "cpl": "cost per lead", "cpa": "cost per acquisition", "ctr": "click-through rate", "dm": "DMs",
                "usp": "USP", "killrule": "kill rule", "precall": "pre-call brief", "cta": "calls to action",
                "vsl": "VSL", "optin": "opt-ins", "replyrate": "reply rate", "closerate": "close rate",
                "creativefatigue": "creative fatigue", "donewithyou": "done-with-you", "highticket": "high-ticket",
                "paidads": "paid ads"}


def _display_token(tok: str, text: str) -> str:
    if tok in DISPLAY_TERM:
        return DISPLAY_TERM[tok]
    counts: Counter = Counter()
    for w in re.findall(r"[A-Za-z][A-Za-z'-]+", text):
        if textnorm.stem(w.lower()) == tok:
            counts[w.lower()] += 1
    return counts.most_common(1)[0][0] if counts else tok


def top_terms(text: str, idf, n: int = 2) -> list[str]:
    tf = Counter(t for t in textnorm.tokens(text) if len(t) > 2 and not t.isdigit())
    ranked = sorted(tf, key=lambda t: -(tf[t] * idf(t)))
    return [_display_token(t, text) for t in ranked[:n]]


def _segment_transcript(doc, lines, excluded_lines, doc_order) -> list[Section]:
    clean = [(i, T.clean_spoken(ln)) for i, ln in lines if ln.strip() and i not in excluded_lines]
    clean = [(i, t) for i, t in clean if t]
    if not clean:
        return []
    idf = _IDF.get() or (lambda t: 1.0)
    segs = texttile(clean, idf)
    out = []
    for n, seg in enumerate(segs, start=1):
        title = doc.title
        if len(segs) > 1:
            terms = top_terms(" ".join(t for _, t in seg), idf)
            title = f"{doc.title}: {', '.join(terms)}" if terms else f"{doc.title} (part {n})"
        out.append(Section(doc.file, doc.kind, doc.title, title, None, seg, doc_order))
    return out


class _IdfHolder:
    def __init__(self):
        self.local = threading.local()

    def get(self):
        return getattr(self.local, "fn", None)

    def set(self, fn) -> None:
        self.local.fn = fn


_IDF = _IdfHolder()


# --------------------------------------------------------------------------- 2. tag skills
K1, B = 1.5, 0.75


def tag_skills(sections: list[Section], idf) -> None:
    if not sections:
        return
    tfs = [Counter(textnorm.tokens(s.title + "\n" + s.text)) for s in sections]
    lens = [sum(tf.values()) or 1 for tf in tfs]
    avg = sum(lens) / len(lens)
    queries = {k: textnorm.expand(textnorm.tokens(q)) for k, (_, q) in common.SKILLS.items() if q}
    for s, tf, ln in zip(sections, tfs, lens):
        scores = {}
        for skill, q in queries.items():
            sc = 0.0
            for t, w in q:
                f = tf.get(t, 0)
                if f:
                    sc += w * idf(t) * f * (K1 + 1) / (f + K1 * (1 - B + B * ln / avg))
            scores[skill] = sc
        best = max(scores.values()) if scores else 0.0
        if best <= 0:
            s.skills, s.skill = ["general"], "general"
            continue
        ranked = sorted(scores, key=lambda k: -scores[k])
        s.skills = [k for k in ranked if scores[k] >= 0.6 * best]
        s.skill = ranked[0]


# --------------------------------------------------------------------------- 3. key points
RULE_START = re.compile(
    r"^(never|always|only|use|ask|keep|send|kill|test|confirm|don'?t|do not|make|book|message|pick|write|run|give|"
    r"change|follow|track|move|state|answer|read|go|check|put|stop|start|add|share|tell|offer|compare|avoid|"
    r"every|each|rule \d+|benchmark|segment \d+)\b", re.I)
MODAL = re.compile(r"\b(must|should|the rule|has to|have to|needs? to|is the|means)\b", re.I)
META = re.compile(r"^(this is (the|our|a)|welcome|that'?s it|alright|okay|ok,|so let'?s|let'?s (talk|start|go)|today we|"
                  r"in this (lesson|module|video)|now (the|let'?s|what)|the (first|second|third|fourth|next) (one|thing) is)\b", re.I)
HEDGE = re.compile(r"\b(maybe|might|perhaps|probably|usually|sometimes|i think|kind of|sort of|i guess|could be)\b", re.I)
MISTAKE = re.compile(r"^(never|don'?t|do not|avoid|no )\b", re.I)
EXAMPLE = re.compile(r"^(for example|example|e\.g\.|for instance)\b", re.I)


@dataclass
class Candidate:
    text: str
    line: int
    pos: int
    score: float


def candidates(sec: Section, idf, skipped: list[dict], tenant=None) -> list[Candidate]:
    out: list[Candidate] = []
    pos = 0
    for n, line in sec.lines:
        for s in T.sentences(line):
            if not T.is_full_sentence(s, 5):
                continue
            # the tenant's banned names and feature rules too, so no real person's name becomes a lesson point
            flags = guardrails.check_for(tenant, s, {}) if tenant is not None else guardrails.check(s, "plain")
            if guardrails.blocked(flags):
                code = next(f["code"] for f in flags if f["severity"] == "block")
                label = content_policy.TACTIC_NAMES.get(code, code.replace("_", " "))
                skipped.append({"file": sec.file, "line": n, "code": code, "text": s[:300],
                                "label": f"Skipped: {label} (line {n})"})
                continue
            toks = textnorm.tokens(s)
            if not toks:
                continue
            sc = 0.0
            if RULE_START.search(s) or MODAL.search(s):
                sc += 2.0
            if T.quantities_in(s):
                sc += 1.5
            sc += sum(idf(t) for t in toks) / max(6, len(toks))
            if pos < 3:
                sc += 0.5
            if HEDGE.search(s):
                sc -= 1.0
            if META.search(s):
                sc -= 3.0
            out.append(Candidate(s, n, pos, sc))
            pos += 1
    return out


def pick_key_points(cands: list[Candidate]) -> tuple[list[Candidate], list[Candidate], list[Candidate]]:
    """-> (key points, examples, mistakes), each in source order."""
    examples = [c for c in cands if EXAMPLE.search(c.text)]
    mistakes = [c for c in cands if MISTAKE.search(c.text) and c not in examples]
    rest = [c for c in cands if c not in examples and c not in mistakes]
    want = max(MIN_KEY_POINTS, min(MAX_KEY_POINTS, round(len(cands) / 3) or MIN_KEY_POINTS))
    pool = rest if len(rest) >= MIN_KEY_POINTS else rest + mistakes
    chosen = sorted(pool, key=lambda c: -c.score)[:want]
    chosen.sort(key=lambda c: (c.line, c.pos))
    return chosen, examples[:3], mistakes[:5]


# --------------------------------------------------------------------------- 4. order
DEFINES = re.compile(r"^(?:the |a |an )?([A-Za-z][A-Za-z -]{2,40}?) (?:is|are|means) (?:the |a |an |where |when |what |how )",
                     re.I)


_COMPOUND_TOKENS = {rep.strip().split()[0] for _, rep in textnorm.COMPOUNDS}


def order_lessons(lessons: list[dict]) -> list[dict]:
    """Within each skill, a section that defines a term comes before sections that use it."""
    by_skill: dict[str, list[dict]] = {}
    for L in lessons:
        by_skill.setdefault(L["skill"], []).append(L)
    out = []
    for skill, group in by_skill.items():
        group.sort(key=lambda L: (L["doc_order"], L["start"]))
        defs = {}
        for i, L in enumerate(group):
            for kp in L["all_sentences"]:
                m = DEFINES.match(kp)
                if m:
                    toks = tuple(textnorm.tokens(m.group(1)))
                    # only programme terms count as definitions ("the Thunder Dome is"), not "the winner is"
                    if not toks or not (len(toks) >= 2 or toks[0] in _COMPOUND_TOKENS):
                        continue
                    if toks not in defs:
                        defs[toks] = i
        edges: dict[int, set[int]] = {i: set() for i in range(len(group))}
        indeg = [0] * len(group)
        for toks, di in defs.items():
            for j, L in enumerate(group):
                if j == di:
                    continue
                body = set(textnorm.tokens(" ".join(L["all_sentences"])))
                if all(t in body for t in toks) and j not in edges[di]:
                    edges[di].add(j)
                    indeg[j] += 1
        ready = [i for i in range(len(group)) if indeg[i] == 0]
        seq = []
        while ready:
            ready.sort()
            i = ready.pop(0)
            seq.append(i)
            for j in sorted(edges[i]):
                indeg[j] -= 1
                if indeg[j] == 0:
                    ready.append(j)
        seq += [i for i in range(len(group)) if i not in seq]  # cycles keep source order
        for o, i in enumerate(seq):
            group[i]["ord"] = o
            out.append(group[i])
    return out


# --------------------------------------------------------------------------- 5. assemble
def _blocked(tenant, text: str) -> list[dict] | None:
    """Block flags for a sentence (the tenant's banned names, words and feature rules when we know the tenant)."""
    flags = guardrails.check_for(tenant, text, {}) if tenant is not None else guardrails.check(text, "plain")
    return [f for f in flags if f["severity"] == "block"] or None


def assemble(sec: Section, kps: list[Candidate], examples: list[Candidate], mistakes: list[Candidate],
             tenant=None, skipped: list[dict] | None = None) -> dict:
    cites: list[dict] = []
    by_line: dict[int, str] = {}

    def cite(c: Candidate) -> str:
        if c.line not in by_line:
            cid = f"S{len(cites) + 1}"
            by_line[c.line] = cid
            cites.append({"id": cid, "file": sec.file, "lines": f"{c.line}-{c.line}", "title": sec.doc_title,
                          "snippet": c.text[:300], "kind": sec.kind})
        return by_line[c.line]

    key_points = [{"text": c.text, "cite": cite(c), "line": c.line} for c in kps]
    ex = [{"text": c.text, "cite": cite(c), "line": c.line} for c in examples]
    # the lines of an "Example ..." section, verbatim, when there were no "Example:" sentences
    if not ex and sec.example_lines:
        for n, t in sec.example_lines[:2]:
            for s in T.sentences(t)[:1]:
                bad = _blocked(tenant, s)
                if bad:
                    if skipped is not None:
                        code = bad[0]["code"]
                        label = content_policy.TACTIC_NAMES.get(code, code.replace("_", " "))
                        skipped.append({"file": sec.file, "line": n, "code": code, "text": s[:300],
                                        "label": f"Skipped: {label} (line {n})"})
                    continue
                ex.append({"text": s, "cite": cite(Candidate(s, n, 0, 0)), "line": n})
    mk = [{"text": c.text, "cite": cite(c), "line": c.line} for c in mistakes]
    body = "\n".join(f"• {k['text']} [{k['cite']}]" for k in key_points)
    words = sum(len(k["text"].split()) for k in key_points + ex + mk)
    return {"key": sec.key, "title": sec.title, "skill": sec.skill, "skills": sec.skills, "file": sec.file,
            "kind": sec.kind, "doc_title": sec.doc_title, "doc_order": sec.doc_order, "start": sec.start, "end": sec.end,
            "source_sha": sec.source_sha, "key_points": key_points, "example": ex, "mistakes": mk, "citations": cites,
            "body": body, "gen_mode": "rules", "est_minutes": max(2, math.ceil(words / 120) + 1),
            "all_sentences": [c.text for c in kps + examples + mistakes], "checks": {"citations": "exact lines"}}


# --------------------------------------------------------------------------- 6. AI explainer (optional)
AI_SYSTEM = ("You write short training lessons for a coaching programme. Use ONLY the numbered course lines. "
             "Write 150-300 words in plain English. End EVERY sentence with the [S#] marker of the line it comes from. "
             "Never add numbers, names, results, promises or claims that are not in the lines.")
_CITE = re.compile(r"\[(S\d+)\]")


def check_explainer(text: str, lesson: dict, tenant=None) -> tuple[bool, list[str]]:
    from ..agents import coach

    allowed = {c["id"] for c in lesson["citations"]}
    lines_by_id = {c["id"]: c["snippet"] for c in lesson["citations"]}
    problems = []
    clean, kept, stripped = coach.verify_citations(text, [], allowed)
    if stripped:
        problems.append(f"invented citations: {', '.join(stripped)}")
    words = len(_CITE.sub("", clean).split())
    if not 150 <= words <= 300:
        problems.append(f"length {words} words (needs 150-300)")
    for s in [x for x in re.split(r"(?<=[.!?\]])\s+", clean) if x.strip()]:
        ids = _CITE.findall(s)
        if not ids:
            problems.append("a sentence has no citation")
            break
        cited = " ".join(lines_by_id.get(i, "") for i in ids)
        st = set(textnorm.tokens(_CITE.sub("", s)))
        ct = set(textnorm.tokens(cited))
        if st and len(st & ct) / len(st) < 0.5:
            problems.append("a sentence drifts from its cited line")
            break
        for q in T.find_quantities(T.words_to_digits(_CITE.sub("", s))):
            if not any(abs(x["value"] - q["value"]) < 1e-9 for x in T.find_quantities(T.words_to_digits(cited))):
                problems.append("a number is not in the cited line")
                break
    if _blocked(tenant, _CITE.sub("", clean)):
        problems.append("guardrails")
    return (not problems), problems


def ai_explainer(tenant, lesson: dict, run) -> None:
    """Rewrite the key points into an explainer when the AI gate is open (or a saved replay exists). Any failed
    check keeps the extractive lesson. Never required: AI off is the normal path."""
    numbered = "\n".join(f"[{c['id']}] {c['snippet']}" for c in lesson["citations"])
    prompt = f"Lesson: {lesson['title']}\nCourse lines:\n{numbered}\n\nWrite the lesson."
    try:
        out = llm.complete_or_replay(tenant, AI_SYSTEM, prompt, purpose="academy_lesson", max_tokens=700, run_id=run.id)
    except llm.AIUnavailable as e:
        lesson["checks"]["ai"] = f"not used: {e.reason}"
        return
    ok, problems = check_explainer(out["text"], lesson, tenant)
    if ok:
        lesson["body"] = out["text"].strip()
        lesson["gen_mode"] = out.get("mode", "live")
        lesson["checks"]["ai"] = "passed every check"
        run.use_ai(lesson["gen_mode"])
    else:
        lesson["checks"]["ai"] = "kept the extractive version: " + "; ".join(problems)


# --------------------------------------------------------------------------- objections (A15 + scenario items)
OBJECTION = re.compile(
    r"^\s*(?:and\s+)?when\s+(?P<who>someone|somebody|a lead|the lead|a prospect|the prospect|a client|the client|they|people)\s+"
    r"(?P<verb>says?|tells? you|asks?|needs? to|wants? to)\s+(?P<x>[^,]{3,80}),\s*(?P<y>.+)$", re.I)


def extract_objections(sections: list[Section], skipped: list[dict] | None = None, tenant=None) -> list[dict]:
    """'When someone says X, Y' -> {objection: X, answer: Y (+ the next sentence on that line), file, line}."""
    out, seen = [], set()
    for sec in sections:
        for n, line in sec.lines:
            sents = T.sentences(line)
            for i, s in enumerate(sents):
                m = OBJECTION.match(s)
                if not m:
                    continue
                x = m.group("x").strip().strip("\"'“”")
                verb = m.group("verb").lower()
                if verb.startswith("need"):
                    x = "I need to " + x
                elif verb.startswith("want"):
                    x = "I want to " + x
                x = re.sub(r"^(that\s+)", "", x, flags=re.I)
                x = x[0].upper() + x[1:]
                y = m.group("y").strip()
                y = y[0].upper() + y[1:]
                answer = y if len(sents) <= i + 1 else f"{y} {sents[i + 1]}"
                if len(y.split()) < 3:
                    continue
                if _blocked(tenant, answer):
                    if skipped is not None:
                        skipped.append({"file": sec.file, "line": n, "code": "objection_answer",
                                        "text": answer[:300], "label": f"Skipped: unsafe objection answer (line {n})"})
                    continue
                k = T.norm_cmp(x)
                if k in seen:
                    continue
                seen.add(k)
                out.append({"objection": x, "answer": answer, "first": y, "file": sec.file, "line": n,
                            "title": sec.doc_title, "kind": sec.kind, "skill": sec.skill, "source": s})
    return out


_obj_cache: dict[str, list[dict]] = {}


def objections_for(tenant) -> list[dict]:
    """Objection pairs from the tenant's material (cached per index), used by call_review.make_drill. Never from
    the fictional fixture material outside the demo (A0)."""
    if not common.material_state(tenant)["ok"]:
        return []
    try:
        idx = index.for_tenant(tenant)
    except Exception:  # noqa: BLE001
        return []
    ck = f"{getattr(tenant, 'id', '')}|{idx.key}"  # per tenant: banned names and rules differ
    if ck in _obj_cache:
        return _obj_cache[ck]
    secs = all_sections(idx)
    tag_skills(secs, idx.idf)
    res = extract_objections(secs, None, tenant)
    _obj_cache[ck] = res
    return res


def objection_answer(tenant, objection: str, query: str = "") -> dict | None:
    """The course's answer to an objection, with an exact line citation, or None."""
    pairs = objections_for(tenant)
    if not pairs:
        return None
    q = set(textnorm.tokens(objection + " " + query))
    best, score = None, 0.0
    for p in pairs:
        pt = set(textnorm.tokens(p["objection"]))
        if not pt:
            continue
        sc = len(q & pt) / len(pt)
        if sc > score:
            best, score = p, sc
    if best is None or score < 0.5:
        return None
    return {"answer": best["answer"], "citation": _pair_cite(best)}


def _pair_cite(p: dict) -> dict:
    return {"id": "S1", "file": p["file"], "lines": f"{p['line']}-{p['line']}", "title": p["title"],
            "snippet": p["source"][:300], "kind": p["kind"]}


def upsert_drills(tenant, pairs: list[dict]) -> int:
    """Seed the objection-drill bank at build time (never on read). Existing drills for the same objection
    get the extracted answer and its exact citation."""
    n = 0
    with tenant.db() as c:
        existing = [(set(textnorm.tokens(r["objection"])), r["id"]) for r in c.execute("SELECT id, objection FROM drills")]
        for p in pairs:
            pt = set(textnorm.tokens(p["objection"]))
            cite = db.jdump(_pair_cite(p))
            # "I need to ask my partner" and "I need to ask a partner" are the same drill
            match = next((did for toks, did in existing if toks and pt and len(toks & pt) / len(toks | pt) >= 0.6), None)
            if match is not None:
                c.execute("UPDATE drills SET answer=?, citation=?, skill=?, lines=? WHERE id=?",
                          (p["answer"], cite, p["skill"], f"{p['line']}-{p['line']}", match))
            else:
                did = c.execute("INSERT INTO drills(objection,answer,citation,source,created,skill,lines) VALUES(?,?,?,?,?,?,?)",
                                (p["objection"], p["answer"], cite, "academy", db.now(), p["skill"],
                                 f"{p['line']}-{p['line']}")).lastrowid
                existing.append((pt, did))
                n += 1
    return n


# --------------------------------------------------------------------------- whole-corpus sections
def all_sections(idx, noisy: list[dict] | None = None) -> list[Section]:
    excluded: dict[str, set[int]] = {}
    for e in idx.excluded:
        a, _, b = e.lines.partition("-")
        excluded.setdefault(e.file, set()).update(range(int(a), int(b or a) + 1))
    _IDF.set(idx.idf)
    secs: list[Section] = []
    for order, doc in enumerate(idx.docs.values()):
        if doc.kind == "summary":
            continue
        if doc.quality == "garbled":
            if noisy is not None:
                noisy.append({"file": doc.file, "title": doc.title, "label": "Too noisy to teach from (garbled transcript)"})
            continue
        secs.extend(segment_doc(doc, excluded.get(doc.file, set()), order))
    return secs


# --------------------------------------------------------------------------- the build run
def _running_build(tenant) -> dict | None:
    with tenant.db() as c:
        r = c.execute("SELECT * FROM academy_builds WHERE status='running' AND created>? ORDER BY id DESC LIMIT 1",
                      (db.now() - 900,)).fetchone()
    return dict(r) if r else None


def start(tenant, actor: str = "system", sync: bool = False) -> dict:
    """Start an Academy build. Raises common.Refused (409) when there is no course material (A0) or a build
    is already running. sync=True runs in this thread (demo seeding, tests); otherwise it runs in the
    background and the caller polls the run."""
    common.ensure_extras(tenant)
    state = common.material_state(tenant)
    if not state["ok"]:
        raise common.Refused(state["reason"])
    with _build_lock:
        if _running_build(tenant):
            raise common.Refused("A build is already running. Wait for it to finish.")
        with tenant.db() as c:
            bid = c.execute("INSERT INTO academy_builds(status,corpus,actor,created) VALUES('running',?,?,?)",
                            (state["source"], actor, db.now())).lastrowid

    def fn(run: runtime.Run):
        with tenant.db() as c:
            c.execute("UPDATE academy_builds SET run_id=? WHERE id=?", (run.id, bid))
        try:
            res = _build(tenant, run, bid, state)
        except Exception:
            with tenant.db() as c:
                c.execute("UPDATE academy_builds SET status='error', finished=? WHERE id=?", (db.now(), bid))
            raise
        return res

    title = "Academy build" + (" (demo material)" if state["demo_material"] else "")
    if sync:
        out = runtime.execute(tenant, "academy_build", title, PLAN, fn, actor)
        audit.log(tenant, actor, "run", {"academy_build": bid, "run_id": out["run_id"]})
        return {"build_id": bid, "run_id": out["run_id"], "status": "done", "result": out["result"]}
    run_id = runtime.launch(tenant, "academy_build", title, PLAN, fn, actor)
    audit.log(tenant, actor, "run", {"academy_build": bid, "run_id": run_id})
    return {"build_id": bid, "run_id": run_id, "status": "running"}


def _build(tenant, run: runtime.Run, build_id: int, state: dict) -> dict:
    from . import quiz

    idx = index.for_tenant(tenant)
    run.note("material", {"source": state["source"]},
             {"documents": len(idx.docs), "demo_material": state["demo_material"],
              "not_used_lines": len(idx.excluded)}, label="Check the course material")
    noisy: list[dict] = []
    secs = run.step("segment", {"documents": len(idx.docs)}, lambda: all_sections(idx, noisy), "Split the material into sections")
    run.note("segment_summary", {}, {"sections": len(secs), "too_noisy": noisy}, label="Sections found")
    run.step("tag", {"skills": list(common.SKILLS)}, lambda: tag_skills(secs, idx.idf) or
             dict(Counter(s.skill for s in secs)), "Tag skills")
    skipped: list[dict] = []
    thin: list[dict] = []

    def _points():
        lessons = []
        for sec in secs:
            cands = candidates(sec, idx.idf, skipped, tenant)
            kps, ex, mk = pick_key_points(cands)
            if len(kps) < 2:
                thin.append({"file": sec.file, "title": sec.title, "label": "Too little to teach from"})
                continue
            lessons.append(assemble(sec, kps, ex, mk, tenant, skipped))
        return lessons

    lessons = run.step("key_points", {"sections": len(secs)}, _points, "Pick key points (guardrail-screened)")
    run.note("skipped", {}, {"skipped": len(skipped), "examples": skipped[:10]}, label="Unsafe sentences skipped")
    lessons = run.step("order", {}, lambda: order_lessons(lessons), "Order lessons")

    def _write():
        ai_ok = llm.unavailable_reason(tenant) is None
        for L in lessons:
            if ai_ok or llm.replay(tenant, "academy_lesson", _probe_prompt(L)):
                ai_explainer(tenant, L, run)
            else:
                L["checks"]["ai"] = "not used: " + (llm.unavailable_reason(tenant) or "AI off")
        return {"lessons": len(lessons), "mode": run.ai_mode}

    run.step("write", {}, _write, "Write lessons (rules; AI optional)")
    objections = extract_objections(secs, skipped, tenant)
    items = run.step("quiz", {"lessons": len(lessons)}, lambda: quiz.generate(tenant, idx, secs, lessons, objections),
                     "Generate quiz items")
    drills_new = run.step("drills", {"pairs": len(objections)}, lambda: upsert_drills(tenant, objections),
                          "Extract objection drills")

    def _save():
        return persist(tenant, lessons, items, build_id)

    saved = run.step("save", {}, _save, "Save drafts and mark stale lessons")
    stats = {**saved, "sections": len(secs), "lessons_built": len(lessons), "items_built": sum(len(v) for v in items.values()),
             "objection_drills": len(objections), "new_drills": drills_new, "skipped": len(skipped), "too_noisy": len(noisy),
             "too_thin": len(thin), "ai_mode": run.ai_mode, "demo_material": state["demo_material"]}
    with tenant.db() as c:
        c.execute("UPDATE academy_builds SET status='done', stats=?, skipped=?, finished=? WHERE id=?",
                  (db.jdump(stats), db.jdump({"unsafe": skipped, "too_noisy": noisy, "too_thin": thin}), db.now(), build_id))
    return stats


def _probe_prompt(L: dict) -> str:
    numbered = "\n".join(f"[{c['id']}] {c['snippet']}" for c in L["citations"])
    return f"Lesson: {L['title']}\nCourse lines:\n{numbered}\n\nWrite the lesson."


# --------------------------------------------------------------------------- 7. persist, drafts, stale
def persist(tenant, lessons: list[dict], items: dict[str, list[dict]], build_id: int) -> dict:
    demo = common.demo_flag(tenant)
    now = db.now()
    created = unchanged = restaled = 0
    keys = set()
    with tenant.db() as c, db.tx(c):
        existing: dict[str, list[dict]] = {}
        for r in c.execute("SELECT id, lesson_key, source_sha, status, gen_version FROM academy_lessons "
                           "WHERE status!='retired' ORDER BY id"):
            existing.setdefault(r["lesson_key"], []).append(dict(r))
        for L in lessons:
            keys.add(L["key"])
            rows = existing.get(L["key"], [])
            if any(r["source_sha"] == L["source_sha"] and r["status"] in ("draft", "published", "rejected") for r in rows):
                # same source: keep the lesson, its status and its learners' progress
                for r in rows:
                    if r["source_sha"] == L["source_sha"]:
                        c.execute("UPDATE academy_lessons SET ord=?, skill=?, skills=? WHERE id=?",
                                  (L["ord"], L["skill"], db.jdump(L["skills"]), r["id"]))
                unchanged += 1
                continue
            version = max([r["gen_version"] or 1 for r in rows], default=0) + 1
            for r in rows:
                if r["status"] == "draft":
                    c.execute("DELETE FROM academy_quiz_items WHERE lesson_id=?", (r["id"],))
                    c.execute("DELETE FROM academy_lessons WHERE id=?", (r["id"],))
                elif r["status"] == "published":
                    c.execute("UPDATE academy_lessons SET status='stale' WHERE id=?", (r["id"],))
                    c.execute("UPDATE academy_quiz_items SET status='stale' WHERE lesson_id=? AND status='published'",
                              (r["id"],))
                    restaled += 1
            lid = c.execute(
                "INSERT INTO academy_lessons(path_key,ord,title,body,key_points,citations,source_file,source_kind,created,"
                "status,gen_mode,gen_version,source_sha,source_lines,skills,prereqs,est_minutes,skill,lesson_key,example,"
                "mistakes,checks,build_id,demo) VALUES(?,?,?,?,?,?,?,?,?,'draft',?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (None, L["ord"], L["title"], L["body"], db.jdump(L["key_points"]), db.jdump(L["citations"]), L["file"],
                 L["kind"], now, L["gen_mode"], version, L["source_sha"], f"{L['start']}-{L['end']}",
                 db.jdump(L["skills"]), "[]", L["est_minutes"], L["skill"], L["key"], db.jdump(L["example"]),
                 db.jdump(L["mistakes"]), db.jdump(L["checks"]), build_id, demo)).lastrowid
            for it in items.get(L["key"], []):
                _insert_item(c, lid, it, demo, now)
            created += 1
        # lessons whose source section disappeared
        for k, rows in existing.items():
            if k in keys:
                continue
            for r in rows:
                if r["status"] == "draft":
                    c.execute("DELETE FROM academy_quiz_items WHERE lesson_id=?", (r["id"],))
                    c.execute("DELETE FROM academy_lessons WHERE id=?", (r["id"],))
                elif r["status"] == "published":
                    c.execute("UPDATE academy_lessons SET status='stale' WHERE id=?", (r["id"],))
                    c.execute("UPDATE academy_quiz_items SET status='stale' WHERE lesson_id=? AND status='published'",
                              (r["id"],))
                    restaled += 1
        # compliance items that are not tied to one lesson (spot the violation) live on the first lesson
    return {"created": created, "unchanged": unchanged, "marked_stale": restaled}


def _insert_item(c, lesson_id: int, it: dict, demo: int, now: float) -> None:
    dup = c.execute("SELECT 1 FROM academy_quiz_items WHERE norm_key=? AND status IN ('draft','published')",
                    (it["norm_key"],)).fetchone()
    if dup:
        return
    c.execute("INSERT INTO academy_quiz_items(lesson_id,kind,prompt,options,answer,explanation,citation,created,status,"
              "pool,generator,difficulty_b,checks,skill,norm_key,accept,quotable,demo) "
              "VALUES(?,?,?,?,?,?,?,?,'draft',?,?,?,?,?,?,?,?,?)",
              (lesson_id, it["kind"], it["prompt"], db.jdump(it.get("options")), db.jdump(it["answer"]),
               it["explanation"], db.jdump(it.get("citation")), now, it["pool"], it["generator"], it["difficulty_b"],
               db.jdump(it["checks"]), it["skill"], it["norm_key"], db.jdump(it.get("accept") or []),
               1 if it.get("quotable", True) else 0, demo))


def check_sources(tenant) -> dict:
    """Compare published lessons with the current material without generating anything: which lessons'
    cited sections changed or disappeared."""
    state = common.material_state(tenant)
    if not state["ok"]:
        raise common.Refused(state["reason"])
    idx = index.for_tenant(tenant)
    secs = {s.key: s for s in all_sections(idx)}
    changed, removed = [], []
    with tenant.db() as c:
        for r in c.execute("SELECT id, title, lesson_key, source_sha FROM academy_lessons WHERE status='published'"):
            s = secs.get(r["lesson_key"])
            if s is None:
                removed.append({"id": r["id"], "title": r["title"]})
            elif s.source_sha != r["source_sha"]:
                changed.append({"id": r["id"], "title": r["title"]})
    return {"changed": changed, "removed": removed, "up_to_date": not changed and not removed}


# --------------------------------------------------------------------------- approval workflow
def decide_lesson(tenant, lesson_id: int, decision: str, actor: str) -> dict:
    """draft -> published (its practice items publish with it; exam items need their own approval),
    draft -> rejected, published/stale -> retired."""
    with tenant.db() as c, db.tx(c):
        r = c.execute("SELECT * FROM academy_lessons WHERE id=?", (lesson_id,)).fetchone()
        if r is None:
            raise KeyError(lesson_id)
        st = r["status"]
        if decision == "publish":
            if st != "draft":
                raise common.Refused(f"Only drafts can be published (this lesson is {st}).")
            # an older published or stale version of the same lesson retires
            olds = [x["id"] for x in c.execute("SELECT id FROM academy_lessons WHERE lesson_key=? AND id!=? AND "
                                                "status IN ('published','stale')", (r["lesson_key"], lesson_id))]
            for oid in olds:
                c.execute("UPDATE academy_lessons SET status='retired' WHERE id=?", (oid,))
                c.execute("UPDATE academy_quiz_items SET status='retired' WHERE lesson_id=? AND status IN ('published','stale')",
                          (oid,))
            c.execute("UPDATE academy_lessons SET status='published', reviewed_by=?, reviewed_at=? WHERE id=?",
                      (actor, db.now(), lesson_id))
            c.execute("UPDATE academy_quiz_items SET status='published' WHERE lesson_id=? AND status='draft' AND pool='practice'",
                      (lesson_id,))
        elif decision == "reject":
            if st != "draft":
                raise common.Refused(f"Only drafts can be rejected (this lesson is {st}).")
            c.execute("UPDATE academy_lessons SET status='rejected', reviewed_by=?, reviewed_at=? WHERE id=?",
                      (actor, db.now(), lesson_id))
            c.execute("UPDATE academy_quiz_items SET status='rejected' WHERE lesson_id=? AND status='draft'", (lesson_id,))
        elif decision == "retire":
            if st not in ("published", "stale"):
                raise common.Refused(f"Only published or stale lessons can be retired (this lesson is {st}).")
            c.execute("UPDATE academy_lessons SET status='retired', reviewed_by=?, reviewed_at=? WHERE id=?",
                      (actor, db.now(), lesson_id))
            c.execute("UPDATE academy_quiz_items SET status='retired' WHERE lesson_id=?", (lesson_id,))
        else:
            raise ValueError(decision)
    audit.log(tenant, actor, "kb.admin", {"academy_lesson": lesson_id, "decision": decision})
    return {"id": lesson_id, "status": {"publish": "published", "reject": "rejected", "retire": "retired"}[decision]}


def publish_all(tenant, actor: str, include_exam: bool = False) -> dict:
    """Bulk 'approve all that passed checks': drafts whose machine checks passed (every key point cited,
    nothing blocked by guardrails). Exam-pool items only with include_exam."""
    with tenant.db() as c:
        drafts = [r["id"] for r in c.execute("SELECT id FROM academy_lessons WHERE status='draft' ORDER BY id")]
    n = 0
    for lid in drafts:
        decide_lesson(tenant, lid, "publish", actor)
        n += 1
    exam = 0
    if include_exam:
        with tenant.db() as c:
            exam = c.execute("UPDATE academy_quiz_items SET status='published' WHERE status='draft' AND pool='exam' AND "
                             "lesson_id IN (SELECT id FROM academy_lessons WHERE status='published')").rowcount
        audit.log(tenant, actor, "kb.admin", {"academy_exam_items_published": exam})
    return {"lessons_published": n, "exam_items_published": exam}


def decide_item(tenant, item_id: int, decision: str, actor: str) -> dict:
    with tenant.db() as c:
        r = c.execute("SELECT i.*, l.status lstatus FROM academy_quiz_items i LEFT JOIN academy_lessons l ON l.id=i.lesson_id "
                      "WHERE i.id=?", (item_id,)).fetchone()
        if r is None:
            raise KeyError(item_id)
        if decision == "publish":
            if r["lstatus"] != "published":
                raise common.Refused("Publish the lesson first; its questions follow it.")
            new = "published"
        elif decision == "reject":
            new = "rejected"
        elif decision == "retire":
            new = "retired"
        elif decision in ("to_exam", "to_practice"):
            pool = "exam" if decision == "to_exam" else "practice"
            if r["kind"] == "write" and pool == "exam":
                raise common.Refused("Write-it questions are graded by rubric and stay in practice.")
            # moving into the exam pool always needs a fresh approval; practice follows its lesson
            new = "draft" if pool == "exam" or r["lstatus"] != "published" else "published"
            if r["status"] in ("rejected", "retired", "stale"):
                new = r["status"]
            c.execute("UPDATE academy_quiz_items SET pool=?, status=? WHERE id=?", (pool, new, item_id))
            audit.log(tenant, actor, "kb.admin", {"academy_item": item_id, "pool": pool})
            return {"id": item_id, "pool": pool, "status": new}
        else:
            raise ValueError(decision)
        c.execute("UPDATE academy_quiz_items SET status=? WHERE id=?", (new, item_id))
    audit.log(tenant, actor, "kb.admin", {"academy_item": item_id, "decision": decision})
    return {"id": item_id, "status": new}
