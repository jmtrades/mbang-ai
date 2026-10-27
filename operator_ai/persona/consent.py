"""Persona identity and consent.

The owner designates the subject (a user with their own owner login). Only that user, signed in as themselves and
after re-entering their password, can sign consent: a statement in their own words, explicit scopes and an expiry,
bound to one profile version hash. Consents are append-only (database triggers); the only change a row ever gets is
its withdrawal. Withdrawal by the subject or the owner, and expiry, take effect in the same request: the consent is
closed, styled AI replays are purged and the caches are cleared in one transaction.

`active(tenant)` is the single gate every styled surface reads. It returns None unless the subject is designated,
their latest consent is in-app signed, unwithdrawn and unexpired, and its profile hash matches the profile as it is
now (a new profile version or a changed phrase decision needs fresh consent).
"""
from __future__ import annotations

import hashlib
import json
import threading
import time

from .. import audit, auth, db

SCOPES: dict[str, str] = {
    "coach_answers": "Frame Student Coach answers with my approved opening and closing lines",
    "lesson_framing": "Frame Academy lesson introductions",
    "roleplay_debriefs": "Frame role-play debrief introductions",
    "student_nudges": "Use my preferred words in student nudges written from templates",
    "public_minis": "Use the same framing in public lead-magnet minis",
    "external_ai_processing": "Send short excerpts of my own words to the AI provider to guide wording",
}
# Never offered as scopes and never accepted as input: a cloned voice, a likeness, or outreach in someone's name.
NOT_OFFERED = ("voice", "likeness", "outreach")
DEFAULT_DAYS = 365
MAX_DAYS = 731
MIN_STATEMENT = 30
STATEMENT_PLACEHOLDER = "Write in your own words what you agree to, and what you don't."
THROTTLE_WINDOW = 15 * 60
THROTTLE_MAX = 8
REMIND_DAYS = 30


class PersonaError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


# --------------------------------------------------------------------------- cache
_lock = threading.Lock()
_cache: dict[str, tuple[float, object]] = {}
TTL = 2.0


def _key(tenant, name: str) -> str:
    return f"{tenant.db_path}|{name}"


def cached(tenant, name: str, fn, ttl: float = TTL):
    k = _key(tenant, name)
    hit = _cache.get(k)
    if hit and hit[0] > time.time():
        return hit[1]
    val = fn()
    with _lock:
        _cache[k] = (time.time() + ttl, val)
    return val


def cache_clear(tenant=None) -> None:
    """Same pattern as guardrails.banned_cache_clear: every write that changes what is allowed clears it."""
    with _lock:
        if tenant is None:
            _cache.clear()
            return
        prefix = f"{tenant.db_path}|"
        for k in [k for k in _cache if k.startswith(prefix)]:
            _cache.pop(k, None)
    try:
        from .. import guardrails

        guardrails.banned_cache_clear(tenant.id)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- identity
def identity(tenant) -> dict:
    raw = tenant.get("persona_identity", {}) or {}
    return {"subject_user_id": raw.get("subject_user_id"), "name": (raw.get("name") or "").strip(),
            "aliases": [a for a in (raw.get("aliases") or []) if isinstance(a, str) and a.strip()],
            "designated_by": raw.get("designated_by"), "designated_at": raw.get("designated_at")}


def names(tenant) -> list[str]:
    """The configured persona name and aliases (longest first). Empty when no persona is configured."""
    def _f():
        ident = identity(tenant)
        out = []
        for n in [ident["name"], *ident["aliases"]]:
            n = " ".join((n or "").replace("(DEMO)", "").split())
            if len(n) >= 2 and n.lower() not in {x.lower() for x in out}:
                out.append(n)
        return sorted(out, key=len, reverse=True)

    return cached(tenant, "names", _f)


LOCKED = ("The name and other names can't change while consent is active: they decide whose words count as the "
          "subject's. Withdraw first; the subject then signs again.")


def _same_aliases(a: list[str], b: list[str]) -> bool:
    return sorted(x.lower() for x in a) == sorted(x.lower() for x in b)


def _clean_aliases(aliases) -> list[str]:
    out: list[str] = []
    for a in aliases or []:
        a = " ".join(str(a).split())[:60]
        if len(a) >= 2 and a.lower() not in {x.lower() for x in out}:
            out.append(a)
    return out[:12]


def owner_users(tenant) -> list[dict]:
    with tenant.db() as c:
        return db.rows(c.execute("SELECT id, username, display FROM users WHERE role='owner' AND active=1 ORDER BY id"))


