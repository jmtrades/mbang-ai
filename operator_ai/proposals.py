"""Proposals: from a discovery call to a written proposal the prospect can accept.

build(prospect_id, call_notes) writes the sections from the prospect's own words (call notes and replies), the
bottleneck and its cost (diagnostic maths on their own numbers, when they shared any), a 30/60/90 plan,
deliverables, two or three pricing options (retainer / retainer + performance share / paid pilot, the structures
in the growth-operator playbook), terms and an explicit no-guarantee line. Every projection carries labelled
assumptions. The whole text is checked by the guardrails in client-asset mode.

Sending is a queue item of kind "proposal" (the author approves their own). Only once it is approved does the
tokenised public page /t/<tenant>/p/<token> show it; the page counts views (not the team's own) and offers
accept: typed name + checkbox + timestamp + hashed IP. Acceptance marks the prospect won, which creates the
client row and emits client_won. Rules only; AI is not used here.
"""
from __future__ import annotations

import hashlib
import html
import math
import os
import re
import secrets
import time

from . import audit, db, diagnostic, guardrails, queue, runtime
from . import prospects as P

CURRENCIES = {"USD": "$", "GBP": "£", "EUR": "€", "CAD": "CA$", "AUD": "A$"}
STATUS_LABELS = {"draft": "Draft", "queued": "Waiting for approval", "published": "Sent: link is live",
                 "accepted": "Accepted", "withdrawn": "Withdrawn"}
OPTION_KEYS = ["retainer", "retainer_performance", "pilot"]
NUMBER_KEYS = {"leads": "Enquiries a month", "booked": "Calls booked a month", "showed": "Calls that showed",
               "closed": "Sales a month", "offer_price": "Their offer price", "ad_spend": "Ad spend a month",
               "dms_sent": "DMs sent a month", "replies": "Replies a month"}
NO_GUARANTEE = ("We don't guarantee results, revenue or income. Any projection in this proposal is a model built on the "
                "assumptions listed next to it, not a promise. Results depend on your offer, your market and the work on "
                "both sides.")
FOCUS = {
    "reply": ("first contact", ["Rewrite and test first messages, one change at a time",
                                "A reply-to-call script your team can follow"]),
    "conversation": ("conversations", ["A short qualifying script: goal and current numbers first",
                                       "Response-time standard for every new reply"]),
    "link": ("moving conversations to a call", ["Offer the call once the goal is clear", "Two specific times instead of a link"]),
    "booked": ("enquiry to booked call", ["Answer every enquiry within five minutes", "Offer two specific times, not a link",
                                          "A simple tracker from enquiry to booked call"]),
    "show": ("show rate", ["Confirmation within five minutes of booking", "A reminder sequence that adds value before the call"]),
    "close": ("close rate", ["A pre-call brief for every call", "A clear call structure and objection library"]),
    "wreg": ("webinar registrations", ["A sharper registration page around one outcome"]),
    "wshow": ("webinar show-up", ["Reminders a day, an hour and at go-live", "Ask registrants to reply to confirm"]),
    "wclose": ("webinar sales", ["Tighten the offer segment", "Ask permission before presenting the offer"]),
    "cpl": ("cost per lead", ["Structured creative tests with equal budgets and a kill rule"]),
}


class ProposalError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


def money(amount, currency: str = "USD") -> str:
    sym = CURRENCIES.get(currency, "")
    try:
        v = float(amount)
    except (TypeError, ValueError):
        return "-"
    s = f"{v:,.0f}" if v == int(v) else f"{v:,.2f}"
    return f"{sym}{s}" if sym else f"{s} {currency}"


def public_url(tenant, token: str) -> str:
    base = os.environ.get("OPERATOR_PUBLIC_URL", "").rstrip("/")
    return f"{base}/t/{tenant.id}/p/{token}"


# --------------------------------------------------------------------------- inputs
def clean_numbers(numbers: dict | None) -> dict:
    out = {}
    for k, v in (numbers or {}).items():
        if k not in NUMBER_KEYS or v in (None, ""):
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise ProposalError(422, f"{NUMBER_KEYS[k]} must be a number")
        if not math.isfinite(f) or f < 0 or f > 1e9:
            raise ProposalError(422, f"{NUMBER_KEYS[k]} must be between 0 and a billion")
        out[k] = f
    return out


