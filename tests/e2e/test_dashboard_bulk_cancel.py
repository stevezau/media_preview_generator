"""Ticking rows and using the selection buttons acts only on those rows."""

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
    expect(page.locator("#job-row-r-run .job-select-cb")).to_have_count(0)
    expect(page.locator("#selectionBar")).to_be_hidden()

    page.locator("#job-row-q-one .job-select-cb").check()
    page.locator("#job-row-q-two .job-select-cb").check()
    expect(page.locator("#cancelSelectedButtonText")).to_have_text("Cancel queued (2)")

    page.locator("#cancelSelectedButton").click()
    accept_app_confirm(page)

    expect(page.locator("#selectionBar")).to_be_hidden()
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

    page.locator("#job-row-q-one .job-select-cb").check()
    page.locator("#job-row-q-two .job-select-cb").check()
    page.locator("#cancelSelectedButton").click()
    accept_app_confirm(page)

    expect(
        page.locator(".toast", has_text="1 could not be cancelled (already started, finished or removed)")
    ).to_be_visible()


def test_delete_finished_acts_only_on_ticked_rows_and_clear_dropdown_hides(authed_page: Page, app_url: str) -> None:
    page = authed_page
    mock_dashboard_defaults(page)
    page.add_init_script(_SOCKET_STUB)
    jobs = {
        "d-one": _job("d-one", status="completed"),
        "d-two": _job("d-two", status="failed"),
        "d-keep": _job("d-keep", status="completed"),
        "q-one": _job("q-one", status="pending"),
    }
    clear_bodies: list[dict] = []

    def jobs_list(route) -> None:
        _fulfill_json(route, {"jobs": list(jobs.values()), "total": len(jobs), "pages": 1, "page": 1})

    def clear(route) -> None:
        body = json.loads(route.request.post_data or "{}")
        clear_bodies.append(body)
        for job_id in body["job_ids"]:
            jobs.pop(job_id)
        _fulfill_json(
            route, {"success": True, "cleared": len(body["job_ids"]), "removed": body["job_ids"], "skipped": []}
        )

    page.route("**/api/jobs?**", jobs_list)
    page.route("**/api/jobs/clear", clear)
    page.goto(f"{app_url}/")

    expect(page.locator("#job-row-d-one")).to_be_visible()
    expect(page.locator("#clearJobsDropdown")).to_be_visible()

    page.locator("#job-row-d-one .job-select-cb").check()
    page.locator("#job-row-d-two .job-select-cb").check()
    page.locator("#job-row-q-one .job-select-cb").check()
    expect(page.locator("#clearJobsDropdown")).to_be_hidden()
    expect(page.locator("#selectionCount")).to_have_text("3 selected")
    expect(page.locator("#deleteSelectedButtonText")).to_have_text("Delete finished (2)")
    expect(page.locator("#cancelSelectedButtonText")).to_have_text("Cancel queued (1)")

    page.locator("#deleteSelectedButton").click()
    accept_app_confirm(page)

    expect(page.locator("#job-row-d-one")).to_have_count(0)
    expect(page.locator("#job-row-d-two")).to_have_count(0)
    expect(page.locator("#job-row-d-keep")).to_be_visible()
    assert sorted(clear_bodies[0]["job_ids"]) == ["d-one", "d-two"]
    assert "statuses" not in clear_bodies[0]


def test_select_all_checkbox_sits_in_its_own_column_left_of_id(authed_page: Page, app_url: str) -> None:
    page = authed_page
    mock_dashboard_defaults(page)
    page.add_init_script(_SOCKET_STUB)
    jobs = {"q-one": _job("q-one", status="pending"), "d-one": _job("d-one", status="completed")}

    def jobs_list(route) -> None:
        _fulfill_json(route, {"jobs": list(jobs.values()), "total": len(jobs), "pages": 1, "page": 1})

    page.route("**/api/jobs?**", jobs_list)
    page.goto(f"{app_url}/")

    box = page.locator("#job-row-q-one .job-select-cb").bounding_box()
    jid = page.locator("#job-row-q-one .jid").bounding_box()
    assert box and jid
    assert box["x"] + box["width"] <= jid["x"]
    assert abs((box["y"] + box["height"] / 2) - (jid["y"] + jid["height"] / 2)) < 6

    page.locator("#selectAllQueued").check()
    expect(page.locator("#selectionCount")).to_have_text("2 selected")


def test_paused_running_rows_are_selectable_but_active_ones_are_not(authed_page: Page, app_url: str) -> None:
    page = authed_page
    mock_dashboard_defaults(page)
    page.add_init_script(_SOCKET_STUB)
    held = _job("r-held", status="running")
    held["paused"] = True
    jobs = {"r-held": held, "r-active": _job("r-active", status="running")}

    def jobs_list(route) -> None:
        _fulfill_json(route, {"jobs": list(jobs.values()), "total": len(jobs), "pages": 1, "page": 1})

    page.route("**/api/jobs?**", jobs_list)
    page.goto(f"{app_url}/")

    expect(page.locator("#job-row-r-held")).to_be_visible()
    expect(page.locator("#job-row-r-held .job-select-cb")).to_have_count(1)
    expect(page.locator("#job-row-r-active .job-select-cb")).to_have_count(0)
    page.locator("#job-row-r-held .job-select-cb").check()
    expect(page.locator("#cancelSelectedButtonText")).to_have_text("Cancel queued (1)")
