"""Team & invites: everyone after the owner joins through a one-time link.

* The owner invites by email + role (coach, setter, closer, student). The link carries a random token; only its
  SHA-256 hash is stored. It expires after 7 days (reset links after 48 hours) and works once.
* The invite email is an approval-queue item (kind "invite"), never sent directly. The link is shown to the owner
  once, so it can also be shared by hand. Once a link is used, cancelled, expired or really sent, its copy in the
  queue is scrubbed.
* Accepting sets a password (strength-checked) and creates the account; students are linked to a student record.
  Failed accept attempts are rate-limited per IP.
* The owner lists people, changes roles, switches accounts off (every session ends) and on, creates reset links
  (same token mechanism), assigns coaches to students and managers to setters/closers, imports students, and
  exports or deletes a person's data (soft-delete + scrub). Everything is audit-logged; the audit never holds a
  token or a password.
"""
from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import secrets
import time

from . import audit, auth, db, queue

INVITE_ROLES = ("coach", "setter", "closer", "student")
ROLE_LABEL = {"owner": "owner", "coach": "coach", "setter": "appointment setter", "closer": "closer", "student": "student"}
ROLE_ORDER = {"owner": 0, "coach": 1, "closer": 2, "setter": 3, "student": 4}
INVITE_TTL = 7 * 86400
RESET_TTL = 2 * 86400
JOIN_WINDOW = 15 * 60
JOIN_MAX_FAILS = 10
FORGOT_MAX = 5
IMPORT_MAX_ROWS = 2000
IMPORT_MAX_INVITES = 200

_EMAIL = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}$")
_ORIGIN = re.compile(r"^https?://[A-Za-z0-9.\-]+(:\d{1,5})?$")
_TOKEN_IN_LINK = re.compile(r"(#/join\?token=)[A-Za-z0-9_\-]+")
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
COMMON_PASSWORDS = ("password", "passw0rd", "12345678", "123456789", "1234567890", "qwerty", "qwertyuiop", "letmein",
                    "welcome", "iloveyou", "admin", "changeme", "football", "monkey", "dragon", "abc123", "111111",
                    "000000", "trustno1", "sunshine")


class TeamError(Exception):
    def __init__(self, status: int, msg: str, detail: object | None = None):
        super().__init__(msg)
        self.status = status
        self.detail = detail if detail is not None else msg


# --------------------------------------------------------------------------- small helpers
def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def norm_email(email: str | None) -> str:
    return (email or "").strip().lower()


def valid_email(email: str | None) -> bool:
    e = norm_email(email)
    return 3 <= len(e) <= 200 and bool(_EMAIL.match(e))


def mask_email(email: str | None) -> str:
    e = norm_email(email)
    if "@" not in e:
        return "***"
    local, _, domain = e.partition("@")
    return f"{local[:1]}***@{domain}"


def clean_display(s: str | None, limit: int = 80) -> str:
    return re.sub(r"\s+", " ", _CTRL.sub(" ", s or "")).strip()[:limit]


def _demo_label(tenant, name: str) -> str:
    """Every person in the demo database is labelled (DEMO)."""
    if tenant.is_demo and not name.endswith("(DEMO)"):
        return f"{name} (DEMO)"[:90]
    return name


def _brand(tenant) -> str:
    return ((tenant.get("brand", {}) or {}).get("name") or "your programme").strip()


def base_url(origin: str | None = None) -> str:
    """Where links point. OPERATOR_PUBLIC_URL wins; else the origin the owner's browser reported (validated)."""
    env = (os.environ.get("OPERATOR_PUBLIC_URL") or "").strip().rstrip("/")
    if env and _ORIGIN.match(env):
        return env
    o = (origin or "").strip().rstrip("/")
    return o if _ORIGIN.match(o) else ""


def join_path(tenant, token: str) -> str:
    return f"/t/{tenant.id}/#/join?token={token}"


def password_problems(pw: str, email: str = "", display: str = "") -> list[str]:
    """Plain-English reasons a new password is too weak (empty list = fine)."""
    out: list[str] = []
    pw = pw or ""
    if len(pw) < 10:
        out.append("Use at least 10 characters.")
    if len(pw) > 200:
        out.append("Use 200 characters or fewer.")
    classes = sum([any(c.islower() for c in pw), any(c.isupper() for c in pw), any(c.isdigit() for c in pw),
                   any(not c.isalnum() for c in pw)])
    if len(pw) < 16 and classes < 3:
        out.append("Mix at least three of: lower-case letters, capitals, numbers, symbols. Or use 16+ characters.")
    low = pw.lower()
    if len(pw) < 20 and any(w in low for w in COMMON_PASSWORDS):
        out.append("Avoid common passwords and keyboard patterns.")
    if pw and len(set(pw)) < 5:
        out.append("Use more different characters.")
    local = norm_email(email).split("@")[0]
    if len(local) >= 4 and local in low:
        out.append("Don't use your email address in your password.")
    for part in clean_display(display).lower().split():
        if len(part) >= 4 and part in low:
            out.append("Don't use your name in your password.")
            break
    return out


def _user(c, uid: int) -> dict | None:
    return db.row(c.execute("SELECT * FROM users WHERE id=? AND deleted IS NULL", (uid,)).fetchone())


def shared_demo_account(tenant, u: dict | None) -> bool:
    """The pitch demo's one-click accounts (demo-owner, demo-coach ...) are shared by every visitor."""
    return bool(tenant.is_demo and u and u.get("username") in {f"demo-{r}" for r in auth.ROLES})


def _target(tenant, uid: int, allow_owner: bool = False, allow_shared_demo: bool = False) -> dict:
    with tenant.db() as c:
        u = _user(c, uid)
    if u is None:
        raise TeamError(404, "Not found")
    if u["role"] == "owner" and not allow_owner:
        raise TeamError(403, "Owner accounts are managed on the server, not from this screen.")
    if not allow_shared_demo and shared_demo_account(tenant, u):
        # switching it off, deleting it or resetting its password would sign out (or lock out) everyone trying the demo
        raise TeamError(403, "This one-click demo account is shared by everyone trying the demo, so it can't be changed. "
                             "Invite a new (DEMO) person to try this.")
    return u


def _banned_name(tenant, name: str) -> bool:
    """A display name may not carry a name on the workspace's banned list (no impersonation)."""
    from . import guardrails

    return bool(name) and guardrails.scrub(name, guardrails.tenant_banned_names(tenant)) != name


def _kill_sessions(c, uid: int) -> int:
    return c.execute("DELETE FROM sessions WHERE user_id=?", (uid,)).rowcount


def _email_taken(c, email: str) -> dict | None:
    return db.row(c.execute("SELECT id, active, role FROM users WHERE deleted IS NULL AND (lower(username)=? OR lower(email)=?)",
                            (email, email)).fetchone())


def _on_never_invite_list(tenant, email: str) -> bool:
    # the never-invite list holds comparable address keys (shared with delivery's suppression list)
    return bool(queue.address_keys(email) & queue.blocked_list(tenant))


