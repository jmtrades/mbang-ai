"""Workspace setup: a live checklist from an empty workspace to a real one, the go-live preflight (also used by
scripts/preflight_live.py) and the first-run checklists for owners, students and the team.

Every item is computed from the workspace's own state by rules (AI not used), says why it matters and gives the
exact next action: a link inside the app, or the server command to run. Secrets are never accepted or shown:
AI keys and live switches change only on the server.
"""
from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path

from . import audit, config, llm, settings

DEFAULT_PRIMARY = "#5b6cff"
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_LOGO_URL = re.compile(r"^https://[^\s\"'<>]{4,490}$")
_LOGO_PATH = re.compile(r"^/(?!/)[A-Za-z0-9._~/-]{1,300}$")
_SPEAKER = re.compile(r"^\s*(?:\[?\(?\d{1,2}:\d{2}(?::\d{2})?\)?\]?\s*[-–]?\s*)?([A-Z][A-Za-z.'\-]{0,20}(?: [A-Z][A-Za-z.'\-]{0,20}){0,2}|Speaker \d+)\s*:\s+\S")
LABEL = "Checked from your workspace's own state by rules (AI not used)"
DECISIONS = {"persona": ("use", "not_now"), "backups": ("configured_elsewhere", "not_yet")}


class SetupError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


# --------------------------------------------------------------------------- helpers
def _count(c, sql: str, args=()) -> int:
    try:
        return int(c.execute(sql, args).fetchone()[0] or 0)
    except Exception:  # noqa: BLE001 - a table from another feature may not exist yet
        return 0


def _tables(c) -> set[str]:
    return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _item(key: str, title: str, status: str, why: str, detail: str = "", *, href: str | None = None,
          action: str | None = None, command: str | None = None, required: bool = True, data: dict | None = None) -> dict:
    """status: done | todo | warn | optional | info"""
    nxt = None
    if href:
        nxt = {"label": action or "Open", "href": href}
    elif command:
        nxt = {"label": action or "Run on the server", "command": command}
    return {"key": key, "title": title, "status": status, "why": why, "detail": detail, "next": nxt,
            "required": required, "data": data or {}}


def decisions(tenant) -> dict:
    d = tenant.get("setup_decisions", {}) or {}
    return d if isinstance(d, dict) else {}


def decide(tenant, item: str, value: str, actor: str) -> dict:
    if item not in DECISIONS or value not in DECISIONS[item]:
        raise SetupError(422, "Unknown decision")
    d = decisions(tenant)
    d[item] = {"value": value, "at": time.time(), "by": actor}
    tenant.set("setup_decisions", d)
    audit.log(tenant, actor, "setup.decide", {"item": item, "value": value})
    return d


# --------------------------------------------------------------------------- course material (A0)
def has_speaker_labels(lines: list[str]) -> bool:
    speakers = []
    for ln in lines[:400]:
        m = _SPEAKER.match(ln or "")
        if m:
            speakers.append(m.group(1).lower())
    return len(speakers) >= 3 and len(set(speakers)) >= 2


def course_material(tenant) -> dict:
    cfg = config.get()
    fixture = tenant.kb_scope == "course" and cfg.using_fixture_corpus
    docs = []
    try:
        from . import index

        docs = list(index.for_tenant(tenant).docs.values())
    except Exception:  # noqa: BLE001 - an index problem shows as "no material", never a 500
        docs = []
    words = sum(len(" ".join(d.lines).split()) for d in docs)
    transcripts = [d for d in docs if d.kind == "transcript"]
    labelled = sum(1 for d in transcripts if has_speaker_labels(d.lines))
    return {"files": len(docs), "words": words, "transcripts": len(transcripts), "transcripts_with_speakers": labelled,
            "fixture": fixture, "scope": tenant.kb_scope}


