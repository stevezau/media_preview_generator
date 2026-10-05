"""User-facing regression coverage for dashboard filtering and worker identity."""

from __future__ import annotations

import json
from collections.abc import Callable
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Page, Route, expect

from . import test_intro_credits_jobs_ui as jobs_ui
from ._mocks import _fulfill_json, mock_dashboard_defaults, mock_worker_groups
from .test_intro_credits_jobs_ui import _job

dashboard = jobs_ui.dashboard


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup: None) -> None:
    return complete_setup


@pytest.mark.e2e
def test_queue_filters_keep_exact_library_pair_and_reset_pagination(authed_page: Page, app_url: str) -> None:
    page = authed_page
    mock_dashboard_defaults(page)
    options = {
        "servers": [
            {"id": "plex-1", "name": "Home Plex", "type": "plex"},
            {"id": "jellyfin-1", "name": "Studio Jellyfin", "type": "jellyfin"},
        ],
        "libraries": [
            {"id": "1", "name": "Movies", "server_id": "plex-1", "server_name": "Home Plex"},
            {"id": "1", "name": "Movies", "server_id": "jellyfin-1", "server_name": "Studio Jellyfin"},
        ],
    }
    job = _job("shared-preview", library_name="Shared archive")
    captured: list[dict[str, list[str]]] = []

    def jobs(route: Route) -> None:
        query = parse_qs(urlparse(route.request.url).query)
        captured.append(query)
        filtered = any(key in query for key in ("server_id", "library_id", "kind", "status", "q"))
        empty = query.get("q") == ["absent"]
        _fulfill_json(
            route,
            {
                "jobs": [] if empty else [job],
                "total": 0 if empty else 1 if filtered else 61,
                "page": int(query.get("page", ["1"])[0]),
                "pages": 1 if filtered else 2,
                "filter_options": options,
            },
        )

    page.route("**/api/jobs?**", jobs)
    page.goto(app_url + "/")
    queue = page.locator("#dashboard-all-jobs")
    expect(queue.get_by_role("option", name="Movies · Home Plex", exact=True)).to_have_count(1)
    expect(queue.get_by_role("option", name="Movies · Studio Jellyfin", exact=True)).to_have_count(1)
    with page.expect_response(lambda req: parse_qs(urlparse(req.url).query).get("page") == ["2"]):
        queue.get_by_role("link", name="Next", exact=True).click()
    with page.expect_response(lambda req: parse_qs(urlparse(req.url).query).get("server_id") == ["jellyfin-1"]):
        queue.get_by_label("Server", exact=True).select_option("jellyfin-1")
    assert captured[-1]["page"] == ["1"]
    queue.get_by_label("Library", exact=True).select_option(json.dumps(["jellyfin-1", "1"], separators=(",", ":")))
    queue.get_by_label("Job type", exact=True).select_option("previews")
    queue.get_by_label("Status", exact=True).select_option("completed")
    with page.expect_response(lambda req: parse_qs(urlparse(req.url).query).get("q") == ["Shared"]):
        queue.get_by_label("Search jobs", exact=True).fill("Shared")
    expected = {
        "server_id": ["jellyfin-1"],
        "library_server_id": ["jellyfin-1"],
        "library_id": ["1"],
        "kind": ["previews"],
        "status": ["completed"],
        "q": ["Shared"],
        "page": ["1"],
    }
    assert {key: captured[-1].get(key) for key in expected} == expected
    expect(queue.get_by_text("1 job matches these filters", exact=True)).to_be_visible()
    queue.get_by_label("Search jobs", exact=True).fill("absent")
    expect(queue.get_by_text("0 jobs match these filters", exact=True)).to_be_visible()
    with page.expect_response(
        lambda req: (
            "/api/jobs?" in req.url
            and not any(key in parse_qs(urlparse(req.url).query) for key in expected if key != "page")
        )
    ):
        queue.get_by_role("button", name="Clear filters", exact=True).click()
    for label in ("Server", "Library", "Job type", "Status", "Search jobs"):
        expect(queue.get_by_label(label, exact=True)).to_have_value("")
    expect(queue.get_by_role("button", name="Clear filters", exact=True)).to_be_hidden()


