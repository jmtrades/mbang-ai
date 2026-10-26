#!/usr/bin/env python3
"""Render the acceptance table of SELF_SCORE.md from operator_ai/scorecard.py (the same rows
the Go-live screen shows). The narrative sections (numbers, known gaps) are written by hand.

  scripts/self_score.py > /tmp/table.md
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from operator_ai import scorecard  # noqa: E402

SECTIONS = {"A": "A. Parity rows", "B": "B. Beyond parity", "C": "C. Guardrails and safety", "D": "D. Platform and ops",
            "E": "E. Quality"}


def main() -> int:
    rows = scorecard.ROWS
    for sec, title in SECTIONS.items():
        print(f"\n### {title}\n")
        print("| ID | Item | Score | Status | Evidence | Notes |")
        print("|---|---|---|---|---|---|")
        for r in rows:
            if r["id"][0] != sec:
                continue
            ev = "<br>".join(f"`{e}`" for e in r["evidence"])
            notes = "; ".join(x for x in (r.get("notes", ""), f"waiting for: {r['waiting_for']}" if r.get("waiting_for") else "") if x)
            print(f"| {r['id']} | {r['item']} | {r['score']} | {r['status']} | {ev} | {notes} |")
    t = scorecard.totals(rows)
    print("\n### Totals\n")
    print("| Section | Score | Max |")
    print("|---|---|---|")
    for sec, v in t.items():
        print(f"| {SECTIONS[sec]} | {v['score']} | {v['max']} |")
    print(f"| **All** | **{sum(v['score'] for v in t.values())}** | **{sum(v['max'] for v in t.values())}** |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
