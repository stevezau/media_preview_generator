"""Job queue: priority control, status tabs and the Clear jobs menu on the redesigned dashboard."""

from __future__ import annotations

import json
from collections.abc import Callable
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Page, expect

from . import test_intro_credits_jobs_ui as jobs_ui
from ._mocks import _fulfill_json

dashboard = jobs_ui.dashboard


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup: None) -> None:
    return complete_setup


def _job(job_id: str, status: str, priority: int = 2, **overrides) -> dict:
    return jobs_ui._job(job_id, status=status, priority=priority, **overrides)


@pytest.mark.e2e
class TestPriorityControl:
    @pytest.mark.parametrize(
        ("status", "editable"),
        [
            ("pending", True),
            ("running", True),
            ("completed", False),
            ("failed", False),
            ("cancelled", False),
        ],
    )
    def test_priority_is_a_dropdown_only_when_job_has_not_finished(
        self, dashboard: Callable[..., Page], status: str, editable: bool
    ) -> None:
        job = _job("aaaaaaaa-0000-4000-8000-000000000001", status, priority=3)
        page = dashboard([job])
        cell = page.locator(f"#job-row-{job['id']} .queue-priority")
        expect(cell).to_contain_text("Low")
        expect(cell.locator(".priority-btn")).to_have_count(1 if editable else 0)
        expect(cell.locator(".priority-readonly")).to_have_count(0 if editable else 1)

    def test_choosing_high_posts_priority_one_and_marks_current_item(self, dashboard: Callable[..., Page]) -> None:
        job = _job("aaaaaaaa-0000-4000-8000-000000000002", "pending", priority=2)
        page = dashboard([job])
        posted: list[dict] = []

        def handler(route) -> None:
            posted.append({"url": route.request.url, "body": route.request.post_data_json})
            _fulfill_json(route, {"success": True})

        page.route("**/api/jobs/*/priority", handler)
        row = page.locator(f"#job-row-{job['id']}")
        button = row.get_by_role("button", name="Priority: Normal. Change priority")
        button.click()
        expect(row.get_by_role("menuitemradio", name="Normal")).to_have_attribute("aria-checked", "true")
        row.get_by_role("menuitemradio", name="High").click()
        expect(row.locator(".priority-btn")).to_contain_text("High")
        assert [(p["url"].rsplit("/api/", 1)[1], p["body"]) for p in posted] == [
            (f"jobs/{job['id']}/priority", {"priority": 1})
        ]


@pytest.mark.e2e
class TestStatusTabs:
    def test_failed_tab_filters_by_status_and_shows_stats_counts(self, authed_page: Page, app_url: str) -> None:
        from ._mocks import mock_dashboard_defaults

        mock_dashboard_defaults(authed_page)
        queries: list[dict] = []

        def jobs(route) -> None:
            queries.append(parse_qs(urlparse(route.request.url).query))
            _fulfill_json(route, {"jobs": [], "total": 0, "page": 1, "pages": 1})

        authed_page.route("**/api/jobs?**", jobs)
        authed_page.route(
            "**/api/jobs/stats",
            lambda r: _fulfill_json(
                r, {"pending": 15, "running": 4, "completed": 588, "failed": 20, "cancelled": 500, "total": 1127}
            ),
        )
        authed_page.goto(app_url + "/")
        tabs = authed_page.locator("#jobStatusTabs")
        expect(tabs.get_by_role("button", name="Failed 20")).to_be_visible()
        expect(tabs.get_by_role("button", name="Unfinished 19")).to_have_attribute("aria-pressed", "true")
        expect(authed_page.locator("#jobStatusFilter")).to_have_value("active")
        with authed_page.expect_response(lambda r: parse_qs(urlparse(r.url).query).get("status") == ["failed"]):
            tabs.get_by_role("button", name="Failed 20").click()
        expect(tabs.get_by_role("button", name="Failed 20")).to_have_attribute("aria-pressed", "true")
        expect(tabs.get_by_role("button", name="Unfinished 19")).to_have_attribute("aria-pressed", "false")
        expect(authed_page.locator("#jobStatusFilter")).to_have_value("failed")
        assert queries[-1]["page"] == ["1"]


@pytest.mark.e2e
class TestClearJobsMenu:
    def test_button_count_follows_ticked_statuses_and_posts_them(self, authed_page: Page, app_url: str) -> None:
        from ._mocks import mock_dashboard_defaults

        mock_dashboard_defaults(authed_page)
        authed_page.route(
            "**/api/jobs/stats",
            lambda r: _fulfill_json(
                r, {"pending": 1, "running": 1, "completed": 588, "failed": 20, "cancelled": 500, "total": 1110}
            ),
        )
        cleared: list[dict] = []

        def clear(route) -> None:
            cleared.append(json.loads(route.request.post_data))
            _fulfill_json(route, {"cleared": 20})

        authed_page.route("**/api/jobs/clear", clear)
        authed_page.goto(app_url + "/")
        authed_page.get_by_role("button", name="Clear jobs", exact=True).click()
        button = authed_page.locator("#clearJobsButton")
        expect(button).to_have_text("Clear 1,108 jobs")
        authed_page.locator("#clearCompleted").uncheck()
        authed_page.locator("#clearCancelled").uncheck()
        expect(button).to_have_text("Clear 20 jobs")
        button.click()
        authed_page.locator("#appConfirmModalOkBtn").click()
        expect(authed_page.locator("#toastBody")).to_contain_text("Cleared 20 failed jobs")
        assert cleared == [{"statuses": ["failed"]}]


