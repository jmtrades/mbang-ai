"""Shared Academy definitions: skills, paths, certification thresholds, the course-material check (A0),
who may see whose training rows, and small helpers used by every Academy module."""
from __future__ import annotations

import hashlib
import math
import threading

from .. import config, db, index

# --------------------------------------------------------------------------- skills
# The first six mirror agents.onboarding.SKILLS; the last four extend them (A5 step 2).
SKILLS: dict[str, tuple[str, str]] = {
    "offer": ("Offer and USP", "USP formula who outcome timeframe mechanism pain niche offer stack"),
    "dms": ("DM setting", "DM opener one question per message follow-up value reply conversation"),
    "sales": ("Closing calls", "call stages discovery gap offer objections think about it expensive partner close"),
    "show_rate": ("Booking and show rate", "reminder ladder confirm booking show rate no-show pre-call brief"),
    "ads": ("Ads testing", "Thunder Dome rules creatives kill rule cost per lead winner audience budget"),
    "webinars": ("Webinars", "seven segment webinar timings offer moment chat replay registrants"),
    "compliance": ("Compliance", "never guarantee honest subject lines plant questions invent verified results "
                                 "permission deadline reason discount fake"),
    "fulfilment": ("Client fulfilment", "client delivery onboarding launch plan done-with-you service support"),
    "client_reporting": ("Client reporting", "report weekly numbers funnel bottleneck benchmark stage converts"),
    "coaching": ("Coaching", "coach review homework community thread feedback student"),
    "general": ("General", ""),
}


def skill_label(key: str) -> str:
    return SKILLS.get(key, (key.replace("_", " ").capitalize(), ""))[0]


# Coaching-mode kind that matches each skill (lesson "Practise it" links).
COACHING_KIND = {"dms": "dm", "show_rate": "reminder", "offer": "bio", "ads": "ad", "webinars": "script",
                 "sales": "script", "compliance": "dm", "client_reporting": "email", "fulfilment": "email"}

# --------------------------------------------------------------------------- paths and levels
LEVELS = ("L1", "L2", "L3", "L4")
LEVEL_NAMES = {"L1": "Foundations", "L2": "Practitioner", "L3": "Certified", "L4": "Lead"}

PATHS: dict[str, dict] = {
    "compliance_core": {"role": "all", "title": "Compliance core", "skills": ["compliance"], "levels": ["L1"],
                        "blurb": "The rules everyone follows before anything goes out under the brand."},
    "growth_operator": {"role": "student", "title": "Growth operator",
                        "skills": ["offer", "dms", "sales", "show_rate", "ads", "webinars", "fulfilment",
                                   "client_reporting", "compliance", "general"],
                        "levels": list(LEVELS), "blurb": "Win a first client and run their growth."},
    "setter": {"role": "setter", "title": "Setter", "skills": ["dms", "show_rate", "compliance"], "levels": list(LEVELS),
               "blurb": "Openers, follow-ups and booking calls that show up."},
    "closer": {"role": "closer", "title": "Closer", "skills": ["sales", "show_rate", "compliance"], "levels": list(LEVELS),
               "blurb": "Call stages, the gap and the common objections."},
    "coach": {"role": "coach", "title": "Coach", "skills": ["coaching", "offer", "dms", "sales", "show_rate", "compliance",
                                                             "general"],
              "levels": list(LEVELS), "blurb": "Teach the method and grade practice fairly."},
}
ROLE_PATH = {"student": "growth_operator", "setter": "setter", "closer": "closer", "coach": "coach"}

# Exact thresholds from the audit (A9). The owner may change them per path; awarded certificates keep a
# snapshot so later edits never silently change them.
DEFAULT_THRESHOLDS: dict[str, dict] = {
    "L1": {"mastery_overall": 0.8, "mastery_skill": 0.7},
    "L2": {"roleplays": 3, "roleplay_score": 75, "write_items": 5, "write_score": 80},
    "L3": {"exam_items": 30, "exam_pass": 0.85, "exam_minutes": 45, "cooldown_hours": 24,
           "setter_dms": 50, "setter_hold_rate": 0.05, "closer_calls": 10, "closer_call_score": 70,
           "operator_clients": 1, "coach_graded": 10, "grade_tolerance": 10},
    "L4": {"calibration_items": 20, "calibration_agreement": 0.8, "grade_tolerance": 10},
}
CERT_DAYS = 180
RECERT_BLOCKS = 3        # guardrail blocks on a person's own items ...
RECERT_WINDOW_DAYS = 30  # ... within this many days trigger recertification

DAY = 86400.0


# --------------------------------------------------------------------------- course material (A0)
class Refused(Exception):
    """An Academy action the current state does not allow (HTTP 409 unless status says otherwise)."""

    def __init__(self, msg: str, status: int = 409):
        super().__init__(msg)
        self.status = status


NO_MATERIAL = "Load your course material first"


