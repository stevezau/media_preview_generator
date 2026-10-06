"""Redesigned Servers, Webhook activity and Logs pages: the markup contracts the redesign introduced."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from ._mocks import (
    mock_server_connection_probe,
    mock_server_previews_readiness,
    mock_servers_list,
)
from .test_operational_followup import _fulfill_json, _logs

pytestmark = pytest.mark.e2e


@pytest.fixture(autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


def _server(server_id: str, name: str, *, enabled: bool = True, libraries: int = 3, enabled_libraries: int = 3) -> dict:
    return {
        "id": server_id,
        "name": name,
        "type": "plex",
        "enabled": enabled,
        "url": f"http://{server_id}:32400",
        "libraries": [{"id": str(i), "name": f"Lib {i}", "enabled": i < enabled_libraries} for i in range(libraries)],
    }


class TestServersCards:
    def test_cards_share_row_offsets_when_issue_row_differs(self, authed_page: Page, app_url: str) -> None:
        mock_servers_list(authed_page, servers=[_server("a", "Alpha"), _server("b", "Beta", enabled=False)])
        mock_server_connection_probe(authed_page, ok=True)
        mock_server_previews_readiness(authed_page, critical_count=2)
        authed_page.goto(f"{app_url}/servers")
        expect(authed_page.locator(".server-readiness-glyph").first).to_be_visible(timeout=3000)

        offsets = authed_page.evaluate(
            """() => [...document.querySelectorAll('.srv-card')].map(card => {
                const top = card.getBoundingClientRect().top;
                return ['.srv-top', '.srv-host', '.srv-issue', '.srv-lib', '.srv-foot'].map(
                    sel => Math.round(card.querySelector(sel).getBoundingClientRect().top - top));
            })"""
        )
        assert len(offsets) == 2
        assert offsets[0] == offsets[1]

    def test_status_dot_carries_connection_error_when_probe_fails(self, authed_page: Page, app_url: str) -> None:
        mock_servers_list(authed_page, servers=[_server("a", "Alpha")])
        mock_server_connection_probe(authed_page, ok=False)
        authed_page.goto(f"{app_url}/servers")

        dot = authed_page.locator("#server-status-a")
        expect(dot).to_have_attribute("aria-label", "Connection failed", timeout=3000)
        expect(dot).to_have_class("srv-dot bad")
        expect(authed_page.locator("#server-error-a")).to_contain_text("Connection failed")

    def test_library_count_warns_when_some_libraries_disabled(self, authed_page: Page, app_url: str) -> None:
        mock_servers_list(authed_page, servers=[_server("a", "Alpha", libraries=5, enabled_libraries=3)])
        mock_server_connection_probe(authed_page, ok=True)
        authed_page.goto(f"{app_url}/servers")

        expect(authed_page.locator(".srv-lib-num")).to_have_text("3")
        expect(authed_page.locator(".srv-lib-num")).to_have_class("srv-lib-num warn")
        expect(authed_page.locator(".srv-lib-sub")).to_have_text("of 5 libraries enabled")

    def test_disabled_card_dims_and_toggle_restores_it(self, authed_page: Page, app_url: str) -> None:
        mock_servers_list(authed_page, servers=[_server("a", "Alpha", enabled=False)])
        authed_page.route(
            "**/api/servers/a/enabled",
            lambda route: _fulfill_json(route, {"id": "a", "enabled": True}),
        )
        mock_server_connection_probe(authed_page, ok=True)
        authed_page.goto(f"{app_url}/servers")

        card = authed_page.locator(".srv-card")
        expect(card).to_have_class("card srv-card off")
        authed_page.locator("#server-enabled-a").check()
        expect(card).to_have_class("card srv-card")

    def test_empty_state_offers_add_server_when_no_servers(self, authed_page: Page, app_url: str) -> None:
        mock_servers_list(authed_page, servers=[])
        authed_page.goto(f"{app_url}/servers")

        empty = authed_page.locator("#serverList .srv-empty")
        expect(empty).to_contain_text("No servers yet")
        expect(empty.get_by_role("button", name="Add Server")).to_be_visible()


STATUS_PILLS = {
    "triggered": ("ok", "check-circle-fill"),
    "queued": ("warn", "hourglass-split"),
    "ignored": ("", "dash-circle"),
    "disabled": ("", "dash-circle"),
    "ignored_no_path": ("bad", "exclamation-triangle-fill"),
    "ignored_no_paths": ("bad", "exclamation-triangle-fill"),
    "error": ("bad", "exclamation-triangle-fill"),
    "test": ("run", "beaker"),
}


def _history(page: Page, statuses: list[str], *, files: bool = False) -> None:
    events = [
        {
            "timestamp": f"2026-10-04T10:{i:02d}:00Z",
            "source": "sonarr",
            "status": status,
            "server_name": "Home Plex",
            "server_type": "plex",
            "title": f"Title {status}",
            **({"files_preview": ["/media/a.mkv"]} if files else {}),
        }
        for i, status in enumerate(statuses)
    ]
    page.route("**/api/webhooks/history", lambda route: _fulfill_json(route, {"events": events}))


class TestWebhookActivity:
    @pytest.mark.parametrize("status", sorted(STATUS_PILLS))
    def test_status_pill_has_tone_and_icon_when_event_has_status(self, authed_page: Page, app_url: str, status: str):
        _history(authed_page, [status])
        authed_page.goto(f"{app_url}/webhook-activity")

        tone, icon = STATUS_PILLS[status]
        pill = authed_page.locator(f"#historyBody .status-badge.{status}")
        expect(pill).to_have_count(1)
        classes = pill.get_attribute("class").split()
        assert "pill" in classes
        assert (tone in classes) if tone else not ({"ok", "bad", "warn", "run"} & set(classes))
        expect(pill.locator(f"i.bi-{icon}")).to_have_count(1)

    def test_outcome_tab_filters_and_syncs_hidden_select_when_clicked(self, authed_page: Page, app_url: str) -> None:
        _history(authed_page, ["triggered", "triggered", "queued", "error"])
        authed_page.goto(f"{app_url}/webhook-activity")

        expect(authed_page.locator("#historyOutcomeTabs button.on")).to_contain_text("All")
        authed_page.locator('#historyOutcomeTabs [data-value="triggered"]').click()
        expect(authed_page.locator("#historyStatus")).to_have_value("triggered")
        expect(authed_page.locator("#historyBody tr.wh-row")).to_have_count(2)
        expect(authed_page.locator("#historyFilterCount")).to_have_text("2 of 4 received events")
        expect(authed_page.locator("#historyReset")).to_be_visible()

    def test_titles_start_at_same_x_when_only_some_rows_expand(self, authed_page: Page, app_url: str) -> None:
        _history(authed_page, ["triggered", "queued"], files=True)
        authed_page.goto(f"{app_url}/webhook-activity")
        expect(authed_page.locator("#historyBody .wh-name")).to_have_count(2)

        xs = authed_page.evaluate(
            "() => [...document.querySelectorAll('#historyBody .wh-name')].map(e => Math.round(e.getBoundingClientRect().left))"
        )
        assert len(xs) == 2
        assert xs[0] == xs[1]
        expect(authed_page.locator("#historyBody .wh-ibtn")).to_have_count(1)

    def test_queued_row_is_faded_when_event_is_queued(self, authed_page: Page, app_url: str) -> None:
        _history(authed_page, ["queued", "triggered"])
        authed_page.goto(f"{app_url}/webhook-activity")

        expect(authed_page.locator("#historyBody tr.faded")).to_have_count(1)
        expect(authed_page.locator("#historyBody tr.faded")).to_contain_text("Queued")

    def test_file_toggle_expands_when_title_line_clicked(self, authed_page: Page, app_url: str) -> None:
        _history(authed_page, ["triggered"], files=True)
        authed_page.goto(f"{app_url}/webhook-activity")

        authed_page.locator("#historyBody .wh-title").click()
        expect(authed_page.locator("#history-detail-0")).to_be_visible()
        expect(authed_page.locator("#history-files-toggle-0")).to_have_attribute("aria-expanded", "true")


class TestLogsPage:
    def test_level_word_is_tinted_and_message_is_plain_when_line_is_error(self, authed_page: Page, app_url: str):
        _logs(authed_page)
        authed_page.goto(f"{app_url}/logs")

        error = authed_page.locator(".log-line.log-level-error")
        info = authed_page.locator(".log-line.log-level-info")
        expect(error).to_have_count(1)
        colours = authed_page.evaluate(
            """() => {
                const c = el => getComputedStyle(el).color;
                const e = document.querySelector('.log-level-error');
                const i = document.querySelector('.log-level-info');
                return {errLvl: c(e.querySelector('.log-lvl')), errMsg: c(e.querySelector('.log-msg')),
                        infoLvl: c(i.querySelector('.log-lvl')), infoMsg: c(i.querySelector('.log-msg')),
                        infoBg: getComputedStyle(i).backgroundColor};
            }"""
        )
        assert colours["errLvl"] != colours["errMsg"]
        assert colours["infoLvl"] != colours["infoMsg"]
        assert colours["infoMsg"] == colours["errMsg"]
        assert colours["infoBg"] == "rgba(0, 0, 0, 0)"
        expect(info).to_have_count(1)

    def test_no_match_state_resets_filters_when_search_matches_nothing(self, authed_page: Page, app_url: str) -> None:
        _logs(authed_page)
        authed_page.goto(f"{app_url}/logs")

        authed_page.locator("#logSearch").fill("zzz-nothing")
        expect(authed_page.locator("#logNoMatch")).to_be_visible()
        authed_page.locator("#logResetFilters").click()
        expect(authed_page.locator("#logNoMatch")).to_be_hidden()
        expect(authed_page.locator("#logSearch")).to_have_value("")

    def test_history_failure_offers_retry_when_history_request_fails(self, authed_page: Page, app_url: str) -> None:
        authed_page.route("**/socket.io/**", lambda route: route.abort())
        authed_page.route("**/api/logs/history**", lambda route: _fulfill_json(route, {"error": "x"}, status=503))
        authed_page.goto(f"{app_url}/logs")

        expect(authed_page.locator("#logFeedback")).to_contain_text("could not be loaded")
        expect(authed_page.locator("#logFeedbackRetry")).to_be_visible()

    def test_view_menu_holds_view_source_wrap_and_group_controls(self, authed_page: Page, app_url: str) -> None:
        _logs(authed_page)
        authed_page.goto(f"{app_url}/logs")

        for control in ("#logView", "#logSource", "#logWrap", "#logGroupSource"):
            expect(authed_page.locator(control)).to_be_hidden()
        authed_page.locator("#logViewBtn").click()
        for control in ("#logView", "#logSource", "#logWrap", "#logGroupSource"):
            expect(authed_page.locator(control)).to_be_visible()