_STATUS_ROWS = [
    ("Completed with warnings", {"status": "completed", "error": "chapter thumbnails failed"}),
    ("Completed", {"status": "completed"}),
    ("Cancelled", {"status": "cancelled"}),
    ("Failed", {"status": "failed", "error": "boom"}),
    ("Paused", {"status": "running", "paused": True}),
    ("Running", {"status": "running"}),
    ("Pending", {"status": "pending"}),
]


@pytest.mark.e2e
class TestStatusPillFit:
    @pytest.mark.parametrize("width", [1920, 1440, 1280, 1100])
    def test_status_pill_stays_inside_its_cell_and_clear_of_priority_when_width_is_narrow(
        self, dashboard: Callable[..., Page], width: int
    ) -> None:
        jobs = [_job(f"row-{i}", **overrides) for i, (_label, overrides) in enumerate(_STATUS_ROWS)]
        page = dashboard(jobs)
        page.set_viewport_size({"width": width, "height": 1000})
        page.locator("#jobStatusFilter").select_option(label="All jobs and history")
        for i, (label, _overrides) in enumerate(_STATUS_ROWS):
            row = page.locator(f"#job-row-row-{i}")
            expect(row).to_be_visible()
            pill = row.locator(".queue-status .status-dot")
            expect(pill).to_have_attribute("aria-label", label)
            pill_box = pill.bounding_box()
            cell_box = row.locator("td.queue-status").bounding_box()
            priority_box = row.locator("td.queue-priority").bounding_box()
            assert pill_box["x"] + pill_box["width"] <= cell_box["x"] + cell_box["width"] + 0.5, label
            assert pill_box["x"] + pill_box["width"] <= priority_box["x"] + 0.5, label
            assert pill.evaluate(
                "el => el.querySelector('.status-label').scrollWidth <= el.querySelector('.status-label').clientWidth"
            ), f"{label} label is truncated at {width}px"


@pytest.mark.e2e
class TestQueueToolbar:
    def test_toolbar_is_two_rows_with_count_inline_when_filters_are_active(
        self, dashboard: Callable[..., Page]
    ) -> None:
        page = dashboard([_job("a", status="running")])
        page.set_viewport_size({"width": 1440, "height": 900})
        page.locator("#jobSearch").fill("zzz")
        expect(page.locator("#jobFilterCount")).to_be_visible()
        expect(page.locator("#clearJobFilters")).to_be_visible()
        box = lambda selector: page.locator(selector).bounding_box()  # noqa: E731
        tabs, search, count, clear = (
            box("#jobStatusTabs"),
            box("#jobSearch"),
            box("#jobFilterCount"),
            box("#clearJobFilters"),
        )
        assert tabs["y"] + tabs["height"] <= search["y"]
        for selector in ("#jobServerFilter", "#jobLibraryFilter", "#jobKindFilter"):
            select = box(selector)
            assert abs(select["y"] - search["y"]) < 6, selector
            assert 150 <= select["width"] <= 170, selector
        assert abs(count["y"] - search["y"]) < 12 and abs(clear["y"] - search["y"]) < 12
        assert count["x"] > clear["x"]
        queue_tools = box(".queue-tools")
        assert queue_tools["height"] < 130

    def test_selects_fold_into_filters_button_with_active_count_when_viewport_is_narrow(
        self, dashboard: Callable[..., Page]
    ) -> None:
        page = dashboard([_job("a", status="running")])
        page.set_viewport_size({"width": 800, "height": 900})
        toggle = page.locator("#queueFiltersToggle")
        expect(toggle).to_be_visible()
        expect(page.locator("#jobServerFilter")).to_be_hidden()
        toggle.click()
        expect(toggle).to_have_attribute("aria-expanded", "true")
        page.locator("#jobKindFilter").select_option("previews")
        expect(toggle).to_contain_text("Filters (1)")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")

    def test_filters_button_is_hidden_when_viewport_is_wide(self, dashboard: Callable[..., Page]) -> None:
        page = dashboard([_job("a", status="running")])
        page.set_viewport_size({"width": 1440, "height": 900})
        expect(page.locator("#queueFiltersToggle")).to_be_hidden()
        expect(page.locator("#jobServerFilter")).to_be_visible()
