"""E2E tests for the dashboard (/) page.

Coverage:

* Empty state when no media servers configured.
* Per-GPU worker config card renders with detected GPUs.
* CPU + GPU stepper buttons increment / decrement and POST settings.
* Update-available badge appears when /api/system/version reports newer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from playwright.sync_api import Page, Route, expect

from ._mocks import (
    _fulfill_json,
    capture_settings_save,
    mock_dashboard_defaults,
    mock_media_servers_status,
    mock_version_with_update,
)


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


@pytest.fixture
def dashboard_page(authed_page: Page, app_url: str) -> Page:
    mock_dashboard_defaults(authed_page)
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
class TestDashboardGpuWorkerConfig:
    def test_per_gpu_card_renders_from_status(self, dashboard_page: Page) -> None:
        # mock_dashboard_defaults registers one GPU.
        expect(dashboard_page.locator("#gpuWorkerConfig")).to_contain_text("GPU 0", timeout=3000)

    def test_cpu_stepper_plus_increments_badge(self, authed_page: Page, app_url: str) -> None:
        mock_dashboard_defaults(authed_page)
        captured = capture_settings_save(authed_page)
        # The click refreshes the worker panel, which re-reads the badge from config, so config must reflect the save.
        authed_page.route(
            "**/api/system/config",
            lambda r: _fulfill_json(
                r, {"gpu_threads": 0, "cpu_threads": (captured[-1] if captured else {}).get("cpu_threads", 1)}
            ),
        )
        authed_page.goto(f"{app_url}/")
        authed_page.wait_for_load_state("domcontentloaded")

        # CPU workers badge starts at "-" then loads to "0" or "1" from
        # /api/system/config (we mocked cpu_threads=1).
        cpu_badge = authed_page.locator("#cpuWorkers")
        expect(cpu_badge).to_have_text("1", timeout=3000)

        # Click the + button.
        plus_btn = authed_page.locator('button.worker-scale-btn[data-worker-type="CPU"][data-direction="1"]')
        plus_btn.click()
        # Wait for the optimistic update + settings POST.
        expect(cpu_badge).to_have_text("2", timeout=2000)
        # Wait until the request landed.
        authed_page.wait_for_timeout(300)
        assert any("cpu_threads" in (c or {}) for c in captured), "POST /api/settings did not include cpu_threads"

    def test_cpu_stepper_changes_one_worker_per_click(self, authed_page: Page, app_url: str) -> None:
        """Saving cpu_threads resizes the live pool, so a click must send that save and nothing else.

        A second /api/workers/add|remove call on top of the save changed two workers per click.
        """
        mock_dashboard_defaults(authed_page)
        saved = {"cpu_threads": 1}
        settings_posts: list[dict] = []
        worker_posts: list[str] = []

        def settings_handler(route: Route) -> None:
            if route.request.method != "POST":
                route.continue_()
                return
            body = route.request.post_data_json or {}
            settings_posts.append(body)
            saved.update(body)
            _fulfill_json(route, {"success": True})

        def workers_handler(route: Route) -> None:
            worker_posts.append(route.request.url)
            _fulfill_json(route, {"success": True, "added": 1, "removed": 1})

        authed_page.route("**/api/settings", settings_handler)
        authed_page.route("**/api/system/config", lambda r: _fulfill_json(r, {"gpu_threads": 0, **saved}))
        authed_page.route("**/api/workers/**", workers_handler)
        authed_page.goto(f"{app_url}/")
        authed_page.wait_for_load_state("domcontentloaded")

        cpu_badge = authed_page.locator("#cpuWorkers")
        expect(cpu_badge).to_have_text("1", timeout=3000)
        plus_btn = authed_page.locator('button.worker-scale-btn[data-worker-type="CPU"][data-direction="1"]')
        minus_btn = authed_page.locator('button.worker-scale-btn[data-worker-type="CPU"][data-direction="-1"]')

        toast_body = authed_page.locator("#toastBody")

        plus_btn.click()
        expect(cpu_badge).to_have_text("2", timeout=2000)
        authed_page.wait_for_timeout(300)
        assert worker_posts == [], f"the + click also resized the pool directly: {worker_posts}"
        expect(toast_body).to_have_text("CPU workers set to 2", timeout=2000)

        minus_btn.click()
        expect(cpu_badge).to_have_text("1", timeout=2000)
        expect(toast_body).to_have_text("CPU workers set to 1", timeout=2000)

        assert settings_posts == [{"cpu_threads": 2}, {"cpu_threads": 1}]
        assert worker_posts == [], f"the - click also resized the pool directly: {worker_posts}"


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

        card = authed_page.locator(f"#active-job-{job['id']}")
        expect(card).to_be_visible(timeout=5000)
        expect(card).to_contain_text("Waiting to retry")
        expect(card).to_contain_text("Next attempt in 5 min")
        expect(card).to_contain_text("Attempt 2 of 5")
        expect(card.locator(".job-kind-badge")).to_have_text("Previews")
        expect(authed_page.locator(f"#job-row-{job['id']}")).to_contain_text("Retry starting in 5 min")

        # The queue's first text column covers scans, webhooks, and retry
        # chains alike — "Job" describes it, not "Library".
        expect(authed_page.locator(".jobs-table thead th:nth-child(2)")).to_have_text("Job")
        expect(card).to_contain_text("Job:")
