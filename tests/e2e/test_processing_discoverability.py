"""Processing controls and readable job results remain complete at phone and desktop sizes."""

import os
from collections.abc import Callable
from pathlib import Path

import pytest
from playwright.sync_api import Locator, Page, expect

from . import test_intro_credits_jobs_ui as jobs_ui
from .test_intro_credits_jobs_ui import _preview_job
from .test_intro_credits_server_tab import (
    _mock_server_page,
    _open_tab,
    _plex_markers_on,
    _plex_ready_details,
    _plex_server,
    _save_and_read_put,
    _status,
    _switch_tab,
    _vendor_server,
)

dashboard = jobs_ui.dashboard


def _capture(locator: Locator, filename: str, tmp_path: Path) -> None:
    evidence = Path(os.environ.get("MPG_UX_EVIDENCE_DIR", str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    locator.screenshot(path=str(evidence / filename), animations="disabled")


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup: None) -> None:
    return complete_setup


def _fits(page: Page) -> None:
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert page.locator(".modal.show .modal-body").evaluate_all(
        "els => els.every(el => el.scrollWidth <= el.clientWidth + 1)"
    )


@pytest.mark.e2e
@pytest.mark.parametrize("width", [1440, 390])
@pytest.mark.parametrize("vendor", ["plex", "emby", "jellyfin"])
def test_processing_keeps_capabilities_and_saves_independent_choices(
    authed_page: Page, app_url: str, width: int, vendor: str, tmp_path: Path
) -> None:
    page = authed_page
    page.set_viewport_size({"width": width, "height": 1000 if width > 600 else 844})
    server = _plex_server(_plex_markers_on()) if vendor == "plex" else _vendor_server(vendor, f"{vendor}-1")
    server["name"] = f"Living room {vendor.title()} · Movies, television and family recordings"
    status = _status(server, "ready", details=_plex_ready_details() if vendor == "plex" else {})
    _mock_server_page(page, server, status)
    _open_tab(page, app_url, server, "processing")
    pane = page.locator("#edit-tab-processing")
    expect(pane.get_by_role("heading", name="Previews", exact=True)).to_be_visible()
    expect(pane.get_by_role("heading", name="Intro & Credits", exact=True)).to_be_visible()
    expect(pane.locator('a[href="/settings#section-markers"]')).to_be_visible()
    assert page.locator('#editServerModal [data-bs-target="#edit-tab-markers"]').count() == 0
    assert page.locator('#editServerModal [data-bs-target="#edit-tab-loudness"]').count() == 0
    if vendor == "plex":
        expect(page.locator("#processingChapters")).to_be_visible()
        expect(page.locator("#edit-tab-loudness")).to_be_visible()
        page.locator("#editPlexChapterThumbnails").check()
        page.locator("#loudnessEnabled").check()
        pane.locator('a[data-section="general"]').click()
        expect(page.locator("#markersPlexAgentGroup")).to_be_visible()
        expect(page.locator("#edit-tab-general #editPlexChapterThumbnails")).to_have_count(0)
        _switch_tab(page, "processing")
        pane.locator('[href="#processingLoudnessTitle"]').click()
        expect(page.locator("#processingLoudnessTitle")).to_be_focused()
        expect(page.locator("#loudnessEnabled")).to_be_in_viewport()
        page.locator("#editServerModal .modal-body").evaluate("el => el.scrollTop = 0")
        _fits(page)
        _capture(page.locator("#editServerModal .modal-content"), f"server-processing-{width}.png", tmp_path)
    else:
        expect(page.locator("#processingChapters")).to_be_hidden()
        expect(page.locator("#edit-tab-loudness")).to_be_hidden()
        _fits(page)
    pane.locator('a[data-section="libraries"]').first.click()
    expect(page.locator("#edit-tab-libraries")).to_be_visible()
    if vendor == "plex":
        expect(page.locator("#editLibraryTable th.loudness-lib-col")).to_be_visible()
    payload = _save_and_read_put(page, server["id"])
    if vendor == "plex":
        assert payload["output"]["chapter_thumbnails"] is True
        assert payload["loudness"] == {"enabled": True, "library_ids": None}
        assert payload["markers"]["enabled"] is True


@pytest.mark.e2e
@pytest.mark.parametrize("width", [1440, 390])
def test_expanded_results_keep_counts_issues_and_current_activity(
    dashboard: Callable[..., Page], width: int, tmp_path: Path
) -> None:
    job = _preview_job()
    job["status"] = "running"
    job["progress"].update(
        current_item="/media/television/" + "An unusually long programme title / " * 5 + "episode.mkv",
        cpu_fallback_files=2,
        outcome={"skipped_file_not_found": 3},
    )
    job["publishers"][0].update(
        counts={"published": 4, "failed": 1},
        frame_sources={"extracted": 4, "cache_hit": 2, "output_existed": 12},
        chapter_counts={"updated": 4, "waiting": 2, "failed": 1, "already_existed": 8},
    )
    job["publishers"].append(
        {"server_id": "jf-1", "server_name": "Home Jellyfin", "server_type": "jellyfin", "counts": {"failed": 1}}
    )
    page = dashboard([job])
    page.set_viewport_size({"width": width, "height": 1000 if width > 600 else 844})
    button = page.locator(f"#job-files-toggle-{job['id']}")
    expect(button).to_have_text("Job details")
    assert button.bounding_box()["height"] >= 44
    button.click()
    expect(button).to_have_attribute("aria-expanded", "true")
    details = page.locator(f"#job-detail-{job['id']}")
    for text in [
        "Results recorded so far",
        "Current activity",
        "Home Plex",
        "Home Jellyfin",
        "Chapters updated × 4",
        "Chapters waiting for Plex × 2",
        "Chapters failed × 1",
        "File issues",
        "2 files ran on the CPU",
    ]:
        expect(details).to_contain_text(text)
    expect(details.locator(".job-result-item")).to_have_count(10)
    expect(details.get_by_role("button", name="Open logs and files")).to_be_visible()
    _fits(page)
    _capture(details.locator("xpath=ancestor::table"), f"job-results-{width}.png", tmp_path)
    button.click()
    expect(details).to_be_hidden()
    expect(button).to_have_attribute("aria-expanded", "false")


@pytest.mark.e2e
def test_job_with_only_file_issues_does_not_lose_its_results(dashboard: Callable[..., Page]) -> None:
    job = _preview_job()
    job["publishers"] = []
    job["progress"]["outcome"] = {"skipped_file_not_found": 2}
    page = dashboard([job])
    page.locator(f"#job-files-toggle-{job['id']}").click()
    details = page.locator(f"#job-detail-{job['id']}")
    expect(details).to_contain_text("File issues")
    expect(details.locator(".job-result-item strong")).to_have_text("× 2")
