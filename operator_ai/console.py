"""Owner console pieces: KB admin (licence, provenance, priority, notes), personality,
members + signed mock payment webhooks, lead-magnet minis + embed, Drive inbox sync,
integrations mapped to the vault, engagement alerts."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from pathlib import Path

from . import audit, auth, config, content_policy, db, guardrails, index, vault

# --------------------------------------------------------------------------- KB admin
LICENCES = ("course", "own", "third_party_public", "unknown")
PROVENANCE = ("owner_confirmed", "source_unconfirmed")


def kb_files(tenant) -> list[dict]:
    idx = index.for_tenant(tenant)
    with tenant.db() as c:
        notes = {r["file"]: dict(r) for r in c.execute("SELECT * FROM kb_notes")}
    out = []
    for f, d in sorted(idx.docs.items()):
        n = notes.get(f, {})
        out.append({"file": f, "title": guardrails.scrub(d.title, guardrails.tenant_banned_names(tenant)), "kind": d.kind,
                    "quality": d.quality, "licence": n.get("licence") or d.licence_status,
                    "provenance": n.get("provenance") or d.provenance, "priority": n.get("priority") or 1.0,
                    "note": n.get("note") or "", "chunks": sum(1 for c in idx.chunks if c.file == f),
                    "excluded": [e.to_dict() for e in idx.excluded if e.file == f]})
    return out


def kb_update(tenant, file: str, actor: str, licence: str | None = None, provenance: str | None = None,
              priority: float | None = None, note: str | None = None) -> dict:
    if file not in index.for_tenant(tenant).docs:
        raise KeyError(file)
    if licence and licence not in LICENCES:
        raise ValueError("licence")
    if provenance and provenance not in PROVENANCE:
        raise ValueError("provenance")
    if priority is not None and not (0.1 <= priority <= 3.0):
        raise ValueError("priority")
    with tenant.db() as c:
        cur = c.execute("SELECT * FROM kb_notes WHERE file=?", (file,)).fetchone()
        if cur is None:
            c.execute("INSERT INTO kb_notes(file,priority,note,provenance,licence,updated) VALUES(?,?,?,?,?,?)",
                      (file, priority or 1.0, note or "", provenance, licence, db.now()))
        else:
            c.execute("UPDATE kb_notes SET priority=COALESCE(?,priority), note=COALESCE(?,note), "
                      "provenance=COALESCE(?,provenance), licence=COALESCE(?,licence), updated=? WHERE file=?",
                      (priority, note, provenance, licence, db.now(), file))
    audit.log(tenant, actor, "kb.admin", {"file": file, "licence": licence, "provenance": provenance, "priority": priority})
    return {"ok": True}


def provenance_panel(tenant) -> dict:
    """B13: where every answer's material comes from."""
    files = kb_files(tenant)
    by = {}
    for f in files:
        by.setdefault(f["provenance"], 0)
        by[f["provenance"]] += 1
    return {"files": files, "by_provenance": by,
            "unconfirmed_note": "Methods from material with unconfirmed authorship get neutral names, never a person's name."}


# --------------------------------------------------------------------------- personality
def _persona(tenant) -> dict:
    """The stored persona, whatever was stored before settings were validated (a non-object reads as empty)."""
    p = tenant.get("persona", {})
    return p if isinstance(p, dict) else {}


def personality(tenant) -> dict:
    p = _persona(tenant)
    limit = p.get("daily_question_limit", 50)
    if isinstance(limit, bool) or not isinstance(limit, (int, float)) or not 1 <= limit <= 1000:
        limit = 50
    style = p.get("style", "")
    return {"display_name": p.get("display_name") if isinstance(p.get("display_name"), str) and p.get("display_name")
            else "Coach", "daily_question_limit": int(limit),
            "banned_words": [w for w in p.get("banned_words", []) if isinstance(w, str)]
            if isinstance(p.get("banned_words"), list) else [],
            "style": style if isinstance(style, str) else "",
            "style_note": "Style rewriting needs AI (NEEDS_AI_BUDGET); banned words are always enforced.",
            "persona_consent": p.get("persona_consent") if isinstance(p.get("persona_consent"), dict) else None}


