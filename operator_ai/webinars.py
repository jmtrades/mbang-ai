"""Webinar execution: a real event with a date, a hosted registration page, registrants, reminders, attendance
and the hand-off to sales.

* A webinar has a start time and time zone, a join link, and a registration page that is served at
  /w/<tenant>/<slug> only after its copy is approved in the queue. The form needs a consent tick, has a hidden
  honeypot field and is rate limited.
* Each show-up and replay email is a template approved ONCE in the queue. Approval fans it out per registrant as
  queue items with meta.send_after relative to the start time and meta.cancel_if ["unsubscribed"]; the delivery
  feature holds them until due. Only clean items are approved on the template's behalf; anything a guardrail flags
  waits for a person.
* Attendance comes from a Zoom-style CSV or is entered by hand. After the event, people who stayed past the offer
  get a setter follow-up draft (approval needed); everyone else gets the approved replay sequence.
* Registration, show and booked rates feed the diagnostic and are available to missions (funnel_metrics).
Nothing here sends anything itself. Works with AI off: every template is plain text.
"""
from __future__ import annotations

import csv
import io
import logging
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import audit, db, events, guardrails, jobs, queue, runtime
from . import brief as brief_mod
from .redact import redact

log = logging.getLogger("operator.webinars")

STATUSES = ("draft", "open", "ended", "done", "cancelled")
STATUS_LABELS = {"draft": "Page waiting for approval", "open": "Registration open", "ended": "Finished: attendance next",
                 "done": "Finished and followed up", "cancelled": "Cancelled"}
SHOWUP_GRACE = 10 * 60      # a reminder whose time passed more than this ago is skipped for a late registrant
POST_EVENT_GRACE = 2 * 3600  # wait this long after the end for an attendance import before following up

UNSUB_LINE = "Don't want these emails? Unsubscribe: {unsubscribe_link}"
# (key, phase, offset in seconds, label, body). Show-up offsets are from the start; replay offsets from the end.
# "confirm" goes out at registration. Merge fields: {first_name} {title} {when} {join_link} {replay_link}
TEMPLATES = [
    ("confirm", "showup", None, "Confirmation",
     "Subject: You're in: {title}\nHi {first_name},\nYou're registered for {title} on {when}.\nJoin here: {join_link}\n"
     "Before we start, think of one question you'd like answered.\n" + UNSUB_LINE),
    ("day_before", "showup", -86400, "Day-before reminder",
     "Subject: Tomorrow: {title}\nHi {first_name},\nA quick reminder that {title} is tomorrow, {when}.\n"
     "Join link: {join_link}\nBring one question; there's time for questions at the end.\n" + UNSUB_LINE),
    ("hour_before", "showup", -3600, "One-hour reminder",
     "Subject: Starting in one hour: {title}\nHi {first_name},\nWe start in an hour ({when}). Grab a pen.\n"
     "Join here: {join_link}\n" + UNSUB_LINE),
    ("live", "showup", 0, "We're live",
     "Subject: We're live: {title}\nHi {first_name},\nWe're starting now.\nJoin here: {join_link}\n" + UNSUB_LINE),
    ("replay_1", "replay", 2 * 3600, "Replay",
     "Subject: The replay: {title}\nHi {first_name},\nHere's the replay of {title}: {replay_link}\n"
     "The chapters let you jump straight to the part that matters most to you.\n" + UNSUB_LINE),
    ("replay_2", "replay", 86400, "Replay: the key part",
     "Subject: The part people asked about\nHi {first_name},\nIf you only have 15 minutes, watch the middle section "
     "of the replay: {replay_link}\nReply with any question and we'll answer it.\n" + UNSUB_LINE),
    ("replay_3", "replay", 2 * 86400, "Replay: questions",
     "Subject: Any questions about {title}?\nHi {first_name},\nThe replay is here if you'd like another look: "
     "{replay_link}\nIf it would help to talk through how this applies to you, reply and we'll find a time.\n" + UNSUB_LINE),
]
TPL = {t[0]: t for t in TEMPLATES}
SETTER_FOLLOWUP = ("Hi {first_name},\nThanks for staying right to the end of {title}. What stood out most for you?\n"
                   "If it would help, I'm happy to walk through how it could apply to your situation on a short call. "
                   "Just reply and we'll find a time.\nIf you'd rather not hear from us, reply \"stop\" and we won't "
                   "email again.")
RATE_DEFS = {
    "reg_rate": "Registration rate = registrants / page visits",
    "show_rate": "Show rate = live attendees / registrants (live only; replay views are not counted)",
    "offer_reach": "Offer reach = attendees still there at the offer / live attendees",
    "booked_rate": "Booked rate = registrants marked as booked a call / live attendees",
}


class WebinarError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


# --------------------------------------------------------------------------- helpers
def _zone(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise WebinarError(422, "Unknown time zone. Use a name like Europe/London or America/New_York.")


def parse_start(value, tz: str) -> float:
    """Epoch seconds, or an ISO date-time (a time without an offset is read in the webinar's time zone)."""
    zone = _zone(tz)
    if isinstance(value, bool):
        raise WebinarError(422, "Start time must be a date and time")
    if isinstance(value, (int, float)):
        ts = float(value)
    elif isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            raise WebinarError(422, "Start time must look like 2026-11-05T19:00")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=zone)
        ts = dt.timestamp()
    else:
        raise WebinarError(422, "Start time is required")
    now = time.time()
    if not (now - 400 * 86400 <= ts <= now + 400 * 86400):
        raise WebinarError(422, "Start time must be within about a year of today")
    return ts


def when_label(ts: float, tz: str) -> str:
    try:
        dt = datetime.fromtimestamp(ts, ZoneInfo(tz or "UTC"))
    except (ZoneInfoNotFoundError, ValueError):
        dt = datetime.fromtimestamp(ts, timezone.utc)
        tz = "UTC"
    return dt.strftime("%A %d %B, %H:%M") + f" ({tz})"


def _slug(title: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (title or "webinar").lower()).strip("-")[:40] or "webinar"
    return f"{base}-{secrets.token_hex(3)}"


def public_base() -> str:
    import os

    return os.environ.get("OPERATOR_PUBLIC_URL", "").rstrip("/")


def page_url(tenant, w: dict) -> str:
    return f"{public_base()}/w/{tenant.id}/{w['slug']}"


def _end(w: dict) -> float:
    return float(w["starts_at"]) + 60 * int(w.get("duration_min") or 60)


_FIRST_OK = re.compile(r"[^\W\d_]+(?:['\u2019\-][^\W\d_]+)*\.?")


def _first(name: str) -> str:
    """The first name merged into an email. Names come from a public form, and these emails go out on the template's
    approval, so anything that isn't plainly a name (a link, a domain, digits, symbols) becomes "there"."""
    n = (name or "").replace(" (DEMO)", "").strip()
    first = n.split()[0] if n else ""
    return first if len(first) <= 30 and _FIRST_OK.fullmatch(first) else "there"


def _row(r) -> dict | None:
    d = db.row(r)
    if d is not None:
        d["page"] = db.jload(d.get("page"), None)
    return d


def get(tenant, wid: int) -> dict | None:
    with tenant.db() as c:
        return _row(c.execute("SELECT * FROM webinars WHERE id=?", (wid,)).fetchone())


def by_slug(tenant, slug: str) -> dict | None:
    if not slug or len(slug) > 80:
        return None
    with tenant.db() as c:
        return _row(c.execute("SELECT * FROM webinars WHERE slug=?", (slug,)).fetchone())


