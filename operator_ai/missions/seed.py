"""Demo seed: one first-client mission for the demo student, part-way through (the early steps done, today's
opener block waiting for approval). Fictional, labelled (DEMO), demo database only."""
from __future__ import annotations

from .. import auth, db, queue
from . import engine, registry, templates
from .registry import DAY, clock

DEMO_TARGETS = [("Lena Brooks (DEMO)", "@lena_fit_demo"), ("Marcus Hale (DEMO)", "@marcus_lifts_demo"),
                ("Priya Nandan (DEMO)", "@priya_moves_demo")]


def _ev(c, mid: int, kind: str, detail: dict, at: float) -> None:
    c.execute("INSERT INTO mission_events(mission_id,kind,detail,created) VALUES(?,?,?,?)", (mid, kind, db.jdump(detail), at))


def seed_demo(tenant) -> dict | None:
    from .. import tenancy

    if not tenant.is_demo or not tenancy.is_demo_db(tenant):
        return None  # the real workspace is never seeded
    with tenant.db() as c:
        u = db.row(c.execute("SELECT * FROM users WHERE username='demo-student'").fetchone())
        if u is None or c.execute("SELECT 1 FROM missions WHERE user_id=?", (u["id"],)).fetchone():
            return None
    user = auth.user_public(u)
    now = clock()
    created = now - 5 * DAY
    answers = {"niche": "fitness coaches (DEMO)", "offer": "Done-for-you DM setting (DEMO)", "price": 2000.0,
               "hours_per_day": 2.0, "channels": "Instagram DMs"}
    m = engine.create(tenant, user, "Win my first client (DEMO)", "first_client", answers, start=False, created=created)
    mid = m["id"]
    targets = templates.targets_for(tenant, "first_client", answers)
    engine._patch_context(tenant, mid, {"targets": targets, "icp": {"niche": answers["niche"]},
                                        "offer": {"offer": answers["offer"], "price": answers["price"]},
                                        "opener_move": "question",
                                        "decisions": [{"at": created + 3600, "rule": "intake", "source": "rules",
                                                       "why": "Started with the 'ask one short question' opener (DEMO)", "by": "demo-student"}],
                                        "what_worked": ["Short openers with one question got the first reply (DEMO)"]})
    by = {s["skey"]: s for s in m["steps"]}
    done = {
        "intake": targets["summary"],
        "offer_lesson": "Offer and USP lesson and quiz passed (DEMO).",
        "offer_doc": "Offer document approved (DEMO).",
        "profile": "Profile copy approved (DEMO).",
        "prospects": "24 prospects on your list (DEMO).",
        "research": "Researched 20 prospect(s) (DEMO).",
        "dm_cert": "DM-setting certification passed (DEMO).",
    }
    rows = registry.prospects(tenant, user["id"], "AND COALESCE(do_not_contact,0)=0 AND handle IS NOT NULL", limit=3)
    picks = [(r.get("name") or h, r.get("handle"), r["id"]) for r, (_, h) in zip(rows, DEMO_TARGETS)] or \
        [(n, h, None) for n, h in DEMO_TARGETS]
    qids = []
    for name, handle, pid in picks:
        body = f"Hi {registry._first(name)}, " + registry.OPENER_MOVES["question"][0].lower() + registry.OPENER_MOVES["question"][1:]
        item = queue.add(tenant, "dm_opener", body, channel="instagram_dm", recipient_ref=handle,
                         title=f"Opener to {name}", actor="system", demo=True,
                         meta={"prospect_facing": True, "lead_tier": "cold", "prospect_id": pid, "opener_move": "question",
                               "first_touch": True, "mission_id": mid, "step_id": by["openers"]["id"],
                               "owner_user_id": user["id"], "mission_step": "openers", "tag": "seed"})
        qids.append(item["id"])
    nxt = registry.next_morning(now)
    with tenant.db() as c, db.tx(c):
        for i, (k, msg) in enumerate(done.items()):
            at = created + 3600 * (i + 1)
            c.execute("UPDATE mission_steps SET status='done', message=?, updated=?, attempts=1 WHERE id=?",
                      (msg, at, by[k]["id"]))
            _ev(c, mid, "step", {"step": k, "title": by[k]["title"], "from": "running", "to": "done", "message": msg}, at)
        c.execute("UPDATE mission_steps SET status='waiting_approval', cycles=1, attempts=2, queue_ids=?, output=?, message=?, "
                  "wait='{}', updated=? WHERE id=?",
                  (db.jdump(qids), db.jdump({"block": 2, "block_qids": qids, "approved_total": 5, "source": "rules",
                                             "move": "question"}),
                   f"Block 2: {len(qids)} opener(s) drafted (cap 5 a day). Approve them in the queue.", now, by["openers"]["id"]))
        c.execute("UPDATE mission_steps SET status='waiting_time', cycles=1, attempts=1, due=?, message=?, updated=? WHERE id=?",
                  (nxt, f"Next check {registry.when(nxt)}.", now, by["followups"]["id"]))
        c.execute("UPDATE mission_steps SET status='waiting_event', attempts=1, output=?, wait=?, message=?, updated=? WHERE id=?",
                  (db.jdump({"replies": 1, "positive": 0}),
                   db.jdump({"events": ["reply_received", "prospect_stage", "client_won"]}),
                   "1 reply so far, 0 positive. Waiting for the next one.", now, by["replies"]["id"]))
        for k, d in (("day7", 7), ("day14", 14), ("day30", 30)):
            due = created + d * DAY
            c.execute("UPDATE mission_steps SET status='waiting_time', attempts=1, due=?, message=?, updated=? WHERE id=?",
                      (due, f"Day {d} check on {registry.when(due)}.", now, by[k]["id"]))
        for i in range(5):
            _ev(c, mid, "sent", {"key": f"demo:{i}", "kind": "dm_opener", "channel": "instagram_dm", "first_touch": True,
                                 "prospect_id": None, "demo": True}, created + DAY + 600 * i)
        _ev(c, mid, "reply", {"key": "demo:reply", "party_type": "prospect", "party_id": "demo-1", "positive": False,
                              "classification": "question", "demo": True}, created + 2 * DAY)
        c.execute("UPDATE missions SET next_check=?, updated=? WHERE id=?", (now + 600, now, mid))
    return {"mission_id": mid, "queue_ids": qids}
