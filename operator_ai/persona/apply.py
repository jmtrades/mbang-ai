"""Applying the persona to coach answers, only under a valid consent and only on consented surfaces.

Rules mode (AI off, B3): cited sentences are never edited, so [S#] line ranges stay exact. The system prefix line
("From the course material:") is replaced by an approved opener chosen by hash(question) % n, and at most one approved
closer or tag is added when the answer is fully covered. No phrase repeats within the user's last 5 answers. The
honest "not covered" answer is left exactly as it is.

AI mode (B4): the existing grounded prompt plus a style card (stylometric targets in plain words, approved phrases with
"at most one per answer", the never-use list) and, only under the external_ai_processing scope, 2-3 short excerpts of
the subject's own words. The output must pass, in order: citation check, a non-empty citation set, containment (each
cited sentence shares >= 0.35 of its content words with its passage; every number appears in a cited passage), no
12-token copy from the excerpts, the phrase cap, then the guardrails and persona guards. Any failure falls back to the
neutral grounded answer. Styled outputs are replayed under 'coach-q:p<version>' (purged on withdrawal or expiry);
neutral answers keep 'coach-q' and never read styled replays.

Honesty: whenever a persona name is configured, "are you <name>?" and requests for personal stories get "I'm an AI
assistant trained on <name>'s material", never a first-person biography.

Surfaces: a signed-in user's coach chat is "coach_answers"; minis set "public_minis"; channel replies, the phone line
and every other caller are neutral unless they set a surface. Voice, likeness and outreach are never surfaces.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar

from .. import db, guardrails, index, llm, textnorm
from . import attribution, consent
from .guards import AUTOBIO

log = logging.getLogger("operator.persona")
_UNSET = object()
_SURFACE: ContextVar = ContextVar("persona_surface", default=_UNSET)
_PREVIEW: ContextVar = ContextVar("persona_preview", default=None)
PREFIX = "From the course material:"
RECENT = 5
CITE_RE = re.compile(r"\[(S\d+)\]")
NUM_RE = re.compile(r"\d[\d,.]*")
SAMPLE_QUESTIONS = ["What should my first message to a prospect do?", "How should I follow up when someone goes quiet?",
                    "What do I do when a lead no-shows?", "How do I handle 'I need to think about it'?",
                    "How do I pick a niche?"]
STYLE_RULES = ("STYLE (wording only): follow the style card below for sentence length, directness and tone. Facts, "
               "numbers and advice come ONLY from the [S#] passages. Never claim to be a person, never tell first-person "
               "stories or results, never mention who the style comes from. Use at most one of the approved phrases, "
               "only if it fits naturally, and never any phrase on the never-use list.")


# --------------------------------------------------------------------------- surfaces
@contextmanager
def surface(name: str | None):
    """Run a block on a named surface ("public_minis", "lesson_framing", ...) or neutral (None)."""
    tok = _SURFACE.set(name)
    try:
        yield
    finally:
        _SURFACE.reset(tok)


def neutral():
    return surface(None)


def current_surface(user: dict | None) -> str | None:
    s = _SURFACE.get()
    if s is not _UNSET:
        return s
    return "coach_answers" if user else None


def retrieval_boost(tenant, idx, query: str | None = None) -> dict[int, float] | None:
    """+10% for chunks mostly said by the subject, so extractive answers reuse their own sentences (B3). Only while
    active (or in an owner preview), never on a neutral surface, only on a surface the subject consented to (a caller
    that sets no surface counts as coach answers), and only for answers that will be extractive: never with the AI on
    (a neutral AI answer, and its saved replay, is always written from the plain ranking) and never when a neutral
    replay exists for the question (its [S#] numbers belong to the plain ranking). None leaves retrieval unchanged."""
    try:
        s = _SURFACE.get()
        if s is None:
            return None
        if _PREVIEW.get() is None:
            p = consent.active(tenant)
            if p is None or ("coach_answers" if s is _UNSET else s) not in p["scopes"]:
                return None
        if llm.unavailable_reason(tenant) is None:
            return None
        if query and llm.replay(tenant, "coach-q", _normq(query)):
            return None
        shares = attribution.chunk_shares(tenant, idx)
        return {cid: 1.1 for cid, sh in shares.items() if sh >= 0.5} or None
    except Exception:  # noqa: BLE001 - never let styling break retrieval
        log.debug("persona boost skipped", exc_info=True)
        return None


# --------------------------------------------------------------------------- honesty
def _patterns(names: list[str]) -> tuple[list[re.Pattern], list[re.Pattern]]:
    alt = "|".join(re.escape(n).replace(r"\ ", r"\s+") for n in names)
    ident = [
        re.compile(rf"\b(are|r)\s+(you|u)\s+(really\s+|actually\s+|the\s+real\s+)?({alt})\b", re.I),
        re.compile(rf"\b(you'?re|you\s+are|ur)\s+(really\s+|actually\s+|the\s+real\s+)?({alt})\b(?!')", re.I),
        re.compile(rf"\b({alt})\W{{0,3}}\s*(is\s+(that|this|it)\s+(you|u)|(are|r)\s+(you|u)\s+(there|real|human))\b", re.I),
        re.compile(rf"\b(pretend|act|roleplay|role-play|role\s+play|speak|answer|talk|reply|respond|write|sign\s+off)"
                   rf"(\s+to\s+be|\s+as\s+if\s+you\s+(are|were)|\s+as|\s+like)\s+({alt})\b", re.I),
        re.compile(rf"\b(pretend|imagine)\s+(you'?re|you\s+are)\s+({alt})\b", re.I),
        re.compile(rf"\b(is\s+this|is\s+that|am\s+i\s+(talking|speaking|chatting)\s+(to|with))\s+(really\s+|actually\s+)?({alt})\b",
                   re.I),
        re.compile(r"\bwho\s+are\s+(you|u)\b", re.I),
        re.compile(r"\bare\s+(you|u)\s+(a\s+)?(real|human|person|bot|robot|an?\s+ai)\b", re.I),
        re.compile(r"\bam\s+i\s+(talking|speaking|chatting)\s+(to|with)\s+(a\s+)?(real|human|person|bot|robot|an?\s+ai)\b", re.I),
    ]
    auto = [
        re.compile(r"\b(tell\s+me|talk)\s+about\s+(your|ur)\s+((own|first|early|old|biggest|best)\s+)*(clients?|agency|"
                   r"business|company|story|journey|life|background|results?|income|first\s+sale|start)\b", re.I),
        re.compile(r"\b(how|when|why)\s+did\s+(you|u)\s+(start|begin|get\s+started|quit)\s*(your\b|ur\b|out\b|off\b|in\b|\?|$)|"
                   r"\b(how|when)\s+did\s+(you|u)\s+(find|land|sign|get|build|scale|grow)\s+(your|ur)\b", re.I),
        re.compile(r"\bwhat\s+(was|were)\s+(your|ur)\s+(first|biggest|best|worst|own)\b", re.I),
        re.compile(r"\b(your|ur)\s+(personal\s+)?(story|journey|backstory|background|first\s+client)\b", re.I),
        re.compile(r"\bhow\s+much\s+(money\s+|revenue\s+)?(do|did|have)\s+(you|u)\s+(make|made|earn|earned)\b", re.I),
        re.compile(r"\bwhat\s+did\s+(you|u)\s+do\s+before\b", re.I),
        re.compile(r"\bhave\s+(you|u)\s+(ever\s+)?(had|worked\s+with|coached|signed|landed|closed|built|run|ran|scaled|"
                   r"grown|made|lost)\s+(a|an|any|your|ur)\b", re.I),
    ]
    return ident, auto


def honesty_kind(question: str, names: list[str]) -> str | None:
    if not names:
        return None
    ident, auto = _patterns(names)
    q = (question or "").replace("’", "'")
    if any(p.search(q) for p in ident):
        return "identity"
    if any(p.search(q) for p in auto):
        return "autobio"
    return None


def _honest(tenant, res: dict, name: str, kind: str, run) -> dict:
    line = f"I'm an AI assistant trained on {name}'s material, not {name}."
    if kind == "autobio":
        line += " I don't have personal stories, clients or results of my own."
    text, keep_ids = line, []
    if kind == "autobio" and res.get("covered") == "yes":
        lines = [ln for ln in (res.get("answer") or "").split("\n") if ln.strip() and ln.strip() != PREFIX]
        kept = [ln for ln in lines if CITE_RE.search(ln) and not AUTOBIO.search(ln)]
        if kept:
            text += "\n\nHere's what the material itself says:\n" + "\n".join(kept)
            keep_ids = [i for ln in kept for i in CITE_RE.findall(ln)]
    cites = [c for c in res.get("citations", []) if c.get("id") in keep_ids]
    out = {**res, "answer": guardrails.scrub(text, guardrails.tenant_banned_names(tenant)), "citations": cites,
           "covered": res.get("covered") if cites else "no", "label": "Honest: an AI assistant, not a person",
           "persona": {"honesty": kind, "name": name, "styled": False}}
    if run is not None:
        run.note("persona_honesty", {"kind": kind}, {"kept_citations": [c["id"] for c in cites]},
                 label="Honesty: AI assistant, not the person")
    return out


# --------------------------------------------------------------------------- helpers
def _normq(q: str) -> str:
    return " ".join(textnorm.tokens(q, keep_stop=True))


def _recent(tenant, user: dict | None) -> set[str]:
    if not user:
        return set()
    with tenant.db() as c:
        rows = c.execute("SELECT phrase FROM persona_uses WHERE user_id=? ORDER BY id DESC LIMIT ?", (user["id"], RECENT)).fetchall()
    out: set[str] = set()
    for r in rows:
        out |= set(db.jload(r["phrase"], []) or [])
    return out


def _record(tenant, user: dict | None, version: int, used: list[str]) -> None:
    if not user:
        return
    with tenant.db() as c:
        c.execute("INSERT INTO persona_uses(user_id,version,phrase,created) VALUES(?,?,?,?)",
                  (user["id"], version, db.jdump(used), db.now()))


def _render(item: dict, role: str) -> str:
    d = " ".join((item.get("display") or item["phrase"]).split()).strip(" ,;")
    if not d:
        return ""
    d = d[0].upper() + d[1:]
    if d[-1] in ".!?:":
        return d
    if item.get("cls") == "tag":
        return d + "?"
    return d + (":" if role == "opener" else ".")


def _contains(text: str, phrase: str) -> bool:
    norm = " ".join(re.sub(r"[^\w'\s]", " ", (phrase or "").lower()).split())
    low = " ".join(re.sub(r"[^\w'\s]", " ", (text or "").lower().replace("’", "'")).split())
    return bool(norm) and bool(re.search(rf"(?<![\w']){re.escape(norm)}(?![\w'])", low))


def _when(ts: float) -> str:
    return time.strftime("%d %b %Y", time.localtime(ts))


def badge(p: dict) -> str:
    n = p["name"]
    return f"Styled after {n}'s teaching phrasing · AI assistant, not {n} · consent v{p['version']}, {_when(p['at'])}"


def _passages(tenant, res: dict) -> dict[str, str]:
    want = {(c.get("file"), c.get("lines")): c.get("id") for c in res.get("citations", [])}
    out = {}
    for ch in index.for_tenant(tenant).chunks:
        sid = want.get((ch.file, ch.lines_label))
        if sid:
            out[sid] = ch.text
    return out


def _subject_passages(tenant, res: dict, passages: dict[str, str]) -> list[str]:
    idx = index.for_tenant(tenant)
    shares = attribution.chunk_shares(tenant, idx)
    keyed = {(ch.file, ch.lines_label): ch.id for ch in idx.chunks}
    out = []
    for c in res.get("citations", []):
        cid = keyed.get((c.get("file"), c.get("lines")))
        if cid is not None and shares.get(cid, 0) >= 0.5 and c.get("id") in passages:
            out.append(passages[c["id"]])
    return out


def _new_blocks(tenant, styled: str, neutral_text: str, meta: dict) -> list[dict]:
    before = {(f["code"], f["match"].lower()) for f in guardrails.check_for(tenant, neutral_text, meta)}
    return [f for f in guardrails.check_for(tenant, styled, meta)
            if f["severity"] == "block" and (f["code"], f["match"].lower()) not in before]


# --------------------------------------------------------------------------- style card + excerpts
def style_card(p: dict) -> str:
    s = (p.get("profile") or {}).get("stylometrics") or {}
    lines = []
    if s.get("sentence_words_mean"):
        lines.append(f"- Sentences are short: about {s['sentence_words_mean']:.0f} words on average, rarely over "
                     f"{s.get('sentence_words_p90', 0):.0f}.")
    if s.get("question_rate", 0) >= 0.08:
        lines.append(f"- Asks the student a question in about {100 * s['question_rate']:.0f}% of sentences.")
    if s.get("you_per_100", 0) >= 3:
        lines.append("- Talks straight to the student: lots of 'you'.")
    if s.get("we_i_ratio", 0) >= 1.2:
        lines.append("- Prefers 'we' and 'let's' over 'I'.")
    if s.get("contraction_rate", 0) >= 0.03:
        lines.append("- Uses contractions (don't, you're, it's).")
    if s.get("imperative_rate", 0) >= 0.15:
        lines.append("- Gives direct instructions (\"Ask...\", \"Send...\").")
    if s.get("certainty_hedge_ratio", 0) >= 1.5:
        lines.append("- Direct and certain rather than hedged.")
    if s.get("exclamation_rate", 1) <= 0.02:
        lines.append("- Rarely uses exclamation marks.")
    approved = [a["display"] or a["phrase"] for a in p.get("approved", [])][:10]
    if approved:
        lines.append("- Approved phrases (use at most one per answer): " + "; ".join(f'"{a}"' for a in approved))
    if p.get("never"):
        lines.append("- Never use: " + "; ".join(f'"{n}"' for n in p["never"][:20]))
    return "STYLE CARD\n" + "\n".join(lines or ["- Plain, direct and short."])


def _excerpt_pool(tenant, p: dict) -> list[str]:
    """The subject's own turns that the signed profile was mined from, and nothing else: a turn qualifies only when its
    fingerprint is in the profile the subject consented to (covered by the profile hash), so later material, a
    re-marked document or a different speaker never reaches the provider as "their words"."""
    allowed = set((p.get("profile") or {}).get("excerpt_keys") or [])
    if not allowed:
        return []

    def _f():
        from . import mine

        try:
            col = mine.collect(tenant)
        except consent.PersonaError:
            return []
        return [x for x in mine.excerpt_candidates(col) if mine.excerpt_key(x) in allowed]

    return consent.cached(tenant, f"excerpts:{p['version']}:{p.get('consent_id')}", _f, ttl=300.0)


def excerpts(tenant, p: dict, question: str) -> list[str]:
    """2-3 of the subject's own turns (15-80 words, names and numbers already removed) closest to the question,
    at most 250 words in total. Only ever sent under the external_ai_processing scope."""
    q = set(textnorm.tokens(question))
    scored = []
    for text in _excerpt_pool(tenant, p):
        st = set(textnorm.tokens(text))
        scored.append((len(q & st) / (len(st) ** 0.5 or 1), text))
    scored.sort(key=lambda x: -x[0])
    out, words = [], 0
    for _, text in scored:
        n = len(text.split())
        if words + n > 250:
            continue
        out.append(text)
        words += n
        if len(out) == 3:
            break
    return out if len(out) >= 2 else out[:1]


# --------------------------------------------------------------------------- verification (B4 chain)
def verify(tenant, p: dict, text: str, cited: list[str], res: dict, passages: dict[str, str], excerpt_list: list[str],
           recent: set[str]) -> tuple[bool, str, str, list[str], list[str]]:
    from ..agents import coach

    allowed = set(passages)
    clean, kept, stripped = coach.verify_citations(text, cited, allowed)
    if stripped:
        return False, "cited a passage that wasn't retrieved", clean, kept, []
    if res.get("covered") != "no" and not kept:
        return False, "no citations", clean, kept, []
    if any(re.search(rf"(?<![\w']){re.escape(n)}(?![\w])", clean, re.I) for n in consent.names(tenant)):
        return False, "named the person", clean, kept, []
    # a sentence keeps the [S#] markers that follow it ("... goal. [S1] Next sentence.")
    all_src = set()
    for i in kept:
        all_src |= set(textnorm.tokens(passages.get(i, "")))
    framing = [a.get("display") or a["phrase"] for a in p.get("approved", [])]
    for sent in re.split(r"(?<=[.!?\]])\s+(?!\[S\d)|\n", clean):
        ids = CITE_RE.findall(sent)
        body = CITE_RE.sub("", sent)
        if ids:
            toks = set(textnorm.tokens(body))
            src = set()
            for i in ids:
                src |= set(textnorm.tokens(passages.get(i, "")))
            if toks and len(toks & src) / len(toks) < 0.35:
                return False, "a sentence isn't supported by its cited passage", clean, kept, []
        else:
            # an uncited sentence may only frame (an approved phrase, a short link) or restate the cited passages;
            # new advice with no source is the invention the style card forbids
            rest = body
            for f in framing:
                rest = re.sub(re.escape(f.strip(" .,:;!?")), " ", rest, flags=re.I)
            toks = set(textnorm.tokens(rest))
            if len(toks) > 3 and len(toks & all_src) / len(toks) < 0.5:
                return False, "added a sentence the passages don't support", clean, kept, []
        for num in NUM_RE.findall(body):
            num = num.rstrip(".,")
            if num and not any(num in passages.get(i, "") for i in kept):
                return False, "a number isn't in any cited passage", clean, kept, []
    words = re.findall(r"[a-z0-9']+", clean.lower())
    joined = " " + " ".join(words) + " "
    src_join = " " + " ".join(re.findall(r"[a-z0-9']+", " ".join(passages.values()).lower())) + " "
    for ex in excerpt_list:
        ew = re.findall(r"[a-z0-9']+", ex.lower())
        for i in range(0, max(0, len(ew) - 11)):
            span = " " + " ".join(ew[i:i + 12]) + " "
            if span in joined and span not in src_join:
                return False, "copied a style excerpt word for word", clean, kept, []
    used = [a["phrase"] for a in p.get("approved", []) if _contains(clean, a.get("display") or a["phrase"])]
    if len(used) > 1:
        return False, "more than one signature phrase", clean, kept, used
    if any(u in recent for u in used):
        return False, "repeated a phrase from a recent answer", clean, kept, used
    if any(_contains(clean, n) for n in p.get("never", [])):
        return False, "used a never-use phrase", clean, kept, used
    meta = {"persona_styled": True, "cited_subject_text": _subject_passages(tenant, res, passages)}
    flags = guardrails.check_for(tenant, clean, meta)
    if guardrails.blocked(flags):
        return False, "guardrails: " + ", ".join(sorted({f["code"] for f in flags if f["severity"] == "block"})), clean, kept, used
    return True, "ok", clean, kept, used


# --------------------------------------------------------------------------- styling modes
NOT_COVERED_TAIL = " (Not covered by the course material: "


def _partly_tail(res: dict) -> str | None:
    """The neutral answer's honest "(Not covered by the course material: ...)" notes for the parts it couldn't answer.
    "" when fully covered; None when the notes can't be found (then AI styling is skipped, so they are never lost)."""
    if res.get("covered") != "partly":
        return ""
    ans = res.get("answer") or ""
    i = ans.find(NOT_COVERED_TAIL)
    return ans[i:] if i >= 0 else None


def _styled(tenant, res: dict, p: dict, text: str, kept: list[str] | None, mode: str, label: str) -> dict:
    if mode in ("live", "replay"):
        text = text + (_partly_tail(res) or "")  # a rewritten answer keeps the honest not-covered notes
    cites = res.get("citations", []) if kept is None else [c for c in res.get("citations", []) if c.get("id") in set(kept)]
    return {**res, "answer": guardrails.scrub(text, guardrails.tenant_banned_names(tenant)), "citations": cites,
            "mode": mode if mode != "rules" else res.get("mode"), "label": label,
            "persona": {"styled": True, "name": p["name"], "version": p["version"], "consent_at": p["at"],
                        "mode": mode, "badge": badge(p), "disclosure": f"/t/{tenant.id}/persona"}}


def _rules(tenant, question: str, res: dict, p: dict, recent: set[str]):
    approved = p.get("approved", [])
    # framing lines need a phrase of two words or more (a lone word is not a line); a tag such as "Fair?" may be one word
    openers = [a for a in approved if a["cls"] == "opener" and a["n"] >= 2 and a["phrase"] not in recent]
    closers = [a for a in approved if (a["cls"] == "closer" and a["n"] >= 2 or a["cls"] == "tag")
               and a["phrase"] not in recent]
    h = int(hashlib.sha256(_normq(question).encode()).hexdigest()[:12], 16)
    op = openers[h % len(openers)] if openers else None
    cl = closers[(h // 7) % len(closers)] if closers and res.get("covered") == "yes" else None
    if op is None and cl is None:
        return None
    lines = (res.get("answer") or "").split("\n")
    has_prefix = bool(lines) and lines[0].strip() == PREFIX
    body = lines[1:] if has_prefix else lines
    out = [_render(op, "opener")] if op else ([PREFIX] if has_prefix else [])
    out += body
    if cl:
        out.append(_render(cl, "closer"))
    text = "\n".join(out)
    # cited sentences are never edited: every original body line must survive verbatim and in order
    if [ln for ln in out if ln in body] != body:
        return "a cited line changed"
    meta = {"persona_styled": True, "cited_subject_text": []}
    if _new_blocks(tenant, text, res.get("answer") or "", meta):
        return "guardrails blocked the framing"
    used = [x["phrase"] for x in (op, cl) if x]
    return _styled(tenant, res, p, text, None, "rules", badge(p)), used


def _ai(tenant, question: str, res: dict, p: dict, recent: set[str], run):
    if llm.unavailable_reason(tenant):
        return None
    passages = _passages(tenant, res)
    if not passages:
        return None
    from ..agents import coach

    ex = excerpts(tenant, p, question) if "external_ai_processing" in p["scopes"] else []
    system = coach.SYSTEM + "\n\n" + STYLE_RULES + "\n\n" + style_card(p)
    if ex:
        system += "\n\nSTYLE EXAMPLES (wording only, never facts):\n" + "\n".join(f"- {e}" for e in ex)
    prompt = "Question: " + question + "\n\nCourse passages:\n" + "\n\n".join(f"[{i}] {t}" for i, t in passages.items())

    def _drop():  # never keep an unverified styled output around for replay
        with tenant.db() as c:
            c.execute("DELETE FROM ai_replays WHERE key=?", (llm._key(f"coach-q:p{p['version']}", prompt),))

    try:
        out = llm.complete(tenant, system, prompt, purpose=f"coach-q:p{p['version']}", max_tokens=700,
                           run_id=run.id if run else None)
    except llm.AIUnavailable:
        return None
    try:
        data = llm.parse_json(out["text"])
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except ValueError:
        _drop()
        return "the AI reply wasn't readable"
    if str(data.get("covered", "")).lower() == "no":
        _drop()
        return "the AI said the passages don't cover it"
    ok, why, clean, kept, used = verify(tenant, p, str(data.get("answer", "")), list(data.get("citations", []) or []), res,
                                        passages, ex, recent)
    if not ok:
        _drop()
        return why
    llm.save_replay(tenant, f"coach-q:p{p['version']}", _normq(question), out["text"], run.id if run else None)
    label = f"AI · styled after {p['name']} (consent v{p['version']})"
    return _styled(tenant, res, p, clean, kept, "live", label), used


def _replayed(tenant, question: str, res: dict, p: dict, recent: set[str]):
    rp = llm.replay(tenant, f"coach-q:p{p['version']}", _normq(question))
    if not rp:
        return None
    passages = _passages(tenant, res)
    try:
        data = llm.parse_json(rp["text"])
    except ValueError:
        return None
    # a replay is the same verified answer to the same question, so the repeat rule doesn't apply to it
    ok, why, clean, kept, used = verify(tenant, p, str(data.get("answer", "")), list(data.get("citations", []) or []), res,
                                        passages, [], set())
    if not ok:
        return None
    label = f"Replayed AI · styled after {p['name']} (consent v{p['version']}, {rp['date']})"
    return _styled(tenant, res, p, clean, kept, "replay", label), used


# --------------------------------------------------------------------------- the hook
def frame(tenant, question: str, res: dict, user: dict | None = None, run=None) -> dict:
    """The coach hook. Returns `res` unchanged unless a persona applies; never raises."""
    try:
        return _frame(tenant, question, res, user, run)
    except Exception:  # noqa: BLE001 - styling must never break an answer
        log.warning("persona framing skipped", exc_info=True)
        return res


def _frame(tenant, question: str, res: dict, user: dict | None, run) -> dict:
    names = consent.names(tenant)
    if not names:
        return res
    name = consent.identity(tenant)["name"] or names[0]
    kind = honesty_kind(question, names)
    if kind:
        return _honest(tenant, res, name, kind, run)
    surf = current_surface(user)
    if surf is None:
        return res
    preview = _PREVIEW.get()
    p = preview or consent.active(tenant)
    if p is None or surf not in p["scopes"]:
        return res
    if user and not preview and consent.neutral_voice(tenant, user["id"]):
        return res
    if res.get("covered") == "no" or not (res.get("answer") or "").strip():
        return res
    recent = set() if preview else _recent(tenant, user)
    outcome = None
    if not preview and _partly_tail(res) is not None:
        outcome = _ai(tenant, question, res, p, recent, run)
        if isinstance(outcome, str):
            if run is not None:
                run.note("persona_style", {"consent": f"v{p['version']}", "mode": "ai"}, {"fallback": outcome},
                         status="skipped", label="Persona framing: fell back to the neutral answer")
            return {**res, "persona": {"styled": False, "fallback": outcome}}
        if outcome is None:
            outcome = _replayed(tenant, question, res, p, recent)
    if outcome is None:
        outcome = _rules(tenant, question, res, p, recent)
    if outcome is None or isinstance(outcome, str):
        return res
    styled, used = outcome
    if not preview:
        _record(tenant, user, p["version"], used)
    if run is not None and styled["persona"]["mode"] in ("live", "replay"):
        run.use_ai(styled["persona"]["mode"])
        styled["ai_mode"] = run.ai_mode
    if run is not None:
        run.note("persona_style", {"surface": surf, "consent": f"v{p['version']}"},
                 {"mode": styled["persona"]["mode"], "phrases_used": len(used)}, label=f"Persona framing (consent v{p['version']})")
    return styled


# --------------------------------------------------------------------------- framing for other surfaces
def framing(tenant, surface_name: str, key: str, user: dict | None = None) -> dict | None:
    """Approved opener/closer lines for a system-written intro (lesson_framing, roleplay_debriefs), or None when no
    valid consent covers that surface or the user chose a neutral voice. Callers put these around their own template
    text and show `badge`; they never edit quoted course text with them."""
    try:
        if _SURFACE.get() is None:  # inside persona.neutral() (channel replies, the phone line): never framed
            return None
        p = consent.active(tenant)
        if p is None or surface_name not in p["scopes"] or surface_name in ("voice", "likeness", "outreach"):
            return None
        if user and consent.neutral_voice(tenant, user["id"]):
            return None
        h = int(hashlib.sha256(f"{surface_name}:{key}".encode()).hexdigest()[:12], 16)
        ops = [a for a in p["approved"] if a["cls"] == "opener" and a["n"] >= 2]
        cls = [a for a in p["approved"] if a["cls"] in ("closer", "tag") and (a["n"] >= 2 or a["cls"] == "tag")]
        if not ops and not cls:
            return None
        return {"opener": _render(ops[h % len(ops)], "opener") if ops else None,
                "closer": _render(cls[(h // 7) % len(cls)], "closer") if cls else None,
                "badge": badge(p), "version": p["version"], "disclosure": f"/t/{tenant.id}/persona"}
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- template text (nudges, debriefs)
def style_template(tenant, text: str, surface_name: str, user: dict | None = None) -> str:
    """Word preferences for text the system writes from templates (never quoted course text). Unchanged on a neutral
    surface, without consent for that surface, or for a user who chose a neutral voice."""
    try:
        if _SURFACE.get() is None:
            return text
        p = consent.active(tenant)
        if p is None or surface_name not in p["scopes"] or surface_name in ("voice", "likeness", "outreach"):
            return text
        if user and consent.neutral_voice(tenant, user["id"]):
            return text
        out = text
        for lx in (p.get("profile") or {}).get("lexicon", []):
            out = re.sub(rf"\b{re.escape(lx['over'])}\b", lambda m: lx["prefer"] if m.group(0).islower() else
                         lx["prefer"].capitalize(), out)
        return out
    except Exception:  # noqa: BLE001
        return text


def styled_meta(tenant, neutral_text: str, styled_text: str) -> dict:
    """Queue meta for a draft whose template text was styled: the neutral wording and the consent it was styled under,
    so a withdrawal (or expiry) can put the neutral wording back on drafts nobody has approved yet. {} when unstyled."""
    if styled_text == neutral_text:
        return {}
    p = consent.active(tenant)
    return {"persona_neutral": neutral_text, "persona_consent_id": p["consent_id"] if p else None}


# --------------------------------------------------------------------------- owner/subject preview
def _offline(tenant):
    """The same workspace with the AI budget read as 0, so a preview never calls a provider, never spends budget and
    never saves an AI replay (a replay written from the preview's re-ranked retrieval would reach students without
    anyone's consent)."""
    import dataclasses

    from .. import tenancy

    class _OfflineTenant(tenancy.Tenant):
        def get(self, key: str, default=None):
            if key in ("ai_token_budget", "critique_token_budget"):
                return 0
            return super().get(key, default)

    return _OfflineTenant(**{f.name: getattr(tenant, f.name) for f in dataclasses.fields(tenant)})


def preview(tenant, questions: list[str] | None = None) -> dict:
    """Neutral vs styled on sample questions, rules mode only and offline (nothing is sent to an AI provider, no AI
    replay is saved and nothing is shown to students). Uses the latest profile's approved phrases, consent or not."""
    from ..agents import coach

    tenant = _offline(tenant)
    prof = consent.latest_profile(tenant)
    if prof is None:
        raise consent.PersonaError(409, "Build a style profile first.")
    ident = consent.identity(tenant)
    rows = consent.phrases(tenant, prof["id"], "approve")
    fake = {"version": prof["version"], "name": ident["name"] or "the subject", "scopes": {"coach_answers"},
            "at": time.time(), "expires": time.time() + 3600, "profile": prof["profile"] or {}, "preview": True,
            "approved": [{"id": r["id"], "phrase": r["phrase"], "display": r["display"], "cls": r["cls"], "n": r["n"]}
                         for r in rows], "never": consent.never_use(tenant)}
    qs = [q for q in (questions or []) if q and q.strip()][:5] or _sample_questions(tenant)
    out = []
    for q in qs:
        with neutral():
            n = coach.answer(tenant, q, record=False)
        tok = _PREVIEW.set(fake)
        try:
            with surface("coach_answers"):
                s = coach.answer(tenant, q, record=False)
        finally:
            _PREVIEW.reset(tok)
        n_ids = [(c["file"], c["lines"]) for c in n["citations"]]
        s_ids = [(c["file"], c["lines"]) for c in s["citations"]]
        body = [ln for ln in s["answer"].split("\n") if CITE_RE.search(ln)]
        flags = guardrails.check_for(tenant, s["answer"], {"persona_styled": True})
        out.append({"question": q, "neutral": n["answer"], "styled": s["answer"], "covered": n["covered"],
                    "styled_applied": bool((s.get("persona") or {}).get("styled")),
                    "checks": {"same_sources": n_ids == s_ids, "cited_lines_unchanged": all(ln in s["answer"] for ln in body),
                               "guardrails_clean": not guardrails.blocked(flags),
                               "flags": [f["code"] for f in flags]}})
    return {"version": prof["version"], "approved": len(rows), "items": out,
            "note": "Preview only: offline framing with the approved phrases. Nobody sees styled answers until the "
                    "subject signs consent."}


def _sample_questions(tenant) -> list[str]:
    try:
        with tenant.db() as c:
            rows = [r["question"] for r in c.execute(
                "SELECT question, COUNT(*) n FROM queries WHERE covered='yes' GROUP BY question ORDER BY n DESC, MAX(id) DESC "
                "LIMIT 5")]
    except Exception:  # noqa: BLE001
        rows = []
    return (rows + [q for q in SAMPLE_QUESTIONS if q not in rows])[:5]
