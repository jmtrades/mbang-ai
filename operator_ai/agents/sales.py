"""Sales agent: lead qualification and booking.

Transparent rule scoring (hot / warm / cold) with owner-editable weights, routing to setter,
closer or nurture, a course-grounded setter script and pre-call brief, a reply whose first
line follows the outreach money rule, value-only follow-ups, three proposed slots from the
calendar, and the booking proposal + calendar invite (owner-only) + show-up ladder, all
queued for approval. Plus the 500-lead burst through the durable job queue.
"""
from __future__ import annotations

import random
import secrets
import time

from .. import calendar, db, guardrails, jobs, queue, retrieval, runtime

DEFAULT_WEIGHTS = {
    "monthly_revenue": {"<10k": 0, "10k-30k": 15, "30k-100k": 25, "100k+": 30},
    "can_invest": {"yes": 25, "maybe": 10, "no": 0},
    "timeline": {"now": 20, "1-3 months": 10, "later": 0},
    "decision_maker": {"true": 15, "false": 0},
    "source": {"referral": 10, "webinar": 8, "lead_magnet": 6, "meta_lead_form": 5, "instagram_dm": 5},
}
TIERS = {"hot": 70, "warm": 40}
ROUTES = {"hot": "closer", "warm": "setter", "cold": "nurture"}
LADDER = [("24h", 24 * 3600), ("2h", 2 * 3600), ("10min", 600)]


def weights(tenant) -> dict:
    w = tenant.get("lead_weights") or {}
    out = {k: dict(v) for k, v in DEFAULT_WEIGHTS.items()}
    for k, v in w.items():
        if k in out and isinstance(v, dict):
            out[k].update({str(kk): float(vv) for kk, vv in v.items()})
    return out


def score(answers: dict, source: str, w: dict) -> dict:
    """Transparent scoring: every point is explained."""
    factors = []
    total = 0.0
    for key in ("monthly_revenue", "can_invest", "timeline", "decision_maker"):
        val = answers.get(key)
        sval = str(val).lower() if isinstance(val, bool) else str(val)
        pts = w[key].get(sval, 0)
        factors.append({"factor": key, "value": val, "points": pts})
        total += pts
    pts = w["source"].get(source or "", 0)
    factors.append({"factor": "source", "value": source, "points": pts})
    total += pts
    total = min(100, round(total))
    tier = "hot" if total >= TIERS["hot"] else "warm" if total >= TIERS["warm"] else "cold"
    return {"score": total, "tier": tier, "route": ROUTES[tier], "factors": factors}


def _brand_of(lead: dict) -> str:
    a = lead.get("answers") or {}
    return a.get("business") or "your business"


def reply_line(lead: dict, tier: str) -> str:
    """Line 1 follows the money rule: warm/hot get the hedged range, cold gets the no-figure line."""
    brand = _brand_of(lead)
    if tier in ("hot", "warm"):
        return guardrails.range_line(brand, "our booking system")
    return guardrails.no_figure_line(brand, "our booking system")


def followups(lead: dict) -> list[str]:
    goal = (lead.get("answers") or {}).get("goal", "more booked calls")
    return [
        f"Here's a 2-minute video on how clinics like yours get {goal} without discounting. Want me to send it?",
        "Quick tip from our booking module: confirm every call within five minutes. What does your confirmation look like today?",
    ]


def setter_script(tenant, lead: dict, tier: str) -> dict:
    hits = retrieval.search(tenant, "DM opener one question per message follow-up offer the call", k=3)
    cites = [retrieval.cite({**h, "id": f"S{i + 1}"}) for i, h in enumerate(hits)]
    a = lead.get("answers") or {}
    lines = [
        f"Opener: Saw {_brand_of(lead)} is working on {a.get('goal', 'growth')}. What's the main thing holding that back right now? [S1]",
        "Qualify: What are your numbers like today, roughly how many new clients a month? (one question per message) [S1]",
        f"Offer the call: {reply_line(lead, tier)} Would a quick call be useful? [S{min(2, len(cites))}]",
    ]
    return {"lines": lines, "citations": cites}


def precall_brief(tenant, lead: dict) -> dict:
    a = lead.get("answers") or {}
    hits = retrieval.search(tenant, "pre-call brief goal current numbers main obstacle what they said in the DMs", k=2)
    cites = [retrieval.cite({**h, "id": f"S{i + 1}"}) for i, h in enumerate(hits)]
    return {
        "goal": a.get("goal"), "current_numbers": a.get("monthly_revenue"), "main_obstacle": a.get("obstacle") or "Not stated yet",
        "said_in_dms": a.get("notes") or "Answered the qualification form",
        "timeline": a.get("timeline"), "decision_maker": a.get("decision_maker"), "citations": cites,
        "why": "A closer with a brief sounds like a doctor who read the chart [S1].",
    }


def _get_lead(tenant, lead_id: int) -> dict:
    with tenant.db() as c:
        r = db.row(c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())
    if r is None:
        raise KeyError(lead_id)
    r["answers"] = db.jload(r["answers"], {})
    return r