def clean_pricing(pricing: dict | None) -> dict:
    p = dict(pricing or {})
    cur = str(p.get("currency") or "USD").upper()
    if cur not in CURRENCIES:
        raise ProposalError(422, "Choose a currency: " + ", ".join(CURRENCIES))
    try:
        retainer = float(p.get("retainer"))
    except (TypeError, ValueError):
        raise ProposalError(422, "Set your monthly fee")
    if not math.isfinite(retainer) or not (0 < retainer <= 1_000_000):
        raise ProposalError(422, "Set your monthly fee")

    def num(key, default, lo, hi):
        v = p.get(key)
        if v in (None, ""):
            return default
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise ProposalError(422, f"{key.replace('_', ' ')} must be a number")
        if not math.isfinite(f) or not (lo <= f <= hi):
            raise ProposalError(422, f"{key.replace('_', ' ')} must be between {lo:g} and {hi:g}")
        return f

    opts = p.get("options") or OPTION_KEYS
    if not isinstance(opts, list) or not opts or any(o not in OPTION_KEYS for o in opts):
        raise ProposalError(422, "Choose which pricing options to include")
    return {"currency": cur, "retainer": retainer, "performance_pct": num("performance_pct", 10, 0, 50),
            "pilot_fee": num("pilot_fee", round(retainer * 0.6, -1) or retainer, 1, 1_000_000),
            "pilot_days": int(num("pilot_days", 30, 7, 120)), "term_months": int(num("term_months", 3, 1, 24)),
            "options": [o for o in OPTION_KEYS if o in opts]}


def options_for(pr: dict) -> list[dict]:
    cur, ret = pr["currency"], pr["retainer"]
    low = max(1.0, round(ret * 0.6 / 50) * 50)
    out = []
    for key in pr["options"]:
        if key == "retainer":
            out.append({"key": key, "label": "Monthly retainer", "amount": ret, "per": "month", "currency": cur,
                        "summary": f"{money(ret, cur)} a month for {pr['term_months']} months, then month to month.",
                        "details": ["Everything in the deliverables", "Weekly numbers review"]})
        elif key == "retainer_performance":
            out.append({"key": key, "label": "Lower retainer + performance share", "amount": low, "per": "month",
                        "currency": cur, "summary": f"{money(low, cur)} a month plus {pr['performance_pct']:g}% of net cash "
                                                    "collected above the baseline we agree in week one.",
                        "details": ["Same deliverables", "Share paid monthly on money actually collected"]})
        elif key == "pilot":
            out.append({"key": key, "label": "Paid pilot", "amount": pr["pilot_fee"], "per": "one-off", "currency": cur,
                        "summary": f"A {pr['pilot_days']}-day pilot for {money(pr['pilot_fee'], cur)}, focused on one "
                                   "metric with a baseline agreed in week one.",
                        "details": ["At the end: move to a retainer, extend, or end cleanly"]})
    return out


def _sentences(text: str) -> list[str]:
    return [s.strip(" -•\t") for s in re.split(r"(?<=[.!?])\s+|\n+", text or "") if len(s.strip()) > 8]


def their_words(tenant, p: dict, call_notes: str) -> tuple[list[str], int]:
    """First-person lines from the call notes and their replies, word for word. A line the guardrails would block
    (for example an income figure) is left out and counted, never rewritten."""
    cands = []
    for s in _sentences(call_notes):
        if re.search(r"\b(i|we|our|my|us|me|i'm|we're)\b", s, re.I):
            cands.append(s)
    replies = [e["detail"].get("text") for e in P.timeline(tenant, p["id"]) if e["kind"] == "reply" and e["detail"].get("text")]
    for r in replies:
        cands += _sentences(r)[:2]
    keep, dropped = [], 0
    for s in cands:
        if len(keep) >= 5:
            break
        s = s[:300]
        if guardrails.blocked(guardrails.check_for(tenant, s, {"client_asset": True})):
            dropped += 1
            continue
        if s not in keep:
            keep.append(s)
    return keep, dropped


