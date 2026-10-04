"""Operational filters, full log data, and vendor-safe wizard resume."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from ._mocks import (
    _fulfill_json,
    capture_settings_save,
    mock_plex_libraries,
    mock_settings_get,
    mock_setup_status,
    mock_setup_token_info,
    mock_system_status,
)

pytestmark = pytest.mark.e2e


def _resume(page: Page, vendor: str, *, missing: bool = False) -> tuple[list, list]:
    mock_settings_get(page)
    mock_setup_status(page, complete=False)
    mock_system_status(page)
    mock_setup_token_info(page)
    state = {"step": 3, "data": {"chosenVendor": vendor, "chosenEjServerId": "saved-server"}}
    saves, updates = [], []

    def state_route(route):
        if route.request.method == "POST":
            body = route.request.post_data_json
            saves.append(body)
            state.update(body)
            _fulfill_json(route, {"success": True})
        else:
            _fulfill_json(route, state)

    def server_route(route):
        if route.request.method == "PATCH":
            updates.append(route.request.post_data_json)
        _fulfill_json(
            route,
            {
                "id": "saved-server",
                "name": "Home " + vendor.title(),
                "type": vendor,
                "path_mappings": [{"remote_prefix": "/server", "local_prefix": "/media"}],
                "exclude_paths": [{"value": "/media/trailers", "type": "path"}],
            },
            status=404 if missing else 200,
        )

    page.route("**/api/setup/state", state_route)
    page.route("**/api/servers/saved-server", server_route)
    page.route(
        "**/api/settings/validate-local-path", lambda route: _fulfill_json(route, {"exists": True, "readable": True})
    )
    return saves, updates


@pytest.mark.parametrize("vendor", ["emby", "jellyfin"])
def test_resume_restores_vendor_paths_and_updates_only_that_server(wizard_page, app_url_wizard, vendor):
    saves, updates = _resume(wizard_page, vendor)
    wizard_page.goto(app_url_wizard + "/setup")
    expect(wizard_page.locator("#setupResumeNotice")).to_contain_text("Home " + vendor.title())
    expect(wizard_page.locator("#step3PlexConfigSection")).to_be_hidden()
    expect(wizard_page.locator("#setupProgressCurrent")).to_have_text("Step 2 of 4 · Paths")
    expect(wizard_page.locator(".path-mapping-local")).to_have_value("/media")
    expect(wizard_page.locator(".exclude-path-value")).to_have_value("/media/trailers")
    wizard_page.locator("#step3Next").click()
    expect(wizard_page.locator('[data-step="4"].setup-step')).to_be_visible()
    assert updates[0]["path_mappings"][0]["local_prefix"] == "/media"
    assert saves[-1]["data"]["chosenEjServerId"] == "saved-server"
    assert "selectedServer" not in saves[-1]["data"]
    assert "token" not in str(saves).lower()
    wizard_page.reload()
    expect(wizard_page.locator('[data-step="4"].setup-step')).to_be_visible()
    wizard_page.locator("#step4Back").click()
    expect(wizard_page.locator(".path-mapping-local")).to_have_value("/media")


def test_missing_resume_server_stays_at_safe_vendor_picker(wizard_page, app_url_wizard):
    saves, updates = _resume(wizard_page, "jellyfin", missing=True)
    wizard_page.goto(app_url_wizard + "/setup")
    expect(wizard_page.locator("#setupResumeNotice")).to_contain_text("unavailable")
    expect(wizard_page.locator('[data-step="1"].setup-step')).to_be_visible()
    assert not saves and not updates


def test_real_file_preview_uses_draft_mapping_and_reports_error(wizard_page, app_url_wizard):
    _resume(wizard_page, "emby")
    captured = []

    def preview(route):
        captured.append(route.request.post_data_json)
        _fulfill_json(
            route,
            {
                "local_path": "/draft/Movie.mkv",
                "exists": True,
                "is_file": True,
                "readable": True,
                "mapping_applied": True,
            },
        )

    wizard_page.route("**/api/setup/preview-file-path", preview)
    wizard_page.goto(app_url_wizard + "/setup")
    wizard_page.locator(".path-mapping-local").fill("/draft")
    wizard_page.locator("#setupSamplePath").fill("/server/Movie.mkv")
    wizard_page.locator("#setupCheckFile").click()
    expect(wizard_page.locator("#setupPathPreviewResult")).to_contain_text("File found and readable")
    assert captured[0]["path_mappings"][0]["local_prefix"] == "/draft"
    wizard_page.locator("#setupSamplePath").fill("/server/Missing.mkv")
    expect(wizard_page.locator("#setupPathPreviewResult")).to_be_empty()
    wizard_page.route(
        "**/api/setup/preview-file-path",
        lambda route: _fulfill_json(route, {"error": "Outside allowed root"}, status=400),
    )
    wizard_page.locator("#setupCheckFile").click()
    expect(wizard_page.locator("#setupPathPreviewResult")).to_contain_text("Outside allowed root")
    expect(wizard_page.locator("#setupCheckFile")).to_be_enabled()


def _activity(page):
    events = [
        {
            "timestamp": "2026-10-04T10:00:00Z",
            "source": "sonarr",
            "status": "triggered",
            "server_id": "p",
            "server_name": "Home Plex",
            "server_type": "plex",
            "title": "The Example",
            "files_preview": ["/media/episode.mkv"],
        },
        {
            "timestamp": "2026-10-04T09:00:00Z",
            "source": "radarr",
            "status": "error",
            "server_id": "j",
            "server_name": "Home Jellyfin",
            "server_type": "jellyfin",
            "title": "Another movie",
        },
    ]
    page.route("**/api/webhooks/history", lambda route: _fulfill_json(route, {"events": events}))


def test_activity_filters_compose_and_distinguish_empty_errors(authed_page, app_url, complete_setup):
    _activity(authed_page)
    authed_page.goto(app_url + "/webhook-activity")
    expect(authed_page.locator("#historyFilterCount")).to_have_text("2 of 2 received events")
    authed_page.locator("#historySearch").fill("episode.mkv")
    expect(authed_page.locator("#historyFilterCount")).to_have_text("1 of 2 received events")
    authed_page.locator("#historyStatus").select_option("error")
    expect(authed_page.locator("#historyNoMatches")).to_be_visible()
    expect(authed_page.locator("#historyEmpty")).to_be_hidden()
    authed_page.get_by_role("button", name="Reset filters").click()
    authed_page.locator("#historyServer").select_option("j")
    expect(authed_page.locator("#historyBody")).to_contain_text("Another movie")
    expect(authed_page.locator("#historyBody")).not_to_contain_text("The Example")
    authed_page.route("**/api/webhooks/history", lambda route: _fulfill_json(route, {"error": "offline"}, status=503))
    authed_page.locator("#historyRefresh").click()
    expect(authed_page.locator("#historyError")).to_be_visible()
    expect(authed_page.locator("#historyRefresh")).to_be_enabled()


def _logs(page):
    page.route("**/socket.io/**", lambda route: route.abort())
    page.route(
        "**/api/logs/history**",
        lambda route: _fulfill_json(
            route,
            {
                "lines": [
                    {
                        "ts": "2026-10-04 10:00:00.000",
                        "level": "INFO",
                        "mod": "worker",
                        "func": "process",
                        "line": 42,
                        "msg": "Processing /media/Example.mkv",
                    },
                    {
                        "ts": "2026-10-04 10:01:00.000",
                        "level": "ERROR",
                        "mod": "publisher",
                        "func": "publish",
                        "line": 17,
                        "msg": "Permission denied <script>alert(1)</script>",
                    },
                ],
                "has_more": False,
            },
        ),
    )


def test_logs_raw_structured_group_filter_and_download_keep_facts(authed_page, app_url, complete_setup):
    _logs(authed_page)
    authed_page.goto(app_url + "/logs")
    expect(authed_page.locator(".log-line")).to_have_count(2)
    authed_page.locator("#logGroupSource").check()
    expect(authed_page.locator(".log-source-heading")).to_have_count(2)
    authed_page.locator("#logSource").select_option("publisher")
    expect(authed_page.locator("#logCount")).to_have_text("1 of 2 lines")
    authed_page.locator(".log-line:not(.log-line-hidden)").click()
    authed_page.locator("#logView").select_option("raw")
    expect(authed_page.locator(".log-line:not(.log-line-hidden) .log-detail")).to_be_hidden()
    expect(authed_page.locator(".log-line:not(.log-line-hidden) .log-raw")).to_contain_text("publisher.publish:17")
    with authed_page.expect_download() as download_info:
        authed_page.locator("#logDownloadBtn").click()
    content = download_info.value.path().read_text()
    assert "publisher.publish:17" in content and "2026-10-04 10:01:00.000" in content
    assert "Permission denied <script>alert(1)</script>" in content
    assert "Processing" not in content
    authed_page.locator("#logSearchRegex").click()
    authed_page.locator("#logSearch").fill("[")
    expect(authed_page.locator("#logFeedback")).to_contain_text("invalid")


def test_logs_history_failure_is_not_empty_state(authed_page, app_url, complete_setup):
    authed_page.route("**/api/logs/history**", lambda route: _fulfill_json(route, {"error": "offline"}, status=503))
    authed_page.goto(app_url + "/logs")
    expect(authed_page.locator("#logFeedback")).to_contain_text("could not be loaded")
    expect(authed_page.locator("#logContainer")).not_to_contain_text("No log history")


def test_older_logs_failure_remains_visible_after_filtering(authed_page, app_url, complete_setup):
    _logs(authed_page)

    def history(route):
        if "before=" in route.request.url:
            _fulfill_json(route, {"error": "offline"}, status=503)
        else:
            _fulfill_json(
                route,
                {
                    "lines": [{"ts": "2026-10-04 10:00:00.000", "msg": "Existing message"}],
                    "has_more": True,
                    "oldest_ts": "2026-10-04 10:00:00.000",
                },
            )

    authed_page.route("**/api/logs/history**", history)
    authed_page.goto(app_url + "/logs")
    authed_page.locator("#loadOlderBtn").click()
    expect(authed_page.locator("#logFeedback")).to_contain_text("Older messages could not be loaded")
    authed_page.locator("#logSearch").fill("Existing")
    expect(authed_page.locator("#logFeedback")).to_contain_text("Older messages could not be loaded")
    expect(authed_page.locator("#loadOlderBtn")).to_be_enabled()


def test_pending_resume_cannot_override_new_vendor_choice(wizard_page, app_url_wizard):
    _resume(wizard_page, "emby")
    pending = []
    wizard_page.route("**/api/servers/saved-server", lambda route: pending.append(route))
    wizard_page.goto(app_url_wizard + "/setup")
    wizard_page.wait_for_timeout(200)
    assert pending
    wizard_page.locator('.wizard-vendor-btn[data-vendor="plex"]').click()
    _fulfill_json(pending[0], {"id": "saved-server", "type": "emby", "name": "Old Emby"})
    expect(wizard_page.locator("#plexSignInPanel")).to_be_visible()
    expect(wizard_page.locator('[data-step="1"].setup-step')).to_be_visible()
    expect(wizard_page.locator("#setupResumeNotice")).to_have_count(0)


def test_failed_emby_paths_do_not_disable_new_plex_paths(wizard_page, app_url_wizard):
    _resume(wizard_page, "emby")
    calls = []

    def server(route):
        calls.append(
            {
                "method": route.request.method,
                "body": route.request.post_data_json if route.request.method != "GET" else None,
            }
        )
        _fulfill_json(
            route, {"id": "saved-server", "type": "emby", "name": "Emby"}, status=200 if len(calls) == 1 else 503
        )

    wizard_page.route("**/api/servers/saved-server", server)
    saved_settings = capture_settings_save(wizard_page)
    mock_plex_libraries(wizard_page)
    wizard_page.goto(app_url_wizard + "/setup")
    expect(wizard_page.locator("#setupResumeNotice")).to_contain_text("Could not load this server")
    expect(wizard_page.locator("#step3Next")).to_be_disabled()
    wizard_page.locator("#step3Back").click()
    wizard_page.locator('.wizard-vendor-btn[data-vendor="plex"]').click()
    wizard_page.locator("#manualConnectDetails").evaluate("el => el.open = true")
    wizard_page.locator("#manualPlexUrl").fill("http://plex.test:32400")
    wizard_page.locator("#manualPlexToken").fill("fixture-token")
    wizard_page.locator("#manualPlexTestBtn").click()
    expect(wizard_page.locator("#step1Next")).to_be_enabled()
    wizard_page.locator("#step1Next").click()
    wizard_page.locator(".library-card").first.click()
    wizard_page.locator("#step2Next").click()
    expect(wizard_page.locator("#step3PlexConfigSection")).to_be_visible()
    expect(wizard_page.locator("#step3Next")).to_be_enabled()
    wizard_page.locator(".path-mapping-plex").fill("/plex-movies")
    wizard_page.locator(".path-mapping-local").fill("/only-plex")
    wizard_page.locator("#step3Next").click()
    expect(wizard_page.locator('[data-step="4"].setup-step')).to_be_visible()
    assert saved_settings[-1]["path_mappings"][0]["local_prefix"] == "/only-plex"
    assert all(call["method"] == "GET" for call in calls)
