"""Diagnostic engine: pure maths first. Stage conversion rates are compared with the SOP
benchmarks, the money cost of not fixing each stage is computed, and the one bottleneck
with the biggest realistic money impact is named. Then a cited prescription (rules, or AI
when the budget allows) and a "Next best action" that opens the Studio line for it."""
from __future__ import annotations

from dataclasses import dataclass

from . import llm, retrieval, runtime


@dataclass
class Stage:
    key: str
    label: str
    num: str
    den: str
    benchmark: float  # conversion rate (0-1) or cost ceiling for cost stages
    cite: str          # phrase to find the benchmark line in the SOP
    studio_line: str
    fix: str
    kind: str = "rate"  # rate | cost


STAGES = [
    Stage("reply", "DM reply rate", "replies", "dms_sent", 0.10, "DM reply rate should be at least 10 percent", "outreach",
          "Test new opener moves (one question per message) and log reply rate by move."),
    Stage("conversation", "Reply → quality conversation", "conversations", "replies", 0.40,
          "40 percent of replies should turn into a quality conversation", "outreach",
          "Ask about their goal and current numbers before anything else."),
    Stage("link", "Conversation → link sent", "links_sent", "conversations", 0.50,
          "50 percent of quality conversations should get the booking link", "outreach",
          "Offer the call once you understand their goal; use the course line for warm leads."),
    Stage("booked", "Link → call booked", "booked", "links_sent", 0.30, "30 percent of people who get the link should book",
          "outreach", "Qualify before sending the link and offer two specific times."),
    Stage("show", "Show rate", "showed", "booked", 0.60, "show rate should be at least 60 percent of booked calls",
          "outreach", "Confirm within five minutes and send the 24h / 2h / 10 min reminder ladder with value."),
    Stage("close", "Close rate", "closed", "showed", 0.20, "close rate should be at least 20 percent of calls that showed",
          "training", "Run objection drills; read the pre-call brief before every call; never discount."),
    Stage("wreg", "Webinar registration rate", "registrants", "visitors", 0.25,
          "webinar registration rate should be at least 25 percent", "funnels",
          "Sharpen the registration headline around one measurable outcome."),
    Stage("wshow", "Webinar show rate", "attendees", "registrants", 0.35, "webinar show rate should be at least 35 percent",
          "webinars", "Send reminders one day, one hour and at go-live; ask registrants to reply to confirm."),
    Stage("wclose", "Webinar close rate", "webinar_sales", "attendees", 0.05, "webinar close rate should be at least 5 percent",
          "webinars", "Tighten the big-mistake and new-mechanism segments; ask permission before the offer."),
    Stage("cpl", "Cost per lead", "ad_spend", "leads", 15.0, "cost per lead should stay under 15 dollars", "creatives",
          "Run a Thunder Dome: 3-5 creatives, equal budgets, 72 hours, kill rule at 2x target.", "cost"),
]
SALES_CHAIN = ["reply", "conversation", "link", "booked", "show", "close"]
WEBINAR_CHAIN = ["wreg", "wshow", "wclose"]
SOP = "sops/show_rate_sales_guide.txt"


def _num(m: dict, k: str) -> float:
    try:
        return float(m.get(k) or 0)
    except (TypeError, ValueError):
        return 0.0


def analyse(metrics: dict) -> dict:
    """Pure maths. Returns per-stage rates, gaps, money cost of not fixing and the bottleneck."""
    price = _num(metrics, "offer_price") or 3000.0
    stages = {s.key: s for s in STAGES}
    rows = []
    for s in STAGES:
        n, d = _num(metrics, s.num), _num(metrics, s.den)
        if s.kind == "cost":
            if d <= 0 or n <= 0:
                continue
            value = n / d
            below = value > s.benchmark
            # money: what the same spend would buy at benchmark CPL, carried through the sales chain
            extra_leads = n / s.benchmark - d if below else 0.0
            close_through = _chain_rate(metrics, SALES_CHAIN, stages) or 0.02
            cost = extra_leads * close_through * price
            rows.append({"key": s.key, "label": s.label, "value": round(value, 2), "benchmark": s.benchmark,
                         "below": below, "gap": round(value - s.benchmark, 2), "monthly_cost": round(cost),
                         "volume": d, "kind": "cost", "realistic": d >= 10})
            continue
        if d <= 0:
            continue
        rate = n / d
        below = rate < s.benchmark
        chain = SALES_CHAIN if s.key in SALES_CHAIN else WEBINAR_CHAIN
        downstream = chain[chain.index(s.key) + 1:]
        down_rate = 1.0
        for k in downstream:
            st = stages[k]
            dd = _num(metrics, st.den)
            down_rate *= (_num(metrics, st.num) / dd) if dd else st.benchmark
        extra = (s.benchmark - rate) * d * down_rate if below else 0.0
        cost = extra * price
        # realistic to fix: enough volume to trust the number and not a dead stage
        realistic = d >= 10 and rate >= s.benchmark * 0.2
        rows.append({"key": s.key, "label": s.label, "value": round(rate, 4), "benchmark": s.benchmark, "below": below,
                     "gap": round(s.benchmark - rate, 4), "monthly_cost": round(cost), "volume": d, "kind": "rate",
                     "realistic": realistic})
    candidates = [r for r in rows if r["below"] and r["realistic"] and r["monthly_cost"] > 0]
    bottleneck = max(candidates, key=lambda r: r["monthly_cost"]) if candidates else None
    return {"stages": rows, "bottleneck": bottleneck, "price": price, "assumption": None if _num(metrics, "offer_price")
            else "Offer price not given; assumed 3,000 for the money impact (DEMO assumption)."}


