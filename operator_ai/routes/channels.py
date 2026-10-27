"""Public (un-authenticated, signature-checked) endpoints: channel webhooks, payment webhook,
MCP, lead-magnet minis and the embed script. Channel webhooks route every message through
inbound.route; SMS / phone / email accept X-Signature-Timestamp (required outside the practice workspace)."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .. import channels, console, mcp_server, tenancy
from ..deps import client_ip, get_tenant

router = APIRouter()  # tenant API (nothing here; everything below is public)
public_router = APIRouter()
MAX_BODY = 256 * 1024


async def _body(request: Request) -> bytes:
    body = await request.body()
    if len(body) > MAX_BODY:
        raise HTTPException(413, "Body too large")
    return body


def _json(body: bytes) -> dict:
    try:
        d = json.loads(body or b"{}")
    except ValueError:
        raise HTTPException(422, "Malformed JSON")
    if not isinstance(d, dict):
        raise HTTPException(422, "Expected a JSON object")
    return d


def _ts(request: Request) -> str | None:
    return request.headers.get("x-signature-timestamp")


def _ch(fn):
    from .. import inbound

    try:
        return fn()
    except channels.ChannelError as e:
        raise HTTPException(e.status, str(e))
    except console.WebhookError as e:
        raise HTTPException(e.status, str(e))
    except inbound.InboundError as e:
        raise HTTPException(e.status, str(e))


@public_router.post("/t/{tid}/hooks/telegram")
async def telegram(request: Request, tenant: tenancy.Tenant = Depends(get_tenant)):
    body = await _body(request)
    return _ch(lambda: channels.telegram(tenant, request.headers.get("x-telegram-bot-api-secret-token"), _json(body)))


@public_router.post("/t/{tid}/hooks/slack")
async def slack(request: Request, tenant: tenancy.Tenant = Depends(get_tenant)):
    body = await _body(request)
    return _ch(lambda: channels.slack(tenant, {k.lower(): v for k, v in request.headers.items()}, body, _json(body)))


@public_router.post("/t/{tid}/hooks/sms")
async def sms(request: Request, tenant: tenancy.Tenant = Depends(get_tenant)):
    body = await _body(request)
    return _ch(lambda: channels.sms(tenant, request.headers.get("x-signature"), body, _json(body), "sms", _ts(request)))


@public_router.post("/t/{tid}/hooks/phone")
async def phone(request: Request, tenant: tenancy.Tenant = Depends(get_tenant)):
    body = await _body(request)
    return _ch(lambda: channels.sms(tenant, request.headers.get("x-signature"), body, _json(body), "phone", _ts(request)))


@public_router.post("/t/{tid}/hooks/email")
async def email_in(request: Request, tenant: tenancy.Tenant = Depends(get_tenant)):
    body = await _body(request)
    return _ch(lambda: channels.inbound_email(tenant, request.headers.get("x-signature"), body, _ts(request)))


@public_router.post("/t/{tid}/hooks/payments")
async def payments(request: Request, tenant: tenancy.Tenant = Depends(get_tenant)):
    body = await _body(request)
    return _ch(lambda: console.payment_webhook(tenant, request.headers.get("x-signature"), body))


async def _mcp(request: Request, tid: str | None):
    t = mcp_server.tenant_for_token(request.headers.get("authorization"), tid)
    if t is None:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32001, "message": "Unauthorized"}},
                            status_code=401)
    body = await _body(request)
    try:
        req = json.loads(body)
    except ValueError:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
    if isinstance(req, list):
        out = [r for r in (mcp_server.handle(t, x) for x in req if isinstance(x, dict)) if r is not None]
        return JSONResponse(out)
    if not isinstance(req, dict):
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}})
    res = mcp_server.handle(t, req)
    return Response(status_code=202) if res is None else JSONResponse(res)


@public_router.post("/mcp")
async def mcp(request: Request):
    return await _mcp(request, None)


@public_router.post("/t/{tid}/mcp")
async def mcp_tenant(tid: str, request: Request):
    return await _mcp(request, tid)


MINI_HTML = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>body{{font-family:system-ui;margin:0;padding:16px;background:#f6f6f3;color:#16181d}}
.card{{background:#fff;border:1px solid #e4e3dd;border-radius:12px;padding:16px;max-width:420px;margin:auto}}
input,textarea,button{{font:inherit;width:100%;box-sizing:border-box;margin-top:8px;padding:8px;border:1px solid #d9d8d2;border-radius:8px}}
button{{background:#5b6cff;color:#fff;border:0;cursor:pointer}}.a{{white-space:pre-wrap;margin-top:12px}}small{{color:#5d6270}}</style></head>
<body><div class="card"><h2 style="margin:0 0 4px">{title}</h2><small>Answers come only from the programme's material. {limit} questions max.</small>
<div id="start"><input id="email" type="email" placeholder="Your email" aria-label="Email">
<label style="display:flex;gap:8px;align-items:flex-start;margin-top:8px"><input id="consent" type="checkbox" style="width:auto;margin:3px 0 0">
<small>{consent}</small></label><button id="go">Start</button></div>
<div id="chat" hidden><textarea id="q" rows="3" placeholder="Ask a question" aria-label="Question"></textarea><button id="ask">Ask</button>
<div class="a" id="a"></div><small id="left"></small></div><small id="err" style="color:#b91c1c"></small></div>
<script>
const base = "/m/{tid}/{slug}"; let tok = null;
async function call(p, b) {{ const r = await fetch(base + p, {{method:"POST", headers:{{"content-type":"application/json"}}, body: JSON.stringify(b)}});
  const d = await r.json(); if (!r.ok) throw new Error(d.detail || "Error"); return d; }}
document.getElementById("go").onclick = async () => {{ try {{ const d = await call("/start", {{email: email.value, consent: consent.checked}});
  tok = d.token; start.hidden = true; chat.hidden = false; err.textContent = ""; }} catch (e) {{ err.textContent = e.message; }} }};
document.getElementById("ask").onclick = async () => {{ try {{ const d = await call("/ask", {{token: tok, question: q.value}});
  a.textContent = d.answer + (d.citations.length ? "\\nSources: " + d.citations.map(c => c.title).join("; ") : "");
  left.textContent = d.remaining + " questions left"; }} catch (e) {{ err.textContent = e.message; }} }};
</script></body></html>"""


