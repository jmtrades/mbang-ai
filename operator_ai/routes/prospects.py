"""API for the first-client engine: ICP, lists, import, prospects, research, drafts, replies, booking, proposals
and stats. Every route needs the "prospects" tab; the owner sees everyone's rows, everyone else only their own.
The public proposal page lives on public_router."""
from __future__ import annotations

import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from .. import auth, db, llm, public_demo, queue, tenancy
from .. import proposals as PR
from .. import prospects as P
from ..deps import COOKIE, Ctx, client_ip, require

router = APIRouter()
public_router = APIRouter()
STAGE_RE = "^(" + "|".join(P.STAGES) + ")$"
CHANNEL_RE = "^(instagram_dm|email)$"
MAX_ID = 9_007_199_254_740_991  # ids beyond this are malformed input (422), never a database overflow (500)
Id = Annotated[int, Path(ge=1, le=MAX_ID)]


def _guard(fn):
    try:
        return fn()
    except (P.ProspectError, PR.ProposalError) as e:
        raise HTTPException(e.status, str(e))


# --------------------------------------------------------------------------- overview, ICP, config
@router.get("/prospects")
def overview(list_name: str | None = Query(None, max_length=80), stage: str | None = Query(None, pattern=STAGE_RE),
             min_fit: int | None = Query(None, ge=0, le=100), q: str | None = Query(None, max_length=80),
             owner_id: int | None = Query(None, ge=1, le=MAX_ID), ctx: Ctx = Depends(require("prospects"))):
    t, u = ctx.tenant, ctx.user
    icp = P.get_icp(t, u["id"])
    return {
        "prospects": P.list_prospects(t, u, list_name, stage, min_fit, q, owner_id),
        "stages": [{"key": s, "label": P.STAGE_LABELS[s]} for s in P.STAGES],
        "lists": P.lists(t, u), "usage": P.usage(t, u["id"]), "caps": P.caps(t),
        "icp_ready": bool(icp.get("niche")), "sender_ready": bool(icp["sender"].get("postal_address")),
        "moves": [{"key": m, "label": P.MOVE_LABELS[m]} for m in P.MOVES],
        "channels": [{"key": k, "label": v} for k, v in P.CHANNEL_LABELS.items()],
        "lawful_bases": [{"key": k, "label": v} for k, v in P.LAWFUL_BASES.items()],
        "reply_kinds": [{"key": k, "label": v} for k, v in P.REPLY_KINDS.items()],
        "is_owner": ctx.role == "owner", "web_research": P.web_research_enabled(t),
        "ai": llm.state(t)["label"],
    }


class SenderBody(BaseModel):
    name: str = Field("", max_length=120)
    business: str = Field("", max_length=160)
    postal_address: str = Field("", max_length=300)
    reply_email: str = Field("", max_length=200)


class ICPBody(BaseModel):
    niche: str = Field("", max_length=120)
    offer: str = Field("", max_length=200)
    price_min: float | None = Field(None, ge=0, le=1e9)
    price_max: float | None = Field(None, ge=0, le=1e9)
    audience_min: float | None = Field(None, ge=0, le=1e10)
    audience_max: float | None = Field(None, ge=0, le=1e10)
    must_haves: list[str] = Field(default_factory=list, max_length=20)
    disqualifiers: list[str] = Field(default_factory=list, max_length=20)
    signals: list[str] = Field(default_factory=list, max_length=20)
    persona: str = Field("", max_length=600)
    sender: SenderBody = Field(default_factory=SenderBody)


@router.get("/prospects/icp")
def get_icp(ctx: Ctx = Depends(require("prospects"))):
    return {"icp": P.get_icp(ctx.tenant, ctx.user["id"])}


@router.put("/prospects/icp")
def put_icp(b: ICPBody, ctx: Ctx = Depends(require("prospects"))):
    return {"icp": _guard(lambda: P.save_icp(ctx.tenant, ctx.user, b.model_dump()))}


class ConfigBody(BaseModel):
    weights: dict[str, int] | None = None
    caps: dict[str, int] | None = None
    min_sample: int | None = Field(None, ge=1, le=1000)


@router.get("/prospects/config")
def get_config(ctx: Ctx = Depends(require("prospects"))):
    return P.config_view(ctx.tenant)


