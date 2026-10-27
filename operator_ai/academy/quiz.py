"""Rules-based quiz generation (A6), grading with robust normalisation, and item analytics.

Item kinds (all generated and checked by code, AI never needed):
  cloze       a rule sentence with its most valuable span blanked (number + unit, programme term, rare noun)
  mc_numeric  multiple choice on a number; distractors are same-unit numbers from elsewhere in the material
  mc_term     multiple choice on a programme term; distractors from the domain's other terms
  order       put an enumeration or numbered sequence back in order
  scenario    "When someone says X" -> the course's move; distractors are other objections' moves plus one
              rule-breaking option that guardrails confirm is blocked
  tf          true/false negation flips (never <-> always); a flipped statement is stored as never quotable
  spot        three compliant course lines plus one injected violation confirmed by guardrails
  write       free text (DM, follow-up, USP, reminder, hook) scored by the rubric and guardrails, with cited fixes

Quality gates: the answer appears in the cited line; options unique after normalisation; source sentence has
at least 8 words; nothing from excluded or garbled lines; dedupe by normalised prompt.
"""
from __future__ import annotations

import functools
import math
import random
import re
from collections import Counter, defaultdict

from .. import db, embeddings, guardrails, rubrics, textnorm
from . import common
from . import text as T

KINDS = ("cloze", "mc_numeric", "mc_term", "order", "scenario", "tf", "spot", "write")
KIND_LABEL = {"cloze": "Fill the gap", "mc_numeric": "Pick the number", "mc_term": "Pick the term", "order": "Put in order",
              "scenario": "What do you say?", "tf": "True or false", "spot": "Spot the rule break", "write": "Write it"}
START_B = {"cloze": 0.3, "mc_numeric": 0.0, "mc_term": -0.2, "order": 0.5, "scenario": 0.0, "tf": -0.5, "spot": -0.3,
           "write": 0.5}
CONTINUOUS_UNITS = {"hour", "minute", "second", "day", "week", "month", "year", "percent", "dollar"}
CHOICE_KINDS = {"mc_numeric", "mc_term", "scenario", "tf", "spot"}
MAX_ITEMS_PER_LESSON = 12
MIN_SOURCE_WORDS = 8

# Injected violations for "spot the rule break". Each must be blocked by guardrails (checked at build time).
VIOLATIONS = [
    ("guarantee", r"guarantee", "We guarantee you will double your bookings in thirty days.", "plain"),
    ("planted_question", r"plant", "Plant a few questions in the chat from a second account.", "plain"),
    ("deceptive_subject", r"subject|following up|re:", "Subject: Re: our chat", "plain"),
    ("outreach_price", r"price|discount|fee", "Our programme price is a small monthly fee, so just reply yes.", "outreach"),
    ("fake_scarcity", r"deadline|scarcity|closing|last chance", "Doors are closing tonight, so this is your last chance.", "plain"),
    ("income_claim", r"income|result|make", "Our students make $10k a month within weeks.", "plain"),
    ("fake_testimonial", r"testimonial|verified|proof|permission", "Write a few fake testimonials to fill the page.", "plain"),
]
# A rule-breaking option for scenario items (confirmed blocked in outreach mode).
BAD_MOVES = ["Offer a discount if they sign up today.", "Promise them guaranteed results so they feel safe.",
             "Tell them the price drops if they decide now."]

WRITE_TASKS = {
    "dms": [("dm", "Write a first-touch DM to a business owner in your niche you have never spoken to. "
                   "One question, no price.", {"lead_tier": "cold"})],
    "offer": [("usp", "Write your USP using the course formula.", {})],
    # generic on purpose: the timings belong to the owner's own reminder ladder, which the lesson teaches
    "show_rate": [("reminder", "Write a reminder you'd send before a booked call, following the course's reminder rules.", {})],
    "ads": [("ad", "Write one short ad hook to test against your current best ad.", {})],
    "client_reporting": [("email", "Write the subject line and opening lines of a weekly client report email.",
                          {"client_asset": True})],
}
FOLLOWUP_TASK = ("followup", "Write a follow-up DM that gives value: a tip, a short video or a question.",
                 {"prior_contact": True, "lead_tier": "cold"})

DETERMINERS = {"the", "a", "an", "your", "their", "our", "his", "her", "its", "my", "each", "every", "any", "per", "of",
               "this", "that", "one", "no", "same", "new", "first", "last", "next", "best", "lowest", "highest"}
NOT_NOUNS = {"because", "cannot", "should", "would", "could", "about", "after", "before", "until", "while", "where",
             "there", "their", "them", "then", "than", "when", "what", "which", "thing", "things", "point", "lot",
             "way", "time", "times", "something", "anything", "everything", "nothing", "someone", "everyone", "people",
             "other", "others", "same", "most", "more", "less", "least", "really", "just", "only", "also", "even"}
_ENUM_CONTEXT = re.compile(r"\b(stages?|steps?|segments?|in order|sequence|ladder|pipeline|phases?)\b", re.I)
_TIME_ITEM = re.compile(r"\b(before|after|within|later|day|hour|minute|week)s?\b", re.I)
_NUMBERED = re.compile(r"^(segment|step|stage|phase)\s+(\d+)\s*[:.\-]\s*(.+)$", re.I)
_NEVER_ALWAYS = re.compile(r"\b(never|always)\b", re.I)


