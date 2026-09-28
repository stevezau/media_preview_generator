"""E2E: the navbar's Automation and Settings hover menus (base.html, nav_menus.js).

On the full-width bar (1200px and up) each word is a link to its page, and hovering it opens a list of the page's
sections. Keyboard: Enter follows the link, ArrowDown / ArrowUp / Space open the list, Escape closes it. In the phone
menu a tap on the word expands the same list. Each list item lands on its section, under the pinned navbar, and is
marked as current while the URL points at it.

Layout runs in this machine's font and in DejaVu Sans, a wider font a Linux browser falls back to.
"""

from __future__ import annotations

import re
from collections.abc import Generator

import pytest
from playwright.sync_api import Locator, Page, expect

ACTIVE = re.compile(r"(^|\s)active(\s|$)")
DESKTOP = {"width": 1440, "height": 900}
PHONE = {"width": 390, "height": 844}
WIDE_FONT = '* { font-family: "DejaVu Sans", sans-serif !important; }'
FONTS = ["page-font", "wide-font"]

MENUS = {
    "automation": {
        "toggle": "#navAutomationDropdown",
        "list": "#navAutomationMenu",
        "page": "/automation",
        "items": [
            ("Triggers", "section-webhooks-overview", "Triggers: webhooks that start jobs as new media arrives"),
            ("Schedules", "section-schedules-list", "Schedules: recurring library scans"),
            ("Quiet Hours", "section-schedules-quiet-hours", "Quiet Hours: times of day when processing pauses"),
        ],
    },
    "settings": {
        "toggle": "#navSettingsDropdown",
        "list": "#navSettingsMenu",
        "page": "/settings",
        "items": [
            ("Processing Options", "section-processing", "Processing Options: jobs, GPUs, CPU workers and thumbnails"),
            ("Intro & Credits", "section-markers", "Intro & Credits: skip-intro and credits markers"),
            ("Logging", "section-logging", "Logging: log level, log files and job history"),
            ("Authentication", "section-auth", "Authentication: the token that signs in to this web app"),
            ("Backups", "section-backups", "Backups: earlier copies of the config files"),
            ("About", "section-about", "About: version and project links"),
        ],
    },
}
OTHER = {"automation": "settings", "settings": "automation"}
SECTION_LINKS = [(name, text, anchor) for name, menu in MENUS.items() for text, anchor, _ in menu["items"]]

# True once the section sits right under the pinned navbar (at the page's scroll-padding-top), or, for a section too
# near the end to get there, once the page is scrolled to its end with the section on screen.
_LANDED = """(id) => {
    const top = document.getElementById(id).getBoundingClientRect().top;
    const pad = parseFloat(getComputedStyle(document.documentElement).scrollPaddingTop) || 0;
    const atEnd = Math.ceil(window.scrollY + window.innerHeight) >= document.documentElement.scrollHeight - 1;
    return Math.abs(top - pad) <= 2 || (atEnd && top >= pad - 2 && top < window.innerHeight - 100);
}"""


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


@pytest.fixture
def nav_page(authed_page: Page) -> Generator[Page, None, None]:
    """The signed-in page, failing the test on any uncaught script error (a key reaching Bootstrap's handler throws)."""
    errors: list[str] = []
    authed_page.on("pageerror", lambda error: errors.append(str(error)))
    yield authed_page
    assert errors == []


def _open(page: Page, app_url: str, path: str = "/", viewport: dict = DESKTOP, font: str = "page-font") -> None:
    page.set_viewport_size(viewport)
    page.goto(f"{app_url}{path}")
    expect(page.locator("nav.navbar")).to_be_visible()
    if font == "wide-font":
        page.add_style_tag(content=WIDE_FONT)


def _box(page: Page, selector: str) -> dict:
    box = page.locator(selector).bounding_box()
    assert box is not None, f"{selector} has no box"
    return box


def _items(page: Page, name: str) -> Locator:
    return page.locator(f"{MENUS[name]['list']} .dropdown-item")


def _expect_open(page: Page, name: str) -> None:
    expect(page.locator(MENUS[name]["list"])).to_be_visible()
    expect(page.locator(MENUS[name]["toggle"])).to_have_attribute("aria-expanded", "true")


