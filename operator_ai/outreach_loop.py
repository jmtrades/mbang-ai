"""Approval-gated outreach loop on practice prospects (DEMO).

Opener -> value-only follow-ups -> stage tracking (sent, replied, quality conversation, link
sent, booked, showed, closed). Plus a DM opener lab (reply rate by opening move with a 95%
Wilson interval), a weekly funnel, a setter league (cap of 5 a day per sender) and a
revenue-goal calculator. Every message goes to the approval queue; approved items go to the
mock Instagram transport."""
from __future__ import annotations

import math
import time
from collections import defaultdict

from . import db, queue, runtime

STAGES = ["new", "sent", "replied", "quality_conversation", "link_sent", "booked", "showed", "closed"]
DAILY_CAP = 5
# Opener and follow-up wording is filled from the prospect's niche and the workspace brief (brief.build), so a
# fitness coach never hears about diaries and a clinic never hears about e-commerce orders.
MOVES = {
    "compliment": "Loved your recent post. How's it been landing with new clients?",
    "question": "Quick question: what's the main thing holding back {outcome} for you right now?",
    "resource": "I put together a one-page checklist for {niche} on getting {outcome} without {pain}. Want it?",
    "mutual": "Saw we're both in the {niche} community. What's working best for you on {outcome} at the moment?",
}
FOLLOWUPS = [
    "Here's a 2-minute video on how {niche} get {outcome} without {pain}. Want me to send it?",
    "One quick tip: reply to every new enquiry within five minutes. How quickly do replies go out today?",
]


def _fill(tenant, template: str, niche: str | None) -> str:
    from . import brief as brief_mod

    b = brief_mod.build(tenant, {})["brief"]
    return template.format(niche=niche or b["niche"], outcome=b["outcome"], pain=b["pain"], mechanism=b["mechanism"])


def cap(tenant) -> int:
    try:
        return int(tenant.get("outreach_daily_cap", DAILY_CAP) or DAILY_CAP)
    except (TypeError, ValueError):
        return DAILY_CAP


def _today_start() -> float:
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def sent_today(tenant, sender: str) -> int:
    """Openers committed today by this sender: confirmed sends plus drafts still waiting to go. Rejected,
    cancelled and failed drafts never count against the cap."""
    with tenant.db() as c:
        return c.execute("SELECT COUNT(*) FROM outreach_loops l LEFT JOIN queue q ON q.id=l.queue_id WHERE l.sender=? "
                         "AND l.step='opener' AND l.created>=? AND COALESCE(q.status,'pending') NOT IN "
                         "('rejected','cancelled','send_failed')", (sender, _today_start())).fetchone()[0]


def prospects(tenant) -> list[dict]:
    with tenant.db() as c:
        return db.rows(c.execute("SELECT * FROM outreach_practice_prospects ORDER BY id"))


