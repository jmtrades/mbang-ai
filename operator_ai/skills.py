"""Business profile (8 sections, read by every skill), the skills library, workers with daily /
weekly triggers and an on-source-change trigger. Workers only ever produce drafts; a worker
whose skill needs a login that isn't in the vault names the missing login instead of running."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from . import brief as brief_mod
from . import db, guardrails, queue, retrieval, runtime, tenancy, vault

PROFILE_SECTIONS = {
    "identity": ["name", "tagline", "owner_display", "postal_address"],  # postal_address: footer of prospect emails
    "offer": ["name", "price", "outcome", "timeframe", "mechanism", "guarantee_policy"],
    "audience": ["who", "niche", "pain", "desire"],
    "proof": ["verified_results"],
    "voice": ["tone", "words_to_use", "words_to_avoid"],
    "channels": ["list"],
    "faq": ["text"],
    "constraints": ["never_say", "legal_notes"],
}


def get_profile(tenant) -> dict:
    p = brief_mod.profile(tenant)
    return {s: {f: (p.get(s, {}) or {}).get(f, "") for f in fs} for s, fs in PROFILE_SECTIONS.items()}


def put_profile(tenant, data: dict, actor: str) -> dict:
    clean = {s: {f: str((data.get(s, {}) or {}).get(f, ""))[:2000] for f in fs} for s, fs in PROFILE_SECTIONS.items()}
    with tenant.db() as c:
        c.execute("INSERT INTO profiles(scope,data,updated) VALUES('business',?,?) ON CONFLICT(scope) DO UPDATE SET "
                  "data=excluded.data, updated=excluded.updated", (db.jdump(clean), db.now()))
    from . import audit

    audit.log(tenant, actor, "settings", {"profile": "business"})
    return clean


def profile_context(tenant) -> str:
    p = get_profile(tenant)
    bits = [f"{s}: " + "; ".join(f"{k}={v}" for k, v in vals.items() if v) for s, vals in p.items()]
    return "\n".join(b for b in bits if not b.endswith(": "))


@dataclass
class Skill:
    key: str
    name: str
    description: str
    needs: list[str]          # vault fields required (empty = none)
    asset: str
    kind: str
    fn: Callable[[dict, dict], str]
    meta: dict


def _p(profile: dict, s: str, f: str, default: str) -> str:
    return (profile.get(s, {}) or {}).get(f) or default


SKILLS: dict[str, Skill] = {}


def _reg(key, name, desc, asset, kind, fn, needs=None, meta=None):
    SKILLS[key] = Skill(key, name, desc, needs or [], asset, kind, fn, meta or {})


_reg("dm_opener", "DM opener", "A question-led opener for a prospect (no price).", "dm_script", "dm_opener",
     lambda i, p: f"Saw your post about {i.get('topic') or _p(p, 'audience', 'pain', 'growth')}. "
                  f"What's the main thing holding new bookings back right now?",
     meta={"prospect_facing": True, "lead_tier": "cold"})
_reg("value_followup", "Value follow-up", "A follow-up that gives value: tip, video or question.", "followup", "followup",
     lambda i, p: f"Here's a quick tip on {_p(p, 'offer', 'mechanism', 'our method')}: start with one change this week. "
                  f"Want the 2-minute video?", meta={"prospect_facing": True, "lead_tier": "cold", "prior_contact": True})
_reg("usp_builder", "USP builder", "The USP formula filled from the profile.", "usp", "usp",
     lambda i, p: f"I help {_p(p, 'audience', 'who', 'clinic owners')} get {_p(p, 'offer', 'outcome', 'more bookings')} in "
                  f"{_p(p, 'offer', 'timeframe', '90 days')} through {_p(p, 'offer', 'mechanism', 'our system')} without "
                  f"{_p(p, 'audience', 'pain', 'discounting')}.")
_reg("ad_hooks", "Ad hooks", "Three short hooks for a Thunder Dome test.", "ad", "ad_hooks",
     lambda i, p: "\n".join([f"Stop relying on {_p(p, 'audience', 'pain', 'discounting')}.",
                            f"{_p(p, 'offer', 'outcome', 'More bookings')} in {_p(p, 'offer', 'timeframe', '90 days')}.",
                            f"The {_p(p, 'offer', 'mechanism', 'system')}, explained. Book a call."]), meta={"client_asset": True})
_reg("webinar_outline", "Webinar outline", "Seven segments with SOP timings.", "webinar", "webinar_outline",
     lambda i, p: "\n".join(f"Segment {n}: {t} ({m} min)" for n, (t, m) in enumerate(
         [("Hook and promise", 5), ("Origin and credibility", 7), ("The big mistake", 10), ("The new mechanism", 15),
          ("Proof framework", 10), ("The offer", 12), ("Questions and close", 6)], start=1)), meta={"client_asset": True})
_reg("objection_answers", "Objection answers", "Course answers to the three common objections.", "script", "training",
     lambda i, p: "Think about it: ask what specifically. Too expensive: go back to the gap. Partner: book a call with both.")
_reg("client_report", "Client report", "A weekly client report email.", "email", "client_report",
     lambda i, p: "Subject: Your weekly numbers\nHere's this week's funnel and the one bottleneck we're fixing next. Reply with questions.",
     meta={"client_asset": True})
_reg("checkin_nudge", "Check-in nudge", "A kind nudge for a quiet student.", "generic", "nudge",
     lambda i, p: f"Hey {i.get('first') or 'there'}, we missed your check-in. What's one small win from this week?")
_reg("reminder_ladder", "Reminder ladder", "24h / 2h / 10 min reminders, each with value.", "reminder", "reminder",
     lambda i, p: "24h: one question to think about before we talk.\n2h: a short video worth a look.\n10 min: have your numbers handy.")
_reg("content_week", "Content week", "Seven post ideas tied to the offer.", "generic", "content_plan",
     lambda i, p: "\n".join(["Mon: the big mistake", "Tue: one tip", "Wed: behind the scenes", "Thu: the mechanism",
                             "Fri: a member question", "Sat: myth vs fact", "Sun: recap"]))
_reg("instagram_post", "Instagram post (scheduled)", "Drafts a post and schedules it on Instagram (needs the Instagram login).",
     "content_post", "post", lambda i, p: f"One thing every {_p(p, 'audience', 'who', 'owner')} should check this week. Comment 'guide'.",
     needs=["instagram_access_token"], meta={})


def run_skill(tenant, key: str, inputs: dict | None = None, user: dict | None = None, queue_it: bool = True) -> dict:
    sk = SKILLS.get(key)
    if sk is None:
        raise KeyError(key)
    missing = [f for f, st in vault.status(tenant, sk.needs).items() if st != "set"] if sk.needs else []
    if missing:
        return {"skill": key, "status": "needs_login", "missing": missing,
                "message": f"Needs login: {', '.join(missing)} (NEEDS_LOGIN). Add it under Integrations."}
    actor = f"{user['username']}({user['role']})" if user else "worker"
    prof = get_profile(tenant)

    def fn(run: runtime.Run):
        run.note("profile", {"sections": list(PROFILE_SECTIONS)}, {"read": True}, label="Read business profile")
        text = run.step("draft", inputs or {}, lambda: sk.fn(inputs or {}, prof), "Draft (rules)")
        flags = guardrails.check_for(tenant, text, sk.meta)
        out = {"skill": key, "status": "drafted", "draft": text, "flags": flags}
        if queue_it:
            item = queue.add(tenant, sk.kind, text, channel="manual", title=f"[{sk.name}] draft",
                             meta={**sk.meta, "skill": key}, run_id=run.id, actor=actor, demo=tenant.is_demo)
            out["queue_id"] = item["id"]
            out["queue_status"] = item["status"]
        return out

    res = runtime.execute(tenant, "skill", f"Skill: {sk.name}", ["Read business profile", "Draft (rules)", "Queue"], fn, actor)
    return {**res["result"], "run_id": res["run_id"]}


# --------------------------------------------------------------------------- workers + triggers
TRIGGERS = ("daily", "weekly", "on_source_change", "manual")


def add_worker(tenant, name: str, skill: str, trigger: str, inputs: dict | None = None, schedule: str = "09:00") -> int:
    if skill not in SKILLS:
        raise KeyError(skill)
    if trigger not in TRIGGERS:
        raise ValueError(trigger)
    with tenant.db() as c:
        return c.execute("INSERT INTO workers(name,skill,input,trigger,schedule,needs,enabled,created) VALUES(?,?,?,?,?,?,1,?)",
                         (name, skill, db.jdump(inputs or {}), trigger, schedule, db.jdump(SKILLS[skill].needs),
                          db.now())).lastrowid


def list_workers(tenant) -> list[dict]:
    with tenant.db() as c:
        rows = db.rows(c.execute("SELECT * FROM workers ORDER BY id"))
    for r in rows:
        r["input"] = db.jload(r["input"], {})
        r["needs"] = db.jload(r["needs"], [])
        r["missing"] = [f for f, st in vault.status(tenant, r["needs"]).items() if st != "set"] if r["needs"] else []
    return rows


def run_worker(tenant, worker_id: int, reason: str = "manual") -> dict:
    with tenant.db() as c:
        w = db.row(c.execute("SELECT * FROM workers WHERE id=?", (worker_id,)).fetchone())
    if w is None:
        raise KeyError(worker_id)
    res = run_skill(tenant, w["skill"], db.jload(w["input"], {}), None)
    status = res["status"] if res["status"] != "drafted" else f"drafted ({reason})"
    with tenant.db() as c:
        c.execute("UPDATE workers SET last_run=?, last_status=? WHERE id=?", (db.now(), status, worker_id))
    return res


def _due(w: dict, now: float) -> bool:
    if not w["enabled"] or w["trigger"] not in ("daily", "weekly"):
        return False
    last = w["last_run"] or 0
    period = 86400 if w["trigger"] == "daily" else 7 * 86400
    hh, _, mm = (w["schedule"] or "09:00").partition(":")
    lt = time.localtime(now)
    sched_today = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, int(hh or 9), int(mm or 0), 0, 0, 0, -1))
    return now >= sched_today and now - last >= period - 3600


def run_due_workers(now: float | None = None) -> int:
    now = now or time.time()
    n = 0
    for t in tenancy.list_all():
        try:
            for w in list_workers(t):
                if _due(w, now):
                    run_worker(t, w["id"], w["trigger"])
                    n += 1
        except Exception:  # noqa: BLE001 - one tenant must not stop the others
            continue
    return n


def on_source_change(tenant, source: str) -> int:
    """Called when the knowledge base or the Drive inbox changes. Drafts only."""
    n = 0
    for w in list_workers(tenant):
        if w["enabled"] and w["trigger"] == "on_source_change":
            run_worker(tenant, w["id"], f"source changed: {source}")
            n += 1
    return n
