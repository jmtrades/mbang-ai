"""One-sentence VSL funnel: brief in -> VSL script, VSL page, application page, thank-you/booking page and follow-ups,
waiting on ONE approval.

The copy and script are written by the AI in the programme's voice from its method notes when the AI is on, else from
templates. Approving the pack puts the three public pages live at /f/<tenant>/<slug> (VSL), .../apply and
.../thanks. The student pastes the recorded video link and their booking link whenever they have them. Applications
are stored for the funnel's owner. Nothing is sent to anyone by approving.
"""
from __future__ import annotations

import json
import re
import secrets
import time

from . import brief as brief_mod
from . import config, db, guardrails, llm, queue, webinar_launch

KIND = "funnel_launch"
QUESTIONS = [
    ("revenue", "What's your business doing per month right now?"),
    ("goal", "Where do you want it to be in 6 months?"),
    ("blocker", "What's the one thing holding you back?"),
    ("budget", "If we're a fit, are you ready to invest in getting there now?"),
]
SECTIONS = [("Hook", 1), ("The problem", 2), ("The big mistake", 2), ("The mechanism", 3), ("Proof", 2),
            ("The offer", 2), ("Who it's for (and not for)", 1), ("Call to action", 1)]
_SCHEMA = """
CREATE TABLE IF NOT EXISTS funnels (
  id INTEGER PRIMARY KEY, slug TEXT UNIQUE, title TEXT, status TEXT, client_id INTEGER, owner_user_id INTEGER,
  data TEXT, video_url TEXT, booking_url TEXT, pack_qid INTEGER, visits INTEGER DEFAULT 0, created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS funnel_applications (
  id INTEGER PRIMARY KEY, funnel_id INTEGER, name TEXT, email TEXT, answers TEXT, consent INTEGER DEFAULT 0,
  created REAL);
"""
WRITER_SYSTEM = (
    "You write a VSL (video sales letter) funnel for a coaching or info business, in the programme owner's voice: "
    "direct, confident, plain words, no hype, no guarantees or income claims, no invented results or names. Reply "
    "ONLY with JSON: {\"headline\": max 12 words, \"subhead\": max 25 words, \"bullets\": [3 short benefit lines], "
    "\"cta\": max 5 words, \"script\": [one object per section {\"section\", \"text\"} in this order: Hook, The "
    "problem, The big mistake, The mechanism, Proof, The offer, Who it's for (and not for), Call to action; each text "
    "60-180 words, spoken to camera, proof as a framework for showing verified results with permission, never made "
    "up; no fake scarcity or pressure such as 'act now', 'last chance', 'limited spots' or 'doors closing'], "
    "\"followups\": [3 short messages for people who applied but didn't book]}. Use only the brief's facts.\n\n"
    "Method notes:\n")


class FunnelError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


def _ensure(c) -> None:
    c.executescript(_SCHEMA)