def queue_openers(tenant, sender: str | None = None, limit: int = 10, user: dict | None = None) -> dict:
    """Draft openers for new practice prospects, respecting the per-sender daily cap."""
    actor = f"{user['username']}({user['role']})" if user else "system"

    def fn(run: runtime.Run):
        with tenant.db() as c:
            rows = db.rows(c.execute("SELECT * FROM outreach_practice_prospects WHERE stage='new' ORDER BY id LIMIT ?", (limit,)))
        queued, deferred = [], []
        for p in rows:
            s = sender or p["sender"] or "setter"
            if sent_today(tenant, s) >= cap(tenant):
                deferred.append({"prospect_id": p["id"], "sender": s, "reason": f"daily cap of {cap(tenant)} reached"})
                continue
            body = _fill(tenant, MOVES[p["opener_move"]], p["niche"])
            item = queue.add(tenant, "dm_opener", body, channel="instagram_dm", recipient_ref=p["handle"],
                             title=f"Opener ({p['opener_move']}) to {p['name']}",
                             meta={"prospect_facing": True, "lead_tier": p["tier"] or "cold", "prospect_id": p["id"],
                                   "opener_move": p["opener_move"], "sender": s, "practice": True},
                             run_id=run.id, actor=actor, demo=True)
            with tenant.db() as c:
                c.execute("INSERT INTO outreach_loops(prospect_id,step,queue_id,opener_move,sender,status,created) VALUES(?,?,?,?,?,?,?)",
                          (p["id"], "opener", item["id"], p["opener_move"], s, item["status"], db.now()))
                c.execute("UPDATE outreach_practice_prospects SET stage='queued', sender=? WHERE id=?", (s, p["id"]))
            queued.append(item["id"])
        run.note("queue", {"limit": limit}, {"queued": len(queued), "deferred": len(deferred)}, label="Queue openers")
        return {"queued": queued, "deferred": deferred, "cap": cap(tenant), "sent": 0}

    out = runtime.execute(tenant, "outreach", "Outreach loop: openers", ["Pick new practice prospects", "Queue openers"], fn, actor)
    return {**out["result"], "run_id": out["run_id"]}


@queue.on_sent("dm_opener")
def _opener_sent(tenant, item: dict, actor: str) -> None:
    """Stage moves on a confirmed send (not on approval), so reply rates count real sends."""
    pid = item["meta"].get("prospect_id")
    if pid and item["meta"].get("practice"):
        with tenant.db() as c:
            c.execute("UPDATE outreach_practice_prospects SET stage='sent' WHERE id=? AND stage IN ('new','queued')", (pid,))
            c.execute("UPDATE outreach_loops SET status='sent' WHERE queue_id=?", (item["id"],))


def record_reply(tenant, prospect_id: int, text: str = "") -> dict:
    """Practice: record that a prospect replied (the mock transport has no real inbox)."""
    with tenant.db() as c:
        p = c.execute("SELECT * FROM outreach_practice_prospects WHERE id=?", (prospect_id,)).fetchone()
        if p is None:
            raise KeyError(prospect_id)
        c.execute("UPDATE outreach_practice_prospects SET replied=1, stage='replied' WHERE id=?", (prospect_id,))
        c.execute("INSERT INTO channel_messages(channel,direction,ext_id,body,meta,created) VALUES(?,?,?,?,?,?)",
                  ("instagram_dm", "in", p["handle"], text or "(practice reply)", db.jdump({"practice": True}), db.now()))
    return {"ok": True}


def advance(tenant, prospect_id: int, stage: str) -> dict:
    if stage not in STAGES:
        raise ValueError(stage)
    with tenant.db() as c:
        n = c.execute("UPDATE outreach_practice_prospects SET stage=? WHERE id=?", (stage, prospect_id)).rowcount
    if not n:
        raise KeyError(prospect_id)
    return {"ok": True, "stage": stage}


def queue_followups(tenant, user: dict | None = None) -> dict:
    actor = f"{user['username']}({user['role']})" if user else "system"
    with tenant.db() as c:
        rows = db.rows(c.execute("SELECT * FROM outreach_practice_prospects WHERE stage='sent' AND replied=0"))
        done = {(r["prospect_id"], r["step"]) for r in c.execute("SELECT prospect_id, step FROM outreach_loops")}
    ids = []
    for p in rows:
        step_n = next((i for i in range(len(FOLLOWUPS)) if (p["id"], f"followup{i + 1}") not in done), None)
        if step_n is None:
            continue
        item = queue.add(tenant, "followup", _fill(tenant, FOLLOWUPS[step_n], p["niche"]), channel="instagram_dm",
                         recipient_ref=p["handle"],
                         title=f"Follow-up {step_n + 1} to {p['name']}",
                         meta={"prospect_facing": True, "lead_tier": p["tier"] or "cold", "prospect_id": p["id"],
                               "prior_contact": True, "practice": True}, actor=actor, demo=True)
        with tenant.db() as c:
            c.execute("INSERT INTO outreach_loops(prospect_id,step,queue_id,opener_move,sender,status,created) VALUES(?,?,?,?,?,?,?)",
                      (p["id"], f"followup{step_n + 1}", item["id"], p["opener_move"], p["sender"], item["status"], db.now()))
        ids.append(item["id"])
    return {"queued": ids, "sent": 0}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, centre - half), min(1.0, centre + half))


