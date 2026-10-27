"""The live lead chain, end to end (built and tested against fakes; switched off by default):

signed Meta lead webhook -> tenant inbox (UNIQUE dedupe key) -> lead fetch with the tenant's
token + appsecret_proof (only with the live switch on; practice leads use the webhook body)
-> qualification with citations from the tenant's own KB scope -> first reply (queue, to the lead's
real handle or email) -> value follow-ups as a cadence once that reply is confirmed sent -> recorded
reply -> booking DM with fresh free slots -> the time they pick -> calendar invite (owner approval)
-> Google event with a deterministic id -> show-up ladder (send_after, cancelled with the booking)
-> outcome -> report.

The inbox gives up after 8 attempts and marks the item dead.
"""
from __future__ import annotations

import json
import time

from .. import audit, calendar, db, events, jobs, queue, retrieval, runtime
from ..agents import sales
from . import gcal, meta, net

MAX_INBOX_ATTEMPTS = 8
INBOX_BACKOFF = 2.0  # seconds; attempt n waits INBOX_BACKOFF * 2**n (capped)


def _event(tenant, lead_id: int | None, kind: str, detail) -> None:
    with tenant.db() as c:
        c.execute("INSERT INTO chain_events(lead_id,kind,detail,created) VALUES(?,?,?,?)",
                  (lead_id, kind, db.jdump(detail), db.now()))


def receive_webhook(tenant, raw: bytes, signature: str | None) -> dict:
    meta.verify_signature(tenant, raw, signature)
    try:
        payload = json.loads(raw)
    except ValueError:
        raise meta.WebhookRejected(422, "Malformed JSON")
    accepted, dupes = [], 0
    for entry in payload.get("entry", []) or []:
        for ch in entry.get("changes", []) or []:
            if ch.get("field") != "leadgen":
                continue
            v = ch.get("value") or {}
            lid = str(v.get("leadgen_id") or "")
            if not lid:
                continue
            with tenant.db() as c:
                cur = c.execute("INSERT OR IGNORE INTO chain_inbox(dedupe_key,body,status,attempts,next_at,created) "
                                "VALUES(?,?,?,?,?,?)", (lid, db.jdump(v), "queued", 0, db.now(), db.now()))
                if cur.rowcount == 0:
                    dupes += 1
                    continue
                iid = cur.lastrowid
            jobs.enqueue(tenant, "chain.process", {"inbox_id": iid}, key=f"chain:{lid}:0")
            accepted.append(iid)
    return {"accepted": len(accepted), "duplicates": dupes, "inbox_ids": accepted}


@jobs.handler("chain.process")
def _job(tenant, payload: dict) -> dict:
    return process_inbox(tenant, int(payload["inbox_id"]))


def process_inbox(tenant, inbox_id: int) -> dict:
    with tenant.db() as c:
        row = db.row(c.execute("SELECT * FROM chain_inbox WHERE id=?", (inbox_id,)).fetchone())
    if row is None or row["status"] in ("done", "dead"):
        return {"skipped": True}
    body = db.jload(row["body"], {})
    try:
        lead = _qualify(tenant, body)
    except Exception as e:  # noqa: BLE001 - every failure is counted; the inbox retries up to 8 times
        attempts = row["attempts"] + 1
        err = net.redact(f"{type(e).__name__}: {e}")
        if attempts >= MAX_INBOX_ATTEMPTS:
            with tenant.db() as c:
                c.execute("UPDATE chain_inbox SET status='dead', attempts=?, error=? WHERE id=?", (attempts, err, inbox_id))
            audit.log(tenant, "chain", "action", {"inbox": inbox_id, "status": "dead"})
            return {"dead": True, "attempts": attempts}
        delay = min(300.0, INBOX_BACKOFF * 2 ** attempts)
        with tenant.db() as c:
            c.execute("UPDATE chain_inbox SET status='retry', attempts=?, error=?, next_at=? WHERE id=?",
                      (attempts, err, db.now() + delay, inbox_id))
        jobs.enqueue(tenant, "chain.process", {"inbox_id": inbox_id}, key=f"chain:{row['dedupe_key']}:{attempts}",
                     delay=delay)
        return {"retry": True, "attempts": attempts}
    with tenant.db() as c:
        c.execute("UPDATE chain_inbox SET status='done', attempts=attempts+1, error=NULL WHERE id=?", (inbox_id,))
    return {"lead_id": lead["id"], "tier": lead["tier"]}


