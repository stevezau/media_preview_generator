"""E2E: the top navbar stays pinned to the top of the window while a page scrolls, under every overlay.

``nav.navbar.sticky-top { position: relative }`` in style.css once outranked Bootstrap's ``.sticky-top`` and the
navbar scrolled away with the page on every screen. Each page here gets a tall spacer so it scrolls the same distance
whatever the test data holds, then the navbar's box is checked to the pixel. The overlay tests pin the other side:
modals, the phone menu, navbar dropdowns, tooltips, toasts and the Inspector's big frame still sit above it.
Setup Health lives in the server editor's modal, so the modal test covers it.
"""

from __future__ import annotations

from urllib.parse import quote

import pytest
from playwright.sync_api import Page, expect

from . import _inspector_fixtures as fx

DESKTOP = {"width": 1440, "height": 900}
PHONE = {"width": 390, "height": 844}
SCROLL_PX = 1200

PAGES = {
    "dashboard": "/",
    "servers": "/servers",
    "automation": "/automation",
    "settings": "/settings",
    "logs": "/logs",
    "webhook-activity": "/webhook-activity",
    "inspector-search": "/inspector",
    "inspector-file": f"/inspector?path={quote(fx.EPISODE)}",
}

# Returns whether the topmost element at (x, y) sits inside the element matching ``selector``.
_HIT_INSIDE = """([selector, x, y]) => {
    const hit = document.elementFromPoint(x, y);
    const target = document.querySelector(selector);
    return !!(hit && target && (hit === target || target.contains(hit)));
}"""


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


def _open(page: Page, app_url: str, name: str) -> None:
    if name.startswith("inspector"):
        fx.install(page)
    page.goto(f"{app_url}{PAGES[name]}")
    if name == "inspector-file":
        expect(page.locator("#inspLoading")).to_have_count(0, timeout=10_000)
    expect(page.locator("nav.navbar")).to_be_visible()


def _scroll_down(page: Page, px: int = SCROLL_PX) -> None:
    """Make the page tall enough to scroll ``px`` and scroll there instantly (some pages set smooth scrolling)."""
    page.evaluate(
        """(px) => {
            const spacer = document.createElement('div');
            spacer.style.height = (px + window.innerHeight) + 'px';
            document.querySelector('main').appendChild(spacer);
            window.scrollTo({top: px, left: 0, behavior: 'instant'});
        }""",
        px,
    )
    page.wait_for_function("(px) => window.scrollY === px", arg=px)


def _nav_box(page: Page) -> dict:
    box = page.locator("nav.navbar").bounding_box()
    assert box is not None
    return box


@pytest.mark.e2e
@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone"])
@pytest.mark.parametrize("name", list(PAGES))
def test_navbar_stays_at_the_top_when_the_page_scrolls(
    authed_page: Page, app_url: str, name: str, viewport: dict
) -> None:
    authed_page.set_viewport_size(viewport)
    _open(authed_page, app_url, name)
    before = _nav_box(authed_page)
    main_top_before = authed_page.locator("main").bounding_box()["y"]
    assert before == {"x": 0, "y": 0, "width": viewport["width"], "height": before["height"]}
    assert before["height"] >= 50

    _scroll_down(authed_page)

    assert authed_page.locator("main").bounding_box()["y"] == main_top_before - SCROLL_PX
    assert _nav_box(authed_page) == before
    # Nothing on the page paints over it.
    assert authed_page.evaluate(_HIT_INSIDE, ["nav.navbar", 5, before["height"] / 2])
    # A pinned navbar must not bring back sideways scrolling at phone width.
    assert authed_page.evaluate("() => document.documentElement.scrollWidth") == viewport["width"]
    if viewport == PHONE:
        # No frosted blur below xl (it would trap the menu drawer), so the bar is opaque or the page shows through.
        bg = authed_page.evaluate("() => getComputedStyle(document.querySelector('nav.navbar')).backgroundColor")
        assert bg == "rgb(15, 15, 26)"


