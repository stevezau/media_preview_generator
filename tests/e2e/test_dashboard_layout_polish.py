"""Summary cards share one height; the job detail's logs button sits on the activity line."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from ._mocks import _fulfill_json, mock_dashboard_defaults
from .test_inspector_behaviours import _SOCKET_STUB
from .test_intro_credits_jobs_ui import _job

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


def test_system_quick_actions_and_job_stats_cards_have_the_same_height(authed_page: Page, app_url: str) -> None:
    page = authed_page
    mock_dashboard_defaults(page)
    page.add_init_script(_SOCKET_STUB)
    page.set_viewport_size({"width": 1600, "height": 900})
    page.goto(f"{app_url}/")

    expect(page.locator(".dashboard-stats-card")).to_be_visible()
    heights = [
        page.locator(sel).evaluate("e => Math.round(e.getBoundingClientRect().height)")
        for sel in (".dashboard-system-card", ".dashboard-quick-card", ".dashboard-stats-card")
    ]
    assert max(heights) - min(heights) <= 1, heights


def test_open_logs_button_sits_on_the_activity_line_not_beside_the_paths(authed_page: Page, app_url: str) -> None:
    page = authed_page
    mock_dashboard_defaults(page)
    page.add_init_script(_SOCKET_STUB)
    job = _job("run-1", status="running")
    page.route("**/api/jobs?**", lambda r: _fulfill_json(r, {"jobs": [job], "total": 1, "pages": 1, "page": 1}))
    page.set_viewport_size({"width": 1600, "height": 900})
    page.goto(f"{app_url}/")

    page.locator("#job-row-run-1 .job-details-toggle, #job-row-run-1 .exp").first.click()
    activity = page.locator("#job-detail-run-1 .job-current-activity")
    button = activity.locator(".job-detail-actions")
    expect(button).to_contain_text("Open logs and files")
    a = activity.bounding_box()
    b = button.bounding_box()
    assert a and b
    assert a["y"] <= b["y"] and b["y"] + b["height"] <= a["y"] + a["height"] + 1
    assert abs((b["x"] + b["width"]) - (a["x"] + a["width"])) < 8
