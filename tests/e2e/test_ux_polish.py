"""Keyboard and mobile journeys for the approved UX polish."""

from __future__ import annotations

import re
from urllib.parse import quote

import pytest
from playwright.sync_api import Page, expect

from . import _inspector_fixtures as fx
from ._mocks import (
    capture_settings_save,
    mock_plex_libraries,
    mock_settings_backups,
    mock_settings_get,
    mock_setup_status,
    mock_system_status,
)
from .conftest import expect_modal_shown, watch_modal_shown
from .test_intro_credits_server_tab import _mock_server_page, _plex_server, _status
from .test_navbar_menus import _expect_landed
from .test_wizard_step3_paths import _drive_to_step3

pytestmark = pytest.mark.e2e
PHONE = {"width": 390, "height": 844}


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


@pytest.fixture
def settings_page(authed_page: Page, app_url: str) -> Page:
    mock_settings_get(authed_page)
    mock_settings_backups(authed_page)
    mock_setup_status(authed_page, complete=True, plex_authenticated=True)
    mock_system_status(authed_page)
    capture_settings_save(authed_page)
    authed_page.goto(f"{app_url}/settings")
    expect(authed_page.locator("#gpuConfigList .card")).to_have_count(2)
    return authed_page


def test_first_tab_skips_navigation_and_moves_focus_to_main(settings_page: Page) -> None:
    settings_page.keyboard.press("Tab")
    skip = settings_page.get_by_role("link", name="Skip to main content")
    expect(skip).to_be_focused()
    expect(skip).to_be_visible()
    settings_page.keyboard.press("Enter")
    expect(settings_page.get_by_role("main")).to_be_focused()


def test_mobile_settings_selection_and_hash_navigation_agree(settings_page: Page) -> None:
    page = settings_page
    page.set_viewport_size(PHONE)
    selector = page.locator("#settingsMobileSection")
    selector.select_option("section-logging")
    expect(page).to_have_url(re.compile(r"#section-logging$"))
    _expect_landed(page, "section-logging")
    destination = page.locator("#section-logging").bounding_box()
    navigation = page.locator(".settings-mobile-nav").bounding_box()
    assert destination and navigation
    assert destination["y"] >= navigation["y"] + navigation["height"]
    expect(page.locator("#settingsMobileSectionFeedback")).to_contain_text("Logging")

    page.goto(page.url.split("#")[0] + "#section-backups")
    expect(selector).to_have_value("section-backups")
    expect(page.locator("#settingsMobileSectionFeedback")).to_contain_text("Backups")
    page.go_back()
    expect(selector).to_have_value("section-logging")


def test_settings_extra_guidance_can_be_opened_and_closed_with_keyboard(settings_page: Page) -> None:
    toggle = settings_page.locator(".settings-help-toggle").first
    expect(toggle).to_have_attribute("aria-expanded", "false")
    detail_id = toggle.get_attribute("aria-controls")
    assert detail_id
    detail = settings_page.locator(f"#{detail_id}")
    expect(detail).to_be_hidden()
    toggle.focus()
    settings_page.keyboard.press("Enter")
    expect(toggle).to_have_attribute("aria-expanded", "true")
    expect(detail).to_be_visible()
    assert detail.inner_text().strip()
    settings_page.keyboard.press("Space")
    expect(toggle).to_have_attribute("aria-expanded", "false")
    expect(detail).to_be_hidden()
    expect(toggle).to_be_focused()


def test_compact_settings_help_keeps_full_guidance_in_its_dialog(settings_page: Page) -> None:
    page = settings_page
    page.get_by_role("button", name="Explain max concurrent jobs").click()
    expect(page.locator("#globalInfoModal")).to_be_visible()
    expect(page.locator("#globalInfoBody")).to_contain_text("Higher values (5–10)")
    expect(page.locator("#globalInfoBody")).to_contain_text("servers + disks can handle it")


