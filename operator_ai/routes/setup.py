"""API for setup (scaffold — filled in by the setup build)."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..deps import Ctx, require

router = APIRouter()


@router.get("/setup")
def overview(ctx: Ctx = Depends(require("setup"))):
    return {"status": "not built yet"}