def _display_status(w: dict, now: float | None = None) -> str:
    now = now or time.time()
    if w["status"] == "open" and w["starts_at"] <= now < _end(w):
        return "Live now"
    if w["status"] == "open" and now >= _end(w):
        return STATUS_LABELS["ended"]
    return STATUS_LABELS.get(w["status"], w["status"])


# --------------------------------------------------------------------------- create
def _page_copy(tenant, title: str, when: str, chain_id: str | None) -> str:
    hooks = []
    if chain_id:
        try:
            from . import webinar_chain

            st = webinar_chain.state(tenant, chain_id)
            th = next((s for s in st["steps"] if s["step"] == "title_hooks"), None)
            hooks = list(((th or {}).get("output") or {}).get("data", {}).get("hooks", []))
        except KeyError:
            hooks = []
    if not hooks:
        b = brief_mod.build(tenant, {})["brief"]
        hooks = [f"What most {b['niche']} get wrong about {b['outcome']}", f"The {b['mechanism']}, step by step",
                 "Live questions at the end"]
    return "\n".join([f"Headline: {title}", f"Subhead: A free live training on {when}.", *[f"- {h}" for h in hooks[:5]],
                      "Button: Save my seat",
                      "Consent: I agree to get emails about this training and its replay. I can unsubscribe at any time."])


def parse_page(body: str) -> dict:
    page = {"headline": "", "subhead": "", "bullets": [], "button": "Save my seat",
            "consent": "I agree to get emails about this training and its replay. I can unsubscribe at any time."}
    for line in (body or "").splitlines():
        s = line.strip()
        low = s.lower()
        if low.startswith("headline:"):
            page["headline"] = s.split(":", 1)[1].strip()
        elif low.startswith("subhead:"):
            page["subhead"] = s.split(":", 1)[1].strip()
        elif low.startswith("button:"):
            page["button"] = s.split(":", 1)[1].strip() or page["button"]
        elif low.startswith("consent:"):
            page["consent"] = s.split(":", 1)[1].strip() or page["consent"]
        elif s.startswith(("-", "*", "•")):
            page["bullets"].append(s.lstrip("-*• ").strip())
    page["bullets"] = [b for b in page["bullets"] if b][:8]
    return page


def create(tenant, data: dict, user: dict | None = None) -> dict:
    actor = f"{user['username']}({user['role']})" if user else "system"
    title = " ".join(str(data.get("title") or "").split())[:140]
    tz = str(data.get("tz") or "UTC")[:60]
    starts = parse_start(data.get("starts_at"), tz)
    join_url = str(data.get("join_url") or "").strip()
    replay_url = str(data.get("replay_url") or "").strip()
    for u in (join_url, replay_url):
        if u and not re.match(r"^https?://\S{3,400}$", u):
            raise WebinarError(422, "Links must start with http:// or https://")
    dur = int(data.get("duration_min") or 60)
    offer_at = int(data.get("offer_at_min") or 35)
    if not 15 <= dur <= 240 or not 1 <= offer_at < dur:
        raise WebinarError(422, "Length must be 15-240 minutes and the offer must start before the end")
    chain_id = data.get("chain_id") or None
    if chain_id:
        try:
            from . import webinar_chain

            webinar_chain.state(tenant, str(chain_id))
        except KeyError:
            raise WebinarError(404, "Webinar chain not found")
    client_id = data.get("client_id") or None
    if client_id is not None:
        with tenant.db() as c:
            if c.execute("SELECT 1 FROM clients WHERE id=?", (int(client_id),)).fetchone() is None:
                raise WebinarError(404, "Client not found")
    if chain_id and not title:
        try:
            from . import webinar_chain

            st = webinar_chain.state(tenant, chain_id)
            th = next((s for s in st["steps"] if s["step"] == "title_hooks"), None)
            title = (((th or {}).get("output") or {}).get("data") or {}).get("title", "")[:140]
        except KeyError:
            raise WebinarError(404, "Webinar chain not found")
    if not title:
        raise WebinarError(422, "Give the webinar a title")
    # the title goes on the public page and into every email as it is, outside the page copy a person approves and
    # edits, so it may carry no claim the guardrails block in client assets
    bad = sorted({f["code"] for f in guardrails.check_for(tenant, title, {"client_asset": True}) if f["severity"] == "block"})
    if bad:
        raise WebinarError(422, "The title can't carry that (" + ", ".join(c.replace("_", " ") for c in bad) + "). "
                                "Use a title that names the topic, without promises or figures.")
    demo = tenant.is_demo
    when = when_label(starts, tz)

    def fn(run: runtime.Run):
        now = db.now()
        with tenant.db() as c:
            wid = c.execute(
                "INSERT INTO webinars(chain_id,slug,title,starts_at,tz,join_url,replay_url,status,duration_min,offer_at_min,"
                "client_id,owner_user_id,demo,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (chain_id, _slug(title), title, starts, tz, join_url, replay_url, "draft", dur, offer_at,
                 int(client_id) if client_id else None, (user or {}).get("id"), 1 if demo else 0, now, now)).lastrowid
        run.note("webinar", data, {"id": wid, "when": when}, label="Event and time zone")
        page = queue.add(tenant, "webinar_page", _page_copy(tenant, title, when, chain_id), channel="manual",
                         title=f"Registration page: {title}", meta={"webinar_id": wid, "client_asset": True, "template": True},
                         run_id=run.id, actor=actor, demo=demo)
        with tenant.db() as c:
            c.execute("UPDATE webinars SET page_qid=? WHERE id=?", (page["id"], wid))
        tq = []
        for key, phase, _off, label, body in TEMPLATES:
            item = queue.add(tenant, "webinar_template", body, channel="manual", title=f"[{label}] {title}",
                             meta={"webinar_id": wid, "template_key": key, "phase": phase, "client_asset": True,
                                   "template": True, "prior_contact": True}, run_id=run.id, actor=actor, demo=demo)
            with tenant.db() as c:
                c.execute("INSERT INTO webinar_templates(webinar_id,key,phase,body,queue_id,status,created) "
                          "VALUES(?,?,?,?,?,?,?)", (wid, key, phase, body, item["id"], "draft", now))
            tq.append(item["id"])
        run.note("drafts", {}, {"page_queue_id": page["id"], "template_queue_ids": tq},
                 label="Page and email templates (for approval)")
        return {"id": wid, "page_queue_id": page["id"], "template_queue_ids": tq}

    out = runtime.execute(tenant, "webinars", f"Webinar: {title}", ["Event and time zone",
                                                                     "Page and email templates (for approval)"], fn, actor)
    return {**out["result"], "run_id": out["run_id"]}


def update(tenant, wid: int, patch: dict, actor: str) -> dict:
    w = get(tenant, wid)
    if w is None:
        raise WebinarError(404, "Webinar not found")
    sets: dict = {}
    for k in ("join_url", "replay_url"):
        if patch.get(k) is not None:
            v = str(patch[k]).strip()
            if v and not re.match(r"^https?://\S{3,400}$", v):
                raise WebinarError(422, "Links must start with http:// or https://")
            sets[k] = v
    if patch.get("tz") is not None:
        _zone(patch["tz"])
        sets["tz"] = patch["tz"]
    if patch.get("starts_at") is not None:
        sets["starts_at"] = parse_start(patch["starts_at"], sets.get("tz") or w["tz"])
    if patch.get("status") == "cancelled":
        sets["status"] = "cancelled"
    elif patch.get("status") not in (None, "cancelled"):
        raise WebinarError(422, "Only cancelling is allowed here; the rest follows from approvals and the clock")
    if not sets:
        return get(tenant, wid)
    sets["updated"] = db.now()
    with tenant.db() as c:
        c.execute(f"UPDATE webinars SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?", (*sets.values(), wid))
    if sets.get("status") == "cancelled":
        _cancel_sends(tenant, wid, None, actor, "webinar cancelled")
    elif any(sets.get(k, w.get(k)) != w.get(k) for k in ("starts_at", "tz", "join_url", "replay_url")):
        _refresh_unsent(tenant, wid, actor)
    audit.log(tenant, actor, "action", {"webinar_update": wid, "fields": sorted(sets)})
    return get(tenant, wid)