@public_router.get("/m/{tid}/{slug}", response_class=HTMLResponse)
def mini_page(tid: str, slug: str):
    try:
        t = tenancy.get(tid)
    except tenancy.TenantNotFound:
        raise HTTPException(404, "Not found")
    m = console.get_mini(t, slug)
    if m is None:
        raise HTTPException(404, "Not found")
    import html

    return MINI_HTML.format(title=html.escape(m["title"]), limit=m["question_limit"], consent=html.escape(m["consent_text"]),
                            tid=html.escape(tid), slug=html.escape(slug))


@public_router.post("/m/{tid}/{slug}/start")
async def mini_start(tid: str, slug: str, request: Request):
    t = get_tenant(tid)
    d = _json(await _body(request))
    return _ch(lambda: console.mini_start(t, slug, str(d.get("email", "")), bool(d.get("consent")), client_ip(request)))


@public_router.post("/m/{tid}/{slug}/ask")
async def mini_ask(tid: str, slug: str, request: Request):
    t = get_tenant(tid)
    d = _json(await _body(request))
    q = str(d.get("question", "")).strip()
    if len(q) < 2:
        raise HTTPException(422, "Ask a question")
    return _ch(lambda: console.mini_ask(t, slug, str(d.get("token", "")), q, client_ip(request)))


EMBED_JS = """(function(){var s=document.currentScript;var t=s.getAttribute('data-tenant'),m=s.getAttribute('data-mini');
var f=document.createElement('iframe');f.src=(s.src.replace(/\\/embed\\.js.*$/,''))+'/m/'+encodeURIComponent(t)+'/'+encodeURIComponent(m);
f.style.cssText='width:380px;height:560px;border:0;border-radius:12px;box-shadow:0 8px 30px rgba(0,0,0,.15)';
f.title='Assistant';s.parentNode.insertBefore(f,s.nextSibling);})();"""


@public_router.get("/embed.js")
def embed_js():
    return Response(EMBED_JS, media_type="application/javascript", headers={"Cache-Control": "no-cache"})


@public_router.post("/t/{tid}/hooks/voice")
async def voice_hook(request: Request, tenant: tenancy.Tenant = Depends(get_tenant)):
    import os

    body = await _body(request)
    from urllib.parse import parse_qsl

    params = dict(parse_qsl(body.decode("utf-8", errors="replace")))
    public = os.environ.get("OPERATOR_PUBLIC_URL", "").rstrip("/")
    url = f"{public}{request.url.path}" if public else str(request.url)
    xml = _ch(lambda: channels.voice_call(tenant, url, request.headers.get("x-twilio-signature"), params))
    return Response(xml, media_type="application/xml")
