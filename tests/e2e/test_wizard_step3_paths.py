"""E2E tests for wizard step 3: Plex config folder + path mappings.

Regressions covered:

* `#wizardPlexConfigFolder` validates inline (regression for the
  duplicate-id bug where validation silently no-op'd).
* Browse button opens the folder picker modal.
* "Add another mapping" appends a row with Browse + validation hooks.
* Path-mapping local input validates against `/api/settings/validate-local-path`.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, Route, expect

from ._mocks import (
    capture_settings_save,
    mock_browse_directories,
    mock_plex_libraries,
    mock_setup_status,
    mock_validate_local_path,
    mock_validate_plex_config_folder,
)


def _drive_to_step3(page: Page, app_url: str) -> None:
    """Walk through step 1 + 2 (mocked) to land on step 3."""
    page.goto(f"{app_url}/setup")
    page.wait_for_load_state("domcontentloaded")
    page.locator('.wizard-vendor-btn[data-vendor="plex"]').click()
    page.evaluate("document.getElementById('manualConnectDetails').open = true")
    page.locator("#manualPlexUrl").fill("http://plex.local:32400")
    page.locator("#manualPlexToken").fill("tok")
    page.locator("#manualPlexTestBtn").click()
    expect(page.locator("#manualPlexResult")).to_contain_text("Connected", timeout=5000)
    expect(page.locator("#step1Next")).to_be_visible()
    expect(page.locator("#step1Next")).to_be_enabled()
    page.locator("#step1Next").click()
    expect(page.locator('div.setup-step[data-step="2"]')).to_have_class("setup-step active")
    # Tick the first library so step 2's Next enables.
    page.locator(".library-card").first.click()
    page.locator("#step2Next").click()
    expect(page.locator('div.setup-step[data-step="3"]')).to_have_class("setup-step active")


@pytest.mark.e2e
class TestPlexConfigFolderValidation:
    def test_valid_path_paints_is_valid(self, wizard_page: Page, app_url_wizard: str) -> None:
        mock_plex_libraries(wizard_page)
        capture_settings_save(wizard_page)
        mock_setup_status(wizard_page, complete=False)
        mock_validate_plex_config_folder(wizard_page, valid=True)
        _drive_to_step3(wizard_page, app_url_wizard)

        cfg = wizard_page.locator("#wizardPlexConfigFolder")
        cfg.fill("/plex")
        # Validator is debounced 400ms; wait it out + a buffer.
        wizard_page.wait_for_timeout(700)
        expect(cfg).to_have_class("form-control is-valid")

    def test_invalid_path_paints_is_invalid_with_error(self, wizard_page: Page, app_url_wizard: str) -> None:
        mock_plex_libraries(wizard_page)
        capture_settings_save(wizard_page)
        mock_setup_status(wizard_page, complete=False)
        mock_validate_plex_config_folder(wizard_page, valid=False, error="Folder not found")
        _drive_to_step3(wizard_page, app_url_wizard)

        cfg = wizard_page.locator("#wizardPlexConfigFolder")
        cfg.fill("/nope")
        wizard_page.wait_for_timeout(700)
        expect(cfg).to_have_class("form-control is-invalid")
        # The error message lands in the sibling .invalid-feedback.
        feedback = cfg.locator("..").locator(".invalid-feedback")
        expect(feedback).to_contain_text("Folder not found")


@pytest.mark.e2e
class TestPathMappingRows:
    def test_browse_button_opens_folder_picker(self, wizard_page: Page, app_url_wizard: str) -> None:
        mock_plex_libraries(wizard_page)
        capture_settings_save(wizard_page)
        mock_setup_status(wizard_page, complete=False)
        mock_validate_plex_config_folder(wizard_page, valid=True)
        mock_browse_directories(wizard_page)
        _drive_to_step3(wizard_page, app_url_wizard)

        wizard_page.locator("#wizardPlexConfigFolderBrowseBtn").click()
        # folder_picker.js lazily injects #folderPickerModal on first open.
        expect(wizard_page.locator("#folderPickerModal")).to_be_visible(timeout=2000)

    def test_add_mapping_row_appends_row_with_browse_and_feedback(self, wizard_page: Page, app_url_wizard: str) -> None:
        mock_plex_libraries(wizard_page)
        capture_settings_save(wizard_page)
        mock_setup_status(wizard_page, complete=False)
        mock_validate_plex_config_folder(wizard_page, valid=True)
        _drive_to_step3(wizard_page, app_url_wizard)

        # Step 3 entry calls settingsManager.get() which renders one default
        # empty row. Wait for that to render before counting.
        first_row = wizard_page.locator("#setupPathMappingsContainer .path-mapping-row").first
        expect(first_row).to_be_visible(timeout=2000)
        rows_before = wizard_page.locator("#setupPathMappingsContainer .path-mapping-row").count()
        wizard_page.locator("#setupAddPathMappingBtn").click()
        rows_after = wizard_page.locator("#setupPathMappingsContainer .path-mapping-row").count()
        assert rows_after == rows_before + 1
        # Newest row has the browse button + feedback divs.
        last_row = wizard_page.locator("#setupPathMappingsContainer .path-mapping-row").last
        expect(last_row.locator(".path-mapping-browse")).to_be_visible()
        expect(last_row.locator(".invalid-feedback")).to_be_attached()
        expect(last_row.locator(".valid-feedback")).to_be_attached()

    def test_local_path_invalid_paints_red(self, wizard_page: Page, app_url_wizard: str) -> None:
        mock_plex_libraries(wizard_page)
        capture_settings_save(wizard_page)
        mock_setup_status(wizard_page, complete=False)
        mock_validate_plex_config_folder(wizard_page, valid=True)
        mock_validate_local_path(wizard_page, exists=False, error="Directory not found")
        _drive_to_step3(wizard_page, app_url_wizard)

        local_input = wizard_page.locator("#setupPathMappingsContainer .path-mapping-row .path-mapping-local").first
        local_input.fill("/nope/not/here")
        wizard_page.wait_for_timeout(700)
        expect(local_input).to_have_class("form-control form-control-sm path-mapping-local is-invalid")


@pytest.mark.e2e
class TestPathMappingPrefixKeys:
    @pytest.mark.parametrize(
        "saved_row",
        [
            pytest.param({"remote_prefix": "/media/current"}, id="remote_prefix_only"),
            pytest.param({"plex_prefix": "/media/current"}, id="plex_prefix_only"),
            pytest.param({"remote_prefix": "/media/current", "plex_prefix": "/media/stale"}, id="both_remote_wins"),
        ],
    )
    def test_saved_row_shows_remote_prefix_and_next_saves_edit_to_both_keys(
        self, wizard_page: Page, app_url_wizard: str, saved_row: dict
    ) -> None:
        """Step 3 renders the saved Plex rows raw and shows ``remote_prefix``, else legacy ``plex_prefix``.

        Reading only ``plex_prefix`` showed the stale value (or a blank box for a
        ``remote_prefix``-only row, which Next then dropped).
        """
        mock_plex_libraries(wizard_page)
        mock_setup_status(wizard_page, complete=False)
        mock_validate_plex_config_folder(wizard_page, valid=True)
        mock_validate_local_path(wizard_page, exists=True)
        posted: list[dict] = []

        def settings_handler(route: Route) -> None:
            if route.request.method == "GET":
                body = {
                    "path_mappings": [{**saved_row, "local_prefix": "/tmp"}],
                    "exclude_paths": [],
                    "media_servers": [],
                }
            else:
                posted.append(route.request.post_data_json or {})
                body = {"success": True}
            route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

        wizard_page.route("**/api/settings", settings_handler)
        _drive_to_step3(wizard_page, app_url_wizard)

        remote_input = wizard_page.locator("#setupPathMappingsContainer .path-mapping-row .path-mapping-plex").first
        expect(remote_input).to_have_value("/media/current", timeout=5000)

        remote_input.fill("/media/edited")
        wizard_page.locator("#wizardPlexConfigFolder").fill("/plex")
        wizard_page.locator("#step3Next").click()
        expect(wizard_page.locator('div.setup-step[data-step="4"]')).to_have_class("setup-step active", timeout=5000)

        saved = [body for body in posted if "path_mappings" in body]
        assert len(saved) == 1, posted
        assert saved[0]["path_mappings"] == [
            {
                "remote_prefix": "/media/edited",
                "plex_prefix": "/media/edited",
                "local_prefix": "/tmp",
                "webhook_prefixes": [],
            }
        ]