def _user(tenant, uid: int | None) -> dict | None:
    if not uid:
        return None
    with tenant.db() as c:
        return db.row(c.execute("SELECT id, username, display, role, active, pw_hash FROM users WHERE id=?", (uid,)).fetchone())


def designate_subject(tenant, user_id: int, actor: str, name: str | None = None, aliases=None) -> dict:
    """Owner only (checked by the route). The subject needs their own active owner login: only they can sign."""
    u = _user(tenant, user_id)
    if u is None or not u["active"] or u["role"] != "owner":
        raise PersonaError(422, "The persona subject needs their own active owner login, so only they can sign consent.")
    cur = identity(tenant)
    same = cur["subject_user_id"] == user_id
    if name and name.strip():
        nm = " ".join(name.split())[:80]
    elif same and cur["name"]:
        nm = cur["name"]
    else:
        nm = u["display"] or u["username"]
    if len(nm) < 2:
        raise PersonaError(422, "The name needs at least two characters.")
    new_aliases = _clean_aliases(aliases if aliases is not None else cur["aliases"])
    if same and (nm != cur["name"] or not _same_aliases(new_aliases, cur["aliases"])) and active(tenant) is not None:
        raise PersonaError(409, LOCKED)
    if cur["subject_user_id"] and not same:
        withdraw(tenant, actor, reason="subject changed")
    tenant.set("persona_identity", {"subject_user_id": user_id, "name": nm, "aliases": new_aliases,
                                    "designated_by": actor, "designated_at": time.time()})
    cache_clear(tenant)
    audit.log(tenant, actor, "persona.subject", {"user_id": user_id})
    return identity(tenant)


def set_identity(tenant, actor: str, name: str | None = None, aliases=None) -> dict:
    cur = identity(tenant)
    if not cur["subject_user_id"]:
        raise PersonaError(409, "Choose the subject first.")
    new_name = cur["name"]
    if name is not None:
        new_name = " ".join(name.split())[:80]
        if len(new_name) < 2:
            raise PersonaError(422, "The name needs at least two characters.")
    new_aliases = _clean_aliases(aliases) if aliases is not None else cur["aliases"]
    # the name and the other names decide whose lines count as the subject's (boost, excerpts, guards): the subject
    # consented to them as they were, so they can't change under an active consent
    if (new_name != cur["name"] or not _same_aliases(new_aliases, cur["aliases"])) and active(tenant) is not None:
        raise PersonaError(409, LOCKED)
    new = {**cur, "name": new_name, "aliases": new_aliases}
    tenant.set("persona_identity", {k: new[k] for k in ("subject_user_id", "name", "aliases", "designated_by",
                                                          "designated_at")})
    cache_clear(tenant)
    audit.log(tenant, actor, "persona.identity", {"name_changed": new_name != cur["name"], "aliases": len(new["aliases"])})
    return identity(tenant)


def legacy_consent(tenant) -> dict | None:
    """The old owner-typed free-text consent. Never valid: it carries no proof it came from the person."""
    p = tenant.get("persona", {}) or {}
    return p.get("persona_consent") or None


# --------------------------------------------------------------------------- profiles + hashes
def profile_row(tenant, profile_id: int | None = None, version: int | None = None) -> dict | None:
    with tenant.db() as c:
        if profile_id is not None:
            r = c.execute("SELECT * FROM persona_profiles WHERE id=?", (profile_id,)).fetchone()
        elif version is not None:
            r = c.execute("SELECT * FROM persona_profiles WHERE version=? ORDER BY id DESC LIMIT 1", (version,)).fetchone()
        else:
            r = c.execute("SELECT * FROM persona_profiles WHERE status IN ('draft','stable') AND version IS NOT NULL "
                          "ORDER BY version DESC LIMIT 1").fetchone()
    r = db.row(r)
    if r:
        r["profile"] = db.jload(r["profile"], {})
    return r


def latest_profile(tenant) -> dict | None:
    return profile_row(tenant)


def phrases(tenant, profile_id: int, decision: str | None = None) -> list[dict]:
    q, args = "SELECT * FROM persona_phrases WHERE profile_id=?", [profile_id]
    if decision:
        q += " AND decision=?"
        args.append(decision)
    q += " ORDER BY CASE cls WHEN 'opener' THEN 0 WHEN 'closer' THEN 1 WHEN 'tag' THEN 2 WHEN 'transition' THEN 3 ELSE 4 END, z DESC"
    with tenant.db() as c:
        rows = db.rows(c.execute(q, args))
    for r in rows:
        r["evidence"] = db.jload(r["evidence"], [])
        r["positions"] = db.jload(r["positions"], {})
    return rows


