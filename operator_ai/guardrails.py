"""Guardrails: one pure function family.

    check(text, mode, meta=None, banned_names=None, tenant=None) -> [flags]

Modes: "outreach" (prospect-facing: money rule applies), "client_asset" (a student's own
VSL/pages/emails: their price may appear), "plain" (everything else). A flag is
{"code", "severity": "block"|"review", "message", "match"}. Any "block" flag stops an item
from being approved until it is edited clean. "review" flags are non-blocking: the item is
marked "needs a person".

The same function runs when an item is queued, again on approval (after edits), inside
self-critique, inside coaching line-fixes and on coach answers.

Text is normalised before any rule runs, so the usual dodges don't work: invisible (Unicode Cf)
characters are removed, look-alike letters from other scripts and accents are folded to plain
Latin, spaced-out words ("g u a r a n t e e") are joined and a de-leeted copy ("gu4rant33d") is
checked too. Negation only counts when the negator governs the match (a few words before it, in
the same clause): "No pressure, but this offer won't last" and "Not only do we guarantee" still
flag.
"""
from __future__ import annotations

import html
import re
import unicodedata
from typing import Iterable

MODES = ("outreach", "client_asset", "plain")

_SENT_SPLIT = re.compile(r"(?<=[.!?\n])\s+")

# --------------------------------------------------------------------------- normalisation
# Look-alike letters (Cyrillic, Greek, Armenian, IPA) that render like Latin letters.
_CONFUSABLES = str.maketrans({
    "а": "a", "в": "b", "е": "e", "ё": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p", "с": "c", "т": "t",
    "у": "y", "х": "x", "ѕ": "s", "і": "i", "ї": "i", "ј": "j", "ԁ": "d", "ԛ": "q", "ԝ": "w", "һ": "h", "ӏ": "l",
    "А": "A", "В": "B", "Е": "E", "Ё": "E", "К": "K", "М": "M", "Н": "H", "О": "O", "Р": "P", "С": "C", "Т": "T",
    "У": "Y", "Х": "X", "Ѕ": "S", "І": "I", "Ї": "I", "Ј": "J", "Ԁ": "D", "Ԛ": "Q", "Ԝ": "W", "Һ": "H", "Ӏ": "I",
    "α": "a", "β": "b", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p", "τ": "t", "υ": "u", "χ": "x",
    "γ": "y", "ϲ": "c", "ϳ": "j",
    "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K", "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P",
    "Τ": "T", "Υ": "Y", "Χ": "X", "Ϲ": "C",
    "օ": "o", "ս": "u", "ց": "g", "հ": "h", "ո": "n", "ռ": "n", "ա": "w",
    "ɑ": "a", "ɡ": "g", "ɩ": "i", "ı": "i", "ȷ": "j", "ʏ": "y", "ᴀ": "a", "ʙ": "b", "ᴄ": "c", "ᴅ": "d", "ᴇ": "e",
    "ɢ": "g", "ʜ": "h", "ɪ": "i", "ᴊ": "j", "ᴋ": "k", "ʟ": "l", "ᴍ": "m", "ɴ": "n", "ᴏ": "o", "ᴘ": "p", "ʀ": "r",
    "ᴛ": "t", "ᴜ": "u", "ᴠ": "v", "ᴡ": "w", "ᴢ": "z", "ꜱ": "s",
    "’": "'", "‘": "'", "‛": "'", "′": "'", "`": "'", "“": '"', "”": '"', "„": '"', "″": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2015": "-", "−": "-",
    "\ufe58": "-", "\ufe63": "-", "\uff0d": "-",
})
# "g u a r a n t e e", "g.u.a.r.a.n.t.e.e", "g-u-a-r-a-n-t-e-e": single letters joined by separators
_SPACED = re.compile(r"(?<![A-Za-z])(?:[A-Za-z][ .\-_*·•|/\\~+]){3,}[A-Za-z](?![A-Za-z])")
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})
_LEET_WORD = re.compile(r"[A-Za-z0-9@$]{4,}")


def _norm(text: str) -> str:
    """The text every rule sees: HTML entities decoded, NFKC, invisible characters removed, accents
    and look-alike letters folded to Latin, dashes and quotes unified, spaced-out words joined."""
    t = html.unescape(text or "")
    t = unicodedata.normalize("NFKC", t)
    t = "".join(ch for ch in t if unicodedata.category(ch) != "Cf")  # zero-width, bidi marks, soft hyphen
    t = unicodedata.normalize("NFKD", t)
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = unicodedata.normalize("NFKC", t.translate(_CONFUSABLES))
    t = re.sub(r"[\u00a0\u2000-\u200a\u202f\u205f\u3000]", " ", t)
    return _SPACED.sub(lambda m: re.sub(r"[^A-Za-z]", "", m.group(0)), t)


def _deleet(t: str) -> str:
    """A copy with digit/symbol look-alikes inside words turned back into letters ("gu4rant33d")."""
    def fix(m: re.Match) -> str:
        w = m.group(0)
        if re.search(r"[A-Za-z]", w) and re.search(r"[013457@$]", w):
            return w.translate(_LEET)
        return w
    return _LEET_WORD.sub(fix, t)


_INTRA_WORD = re.compile(r"(?<=[A-Za-z])[-_.·•*](?=[A-Za-z])")  # not apostrophes: "shouldn't" stays a negator


def _joined(t: str) -> str:
    """A copy with separators inside words removed ("Guar-antee", "gu.ar.an.tee" -> "Guarantee")."""
    return _INTRA_WORD.sub("", t)


# Unicode bidi overrides and isolates can make text display differently from what it says ("\u202eeetnaraug"
# shows as "guarantee"); ordinary right-to-left text never needs them.
_BIDI_CONTROLS = re.compile("[\u202a-\u202e\u2066-\u2069]")


