"""AI departments builder."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import departments as dp
from ..deps import Ctx, require

router = APIRouter()


@router.get("/builder/presets")
def presets(ctx: Ctx = Depends(require("builder"))):
    return {"presets": [{"key": p["key"], "name": p["name"], "niche": p["niche"], "offer": p["offer"], "demo": True}
                        for p in dp.PRESETS.values()],
            "own_fields": ["name", "niche", "offer", "price", "audience", "outcome", "mechanism", "channels", "faq"],
            "ai": dp.ai_reason(ctx.tenant) or "on"}


class OwnBusiness(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    niche: str = Field("", max_length=120)
    offer: str = Field("", max_length=200)
    price: float = Field(0, ge=0, le=1_000_000)
    audience: str = Field("", max_length=200)
    outcome: str = Field("", max_length=200)
    mechanism: str = Field("", max_length=200)
    channels: str = Field("", max_length=200)
    faq: str = Field("", max_length=2000)


class GenerateBody(BaseModel):
    preset: str | None = Field(None, pattern="^(physio|bookkeeping|gym)$")
    business: OwnBusiness | None = None


@router.post("/builder/generate")
def generate(b: GenerateBody, ctx: Ctx = Depends(require("builder"))):
    if b.preset:
        biz = dp.PRESETS[b.preset]
        key = b.preset
    elif b.business:
        biz = dp.business_from_profile(b.business.model_dump())
        key = "own"
    else:
        raise HTTPException(422, "Pick a preset or fill in your own business")
    gen = dp.generate(biz)
    build_id = dp.save_build(ctx.tenant, key, gen["business"], gen)
    return {"build_id": build_id, **gen}


@router.get("/builder/builds/{build_id}")
def build(build_id: int, ctx: Ctx = Depends(require("builder"))):
    try:
        return dp.get_build(ctx.tenant, build_id)
    except KeyError:
        raise HTTPException(404, "Build not found")


@router.post("/builder/builds/{build_id}/agents/{agent_key}/run")
def run(build_id: int, agent_key: str, ctx: Ctx = Depends(require("builder"))):
    try:
        run_id = dp.run_agent(ctx.tenant, build_id, agent_key, ctx.user, background=True)
    except KeyError:
        raise HTTPException(404, "Build or agent not found")
    return {"run_id": run_id}


@router.post("/builder/builds/{build_id}/start-over")
def start_over(build_id: int, ctx: Ctx = Depends(require("builder"))):
    return dp.start_over(ctx.tenant, build_id, ctx.actor)
