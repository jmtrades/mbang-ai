"""Delivery API: confirm a manual send, cancel / send now, the sales booking steps (reply -> times -> pick),
sending settings, follow-up sequences, contact points, and the public one-click unsubscribe page."""
from __future__ import annotations

import html

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .. import auth, db, delivery, queue
from ..agents import sales
from ..deps import Ctx, require, require_owner
from ..live import chain

router = APIRouter()
public_router = APIRouter()


def _qerr(e: queue.QueueError):
    raise HTTPException(e.status, str(e))


def _visible(ctx: Ctx, qid: int) -> dict:
    """The item if this user may see it (same rule as GET /queue/{id}); otherwise 404."""
    item = queue.get(ctx.tenant, qid, scrub=False)
    kinds = queue.decidable_kinds(ctx.role)
    own = item is not None and item["meta"].get("owner_user_id") == ctx.user["id"] and item["kind"] in auth.OWN_DECIDE
    if item is None or (kinds is not None and item["kind"] not in kinds and not own):
        raise HTTPException(404, "Not found")
    return item


# --------------------------------------------------------------------------- queue item delivery actions
class SentBody(BaseModel):
    permalink: str | None = Field(None, max_length=500, pattern=r"^https?://\S+$")


@router.post("/queue/{qid}/sent")
def mark_sent(qid: int, b: SentBody | None = None, ctx: Ctx = Depends(require("queue"))):
    _visible(ctx, qid)
    try:
        return queue.mark_sent(ctx.tenant, qid, ctx.actor, ctx.role, ctx.user["id"], permalink=b.permalink if b else None)
    except queue.QueueError as e:
        _qerr(e)


class CancelBody(BaseModel):
    reason: str = Field("", max_length=300)


@router.post("/queue/{qid}/cancel")
def cancel(qid: int, b: CancelBody | None = None, ctx: Ctx = Depends(require("queue"))):
    _visible(ctx, qid)
    try:
        return queue.cancel(ctx.tenant, qid, ctx.actor, ctx.role, ctx.user["id"], reason=b.reason if b else "")
    except queue.QueueError as e:
        _qerr(e)


@router.post("/queue/{qid}/send-now")
def send_now(qid: int, ctx: Ctx = Depends(require("queue"))):
    _visible(ctx, qid)
    try:
        return queue.send_now(ctx.tenant, qid, ctx.actor, ctx.role, ctx.user["id"])
    except queue.QueueError as e:
        _qerr(e)


# --------------------------------------------------------------------------- overview + settings
@router.get("/delivery/overview")
def overview(ctx: Ctx = Depends(require("queue"))):
    t = ctx.tenant
    out = {"reject_codes": list(queue.REJECT_CODES), "channels": delivery.CHANNEL_LABEL,
           "manual_channels": sorted(queue.MANUAL_CHANNELS)}
    if ctx.role == "owner":
        from ..live import senders

        out.update({
            "settings": delivery.get_settings(t), "postal_address": delivery.postal_address(t),
            "public_url_set": not delivery._is_local(delivery.public_base()),
            "email_cap_today": delivery.email_cap_today(t), "emails_sent_today": delivery.live_emails_today(t),
            "live_channels": {ch: senders.ready(t, ch) for ch in senders.NEEDS},
            "suppressed": len(delivery.suppression_list(t, 1000)),
        })
    return out


class SettingsBody(BaseModel):
    email_daily_cap: int | None = Field(None, ge=0, le=2000)
    warmup_enabled: bool | None = None
    warmup_first_day: int | None = Field(None, ge=1, le=500)
    warmup_step: int | None = Field(None, ge=0, le=200)
    daily_touch_cap: int | None = Field(None, ge=1, le=1000)
    cadence_days: list[int] | None = Field(None, min_length=1, max_length=8)


@router.get("/delivery/settings")
def get_settings(ctx: Ctx = Depends(require_owner)):
    return {"settings": delivery.get_settings(ctx.tenant), "postal_address": delivery.postal_address(ctx.tenant)}


@router.put("/delivery/settings")
def put_settings(b: SettingsBody, ctx: Ctx = Depends(require_owner)):
    data = b.model_dump(exclude_none=True)
    if "cadence_days" in data and any(not 1 <= d <= 365 for d in data["cadence_days"]):
        raise HTTPException(422, "Follow-up days must be between 1 and 365")
    return {"settings": delivery.put_settings(ctx.tenant, data, ctx.actor)}


@router.get("/delivery/suppressions")
def suppressions(ctx: Ctx = Depends(require_owner)):
    return {"items": delivery.suppression_list(ctx.tenant)}


@router.get("/delivery/feedback")
def feedback(ctx: Ctx = Depends(require_owner)):
    return {"items": queue.feedback(ctx.tenant)}


@router.post("/delivery/run-now")
def run_now(ctx: Ctx = Depends(require_owner)):
    """Run the scheduled-send and follow-up ticks now (they also run on the server's schedule)."""
    return {"dispatch": delivery.dispatch_due(ctx.tenant), "cadences": delivery.run_cadences(ctx.tenant)}


