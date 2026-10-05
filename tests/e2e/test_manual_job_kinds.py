"""Creation flows dispatch explicit targets; Job Details retains real Inspector links."""

from __future__ import annotations

from urllib.parse import parse_qs, quote, urlparse

import pytest
from playwright.sync_api import Page, Route, expect

from ._mocks import _fulfill_json, mock_dashboard_defaults, mock_media_servers_status

pytestmark = pytest.mark.e2e

_SERVERS = [
    {"id": "plex-1", "name": "Home Plex", "type": "plex", "enabled": True},
    {"id": "jf-1", "name": "Home Jellyfin", "type": "jellyfin", "enabled": True},
]
_KINDS = [
    ("previews", "manualKindPreviews", "/api/jobs/manual", "2"),
    ("intro_credits", "manualKindMarkers", "/api/markers/jobs", "3"),
    ("loudness", "manualKindLoudness", "/api/loudness/jobs", "3"),
]


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


@pytest.fixture
def creation_page(authed_page: Page, app_url: str) -> tuple[Page, list[dict]]:
    page = authed_page
    mock_dashboard_defaults(page)
    mock_media_servers_status(page, servers=_SERVERS)
    page.route("**/api/servers", lambda route: _fulfill_json(route, {"servers": _SERVERS}))
    page.route(
        "**/api/libraries**",
        lambda route: _fulfill_json(
            route,
            {"libraries": [{"id": "1", "name": "Movies", "type": "movie", "server_id": "plex-1"}]},
        ),
    )
    writes: list[dict] = []

    def capture_write(route: Route) -> None:
        if route.request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            writes.append({"path": urlparse(route.request.url).path, "body": route.request.post_data_json})
            _fulfill_json(route, {"id": "sample-created-job", "status": "pending"})
        else:
            route.fallback()

    page.route("**/api/**", capture_write)
    page.goto(f"{app_url}/")
    expect(page.get_by_role("button", name="Process a file or folder", exact=True)).to_be_visible()
    return page, writes


def _open_manual(page: Page, radio: str = "manualKindPreviews") -> None:
    page.get_by_role("button", name="Process a file or folder", exact=True).click()
    expect(page.locator("#manualTriggerModal")).to_be_visible()
    expect(page.locator("#manualServerScope option")).to_have_count(3)
    page.locator("#" + radio).check()


def _paste_paths(page: Page, paths: str) -> None:
    page.locator("#manualAdvanced summary").click()
    page.locator("#manualFilePaths").fill(paths)


@pytest.mark.parametrize(("kind", "radio", "endpoint", "priority"), _KINDS)
@pytest.mark.parametrize("force", [False, True])
def test_manual_kind_posts_only_its_supported_fields(
    creation_page: tuple[Page, list[dict]], kind: str, radio: str, endpoint: str, priority: str, force: bool
) -> None:
    page, writes = creation_page
    _open_manual(page)
    page.locator("#manualServerScope").select_option("jf-1")
    page.locator("#" + radio).check()
    expect(page.locator("#manualPriority")).to_have_value(priority)
    if kind != "previews":
        expect(page.locator("#manualPublishServerGroup")).to_be_hidden()
        page.locator("#manualSearchServer").select_option("plex-1")
    if kind == "previews":
        page.locator("#manualForceRegenerate" if force else "#manualMissingOnly").check()
    elif kind == "intro_credits":
        page.locator("#manualMarkersForce").set_checked(force)
    else:
        expect(page.locator("#manualMarkersForceGroup")).to_be_hidden()
        expect(page.locator("#manualProcessingModeGroup")).to_be_hidden()
        page.evaluate(
            "force => { document.getElementById('manualMarkersForce').checked = force; "
            "document.getElementById('manualForceRegenerate').checked = force; }",
            force,
        )
    _paste_paths(page, " /data/Movies/A.mkv \n/data/TV/Series\n/data/Movies/A.mkv")
    page.locator("#manualPriority").select_option("1")
    page.locator("#manualStartButton").click()
    expect(page.locator("#manualTriggerModal")).to_be_hidden()
    expected = {"file_paths": ["/data/Movies/A.mkv", "/data/TV/Series"], "priority": 1}
    if kind == "previews":
        expected.update(force_regenerate=force, server_id="jf-1")
    elif kind == "intro_credits":
        expected["force"] = force
    assert writes == [{"path": endpoint, "body": expected}]


