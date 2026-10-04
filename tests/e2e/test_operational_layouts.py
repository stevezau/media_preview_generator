"""Bounded viewport/theme coverage for the operational pages."""

import os
from pathlib import Path

import pytest
from playwright.sync_api import expect

from .test_operational_followup import _activity, _logs, _resume

pytestmark = pytest.mark.e2e
SIZES = [(1440, "dark"), (1440, "light"), (390, "dark"), (390, "light")]


def _capture(page, name):
    if os.environ.get("MPG_UX_SCREENSHOTS") == "1":
        destination = Path(__file__).resolve().parents[2] / "docs/design/followup-ux"
        destination.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(destination / f"operations-{name}.jpg"), quality=75, full_page=False)


@pytest.mark.parametrize("width,theme", SIZES)
def test_operational_pages_fit_viewport(authed_page, app_url, complete_setup, width, theme):
    page = authed_page
    page.set_viewport_size({"width": width, "height": 900 if width > 400 else 844})
    page.add_init_script(f"localStorage.setItem('theme', '{theme}')")
    _logs(page)
    _activity(page)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    for route in ["logs", "webhook-activity", "automation"]:
        page.goto(app_url + "/" + route)
        expect(page.locator("h1")).to_be_visible()
        if route == "logs":
            expect(page.locator(".log-line")).to_have_count(2)
        if route == "webhook-activity":
            expect(page.locator("#historyFilterCount")).to_contain_text("2 of 2")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), route
        if (width == 390 and theme == "dark" and route in ["logs", "webhook-activity"]) or (
            width == 1440 and theme == "light" and route == "automation"
        ):
            _capture(page, f"{route}-{width}-{theme}")
    assert not errors


@pytest.mark.parametrize("width,theme", SIZES)
def test_setup_and_login_fit_viewport(wizard_page, app_url_wizard, width, theme):
    page = wizard_page
    page.set_viewport_size({"width": width, "height": 900 if width > 400 else 844})
    page.add_init_script(f"localStorage.setItem('theme', '{theme}')")
    _resume(page, "jellyfin")
    page.goto(app_url_wizard + "/setup")
    expect(page.locator("#step3Next")).to_be_enabled()
    expect(page.locator("#setupProgressCurrent")).to_have_text("Step 2 of 4 · Paths")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    page.locator("#setupPathPreviewHeading").scroll_into_view_if_needed()
    if width == 390 and theme == "dark":
        _capture(page, "setup-390-dark")
    page.context.clear_cookies()
    page.goto(app_url_wizard + "/login")
    expect(page.locator("#token")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    if width == 1440 and theme == "dark":
        _capture(page, "login-1440-dark")
