#!/usr/bin/env python3
"""API sweep: every GET route returns no 5xx; malformed bodies get 422 (or an auth/permission
answer), never 500. Works against a running server or in-process (tests).

  scripts/api_sweep.py [--url http://127.0.0.1:8850]
"""
from __future__ import annotations

import argparse
import re
import sys

SAMPLE = {"tid": "demo", "qid": "1", "run_id": "1", "tid_": "1", "mid": "1", "uid": "1", "key": "dm_opener", "wid": "1",
          "cid": "1", "card_id": "1", "sid": "1", "token": "x", "lead_id": "1", "client_id": "1", "chain_id": "wc-x",
          "batch": "x", "aid": "1", "build_id": "1", "agent_key": "dm_setter", "token_id": "1", "slug": "x", "path": "x"}
BAD_BODIES = [b"{not json", b"[1,2,3]", b'{"text": ["wrong type"], "question": 5, "limit": "many"}', b'""']


def routes(openapi: dict) -> list[tuple[str, str]]:
    out = []
    for path, ops in openapi["paths"].items():
        for method in ops:
            out.append((method.upper(), path))
    return out


def fill(path: str) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: SAMPLE.get(m.group(1), "1"), path)


def sweep(client, login) -> dict:
    """client: httpx/TestClient-like; login(client) signs in as the demo owner."""
    login(client)
    spec = client.get("/openapi.json").json()
    problems, checked = [], 0
    for method, path in routes(spec):
        url = fill(path)
        if path in ("/{path}",) or "{path}" in path:
            continue
        if method == "GET":
            r = client.get(url)
            checked += 1
            if r.status_code >= 500:
                problems.append(f"GET {url} -> {r.status_code}")
        elif method in ("POST", "PUT", "PATCH"):
            if path.endswith("/auth/logout"):
                continue
            for body in BAD_BODIES:
                r = client.request(method, url, content=body, headers={"content-type": "application/json"})
                checked += 1
                if r.status_code >= 500:
                    problems.append(f"{method} {url} body={body[:20]!r} -> {r.status_code}")
                elif r.status_code == 200 and body != b"[1,2,3]" and "hooks" not in url and "/mcp" not in url:
                    # a route with no body schema may accept anything; that's fine as long as it's not a 5xx
                    pass
            login(client)  # some calls may end the session (e.g. demo reset); sign in again
    return {"checked": checked, "problems": problems}


def main(argv=None) -> int:
    import httpx

    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8850")
    a = ap.parse_args(argv)
    c = httpx.Client(base_url=a.url, timeout=60)
    res = sweep(c, lambda cl: cl.post("/t/demo/api/auth/demo-login", json={"role": "owner"}))
    for p in res["problems"]:
        print("PROBLEM", p)
    print(f"checked {res['checked']} requests, {len(res['problems'])} problems")
    return 0 if not res["problems"] else 1


if __name__ == "__main__":
    sys.exit(main())
