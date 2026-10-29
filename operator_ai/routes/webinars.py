"""Webinar execution (owner, "webinar" tab) and the public registration page /w/<tenant>/<slug>."""
from __future__ import annotations

import html
import json
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .. import public_demo, tenancy, webinars
from ..deps import Ctx, client_ip, require

router = APIRouter()
public_router = APIRouter()
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{2,79}$")
_TID = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")
MAX_BODY = 32 * 1024


def _err(e: webinars.WebinarError):
    raise HTTPException(e.status, str(e))


class WebinarBody(BaseModel):
    title: str = Field("", max_length=140)
    starts_at: float | str
    tz: str = Field("UTC", min_length=1, max_length=60)
    join_url: str = Field("", max_length=400)
    replay_url: str = Field("", max_length=400)
    duration_min: int = Field(60, ge=15, le=240)
    offer_at_min: int = Field(35, ge=1, le=239)
    chain_id: str | None = Field(None, max_length=80)
    client_id: int | None = Field(None, ge=1)


@router.get("/webinars")
def list_webinars(ctx: Ctx = Depends(require("webinar"))):
    return {"items": webinars.list_all(ctx.tenant), "public_url_set": bool(webinars.public_base())}


@router.post("/webinars")
def create_webinar(b: WebinarBody, ctx: Ctx = Depends(require("webinar"))):
    try:
        return webinars.create(ctx.tenant, b.model_dump(), ctx.user)
    except webinars.WebinarError as e:
        _err(e)


@router.get("/webinars/{wid}")
def webinar_detail(wid: int, ctx: Ctx = Depends(require("webinar"))):
    try:
        return webinars.detail(ctx.tenant, wid)
    except webinars.WebinarError as e:
        _err(e)


class WebinarPatch(BaseModel):
    join_url: str | None = Field(None, max_length=400)
    replay_url: str | None = Field(None, max_length=400)
    starts_at: float | str | None = None
    tz: str | None = Field(None, max_length=60)
    status: str | None = Field(None, pattern="^(cancelled)$")


@router.put("/webinars/{wid}")
def update_webinar(wid: int, b: WebinarPatch, ctx: Ctx = Depends(require("webinar"))):
    try:
        return webinars.update(ctx.tenant, wid, b.model_dump(), ctx.actor)
    except webinars.WebinarError as e:
        _err(e)


class RegistrantBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=200)
    consent: bool
    source: str = Field("manual", max_length=40)


@router.post("/webinars/{wid}/registrants")
def add_registrant(wid: int, b: RegistrantBody, ctx: Ctx = Depends(require("webinar"))):
    try:
        return webinars.add_registrant(ctx.tenant, wid, b.name, b.email, b.consent, b.source, ctx.actor)
    except webinars.WebinarError as e:
        _err(e)


class AttendancePatch(BaseModel):
    attended: bool | None = None
    minutes_watched: float | None = Field(None, ge=0, le=600)
    stayed_past_offer: bool | None = None
    booked: bool | None = None


@router.put("/webinars/{wid}/registrants/{rid}")
def set_attendance(wid: int, rid: int, b: AttendancePatch, ctx: Ctx = Depends(require("webinar"))):
    try:
        return webinars.set_attendance(ctx.tenant, wid, rid, b.model_dump(), ctx.actor)
    except webinars.WebinarError as e:
        _err(e)


class CsvBody(BaseModel):
    csv: str = Field(min_length=10, max_length=2_000_000)


@router.post("/webinars/{wid}/attendance")
def import_attendance(wid: int, b: CsvBody, ctx: Ctx = Depends(require("webinar"))):
    try:
        return webinars.import_attendance(ctx.tenant, wid, b.csv, ctx.actor)
    except webinars.WebinarError as e:
        _err(e)


@router.post("/webinars/{wid}/post-event")
def post_event(wid: int, ctx: Ctx = Depends(require("webinar"))):
    try:
        return webinars.post_event(ctx.tenant, wid, ctx.actor)
    except webinars.WebinarError as e:
        _err(e)


@router.get("/webinars/{wid}/rates")
def rates(wid: int, ctx: Ctx = Depends(require("webinar"))):
    if webinars.get(ctx.tenant, wid) is None:
        raise HTTPException(404, "Webinar not found")
    return webinars.rates(ctx.tenant, wid)


