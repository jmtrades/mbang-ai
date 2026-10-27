"""Typed step registry for missions.

Every step a mission can take is a StepType that wraps an existing function (diagnostic, Studio, the
webinar chain, the sales and onboarding agents, fulfilment, Ads Ops, call review) or a feature module
that may not be installed yet (prospects, proposals, academy, delivery, client_ops, webinars). Those are
imported inside the step with try/except ImportError and the step degrades with a plain-English note.

A step's run function is an idempotent "advance" function: it reads the step's previous state and returns
a StepResult. The engine calls it again whenever something the step waits for happens (an approval, an
event, a time, an answer), so a step never drafts the same thing twice.

wait_for:  none      finishes in one go
           approval  drafts go to the approval queue; the step waits until each one is decided
           event     waits for a domain event (reply_received, client_won, certification_awarded ...)
           time      waits until a time (a checkpoint, the next daily block)
Outbound steps always wait for approval, whatever a template or the AI planner asks for.
"""
from __future__ import annotations

import importlib
import inspect
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import db, guardrails, llm, queue, runtime

log = logging.getLogger("operator.missions")

DAY = 86400.0


def clock() -> float:
    """Current time. Tests replace this to move time forward."""
    return time.time()


STATUSES = ("pending", "ready", "running", "waiting_approval", "waiting_event", "waiting_time", "done", "failed",
            "skipped", "blocked_human")
TERMINAL = ("done", "skipped")
WAITING = ("waiting_approval", "waiting_event", "waiting_time", "blocked_human")
STATUS_LABEL = {
    "pending": "Not started", "ready": "Starting", "running": "Working", "waiting_approval": "Waiting for approval",
    "waiting_event": "Waiting", "waiting_time": "Scheduled", "done": "Done", "failed": "Failed", "skipped": "Skipped",
    "blocked_human": "Needs you",
}
QUEUE_OPEN = ("pending", "held", "needs_human")
QUEUE_NOT_SENT = ("rejected", "cancelled", "send_failed")
POSITIVE_REPLIES = ("positive", "question", "price_ask", "interested", "booked")


# --------------------------------------------------------------------------- types
@dataclass
class Param:
    type: str                      # int | float | str | bool | enum
    default: Any = None
    min: float | None = None
    max: float | None = None
    choices: tuple = ()
    help: str = ""


@dataclass
class StepType:
    key: str
    label: str
    description: str
    run: Callable[["StepCtx"], "StepResult"]
    wait_for: str = "none"
    outbound: bool = False
    recurring: bool = False
    needs_cert: tuple[str, str] | None = None
    success: str = ""
    params: dict[str, Param] = field(default_factory=dict)
    human_action: str | None = None       # what "mark done" means for this step, in plain English
    outcomes: tuple[str, ...] = ()        # allowed outcomes for mark-done
    planner: bool = True                  # the optional AI planner may use it
    feature: str | None = None            # optional feature module it prefers

    def schema(self) -> dict:
        return {"key": self.key, "label": self.label, "description": self.description, "wait_for": self.wait_for,
                "outbound": self.outbound, "recurring": self.recurring, "success": self.success,
                "needs_cert": list(self.needs_cert) if self.needs_cert else None,
                "params": {k: {"type": p.type, "default": p.default, "min": p.min, "max": p.max,
                               "choices": list(p.choices), "help": p.help} for k, p in self.params.items()}}


@dataclass
class StepResult:
    status: str
    message: str = ""
    output: dict | None = None
    queue_ids: list[int] | None = None
    wait: dict | None = None               # {"events": [...], "recheck": bool}
    due: float | None = None               # wake again at this time
    context: dict | None = None            # mission memory patch (top-level keys replaced)
    cycle: bool = False                    # a recurring step finished one cycle
    spawn: list[dict] | None = None        # child missions to start: {"template", "answers", "goal"}


@dataclass
class StepCtx:
    tenant: Any
    mission: dict
    step: dict
    params: dict
    user: dict | None
    run: runtime.Run
    now: float

    @property
    def ctx(self) -> dict:
        return self.mission.setdefault("context", {})

    @property
    def out(self) -> dict:
        return self.step.get("output") or {}

    @property
    def uid(self) -> int:
        return self.mission["user_id"]

    @property
    def mid(self) -> int:
        return self.mission["id"]

    @property
    def actor(self) -> str:
        u = self.user
        return f"{u['username']}({u['role']})" if u else "system"

    @property
    def answers(self) -> dict:
        return self.ctx.get("answers") or {}


class FeatureMismatch(RuntimeError):
    """A feature module is installed but its function doesn't take arguments we can supply."""


REGISTRY: dict[str, StepType] = {}


def step_type(key: str, label: str, description: str, **kw):
    def deco(fn):
        REGISTRY[key] = StepType(key=key, label=label, description=description, run=fn, **kw)
        return fn

    return deco


def get_type(key: str) -> StepType | None:
    return REGISTRY.get(key)


def schema() -> list[dict]:
    return [t.schema() for t in REGISTRY.values()]


# --------------------------------------------------------------------------- feature modules (guarded)
def feature(name: str):
    """A feature module other engineers are building, or None while it isn't installed."""
    try:
        return importlib.import_module(f"operator_ai.{name}")
    except ImportError:
        return None


def feature_fn(name: str, *fns: str):
    mod = feature(name)
    if mod is None:
        return None
    for f in fns:
        fn = getattr(mod, f, None)
        if callable(fn):
            return fn
    return None


def invoke(fn: Callable, tenant, **cands):
    """Call fn(tenant, ...) passing only the keyword arguments it declares. Raises FeatureMismatch when it needs
    an argument we can't supply."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return fn(tenant)
    params = list(sig.parameters.values())[1:]
    kwargs = {}
    for p in params:
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        if p.name in cands and cands[p.name] is not None:
            kwargs[p.name] = cands[p.name]
        elif p.default is p.empty:
            raise FeatureMismatch(f"{getattr(fn, '__name__', 'feature')} needs '{p.name}'")
    return fn(tenant, **kwargs)


def qids_from(tenant, res) -> list[int]:
    """Queue ids out of whatever a wrapped function returned."""
    found: list[int] = []

    def take(x):
        if isinstance(x, bool):
            return
        if isinstance(x, int):
            found.append(x)
        elif isinstance(x, dict):
            if isinstance(x.get("queue_id"), int):
                found.append(x["queue_id"])
            elif isinstance(x.get("id"), int) and "kind" in x and "status" in x:
                found.append(x["id"])
            for k in ("queue_ids", "queued", "items", "drafts", "openers"):
                v = x.get(k)
                if isinstance(v, list):
                    for y in v:
                        take(y)
        elif isinstance(x, list):
            for y in x:
                take(y)

    take(res)
    ids = sorted({i for i in found if i > 0})
    if not ids:
        return []
    with tenant.db() as c:
        have = {r["id"] for r in c.execute(f"SELECT id FROM queue WHERE id IN ({','.join('?' * len(ids))})", ids)}
    return [i for i in ids if i in have]


# --------------------------------------------------------------------------- mission events & memory helpers
def add_event(tenant, mid: int, kind: str, detail: dict | None = None, key: str | None = None) -> int | None:
    """Append to the mission's event log. With a key, the same (kind, key) is written once."""
    d = dict(detail or {})
    if key:
        d["key"] = key
    with tenant.db() as c:
        if key and c.execute("SELECT 1 FROM mission_events WHERE mission_id=? AND kind=? AND json_extract(detail,'$.key')=?",
                             (mid, kind, key)).fetchone():
            return None
        return c.execute("INSERT INTO mission_events(mission_id,kind,detail,created) VALUES(?,?,?,?)",
                         (mid, kind, db.jdump(d), clock())).lastrowid


def events_of(tenant, mid: int, *kinds: str) -> list[dict]:
    q = "SELECT * FROM mission_events WHERE mission_id=?"
    args: list = [mid]
    if kinds:
        q += f" AND kind IN ({','.join('?' * len(kinds))})"
        args += list(kinds)
    with tenant.db() as c:
        rows = db.rows(c.execute(q + " ORDER BY id", args))
    for r in rows:
        r["detail"] = db.jload(r["detail"], {})
    return rows


def q_states(tenant, qids: list[int]) -> dict:
    out = {"open": [], "approved": [], "rejected": [], "held": [], "status": {}}
    if not qids:
        return out
    with tenant.db() as c:
        rows = c.execute(f"SELECT id, status FROM queue WHERE id IN ({','.join('?' * len(qids))})", list(qids)).fetchall()
    for r in rows:
        out["status"][r["id"]] = r["status"]
        if r["status"] in QUEUE_OPEN:
            out["open"].append(r["id"])
            if r["status"] == "held":
                out["held"].append(r["id"])
        elif r["status"] in QUEUE_NOT_SENT:
            out["rejected"].append(r["id"])
        else:
            out["approved"].append(r["id"])
    return out


