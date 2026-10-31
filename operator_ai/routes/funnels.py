"""VSL funnels: build from one brief (owner and students, "clients" tab), edit the video and booking links, download the
script; public VSL, application and thank-you pages at /f/<tenant>/<slug>."""
from __future__ import annotations

import html
import json
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .. import funnel_launch, public_demo, tenancy
from ..deps import Ctx, require
from .webinars import _REG_CSS

router = APIRouter()
public_router = APIRouter()
_TID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
_SLUG = re.compile(r"^[a-z0-9-]{3,60}$")


class FunnelBody(BaseModel):
    client_id: int | None = Field(None, ge=1)
    niche: str = Field("", max_length=120)
    audience: str = Field("", max_length=200)
    offer: str = Field("", max_length=300)
    price: float | None = Field(None, ge=0, le=1_000_000)
    outcome: str = Field("", max_length=200)
    mechanism: str = Field("", max_length=120)
    pain: str = Field("", max_length=200)
    timeframe: str = Field("", max_length=60)
    business: str = Field("", max_length=120)
    video_url: str = Field("", max_length=500)
    booking_url: str = Field("", max_length=500)


class FunnelPatch(BaseModel):
    video_url: str | None = Field(None, max_length=500)
    booking_url: str | None = Field(None, max_length=500)


def _err(e: funnel_launch.FunnelError):
    raise HTTPException(e.status, str(e))


@router.post("/funnels")
def create_funnel(b: FunnelBody, ctx: Ctx = Depends(require("clients"))):
    try:
        return funnel_launch.launch(ctx.tenant, b.model_dump(), ctx.user)
    except funnel_launch.FunnelError as e:
        _err(e)


@router.get("/funnels")
def list_funnels(ctx: Ctx = Depends(require("clients"))):
    return {"items": funnel_launch.list_funnels(ctx.tenant, ctx.user)}


@router.get("/funnels/{fid}")
def funnel_detail(fid: int, ctx: Ctx = Depends(require("clients"))):
    try:
        return funnel_launch.summary(ctx.tenant, fid, ctx.user)
    except funnel_launch.FunnelError as e:
        _err(e)


@router.patch("/funnels/{fid}")
def funnel_update(fid: int, b: FunnelPatch, ctx: Ctx = Depends(require("clients"))):
    try:
        return funnel_launch.update(ctx.tenant, fid, b.model_dump(), ctx.user)
    except funnel_launch.FunnelError as e:
        _err(e)


@router.get("/funnels/{fid}/script.txt")
def funnel_script(fid: int, ctx: Ctx = Depends(require("clients"))):
    try:
        s = funnel_launch.summary(ctx.tenant, fid, ctx.user)
    except funnel_launch.FunnelError as e:
        _err(e)
    name = re.sub(r"[^A-Za-z0-9]+", "-", s["title"]).strip("-")[:60] or "vsl-script"
    return PlainTextResponse(funnel_launch.script_text(s["title"], s["copy"]),
                             headers={"Content-Disposition": f'attachment; filename="{name}.txt"'})