def _money_norm(t: str) -> str:
    """Join digits split by spaces ("3 0 0 0", "3 000") so amounts can't hide from the money rule."""
    t = re.sub(r"(?<![\d.,])\d(?: \d){2,}(?![\d])", lambda m: m.group(0).replace(" ", ""), t)
    return re.sub(r"(?<=\d) (?=\d{3}(?!\d))", "", t)


# --------------------------------------------------------------------------- negation
# A negator only counts when it governs the match: in the same clause (no contrast word, comma, colon,
# dash or new subject in between) and within a few words. "Determiner" negators (no, without, nothing)
# only govern the phrase right after them; verbs like avoid/stop govern their direct object.
_NEG_AUX = r"never|not|don't|dont|do not|doesn't|doesnt|does not|didn't|did not|won't|wont|will not|isn't|aren't|" \
           r"wasn't|weren't|can't|cant|cannot|can not|shouldn't|should not|must not|mustn't|wouldn't|would not|" \
           r"(?:no one|no-one|nobody|none of us) (?:can|could|will|would|should|may|might)"  # "no one can guarantee results"
_NEG_OBJ = r"avoid|avoiding|stop|stopped|refuse|refuses|refused|ban|bans|banned|forbid|forbids|forbidden|prohibit|" \
           r"prohibits|prohibited|skip|ditch"
_NEG_DET = r"no|without|nothing|nobody|none|zero"
_NEG = re.compile(rf"\b(?:(?P<aux>{_NEG_AUX})|(?P<obj>{_NEG_OBJ})|(?P<det>{_NEG_DET}))\b", re.I)
_NOT_NEG = re.compile(r"\b(?:not only|not just|no doubt|no wonder|no more|no longer|nothing but|can'?t wait|cannot wait|"
                      r"don'?t wait|do not wait|don'?t miss|do not miss|never miss|not to mention|no need|can'?t lose|cannot lose|"
                      r"can'?t fail|cannot fail|can'?t go wrong|cannot go wrong|won'?t regret|will not regret)\b", re.I)
# a negator that governs an interjection, not the claim: "Don't worry guaranteed results", "Not kidding, ...",
# "You can't lose with guaranteed results". Any of these words between the negator and the match means the
# negator is about something else.
_OBJ_PRONOUN = re.compile(r"\b(tell|telling|want|wanting|ask|asking|need|needing|expect|expecting|advise|advising|"
                          r"recommend|recommending|encourage|encouraging|push|pushing|pressure|pressuring|urge|urging|"
                          r"let|make|making|get|getting)\s+(?:you|them|him|her|us)\b", re.I)
_INTERJECT = {"worry", "worries", "worrying", "stress", "stressing", "panic", "hesitate", "fret", "sweat", "kid", "kidding",
              "joke", "joking", "lie", "lying", "fear", "regret"}
_CUT = re.compile(r"[,;:()\[\]]|\s-\s|\s--\s|\b(?:but|however|yet|although|though|except|instead|rather|whereas|while|"
                  r"unless|so|then|because|since|plus|meanwhile|we|i|you|they|he|she|it|our|my|your|their|us|we're|"
                  r"you're|they're|i'm|we've|i've|you'll|we'll|i'll)\b", re.I)
# words that don't move a match out of a negator's reach ("never put a guarantee", "don't use fake scarcity
# like saying ...", "don't want to guarantee")
_FILLER = {"a", "an", "the", "any", "or", "nor", "and", "ever", "even", "to", "be", "been", "being", "use", "using",
           "used", "say", "saying", "said", "like", "such", "as", "eg", "e.g", "example", "instance", "claim",
           "claiming", "promise", "promising", "make", "making", "write", "writing", "put", "putting", "add",
           "adding", "include", "including", "send", "sending", "post", "posting", "mention", "mentioning", "offer",
           "offering", "give", "giving", "fake", "false", "made", "up", "want", "wanting", "try", "trying", "really",
           "actually", "just", "phrases", "words", "lines", "things", "anything", "something", "type", "kind", "of",
           "these", "those", "that", "this", "kinds", "types", "ever."}


def negated(text: str, start: int) -> bool:
    """True when the match at `start` is governed by a negator in the same clause, e.g. "never plant
    questions", "we don't guarantee", "don't use fake scarcity like saying doors are closing"."""
    prefix = text[max(0, start - 120):start]
    cut = max(prefix.rfind("."), prefix.rfind("!"), prefix.rfind("?"), prefix.rfind("\n"))
    if cut >= 0:
        prefix = prefix[cut + 1:]
    prefix = re.sub(r"\bnever,\s*ever\b", "never ever", prefix, flags=re.I)
    # a pronoun that is the OBJECT of the governed verb is not a new subject: "I would never tell you to borrow
    # from family", "I don't want you to put it on a credit card" (spoken-call rules in the role-play simulator)
    prefix = _OBJ_PRONOUN.sub(lambda m: m.group(1) + " ", prefix)
    last_cut = 0
    for m in _CUT.finditer(prefix):
        last_cut = m.end()
    tail = prefix[last_cut:]
    skip = [(m.start(), m.end()) for m in _NOT_NEG.finditer(tail)]
    neg = None
    for m in _NEG.finditer(tail):
        if any(a <= m.start() < b for a, b in skip):
            continue
        neg = m
    if neg is None:
        return False
    if tail[neg.end():neg.end() + 1] == "-":
        return False  # a compound adjective ("never-ending", "can't-lose", "no-brainer") negates nothing
    between = [w.lower() for w in re.findall(r"[A-Za-z0-9'.]+", tail[neg.end():])]
    content = [w for w in between if w.strip(".") not in _FILLER]
    if any(w.strip(".") in _INTERJECT for w in content):
        return False
    if neg.group("aux"):
        return len(content) <= 2
    return len(content) == 0  # "no hassle, guaranteed" and "stop guessing, guaranteed" don't negate the match