def _slug(title: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "vsl"
    return f"{base}-{secrets.token_hex(3)}"


def _template(b: dict) -> dict:
    o, aud, mech, pain, tf, offer = (b["outcome"], b["audience"], b["mechanism"], b["pain"], b["timeframe"],
                                     b.get("offer") or "the programme")
    texts = {
        "Hook": f"If you're one of the {aud} who wants {o} without {pain}, watch this to the end.",
        "The problem": f"Most {aud} work hard and still don't get {o}. It isn't effort. It's the approach.",
        "The big mistake": f"The big mistake is {pain}. It feels like progress, but it keeps you stuck.",
        "The mechanism": f"Our {mech} fixes that, step by step. Here's how it works, in plain terms.",
        "Proof": "Here's how we show it works: real, verified results, shared with permission. No made-up numbers.",
        "The offer": f"{offer[:1].upper() + offer[1:]}: everything you need to get {o} in {tf}.",
        "Who it's for (and not for)": f"This is for {aud} who are ready to put in the work. It's not for anyone "
                                      "looking for a shortcut.",
        "Call to action": "If that's you, click below, answer a few quick questions, and book a call.",
    }
    from .static_ads import _yours

    return {"headline": f"How {aud} get {o} without {pain}", "subhead": f"A short video on the {mech}, and how to "
            f"get {_yours(o)} in {tf}.", "bullets": [f"Why most {aud} stay stuck", f"The {mech}, explained simply",
                                             "How to know if this is right for you"],
            "cta": "Apply for a call", "script": [{"section": s, "text": texts[s]} for s, _ in SECTIONS],
            "followups": [f"Saw you applied. Anything I can answer before we talk about {o}?",
                          "Quick one: here's a short video on the exact problem you mentioned.",
                          "Still keen? Grab a time that works and we'll map out your plan."]}


def _ai_copy(tenant, b: dict) -> dict | None:
    if llm.unavailable_reason(tenant) is not None:
        return None
    from .agents import coach_recover

    facts = {k: b.get(k) for k in ("business", "niche", "audience", "offer", "price", "outcome", "mechanism", "pain",
                                   "timeframe")}
    try:
        out = llm.complete(tenant, WRITER_SYSTEM + coach_recover.method_notes(), f"Brief: {facts}",
                           purpose="funnel_copy", max_tokens=4000, timeout=90.0)
        data = llm.parse_json(out["text"])
    except (llm.AIUnavailable, ValueError):
        return None
    tpl = _template(b)
    script = {str(s.get("section", "")).lower(): str(s.get("text", "")).strip().replace("**", "")
              for s in data.get("script") or [] if isinstance(s, dict)}
    good = {
        "headline": str(data.get("headline") or tpl["headline"])[:120],
        "subhead": str(data.get("subhead") or tpl["subhead"])[:240],
        "bullets": [str(x)[:140] for x in (data.get("bullets") or [])][:4] or tpl["bullets"],
        "cta": str(data.get("cta") or tpl["cta"])[:40],
        "script": [{"section": s, "text": script.get(s.lower()) or t["text"]} for (s, _), t in
                   zip(SECTIONS, tpl["script"], strict=False)],
        "followups": [str(x)[:400] for x in (data.get("followups") or [])][:3] or tpl["followups"],
    }
    return good


def script_text(title: str, copy: dict) -> str:
    total = sum(m for _, m in SECTIONS)
    lines = [f"VSL SCRIPT: {title}", f"About {total} minutes. Read to camera; pause between sections.", ""]
    for (name, mins), part in zip(SECTIONS, copy["script"], strict=False):
        lines += [f"{name.upper()} (about {mins} min)", part["text"], ""]
    return "\n".join(lines).strip() + "\n"


def launch(tenant, data: dict, user: dict | None = None) -> dict:
    """Build a whole VSL funnel from one brief and queue it as one approval pack."""
    partial: dict = {}
    client = None
    if data.get("client_id"):
        try:
            client = webinar_launch._client(tenant, int(data["client_id"]), user)
        except webinar_launch.LaunchError as e:
            raise FunnelError(e.status, str(e)) from None
        cb = brief_mod.for_client(client)
        partial.update({k: v for k, v in cb["brief"].items() if v not in (None, "") and k not in cb["missing"]})
    partial.update({k: data[k] for k in brief_mod.FIELDS if data.get(k) not in (None, "")})
    partial = webinar_launch._fill_audience(partial, for_client=client is not None)
    b = brief_mod.build(None if client else tenant, partial)["brief"]
    written = _ai_copy(tenant, b)
    if written and guardrails.blocked(guardrails.check_for(tenant, script_text(written["headline"], written),
                                                           {"client_asset": True})):
        written = None  # the AI's wording tripped a guardrail: use the clean template rather than a held pack
    copy = written or _template(b)
    title = copy["headline"]
    now = time.time()
    owner = (user or {}).get("id") if not webinar_launch._is_owner(user) else None
    store = {"copy": copy, "brief": {k: b.get(k) for k in ("business", "niche", "audience", "offer", "price")},
             "words": "ai" if written else "template"}
    with tenant.db() as c:
        _ensure(c)
        fid = c.execute("INSERT INTO funnels(slug,title,status,client_id,owner_user_id,data,video_url,booking_url,"
                        "created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (_slug(title), title, "draft", client["id"] if client else None, owner, json.dumps(store),
                         str(data.get("video_url") or ""), str(data.get("booking_url") or ""), now, now)).lastrowid
    body = "\n".join([f"Approve once to put the VSL funnel \"{title}\" live.", "",
                      "Approving this will publish three pages: the VSL page, the application page and the thank-you "
                      "page with the booking link.", "", "Already written: the VSL script (about 14 minutes), the "
                      "page copy and 3 follow-ups for people who apply but don't book.", "",
                      script_text(title, copy)])
    meta = {"funnel_id": fid, "client_id": client["id"] if client else None, "client_asset": True}
    if owner:
        meta["owner_user_id"] = owner
    item = queue.add(tenant, KIND, body, channel="manual", title=f"VSL funnel: {title}"[:200], meta=meta,
                     actor=webinar_launch._actor(user))
    with tenant.db() as c:
        c.execute("UPDATE funnels SET pack_qid=? WHERE id=?", (item["id"], fid))
    return summary(tenant, fid, user)


def _row(tenant, fid: int, user: dict | None) -> dict:
    with tenant.db() as c:
        _ensure(c)
        found = db.rows(c.execute("SELECT * FROM funnels WHERE id=?", (int(fid),)))
    r = found[0] if found else None
    if r is None or (not webinar_launch._is_owner(user) and r.get("owner_user_id") != (user or {}).get("id")):
        raise FunnelError(404, "Funnel not found")
    return r


def by_slug(tenant, slug: str) -> dict | None:
    with tenant.db() as c:
        _ensure(c)
        found = db.rows(c.execute("SELECT * FROM funnels WHERE slug=?", (slug,)))
    return found[0] if found else None


