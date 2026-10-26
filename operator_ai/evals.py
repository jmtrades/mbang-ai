"""Evaluation runs: retrieval hit@k, out-of-scope honesty, reviewer flaw detection.
Results are stored in eval_runs so the Evals tab can show a trend."""
from __future__ import annotations

import json
from pathlib import Path

from . import config, db, retrieval

TESTS = config.ROOT / "tests"


def load(name: str) -> list[dict]:
    return json.loads((TESTS / name).read_text())


def retrieval_eval(tenant, gold: list[dict] | None = None, k_max: int = 10) -> dict:
    gold = gold or load("gold_questions.json")
    hit5 = hit10 = 0
    misses = []
    for g in gold:
        hits = retrieval.search(tenant, g["question"], k=k_max)
        files = [h["file"] for h in hits]
        exp = set(g["expected"])
        r = next((i for i, f in enumerate(files) if f in exp), None)
        if r is not None and r < 5:
            hit5 += 1
        if r is not None and r < 10:
            hit10 += 1
        else:
            misses.append({"id": g["id"], "question": g["question"], "got": files[:3]})
    n = len(gold)
    metrics = {"n": n, "hit@5": round(hit5 / n, 3), "hit@10": round(hit10 / n, 3), "misses": misses}
    store(tenant, "retrieval", metrics)
    return metrics


def honesty_eval(tenant) -> dict:
    from .agents import coach

    nc = load("not_covered_questions.json")
    honest = 0
    wrong = []
    for q in nc:
        res = coach.answer(tenant, q["question"], user=None, record=False)
        if res["covered"] == "no":
            honest += 1
        else:
            wrong.append(q["question"])
    gold = load("gold_questions.json")
    answered = sum(1 for g in gold if coach.answer(tenant, g["question"], user=None, record=False)["covered"] != "no")
    metrics = {"n_out_of_scope": len(nc), "honesty": round(honest / len(nc), 3), "false_answers": wrong,
               "in_scope_answered": round(answered / len(gold), 3)}
    store(tenant, "honesty", metrics)
    return metrics


def reviewer_eval(tenant) -> dict:
    from .agents import reviewer

    tasks = load("audit_tasks.json")
    found = total = 0
    detail = []
    for t in tasks:
        res = reviewer.review(tenant, t["text"], asset_type=t["asset_type"], user=None)
        codes = set(res["detected"])
        got = [f for f in t["flaws"] if f in codes]
        found += len(got)
        total += len(t["flaws"])
        detail.append({"id": t["id"], "expected": t["flaws"], "found": got})
    metrics = {"flaw_detection": round(found / total, 3), "found": found, "total": total, "detail": detail}
    store(tenant, "reviewer", metrics)
    return metrics


def store(tenant, kind: str, metrics: dict) -> None:
    corpus = "fixture" if config.get().using_fixture_corpus else "inputs"
    with tenant.db() as c:
        c.execute("INSERT INTO eval_runs(kind,metrics,corpus,created) VALUES(?,?,?,?)",
                  (kind, db.jdump(metrics), corpus, db.now()))


def history(tenant, limit: int = 50) -> list[dict]:
    with tenant.db() as c:
        out = db.rows(c.execute("SELECT * FROM eval_runs ORDER BY id DESC LIMIT ?", (limit,)))
    for r in out:
        r["metrics"] = db.jload(r["metrics"], {})
    return out
