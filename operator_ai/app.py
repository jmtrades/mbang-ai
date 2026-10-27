"""FastAPI application: health, readiness, tenant-scoped API and the built web app."""
from __future__ import annotations

import importlib
import logging
import re
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from . import config, tenancy
from .redact import redact

log = logging.getLogger("operator")
WEB_DIST = config.ROOT / "web" / "dist"
STARTED = time.time()

ROUTE_MODULES = [
    "core", "search", "queue", "agents", "sales", "students", "clients", "studio", "ads", "outreach",
    "builder", "platform", "owner", "channels", "live", "reports",
    "academy", "missions", "prospects", "playbooks", "team", "setup",
]


def _load_routers(app: FastAPI) -> None:
    for name in ROUTE_MODULES:
        try:
            mod = importlib.import_module(f".routes.{name}", __package__)
        except ModuleNotFoundError as e:
            if e.name and e.name.endswith(f"routes.{name}"):
                continue
            raise
        app.include_router(mod.router, prefix="/t/{tid}/api")
        extra = getattr(mod, "public_router", None)
        if extra is not None:
            app.include_router(extra)


_TENANT_PATH = re.compile(r"^/t/([^/]+)/(api|report)")


def create_app(start_workers: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        tenancy.ensure_defaults()
        if start_workers:
            from . import index, workers

            index.warm_async()
            workers.start()
        try:
            yield
        finally:
            if start_workers:
                from . import workers

                workers.stop()

    app = FastAPI(title="Operator AI", docs_url=None, redoc_url=None, openapi_url="/openapi.json", lifespan=lifespan)

    @app.middleware("http")
    async def scrub_banned_names(request: Request, call_next):
        """Safety net: banned names are scrubbed from every tenant API response (the settings
        endpoint that manages the list is the one exception)."""
        response = await call_next(request)
        m = _TENANT_PATH.match(request.url.path)
        if not m or request.url.path.endswith("/api/settings") or "json" not in response.headers.get("content-type", ""):
            return response
        from . import guardrails

        names = guardrails.banned_cached(m.group(1))
        if not names:
            return response
        body = b"".join([chunk async for chunk in response.body_iterator])
        text = guardrails.scrub(body.decode("utf-8", errors="replace"), names)
        headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        return Response(text, status_code=response.status_code, headers=headers, media_type="application/json")

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        errs = [{"loc": list(e.get("loc", [])), "msg": redact(e.get("msg", ""))} for e in exc.errors()]
        return JSONResponse({"detail": errs}, status_code=422)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        log.exception("unhandled: %s", redact(exc))
        return JSONResponse({"detail": "Something went wrong. It has been logged.", "error": redact(type(exc).__name__)},
                            status_code=500)

    @app.get("/health")
    def health():
        return {"ok": True, "uptime_s": round(time.time() - STARTED, 1)}

    @app.get("/ready")
    def ready():
        from . import readiness

        return readiness.ready()

    _load_routers(app)

    # ---- web app -------------------------------------------------------------
    def _index() -> Response:
        idx = WEB_DIST / "index.html"
        if not idx.exists():
            return JSONResponse({"detail": "Web app not built. Run: cd web && npm run build"}, status_code=503)
        return FileResponse(idx, headers={"Cache-Control": "no-cache"})

    @app.get("/")
    def root():
        return RedirectResponse(f"/t/{config.get().default_tenant}/")

    @app.get("/t/{tid}")
    def tenant_root_noslash(tid: str):
        return RedirectResponse(f"/t/{tid}/")

    @app.get("/t/{tid}/")
    def tenant_root(tid: str):
        try:
            tenancy.get(tid)
        except tenancy.TenantNotFound:
            raise HTTPException(404, "Not found")
        return _index()

    @app.get("/{path:path}")
    def static(path: str):
        if path.startswith(("t/", "api/", "m/")):
            raise HTTPException(404, "Not found")
        f = (WEB_DIST / path).resolve()
        if WEB_DIST.exists() and str(f).startswith(str(WEB_DIST.resolve())) and f.is_file():
            headers = {"Cache-Control": "no-cache"} if f.name in ("sw.js", "manifest.webmanifest") else {}
            return FileResponse(f, headers=headers)
        raise HTTPException(404, "Not found")

    return app


app = create_app()