# --------------------------------------------------------------------------- approvals and fan-out
@queue.on_approve("webinar_page")
def _page_approved(tenant, item: dict, actor: str) -> None:
    wid = item["meta"].get("webinar_id")
    if not wid:
        return
    page = parse_page(item["body"])
    with tenant.db() as c:
        c.execute("UPDATE webinars SET page=?, page_qid=?, status=CASE WHEN status='draft' THEN 'open' ELSE status END, "
                  "updated=? WHERE id=?", (db.jdump(page), item["id"], db.now(), wid))


@queue.on_approve("webinar_template")
def _template_approved(tenant, item: dict, actor: str) -> None:
    wid, key = item["meta"].get("webinar_id"), item["meta"].get("template_key")
    if not wid or key not in TPL:
        return
    with tenant.db() as c:
        c.execute("UPDATE webinar_templates SET body=?, status='approved', approved_by=?, approved_at=? "
                  "WHERE webinar_id=? AND key=?", (item["body"], actor, db.now(), wid, key))
    fan_out_or_job(tenant, wid, key, actor)


FAN_INLINE_MAX = 50  # bigger lists fan out in the background job queue so an approval click stays fast


def fan_out_or_job(tenant, wid: int, key: str, actor: str = "system", registrant_ids: list[int] | None = None) -> list[int]:
    with tenant.db() as c:
        n = c.execute("SELECT COUNT(*) FROM webinar_registrants WHERE webinar_id=? AND consent=1 AND unsubscribed=0",
                      (wid,)).fetchone()[0]
    if registrant_ids is not None:
        n = min(n, len(registrant_ids))
    if n > FAN_INLINE_MAX:
        jobs.enqueue(tenant, "webinars.fan_out", {"webinar_id": wid, "key": key, "actor": actor,
                                                  "registrant_ids": registrant_ids},
                     key=f"wfan:{wid}:{key}:{secrets.token_hex(4)}")
        return []
    return fan_out(tenant, wid, key, actor, registrant_ids)


@jobs.handler("webinars.fan_out")
def _job_fan_out(tenant, payload: dict) -> dict:
    ids = fan_out(tenant, int(payload["webinar_id"]), str(payload["key"]), str(payload.get("actor") or "system"),
                  payload.get("registrant_ids"))
    return {"queued": len(ids)}


def _template(tenant, wid: int, key: str) -> dict | None:
    with tenant.db() as c:
        return db.row(c.execute("SELECT * FROM webinar_templates WHERE webinar_id=? AND key=?", (wid, key)).fetchone())


def _absolute(link: str) -> str:
    """Links in emails must be complete; without OPERATOR_PUBLIC_URL the address part is a visible placeholder."""
    return link if link.startswith("http") else "[your workspace address]" + link


def with_unsubscribe(body: str) -> str:
    """Every per-registrant email carries the unsubscribe line, even if it was edited out of the template."""
    return body if "{unsubscribe_link}" in body else body.rstrip() + "\n" + UNSUB_LINE


def render(body: str, w: dict, reg: dict, tenant) -> str:
    fields = {"first_name": _first(reg["name"]), "title": w["title"], "when": when_label(w["starts_at"], w["tz"]),
              "join_link": w.get("join_url") or "(the join link will follow)",
              "replay_link": w.get("replay_url") or "(the replay link will follow)",
              "unsubscribe_link": _absolute(f"{page_url(tenant, w)}/unsubscribe?r={reg.get('token') or ''}")}
    out = body
    for k, v in fields.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def _send_after(w: dict, key: str, registered_at: float) -> float:
    _k, phase, off, _l, _b = TPL[key]
    if off is None:
        return registered_at
    return (float(w["starts_at"]) if phase == "showup" else _end(w)) + off


HOLD_SLACK = 60  # a send due within a minute goes now


def queue_holds_timed() -> bool:
    """True once the delivery feature makes queue.approve hold items whose meta.send_after is still in the future
    (status "scheduled"). Until then an approved email would go out the moment it is approved, so timed sends wait
    here (webinar_sends.due_at) and webinars.tick creates and approves each one when it is due."""
    return "scheduled" in getattr(queue, "STATUSES", ())


def _fan_one(tenant, w: dict, reg: dict, tpl: dict, actor: str, now: float) -> int | None:
    key = tpl["key"]
    if reg.get("unsubscribed") or not reg.get("consent") or w["status"] == "cancelled":
        return None
    send_after = _send_after(w, key, max(reg.get("registered_at") or now, now) if key == "confirm" else now)
    if TPL[key][1] == "showup" and key != "confirm" and send_after < now - SHOWUP_GRACE:
        return None  # e.g. the one-hour reminder for someone who registered ten minutes before the start
    with tenant.db() as c:
        claimed = c.execute("INSERT OR IGNORE INTO webinar_sends(webinar_id,registrant_id,template_key,created,due_at) "
                            "VALUES(?,?,?,?,?)", (w["id"], reg["id"], key, now, send_after)).rowcount
    if not claimed:
        return None
    if send_after > now + HOLD_SLACK and not queue_holds_timed():
        return None  # held here until it is due (webinars.tick); nothing exists to send early
    return _create_send(tenant, w, reg, tpl, actor, send_after)


def _create_send(tenant, w: dict, reg: dict, tpl: dict, actor: str, send_after: float, run_id: int | None = None) -> int:
    key = tpl["key"]
    label = TPL[key][3]
    meta = {"webinar_id": w["id"], "registrant_id": reg["id"], "template_key": key, "client_asset": True,
            "prior_contact": True, "send_after": send_after, "cancel_if": ["unsubscribed"],
            "from_template": tpl.get("queue_id")}
    if w.get("owner_user_id"):
        meta["owner_user_id"] = w["owner_user_id"]
    if TPL[key][1] == "showup" and key != "confirm":
        # a show-up reminder is time-bound: delivery's dispatcher cancels it instead of sending it late
        meta["send_before"] = send_after + SHOWUP_GRACE
    subject = next((l.split(":", 1)[1].strip() for l in tpl["body"].splitlines() if l.lower().startswith("subject:")), None)
    body = render(with_unsubscribe(tpl["body"]), w, reg, tenant)
    if subject:
        meta["subject"] = render(subject, w, reg, tenant)
        body = "\n".join(l for l in body.splitlines() if not l.lower().startswith("subject:"))
    item = queue.add(tenant, "webinar_email", body, channel="email", recipient_ref=reg["email"],
                     title=f"[{label}] {w['title']} → {_first(reg['name'])}"[:200], meta=meta, actor=actor,
                     demo=bool(w.get("demo")) or tenant.is_demo, run_id=run_id)
    with tenant.db() as c:
        c.execute("UPDATE webinar_sends SET queue_id=? WHERE webinar_id=? AND registrant_id=? AND template_key=?",
                  (item["id"], w["id"], reg["id"], key))
    if item["status"] == "pending":  # clean: approved on the template's approval. Anything flagged waits for a person.
        try:
            queue.approve(tenant, item["id"], f"{actor} via approved template #{tpl.get('queue_id')}", role="owner")
        except queue.QueueError as e:
            log.info("template fan-out left item %s open: %s", item["id"], redact(e))
    return item["id"]


