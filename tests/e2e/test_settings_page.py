"""E2E tests for /settings — full page interaction coverage."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from ._mocks import (
    capture_settings_backups_restore,
    capture_settings_save,
    mock_settings_backups,
    mock_settings_get,
    mock_setup_status,
    mock_system_status,
    mock_token_regenerate,
    mock_token_set,
    mock_worker_groups,
)
from .conftest import accept_app_confirm


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


@pytest.fixture
def settings_page(authed_page: Page, app_url: str) -> Page:
    mock_settings_get(authed_page)
    mock_worker_groups(authed_page)
    mock_setup_status(authed_page, complete=True, plex_authenticated=True)
    mock_system_status(authed_page)
    mock_settings_backups(authed_page)
    capture_settings_save(authed_page)
    authed_page.goto(f"{app_url}/settings")
    authed_page.wait_for_load_state("domcontentloaded")
    return authed_page


@pytest.mark.e2e
class TestSettingsLayout:
    def test_sidebar_links_present(self, settings_page: Page) -> None:
        for href in (
            "#section-processing",
            "#section-logging",
            "#section-auth",
            "#section-backups",
            "#section-about",
        ):
            expect(settings_page.locator(f'a[href="{href}"]')).to_be_visible()

    def test_per_gpu_panel_renders_cards(self, settings_page: Page) -> None:
        # mock_system_status renders 2 GPUs by default.
        expect(settings_page.locator("#gpuDetecting")).to_be_hidden(timeout=3000)
        expect(settings_page.locator("#gpuConfigList .gpu-tuning-threads")).to_have_count(2)

    def test_gpu_tuning_does_not_edit_worker_allocation(self, settings_page: Page) -> None:
        expect(settings_page.locator("#gpuDetecting")).to_be_hidden(timeout=3000)
        expect(settings_page.locator(".gpu-workers, .gpu-enable-toggle")).to_have_count(0)
        tuning = settings_page.locator(".gpu-tuning-threads").first
        tuning.fill("4")
        config = settings_page.evaluate("collectGpuConfig()")
        assert config[0]["ffmpeg_threads"] == 4
        assert all("workers" not in gpu and "enabled" not in gpu for gpu in config)


@pytest.mark.e2e
class TestSettingsSteppers:
    def test_cpu_group_count_is_staged_until_apply(self, settings_page: Page) -> None:
        settings_page.locator('[data-edit="cpu"]').click()
        count = settings_page.locator("#workerGroupMembers [data-member] [data-count]")
        expect(count).to_have_value("1")
        count.fill("2")
        expect(settings_page.locator("#workerGroupApplyRow")).to_be_visible()
        settings_page.locator("#workerGroupApply").click()
        row = settings_page.locator('[data-group-id="cpu"]')
        expect(row.locator(".worker-group-control-label")).to_have_text("Workers")
        expect(row.locator('[aria-label="Configured workers"]')).to_have_text("2")
        settings_page.reload()
        expect(row.locator('[aria-label="Configured workers"]')).to_have_text("2")

    def test_thumbnail_interval_stepper_works(self, settings_page: Page) -> None:
        interval = settings_page.locator("#thumbnailInterval")
        # Default unified to 10s post-#238 (BIF/Plex community convention).
        # See settings.html input value, settings_manager.thumbnail_interval,
        # config/__init__.py loader default, and docs/reference.md.
        expect(interval).to_have_value("10")
        plus = interval.locator(".. >> .stepper-plus").first
        plus.click()
        expect(interval).to_have_value("11")

    def test_log_rotation_stepper_works(self, settings_page: Page) -> None:
        rot = settings_page.locator("#logRotationSize")
        expect(rot).to_have_value("10")
        plus = rot.locator(".. >> .stepper-plus").first
        plus.click()
        expect(rot).to_have_value("11")


@pytest.mark.e2e
class TestSettingsAuth:
    def test_set_custom_token_matching_succeeds(self, authed_page: Page, app_url: str) -> None:
        mock_settings_get(authed_page)
        mock_setup_status(authed_page, complete=True)
        mock_system_status(authed_page)
        mock_settings_backups(authed_page)
        capture_settings_save(authed_page)
        captured = mock_token_set(authed_page, ok=True)
        authed_page.goto(f"{app_url}/settings")
        authed_page.wait_for_load_state("domcontentloaded")

        # Settings page uses customAuthToken + customAuthTokenConfirm.
        # The "log out all sessions" prompt is an appConfirm modal — click
        # its OK button after triggering setCustomToken(). ``void`` so
        # Playwright's auto-await on returned Promises doesn't deadlock
        # waiting for the modal to resolve before we've clicked OK.
        authed_page.locator("#customAuthToken").fill("brand-new-tok-1")
        authed_page.locator("#customAuthTokenConfirm").fill("brand-new-tok-1")
        authed_page.evaluate("void setCustomToken()")
        with authed_page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/api/token/set"),
            timeout=5000,
        ) as req_info:
            accept_app_confirm(authed_page)
        req_info.value  # noqa: B018 — waiting for the response, not the request, means the mock has recorded the body
        assert captured, "POST /api/token/set never fired"
        # Pin the payload: the token field must equal the typed value.
        # A regression that always sent an empty/wrong token would have
        # silently passed the call-count check (audit P2 finding).
        assert captured[0].get("token") == "brand-new-tok-1", (
            f"POST /api/token/set body must carry the typed token; got {captured[0]!r}"
        )

    def test_regenerate_token_button_calls_endpoint(self, authed_page: Page, app_url: str) -> None:
        mock_settings_get(authed_page)
        mock_setup_status(authed_page, complete=True)
        mock_system_status(authed_page)
        mock_settings_backups(authed_page)
        capture_settings_save(authed_page)
        called = mock_token_regenerate(authed_page)
        authed_page.goto(f"{app_url}/settings")
        authed_page.wait_for_load_state("domcontentloaded")

        # Direct invoke to bypass any visibility/scroll issues. The confirm
        # is an appConfirm modal, not native window.confirm. ``void`` so
        # Playwright's auto-await on returned Promises doesn't deadlock
        # waiting for the modal to resolve before we've clicked OK.
        authed_page.evaluate("void regenerateToken()")
        with authed_page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/api/token/regenerate"),
            timeout=5000,
        ) as req_info:
            accept_app_confirm(authed_page)
        req_info.value  # noqa: B018 — waiting for the response, not the request, means the mock has recorded the body
        assert called, "POST /api/token/regenerate never fired"


@pytest.mark.e2e
class TestSettingsBackupsPanel:
    def test_panel_renders_three_entries_newest_first(self, settings_page: Page) -> None:
        # Default mock has 2 timestamped + 1 legacy entry for settings.json.
        # D17 — each file's snapshot list is now rendered as a single
        # <select> with one <option> per backup (replacing the old per-row
        # Restore button list). Assert the option count instead of the
        # button count, and confirm the newest option is selected first.
        expect(settings_page.locator("#backupRestorePanel")).to_contain_text("settings.json", timeout=3000)
        expect(settings_page.locator("#backupRestorePanel")).to_contain_text("legacy")
        options = settings_page.locator("#backupRestorePanel select option")
        assert options.count() >= 3

    def test_restore_specific_backup_posts_filename(self, settings_page: Page) -> None:
        captured = capture_settings_backups_restore(settings_page)
        # D17 — the dropdown defaults to the first <option> (the newest
        # 20260201-100000 timestamp from the default mock). The lone
        # "Restore selected" button reads the select's value, so a
        # plain click on it posts the newest backup filename. Restore
        # is gated by an appConfirm modal — accept it to fire the POST.
        settings_page.locator("#backupRestorePanel button:has-text('Restore')").first.click()
        with settings_page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/api/settings/backups/restore"),
            timeout=5000,
        ) as req_info:
            accept_app_confirm(settings_page)
        req_info.value  # noqa: B018 — waiting for the response, not the request, means the mock has recorded the body
        assert captured, "POST /api/settings/backups/restore never fired"
        assert captured[0]["file"] == "settings.json"
        # Newest entry is the timestamped one.
        assert "20260201-100000" in (captured[0].get("backup") or "")


@pytest.mark.e2e
class TestThumbnailQualityDirection:
    """Issue #286 — the rendered tooltip must not invert the qscale.

    ``thumbnail_quality`` is passed verbatim to FFmpeg's ``-q:v``, so lower
    is better.  The tooltip claimed the opposite and users cranked the
    slider up expecting sharper thumbs.
    """

    def test_tooltip_states_lower_is_better(self, settings_page: Page) -> None:
        icon = settings_page.locator('label[for="thumbnailQuality"] .info-icon')
        # Bootstrap's Tooltip constructor moves `title` to
        # `data-bs-original-title`, so read whichever survived init.
        title = icon.get_attribute("data-bs-original-title") or icon.get_attribute("title")
        assert title, "thumbnail quality tooltip has no text"
        title_lc = title.lower()
        assert "lower is better" in title_lc
        assert "higher = better" not in title_lc
        assert "10 = sharpest" not in title_lc

    def test_visible_label_hints_direction_without_hovering(self, settings_page: Page) -> None:
        label = settings_page.locator('label[for="thumbnailQuality"]')
        expect(label).to_contain_text("lower = sharper")


@pytest.mark.e2e
class TestSettingsPolish:
    def test_pause_chart_is_hidden_when_quiet_hours_are_off(self, settings_page: Page) -> None:
        enabled = settings_page.locator("#quietHoursEnabled")
        if enabled.is_checked():
            enabled.evaluate("el => { el.checked = false; el.dispatchEvent(new Event('change', { bubbles: true })); }")
        expect(settings_page.locator("#pauseWeekCard")).to_be_hidden()
        expect(settings_page.locator("#quietHoursNextHint")).to_have_text("Applied when on.")

    def test_pause_chart_shows_when_quiet_hours_are_on(self, settings_page: Page) -> None:
        enabled = settings_page.locator("#quietHoursEnabled")
        enabled.evaluate("el => { el.checked = true; el.dispatchEvent(new Event('change', { bubbles: true })); }")
        expect(settings_page.locator("#pauseWeekCard")).to_be_visible()

    def test_pause_rules_sentence_lives_in_the_info_dialog_when_page_loads(self, settings_page: Page) -> None:
        assert "Missed scheduled runs are not caught up" in settings_page.evaluate(
            "document.getElementById('infoPauseRulesTpl').content.textContent"
        )
        expect(settings_page.locator("#section-worker-quiet-hours .lead-note")).to_have_count(0)

    def test_every_switch_uses_the_accent_colour_when_checked(self, settings_page: Page) -> None:
        colours = settings_page.evaluate(
            """() => {
                const probe = (parent) => { const i = document.createElement('input'); i.type = 'checkbox';
                    i.className = 'form-check-input'; i.checked = true; parent.appendChild(i);
                    const c = getComputedStyle(i).backgroundColor; i.remove(); return c; };
                return ['section-processing', 'section-markers'].map(id => probe(document.getElementById(id)));
            }"""
        )
        assert len(set(colours)) == 1

    def test_markers_decision_link_has_an_accessible_name(self, settings_page: Page) -> None:
        link = settings_page.locator("#markersHowItDecidesMore")
        # The link sits in a collapsed fold, so read its text rather than its rendered text.
        assert (link.text_content() or "").strip() or link.get_attribute("aria-label")

    def test_section_subtitles_fit_on_one_line_when_desktop(self, settings_page: Page) -> None:
        for desc in settings_page.locator(".sec-desc").all():
            assert len(desc.inner_text()) <= 90

    def test_backup_restore_panel_is_neutral_when_page_loads(self, settings_page: Page) -> None:
        expect(settings_page.locator("#section-backups .danger-zone")).to_have_count(0)
        expect(settings_page.locator("#section-backups .restore-zone")).to_be_visible()
