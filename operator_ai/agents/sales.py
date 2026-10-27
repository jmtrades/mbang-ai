"""Sales agent: lead qualification and booking.

Transparent rule scoring (hot / warm / cold) with owner-editable weights, routing to setter,
closer or nurture, a course-grounded setter script and pre-call brief, a reply whose first
line follows the outreach money rule, and an event-driven booking sequence:

    process_lead    -> the reply is queued; value-only follow-ups run as a cadence that starts only
                       once the reply is confirmed sent (delivery.register_cadence)
    record_reply    -> reply_received; follow-ups stop; a positive reply gets a booking DM with FRESH
                       free times (meta.slots_valid_until; an expired draft is redrafted, never sent stale)
    pick_slot       -> the owner-only calendar invite for the time they picked
    invite approved -> one calendar event + the show-up ladder, each reminder with meta.send_after and
                       cancel_if ["booking_cancelled"], addressed to the lead's real handle or email

Everything is queued for approval. Plus the 500-lead burst through the durable job queue.
"""
from __future__ import annotations

import random
import re
import secrets
import time

from .. import calendar, db, events, guardrails, jobs, queue, retrieval, runtime

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
LADDER_BODY = {
    "24h": "Looking forward to our call. One question to think about before we talk: what would ten more booked calls "
           "a month change for you?",
    "2h": "See you in two hours. Here's a short video worth a look before we talk: how the reminder ladder lifts show rates.",
    "10min": "Starting in ten minutes. Quick tip: have your current numbers to hand so we can use the time well.",
}
SLOTS_VALID_FOR = 24 * 3600


class NoSlots(Exception):
    """The calendar has no free time to offer."""

    def __init__(self, msg: str = "No free 45-minute times in the next two weeks (weekdays 09:00 to 17:00). Free up some "
                                  "time in the calendar, or ask them which times suit them."):
        super().__init__(msg)


class BookingError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


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
        "Follow-up sequence (starts once the reply is sent)", "Booking waits for a positive reply"]


def _contact(lead: dict) -> tuple[str | None, str]:
    """(recipient, channel): the lead's handle by Instagram DM, else their email."""
    if lead.get("handle"):
        return lead["handle"], "instagram_dm"
    if lead.get("email"):
        return lead["email"], "email"
    return None, "instagram_dm"


def process_lead(tenant, lead_id: int, user: dict | None = None, full: bool = True) -> dict:
    from .. import delivery

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
        rcpt, channel = _contact(lead)
        reply = reply_line(lead, sc["tier"]) + " Would a quick call this week be useful?"
        q_reply = run.step("reply", {"tier": sc["tier"]},
                           lambda: queue.add(tenant, "setter_reply", reply, channel=channel, recipient_ref=rcpt,
                                             title=f"Reply to {lead['name']}", meta=meta, run_id=run.id, actor=actor, demo=demo),
                           "Reply draft (money rule)")
        out = {"lead_id": lead_id, **sc, "script": script, "brief": brief, "queue_ids": [q_reply["id"]],
               "reply_status": q_reply["status"]}
        out["cadence_id"] = run.step(
            "cadence", {"steps": 3},
            lambda: delivery.register_cadence(tenant, ("lead", lead_id), q_reply["id"], channel, rcpt, template="lead",
                                              lead_tier=sc["tier"], context={"followups": followups(lead)}, demo=demo),
            "Follow-up sequence (starts once the reply is sent)")
        if sc["tier"] == "cold":
            run.note("booking", {}, "Cold lead goes to nurture: no booking proposed", label="Booking waits for a positive reply",
                     status="skipped")
            out["next"] = "Cold lead: value-only follow-ups, then nurture. No booking is proposed."
        else:
            run.note("booking", {}, "Times are offered only after a positive reply, so they are fresh when sent",
                     label="Booking waits for a positive reply")
            out["next"] = "When they reply, record it here: a positive reply drafts a booking message with fresh times."
        return out

    res = runtime.execute(tenant, "sales", f"Lead: {lead['name']}", PLAN, fn, actor)
    return {**res["result"], "run_id": res["run_id"]}


# --------------------------------------------------------------------------- replies, times, picks
_STOP = re.compile(r"\b(stop|unsubscribe|remove me|do not (contact|message) me|don'?t (contact|message) me)\b", re.I)
_NEGATIVE = re.compile(r"\b(no thanks|no thank you|not interested|not right now|not now|not for (me|us)|no longer|"
                       r"leave (me|us) alone|pass on this)\b", re.I)