def tag_queue(tenant, qids: list[int], mid: int, sid: int, owner_user_id: int) -> None:
    """Link queue items drafted by a wrapped function back to the mission step (and to the user who drafts for
    their own prospects/clients, when the function didn't set an owner)."""
    if not qids:
        return
    marks = ",".join("?" * len(qids))
    with tenant.db() as c:
        c.execute(f"UPDATE queue SET meta=json_set(COALESCE(meta,'{{}}'),'$.mission_id',?,'$.step_id',?) WHERE id IN ({marks})",
                  [mid, sid, *qids])
        c.execute(f"UPDATE queue SET meta=json_set(meta,'$.owner_user_id',?) WHERE id IN ({marks}) "
                  "AND json_extract(meta,'$.owner_user_id') IS NULL", [owner_user_id, *qids])


def draft(sc: StepCtx, kind: str, body: str, *, channel: str = "manual", recipient: str | None = None, title: str = "",
          meta: dict | None = None) -> dict:
    """Queue one draft for approval, linked to this mission step and owned by the mission's user."""
    m = {**(meta or {}), "mission_id": sc.mid, "step_id": sc.step["id"], "owner_user_id": sc.uid,
         "mission_step": sc.step.get("skey")}
    return queue.add(sc.tenant, kind, body, channel=channel, recipient_ref=recipient, title=title[:200], meta=m,
                     run_id=sc.run.id, actor=sc.actor, demo=sc.tenant.is_demo)


def ai_text(sc: StepCtx, purpose: str, system: str, prompt: str, meta: dict | None = None) -> tuple[str | None, str, str]:
    """Optional AI wording behind the budget gate. Returns (text or None, mode, label). AI output that fails the
    guardrails is thrown away, never shown."""
    try:
        r = llm.complete_or_replay(sc.tenant, system, prompt, purpose, max_tokens=500, run_id=sc.run.id)
    except llm.AIUnavailable as e:
        return None, "rules", f"Rules draft ({e.reason})"
    text = (r.get("text") or "").strip()
    if not text or guardrails.blocked(guardrails.check_for(sc.tenant, text, meta or {})):
        return None, "rules", "Rules draft (the AI draft didn't pass the guardrails)"
    sc.run.use_ai(r.get("mode", "live"))
    return text, r.get("mode", "live"), r.get("label") or "AI"


def prospects(tenant, uid: int, where: str = "", args: tuple = (), order: str = "COALESCE(fit_score,-1) DESC, id",
              limit: int = 500) -> list[dict]:
    """The user's own prospects (the prospects feature owns the table; missions only read it)."""
    with tenant.db() as c:
        rows = db.rows(c.execute(f"SELECT * FROM prospects WHERE owner_user_id=? {where} ORDER BY {order} LIMIT ?",
                                 (uid, *args, limit)))
    for r in rows:
        r["research"] = db.jload(r.get("research"), None)
    return rows


def prospect(tenant, pid: int | None, uid: int | None = None) -> dict | None:
    if not pid:
        return None
    with tenant.db() as c:
        r = db.row(c.execute("SELECT * FROM prospects WHERE id=?", (pid,)).fetchone())
    if r is None or (uid is not None and r.get("owner_user_id") not in (uid, None)):
        return None
    r["research"] = db.jload(r.get("research"), None)
    return r


def next_morning(now: float, hour: int = 9) -> float:
    lt = time.localtime(now + DAY)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, 0, 0, 0, 0, -1))


def day_start(now: float) -> float:
    lt = time.localtime(now)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def when(ts: float | None) -> str:
    return time.strftime("%a %d %b %H:%M", time.localtime(ts)) if ts else ""


def money(v) -> str:
    try:
        return f"${float(v):,.0f}"
    except (TypeError, ValueError):
        return "your price"


def _first(name: str | None) -> str:
    n = re.sub(r"\(DEMO\)", "", name or "").strip()
    return n.split()[0] if n else "there"


def _approval_result(sc: StepCtx, qids: list[int], what: str, done_msg: str) -> StepResult:
    """Common tail for draft-and-approve steps."""
    st = q_states(sc.tenant, qids)
    if st["open"]:
        held = f" {len(st['held'])} held by the guardrails: edit it clean first." if st["held"] else ""
        return StepResult("waiting_approval", f"{what} waiting for approval in the queue.{held}", queue_ids=qids)
    if st["approved"]:
        return StepResult("done", done_msg)
    return StepResult("blocked_human", f"{what} was rejected. Press Retry to draft it again, or Skip this step.",
                      wait={"events": ["context_updated"]})


# =========================================================================== step types
# --------------------------------------------------------------------------- intake
@step_type("intake", "Goal intake", "Collect the answers the plan needs and set measurable targets.",
           success="Every required answer given; targets set from the SOP benchmark rates", planner=False)
def _intake(sc: StepCtx) -> StepResult:
    from . import templates

    tpl = templates.get(sc.mission["template"])
    missing = templates.missing(tpl, sc.answers) if tpl else []
    if missing:
        labels = "; ".join(q.label for q in missing)
        return StepResult("blocked_human", f"Answer {len(missing)} question(s) to plan this mission: {labels}",
                          output={"missing": [q.key for q in missing]}, wait={"events": ["context_updated"]})
    targets = templates.targets_for(sc.tenant, sc.mission["template"], sc.answers)
    a = sc.answers
    patch = {"targets": targets,
             "icp": {k: a[k] for k in ("niche", "audience") if a.get(k)},
             "offer": {k: a[k] for k in ("offer", "price", "outcome") if a.get(k) not in (None, "")}}
    if "opener_move" not in sc.ctx:
        patch["opener_move"] = "question"
    msg = targets.get("summary") or "Plan set."
    return StepResult("done", msg, output={"targets": targets}, context=patch)


# --------------------------------------------------------------------------- certification gate
PATH_LABEL = {"growth_operator": "Growth operator", "setter": "DM setting", "closer": "Closing calls", "coach": "Coaching",
              "compliance_core": "Compliance basics"}


def cert_state(tenant, uid: int, path_key: str, level: str) -> str:
    """'certified' | 'missing' | 'no_academy'"""
    fn = feature_fn("academy", "is_certified")
    if fn is None:
        return "no_academy"
    try:
        return "certified" if fn(tenant, uid, path_key, level) else "missing"
    except Exception:  # noqa: BLE001 - a broken academy must not stop missions; treat as not set up
        log.warning("academy.is_certified failed")
        return "no_academy"


def assign_path(sc: StepCtx, path_key: str, level: str) -> bool:
    fn = feature_fn("academy", "assign")
    if fn is None:
        return False
    try:
        invoke(fn, sc.tenant, user_id=sc.uid, user=sc.user, path_key=path_key, level=level, actor=sc.actor,
               assigned_by=sc.actor, reason=f"Mission #{sc.mid}", mission_id=sc.mid)
        return True
    except Exception:  # noqa: BLE001
        log.warning("academy.assign failed")
        return False


@step_type("cert_gate", "Certification gate", "Learn and pass a short quiz/role-play before doing the real thing.",
           wait_for="event", success="Certified at the level asked for",
           params={"path_key": Param("enum", "setter", choices=tuple(PATH_LABEL)),
                   "level": Param("enum", "L1", choices=("L1", "L2", "L3", "L4")),
                   "label": Param("str", "")},
           feature="academy")
def _cert_gate(sc: StepCtx) -> StepResult:
    path, level = sc.params.get("path_key", "setter"), sc.params.get("level", "L1")
    name = sc.params.get("label") or f"{PATH_LABEL.get(path, path)} {level}"
    cap = name[:1].upper() + name[1:]
    state = cert_state(sc.tenant, sc.uid, path, level)
    if state == "no_academy":
        return StepResult("skipped", f"{cap}: the Academy isn't set up on this workspace yet, so this gate is skipped.",
                          output={"gate": "no_academy"})
    if state == "certified":
        return StepResult("done", f"{cap}: certified.", output={"gate": "certified"})
    assigned = sc.out.get("assigned") or assign_path(sc, path, level)
    return StepResult("waiting_event", f"Complete {name} in the Academy (lesson, quiz and role-play) to unlock the next steps.",
                      output={"gate": "waiting", "assigned": bool(assigned), "href": "#/academy"},
                      wait={"events": ["certification_awarded"]})


# --------------------------------------------------------------------------- offer & profile drafts
def _brief(sc: StepCtx) -> dict:
    from .. import brief as brief_mod

    a = sc.answers
    partial = {"niche": a.get("niche"), "audience": a.get("audience") or (f"{a['niche']}" if a.get("niche") else None),
               "offer": a.get("offer"), "price": a.get("price"), "outcome": a.get("outcome"),
               "mechanism": a.get("mechanism"), "pain": a.get("pain"), "timeframe": a.get("timeframe")}
    return brief_mod.build(sc.tenant, partial)


@step_type("offer_doc", "Offer document", "Draft a one-page offer (who, outcome, how, price, who it isn't for) for approval.",
           wait_for="approval", success="Offer document approved", human_action=None)
