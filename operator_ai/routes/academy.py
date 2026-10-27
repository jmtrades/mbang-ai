"""Academy API (A11). Everything sits under the existing "academy" tab; role checks happen inside:
learner routes for everyone, team routes for the owner and coaches (coaches only see their own learners),
admin routes for the owner (coaches may read)."""
from __future__ import annotations

from typing import Annotated, Literal, Union

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field

from ..academy import admin, build, certify, common, learn, srs, team
from ..deps import Ctx, require

router = APIRouter()
TAB = require("academy")
MAX_ID = 2**63 - 1  # SQLite's largest integer: a bigger id is malformed input (422), never a crash
Id = Annotated[int, Path(ge=1, le=MAX_ID)]


def _staff(ctx: Ctx) -> None:
    if ctx.role not in ("owner", "coach"):
        raise HTTPException(403, "Only the owner and coaches can see team training")


def _owner(ctx: Ctx) -> None:
    if ctx.role != "owner":
        raise HTTPException(403, "Only the owner can change the Academy")


def _closed_book(ctx: Ctx) -> None:
    """While a certification exam is open the Academy is closed-book, like the paused coach: no lessons, no
    practice answers and no drill answers, because every one of them shows the lines the exam asks about."""
    if certify.exam_open(ctx.tenant, ctx.user["id"]):
        raise HTTPException(423, "Lessons and practice are paused while your certification exam is open. Submit the exam first.")


