"""E2E: every page's state-changing fetches carry the CSRF token without each call site adding it.

Runs against the real app (no ``page.route`` mocks on the calls under test), so the server's CSRF check is what
answers.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from .conftest import get_free_port

# ``fetch`` with no X-CSRFToken header, the way a call site that forgot it would send it.
_BARE_POST = """async (url) => {
    const r = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
    return {status: r.status, body: await r.text()};
}"""

# The same request through the browser's own fetch, bypassing the page's wrapper.
_NATIVE_POST = """async (url) => {
    const frame = document.createElement('iframe');
    frame.src = 'about:blank';
    document.body.appendChild(frame);
    const r = await frame.contentWindow.fetch(new URL(url, location.href), {method: 'POST', credentials: 'include'});
    return {status: r.status, body: await r.text()};
}"""


@pytest.mark.e2e
class TestCsrfFetchWrapper:
    @pytest.mark.parametrize("page_path", ["/", "/settings", "/servers", "/automation", "/logs"])
    def test_a_bare_post_from_any_page_is_accepted(
        self, authed_page: Page, app_url: str, complete_setup: None, page_path: str
    ) -> None:
        authed_page.goto(f"{app_url}{page_path}")

        result = authed_page.evaluate(_BARE_POST, "/api/system/whats-new/dismiss")

        assert result["status"] == 200, result

    def test_the_server_refuses_the_same_post_without_the_token(
        self, authed_page: Page, app_url: str, complete_setup: None
    ) -> None:
        # Proves the check above isn't passing only because the server lets everything through.
        authed_page.goto(f"{app_url}/logs")

        result = authed_page.evaluate(_NATIVE_POST, "/api/system/whats-new/dismiss")

        assert result["status"] == 400
        assert "security token" in result["body"]

    def test_the_token_never_goes_to_another_site(self, authed_page: Page, app_url: str, complete_setup: None) -> None:
        seen: dict = {}

        def capture(route):
            seen.update(route.request.headers)
            route.fulfill(status=200, body="{}", headers={"Access-Control-Allow-Origin": "*"})

        authed_page.route("https://other-site.example/**", capture)
        authed_page.goto(f"{app_url}/logs")

        authed_page.evaluate(
            "async () => { await fetch('https://other-site.example/hook', {method: 'POST', body: 'x'}); }"
        )

        assert seen, "the cross-site request never went out"
        assert "x-csrftoken" not in seen

    def test_a_get_goes_out_without_the_token(self, authed_page: Page, app_url: str, complete_setup: None) -> None:
        seen: dict = {}

        def capture(route):
            seen.update(route.request.headers)
            route.continue_()

        authed_page.goto(f"{app_url}/logs")
        authed_page.route("**/api/system/status", capture)

        authed_page.evaluate("async () => { await fetch('/api/system/status'); }")

        assert seen, "the GET never went out"
        assert "x-csrftoken" not in seen


@pytest.mark.e2e
class TestLoginFormWithCsrf:
    # Signing in through the form with its token is covered by test_webapp.py's login tests.
    def test_an_expired_sign_in_page_says_so(self, page: Page, app_url: str, complete_setup: None) -> None:
        page.goto(f"{app_url}/login")
        page.context.clear_cookies()  # the session holding the form's token is gone, as after a restart
        page.locator("#token").fill("e2e-test-token")
        page.locator('button[type="submit"]').click()

        expect(page.locator(".alert-warning")).to_contain_text("This sign-in page expired.", timeout=3000)


@pytest.mark.e2e
class TestSignOut:
    def test_the_navs_logout_signs_out(self, authed_page: Page, app_url: str, complete_setup: None) -> None:
        authed_page.goto(f"{app_url}/logs")

        authed_page.locator("#navLogoutBtn").click()

        expect(authed_page).to_have_url(f"{app_url}/login", timeout=5000)
        authed_page.goto(f"{app_url}/logs")
        expect(authed_page).to_have_url(f"{app_url}/login", timeout=5000)

    def test_another_app_on_this_host_cant_sign_the_user_out(
        self, authed_page: Page, app_url: str, complete_setup: None
    ) -> None:
        # A page on another port of this host is the same site, so the browser sends the session cookie with anything
        # it makes it fetch; a link it follows sends it from any site. (An <img> sends it too, but the browser throws
        # away an image request's HTML answer, so a test can't tell what the app did with it.)
        other_app = f"http://localhost:{get_free_port()}/"  # nothing listens: the route answers
        page = f'<script>window.location.href = "{app_url}/logout";</script>'
        authed_page.route(other_app, lambda route: route.fulfill(status=200, content_type="text/html", body=page))

        with authed_page.expect_request(f"{app_url}/logout") as request:
            authed_page.goto(other_app)
        assert "session=" in (request.value.all_headers().get("cookie") or "")

        expect(authed_page.locator("#logoutConfirm")).to_contain_text("Sign out?", timeout=5000)
        authed_page.goto(f"{app_url}/logs")
        expect(authed_page).to_have_url(f"{app_url}/logs")
        expect(authed_page.locator("#navLogoutBtn")).to_be_visible()

    def test_opening_logout_asks_first_and_its_button_signs_out(
        self, authed_page: Page, app_url: str, complete_setup: None
    ) -> None:
        authed_page.goto(f"{app_url}/logout")
        expect(authed_page.locator("#logoutConfirm")).to_contain_text("Sign out?")

        authed_page.locator("#logoutConfirmBtn").click()

        expect(authed_page).to_have_url(f"{app_url}/login", timeout=5000)