# --------------------------------------------------------------------------- helpers
def _rng(key: str) -> random.Random:
    return random.Random(int(common.sha(key, 12), 16))


def source_key(cite: dict | None, fallback: str = "") -> str:
    """The source line(s) a question is built from. Every question from one line shares a pool and an exam
    draws at most one question per line, so no practice question (whose answer is shown after one try) and
    no other exam question ever hands out an exam answer."""
    if cite and cite.get("file"):
        return f"{cite['file']}|{cite.get('lines') or ''}"
    return fallback


def _pool(src_key: str, kind: str) -> str:
    if kind == "write":
        return "practice"
    return "exam" if int(common.sha("pool|" + src_key, 8), 16) % 10 < 4 else "practice"


def _cite(L: dict, cite_id: str) -> dict | None:
    c = next((x for x in L["citations"] if x["id"] == cite_id), None)
    return {**c, "id": "S1"} if c else None


def _item(kind: str, prompt: str, answer, *, L: dict, cite: dict | None, explanation: str, options=None,
          accept=None, skill: str | None = None, checks: dict | None = None, quotable: bool = True,
          generator: str | None = None) -> dict:
    nk = common.sha(kind + "|" + T.norm_cmp(prompt) + "|" + T.norm_cmp(str(answer)), 24)
    return {"kind": kind, "prompt": prompt, "answer": answer, "options": options, "accept": accept or [],
            "explanation": explanation, "citation": cite, "skill": skill or L["skill"], "norm_key": nk,
            "pool": _pool(source_key(cite, nk), kind), "generator": generator or f"rules:{kind}:v1", "difficulty_b": START_B[kind],
            "checks": checks or {}, "quotable": quotable}


def _opt_key(o: str) -> str:
    toks = textnorm.tokens(T.words_to_digits(o or ""), keep_stop=True)
    return " ".join(toks) if toks else T.norm_cmp(o)


def _unique_options(opts: list[str]) -> bool:
    n = [_opt_key(o) for o in opts]
    return len(set(n)) == len(n) and all(n)


def _shuffle(opts: list[str], key: str) -> list[str]:
    o = list(opts)
    _rng(key).shuffle(o)
    return o


# --------------------------------------------------------------------------- corpus-wide pools
class Pools:
    def __init__(self, idx, secs):
        self.idf = idx.idf
        self.line_text: dict[tuple[str, int], str] = {}
        self.quantities: dict[str, list[tuple[float, str]]] = defaultdict(list)   # unit -> [(value, section key)]
        self.terms: dict[str, set[str]] = defaultdict(set)                         # file -> terms present
        self.all_terms: set[str] = set()
        by_tok: dict[str, str] = {}
        self.never_lines: list[tuple[str, dict]] = []                               # (text, citation)
        for s in secs:
            for n, t in s.lines + s.example_lines:
                self.line_text[(s.file, n)] = t
                for q in T.quantities_in(t):
                    if q["value"] > 0:
                        self.quantities[q["unit"]].append((q["value"], s.key))
                low = t.lower()
                for tok, disp in _compounds_in(low):
                    disp = by_tok.setdefault(tok, disp)
                    self.terms[s.file].add(disp)
                    self.all_terms.add(disp)
                for sent in T.sentences(t):
                    if re.match(r"^(never|don'?t|do not|no )\b", sent, re.I) or "must be honest" in sent.lower():
                        self.never_lines.append((sent, {"id": "S1", "file": s.file, "lines": f"{n}-{n}",
                                                        "title": s.doc_title, "snippet": sent[:300], "kind": s.kind}))


def _compounds_in(low: str) -> list[tuple[str, str]]:
    out = []
    for pat, rep in textnorm.COMPOUNDS:
        m = pat.search(low)
        if m:
            tok = rep.strip().split()[0]
            if tok in ("24h", "2h", "10min", "72h", "48h", "1000impressions", "agreedrange"):
                continue
            out.append((tok, m.group(0).strip()))
    return out


