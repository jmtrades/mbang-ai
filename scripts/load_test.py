#!/usr/bin/env python3
"""Load test against a running server:
  1. /health requests per second (target ≥ 300 RPS locally),
  2. accountability dashboard over 2,000 DEMO students (target p95 < 1 s),
  3. a 500-lead burst through the job queue (target 0 failures).

  scripts/load_test.py [--url http://127.0.0.1:8850] [--seconds 5] [--concurrency 16] [--seed-students 2000]
"""
from __future__ import annotations

import argparse
import statistics
import sys
import threading
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def health_rps(url: str, seconds: float, concurrency: int) -> dict:
    stop = time.time() + seconds
    counts = [0] * concurrency
    errors = [0] * concurrency

    def worker(i: int):
        with httpx.Client(base_url=url, timeout=5) as c:
            while time.time() < stop:
                try:
                    r = c.get("/health")
                    if r.status_code == 200:
                        counts[i] += 1
                    else:
                        errors[i] += 1
                except httpx.HTTPError:
                    errors[i] += 1

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(concurrency)]
    t0 = time.time()
    [t.start() for t in ts]
    [t.join() for t in ts]
    dt = time.time() - t0
    return {"requests": sum(counts), "errors": sum(errors), "seconds": round(dt, 2), "rps": round(sum(counts) / dt, 1)}


def dashboard_p95(url: str, n: int = 20) -> dict:
    c = httpx.Client(base_url=url, timeout=30)
    c.post("/t/demo/api/auth/demo-login", json={"role": "owner"})
    times = []
    total = 0
    for _ in range(n):
        t0 = time.time()
        r = c.get("/t/demo/api/students")
        times.append(time.time() - t0)
        total = r.json().get("total", 0)
    times.sort()
    return {"students": total, "p50_ms": round(statistics.median(times) * 1000), "p95_ms": round(times[int(0.95 * (n - 1))] * 1000)}


def burst(url: str, n: int = 500, timeout: float = 180) -> dict:
    c = httpx.Client(base_url=url, timeout=30)
    c.post("/t/demo/api/auth/demo-login", json={"role": "owner"})
    t0 = time.time()
    b = c.post("/t/demo/api/burst", json={"n": n}).json()["batch"]
    st = {}
    while time.time() - t0 < timeout:
        st = c.get(f"/t/demo/api/burst/{b}").json()
        if st.get("complete"):
            break
        time.sleep(0.5)
    st["seconds"] = round(time.time() - t0, 1)
    return st


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8850")
    ap.add_argument("--seconds", type=float, default=5)
    ap.add_argument("--concurrency", type=int, default=16)
    ap.add_argument("--seed-students", type=int, default=2000)
    ap.add_argument("--data-dir", help="seed the demo DB directly (same machine) before measuring")
    a = ap.parse_args(argv)
    if a.seed_students and a.data_dir:
        import os

        os.environ["OPERATOR_DATA_DIR"] = a.data_dir
        from operator_ai import tenancy
        from operator_ai.agents import accountability

        t = tenancy.get("demo")
        with t.db() as c:
            have = c.execute("SELECT COUNT(*) FROM students").fetchone()[0]
        if have < a.seed_students:
            accountability.seed_bulk(t, a.seed_students - have)
    h = health_rps(a.url, a.seconds, a.concurrency)
    print(f"/health: {h['rps']} RPS ({h['requests']} ok, {h['errors']} errors in {h['seconds']} s)")
    d = dashboard_p95(a.url)
    print(f"dashboard over {d['students']} students: p50 {d['p50_ms']} ms, p95 {d['p95_ms']} ms")
    b = burst(a.url)
    print(f"500-lead burst: {b.get('done')} done, {b.get('failed')} failed in {b.get('seconds')} s; tiers {b.get('tiers')}")
    ok = h["rps"] >= 300 and d["p95_ms"] < 1000 and b.get("failed") == 0 and b.get("done") == 500
    print("LOAD TEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