def _expect_closed(page: Page, name: str) -> None:
    expect(page.locator(MENUS[name]["list"])).to_be_hidden()
    expect(page.locator(MENUS[name]["toggle"])).to_have_attribute("aria-expanded", "false")


def _expect_landed(page: Page, anchor: str) -> None:
    page.wait_for_function(_LANDED, arg=anchor, timeout=10_000)
    nav = _box(page, "nav.navbar")
    assert _box(page, f"#{anchor}")["y"] >= nav["y"] + nav["height"]
    # And stays there while the page fills in from its API calls (which once pushed Settings sections off screen).
    page.wait_for_timeout(1000)
    assert page.evaluate(_LANDED, anchor)


def _expect_current(page: Page, name: str, anchor: str | None) -> None:
    """Only the item for ``anchor`` (none when None) is marked current."""
    for _, item_anchor, _ in MENUS[name]["items"]:
        item = page.locator(f'{MENUS[name]["list"]} .dropdown-item[data-anchor="{item_anchor}"]')
        if item_anchor == anchor:
            expect(item).to_have_class(ACTIVE)
            expect(item).to_have_attribute("aria-current", "location")
        else:
            expect(item).not_to_have_class(ACTIVE)
            expect(item).not_to_have_attribute("aria-current", "location")


@pytest.mark.e2e
class TestDesktopHover:
    @pytest.mark.parametrize("name", list(MENUS))
    def test_hovering_the_word_opens_its_sections_and_leaving_closes_them(
        self, nav_page: Page, app_url: str, name: str
    ) -> None:
        menu = MENUS[name]
        _open(nav_page, app_url)
        toggle = nav_page.locator(menu["toggle"])
        expect(toggle).to_have_attribute("href", menu["page"])
        expect(toggle).to_have_attribute("aria-haspopup", "true")
        expect(toggle).to_have_attribute("aria-controls", menu["list"].lstrip("#"))
        expect(toggle).not_to_have_attribute("data-bs-toggle", "dropdown")
        _expect_closed(nav_page, name)

        toggle.hover()

        _expect_open(nav_page, name)
        items = _items(nav_page, name)
        expect(items).to_have_text([text for text, _, _ in menu["items"]])
        assert [items.nth(i).get_attribute("href") for i in range(items.count())] == [
            f"{menu['page']}#{anchor}" for _, anchor, _ in menu["items"]
        ]
        assert [items.nth(i).get_attribute("aria-label") for i in range(items.count())] == [
            label for _, _, label in menu["items"]
        ]
        # The list hangs from the word, not somewhere else on the bar.
        word, box = _box(nav_page, menu["toggle"]), _box(nav_page, menu["list"])
        assert word["y"] + word["height"] <= box["y"] <= word["y"] + word["height"] + 12
        assert abs(box["x"] - word["x"]) <= 2
        _expect_closed(nav_page, OTHER[name])

        nav_page.mouse.move(700, 800)
        _expect_closed(nav_page, name)

    @pytest.mark.parametrize("name", list(MENUS))
    def test_the_list_opens_only_once_the_pointer_rests_on_the_word(
        self, nav_page: Page, app_url: str, name: str
    ) -> None:
        nav_page.clock.install()
        _open(nav_page, app_url)
        word = _box(nav_page, MENUS[name]["toggle"])
        x, y = word["x"] + word["width"] / 2, word["y"] + word["height"] / 2
        is_open = nav_page.locator(MENUS[name]["list"]).is_visible

        # Passing over the word on the way somewhere else.
        nav_page.mouse.move(x, y)
        nav_page.clock.run_for(60)
        nav_page.mouse.move(x, 1)
        nav_page.clock.run_for(1000)
        assert not is_open()

        nav_page.mouse.move(x, y)
        nav_page.clock.run_for(80)
        assert not is_open()
        nav_page.clock.run_for(40)
        assert is_open()

    @pytest.mark.parametrize("name", list(MENUS))
    def test_a_short_trip_off_the_list_does_not_close_it(self, nav_page: Page, app_url: str, name: str) -> None:
        nav_page.clock.install()
        _open(nav_page, app_url)
        word = _box(nav_page, MENUS[name]["toggle"])
        nav_page.mouse.move(word["x"] + word["width"] / 2, word["y"] + word["height"] / 2)
        nav_page.clock.run_for(150)
        is_open = nav_page.locator(MENUS[name]["list"]).is_visible
        assert is_open()
        box = _box(nav_page, MENUS[name]["list"])
        last = _items(nav_page, name).last.bounding_box()
        off_the_list = (box["x"] + box["width"] + 30, box["y"] + 20)

        # A sloppy diagonal: off the list's right edge for a moment, then back onto its last item.
        nav_page.mouse.move(*off_the_list)
        nav_page.clock.run_for(200)
        assert is_open()
        nav_page.mouse.move(last["x"] + 20, last["y"] + last["height"] / 2)
        nav_page.clock.run_for(1000)
        assert is_open()

        nav_page.mouse.move(*off_the_list)
        nav_page.clock.run_for(200)
        assert is_open()
        nav_page.clock.run_for(100)
        assert not is_open()

    def test_moving_to_the_other_word_swaps_the_lists(self, nav_page: Page, app_url: str) -> None:
        _open(nav_page, app_url)
        nav_page.locator(MENUS["automation"]["toggle"]).hover()
        _expect_open(nav_page, "automation")

        nav_page.locator(MENUS["settings"]["toggle"]).hover()

        _expect_open(nav_page, "settings")
        _expect_closed(nav_page, "automation")

    @pytest.mark.parametrize("name", list(MENUS))
    def test_clicking_the_word_opens_its_page(self, nav_page: Page, app_url: str, name: str) -> None:
        _open(nav_page, app_url)

        nav_page.locator(MENUS[name]["toggle"]).click()

        nav_page.wait_for_url(f"{app_url}{MENUS[name]['page']}")
        expect(nav_page.locator(MENUS[name]["toggle"])).to_have_class(ACTIVE)
        expect(nav_page.locator(MENUS[OTHER[name]]["toggle"])).not_to_have_class(ACTIVE)

    def test_hovering_leaves_focus_in_the_field_being_edited(self, nav_page: Page, app_url: str) -> None:
        _open(nav_page, app_url, "/settings")
        field = nav_page.locator("#webhookRetryCount")
        field.focus()

        nav_page.locator(MENUS["automation"]["toggle"]).hover()

        _expect_open(nav_page, "automation")
        expect(field).to_be_focused()

    def test_one_navbar_list_open_at_a_time(self, nav_page: Page, app_url: str) -> None:
        _open(nav_page, app_url)
        tools = nav_page.locator("#navToolsDropdown + .dropdown-menu")
        nav_page.locator("#navToolsDropdown").click()
        expect(tools).to_be_visible()

        nav_page.locator(MENUS["settings"]["toggle"]).hover()
        _expect_open(nav_page, "settings")
        expect(tools).to_be_hidden()

        nav_page.locator("#navToolsDropdown").click()
        expect(tools).to_be_visible()
        _expect_closed(nav_page, "settings")


