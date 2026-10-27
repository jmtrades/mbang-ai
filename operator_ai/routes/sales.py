"""Leads, qualification, booking, burst, rescue desk."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .. import db
from ..agents import sales
from ..deps import Ctx, require, require_owner

router = APIRouter()


@router.get("/leads")
def leads(tier: str | None = Query(None, max_length=10), batch: str | None = Query(None, max_length=80),
          limit: int = Query(200, ge=1, le=1000), ctx: Ctx = Depends(require("sales"))):
    q = "SELECT * FROM leads WHERE 1=1"
    args: list[Any] = []
    if tier:
        q += " AND tier=?"
        args.append(tier)
    if batch:
        q += " AND batch=?"
        args.append(batch)
    else:
        q += " AND batch IS NULL"
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with ctx.tenant.db() as c:
        rows = db.rows(c.execute(q, args))
        split = {r["tier"] or "unscored": r["n"] for r in c.execute(
            "SELECT tier, COUNT(*) n FROM leads WHERE batch IS NULL GROUP BY tier")}
    for r in rows:
        r["answers"] = db.jload(r["answers"], {})
    return {"items": rows, "tiers": split}


@router.post("/leads/{lead_id}/process")
def process(lead_id: int, ctx: Ctx = Depends(require("sales"))):
    try:
        return sales.process_lead(ctx.tenant, lead_id, ctx.user)
    except KeyError:
        raise HTTPException(404, "Lead not found")


@router.get("/leads/weights")
def get_weights(ctx: Ctx = Depends(require("sales"))):
    return {"weights": sales.weights(ctx.tenant), "tiers": sales.TIERS, "routes": sales.ROUTES}


class WeightsBody(BaseModel):
    weights: dict[str, dict[str, float]]


@router.put("/leads/weights")
def put_weights(b: WeightsBody, ctx: Ctx = Depends(require_owner)):
    from .. import settings

    clean = {k: v for k, v in b.weights.items() if k in sales.DEFAULT_WEIGHTS}
    settings.update_from_api(ctx.tenant, "lead_weights", clean, ctx.actor)
    return {"weights": sales.weights(ctx.tenant)}


class BurstBody(BaseModel):
    n: int = Field(500, ge=1, le=2000)


@router.post("/burst")
def burst(b: BurstBody, ctx: Ctx = Depends(require_owner)):
    from .. import ratelimit

    if ctx.tenant.is_demo and b.n > ratelimit.DEMO_BURST_MAX:
        raise HTTPException(422, f"The pitch demo runs bursts of up to {ratelimit.DEMO_BURST_MAX} leads.")
    ratelimit.demo_heavy(ctx.tenant, ctx.ip)
    return sales.start_burst(ctx.tenant, b.n)


@router.get("/burst/{batch}")
def burst_status(batch: str, ctx: Ctx = Depends(require("sales"))):
    return sales.burst_status(ctx.tenant, batch)


@router.get("/rescue")
def rescue(ctx: Ctx = Depends(require("sales"))):
    return {"items": sales.rescue_desk(ctx.tenant)}