# --------------------------------------------------------------------------- follow-up sequences
def _cadence_scope(ctx: Ctx) -> dict:
    if ctx.role == "owner":
        return {}
    ids = [ctx.user["id"]]
    if ctx.role == "coach":
        with ctx.tenant.db() as c:
            ids += [r["id"] for r in c.execute("SELECT id FROM users WHERE coach_id=?", (ctx.user["id"],))]
    types = ["lead", "chain_lead"] if auth.has_tab(ctx.role, "sales") else []
    return {"owner_user_ids": ids, "party_types": types}


@router.get("/delivery/cadences")
def cadences(ctx: Ctx = Depends(require("queue"))):
    return {"items": delivery.list_cadences(ctx.tenant, **_cadence_scope(ctx))}


@router.post("/delivery/cadences/{cid}/stop")
def stop_cadence(cid: int, ctx: Ctx = Depends(require("queue"))):
    if cid not in {r["id"] for r in delivery.list_cadences(ctx.tenant, limit=5000, **_cadence_scope(ctx))}:
        raise HTTPException(404, "Not found")
    if not delivery.stop_cadence(ctx.tenant, cid, ctx.actor):
        raise HTTPException(409, "This sequence has already stopped")
    return {"ok": True}


# --------------------------------------------------------------------------- contact points
def _may_party(ctx: Ctx, party_type: str, party_id: int) -> bool:
    if ctx.role == "owner":
        return True
    with ctx.tenant.db() as c:
        if party_type == "prospect":
            r = c.execute("SELECT owner_user_id FROM prospects WHERE id=?", (party_id,)).fetchone()
            return bool(r and r["owner_user_id"] == ctx.user["id"])
        if party_type in ("lead", "chain_lead"):
            return auth.has_tab(ctx.role, "sales")
        if party_type == "student":
            if ctx.role == "coach":
                r = c.execute("SELECT coach_id FROM students WHERE id=?", (party_id,)).fetchone()
                return bool(r and r["coach_id"] == ctx.user["id"])
            return ctx.user.get("student_id") == party_id
        if party_type == "client":
            r = c.execute("SELECT student_id FROM clients WHERE id=?", (party_id,)).fetchone()
            return bool(r and r["student_id"] is not None and r["student_id"] == ctx.user.get("student_id"))
    return False


PARTY_PATTERN = "^(prospect|lead|chain_lead|student|client)$"


@router.get("/delivery/contacts")
def list_contacts(party_type: str = Query(..., pattern=PARTY_PATTERN), party_id: int = Query(..., ge=1),
                  ctx: Ctx = Depends(require("queue"))):
    if not _may_party(ctx, party_type, party_id):
        raise HTTPException(404, "Not found")
    return {"items": delivery.contact_points(ctx.tenant, party_type, party_id)}


class ContactBody(BaseModel):
    party_type: str = Field(pattern=PARTY_PATTERN)
    party_id: int = Field(ge=1)
    channel: str = Field(pattern="^(email|sms|phone|whatsapp|telegram|slack|instagram_dm|linkedin)$")
    address: str = Field(min_length=2, max_length=300)
    verified: bool = False
    opted_in: bool = False


@router.post("/delivery/contacts")
def add_contact(b: ContactBody, ctx: Ctx = Depends(require("queue"))):
    if not _may_party(ctx, b.party_type, b.party_id):
        raise HTTPException(404, "Not found")
    try:
        return delivery.add_contact_point(ctx.tenant, b.party_type, b.party_id, b.channel, b.address, verified=b.verified,
                                          opted_in_=b.opted_in, source="manual", actor=ctx.actor)
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.delete("/delivery/contacts/{cp_id}")
def delete_contact(cp_id: int, ctx: Ctx = Depends(require("queue"))):
    with ctx.tenant.db() as c:
        r = db.row(c.execute("SELECT party_type, party_id FROM contact_points WHERE id=?", (cp_id,)).fetchone())
    if r is None or not _may_party(ctx, r["party_type"], r["party_id"]):
        raise HTTPException(404, "Not found")
    delivery.delete_contact_point(ctx.tenant, cp_id, ctx.actor)
    return {"ok": True}


# --------------------------------------------------------------------------- sales booking steps
def _booking_call(fn):
    try:
        return fn()
    except KeyError:
        raise HTTPException(404, "Lead not found")
    except sales.NoSlots as e:
        raise HTTPException(409, str(e))
    except sales.BookingError as e:
        raise HTTPException(e.status, str(e))