def _offer_doc(sc: StepCtx) -> StepResult:
    qids = sc.out.get("qids") or []
    if not qids:
        built = _brief(sc)
        b = built["brief"]
        rules = (f"Offer: {b['offer']}\nWho it's for: {b['audience']}\nThe outcome: {b['outcome']} in {b['timeframe']}\n"
                 f"How: our {b['mechanism']}\nWithout: {b['pain']}\nOne line (USP): {built['usp']}\n"
                 f"Price: {money(b['price'])} (your own offer price; only for clients and proposals, never in cold DMs)\n"
                 "What's included: a kickoff call, a written 30/60/90 plan, weekly numbers and one fix at a time.\n"
                 "Who it isn't for: anyone looking for a shortcut or a promised result.\n"
                 "Rules: no promised results and no income claims; only use results you can prove, with permission.")
        text, mode, label = ai_text(sc, "mission:offer_doc", "Tighten this offer document. Keep every line true. "
                                    "No guarantees, no income claims, no invented results. Plain English.", rules, {})
        item = draft(sc, "mission_draft", text or rules, title="Offer document (mission)",
                     meta={"draft": "offer_doc", "mode": mode})
        return StepResult("waiting_approval", "Offer document drafted: approve it (or edit it first) in the queue.",
                          queue_ids=[item["id"]], output={"qids": [item["id"]], "mode": mode, "label": label})
    return _approval_result(sc, qids, "Offer document", "Offer document approved.")


@step_type("profile_assets", "Profile and bio", "Draft profile bio, pinned post and highlight ideas that match the offer.",
           wait_for="approval", success="Profile copy approved")
def _profile_assets(sc: StepCtx) -> StepResult:
    qids = sc.out.get("qids") or []
    if not qids:
        b = _brief(sc)["brief"]
        body = (f"Bio: Helping {b['audience']} get {b['outcome']} without {b['pain']}. Message me 'PLAN' for the one-page plan.\n"
                f"Pinned post 1: The one mistake that keeps {b['audience']} stuck (and what to do instead).\n"
                f"Pinned post 2: How our {b['mechanism']} works, in three steps.\n"
                "Pinned post 3: Who I work with, and who I don't.\n"
                "Highlights: About · How it works · FAQ · Results (only real, permitted results)")
        item = draft(sc, "mission_draft", body, title="Profile and bio (mission)", meta={"draft": "profile_assets"})
        return StepResult("waiting_approval", "Profile copy drafted: approve it in the queue.", queue_ids=[item["id"]],
                          output={"qids": [item["id"]]})
    return _approval_result(sc, qids, "Profile copy", "Profile copy approved.")


# --------------------------------------------------------------------------- prospects
@step_type("prospect_list", "Prospect list", "Build a list of real prospects that fit the niche (CSV import or one at a time).",
           wait_for="event", success="At least the minimum number of prospects on the list",
           params={"min": Param("int", 30, 1, 1000, help="How many prospects the list needs")}, feature="prospects")
def _prospect_list(sc: StepCtx) -> StepResult:
    need = int(sc.params.get("min") or 30)
    rows = prospects(sc.tenant, sc.uid, "AND COALESCE(do_not_contact,0)=0")
    if len(rows) >= need:
        return StepResult("done", f"{len(rows)} prospects on your list.", output={"count": len(rows)})
    return StepResult("blocked_human", f"Add {need - len(rows)} more prospects on the Prospects page (import a CSV or add "
                      f"them one at a time). You have {len(rows)} of {need}.",
                      output={"count": len(rows), "need": need, "href": "#/prospects"},
                      wait={"events": ["prospect_added", "prospect_stage"], "recheck": True})


@step_type("research_fit", "Research and fit score", "Research the best-fit prospects and score how well each fits.",
           success="Top prospects researched with sources", params={"top": Param("int", 50, 1, 500),
                                                                    "force": Param("bool", False)},
           feature="prospects")
def _research_fit(sc: StepCtx) -> StepResult:
    fn = feature_fn("prospects", "research")
    if fn is None:
        return StepResult("skipped", "Prospect research isn't available on this workspace yet, so openers stay general "
                                     "(no personal details).", output={"researched": 0})
    top = int(sc.params.get("top") or 50)
    where = "AND COALESCE(do_not_contact,0)=0" + ("" if sc.params.get("force") else " AND research IS NULL")
    rows = prospects(sc.tenant, sc.uid, where, limit=top)
    ok, errors = 0, 0
    for p in rows:
        try:
            invoke(fn, sc.tenant, prospect_id=p["id"], pid=p["id"], user=sc.user, owner_user_id=sc.uid, mission_id=sc.mid,
                   actor=sc.actor)
            ok += 1
        except Exception:  # noqa: BLE001 - one prospect failing must not stop the others
            errors += 1
    msg = f"Researched {ok} prospect(s)" + (f"; {errors} couldn't be researched" if errors else "") + "."
    return StepResult("done", msg, output={"researched": ok, "errors": errors})


# --------------------------------------------------------------------------- outreach
OPENER_MOVES = {
    "question": "Quick question: what's the main thing holding back new clients for you right now?",
    "resource": "I put together a one-page checklist on turning the audience you already have into booked calls. "
                "Want me to send it over?",
    "curiosity": "Curious how most of your new clients find you at the moment: referrals, content or ads?",
}
MOVE_ORDER = ["question", "resource", "curiosity"]
FOLLOWUPS = [
    (2, "One idea that tends to help: answer every new enquiry within five minutes. How quickly do new enquiries hear "
        "back from you today?"),
    (4, "Happy to send the one-page checklist if it's useful. Want it?"),
    (7, "Last note from me for now. If more booked calls are on your list this quarter, I'm happy to compare notes. "
        "Should I check back in a month?"),
]


def daily_cap(sc: StepCtx) -> int:
    from .. import outreach_loop

    t = (sc.ctx.get("targets") or {}).get("openers_per_day") or outreach_loop.cap(sc.tenant)
    return max(0, min(int(t), outreach_loop.cap(sc.tenant)))


def _drafted_today(sc: StepCtx) -> int:
    start = day_start(sc.now)
    with sc.tenant.db() as c:
        return c.execute("SELECT COUNT(*) FROM queue WHERE kind IN ('dm_opener','prospect_email','prospect_dm') "
                         "AND json_extract(meta,'$.owner_user_id')=? AND json_extract(meta,'$.first_touch')=1 "
                         "AND status NOT IN ('rejected','cancelled') AND created>=?", (sc.uid, start)).fetchone()[0]


def _contacted_ids(sc: StepCtx) -> set[int]:
    with sc.tenant.db() as c:
        return {r[0] for r in c.execute(
            "SELECT DISTINCT json_extract(meta,'$.prospect_id') FROM queue WHERE json_extract(meta,'$.owner_user_id')=? "
            "AND json_extract(meta,'$.first_touch')=1 AND status NOT IN ('rejected','cancelled')", (sc.uid,)) if r[0]}


def _opener_candidates(sc: StepCtx, n: int) -> list[dict]:
    done = _contacted_ids(sc)
    rows = prospects(sc.tenant, sc.uid, "AND COALESCE(do_not_contact,0)=0 AND COALESCE(stage,'new') IN ('new','researched')")
    return [p for p in rows if p["id"] not in done and (p.get("handle") or p.get("email"))][:n]


def _fallback_openers(sc: StepCtx, picks: list[dict]) -> list[int]:
    move = sc.ctx.get("opener_move") or "question"
    body = OPENER_MOVES.get(move, OPENER_MOVES["question"])
    ids = []
    for p in picks:
        meta = {"prospect_facing": True, "lead_tier": "cold", "prospect_id": p["id"], "opener_move": move,
                "first_touch": True}
        if p.get("handle"):
            item = draft(sc, "dm_opener", f"Hi {_first(p.get('name'))}, {body[0].lower() + body[1:]}", channel="instagram_dm",
                         recipient=p["handle"], title=f"Opener to {p.get('name') or p['handle']}", meta=meta)
        else:
            item = draft(sc, "prospect_email", f"Hi {_first(p.get('name'))},\n\n{body}\n", channel="email",
                         recipient=p["email"], title=f"Opener email to {p.get('name') or p['email']}",
                         meta={**meta, "subject": "A quick question"})
        ids.append(item["id"])
    return ids


@step_type("daily_openers", "Daily opener block", "Each day, draft a capped block of first messages to the best-fit "
           "prospects. Every one waits for approval; it repeats until the goal is won.",
           wait_for="approval", outbound=True, recurring=True, needs_cert=("setter", "L1"),
           success="Openers approved and confirmed sent each day, within the daily cap",
           params={"per_day": Param("int", None, 1, 50, help="Defaults to the plan target, never above the cap")},
           feature="prospects")