def fan_out(tenant, wid: int, key: str, actor: str = "system", registrant_ids: list[int] | None = None) -> list[int]:
    w = get(tenant, wid)
    tpl = _template(tenant, wid, key)
    if w is None or tpl is None or tpl["status"] != "approved" or w["status"] == "cancelled":
        return []
    phase = TPL[key][1]
    if phase == "replay" and not w.get("post_event_at"):
        return []  # replays go out after the event, to people who didn't see the offer
    q = "SELECT * FROM webinar_registrants WHERE webinar_id=? AND consent=1 AND unsubscribed=0"
    if phase == "replay":
        q += " AND stayed_past_offer=0"
    with tenant.db() as c:
        regs = db.rows(c.execute(q, (wid,)))
    now = db.now()
    out = []
    for reg in regs:
        if registrant_ids is not None and reg["id"] not in registrant_ids:
            continue
        qid = _fan_one(tenant, w, reg, tpl, actor, now)
        if qid:
            out.append(qid)
    return out


def release_due(tenant, now: float | None = None, limit: int = 500) -> list[int]:
    """Timed sends held here (see queue_holds_timed) whose time has come: create each per-registrant item and approve
    it on the template's behalf, re-checking everything first (template still approved, webinar not cancelled,
    registrant still subscribed and consenting, a reminder not hours stale, a replay not for someone who saw the offer).
    Runs as one traced run per tick that has something due."""
    now = now or time.time()
    with tenant.db() as c:
        rows = db.rows(c.execute("SELECT * FROM webinar_sends WHERE queue_id IS NULL AND due_at IS NOT NULL AND due_at<=? "
                                 "ORDER BY due_at LIMIT ?", (now + HOLD_SLACK, limit)))
    if not rows:
        return []

    def fn(run: runtime.Run) -> list[int]:
        out: list[int] = []
        dropped = 0
        webs: dict[int, dict | None] = {}
        for s in rows:
            wid, key = s["webinar_id"], s["template_key"]
            if wid not in webs:
                webs[wid] = get(tenant, wid)
            w = webs[wid]
            tpl = _template(tenant, wid, key)
            with tenant.db() as c:
                reg = db.row(c.execute("SELECT * FROM webinar_registrants WHERE id=?", (s["registrant_id"],)).fetchone())
            phase = TPL[key][1] if key in TPL else None
            ok = (w is not None and tpl is not None and reg is not None and phase is not None
                  and tpl["status"] == "approved" and w["status"] != "cancelled" and reg["consent"] and not reg["unsubscribed"]
                  and not (phase == "showup" and key != "confirm" and s["due_at"] < now - SHOWUP_GRACE)
                  and not (phase == "replay" and reg["stayed_past_offer"]))
            with tenant.db() as c:
                if not ok:
                    dropped += c.execute("DELETE FROM webinar_sends WHERE id=? AND queue_id IS NULL", (s["id"],)).rowcount
                    continue
                # claim the row first so two ticks can never create the same email twice
                if not c.execute("UPDATE webinar_sends SET queue_id=-1 WHERE id=? AND queue_id IS NULL", (s["id"],)).rowcount:
                    continue
            try:
                out.append(_create_send(tenant, w, reg, tpl, "system(webinar schedule)", s["due_at"], run.id))
            except Exception as e:  # noqa: BLE001 - give the row back so the next tick tries again
                with tenant.db() as c:
                    c.execute("UPDATE webinar_sends SET queue_id=NULL WHERE id=? AND queue_id=-1", (s["id"],))
                log.warning("scheduled webinar email %s failed: %s", s["id"], redact(e))
        run.note("release", {"due": len(rows)}, {"queued": out, "dropped": dropped},
                 label="Emails whose time has come (approved templates)")
        return out

    return runtime.execute(tenant, "webinars", "Scheduled webinar emails", ["Emails whose time has come (approved templates)"],
                           fn, "system(webinar schedule)")["result"]


def _cancel_item(tenant, it: dict, actor: str, reason: str) -> bool:
    """Withdraw one per-registrant email that hasn't gone out. Open items are rejected; items the delivery feature
    holds (scheduled, waiting for a manual send, failed) are cancelled through it."""
    if it["status"] in queue.OPEN:
        try:
            queue.reject(tenant, it["id"], actor, reason=reason)
            return True
        except queue.QueueError:
            return False
    if it["status"] not in ("scheduled", "awaiting_manual_send", "send_failed", "awaiting_send"):
        return False  # already sent, approved, rejected or cancelled
    fn = getattr(queue, "cancel_system", None)
    if fn is not None:
        try:
            return bool(fn(tenant, it["id"], reason, actor))
        except Exception as e:  # noqa: BLE001 - fall back to the direct status change below
            log.info("cancel_system failed for %s: %s", it["id"], redact(e))
    try:
        from . import delivery

        dfn = getattr(delivery, "cancel", None)
        if dfn is not None:
            dfn(tenant, it["id"], reason)
            return True
    except ImportError:
        pass
    with tenant.db() as c:
        n = c.execute("UPDATE queue SET status='rejected', decided_by=?, decided_at=? WHERE id=? AND status=?",
                      (actor, db.now(), it["id"], it["status"])).rowcount
    if n:
        audit.log(tenant, actor, "queue.reject", {"id": it["id"], "reason": reason})
    return bool(n)


def _cancel_sends(tenant, wid: int, registrant_id: int | None, actor: str, reason: str) -> int:
    where, args = "webinar_id=?", [wid]
    if registrant_id is not None:
        where += " AND registrant_id=?"
        args.append(registrant_id)
    with tenant.db() as c:
        held = c.execute(f"DELETE FROM webinar_sends WHERE {where} AND queue_id IS NULL", args).rowcount
        qids = [r["queue_id"] for r in c.execute(f"SELECT queue_id FROM webinar_sends WHERE {where} AND queue_id>0", args)]
    n = held
    for qid in qids:
        it = queue.get(tenant, qid, scrub=False)
        if it is not None and _cancel_item(tenant, it, actor, reason):
            n += 1
    return n


def _refresh_unsent(tenant, wid: int, actor: str) -> int:
    """The time, time zone or a link changed: every email that hasn't gone out yet is withdrawn and drafted again
    with the new details (and the new send time). Emails already sent are left alone."""
    with tenant.db() as c:
        rows = db.rows(c.execute("SELECT * FROM webinar_sends WHERE webinar_id=?", (wid,)))
    redo = []
    for s in rows:
        if s["queue_id"] is None:
            redo.append(s)
        elif s["queue_id"] > 0:
            it = queue.get(tenant, s["queue_id"], scrub=False)
            if it is not None and _cancel_item(tenant, it, actor, "the webinar's details changed; redrafted"):
                redo.append(s)
    if not redo:
        return 0
    with tenant.db() as c:
        c.execute(f"DELETE FROM webinar_sends WHERE id IN ({','.join('?' * len(redo))})", [s["id"] for s in redo])
    w = get(tenant, wid)
    now = db.now()
    n = 0
    for s in redo:
        tpl = _template(tenant, wid, s["template_key"])
        with tenant.db() as c:
            reg = db.row(c.execute("SELECT * FROM webinar_registrants WHERE id=?", (s["registrant_id"],)).fetchone())
        if w is None or tpl is None or reg is None or tpl["status"] != "approved" or s["template_key"] not in TPL:
            continue
        if TPL[s["template_key"]][1] == "replay" and reg["stayed_past_offer"]:
            continue
        _fan_one(tenant, w, reg, tpl, actor, now)
        n += 1
    return n


