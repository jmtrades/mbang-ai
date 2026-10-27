"""Persona API: owner set-up, the subject's own consent, withdrawal, the per-user neutral-voice switch and a public
disclosure page. Every write is audited (persona.*). Voice, likeness and outreach are never accepted as inputs."""
from __future__ import annotations

import html
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from .. import audit, guardrails, persona, tenancy
from ..deps import Ctx, get_ctx, get_tenant, require_owner

router = APIRouter()
public_router = APIRouter()


class _Strict(BaseModel):
    # unknown fields (a "voice_sample", "avatar" or "photo" included) are refused with 422
    model_config = ConfigDict(extra="forbid")


class SubjectBody(_Strict):
    user_id: int = Field(ge=1)
    name: str | None = Field(None, max_length=80)
    aliases: list[str] | None = Field(None, max_length=12)


class IdentityBody(_Strict):
    name: str | None = Field(None, max_length=80)
    aliases: list[str] | None = Field(None, max_length=12)


class MineBody(_Strict):
    note: str | None = Field(None, max_length=200)


class PhraseBody(_Strict):
    decision: str = Field(pattern="^(approve|reject|never_use|pending)$")


class ConsentBody(_Strict):
    statement: str = Field(min_length=1, max_length=2000)
    scopes: list[str] = Field(min_length=1, max_length=10)
    profile_version: int = Field(ge=1)
    expires: float | None = Field(None, gt=0)
    password: str = Field(min_length=1, max_length=200)


class WithdrawBody(_Strict):
    delete_profile: bool = False
    reason: str | None = Field(None, max_length=200)


class AttributionBody(_Strict):
    file: str = Field(min_length=1, max_length=300)
    speaker: str | None = Field(None, max_length=80)


class MeBody(_Strict):
    neutral_voice: bool


class PreviewBody(_Strict):
    questions: list[str] = Field(default_factory=list, max_length=5)


def _err(e: persona.PersonaError):
    raise HTTPException(e.status, str(e))


def _subject_or_owner(ctx: Ctx) -> bool:
    return persona.identity(ctx.tenant)["subject_user_id"] == ctx.user["id"]


def _require_owner_or_subject(ctx: Ctx = Depends(get_ctx)) -> Ctx:
    if ctx.role != "owner" and not _subject_or_owner(ctx):
        raise HTTPException(403, "Owner or the persona subject only")
    return ctx


def _profile_public(p: dict | None) -> dict | None:
    if not p:
        return None
    prof = p.get("profile") or {}
    return {"version": p["version"], "maturity": p["status"], "words": p.get("words") or prof.get("words", 0),
            "created": p["created"], "hash": (p.get("profile_hash") or "")[:12], "stylometrics": prof.get("stylometrics", {}),
            "lexicon": prof.get("lexicon", []), "report": prof.get("report", {}), "dropped_lines": prof.get("dropped_lines", {}),
            "background_words": prof.get("background_words", 0), "demo": bool(p.get("demo"))}


def _active_public(a: dict | None) -> dict | None:
    if not a:
        return None
    return {"name": a["name"], "version": a["version"], "at": a["at"], "expires": a["expires"],
            "scopes": sorted(a["scopes"]), "consent_id": a["consent_id"], "badge": persona.badge(a)}


@router.get("/persona")
def overview(ctx: Ctx = Depends(require_owner)):
    t = ctx.tenant
    ident = persona.identity(t)
    users = {u["id"]: u for u in persona.consent.owner_users(t)}
    prof = persona.latest_profile(t)
    legacy = persona.legacy_consent(t)
    with t.db() as c:
        subj = c.execute("SELECT id, username, display FROM users WHERE id=?", (ident["subject_user_id"] or 0,)).fetchone()
    return {
        "status": persona.status(t),
        "identity": {**ident, "subject": dict(subj) if subj else None},
        "candidates": list(users.values()),
        "is_subject": ident["subject_user_id"] == ctx.user["id"],
        "active": _active_public(persona.active(t)),
        "legacy": {"present": bool(legacy), "valid": False,
                   "banner": "An older free-text persona consent typed in the owner console is on file. It is not valid: "
                             "consent now has to be signed by the person themselves." if legacy else None},
        "coverage": persona.coverage(t),
        "profile": _profile_public(prof),
        "phrases": persona.phrases(t, prof["id"]) if prof else [],
        "consents": persona.consents(t, None, 20),
        "timeline": audit.recent(t, 60, "persona."),
        "scopes": [{"key": k, "label": v} for k, v in persona.SCOPES.items()],
        "not_offered": list(persona.NOT_OFFERED),
        "disclosure_url": f"/t/{t.id}/persona",
        "is_demo": t.is_demo,
    }