PLAN = ["Score lead (rules)", "Route", "Setter script (cited)", "Pre-call brief", "Reply draft (money rule)",
        "Value-only follow-ups", "Propose 3 slots", "Queue booking, invite and show-up ladder"]


def process_lead(tenant, lead_id: int, user: dict | None = None, full: bool = True) -> dict:
    actor = f"{user['username']}({user['role']})" if user else "system"
    lead = _get_lead(tenant, lead_id)
    demo = bool(lead.get("demo"))

    def fn(run: runtime.Run):
        sc = run.step("score", {"answers": lead["answers"], "source": lead["source"]},
                      lambda: score(lead["answers"], lead["source"], weights(tenant)), "Score lead (rules)")
        with tenant.db() as c:
            c.execute("UPDATE leads SET tier=?, score=?, route=? WHERE id=?", (sc["tier"], sc["score"], sc["route"], lead_id))
        run.note("route", {"tier": sc["tier"]}, sc["route"], label="Route")
        script = run.step("setter_script", {"tier": sc["tier"]}, lambda: setter_script(tenant, lead, sc["tier"]),
                          "Setter script (cited)")
        brief = run.step("precall_brief", {}, lambda: precall_brief(tenant, lead), "Pre-call brief")
        meta = {"prospect_facing": True, "lead_tier": sc["tier"], "lead_id": lead_id, "prior_contact": True}
        rcpt = lead["handle"] or lead["email"]
        reply = reply_line(lead, sc["tier"]) + " Would a quick call this week be useful?"
        q_reply = run.step("reply", {"tier": sc["tier"]},
                           lambda: queue.add(tenant, "setter_reply", reply, channel="instagram_dm", recipient_ref=rcpt,
                                             title=f"Reply to {lead['name']}", meta=meta, run_id=run.id, actor=actor, demo=demo),
                           "Reply draft (money rule)")
        out = {"lead_id": lead_id, **sc, "script": script, "brief": brief, "queue_ids": [q_reply["id"]],
               "reply_status": q_reply["status"]}
        fus = run.step("followups", {}, lambda: [
            queue.add(tenant, "followup", t, channel="instagram_dm", recipient_ref=rcpt, title=f"Follow-up {i + 1} to {lead['name']}",
                      meta={**meta, "step": i + 1}, run_id=run.id, actor=actor, demo=demo)["id"]
            for i, t in enumerate(followups(lead))], "Value-only follow-ups")
        out["queue_ids"] += fus
        if sc["tier"] == "cold":
            run.note("slots", {}, "Cold lead goes to nurture: no booking proposed", label="Propose 3 slots", status="skipped")
            run.note("booking", {}, "skipped", label="Queue booking, invite and show-up ladder", status="skipped")
            return out
        slots = run.step("slots", {}, lambda: _slots(tenant), "Propose 3 slots")
        out["slots"] = slots

        def _queue_booking():
            ids = []
            times = ", ".join(s["label"] for s in slots)
            ids.append(queue.add(tenant, "booking_dm", f"Here are three times that work: {times}. Which suits you best?",
                                 channel="instagram_dm", recipient_ref=rcpt, title=f"Booking times for {lead['name']}",
                                 meta={**meta, "slots": slots}, run_id=run.id, actor=actor, demo=demo)["id"])
            s0 = slots[0]
            ids.append(queue.add(tenant, "calendar_invite", f"Strategy call with {lead['name']} at {s0['label']}.",
                                 channel="calendar", recipient_ref=lead["email"], title=f"Invite: {lead['name']}",
                                 meta={"lead_id": lead_id, "slot": s0, "attendees": [lead["email"]], "owner_only": True},
                                 run_id=run.id, actor=actor, demo=demo)["id"])
            for label, before in LADDER:
                body = {"24h": "Looking forward to tomorrow. One question to think about: what would ten more booked calls a month change for you?",
                        "2h": "See you in two hours. Here's a short video worth a look before we talk: how the reminder ladder lifts show rates.",
                        "10min": "Starting in ten minutes. Quick tip: have your current numbers to hand so we can use the time well."}[label]
                ids.append(queue.add(tenant, "reminder", body, channel="sms", recipient_ref=rcpt, title=f"{label} reminder: {lead['name']}",
                                     meta={"lead_id": lead_id, "send_at": s0["start"] - before, "ladder": label},
                                     run_id=run.id, actor=actor, demo=demo)["id"])
            return ids

        out["queue_ids"] += run.step("booking", {"slot": slots[0] if slots else None}, _queue_booking,
                                     "Queue booking, invite and show-up ladder")
        return out

    res = runtime.execute(tenant, "sales", f"Lead: {lead['name']}", PLAN, fn, actor)
    return {**res["result"], "run_id": res["run_id"]}


def _slots(tenant) -> list[dict]:
    if tenant.get("live_calendar_enabled", False):
        from ..live import gcal

        return gcal.free_slots(tenant, 3)
    return calendar.free_slots(tenant, 3)


