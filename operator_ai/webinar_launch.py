"""One-click webinar launch: one brief in, the whole webinar built, scheduled and waiting on ONE approval.

A launch runs the webinar chain (offer, hooks, slide script, pages, show-up and replay sequences, run-of-show,
chat prompts, sales hand-off), creates and schedules the event, and queues a single "launch pack". Approving the
pack approves the registration page (so /w/<tenant>/<slug> goes live) and every show-up / replay email template
(so reminders fan out to each registrant against the start time, exactly as approving them one by one does).
The slide script is also available as a real .pptx deck.

Nothing new is sent by approving the pack: emails still go out only to people who register and consent, each at
its scheduled time. A student may launch for themselves or for one of their own clients, and sees only their own
launches.
"""
from __future__ import annotations

import io
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import brief as brief_mod
from . import config, db, queue, webinar_chain, webinars

KIND = "webinar_launch"
DEFAULT_TZ = "America/New_York"
DEFAULT_HOUR = 19


class LaunchError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


def _actor(user: dict | None) -> str:
    return f"{user['username']}({user['role']})" if user else "system"


def _is_owner(user: dict | None) -> bool:
    return not user or user.get("role") == "owner"


def _client(tenant, client_id: int, user: dict | None) -> dict:
    with tenant.db() as c:
        found = db.rows(c.execute("SELECT * FROM clients WHERE id=?", (int(client_id),)))
    row = found[0] if found else None
    if not row or (not _is_owner(user) and row.get("owner_user_id") not in (None, (user or {}).get("id"))):
        raise LaunchError(404, "Client not found")
    return row


def launch(tenant, data: dict, user: dict | None = None) -> dict:
    """Build, schedule and queue a whole webinar from one brief. Returns the launch summary."""
    if not data.get("starts_at"):
        raise LaunchError(422, "When is the webinar? Give a date and time.")
    tz = str(data.get("tz") or DEFAULT_TZ)
    partial: dict = {}
    client = None
    if data.get("client_id"):
        client = _client(tenant, int(data["client_id"]), user)
        partial.update({k: v for k, v in brief_mod.for_client(client)["brief"].items() if v not in (None, "")})
    partial.update({k: data[k] for k in brief_mod.FIELDS if data.get(k) not in (None, "")})
    try:
        starts = webinars.parse_start(data["starts_at"], tz)
    except webinars.WebinarError as e:
        raise LaunchError(e.status, str(e)) from None
    chain = webinar_chain.run_chain(tenant, partial, user)
    try:
        w = webinars.create(tenant, {"chain_id": chain["chain_id"], "starts_at": starts, "tz": tz,
                                     "title": data.get("title") or "", "client_id": client["id"] if client else None,
                                     "duration_min": data.get("duration_min") or 60}, user)
    except webinars.WebinarError as e:
        raise LaunchError(e.status, str(e)) from None
    web = webinars.get(tenant, w["id"])
    if web is None:
        raise LaunchError(500, "The webinar was not saved")
    when = webinars.when_label(web["starts_at"], web["tz"])
    n_tpl = len(w["template_queue_ids"])
    minutes = sum(m for _, m in webinar_chain.SEGMENTS)
    body = "\n".join([
        f"Approve once to launch \"{web['title']}\" on {when}.",
        "",
        "Approving this will:",
        "- Publish the registration page so people can sign up.",
        f"- Turn on {n_tpl} show-up and replay emails. Each registrant gets them at the right time before and after "
        "the event, and only if they agreed to emails.",
        f"- Keep the slide deck ready to download ({len(webinar_chain.SEGMENTS)} segments, about {minutes} minutes).",
        "",
        "Already drafted: offer, title and hooks, slide script, registration and thank-you pages, show-up and replay "
        "sequences, run-of-show, chat and offer-moment prompts, and the hand-off to sales.",
    ])
    meta = {"webinar_id": w["id"], "chain_id": chain["chain_id"], "page_qid": w["page_queue_id"],
            "template_qids": w["template_queue_ids"], "chain_qids": chain["queue_ids"],
            "client_id": client["id"] if client else None}
    if user and not _is_owner(user):
        meta["owner_user_id"] = user["id"]
    pack = queue.add(tenant, KIND, body, channel="manual", title=f"Launch pack: {web['title']}"[:200], meta=meta,
                     run_id=chain.get("run_id"), actor=_actor(user), demo=tenant.is_demo)
    return summary(tenant, w["id"], user, pack=pack, chain=chain)