def _contact(fields: dict) -> tuple[str | None, str]:
    """(recipient, channel) from the lead form: their Instagram handle by DM, else their email."""
    ig = (fields.get("instagram") or "").strip()
    if ig:
        return ("@" + ig.lstrip("@")), "instagram_dm"
    email = (fields.get("email") or "").strip()
    if email:
        return email, "email"
    return None, "instagram_dm"


def _qualify(tenant, body: dict) -> dict:
    from .. import delivery

    leadgen_id = str(body["leadgen_id"])
    if tenant.get("live_meta_enabled", False):
        fetched = meta.fetch_lead(tenant, leadgen_id)  # real fetch: tenant token + appsecret_proof
        fields = fetched["fields"]
        source = "meta_live"
    else:
        fields = body.get("practice_fields") or {}
        source = "meta_practice"
    answers = {
        "monthly_revenue": fields.get("monthly_revenue", "<10k"), "can_invest": fields.get("can_invest", "maybe"),
        "timeline": fields.get("timeline", "later"), "decision_maker": str(fields.get("decision_maker", "true")).lower() == "true",
        "goal": fields.get("goal", "more booked calls"), "business": fields.get("business") or fields.get("company_name") or "",
    }
    sc = sales.score(answers, "meta_lead_form", sales.weights(tenant))
    hits = retrieval.search(tenant, "qualify before you send the link goal timeline invest", k=2)
    cites = [retrieval.cite({**h, "id": f"S{i + 1}"}) for i, h in enumerate(hits)]
    with tenant.db() as c:
        cur = c.execute("INSERT OR IGNORE INTO chain_leads(leadgen_id,data,tier,score,citations,stage,created) VALUES(?,?,?,?,?,?,?)",
                        (leadgen_id, db.jdump({"fields": fields, "answers": answers, "source": source}), sc["tier"], sc["score"],
                         db.jdump(cites), "qualified", db.now()))
        lead_row = c.execute("SELECT * FROM chain_leads WHERE leadgen_id=?", (leadgen_id,)).fetchone()
    lead = {"id": lead_row["id"], "tier": lead_row["tier"], "fields": fields, "answers": answers}
    if cur.rowcount == 0:
        return lead  # already processed (idempotent)
    _event(tenant, lead["id"], "qualified", {"tier": sc["tier"], "score": sc["score"], "citations": len(cites),
                                             "kb_scope": tenant.kb_scope})
    name = fields.get("full_name") or "Lead"
    rcpt, channel = _contact(fields)
    meta_q = {"prospect_facing": True, "lead_tier": sc["tier"], "prior_contact": True, "chain_lead_id": lead["id"]}
    first = sales.reply_line({"answers": answers}, sc["tier"]) + " Would a quick call this week be useful?"
    q1 = queue.add(tenant, "followup", first, channel=channel, recipient_ref=rcpt, title=f"Lead follow-up: {name}",
                   meta={**meta_q, "citations": cites, "step": 1}, actor="chain")
    # the value follow-ups wait: they start only once this first message is confirmed sent
    cid = delivery.register_cadence(tenant, ("chain_lead", lead["id"]), q1["id"], channel, rcpt, template="lead",
                                    lead_tier=sc["tier"], context={"followups": sales.followups({"answers": answers})})
    _event(tenant, lead["id"], "followups_queued", {"queue_ids": [q1["id"]], "cadence_id": cid})
    return lead


def _lead(tenant, lead_id: int) -> dict:
    with tenant.db() as c:
        lead = db.row(c.execute("SELECT * FROM chain_leads WHERE id=?", (lead_id,)).fetchone())
    if lead is None:
        raise KeyError(lead_id)
    lead["fields"] = (db.jload(lead["data"], {}) or {}).get("fields", {}) or {}
    return lead


