"""Tenant settings. The API may change ordinary settings; BOX_ONLY settings (live switches,
the Meta allowlist and both AI budgets) change only through scripts/tenant_admin.py with a
recorded owner yes, or the server environment.

Every API-editable setting has a shape. A value of the wrong shape is refused with InvalidSetting (the API
answers 422) instead of being stored, so one bad value can never break the response scrub or a page that
reads it."""
from __future__ import annotations

import json
import math
import re

from . import audit
from .config import BOX_ONLY
from .errors import InputError

# "persona" is not here: display name, banned words and limits change through /personality, and the persona itself
# (subject, consent, phrases) only through /persona, where the subject signs for themselves.
API_EDITABLE = {
    "brand", "banned_names", "blocked_attendees", "lead_weights", "lead_routes", "show_ladder",
    "outreach_daily_cap", "revenue_goal", "third_party_kb", "coach_assign", "retention_weights", "builder_note",
}
MAX_JSON = 20_000  # bytes of JSON for any one setting


class BoxOnly(Exception):
    pass


class InvalidSetting(InputError):
    pass


def _bad(key: str, why: str):
    raise InvalidSetting(f"{key}: {why}")


def _finite(key: str, v, lo: float | None = None, hi: float | None = None) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        _bad(key, "must be a finite number")
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        _bad(key, f"must be between {lo} and {hi}")
    return v


def _text(key: str, v, max_len: int, min_len: int = 0) -> str:
    if not isinstance(v, str):
        _bad(key, "must be text")
    v = " ".join(v.split())
    if not (min_len <= len(v) <= max_len):
        _bad(key, f"must be {min_len}-{max_len} characters")
    return v


def _str_list(key: str, v, max_items: int, min_len: int, max_len: int) -> list[str]:
    if not isinstance(v, list):
        _bad(key, "must be a list of text values")
    if len(v) > max_items:
        _bad(key, f"at most {max_items} entries")
    out = []
    for x in v:
        s = _text(key, x, max_len, min_len)
        if s and s not in out:
            out.append(s)
    return out


_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_BRAND_TEXT = {"name": 80, "owner_display": 80, "tagline": 160, "support_email": 200}


def _brand(key: str, v) -> dict:
    if not isinstance(v, dict):
        _bad(key, "must be an object")
    out = {}
    for k, x in v.items():
        if not isinstance(k, str) or len(k) > 40:
            _bad(key, "unknown field")
        if k in ("primary", "accent"):
            if not isinstance(x, str) or not _HEX.match(x):
                _bad(key, f"{k} must be a colour like #5b6cff")
            out[k] = x
        elif k == "logo":
            if not isinstance(x, str) or len(x) > 500 or (x and not re.match(r"^(https://|/)", x)):
                _bad(key, "logo must be empty, an https:// address or a path on this site")
            out[k] = x
        elif k in _BRAND_TEXT:
            out[k] = _text(key, x, _BRAND_TEXT[k], 1 if k == "name" else 0)
        elif isinstance(x, (str, bool)) or (isinstance(x, (int, float)) and math.isfinite(x)):
            out[k] = x[:200] if isinstance(x, str) else x  # extra display fields other features may add
        else:
            _bad(key, f"{k} must be text, a number or on/off")
    return out


def _persona(key: str, v) -> dict:
    if not isinstance(v, dict):
        _bad(key, "must be an object")
    out = dict(v)
    if "display_name" in v:
        out["display_name"] = _text(key, v["display_name"], 60)
    if "daily_question_limit" in v:
        out["daily_question_limit"] = int(_finite(key, v["daily_question_limit"], 1, 1000))
    if "banned_words" in v:
        out["banned_words"] = _str_list(key, v["banned_words"], 100, 2, 60)
    if "style" in v and not isinstance(v["style"], str):
        _bad(key, "style must be text")
    if v.get("persona_consent") is not None and not isinstance(v["persona_consent"], dict):
        _bad(key, "persona_consent must be an object")
    return out


def _bool(key: str, v) -> bool:
    if not isinstance(v, bool):
        _bad(key, "must be true or false")
    return v


def _number_map(key: str, v, depth: int) -> dict:
    if not isinstance(v, dict) or len(v) > 50:
        _bad(key, "must be an object of numbers")
    out = {}
    for k, x in v.items():
        if not isinstance(k, str) or len(k) > 60:
            _bad(key, "keys must be short text")
        out[k] = _number_map(key, x, depth - 1) if depth > 1 else _finite(key, x, -1000, 1000)
    return out


def _json_safe(key: str, v):
    try:
        s = json.dumps(v, allow_nan=False)
    except (TypeError, ValueError):
        _bad(key, "must be plain JSON with finite numbers")
    if len(s) > MAX_JSON:
        _bad(key, "is too large")
    return v


VALIDATORS = {
    "brand": _brand,
    "persona": _persona,
    "banned_names": lambda k, v: _str_list(k, v, 200, 2, 80),
    "blocked_attendees": lambda k, v: _str_list(k, v, 1000, 2, 200),
    "lead_weights": lambda k, v: _number_map(k, v, 2),
    "retention_weights": lambda k, v: _number_map(k, v, 1),
    "outreach_daily_cap": lambda k, v: int(_finite(k, v, 0, 1000)),
    "revenue_goal": lambda k, v: _finite(k, v, 0, 1_000_000_000),
    "third_party_kb": _bool,
    "builder_note": lambda k, v: _text(k, v, 2000),
}


def validate(key: str, value):
    """The cleaned value for an API-editable setting, or InvalidSetting. Keys without a dedicated shape still
    have to be plain, finite, size-limited JSON."""
    fn = VALIDATORS.get(key)
    value = fn(key, value) if fn else value
    return _json_safe(key, value)


def update_from_api(tenant, key: str, value, actor: str) -> None:
    if key in BOX_ONLY:
        raise BoxOnly(f"{key} can only be changed on the server (scripts/tenant_admin.py)")
    if key not in API_EDITABLE:
        raise KeyError(key)
    value = validate(key, value)
    tenant.set(key, value)
    if key in ("banned_names", "persona"):
        from . import guardrails

        guardrails.banned_cache_clear(tenant.id)
    detail = {"key": key}
    if isinstance(value, bool):
        detail["to"] = value  # on/off switches (third_party_kb) record which way they were turned
    audit.log(tenant, actor, "settings", detail)


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