def summary(tenant, fid: int, user: dict | None = None) -> dict:
    r = _row(tenant, fid, user)
    d = json.loads(r["data"] or "{}")
    with tenant.db() as c:
        apps = c.execute("SELECT COUNT(*) n FROM funnel_applications WHERE funnel_id=?", (r["id"],)).fetchone()["n"]
    pack = queue.get(tenant, r["pack_qid"]) if r.get("pack_qid") else None
    base = f"/f/{tenant.id}/{r['slug']}"
    return {"funnel_id": r["id"], "title": r["title"], "status": r["status"], "live": r["status"] == "live",
            "page_url": base, "apply_url": f"{base}/apply", "thanks_url": f"{base}/thanks",
            "script_url": f"/t/{tenant.id}/api/funnels/{r['id']}/script.txt", "video_url": r["video_url"] or "",
            "booking_url": r["booking_url"] or "", "applications": apps, "words": d.get("words"),
            "copy": d.get("copy") or {}, "pack": {"id": pack["id"], "status": pack["status"]} if pack else None}


def update(tenant, fid: int, patch: dict, user: dict | None = None) -> dict:
    _row(tenant, fid, user)
    sets = {k: str(v or "").strip() for k, v in patch.items() if k in ("video_url", "booking_url") and v is not None}
    for k, v in sets.items():
        if v and not re.match(r"^https://[^\s<>\"']{4,500}$", v):
            raise FunnelError(422, f"The {k.replace('_', ' ')} must be a full https:// link")
    if sets:
        with tenant.db() as c:
            c.execute(f"UPDATE funnels SET {', '.join(f'{k}=?' for k in sets)}, updated=? WHERE id=?",
                      (*sets.values(), time.time(), int(fid)))
    return summary(tenant, fid, user)


def list_funnels(tenant, user: dict | None = None) -> list[dict]:
    with tenant.db() as c:
        _ensure(c)
        rows = db.rows(c.execute("SELECT id, owner_user_id FROM funnels ORDER BY id DESC LIMIT 100"))
    mine = [r for r in rows if webinar_launch._is_owner(user) or r.get("owner_user_id") == (user or {}).get("id")]
    return [summary(tenant, r["id"], user) for r in mine]


@queue.on_approve(KIND)
def _pack_approved(tenant, item: dict, actor: str) -> None:
    fid = (item.get("meta") or {}).get("funnel_id")
    if fid:
        with tenant.db() as c:
            c.execute("UPDATE funnels SET status='live', updated=? WHERE id=?", (time.time(), int(fid)))


def apply(tenant, slug: str, form: dict) -> dict:
    """A visitor's application from the public page."""
    f = by_slug(tenant, slug)
    if f is None or f["status"] != "live":
        raise FunnelError(404, "Not found")
    name, email = str(form.get("name") or "").strip()[:120], str(form.get("email") or "").strip()[:200]
    if not name or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise FunnelError(422, "Please add your name and a valid email.")
    if not form.get("consent"):
        raise FunnelError(422, "Please tick the box so we can contact you about your application.")
    answers = {k: str(form.get(k) or "").strip()[:600] for k, _ in QUESTIONS}
    with tenant.db() as c:
        c.execute("INSERT INTO funnel_applications(funnel_id,name,email,answers,consent,created) VALUES(?,?,?,?,1,?)",
                  (f["id"], name, email, json.dumps(answers), time.time()))
    return {"ok": True, "next": f"/f/{tenant.id}/{slug}/thanks"}


def brand(tenant, f: dict) -> str:
    if f.get("client_id"):
        with tenant.db() as c:
            r = c.execute("SELECT name FROM clients WHERE id=?", (f["client_id"],)).fetchone()
        if r and r["name"]:
            return str(r["name"])
    b = tenant.get("brand", {}) or {}
    return (b.get("name") if isinstance(b, dict) else None) or config.get().brand


# --------------------------------------------------------------------------- chat: "build a VSL funnel for my X client"
ASK = re.compile(r"\b(build|make|create|launch|set up|write|draft)\b[^.?!\n]{0,40}\b(vsl|funnel|sales page)\b", re.I)


def wants_funnel(text: str) -> bool:
    return bool(ASK.search(text or "")) and "webinar" not in (text or "").lower()


def from_chat(tenant, text: str, user: dict) -> tuple[str, dict]:
    m = webinar_launch._CLIENT.search(text or "")
    hint = m.group("what").strip() if m else ""
    client = webinar_launch.match_client(tenant, hint, user)
    data: dict = {"client_id": client["id"]} if client else ({"niche": hint} if hint else {})
    bm = re.search(r"https://[^\s]+", text or "")
    if bm:
        data["booking_url"] = bm.group(0).rstrip(".,)")
    try:
        res = launch(tenant, data, user)
    except FunnelError as e:
        return str(e), {"funnel": None}
    who = f" for {client['name']}" if client else ""
    reply = (f"Done. I built your VSL funnel{who}: \"{res['title']}\". The VSL script (about 14 minutes, ready to read "
             "on camera), the VSL page, the application page with qualifying questions, the thank-you page with the "
             "booking link, and 3 follow-ups are ready. Approve once and the pages go live. Paste your video and "
             "booking links whenever you have them.")
    return reply, {"funnel": res}
