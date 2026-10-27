"""Client operations after the win.

On `client_won` a client moves to onboarding: an access checklist (intake form, Meta Business Manager partner
access, pixel / Conversions API, CRM, calendar, domain, kickoff call — ticked by hand unless a live switch can
verify it), a public intake link for the client, a kickoff invite drafted for approval, and a fulfilment run written
from the client's own brief.

Then a weekly loop (`tick`, on the client's chosen weekday): numbers for the week (an Ads Manager CSV, the manual
form, or the Meta read API when the tenant's live switch is on), the diagnostic, ad change proposals (approval
gated), a weekly client report drafted for approval, a monthly review draft and churn-risk signals that raise an
escalation. Nothing here sends anything: every outgoing item goes through queue.add and leaves only on approval.
Works with AI off (templates and rules); an AI summary line is added only when the tenant's AI budget allows it.
"""
from __future__ import annotations

import logging
import math
import os
import re
import secrets
import time

from . import audit, db, events, queue, runtime
from . import brief as brief_mod
from .redact import redact

log = logging.getLogger("operator.client_ops")

STATUSES = ("onboarding", "active", "paused", "ended")
STATUS_LABELS = {"onboarding": "Onboarding", "active": "Active", "paused": "Paused", "ended": "Ended"}
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# (key, title, what it means). Ticked by hand unless a live switch verifies it (LIVE_VERIFIABLE).
CHECKLIST = [
    ("intake_form", "Intake form submitted", "The client fills in their offer, audience, proof (with permission) and asset links."),
    ("meta_partner_access", "Meta Business Manager partner access",
     "The client adds your business as a partner on their ad account and Page."),
    ("pixel_capi", "Pixel and Conversions API", "The pixel is on every page and the Conversions API sends lead events."),
    ("crm", "CRM access", "You can see their pipeline, so booked calls and sales can be checked every week."),
    ("calendar", "Calendar", "You know which calendar their calls are booked into."),
    ("domain", "Domain and email sending", "You can point a page at their domain or verify email sending on it."),
    ("kickoff_call", "Kickoff call held", "Scope, deliverables and the first milestone read back and agreed on the call."),
]
CHECK_KEYS = [k for k, _, _ in CHECKLIST]
LIVE_VERIFIABLE = {"meta_partner_access", "pixel_capi"}

# numbers a client report can track (the diagnostic reads the same keys)
METRIC_KEYS = {
    "ad_spend": "Ad spend", "leads": "Leads", "impressions": "Impressions", "clicks": "Link clicks",
    "dms_sent": "DMs sent", "replies": "Replies", "conversations": "Quality conversations", "links_sent": "Booking links sent",
    "booked": "Calls booked", "showed": "Calls showed", "closed": "Sales closed", "revenue": "Cash collected",
    "visitors": "Page visitors", "registrants": "Webinar registrants", "attendees": "Webinar attendees",
    "webinar_sales": "Webinar sales",
}
REPORT_KPIS = ["ad_spend", "leads", "booked", "showed", "closed", "revenue", "registrants", "attendees"]
DEFAULT_TARGETS = {"leads": 40, "booked": 10, "showed": 6, "closed": 2}
ACCESS_KEYS = {"ad_account_id", "pixel_id", "crm", "calendar_url", "domain", "notes"}
WEEK = 7 * 86400
MONTH = 28 * 86400


class ClientOpsError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


# --------------------------------------------------------------------------- reads
def _parse(r: dict | None) -> dict | None:
    if r is None:
        return None
    for k, d in (("metrics", {}), ("brief", {}), ("kpi_targets", {}), ("access", {}), ("risk", None)):
        r[k] = db.jload(r.get(k), d) if isinstance(r.get(k), (str, type(None))) else r.get(k)
        if r[k] is None and d is not None:
            r[k] = d
    r["status"] = r.get("status") or "active"
    r["report_weekday"] = int(r.get("report_weekday") or 0)
    return r


def get_client(tenant, client_id: int) -> dict | None:
    with tenant.db() as c:
        return _parse(db.row(c.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()))


def can_see(user: dict, client: dict) -> bool:
    """Owner sees every client; anyone else only clients they own (a student's own client)."""
    if user.get("role") == "owner":
        return True
    return client.get("owner_user_id") is not None and client.get("owner_user_id") == user.get("id")


def visible(tenant, user: dict) -> list[dict]:
    with tenant.db() as c:
        if user.get("role") == "owner":
            rows = db.rows(c.execute("SELECT * FROM clients ORDER BY id"))
        else:
            rows = db.rows(c.execute("SELECT * FROM clients WHERE owner_user_id=? ORDER BY id", (user.get("id"),)))
        tasks = db.rows(c.execute("SELECT client_id, status FROM client_tasks WHERE kind='check'"))
    prog: dict[int, list[int]] = {}
    for t in tasks:
        p = prog.setdefault(t["client_id"], [0, 0])
        p[1] += 1
        p[0] += 1 if t["status"] == "done" else 0
    out = []
    for r in rows:
        r = _parse(r)
        done, total = prog.get(r["id"], [0, 0])
        r["checklist"] = {"done": done, "total": total}
        r["status_label"] = STATUS_LABELS.get(r["status"], r["status"])
        r.pop("intake_token", None)
        out.append(r)
    return out


def for_user(tenant, user: dict, client_id: int) -> dict:
    cl = get_client(tenant, client_id)
    if cl is None or not can_see(user, cl):
        raise ClientOpsError(404, "Client not found")
    return cl


def public_base() -> str:
    return os.environ.get("OPERATOR_PUBLIC_URL", "").rstrip("/")


def intake_url(tenant, client: dict) -> str | None:
    tok = client.get("intake_token")
    return f"{public_base()}/c/{tenant.id}/{tok}" if tok else None


def absolute(link: str) -> str:
    """Links in emails must be complete. Without OPERATOR_PUBLIC_URL the address part is a visible placeholder,
    so the person approving the email sees it and fills it in."""
    return link if link.startswith("http") else "[your workspace address]" + link


