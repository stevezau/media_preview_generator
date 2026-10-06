"""Finished chapter issues stay visible and offer an explicit bounded user action."""

from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import expect

from ._mocks import _fulfill_json, mock_dashboard_defaults
from .test_inspector_behaviours import _SOCKET_STUB
from .test_intro_credits_jobs_ui import _job

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup):
    return complete_setup


@pytest.fixture
def warning_page(authed_page, app_url):
    page = authed_page
    mock_dashboard_defaults(page)
    page.add_init_script(_SOCKET_STUB)
    warning = _job(
        "chapter-warning",
        library_name="Synthetic chapter warning",
        error="Scrubber ready; chapter thumbnails failed for 1 server item(s)",
        publishers=[
            {
                "server_id": "plex",
                "name": "Plex",
                "chapter_counts": {"failed": 1},
                "counts": {"published_chapters_failed": 1},
            }
        ],
        config={"file_paths": ["/synthetic/movie.mkv"]},
    )
    active = _job("active-loudness", kind="loudness", status="running", library_name="Ongoing audio analysis")
    queries = []
    posts = []
    state = {"count": 1}

    def jobs(route):
        query = parse_qs(urlparse(route.request.url).query)
        queries.append(query)
        selected = [warning] if query.get("status") == ["chapter_warnings"] else [active]
        if query.get("q"):
            selected = []
        _fulfill_json(
            route,
            {"jobs": selected, "total": len(selected), "page": 1, "pages": 1, "chapter_warning_count": state["count"]},
        )

    page.route("**/api/jobs?**", jobs)
    page.route("**/api/jobs/chapter-warning", lambda route: _fulfill_json(route, warning))
    page.route("**/api/jobs/chapter-warning/logs**", lambda route: _fulfill_json(route, {"logs": [], "total_lines": 0}))
    page.route("**/api/jobs/chapter-warning/files?**", lambda route: _fulfill_json(route, {"files": [], "total": 0}))
    page.route("**/api/jobs/chapter-warning/attempts**", lambda route: _fulfill_json(route, {"attempts": []}))
    page.route("**/api/jobs/chapter-warning/reprocess", lambda route: posts.append(route))
    return page, app_url, queries, posts, state


@pytest.mark.parametrize("width", [1440, 390], ids=["desktop", "mobile"])
def test_warning_notice_survives_unfinished_filter_and_review_clears_other_filters(warning_page, width, tmp_path):
    page, app_url, queries, _posts, _state = warning_page
    page.set_viewport_size({"width": width, "height": 1000})
    errors = []
    page.on("pageerror", lambda error: errors.append(error))
    page.goto(app_url)
    expect(page.locator("#jobStatusFilter")).to_have_value("active")
    expect(page.locator("#job-row-active-loudness")).to_be_visible()
    expect(page.locator("#job-row-chapter-warning")).to_have_count(0)
    expect(page.locator("#chapterWarningsNotice")).to_be_visible()
    expect(page.locator("#chapterWarningsText")).to_contain_text("1 job ended with chapter issues")
    page.locator("#jobKindFilter").select_option("loudness")
    page.locator("#jobSearch").fill("nonmatching search")
    expect(page.locator("#jobQueue [id^='job-row-']")).to_have_count(0)
    page.get_by_role("button", name="Review chapter warnings", exact=True).click()
    expect(page.locator("#job-row-chapter-warning")).to_be_visible()
    expect(page.locator("#jobStatusFilter")).to_have_value("chapter_warnings")
    expect(page.locator("#jobKindFilter")).to_have_value("")
    expect(page.locator("#jobSearch")).to_have_value("")
    assert queries[-1].get("status") == ["chapter_warnings"]
    assert "kind" not in queries[-1] and "q" not in queries[-1]
    page.locator(".card").filter(has=page.locator("#jobQueue")).screenshot(
        path=str(tmp_path / f"chapter-warnings-{width}.png"),
        style=".navbar, .skip-link { visibility: hidden !important; }",
    )
    page.locator("#job-row-chapter-warning").get_by_role("button", name="View logs", exact=True).click()
    expect(page.locator("#logsModal")).to_be_visible()
    expect(page.locator("#opActionReprocess")).to_be_visible()
    expect(page.locator("#opActionReprocess")).to_have_text("Re-run job")
    expect(page.locator("#opActionRetryNow")).to_be_hidden()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    assert not errors


def test_explicit_rerun_prevents_duplicate_posts_and_recovers_after_request_failure(warning_page):
    page, app_url, _queries, posts, _state = warning_page
    page.goto(app_url)
    page.get_by_role("button", name="Review chapter warnings", exact=True).click()
    expect(page.locator("#job-row-chapter-warning")).to_be_visible()
    page.locator("#job-row-chapter-warning").get_by_role("button", name="View logs", exact=True).click()
    button = page.locator("#opActionReprocess")
    expect(button).to_be_visible()
    button.click()
    expect(button).to_be_disabled()
    page.evaluate("void onOperatorReprocess()")
    assert len(posts) == 1
    assert posts[0].request.method == "POST"
    assert posts[0].request.post_data_json in (None, {})
    _fulfill_json(posts[0], {"error": "Temporary request failure"}, status=503)
    expect(button).to_be_enabled()
    expect(page.locator(".toast").last).to_contain_text("Failed to reprocess job")
    button.click()
    expect(button).to_be_disabled()
    assert len(posts) == 2
    _fulfill_json(posts[1], {"id": "new-requested-run"})
    expect(button).to_be_enabled()
    expect(page.locator(".toast").last).to_contain_text("Reprocess Started")


def test_empty_warning_count_hides_notice_without_hiding_active_work(warning_page):
    page, app_url, _queries, _posts, state = warning_page
    state["count"] = 0
    page.goto(app_url)
    expect(page.locator("#job-row-active-loudness")).to_be_visible()
    expect(page.locator("#chapterWarningsNotice")).to_be_hidden()