# --------------------------------------------------------------------------- preflight (shared with the script)
def preflight(tenant) -> list[dict]:
    """Go-live checks as {name, ok, need}. Offline and read-only; never includes a value."""
    from . import console, embeddings, tenancy, vault

    cfg = config.get()
    out: list[dict] = []

    def add(name, ok, need=""):
        out.append({"name": name, "ok": bool(ok), "need": need})

    add("Web app built (web/dist)", (config.ROOT / "web" / "dist" / "index.html").exists(), "cd web && npm run build")
    add("Course material in inputs/", not cfg.using_fixture_corpus, "copy the course folders into inputs/")
    add("Secure cookies (OPERATOR_COOKIE_SECURE=1)", cfg.cookie_secure, "set OPERATOR_COOKIE_SECURE=1 behind HTTPS")
    add("Public URL / domain (OPERATOR_PUBLIC_URL)", bool(os.environ.get("OPERATOR_PUBLIC_URL")), "set OPERATOR_PUBLIC_URL")
    add("Caddyfile (automatic HTTPS)", (config.ROOT / "deploy" / "Caddyfile").exists(), "deploy/Caddyfile")
    add("Backups (scripts/backup.sh)", (config.ROOT / "scripts" / "backup.sh").exists(), "scripts/backup.sh + timer")
    add("AI model key in environment", bool(config.llm_key()), "ANTHROPIC_API_KEY or OPENAI_API_KEY")
    add("AI token budget > 0 (optional)", llm.budget(tenant) > 0, "scripts/tenant_admin.py budget <tid> ai <n> --owner-yes ...")
    try:
        cached = shutil.which("python3") is not None and embeddings.model_cached()
    except Exception:  # noqa: BLE001
        cached = False
    add("Embedding model cached locally (optional)", cached, "scripts/fetch_embeddings.py")
    for it in console.INTEGRATIONS:
        if not it["fields"]:
            continue
        st = vault.status(tenant, it["fields"])
        missing = [f for f, v in st.items() if v != "set"]
        add(f"{it['name']} login", not missing, ", ".join(missing))
    add("Meta live switch", tenant.get("live_meta_enabled", False),
        "tenant_admin.py switch <tid> live_meta_enabled on --owner-yes ...")
    add("Meta account allowlist non-empty", bool(tenant.get("meta_allowlist", [])),
        "tenant_admin.py allowlist <tid> add <act_id> ...")
    add("Calendar live switch", tenant.get("live_calendar_enabled", False),
        "tenant_admin.py switch <tid> live_calendar_enabled on ...")
    try:
        separate = tenancy.get("demo").db_path != tenant.db_path or tenant.is_demo
    except tenancy.TenantNotFound:
        separate = True
    add("Demo DB separate from real", separate, "")
    return out


# --------------------------------------------------------------------------- backups
def backup_state() -> dict:
    dest = Path(os.environ.get("OPERATOR_BACKUP_DIR") or (config.ROOT / "backups"))
    if not dest.is_absolute():
        dest = config.ROOT / dest
    newest = None
    try:
        files = sorted(dest.glob("operator-*.tgz"), key=lambda p: p.stat().st_mtime, reverse=True)
        newest = files[0].stat().st_mtime if files else None
    except OSError:
        newest = None
    return {"newest": newest, "fresh": bool(newest and time.time() - newest < 8 * 86400)}


# --------------------------------------------------------------------------- the checklist
LIVE_SENTENCES = {
    "live_channels_enabled": ("Approved Telegram, Slack, SMS and email messages are sent for real.",
                              "Approved Telegram, Slack, SMS and email messages go to a practice inbox; nobody receives them."),
    "live_calendar_enabled": ("Approved calendar invites are created on your real calendar.",
                              "Approved calendar invites stay on a practice calendar."),
    "live_meta_enabled": ("Approved ad changes can reach the ad accounts on your allowlist (new ads always start paused).",
                          "Ad changes stay on a practice ad account."),
}