def never_use(tenant) -> list[str]:
    with tenant.db() as c:
        return sorted({r["phrase"] for r in c.execute("SELECT phrase FROM persona_phrases WHERE decision='never_use'")})


def compute_hash(tenant, prof: dict) -> str:
    """What the subject consents to: this profile version as mined, their phrase decisions, and whose words they are
    (subject, name and other names)."""
    rows = phrases(tenant, prof["id"])
    ident = identity(tenant)
    body = {"version": prof.get("version"), "profile": prof.get("profile") or {},
            "identity": {"subject": ident["subject_user_id"], "name": ident["name"],
                         "aliases": sorted(a.lower() for a in ident["aliases"])},
            "approved": sorted((r["cls"], r["phrase"]) for r in rows if r["decision"] == "approve"),
            "never": sorted(r["phrase"] for r in rows if r["decision"] == "never_use")}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def refresh_hash(tenant, profile_id: int) -> str:
    prof = profile_row(tenant, profile_id=profile_id)
    if prof is None:
        return ""
    h = compute_hash(tenant, prof)
    with tenant.db() as c:
        c.execute("UPDATE persona_profiles SET profile_hash=?, updated=? WHERE id=?", (h, db.now(), profile_id))
    return h


DECISIONS = ("approve", "reject", "never_use", "pending")


def decide_phrase(tenant, phrase_id: int, decision: str, user: dict, actor: str) -> dict:
    if decision not in DECISIONS:
        raise PersonaError(422, "Decision must be approve, reject, never_use or pending.")
    ident = identity(tenant)
    is_subject = ident["subject_user_id"] == user["id"]
    if decision in ("approve", "pending") and not is_subject:
        raise PersonaError(403, "Only the subject can approve their own phrases.")
    if not is_subject and user["role"] != "owner":
        raise PersonaError(403, "Owner or subject only.")
    with tenant.db() as c:
        r = db.row(c.execute("SELECT * FROM persona_phrases WHERE id=?", (phrase_id,)).fetchone())
        if r is None:
            raise PersonaError(404, "Not found")
        c.execute("UPDATE persona_phrases SET decision=?, decided_by=?, decided_at=? WHERE id=?",
                  (decision, actor, db.now(), phrase_id))
    refresh_hash(tenant, r["profile_id"])
    cache_clear(tenant)
    audit.log(tenant, actor, "persona.approve", {"phrase_id": phrase_id, "decision": decision, "version": r["version"]})
    return {"ok": True, "id": phrase_id, "decision": decision}


# --------------------------------------------------------------------------- consent
def consents(tenant, subject_user_id: int | None = None, limit: int = 50) -> list[dict]:
    q, args = "SELECT * FROM persona_consents", []
    if subject_user_id is not None:
        q += " WHERE subject_user_id=?"
        args.append(subject_user_id)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with tenant.db() as c:
        rows = db.rows(c.execute(q, args))
    for r in rows:
        r["scopes"] = db.jload(r["scopes"], [])
    return rows


def _throttled(tenant, user_id: int) -> bool:
    with tenant.db() as c:
        n = c.execute("SELECT COUNT(*) FROM login_fails WHERE key=? AND at>?",
                      (f"persona:{user_id}", time.time() - THROTTLE_WINDOW)).fetchone()[0]
    return n >= THROTTLE_MAX