def _sentence_at(text: str, start: int, end: int) -> str:
    s = max(text.rfind(".", 0, start), text.rfind("!", 0, start), text.rfind("?", 0, start), text.rfind("\n", 0, start))
    e_candidates = [i for i in (text.find(".", end), text.find("!", end), text.find("?", end), text.find("\n", end)) if i >= 0]
    e = min(e_candidates) if e_candidates else len(text)
    return text[s + 1:e + 1]


def _flag(code: str, severity: str, message: str, match: str = "") -> dict:
    return {"code": code, "severity": severity, "message": message, "match": match.strip()[:120]}


# --------------------------------------------------------------------------- shared amount patterns
CURRENCY_CHARS = "".join(ch for ch in map(chr, range(0x20000)) if unicodedata.category(ch) == "Sc")
_CUR = "[" + re.escape(CURRENCY_CHARS) + "]"
_ISO = (r"(?:USD|EUR|GBP|AUD|CAD|NZD|INR|CHF|JPY|CNY|RMB|SGD|HKD|AED|SAR|ZAR|MXN|BRL|SEK|NOK|DKK|PLN|PHP|NGN|KES|IDR|"
        r"MYR|THB|KRW|TRY|ILS|CZK|HUF|RON|EGP|PKR|BDT|VND|CLP|COP|ARS|PEN)")
_N = r"\d[\d,.']*"
# a whole number run: starts only where a run starts ("1,1,1,..." used to be scanned from every digit: quadratic)
_NS = r"(?<![\w,.'])\d[\d,.']*"
_NOT_MONEY_NOUN = (r"(?!\s*\+?\s*(?:followers?|subscribers?|views?|likes?|members?|people|patients?|clients?|leads?|"
                   r"impressions?|downloads?|students?|users?|customers?|fans?|listeners?|readers?|visitors?|calls?|"
                   r"bookings?|dms?|messages?|emails?|posts?|reviews?|steps?|words?|miles?|km|kg|lbs?|run|race)\b)")
_MULT = r"(?:k|m|mm|bn|grand|thousand|million|billion)"
_SPELLED = (r"(?:a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|"
            r"seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|a few|few|"
            r"several|couple|a couple of|multiple|tens of|hundreds of)")
# at most seven words ("two hundred and fifty"): unbounded, a long run of number words was scanned quadratically
_SPELLED_SEQ = rf"{_SPELLED}(?:[\s-]+(?:and\s+)?(?:{_SPELLED}|hundred)){{0,6}}"
_FIG = r"(?:three|four|five|six|seven|eight|nine|multi|multiple|\d+|high|low|mid)"
_CASH_WORD = r"(?:dollars?|bucks|euros?|quid|rupees?|pounds sterling|cents|usd|eur|gbp)"
AMOUNT = (rf"(?:{_CUR}\s*\d[\d,.']*(?:\s*{_MULT}\b)?|(?<![\d,.'])\d[\d,.']*\s*{_CUR}|\b{_ISO}\s*\d|{_NS}\s*{_ISO}\b|"
          rf"{_NS}\s*{_MULT}\b{_NOT_MONEY_NOUN}|{_NS}\s*{_CASH_WORD}\b|"
          rf"\b{_SPELLED_SEQ}[\s-]+(?:thousand|million|billion|grand|k|{_CASH_WORD})\b{_NOT_MONEY_NOUN}|"
          rf"\b{_FIG}[\s-]*figures?\b)")

# A bare amount with no currency, "k" or cash word ("made 40,000 last month", "bring in 25000 a month"): grouped
# thousands or 4+ digits, never a year on its own (2026), and only when what follows reads like money (the end of
# the clause, a period, "in sales", "profit" ...), never a count of something ("5000 cold calls").
_BARE = (r"(?<![\d,.'])(?!(?:19|20)\d\d(?![\d,.' ]?\d))(?:\d{1,3}(?:[,.' ]\d{3})+(?:[.,]\d{1,2})?|\d{4,}(?:[.,]\d{1,2})?)"
         r"(?![\d%])")
_BARE_FOLLOW = (r"(?=\s*(?:[.!?,;:)\n]|$|\b(?:last|this|next|per|a|an|each|every|in|on|from|with|through|off|since|after|"
                r"before|within|by|monthly|weekly|yearly|annually|daily|profit|revenue|income|cash|net|gross|total|alone|"
                r"already|now|today|selling|doing|or\s+more|plus|and|just|only|working)\b))")
_EARN = (r"\b(?:make|made|makes|making|earn(?:s|ed|ing)?|generat(?:e|es|ed|ing)|bank(?:s|ed|ing)?|clear(?:s|ed|ing)?|"
         r"net(?:s|ted|ting)?|gross(?:es|ed|ing)?|pull(?:s|ed|ing)?\s+in|bring(?:s|ing)?\s+in|brought\s+in|"
         r"take\s+home|took\s+home|taking\s+home|averag(?:e|es|ed|ing)|doing)\b")
_EARN_QUAL = (r"(?:(?:up\s+to|over|more\s+than|as\s+much\s+as|at\s+least|an?\s+extra|about|around|nearly|almost|"
              r"roughly|close\s+to|well\s+over)\s+)?")
BARE_INCOME = rf"{_EARN}\s+{_EARN_QUAL}{_BARE}{_BARE_FOLLOW}"

# --------------------------------------------------------------------------- fake proof / deception
_SUBJ = (r"\b(?:our |my |the |most |many |all |some |these |those |every |of our )*(?:students?|clients?|members?|"
         r"customers?|people|graduates?|operators?|users?|coaches|partners|they|we|i|you|everyone|everybody)\b")
