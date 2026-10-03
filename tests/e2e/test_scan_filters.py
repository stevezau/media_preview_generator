"""Full-scan filters follow selected library types and survive schedule edits."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from ._mocks import _fulfill_json, mock_dashboard_defaults, mock_media_servers_status, mock_servers_list


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


def _seed(page: Page, schedules: list[dict] | None = None, libraries: list[dict] | None = None) -> None:
    mock_dashboard_defaults(page)
    servers = [{"id": "plex-1", "name": "Plex", "type": "plex", "enabled": True, "status": "connected"}]
    mock_servers_list(page, servers=servers)
    mock_media_servers_status(page, servers=servers)
    if libraries is None:
        libraries = [
            {"id": "movies", "name": "Movies", "type": "movie", "server_id": "plex-1"},
            {"id": "tv", "name": "TV Shows", "type": "show", "server_id": "plex-1"},
        ]
    page.route("**/api/libraries**", lambda route: _fulfill_json(route, {"libraries": libraries}))
    page.route(
        "**/api/schedules",
        lambda route: _fulfill_json(
            route,
            {"schedules": schedules or []} if route.request.method == "GET" else {"id": "saved"},
        ),
    )
    page.route(
        "**/api/jobs",
        lambda route: (
            _fulfill_json(route, {"id": "job", "status": "queued"})
            if route.request.method == "POST"
            else route.continue_()
        ),
    )


@pytest.fixture
def filter_page(authed_page: Page, app_url: str) -> Page:
    _seed(authed_page)
    authed_page.goto(app_url)
    authed_page.wait_for_function("libraries.length === 2")
    authed_page.locator('button:has-text("Start New Job")').click()
    authed_page.locator("#jobScanFilters > summary").click()
    return authed_page


def _submit_job(page: Page) -> dict:
    with page.expect_request(lambda request: request.url.endswith("/api/jobs") and request.method == "POST") as sent:
        page.locator("#newJobModal .modal-footer .btn-primary").click()
    return sent.value.post_data_json["config"]


@pytest.mark.e2e
def test_job_combines_shared_tv_and_movie_filters(filter_page: Page) -> None:
    page = filter_page
    expect(page.locator('#jobScanFilters [data-filter-type="tv"]')).to_be_visible()
    expect(page.locator('#jobScanFilters [data-filter-type="movie"]')).to_be_visible()
    page.locator("#jobAddedFilter").select_option("last_days")
    page.locator("#jobAddedLastDays").fill("90")
    page.locator("#jobSeasonMode").select_option("latest")
    page.locator("#jobLatestSeasons").fill("2")
    page.locator("#jobMovieYearMode").select_option("range")
    page.locator("#jobMovieYearFrom").fill("2020")
    page.locator("#jobMovieYearTo").fill("2026")
    expect(page.locator("#jobScanFilters [data-filter-summary]")).to_contain_text("TV: latest 2 seasons")
    config = _submit_job(page)
    assert config == {
        "force_generate": False,
        "sort_by": "default",
        "added_filter": "last_days",
        "added_last_days": 90,
        "latest_seasons": 2,
        "movie_year_from": 2020,
        "movie_year_to": 2026,
    }


@pytest.mark.e2e
@pytest.mark.parametrize("check_servers", [False, True])
def test_intro_credits_job_hides_and_omits_preview_filters(filter_page: Page, check_servers: bool) -> None:
    page = filter_page
    page.locator("#jobAddedFilter").select_option("last_days")
    page.locator("#jobAddedLastDays").fill("0")
    page.locator("#jobKindMarkers").check()
    if check_servers:
        page.locator("#jobMarkersModeCheckServers").check()
    expect(page.locator("#jobScanFilters")).to_be_hidden()
    endpoint = "/api/markers/reconcile" if check_servers else "/api/markers/jobs"
    page.route(f"**{endpoint}", lambda route: _fulfill_json(route, {"id": "markers", "job_id": "markers"}))
    with page.expect_request(lambda request: request.url.endswith(endpoint) and request.method == "POST") as sent:
        page.locator("#newJobModal .modal-footer .btn-primary").click()
    assert "added_filter" not in sent.value.post_data_json
    assert "added_last_days" not in sent.value.post_data_json
    assert "config" not in sent.value.post_data_json


@pytest.mark.e2e
def test_job_date_range_keeps_calendar_dates_and_one_sided_movie_year(filter_page: Page) -> None:
    page = filter_page
    page.locator("#jobAddedFilter").select_option("date_range")
    page.locator("#jobAddedFrom").fill("2025-01-01")
    page.locator("#jobAddedTo").fill("2025-12-31")
    page.locator("#jobMovieYearMode").select_option("range")
    page.locator("#jobMovieYearTo").fill("2024")
    config = _submit_job(page)
    assert config["added_from"] == "2025-01-01"
    assert config["added_to"] == "2025-12-31"
    assert config["movie_year_to"] == 2024
    assert "movie_year_from" not in config
    assert "latest_seasons" not in config


@pytest.mark.e2e
def test_irrelevant_type_filters_clear_when_library_selection_changes(filter_page: Page) -> None:
    page = filter_page
    page.locator("#jobSeasonMode").select_option("latest")
    page.locator("#jobLatestSeasons").fill("3")
    page.locator("#jobMovieYearMode").select_option("range")
    page.locator("#jobMovieYearFrom").fill("2020")
    page.locator("#jobLibraryAll").uncheck()
    page.locator("#jobLib_movies").check()
    expect(page.locator('#jobScanFilters [data-filter-type="tv"]')).to_be_hidden()
    expect(page.locator("#jobScanFilters [data-filter-summary]")).not_to_contain_text("TV:")
    page.locator("#jobLib_tv").check()
    page.locator("#jobLib_movies").uncheck()
    expect(page.locator('#jobScanFilters [data-filter-type="movie"]')).to_be_hidden()
    expect(page.locator("#jobSeasonMode")).to_have_value("all")
    page.locator("#jobLib_movies").check()
    expect(page.locator("#jobMovieYearMode")).to_have_value("all")
    config = _submit_job(page)
    assert "latest_seasons" not in config
    assert "movie_year_from" not in config


@pytest.mark.e2e
def test_clear_and_reopen_reset_filter_controls(filter_page: Page) -> None:
    page = filter_page
    page.locator("#jobAddedFilter").select_option("last_days")
    page.locator("#jobAddedLastDays").fill("7")
    page.locator("#jobSeasonMode").select_option("latest")
    page.locator("#jobScanFilters [data-filter-clear]").click()
    expect(page.locator("#jobAddedFilter")).to_have_value("all")
    expect(page.locator("#jobSeasonMode")).to_have_value("all")
    expect(page.locator("#jobScanFilters [data-filter-summary]")).to_have_text("All selected media")
    page.locator("#jobAddedFilter").select_option("last_days")
    page.locator("#newJobModal .modal-footer").get_by_role("button", name="Cancel").click()
    page.locator('button:has-text("Start New Job")').click()
    expect(page.locator("#jobScanFilters")).not_to_have_attribute("open", "")
    page.locator("#jobScanFilters > summary").click()
    expect(page.locator("#jobAddedFilter")).to_have_value("all")


@pytest.mark.e2e
@pytest.mark.parametrize(
    "invalid_case", ["fractional_days", "no_dates", "reversed_dates", "zero_seasons", "empty_years", "reversed_years"]
)
def test_invalid_filters_block_submission(filter_page: Page, invalid_case: str) -> None:
    page = filter_page
    sent = []
    page.on(
        "request",
        lambda request: (
            sent.append(request) if request.method == "POST" and request.url.endswith("/api/jobs") else None
        ),
    )
    if invalid_case == "fractional_days":
        page.locator("#jobAddedFilter").select_option("last_days")
        page.locator("#jobAddedLastDays").fill("1.5")
    elif invalid_case in {"no_dates", "reversed_dates"}:
        page.locator("#jobAddedFilter").select_option("date_range")
        if invalid_case == "reversed_dates":
            page.locator("#jobAddedFrom").fill("2026-02-01")
            page.locator("#jobAddedTo").fill("2026-01-01")
    elif invalid_case == "zero_seasons":
        page.locator("#jobSeasonMode").select_option("latest")
        page.locator("#jobLatestSeasons").fill("0")
    else:
        page.locator("#jobMovieYearMode").select_option("range")
        if invalid_case == "reversed_years":
            page.locator("#jobMovieYearFrom").fill("2026")
            page.locator("#jobMovieYearTo").fill("2020")
    page.locator("#newJobModal .modal-footer .btn-primary").click()
    expect(page.locator("#jobScanFilters input:invalid").first).to_be_visible()
    assert not sent


@pytest.mark.e2e
def test_unknown_library_type_keeps_both_type_filters_available(authed_page: Page, app_url: str) -> None:
    _seed(authed_page, libraries=[{"id": "mixed", "name": "Mixed", "type": "mixed"}])
    authed_page.goto(app_url)
    authed_page.wait_for_function("libraries.length === 1")
    authed_page.locator('button:has-text("Start New Job")').click()
    authed_page.locator("#jobScanFilters > summary").click()
    expect(authed_page.locator('#jobScanFilters [data-filter-type="tv"]')).to_be_visible()
    expect(authed_page.locator('#jobScanFilters [data-filter-type="movie"]')).to_be_visible()
    expect(authed_page.locator("[data-filter-scope-note]").first).to_contain_text("unknown")


@pytest.mark.e2e
def test_schedule_edit_round_trips_filters_and_inherited_order(authed_page: Page, app_url: str) -> None:
    config = {"added_filter": "last_days", "added_last_days": 30, "latest_seasons": 2, "movie_year_from": 2010}
    _seed(
        authed_page,
        schedules=[{"id": "filtered", "name": "Filtered", "config": config, "cron_expression": "0 3 * * *"}],
    )
    authed_page.route("**/api/schedules/filtered", lambda route: _fulfill_json(route, {"id": "filtered"}))
    authed_page.goto(f"{app_url}/automation#schedules")
    authed_page.wait_for_function("schedules.some(s => s.id === 'filtered') && libraries.length === 2")
    authed_page.evaluate("showEditScheduleModal('filtered')")
    expect(authed_page.locator("#scheduleLatestSeasons")).to_have_value("2")
    expect(authed_page.locator("#scheduleMovieYearFrom")).to_have_value("2010")
    expect(authed_page.locator("#scheduleAddedLastDays")).to_have_value("30")
    with authed_page.expect_request("**/api/schedules/filtered") as sent:
        authed_page.locator("#scheduleSubmitBtn").click()
    saved = sent.value.post_data_json["config"]
    assert {key: saved[key] for key in config} == config
    assert "sort_by" not in saved


@pytest.mark.e2e
@pytest.mark.parametrize(
    ("mode_id", "job_type"), [("scanModeRecent", "recently_added"), ("scanModeMarkers", "intro_credits")]
)
def test_non_full_scan_schedule_omits_full_scan_filters(
    authed_page: Page, app_url: str, mode_id: str, job_type: str
) -> None:
    _seed(authed_page)
    authed_page.goto(f"{app_url}/automation#schedules")
    authed_page.wait_for_function("libraries.length === 2")
    authed_page.locator('button:has-text("Add Schedule")').first.click()
    authed_page.locator("#scheduleName").fill("Recent")
    authed_page.locator("#scheduleScanFilters > summary").click()
    authed_page.locator("#scheduleSeasonMode").select_option("latest")
    authed_page.locator(f"#{mode_id}").check()
    expect(authed_page.locator("#scheduleScanFilters")).to_be_hidden()
    with authed_page.expect_request(
        lambda request: request.url.endswith("/api/schedules") and request.method == "POST"
    ) as sent:
        authed_page.locator("#scheduleSubmitBtn").click()
    config = sent.value.post_data_json["config"]
    assert config["job_type"] == job_type
    assert "added_filter" not in config
    assert "latest_seasons" not in config


@pytest.mark.e2e
def test_new_schedule_refreshes_all_library_types_after_server_specific_selection(
    authed_page: Page, app_url: str
) -> None:
    _seed(authed_page)
    movies = {"id": "movies", "name": "Movies", "type": "movie", "server_id": "plex-1"}
    tv = {"id": "tv", "name": "TV Shows", "type": "show", "server_id": "other"}
    authed_page.route(
        "**/api/libraries**",
        lambda route: _fulfill_json(
            route,
            {"libraries": [movies] if "server_id=plex-1" in route.request.url else [movies, tv]},
        ),
    )
    authed_page.goto(f"{app_url}/automation#schedules")
    authed_page.wait_for_function("libraries.length === 2")
    authed_page.locator('button:has-text("Add Schedule")').first.click()
    authed_page.locator("#scheduleScanFilters > summary").click()
    expect(authed_page.locator('#scheduleServer option[value="plex-1"]')).to_be_attached()
    authed_page.locator("#scheduleServer").select_option("plex-1")
    expect(authed_page.locator('#scheduleScanFilters [data-filter-type="tv"]')).to_be_hidden()
    authed_page.locator('#newScheduleModal .modal-footer button:has-text("Cancel")').click()
    authed_page.locator('button:has-text("Add Schedule")').first.click()
    authed_page.locator("#scheduleScanFilters > summary").click()
    expect(authed_page.locator('#scheduleScanFilters [data-filter-type="tv"]')).to_be_visible()
    expect(authed_page.locator('#scheduleScanFilters [data-filter-type="movie"]')).to_be_visible()
