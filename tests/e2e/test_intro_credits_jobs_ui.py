"""E2E: Intro & Credits jobs on the dashboard — Start job modal, queue rows, publishers block, Files panel, pause.

Every API the page reads is mocked, so each test pins what the page sends and how it renders the job shapes the
backend produces (``job.kind == "intro_credits"``, ``markers_*`` counters, ``reason_code`` on waiting rows).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from playwright.sync_api import Page, Route, expect

from ._mocks import _fulfill_json, mock_dashboard_defaults, mock_media_servers_status

_SERVERS = [
    {"id": "plex-1", "name": "Home Plex", "type": "plex", "enabled": True, "status": "connected", "url": "http://p"},
    {
        "id": "jf-1",
        "name": "Home Jellyfin",
        "type": "jellyfin",
        "enabled": True,
        "status": "connected",
        "url": "http://j",
    },
]
# Library ids repeat across servers (Plex and Jellyfin both have a "1"), so the body must carry server + library pairs.
_LIBRARIES = [
    {
        "id": "1",
        "name": "Movies",
        "type": "movie",
        "server_id": "plex-1",
        "server_name": "Home Plex",
        "server_type": "plex",
    },
    {
        "id": "2",
        "name": "TV Shows",
        "type": "show",
        "server_id": "plex-1",
        "server_name": "Home Plex",
        "server_type": "plex",
    },
    {
        "id": "1",
        "name": "Anime",
        "type": "show",
        "server_id": "jf-1",
        "server_name": "Home Jellyfin",
        "server_type": "jellyfin",
    },
]


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


def _job(job_id: str, **overrides) -> dict:
    job = {
        "id": job_id,
        "status": "completed",
        "created_at": "2026-09-14T01:00:00+00:00",
        "started_at": "2026-09-14T01:00:01+00:00",
        "completed_at": "2026-09-14T01:05:00+00:00",
        "library_id": None,
        "library_name": "Rick and Morty S01",
        "server_id": None,
        "server_name": None,
        "server_type": None,
        "publishers": [],
        "progress": {
            "percent": 100.0,
            "current_item": "",
            "total_items": 11,
            "processed_items": 11,
            "speed": "0.0x",
            "current_file": "",
            "workers": [],
            "outcome": None,
            "retry_eta": None,
            "retry_wait_total": None,
        },
        "error": None,
        "config": {},
        "paused": False,
        "priority": 2,
        "parent_schedule_id": "",
        "kind": "previews",
    }
    for key, value in overrides.items():
        if key == "progress":
            job["progress"].update(value)
        else:
            job[key] = value
    return job


def _preview_job() -> dict:
    return _job(
        "e64567e1-0000-4000-8000-000000000001",
        config={"source": "sonarr"},
        publishers=[
            {
                "server_id": "plex-1",
                "server_name": "Home Plex",
                "server_type": "plex",
                "counts": {"published": 11},
                "frame_sources": {"extracted": 11},
            }
        ],
        progress={"outcome": {"generated": 11}},
    )


# Runs the dashboard's 1 s countdown ticker once and reads the element straight after, before any re-render.
_TICK_AND_READ = "(selector) => { _updateElapsedTimers(); return document.querySelector(selector).textContent.trim(); }"


def _markers_job(job_id: str = "7c1f09aa-0000-4000-8000-000000000002", **overrides) -> dict:
    base = {
        "library_name": "Intro & Credits · Rick and Morty S01",
        "kind": "intro_credits",
        "priority": 3,
        "config": {
            "kind": "intro_credits",
            "source": "sonarr",
            "libraries": [],
            "file_paths": ["/data/tv/Rick and Morty/Season 01/e01.mkv"],
            "follows_job_id": "e64567e1-0000-4000-8000-000000000001",
            "force": False,
        },
        "publishers": [
            {
                "server_id": "plex-1",
                "server_name": "Home Plex",
                "server_type": "plex",
                "counts": {"markers_written": 8, "markers_needs_review": 3},
            },
            {
                "server_id": "jf-1",
                "server_name": "Home Jellyfin",
                "server_type": "jellyfin",
                "counts": {"markers_written": 8, "markers_up_to_date": 3},
            },
            {
                "server_id": "emby-1",
                "server_name": "Home Emby",
                "server_type": "emby",
                "counts": {"markers_skipped": 11},
                "messages": {"markers_skipped": "Emby plugin not installed"},
            },
        ],
        "progress": {"outcome": {"markers_published": 8, "markers_needs_review": 3}},
    }
    base.update(overrides)
    return _job(job_id, **base)


def _serve_jobs(page: Page, jobs_ref: dict) -> None:
    """GET /api/jobs?... returns ``jobs_ref["jobs"]`` at call time, so a test can change the queue mid-test."""
    page.route(
        "**/api/jobs?**",
        lambda r: _fulfill_json(r, {"jobs": jobs_ref["jobs"], "total": len(jobs_ref["jobs"]), "page": 1, "pages": 1}),
    )


@pytest.fixture
def dashboard(authed_page: Page, app_url: str):
    """Dashboard with two servers' libraries, and a ``load(jobs, processing_paused=False)`` helper."""
    mock_dashboard_defaults(authed_page)
    mock_media_servers_status(authed_page, servers=_SERVERS)
    authed_page.route("**/api/libraries", lambda r: _fulfill_json(r, {"libraries": _LIBRARIES}))
    jobs_ref: dict = {"jobs": []}
    state = {"paused": False}
    _serve_jobs(authed_page, jobs_ref)
    authed_page.route("**/api/processing/state", lambda r: _fulfill_json(r, {"paused": state["paused"]}))

    def load(jobs: list[dict] | None = None, processing_paused: bool = False) -> Page:
        jobs_ref["jobs"] = list(jobs or [])
        state["paused"] = processing_paused
        authed_page.goto(f"{app_url}/")
        authed_page.wait_for_load_state("domcontentloaded")
        return authed_page

    load.jobs_ref = jobs_ref  # type: ignore[attr-defined]
    return load