def _call(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except common.Refused as e:
        raise HTTPException(e.status, str(e))
    except KeyError:
        raise HTTPException(404, "Not found")
    except ValueError as e:
        raise HTTPException(422, str(e))
    except OverflowError:
        raise HTTPException(422, "That number is too large")


# --------------------------------------------------------------------------- learner
@router.get("/academy")
def overview(ctx: Ctx = Depends(TAB)):
    return learn.today(ctx.tenant, ctx.user)


@router.get("/academy/paths")
def paths(ctx: Ctx = Depends(TAB)):
    return {"paths": learn.paths(ctx.tenant, ctx.user)}


@router.get("/academy/paths/{key}")
def path(key: str, ctx: Ctx = Depends(TAB)):
    if key not in common.PATHS:
        raise HTTPException(404, "Not found")
    return _call(learn.path_detail, ctx.tenant, ctx.user, key)


@router.get("/academy/lessons/{lesson_id}")
def lesson(lesson_id: Id, ctx: Ctx = Depends(TAB)):
    _closed_book(ctx)
    return _call(learn.lesson_view, ctx.tenant, ctx.user, lesson_id)


@router.post("/academy/lessons/{lesson_id}/complete")
def complete(lesson_id: Id, ctx: Ctx = Depends(TAB)):
    return _call(learn.complete_lesson, ctx.tenant, ctx.user, lesson_id)


@router.get("/academy/lessons/{lesson_id}/quiz")
def lesson_quiz(lesson_id: Id, ctx: Ctx = Depends(TAB)):
    _closed_book(ctx)
    view = _call(learn.lesson_view, ctx.tenant, ctx.user, lesson_id)
    return {"lesson_id": lesson_id, "items": view["checks"] + view["write_tasks"]}


class AnswerBody(BaseModel):
    answer: Union[str, list[str]] = Field(...)
    ms: int | None = Field(None, ge=0, le=3_600_000)
    context: Literal["quiz", "review", "practice"] = "quiz"
    session_id: str | None = Field(None, max_length=80)


@router.post("/academy/items/{item_id}/answer")
def answer(item_id: Id, b: AnswerBody, ctx: Ctx = Depends(TAB)):
    _closed_book(ctx)
    if isinstance(b.answer, str) and len(b.answer) > 4000:
        raise HTTPException(422, "Answer is too long")
    if isinstance(b.answer, list) and (len(b.answer) > 20 or any(len(x) > 300 for x in b.answer)):
        raise HTTPException(422, "Answer is too long")
    return _call(learn.answer, ctx.tenant, ctx.user, item_id, b.answer, b.ms, b.context, b.session_id)


class ReportBody(BaseModel):
    reason: str = Field("", max_length=500)


@router.post("/academy/items/{item_id}/report")
def report(item_id: Id, b: ReportBody, ctx: Ctx = Depends(TAB)):
    return _call(learn.report_item, ctx.tenant, ctx.user, item_id, b.reason)


@router.get("/academy/review/due")
def review_due(ctx: Ctx = Depends(TAB)):
    cards = srs.due(ctx.tenant, ctx.user["id"])
    return {"cards": cards, "count": len(cards)}


class GradeBody(BaseModel):
    grade: int = Field(ge=0, le=5)


@router.post("/academy/review/{card_id}/grade")
def review_grade(card_id: Id, b: GradeBody, ctx: Ctx = Depends(TAB)):
    """Self-grading is only for objection drills (free recall, then the course answer); questions grade themselves."""
    return _call(srs.grade_drill, ctx.tenant, ctx.user["id"], card_id, b.grade)


@router.get("/academy/drills")
def drills(ctx: Ctx = Depends(TAB)):
    # the list never carries the answers: recall first, then reveal one card (GET /academy/drills/{id}/answer)
    cards = [{k: v for k, v in c.items() if k not in ("answer", "citation")}
             for c in srs.drill_cards(ctx.tenant, ctx.user["id"])]
    return {"cards": cards}


@router.get("/academy/drills/{card_id}/answer")
def drill_answer(card_id: Id, ctx: Ctx = Depends(TAB)):
    _closed_book(ctx)
    for c in srs.drill_cards(ctx.tenant, ctx.user["id"]):
        if c["card_id"] == card_id:
            return {"card_id": card_id, "objection": c["objection"], "answer": c["answer"], "citation": c["citation"]}
    raise HTTPException(404, "Not found")


@router.get("/academy/practice")
def practice(skill: str | None = Query(None, max_length=40), n: int = Query(5, ge=1, le=20), ctx: Ctx = Depends(TAB)):
    if skill and skill not in common.SKILLS:
        raise HTTPException(422, "Unknown skill")
    return {"items": srs.adaptive_items(ctx.tenant, ctx.user["id"], skill, n), "skill": skill,
            "mastery": list(srs.mastery(ctx.tenant, ctx.user["id"]).values()),
            "skills": [{"key": s, "label": common.skill_label(s)} for s in common.SKILLS
                       if s in srs.skills_with_items(ctx.tenant)]}


@router.get("/academy/session")
def session(ctx: Ctx = Depends(TAB)):
    keys = common.user_paths(ctx.tenant, ctx.user)
    skills = sorted({s for k in keys for s in common.PATHS[k]["skills"]})
    return srs.session(ctx.tenant, ctx.user, skills)


class ExamBody(BaseModel):
    path: str = Field(min_length=2, max_length=40)
    level: Literal["L3"] = "L3"


@router.post("/academy/exams")
def exam_start(b: ExamBody, ctx: Ctx = Depends(TAB)):
    if b.path not in common.PATHS:
        raise HTTPException(422, "Unknown path")
    return _call(certify.start_exam, ctx.tenant, ctx.user, b.path, b.level)


@router.get("/academy/exams/{exam_id}")
def exam_get(exam_id: Id, ctx: Ctx = Depends(TAB)):
    return _call(certify.get_exam, ctx.tenant, ctx.user, exam_id)


ExamAnswer = Union[Annotated[str, Field(max_length=4000)], Annotated[list[Annotated[str, Field(max_length=300)]], Field(max_length=20)]]


class SubmitBody(BaseModel):
    answers: dict[Annotated[str, Field(max_length=20)], ExamAnswer] = Field(default_factory=dict)


@router.post("/academy/exams/{exam_id}/submit")
def exam_submit(exam_id: Id, b: SubmitBody, ctx: Ctx = Depends(TAB)):
    if len(b.answers) > 200:
        raise HTTPException(422, "Too many answers")
    return _call(certify.submit_exam, ctx.tenant, ctx.user, exam_id, b.answers)


@router.get("/academy/certs/me")
def certs_me(ctx: Ctx = Depends(TAB)):
    keys = common.user_paths(ctx.tenant, ctx.user)
    return {"ladders": [certify.ladder(ctx.tenant, ctx.user, k) for k in keys],
            "note": "Internal training credentials. They show training completed, never income or results."}


@router.post("/academy/certs/check")
def certs_check(ctx: Ctx = Depends(TAB)):
    return {"awarded": certify.refresh(ctx.tenant, ctx.user, ctx.actor)}


# --------------------------------------------------------------------------- team (owner, coach)
@router.get("/academy/team")
def team_view(ctx: Ctx = Depends(TAB)):
    _staff(ctx)
    return team.cohort(ctx.tenant, ctx.user)


@router.get("/academy/team/misconceptions")
def team_misconceptions(ctx: Ctx = Depends(TAB)):
    _staff(ctx)
    _closed_book(ctx)
    return {"items": team.misconceptions(ctx.tenant, ctx.user)}


@router.get("/academy/team/signoffs")
def team_signoffs(ctx: Ctx = Depends(TAB)):
    _staff(ctx)
    return {"items": team.signoff_queue(ctx.tenant, ctx.user)}


@router.get("/academy/team/impact")
def team_impact(ctx: Ctx = Depends(TAB)):
    _staff(ctx)
    return team.impact(ctx.tenant, ctx.user)


class ManagerBody(BaseModel):
    manager_id: int = Field(ge=1, le=MAX_ID)
    member_id: int = Field(ge=1, le=MAX_ID)


@router.get("/academy/team/managers")
def team_managers(ctx: Ctx = Depends(TAB)):
    _owner(ctx)
    return team.managers(ctx.tenant)


@router.post("/academy/team/managers")
def link_manager(b: ManagerBody, ctx: Ctx = Depends(TAB)):
    """Owner: a coach manages this setter, closer, student or coach (team_assignments)."""
    _owner(ctx)
    return _call(team.link_manager, ctx.tenant, b.manager_id, b.member_id, ctx.actor)


@router.delete("/academy/team/managers/{manager_id}/{member_id}")
def unlink_manager(manager_id: Id, member_id: Id, ctx: Ctx = Depends(TAB)):
    _owner(ctx)
    return _call(team.unlink_manager, ctx.tenant, manager_id, member_id, ctx.actor)


@router.get("/academy/team/{user_id}")
def team_member(user_id: Id, ctx: Ctx = Depends(TAB)):
    _staff(ctx)
    return _call(team.learner_detail, ctx.tenant, ctx.user, user_id)


class SignoffBody(BaseModel):
    score: float = Field(ge=0, le=100)
    note: str = Field("", max_length=500)


@router.post("/academy/team/roleplays/{roleplay_id}/signoff")
def roleplay_signoff(roleplay_id: Id, b: SignoffBody, ctx: Ctx = Depends(TAB)):
    _staff(ctx)
    return _call(team.signoff_roleplay, ctx.tenant, roleplay_id, ctx.user, b.score, b.note, ctx.actor)


class AssignBody(BaseModel):
    user_id: int = Field(ge=1, le=MAX_ID)
    kind: Literal["lesson", "scenario", "path"]
    ref: str = Field(min_length=1, max_length=120)
    due_days: int | None = Field(None, ge=1, le=120)
    note: str = Field("", max_length=400)


@router.post("/academy/assign")
def assign(b: AssignBody, ctx: Ctx = Depends(TAB)):
    _staff(ctx)
    if b.kind == "lesson" and not (b.ref.isascii() and b.ref.isdigit() and len(b.ref) <= 18):
        raise HTTPException(422, "A lesson is assigned by its id")
    return _call(team.assign, ctx.tenant, ctx.user, b.user_id, b.kind, b.ref, b.due_days, b.note, ctx.actor)


@router.post("/academy/certs/{cert_id}/signoff")
def cert_signoff(cert_id: Id, ctx: Ctx = Depends(TAB)):
    _staff(ctx)
    return _call(certify.signoff_cert, ctx.tenant, cert_id, ctx.user, ctx.actor)


class RevokeBody(BaseModel):
    reason: str = Field(min_length=3, max_length=300)


@router.post("/academy/certs/{cert_id}/revoke")
def cert_revoke(cert_id: Id, b: RevokeBody, ctx: Ctx = Depends(TAB)):
    _staff(ctx)
    return _call(certify.revoke_cert, ctx.tenant, cert_id, ctx.user, b.reason, ctx.actor)


# --------------------------------------------------------------------------- admin (owner; coaches read)
@router.post("/academy/build")
def start_build(ctx: Ctx = Depends(TAB)):
    _owner(ctx)
    # the demo material is tiny, so the demo builds in-line; real material builds in the background
    return _call(build.start, ctx.tenant, ctx.actor, sync=ctx.tenant.is_demo)


@router.get("/academy/admin/builds")
def admin_builds(ctx: Ctx = Depends(TAB)):
    _staff(ctx)
    return {"builds": admin.builds(ctx.tenant), "material": common.material_state(ctx.tenant)}


@router.get("/academy/admin/drafts")
def admin_drafts(status: Literal["draft", "published", "stale", "rejected", "retired"] = "draft", ctx: Ctx = Depends(TAB)):
    _staff(ctx)
    _closed_book(ctx)  # a coach sitting their own exam can't read the lessons here either
    return admin.drafts(ctx.tenant, status, owner=ctx.role == "owner")


class DecideBody(BaseModel):
    decision: Literal["publish", "reject", "retire"]


@router.post("/academy/admin/lessons/{lesson_id}/decide")
def decide_lesson(lesson_id: Id, b: DecideBody, ctx: Ctx = Depends(TAB)):
    _owner(ctx)
    return _call(build.decide_lesson, ctx.tenant, lesson_id, b.decision, ctx.actor)


class ItemDecideBody(BaseModel):
    decision: Literal["publish", "reject", "retire", "to_exam", "to_practice"]


@router.post("/academy/admin/items/{item_id}/decide")
def decide_item(item_id: Id, b: ItemDecideBody, ctx: Ctx = Depends(TAB)):
    _owner(ctx)
    return _call(build.decide_item, ctx.tenant, item_id, b.decision, ctx.actor)


class PublishAllBody(BaseModel):
    include_exam: bool = False


@router.post("/academy/admin/publish-all")
def publish_all(b: PublishAllBody, ctx: Ctx = Depends(TAB)):
    _owner(ctx)
    return _call(build.publish_all, ctx.tenant, ctx.actor, b.include_exam)


@router.post("/academy/admin/check-sources")
def check_sources(ctx: Ctx = Depends(TAB)):
    _owner(ctx)
    return _call(build.check_sources, ctx.tenant)


@router.get("/academy/admin/coverage")
def admin_coverage(ctx: Ctx = Depends(TAB)):
    _staff(ctx)
    return admin.coverage(ctx.tenant, ctx.user)


@router.get("/academy/admin/item-stats")
def admin_item_stats(ctx: Ctx = Depends(TAB)):
    _staff(ctx)
    return admin.stats(ctx.tenant)


@router.get("/academy/admin/settings")
def admin_settings(ctx: Ctx = Depends(TAB)):
    _staff(ctx)
    return admin.settings_view(ctx.tenant)


class SettingsBody(BaseModel):
    require_cert_for_own_outreach: bool | None = None
    require_setter_cert_for_decisions: bool | None = None


@router.put("/academy/admin/settings")
def put_admin_settings(b: SettingsBody, ctx: Ctx = Depends(TAB)):
    _owner(ctx)
    changes = {k: v for k, v in b.model_dump().items() if v is not None}
    if not changes:
        raise HTTPException(422, "Nothing to change")
    out: dict = {}
    for k, v in changes.items():
        out.update(admin.set_setting(ctx.tenant, v, ctx.actor, k))
    return out


class ThresholdsBody(BaseModel):
    levels: dict[str, dict[str, float]]


@router.put("/academy/admin/paths/{key}")
def put_thresholds(key: str, b: ThresholdsBody, ctx: Ctx = Depends(TAB)):
    _owner(ctx)
    return _call(admin.set_thresholds, ctx.tenant, key, b.levels, ctx.actor)