@router.post("/webinars/{wid}/diagnose")
def diagnose(wid: int, ctx: Ctx = Depends(require("webinar"))):
    try:
        return webinars.diagnose(ctx.tenant, wid, ctx.user)
    except webinars.WebinarError as e:
        _err(e)


# --------------------------------------------------------------------------- public page
_CSS = """body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:0;background:#f6f6f3;color:#16181d}
main{max-width:560px;margin:0 auto;padding:24px 16px}.card{background:#fff;border:1px solid #e4e3dd;border-radius:14px;padding:20px}
h1{font-size:26px;line-height:1.2;margin:0 0 8px}p.sub{color:#3d4250;margin:0 0 12px}ul{padding-left:20px}li{margin:4px 0}
label{display:block;font-size:13px;color:#5d6270;margin-top:10px}input[type=text],input[type=email],textarea,select{font:inherit;width:100%;box-sizing:border-box;margin-top:4px;padding:10px;border:1px solid #d9d8d2;border-radius:10px}
.consent{display:flex;gap:8px;align-items:flex-start;margin-top:12px;color:#16181d;font-size:14px}.consent input{margin-top:3px}
button{font:inherit;width:100%;margin-top:14px;padding:12px;border:0;border-radius:10px;background:#5b6cff;color:#fff;cursor:pointer}
.hp{position:absolute;left:-9999px;width:1px;height:1px;overflow:hidden}.msg{margin-top:12px;font-size:14px}.err{color:#b91c1c}
small{color:#5d6270}"""

_REG_CSS = """*{box-sizing:border-box}
body{font-family:Inter,system-ui,-apple-system,Segoe UI,sans-serif;margin:0;background:#f8fafc;
color:#0f172a;-webkit-font-smoothing:antialiased}
.hero{background:radial-gradient(1200px 500px at 85% -10%,#334155 0%,transparent 60%),#0f172a;color:#fff;
padding:56px 20px 120px}
.wrap{max-width:1080px;margin:0 auto;display:grid;grid-template-columns:1.25fr 1fr;gap:48px;align-items:start}
.kicker{display:inline-flex;align-items:center;gap:10px;color:#f59e0b;font-weight:700;
font-size:13px;letter-spacing:.12em;
text-transform:uppercase}.kicker:before{content:"";width:28px;height:3px;background:#f59e0b;border-radius:2px}
h1{font-size:46px;line-height:1.08;letter-spacing:-.02em;margin:18px 0 16px}
p.sub{color:#cbd5e1;font-size:18px;line-height:1.5;
margin:0 0 24px}.when{display:inline-flex;align-items:center;gap:10px;background:rgba(245,158,11,.12);border:1px solid
rgba(245,158,11,.4);color:#fde68a;padding:10px 14px;border-radius:999px;font-weight:600;font-size:15px}
.host{margin-top:18px;color:#94a3b8;font-size:14px}.host b{color:#e2e8f0}
.card{background:#fff;color:#0f172a;border-radius:18px;padding:28px;box-shadow:0 30px 60px -20px rgba(2,6,23,.45);
margin-top:8px}.card h2{font-size:20px;margin:0 0 4px}.card .lead{color:#64748b;font-size:14px;margin:0 0 8px}
label{display:block;font-size:13px;font-weight:600;color:#334155;margin-top:14px}
input[type=text],input[type=email]{font:inherit;width:100%;margin-top:6px;padding:12px 14px;border:1px solid #cbd5e1;
border-radius:10px;background:#f8fafc}input:focus{outline:2px solid #f59e0b;outline-offset:1px;background:#fff}
.consent{display:flex;gap:10px;align-items:flex-start;margin-top:16px;font-weight:400;font-size:13px;color:#475569}
.consent input{margin-top:2px;width:16px;height:16px;accent-color:#f59e0b}
button{font:inherit;font-weight:700;font-size:16px;width:100%;margin-top:18px;padding:15px;border:0;border-radius:12px;
background:#f59e0b;color:#0f172a;cursor:pointer;transition:transform .1s}button:hover{transform:translateY(-1px)}
button:disabled{opacity:.6;cursor:default;transform:none}
.free{text-align:center;color:#94a3b8;font-size:12px;margin-top:10px}
.hp{position:absolute;left:-9999px;width:1px;height:1px;overflow:hidden}
.msg{margin-top:12px;font-size:14px;color:#047857}
.err{color:#b91c1c}.learn{max-width:1120px;margin:-64px auto 0;padding:0 20px 64px}
.learn .panel{background:#fff;border:1px solid #e2e8f0;border-radius:18px;padding:32px;max-width:620px}
.learn h3{margin:0 0 18px;font-size:22px}.learn ul{list-style:none;padding:0;margin:0;display:grid;gap:14px}
.learn li{display:flex;gap:12px;align-items:flex-start;font-size:16px;line-height:1.45;color:#1e293b}
.learn li:before{content:"\\2713";flex:none;width:24px;height:24px;border-radius:50%;background:#fef3c7;color:#b45309;
font-weight:700;font-size:13px;display:grid;place-items:center}
footer{text-align:center;color:#94a3b8;font-size:12px;padding:0 20px 40px}
@media(max-width:860px){.wrap{grid-template-columns:1fr;gap:28px}h1{font-size:34px}.hero{padding:40px 16px 96px}
.learn{margin-top:-56px;padding:0 16px 48px}.learn .panel{padding:24px}}"""