# --------------------------------------------------------------------------- registration (public)
_EMAIL = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_rl_lock = threading.Lock()
_rl: dict[tuple[str, str], list[float]] = {}
RATE_N, RATE_WINDOW = 5, 600


def rate_limited(tid: str, ip: str, bucket: str = "reg", n: int = RATE_N, window: int = RATE_WINDOW) -> bool:
    now = time.time()
    key = (f"{tid}:{bucket}", ip or "?")
    with _rl_lock:
        hits = [t for t in _rl.get(key, []) if t > now - window]
        if len(hits) >= n:
            _rl[key] = hits
            return True
        hits.append(now)
        _rl[key] = hits
        if len(_rl) > 5000:  # keep memory bounded
            for k in list(_rl)[:1000]:
                _rl.pop(k, None)
    return False


def page_view(tenant, slug: str) -> dict | None:
    w = by_slug(tenant, slug)
    if w is None or w["status"] == "draft" or not w.get("page"):
        return None
    with tenant.db() as c:
        c.execute("UPDATE webinars SET visits=visits+1 WHERE id=?", (w["id"],))
    now = time.time()
    return {**w, "when": when_label(w["starts_at"], w["tz"]),
            "closed": w["status"] in ("done", "cancelled") or now >= _end(w)}


def register(tenant, slug: str, name, email, consent, honeypot="", source="", ip: str = "", now: float | None = None) -> dict:
    w = by_slug(tenant, slug)
    if w is None or w["status"] == "draft" or not w.get("page"):
        raise WebinarError(404, "Not found")
    now = now or time.time()
    if w["status"] in ("done", "cancelled") or now >= _end(w):
        raise WebinarError(409, "Registration for this training has closed.")
    if honeypot:  # bots fill the hidden field: say thanks, store nothing
        return {"ok": True, "message": "You're registered. Check your inbox for the details."}
    if rate_limited(tenant.id, ip):
        raise WebinarError(429, "Too many sign-ups from this connection. Please try again in a few minutes.")
    name = " ".join(str(name or "").split())
    email = str(email or "").strip()
    if not 1 <= len(name) <= 120:
        raise WebinarError(422, "Please add your name")
    if len(email) > 200 or not _EMAIL.match(email):
        raise WebinarError(422, "Please add a valid email address")
    if consent is not True:
        raise WebinarError(422, "Please tick the box to agree to emails about this training")
    source = re.sub(r"[^a-z0-9_\-]", "", str(source or "direct").lower())[:40] or "direct"
    with tenant.db() as c:
        cur = c.execute("INSERT OR IGNORE INTO webinar_registrants(webinar_id,name,email,source,consent,registered_at,token,demo) "
                        "VALUES(?,?,?,?,?,?,?,?)", (w["id"], name, email.lower(), source, 1, now, secrets.token_urlsafe(16),
                                                    1 if tenant.is_demo else 0))
        new = cur.rowcount == 1
        reg = db.row(c.execute("SELECT * FROM webinar_registrants WHERE webinar_id=? AND email=?",
                               (w["id"], email.lower())).fetchone())
    if new:
        with tenant.db() as c:
            tpls = db.rows(c.execute("SELECT * FROM webinar_templates WHERE webinar_id=? AND phase='showup' AND status='approved'",
                                     (w["id"],)))
        for t in tpls:
            _fan_one(tenant, w, reg, t, "system(registration)", db.now())
        audit.log(tenant, "public(registration)", "consent", {"webinar_id": w["id"], "registrant_id": reg["id"],
                                                               "source": source})
    # the same answer whether or not the address was already registered, so the form can't be used to find out
    # who signed up
    return {"ok": True, "message": "You're registered. Check your inbox for the details.",
            "when": when_label(w["starts_at"], w["tz"]), "join_url": w.get("join_url") or None}


def unsubscribe(tenant, slug: str, token: str) -> dict:
    w = by_slug(tenant, slug)
    if w is None or not token or len(token) > 80:
        raise WebinarError(404, "Not found")
    with tenant.db() as c:
        reg = db.row(c.execute("SELECT * FROM webinar_registrants WHERE webinar_id=? AND token=?", (w["id"], token)).fetchone())
        if reg is None:
            raise WebinarError(404, "Not found")
        c.execute("UPDATE webinar_registrants SET unsubscribed=1 WHERE id=?", (reg["id"],))
    n = _cancel_sends(tenant, w["id"], reg["id"], "public(unsubscribe)", "registrant unsubscribed")
    try:
        from . import delivery

        fn = getattr(delivery, "mark_unsubscribed", None)
        if fn is not None:
            fn(tenant, "email", reg["email"])
        elif getattr(delivery, "suppress", None) is not None:
            delivery.suppress(tenant, reg["email"], "email", "unsubscribed from webinar emails")
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001 - the registrant is unsubscribed here whatever the other feature does
        log.warning("delivery unsubscribe hook failed: %s", redact(e))
    audit.log(tenant, "public(unsubscribe)", "consent", {"webinar_id": w["id"], "registrant_id": reg["id"],
                                                          "unsubscribed": True, "cancelled": n})
    return {"ok": True, "cancelled": n}


# --------------------------------------------------------------------------- registrants and attendance
def registrants(tenant, wid: int, limit: int = 1000) -> list[dict]:
    with tenant.db() as c:
        rows = db.rows(c.execute("SELECT id,webinar_id,name,email,source,consent,registered_at,attended,minutes_watched,"
                                 "stayed_past_offer,booked,unsubscribed,post_done,demo FROM webinar_registrants "
                                 "WHERE webinar_id=? ORDER BY registered_at DESC, id DESC LIMIT ?", (wid, limit)))
    return rows


def add_registrant(tenant, wid: int, name: str, email: str, consent: bool, source: str, actor: str) -> dict:
    w = get(tenant, wid)
    if w is None:
        raise WebinarError(404, "Webinar not found")
    if consent is not True:
        raise WebinarError(422, "Only add people who agreed to get emails about this training")
    name = " ".join(str(name or "").split())
    email = str(email or "").strip().lower()
    if not 1 <= len(name) <= 120 or not _EMAIL.match(email):
        raise WebinarError(422, "Add a name and a valid email address")
    now = db.now()
    with tenant.db() as c:
        cur = c.execute("INSERT OR IGNORE INTO webinar_registrants(webinar_id,name,email,source,consent,registered_at,token,demo) "
                        "VALUES(?,?,?,?,?,?,?,?)", (wid, name, email, (source or "manual")[:40], 1, now,
                                                    secrets.token_urlsafe(16), 1 if tenant.is_demo else 0))
        if cur.rowcount != 1:
            raise WebinarError(409, "That email is already registered")
        reg = db.row(c.execute("SELECT * FROM webinar_registrants WHERE id=?", (cur.lastrowid,)).fetchone())
        tpls = db.rows(c.execute("SELECT * FROM webinar_templates WHERE webinar_id=? AND phase='showup' AND status='approved'",
                                 (wid,)))
    for t in tpls:
        _fan_one(tenant, w, reg, t, actor, now)
    audit.log(tenant, actor, "consent", {"webinar_id": wid, "registrant_id": reg["id"], "source": "manual"})
    return reg


