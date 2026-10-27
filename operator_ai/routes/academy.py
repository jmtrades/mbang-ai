"""API for academy (scaffold — filled in by the academy build)."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..deps import Ctx, require

router = APIRouter()


@router.get("/academy")
def overview(ctx: Ctx = Depends(require("academy"))):
    return {"status": "not built yet"}