def tasks(tenant, client_id: int) -> list[dict]:
    order = {k: i for i, k in enumerate(CHECK_KEYS)}
    with tenant.db() as c:
        rows = db.rows(c.execute("SELECT * FROM client_tasks WHERE client_id=? ORDER BY id", (client_id,)))
    checks = sorted([r for r in rows if r["kind"] == "check"], key=lambda r: order.get(r["key"], 99))
    for r in checks:
        r["live_check"] = r["key"] in LIVE_VERIFIABLE
    return checks + [r for r in rows if r["kind"] != "check"]


def history(tenant, client_id: int, limit: int = 26) -> list[dict]:
    with tenant.db() as c:
        rows = db.rows(c.execute("SELECT * FROM client_metrics WHERE client_id=? ORDER BY period_start DESC, id DESC LIMIT ?",
                                 (client_id, limit)))
    for r in rows:
        r["metrics"] = db.jload(r["metrics"], {})
    return list(reversed(rows))


def client_items(tenant, client_id: int, status: str | None = None, kind: str | None = None, limit: int = 200) -> list[dict]:
    """Queue items drafted for one client (meta.client_id), newest first."""
    q = "SELECT * FROM queue WHERE json_extract(meta,'$.client_id')=?"
    args: list = [client_id]
    if status == "open":
        q += " AND status IN ('pending','held','needs_human')"
    elif status:
        q += " AND status=?"
        args.append(status)
    if kind:
        q += " AND kind=?"
        args.append(kind)
    with tenant.db() as c:
        rows = db.rows(c.execute(q + " ORDER BY id DESC LIMIT ?", (*args, limit)))
    for r in rows:
        r["meta"] = db.jload(r["meta"], {})
    return rows


def detail(tenant, user: dict, client_id: int) -> dict:
    cl = for_user(tenant, user, client_id)
    built = brief_mod.for_client(cl)
    hist = history(tenant, client_id)
    latest = _by_period(hist)[-1] if hist else (cl["metrics"] or {})
    items = client_items(tenant, client_id, limit=40)
    targets = {**DEFAULT_TARGETS, **(cl["kpi_targets"] or {})}
    out = {k: v for k, v in cl.items() if k != "intake_token"}
    out.update(status_label=STATUS_LABELS.get(cl["status"], cl["status"]), brief=built["brief"],
               brief_missing=built["missing"], proof=built.get("proof"), tasks=tasks(tenant, client_id), history=hist,
               kpis=kpi_status(latest, targets), kpi_targets=targets, intake_url=intake_url(tenant, cl),
               intake_link_needs_public_url=not public_base(), weekday_label=WEEKDAYS[cl["report_weekday"] % 7],
               queue=[{"id": i["id"], "kind": i["kind"], "title": i["title"], "status": i["status"],
                       "created": i["created"]} for i in items[:20]],
               metric_keys=METRIC_KEYS)
    return out


# --------------------------------------------------------------------------- onboarding
def _week_start(ts: float | None = None) -> float:
    lt = time.localtime(ts or time.time())
    day0 = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    return day0 - lt.tm_wday * 86400


def _today_start(ts: float | None = None) -> float:
    lt = time.localtime(ts or time.time())
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def _prospect(tenant, prospect_id) -> dict | None:
    if not prospect_id:
        return None
    try:
        with tenant.db() as c:
            return db.row(c.execute("SELECT * FROM prospects WHERE id=?", (int(prospect_id),)).fetchone())
    except Exception:  # noqa: BLE001 - the prospects table is owned by another feature; missing data is fine
        return None


def body_only(text: str) -> str:
    """Email drafts carry their subject in meta.subject (guardrails check both); the body has no Subject: line."""
    lines = text.splitlines()
    return "\n".join(lines[1:]) if lines and lines[0].lower().startswith("subject:") else text


def kickoff_body(tenant, cl: dict, link: str | None) -> str:
    from . import calendar

    first = (cl.get("contact_name") or "").replace(" (DEMO)", "").split(" ")[0] or "there"
    slots = calendar.free_slots(tenant, 3)
    times = ", ".join(s["label"] for s in slots) or "any time that suits you this week"
    intake = absolute(link) if link else "(the intake link will follow)"
    return (f"Subject: Welcome aboard: next steps for {cl['name']}\n"
            f"Hi {first},\n"
            "Thanks for choosing to work with us. Two quick steps so we can start well:\n"
            f"1. Fill in the intake form (about 10 minutes): {intake}\n"
            f"2. Pick a time for our kickoff call: {times}\n"
            "On the kickoff call we'll agree the first milestone, who owns it and the date, and read back exactly "
            "what's included. Reply here with any questions.")


