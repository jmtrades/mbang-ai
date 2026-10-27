"""Studio: seven lines of work (outreach, ads, funnels, creatives, webinars, student training,
client delivery). Each goes brief -> draft -> rubric and guardrail review -> approval queue.
The Launch kit takes one brief and queues every line at once. Nothing sends. Drafts come from
rules/templates; a free-form AI draft is used only when the AI budget allows."""
from __future__ import annotations

import secrets

from . import brief as brief_mod
from . import critique, db, guardrails, llm, queue, runtime

LINES = {
    "outreach": "Outreach", "ads": "Ads", "funnels": "Funnels", "creatives": "Creatives", "webinars": "Webinars",
    "training": "Student training", "delivery": "Client delivery",
}


def _drafts(line: str, b: dict, tenant=None) -> list[dict]:
    """Rules drafts per line: (title, asset_type, body, meta)."""
    n, o, mech, pain, aud = b["niche"], b["outcome"], b["mechanism"], b["pain"], b["audience"]
    if line == "outreach":
        return [
            {"title": "Opener DM (question move)", "asset": "dm_script", "kind": "dm_opener", "channel": "instagram_dm",
             "body": f"Saw your post about filling the diary. What's the main thing holding new bookings back right now?",
             "meta": {"prospect_facing": True, "lead_tier": "cold"}},
            {"title": "Value-only follow-up", "asset": "followup", "kind": "followup", "channel": "instagram_dm",
             "body": f"Here's a short video on how {n} get {o} without {pain}. Want me to send it over?",
             "meta": {"prospect_facing": True, "lead_tier": "cold", "prior_contact": True}},
            {"title": "Call offer (warm lead)", "asset": "dm_script", "kind": "dm", "channel": "instagram_dm",
             "body": guardrails.range_line("[their brand]", f"our {mech}", tenant) + " Would a quick call be useful?",
             "meta": {"prospect_facing": True, "lead_tier": "warm", "prior_contact": True}},
        ]
    if line == "ads":
        return [{"title": f"Ad {i + 1}", "asset": "ad", "kind": "ad_copy", "channel": "manual",
                 "body": f"{hook}\nWe help {aud} get {o} through our {mech}. Book a call.", "meta": {"client_asset": True}}
                for i, hook in enumerate([f"Stop relying on {pain}.", f"{o.capitalize()} in {b['timeframe']}.",
                                          f"The {mech}, explained."])]
    if line == "funnels":
        return [{"title": "Landing page", "asset": "funnel", "kind": "page", "channel": "manual",
                 "body": f"Headline: {o.capitalize()} without {pain}.\nSubhead: For {aud} who want a predictable diary.\n"
                         f"- The {mech} in plain steps\n- What it costs you to stay where you are\n- Who this is for (and not for)\nRegister now",
                 "meta": {"client_asset": True}}]
    if line == "creatives":
        return [{"title": f"Thunder Dome creative {chr(65 + i)}", "asset": "script", "kind": "creative_brief", "channel": "manual",
                 "body": f"Hook: {h}\nVisual: same talking-head setup for every creative (one variable changed: the hook).\n"
                         f"Budget: equal across 3-5 creatives, 72 hours, kill rule at 2x target cost per lead.",
                 "meta": {"client_asset": True}}
                for i, h in enumerate([f"Your diary shouldn't depend on {pain}.", f"What {n} get wrong about referrals.",
                                       f"{o.capitalize()}: the simple version."])]
    if line == "webinars":
        segs = "\n".join(f"Segment {i + 1}: {nm} ({m} min)" for i, (nm, m) in enumerate(
            [("Hook and promise", 5), ("Origin and credibility", 7), ("The big mistake", 10), ("The new mechanism", 15),
             ("Proof framework", 10), ("The offer", 12), ("Questions and close", 6)]))
        return [{"title": "Webinar outline", "asset": "webinar", "kind": "webinar_outline", "channel": "manual",
                 "body": f"How {n} get {o} without {pain}.\n{segs}", "meta": {"client_asset": True}}]
    if line == "training":
        return [{"title": "Student training: objection drill", "asset": "script", "kind": "training", "channel": "manual",
                 "body": "Drill: 'I need to think about it.' Ask what specifically they want to think through. "
                         "Drill: 'It's too expensive.' Go back to the gap. Drill: 'I need to ask my partner.' Offer a short call with both.",
                 "meta": {}}]
    if line == "delivery":
        return [{"title": "Client weekly report email", "asset": "email", "kind": "client_report", "channel": "email",
                 "body": f"Subject: Your weekly numbers\nHere's this week's funnel, the one bottleneck we're fixing, and next week's plan. "
                         f"Reply with any questions.", "meta": {"client_asset": True}}]
    raise KeyError(line)