@pytest.mark.parametrize(("kind", "radio", "endpoint", "priority"), _KINDS)
@pytest.mark.parametrize("paths", ["", "relative/file.mkv", "/data/../private/file.mkv", "Z:\\Movies\\file.mkv"])
def test_manual_cannot_submit_empty_or_non_container_paths(
    creation_page: tuple[Page, list[dict]], kind: str, radio: str, endpoint: str, priority: str, paths: str
) -> None:
    page, writes = creation_page
    _open_manual(page, radio)
    _paste_paths(page, paths)
    expect(page.locator("#manualStartButton")).to_be_disabled()
    page.evaluate("() => startManualJob()")
    expect(page.locator("#manualSubmissionError")).not_to_be_empty()
    assert writes == []


@pytest.mark.parametrize(("kind", "radio", "endpoint", "priority"), _KINDS)
def test_search_scope_is_applied_without_changing_selection_or_submitting(
    creation_page: tuple[Page, list[dict]], kind: str, radio: str, endpoint: str, priority: str
) -> None:
    page, writes = creation_page
    queries: list[dict] = []

    def search(route: Route) -> None:
        queries.append(parse_qs(urlparse(route.request.url).query))
        _fulfill_json(route, {"results": []})

    page.route("**/api/media/search?**", search)
    _open_manual(page, radio)
    page.locator("#manualServerScope" if kind == "previews" else "#manualSearchServer").select_option("jf-1")
    page.locator("#manualSearchInput").fill("Movie")
    expect(page.locator("#manualSearchResults")).to_contain_text("No matches")
    assert queries[-1] == {"q": ["Movie"], "server_id": ["jf-1"]}
    assert writes == []


def test_clear_all_clears_chips_and_pasted_paths(creation_page: tuple[Page, list[dict]]) -> None:
    page, writes = creation_page
    _open_manual(page)
    page.evaluate("() => manualAddSelection({kind:'file',label:'A',paths:['/data/A.mkv']})")
    _paste_paths(page, "/data/B.mkv")
    page.locator("#manualClearAll").click()
    expect(page.locator("#manualChips .manual-chip")).to_have_count(0)
    expect(page.locator("#manualFilePaths")).to_have_value("")
    expect(page.locator("#manualStartButton")).to_be_disabled()
    assert writes == []


def test_start_dialog_preserves_preview_filters_and_hides_them_for_other_kinds(
    creation_page: tuple[Page, list[dict]],
) -> None:
    page, writes = creation_page
    page.get_by_role("button", name="Start New Job", exact=True).click()
    expect(page.locator("#jobScanFiltersGroup")).to_be_visible()
    expect(page.locator("#jobSortBy option")).to_have_text(
        ["Default (server order)", "Newest added first", "Oldest added first", "Random"]
    )
    page.locator("#jobScanFiltersGroup summary").click()
    expect(page.locator("#jobAddedFilter")).to_be_visible()
    page.locator("#jobSortBy").select_option("random")
    for radio in ("jobKindMarkers", "jobKindLoudness"):
        page.locator("#" + radio).check()
        expect(page.locator("#jobScanFiltersGroup")).to_be_hidden()
        expect(page.locator("#jobSortByGroup")).to_be_hidden()
        expect(page.locator("#jobOwnRunnerFiltersNote")).to_be_visible()
    page.locator("#jobKindMarkers").check()
    page.locator("#jobMarkersModeCheckServers").check()
    expect(page.locator("#jobLibrariesGroup")).to_be_hidden()
    expect(page.locator("#jobOwnRunnerFiltersNote")).to_be_hidden()
    page.locator("#jobKindPreviews").check()
    expect(page.locator("#jobScanFiltersGroup")).to_be_visible()
    expect(page.locator("#jobSortBy")).to_have_value("random")
    assert writes == []