def start_onboarding(tenant, client_id: int, owner_user_id: int | None = None, prospect_id: int | None = None,
                     actor: str = "system", run_fulfilment: bool = True) -> dict:
    """Idempotent: a second call keeps the checklist and the intake link, and never drafts a second kickoff invite
    while the first one is still open."""
    cl = get_client(tenant, client_id)
    if cl is None:
        raise ClientOpsError(404, "Client not found")
    pr = _prospect(tenant, prospect_id or cl.get("prospect_id"))

    def fn(run: runtime.Run):
        now = db.now()
        updates = {"status": "onboarding" if cl["status"] in (None, "", "onboarding", "active") and not cl.get("start_date")
                   else cl["status"], "start_date": cl.get("start_date") or now,
                   "intake_token": cl.get("intake_token") or secrets.token_urlsafe(18)}
        if owner_user_id is not None and not cl.get("owner_user_id"):
            updates["owner_user_id"] = int(owner_user_id)
        if pr:
            updates["prospect_id"] = pr["id"]
            if not cl.get("email") and pr.get("email"):
                updates["email"] = pr["email"]
            if not cl.get("contact_name") and pr.get("name"):
                updates["contact_name"] = pr["name"]
            if not cl.get("niche") and pr.get("niche"):
                updates["niche"] = pr["niche"]
            if not cl.get("offer") and pr.get("offer"):
                updates["offer"] = pr["offer"]
        sets = ", ".join(f"{k}=?" for k in updates)
        with tenant.db() as c:
            c.execute(f"UPDATE clients SET {sets} WHERE id=?", (*updates.values(), client_id))
            for k, title, what in CHECKLIST:
                c.execute("INSERT OR IGNORE INTO client_tasks(client_id,key,kind,title,detail,status,due,created,demo) "
                          "VALUES(?,?,?,?,?,?,?,?,?)", (client_id, k, "check", title, what, "open",
                                                        now + (7 if k != "kickoff_call" else 5) * 86400, now,
                                                        1 if cl["demo"] or tenant.is_demo else 0))
        cl2 = get_client(tenant, client_id)
        run.note("checklist", {"client_id": client_id}, {"status": cl2["status"], "items": CHECK_KEYS}, label="Access checklist")
        link = intake_url(tenant, cl2)
        open_kick = [i for i in client_items(tenant, client_id, kind="booking_dm")
                     if i["meta"].get("purpose") == "client_kickoff" and i["status"] != "rejected"]
        kick = open_kick[0] if open_kick else None
        if kick is None:
            meta = {"client_id": client_id, "purpose": "client_kickoff", "prior_contact": True,
                    "subject": f"Welcome aboard: next steps for {cl2['name']}"}
            if cl2.get("owner_user_id"):
                meta["owner_user_id"] = cl2["owner_user_id"]
            kick = queue.add(tenant, "booking_dm", body_only(kickoff_body(tenant, cl2, link)), channel="email",
                             recipient_ref=cl2.get("email") or f"client:{client_id}", title=f"Kickoff invite: {cl2['name']}",
                             meta=meta, run_id=run.id, actor=actor, demo=bool(cl2["demo"]))
        run.note("kickoff", {}, {"queue_id": kick["id"], "status": kick["status"]}, label="Kickoff invite (for approval)")
        fres = None
        done_before = any(i["meta"].get("fulfilment") and i["status"] != "rejected"
                          for i in client_items(tenant, client_id, limit=100))
        if run_fulfilment and done_before:
            run.note("fulfilment", {"client_id": client_id}, "Fulfilment drafts already exist for this client",
                     label="Fulfilment from the client's brief", status="skipped")
        elif run_fulfilment:
            from .agents import fulfilment

            fres = run.step("fulfilment", {"client_id": client_id},
                            lambda: {k: v for k, v in fulfilment.fulfil(tenant, client_id).items()
                                     if k in ("run_id", "queue_ids", "brief_missing")},
                            "Fulfilment from the client's brief", allow_fail=True)
        audit.log(tenant, actor, "action", {"client_onboarding": client_id, "kickoff_queue_id": kick["id"]})
        return {"client_id": client_id, "status": cl2["status"], "kickoff_queue_id": kick["id"],
                "intake_url": link, "fulfilment": fres}

    out = runtime.execute(tenant, "client_ops", f"Onboard {cl['name']}",
                          ["Access checklist", "Kickoff invite (for approval)", "Fulfilment from the client's brief"], fn,
                          actor)
    return {**out["result"], "run_id": out["run_id"]}


@events.on("client_won")
def _on_client_won(tenant, name: str, payload: dict) -> None:
    cid = payload.get("client_id")
    try:
        cid = int(cid)
    except (TypeError, ValueError):
        return
    owner = payload.get("owner_user_id")
    start_onboarding(tenant, cid, owner_user_id=int(owner) if isinstance(owner, int) or str(owner or "").isdigit() else None,
                     prospect_id=payload.get("prospect_id"))


def set_task(tenant, client_id: int, key: str, done: bool, actor: str, verified_by: str = "manual") -> dict:
    if key not in CHECK_KEYS:
        raise ClientOpsError(422, "Unknown checklist item")
    now = db.now()
    with tenant.db() as c:
        n = c.execute("UPDATE client_tasks SET status=?, verified_by=?, done_at=?, done_by=? WHERE client_id=? AND key=?",
                      ("done" if done else "open", verified_by if done else None, now if done else None,
                       actor if done else None, client_id, key)).rowcount
        if not n:
            raise ClientOpsError(404, "Start onboarding first: this client has no checklist yet")
        left = c.execute("SELECT COUNT(*) FROM client_tasks WHERE client_id=? AND kind='check' AND status!='done'",
                         (client_id,)).fetchone()[0]
        st = c.execute("SELECT status FROM clients WHERE id=?", (client_id,)).fetchone()
        if left == 0 and st and st["status"] == "onboarding":
            c.execute("UPDATE clients SET status='active' WHERE id=?", (client_id,))
    audit.log(tenant, actor, "action", {"client_task": key, "client_id": client_id, "done": done, "by": verified_by})
    return {"ok": True, "left": left, "status": "active" if left == 0 and st and st["status"] == "onboarding"
            else (st["status"] if st else None)}


def verify_task(tenant, client_id: int, key: str, actor: str) -> dict:
    """A live check for items an API can confirm. Only with the tenant's live Meta switch on and the client's ad
    account on the allowlist; otherwise it says so and the item stays a manual tick."""
    if key not in CHECK_KEYS:
        raise ClientOpsError(422, "Unknown checklist item")
    cl = get_client(tenant, client_id)
    if cl is None:
        raise ClientOpsError(404, "Client not found")
    if key not in LIVE_VERIFIABLE:
        return {"verified": False, "reason": "There's no automatic check for this item. Tick it by hand once it's done."}
    acct = (cl["access"] or {}).get("ad_account_id")
    if not tenant.get("live_meta_enabled", False):
        return {"verified": False, "reason": "Automatic checks are off for this workspace. Tick it by hand once it's done."}
    if not acct or acct not in (tenant.get("meta_allowlist", []) or []):
        return {"verified": False, "reason": "Add the client's ad account ID and ask the owner to allowlist it first."}
    from .live import meta as live_meta

    try:
        ad = live_meta.MetaAdsAdapter(tenant, acct)
        ok = ad.get(acct) is not None
    except Exception as e:  # noqa: BLE001 - a failed check never ticks the item; the reason is redacted
        return {"verified": False, "reason": redact(f"The check didn't go through ({type(e).__name__}). Try again or tick by hand.")}
    if ok:
        set_task(tenant, client_id, key, True, actor, verified_by="live")
    return {"verified": ok, "reason": None if ok else "The ad account isn't visible to your business yet."}


# --------------------------------------------------------------------------- intake (public form)
_URL = re.compile(r"^https?://[^\s<>\"']{3,300}$", re.I)
_EMAIL = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
INTAKE_TEXT = {"offer": 300, "audience": 200, "outcome": 200, "mechanism": 160, "pain": 160, "timeframe": 60, "niche": 120}


