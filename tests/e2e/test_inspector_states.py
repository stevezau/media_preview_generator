"""E2E: the Inspector's states — loading, no preview, not in any library, gone from disk, a server that can't be
reached, a job working on the file, and an item with several versions — plus its Regenerate and Re-detect actions."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, quote, urlparse

import pytest
from playwright.sync_api import Page, expect

from . import _inspector_fixtures as fx


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


def _api_with(file: dict, item: object) -> fx.InspectorApi:
    api = fx.InspectorApi()
    api.add(file, item, fx.default_kinds(file["canonical_path"]))
    return api


def _open(page: Page, app_url: str, path: str) -> None:
    page.goto(f"{app_url}/inspector?path={quote(path)}")
    expect(page.locator("#inspLoading")).to_have_count(0, timeout=10_000)


@pytest.mark.e2e
class TestStates:
    def test_loading_shows_until_the_file_answers(self, authed_page: Page, app_url: str) -> None:
        api = fx.install(authed_page)
        api.hold_files = True
        authed_page.goto(f"{app_url}/inspector?path={quote(fx.EPISODE)}")
        expect(authed_page.locator("#inspLoading")).to_have_text("Reading this file…")
        expect(authed_page.locator("#inspTitle")).to_have_text(fx.EPISODE.split("/")[-1])
        expect(authed_page.locator("#inspTiles")).to_have_count(0)
        fx.screenshot(authed_page, "16-state-loading")
        api.release()
        expect(authed_page.locator("#inspLoading")).to_have_count(0)
        expect(authed_page.locator("#inspTitle")).to_have_text("Blood Legacy")
        expect(authed_page.locator("#inspTitleSub")).to_have_text("2024 · S01E01")

    def test_no_preview_still_adjusts_on_frames_straight_from_the_video(self, authed_page: Page, app_url: str) -> None:
        file, item = fx.checked_episode()
        file["preview"] = None
        for row in file["previews"]:
            row.update(exists=False, frame_count=None)
        api = fx.install(authed_page, _api_with(file, item))
        _open(authed_page, app_url, fx.EPISODE)

        expect(authed_page.locator("#inspTiles [data-tile='preview'] .insp-stat-title")).to_have_text("No preview yet")
        expect(authed_page.locator("#inspStrip .insp-tl-none").first).to_have_text("No preview")
        expect(authed_page.locator("#inspFrameText")).to_have_text("no preview frame here")
        expect(authed_page.locator("#inspServers .insp-server[data-server-id='plex-1']")).to_contain_text(
            "No preview yet"
        )
        authed_page.locator("#inspAdjust").click()
        intro = authed_page.locator("[data-edge='intro-start']")
        expect(intro.locator(".insp-frame img")).to_have_count(8)
        assert {"path": fx.EPISODE, "start_ms": 162_000, "count": 8} in api.frame_requests

    def test_a_path_in_no_library(self, authed_page: Page, app_url: str) -> None:
        fx.install(authed_page)
        _open(authed_page, app_url, "/elsewhere/Home Video.mkv")
        card = authed_page.locator("[data-state='Not in any library']")
        expect(card).to_contain_text("No server has a library that holds this path")
        expect(authed_page.locator("#inspRegenerate")).to_have_count(0)
        expect(authed_page.locator("#inspTiles")).to_have_count(0)
        fx.screenshot(authed_page, "17-not-in-library")

    def test_gone_from_disk(self, authed_page: Page, app_url: str) -> None:
        file, _item = fx.checked_episode()
        file.update(exists=False, previews=[], preview=None)
        api = fx.install(authed_page, _api_with(file, (400, {"error": "Path is not a file inside any server library"})))
        _open(authed_page, app_url, fx.EPISODE)
        gone = authed_page.locator("#inspGone")
        expect(gone).to_contain_text("Gone from disk")
        expect(gone).to_contain_text("Intro & Credits still has what it found for it at this path.")
        fx.screenshot(authed_page, "18-gone")
        gone.get_by_role("button", name="Search for it").click()
        expect(authed_page.locator("#inspQuery")).to_have_value("Blood Legacy")
        assert api.saves == []

    def test_a_server_that_cant_be_reached(self, authed_page: Page, app_url: str) -> None:
        file, item = fx.checked_episode()
        file["previews"][1] = {**file["previews"][1], "exists": False, "path": "", "error": "Couldn't reach Jellyfin"}
        item["servers"][1].update(
            current=None,
            plan="unknown",
            capability_state="unknown",
            error="Couldn't read this server's Intro & Credits state (ConnectionError)",
        )
        fx.install(authed_page, _api_with(file, item))
        _open(authed_page, app_url, fx.EPISODE)

        expect(authed_page.locator("#inspTiles [data-tile='servers'] .insp-stat-sub")).to_have_text(
            "Plex has both. Jellyfin couldn't be read just now."
        )
        expect(authed_page.locator("#inspStrip .insp-lane[data-server-id='jf-1']")).to_have_text(
            "Couldn't read what it shows now"
        )
        jf = authed_page.locator("#inspServers .insp-server[data-server-id='jf-1']")
        expect(jf).to_contain_text("Couldn't read this server's Intro & Credits state (ConnectionError)")
        expect(jf).to_contain_text("Preview: Couldn't reach Jellyfin")
        fx.screenshot(authed_page, "19-unreachable")

    def test_a_job_working_on_the_file_shows_its_status(self, authed_page: Page, app_url: str) -> None:
        file, item = fx.checked_episode()
        file["job"] = {
            "id": "job-9",
            "kind": "intro_credits",
            "status": "running",
            "name": "Intro & Credits: 1 file",
            "percent": 42.0,
        }
        fx.install(authed_page, _api_with(file, item))
        _open(authed_page, app_url, fx.EPISODE)
        banner = authed_page.locator("#inspJobBanner")
        expect(banner.locator(".insp-banner-text")).to_have_text(
            "Working on this file: Intro & Credits job “Intro & Credits: 1 file” · 42%"
        )
        expect(banner.get_by_role("link", name="Open on the Dashboard")).to_have_attribute("href", "/?job=job-9")
        # The banner sits above the title, as in the design.
        top = banner.bounding_box()
        title = authed_page.locator("#inspTitle").bounding_box()
        assert top and title and top["y"] < title["y"]
        fx.screenshot(authed_page, "10-job-banner")

    def test_several_versions_switch_in_place(self, authed_page: Page, app_url: str) -> None:
        file, item = fx.unchecked_film()
        file["versions"] = [
            {"path": fx.FILM_1080, "label": "1080p", "current": False},
            {"path": fx.FILM, "label": "2160p Dolby Vision", "current": True},
        ]
        other = {**file, "canonical_path": fx.FILM_1080, "quality": "1080p"}
        other["versions"] = [{**v, "current": not v["current"]} for v in file["versions"]]
        api = _api_with(file, item)
        api.add(other, {**item, "canonical_path": fx.FILM_1080})
        fx.install(authed_page, api)
        _open(authed_page, app_url, fx.FILM)

        bar = authed_page.locator("#inspVersions")
        expect(bar.locator(".insp-versions-text")).to_have_text("This film has 2 files")
        expect(bar.locator("button[aria-pressed='true']")).to_have_text("2160p Dolby Vision · this one")
        expect(bar.locator(".info-icon")).to_have_attribute("aria-label", re.compile(r"^The server keeps these files"))
        fx.screenshot(authed_page, "09-versions")
        bar.get_by_role("button", name="1080p", exact=True).click()
        expect(authed_page.locator("#inspPath")).to_have_text(fx.FILM_1080)
        assert parse_qs(urlparse(authed_page.url).query) == {"path": [fx.FILM_1080]}
        expect(authed_page.locator("#inspVersions button[aria-pressed='true']")).to_have_text("1080p · this one")


@pytest.mark.e2e
class TestActions:
    def test_regenerate_queues_a_forced_preview_job_for_this_file(self, authed_page: Page, app_url: str) -> None:
        api = fx.install(authed_page)
        _open(authed_page, app_url, fx.EPISODE)
        with authed_page.expect_response(lambda r: r.url.endswith("/api/jobs/manual")):
            authed_page.locator("#inspRegenerate").click()
        assert api.manual_jobs == [{"file_paths": [fx.EPISODE], "force_regenerate": True, "priority": 1}]
        expect(authed_page.locator("#inspJobBanner")).to_contain_text("Queued for this file: Preview job")

    def test_redetect_queues_this_file_and_shows_it(self, authed_page: Page, app_url: str) -> None:
        api = fx.install(authed_page)
        _open(authed_page, app_url, fx.FILM)
        with authed_page.expect_response(lambda r: r.url.endswith("/api/markers/item/redetect")):
            authed_page.locator("#inspRedetect").click()
        assert api.redetects == [{"path": fx.FILM}]
        expect(authed_page.locator("#inspJobBanner")).to_contain_text("Queued for this file: Intro & Credits job")
