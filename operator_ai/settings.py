"""Tenant settings. The API may change ordinary settings; BOX_ONLY settings (live switches,
the Meta allowlist and both AI budgets) change only through scripts/tenant_admin.py with a
recorded owner yes, or the server environment."""
from __future__ import annotations

from . import audit
from .config import BOX_ONLY

API_EDITABLE = {
    "brand", "persona", "banned_names", "blocked_attendees", "lead_weights", "lead_routes", "show_ladder",
    "outreach_daily_cap", "revenue_goal", "third_party_kb", "coach_assign", "retention_weights", "builder_note",
}


class BoxOnly(Exception):
    pass


def update_from_api(tenant, key: str, value, actor: str) -> None:
    if key in BOX_ONLY:
        raise BoxOnly(f"{key} can only be changed on the server (scripts/tenant_admin.py)")
    if key not in API_EDITABLE:
        raise KeyError(key)
    tenant.set(key, value)
    audit.log(tenant, actor, "settings", {"key": key})


def set_box(tenant, key: str, value, reason: str, actor: str = "server-cli") -> None:
    """Server-side only. Turning a switch on needs a recorded owner yes; turning it off needs nothing."""
    if key not in BOX_ONLY:
        raise KeyError(key)
    turning_on = bool(value) and value != [] and value != 0
    if turning_on and not (reason or "").strip():
        raise ValueError("Turning this on needs --owner-yes \"<recorded reason>\"")
    old = tenant.get(key)
    tenant.set(key, value)
    detail = {"key": key, "from": _summ(old), "to": _summ(value)}
    if turning_on:
        detail["owner_yes"] = reason
    audit.log(tenant, actor, "switch", detail)


def _summ(v):
    if isinstance(v, list):
        return f"{len(v)} entries"
    return v


def public_view(tenant) -> dict:
    s = tenant.all_settings()
    out = {k: s.get(k) for k in sorted(API_EDITABLE) if k in s}
    out["server_only"] = {k: s.get(k) if k != "meta_allowlist" else len(s.get(k) or []) for k in sorted(BOX_ONLY)}
    return out
