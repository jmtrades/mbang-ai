"""Client fulfilment: money model (3 scenarios, labelled assumptions), bottleneck from the
client's metrics against SOP thresholds, a cited launch plan, ad hooks (reviewed; regenerated
once if they score under 60), a VSL outline and a client report. Everything queued and
guardrail-checked."""
from __future__ import annotations

from .. import db, diagnostic, guardrails, queue, retrieval, rubrics, runtime

HOOK_TEMPLATES = [
    "Your front desk is losing {niche_short} patients every week.",
    "Stop discounting to fill your diary.",
    "{outcome} without {pain}.",
    "The one number every {niche_short} owner should check on Monday.",
    "Why {niche} struggle to {goal_verb} (and the fix).",
]
HOOK_RETRY = [
    "{outcome} in {timeframe}.",
    "Three questions to ask before you spend another dollar on ads.",
    "What {niche_short} owners get wrong about referrals.",
    "A simple way to fill {niche_short} diaries.",
    "The {mechanism} in 60 seconds.",
]


def money_model(price: float, metrics: dict) -> dict:
    leads = float(metrics.get("leads") or 100)
    showed = float(metrics.get("showed") or 0)
    closed = float(metrics.get("closed") or 0)
    base_close = (closed / showed) if showed else 0.2
    booked_per_lead = (float(metrics.get("booked") or 0) / leads) if leads else 0.1
    scen = []
    for name, lm, cm, note in [("Conservative", 0.8, 0.8, "20% fewer leads and a lower close rate"),
                               ("Base", 1.0, 1.0, "current lead volume and close rate"),
                               ("Stretch", 1.3, 1.15, "30% more leads after the bottleneck fix")]:
        l = leads * lm
        calls = l * (booked_per_lead or 0.1) * 0.6
        sales = calls * min(0.5, base_close * cm)
        scen.append({"name": name, "leads": round(l), "calls": round(calls, 1), "sales": round(sales, 1),
                     "revenue": round(sales * price), "assumptions": note})
    return {"price": price, "scenarios": scen,
            "labels": ["Assumption: 60% of booked calls show (SOP floor).", "Assumption: close rate from your numbers, capped at 50%.",
                       "Every figure is a model, not a promise (DEMO numbers in the demo workspace)."]}


def _hooks(client: dict, templates: list[str]) -> list[str]:
    niche = client.get("niche") or "clinics"
    short = niche.split()[0].rstrip("s")
    ctx = {"niche": niche, "niche_short": short, "outcome": "20 new bookings a month", "pain": "discounting",
           "goal_verb": "fill their diary", "timeframe": "90 days", "mechanism": "referral-first system"}
    return [t.format(**ctx) for t in templates]


def review_hooks(tenant, hooks: list[str]) -> list[dict]:
    names = guardrails.tenant_banned_names(tenant)
    return [{"hook": h, **{k: v for k, v in rubrics.score(h + " Book a call.", "ad", {"client_asset": True}, names).items()
                         if k in ("score", "detected")}} for h in hooks]


PLAN = ["Load client", "Money model (3 scenarios)", "Bottleneck vs SOP", "Launch plan (cited)", "Ad hooks (reviewed)",
        "VSL outline", "Client report", "Queue everything"]