def test_files_use_native_inspector_links_and_keyboard_path_disclosure(
    creation_page: tuple[Page, list[dict]],
) -> None:
    page, writes = creation_page
    path = '/data/Movies/A "quoted" & special.mkv'
    bif = "/data/previews/Old Film.bif"
    files = [
        {"file": path, "outcome": "generated", "servers": [], "worker": "CPU Worker 2"},
        {"file": "Old Film", "bif_path": bif, "outcome": "generated", "servers": []},
        {"file": "Unknown file", "outcome": "failed", "servers": []},
    ]
    page.route("**/api/jobs/*/logs**", lambda route: _fulfill_json(route, {"logs": [], "total_lines": 0}))
    page.route(
        "**/api/jobs/*/files?**",
        lambda route: _fulfill_json(
            route,
            {"files": files, "page": 1, "total_pages": 1, "filtered_count": 3, "total": 3},
        ),
    )
    page.evaluate(
        """() => {
            jobs = [{id:'inspector-links',library_name:'Files',status:'completed',config:{},publishers:[],progress:{}}];
            openJobDetails('inspector-links', 'files');
        }"""
    )
    rows = page.locator("#fileResultsBody tr")
    expect(rows).to_have_count(3)
    for index, expected in (
        (0, "/inspector?path=" + quote(path, safe="")),
        (1, "/inspector?bif=" + quote(bif, safe="")),
    ):
        link = rows.nth(index).get_by_role("link", name="Open in the Inspector", exact=True)
        expect(link).to_have_attribute("href", expected)
        expect(link).to_have_attribute("target", "_blank")
        expect(link).to_have_attribute("rel", "noopener")
    expect(rows.nth(2).get_by_role("link")).to_have_count(0)
    summary = rows.first.locator("details summary")
    summary.focus()
    summary.press("Enter")
    expect(rows.first.locator("details code")).to_be_visible()
    expect(rows.first.locator("details code")).to_have_text(path)
    assert writes == []


def test_requested_paths_are_searchable_and_paged_without_invented_outcomes(
    creation_page: tuple[Page, list[dict]],
) -> None:
    page, writes = creation_page
    requests: list[str] = []
    page.route("**/api/jobs/*/logs**", lambda route: _fulfill_json(route, {"logs": [], "total_lines": 0}))

    def outcomes(route: Route) -> None:
        requests.append(route.request.url)
        _fulfill_json(route, {"files": [], "page": 1, "total_pages": 1, "filtered_count": 0, "total": 0})

    page.route("**/api/jobs/*/files?**", outcomes)
    page.evaluate(
        """() => {
            const paths = Array.from({length:10000}, (_,i) => '/data/item-' + String(i).padStart(5,'0'));
            paths[1] = paths[0];
            jobs = [{id:'requested-paths',library_name:'Requested paths',status:'pending',
                     config:{webhook_paths:paths},publishers:[],progress:{}}];
            openJobDetails('requested-paths', 'files', 'requested');
        }"""
    )
    rows = page.locator("#fileResultsBody tr")
    expect(rows).to_have_count(100)
    expect(page.locator("#fileResultsCount")).to_contain_text("10,000 requested paths")
    expect(page.locator("#fileOutcomeFilter")).to_be_hidden()
    expect(rows.locator("a")).to_have_count(0)
    assert rows.nth(0).locator("code").text_content() == rows.nth(1).locator("code").text_content()
    assert requests == []
    # A queue filter or background page refresh can remove this job from the visible rows.
    page.evaluate("jobs = []")
    page.locator("#filePerPageSelect").select_option("500")
    expect(rows).to_have_count(500)
    page.locator("#filePerPageSelect").select_option("50")
    expect(rows).to_have_count(50)
    page.locator("#filePaginationControls").get_by_role("link", name="200", exact=True).click()
    expect(rows.last).to_contain_text("item-09999")
    page.locator("#fileResultsSearch").fill("item-09999")
    expect(rows).to_have_count(1)
    expect(rows.first.locator("code")).to_have_text("/data/item-09999")
    expect(page.locator("#fileResultsCount")).to_contain_text("1 requested paths")
    page.locator("#fileResultsView").select_option("results")
    expect(page.locator("#requestedPathsNote")).to_be_hidden()
    expect(page.locator("#fileOutcomeFilter")).to_be_visible()
    expect(page.locator("#fileResultsBody")).to_contain_text("No matching files")
    assert len(requests) == 1
    assert writes == []


