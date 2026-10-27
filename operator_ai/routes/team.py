"""Team & invites (owner only) and the public join / reset / forgot-password endpoints.

Every body model forbids unknown fields, so a malformed or unexpected body is a 422 and never changes anything.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .. import team, tenancy
from ..deps import Ctx, client_ip, get_tenant, require
from .core import _set_cookie

router = APIRouter()
public_router = APIRouter(prefix="/t/{tid}/api")


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _err(e: team.TeamError):
    raise HTTPException(e.status, str(e))


_ORIGIN = Field(None, max_length=200, pattern=r"^https?://[A-Za-z0-9.\-]+(:\d{1,5})?$")


# --------------------------------------------------------------------------- overview
@router.get("/team")
def overview(ctx: Ctx = Depends(require("team"))):
    return team.overview(ctx.tenant)


@router.get("/team/students")
def students(q: str = Query("", max_length=80), limit: int = Query(300, ge=1, le=2000),
             ctx: Ctx = Depends(require("team"))):
    return team.list_students(ctx.tenant, q, limit)


# --------------------------------------------------------------------------- invites
class InviteBody(_Body):
    email: str | None = Field(None, max_length=200)
    role: str = Field(pattern="^(coach|setter|closer|student)$")
    display: str | None = Field(None, max_length=80)
    coach_id: int | None = Field(None, ge=1)
    student_id: int | None = Field(None, ge=1)
    send_email: bool = True
    origin: str | None = _ORIGIN


@router.post("/team/invites")
def invite(b: InviteBody, ctx: Ctx = Depends(require("team"))):
    try:
        return team.create_invite(ctx.tenant, ctx.actor, email=b.email, role=b.role, display=b.display,
                                  coach_id=b.coach_id, student_id=b.student_id, send_email=b.send_email,
                                  origin=b.origin, inviter=ctx.user.get("display") or "")
    except team.TeamError as e:
        _err(e)


class NoteBody(_Body):
    reason: str = Field("", max_length=300)


@router.post("/team/invites/{iid}/revoke")
def revoke(iid: int, b: NoteBody, ctx: Ctx = Depends(require("team"))):
    try:
        return team.revoke_invite(ctx.tenant, iid, ctx.actor)
    except team.TeamError as e:
        _err(e)


# --------------------------------------------------------------------------- people
class RoleBody(_Body):
    role: str = Field(pattern="^(coach|setter|closer|student)$")


@router.post("/team/users/{uid}/role")
def change_role(uid: int, b: RoleBody, ctx: Ctx = Depends(require("team"))):
    if uid == ctx.user["id"]:
        raise HTTPException(409, "You can't change your own role.")
    try:
        return team.set_role(ctx.tenant, uid, b.role, ctx.actor)
    except team.TeamError as e:
        _err(e)


@router.post("/team/users/{uid}/deactivate")
def deactivate(uid: int, b: NoteBody, ctx: Ctx = Depends(require("team"))):
    if uid == ctx.user["id"]:
        raise HTTPException(409, "You can't switch off your own account.")
    try:
        return team.set_active(ctx.tenant, uid, False, ctx.actor)
    except team.TeamError as e:
        _err(e)


@router.post("/team/users/{uid}/reactivate")
def reactivate(uid: int, b: NoteBody, ctx: Ctx = Depends(require("team"))):
    try:
        return team.set_active(ctx.tenant, uid, True, ctx.actor)
    except team.TeamError as e:
        _err(e)


class ResetBody(_Body):
    send_email: bool = True
    origin: str | None = _ORIGIN


@router.post("/team/users/{uid}/reset-link")
def reset_link(uid: int, b: ResetBody, ctx: Ctx = Depends(require("team"))):
    try:
        return team.create_reset_link(ctx.tenant, uid, ctx.actor, send_email=b.send_email, origin=b.origin,
                                      inviter=ctx.user.get("display") or "")
    except team.TeamError as e:
        _err(e)


@router.get("/team/users/{uid}/export")
def export(uid: int, ctx: Ctx = Depends(require("team"))):
    try:
        data = team.export_user(ctx.tenant, uid, ctx.actor)
    except team.TeamError as e:
        _err(e)
    return JSONResponse(data, headers={"Content-Disposition": f'attachment; filename="user-{uid}-export.json"'})


class DeleteBody(_Body):
    confirm: str = Field(min_length=1, max_length=200)


@router.post("/team/users/{uid}/delete")
def delete(uid: int, b: DeleteBody, ctx: Ctx = Depends(require("team"))):
    try:
        return team.delete_user(ctx.tenant, uid, b.confirm, ctx.actor, actor_id=ctx.user["id"])
    except team.TeamError as e:
        _err(e)


# --------------------------------------------------------------------------- students + assignments
class CoachBody(_Body):
    coach_id: int | None = Field(None, ge=1)


@router.post("/team/students/{sid}/coach")
def set_coach(sid: int, b: CoachBody, ctx: Ctx = Depends(require("team"))):
    try:
        return team.assign_coach(ctx.tenant, sid, b.coach_id, ctx.actor)
    except team.TeamError as e:
        _err(e)


class ImportBody(_Body):
    text: str = Field(min_length=1, max_length=400_000)
    coach_id: int | None = Field(None, ge=1)
    invite: bool = False
    origin: str | None = _ORIGIN


@router.post("/team/students/import")
def import_students(b: ImportBody, ctx: Ctx = Depends(require("team"))):
    try:
        return team.import_students(ctx.tenant, b.text, ctx.actor, coach_id=b.coach_id, invite=b.invite,
                                    origin=b.origin, inviter=ctx.user.get("display") or "")
    except team.TeamError as e:
        _err(e)


class AssignBody(_Body):
    manager_id: int = Field(ge=1)
    member_id: int = Field(ge=1)


@router.post("/team/assignments")
def assign(b: AssignBody, ctx: Ctx = Depends(require("team"))):
    try:
        return team.add_assignment(ctx.tenant, b.manager_id, b.member_id, ctx.actor)
    except team.TeamError as e:
        _err(e)


@router.delete("/team/assignments/{manager_id}/{member_id}")
def unassign(manager_id: int, member_id: int, ctx: Ctx = Depends(require("team"))):
    try:
        return team.remove_assignment(ctx.tenant, manager_id, member_id, ctx.actor)
    except team.TeamError as e:
        _err(e)


@router.post("/team/reset-requests/{rid}/dismiss")
def dismiss_request(rid: int, b: NoteBody, ctx: Ctx = Depends(require("team"))):
    import time

    from .. import audit

    with ctx.tenant.db() as c:
        n = c.execute("UPDATE team_reset_requests SET handled=? WHERE id=? AND handled IS NULL", (time.time(), rid)).rowcount
    if not n:
        raise HTTPException(404, "Not found")
    audit.log(ctx.tenant, ctx.actor, "team.reset_request_dismissed", {"request_id": rid})
    return {"ok": True}


# --------------------------------------------------------------------------- public: join, reset, forgot
class TokenBody(_Body):
    token: str = Field(min_length=1, max_length=300)


class AcceptBody(_Body):
    token: str = Field(min_length=1, max_length=300)
    password: str = Field(min_length=1, max_length=200)
    display: str | None = Field(None, max_length=80)


class ForgotBody(_Body):
    ident: str = Field(min_length=1, max_length=200)


@public_router.post("/join/check")
def join_check(b: TokenBody, request: Request, tenant: tenancy.Tenant = Depends(get_tenant)):
    try:
        return team.check_token(tenant, b.token, client_ip(request))
    except team.TeamError as e:
        _err(e)


@public_router.post("/join/accept")
def join_accept(b: AcceptBody, request: Request, response: Response, tenant: tenancy.Tenant = Depends(get_tenant)):
    try:
        session, user = team.accept(tenant, b.token, b.password, client_ip(request), b.display)
    except team.TeamError as e:
        _err(e)
    _set_cookie(response, tenant.id, session)
    return {"ok": True, "user": user}


@public_router.post("/join/forgot")
def join_forgot(b: ForgotBody, request: Request, tenant: tenancy.Tenant = Depends(get_tenant)):
    try:
        return team.request_reset(tenant, b.ident, client_ip(request))
    except team.TeamError as e:
        _err(e)
