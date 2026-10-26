"""Onboarding: intake -> gap analysis -> cited lessons per gap -> 30/60/90-day plan -> coach
assignment -> milestone tasks -> welcome and kickoff drafted into the queue."""
from __future__ import annotations

from .. import db, queue, retrieval, runtime

SKILLS = {
    "offer": ("Offer and USP", "USP formula who outcome timeframe mechanism pain"),
    "dms": ("DM setting", "DM opener one question per message follow-up value"),
    "sales": ("Closing calls", "call stages discovery gap objections think about it"),
    "ads": ("Ads testing", "Thunder Dome rules creatives kill rule"),
    "webinars": ("Webinars", "seven segment webinar timings offer moment"),
    "show_rate": ("Booking and show rate", "reminder ladder 24 hours 2 hours 10 minutes confirm"),
}
PLAN_TEMPLATES = {
    "offer": ("Write three USP versions with the formula; pick the one a stranger can repeat", "Test the USP in 20 DMs",
              "Refine the offer stack to three meaningful pieces"),
    "dms": ("Send 20 openers a day using two opener moves", "Log reply rate per move; drop the weaker move",
            "Reach a 10% reply rate on 300+ DMs"),
    "sales": ("Learn the five call stages; role-play twice", "Run objection drills three times a week",
              "Reach a 20% close rate on showed calls"),
    "ads": ("Set up the practice ad account", "Run the first Thunder Dome (3-5 creatives, 72 hours)",
            "Graduate a winner to the scaling campaign"),
    "webinars": ("Outline the seven segments", "Dry-run the webinar with a coach", "Run the first live webinar"),
    "show_rate": ("Set up confirmation within 5 minutes", "Turn on the 24h / 2h / 10min reminder ladder",
                  "Hold a 60%+ show rate for four weeks"),
}


def gaps(intake: dict) -> list[dict]:
    skills = intake.get("skills") or {}
    out = []
    for key, (label, _) in SKILLS.items():
        lvl = skills.get(key)
        try:
            lvl = int(lvl)
        except (TypeError, ValueError):
            lvl = None
        if lvl is not None and lvl <= 2:
            out.append({"key": key, "label": label, "level": lvl, "why": f"Self-rated {lvl}/5"})
    if not (intake.get("usp") or "").strip() and not any(g["key"] == "offer" for g in out):
        out.append({"key": "offer", "label": SKILLS["offer"][0], "level": None, "why": "No USP written yet"})
    try:
        clients = int(intake.get("clients") or 0)
    except (TypeError, ValueError):
        clients = 0
    if clients == 0 and not any(g["key"] == "dms" for g in out):
        out.append({"key": "dms", "label": SKILLS["dms"][0], "level": None, "why": "No clients yet: outreach first"})
    if not out:
        out.append({"key": "show_rate", "label": SKILLS["show_rate"][0], "level": None, "why": "Strong start: protect the show rate"})
    return out[:4]


def assign_coach(tenant) -> dict | None:
    with tenant.db() as c:
        r = c.execute(
            "SELECT u.id, u.display, (SELECT COUNT(*) FROM students s WHERE s.coach_id=u.id) n FROM users u "
            "WHERE u.role='coach' AND u.active=1 ORDER BY n, u.id LIMIT 1").fetchone()
    return dict(r) if r else None


PLAN = ["Read intake", "Gap analysis", "Lessons per gap (cited)", "30/60/90-day plan", "Assign coach", "Milestone tasks",
        "Draft welcome and kickoff"]


def onboard(tenant, intake: dict, user: dict | None = None) -> dict:
    actor = f"{user['username']}({user['role']})" if user else "system"
    name = intake.get("name") or "New student"
    demo = tenant.is_demo or "(DEMO)" in name

    def fn(run: runtime.Run):
        run.note("intake", intake, {"fields": sorted(intake)}, label="Read intake")
        g = run.step("gaps", {}, lambda: gaps(intake), "Gap analysis")

        def _lessons():
            out = []
            n = 0
            for gap in g:
                hits = retrieval.search(tenant, SKILLS[gap["key"]][1], k=2)
                cites = []
                for h in hits:
                    n += 1
                    cites.append(retrieval.cite({**h, "id": f"S{n}"}))
                out.append({**gap, "lessons": cites})
            return out

        lessons = run.step("lessons", {"gaps": [x["key"] for x in g]}, _lessons, "Lessons per gap (cited)")
        plan = run.step("plan", {}, lambda: {
            "30": [PLAN_TEMPLATES[x["key"]][0] for x in g], "60": [PLAN_TEMPLATES[x["key"]][1] for x in g],
            "90": [PLAN_TEMPLATES[x["key"]][2] for x in g]}, "30/60/90-day plan")
        coach = run.step("assign_coach", {}, lambda: assign_coach(tenant), "Assign coach")
        with tenant.db() as c:
            sid = c.execute("INSERT INTO students(name,email,coach_id,joined,niche,stage,clients,demo,intake,plan) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (name, intake.get("email"), coach["id"] if coach else None, db.now(), intake.get("niche"),
                             "foundations", int(intake.get("clients") or 0), 1 if demo else 0, db.jdump(intake),
                             db.jdump(plan))).lastrowid

        def _tasks():
            ids = []
            with tenant.db() as c:
                for days, items in ((30, plan["30"]), (60, plan["60"]), (90, plan["90"])):
                    for it in items:
                        ids.append(c.execute("INSERT INTO coach_tasks(coach_id,student_id,kind,detail,status,due,created,demo) "
                                             "VALUES(?,?,?,?,?,?,?,?)",
                                             (coach["id"] if coach else None, sid, "milestone", it, "open",
                                              db.now() + days * 86400, db.now(), 1 if demo else 0)).lastrowid)
            return ids

        tasks = run.step("milestones", {}, _tasks, "Milestone tasks")
        first = name.split()[0]
        coach_name = coach["display"] if coach else "your coach"

        def _drafts():
            w = queue.add(tenant, "welcome",
                          f"Welcome to the programme, {first}! {coach_name} is your coach. Your first 30 days focus on: "
                          + "; ".join(plan["30"]) + ".", channel="email", recipient_ref=intake.get("email"),
                          title=f"Welcome {name}", meta={"student_id": sid}, run_id=run.id, actor=actor, demo=demo)
            k = queue.add(tenant, "kickoff", f"Kickoff call: {name} with {coach_name}. Agenda: review gaps ("
                          + ", ".join(x["label"] for x in g) + ") and agree the 30-day plan.", channel="calendar",
                          recipient_ref=intake.get("email"), title=f"Kickoff: {name}",
                          meta={"student_id": sid, "attendees": [intake.get("email")] if intake.get("email") else []},
                          run_id=run.id, actor=actor, demo=demo)
            return [w["id"], k["id"]]

        qids = run.step("drafts", {}, _drafts, "Draft welcome and kickoff")
        return {"student_id": sid, "gaps": lessons, "plan": plan, "coach": coach, "tasks": tasks, "queue_ids": qids}

    out = runtime.execute(tenant, "onboarding", f"Onboard {name}", PLAN, fn, actor)
    return {**out["result"], "run_id": out["run_id"]}
