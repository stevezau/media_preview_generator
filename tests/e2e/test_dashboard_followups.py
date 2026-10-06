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
        subtitles = [
            authed_page.locator('[data-group-id="gpu"] .hw > span'),
            authed_page.locator('[data-system-group="gpu"] .pool-lbl small'),
        ]
        for subtitle in subtitles:
            expect(subtitle).to_have_text(LONG_DEVICE)
            expect(subtitle).to_have_attribute("title", LONG_DEVICE)
            line_height = subtitle.evaluate("e => parseFloat(getComputedStyle(e).lineHeight) || e.scrollHeight")
            assert subtitle.evaluate("e => e.getBoundingClientRect().height") <= line_height + 1
            assert subtitle.evaluate("e => getComputedStyle(e).textOverflow") == "ellipsis"


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