@pytest.mark.e2e
@pytest.mark.parametrize("width", [1440, 390])
def test_worker_identity_and_open_file_survive_progress_and_status_updates(
    authed_page: Page, app_url: str, width: int
) -> None:
    page = authed_page
    page.set_viewport_size({"width": width, "height": 1000 if width > 600 else 844})
    mock_dashboard_defaults(page)
    workers = [
        {
            "worker_id": 42,
            "worker_type": "GPU",
            "worker_name": "GPU Worker 1 (Archive rendering device)",
            "status": "processing",
            "progress_percent": 58,
            "current_title": "The Glass Observatory",
            "current_file": "/media/Movies/The Glass Observatory/film.mkv",
            "library_name": "Movies",
            "job_id": "preview-job",
            "job_kind": "previews",
            "ffmpeg_started": True,
            "speed": "4.2x",
            "eta_seconds": 84,
        },
        {
            "worker_id": 5,
            "worker_type": "CPU",
            "worker_name": "CPU Worker 1",
            "status": "processing",
            "progress_percent": 0,
            "current_title": "Across the Sound",
            "current_file": "/media/Movies/Across the Sound/film.mkv",
            "library_name": "Movies",
            "job_id": "loudness-job",
            "job_kind": "loudness",
            "current_phase": "Analyzing audio · stream 1/2",
            "ffmpeg_started": False,
        },
    ]
    page.route("**/api/jobs/workers", lambda route: _fulfill_json(route, {"workers": workers}))
    page.goto(app_url + "/")
    preview = page.locator('[data-worker-key="GPU_42"]')
    loudness = page.locator('[data-worker-key="CPU_5"]')
    expect(preview.get_by_text("#42", exact=True)).to_have_attribute("aria-label", "Worker ID 42")
    expect(loudness.get_by_text("#5", exact=True)).to_have_attribute("aria-label", "Worker ID 5")
    expect(preview.locator("[data-current-job]")).to_contain_text("Movies")
    expect(preview.locator("[data-current-job]")).to_contain_text("Previews")
    expect(preview.locator("[data-worker-job]")).to_have_attribute("data-job-id", "preview-job")
    expect(loudness.locator("[data-current-job]")).to_contain_text("Plex loudness")
    expect(loudness.locator("[data-percent]")).to_contain_text("stream 1/2")
    assert loudness.locator("[data-progress-wrap]").get_attribute("aria-valuenow") is None
    preview.locator("[data-file] summary").click()
    expect(preview.locator("[data-file-path]")).to_be_visible()
    workers[0]["progress_percent"] = 67.3
    page.evaluate("workers => updateWorkerStatuses(workers)", workers)
    expect(preview.locator("[data-percent]")).to_have_text("67.3%")
    expect(preview.locator("[data-file]")).to_have_attribute("open", "")
    expect(preview.locator("[data-file-path]")).to_have_text(workers[0]["current_file"])
    workers[0].update(paused=True, retiring=True)
    page.evaluate("workers => updateWorkerStatuses(workers)", workers)
    expect(preview.get_by_text("Paused · finishing after resume", exact=True)).to_be_visible()
    name_box = preview.locator("[data-name]").bounding_box()
    status_box = preview.locator("[data-status-badge]").bounding_box()
    assert name_box and status_box and status_box["y"] >= name_box["y"] + name_box["height"]
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    workers[0].update(status="idle", paused=False, retiring=False, job_id=None)
    page.evaluate("workers => updateWorkerStatuses(workers)", workers)
    expect(preview.locator("[data-current-job]")).to_be_hidden()
    expect(preview.get_by_text("#42", exact=True)).to_be_visible()