def by_token(tenant, token: str) -> dict | None:
    if not token or len(token) < 10 or len(token) > 80:
        return None
    with tenant.db() as c:
        cl = _parse(db.row(c.execute("SELECT * FROM clients WHERE intake_token=?", (token,)).fetchone()))
    if cl is None or cl["status"] not in ("onboarding", "active"):
        return None
    return cl


def submit_intake(tenant, token: str, data: dict, actor: str = "client(intake)") -> dict:
    cl = by_token(tenant, token)
    if cl is None:
        raise ClientOpsError(404, "This link isn't valid any more. Ask for a new one.")
    clean: dict = {}
    for k, mx in INTAKE_TEXT.items():
        v = data.get(k)
        if v is None or v == "":
            continue
        if not isinstance(v, str):
            raise ClientOpsError(422, f"{k} must be text")
        v = " ".join(v.split())
        if len(v) > mx:
            raise ClientOpsError(422, f"{k} is too long (max {mx} characters)")
        if redact(v) != v:
            raise ClientOpsError(422, "Please don't paste passwords or access tokens into this form.")
        clean[k] = v
    if not clean.get("offer") or not clean.get("audience"):
        raise ClientOpsError(422, "Tell us at least your offer and who it's for.")
    proof_text = data.get("proof") or ""
    if not isinstance(proof_text, str) or len(proof_text) > 2000:
        raise ClientOpsError(422, "Proof must be text (max 2,000 characters)")
    permission = data.get("proof_permission") is True
    assets = data.get("assets") or []
    if isinstance(assets, str):
        assets = [a.strip() for a in assets.splitlines() if a.strip()]
    if not isinstance(assets, list) or len(assets) > 10 or not all(isinstance(a, str) and _URL.match(a.strip()) for a in assets):
        raise ClientOpsError(422, "Asset links must be up to 10 web addresses starting with http:// or https://")
    email = data.get("email") or ""
    if email and (not isinstance(email, str) or not _EMAIL.match(email.strip()) or len(email) > 200):
        raise ClientOpsError(422, "That email address doesn't look right")
    wd = data.get("report_weekday")
    if wd is not None and (not isinstance(wd, int) or isinstance(wd, bool) or not 0 <= wd <= 6):
        raise ClientOpsError(422, "Report day must be 0 (Monday) to 6 (Sunday)")
    brief = {**(cl["brief"] or {}), **clean,
             "proof": {"text": proof_text.strip(), "permission": permission} if proof_text.strip() else None,
             "assets": [a.strip() for a in assets]}
    sets = {"brief": db.jdump(brief), "intake_at": db.now()}
    if email:
        sets["email"] = email.strip()
    if wd is not None:
        sets["report_weekday"] = wd
    if clean.get("niche"):
        sets["niche"] = clean["niche"]
    if clean.get("offer") and not cl.get("offer"):
        sets["offer"] = clean["offer"]
    with tenant.db() as c:
        c.execute(f"UPDATE clients SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?", (*sets.values(), cl["id"]))
    try:
        set_task(tenant, cl["id"], "intake_form", True, actor, verified_by="client")
    except ClientOpsError:
        pass
    audit.log(tenant, actor, "action", {"client_intake": cl["id"], "fields": sorted(clean), "proof_permission": permission})
    refreshed = refresh_fulfilment(tenant, cl["id"], actor)
    return {"ok": True, "fulfilment_run_id": refreshed.get("run_id")}


def refresh_fulfilment(tenant, client_id: int, actor: str = "system") -> dict:
    """Re-run fulfilment with the current brief; open drafts from earlier runs are superseded (rejected)."""
    from .agents import fulfilment

    for it in client_items(tenant, client_id, status="open", limit=500):
        if it["meta"].get("fulfilment"):
            try:
                queue.reject(tenant, it["id"], actor, reason="superseded by the client's intake answers")
            except queue.QueueError:
                pass
    try:
        return fulfilment.fulfil(tenant, client_id)
    except Exception as e:  # noqa: BLE001 - the intake is saved even if the drafts can't be rebuilt right now
        log.warning("fulfilment refresh failed: %s", redact(e))
        return {}


# --------------------------------------------------------------------------- updates
def update(tenant, client_id: int, patch: dict, actor: str) -> dict:
    cl = get_client(tenant, client_id)
    if cl is None:
        raise ClientOpsError(404, "Client not found")
    sets: dict = {}
    if "status" in patch and patch["status"] is not None:
        if patch["status"] not in STATUSES:
            raise ClientOpsError(422, "Unknown status")
        sets["status"] = patch["status"]
    for k in ("retainer",):
        if patch.get(k) is not None:
            sets[k] = float(patch[k])
    for k in ("email", "contact_name", "niche", "offer"):
        if patch.get(k) is not None:
            v = str(patch[k]).strip()
            if k == "email" and v and not _EMAIL.match(v):
                raise ClientOpsError(422, "That email address doesn't look right")
            sets[k] = v
    if patch.get("price") is not None:
        sets["price"] = float(patch["price"])
    if patch.get("report_weekday") is not None:
        sets["report_weekday"] = int(patch["report_weekday"])
    if patch.get("kpi_targets") is not None:
        kt = {k: float(v) for k, v in patch["kpi_targets"].items() if k in METRIC_KEYS}
        sets["kpi_targets"] = db.jdump(kt)
    if patch.get("access") is not None:
        acc = {}
        for k, v in patch["access"].items():
            if k not in ACCESS_KEYS:
                raise ClientOpsError(422, f"Unknown access field {k}")
            v = str(v or "").strip()[:300]
            if redact(v) != v or re.search(r"\b(password|passwd|secret)\b", v, re.I):
                raise ClientOpsError(422, "Don't store passwords or tokens here. Keep logins in the client's own tools.")
            acc[k] = v
        sets["access"] = db.jdump({**(cl["access"] or {}), **acc})
    if patch.get("brief") is not None:
        b = dict(cl["brief"] or {})
        for k, v in patch["brief"].items():
            if k in brief_mod.FIELDS and k not in ("date", "faq"):
                b[k] = str(v).strip()[:300]
        sets["brief"] = db.jdump(b)
    if not sets:
        return get_client(tenant, client_id)
    with tenant.db() as c:
        c.execute(f"UPDATE clients SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?", (*sets.values(), client_id))
    audit.log(tenant, actor, "action", {"client_update": client_id, "fields": sorted(sets)})
    return get_client(tenant, client_id)