def opener_lab(tenant) -> list[dict]:
    with tenant.db() as c:
        rows = c.execute("SELECT opener_move, COUNT(*) n, SUM(replied) r FROM outreach_practice_prospects "
                         "WHERE stage NOT IN ('new','queued') GROUP BY opener_move").fetchall()
    out = []
    for r in rows:
        n, k = r["n"], r["r"] or 0
        lo, hi = wilson(k, n)
        out.append({"move": r["opener_move"], "sent": n, "replies": k, "rate": round(k / n, 3) if n else 0,
                    "ci95": [round(lo, 3), round(hi, 3)]})
    return sorted(out, key=lambda x: -x["rate"])


def weekly_funnel(tenant) -> list[dict]:
    with tenant.db() as c:
        rows = c.execute("SELECT stage, created FROM outreach_practice_prospects").fetchall()
    order = {s: i for i, s in enumerate(STAGES)}
    weeks: dict[str, dict] = defaultdict(lambda: {s: 0 for s in STAGES[1:]})
    for r in rows:
        wk = time.strftime("%G-W%V", time.localtime(r["created"]))
        reached = order.get(r["stage"], 0)
        for s in STAGES[1:]:
            if order[s] <= reached:
                weeks[wk][s] += 1
    return [{"week": k, **v} for k, v in sorted(weeks.items())]


def league(tenant) -> list[dict]:
    with tenant.db() as c:
        rows = c.execute("SELECT sender, COUNT(*) n, SUM(replied) r, SUM(CASE WHEN stage IN ('booked','showed','closed') "
                         "THEN 1 ELSE 0 END) b FROM outreach_practice_prospects WHERE stage NOT IN ('new','queued') "
                         "GROUP BY sender").fetchall()
    out = [{"sender": r["sender"], "sent": r["n"], "replies": r["r"] or 0, "booked": r["b"] or 0,
            "reply_rate": round((r["r"] or 0) / r["n"], 3) if r["n"] else 0, "today": sent_today(tenant, r["sender"]),
            "cap": cap(tenant)} for r in rows]
    return sorted(out, key=lambda x: (-x["booked"], -x["reply_rate"]))


def goal_calculator(goal: float, price: float, close_rate: float = 0.2, show_rate: float = 0.6, book_rate: float = 0.3,
                    reply_rate: float = 0.1, convo_rate: float = 0.4, link_rate: float = 0.5, working_days: int = 22,
                    senders: int = 1, per_sender_cap: int = DAILY_CAP) -> dict:
    """Revenue goal -> DMs a day. Capped: per-sender volume can't exceed a sustainable cap, so
    the calculator says how many senders the goal needs instead of asking one person to spam."""
    price = max(price, 1)
    sales = goal / price
    rates = [close_rate, show_rate, book_rate, link_rate, convo_rate, reply_rate]
    funnel = 1.0
    for r in rates:
        funnel *= max(r, 1e-6)
    dms_month = sales / funnel
    per_day = dms_month / working_days
    capped = per_day > senders * per_sender_cap
    return {"sales_needed": round(sales, 1), "dms_per_month": round(dms_month), "dms_per_day": round(per_day),
            "per_sender_cap": per_sender_cap, "senders_needed": max(1, math.ceil(per_day / per_sender_cap)),
            "capped": capped, "dms_per_day_capped": min(round(per_day), senders * per_sender_cap),
            "note": "Model, not a promise. Rates default to the SOP benchmarks."}
