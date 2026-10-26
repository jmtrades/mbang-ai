"""FastAPI dependencies: tenant resolution, session lookup, RBAC."""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request

from . import auth, tenancy

COOKIE = "op_s"


@dataclass
class Ctx:
    tenant: tenancy.Tenant
    user: dict
    ip: str

    @property
    def role(self) -> str:
        return self.user["role"]

    @property
    def actor(self) -> str:
        return f"{self.user['username']}({self.role})"


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def get_tenant(tid: str) -> tenancy.Tenant:
    try:
        return tenancy.get(tid)
    except tenancy.TenantNotFound:
        raise HTTPException(404, "Not found")


def get_ctx(request: Request, tenant: tenancy.Tenant = Depends(get_tenant)) -> Ctx:
    token = request.cookies.get(COOKIE)
    user = auth.session_user(tenant, token)
    if user is None:
        if token and _valid_elsewhere(tenant.id, token):
            # A session from another tenant never works here and never reveals anything.
            raise HTTPException(404, "Not found")
        raise HTTPException(401, "Sign in required")
    return Ctx(tenant=tenant, user=user, ip=client_ip(request))


def _valid_elsewhere(tid: str, token: str) -> bool:
    for t in tenancy.list_all():
        if t.id != tid and auth.session_user(t, token):
            return True
    return False


def require(tab: str):
    def dep(ctx: Ctx = Depends(get_ctx)) -> Ctx:
        if not auth.has_tab(ctx.role, tab):
            raise HTTPException(403, f"Your role can't use {tab}")
        return ctx

    return dep


def require_owner(ctx: Ctx = Depends(get_ctx)) -> Ctx:
    if ctx.role != "owner":
        raise HTTPException(403, "Owner only")
    return ctx
