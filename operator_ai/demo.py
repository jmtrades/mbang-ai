"""Pitch demo workspace: a separate database (data/demo.db) seeded with a believable,
fictional cohort. Every fictional person, business and number carries "(DEMO)"; fake
emails use example.invalid. Resets back up first and refuse to touch any other database."""
from __future__ import annotations

import random
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from . import audit, auth, config, db, tenancy

_lock = threading.Lock()

FIRST = ["Ava", "Liam", "Maya", "Noah", "Zara", "Ethan", "Isla", "Owen", "Leah", "Ravi", "Nina", "Theo", "Ines", "Kofi",
         "Mila", "Jonah", "Priya", "Felix", "Hana", "Luca", "Sana", "Marco", "Elif", "Tomas", "Yara", "Dev", "Clara",
         "Arlo", "Nadia", "Rowan", "Imani", "Soren", "Lena", "Omar", "Freya", "Kai", "Rosa", "Abel", "Tessa", "Idris"]
LAST = ["Hartley", "Okafor", "Lindqvist", "Moreau", "Castillo", "Brennan", "Achterberg", "Varga", "Nakamura", "Duarte",
        "Whitlock", "Osei", "Kowalczyk", "Fairbairn", "Marchetti", "Delacroix", "Halvorsen", "Quayle", "Ferreira", "Tiwari"]
NICHES = ["physio clinics", "dental clinics", "roofing contractors", "bookkeeping firms", "med spas", "personal trainers",
          "solar installers", "law firms", "landscapers", "chiropractors", "wedding photographers", "HVAC companies"]
BUSINESS_WORDS = ["Peak", "Harbour", "Northside", "Bright", "Summit", "Cedar", "Lantern", "Willow", "Granite", "Coastal"]


def _name(rng: random.Random) -> str:
    return f"{rng.choice(FIRST)} {rng.choice(LAST)} (DEMO)"


def _email(name: str) -> str:
    slug = name.replace(" (DEMO)", "").lower().replace(" ", ".")
    return f"{slug}@example.invalid"


class DemoResetRefused(Exception):
    pass


def ensure_seeded(tenant) -> None:
    if not tenant.is_demo:
        return
    with _lock:
        tenancy.ensure_demo_budget(tenant)
        if tenant.get("seeded_at"):
            if tenancy.is_demo_db(tenant) and tenant.get("demo_features") != FEATURE_SEEDERS:
                # seeded by an older build: features added since would show empty pages, so start the demo fresh
                _reset_locked(tenant, "system:upgrade")
                return
            if tenant.get("third_party_kb") is None and tenancy.is_demo_db(tenant):
                tenant.set("third_party_kb", True)  # demo seeded before the playbooks existed
            if tenancy.is_demo_db(tenant):
                _seed_claims(tenant)  # demo seeded before proof records existed (idempotent)
            return
        seed(tenant)


def _seed_claims(tenant) -> None:
    """The fictional proof record goes in before the queue, so the demo's range DM is judged against it."""
    try:
        from . import claims
    except ImportError:  # pragma: no cover
        return
    claims.seed_demo(tenant)