def _daily_openers(sc: StepCtx) -> StepResult:
    if sc.ctx.get("won"):
        return StepResult("done", "Goal won: no more opener blocks.")
    o = sc.out
    block = o.get("block_qids") or []
    if block:
        st = q_states(sc.tenant, block)
        if st["open"]:
            return StepResult("waiting_approval", f"{len(st['open'])} opener(s) waiting for your approval "
                              f"(block {o.get('block', 1)}).", queue_ids=block)
        nxt = next_morning(sc.now)
        return StepResult("waiting_time", f"Block {o.get('block', 1)}: {len(st['approved'])} approved, "
                          f"{len(st['rejected'])} rejected. Next block {when(nxt)}.",
                          output={"block_qids": [], "next_block_at": nxt,
                                  "approved_total": o.get("approved_total", 0) + len(st["approved"])},
                          due=nxt, cycle=True)
    if o.get("next_block_at") and sc.now < o["next_block_at"]:
        return StepResult("waiting_time", f"Next block {when(o['next_block_at'])}.", due=o["next_block_at"])
    cap = daily_cap(sc)
    if sc.params.get("per_day"):
        cap = min(cap, int(sc.params["per_day"]))
    room = cap - _drafted_today(sc)
    if room <= 0:
        nxt = next_morning(sc.now)
        return StepResult("waiting_time", f"Today's cap of {cap} openers is used. Next block {when(nxt)}.",
                          output={"next_block_at": nxt}, due=nxt)
    picks = _opener_candidates(sc, room)
    if not picks:
        return StepResult("blocked_human", "No prospects left to contact. Add more on the Prospects page.",
                          output={"href": "#/prospects"}, wait={"events": ["prospect_added"], "recheck": True})
    fn = feature_fn("prospects", "draft_openers")
    qids: list[int] = []
    source = "rules"
    if fn is not None:
        try:
            res = invoke(fn, sc.tenant, prospect_ids=[p["id"] for p in picks], ids=[p["id"] for p in picks],
                         user=sc.user, owner_user_id=sc.uid, limit=len(picks), mission_id=sc.mid, actor=sc.actor)
            qids = qids_from(sc.tenant, res)
            source = "prospects"
        except Exception:  # noqa: BLE001 - fall back to the generic rules openers
            log.warning("prospects.draft_openers failed; using rules openers")
            qids = []
    if not qids:
        qids = _fallback_openers(sc, picks)
        source = "rules"
    else:
        tag_queue(sc.tenant, qids, sc.mid, sc.step["id"], sc.uid)
        with sc.tenant.db() as c:
            c.execute(f"UPDATE queue SET meta=json_set(meta,'$.first_touch',1) WHERE id IN ({','.join('?' * len(qids))})", qids)
    n = o.get("block", 0) + 1
    return StepResult("waiting_approval", f"Block {n}: {len(qids)} opener(s) drafted (cap {cap} a day). Approve them in the queue.",
                      queue_ids=qids, output={"block_qids": qids, "block": n, "next_block_at": None, "source": source,
                                              "move": sc.ctx.get("opener_move", "question")})


def sent_openers(tenant, mid: int) -> list[dict]:
    return [e for e in events_of(tenant, mid, "sent") if e["detail"].get("first_touch") and e["detail"].get("prospect_id")]


def replied_ids(tenant, mid: int) -> set:
    return {e["detail"].get("party_id") for e in events_of(tenant, mid, "reply")}


@step_type("followups", "Follow-ups", "Value-only follow-ups on days 2, 4 and 7 after a confirmed send, only while the "
           "prospect hasn't replied.", wait_for="approval", outbound=True, recurring=True,
           success="Follow-ups approved; none sent after a reply")
def _followups(sc: StepCtx) -> StepResult:
    if sc.ctx.get("won"):
        return StepResult("done", "Goal won: follow-ups stopped.")
    if feature_fn("delivery", "run_cadences") is not None:
        return StepResult("waiting_event", "Follow-ups run on the follow-up cadence after each confirmed send.",
                          wait={"events": ["client_won"]}, cycle=sc.step.get("cycles", 0) == 0,
                          output={"handled_by": "cadence"})
    open_ids = q_states(sc.tenant, [q for q in (sc.step.get("queue_ids") or [])])["open"]
    replied = replied_ids(sc.tenant, sc.mid)
    with sc.tenant.db() as c:
        drafted: dict[int, int] = {}
        for r in c.execute("SELECT json_extract(meta,'$.prospect_id') pid, COUNT(*) n FROM queue WHERE kind='followup' "
                           "AND json_extract(meta,'$.mission_id')=? GROUP BY pid", (sc.mid,)):
            drafted[r["pid"]] = r["n"]
    new_ids, next_due = [], next_morning(sc.now)
    for e in sent_openers(sc.tenant, sc.mid):
        pid = e["detail"]["prospect_id"]
        k = drafted.get(pid, 0)
        if k >= len(FOLLOWUPS) or pid in replied:
            continue
        p = prospect(sc.tenant, pid, sc.uid)
        if p is None or p.get("do_not_contact") or (p.get("stage") or "contacted") not in ("new", "researched", "contacted"):
            continue
        days, body = FOLLOWUPS[k]
        due_at = e["created"] + days * DAY
        if sc.now < due_at - DAY:
            next_due = min(next_due, due_at - DAY)
            continue
        ch = "instagram_dm" if p.get("handle") else "email"
        item = draft(sc, "followup", body, channel=ch, recipient=p.get("handle") or p.get("email"),
                     title=f"Follow-up {k + 1} to {p.get('name') or 'prospect'}",
                     meta={"prospect_facing": True, "lead_tier": "cold", "prospect_id": pid, "prior_contact": True,
                           "followup": k + 1, "send_after": max(due_at, sc.now),
                           "cancel_if": ["lead_replied_since", "prospect_dnc", "stage_beyond:contacted"],
                           **({"subject": "A quick question"} if ch == "email" else {})})
        new_ids.append(item["id"])
    pending = open_ids + new_ids
    msg = (f"{len(new_ids)} follow-up(s) drafted. " if new_ids else "") + \
        (f"{len(pending)} waiting for approval." if pending else f"Next check {when(next_due)}.")
    if pending:
        return StepResult("waiting_approval", msg, queue_ids=new_ids, due=next_due, cycle=sc.step.get("cycles", 0) == 0)
    return StepResult("waiting_time", msg, due=next_due, cycle=sc.step.get("cycles", 0) == 0)


@step_type("reply_watch", "Reply handling", "Wait for prospects to reply; each positive reply moves to booking.",
           wait_for="event", recurring=True, success="Positive replies recorded and handed to booking",
           human_action="Record a positive reply", outcomes=("positive", "not_interested"))
def _reply_watch(sc: StepCtx) -> StepResult:
    if sc.ctx.get("won"):
        return StepResult("done", "Goal won.")
    reps = events_of(sc.tenant, sc.mid, "reply")
    pos = [e for e in reps if e["detail"].get("positive")]
    return StepResult("waiting_event", f"{len(reps)} repl{'y' if len(reps) == 1 else 'ies'} so far, {len(pos)} positive. "
                      "Waiting for the next one.", output={"replies": len(reps), "positive": len(pos)},
                      wait={"events": ["reply_received", "prospect_stage", "client_won"]},
                      cycle=len(pos) > (sc.step.get("cycles") or 0))


def _slots(tenant, n: int = 2) -> list[dict]:
    from .. import calendar

    return calendar.free_slots(tenant, n)


@step_type("booking", "Booking", "For each positive reply, draft a booking message with two fresh times. Waits for the "
           "booking to be made.", wait_for="approval", outbound=True, recurring=True,
           success="Calls booked (target reached, or the goal won)",
           params={"target": Param("int", None, 1, 500, help="Stop after this many booked calls")},
           human_action="Mark a call as booked", outcomes=("booked",))
def _booking(sc: StepCtx) -> StepResult:
    if sc.ctx.get("won"):
        return StepResult("done", "Goal won.")
    booked = len(events_of(sc.tenant, sc.mid, "booked"))
    target = sc.params.get("target")
    if target and booked >= int(target):
        return StepResult("done", f"{booked} call(s) booked: target of {target} reached.")
    with sc.tenant.db() as c:
        have = {r[0] for r in c.execute("SELECT json_extract(meta,'$.prospect_id') FROM queue WHERE kind='booking_dm' "
                                        "AND json_extract(meta,'$.mission_id')=?", (sc.mid,))}
        stale = db.rows(c.execute("SELECT id, meta FROM queue WHERE kind='booking_dm' AND json_extract(meta,'$.mission_id')=? "
                                  "AND status IN ('pending','needs_human') AND json_extract(meta,'$.slots_valid_until')<?",
                                  (sc.mid, sc.now)))
    for r in stale:  # proposed times went by while the draft waited: refresh them
        slots = _slots(sc.tenant)
        if len(slots) >= 2:
            try:
                queue.edit(sc.tenant, r["id"], _booking_text(slots), "system", "owner",
                           meta_patch={"slots": slots, "slots_valid_until": slots[0]["start"]})
            except queue.QueueError:
                pass
    new_ids = []
    booked_parties = {e["detail"].get("party_id") for e in events_of(sc.tenant, sc.mid, "booked")}
    for e in events_of(sc.tenant, sc.mid, "reply"):
        pid = e["detail"].get("party_id")
        if not e["detail"].get("positive") or pid in have or pid in booked_parties:
            continue
        p = prospect(sc.tenant, pid, sc.uid) if e["detail"].get("party_type", "prospect") == "prospect" else None
        rcpt = (p or {}).get("handle") or (p or {}).get("email") or e["detail"].get("handle")
        if not rcpt or (p and p.get("do_not_contact")):
            continue
        slots = _slots(sc.tenant)
        if len(slots) < 2:
            continue
        item = draft(sc, "booking_dm", _booking_text(slots), channel="instagram_dm" if (p or {}).get("handle") else "email",
                     recipient=rcpt, title=f"Booking times for {(p or {}).get('name') or 'prospect'}",
                     meta={"prospect_facing": True, "lead_tier": "warm", "prior_contact": True, "prospect_id": pid,
                           "slots": slots, "slots_valid_until": slots[0]["start"]})
        new_ids.append(item["id"])
        have.add(pid)
    open_ids = q_states(sc.tenant, sc.step.get("queue_ids") or [])["open"] + new_ids
    msg = f"{booked} call(s) booked" + (f" of {target}" if target else "") + "."
    if new_ids:
        msg += f" {len(new_ids)} booking message(s) drafted with two fresh times each."
    res_kw = {"queue_ids": new_ids, "cycle": booked > (sc.step.get("cycles") or 0),
              "wait": {"events": ["reply_received", "booking_made", "prospect_stage", "client_won"]},
              "output": {"booked": booked}}
    if open_ids:
        return StepResult("waiting_approval", msg + f" {len(open_ids)} waiting for approval.", **res_kw)
    return StepResult("waiting_event", msg + " Waiting for the next booking.", **res_kw)


