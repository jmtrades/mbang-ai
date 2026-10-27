"""Diagnostic engine: pure maths first. Stage conversion rates are compared with the SOP
benchmarks, the money cost of not fixing each stage is computed, and the one bottleneck
with the biggest realistic money impact is named. Then a cited prescription (rules, or AI
when the budget allows) and a "Next best action" that opens the Studio line for it.

The maths, in plain words:
* a stage is measured only when both of its numbers were given (a missing number is not a zero);
* the upside of fixing a stage is carried through the later stages at max(actual, benchmark), so a later
  stage at zero (for example no closes yet) never hides an earlier problem;
* a stage far below its benchmark (under a fifth of it) with enough volume is "critical" and ranks first;
* cost per lead is sized on lead -> close (the actual close rate per lead when known);
* NaN, Infinity, negative and absurdly large numbers are refused (the API answers 422)."""
from __future__ import annotations

import math
from dataclasses import dataclass

from . import llm, retrieval, runtime
from .errors import InputError


class BadMetrics(InputError):
    pass


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


MIN_VOLUME = 10          # people at the start of a stage before its rate is trusted
CRITICAL_SHARE = 0.2     # under a fifth of the benchmark: a "dead" stage, ranked first
MAX_VALUE = 1e12
DEFAULT_LEAD_TO_CLOSE = 0.02
METRIC_KEYS = {k for s in STAGES for k in (s.num, s.den)} | {"offer_price"}


def clean_metrics(metrics) -> dict[str, float]:
    """The numbers the maths uses: finite, >= 0 and sane. Anything else is refused (BadMetrics -> 422).
    Keys the diagnostic doesn't use are ignored; empty values count as "not given"."""
    if not isinstance(metrics, dict):
        raise BadMetrics("Metrics must be an object of numbers")
    out: dict[str, float] = {}
    for k, v in metrics.items():
        if k not in METRIC_KEYS or v is None or v == "":
            continue
        if isinstance(v, bool):
            raise BadMetrics(f"{k} must be a number")
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise BadMetrics(f"{k} must be a number")
        if not math.isfinite(f):
            raise BadMetrics(f"{k} must be a finite number")
        if f < 0:
            raise BadMetrics(f"{k} can't be negative")
        if f > MAX_VALUE:
            raise BadMetrics(f"{k} is too large")
        out[k] = f
    return out


def _rate(m: dict, st: Stage) -> float | None:
    """A stage's actual rate, or None when either number wasn't given (or the base is zero)."""
    if st.num in m and st.den in m and m[st.den] > 0:
        return m[st.num] / m[st.den]
    return None


def _lead_to_close(m: dict, stages: dict) -> float:
    """Share of leads that become a sale: closed / leads when both are known, else booked / leads through the
    show and close stages (each at max(actual, benchmark)), else a 2% default."""
    leads = m.get("leads") or 0
    if leads > 0 and m.get("closed", 0) > 0:
        return min(1.0, m["closed"] / leads)
    if leads > 0 and m.get("booked", 0) > 0:
        r = min(1.0, m["booked"] / leads)
        for k in ("show", "close"):
            actual = _rate(m, stages[k])
            r *= max(actual if actual is not None else 0.0, stages[k].benchmark)
        return r
    return DEFAULT_LEAD_TO_CLOSE


def analyse(metrics: dict) -> dict:
    """Pure maths. Returns per-stage rates, gaps, money cost of not fixing and the bottleneck."""
    m = clean_metrics(metrics)
    price = m.get("offer_price") or 3000.0
    stages = {s.key: s for s in STAGES}
    rows = []
    for s in STAGES:
        if s.kind == "cost":
            n, d = m.get(s.num, 0.0), m.get(s.den, 0.0)
            if d <= 0 or n <= 0:
                continue
            value = n / d
            below = value > s.benchmark
            # money: the extra leads the same spend would buy at the benchmark CPL, times lead -> close
            extra_leads = n / s.benchmark - d if below else 0.0
            cost = extra_leads * _lead_to_close(m, stages) * price
            rows.append({"key": s.key, "label": s.label, "value": round(value, 2), "benchmark": s.benchmark,
                         "below": below, "gap": round(value - s.benchmark, 2), "monthly_cost": round(cost),
                         "volume": d, "kind": "cost", "realistic": d >= MIN_VOLUME, "critical": False})
            continue
        rate = _rate(m, s)
        if rate is None:
            continue
        d = m[s.den]
        below = rate < s.benchmark
        chain = SALES_CHAIN if s.key in SALES_CHAIN else WEBINAR_CHAIN
        down_rate = 1.0
        for k in chain[chain.index(s.key) + 1:]:
            actual = _rate(m, stages[k])
            # sizing the upside: a later stage counts at least at its benchmark, so a zero there (no closes
            # yet) can't zero out the cost of an earlier, fixable problem
            down_rate *= max(min(actual, 1.0) if actual is not None else 0.0, stages[k].benchmark)
        extra = (s.benchmark - rate) * d * down_rate if below else 0.0
        enough = d >= MIN_VOLUME
        rows.append({"key": s.key, "label": s.label, "value": round(rate, 4), "benchmark": s.benchmark, "below": below,
                     "gap": round(s.benchmark - rate, 4), "monthly_cost": round(extra * price), "volume": d,
                     "kind": "rate", "realistic": enough, "critical": bool(below and enough and rate < s.benchmark * CRITICAL_SHARE)})
    candidates = [r for r in rows if r["below"] and r["realistic"] and (r["monthly_cost"] > 0 or r["critical"])]
    # a critical (far below benchmark) stage comes first; then the biggest money impact
    bottleneck = max(candidates, key=lambda r: (r["critical"], r["monthly_cost"])) if candidates else None
    below_low_volume = [r for r in rows if r["below"] and not r["realistic"]]
    status = ("bottleneck" if bottleneck else "low_volume" if below_low_volume else "at_benchmark" if rows else "no_data")
    return {"stages": rows, "bottleneck": bottleneck, "price": price, "status": status,
            "below_low_volume": [r["label"] for r in below_low_volume],
            "assumption": None if m.get("offer_price")
            else "Offer price not given; assumed 3,000 for the money impact (DEMO assumption)."}


def no_bottleneck_text(res: dict) -> str:
    """What to say when no bottleneck can be named. "All at benchmark" only when nothing is below it."""
    if res.get("status") == "low_volume":
        return (f"Below the SOP benchmark, but with too little volume to call it yet: "
                f"{', '.join(res['below_low_volume'])}. A stage needs at least {MIN_VOLUME} people at its start "
                f"before its rate is trusted. Keep volume up and diagnose again.")
    if res.get("status") == "no_data":
        return ("Not enough numbers to diagnose. Enter both numbers of at least one stage, for example DMs sent "
                "and replies.")
    return "Every stage with enough data is at or above the SOP benchmark. Keep volume up."


PLAN = ["Compute stage conversion rates", "Compare with SOP benchmarks", "Cost of not fixing each stage",
        "Name the bottleneck", "Cited prescription", "Next best action"]


def diagnose(tenant, metrics: dict, user: dict | None = None, label: str = "") -> dict:
    actor = f"{user['username']}({user['role']})" if user else "system"
    metrics = clean_metrics(metrics)  # refuse bad numbers before a run starts

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
            out = {**res, "prescription": no_bottleneck_text(res),
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
        far = " That is far below the benchmark, so fix it first." if bn.get("critical") else ""
        text = (f"Your bottleneck is {stage.label.lower()}: {val} against the SOP benchmark of {target} [S1].{far} "
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