def seed(tenant, n_students: int = 24, n_leads: int = 37) -> dict:
    if not tenancy.is_demo_db(tenant):
        raise DemoResetRefused("Demo data can only be written to the demo database.")
    rng = random.Random(42)
    now = time.time()
    day = 86400
    counts: dict[str, int] = {}
    with tenant.db() as c, db.tx(c):
        # one-click role users; passwords are random and never used (demo-login only)
        users = {}
        for role, display in [("owner", "Owner (DEMO)"), ("coach", "Coach Rivera (DEMO)"), ("setter", "Setter Imani (DEMO)"),
                              ("closer", "Closer Abel (DEMO)"), ("student", "Student Ava Hartley (DEMO)")]:
            uid = c.execute("INSERT INTO users(username,display,role,pw_hash,created) VALUES(?,?,?,?,?)",
                            (f"demo-{role}", display, role, auth.hash_pw(secrets.token_urlsafe(16)), now)).lastrowid
            users[role] = uid
        coach2 = c.execute("INSERT INTO users(username,display,role,pw_hash,created) VALUES(?,?,?,?,?)",
                           ("demo-coach2", "Coach Okafor (DEMO)", "coach", auth.hash_pw(secrets.token_urlsafe(16)), now)).lastrowid
        coaches = [users["coach"], coach2]

        # students with six weeks of check-ins
        student_ids = []
        for i in range(n_students):
            name = "Ava Hartley (DEMO)" if i == 0 else _name(rng)
            joined = now - rng.randint(42, 120) * day
            niche = rng.choice(NICHES)
            sid = c.execute(
                "INSERT INTO students(name,email,coach_id,joined,niche,stage,clients,demo) VALUES(?,?,?,?,?,?,?,1)",
                (name, _email(name), coaches[i % 2], joined, niche, rng.choice(["foundations", "outreach", "first client", "scaling"]),
                 rng.choice([0, 0, 0, 1, 1, 2, 3])),
            ).lastrowid
            student_ids.append(sid)
            trend = rng.choice(["steady", "steady", "rising", "falling", "ghost"])
            for w in range(6, 0, -1):
                if trend == "ghost" and w <= 2:
                    continue
                if rng.random() < 0.08:
                    continue
                base = {"steady": 60, "rising": 30 + (6 - w) * 15, "falling": 90 - (6 - w) * 15, "ghost": 40}[trend]
                outreach = max(0, int(rng.gauss(base, 10)))
                booked = max(0, int(outreach * rng.uniform(0.03, 0.09)))
                showed = sum(1 for _ in range(booked) if rng.random() < (0.45 if trend == "falling" else 0.7))
                mood = max(1, min(5, int(rng.gauss({"falling": 2.4, "ghost": 2.8}.get(trend, 3.8), 0.7))))
                c.execute(
                    "INSERT INTO checkins(student_id,at,outreach,calls_booked,calls_showed,mood,wins,blockers,demo) "
                    "VALUES(?,?,?,?,?,?,?,?,1)",
                    (sid, now - w * 7 * day + rng.randint(0, day), outreach, booked, showed, mood,
                     rng.choice(["Booked a discovery call (DEMO)", "Finished my USP (DEMO)", "Sent 50 openers (DEMO)", ""]),
                     rng.choice(["", "Low reply rate (DEMO)", "No-shows (DEMO)", "Not sure what to post (DEMO)"])),
                )
        c.execute("UPDATE users SET student_id=? WHERE id=?", (student_ids[0], users["student"]))
        counts["students"] = n_students

        # leads
        from .agents.sales import DEFAULT_WEIGHTS
        from .agents.sales import score as score_lead

        sources = ["meta_lead_form", "instagram_dm", "webinar", "referral", "lead_magnet"]
        for i in range(n_leads):
            name = _name(rng)
            answers = {
                "monthly_revenue": rng.choice(["<10k", "10k-30k", "30k-100k", "100k+"]),
                "can_invest": rng.choice(["yes", "yes", "maybe", "no"]),
                "timeline": rng.choice(["now", "1-3 months", "later"]),
                "decision_maker": rng.choice([True, True, False]),
                "goal": rng.choice(["more booked calls", "fix show rate", "launch a webinar", "scale ads"]),
                "niche": rng.choice(NICHES),
                "business": f"{rng.choice(BUSINESS_WORDS)} {rng.choice(['Physio', 'Dental', 'Roofing', 'Books', 'Fitness'])} (DEMO)",
            }
            src = rng.choice(sources)
            sc = score_lead(answers, src, DEFAULT_WEIGHTS) if i % 3 else None  # a third stay unscored for the demo
            c.execute("INSERT INTO leads(name,handle,email,source,answers,stage,created,demo,anon_id,tier,score,route) "
                      "VALUES(?,?,?,?,?,?,?,1,?,?,?,?)",
                      (name, "@" + name.split()[0].lower() + "_demo", _email(name), src, db.jdump(answers),
                       rng.choice(["new", "new", "replied", "quality_conversation", "link_sent", "booked", "showed", "closed"]),
                       now - rng.randint(0, 30) * day, f"L-{secrets.token_hex(3)}", sc and sc["tier"], sc and sc["score"],
                       sc and sc["route"]))
        counts["leads"] = n_leads

        # clients
        for i in range(4):
            name = f"{rng.choice(BUSINESS_WORDS)} {rng.choice(['Physio', 'Dental', 'Roofing', 'Bookkeeping'])} (DEMO)"
            metrics = {"dms_sent": rng.randint(300, 900), "replies": rng.randint(20, 90), "conversations": rng.randint(10, 40),
                       "links_sent": rng.randint(5, 25), "booked": rng.randint(4, 18), "showed": rng.randint(2, 12),
                       "closed": rng.randint(0, 4), "ad_spend": rng.randint(800, 4000), "leads": rng.randint(40, 300),
                       "visitors": rng.randint(800, 4000), "registrants": rng.randint(150, 900),
                       "attendees": rng.randint(40, 300), "webinar_sales": rng.randint(1, 15)}
            c.execute("INSERT INTO clients(student_id,name,niche,offer,price,metrics,demo,created) VALUES(?,?,?,?,?,?,1,?)",
                      (student_ids[i], name, rng.choice(NICHES), "Done-with-you patient acquisition (DEMO)", 2500 + 500 * i,
                       db.jdump(metrics), now))
        counts["clients"] = 4

        # practice ad account (mock adapter data)
        _seed_ads(c, rng, now)
        # calendar busy blocks
        for d in range(1, 6):
            start = _day_at(now + d * day, 10 + rng.randint(0, 5))
            c.execute("INSERT INTO calendar_events(title,start,end,attendees,status,demo,created) VALUES(?,?,?,?,?,1,?)",
                      ("Coaching block (DEMO)", start, start + 3600, "[]", "busy", now))
        # practice outreach prospects
        moves = ["compliment", "question", "resource", "mutual"]
        for i in range(30):
            name = _name(rng)
            c.execute(
                "INSERT INTO outreach_practice_prospects(name,handle,niche,followers,stage,opener_move,sender,tier,replied,created,demo) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,1)",
                (name, "@" + name.split()[0].lower() + f"{i}_demo", rng.choice(NICHES), rng.randint(800, 40000), "new",
                 moves[i % 4], rng.choice(["demo-setter", "setter-b (DEMO)"]), rng.choice(["warm", "cold", "hot"]),
                 0, now))
        # CRM
        for i in range(10):
            name = _name(rng)
            c.execute("INSERT INTO crm_contacts(name,email,stage,tags,demo,created) VALUES(?,?,?,?,1,?)",
                      (name, _email(name), rng.choice(["new", "contacted", "booked", "showed"]), "[\"DEMO\"]", now))
        # members
        for i in range(8):
            name = _name(rng)
            c.execute("INSERT OR IGNORE INTO members(email,status,plan,updated,demo) VALUES(?,?,?,?,1)",
                      (_email(name), rng.choice(["active", "active", "past_due"]), "core (DEMO)", now))

    tenant.set("seeded_at", now)
    tenant.set("brand", {**(tenant.get("brand", {}) or {}), "name": tenant.get("brand", {}).get("name") or config.get().brand})
    # the demo shows the 2026 Playbooks (external, researched best practice); real workspaces start with it off
    tenant.set("third_party_kb", True)
    _seed_claims(tenant)
    _seed_queue(tenant, rng)
    _seed_questions(tenant)
    _seed_features(tenant)
    tenant.set("demo_features", FEATURE_SEEDERS)
    audit.log(tenant, "system", "demo.reset", {"seeded": counts})
    return counts