def _booking_text(slots: list[dict]) -> str:
    return (f"Great to hear. Would {slots[0]['label']} or {slots[1]['label']} work for a 20-minute call? "
            "If neither suits, send me a time that does.")


@step_type("reminder_ladder", "Confirmations and reminders", "Confirm each booked call and draft 24-hour and 2-hour "
           "reminders, scheduled against the call time and cancelled if the booking is cancelled.",
           wait_for="approval", outbound=True, recurring=True, success="Every booked call has a confirmation and reminders")
def _reminder_ladder(sc: StepCtx) -> StepResult:
    if sc.ctx.get("won"):
        return StepResult("done", "Goal won.")
    with sc.tenant.db() as c:
        have = {r[0] for r in c.execute("SELECT DISTINCT json_extract(meta,'$.party_key') FROM queue WHERE kind='reminder' "
                                        "AND json_extract(meta,'$.mission_id')=?", (sc.mid,))}
    new_ids = []
    for e in events_of(sc.tenant, sc.mid, "booked"):
        d = e["detail"]
        starts = d.get("starts_at")
        key = f"{d.get('party_type', 'prospect')}:{d.get('party_id')}:{starts}"
        if not starts or key in have or starts <= sc.now:
            continue
        p = prospect(sc.tenant, d.get("party_id"), sc.uid)
        rcpt = (p or {}).get("handle") or (p or {}).get("email")
        if not rcpt:
            continue
        base = {"prospect_facing": True, "lead_tier": "warm", "prior_contact": True, "prospect_id": d.get("party_id"),
                "party_key": key, "cancel_if": ["booking_cancelled"], "starts_at": starts}
        ch = "instagram_dm" if (p or {}).get("handle") else "email"
        for label, before, body in (
                ("confirmation", None, f"You're booked for {when(starts)}. Reply here if anything changes."),
                ("24h", DAY, "Looking forward to tomorrow. One question to think about: what would ten more booked calls "
                             "a month change for you?"),
                ("2h", 2 * 3600, "See you in two hours. Have your current numbers to hand so we can use the time well.")):
            send_after = sc.now if before is None else starts - before
            if send_after < sc.now and before is not None:
                continue
            item = draft(sc, "reminder", body, channel=ch, recipient=rcpt, title=f"{label} for {(p or {}).get('name') or 'call'}",
                         meta={**base, "ladder": label, "send_after": send_after})
            new_ids.append(item["id"])
        have.add(key)
    open_ids = q_states(sc.tenant, sc.step.get("queue_ids") or [])["open"] + new_ids
    if open_ids:
        return StepResult("waiting_approval", f"{len(open_ids)} confirmation/reminder message(s) waiting for approval.",
                          queue_ids=new_ids, cycle=sc.step.get("cycles", 0) == 0, wait={"events": ["booking_made"]})
    return StepResult("waiting_event", "Every booked call has its confirmation and reminders. Waiting for the next booking.",
                      wait={"events": ["booking_made", "client_won"]}, cycle=sc.step.get("cycles", 0) == 0)


# --------------------------------------------------------------------------- the call
def _last_party(sc: StepCtx, *kinds: str) -> dict | None:
    """The latest event of these kinds, preferring one that names who it was with."""
    evs = events_of(sc.tenant, sc.mid, *kinds)
    named = [e for e in evs if e["detail"].get("party_id") is not None]
    pick = (named or evs)[-1]["detail"] if evs else None
    if pick is not None and pick.get("party_type", "prospect") == "prospect" and not pick.get("name"):
        p = prospect(sc.tenant, pick.get("party_id"), sc.uid) if isinstance(pick.get("party_id"), int) else None
        if p:
            pick = {**pick, "name": p.get("name")}
    return pick


@step_type("precall_prep", "Pre-call brief and practice", "Write a pre-call brief from the research and the replies, "
           "and practise the call with a role-play and objection drills.", success="Brief ready before the call")
def _precall_prep(sc: StepCtx) -> StepResult:
    from .. import retrieval

    d = _last_party(sc, "booked") or {}
    p = prospect(sc.tenant, d.get("party_id"), sc.uid) or {}
    facts = [f for f in ((p.get("research") or {}).get("facts") or []) if isinstance(f, dict)][:6]
    hits = retrieval.search(sc.tenant, "pre-call brief goal current numbers main obstacle what they said in the DMs", k=2)
    cites = [retrieval.cite({**h, "id": f"S{i + 1}"}) for i, h in enumerate(hits)]
    brief = {
        "who": p.get("name") or d.get("name") or "Your prospect",
        "what_they_do": ", ".join(x for x in (p.get("niche"), p.get("offer")) if x) or "Not researched yet",
        "facts": [{"claim": f.get("claim"), "source_url": f.get("source_url")} for f in facts],
        "said": d.get("note") or "See the conversation in your DMs",
        "ask": ["What's the goal for the next 90 days?", "What are the numbers today?", "What have you tried?",
                "Why now?"],
        "prepare_for": ["I need to think about it", "It's too expensive", "I need to ask my partner"],
        "rules": "No promised results and no income claims on the call. Only share results you can prove.",
        "citations": cites,
    }
    text = " ".join([brief["who"], brief["what_they_do"], brief["said"]] + [str(f.get("claim") or "") for f in brief["facts"]])
    flags = guardrails.check_for(sc.tenant, text, {})
    if feature("academy") is not None:
        assign_path(sc, "closer", "L1")
    return StepResult("done", f"Pre-call brief ready for {brief['who']}. Practise with the objection drills before the call.",
                      output={"brief": brief, "flags": flags, "href": "#/drills"})


@step_type("take_call", "Take the call", "You take the discovery call, then say how it went.", wait_for="event",
           success="Call happened", human_action="Say how the call went", outcomes=("showed", "no_show", "rescheduled"),
           planner=True)
def _take_call(sc: StepCtx) -> StepResult:
    d = _last_party(sc, "booked") or {}
    showed = events_of(sc.tenant, sc.mid, "showed")
    if showed and showed[-1]["created"] >= (sc.step.get("created") or 0):
        return StepResult("done", "Call done: they showed up.")
    at = f" at {when(d['starts_at'])}" if d.get("starts_at") else ""
    no_shows = len(events_of(sc.tenant, sc.mid, "no_show"))
    extra = f" {no_shows} no-show(s) so far; the booking step keeps offering new times." if no_shows else ""
    return StepResult("blocked_human", f"Take your call with {d.get('name') or 'your prospect'}{at}, then mark how it went.{extra}",
                      wait={"events": ["booking_made", "booking_cancelled"]})


@step_type("call_review", "Call review (optional)", "Review the call recording or notes against the five call stages.",
           success="Call reviewed, biggest leak named", human_action="Mark reviewed")
def _call_review(sc: StepCtx) -> StepResult:
    if sc.ctx.get("won"):
        return StepResult("skipped", "Goal won before this step was needed.")
    uname = (sc.user or {}).get("username") or ""
    with sc.tenant.db() as c:
        r = db.row(c.execute("SELECT id, review FROM calls WHERE status='reviewed' AND rep=? AND created>=? ORDER BY id DESC "
                             "LIMIT 1", (uname, sc.step.get("created") or 0)).fetchone())
    if r:
        rev = db.jload(r["review"], {}) or {}
        leak = (rev.get("biggest_leak") or {}).get("label")
        return StepResult("done", f"Call reviewed (score {rev.get('overall', '?')}/100)" + (f"; biggest leak: {leak}." if leak else "."),
                          output={"call_id": r["id"]})
    return StepResult("blocked_human", "Optional: add the call transcript in Call review (it needs every party's consent), "
                      "ask your coach to review it, or skip this step.", wait={"events": [], "recheck": True})


@step_type("proposal", "Proposal", "Turn the call into a proposal (their problem, the plan, what's included, your price, "
           "no guarantees) and send it after approval.", wait_for="approval", outbound=True,
           success="Proposal approved and sent", feature="proposals")
