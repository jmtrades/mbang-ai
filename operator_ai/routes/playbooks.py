"""API for playbooks (scaffold — filled in by the playbooks build)."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..deps import Ctx, require

router = APIRouter()


@router.get("/playbooks")
def overview(ctx: Ctx = Depends(require("playbooks"))):
    return {"status": "not built yet"}