def test_mobile_automation_jumps_switch_panes_and_follow_hash(authed_page: Page, app_url: str) -> None:
    page = authed_page
    page.set_viewport_size(PHONE)
    page.goto(f"{app_url}/automation")
    page.locator('[data-automation-mobile-target="quiet-hours"]').click()
    expect(page.locator('[data-automation-mobile-target="quiet-hours"]')).to_have_attribute(
        "aria-current", re.compile("^(page|location|true)$")
    )
    expect(page.locator("#pane-schedules")).to_be_visible()
    expect(page.locator("#pane-triggers")).to_be_hidden()
    expect(page).to_have_url(re.compile(r"#section-schedules-quiet-hours$"))
    _expect_landed(page, "section-schedules-quiet-hours")
    destination = page.locator("#section-schedules-quiet-hours").bounding_box()
    navigation = page.locator("#automationMobileNav").bounding_box()
    assert destination and navigation
    assert destination["y"] >= navigation["y"] + navigation["height"]
    expect(page.locator("#automationMobileFeedback")).to_contain_text(re.compile("Quiet hours", re.I))

    page.locator('[data-automation-mobile-target="triggers"]').click()
    expect(page.locator("#pane-triggers")).to_be_visible()
    expect(page.locator("#pane-schedules")).to_be_hidden()
    page.goto(f"{app_url}/automation#section-schedules-list")
    expect(page.locator("#pane-schedules")).to_be_visible()
    expect(page.locator("#automationMobileFeedback")).to_contain_text("Schedules")


def test_server_section_selector_tracks_tabs_when_viewport_changes(authed_page: Page, app_url: str) -> None:
    page = authed_page
    server = _plex_server()
    _mock_server_page(page, server, _status(server, "ready"))
    page.set_viewport_size(PHONE)
    page.goto(f"{app_url}/servers")
    watch_modal_shown(page, "editServerModal")
    page.locator(f'.edit-server-btn[data-id="{server["id"]}"]').click()
    expect_modal_shown(page, "editServerModal")
    selector = page.locator("#editServerSectionSelect")
    selector.select_option(label="Libraries")
    expect(page.locator("#edit-tab-libraries")).to_be_visible()
    selector.select_option(label="Loudness")
    expect(page.locator("#edit-tab-loudness")).to_be_visible()

    page.set_viewport_size({"width": 1440, "height": 900})
    page.locator('#editServerModal [data-bs-target="#edit-tab-libraries"]').click()
    page.set_viewport_size(PHONE)
    expect(selector.locator("option:checked")).to_have_text("Libraries")
    expect(page.locator("#edit-tab-libraries")).to_be_visible()


def test_setup_progress_name_updates_after_forward_and_back(wizard_page: Page, app_url_wizard: str) -> None:
    page = wizard_page
    mock_plex_libraries(page)
    capture_settings_save(page)
    mock_setup_status(page, complete=False)
    _drive_to_step3(page, app_url_wizard)
    progress = page.get_by_role("progressbar")
    expect(progress).to_have_accessible_name("Setup progress: step 3 of 5, Paths")
    expect(progress).to_have_attribute("aria-valuenow", "3")
    page.locator("#step3Back").click()
    expect(progress).to_have_accessible_name("Setup progress: step 2 of 5, Connect")
    expect(progress).to_have_attribute("aria-valuenow", "2")


def test_mobile_inspector_jumps_only_offer_sections_present_in_the_file(authed_page: Page, app_url: str) -> None:
    page = authed_page
    api = fx.InspectorApi()
    file, item = fx.checked_film()
    api.add(file, item, fx.default_kinds(file["canonical_path"]))
    fx.install(page, api)
    page.set_viewport_size(PHONE)
    page.goto(f"{app_url}/inspector?path={quote(fx.FILM)}")
    expect(page.locator("#inspEvidence")).to_be_visible()
    nav = page.get_by_role("navigation", name="Inspector sections")
    expect(nav.get_by_role("link", name="Loudness")).to_be_hidden()
    nav.get_by_role("link", name="Servers").click()
    expect(page.locator("#inspServers")).to_be_in_viewport()


def test_mobile_help_opens_documentation_in_a_separate_tab(authed_page: Page, app_url: str) -> None:
    page = authed_page
    page.set_viewport_size(PHONE)
    page.context.route("https://github.com/**", lambda route: route.fulfill(body="Documentation fixture"))
    page.goto(f"{app_url}/settings")
    page.locator(".navbar-toggler").click()
    expect(page.locator("#helpMenuBtn")).to_be_visible()
    page.locator("#helpMenuBtn").click()
    with page.expect_popup() as opened:
        page.locator('[aria-labelledby="helpMenuBtn"]').get_by_role("link", name="Documentation").click()
    popup = opened.value
    expect(popup.locator("body")).to_have_text("Documentation fixture")
    expect(page).to_have_url(f"{app_url}/settings")
    popup.close()
