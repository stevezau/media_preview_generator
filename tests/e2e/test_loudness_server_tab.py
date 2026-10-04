"""E2E: Servers → Edit → "Loudness" tab (Plex only) and the Libraries tab's Loudness column (``loudness.library_ids``).

Reuses the Intro & Credits tab's page mocks: the server list and single-server GET, the captured PUT, and the markers
status the Edit dialog asks for on open. Save tests assert the ``loudness`` block the page sends.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from .test_intro_credits_server_tab import (
    _mock_server_page,
    _open_tab,
    _plex_markers_on,
    _plex_ready_details,
    _plex_server,
    _save_and_read_put,
    _status,
    _switch_tab,
    _vendor_server,
)


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


def _confirmed_plex(loudness: dict | None = None) -> dict:
    server = _plex_server(_plex_markers_on())
    # Plex names a TV library's kind "episode" (plexapi's METADATA_TYPE); the shared fixture says "show".
    server["libraries"] = [
        {**lib, "kind": "episode" if lib["kind"] == "show" else lib["kind"]} for lib in server["libraries"]
    ]
    server["loudness"] = loudness or {"enabled": False, "library_ids": None}
    return server


def _open(page: Page, app_url: str, server: dict) -> None:
    _mock_server_page(page, server, _status(server, "ready", details=_plex_ready_details()))
    _open_tab(page, app_url, server, tab="loudness")


def _column(page: Page):
    return page.locator("#editLibraryTable th.loudness-lib-col")


def _lib(page: Page, lib_id: str):
    return page.locator(f"#editLibraryList .loudness-lib-toggle[data-id='{lib_id}']")


@pytest.mark.e2e
class TestLoudnessTab:
    def test_switch_shows_the_column_with_movie_and_tv_defaults_and_saves(
        self, authed_page: Page, app_url: str
    ) -> None:
        page = authed_page
        _open(page, app_url, _confirmed_plex())
        expect(page.locator("#loudnessEnabled")).to_be_enabled()
        _switch_tab(page, "libraries")
        expect(_column(page)).to_be_hidden()
        _switch_tab(page, "loudness")
        page.locator("label[for='loudnessEnabled']").click()
        _switch_tab(page, "libraries")
        expect(_column(page)).to_be_visible()
        # Movie and TV libraries start on.
        for lib_id in ("1", "2", "3"):
            expect(_lib(page, lib_id)).to_be_checked()
        body = _save_and_read_put(page, "plex-1")
        assert body["loudness"] == {"enabled": True, "library_ids": None}

    def test_unticking_a_library_saves_an_explicit_choice(self, authed_page: Page, app_url: str) -> None:
        page = authed_page
        _open(page, app_url, _confirmed_plex({"enabled": True, "library_ids": None}))
        _switch_tab(page, "libraries")
        _lib(page, "3").click()
        body = _save_and_read_put(page, "plex-1")
        assert body["loudness"] == {"enabled": True, "library_ids": ["1", "2"]}

    def test_stored_choice_is_shown(self, authed_page: Page, app_url: str) -> None:
        page = authed_page
        _open(page, app_url, _confirmed_plex({"enabled": True, "library_ids": ["2"]}))
        _switch_tab(page, "libraries")
        expect(_lib(page, "2")).to_be_checked()
        expect(_lib(page, "1")).not_to_be_checked()

    def test_switch_is_independent_of_marker_write_confirmation(self, authed_page: Page, app_url: str) -> None:
        page = authed_page
        server = _plex_server()
        server["loudness"] = {"enabled": False, "library_ids": None}
        _open(page, app_url, server)
        expect(page.locator("#loudnessEnabled")).to_be_enabled()
        page.locator("label[for='loudnessEnabled']").click()
        body = _save_and_read_put(page, "plex-1")
        assert body["loudness"] == {"enabled": True, "library_ids": None}
        assert body["markers"]["enabled"] is False
        assert not body["markers"]["plex"].get("db_write_confirmed_at")

    def test_switch_explains_unsupported_helper(self, authed_page: Page, app_url: str) -> None:
        server = _plex_server()
        server["markers"]["plex"]["agent"] = {"enabled": True, "url": "http://helper:8765", "token": "****"}
        _open(authed_page, app_url, server)
        expect(authed_page.locator("#loudnessEnabled")).to_be_disabled()
        expect(authed_page.locator("#loudnessHelperHint")).to_be_visible()
        expect(authed_page.locator("#loudnessHelperHint")).to_contain_text("does not support loudness")

    @pytest.mark.parametrize("enabled", ["false", "true", 1, None])
    def test_invalid_stored_enable_is_displayed_and_saved_as_off(
        self, authed_page: Page, app_url: str, enabled
    ) -> None:
        _open(authed_page, app_url, _confirmed_plex({"enabled": enabled, "library_ids": None}))
        expect(authed_page.locator("#loudnessEnabled")).not_to_be_checked()
        body = _save_and_read_put(authed_page, "plex-1")
        assert body["loudness"] == {"enabled": False, "library_ids": None}

    def test_tab_is_hidden_for_jellyfin(self, authed_page: Page, app_url: str) -> None:
        page = authed_page
        server = _vendor_server("jellyfin", "jf-1")
        _mock_server_page(page, server, _status(server, "ready"))
        _open_tab(page, app_url, server, tab="general")
        expect(page.locator("#edit-tab-loudness")).to_be_hidden()

    def test_music_library_is_not_selectable(self, authed_page: Page, app_url: str) -> None:
        server = _confirmed_plex({"enabled": True, "library_ids": ["1", "music"]})
        server["libraries"].append(
            {"id": "music", "name": "Music", "kind": "track", "enabled": True, "remote_paths": []}
        )
        _open(authed_page, app_url, server)
        _switch_tab(authed_page, "libraries")
        music = authed_page.locator("#editLibraryList tr[data-lib-id='music'] .loudness-lib-cell")
        expect(music).to_contain_text("Not supported")
        expect(music.locator("input")).to_have_count(0)
        body = _save_and_read_put(authed_page, "plex-1")
        assert body["loudness"] == {"enabled": True, "library_ids": ["1"]}