@pytest.mark.e2e
class TestKeyboard:
    @pytest.mark.parametrize("name", list(MENUS))
    def test_arrow_keys_open_and_walk_the_list_and_escape_closes_it(
        self, nav_page: Page, app_url: str, name: str
    ) -> None:
        _open(nav_page, app_url)
        toggle = nav_page.locator(MENUS[name]["toggle"])
        items = _items(nav_page, name)
        toggle.focus()

        nav_page.keyboard.press("ArrowDown")
        _expect_open(nav_page, name)
        expect(items.first).to_be_focused()
        nav_page.keyboard.press("ArrowDown")
        expect(items.nth(1)).to_be_focused()
        nav_page.keyboard.press("End")
        expect(items.last).to_be_focused()
        nav_page.keyboard.press("ArrowDown")
        expect(items.first).to_be_focused()
        nav_page.keyboard.press("ArrowUp")
        expect(items.last).to_be_focused()
        nav_page.keyboard.press("Home")
        expect(items.first).to_be_focused()

        nav_page.keyboard.press("Escape")
        _expect_closed(nav_page, name)
        expect(toggle).to_be_focused()

        nav_page.keyboard.press("ArrowUp")
        _expect_open(nav_page, name)
        expect(items.last).to_be_focused()
        nav_page.keyboard.press("Escape")
        _expect_closed(nav_page, name)

    @pytest.mark.parametrize("name", list(MENUS))
    def test_space_opens_the_list_without_scrolling_the_page(self, nav_page: Page, app_url: str, name: str) -> None:
        _open(nav_page, app_url)
        nav_page.locator(MENUS[name]["toggle"]).focus()

        nav_page.keyboard.press("Space")

        _expect_open(nav_page, name)
        expect(_items(nav_page, name).first).to_be_focused()
        assert nav_page.evaluate("() => window.scrollY") == 0

    @pytest.mark.parametrize("name", list(MENUS))
    def test_enter_on_the_word_opens_its_page(self, nav_page: Page, app_url: str, name: str) -> None:
        _open(nav_page, app_url)
        nav_page.locator(MENUS[name]["toggle"]).focus()

        nav_page.keyboard.press("Enter")

        nav_page.wait_for_url(f"{app_url}{MENUS[name]['page']}")

    def test_enter_on_an_item_opens_its_section(self, nav_page: Page, app_url: str) -> None:
        _open(nav_page, app_url)
        nav_page.locator(MENUS["settings"]["toggle"]).focus()

        for key in ("ArrowDown", "ArrowDown", "ArrowDown", "Enter"):
            nav_page.keyboard.press(key)

        nav_page.wait_for_url(f"{app_url}/settings#section-logging")
        _expect_landed(nav_page, "section-logging")

    def test_tabbing_out_of_the_list_closes_it(self, nav_page: Page, app_url: str) -> None:
        _open(nav_page, app_url)
        nav_page.locator(MENUS["automation"]["toggle"]).focus()
        nav_page.keyboard.press("ArrowDown")
        _expect_open(nav_page, "automation")

        nav_page.keyboard.press("Shift+Tab")
        expect(nav_page.locator(MENUS["automation"]["toggle"])).to_be_focused()
        _expect_open(nav_page, "automation")
        nav_page.keyboard.press("Shift+Tab")

        _expect_closed(nav_page, "automation")