@router.put("/prospects/config")
def put_config(b: ConfigBody, ctx: Ctx = Depends(require("prospects"))):
    if ctx.role != "owner":
        raise HTTPException(403, "Only the owner can change scoring weights and caps")
    for d, lo, hi in ((b.weights, 0, 100), (b.caps, 0, 500)):
        if d is not None and any(not (lo <= v <= hi) for v in d.values()):
            raise HTTPException(422, f"Values must be between {lo} and {hi}")
    return P.save_config(ctx.tenant, ctx.actor, b.weights, b.caps, b.min_sample)


@router.get("/prospects/lists")
def get_lists(ctx: Ctx = Depends(require("prospects"))):
    return {"lists": P.lists(ctx.tenant, ctx.user)}


@router.get("/prospects/imports")
def get_imports(ctx: Ctx = Depends(require("prospects"))):
    return {"imports": P.imports(ctx.tenant, ctx.user)}


class ImportBody(BaseModel):
    csv: str = Field(min_length=1, max_length=3_000_000)
    filename: str = Field("upload.csv", max_length=200)
    list_name: str = Field("", max_length=80)
    lawful_basis: str = Field(min_length=1, max_length=40)
    lawful_note: str = Field("", max_length=500)


@router.post("/prospects/import")
def import_csv(b: ImportBody, ctx: Ctx = Depends(require("prospects"))):
    return _guard(lambda: P.import_csv(ctx.tenant, ctx.user, b.csv, b.filename, b.list_name, b.lawful_basis, b.lawful_note))


class BulkBody(BaseModel):
    channel: str = Field("instagram_dm", pattern=CHANNEL_RE)
    ids: list[Annotated[int, Field(ge=1, le=MAX_ID)]] | None = Field(None, max_length=500)
    list_name: str | None = Field(None, max_length=80)
    limit: int = Field(20, ge=1, le=100)


@router.post("/prospects/draft-openers")
def draft_openers(b: BulkBody, ctx: Ctx = Depends(require("prospects"))):
    return _guard(lambda: P.draft_openers_bulk(ctx.tenant, ctx.user, b.channel, b.ids, b.list_name, b.limit))


@router.get("/prospects/stats")
def stats(ctx: Ctx = Depends(require("prospects"))):
    return P.stats(ctx.tenant, ctx.user)


# --------------------------------------------------------------------------- proposals
class PricingBody(BaseModel):
    currency: str = Field("USD", pattern="^(USD|GBP|EUR|CAD|AUD)$")
    retainer: float = Field(gt=0, le=1_000_000)
    performance_pct: float | None = Field(None, ge=0, le=50)
    pilot_fee: float | None = Field(None, gt=0, le=1_000_000)
    pilot_days: int | None = Field(None, ge=7, le=120)
    term_months: int | None = Field(None, ge=1, le=24)
    options: list[str] | None = Field(None, max_length=3)


class BuildBody(BaseModel):
    prospect_id: int = Field(ge=1, le=MAX_ID)
    call_notes: str = Field("", max_length=6000)
    numbers: dict[str, float | None] = Field(default_factory=dict, max_length=20)
    pricing: PricingBody
    title: str | None = Field(None, max_length=160)
    include_projection: bool = True


@router.get("/prospects/proposals")
def list_proposals(prospect_id: int | None = Query(None, ge=1, le=MAX_ID), ctx: Ctx = Depends(require("prospects"))):
    return {"proposals": PR.list_for(ctx.tenant, ctx.user, prospect_id), "number_fields": PR.NUMBER_KEYS,
            "currencies": list(PR.CURRENCIES)}


@router.post("/prospects/proposals")
def build_proposal(b: BuildBody, ctx: Ctx = Depends(require("prospects"))):
    return _guard(lambda: PR.build(ctx.tenant, ctx.user, b.prospect_id, b.call_notes, b.numbers,
                                   b.pricing.model_dump(exclude_none=True), b.title, b.include_projection))


@router.get("/prospects/proposals/{prop_id}")
def get_proposal(prop_id: Id, ctx: Ctx = Depends(require("prospects"))):
    return {"proposal": _guard(lambda: PR.view(ctx.tenant, PR.visible(ctx.tenant, ctx.user, prop_id)))}


class SectionEdit(BaseModel):
    key: str = Field(min_length=1, max_length=40)
    title: str | None = Field(None, max_length=120)
    text: str = Field(min_length=1, max_length=6000)


class EditProposalBody(BaseModel):
    sections: list[SectionEdit] | None = Field(None, max_length=20)
    pricing: PricingBody | None = None
    title: str | None = Field(None, max_length=160)