def _pack_for(tenant, wid: int) -> dict | None:
    with tenant.db() as c:
        r = c.execute("SELECT id FROM queue WHERE kind=? AND json_extract(meta,'$.webinar_id')=? "
                      "ORDER BY id DESC LIMIT 1", (KIND, wid)).fetchone()
    return queue.get(tenant, r["id"]) if r else None


def _webinar(tenant, wid: int, user: dict | None) -> dict:
    w = webinars.get(tenant, wid)
    mine = _is_owner(user) or w is not None and w.get("owner_user_id") == (user or {}).get("id")
    if not w or not _pack_for(tenant, wid) or not mine:
        raise LaunchError(404, "Launch not found")
    return w


def summary(tenant, wid: int, user: dict | None = None, pack: dict | None = None, chain: dict | None = None) -> dict:
    w = _webinar(tenant, wid, user)
    pack = pack or _pack_for(tenant, wid)
    try:
        st = webinar_chain.state(tenant, w["chain_id"])
        assets = [{"step": s["step"], "label": s["label"], "body": ((s.get("output") or {}).get("body") or "")}
                  for s in st["steps"]]
    except KeyError:
        assets = []
    live = w.get("status") in ("open", "live", "done") and bool(w.get("page"))
    with tenant.db() as c:
        tpl = db.rows(c.execute("SELECT key,status FROM webinar_templates WHERE webinar_id=?", (wid,)))
        n_reg = c.execute("SELECT COUNT(*) FROM webinar_registrants WHERE webinar_id=?", (wid,)).fetchone()[0]
    return {"webinar_id": wid, "title": w["title"], "when": webinars.when_label(w["starts_at"], w["tz"]),
            "starts_at": w["starts_at"], "tz": w["tz"], "status": w.get("status"), "live": live,
            "page_url": webinars.page_url(tenant, w), "deck_url": f"/t/{tenant.id}/api/launches/{wid}/deck.pptx",
            "pack": {"id": pack["id"], "status": pack["status"], "body": pack["body"]} if pack else None,
            "emails": {"total": len(tpl), "on": sum(1 for t in tpl if t["status"] == "approved")},
            "registrants": n_reg, "assets": assets, "chain_id": w["chain_id"],
            "run_id": (chain or {}).get("run_id")}


def list_launches(tenant, user: dict | None = None) -> list[dict]:
    with tenant.db() as c:
        ids = [r[0] for r in c.execute("SELECT DISTINCT json_extract(meta,'$.webinar_id') FROM queue WHERE kind=? "
                                       "ORDER BY id DESC", (KIND,))]
    out = []
    for wid in ids:
        try:
            s = summary(tenant, int(wid), user)
        except LaunchError:
            continue
        s.pop("assets", None)
        out.append(s)
    return out


@queue.on_approve(KIND)
def _pack_approved(tenant, item: dict, actor: str) -> None:
    """Approve the page and every email template the pack stands for. Their own approve hooks do the work: the page
    goes live and the templates fan out per registrant. Guardrails still run on each one."""
    m = item["meta"]
    problems = []
    for qid in [m.get("page_qid"), *(m.get("template_qids") or [])]:
        if not qid:
            continue
        it = queue.get(tenant, int(qid), scrub=False)
        if not it or it["status"] not in queue.OPEN:
            continue
        try:
            queue.approve(tenant, int(qid), actor, role="owner")
        except queue.QueueError as e:
            problems.append(f"{it['title']}: {e}")
    if problems:
        raise queue.HookFailed("Launched, but these need a fix in Approvals: " + "; ".join(problems)[:400], status=409)


