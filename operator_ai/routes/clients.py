"""Clients: onboarding after the win, the weekly delivery loop and fulfilment. The owner sees every client; a
student sees and runs only their own clients. The client's intake form is public at /c/<tenant>/<token>."""
from __future__ import annotations

import html
import json
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .. import client_ops, db, public_demo, tenancy, webinars
from ..agents import fulfilment
from ..deps import Ctx, client_ip, require

router = APIRouter()
public_router = APIRouter()
_TID = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{10,80}$")


def _err(e: client_ops.ClientOpsError):
    raise HTTPException(e.status, str(e))


def _mine(ctx: Ctx, client_id: int) -> dict:
    try:
        return client_ops.for_user(ctx.tenant, ctx.user, client_id)
    except client_ops.ClientOpsError as e:
        _err(e)


@router.get("/clients")
def clients(ctx: Ctx = Depends(require("clients"))):
    return {"items": client_ops.visible(ctx.tenant, ctx.user), "role": ctx.role,
            "statuses": client_ops.STATUS_LABELS, "weekdays": client_ops.WEEKDAYS}


class ClientBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    niche: str = Field("", max_length=120)
    offer: str = Field("", max_length=300)
    price: float = Field(3000, ge=0, le=1_000_000)
    metrics: dict[str, float] = Field(default_factory=dict)
    email: str = Field("", max_length=200)
    contact_name: str = Field("", max_length=120)
    onboard: bool = True


@router.post("/clients")
def add_client(b: ClientBody, ctx: Ctx = Depends(require("clients"))):
    if b.email and not re.match(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$", b.email):
        raise HTTPException(422, "That email address doesn't look right")
    try:
        metrics = client_ops.finite_numbers(b.metrics, "Starting numbers")
    except client_ops.ClientOpsError as e:
        _err(e)
    owner_uid = None if ctx.role == "owner" else ctx.user["id"]
    with ctx.tenant.db() as c:
        cid = c.execute("INSERT INTO clients(name,niche,offer,price,metrics,demo,created,email,contact_name,owner_user_id,status) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (b.name, b.niche, b.offer, b.price, db.jdump(metrics), 1 if ctx.tenant.is_demo else 0, db.now(),
                         b.email or None, b.contact_name or None, owner_uid, None if b.onboard else "active")).lastrowid
    out: dict[str, Any] = {"id": cid}
    if b.onboard:
        out["onboarding"] = client_ops.start_onboarding(ctx.tenant, cid, owner_user_id=owner_uid, actor=ctx.actor)
    return out


@router.get("/clients/{client_id}")
def client_detail(client_id: int, ctx: Ctx = Depends(require("clients"))):
    try:
        return client_ops.detail(ctx.tenant, ctx.user, client_id)
    except client_ops.ClientOpsError as e:
        _err(e)


class ClientPatch(BaseModel):
    status: str | None = Field(None, pattern="^(onboarding|active|paused|ended)$")
    retainer: float | None = Field(None, ge=0, le=1_000_000)
    price: float | None = Field(None, ge=0, le=1_000_000)
    email: str | None = Field(None, max_length=200)
    contact_name: str | None = Field(None, max_length=120)
    niche: str | None = Field(None, max_length=120)
    offer: str | None = Field(None, max_length=300)
    report_weekday: int | None = Field(None, ge=0, le=6)
    kpi_targets: dict[str, float] | None = None
    access: dict[str, str] | None = None
    brief: dict[str, str] | None = None


@router.put("/clients/{client_id}")
def update_client(client_id: int, b: ClientPatch, ctx: Ctx = Depends(require("clients"))):
    _mine(ctx, client_id)
    try:
        client_ops.update(ctx.tenant, client_id, b.model_dump(exclude_none=True), ctx.actor, ctx.role)
        return client_ops.detail(ctx.tenant, ctx.user, client_id)
    except client_ops.ClientOpsError as e:
        _err(e)


@router.post("/clients/{client_id}/onboard")
def onboard(client_id: int, ctx: Ctx = Depends(require("clients"))):
    cl = _mine(ctx, client_id)
    try:
        return client_ops.start_onboarding(ctx.tenant, client_id, owner_user_id=cl.get("owner_user_id"), actor=ctx.actor)
    except client_ops.ClientOpsError as e:
        _err(e)


class TaskBody(BaseModel):
    done: bool


@router.post("/clients/{client_id}/tasks/{key}")
def tick_task(client_id: int, key: str, b: TaskBody, ctx: Ctx = Depends(require("clients"))):
    _mine(ctx, client_id)
    try:
        return client_ops.set_task(ctx.tenant, client_id, key, b.done, ctx.actor)
    except client_ops.ClientOpsError as e:
        _err(e)