# --------------------------------------------------------------------------- sections
def build_sections(tenant, p: dict, call_notes: str, numbers: dict, pr: dict, include_projection: bool = True) -> tuple[list, list]:
    cur = pr["currency"]
    secs = []
    words, dropped = their_words(tenant, p, call_notes)
    if words:
        text = "\n".join(f"- \"{w}\"" for w in words)
    else:
        facts = [f.get("claim") for f in P._facts(p) if f.get("kind") != "field"][:3]
        text = ("From our conversation and research:\n" + "\n".join(f"- {f}" for f in facts)) if facts else \
            "Add your call notes so this section uses their own words."
    if dropped:
        text += f"\n(Left out {dropped} line{'s' if dropped > 1 else ''} that read like an income or results claim.)"
    secs.append({"key": "problem", "title": "Where you are now, in your words", "text": text})

    metrics = dict(numbers)
    assumptions = []
    if "leads" in metrics and "links_sent" not in metrics and "booked" in metrics:
        metrics["links_sent"] = metrics["leads"]
        assumptions.append("Each enquiry is treated as someone who was offered a call.")
    focus_key, bn = None, None
    if numbers:
        res = diagnostic.analyse(metrics)
        bn = res.get("bottleneck")
        step = FOCUS.get((bn or {}).get("key", ""), ("",))[0] or (bn or {}).get("label", "").lower()
        if bn and numbers.get("offer_price"):
            focus_key = bn["key"]
            val = f"{bn['value']:.0%}" if bn["kind"] == "rate" else money(bn["value"], cur)
            target = f"{bn['benchmark']:.0%}" if bn["kind"] == "rate" else money(bn["benchmark"], cur)
            text = (f"Your numbers point to {step}: {val} against a working benchmark of {target}. "
                    f"Closing that gap is estimated at about {money(bn['monthly_cost'], cur)} a month in extra sales at your "
                    "current price (an estimate, not a promise).")
            assumptions += ["Uses the monthly numbers you shared on the call.",
                            "Benchmarks are the programme's working targets, not guarantees.",
                            f"Your offer price is {money(numbers['offer_price'], cur)}."]
        elif bn:
            focus_key = bn["key"]
            text = f"Your numbers point to {step} as the weakest step. Share your offer price and we'll put a cost on it."
        else:
            text = "Every step with enough data is at or above the working benchmarks, so the plan focuses on volume."
    else:
        text = "No numbers were shared yet, so no cost is estimated. Week one starts by measuring your funnel."
    secs.append({"key": "bottleneck", "title": "The bottleneck and what it's costing", "text": text,
                 "assumptions": assumptions})

    focus, moves = FOCUS.get(focus_key or "", ("a steady flow of booked calls", ["A simple tracker from enquiry to booked call",
                                                                                 "A consistent outreach and follow-up rhythm"]))
    secs.append({"key": "plan", "title": "30/60/90 day plan", "text": "\n".join([
        f"- Days 1 to 30: measure the funnel, agree the baseline and fix {focus} first. {moves[0]}.",
        f"- Days 31 to 60: build on what moved. {moves[1] if len(moves) > 1 else 'Tighten the follow-up rhythm'}.",
        "- Days 61 to 90: keep what works, cut what doesn't, and agree the next quarter's targets together.",
    ])})
    deliverables = moves + ["A weekly 30-minute numbers review", "A written report every month",
                            "Every message, ad or page approved by you before it goes out"]
    secs.append({"key": "deliverables", "title": "What you get", "text": "\n".join(f"- {d}" for d in deliverables)})

    opts = options_for(pr)
    secs.append({"key": "options", "title": "Options", "text": "\n".join(f"- {o['label']}: {o['summary']}" for o in opts)})

    terms = [f"Term: {pr['term_months']} month{'s' if pr['term_months'] != 1 else ''}, then month to month. Either side can end "
             "it with 30 days' written notice.",
             "Fees are paid monthly in advance. Prices are shown in full; there are no other fees."]
    if "retainer_performance" in pr["options"]:
        terms.append("Performance share is worked out on net cash collected: money actually received, less refunds, "
                     "chargebacks, payment-processing fees and sales tax. It is paid monthly on instalments actually "
                     "collected, and refunds after a payout are netted against the next statement.")
    terms += ["Ad spend is paid by you directly to the ad platform and is not part of these fees.",
              "You keep ownership of your accounts, content and customer data. We get read-only access to the numbers we "
              "report on.",
              "Case studies only with your written permission."]
    secs.append({"key": "terms", "title": "Terms", "text": "\n".join(f"- {t}" for t in terms)})

    proj = projection(numbers, bn, cur) if include_projection else None
    if proj:
        secs.append({"key": "projection", "title": "What the numbers could look like (a model, not a promise)", **proj})
    secs.append({"key": "no_guarantee", "title": "What we don't promise", "text": NO_GUARANTEE})
    return secs, opts


def projection(numbers: dict, bn: dict | None, cur: str) -> dict | None:
    """Their own funnel with only the bottleneck step changed: as now, halfway to the working benchmark, and at it.
    Needs their sales a month, their price and a rate-type bottleneck; otherwise there is no projection."""
    sales, price = numbers.get("closed"), numbers.get("offer_price")
    if not (bn and bn.get("kind") == "rate" and sales and price and bn["value"] > 0):
        return None
    r, b = bn["value"], bn["benchmark"]
    rows = []
    for name, rate in (("As now", r), ("Halfway to the benchmark", r + (b - r) / 2), ("At the benchmark", b)):
        s = sales * rate / r
        rows.append(f"- {name}: about {s:.1f} sales a month, about {money(round(s * price), cur)} in revenue (model)")
    step = FOCUS.get(bn["key"], (bn["label"].lower(),))[0]
    return {"text": "\n".join(rows), "assumptions": [
        f"Only {step} changes; every other step stays at your current rate.",
        f"Uses your {sales:g} sales a month and your price of {money(price, cur)}.",
        "Benchmarks are the programme's working targets, not guarantees."]}