def _proposal(sc: StepCtx) -> StepResult:
    qids = sc.out.get("qids") or []
    if qids:
        return _approval_result(sc, qids, "Proposal", "Proposal approved.")
    if sc.ctx.get("won"):
        return StepResult("skipped", "Goal won before a proposal was needed.")
    d = _last_party(sc, "showed", "booked") or {}
    pid = d.get("party_id")
    fn = feature_fn("proposals", "build")
    if fn is not None and pid:
        try:
            res = invoke(fn, sc.tenant, prospect_id=pid, user=sc.user, owner_user_id=sc.uid, mission_id=sc.mid,
                         call_notes=sc.ctx.get("call_notes") or "", actor=sc.actor)
            got = qids_from(sc.tenant, res)
            if got:
                tag_queue(sc.tenant, got, sc.mid, sc.step["id"], sc.uid)
                return StepResult("waiting_approval", "Proposal drafted: approve it in the queue.", queue_ids=got,
                                  output={"qids": got, "source": "proposals"})
            return StepResult("blocked_human", "Proposal drafted: review it and send it for approval on the Prospects page.",
                              output={"source": "proposals", "href": "#/prospects"}, wait={"events": ["item_sent"]})
        except Exception:  # noqa: BLE001 - fall back to the rules proposal
            log.warning("proposals.build failed; using the rules proposal")
    p = prospect(sc.tenant, pid, sc.uid) or {}
    a = sc.answers
    who = p.get("name") or "your business"
    body = (f"Proposal for {who}\n\nWhere you are: {sc.ctx.get('call_notes') or 'what you told me on the call'}.\n"
            f"What we'll do: {a.get('offer') or 'the programme we discussed'}.\n"
            "First 30 days: agree the numbers, fix the one biggest bottleneck, set up weekly reporting.\n"
            "Days 31-60: build on what worked; test one new thing at a time.\n"
            "Days 61-90: review together and decide what's next.\n"
            "What's included: a kickoff call, weekly numbers, one review call a month.\n"
            f"Investment: {money(a.get('price'))} a month.\n"
            "We don't guarantee specific results; we commit to the work and to honest weekly numbers.")
    rcpt = p.get("email") or p.get("handle")
    ch = "email" if p.get("email") else "instagram_dm" if p.get("handle") else "manual"
    item = draft(sc, "proposal", body, channel=ch, recipient=rcpt,
                 title=f"Proposal for {who}", meta={"client_asset": True, "prospect_id": pid, "subject": f"Proposal for {who}"})
    return StepResult("waiting_approval", "Proposal drafted from the template: check the details, then approve it.",
                      queue_ids=[item["id"]], output={"qids": [item["id"]], "source": "rules"})


@step_type("await_win", "Win the client", "Wait for the proposal to be accepted.", wait_for="event",
           success="Client won", human_action="Mark as won", outcomes=("won", "lost"))
def _await_win(sc: StepCtx) -> StepResult:
    if sc.ctx.get("won"):
        return StepResult("done", "Client won.", output={"client_id": sc.ctx.get("client_id")})
    if events_of(sc.tenant, sc.mid, "lost") and not sc.out.get("acknowledged_lost"):
        return StepResult("blocked_human", "Marked as lost. Keep going: the opener and booking steps are still running. "
                          "Mark as won when a client says yes.", output={"acknowledged_lost": True},
                          wait={"events": ["client_won", "prospect_stage"]})
    return StepResult("waiting_event", "Waiting for a yes. Mark it as won when they accept.",
                      wait={"events": ["client_won", "prospect_stage"]})


@step_type("client_onboarding_start", "Start client onboarding", "When a client is won, start their onboarding.",
           success="Onboarding started", feature="client_ops")
def _client_onboarding_start(sc: StepCtx) -> StepResult:
    cid = sc.ctx.get("client_id")
    fn = feature_fn("client_ops", "start_onboarding")
    if fn is not None and cid:
        try:
            invoke(fn, sc.tenant, client_id=cid, user=sc.user, owner_user_id=sc.uid, mission_id=sc.mid, actor=sc.actor)
            return StepResult("done", "Client onboarding started.", output={"client_id": cid, "source": "client_ops"})
        except Exception:  # noqa: BLE001 - fall back to an onboarding mission
            log.warning("client_ops.start_onboarding failed; starting an onboarding mission")
    return StepResult("done", "Started a client onboarding mission.",
                      spawn=[{"template": "client_onboarding", "goal": "Onboard my new client",
                              "answers": {"client_id": cid} if cid else {}}])


# --------------------------------------------------------------------------- measure & correct
@step_type("checkpoint", "Checkpoint", "On the given day, measure progress against the plan and suggest changes if "
           "the numbers say so.", wait_for="time", success="Progress measured; plan changes suggested where needed",
           params={"day": Param("int", 7, 1, 365)})
def _checkpoint(sc: StepCtx) -> StepResult:
    from . import evaluator

    day = int(sc.params.get("day") or 7)
    due = (sc.mission.get("created") or sc.now) + day * DAY
    if sc.now < due:
        return StepResult("waiting_time", f"Day {day} check on {when(due)}.", due=due)
    if sc.ctx.get("won"):
        return StepResult("done", f"Day {day}: goal already won.")
    res = evaluator.evaluate(sc.tenant, sc.mission, now=sc.now, actor=sc.actor)
    findings = res.get("findings") or []
    msg = f"Day {day} check: " + ("; ".join(f["why"] for f in findings) if findings else "on track, no change needed.")
    return StepResult("done", msg, output={"metrics": res.get("metrics"), "findings": findings,
                                           "replan_ids": res.get("replan_ids", [])})


NUMBER_KEYS = ("dms_sent", "replies", "conversations", "links_sent", "booked", "showed", "closed", "ad_spend", "leads",
               "visitors", "registrants", "attendees", "webinar_sales", "offer_price")


def mission_numbers(tenant, mission: dict) -> dict:
    """Funnel numbers for the diagnostic: the user's own numbers from intake, else this mission's measured ones."""
    a = (mission.get("context") or {}).get("answers") or {}
    nums = {k: float(a[k]) for k in NUMBER_KEYS if isinstance(a.get(k), (int, float))}
    if len([k for k in nums if k != "offer_price"]) >= 2:
        return nums
    from . import evaluator

    m = evaluator.metrics(tenant, mission)
    lead, lag = m["leading"], m["lagging"]
    out = {"dms_sent": lead["openers_sent"], "replies": lead["replies"], "booked": lag["calls_booked"],
           "showed": lag["calls_showed"], "closed": lag["wins"]}
    if a.get("price"):
        out["offer_price"] = float(a["price"])
    return {k: v for k, v in out.items() if v}


@step_type("diagnose", "Find the bottleneck", "Compare each stage with the SOP benchmarks and name the one bottleneck "
           "worth fixing first.", success="Bottleneck named with its money impact")
def _diagnose(sc: StepCtx) -> StepResult:
    from .. import diagnostic

    nums = mission_numbers(sc.tenant, sc.mission)
    if len([k for k in nums if k != "offer_price"]) < 2:
        return StepResult("blocked_human", "Enter at least two of your current numbers (for example DMs sent and replies) "
                          "so the bottleneck can be found.", wait={"events": ["context_updated"]})
    res = diagnostic.diagnose(sc.tenant, nums, user=sc.user, label=f"mission #{sc.mid}")
    bn = res.get("bottleneck")
    patch = {"diagnosis": {"bottleneck": bn and {k: bn[k] for k in ("key", "label", "value", "benchmark", "monthly_cost")},
                           "run_id": res.get("run_id")},
             "next_best_action": res.get("next_best_action")}
    msg = res.get("prescription") or "Diagnosis done."
    return StepResult("done", msg[:400], output={"bottleneck": bn, "run_id": res.get("run_id"),
                                                 "next_best_action": res.get("next_best_action")}, context=patch)


@step_type("studio_line", "Draft the fix", "Run the Studio line for the fix (drafts reviewed against the rubric and the "
           "guardrails), then approve them.", wait_for="approval", outbound=True, success="Drafts approved",
           params={"line": Param("enum", None, choices=("outreach", "ads", "funnels", "creatives", "webinars", "training",
                                                         "delivery"))})
def _studio_line(sc: StepCtx) -> StepResult:
    from .. import studio

    qids = sc.out.get("qids") or []
    if qids:
        return _approval_result(sc, qids, "Studio drafts", "Studio drafts approved.")
    line = sc.params.get("line") or ((sc.ctx.get("next_best_action") or {}).get("line")) or "outreach"
    if line not in studio.LINES:
        line = "outreach"
    a = sc.answers
    brief = {k: a.get(k) for k in ("niche", "offer", "price", "outcome", "audience") if a.get(k)}
    res = studio.run_line(sc.tenant, line, brief, sc.user)
    got = qids_from(sc.tenant, res.get("queue_ids"))
    tag_queue(sc.tenant, got, sc.mid, sc.step["id"], sc.uid)
    if not got:
        return StepResult("done", f"Studio ({studio.LINES[line]}) had nothing to queue.", output={"run_id": res.get("run_id")})
    return StepResult("waiting_approval", f"{len(got)} Studio draft(s) for {studio.LINES[line]} waiting for approval.",
                      queue_ids=got, output={"qids": got, "line": line, "child_run_id": res.get("run_id")})


