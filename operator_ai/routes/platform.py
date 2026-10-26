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


# ---------------------------------------------------------------- uploads
from fastapi import File, Form, UploadFile  # noqa: E402

from .. import call_review, channels, crm, skills, uploads  # noqa: E402


@router.post("/uploads")
async def upload(file: UploadFile = File(...), project_id: int | None = Form(None), to_kb: bool = Form(False),
                 ctx: Ctx = Depends(require("projects"))):
    data = await file.read(uploads.MAX_BYTES + 1)
    allow_kb = to_kb and ctx.role == "owner" and ctx.tenant.kb_scope in ("own", "course")
    try:
        return uploads.save(ctx.tenant, ctx.user["id"], file.filename or "upload", data, project_id, to_kb=allow_kb)
    except uploads.UploadError as e:
        raise HTTPException(422, str(e))


@router.get("/uploads")
def list_uploads(project_id: int | None = None, ctx: Ctx = Depends(require("projects"))):
    return {"items": uploads.list_for(ctx.tenant, ctx.user["id"], project_id), "ocr": uploads.ocr_backend() or "none"}


@router.delete("/uploads/{uid}")
def delete_upload(uid: int, ctx: Ctx = Depends(require("projects"))):
    if not uploads.delete(ctx.tenant, ctx.user["id"], uid):
        raise HTTPException(404, "Not found")
    return {"ok": True}


# ---------------------------------------------------------------- business profile
@router.get("/profile")
def get_profile(ctx: Ctx = Depends(require("profile"))):
    return {"profile": skills.get_profile(ctx.tenant), "sections": skills.PROFILE_SECTIONS}


class ProfileBody(BaseModel):
    profile: dict[str, dict[str, str]]


@router.put("/profile")
def put_profile(b: ProfileBody, ctx: Ctx = Depends(require("profile"))):
    return {"profile": skills.put_profile(ctx.tenant, b.profile, ctx.actor)}


# ---------------------------------------------------------------- skills + workers
@router.get("/skills")
def list_skills(ctx: Ctx = Depends(require("skills"))):
    from .. import vault

    out = []
    for s in skills.SKILLS.values():
        st = vault.status(ctx.tenant, s.needs) if s.needs else {}
        out.append({"key": s.key, "name": s.name, "description": s.description, "needs": s.needs,
                    "missing": [k for k, v in st.items() if v != "set"]})
    return {"skills": out, "workers": skills.list_workers(ctx.tenant), "triggers": skills.TRIGGERS}


class SkillRunBody(BaseModel):
    inputs: dict[str, str] = Field(default_factory=dict)


@router.post("/skills/{key}/run")
def run_skill(key: str, b: SkillRunBody, ctx: Ctx = Depends(require("skills"))):
    try:
        return skills.run_skill(ctx.tenant, key, b.inputs, ctx.user)
    except KeyError:
        raise HTTPException(404, "Skill not found")


class WorkerBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    skill: str = Field(min_length=1, max_length=40)
    trigger: str = Field(pattern="^(daily|weekly|on_source_change|manual)$")
    schedule: str = Field("09:00", pattern=r"^\d{1,2}:\d{2}$")
    inputs: dict[str, str] = Field(default_factory=dict)


@router.post("/workers")
def add_worker(b: WorkerBody, ctx: Ctx = Depends(require("skills"))):
    try:
        return {"id": skills.add_worker(ctx.tenant, b.name, b.skill, b.trigger, b.inputs, b.schedule)}
    except KeyError:
        raise HTTPException(404, "Skill not found")


@router.post("/workers/{wid}/run")
def run_worker(wid: int, ctx: Ctx = Depends(require("skills"))):
    try:
        return skills.run_worker(ctx.tenant, wid)
    except KeyError:
        raise HTTPException(404, "Worker not found")