def checklist(tenant) -> dict:
    from . import console

    cfg = config.get()
    tid = tenant.id
    items: list[dict] = []
    brand = tenant.get("brand", {}) or {}
    dec = decisions(tenant)

    # 1. brand
    name_set = bool(brand.get("name")) and brand.get("name") != cfg.brand
    colours = bool(brand.get("primary")) and brand.get("primary") != DEFAULT_PRIMARY
    logo = bool(brand.get("logo"))
    done = sum([name_set, colours, logo])
    items.append(_item("brand", "Your brand: name, colours and logo",
                       "done" if done == 3 else "todo",
                       "People should see your programme's name and colours, not ours, from the sign-in page onwards.",
                       f"Name {'set' if name_set else 'not set'} · colours {'set' if colours else 'default'} · "
                       f"logo {'set' if logo else 'not set'}",
                       href="#/setup?brand=1", action="Edit brand", data={"brand": brand}))

    # 2. course material (A0)
    cm = course_material(tenant)
    if cm["fixture"]:
        st, detail = "warn", ("Using the small fictional sample material that ships for tests "
                              f"({cm['words']:,} words). Lessons and answers built now would teach invented content.")
    elif cm["files"] == 0:
        st, detail = "todo", "No material loaded yet."
    else:
        st, detail = "done", (f"{cm['files']:,} files · {cm['words']:,} words · {cm['transcripts']:,} transcripts, "
                              f"{cm['transcripts_with_speakers']:,} with speaker labels")
    items.append(_item("material", "Course material loaded", st,
                       "Every answer, lesson and draft is built from this material. With the sample material the "
                       "Academy and persona would teach made-up numbers.",
                       detail, command="cp -R /path/to/your/course/folders/* inputs/",
                       action="On the server: copy your course folders into inputs/ (the index rebuilds by itself)",
                       data=cm))

    # 3. playbooks switch decision
    pb = tenant.get("third_party_kb", None)
    items.append(_item("playbooks", "Decide on the 2026 Playbooks", "done" if pb is not None else "todo",
                       "Researched public best practice, kept separate from your material. Off by default; you choose.",
                       "Decided: on" if pb is True else "Decided: off" if pb is False else "Not decided yet",
                       href="#/playbooks", action="Open the Playbooks"))

    # 4. AI key + budget (server only)
    key = bool(config.llm_key())
    budget, used = llm.budget(tenant), llm.used(tenant)
    reason = llm.unavailable_reason(tenant)
    ai_detail = (f"Key {'present' if key else 'missing'} on the server · budget {budget:,} tokens, {used:,} used"
                 + ("" if reason is None else f" · {reason}"))
    items.append(_item("ai", "AI key and budget (optional)", "done" if reason is None else "optional",
                       "Everything works without AI (rules, templates, retrieval, labelled replays). A key plus a budget "
                       "adds AI drafting. Keys are never typed into the browser.",
                       ai_detail, command=f'scripts/tenant_admin.py budget {tid} ai 200000 --owner-yes "<your reason>"',
                       action="On the server: put ANTHROPIC_API_KEY (or OPENAI_API_KEY) in the server's environment, "
                              "then set a token budget", required=False,
                       data={"key_present": key, "budget": budget, "used": used}))

    # 5. integrations
    ints = console.integrations(tenant)
    connected = [i["name"] for i in ints if i["connected"]]
    items.append(_item("integrations", "Integrations", "done" if len(connected) >= 1 else "optional",
                       "Logins for calendar, email, messaging and ads are stored encrypted on the server. Until they are "
                       "added, everything runs on practice (mock) connections.",
                       f"{len(connected)} of {len(ints)} connected" + (f": {', '.join(connected)}" if connected else ""),
                       href="#/integrations", action="Open Integrations", required=False,
                       data={"items": [{"name": i["name"], "connected": i["connected"], "live": i.get("live")}
                                       for i in ints]}))

    # 6. live switches (read-only sentences)
    sentences = []
    for k, (on, off) in LIVE_SENTENCES.items():
        sentences.append(on if tenant.get(k, False) else off)
    allow = len(tenant.get("meta_allowlist", []) or [])
    if tenant.get("live_meta_enabled", False):
        sentences.append(f"{allow} ad account(s) on the allowlist.")
    items.append(_item("live", "Live switches (read-only)", "info",
                       "Live sending is switched on only on the server, with your recorded reason. Approval is still "
                       "needed for every item either way.", " ".join(sentences),
                       command=f'scripts/tenant_admin.py switch {tid} live_channels_enabled on --owner-yes "<your reason>"',
                       action="Change on the server", required=False, data={"sentences": sentences}))

    with tenant.db() as c:
        tables = _tables(c)
        team = _count(c, "SELECT COUNT(*) FROM users WHERE role<>'owner' AND deleted IS NULL AND active=1")
        invites = _count(c, "SELECT COUNT(*) FROM invites WHERE COALESCE(kind,'invite')='invite'")
        students = _count(c, "SELECT COUNT(*) FROM students")
        missions = _count(c, "SELECT COUNT(*) FROM missions WHERE COALESCE(status,'') NOT IN ('cancelled')") \
            if "missions" in tables else 0
        academy_n, academy_what = _academy(c, tables)
    persona_n, persona_what = _persona_state(tenant)

    # 7. team invited
    items.append(_item("team", "Invite your team", "done" if team or invites else "todo",
                       "Coaches, setters and closers each get their own sign-in, see only what their role allows and "
                       "approve only their kind of drafts.",
                       f"{team} team member(s) · {invites} invite(s) sent", href="#/team", action="Invite people"))
    # 8. students imported
    items.append(_item("students", "Import your students", "done" if students else "todo",
                       "Students need a record before check-ins, accountability and their own coaching can start.",
                       f"{students:,} student record(s)", href="#/team?tab=students", action="Import students"))
    # 9. first mission
    items.append(_item("mission", "Start a first mission", "done" if missions else "todo",
                       "A mission is a goal the agents work towards step by step, with every outgoing item approved.",
                       f"{missions} mission(s)", href="#/missions", action="Open Missions"))
    # 10. academy
    items.append(_item("academy", "Publish a first Academy build", "done" if academy_n else "todo",
                       "The Academy turns your material into lessons, quizzes and certifications for each role. Load your "
                       "real material first.", f"{academy_n} {academy_what}", href="#/academy", action="Open the Academy"))
    # 11. persona decision (optional)
    pdec = (dec.get("persona") or {}).get("value")
    pstat = "done" if persona_n or pdec else "optional"
    items.append(_item("persona", "Decide on a persona (optional)", pstat,
                       "A named persona needs the person's recorded consent. Without one, the assistant speaks as your "
                       "brand.", persona_what if persona_n else {"use": "You plan to set one up",
                                                                     "not_now": "Decided: not now"}.get(pdec, "Not decided"),
                       href="#/console", action="Open the persona section", required=False))
    # 12. backups
    bk = backup_state()
    bdec = (dec.get("backups") or {}).get("value")
    bst = "done" if bk["fresh"] or bdec == "configured_elsewhere" else "todo"
    when = time.strftime("%d %b %Y", time.localtime(bk["newest"])) if bk["newest"] else None
    items.append(_item("backups", "Backups configured", bst,
                       "A nightly backup means a lost server or a bad change costs a day, not the programme.",
                       (f"Newest backup: {when}" if when else "No backup archive found on this server")
                       + (" · marked as handled elsewhere" if bdec == "configured_elsewhere" else ""),
                       command="scripts/backup.sh",
                       action="On the server: run it once, then schedule it nightly (cron or a systemd timer, see "
                              "deploy/README.md)", data=bk))
    # 13. preflight
    pf = preflight(tenant)
    ok = sum(1 for p in pf if p["ok"])
    items.append(_item("preflight", "Go-live preflight", "done" if ok == len(pf) else "info",
                       "The same checks as scripts/preflight_live.py: what is still missing before going live.",
                       f"{ok} of {len(pf)} OK", command=f"scripts/preflight_live.py --tenant {tid}", action="Run on the server",
                       required=False, data={"rows": pf}))

    req = [i for i in items if i["required"]]
    return {"items": items, "done": sum(1 for i in req if i["status"] == "done"), "total": len(req),
            "label": LABEL, "is_demo": tenant.is_demo, "tenant": tid}