def sign_consent(tenant, user: dict, statement: str, scopes: list[str], profile_version: int, password: str,
                 expires: float | None, actor: str, *, _verified: bool = False, demo: bool = False) -> dict:
    ident = identity(tenant)
    if not ident["subject_user_id"] or ident["subject_user_id"] != user["id"]:
        raise PersonaError(403, "Only the designated subject can sign their own persona consent.")
    me = _user(tenant, user["id"])
    if me is None or not me["active"] or me["role"] != "owner":
        raise PersonaError(403, "The persona subject signs on their own active owner login.")
    if not _verified:
        if _throttled(tenant, user["id"]):
            raise PersonaError(429, "Too many wrong passwords. Try again in 15 minutes.")
        u = _user(tenant, user["id"])
        if u is None or not u["active"] or not auth.check_pw(password or "", u["pw_hash"]):
            with tenant.db() as c:
                c.execute("INSERT INTO login_fails(key,at) VALUES(?,?)", (f"persona:{user['id']}", time.time()))
            audit.log(tenant, actor, "persona.consent_fail", {"reason": "password"})
            raise PersonaError(403, "That password didn't match. Consent was not recorded.")
    text = " ".join((statement or "").split())
    if len(text) < MIN_STATEMENT or text.lower() == STATEMENT_PLACEHOLDER.lower():
        raise PersonaError(422, f"Write the statement in your own words (at least {MIN_STATEMENT} characters).")
    if len(text) > 2000:
        raise PersonaError(422, "Keep the statement under 2,000 characters.")
    sc = list(dict.fromkeys(scopes or []))
    bad = [s for s in sc if s not in SCOPES]
    if bad:
        raise PersonaError(422, "These can't be consented to here: " + ", ".join(sorted(bad)[:5]))
    if not sc:
        raise PersonaError(422, "Choose at least one scope.")
    prof = latest_profile(tenant)
    if prof is None:
        raise PersonaError(409, "There is no style profile to consent to yet.")
    if int(profile_version) != int(prof["version"]):
        raise PersonaError(409, f"Profile v{prof['version']} is the current version. Review it, then sign for it.")
    now = time.time()
    exp = float(expires) if expires else now + DEFAULT_DAYS * 86400
    if exp < now + 86400 or exp > now + MAX_DAYS * 86400:
        raise PersonaError(422, "Choose an expiry between tomorrow and two years from now.")
    h = refresh_hash(tenant, prof["id"])
    with tenant.db() as c, db.tx(c):
        c.execute("UPDATE persona_consents SET withdrawn_at=?, withdrawn_by=? WHERE subject_user_id=? AND withdrawn_at IS NULL",
                  (now, "superseded", user["id"]))
        cid = c.execute(
            "INSERT INTO persona_consents(subject_user_id,profile_id,profile_version,profile_hash,statement,statement_sha,"
            "scopes,method,at,expires,withdrawn_at,withdrawn_by,witnessed_by,demo) VALUES(?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,?)",
            (user["id"], prof["id"], prof["version"], h, text, hashlib.sha256(text.encode()).hexdigest(), db.jdump(sc),
             "in_app_signed", now, exp, 1 if demo or tenant.is_demo else 0)).lastrowid
    cache_clear(tenant)
    audit.log(tenant, actor, "persona.consent", {"consent_id": cid, "version": prof["version"], "scopes": sc,
                                                 "expires": time.strftime("%Y-%m-%d", time.localtime(exp))})
    return consents(tenant, user["id"], 1)[0]


def _purge(c) -> int:
    """Styled replays only ('coach-q:p<version>'). Neutral 'coach-q' replays are never touched: they are never written
    from persona-ranked retrieval (the +10% boost only applies to extractive answers, with the AI off)."""
    return c.execute("DELETE FROM ai_replays WHERE purpose LIKE 'coach-q:p%'").rowcount


def withdraw(tenant, actor: str, delete_profile: bool = False, reason: str = "") -> dict:
    """Takes effect in this request: consent closed, styled replays purged, profile optionally deleted (the consent
    rows and the audit log stay), caches cleared."""
    now = time.time()
    with tenant.db() as c, db.tx(c):
        n = c.execute("UPDATE persona_consents SET withdrawn_at=?, withdrawn_by=? WHERE withdrawn_at IS NULL",
                      (now, actor[:120])).rowcount
        purged = _purge(c)
        deleted = 0
        if delete_profile:
            c.execute("DELETE FROM persona_phrases")
            c.execute("DELETE FROM persona_uses")
            deleted = c.execute("UPDATE persona_profiles SET status='deleted', profile=NULL WHERE status!='deleted'").rowcount
    cache_clear(tenant)
    audit.log(tenant, actor, "persona.withdraw", {"withdrawn": n, "replays_purged": purged, "profile_deleted": deleted,
                                                  "reason": (reason or "")[:200]})
    return {"ok": True, "withdrawn": n, "replays_purged": purged, "profiles_deleted": deleted}


def _expire(tenant, consent_id: int, expires_at: float) -> None:
    with tenant.db() as c, db.tx(c):
        n = c.execute("UPDATE persona_consents SET withdrawn_at=?, withdrawn_by='expired' WHERE id=? AND withdrawn_at IS NULL",
                      (expires_at, consent_id)).rowcount
        purged = _purge(c) if n else 0
    cache_clear(tenant)
    if n:
        audit.log(tenant, "system", "persona.expired", {"consent_id": consent_id, "replays_purged": purged})


