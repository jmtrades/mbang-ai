"""Sign-in, session, identity and branding."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .. import auth, config, ratelimit, tenancy
from ..deps import COOKIE, Ctx, client_ip, get_ctx, get_tenant

router = APIRouter()


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)


class DemoLoginBody(BaseModel):
    role: str = Field(pattern="^(owner|coach|setter|closer|student)$")


def _set_cookie(resp: Response, tid: str, token: str, request: Request | None = None) -> None:
    """HttpOnly, SameSite=Lax, scoped to the tenant's path; Secure when configured or when the request
    itself came over HTTPS (behind the proxy, uvicorn's --proxy-headers sets the scheme)."""
    secure = config.get().cookie_secure or (request is not None and request.url.scheme == "https")
    resp.set_cookie(COOKIE, token, path=f"/t/{tid}", httponly=True, samesite="lax",
                    secure=secure, max_age=config.get().session_hours * 3600)


def _rotate(request: Request, tenant: tenancy.Tenant) -> bool:
    """A sign-in always starts a brand-new server-side session; any session the browser presented is ended
    first, so a token planted before sign-in is never upgraded. Returns True if one was ended."""
    old = request.cookies.get(COOKIE)
    if old and auth.session_user(tenant, old) is not None:
        auth.end_session(tenant, old)
        return True
    return False


@router.get("/public")
def public_info(tenant: tenancy.Tenant = Depends(get_tenant)):
    brand = tenant.get("brand", {}) or {}
    return {"tenant": tenant.id, "is_demo": tenant.is_demo, "brand": brand,
            "demo_tenant": "demo", "default_tenant": config.get().default_tenant}


@router.post("/auth/login")
def login(body: LoginBody, request: Request, response: Response, tenant: tenancy.Tenant = Depends(get_tenant)):
    try:
        res = auth.login(tenant, body.username, body.password, client_ip(request))
    except auth.Throttled:
        raise HTTPException(429, "Too many failed sign-ins. Try again in 15 minutes.")
    if res is None:
        raise HTTPException(401, "Wrong username or password")
    token, user = res
    _rotate(request, tenant)
    _set_cookie(response, tenant.id, token, request)
    return {"user": user}


@router.post("/auth/demo-login")
def demo_login(body: DemoLoginBody, request: Request, response: Response,
               tenant: tenancy.Tenant = Depends(get_tenant)):
    if not tenant.is_demo:
        raise HTTPException(403, "One-click roles exist only in the pitch demo workspace")
    from .. import audit, demo

    # switching roles with a live demo session swaps it one for one; only brand-new sessions count
    # against the per-IP limit (anyone on the internet can call this)
    if not _rotate(request, tenant):
        ratelimit.hit("demo-login", client_ip(request), ratelimit.limit("demo_login", 20), 600,
                      "Too many demo sign-ins from this network. Try again in a few minutes.")
    demo.ensure_seeded(tenant)
    with tenant.db() as c:
        u = c.execute("SELECT * FROM users WHERE username=?", (f"demo-{body.role}",)).fetchone()
    if u is None:
        raise HTTPException(500, "Demo users missing")
    token = auth.start_session(tenant, u["id"], client_ip(request))
    audit.log(tenant, u["username"], "login", {"demo": True, "role": body.role})
    _set_cookie(response, tenant.id, token, request)
    return {"user": auth.user_public(dict(u))}


@router.post("/auth/logout")
def logout(request: Request, response: Response, tenant: tenancy.Tenant = Depends(get_tenant)):
    auth.end_session(tenant, request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE, path=f"/t/{tenant.id}")
    return {"ok": True}


@router.get("/auth/session")
def session(request: Request, tenant: tenancy.Tenant = Depends(get_tenant)):
    """Always 200: the app's first call, so a signed-out visit doesn't log a 401."""
    user = auth.session_user(tenant, request.cookies.get(COOKIE))
    if user is None:
        return {"signed_in": False}
    return {"signed_in": True, **me(Ctx(tenant=tenant, user=user, ip=client_ip(request)))}


@router.get("/me")
def me(ctx: Ctx = Depends(get_ctx)):
    from .. import llm

    return {
        "user": ctx.user,
        "tabs": auth.tabs_for(ctx.role),
        "tenant": ctx.tenant.to_dict(),
        "brand": ctx.tenant.get("brand", {}),
        "is_demo": ctx.tenant.is_demo,
        "ai": llm.state(ctx.tenant),
        "can_decide": sorted(auth.ROLE_DECIDE.get(ctx.role, set())) if ctx.role != "owner" else ["*"],
    }


@router.post("/demo/reset")
def demo_reset(ctx: Ctx = Depends(get_ctx)):
    from .. import demo

    if ctx.role != "owner":
        raise HTTPException(403, "Owner only")
    if not tenancy.is_demo_db(ctx.tenant):
        raise HTTPException(403, "Refused: demo resets only run against the demo database, never the real workspace.")
    # a reset re-seeds the whole demo (and backs it up first): a few per network per hour, and a global cap
    ratelimit.hit("demo-reset", ctx.ip, ratelimit.limit("demo_reset", 3), 3600,
                  "The demo was reset recently from this network. Try again later.")
    ratelimit.hit("demo-reset-all", "*", ratelimit.limit("demo_reset_all", 12), 3600,
                  "The demo has been reset many times this hour. Try again later.")
    try:
        return demo.reset(ctx.tenant, ctx.actor)
    except demo.DemoResetRefused as e:
        raise HTTPException(403, str(e))
