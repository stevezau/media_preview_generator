"""E2E tests for the /login page (additional coverage beyond test_webapp.py)."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.e2e
class TestLoginPage:
    def test_token_input_is_autofocused(self, page: Page, app_url: str) -> None:
        page.goto(f"{app_url}/login")
        expect(page.locator("#token")).to_be_focused()

    def test_invalid_token_shows_error_alert(self, page: Page, app_url: str) -> None:
        page.goto(f"{app_url}/login")
        page.locator("#token").fill("definitely-not-the-real-token")
        page.locator('button[type="submit"]').click()
        # The error alert renders with the new trimmed copy.
        expect(page.locator(".alert-danger")).to_contain_text("didn", timeout=3000)

    def test_login_page_shows_title_and_concise_subtitle(self, page: Page, app_url: str) -> None:
        page.goto(f"{app_url}/login")
        expect(page.locator("h1")).to_have_text("Media Preview Generator")
        expect(page.locator(".logo-container p")).to_have_text("Enter your token to continue")

    def test_token_field_and_reveal_button_are_touch_sized_when_viewport_is_phone(
        self, page: Page, app_url: str
    ) -> None:
        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(f"{app_url}/login")
        token = page.locator("#token").bounding_box()
        reveal = page.locator("#tokenReveal").bounding_box()
        assert token["height"] >= 44
        assert reveal["width"] >= 44 and reveal["height"] >= 44
