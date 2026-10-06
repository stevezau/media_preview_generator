"""The default queue shows unfinished work and keeps history and retry state truthful."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Page, Route, expect

from ._mocks import _fulfill_json, mock_dashboard_defaults
from .test_inspector_behaviours import _SOCKET_STUB
from .test_intro_credits_jobs_ui import _job

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


def _mixed_jobs() -> list[dict]:
    return [
        _job(
            "running-preview",
            status="running",
            completed_at=None,
            library_name="Movies",
            progress={"percent": 30, "processed_items": 30, "total_items": 100},
        ),
        _job(
            "paused-loudness",
            kind="loudness",
            status="pending",
            completed_at=None,
            paused=True,
            library_name="TV Shows loudness",
            progress={"percent": 0, "processed_items": 0, "total_items": 115383},
        ),
        _job(
            "retry-preview",
            status="pending",
            completed_at=None,
            library_name="TV Shows",
            config={
                "is_retry_chain": True,
                "last_outcome": "queued_for_slot",
                "retry_attempt": 1,
                "retry_max_attempts": 3,
            },
            progress={
                "percent": 100,
                "processed_items": 10,
                "total_items": 10,
                "outcome": {"generated": 9, "failed": 1},
                "current_item": "Waiting to retry 1 file",
            },
        ),
        _job(
            "queued-preview",
            status="pending",
            completed_at=None,
            library_name="Sports",
            progress={"percent": 0, "processed_items": 0, "total_items": 100},
        ),
        _job(
            "queued-loudness",
            kind="loudness",
            status="pending",
            completed_at=None,
            library_name="Sports loudness",
            config={"follows_job_id": "queued-preview"},
            progress={"percent": 0, "processed_items": 0, "total_items": 100},
        ),
        _job("finished-preview", library_name="Finished programme"),
        _job("failed-preview", status="failed", error="File could not be read", library_name="Old failed programme"),
        _job(
            "superseded-loudness",
            kind="loudness",
            status="cancelled",
            library_name="Superseded loudness batch",
            config={"superseded_by": "queued-loudness"},
        ),
    ]


@pytest.fixture
def queue_view(authed_page: Page, app_url: str):
    page = authed_page
    mock_dashboard_defaults(page)
    page.add_init_script(_SOCKET_STUB)
    all_jobs = _mixed_jobs()
    queries: list[dict] = []

    def jobs(route: Route) -> None:
        query = parse_qs(urlparse(route.request.url).query)
        queries.append(query)
        status = query.get("status", [""])[0]
        selected = all_jobs
        if status == "active":
            selected = [j for j in selected if j["status"] in {"pending", "running"}]
        elif status:
            selected = [j for j in selected if j["status"] == status]
        _fulfill_json(route, {"jobs": selected, "total": len(selected), "page": 1, "pages": 1})

    page.route("**/api/jobs?**", jobs)
    page.route(
        "**/api/jobs/stats",
        lambda route: _fulfill_json(
            route, {"total": 8, "running": 1, "pending": 4, "completed": 1, "failed": 1, "cancelled": 1}
        ),
    )
    return page, app_url, queries


@pytest.mark.parametrize("width", [1440, 390], ids=["desktop", "mobile"])
def test_unfinished_default_keeps_paused_and_retries_with_accessible_history(queue_view, width: int, tmp_path) -> None:
    page, app_url, queries = queue_view
    page.set_viewport_size({"width": width, "height": 1000})
    page.goto(f"{app_url}/")
    expect(page.locator("#job-row-running-preview")).to_be_visible()
    page.locator(".card").filter(has=page.locator("#jobQueue")).screenshot(
        path=str(tmp_path / f"queue-initial-{width}.png"),
        style=".navbar, .skip-link { visibility: hidden !important; }",
    )
    expect(page.locator("#jobStatusFilter")).to_have_value("active")
    expect(page.locator("#jobQueue [id^='job-row-']")).to_have_count(5)
    assert queries[0].get("status") == ["active"]
    expect(page.locator("#job-row-paused-loudness")).to_be_visible()
    expect(page.locator("#job-row-retry-preview")).to_be_visible()
    expect(page.locator("#job-row-superseded-loudness")).to_have_count(0)
    order = page.locator("#jobQueue [id^='job-row-']").evaluate_all("rows => rows.map(row => row.id)")
    assert order.index("job-row-queued-loudness") == order.index("job-row-queued-preview") + 1

    page.locator("#jobStatusFilter").select_option("")
    expect(page.locator("#jobQueue [id^='job-row-']")).to_have_count(8)
    expect(page.locator("#job-row-finished-preview")).to_be_visible()
    expect(page.locator("#job-row-failed-preview")).to_be_visible()
    expect(page.locator("#job-row-superseded-loudness")).to_be_visible()
    assert "status" not in queries[-1]
    page.locator("#clearJobFilters").click()
    expect(page.locator("#jobStatusFilter")).to_have_value("active")
    expect(page.locator("#jobQueue [id^='job-row-']")).to_have_count(5)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")


def test_unfinished_retry_never_claims_completed_progress_before_or_after_live_update(queue_view) -> None:
    page, app_url, _ = queue_view
    page.goto(f"{app_url}/")
    row = page.locator("#job-row-retry-preview")
    expect(row).to_be_visible()
    cell = row.locator(".queue-progress-cell")
    expect(cell).not_to_contain_text("100.0%")
    expect(cell.locator('[role="progressbar"][aria-valuenow="100.0"]')).to_have_count(0)
    expect(cell).to_contain_text(re.compile("retry", re.IGNORECASE))
    page.evaluate(
        "() => updateJobProgress('retry-preview', {percent:100,processed_items:10,total_items:10,current_item:'Waiting to retry 1 file'})"
    )
    expect(cell).not_to_contain_text("100.0%")
    expect(cell).to_contain_text(re.compile("retry", re.IGNORECASE))


def test_default_empty_queue_explains_how_to_find_finished_jobs(queue_view) -> None:
    page, app_url, _ = queue_view
    page.route("**/api/jobs?**", lambda route: _fulfill_json(route, {"jobs": [], "total": 0, "page": 1, "pages": 1}))
    page.goto(f"{app_url}/")
    expect(page.locator("#jobQueue")).to_contain_text("Nothing queued")
    expect(page.locator("#jobQueue")).to_contain_text("All jobs and history")