# --------------------------------------------------------------------------- generators
def _cloze_number(L, kp, cite, pools, heading_low) -> list[dict]:
    s = kp["text"]
    qs = T.quantities_in(s)
    if not qs:
        return []
    q = qs[0]
    span = s[q["start"]:q["end"]]
    if s.lower().count(span.lower()) > 1 or span.lower() in heading_low:
        return []
    prompt = s[:q["start"]] + "_____" + s[q["end"]:]
    ans = T.fmt_quantity(q["value"], q["unit"])
    line = pools.line_text.get((cite["file"], int(cite["lines"].split("-")[0])), s) if cite else s
    in_src = any(abs(x["value"] - q["value"]) < 1e-9 and x["unit"] == q["unit"] for x in T.quantities_in(line))
    items = [_item("cloze", prompt, ans, L=L, cite=cite, accept=[span, T.fmt_num(q["value"])],
                   explanation=f"The course says: “{s}”", checks={"answer_in_source": in_src, "source_words": len(s.split())})]
    # numeric multiple choice on the same number
    same_sec = {v for v, k in pools.quantities[q["unit"]] if k == L["key"]}
    cands = sorted({v for v, k in pools.quantities[q["unit"]] if k != L["key"] and v not in same_sec and v != q["value"]},
                   key=lambda v: abs(math.log(v) - math.log(q["value"])))
    distract = cands[:3]
    x = q["value"]
    if q["unit"] in CONTINUOUS_UNITS:
        pads = [T.nice(x * 0.5), T.nice(x * 2), T.nice(x + max(1.0, T.nice(x * 0.25)))]
    elif x >= 10:  # larger counts: "under 39 words" vs "under 40 words" would test nothing the lesson taught
        pads = [T.nice(x * 0.5), T.nice(x * 2), T.nice(x * 1.5), T.nice(x * 3)]
    else:  # small counts: whole numbers only
        pads = [x + 1, x * 2 if x >= 2 else x + 2, x - 1 if x > 1 else x + 3, x + 2]
    for v in pads:
        if len(distract) >= 3:
            break
        if v > 0 and v != x and v not in distract and v not in same_sec:
            distract.append(v)
    if len(distract) >= 3:
        opts = [ans] + [T.fmt_quantity(v, q["unit"]) for v in distract[:3]]
        if _unique_options(opts):
            items.append(_item("mc_numeric", prompt, ans, L=L, cite=cite, options=_shuffle(opts, s + "num"),
                               explanation=f"The course says: “{s}”",
                               checks={"answer_in_source": in_src, "options_unique": True,
                                       "distractors": "same unit, from elsewhere in the material" if cands else "padded"}))
    return items


def _cloze_term(L, kp, cite, pools, heading_low, idf) -> list[dict]:
    s = kp["text"]
    low = s.lower()
    term = None
    for tok, disp in _compounds_in(low):
        if disp in heading_low or low.count(disp) > 1:
            continue
        term = disp
        break
    is_compound = term is not None
    if term is None:
        # the rarest noun: a word right after a determiner ("the gap", "a brief", "your mechanism")
        words = re.findall(r"[A-Za-z][A-Za-z'-]*", s)
        best, bscore = None, 0.0
        for i, w in enumerate(words):
            wl = w.lower()
            if i == 0 or len(wl) < 4 or "'" in wl or wl in NOT_NOUNS or wl in heading_low:
                continue
            if words[i - 1].lower() not in DETERMINERS:
                continue
            st = textnorm.stem(wl)
            if wl in textnorm.STOP or st in textnorm.STOP or wl.endswith(("ly", "ed")):
                continue
            if len(re.findall(r"(?<![\w-])" + re.escape(wl) + r"(?![\w-])", low)) > 1:
                continue
            sc = idf(st)
            if sc > bscore:
                best, bscore = w, sc
        term = best
    if not term:
        return []
    m = re.search(r"(?<![\w-])" + re.escape(term) + r"(?![\w-])", s, re.I)
    if not m:
        return []
    span = s[m.start():m.end()]
    prompt = s[:m.start()] + "_____" + s[m.end():]
    items = [_item("cloze", prompt, span, L=L, cite=cite, accept=[span.lower()],
                   explanation=f"The course says: “{s}”", checks={"answer_in_source": True, "source_words": len(s.split())})]
    if is_compound:
        pool = sorted(t for t in pools.all_terms if _opt_key(t) != _opt_key(span) and t not in low)
        if len(pool) >= 3:
            chosen = _pick_terms(span.lower(), pool, pools.terms.get(cite["file"] if cite else "", set()))
            if len(chosen) >= 3:
                opts = [span.lower()] + chosen[:3]
                if _unique_options(opts):
                    items.append(_item("mc_term", prompt, span.lower(), L=L, cite=cite, options=_shuffle(opts, s + "term"),
                                       explanation=f"The course says: “{s}”",
                                       checks={"answer_in_source": True, "options_unique": True}))
    return items


def _pick_terms(correct: str, pool: list[str], cooccur: set[str]) -> list[str]:
    """Distractors at embedding cosine 0.4-0.8 from the right answer; co-occurring terms as the fallback."""
    try:
        vecs = embeddings.get().embed([correct] + pool)
        sims = vecs[1:] @ vecs[0]
        mid = [p for p, s in sorted(zip(pool, sims), key=lambda x: -x[1]) if 0.4 <= s <= 0.8]
    except Exception:  # noqa: BLE001 - embeddings are an optional signal
        mid = []
    out = list(mid[:3])
    for p in sorted(cooccur):
        if len(out) >= 3:
            break
        if p != correct and p not in out and p in pool:
            out.append(p)
    for p in pool:
        if len(out) >= 3:
            break
        if p not in out:
            out.append(p)
    return out


def _tf(L, kp, cite) -> list[dict]:
    s = kp["text"]
    hits = _NEVER_ALWAYS.findall(s)
    if len(hits) != 1 or not re.match(r"^(never|always)\b", s, re.I) or len(s.split()) < 5:
        return []
    flip = int(common.sha(s, 4), 16) % 2 == 0
    if flip:
        def swap(m):
            w = m.group(0)
            new = "always" if w.lower() == "never" else "never"
            return new.capitalize() if w[0].isupper() else new
        stmt = _NEVER_ALWAYS.sub(swap, s, count=1)
        return [_item("tf", f"True or false: {stmt}", "False", L=L, cite=cite, options=["True", "False"],
                      explanation=f"False. The course says: “{s}”", quotable=False,
                      checks={"answer_in_source": True, "flipped": True})]
    return [_item("tf", f"True or false: {s}", "True", L=L, cite=cite, options=["True", "False"],
                  explanation=f"True. The course says: “{s}”", checks={"answer_in_source": True})]