def compile_text(title: str, sections: list[dict]) -> str:
    out = [title]
    for s in sections:
        out.append(f"\n{s['title']}\n{s['text']}")
        if s.get("assumptions"):
            out.append("Assumptions:\n" + "\n".join(f"- {a}" for a in s["assumptions"]))
    return "\n".join(out)


def _check(tenant, title: str, sections: list[dict], pid: int, prop_id: int | None) -> list[dict]:
    meta = {"client_asset": True, "feature": P.FEATURE, "prospect_id": pid, "proposal_id": prop_id or -1}
    return guardrails.check_for(tenant, compile_text(title, sections), meta)


# --------------------------------------------------------------------------- rows
def _decode(r: dict) -> dict:
    r["sections"] = db.jload(r.get("sections"), []) or []
    r["terms"] = db.jload(r.get("terms"), {}) or {}
    r["flags"] = db.jload(r.get("flags"), []) or []
    return r


def _row(tenant, prop_id: int) -> dict | None:
    with tenant.db() as c:
        r = c.execute("SELECT * FROM proposals WHERE id=?", (prop_id,)).fetchone()
    return _sync(tenant, _decode(dict(r))) if r else None


def _sync(tenant, r: dict) -> dict:
    """A proposal whose approval item was rejected goes back to draft."""
    if r["status"] == "queued" and r.get("queue_id"):
        q = queue.get(tenant, r["queue_id"], scrub=False)
        if q is None or q["status"] == "rejected":
            with tenant.db() as c:
                c.execute("UPDATE proposals SET status='draft', queue_id=NULL, token=NULL, updated=? WHERE id=?",
                          (db.now(), r["id"]))
            r.update(status="draft", queue_id=None, token=None)
    return r


def view(tenant, r: dict, include_text: bool = True) -> dict:
    out = {k: r.get(k) for k in ("id", "owner_user_id", "prospect_id", "title", "status", "queue_id", "created", "updated",
                                 "views", "first_viewed", "last_viewed", "accepted_at", "accepted_name", "accepted_option",
                                 "sent_at", "demo", "run_id", "call_notes")}
    out["status_label"] = STATUS_LABELS.get(r["status"], r["status"])
    out["sections"] = r["sections"]
    out["options"] = r["terms"].get("options", [])
    out["pricing"] = r["terms"].get("pricing", {})
    out["numbers"] = r["terms"].get("numbers", {})
    out["flags"] = r["flags"]
    out["blocked"] = guardrails.blocked(r["flags"])
    out["url"] = public_url(tenant, r["token"]) if r.get("token") and r["status"] in ("published", "accepted") else None
    out["mode"], out["label"] = "rules", "Built from your call notes, their replies and the research (rules; AI not used)"
    if include_text:
        out["text"] = compile_text(r["title"] or "", r["sections"])
    return out


def visible(tenant, user: dict, prop_id: int) -> dict:
    r = _row(tenant, prop_id)
    if r is None or (user["role"] != "owner" and r["owner_user_id"] != user["id"]):
        raise ProposalError(404, "Not found")
    return r


def list_for(tenant, user: dict, prospect_id: int | None = None) -> list[dict]:
    sql, args = "SELECT * FROM proposals WHERE 1=1", []
    if user["role"] != "owner":
        sql += " AND owner_user_id=?"
        args.append(user["id"])
    if prospect_id:
        sql += " AND prospect_id=?"
        args.append(prospect_id)
    sql += " ORDER BY id DESC LIMIT 200"
    with tenant.db() as c:
        rows = [_decode(dict(r)) for r in c.execute(sql, args)]
        names = {r["id"]: r["name"] for r in c.execute("SELECT id, name FROM prospects")}
    out = []
    for r in rows:
        v = view(tenant, _sync(tenant, r), include_text=False)
        v["prospect_name"] = names.get(r["prospect_id"], "")
        out.append(v)
    return out


