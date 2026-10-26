"""Ads Ops: Diagnose (read-only, writes nothing), Propose changes (queues approvals only),
Build new test (PAUSED objects with zero budget on the practice account), a 4-step practice
go-live flow with approval and rollback, and "Reset practice account"."""
from __future__ import annotations

from .. import actions, db, retrieval, runtime
from ..adapters.ads import MockAdsAdapter, adapter_for

PRACTICE = "act_DEMO_practice"


def practice_account(tenant) -> str:
    ad = MockAdsAdapter(tenant)
    accts = ad.accounts()
    if accts:
        return accts[0]["id"]
    # real workspace: an empty practice account (no fictional metrics)
    with tenant.db() as c:
        c.execute("INSERT OR IGNORE INTO mock_ads(id,kind,parent,name,status,daily_budget,data,metrics,updated) "
                  "VALUES(?,?,?,?,?,?,?,?,?)", ("act_practice", "account", None, "Practice ad account", "ACTIVE", 0, "{}",
                                               "{}", db.now()))
    return "act_practice"


def diagnose(tenant, account_id: str | None = None, target_cpl: float = 10.0, user: dict | None = None) -> dict:
    """Read-only. Applies the Thunder Dome kill rule and winner rule to every ad."""
    actor = f"{user['username']}({user['role']})" if user else "system"
    account_id = account_id or practice_account(tenant)

    def fn(run: runtime.Run):
        ad = adapter_for(tenant, account_id)
        ads = run.step("read", {"account": account_id}, lambda: ad.tree(account_id), "Read account (read-only)")
        rows = []
        for a in ads:
            m = a["metrics"] or {}
            spend, leads = float(m.get("spend", 0)), float(m.get("leads", 0))
            imp, clicks = float(m.get("impressions", 0)), float(m.get("clicks", 0))
            cpl = spend / leads if leads else None
            ctr = clicks / imp if imp else 0.0
            kill = spend >= 1.5 * target_cpl and (leads == 0 or (cpl or 0) > 2 * target_cpl) and imp >= 1000
            rows.append({"id": a["id"], "name": a["name"], "status": a["status"], "campaign": a["campaign"]["name"],
                         "spend": spend, "leads": leads, "cpl": round(cpl, 2) if cpl else None, "ctr": round(ctr, 4),
                         "impressions": imp, "hours_live": m.get("hours_live"), "kill": kill})
        live = [r for r in rows if r["cpl"] is not None and not r["kill"] and r["ctr"] >= 0.01]
        winner = min(live, key=lambda r: r["cpl"]) if live else None
        hits = retrieval.search(tenant, "Thunder Dome kill rule two times target cost per lead winner click-through", k=2)
        cites = [retrieval.cite({**h, "id": f"S{i + 1}"}) for i, h in enumerate(hits)]
        run.note("rules", {"target_cpl": target_cpl}, {"kill": [r["id"] for r in rows if r["kill"]],
                                                       "winner": winner["id"] if winner else None}, label="Apply SOP rules")
        return {"account": account_id, "adapter": ad.name, "ads": rows, "winner": winner, "target_cpl": target_cpl,
                "citations": cites, "writes": 0}

    out = runtime.execute(tenant, "ads_ops", f"Diagnose {account_id}", ["Read account (read-only)", "Apply SOP rules"], fn, actor)
    return {**out["result"], "run_id": out["run_id"]}


def propose_changes(tenant, account_id: str | None = None, target_cpl: float = 10.0, user: dict | None = None) -> dict:
    """Queue one owner-only approval per suggested change. Writes nothing."""
    actor = f"{user['username']}({user['role']})" if user else "system"
    d = diagnose(tenant, account_id, target_cpl, user)
    proposals = []
    for r in d["ads"]:
        if r["kill"] and r["status"] == "ACTIVE":
            proposals.append(actions.propose(tenant, "ad_change", {
                "account": d["account"], "ops": [{"op": "pause", "id": r["id"]}],
                "why": f"Kill rule: spent {r['spend']:.0f} with CPL {r['cpl'] or 'no leads'} vs target {target_cpl}"},
                actor, title=f"Pause {r['name']}"))
    return {"account": d["account"], "proposals": proposals, "diagnosis_run": d["run_id"], "writes": 0}


def build_test(tenant, name: str, creatives: list[str], account_id: str | None = None, user: dict | None = None) -> dict:
    actor = f"{user['username']}({user['role']})" if user else "system"
    account_id = account_id or practice_account(tenant)
    proposal = {"account": account_id, "ops": [{"op": "create_test", "account": account_id, "name": name,
                                                "creatives": creatives[:5]}],
                "why": "New Thunder Dome test (PAUSED, zero budget)"}
    ad = adapter_for(tenant, account_id)
    if ad.name == "mock":
        return actions.run_direct(tenant, "ad_change", proposal, actor)
    return actions.propose(tenant, "ad_change", proposal, actor, title=f"Create PAUSED test {name}")


def propose_go_live(tenant, campaign_id: str, daily_budget: float, user: dict | None = None) -> dict:
    """Step 2 of the practice go-live flow: activation and budget need an owner approval."""
    actor = f"{user['username']}({user['role']})" if user else "system"
    ad = MockAdsAdapter(tenant)
    cmp = ad.get(campaign_id)
    if cmp is None:
        raise KeyError(campaign_id)
    ops = [{"op": "activate", "id": campaign_id, "daily_budget": 0.0}]
    for ad_set in ad.list("adset", campaign_id):
        ops.append({"op": "activate", "id": ad_set["id"], "daily_budget": daily_budget})
        for a in ad.list("ad", ad_set["id"]):
            ops.append({"op": "activate", "id": a["id"], "daily_budget": 0.0})
    return actions.propose(tenant, "ad_change", {"account": cmp["parent"], "ops": ops,
                                                 "why": f"Practice go-live at {daily_budget:.0f}/day"},
                           actor, title=f"Go live: {cmp['name']}")


def reset_practice(tenant, actor: str = "system") -> dict:
    from .. import audit

    with tenant.db() as c:
        c.execute("DELETE FROM mock_ads")
    if tenant.is_demo:
        import random
        import time

        from ..demo import _seed_ads

        with tenant.db() as c:
            _seed_ads(c, random.Random(42), time.time())
    else:
        practice_account(tenant)
    audit.log(tenant, actor, "action", {"reset": "practice ad account"})
    return {"ok": True}
