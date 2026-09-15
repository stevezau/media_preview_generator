"""E2E: Preview Inspector → Intro & Credits → "Whole season".

Search, BIF and ``GET /api/markers/item`` are mocked as in ``test_intro_credits_inspector.py``; ``GET
/api/markers/season`` returns the ``markers.inspect.season_payload`` shape and ``POST /api/markers/season/publish`` is
captured, so each test pins how one season state renders and what the page sends.
"""

from __future__ import annotations

import copy
import json
import re
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Page, Route, expect

from ._mocks import _fulfill_json
from .test_intro_credits_inspector import _DURATION, _MEDIA_FILE, _Inspector, _result, south_park

_FOLDER = "/data/tv/South Park (1997)/Season 01"
_SERVERS = [
    {"server_id": "plex-1", "server_name": "Plex", "server_type": "plex", "markers_enabled": True},
    {"server_id": "jf-1", "server_name": "Jellyfin", "server_type": "jellyfin", "markers_enabled": True},
    {"server_id": "emby-1", "server_name": "Emby", "server_type": "emby", "markers_enabled": False},
]


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


def _type(status, start=None, end=None, *, locked=False, proposed=None):
    marker = None
    if status == "decided":
        marker = {"type": "x", "start_ms": start, "end_ms": end, "decided_by": ["season_audio"], "locked": locked}
    return {"status": status, "reason": "", "marker": marker, "proposed": proposed}


def _dots(plex, jf, message=""):
    return {
        "plex-1": {"state": plex, "message": message},
        "jf-1": {"state": jf, "message": message},
        "emby-1": {"state": "off", "message": ""},
    }


def _episode(n, intro, credits, evidence, dots, *, known=True):
    return {
        "path": f"{_FOLDER}/South Park S01E{n:02d}.mkv",
        "name": f"South Park S01E{n:02d}.mkv",
        "episode": f"E{n:02d}",
        "known": known,
        "duration_ms": _DURATION if known else None,
        "intro": intro,
        "credits": credits,
        "evidence": evidence,
        "servers": dots,
    }


def season() -> dict:
    audio = {"source": "season_audio", "label": "10/10"}
    tidb = {"source": "theintrodb", "label": ""}
    return {
        "folder": _FOLDER,
        "show": "South Park (1997) {tvdb-75897}",
        "season": "Season 01",
        "servers": copy.deepcopy(_SERVERS),
        "episodes": [
            _episode(1, _type("decided", 127_000, 157_000), _type("decided", 1_295_000, _DURATION), [audio, tidb], _dots("ok", "ok", "2 marker(s)")),
            _episode(2, _type("decided", 1_000, 30_000, locked=True), _type("decided", 1_246_000, _DURATION), [audio, {"source": "user", "label": ""}], _dots("ok", "waiting", "Not in this server's library yet")),
            _episode(3, _type("decided", 2_000, 29_000), _type("needs_review", proposed={"start_ms": 1_230_000, "end_ms": _DURATION}), [audio], _dots("ok", "failed", "HTTP 500 from the plugin")),
            _episode(4, _type(None), _type(None), [], _dots("none", "none"), known=False),
        ],
        "counts": {"episodes": 4, "total_episodes": 4, "ready": 2, "needs_review": 1},
    }  # fmt: skip


class _Season(_Inspector):
    def __init__(self, page: Page, app_url: str, payload: dict, *, results=None, season_status: int = 200) -> None:
        super().__init__(page, app_url, south_park(), results=results)
        self.season_payload = payload
        self.season_status = season_status
        self.season_requests: list[str] = []
        self.publish_bodies: list[dict] = []
        page.route("**/api/markers/season?**", self._season)
        page.route("**/api/markers/season/publish", self._publish)

    def _season(self, route: Route) -> None:
        self.season_requests.append(route.request.url)
        _fulfill_json(route, self.season_payload, status=self.season_status)

    def _publish(self, route: Route) -> None:
        self.publish_bodies.append(route.request.post_data_json or {})
        _fulfill_json(route, {"job_id": "5a5a5a5a-1111-4222-8333-444455556666"}, status=202)

    def whole_season(self) -> Page:
        self.page.locator("label[for='markersViewSeason']").click()
        expect(self.page.locator("#markersSeasonBody")).not_to_contain_text("Loading", timeout=3000)
        return self.page


