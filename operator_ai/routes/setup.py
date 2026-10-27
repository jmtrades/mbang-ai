"""Workspace setup (owner), first-run checklists (everyone, their own) and per-user business memory
("What I know about you": everyone, their own; owner and a student's coach may read a student's)."""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .. import audit, db, memory, setup
from ..deps import Ctx, get_ctx, require

router = APIRouter()


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _err(e: setup.SetupError | memory.MemoryError_):
    raise HTTPException(e.status, str(e))


# --------------------------------------------------------------------------- owner checklist
@router.get("/setup")
def overview(ctx: Ctx = Depends(require("setup"))):
    return setup.checklist(ctx.tenant)


@router.get("/setup/preflight")
def preflight(ctx: Ctx = Depends(require("setup"))):
    rows = setup.preflight(ctx.tenant)
    return {"rows": rows, "ok": sum(1 for r in rows if r["ok"]), "total": len(rows),
            "note": "The same checks as scripts/preflight_live.py. Values are never shown."}


class BrandBody(_Body):
    name: str | None = Field(None, max_length=60)
    owner_display: str | None = Field(None, max_length=60)
    primary: str | None = Field(None, max_length=7)
    accent: str | None = Field(None, max_length=7)
    logo: str | None = Field(None, max_length=200_000)


@router.post("/setup/brand")
def brand(b: BrandBody, ctx: Ctx = Depends(require("setup"))):
    try:
        return {"brand": setup.save_brand(ctx.tenant, b.model_dump(exclude_none=True), ctx.actor)}
    except setup.SetupError as e:
        _err(e)


class DecideBody(_Body):
    item: str = Field(pattern="^(persona|backups)$")
    value: str = Field(pattern="^(use|not_now|configured_elsewhere|not_yet)$")


@router.post("/setup/decide")
def decide(b: DecideBody, ctx: Ctx = Depends(require("setup"))):
    try:
        return {"decisions": setup.decide(ctx.tenant, b.item, b.value, ctx.actor)}
    except setup.SetupError as e:
        _err(e)


# --------------------------------------------------------------------------- first-run (own)
@router.get("/setup/first-run")
def first_run(role: str | None = Query(None, pattern="^(owner|coach|setter|closer|student)$"), ctx: Ctx = Depends(get_ctx)):
    try:
        return setup.first_run(ctx.tenant, ctx.user, role)
    except setup.SetupError as e:
        _err(e)


class MarkBody(_Body):
    done: bool


@router.post("/setup/first-run/{item}")
def mark(item: str, b: MarkBody, ctx: Ctx = Depends(get_ctx)):
    try:
        return setup.mark(ctx.tenant, ctx.user, item, b.done)
    except setup.SetupError as e:
        _err(e)


# --------------------------------------------------------------------------- memory (own)
class NumberIn(_Body):
    label: str = Field(max_length=60)
    value: str = Field(max_length=40)
    source: str | None = Field(None, max_length=40)


class DecisionIn(_Body):
    text: str = Field(max_length=300)
    at: float | None = None
    source: str | None = Field(None, max_length=40)


class ProfileIn(_Body):
    offer: str = Field("", max_length=300)
    niche: str = Field("", max_length=200)
    goal: str = Field("", max_length=300)
    notes: str = Field("", max_length=2000)
    price: float | None = Field(None, ge=0, le=10_000_000)
    currency: str = Field("USD", pattern="^[A-Z]{3}$")


class MemoryBody(_Body):
    profile: ProfileIn
    numbers: list[NumberIn] = Field(default_factory=list, max_length=memory.MAX_NUMBERS)
    decisions: list[DecisionIn] = Field(default_factory=list, max_length=memory.MAX_DECISIONS)


@router.get("/memory")
def get_memory(ctx: Ctx = Depends(get_ctx)):
    out = memory.get(ctx.tenant, ctx.user["id"])
    out["suggestions"] = memory.suggestions(ctx.tenant, ctx.user)
    return out


@router.put("/memory")
def put_memory(b: MemoryBody, ctx: Ctx = Depends(get_ctx)):
    try:
        return memory.put(ctx.tenant, ctx.user["id"], b.model_dump(), ctx.actor)
    except memory.MemoryError_ as e:
        _err(e)


@router.delete("/memory")
def delete_memory(ctx: Ctx = Depends(get_ctx)):
    return {"ok": True, "deleted": memory.delete_all(ctx.tenant, ctx.user["id"], ctx.actor)}


@router.get("/memory/export")
def export_memory(ctx: Ctx = Depends(get_ctx)):
    return JSONResponse(memory.export(ctx.tenant, ctx.user["id"], ctx.actor),
                        headers={"Content-Disposition": 'attachment; filename="what-i-know-about-you.json"'})


@router.get("/memory/user/{uid}")
def read_member_memory(uid: Annotated[int, Path(ge=1, le=2**62)], ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    """Read-only: the owner sees anyone's; a coach sees only their own students'; nobody else."""
    if uid == ctx.user["id"]:
        return memory.get(ctx.tenant, uid)
    with ctx.tenant.db() as c:
        u = db.row(c.execute("SELECT u.id, u.role, COALESCE(s.coach_id, u.coach_id) coach_id FROM users u "
                             "LEFT JOIN students s ON s.id=u.student_id WHERE u.id=? AND u.deleted IS NULL", (uid,)).fetchone())
    # the student record says who the coach is (it is what Accountability uses)
    allowed = u is not None and (ctx.role == "owner" or (ctx.role == "coach" and u["role"] == "student"
                                                         and u["coach_id"] == ctx.user["id"]))
    if not allowed:
        raise HTTPException(404, "Not found")
    # someone else's memory was opened: that is on the record (the values never are)
    audit.log(ctx.tenant, ctx.actor, "memory.read", {"user_id": uid})
    out = memory.get(ctx.tenant, uid)
    out["read_only"] = True
    return out