def _capture_posts(page: Page, pattern: str, response: dict, status: int = 201) -> list[dict]:
    captured: list[dict] = []

    def handler(route: Route) -> None:
        if route.request.method != "POST":
            route.continue_()
            return
        captured.append(route.request.post_data_json or {})
        _fulfill_json(route, response, status=status)

    page.route(pattern, handler)
    return captured


def _open_start_modal(page: Page) -> None:
    page.locator('button:has-text("Start New Job")').first.click()
    expect(page.locator("#newJobForm")).to_be_visible(timeout=3000)
    expect(page.locator('.job-library-checkbox[data-server-id="plex-1"][value="2"]')).to_be_attached(timeout=3000)


def _start_button(page: Page):
    return page.locator('#newJobModal .modal-footer button:has-text("Start Job")')


@pytest.mark.e2e
class TestStartJobModalIntroCredits:
    def test_choosing_intro_credits_hides_preview_controls_and_defaults_to_low(self, dashboard) -> None:
        page = dashboard()
        _open_start_modal(page)
        expect(page.locator("#jobKindPreviews")).to_be_checked()
        expect(page.locator("#jobProcessingModeGroup")).to_be_visible()
        expect(page.locator("#jobSortByGroup")).to_be_visible()
        expect(page.locator("#jobMarkersForce")).to_be_hidden()

        page.locator("#jobKindMarkers").check()

        expect(page.locator("#jobProcessingModeGroup")).to_be_hidden()
        expect(page.locator("#jobSortByGroup")).to_be_hidden()
        expect(page.locator("#jobMarkersForce")).to_be_visible()
        expect(page.locator("#jobMarkersForce")).not_to_be_checked()
        expect(page.locator("#jobPriority")).to_have_value("3")
        tip = page.locator("#jobKindMarkersInfo.info-icon")
        assert (tip.get_attribute("data-bs-original-title") or tip.get_attribute("title")) == (
            "Finds Skip Intro / Skip Credits markers for the chosen libraries and sends them to every server with "
            "Intro & Credits turned on. Runs at low priority on the same workers as previews."
        )

        page.locator("#jobKindPreviews").check()
        expect(page.locator("#jobProcessingModeGroup")).to_be_visible()
        expect(page.locator("#jobSortByGroup")).to_be_visible()
        expect(page.locator("#jobMarkersForce")).to_be_hidden()
        expect(page.locator("#jobPriority")).to_have_value("2")

    def test_two_ticked_libraries_post_server_library_pairs(self, dashboard) -> None:
        page = dashboard()
        markers_posts = _capture_posts(page, "**/api/markers/jobs", {"id": "ic-1", "kind": "intro_credits"})
        preview_posts = _capture_posts(page, "**/api/jobs", {"id": "job-1"})
        _open_start_modal(page)
        page.locator("#jobKindMarkers").check()
        page.locator("#jobLibraryAll").uncheck()
        page.locator('.job-library-checkbox[data-server-id="plex-1"][value="1"]').check()
        page.locator('.job-library-checkbox[data-server-id="plex-1"][value="2"]').check()

        with page.expect_request("**/api/markers/jobs"):
            _start_button(page).click()

        expect(page.locator("#newJobModal")).to_be_hidden(timeout=3000)
        assert len(markers_posts) == 1
        body = markers_posts[0]
        assert body["libraries"] == [
            {"server_id": "plex-1", "library_id": "1"},
            {"server_id": "plex-1", "library_id": "2"},
        ]
        assert body["priority"] == 3
        assert body["force"] is False
        assert body["library_name"] == "Intro & Credits: Movies, TV Shows"
        assert preview_posts == []

    def test_same_library_id_on_two_servers_keeps_each_server(self, dashboard) -> None:
        page = dashboard()
        posts = _capture_posts(page, "**/api/markers/jobs", {"id": "ic-1"})
        _open_start_modal(page)
        page.locator("#jobKindMarkers").check()
        page.locator("#jobLibraryAll").uncheck()
        page.locator('.job-library-checkbox[data-server-id="plex-1"][value="1"]').check()
        page.locator('.job-library-checkbox[data-server-id="jf-1"][value="1"]').check()

        with page.expect_request("**/api/markers/jobs"):
            _start_button(page).click()

        assert posts[0]["libraries"] == [
            {"server_id": "plex-1", "library_id": "1"},
            {"server_id": "jf-1", "library_id": "1"},
        ]
        assert posts[0]["library_name"] == "Intro & Credits: Movies, Anime"

    def test_all_libraries_posts_an_empty_list_and_force_when_ticked(self, dashboard) -> None:
        page = dashboard()
        posts = _capture_posts(page, "**/api/markers/jobs", {"id": "ic-1"})
        _open_start_modal(page)
        page.locator("#jobKindMarkers").check()
        page.locator("#jobMarkersForce").check()
        page.locator("#jobPriority").select_option("1")

        with page.expect_request("**/api/markers/jobs"):
            _start_button(page).click()

        assert posts[0]["libraries"] == []
        assert posts[0]["force"] is True
        assert posts[0]["priority"] == 1
        assert posts[0]["library_name"] == "Intro & Credits: All Libraries"

    def test_previews_still_post_the_old_body_to_the_jobs_endpoint(self, dashboard) -> None:
        page = dashboard()
        markers_posts = _capture_posts(page, "**/api/markers/jobs", {"id": "ic-1"})
        preview_posts = _capture_posts(page, "**/api/jobs", {"id": "job-1"})
        _open_start_modal(page)
        page.locator("#jobLibraryAll").uncheck()
        page.locator('.job-library-checkbox[data-server-id="plex-1"][value="2"]').check()
        page.locator("#jobRegenerateAll").check()

        with page.expect_request("**/api/jobs"):
            _start_button(page).click()

        assert markers_posts == []
        assert preview_posts == [
            {
                "library_ids": ["2"],
                "library_name": "TV Shows",
                "priority": 2,
                "config": {"force_generate": True},
                "server_id": "plex-1",
            }
        ]

    def test_check_servers_hides_the_libraries_and_posts_to_the_reconcile_route(self, dashboard) -> None:
        page = dashboard()
        reconcile_posts = _capture_posts(
            page, "**/api/markers/reconcile", {"job_id": "rc-1", "already_queued": False}, status=202
        )
        markers_posts = _capture_posts(page, "**/api/markers/jobs", {"id": "ic-1"})
        _open_start_modal(page)
        expect(page.locator("#jobMarkersModeGroup")).to_be_hidden()
        page.locator("#jobKindMarkers").check()
        expect(page.locator("#jobMarkersModeGroup")).to_be_visible()
        expect(page.locator("#jobMarkersModeFind")).to_be_checked()
        expect(page.locator("#jobLibrariesGroup")).to_be_visible()

        page.locator("#jobMarkersModeCheckServers").check()

        expect(page.locator("#jobLibrariesGroup")).to_be_hidden()
        expect(page.locator("#jobMarkersForce")).to_be_hidden()
        expect(page.locator("#jobProcessingModeGroup")).to_be_hidden()
        expect(page.locator("#jobPriority")).to_have_value("3")
        tip = page.locator("#jobMarkersCheckServersInfo.info-icon")
        assert (tip.get_attribute("data-bs-original-title") or tip.get_attribute("title")) == (
            "Checks that every server with Intro & Credits on still shows the markers this app sent, and sends them again "
            "where they're missing or changed (unless that server is set to keep its own). Covers all servers and libraries."
        )
        page.locator("#jobPriority").select_option("2")
        with page.expect_request("**/api/markers/reconcile"):
            _start_button(page).click()

        expect(page.locator("#newJobModal")).to_be_hidden(timeout=3000)
        assert reconcile_posts == [{"priority": 2}]
        assert markers_posts == []
        expect(page.locator("#toastBody")).to_have_text("Intro & Credits · Check servers has been queued", timeout=3000)

    def test_check_servers_already_queued_says_so_whatever_priority_was_picked(self, dashboard) -> None:
        page = dashboard()
        reconcile_posts = _capture_posts(
            page, "**/api/markers/reconcile", {"job_id": "rc-0", "already_queued": True}, status=202
        )
        _open_start_modal(page)
        page.locator("#jobKindMarkers").check()
        page.locator("#jobMarkersModeCheckServers").check()
        page.locator("#jobPriority").select_option("1")
        with page.expect_request("**/api/markers/reconcile"):
            _start_button(page).click()
        expect(page.locator("#newJobModal")).to_be_hidden(timeout=3000)
        assert reconcile_posts == [{"priority": 1}]
        expect(page.locator("#toastBody")).to_have_text("A Check servers job is already queued", timeout=3000)

    def test_check_servers_already_there_but_paused_says_how_to_go_on(self, dashboard) -> None:
        page = dashboard()
        _capture_posts(
            page, "**/api/markers/reconcile", {"job_id": "rc-0", "already_queued": True, "paused": True}, status=202
        )
        _open_start_modal(page)
        page.locator("#jobKindMarkers").check()
        page.locator("#jobMarkersModeCheckServers").check()
        with page.expect_request("**/api/markers/reconcile"):
            _start_button(page).click()
        expect(page.locator("#newJobModal")).to_be_hidden(timeout=3000)
        expect(page.locator("#toastBody")).to_have_text(
            "A Check servers job is paused. Resume or cancel it on the dashboard.", timeout=3000
        )

    @pytest.mark.parametrize(
        ("answer", "toast"),
        [
            ({"job_id": "rc-1", "already_queued": False}, "Intro & Credits · Check servers has been queued"),
            ({"job_id": "rc-0", "already_queued": True}, "A Check servers job is already queued"),
            (
                {"job_id": "rc-0", "already_queued": True, "paused": True},
                "A Check servers job is paused. Resume or cancel it on the dashboard.",
            ),
        ],
        ids=["queued", "already-queued", "paused"],
    )
    def test_rerun_of_a_check_servers_job_answers_like_the_modal(self, dashboard, answer, toast) -> None:
        finished = _markers_job(
            library_name="Intro & Credits · Check servers",
            config={
                "kind": "intro_credits",
                "source": "reconcile",
                "reconcile": True,
                "libraries": [],
                "file_paths": [],
            },
        )
        page = dashboard([finished])
        posts = _capture_posts(page, f"**/api/jobs/{finished['id']}/reprocess", answer, status=202)
        rerun = page.locator(f"#job-row-{finished['id']} button[aria-label='Re-run job']")
        expect(rerun).to_be_visible(timeout=5000)
        with page.expect_request(f"**/api/jobs/{finished['id']}/reprocess"):
            rerun.click()
        expect(page.locator("#toastBody")).to_have_text(toast, timeout=3000)
        assert posts == [{}]

    def test_check_servers_with_intro_and_credits_off_everywhere_says_why(self, dashboard) -> None:
        page = dashboard()
        _capture_posts(
            page,
            "**/api/markers/reconcile",
            {"job_id": None, "reason": "Intro & Credits is off on every server"},
            status=200,
        )
        _open_start_modal(page)
        page.locator("#jobKindMarkers").check()
        page.locator("#jobMarkersModeCheckServers").check()
        with page.expect_request("**/api/markers/reconcile"):
            _start_button(page).click()
        expect(page.locator("#toastBody")).to_have_text("Intro & Credits is off on every server", timeout=3000)
        expect(page.locator("#newJobModal")).to_be_hidden(timeout=3000)

    def test_find_markers_again_shows_the_libraries_and_reopening_resets_the_mode(self, dashboard) -> None:
        page = dashboard()
        _open_start_modal(page)
        page.locator("#jobKindMarkers").check()
        page.locator("#jobMarkersModeCheckServers").check()
        page.locator("#jobMarkersModeFind").check()
        expect(page.locator("#jobLibrariesGroup")).to_be_visible()
        expect(page.locator("#jobMarkersForce")).to_be_visible()
        page.locator("#jobMarkersModeCheckServers").check()
        page.locator("#jobKindPreviews").check()
        expect(page.locator("#jobMarkersModeGroup")).to_be_hidden()
        expect(page.locator("#jobLibrariesGroup")).to_be_visible()
        page.locator("#jobKindMarkers").check()
        page.locator("#newJobModal .btn-close").click()
        expect(page.locator("#newJobModal")).to_be_hidden(timeout=3000)

        _open_start_modal(page)
        page.locator("#jobKindMarkers").check()

        expect(page.locator("#jobMarkersModeFind")).to_be_checked()
        expect(page.locator("#jobLibrariesGroup")).to_be_visible()

    def test_reopening_the_modal_resets_the_job_type_to_previews(self, dashboard) -> None:
        page = dashboard()
        _open_start_modal(page)
        page.locator("#jobKindMarkers").check()
        page.locator("#newJobModal .btn-close").click()
        expect(page.locator("#newJobModal")).to_be_hidden(timeout=3000)

        _open_start_modal(page)

        expect(page.locator("#jobKindPreviews")).to_be_checked()
        expect(page.locator("#jobProcessingModeGroup")).to_be_visible()
        expect(page.locator("#jobPriority")).to_have_value("2")

    def test_reopening_after_a_preview_job_keeps_the_chosen_priority(self, dashboard) -> None:
        page = dashboard()
        _open_start_modal(page)
        page.locator("#jobPriority").select_option("1")
        page.locator("#newJobModal .btn-close").click()
        expect(page.locator("#newJobModal")).to_be_hidden(timeout=3000)

        _open_start_modal(page)

        expect(page.locator("#jobPriority")).to_have_value("1")


