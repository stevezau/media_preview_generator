"""E2E: the Inspector — search, a checked episode, an unchecked film, Needs your check, Adjust and All frames.

The Inspector's API is mocked by ``_inspector_fixtures.install`` in the real routes' shapes; each test pins what one
screen shows and, for the writes, exactly what the page sends (save = lock + publish, unlock, re-detect).
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


def _boxes_overlap(a: dict, b: dict) -> bool:
    return not (
        a["x"] + a["width"] <= b["x"]
        or b["x"] + b["width"] <= a["x"]
        or a["y"] + a["height"] <= b["y"]
        or b["y"] + b["height"] <= a["y"]
    )


def _assert_labels_never_overlap(page: Page) -> None:
    lanes = page.locator("#inspFile [data-layout='1']")
    for i in range(lanes.count()):
        labels = lanes.nth(i).locator(":scope > .insp-lane-label")
        boxes = [labels.nth(j).bounding_box() for j in range(labels.count())]
        boxes = [b for b in boxes if b and b["width"] > 0]
        for x in range(len(boxes)):
            for y in range(x + 1, len(boxes)):
                assert not _boxes_overlap(boxes[x], boxes[y]), f"lane {i}: labels {x} and {y} overlap"


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
        expect(rows.nth(2).locator(".insp-cell-markers")).to_have_text("Needs review")
        # Rows name the title, never the file's path.
        expect(authed_page.locator("#inspResults")).not_to_contain_text("/data/")
        fx.screenshot(authed_page, "01-search")

    def test_choosing_a_result_opens_the_inspector_and_folds_the_results_away(
        self, authed_page: Page, app_url: str
    ) -> None:
        fx.install(authed_page)
        authed_page.goto(f"{app_url}/inspector")
        authed_page.locator("#inspQuery").fill("matrix")
        authed_page.locator("#inspResults button.insp-row").first.click()

        _expect_path_param(authed_page, app_url, fx.FILM)
        expect(authed_page.locator("#inspSearch")).to_be_hidden()
        expect(authed_page.locator("#inspTitle")).to_have_text("The Matrix (1999)")
        expect(authed_page.locator("#inspPath")).to_have_text(fx.FILM)
        expect(authed_page.locator("#inspShowResultsText")).to_have_text("Back to the results for “matrix”")

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
        episodes = panel.locator(".insp-episode")
        expect(episodes).to_have_count(10)
        expect(episodes.nth(0)).to_contain_text("E01")
        expect(episodes.nth(0)).to_contain_text("Intro + credits")
        expect(episodes.nth(6)).to_contain_text("Not checked yet")
        expect(episodes.nth(9)).to_contain_text("Needs review")
        fx.screenshot(authed_page, "02-search-show-picker")

        panel.locator(".insp-seasons button", has_text="Season 2").click()
        expect(panel.locator(".insp-episode")).to_have_count(8)
        panel.locator(".insp-seasons button", has_text="Season 1").click()
        panel.locator(".insp-episode").first.click()
        expect(authed_page.locator("#inspTitle")).to_have_text("Blood Legacy (2024) · S01E01")
        _expect_path_param(authed_page, app_url, fx.EPISODE)

    def test_a_path_starting_with_a_slash_opens_that_file(self, authed_page: Page, app_url: str) -> None:
        fx.install(authed_page)
        authed_page.goto(f"{app_url}/inspector")
        authed_page.locator("#inspQuery").fill(fx.EPISODE)
        expect(authed_page.locator("#inspResults")).to_contain_text("Open this file")
        authed_page.locator("#inspQuery").press("Enter")
        expect(authed_page.locator("#inspTitle")).to_have_text("Blood Legacy (2024) · S01E01")

    def test_the_old_address_redirects_with_its_file_link(self, authed_page: Page, app_url: str) -> None:
        fx.install(authed_page)
        authed_page.goto(f"{app_url}/bif-viewer?file={quote(fx.EPISODE)}&tab=markers")
        _expect_path_param(authed_page, app_url, fx.EPISODE)
        expect(authed_page.locator("#inspTitle")).to_have_text("Blood Legacy (2024) · S01E01")


@pytest.mark.e2e
class TestCheckedEpisode:
    def test_summary_facts_timeline_and_lanes(self, authed_page: Page, app_url: str) -> None:
        fx.install(authed_page)
        _open(authed_page, app_url, fx.EPISODE)

        expect(authed_page.locator("#inspSummary")).to_have_text(
            "Skip Intro runs 2:45 – 2:59 and Skip Credits starts at 24:59. Plex has both. Jellyfin gets them on the next job."
        )
        facts = authed_page.locator("#inspFacts")
        expect(facts).to_contain_text("828 frames · one every 2 s · covers 27:36")
        expect(facts).to_contain_text("16.6 MB")
        expect(facts).to_contain_text("Intro & Credits checked")
        for label in ("Regenerate preview", "Re-detect intro & credits", "Adjust"):
            expect(authed_page.get_by_role("button", name=label, exact=True)).to_be_visible()

        strip = authed_page.locator("#inspFilmstrip img")
        assert strip.count() >= 8
        expect(authed_page.locator("#inspFilmstrip .insp-band-intro")).to_have_count(1)
        expect(authed_page.locator("#inspFilmstrip .insp-band-credits")).to_have_count(1)
        # The strip's images are real frames from the preview.
        src = strip.first.get_attribute("src") or ""
        assert src.startswith("/api/bif/frame?path=") and quote(fx.PLEX_BIF, safe="") in src

        names = authed_page.locator("#inspWholeFile .insp-lane-name")
        expect(names.filter(has_text="Decided")).to_have_count(1)
        plex_lane = authed_page.locator("#inspWholeFile .insp-lane[data-server-id='plex-1']")
        expect(plex_lane).to_contain_text("Same as decided")
        jf_lane = authed_page.locator("#inspWholeFile .insp-lane[data-server-id='jf-1']")
        expect(jf_lane).to_have_text("Nothing yet · the next job adds intro and credits")
        _assert_labels_never_overlap(authed_page)

    def test_close_ups_evidence_and_servers(self, authed_page: Page, app_url: str) -> None:
        fx.install(authed_page)
        _open(authed_page, app_url, fx.EPISODE)

        intro = authed_page.locator("#inspCloseups [data-closeup='intro']")
        expect(intro.locator(".insp-closeup-title")).to_have_text("Intro · 2:45 – 2:59")
        expect(intro.locator(".insp-edge-label")).to_have_text(["Starts at 2:45", "Ends at 2:59"])
        first_edge = intro.locator(".insp-frames").first
        # A frame every 2 s: the first at or after the 2:45 edge is 2:46, the cut sits before it.
        expect(first_edge.locator(".insp-frame span")).to_have_text(["2:40", "2:42", "2:44", "2:46", "2:48", "2:50"])
        expect(first_edge.locator(".insp-cut")).to_have_count(1)
        credits = authed_page.locator("#inspCloseups [data-closeup='credits']")
        expect(credits.locator(".insp-closeup-title")).to_have_text("Credits · 24:59 → end (27:36)")
        expect(credits).to_contain_text("Runs to the end of the file")

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
        expect(plex).to_contain_text("Intro & credits up to date")
        expect(plex).to_contain_text("Preview in place · 828 frames")
        plex.locator("summary", has_text="File locations").click()
        expect(plex.locator("details div")).to_have_text(f"Preview: {fx.PLEX_BIF}")
        jf = authed_page.locator("#inspServers .insp-server[data-server-id='jf-1']")
        expect(jf).to_contain_text("Intro & credits: adds them on the next job")
        expect(jf).to_contain_text("Preview in place · trickplay tiles")
        fx.screenshot(authed_page, "03-checked-episode")


@pytest.mark.e2e
class TestUncheckedFilm:
    def test_server_markers_show_grey_with_a_zoomed_ending_and_a_close_up_each(
        self, authed_page: Page, app_url: str
    ) -> None:
        fx.install(authed_page)
        _open(authed_page, app_url, fx.FILM)

        card = authed_page.locator("#inspSummaryCard")
        expect(card).to_have_attribute("data-mode", "unchecked")
        expect(card).to_contain_text("Not checked by Intro & Credits yet")
        expect(card).to_contain_text("Plex shows 2 credits markers today, drawn in grey on the frames below")
        expect(card).to_contain_text("With “Keep Plex's” on, Plex's markers stay as they are.")
        expect(authed_page.get_by_role("button", name="Check intro & credits now")).to_be_visible()
        expect(authed_page.get_by_role("button", name="Adjust", exact=True)).to_have_count(0)

        expect(authed_page.locator("#inspFilmstrip .insp-band-server")).to_have_count(2)
        expect(authed_page.locator("#inspWholeFile .insp-lane[data-server-id='plex-1']")).to_contain_text(
            "2 credits markers near the end · zoomed below"
        )
        ending = authed_page.locator("#inspEnding")
        expect(ending.locator(".insp-card-title")).to_have_text("Ending, zoomed · 2:07:00 – 2:16:18 (end of file)")
        expect(ending.locator(".insp-lane-name")).to_contain_text(["Plex ①", "Plex ②"])
        expect(ending).to_contain_text("Credits 2:08:00–2:08:30 (30 s)")
        expect(ending).to_contain_text("Credits 2:09:28 → end")

        closeups = authed_page.locator("#inspCloseups [data-closeup='server']")
        expect(closeups).to_have_count(2)
        expect(closeups.nth(0).locator(".insp-closeup-title")).to_have_text("Plex ① starts 2:08:00")
        expect(closeups.nth(1).locator(".insp-closeup-title")).to_have_text("Plex ② starts 2:09:28")
        expect(closeups.nth(0)).to_contain_text("Plex's own marker.")
        _assert_labels_never_overlap(authed_page)
        fx.screenshot(authed_page, "04-unchecked-film")


@pytest.mark.e2e
class TestNeedsReview:
    def test_pick_a_candidate_and_save_locks_and_publishes_it(self, authed_page: Page, app_url: str) -> None:
        api = fx.install(authed_page)
        _open(authed_page, app_url, fx.REVIEW)

        panel = authed_page.locator("[data-review='credits']")
        expect(panel.locator(".insp-review-title")).to_have_text(
            "Where do the credits start? Two answers disagree by 28 seconds."
        )
        cards = panel.locator("[data-candidate]")
        expect(cards).to_have_count(2)
        expect(cards.nth(0)).to_contain_text("1:32:09")
        expect(cards.nth(0)).to_contain_text("From the file's “Credits” chapter")
        expect(cards.nth(1)).to_contain_text("From the credits read on screen · Plex's own marker agrees")
        # Seven exact frames a second apart, the answer's own ringed.
        expect(cards.nth(1).locator(".insp-frame")).to_have_count(7)
        expect(cards.nth(1).locator(".insp-frame.is-ringed span")).to_have_text("1:32:37")
        assert {"path": fx.REVIEW, "start_ms": 5_554_000, "count": 7} in api.frame_requests

        cards.nth(1).get_by_role("button", name="Credits start at 1:32:37").click()
        confirm = panel.locator("[data-confirm='credits']")
        expect(confirm).to_contain_text("Selected: credits start at 1:32:37")
        fx.screenshot(authed_page, "05-needs-review")

        with authed_page.expect_response(
            lambda r: r.url.endswith("/api/markers/item/markers") and r.request.method == "POST"
        ):
            confirm.get_by_role("button", name="Save and send to Plex").click()
        # Save is the lock-and-publish route: the chosen start, running to the end of the file.
        assert api.saves == [
            {"path": fx.REVIEW, "markers": [{"type": "credits", "start_ms": 5_557_000, "end_ms": None}]}
        ]

    def test_pick_the_frame_yourself_and_not_now(self, authed_page: Page, app_url: str) -> None:
        api = fx.install(authed_page)
        _open(authed_page, app_url, fx.REVIEW)
        pick = authed_page.locator("[data-pick-yourself='credits']")
        expect(pick.locator(".insp-frame")).to_have_count(14)
        expect(pick.locator(".insp-frame span").first).to_have_text("1:32:04")

        pick.get_by_role("button", name="Show 10 seconds later").click()
        expect(pick.locator(".insp-frame span").first).to_have_text("1:32:14")
        pick.get_by_role("button", name="Show 10 seconds earlier").click()
        pick.locator(".insp-frame", has_text="1:32:10").click()
        confirm = authed_page.locator("[data-confirm='credits']")
        expect(confirm).to_contain_text("Selected: credits start at 1:32:10")
        expect(pick.locator(".insp-frame.is-ringed span")).to_have_text("1:32:10")

        confirm.get_by_role("button", name="Not now").click()
        expect(authed_page.locator("[data-confirm='credits']")).to_have_count(0)
        pick.locator(".insp-frame", has_text="1:32:12").click()
        with authed_page.expect_response(
            lambda r: r.url.endswith("/api/markers/item/markers") and r.request.method == "POST"
        ):
            authed_page.locator("[data-confirm='credits']").get_by_role("button", name="Save and send to Plex").click()
        assert api.saves == [
            {"path": fx.REVIEW, "markers": [{"type": "credits", "start_ms": 5_532_000, "end_ms": None}]}
        ]

    def test_back_to_automatic_unlocks_the_types_you_set(self, authed_page: Page, app_url: str) -> None:
        api = fx.InspectorApi()
        file, item = fx.checked_episode()
        item["decisions"]["credits"]["marker"]["locked"] = True
        item["decisions"]["credits"]["marker"]["decided_by"] = ["user"]
        api.add(file, item, fx.default_kinds(fx.EPISODE))
        fx.install(authed_page, api)
        _open(authed_page, app_url, fx.EPISODE)

        expect(authed_page.locator("#inspLocked")).to_contain_text("You set the credits, so later checks keep them.")
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

        intro_start = authed_page.locator("[data-edge='intro-start']")
        expect(intro_start.locator(".insp-edge-label")).to_have_text("Starts at 2:45")
        expect(intro_start.locator(".insp-frame img")).to_have_count(7)
        assert {"path": fx.EPISODE, "start_ms": 162_000, "count": 7} in api.frame_requests
        fx.screenshot(authed_page, "06-adjust")

        intro_start.get_by_role("button", name="Move the intro start one second later").click()
        expect(authed_page.locator("[data-edge='intro-start'] .insp-edge-label")).to_have_text("Starts at 2:46")
        authed_page.locator("[data-edge='intro-end']").get_by_role(
            "button", name="Frame at 3:01: set the intro end here"
        ).click()
        expect(authed_page.locator("[data-edge='intro-end'] .insp-edge-label")).to_have_text("Ends at 3:01")

        bar = authed_page.locator("#inspAdjustBar")
        expect(bar).to_contain_text("Your times: intro 2:46–3:01 · credits 24:59 → end")
        with authed_page.expect_response(
            lambda r: r.url.endswith("/api/markers/item/markers") and r.request.method == "POST"
        ):
            bar.get_by_role("button", name="Save and send to Plex and Jellyfin").click()
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
        expect(authed_page.locator("[data-edge='intro-start'] .insp-edge-label")).to_have_text("Starts at 0:00")
        expect(authed_page.locator("[data-edge='intro-end'] .insp-edge-label")).to_have_text("Ends at 0:30")
        field = authed_page.locator("[data-edge='intro-end'] input")
        field.fill("0:41")
        field.press("Enter")
        expect(authed_page.locator("[data-edge='intro-end'] .insp-edge-label")).to_have_text("Ends at 0:41")
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
        authed_page.locator("#inspAdjustCancel").click()
        expect(authed_page.locator("#inspAdjustBar")).to_have_count(0)
        expect(authed_page.locator("[data-closeup='intro'] .insp-closeup-title")).to_have_text("Intro · 2:45 – 2:59")
        assert api.saves == []


@pytest.mark.e2e
class TestAllFrames:
    def test_toggle_shows_every_frame_with_intro_and_credits_edged(self, authed_page: Page, app_url: str) -> None:
        fx.install(authed_page)
        _open(authed_page, app_url, fx.EPISODE)
        authed_page.locator("#inspViewAll").click()

        grid = authed_page.locator("#inspAllFrames .insp-allframes button")
        expect(grid).to_have_count(828)
        # Intro 2:45-2:59 at a frame every 2 s: frames 83-89 (2:46 ... 2:58).
        expect(authed_page.locator("#inspAllFrames .insp-allframes button.is-intro")).to_have_count(7)
        expect(grid.nth(83)).to_have_class("is-intro")
        expect(grid.nth(750)).to_have_class("is-credits")
        grid.nth(100).click()
        expect(authed_page.locator("#inspAllFramesLabel")).to_have_text("Frame 100 of 827 · 3:20")
        assert "index=100" in (authed_page.locator("#inspAllFramesBig").get_attribute("src") or "")
        authed_page.keyboard.press("ArrowRight")
        expect(authed_page.locator("#inspAllFramesLabel")).to_have_text("Frame 101 of 827 · 3:22")
        fx.screenshot(authed_page, "07-all-frames")

        authed_page.locator("#inspAllStep").select_option("10")
        expect(grid).to_have_count(83)
        authed_page.locator("#inspViewTimeline").click()
        expect(authed_page.locator("#inspFilmstrip")).to_be_visible()