@pytest.mark.e2e
class TestSectionLinks:
    @pytest.mark.parametrize("name,text,anchor", SECTION_LINKS, ids=[a for _, _, a in SECTION_LINKS])
    def test_each_item_lands_on_its_section_and_is_marked_current(
        self, nav_page: Page, app_url: str, name: str, text: str, anchor: str
    ) -> None:
        menu = MENUS[name]
        _open(nav_page, app_url)
        nav_page.locator(menu["toggle"]).hover()
        _expect_open(nav_page, name)

        nav_page.locator(f"{menu['list']} .dropdown-item", has_text=text).click()

        nav_page.wait_for_url(f"{app_url}{menu['page']}#{anchor}")
        expect(nav_page.locator(f"#{anchor}")).to_be_visible()
        _expect_landed(nav_page, anchor)
        expect(nav_page.locator(menu["toggle"])).to_have_class(ACTIVE)
        _expect_current(nav_page, name, anchor)
        _expect_current(nav_page, OTHER[name], None)

    def test_automation_items_switch_tabs_on_the_page_itself(self, nav_page: Page, app_url: str) -> None:
        _open(nav_page, app_url, "/automation")
        expect(nav_page.locator("#pane-triggers")).to_be_visible()
        toggle = nav_page.locator(MENUS["automation"]["toggle"])

        toggle.hover()
        _items(nav_page, "automation").filter(has_text="Schedules").click()

        _expect_closed(nav_page, "automation")
        expect(nav_page.locator("#pane-schedules")).to_be_visible()
        expect(nav_page.locator("#pane-triggers")).to_be_hidden()
        assert nav_page.evaluate("() => window.location.hash") == "#section-schedules-list"
        _expect_landed(nav_page, "section-schedules-list")
        _expect_current(nav_page, "automation", "section-schedules-list")

        toggle.hover()
        _items(nav_page, "automation").filter(has_text="Triggers").click()

        expect(nav_page.locator("#pane-triggers")).to_be_visible()
        expect(nav_page.locator("#pane-schedules")).to_be_hidden()
        _expect_landed(nav_page, "section-webhooks-overview")
        _expect_current(nav_page, "automation", "section-webhooks-overview")

    def test_settings_items_scroll_the_page_itself(self, nav_page: Page, app_url: str) -> None:
        _open(nav_page, app_url, "/settings")
        _expect_current(nav_page, "settings", None)

        nav_page.locator(MENUS["settings"]["toggle"]).hover()
        _items(nav_page, "settings").filter(has_text="Backups").click()

        _expect_closed(nav_page, "settings")
        _expect_landed(nav_page, "section-backups")
        _expect_current(nav_page, "settings", "section-backups")

        # The page's own sidebar moves the highlight too.
        nav_page.locator('#settings-sidebar a[href="#section-logging"]').click()
        _expect_landed(nav_page, "section-logging")
        _expect_current(nav_page, "settings", "section-logging")

    def test_only_the_page_the_url_is_on_is_highlighted(self, nav_page: Page, app_url: str) -> None:
        _open(nav_page, app_url)
        for name in MENUS:
            expect(nav_page.locator(MENUS[name]["toggle"])).not_to_have_class(ACTIVE)
            _expect_current(nav_page, name, None)