def _row_ids(page: Page) -> list[str]:
    return page.locator("#jobQueue tr.job-row").evaluate_all("rows => rows.map(r => r.id.replace('job-row-', ''))")


@pytest.mark.e2e
class TestQueueRows:
    def test_follow_up_renders_under_its_preview_job_with_the_markers_breakdown(self, dashboard) -> None:
        other = _job("aaaaaaaa-0000-4000-8000-000000000009", library_name="Other show")
        preview = _preview_job()
        follower = _markers_job()
        # The API lists newest first: the follower comes before its preview job.
        page = dashboard([follower, other, preview])
        expect(page.locator(f"#job-row-{follower['id']}")).to_be_visible(timeout=5000)

        assert _row_ids(page) == [other["id"], preview["id"], follower["id"]]
        row = page.locator(f"#job-row-{follower['id']}")
        expect(row).to_contain_text("↳")
        expect(row).to_contain_text("follows e64567e1")
        expect(row.locator(".job-kind-badge")).to_have_text("Intro & Credits")
        expect(page.locator(f"#job-row-{preview['id']} .job-kind-badge")).to_have_count(0)

        page.locator(f"#job-files-toggle-{follower['id']}").click()
        detail = page.locator(f"#job-detail-{follower['id']}")
        expect(detail).to_be_visible()
        expect(detail).to_contain_text("Markers written × 8")
        expect(detail).to_contain_text("Needs review × 3")
        expect(detail).to_contain_text("Up to date × 3")
        expect(detail).to_contain_text("Skipped × 11 · Emby plugin not installed")
        expect(detail).not_to_contain_text("Generated")
        expect(detail).not_to_contain_text("Reused")
        plex_line = detail.locator("div", has_text="Home Plex").last
        expect(plex_line).to_contain_text(re.compile(r"Markers written × 8\s*Needs review × 3"))

    def test_a_quote_in_a_job_error_stays_inside_the_status_tooltip(self, dashboard) -> None:
        # An Intro & Credits job fails with f"{type(exc).__name__}: {exc}", which can quote a file name.
        error = 'FileNotFoundError: [Errno 2] No such file: \'/data/x " onmouseover="window.__xss=1" y.mkv\''
        job = _markers_job(status="failed", error=error, progress={"outcome": None})
        page = dashboard([job])
        row = page.locator(f"#job-row-{job['id']}")
        expect(row).to_be_visible(timeout=5000)

        assert row.locator("[onmouseover]").count() == 0
        badge = row.locator("[data-bs-toggle='tooltip']").filter(has_text="Failed").first
        tooltip = badge.evaluate("el => el.getAttribute('data-bs-original-title') || el.getAttribute('title')")
        assert tooltip == error

    def test_preview_job_breakdown_is_unchanged(self, dashboard) -> None:
        preview = _preview_job()
        page = dashboard([preview])
        page.locator(f"#job-files-toggle-{preview['id']}").click()
        detail = page.locator(f"#job-detail-{preview['id']}")
        expect(detail).to_contain_text("Generated × 11")
        expect(page.locator(f"#job-row-{preview['id']}")).not_to_contain_text("↳")

    def test_follow_up_whose_preview_job_is_not_listed_renders_in_place(self, dashboard) -> None:
        other = _job("aaaaaaaa-0000-4000-8000-000000000009", library_name="Other show")
        follower = _markers_job()
        page = dashboard([follower, other])
        expect(page.locator(f"#job-row-{follower['id']}")).to_be_visible(timeout=5000)

        assert _row_ids(page) == [follower["id"], other["id"]]
        expect(page.locator(f"#job-row-{follower['id']}")).not_to_contain_text("↳")
        expect(page.locator(f"#job-row-{follower['id']}")).not_to_contain_text("follows")

    def test_skipped_suffix_only_when_every_row_skipped_with_one_message(self, dashboard) -> None:
        mixed = _markers_job(
            config={"kind": "intro_credits", "source": "manual", "libraries": [], "file_paths": []},
            publishers=[
                {
                    "server_id": "emby-1",
                    "server_name": "Mixed Emby",
                    "server_type": "emby",
                    "counts": {"markers_skipped": 4},
                    "messages": {"markers_skipped": None},
                },
                {
                    "server_id": "plex-1",
                    "server_name": "Partly Plex",
                    "server_type": "plex",
                    "counts": {"markers_skipped": 2, "markers_written": 1},
                    "messages": {"markers_skipped": "Plex Pass needed"},
                },
                {
                    "server_id": "jf-1",
                    "server_name": "Home Jellyfin",
                    "server_type": "jellyfin",
                    "counts": {"markers_none": 2, "failed": 1, "markers_waiting": 1},
                },
            ],
        )
        page = dashboard([mixed])
        page.locator(f"#job-files-toggle-{mixed['id']}").click()
        detail = page.locator(f"#job-detail-{mixed['id']}")

        expect(detail).to_contain_text("Skipped × 4")
        expect(detail).not_to_contain_text("Skipped × 4 ·")
        expect(detail).not_to_contain_text("Plex Pass needed")
        jellyfin = detail.locator("div", has_text="Home Jellyfin").last
        expect(jellyfin).to_contain_text(re.compile(r"Waiting × 1\s*No markers found × 2\s*Failed × 1"))

    def test_retry_job_is_labelled_and_offers_no_chain_retry_button(self, dashboard) -> None:
        eta = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        retry = _markers_job(
            "bbbbbbbb-0000-4000-8000-000000000003",
            library_name="Retry: Intro & Credits · Rick and Morty S01",
            status="pending",
            completed_at=None,
            publishers=[],
            config={
                "kind": "intro_credits",
                "source": "sonarr",
                "file_paths": ["/data/tv/e01.mkv"],
                "retry_attempt": 2,
                "retry_delay": 300,
                "retry_not_before": eta,
            },
            progress={
                "percent": 0,
                "outcome": None,
                "retry_eta": eta,
                "retry_wait_total": 300,
                "current_item": "Retry starting in 300s — waiting for these files to appear on disk or on a server",
            },
        )
        soon_eta = (datetime.now(UTC) + timedelta(seconds=45)).isoformat()
        soon = json.loads(json.dumps(retry))
        soon["id"] = "bbbbbbbb-0000-4000-8000-000000000004"
        soon["config"]["retry_not_before"] = soon_eta
        soon["progress"]["retry_eta"] = soon_eta
        page = dashboard([retry, soon])
        row = page.locator(f"#job-row-{retry['id']}")
        expect(row).to_be_visible(timeout=5000)

        expect(row.locator(".markers-retry-chip")).to_contain_text("Retry 2")
        # Minutes from 90 s up (rounded), seconds below.
        expect(row).to_contain_text("Retry starting in 5 min")
        expect(page.locator(f"#job-row-{soon['id']}")).to_contain_text(re.compile(r"Retry starting in 4\d s"))
        expect(row.locator('button[aria-label="Retry now"]')).to_have_count(0)
        expect(row).to_contain_text("Rick and Morty S01")

    def test_verify_job_keeps_its_prefix_and_has_no_retry_chip(self, dashboard) -> None:
        eta = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
        verify = _markers_job(
            "bbbbbbbb-0000-4000-8000-000000000005",
            library_name="Verify: Intro & Credits · Rick and Morty S01",
            status="pending",
            completed_at=None,
            publishers=[],
            config={
                "kind": "intro_credits",
                "source": "sonarr",
                "file_paths": ["/data/tv/e01.mkv"],
                "verify": True,
                "retry_delay": 600,
                "retry_not_before": eta,
            },
            progress={"percent": 0, "outcome": None, "retry_eta": eta, "retry_wait_total": 600},
        )
        soon = json.loads(json.dumps(verify))
        soon["id"] = "bbbbbbbb-0000-4000-8000-000000000006"
        soon_eta = (datetime.now(UTC) + timedelta(seconds=45)).isoformat()
        soon["config"]["retry_not_before"] = soon_eta
        soon["progress"]["retry_eta"] = soon_eta
        page = dashboard([verify, soon])
        row = page.locator(f"#job-row-{verify['id']}")
        expect(row).to_contain_text("Verify: Rick and Morty S01", timeout=5000)
        expect(row).not_to_contain_text("Intro & Credits · Rick")
        expect(row.locator(".markers-retry-chip")).to_have_count(0)
        # The owner's wording for a delayed check: minutes from 90 s up (rounded), seconds below.
        expect(row.locator("[data-scheduled-at]")).to_have_text("Checking again in 10 min")
        expect(page.locator(f"#job-row-{soon['id']} [data-scheduled-at]")).to_have_text(
            re.compile(r"^Checking again in 4\d s$")
        )
        # The 1 s ticker rewrites the countdown between renders with the same words (read once, no retrying).
        ticked = page.evaluate(_TICK_AND_READ, f"#job-row-{soon['id']} [data-scheduled-at]")
        assert re.fullmatch(r"Checking again in [34]\d s", ticked), ticked
        expect(row).not_to_contain_text("Retry starting")

    def test_running_verify_card_checks_again_without_the_retry_wording(self, dashboard) -> None:
        eta = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
        waiting = {"percent": 0, "outcome": None, "retry_eta": eta, "retry_wait_total": 600}
        verify = _markers_job(
            "bbbbbbbb-0000-4000-8000-000000000007",
            library_name="Verify: Intro & Credits · Rick and Morty S01",
            status="running",
            completed_at=None,
            publishers=[],
            config={"kind": "intro_credits", "source": "sonarr", "file_paths": ["/a.mkv"], "verify": True},
            progress=dict(waiting),
        )
        retry = _markers_job(
            "bbbbbbbb-0000-4000-8000-000000000008",
            library_name="Retry: Intro & Credits · Rick and Morty S01",
            status="running",
            completed_at=None,
            publishers=[],
            config={"kind": "intro_credits", "source": "sonarr", "file_paths": ["/a.mkv"], "retry_attempt": 1},
            progress=dict(waiting),
        )
        page = dashboard([verify, retry])
        card = page.locator(f"#active-job-{verify['id']}")
        expect(card).to_be_visible(timeout=5000)
        expect(card.locator(".retry-countdown-label")).to_have_text("Checking again in 10 min")
        expect(card).to_contain_text("Waiting to check again")
        expect(card).not_to_contain_text("Backing off after a failure")
        expect(card).not_to_contain_text("Waiting to retry")
        expect(card.locator(".markers-retry-chip")).to_have_count(0)
        ticked = page.evaluate(_TICK_AND_READ, f"#active-job-{verify['id']} .retry-countdown-label")
        assert ticked == "Checking again in 10 min", ticked
        retry_card = page.locator(f"#active-job-{retry['id']}")
        expect(retry_card.locator(".retry-countdown-label")).to_have_text("Next attempt in 10 min")
        ticked = page.evaluate(_TICK_AND_READ, f"#active-job-{retry['id']} .retry-countdown-label")
        assert ticked == "Next attempt in 10 min", ticked
        expect(retry_card).to_contain_text("Backing off after a failure — will try again automatically.")
        expect(retry_card).to_contain_text("Waiting to retry")