# --------------------------------------------------------------------------- invite email (template, AI not used)
def _article(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


def draft_invite_email(tenant, *, name: str, role: str, inviter: str, link: str, expires: float, kind: str = "invite") -> tuple[str, str]:
    """(subject, body). A plain template: no claims, no urgency, the expiry has its reason."""
    brand = _brand(tenant)
    first = (clean_display(name).split(" ")[0] if name else "") or "there"
    when = time.strftime("%d %b %Y", time.localtime(expires))
    if kind == "reset":
        subject = f"Reset your {brand} password"
        body = (f"Hi {first},\n\n{inviter or 'The workspace owner'} created a link for you to choose a new password "
                f"for {brand}.\n\nOpen this link and choose a new password:\n{link}\n\n"
                f"The link works once and stops working on {when} so that an old link can't be reused. "
                f"Choosing a new password signs you out everywhere else.\n\n"
                f"If you didn't ask for this, you can ignore this email and your password stays the same.\n")
        return subject, body
    label = ROLE_LABEL.get(role, role)
    subject = f"You're invited to {brand}"
    body = (f"Hi {first},\n\n{inviter or 'The workspace owner'} has invited you to join {brand} as {_article(label)} {label}.\n\n"
            f"To accept, open this link and choose a password:\n{link}\n\n"
            f"The link works once and stops working on {when} so that an old link can't be reused.\n\n"
            f"If you weren't expecting this, you can ignore this email and nothing will happen.\n")
    return subject, body


# --------------------------------------------------------------------------- invites
def _public_invite(r: dict, now: float | None = None) -> dict:
    now = now or time.time()
    expires = r.get("expires") or 0
    # housekeeping closes expired links after the fact (revoked >= expires): those still read as expired
    state = ("used" if r.get("used") else "cancelled" if r.get("revoked") and r["revoked"] < expires else
             "expired" if expires < now else "cancelled" if r.get("revoked") else "open")
    return {"id": r["id"], "kind": r.get("kind") or "invite", "email": r.get("email"), "role": r.get("role"),
            "role_label": ROLE_LABEL.get(r.get("role") or "", r.get("role")), "display": r.get("display"),
            "user_id": r.get("user_id"), "coach_id": r.get("coach_id"), "student_id": r.get("student_id"),
            "queue_id": r.get("queue_id"), "created": r.get("created"), "expires": r.get("expires"),
            "used": r.get("used"), "revoked": r.get("revoked"), "state": state, "created_by": r.get("created_by"),
            "demo": bool(r.get("demo"))}


def _queue_invite_email(tenant, iid: int, *, kind: str, email: str, name: str, role: str, inviter: str, link: str,
                        expires: float, actor: str, run_id: int | None) -> dict:
    subject, body = draft_invite_email(tenant, name=name, role=role, inviter=inviter, link=link, expires=expires, kind=kind)
    qkind = "invite" if kind == "invite" else "password_reset"
    title = (f"Invite {name or mask_email(email)} as {ROLE_LABEL.get(role, role)}" if kind == "invite"
             else f"Password reset link for {name or mask_email(email)}")
    item = queue.add(tenant, qkind, body, channel="email", recipient_ref=email, title=title,
                     meta={"subject": subject, "invite_id": iid, "tag": "team", "team_kind": kind,
                           "label": "Drafted from a template (AI not used)"},
                     run_id=run_id, actor=actor, demo=tenant.is_demo)
    with tenant.db() as c:
        c.execute("UPDATE invites SET queue_id=? WHERE id=?", (item["id"], iid))
    return item


def _traced(inv: dict) -> dict:
    """What a run trace keeps of an invite (coaches can read traces): no full address, no name."""
    return {**inv, "email": mask_email(inv.get("email")) if inv.get("email") else None, "display": None}


def _queue_summary(item: dict | None) -> dict | None:
    if not item:
        return None
    return {"id": item["id"], "status": item["status"], "flags": item.get("flags", []), "kind": item["kind"]}


def create_invite(tenant, actor: str, *, email: str | None = None, role: str = "student", display: str | None = None,
                  coach_id: int | None = None, student_id: int | None = None, send_email: bool = True,
                  origin: str | None = None, inviter: str = "") -> dict:
    """Create a one-time invite. Returns the link ONCE (it is never stored in clear)."""
    from . import runtime

    if role not in INVITE_ROLES:
        raise TeamError(422, "Pick one of: coach, setter, closer, student.")
    student = None
    with tenant.db() as c:
        if student_id is not None:
            if role != "student":
                raise TeamError(422, "Only a student invite can be linked to a student record.")
            student = db.row(c.execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone())
            if student is None:
                raise TeamError(404, "Student record not found")
            if c.execute("SELECT 1 FROM users WHERE student_id=? AND deleted IS NULL", (student_id,)).fetchone():
                raise TeamError(409, "That student already has an account.")
        em = norm_email(email or (student or {}).get("email"))
        if not valid_email(em):
            raise TeamError(422, "Enter a valid email address.")
        if _email_taken(c, em):
            raise TeamError(409, "Someone with that email already has an account. Manage them under People.")
        if coach_id is not None:
            if role != "student":
                coach_id = None
            else:
                co = _user(c, coach_id)
                if co is None or co["role"] != "coach" or not co["active"]:
                    raise TeamError(422, "Pick an active coach.")
    if _on_never_invite_list(tenant, em):
        raise TeamError(409, "That address is on the never-invite list (Settings).")
    if tenant.is_demo and not em.endswith("@example.invalid"):
        raise TeamError(422, "The pitch demo only invites fictional addresses ending in @example.invalid.")
    name = clean_display(display) or clean_display((student or {}).get("name")) or em.split("@")[0]
    if _banned_name(tenant, name):
        raise TeamError(422, "That name is on your banned-names list (Settings), so it can't be used. Leave the name "
                             "empty or use another one.")
    name = _demo_label(tenant, name)
    plan = ["Check the address and role", "Create a one-time link (only its fingerprint is stored)",
            "Draft the invite email from a template", "Put the email in the approval queue"]
    # the link exists only in the response (and the queued email); the run trace never holds it
    once: dict = {}

    def fn(run):
        token = secrets.token_urlsafe(32)
        now = time.time()
        expires = now + INVITE_TTL

        def _insert():
            with tenant.db() as c, db.tx(c):
                c.execute("UPDATE invites SET revoked=? WHERE lower(email)=? AND COALESCE(kind,'invite')='invite' "
                          "AND used IS NULL AND revoked IS NULL", (now, em))
                iid = c.execute("INSERT INTO invites(email,role,token_hash,created_by,created,expires,kind,display,coach_id,"
                                "student_id,demo) VALUES(?,?,?,?,?,?,'invite',?,?,?,?)",
                                (em, role, _hash(token), actor, now, expires, name, coach_id, student_id,
                                 1 if tenant.is_demo else 0)).lastrowid
            return iid

        run.note("check", {"role": role, "email": mask_email(em)}, {"ok": True}, label="Checked the address and role")
        iid = run.step("create_link", {"expires_in_days": INVITE_TTL // 86400}, _insert, label="Created a one-time link")
        path = join_path(tenant, token)
        full = base_url(origin) + path
        once.update(link=path, full_link=full if base_url(origin) else None)
        item = None
        if send_email:
            def _queue():
                nonlocal item
                item = _queue_invite_email(tenant, iid, kind="invite", email=em, name=name, role=role, inviter=inviter,
                                           link=full, expires=expires, actor=actor, run_id=run.id)
                return _queue_summary(item)

            run.step("queue_email", {"to": mask_email(em), "kind": "invite"}, _queue,
                     label="Drafted the invite email (template) and queued it for approval")
        audit.log(tenant, actor, "team.invite", {"invite_id": iid, "role": role, "to": mask_email(em),
                                                 "queued": item["id"] if item else None})
        _cleanup_from_others(tenant, iid, em)
        with tenant.db() as c:
            row = db.row(c.execute("SELECT * FROM invites WHERE id=?", (iid,)).fetchone())
        once["invite"] = _public_invite(row)
        return {"invite": _traced(once["invite"]), "shown_once": True, "queue_item": _queue_summary(item),
                "label": "Invite email drafted from a template (AI not used)"}

    res = runtime.execute(tenant, "team", f"Invite a {ROLE_LABEL[role]}", plan, fn, actor=actor)
    out = {**res["result"], **once}
    out["run_id"] = res["run_id"]
    return out


def _cleanup_from_others(tenant, keep_id: int, email: str) -> None:
    """Earlier open invites to the same address were cancelled; close their queued emails."""
    with tenant.db() as c:
        rows = db.rows(c.execute("SELECT id, queue_id FROM invites WHERE lower(email)=? AND id<>? AND revoked IS NOT NULL "
                                 "AND queue_id IS NOT NULL", (email, keep_id)))
    for r in rows:
        _retire_queue_copy(tenant, r["queue_id"], "Replaced by a newer invite")


def list_invites(tenant, limit: int = 300) -> list[dict]:
    now = time.time()
    with tenant.db() as c:
        rows = db.rows(c.execute("SELECT * FROM invites WHERE COALESCE(kind,'invite') IN ('invite','reset') "
                                 "ORDER BY id DESC LIMIT ?", (limit,)))
    return [_public_invite(r, now) for r in rows]


def revoke_invite(tenant, iid: int, actor: str) -> dict:
    with tenant.db() as c:
        r = db.row(c.execute("SELECT * FROM invites WHERE id=?", (iid,)).fetchone())
        if r is None or (r.get("kind") or "invite") not in ("invite", "reset"):
            raise TeamError(404, "Not found")
        if r["used"]:
            raise TeamError(409, "That link was already used.")
        if not r["revoked"]:
            c.execute("UPDATE invites SET revoked=? WHERE id=?", (time.time(), iid))
    _retire_queue_copy(tenant, r["queue_id"], "Link cancelled by the owner")
    audit.log(tenant, actor, "team.invite_revoke", {"invite_id": iid})
    with tenant.db() as c:
        return _public_invite(db.row(c.execute("SELECT * FROM invites WHERE id=?", (iid,)).fetchone()))


def _retire_queue_copy(tenant, qid: int | None, reason: str) -> None:
    """The link in a queued email is dead or delivered: close an open item and scrub the token from every copy."""
    if not qid:
        return
    item = queue.get(tenant, qid, scrub=False)
    if item is None:
        return
    if item["status"] in queue.OPEN:
        try:
            queue.reject(tenant, qid, "system", "owner", reason)
        except queue.QueueError:
            pass
    scrub_link(tenant, qid)


def scrub_link(tenant, qid: int) -> None:
    with tenant.db() as c:
        r = c.execute("SELECT body FROM queue WHERE id=?", (qid,)).fetchone()
        if r and _TOKEN_IN_LINK.search(r["body"] or ""):
            c.execute("UPDATE queue SET body=? WHERE id=?", (_TOKEN_IN_LINK.sub(r"\1[link removed]", r["body"]), qid))
        for m in c.execute("SELECT id, body FROM channel_messages WHERE json_extract(meta,'$.queue_id')=?", (qid,)).fetchall():
            if _TOKEN_IN_LINK.search(m["body"] or ""):
                c.execute("UPDATE channel_messages SET body=? WHERE id=?", (_TOKEN_IN_LINK.sub(r"\1[link removed]", m["body"]), m["id"]))


def cleanup(tenant) -> int:
    """Lazy housekeeping: scrub links of expired invites from their queue copies. Returns how many were retired."""
    now = time.time()
    with tenant.db() as c:
        rows = db.rows(c.execute("SELECT id, queue_id FROM invites WHERE used IS NULL AND revoked IS NULL AND expires<? "
                                 "AND queue_id IS NOT NULL", (now,)))
        for r in rows:
            c.execute("UPDATE invites SET revoked=? WHERE id=?", (now, r["id"]))
    for r in rows:
        _retire_queue_copy(tenant, r["queue_id"], "Link expired")
    return len(rows)


@queue.on_sent("invite")
@queue.on_sent("password_reset")
def _after_invite_sent(tenant, item: dict, actor: str) -> None:
    """Once the email has really gone out, the only copy of the link left is in the recipient's inbox.
    A practice (mock) delivery in the real workspace reached nobody, so the owner keeps the copy to send by hand."""
    delivered = str(item.get("delivered") or "")
    if delivered.startswith("mock:") and not tenant.is_demo:
        return
    scrub_link(tenant, item["id"])


# --------------------------------------------------------------------------- accept (public)
def _fails(c, key: str) -> int:
    return c.execute("SELECT COUNT(*) FROM login_fails WHERE key=? AND at>?", (key, time.time() - JOIN_WINDOW)).fetchone()[0]


def _throttle(tenant, ip: str) -> None:
    with tenant.db() as c:
        if _fails(c, f"join:{ip}") >= JOIN_MAX_FAILS:
            raise TeamError(429, "Too many attempts with links that don't work. Try again in 15 minutes.")


def _fail(tenant, ip: str) -> None:
    with tenant.db() as c:
        c.execute("INSERT INTO login_fails(key,at) VALUES(?,?)", (f"join:{ip}", time.time()))


def _lookup(tenant, token: str, ip: str) -> dict:
    _throttle(tenant, ip)
    tok = (token or "").strip()
    row = None
    if 16 <= len(tok) <= 200 and re.fullmatch(r"[A-Za-z0-9_\-]+", tok):
        with tenant.db() as c:
            row = db.row(c.execute("SELECT * FROM invites WHERE token_hash=?", (_hash(tok),)).fetchone())
    if row is None or (row.get("kind") or "invite") not in ("invite", "reset"):
        _fail(tenant, ip)
        audit.log(tenant, "public", "team.join_fail", {"ip": ip, "reason": "unknown"})
        raise TeamError(404, "This link isn't valid. Check you copied all of it, or ask for a new one.")
    st = _public_invite(row)["state"]
    if st != "open":
        _fail(tenant, ip)
        audit.log(tenant, "public", "team.join_fail", {"ip": ip, "reason": st, "invite_id": row["id"]})
        msg = {"used": "This link has already been used. Sign in, or ask for a new link.",
               "cancelled": "This link was cancelled. Ask for a new one.",
               "expired": "This link has expired. Ask for a new one."}[st]
        raise TeamError(410, msg)
    return row


def check_token(tenant, token: str, ip: str) -> dict:
    row = _lookup(tenant, token, ip)
    kind = row.get("kind") or "invite"
    out = {"valid": True, "kind": kind, "role": row["role"], "role_label": ROLE_LABEL.get(row["role"], row["role"]),
           "email": row["email"], "display": row.get("display") or "", "brand": _brand(tenant),
           "expires": row["expires"], "is_demo": tenant.is_demo}
    if kind == "reset":
        with tenant.db() as c:
            u = _user(c, row["user_id"] or 0)
        out["username"] = u["username"] if u else None
        out["display"] = (u or {}).get("display") or ""
    return out


def _link_student(c, tenant, inv: dict, name: str, email: str) -> tuple[int, int | None]:
    sid = inv.get("student_id")
    s = None
    if sid:
        s = db.row(c.execute("SELECT id, coach_id FROM students WHERE id=?", (sid,)).fetchone())
        if s and c.execute("SELECT 1 FROM users WHERE student_id=? AND deleted IS NULL", (sid,)).fetchone():
            raise TeamError(409, "That student record already has an account. Ask the owner for help.")
    if s is None:
        s = db.row(c.execute("SELECT id, coach_id FROM students WHERE lower(email)=? AND id NOT IN "
                             "(SELECT student_id FROM users WHERE student_id IS NOT NULL AND deleted IS NULL) "
                             "ORDER BY id LIMIT 1", (email,)).fetchone())
    coach = inv.get("coach_id")
    if coach:
        co = _user(c, coach)
        if co is None or co["role"] != "coach" or not co["active"]:
            coach = None  # the coach picked at invite time was switched off, deleted or moved since
    if s is None:
        sid = c.execute("INSERT INTO students(name,email,coach_id,joined,stage,clients,demo) VALUES(?,?,?,?,?,0,?)",
                        (name, email, coach, time.time(), "foundations", 1 if tenant.is_demo else 0)).lastrowid
        return sid, coach
    if coach:
        c.execute("UPDATE students SET coach_id=? WHERE id=?", (coach, s["id"]))
    return s["id"], coach or s.get("coach_id")


def accept(tenant, token: str, password: str, ip: str, display: str | None = None) -> tuple[str, dict]:
    """Use a link: create the account (invite) or set a new password (reset). Returns (session token, user)."""
    row = _lookup(tenant, token, ip)
    kind = row.get("kind") or "invite"
    email = norm_email(row["email"])
    if kind == "reset":
        with tenant.db() as c:
            u = _user(c, row["user_id"] or 0)
        if u is None or not u["active"] or u["role"] == "owner":
            raise TeamError(410, "This account can't be reset from a link. Ask the owner.")
        probs = password_problems(password, u.get("email") or u["username"], u.get("display") or "")
        if probs:
            raise TeamError(422, " ".join(probs), {"problems": probs})
        pw_hash = auth.hash_pw(password)
        now = time.time()
        with tenant.db() as c, db.tx(c):
            if c.execute("UPDATE invites SET used=? WHERE id=? AND used IS NULL AND revoked IS NULL AND expires>?",
                         (now, row["id"], now)).rowcount != 1:
                raise TeamError(410, "This link has already been used. Sign in, or ask for a new link.")
            c.execute("UPDATE users SET pw_hash=? WHERE id=?", (pw_hash, u["id"]))
            ended = _kill_sessions(c, u["id"])
            c.execute("UPDATE team_reset_requests SET handled=? WHERE user_id=? AND handled IS NULL", (now, u["id"]))
        _retire_queue_copy(tenant, row.get("queue_id"), "Link already used")
        audit.log(tenant, u["username"], "team.password_reset", {"user_id": u["id"], "invite_id": row["id"],
                                                                  "sessions_ended": ended, "ip": ip})
        session = auth.start_session(tenant, u["id"], ip)
        with tenant.db() as c:
            return session, auth.user_public(_user(c, u["id"]))

    name = clean_display(display) or row.get("display") or email.split("@")[0]
    if _banned_name(tenant, name):
        raise TeamError(422, "That name can't be used in this workspace. Choose another name.")
    name = _demo_label(tenant, name)
    probs = password_problems(password, email, name)
    if probs:
        raise TeamError(422, " ".join(probs), {"problems": probs})
    pw_hash = auth.hash_pw(password)
    now = time.time()
    with tenant.db() as c, db.tx(c):
        if c.execute("UPDATE invites SET used=? WHERE id=? AND used IS NULL AND revoked IS NULL AND expires>?",
                     (now, row["id"], now)).rowcount != 1:
            raise TeamError(410, "This link has already been used. Sign in, or ask for a new link.")
        taken = _email_taken(c, email)
        if taken:
            raise TeamError(409, "An account with this email already exists. Sign in, or ask the owner to switch it back on.")
        sid, coach = (None, None)
        if row["role"] == "student":
            sid, coach = _link_student(c, tenant, row, name, email)
        uid = c.execute("INSERT INTO users(username,display,role,pw_hash,coach_id,student_id,created,email) "
                        "VALUES(?,?,?,?,?,?,?,?)", (email, name, row["role"], pw_hash, coach, sid, now, email)).lastrowid
        c.execute("UPDATE invites SET user_id=? WHERE id=?", (uid, row["id"]))
    _retire_queue_copy(tenant, row.get("queue_id"), "Invite already accepted")
    audit.log(tenant, email, "team.join", {"user_id": uid, "role": row["role"], "invite_id": row["id"],
                                           "student_id": sid, "ip": ip})
    session = auth.start_session(tenant, uid, ip)
    with tenant.db() as c:
        return session, auth.user_public(_user(c, uid))


def request_reset(tenant, ident: str, ip: str) -> dict:
    """Public 'forgot password': records a request for the owner (no email is sent, nothing is revealed)."""
    generic = {"ok": True, "message": "If that account exists, the workspace owner has been asked to send you a reset link."}
    with tenant.db() as c:
        if _fails(c, f"forgot:{ip}") >= FORGOT_MAX:
            raise TeamError(429, "Too many requests. Try again in 15 minutes.")
        c.execute("INSERT INTO login_fails(key,at) VALUES(?,?)", (f"forgot:{ip}", time.time()))
        key = norm_email(ident)
        u = db.row(c.execute("SELECT * FROM users WHERE deleted IS NULL AND active=1 AND role<>'owner' AND "
                             "(lower(username)=? OR lower(email)=?)", (key, key)).fetchone()) if key else None
        if u is None or shared_demo_account(tenant, u):
            return generic
        recent = c.execute("SELECT 1 FROM team_reset_requests WHERE user_id=? AND handled IS NULL AND created>?",
                           (u["id"], time.time() - 86400)).fetchone()
        if recent:
            return generic
        rid = c.execute("INSERT INTO team_reset_requests(user_id,created) VALUES(?,?)", (u["id"], time.time())).lastrowid
    audit.log(tenant, "public", "team.reset_request", {"user_id": u["id"], "request_id": rid, "ip": ip})
    try:
        from . import escalations

        escalations.raise_(tenant, "password_reset_request", "low",
                           f"{u.get('display') or 'A team member'} asked for a password reset link (Team & invites).",
                           ref=f"user:{u['id']}")
    except Exception:  # noqa: BLE001 - escalations land with another feature; the Team screen lists requests anyway
        pass
    return generic


def reset_requests(tenant) -> list[dict]:
    with tenant.db() as c:
        return db.rows(c.execute("SELECT r.id, r.user_id, r.created, u.display, u.role FROM team_reset_requests r "
                                 "JOIN users u ON u.id=r.user_id WHERE r.handled IS NULL AND u.deleted IS NULL "
                                 "ORDER BY r.id DESC LIMIT 100"))


def create_reset_link(tenant, uid: int, actor: str, *, send_email: bool = True, origin: str | None = None,
                      inviter: str = "") -> dict:
    from . import runtime

    u = _target(tenant, uid)
    if not u["active"]:
        raise TeamError(409, "Switch this account back on first.")
    email = norm_email(u.get("email") or (u["username"] if valid_email(u["username"]) else ""))
    plan = ["Cancel older reset links", "Create a one-time link (only its fingerprint is stored)",
            "Draft the reset email from a template", "Put the email in the approval queue"]
    once: dict = {}

    def fn(run):
        token = secrets.token_urlsafe(32)
        now = time.time()
        expires = now + RESET_TTL

        def _insert():
            with tenant.db() as c, db.tx(c):
                old = db.rows(c.execute("SELECT id, queue_id FROM invites WHERE user_id=? AND kind='reset' AND used IS NULL "
                                        "AND revoked IS NULL", (uid,)))
                c.execute("UPDATE invites SET revoked=? WHERE user_id=? AND kind='reset' AND used IS NULL AND revoked IS NULL",
                          (now, uid))
                iid = c.execute("INSERT INTO invites(email,role,token_hash,created_by,created,expires,kind,display,user_id,demo) "
                                "VALUES(?,?,?,?,?,?,'reset',?,?,?)",
                                (email or None, u["role"], _hash(token), actor, now, expires, u.get("display"), uid,
                                 1 if tenant.is_demo else 0)).lastrowid
                c.execute("UPDATE team_reset_requests SET handled=? WHERE user_id=? AND handled IS NULL", (now, uid))
            for o in old:
                _retire_queue_copy(tenant, o["queue_id"], "Replaced by a newer reset link")
            return iid

        iid = run.step("create_link", {"user_id": uid, "expires_in_hours": RESET_TTL // 3600}, _insert,
                       label="Created a one-time reset link")
        path = join_path(tenant, token)
        full = base_url(origin) + path
        once.update(link=path, full_link=full if base_url(origin) else None)
        item = None
        if send_email and email and not (tenant.is_demo and not email.endswith("@example.invalid")):
            def _queue():
                nonlocal item
                item = _queue_invite_email(tenant, iid, kind="reset", email=email, name=u.get("display") or "",
                                           role=u["role"], inviter=inviter, link=full, expires=expires, actor=actor,
                                           run_id=run.id)
                return _queue_summary(item)

            run.step("queue_email", {"to": mask_email(email), "kind": "reset"}, _queue,
                     label="Drafted the reset email (template) and queued it for approval")
        audit.log(tenant, actor, "team.reset_link", {"user_id": uid, "invite_id": iid,
                                                     "queued": item["id"] if item else None})
        with tenant.db() as c:
            row = db.row(c.execute("SELECT * FROM invites WHERE id=?", (iid,)).fetchone())
        once["invite"] = _public_invite(row)
        return {"invite": _traced(once["invite"]), "shown_once": True, "queue_item": _queue_summary(item),
                "no_email": not email, "label": "Reset email drafted from a template (AI not used)"}

    res = runtime.execute(tenant, "team", f"Password reset link for user #{uid}", plan, fn, actor=actor)
    out = {**res["result"], **once}
    out["run_id"] = res["run_id"]
    return out


# --------------------------------------------------------------------------- people
def list_users(tenant) -> list[dict]:
    now = time.time()
    with tenant.db() as c:
        users = db.rows(c.execute("SELECT id, username, display, role, coach_id, student_id, active, created, email "
                                  "FROM users WHERE deleted IS NULL"))
        sess = {r["user_id"]: r["n"] for r in c.execute("SELECT user_id, COUNT(*) n FROM sessions WHERE expires>? "
                                                        "GROUP BY user_id", (now,))}
        per_coach = {r["coach_id"]: r["n"] for r in c.execute("SELECT coach_id, COUNT(*) n FROM students "
                                                              "WHERE coach_id IS NOT NULL GROUP BY coach_id")}
        assigns = db.rows(c.execute("SELECT manager_id, member_id FROM team_assignments"))
        student_coach = {r["id"]: r["coach_id"] for r in c.execute(
            "SELECT s.id, s.coach_id FROM students s JOIN users u ON u.student_id=s.id WHERE u.deleted IS NULL")}
    names = {u["id"]: u["display"] for u in users}
    for u in users:
        if u["student_id"] and student_coach.get(u["student_id"]):
            u["coach_id"] = student_coach[u["student_id"]]  # the student record is the source of truth
        u["active"] = bool(u["active"])
        u["role_label"] = ROLE_LABEL.get(u["role"], u["role"])
        u["sessions"] = sess.get(u["id"], 0)
        u["coach"] = names.get(u["coach_id"]) if u["coach_id"] else None
        u["students"] = per_coach.get(u["id"], 0) if u["role"] == "coach" else None
        u["managers"] = [{"id": a["manager_id"], "display": names.get(a["manager_id"])} for a in assigns
                         if a["member_id"] == u["id"] and a["manager_id"] in names]
        u["manages"] = [{"id": a["member_id"], "display": names.get(a["member_id"])} for a in assigns
                        if a["manager_id"] == u["id"] and a["member_id"] in names]
        u["demo"] = "(DEMO)" in (u["display"] or "")
        u["shared_demo"] = shared_demo_account(tenant, u)
    users.sort(key=lambda u: (not u["active"], ROLE_ORDER.get(u["role"], 9), (u["display"] or "").lower()))
    return users


def set_role(tenant, uid: int, role: str, actor: str) -> dict:
    if role not in INVITE_ROLES:
        raise TeamError(422, "Pick one of: coach, setter, closer, student.")
    u = _target(tenant, uid)
    old = u["role"]
    warning = None
    with tenant.db() as c, db.tx(c):
        c.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
        if role == "student" and not u.get("student_id"):
            email = norm_email(u.get("email") or u["username"])
            s = c.execute("SELECT id FROM students WHERE lower(email)=? AND id NOT IN (SELECT student_id FROM users "
                          "WHERE student_id IS NOT NULL AND deleted IS NULL) LIMIT 1", (email,)).fetchone() if email else None
            sid = s["id"] if s else c.execute(
                "INSERT INTO students(name,email,coach_id,joined,stage,clients,demo) VALUES(?,?,?,?,?,0,?)",
                (u["display"], email or None, None, time.time(), "foundations", 1 if tenant.is_demo else 0)).lastrowid
            c.execute("UPDATE users SET student_id=? WHERE id=?", (sid, uid))
        if role not in ("setter", "closer"):
            c.execute("DELETE FROM team_assignments WHERE member_id=?", (uid,))
        if role == "student":
            c.execute("DELETE FROM team_assignments WHERE manager_id=?", (uid,))
        if old == "coach" and role != "coach":
            n = c.execute("SELECT COUNT(*) FROM students WHERE coach_id=?", (uid,)).fetchone()[0]
            if n:
                warning = f"{n} student(s) are still assigned to this person as their coach. Reassign them under Students."
    audit.log(tenant, actor, "team.role", {"user_id": uid, "from": old, "to": role})
    return {"ok": True, "user_id": uid, "role": role, "warning": warning}


def set_active(tenant, uid: int, active: bool, actor: str) -> dict:
    u = _target(tenant, uid)
    ended = 0
    open_links: list[dict] = []
    with tenant.db() as c, db.tx(c):
        c.execute("UPDATE users SET active=? WHERE id=?", (1 if active else 0, uid))
        if not active:
            ended = _kill_sessions(c, uid)
            # an unused reset link for a switched-off account must not work, and its queued email must not go out
            open_links = db.rows(c.execute("SELECT id, queue_id FROM invites WHERE user_id=? AND kind='reset' AND used IS NULL "
                                           "AND revoked IS NULL", (uid,)))
            c.execute("UPDATE invites SET revoked=? WHERE user_id=? AND kind='reset' AND used IS NULL AND revoked IS NULL",
                      (time.time(), uid))
    for r in open_links:
        _retire_queue_copy(tenant, r["queue_id"], "Account switched off")
    audit.log(tenant, actor, "team.activate" if active else "team.deactivate",
              {"user_id": uid, "role": u["role"], "sessions_ended": ended})
    return {"ok": True, "user_id": uid, "active": active, "sessions_ended": ended}


def assign_coach(tenant, student_id: int, coach_id: int | None, actor: str) -> dict:
    with tenant.db() as c, db.tx(c):
        s = c.execute("SELECT id FROM students WHERE id=?", (student_id,)).fetchone()
        if s is None:
            raise TeamError(404, "Not found")
        if coach_id is not None:
            co = _user(c, coach_id)
            if co is None or co["role"] != "coach" or not co["active"]:
                raise TeamError(422, "Pick an active coach.")
        c.execute("UPDATE students SET coach_id=? WHERE id=?", (coach_id, student_id))
        c.execute("UPDATE users SET coach_id=? WHERE student_id=? AND deleted IS NULL", (coach_id, student_id))
    audit.log(tenant, actor, "team.coach", {"student_id": student_id, "coach_id": coach_id})
    return {"ok": True, "student_id": student_id, "coach_id": coach_id}


def add_assignment(tenant, manager_id: int, member_id: int, actor: str) -> dict:
    if manager_id == member_id:
        raise TeamError(422, "A person can't manage themselves.")
    with tenant.db() as c:
        mgr, mem = _user(c, manager_id), _user(c, member_id)
        if mgr is None or mem is None:
            raise TeamError(404, "Not found")
        if mem["role"] not in ("setter", "closer"):
            raise TeamError(422, "Only setters and closers are assigned to a manager.")
        if mgr["role"] == "student" or not mgr["active"] or not mem["active"]:
            raise TeamError(422, "Pick an active owner, coach, closer or setter as the manager.")
        c.execute("INSERT OR IGNORE INTO team_assignments(manager_id, member_id) VALUES(?,?)", (manager_id, member_id))
    audit.log(tenant, actor, "team.assign", {"manager_id": manager_id, "member_id": member_id})
    return {"ok": True}


def remove_assignment(tenant, manager_id: int, member_id: int, actor: str) -> dict:
    with tenant.db() as c:
        n = c.execute("DELETE FROM team_assignments WHERE manager_id=? AND member_id=?", (manager_id, member_id)).rowcount
    if not n:
        raise TeamError(404, "Not found")
    audit.log(tenant, actor, "team.unassign", {"manager_id": manager_id, "member_id": member_id})
    return {"ok": True}


def assignments(tenant) -> list[dict]:
    with tenant.db() as c:
        return db.rows(c.execute(
            "SELECT a.manager_id, a.member_id, m.display manager, m.role manager_role, p.display member, p.role member_role "
            "FROM team_assignments a JOIN users m ON m.id=a.manager_id JOIN users p ON p.id=a.member_id "
            "WHERE m.deleted IS NULL AND p.deleted IS NULL ORDER BY m.display, p.display"))


# --------------------------------------------------------------------------- students
def list_students(tenant, q: str = "", limit: int = 300) -> dict:
    args: list = []
    where = "1=1"
    if q:
        where = "(lower(s.name) LIKE ? OR lower(COALESCE(s.email,'')) LIKE ?)"
        like = f"%{q.strip().lower()[:80]}%"
        args += [like, like]
    with tenant.db() as c:
        total = c.execute("SELECT COUNT(*) FROM students").fetchone()[0]
        rows = db.rows(c.execute(
            f"SELECT s.id, s.name, s.email, s.coach_id, s.niche, s.stage, s.demo, s.joined, "
            f"co.display coach, u.id user_id, u.active user_active, "
            f"(SELECT i.id FROM invites i WHERE i.student_id=s.id AND i.used IS NULL AND i.revoked IS NULL AND i.expires>? "
            f" ORDER BY i.id DESC LIMIT 1) open_invite "
            f"FROM students s LEFT JOIN users co ON co.id=s.coach_id "
            f"LEFT JOIN users u ON u.student_id=s.id AND u.deleted IS NULL WHERE {where} ORDER BY s.id DESC LIMIT ?",
            [time.time(), *args, limit]))
    for r in rows:
        r["account"] = ("active" if r["user_id"] and r["user_active"] else "switched off" if r["user_id"] else
                        "invited" if r["open_invite"] else "none")
        r["demo"] = bool(r["demo"])
    return {"items": rows, "total": total, "shown": len(rows)}


def parse_import(text: str) -> tuple[list[dict], list[dict]]:
    """CSV lines "name, email[, niche]" (a header row is optional). Returns (rows, problems)."""
    rows, problems = [], []
    reader = csv.reader(io.StringIO(text or ""))
    try:
        records = list(enumerate(reader, start=1))
    except csv.Error:
        raise TeamError(422, f"Line {reader.line_num or 1} can't be read: check for a missing quote or a very long "
                             "value. Paste one student per line: name, email, niche.")
    for n, rec in records:
        cells = [clean_display(x, 200) for x in rec]
        if not any(cells):
            continue
        if n == 1 and len(cells) >= 2 and cells[0].lower() in ("name", "full name") and "email" in cells[1].lower():
            continue
        if len(cells) < 2:
            problems.append({"line": n, "reason": "Needs at least a name and an email"})
            continue
        name, email = cells[0][:120], norm_email(cells[1])
        if not name:
            problems.append({"line": n, "reason": "Missing name"})
            continue
        if not valid_email(email):
            problems.append({"line": n, "reason": "Not a valid email"})
            continue
        rows.append({"line": n, "name": name, "email": email, "niche": (cells[2][:120] if len(cells) > 2 else "")})
        if len(rows) > IMPORT_MAX_ROWS:
            raise TeamError(422, f"Import at most {IMPORT_MAX_ROWS} students at a time.")
    return rows, problems


def import_students(tenant, text: str, actor: str, *, coach_id: int | None = None, invite: bool = False,
                    origin: str | None = None, inviter: str = "") -> dict:
    from . import runtime

    rows, problems = parse_import(text)
    if not rows and not problems:
        raise TeamError(422, "Paste at least one line: name, email[, niche].")
    if coach_id is not None:
        with tenant.db() as c:
            co = _user(c, coach_id)
        if co is None or co["role"] != "coach" or not co["active"]:
            raise TeamError(422, "Pick an active coach.")
    plan = ["Read the pasted list", "Skip duplicates and invalid lines", "Create student records",
            "Draft invites for approval" if invite else "No invites (records only)"]

    def fn(run):
        created, skipped = [], list(problems)
        seen: set[str] = set()

        def _write():
            with tenant.db() as c, db.tx(c):
                for r in rows:
                    em = r["email"]
                    if em in seen:
                        skipped.append({"line": r["line"], "reason": "Duplicate in this list"})
                        continue
                    seen.add(em)
                    if tenant.is_demo and not em.endswith("@example.invalid"):
                        skipped.append({"line": r["line"], "reason": "The pitch demo only takes @example.invalid addresses"})
                        continue
                    if c.execute("SELECT 1 FROM students WHERE lower(email)=?", (em,)).fetchone():
                        skipped.append({"line": r["line"], "reason": "Already a student"})
                        continue
                    name = _demo_label(tenant, r["name"])
                    sid = c.execute("INSERT INTO students(name,email,coach_id,joined,niche,stage,clients,demo) "
                                    "VALUES(?,?,?,?,?,?,0,?)", (name, em, coach_id, time.time(), r["niche"] or None,
                                                                "foundations", 1 if tenant.is_demo else 0)).lastrowid
                    created.append({"student_id": sid, "name": name, "email": em})
            return {"created": len(created), "skipped": len(skipped)}

        run.step("import", {"lines": len(rows) + len(problems)}, _write, label="Created student records")
        invites, not_invited = links, 0
        if invite:
            for s in created[:IMPORT_MAX_INVITES]:
                if _on_never_invite_list(tenant, s["email"]):
                    not_invited += 1
                    continue
                try:
                    r = create_invite(tenant, actor, email=s["email"], role="student", display=s["name"],
                                      coach_id=coach_id, student_id=s["student_id"], send_email=True, origin=origin,
                                      inviter=inviter)
                    invites.append({"student_id": s["student_id"], "email": s["email"], "link": r["link"],
                                    "queue_id": (r["queue_item"] or {}).get("id")})
                except TeamError:
                    not_invited += 1
            not_invited += max(0, len(created) - IMPORT_MAX_INVITES)
            run.note("invites", {"requested": len(created)}, {"drafted": len(invites), "not_invited": not_invited},
                     label="Drafted invites for approval")
        audit.log(tenant, actor, "team.import", {"created": len(created), "skipped": len(skipped), "invites": len(invites)})
        # the trace keeps counts and line numbers; names, addresses and one-time links go back to the owner only
        made.extend(created)
        return {"created": len(created), "skipped": skipped, "invites": len(invites), "not_invited": not_invited}

    links: list[dict] = []
    made: list[dict] = []
    res = runtime.execute(tenant, "team", "Import students", plan, fn, actor=actor)
    out = {**res["result"], "created": made, "invites": links}
    out["run_id"] = res["run_id"]
    return out


# --------------------------------------------------------------------------- data rights
_SECRET_COLS = re.compile(r"(^|_)(hash|token|secret|password)$|^share_token$", re.I)
# Personal tables scrubbed on delete: (table, column holding the user id). Missing tables are skipped.
PERSONAL_TABLES = [
    ("user_memory", "user_id"), ("setup_progress", "user_id"), ("queries", "user_id"), ("projects", "user_id"),
    ("memories", "user_id"), ("uploads", "user_id"), ("voice_sessions", "user_id"), ("drill_cards", "user_id"),
    ("academy_progress", "user_id"), ("academy_attempts", "user_id"), ("academy_review", "user_id"),
    ("roleplays", "user_id"), ("certifications", "user_id"), ("channel_links", "user_id"),
    ("team_reset_requests", "user_id"), ("prospects", "owner_user_id"), ("proposals", "owner_user_id"),
]
EXPORT_LIMIT = 5000
# what the owner reads after a delete (plain words, not table names)
REMOVED_LABEL = {
    "user_memory": "“What I know about you” entries", "setup_progress": "checklist ticks", "queries": "questions asked",
    "projects": "projects", "memories": "project notes", "uploads": "uploaded files", "voice_sessions": "voice sessions",
    "drill_cards": "practice cards", "academy_progress": "lesson progress records", "academy_attempts": "quiz answers",
    "academy_review": "review cards", "roleplays": "role-plays", "certifications": "certifications",
    "channel_links": "linked chat accounts", "team_reset_requests": "password reset requests", "prospects": "prospects",
    "proposals": "proposals", "threads": "chats", "missions": "missions", "team_assignments": "team assignments",
    "invites": "invite and reset links closed", "students_unassigned": "students now without a coach",
    "checkin_notes": "check-in notes cleared",
}


def _tables(c) -> dict[str, set[str]]:
    out = {}
    for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        out[r["name"]] = {x["name"] for x in c.execute(f"PRAGMA table_info({r['name']})")}
    return out


def _clean_row(r: dict) -> dict:
    return {k: (db.jload(v, v) if isinstance(v, str) and v[:1] in "[{" else v) for k, v in r.items()
            if not _SECRET_COLS.search(k)}


def export_user(tenant, uid: int, actor: str) -> dict:
    """Everything this workspace holds about one person, as JSON (no password or token fingerprints)."""
    with tenant.db() as c:
        u = db.row(c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())
        if u is None or u.get("deleted"):
            raise TeamError(404, "Not found")
        tables = _tables(c)
        data: dict = {"account": _clean_row(u)}
        sid = u.get("student_id")
        if sid:
            data["student_record"] = _clean_row(db.row(c.execute("SELECT * FROM students WHERE id=?", (sid,)).fetchone()) or {})
            data["checkins"] = [_clean_row(dict(r)) for r in c.execute("SELECT * FROM checkins WHERE student_id=? LIMIT ?",
                                                                        (sid, EXPORT_LIMIT)).fetchall()]
            data["coach_tasks"] = [_clean_row(dict(r)) for r in c.execute("SELECT * FROM coach_tasks WHERE student_id=? LIMIT ?",
                                                                           (sid, EXPORT_LIMIT)).fetchall()]
        th = [_clean_row(dict(r)) for r in c.execute("SELECT * FROM threads WHERE user_id=? LIMIT ?", (uid, EXPORT_LIMIT))]
        for t in th:
            t["messages"] = [_clean_row(dict(m)) for m in c.execute("SELECT * FROM messages WHERE thread_id=? LIMIT ?",
                                                                     (t["id"], EXPORT_LIMIT))]
        data["threads"] = th
        skip = {"users", "sessions", "login_fails", "threads", "messages", "checkins", "coach_tasks", "students"}
        for name, cols in sorted(tables.items()):
            if name in skip:
                continue
            col = "user_id" if "user_id" in cols else "owner_user_id" if "owner_user_id" in cols else None
            if col:
                rows = [_clean_row(dict(r)) for r in c.execute(f"SELECT * FROM {name} WHERE {col}=? LIMIT ?", (uid, EXPORT_LIMIT))]
                if rows:
                    data[name] = rows
        data["team_assignments"] = db.rows(c.execute("SELECT * FROM team_assignments WHERE manager_id=? OR member_id=?",
                                                     (uid, uid)))
        data["queue_items_drafted_for_them"] = [
            _clean_row({k: r[k] for k in ("id", "kind", "channel", "title", "body", "status", "created", "decided_at")})
            for r in c.execute("SELECT * FROM queue WHERE json_extract(meta,'$.owner_user_id')=? LIMIT ?", (uid, EXPORT_LIMIT))]
        addr = norm_email(u.get("email") or (u["username"] if valid_email(u["username"]) else ""))
        data["team_emails_to_them"] = [
            _clean_row({k: r[k] for k in ("id", "kind", "channel", "title", "body", "status", "created", "decided_at")})
            for r in c.execute("SELECT * FROM queue WHERE kind IN ('invite','password_reset') AND lower(recipient_ref)=? "
                               "LIMIT ?", (addr, EXPORT_LIMIT))] if addr else []
        # exact actor match: a LIKE pattern would let '_' or '%' in an address pull in someone else's sign-ins
        uname = u["username"]
        data["sign_in_activity"] = db.rows(c.execute(
            "SELECT at, kind FROM audit WHERE (actor=? OR substr(actor, 1, ?)=?) AND kind IN "
            "('login','logout','login.fail','team.join','team.password_reset') ORDER BY id DESC LIMIT 500",
            (uname, len(uname) + 1, uname + "(")))
        data["active_sessions"] = c.execute("SELECT COUNT(*) FROM sessions WHERE user_id=? AND expires>?",
                                            (uid, time.time())).fetchone()[0]
    _strip_tokens(data)
    audit.log(tenant, actor, "team.export", {"user_id": uid, "sections": sorted(data)})
    return {"exported_at": time.time(), "workspace": tenant.id, "user_id": uid,
            "note": "Everything this workspace stores about this person. Password and link fingerprints are never included.",
            "data": data}


def _strip_tokens(obj):
    """Join links inside exported text never carry a live token."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str):
                obj[k] = _TOKEN_IN_LINK.sub(r"\1[link removed]", v)
            else:
                _strip_tokens(v)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, str):
                obj[i] = _TOKEN_IN_LINK.sub(r"\1[link removed]", v)
            else:
                _strip_tokens(v)


def delete_user(tenant, uid: int, confirm: str, actor: str, actor_id: int | None = None) -> dict:
    """Soft-delete + scrub. The account row stays (so approval history and the audit log still make sense) but its
    name, email and password are replaced and every personal record is removed. Open drafts in their name are rejected."""
    if actor_id is not None and uid == actor_id:
        raise TeamError(409, "You can't delete your own account here.")
    u = _target(tenant, uid)
    if (confirm or "").strip() != u["username"]:
        raise TeamError(422, "Type the person's username exactly to confirm.")
    removed: dict[str, int] = {}
    now = time.time()
    with tenant.db() as c, db.tx(c):
        tables = _tables(c)
        for name, col in PERSONAL_TABLES:
            if name in tables and col in tables[name]:
                removed[name] = c.execute(f"DELETE FROM {name} WHERE {col}=?", (uid,)).rowcount
        if "threads" in tables:
            tids = [r["id"] for r in c.execute("SELECT id FROM threads WHERE user_id=?", (uid,)).fetchall()]
            for tid in tids:
                c.execute("DELETE FROM messages WHERE thread_id=?", (tid,))
            removed["threads"] = c.execute("DELETE FROM threads WHERE user_id=?", (uid,)).rowcount
        if "missions" in tables:
            mids = [r["id"] for r in c.execute("SELECT id FROM missions WHERE user_id=?", (uid,)).fetchall()]
            for mid in mids:
                for child in ("mission_steps", "mission_events"):
                    if child in tables:
                        c.execute(f"DELETE FROM {child} WHERE mission_id=?", (mid,))
            removed["missions"] = c.execute("DELETE FROM missions WHERE user_id=?", (uid,)).rowcount
        removed["team_assignments"] = c.execute("DELETE FROM team_assignments WHERE manager_id=? OR member_id=?",
                                                (uid, uid)).rowcount
        sid = u.get("student_id")
        # every link about this person (their invites, reset links, invites tied to their student record) stops
        # working, and a queued email carrying one is withdrawn below
        mine = "(user_id=? OR lower(email)=? OR (? IS NOT NULL AND student_id=?))"
        args = (uid, norm_email(u.get("email") or u["username"]), sid, sid)
        open_links = db.rows(c.execute(f"SELECT queue_id FROM invites WHERE {mine} AND used IS NULL AND queue_id IS NOT NULL",
                                       args))
        removed["invites"] = c.execute(f"UPDATE invites SET email=NULL, display=NULL, revoked=COALESCE(revoked, ?) "
                                       f"WHERE {mine}", (now, *args)).rowcount
        if u["role"] == "coach" or c.execute("SELECT 1 FROM students WHERE coach_id=?", (uid,)).fetchone():
            # their students go back to 'no coach yet' instead of pointing at a deleted account
            removed["students_unassigned"] = c.execute("UPDATE students SET coach_id=NULL WHERE coach_id=?", (uid,)).rowcount
            c.execute("UPDATE users SET coach_id=NULL WHERE coach_id=?", (uid,))
        if sid:
            c.execute("UPDATE students SET name='Deleted student', email=NULL, intake=NULL, plan=NULL, niche=NULL "
                      "WHERE id=?", (sid,))
            removed["checkin_notes"] = c.execute("UPDATE checkins SET wins='', blockers='' WHERE student_id=?", (sid,)).rowcount
        _kill_sessions(c, uid)
        c.execute("UPDATE users SET username=?, display='Deleted user', email=NULL, pw_hash=?, active=0, deleted=?, "
                  "coach_id=NULL WHERE id=?", (f"deleted-{uid}-{secrets.token_hex(3)}", auth.hash_pw(secrets.token_urlsafe(24)),
                                               now, uid))
    for r in open_links:
        _retire_queue_copy(tenant, r["queue_id"], "Account deleted")
    rejected = 0
    for item in queue.list_items(tenant, status="open", limit=5000):
        if item["meta"].get("owner_user_id") == uid:
            try:
                queue.reject(tenant, item["id"], "system", "owner", "Account deleted")
                rejected += 1
            except queue.QueueError:
                pass
    audit.log(tenant, actor, "team.delete", {"user_id": uid, "removed": removed, "drafts_rejected": rejected})
    kept = {k: v for k, v in removed.items() if v}
    return {"ok": True, "user_id": uid, "removed": kept, "drafts_rejected": rejected,
            "removed_list": [{"label": REMOVED_LABEL.get(k, k.replace("_", " ")), "count": v} for k, v in kept.items()],
            "kept": ["Approval history and the audit log (they prove what was sent and who approved it). "
                     "The account now shows as 'Deleted user'.",
                     "Check-in numbers without notes, so programme totals stay correct."]}


# --------------------------------------------------------------------------- overview
def overview(tenant) -> dict:
    cleanup(tenant)
    users = list_users(tenant)
    invites = list_invites(tenant)
    with tenant.db() as c:
        n_students = c.execute("SELECT COUNT(*) FROM students").fetchone()[0]
    email_live = False
    try:
        from .live import senders

        email_live = bool(senders.ready(tenant, "email"))
    except Exception:  # noqa: BLE001
        email_live = False
    coaches = [{"id": u["id"], "display": u["display"]} for u in users if u["role"] == "coach" and u["active"]]
    return {
        "users": users,
        "invites": invites,
        "reset_requests": reset_requests(tenant),
        "assignments": assignments(tenant),
        "coaches": coaches,
        "managers": [{"id": u["id"], "display": u["display"], "role": u["role"]} for u in users
                     if u["active"] and u["role"] != "student"],
        "members": [{"id": u["id"], "display": u["display"], "role": u["role"]} for u in users
                    if u["active"] and u["role"] in ("setter", "closer")],
        "counts": {"people": sum(1 for u in users if u["active"]), "switched_off": sum(1 for u in users if not u["active"]),
                   "open_invites": sum(1 for i in invites if i["state"] == "open"), "students": n_students},
        "email_live": email_live,
        "roles": [{"value": r, "label": ROLE_LABEL[r]} for r in INVITE_ROLES],
        "invite_days": INVITE_TTL // 86400, "reset_hours": RESET_TTL // 3600,
    }


# --------------------------------------------------------------------------- demo
def seed_demo(tenant) -> None:
    """Fictional team activity for the pitch demo (demo database only)."""
    from . import tenancy

    if not tenancy.is_demo_db(tenant):
        return
    now = time.time()
    with tenant.db() as c:
        ids = {r["username"]: r["id"] for r in c.execute("SELECT id, username FROM users")}
    setter, closer, student = ids.get("demo-setter"), ids.get("demo-closer"), ids.get("demo-student")
    coach2 = ids.get("demo-coach2")
    with tenant.db() as c, db.tx(c):
        for uname, row in (("demo-setter", "imani.setter@example.invalid"), ("demo-closer", "abel.closer@example.invalid"),
                           ("demo-coach", "rivera.coach@example.invalid"), ("demo-student", "ava.hartley@example.invalid"),
                           ("demo-coach2", "okafor.coach@example.invalid")):
            if uname in ids:
                c.execute("UPDATE users SET email=? WHERE id=?", (row, ids[uname]))
        if setter and closer:
            c.execute("INSERT OR IGNORE INTO team_assignments(manager_id, member_id) VALUES(?,?)", (closer, setter))
        c.execute("INSERT OR IGNORE INTO users(username,display,role,pw_hash,active,created,email) VALUES(?,?,?,?,0,?,?)",
                  ("demo-former-setter", "Former setter Quayle (DEMO)", "setter", auth.hash_pw(secrets.token_urlsafe(16)),
                   now - 40 * 86400, "former.setter@example.invalid"))
        c.execute("INSERT INTO invites(email,role,token_hash,created_by,created,expires,kind,display,demo) "
                            "VALUES(?,?,?,?,?,?,'invite',?,1)",
                            ("priya.tiwari@example.invalid", "student", _hash(secrets.token_urlsafe(32)), "demo-owner(owner)",
                             now - 10 * 86400, now - 3 * 86400, "Priya Tiwari (DEMO)"))
        if coach2:
            # a reset request the visitor can act on (the one-click accounts are shared, so they can't be reset)
            c.execute("INSERT INTO team_reset_requests(user_id, created) VALUES(?,?)", (coach2, now - 3600))
        if student:
            c.execute("INSERT OR IGNORE INTO setup_progress(user_id,item,done) VALUES(?,?,?)", (student, "meet_coach", now - 30 * 86400))
    # one open invite waiting for approval (its link works only inside the demo database)
    create_invite(tenant, "demo-owner(owner)", email="sam.rowe@example.invalid", role="setter",
                  display="Sam Rowe (DEMO)", send_email=True, inviter="Owner (DEMO)")
    try:
        from . import memory

        memory.seed_demo(tenant)
    except ImportError:
        pass