@router.put("/prospects/proposals/{prop_id}")
def edit_proposal(prop_id: Id, b: EditProposalBody, ctx: Ctx = Depends(require("prospects"))):
    secs = [s.model_dump() for s in b.sections] if b.sections is not None else None
    pricing = b.pricing.model_dump(exclude_none=True) if b.pricing is not None else None
    return {"proposal": _guard(lambda: PR.update(ctx.tenant, ctx.user, prop_id, secs, pricing, b.title))}


class SendBody(BaseModel):
    channel: str = Field("email", pattern=CHANNEL_RE)
    message: str | None = Field(None, max_length=1500)


@router.post("/prospects/proposals/{prop_id}/send")
def send_proposal(prop_id: Id, b: SendBody, ctx: Ctx = Depends(require("prospects"))):
    return _guard(lambda: PR.send(ctx.tenant, ctx.user, prop_id, b.channel, b.message))


@router.post("/prospects/proposals/{prop_id}/withdraw")
def withdraw_proposal(prop_id: Id, ctx: Ctx = Depends(require("prospects"))):
    return {"proposal": _guard(lambda: PR.withdraw(ctx.tenant, ctx.user, prop_id))}


# --------------------------------------------------------------------------- one prospect
class AddBody(BaseModel):
    name: str | None = Field(None, max_length=160)
    business: str | None = Field(None, max_length=160)
    handle: str | None = Field(None, max_length=200)
    email: str | None = Field(None, max_length=200)
    website: str | None = Field(None, max_length=300)
    niche: str | None = Field(None, max_length=160)
    followers: int | str | None = None
    offer: str | None = Field(None, max_length=300)
    notes: str | None = Field(None, max_length=2000)
    ad_activity: str | None = Field(None, max_length=200)
    list_name: str = Field("", max_length=80)
    lawful_basis: str = Field(min_length=1, max_length=40)
    lawful_note: str = Field("", max_length=500)


@router.post("/prospects")
def add(b: AddBody, ctx: Ctx = Depends(require("prospects"))):
    fields = b.model_dump(exclude={"lawful_basis", "lawful_note", "list_name"})
    return _guard(lambda: P.add_prospect(ctx.tenant, ctx.user, fields, b.lawful_basis, b.lawful_note, b.list_name))


def _drafts(tenant, pid: int) -> list[dict]:
    with tenant.db() as c:
        ids = [r["id"] for r in c.execute("SELECT id FROM queue WHERE json_extract(meta,'$.prospect_id')=? AND "
                                          "json_extract(meta,'$.feature')=? ORDER BY id DESC LIMIT 50", (pid, P.FEATURE))]
    out = []
    for qid in ids:
        it = queue.get(tenant, qid)
        if it is None:
            continue
        m = it["meta"]
        out.append({"id": it["id"], "kind": it["kind"], "channel": it["channel"], "title": it["title"], "status": it["status"],
                    "body": it["body_display"], "flags": it["flags"], "touch": m.get("touch"), "move": m.get("move"),
                    "send_after": m.get("send_after"), "subject": m.get("subject"), "created": it["created"]})
    return out


@router.get("/prospects/{pid}")
def detail(pid: Id, ctx: Ctx = Depends(require("prospects"))):
    p = _guard(lambda: P.visible(ctx.tenant, ctx.user, pid))
    names = P._owner_names(ctx.tenant) if ctx.role == "owner" else None
    return {"prospect": P.public(p, names), "timeline": P.timeline(ctx.tenant, pid), "drafts": _drafts(ctx.tenant, pid),
            "proposals": PR.list_for(ctx.tenant, ctx.user, pid), "usage": P.usage(ctx.tenant, p["owner_user_id"]),
            "icp": P.get_icp(ctx.tenant, p["owner_user_id"]) if p["owner_user_id"] == ctx.user["id"] else None}


class UpdateBody(BaseModel):
    name: str | None = Field(None, max_length=160)
    business: str | None = Field(None, max_length=160)
    handle: str | None = Field(None, max_length=200)
    email: str | None = Field(None, max_length=200)
    website: str | None = Field(None, max_length=300)
    niche: str | None = Field(None, max_length=160)
    followers: int | str | None = None
    offer: str | None = Field(None, max_length=300)
    notes: str | None = Field(None, max_length=2000)
    ad_activity: str | None = Field(None, max_length=200)
    list_name: str | None = Field(None, max_length=80)