def log_call(tenant, client_id: int, outcome: str, note: str, actor: str) -> dict:
    if outcome not in ("held", "missed"):
        raise ClientOpsError(422, "Outcome must be held or missed")
    now = db.now()
    with tenant.db() as c:
        tid = c.execute("INSERT INTO client_tasks(client_id,key,kind,title,detail,status,done_at,done_by,created,demo) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (client_id, f"call:{secrets.token_hex(4)}", "call", "Client call", note[:500], outcome, now, actor,
                         now, 1 if tenant.is_demo else 0)).lastrowid
    return {"id": tid, "outcome": outcome}


# --------------------------------------------------------------------------- metrics
def _clean_metrics(metrics: dict) -> dict:
    if not isinstance(metrics, dict) or not metrics:
        raise ClientOpsError(422, "Add at least one number")
    out = {}
    for k, v in metrics.items():
        if k not in METRIC_KEYS:
            raise ClientOpsError(422, f"Unknown number: {k}")
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise ClientOpsError(422, f"{METRIC_KEYS[k]} must be a number")
        if not math.isfinite(f) or f < 0 or f > 1e9:
            raise ClientOpsError(422, f"{METRIC_KEYS[k]} must be between 0 and 1,000,000,000")
        out[k] = f
    return out


def record_metrics(tenant, client_id: int, metrics: dict, period_start: float | None = None, source: str = "manual",
                   note: str = "", actor: str = "system") -> dict:
    m = _clean_metrics(metrics)
    if get_client(tenant, client_id) is None:
        raise ClientOpsError(404, "Client not found")
    ps = _week_start(period_start) if period_start else _week_start()
    with tenant.db() as c:
        prev = c.execute("SELECT id, metrics FROM client_metrics WHERE client_id=? AND period_start=? AND source=?",
                         (client_id, ps, source)).fetchone()
        if prev:  # one row per week and source: a second entry for the same week updates it
            merged = {**db.jload(prev["metrics"], {}), **m}
            c.execute("UPDATE client_metrics SET metrics=?, note=?, created=? WHERE id=?",
                      (db.jdump(merged), note[:300], db.now(), prev["id"]))
            mid = prev["id"]
        else:
            mid = c.execute("INSERT INTO client_metrics(client_id,period_start,metrics,source,note,created,demo) "
                            "VALUES(?,?,?,?,?,?,?)", (client_id, ps, db.jdump(m), source, note[:300], db.now(),
                                                      1 if tenant.is_demo else 0)).lastrowid
        latest = _latest_period(c, client_id)
        c.execute("UPDATE clients SET metrics=? WHERE id=?", (db.jdump(latest), client_id))
    audit.log(tenant, actor, "action", {"client_metrics": client_id, "source": source, "keys": sorted(m)})
    return {"id": mid, "period_start": ps, "metrics": m, "source": source}


def _latest_period(c, client_id: int) -> dict:
    r = c.execute("SELECT MAX(period_start) p FROM client_metrics WHERE client_id=?", (client_id,)).fetchone()
    if not r or r["p"] is None:
        return {}
    merged: dict = {}
    for row in c.execute("SELECT metrics FROM client_metrics WHERE client_id=? AND period_start=? ORDER BY id",
                         (client_id, r["p"])):
        merged.update(db.jload(row["metrics"], {}))
    return merged


def import_ads_csv(tenant, client_id: int, text: str, period_start: float | None = None, actor: str = "system") -> dict:
    from . import ads_import

    try:
        ads = ads_import.parse(text)
    except ads_import.ImportError_ as e:
        raise ClientOpsError(422, str(e))
    tot = {"ad_spend": round(sum(a["spend"] for a in ads), 2), "leads": sum(a["leads"] for a in ads),
           "impressions": sum(a["impressions"] for a in ads), "clicks": sum(a["clicks"] for a in ads)}
    res = record_metrics(tenant, client_id, tot, period_start, source="ads_csv", note=f"{len(ads)} ads", actor=actor)
    return {**res, "ads": len(ads)}


def meta_metrics(tenant, client: dict) -> dict | None:
    """Read this week's totals from the Meta API: only with the live switch on and the account allowlisted."""
    acct = (client.get("access") or {}).get("ad_account_id")
    if not acct or not tenant.get("live_meta_enabled", False) or acct not in (tenant.get("meta_allowlist", []) or []):
        return None
    from .live import meta as live_meta

    try:
        ads = live_meta.MetaAdsAdapter(tenant, acct).tree(acct)
    except Exception as e:  # noqa: BLE001
        log.warning("meta read failed: %s", redact(e))
        return None
    return {"ad_spend": round(sum(a["metrics"].get("spend", 0) for a in ads), 2),
            "leads": sum(a["metrics"].get("leads", 0) for a in ads),
            "impressions": sum(a["metrics"].get("impressions", 0) for a in ads),
            "clicks": sum(a["metrics"].get("clicks", 0) for a in ads)}


def kpi_status(latest: dict, targets: dict) -> list[dict]:
    out = []
    for k in REPORT_KPIS:
        if k not in latest and k not in targets:
            continue
        v = latest.get(k)
        t = targets.get(k)
        out.append({"key": k, "label": METRIC_KEYS[k], "value": v, "target": t,
                    "ok": None if v is None or t is None else (v <= t if k == "ad_spend" else v >= t)})
    return out


