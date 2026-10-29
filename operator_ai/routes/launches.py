"""One-click webinar launch (owner and students, "clients" tab): build, schedule, one approval, slide deck."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .. import webinar_launch
from ..deps import Ctx, require

router = APIRouter()


class LaunchBody(BaseModel):
    starts_at: float | str
    tz: str = Field(webinar_launch.DEFAULT_TZ, min_length=1, max_length=60)
    title: str = Field("", max_length=140)
    client_id: int | None = Field(None, ge=1)
    duration_min: int = Field(60, ge=15, le=240)
    niche: str = Field("", max_length=120)
    audience: str = Field("", max_length=200)
    offer: str = Field("", max_length=300)
    price: float | None = Field(None, ge=0, le=1_000_000)
    outcome: str = Field("", max_length=200)
    mechanism: str = Field("", max_length=120)
    pain: str = Field("", max_length=200)
    timeframe: str = Field("", max_length=60)


def _err(e: webinar_launch.LaunchError):
    raise HTTPException(e.status, str(e))


@router.post("/launches")
def create_launch(b: LaunchBody, ctx: Ctx = Depends(require("clients"))):
    try:
        return webinar_launch.launch(ctx.tenant, b.model_dump(), ctx.user)
    except webinar_launch.LaunchError as e:
        _err(e)


@router.get("/launches")
def list_launches(ctx: Ctx = Depends(require("clients"))):
    return {"items": webinar_launch.list_launches(ctx.tenant, ctx.user)}


@router.get("/launches/{wid}")
def launch_detail(wid: int, ctx: Ctx = Depends(require("clients"))):
    try:
        return webinar_launch.summary(ctx.tenant, wid, ctx.user)
    except webinar_launch.LaunchError as e:
        _err(e)


@router.get("/launches/{wid}/deck.pptx")
def launch_deck(wid: int, ctx: Ctx = Depends(require("clients"))):
    try:
        name, data = webinar_launch.deck(ctx.tenant, wid, ctx.user)
    except webinar_launch.LaunchError as e:
        _err(e)
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})