# --------------------------------------------------------------------------- build / edit / send / withdraw
def build(tenant, user: dict, prospect_id: int, call_notes: str = "", numbers: dict | None = None,
          pricing: dict | None = None, title: str | None = None, include_projection: bool = True) -> dict:
    try:
        p = P.visible(tenant, user, prospect_id)
    except P.ProspectError as e:
        raise ProposalError(e.status, str(e))
    if p["do_not_contact"]:
        raise ProposalError(409, "This prospect asked not to be contacted")
    nums = clean_numbers(numbers)
    pr = clean_pricing(pricing)
    notes = (call_notes or "").strip()[:6000]
    title = P._clean(title, 160) or f"Proposal for {P._biz(p) if p.get('business') else p['name']}"
    actor = P._actor(user)

    def fn(run: runtime.Run):
        secs, opts = run.step("sections", {"numbers": sorted(nums), "options": pr["options"]},
                              lambda: build_sections(tenant, p, notes, nums, pr, include_projection),
                              "Write the sections from their words, numbers and the playbook structures")
        flags = run.step("guardrails", {}, lambda: _check(tenant, title, secs, p["id"], None),
                         "Guardrails (client-asset mode)")
        now = db.now()
        with tenant.db() as c:
            pid_ = c.execute("INSERT INTO proposals(owner_user_id,prospect_id,title,body,terms,status,created,sections,flags,"
                             "call_notes,mode,run_id,demo,updated,views) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                             (p["owner_user_id"], p["id"], title, compile_text(title, secs),
                              db.jdump({"pricing": pr, "options": opts, "numbers": nums}), "draft", now, db.jdump(secs),
                              db.jdump(flags), notes, "rules", run.id, 1 if tenant.is_demo else 0, now)).lastrowid
        P._event(tenant, p["id"], "proposal_created", {"proposal_id": pid_}, actor)
        return {"proposal_id": pid_, "blocked": guardrails.blocked(flags)}

    out = runtime.execute(tenant, P.FEATURE, f"Proposal: {p['name']}",
                          ["Write the sections from their words, numbers and the playbook structures",
                           "Guardrails (client-asset mode)"], fn, actor)
    audit.log(tenant, actor, "prospects", {"proposal": out["result"]["proposal_id"]})
    return {"proposal": view(tenant, _row(tenant, out["result"]["proposal_id"])), "run_id": out["run_id"]}


def update(tenant, user: dict, prop_id: int, sections: list[dict] | None = None, pricing: dict | None = None,
           title: str | None = None) -> dict:
    r = visible(tenant, user, prop_id)
    if r["status"] != "draft":
        raise ProposalError(409, "Only a draft can be edited. Withdraw it and build a new one.")
    secs = r["sections"]
    terms = r["terms"]
    if pricing is not None:
        pr = clean_pricing(pricing)
        old_cur = (terms.get("pricing") or {}).get("currency")
        if old_cur and pr["currency"] != old_cur:
            raise ProposalError(422, f"This proposal is in {old_cur}: its costs and projection use that currency. "
                                     "Build a new proposal to use a different currency.")
        opts = options_for(pr)
        terms = {**terms, "pricing": pr, "options": opts}
        secs = [{**s, "text": "\n".join(f"- {o['label']}: {o['summary']}" for o in opts)} if s["key"] == "options" else s
                for s in secs]
    if sections is not None:
        by_key = {s.get("key"): s for s in sections if isinstance(s, dict)}
        new = []
        for s in secs:
            e = by_key.get(s["key"])
            if e is not None and s["key"] not in ("options", "no_guarantee"):
                txt = str(e.get("text") or "").strip()[:6000]
                if not txt:
                    raise ProposalError(422, f"\"{s['title']}\" can't be empty")
                s = {**s, "text": txt, "title": P._clean(e.get("title"), 120) or s["title"]}
            new.append(s)
        secs = new
    title = P._clean(title, 160) or r["title"]
    flags = _check(tenant, title, secs, r["prospect_id"], prop_id)
    with tenant.db() as c:
        c.execute("UPDATE proposals SET title=?, sections=?, terms=?, flags=?, body=?, updated=? WHERE id=?",
                  (title, db.jdump(secs), db.jdump(terms), db.jdump(flags), compile_text(title, secs), db.now(), prop_id))
    return view(tenant, _row(tenant, prop_id))