# ---------------------------------------------------------------- call review + drills
class CallBody(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    rep: str = Field(min_length=1, max_length=80)
    parties: list[str] = Field(min_length=1, max_length=10)
    transcript: str = Field(min_length=10, max_length=200000)


@router.post("/calls")
def add_call(b: CallBody, ctx: Ctx = Depends(require("calls"))):
    return {"id": call_review.create_call(ctx.tenant, b.title, b.rep, b.parties, b.transcript)}


@router.get("/calls")
def list_calls(ctx: Ctx = Depends(require("calls"))):
    from .. import db as _db

    with ctx.tenant.db() as c:
        rows = _db.rows(c.execute("SELECT id,title,rep,parties,status,review,created FROM calls ORDER BY id DESC"))
    for r in rows:
        r["parties"] = _db.jload(r["parties"], [])
        r["review"] = _db.jload(r["review"], None)
        r["consent"] = call_review.consent_status(ctx.tenant, r["id"])
    return {"items": rows, "trends": call_review.rep_trends(ctx.tenant)}


class ConsentBody(BaseModel):
    party: str = Field(min_length=1, max_length=80)
    method: str = Field("verbal on recording", max_length=80)


@router.post("/calls/{cid}/consent")
def consent(cid: int, b: ConsentBody, ctx: Ctx = Depends(require("calls"))):
    try:
        return call_review.record_consent(ctx.tenant, cid, b.party, b.method, ctx.actor)
    except KeyError:
        raise HTTPException(404, "Call not found")
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.post("/calls/{cid}/review")
def review_call(cid: int, ctx: Ctx = Depends(require("calls"))):
    try:
        return call_review.review(ctx.tenant, cid, ctx.user)
    except call_review.ConsentMissing as e:
        raise HTTPException(409, str(e))
    except KeyError:
        raise HTTPException(404, "Call not found")


@router.get("/drills")
def drills(ctx: Ctx = Depends(require("drills"))):
    return {"cards": call_review.due_cards(ctx.tenant, ctx.user["id"])}


class GradeBody(BaseModel):
    grade: int = Field(ge=0, le=5)


@router.post("/drills/{card_id}/grade")
def grade(card_id: int, b: GradeBody, ctx: Ctx = Depends(require("drills"))):
    try:
        return call_review.grade(ctx.tenant, ctx.user["id"], card_id, b.grade)
    except KeyError:
        raise HTTPException(404, "Card not found")


# ---------------------------------------------------------------- voice + voice-note deck
class VoiceStart(BaseModel):
    consent: bool


@router.post("/voice/start")
def voice_start(b: VoiceStart, ctx: Ctx = Depends(require("coach"))):
    try:
        return channels.voice_start(ctx.tenant, ctx.user["id"], b.consent)
    except channels.ChannelError as e:
        raise HTTPException(e.status, str(e))


class VoiceChunk(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    seconds: float = Field(gt=0, le=600)


@router.post("/voice/{sid}/chunk")
def voice_chunk(sid: int, b: VoiceChunk, ctx: Ctx = Depends(require("coach"))):
    try:
        return channels.voice_chunk(ctx.tenant, ctx.user["id"], sid, b.text, b.seconds)
    except channels.ChannelError as e:
        raise HTTPException(e.status, str(e))


@router.post("/voice/{sid}/end")
def voice_end(sid: int, ctx: Ctx = Depends(require("coach"))):
    try:
        return channels.voice_end(ctx.tenant, ctx.user["id"], sid)
    except channels.ChannelError as e:
        raise HTTPException(e.status, str(e))


class DeckBody(BaseModel):
    transcript: str = Field(min_length=10, max_length=100000)


@router.post("/voice-note/deck")
def deck(b: DeckBody, ctx: Ctx = Depends(require("coach"))):
    return channels.voice_note_deck(ctx.tenant, b.transcript)


@router.post("/channels/link-code")
def link(ctx: Ctx = Depends(require("settings"))):
    return {"code": channels.link_code(ctx.tenant, ctx.user["id"]), "how": "Send /link CODE to the bot"}


# ---------------------------------------------------------------- CRM (mock)
@router.get("/crm")
def crm_list(ctx: Ctx = Depends(require("crm"))):
    return {"contacts": crm.contacts(ctx.tenant), "stages": crm.STAGES, "terminal": sorted(crm.TERMINAL)}


class CrmContact(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: str | None = Field(None, max_length=200)


@router.post("/crm/contacts")
def crm_add(b: CrmContact, ctx: Ctx = Depends(require("crm"))):
    name = b.name if not ctx.tenant.is_demo or b.name.endswith("(DEMO)") else f"{b.name} (DEMO)"
    return {"id": crm.add_contact(ctx.tenant, name, b.email)}


class CrmMove(BaseModel):
    stage: str = Field(pattern="^(new|contacted|booked|showed|closed_won|closed_lost)$")


@router.post("/crm/contacts/{cid}/move")
def crm_move(cid: int, b: CrmMove, ctx: Ctx = Depends(require("crm"))):
    try:
        return crm.move(ctx.tenant, cid, b.stage, ctx.actor)
    except KeyError:
        raise HTTPException(404, "Contact not found")