def _row(page: Page, episode: str):
    return page.locator(f'#markersSeasonBody tr[data-episode="{episode}"]')


@pytest.mark.e2e
class TestSeasonView:
    def test_toggle_shows_for_an_episode_and_loads_the_season_only_when_asked(
        self, authed_page: Page, app_url: str
    ) -> None:
        view = _Season(authed_page, app_url, season())
        view.open_result()
        page = view.open_tab()
        expect(page.locator("#markersViewToggle")).to_be_visible()
        expect(page.locator("#markersViewEpisode")).to_be_checked()
        assert view.season_requests == []

        view.whole_season()
        (url,) = view.season_requests
        assert parse_qs(urlparse(url).query)["path"] == [_MEDIA_FILE]
        expect(page.locator("#markersEpisodeView")).to_be_hidden()

    def test_toggle_is_hidden_for_a_movie(self, authed_page: Page, app_url: str) -> None:
        view = _Season(authed_page, app_url, season(), results=[{**_result(), "type": "movie"}])
        view.open_result()
        page = view.open_tab()
        expect(page.locator("#markersViewToggle")).to_be_hidden()

    def test_header_counts_rows_chips_and_dots(self, authed_page: Page, app_url: str) -> None:
        view = _Season(authed_page, app_url, season())
        view.open_result()
        view.open_tab()
        page = view.whole_season()
        body = page.locator("#markersSeasonBody")
        expect(body.locator(".mk-season-title")).to_have_text("South Park (1997) · Season 01")
        expect(body.locator(".mk-season-sub")).to_have_text("4 episodes")
        expect(body.locator(".mk-season-ready")).to_have_text("2 ready")
        expect(body.locator(".mk-season-review")).to_have_text("1 need review")
        expect(body.locator("#markersSeasonPublishBtn")).to_have_text("Publish 2 to 2 servers")
        expect(body.locator("thead th.mk-season-servers")).to_have_text("Plex · Jellyfin · Emby")

        expect(_row(page, "E01").locator("td").nth(1)).to_have_text("2:07 – 2:37")
        expect(_row(page, "E01").locator("td").nth(2)).to_have_text("21:35 →")
        expect(_row(page, "E01").locator(".mk-chip")).to_have_text(["Audio 10/10", "TheIntroDB"])
        expect(_row(page, "E01").locator(".mk-season-action")).to_have_text("Published")
        expect(_row(page, "E02").locator(".mk-chip")).to_have_text(["Audio 10/10", "Your marker", "🔒 Locked by you"])
        expect(_row(page, "E02").locator(".mk-season-action")).to_have_text("")
        expect(_row(page, "E03").locator("td").nth(2)).to_have_text("Needs review")
        expect(_row(page, "E03").locator(".mk-season-action button")).to_have_text("Review")
        expect(_row(page, "E04").locator("td").nth(1)).to_have_text("Not checked yet")

        dots = _row(page, "E02").locator(".mk-dot")
        expect(dots).to_have_count(3)
        expect(dots.nth(0)).to_have_class(re.compile(r"\bmk-dot-ok\b"))
        expect(dots.nth(1)).to_have_class(re.compile(r"\bmk-dot-waiting\b"))
        expect(dots.nth(1)).to_have_attribute("title", "Jellyfin: Not in this server's library yet")
        expect(dots.nth(2)).to_have_class(re.compile(r"\bmk-dot-off\b"))
        expect(_row(page, "E03").locator(".mk-dot").nth(1)).to_have_class(re.compile(r"\bmk-dot-failed\b"))
        expect(body.locator(".mk-season-legend")).to_have_text(
            "Dots: green = server shows this marker, amber = waiting, red = failed, grey = server not enabled or nothing sent yet"
        )

    @pytest.mark.parametrize(
        ("total", "text"),
        [(40, "40 episodes"), (60, "60 episodes (showing the 40 nearest)")],
    )
    def test_a_capped_season_says_how_many_it_has(self, authed_page: Page, app_url: str, total: int, text: str) -> None:
        payload = season()
        payload["episodes"] = [
            _episode(n, _type(None), _type(None), [], _dots("none", "none"), known=False) for n in range(1, 41)
        ]
        payload["counts"] = {"episodes": 40, "total_episodes": total, "ready": 0, "needs_review": 0}
        view = _Season(authed_page, app_url, payload)
        view.open_result()
        view.open_tab()
        page = view.whole_season()
        expect(page.locator("#markersSeasonBody .mk-season-sub")).to_have_text(text)

    def test_publish_sends_the_episode_path_and_links_the_job(self, authed_page: Page, app_url: str) -> None:
        view = _Season(authed_page, app_url, season())
        view.open_result()
        view.open_tab()
        page = view.whole_season()
        page.locator("#markersSeasonPublishBtn").click()
        expect(page.locator("#toastBody")).to_contain_text("5a5a5a5a", timeout=3000)
        assert view.publish_bodies == [{"path": _MEDIA_FILE}]

    def test_publish_is_disabled_with_nothing_ready_or_no_server_on(self, authed_page: Page, app_url: str) -> None:
        payload = season()
        payload["counts"]["ready"] = 0
        view = _Season(authed_page, app_url, payload)
        view.open_result()
        view.open_tab()
        page = view.whole_season()
        expect(page.locator("#markersSeasonPublishBtn")).to_be_disabled()
        expect(page.locator("#markersSeasonPublishBtn")).to_have_text("Publish 0 to 2 servers")

    def test_review_opens_that_episode(self, authed_page: Page, app_url: str) -> None:
        view = _Season(authed_page, app_url, season())
        view.open_result()
        view.open_tab()
        page = view.whole_season()
        before = len(view.item_requests)
        _row(page, "E03").locator(".mk-season-action button").click()
        expect(page.locator("#markersViewEpisode")).to_be_checked()
        expect(page.locator("#markersEpisodeView")).to_be_visible()
        for _ in range(30):
            if len(view.item_requests) > before:
                break
            page.wait_for_timeout(100)
        assert parse_qs(urlparse(view.item_requests[before]).query)["path"] == [f"{_FOLDER}/South Park S01E03.mkv"]

    def test_a_season_error_is_shown(self, authed_page: Page, app_url: str) -> None:
        view = _Season(authed_page, app_url, {"error": "Not a TV episode"}, season_status=400)
        view.open_result()
        view.open_tab()
        page = view.whole_season()
        expect(page.locator("#markersSeasonBody .alert-warning")).to_have_text(
            "Couldn't load this season: Not a TV episode"
        )

    def test_a_second_toggle_uses_the_loaded_season(self, authed_page: Page, app_url: str) -> None:
        view = _Season(authed_page, app_url, season())
        view.open_result()
        view.open_tab()
        page = view.whole_season()
        page.locator("label[for='markersViewEpisode']").click()
        view.whole_season()
        assert len(view.season_requests) == 1

    def test_switching_to_whole_season_before_the_episode_loads_uses_the_canonical_path(
        self, authed_page: Page, app_url: str
    ) -> None:
        # The search result's media_file is a placeholder path; the episode's real (canonical) path only arrives
        # once GET /api/markers/item resolves. Picking "Whole season" before that happens must not strand the
        # season view on the placeholder's data once the real path is known.
        other_path = f"{_FOLDER}/South Park S01E03 (Alt Cut).mkv"
        item_payload = copy.deepcopy(south_park())
        item_payload["canonical_path"] = other_path

        wrong_season = season()
        right_season = copy.deepcopy(season())
        right_season["season"] = "Season 02"

        view = _Season(authed_page, app_url, wrong_season)
        held: list[Route] = []
        view.item_handler = lambda route: held.append(route)

        def _season_by_path(route: Route) -> None:
            view.season_requests.append(route.request.url)
            queried = parse_qs(urlparse(route.request.url).query)["path"][0]
            _fulfill_json(route, right_season if queried == other_path else wrong_season)

        authed_page.route("**/api/markers/season?**", _season_by_path)

        page = view.open_result()
        page.locator("#inspectorMarkersTabBtn").click()
        expect(page.locator("#markersViewToggle")).to_be_visible(timeout=3000)
        page.locator("label[for='markersViewSeason']").click()
        expect(page.locator(".mk-season-title")).to_contain_text("Season 01", timeout=3000)
        assert len(held) == 1

        held[0].fulfill(status=200, content_type="application/json", body=json.dumps(item_payload))

        expect(page.locator(".mk-season-title")).to_contain_text("Season 02", timeout=3000)
        paths = [parse_qs(urlparse(u).query)["path"][0] for u in view.season_requests]
        assert paths == [_MEDIA_FILE, other_path]

    def test_a_canonical_path_arriving_never_loads_a_season_nobody_asked_for(
        self, authed_page: Page, app_url: str
    ) -> None:
        other_path = f"{_FOLDER}/South Park S01E03 (Alt Cut).mkv"
        view = _Season(authed_page, app_url, season())
        view.payload = {**south_park(), "canonical_path": other_path}
        view.open_result()
        page = view.open_tab()
        expect(page.locator("#markersInspectorPath")).to_have_text(other_path)
        page.locator("button[data-bs-target='#inspector-tab-frames']").click()
        view.open_tab()
        page.wait_for_timeout(300)
        assert view.season_requests == []

    def test_re_showing_the_tab_asks_for_the_canonical_path_only(self, authed_page: Page, app_url: str) -> None:
        # Once the item's canonical path is known, showing the tab again must not re-request the placeholder
        # media_file's season first (setItem clears the path; the canonical one is remembered per media_file).
        other_path = f"{_FOLDER}/South Park S01E03 (Alt Cut).mkv"
        view = _Season(authed_page, app_url, season())
        view.payload = {**south_park(), "canonical_path": other_path}
        view.open_result()
        view.open_tab()
        page = view.whole_season()
        for _ in range(4):
            page.locator("button[data-bs-target='#inspector-tab-frames']").click()
            expect(page.locator("#inspector-tab-markers")).not_to_have_class(re.compile(r"\bactive\b"))
            view.open_tab()
            expect(page.locator("#markersSeasonBody .mk-season-title")).to_be_visible()
        page.wait_for_timeout(300)
        paths = [parse_qs(urlparse(u).query)["path"][0] for u in view.season_requests]
        assert paths == [other_path]

    def test_switching_to_another_episode_keeps_the_whole_season_view(self, authed_page: Page, app_url: str) -> None:
        other_path = f"{_FOLDER}/South Park S01E04.mkv"

        def _item_by_path(route: Route) -> None:
            queried = parse_qs(urlparse(route.request.url).query).get("path", [None])[0]
            payload = copy.deepcopy(south_park())
            payload["canonical_path"] = queried or _MEDIA_FILE
            _fulfill_json(route, payload)

        view = _Season(
            authed_page,
            app_url,
            season(),
            results=[_result(), _result(item_id="9999", media_file=other_path, title="South Park S01E04")],
        )
        view.item_handler = _item_by_path
        view.open_result(0)
        view.open_tab()
        page = view.whole_season()
        assert len(view.season_requests) == 1

        view.open_result(1)
        expect(page.locator("#markersViewSeason")).to_be_checked()
        expect(page.locator("#markersSeasonBody")).to_be_visible()
        expect(page.locator("#markersEpisodeView")).to_be_hidden()
        for _ in range(30):
            if len(view.season_requests) > 1:
                break
            page.wait_for_timeout(100)
        assert parse_qs(urlparse(view.season_requests[-1]).query)["path"] == [other_path]

    def test_switching_to_a_movie_leaves_the_whole_season_view(self, authed_page: Page, app_url: str) -> None:
        movie_path = "/data/movies/Movie (2020)/Movie.mkv"
        movie_result = {**_result(item_id="9999", media_file=movie_path, title="Movie (2020)"), "type": "movie"}
        view = _Season(authed_page, app_url, season(), results=[_result(), movie_result])
        view.open_result(0)
        view.open_tab()
        page = view.whole_season()

        view.open_result(1)
        expect(page.locator("#markersViewToggle")).to_be_hidden()
        expect(page.locator("#markersEpisodeView")).to_be_visible()
        expect(page.locator("#markersSeasonBody")).to_be_hidden()
        expect(page.locator("#markersViewEpisode")).to_be_checked()
