"""Persona guardrails, plugged into guardrails.check() through guardrails.RULE_PLUGINS.

Active whenever a persona name or aliases are configured for the tenant (consent or not):
  impersonation           block, all modes: "I'm X", "this is X", "X here", "X speaking", "As X, I ...", "it's X",
                          sign-offs "- X" / "from X" / "Signed: X". Invisible characters inside the name don't hide it.
  persona_in_outreach     block: the name or an approved signature phrase in outreach or client assets, or in any item
                          someone drafted for their own prospects or clients, so it sounds like the sender.
  persona_autobio         block in styled output: first-person experience or achievement claims ("when I started",
                          "I scaled", "my clients") unless a cited passage the subject said makes the same claim.
  speculative_attribution block: "X would say / thinks / recommends" without an [S#] citation on that sentence.
Income claims stay blocked by the core rules regardless.
"""
from __future__ import annotations

import re

from . import consent

AUTOBIO = re.compile(
    r"\b(when i (started|began|was starting|first|launched|built|scaled|quit|left|signed|landed|got my|had my)|"
    r"i (built|scaled|made|grew|started|launched|founded|ran|sold|signed|landed|closed|earned|quit|hired|bought|"
    r"doubled|tripled|went from|helped|coached|worked with|once)\b|"
    r"i(?:'ve| have| had)( (already|personally|actually|just))? (built|scaled|made|grown|helped|coached|trained|signed|"
    r"landed|closed|earned|launched|started|run|sold|worked with|hired|doubled|tripled|founded|gone from)\b|"
    r"my (first |old |own )?(clients?|agency|business|company|students?|story|journey|results?|income|revenue|team)\b|"
    r"back when i\b|years ago,? i\b|i used to\b|i remember when\b|in my (own )?(business|agency|experience)\b)", re.I)
_CITE = re.compile(r"\[S\d+\]")
_SENT = re.compile(r"[^.!?\n]+[.!?]*")
_PRE = re.compile(r"\b(i'?m|i am|this is|it'?s|its|here'?s|my name is|my name'?s|call me|signed:?|"
                  r"you'?re (talking|speaking|chatting) (to|with))\s+((really|actually|just|truly|definitely|in fact)\s+)?$",
                  re.I)
_AS = re.compile(r"(?:^|[\s(\"'])(as|speaking as)\s+$", re.I)
_POST = re.compile(r"\s+(here|speaking|writing|signing off)\b|\s*[,:]?\s+(i|i'm|i've|i'll|i'd|me|my)\b", re.I)
_SIGNOFF = re.compile(r"(?:^|\n)\s*(?:[-—–~]+|cheers,?|thanks,?|best,?|regards,?|love,?|from|signed[,:]?|xo,?)\s*$", re.I)
_TAIL = re.compile(r"\s*(\([^)\n]{0,60}\))?[\s.!]*$")
# invisible characters (zero-width spaces, joiners, soft hyphens) that would split a name and hide it from the rules
_INVISIBLE = re.compile(r"[\u00ad\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")
OFF_KINDS = {"dm", "dm_opener", "followup", "prospect_dm", "prospect_email", "booking_dm", "proposal", "reminder",
             "client_report", "channel_reply", "mission_draft", "setter_reply", "precall_brief", "calendar_invite",
             "ad_change", "crm_move", "email", "welcome", "kickoff"}


def _flag(code: str, message: str, match: str) -> dict:
    return {"code": code, "severity": "block", "message": message, "match": (match or "").strip()[:120]}


def _alt(names: list[str]) -> str:
    return "|".join(re.escape(n).replace(r"\ ", r"\s+") for n in names)


def approved_phrases(tenant) -> list[str]:
    """Approved phrases of the latest profile (three words or more, so common short phrases never block outreach)."""
    def _f():
        prof = consent.latest_profile(tenant)
        if prof is None:
            return []
        return sorted({(r["display"] or r["phrase"]).strip(" .,:;!?").lower() for r in consent.phrases(tenant, prof["id"], "approve")
                       if r["n"] >= 3}, key=len, reverse=True)

    return consent.cached(tenant, "approved_phrases", _f)


def _sentence(text: str, start: int) -> str:
    for m in _SENT.finditer(text):
        if m.start() <= start < m.end():
            return m.group(0)
    return text


