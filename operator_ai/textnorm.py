"""Tokenising for retrieval: domain compounds, a light stemmer, stopwords and synonyms."""
from __future__ import annotations

import re

# Programme compounds are folded into single tokens before tokenising.
COMPOUNDS: list[tuple[re.Pattern, str]] = [
    (re.compile(p, re.I), r) for p, r in [
        (r"\bno[\s-]?shows?\b", " noshow "),
        (r"\bopt[\s-]?ins?\b", " optin "),
        (r"\bthunder[\s-]?domes?\b", " thunderdome "),
        (r"\bshow[\s-]?up rates?\b|\bshow[\s-]?rates?\b", " showrate "),
        (r"\bfollow[\s-]?ups?\b|\bfollowing up\b", " followup "),
        (r"\bclick[\s-]?through[\s-]?rates?\b|\bctr\b", " ctr "),
        (r"\bcost[\s-]?per[\s-]?leads?\b|\bcpl\b", " cpl "),
        (r"\bcost[\s-]?per[\s-]?acquisitions?\b|\bcpa\b", " cpa "),
        (r"\bpre[\s-]?call\b", " precall "),
        (r"\bcalls? to action\b|\bctas?\b", " cta "),
        (r"\bvideo sales letters?\b|\bvsls?\b", " vsl "),
        (r"\bdirect messages?\b|\bdms?\b", " dm "),
        (r"\bunique selling (proposition|point)s?\b|\busps?\b", " usp "),
        (r"\bcreative fatigue\b", " creativefatigue "),
        (r"\bkill rule\b", " killrule "),
        (r"\breply rates?\b", " replyrate "),
        (r"\bclose rates?\b|\bclosing rates?\b", " closerate "),
        (r"\bdone[\s-]with[\s-]you\b", " donewithyou "),
        (r"\bhigh[\s-]ticket\b", " highticket "),
        (r"\bpaid ads?\b", " paidads "),
        (r"\bfifty to a hundred k\b|\$?50k?\s?(-|to)\s?\$?100k", " agreedrange "),
        (r"\btwenty[\s-]four hours\b|\b24 ?h(ours)?\b", " 24h hours "),
        (r"\btwo hours\b|\b2 ?h(ours)?\b", " 2h hours "),
        (r"\bten minutes\b|\b10 ?min(utes)?\b", " 10min minutes "),
        (r"\bseventy[\s-]two hours\b|\b72 ?h(ours)?\b", " 72h hours "),
        (r"\bforty[\s-]eight hours\b|\b48 ?h(ours)?\b", " 48h hours "),
        (r"\bone thousand impressions\b|\b1,?000 impressions\b", " 1000impressions impressions "),
    ]
]

STOP = set("""a an the and or but if of to in on at for from by with without about into over under is are was were be been
being do does did doing have has had having i you he she it we they me him her us them my your his its our their this that
these those what which who whom whose when where why how all any both each few more most other some such no nor not only own
same so than too very can will just should could would may might must shall s t don now get got go going there here then
also up down out off again further once like okay ok alright yeah um uh so well really gonna wanna let lets let's thing things
one two three want make sure way much many""".split())

SYNONYMS: dict[str, list[str]] = {
    "price": ["cost", "expensive", "fee", "pricing"],
    "expensive": ["price", "cost"],
    "kill": ["killrule", "switch", "off"],
    "killrule": ["kill"],
    "prospect": ["lead"],
    "lead": ["prospect"],
    "sign": ["symptom", "show"],
    "contain": ["include", "add"],
    "soon": ["fast", "quick", "within"],
    "broad": ["wide", "narrow"],
    "delete": ["archive", "remove"],
    "handle": ["deal", "objection"],
    "happen": ["book", "within"],
    "winner": ["champion", "best", "win"],
    "remind": ["reminder", "ladder"],
    "reminder": ["remind", "ladder"],
    "objection": ["think", "expensive", "partner"],
    "opener": ["opening", "first", "move"],
    "webinar": ["segment", "masterclass", "live"],
    "niche": ["market", "audience", "who"],
    "partner": ["spouse", "wife", "husband"],
    "spouse": ["partner"],
    "creative": ["ad", "hook", "creatives"],
    "ad": ["creative", "ads"],
    "ads": ["creative", "ad"],
    "show": ["showrate", "attend"],
    "attend": ["show", "showrate"],
    "book": ["booking", "booked", "calendar"],
    "close": ["closerate", "closing", "sale"],
    "sell": ["close", "offer"],
    "test": ["thunderdome", "testing"],
    "followup": ["follow", "nudge"],
    "message": ["dm", "messages"],
    "dm": ["message", "inbox"],
    "brief": ["precall"],
    "deadline": ["scarcity", "cap"],
    "replay": ["recording"],
    "email": ["subject", "sequence"],
    "budget": ["spend", "dollars"],
    "rule": ["rules"],
    "long": ["duration", "minimum", "hours"],
    "duration": ["long", "minimum"],
}

