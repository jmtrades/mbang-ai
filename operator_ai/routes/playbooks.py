"""2026 Playbooks: researched, dated, sourced public best practice (not the course material).

GET /playbooks                      domains + counts + switch state + researched dates (every role with the tab)
GET /playbooks/search?q=            ranked entries X1..Xn
GET /playbooks/domain/{domain}      one domain's entries, open questions and note
GET /playbooks/entry/{entry_id}     one entry: steps, do/don't, benchmarks, sources, as_of, confidence

The switch is the tenant setting ``third_party_kb`` (default off; the demo turns it on). The owner turns
it on or off with the existing settings API (PUT /settings {"key": "third_party_kb", "value": true}),
which is owner-only and audited. While it is off, only the owner may browse (to decide); other roles
get the overview with ``can_browse: false`` and a 403 from the content endpoints.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from .. import playbooks
from ..deps import Ctx, require

router = APIRouter()
OFF_MSG = "The 2026 Playbooks are switched off for this workspace. Your programme owner can turn them on."


def _can_browse(ctx: Ctx) -> bool:
    return ctx.role == "owner" or playbooks.enabled(ctx.tenant)


def _gate(ctx: Ctx) -> None:
    if not _can_browse(ctx):
        raise HTTPException(403, OFF_MSG)


@router.get("/playbooks")
def overview(ctx: Ctx = Depends(require("playbooks"))):
    ov = playbooks.overview(ctx.tenant)
    is_owner = ctx.role == "owner"
    ov.update({
        "can_browse": _can_browse(ctx),
        "can_toggle": is_owner,
        "toggle": {"method": "PUT", "path": "/settings", "key": playbooks.SWITCH} if is_owner else None,
        "off_message": None if ov["enabled"] else (
            "Off: coach answers don't use these. You're previewing the library as the owner." if is_owner else OFF_MSG),
    })
    if not is_owner:
        ov["problems"] = []  # loader notes are for the owner
    if not ov["can_browse"]:
        # switched off: other roles get the state only, not the library's domains, notes or categories
        ov.update({"domains": [], "total": 0, "researched": [], "latest_researched": None,
                   "confidence": {c: 0 for c in playbooks.CONFIDENCE}})
    return ov


@router.get("/playbooks/search")
def search(q: str = Query("", max_length=300), k: int = Query(12, ge=1, le=30),
           domain: str | None = Query(None, max_length=96), ctx: Ctx = Depends(require("playbooks"))):
    _gate(ctx)
    q = q.strip()
    if len(q) < 2:
        return {"q": q, "items": [], "hint": "Type at least two characters to search the playbooks."}
    items = playbooks.search(ctx.tenant, q, k=k, respect_switch=False, domain=domain or None)
    return {"q": q, "items": items, "external": True, "library": playbooks.LIBRARY_NAME,
            "hint": None if items else "No playbook entry matches that. Try fewer or different words."}


@router.get("/playbooks/domain/{domain}")
def domain_detail(domain: str, ctx: Ctx = Depends(require("playbooks"))):
    _gate(ctx)
    d = playbooks.domain_detail(domain, ctx.tenant)
    if d is None:
        raise HTTPException(404, "No such playbook domain")
    return d


@router.get("/playbooks/entry/{entry_id}")
def entry(entry_id: str, ctx: Ctx = Depends(require("playbooks"))):
    _gate(ctx)
    e = playbooks.get(entry_id, ctx.tenant)
    if e is None:
        raise HTTPException(404, "No such playbook entry")
    return e
