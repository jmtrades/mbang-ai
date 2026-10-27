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


_EU_VALUE = re.compile(r"^-?\d{1,3}(\.\d{3})+(,\d+)?$|^-?\d+,\d{1,2}$")   # 1.234,56  1.234.567  12,5
_US_VALUE = re.compile(r"^-?\d{1,3}(,\d{3})+(\.\d+)?$|^-?\d+\.\d{1,2}$")   # 1,234.56  1,234,567  12.5


def _clean(v) -> str:
    return re.sub(r"[^\d.,-]", "", str(v or "").replace("\u00a0", "").replace("\u202f", "").replace(" ", ""))


def detect_eu(values: list[str]) -> bool | None:
    """True when the numbers use a comma as the decimal separator (most of Europe), False for a dot, None when
    nothing in the file says. Decided per file from the unambiguous values: "1.234,56" or "12,5" says EU,
    "1,234.56" or "12.5" says US."""
    eu = us = 0
    for v in values:
        s = _clean(v)
        if "," in s and "." in s:
            if s.rfind(",") > s.rfind("."):
                eu += 1
            else:
                us += 1
        elif _EU_VALUE.match(s) and not _US_VALUE.match(s):
            eu += 1
        elif _US_VALUE.match(s) and not _EU_VALUE.match(s):
            us += 1
    if eu == us:
        return None
    return eu > us


def _num(v, eu: bool | None = None, integer: bool = False) -> float:
    """A number from an export cell, whatever the locale: "1,234.56", "1.234,56", "€ 2.345,00", "1 234".
    eu=None decides from the value alone; integer=True (impressions, clicks, leads) reads "1.234" as 1234."""
    s = _clean(v)
    if not s or not re.search(r"\d", s):
        return 0.0
    if "," in s and "." in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
    elif "," in s:
        parts = s.split(",")
        if eu is True:
            dec = "," if len(parts) == 2 and not (integer and len(parts[1]) == 3) else None
        else:
            dec = "," if len(parts) == 2 and len(parts[1]) != 3 else None
    elif "." in s:
        parts = s.split(".")
        if len(parts) > 2:
            dec = None  # 1.234.567: thousands
        elif len(parts[1]) == 3 and (eu or integer):
            dec = None  # 1.234 in an EU file, or a count column: a thousands separator
        else:
            dec = "."
    else:
        dec = None
    thousands = {",": ".", ".": ",", None: ",."}[dec]
    for ch in thousands:
        s = s.replace(ch, "")
    if dec == ",":
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _delimiter(head: str) -> str:
    counts = {d: head.count(d) for d in ("\t", ";", ",")}
    return max(counts, key=lambda d: (counts[d], d == ","))


def looks_like_ads_export(text: str) -> bool:
    """An export, not a chat message that happens to mention impressions: at least a header and one row, and
    a header with at least three delimited cells including impressions and spend/cost."""
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    if len(lines) < 2:
        return False
    head = lines[0].lower()
    cells = [c.strip() for c in head.split(_delimiter(head))]
    if len([c for c in cells if c]) < 3:
        return False
    return any("impression" in c for c in cells) and any(("spent" in c or "spend" in c or "cost" in c) for c in cells)


# a summary row, not an ad: "Total", "Totals", "Total results", "Results from 4 ads" (an ad merely *named*
# "Total Transformation Hook" is an ad)
_TOTAL_ROW = re.compile(r"^(totals?|total results|grand total|results from \d+ (ads?|ad sets?|campaigns?))$", re.I)


def parse(text: str) -> list[dict]:
    text = (text or "").lstrip("\ufeff")
    first = (text.splitlines() or [""])[0]
    rows = list(csv.reader(io.StringIO(text), delimiter=_delimiter(first)))
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
    body = [r for r in rows[1:] if any(c.strip() for c in r)]
    eu = detect_eu([r[idx[k]] for r in body for k in ("spend", "impressions", "leads", "clicks")
                    if k in idx and idx[k] < len(r)])
    if eu is None:
        eu = _delimiter(first) == ";"  # semicolon exports come from comma-decimal locales
    out = []
    for r in body:
        get = lambda k: r[idx[k]] if k in idx and idx[k] < len(r) else ""
        raw_name = get("name").strip()
        if ("name" in idx and not raw_name) or _TOTAL_ROW.match(raw_name):
            continue  # the summary row (named "Total", or no ad name at all)
        name = raw_name or f"Row {len(out) + 1}"
        out.append({"name": name, "spend": _num(get("spend"), eu), "leads": _num(get("leads"), eu, integer=True),
                    "impressions": _num(get("impressions"), eu, integer=True),
                    "clicks": _num(get("clicks"), eu, integer=True), "status": get("status").strip()})
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
