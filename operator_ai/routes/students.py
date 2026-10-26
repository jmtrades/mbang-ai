"""Students: accountability board, check-ins, onboarding, Retention Star, coach tasks."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import db
from ..agents import accountability, onboarding
from ..deps import Ctx, require

router = APIRouter()


@router.get("/students")
def students(mine: bool = False, ctx: Ctx = Depends(require("students"))):
    coach_id = ctx.user["id"] if (mine or ctx.role == "coach") else None
    return accountability.board(ctx.tenant, coach_id)


@router.get("/students/star")
def star(ctx: Ctx = Depends(require("students"))):
    return accountability.retention_star(ctx.tenant)


@router.get("/students/digest")
def digest(ctx: Ctx = Depends(require("students"))):
    return accountability.digest(ctx.tenant)


@router.post("/students/interventions")
def interventions(ctx: Ctx = Depends(require("students"))):
    return accountability.run_interventions(ctx.tenant, ctx.user)


@router.get("/students/{sid}")
def student(sid: int, ctx: Ctx = Depends(require("students"))):
    with ctx.tenant.db() as c:
        s = db.row(c.execute("SELECT * FROM students WHERE id=?", (sid,)).fetchone())
        if s is None or (ctx.role == "coach" and s["coach_id"] != ctx.user["id"]):
            raise HTTPException(404, "Not found")
        cks = db.rows(c.execute("SELECT * FROM checkins WHERE student_id=? ORDER BY at", (sid,)))
        tasks = db.rows(c.execute("SELECT * FROM coach_tasks WHERE student_id=? ORDER BY id DESC", (sid,)))
    s["intake"] = db.jload(s.get("intake"), None)
    s["plan"] = db.jload(s.get("plan"), None)
    s["risk"] = accountability.risk(s, [{"at": c["at"], "outreach": c["outreach"] or 0, "booked": c["calls_booked"] or 0,
                                         "showed": c["calls_showed"] or 0, "mood": c["mood"] or 0, "wins": c["wins"] or ""}
                                        for c in cks])
    return {"student": s, "checkins": cks, "tasks": tasks}


@router.get("/coach-tasks")
def coach_tasks(ctx: Ctx = Depends(require("students"))):
    with ctx.tenant.db() as c:
        if ctx.role == "coach":
            rows = db.rows(c.execute("SELECT t.*, s.name student FROM coach_tasks t LEFT JOIN students s ON s.id=t.student_id "
                                     "WHERE t.coach_id=? AND t.status='open' ORDER BY t.due LIMIT 300", (ctx.user["id"],)))
        else:
            rows = db.rows(c.execute("SELECT t.*, s.name student FROM coach_tasks t LEFT JOIN students s ON s.id=t.student_id "
                                     "WHERE t.status='open' ORDER BY t.due LIMIT 300"))
    return {"items": rows}


class CheckinBody(BaseModel):
    outreach: int = Field(0, ge=0, le=100000)
    calls_booked: int = Field(0, ge=0, le=10000)
    calls_showed: int = Field(0, ge=0, le=10000)
    mood: int = Field(3, ge=1, le=5)
    wins: str = Field("", max_length=500)
    blockers: str = Field("", max_length=500)


@router.post("/checkin")
def checkin(b: CheckinBody, ctx: Ctx = Depends(require("checkin"))):
    sid = ctx.user.get("student_id")
    if not sid:
        raise HTTPException(409, "Your account isn't linked to a student record yet")
    cid = accountability.submit_checkin(ctx.tenant, sid, b.model_dump())
    return {"id": cid, "ok": True}


@router.get("/checkin/mine")
def my_checkins(ctx: Ctx = Depends(require("checkin"))):
    sid = ctx.user.get("student_id")
    with ctx.tenant.db() as c:
        rows = db.rows(c.execute("SELECT * FROM checkins WHERE student_id=? ORDER BY at DESC LIMIT 20", (sid,))) if sid else []
    return {"items": rows, "student_id": sid}


class IntakeBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: str | None = Field(None, max_length=200)
    niche: str = Field("", max_length=120)
    usp: str = Field("", max_length=500)
    clients: int = Field(0, ge=0, le=10000)
    hours_per_week: int = Field(10, ge=0, le=100)
    goals: str = Field("", max_length=1000)
    skills: dict[str, Any] = Field(default_factory=dict)


@router.post("/onboarding")
def onboard(b: IntakeBody, ctx: Ctx = Depends(require("onboarding"))):
    return onboarding.onboard(ctx.tenant, b.model_dump(), ctx.user)
