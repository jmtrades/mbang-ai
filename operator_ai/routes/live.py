"""Live layer endpoints: the signed Meta lead webhook (public) and the chain controls."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import PlainTextResponse

from .. import tenancy
from ..deps import Ctx, get_ctx, get_tenant, require
from ..live import chain, meta

router = APIRouter()
public_router = APIRouter()
LeadId = Annotated[int, Path(ge=1, le=2 ** 53)]  # beyond this: malformed input (422), never a DB overflow (500)


@public_router.get("/t/{tid}/hooks/meta")
def meta_handshake(tenant: tenancy.Tenant = Depends(get_tenant), hub_mode: str | None = Query(None, alias="hub.mode"),
                   hub_token: str | None = Query(None, alias="hub.verify_token"),
                   hub_challenge: str | None = Query(None, alias="hub.challenge")):
    try:
        return PlainTextResponse(meta.verify_handshake(tenant, hub_mode, hub_token, hub_challenge))
    except meta.WebhookRejected as e:
        raise HTTPException(e.status, str(e))


@public_router.post("/t/{tid}/hooks/meta")
async def meta_webhook(request: Request, tenant: tenancy.Tenant = Depends(get_tenant)):
    raw = await request.body()
    try:
        return chain.receive_webhook(tenant, raw, request.headers.get("x-hub-signature-256"))
    except meta.WebhookRejected as e:
        raise HTTPException(e.status, str(e))


@public_router.get("/t/{tid}/report")
def tenant_report(ctx: Ctx = Depends(get_ctx)):
    if ctx.role not in ("owner", "coach", "closer"):
        raise HTTPException(403, "Not for your role")
    return chain.report(ctx.tenant)


@router.get("/chain")
def chain_leads(ctx: Ctx = Depends(require("sales"))):
    from .. import db

    with ctx.tenant.db() as c:
        leads = db.rows(c.execute("SELECT * FROM chain_leads ORDER BY id DESC LIMIT 200"))
    for r in leads:
        r["data"] = db.jload(r["data"], {})
        r["citations"] = db.jload(r["citations"], [])
    return {"leads": leads, "report": chain.report(ctx.tenant),
            "live": {"meta": bool(ctx.tenant.get("live_meta_enabled", False)),
                     "calendar": bool(ctx.tenant.get("live_calendar_enabled", False))}}


from pydantic import BaseModel, Field  # noqa: E402


class ReplyBody(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    # a person's reading of the reply; omitted -> classified by rules
    classification: str | None = Field(None, pattern="^(positive|negative|question|neutral|stop)$")


@router.post("/chain/leads/{lead_id}/reply")
def reply(lead_id: LeadId, b: ReplyBody, ctx: Ctx = Depends(require("sales"))):
    try:
        return chain.record_reply(ctx.tenant, lead_id, b.text, b.classification)
    except KeyError:
        raise HTTPException(404, "Lead not found")


class OutcomeBody(BaseModel):
    outcome: str = Field(pattern="^(showed|no_show|closed|lost)$")


@router.post("/chain/leads/{lead_id}/outcome")
def outcome(lead_id: LeadId, b: OutcomeBody, ctx: Ctx = Depends(require("sales"))):
    try:
        return chain.record_outcome(ctx.tenant, lead_id, b.outcome)
    except KeyError:
        raise HTTPException(404, "Lead not found")
