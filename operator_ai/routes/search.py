"""Library search and source viewing."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from .. import retrieval
from ..deps import Ctx, get_ctx

router = APIRouter()


@router.get("/search")
def search(q: str = Query(min_length=1, max_length=500), k: int = Query(10, ge=1, le=50), ctx: Ctx = Depends(get_ctx)):
    hits = retrieval.search(ctx.tenant, q, k=k)
    return {"query": q, "hits": [{kk: v for kk, v in h.items() if kk != "text"} for h in hits]}


@router.get("/source")
def source(file: str = Query(min_length=1, max_length=300), lines: str = Query("", max_length=40),
           ctx: Ctx = Depends(get_ctx)):
    view = retrieval.source_view(ctx.tenant, file, lines)
    if view is None:
        raise HTTPException(404, "Not an indexed source file")
    return view