@router.post("/clients/{client_id}/tasks/{key}/verify")
def verify_task(client_id: int, key: str, ctx: Ctx = Depends(require("clients"))):
    _mine(ctx, client_id)
    try:
        return client_ops.verify_task(ctx.tenant, client_id, key, ctx.actor)
    except client_ops.ClientOpsError as e:
        _err(e)


class MetricsBody(BaseModel):
    metrics: dict[str, float]
    period_start: float | None = Field(None, ge=0)
    note: str = Field("", max_length=300)


@router.post("/clients/{client_id}/metrics")
def add_metrics(client_id: int, b: MetricsBody, ctx: Ctx = Depends(require("clients"))):
    _mine(ctx, client_id)
    try:
        return client_ops.record_metrics(ctx.tenant, client_id, b.metrics, b.period_start, "manual", b.note, ctx.actor)
    except client_ops.ClientOpsError as e:
        _err(e)


class CsvBody(BaseModel):
    csv: str = Field(min_length=10, max_length=2_000_000)
    period_start: float | None = Field(None, ge=0)


@router.post("/clients/{client_id}/metrics/csv")
def add_metrics_csv(client_id: int, b: CsvBody, ctx: Ctx = Depends(require("clients"))):
    _mine(ctx, client_id)
    try:
        return client_ops.import_ads_csv(ctx.tenant, client_id, b.csv, b.period_start, ctx.actor)
    except client_ops.ClientOpsError as e:
        _err(e)


class CallBody(BaseModel):
    outcome: str = Field(pattern="^(held|missed)$")
    note: str = Field("", max_length=500)


@router.post("/clients/{client_id}/calls")
def log_call(client_id: int, b: CallBody, ctx: Ctx = Depends(require("clients"))):
    _mine(ctx, client_id)
    try:
        return client_ops.log_call(ctx.tenant, client_id, b.outcome, b.note, ctx.actor)
    except client_ops.ClientOpsError as e:
        _err(e)


@router.post("/clients/{client_id}/weekly")
def weekly(client_id: int, ctx: Ctx = Depends(require("clients"))):
    _mine(ctx, client_id)
    try:
        return client_ops.weekly_ops(ctx.tenant, client_id, ctx.user)
    except client_ops.ClientOpsError as e:
        _err(e)


@router.post("/clients/{client_id}/fulfil")
def fulfil(client_id: int, ctx: Ctx = Depends(require("clients"))):
    _mine(ctx, client_id)
    try:
        return fulfilment.fulfil(ctx.tenant, client_id, ctx.user)
    except KeyError:
        raise HTTPException(404, "Client not found")


# --------------------------------------------------------------------------- public intake form
_CSS = """body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:0;background:#f6f6f3;color:#16181d}
main{max-width:620px;margin:0 auto;padding:24px 16px}.card{background:#fff;border:1px solid #e4e3dd;border-radius:14px;padding:20px}
h1{font-size:24px;margin:0 0 6px}p{color:#3d4250}label{display:block;font-size:13px;color:#5d6270;margin-top:12px}
input[type=text],input[type=email],textarea,select{font:inherit;width:100%;box-sizing:border-box;margin-top:4px;padding:10px;border:1px solid #d9d8d2;border-radius:10px}
.check{display:flex;gap:8px;align-items:flex-start;color:#16181d;font-size:14px}.check input{margin-top:3px}
button{font:inherit;width:100%;margin-top:16px;padding:12px;border:0;border-radius:10px;background:#5b6cff;color:#fff;cursor:pointer}
.hp{position:absolute;left:-9999px;width:1px;height:1px;overflow:hidden}.msg{margin-top:12px;font-size:14px}.err{color:#b91c1c}"""

