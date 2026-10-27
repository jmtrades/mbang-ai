"""Text helpers for lesson and quiz generation: sentences, number words, units, answer normalisation."""
from __future__ import annotations

import math
import re

from .. import textnorm

# --------------------------------------------------------------------------- sentences
_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'“(])")


def sentences(line: str) -> list[str]:
    return [s.strip() for s in _SPLIT.split(line or "") if s.strip()]


def is_full_sentence(s: str, min_words: int = 6) -> bool:
    s = s.strip()
    return len(s.split()) >= min_words and (s[-1] in ".!?" or ":" in s)


def norm_space(s: str) -> str:
    s = (s or "").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", s).strip()


def norm_cmp(s: str) -> str:
    """Lower-case, collapsed, punctuation-light form used for 'appears verbatim' and uniqueness checks."""
    s = norm_space(s).lower()
    s = re.sub(r"[^\w\s%$.'-]", " ", s)
    return re.sub(r"\s+", " ", s).strip(" .")


# --------------------------------------------------------------------------- disfluencies (transcripts)
DISFLUENCY = {"uh", "um", "er", "erm", "uhh", "umm", "[inaudible]", "inaudible"}
_REPEAT = re.compile(r"\b(\w+)(?:\s+\1\b)+", re.I)


def clean_spoken(line: str) -> str:
    words = [w for w in (line or "").split() if w.strip(",.").lower() not in DISFLUENCY]
    out = " ".join(words)
    out = _REPEAT.sub(r"\1", out)
    return re.sub(r"\s+([,.!?])", r"\1", out).strip()


# --------------------------------------------------------------------------- numbers
UNITS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
         "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
         "seventeen": 17, "eighteen": 18, "nineteen": 19}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
SCALES = {"hundred": 100, "thousand": 1000, "million": 1_000_000}
_NUMWORD = set(UNITS) | set(TENS) | set(SCALES)
_TOKEN = re.compile(r"[A-Za-z]+|\d[\d,]*(?:\.\d+)?|[^\sA-Za-z\d]+|\s+")


def _parse_words(words: list[str]) -> float | None:
    total, cur, seen = 0.0, 0.0, False
    for w in words:
        if w in UNITS:
            cur += UNITS[w]
        elif w in TENS:
            cur += TENS[w]
        elif w == "hundred":
            cur = (cur or 1) * 100
        elif w in ("thousand", "million"):
            total += (cur or 1) * SCALES[w]
            cur = 0
        else:
            return None
        seen = True
    return total + cur if seen else None


def words_to_digits(text: str) -> str:
    """'seventy-two hours' -> '72 hours'; 'one and a half times' -> '1.5 times'; digits are left alone."""
    s = (text or "").replace("-", " - ")
    toks = _TOKEN.findall(s)
    out: list[str] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        if t.lower() in _NUMWORD:
            words, j, last = [], i, i
            prev = None  # kind of the previous number word: unit | tens | scale
            while j < len(toks):
                w = toks[j].lower()
                if w in _NUMWORD:
                    kind = "unit" if w in UNITS else "tens" if w in TENS else "scale"
                    # "three five" or "twenty thirty" are two numbers, not one
                    if prev == "unit" and kind != "scale":
                        break
                    if prev == "tens" and kind == "tens":
                        break
                    if prev == "tens" and kind == "unit" and UNITS[w] >= 10:
                        break
                    words.append(w)
                    last = j
                    prev = kind
                    j += 1
                elif w.isspace() or w == "-":
                    j += 1
                elif w == "and" and prev == "scale":
                    j += 1
                else:
                    break
            val = _parse_words(words)
            if val is not None:
                # "one and a half"
                rest = "".join(toks[last + 1:last + 8]).lower()
                m = re.match(r"\s+and\s+a\s+half\b", rest)
                if m:
                    val += 0.5
                    consumed = len(m.group(0))
                    acc, k = 0, last + 1
                    while k < len(toks) and acc < consumed:
                        acc += len(toks[k])
                        k += 1
                    last = k - 1
                out.append(fmt_num(val))
                # keep the whitespace that followed the number phrase
                i = last + 1
                continue
        out.append(t)
        i += 1
    return re.sub(r"\s+-\s+", "-", "".join(out)).replace(" - ", "-")


def fmt_num(v: float) -> str:
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return f"{v:g}"


UNIT_ALIASES = {
    "h": "hour", "hr": "hour", "hrs": "hour", "hour": "hour", "hours": "hour",
    "min": "minute", "mins": "minute", "minute": "minute", "minutes": "minute",
    "sec": "second", "secs": "second", "second": "second", "seconds": "second",
    "d": "day", "day": "day", "days": "day", "week": "week", "weeks": "week", "month": "month", "months": "month",
    "year": "year", "years": "year", "%": "percent", "percent": "percent", "pct": "percent", "per cent": "percent",
    "$": "dollar", "dollar": "dollar", "dollars": "dollar", "usd": "dollar", "bucks": "dollar",
}
COUNT_NOUNS = ["creative", "impression", "word", "time", "segment", "reminder", "call", "message", "question", "stage",
               "lead", "booking", "send", "chair", "attempt", "spot", "step", "follow-up", "opener", "version", "piece",
               "bonus", "dm", "move", "rule", "objection", "client", "patient", "variable"]
