"""Real browser/form/API/disk round-trip for independent loudness opt-in."""

import json
from pathlib import Path

import pytest
from playwright.sync_api import expect

from .test_journey_edit_existing_server import _seeded_server


def _server() -> dict:
    server = _seeded_server()
    server["markers"] = {"enabled": False, "plex": {"db_write_confirmed_at": None}}
    server["output"]["chapter_thumbnails"] = True
    return server


@pytest.mark.e2e
@pytest.mark.parametrize("backend_real_app", [{"media_servers": [_server()]}], indirect=True)
def test_loudness_toggle_persists_without_marker_consent_or_changing_chapters(backend_real_app, backend_real_page):
    url, config_dir = backend_real_app
    page = backend_real_page
    for previous, desired in [(False, True), (True, False)]:
        page.goto(url + "/servers")
        page.locator(".edit-server-btn[data-id='plex-edit-test']").click()
        expect(page.locator("#editServerModal")).to_be_visible()
        page.locator("[data-bs-target='#edit-tab-processing']").click()
        toggle = page.locator("#loudnessEnabled")
        expect(toggle).to_be_enabled()
        expect(toggle).to_be_checked(checked=previous)
        toggle.set_checked(desired)
        page.locator("#editServerSave").click()
        expect(page.locator("#editServerModal")).to_be_hidden(timeout=10000)
        saved = json.loads((Path(config_dir) / "settings.json").read_text())["media_servers"][0]
        assert saved["loudness"]["enabled"] is desired
        assert saved["markers"]["enabled"] is False
        assert not saved["markers"]["plex"].get("db_write_confirmed_at")
        assert saved["output"]["chapter_thumbnails"] is True