_POSITIVE = re.compile(r"\b(yes|yeah|yep|sure|keen|interested|sounds (good|great)|let'?s|love to|happy to|okay|ok|"
                       r"book|works for me|when can|send (me )?(the )?times|go ahead)\b", re.I)
CLASSES = ("positive", "negative", "question", "neutral", "stop")


def classify_reply(text: str) -> str:
    """Rules (AI off): stop > negative > positive > question > neutral."""
    t = (text or "").strip()
    if _STOP.search(t):
        return "stop"
    if _NEGATIVE.search(t):
        return "negative"
    if _POSITIVE.search(t):
        return "positive"
    if "?" in t:
        return "question"
    return "neutral"


def _booking_body(slots: list[dict]) -> str:
    times = ", ".join(s["label"] for s in slots)
    lead_in = "Here are three times that work" if len(slots) == 3 else "Here are some times that work"
    return f"Great to hear back. {lead_in}: {times}. Which suits you best?"


def _valid_until(slots: list[dict], now: float | None = None) -> float:
    now = now or time.time()
    return min(now + SLOTS_VALID_FOR, slots[0]["start"] - 3600)


def refresh_booking_draft(tenant, item: dict) -> tuple[str, dict]:
    """Fresh times for a booking DM whose times expired (raises NoSlots)."""
    slots = _slots(tenant)
    if not slots:
        raise NoSlots()
    meta = {**item["meta"], "slots": slots, "slots_valid_until": _valid_until(slots)}
    return _booking_body(slots), meta


def _offer(tenant, lead: dict, run_id: int | None, actor: str) -> dict:
    slots = _slots(tenant)
    if not slots:
        raise NoSlots()
    rcpt, channel = _contact(lead)
    meta = {"prospect_facing": True, "lead_tier": lead.get("tier") or "warm", "lead_id": lead["id"], "prior_contact": True,
            "slots": slots, "slots_valid_until": _valid_until(slots)}
    q = queue.add(tenant, "booking_dm", _booking_body(slots), channel=channel, recipient_ref=rcpt,
                  title=f"Booking times for {lead['name']}", meta=meta, run_id=run_id, actor=actor, demo=bool(lead.get("demo")))
    return {"booking_dm": q["id"], "status": q["status"], "slots": slots, "slots_valid_until": meta["slots_valid_until"]}


def record_reply(tenant, lead_id: int, text: str, classification: str | None = None, user: dict | None = None) -> dict:
    """A reply from the lead: follow-ups stop; a positive reply gets a booking DM with fresh times."""
    from .. import delivery

    lead = _get_lead(tenant, lead_id)
    actor = f"{user['username']}({user['role']})" if user else "system"
    if classification is not None and classification not in CLASSES:
        raise BookingError(422, f"Unknown reply type. Use one of: {', '.join(CLASSES)}")

    def fn(run: runtime.Run):
        cls = classification or run.step("classify", {"text": text[:200]}, lambda: classify_reply(text),
                                         "Classify the reply (rules, AI off)")
        rcpt, channel = _contact(lead)
        with tenant.db() as c:
            c.execute("INSERT INTO channel_messages(channel,direction,ext_id,body,meta,created) VALUES(?,?,?,?,?,?)",
                      (channel, "in", rcpt, (text or "")[:4000], db.jdump({"lead_id": lead_id, "recorded_by": actor}), db.now()))
            c.execute("UPDATE leads SET stage='replied' WHERE id=? AND stage IN ('new','nurture')", (lead_id,))
        events.emit(tenant, "reply_received", {"party_type": "lead", "party_id": lead_id, "owner_user_id": None,
                                               "classification": cls})
        out = {"lead_id": lead_id, "classification": cls, "mode": "rules", "label": "Classified by rules (AI off)",
               "booking": None}
        if cls == "stop":
            delivery.set_dnc(tenant, ("lead", lead_id), actor, "asked to stop")
            for a in (lead.get("email"), lead.get("handle")):
                if a:
                    delivery.suppress(tenant, a, None, "asked to stop", ("lead", lead_id))
            out["next"] = "They asked not to be contacted: marked do-not-contact and nothing more will be sent."
        elif cls == "positive":
            try:
                out["booking"] = run.step("offer", {}, lambda: _offer(tenant, lead, run.id, actor), "Offer fresh times")
                out["next"] = "Approve the booking message. When they pick a time, record it to draft the invite."
            except NoSlots as e:
                out["booking"] = {"no_slots": True, "message": str(e)}
                out["next"] = str(e)
        else:
            out["next"] = "Follow-ups stopped because they replied. Answer them yourself, or offer times when they're ready."
        return out

    res = runtime.execute(tenant, "sales", f"Reply from {lead['name']}",
                          ["Record the reply", "Classify (rules)", "Stop follow-ups", "Offer fresh times"], fn, actor)
    return {**res["result"], "run_id": res["run_id"]}