# --------------------------------------------------------------------------- slide deck
def deck(tenant, wid: int, user: dict | None = None) -> tuple[str, bytes]:
    """The slide script as a real PowerPoint deck: title, agenda, one slide per segment, the offer, next steps."""
    from pptx import Presentation
    from pptx.util import Inches, Pt

    w = _webinar(tenant, wid, user)
    st = webinar_chain.state(tenant, w["chain_id"])
    b = st.get("brief") or {}
    outs = {s["step"]: (s.get("output") or {}) for s in st["steps"]}
    hooks = ((outs.get("title_hooks") or {}).get("data") or {}).get("hooks") or []
    usp = ((outs.get("offer") or {}).get("data") or {}).get("usp") or ""
    script = (outs.get("slide_script") or {}).get("body") or ""
    notes = {}
    for line in script.splitlines():
        mm = re.match(r"Segment (\d+): .*?—\s*(.+)$", line)
        if mm:
            notes[int(mm.group(1))] = mm.group(2).strip()
    brand = config.get().brand
    prs = Presentation()  # the default 4:3 template keeps every placeholder where its layout expects it

    def slide(title: str, bullets: list[str], note: str = ""):
        s = prs.slides.add_slide(prs.slide_layouts[1])
        s.shapes.title.text = title
        tf = s.placeholders[1].text_frame
        tf.clear()
        for i, t in enumerate(bullets):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.text = t
            p.font.size = Pt(24)
        if note:
            s.notes_slide.notes_text_frame.text = note
        return s

    t = prs.slides.add_slide(prs.slide_layouts[0])
    t.shapes.title.text = w["title"]
    for p in t.shapes.title.text_frame.paragraphs:
        p.font.size = Pt(34)
    for shp, top, h in ((t.shapes.title, 1.2, 3.0), (t.placeholders[1], 4.6, 1.6)):  # set all four or width becomes 0
        shp.left, shp.top, shp.width, shp.height = Inches(0.75), Inches(top), Inches(8.5), Inches(h)
    t.placeholders[1].text = f"{brand} · Free live training · {webinars.when_label(w['starts_at'], w['tz'])}"
    slide("What you'll learn today", hooks[:4] or [usp], "Open with the promise. Keep it under a minute.")
    slide("Agenda", [f"{name} ({mins} min)" for name, mins in webinar_chain.SEGMENTS])
    for i, (name, mins) in enumerate(webinar_chain.SEGMENTS, start=1):
        point = notes.get(i, "")
        slide(f"{i}. {name}", [point or name, f"{mins} minutes"], f"Segment {i}: {name}, {mins} min. {point}".strip())
    slide("The offer", [(b.get("offer") or "").capitalize(), usp, f"Investment: ${float(b.get('price') or 0):,.0f}"],
          "Who it is for and who it is not for. Only verified results, with permission.")
    slide("Next step", ["Book a call to see if it's a fit", "Questions? Ask in the chat now"],
          "Stay on for live questions.")
    buf = io.BytesIO()
    prs.save(buf)
    name = re.sub(r"[^A-Za-z0-9]+", "-", w["title"]).strip("-")[:60] or "webinar"
    return f"{name}.pptx", buf.getvalue()


# --------------------------------------------------------------------------- chat: "launch a webinar ... on Nov 12"
LAUNCH_ASK = re.compile(r"\b(launch|run|set up|schedule|host)\b[^.?!\n]{0,40}\bwebinar\b", re.I)
_MON = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_MONTHS = {m: i for i, m in enumerate(_MON, 1)}
_DATE = re.compile(r"\b(?P<mon>" + "|".join(_MON) + r")[a-z]*\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?\b"
                   r"|\b(?P<iso>\d{4}-\d{2}-\d{2})\b", re.I)
_TIME = re.compile(r"\b(?:at\s+)?(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)\b", re.I)
_TZ = {"pt": "America/Los_Angeles", "pst": "America/Los_Angeles", "pdt": "America/Los_Angeles",
       "pacific": "America/Los_Angeles", "et": "America/New_York", "est": "America/New_York",
       "edt": "America/New_York", "eastern": "America/New_York",
       "ct": "America/Chicago", "cst": "America/Chicago", "central": "America/Chicago", "uk": "Europe/London",
       "gmt": "Europe/London", "bst": "Europe/London", "london": "Europe/London"}
_CLIENT = re.compile(r"\bfor (?:my |our )?(?P<what>[\w &'-]{2,60}?) client\b", re.I)


