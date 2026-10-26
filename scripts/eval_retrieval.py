#!/usr/bin/env python3
"""Retrieval eval: hit@5 / hit@10 on tests/gold_questions.json against the active corpus.
Usage: scripts/eval_retrieval.py [--tenant owner] [--gold path.json]"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from operator_ai import evals, tenancy  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", default="owner")
    ap.add_argument("--gold")
    a = ap.parse_args()
    tenancy.ensure_defaults()
    t = tenancy.get(a.tenant)
    gold = json.loads(Path(a.gold).read_text()) if a.gold else None
    m = evals.retrieval_eval(t, gold)
    print(json.dumps({k: v for k, v in m.items() if k != "misses"}, indent=1))
    for miss in m["misses"]:
        print("MISS", miss["id"], miss["question"], "->", miss["got"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
