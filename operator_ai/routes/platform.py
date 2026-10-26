"""Platform features: threads, projects + memory (more in phase 8)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .. import threads
from ..deps import Ctx, require

router = APIRouter()


@router.get("/threads")
def list_threads(q: str | None = Query(None, max_length=200), archived: bool = False, project_id: int | None = None,
                 ctx: Ctx = Depends(require("threads"))):
    return {"items": threads.list_for(ctx.tenant, ctx.user["id"], q, archived, project_id)}


@router.get("/threads/{tid_}")
def get_thread(tid_: int, ctx: Ctx = Depends(require("threads"))):
    t = threads.get(ctx.tenant, tid_, ctx.user["id"])
    if t is None:
        raise HTTPException(404, "Not found")
    return t


class ThreadPatch(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=120)
    archived: bool | None = None


@router.patch("/threads/{tid_}")
def patch_thread(tid_: int, b: ThreadPatch, ctx: Ctx = Depends(require("threads"))):
    if not threads.update(ctx.tenant, tid_, ctx.user["id"], b.title, b.archived):
        raise HTTPException(404, "Not found")
    return {"ok": True}


@router.post("/threads/{tid_}/share")
def share_thread(tid_: int, ctx: Ctx = Depends(require("threads"))):
    tok = threads.share(ctx.tenant, tid_, ctx.user["id"])
    if tok is None:
        raise HTTPException(404, "Not found")
    return {"token": tok, "link": f"#/threads/shared/{tok}", "scope": "internal (signed-in members of this workspace)"}


@router.get("/threads/shared/{token}")
def shared_thread(token: str, ctx: Ctx = Depends(require("threads"))):
    t = threads.by_share(ctx.tenant, token)
    if t is None:
        raise HTTPException(404, "Not found")
    return t


class ProjectBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field("", max_length=2000)


@router.get("/projects")
def list_projects(ctx: Ctx = Depends(require("projects"))):
    return {"items": threads.projects(ctx.tenant, ctx.user["id"])}


@router.post("/projects")
def create_project(b: ProjectBody, ctx: Ctx = Depends(require("projects"))):
    return {"id": threads.create_project(ctx.tenant, ctx.user["id"], b.name, b.description)}


class MemoryBody(BaseModel):
    content: str = Field(min_length=1, max_length=2000)
    project_id: int | None = None


@router.get("/memories")
def list_memories(project_id: int | None = None, ctx: Ctx = Depends(require("projects"))):
    return {"items": threads.memories(ctx.tenant, ctx.user["id"], project_id)}


@router.post("/memories")
def add_memory(b: MemoryBody, ctx: Ctx = Depends(require("projects"))):
    try:
        return {"id": threads.add_memory(ctx.tenant, ctx.user["id"], b.project_id, b.content)}
    except PermissionError:
        raise HTTPException(404, "Project not found")


@router.delete("/memories/{mid}")
def delete_memory(mid: int, ctx: Ctx = Depends(require("projects"))):
    if not threads.delete_memory(ctx.tenant, ctx.user["id"], mid):
        raise HTTPException(404, "Not found")
    return {"ok": True}
