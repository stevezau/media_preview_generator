"""E2E tests for the dashboard quick actions, worker-card Logs button, long names and queue columns."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from playwright.sync_api import Page, expect

from ._mocks import _fulfill_json, mock_dashboard_defaults, mock_worker_groups

LONG_DEVICE = "Intel Corporation Raptor Lake-S GT1 [UHD Graphics 770] (rev 04)"


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


def _schedule(schedule_id: str, name: str, hours: int) -> dict:
    return {
        "id": schedule_id,
        "name": name,
        "enabled": True,
        "next_run": (datetime.now(UTC) + timedelta(hours=hours)).isoformat(),
    }


def _worker(job_id: str | None = "abcd1234-0000-4000-8000-000000000001") -> dict:
    return {
        "worker_id": 1,
        "worker_type": "CPU",
        "worker_name": "CPU workers 1",
        "group_id": "cpu",
        "group_name": "CPU workers",
        "status": "processing" if job_id else "idle",
        "progress_percent": 10,
        "current_title": "Movie",
        "job_id": job_id,
    }


@pytest.mark.e2e
class TestQuickActions:
    def test_quick_actions_shows_four_tiles_and_no_libraries_tile(self, authed_page: Page, app_url: str) -> None:
        mock_dashboard_defaults(authed_page)
        mock_worker_groups(authed_page)
        authed_page.goto(app_url + "/")
        quick = authed_page.locator(".dashboard-quick-card")
        expect(quick.locator(".dash-tile-link")).to_have_count(4)
        expect(quick.locator("#libraryList")).to_have_count(0)
        expect(quick).not_to_contain_text("libraries on")
        for link, href in (("Inspector", "/inspector"), ("Webhook activity", "/webhook-activity"), ("Logs", "/logs")):
            expect(quick.get_by_role("link", name=link)).to_have_attribute("href", href)
        heights = quick.locator(".dash-tile-link").evaluate_all("els => els.map(e => e.getBoundingClientRect().height)")
        assert all(height >= 44 for height in heights)

    def test_run_next_schedule_tile_is_disabled_when_there_are_no_schedules(
        self, authed_page: Page, app_url: str
    ) -> None:
        mock_dashboard_defaults(authed_page)
        mock_worker_groups(authed_page)
        authed_page.goto(app_url + "/")
        tile = authed_page.locator("#quickRunNextSchedule")
        expect(tile).to_be_disabled()
        expect(tile).to_contain_text("No schedules")

    def test_run_next_schedule_tile_posts_the_soonest_enabled_schedule(self, authed_page: Page, app_url: str) -> None:
        mock_dashboard_defaults(authed_page)
        mock_worker_groups(authed_page)
        schedules = [_schedule("later", "Weekly scan", 48), _schedule("soon", "Nightly scan", 3)]
        authed_page.route("**/api/schedules", lambda r: _fulfill_json(r, {"schedules": schedules}))
        runs: list[tuple[str, str]] = []

        def run(route) -> None:
            runs.append((route.request.method, route.request.url.rsplit("/api/", 1)[1]))
            _fulfill_json(route, {"success": True})

        authed_page.route("**/api/schedules/*/run", run)
        authed_page.goto(app_url + "/")
        tile = authed_page.locator("#quickRunNextSchedule")
        expect(tile).to_contain_text("Run Nightly scan now")
        tile.click()
        expect(authed_page.locator(".toast", has_text="queued")).to_be_visible()
        assert runs == [("POST", "schedules/soon/run")]

    def test_run_next_schedule_failure_shows_an_error_toast(self, authed_page: Page, app_url: str) -> None:
        mock_dashboard_defaults(authed_page)
        mock_worker_groups(authed_page)
        authed_page.route(
            "**/api/schedules", lambda r: _fulfill_json(r, {"schedules": [_schedule("soon", "Nightly scan", 3)]})
        )
        authed_page.route(
            "**/api/schedules/*/run", lambda r: r.fulfill(status=500, json={"error": "Scheduler is down"})
        )
        authed_page.goto(app_url + "/")
        authed_page.locator("#quickRunNextSchedule").click()
        expect(authed_page.locator(".toast", has_text="Scheduler is down")).to_be_visible()

    def test_webhook_activity_tile_pill_shows_pending_count_and_hides_at_zero(
        self, authed_page: Page, app_url: str
    ) -> None:
        mock_dashboard_defaults(authed_page)
        mock_worker_groups(authed_page)
        authed_page.route("**/api/webhooks/pending", lambda r: _fulfill_json(r, {"pending": [{"id": 1}, {"id": 2}]}))
        authed_page.goto(app_url + "/")
        pill = authed_page.locator("#quickWebhookPending")
        expect(pill).to_have_text("2")
        authed_page.route("**/api/webhooks/pending", lambda r: _fulfill_json(r, {"pending": []}))
        authed_page.evaluate("loadPendingWebhooks()")
        expect(pill).to_be_hidden()


@pytest.mark.e2e
class TestWorkerCardLogs:
    def _open(self, page: Page, app_url: str, worker: dict) -> None:
        mock_dashboard_defaults(page)
        mock_worker_groups(page)
        page.route("**/api/jobs/workers", lambda r: _fulfill_json(r, {"workers": [worker]}))
        page.goto(app_url + "/")

    def test_running_worker_card_has_a_logs_button_that_opens_that_jobs_logs(
        self, authed_page: Page, app_url: str
    ) -> None:
        self._open(authed_page, app_url, _worker())
        authed_page.evaluate("window.__opened = []; window.openJobDetails = (...args) => window.__opened.push(args)")
        button = authed_page.get_by_role("button", name="View logs for job abcd1234-0000-4000-8000-000000000001")
        expect(button).to_be_visible()
        expect(button).to_have_attribute("title", "Job logs")
        authed_page.evaluate("window.__opened.length = 0")
        button.click()
        assert authed_page.evaluate("window.__opened") == [["abcd1234-0000-4000-8000-000000000001", "logs"]]

    def test_idle_worker_card_has_no_visible_logs_button(self, authed_page: Page, app_url: str) -> None:
        self._open(authed_page, app_url, _worker(job_id=None))
        expect(authed_page.locator("[data-worker-logs]:visible")).to_have_count(0)


@pytest.mark.e2e
class TestLongHardwareNames:
    def test_long_gpu_name_stays_on_one_line_with_the_full_name_in_title(self, authed_page: Page, app_url: str) -> None:
        mock_dashboard_defaults(authed_page)
        groups = mock_worker_groups(authed_page)
        gpu = groups["state"]["groups"][1]
        gpu["name"] = "GPU video"
        groups["state"]["hardware"][0]["name"] = LONG_DEVICE
        authed_page.set_viewport_size({"width": 1440, "height": 900})
        authed_page.goto(app_url + "/")
        # The System card line is read-only and summarises counts only, so the device name lives on the member row.
        expect(authed_page.locator('[data-system-group="gpu"] .pool-lbl small')).to_have_text("1 GPU")
        for subtitle in [authed_page.locator('[data-member-block="gpu:m1"] .devname .nm')]:
            expect(subtitle).to_have_text(LONG_DEVICE)
            expect(subtitle).to_have_attribute("title", LONG_DEVICE)
            line_height = subtitle.evaluate("e => parseFloat(getComputedStyle(e).lineHeight) || e.scrollHeight")
            assert subtitle.evaluate("e => e.getBoundingClientRect().height") <= line_height + 1
            assert subtitle.evaluate("e => getComputedStyle(e).textOverflow") == "ellipsis"

    def test_long_device_name_does_not_widen_the_page_when_viewport_is_phone_sized(
        self, authed_page: Page, app_url: str
    ) -> None:
        mock_dashboard_defaults(authed_page)
        groups = mock_worker_groups(authed_page)
        groups["state"]["groups"][1]["name"] = "UHD Graphics 770"
        groups["state"]["hardware"][0]["name"] = LONG_DEVICE
        authed_page.set_viewport_size({"width": 390, "height": 844})
        authed_page.goto(app_url + "/")
        expect(authed_page.locator('[data-member-block="gpu:m1"] .devname .nm')).to_have_text(LONG_DEVICE)
        assert authed_page.evaluate("document.documentElement.scrollWidth") <= 390


@pytest.mark.e2e
class TestWorkerGroupHeaderStaysInItsColumn:
    @pytest.mark.parametrize("width", [1600, 1440, 1280, 1100, 390])
    def test_long_hardware_name_keeps_tools_inside_their_own_group_when_three_groups_show(
        self, authed_page: Page, app_url: str, width: int
    ) -> None:
        mock_dashboard_defaults(authed_page)
        groups = mock_worker_groups(authed_page)
        groups["state"]["groups"][1]["name"] = "GPU video"
        groups["state"]["hardware"][0]["name"] = LONG_DEVICE
        groups["state"]["groups"].append(
            {
                "id": "cpu-loud",
                "name": "CPU loudness",
                "enabled": True,
                "members": [{"id": "m1", "resource": "cpu", "device": None, "count": 2, "job_types": ["loudness"]}],
                "availability": {"mode": "always", "windows": []},
            }
        )
        authed_page.set_viewport_size({"width": width, "height": 900})
        authed_page.goto(app_url + "/")
        expect(authed_page.locator('[data-member-block="gpu:m1"] .devname .nm')).to_have_text(LONG_DEVICE)
        boxes = authed_page.evaluate(
            """() => [...document.querySelectorAll('#workerGroupLiveRows > .worker-group-section')].map(section => {
                const rect = e => { const r = e.getBoundingClientRect(); return {l: r.left, r: r.right, t: r.top, b: r.bottom}; };
                return {
                    section: rect(section),
                    header: rect(section.querySelector('.worker-group-dashboard-header')),
                    tools: rect(section.querySelector('.g-tools')),
                    chip: rect(section.querySelector('.occ-chip')),
                };
            })"""
        )
        assert len(boxes) == 3
        for box in boxes:
            assert box["tools"]["r"] <= box["section"]["r"] + 0.5
            assert box["chip"]["r"] <= box["section"]["r"] + 0.5
            assert box["header"]["l"] >= box["section"]["l"] - 0.5
            assert box["header"]["r"] <= box["section"]["r"] + 0.5
        for left, right in zip(boxes, boxes[1:], strict=False):
            same_row = left["section"]["t"] < right["section"]["b"] and right["section"]["t"] < left["section"]["b"]
            if same_row:
                assert left["header"]["r"] <= right["header"]["l"] + 0.5

    def test_hardware_name_truncates_with_ellipsis_and_keeps_full_text_in_title_when_column_is_narrow(
        self, authed_page: Page, app_url: str
    ) -> None:
        mock_dashboard_defaults(authed_page)
        groups = mock_worker_groups(authed_page)
        groups["state"]["hardware"][0]["name"] = LONG_DEVICE
        groups["state"]["groups"].append({**groups["state"]["groups"][0], "id": "cpu-loud", "name": "CPU loudness"})
        authed_page.set_viewport_size({"width": 390, "height": 900})
        authed_page.goto(app_url + "/")
        hardware = authed_page.locator('[data-member-block="gpu:m1"] .devname .nm')
        expect(hardware).to_have_attribute("title", LONG_DEVICE)
        assert hardware.evaluate("e => getComputedStyle(e).textOverflow") == "ellipsis"
        assert hardware.evaluate("e => e.scrollWidth > e.clientWidth")


@pytest.mark.e2e
class TestQueuedProgressText:
    WAIT_PREVIEW = "Queued — waiting for the preview job for these files to finish"
    WAIT_SLOT = "Queued — waiting for active slot (4 of 5 busy, 1 reserved for high priority)"

    def _open(self, page: Page, app_url: str, message: str, *, wait_reason: str | None = None) -> None:
        mock_dashboard_defaults(page)
        mock_worker_groups(page)
        job = {
            "id": "12345678-aaaa-4bbb-8ccc-1234567890ab",
            "status": "pending",
            "library_name": "Movies",
            "created_at": datetime.now(UTC).isoformat(),
            "progress": {"percent": 0, "processed_items": 0, "total_items": 3, "current_item": message},
            "config": {"resource_wait": {"reason": wait_reason}} if wait_reason else {},
        }
        page.route("**/api/jobs?**", lambda r: _fulfill_json(r, {"jobs": [job], "total": 1, "page": 1}))
        page.set_viewport_size({"width": 1440, "height": 900})
        page.goto(app_url + "/")

    @pytest.mark.parametrize(
        ("message", "short"),
        [
            (WAIT_PREVIEW, "Waiting for previews to finish"),
            (WAIT_SLOT, "Waiting for a free worker"),
            ("Queued - waiting for the preview job for these files to finish", "Waiting for previews to finish"),
            ("Queued — waiting for GPU memory", "waiting for GPU memory"),
            ("Waiting for the Plex scan", "Waiting for the Plex scan"),
        ],
    )
    def test_queued_reason_is_shortened_to_one_line_with_the_full_text_in_title(
        self, authed_page: Page, app_url: str, message: str, short: str
    ) -> None:
        self._open(authed_page, app_url, message)
        cell = authed_page.locator(".job-row .queue-progress-cell")
        line = cell.locator(".queue-phase-line")
        expect(line).to_have_text(short)
        expect(line).to_have_attribute("title", message)
        expect(line).to_have_attribute("aria-label", message)
        assert "Queued" not in cell.inner_text().replace(message, "") or short.startswith("Queued")
        style = line.evaluate(
            "e => { const s = getComputedStyle(e); return [s.whiteSpace, s.textOverflow, s.overflow]; }"
        )
        assert style == ["nowrap", "ellipsis", "hidden"]
        line_height = line.evaluate("e => parseFloat(getComputedStyle(e).lineHeight)")
        assert line.evaluate("e => e.getBoundingClientRect().height") <= line_height + 1

    def test_queued_heading_is_dropped_when_a_reason_exists(self, authed_page: Page, app_url: str) -> None:
        self._open(authed_page, app_url, self.WAIT_SLOT)
        expect(authed_page.locator(".job-row .queue-progress-cell > .small")).to_have_count(0)

    def test_plain_queued_label_stays_when_no_reason_exists(self, authed_page: Page, app_url: str) -> None:
        self._open(authed_page, app_url, "")
        cell = authed_page.locator(".job-row .queue-progress-cell")
        expect(cell).to_contain_text("Queued")
        expect(cell.locator(".queue-phase-line")).to_have_count(0)

    def test_resource_wait_reason_takes_priority_over_current_item(self, authed_page: Page, app_url: str) -> None:
        self._open(authed_page, app_url, "something else", wait_reason=self.WAIT_SLOT)
        line = authed_page.locator(".job-row .queue-phase-line")
        expect(line).to_have_text("Waiting for a free worker")
        expect(line).to_have_attribute("title", self.WAIT_SLOT)


@pytest.mark.e2e
class TestExpandedJobRowLayout:
    def _open(self, page: Page, app_url: str, width: int = 1440) -> None:
        mock_dashboard_defaults(page)
        mock_worker_groups(page)
        paths = [f"/data/Movies/Film {n}/Film {n}.mkv" for n in range(8)]
        job = {
            "id": "12345678-aaaa-4bbb-8ccc-1234567890ab",
            "status": "running",
            "library_name": "Movies",
            "created_at": datetime.now(UTC).isoformat(),
            "started_at": datetime.now(UTC).isoformat(),
            "progress": {"percent": 5, "processed_items": 1, "total_items": 8, "current_item": ""},
            "config": {"file_paths": paths, "file_paths_count": 8},
        }
        page.route("**/api/jobs?**", lambda r: _fulfill_json(r, {"jobs": [job], "total": 1, "page": 1}))
        page.set_viewport_size({"width": width, "height": 900})
        page.goto(app_url + "/")
        page.locator(".job-row .exp, .job-row .job-details-toggle").first.click()
        expect(page.locator(".job-files-detail:not(.d-none) .queue-file-row").first).to_be_visible()

    @pytest.mark.parametrize("width", [1600, 1440, 1280])
    def test_path_list_uses_the_full_row_width_when_only_a_short_activity_line_exists(
        self, authed_page: Page, app_url: str, width: int
    ) -> None:
        self._open(authed_page, app_url, width)
        detail = authed_page.locator(".job-files-detail .job-expanded-content")
        files = authed_page.locator(".job-files-detail .job-detail-files")
        content_width = detail.evaluate(
            "e => e.clientWidth - parseFloat(getComputedStyle(e).paddingLeft) - parseFloat(getComputedStyle(e).paddingRight)"
        )
        assert files.evaluate("e => e.getBoundingClientRect().width") >= content_width * 0.8

    def test_every_field_is_present_and_activity_sits_above_the_paths(self, authed_page: Page, app_url: str) -> None:
        self._open(authed_page, app_url)
        detail = authed_page.locator(".job-files-detail")
        expect(detail.locator(".job-current-activity")).to_contain_text("Started")
        expect(detail.locator(".job-detail-files")).to_contain_text("Requested paths")
        expect(detail.locator(".queue-files-all")).to_be_visible()
        expect(detail.locator(".job-detail-actions")).to_contain_text("Open logs and files")
        activity_bottom = detail.locator(".job-current-activity").evaluate("e => e.getBoundingClientRect().bottom")
        files_top = detail.locator(".job-detail-files").evaluate("e => e.getBoundingClientRect().top")
        assert activity_bottom <= files_top

    def test_everything_stacks_in_one_column_when_viewport_is_phone_sized(
        self, authed_page: Page, app_url: str
    ) -> None:
        self._open(authed_page, app_url, 390)
        assert authed_page.evaluate("document.documentElement.scrollWidth") <= 390
        files = authed_page.locator(".job-files-detail .job-detail-files")
        assert files.evaluate("e => e.getBoundingClientRect().right") <= 390


@pytest.mark.e2e
class TestQueueColumns:
    def _job(self, message: str) -> dict:
        return {
            "id": "12345678-aaaa-4bbb-8ccc-1234567890ab",
            "status": "running",
            "library_name": "Movies",
            "created_at": datetime.now(UTC).isoformat(),
            "progress": {
                "percent": 0,
                "processed_items": 382,
                "total_items": 124896,
                "current_item": message,
            },
            "config": {},
        }

    def _open(self, page: Page, app_url: str, message: str, width: int = 1440) -> None:
        mock_dashboard_defaults(page)
        mock_worker_groups(page)
        job = self._job(message)
        page.route("**/api/jobs?**", lambda r: _fulfill_json(r, {"jobs": [job], "total": 1, "page": 1}))
        page.set_viewport_size({"width": width, "height": 900})
        page.goto(app_url + "/")

    @pytest.mark.parametrize("width", [1440, 1920])
    def test_id_column_is_narrow_and_the_expander_sits_beside_it(
        self, authed_page: Page, app_url: str, width: int
    ) -> None:
        self._open(authed_page, app_url, "Checking Plex loudness 382/124896", width)
        id_cell = authed_page.locator(".jobs-table .job-row .queue-id")
        expect(id_cell).to_be_visible()
        assert id_cell.evaluate("e => e.getBoundingClientRect().width") <= 100
        header = authed_page.locator(".jobs-table thead th").first
        assert (
            abs(
                header.evaluate("e => e.getBoundingClientRect().left")
                - id_cell.evaluate("e => e.getBoundingClientRect().left")
            )
            < 1
        )
        gap = authed_page.evaluate(
            "() => document.querySelector('.job-row .exp').getBoundingClientRect().left"
            " - document.querySelector('.job-row .queue-id').getBoundingClientRect().right"
        )
        assert gap <= 16

    def test_progress_cell_shows_the_count_once_when_the_message_repeats_it(
        self, authed_page: Page, app_url: str
    ) -> None:
        self._open(authed_page, app_url, "Checking Plex loudness 382/124896")
        cell = authed_page.locator(".job-row .queue-progress-cell")
        expect(cell.locator(".queue-items")).to_have_text("382 / 124,896")
        expect(cell.locator(".queue-phase")).to_have_text("Checking Plex loudness")
        assert "124896" not in cell.inner_text()

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("Checking Plex loudness… 382/124896", "Checking Plex loudness"),
            ("382/124896 completed", ""),
            ("Checking Plex loudness 100/124896", "Checking Plex loudness 100/124896"),
            ("Waiting for an available worker", "Waiting for an available worker"),
            ("", ""),
        ],
    )
    def test_dedupe_strips_only_a_trailing_count_equal_to_the_shown_one(
        self, authed_page: Page, app_url: str, message: str, expected: str
    ) -> None:
        self._open(authed_page, app_url, "x")
        assert authed_page.evaluate("([m]) => _dedupeActivityCount(m, 382, 124896)", [message]) == expected