def material_state(tenant) -> dict:
    """Which material the Academy would be built from, and whether a build is allowed (A0).

    Outside the demo tenant the fictional fixture corpus never becomes training: building is refused
    until the owner's own material is loaded. In the demo tenant every lesson carries "Demo material"."""
    cfg = config.get()
    scope = tenant.kb_scope
    if scope == "empty":
        return {"ok": False, "reason": NO_MATERIAL + ": this workspace has no knowledge base yet.",
                "demo_material": False, "source": "none"}
    if scope == "course" and cfg.using_fixture_corpus:
        if tenant.is_demo:
            return {"ok": True, "reason": "", "demo_material": True, "source": "demo"}
        return {"ok": False, "reason": NO_MATERIAL + ": only the small fictional test material is on this server. "
                                                     "Copy the course folders into inputs/ and build again.",
                "demo_material": False, "source": "fixture"}
    try:
        idx = index.for_tenant(tenant)
    except Exception:  # noqa: BLE001 - licence or read errors mean there is nothing we may build from
        return {"ok": False, "reason": NO_MATERIAL + ".", "demo_material": False, "source": "none"}
    if not idx.docs:
        return {"ok": False, "reason": NO_MATERIAL + ": the knowledge base is empty.", "demo_material": False,
                "source": "none"}
    return {"ok": True, "reason": "", "demo_material": bool(tenant.is_demo), "source": "course" if scope == "course" else "own"}


# --------------------------------------------------------------------------- per-database extras
_extras_done: set[str] = set()
_extras_lock = threading.Lock()


def ensure_extras(tenant) -> None:
    """Indexes on migrated columns (they cannot live in TENANT_SCHEMA, which runs before the column
    migrations on an existing database) and the default path rows."""
    key = str(tenant.db_path)
    if key in _extras_done and key in db._initialised:
        return
    with _extras_lock:
        with tenant.db() as c:
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS academy_review_kind ON academy_review(user_id, item_kind, item_id)")
            c.execute("CREATE INDEX IF NOT EXISTS academy_items_lesson ON academy_quiz_items(lesson_id, status)")
            c.execute("CREATE INDEX IF NOT EXISTS academy_lessons_key ON academy_lessons(lesson_key, status)")
            for k, p in PATHS.items():
                c.execute("INSERT OR IGNORE INTO academy_paths(key,role,title,skill_order,levels,required,updated) "
                          "VALUES(?,?,?,?,?,?,?)",
                          (k, p["role"], p["title"], db.jdump(p["skills"]), db.jdump(DEFAULT_THRESHOLDS),
                           db.jdump(p["levels"]), db.now()))
        _extras_done.add(key)


def path_def(tenant, key: str) -> dict | None:
    """Path definition with the tenant's current thresholds."""
    if key not in PATHS:
        return None
    ensure_extras(tenant)
    with tenant.db() as c:
        r = c.execute("SELECT * FROM academy_paths WHERE key=?", (key,)).fetchone()
    base = dict(PATHS[key])
    th = {lv: dict(v) for lv, v in DEFAULT_THRESHOLDS.items()}
    if r is not None:
        for lv, v in (db.jload(r["levels"], {}) or {}).items():
            if lv in th and isinstance(v, dict):
                th[lv].update({k: v[k] for k in v if k in th[lv]})
        base["skills"] = db.jload(r["skill_order"], base["skills"]) or base["skills"]
    base["key"] = key
    base["thresholds"] = th
    return base


def paths_for_role(role: str) -> list[str]:
    if role == "owner":
        return list(PATHS)
    out = ["compliance_core"]
    if role in ROLE_PATH:
        out.append(ROLE_PATH[role])
    return out


def user_paths(tenant, user: dict) -> list[str]:
    """The role's own paths plus any path someone assigned."""
    keys = paths_for_role(user["role"])
    with tenant.db() as c:
        for r in c.execute("SELECT path_key FROM academy_enrolments WHERE user_id=? AND status!='removed'", (user["id"],)):
            if r["path_key"] in PATHS and r["path_key"] not in keys:
                keys.append(r["path_key"])
    return keys


# --------------------------------------------------------------------------- who sees whose rows
def learner_ids(tenant, user: dict) -> list[int] | None:
    """User ids whose training this user may see. None = everyone (owner). Coaches see the students they
    coach (students.coach_id or users.coach_id) and the people assigned to them (team_assignments)."""
    if user["role"] == "owner":
        return None
    if user["role"] != "coach":
        return [user["id"]]
    uid = user["id"]
    with tenant.db() as c:
        ids = {r["id"] for r in c.execute(
            "SELECT u.id FROM users u WHERE u.active=1 AND u.id!=? AND (u.coach_id=? OR u.student_id IN "
            "(SELECT s.id FROM students s WHERE s.coach_id=?))", (uid, uid, uid))}
        ids |= {r["member_id"] for r in c.execute("SELECT member_id FROM team_assignments WHERE manager_id=?", (uid,))}
    ids.discard(uid)
    return sorted(ids)


def can_see(tenant, viewer: dict, user_id: int) -> bool:
    if viewer["id"] == user_id:
        return True
    ids = learner_ids(tenant, viewer)
    return ids is None or user_id in ids


def user_row(tenant, user_id: int) -> dict | None:
    with tenant.db() as c:
        r = c.execute("SELECT id, username, display, role, coach_id, student_id, active FROM users WHERE id=?",
                      (user_id,)).fetchone()
    return dict(r) if r else None


# --------------------------------------------------------------------------- helpers
def sha(text: str, n: int = 16) -> str:
    return hashlib.sha256((text or "").encode()).hexdigest()[:n]


def sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1 / (1 + z)
    z = math.exp(x)
    return z / (1 + z)


def demo_flag(tenant) -> int:
    return 1 if tenant.is_demo else 0