REG_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>{css}</style></head><body>
<section class="hero"><div class="wrap"><div><span class="kicker">Free live training</span><h1>{headline}</h1>
<p class="sub">{subhead}</p>
<div class="when">&#128197; {when}</div><div class="host">Hosted by <b>{brand}</b></div></div>
<div class="card"><h2>Save your free seat</h2><p class="lead">Takes 10 seconds. We'll email you the link.</p>
<form id="f" novalidate><label>Your name<input type="text" name="name" autocomplete="name" required maxlength="120"></label>
<label>Email<input type="email" name="email" autocomplete="email" required maxlength="200"></label>
<div class="hp" aria-hidden="true"><label>Website<input type="text" name="website" tabindex="-1" autocomplete="off"></label></div>
<label class="consent"><input type="checkbox" name="consent"> <span>{consent}</span></label>
<button type="submit">{button}</button>
<div class="free">100% free &middot; Live Q&amp;A at the end</div>
<div class="msg" id="m" role="status"></div></form></div></div></section>
<section class="learn"><div class="panel"><h3>What you'll learn</h3><ul>{bullets}</ul></div></section>
<footer>&copy; {brand}</footer>
<script>
const cfg = {cfg};
document.getElementById("f").addEventListener("submit", async (ev) => {{
  ev.preventDefault(); const f = ev.target; const m = document.getElementById("m"); m.className = "msg"; m.textContent = "";
  const body = {{name: f.name.value, email: f.email.value, consent: f.consent.checked, website: f.website.value,
    source: new URLSearchParams(location.search).get("src") || "direct"}};
  try {{
    const r = await fetch(cfg.base + "/register", {{method: "POST", headers: {{"content-type": "application/json"}}, body: JSON.stringify(body)}});
    const d = await r.json();
    if (!r.ok) throw new Error(typeof d.detail === "string" ? d.detail : "Please check the form and try again.");
    f.querySelectorAll("input,button").forEach(x => x.disabled = true);
    m.textContent = d.message + (d.join_url ? " Join link: " + d.join_url : "");
  }} catch (e) {{ m.className = "msg err"; m.textContent = e.message; }}
}});
</script></body></html>"""

CLOSED_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>{css}</style></head><body><main><div class="card"><small>{brand}</small><h1>{title}</h1>
<p class="sub">{message}</p></div></main></body></html>"""

UNSUB_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>Unsubscribe</title><style>{css}</style></head><body><main><div class="card">
<h1>Stop emails about {title}?</h1><p class="sub">You won't get any more emails about this training.</p>
<form method="post"><input type="hidden" name="r" value="{token}"><button type="submit">Unsubscribe</button></form></div></main></body></html>"""


def _tenant(tid: str):
    if not _TID.match(tid or ""):
        raise HTTPException(404, "Not found")
    try:
        return tenancy.get(tid)
    except tenancy.TenantNotFound:
        raise HTTPException(404, "Not found")


def _brand(t) -> str:
    from .. import config

    b = t.get("brand", {}) or {}
    return (b.get("name") if isinstance(b, dict) else None) or config.get().brand


def _host(t, w: dict) -> str:
    """The client's name on a page made for a client, else the workspace brand."""
    if w.get("client_id"):
        with t.db() as c:
            r = c.execute("SELECT name FROM clients WHERE id=?", (int(w["client_id"]),)).fetchone()
        if r and r["name"]:
            return str(r["name"])
    return _brand(t)


