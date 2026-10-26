"""Studio lines, Launch kit, brief builder, webinar chain, rehearsal scorer."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import brief, studio, webinar_chain
from ..deps import Ctx, require

router = APIRouter()


class BriefBody(BaseModel):
    brief: dict[str, Any] = Field(default_factory=dict)


@router.post("/brief")
def build_brief(b: BriefBody, ctx: Ctx = Depends(require("studio"))):
    return brief.build(ctx.tenant, b.brief)


@router.get("/studio/lines")
def lines(ctx: Ctx = Depends(require("studio"))):
    return {"lines": studio.LINES}


class LineBody(BaseModel):
    line: str = Field(pattern="^(outreach|ads|funnels|creatives|webinars|training|delivery)$")
    brief: dict[str, Any] = Field(default_factory=dict)


@router.post("/studio/run")
def run_line(b: LineBody, ctx: Ctx = Depends(require("studio"))):
    return studio.run_line(ctx.tenant, b.line, b.brief, ctx.user)


@router.post("/studio/launch-kit")
def launch_kit(b: BriefBody, ctx: Ctx = Depends(require("studio"))):
    return studio.launch_kit(ctx.tenant, b.brief, ctx.user)


@router.post("/webinar/chain")
def chain(b: BriefBody, ctx: Ctx = Depends(require("webinar"))):
    return webinar_chain.run_chain(ctx.tenant, b.brief, ctx.user)


@router.get("/webinar/chain/{chain_id}")
def chain_state(chain_id: str, ctx: Ctx = Depends(require("webinar"))):
    try:
        return webinar_chain.state(ctx.tenant, chain_id)
    except KeyError:
        raise HTTPException(404, "Chain not found")


@router.get("/webinar/chains")
def chains(ctx: Ctx = Depends(require("webinar"))):
    with ctx.tenant.db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT chain_id, MIN(created) created, MAX(version) version FROM chain_steps GROUP BY chain_id ORDER BY created DESC LIMIT 50")]
    return {"items": rows}


class RerunBody(BaseModel):
    step: str = Field(min_length=1, max_length=40)
    brief: dict[str, Any] = Field(default_factory=dict)


@router.post("/webinar/chain/{chain_id}/rerun")
def rerun(chain_id: str, b: RerunBody, ctx: Ctx = Depends(require("webinar"))):
    try:
        return webinar_chain.rerun_step(ctx.tenant, chain_id, b.step, b.brief, ctx.user)
    except KeyError:
        raise HTTPException(404, "Chain or step not found")


class RehearsalBody(BaseModel):
    script: str = Field(min_length=2, max_length=50000)
    minutes: dict[str, float] = Field(default_factory=dict)


@router.post("/webinar/rehearsal")
def rehearsal(b: RehearsalBody, ctx: Ctx = Depends(require("webinar"))):
    return webinar_chain.rehearsal_score(ctx.tenant, b.script, b.minutes)
