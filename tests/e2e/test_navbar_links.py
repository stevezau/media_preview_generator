"""E2E: the navbar's running version, Ko-fi coffee link and Star button, at desktop and phone width.

The app here runs as a ``dev`` image (``GIT_BRANCH=dev``, ``GIT_SHA=a9c8177…``) so the version text is exact, with
its GitHub calls sent to a dead proxy so they fail fast. Each test then pins the cached update check through the
test-only ``/api/__test/version-cache`` endpoint, which is what the navbar reads.

The layout tests run twice: in this machine's font and in DejaVu Sans, a wide font a Linux browser falls back to
(the CI runner's fonts wrapped a bar that fit here). One row and no sideways scroll must hold in both.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.request
from collections.abc import Generator

import pytest
from playwright.sync_api import BrowserContext, Page, expect

from .conftest import _capture_session_cookie, _seed_settings_complete, _start_app, get_free_port

KOFI = "https://ko-fi.com/stevezau"
REPO = "https://github.com/stevezau/media_preview_generator"
RELEASES = "https://github.com/stevezau/media_preview_generator/releases/latest"
DESKTOP = {"width": 1440, "height": 900}
PHONE = {"width": 390, "height": 844}
WIDE_FONT = '* { font-family: "DejaVu Sans", sans-serif !important; }'
FONTS = ["page-font", "wide-font"]

NO_UPDATE = {
    "current_version": "dev@a9c8177",
    "latest_version": None,
    "update_available": False,
    "install_type": "dev_docker",
}
DEV_UPDATE = {**NO_UPDATE, "latest_version": "dev@896c569", "update_available": True}
RELEASE_UPDATE = {
    "current_version": "3.4.0",
    "latest_version": "3.4.1",
    "update_available": True,
    "install_type": "docker",
}


def _call(url: str, body: dict | None = None) -> None:
    req = urllib.request.Request(
        url,
        method="POST" if body is not None else "GET",
        headers={"X-Auth-Token": "e2e-test-token", "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None,
    )
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 (test-only localhost)
        assert resp.status == 200


@pytest.fixture(scope="module")
def dev_app(tmp_path_factory) -> Generator[str, None, None]:
    config_dir = str(tmp_path_factory.mktemp("navbar_dev_app"))
    _seed_settings_complete(config_dir)
    dead_proxy = "http://127.0.0.1:9"
    extra_env = {
        "GIT_BRANCH": "dev",
        "GIT_SHA": "a9c8177d2e41",
        "HTTPS_PROXY": dead_proxy,
        "https_proxy": dead_proxy,
        "CORS_ORIGINS": "*",
    }
    port = get_free_port()
    proc = _start_app(config_dir, port, extra_env=extra_env)
    url = f"http://localhost:{port}"
    try:
        # Let the startup check finish (it fails fast on the dead proxy) before any test pins the cache.
        _call(f"{url}/api/system/version")
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="module")
def dev_cookie(dev_app: str) -> dict:
    return _capture_session_cookie(dev_app)


@pytest.fixture
def dev_page(page: Page, context: BrowserContext, dev_cookie: dict) -> Page:
    context.add_cookies([dev_cookie])
    return page


def _open(page: Page, app: str, check: dict, viewport: dict, path: str = "/", font: str = "page-font") -> None:
    _call(f"{app}/api/__test/version-cache", check)
    page.set_viewport_size(viewport)
    page.goto(f"{app}{path}")
    expect(page.locator("nav.navbar")).to_be_visible()
    if font == "wide-font":
        page.add_style_tag(content=WIDE_FONT)
        assert "DejaVu Sans" in page.evaluate(
            "() => getComputedStyle(document.querySelector('.navbar-brand')).fontFamily"
        )


def _box(page: Page, selector: str) -> dict:
    box = page.locator(selector).bounding_box()
    assert box is not None, f"{selector} has no box"
    return box


def _no_sideways_scroll(page: Page, viewport: dict) -> None:
    assert page.evaluate("() => document.documentElement.scrollWidth") == viewport["width"]


def _stays_pinned(page: Page) -> None:
    before = _box(page, "nav.navbar")
    page.evaluate(
        """() => {
            const spacer = document.createElement('div');
            spacer.style.height = (1200 + window.innerHeight) + 'px';
            document.querySelector('main').appendChild(spacer);
            window.scrollTo({top: 1200, left: 0, behavior: 'instant'});
        }"""
    )
    page.wait_for_function("() => window.scrollY === 1200")
    assert _box(page, "nav.navbar") == before
    assert before["y"] == 0


def _expect_new_tab_link(page: Page, selector: str, href: str) -> None:
    link = page.locator(selector)
    expect(link).to_have_attribute("href", href)
    expect(link).to_have_attribute("target", "_blank")
    expect(link).to_have_attribute("rel", "noopener noreferrer")


@pytest.mark.e2e
class TestDesktop:
    @pytest.mark.parametrize("path", ["/", "/settings", "/automation"])
    def test_version_coffee_and_star_sit_in_one_tidy_row(self, dev_page: Page, dev_app: str, path: str) -> None:
        _open(dev_page, dev_app, NO_UPDATE, DESKTOP, path)

        expect(dev_page.locator("#navVersionText")).to_have_text("dev@a9c8177")
        _expect_new_tab_link(dev_page, "#navVersion", RELEASES)
        expect(dev_page.locator("#navVersion")).to_have_attribute(
            "aria-label", "Version dev@a9c8177. Release notes, opens in a new tab"
        )
        expect(dev_page.locator("#navVersionDot")).to_have_count(0)

        _expect_new_tab_link(dev_page, "#sponsorLinkBtn", KOFI)
        expect(dev_page.locator("#sponsorLinkBtn")).to_have_attribute("aria-label", "Buy me a coffee on Ko-fi")
        expect(dev_page.locator("#sponsorLinkBtn")).to_have_attribute("title", "Buy me a coffee on Ko-fi")
        expect(dev_page.locator("#sponsorLinkBtn")).to_be_visible()

        _expect_new_tab_link(dev_page, "#navStarBtn", REPO)
        expect(dev_page.locator("#navStarBtn")).to_have_attribute(
            "aria-label", "Star Media Preview Generator on GitHub"
        )
        expect(dev_page.locator("#navStarBtn")).to_have_text("Star", use_inner_text=True)

        # The version sits under the wordmark, inside the bar, which stays one 57px row.
        nav, brand, version = (
            _box(dev_page, "nav.navbar"),
            _box(dev_page, ".navbar-brand"),
            _box(dev_page, "#navVersion"),
        )
        assert nav["height"] == 57
        assert version["x"] > brand["x"] + 26
        assert version["y"] >= brand["y"] + brand["height"] - 1
        assert version["y"] + version["height"] <= nav["height"]
        _no_sideways_scroll(dev_page, DESKTOP)
        _stays_pinned(dev_page)

    @pytest.mark.parametrize("font", FONTS)
    @pytest.mark.parametrize("width", [1200, 1280, 1400, 1440])
    def test_the_full_bar_is_one_row_with_room_to_spare(
        self, dev_page: Page, dev_app: str, width: int, font: str
    ) -> None:
        viewport = {"width": width, "height": 900}
        _open(dev_page, dev_app, NO_UPDATE, viewport, font=font)

        expect(dev_page.locator(".navbar-toggler")).to_be_hidden()
        assert _box(dev_page, "nav.navbar")["height"] == 57
        main_links = _box(dev_page, "#navbarNav .navbar-nav.me-auto")
        utilities = _box(dev_page, "#navbarNav .navbar-nav-utility")
        assert main_links["y"] == utilities["y"]
        # A clear margin, not a fit that one font wider tips into two rows.
        assert utilities["x"] - (main_links["x"] + main_links["width"]) >= 40
        logout = _box(dev_page, "#navLogoutBtn")
        assert logout["x"] + logout["width"] <= width
        # Below 1400px Star keeps its outline and the main links keep their words, dropping the extras.
        star_word = dev_page.locator("#navStarBtn .navbar-star-word")
        link_icon = dev_page.locator("#navbarNav .navbar-nav.me-auto .nav-link > .bi").first
        if width >= 1400:
            expect(star_word).to_be_visible()
            expect(link_icon).to_be_visible()
        else:
            expect(star_word).to_be_hidden()
            expect(link_icon).to_be_hidden()
        expect(dev_page.locator("#navStarBtn")).to_be_visible()
        _no_sideways_scroll(dev_page, viewport)
        _stays_pinned(dev_page)

    @pytest.mark.parametrize("font", FONTS)
    @pytest.mark.parametrize("width", [992, 1100])
    def test_below_1200_the_links_move_into_the_menu(self, dev_page: Page, dev_app: str, width: int, font: str) -> None:
        viewport = {"width": width, "height": 900}
        _open(dev_page, dev_app, NO_UPDATE, viewport, font=font)

        expect(dev_page.locator(".navbar-toggler")).to_be_visible()
        expect(dev_page.locator("#navStarBtn")).to_be_hidden()
        assert _box(dev_page, "nav.navbar")["height"] == 61
        _no_sideways_scroll(dev_page, viewport)

        dev_page.locator(".navbar-toggler").click()
        expect(dev_page.locator("#navbarNav.show")).to_be_visible()
        expect(dev_page.locator("#navStarBtn")).to_have_text("Star on GitHub", use_inner_text=True)
        expect(dev_page.locator("#sponsorLinkBtn")).to_have_text("Buy me a coffee", use_inner_text=True)

    def test_help_menu_leaves_the_coffee_link_to_the_navbar_cup(self, dev_page: Page, dev_app: str) -> None:
        _open(dev_page, dev_app, NO_UPDATE, DESKTOP)
        dev_page.locator("#helpMenuBtn").click()

        menu = dev_page.locator('.dropdown-menu[aria-labelledby="helpMenuBtn"]')
        expect(menu.locator("a", has_text="GitHub repository")).to_be_visible()
        expect(menu.locator(f'a[href="{KOFI}"]')).to_have_count(0)
        expect(menu.locator('a[href*="sponsors"]')).to_have_count(0)
        # The divider that set the coffee row off still separates the repo link, so it isn't stray.
        expect(menu.locator("li:last-child a")).to_have_text("GitHub repository")
        expect(menu.locator("li:nth-last-child(2) hr.dropdown-divider")).to_have_count(1)


@pytest.mark.e2e
class TestUpdateDot:
    @pytest.mark.parametrize(
        "check,label,note",
        [
            (DEV_UPDATE, "dev@a9c8177", "Version dev@896c569 is available"),
            (RELEASE_UPDATE, "v3.4.0", "Version 3.4.1 is available"),
        ],
        ids=["dev-image", "release"],
    )
    def test_an_available_update_shows_a_dot_with_the_new_version(
        self, dev_page: Page, dev_app: str, check: dict, label: str, note: str
    ) -> None:
        _open(dev_page, dev_app, check, DESKTOP)

        expect(dev_page.locator("#navVersionText")).to_have_text(label)
        expect(dev_page.locator("#navVersionDot")).to_be_visible()
        expect(dev_page.locator("#navVersion")).to_have_attribute(
            "aria-label", f"Version {label}. {note}, opens in a new tab"
        )
        dev_page.locator("#navVersion").hover()
        expect(dev_page.locator(".tooltip.show")).to_have_text(note)


@pytest.mark.e2e
class TestPhone:
    @pytest.mark.parametrize("font", FONTS)
    @pytest.mark.parametrize("width", [320, 360, 390])
    def test_brand_version_and_menu_button_share_one_row(
        self, dev_page: Page, dev_app: str, width: int, font: str
    ) -> None:
        viewport = {"width": width, "height": 844}
        _open(dev_page, dev_app, NO_UPDATE, viewport, font=font)

        nav, toggler = _box(dev_page, "nav.navbar"), _box(dev_page, ".navbar-toggler")
        assert nav["height"] == 61
        assert toggler["x"] + toggler["width"] <= width
        assert toggler["y"] + toggler["height"] <= nav["height"]
        expect(dev_page.locator("#navVersionText")).to_be_visible()
        _no_sideways_scroll(dev_page, viewport)

    @pytest.mark.parametrize("font", FONTS)
    def test_version_on_the_bar_and_coffee_and_star_in_the_menu(self, dev_page: Page, dev_app: str, font: str) -> None:
        _open(dev_page, dev_app, NO_UPDATE, PHONE, font=font)

        expect(dev_page.locator("#navVersionText")).to_be_visible()
        expect(dev_page.locator("#navVersionText")).to_have_text("dev@a9c8177")
        nav, version = _box(dev_page, "nav.navbar"), _box(dev_page, "#navVersion")
        assert version["y"] + version["height"] <= nav["height"]
        expect(dev_page.locator("#navStarBtn")).to_be_hidden()
        _no_sideways_scroll(dev_page, PHONE)
        _stays_pinned(dev_page)

        dev_page.locator(".navbar-toggler").click()
        expect(dev_page.locator("#navbarNav.show")).to_be_visible()

        star = dev_page.locator("#navStarBtn")
        expect(star).to_be_visible()
        expect(star).to_have_text("Star on GitHub", use_inner_text=True)
        expect(star).to_have_attribute("href", REPO)
        coffee = dev_page.locator("#sponsorLinkBtn")
        expect(coffee).to_be_visible()
        expect(coffee).to_have_text("Buy me a coffee", use_inner_text=True)
        expect(coffee).to_have_attribute("href", KOFI)
        # notifications.js fills the bell row's label; it once found it by its breakpoint class.
        expect(dev_page.locator("#notificationBellLabel")).to_have_text(
            re.compile(r"^(No new notifications|1 notification|\d+ notifications)$")
        )
        drawer = _box(dev_page, "#navbarNav")
        for selector in ("#navStarBtn", "#sponsorLinkBtn"):
            row = _box(dev_page, selector)
            assert drawer["x"] <= row["x"] and row["x"] + row["width"] <= drawer["x"] + drawer["width"]
        _no_sideways_scroll(dev_page, PHONE)