def set_attendance(tenant, wid: int, rid: int, patch: dict, actor: str) -> dict:
    w = get(tenant, wid)
    if w is None:
        raise WebinarError(404, "Webinar not found")
    sets = {}
    for k in ("attended", "stayed_past_offer", "booked"):
        if patch.get(k) is not None:
            sets[k] = 1 if patch[k] else 0
    if patch.get("minutes_watched") is not None:
        m = float(patch["minutes_watched"])
        if not 0 <= m <= 600:
            raise WebinarError(422, "Minutes watched must be between 0 and 600")
        sets["minutes_watched"] = m
        if patch.get("stayed_past_offer") is None:
            sets["stayed_past_offer"] = 1 if m >= int(w["offer_at_min"] or 35) else 0
        if patch.get("attended") is None:
            sets["attended"] = 1 if m > 0 else 0
    if sets.get("stayed_past_offer"):
        sets["attended"] = 1
    if not sets:
        raise WebinarError(422, "Nothing to change")
    with tenant.db() as c:
        n = c.execute(f"UPDATE webinar_registrants SET {', '.join(f'{k}=?' for k in sets)} WHERE id=? AND webinar_id=?",
                      (*sets.values(), rid, wid)).rowcount
    if not n:
        raise WebinarError(404, "Registrant not found")
    audit.log(tenant, actor, "action", {"webinar_attendance": wid, "registrant_id": rid, "fields": sorted(sets)})
    return {"ok": True}


_COLS = {
    "email": [r"e-?mail"],
    "name": [r"^name", r"user name", r"^first name", r"participant"],
    "minutes": [r"duration", r"time in session", r"minutes", r"watch"],
    "attended": [r"^attended$", r"attended"],
    "leave": [r"leave time", r"^left"],
}


def parse_attendance(text: str) -> list[dict]:
    text = (text or "").lstrip("﻿")
    lines = text.splitlines()
    # Zoom reports start with a summary block; the attendee table begins at the first header with an email column
    start = next((i for i, l in enumerate(lines) if re.search(r"e-?mail", l, re.I) and ("," in l or "\t" in l)), None)
    if start is None:
        raise WebinarError(422, "Couldn't find an email column. Export the attendee report as CSV from your webinar tool.")
    body = "\n".join(lines[start:])
    dialect = "excel-tab" if "\t" in lines[start] else "excel"
    try:
        rows = list(csv.reader(io.StringIO(body), dialect=dialect))
    except csv.Error:
        raise WebinarError(422, "That file couldn't be read as a CSV (a cell is too long or broken). Export it again.")
    header = [h.strip().lower() for h in rows[0]]
    idx: dict[str, int] = {}
    for key, pats in _COLS.items():
        for p in pats:
            hit = next((i for i, h in enumerate(header) if re.search(p, h) and i not in idx.values()), None)
            if hit is not None:
                idx[key] = hit
                break
    out: dict[str, dict] = {}
    for r in rows[1:]:
        if not any(x.strip() for x in r):
            continue
        g = lambda k: r[idx[k]].strip() if k in idx and idx[k] < len(r) else ""
        email = g("email").lower()
        if not _EMAIL.match(email):
            continue
        try:
            mins = float(re.sub(r"[^\d.]", "", g("minutes")) or 0)
        except ValueError:
            mins = 0.0
        att = g("attended").lower()
        attended = (att in ("yes", "y", "true", "1")) if att else mins > 0
        e = out.setdefault(email, {"email": email, "name": g("name") or email.split("@")[0], "minutes": 0.0,
                                   "attended": False, "leave": None})
        e["minutes"] += mins  # people who rejoin appear on several rows
        e["attended"] = e["attended"] or attended
        if g("leave"):
            e["leave"] = g("leave")
    if not out:
        raise WebinarError(422, "No attendee rows with an email address were found")
    return list(out.values())


def _leave_ts(value: str | None, tz: str) -> float | None:
    if not value:
        return None
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(value.strip(), fmt).replace(tzinfo=ZoneInfo(tz or "UTC")).timestamp()
        except (ValueError, ZoneInfoNotFoundError):
            continue
    return None


def import_attendance(tenant, wid: int, text: str, actor: str = "system", now: float | None = None) -> dict:
    w = get(tenant, wid)
    if w is None:
        raise WebinarError(404, "Webinar not found")
    rows = parse_attendance(text)
    offer_ts = float(w["starts_at"]) + 60 * int(w["offer_at_min"] or 35)
    matched = added = 0
    for r in rows:
        leave = _leave_ts(r["leave"], w["tz"])
        if leave is not None and not (float(w["starts_at"]) - 43200 <= leave <= _end(w) + 43200):
            leave = None  # a leave time from another day (or a misread format) can't be trusted; use minutes instead
        stayed = (leave >= offer_ts) if leave is not None else r["minutes"] >= int(w["offer_at_min"] or 35)
        stayed = bool(stayed and r["attended"])
        with tenant.db() as c:
            n = c.execute("UPDATE webinar_registrants SET attended=?, minutes_watched=?, stayed_past_offer=? "
                          "WHERE webinar_id=? AND email=?", (1 if r["attended"] else 0, r["minutes"], 1 if stayed else 0,
                                                             wid, r["email"])).rowcount
            if n:
                matched += 1
            else:  # attended without registering here: counted, but never emailed (no consent on file)
                c.execute("INSERT OR IGNORE INTO webinar_registrants(webinar_id,name,email,source,consent,registered_at,"
                          "attended,minutes_watched,stayed_past_offer,token,demo) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          (wid, r["name"][:120], r["email"], "attendance_import", 0, w["starts_at"],
                           1 if r["attended"] else 0, r["minutes"], 1 if stayed else 0, secrets.token_urlsafe(16),
                           1 if tenant.is_demo else 0))
                added += 1
    audit.log(tenant, actor, "action", {"webinar_attendance_import": wid, "rows": len(rows), "matched": matched,
                                        "added_without_consent": added})
    res = {"rows": len(rows), "matched": matched, "added_without_consent": added, **rates(tenant, wid)}
    now = now or time.time()
    if now >= _end(w) and w["status"] not in ("cancelled",):
        res["post_event"] = post_event(tenant, wid, actor, now=now)
    return res