@router.get("/persona/me")
def me(ctx: Ctx = Depends(get_ctx)):
    """For every signed-in user: is a persona styling my answers, under what consent, and my neutral-voice choice.
    The subject also gets what they need to review and sign."""
    t = ctx.tenant
    a = persona.active(t)
    ident = persona.identity(t)
    is_subject = bool(ident["subject_user_id"]) and ident["subject_user_id"] == ctx.user["id"]
    out = {"is_subject": is_subject, "active": _active_public(a), "status": persona.status(t)["label"],
           "neutral_voice": persona.neutral_voice(t, ctx.user["id"]), "disclosure_url": f"/t/{t.id}/persona",
           "is_demo": t.is_demo}
    if is_subject:
        prof = persona.latest_profile(t)
        rows = persona.phrases(t, prof["id"]) if prof else []
        last = persona.consents(t, ctx.user["id"], 1)
        last = last[0] if last else None
        days_left = int((a["expires"] - time.time()) // 86400) if a else None
        out["subject"] = {
            "name": ident["name"], "aliases": ident["aliases"], "profile": _profile_public(prof), "phrases": rows,
            "pending": sum(1 for r in rows if r["decision"] == "pending"),
            "approved": sum(1 for r in rows if r["decision"] == "approve"),
            "last_consent": last, "days_left": days_left,
            "reminder": (f"Your consent expires in {days_left} days. Renew it or let it lapse; answers go neutral either way."
                         if days_left is not None and days_left <= persona.consent.REMIND_DAYS else None),
            "scopes": [{"key": k, "label": v} for k, v in persona.SCOPES.items()],
            "not_offered": list(persona.NOT_OFFERED), "placeholder": persona.consent.STATEMENT_PLACEHOLDER,
            "min_statement": persona.consent.MIN_STATEMENT, "default_days": persona.consent.DEFAULT_DAYS,
        }
    return out


@router.put("/persona/me")
def set_me(b: MeBody, ctx: Ctx = Depends(get_ctx)):
    return {"neutral_voice": persona.set_neutral_voice(ctx.tenant, ctx.user["id"], b.neutral_voice)}


@router.post("/persona/subject")
def designate(b: SubjectBody, ctx: Ctx = Depends(require_owner)):
    try:
        return {"identity": persona.designate_subject(ctx.tenant, b.user_id, ctx.actor, b.name, b.aliases),
                "status": persona.status(ctx.tenant)}
    except persona.PersonaError as e:
        _err(e)


@router.put("/persona/identity")
def identity(b: IdentityBody, ctx: Ctx = Depends(require_owner)):
    try:
        return {"identity": persona.set_identity(ctx.tenant, ctx.actor, b.name, b.aliases)}
    except persona.PersonaError as e:
        _err(e)


@router.post("/persona/mine")
def mine(b: MineBody, ctx: Ctx = Depends(_require_owner_or_subject)):
    try:
        return persona.build_profile(ctx.tenant, ctx.actor)
    except persona.PersonaError as e:
        _err(e)


@router.get("/persona/profiles/{v}")
def profile(v: int, ctx: Ctx = Depends(_require_owner_or_subject)):
    p = persona.consent.profile_row(ctx.tenant, version=v)
    if p is None:
        raise HTTPException(404, "No profile with that version")
    return {"profile": _profile_public(p), "phrases": persona.phrases(ctx.tenant, p["id"]), "status": p["status"]}


@router.put("/persona/phrases/{pid}")
def phrase(pid: int, b: PhraseBody, ctx: Ctx = Depends(_require_owner_or_subject)):
    try:
        return persona.decide_phrase(ctx.tenant, pid, b.decision, ctx.user, ctx.actor)
    except persona.PersonaError as e:
        _err(e)


@router.get("/persona/preview")
def preview_get(q: str | None = Query(None, max_length=300), ctx: Ctx = Depends(_require_owner_or_subject)):
    try:
        return persona.preview(ctx.tenant, [q] if q else None)
    except persona.PersonaError as e:
        _err(e)


@router.post("/persona/preview")
def preview_post(b: PreviewBody, ctx: Ctx = Depends(_require_owner_or_subject)):
    try:
        return persona.preview(ctx.tenant, [q[:300] for q in b.questions])
    except persona.PersonaError as e:
        _err(e)


@router.post("/persona/consent")
def sign(b: ConsentBody, ctx: Ctx = Depends(get_ctx)):
    try:
        row = persona.sign_consent(ctx.tenant, ctx.user, b.statement, b.scopes, b.profile_version, b.password, b.expires,
                                   ctx.actor)
    except persona.PersonaError as e:
        _err(e)
    return {"consent": row, "status": persona.status(ctx.tenant)}


@router.post("/persona/withdraw")
def withdraw(b: WithdrawBody, ctx: Ctx = Depends(_require_owner_or_subject)):
    res = persona.withdraw(ctx.tenant, ctx.actor, b.delete_profile, b.reason or "")
    return {**res, "status": persona.status(ctx.tenant)}


@router.get("/persona/attribution")
def attribution(ctx: Ctx = Depends(_require_owner_or_subject)):
    return persona.coverage(ctx.tenant)


@router.put("/persona/attribution")
def set_attribution(b: AttributionBody, ctx: Ctx = Depends(require_owner)):
    try:
        return persona.set_doc_speaker(ctx.tenant, b.file, b.speaker, ctx.actor)
    except persona.PersonaError as e:
        _err(e)


@router.get("/public/persona")
def public_json(tenant: tenancy.Tenant = Depends(get_tenant)):
    return _disclosure(tenant)


PUBLIC_SCOPES = {
    "coach_answers": "Opening and closing lines on Student Coach answers",
    "lesson_framing": "Introductions to Academy lessons",
    "roleplay_debriefs": "Introductions to role-play debriefs",
    "student_nudges": "Word choices in student nudges written from templates",
    "public_minis": "The same framing in public lead-magnet assistants",
    "external_ai_processing": "Short excerpts of their own words may be sent to the AI provider to guide wording",
}


def _disclosure(tenant) -> dict:
    a = persona.active(tenant)
    ident = persona.identity(tenant)
    brand = (tenant.get("brand", {}) or {}).get("name") or "this programme"
    last = persona.consents(tenant, ident["subject_user_id"], 1) if ident["subject_user_id"] else []
    last = last[0] if last else None
    out = {"brand": brand, "active": bool(a), "is_demo": tenant.is_demo,
           "never": ["Speak as the person or claim to be them", "Tell personal stories, clients or results as theirs",
                     "Use a cloned voice or the person's likeness", "Write outreach, client material or anything sent "
                                                                    "in someone's name"],
           "report": "Tell the programme team in the app, or reply to any message from the programme, and say which "
                     "answer concerned you. Styling can be switched off for everyone at once."}
    if a:
        out.update({"name": a["name"], "version": a["version"], "signed": a["at"], "expires": a["expires"],
                    "scopes": [PUBLIC_SCOPES[s] for s in sorted(a["scopes"]) if s in PUBLIC_SCOPES],
                    "summary": f"Some answers are framed with {a['name']}'s own approved teaching phrases. It is an AI "
                               f"assistant, not {a['name']}. {a['name']} signed this consent in the app themselves."})
    elif last and last.get("withdrawn_at"):
        out["summary"] = ("Consent was withdrawn or has expired, so every answer is in a neutral voice. Earlier styled "
                          "messages stay as they were, marked as styled under a consent that has ended.")
    else:
        out["summary"] = "Every answer is in a neutral voice. No one's persona is in use."
    return out


@public_router.get("/t/{tid}/persona", response_class=HTMLResponse)
def disclosure_page(tid: str):
    tenant = get_tenant(tid)
    d = _disclosure(tenant)
    names = guardrails.tenant_banned_names(tenant)

    def e(s) -> str:
        return html.escape(guardrails.scrub(str(s), names))

    def day(ts) -> str:
        return time.strftime("%d %B %Y", time.localtime(ts))

    rows = ""
    if d["active"]:
        rows = (f"<dt>Whose phrasing</dt><dd>{e(d['name'])}</dd><dt>Consent</dt><dd>Version {d['version']}, signed "
                f"{e(day(d['signed']))} by {e(d['name'])} in the app; expires {e(day(d['expires']))}</dd>"
                f"<dt>Used for</dt><dd><ul>{''.join(f'<li>{e(s)}</li>' for s in d['scopes'])}</ul></dd>")
    demo = "<p class='demo'>Pitch demo: the person and consent below are fictional (DEMO).</p>" if d["is_demo"] else ""
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>About this assistant's voice</title>
<style>body{{font:16px/1.55 system-ui,-apple-system,Segoe UI,sans-serif;margin:0;background:#f7f7f8;color:#16181d}}
main{{max-width:640px;margin:0 auto;padding:32px 16px}}h1{{font-size:24px;margin:0 0 8px}}
.card{{background:#fff;border:1px solid #e3e4e8;border-radius:12px;padding:16px 18px;margin:16px 0}}
dt{{font-weight:600;margin-top:10px}}dd{{margin:2px 0 0}}ul{{margin:4px 0;padding-left:20px}}
.demo{{background:#ede9fe;color:#4c1d95;border-radius:8px;padding:6px 10px;font-size:14px}}
.muted{{color:#5d6270;font-size:14px}}</style></head><body><main>
<h1>About this assistant's voice</h1><p class="muted">{e(d['brand'])}</p>{demo}
<div class="card"><p>{e(d['summary'])}</p>{f'<dl>{rows}</dl>' if rows else ''}</div>
<div class="card"><h2 style="font-size:17px;margin:0 0 6px">What it never does</h2><ul>{''.join(f'<li>{e(x)}</li>' for x in d['never'])}</ul></div>
<div class="card"><h2 style="font-size:17px;margin:0 0 6px">Report a concern</h2><p>{e(d['report'])}</p></div>
</main></body></html>"""
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})
