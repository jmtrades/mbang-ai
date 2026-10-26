"""Headless Playwright: every page × every role × desktop (1440×900) and mobile (390×844),
no console errors, no error boxes. Screenshots go to screens/."""
import pytest

from operator_ai import auth
from operator_ai.config import ROOT

pytestmark = pytest.mark.ui

VIEWPORTS = {"desktop": {"width": 1440, "height": 900}, "mobile": {"width": 390, "height": 844}}
IGNORED = ("favicon", "Download the React DevTools")


@pytest.mark.timeout(1500)
def test_every_page_every_role(tmp_path):
    from playwright.sync_api import sync_playwright

    from tests.server import running_server

    shots = ROOT / "screens"
    problems = []
    visited = 0
    with running_server(tmp_path) as base, sync_playwright() as p:
        browser = p.chromium.launch()
        for vp_name, vp in VIEWPORTS.items():
            for role in auth.ROLES:
                ctx = browser.new_context(viewport=vp, device_scale_factor=1)
                page = ctx.new_page()
                errors: list[str] = []
                page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(f"{base}/t/demo/", wait_until="networkidle")
                page.get_by_test_id("pitch-demo").get_by_role("button", name=role).click()
                page.get_by_test_id("ai-pill").wait_for(timeout=15000)
                for tab in auth.tabs_for(role):
                    errors.clear()
                    page.evaluate(f"location.hash = '#/{tab}'")
                    try:
                        page.locator("main h1").first.wait_for(timeout=15000)
                    except Exception as e:  # noqa: BLE001
                        problems.append(f"{vp_name}/{role}/{tab}: no heading ({type(e).__name__})")
                        continue
                    # let the page's data requests settle (skeletons replaced by content)
                    try:
                        page.locator("main [aria-busy=true]").first.wait_for(state="detached", timeout=8000)
                    except Exception:  # noqa: BLE001
                        pass
                    page.wait_for_timeout(250)
                    alerts = page.locator("main [role=alert]")
                    if alerts.count():
                        problems.append(f"{vp_name}/{role}/{tab}: error box: {alerts.first.inner_text()[:120]}")
                    errs = [e for e in errors if not any(i in e for i in IGNORED)]
                    if errs:
                        problems.append(f"{vp_name}/{role}/{tab}: console: {errs[:2]}")
                    # mobile: the page itself must not scroll sideways
                    if vp_name == "mobile":
                        overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
                        if overflow > 2:
                            problems.append(f"{vp_name}/{role}/{tab}: horizontal overflow {overflow}px")
                    (shots / vp_name).mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(shots / vp_name / f"{role}-{tab}.png"))
                    visited += 1
                ctx.close()
        browser.close()
    assert visited >= 2 * sum(len(auth.tabs_for(r)) for r in auth.ROLES) - len(problems)
    assert problems == [], "\n".join(problems)


@pytest.mark.timeout(300)
def test_pwa_installable_and_offline_shell(tmp_path):
    from playwright.sync_api import sync_playwright

    from tests.server import running_server

    with running_server(tmp_path) as base, sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(f"{base}/t/demo/", wait_until="networkidle")
        manifest = page.evaluate("document.querySelector('link[rel=manifest]').href")
        assert manifest.endswith("/manifest.webmanifest")
        assert page.evaluate("navigator.serviceWorker.ready.then(r => !!r.active)")
        page.reload(wait_until="networkidle")  # navigation now cached by the service worker
        ctx.set_offline(True)
        page.reload()
        page.wait_for_timeout(500)
        body = page.locator("body").inner_text()
        # the cached app shell boots offline and shows its designed reconnect state (or the offline page)
        assert "Reconnecting to the server" in body or "You're offline" in body, body[:200]
        ctx.set_offline(False)
        browser.close()


@pytest.mark.timeout(300)
def test_one_chat_routes_question_numbers_review_and_csv(tmp_path):
    from playwright.sync_api import expect, sync_playwright

    from tests.server import running_server
    from tests.test_router_chat import ADS_CSV

    csv_path = tmp_path / "ads_export.csv"
    csv_path.write_text(ADS_CSV)
    with running_server(tmp_path) as base, sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{base}/t/demo/", wait_until="networkidle")
        page.get_by_test_id("pitch-demo").get_by_role("button", name="student").click()
        page.evaluate("location.hash = '#/coach'")
        box = page.get_by_label("Message")
        chat = page.get_by_test_id("chat")

        def say(text, expect_text):
            box.fill(text)
            box.press("Enter")
            expect(chat).to_contain_text(expect_text, timeout=20000)

        say("What are the rules for a Thunder Dome test?", "three and five creatives")
        say("How long should it run?", "follow-up, read in context")
        say("800 DMs, 40 replies, 20 conversations, 10 links sent, 6 booked, 2 showed, 1 closed, price $3k", "Bottleneck: DM reply rate")
        say("Review this DM:\nHey! What's your goal? How many clients? Our fee is $3k a month.", "Dm Script scored")
        page.set_input_files("input[type=file][aria-label='Attach a file']", str(csv_path))
        expect(chat).to_contain_text("Read-only: nothing was changed in any ad account.", timeout=20000)
        expect(chat).to_contain_text("winner")
        assert errors == []
        page.screenshot(path=str(ROOT / "screens" / "desktop" / "student-coach-one-chat.png"), full_page=True)
        browser.close()