_VERB = (r"\b(?:make|made|makes|making|earn|earns|earned|earning|generate[ds]?|generating|hit|hits|hitting|"
         r"clear(?:s|ed|ing)?|bank(?:s|ed|ing)?|pull(?:s|ed|ing)?\s+in|net(?:s|ted|ting)?|gross(?:es|ed|ing)?|"
         r"take\s+home|took\s+home|bring(?:s|ing)?\s+in|brought\s+in|scale[ds]?\s+to|scaling\s+to|reach(?:es|ed)?|"
         r"averag(?:e|es|ed|ing)|doing|cross(?:ed|es)?|surpass(?:ed|es)?|crack(?:ed|s)?|replace[ds]?\s+(?:their|my|your)\s+"
         r"(?:income|salary))\b")
_INCOME_QUAL = (r"up\s+to\s+|over\s+|more\s+than\s+|as\s+much\s+as\s+|at\s+least\s+|an?\s+extra\s+|your\s+first\s+|"
                r"another\s+|a\s+steady\s+|consistent\s+")

# (code, pattern, message). All block, all negation-aware.
DECEPTION_RULES: list[tuple[str, re.Pattern, str]] = [
    ("guarantee", re.compile(
        # the stem tolerates the usual misspellings: garanteed, guaranteeed, guaranty, guaranties
        # (gaurantee, garuntee, gurantee, guarentee, garanteed, guaranteeed ...)
        r"\bg(?:u?a+u?|u)r+[aeu]+n+t+(?:e+[ed]*s?|e+ing|y|ies)\b|\bmoney[- ]?back\b|\brisk[- ]?free\b|"
        r"\bwarrant(?:y|ies)\b|\b(?:we|i)\s+promise\s+(?:you\s+)?(?:results|success|clients|sales|leads|bookings|"
        r"revenue|growth|profit)\b|\b100\s?%\s+(?:success(?:\s+rate)?|satisfaction|results?|certain|sure)\b|"
        r"\bno[- ]questions?[- ]asked\b|(?<!non-)(?<!non )\b(?:fully\s+)?refundable\b|"
        r"\b(?:no|zero|without any|without)[- ]risk\b|\b(?:full|100\s?%|complete|total|guaranteed)\s+refunds?\b|"
        r"\brefund(?:s|ed)?\b[^.!?\n]{0,40}\b(?:if|unless|when)\b[^.!?\n]{0,40}(?:\bnot\b|n't\b|\bunhappy\b|\bfail|"
        r"\bno results\b|\bsatisf)|\b(?:get|give you|have)\s+(?:all\s+)?(?:your|the)\s+money\s+back\b|"
        r"\bor\s+(?:you\s+)?(?:don'?t|do not|won'?t)\s+pay\b|\bpay\s+nothing\s+(?:unless|until|if)\b|"
        r"\bor\s+(?:it'?s|its)\s+free\b|\bpromise\s+(?:that\s+)?you(?:'ll|\s+will)?\s+(?:get|make|hit|earn|see|double|"
        r"triple|land|close|reach)\b|\b(?:results|success)\s+(?:are\s+|is\s+)?(?:100\s?%\s+)?(?:assured|certain|ensured)\b",
        re.I),
     "Guarantees, refunds-if-it-fails and money-back promises aren't allowed"),
    # "let me make [Brand] ..." is the range line's shape (judged by the outreach money rule), not a subject's income
    ("income_claim", re.compile(rf"{_SUBJ}[^.!?\n]{{0,40}}?(?<!let me )(?<!let us )(?<!let's ){_VERB}"
                                rf"[^.!?\n]{{0,25}}?{AMOUNT}", re.I),
     "Income claims about students, clients or yourself aren't allowed"),
    # "Our clients made 40,000 last month": the same claim with a bare number
    ("income_claim", re.compile(rf"{_SUBJ}[^.!?\n]{{0,40}}?(?<!let me )(?<!let us )(?<!let's ){BARE_INCOME}", re.I),
     "Income claims about students, clients or yourself aren't allowed"),
    ("income_claim", re.compile(r"(?<![\w,.'])\d{2,4}\s?k\s?\+|(?:" + _CUR + r"\s*)?(?<![\w,.'])\d[\d,.]*\s*k\+?\s*months\b", re.I),
     "Income claims like \"100K+\" or \"$30k months\" aren't allowed"),
    # "earn $10k a month", "make up to $10k", "make your first $10k" (a plain "clinics that make $1M a year"
    # describing the audience is not a promise, so "make" needs one of the qualifiers)
    ("income_claim", re.compile(
        rf"\b(?:earn|earning|bank|banking)\s+(?:{_INCOME_QUAL})?{AMOUNT}|"
        rf"\b(?:make|making|clear|clearing|take home)\s+(?:{_INCOME_QUAL}){AMOUNT}", re.I),
     "Income promises (\"earn up to $10k a month\") aren't allowed"),
    ("fake_testimonial", re.compile(
        r"\b(fake|invent(ed|ing)?|made[- ]up|make up|fabricat\w*|write (a|some) (fake )?)\b[^.!?\n]{0,30}"
        r"\b(testimonials?|reviews?|case stud(y|ies)|results?|screenshots?|success stor(y|ies))\b", re.I),
     "Invented testimonials, reviews, case studies or results aren't allowed"),
    ("unverified_result", re.compile(
        r"\b(student|client|member|customer)s? (named |called )?[A-Z][a-z]+ (went from|made|hit|closed|doubled|tripled|10x'?d|grew)\b"),
     "A result about a named person needs verified proof and permission; remove it or attach the proof"),
    ("fake_shoutout", re.compile(
        r"\b(?i:shout[- ]?outs?) (?i:to )?[A-Z][a-z]+\b[^.!?\n]{0,40}\b(booked|joined|signed|closed|bought)\b|"
        r"\b[A-Z][a-z]+ (just|has just) (booked|joined|signed up|bought|enrolled)\b"),
     "Booking or sign-up shout-outs with names can't be verified here"),
    ("planted_question", re.compile(
        r"\bplant(ed|ing|s)?\b[^.!?\n]{0,25}\bquestions?\b|\bseed(ed|ing)? the chat\b|"
        r"\b(fake|dummy|second|burner|sock ?puppet|alt) (chat |zoom |ig |instagram )?accounts?\b|"
        r"\bpretend(ing)? to be an? (attendee|customer|student|viewer|lead|buyer)\b|\binvent(ed|ing)? attendees?\b", re.I),
     "Planted audience questions or fake chat accounts aren't allowed"),
    ("invented_credential", re.compile(
        r"\b(say|tell (them|people|the lead|prospects)|claim|mention) (that )?(you'?ve|you have|you) "
        r"(closed|made|generated|done|sold|managed|scaled)\b|\bpretend (you|to have)\b", re.I),
     "Invented credentials aren't allowed; only state what is true"),
    ("fake_scarcity", re.compile(
        r"\bdoors? (are |is )?(clos(e|es|ing)|shutting)\b|\bwon'?t last\b|\blast chance\b|\bselling out\b|"
        r"\bbefore it'?s gone\b|\bonly (a )?(few|handful of) (spots|places|seats)( left)?\b|\bhurry\b|\bact now\b|"
        r"\bnow or never\b|\balmost (sold out|gone|full)\b|\b(spots|seats|places) (are )?(filling|running out|going) fast\b|"
        r"\btime is running out\b|\bonce it'?s gone\b", re.I),
     "Fake scarcity (\"doors closing\", \"won't last\", \"last chance\") isn't allowed"),
]