def fulfil(tenant, client_id: int, user: dict | None = None) -> dict:
    actor = f"{user['username']}({user['role']})" if user else "system"
    with tenant.db() as c:
        cl = db.row(c.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone())
    if cl is None:
        raise KeyError(client_id)
    cl["metrics"] = db.jload(cl["metrics"], {})
    demo = bool(cl["demo"])

    def fn(run: runtime.Run):
        run.note("client", {"id": client_id}, {"name": cl["name"], "niche": cl["niche"]}, label="Load client")
        mm = run.step("money_model", {"price": cl["price"]}, lambda: money_model(float(cl["price"] or 3000), cl["metrics"]),
                      "Money model (3 scenarios)")
        an = run.step("bottleneck", {}, lambda: diagnostic.analyse({**cl["metrics"], "offer_price": cl["price"]}),
                      "Bottleneck vs SOP")

        def _plan():
            steps = [("Week 1", "Run a Thunder Dome creative test (3-5 creatives, equal budgets, 72 hours)", "Thunder Dome rules creatives"),
                     ("Week 2", "Turn on the 24h / 2h / 10 min reminder ladder", "reminders twenty-four hours two hours ten minutes"),
                     ("Week 3", "Outline the seven-segment webinar", "seven segment webinar"),
                     ("Week 4", "Launch and review the weekly funnel numbers", "track every stage bottleneck fix that stage first")]
            out = []
            for i, (wk, what, q) in enumerate(steps):
                h = retrieval.search(tenant, q, k=1)
                out.append({"week": wk, "step": what, "citation": retrieval.cite({**h[0], "id": f"S{i + 1}"}) if h else None})
            return out

        plan = run.step("launch_plan", {}, _plan, "Launch plan (cited)")

        def _hooks_step():
            reviewed = review_hooks(tenant, _hooks(cl, HOOK_TEMPLATES))
            regenerated = False
            if any(h["score"] < 60 for h in reviewed):
                regenerated = True
                retry = review_hooks(tenant, _hooks(cl, HOOK_RETRY))
                reviewed = [r2 if r1["score"] < 60 and r2["score"] > r1["score"] else r1 for r1, r2 in zip(reviewed, retry)]
            return {"hooks": reviewed, "regenerated_once": regenerated}

        hooks = run.step("ad_hooks", {}, _hooks_step, "Ad hooks (reviewed)")
        vsl = run.step("vsl", {}, lambda: [
            "Hook: name the outcome in one line", "The big mistake most " + (cl["niche"] or "owners") + " make",
            "The new mechanism: referral-first system", "Proof: only verified results with permission",
            "The offer: what's included, who it's for and not for", f"Price: {cl['price']:,.0f} (client asset)",
            "One call to action"], "VSL outline")
        bn = an["bottleneck"]
        report = (f"Client report: {cl['name']}\nBottleneck: {bn['label'] if bn else 'none'}"
                  + (f" ({bn['value']:.0%} vs {bn['benchmark']:.0%})" if bn and bn['kind'] == 'rate' else "")
                  + "\nModel (base): " + f"{mm['scenarios'][1]['sales']} sales, revenue {mm['scenarios'][1]['revenue']:,} (model, not a promise)"
                  + "\nNext 4 weeks: " + "; ".join(p["step"] for p in plan))

        def _queue():
            meta = {"client_asset": True, "client_id": client_id}
            ids = [
                queue.add(tenant, "client_report", report, channel="email", title=f"Client report: {cl['name']}", meta=meta,
                          run_id=run.id, actor=actor, demo=demo)["id"],
                queue.add(tenant, "ad_hooks", "\n".join(h["hook"] for h in hooks["hooks"]), channel="manual",
                          title=f"Ad hooks: {cl['name']}", meta=meta, run_id=run.id, actor=actor, demo=demo)["id"],
                queue.add(tenant, "vsl_outline", "\n".join(vsl), channel="manual", title=f"VSL outline: {cl['name']}", meta=meta,
                          run_id=run.id, actor=actor, demo=demo)["id"],
                queue.add(tenant, "launch_plan", "\n".join(f"{p['week']}: {p['step']}" for p in plan), channel="manual",
                          title=f"Launch plan: {cl['name']}", meta=meta, run_id=run.id, actor=actor, demo=demo)["id"],
            ]
            return ids

        qids = run.step("queue", {}, _queue, "Queue everything")
        run.note("report", {}, report, label="Client report")
        return {"client": {"id": client_id, "name": cl["name"]}, "money_model": mm, "analysis": an, "launch_plan": plan,
                "hooks": hooks, "vsl_outline": vsl, "report": report, "queue_ids": qids}

    out = runtime.execute(tenant, "fulfilment", f"Fulfil {cl['name']}", PLAN, fn, actor)
    return {**out["result"], "run_id": out["run_id"]}