def _compute_active(tenant) -> dict | None:
    ident = identity(tenant)
    sid = ident["subject_user_id"]
    if not sid or not ident["name"]:
        return None
    u = _user(tenant, sid)
    if u is None or not u["active"] or u["role"] != "owner":
        return None
    with tenant.db() as c:
        r = db.row(c.execute("SELECT * FROM persona_consents WHERE subject_user_id=? AND withdrawn_at IS NULL "
                             "ORDER BY id DESC LIMIT 1", (sid,)).fetchone())
    if r is None or r["method"] != "in_app_signed":
        return None
    if r["expires"] <= time.time():
        _expire(tenant, r["id"], r["expires"])
        return None
    prof = profile_row(tenant, profile_id=r["profile_id"])
    if prof is None or prof["status"] not in ("draft", "stable") or compute_hash(tenant, prof) != r["profile_hash"]:
        return None
    rows = phrases(tenant, prof["id"], "approve")
    return {"version": prof["version"], "profile_id": prof["id"], "consent_id": r["id"], "name": ident["name"],
            "aliases": ident["aliases"], "scopes": set(db.jload(r["scopes"], [])), "at": r["at"], "expires": r["expires"],
            "subject_user_id": sid, "profile": prof["profile"] or {},
            "approved": [{"id": x["id"], "phrase": x["phrase"], "display": x["display"] or x["phrase"], "cls": x["cls"],
                          "n": x["n"]} for x in rows],
            "never": never_use(tenant)}


def active(tenant) -> dict | None:
    """The one gate: None unless a valid, subject-signed, unexpired consent matches the current profile."""
    a = cached(tenant, "active", lambda: _compute_active(tenant))
    if a is not None and a["expires"] <= time.time():
        cache_clear(tenant)
        a = _compute_active(tenant)
    return a


# --------------------------------------------------------------------------- per-user preference
def neutral_voice(tenant, user_id: int) -> bool:
    def _f():
        with tenant.db() as c:
            r = c.execute("SELECT neutral_voice FROM persona_prefs WHERE user_id=?", (user_id,)).fetchone()
        return bool(r and r["neutral_voice"])

    return cached(tenant, f"neutral:{user_id}", _f)


def set_neutral_voice(tenant, user_id: int, value: bool) -> bool:
    with tenant.db() as c:
        c.execute("INSERT INTO persona_prefs(user_id,neutral_voice,updated) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE "
                  "SET neutral_voice=excluded.neutral_voice, updated=excluded.updated", (user_id, 1 if value else 0, db.now()))
    with _lock:
        _cache.pop(_key(tenant, f"neutral:{user_id}"), None)
    return bool(value)


# --------------------------------------------------------------------------- status
def status(tenant) -> dict:
    """Neutral, Draft vN, Awaiting subject, Active vN, Withdrawn or Expired."""
    a = active(tenant)
    ident = identity(tenant)
    prof = latest_profile(tenant)
    if a:
        return {"key": "active", "label": f"Active v{a['version']}",
                "detail": f"Styling is on under consent v{a['version']} until {time.strftime('%d %b %Y', time.localtime(a['expires']))}."}
    if not ident["subject_user_id"]:
        return {"key": "neutral", "label": "Neutral", "detail": "No subject chosen. Every answer is in a neutral voice."}
    last = consents(tenant, ident["subject_user_id"], 1)
    last = last[0] if last else None
    if last and last["withdrawn_at"] and (prof is None or prof["created"] <= last["at"] or prof["version"] == last["profile_version"]):
        if last["withdrawn_by"] == "expired":
            return {"key": "expired", "label": "Expired", "detail": "Consent expired. Answers are neutral until the subject signs again."}
        return {"key": "withdrawn", "label": "Withdrawn", "detail": "Consent was withdrawn. Answers are neutral."}
    if prof is None:
        return {"key": "neutral", "label": "Neutral", "detail": "No style profile yet. Load attributed material, then build one."}
    pending = len(phrases(tenant, prof["id"], "pending"))
    if pending:
        return {"key": "draft", "label": f"Draft v{prof['version']}",
                "detail": f"{pending} phrases are waiting for the subject's review."}
    return {"key": "awaiting_subject", "label": "Awaiting subject",
            "detail": f"Profile v{prof['version']} is reviewed. Styling starts only when the subject signs consent."}