def _academy(c, tables: set[str]) -> tuple[int, str]:
    """The Academy is set up once the owner has published lessons from a build (academy_lessons.status 'published';
    drafts waiting for review and stale lessons don't count)."""
    if "academy_lessons" not in tables:
        return 0, "published lesson(s)"
    return _count(c, "SELECT COUNT(*) FROM academy_lessons WHERE status='published'"), "published lesson(s)"


def _persona_state(tenant) -> tuple[int, str]:
    """1 with a plain description when a persona is live (the subject's signed consent is in force), else 0 with
    the current state (a mined profile waiting for the subject, a withdrawn or expired consent ...)."""
    try:
        from . import persona

        st = persona.status(tenant)
        label = str(st.get("label") or st.get("state") or "")
        if persona.active(tenant):
            return 1, f"Persona live: {label}" if label else "Persona live (signed consent in force)"
        return 0, label or "No persona"
    except Exception:  # noqa: BLE001 - the checklist must render even if a feature can't be read
        return 0, "No persona"


# --------------------------------------------------------------------------- brand
def save_brand(tenant, data: dict, actor: str) -> dict:
    from . import guardrails

    cur = dict(tenant.get("brand", {}) or {})
    new = dict(cur)
    if "name" in data and data["name"] is not None:
        name = re.sub(r"\s+", " ", str(data["name"])).strip()
        if not (1 <= len(name) <= 60):
            raise SetupError(422, "The brand name needs 1 to 60 characters.")
        if guardrails.check(name, "plain", {}, guardrails.tenant_banned_names(tenant)):
            raise SetupError(422, "That name can't be used (it matches your banned-names list or a guardrail).")
        new["name"] = name
    if "owner_display" in data and data["owner_display"] is not None:
        od = re.sub(r"\s+", " ", str(data["owner_display"])).strip()[:60]
        if od and guardrails.check(od, "plain", {}, guardrails.tenant_banned_names(tenant)):
            raise SetupError(422, "That display name can't be used (it matches your banned-names list).")
        new["owner_display"] = od
    for k in ("primary", "accent"):
        if k in data and data[k] is not None:
            if not _HEX.match(str(data[k])):
                raise SetupError(422, f"{k} colour must look like #1a2b3c")
            new[k] = str(data[k]).lower()
    if "logo" in data and data["logo"] is not None:
        logo = str(data["logo"]).strip()
        # the same rule as the settings shape check (hardening): an https:// link or a path on this site (never a
        # protocol-relative "//host" address); the shell shows it in the menu, the header and on sign-in
        if logo and not (_LOGO_URL.match(logo) or _LOGO_PATH.match(logo)):
            raise SetupError(422, "The logo must be an https:// image link or a path on this site (for example /logo.png).")
        new["logo"] = logo
    settings.update_from_api(tenant, "brand", new, actor)
    return new


