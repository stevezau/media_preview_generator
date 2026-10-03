"""E2E: the Inspector — search, a checked episode, Back to automatic, and Adjust.

The Inspector's API is mocked by ``_inspector_fixtures.install`` in the real routes' shapes; each test pins what one
screen shows and, for the writes, exactly what the page sends (save = lock + publish, unlock, re-detect).
The Timeline strip, the big frame and the not-checked page are in ``test_inspector_timeline.py``.
``INSPECTOR_SCREENSHOT_DIR`` saves a screenshot of each screen for checking against the design.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, quote, urlparse

import pytest
from playwright.sync_api import Page, expect

from . import _inspector_fixtures as fx


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


def _open(page: Page, app_url: str, path: str) -> None:
    page.goto(f"{app_url}/inspector?path={quote(path)}")
    expect(page.locator("#inspLoading")).to_have_count(0, timeout=10_000)


def _expect_path_param(page: Page, app_url: str, path: str) -> None:
    """The page's address is ``/inspector?path=<path>`` (however the browser chose to encode it)."""
    expect(page).to_have_url(re.compile(re.escape(f"{app_url}/inspector?path=")))
    query = parse_qs(urlparse(page.url).query)
    assert query == {"path": [path]}


@pytest.mark.e2e
class TestSearch:
    def test_typing_a_title_lists_films_with_preview_and_intro_credits_status(
        self, authed_page: Page, app_url: str
    ) -> None:
        fx.install(authed_page)
        authed_page.goto(f"{app_url}/inspector")
        expect(authed_page.locator("#inspScope option")).to_have_text(["All servers", "Plex", "Jellyfin"])
        authed_page.locator("#inspQuery").fill("matrix")

        rows = authed_page.locator("#inspResults button.insp-row")
        expect(rows).to_have_count(3)
        first = rows.nth(0)
        expect(first.locator(".insp-row-title")).to_have_text("The Matrix (1999)")
        expect(first.locator(".insp-row-meta")).to_have_text("Film · 2160p Dolby Vision · Plex")
        expect(first.locator(".insp-cell-preview")).to_have_text("Ready · 4,089 frames")
        expect(first.locator(".insp-cell-markers")).to_have_text("Not checked yet")
        expect(rows.nth(1).locator(".insp-cell-markers")).to_have_text("Credits set")
        expect(rows.nth(2).locator(".insp-cell-preview")).to_have_text("Missing")
        expect(rows.nth(2).locator(".insp-cell-markers")).to_have_text("Nothing found")
        # Rows name the title, never the file's path.
        expect(authed_page.locator("#inspResults")).not_to_contain_text("/data/")
        fx.screenshot(authed_page, "13-search")

    def test_choosing_a_result_opens_the_inspector_and_folds_the_results_away(
        self, authed_page: Page, app_url: str
    ) -> None:
        fx.install(authed_page)
        authed_page.goto(f"{app_url}/inspector")
        authed_page.locator("#inspQuery").fill("matrix")
        authed_page.locator("#inspResults button.insp-row").first.click()

        _expect_path_param(authed_page, app_url, fx.FILM)
        expect(authed_page.locator("#inspSearch")).to_be_hidden()
        expect(authed_page.locator("#inspTitle")).to_have_text("The Matrix")
        expect(authed_page.locator("#inspTitleSub")).to_have_text("1999")
        expect(authed_page.locator("#inspPath")).to_have_text(fx.FILM)
        expect(authed_page.locator("#inspShowResultsText")).to_have_text("Results for “matrix”")

        authed_page.locator("#inspShowResults").click()
        expect(authed_page.locator("#inspSearch")).to_be_visible()
        expect(authed_page.locator("#inspResults button.insp-row")).to_have_count(3)

    def test_a_show_picks_the_season_then_the_episode_in_place(self, authed_page: Page, app_url: str) -> None:
        fx.install(authed_page)
        authed_page.goto(f"{app_url}/inspector")
        authed_page.locator("#inspQuery").fill("blood")
        show = authed_page.locator("#inspResults button.insp-row").first
        expect(show.locator(".insp-row-meta")).to_have_text("TV show · 18 episodes · Plex, Jellyfin")
        show.click()

        panel = authed_page.locator(".insp-show")
        expect(panel.locator(".insp-seasons button")).to_have_text(["Season 1", "Season 2"])
        expect(panel.locator(".insp-seasons button[aria-pressed='true']")).to_have_text("Season 1")
        episodes = panel.locator(".insp-episode")
        expect(episodes).to_have_count(10)
        expect(episodes.nth(0)).to_contain_text("E01")
        expect(episodes.nth(0)).to_contain_text("Intro + credits")
        expect(episodes.nth(6)).to_contain_text("Not checked yet")
        expect(episodes.nth(9)).to_contain_text("Nothing found")
        fx.screenshot(authed_page, "13b-search-show-picker")

        panel.locator(".insp-seasons button", has_text="Season 2").click()
        expect(panel.locator(".insp-episode")).to_have_count(8)
        panel.locator(".insp-seasons button", has_text="Season 1").click()
        panel.locator(".insp-episode").first.click()
        expect(authed_page.locator("#inspTitle")).to_have_text("Blood Legacy")
        expect(authed_page.locator("#inspTitleSub")).to_have_text("2024 · S01E01")
        _expect_path_param(authed_page, app_url, fx.EPISODE)

    def test_a_path_starting_with_a_slash_opens_that_file(self, authed_page: Page, app_url: str) -> None:
        fx.install(authed_page)
        authed_page.goto(f"{app_url}/inspector")
        authed_page.locator("#inspQuery").fill(fx.EPISODE)
        expect(authed_page.locator("#inspResults")).to_contain_text("Open this file")
        authed_page.locator("#inspQuery").press("Enter")
        expect(authed_page.locator("#inspTitle")).to_have_text("Blood Legacy")
        expect(authed_page.locator("#inspShowResultsText")).to_have_text("New search")

    def test_the_old_address_redirects_with_its_file_link(self, authed_page: Page, app_url: str) -> None:
        fx.install(authed_page)
        authed_page.goto(f"{app_url}/bif-viewer?file={quote(fx.EPISODE)}&tab=markers")
        _expect_path_param(authed_page, app_url, fx.EPISODE)
        expect(authed_page.locator("#inspTitle")).to_have_text("Blood Legacy")