@pytest.mark.e2e
def test_hidden_occupied_group_remains_discoverable_and_keeps_paused_state(authed_page: Page, app_url: str) -> None:
    page = authed_page
    page.set_viewport_size({"width": 390, "height": 844})
    mock_dashboard_defaults(page)
    state = mock_worker_groups(page)["state"]
    state["groups"] = [
        {
            "id": f"group-{index}",
            "name": "Archive work" if index == 7 else f"Idle group {index}",
            "resource": "cpu",
            "device": None,
            "count": 1,
            "enabled": True,
            "job_types": ["previews"],
            "availability": {"mode": "always", "windows": []},
        }
        for index in range(1, 8)
    ]
    state["capacity"]["groups"] = [
        {"id": f"group-{index}", "available": 0 if index == 7 else 1, "busy": int(index == 7), "finishing": 0}
        for index in range(1, 8)
    ]
    worker = {
        "worker_id": 9,
        "worker_type": "CPU",
        "worker_name": "CPU Worker 1",
        "group_id": "group-7",
        "group_name": "Archive work",
        "status": "processing",
        "paused": True,
        "progress_percent": 58,
        "current_title": "Paused archive file",
        "job_kind": "previews",
        "ffmpeg_started": True,
    }
    page.route("**/api/jobs/workers", lambda route: _fulfill_json(route, {"workers": [worker]}))
    page.goto(app_url + "/")
    controls = page.locator("#workerGroupFilters")
    expect(page.get_by_text("Archive work", exact=True)).to_be_hidden()
    expect(page.locator("#workerGroupSummary")).to_contain_text("7 groups · 1 occupied")
    outside = controls.get_by_role("button", name="1 occupied group outside this view", exact=True)
    expect(outside).to_be_visible()
    outside.focus()
    page.keyboard.press("Enter")
    expect(page.get_by_text("Archive work", exact=True)).to_be_visible()
    expect(page.get_by_text("1 paused", exact=True)).to_be_visible()
    expect(page.locator("#workerGroupSummary")).to_contain_text("Showing 1 of 1 matching groups")
    controls.get_by_role("button", name="Clear filters", exact=True).click()
    controls.get_by_role("searchbox", name="Search worker groups").fill("Idle group 1")
    expect(page.locator("#workerGroupSummary")).to_contain_text("7 groups · 1 occupied")
    expect(page.locator("#workerGroupSummary")).to_contain_text("Showing 1 of 1 matching groups")
    expect(outside).to_be_visible()
    indicator = page.locator('[data-group-id="group-1"] [data-group-indicator="configured"] summary')
    indicator.focus()
    page.keyboard.press("Enter")
    expect(
        page.locator('[data-group-id="group-1"]').get_by_text(
            "1 configured worker. Worker counts set simultaneous tasks, not CPU cores.", exact=True
        )
    ).to_be_visible()
    assert indicator.bounding_box()["height"] >= 44
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.e2e
def test_phone_files_and_server_results_remain_keyboard_accessible(dashboard: Callable[..., Page]) -> None:
    job = jobs_ui._preview_job()
    job["library_name"] = "Archive sample"
    job["publishers"].append(
        {"server_id": "jf-1", "server_name": "Home Jellyfin", "server_type": "jellyfin", "counts": {"published": 2}}
    )
    page = dashboard([job])
    page.set_viewport_size({"width": 390, "height": 844})
    page.route("**/api/jobs/*/logs**", lambda route: _fulfill_json(route, {"logs": [], "total_lines": 0}))
    page.route(
        "**/api/jobs/*/files**",
        lambda route: _fulfill_json(
            route,
            jobs_ui._files_response([{"file": "/media/Movies/film.mkv", "outcome": "generated", "worker": "GPU 1"}]),
        ),
    )
    page.locator(f'#job-row-{job["id"]} button[aria-label="View logs"]').click()
    modal = page.locator("#logsModal")
    expect(modal).to_be_visible()
    modal.get_by_role("tab", name="Files", exact=True).click()
    expect(modal.locator("#fileResultsBody")).to_contain_text("film.mkv")
    expect(modal.locator("#logsJobId")).to_be_hidden()
    expect(modal.locator("#logsModalPublishers")).to_be_hidden()
    expect(modal.get_by_role("tab", name="Files", exact=True)).to_be_in_viewport()
    first_file = modal.locator("#fileResultsBody tr").first
    expect(first_file).to_be_in_viewport()
    assert modal.locator(".modal-body").evaluate("el => el.scrollWidth <= el.clientWidth + 1")
    results = modal.locator("#jobServerResults summary")
    results.focus()
    page.keyboard.press("Enter")
    expect(modal.locator("#logsModalPublishers")).to_contain_text("Home Plex")
    expect(modal.locator("#logsModalPublishers")).to_be_visible()
    for server_name in ("Home Plex", "Home Jellyfin"):
        expect(modal.locator("#logsModalPublishers").get_by_text(server_name, exact=True)).to_be_visible()
    identity = modal.locator(".job-identity-disclosure summary")
    identity.focus()
    page.keyboard.press("Enter")
    expect(modal.locator("#logsJobId")).to_contain_text(job["id"])
    expect(modal.get_by_role("button", name="Copy Job ID", exact=True)).to_be_visible()
    modal.get_by_role("tab", name="Logs", exact=True).click()
    expect(modal.get_by_role("textbox", name="Filter logs", exact=True)).to_be_visible()