# --------------------------------------------------------------------------- churn risk
def churn_signals(tenant, cl: dict, hist: list[dict], now: float | None = None) -> list[dict]:
    now = now or db.now()
    sig = []
    targets = {**DEFAULT_TARGETS, **(cl.get("kpi_targets") or {})}
    periods = _by_period(hist)
    if len(periods) >= 2:
        cur, prev = periods[-1], periods[-2]
        for k in ("leads", "booked", "closed", "revenue"):
            if prev.get(k) and cur.get(k) is not None and cur[k] <= prev[k] * 0.8:
                sev = "high" if len(periods) >= 3 and periods[-3].get(k) and prev[k] <= periods[-3][k] * 0.8 else "medium"
                sig.append({"code": f"falling_{k}", "severity": sev,
                            "label": f"{METRIC_KEYS[k]} fell from {prev[k]:,.0f} to {cur[k]:,.0f} week on week"})
    if periods:
        cur = periods[-1]
        for k in ("leads", "booked", "closed"):
            t = targets.get(k)
            if t and cur.get(k) is not None and cur[k] < t * 0.5:
                sig.append({"code": f"under_target_{k}", "severity": "medium",
                            "label": f"{METRIC_KEYS[k]} at {cur[k]:,.0f} against a target of {t:,.0f}"})
    with tenant.db() as c:
        calls = [r["status"] for r in c.execute(
            "SELECT status FROM client_tasks WHERE client_id=? AND kind='call' ORDER BY created DESC LIMIT 2", (cl["id"],))]
        last_touch = c.execute("SELECT MAX(done_at) FROM client_tasks WHERE client_id=? AND status IN ('done','held')",
                               (cl["id"],)).fetchone()[0]
        sent_touch = c.execute("SELECT MAX(decided_at) FROM queue WHERE status='approved' AND "
                               "json_extract(meta,'$.client_id')=?", (cl["id"],)).fetchone()[0]
        last_touch = max(last_touch or 0, sent_touch or 0) or None
        open_checks = c.execute("SELECT COUNT(*) FROM client_tasks WHERE client_id=? AND kind='check' AND status!='done'",
                                (cl["id"],)).fetchone()[0]
    if len(calls) == 2 and all(s == "missed" for s in calls):
        sig.append({"code": "missed_calls", "severity": "high", "label": "Two client calls missed in a row"})
    started = cl.get("start_date") or cl.get("created") or now
    if now - started > 14 * 86400 and now - (last_touch or started) > 14 * 86400:
        sig.append({"code": "no_touch", "severity": "medium", "label": "No logged human contact for 14 days or more"})
    if cl.get("status") == "onboarding" and open_checks and now - started > 14 * 86400:
        sig.append({"code": "onboarding_late", "severity": "high",
                    "label": f"Onboarding still has {open_checks} open item(s) after two weeks"})
    elif cl.get("status") == "onboarding" and not cl.get("intake_at") and now - started > 7 * 86400:
        sig.append({"code": "intake_missing", "severity": "medium", "label": "The intake form isn't in after a week"})
    return sig


def _by_period(hist: list[dict]) -> list[dict]:
    merged: dict[float, dict] = {}
    for h in hist:
        merged.setdefault(h["period_start"], {}).update(h["metrics"] or {})
    return [merged[k] for k in sorted(merged)]


def _escalate(tenant, cl: dict, signals: list[dict], actor: str) -> dict | None:
    if not signals:
        if cl.get("risk"):
            with tenant.db() as c:
                c.execute("UPDATE clients SET risk=NULL WHERE id=?", (cl["id"],))
        return None
    codes = sorted(s["code"] for s in signals)
    prev = cl.get("risk") or {}
    if prev.get("codes") == codes and db.now() - (prev.get("at") or 0) < WEEK:
        return prev  # already raised for the same signals this week
    sev = "high" if any(s["severity"] == "high" for s in signals) else "medium"
    detail = f"Churn risk for {cl['name']}: " + "; ".join(s["label"] for s in signals)
    esc_id = None
    try:
        from . import escalations

        esc_id = escalations.raise_(tenant, "client_churn_risk", sev, detail, assignee_user_id=cl.get("owner_user_id"),
                                    ref=f"client:{cl['id']}")
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001 - the signal is still stored on the client and shown in the UI
        log.warning("escalation failed: %s", redact(e))
    if esc_id is None:  # escalations not available: keep a visible task instead
        with tenant.db() as c:
            c.execute("INSERT OR IGNORE INTO client_tasks(client_id,key,kind,title,detail,status,created,demo) "
                      "VALUES(?,?,?,?,?,?,?,?)", (cl["id"], f"risk:{int(_week_start())}", "risk", "Churn risk: reach out",
                                                  detail[:500], "open", db.now(), 1 if tenant.is_demo else 0))
    risk = {"codes": codes, "signals": signals, "severity": sev, "at": db.now(), "escalation_id": esc_id}
    with tenant.db() as c:
        c.execute("UPDATE clients SET risk=? WHERE id=?", (db.jdump(risk), cl["id"]))
    audit.log(tenant, actor, "action", {"client_risk": cl["id"], "codes": codes, "escalation_id": esc_id})
    return risk


# --------------------------------------------------------------------------- weekly loop
MONEY_KEYS = {"ad_spend", "revenue"}


def _fmt(k: str, v) -> str:
    if v is None:
        return "not recorded"
    return f"${v:,.2f}" if k == "ad_spend" else f"${v:,.0f}" if k in MONEY_KEYS else f"{v:,.0f}"


def report_body(cl: dict, b: dict, latest: dict, prev: dict, targets: dict, analysis: dict, actions_done: list[str],
                source: str, demo: bool, ai_line: str | None = None) -> str:
    rows = []
    for k in REPORT_KPIS:
        if k not in latest:
            continue
        t = targets.get(k)
        delta = ""
        if prev.get(k):
            ch = (latest[k] - prev[k]) / prev[k]
            delta = (", the same as last week" if abs(ch) < 0.005 else
                     f", {'up' if ch > 0 else 'down'} {abs(ch):.0%} on last week")
        rows.append(f"- {METRIC_KEYS[k]}: {_fmt(k, latest[k])}" + (f" (target {_fmt(k, t)})" if t is not None else "") + delta)
    bn = analysis.get("bottleneck")
    insight = (f"The stage to fix first is {bn['label'].lower()}." if bn else
               "No stage is below its benchmark with enough data to call it; next week we keep volume up.")
    acts = actions_done[:3] or ["Reviewed this week's numbers"]
    window = "Ad results use a 7-day click and 1-day view attribution window." if source in ("meta", "ads_csv") else ""
    src = {"meta": "the Meta Ads read connection", "ads_csv": "your Ads Manager export", "manual": "the numbers entered by hand",
           "webinar": "the webinar attendance report"}.get(source, "the numbers on file")
    lines = [f"Subject: {b['business']}: your weekly numbers",
             f"Hi {(cl.get('contact_name') or '').replace(' (DEMO)', '').split(' ')[0] or 'there'},",
             ai_line or f"Here's your week, measured against the goal of {b['outcome']}.",
             "", "This week's numbers" + (" (DEMO)" if demo else "") + f", from {src}:", *(rows or ["- No numbers recorded this week yet"]),
             window, "", "What we did:", *[f"- {a}" for a in acts], "", "What we learned:", f"- {insight}", "",
             "Next week:", f"- {_next_step(bn)}", "", "Reply with any questions. Numbers are what was measured, not a forecast."]
    return "\n".join(l for l in lines if l is not None).replace("\n\n\n", "\n\n")