def wants_launch(text: str) -> bool:
    """A launch ask WITH a date runs the one-click launch; without one it stays a mission goal (its intake asks)."""
    t = text or ""
    return bool(LAUNCH_ASK.search(t)) and bool(_DATE.search(t) or re.search(r"\bnext week\b", t, re.I))


def parse_request(text: str, now: datetime | None = None) -> dict:
    """Pull the date, time, time zone and client hint out of a chat message. Missing date -> no starts_at."""
    tz = DEFAULT_TZ
    for word in re.findall(r"[a-z]+", (text or "").lower()):
        if word in _TZ:
            tz = _TZ[word]
            break
    try:
        zone = ZoneInfo(tz)
    except ZoneInfoNotFoundError:
        zone, tz = ZoneInfo("UTC"), "UTC"
    now = now or datetime.now(zone)
    out: dict = {"tz": tz}
    d = _DATE.search(text or "")
    if d:
        if d.group("iso"):
            day = datetime.fromisoformat(d.group("iso")).date()
        else:
            mon, dd = _MONTHS[d.group("mon").lower()[:3]], int(d.group("day"))
            try:
                day = now.replace(month=mon, day=dd).date()
            except ValueError:
                day = None
            if day and day < now.date():
                day = day.replace(year=day.year + 1)
        if day:
            hh, mi = DEFAULT_HOUR, 0
            tm = _TIME.search(text[d.end():] if d else text)
            if tm:
                hh = int(tm.group("h")) % 12 + (12 if tm.group("ap").lower() == "pm" else 0)
                mi = int(tm.group("m") or 0)
            out["starts_at"] = datetime(day.year, day.month, day.day, hh, mi).isoformat(timespec="minutes")
    elif re.search(r"\bnext week\b", text or "", re.I):
        day = (now + timedelta(days=7)).date()
        out["starts_at"] = datetime(day.year, day.month, day.day, DEFAULT_HOUR, 0).isoformat(timespec="minutes")
    cm = _CLIENT.search(text or "")
    if cm:
        out["client_hint"] = cm.group("what").strip()
    return out


def match_client(tenant, hint: str, user: dict | None) -> dict | None:
    if not hint:
        return None
    with tenant.db() as c:
        rows = db.rows(c.execute("SELECT * FROM clients ORDER BY id DESC"))
    if not _is_owner(user):
        rows = [r for r in rows if r.get("owner_user_id") == (user or {}).get("id")]
    words = [w for w in re.findall(r"[a-z]+", hint.lower()) if len(w) > 2]
    best, score = None, 0
    for r in rows:
        hay = f"{r.get('name', '')} {r.get('niche', '')} {r.get('offer', '')}".lower()
        s = sum(1 for w in words if w in hay)
        if s > score:
            best, score = r, s
    return best


def from_chat(tenant, text: str, user: dict) -> tuple[str, dict]:
    """Chat entry point. Returns (reply, meta)."""
    req = parse_request(text)
    if not req.get("starts_at"):
        return ("I can launch the whole webinar in one go: offer, hooks, slide deck, registration page and every "
                "reminder email. When is it? For example: 'Launch a webinar for my trading client on November 12 at "
                "7pm ET'."), {"launch": None}
    data = {"starts_at": req["starts_at"], "tz": req["tz"]}
    client = match_client(tenant, req.get("client_hint", ""), user)
    if client:
        data["client_id"] = client["id"]
    elif req.get("client_hint"):
        hint = req["client_hint"]
        data["niche"] = hint if hint.lower().endswith("s") else f"{hint} businesses"
    try:
        res = launch(tenant, data, user)
    except LaunchError as e:
        return str(e), {"launch": None}
    who = f" for {client['name']}" if client else ""
    n = res["emails"]["total"]
    reply = (f"Done. I built your webinar{who}: \"{res['title']}\" on {res['when']}. Offer, hooks, a "
             f"{len(webinar_chain.SEGMENTS)}-part slide deck, registration and thank-you pages, {n} "
             "show-up and replay emails, run-of-show, chat prompts and the sales hand-off are ready. Approve the "
             "launch pack once and the page goes live and the reminders switch on.")
    return reply, {"launch": res}