_DEADLINE = re.compile(
    r"\b(deadline|clos(es|ing) (on|at|this|tonight|tomorrow|friday|monday|sunday|saturday)|"
    r"enrol(l)?ment (closes|ends)|registration (closes|ends)|ends (tonight|tomorrow|on|this)|until (friday|midnight|sunday)|"
    r"cap(ped)? (it |the cohort |numbers )?at \d+|limited to \d+|only \d+ (spots|places|seats)( left)?|"
    r"starts on the \w+|offer (ends|expires))\b",
    re.I,
)
_REASON = re.compile(
    r"\b(because|since|so that|so (our|the|each|every) |due to|as (we|our|the)|in order to|which (is|means)|"
    r"the reason|to keep|to make sure|so coaches|so we can)\b",
    re.I,
)

_SUBJECT_REPLY = re.compile(r"^\s*subject\s*:\s*(re|fwd?|fw)\s*:", re.I | re.M)
_FIRST_TOUCH_LIE = re.compile(
    r"\b(following up|just following up|as promised|per our (call|conversation|chat)|as we discussed|"
    r"circling back|bumping (this|my last)|my last (message|email))\b",
    re.I,
)

# --------------------------------------------------------------------------- outreach money rule
_ALLOWED_RANGE = re.compile(
    r"\$\s?50\s?[kK]\s?(?:-|to)\s?\$\s?100\s?[kK]\s?(?:a|per|/)\s?(?:month|mo)\b", re.I)
# the only sentence shape that may carry the range: "Let me (help) make <brand> $50k-$100k a month through ..."
_RANGE_SHAPE = re.compile(r"\b(?:let me|let us|let's)\s+(?:help\s+)?make\s+(?P<brand>[^.!?\n]{1,80}?)\s*$", re.I)
_RANGE_PERSON = re.compile(r"^(?:you|yourself|them|him|her|me|us|students?|clients?|members?|people|graduates?|"
                           r"customers?|everyone|anyone)\b", re.I)
# a person anywhere in the [Brand] slot ("Acme owners", "your clients", "Acme and you") makes it personal income
_RANGE_PERSON_ANY = re.compile(r"\b(?:you|yourself|yourselves|owners?|founders?|personally|him|her|them|me|us|students?|"
                               r"clients?|members?|people|graduates?|customers?|everyone|anyone|coaches|operators?|"
                               r"family|families)\b", re.I)
