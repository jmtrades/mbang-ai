"""The live lead chain, end to end (built and tested against fakes; switched off by default):

signed Meta lead webhook -> tenant inbox (UNIQUE dedupe key) -> lead fetch with the tenant's
token + appsecret_proof (only with the live switch on; practice leads use the webhook body)
-> qualification with citations from the tenant's own KB scope -> follow-up DMs (queue) ->
recorded reply -> booking DM with real free slots -> calendar invite proposal (owner
approval) -> Google event with a deterministic id -> show-up ladder -> outcome -> report.

The inbox gives up after 8 attempts and marks the item dead.
"""
from __future__ import annotations

import json
import time

from .. import audit, calendar, db, jobs, queue, retrieval
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


def _qualify(tenant, body: dict) -> dict:
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
    handle = fields.get("instagram") or fields.get("email") or f"lead-{lead['id']}"
    meta_q = {"prospect_facing": True, "lead_tier": sc["tier"], "prior_contact": True, "chain_lead_id": lead["id"]}
    first = sales.reply_line({"answers": answers}, sc["tier"]) + " Would a quick call this week be useful?"
    q1 = queue.add(tenant, "followup", first, channel="instagram_dm", recipient_ref=handle, title=f"Lead follow-up: {name}",
                   meta={**meta_q, "citations": cites}, actor="chain")
    q2 = queue.add(tenant, "followup", sales.followups({"answers": answers})[0], channel="instagram_dm", recipient_ref=handle,
                   title=f"Value follow-up: {name}", meta={**meta_q, "step": 2}, actor="chain")
    _event(tenant, lead["id"], "followups_queued", {"queue_ids": [q1["id"], q2["id"]]})
    return lead


def record_reply(tenant, lead_id: int, text: str) -> dict:
    """A recorded reply from the lead -> booking DM with real free slots + invite proposal."""
    with tenant.db() as c:
        lead = db.row(c.execute("SELECT * FROM chain_leads WHERE id=?", (lead_id,)).fetchone())
    if lead is None:
        raise KeyError(lead_id)
    data = db.jload(lead["data"], {})
    fields = data.get("fields", {})
    _event(tenant, lead_id, "reply", {"text": text[:300]})
    slots = gcal.free_slots(tenant, 3) if tenant.get("live_calendar_enabled", False) else calendar.free_slots(tenant, 3)
    handle = fields.get("instagram") or fields.get("email") or f"lead-{lead_id}"
    meta_q = {"prospect_facing": True, "lead_tier": lead["tier"], "prior_contact": True, "chain_lead_id": lead_id}
    b = queue.add(tenant, "booking_dm", "Great to hear back. Here are three times that work: "
                  + ", ".join(s["label"] for s in slots) + ". Which suits you best?", channel="instagram_dm",
                  recipient_ref=handle, title=f"Booking times: {fields.get('full_name', 'lead')}", meta={**meta_q, "slots": slots},
                  actor="chain")
    s0 = slots[0]
    email = fields.get("email")
    inv = queue.add(tenant, "calendar_invite", f"Strategy call with {fields.get('full_name', 'the lead')} at {s0['label']}.",
                    channel="calendar", recipient_ref=email, title=f"Invite: {fields.get('full_name', 'lead')}",
                    meta={"chain_lead_id": lead_id, "slot": s0, "attendees": [email] if email else [], "live": True,
                          "owner_only": True}, actor="chain")
    with tenant.db() as c:
        c.execute("UPDATE chain_leads SET stage='booking' WHERE id=?", (lead_id,))
        c.execute("INSERT INTO chain_bookings(lead_id,slot_start,slot_end,status,queue_id,created) VALUES(?,?,?,?,?,?)",
                  (lead_id, s0["start"], s0["end"], "proposed", inv["id"], db.now()))
    _event(tenant, lead_id, "booking_proposed", {"slots": len(slots), "invite_queue_id": inv["id"],
                                                 "calendar": "google" if tenant.get("live_calendar_enabled") else "local"})
    return {"booking_dm": b["id"], "invite": inv["id"], "slots": slots}


def deliver_calendar_invite(tenant, item: dict) -> str:
    """Called by queue.deliver for an APPROVED invite when the live calendar switch is on."""
    lead_id = item["meta"].get("chain_lead_id") or item["meta"].get("lead_id")
    slot = item["meta"]["slot"]
    eid = gcal.event_id(tenant.id, lead_id, int(slot["start"]))
    ev = gcal.insert_event(tenant, eid, item["title"] or "Call", slot["start"], slot["end"], item["meta"].get("attendees", []))
    with tenant.db() as c:
        c.execute("UPDATE chain_bookings SET event_id=?, status='booked' WHERE queue_id=?", (ev.get("id", eid), item["id"]))
        if lead_id:
            c.execute("UPDATE chain_leads SET stage='booked' WHERE id=?", (lead_id,))
    calendar.add_event(tenant, item["title"] or "Call", slot["start"], slot["end"], item["meta"].get("attendees", []),
                       ext_id=ev.get("id", eid))
    if lead_id:
        _event(tenant, lead_id, "event_created", {"event_id": ev.get("id", eid), "existing": bool(ev.get("existing"))})
        _ladder(tenant, lead_id, slot)
    return f"live:google_calendar:{ev.get('id', eid)}"


def _ladder(tenant, lead_id: int, slot: dict) -> list[int]:
    ids = []
    for label, before in sales.LADDER:
        body = {"24h": "Looking forward to tomorrow. One question to think about: what would ten more bookings a month change?",
                "2h": "See you in two hours. Here's a short video worth a look before we talk.",
                "10min": "Starting in ten minutes. Have your current numbers handy."}[label]
        ids.append(queue.add(tenant, "reminder", body, channel="instagram_dm", recipient_ref=f"lead-{lead_id}",
                             title=f"{label} reminder (lead {lead_id})", meta={"chain_lead_id": lead_id, "ladder": label,
                                                                                  "send_at": slot["start"] - before},
                             actor="chain")["id"])
    _event(tenant, lead_id, "ladder_queued", {"queue_ids": ids})
    return ids


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
