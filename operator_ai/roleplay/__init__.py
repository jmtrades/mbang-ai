"""Role-play simulator: practice that can't be gamed.

* scenarios.py - scenario library (data), validation, owner edits (roleplay_scenarios)
* acts.py      - what the learner did in a turn (questions, gap playback with revealed facts, SOP moves, stuffing)
* engine.py    - deterministic state machine (trust/patience meters, hidden facts, objection queue, seeded replies)
* replies.py   - template reply bank keyed by (event, temperament, trust band)
* ai.py        - optional AI phrasing of the persona line (rules decide; post-checks; replays; token cap)
* score.py     - turn-aware, order-aware scorer with SOP citations (shared roleplays score contract)
* rubric.py    - rubric rules as owner-editable data with a citation validator (roleplay_rubrics)
* sessions.py  - storage, visibility, traces, sign-off, drill cards
"""
from __future__ import annotations

DEMO_SCRIPTS = [
    ("discovery_call", 7, [
        "Thanks for making the time. Love the class clips you post. Before I share anything, what would you like the "
        "studio to look like by the end of spring?",
        "That's a clear goal. Where are the numbers right now, roughly per week?",
        "What have you already tried to fill those evening classes?",
        "And what made you look at this now rather than in six months?",
        "So if I've got this right: you want twenty more regular members by spring, and right now four trial sign-ups a "
        "week turn into two that stick. Is that the gap?",
        "Would it be useful if I showed you how we'd fill the second room? Here's how we work: we follow up every trial "
        "sign-up within the hour and run a simple referral night each month.",
        "Fair. You said the second room sits empty most evenings. How does the cost of staying where you are look "
        "next to that?",
        "Shall we book a short call on Thursday to map out the first month?",
    ], True),
    ("objection_think", 11, [
        "I totally understand. Most people feel that way, but this really is the best time to start.",
        "Totally fair. When you say you want to think about it, what specifically do you want to think through?",
        "That makes sense. Realistically it's about five hours a week, and we can plan around your wedding weekends. "
        "Shall we book a short call next Tuesday to map it out?",
    ], False),
]


def seed_demo(tenant) -> int:
    """Two completed, scored role-plays for the demo student (one signed off by the demo coach). Demo DB only."""
    from .. import tenancy
    from . import sessions

    if not tenancy.is_demo_db(tenant):
        return 0
    with tenant.db() as c:
        stu = c.execute("SELECT * FROM users WHERE username='demo-student'").fetchone()
        coach = c.execute("SELECT * FROM users WHERE username='demo-coach'").fetchone()
        if stu is None or c.execute("SELECT 1 FROM roleplays WHERE user_id=? LIMIT 1", (stu["id"],)).fetchone():
            return 0
    learner = {"id": stu["id"], "username": stu["username"], "role": stu["role"], "display": stu["display"]}
    made = 0
    for key, seed, lines, sign in DEMO_SCRIPTS:
        v = sessions.start(tenant, learner, key, seed=seed, mode="rules")
        for line in lines:
            if v["status"] != "active":
                break
            v = sessions.turn(tenant, learner, v["id"], line)
        if v["status"] == "active":
            v = sessions.end(tenant, learner, v["id"])
        made += 1
        if sign and coach is not None:
            sessions.signoff(tenant, {"id": coach["id"], "username": coach["username"], "role": coach["role"],
                                      "display": coach["display"]}, v["id"],
                             "Strong discovery and a clean gap playback (DEMO).")
    return made