_PERIOD = r"(?:/\s*|per\s+|a\s+|an\s+|each\s+|every\s+)(?:mo|mth|month|yr|year|annum|week|wk|day|hour|hr|session|call|client)s?\b"
_MONEY_RX = [
    re.compile(rf"{_CUR}\s*\d"),                                                   # $3000, $  3000, € 3.000, ₹50000
    # (anchored at the start of a digit run: unanchored, a long run of digits made this quadratic)
    re.compile(rf"(?<![\d,.'])\d[\d,.']*\s?{_CUR}"),                               # 3000€, 3.000 €
    re.compile(rf"\b{_ISO}\s*\d|{_NS}\s*{_ISO}\b", re.I),                          # USD 3000, 3000 usd
    re.compile(r"\bRs\.?\s*\d", re.I),
    re.compile(rf"{_NS}\s*{_MULT}\b{_NOT_MONEY_NOUN}", re.I),                     # 30k, 10 thousand, 2 grand
    re.compile(rf"{_NS}\s*{_CASH_WORD}\b", re.I),                                 # 3000 dollars, 500 bucks
    re.compile(rf"\b{_SPELLED_SEQ}[\s-]+(?:thousand|million|billion|grand|k|{_CASH_WORD})\b{_NOT_MONEY_NOUN}", re.I),
    re.compile(rf"\b{_FIG}[\s-]*figures?\b", re.I),                                # six figures, 7-figure
    re.compile(r"\b(?:pay|paying|paid|invest|investing|spend|spending|charge|charging|costs?|priced?|fee|rate|budget)"
               r"\s+(?:is\s+|of\s+|only\s+|just\s+|about\s+|around\s+|roughly\s+|under\s+|over\s+|at\s+)*\d", re.I),
    re.compile(r"(?<![\w,.'])\d[\d,.']{2,}\s*(?:to (?:start|join|begin|get started|enrol+|sign up)|up ?front|down|deposit)\b", re.I),
    re.compile(rf"{_CUR}{{2,}}"),                                                  # "make $$$"
    re.compile(rf"\b(?:only|just)\s+\d[\d,.']{{2,}}\b{_NOT_MONEY_NOUN}", re.I),   # "only 1997"
    re.compile(r"\b\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?(?!\d)(?!\s*[A-Za-z])"),      # a bare "2,997." with nothing counted
    re.compile(rf"{_EARN}\s+(?:you\s+|them\s+|your\s+\w+\s+)?{_EARN_QUAL}{_BARE}{_BARE_FOLLOW}", re.I),  # "made 40000 last month"
    # a bare price: "the program is 2997", "it's 1997 today", "join for 997 today", "1997 gets you in"
    re.compile(r"\b(?:program(?:me)?|course|coaching|offer|package|membership|mentorship|enrol+ment|access|it|that|this|"
               r"investment|call)(?:'s|\s+(?:is|costs?|runs?|comes\s+to|will\s+be|would\s+be))\s+(?:just\s+|only\s+|about\s+|"
               r"around\s+|roughly\s+)?(?!(?:19|20)\d\d\b)\d{3,}(?:[.,]\d{2})?(?![\d%])"
               r"(?=\s*(?:[.!?,;)\n]|$|\b(?:today|now|upfront|up\s+front|total|flat|one[- ]time|all[- ]in|in\s+full|to\s+join|"
               r"to\s+start|a\s+month|per|monthly|for|and|plus|with)\b))", re.I),
    re.compile(r"\bfor\s+(?:just\s+|only\s+)?\d{3,}(?:[.,]\d{2})?\s+(?:today|now|upfront|up\s+front|total|flat|one[- ]time|"
               r"all[- ]in|in\s+full)\b", re.I),
    re.compile(r"\b\d{3,}(?:[.,]\d{2})?\s+gets\s+you\b", re.I),
]
_PER_PERIOD = re.compile(rf"(?<![\w,.'])(?P<n>\d[\d,.']*)\s*(?P<k>k\b)?\s*{_PERIOD}", re.I)
# "40,000 monthly", "25000 last month", "40,000 in their first month": a period word after a big bare number
_PER_PERIOD_WORDS = re.compile(
    rf"{_BARE}\s*(?:monthly|weekly|yearly|annually|(?:last|this|next|every|each)\s+(?:month|year|quarter|week)|"
    rf"in\s+(?:a|one|their|your|the|his|her|my|our)\s+(?:first\s+)?(?:month|year|quarter|week))\b", re.I)
_PRICE_WORDS = re.compile(
    r"\b(price[sd]?|pricing|fees?|retainers?|investment|payment plans?|deposit|discount(ed|s)?|per month|"
    r"monthly fee|charge[sd]?|costs? (you|just|only)|it costs?|the cost of (the|our|my) (programme|program|service|offer))\b",
    re.I,
)
_HEDGE = re.compile(r"\b(let me|let us|let's|could|can|help|aim|potentially|might|would)\b", re.I)
_DEFINITE = re.compile(
    r"\b(will (add|make|get|bring|generate|deliver)|adds|guaranteed?|you'?ll (make|get|add|hit)|is going to|"
    r"are going to)\b", re.I)


def mode_for(meta: dict | None) -> str:
    """The outreach rule wins when an item is both prospect-facing and a client asset."""
    meta = meta or {}
    if meta.get("prospect_facing"):
        return "outreach"
    if meta.get("client_asset"):
        return "client_asset"
    return meta.get("mode") if meta.get("mode") in MODES else "plain"


_INVISIBLE = "[\u00ad\u200b-\u200f\u2060-\u2064\ufeff]*"


def _name_patterns(names: Iterable[str]) -> list[tuple[str, re.Pattern]]:
    """Whole-word, case-insensitive; tolerant of invisible characters and spacing inside the name."""
    out = []
    for n in names or []:
        if not isinstance(n, str):
            continue
        n = " ".join(n.split())
        if len(n) < 2:
            continue
        parts = []
        for ch in n:
            parts.append(r"\s+" if ch == " " else re.escape(ch) + _INVISIBLE)
        out.append((n, re.compile(r"(?<![\w])" + "".join(parts) + r"(?![\w])", re.I)))
    return out


def scrub(text: str, names: Iterable[str]) -> str:
    """Remove every banned name from text shown in the UI or generated output."""
    s = text or ""
    for _, pat in _name_patterns(names):
        s = pat.sub("[name removed]", s)
    return s


def scrub_json(value, names: Iterable[str]):
    """Scrub string values in decoded JSON (dict keys and non-strings are left alone, so a banned word
    that happens to equal a key or a literal like "status" or "false" never corrupts a response)."""
    pats = _name_patterns(names)
    if not pats:
        return value

    def walk(v):
        if isinstance(v, str):
            for _, pat in pats:
                v = pat.sub("[name removed]", v)
            return v
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        return v
    return walk(value)


# Feature rule plug-ins, "module:function", called as fn(text, mode, meta, tenant) -> list[flag] with the
# normalised text; tenant is None when check() runs without one. A module that does not exist yet is skipped.
# Each feature owns its own rules module so rules from parallel work never collide in this file.
RULE_PLUGINS = [
    "operator_ai.persona:guard_rules",     # impersonation, invented autobiography, persona in outreach, attribution
    "operator_ai.prospects:guard_rules",   # personalisation must cite researched facts
    "operator_ai.delivery:guard_rules",    # prospect email needs an unsubscribe line and a postal address
    "operator_ai.claims:guard_rules",      # result and revenue figures need a substantiation record
]
_plugins: list | None = None


def _rule_plugins() -> list:
    global _plugins
    if _plugins is None:
        import importlib

        found = []
        for spec in RULE_PLUGINS:
            mod, _, fn = spec.partition(":")
            try:
                f = getattr(importlib.import_module(mod), fn, None)
            except ModuleNotFoundError as e:
                if e.name != mod:
                    raise
                f = None
            if f is not None:
                found.append(f)
        _plugins = found
    return _plugins