def send(tenant, user: dict, prop_id: int, channel: str = "email", message: str | None = None) -> dict:
    r = visible(tenant, user, prop_id)
    if r["status"] != "draft":
        raise ProposalError(409, f"This proposal is already {STATUS_LABELS.get(r['status'], r['status']).lower()}")
    p = P._row(tenant, r["prospect_id"])
    if p is None:
        raise ProposalError(404, "Not found")
    try:
        P._check_contactable({**p, "stage": "replied" if p["stage"] in ("won", "lost") else p["stage"]}, channel, tenant)
    except P.ProspectError as e:
        raise ProposalError(e.status, str(e))
    flags = _check(tenant, r["title"], r["sections"], r["prospect_id"], prop_id)
    if guardrails.blocked(flags):
        with tenant.db() as c:
            c.execute("UPDATE proposals SET flags=? WHERE id=?", (db.jdump(flags), prop_id))
        raise ProposalError(409, "Fix the flagged lines before sending")
    token = secrets.token_urlsafe(18)
    url = public_url(tenant, token)
    first = P._first_name(p)
    s = P.sender_of(tenant, p["owner_user_id"])
    cover = P._clean(message, 1500) or ("Thanks for the conversation. Here's the proposal we discussed. It covers where you "
                                        "are now, the plan for the first 90 days, the options and the terms. If it looks "
                                        "right you can accept it on the page; if not, just reply with any questions.")
    greet = f"Hi {first}," if first else "Hi,"
    sig = "\n".join(x for x in (s.get("name"), s.get("business")) if x)
    if channel == "email":
        body = f"{greet}\n\n{cover}\n\n{url}\n\n{sig}\n\n---\n{compile_text(r['title'], r['sections'])}"
    else:
        # a DM has a length limit: shorten the cover note, never the link
        room = max(0, 990 - len(greet) - len(url) - 2)
        short = cover if len(cover) <= room else cover[: max(0, room - 3)].rstrip() + "..."
        body = f"{greet} {short} {url}"
    actor = P._actor(user)
    meta = {"feature": P.FEATURE, "owner_user_id": p["owner_user_id"], "prospect_id": p["id"], "proposal_id": prop_id,
            "client_asset": True, "prior_contact": True, "touch": "proposal",
            **({"subject": r["title"]} if channel == "email" else {})}
    item = queue.add(tenant, "proposal", body, channel=channel, recipient_ref=P._recipient(p, channel),
                     title=f"Proposal to {p['name']}", meta=meta, run_id=r.get("run_id"), actor=actor)
    with tenant.db() as c:
        c.execute("UPDATE proposals SET status='queued', token=?, queue_id=?, flags=?, updated=? WHERE id=?",
                  (token, item["id"], db.jdump(flags), db.now(), prop_id))
    P._event(tenant, p["id"], "proposal_queued", {"proposal_id": prop_id, "queue_id": item["id"]}, actor)
    return {"proposal": view(tenant, _row(tenant, prop_id)), "queue_id": item["id"], "status": item["status"]}


def withdraw(tenant, user: dict, prop_id: int) -> dict:
    r = visible(tenant, user, prop_id)
    if r["status"] in ("accepted", "withdrawn"):
        raise ProposalError(409, f"This proposal is already {r['status']}")
    actor = P._actor(user)
    if r["status"] == "queued" and r.get("queue_id"):
        try:
            queue.reject(tenant, r["queue_id"], actor, role="owner", reason="Proposal withdrawn")
        except queue.QueueError:
            pass
    with tenant.db() as c:
        c.execute("UPDATE proposals SET status='withdrawn', updated=? WHERE id=?", (db.now(), prop_id))
    P._event(tenant, r["prospect_id"], "proposal_withdrawn", {"proposal_id": prop_id}, actor)
    return view(tenant, _row(tenant, prop_id))


@queue.on_approve("proposal")
def _approved(tenant, item: dict, actor: str) -> None:
    pid_ = (item.get("meta") or {}).get("proposal_id")
    if not pid_:
        return
    with tenant.db() as c:
        n = c.execute("UPDATE proposals SET status='published', updated=? WHERE id=? AND status='queued'",
                      (db.now(), int(pid_))).rowcount
        r = c.execute("SELECT prospect_id FROM proposals WHERE id=?", (int(pid_),)).fetchone()
    if not n or r is None:
        return
    P._event(tenant, r["prospect_id"], "proposal_sent", {"proposal_id": int(pid_), "queue_id": item["id"]}, actor)
    p = P._row(tenant, r["prospect_id"])
    if p and not p["do_not_contact"] and p["stage"] in P.EARLY + ("contacted", "replied", "call_booked", "nurture"):
        P.set_stage(tenant, p["id"], "proposal", actor)


@queue.on_sent("proposal")
def _sent(tenant, item: dict, actor: str) -> None:
    pid_ = (item.get("meta") or {}).get("proposal_id")
    if pid_:
        with tenant.db() as c:
            c.execute("UPDATE proposals SET sent_at=COALESCE(sent_at, ?) WHERE id=?", (db.now(), int(pid_)))


# --------------------------------------------------------------------------- the public page
def by_token(tenant, token: str) -> dict | None:
    if not token or len(token) > 80 or not re.match(r"^[A-Za-z0-9_\-]+$", token):
        return None
    with tenant.db() as c:
        r = c.execute("SELECT * FROM proposals WHERE token=?", (token,)).fetchone()
    if r is None:
        return None
    r = _decode(dict(r))
    return r if r["status"] in ("published", "accepted") else None


VIEW_DEDUPE_S = 30 * 60
_recent_views: dict[tuple, float] = {}


