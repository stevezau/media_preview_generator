"""E2E: Tools → Intro & Credits is its own menu entry that opens the Inspector on its Intro & Credits tab."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from ._mocks import mock_servers_list
from .test_preview_inspector import _mock_inspector_defaults


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


def _open_tools_menu(page: Page) -> None:
    page.locator("#navToolsDropdown").click()


@pytest.mark.e2e
class TestToolsMenuEntry:
    def test_menu_lists_intro_and_credits_beside_the_preview_inspector(self, authed_page: Page, app_url: str) -> None:
        mock_servers_list(authed_page, [])
        _mock_inspector_defaults(authed_page)
        authed_page.goto(f"{app_url}/bif-viewer")
        _open_tools_menu(authed_page)

        items = authed_page.locator("#navToolsDropdown + .dropdown-menu .dropdown-item")
        labels = [t.strip() for t in items.all_inner_texts()]
        # Its own entry, straight after the Preview Inspector it shares a page with. Other menu entries may change.
        assert labels.index("Intro & Credits") == labels.index("Preview Inspector") + 1
        hrefs = {label: items.nth(i).get_attribute("href") for i, label in enumerate(labels)}
        assert hrefs["Preview Inspector"] == "/bif-viewer"
        assert hrefs["Intro & Credits"] == "/bif-viewer?tab=markers"

    def test_the_menu_entry_opens_the_intro_and_credits_tab_with_its_own_heading(
        self, authed_page: Page, app_url: str
    ) -> None:
        mock_servers_list(authed_page, [])
        _mock_inspector_defaults(authed_page)
        authed_page.goto(f"{app_url}/bif-viewer")
        _open_tools_menu(authed_page)
        authed_page.get_by_role("link", name="Intro & Credits").click()

        expect(authed_page).to_have_url(f"{app_url}/bif-viewer?tab=markers")
        expect(authed_page.locator("h1.page-title")).to_have_text("Intro & Credits")
        expect(authed_page).to_have_title("Intro & Credits - Media Preview Generator")
        expect(authed_page.locator("#inspectorMarkersTabBtn")).to_have_class(re.compile(r"\bactive\b"))
        expect(authed_page.locator("#inspector-tab-markers")).to_have_class(re.compile(r"\bactive\b"))
        expect(authed_page.locator("#inspector-tab-frames")).not_to_have_class(re.compile(r"\bactive\b"))
        expect(authed_page.locator("#emptyState")).to_contain_text("to see its skip markers")

    def test_the_preview_inspector_entry_still_opens_the_frames_tab(self, authed_page: Page, app_url: str) -> None:
        mock_servers_list(authed_page, [])
        _mock_inspector_defaults(authed_page)
        authed_page.goto(f"{app_url}/bif-viewer")

        expect(authed_page.locator("h1.page-title")).to_have_text("Preview Inspector")
        expect(authed_page).to_have_title("Preview Inspector - Media Preview Generator")
        expect(authed_page.locator("#inspector-tab-frames")).to_have_class(re.compile(r"\bactive\b"))
        expect(authed_page.locator("#inspector-tab-markers")).not_to_have_class(re.compile(r"\bactive\b"))
        expect(authed_page.locator("#emptyState")).to_contain_text("to inspect its preview thumbnails")

    @pytest.mark.parametrize(
        ("path", "active_entry"),
        [("/bif-viewer", "Preview Inspector"), ("/bif-viewer?tab=markers", "Intro & Credits")],
    )
    def test_only_the_entry_for_the_open_tab_is_highlighted(
        self, authed_page: Page, app_url: str, path: str, active_entry: str
    ) -> None:
        mock_servers_list(authed_page, [])
        _mock_inspector_defaults(authed_page)
        authed_page.goto(f"{app_url}{path}")
        _open_tools_menu(authed_page)

        active = authed_page.locator("#navToolsDropdown + .dropdown-menu .dropdown-item.active")
        assert [t.strip() for t in active.all_inner_texts()] == [active_entry]

    def test_an_unknown_tab_value_falls_back_to_the_frames_tab(self, authed_page: Page, app_url: str) -> None:
        mock_servers_list(authed_page, [])
        _mock_inspector_defaults(authed_page)
        authed_page.goto(f"{app_url}/bif-viewer?tab=nonsense")

        expect(authed_page.locator("h1.page-title")).to_have_text("Preview Inspector")
        expect(authed_page.locator("#inspector-tab-frames")).to_have_class(re.compile(r"\bactive\b"))


@pytest.mark.e2e
def test_opening_a_file_from_the_menu_entry_shows_its_markers_without_clicking_the_tab(
    authed_page: Page, app_url: str
) -> None:
    # The whole point of the entry: land on the markers, not on the frames tab you then have to leave.
    from .test_intro_credits_inspector import _Inspector, south_park

    _Inspector(authed_page, app_url, south_park())  # registers the page's mocked endpoints
    authed_page.goto(f"{app_url}/bif-viewer?tab=markers")
    authed_page.wait_for_function("() => document.querySelector('#serverSelect option[value=\"plex-1\"]')")
    authed_page.locator("#searchInput").fill("South Park")
    authed_page.locator("#searchBtn").click()
    authed_page.locator(".result-item").first.click()

    expect(authed_page.locator("#viewerPanel")).not_to_have_class(re.compile(r"\bd-none\b"), timeout=3000)
    expect(authed_page.locator("#inspector-tab-markers")).to_be_visible()
    expect(authed_page.locator("#markersInspectorBody > *")).not_to_have_count(0, timeout=5000)
    expect(authed_page.locator("#markersInspectorBody")).not_to_contain_text("Loading", timeout=3000)
