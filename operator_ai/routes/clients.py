"""Clients and fulfilment."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import db
from ..agents import fulfilment
from ..deps import Ctx, require

router = APIRouter()


@router.get("/clients")
def clients(ctx: Ctx = Depends(require("clients"))):
    with ctx.tenant.db() as c:
        rows = db.rows(c.execute("SELECT * FROM clients ORDER BY id"))
    for r in rows:
        r["metrics"] = db.jload(r["metrics"], {})
    return {"items": rows}


class ClientBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    niche: str = Field("", max_length=120)
    offer: str = Field("", max_length=300)
    price: float = Field(3000, ge=0, le=1_000_000)
    metrics: dict[str, float] = Field(default_factory=dict)


@router.post("/clients")
def add_client(b: ClientBody, ctx: Ctx = Depends(require("clients"))):
    with ctx.tenant.db() as c:
        cid = c.execute("INSERT INTO clients(name,niche,offer,price,metrics,demo,created) VALUES(?,?,?,?,?,?,?)",
                        (b.name, b.niche, b.offer, b.price, db.jdump(b.metrics), 1 if ctx.tenant.is_demo else 0,
                         db.now())).lastrowid
    return {"id": cid}


@router.post("/clients/{client_id}/fulfil")
def fulfil(client_id: int, ctx: Ctx = Depends(require("clients"))):
    try:
        return fulfilment.fulfil(ctx.tenant, client_id, ctx.user)
    except KeyError:
        raise HTTPException(404, "Client not found")
