"""Missions API: goal-driven, multi-step plans that do the work and wait for approval.

Owner sees every mission; everyone else sees their own (a coach may also view their students' missions, read-only).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .. import db
from ..deps import Ctx, require
from ..missions import ai_planner, engine, evaluator, registry, templates

router = APIRouter()
TAB = "missions"


def _err(e: Exception):
    if isinstance(e, engine.MissionError):
        raise HTTPException(e.status, str(e))
    if isinstance(e, templates.AnswerError):
        raise HTTPException(422, str(e))
    raise e


def _load(ctx: Ctx, mid: int, act: bool = False) -> dict:
    m = engine.get(ctx.tenant, mid)
    if m is None or not engine.can_view(ctx.tenant, ctx.user, m):
        raise HTTPException(404, "Mission not found")
    if act and not engine.can_act(ctx.user, m):
        raise HTTPException(403, "Only the person running this mission (or the owner) can change it")
    return m


def _clients_for(ctx: Ctx) -> list[dict]:
    with ctx.tenant.db() as c:
        if ctx.role == "owner":
            rows = db.rows(c.execute("SELECT id, name FROM clients ORDER BY id DESC LIMIT 200"))
        elif ctx.user.get("student_id"):
            rows = db.rows(c.execute("SELECT id, name FROM clients WHERE student_id=? ORDER BY id DESC LIMIT 200",
                                     (ctx.user["student_id"],)))
        else:
            rows = []
    return rows


@router.get("/missions")
def list_missions(scope: str = Query("mine", pattern="^(mine|all|students)$"), ctx: Ctx = Depends(require(TAB))):
    if scope == "all" and ctx.role != "owner":
        raise HTTPException(403, "Only the owner can see every mission")
    if scope == "students" and ctx.role not in ("owner", "coach"):
        raise HTTPException(403, "Only coaches and the owner can see students' missions")
    return {"missions": engine.list_for(ctx.tenant, ctx.user, scope), "scope": scope,
            "scopes": ["mine"] + (["all", "students"] if ctx.role == "owner" else ["students"] if ctx.role == "coach" else []),
            "ai": ai_planner.availability(ctx.tenant)}


@router.get("/missions/templates")
def list_templates(ctx: Ctx = Depends(require(TAB))):
    return {"templates": [t.public() for t in templates.for_role(ctx.role)], "clients": _clients_for(ctx),
            "ai": ai_planner.availability(ctx.tenant),
            "note": "Plans come from fixed templates and work with AI off. Nothing goes out without approval."}


@router.get("/missions/next-actions")
def next_actions(ctx: Ctx = Depends(require(TAB))):
    return {"items": engine.next_actions(ctx.tenant, ctx.user)}


class CreateBody(BaseModel):
    goal: str = Field(min_length=1, max_length=300)
    template: str | None = Field(None, max_length=40, pattern="^[a-z_0-9]+$")
    answers: dict[str, Any] = Field(default_factory=dict, max_length=40)


@router.post("/missions")
def create(b: CreateBody, ctx: Ctx = Depends(require(TAB))):
    if b.template and b.template != templates.CUSTOM and templates.get(b.template) is None:
        raise HTTPException(422, "Unknown mission template")
    try:
        m = engine.create(ctx.tenant, ctx.user, b.goal, b.template, b.answers)
    except (engine.MissionError, templates.AnswerError) as e:
        _err(e)
    return engine.detail(ctx.tenant, m, ctx.user)


@router.get("/missions/{mid}")
def get_mission(mid: int, ctx: Ctx = Depends(require(TAB))):
    m = _load(ctx, mid)
    if engine.can_act(ctx.user, m) and m["status"] == engine.ACTIVE:
        engine.sync(ctx.tenant, mid)
        engine.pump(ctx.tenant, mid)
        m = engine.get(ctx.tenant, mid)
    return engine.detail(ctx.tenant, m, ctx.user)


@router.get("/missions/{mid}/events")
def mission_events(mid: int, limit: int = Query(200, ge=1, le=1000), ctx: Ctx = Depends(require(TAB))):
    _load(ctx, mid)
    return {"events": engine._public_events(registry.events_of(ctx.tenant, mid)[-limit:])}


def _status(ctx: Ctx, mid: int, action: str):
    _load(ctx, mid, act=True)
    try:
        return engine.detail(ctx.tenant, engine.set_status(ctx.tenant, mid, action, ctx.actor), ctx.user)
    except engine.MissionError as e:
        _err(e)


@router.post("/missions/{mid}/pause")
def pause(mid: int, ctx: Ctx = Depends(require(TAB))):
    return _status(ctx, mid, "pause")


@router.post("/missions/{mid}/resume")
def resume(mid: int, ctx: Ctx = Depends(require(TAB))):
    return _status(ctx, mid, "resume")


@router.post("/missions/{mid}/cancel")
def cancel(mid: int, ctx: Ctx = Depends(require(TAB))):
    return _status(ctx, mid, "cancel")


class AnswersBody(BaseModel):
    answers: dict[str, Any] = Field(default_factory=dict, max_length=40)


@router.post("/missions/{mid}/answers")
def answers(mid: int, b: AnswersBody, ctx: Ctx = Depends(require(TAB))):
    _load(ctx, mid, act=True)
    try:
        return engine.detail(ctx.tenant, engine.update_answers(ctx.tenant, mid, b.answers, ctx.actor), ctx.user)
    except (engine.MissionError, templates.AnswerError) as e:
        _err(e)


def _step_action(ctx: Ctx, mid: int, sid: int, fn, **kw):
    _load(ctx, mid, act=True)
    try:
        return engine.detail(ctx.tenant, fn(ctx.tenant, mid, sid, ctx.actor, **kw), ctx.user)
    except (engine.MissionError, templates.AnswerError) as e:
        _err(e)


@router.post("/missions/{mid}/steps/{sid}/retry")
def retry(mid: int, sid: int, ctx: Ctx = Depends(require(TAB))):
    return _step_action(ctx, mid, sid, engine.retry_step)


@router.post("/missions/{mid}/steps/{sid}/skip")
def skip(mid: int, sid: int, ctx: Ctx = Depends(require(TAB))):
    return _step_action(ctx, mid, sid, engine.skip_step)


class MarkDoneBody(BaseModel):
    outcome: str | None = Field(None, max_length=30, pattern="^[a-z_]+$")
    note: str = Field("", max_length=500)
    value: float | None = Field(None, ge=0, le=100_000_000, allow_inf_nan=False)
    numbers: dict[str, float] | None = Field(None, max_length=20)


@router.post("/missions/{mid}/steps/{sid}/mark-done")
def mark_done(mid: int, sid: int, b: MarkDoneBody | None = None, ctx: Ctx = Depends(require(TAB))):
    b = b or MarkDoneBody()
    return _step_action(ctx, mid, sid, engine.mark_done, outcome=b.outcome, note=b.note, value=b.value, numbers=b.numbers)


class ReplanBody(BaseModel):
    source: str = Field("rules", pattern="^(rules|ai)$")


@router.post("/missions/{mid}/replan")
def replan(mid: int, b: ReplanBody | None = None, ctx: Ctx = Depends(require(TAB))):
    """Suggest plan changes as a diff the user confirms. Rules first; AI only when asked and the budget allows."""
    m = _load(ctx, mid, act=True)
    if m["status"] in ("done", "cancelled"):
        raise HTTPException(409, f"The mission is {m['status']}")
    src = (b or ReplanBody()).source
    if src == "ai":
        res = ai_planner.propose(ctx.tenant, m, ctx.actor)
    else:
        ev = evaluator.evaluate(ctx.tenant, m, actor=ctx.actor)
        res = {"findings": ev["findings"], "replan_ids": ev["replan_ids"],
               "label": "Rules check done" + ("" if ev["findings"] else ": on track, nothing to change")}
    with ctx.tenant.db() as c:
        pending = db.rows(c.execute("SELECT * FROM mission_replans WHERE mission_id=? AND status='proposed' ORDER BY id",
                                    (mid,)))
    for r in pending:
        r["diff"] = db.jload(r["diff"], {})
    return {**res, "pending": pending}


class ConfirmBody(BaseModel):
    replan_id: int = Field(ge=1)
    accept: bool = True


@router.post("/missions/{mid}/replan/confirm")
def confirm(mid: int, b: ConfirmBody, ctx: Ctx = Depends(require(TAB))):
    _load(ctx, mid, act=True)
    try:
        m = evaluator.apply_replan(ctx.tenant, mid, b.replan_id, ctx.actor, b.accept)
    except engine.MissionError as e:
        _err(e)
    return engine.detail(ctx.tenant, m, ctx.user)
