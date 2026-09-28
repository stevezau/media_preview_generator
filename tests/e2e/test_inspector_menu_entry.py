"""E2E: Tools → Inspector is one menu entry for a file's preview frames and its intro & credits."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from . import _inspector_fixtures as fx


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


@pytest.mark.e2e
class TestToolsMenuEntry:
    def test_one_entry_opens_the_inspector(self, authed_page: Page, app_url: str) -> None:
        fx.install(authed_page)
        authed_page.goto(f"{app_url}/logs")
        authed_page.locator("#navToolsDropdown").click()

        items = authed_page.locator("#navToolsDropdown + .dropdown-menu .dropdown-item")
        labels = [t.strip() for t in items.all_inner_texts()]
        assert "Inspector" in labels
        # The two old entries are one page now.
        assert "Preview Inspector" not in labels
        assert "Intro & Credits" not in labels
        authed_page.locator("#navToolsDropdown + .dropdown-menu a[href='/inspector']").click()

        expect(authed_page).to_have_url(f"{app_url}/inspector")
        expect(authed_page).to_have_title("Inspector - Media Preview Generator")
        expect(authed_page.locator("#inspSearchTitle")).to_have_text("Inspector")
        expect(authed_page.locator("#inspQuery")).to_be_visible()

    def test_the_menu_marks_the_inspector_active(self, authed_page: Page, app_url: str) -> None:
        fx.install(authed_page)
        authed_page.goto(f"{app_url}/inspector")
        expect(authed_page.locator("#navToolsDropdown")).to_have_class(re.compile(r"\bactive\b"))
        authed_page.locator("#navToolsDropdown").click()
        expect(authed_page.locator("#navToolsDropdown + .dropdown-menu a[href='/inspector']")).to_have_class(
            re.compile(r"\bactive\b")
        )

    def test_the_old_intro_and_credits_address_lands_on_the_inspector(self, authed_page: Page, app_url: str) -> None:
        fx.install(authed_page)
        authed_page.goto(f"{app_url}/bif-viewer?tab=markers")
        expect(authed_page).to_have_url(f"{app_url}/inspector")
        expect(authed_page.locator("#inspSearchTitle")).to_have_text("Inspector")