def _supported(sentence: str, cited: list[str]) -> bool:
    from .. import textnorm

    toks = set(textnorm.tokens(_CITE.sub("", sentence)))
    if not toks:
        return True
    for c in cited or []:
        src = set(textnorm.tokens(c))
        if len(toks & src) / len(toks) >= 0.6:
            return True
    return False


def guard_rules(text: str, mode: str, meta: dict, tenant) -> list[dict]:
    if tenant is None:
        return []
    try:
        names = consent.names(tenant)
    except Exception:  # noqa: BLE001 - a tenant without the persona tables yet: nothing configured
        return []
    if not names:
        return []
    meta = meta or {}
    alt = _alt(names)
    flags: list[dict] = []
    t = _INVISIBLE.sub("", text or "")

    # (a) impersonation
    for m in re.finditer(rf"(?<![\w'])({alt})(?![\w])", t, re.I):
        after = t[m.end():m.end() + 16]
        if re.match(r"\s*'s\b", after):
            continue  # possessive: "the persona's framework" is not a claim to be them
        before = t[max(0, m.start() - 48):m.start()]
        # "I'm X", "this is X", "X here", "X speaking", "As X, I recommend..."
        claims = (bool(_PRE.search(before)) or bool(re.match(r"\s+(here|speaking|writing|signing off)\b", after, re.I))
                  or (bool(_AS.search(before)) and bool(_POST.match(after))))
        signoff = bool(_SIGNOFF.search(before)) and bool(_TAIL.fullmatch(t[m.end():]))
        if claims or signoff:
            flags.append(_flag("impersonation", "Never speak as the persona: this assistant is AI, not the person.",
                               "[persona name]"))
            break

    # (b) persona in outreach / client assets / own-prospect drafts
    outreachy = (mode in ("outreach", "client_asset") or meta.get("prospect_facing") or meta.get("client_asset")
                 or meta.get("owner_user_id") is not None or meta.get("kind") in OFF_KINDS)
    if outreachy:
        m = re.search(rf"\b({alt})\b", t, re.I)
        if m:
            flags.append(_flag("persona_in_outreach", "Outreach and client assets must sound like the sender, never use the "
                                                      "persona's name.", "[persona name]"))
        else:
            low = " ".join(re.sub(r"[^\w'\s]", " ", t.lower()).split())
            for ph in approved_phrases(tenant):
                norm = " ".join(re.sub(r"[^\w'\s]", " ", ph).split())
                if norm and re.search(rf"(?<![\w']){re.escape(norm)}(?![\w'])", low):
                    flags.append(_flag("persona_in_outreach", "Outreach and client assets must sound like the sender, "
                                                              "not use the persona's signature phrases.", ph))
                    break

    # (c) invented autobiography in styled output
    if meta.get("persona_styled"):
        for m in AUTOBIO.finditer(t):
            sent = _sentence(t, m.start())
            if _supported(sent, meta.get("cited_subject_text") or []):
                continue
            flags.append(_flag("persona_autobio", "Styled answers never tell first-person stories the material doesn't contain.",
                               m.group(0)))
            break

    # (d) speculative attribution
    spec = re.compile(rf"\b({alt})(?:'s)?\s+(?:would|might|will|could|probably|always|usually|definitely)?\s*"
                      rf"(say|says|said|think|thinks|believe|believes|recommend|recommends|suggest|suggests|want|wants you|"
                      rf"tell you|tells you|teach|teaches|advise|advises)\b", re.I)
    spec2 = re.compile(rf"\b(according\s+to|in\s+the\s+words\s+of|if\s+you\s+asked|if)\s+({alt})\b(?!'s)"
                       rf"|\b({alt})'s\s+(advice|view|take|opinion|rule|philosophy|approach|belief|recommendation|answer|"
                       rf"position|stance)\s+(is|would\s+be|was|here)\b", re.I)
    for m in [*spec.finditer(t), *spec2.finditer(t)]:
        if m.re is spec2 and m.group(1) and m.group(1).lower() == "if" and not re.match(
                r"\s+(were|was|is|could|would)\b", t[m.end():m.end() + 10], re.I):
            continue  # "if Kofi" only counts as "if Kofi were here / would ..."
        if not _CITE.search(_sentence(t, m.start())):
            word = m.group(2) if m.re is spec else "attribution"
            flags.append(_flag("speculative_attribution", "Don't say what the persona would say or think without citing "
                                                          "where they said it.", "[persona name] " + word))
            break
    return flags
