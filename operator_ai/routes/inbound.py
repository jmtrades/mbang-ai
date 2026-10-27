"""Inbox and escalations API.

Everyone with the approval queue sees their own conversations (a coach also sees their students'); the
owner sees all. Triage of unknown senders, the SLA check and alert settings are owner-only (Channels tab).
Drafted replies are approved, edited or rejected through the normal /queue endpoints."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .. import auth, escalations, inbound, llm
from ..deps import Ctx, get_ctx, require

router = APIRouter()


def _err(e: Exception):
    raise HTTPException(getattr(e, "status", 500), str(e))


def _capture_ctx(ctx: Ctx = Depends(get_ctx)) -> Ctx:
    """Pasting a reply in is for people who run outreach: anyone with the Prospects or Channels tab."""
    if not (auth.has_tab(ctx.role, "prospects") or auth.has_tab(ctx.role, "channels")):
        raise HTTPException(403, "Your role can't add replies to the inbox")
    return ctx


# ---------------------------------------------------------------- inbox
@router.get("/inbox")
def inbox_list(view: Literal["all", "needs_reply", "triage", "escalated"] = "all",
               limit: int = Query(100, ge=1, le=500), ctx: Ctx = Depends(require("queue"))):
    data = inbound.inbox(ctx.tenant, ctx.user, view, limit)
    reason = llm.unavailable_reason(ctx.tenant)
    return {**data, "escalations": escalations.counts(ctx.tenant, ctx.user),
            "ai": {"on": reason is None,
                   "label": "AI can break ties when the rules can't sort a reply" if reason is None else
                   "AI is off: replies are sorted by keyword rules and drafted from templates"},
            "labels": inbound.LABELS, "channels": inbound.CHANNEL_LABELS,
            "can_triage": auth.has_tab(ctx.role, "channels"),
            "can_capture": auth.has_tab(ctx.role, "prospects") or auth.has_tab(ctx.role, "channels")}


@router.get("/inbox/thread")
def inbox_thread(key: str = Query(..., min_length=3, max_length=60, pattern=r"^[a-z_]+:\d{1,12}$"),
                 ctx: Ctx = Depends(require("queue"))):
    try:
        return inbound.thread(ctx.tenant, ctx.user, key)
    except inbound.InboundError as e:
        _err(e)


class CaptureBody(BaseModel):
    channel: Literal["instagram_dm", "linkedin", "facebook", "manual"] = "instagram_dm"
    handle: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=4000)
    prospect_id: int | None = Field(None, ge=1)


@router.post("/inbox/capture")
def inbox_capture(b: CaptureBody, ctx: Ctx = Depends(_capture_ctx)):
    if not b.body.strip():
        raise HTTPException(422, "Paste the message they sent")
    try:
        return inbound.capture(ctx.tenant, ctx.user, b.channel, b.handle, b.body, b.prospect_id, ctx.actor)
    except inbound.InboundError as e:
        _err(e)


class AssignBody(BaseModel):
    party_type: Literal["prospect", "lead", "client", "student"]
    party_id: int = Field(ge=1)


@router.post("/inbox/{inbound_id}/assign")
def inbox_assign(inbound_id: int, b: AssignBody, ctx: Ctx = Depends(require("channels"))):
    try:
        return inbound.assign(ctx.tenant, ctx.user, inbound_id, b.party_type, b.party_id, ctx.actor)
    except inbound.InboundError as e:
        _err(e)


class ClassifyBody(BaseModel):
    classification: Literal["positive", "question", "price_ask", "objection_not_now", "objection_cost",
                            "objection_partner", "not_interested", "unsubscribe", "out_of_office", "legal_complaint"]


@router.post("/inbox/{inbound_id}/classify")
def inbox_classify(inbound_id: int, b: ClassifyBody, ctx: Ctx = Depends(require("queue"))):
    try:
        return inbound.reclassify(ctx.tenant, ctx.user, inbound_id, b.classification, ctx.actor)
    except inbound.InboundError as e:
        _err(e)


@router.post("/inbox/{inbound_id}/dismiss")
def inbox_dismiss(inbound_id: int, ctx: Ctx = Depends(require("queue"))):
    try:
        return inbound.dismiss(ctx.tenant, ctx.user, inbound_id, ctx.actor)
    except inbound.InboundError as e:
        _err(e)


@router.get("/inbox/parties")
def inbox_parties(q: str = Query(..., min_length=2, max_length=80), ctx: Ctx = Depends(require("channels"))):
    return {"items": inbound.parties(ctx.tenant, q)}


class InboxSettings(BaseModel):
    telegram_alerts: bool


@router.get("/inbox/settings")
def inbox_settings(ctx: Ctx = Depends(require("channels"))):
    with ctx.tenant.db() as c:
        linked = c.execute("SELECT COUNT(*) FROM channel_links WHERE channel='telegram' AND user_id=? AND status='linked'",
                           (ctx.user["id"],)).fetchone()[0]
    return {"telegram_alerts": bool(ctx.tenant.get("escalation_telegram_alerts", False)), "telegram_linked": bool(linked),
            "sla_minutes": inbound.SLA_SECONDS // 60, "held_hours": inbound.HELD_SECONDS // 3600}


@router.put("/inbox/settings")
def inbox_settings_put(b: InboxSettings, ctx: Ctx = Depends(require("channels"))):
    from .. import audit

    ctx.tenant.set("escalation_telegram_alerts", bool(b.telegram_alerts))
    audit.log(ctx.tenant, ctx.actor, "settings", {"key": "escalation_telegram_alerts", "value": bool(b.telegram_alerts)})
    return inbox_settings(ctx)


# ---------------------------------------------------------------- escalations
@router.get("/escalations")
def escalation_list(status: Literal["open", "acked", "resolved", "all"] = "open", ctx: Ctx = Depends(require("queue"))):
    return {"items": escalations.list_for(ctx.tenant, ctx.user, status), "counts": escalations.counts(ctx.tenant, ctx.user),
            "kinds": escalations.KINDS}


@router.post("/escalations/{eid}/ack")
def escalation_ack(eid: int, ctx: Ctx = Depends(require("queue"))):
    try:
        return escalations.ack(ctx.tenant, eid, ctx.user, ctx.actor)
    except escalations.EscalationError as e:
        _err(e)


class ResolveBody(BaseModel):
    note: str = Field("", max_length=500)


@router.post("/escalations/{eid}/resolve")
def escalation_resolve(eid: int, b: ResolveBody | None = None, ctx: Ctx = Depends(require("queue"))):
    try:
        return escalations.resolve(ctx.tenant, eid, ctx.user, ctx.actor, b.note if b else "")
    except escalations.EscalationError as e:
        _err(e)


@router.post("/escalations/check")
def escalation_check(ctx: Ctx = Depends(require("channels"))):
    return {"checked": inbound.check_slas(ctx.tenant), "counts": escalations.counts(ctx.tenant, ctx.user)}