@pytest.mark.e2e
@pytest.mark.parametrize("theme", ["dark", "light"])
def test_queue_metadata_keeps_explicit_owners_and_readable_type(dashboard: Callable[..., Page], theme: str) -> None:
    scope = [
        {
            "server_id": "plex-1",
            "server_name": "Home Plex",
            "server_type": "plex",
            "library_id": "1",
            "library_name": "Movies",
        },
        {
            "server_id": "jf-1",
            "server_name": "Home Jellyfin",
            "server_type": "jellyfin",
            "library_id": "1",
            "library_name": "Movies",
        },
    ]
    job = _job(
        "shared-movie",
        library_name="Shared archive",
        library_names=["Movies"],
        library_scope=scope,
        server_id="plex-1",
        server_name="Home Plex",
        server_type="plex",
        config={"source": "sonarr"},
        publishers=[{"server_id": "other", "server_name": "Unrelated publisher", "server_type": "emby"}],
    )
    page = dashboard([job])
    page.evaluate("theme => document.documentElement.setAttribute('data-bs-theme', theme)", theme)
    metadata = page.locator("#job-row-shared-movie .queue-metadata")
    for text in ("Previews", "Movies", "Home Plex", "Home Jellyfin", "Sonarr"):
        expect(metadata).to_contain_text(text)
    expect(metadata).not_to_contain_text("Unrelated publisher")
    assert metadata.locator(".job-kind-badge").evaluate("el => parseFloat(getComputedStyle(el).fontSize)") >= 12
    page.evaluate("() => updateJobQueue(true)")
    expect(metadata).to_contain_text("Home Jellyfin")


@pytest.mark.e2e
def test_server_owner_disclosure_keeps_keyboard_focus_during_queue_refresh(dashboard: Callable[..., Page]) -> None:
    job = _job(
        "multi-owner",
        library_name="Shared archive",
        server_id="plex-1",
        server_name="Home Plex",
        server_type="plex",
        library_scope=[
            {
                "server_id": "jf-1",
                "server_name": "Home Jellyfin",
                "server_type": "jellyfin",
                "library_id": "1",
                "library_name": "Movies",
            },
            {
                "server_id": "emby-1",
                "server_name": "Home Emby",
                "server_type": "emby",
                "library_id": "1",
                "library_name": "Movies",
            },
        ],
    )
    page = dashboard([job])
    row = page.locator("#job-row-multi-owner")
    summary = row.get_by_label("Show associated library servers", exact=True)
    summary.focus()
    page.keyboard.press("Enter")
    expect(row.get_by_text("Home Jellyfin", exact=True)).to_be_visible()
    page.evaluate(
        "() => { jobs.find(job => job.id === 'multi-owner').library_name = 'Updated archive'; updateJobQueue(true); }"
    )
    expect(row).to_contain_text("Updated archive")
    expect(summary).to_be_focused()
    expect(row.get_by_text("Home Jellyfin", exact=True)).to_be_visible()
    expect(row.get_by_text("Home Emby", exact=True)).to_be_visible()