@pytest.mark.e2e
class TestOverlaysStayAboveThePinnedNavbar:
    def test_a_modal_covers_the_navbar(self, authed_page: Page, app_url: str) -> None:
        authed_page.set_viewport_size(DESKTOP)
        _open(authed_page, app_url, "settings")
        _scroll_down(authed_page)
        nav = _nav_box(authed_page)

        authed_page.evaluate("() => { window.appConfirm('Scrolled confirm'); }")
        expect(authed_page.locator("#appConfirmModal.show")).to_be_visible()

        assert authed_page.evaluate(_HIT_INSIDE, ["#appConfirmModal", 5, nav["height"] / 2])

    def test_the_phone_menu_opens_full_height_over_the_page(self, authed_page: Page, app_url: str) -> None:
        authed_page.set_viewport_size(PHONE)
        _open(authed_page, app_url, "settings")
        _scroll_down(authed_page)

        authed_page.locator(".navbar-toggler").click()
        drawer = authed_page.locator("#navbarNav.show")
        expect(drawer).to_be_visible()
        authed_page.wait_for_function(
            "() => { const r = document.getElementById('navbarNav').getBoundingClientRect(); return r.right === innerWidth; }"
        )

        box = drawer.bounding_box()
        assert box["y"] == 0
        assert box["height"] == PHONE["height"]
        assert box["x"] + box["width"] == PHONE["width"]
        assert authed_page.evaluate(_HIT_INSIDE, ["#navbarNav", box["x"] + box["width"] / 2, PHONE["height"] / 2])

    def test_a_navbar_dropdown_opens_on_top_of_the_page(self, authed_page: Page, app_url: str) -> None:
        authed_page.set_viewport_size(DESKTOP)
        _open(authed_page, app_url, "settings")
        _scroll_down(authed_page)

        authed_page.locator("#navToolsDropdown").click()
        menu = authed_page.locator("#navToolsDropdown + .dropdown-menu")
        expect(menu).to_be_visible()

        box = menu.bounding_box()
        assert box["y"] < _nav_box(authed_page)["height"] + 10
        assert authed_page.evaluate(
            _HIT_INSIDE, ["#navToolsDropdown + .dropdown-menu", box["x"] + 20, box["y"] + box["height"] - 12]
        )

    def test_a_tooltip_over_the_navbar_shows_above_it(self, authed_page: Page, app_url: str) -> None:
        authed_page.set_viewport_size(DESKTOP)
        _open(authed_page, app_url, "settings")
        _scroll_down(authed_page)
        nav = _nav_box(authed_page)

        # A trigger just under the navbar: the tooltip's default "top" placement lands over the navbar.
        authed_page.evaluate(
            """(top) => {
                const b = document.createElement('button');
                b.id = 'e2eTipTrigger';
                b.textContent = 'tip';
                b.setAttribute('title', 'A tooltip over the navbar');
                b.style.cssText = 'position:fixed;left:300px;top:' + top + 'px;';
                document.body.appendChild(b);
                bootstrap.Tooltip.getOrCreateInstance(b, {animation: false}).show();
            }""",
            nav["height"] + 8,
        )
        tip = authed_page.locator(".tooltip.show")
        expect(tip).to_be_visible()

        box = tip.bounding_box()
        assert box["y"] < nav["height"]
        assert authed_page.evaluate(_HIT_INSIDE, [".tooltip.show", box["x"] + box["width"] / 2, box["y"] + 4])

    def test_toasts_stack_above_the_navbar(self, authed_page: Page, app_url: str) -> None:
        authed_page.set_viewport_size(DESKTOP)
        _open(authed_page, app_url, "settings")
        z = authed_page.evaluate(
            """() => [getComputedStyle(document.querySelector('nav.navbar')).zIndex,
                      getComputedStyle(document.querySelector('.toast-container')).zIndex].map(Number)"""
        )
        assert z[1] > z[0]

    def test_the_inspector_big_frame_covers_the_navbar(self, authed_page: Page, app_url: str) -> None:
        authed_page.set_viewport_size(DESKTOP)
        api = fx.InspectorApi()
        film, item = fx.checked_film()
        api.add(film, item, fx.default_kinds(film["canonical_path"]))
        fx.install(authed_page, api)
        authed_page.goto(f"{app_url}/inspector?path={quote(fx.FILM)}")
        expect(authed_page.locator("#inspLoading")).to_have_count(0, timeout=10_000)
        _scroll_down(authed_page, 300)
        nav = _nav_box(authed_page)
        assert nav["y"] == 0

        authed_page.locator("#inspStrip .insp-tl-frame.is-now .insp-tl-img").click()
        expect(authed_page.locator("#inspFrameDialog")).to_be_visible()

        assert authed_page.evaluate(_HIT_INSIDE, ["#inspFrameDialog", 5, nav["height"] / 2])