def set_personality(tenant, data: dict, actor: str) -> dict:
    cur = personality(tenant)
    name = (data.get("display_name") or cur.get("display_name") or "Coach").strip()[:60]
    consent = cur.get("persona_consent")
    if data.get("persona_consent_text"):
        consent = {"text": str(data["persona_consent_text"])[:500], "at": time.time(), "by": actor}
    new = {"display_name": name, "daily_question_limit": int(data.get("daily_question_limit") or cur.get("daily_question_limit", 50)),
           "banned_words": [w.strip() for w in (data.get("banned_words") or cur.get("banned_words", [])) if str(w).strip()][:100],
           "style": str(data.get("style", cur.get("style", "")))[:500], "persona_consent": consent}
    tenant.set("persona", new)
    audit.log(tenant, actor, "settings", {"persona": "updated", "consent": bool(consent)})
    return personality(tenant)


# --------------------------------------------------------------------------- members + payments
PAYMENT_SECRET = "payments_webhook_secret"


def sign_payment(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class WebhookError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


def payment_webhook(tenant, sig: str | None, body: bytes) -> dict:
    """Signed mock payment webhook -> member access. Idempotent by event id."""
    if len(body) > 256 * 1024:
        raise WebhookError(413, "Body too large")
    secret = vault.ensure_practice_secret(tenant, PAYMENT_SECRET)
    if not sig or not hmac.compare_digest(sig, sign_payment(secret, body)):
        raise WebhookError(401, "Bad signature")
    try:
        ev = json.loads(body)
        eid, kind, email = str(ev["id"]), str(ev["type"]), str(ev["data"]["email"]).lower()
    except (ValueError, KeyError, TypeError):
        raise WebhookError(422, "Malformed event")
    with tenant.db() as c:
        try:
            c.execute("INSERT INTO payment_events(event_id,kind,email,data,created) VALUES(?,?,?,?,?)",
                      (eid, kind, email, body.decode()[:4000], db.now()))
        except Exception:  # noqa: BLE001 - UNIQUE(event_id): replay
            return {"ok": True, "duplicate": True}
    status = {"payment.succeeded": "active", "subscription.renewed": "active", "payment.failed": "past_due",
              "subscription.cancelled": "cancelled", "payment.refunded": "cancelled"}.get(kind)
    if status is None:
        return {"ok": True, "ignored": kind}
    with tenant.db() as c:
        c.execute("INSERT INTO members(email,status,plan,updated,demo) VALUES(?,?,?,?,?) ON CONFLICT(email) DO UPDATE SET "
                  "status=excluded.status, updated=excluded.updated",
                  (email, status, ev["data"].get("plan", "core"), db.now(), 1 if tenant.is_demo else 0))
        u = c.execute("SELECT id FROM users WHERE username=?", (email,)).fetchone()
    invite = None
    if status == "active" and u is None:
        pw = secrets.token_urlsafe(12)
        auth.create_user(tenant, email, pw, "student", display=email.split("@")[0])
        invite = "member account created (invite sent manually by the owner; password never shown)"
    elif u is not None:
        with tenant.db() as c:
            c.execute("UPDATE users SET active=? WHERE id=?", (1 if status == "active" else 0, u["id"]))
            if status != "active":
                c.execute("DELETE FROM sessions WHERE user_id=?", (u["id"],))
    audit.log(tenant, "payments-webhook", "payment", {"event": eid, "kind": kind, "status": status})
    return {"ok": True, "member_status": status, "access": status == "active", "invite": invite}


def members(tenant) -> list[dict]:
    with tenant.db() as c:
        return db.rows(c.execute("SELECT * FROM members ORDER BY updated DESC"))


# --------------------------------------------------------------------------- minis + embed
def create_mini(tenant, slug: str, title: str, scope: list[str], question_limit: int, consent_text: str) -> int:
    if not re.match(r"^[a-z0-9-]{2,40}$", slug):
        raise ValueError("slug")
    with tenant.db() as c:
        return c.execute("INSERT INTO minis(slug,title,scope,question_limit,consent_text,created) VALUES(?,?,?,?,?,?)",
                         (slug, title[:120], db.jdump(scope), max(1, min(50, question_limit)), consent_text[:500],
                          db.now())).lastrowid


def get_mini(tenant, slug: str) -> dict | None:
    with tenant.db() as c:
        r = db.row(c.execute("SELECT * FROM minis WHERE slug=?", (slug,)).fetchone())
    if r:
        r["scope"] = db.jload(r["scope"], [])
    return r


def list_minis(tenant) -> list[dict]:
    with tenant.db() as c:
        rows = db.rows(c.execute("SELECT * FROM minis ORDER BY id DESC"))
    for r in rows:
        r["scope"] = db.jload(r["scope"], [])
        with tenant.db() as c:
            r["sessions"] = c.execute("SELECT COUNT(*) FROM mini_sessions WHERE mini_id=?", (r["id"],)).fetchone()[0]
    return rows


# Public minis are reachable by anyone: limits per network (in memory) and per email address (in the DB, so a
# throwaway session token doesn't reset the question limit).
MINI_SESSIONS_PER_EMAIL_DAY = 3
MINI_STARTS_PER_IP_HOUR = 10
MINI_ASKS_PER_IP_HOUR = 60
OUT_OF_SCOPE = "That's outside what this assistant covers."


def _mini_email_rows(tenant, mini_id: int, email: str) -> list[dict]:
    from .queue import canonical_email

    want = canonical_email(email)
    with tenant.db() as c:
        rows = db.rows(c.execute("SELECT id, email, questions FROM mini_sessions WHERE mini_id=? AND created>?",
                                 (mini_id, db.now() - 86400)))
    return [r for r in rows if canonical_email(r["email"] or "") == want]


def mini_start(tenant, slug: str, email: str, consent: bool, ip: str | None = None) -> dict:
    from . import ratelimit

    m = get_mini(tenant, slug)
    if m is None:
        raise WebhookError(404, "Not found")
    if not consent:
        raise WebhookError(422, "Consent is required to use this assistant")
    email = (email or "").strip()
    if len(email) > 200 or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise WebhookError(422, "A valid email is required")
    try:
        ratelimit.hit("mini-start", ip or "?", ratelimit.limit("mini_start", MINI_STARTS_PER_IP_HOUR), 3600)
    except ratelimit.Limited:
        raise WebhookError(429, "Too many sessions from this network. Try again later.")
    if len(_mini_email_rows(tenant, m["id"], email)) >= MINI_SESSIONS_PER_EMAIL_DAY:
        raise WebhookError(429, "This email has already used this assistant today. Try again tomorrow.")
    tok = secrets.token_urlsafe(16)
    with tenant.db() as c:
        c.execute("INSERT INTO mini_sessions(mini_id,token,email,consent,created) VALUES(?,?,?,?,?)",
                  (m["id"], tok, email.lower(), 1, db.now()))
    audit.log(tenant, f"mini:{slug}", "consent", {"mini": slug})
    return {"token": tok, "question_limit": m["question_limit"], "title": m["title"]}


def _scoped_answer(tenant, question: str, scope: set[str]) -> dict:
    """Answer only from passages in the mini's chosen files: retrieval is filtered BEFORE the answer is built,
    so no sentence from another file can appear. Extractive (no AI) and guardrail-checked, with a trace."""
    from . import retrieval, runtime
    from .agents import coach

    def fn(run):
        hits = run.step("retrieve", {"question": question, "scope": sorted(scope)},
                        lambda: [h for h in retrieval.search(tenant, question, k=40) if h["file"] in scope][:6],
                        "Retrieval inside the mini's files")
        hits = [{**h, "id": f"S{i}"} for i, h in enumerate(hits, start=1)]
        cov = run.step("coverage", {}, lambda: retrieval.coverage(tenant, question, hits), "Coverage judge")
        if cov["covered"] == "no" or not hits:
            return {"covered": "no", "answer": OUT_OF_SCOPE, "citations": []}
        text = run.step("answer", {"mode": "extractive"},
                        lambda: coach.extractive(question, hits, max_sentences=3, idf=index.for_tenant(tenant).idf),
                        "Answer from in-scope passages only")
        lines = [ln for ln in text.splitlines() if ln.strip() and not guardrails.blocked(
            guardrails.check(ln, "plain", None, None, tenant=tenant))]
        kept = {c for ln in lines for c in coach.CITE_RE.findall(ln)}
        if not lines or not kept:
            return {"covered": "no", "answer": OUT_OF_SCOPE, "citations": []}
        names = guardrails.tenant_banned_names(tenant)
        return {"covered": cov["covered"], "answer": guardrails.scrub("From the course material:\n" + "\n".join(lines), names),
                "citations": [retrieval.cite(h) for h in hits if h["id"] in kept]}

    out = runtime.execute(tenant, "mini", f"Mini: {question[:60]}", ["Retrieval inside the mini's files", "Coverage judge",
                                                                    "Answer from in-scope passages only"], fn, "mini")
    return out["result"]


def mini_ask(tenant, slug: str, token: str, question: str, ip: str | None = None) -> dict:
    from . import ratelimit

    m = get_mini(tenant, slug)
    if m is None:
        raise WebhookError(404, "Not found")
    with tenant.db() as c:
        s = c.execute("SELECT * FROM mini_sessions WHERE token=? AND mini_id=?", (token, m["id"])).fetchone()
    if s is None or not s["consent"]:
        raise WebhookError(401, "Start a session first")
    if s["questions"] >= m["question_limit"]:
        raise WebhookError(429, f"Question limit of {m['question_limit']} reached")
    # the question limit is per email address per day, not per (free, unlimited) session token
    if sum(r["questions"] or 0 for r in _mini_email_rows(tenant, m["id"], s["email"] or "")) >= m["question_limit"]:
        raise WebhookError(429, f"Question limit of {m['question_limit']} reached for today")
    try:
        ratelimit.hit("mini-ask", ip or "?", ratelimit.limit("mini_ask", MINI_ASKS_PER_IP_HOUR), 3600)
    except ratelimit.Limited:
        raise WebhookError(429, "Too many questions from this network. Try again later.")
    from . import retrieval
    from .agents import coach

    question = question[:1000]
    allowed = {f for f in m["scope"] if isinstance(f, str)}
    if allowed:
        res = _scoped_answer(tenant, question, allowed)
        retrieval.record_query(tenant, None, question, res["covered"], "en", None)
    else:
        # an unscoped mini answers from the whole course; a public mini never adds the external 2026 Playbooks
        res = coach.answer(tenant, question, record=True, external=False)
    with tenant.db() as c:
        c.execute("UPDATE mini_sessions SET questions=questions+1 WHERE id=?", (s["id"],))
    return {"covered": res["covered"], "answer": res["answer"], "citations": res["citations"] if res["covered"] != "no" else [],
            "remaining": m["question_limit"] - s["questions"] - 1}


def embed_snippet(tid: str, slug: str) -> str:
    return (f'<script src="/embed.js" data-tenant="{tid}" data-mini="{slug}" async></script>\n'
            f'<!-- or: <iframe src="/m/{tid}/{slug}" style="width:380px;height:560px;border:0"></iframe> -->')


# --------------------------------------------------------------------------- Drive inbox sync
def drive_inbox(tenant) -> Path:
    """The tenant's inbox folder. On the server it can point at a Google Drive for desktop (or any
    synced) folder via OPERATOR_DRIVE_INBOX_<TENANT_ID> — server-only, never settable from the API."""
    import os

    env = os.environ.get(f"OPERATOR_DRIVE_INBOX_{tenant.id.upper().replace('-', '_')}")
    p = Path(env) if env else tenant.dir / "drive_inbox"
    p.mkdir(parents=True, exist_ok=True)
    return p


def drive_auto_sync(tenant, now: float | None = None) -> int:
    """Daily: fetch and screen new or changed files (they stay staged for the owner's OK)."""
    now = now or time.time()
    last = float(tenant.get("drive_last_sync", 0) or 0)
    if now - last < 86400:
        return 0
    tenant.set("drive_last_sync", now)
    return len([s for s in drive_fetch_and_stage(tenant, "scheduler") if s["status"] == "staged"])


def drive_list(tenant) -> dict:
    """list -> diff: what's new or changed since it was last staged."""
    with tenant.db() as c:
        known = {r["filename"]: r["sha"] for r in c.execute("SELECT filename, sha FROM drive_staged")}
    files = []
    for p in sorted(drive_inbox(tenant).iterdir()):
        if not p.is_file() or p.name.startswith("."):
            continue
        sha = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
        state = "new" if p.name not in known else "changed" if known[p.name] != sha else "unchanged"
        files.append({"filename": p.name, "size": p.stat().st_size, "sha": sha, "state": state})
    inbox = drive_inbox(tenant)
    try:
        shown = str(inbox.relative_to(config.get().data_dir))
    except ValueError:
        shown = "server-configured synced folder"
    return {"inbox": shown, "files": files, "last_sync": tenant.get("drive_last_sync")}


def drive_fetch_and_stage(tenant, actor: str) -> list[dict]:
    """fetch -> guardrail / content-policy screen -> stage for the owner's OK."""
    from . import uploads

    staged = []
    for f in drive_list(tenant)["files"]:
        if f["state"] == "unchanged":
            continue
        data = (drive_inbox(tenant) / f["filename"]).read_bytes()
        try:
            text = uploads.extract(f["filename"], data)["text"]
        except uploads.UploadError as e:
            staged.append({"filename": f["filename"], "status": "rejected", "reason": str(e)})
            continue
        flags = []
        for i, line in enumerate(text.splitlines(), start=1):
            t = content_policy.classify(line)
            if t:
                flags.append({"line": i, "label": content_policy.not_used_label(t)})
        with tenant.db() as c:
            c.execute("DELETE FROM drive_staged WHERE filename=?", (f["filename"],))
            c.execute("INSERT INTO drive_staged(filename,sha,status,flags,text,created) VALUES(?,?,?,?,?,?)",
                      (f["filename"], f["sha"], "staged", db.jdump(flags), text, db.now()))
        staged.append({"filename": f["filename"], "status": "staged", "flags": flags})
    audit.log(tenant, actor, "drive.stage", {"staged": len(staged)})
    return staged


def drive_staged(tenant) -> list[dict]:
    with tenant.db() as c:
        rows = db.rows(c.execute("SELECT id,filename,sha,status,flags,created FROM drive_staged ORDER BY id DESC"))
    for r in rows:
        r["flags"] = db.jload(r["flags"], [])
    return rows


def drive_decide(tenant, staged_id: int, approve: bool, actor: str) -> dict:
    """The owner's OK adds the file to the knowledge base (never to inputs/, which is read-only)."""
    with tenant.db() as c:
        r = c.execute("SELECT * FROM drive_staged WHERE id=?", (staged_id,)).fetchone()
        if r is None:
            raise KeyError(staged_id)
        c.execute("UPDATE drive_staged SET status=? WHERE id=?", ("approved" if approve else "rejected", staged_id))
    if approve:
        tenant.kb_dir.mkdir(parents=True, exist_ok=True)
        (tenant.kb_dir / f"{Path(r['filename']).stem}.txt").write_text(r["text"])
        index.invalidate()
        from . import skills

        skills.on_source_change(tenant, f"drive:{r['filename']}")
    audit.log(tenant, actor, "drive.stage", {"id": staged_id, "approved": approve})
    return {"ok": True, "approved": approve}


# --------------------------------------------------------------------------- integrations (vault-mapped)
INTEGRATIONS = [
    {"key": "meta", "name": "Meta (ads + lead forms)", "fields": ["meta_app_id", "meta_app_secret", "meta_access_token",
                                                                   "meta_verify_token"], "live_switch": "live_meta_enabled"},
    {"key": "google_calendar", "name": "Google Calendar", "fields": ["google_client_id", "google_client_secret",
                                                                     "google_refresh_token", "google_calendar_id"],
     "live_switch": "live_calendar_enabled"},
    {"key": "instagram", "name": "Instagram messaging", "fields": ["instagram_access_token"]},
    {"key": "telegram", "name": "Telegram bot", "fields": ["telegram_bot_token", "telegram_webhook_secret"],
     "live_switch": "live_channels_enabled"},
    {"key": "slack", "name": "Slack app", "fields": ["slack_bot_token", "slack_signing_secret"], "live_switch": "live_channels_enabled"},
    {"key": "sms", "name": "SMS / phone provider (Twilio-style)", "fields": ["sms_account_sid", "sms_auth_token", "sms_from_number",
                                                                            "sms_webhook_secret"], "live_switch": "live_channels_enabled"},
    {"key": "email", "name": "Email (SMTP + inbound)", "fields": ["smtp_host", "smtp_port", "smtp_user", "smtp_password", "smtp_from",
                                                                  "email_webhook_secret"], "live_switch": "live_channels_enabled"},
    {"key": "payments", "name": "Payments", "fields": ["payments_api_key", "payments_webhook_secret"]},
    {"key": "crm", "name": "GoHighLevel-style CRM", "fields": ["crm_api_key", "crm_location_id"]},
    {"key": "drive", "name": "Google Drive (inbox sync)", "fields": ["drive_folder_id"]},
    {"key": "llm", "name": "AI model", "fields": [], "env": ["ANTHROPIC_API_KEY or OPENAI_API_KEY", "OPERATOR_AI_TOKEN_BUDGET"]},
]


def integrations(tenant) -> list[dict]:
    out = []
    for it in INTEGRATIONS:
        st = vault.status(tenant, it["fields"]) if it["fields"] else {}
        ready = all(v == "set" for v in st.values()) if st else bool(config.llm_key())
        row = {**it, "fields": st, "connected": ready,
               "status": "REAL_OK" if ready else "NEEDS_LOGIN" if it["key"] != "llm" else "NEEDS_AI_BUDGET"}
        if it.get("live_switch"):
            row["live"] = bool(tenant.get(it["live_switch"], False))
        out.append(row)
    return out


# --------------------------------------------------------------------------- engagement alerts
def engagement_alerts(tenant) -> list[dict]:
    alerts = []
    now = time.time()
    with tenant.db() as c:
        unanswered = c.execute("SELECT COUNT(*) FROM queries WHERE covered='no' AND created>?", (now - 7 * 86400,)).fetchone()[0]
        total_q = c.execute("SELECT COUNT(*) FROM queries WHERE created>?", (now - 7 * 86400,)).fetchone()[0]
        quiet = c.execute("SELECT COUNT(*) FROM students s WHERE NOT EXISTS (SELECT 1 FROM checkins k WHERE k.student_id=s.id "
                          "AND k.at>?)", (now - 14 * 86400,)).fetchone()[0]
        held = c.execute("SELECT COUNT(*) FROM queue WHERE status='held'").fetchone()[0]
        old_pending = c.execute("SELECT COUNT(*) FROM queue WHERE status='pending' AND created<?", (now - 2 * 86400,)).fetchone()[0]
    if total_q and unanswered / total_q > 0.2:
        alerts.append({"level": "warn", "text": f"{unanswered} of {total_q} student questions this week weren't covered by "
                                                "the material. Consider adding a note or lesson.", "href": "#/console?tab=queries"})
    if quiet:
        alerts.append({"level": "warn", "text": f"{quiet} students haven't checked in for 14 days.", "href": "#/students"})
    if held:
        alerts.append({"level": "info", "text": f"{held} drafts are held by guardrails and need editing.", "href": "#/queue?status=held"})
    if old_pending:
        alerts.append({"level": "info", "text": f"{old_pending} drafts have waited more than 2 days for approval.",
                       "href": "#/queue"})
    return alerts