# --------------------------------------------------------------------------- burst
def quick_lead(tenant, lead_id: int) -> dict:
    """Burst path: score, route and queue one template reply (no AI)."""
    lead = _get_lead(tenant, lead_id)
    sc = score(lead["answers"], lead["source"], weights(tenant))
    with tenant.db() as c:
        c.execute("UPDATE leads SET tier=?, score=?, route=? WHERE id=?", (sc["tier"], sc["score"], sc["route"], lead_id))
    reply = reply_line(lead, sc["tier"]) + " Would a quick call this week be useful?"
    q = queue.add(tenant, "setter_reply", reply, channel="instagram_dm", recipient_ref=lead["handle"],
                  title=f"Reply to {lead['name']}", meta={"prospect_facing": True, "lead_tier": sc["tier"], "prior_contact": True,
                                                          "lead_id": lead_id, "tag": f"burst:{lead['batch']}"},
                  actor="burst", demo=bool(lead["demo"]))
    return {"tier": sc["tier"], "queue_id": q["id"], "status": q["status"]}


@jobs.handler("sales.quick_lead")
def _job_quick(tenant, payload: dict) -> dict:
    return quick_lead(tenant, int(payload["lead_id"]))


def start_burst(tenant, n: int = 500, seed: int | None = None) -> dict:
    """Create n DEMO leads and push each through the job queue (idempotency key per lead)."""
    rng = random.Random(seed if seed is not None else time.time())
    batch = f"burst-{time.strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(2)}"
    from ..demo import FIRST, LAST, NICHES

    rows = []
    t = db.now()
    for i in range(n):
        name = f"{rng.choice(FIRST)} {rng.choice(LAST)} (DEMO)"
        answers = {"monthly_revenue": rng.choice(["<10k", "10k-30k", "30k-100k", "100k+"]),
                   "can_invest": rng.choice(["yes", "maybe", "no"]), "timeline": rng.choice(["now", "1-3 months", "later"]),
                   "decision_maker": rng.choice([True, False]), "goal": "more booked calls", "niche": rng.choice(NICHES),
                   "business": f"Burst Lead {i + 1} Co (DEMO)"}
        rows.append((name, f"@burst{i}_demo", f"burst{i}@example.invalid", rng.choice(list(DEFAULT_WEIGHTS["source"])),
                     db.jdump(answers), "new", t, 1, f"L-{secrets.token_hex(3)}", batch))
    with tenant.db() as c, db.tx(c):
        c.executemany("INSERT INTO leads(name,handle,email,source,answers,stage,created,demo,anon_id,batch) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      rows)
        ids = [r["id"] for r in c.execute("SELECT id FROM leads WHERE batch=? ORDER BY id", (batch,))]
    jobs.enqueue_many(tenant, "sales.quick_lead", [(f"lead:{lid}", {"lead_id": lid}) for lid in ids], batch)
    return {"batch": batch, "leads": len(ids)}


def burst_status(tenant, batch: str) -> dict:
    st = jobs.batch_status(tenant, batch)
    with tenant.db() as c:
        st["tiers"] = {r["tier"] or "unscored": r["n"] for r in c.execute(
            "SELECT tier, COUNT(*) n FROM leads WHERE batch=? GROUP BY tier", (batch,))}
    st["complete"] = st["total"] > 0 and st["done"] + st["failed"] == st["total"]
    return st


# --------------------------------------------------------------------------- lead rescue desk
def rescue_desk(tenant, stalled_days: int = 5) -> list[dict]:
    """Stalled leads by anonymous id only (no names), with a value-only rescue suggestion."""
    cutoff = db.now() - stalled_days * 86400
    with tenant.db() as c:
        rows = db.rows(c.execute(
            "SELECT id, anon_id, stage, tier, created FROM leads WHERE stage IN ('replied','quality_conversation','link_sent','booked') "
            "AND created < ? ORDER BY created LIMIT 100", (cutoff,)))
    tips = {"replied": "Send a short, useful video related to their goal and ask one question.",
            "quality_conversation": "Recap their goal in one line and offer the call.",
            "link_sent": "Offer two specific times instead of the link.",
            "booked": "Send the 24h value reminder and ask them to reply to confirm."}
    return [{"lead_id": r["id"], "anon_id": r["anon_id"], "stage": r["stage"], "tier": r["tier"],
             "days_stalled": int((db.now() - r["created"]) / 86400), "suggestion": tips.get(r["stage"], "")} for r in rows]


@queue.on_approve("calendar_invite")
def _invite_approved(tenant, item: dict, actor: str) -> None:
    """An approved invite on the mock calendar becomes a local calendar event (no real invite)."""
    slot = item["meta"].get("slot")
    if slot and item.get("delivered", "") != "live":
        calendar.add_event(tenant, item["title"] or "Call", slot["start"], slot["end"], item["meta"].get("attendees", []),
                           ext_id=f"local-q{item['id']}", demo=bool(item.get("demo")))