def _enumerations(sec) -> list[tuple[list[str], str, int, int]]:
    """-> [(items in source order, hint, first line, last line)]"""
    out = []
    # numbered sequences ("Segment 1: ...")
    seq, lines = [], []
    for n, t in sec.lines:
        m = _NUMBERED.match(t.strip())
        if m and (not seq or int(m.group(2)) == seq[-1][0] + 1):
            label = re.split(r"[,.;(]", m.group(3))[0].strip()
            if 1 <= len(label.split()) <= 6:
                seq.append((int(m.group(2)), label))
                lines.append(n)
                continue
        if len(seq) >= 3:
            out.append(([x[1] for x in seq], f"the {m_kind(sec, lines)}", lines[0], lines[-1]))
        seq, lines = [], []
    if len(seq) >= 3:
        out.append(([x[1] for x in seq], f"the {m_kind(sec, lines)}", lines[0], lines[-1]))
    # comma lists in one sentence, when the context says order matters
    prev = ""
    for n, t in sec.lines:
        for s in T.sentences(t):
            body = s.rstrip(".!?")
            head = ""
            if ":" in body:
                head, body = body.split(":", 1)
            parts = [p.strip() for p in re.split(r",\s*", body) if p.strip()]
            if len(parts) >= 3 and re.match(r"^(and|or|then)\s+", parts[-1], re.I):
                parts[-1] = re.sub(r"^(and|or|then)\s+", "", parts[-1], flags=re.I)
            elif len(parts) >= 2 and " and " in parts[-1]:
                a, _, b = parts[-1].rpartition(" and ")
                if a.strip() and b.strip():
                    parts = parts[:-1] + [a.strip(), b.strip()]
            ok = (3 <= len(parts) <= 8 and all(1 <= len(p.split()) <= 6 for p in parts)
                  and not any(len(p.split()) == 1 and p.lower().endswith("ly") for p in parts)
                  and not re.match(r"^(if|once|for example|when|so)\b", parts[0], re.I))
            ctx = " ".join([prev if not _NUMBERED.match(prev.strip()) else "", head, s])
            timed = sum(1 for p in parts if _TIME_ITEM.search(p)) >= max(2, len(parts) - 1)
            if ok and (_ENUM_CONTEXT.search(ctx) or timed) and not re.search(r"\b(or)\b", body, re.I):
                hint = head.strip()
                if not hint and _ENUM_CONTEXT.search(prev) and len(prev) <= 90:
                    hint = prev.strip().rstrip(".")
                out.append((parts, hint, n, n))
            prev = s
    return out


def m_kind(sec, lines) -> str:
    for n, t in sec.lines:
        if n == lines[0]:
            return _NUMBERED.match(t.strip()).group(1).lower() + "s"
    return "steps"


def _order(L, sec, pools) -> list[dict]:
    items = []
    for parts, hint, a, b in _enumerations(sec):
        src = " ".join(pools.line_text.get((sec.file, n), "") for n in range(a, b + 1))
        in_src = all(T.norm_cmp(p) in T.norm_cmp(T.clean_spoken(src)) for p in parts)
        if not in_src:
            continue
        key = "|".join(parts)
        shuffled = _shuffle(parts, key)
        tries = 0
        while shuffled == parts and tries < 5:
            shuffled = _shuffle(parts, key + str(tries))
            tries += 1
        if shuffled == parts or not _unique_options(parts):
            continue
        cite = {"id": "S1", "file": sec.file, "lines": f"{a}-{b}", "title": sec.doc_title, "snippet": src[:300],
                "kind": sec.kind}
        hint_txt = f" ({hint})" if hint and len(hint) <= 90 else ""
        items.append(_item("order", f"Put these in the order the course teaches them{hint_txt}.", parts, L=L, cite=cite,
                           options=shuffled, explanation="The course order: " + " → ".join(parts),
                           checks={"answer_in_source": True, "options_unique": True}))
    return items


def _scenarios(L, objections: list[dict]) -> list[dict]:
    mine = [o for o in objections if o["file"] == L["file"] and L["start"] <= o["line"] <= L["end"]]
    items = []
    for o in mine:
        others = [x["first"] for x in objections if x is not o]
        bad = None
        for mv in BAD_MOVES:
            if guardrails.blocked(guardrails.check(mv, "outreach", {"lead_tier": "warm"})):
                bad = mv
                break
        opts = [o["first"]] + others[:2] + ([bad] if bad else [])
        if len(opts) < 3 or not _unique_options(opts):
            continue
        cite = {"id": "S1", "file": o["file"], "lines": f"{o['line']}-{o['line']}", "title": o["title"],
                "snippet": o["source"][:300], "kind": o["kind"]}
        said = f"“{o['objection']}”" + ("" if o["objection"].endswith("?") else ".")
        items.append(_item("scenario", f"A prospect says: {said} What does the course tell you to do?",
                           o["first"], L=L, cite=cite, options=_shuffle(opts, o["objection"]),
                           explanation=f"The course says: “{o['source']}”",
                           checks={"answer_in_source": True, "options_unique": True,
                                   "rule_break_option_blocked": bool(bad)}))
    return items