def _chain_rate(metrics: dict, chain: list[str], stages: dict) -> float:
    r = 1.0
    any_data = False
    for k in chain:
        st = stages[k]
        d = _num(metrics, st.den)
        if d:
            r *= _num(metrics, st.num) / d
            any_data = True
        else:
            r *= st.benchmark
    return r if any_data else 0.0


PLAN = ["Compute stage conversion rates", "Compare with SOP benchmarks", "Cost of not fixing each stage",
        "Name the bottleneck", "Cited prescription", "Next best action"]


def diagnose(tenant, metrics: dict, user: dict | None = None, label: str = "") -> dict:
    actor = f"{user['username']}({user['role']})" if user else "system"

    def fn(run: runtime.Run):
        res = run.step("rates", {"metrics": metrics}, lambda: analyse(metrics), "Compute stage conversion rates")
        bench = run.step("benchmarks", {}, lambda: _benchmarks(tenant), "Compare with SOP benchmarks")
        for r in res["stages"]:
            r["citation"] = bench.get(r["key"])
        run.note("cost", {}, [{"stage": r["key"], "monthly_cost": r["monthly_cost"]} for r in res["stages"]],
                 label="Cost of not fixing each stage")
        bn = res["bottleneck"]
        run.note("bottleneck", {}, bn, label="Name the bottleneck")
        if not bn:
            out = {**res, "prescription": "Every stage with enough data is at or above the SOP benchmark. Keep volume up.",
                   "citations": [], "next_best_action": None, "mode": "rules", "label": label}
            run.note("prescription", {}, out["prescription"], label="Cited prescription")
            return out
        stage = next(s for s in STAGES if s.key == bn["key"])
        hits = retrieval.search(tenant, stage.fix + " " + stage.label, k=3)
        cites = [retrieval.cite({**h, "id": f"S{i + 1}"}) for i, h in enumerate(hits)]
        if bn.get("citation"):
            cites = [bn["citation"]] + [c for c in cites if (c["file"], c["lines"]) != (bn["citation"]["file"], bn["citation"]["lines"])]
            for i, c in enumerate(cites):
                c["id"] = f"S{i + 1}"
        val = f"{bn['value']:.0%}" if bn["kind"] == "rate" else f"${bn['value']:.2f}"
        target = f"{bn['benchmark']:.0%}" if bn["kind"] == "rate" else f"${bn['benchmark']:.2f}"
        text = (f"Your bottleneck is {stage.label.lower()}: {val} against the SOP benchmark of {target} [S1]. "
                f"Fixing it is worth about {bn['monthly_cost']:,} a month at your current numbers. {stage.fix} "
                f"[S{min(2, len(cites))}]")
        mode = "rules"
        if llm.unavailable_reason(tenant) is None:
            try:
                ai = llm.complete(tenant, "Write a 3-sentence prescription using only the passages. Cite [S#].",
                                  f"Bottleneck: {stage.label} {val} vs {target}.\n" + "\n".join(
                                      f"[{c['id']}] {c['snippet']}" for c in cites), purpose="diagnose", run_id=run.id)
                text = ai["text"].strip()
                mode = "live"
                run.use_ai("live")
            except llm.AIUnavailable:
                pass
        run.note("prescription", {"stage": stage.key}, text, label="Cited prescription")
        nba = {"line": stage.studio_line, "stage": stage.key,
               "brief": f"Fix {stage.label.lower()} ({val} vs {target}). {stage.fix}",
               "href": f"#/studio?line={stage.studio_line}&brief=" + f"Fix {stage.label.lower()}"}
        run.note("next_best_action", {}, nba, label="Next best action")
        return {**res, "prescription": text, "citations": cites, "next_best_action": nba, "mode": mode, "label": label}

    out = runtime.execute(tenant, "diagnostic", f"Diagnose {label}".strip(), PLAN, fn, actor)
    return {**out["result"], "run_id": out["run_id"]}


def _benchmarks(tenant) -> dict:
    """The SOP line behind each benchmark, so every threshold is cited."""
    out = {}
    for s in STAGES:
        hits = retrieval.search(tenant, s.cite, k=3)
        hit = next((h for h in hits if h["file"] == SOP), hits[0] if hits else None)
        if hit:
            # narrow the citation to the exact benchmark line when we can find it
            lines = _find_line(tenant, hit, s.cite)
            out[s.key] = {**retrieval.cite({**hit, "id": "S1"}), **({"lines": lines} if lines else {})}
    return out


def _find_line(tenant, hit: dict, phrase: str) -> str | None:
    from . import index, textnorm

    doc = index.for_tenant(tenant).docs.get(hit["file"])
    if not doc:
        return None
    want = set(textnorm.tokens(phrase))
    best, best_n = None, 0
    for i, ln in enumerate(doc.lines, start=1):
        n = len(want & set(textnorm.tokens(ln)))
        if n > best_n:
            best, best_n = i, n
    return f"{best}-{best}" if best else None