def _next_step(bn: dict | None) -> str:
    if not bn:
        return "Keep the current plan running and add volume where it's working."
    from . import diagnostic

    st = next((s for s in diagnostic.STAGES if s.key == bn["key"]), None)
    return st.fix if st else "Work on the weakest stage."


def _actions_done(tenant, client_id: int, since: float) -> list[str]:
    with tenant.db() as c:
        done = [r["title"] for r in c.execute(
            "SELECT title FROM client_tasks WHERE client_id=? AND kind='check' AND status='done' AND done_at>=?",
            (client_id, since))]
    approved = [i["title"] for i in client_items(tenant, client_id, status="approved", limit=50)
                if (i.get("decided_at") or 0) >= since]
    return [f"Completed: {t}" for t in done] + [f"Approved: {t}" for t in approved]


def weekly_ops(tenant, client_id: int, user: dict | None = None, now: float | None = None) -> dict:
    """One pass of the weekly loop for one client. Drafts only; every outgoing item waits for approval."""
    from . import diagnostic, llm

    actor = f"{user['username']}({user['role']})" if user else "system"
    cl = get_client(tenant, client_id)
    if cl is None:
        raise ClientOpsError(404, "Client not found")
    now = now or db.now()
    demo = bool(cl["demo"]) or tenant.is_demo

    def fn(run: runtime.Run):
        live = run.step("metrics_live", {"source": "meta"}, lambda: meta_metrics(tenant, cl), "Meta read (when switched on)",
                        allow_fail=True)
        if live:
            record_metrics(tenant, client_id, live, now, source="meta", actor=actor)
        hist = history(tenant, client_id)
        periods = _by_period(hist)
        latest = periods[-1] if periods else dict(cl["metrics"] or {})
        prev = periods[-2] if len(periods) >= 2 else {}
        source = "meta" if live else (hist[-1]["source"] if hist else "none")
        fresh = bool(hist) and now - hist[-1]["period_start"] < 2 * WEEK
        run.note("metrics", {"periods": len(periods)}, {"latest": latest, "source": source, "fresh": fresh},
                 label="This week's numbers")
        an = run.step("diagnose", {}, lambda: diagnostic.analyse({**latest, "offer_price": cl["price"] or 3000}),
                      "Diagnostic (pure maths)")
        acct = (cl["access"] or {}).get("ad_account_id")
        props = None
        waiting = [i for i in queue.list_items(tenant, status="open", kind="ad_change", limit=300)
                   if acct and i["recipient_ref"] == acct]
        if acct and waiting:
            run.note("ads", {"account": acct}, f"{len(waiting)} earlier proposal(s) still waiting for approval: none added",
                     label="Ad change proposals (approval gated)", status="skipped")
        elif acct:
            from .agents import ads_ops

            props = run.step("ads", {"account": acct}, lambda: ads_ops.propose_changes(tenant, acct, user=user),
                             "Ad change proposals (approval gated)", allow_fail=True)
        else:
            run.note("ads", {}, "No ad account linked: no ad proposals", label="Ad change proposals (approval gated)",
                     status="skipped")
        built = brief_mod.for_client(cl)
        b = built["brief"]
        targets = {**DEFAULT_TARGETS, **(cl["kpi_targets"] or {})}
        ai_line = None
        mode = "rules"
        if llm.unavailable_reason(tenant) is None:
            try:
                ai = llm.complete_or_replay(
                    tenant, "Write one plain, honest sentence summarising a client's week from the numbers. No promises, "
                            "no forecasts, no income claims.",
                    f"Numbers this week: {latest}\nLast week: {prev}\nGoal: {b['outcome']}", purpose=f"client_week:{client_id}",
                    max_tokens=120)
                ai_line = ai["text"].strip().splitlines()[0][:300] if ai.get("text") else None
                mode = ai.get("mode", "live")
                run.use_ai(mode)
            except llm.AIUnavailable:
                pass
        body = report_body(cl, b, latest, prev, targets, an, _actions_done(tenant, client_id, now - WEEK), source, demo,
                           ai_line)
        meta = {"client_asset": True, "client_id": client_id, "report": "weekly", "period_start": _week_start(now),
                "written_by": mode, "subject": f"{b['business']}: your weekly numbers"}
        if cl.get("owner_user_id"):
            meta["owner_user_id"] = cl["owner_user_id"]
        rep = queue.add(tenant, "client_report", body_only(body), channel="email",
                        recipient_ref=cl.get("email") or f"client:{client_id}",
                        title=f"Weekly report: {cl['name']}", meta=meta, run_id=run.id, actor=actor, demo=demo)
        run.note("report", {}, {"queue_id": rep["id"], "status": rep["status"], "flags": rep["flags"]},
                 label="Weekly report (for approval)")
        monthly = None
        started = cl.get("start_date") or cl.get("created") or now
        if now - (cl.get("last_review_at") or started) >= MONTH:
            base = periods[0] if periods else {}
            lines = [f"Subject: {b['business']}: monthly review",
                     "A 30-minute review with the decision-maker: goals re-confirmed, trend against the starting point.", ""]
            for k in REPORT_KPIS:
                if k in latest or k in base:
                    lines.append(f"- {METRIC_KEYS[k]}: started at {_fmt(k, base.get(k))}, now {_fmt(k, latest.get(k))}")
            lines += ["", "Questions for the review: is the goal still right? What should we stop, start and keep?"]
            mm = {**meta, "report": "monthly", "subject": f"{b['business']}: monthly review"}
            monthly = queue.add(tenant, "client_report", body_only("\n".join(lines)), channel="email",
                                recipient_ref=cl.get("email") or f"client:{client_id}", title=f"Monthly review: {cl['name']}",
                                meta=mm, run_id=run.id, actor=actor, demo=demo)
            with tenant.db() as c:
                c.execute("UPDATE clients SET last_review_at=? WHERE id=?", (now, client_id))
        run.note("monthly", {}, {"queue_id": monthly["id"]} if monthly else "Not due yet", label="Monthly review",
                 status="ok" if monthly else "skipped")
        signals = churn_signals(tenant, {**cl, "kpi_targets": cl["kpi_targets"]}, hist, now)
        risk = _escalate(tenant, cl, signals, actor)
        run.note("risk", {}, {"signals": signals, "escalation_id": (risk or {}).get("escalation_id")},
                 label="Churn-risk signals")
        with tenant.db() as c:
            c.execute("UPDATE clients SET last_report_at=? WHERE id=?", (now, client_id))
        return {"client_id": client_id, "latest": latest, "source": source, "fresh": fresh, "analysis": an,
                "report_queue_id": rep["id"], "report_status": rep["status"],
                "monthly_queue_id": monthly["id"] if monthly else None,
                "proposals": (props or {}).get("proposals", []) if props else [], "signals": signals, "mode": mode,
                "mode_label": "Written from a template (AI off)" if mode == "rules" else "AI summary line, checked by guardrails"}

    out = runtime.execute(tenant, "client_ops", f"Weekly ops: {cl['name']}",
                          ["Meta read (when switched on)", "This week's numbers", "Diagnostic (pure maths)",
                           "Ad change proposals (approval gated)", "Weekly report (for approval)", "Monthly review",
                           "Churn-risk signals"], fn, actor)
    return {**out["result"], "run_id": out["run_id"]}