def _spot(L, pools) -> list[dict]:
    clean = [k for k in L["key_points"] if len(k["text"]) <= 170 and not
             guardrails.check(k["text"], "outreach", {"lead_tier": "warm"})][:3]
    if len(clean) < 3:
        return []
    text_low = " ".join(k["text"] for k in L["key_points"] + L["mistakes"]).lower()
    rules = {code: next((c for t, c in pools.never_lines if re.search(kw, t, re.I)), None) for code, kw, _, _ in VIOLATIONS}
    # a violation of a rule the course itself states comes first, so the explanation can cite that rule; one the
    # course never states is cited to nothing rather than to an unrelated lesson line
    order = sorted(VIOLATIONS, key=lambda v: (0 if rules[v[0]] else 1, 0 if re.search(v[1], text_low) else 1,
                                              VIOLATIONS.index(v)))
    for code, kw, line, mode in order:
        flags = guardrails.check(line, mode, {"lead_tier": "warm"} if mode == "outreach" else {})
        if not guardrails.blocked(flags):
            continue
        msg = next(f["message"] for f in flags if f["severity"] == "block")
        rule = rules[code]
        opts = [k["text"] for k in clean] + [line]
        if not _unique_options(opts):
            continue
        expl = f"“{line}” breaks the rules: {msg}."
        if rule:
            expl += f" The course: “{rule['snippet']}”"
        return [_item("spot", "Three of these lines follow the course. Which one breaks the rules?", line, L=L,
                      cite=rule, options=_shuffle(opts, L["key"] + "spot"),
                      explanation=expl, skill="compliance",
                      checks={"violation_blocked_by_guardrails": True, "others_clean": True, "options_unique": True})]
    return []


def _write(L, skill_done: set[str]) -> list[dict]:
    tasks = list(WRITE_TASKS.get(L["skill"], []))
    if L["skill"] == "dms" and any("follow" in k["text"].lower() for k in L["key_points"]):
        tasks.append(FOLLOWUP_TASK)
    out = []
    for asset, prompt, meta in tasks:
        if asset in skill_done:
            continue
        skill_done.add(asset)
        cite = _cite(L, L["key_points"][0]["cite"]) if L["key_points"] else None
        it = _item("write", prompt, {"asset": asset, "meta": meta}, L=L, cite=cite,
                   explanation="Scored by the programme rubric and the guardrails, with fixes cited to the course.",
                   checks={"graded_by": "rubric + guardrails"})
        out.append(it)
    return out


def generate(tenant, idx, secs, lessons: list[dict], objections: list[dict]) -> dict[str, list[dict]]:
    """lesson key -> items (deduplicated by normalised prompt)."""
    pools = Pools(idx, secs)
    by_key = {s.key: s for s in secs}
    seen: set[str] = set()
    write_done: set[str] = set()
    out: dict[str, list[dict]] = {}
    for L in sorted(lessons, key=lambda x: (x["skill"], x["ord"])):
        sec = by_key.get(L["key"])
        heading_low = (L["title"] or "").lower()
        items: list[dict] = []
        for kp in L["key_points"] + L["mistakes"]:
            if len(kp["text"].split()) < MIN_SOURCE_WORDS:
                continue
            cite = _cite(L, kp["cite"])
            got = _cloze_number(L, kp, cite, pools, heading_low)
            if not got:
                got = _cloze_term(L, kp, cite, pools, heading_low, idx.idf)
            items += got
            items += _tf(L, kp, cite)
        if sec is not None:
            items += _order(L, sec, pools)
        items += _scenarios(L, objections)
        items += _spot(L, pools)
        items += _write(L, write_done)
        kept = []
        for it in items:
            if it["norm_key"] in seen:
                continue
            if it["kind"] in CHOICE_KINDS and not _unique_options(it["options"] or []):
                continue
            if it["checks"].get("answer_in_source") is False:
                continue
            # A3: a right answer is never text the guardrails would block ("following up" out of context)
            ans = " ".join(it["answer"]) if isinstance(it["answer"], list) else str(it["answer"])
            if it["kind"] not in ("spot", "write", "tf") and guardrails.blocked(
                    guardrails.check_for(tenant, ans, {}) if tenant is not None else guardrails.check(ans, "plain")):
                continue
            seen.add(it["norm_key"])
            kept.append(it)
        # keep the mix varied when a lesson has many candidates
        if len(kept) > MAX_ITEMS_PER_LESSON:
            by_kind: dict[str, list[dict]] = defaultdict(list)
            for it in kept:
                by_kind[it["kind"]].append(it)
            mixed: list[dict] = []
            while len(mixed) < MAX_ITEMS_PER_LESSON and any(by_kind.values()):
                for k in list(by_kind):
                    if by_kind[k] and len(mixed) < MAX_ITEMS_PER_LESSON:
                        mixed.append(by_kind[k].pop(0))
            kept = mixed
        out[L["key"]] = kept
    # Questions that always stay in practice (write-it) show their cited line once answered: every question
    # on that line stays in practice too, so the exam pool never shares a line with a practice question.
    practice_only = {source_key(it["citation"], it["norm_key"]) for items in out.values() for it in items
                     if it["kind"] == "write"}
    for items in out.values():
        for it in items:
            if it["pool"] == "exam" and source_key(it["citation"], it["norm_key"]) in practice_only:
                it["pool"] = "practice"
    return out


