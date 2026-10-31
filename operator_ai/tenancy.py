"""Tenant registry, per-tenant databases and settings."""
from __future__ import annotations

import os
import re
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path

from . import config, db

KB_SCOPES = ("course", "empty", "own")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")
_lock = threading.Lock()


class LicenceError(Exception):
    """Raised when a non-owner tenant asks for the course knowledge base."""


class TenantNotFound(Exception):
    pass


@dataclass
class Tenant:
    id: str
    name: str
    kb_scope: str
    kind: str  # owner | client | demo
    is_demo: bool

    @property
    def dir(self) -> Path:
        return config.get().tenants_dir / self.id

    @property
    def db_path(self) -> Path:
        if self.is_demo:
            return config.get().demo_db_path
        return self.dir / "tenant.db"

    @property
    def kb_dir(self) -> Path:
        return self.dir / "kb"

    def db(self):
        return db.open_db(self.db_path)

    # settings -------------------------------------------------------------
    def get(self, key: str, default=None):
        with self.db() as c:
            r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if r is None:
            return default
        return db.jload(r["value"], default)

    def set(self, key: str, value) -> None:
        with self.db() as c:
            c.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, db.jdump(value)),
            )

    def all_settings(self) -> dict:
        with self.db() as c:
            return {r["key"]: db.jload(r["value"]) for r in c.execute("SELECT key,value FROM settings")}

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "kb_scope": self.kb_scope, "kind": self.kind,
                "is_demo": self.is_demo}


def _registry():
    cfg = config.get()
    cfg.tenants_dir.mkdir(parents=True, exist_ok=True)
    db.init_registry(cfg.registry_path)
    return db.connect(cfg.registry_path)


def _from_row(r) -> Tenant:
    return Tenant(id=r["id"], name=r["name"], kb_scope=r["kb_scope"], kind=r["kind"], is_demo=bool(r["is_demo"]))


_cache: dict[tuple[str, str], Tenant] = {}


def get(tid: str) -> Tenant:
    # a tenant id comes straight from the URL: anything that isn't a valid id is simply not found
    if not isinstance(tid, str) or not _ID_RE.match(tid):
        raise TenantNotFound(str(tid)[:40])
    key = (str(config.get().data_dir), tid)
    if key in _cache:
        return _cache[key]
    c = _registry()
    try:
        r = c.execute("SELECT * FROM tenants WHERE id=?", (tid,)).fetchone()
    finally:
        c.close()
    if r is None:
        raise TenantNotFound(tid)
    t = _from_row(r)
    _cache[key] = t
    return t


def list_all() -> list[Tenant]:
    c = _registry()
    try:
        return [_from_row(r) for r in c.execute("SELECT * FROM tenants ORDER BY created")]
    finally:
        c.close()


def clear_cache() -> None:
    _cache.clear()


def check_licence(kind: str, kb_scope: str) -> None:
    if kb_scope not in KB_SCOPES:
        raise ValueError(f"unknown kb_scope {kb_scope}")
    if kb_scope == "course" and kind not in ("owner", "demo"):
        raise LicenceError("The course knowledge base belongs to the programme owner and cannot be provisioned to a client tenant.")


def _write_secret_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(content)
    os.chmod(path, 0o600)