def _first_unnegated(pat: re.Pattern, views: list[str]) -> re.Match | None:
    for v in views:
        for m in pat.finditer(v):
            if negated(v, m.start()):
                continue
            return m
    return None


def check(text: str, mode: str = "plain", meta: dict | None = None, banned_names: Iterable[str] | None = None,
          tenant=None) -> list[dict]:
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode}")
    meta = meta or {}
    t = _norm(text)
    flags: list[dict] = []

    # banned names: block and the UI scrubs them
    for name, pat in _name_patterns([_norm(n) for n in (banned_names or []) if isinstance(n, str)]):
        if pat.search(t):
            flags.append(_flag("banned_name", "block", "Mentions a name on the banned list", "[name removed]"))

    # every deception rule runs on the normalised text and on a de-leeted copy, in every mode: the
    # allowed outreach range is never exempt from the income-claim rule
    views = [t]
    for v in (_deleet(t), _joined(t), _joined(_deleet(t))):
        if v not in views:
            views.append(v)
    if _BIDI_CONTROLS.search(html.unescape(text or "")):
        flags.append(_flag("hidden_characters", "block" if mode in ("outreach", "client_asset") else "review",
                           "Direction-control characters can make text display differently from what it says; "
                           "remove them", "[hidden characters]"))
    for code, pat, msg in DECEPTION_RULES:
        m = _first_unnegated(pat, views)
        if m:
            flags.append(_flag(code, "block", msg, m.group(0)))

    # deadlines and capacity limits: a stated reason passes, none -> needs a person
    for m in _DEADLINE.finditer(t):
        if negated(t, m.start()):
            continue
        sent = _sentence_at(t, m.start(), m.end())
        if not _REASON.search(sent):
            flags.append(_flag("deadline_no_reason", "review",
                               "A deadline or capacity limit needs a stated reason; a person should check it", m.group(0)))
            break

    # deceptive subject lines / first-touch lies
    if not meta.get("prior_contact"):
        m = _SUBJECT_REPLY.search(t)
        if m:
            flags.append(_flag("deceptive_subject", "block", "\"Re:\"/\"Fwd:\" on a first touch is deceptive", m.group(0)))
        m = _FIRST_TOUCH_LIE.search(t)
        if m and not negated(t, m.start()):
            flags.append(_flag("deceptive_subject", "block",
                               "\"Following up\" on a first touch pretends there was earlier contact", m.group(0)))

    if mode == "outreach":
        flags.extend(_outreach_money(t, meta))
    else:
        # "let me make <person> <amount>" is exempt from the income rule only as the range line about a brand, which
        # the outreach money rule judges; outside outreach nothing else would, so a person's income here is a claim
        flags.extend(_income_promise(t))
        if mode == "client_asset":
            # client assets reach prospects too (proposals, webinar pages and emails, the client's own funnel): the
            # range keeps its rules there, and needs a proof record like anywhere else (claims.guard_rules)
            mt = _money_norm(t)
            tier = meta.get("lead_tier") or meta.get("tier")
            flags.extend(_range_flags(mt, list(_ALLOWED_RANGE.finditer(mt)), str(tier).lower() if tier else None))
    for rule in _rule_plugins():
        flags.extend(rule(t, mode, meta, tenant) or [])
    return _dedupe(flags)


def money_hit(text: str) -> re.Match | None:
    """The first amount in (normalised) text: any currency symbol or code next to a number, k / thousand /
    grand / figures, spelled-out amounts, "<number> a month", "pay 2500"."""
    for rx in _MONEY_RX:
        m = rx.search(text)
        if m:
            return m
    m = _PER_PERIOD_WORDS.search(text)
    if m:
        return m
    for m in _PER_PERIOD.finditer(text):
        try:
            n = float(re.sub(r"[,.']", "", m.group("n")) or 0)
        except ValueError:
            continue
        if m.group("k") or n >= 100:
            return m
    return None


_LET_MAKE = re.compile(rf"\b(?:let me|let us|let's)\s+(?:help\s+)?make\s+(?!sure\b|certain\b)(?P<slot>[^.!?\n]{{1,40}}?)"
                       rf"\s*{AMOUNT}", re.I)
_NOT_INCOME_SLOT = re.compile(r"\b(?:offer|deal|discount|price|quote|budget|deposit|payment|plan|invoice|refund)s?\b", re.I)


def _income_promise(t: str) -> list[dict]:
    for v in dict.fromkeys((t, _joined(t))):
        for m in _LET_MAKE.finditer(v):
            slot = m.group("slot")
            if negated(v, m.start()) or len(slot.split()) > 5 or _NOT_INCOME_SLOT.search(slot):
                continue
            if _RANGE_PERSON.search(slot.strip()) or _RANGE_PERSON_ANY.search(slot):
                return [_flag("income_claim", "block", "Promising a person an income (\"let me make you $...\") is an "
                                                       "income claim", m.group(0))]
    return []


def money_figure(text: str) -> str | None:
    """The first money figure in raw text (normalised the way the outreach rule sees it), or None. A surface that
    talks to strangers with nobody approving each answer (public minis) shows no figures at all: that is the cold-lead
    outreach rule, and it keeps income lines and the $50k-$100k range off it however the material phrases them."""
    m = money_hit(_money_norm(_norm(text or "")))
    return m.group(0) if m else None