def test_worker_link_opens_job_outside_filtered_queue(creation_page: tuple[Page, list[dict]]) -> None:
    page, writes = creation_page
    target = {
        "id": "off-page-audio",
        "library_name": "Audio outside current queue filter",
        "kind": "loudness",
        "status": "running",
        "config": {"file_paths": ["/data/audio/movie.mkv"]},
        "publishers": [],
        "progress": {"processed_items": 1, "total_items": 3, "current_item": "Analyzing audio"},
    }
    reads: list[str] = []

    def read_job(route: Route) -> None:
        reads.append(urlparse(route.request.url).path)
        _fulfill_json(route, target)

    page.route("**/api/jobs/off-page-audio", read_job)
    page.route("**/api/jobs/off-page-audio/logs**", lambda route: _fulfill_json(route, {"logs": [], "total_lines": 0}))
    page.route("**/api/jobs/off-page-audio/files?**", lambda route: _fulfill_json(route, {"files": [], "total": 0}))
    page.evaluate("""() => {
        jobs = [];
        updateWorkerStatuses([{worker_id: 12, worker_type: 'CPU', worker_name: 'CPU Worker 1',
            status: 'processing', job_id: 'off-page-audio', job_kind: 'loudness',
            current_title: 'Audio movie', current_file: '/data/audio/movie.mkv'}]);
    }""")
    page.locator('[data-worker-job][data-job-id="off-page-audio"]').click()
    expect(page.locator("#logsModal")).to_be_visible()
    expect(page.locator("#logsModalHeader")).to_contain_text(target["library_name"])
    expect(page.locator("#logsAutoScroll")).to_be_checked()
    page.locator("#filesTab").click()
    page.locator("#fileResultsView").select_option("requested")
    expect(page.locator("#fileResultsBody code")).to_have_text("/data/audio/movie.mkv")
    assert reads and set(reads) == {"/api/jobs/off-page-audio"}
    assert page.evaluate("logsRefreshInterval !== null")
    assert page.evaluate("jobs.length") == 0
    assert writes == []


def test_files_footer_refreshes_files_and_logs_actions_return_on_logs(
    creation_page: tuple[Page, list[dict]],
) -> None:
    page, writes = creation_page
    page.set_viewport_size({"width": 390, "height": 844})
    file_reads: list[dict] = []
    page.route("**/api/jobs/footer-job/logs**", lambda route: _fulfill_json(route, {"logs": [], "total_lines": 0}))

    def files(route: Route) -> None:
        file_reads.append(parse_qs(urlparse(route.request.url).query))
        _fulfill_json(route, {"files": [], "total": 0, "filtered_count": 0, "total_pages": 1})

    page.route("**/api/jobs/footer-job/files?**", files)
    page.evaluate("""() => {
        jobs = [{id:'footer-job',library_name:'Movies',kind:'previews',status:'completed',
                 config:{},publishers:[],progress:{}}];
        openJobDetails('footer-job','files');
    }""")
    footer = page.locator("#logsModal .modal-footer")
    expect(footer).to_have_attribute("data-tab", "files")
    expect(footer.locator("[data-logs-footer]:visible")).to_have_count(0)
    expect(footer.get_by_role("button", name="Refresh files", exact=True)).to_be_visible()
    expect(footer.get_by_role("button", name="Close", exact=True)).to_be_visible()
    expect(page.locator("#fileResultsBody")).to_contain_text("No matching files")
    initial_reads = len(file_reads)
    with page.expect_response(lambda response: "/api/jobs/footer-job/files?" in response.url):
        footer.get_by_role("button", name="Refresh files", exact=True).click()
    assert len(file_reads) == initial_reads + 1
    assert all(read.get("page") == ["1"] and read.get("per_page") == ["100"] for read in file_reads)
    assert footer.bounding_box()["height"] < 100
    page.locator("#logsTab").click()
    expect(footer).to_have_attribute("data-tab", "logs")
    for label in ("Copy", "Download", "Refresh"):
        expect(footer.get_by_role("button", name=label, exact=True)).to_be_visible()
    expect(footer.locator("[data-files-footer]")).to_be_hidden()
    page.locator("#filesTab").click()
    expect(footer.locator("[data-logs-footer]:visible")).to_have_count(0)
    assert writes == []