def _files_response(files: list[dict]) -> dict:
    return {
        "files": files,
        "page": 1,
        "total_pages": 1,
        "filtered_count": len(files),
        "total": len(files),
        "processed_total": len(files),
        "list_truncated": False,
    }


@pytest.mark.e2e
class TestFilesPanel:
    def _open_files(self, page: Page, job: dict, files: list[dict]) -> list[str]:
        requests: list[str] = []

        def files_handler(route: Route) -> None:
            requests.append(route.request.url)
            _fulfill_json(route, _files_response(files))

        page.route("**/api/jobs/*/logs**", lambda r: _fulfill_json(r, {"logs": [], "total_lines": 0}))
        page.route("**/api/jobs/*/files**", files_handler)
        expect(page.locator(f"#job-row-{job['id']}")).to_be_visible(timeout=5000)
        page.locator(f'#job-row-{job["id"]} button[aria-label="View logs"]').click()
        expect(page.locator("#logsModal")).to_be_visible(timeout=3000)
        page.locator("#filesTab").click()
        expect(page.locator("#fileResultsBody")).not_to_contain_text("Click to load", timeout=3000)
        return requests

    def test_needs_review_filter_sends_its_outcome_key(self, dashboard) -> None:
        job = _markers_job(config={"kind": "intro_credits", "source": "manual", "libraries": [], "file_paths": []})
        page = dashboard([job])
        requests = self._open_files(page, job, [])
        assert requests and all("outcome=" not in url for url in requests)

        expect(page.locator("#logsModalHeader")).to_contain_text("Intro & Credits")
        with page.expect_request(re.compile(r".*/api/jobs/.*/files\?.*")) as filtered:
            page.locator("#fileOutcomeFilter").select_option(label="Needs review")

        assert f"/api/jobs/{job['id']}/files?" in filtered.value.url
        assert "outcome=markers_needs_review" in filtered.value.url

    def test_filter_lists_only_the_outcomes_of_the_job_kind(self, dashboard) -> None:
        markers = _markers_job(config={"kind": "intro_credits", "source": "manual", "libraries": [], "file_paths": []})
        preview = _preview_job()
        page = dashboard([markers, preview])

        self._open_files(page, markers, [])
        visible = page.locator("#fileOutcomeFilter option").evaluate_all(
            "opts => opts.filter(o => !o.hidden && !o.disabled).map(o => o.value)"
        )
        assert visible == [
            "",
            "markers_published",
            "markers_up_to_date",
            "markers_needs_review",
            "markers_waiting",
            "markers_skipped",
            "markers_none",
            "markers_no_owners",
            "failed",
            "skipped_file_not_found",
        ]
        page.evaluate("() => bootstrap.Modal.getInstance(document.getElementById('logsModal')).hide()")
        expect(page.locator("#logsModal")).to_be_hidden(timeout=3000)

        page.locator(f'#job-row-{preview["id"]} button[aria-label="View logs"]').click()
        expect(page.locator("#logsModal")).to_be_visible(timeout=3000)
        visible = page.locator("#fileOutcomeFilter option").evaluate_all(
            "opts => opts.filter(o => !o.hidden && !o.disabled).map(o => o.value)"
        )
        assert visible == [
            "",
            "generated",
            "skipped_bif_exists",
            "skipped_not_indexed",
            "failed",
            "no_media_parts",
            "skipped_excluded",
            "skipped_file_not_found",
            "skipped_invalid_hash",
            "unresolved_plex",
        ]

    def test_file_rows_show_markers_statuses_and_the_not_in_library_retry(self, dashboard) -> None:
        job = _markers_job(config={"kind": "intro_credits", "source": "manual", "libraries": [], "file_paths": []})
        page = dashboard([job])
        files = [
            {
                "file": "/data/tv/Show/S01E01.mkv",
                "outcome": "markers_waiting",
                "reason": "intro 0:11–0:37 (chapters)",
                "worker": "Intro & Credits",
                "servers": [
                    {
                        "id": "plex-1",
                        "name": "Home Plex",
                        "type": "plex",
                        "status": "markers_waiting",
                        "reason_code": "not_in_library",
                    },
                    {"id": "jf-1", "name": "Home Jellyfin", "type": "jellyfin", "status": "markers_written"},
                ],
            },
            {
                "file": "/data/tv/Show/S01E02.mkv",
                "outcome": "markers_waiting",
                "reason": "intro 0:12–0:38 (chapters)",
                "worker": "Intro & Credits",
                "servers": [
                    {"id": "plex-1", "name": "Home Plex", "type": "plex", "status": "markers_waiting"},
                    {"id": "jf-1", "name": "Home Jellyfin", "type": "jellyfin", "status": "failed"},
                ],
            },
        ]

        self._open_files(page, job, files)

        rows = page.locator("#fileResultsBody tr")
        expect(rows).to_have_count(2)
        first, second = rows.nth(0), rows.nth(1)
        expect(first.locator("td").nth(1)).to_have_text("Waiting")
        expect(first).to_contain_text("Home Plex: Not in the server's library yet — will retry")
        expect(first).to_contain_text("intro 0:11–0:37 (chapters)")
        plex_pill = first.locator("td").nth(2).locator(".badge", has_text="Home Plex")
        expect(plex_pill).to_contain_text("Waiting")
        assert "Not in the server's library yet — will retry" in (plex_pill.get_attribute("title") or "")
        expect(first.locator("td").nth(2).locator(".badge", has_text="Home Jellyfin")).to_contain_text(
            "Markers written"
        )
        expect(second).not_to_contain_text("will retry")
        expect(second.locator("td").nth(2).locator(".badge", has_text="Home Plex")).to_contain_text("Waiting")
        expect(second.locator("td").nth(2).locator(".badge", has_text="Home Jellyfin")).to_contain_text("Failed")

    def test_file_rows_list_each_servers_own_message_but_not_routine_ones(self, dashboard) -> None:
        job = _markers_job(config={"kind": "intro_credits", "source": "manual", "libraries": [], "file_paths": []})
        page = dashboard([job])
        files = [
            {
                "file": "/data/tv/Synth/S01E02.mkv",
                "outcome": "markers_up_to_date",
                "reason": "intro 0:17–1:05 (chapters); credits 1:40–2:00 (chapters)",
                "worker": "Intro & Credits",
                "servers": [
                    {
                        "id": "plex-1",
                        "name": "Home Plex",
                        "type": "plex",
                        "status": "markers_up_to_date",
                        "message": "1 marker(s); keeping Plex's credits",
                    },
                    {
                        "id": "jf-1",
                        "name": "Home Jellyfin",
                        "type": "jellyfin",
                        "status": "markers_written",
                        "message": "2 marker(s)",
                    },
                    {
                        "id": "jf-2",
                        "name": "Old Jellyfin",
                        "type": "jellyfin",
                        "status": "failed",
                        "message": "Couldn't reach Jellyfin",
                    },
                ],
            }
        ]

        self._open_files(page, job, files)

        row = page.locator("#fileResultsBody tr").first
        notes = row.locator(".markers-server-note")
        expect(notes).to_have_count(2)
        expect(notes.nth(0)).to_have_text("Home Plex: 1 marker(s); keeping Plex's credits")
        expect(notes.nth(1)).to_have_text("Old Jellyfin: Couldn't reach Jellyfin")
        expect(notes.nth(1)).to_have_class(re.compile(r"\btext-danger-emphasis\b"))
        expect(row).not_to_contain_text("Home Jellyfin: 2 marker(s)")
        plex_pill = row.locator("td").nth(2).locator(".badge", has_text="Home Plex")
        assert plex_pill.get_attribute("title") == "Home Plex — 1 marker(s); keeping Plex's credits"
        jellyfin_pill = row.locator("td").nth(2).locator(".badge", has_text="Home Jellyfin")
        assert jellyfin_pill.get_attribute("title") == "Home Jellyfin — Markers written"

    def test_a_quote_in_a_file_name_or_reason_stays_inside_its_title(self, dashboard) -> None:
        # A media file's name is outside this app's control; '"' used to close the title attribute early.
        job = _markers_job(config={"kind": "intro_credits", "source": "manual", "libraries": [], "file_paths": []})
        page = dashboard([job])
        name = '/data/tv/Show/S01E01 " onmouseover="window.__xss=1" x=".mkv'
        reason = 'Couldn\'t read "x" onmouseover="window.__xss=2"'
        files = [{"file": name, "outcome": "failed", "reason": reason, "worker": "Intro & Credits", "servers": []}]

        self._open_files(page, job, files)

        row = page.locator("#fileResultsBody tr").first
        assert row.locator("small.text-truncate").get_attribute("title") == name
        assert row.locator("td").nth(3).locator("small").get_attribute("title") == reason
        assert row.locator("[onmouseover]").count() == 0

    def test_preview_file_pills_are_unchanged(self, dashboard) -> None:
        preview = _preview_job()
        page = dashboard([preview])
        files = [
            {
                "file": "/data/Movies/A.mkv",
                "outcome": "generated",
                "reason": "",
                "worker": "GPU Worker 1",
                "servers": [
                    {"id": "plex-1", "name": "Home Plex", "type": "plex", "status": "published", "message": "Queued"}
                ],
            }
        ]

        self._open_files(page, preview, files)

        pill = page.locator("#fileResultsBody tr").first.locator("td").nth(2).locator(".badge")
        expect(pill).to_have_text("Home Plex")
        assert pill.get_attribute("title") == "Home Plex — Generated"
        expect(page.locator("#fileResultsBody .markers-server-note")).to_have_count(0)
        expect(page.locator("#logsModalHeader")).not_to_contain_text("Intro & Credits")