# Feature demo seeders, "module:function" called as fn(tenant) after the base seed. Missing modules are skipped.
FEATURE_SEEDERS = [
    "operator_ai.academy:seed_demo", "operator_ai.roleplay:seed_demo", "operator_ai.prospects:seed_demo",
    "operator_ai.missions:seed_demo", "operator_ai.client_ops:seed_demo", "operator_ai.webinars:seed_demo",
    "operator_ai.delivery:seed_demo", "operator_ai.inbound:seed_demo", "operator_ai.persona:seed_demo",
    "operator_ai.team:seed_demo", "operator_ai.claims:seed_demo", "operator_ai.playbooks:seed_demo",
]


def _seed_features(tenant) -> None:
    import importlib

    if not tenancy.is_demo_db(tenant):
        return
    for spec in FEATURE_SEEDERS:
        mod, _, fn = spec.partition(":")
        try:
            f = getattr(importlib.import_module(mod), fn, None)
        except ModuleNotFoundError as e:
            if e.name != mod:
                raise
            continue
        if f is not None:
            f(tenant)


def _day_at(ts: float, hour: int) -> float:
    lt = time.localtime(ts)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, 0, 0, 0, 0, -1))


def _seed_ads(c, rng, now) -> None:
    acct = "act_DEMO_practice"
    c.execute("INSERT INTO mock_ads(id,kind,parent,name,status,daily_budget,data,metrics,updated) VALUES(?,?,?,?,?,?,?,?,?)",
              (acct, "account", None, "Practice ad account (DEMO)", "ACTIVE", 0, "{}", "{}", now))
    for ci, cname in enumerate(["Thunder Dome round 4 (DEMO)", "Scaling: champion creative (DEMO)"]):
        cid = f"cmp_demo_{ci + 1}"
        c.execute("INSERT INTO mock_ads VALUES(?,?,?,?,?,?,?,?,?)",
                  (cid, "campaign", acct, cname, "ACTIVE" if ci else "PAUSED", 0, "{}", "{}", now))
        asid = f"as_demo_{ci + 1}"
        c.execute("INSERT INTO mock_ads VALUES(?,?,?,?,?,?,?,?,?)",
                  (asid, "adset", cid, f"Ad set {ci + 1} (DEMO)", "ACTIVE" if ci else "PAUSED", 30.0 if ci else 0.0,
                   db.jdump({"audience": "Clinic owners 30-55 (DEMO)"}), "{}", now))
        for ai in range(4 if ci == 0 else 1):
            spend = rng.randint(40, 160)
            leads = rng.randint(0, 12)
            impressions = rng.randint(800, 6000)
            clicks = int(impressions * rng.uniform(0.004, 0.02))
            c.execute("INSERT INTO mock_ads VALUES(?,?,?,?,?,?,?,?,?)",
                      (f"ad_demo_{ci + 1}_{ai + 1}", "ad", asid, f"Creative {chr(65 + ai)} (DEMO)", "ACTIVE" if ci else "PAUSED", 0,
                       db.jdump({"hook": rng.choice(["Your front desk is losing patients (DEMO)", "Stop discounting sessions (DEMO)",
                                                     "Referral nights that fill your diary (DEMO)", "3 numbers every clinic should track (DEMO)"])}),
                       db.jdump({"spend": spend, "leads": leads, "impressions": impressions, "clicks": clicks, "hours_live": 96}), now))


