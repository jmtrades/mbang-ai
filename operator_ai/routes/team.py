"""API for team (scaffold — filled in by the team build)."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..deps import Ctx, require

router = APIRouter()


@router.get("/team")
def overview(ctx: Ctx = Depends(require("team"))):
    return {"status": "not built yet"}
