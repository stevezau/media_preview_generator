"""Polish-audit regressions for /automation, /logs and /webhook-activity."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

from ._mocks import _fulfill_json


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


_LOG_LINE = """() => {
    const c = document.getElementById('logContainer');
    c.innerHTML = '<span class="log-line log-level-info"><span class="log-ts">t</span>'
        + '<span class="log-lvl">INFO</span><span class="log-mod">[m]</span><span class="log-msg">hi</span></span>';
    const cs = getComputedStyle(c);
    const lvl = getComputedStyle(c.querySelector('.log-lvl'));
    return {bg: cs.backgroundColor, tt: lvl.textTransform, fw: lvl.fontWeight};
}"""


@pytest.mark.e2e
class TestLogsPolish:
    def _open(self, authed_page: Page, app_url: str) -> None:
        authed_page.route("**/api/logs**", lambda r: _fulfill_json(r, {"logs": [], "files": []}))
        authed_page.goto(f"{app_url}/logs")
        authed_page.wait_for_selector("#logContainer")

    def test_log_body_is_not_near_black_when_theme_is_light(self, authed_page: Page, app_url: str) -> None:
        self._open(authed_page, app_url)
        authed_page.evaluate("document.documentElement.setAttribute('data-bs-theme', 'light')")
        bg = authed_page.evaluate(_LOG_LINE)["bg"]
        assert bg == "rgb(30, 41, 59)"

    def test_log_body_keeps_terminal_colour_when_theme_is_dark(self, authed_page: Page, app_url: str) -> None:
        self._open(authed_page, app_url)
        authed_page.evaluate("document.documentElement.setAttribute('data-bs-theme', 'dark')")
        assert authed_page.evaluate(_LOG_LINE)["bg"] == "rgb(11, 11, 20)"

    def test_level_word_is_semibold_sentence_case_when_rendered(self, authed_page: Page, app_url: str) -> None:
        self._open(authed_page, app_url)
        result = authed_page.evaluate(_LOG_LINE)
        assert result["tt"] == "lowercase"
        assert result["fw"] == "600"

    def test_no_horizontal_overflow_when_viewport_is_phone(self, authed_page: Page, app_url: str) -> None:
        authed_page.set_viewport_size({"width": 390, "height": 844})
        self._open(authed_page, app_url)
        assert authed_page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")


@pytest.mark.e2e
class TestWebhookActivityPolish:
    def _open(self, authed_page: Page, app_url: str) -> None:
        events = [
            {"timestamp": "2026-10-01T10:00:00", "source": "plex", "status": "triggered", "title": "A",
             "server_name": "Plex", "server_type": "plex", "files_preview": [], "path_count": 1},
            {"timestamp": "2026-10-01T10:01:00", "source": "sonarr", "status": "queued", "title": "B"},
            {"timestamp": "2026-10-01T10:02:00", "source": "sonarr", "status": "queued", "title": "C",
             "server_name": "Home Emby", "server_type": "emby"},
        ]  # fmt: skip
        authed_page.route(
            "**/api/webhooks/history**", lambda r: _fulfill_json(r, {"history": events, "events": events})
        )
        authed_page.goto(f"{app_url}/webhook-activity")
        authed_page.wait_for_selector("#historyTable tbody tr.wh-row")

    def test_server_cell_is_empty_when_it_repeats_source_or_is_all(self, authed_page: Page, app_url: str) -> None:
        self._open(authed_page, app_url)
        cells = authed_page.locator("#historyTable tbody tr.wh-row td.wh-srv")
        texts = [cells.nth(i).inner_text().strip() for i in range(cells.count())]
        assert "" in texts
        assert "All" not in texts
        assert "Home Emby" in texts
        assert "Plex" not in texts

    def test_source_chip_is_neutral_when_rendered(self, authed_page: Page, app_url: str) -> None:
        self._open(authed_page, app_url)
        bg = authed_page.locator(".source-badge").first.evaluate("e => getComputedStyle(e).backgroundColor")
        inset = authed_page.evaluate("getComputedStyle(document.querySelector('.wh-seg')).backgroundColor")
        assert bg == inset

    def test_no_horizontal_overflow_when_viewport_is_phone(self, authed_page: Page, app_url: str) -> None:
        authed_page.set_viewport_size({"width": 390, "height": 844})
        self._open(authed_page, app_url)
        assert authed_page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")


@pytest.mark.e2e
class TestAutomationPolish:
    def test_state_pill_is_hidden_when_switch_shows_state(self, authed_page: Page, app_url: str) -> None:
        authed_page.goto(f"{app_url}/automation")
        authed_page.wait_for_selector("#webhookEnabled", state="attached")
        assert not authed_page.locator("#webhookStatePill").is_visible()

    def test_no_horizontal_overflow_when_viewport_is_phone(self, authed_page: Page, app_url: str) -> None:
        authed_page.set_viewport_size({"width": 390, "height": 844})
        authed_page.goto(f"{app_url}/automation#section-webhooks-custom")
        authed_page.wait_for_selector("#webhookEnabled", state="attached")
        assert authed_page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
