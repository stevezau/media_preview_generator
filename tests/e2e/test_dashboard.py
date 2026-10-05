"""E2E tests for the dashboard (/) page.

Coverage:

* Empty state when no media servers configured.
* Group hardware, configured capacity, and Settings links remain visible.
* Dashboard group headers are read-only; configuration lives in Settings.
* Update-available badge appears when /api/system/version reports newer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from playwright.sync_api import Page, expect

from ._mocks import (
    _fulfill_json,
    capture_settings_save,
    mock_dashboard_defaults,
    mock_media_servers_status,
    mock_version_with_update,
    mock_worker_groups,
)


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


@pytest.fixture
def dashboard_page(authed_page: Page, app_url: str) -> Page:
    mock_dashboard_defaults(authed_page)
    mock_worker_groups(authed_page)
    authed_page.goto(f"{app_url}/")
    authed_page.wait_for_load_state("domcontentloaded")
    return authed_page


@pytest.mark.e2e
class TestDashboardEmptyState:
    def test_empty_state_banner_visible_when_no_servers(self, dashboard_page: Page) -> None:
        # The empty-state banner is gated by the dashboard JS that calls
        # /api/system/media-servers. With our mocked empty list it should
        # render.
        expect(dashboard_page.locator("text=No media servers configured yet")).to_be_visible(timeout=3000)

    def test_empty_state_cta_links_to_servers(self, dashboard_page: Page) -> None:
        cta = dashboard_page.locator('a[href="/servers"]:has-text("Add a media server")')
        expect(cta).to_be_visible()


@pytest.mark.e2e
class TestDashboardWithServers:
    def test_media_servers_status_renders_per_server(self, authed_page: Page, app_url: str) -> None:
        mock_dashboard_defaults(authed_page)
        mock_media_servers_status(
            authed_page,
            servers=[
                {
                    "id": "plex-1",
                    "name": "Home Plex",
                    "type": "plex",
                    "enabled": True,
                    "status": "connected",
                    "url": "http://plex.local:32400",
                },
                {
                    "id": "emby-1",
                    "name": "My Emby",
                    "type": "emby",
                    "enabled": True,
                    "status": "connected",
                    "url": "http://emby.local:8096",
                },
            ],
        )
        authed_page.goto(f"{app_url}/")
        authed_page.wait_for_load_state("domcontentloaded")
        # Each server's name should appear in the status block.
        expect(authed_page.locator("#mediaServersStatus")).to_contain_text("Home Plex", timeout=3000)
        expect(authed_page.locator("#mediaServersStatus")).to_contain_text("My Emby")


@pytest.mark.e2e
class TestDashboardWorkerGroups:
    def test_groups_show_detected_hardware(self, dashboard_page: Page) -> None:
        expect(dashboard_page.locator("#workerGroupDashboard")).to_contain_text("GPU 0")
        expect(dashboard_page.locator("#gpuWorkerConfig, #cpuWorkers")).to_have_count(0)

    def test_group_controls_are_read_only_with_direct_edit_link(self, authed_page: Page, app_url: str) -> None:
        mock_dashboard_defaults(authed_page)
        groups = mock_worker_groups(authed_page)
        legacy = capture_settings_save(authed_page)
        authed_page.goto(app_url + "/")
        row = authed_page.locator('[data-group-id="cpu"]')
        expect(row.locator('[data-group-indicator="configured"] summary')).to_have_attribute(
            "aria-label", "1 configured worker. Worker counts set simultaneous tasks, not CPU cores."
        )
        expect(row.locator('[data-scale], input[type="checkbox"]')).to_have_count(0)
        expect(row.get_by_role("link", name="Edit CPU workers", exact=True)).to_have_attribute(
            "href", "/settings?worker_group=cpu#section-workers"
        )
        assert not groups["writes"]
        assert not legacy

    def test_configured_capacity_does_not_require_dashboard_stepper(self, authed_page: Page, app_url: str) -> None:
        mock_dashboard_defaults(authed_page)
        groups = mock_worker_groups(authed_page, cpu_count=32)
        authed_page.goto(app_url + "/")
        row = authed_page.locator('[data-group-id="cpu"]')
        expect(row.locator('[data-group-indicator="configured"] summary')).to_have_attribute(
            "aria-label", "32 configured workers. Worker counts set simultaneous tasks, not CPU cores."
        )
        expect(row.locator("[data-scale]")).to_have_count(0)
        assert not groups["writes"]


@pytest.mark.e2e
class TestDashboardVersion:
    def test_update_available_badge_shown_when_newer(self, authed_page: Page, app_url: str) -> None:
        mock_dashboard_defaults(authed_page)
        mock_version_with_update(authed_page)
        authed_page.goto(f"{app_url}/")
        authed_page.wait_for_load_state("domcontentloaded")
        badge = authed_page.locator("#dashboardUpdateBadge")
        expect(badge).to_be_visible(timeout=3000)
        expect(badge).to_contain_text("Update available")


@pytest.mark.e2e
class TestActiveJobWaitingToRetry:
    def test_preview_job_waiting_to_retry_shows_its_attempt_and_countdown(
        self, authed_page: Page, app_url: str
    ) -> None:
        """Regression: the retry-waiting card read undefined retryAttempt/maxRetries, so the render threw and the
        Active Jobs card never appeared."""
        mock_dashboard_defaults(authed_page)
        eta = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        job = {
            "id": "abcdef01-0000-4000-8000-000000000001",
            "status": "running",
            "created_at": "2026-09-14T01:00:00+00:00",
            "started_at": "2026-09-14T01:00:01+00:00",
            "completed_at": None,
            "library_name": "Retry: Show S01E01.mkv",
            "server_id": "jf-1",
            "server_name": "Home Jellyfin",
            "server_type": "jellyfin",
            "publishers": [],
            "progress": {
                "percent": 0.0,
                "current_item": "",
                "total_items": 1,
                "processed_items": 0,
                "workers": [],
                "outcome": None,
                "retry_eta": eta,
                "retry_wait_total": 300,
            },
            "error": None,
            "config": {"is_retry_chain": True, "retry_attempt": 2, "max_retries": 5},
            "paused": False,
            "priority": 2,
            "parent_schedule_id": "",
            "kind": "previews",
        }
        authed_page.route(
            "**/api/jobs?**", lambda r: _fulfill_json(r, {"jobs": [job], "total": 1, "page": 1, "pages": 1})
        )
        authed_page.goto(f"{app_url}/")
        authed_page.wait_for_load_state("domcontentloaded")

        row = authed_page.locator(f"#job-row-{job['id']}")
        expect(row).to_be_visible(timeout=5000)
        expect(row).to_contain_text("Retry starting in 5 min")
        expect(row.locator(".job-kind-badge")).to_have_text("Previews")
        expect(authed_page.locator("#activeJobsContainer")).to_have_count(0)
        expect(authed_page.locator(".jobs-table thead th:nth-child(2)")).to_have_text("Job")
        row.get_by_role("button", name="Job details", exact=True).click()
        detail = authed_page.locator(f"#job-detail-{job['id']}")
        expect(detail).to_be_visible()
        expect(detail).to_contain_text("Started")