def due(cl: dict, now: float | None = None) -> bool:
    now = now or time.time()
    if cl.get("status") not in ("onboarding", "active"):
        return False
    if time.localtime(now).tm_wday != int(cl.get("report_weekday") or 0) % 7:
        return False
    return (cl.get("last_report_at") or 0) < _today_start(now)


def tick(tenant, now: float | None = None) -> dict:
    """Per-tenant tick (scheduler.TENANT_TICKS): runs the weekly loop for every client due today."""
    now = now or time.time()
    with tenant.db() as c:
        rows = [_parse(r) for r in db.rows(c.execute(
            "SELECT * FROM clients WHERE status IN ('onboarding','active')"))]
    ran, failed = [], []
    for cl in rows:
        if not due(cl, now):
            continue
        try:
            ran.append(weekly_ops(tenant, cl["id"], now=now)["run_id"])
        except Exception as e:  # noqa: BLE001 - one client must not stop the others
            failed.append(cl["id"])
            log.warning("weekly ops failed for client %s: %s", cl["id"], redact(e))
    return {"ran": ran, "failed": failed}


tick.demo_too = True


# --------------------------------------------------------------------------- demo
def seed_demo(tenant) -> None:
    """Demo only: one won client in onboarding (checklist part done, kickoff invite waiting for approval) and a
    few weeks of numbers for the existing demo clients. Never runs against a real workspace."""
    from . import tenancy

    if not tenant.is_demo or not tenancy.is_demo_db(tenant):
        return
    now = time.time()
    with tenant.db() as c:
        if c.execute("SELECT COUNT(*) FROM client_tasks").fetchone()[0]:
            return
        student = c.execute("SELECT id FROM users WHERE username='demo-student'").fetchone()
        base = db.rows(c.execute("SELECT id, metrics FROM clients WHERE demo=1 ORDER BY id"))
        contacts = ["Imani Fairbairn (DEMO)", "Soren Achterberg (DEMO)", "Tessa Kowalczyk (DEMO)", "Idris Nakamura (DEMO)"]
        for i, r in enumerate(base):
            m = db.jload(r["metrics"], {})
            c.execute("UPDATE clients SET status='active', start_date=?, report_weekday=?, email=?, contact_name=?, "
                      "kpi_targets=? WHERE id=?",
                      (now - (70 + 7 * i) * 86400, i % 5, f"client{r['id']}@example.invalid", contacts[i % len(contacts)],
                       db.jdump({"leads": 60, "booked": 12, "showed": 8, "closed": 2}), r["id"]))
            for w in range(4, 0, -1):
                f = 1 + (0.08 * (4 - w) if i % 2 == 0 else -0.12 * (4 - w))  # alternate rising and falling trends
                leads = max(1, round(float(m.get("leads", 40)) / 4 * f))
                booked = max(1, round(leads * 0.22))
                showed = max(0, round(booked * (0.7 if i % 2 == 0 else 0.5)))
                week = {"leads": leads, "booked": booked, "showed": showed, "closed": round(showed * 0.3),
                        "ad_spend": round(float(m.get("ad_spend", 1200)) / 4 * f)}
                c.execute("INSERT INTO client_metrics(client_id,period_start,metrics,source,note,created,demo) "
                          "VALUES(?,?,?,?,?,?,1)", (r["id"], _week_start(now - w * WEEK), db.jdump(week), "manual",
                                                    "(DEMO)", now))
        cid = c.execute(
            "INSERT INTO clients(student_id,name,niche,offer,price,metrics,demo,created,email,contact_name,report_weekday,"
            "brief,kpi_targets,access) VALUES(?,?,?,?,?,?,1,?,?,?,?,?,?,?)",
            (None, "Northstar Fitness Coaching (DEMO)", "online fitness coaches", "12-week coaching programme (DEMO)", 1800,
             "{}", now - 2 * 86400, "kai.marchetti@example.invalid", "Kai Marchetti (DEMO)", 0,
             db.jdump({"audience": "busy professionals who want to get fit at home"}),
             db.jdump({"leads": 40, "booked": 10, "showed": 6, "closed": 2}),
             db.jdump({"ad_account_id": "act_DEMO_practice"}))).lastrowid
    start_onboarding(tenant, cid, owner_user_id=student["id"] if student else None, run_fulfilment=False)
    with tenant.db() as c:
        c.execute("UPDATE clients SET start_date=? WHERE id=?", (now - 2 * 86400, cid))
    for key in ("meta_partner_access", "crm"):
        set_task(tenant, cid, key, True, "demo-student(student)")
