"""Owner console: KB admin, provenance, personality, members, minis, Drive sync, integrations,
vault, MCP tokens, engagement alerts."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import claims, console, index, mcp_server, tenancy, vault
from ..deps import Ctx, require, require_owner

router = APIRouter()

DEMO_READ_ONLY = "Read-only in the pitch demo: the knowledge base and proof records can only be changed in a real workspace."


def kb_writer(ctx: Ctx = Depends(require_owner)) -> Ctx:
    """KB admin and proof records change what every coach answer and prospect message may say: owner only,
    and never in the public demo (anyone can sign in there as owner with one click)."""
    if ctx.tenant.is_demo:
        raise HTTPException(403, DEMO_READ_ONLY)
    return ctx


@router.get("/kb")
def kb(ctx: Ctx = Depends(require("kb"))):
    idx = index.for_tenant(ctx.tenant)
    return {"files": console.kb_files(ctx.tenant), "scope": ctx.tenant.kb_scope, "status": idx.status(),
            "third_party_switch": False, "exclusions": index.load_exclusions(ctx.tenant),
            "read_only": ctx.tenant.is_demo or ctx.role != "owner",
            "read_only_reason": DEMO_READ_ONLY if ctx.tenant.is_demo else None}


class KbPatch(BaseModel):
    file: str = Field(min_length=1, max_length=300)
    licence: str | None = Field(None, max_length=40)
    provenance: str | None = Field(None, max_length=40)
    priority: float | None = None
    note: str | None = Field(None, max_length=2000)


@router.put("/kb")
def kb_patch(b: KbPatch, ctx: Ctx = Depends(kb_writer)):
    try:
        return console.kb_update(ctx.tenant, b.file, ctx.actor, b.licence, b.provenance, b.priority, b.note)
    except KeyError:
        raise HTTPException(404, "Not an indexed file")
    except ValueError as e:
        raise HTTPException(422, f"Bad {e}")


class Exclusion(BaseModel):
    file: str = Field(min_length=1, max_length=300)
    lines: str | None = Field(None, pattern=r"^\d+(-\d+)?$")
    ts: str | None = Field(None, pattern=r"^\d{1,2}:\d{2}(:\d{2})?(-\d{1,2}:\d{2}(:\d{2})?)?$")


@router.post("/kb/exclusions")
def add_exclusion(b: Exclusion, ctx: Ctx = Depends(kb_writer)):
    if not b.lines and not b.ts:
        raise HTTPException(422, "Give a line range (10-12) or a time range (0:01:00-0:02:00)")
    if b.file not in index.for_tenant(ctx.tenant).docs:
        raise HTTPException(404, "Not an indexed file")
    items = index.load_exclusions(ctx.tenant)
    if len(items) >= 500:
        raise HTTPException(422, "Too many exclusions (500 max); remove some first")
    items.append({k: v for k, v in b.model_dump().items() if v})
    index.save_exclusions(ctx.tenant, items)
    from .. import audit

    audit.log(ctx.tenant, ctx.actor, "kb.admin", {"exclusion": b.file})
    return {"exclusions": index.load_exclusions(ctx.tenant)}


@router.get("/provenance")
def provenance(ctx: Ctx = Depends(require("kb"))):
    return console.provenance_panel(ctx.tenant)


@router.get("/personality")
def get_personality(ctx: Ctx = Depends(require("console"))):
    return console.personality(ctx.tenant)


class PersonalityBody(BaseModel):
    display_name: str | None = Field(None, max_length=60)
    daily_question_limit: int | None = Field(None, ge=1, le=1000)
    banned_words: list[str] | None = Field(None, max_length=100)
    style: str | None = Field(None, max_length=500)
    persona_consent_text: str | None = Field(None, max_length=500)


@router.put("/personality")
def put_personality(b: PersonalityBody, ctx: Ctx = Depends(require_owner)):
    try:
        return console.set_personality(ctx.tenant, b.model_dump(exclude_none=True), ctx.actor)
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.get("/members")
def members(ctx: Ctx = Depends(require("console"))):
    return {"items": console.members(ctx.tenant), "webhook": f"/t/{ctx.tenant.id}/hooks/payments",
            "signature": "X-Signature: hex HMAC-SHA256 of the raw body with the tenant's payments secret"}


@router.get("/alerts")
def alerts(ctx: Ctx = Depends(require("console"))):
    return {"items": console.engagement_alerts(ctx.tenant)}


# minis
@router.get("/minis")
def minis(ctx: Ctx = Depends(require("minis"))):
    return {"items": [{**m, "embed": console.embed_snippet(ctx.tenant.id, m["slug"]), "preview": f"/m/{ctx.tenant.id}/{m['slug']}"}
                      for m in console.list_minis(ctx.tenant)]}


class MiniBody(BaseModel):
    slug: str = Field(pattern="^[a-z0-9-]{2,40}$")
    title: str = Field(min_length=1, max_length=120)
    scope: list[str] = Field(default_factory=list, max_length=50)
    question_limit: int = Field(5, ge=1, le=50)
    consent_text: str = Field("I agree to share my email and receive follow-up.", max_length=500)


@router.post("/minis")
def create_mini(b: MiniBody, ctx: Ctx = Depends(require_owner)):
    try:
        return {"id": console.create_mini(ctx.tenant, b.slug, b.title, b.scope, b.question_limit, b.consent_text)}
    except Exception:  # noqa: BLE001 - UNIQUE slug
        raise HTTPException(409, "Slug already used")


# Drive inbox
@router.get("/drive")
def drive(ctx: Ctx = Depends(require("drive"))):
    return {**console.drive_list(ctx.tenant), "staged": console.drive_staged(ctx.tenant)}


@router.post("/drive/sync")
def drive_sync(ctx: Ctx = Depends(kb_writer)):
    return {"staged": console.drive_fetch_and_stage(ctx.tenant, ctx.actor)}


class DriveDecision(BaseModel):
    approve: bool


@router.post("/drive/staged/{sid}")
def drive_decide(sid: int, b: DriveDecision, ctx: Ctx = Depends(kb_writer)):
    try:
        return console.drive_decide(ctx.tenant, sid, b.approve, ctx.actor)
    except KeyError:
        raise HTTPException(404, "Not found")


# integrations + vault
@router.get("/integrations")
def integrations(ctx: Ctx = Depends(require("integrations"))):
    return {"items": console.integrations(ctx.tenant)}


class VaultBody(BaseModel):
    fields: dict[str, str] = Field(min_length=1, max_length=20)


@router.put("/vault")
def vault_put(b: VaultBody, ctx: Ctx = Depends(require_owner)):
    allowed = {f for it in console.INTEGRATIONS for f in it["fields"]}
    bad = [k for k in b.fields if k not in allowed]
    if bad:
        raise HTTPException(422, f"Unknown field(s): {', '.join(bad)}")
    saved = vault.put(ctx.tenant, b.fields, ctx.actor)
    return {"saved": saved, "status": vault.status(ctx.tenant, saved)}  # set / not set only


@router.get("/vault")
def vault_status(ctx: Ctx = Depends(require_owner)):
    fields = sorted({f for it in console.INTEGRATIONS for f in it["fields"]})
    return {"status": vault.status(ctx.tenant, fields)}


# MCP tokens
class TokenBody(BaseModel):
    label: str = Field(min_length=1, max_length=80)


@router.get("/mcp/tokens")
def tokens(ctx: Ctx = Depends(require("mcp"))):
    return {"items": mcp_server.list_tokens(ctx.tenant), "endpoint": "/mcp", "tools": [t["name"] for t in mcp_server.TOOLS]}


@router.post("/mcp/tokens")
def mint(b: TokenBody, ctx: Ctx = Depends(require_owner)):
    return {"token": mcp_server.mint(ctx.tenant, b.label, ctx.actor), "shown_once": True}


@router.delete("/mcp/tokens/{token_id}")
def revoke(token_id: int, ctx: Ctx = Depends(require_owner)):
    mcp_server.revoke(ctx.tenant, token_id, ctx.actor)
    return {"ok": True}


# tenants (owner of the owner workspace only)
@router.get("/tenants")
def tenants(ctx: Ctx = Depends(require_owner)):
    if ctx.tenant.kind != "owner":
        raise HTTPException(403, "Only the programme owner workspace lists tenants")
    return {"items": [t.to_dict() for t in tenancy.list_all()]}


# proof records (claims substantiation): a figure reaches prospects only while a current record exists
@router.get("/claims")
def claims_list(ctx: Ctx = Depends(require("console"))):
    return {"items": claims.list_records(ctx.tenant), "claim_keys": claims.CLAIM_KEYS,
            "current": claims.has_valid(ctx.tenant), "evidence_types": sorted(claims.EVIDENCE_TYPES),
            "max_file_mb": claims.MAX_FILE_BYTES // (1024 * 1024), "max_days": claims.MAX_VALID_DAYS,
            "read_only": ctx.tenant.is_demo or ctx.role != "owner",
            "read_only_reason": DEMO_READ_ONLY if ctx.tenant.is_demo else None}


class ClaimBody(BaseModel):
    claim_key: str = Field("outreach_range", pattern="^[a-z_]{2,40}$")
    typicality_note: str = Field(min_length=1, max_length=1000)
    valid_days: int = Field(180, ge=1, le=claims.MAX_VALID_DAYS)
    statement: str | None = Field(None, max_length=300)


@router.post("/claims")
def claims_add(b: ClaimBody, ctx: Ctx = Depends(kb_writer)):
    return claims.add(ctx.tenant, b.claim_key, b.typicality_note, b.valid_days, ctx.actor, b.statement)


@router.post("/claims/{rid}/evidence")
async def claims_evidence(rid: int, file: UploadFile = File(...), ctx: Ctx = Depends(kb_writer)):
    data = await file.read(claims.MAX_FILE_BYTES + 1)
    return claims.add_evidence(ctx.tenant, rid, file.filename or "evidence", data, ctx.actor)


@router.get("/claims/{rid}/evidence/{eid}")
def claims_evidence_file(rid: int, eid: int, ctx: Ctx = Depends(require_owner)):
    path, ev = claims.evidence_file(ctx.tenant, rid, eid)
    return FileResponse(path, media_type=ev.get("type") or "application/octet-stream", filename=ev.get("name"),
                        headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"})


class ExpireBody(BaseModel):
    reason: str = Field("", max_length=300)


@router.post("/claims/{rid}/expire")
def claims_expire(rid: int, b: ExpireBody, ctx: Ctx = Depends(kb_writer)):
    return claims.expire(ctx.tenant, rid, ctx.actor, b.reason)
