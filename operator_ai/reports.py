"""Reporting: Day One outcome counts, Command Centre KPIs, the Morning brief (today's few
decisions ranked by rules; read-only) and the owner digest."""
from __future__ import annotations

import time

from . import db, evals, llm, queue, runtime


def day_one(tenant) -> dict:
    """Six outcome cards with live counts."""
    with tenant.db() as c:
        q = lambda sql, *a: c.execute(sql, a).fetchone()[0]
        week = time.time() - 7 * 86400
        cards = [
            {"key": "answers", "title": "Questions answered from your material", "count": q("SELECT COUNT(*) FROM queries WHERE covered!='no'"),
             "href": "#/coach"},
            {"key": "drafts", "title": "Drafts waiting for your approval", "count": q("SELECT COUNT(*) FROM queue WHERE status IN ('pending','held','needs_human')"),
             "href": "#/queue"},
            {"key": "leads", "title": "Leads qualified", "count": q("SELECT COUNT(*) FROM leads WHERE tier IS NOT NULL") + q("SELECT COUNT(*) FROM chain_leads"),
             "href": "#/sales"},
            {"key": "students", "title": "Students checked in this week", "count": q("SELECT COUNT(DISTINCT student_id) FROM checkins WHERE at>?", week),
             "href": "#/students"},
            {"key": "runs", "title": "Agent runs (traced)", "count": q("SELECT COUNT(*) FROM runs"), "href": "#/runs"},
            {"key": "blocked", "title": "Risky drafts stopped by guardrails", "count": q("SELECT COUNT(*) FROM audit WHERE kind='guardrail.block'"),
             "href": "#/audit"},
        ]
    return {"cards": cards, "is_demo": tenant.is_demo}


def command_centre(tenant) -> dict:
    from .agents import accountability

    t0 = time.time()
    with tenant.db() as c:
        leads = {r["tier"] or "unscored": r["n"] for r in c.execute("SELECT tier, COUNT(*) n FROM leads WHERE batch IS NULL GROUP BY tier")}
        runs_today = c.execute("SELECT COUNT(*) FROM runs WHERE created>?", (time.time() - 86400,)).fetchone()[0]
        agents = {r["agent"]: r["n"] for r in c.execute("SELECT agent, COUNT(*) n FROM runs WHERE created>? GROUP BY agent",
                                                        (time.time() - 7 * 86400,))}
        students = c.execute("SELECT COUNT(*) FROM students").fetchone()[0]
    board = accountability.board(tenant)
    latest = {}
    for r in evals.history(tenant, 50):
        latest.setdefault(r["kind"], r["metrics"])
    return {
        "kpis": {"students": students, "at_risk": board["counts"].get("high", 0) + board["counts"].get("medium", 0),
                 "leads": leads, "queue": queue.counts(tenant), "runs_today": runs_today},
        "agents_7d": agents, "at_risk": board["students"][:8], "recent_runs": runtime.recent(tenant, 12),
        "evals": {k: {kk: vv for kk, vv in v.items() if kk not in ("misses", "detail", "false_answers")} for k, v in latest.items()},
        "ai": llm.state(tenant), "is_demo": tenant.is_demo, "ms": int((time.time() - t0) * 1000),
    }


def morning_brief(tenant) -> dict:
    """Today's few decisions, ranked by rules. Read-only: computing it changes nothing."""
    from . import console, diagnostic
    from .agents import accountability

    items = []
    qc = queue.counts(tenant)
    if qc.get("held"):
        items.append({"score": 100, "title": f"Fix {qc['held']} held draft(s)", "why": "Guardrails stopped them; they can't go out until edited.",
                      "href": "#/queue?status=held"})
    if qc.get("needs_human"):
        items.append({"score": 80, "title": f"Check {qc['needs_human']} draft(s) that need a person",
                      "why": "A deadline without a reason, a failed critique or an escalated ticket.", "href": "#/queue?status=needs_human"})
    if qc.get("pending"):
        items.append({"score": 60 + min(20, qc["pending"]), "title": f"Approve or reject {qc['pending']} pending draft(s)",
                      "why": "Nothing is sent until you decide.", "href": "#/queue?status=pending"})
    b = accountability.board(tenant)
    high = [s for s in b["students"] if s["level"] == "high"]
    if high:
        items.append({"score": 70 + min(25, len(high)), "title": f"{len(high)} student(s) at high risk",
                      "why": "; ".join(f"{s['name']}: {s['signals'][0]['label']}" for s in high[:3]), "href": "#/students"})
    with tenant.db() as c:
        rows = db.rows(c.execute("SELECT name, metrics, price FROM clients"))
    worst = None
    for r in rows:
        a = diagnostic.analyse({**db.jload(r["metrics"], {}), "offer_price": r["price"]})
        if a["bottleneck"] and (worst is None or a["bottleneck"]["monthly_cost"] > worst[1]["monthly_cost"]):
            worst = (r["name"], a["bottleneck"])
    if worst:
        items.append({"score": 65, "title": f"Funnel bottleneck: {worst[0]} — {worst[1]['label']}",
                      "why": f"≈ {worst[1]['monthly_cost']:,} a month at current numbers.", "href": "#/diagnostic"})
    missing = [i for i in console.integrations(tenant) if i["status"] == "NEEDS_LOGIN" and i["key"] in ("meta", "google_calendar", "instagram")]
    if missing:
        items.append({"score": 30, "title": f"{len(missing)} login(s) missing for going live",
                      "why": ", ".join(i["name"] for i in missing), "href": "#/integrations"})
    items.sort(key=lambda x: -x["score"])
    for i, it in enumerate(items, start=1):
        it["rank"] = i
    date = time.strftime("%A %d %B")
    text = f"Morning brief — {date}\n" + "\n".join(f"{it['rank']}. {it['title']} — {it['why']}" for it in items[:6])
    return {"date": date, "items": items[:6], "text": text, "read_only": True}