# --------------------------------------------------------------------------- after the event
def post_event(tenant, wid: int, actor: str = "system", now: float | None = None) -> dict:
    """Setter follow-up drafts for people who stayed past the offer; the approved replay sequence for everyone
    else. Each registrant is handled once."""
    w = get(tenant, wid)
    if w is None:
        raise WebinarError(404, "Webinar not found")
    now = now or time.time()
    if w["status"] == "cancelled":
        raise WebinarError(409, "This webinar was cancelled")
    if now < _end(w):
        raise WebinarError(409, "The webinar hasn't finished yet")

    def fn(run: runtime.Run):
        with tenant.db() as c:
            c.execute("UPDATE webinars SET status='done', post_event_at=COALESCE(post_event_at, ?), updated=? WHERE id=?",
                      (now, now, wid))
            regs = db.rows(c.execute("SELECT * FROM webinar_registrants WHERE webinar_id=? AND post_done=0", (wid,)))
            # attendance that arrived after the follow-up ran: people now known to have stayed for the offer and who
            # have no setter follow-up yet get one, and their replay emails that haven't gone out are withdrawn
            late = db.rows(c.execute(
                "SELECT * FROM webinar_registrants r WHERE webinar_id=? AND post_done=1 AND stayed_past_offer=1 AND consent=1 "
                "AND unsubscribed=0 AND NOT EXISTS (SELECT 1 FROM queue q WHERE q.kind='followup' AND "
                "json_extract(q.meta,'$.webinar_id')=r.webinar_id AND json_extract(q.meta,'$.registrant_id')=r.id)", (wid,)))
        w2 = get(tenant, wid)
        drafts, replay_for = [], []
        for reg in regs + late:
            if reg["stayed_past_offer"] and reg["consent"] and not reg["unsubscribed"]:
                meta = {"prospect_facing": True, "lead_tier": "warm", "prior_contact": True, "webinar_id": wid,
                        "registrant_id": reg["id"], "source": "webinar"}
                if w2.get("owner_user_id"):
                    meta["owner_user_id"] = w2["owner_user_id"]
                body = SETTER_FOLLOWUP.replace("{first_name}", _first(reg["name"])).replace("{title}", w2["title"])
                it = queue.add(tenant, "followup", body, channel="email", recipient_ref=reg["email"],
                               title=f"Setter follow-up: {_first(reg['name'])} stayed for the offer",
                               meta={**meta, "subject": f"Your question about {w2['title']}"[:120]}, run_id=run.id,
                               actor=actor, demo=bool(w2.get("demo")) or tenant.is_demo)
                drafts.append(it["id"])
                if reg["post_done"]:
                    _cancel_replays(tenant, wid, reg["id"], actor)
            elif not reg["stayed_past_offer"]:
                replay_for.append(reg["id"])
        run.note("followups", {}, {"setter_drafts": drafts}, label="Setter follow-ups (for approval)")
        sent = []
        for key, phase, *_ in TEMPLATES:
            if phase == "replay":
                sent += fan_out_or_job(tenant, wid, key, actor, registrant_ids=replay_for)
        run.note("replays", {}, {"queued": len(sent)}, label="Replay sequence (approved templates)")
        with tenant.db() as c:
            if regs:
                c.execute(f"UPDATE webinar_registrants SET post_done=1 WHERE id IN ({','.join('?' * len(regs))})",
                          [r["id"] for r in regs])
        r = rates(tenant, wid)
        if w2.get("client_id"):
            try:
                from . import client_ops

                client_ops.record_metrics(tenant, int(w2["client_id"]), diagnostic_metrics(r), w2["starts_at"],
                                          source="webinar", note=w2["title"][:100], actor=actor)
            except Exception as e:  # noqa: BLE001 - the follow-ups above still stand
                log.warning("webinar metrics to client failed: %s", redact(e))
        return {"webinar_id": wid, "setter_drafts": drafts, "replay_items": sent, "rates": r}

    out = runtime.execute(tenant, "webinars", f"After the webinar: {w['title']}",
                          ["Setter follow-ups (for approval)", "Replay sequence (approved templates)"], fn, actor)
    return {**out["result"], "run_id": out["run_id"]}


def _cancel_replays(tenant, wid: int, registrant_id: int, actor: str) -> int:
    """Withdraw replay emails that haven't gone out for someone who, it turns out, stayed for the offer."""
    keys = [k for k, phase, *_ in TEMPLATES if phase == "replay"]
    marks = ",".join("?" * len(keys))
    with tenant.db() as c:
        n = c.execute(f"DELETE FROM webinar_sends WHERE webinar_id=? AND registrant_id=? AND queue_id IS NULL "
                      f"AND template_key IN ({marks})", (wid, registrant_id, *keys)).rowcount
        qids = [r["queue_id"] for r in c.execute(
            f"SELECT queue_id FROM webinar_sends WHERE webinar_id=? AND registrant_id=? AND queue_id>0 "
            f"AND template_key IN ({marks})", (wid, registrant_id, *keys))]
    for qid in qids:
        it = queue.get(tenant, qid, scrub=False)
        if it is not None and _cancel_item(tenant, it, actor, "they stayed for the offer: a setter follows up instead"):
            n += 1
    return n


def tick(tenant, now: float | None = None) -> dict:
    """Per-tenant tick: close registration when an event ends, then follow up once the attendance window passes."""
    now = now or time.time()
    released = release_due(tenant, now)
    with tenant.db() as c:
        rows = [_row(r) for r in c.execute("SELECT * FROM webinars WHERE status IN ('open','ended')")]
    done = []
    for w in rows:
        end = _end(w)
        if now < end:
            continue
        if w["status"] == "open":
            with tenant.db() as c:
                c.execute("UPDATE webinars SET status='ended', updated=? WHERE id=?", (now, w["id"]))
        if now >= end + POST_EVENT_GRACE:
            try:
                post_event(tenant, w["id"], "system(tick)", now=now)
                done.append(w["id"])
            except Exception as e:  # noqa: BLE001 - one webinar must not stop the others
                log.warning("post-event failed for webinar %s: %s", w["id"], redact(e))
    return {"post_event": done, "released": released}


tick.demo_too = True


# --------------------------------------------------------------------------- rates (diagnostic, missions)
def rates(tenant, wid: int) -> dict:
    with tenant.db() as c:
        w = c.execute("SELECT visits FROM webinars WHERE id=?", (wid,)).fetchone()
        r = c.execute("SELECT COUNT(*) n, SUM(consent) consented, SUM(attended) att, SUM(stayed_past_offer) stayed, "
                      "SUM(booked) booked, SUM(unsubscribed) unsub FROM webinar_registrants WHERE webinar_id=? "
                      "AND source != 'attendance_import'", (wid,)).fetchone()
        walk_ins = c.execute("SELECT COUNT(*) FROM webinar_registrants WHERE webinar_id=? AND source='attendance_import'",
                             (wid,)).fetchone()[0]
    visits = int(w["visits"] or 0) if w else 0
    n, att, stayed, booked = int(r["n"] or 0), int(r["att"] or 0), int(r["stayed"] or 0), int(r["booked"] or 0)
    div = lambda a, b: round(a / b, 4) if b else None
    return {"visits": visits, "registrants": n, "consented": int(r["consented"] or 0), "attended": att,
            "stayed_past_offer": stayed, "booked": booked, "unsubscribed": int(r["unsub"] or 0),
            "attended_without_registering": walk_ins, "reg_rate": div(n, visits), "show_rate": div(att, n),
            "offer_reach": div(stayed, att), "booked_rate": div(booked, att), "definitions": RATE_DEFS}


def diagnostic_metrics(r: dict) -> dict:
    """The keys diagnostic.analyse reads for the webinar stages."""
    out = {"registrants": r["registrants"], "attendees": r["attended"]}
    if r.get("visits"):
        out["visitors"] = r["visits"]
    return out


def diagnose(tenant, wid: int, user: dict | None = None) -> dict:
    from . import diagnostic

    w = get(tenant, wid)
    if w is None:
        raise WebinarError(404, "Webinar not found")
    r = rates(tenant, wid)
    return diagnostic.diagnose(tenant, diagnostic_metrics(r), user=user, label=w["title"])


def funnel_metrics(tenant, webinar_id: int | None = None, owner_user_id: int | None = None) -> dict:
    """Registration -> show -> booked totals across webinars (or one), for missions and reports."""
    q, args = "SELECT id FROM webinars WHERE status != 'cancelled'", []
    if webinar_id is not None:
        q += " AND id=?"
        args.append(webinar_id)
    if owner_user_id is not None:
        q += " AND owner_user_id=?"
        args.append(owner_user_id)
    with tenant.db() as c:
        ids = [r["id"] for r in c.execute(q, args)]
    tot = {"webinars": len(ids), "visits": 0, "registrants": 0, "attended": 0, "stayed_past_offer": 0, "booked": 0}
    for i in ids:
        r = rates(tenant, i)
        for k in ("visits", "registrants", "attended", "stayed_past_offer", "booked"):
            tot[k] += r[k]
    div = lambda a, b: round(a / b, 4) if b else None
    tot.update(reg_rate=div(tot["registrants"], tot["visits"]), show_rate=div(tot["attended"], tot["registrants"]),
               booked_rate=div(tot["booked"], tot["attended"]))
    return tot