@step_type("remeasure", "Re-measure", "After the fix has had time to work, enter the new numbers and compare.",
           wait_for="time", success="Before/after comparison recorded", params={"days": Param("int", 14, 1, 120)},
           human_action="Enter the new numbers", outcomes=("numbers",))
def _remeasure(sc: StepCtx) -> StepResult:
    from .. import diagnostic

    days = int(sc.params.get("days") or 14)
    start = sc.out.get("started") or sc.now
    due = start + days * DAY
    after = sc.ctx.get("after_numbers")
    if not after:
        if sc.now < due:
            return StepResult("waiting_time", f"Give the fix {days} days. Re-measure on {when(due)}.", due=due,
                              output={"started": start})
        return StepResult("blocked_human", f"It's been {days} days: enter your numbers since the fix to compare.",
                          output={"started": start}, wait={"events": ["context_updated"]})
    before = diagnostic.analyse(mission_numbers(sc.tenant, sc.mission))
    now_ = diagnostic.analyse(after)
    key = ((sc.ctx.get("diagnosis") or {}).get("bottleneck") or {}).get("key")
    b = next((r for r in before["stages"] if r["key"] == key), None)
    n = next((r for r in now_["stages"] if r["key"] == key), None)
    if b and n:
        better = (n["value"] > b["value"]) if n["kind"] == "rate" else (n["value"] < b["value"])
        msg = f"{b['label']}: {b['value']} before, {n['value']} after ({'better' if better else 'not better yet'})."
        worked = list(sc.ctx.get("what_worked") or [])
        if better:
            worked.append(f"Fixing {b['label'].lower()} moved it from {b['value']} to {n['value']}.")
        return StepResult("done", msg, output={"before": b, "after": n}, context={"what_worked": worked[-20:]})
    return StepResult("done", "New numbers recorded.", output={"after": after})


# --------------------------------------------------------------------------- webinars
@step_type("webinar_chain", "Webinar assets", "Run the webinar chain: offer, title and hooks, slide script, pages, "
           "show-up and replay emails, run-of-show and the sales hand-off, each queued for approval.",
           wait_for="approval", outbound=True, success="Webinar assets approved")
def _webinar_chain(sc: StepCtx) -> StepResult:
    from .. import webinar_chain

    qids = sc.out.get("qids") or []
    if qids:
        return _approval_result(sc, qids, "Webinar assets", "Webinar assets approved.")
    a = sc.answers
    brief = {k: a.get(k) for k in ("niche", "offer", "price", "outcome", "audience") if a.get(k)}
    if a.get("event_date"):
        brief["date"] = when(a["event_date"])
    res = webinar_chain.run_chain(sc.tenant, brief, sc.user)
    got = qids_from(sc.tenant, res.get("queue_ids"))
    tag_queue(sc.tenant, got, sc.mid, sc.step["id"], sc.uid)
    return StepResult("waiting_approval", f"{len(got)} webinar asset(s) drafted: approve them in the queue.", queue_ids=got,
                      output={"qids": got, "chain_id": res.get("chain_id"), "child_run_id": res.get("run_id")},
                      context={"chain_id": res.get("chain_id")})


@step_type("webinar_schedule", "Schedule the webinar", "Set the event date and the registration page.",
           success="Event scheduled", feature="webinars")
def _webinar_schedule(sc: StepCtx) -> StepResult:
    at = sc.answers.get("event_date")
    fn = feature_fn("webinars", "schedule", "create", "create_webinar")
    if fn is not None and at:
        try:
            res = invoke(fn, sc.tenant, chain_id=sc.ctx.get("chain_id"), starts_at=at, user=sc.user, mission_id=sc.mid,
                         title=sc.answers.get("offer") or sc.mission.get("goal"), actor=sc.actor)
            wid = res.get("id") if isinstance(res, dict) else res
            return StepResult("done", f"Webinar scheduled for {when(at)}.", output={"webinar": wid}, context={"webinar_id": wid})
        except Exception:  # noqa: BLE001
            log.warning("webinars scheduling failed; recording the date only")
    if not at:
        return StepResult("blocked_human", "Pick the event date to schedule the webinar.", wait={"events": ["context_updated"]})
    return StepResult("done", f"Event date recorded: {when(at)}. Publish the registration page from the Webinar page "
                              "when the assets are approved.", output={"href": "#/webinar"})


@step_type("wait_until", "Wait for the date", "Wait until a date from the intake answers (for example the event day).",
           wait_for="time", success="Date reached", params={"at_key": Param("enum", "event_date", choices=("event_date",)),
                                                            "label": Param("str", "")})
def _wait_until(sc: StepCtx) -> StepResult:
    at = sc.answers.get(sc.params.get("at_key") or "event_date")
    label = sc.params.get("label") or "the date"
    if not at:
        return StepResult("blocked_human", "Pick the date first.", wait={"events": ["context_updated"]})
    if sc.now < float(at):
        return StepResult("waiting_time", f"Waiting for {label}: {when(at)}.", due=float(at))
    return StepResult("done", f"{label[:1].upper() + label[1:]} reached.")


@step_type("human_task", "Your task", "Something only a person can do; mark it done when it's done.", wait_for="event",
           success="Marked done", human_action="Mark done", outcomes=("done",),
           params={"task": Param("str", "Do the task"), "href": Param("str", "")})
def _human_task(sc: StepCtx) -> StepResult:
    return StepResult("blocked_human", str(sc.params.get("task") or "Do the task, then mark it done."),
                      output={"href": sc.params.get("href") or None}, wait={"events": []})


# --------------------------------------------------------------------------- ads
@step_type("ads_diagnose", "Read the ad account", "Read the ad account (read-only) and apply the kill and winner rules.",
           success="Kill list and winner named", params={"target_cpl": Param("float", 10.0, 0.5, 10000)})
def _ads_diagnose(sc: StepCtx) -> StepResult:
    from ..agents import ads_ops

    cpl = float(sc.answers.get("target_cpl") or sc.params.get("target_cpl") or 10.0)
    res = ads_ops.diagnose(sc.tenant, None, cpl, sc.user)
    kills = [a["name"] for a in res["ads"] if a["kill"]]
    win = (res.get("winner") or {}).get("name")
    msg = f"{len(res['ads'])} ads read (nothing changed). {len(kills)} hit the kill rule" + (f"; winner: {win}." if win else ".")
    return StepResult("done", msg, output={"kill": kills, "winner": win, "child_run_id": res.get("run_id")})


@step_type("ads_propose", "Propose ad changes", "Queue one owner approval per ad that should be paused. Nothing changes "
           "until it's approved.", wait_for="approval", outbound=True, success="Changes decided",
           params={"target_cpl": Param("float", 10.0, 0.5, 10000)})
def _ads_propose(sc: StepCtx) -> StepResult:
    from ..agents import ads_ops

    qids = sc.out.get("qids")
    if qids is not None:
        if not qids:
            return StepResult("done", "No ad needed pausing.")
        return _approval_result(sc, qids, "Ad changes", "Ad changes decided.")
    cpl = float(sc.answers.get("target_cpl") or sc.params.get("target_cpl") or 10.0)
    res = ads_ops.propose_changes(sc.tenant, None, cpl, sc.user)
    got = [p["queue_id"] for p in res.get("proposals", []) if p.get("queue_id")]
    tag_queue(sc.tenant, got, sc.mid, sc.step["id"], sc.uid)
    if not got:
        return StepResult("done", "No ad needed pausing.", output={"qids": []})
    return StepResult("waiting_approval", f"{len(got)} ad change(s) waiting for the owner's approval.", queue_ids=got,
                      output={"qids": got})


@step_type("ads_test", "Build a new creative test", "Build a Thunder Dome test (paused, zero budget) from the best hooks.",
           success="Paused test built", params={"name": Param("str", "Mission test")})
def _ads_test(sc: StepCtx) -> StepResult:
    from ..agents import ads_ops

    b = _brief(sc)["brief"]
    hooks = [f"Stop relying on {b['pain']}.", f"{b['outcome'][:1].upper() + b['outcome'][1:]} in {b['timeframe']}.",
             f"The {b['mechanism']}, explained."]
    res = ads_ops.build_test(sc.tenant, str(sc.params.get("name") or "Mission test")[:60], hooks, None, sc.user)
    q = res.get("queue_id")
    if q:
        tag_queue(sc.tenant, [q], sc.mid, sc.step["id"], sc.uid)
    return StepResult("done", "Test built paused with zero budget; going live needs an owner approval."
                      if not q else "Test proposed: it waits for the owner's approval.", output={"action": res.get("action_id")})


# --------------------------------------------------------------------------- students & clients
@step_type("onboard_student", "Onboard the student", "Intake, gaps, cited lessons, a 30/60/90 plan, a coach and "
           "milestone tasks; the welcome and kickoff are queued for approval.", wait_for="approval", outbound=True,
           success="Student onboarded")
