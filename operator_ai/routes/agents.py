"""Student Coach, Work Reviewer, Coaching mode, Diagnostic, evals."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .. import db, diagnostic, evals, threads
from ..agents import coach, coaching, reviewer
from ..deps import Ctx, require, require_owner

router = APIRouter()


class AskBody(BaseModel):
    question: str = Field(min_length=2, max_length=2000)
    thread_id: int | None = None


@router.post("/coach/ask")
def ask(b: AskBody, ctx: Ctx = Depends(require("coach"))):
    if b.thread_id and threads.get(ctx.tenant, b.thread_id, ctx.user["id"]) is None:
        raise HTTPException(404, "Thread not found")
    if ctx.role == "student":
        from .. import console

        limit = console.personality(ctx.tenant)["daily_question_limit"]
        with ctx.tenant.db() as c:
            n = c.execute("SELECT COUNT(*) FROM queries WHERE user_id=? AND created>?",
                          (ctx.user["id"], db.now() - 86400)).fetchone()[0]
        if n >= limit:
            raise HTTPException(429, f"Daily question limit of {limit} reached. It resets in 24 hours.")
    return coach.answer(ctx.tenant, b.question, user=ctx.user, record=True, thread=True, thread_id=b.thread_id)


class ReviewBody(BaseModel):
    text: str = Field(min_length=2, max_length=20000)
    asset_type: str | None = Field(None, max_length=40)


@router.post("/review")
def review(b: ReviewBody, ctx: Ctx = Depends(require("review"))):
    return reviewer.review(ctx.tenant, b.text, b.asset_type, user=ctx.user)


@router.get("/review/types")
def review_types(ctx: Ctx = Depends(require("review"))):
    return {"types": reviewer.TYPES}


class CoachingBody(BaseModel):
    text: str = Field(min_length=2, max_length=10000)
    kind: str = Field(pattern="^(dm|followup|cold_video_pitch|bio|caption|script|reminder|vsl|email|ad)$")
    lead_tier: str | None = Field(None, pattern="^(hot|warm|cold|unqualified)$")


@router.post("/coaching")
def coaching_route(b: CoachingBody, ctx: Ctx = Depends(require("coaching"))):
    return coaching.coach_work(ctx.tenant, b.text, b.kind, user=ctx.user, lead_tier=b.lead_tier)


class DiagnoseBody(BaseModel):
    metrics: dict[str, float] = Field(default_factory=dict)
    label: str = Field("", max_length=120)
    client_id: int | None = None


@router.post("/diagnose")
def diagnose(b: DiagnoseBody, ctx: Ctx = Depends(require("diagnostic"))):
    metrics = dict(b.metrics)
    label = b.label
    if b.client_id:
        with ctx.tenant.db() as c:
            r = c.execute("SELECT name, metrics, price FROM clients WHERE id=?", (b.client_id,)).fetchone()
        if r is None:
            raise HTTPException(404, "Client not found")
        metrics = {**db.jload(r["metrics"], {}), "offer_price": r["price"], **metrics}
        label = label or r["name"]
    return diagnostic.diagnose(ctx.tenant, metrics, user=ctx.user, label=label)


@router.get("/diagnose/stages")
def stages(ctx: Ctx = Depends(require("diagnostic"))):
    return {"stages": [{"key": s.key, "label": s.label, "num": s.num, "den": s.den, "benchmark": s.benchmark,
                        "kind": s.kind} for s in diagnostic.STAGES]}


@router.get("/evals")
def eval_history(ctx: Ctx = Depends(require("evals"))):
    return {"runs": evals.history(ctx.tenant)}


class EvalBody(BaseModel):
    kind: str = Field(pattern="^(retrieval|honesty|reviewer)$")


@router.post("/evals/run")
def eval_run(b: EvalBody, ctx: Ctx = Depends(require_owner)):
    fn: Any = {"retrieval": evals.retrieval_eval, "honesty": evals.honesty_eval, "reviewer": evals.reviewer_eval}[b.kind]
    return fn(ctx.tenant)


@router.get("/queries")
def queries(unanswered: bool = Query(False), ctx: Ctx = Depends(require("console"))):
    with ctx.tenant.db() as c:
        if unanswered:
            rows = db.rows(c.execute("SELECT * FROM queries WHERE covered='no' ORDER BY id DESC LIMIT 200"))
        else:
            rows = db.rows(c.execute("SELECT * FROM queries ORDER BY id DESC LIMIT 200"))
    return {"items": rows}