INTAKE_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>Intake: {name}</title><style>{css}</style></head><body><main><div class="card">
<h1>Welcome, {name}</h1><p>About ten minutes. Your answers shape every ad, page and report we write for you, so plain words are best.
Please don't put passwords here; we'll ask for access through your own tools.</p>
<form id="f" novalidate>
<label>What do you sell? (your offer)<input type="text" name="offer" maxlength="300" value="{offer}"></label>
<label>Who is it for?<input type="text" name="audience" maxlength="200" value="{audience}"></label>
<label>Your market in a few words (e.g. "online fitness coaches")<input type="text" name="niche" maxlength="120" value="{niche}"></label>
<label>The result your customers want<input type="text" name="outcome" maxlength="200" value="{outcome}"></label>
<label>How you get them there (your method)<input type="text" name="mechanism" maxlength="160" value="{mechanism}"></label>
<label>What they're tired of<input type="text" name="pain" maxlength="160" value="{pain}"></label>
<label>Typical timeframe<input type="text" name="timeframe" maxlength="60" value="{timeframe}"></label>
<label>Proof you already have (real results, reviews, case studies)<textarea name="proof" rows="4" maxlength="2000"></textarea></label>
<label class="check"><input type="checkbox" name="proof_permission"> <span>I have permission from the people involved to use this proof in marketing.</span></label>
<label>Links to your assets (one per line: website, brand folder, videos)<textarea name="assets" rows="3"></textarea></label>
<label>Best email for your weekly report<input type="email" name="email" maxlength="200" value="{email}"></label>
<label>Which day should your weekly report arrive?<select name="report_weekday">{days}</select></label>
<div class="hp" aria-hidden="true"><label>Website<input type="text" name="website" tabindex="-1" autocomplete="off"></label></div>
<button type="submit">Send my answers</button><div class="msg" id="m" role="status"></div></form></div></main>
<script>
const cfg = {cfg};
document.getElementById("f").addEventListener("submit", async (ev) => {{
  ev.preventDefault(); const f = ev.target; const m = document.getElementById("m"); m.className = "msg"; m.textContent = "";
  const v = (n) => f[n].value.trim();
  const body = {{offer: v("offer"), audience: v("audience"), niche: v("niche"), outcome: v("outcome"), mechanism: v("mechanism"),
    pain: v("pain"), timeframe: v("timeframe"), proof: v("proof"), proof_permission: f.proof_permission.checked,
    assets: v("assets").split("\\n").map(s => s.trim()).filter(Boolean), email: v("email"),
    report_weekday: Number(f.report_weekday.value), website: f.website.value}};
  try {{
    const r = await fetch(cfg.base, {{method: "POST", headers: {{"content-type": "application/json"}}, body: JSON.stringify(body)}});
    const d = await r.json();
    if (!r.ok) throw new Error(typeof d.detail === "string" ? d.detail : "Please check the form and try again.");
    m.textContent = "Thank you. We've got your answers and will use them from today.";
  }} catch (e) {{ m.className = "msg err"; m.textContent = e.message; }}
}});
</script></body></html>"""


def _tenant(tid: str):
    if not _TID.match(tid or ""):
        raise HTTPException(404, "Not found")
    try:
        return tenancy.get(tid)
    except tenancy.TenantNotFound:
        raise HTTPException(404, "Not found")


@public_router.get("/c/{tid}/{token}", response_class=HTMLResponse)
def intake_page(tid: str, token: str):
    t = _tenant(tid)
    cl = client_ops.by_token(t, token) if _TOKEN.match(token or "") else None
    if cl is None:
        raise HTTPException(404, "Not found")
    b = cl["brief"] or {}
    e = lambda k: html.escape(str(b.get(k) or ""))
    days = "".join(f"<option value='{i}'{' selected' if i == cl['report_weekday'] else ''}>{d}</option>"
                   for i, d in enumerate(client_ops.WEEKDAYS))
    cfg = json.dumps({"base": f"/c/{tid}/{token}"}).replace("<", "\\u003c")
    return HTMLResponse(public_demo.label(t, INTAKE_HTML.format(
        name=html.escape(cl["name"]), css=_CSS, offer=html.escape(b.get("offer") or cl.get("offer") or ""),
        audience=e("audience"), niche=html.escape(b.get("niche") or cl.get("niche") or ""), outcome=e("outcome"),
        mechanism=e("mechanism"), pain=e("pain"), timeframe=e("timeframe"), email=html.escape(cl.get("email") or ""),
        days=days, cfg=cfg)),
        # the page shows the client's own details: never cached, and the link (its token) never leaks as a referrer
        headers={"X-Robots-Tag": "noindex", "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@public_router.post("/c/{tid}/{token}")
async def intake_submit(tid: str, token: str, request: Request):
    t = _tenant(tid)
    if not _TOKEN.match(token or ""):
        raise HTTPException(404, "Not found")
    raw = await request.body()
    if len(raw) > 64 * 1024:
        raise HTTPException(413, "Body too large")
    try:
        d = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(422, "Malformed JSON")
    if not isinstance(d, dict):
        raise HTTPException(422, "Expected a JSON object")
    if d.get("website"):  # honeypot
        return {"ok": True}
    refused = public_demo.email_refused(t, d.get("email"))
    if refused:
        raise HTTPException(422, refused)
    if webinars.rate_limited(t.id, client_ip(request), "intake", 10, 600):
        raise HTTPException(429, "Too many submissions. Please try again in a few minutes.")
    try:
        return client_ops.submit_intake(t, token, d)
    except client_ops.ClientOpsError as e:
        _err(e)
