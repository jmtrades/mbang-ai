"""Day One, Command Centre, Morning brief, Go-live scorecard."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from .. import reports, scorecard
from ..deps import Ctx, require

router = APIRouter()


@router.get("/day-one")
def day_one(ctx: Ctx = Depends(require("home"))):
    return reports.day_one(ctx.tenant)


@router.get("/command-centre")
def command_centre(ctx: Ctx = Depends(require("command"))):
    return reports.command_centre(ctx.tenant)


@router.get("/brief")
def brief(ctx: Ctx = Depends(require("brief"))):
    return reports.morning_brief(ctx.tenant, ctx.user)


@router.get("/golive")
def golive(ctx: Ctx = Depends(require("golive"))):
    rows = scorecard.rows_for(ctx.tenant)
    return {"rows": rows, "totals": scorecard.totals(rows),
            "labels": {"REAL_OK": "Works now on real or practice/mock paths", "NEEDS_LOGIN": "Built on a mock; only a key or account is missing",
                       "NEEDS_AI_BUDGET": "Rules/replay path works; the rest needs a model"}}
