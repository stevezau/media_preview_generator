"""Overlays redesign: creation dialogs, Job details, navbar menus, notifications, toasts and the info dialog.

Each creation dialog is driven through its redesigned controls (type cards, segmented controls, switches) and the
exact request it sends is asserted, so a restyle cannot change what the app submits.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, Route, expect

from ._mocks import _fulfill_json, mock_dashboard_defaults, mock_media_servers_status, mock_servers_list, pick_job_kind
from .conftest import expect_modal_shown, watch_modal_shown

pytestmark = pytest.mark.e2e

_SERVERS = [
    {"id": "plex-1", "name": "Home Plex", "type": "plex", "enabled": True, "status": "connected"},
    {"id": "jf-1", "name": "Home Jellyfin", "type": "jellyfin", "enabled": True, "status": "connected"},
]
_LIBRARIES = [
    {"id": "1", "name": "Movies", "type": "movie", "server_id": "plex-1", "server_name": "Home Plex"},
    {"id": "2", "name": "TV Shows", "type": "show", "server_id": "plex-1", "server_name": "Home Plex"},
    {"id": "1", "name": "Anime", "type": "show", "server_id": "jf-1", "server_name": "Home Jellyfin"},
]
_NOTIFICATIONS = {
    "notifications": [
        {
            "id": "vk",
            "severity": "warning",
            "title": "Vulkan GPU probe failed",
            "source": "vulkan_probe",
            "body_html": "<p>HDR previews use the CPU path.</p>",
        },
        {
            "id": "mnt",
            "severity": "error",
            "title": "Media folder not mounted",
            "body_html": "<p>/data/movies is empty.</p>",
            "permanent_dismissable": False,
        },
    ]
}


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


@pytest.fixture
def overlay_page(authed_page: Page, app_url: str) -> tuple[Page, list[dict]]:
    """The dashboard with servers, libraries and notifications mocked; every write is captured and answered."""
    page = authed_page
    mock_dashboard_defaults(page)
    mock_media_servers_status(page, servers=_SERVERS)
    mock_servers_list(page, servers=_SERVERS)
    page.route("**/api/libraries**", lambda route: _fulfill_json(route, {"libraries": _LIBRARIES}))
    page.route("**/api/system/notifications", lambda route: _fulfill_json(route, _NOTIFICATIONS))
    page.route("**/api/schedules", lambda route: _fulfill_json(route, {"schedules": []}))
    writes: list[dict] = []

    def capture(route: Route) -> None:
        request = route.request
        if request.method in {"POST", "PUT"} and "/api/system/notifications" not in request.url:
            writes.append({"url": request.url.split("/api/", 1)[1], "body": request.post_data_json})
            _fulfill_json(route, {"id": "created", "job_id": "created", "status": "pending"})
        else:
            route.fallback()

    page.route("**/api/**", capture)
    page.goto(f"{app_url}/")
    page.wait_for_function("libraries.length === 3")
    return page, writes


def _segment(page: Page, select_id: str, name: str):
    return page.locator(f"#{select_id} + .ov-seg").get_by_role("radio", name=name)


def _open_start(page: Page) -> None:
    watch_modal_shown(page, "newJobModal")
    page.locator('.dashboard-quick-card button:has-text("Start new job")').click()
    expect_modal_shown(page, "newJobModal")


class TestStartNewJob:
    def test_previews_job_posts_servers_libraries_priority_and_filters(self, overlay_page) -> None:
        page, writes = overlay_page
        _open_start(page)
        expect(page.locator("#jobStartLabel")).to_have_text("Start previews job")
        expect(page.locator("#jobScopeBadge")).to_contain_text("every enabled library on all servers")
        page.locator("#jobLibraryAll").uncheck()
        page.locator('.job-library-checkbox[data-server-id="plex-1"][value="1"]').check()
        expect(page.locator("#jobScopeBadge")).to_contain_text("Home Plex")
        expect(page.locator("#jobScopeBadge")).to_have_attribute("data-scope", "one")
        page.locator('.job-library-checkbox[data-server-id="jf-1"]').check()
        expect(page.locator("#jobScopeBadge")).to_have_attribute("data-scope", "many")
        page.locator('.job-library-checkbox[data-server-id="jf-1"]').uncheck()
        _segment(page, "jobPriority", "High").click()
        expect(page.locator("#jobPriority")).to_have_value("1")
        page.locator("#jobRegenerateAll").check()
        expect(page.locator("#jobProcessingModeHint")).to_contain_text("Rebuilds every preview")
        page.locator("#jobSortBy").select_option("newest")
        page.locator("#jobScanFilters > summary").click()
        page.locator("#jobAddedFilter").select_option("last_days")
        page.locator("#jobAddedLastDays").fill("14")
        expect(page.locator("#jobSummary")).to_contain_text("1 library")
        page.locator("#jobStartButton").click()
        expect(page.locator("#newJobModal")).to_be_hidden()
        assert [w for w in writes if w["url"] == "jobs"][0]["body"] == {
            "library_ids": ["1"],
            "library_name": "Movies",
            "priority": 1,
            "config": {"force_generate": True, "sort_by": "newest", "added_filter": "last_days", "added_last_days": 14},
            "server_id": "plex-1",
        }

    def test_filter_errors_show_inline_beside_the_field(self, overlay_page) -> None:
        page, writes = overlay_page
        _open_start(page)
        page.locator("#jobScanFilters > summary").click()
        page.locator("#jobMovieYearMode").select_option("range")
        page.locator("#jobStartButton").click()
        expect(page.locator('#jobScanFilters [data-filter-error="movie_year_from"]')).to_contain_text(
            "Enter at least one release year."
        )
        assert not [w for w in writes if w["url"] == "jobs"]

    def test_no_library_ticked_shows_the_toast_and_an_inline_error(self, overlay_page) -> None:
        page, writes = overlay_page
        _open_start(page)
        page.locator("#jobLibraryAll").uncheck()
        page.locator("#jobStartButton").click()
        expect(page.locator("#toastBody")).to_have_text("Please select at least one library")
        expect(page.locator("#jobLibrariesError")).to_be_visible()
        assert writes == []

    @pytest.mark.parametrize(
        ("radio", "label", "url", "priority"),
        [
            ("jobKindMarkers", "Start intro & credits job", "markers/jobs", 3),
            ("jobKindLoudness", "Start loudness job", "loudness/jobs", 3),
        ],
    )
    def test_own_runner_jobs_post_server_library_pairs_at_low_priority(
        self, overlay_page, radio: str, label: str, url: str, priority: int
    ) -> None:
        page, writes = overlay_page
        _open_start(page)
        pick_job_kind(page, radio)
        expect(page.locator("#jobStartLabel")).to_have_text(label)
        expect(page.locator("#jobPriority")).to_have_value(str(priority))
        expect(_segment(page, "jobPriority", "Low")).to_have_attribute("aria-checked", "true")
        expect(page.locator("#jobProcessingModeGroup")).to_be_hidden()
        expect(page.locator("#jobOwnRunnerFiltersNote")).to_be_visible()
        page.locator("#jobLibraryAll").uncheck()
        page.locator('.job-library-checkbox[data-server-id="jf-1"]').check()
        page.locator("#jobStartButton").click()
        expect(page.locator("#newJobModal")).to_be_hidden()
        body = [w for w in writes if w["url"] == url][0]["body"]
        assert body["libraries"] == [{"server_id": "jf-1", "library_id": "1"}]
        assert body["priority"] == priority

    def test_check_servers_hides_the_libraries_and_posts_the_reconcile_request(self, overlay_page) -> None:
        page, writes = overlay_page
        _open_start(page)
        pick_job_kind(page, "jobKindMarkers")
        page.locator("#jobMarkersModeCheckServers").check()
        expect(page.locator("#jobLibrariesGroup")).to_be_hidden()
        expect(page.locator("#jobCheckServersNote")).to_be_visible()
        expect(page.locator("#jobStartLabel")).to_have_text("Check servers")
        _segment(page, "jobPriority", "Normal").click()
        page.locator("#jobStartButton").click()
        assert [w for w in writes if w["url"] == "markers/reconcile"][0]["body"] == {"priority": 2}

    def test_type_cards_colour_the_dialog_by_the_first_ticked_type(self, overlay_page) -> None:
        page, _ = overlay_page
        _open_start(page)
        expect(page.locator("#newJobModal")).to_have_attribute("data-kind", "previews")
        pick_job_kind(page, "jobKindMarkers")
        expect(page.locator("#newJobModal")).to_have_attribute("data-kind", "intro_credits")
        pick_job_kind(page, "jobKindLoudness")
        expect(page.locator("#newJobModal")).to_have_attribute("data-kind", "loudness")

    def test_the_last_ticked_type_cannot_be_unticked(self, overlay_page) -> None:
        page, _ = overlay_page
        _open_start(page)
        page.locator("#jobKindPreviews").click()
        expect(page.locator("#jobKindPreviews")).to_be_checked()

    def test_ticking_several_types_posts_one_job_each_previews_first(self, overlay_page) -> None:
        page, writes = overlay_page
        _open_start(page)
        page.locator("#jobKindMarkers").check()
        page.locator("#jobKindLoudness").check()
        expect(page.locator("#jobStartLabel")).to_have_text("Start 3 jobs")
        expect(page.locator("#jobPriority")).to_have_value("2")
        expect(page.locator("#jobProcessingModeGroup")).to_be_visible()
        expect(page.locator("#jobMarkersForce")).to_be_visible()
        page.locator("#jobStartButton").click()
        expect(page.locator("#newJobModal")).to_be_hidden()
        posted = [w["url"] for w in writes if w["url"] in ("jobs", "markers/jobs", "loudness/jobs")]
        assert posted == ["jobs", "markers/jobs", "loudness/jobs"]
        expect(page.locator("#toastBody")).to_have_text("3 jobs have been started")

    def test_unticking_previews_hides_its_options_and_defaults_to_low(self, overlay_page) -> None:
        page, _ = overlay_page
        _open_start(page)
        page.locator("#jobKindLoudness").check()
        page.locator("#jobKindPreviews").uncheck()
        expect(page.locator("#jobProcessingModeGroup")).to_be_hidden()
        expect(page.locator("#jobScanFiltersGroup")).to_be_hidden()
        expect(page.locator("#jobPriority")).to_have_value("3")
        expect(page.locator("#jobStartLabel")).to_have_text("Start loudness job")

    def test_check_servers_clears_the_other_types_and_ticking_one_leaves_it(self, overlay_page) -> None:
        page, _ = overlay_page
        _open_start(page)
        page.locator("#jobKindMarkers").check()
        page.locator("#jobMarkersModeCheckServers").check()
        expect(page.locator("#jobKindPreviews")).not_to_be_checked()
        expect(page.locator("#jobKindLoudness")).not_to_be_checked()
        page.locator("#jobKindLoudness").check()
        expect(page.locator("#jobMarkersModeFind")).to_be_checked()
        expect(page.locator("#jobLibrariesGroup")).to_be_visible()


class TestManualTrigger:
    @pytest.mark.parametrize(
        ("radio", "label", "url", "expected"),
        [
            (
                "manualKindPreviews",
                "Start previews job",
                "jobs/manual",
                {"file_paths": ["/data/tv/Show"], "priority": 1, "force_regenerate": True},
            ),
            (
                "manualKindMarkers",
                "Start intro & credits job",
                "markers/jobs",
                {"file_paths": ["/data/tv/Show"], "priority": 1, "force": True},
            ),
            (
                "manualKindLoudness",
                "Start loudness job",
                "loudness/jobs",
                {"file_paths": ["/data/tv/Show"], "priority": 1},
            ),
        ],
    )
    def test_typed_path_and_options_post_the_kinds_payload(
        self, overlay_page, radio: str, label: str, url: str, expected: dict
    ) -> None:
        page, writes = overlay_page
        page.get_by_role("button", name="Process a file or folder", exact=True).click()
        expect(page.locator("#manualTriggerModal")).to_be_visible()
        expect(page.locator("#manualStartButton")).to_be_disabled()
        page.locator(f"#{radio}").check()
        expect(page.locator("#manualStartLabel")).to_have_text(label)
        expect(page.locator("#manualKindHelp")).not_to_be_empty()
        assert len(page.locator("#manualKindHelp").inner_text()) <= 90
        page.locator("#manualAdvanced summary").click()
        page.locator("#manualFilePaths").fill("/data/tv/Show")
        expect(page.locator("#manualStartButton")).to_be_enabled()
        if radio == "manualKindPreviews":
            page.locator("#manualForceRegenerate").check()
            expect(page.locator("#manualProcessingModeHint")).to_contain_text("Rebuilds every preview")
        if radio == "manualKindMarkers":
            page.locator("#manualMarkersForce").check()
        _segment(page, "manualPriority", "High").click()
        page.locator("#manualStartButton").click()
        expect(page.locator("#manualTriggerModal")).to_be_hidden()
        assert [w for w in writes if w["url"] == url][0]["body"] == expected

    def test_relative_path_shows_the_inline_error_and_keeps_start_disabled(self, overlay_page) -> None:
        page, writes = overlay_page
        page.get_by_role("button", name="Process a file or folder", exact=True).click()
        page.locator("#manualAdvanced summary").click()
        page.locator("#manualFilePaths").fill("relative/path")
        expect(page.locator("#manualPathValidation")).to_contain_text("Use absolute container paths")
        expect(page.locator("#manualStartButton")).to_be_disabled()
        assert writes == []


def _schedule_page(page: Page, app_url: str) -> None:
    page.goto(f"{app_url}/automation#section-schedules-list")
    page.wait_for_function("libraries.length === 3")
    page.evaluate("showNewScheduleModal()")
    expect(page.locator("#newScheduleModal")).to_be_visible()


class TestSchedule:
    def test_specific_time_posts_days_as_cron_and_summarises_them(self, overlay_page, app_url: str) -> None:
        page, writes = overlay_page
        _schedule_page(page, app_url)
        expect(page.locator("#scheduleWhenSummary")).to_contain_text("Mon to Fri at 02:00")
        page.locator("#scheduleName").fill("Nightly")
        page.locator("#scheduleTime").fill("03:30")
        page.locator("#daySat").check()
        expect(page.locator("#scheduleWhenSummary")).to_contain_text("Mon to Sat at 03:30")
        page.locator("#scheduleStopTime").fill("06:00")
        _segment(page, "schedulePriority", "High").click()
        page.locator("#scheduleSubmitBtn").click()
        expect(page.locator("#newScheduleModal")).to_be_hidden()
        body = [w for w in writes if w["url"] == "schedules"][0]["body"]
        assert body["cron_expression"] == "30 3 * * 0,1,2,3,4,5"
        assert body["stop_time"] == "06:00"
        assert body["priority"] == 1
        assert body["enabled"] is True
        assert body["config"]["job_type"] == "full_library"

    def test_interval_posts_minutes_and_drops_the_stop_time(self, overlay_page, app_url: str) -> None:
        page, writes = overlay_page
        _schedule_page(page, app_url)
        page.locator("#scheduleName").fill("Often")
        page.locator("#scheduleTypeInterval").check()
        expect(page.locator("#scheduleStopTimeGroup")).to_be_hidden()
        page.locator("#scheduleIntervalValue").fill("3")
        page.locator("#scheduleEnabled").uncheck()
        page.locator("#scheduleSubmitBtn").click()
        body = [w for w in writes if w["url"] == "schedules"][0]["body"]
        assert body["interval_minutes"] == 180
        assert body["stop_time"] == ""
        assert body["enabled"] is False
        assert body["priority"] is None

    def test_cron_posts_the_expression_and_rejects_four_fields_inline(self, overlay_page, app_url: str) -> None:
        page, writes = overlay_page
        _schedule_page(page, app_url)
        page.locator("#scheduleName").fill("Cron")
        page.locator("#scheduleTypeCron").check()
        page.locator("#scheduleCronInput").fill("0 */2 * *")
        page.locator("#scheduleSubmitBtn").click()
        expect(page.locator("#scheduleCronError")).to_contain_text("must have 5 fields")
        assert writes == []
        page.locator("#scheduleCronInput").fill("0 */2 * * *")
        expect(page.locator("#scheduleCronError")).to_be_hidden()
        page.locator("#scheduleSubmitBtn").click()
        assert [w for w in writes if w["url"] == "schedules"][0]["body"]["cron_expression"] == "0 */2 * * *"

    def test_scan_modes_post_their_job_type_and_check_servers_hides_scope(self, overlay_page, app_url: str) -> None:
        page, writes = overlay_page
        _schedule_page(page, app_url)
        page.locator("#scheduleName").fill("Recent")
        page.locator("#scanModeRecent").check()
        expect(page.locator("#scheduleIntervalValue")).to_have_value("15")
        expect(page.locator("#scheduleLookbackGroup")).to_be_visible()
        page.locator("#scanModeMarkers").check()
        page.locator("#scheduleMarkersCheckServers").check()
        expect(page.locator("#scheduleScopeSection")).to_be_hidden()
        expect(page.locator("#scheduleCheckServersNote")).to_be_visible()
        expect(page.locator("#newScheduleModal")).to_have_attribute("data-kind", "check")
        page.locator("#scheduleSubmitBtn").click()
        config = [w for w in writes if w["url"] == "schedules"][0]["body"]["config"]
        assert config == {"job_type": "intro_credits", "reconcile": True}

    def test_missing_name_and_days_show_inline_errors(self, overlay_page, app_url: str) -> None:
        page, writes = overlay_page
        _schedule_page(page, app_url)
        page.locator("#scheduleSubmitBtn").click()
        expect(page.locator("#scheduleNameError")).to_have_text("Name is required.")
        page.locator("#scheduleName").fill("x")
        for day in ("Mon", "Tue", "Wed", "Thu", "Fri"):
            page.locator(f"#day{day}").uncheck()
        page.locator("#scheduleSubmitBtn").click()
        expect(page.locator("#scheduleDaysError")).to_have_text("Select at least one day.")
        assert writes == []

    def test_edit_schedule_prefills_and_puts_the_change(self, overlay_page, app_url: str) -> None:
        page, writes = overlay_page
        page.route(
            "**/api/schedules",
            lambda route: _fulfill_json(
                route,
                {
                    "schedules": [
                        {
                            "id": "sched-1",
                            "name": "Weekend",
                            "enabled": True,
                            "trigger_type": "interval",
                            "trigger_value": "120",
                            "priority": 3,
                            "library_ids": [],
                            "config": {"job_type": "full_library"},
                        }
                    ]
                },
            ),
        )
        page.goto(f"{app_url}/automation#section-schedules-list")
        page.wait_for_function("schedules.some(s => s.id === 'sched-1') && libraries.length === 3")
        page.evaluate("showEditScheduleModal('sched-1')")
        expect(page.locator("#scheduleModalTitle")).to_have_text("Edit Schedule")
        expect(page.locator("#scheduleName")).to_have_value("Weekend")
        expect(page.locator("#scheduleTypeInterval")).to_be_checked()
        expect(_segment(page, "schedulePriority", "Low")).to_have_attribute("aria-checked", "true")
        expect(page.locator("#scheduleSubmitBtn")).to_contain_text("Save Changes")
        page.locator("#scheduleIntervalValue").fill("4")
        page.locator("#scheduleSubmitBtn").click()
        body = [w for w in writes if w["url"] == "schedules/sched-1"][0]["body"]
        assert body["interval_minutes"] == 240
        assert body["priority"] == 3


def _open_job(page: Page, job: dict, width: int) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    page.route("**/api/jobs/*/logs**", lambda route: _fulfill_json(route, {"logs": [], "total_lines": 0}))
    page.route(
        "**/api/jobs/*/files**",
        lambda route: _fulfill_json(
            route,
            {
                "files": [{"file": "/media/film.mkv", "outcome": "generated", "worker": "GPU 1"}],
                "total": 1,
                "filtered_count": 1,
                "total_pages": 1,
                "page": 1,
            },
        ),
    )
    page.evaluate("job => { jobs = [job]; openJobDetails(job.id); }", job)
    expect(page.locator("#logsModal")).to_be_visible()


_JOB = {
    "id": "job-detail-1",
    "library_name": "Movies",
    "kind": "previews",
    "status": "failed",
    "error": "Plex returned 401",
    "priority": 2,
    "created_at": "2026-10-01T10:00:00+00:00",
    "started_at": "2026-10-01T10:00:05+00:00",
    "completed_at": "2026-10-01T10:00:20+00:00",
    "config": {"source": "manual", "file_paths": ["/media/film.mkv"], "file_paths_count": 1},
    "progress": {"processed_items": 1, "total_items": 1},
    "publishers": [
        {"server_id": "plex-1", "server_name": "Home Plex", "server_type": "plex", "counts": {"published": 1}}
    ],
}


@pytest.mark.parametrize("width", [1440, 390])
class TestJobDetails:
    def test_facts_error_tabs_and_log_tools_are_visible(self, overlay_page, width: int) -> None:
        page, _ = overlay_page
        _open_job(page, _JOB, width)
        modal = page.locator("#logsModal")
        expect(modal.locator("#jobDetailsError")).to_contain_text("Plex returned 401")
        facts = modal.locator(".job-details-facts")
        for text in ("Files", "1 of 1 processed", "Priority", "Normal", "Created", "Started", "Finished"):
            expect(facts).to_contain_text(text)
        expect(modal.locator("#logsJobId")).to_contain_text("job-detail-1")
        expect(modal.get_by_role("button", name="Copy Job ID", exact=True)).to_be_visible()
        for label in ("Copy", "Download", "Refresh"):
            expect(modal.locator("#logsTabPane").get_by_role("button", name=label, exact=True)).to_be_visible()
        expect(modal.get_by_role("textbox", name="Filter logs", exact=True)).to_be_visible()
        expect(modal.locator("#logsAutoScroll")).to_be_attached()
        modal.get_by_role("tab", name="Files", exact=True).click()
        expect(modal.locator("#fileResultsBody")).to_contain_text("film.mkv")
        expect(modal.locator("#filesTabCount")).to_have_text("1")
        expect(modal.locator("#fileOutcomeFilter optgroup:visible, #fileOutcomeFilter optgroup")).not_to_have_count(0)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

    def test_job_actions_live_in_the_footer_and_the_sheet_fills_a_phone(self, overlay_page, width: int) -> None:
        page, writes = overlay_page
        _open_job(page, _JOB, width)
        footer = page.locator("#logsModal .modal-footer")
        expect(page.locator("#opActionReprocess")).to_have_text("Re-run job")
        expect(page.locator("#opActionRetryNow")).to_be_hidden()
        expect(page.locator("#opActionCancelChain")).to_be_hidden()
        expect(footer.get_by_role("button", name="Close", exact=True)).to_be_visible()
        for label in ("Copy", "Download"):
            expect(footer.get_by_role("button", name=label, exact=True)).to_have_count(0)
        box = page.locator("#logsModal .modal-content").bounding_box()
        if width == 390:
            assert box["width"] >= 389 and box["height"] >= 840
        page.locator("#opActionReprocess").click()
        assert [w for w in writes if "reprocess" in w["url"] or "rerun" in w["url"] or w["url"].startswith("jobs")]

    def test_retry_chain_shows_attempts_and_operator_actions(self, overlay_page, width: int) -> None:
        page, _ = overlay_page
        chain = {
            **_JOB,
            "id": "chain-1",
            "status": "pending",
            "error": None,
            "config": {"is_retry_chain": True, "retry_attempt": 1, "retry_max_attempts": 5, "source": "webhook"},
            "progress": {"retry_eta": "2999-01-01T00:00:00+00:00", "processed_items": 0, "total_items": 1},
        }
        page.route("**/api/jobs/chain-1/attempts**", lambda route: _fulfill_json(route, {"attempts": []}))
        _open_job(page, chain, width)
        expect(page.locator("#attemptsDropdownWrap")).to_be_visible()
        expect(page.locator("#opActionRetryNow")).to_be_visible()
        expect(page.locator("#opActionCancelChain")).to_be_visible()
        expect(page.locator("#logsLiveHint")).to_be_hidden()


class TestNavbarMenus:
    @pytest.mark.parametrize(
        ("toggle", "menu", "count"),
        [("#navAutomationDropdown", "#navAutomationMenu", 2), ("#navSettingsDropdown", "#navSettingsMenu", 8)],
    )
    def test_menu_opens_by_keyboard_and_caret_and_closes_on_escape(
        self, overlay_page, toggle: str, menu: str, count: int
    ) -> None:
        page, _ = overlay_page
        page.set_viewport_size({"width": 1440, "height": 900})
        page.locator(toggle).focus()
        page.keyboard.press("ArrowDown")
        expect(page.locator(menu)).to_be_visible()
        expect(page.locator(f"{menu} .dropdown-item")).to_have_count(count)
        expect(page.locator(f"{menu} .dropdown-item small").first).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.locator(menu)).to_be_hidden()
        box = page.locator(toggle).bounding_box()
        page.mouse.click(box["x"] + box["width"] - 10, box["y"] + box["height"] / 2)
        expect(page.locator(menu)).to_be_visible()
        assert page.url.endswith("/")

    def test_settings_menu_has_two_groups_and_tools_keeps_run_setup_again(self, overlay_page) -> None:
        page, _ = overlay_page
        page.set_viewport_size({"width": 1440, "height": 900})
        page.locator("#navSettingsDropdown").hover()
        expect(page.locator("#navSettingsMenu .dropdown-header")).to_have_text(["Processing", "System"])
        page.locator("#navToolsDropdown").hover()
        expect(page.locator("#navToolsMenu .dropdown-item b")).to_have_text(
            ["Inspector", "Logs", "Webhook Activity", "Run setup again"]
        )
        expect(page.locator('#navToolsMenu a[href="/setup?rerun=1"]')).to_be_visible()

    def test_help_menu_lists_five_links(self, overlay_page) -> None:
        page, _ = overlay_page
        page.set_viewport_size({"width": 1440, "height": 900})
        page.locator("#helpMenuBtn").click()
        expect(page.locator('.dropdown-menu[aria-labelledby="helpMenuBtn"] .dropdown-item')).to_have_count(5)

    def test_phone_menu_lists_every_item_and_pins_logout(self, overlay_page) -> None:
        page, _ = overlay_page
        page.set_viewport_size({"width": 390, "height": 844})
        page.locator(".navbar-toggler").click()
        drawer = page.locator("#navbarNav")
        expect(drawer).to_be_visible()
        for label in ("Dashboard", "Servers", "Automation", "Settings", "Tools"):
            expect(drawer.get_by_role("link", name=label).first).to_be_visible()
        for toggle, menu, count in (
            ("#navAutomationDropdown", "#navAutomationMenu", 2),
            ("#navSettingsDropdown", "#navSettingsMenu", 8),
            ("#navToolsDropdown", "#navToolsMenu", 4),
        ):
            page.locator(toggle).click()
            expect(page.locator(f"{menu} .dropdown-item")).to_have_count(count)
            expect(page.locator(f"{menu} .dropdown-item").first).to_be_visible()
        expect(drawer.locator("#notificationBellLabel")).to_have_text("2 notifications")
        for name in ("Help & feedback", "Buy me a coffee", "Star on GitHub", "Switch to light theme"):
            expect(drawer.get_by_text(name)).to_be_visible()
        expect(drawer.locator("#navLogoutBtn")).to_be_in_viewport()
        assert page.evaluate("document.documentElement.scrollWidth") == 390


class TestNotificationsToastsAndInfo:
    def test_bell_shows_severity_cards_and_the_dismiss_rules(self, overlay_page) -> None:
        page, _ = overlay_page
        page.set_viewport_size({"width": 1440, "height": 900})
        expect(page.locator("#notificationBellBadge")).to_have_text("2")
        page.locator("#notificationBellBtn").click()
        warning = page.locator('.notification-entry[data-notification-id="vk"]')
        expect(warning).to_have_class(__import__("re").compile("ov-nt-warning"))
        expect(page.locator('.notification-entry[data-notification-id="mnt"]')).to_have_class(
            __import__("re").compile("ov-nt-error")
        )
        warning.get_by_text("Show details").click()
        expect(warning.get_by_text("Copy diagnostic bundle")).to_be_visible()
        expect(warning.get_by_role("button", name="Dismiss permanently")).to_be_visible()
        expect(
            page.locator('.notification-entry[data-notification-id="mnt"]').get_by_role(
                "button", name="Dismiss permanently"
            )
        ).to_have_count(0)
        expect(page.locator("#notificationResetBtn")).to_have_text("Restore dismissed")

    def test_toasts_stack_up_to_three_and_errors_stay_until_closed(self, overlay_page) -> None:
        page, _ = overlay_page
        page.evaluate(
            "() => { showToast('One', 'first', 'success'); showToast('Two', 'second', 'danger');"
            " showToast('Three', 'third', 'warning'); showToast('Four', 'fourth', 'info'); }"
        )
        expect(page.locator(".toast-container .toast.show")).to_have_count(3)
        expect(page.locator("#toastTitle")).to_have_text("Four")
        expect(page.locator(".toast-container .toast[data-type='danger']")).to_have_count(1)
        page.wait_for_timeout(5600)
        expect(page.locator(".toast-container .toast[data-type='danger']")).to_be_visible()
        expect(page.locator("#toastNotification")).to_be_hidden()

    def test_info_dialog_opens_closes_and_returns_focus_to_the_icon(self, overlay_page) -> None:
        page, _ = overlay_page
        _open_start(page)
        info = page.locator("#jobKindMarkersInfo")
        info.focus()
        watch_modal_shown(page, "globalInfoModal")
        page.keyboard.press("Enter")
        expect_modal_shown(page, "globalInfoModal")
        expect(page.locator("#globalInfoTitle")).to_have_text("Intro & Credits job")
        page.keyboard.press("Escape")
        expect(page.locator("#globalInfoModal")).to_be_hidden()
        expect(info).to_be_focused()

    def test_confirm_focuses_cancel_for_danger_and_resolves_false_on_escape(self, overlay_page) -> None:
        page, _ = overlay_page
        page.evaluate(
            "() => { window._answer = null; appConfirm('Delete it?', {title: 'Delete', confirmText: 'Delete'})"
            ".then(v => { window._answer = v; }); }"
        )
        expect(page.locator("#appConfirmModal")).to_be_visible()
        expect(page.locator("#appConfirmModalCancelBtn")).to_be_focused()
        expect(page.locator("#appConfirmModalOkBtn")).to_have_text("Delete")
        expect(page.locator("#appConfirmModalIconTile")).to_have_attribute("data-variant", "danger")
        page.keyboard.press("Escape")
        page.wait_for_function("window._answer === false")