def record_view(tenant, r: dict, count: bool, ip: str | None = None) -> None:
    """Counts an opening of the page. The team's own views don't count, and repeat loads from the same network
    address within 30 minutes count once (so a refresh doesn't inflate the number)."""
    if not count or r["status"] != "published":
        return
    now = db.now()
    if ip is not None:
        key = (tenant.id, r["id"], _ip_hash(tenant, ip or "?"))
        last = _recent_views.get(key)
        if last is not None and now - last < VIEW_DEDUPE_S:
            return
        if len(_recent_views) > 20_000:
            for k in [k for k, v in _recent_views.items() if now - v >= VIEW_DEDUPE_S]:
                _recent_views.pop(k, None)
        _recent_views[key] = now
    with tenant.db() as c:
        c.execute("UPDATE proposals SET views=COALESCE(views,0)+1, first_viewed=COALESCE(first_viewed,?), last_viewed=? "
                  "WHERE id=?", (now, now, r["id"]))
    if not r.get("first_viewed"):
        P._event(tenant, r["prospect_id"], "proposal_viewed", {"proposal_id": r["id"]}, "prospect")


def _ip_hash(tenant, ip: str) -> str:
    salt = tenant.get("proposal_ip_salt")
    if not salt:
        salt = secrets.token_hex(16)
        tenant.set("proposal_ip_salt", salt)
    return hashlib.sha256(f"{salt}|{ip}".encode()).hexdigest()[:32]