def record_reply(tenant, lead_id: int, text: str, classification: str | None = None) -> dict:
    """A recorded reply from the lead: follow-ups stop; a positive reply gets a booking DM with fresh free times.
    The calendar invite waits until they pick a time (pick_slot)."""
    from .. import delivery

    lead = _lead(tenant, lead_id)
    if classification is not None and classification not in sales.CLASSES:
        raise sales.BookingError(422, f"Unknown reply type. Use one of: {', '.join(sales.CLASSES)}")

    def fn(run: runtime.Run):
        cls = classification or run.step("classify", {"text": text[:200]}, lambda: sales.classify_reply(text),
                                         "Classify the reply (rules, AI off)")
        _event(tenant, lead_id, "reply", {"text": text[:300], "classification": cls})
        rcpt, channel = _contact(lead["fields"])
        with tenant.db() as c:
            c.execute("INSERT INTO channel_messages(channel,direction,ext_id,body,meta,created) VALUES(?,?,?,?,?,?)",
                      (channel, "in", rcpt, (text or "")[:4000], db.jdump({"chain_lead_id": lead_id}), db.now()))
            c.execute("UPDATE chain_leads SET stage='replied' WHERE id=? AND stage='qualified'", (lead_id,))
        events.emit(tenant, "reply_received", {"party_type": "chain_lead", "party_id": lead_id, "owner_user_id": None,
                                               "classification": cls})
        out = {"lead_id": lead_id, "classification": cls, "label": "Classified by rules (AI off)", "booking_dm": None,
               "slots": [], "invite": None}
        if cls == "stop":
            for a in (lead["fields"].get("email"), lead["fields"].get("instagram")):
                if a:
                    delivery.suppress(tenant, a, None, "asked to stop", ("chain_lead", lead_id))
            with tenant.db() as c:
                c.execute("UPDATE chain_leads SET stage='lost' WHERE id=?", (lead_id,))
            events.emit(tenant, "dnc_set", {"party_type": "chain_lead", "party_id": lead_id})
            out["next"] = "They asked not to be contacted. Nothing more will be sent."
        elif cls == "positive":
            try:
                out.update(run.step("offer", {}, lambda: _offer(tenant, lead, run.id), "Offer fresh times"))
            except sales.NoSlots as e:
                out.update(no_slots=True, message=str(e))
        return out

    res = runtime.execute(tenant, "chain", f"Reply from chain lead {lead_id}",
                          ["Record the reply", "Classify (rules)", "Stop follow-ups", "Offer fresh times"], fn, "chain")
    return {**res["result"], "run_id": res["run_id"]}


def _offer(tenant, lead: dict, run_id: int | None) -> dict:
    lead_id = lead["id"]
    slots = sales._slots(tenant)
    if not slots:
        _event(tenant, lead_id, "no_slots", {})
        raise sales.NoSlots()
    rcpt, channel = _contact(lead["fields"])
    meta_q = {"prospect_facing": True, "lead_tier": lead["tier"], "prior_contact": True, "chain_lead_id": lead_id,
              "slots": slots, "slots_valid_until": sales._valid_until(slots)}
    b = queue.add(tenant, "booking_dm", sales._booking_body(slots), channel=channel, recipient_ref=rcpt,
                  title=f"Booking times: {lead['fields'].get('full_name', 'lead')}", meta=meta_q, run_id=run_id, actor="chain")
    _event(tenant, lead_id, "times_offered", {"slots": len(slots), "booking_dm": b["id"],
                                              "calendar": "google" if tenant.get("live_calendar_enabled") else "local"})
    return {"booking_dm": b["id"], "slots": slots, "slots_valid_until": meta_q["slots_valid_until"]}


def offer_times(tenant, lead_id: int) -> dict:
    """Booking DM with fresh free times (Google freeBusy when the live calendar is on). NoSlots when full."""
    lead = _lead(tenant, lead_id)
    res = runtime.execute(tenant, "chain", f"Offer times: chain lead {lead_id}", ["Find free times", "Draft the booking message"],
                          lambda run: _offer(tenant, lead, run.id), "chain")
    return {**res["result"], "run_id": res["run_id"]}