def offer_times(tenant, lead_id: int, user: dict | None = None) -> dict:
    """Draft a booking DM with fresh free times (NoSlots when the calendar is full)."""
    lead = _get_lead(tenant, lead_id)
    actor = f"{user['username']}({user['role']})" if user else "system"
    if lead.get("do_not_contact"):
        raise BookingError(409, "This lead is marked do-not-contact.")
    res = runtime.execute(tenant, "sales", f"Offer times: {lead['name']}", ["Find free times", "Draft the booking message"],
                          lambda run: _offer(tenant, lead, run.id, actor), actor)
    return {**res["result"], "run_id": res["run_id"]}


def latest_booking_dm(tenant, key: str, party_id: int) -> dict | None:
    with tenant.db() as c:
        r = c.execute(f"SELECT id FROM queue WHERE kind='booking_dm' AND json_extract(meta,'$.{key}')=? AND status NOT IN "
                      "('rejected','cancelled') ORDER BY id DESC LIMIT 1", (party_id,)).fetchone()
    return queue.get(tenant, r["id"], scrub=False) if r else None


def choose_slot(tenant, bdm: dict | None, index: int | None = None, start: float | None = None) -> dict:
    """The slot a lead picked from the latest booking DM, checked for expiry and that it is still free."""
    if bdm is None:
        raise BookingError(409, "No times have been offered yet. Offer times first.")
    if bdm["status"] in queue.OPEN + ("sending",):
        raise BookingError(409, "Approve the booking message first: they can only pick from times they were sent.")
    if bdm["status"] == "scheduled":
        raise BookingError(409, "The booking message hasn't gone out yet (it's scheduled). Send it first.")
    if bdm["status"] not in ("sent", "awaiting_manual_send", "approved"):
        raise BookingError(409, "The booking message with these times was never sent. Offer fresh times first.")
    slots = bdm["meta"].get("slots") or []
    if not slots:
        raise BookingError(409, "That message offered no times. Offer times first.")
    if (queue._num(bdm["meta"].get("slots_valid_until")) or 0) < time.time():
        raise BookingError(409, "Those times have expired. Offer fresh times first.")
    if start is not None:
        pick = next((s for s in slots if abs(float(s["start"]) - float(start)) < 1), None)
    elif index is not None and 0 <= index < len(slots):
        pick = slots[index]
    else:
        pick = None
    if pick is None:
        raise BookingError(422, f"Pick one of the {len(slots)} offered times.")
    if calendar.busy(tenant, pick["start"], pick["end"]):
        raise BookingError(409, "That time has just been taken. Offer fresh times.")
    return pick


def pick_slot(tenant, lead_id: int, index: int | None = None, start: float | None = None, user: dict | None = None) -> dict:
    """The lead picked a time: queue the owner-only calendar invite for it."""
    lead = _get_lead(tenant, lead_id)
    actor = f"{user['username']}({user['role']})" if user else "system"
    pick = choose_slot(tenant, latest_booking_dm(tenant, "lead_id", lead_id), index, start)

    def fn(run: runtime.Run):
        inv = queue.add(tenant, "calendar_invite", f"Strategy call with {lead['name']} at {pick['label']}.", channel="calendar",
                        recipient_ref=lead["email"], title=f"Invite: {lead['name']}",
                        meta={"lead_id": lead_id, "slot": pick, "attendees": [lead["email"]] if lead["email"] else [],
                              "owner_only": True}, run_id=run.id, actor=actor, demo=bool(lead.get("demo")))
        with tenant.db() as c:
            c.execute("UPDATE leads SET stage='booking' WHERE id=?", (lead_id,))
        run.note("invite", {"slot": pick["label"]}, {"queue_id": inv["id"]}, label="Queue the invite (owner approves)")
        return {"invite": inv["id"], "status": inv["status"], "slot": pick}

    res = runtime.execute(tenant, "sales", f"Time picked: {lead['name']}",
                          ["Check the time is still free", "Queue the invite (owner approves)"], fn, actor)
    return {**res["result"], "run_id": res["run_id"]}