def _outreach_money(t: str, meta: dict) -> list[dict]:
    flags = []
    tier = str(meta.get("lead_tier") or meta.get("tier") or "cold").lower()
    t = _money_norm(t)
    ranges = list(_ALLOWED_RANGE.finditer(t))
    stripped = _ALLOWED_RANGE.sub(" ", t)
    m = money_hit(stripped)
    if m:
        flags.append(_flag("outreach_money", "block",
                           "Prospect outreach may only mention \"$50k-$100k a month\" (hedged); no other amounts", m.group(0)))
    for m in _PRICE_WORDS.finditer(stripped):
        if negated(stripped, m.start()):
            continue
        flags.append(_flag("outreach_price", "block", "No price, fee or retainer wording in prospect outreach", m.group(0)))
        break
    return flags + _range_flags(t, ranges, tier)


def _range_flags(t: str, ranges: list[re.Match], tier: str | None) -> list[dict]:
    """The $50k-$100k range's own rules, wherever it appears: not for cold leads (when the tier is known: tier None
    skips that check), hedged, never definite, and only in the "Let me make [Brand] ..." shape about a business."""
    flags = []
    for m in ranges:
        sent = _sentence_at(t, m.start(), m.end())
        s = max(t.rfind(".", 0, m.start()), t.rfind("!", 0, m.start()), t.rfind("?", 0, m.start()), t.rfind("\n", 0, m.start()))
        shape = _RANGE_SHAPE.search(t[s + 1:m.start()])
        if tier is not None and tier in ("cold", "unqualified", "unknown", "nurture"):
            flags.append(_flag("cold_lead_figure", "block",
                               "Cold or unqualified leads get the no-figure line (\"Let me help [Brand] grow through ...\")",
                               m.group(0)))
        elif _DEFINITE.search(sent):
            flags.append(_flag("outreach_definite", "block", "The range must be hedged, never definite (\"will add\", \"adds\")",
                               _DEFINITE.search(sent).group(0)))
        elif not _HEDGE.search(sent):
            flags.append(_flag("outreach_unhedged", "block",
                               "Hedge the range: \"Let me make [Brand] $50k-$100k a month through ...\"", m.group(0)))
        elif not shape or _RANGE_PERSON.search(shape.group("brand").strip()) or \
                _RANGE_PERSON_ANY.search(shape.group("brand")):
            flags.append(_flag("outreach_range_shape", "block",
                               "The range may only appear as \"Let me make [Brand] $50k-$100k a month through ...\" "
                               "(a business's revenue, never a person's income)", m.group(0)))
    return flags


def _dedupe(flags: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for f in flags:
        k = (f["code"], f["match"].lower())
        if k in seen:
            continue
        seen.add(k)
        out.append(f)
    return out


def blocked(flags: list[dict]) -> bool:
    return any(f["severity"] == "block" for f in flags)


def needs_review(flags: list[dict]) -> bool:
    return any(f["severity"] == "review" for f in flags)


# --------------------------------------------------------------------------- helpers used by drafting code
AI_WORDS = [
    "delve", "unlock", "elevate", "game-changer", "game changer", "in today's fast-paced", "leverage", "synergy",
    "revolutionize", "revolutionise", "seamless", "cutting-edge", "tapestry", "embark", "supercharge", "skyrocket",
    "unleash", "dive deep", "harness the power", "look no further", "rest assured", "navigate the complexities",
]


def ai_words(text: str) -> list[str]:
    low = (text or "").lower()
    return [w for w in AI_WORDS if w in low]


def no_figure_line(brand: str, mechanism: str = "our system") -> str:
    return f"Let me help {brand} grow through {mechanism}."


def range_line(brand: str, mechanism: str = "our system", tenant=None) -> str:
    """The hedged range line, but only while the tenant holds a current substantiation record for it
    (claims.py). No tenant or no valid record -> the no-figure line (the default for every new workspace)."""
    try:
        from . import claims
    except ImportError:  # pragma: no cover - claims ships with this module
        return no_figure_line(brand, mechanism)
    if tenant is None or not claims.has_valid(tenant):
        return no_figure_line(brand, mechanism)
    return f"Let me make {brand} $50k-$100k a month through {mechanism}."


def _str_list(v) -> list[str]:
    """Settings written before validation existed may hold anything; only real names count."""
    if not isinstance(v, (list, tuple)):
        return []
    return [" ".join(x.split()) for x in v if isinstance(x, str) and len(x.strip()) >= 2]


def tenant_scrub_names(tenant) -> list[str]:
    """Banned names only (the owner's list plus OPERATOR_BANNED_NAMES): what the response scrub removes.
    Persona style words are enforced on generated text, never scrubbed from API responses."""
    import os

    names = _str_list(tenant.get("banned_names", [])) if tenant is not None else []
    env = os.environ.get("OPERATOR_BANNED_NAMES", "")
    names += [n.strip() for n in env.split(",") if len(n.strip()) >= 2]
    return list(dict.fromkeys(names))


def tenant_banned_names(tenant) -> list[str]:
    persona = tenant.get("persona", {}) if tenant is not None else {}
    words = _str_list((persona or {}).get("banned_words", [])) if isinstance(persona, dict) else []
    return list(dict.fromkeys(tenant_scrub_names(tenant) + words))


def check_for(tenant, text: str, meta: dict | None = None) -> list[dict]:
    """check() with the tenant's banned names and banned words, mode chosen from meta."""
    return check(text, mode_for(meta), meta, tenant_banned_names(tenant), tenant=tenant)


_banned_cache: dict[str, tuple[float, list[str]]] = {}


def banned_cached(tid: str) -> list[str]:
    """Banned names for a tenant id, cached for two seconds (used by the response scrub)."""
    import time

    from . import tenancy

    hit = _banned_cache.get(tid)
    if hit and hit[0] > time.time():
        return hit[1]
    try:
        names = tenant_scrub_names(tenancy.get(tid))
    except Exception:  # noqa: BLE001 - unknown tenant: the route itself answers 404
        names = []
    _banned_cache[tid] = (time.time() + 2.0, names)
    return names


def banned_cache_clear(tid: str) -> None:
    _banned_cache.pop(tid, None)