def pick_slot(tenant, lead_id: int, index: int | None = 0, start: float | None = None) -> dict:
    """They picked a time from the booking DM: queue the owner-only calendar invite for it."""
    lead = _lead(tenant, lead_id)
    s0 = sales.choose_slot(tenant, sales.latest_booking_dm(tenant, "chain_lead_id", lead_id), index, start)
    fields = lead["fields"]
    email = fields.get("email")

    def fn(run: runtime.Run):
        inv = queue.add(tenant, "calendar_invite", f"Strategy call with {fields.get('full_name', 'the lead')} at {s0['label']}.",
                        channel="calendar", recipient_ref=email, title=f"Invite: {fields.get('full_name', 'lead')}",
                        meta={"chain_lead_id": lead_id, "slot": s0, "attendees": [email] if email else [], "live": True,
                              "owner_only": True}, run_id=run.id, actor="chain")
        with tenant.db() as c:
            c.execute("UPDATE chain_leads SET stage='booking' WHERE id=?", (lead_id,))
            c.execute("INSERT INTO chain_bookings(lead_id,slot_start,slot_end,status,queue_id,created) VALUES(?,?,?,?,?,?)",
                      (lead_id, s0["start"], s0["end"], "proposed", inv["id"], db.now()))
        _event(tenant, lead_id, "booking_proposed", {"invite_queue_id": inv["id"],
                                                     "calendar": "google" if tenant.get("live_calendar_enabled") else "local"})
        run.note("invite", {"slot": s0["label"]}, {"queue_id": inv["id"]}, label="Queue the invite (owner approves)")
        return {"invite": inv["id"], "slot": s0, "status": inv["status"]}

    res = runtime.execute(tenant, "chain", f"Time picked: chain lead {lead_id}",
                          ["Check the time is still free", "Queue the invite (owner approves)"], fn, "chain")
    return {**res["result"], "run_id": res["run_id"]}


def deliver_calendar_invite(tenant, item: dict) -> str:
    """Called by the queue for an APPROVED invite when the live calendar switch is on. Writes one local
    mirror of the Google event; the approval hook (agents/sales.py) adds the show-up ladder."""
    lead_id = item["meta"].get("chain_lead_id") or item["meta"].get("lead_id")
    slot = item["meta"]["slot"]
    eid = gcal.event_id(tenant.id, lead_id, int(slot["start"]))
    ev = gcal.insert_event(tenant, eid, item["title"] or "Call", slot["start"], slot["end"], item["meta"].get("attendees", []))
    gid = ev.get("id", eid)
    with tenant.db() as c:
        c.execute("UPDATE chain_bookings SET event_id=?, status='booked' WHERE queue_id=?", (gid, item["id"]))
        if item["meta"].get("chain_lead_id"):
            c.execute("UPDATE chain_leads SET stage='booked' WHERE id=?", (lead_id,))
        have = c.execute("SELECT 1 FROM calendar_events WHERE ext_id=?", (gid,)).fetchone()
    if not have:
        calendar.add_event(tenant, item["title"] or "Call", slot["start"], slot["end"], item["meta"].get("attendees", []),
                           ext_id=gid)
    if item["meta"].get("chain_lead_id"):
        _event(tenant, lead_id, "event_created", {"event_id": gid, "existing": bool(ev.get("existing"))})
    return f"live:google_calendar:{gid}"


def record_outcome(tenant, lead_id: int, outcome: str) -> dict:
    if outcome not in ("showed", "no_show", "closed", "lost"):
        raise ValueError(outcome)
    with tenant.db() as c:
        n = c.execute("UPDATE chain_leads SET stage=? WHERE id=?", (outcome, lead_id)).rowcount
    if not n:
        raise KeyError(lead_id)
    _event(tenant, lead_id, "outcome", {"outcome": outcome})
    return {"ok": True}


def report(tenant) -> dict:
    with tenant.db() as c:
        stages = {r["stage"]: r["n"] for r in c.execute("SELECT stage, COUNT(*) n FROM chain_leads GROUP BY stage")}
        tiers = {r["tier"]: r["n"] for r in c.execute("SELECT tier, COUNT(*) n FROM chain_leads GROUP BY tier")}
        inbox = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) n FROM chain_inbox GROUP BY status")}
        events = db.rows(c.execute("SELECT * FROM chain_events ORDER BY id DESC LIMIT 50"))
    for e in events:
        e["detail"] = db.jload(e["detail"], {})
    return {"tenant": tenant.id, "kb_scope": tenant.kb_scope, "leads_by_stage": stages, "leads_by_tier": tiers,
            "inbox": inbox, "recent_events": events, "generated": time.time()}
