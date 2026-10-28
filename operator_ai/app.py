"""FastAPI application: health, readiness, tenant-scoped API and the built web app."""
from __future__ import annotations

import importlib
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from . import config, ratelimit, tenancy
from .errors import InputError
from .redact import redact

log = logging.getLogger("operator")
WEB_DIST = config.ROOT / "web" / "dist"
STARTED = time.time()

ROUTE_MODULES = [
    "core", "search", "queue", "agents", "sales", "students", "clients", "studio", "ads", "outreach",
    "builder", "platform", "owner", "channels", "live", "reports",
    "academy", "missions", "prospects", "playbooks", "team", "setup",
    "roleplay", "persona", "delivery", "inbound", "webinars",
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
_TENANT_API = re.compile(r"^/t/[^/]+/api/")
# responses the scrub never rewrites: the screens that manage the banned list and the persona's banned
# words must show (and save back) exactly what the owner typed
_SCRUB_EXEMPT = re.compile(r"/api/(settings|personality)$")
_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


class _NonFinite(ValueError):
    pass


def _reject_constant(name: str):
    raise _NonFinite(name)


def _origin_netloc(request: Request) -> str | None:
    """host[:port] of the page that sent the request (Origin, else Referer), or None when neither is sent."""
    for h in ("origin", "referer"):
        v = request.headers.get(h)
        if v:
            if v == "null":
                return "null"
            try:
                return urlsplit(v).netloc.lower() or "null"
            except ValueError:
                return "null"
    return None


def _allowed_netlocs(request: Request) -> set[str]:
    out = {(request.headers.get("host") or "").lower()}
    fwd = request.headers.get("x-forwarded-host")
    if fwd:
        out.add(fwd.split(",")[0].strip().lower())
    for v in [os.environ.get("OPERATOR_PUBLIC_URL", "")] + os.environ.get("OPERATOR_ALLOWED_ORIGINS", "").split(","):
        v = v.strip()
        if v:
            out.add(urlsplit(v).netloc.lower())
    return {x for x in out if x}


def create_app(start_workers: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        tenancy.ensure_defaults()
        try:  # crash recovery: runs left "running" by a restart become "interrupted" (escalations.recover_runs)
            from . import escalations

            for t in tenancy.list_all():
                escalations.recover_runs(t)
        except Exception as e:  # noqa: BLE001 - recovery must never stop the app from starting
            log.warning("run recovery skipped: %s", redact(type(e).__name__))
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
        """Safety net: banned names are scrubbed from every tenant API response. The scrub walks the decoded
        JSON and rewrites string values only, so a banned word that equals a key or a JSON literal ("status",
        "false") can never corrupt a response. The settings and personality endpoints that manage the lists
        are exempt, so what the owner saved round-trips unchanged."""
        response = await call_next(request)
        m = _TENANT_PATH.match(request.url.path)
        if not m or _SCRUB_EXEMPT.search(request.url.path) or "json" not in response.headers.get("content-type", ""):
            return response
        from . import guardrails

        names = guardrails.banned_cached(m.group(1))
        if not names:
            return response
        body = b"".join([chunk async for chunk in response.body_iterator])
        headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        try:
            data = json.loads(body)
        except ValueError:
            return Response(body, status_code=response.status_code, headers=headers, media_type="application/json")
        out = json.dumps(guardrails.scrub_json(data, names), ensure_ascii=False, separators=(",", ":"))
        return Response(out, status_code=response.status_code, headers=headers, media_type="application/json")

    @app.middleware("http")
    async def same_origin_writes(request: Request, call_next):
        """CSRF defence in depth (the session cookie is already SameSite=Lax and HttpOnly): a state-changing
        API call sent by a page on another site is refused. Requests with no Origin/Referer (server-to-server,
        scripts, tests) are judged by the cookie alone."""
        if request.method not in _SAFE_METHODS and _TENANT_API.match(request.url.path):
            src = _origin_netloc(request)
            if src is not None and src not in _allowed_netlocs(request):
                return JSONResponse({"detail": "Refused: this request came from another website."}, status_code=403)
        return await call_next(request)

    @app.middleware("http")
    async def reject_non_finite_json(request: Request, call_next):
        """Python's JSON parser accepts NaN and Infinity; the app never does (they turn into 500s later)."""
        if request.method in ("POST", "PUT", "PATCH") and "json" in request.headers.get("content-type", ""):
            body = await request.body()
            if b"NaN" in body or b"Infinity" in body:
                try:
                    json.loads(body, parse_constant=_reject_constant)
                except _NonFinite:
                    return JSONResponse({"detail": "Numbers must be finite: NaN and Infinity aren't allowed."},
                                        status_code=422)
                except ValueError:
                    pass  # malformed JSON: the route answers 422 itself
        return await call_next(request)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        if request.url.path.startswith("/t/"):
            # the app itself is never framed by another site (the public minis under /m/ are embeddable)
            response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        if _TENANT_API.match(request.url.path):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(InputError)
    async def _input_error(request: Request, exc: InputError):
        return JSONResponse({"detail": redact(str(exc))}, status_code=exc.status)

    @app.exception_handler(ratelimit.Limited)
    async def _limited(request: Request, exc: ratelimit.Limited):
        return JSONResponse({"detail": str(exc)}, status_code=429, headers={"Retry-After": str(exc.retry_after)})

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
        from . import __version__

        # the commit is stamped at build time (deploy/Dockerfile, CI); "unknown" in a plain checkout
        return {"ok": True, "version": __version__, "commit": os.environ.get("OPERATOR_COMMIT", "unknown")[:40],
                "uptime_s": round(time.time() - STARTED, 1)}

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
        root = WEB_DIST.resolve()
        f = (WEB_DIST / path).resolve()
        # is_relative_to, not a string prefix: "../dist-old/x" must not pass as inside "dist"
        if WEB_DIST.exists() and f.is_relative_to(root) and f != root and f.is_file():
            headers = {"Cache-Control": "no-cache"} if f.name in ("sw.js", "manifest.webmanifest") else {}
            return FileResponse(f, headers=headers)
        raise HTTPException(404, "Not found")

    return app


app = create_app()