def _seed_queue(tenant, rng) -> None:
    from . import queue

    drafts = [
        ("dm", "instagram_dm", "@maya_demo", "Opener to Maya (DEMO)", "Loved your post about referral nights at your clinic. Are they filling up the way you hoped?", {"prospect_facing": True, "lead_tier": "warm"}),
        ("dm", "instagram_dm", "@owen_demo", "Opener to Owen (DEMO)", "Saw your before/after reel. Quick question: where do most of your new patients come from right now?", {"prospect_facing": True}),
        ("followup", "instagram_dm", "@leah_demo", "Follow-up to Leah (DEMO)", "Here's a 2-minute video on the reminder ladder we use to lift show rates. Worth a look?", {"prospect_facing": True}),
        ("followup", "instagram_dm", "@kofi_demo", "Follow-up to Kofi (DEMO)", "Just following up on my last message!", {"prospect_facing": True}),
        ("dm", "instagram_dm", "@nina_demo", "Reply to Nina (DEMO)", "Let me make Northside Physio (DEMO) $50k-$100k a month through our referral-first system. Would a quick call be useful?", {"prospect_facing": True, "lead_tier": "warm"}),
        ("dm", "instagram_dm", "@theo_demo", "Reply to Theo (DEMO)", "Our programme is $3,000 a month and we guarantee results.", {"prospect_facing": True}),
        ("reminder", "instagram_dm", "@ravi_demo", "24h reminder for Ravi (DEMO)", "Looking forward to tomorrow's call. One question to think about: what would 20 more bookings a month change for you?", {}),
        ("reminder", "sms", "+1-555-0101 (DEMO)", "2h reminder for Isla (DEMO)", "Don't forget your call!", {}),
        ("nudge", "telegram", "Zara Okafor (DEMO)", "Nudge: Zara missed a check-in (DEMO)", "Hey Zara, noticed no check-in this week. What's one small win from the last few days?", {}),
        ("nudge", "telegram", "Luca Varga (DEMO)", "Nudge: Luca's show rate dropped (DEMO)", "Your show rate dipped this week. Here's the reminder ladder checklist from the booking module.", {}),
        ("welcome", "email", "mila.duarte@example.invalid", "Welcome Mila (DEMO)", "Welcome to the programme, Mila! Your coach will reach out within 24 hours with your 30/60/90 plan.", {}),
        ("kickoff", "calendar", "jonah.osei@example.invalid", "Kickoff call: Jonah (DEMO)", "Kickoff call to agree your first 30 days.", {}),
        ("calendar_invite", "calendar", "priya.moreau@example.invalid", "Strategy call: Priya (DEMO)", "Strategy call with the closer.", {"attendees": ["priya.moreau@example.invalid"]}),
        ("email", "email", "felix.brennan@example.invalid", "Replay email (DEMO)", "Subject: Your replay is ready\nHere's the replay of today's training. It stays up for 48 hours because we refresh the examples each cohort.", {"client_asset": True}),
        ("email", "email", "hana.lindqvist@example.invalid", "Webinar invite (DEMO)", "Subject: Re: our chat\nDoors closing tonight - last chance!", {"client_asset": True}),
        ("ad_change", "ads", "act_DEMO_practice", "Pause Creative C (DEMO)", "Pause Creative C: cost per lead is 2.4x target after 1.5x target spend (Thunder Dome kill rule).", {}),
        ("intervention", "manual", "Coach Rivera (DEMO)", "Escalation: Soren at risk (DEMO)", "Soren has missed two check-ins and mood is trending down. Book a 15-minute call this week.", {}),
        ("email", "email", "dev.ferreira@example.invalid", "Cohort notice (DEMO)", "Subject: Next cohort\nEnrollment closes on Friday.", {"client_asset": True}),
    ]
    for kind, ch, rcpt, title, body, meta in drafts:
        queue.add(tenant, kind, body, channel=ch, recipient_ref=rcpt, title=title, meta={**meta, "tag": "seed"},
                  actor="system", demo=True)


