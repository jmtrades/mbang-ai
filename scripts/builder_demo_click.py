#!/usr/bin/env python3
"""Headless click-through of the 90-second builder demo (§11). Prints CLEAN x/N.

  scripts/builder_demo_click.py --times 3 [--url http://127.0.0.1:8850] [--bonus]
"""
from __future__ import annotations

import argparse
import re
import sys
import time

from playwright.sync_api import expect, sync_playwright

TIMEOUT = 15_000


def click_through(page, base: str, bonus: bool = True) -> list[str]:
    """Run the §11 script once. Returns a list of problems (empty = CLEAN)."""
    problems: list[str] = []
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))

    def check(cond: bool, what: str):
        if not cond:
            problems.append(what)

    # 1. Sign-in -> Pitch demo -> Owner
    page.goto(f"{base}/t/demo/", wait_until="networkidle")
    if page.get_by_test_id("pitch-demo").count():
        page.get_by_test_id("pitch-demo").get_by_role("button", name="owner").click()
    expect(page.get_by_test_id("whoami")).to_have_text("Owner (DEMO) · PITCH DEMO", timeout=TIMEOUT)
    check(page.locator("nav[aria-label=Main]").first.is_visible(), "sidebar visible")

    # 2. Sidebar -> AI departments
    page.locator("[data-nav=builder]").first.click()
    expect(page.get_by_role("heading", name="Build a business's AI team")).to_be_visible(timeout=TIMEOUT)
    picker = page.get_by_test_id("business-picker")
    check(picker.locator("[data-preset]").count() == 4, "3 presets + own business")
    check(picker.get_by_text("DEMO", exact=True).count() == 3, "3 businesses marked DEMO")
    check(picker.get_by_text("Your own business").count() == 1, "'Your own business' card")

    # 3. Physio preset -> Generate departments (< 1 s, rules)
    picker.locator("[data-preset=physio]").click()
    t0 = time.time()
    page.get_by_test_id("generate").click()
    summary = page.get_by_test_id("dept-summary")
    expect(summary).to_contain_text("5 departments · 10 agents", timeout=TIMEOUT)
    gen_s = time.time() - t0
    check(gen_s < 1.5, f"generate took {gen_s:.2f}s (want < 1 s server side)")
    expect(summary).to_contain_text("rules, no AI call")
    deps = page.get_by_test_id("departments")
    cards = deps.locator("section")
    check(cards.count() == 5, "five department cards")
    for i in range(cards.count()):
        c = cards.nth(i)
        check("KPI:" in c.inner_text(), f"department {i + 1} shows a KPI")
        check(c.locator("[data-agent]").count() == 2, f"department {i + 1} has 2 agents")

    # 4. Sales follow-up & DM setter -> DM setter
    page.locator("[data-agent=dm_setter]").click()
    card = page.get_by_test_id("agent-card")
    expect(card).to_be_visible(timeout=TIMEOUT)
    text = card.inner_text()
    for needle in ("Produces", "Trigger", "KPI", "No price in DMs", "One question per message",
                   "Everything goes to approval", "Instructions"):
        check(needle in text, f"agent card shows {needle}")
    check(card.get_by_text("DEMO", exact=True).count() == 3, "3 DEMO inbound DMs")

    # 5. Run on demo data
    t0 = time.time()
    page.get_by_test_id("run-demo").click()
    headline = page.get_by_test_id("run-headline")
    expect(headline).to_have_text("3 drafts · 3 pass review · Sent: 0", timeout=30_000)
    run_s = time.time() - t0
    check(run_s < 5, f"run took {run_s:.1f}s")
    steps = page.get_by_test_id("run-steps").locator("li")
    check(steps.count() == 5, "5 steps shown")
    check(page.get_by_test_id("run-steps").locator("[data-status=ok]").count() == 5, "5 steps ticked")
    rc = page.get_by_test_id("reply-card")
    check(rc.count() == 3, "three reply cards")
    for i in range(rc.count()):
        t = rc.nth(i).inner_text()
        check(("AI" in t or "Rules" in t) and "score" in t and "Queue #" in t, f"reply card {i + 1} has badge, score, queue #")

    # 6. Review in the queue
    page.get_by_test_id("review-queue").click()
    items = page.get_by_test_id("queue-item")
    expect(items.first).to_be_visible(timeout=TIMEOUT)
    check(items.count() == 3, "3 drafts in the filtered queue")
    for i in range(items.count()):
        t = items.nth(i).inner_text()
        check(re.search(r"Reply to .+\(DEMO\)", t) is not None, f"queue item {i + 1} is 'Reply to … (DEMO)'")
        check(items.nth(i).get_attribute("data-status") == "pending", f"queue item {i + 1} pending approval")

    # 7. Back -> Start over
    page.go_back()
    page.get_by_test_id("start-over").click()
    expect(page.get_by_role("heading", name="Build a business's AI team")).to_be_visible(timeout=TIMEOUT)

    if bonus:
        # Bookkeeping -> Support agent: refund / legal threats escalated
        page.get_by_test_id("business-picker").locator("[data-preset=bookkeeping]").click()
        page.get_by_test_id("generate").click()
        expect(page.get_by_test_id("dept-summary")).to_contain_text("5 departments · 10 agents", timeout=TIMEOUT)
        page.locator("[data-agent=support_agent]").click()
        page.get_by_test_id("run-demo").click()
        expect(page.get_by_test_id("run-headline")).to_be_visible(timeout=30_000)
        esc = page.get_by_test_id("reply-card").filter(has_text="Escalated to a person")
        check(esc.count() == 2, f"refund + legal tickets escalated (got {esc.count()})")
        page.get_by_test_id("start-over").click()
        # Gym -> Ad performance analyst: every $ line labelled DEMO
        page.get_by_test_id("business-picker").locator("[data-preset=gym]").click()
        page.get_by_test_id("generate").click()
        expect(page.get_by_test_id("dept-summary")).to_contain_text("5 departments · 10 agents", timeout=TIMEOUT)
        page.locator("[data-agent=ad_performance_analyst]").click()
        page.get_by_test_id("run-demo").click()
        expect(page.get_by_test_id("run-headline")).to_be_visible(timeout=30_000)
        body = page.get_by_test_id("reply-cards").inner_text()
        dollar_bits = re.findall(r"\$[\d,.]+(?: \(DEMO\))?", body)
        check(dollar_bits and all(b.endswith("(DEMO)") for b in dollar_bits), "every $ figure labelled DEMO")
        page.get_by_test_id("start-over").click()
        expect(page.get_by_role("heading", name="Build a business's AI team")).to_be_visible(timeout=TIMEOUT)

    errs = [e for e in errors if "favicon" not in e]
    check(not errs, f"console errors: {errs[:3]}")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--times", type=int, default=1)
    ap.add_argument("--url", default="http://127.0.0.1:8850")
    ap.add_argument("--no-bonus", action="store_true")
    a = ap.parse_args(argv)
    clean = 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for i in range(a.times):
            ctx = browser.new_context(viewport={"width": 1440, "height": 900})
            page = ctx.new_page()
            try:
                probs = click_through(page, a.url.rstrip("/"), bonus=not a.no_bonus)
            except Exception as e:  # noqa: BLE001
                probs = [f"{type(e).__name__}: {str(e).splitlines()[0][:200]}"]
            ctx.close()
            if probs:
                print(f"RUN {i + 1}: NOT CLEAN")
                for pr in probs:
                    print("  -", pr)
            else:
                clean += 1
                print(f"RUN {i + 1}: CLEAN")
        browser.close()
    print(f"CLEAN {clean}/{a.times}")
    return 0 if clean == a.times else 1


if __name__ == "__main__":
    sys.exit(main())
