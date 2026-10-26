"""Sign-in, session, identity and branding."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .. import auth, config, tenancy
from ..deps import COOKIE, Ctx, client_ip, get_ctx, get_tenant

router = APIRouter()


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)


class DemoLoginBody(BaseModel):
    role: str = Field(pattern="^(owner|coach|setter|closer|student)$")


def _set_cookie(resp: Response, tid: str, token: str) -> None:
    resp.set_cookie(COOKIE, token, path=f"/t/{tid}", httponly=True, samesite="lax",
                    secure=config.get().cookie_secure, max_age=config.get().session_hours * 3600)


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
    _set_cookie(response, tenant.id, token)
    return {"user": user}


@router.post("/auth/demo-login")
def demo_login(body: DemoLoginBody, request: Request, response: Response,
               tenant: tenancy.Tenant = Depends(get_tenant)):
    if not tenant.is_demo:
        raise HTTPException(403, "One-click roles exist only in the pitch demo workspace")
    from .. import audit, demo

    demo.ensure_seeded(tenant)
    with tenant.db() as c:
        u = c.execute("SELECT * FROM users WHERE username=?", (f"demo-{body.role}",)).fetchone()
    if u is None:
        raise HTTPException(500, "Demo users missing")
    token = auth.start_session(tenant, u["id"], client_ip(request))
    audit.log(tenant, u["username"], "login", {"demo": True, "role": body.role})
    _set_cookie(response, tenant.id, token)
    return {"user": auth.user_public(dict(u))}


@router.post("/auth/logout")
def logout(request: Request, response: Response, tenant: tenancy.Tenant = Depends(get_tenant)):
    auth.end_session(tenant, request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE, path=f"/t/{tenant.id}")
    return {"ok": True}


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
