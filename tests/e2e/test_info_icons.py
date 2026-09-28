"""E2E: one ⓘ rule across the app.

Every ⓘ's hover is a short sentence. When a click opens a detail, the hover ends with "Click for more." and the icon
has the pointer class (``.info-icon-more``); when there is no detail, it has neither and the cursor stays default.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from ._mocks import mock_settings_backups, mock_settings_get, mock_setup_status, mock_system_status

MORE = "Click for more."

# What a rendered ⓘ says about itself: whether a detail is attached (the three ways app.js's click handler finds
# one), its hover text, whether it has the pointer class, and the cursor it actually shows.
_AUDIT_JS = """() => Array.from(document.querySelectorAll('.info-icon')).map((el) => {
    const tpl = el.dataset.explainTemplate ? document.getElementById(el.dataset.explainTemplate) : null;
    return {
        where: el.id || el.getAttribute('data-explain-title') || el.closest('label, h6, th, div')?.textContent.trim().slice(0, 60) || '',
        hasDetail: !!(el._explanationHtml || (tpl && tpl.innerHTML.trim()) || el.dataset.explainHtml),
        hint: el.getAttribute('data-bs-original-title') ?? el.getAttribute('title') ?? '',
        pointerClass: el.classList.contains('info-icon-more'),
        cursor: getComputedStyle(el).cursor,
    };
})"""


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


def _open(page: Page, app_url: str, path: str) -> list[dict]:
    mock_settings_get(page)
    mock_setup_status(page, complete=True, plex_authenticated=True)
    mock_system_status(page)
    mock_settings_backups(page)
    page.goto(f"{app_url}{path}")
    page.wait_for_load_state("domcontentloaded")
    if path == "/settings":
        # The per-GPU cards (and their ⓘs) render once /api/system/status answers.
        expect(page.locator("#gpuConfigList .card").first).to_be_visible(timeout=5000)
    return page.evaluate(_AUDIT_JS)


@pytest.mark.e2e
class TestInfoIconRule:
    @pytest.mark.parametrize("path", ["/", "/settings", "/servers", "/automation"])
    def test_click_for_more_and_pointer_exactly_when_a_detail_is_attached(
        self, authed_page: Page, app_url: str, path: str
    ) -> None:
        icons = _open(authed_page, app_url, path)
        assert icons, f"no ⓘ rendered on {path}"

        for icon in icons:
            hint = icon["hint"]
            assert hint, f"{path}: ⓘ at {icon['where']!r} has no hover text"
            if icon["hasDetail"]:
                assert hint.endswith(f" {MORE}") or hint == MORE, (path, icon)
                assert hint.count(MORE) == 1, (path, icon)
                assert icon["pointerClass"], (path, icon)
                assert icon["cursor"] == "pointer", (path, icon)
            else:
                assert "click for" not in hint.lower(), (path, icon)
                assert not icon["pointerClass"], (path, icon)
                assert icon["cursor"] == "default", (path, icon)

    def test_settings_has_both_kinds(self, authed_page: Page, app_url: str) -> None:
        # Guards the test above against passing vacuously on a page where every ⓘ is one kind.
        icons = _open(authed_page, app_url, "/settings")
        assert any(icon["hasDetail"] for icon in icons)
        assert any(not icon["hasDetail"] for icon in icons)

    def test_a_detail_icon_opens_the_info_modal(self, authed_page: Page, app_url: str) -> None:
        _open(authed_page, app_url, "/settings")
        icon = authed_page.locator('label[for="incomingJobPriority"] .info-icon')
        expect(icon).to_have_attribute(
            "data-bs-original-title",
            "Priority for jobs started when new media lands: webhooks and Recently Added sweeps. Click for more.",
        )
        icon.click()
        modal = authed_page.locator("#globalInfoModal")
        expect(modal).to_be_visible(timeout=5000)
        expect(authed_page.locator("#globalInfoTitle")).to_have_text("Incoming job priority")
        expect(authed_page.locator("#globalInfoBody")).to_contain_text("High lets a new episode jump ahead")

    def test_a_hover_only_icon_opens_nothing(self, authed_page: Page, app_url: str) -> None:
        _open(authed_page, app_url, "/settings")
        icon = authed_page.locator('label[for="logLevel"] .info-icon')
        expect(icon).not_to_have_class("info-icon-more")
        icon.click()
        expect(authed_page.locator("#globalInfoModal")).to_be_hidden()

    def test_gpu_panel_icons_follow_the_rule(self, authed_page: Page, app_url: str) -> None:
        # Formerly bare <i data-bs-toggle="tooltip"> icons rendered by gpu_config_panel.js.
        _open(authed_page, app_url, "/settings")
        icons = authed_page.locator("#gpuConfigList .info-icon")
        expect(icons.first).to_be_visible()
        assert icons.count() >= 2
        expect(icons.first).to_have_attribute(
            "data-bs-original-title",
            "How many files this GPU works on at once. Each one uses GPU memory, so start with 1.",
        )