def _onboard_student(sc: StepCtx) -> StepResult:
    from ..agents import onboarding

    qids = sc.out.get("qids")
    if qids is not None:
        return _approval_result(sc, qids, "Welcome and kickoff", "Student onboarded; welcome and kickoff decided.") if qids \
            else StepResult("done", "Student onboarded.")
    if (sc.user or {}).get("role") == "student":
        return StepResult("skipped", "You're already onboarded.")
    a = sc.answers
    if not a.get("name"):
        return StepResult("blocked_human", "Enter the student's name to onboard them.", wait={"events": ["context_updated"]})
    intake = {k: a.get(k) for k in ("name", "email", "niche", "clients", "usp") if a.get(k) not in (None, "")}
    res = onboarding.onboard(sc.tenant, intake, sc.user)
    got = qids_from(sc.tenant, res.get("queue_ids"))
    tag_queue(sc.tenant, got, sc.mid, sc.step["id"], sc.uid)
    return StepResult("waiting_approval" if got else "done", "Student onboarded; the welcome and kickoff wait for approval.",
                      queue_ids=got, output={"qids": got, "student_id": res.get("student_id"), "child_run_id": res.get("run_id")},
                      context={"student_id": res.get("student_id")})


def _my_client(sc: StepCtx, cid) -> dict | None:
    if not cid:
        return None
    with sc.tenant.db() as c:
        cl = db.row(c.execute("SELECT * FROM clients WHERE id=?", (int(cid),)).fetchone())
    if cl is None:
        return None
    if (sc.user or {}).get("role") != "owner":
        sid = (sc.user or {}).get("student_id")
        if cl.get("student_id") not in (sid,) and sc.ctx.get("client_id") != cl["id"]:
            return None
    return cl


@step_type("fulfil_client", "Client plan", "Money model, bottleneck, cited launch plan, reviewed ad hooks, VSL outline "
           "and the first client report, all queued for approval.", wait_for="approval", outbound=True,
           success="Client plan approved", feature="client_ops")
def _fulfil_client(sc: StepCtx) -> StepResult:
    from ..agents import fulfilment

    qids = sc.out.get("qids")
    if qids is not None:
        return _approval_result(sc, qids, "Client plan", "Client plan approved.") if qids else StepResult("done", "Done.")
    cid = sc.answers.get("client_id") or sc.ctx.get("client_id")
    cl = _my_client(sc, cid)
    if cl is None:
        return StepResult("blocked_human", "Choose the client this is for (the client is added when you win them).",
                          wait={"events": ["client_won", "context_updated"]})
    fn = feature_fn("client_ops", "start_onboarding")
    if fn is not None:
        try:
            res = invoke(fn, sc.tenant, client_id=cl["id"], user=sc.user, owner_user_id=sc.uid, mission_id=sc.mid,
                         actor=sc.actor)
            got = qids_from(sc.tenant, res)
            tag_queue(sc.tenant, got, sc.mid, sc.step["id"], sc.uid)
            return StepResult("waiting_approval" if got else "done", "Client onboarding started.", queue_ids=got,
                              output={"qids": got, "source": "client_ops"}, context={"client_id": cl["id"]})
        except Exception:  # noqa: BLE001
            log.warning("client_ops.start_onboarding failed; using fulfilment")
    res = fulfilment.fulfil(sc.tenant, cl["id"], sc.user)
    got = qids_from(sc.tenant, res.get("queue_ids"))
    tag_queue(sc.tenant, got, sc.mid, sc.step["id"], sc.uid)
    return StepResult("waiting_approval" if got else "done", f"Client plan for {cl['name']} drafted: approve it in the queue.",
                      queue_ids=got, output={"qids": got, "child_run_id": res.get("run_id")}, context={"client_id": cl["id"]})


WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def next_weekday(now: float, weekday: int, hour: int = 9) -> float:
    lt = time.localtime(now)
    for d in range(0, 8):
        t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + d, hour, 0, 0, 0, 0, -1))
        if time.localtime(t).tm_wday == weekday and t > now:
            return t
    return now + 7 * DAY


@step_type("weekly_client_ops", "Weekly client report", "Every week on the chosen day: read each client's numbers, "
           "name the bottleneck and draft the client report for approval.", wait_for="approval", outbound=True,
           recurring=True, success="A report approved every week",
           params={"weekday": Param("enum", "monday", choices=WEEKDAYS)}, feature="client_ops")
def _weekly_client_ops(sc: StepCtx) -> StepResult:
    from .. import diagnostic

    o = sc.out
    block = o.get("block_qids") or []
    wd = WEEKDAYS.index(str(sc.answers.get("weekday") or sc.params.get("weekday") or "monday").lower()) \
        if str(sc.answers.get("weekday") or sc.params.get("weekday") or "monday").lower() in WEEKDAYS else 0
    if block:
        st = q_states(sc.tenant, block)
        if st["open"]:
            return StepResult("waiting_approval", f"{len(st['open'])} weekly report(s) waiting for approval.", queue_ids=block)
        nxt = next_weekday(sc.now, wd)
        return StepResult("waiting_time", f"This week's reports decided. Next run {when(nxt)}.",
                          output={"block_qids": [], "next_at": nxt}, due=nxt, cycle=True)
    if o.get("next_at") and sc.now < o["next_at"]:
        return StepResult("waiting_time", f"Next run {when(o['next_at'])}.", due=o["next_at"])
    cid = sc.answers.get("client_id") or sc.ctx.get("client_id")
    clients = []
    with sc.tenant.db() as c:
        if cid:
            cl = _my_client(sc, cid)
            clients = [cl] if cl else []
        elif (sc.user or {}).get("role") == "owner":
            clients = db.rows(c.execute("SELECT * FROM clients ORDER BY id LIMIT 50"))
        elif (sc.user or {}).get("student_id"):
            clients = db.rows(c.execute("SELECT * FROM clients WHERE student_id=? ORDER BY id LIMIT 50",
                                        ((sc.user or {}).get("student_id"),)))
    if not clients:
        return StepResult("blocked_human", "No clients yet: this runs once you have one.",
                          wait={"events": ["client_won", "context_updated"], "recheck": True})
    fn = feature_fn("client_ops", "weekly_ops")
    ids: list[int] = []
    for cl in clients:
        got: list[int] = []
        if fn is not None:
            try:
                got = qids_from(sc.tenant, invoke(fn, sc.tenant, client_id=cl["id"], user=sc.user, owner_user_id=sc.uid,
                                                  mission_id=sc.mid, actor=sc.actor))
                tag_queue(sc.tenant, got, sc.mid, sc.step["id"], sc.uid)
            except Exception:  # noqa: BLE001
                got = []
        if not got:
            m = db.jload(cl.get("metrics"), {}) or {}
            an = diagnostic.analyse({**m, "offer_price": cl.get("price") or 0})
            bn = an.get("bottleneck")
            body = (f"Subject: Your weekly numbers\nHi,\nHere's this week's summary for {cl['name']}.\n"
                    + (f"The one thing we're fixing: {bn['label'].lower()} ({bn['value']:.0%} against a benchmark of "
                       f"{bn['benchmark']:.0%}).\n" if bn and bn["kind"] == "rate" else "Every stage with enough data is at or above benchmark.\n")
                    + "Next week: one change at a time, and we'll report the numbers again next week.\nReply with any questions.")
            got = [draft(sc, "client_report", body, channel="email", title=f"Weekly report: {cl['name']}",
                         meta={"client_asset": True, "client_id": cl["id"], "subject": "Your weekly numbers"})["id"]]
        ids += got
    return StepResult("waiting_approval", f"{len(ids)} weekly report(s) drafted for approval.", queue_ids=ids,
                      output={"block_qids": ids, "next_at": None})


@step_type("spawn_mission", "Start the next mission", "Start a follow-on mission from a template.",
           success="Follow-on mission started", params={"template": Param("str", "first_client")})
def _spawn_mission(sc: StepCtx) -> StepResult:
    from . import templates

    t = str(sc.params.get("template") or "first_client")
    if templates.get(t) is None:
        return StepResult("failed", "Unknown template.")
    if sc.out.get("child_id"):
        return StepResult("done", f"Started mission #{sc.out['child_id']}.")
    return StepResult("done", f"Starting a '{templates.get(t).label}' mission.",
                      spawn=[{"template": t, "goal": templates.get(t).label, "answers": dict(sc.answers)}])


@step_type("escalate", "Escalate to a coach", "Raise an escalation for a coach, with the diagnostic attached.",
           success="Escalation raised", params={"kind": Param("str", "mission_off_track"), "detail": Param("str", "")})
def _escalate(sc: StepCtx) -> StepResult:
    from . import evaluator

    eid = evaluator.escalate(sc.tenant, sc.mission, str(sc.params.get("kind") or "mission_off_track"),
                             str(sc.params.get("detail") or "Mission needs a coach's eyes"))
    return StepResult("done", "A coach has been asked to look at this mission.", output={"escalation": eid})


def is_outbound_kind(kind: str) -> bool:
    t = REGISTRY.get(kind)
    return bool(t and t.outbound)
