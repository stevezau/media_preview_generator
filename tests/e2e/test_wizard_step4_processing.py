"""E2E tests for wizard step 4: per-GPU panel + steppers + CPU workers.

Regressions covered:

* Per-GPU panel renders one card per detected GPU.
* `+`/`−` stepper buttons mutate the input value and respect min/max.
* Disabling a GPU greys out its workers/threads cells.
* Re-scan GPUs button POSTs `/api/system/rescan-gpus`.
* Thumbnail Quality copy states the qscale direction (issue #286).
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from ._mocks import (
    capture_settings_save,
    mock_plex_libraries,
    mock_settings_get,
    mock_setup_status,
    mock_system_rescan_gpus,
    mock_system_status,
    mock_validate_plex_config_folder,
    mock_worker_groups,
)


def _drive_to_step4(page: Page, app_url: str) -> None:
    mock_worker_groups(page)
    page.goto(f"{app_url}/setup")
    page.wait_for_load_state("domcontentloaded")
    page.locator('.wizard-vendor-btn[data-vendor="plex"]').click()
    page.evaluate("document.getElementById('manualConnectDetails').open = true")
    page.locator("#manualPlexUrl").fill("http://plex.local:32400")
    page.locator("#manualPlexToken").fill("tok")
    page.locator("#manualPlexTestBtn").click()
    expect(page.locator("#manualPlexResult")).to_contain_text("Connected", timeout=5000)
    # ``to_be_visible`` ahead of the click surfaces a clearer error than
    # a 30s ``element is not stable`` timeout if step 1 ever regresses
    # to async layout shift around ``#step1Next``.
    expect(page.locator("#step1Next")).to_be_visible()
    expect(page.locator("#step1Next")).to_be_enabled()
    page.locator("#step1Next").click()
    page.locator(".library-card").first.click()
    page.locator("#step2Next").click()
    expect(page.locator('div.setup-step[data-step="3"]')).to_have_class("setup-step active")
    page.locator("#wizardPlexConfigFolder").fill("/plex")
    page.locator("#step3Next").click()
    expect(page.locator('div.setup-step[data-step="4"]')).to_have_class("setup-step active")


@pytest.mark.e2e
class TestPerGpuPanel:
    def test_renders_card_per_detected_gpu(self, wizard_page: Page, app_url_wizard: str) -> None:
        mock_plex_libraries(wizard_page)
        capture_settings_save(wizard_page)
        mock_setup_status(wizard_page, complete=False)
        mock_validate_plex_config_folder(wizard_page, valid=True)
        mock_settings_get(wizard_page)
        mock_system_status(
            wizard_page,
            gpus=[
                {"type": "nvidia", "device": "/dev/nvidia0", "name": "GPU 0", "status": "ok"},
                {"type": "vaapi", "device": "/dev/dri/renderD128", "name": "iGPU", "status": "ok"},
            ],
        )
        _drive_to_step4(wizard_page, app_url_wizard)
        # Wait for the "Detecting GPUs..." spinner to be hidden.
        expect(wizard_page.locator("#gpuDetecting")).to_be_hidden()
        cards = wizard_page.locator("#gpuConfigList .gpu-tuning-threads")
        expect(cards).to_have_count(2)

    def test_device_tuning_changes_threads(self, wizard_page: Page, app_url_wizard: str) -> None:
        mock_plex_libraries(wizard_page)
        capture_settings_save(wizard_page)
        mock_setup_status(wizard_page, complete=False)
        mock_validate_plex_config_folder(wizard_page, valid=True)
        mock_settings_get(wizard_page)
        mock_system_status(wizard_page)  # default: 2 GPUs
        _drive_to_step4(wizard_page, app_url_wizard)
        expect(wizard_page.locator("#gpuDetecting")).to_be_hidden()

        tuning = wizard_page.locator(".gpu-tuning-threads").first
        expect(tuning).to_have_value("2")
        tuning.fill("3")
        assert wizard_page.evaluate("collectGpuConfig()[0].ffmpeg_threads") == 3

    def test_zero_group_count_is_rejected(self, wizard_page: Page, app_url_wizard: str) -> None:
        mock_plex_libraries(wizard_page)
        capture_settings_save(wizard_page)
        mock_setup_status(wizard_page, complete=False)
        mock_validate_plex_config_folder(wizard_page, valid=True)
        mock_settings_get(wizard_page)
        mock_system_status(wizard_page)
        _drive_to_step4(wizard_page, app_url_wizard)
        expect(wizard_page.locator("#gpuDetecting")).to_be_hidden()

        wizard_page.locator('[data-edit="cpu"]').click()
        wizard_page.locator("#workerGroupCount").fill("0")
        wizard_page.locator("#workerGroupApply").click()
        expect(wizard_page.locator("#workerGroupMessage")).to_contain_text("Disable a group")

    def test_disabling_gpu_group_keeps_tuning_editable(self, wizard_page: Page, app_url_wizard: str) -> None:
        mock_plex_libraries(wizard_page)
        capture_settings_save(wizard_page)
        mock_setup_status(wizard_page, complete=False)
        mock_validate_plex_config_folder(wizard_page, valid=True)
        mock_settings_get(wizard_page)
        mock_system_status(wizard_page)
        _drive_to_step4(wizard_page, app_url_wizard)
        expect(wizard_page.locator("#gpuDetecting")).to_be_hidden()

        wizard_page.locator('[data-enable="gpu"]').uncheck()
        expect(wizard_page.locator("#workerGroupApplyRow")).to_be_visible()
        expect(wizard_page.locator(".gpu-tuning-threads").first).to_be_enabled()
        wizard_page.locator("#workerGroupApply").click()
        expect(wizard_page.locator('[data-enable="gpu"]')).not_to_be_checked()

    def test_rescan_gpus_button_calls_endpoint(self, wizard_page: Page, app_url_wizard: str) -> None:
        mock_plex_libraries(wizard_page)
        capture_settings_save(wizard_page)
        mock_setup_status(wizard_page, complete=False)
        mock_validate_plex_config_folder(wizard_page, valid=True)
        mock_settings_get(wizard_page)
        mock_system_status(wizard_page)
        called = mock_system_rescan_gpus(wizard_page)
        _drive_to_step4(wizard_page, app_url_wizard)
        expect(wizard_page.locator("#gpuDetecting")).to_be_hidden()

        wizard_page.locator("#gpuRescanBtn").click()
        # Give the JS a tick to fire the fetch.
        wizard_page.wait_for_timeout(300)
        assert called, "POST /api/system/rescan-gpus was never called"


@pytest.mark.e2e
class TestCpuWorkersStepper:
    def test_cpu_group_draft_saves_before_wizard_advances(self, wizard_page: Page, app_url_wizard: str) -> None:
        mock_plex_libraries(wizard_page)
        capture_settings_save(wizard_page)
        mock_setup_status(wizard_page, complete=False)
        mock_validate_plex_config_folder(wizard_page, valid=True)
        mock_settings_get(wizard_page)
        mock_system_status(wizard_page)
        _drive_to_step4(wizard_page, app_url_wizard)

        wizard_page.locator('[data-edit="cpu"]').click()
        cpu = wizard_page.locator("#workerGroupCount")
        expect(cpu).to_have_value("1")
        cpu.fill("2")
        wizard_page.locator("#step4Next").click()
        expect(wizard_page.locator('div.setup-step[data-step="5"]')).to_have_class("setup-step active")


@pytest.mark.e2e
class TestThumbnailQualityDirection:
    """Issue #286 — the wizard's copy of the tooltip drifted the same way.

    settings.html and setup.html are independent copies; both shipped the
    inverted scale, so both cells need a row.
    """

    def test_wizard_tooltip_states_lower_is_better(self, wizard_page: Page, app_url_wizard: str) -> None:
        mock_plex_libraries(wizard_page)
        capture_settings_save(wizard_page)
        mock_setup_status(wizard_page, complete=False)
        mock_validate_plex_config_folder(wizard_page, valid=True)
        mock_settings_get(wizard_page)
        mock_system_status(wizard_page)
        _drive_to_step4(wizard_page, app_url_wizard)

        icon = wizard_page.locator('label[for="thumbnailQuality"] .info-icon')
        # Bootstrap's Tooltip constructor moves `title` to
        # `data-bs-original-title`, so read whichever survived init.
        title = icon.get_attribute("data-bs-original-title") or icon.get_attribute("title")
        assert title, "thumbnail quality tooltip has no text"
        title_lc = title.lower()
        assert "lower is better" in title_lc
        assert "higher = better" not in title_lc
        assert "10 = sharpest" not in title_lc

        # setup.html puts the visible hint in the .form-text under the
        # slider, not in the label like settings.html does.
        expect(wizard_page.locator("#qualityValue").locator("..")).to_contain_text("lower = sharper")


@pytest.mark.e2e
class TestWorkerGroupStyling:
    def test_worker_group_rows_are_styled_and_charted_when_wizard_reaches_step4(
        self, wizard_page: Page, app_url_wizard: str
    ) -> None:
        mock_plex_libraries(wizard_page)
        capture_settings_save(wizard_page)
        mock_setup_status(wizard_page, complete=False)
        mock_validate_plex_config_folder(wizard_page, valid=True)
        mock_settings_get(wizard_page)
        mock_system_status(wizard_page)
        _drive_to_step4(wizard_page, app_url_wizard)
        item = wizard_page.locator("#workerGroupSettings .wg-item").first
        expect(item).to_be_visible()
        # The settings stylesheet gives each group a bordered card; unstyled it would be 0px.
        assert item.evaluate("el => getComputedStyle(el).borderTopWidth") == "1px"
        expect(wizard_page.locator("#workerWeekGraph .wkg-row")).to_have_count(7)
