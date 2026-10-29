"""Users, sessions, login throttle and role-based access."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from . import config, db

ROLES = ("owner", "coach", "setter", "closer", "student")

# Every UI tab is also the permission its API routes require.
ALL_TABS = [
    "home", "missions", "command", "brief",
    "academy", "playbooks",
    "coach", "threads",
    "review", "coaching",
    "sales", "prospects", "outreach", "calls", "drills",
    "students", "onboarding", "checkin",
    "clients", "diagnostic", "webinar", "ads",
    "studio", "builder",
    "queue", "runs", "audit", "kb", "evals", "golive", "console", "integrations", "channels", "mcp", "crm",
    "minis", "drive", "skills", "profile",
    "team", "setup",
    "projects", "settings",
]

ROLE_TABS: dict[str, set[str]] = {
    "owner": set(ALL_TABS) - {"checkin"},
    "coach": {"home", "missions", "brief", "academy", "playbooks", "coach", "threads", "review", "coaching", "students",
              "onboarding", "studio", "queue", "runs", "calls", "drills", "projects", "settings"},
    "setter": {"home", "missions", "academy", "playbooks", "coach", "threads", "sales", "prospects", "outreach", "drills",
               "queue", "projects", "settings"},
    "closer": {"home", "missions", "academy", "playbooks", "coach", "threads", "sales", "prospects", "calls", "drills",
               "queue", "projects", "settings"},
    "student": {"home", "missions", "academy", "playbooks", "coach", "threads", "review", "coaching", "prospects", "checkin",
                "drills", "queue", "projects", "settings",
                "clients"},  # a student's own clients only (routes/clients.py scopes every row by owner_user_id)
}

# Queue kinds a non-owner role may approve or reject. Everything else is owner-only
# (calendar invites, ad changes, CRM terminal moves, client assets ...).
ROLE_DECIDE: dict[str, set[str]] = {
    "coach": {"nudge", "welcome", "kickoff", "coach_note", "intervention", "checkin_reply"},
    "setter": {"dm", "followup", "dm_opener", "booking_dm", "reminder", "setter_reply"},
    "closer": {"followup", "booking_dm", "reminder", "precall_brief"},
    "student": set(),
}

# Outreach a user drafted for their OWN prospects/clients (queue item meta.owner_user_id == user id):
# any role may decide these — a student running their growth-operator business approves their own
# DMs and emails. Guardrails still run on every approval.
OWN_DECIDE: set[str] = {"dm_opener", "followup", "prospect_dm", "prospect_email", "booking_dm", "proposal",
                        "reminder", "client_report", "channel_reply", "mission_draft",
                        "webinar_launch"}

LOGIN_WINDOW = 15 * 60
LOGIN_MAX_FAILS = 8


def has_tab(role: str, tab: str) -> bool:
    return tab in ROLE_TABS.get(role, set())


def can_decide(role: str, kind: str, item_owner: int | None = None, user_id: int | None = None) -> bool:
    if role == "owner":
        return True
    if item_owner is not None and user_id is not None:
        # An item someone drafted for their OWN prospects/clients belongs to them (and the owner): a setter's
        # "followup" rights cover the programme's leads, never a student's own outreach.
        if item_owner != user_id:
            return False
        if kind in OWN_DECIDE:
            return True
    return kind in ROLE_DECIDE.get(role, set())


def owns_or_shared(role: str, item_owner: int | None, user_id: int | None) -> bool:
    """May this user see a queue item at all? Personal items (meta.owner_user_id set) only to their author and the
    owner; everything else is governed by the role's kinds."""
    return role == "owner" or item_owner is None or (user_id is not None and item_owner == user_id)


def hash_pw(pw: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${dk.hex()}"


def check_pw(pw: str, stored: str) -> bool:
    try:
        _, salt_hex, dk_hex = stored.split("$")
        dk = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(dk.hex(), dk_hex)
    except (ValueError, TypeError):
        return False


_DUMMY_HASH = hash_pw(secrets.token_urlsafe(16))


def create_user(tenant, username: str, pw: str, role: str, display: str | None = None,
                coach_id: int | None = None) -> int:
    if role not in ROLES:
        raise ValueError("bad role")
    with tenant.db() as c:
        cur = c.execute(
            "INSERT INTO users(username,display,role,pw_hash,coach_id,created) VALUES(?,?,?,?,?,?)",
            (username, display or username, role, hash_pw(pw), coach_id, db.now()),
        )
        return cur.lastrowid


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Throttled(Exception):
    pass


def _fail_count(c, key: str) -> int:
    since = time.time() - LOGIN_WINDOW
    return c.execute("SELECT COUNT(*) FROM login_fails WHERE key=? AND at>?", (key, since)).fetchone()[0]


def login(tenant, username: str, pw: str, ip: str) -> tuple[str, dict] | None:
    """Returns (session token, user) or None. Raises Throttled after 8 failures in 15 minutes
    for the same IP or the same username."""
    from . import audit

    with tenant.db() as c:
        if _fail_count(c, f"ip:{ip}") >= LOGIN_MAX_FAILS or _fail_count(c, f"user:{username}") >= LOGIN_MAX_FAILS:
            raise Throttled()
        u = c.execute("SELECT * FROM users WHERE username=? AND active=1", (username,)).fetchone()
        if u is None and "@" in username:  # [team] invited people sign in with their (lower-case) email; phones capitalise
            u = c.execute("SELECT * FROM users WHERE username=? AND active=1", (username.strip().lower(),)).fetchone()
        # an unknown username costs the same scrypt work as a wrong password, so timing doesn't reveal which
        # usernames exist
        ok = check_pw(pw, u["pw_hash"] if u is not None else _DUMMY_HASH) and u is not None
        if not ok:
            c.execute("INSERT INTO login_fails(key,at) VALUES(?,?)", (f"ip:{ip}", time.time()))
            c.execute("INSERT INTO login_fails(key,at) VALUES(?,?)", (f"user:{username}", time.time()))
    if not ok:
        audit.log(tenant, username, "login.fail", {"ip": ip})
        return None
    token = start_session(tenant, u["id"], ip)
    audit.log(tenant, username, "login", {"ip": ip, "role": u["role"]})
    return token, user_public(dict(u))


def start_session(tenant, user_id: int, ip: str = "") -> str:
    token = secrets.token_urlsafe(32)
    hours = config.get().session_hours
    with tenant.db() as c:
        c.execute("INSERT INTO sessions(token_hash,user_id,created,expires,ip) VALUES(?,?,?,?,?)",
                  (_hash_token(token), user_id, time.time(), time.time() + hours * 3600, ip))
    return token


def session_user(tenant, token: str | None) -> dict | None:
    if not token:
        return None
    with tenant.db() as c:
        r = c.execute(
            "SELECT u.*, s.expires FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?",
            (_hash_token(token),),
        ).fetchone()
        if r is None:
            return None
        if r["expires"] < time.time():
            c.execute("DELETE FROM sessions WHERE token_hash=?", (_hash_token(token),))
            return None
        if not r["active"]:
            return None
    return user_public(dict(r))


def end_session(tenant, token: str | None) -> None:
    if token:
        with tenant.db() as c:
            c.execute("DELETE FROM sessions WHERE token_hash=?", (_hash_token(token),))


def user_public(u: dict) -> dict:
    return {"id": u["id"], "username": u["username"], "display": u.get("display") or u["username"],
            "role": u["role"], "coach_id": u.get("coach_id"), "student_id": u.get("student_id")}


def tabs_for(role: str) -> list[str]:
    return [t for t in ALL_TABS if has_tab(role, t)]