DEMO_QUESTIONS = [
    "What are the rules for a Thunder Dome test?", "When should reminders go out before a call?",
    "What do I do when a lead no-shows?", "What's the USP formula?", "Can I mention price in a DM?",
    "How long is the new mechanism segment?", "How do I handle 'I need to think about it'?",
    "What show rate should I aim for?", "How often should I follow up after a DM?", "How do I pick a niche?",
    "What goes in a pre-call brief?", "How many creatives go into a Thunder Dome?", "How do I set up TikTok Shop?",
]


def _seed_questions(tenant) -> None:
    try:
        from .agents import coach
    except ImportError:
        return
    with tenant.db() as c:
        uid = c.execute("SELECT id FROM users WHERE username='demo-student'").fetchone()["id"]
    user = {"id": uid, "username": "demo-student", "role": "student"}
    for q in DEMO_QUESTIONS:
        coach.answer(tenant, q, user=user, record=True, thread=True)


# --------------------------------------------------------------------------- reset
DEMO_BACKUPS_KEEP = 5
# Settings a reset never touches: branding, the demo marker, every server-only switch and, crucially, the
# demo's AI budgets *and what has been used of them*, so resetting the demo never refills its AI budget.
RESET_KEEP = ("brand", "is_demo", "live_meta_enabled", "meta_allowlist", "live_calendar_enabled", "live_channels_enabled",
              "ai_token_budget", "ai_token_used", "critique_token_budget", "critique_token_used", "demo_course_material")


def backup(tenant) -> Path:
    """Copy the demo DB before a reset. Only the newest DEMO_BACKUPS_KEEP demo copies are kept, so repeated
    resets can't fill the disk."""
    src = tenant.db_path
    dest_dir = config.get().data_dir / "backups"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"demo-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}.db"
    s = sqlite3.connect(str(src))
    d = sqlite3.connect(str(dest))
    try:
        s.backup(d)
    finally:
        s.close()
        d.close()
    dest.chmod(0o600)
    old = sorted(dest_dir.glob("demo-*.db"), key=lambda p: (p.stat().st_mtime, p.name))
    for p in old[:-DEMO_BACKUPS_KEEP]:
        if p != dest:
            p.unlink(missing_ok=True)
    return dest


def reset(tenant, actor: str = "system") -> dict:
    """Back up, wipe and re-seed the demo DB. Refused on any database that isn't the demo DB."""
    if not tenancy.is_demo_db(tenant):
        raise DemoResetRefused("Refused: demo resets only run against the demo database, never the real workspace.")
    with _lock:
        return _reset_locked(tenant, actor)


def _reset_locked(tenant, actor: str) -> dict:
    if not tenancy.is_demo_db(tenant):
        raise DemoResetRefused("Refused: demo resets only run against the demo database, never the real workspace.")
    bk = backup(tenant)
    with tenant.db() as c:
        tables = [r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        with db.tx(c):
            for t in tables:
                if t in ("settings", "audit"):
                    continue
                c.execute(f"DELETE FROM {t}")
            c.execute(f"DELETE FROM settings WHERE key NOT IN ({','.join('?' * len(RESET_KEEP))})", RESET_KEEP)
    try:
        from . import claims

        claims.reset_files(tenant)
    except ImportError:  # pragma: no cover
        pass
    # anything added to the demo's own knowledge-base folder (before KB writes were refused there) goes too
    if tenant.kb_dir.exists() and any(tenant.kb_dir.glob("*.txt")):
        from . import index

        for f in tenant.kb_dir.glob("*.txt"):
            f.unlink(missing_ok=True)
        index.invalidate()
    tenancy.ensure_demo_budget(tenant)
    counts = seed(tenant)
    audit.log(tenant, actor, "demo.reset", {"backup": bk.name})
    return {"ok": True, "backup": bk.name, "seeded": counts}
