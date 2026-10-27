"""API for missions (scaffold — filled in by the missions build)."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..deps import Ctx, require

router = APIRouter()


@router.get("/missions")
def overview(ctx: Ctx = Depends(require("missions"))):
    return {"status": "not built yet"}