def accept(tenant, token: str, name: str, agree: bool, option: str | None, ip: str) -> dict:
    r = by_token(tenant, token)
    if r is None:
        raise ProposalError(404, "This proposal isn't available")
    if r["status"] == "accepted":
        raise ProposalError(409, "This proposal has already been accepted")
    name = re.sub(r"\s+", " ", name or "").strip()
    if not (2 <= len(name) <= 120) or not re.search(r"[^\W\d_]", name):
        raise ProposalError(422, "Type your full name to accept")
    if agree is not True:
        raise ProposalError(422, "Tick the box to confirm you agree to the terms")
    opts = r["terms"].get("options") or []
    keys = [o["key"] for o in opts]
    if option is None and len(keys) == 1:
        option = keys[0]
    if keys and option not in keys:
        raise ProposalError(422, "Choose one of the options")
    chosen = next((o for o in opts if o["key"] == option), None)
    now = db.now()
    with tenant.db() as c:
        n = c.execute("UPDATE proposals SET status='accepted', accepted_at=?, accepted_name=?, accepted_ip_hash=?, "
                      "accepted_option=?, updated=? WHERE id=? AND status='published'",
                      (now, name, _ip_hash(tenant, ip or "?"), option, now, r["id"])).rowcount
    if not n:
        raise ProposalError(409, "This proposal has already been accepted")
    P._event(tenant, r["prospect_id"], "proposal_accepted", {"proposal_id": r["id"], "option": option, "name": name},
             "prospect")
    audit.log(tenant, "prospect", "prospects", {"proposal_accepted": r["id"], "option": option})
    try:
        P.mark_won(tenant, r["prospect_id"], "prospect", price=(chosen or {}).get("amount"),
                   offer=(chosen or {}).get("label") or r["title"], via="proposal")
    except P.ProspectError:
        pass
    return {"ok": True, "accepted_at": now, "option": option}


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow">
<title>{title}</title><style>
*{{box-sizing:border-box}}body{{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:0;background:#f6f6f3;color:#16181d;
line-height:1.55}}main{{max-width:760px;margin:0 auto;padding:24px 16px 64px}}header{{margin-bottom:16px}}
.brand{{font-size:13px;color:#5d6270;font-weight:600}}h1{{font-size:24px;margin:4px 0 0}}
section{{background:#fff;border:1px solid #e4e3dd;border-radius:12px;padding:16px;margin-top:12px}}h2{{font-size:16px;margin:0 0 8px}}
ul{{margin:0;padding-left:20px}}li{{margin:4px 0}}p{{margin:0 0 6px}}.muted{{color:#5d6270;font-size:13px}}
.opt{{display:flex;gap:10px;align-items:flex-start;border:1px solid #e4e3dd;border-radius:10px;padding:12px;margin-top:8px;cursor:pointer}}
.opt b{{display:block}}.amt{{font-size:18px;font-weight:700}}input[type=text]{{width:100%;font:inherit;padding:10px;border:1px solid #d9d8d2;
border-radius:8px;margin-top:6px}}button{{font:inherit;background:#16a34a;color:#fff;border:0;border-radius:8px;padding:10px 16px;
margin-top:12px;cursor:pointer;width:100%}}.ok{{background:#ecfdf3;border-color:#bbf7d0}}#err{{color:#b91c1c}}
.assume{{margin-top:8px;font-size:13px;color:#5d6270}}</style></head><body><main>
<header><div class="brand">{brand}</div><h1>{title}</h1><p class="muted">Prepared for {who}</p></header>
{sections}
{accept}
</main>{script}</body></html>"""


def _render_text(text: str) -> str:
    out, items = [], []
    for line in (text or "").split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("- "):
            items.append(f"<li>{html.escape(line[2:])}</li>")
        else:
            if items:
                out.append("<ul>" + "".join(items) + "</ul>")
                items = []
            out.append(f"<p>{html.escape(line)}</p>")
    if items:
        out.append("<ul>" + "".join(items) + "</ul>")
    return "".join(out)


def render_html(tenant, r: dict) -> str:
    names = guardrails.tenant_banned_names(tenant)
    sc = (lambda s: guardrails.scrub(s or "", names)) if names else (lambda s: s or "")
    p = P._row(tenant, r["prospect_id"]) or {}
    brand = ((tenant.get("brand") or {}) or {}).get("name") or ""
    secs = []
    for s in r["sections"]:
        if s["key"] == "options":
            continue
        body = _render_text(sc(s["text"]))
        if s.get("assumptions"):
            body += "<div class='assume'><b>Assumptions:</b><ul>" + "".join(
                f"<li>{html.escape(sc(a))}</li>" for a in s["assumptions"]) + "</ul></div>"
        secs.append(f"<section><h2>{html.escape(sc(s['title']))}</h2>{body}</section>")
    opts = r["terms"].get("options") or []
    accepted = r["status"] == "accepted"
    opt_html = "".join(
        f"<label class='opt'><input type='radio' name='option' value='{html.escape(o['key'])}' "
        f"aria-label='{html.escape(o['label'])}'"
        f"{' checked' if (len(opts) == 1 or o['key'] == r.get('accepted_option')) else ''}{' disabled' if accepted else ''}>"
        f"<span><b>{html.escape(o['label'])}</b><span class='amt'>{html.escape(money(o['amount'], o['currency']))}</span>"
        f" <span class='muted'>{'a month' if o['per'] == 'month' else 'one-off'}</span><br>{html.escape(sc(o['summary']))}</span>"
        f"</label>" for o in opts)
    # the options section is shown before terms
    opt_section = f"<section><h2>Options</h2>{opt_html}</section>"
    idx = next((i for i, s in enumerate([x for x in r["sections"] if x["key"] != "options"]) if s["key"] == "terms"), len(secs))
    secs.insert(idx, opt_section)
    if accepted:
        when = time.strftime("%d %b %Y %H:%M", time.localtime(r["accepted_at"] or 0))
        acc = (f"<section class='ok'><h2>Accepted</h2><p>Accepted by {html.escape(r.get('accepted_name') or '')} on "
               f"{html.escape(when)}.</p></section>")
        script = ""
    else:
        acc = ("<section><h2>Accept this proposal</h2><p class='muted'>Choose an option above, type your full name and tick "
               "the box. We record your name, the time and a scrambled version of your network address as your "
               "acceptance.</p><label class='muted' for='nm'>Your full name</label><input id='nm' type='text' "
               "autocomplete='name' maxlength='120'><label style='display:flex;gap:8px;align-items:flex-start;margin-top:10px'>"
               "<input id='ag' type='checkbox' style='margin-top:4px'><span>I've read this proposal and agree to its terms."
               "</span></label><button id='go' type='button'>Accept proposal</button><p id='err' role='alert'></p></section>")
        script = ("<script>document.getElementById('go').onclick=async()=>{const e=document.getElementById('err');e.textContent='';"
                  "const o=document.querySelector('input[name=option]:checked');try{const r=await fetch(location.pathname"
                  ".replace(/\\/$/,'')+'/accept',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify("
                  "{name:document.getElementById('nm').value,agree:document.getElementById('ag').checked,option:o?o.value:null})});"
                  "const d=await r.json().catch(()=>({}));if(!r.ok){e.textContent=typeof d.detail==='string'?d.detail:"
                  "'Please check the form and try again.';return;}location.reload();}catch(x){e.textContent="
                  "\"Couldn't reach the server. Try again.\";}};</script>")
    who = html.escape(sc(re.sub(r"\s*\(DEMO\)", " (DEMO)", p.get("business") or p.get("name") or "you")))
    return _PAGE.format(title=html.escape(sc(r["title"] or "Proposal")), brand=html.escape(sc(brand)), who=who,
                        sections="".join(secs), accept=acc, script=script)


NOT_AVAILABLE = ("<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
                 "content='width=device-width,initial-scale=1'><meta name='robots' content='noindex'><title>Proposal</title>"
                 "</head><body style='font-family:system-ui;padding:24px;background:#f6f6f3'><main style='max-width:520px;"
                 "margin:auto;background:#fff;border:1px solid #e4e3dd;border-radius:12px;padding:16px'><h1 style='font-size:20px'>"
                 "This proposal isn't available</h1><p>The link may be wrong, or the proposal was withdrawn. Reply to the "
                 "message you got it from and we'll sort it out.</p></main></body></html>")