@router.put("/prospects/{pid}")
def update(pid: Id, b: UpdateBody, ctx: Ctx = Depends(require("prospects"))):
    return {"prospect": _guard(lambda: P.update_prospect(ctx.tenant, ctx.user, pid, b.model_dump(exclude_unset=True)))}


class StageBody(BaseModel):
    stage: str = Field(pattern=STAGE_RE)
    reason: str = Field("", max_length=300)


@router.post("/prospects/{pid}/stage")
def stage(pid: Id, b: StageBody, ctx: Ctx = Depends(require("prospects"))):
    return {"prospect": _guard(lambda: P.set_stage(ctx.tenant, pid, b.stage, ctx.actor, b.reason, user=ctx.user))}


class ReasonBody(BaseModel):
    reason: str = Field("", max_length=300)


@router.post("/prospects/{pid}/dnc")
def dnc(pid: Id, b: ReasonBody, ctx: Ctx = Depends(require("prospects"))):
    _guard(lambda: P.visible(ctx.tenant, ctx.user, pid))
    return {"prospect": _guard(lambda: P.set_dnc(ctx.tenant, pid, ctx.actor, b.reason or "Marked do not contact"))}


@router.post("/prospects/{pid}/release")
def release(pid: Id, ctx: Ctx = Depends(require("prospects"))):
    if ctx.role != "owner":
        raise HTTPException(403, "Only the owner can release a claim")
    return {"prospect": _guard(lambda: P.release_claim(ctx.tenant, pid, ctx.actor))}


class ResearchBody(BaseModel):
    web: bool = False


@router.post("/prospects/{pid}/research")
def research(pid: Id, b: ResearchBody, ctx: Ctx = Depends(require("prospects"))):
    return _guard(lambda: P.research(ctx.tenant, ctx.user, pid, b.web))


class FactBody(BaseModel):
    claim: str = Field(min_length=3, max_length=300)
    source: str = Field(min_length=2, max_length=120)
    source_url: str | None = Field(None, max_length=400)
    quote: str = Field("", max_length=400)


@router.post("/prospects/{pid}/facts")
def add_fact(pid: Id, b: FactBody, ctx: Ctx = Depends(require("prospects"))):
    return {"prospect": _guard(lambda: P.add_fact(ctx.tenant, ctx.user, pid, b.claim, b.source, b.source_url, b.quote))}


@router.get("/prospects/{pid}/suggestions")
def suggestions(pid: Id, channel: str = Query("instagram_dm", pattern=CHANNEL_RE), ai: bool = False,
                ctx: Ctx = Depends(require("prospects"))):
    return _guard(lambda: P.suggestions(ctx.tenant, ctx.user, pid, channel, ai))


class DraftBody(BaseModel):
    channel: str = Field("instagram_dm", pattern=CHANNEL_RE)
    move: str = Field(pattern="^(" + "|".join(P.MOVES) + ")$")
    text: str | None = Field(None, min_length=1, max_length=5000)
    subject: str | None = Field(None, max_length=150)


@router.post("/prospects/{pid}/drafts")
def draft(pid: Id, b: DraftBody, ctx: Ctx = Depends(require("prospects"))):
    return _guard(lambda: P.draft_opener(ctx.tenant, ctx.user, pid, b.channel, b.move, b.text, b.subject))


@router.post("/prospects/{pid}/followups")
def followups(pid: Id, ctx: Ctx = Depends(require("prospects"))):
    p = _guard(lambda: P.visible(ctx.tenant, ctx.user, pid))
    if p["stage"] != "contacted":
        raise HTTPException(409, "Follow-ups are for prospects you've contacted who haven't replied yet")
    ids = P.draft_followups(ctx.tenant, pid, db.now(), ctx.actor)
    if not ids:
        raise HTTPException(409, "Follow-ups already exist for this prospect, or there's no way to reach them")
    return {"queued": ids}


class ReplyBody(BaseModel):
    text: str = Field("", max_length=2000)
    classification: str = Field("interested", pattern="^(" + "|".join(P.REPLY_KINDS) + ")$")


@router.post("/prospects/{pid}/reply")
def reply(pid: Id, b: ReplyBody, ctx: Ctx = Depends(require("prospects"))):
    return {"prospect": _guard(lambda: P.record_reply(ctx.tenant, pid, ctx.actor, b.text, b.classification, user=ctx.user))}


@router.get("/prospects/{pid}/slots")
def slots(pid: Id, ctx: Ctx = Depends(require("prospects"))):
    _guard(lambda: P.visible(ctx.tenant, ctx.user, pid))
    s = P.slots(ctx.tenant, 3)
    return {"slots": s, "message": None if s else "No free times in the next two weeks."}