# --------------------------------------------------------------------------- reading items
def load(tenant, item_id: int) -> dict | None:
    with tenant.db() as c:
        r = c.execute("SELECT * FROM academy_quiz_items WHERE id=?", (item_id,)).fetchone()
    if r is None:
        return None
    return _row(dict(r))


def _row(r: dict) -> dict:
    r["options"] = db.jload(r.get("options"), None)
    r["answer"] = db.jload(r.get("answer"), r.get("answer"))
    r["citation"] = db.jload(r.get("citation"), None)
    r["checks"] = db.jload(r.get("checks"), {})
    r["accept"] = db.jload(r.get("accept"), [])
    return r


def public(item: dict) -> dict:
    """What a learner may see before answering: never the answer, the explanation or the citation
    (the cited line would give a cloze away)."""
    out = {"id": item["id"], "kind": item["kind"], "kind_label": KIND_LABEL.get(item["kind"], item["kind"]),
           "prompt": item["prompt"], "skill": item.get("skill"), "skill_label": common.skill_label(item.get("skill") or ""),
           "lesson_id": item.get("lesson_id")}
    if item["kind"] in CHOICE_KINDS or item["kind"] == "order":
        out["options"] = item["options"]
    if item["kind"] == "write":
        out["asset"] = (item["answer"] or {}).get("asset")
    return out


# --------------------------------------------------------------------------- grading
def grade(tenant, item: dict, answer) -> dict:
    """-> {correct, score 0-100, chosen_option, feedback}. Raises ValueError on an answer of the wrong shape."""
    kind = item["kind"]
    exp = item["answer"]
    if kind == "order":
        if not isinstance(answer, list) or not all(isinstance(x, str) for x in answer) or len(answer) > 20:
            raise ValueError("An ordering answer is a list of the options in your order.")
        ok = [T.norm_cmp(x) for x in answer] == [T.norm_cmp(x) for x in exp]
        return {"correct": ok, "score": 100.0 if ok else 0.0, "chosen_option": " | ".join(answer)[:300], "feedback": None}
    if not isinstance(answer, str):
        raise ValueError("The answer must be text.")
    answer = answer.strip()[:4000]
    if kind in CHOICE_KINDS:
        opts = item["options"] or []
        if answer not in opts and T.norm_cmp(answer) not in {T.norm_cmp(o) for o in opts}:
            raise ValueError("Pick one of the options.")
        ok = T.norm_cmp(answer) == T.norm_cmp(str(exp))
        return {"correct": ok, "score": 100.0 if ok else 0.0, "chosen_option": answer[:300], "feedback": None}
    if kind == "cloze":
        ok = T.same_answer(answer, str(exp), item.get("accept") or [])
        return {"correct": ok, "score": 100.0 if ok else 0.0, "chosen_option": answer[:300], "feedback": None}
    if kind == "write":
        return _grade_write(tenant, item, answer)
    raise ValueError(f"Unknown question type {kind}")


def write_threshold(tenant) -> float:
    return float(common.DEFAULT_THRESHOLDS["L2"]["write_score"])


# --------------------------------------------------------------------------- write-it: a real attempt first
# The rubric scores the craft of a draft (one question, short, gives value, plain words, guardrails). On its own it
# gives "zzz qqq www" 100 as a DM: three nonsense words break none of those rules. A write-it answer is mastery and
# L2 certificate evidence, so the rubric only counts once the draft is a real attempt at the task: the length of the
# thing asked for (not a wall of text), written as a message rather than a keyword list or one word repeated, not the
# task, the feedback or a course line pasted back, and carrying the parts the task asks for.
WRITE_SHAPE: dict[str, dict] = {
    "dm": {"label": "DM", "min_words": 8, "max_words": 80, "min_questions": 1},
    "followup": {"label": "follow-up", "min_words": 8, "max_words": 80},
    "reminder": {"label": "reminder", "min_words": 8, "max_words": 80, "names_the_call": True},
    "usp": {"label": "USP", "min_words": 10, "max_words": 60, "usp": True},
    "ad": {"label": "ad hook", "min_words": 4, "max_words": 40},
    "email": {"label": "email", "min_words": 12, "max_words": 250, "subject": True},
}
GENERIC_SHAPE = {"label": "draft", "min_words": 5, "max_words": 300}
NOT_AN_ATTEMPT_CAP = 30.0
PASTE_SHARE = 0.7          # this share of the draft's words comes from one task, feedback or course line
PASTE_SHARE_ALL = 0.9      # ... or from all of them together
_WORD = re.compile(r"[A-Za-z0-9$£€][\w'’$%-]*")
_FUNCTION = {"i'm", "it's", "you're", "we're", "what's", "that's", "here's", "there's", "hi", "hey", "am", "i've",
             "i'd", "i'll", "you'll", "can't", "don't", "won't", "isn't", "let's",
             # learners also write in French, Spanish and Portuguese: their function words count too
             "de", "la", "le", "les", "el", "los", "las", "un", "una", "une", "des", "du", "que", "qui", "y", "et", "e",
             "en", "es", "est", "para", "por", "pour", "con", "com", "avec", "o", "os", "um", "uma", "je", "tu", "vous",
             "yo", "mi", "su", "se", "não", "no", "você", "nous", "ce", "como", "comment"}