@pytest.mark.e2e
class TestPhoneMenu:
    @pytest.mark.parametrize("font", FONTS)
    def test_both_words_expand_to_the_same_sections(self, nav_page: Page, app_url: str, font: str) -> None:
        _open(nav_page, app_url, viewport=PHONE, font=font)
        nav_page.locator(".navbar-toggler").click()
        expect(nav_page.locator("#navbarNav.show")).to_be_visible()
        drawer = _box(nav_page, "#navbarNav")

        for name in ("automation", "settings"):
            nav_page.locator(MENUS[name]["toggle"]).click()

            _expect_open(nav_page, name)
            _expect_closed(nav_page, OTHER[name])
            assert nav_page.url == f"{app_url}/"
            items = _items(nav_page, name)
            expect(items).to_have_text([text for text, _, _ in MENUS[name]["items"]])
            for i in range(items.count()):
                row = items.nth(i).bounding_box()
                assert drawer["x"] <= row["x"] and row["x"] + row["width"] <= drawer["x"] + drawer["width"]
            assert nav_page.evaluate("() => document.documentElement.scrollWidth") == PHONE["width"]

        nav_page.locator(MENUS["settings"]["toggle"]).click()
        _expect_closed(nav_page, "settings")
        expect(nav_page.locator("#navbarNav.show")).to_be_visible()

    @pytest.mark.parametrize(
        "name,text,anchor",
        [
            ("settings", "Authentication", "section-auth"),
            ("automation", "Quiet Hours", "section-schedules-quiet-hours"),
        ],
        ids=["settings", "automation"],
    )
    def test_a_section_closes_the_menu_and_lands_on_it(
        self, nav_page: Page, app_url: str, name: str, text: str, anchor: str
    ) -> None:
        _open(nav_page, app_url, viewport=PHONE)
        nav_page.locator(".navbar-toggler").click()
        expect(nav_page.locator("#navbarNav.show")).to_be_visible()
        nav_page.locator(MENUS[name]["toggle"]).click()

        _items(nav_page, name).filter(has_text=text).click()

        nav_page.wait_for_url(f"{app_url}{MENUS[name]['page']}#{anchor}")
        expect(nav_page.locator("#navbarNav")).to_be_hidden()
        _expect_landed(nav_page, anchor)


@pytest.mark.e2e
class TestLayout:
    @pytest.mark.parametrize("font", FONTS)
    @pytest.mark.parametrize("width", [1200, 1280, 1440])
    def test_open_lists_fit_the_window(self, nav_page: Page, app_url: str, width: int, font: str) -> None:
        viewport = {"width": width, "height": 900}
        _open(nav_page, app_url, viewport=viewport, font=font)
        expect(nav_page.locator(".navbar-toggler")).to_be_hidden()

        for name in MENUS:
            nav_page.locator(MENUS[name]["toggle"]).hover()
            _expect_open(nav_page, name)
            box = _box(nav_page, MENUS[name]["list"])
            assert 0 <= box["x"] and box["x"] + box["width"] <= width
            assert box["y"] + box["height"] <= viewport["height"]
            assert _box(nav_page, "nav.navbar")["height"] == 57
            assert nav_page.evaluate("() => document.documentElement.scrollWidth") == width
