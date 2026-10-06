"""Slow queue reads must render, while changed selections still reject stale answers."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Page, Route, expect

from ._mocks import _fulfill_json, mock_dashboard_defaults
from .test_inspector_behaviours import _SOCKET_STUB, _emit
from .test_intro_credits_jobs_ui import _job

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


@pytest.fixture
def held_queue(authed_page: Page, app_url: str) -> tuple[Page, list[Route]]:
    page = authed_page
    mock_dashboard_defaults(page)
    page.add_init_script(_SOCKET_STUB)
    held: list[Route] = []
    page.route("**/api/jobs?**", lambda route: held.append(route))
    page.goto(f"{app_url}/")
    _wait_for_reads(page, held, 1)
    return page, held


def _wait_for_reads(page: Page, held: list[Route], count: int) -> None:
    for _ in range(100):
        if len(held) >= count:
            return
        page.wait_for_timeout(20)
    assert len(held) >= count, "The requested queue read never started"


def _answer(route: Route, name: str, *, page: int = 1) -> None:
    _fulfill_json(route, {"jobs": [_job(name)], "total": 100, "pages": 2, "page": page})


def test_slow_first_queue_read_survives_poll_and_socket_refreshes(held_queue) -> None:
    page, held = held_queue
    # Reproduce a response slower than the real five-second dashboard poll.
    page.wait_for_timeout(5200)
    _emit(page, "connect", {})
    _emit(page, "job_created", _job("new-job"))
    _emit(page, "job_paused", {"id": "new-job"})
    page.wait_for_timeout(50)
    assert len(held) == 1, "Same-selection refreshes must share the in-flight read"

    _answer(held[0], "slow-first-result")
    expect(page.locator("#job-row-slow-first-result")).to_be_visible()
    # A subsequent refresh can read changes made while the first read was busy.
    _emit(page, "connect", {})
    _wait_for_reads(page, held, 2)
    assert len(held) == 2
    _answer(held[1], "after-events")
    expect(page.locator("#job-row-after-events")).to_be_visible()
    expect(page.locator("#job-row-slow-first-result")).to_have_count(0)


@pytest.mark.parametrize("selection", ["filter", "page", "forced-refresh"])
def test_new_selection_wins_over_late_old_queue_response(held_queue, selection: str) -> None:
    page, held = held_queue
    if selection == "filter":
        page.locator("#jobStatusFilter").select_option("failed")
    elif selection == "page":
        page.evaluate("() => { jobTotalPages = 2; goToJobPage(2); }")
    else:
        page.evaluate("() => { loadJobs({force: true}); }")
    _wait_for_reads(page, held, 2)
    query = parse_qs(urlparse(held[1].request.url).query)
    expected = {"page": ["2" if selection == "page" else "1"], "per_page": ["50"], "status": ["active"]}
    if selection == "filter":
        expected["status"] = ["failed"]
    assert query == expected
    _answer(held[1], "current-selection", page=2 if selection == "page" else 1)
    expect(page.locator("#job-row-current-selection")).to_be_visible()
    _answer(held[0], "stale-selection")
    page.wait_for_timeout(100)
    expect(page.locator("#job-row-current-selection")).to_be_visible()
    expect(page.locator("#job-row-stale-selection")).to_have_count(0)


def test_failed_queue_read_releases_refresh_for_retry(held_queue) -> None:
    page, held = held_queue
    _fulfill_json(held[0], {"error": "Temporarily unavailable"}, status=503)
    expect(page.locator("#jobQueue")).to_contain_text("Loading job queue")
    _emit(page, "connect", {})
    _wait_for_reads(page, held, 2)
    _answer(held[1], "recovered")
    expect(page.locator("#job-row-recovered")).to_be_visible()


def test_old_response_does_not_release_a_newer_read_still_in_flight(held_queue) -> None:
    page, held = held_queue
    page.locator("#jobStatusFilter").select_option("failed")
    _wait_for_reads(page, held, 2)
    _answer(held[0], "stale-selection")
    page.wait_for_timeout(50)
    _emit(page, "connect", {})
    page.wait_for_timeout(50)
    assert len(held) == 2, "Completing an old selection must not clear the newer in-flight read"
    expect(page.locator("#job-row-stale-selection")).to_have_count(0)
    _answer(held[1], "current-selection")
    expect(page.locator("#job-row-current-selection")).to_be_visible()
