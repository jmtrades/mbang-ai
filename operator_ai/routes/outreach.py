"""Approval-gated outreach loop on practice prospects (DEMO)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import outreach_loop as ol
from ..deps import Ctx, require

router = APIRouter()


@router.get("/outreach")
def overview(ctx: Ctx = Depends(require("outreach"))):
    return {"prospects": ol.prospects(ctx.tenant), "lab": ol.opener_lab(ctx.tenant), "funnel": ol.weekly_funnel(ctx.tenant),
            "league": ol.league(ctx.tenant), "cap": ol.cap(ctx.tenant), "stages": ol.STAGES}


class OpenersBody(BaseModel):
    limit: int = Field(10, ge=1, le=100)
    sender: str | None = Field(None, max_length=60)


@router.post("/outreach/openers")
def openers(b: OpenersBody, ctx: Ctx = Depends(require("outreach"))):
    return ol.queue_openers(ctx.tenant, b.sender, b.limit, ctx.user)


@router.post("/outreach/followups")
def followups(ctx: Ctx = Depends(require("outreach"))):
    return ol.queue_followups(ctx.tenant, ctx.user)


class ReplyBody(BaseModel):
    text: str = Field("", max_length=1000)


@router.post("/outreach/prospects/{pid}/reply")
def reply(pid: int, b: ReplyBody, ctx: Ctx = Depends(require("outreach"))):
    try:
        return ol.record_reply(ctx.tenant, pid, b.text)
    except KeyError:
        raise HTTPException(404, "Prospect not found")


class StageBody(BaseModel):
    stage: str = Field(pattern="^(new|sent|replied|quality_conversation|link_sent|booked|showed|closed)$")


@router.post("/outreach/prospects/{pid}/stage")
def stage(pid: int, b: StageBody, ctx: Ctx = Depends(require("outreach"))):
    try:
        return ol.advance(ctx.tenant, pid, b.stage)
    except KeyError:
        raise HTTPException(404, "Prospect not found")


class GoalBody(BaseModel):
    goal: float = Field(gt=0, le=10_000_000)
    price: float = Field(gt=0, le=1_000_000)
    close_rate: float = Field(0.2, gt=0, le=1)
    show_rate: float = Field(0.6, gt=0, le=1)
    book_rate: float = Field(0.3, gt=0, le=1)
    reply_rate: float = Field(0.1, gt=0, le=1)
    senders: int = Field(1, ge=1, le=100)


@router.post("/outreach/goal")
def goal(b: GoalBody, ctx: Ctx = Depends(require("outreach"))):
    return ol.goal_calculator(b.goal, b.price, b.close_rate, b.show_rate, b.book_rate, b.reply_rate, senders=b.senders,
                              per_sender_cap=ol.cap(ctx.tenant))
