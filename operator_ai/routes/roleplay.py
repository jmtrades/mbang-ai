"""Role-play simulator API: /academy/scenarios... and /academy/roleplays... (all need the academy tab).

Learners start, play, end and drill their own sessions. Coaches see their students' sessions and sign them
off; the owner sees everything, edits scenarios and rubric rules, and runs the citation validator.
"""
from __future__ import annotations

import re
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .. import guardrails
from ..deps import Ctx, require
from ..roleplay import rubric, scenarios, score, sessions

router = APIRouter()
KEY = r"^[a-z0-9_]{3,40}$"


def _owner(ctx: Ctx) -> None:
    if ctx.role != "owner":
        raise HTTPException(403, "Only the owner can change this")


def _call(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except sessions.NotVisible:
        raise HTTPException(404, "Not found")
    except sessions.Forbidden as e:
        raise HTTPException(403, str(e))
    except sessions.Conflict as e:
        raise HTTPException(409, str(e))
    except scenarios.ScenarioError as e:
        raise HTTPException(422, [{"loc": ["body", "data"], "msg": m} for m in e.errors])


# --------------------------------------------------------------------------- scenarios
@router.get("/academy/scenarios")
def list_scenarios(ctx: Ctx = Depends(require("academy"))):
    items = [scenarios.public(s, ctx.role) for s in scenarios.all_(ctx.tenant)]
    items.sort(key=lambda s: (not s["recommended"], s["difficulty"], s["title"]))
    return {"items": items, "skills": scenarios.SKILLS, "can_edit": ctx.role == "owner",
            "can_test": ctx.role in ("owner", "coach")}


@router.get("/academy/scenarios/{key}")
def get_scenario(key: str, ctx: Ctx = Depends(require("academy"))):
    s = scenarios.get(ctx.tenant, key)
    if s is None or (not s.get("_active", True) and ctx.role != "owner"):
        raise HTTPException(404, "Not found")
    out = scenarios.public(s, ctx.role)
    if ctx.role == "owner":
        out["data"] = {k: v for k, v in s.items() if not k.startswith("_")}
        out["active"] = s.get("_active", True)
        out["builtin"] = key in scenarios.BUILTIN_KEYS
    return out


MAX_SCENARIO_BYTES = 60_000


def _check_size(data: dict | None) -> None:
    import json

    if data is not None and len(json.dumps(data, default=str)) > MAX_SCENARIO_BYTES:
        raise HTTPException(422, [{"loc": ["body", "data"], "msg": "The scenario is too large (60 KB at most)"}])


class ScenarioBody(BaseModel):
    data: dict[str, Any]
    active: bool = True


@router.put("/academy/scenarios/{key}")
def put_scenario(key: str, b: ScenarioBody, ctx: Ctx = Depends(require("academy"))):
    _owner(ctx)
    if not re.match(KEY, key):
        raise HTTPException(422, [{"loc": ["path", "key"], "msg": "key: 3-40 characters of a-z, 0-9 or _"}])
    _check_size(b.data)
    s = _call(scenarios.upsert, ctx.tenant, key, b.data, ctx.actor, b.active)
    return {"ok": True, "scenario": scenarios.public(s, ctx.role), "version": s["_version"], "source": s["_source"]}


@router.post("/academy/scenarios/{key}/reset")
def reset_scenario(key: str, ctx: Ctx = Depends(require("academy"))):
    _owner(ctx)
    base = scenarios.builtin(key)
    if base is None:
        raise HTTPException(404, "Only built-in scenarios can be reset")
    s = _call(scenarios.upsert, ctx.tenant, key, base, ctx.actor, True)
    return {"ok": True, "version": s["_version"]}


class TestRunBody(BaseModel):
    lines: list[str] = Field(min_length=1, max_length=30)
    seed: int = Field(1, ge=0, le=1_000_000_000)
    data: dict[str, Any] | None = None


@router.post("/academy/scenarios/{key}/test-run")
def test_run(key: str, b: TestRunBody, ctx: Ctx = Depends(require("academy"))):
    if ctx.role not in ("owner", "coach"):
        raise HTTPException(403, "Only the owner or a coach can test-run scenarios")
    if any(len(x) > 1500 for x in b.lines):
        raise HTTPException(422, [{"loc": ["body", "lines"], "msg": "Each line can be at most 1,500 characters"}])
    if b.data is not None:
        _check_size(b.data)
        if ctx.role != "owner":
            raise HTTPException(403, "Only the owner can test-run a draft scenario")
        data = {**b.data, "key": key}
    else:
        data = scenarios.get(ctx.tenant, key)
        if data is None:
            raise HTTPException(404, "Not found")
    return _call(sessions.test_run, ctx.tenant, ctx.user, data, b.lines, b.seed)


# --------------------------------------------------------------------------- rubric rules (declared before /{rid})
@router.get("/academy/roleplays/rubric")
def get_rubric(ctx: Ctx = Depends(require("academy"))):
    rules = rubric.rules(ctx.tenant)
    checks = {v["rule_key"]: v for v in rubric.validate(ctx.tenant)}
    items = []
    for k, r in rules.items():
        v = checks.get(k, {})
        items.append({"rule_key": k, "label": r["label"], "weight": r["weight"], "params": r["params"], "kind": r["kind"],
                      "cite_file": r["cite_file"], "cite_lines": r["cite_lines"], "must_contain": r["must_contain"],
                      "status": r["status"], "note": r["note"], "ok": v.get("ok", False), "issues": v.get("issues", []),
                      "quote": v.get("quote", ""), "source_title": v.get("title", "")})
    return {"items": items, "can_edit": ctx.role == "owner", "flagged": sum(1 for i in items if not i["ok"]),
            "confirmed": sum(1 for i in items if i["status"] == "confirmed")}


class RulePatch(BaseModel):
    label: str | None = Field(None, min_length=2, max_length=160)
    weight: float | None = Field(None, ge=0, le=100)
    params: dict[str, Any] | None = None
    cite_file: str | None = Field(None, max_length=300)
    cite_lines: str | None = Field(None, max_length=40, pattern=r"^(\d{1,5}(-\d{1,5})?|guidance|(steps|do|dont):\d{1,3})?$")
    must_contain: list[str] | None = Field(None, max_length=10)
    confirmed: bool | None = None


@router.put("/academy/roleplays/rubric/{rule_key}")
def put_rule(rule_key: str, b: RulePatch, ctx: Ctx = Depends(require("academy"))):
    _owner(ctx)
    if b.params is not None and len(str(b.params)) > 2000:
        raise HTTPException(422, [{"loc": ["body", "params"], "msg": "params is too large"}])
    if b.must_contain is not None and any(not isinstance(x, str) or not x.strip() or len(x) > 80 for x in b.must_contain):
        raise HTTPException(422, [{"loc": ["body", "must_contain"], "msg": "Each term must be 1-80 characters"}])
    try:
        r = rubric.update(ctx.tenant, rule_key, b.model_dump(), ctx.actor)
    except KeyError:
        raise HTTPException(404, "Not found")
    except rubric.RuleError as e:
        raise HTTPException(422, [{"loc": ["body", "params"], "msg": str(e)}])
    return {"ok": True, "rule": r, "check": rubric.validate_rule(ctx.tenant, r)}


@router.post("/academy/roleplays/rubric/validate")
def validate_rubric(ctx: Ctx = Depends(require("academy"))):
    _owner(ctx)
    res = rubric.validate(ctx.tenant)
    return {"items": res, "flagged": [r["rule_key"] for r in res if not r["ok"]]}


class ScoreTextBody(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    scenario_key: str | None = Field(None, pattern=KEY)


MAX_TRANSCRIPT_LINES = 120


@router.post("/academy/roleplays/score-text")
def score_text(b: ScoreTextBody, ctx: Ctx = Depends(require("academy"))):
    # Scoring is quadratic in the number of turns (each playback is checked against every earlier answer), so a
    # long paste is refused rather than tying up the server.
    if sum(1 for line in b.text.splitlines() if line.strip()) > MAX_TRANSCRIPT_LINES:
        raise HTTPException(422, [{"loc": ["body", "text"],
                                   "msg": f"Paste at most {MAX_TRANSCRIPT_LINES} lines (one line per turn)"}])
    sc = None
    if b.scenario_key:
        sc = scenarios.get(ctx.tenant, b.scenario_key)
        if sc is None:
            raise HTTPException(404, "Not found")
    res = score.score_text(b.text, sc, rubric.rules(ctx.tenant), ctx.tenant)
    return res


# --------------------------------------------------------------------------- sessions
@router.get("/academy/roleplays")
def list_roleplays(user_id: int | None = None, status: Literal["active", "complete", "abandoned"] | None = None,
                   pending_signoff: bool = False, limit: int = Query(50, ge=1, le=200),
                   ctx: Ctx = Depends(require("academy"))):
    items = sessions.list_for(ctx.tenant, ctx.user, user_id, status, pending_signoff, limit)
    return {"items": items, "can_signoff": ctx.role in ("owner", "coach")}


class StartBody(BaseModel):
    scenario_key: str = Field(pattern=KEY)
    seed: int | None = Field(None, ge=0, le=1_000_000_000)
    mode: Literal["rules", "ai"] = "rules"
    exam: bool = False
    hints: bool = True


@router.post("/academy/roleplays")
def start(b: StartBody, ctx: Ctx = Depends(require("academy"))):
    try:
        return sessions.start(ctx.tenant, ctx.user, b.scenario_key, b.seed, b.mode, b.exam, b.hints)
    except KeyError:
        raise HTTPException(404, "That scenario doesn't exist")


@router.get("/academy/roleplays/{rid}")
def get_roleplay(rid: int, ctx: Ctx = Depends(require("academy"))):
    return _call(sessions.view, ctx.tenant, ctx.user, rid)


class TurnBody(BaseModel):
    text: str = Field(min_length=1, max_length=1500)


@router.post("/academy/roleplays/{rid}/turn")
def play_turn(rid: int, b: TurnBody, ctx: Ctx = Depends(require("academy"))):
    if not b.text.strip():
        raise HTTPException(422, [{"loc": ["body", "text"], "msg": "Say something first"}])
    return _call(sessions.turn, ctx.tenant, ctx.user, rid, b.text)


@router.post("/academy/roleplays/{rid}/end")
def end(rid: int, ctx: Ctx = Depends(require("academy"))):
    return _call(sessions.end, ctx.tenant, ctx.user, rid)


@router.post("/academy/roleplays/{rid}/abandon")
def abandon(rid: int, ctx: Ctx = Depends(require("academy"))):
    return _call(sessions.abandon, ctx.tenant, ctx.user, rid)


class SignoffBody(BaseModel):
    note: str = Field("", max_length=500)


@router.post("/academy/roleplays/{rid}/signoff")
def signoff(rid: int, b: SignoffBody, ctx: Ctx = Depends(require("academy"))):
    if guardrails.blocked(guardrails.check_for(ctx.tenant, b.note, {})) and b.note.strip():
        raise HTTPException(422, [{"loc": ["body", "note"], "msg": "The note is blocked by the guardrails"}])
    return _call(sessions.signoff, ctx.tenant, ctx.user, rid, b.note)


class DrillBody(BaseModel):
    criterion: str = Field(pattern=r"^[a-z_]{3,40}$")


@router.post("/academy/roleplays/{rid}/drill")
def drill(rid: int, b: DrillBody, ctx: Ctx = Depends(require("academy"))):
    try:
        return _call(sessions.drill, ctx.tenant, ctx.user, rid, b.criterion)
    except KeyError:
        raise HTTPException(404, "That criterion isn't on this scorecard")
