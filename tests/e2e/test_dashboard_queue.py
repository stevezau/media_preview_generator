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