class LeadReplyBody(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    classification: str | None = Field(None, pattern="^(positive|negative|question|neutral|stop)$")


class PickBody(BaseModel):
    index: int | None = Field(None, ge=0, le=9)
    start: float | None = Field(None, gt=0)


@router.post("/leads/{lead_id}/reply")
def lead_reply(lead_id: int, b: LeadReplyBody, ctx: Ctx = Depends(require("sales"))):
    return _booking_call(lambda: sales.record_reply(ctx.tenant, lead_id, b.text, b.classification, ctx.user))


@router.post("/leads/{lead_id}/offer-times")
def lead_offer(lead_id: int, ctx: Ctx = Depends(require("sales"))):
    return _booking_call(lambda: sales.offer_times(ctx.tenant, lead_id, ctx.user))


@router.post("/leads/{lead_id}/pick-slot")
def lead_pick(lead_id: int, b: PickBody, ctx: Ctx = Depends(require("sales"))):
    if b.index is None and b.start is None:
        raise HTTPException(422, "Say which time they picked")
    return _booking_call(lambda: sales.pick_slot(ctx.tenant, lead_id, b.index, b.start, ctx.user))


@router.get("/leads/{lead_id}/booking")
def lead_booking(lead_id: int, ctx: Ctx = Depends(require("sales"))):
    """The latest booking message for a lead (its times and whether they are still valid)."""
    with ctx.tenant.db() as c:
        lead = c.execute("SELECT id, stage, do_not_contact FROM leads WHERE id=?", (lead_id,)).fetchone()
    if lead is None:
        raise HTTPException(404, "Lead not found")
    bdm = sales.latest_booking_dm(ctx.tenant, "lead_id", lead_id)
    import time as _t

    return {"lead_id": lead_id, "stage": lead["stage"], "do_not_contact": bool(lead["do_not_contact"]),
            "booking_dm": None if bdm is None else {"id": bdm["id"], "status": bdm["status"],
                                                     "slots": bdm["meta"].get("slots") or [],
                                                     "valid": (queue._num(bdm["meta"].get("slots_valid_until")) or 0) > _t.time()}}


@router.post("/chain/leads/{lead_id}/offer-times")
def chain_offer(lead_id: int, ctx: Ctx = Depends(require("sales"))):
    return _booking_call(lambda: chain.offer_times(ctx.tenant, lead_id))


@router.post("/chain/leads/{lead_id}/pick-slot")
def chain_pick(lead_id: int, b: PickBody, ctx: Ctx = Depends(require("sales"))):
    if b.index is None and b.start is None:
        raise HTTPException(422, "Say which time they picked")
    return _booking_call(lambda: chain.pick_slot(ctx.tenant, lead_id, b.index, b.start))


@router.post("/bookings/{event_id}/cancel")
def cancel_booking(event_id: int, ctx: Ctx = Depends(require("sales"))):
    try:
        return delivery.cancel_booking(ctx.tenant, event_id, ctx.actor)
    except KeyError:
        raise HTTPException(404, "Not found")


# --------------------------------------------------------------------------- public: one-click unsubscribe
_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,
initial-scale=1"><meta name="robots" content="noindex"><title>{title}</title><style>body{{font:16px/1.5 system-ui,
sans-serif;background:#f6f7f9;color:#14161a;margin:0;display:flex;min-height:100vh;align-items:center;justify-content:center}}
main{{background:#fff;border:1px solid #e3e6ea;border-radius:12px;padding:28px;max-width:420px;margin:16px}}h1{{font-size:20px;
margin:0 0 8px}}button{{font:inherit;background:#14161a;color:#fff;border:0;border-radius:8px;padding:10px 16px;cursor:pointer}}
p{{margin:0 0 16px;color:#4a4f57}}</style></head><body><main><h1>{title}</h1><p>{text}</p>{form}</main></body></html>"""


def _page(title: str, text: str, form: str = "", status: int = 200) -> HTMLResponse:
    return HTMLResponse(_PAGE.format(title=html.escape(title), text=html.escape(text), form=form), status_code=status,
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


_INVALID = ("Link not valid", "This unsubscribe link isn't valid. If you keep getting emails you don't want, reply to "
                              "one of them and ask to be removed.")


@public_router.get("/u/{token}", response_class=HTMLResponse)
def unsubscribe_page(token: str):
    found = delivery.lookup_token(token)
    if not found:
        return _page(*_INVALID, status=404)
    t, row = found
    if row["used"]:
        return _page("You're unsubscribed", f"You won't get more emails like this from {delivery.brand_name(t)}.")
    form = f'<form method="post" action="/u/{html.escape(token, quote=True)}"><button type="submit">Unsubscribe</button></form>'
    return _page("Unsubscribe", f"Stop emails like this from {delivery.brand_name(t)}? One click and you're off the list.", form)


@public_router.post("/u/{token}", response_class=HTMLResponse)
async def unsubscribe_post(token: str, request: Request):
    await request.body()  # one-click POST (RFC 8058) or the form button; the body itself is not needed
    res = delivery.unsubscribe(token)
    if res is None:
        return _page(*_INVALID, status=404)
    return _page("You're unsubscribed", f"You won't get more emails like this from {res['brand']}.")