_TOKEN = re.compile(r"[a-z0-9]+")


ER_KEEP = {"offer", "after", "never", "other", "over", "under", "order", "answer", "number", "water", "paper", "power",
           "ever", "either", "rather", "whether", "together", "consider", "remember", "deliver", "register", "partner",
           "cluster", "filter", "letter", "master", "matter", "member", "reminder", "corner", "border", "folder", "later",
           "better", "buyer", "header", "chapter", "quarter", "minister", "trigger", "printer", "cover", "discover",
           "ladder", "user", "tier", "lower", "higher", "longer", "fewer", "closer", "setter", "owner", "trainer"}


def stem(w: str) -> str:
    if len(w) <= 3 or w.isdigit():
        return w
    if w.endswith("s") and not w.endswith("ss") and w[:-1] in ER_KEEP:
        return w[:-1]
    if w in ER_KEEP:
        return w
    for suf, rep in (("ational", "ate"), ("ization", "ize"), ("fulness", "ful"), ("iveness", "ive"),
                     ("ingly", ""), ("edly", ""), ("ies", "y"), ("ied", "y"), ("ing", ""), ("ed", ""),
                     ("ers", ""), ("er", "")):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            w = w[: -len(suf)] + rep
            break
    else:
        if w.endswith(("sses", "xes", "zes", "ches", "shes")) and len(w) > 4:
            w = w[:-2]
        elif w.endswith("s") and not w.endswith("ss") and len(w) > 3:
            w = w[:-1]
    if w.endswith("ly") and len(w) > 5:
        w = w[:-2]
    if len(w) >= 4 and w[-1] == w[-2] and w[-1] not in "lsz":
        w = w[:-1]
    if len(w) > 4 and w.endswith("e"):
        w = w[:-1]
    return w


def fold(text: str) -> str:
    t = text.lower().replace("’", "'")
    for pat, rep in COMPOUNDS:
        t = pat.sub(rep, t)
    return t


def tokens(text: str, keep_stop: bool = False) -> list[str]:
    out = []
    for w in _TOKEN.findall(fold(text)):
        if not keep_stop and w in STOP:
            continue
        out.append(stem(w))
    return out


_SYN: dict[str, list[str]] | None = None


def _syn() -> dict[str, list[str]]:
    global _SYN
    if _SYN is None:
        _SYN = {}
        for k, vs in SYNONYMS.items():
            _SYN.setdefault(stem(k), []).extend(stem(v) for v in vs)
    return _SYN


def expand(toks: list[str]) -> list[tuple[str, float]]:
    """Query tokens with weights: originals 1.0, synonyms 0.4."""
    seen: dict[str, float] = {}
    for t in toks:
        seen[t] = max(seen.get(t, 0), 1.0)
    syn = _syn()
    for t in list(seen):
        for s in syn.get(t, []):
            if s not in seen:
                seen[s] = 0.4
    return list(seen.items())


# Common English words: when the corpus doesn't use one, it says little about whether the
# question is on-curriculum (unlike a rare word such as "tiktok" or "shopify").
COMMON = {stem(w) for w in """handle contain soon sign broad happen start deal include use work keep better good best
quick fast right wrong mean means know tell say said show look find give take put help try ask call feel time year
month week day today tomorrow people person thing stuff lot little big small new old first last next long short high
low many much more most less least few enough every each same different kind type part whole full able possible
example really actually basically also just even still already ever maybe probably usually often sometimes always
never again around away back down off out over under through between before after during while since until
idea point problem question answer reason result case fact way place group number level side end line""".split()}