for _n in COUNT_NOUNS:
    UNIT_ALIASES[_n] = _n
    UNIT_ALIASES[_n + "s"] = _n
UNIT_PLURAL = {"hour": "hours", "minute": "minutes", "second": "seconds", "day": "days", "week": "weeks",
               "month": "months", "year": "years", "percent": "percent", "dollar": "dollars"}

_UNIT_WORDS = sorted({k for k in UNIT_ALIASES if k not in ("$", "%")}, key=len, reverse=True)
NUM_UNIT = re.compile(
    r"(?P<dollar>\$\s?)?(?P<num>\d[\d,]*(?:\.\d+)?)(?:\s?(?P<k>[kK])\b)?\s?(?:-\s?)?"
    r"(?P<unit>%|(?:" + "|".join(re.escape(u) for u in _UNIT_WORDS) + r")\b)?", re.I)


def unit_of(raw: str | None, dollar: bool = False) -> str | None:
    if dollar:
        return "dollar"
    if not raw:
        return None
    return UNIT_ALIASES.get(raw.lower().strip())


def find_quantities(text: str) -> list[dict]:
    """Numbers with their unit in already-digitised text: [{value, unit, start, end, raw}]."""
    out = []
    for m in NUM_UNIT.finditer(text or ""):
        try:
            v = float(m.group("num").replace(",", ""))
        except ValueError:
            continue
        if m.group("k"):
            v *= 1000
        unit = unit_of(m.group("unit"), bool(m.group("dollar")))
        out.append({"value": v, "unit": unit, "start": m.start(), "end": m.end(), "raw": m.group(0).strip()})
    return out


_NUMW = "|".join(sorted(_NUMWORD, key=len, reverse=True))
_ORIG_NUM = re.compile(
    r"(?<![\w$])(?:\$\s?)?(?:\d[\d,]*(?:\.\d+)?(?:\s?[kK]\b)?|(?:(?:" + _NUMW + r")(?:[\s-]+(?:and\s+a\s+half\b|and\s+)?)?)+)"
    r"\s?-?\s?(?:%|(?:" + "|".join(re.escape(u) for u in _UNIT_WORDS) + r")\b)?", re.I)


def quantities_in(text: str) -> list[dict]:
    """Number phrases (digits or words) with a unit, as spans of the ORIGINAL text:
    [{value, unit, start, end, raw}]. Only phrases that carry a unit are returned."""
    out = []
    for m in _ORIG_NUM.finditer(text or ""):
        raw = m.group(0).rstrip(" -")
        start = m.start()
        # "three and five creatives" is two numbers: keep the one next to the unit
        for am in reversed(list(re.finditer(r"\s+and\s+(?!a\s+half)", raw, re.I))):
            before = raw[:am.start()].split()[-1].lower() if raw[:am.start()].split() else ""
            if before not in ("hundred", "thousand", "million"):
                start += am.end()
                raw = raw[am.end():]
                break
        if not raw.strip():
            continue
        q = find_quantities(words_to_digits(raw))
        if not q:
            continue
        q = q[-1]
        if q["unit"] is None:
            continue
        out.append({"value": q["value"], "unit": q["unit"], "start": start, "end": start + len(raw), "raw": raw.strip()})
    return out


def fmt_quantity(v: float, unit: str | None) -> str:
    if unit == "dollar":
        return f"${fmt_num(v)}"
    if unit == "percent":
        return f"{fmt_num(v)}%"
    if not unit:
        return fmt_num(v)
    if v == 1:
        return f"1 {unit}"
    return f"{fmt_num(v)} {UNIT_PLURAL.get(unit, unit + 's')}"


def nice(v: float) -> float:
    """Round to a 'nice' number for padded distractors."""
    if v <= 0:
        return 1.0
    if v < 10:
        return float(round(v * 2) / 2) if v < 3 else float(round(v))
    mag = 10 ** int(math.floor(math.log10(v)))
    step = mag / 2 if v / mag < 3 else mag
    return float(round(v / step) * step)


# --------------------------------------------------------------------------- answer normalisation
def answer_tokens(text: str) -> set[str]:
    """Stemmed, compound-folded tokens of an answer, with number words turned into digits and units aliased."""
    s = words_to_digits(text or "")
    toks = set()
    for t in textnorm.tokens(s):
        toks.add(UNIT_ALIASES.get(t, t))
    return toks


def same_answer(given: str, expected: str, accept: list[str] | None = None) -> bool:
    """Free-text answer match: identical numbers (unit may be omitted), or the expected key tokens all present
    (synonyms count)."""
    cands = [expected] + list(accept or [])
    g = norm_cmp(words_to_digits(given))
    if not g:
        return False
    gq = find_quantities(words_to_digits(given))
    for exp in cands:
        eq = find_quantities(words_to_digits(exp))
        if eq:
            want = eq[0]
            for q in gq:
                if abs(q["value"] - want["value"]) < 1e-9 and (q["unit"] is None or want["unit"] is None or q["unit"] == want["unit"]):
                    return True
            continue
        et = answer_tokens(exp)
        if not et:
            if norm_cmp(exp) == g:
                return True
            continue
        gt = answer_tokens(given)
        syn = textnorm._syn()
        ok = True
        for t in et:
            if t in gt:
                continue
            if any(s in gt for s in syn.get(t, [])) or any(t in syn.get(x, []) for x in gt):
                continue
            ok = False
            break
        if ok:
            return True
    return False
