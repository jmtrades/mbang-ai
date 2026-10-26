"""Ads Ops (mock adapter by default) and the action history."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import actions
from ..adapters.ads import MockAdsAdapter, adapter_for
from ..agents import ads_ops
from ..deps import Ctx, require, require_owner

router = APIRouter()


@router.get("/ads")
def account(ctx: Ctx = Depends(require("ads"))):
    acct = ads_ops.practice_account(ctx.tenant)
    ad = adapter_for(ctx.tenant, acct)
    return {"account": acct, "adapter": ad.name, "tree": MockAdsAdapter(ctx.tenant).tree(acct),
            "campaigns": MockAdsAdapter(ctx.tenant).list("campaign", acct),
            "live_switch": bool(ctx.tenant.get("live_meta_enabled", False)),
            "allowlist_size": len(ctx.tenant.get("meta_allowlist", []) or [])}


class DiagBody(BaseModel):
    target_cpl: float = Field(10.0, gt=0, le=10000)


@router.post("/ads/diagnose")
def diagnose(b: DiagBody, ctx: Ctx = Depends(require("ads"))):
    return ads_ops.diagnose(ctx.tenant, None, b.target_cpl, ctx.user)


@router.post("/ads/propose")
def propose(b: DiagBody, ctx: Ctx = Depends(require("ads"))):
    return ads_ops.propose_changes(ctx.tenant, None, b.target_cpl, ctx.user)


class BuildBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    creatives: list[str] = Field(min_length=1, max_length=5)


@router.post("/ads/build-test")
def build(b: BuildBody, ctx: Ctx = Depends(require("ads"))):
    return ads_ops.build_test(ctx.tenant, b.name, b.creatives, None, ctx.user)


class GoLiveBody(BaseModel):
    campaign_id: str = Field(min_length=1, max_length=60)
    daily_budget: float = Field(gt=0, le=10000)


@router.post("/ads/go-live")
def go_live(b: GoLiveBody, ctx: Ctx = Depends(require("ads"))):
    try:
        return ads_ops.propose_go_live(ctx.tenant, b.campaign_id, b.daily_budget, ctx.user)
    except KeyError:
        raise HTTPException(404, "Campaign not found")


@router.post("/ads/reset")
def reset(ctx: Ctx = Depends(require_owner)):
    return ads_ops.reset_practice(ctx.tenant, ctx.actor)


@router.get("/actions")
def history(ctx: Ctx = Depends(require("ads"))):
    return {"items": actions.history(ctx.tenant)}


@router.post("/actions/{aid}/rollback")
def rollback(aid: int, ctx: Ctx = Depends(require_owner)):
    try:
        return actions.rollback(ctx.tenant, aid, ctx.actor)
    except actions.ActionError as e:
        raise HTTPException(409, str(e))