@public_router.get("/w/{tid}/{slug}", response_class=HTMLResponse)
def reg_page(tid: str, slug: str):
    t = _tenant(tid)
    if not _SLUG.match(slug or ""):
        raise HTTPException(404, "Not found")
    w = webinars.page_view(t, slug)
    if w is None:
        raise HTTPException(404, "Not found")
    e = html.escape
    if w["closed"]:
        msg = "This training was cancelled." if w["status"] == "cancelled" else "This training has finished. Registration is closed."
        return HTMLResponse(CLOSED_HTML.format(title=e(w["title"]), css=_CSS, brand=e(_brand(t)), message=e(msg)))
    p = w["page"] or {}
    cfg = json.dumps({"base": f"/w/{tid}/{slug}"}).replace("<", "\\u003c")
    return HTMLResponse(public_demo.label(t, REG_HTML.format(
        title=e(w["title"]), css=_REG_CSS, brand=e(_host(t, w)), headline=e(p.get("headline") or w["title"]),
        subhead=e(p.get("subhead") or ""), when=e(w["when"]), bullets="".join(f"<li>{e(b)}</li>" for b in p.get("bullets", [])),
        consent=e(p.get("consent") or ""), button=e(p.get("button") or "Save my seat"), cfg=cfg)))


@public_router.post("/w/{tid}/{slug}/register")
async def register(tid: str, slug: str, request: Request):
    t = _tenant(tid)
    if not _SLUG.match(slug or ""):
        raise HTTPException(404, "Not found")
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise HTTPException(413, "Body too large")
    try:
        d = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(422, "Malformed JSON")
    if not isinstance(d, dict):
        raise HTTPException(422, "Expected a JSON object")
    refused = None if d.get("website") else public_demo.email_refused(t, d.get("email"))  # bots still get "thanks"
    if refused:
        raise HTTPException(422, refused)
    try:
        return webinars.register(t, slug, d.get("name"), d.get("email"), d.get("consent"), d.get("website") or "",
                                 d.get("source") or "", client_ip(request))
    except webinars.WebinarError as e:
        _err(e)


@public_router.get("/w/{tid}/{slug}/unsubscribe", response_class=HTMLResponse)
def unsubscribe_page(tid: str, slug: str, r: str = ""):
    t = _tenant(tid)
    w = webinars.by_slug(t, slug) if _SLUG.match(slug or "") else None
    if w is None or not re.match(r"^[A-Za-z0-9_-]{10,80}$", r or ""):
        raise HTTPException(404, "Not found")
    with t.db() as c:  # only a real registrant's link shows the page (a made-up token never reveals a draft's title)
        if c.execute("SELECT 1 FROM webinar_registrants WHERE webinar_id=? AND token=?", (w["id"], r)).fetchone() is None:
            raise HTTPException(404, "Not found")
    return HTMLResponse(UNSUB_HTML.format(css=_CSS, title=html.escape(w["title"]), token=html.escape(r)))


@public_router.post("/w/{tid}/{slug}/unsubscribe", response_class=HTMLResponse)
async def unsubscribe(tid: str, slug: str, request: Request):
    t = _tenant(tid)
    if not _SLUG.match(slug or ""):
        raise HTTPException(404, "Not found")
    raw = (await request.body())[:2048].decode("utf-8", errors="replace")
    from urllib.parse import parse_qs

    token = (parse_qs(raw).get("r") or [""])[0]
    if webinars.rate_limited(t.id, client_ip(request), "unsub", 20, 600):
        raise HTTPException(429, "Too many requests. Please try again in a few minutes.")
    try:
        webinars.unsubscribe(t, slug, token)
    except webinars.WebinarError as e:
        _err(e)
    w = webinars.by_slug(t, slug)
    return HTMLResponse(CLOSED_HTML.format(title=html.escape(w["title"]), css=_CSS, brand=html.escape(_brand(t)),
                                           message="Done. You won't get any more emails about this training."))