def create(tid: str, name: str, kb_scope: str = "empty", kind: str = "client") -> tuple[Tenant, Path | None]:
    """Provision a tenant. Returns the tenant and the path of the mode-600 file holding
    the generated owner password (never printed)."""
    from . import auth, audit

    if not _ID_RE.match(tid):
        raise ValueError("tenant id must be 2-32 chars of a-z, 0-9, _ or -")
    check_licence(kind, kb_scope)
    with _lock:
        c = _registry()
        try:
            if c.execute("SELECT 1 FROM tenants WHERE id=?", (tid,)).fetchone():
                raise ValueError(f"tenant {tid} exists")
            c.execute("INSERT INTO tenants(id,name,kb_scope,kind,created,is_demo) VALUES(?,?,?,?,?,?)",
                      (tid, name, kb_scope, kind, db.now(), 1 if kind == "demo" else 0))
        finally:
            c.close()
    clear_cache()
    t = get(tid)
    t.dir.mkdir(parents=True, exist_ok=True)
    t.kb_dir.mkdir(parents=True, exist_ok=True)
    db.init_tenant_db(t.db_path)
    t.set("brand", {"name": config.get().brand, "primary": "#c9a44c", "accent": "#e7cf8a",
                    "owner_display": config.get().owner_display, "logo": ""})
    t.set("is_demo", t.is_demo)
    if kind == "client":
        t.set("ai_token_budget", 0)
        t.set("critique_token_budget", 0)
    if kind == "demo":
        ensure_demo_budget(t)
    t.set("live_meta_enabled", False)
    t.set("meta_allowlist", [])
    t.set("live_calendar_enabled", False)
    t.set("live_channels_enabled", False)
    pw_path = None
    if kind != "demo":
        pw = secrets.token_urlsafe(14)
        auth.create_user(t, "owner", pw, "owner", display="Owner")
        pw_path = t.dir / "owner_password.txt"
        _write_secret_file(pw_path, pw + "\n")
    audit.log(t, "system", "tenant.create", {"id": tid, "kb_scope": kb_scope, "kind": kind})
    return t, pw_path


def set_kb_scope(tid: str, kb_scope: str, actor: str = "system") -> Tenant:
    from . import audit

    t = get(tid)
    check_licence(t.kind, kb_scope)
    c = _registry()
    try:
        c.execute("UPDATE tenants SET kb_scope=? WHERE id=?", (kb_scope, tid))
    finally:
        c.close()
    clear_cache()
    t2 = get(tid)
    audit.log(t2, actor, "kb.scope", {"from": t.kb_scope, "to": kb_scope})
    return t2


def ensure_defaults() -> None:
    """Make sure the owner (real) workspace and the separate demo workspace exist."""
    cfg = config.get()
    existing = {t.id for t in list_all()}
    if cfg.default_tenant not in existing:
        create(cfg.default_tenant, f"{cfg.brand} programme", kb_scope="course", kind="owner")
    if "demo" not in existing:
        create("demo", f"{cfg.brand} pitch demo", kb_scope="course", kind="demo")
    else:
        ensure_demo_budget(get("demo"))  # demo workspaces created before the demo had its own budget


# The public demo never inherits the server-wide AI budget: it gets its own small one, capped by the server
# default (no AI on the server -> none in the demo). Its *_used counters survive every reset, so resetting
# the demo never refills the budget; only the server CLI can (scripts/tenant_admin.py budget demo ...).
DEMO_AI_BUDGET_CAP = 20_000
DEMO_CRITIQUE_BUDGET_CAP = 5_000


def demo_budgets() -> dict[str, int]:
    cfg = config.get()

    def cap(env: str, default: int) -> int:
        try:
            return max(0, int(os.environ.get(env, default)))
        except ValueError:
            return default
    return {"ai_token_budget": max(0, min(cfg.ai_token_budget_default, cap("OPERATOR_DEMO_AI_TOKEN_BUDGET",
                                                                          DEMO_AI_BUDGET_CAP))),
            "critique_token_budget": max(0, min(cfg.critique_token_budget_default,
                                                cap("OPERATOR_DEMO_CRITIQUE_TOKEN_BUDGET", DEMO_CRITIQUE_BUDGET_CAP)))}


def ensure_demo_budget(t: Tenant) -> None:
    """Pin the demo's budgets once (never overwrite what the server CLI set later)."""
    if not t.is_demo:
        return
    for k, v in demo_budgets().items():
        if t.get(k) is None:
            t.set(k, v)


def is_demo_db(t: Tenant) -> bool:
    """A reset may only touch the demo DB: both the registry flag and the file path must agree,
    and the database itself must carry the demo marker."""
    if not t.is_demo:
        return False
    if t.db_path.resolve() != config.get().demo_db_path.resolve():
        return False
    return bool(t.get("is_demo", False))