# --------------------------------------------------------------------------- public pages
_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title><style>{css}
.vsl{{max-width:860px;margin:0 auto}}.frame{{position:relative;padding-top:56.25%;border-radius:16px;overflow:hidden;
background:#020617;box-shadow:0 30px 60px -20px rgba(2,6,23,.6)}}.frame iframe,.frame video{{position:absolute;inset:0;
width:100%;height:100%;border:0}}.soon{{position:absolute;inset:0;display:grid;place-items:center;color:#94a3b8;
font-size:15px}}.center{{text-align:center}}.go{{display:inline-block;margin-top:26px;background:#f59e0b;color:#0f172a;
font-weight:700;font-size:18px;padding:16px 34px;border-radius:12px;text-decoration:none}}textarea{{font:inherit;
width:100%;margin-top:6px;padding:12px 14px;border:1px solid #cbd5e1;border-radius:10px;background:#f8fafc;
min-height:70px}}.single{{max-width:640px;margin:0 auto}}</style></head><body>{body}</body></html>"""


def _tenant(tid: str):
    if not _TID.match(tid or ""):
        raise HTTPException(404, "Not found")
    try:
        return tenancy.get(tid)
    except tenancy.TenantNotFound:
        raise HTTPException(404, "Not found") from None


def _live(tid: str, slug: str):
    t = _tenant(tid)
    f = funnel_launch.by_slug(t, slug) if _SLUG.match(slug or "") else None
    if f is None or f["status"] != "live":
        raise HTTPException(404, "Not found")
    return t, f, json.loads(f["data"] or "{}").get("copy") or {}


def _embed(url: str) -> str:
    e = html.escape
    for pat, tpl in ((r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)([\w-]{6,20})",
                      "https://www.youtube.com/embed/{}"),
                     (r"vimeo\.com/(\d{5,12})", "https://player.vimeo.com/video/{}"),
                     (r"loom\.com/share/([\w]{10,40})", "https://www.loom.com/embed/{}")):
        m = re.search(pat, url or "")
        if m:
            return (f'<iframe src="{e(tpl.format(m.group(1)))}" allow="autoplay; fullscreen; picture-in-picture" '
                    'allowfullscreen title="Video"></iframe>')
    if re.match(r"^https://\S+\.mp4$", url or ""):
        return f'<video src="{e(url)}" controls playsinline></video>'
    return '<div class="soon">The video is coming soon.</div>'


def _html(t, title: str, body: str) -> HTMLResponse:
    return HTMLResponse(public_demo.label(t, _PAGE.format(title=html.escape(title), css=_REG_CSS, body=body)))


@public_router.get("/f/{tid}/{slug}", response_class=HTMLResponse)
def vsl_page(tid: str, slug: str):
    t, f, copy = _live(tid, slug)
    with t.db() as c:
        c.execute("UPDATE funnels SET visits=visits+1 WHERE id=?", (f["id"],))
    e = html.escape
    bullets = "".join(f"<li>{e(b)}</li>" for b in copy.get("bullets", []))
    body = (f'<section class="hero"><div class="vsl"><div class="center"><span class="kicker">'
            f'{e(funnel_launch.brand(t, f))}</span><h1>{e(copy.get("headline") or f["title"])}</h1>'
            f'<p class="sub">{e(copy.get("subhead", ""))}</p></div><div class="frame">{_embed(f["video_url"] or "")}'
            f'</div><div class="center"><a class="go" href="/f/{e(tid)}/{e(slug)}/apply">'
            f'{e(copy.get("cta") or "Apply now")}</a></div></div></section>'
            f'<section class="learn" style="max-width:900px"><div class="panel" style="margin:0 auto">'
            f'<h3>In this video</h3><ul>{bullets}</ul></div></section>'
            f'<footer>&copy; {e(funnel_launch.brand(t, f))}</footer>')
    return _html(t, f["title"], body)


@public_router.get("/f/{tid}/{slug}/apply", response_class=HTMLResponse)
def apply_page(tid: str, slug: str):
    t, f, _copy = _live(tid, slug)
    e = html.escape
    qs = "".join(f'<label>{e(q)}<textarea name="{k}" maxlength="600"></textarea></label>'
                 for k, q in funnel_launch.QUESTIONS)
    cfg = json.dumps({"base": f"/f/{tid}/{slug}"}).replace("<", "\\u003c")
    body = (f'<section class="hero" style="padding-bottom:60px"><div class="single"><span class="kicker">Apply</span>'
            f'<h1>See if you\'re a fit</h1><p class="sub">A few quick questions so the call is worth your time.</p>'
            f'<div class="card"><form id="f" novalidate><label>Your name<input type="text" name="name" required '
            f'maxlength="120"></label><label>Email<input type="email" name="email" required maxlength="200"></label>'
            f'{qs}<label class="consent"><input type="checkbox" name="consent"> <span>I agree to be contacted about '
            f'my application.</span></label><button type="submit">Submit application</button>'
            f'<div class="msg" id="m" role="status"></div></form></div></div></section>'
            f'<script>const cfg={cfg};document.getElementById("f").addEventListener("submit",async(ev)=>{{'
            f'ev.preventDefault();const f=ev.target,m=document.getElementById("m");m.className="msg";m.textContent="";'
            f'const body={{consent:f.consent.checked}};for(const el of f.querySelectorAll("input[type=text],'
            f'input[type=email],textarea"))body[el.name]=el.value;try{{const r=await fetch(cfg.base+"/apply",'
            f'{{method:"POST",headers:{{"content-type":"application/json"}},body:JSON.stringify(body)}});'
            f'const d=await r.json();if(!r.ok)throw new Error(typeof d.detail==="string"?d.detail:"Please check the '
            f'form.");location.href=d.next}}catch(e){{m.className="msg err";m.textContent=e.message}}}});</script>')
    return _html(t, f"Apply: {f['title']}", body)


@public_router.post("/f/{tid}/{slug}/apply")
async def apply_submit(tid: str, slug: str, request: Request):
    t = _tenant(tid)
    try:
        d = json.loads((await request.body())[:20_000] or b"{}")
    except ValueError:
        raise HTTPException(422, "Malformed JSON") from None
    if not isinstance(d, dict):
        raise HTTPException(422, "Expected a JSON object")
    refused = public_demo.email_refused(t, d.get("email"))
    if refused:
        raise HTTPException(422, refused)
    try:
        return funnel_launch.apply(t, slug, d)
    except funnel_launch.FunnelError as e:
        raise HTTPException(e.status, str(e)) from None


@public_router.get("/f/{tid}/{slug}/thanks", response_class=HTMLResponse)
def thanks_page(tid: str, slug: str):
    t, f, _copy = _live(tid, slug)
    e = html.escape
    book = (f'<a class="go" href="{e(f["booking_url"])}" target="_blank" rel="noopener">Book your call</a>'
            if f.get("booking_url") else '<p class="sub">We\'ll be in touch shortly to book your call.</p>')
    body = (f'<section class="hero"><div class="single center"><span class="kicker">Application received</span>'
            f'<h1>You\'re in. One last step.</h1><p class="sub">Pick a time for your call. Come with your numbers and '
            f'your one biggest question.</p>{book}</div></section>'
            f'<footer>&copy; {e(funnel_launch.brand(t, f))}</footer>')
    return _html(t, f"Thanks: {f['title']}", body)