@pytest.mark.e2e
class TestCheckedEpisode:
    def test_tiles_and_rows(self, authed_page: Page, app_url: str) -> None:
        fx.install(authed_page)
        _open(authed_page, app_url, fx.EPISODE)

        tiles = authed_page.locator("#inspTiles")
        expect(tiles.locator("[data-tile='servers'] .insp-stat-title")).to_have_text("1 needs attention")
        expect(tiles.locator("[data-tile='servers'] .insp-stat-sub")).to_have_text(
            "Plex has both. Jellyfin gets them on the next job."
        )
        expect(tiles.locator("[data-tile='found'] .insp-stat-title > div > div")).to_have_text(
            ["Intro 2:45 – 2:59", "Credits 24:59 → end"]
        )
        expect(tiles.locator("[data-tile='found'] .insp-stat-sub")).to_have_text(
            "From Season audio, Chapters and Credit text"
        )
        expect(tiles.locator("[data-tile='preview'] .insp-stat-title")).to_have_text("828 frames · every 2 s")
        expect(tiles.locator("[data-tile='preview'] .insp-stat-sub")).to_contain_text("16.6 MB · covers 27:36")
        expect(tiles.locator("[data-tile='checked'] .insp-stat-sub")).to_have_text("Intro found · credits found")
        for label in ("Regenerate preview", "Re-detect intro & credits", "Adjust"):
            expect(authed_page.get_by_role("button", name=label, exact=True)).to_be_visible()

        # The strip's images are real frames from the preview.
        img = authed_page.locator("#inspStrip .insp-tl-frame.is-now img")
        expect(img).to_have_attribute("src", re.compile(r"^/api/bif/frame\?path="))
        assert quote(fx.PLEX_BIF, safe="") in (img.get_attribute("src") or "")
        found = authed_page.locator("#inspStrip .insp-lane[data-lane='found']")
        expect(found.locator(".insp-band")).to_have_text(["Intro 2:45 – 2:59", "Credits 24:59 → end"])
        plex = authed_page.locator("#inspStrip .insp-lane[data-server-id='plex-1']")
        expect(plex.locator(".insp-band.is-tint")).to_have_text(
            ["Ours · Intro 2:45 – 2:59", "Ours · Credits 24:59 → end"]
        )
        jf = authed_page.locator("#inspStrip .insp-lane[data-server-id='jf-1']")
        expect(jf).to_have_text("Nothing yet · the next job adds intro 2:45 – 2:59 and credits 24:59 → end")

    def test_evidence_and_servers(self, authed_page: Page, app_url: str) -> None:
        fx.install(authed_page)
        _open(authed_page, app_url, fx.EPISODE)

        evidence = authed_page.locator("#inspEvidence")
        expect(evidence.locator(".insp-ev[data-source='season_audio']")).to_contain_text(
            "Same theme in 20 of 20 episodes"
        )
        expect(evidence.locator(".insp-ev[data-source='season_audio']")).to_contain_text("Used for the intro")
        expect(evidence.locator(".insp-ev[data-source='chapters']")).to_contain_text("“Credits” chapter at 24:59")
        expect(evidence.locator(".insp-ev[data-source='introdb']")).to_contain_text("No entry")
        expect(evidence.locator(".insp-ev[data-source='server_markers']")).to_contain_text(
            "Made for an earlier version of this file, so not used"
        )

        plex = authed_page.locator("#inspServers .insp-server[data-server-id='plex-1']")
        expect(plex.locator(".insp-server-shows")).to_have_text("Ours · intro 2:45 – 2:59 and credits 24:59 → end")
        expect(plex.locator(".insp-server-plan")).to_have_text("Intro & credits up to date · sent by this app")
        expect(plex.locator(".insp-server-preview")).to_have_text("Preview in place · 828 frames")
        jf = authed_page.locator("#inspServers .insp-server[data-server-id='jf-1']")
        expect(jf.locator(".insp-server-plan")).to_have_text("Intro & credits: adds them on the next job")
        expect(jf.locator(".insp-server-preview")).to_have_text("Preview in place · trickplay tiles · 166 frames")
        authed_page.locator("#inspServers .insp-locations summary").click()
        expect(authed_page.locator("#inspServers .insp-locations div[data-server-id='plex-1']")).to_have_text(
            f"Plex · Preview: {fx.PLEX_BIF}"
        )
        fx.screenshot(authed_page, "15-checked-episode")


