"""Browser checks for the cross-feature wiring: the Academy's Role-play tab, the Inbox for people who aren't the
owner (no new nav tab), "Forgot password?" on the sign-in page and the brand logo in the shell."""
import pytest

pytestmark = pytest.mark.ui
IGNORED = ("favicon", "Download the React DevTools")


def _errors(page):
    errs: list[str] = []
    page.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errs.append(str(e)))
    return errs


def _demo(page, base, role):
    page.goto(f"{base}/t/demo/", wait_until="networkidle")
    page.get_by_test_id("pitch-demo").get_by_role("button", name=role).click()
    page.get_by_test_id("ai-pill").wait_for(timeout=15000)


@pytest.mark.timeout(600)
def test_role_play_tab_inbox_forgot_link_and_logo(tmp_path):
    from playwright.sync_api import sync_playwright

    from tests.server import running_server

    with running_server(tmp_path) as base, sync_playwright() as p:
        browser = p.chromium.launch()
        for vp in ({"width": 1440, "height": 900}, {"width": 390, "height": 844}):
            # a student opens the Academy's Role-play tab and plays a turn
            ctx = browser.new_context(viewport=vp)
            page = ctx.new_page()
            errs = _errors(page)
            _demo(page, base, "student")
            page.evaluate("location.hash = '#/academy'")
            page.locator("main h1").first.wait_for(timeout=15000)
            page.get_by_role("tab", name="Role-play").click()
            panel = page.get_by_test_id("roleplay-panel")
            panel.wait_for(timeout=15000)
            assert "Role-play is coming" not in panel.inner_text()
            panel.get_by_role("button", name="Start").first.click()
            page.get_by_test_id("roleplay-chat").wait_for(timeout=15000)
            before = page.get_by_test_id("roleplay-chat").locator("li").count()
            page.get_by_label("Your line").fill("Thanks for making the time. What would you like the next three months to look like?")
            page.get_by_test_id("roleplay-send").click()
            page.wait_for_function(f"document.querySelectorAll('[data-testid=roleplay-chat] li').length > {before}", timeout=15000)
            # the student's replies: an Inbox tab inside Prospects (no new navigation tab)
            page.evaluate("location.hash = '#/prospects'")
            page.locator("main h1").first.wait_for(timeout=15000)
            page.get_by_role("tab", name="Inbox").click()
            page.get_by_test_id("prospects-inbox").get_by_test_id("inbox").wait_for(timeout=15000)
            assert not page.locator("nav[aria-label=Main] a[href='#/channels']").count()
            assert [e for e in errs if not any(i in e for i in IGNORED)] == []
            overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
            assert overflow <= 2, overflow
            ctx.close()
        # a coach (no Prospects tab) reads their students' conversations from the Queue page
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        _demo(page, base, "coach")
        page.evaluate("location.hash = '#/queue'")
        page.locator("main h1").first.wait_for(timeout=15000)
        page.get_by_role("tab", name="Inbox").click()
        page.get_by_test_id("queue-inbox").get_by_test_id("inbox").wait_for(timeout=15000)
        ctx.close()
        # the real workspace's sign-in page links "Forgot password?" to the join screen
        ctx = browser.new_context(viewport={"width": 390, "height": 844})
        page = ctx.new_page()
        page.goto(f"{base}/t/owner/", wait_until="networkidle")
        page.get_by_test_id("forgot-password").click()
        page.get_by_text("Forgot your password?").wait_for(timeout=15000)
        assert page.get_by_test_id("brand-monogram").count()  # no logo set yet: the monogram
        ctx.close()
        # with a logo saved (Setup, brand), the shell shows it instead of the monogram
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        _demo(page, base, "owner")
        logo = "/icon.svg"  # a path on this site (the app's own icon)
        r = page.request.post(f"{base}/t/demo/api/setup/brand", data={"name": "Peak Growth (DEMO)", "logo": logo})
        assert r.status == 200, r.text()
        page.reload(wait_until="networkidle")
        page.get_by_test_id("ai-pill").wait_for(timeout=15000)
        assert page.get_by_test_id("brand-logo").first.get_attribute("src") == logo
        ctx.close()
        browser.close()
