"""API for prospects (scaffold — filled in by the prospects build)."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..deps import Ctx, require

router = APIRouter()


@router.get("/prospects")
def overview(ctx: Ctx = Depends(require("prospects"))):
    return {"status": "not built yet"}