_USP_SHAPE = re.compile(r"\bi help\s+(.+?)\s+(?:get|achieve|reach|hit|land|book|fill)\s+(.+?)\s+(?:in|within)\s+(.+?)\s+"
                        r"(?:through|with|using|by)\s+(.+?)\s+without\s+(\S.*)", re.I | re.S)
_CALL_WORDS = re.compile(r"\b(call|meeting|session|chat|appointment|consult\w*|zoom)\b", re.I)
_SUBJECT = re.compile(r"^\s*subject\s*:\s*\S", re.I | re.M)
# drafts that make the rubric print its fixes, so a fix pasted back as an answer is recognised
_FIX_PROBES = ("", "Hi? How are you? What now?", "word " * 130, "Subject: Re: following up\nJust checking in.",
               "Hey, I'm going to show you how businesses grow. Click here, book a call, reply today.",
               "In today's fast-paced world, let's delve into the synergy of seamless growth.")


@functools.lru_cache(maxsize=64)
def _feedback_texts(asset: str) -> tuple[str, ...]:
    """Every label and fix the rubric for this asset can show a learner."""
    out: list[str] = []
    for rule in rubrics.get(asset):
        out.append(rule.label)
        for probe in _FIX_PROBES:
            try:
                fix = rule.check(probe, {"mode": "plain", "meta": {}, "banned": []})[2]
            except Exception:  # noqa: BLE001 - a probe that a rule can't read just adds nothing
                continue
            if fix:
                out.append(fix)
    return tuple(dict.fromkeys(out))


def _lesson_lines(tenant, item: dict) -> list[str]:
    if tenant is None or not item.get("lesson_id"):
        return []
    with tenant.db() as c:
        r = c.execute("SELECT key_points, mistakes, example FROM academy_lessons WHERE id=?", (item["lesson_id"],)).fetchone()
    if r is None:
        return []
    return [k.get("text", "") for col in ("key_points", "mistakes", "example") for k in (db.jload(r[col], []) or [])
            if isinstance(k, dict)]


def attempt_problems(tenant, item: dict, text: str) -> list[str]:
    """Why this draft isn't a real attempt at the write-it task yet ([] when it is)."""
    asset = (item.get("answer") or {}).get("asset", "generic")
    shape = WRITE_SHAPE.get(asset, GENERIC_SHAPE)
    label = shape["label"]
    words = [w.lower().strip("'’-.") for w in _WORD.findall(text or "")]
    words = [w for w in words if w]
    n = len(words)
    probs: list[str] = []
    if n < shape["min_words"]:
        probs.append(f"Too short to be a real {label}: write at least {shape['min_words']} words, the way you'd send it.")
    elif n > shape["max_words"]:
        probs.append(f"Far too long for a {label} ({n} words): keep it under {shape['max_words']} words.")
    if n >= 4:
        function_words = sum(1 for w in words if w in textnorm.STOP or w in _FUNCTION)
        if function_words == 0 or (n >= 8 and function_words / n < 0.15):
            probs.append("This reads like a list of keywords, not a message. Write it the way you'd send it.")
        elif n >= 6 and len(set(words)) / n < 0.5:
            probs.append("The same few words over and over: write a real message.")
    mine = set(textnorm.tokens(text or ""))
    if len(mine) >= 3:
        sources = [item.get("prompt") or "", (item.get("citation") or {}).get("snippet") or ""]
        sources += list(_feedback_texts(asset)) + _lesson_lines(tenant, item)
        toks = [set(textnorm.tokens(s)) for s in sources if s]
        every = set().union(*toks) if toks else set()
        if any(len(mine & st) / len(mine) >= PASTE_SHARE for st in toks if st) or \
                len(mine & every) / len(mine) >= PASTE_SHARE_ALL:
            probs.append("This repeats the task, the feedback or a line from the lesson instead of answering it. "
                         "Write your own message.")
    if text.count("?") < shape.get("min_questions", 0):
        probs.append(f"A {label} asks one question to start the conversation; this one asks none.")
    if shape.get("names_the_call") and not _CALL_WORDS.search(text):
        probs.append("Say which call the reminder is for.")
    if shape.get("usp"):
        m = _USP_SHAPE.search(text)
        if not m or not all(any(t not in textnorm.STOP for t in re.findall(r"[a-z0-9]+", g.lower())) for g in m.groups()):
            probs.append("Fill in every part of the formula: I help [who] get [outcome] in [timeframe] through [mechanism] "
                         "without [pain].")
    if shape.get("subject"):
        body = " ".join(ln for ln in (text or "").splitlines() if not re.match(r"^\s*subject\s*:", ln, re.I))
        if not _SUBJECT.search(text or "") or len(body.split()) < 6:
            probs.append("Write a subject line (Subject: ...) and the opening lines under it.")
    return probs


def _attempt_criterion(problems: list[str]) -> dict:
    return {"label": "A real attempt at the task", "score": 0, "fix": " ".join(problems), "quote": "", "citations": []}


