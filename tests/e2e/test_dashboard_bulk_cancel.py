"""Ticking queued rows and using Cancel selected cancels only those rows."""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, expect

from ._mocks import _fulfill_json, mock_dashboard_defaults
from .conftest import accept_app_confirm
from .test_inspector_behaviours import _SOCKET_STUB
from .test_intro_credits_jobs_ui import _job

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


def test_cancel_selected_cancels_only_ticked_queued_rows(authed_page: Page, app_url: str) -> None:
    page = authed_page
    mock_dashboard_defaults(page)
    page.add_init_script(_SOCKET_STUB)
    jobs = {
        "q-one": _job("q-one", status="pending"),
        "q-two": _job("q-two", status="pending"),
        "q-keep": _job("q-keep", status="pending"),
        "r-run": _job("r-run", status="running"),
    }
    bulk_bodies: list[dict] = []

    def jobs_list(route) -> None:
        _fulfill_json(route, {"jobs": list(jobs.values()), "total": len(jobs), "pages": 1, "page": 1})

    def cancel_bulk(route) -> None:
        body = json.loads(route.request.post_data or "{}")
        bulk_bodies.append(body)
        for job_id in body["job_ids"]:
            jobs[job_id]["status"] = "cancelled"
        _fulfill_json(route, {"cancelled": body["job_ids"], "skipped": []})

    page.route("**/api/jobs?**", jobs_list)
    page.route("**/api/jobs/cancel-bulk", cancel_bulk)
    page.goto(f"{app_url}/")

    expect(page.locator("#job-row-q-one")).to_be_visible()
    expect(page.locator("#job-row-r-run .queued-select-cb")).to_have_count(0)
    expect(page.locator("#cancelSelectedButton")).to_be_hidden()

    page.locator("#job-row-q-one .queued-select-cb").check()
    page.locator("#job-row-q-two .queued-select-cb").check()
    expect(page.locator("#cancelSelectedButtonText")).to_have_text("Cancel selected (2)")

    page.locator("#cancelSelectedButton button", has_text="Cancel selected").click()
    accept_app_confirm(page)

    expect(page.locator("#cancelSelectedButton")).to_be_hidden()
    expect(page.locator("#job-row-q-one")).to_contain_text("Cancelled")
    expect(page.locator("#job-row-q-two")).to_contain_text("Cancelled")
    expect(page.locator("#job-row-q-keep")).not_to_contain_text("Cancelled")
    assert sorted(bulk_bodies[0]["job_ids"]) == ["q-one", "q-two"]


def test_cancel_selected_shows_neutral_toast_when_some_are_skipped(authed_page: Page, app_url: str) -> None:
    page = authed_page
    mock_dashboard_defaults(page)
    page.add_init_script(_SOCKET_STUB)
    jobs = {
        "q-one": _job("q-one", status="pending"),
        "q-two": _job("q-two", status="pending"),
    }

    def jobs_list(route) -> None:
        _fulfill_json(route, {"jobs": list(jobs.values()), "total": len(jobs), "pages": 1, "page": 1})

    def cancel_bulk(route) -> None:
        jobs["q-one"]["status"] = "cancelled"
        _fulfill_json(
            route,
            {"cancelled": ["q-one"], "skipped": [{"id": "q-two", "reason": "not_pending (running)"}]},
        )

    page.route("**/api/jobs?**", jobs_list)
    page.route("**/api/jobs/cancel-bulk", cancel_bulk)
    page.goto(f"{app_url}/")

    page.locator("#job-row-q-one .queued-select-cb").check()
    page.locator("#job-row-q-two .queued-select-cb").check()
    page.locator("#cancelSelectedButton button", has_text="Cancel selected").click()
    accept_app_confirm(page)

    expect(
        page.locator(".toast", has_text="1 could not be cancelled (already started, finished or removed)")
    ).to_be_visible()
