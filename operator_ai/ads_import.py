"""Audit a real ad account from an Ads Manager export (CSV) — no login needed.

Reads the columns Meta Ads Manager exports (names vary by locale/report; matched loosely):
ad name, amount spent, results / leads, impressions, link clicks / clicks, CTR. Applies the
Thunder Dome kill rule and winner rule from the SOP, with citations. Read-only: nothing is
written to any ad account."""
from __future__ import annotations

import csv
import io
import re

from . import retrieval

COLS = {
    "name": [r"^ad name$", r"^ad$", r"ad name", r"^campaign name$", r"^name$"],
    "spend": [r"amount spent", r"^spend", r"cost"],
    "leads": [r"^leads?$", r"^results$", r"leads? \(", r"on-facebook leads", r"website leads", r"conversions?$"],
    "impressions": [r"^impressions$", r"impressions"],
    "clicks": [r"link clicks", r"^clicks", r"clicks \(all\)", r"outbound clicks"],
    "status": [r"delivery", r"status"],
}


class ImportError_(ValueError):
    pass


def _num(v: str) -> float:
    s = re.sub(r"[^\d.,-]", "", str(v or ""))
    if not s:
        return 0.0
    if s.count(",") and s.count("."):
        s = s.replace(",", "")
    elif s.count(",") == 1 and len(s.split(",")[1]) != 3:
        s = s.replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def looks_like_ads_export(text: str) -> bool:
    head = (text.splitlines() or [""])[0].lower()
    return ("," in head or "\t" in head) and ("impression" in head) and ("spent" in head or "spend" in head or "cost" in head)


def parse(text: str) -> list[dict]:
    dialect = "excel-tab" if "\t" in (text.splitlines() or [""])[0] else "excel"
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿")), dialect=dialect))
    if len(rows) < 2:
        raise ImportError_("The file has no data rows")
    header = [h.strip().lower() for h in rows[0]]
    idx: dict[str, int] = {}
    for key, pats in COLS.items():
        for p in pats:
            hit = next((i for i, h in enumerate(header) if re.search(p, h) and i not in idx.values()), None)
            if hit is not None:
                idx[key] = hit
                break
    if "spend" not in idx or "impressions" not in idx:
        raise ImportError_("Couldn't find the spend and impressions columns. Export from Ads Manager with "
                           "'Amount spent', 'Results' (or 'Leads'), 'Impressions' and 'Link clicks'.")
    out = []
    for r in rows[1:]:
        if not any(c.strip() for c in r):
            continue
        get = lambda k: r[idx[k]] if k in idx and idx[k] < len(r) else ""
        name = get("name") or f"Row {len(out) + 1}"
        if name.lower().startswith(("total", "results from")):
            continue
        out.append({"name": name.strip(), "spend": _num(get("spend")), "leads": _num(get("leads")),
                    "impressions": _num(get("impressions")), "clicks": _num(get("clicks")), "status": get("status").strip()})
    if not out:
        raise ImportError_("No ad rows found")
    return out


def audit(tenant, text: str, target_cpl: float = 10.0) -> dict:
    ads = parse(text)
    rows = []
    for a in ads:
        cpl = a["spend"] / a["leads"] if a["leads"] else None
        ctr = a["clicks"] / a["impressions"] if a["impressions"] else 0.0
        enough = a["impressions"] >= 1000
        spent_enough = a["spend"] >= 1.5 * target_cpl
        kill = enough and spent_enough and (a["leads"] == 0 or (cpl or 0) > 2 * target_cpl)
        verdict = ("kill" if kill else "too early (under 1,000 impressions)" if not enough
                   else "keep" if cpl is not None else "watch")
        rows.append({**a, "cpl": round(cpl, 2) if cpl else None, "ctr": round(ctr, 4), "kill": kill, "verdict": verdict})
    live = [r for r in rows if r["cpl"] is not None and not r["kill"] and r["ctr"] >= 0.01 and r["impressions"] >= 1000]
    winner = min(live, key=lambda r: r["cpl"]) if live else None
    spend = sum(r["spend"] for r in rows)
    leads = sum(r["leads"] for r in rows)
    wasted = sum(r["spend"] for r in rows if r["kill"])
    hits = retrieval.search(tenant, "Thunder Dome kill rule two times target cost per lead winner click-through rate", k=2)
    cites = [retrieval.cite({**h, "id": f"S{i + 1}"}) for i, h in enumerate(hits)]
    summary = (f"{len(rows)} ads, {spend:,.2f} spent, {int(leads)} leads"
               + (f", blended cost per lead {spend / leads:,.2f}" if leads else "")
               + f". {sum(r['kill'] for r in rows)} ad(s) hit the kill rule ({wasted:,.2f} of spend)"
               + (f"; winner: {winner['name']} at {winner['cpl']:,.2f} per lead" if winner else "; no clear winner yet")
               + f". Target cost per lead used: {target_cpl:,.2f}.")
    return {"ads": rows, "winner": winner, "summary": summary, "spend": round(spend, 2), "leads": leads,
            "wasted": round(wasted, 2), "target_cpl": target_cpl, "citations": cites, "writes": 0}