def _grade_write(tenant, item: dict, text: str) -> dict:
    from .. import critique

    spec = item["answer"] or {}
    asset = spec.get("asset", "generic")
    meta = dict(spec.get("meta") or {})
    problems = attempt_problems(tenant, item, text)
    if len(text.split()) < 3:
        return {"correct": False, "score": 0.0, "chosen_option": text[:300],
                "feedback": {"criteria": [_attempt_criterion(problems)], "flags": [], "citations": [], "attempt": problems,
                             "note": "Write at least a full sentence."}}
    res = rubrics.score(text, asset, meta, guardrails.tenant_banned_names(tenant), tenant=tenant)
    flags = res["flags"]
    cites, by_key = critique.evidence(tenant, res["criteria"])
    crit = [{"label": c["label"], "score": c["score"], "fix": c["fix"], "quote": c["quote"],
             "citations": by_key.get(c["key"], [])} for c in res["criteria"]]
    score = float(res["score"])
    if guardrails.blocked(flags):
        score = min(score, 40.0)
    if problems:
        score = min(score, NOT_AN_ATTEMPT_CAP)
        crit.insert(0, _attempt_criterion(problems))
    ok = score >= write_threshold(tenant) and not guardrails.blocked(flags) and not problems
    return {"correct": ok, "score": score, "chosen_option": None,
            "feedback": {"criteria": crit, "flags": flags, "citations": cites, "attempt": problems, "mode": "rules"}}


# --------------------------------------------------------------------------- item analytics
FLAG_MIN_ATTEMPTS = 30


def item_stats(tenant, item_ids: list[int] | None = None, min_attempts: int = FLAG_MIN_ATTEMPTS) -> list[dict]:
    """p-value and point-biserial discrimination on each person's first attempt; auto-flags when
    p > 0.95, p < 0.2, r_pb < 0.1 (after `min_attempts` attempts) or 3+ 'report question' clicks."""
    with tenant.db() as c:
        att = c.execute("SELECT user_id, quiz_item_id, correct, chosen_option FROM academy_attempts "
                        "WHERE quiz_item_id IS NOT NULL ORDER BY id").fetchall()
        items = {r["id"]: dict(r) for r in c.execute(
            "SELECT i.id, i.kind, i.prompt, i.answer, i.status, i.pool, i.skill, i.lesson_id, i.reported, i.exposures, "
            "l.title lesson_title FROM academy_quiz_items i LEFT JOIN academy_lessons l ON l.id=i.lesson_id "
            "WHERE i.status IN ('published','draft')")}
    first: dict[tuple[int, int], dict] = {}
    for a in att:
        k = (a["user_id"], a["quiz_item_id"])
        if k not in first:
            first[k] = dict(a)
    user_tot: dict[int, list[int]] = defaultdict(list)
    for (u, _), a in first.items():
        user_tot[u].append(1 if a["correct"] else 0)
    by_item: dict[int, list[dict]] = defaultdict(list)
    for (u, i), a in first.items():
        by_item[i].append(a)
    out = []
    for iid, it in items.items():
        if item_ids is not None and iid not in item_ids:
            continue
        rows = by_item.get(iid, [])
        n = len(rows)
        p = sum(1 for r in rows if r["correct"]) / n if n else None
        rpb = None
        if n >= 3 and p not in (None, 0.0, 1.0):
            xs, ys = [], []
            for r in rows:
                tot = user_tot[r["user_id"]]
                rest = (sum(tot) - (1 if r["correct"] else 0)) / max(1, len(tot) - 1) if len(tot) > 1 else 0.0
                xs.append(1 if r["correct"] else 0)
                ys.append(rest)
            rpb = _pbis(xs, ys)
        wrong = Counter(r["chosen_option"] for r in rows if not r["correct"] and r["chosen_option"])
        flags = []
        if n >= min_attempts and p is not None:
            if p > 0.95:
                flags.append("too easy (p > 0.95)")
            if p < 0.2:
                flags.append("too hard or wrong key (p < 0.2)")
            if rpb is not None and rpb < 0.1:
                flags.append("doesn't separate strong from weak learners (r < 0.1)")
        if (it["reported"] or 0) >= 3:
            flags.append(f"reported {it['reported']} times")
        out.append({"id": iid, "kind": it["kind"], "kind_label": KIND_LABEL.get(it["kind"], it["kind"]),
                    "prompt": it["prompt"], "lesson_id": it["lesson_id"], "lesson_title": it["lesson_title"],
                    "skill": it["skill"], "pool": it["pool"], "status": it["status"], "attempts": n,
                    "p_value": None if p is None else round(p, 3), "discrimination": None if rpb is None else round(rpb, 3),
                    "top_wrong": wrong.most_common(1)[0][0] if wrong else None, "reported": it["reported"] or 0,
                    "flags": flags})
    return out


def _pbis(xs: list[int], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    m = sum(ys) / n
    sd = math.sqrt(sum((y - m) ** 2 for y in ys) / n)
    if sd == 0:
        return 0.0
    g1 = [y for x, y in zip(xs, ys) if x]
    g0 = [y for x, y in zip(xs, ys) if not x]
    if not g1 or not g0:
        return None
    p = len(g1) / n
    return (sum(g1) / len(g1) - sum(g0) / len(g0)) / sd * math.sqrt(p * (1 - p))