@pytest.mark.e2e
class TestBackToAutomatic:
    def test_back_to_automatic_unlocks_the_types_you_set(self, authed_page: Page, app_url: str) -> None:
        api = fx.InspectorApi()
        file, item = fx.checked_episode()
        item["decisions"]["credits"]["marker"]["locked"] = True
        item["decisions"]["credits"]["marker"]["decided_by"] = ["user"]
        api.add(file, item, fx.default_kinds(fx.EPISODE))
        fx.install(authed_page, api)
        _open(authed_page, app_url, fx.EPISODE)

        locked = authed_page.locator("#inspLocked")
        expect(locked.locator(".insp-ev-found")).to_have_text("Credits 24:59 → end")
        expect(locked.locator(".insp-ev-note")).to_have_text("Locked · later checks keep them")
        authed_page.locator("#inspUnlock").click()
        with authed_page.expect_response(
            lambda r: r.url.endswith("/api/markers/item/markers") and r.request.method == "DELETE"
        ):
            authed_page.locator("#inspUnlockConfirm").click()
        assert api.unlocks == [{"path": fx.EPISODE, "types": ["credits"]}]


@pytest.mark.e2e
class TestAdjust:
    def test_nudging_an_edge_reads_exact_frames_and_saves_the_new_time(self, authed_page: Page, app_url: str) -> None:
        api = fx.install(authed_page)
        _open(authed_page, app_url, fx.EPISODE)
        authed_page.get_by_role("button", name="Adjust", exact=True).click()

        panel = authed_page.locator("#inspTimeline #inspAdjustPanel")
        expect(panel.locator(".insp-adjust-title")).to_have_text("Adjust intro and credits")
        intro_start = panel.locator("[data-edge='intro-start']")
        expect(intro_start.locator("input")).to_have_value("2:45")
        expect(intro_start.locator(".insp-frame img")).to_have_count(8)
        expect(intro_start.locator(".insp-adj-flag")).to_have_text("Intro start · 2:45")
        assert {"path": fx.EPISODE, "start_ms": 162_000, "count": 8} in api.frame_requests
        fx.screenshot(authed_page, "04b-adjust-episode")

        intro_start.get_by_role("button", name="Move the intro start one second later").click()
        expect(panel.locator("[data-edge='intro-start'] input")).to_have_value("2:46")
        panel.locator("[data-edge='intro-end']").get_by_role(
            "button", name="Frame at 3:01: set the intro end here"
        ).click()
        expect(panel.locator("[data-edge='intro-end'] input")).to_have_value("3:01")

        expect(panel.locator("[data-adjust='intro'] .insp-adjust-range")).to_have_text("2:46 → 3:01")
        expect(panel.locator("[data-adjust='credits'] .insp-adjust-range")).to_have_text("24:59 → end of file")
        with authed_page.expect_response(
            lambda r: r.url.endswith("/api/markers/item/markers") and r.request.method == "POST"
        ):
            panel.get_by_role("button", name="Save and send to Plex and Jellyfin").click()
        assert api.saves == [
            {
                "path": fx.EPISODE,
                "markers": [
                    {"type": "intro", "start_ms": 166_000, "end_ms": 181_000},
                    {"type": "credits", "start_ms": 1_499_000, "end_ms": None},
                ],
            }
        ]

    def test_adding_an_intro_that_wasnt_found_starts_from_round_times(self, authed_page: Page, app_url: str) -> None:
        file, item = fx.checked_episode()
        item["decisions"]["intro"] = {
            "status": "no_evidence",
            "reason": "",
            "marker": None,
            "proposed": None,
            "shortened_by": None,
        }
        api = fx.InspectorApi()
        api.add(file, item, fx.default_kinds(fx.EPISODE))
        fx.install(authed_page, api)
        _open(authed_page, app_url, fx.EPISODE)
        authed_page.get_by_role("button", name="Adjust", exact=True).click()

        expect(authed_page.locator("[data-add='intro']")).to_contain_text("Intro · not found")
        authed_page.locator("#inspAdd-intro").click()
        expect(authed_page.locator("[data-edge='intro-start'] input")).to_have_value("0:00")
        expect(authed_page.locator("[data-edge='intro-end'] input")).to_have_value("0:30")
        field = authed_page.locator("[data-edge='intro-end'] input")
        field.fill("0:41")
        field.press("Enter")
        expect(authed_page.locator("[data-edge='intro-end'] input")).to_have_value("0:41")
        expect(authed_page.locator("[data-adjust='intro'] .insp-adjust-range")).to_have_text("0:00 → 0:41")
        with authed_page.expect_response(
            lambda r: r.url.endswith("/api/markers/item/markers") and r.request.method == "POST"
        ):
            authed_page.locator("#inspAdjustSave").click()
        assert api.saves == [
            {
                "path": fx.EPISODE,
                "markers": [
                    {"type": "intro", "start_ms": 0, "end_ms": 41_000},
                    {"type": "credits", "start_ms": 1_499_000, "end_ms": None},
                ],
            }
        ]

    def test_a_type_no_owner_can_show_is_left_out_of_the_save(self, authed_page: Page, app_url: str) -> None:
        """A decided recap with only Plex taking markers: sending it would make the save route refuse the whole save."""
        file, item = fx.checked_episode()
        item["decisions"]["recap"] = {
            "status": "decided",
            "reason": "",
            "marker": {"type": "recap", "start_ms": 0, "end_ms": 60_000, "decided_by": ["chapters"], "locked": False},
            "proposed": None,
            "shortened_by": None,
        }
        item["servers"][1]["markers_enabled"] = False
        api = fx.InspectorApi()
        api.add(file, item, fx.default_kinds(fx.EPISODE))
        fx.install(authed_page, api)
        _open(authed_page, app_url, fx.EPISODE)
        authed_page.get_by_role("button", name="Adjust", exact=True).click()

        expect(authed_page.locator("[data-cant-adjust='recap']")).to_contain_text("No server with Intro & Credits on")
        with authed_page.expect_response(
            lambda r: r.url.endswith("/api/markers/item/markers") and r.request.method == "POST"
        ):
            authed_page.locator("#inspAdjustSave").click()
        assert [m["type"] for m in api.saves[0]["markers"]] == ["intro", "credits"]

    def test_cancel_leaves_everything_as_it_was(self, authed_page: Page, app_url: str) -> None:
        api = fx.install(authed_page)
        _open(authed_page, app_url, fx.EPISODE)
        authed_page.get_by_role("button", name="Adjust", exact=True).click()
        authed_page.locator("[data-edge='intro-start']").get_by_role(
            "button", name="Move the intro start one second later"
        ).click()
        authed_page.locator("#inspAdjustCancel").click()
        expect(authed_page.locator("#inspAdjustPanel")).to_have_count(0)
        expect(authed_page.locator("#inspAdjust")).to_have_text("Adjust")
        expect(authed_page.locator("#inspTiles [data-tile='found'] [data-type='intro']")).to_have_text(
            "Intro 2:45 – 2:59"
        )
        assert api.saves == []