def queue_ladder(tenant, invite: dict, party: tuple[str, int], event_id: int | None, slot: dict) -> list[int]:
    """Show-up reminders for an approved call: each waits for its time (send_after) and is cancelled if the
    call is cancelled. Reminders whose time has passed are sent now unless a later one already covers it."""
    from .. import delivery

    row = delivery._party_row(tenant, party) or {}
    rcpt, channel = _contact({"handle": row.get("handle"), "email": row.get("email")})
    name = row.get("name") or "the lead"
    tier = row.get("tier") or "warm"
    key = delivery.PARTY_META[party[0]]
    now = time.time()
    start = float(slot["start"])
    ids = []
    for i, (label, before) in enumerate(LADDER):
        at = start - before
        if at <= now:
            later = LADDER[i + 1][1] if i + 1 < len(LADDER) else 0
            if start - now <= later + 900:
                continue  # a later reminder covers it
            at = now
        meta = {key: party[1], "ladder": label, "send_after": at, "cancel_if": ["booking_cancelled"],
                "calendar_event_id": event_id, "prospect_facing": True, "lead_tier": tier, "prior_contact": True,
                "invite_queue_id": invite["id"]}
        if channel == "email":
            meta["subject"] = "Our call"
        ids.append(queue.add(tenant, "reminder", LADDER_BODY[label], channel=channel, recipient_ref=rcpt,
                             title=f"{label} reminder: {name}", meta=meta, run_id=invite.get("run_id"), actor="sales",
                             demo=bool(invite.get("demo")))["id"])
    return ids


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
    """An approved invite: exactly one calendar event (a live Google invite already wrote its local mirror),
    booking_made, and the show-up ladder for the lead it belongs to."""
    slot = item["meta"].get("slot")
    if not slot:
        return
    delivered = item.get("delivered") or ""
    with tenant.db() as c:
        if delivered.startswith("live:"):
            r = c.execute("SELECT id FROM calendar_events WHERE ext_id=? ORDER BY id DESC LIMIT 1",
                          (delivered.rsplit(":", 1)[1],)).fetchone()
        else:
            r = c.execute("SELECT id FROM calendar_events WHERE ext_id=?", (f"local-q{item['id']}",)).fetchone()
    ev = r["id"] if r else calendar.add_event(tenant, item["title"] or "Call", slot["start"], slot["end"],
                                              item["meta"].get("attendees", []), ext_id=f"local-q{item['id']}",
                                              demo=bool(item.get("demo")))
    meta = item["meta"]
    party = (("chain_lead", int(meta["chain_lead_id"])) if meta.get("chain_lead_id") else
             ("lead", int(meta["lead_id"])) if meta.get("lead_id") else None)
    with tenant.db() as c:
        c.execute("UPDATE calendar_events SET party_type=?, party_id=?, queue_id=? WHERE id=?",
                  (party[0] if party else None, party[1] if party else None, item["id"], ev))
        if party and party[0] == "lead":
            c.execute("UPDATE leads SET stage='booked' WHERE id=?", (party[1],))
        if party and party[0] == "chain_lead" and not delivered.startswith("live:"):
            c.execute("UPDATE chain_bookings SET status='booked', event_id=? WHERE queue_id=?", (f"local-q{item['id']}", item["id"]))
            c.execute("UPDATE chain_leads SET stage='booked' WHERE id=?", (party[1],))
    with tenant.db() as c:
        laddered = c.execute("SELECT 1 FROM queue WHERE kind='reminder' AND json_extract(meta,'$.invite_queue_id')=? LIMIT 1",
                             (item["id"],)).fetchone()
    if party and not laddered:
        events.emit(tenant, "booking_made", {"party_type": party[0], "party_id": party[1], "starts_at": slot["start"],
                                             "event_id": ev, "queue_id": item["id"]})
        ids = queue_ladder(tenant, item, party, ev, slot)
        if party[0] == "chain_lead":
            from ..live import chain

            chain._event(tenant, party[1], "ladder_queued", {"queue_ids": ids})