class BookBody(BaseModel):
    mode: str = Field(pattern="^(offer|record)$")
    start: float | None = Field(None, ge=0, le=1e11)
    minutes: int = Field(45, ge=15, le=180)
    channel: str | None = Field(None, pattern=CHANNEL_RE)


@router.post("/prospects/{pid}/book")
def book(pid: Id, b: BookBody, ctx: Ctx = Depends(require("prospects"))):
    return _guard(lambda: P.book(ctx.tenant, ctx.user, pid, b.mode, b.start, b.minutes, b.channel))


@router.get("/prospects/{pid}/brief")
def brief(pid: Id, ctx: Ctx = Depends(require("prospects"))):
    p = _guard(lambda: P.visible(ctx.tenant, ctx.user, pid))
    return {"brief": P.precall_brief(ctx.tenant, p)}


# --------------------------------------------------------------------------- public proposal page
def _tenant_or_404(tid: str):
    try:
        return tenancy.get(tid)
    except tenancy.TenantNotFound:
        return None


_HDRS = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow", "Referrer-Policy": "no-referrer",
         # the page can't be framed (no clickjacking of "Accept"), loads nothing from elsewhere and posts only home
         "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff",
         "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                                    "connect-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
                                    "form-action 'none'"}
# Unauthenticated pages: wrong tokens and refused acceptances are counted per (hashed) network address, and after
# FAIL_MAX of them in FAIL_WINDOW seconds that address gets 429 until the window passes.
FAIL_WINDOW, FAIL_MAX = 15 * 60, 20


def _fail_key(t, ip: str) -> str:
    return f"proposal:{PR._ip_hash(t, ip or '?')}"


def _throttled(t, ip: str) -> bool:
    with t.db() as c:
        n = c.execute("SELECT COUNT(*) FROM login_fails WHERE key=? AND at>?",
                      (_fail_key(t, ip), time.time() - FAIL_WINDOW)).fetchone()[0]
    return n >= FAIL_MAX


def _failed(t, ip: str) -> None:
    with t.db() as c:
        c.execute("INSERT INTO login_fails(key,at) VALUES(?,?)", (_fail_key(t, ip), time.time()))


_TOO_MANY = "Too many attempts from this network. Wait a few minutes and try again."
_TOO_MANY_PAGE = PR.NOT_AVAILABLE.replace("This proposal isn't available", "Please wait a few minutes").replace(
    "The link may be wrong, or the proposal was withdrawn. Reply to the message you got it from and we'll sort it out.",
    _TOO_MANY)


@public_router.get("/t/{tid}/p/{token}", response_class=HTMLResponse)
def proposal_page(tid: str, token: str, request: Request):
    t = _tenant_or_404(tid)
    if t is None:
        return HTMLResponse(PR.NOT_AVAILABLE, status_code=404, headers=_HDRS)
    ip = client_ip(request)
    if _throttled(t, ip):
        return HTMLResponse(_TOO_MANY_PAGE, status_code=429, headers={**_HDRS, "Retry-After": str(FAIL_WINDOW)})
    r = PR.by_token(t, token)
    if r is None:
        _failed(t, ip)
        return HTMLResponse(PR.NOT_AVAILABLE, status_code=404, headers=_HDRS)
    team = auth.session_user(t, request.cookies.get(COOKIE)) is not None
    PR.record_view(t, r, count=not team, ip=ip)
    return HTMLResponse(public_demo.label(t, PR.render_html(t, r)), headers=_HDRS)


class AcceptBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    agree: bool = False
    option: str | None = Field(None, max_length=40)


@public_router.post("/t/{tid}/p/{token}/accept")
def proposal_accept(tid: str, token: str, b: AcceptBody, request: Request) -> Any:
    t = _tenant_or_404(tid)
    if t is None:
        return JSONResponse({"detail": "This proposal isn't available"}, status_code=404)
    ip = client_ip(request)
    if _throttled(t, ip):
        return JSONResponse({"detail": _TOO_MANY}, status_code=429, headers={"Retry-After": str(FAIL_WINDOW)})
    try:
        return PR.accept(t, token, b.name, b.agree, b.option, ip)
    except PR.ProposalError as e:
        if e.status in (404, 422):
            _failed(t, ip)
        return JSONResponse({"detail": str(e)}, status_code=e.status)