def run_line(tenant, line: str, partial_brief: dict | None = None, user: dict | None = None, kit_id: str | None = None,
             run: runtime.Run | None = None) -> dict:
    if line not in LINES:
        raise KeyError(line)
    actor = f"{user['username']}({user['role']})" if user else "system"
    built = brief_mod.build(tenant, partial_brief)
    b = built["brief"]

    def fn(r: runtime.Run):
        r.note("brief", partial_brief or {}, {"brief": b, "missing": built["missing"]}, label="Brief")
        drafts = r.step("draft", {"line": line}, lambda: _drafts(line, b, tenant), "Draft (rules)")
        if llm.unavailable_reason(tenant) is None and line in ("ads", "outreach"):
            # free-form AI variant; it still goes through the same review below
            try:
                ai = llm.complete(tenant, "Write one short, honest variant. No guarantees, no income claims, one call to action.",
                                  f"Line: {line}\nBrief: {b}", purpose=f"studio:{line}", run_id=r.id, max_tokens=300)
                base = drafts[0]
                drafts.append({**base, "title": base["title"] + " (AI variant)", "body": ai["text"].strip(), "ai": True})
                r.use_ai("live")
            except llm.AIUnavailable:
                pass
        reviewed = []
        qids = []
        for d in drafts:
            res = r.step("review", {"title": d["title"]}, lambda d=d: critique.loop(tenant, d["body"], d["asset"], d["meta"],
                                                                                    brief=str(b), run=r),
                         f"Review: {d['title']}")
            item = queue.add(tenant, d["kind"], res["final"], channel=d["channel"], title=f"[{LINES[line]}] {d['title']}",
                             meta={**d["meta"], "studio_line": line, "kit_id": kit_id, "score": res["score"],
                                   "needs_human": res["needs_human"], "tag": f"studio:{kit_id or line}"},
                             run_id=r.id, actor=actor, demo=tenant.is_demo)
            if res["needs_human"] and item["status"] == "pending":
                with tenant.db() as c:
                    c.execute("UPDATE queue SET status='needs_human' WHERE id=?", (item["id"],))
                item["status"] = "needs_human"
            reviewed.append({"title": d["title"], "score": res["score"], "passes": res["passes"], "queue_id": item["id"],
                             "status": item["status"], "ai": bool(d.get("ai")), "citations": res["critique"]["citations"]})
            qids.append(item["id"])
        r.note("queue", {}, {"queue_ids": qids}, label="Approval queue")
        with tenant.db() as c:
            c.execute("INSERT INTO studio_runs(line,brief,run_id,queue_ids,kit_id,created) VALUES(?,?,?,?,?,?)",
                      (line, db.jdump(b), r.id, db.jdump(qids), kit_id, db.now()))
        return {"line": line, "drafts": reviewed, "queue_ids": qids, "brief": b, "missing": built["missing"], "sent": 0}

    if run is not None:
        res = fn(run)
        res["run_id"] = run.id
        return res
    out = runtime.execute(tenant, "studio", f"Studio: {LINES[line]}", ["Brief", "Draft", "Rubric + guardrail review",
                                                                        "Approval queue"], fn, actor)
    return {**out["result"], "run_id": out["run_id"]}


def launch_kit(tenant, partial_brief: dict | None = None, user: dict | None = None) -> dict:
    kit_id = f"kit-{secrets.token_hex(4)}"
    actor = f"{user['username']}({user['role']})" if user else "system"

    def fn(r: runtime.Run):
        out = {}
        for line in LINES:
            out[line] = run_line(tenant, line, partial_brief, user, kit_id=kit_id, run=r)
        qids = [q for v in out.values() for q in v["queue_ids"]]
        return {"kit_id": kit_id, "lines": {k: {"drafts": v["drafts"]} for k, v in out.items()}, "queue_ids": qids, "sent": 0}

    res = runtime.execute(tenant, "launch_kit", f"Launch kit {kit_id}", [f"Line: {v}" for v in LINES.values()], fn, actor)
    return {**res["result"], "run_id": res["run_id"]}