def _running(job: dict, paused: bool = False) -> dict:
    job = json.loads(json.dumps(job))
    job.update({"status": "running", "completed_at": None, "paused": paused})
    job["progress"].update({"percent": 40.0, "processed_items": 4, "total_items": 10})
    return job


@pytest.mark.e2e
class TestPerJobPause:
    def test_pausing_a_markers_job_pauses_only_that_job(self, dashboard) -> None:
        job = _running(_markers_job(config={"kind": "intro_credits", "source": "manual", "file_paths": []}))
        page = dashboard([job])
        row = page.locator(f"#job-row-{job['id']}")
        pause = row.locator('button[aria-label="Pause job"]')
        expect(pause).to_be_visible(timeout=5000)
        assert pause.get_attribute("title") == "Pause this job"
        expect(page.locator("#activeJobsCount")).to_have_text("1 running")

        paused_job = _running(job, paused=True)

        def pause_handler(route: Route) -> None:
            dashboard.jobs_ref["jobs"] = [paused_job]
            _fulfill_json(route, paused_job)

        page.route(f"**/api/jobs/{job['id']}/pause", pause_handler)
        with page.expect_request(f"**/api/jobs/{job['id']}/pause") as req:
            pause.click()
        assert req.value.method == "POST"

        expect(row.locator(".status-dot")).to_have_text("Paused", timeout=3000)
        expect(row.locator('button[aria-label="Resume job"]')).to_have_attribute("title", "Resume this job")
        expect(page.locator("#globalPauseResumeQueue")).to_contain_text("Pause Processing")
        expect(page.locator("#globalPauseResumeQueue")).not_to_contain_text("Resume Processing")
        # Paused on its own, the job has handed its slot back: it isn't an active job any more.
        expect(page.locator("#activeJobsCount")).to_have_text("Idle")
        expect(page.locator(f"#active-job-{job['id']}")).to_have_count(0)
        tooltip = row.locator(".status-dot").get_attribute("data-bs-original-title") or row.locator(
            ".status-dot"
        ).get_attribute("title")
        assert "not using a job slot" in (tooltip or "")

    def test_resume_posts_to_the_job_resume_endpoint(self, dashboard) -> None:
        job = _running(_markers_job(config={"kind": "intro_credits", "source": "manual", "file_paths": []}), True)
        page = dashboard([job])
        resumed = _running(job)

        def resume_handler(route: Route) -> None:
            dashboard.jobs_ref["jobs"] = [resumed]
            _fulfill_json(route, {**resumed, "processing_paused": False})

        page.route(f"**/api/jobs/{job['id']}/resume", resume_handler)
        resume = page.locator(f'#job-row-{job["id"]} button[aria-label="Resume job"]')
        expect(resume).to_be_visible(timeout=5000)
        with page.expect_request(f"**/api/jobs/{job['id']}/resume") as req:
            resume.click()

        assert req.value.method == "POST"
        expect(page.locator(f"#job-row-{job['id']} .status-dot")).to_have_text("Running", timeout=3000)

    def test_preview_rows_get_no_per_job_pause_button(self, dashboard) -> None:
        job = _running(_preview_job())
        page = dashboard([job])
        expect(page.locator(f"#job-row-{job['id']}")).to_be_visible(timeout=5000)

        expect(page.locator(f'#job-row-{job["id"]} button[aria-label="Pause job"]')).to_have_count(0)
        expect(page.locator(f"#active-job-{job['id']}")).to_be_visible()

    def test_pause_all_shows_a_running_markers_job_as_held(self, dashboard) -> None:
        job = _running(_markers_job(config={"kind": "intro_credits", "source": "manual", "file_paths": []}))
        page = dashboard([job], processing_paused=True)
        row = page.locator(f"#job-row-{job['id']}")

        expect(row.locator(".status-dot")).to_have_text("Paused", timeout=5000)
        dot = row.locator(".status-dot")
        tooltip = dot.get_attribute("data-bs-original-title") or dot.get_attribute("title") or ""
        assert "Pause all" in tooltip
        # Held by Pause all it keeps its slot, so it stays an active job; its own Pause button still works.
        expect(page.locator(f"#active-job-{job['id']}")).to_be_visible()
        expect(row.locator('button[aria-label="Pause job"]')).to_be_visible()