# --------------------------------------------------------------------------- first-run checklists
def _progress(c, uid: int) -> dict[str, float]:
    return {r["item"]: r["done"] for r in c.execute("SELECT item, done FROM setup_progress WHERE user_id=?", (uid,))}


FIRST_RUN_ROLES = ("owner", "student", "coach", "setter", "closer")


def first_run(tenant, user: dict, role: str | None = None) -> dict:
    role = role or user["role"]
    if role not in FIRST_RUN_ROLES:
        raise SetupError(422, "Unknown role")
    if role != user["role"]:
        raise SetupError(403, "You can only see your own first-run checklist.")
    uid = user["id"]
    cfg = config.get()
    with tenant.db() as c:
        tables = _tables(c)
        prog = _progress(c, uid)
        items: list[dict] = []

        def add(key, title, why, auto: bool | None, href, action, manual=False, detail=""):
            done_at = prog.get(key)
            done = bool(done_at) or bool(auto)
            items.append({"key": key, "title": title, "why": why, "done": done, "auto": auto is not None,
                          "manual": manual, "done_at": done_at, "href": href, "action": action, "detail": detail})

        if role == "owner":
            material = not (tenant.kb_scope == "course" and cfg.using_fixture_corpus)
            brand = tenant.get("brand", {}) or {}
            add("material", "Connect your course material", "Answers and lessons come from it.", material, "#/setup",
                "See how", detail="" if material else "Using the fictional sample material for now.")
            add("brand", "Set your brand and voice", "People see your name and colours.",
                bool(brand.get("name") and brand.get("name") != cfg.brand) or bool(brand.get("logo")), "#/setup?brand=1",
                "Edit brand")
            add("team", "Invite your team", "Coaches, setters and closers get their own sign-in.",
                _count(c, "SELECT COUNT(*) FROM invites WHERE COALESCE(kind,'invite')='invite'") > 0
                or _count(c, "SELECT COUNT(*) FROM users WHERE role<>'owner' AND deleted IS NULL") > 0, "#/team", "Invite")
            add("students", "Import your students", "Check-ins and coaching start from a record.",
                _count(c, "SELECT COUNT(*) FROM students") > 0, "#/team?tab=students", "Import")
            add("question", "Ask your first question", "See a cited answer from your own material.",
                _count(c, "SELECT COUNT(*) FROM queries WHERE user_id=?", (uid,)) > 0, "#/coach", "Ask")
        elif role == "student":
            sid = user.get("student_id")
            coach = None
            coach_id = user.get("coach_id")
            if not coach_id and sid:
                r = c.execute("SELECT coach_id FROM students WHERE id=?", (sid,)).fetchone()
                coach_id = r["coach_id"] if r else None
            if coach_id:
                r = c.execute("SELECT display FROM users WHERE id=? AND deleted IS NULL", (coach_id,)).fetchone()
                coach = r["display"] if r else None
            add("meet_coach", "Meet your coach", "Your coach sets your first 30 days with you.", None,
                "#/coach", "Ask a question", manual=True,
                detail=f"Your coach is {coach}." if coach else "Your coach will be assigned soon.")
            add("checkin", "Do your first weekly check-in", "Two minutes a week keeps your coach in the loop.",
                bool(sid) and _count(c, "SELECT COUNT(*) FROM checkins WHERE student_id=?", (sid,)) > 0, "#/checkin",
                "Check in")
            lessons = 0
            if "academy_progress" in tables:  # the Academy marks a finished lesson status='complete'
                lessons = _count(c, "SELECT COUNT(*) FROM academy_progress WHERE user_id=? AND status='complete'", (uid,))
            add("lesson", "Finish your first lesson", "Lessons come from the programme's own material.", lessons > 0,
                "#/academy", "Open the Academy")
            plays = _count(c, "SELECT COUNT(*) FROM roleplays WHERE user_id=? AND status='complete'", (uid,)) \
                if "roleplays" in tables else 0
            add("roleplay", "Do your first role-play", "Practise a sales conversation before a real one.", plays > 0,
                "#/academy", "Practise")
            pros = _count(c, "SELECT COUNT(*) FROM prospects WHERE owner_user_id=?", (uid,)) if "prospects" in tables else 0
            add("prospects", "Build your first prospect list", "Ten good-fit businesses beat a hundred random ones.",
                pros > 0, "#/prospects", "Open Prospects")
            # delivery truth: outreach counts once it really went out (sent by a live sender or confirmed "I sent it")
            sent = _count(c, "SELECT COUNT(*) FROM queue WHERE json_extract(meta,'$.owner_user_id')=? AND status='sent' "
                             "AND json_extract(meta,'$.prospect_facing')=1", (uid,))
            add("outreach", "Send your first outreach", "You approve every message before it goes out.", sent > 0,
                "#/queue", "Open your approvals")
        else:
            add("memory", "Tell the assistant about your work", "Advice uses what you tell it.",
                _count(c, "SELECT COUNT(*) FROM user_memory WHERE user_id=?", (uid,)) > 0, "#/settings", "Open Settings")
            lessons = 0
            if "academy_progress" in tables:
                lessons = _count(c, "SELECT COUNT(*) FROM academy_progress WHERE user_id=? AND status='complete'", (uid,))
            add("lesson", "Finish your first lesson", "Your role's path in the Academy.", lessons > 0, "#/academy",
                "Open the Academy")
            # drills live in the Academy's review store (academy_review, item_kind 'drill'); older drill_cards rows too
            drilled = _count(c, "SELECT COUNT(*) FROM academy_review WHERE user_id=? AND item_kind='drill' AND reps>0",
                             (uid,)) if "academy_review" in tables else 0
            drilled += _count(c, "SELECT COUNT(*) FROM drill_cards WHERE user_id=? AND reps>0", (uid,))
            add("drill", "Practise an objection", "Short spaced-repetition drills.", drilled > 0, "#/drills", "Open drills")
            if role == "coach":
                add("students", "Look over your students", "See who needs you this week.", None, "#/students",
                    "Open Accountability", manual=True)
            else:
                add("queue", "Check your approval queue", "Drafts in your lane wait for you.", None, "#/queue",
                    "Open the queue", manual=True)
        # remember auto-detected progress so the list never goes backwards
        now = time.time()
        for it in items:
            if it["done"] and not it["done_at"]:
                c.execute("INSERT OR IGNORE INTO setup_progress(user_id,item,done) VALUES(?,?,?)", (uid, it["key"], now))
                it["done_at"] = now
        dismissed = bool(prog.get("_dismissed"))
    done = sum(1 for i in items if i["done"])
    return {"role": role, "items": items, "done": done, "total": len(items), "complete": done == len(items),
            "dismissed": dismissed, "label": LABEL}


def mark(tenant, user: dict, item: str, done: bool) -> dict:
    if not re.fullmatch(r"[a-z_]{1,40}", item or ""):
        raise SetupError(422, "Unknown item")
    current = first_run(tenant, user)
    keys = {i["key"]: i for i in current["items"]}
    if item != "_dismissed" and item not in keys:
        raise SetupError(404, "Not found")
    if item in keys and keys[item]["auto"]:
        raise SetupError(409, "This step is checked automatically from your records.")
    with tenant.db() as c:
        if done:
            c.execute("INSERT OR REPLACE INTO setup_progress(user_id,item,done) VALUES(?,?,?)", (user["id"], item, time.time()))
        else:
            c.execute("DELETE FROM setup_progress WHERE user_id=? AND item=?", (user["id"], item))
    return first_run(tenant, user)