@events.on("booking_made")
def _booking_made(tenant, name: str, payload: dict) -> None:
    """A call booked by someone who registered for a webinar counts toward that webinar's booked rate."""
    table = {"lead": "leads", "prospect": "prospects"}.get(str(payload.get("party_type") or ""))
    pid = payload.get("party_id")
    if not table or not isinstance(pid, int):
        return
    try:
        with tenant.db() as c:
            r = c.execute(f"SELECT email FROM {table} WHERE id=?", (pid,)).fetchone()
            if r and r["email"]:
                c.execute("UPDATE webinar_registrants SET booked=1 WHERE email=? AND attended=1", (r["email"].lower(),))
    except Exception as e:  # noqa: BLE001
        log.warning("booking_made webinar link failed: %s", redact(e))


# --------------------------------------------------------------------------- list / detail
def list_all(tenant) -> list[dict]:
    with tenant.db() as c:
        rows = [_row(r) for r in c.execute("SELECT * FROM webinars ORDER BY starts_at DESC")]
    out = []
    for w in rows:
        out.append({"id": w["id"], "title": w["title"], "starts_at": w["starts_at"], "tz": w["tz"],
                    "when": when_label(w["starts_at"], w["tz"]), "status": w["status"],
                    "status_label": _display_status(w), "demo": w["demo"], "rates": rates(tenant, w["id"]),
                    "client_id": w["client_id"]})
    return out


def detail(tenant, wid: int) -> dict:
    w = get(tenant, wid)
    if w is None:
        raise WebinarError(404, "Webinar not found")
    with tenant.db() as c:
        tpls = db.rows(c.execute("SELECT * FROM webinar_templates WHERE webinar_id=? ORDER BY id", (wid,)))
        sends = {r["template_key"]: r["n"] for r in c.execute(
            "SELECT template_key, COUNT(*) n FROM webinar_sends WHERE webinar_id=? GROUP BY template_key", (wid,))}
        waiting = {r["template_key"]: r["n"] for r in c.execute(
            "SELECT template_key, COUNT(*) n FROM webinar_sends WHERE webinar_id=? AND queue_id IS NULL GROUP BY template_key",
            (wid,))}
    for t in tpls:
        it = queue.get(tenant, t["queue_id"]) if t["queue_id"] else None
        t.update(label=TPL[t["key"]][3] if t["key"] in TPL else t["key"], queue_status=it["status"] if it else None,
                 fanned_out=sends.get(t["key"], 0), waiting=waiting.get(t["key"], 0))
        off = TPL.get(t["key"], (None, None, None))[2]
        t["timing"] = ("At registration" if off is None else
                       ("When we go live" if off == 0 else
                        (f"{abs(off) // 3600} hour(s) {'before the start' if off < 0 else 'after the end'}" if abs(off) < 86400
                         else f"{abs(off) // 86400} day(s) {'before the start' if off < 0 else 'after the end'}")))
    page_item = queue.get(tenant, w["page_qid"]) if w.get("page_qid") else None
    return {**w, "when": when_label(w["starts_at"], w["tz"]), "status_label": _display_status(w),
            "public_url": page_url(tenant, w), "needs_public_url": not public_base(), "templates": tpls,
            "page_status": page_item["status"] if page_item else None, "registrants": registrants(tenant, wid, 500),
            "rates": rates(tenant, wid), "ends_at": _end(w)}


# --------------------------------------------------------------------------- demo
def seed_demo(tenant) -> None:
    """Demo only: one past webinar with registrants, attendance and the follow-ups it produced, and one upcoming
    webinar with its page live and the email templates waiting for approval."""
    from . import tenancy

    if not tenant.is_demo or not tenancy.is_demo_db(tenant):
        return
    with tenant.db() as c:
        if c.execute("SELECT COUNT(*) FROM webinars").fetchone()[0]:
            return
        owner = c.execute("SELECT id FROM users WHERE username='demo-owner'").fetchone()
    user = {"id": owner["id"] if owner else None, "username": "demo-owner", "role": "owner"}
    now = time.time()
    london = ZoneInfo("Europe/London")
    at_seven = lambda days: datetime.fromtimestamp(now + days * 86400, london).replace(
        hour=19, minute=0, second=0, microsecond=0).timestamp()
    names = ["Ava Lindqvist", "Noah Castillo", "Isla Brennan", "Ravi Okafor", "Mila Duarte", "Theo Varga"]
    # past event: four days ago, attendance in, setter follow-ups drafted. The replay emails still wait for the
    # owner, so approving one in the demo shows it fan out to everyone who missed the offer (nothing is sent here).
    past = create(tenant, {"title": "How growth operators win their first client (DEMO)", "starts_at": at_seven(-4),
                           "tz": "Europe/London", "join_url": "https://example.invalid/join/demo-past",
                           "replay_url": "https://example.invalid/replay/demo-past", "duration_min": 60,
                           "offer_at_min": 35}, user)
    keep_open = {past["template_queue_ids"][[t[0] for t in TEMPLATES].index(k)] for k in ("replay_1", "replay_2", "replay_3")}
    for qid in [past["page_queue_id"], *past["template_queue_ids"]]:
        if qid not in keep_open:
            queue.approve(tenant, qid, "demo-owner(owner)")
    wid = past["id"]
    with tenant.db() as c:
        for i, n in enumerate(names):
            mins = [62, 41, 12, 0, 55, 7][i]
            c.execute("INSERT INTO webinar_registrants(webinar_id,name,email,source,consent,registered_at,attended,"
                      "minutes_watched,stayed_past_offer,booked,token,demo) VALUES(?,?,?,?,?,?,?,?,?,?,?,1)",
                      (wid, f"{n} (DEMO)", n.lower().replace(" ", ".") + "@example.invalid",
                       ["paid_social", "email", "organic"][i % 3], 1, now - (10 - i % 7) * 86400, 1 if mins else 0, mins,
                       1 if mins >= 35 else 0, 1 if i in (0, 4) else 0, secrets.token_urlsafe(16)))
        c.execute("UPDATE webinars SET visits=? WHERE id=?", (19, wid))
    post_event(tenant, wid, "demo-owner(owner)", now=now)
    # upcoming event: page approved and live, templates waiting for the owner's approval
    nxt = create(tenant, {"title": "The weekly numbers every client report needs (DEMO)", "starts_at": at_seven(6),
                          "tz": "Europe/London", "join_url": "https://example.invalid/join/demo-next", "duration_min": 60,
                          "offer_at_min": 35}, user)
    queue.approve(tenant, nxt["page_queue_id"], "demo-owner(owner)")
    with tenant.db() as c:
        for i, n in enumerate(names[:3]):
            c.execute("INSERT INTO webinar_registrants(webinar_id,name,email,source,consent,registered_at,token,demo) "
                      "VALUES(?,?,?,?,?,?,?,1)", (nxt["id"], f"{n} (DEMO)", "next." + n.lower().replace(" ", ".")
                                                  + "@example.invalid", "email", 1, now - i * 3600, secrets.token_urlsafe(16)))
        c.execute("UPDATE webinars SET visits=? WHERE id=?", (9, nxt["id"]))
