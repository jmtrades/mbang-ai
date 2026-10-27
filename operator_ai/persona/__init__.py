"""Persona: answers framed in the programme owner's own teaching phrasing, honestly and only with that person's
signed consent.

    persona.active(tenant)             -> None unless a valid, subject-signed, unexpired consent matches the profile
    persona.frame(tenant, q, res, ...) -> the coach hook (apply.py); neutral unless a persona applies
    persona.surface("public_minis")    -> context manager: run a block on a consented surface (None = neutral)
    persona.guard_rules(...)           -> impersonation, persona in outreach, invented autobiography, attribution
    persona.seed_demo(tenant)          -> fictional demo persona (demo database only)

What it never does: speak as the person, tell their stories, clone a voice, use a likeness, or appear in outreach,
client assets, calendar, ads, CRM actions or the phone line. Modules: consent.py (who may sign, scopes, expiry,
withdrawal), attribution.py (who said which line), mine.py (offline style profile), apply.py (framing and checks).
"""
from __future__ import annotations

from .apply import (badge, frame, framing, honesty_kind, neutral, preview, retrieval_boost, style_template,  # noqa: F401
                    surface)
from .attribution import DRAFT_WORDS, STABLE_WORDS, coverage, set_doc_speaker  # noqa: F401
from .consent import (NOT_OFFERED, SCOPES, PersonaError, active, cache_clear, consents, decide_phrase,  # noqa: F401
                      designate_subject, identity, latest_profile, legacy_consent, names, neutral_voice, phrases,
                      set_identity, set_neutral_voice, sign_consent, status, withdraw)
from .guards import guard_rules  # noqa: F401

# Text-to-speech never takes a persona field: the phone line uses channels.TTS_VOICE from a constant allowlist of
# neutral system voices, and the browser read-aloud uses the device's default voice.


def build_profile(tenant, actor: str = "system") -> dict:
    from .mine import mine as _run

    return _run(tenant, actor)


def seed_demo(tenant) -> None:
    """Fictional demo persona: subject = the demo owner, a profile mined from DEMO transcripts, a few phrases approved
    and a demo consent (coach answers, lesson framing, debriefs, nudges; no external AI). Demo database only."""
    import time

    from .. import tenancy
    from . import demo_material

    if not tenancy.is_demo_db(tenant):
        return
    cache_clear(tenant)
    with tenant.db() as c:
        u = c.execute("SELECT id, username, role FROM users WHERE username='demo-owner'").fetchone()
    if u is None:
        return
    user = {"id": u["id"], "username": u["username"], "role": u["role"]}
    designate_subject(tenant, u["id"], "system (DEMO)", name=demo_material.DEMO_NAME, aliases=demo_material.DEMO_ALIASES)
    try:
        res = build_profile(tenant, "system (DEMO)")
    except PersonaError:
        return
    prof = latest_profile(tenant)
    rows = phrases(tenant, prof["id"])
    picked = {"opener": 0, "closer": 0, "tag": 0, "signature": 0, "transition": 0}
    # framing lines only (what rules mode uses); the demo subject leaves signature phrases and transitions pending
    limits = {"opener": 2, "closer": 2, "tag": 1, "signature": 0, "transition": 0}
    for r in rows:
        if picked.get(r["cls"], 9) < limits.get(r["cls"], 0) and (r["n"] >= 3 or r["cls"] == "tag"):
            decide_phrase(tenant, r["id"], "approve", user, "demo-owner (DEMO)")
            picked[r["cls"]] += 1
    for r in rows:  # show the never-use decision on one signature phrase
        if r["decision"] == "pending" and r["cls"] == "signature":
            decide_phrase(tenant, r["id"], "never_use", user, "demo-owner (DEMO)")
            break
    sign_consent(tenant, user, "(DEMO) I agree that the coach may open and close its answers with the phrases I approved "
                               "above. It must always say it is an AI assistant, never speak as me, and never use my "
                               "words in anyone's outreach.",
                 ["coach_answers", "lesson_framing", "roleplay_debriefs", "student_nudges"], res["version"], "",
                 time.time() + 365 * 86400, "demo-owner (DEMO)", _verified=True, demo=True)
