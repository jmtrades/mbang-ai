"""Approval queue, runs/traces, audit log, guardrail check, settings."""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field

from .. import audit, guardrails, queue, runtime, settings
from ..deps import Ctx, get_ctx, require, require_owner

router = APIRouter()
QueueId = Annotated[int, Path(ge=1, le=2 ** 53)]  # beyond this: malformed input (422), never a DB overflow (500)


def _qerr(e: queue.QueueError):
    raise HTTPException(e.status, str(e))


@router.get("/queue")
def list_queue(status: str | None = Query(None, max_length=20), kind: str | None = Query(None, max_length=40),
               run_id: int | None = Query(None, ge=1, le=2 ** 53), chain_id: str | None = Query(None, max_length=80),
               ids: str | None = Query(None, max_length=2000), tag: str | None = Query(None, max_length=80),
               ctx: Ctx = Depends(require("queue"))):
    id_list = [int(x) for x in ids.split(",") if x.strip().isdigit() and len(x.strip()) <= 15] if ids else None
    kinds = queue.decidable_kinds(ctx.role)
    own = None if ctx.role == "owner" else ctx.user["id"]
    items = queue.list_items(ctx.tenant, status=status, kind=kind, run_id=run_id, chain_id=chain_id, ids=id_list,
                             kinds=kinds, tag=tag, own_user_id=own)
    # counts cover only what this person may see (never other people's drafts)
    return {"items": items, "counts": queue.counts(ctx.tenant, kinds, own)}


@router.get("/queue/{qid}")
def get_item(qid: QueueId, ctx: Ctx = Depends(require("queue"))):
    item = queue.get(ctx.tenant, qid)
    if not queue.visible(ctx.role, item, ctx.user["id"]):
        raise HTTPException(404, "Not found")
    return item


class EditBody(BaseModel):
    body: str = Field(min_length=1, max_length=20000)


@router.put("/queue/{qid}")
def edit_item(qid: QueueId, b: EditBody, ctx: Ctx = Depends(require("queue"))):
    try:
        return queue.edit(ctx.tenant, qid, b.body, ctx.actor, ctx.role, user_id=ctx.user["id"])
    except queue.QueueError as e:
        _qerr(e)


@router.post("/queue/{qid}/approve")
def approve(qid: QueueId, ctx: Ctx = Depends(require("queue"))):
    try:
        return queue.approve(ctx.tenant, qid, ctx.actor, ctx.role, user_id=ctx.user["id"])
    except queue.QueueError as e:
        _qerr(e)


class RejectBody(BaseModel):
    reason: str = Field("", max_length=500)
    # why it was rejected (feeds the learning loop); omitted -> "other"
    reason_code: str | None = Field(None, pattern="^(" + "|".join(queue.REJECT_CODES) + ")$")


@router.post("/queue/{qid}/reject")
def reject(qid: QueueId, b: RejectBody | None = None, ctx: Ctx = Depends(require("queue"))):
    try:
        return queue.reject(ctx.tenant, qid, ctx.actor, ctx.role, (b.reason if b else ""), user_id=ctx.user["id"],
                            reason_code=b.reason_code if b else None)
    except queue.QueueError as e:
        _qerr(e)


@router.get("/runs")
def runs(agent: str | None = Query(None, max_length=40), limit: int = Query(50, ge=1, le=500),
         ctx: Ctx = Depends(get_ctx)):
    # owners and coaches see every run; other roles see runs they started
    rows = runtime.recent(ctx.tenant, limit, agent)
    if ctx.role not in ("owner", "coach"):
        rows = [r for r in rows if (r.get("actor") or "").startswith(ctx.user["username"] + "(")]
    return {"runs": rows}


@router.get("/runs/{run_id}")
def run(run_id: Annotated[int, Path(ge=1, le=2 ** 53)], ctx: Ctx = Depends(get_ctx)):
    r = runtime.get(ctx.tenant, run_id)
    if r is None:
        raise HTTPException(404, "Not found")
    if ctx.role not in ("owner", "coach") and not (r.get("actor") or "").startswith(ctx.user["username"] + "("):
        raise HTTPException(404, "Not found")
    return r


@router.get("/audit")
def audit_log(kind: str | None = Query(None, max_length=40), limit: int = Query(200, ge=1, le=2000),
              ctx: Ctx = Depends(require("audit"))):
    return {"items": audit.recent(ctx.tenant, limit, kind)}


class CheckBody(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    mode: str = Field("plain", pattern="^(outreach|client_asset|plain)$")
    meta: dict[str, Any] = Field(default_factory=dict)


@router.post("/guardrails/check")
def check(b: CheckBody, ctx: Ctx = Depends(get_ctx)):
    flags = guardrails.check(b.text, b.mode, b.meta, guardrails.tenant_banned_names(ctx.tenant), tenant=ctx.tenant)
    return {"flags": flags, "blocked": guardrails.blocked(flags), "needs_review": guardrails.needs_review(flags)}


@router.get("/settings")
def get_settings(ctx: Ctx = Depends(require("settings"))):
    view = settings.public_view(ctx.tenant)
    if ctx.role != "owner":
        view = {"brand": view.get("brand")}
    return view


class SettingBody(BaseModel):
    key: str = Field(min_length=1, max_length=60)
    value: Any = None


@router.put("/settings")
def put_setting(b: SettingBody, ctx: Ctx = Depends(require_owner)):
    try:
        settings.update_from_api(ctx.tenant, b.key, b.value, ctx.actor)
    except settings.BoxOnly as e:
        raise HTTPException(403, str(e))
    except KeyError:
        raise HTTPException(422, f"Unknown setting {b.key}")
    return {"ok": True}
