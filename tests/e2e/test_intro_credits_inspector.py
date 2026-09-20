"""E2E: Preview Inspector → Intro & Credits tab (read-only + Re-detect).

The search result, BIF endpoints and ``GET /api/markers/item`` are mocked with the payload shape
``markers.inspect.item_payload`` builds, so each test pins how one decision/evidence/server state renders.
"""

from __future__ import annotations

import copy
import json
import re
import urllib.parse

import pytest
from playwright.sync_api import Page, Route, expect

from ._mocks import _fulfill_json, mock_servers_list

_MEDIA_FILE = "/data/tv/South Park (1997)/Season 01/South Park S01E03.mkv"
_DURATION = 1_322_000  # 22:02


def _quote(value: str) -> str:
    """``value`` the way the page's encodeURIComponent writes it into a query."""
    return urllib.parse.quote(value, safe="-_.!~*'()")


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


def _decision(status: str | None, marker: tuple[str, int, int] | None = None, **extra) -> dict:
    return {
        "status": status,
        "reason": extra.get("reason", ""),
        "shortened_by": extra.get("shortened_by"),
        "marker": (
            {
                "type": marker[0],
                "start_ms": marker[1],
                "end_ms": marker[2],
                "decided_by": extra.get("decided_by", ["chapters"]),
                "locked": False,
            }
            if marker
            else None
        ),
        "proposed": extra.get("proposed"),
    }


def _evidence(source: str, mtype: str | None, start: int | None, end: int | None, detail: str = "") -> dict:
    return {
        "source": source,
        "origin": "",
        "type": mtype,
        "start_ms": start,
        "end_ms": end,
        "confidence": 1.0,
        "detail": detail,
        "fetched_at": "2026-09-14T01:00:00+00:00",
    }


def _server(server_id: str, name: str, stype: str, plan: str, current: list | None, **extra) -> dict:
    row = {
        "server_id": server_id,
        "server_name": name,
        "server_type": stype,
        "markers_enabled": True,
        "capability_state": "ready",
        "can_show": ["intro", "credits", "recap", "preview"] if stype == "jellyfin" else ["intro", "credits"],
        "current": current,
        "published": [],
        "publish_status": None,
        "publish_message": "",
        "plan": plan,
        "plan_reason": "",
        "error": None,
    }
    row.update(extra)
    return row


def _marker(mtype: str, start: int, end: int) -> dict:
    return {"type": mtype, "start_ms": start, "end_ms": end}


def south_park() -> dict:
    return {
        "known": True,
        "canonical_path": _MEDIA_FILE,
        "duration_ms": _DURATION,
        "is_movie": False,
        "decisions": {
            "intro": _decision("decided", ("intro", 11_000, 37_000)),
            "credits": _decision("decided", ("credits", 1_299_000, _DURATION)),
            "recap": _decision("disabled"),
            "preview": _decision("disabled"),
        },
        "evidence": [
            _evidence("chapters", "intro", 11_000, 37_000),
            _evidence("chapters", "credits", 1_299_000, _DURATION),
            _evidence("theintrodb", "intro", 0, 35_000),
            _evidence("introdb", None, None, None, "IntroDB needs a TV episode with an imdb id"),
        ],
        "servers": [
            _server(
                "plex-1",
                "Plex",
                "plex",
                "will_replace",
                [_marker("intro", 76_508, 112_748), _marker("credits", 1_265_000, 1_297_000)],
                version_count=1,
            ),
            _server("jf-1", "Jellyfin", "jellyfin", "will_add", []),
            _server("emby-1", "Emby", "emby", "will_add", []),
        ],
    }


def _labelled(source: str, mtype: str, start: int, end: int, label: str) -> dict:
    return {**_evidence(source, mtype, start, end), "label": label}


def season_audio() -> dict:
    payload = south_park()
    payload["decisions"]["intro"] = _decision(
        "decided", ("intro", 2_000, 29_000), decided_by=["season_audio", "theintrodb"]
    )
    payload["evidence"] = [
        _labelled("season_audio", "intro", 2_000, 29_000, "10/10"),
        _labelled("season_audio_previous", "intro", 2_500, 29_500, "4/4"),
        _evidence("theintrodb", "intro", 1_000, 29_000),
        # chapters isn't in COUNTED_SOURCES: a label here (some chapter title, not a match count) must never be
        # appended to its bar the way season_audio's is.
        _labelled("chapters", "intro", 5_000, 27_000, "Opening"),
    ]
    payload["decisions"]["credits"] = _decision("decided", ("credits", 1_250_000, 1_280_000))
    # Owner decision R1: Emby still gets this credits marker (never hidden) — but the real backend
    # (markers.inspect._plan / publishers.emby.credits_note) adds this note because it ends well before the file
    # does. The frontend only ever displays plan_reason verbatim, so the fixture supplies it rather than the JS
    # guessing at the wording.
    emby = next(s for s in payload["servers"] if s["server_id"] == "emby-1")
    emby["plan_reason"] = "Emby skips to the end of the file"
    return payload


def needs_review() -> dict:
    payload = south_park()
    payload["decisions"]["credits"] = _decision(
        "needs_review",
        reason="Sources disagree by more than 10 s",
        proposed={"start_ms": 1_290_000, "end_ms": _DURATION},
    )
    payload["evidence"] = [
        _evidence("chapters", "intro", 11_000, 37_000),
        _evidence("chapters", "credits", 1_290_000, _DURATION),
        _evidence("theintrodb", "credits", 1_250_000, _DURATION),
    ]
    return payload


def movie() -> dict:
    return {
        "known": True,
        "canonical_path": _MEDIA_FILE,
        "duration_ms": 7_200_000,
        "is_movie": True,
        "decisions": {
            "intro": _decision("disabled"),
            "credits": _decision("decided", ("credits", 6_900_000, 7_200_000)),
            "recap": _decision("disabled"),
            "preview": _decision("disabled"),
        },
        "evidence": [_evidence("chapters", "credits", 6_900_000, 7_200_000)],
        "servers": [_server("plex-1", "Plex", "plex", "up_to_date", [_marker("credits", 6_900_000, 7_200_000)])],
    }


def movie_with_credit_text() -> dict:
    payload = movie()
    payload["decisions"]["credits"] = _decision(
        "decided", ("credits", 6_912_000, 7_200_000), decided_by=["credits_text", "server_markers"]
    )
    payload["evidence"] = [
        _evidence("chapters", None, None, None, "No chapters"),
        _evidence("credits_text", "credits", 6_912_000, None),
    ]
    payload["servers"] = [_server("plex-1", "Plex", "plex", "up_to_date", [_marker("credits", 6_915_000, 7_200_000)])]
    return payload


def not_checked() -> dict:
    return {
        "known": False,
        "canonical_path": _MEDIA_FILE,
        "duration_ms": None,
        "is_movie": None,
        "decisions": {t: _decision(None) for t in ("intro", "credits", "recap", "preview")},
        "evidence": [],
        "servers": [_server("plex-1", "Plex", "plex", "nothing_to_publish", [])],
    }


def every_plan() -> dict:
    payload = south_park()
    intro_now = [_marker("intro", 11_000, 37_000)]
    payload["servers"] = [
        _server("s-add", "Adds", "jellyfin", "will_add", []),
        _server("s-replace", "Replaces", "plex", "will_replace", [_marker("intro", 76_508, 112_748)], version_count=2),
        _server(
            "s-keep",
            "Keeps",
            "plex",
            "keeps_plex",
            [_marker("intro", 11_000, 37_000), _marker("credits", 1_250_000, 1_280_000)],
            plan_reason="Keeping Plex's credits",
            version_count=1,
        ),
        _server(
            "s-remove",
            "Removes",
            "jellyfin",
            "will_remove",
            intro_now,
            published=[_marker("intro", 11_000, 37_000)],
        ),
        _server("s-same", "Same", "jellyfin", "up_to_date", intro_now + [_marker("credits", 1_299_000, _DURATION)]),
        _server(
            "s-wait",
            "Waits",
            "plex",
            "waiting",
            [],
            plan_reason="versions don't agree yet",
            publish_status="waiting",
            publish_message="Not in this server's library yet",
        ),
        _server("s-unread", "Unreadable", "emby", "unknown", None),
        _server(
            "s-broken",
            "Broken",
            "plex",
            "unknown",
            None,
            capability_state="unknown",
            markers_enabled=False,
            error="Couldn't read this server's Intro & Credits state (TimeoutError)",
        ),
        _server(
            "s-off",
            "Off",
            "emby",
            "not_enabled",
            [],
            markers_enabled=False,
            plan_reason="Intro & Credits is off for this server",
        ),
        _server("s-nothing", "Nothing", "jellyfin", "nothing_to_publish", []),
        _server(
            "s-failed",
            "Failing",
            "jellyfin",
            "will_add",
            [],
            publish_status="failed",
            publish_message="HTTP 500 from the plugin",
        ),
    ]
    return payload


def _result(item_id: str = "4321", media_file: str = _MEDIA_FILE, title: str = "South Park S01E03") -> dict:
    return {
        "title": title,
        "type": "episode",
        "year": None,
        "media_file": media_file,
        "item_id": item_id,
        "preview_kind": "bif",
        "preview_path": "/plex/Media/localhost/a/bcd.bundle/Contents/Indexes/index-sd.bif",
        "preview_exists": True,
    }


class _Inspector:
    """The Inspector page with one search result, and a spy on the markers endpoints."""

    def __init__(
        self,
        page: Page,
        app_url: str,
        payload: dict,
        results: list[dict] | None = None,
        item_handler=None,
        server_type: str = "plex",
    ) -> None:
        self.page = page
        self.app_url = app_url
        self.payload = payload
        self.item_requests: list[str] = []
        self.info_requests: list[str] = []
        self.redetect_bodies: list[dict] = []
        self.save_bodies: list[dict] = []
        self.unlock_bodies: list[dict] = []
        # What POST / DELETE /api/markers/item/markers answer. A tuple of (status, body) so a test can make one fail.
        self.save_answer: tuple[int, dict] = (200, {"markers": {}, "servers": []})
        self.unlock_answer: tuple[int, dict] = (200, {"unlocked": [], "markers": {}, "decisions": {}})
        self.item_handler = item_handler
        server = {"id": "plex-1", "name": server_type.title(), "type": server_type, "enabled": True, "url": "http://p"}
        mock_servers_list(page, servers=[server])
        page.route(
            "**/api/bif/servers/*/search**",
            lambda r: _fulfill_json(
                r, {"server_id": "plex-1", "server_type": server_type, "results": results or [_result()]}
            ),
        )
        page.route("**/api/bif/info**", self._info)
        page.route("**/api/bif/frame**", lambda r: r.fulfill(status=200, body=b""))
        page.route("**/api/markers/item?**", self._item)
        page.route("**/api/markers/item/redetect", self._redetect)
        page.route("**/api/markers/item/markers", self._markers)

    def _info(self, route: Route) -> None:
        self.info_requests.append(route.request.url)
        _fulfill_json(
            route,
            {
                "frame_count": 5,
                "frame_interval_ms": 2000,
                "file_size": 100,
                "avg_frame_size": 20,
                "suspect_frame_count": 0,
                "created_at": None,
            },
        )

    def _item(self, route: Route) -> None:
        self.item_requests.append(route.request.url)
        if self.item_handler is not None:
            self.item_handler(route)
            return
        _fulfill_json(route, self.payload)

    def _redetect(self, route: Route) -> None:
        self.redetect_bodies.append(route.request.post_data_json or {})
        _fulfill_json(route, {"job_id": "9f8e7d6c-1111-4222-8333-444455556666"}, status=202)

    def _markers(self, route: Route) -> None:
        body = route.request.post_data_json or {}
        if route.request.method == "DELETE":
            self.unlock_bodies.append(body)
            status, answer = self.unlock_answer
        else:
            self.save_bodies.append(body)
            status, answer = self.save_answer
        _fulfill_json(route, answer, status=status)

    def open_result(self, index: int = 0) -> Page:
        page = self.page
        if not page.url.endswith("/bif-viewer"):
            page.goto(f"{self.app_url}/bif-viewer")
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_function("() => document.querySelector('#serverSelect option[value=\"plex-1\"]')")
            page.locator("#searchInput").fill("South Park")
            page.locator("#searchBtn").click()
        page.locator(".result-item").nth(index).click()
        expect(page.locator("#viewerPanel")).not_to_have_class(re.compile(r"\bd-none\b"), timeout=3000)
        return page

    def open_tab(self) -> Page:
        self.page.locator("#inspectorMarkersTabBtn").click()
        expect(self.page.locator("#inspector-tab-markers")).to_have_class(re.compile(r"\bactive\b"))
        # An empty body doesn't contain "Loading" either, so wait for it to have rendered something first.
        expect(self.page.locator("#markersInspectorBody > *")).not_to_have_count(0, timeout=5000)
        expect(self.page.locator("#markersInspectorBody")).not_to_contain_text("Loading", timeout=3000)
        return self.page


def _lane(page: Page, window: str, label: str):
    return page.locator(f'.mk-window[data-window="{window}"] .mk-lane[data-lane="{label}"]')


def _server_card(page: Page, server_id: str):
    return page.locator(f'.mk-server[data-server-id="{server_id}"]')


@pytest.mark.e2e
class TestIntroCreditsTab:
    def test_south_park_decision_evidence_and_servers(self, authed_page: Page, app_url: str) -> None:
        inspector = _Inspector(authed_page, app_url, south_park())
        inspector.open_result()
        page = inspector.open_tab()

        expect(page.locator("#markersInspectorPath")).to_have_text(_MEDIA_FILE)
        windows = page.locator(".mk-window")
        expect(windows).to_have_count(2)
        expect(windows.nth(0).locator(".mk-window-title")).to_have_text("Opening · 0:00 – 3:00")
        expect(windows.nth(1).locator(".mk-window-title")).to_have_text("Ending · 19:02 – 22:02 (end of file)")
        expect(windows.nth(0).locator(".mk-axis span")).to_have_count(4)

        decision = _lane(page, "opening", "Decision")
        expect(decision).to_contain_text("0:11–0:37")
        expect(_lane(page, "ending", "Decision")).to_contain_text("21:39 →")
        plex_now = _lane(page, "opening", "Plex now")
        expect(plex_now).to_have_class(re.compile(r"\blane-disagree\b"))
        expect(plex_now).to_contain_text("✕")
        expect(plex_now).to_contain_text("1:16–1:52")
        expect(_lane(page, "ending", "Plex now")).to_have_class(re.compile(r"\blane-disagree\b"))
        chapters = _lane(page, "opening", "Chapters")
        expect(chapters).to_contain_text("0:11–0:37")
        expect(chapters).not_to_have_class(re.compile(r"\blane-disagree\b"))
        # TheIntroDB's end (0:35) is within 5 s of the decided end (0:37): it agrees.
        expect(_lane(page, "opening", "TheIntroDB")).not_to_have_class(re.compile(r"\blane-disagree\b"))
        expect(_lane(page, "opening", "IntroDB")).to_contain_text("IntroDB needs a TV episode with an imdb id")
        expect(_lane(page, "opening", "Jellyfin now")).to_contain_text("none yet")

        # The decided bar sits at (11 000 − 0) / 180 000 of the window, 26 s wide.
        bar = decision.locator(".mk-bar").first
        style = bar.get_attribute("style") or ""
        assert "left: 6.11" in style and "width: 14.44" in style, style

        chips = page.locator(".mk-chips")
        expect(chips).to_contain_text("Intro 0:11–0:37")
        expect(chips).to_contain_text("Credits 21:39 →")
        expect(chips).to_contain_text("Recap: Detection off")
        expect(chips).to_contain_text("Preview: Detection off")

        plex = _server_card(page, "plex-1")
        expect(plex.locator(".mk-plan")).to_have_text("Will replace")
        expect(plex).to_contain_text("Intro 1:16–1:52 → 0:11–0:37")
        # One version: the shared-set note would only confuse.
        expect(plex).not_to_contain_text("All versions")
        expect(_server_card(page, "jf-1").locator(".mk-plan")).to_have_text("Will add")
        expect(_server_card(page, "jf-1")).not_to_contain_text("All versions")
        expect(_server_card(page, "emby-1")).to_contain_text("Emby has no “credits end”")
        # Nothing was shortened to a server's own marker: no note under either window.
        expect(page.locator(".mk-window-note")).to_have_count(0)

    @pytest.mark.parametrize(("count", "shown"), [(2, True), (1, False), (None, False)], ids=["two", "one", "unknown"])
    def test_shared_marker_set_note_only_for_a_plex_item_with_several_versions(
        self, authed_page: Page, app_url: str, count, shown: bool
    ) -> None:
        payload = south_park()
        payload["servers"][0]["version_count"] = count
        inspector = _Inspector(authed_page, app_url, payload)
        inspector.open_result()
        page = inspector.open_tab()
        plex = _server_card(page, "plex-1")
        expect(plex.locator(".mk-plan")).to_have_text("Will replace")
        if shown:
            expect(plex).to_contain_text("All versions of this item share one set of markers")
        else:
            expect(plex).not_to_contain_text("All versions")

    def test_season_audio_lanes_carry_their_match_count(self, authed_page: Page, app_url: str) -> None:
        inspector = _Inspector(authed_page, app_url, season_audio())
        inspector.open_result()
        page = inspector.open_tab()
        expect(_lane(page, "opening", "Season audio").locator(".mk-bar")).to_have_text("0:02–0:29 · 10/10")
        expect(_lane(page, "opening", "Previous season audio").locator(".mk-bar")).to_have_text("0:02–0:29 · 4/4")
        expect(_lane(page, "opening", "TheIntroDB").locator(".mk-bar")).to_have_text("0:01–0:29")
        # chapters isn't a counted source: its "Opening" label must not turn into " · Opening" on the bar.
        expect(_lane(page, "opening", "Chapters").locator(".mk-bar")).to_have_text("0:05–0:27")

    def test_emby_card_explains_credits_that_end_before_the_file(self, authed_page: Page, app_url: str) -> None:
        # Owner decision R1: Emby always gets the decided credits start, even one that ends well before the file
        # does — the card must still list it, alongside the backend's plan_reason note explaining the consequence.
        inspector = _Inspector(authed_page, app_url, season_audio())
        inspector.open_result()
        page = inspector.open_tab()
        card = _server_card(page, "emby-1")
        expect(card).to_contain_text("Credits 20:50–21:20")
        expect(card).to_contain_text("Emby skips to the end of the file")
        # Not duplicated: the frontend has no copy of this wording of its own, only what plan_reason carries.
        assert card.inner_text().count("Emby skips to the end of the file") == 1
        expect(_server_card(page, "jf-1")).not_to_contain_text("Emby skips to the end of the file")

    def test_emby_card_for_credits_to_the_end_keeps_the_no_end_note(self, authed_page: Page, app_url: str) -> None:
        # plan_reason carries no note here (credits run to the end): the backend-specific text must not appear,
        # only the evergreen "Emby has no credits end" structural note.
        inspector = _Inspector(authed_page, app_url, south_park())
        inspector.open_result()
        page = inspector.open_tab()
        card = _server_card(page, "emby-1")
        expect(card).to_contain_text("Emby has no “credits end”")
        expect(card).not_to_contain_text("Emby skips to the end of the file")

    def test_emby_card_combines_kept_and_credits_notes(self, authed_page: Page, app_url: str) -> None:
        # "Keep Emby's" row: plan_reason can carry both the kept-types note and the credits-before-end note
        # together — markers.outcomes.with_kept_note joins them as "Keeping Emby's intro; Emby skips to the end
        # of the file" (kept-types first, capitalised, since it's the message the credits note gets appended to).
        payload = season_audio()
        emby = next(s for s in payload["servers"] if s["server_id"] == "emby-1")
        emby["plan"] = "keeps_emby"
        emby["plan_reason"] = "Keeping Emby's intro; Emby skips to the end of the file"
        inspector = _Inspector(authed_page, app_url, payload)
        inspector.open_result()
        page = inspector.open_tab()
        card = _server_card(page, "emby-1")
        expect(card.locator(".mk-plan")).to_have_text("Keeps Emby's")
        expect(card).to_contain_text("Keeping Emby's intro")
        expect(card).to_contain_text("Emby skips to the end of the file")

    def test_needs_review_shows_the_chip_and_a_dashed_proposal(self, authed_page: Page, app_url: str) -> None:
        inspector = _Inspector(authed_page, app_url, needs_review())
        inspector.open_result()
        page = inspector.open_tab()

        expect(page.locator(".mk-chips")).to_contain_text("Needs review")
        decision = _lane(page, "ending", "Decision")
        expect(decision).to_have_class(re.compile(r"\blane-proposed\b"))
        expect(decision.locator(".mk-bar.mk-bar-proposed")).to_contain_text("21:30 →")
        expect(page.locator(".mk-window").nth(1)).to_contain_text("Sources disagree by more than 10 s")
        # Nothing is decided for credits, so no credits lane can disagree with it.
        expect(_lane(page, "ending", "TheIntroDB")).not_to_have_class(re.compile(r"\blane-disagree\b"))

    @pytest.mark.parametrize(
        ("servers", "line"),
        [
            (["Lab Plex"], "Shortened to Lab Plex's own credits start"),
            (["Lab Plex", "Jellyfin"], "Shortened to the servers' own credits start (Lab Plex, Jellyfin)"),
        ],
        ids=["one-server", "two-servers"],
    )
    def test_credits_shortened_to_a_servers_own_marker_say_so_under_the_ending(
        self, authed_page: Page, app_url: str, servers: list, line: str
    ) -> None:
        payload = south_park()
        credits = payload["decisions"]["credits"]
        credits["reason"] = "chapters; start shortened to the server's own marker (plex-1)"
        credits["shortened_by"] = {"servers": servers}
        credits["marker"]["decided_by"] = ["chapters", "server_markers"]
        inspector = _Inspector(authed_page, app_url, payload)
        inspector.open_result()
        page = inspector.open_tab()

        note = page.locator('.mk-window[data-window="ending"] .mk-window-note')
        expect(note).to_have_count(1)
        expect(note).to_have_text(line)
        expect(page.locator('.mk-window[data-window="opening"] .mk-window-note')).to_have_count(0)
        expect(page.locator(".mk-chips")).to_contain_text("Credits 21:39 →")

    def test_movie_shows_only_the_ending_window(self, authed_page: Page, app_url: str) -> None:
        inspector = _Inspector(authed_page, app_url, movie())
        inspector.open_result()
        page = inspector.open_tab()

        windows = page.locator(".mk-window")
        expect(windows).to_have_count(1)
        # Movie credits start 5 minutes before the end: the window widens to a whole minute with some lead-in.
        expect(windows.first.locator(".mk-window-title")).to_have_text("Ending · 1:54:00 – 2:00:00 (end of file)")
        expect(_lane(page, "ending", "Decision")).to_contain_text("1:55:00 →")
        expect(_server_card(page, "plex-1").locator(".mk-plan")).to_have_text("Up to date")
        # Intros and recaps are never detected for movies; preview is simply off.
        chips = page.locator(".mk-chips")
        expect(chips).to_contain_text("Intro: not used for movies")
        expect(chips).to_contain_text("Recap: not used for movies")
        expect(chips).to_contain_text("Preview: Detection off")
        expect(chips).not_to_contain_text("Intro: Detection off")

    def test_credit_text_has_its_own_lane_in_the_ending(self, authed_page: Page, app_url: str) -> None:
        inspector = _Inspector(authed_page, app_url, movie_with_credit_text())
        inspector.open_result()
        page = inspector.open_tab()
        expect(_lane(page, "ending", "Credit text")).to_contain_text("1:55:12 →")
        expect(_lane(page, "ending", "Credit text").locator(".mk-disagree")).to_have_count(0)

    def test_credit_text_with_a_scene_after_the_roll_shows_where_the_skip_ends(
        self, authed_page: Page, app_url: str
    ) -> None:
        # Q3: an end 20 s before the end of a 2 h file ("1:59:40"); laneRange() writes "start–end" unless the end is
        # within 2 s of the end of the file.
        payload = movie_with_credit_text()
        payload["evidence"][-1] = _evidence("credits_text", "credits", 6_912_000, 7_180_000)
        payload["decisions"]["credits"] = _decision(
            "decided", ("credits", 6_912_000, 7_180_000), decided_by=["credits_text"]
        )
        inspector = _Inspector(authed_page, app_url, payload)
        inspector.open_result()
        page = inspector.open_tab()
        expect(_lane(page, "ending", "Credit text")).to_contain_text("1:55:12–1:59:40")
        expect(_lane(page, "ending", "Decision")).to_contain_text("1:55:12–1:59:40")

    def test_credit_text_that_found_nothing_says_so(self, authed_page: Page, app_url: str) -> None:
        payload = movie()
        payload["evidence"].append(_evidence("credits_text", None, None, None))
        inspector = _Inspector(authed_page, app_url, payload)
        inspector.open_result()
        page = inspector.open_tab()
        expect(_lane(page, "ending", "Credit text")).to_contain_text("Nothing found")

    def test_late_intro_widens_the_opening_window_and_far_segments_are_noted(
        self, authed_page: Page, app_url: str
    ) -> None:
        payload = south_park()
        payload["decisions"]["intro"] = _decision("decided", ("intro", 190_000, 220_000))
        payload["evidence"] = [_evidence("chapters", "intro", 190_000, 220_000)]
        payload["servers"] = [_server("plex-1", "Plex", "plex", "will_replace", [_marker("intro", 360_000, 390_000)])]
        inspector = _Inspector(authed_page, app_url, payload)
        inspector.open_result()
        page = inspector.open_tab()

        expect(page.locator(".mk-window").first.locator(".mk-window-title")).to_have_text("Opening · 0:00 – 5:00")
        expect(_lane(page, "opening", "Decision")).to_contain_text("Intro 3:10–3:40")
        plex_now = _lane(page, "opening", "Plex now")
        expect(plex_now).to_contain_text("6:00–6:30 · outside this view")
        expect(plex_now).to_have_class(re.compile(r"\blane-disagree\b"))

    def test_recap_and_preview_decisions_use_their_own_tolerances(self, authed_page: Page, app_url: str) -> None:
        payload = south_park()
        payload["decisions"] = {
            "intro": _decision("decided", ("intro", 45_000, 75_000)),
            "credits": _decision("no_evidence"),
            "recap": _decision("decided", ("recap", 0, 40_000)),
            "preview": _decision("decided", ("preview", 1_300_000, _DURATION)),
        }
        payload["evidence"] = [
            _evidence("chapters", "intro", 45_000, 75_000),
            _evidence("chapters", "preview", 1_308_000, _DURATION),
            # Recap agrees on its end: 20 s apart at the start, 3 s at the end.
            _evidence("season_audio", "recap", 20_000, 43_000),
            # 7 s past the recap's end: outside the 5 s recap tolerance (10 s would have let it agree).
            _evidence("theintrodb", "recap", 0, 47_000),
            # Preview agrees on its start: 11 s early is outside 10 s, although its end is only 7 s off.
            _evidence("skipdb", "preview", 1_289_000, 1_315_000),
        ]
        payload["servers"] = [_server("jf-1", "Jellyfin", "jellyfin", "will_add", [])]
        inspector = _Inspector(authed_page, app_url, payload)
        inspector.open_result()
        page = inspector.open_tab()

        chips = page.locator(".mk-chips")
        expect(chips).to_contain_text("Recap 0:00–0:40")
        expect(chips).to_contain_text("Intro 0:45–1:15")
        expect(chips).to_contain_text("Preview 21:40 →")
        expect(chips).to_contain_text("Credits: No markers found")
        opening = _lane(page, "opening", "Decision")
        expect(opening.locator(".mk-bar-result", has_text="Recap 0:00–0:40")).to_have_count(1)
        expect(opening.locator(".mk-bar-result", has_text="Intro 0:45–1:15")).to_have_count(1)
        ending = _lane(page, "ending", "Decision")
        expect(ending.locator(".mk-bar-result", has_text="Preview 21:40 →")).to_have_count(1)
        expect(ending).to_contain_text("Credits: no markers found")
        # Preview and credits live in the ending window only; recap and intro in the opening one only.
        expect(page.locator('.mk-window[data-window="ending"] .mk-bar-result', has_text="Recap")).to_have_count(0)
        expect(page.locator('.mk-window[data-window="opening"] .mk-bar-result', has_text="Preview")).to_have_count(0)

        disagree = re.compile(r"\blane-disagree\b")
        expect(_lane(page, "opening", "Season audio")).not_to_have_class(disagree)
        expect(_lane(page, "opening", "TheIntroDB")).to_have_class(disagree)
        expect(_lane(page, "opening", "Chapters")).not_to_have_class(disagree)
        expect(_lane(page, "ending", "Chapters")).not_to_have_class(disagree)
        expect(_lane(page, "ending", "SkipDB")).to_have_class(disagree)
        expect(page.locator('.mk-window[data-window="ending"] .mk-lane[data-lane="Season audio"]')).to_have_count(0)

    def test_needs_review_recap_and_preview_show_their_proposals(self, authed_page: Page, app_url: str) -> None:
        payload = south_park()
        payload["decisions"]["recap"] = _decision(
            "needs_review", reason="Recap sources disagree", proposed={"start_ms": 0, "end_ms": 40_000}
        )
        payload["decisions"]["preview"] = _decision(
            "needs_review", reason="Preview sources disagree", proposed={"start_ms": 1_300_000, "end_ms": _DURATION}
        )
        payload["evidence"] = [
            _evidence("chapters", "intro", 11_000, 37_000),
            _evidence("theintrodb", "recap", 0, 80_000),
            _evidence("skipdb", "preview", 1_200_000, _DURATION),
        ]
        inspector = _Inspector(authed_page, app_url, payload)
        inspector.open_result()
        page = inspector.open_tab()

        chips = page.locator(".mk-chips")
        expect(chips).to_contain_text("Recap: Needs review")
        expect(chips).to_contain_text("Preview: Needs review")
        opening = _lane(page, "opening", "Decision")
        expect(opening).to_have_class(re.compile(r"\blane-proposed\b"))
        expect(opening.locator(".mk-bar-proposed")).to_contain_text("Recap 0:00–0:40")
        expect(opening.locator(".mk-bar-result")).to_contain_text("Intro 0:11–0:37")
        ending = _lane(page, "ending", "Decision")
        expect(ending).to_have_class(re.compile(r"\blane-proposed\b"))
        expect(ending.locator(".mk-bar-proposed")).to_contain_text("Preview 21:40 →")
        expect(page.locator('.mk-window[data-window="opening"]')).to_contain_text(
            "Recap needs review: Recap sources disagree"
        )
        expect(page.locator('.mk-window[data-window="ending"]')).to_contain_text(
            "Preview needs review: Preview sources disagree"
        )
        # Nothing is decided for recap or preview, so their far-off evidence can't disagree with a decision.
        expect(_lane(page, "opening", "TheIntroDB")).not_to_have_class(re.compile(r"\blane-disagree\b"))
        expect(_lane(page, "ending", "SkipDB")).not_to_have_class(re.compile(r"\blane-disagree\b"))

    def test_not_checked_yet_then_redetect_queues_a_job(self, authed_page: Page, app_url: str) -> None:
        inspector = _Inspector(authed_page, app_url, not_checked())
        inspector.open_result()
        page = inspector.open_tab()

        expect(page.locator("#markersInspectorBody")).to_contain_text("Not checked yet")
        expect(page.locator(".mk-window")).to_have_count(0)
        expect(_server_card(page, "plex-1").locator(".mk-plan")).to_have_text("Nothing to send yet")
        button = page.locator("#markersRedetectBtn")
        expect(button).to_be_enabled()

        with page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/api/markers/item/redetect"), timeout=5000
        ) as req:
            button.click()

        assert req.value.request.method == "POST"
        assert inspector.redetect_bodies == [{"path": _MEDIA_FILE}]
        toast = page.locator("#toastNotification")
        expect(toast).to_contain_text("Queued — see the Dashboard", timeout=3000)
        link = toast.locator("a", has_text="9f8e7d6c")
        expect(link).to_have_attribute("href", "/?job=9f8e7d6c-1111-4222-8333-444455556666")

    def test_every_plan_state_renders(self, authed_page: Page, app_url: str) -> None:
        inspector = _Inspector(authed_page, app_url, every_plan())
        inspector.open_result()
        page = inspector.open_tab()

        expected = {
            "s-add": ("Will add", ["Intro 0:11–0:37 · Credits 21:39–end"]),
            "s-replace": ("Will replace", ["Intro 1:16–1:52 → 0:11–0:37", "All versions of this item share"]),
            "s-keep": ("Keeps Plex's", ["Keeping Plex's credits"]),
            "s-remove": ("Will remove", ["Removes Intro 0:11–0:37"]),
            "s-same": ("Up to date", ["Intro 0:11–0:37 · Credits 21:39–end"]),
            "s-wait": ("Waiting", ["versions don't agree yet", "Not in this server's library yet"]),
            "s-unread": ("Unknown", ["Couldn't read what the server shows"]),
            "s-broken": ("Unknown", ["Couldn't read this server's Intro & Credits state (TimeoutError)"]),
            "s-off": ("Intro & Credits off", ["Intro & Credits is off for this server"]),
            "s-nothing": ("Nothing to send yet", []),
            "s-failed": ("Will add", ["HTTP 500 from the plugin"]),
        }
        for server_id, (label, lines) in expected.items():
            card = _server_card(page, server_id)
            expect(card.locator(".mk-plan")).to_have_text(label)
            for line in lines:
                expect(card).to_contain_text(line)
        expect(_server_card(page, "s-keep")).not_to_contain_text("All versions")
        expect(_server_card(page, "s-unread")).not_to_contain_text("none yet")
        expect(_lane(page, "opening", "Unreadable now")).to_contain_text("Couldn't read what the server shows")
        expect(_lane(page, "opening", "Broken now")).to_contain_text("Couldn't read what the server shows")
        expect(_server_card(page, "s-broken").locator(".mk-error")).to_be_visible()


@pytest.mark.e2e
class TestIntroCreditsTabLoading:
    def test_markers_are_only_requested_once_the_tab_opens(self, authed_page: Page, app_url: str) -> None:
        inspector = _Inspector(authed_page, app_url, south_park())
        page = inspector.open_result()
        expect(page.locator("#inspector-tab-frames")).to_have_class(re.compile(r"\bactive\b"))
        expect(page.locator("#previewFrame")).to_be_visible()
        page.wait_for_timeout(300)
        assert inspector.item_requests == []

        inspector.open_tab()
        assert len(inspector.item_requests) == 1
        assert "path=" in inspector.item_requests[0]
        assert "South%20Park%20S01E03.mkv" in inspector.item_requests[0]

        # Back to Frames and again: the item is cached.
        page.locator('button[data-bs-target="#inspector-tab-frames"]').click()
        expect(page.locator("#previewFrame")).to_be_visible()
        page.locator("#nextBtn").click()
        expect(page.locator("#currentFrame")).to_have_text("1")
        inspector.open_tab()
        assert len(inspector.item_requests) == 1

    def test_a_second_item_with_the_tab_open_loads_its_own_markers(self, authed_page: Page, app_url: str) -> None:
        other = "/data/tv/South Park (1997)/Season 01/South Park S01E04.mkv"
        inspector = _Inspector(
            authed_page,
            app_url,
            south_park(),
            results=[_result(), _result(item_id="9999", media_file=other, title="South Park S01E04")],
        )
        inspector.open_result(0)
        inspector.open_tab()
        second = copy.deepcopy(south_park())
        second["canonical_path"] = other
        inspector.payload = second

        inspector.open_result(1)

        expect(authed_page.locator("#markersInspectorPath")).to_have_text(other, timeout=3000)
        assert len(inspector.item_requests) == 2
        assert "S01E04.mkv" in inspector.item_requests[1]

    def test_a_pasted_preview_path_has_no_file_to_look_up(self, authed_page: Page, app_url: str) -> None:
        inspector = _Inspector(authed_page, app_url, south_park())
        page = authed_page
        page.goto(f"{app_url}/bif-viewer")
        page.wait_for_load_state("domcontentloaded")
        page.locator('button[data-bs-target="#tabPath"]').click()
        page.locator("#pathInput").fill("/plex/Media/localhost/a/bcd.bundle/Contents/Indexes/index-sd.bif")
        page.locator("#loadPathBtn").click()
        expect(page.locator("#viewerPanel")).not_to_have_class(re.compile(r"\bd-none\b"), timeout=3000)

        inspector.open_tab()

        expect(page.locator("#markersInspectorBody")).to_contain_text("Search for the title")
        expect(page.locator("#markersRedetectBtn")).to_be_disabled()
        assert inspector.item_requests == []

    def test_a_path_the_app_cant_place_is_retried_by_server_and_item_id(self, authed_page: Page, app_url: str) -> None:
        payload = south_park()

        def by_id_only(route: Route) -> None:
            if "path=" in route.request.url:
                _fulfill_json(route, {"error": "Path is not a file inside any server library"}, status=400)
            else:
                _fulfill_json(route, payload)

        inspector = _Inspector(authed_page, app_url, payload, item_handler=by_id_only)
        inspector.open_result()
        page = inspector.open_tab()

        assert len(inspector.item_requests) == 2
        assert "path=" in inspector.item_requests[0]
        assert "server_id=plex-1&item_id=4321" in inspector.item_requests[1]
        assert "path=" not in inspector.item_requests[1].replace("version_file=", "")
        # The version clicked goes along, so the server's other version never opens in its place.
        assert "version_file=" + _quote(_MEDIA_FILE) in inspector.item_requests[1]
        expect(_server_card(page, "plex-1").locator(".mk-plan")).to_have_text("Will replace")
        expect(page.locator("#markersInspectorPath")).to_have_text(_MEDIA_FILE)

    def test_a_slow_answer_for_the_previous_file_doesnt_replace_the_current_one(
        self, authed_page: Page, app_url: str
    ) -> None:
        other = "/data/tv/South Park (1997)/Season 01/South Park S01E04.mkv"
        first = south_park()
        second = copy.deepcopy(south_park())
        second["canonical_path"] = other
        second["servers"] = [_server("plex-e04", "Plex E04", "plex", "up_to_date", [])]
        held: list[Route] = []

        def respond(route: Route) -> None:
            if "S01E03" in route.request.url:
                held.append(route)  # answered later, after the second file's answer
            else:
                _fulfill_json(route, second)

        inspector = _Inspector(
            authed_page,
            app_url,
            first,
            results=[_result(), _result(item_id="9999", media_file=other, title="South Park S01E04")],
            item_handler=respond,
        )
        page = inspector.open_result(0)
        page.locator("#inspectorMarkersTabBtn").click()
        expect(page.locator("#markersInspectorBody")).to_contain_text("Loading", timeout=3000)
        assert len(held) == 1

        inspector.open_result(1)
        expect(_server_card(page, "plex-e04")).to_be_visible(timeout=3000)
        held[0].fulfill(status=200, content_type="application/json", body=json.dumps(first))
        page.wait_for_timeout(500)

        expect(page.locator("#markersInspectorPath")).to_have_text(other)
        expect(_server_card(page, "plex-e04")).to_be_visible()
        expect(_server_card(page, "plex-1")).to_have_count(0)


_V1080 = "/data/movies/Heat (1995)/Heat (1995) - 1080p.mkv"
_V2160 = "/data/movies/Heat (1995)/Heat (1995) - 2160p.mkv"
# Plex: one item, every version shares its id. Jellyfin: each version's own id. Emby: each version its own item.
_VERSION_IDS = {
    "plex": ("4321", "4321"),
    "jellyfin": ("0123456789abcdef0123456789abcde1", "0123456789abcdef0123456789abcde2"),
    "emby": ("5001", "5002"),
}


@pytest.mark.e2e
class TestVersionNotOnThisDisk:
    """The owner's rule (2026-09-19): a version whose file isn't on this disk says so; another version never opens."""

    @pytest.mark.parametrize("server_type", ["plex", "jellyfin", "emby"])
    def test_a_version_whose_file_isnt_here_says_so(self, authed_page: Page, app_url: str, server_type) -> None:
        first_id, second_id = _VERSION_IDS[server_type]
        results = [
            {**_result(item_id=first_id, media_file=_V1080, title="Heat (1995)"), "type": "movie"},
            {
                **_result(item_id=second_id, media_file=_V2160, title="Heat (1995)"),
                "type": "movie",
                "preview_path": "",
                "preview_exists": False,
            },
        ]

        def answer(route: Route) -> None:
            if "path=" in route.request.url.replace("version_file=", ""):
                _fulfill_json(route, {"error": "Path is not a file inside any server library"}, status=400)
            else:
                body = {"error": "This version's file isn't on this disk", "reason": "version_not_here"}
                _fulfill_json(route, body, status=404)

        inspector = _Inspector(
            authed_page, app_url, south_park(), results=results, item_handler=answer, server_type=server_type
        )
        inspector.open_result(1)
        page = inspector.open_tab()

        box = page.locator("#markersInspectorBody .mk-version-not-here")
        expect(box).to_contain_text("This version's file isn't on this disk")
        expect(box).to_contain_text("Pick another version")
        expect(page.locator("#markersInspectorBody")).not_to_contain_text("Couldn't load")
        expect(page.locator("#markersInspectorBody .mk-window")).to_have_count(0)
        expect(page.locator("#markersRedetectBtn")).to_be_disabled()
        expect(page.locator("#markersInspectorPath")).to_have_text(_V2160)
        assert len(inspector.item_requests) == 2
        by_id = inspector.item_requests[1]
        assert f"item_id={second_id}" in by_id
        assert "version_file=" + _quote(_V2160) in by_id


@pytest.mark.e2e
class TestResultsWithoutAPreview:
    def test_a_result_without_a_preview_opens_the_inspector(self, authed_page: Page, app_url: str) -> None:
        no_preview = _result()
        no_preview.update(
            preview_path="", preview_exists=False, note="Plex hasn't analyzed this item yet — no bundle hash returned."
        )
        other = "/data/tv/South Park (1997)/Season 01/South Park S01E04.mkv"
        with_preview = _result(item_id="9999", media_file=other, title="South Park S01E04")
        inspector = _Inspector(authed_page, app_url, south_park(), results=[no_preview, with_preview])
        page = inspector.open_result(0)

        expect(page.locator("#framesNoPreview")).to_be_visible()
        expect(page.locator("#framesNoPreview")).to_contain_text("No preview yet")
        expect(page.locator("#framesNoPreview")).to_contain_text("Plex hasn't analyzed this item yet")
        expect(page.locator("#previewFrame")).to_be_hidden()
        expect(page.locator("#metaBadges")).to_contain_text("South Park S01E03")
        assert inspector.info_requests == []

        inspector.open_tab()
        assert len(inspector.item_requests) == 1
        assert "South%20Park%20S01E03.mkv" in inspector.item_requests[0]
        expect(_server_card(page, "plex-1").locator(".mk-plan")).to_have_text("Will replace")

        # A result with a preview still loads its frames as before.
        page.locator('button[data-bs-target="#inspector-tab-frames"]').click()
        inspector.open_result(1)
        expect(page.locator("#framesNoPreview")).to_be_hidden()
        expect(page.locator("#previewFrame")).to_be_visible()
        assert len(inspector.info_requests) == 1
        assert "index-sd.bif" in inspector.info_requests[0]


# ---------------------------------------------------------------------------
# Adjust / Lock editor (phase 4, Task 5)
# ---------------------------------------------------------------------------


def _saved(mtype: str, start: int, end: int, locked: bool = True) -> dict:
    return {
        "type": mtype,
        "start_ms": start,
        "end_ms": end,
        "locked": locked,
        "locked_at": "2026-09-20T10:00:00+00:00",
    }


def _row(server_id: str, name: str, stype: str, result: str, **extra) -> dict:
    """One row of POST /api/markers/item/markers (``api_markers._editor_server_row``)."""
    row = {
        "server_id": server_id,
        "server_name": name,
        "server_type": stype,
        "result": result,
        "message": "",
        "can_show": ["intro", "credits", "recap", "preview"] if stype == "jellyfin" else ["intro", "credits"],
        "cant_show": [],
        "notes": [],
        "replaced_own": [],
    }
    row.update(extra)
    return row


def locked_payload() -> dict:
    """South Park with both decided types locked by the user, as the Inspector reads it after a save."""
    payload = south_park()
    for mtype in ("intro", "credits"):
        decision = payload["decisions"][mtype]
        decision["reason"] = "locked by user"
        decision["marker"]["locked"] = True
        decision["marker"]["locked_at"] = "2026-09-20T10:00:00+00:00"
        decision["marker"]["decided_by"] = ["user"]
    return payload


def _strip(page: Page, mtype: str):
    return page.locator(f'.mk-edit-strip[data-edit-type="{mtype}"]')


def _field(page: Page, mtype: str, edge: str):
    return _strip(page, mtype).locator(".mk-time").nth(0 if edge == "start" else 1)


def _handle(page: Page, window: str, edge: str):
    return page.locator(f'.mk-window[data-window="{window}"] .mk-bar-editing .mk-handle-{edge}')


def _save_button(page: Page):
    return page.locator(".mk-edit-actions .mk-edit-save")


def _open_editor(inspector: _Inspector) -> Page:
    inspector.open_result()
    page = inspector.open_tab()
    page.locator("#markersAdjustBtn").click()
    expect(page.locator(".mk-edit-actions")).to_be_visible()
    return page


@pytest.mark.e2e
class TestAdjustEditor:
    def test_adjust_opens_the_editor_over_both_windows(self, authed_page: Page, app_url: str) -> None:
        inspector = _Inspector(authed_page, app_url, south_park())
        page = _open_editor(inspector)

        expect(page.locator("#markersInspectorBody")).to_contain_text(
            "Adjusting this episode. Nothing changes on your servers until you save."
        )
        # One strip per adjustable type, in the window that shows it; recap and preview have nothing to drag here.
        expect(page.locator(".mk-edit-strip")).to_have_count(2)
        expect(page.locator('.mk-window[data-window="opening"] .mk-edit-strip')).to_have_count(1)
        expect(page.locator('.mk-window[data-window="ending"] .mk-edit-strip')).to_have_count(1)
        expect(_strip(page, "intro")).to_contain_text("Intro")
        expect(_field(page, "intro", "start")).to_have_value("0:11")
        expect(_field(page, "intro", "end")).to_have_value("0:37")
        expect(_strip(page, "intro")).to_contain_text("26 seconds long")
        expect(_strip(page, "intro")).to_contain_text("Arrow keys move it 1 second")
        expect(_strip(page, "intro")).to_contain_text("hold Shift for 10 seconds")

        # Credits already run to the end of the file, so the switch starts on and the end box is closed.
        expect(_strip(page, "credits")).to_contain_text("Runs to the end of the file")
        expect(_strip(page, "credits").locator("input[role='switch']")).to_be_checked()
        expect(_field(page, "credits", "end")).to_be_disabled()

        expect(page.locator(".mk-edit-pending")).to_have_text("Intro 0:11–0:37 · Credits 21:39 →")
        expect(page.locator(".mk-edit-actions")).to_contain_text(
            "Saving keeps your times — later checks won't change them."
        )
        expect(_save_button(page)).to_have_text("Save and publish to 3 servers")
        # Nothing reaches a server until Save is pressed.
        assert inspector.save_bodies == []
        # While an edit is open the other two actions are out of reach.
        expect(page.locator("#markersAdjustBtn")).to_be_disabled()
        expect(page.locator("#markersLockBtn")).to_be_disabled()
        expect(page.locator("#markersRedetectBtn")).to_be_disabled()

    def test_the_handles_are_named_for_a_screen_reader_and_follow_the_times(
        self, authed_page: Page, app_url: str
    ) -> None:
        inspector = _Inspector(authed_page, app_url, south_park())
        page = _open_editor(inspector)

        start = _handle(page, "opening", "start")
        expect(start).to_have_attribute("aria-label", "Intro start, 0 minutes 11 seconds")
        expect(_handle(page, "opening", "end")).to_have_attribute("aria-label", "Intro end, 0 minutes 37 seconds")
        expect(_handle(page, "ending", "start")).to_have_attribute("aria-label", "Credits start, 21 minutes 39 seconds")
        start.press("ArrowRight")
        expect(start).to_have_attribute("aria-label", "Intro start, 0 minutes 12 seconds")

    def test_arrow_keys_nudge_a_second_and_shift_ten(self, authed_page: Page, app_url: str) -> None:
        inspector = _Inspector(authed_page, app_url, south_park())
        page = _open_editor(inspector)

        start = _handle(page, "opening", "start")
        start.press("ArrowRight")
        expect(_field(page, "intro", "start")).to_have_value("0:12")
        start.press("Shift+ArrowRight")
        expect(_field(page, "intro", "start")).to_have_value("0:22")
        start.press("ArrowLeft")
        expect(_field(page, "intro", "start")).to_have_value("0:21")
        expect(_strip(page, "intro")).to_contain_text("16 seconds long")
        expect(page.locator(".mk-edit-pending")).to_contain_text("Intro 0:21–0:37")

        _handle(page, "opening", "end").press("Shift+ArrowLeft")
        expect(_field(page, "intro", "end")).to_have_value("0:27")
        # The page's own arrow keys step the preview frame; a nudge must not also do that.
        expect(page.locator("#currentFrame")).to_have_text("0")

    def test_the_whole_edit_without_a_mouse(self, authed_page: Page, app_url: str) -> None:
        inspector = _Inspector(authed_page, app_url, south_park())
        inspector.save_answer = (200, {"markers": {"intro": _saved("intro", 20_000, 37_000)}, "servers": []})
        inspector.open_result()
        page = inspector.open_tab()
        # Keyboard only: the button is activated with Enter, the handle nudged, Save pressed with Enter.
        page.locator("#markersAdjustBtn").press("Enter")
        expect(page.locator(".mk-edit-actions")).to_be_visible()
        _handle(page, "opening", "start").press("Shift+ArrowRight")
        _handle(page, "opening", "start").press("ArrowLeft")
        expect(_field(page, "intro", "start")).to_have_value("0:20")

        with page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/api/markers/item/markers"), timeout=5000
        ):
            _save_button(page).press("Enter")

        assert len(inspector.save_bodies) == 1
        body = inspector.save_bodies[0]
        assert body["path"] == _MEDIA_FILE
        assert body["markers"] == [
            {"type": "intro", "start_ms": 20_000, "end_ms": 37_000},
            # Credits run to the end of the file, so no end is sent at all — the API reads null as "to the end".
            {"type": "credits", "start_ms": 1_299_000, "end_ms": None},
        ]

    def test_dragging_a_handle_moves_the_marker_and_that_is_what_gets_saved(
        self, authed_page: Page, app_url: str
    ) -> None:
        inspector = _Inspector(authed_page, app_url, south_park())
        page = _open_editor(inspector)

        handle = _handle(page, "opening", "start")
        handle.scroll_into_view_if_needed()
        box = handle.bounding_box()
        assert box is not None
        middle = box["y"] + box["height"] / 2
        page.mouse.move(box["x"] + box["width"] / 2, middle)
        page.mouse.down()
        page.mouse.move(box["x"] + box["width"] / 2 + 30, middle, steps=6)
        page.mouse.up()

        dragged = _field(page, "intro", "start").input_value()
        assert dragged != "0:11", "the drag moved nothing"
        expect(page.locator(".mk-edit-pending")).to_contain_text(f"Intro {dragged}–0:37")

        with page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/api/markers/item/markers"), timeout=5000
        ):
            _save_button(page).click()
        minutes, seconds = dragged.split(":")
        sent = next(m for m in inspector.save_bodies[0]["markers"] if m["type"] == "intro")
        assert sent["start_ms"] == (int(minutes) * 60 + int(seconds)) * 1000
        assert 11_000 < sent["start_ms"] < 36_000

    def test_typed_times_are_read_and_the_two_refusals_hold_save_back(self, authed_page: Page, app_url: str) -> None:
        inspector = _Inspector(authed_page, app_url, south_park())
        page = _open_editor(inspector)

        _field(page, "intro", "start").fill("1:05")
        expect(page.locator(".mk-edit-pending")).to_contain_text("Intro 1:05–0:37")
        expect(_strip(page, "intro")).to_contain_text("The end has to come after the start.")
        expect(_field(page, "intro", "start")).to_have_class(re.compile(r"\bis-invalid\b"))
        expect(_save_button(page)).to_be_disabled()

        _field(page, "intro", "start").fill("0:05")
        expect(_save_button(page)).to_be_enabled()
        _field(page, "intro", "end").fill("25:00")
        expect(_strip(page, "intro")).to_contain_text("That's past the end of the file (22:02).")
        expect(_field(page, "intro", "end")).to_have_class(re.compile(r"\bis-invalid\b"))
        expect(_save_button(page)).to_be_disabled()
        assert inspector.save_bodies == []

    def test_an_unusual_marker_warns_and_still_saves(self, authed_page: Page, app_url: str) -> None:
        # P-R2: every bound but "inside the file" and "ends after it starts" is advice, not a refusal.
        inspector = _Inspector(authed_page, app_url, south_park())
        page = _open_editor(inspector)

        _field(page, "intro", "end").fill("0:13")
        expect(_strip(page, "intro")).to_contain_text("That's shorter than most intros. This will still be saved.")
        expect(_save_button(page)).to_be_enabled()
        # The end moves out first: a start past the end would be a refusal, and a refusal hides the advice.
        _field(page, "intro", "end").fill("21:00")
        expect(_strip(page, "intro")).to_contain_text("That's longer than most intros. This will still be saved.")
        _field(page, "intro", "start").fill("15:00")
        expect(_strip(page, "intro")).to_contain_text(
            "That's later in the file than intros usually are. This will still be saved."
        )
        expect(_save_button(page)).to_be_enabled()

        # Credits that stop before the file does: the switch goes off and the warning appears, save still allowed.
        _strip(page, "credits").locator("input[role='switch']").uncheck()
        expect(_field(page, "credits", "end")).to_be_enabled()
        _field(page, "credits", "end").fill("21:50")
        expect(_strip(page, "credits")).to_contain_text(
            "Credits usually run to the end of the file. This will still be saved."
        )
        _field(page, "credits", "start").fill("5:00")
        expect(_strip(page, "credits")).to_contain_text(
            "That's earlier than credits usually start. This will still be saved."
        )

        with page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/api/markers/item/markers"), timeout=5000
        ):
            _save_button(page).click()
        assert inspector.save_bodies[0]["markers"] == [
            {"type": "intro", "start_ms": 900_000, "end_ms": 1_260_000},
            {"type": "credits", "start_ms": 300_000, "end_ms": 1_310_000},
        ]

    def test_cancel_puts_the_tab_back_and_sends_nothing(self, authed_page: Page, app_url: str) -> None:
        inspector = _Inspector(authed_page, app_url, south_park())
        page = _open_editor(inspector)
        _handle(page, "opening", "start").press("Shift+ArrowRight")
        expect(_field(page, "intro", "start")).to_have_value("0:21")

        page.locator(".mk-edit-cancel").click()

        expect(page.locator(".mk-edit-actions")).to_have_count(0)
        expect(page.locator(".mk-edit-strip")).to_have_count(0)
        expect(page.locator("#markersInspectorBody")).not_to_contain_text("Adjusting this episode")
        expect(_lane(page, "opening", "Decision")).to_contain_text("Intro 0:11–0:37")
        expect(page.locator("#markersAdjustBtn")).to_be_enabled()
        expect(page.locator("#markersRedetectBtn")).to_be_enabled()
        assert inspector.save_bodies == []

    def test_a_movie_adjusts_only_its_ending(self, authed_page: Page, app_url: str) -> None:
        inspector = _Inspector(authed_page, app_url, movie())
        page = _open_editor(inspector)
        expect(page.locator("#markersInspectorBody")).to_contain_text("Adjusting this movie.")
        expect(page.locator(".mk-edit-strip")).to_have_count(1)
        expect(_field(page, "credits", "start")).to_have_value("1:55:00")
        expect(_save_button(page)).to_have_text("Save and publish to 1 server")

    def test_a_file_no_job_has_looked_at_cant_be_adjusted(self, authed_page: Page, app_url: str) -> None:
        """No length means no timeline, so there is nothing to drag *and* nowhere to add one."""
        inspector = _Inspector(authed_page, app_url, not_checked())
        inspector.open_result()
        page = inspector.open_tab()
        expect(page.locator("#markersAdjustBtn")).to_be_disabled()
        expect(page.locator("#markersLockBtn")).to_be_disabled()
        expect(page.locator("#markersRedetectBtn")).to_be_enabled()

    def test_a_save_that_fails_keeps_the_edit_on_screen(self, authed_page: Page, app_url: str) -> None:
        inspector = _Inspector(authed_page, app_url, south_park())
        inspector.save_answer = (409, {"error": "This file changed on disk since it was analysed; re-detect it first."})
        page = _open_editor(inspector)
        _handle(page, "opening", "start").press("ArrowRight")

        _save_button(page).click()

        expect(page.locator("#toastNotification")).to_contain_text("changed on disk", timeout=5000)
        expect(page.locator(".mk-edit-actions")).to_be_visible()
        expect(_field(page, "intro", "start")).to_have_value("0:12")
        expect(_save_button(page)).to_be_enabled()
        expect(page.locator(".mk-saved")).to_have_count(0)


@pytest.mark.e2e
class TestTypesAServerCantShow:
    """Owner ruling: a type one enabled server shows stays editable; only "nobody shows it" refuses the edit."""

    @staticmethod
    def _with_recap(server_types: list[str]) -> dict:
        payload = south_park()
        payload["decisions"]["recap"] = _decision("decided", ("recap", 0, 40_000))
        payload["servers"] = [
            _server(f"s-{i}", stype.title(), stype, "will_add", []) for i, stype in enumerate(server_types)
        ]
        return payload

    def test_a_recap_a_jellyfin_can_show_is_editable_and_names_who_misses_out(
        self, authed_page: Page, app_url: str
    ) -> None:
        inspector = _Inspector(authed_page, app_url, self._with_recap(["plex", "emby", "jellyfin"]))
        page = _open_editor(inspector)

        expect(_field(page, "recap", "start")).to_be_enabled()
        expect(_strip(page, "recap")).to_contain_text(
            "Only Jellyfin shows recaps. Plex and Emby have no recap marker, so this one won't reach them."
        )
        expect(page.locator(".mk-edit-pending")).to_contain_text("Recap 0:00–0:40")
        expect(_save_button(page)).to_be_enabled()

    def test_a_recap_no_server_can_show_is_refused_without_refusing_the_rest(
        self, authed_page: Page, app_url: str
    ) -> None:
        inspector = _Inspector(authed_page, app_url, self._with_recap(["plex", "emby"]))
        page = _open_editor(inspector)

        expect(_field(page, "recap", "start")).to_be_disabled()
        expect(_field(page, "recap", "end")).to_be_disabled()
        expect(_strip(page, "recap")).to_contain_text(
            "Recaps can't be adjusted here: neither Plex nor Emby has a recap marker, "
            "and no other server has this file."
        )
        # The intro and credits on the same screen are still perfectly editable, and the recap is left out.
        expect(_field(page, "intro", "start")).to_be_enabled()
        expect(page.locator(".mk-edit-pending")).not_to_contain_text("Recap")
        expect(_save_button(page)).to_have_text("Save and publish to 2 servers")

        with page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/api/markers/item/markers"), timeout=5000
        ):
            _save_button(page).click()
        assert [m["type"] for m in inspector.save_bodies[0]["markers"]] == ["intro", "credits"]

    def test_no_server_has_intro_and_credits_on_so_nothing_can_be_saved(self, authed_page: Page, app_url: str) -> None:
        payload = south_park()
        for server in payload["servers"]:
            server["markers_enabled"] = False
        inspector = _Inspector(authed_page, app_url, payload)
        page = _open_editor(inspector)

        expect(_strip(page, "intro")).to_contain_text(
            "Intros can't be adjusted here: no server with Intro & Credits on has this file."
        )
        expect(_field(page, "intro", "start")).to_be_disabled()
        expect(_save_button(page)).to_have_text("Save")
        expect(_save_button(page)).to_be_disabled()
        expect(page.locator(".mk-edit-pending")).to_have_text("Nothing to save yet")


def every_result() -> dict:
    """An intro-and-credits file with one server per result the save can answer with."""
    payload = south_park()
    payload["decisions"]["credits"] = _decision("decided", ("credits", 1_250_000, 1_280_000))
    payload["servers"] = [
        _server("plex-main", "Plex · Main", "plex", "will_replace", [], version_count=2),
        _server("emby-lab", "Emby · Lab", "emby", "will_add", []),
        _server("jf-lab", "Jellyfin · Lab", "jellyfin", "will_add", []),
        _server("plex-parents", "Plex · Parents", "plex", "not_enabled", [], markers_enabled=False),
        _server("jf-wait", "Jellyfin · Attic", "jellyfin", "waiting", []),
        _server("jf-same", "Jellyfin · Shed", "jellyfin", "up_to_date", []),
        _server("jf-review", "Jellyfin · Loft", "jellyfin", "will_add", []),
    ]
    return payload


def every_result_answer() -> dict:
    """The save's answer. Each row is the API's own words for that server (``_editor_server_row``)."""
    return {
        "canonical_path": _MEDIA_FILE,
        "duration_ms": _DURATION,
        "markers": {
            "intro": _saved("intro", 11_000, 37_000),
            "credits": _saved("credits", 1_250_000, 1_280_000),
        },
        "servers": [
            _row("plex-main", "Plex · Main", "plex", "written", replaced_own=["intro"], message="2 marker(s)."),
            _row(
                "emby-lab",
                "Emby · Lab",
                "emby",
                "written",
                notes=[{"type": "credits", "field": "end", "note": "Emby skips to the end of the file"}],
            ),
            _row("jf-lab", "Jellyfin · Lab", "jellyfin", "failed", message="HTTP 500 from the plugin"),
            _row("plex-parents", "Plex · Parents", "plex", "not_enabled", message="Intro & Credits is off"),
            _row("jf-wait", "Jellyfin · Attic", "jellyfin", "waiting", message="Not in this server's library yet"),
            _row("jf-same", "Jellyfin · Shed", "jellyfin", "unchanged"),
            _row("jf-review", "Jellyfin · Loft", "jellyfin", "needs_review", message="Sources disagree on credits"),
        ],
    }


def _with_types(types: list[str]) -> dict:
    """South Park plus the extra types, with a Jellyfin on hand so they are all adjustable."""
    payload = south_park()
    payload["decisions"]["credits"] = _decision("decided", ("credits", 1_250_000, 1_280_000))
    if "recap" in types:
        payload["decisions"]["recap"] = _decision("decided", ("recap", 0, 40_000))
    if "preview" in types:
        payload["decisions"]["preview"] = _decision("decided", ("preview", 1_300_000, _DURATION))
    payload["servers"] = [
        _server("jf-lab", "Jellyfin · Lab", "jellyfin", "will_add", []),
        _server("emby-den", "Emby · Den", "emby", "will_add", []),
        _server("plex-den", "Plex · Den", "plex", "nothing_to_publish", []),
    ]
    return payload


@pytest.mark.e2e
class TestSaveAndItsResults:
    def test_saving_shows_every_server_result_and_the_locked_state(self, authed_page: Page, app_url: str) -> None:
        inspector = _Inspector(authed_page, app_url, every_result())
        inspector.save_answer = (200, every_result_answer())
        page = _open_editor(inspector)

        with page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/api/markers/item/markers"), timeout=5000
        ):
            _save_button(page).click()

        expect(page.locator(".mk-saved")).to_contain_text("Saved and locked. Your times stay until you unlock them.")
        # The editor closes on a save: nothing is left to drag or cancel.
        expect(page.locator(".mk-edit-actions")).to_have_count(0)
        expect(page.locator(".mk-edit-strip")).to_have_count(0)

        expected = {
            "plex-main": (
                "Updated",
                [
                    "Intro 0:11–0:37 · Credits 20:50–21:20",
                    "Replaced Plex's own marker. This server is set to keep Plex's, "
                    "but a marker you adjust always wins.",
                    "All versions of this item share one set of markers",
                ],
            ),
            "emby-lab": (
                "Updated",
                [
                    "Intro 0:11–0:37 · Credits 20:50",
                    "Your credits end wasn't sent. Emby skips to the end of the file, "
                    "past any scene after the credits.",
                ],
            ),
            "jf-lab": (
                "Couldn't reach it",
                ["Your times are saved. This server gets them at the next Check servers run."],
            ),
            "plex-parents": ("Intro & Credits off", ["Turn on Intro & Credits for this server to send markers here."]),
            "jf-wait": ("Waiting", ["Not in this server's library yet"]),
            "jf-same": ("Up to date", ["Intro 0:11–0:37 · Credits 20:50–21:20"]),
            "jf-review": ("Needs review", ["Sources disagree on credits"]),
        }
        for server_id, (badge, lines) in expected.items():
            card = _server_card(page, server_id)
            expect(card.locator(".mk-plan")).to_have_text(badge)
            for line in lines:
                expect(card).to_contain_text(line)
        # The publisher's own row message is job wording; the card shows the editor's words, not "2 marker(s).".
        expect(_server_card(page, "plex-main")).not_to_contain_text("marker(s)")
        # Only Emby's credits end goes missing; nobody else says so.
        expect(_server_card(page, "jf-same")).not_to_contain_text("credits end wasn't sent")
        # A server that took everything says nothing about types it couldn't show.
        expect(_server_card(page, "jf-same")).not_to_contain_text("has no")

        # The tab now reads as locked, and the header offers Unlock instead of Lock.
        expect(page.locator(".mk-chips")).to_contain_text("Locked by you")
        expect(page.locator("#markersLockBtn")).to_have_text("Unlock")
        expect(page.locator(".mk-lane-decision .mk-lock")).to_have_count(2)

    def test_a_server_that_took_some_types_says_which_one_it_couldnt(self, authed_page: Page, app_url: str) -> None:
        inspector = _Inspector(authed_page, app_url, _with_types(["recap"]))
        inspector.save_answer = (
            200,
            {
                "markers": {
                    "intro": _saved("intro", 11_000, 37_000),
                    "recap": _saved("recap", 0, 40_000),
                    "credits": _saved("credits", 1_250_000, 1_280_000),
                },
                "servers": [
                    _row("jf-lab", "Jellyfin · Lab", "jellyfin", "written"),
                    _row("emby-den", "Emby · Den", "emby", "written", cant_show=["recap"]),
                    _row("plex-den", "Plex · Den", "plex", "written", cant_show=["recap"]),
                ],
            },
        )
        page = _open_editor(inspector)
        with page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/api/markers/item/markers"), timeout=5000
        ):
            _save_button(page).click()

        emby = _server_card(page, "emby-den")
        expect(emby.locator(".mk-plan")).to_have_text("Recap not sent")
        expect(emby).to_contain_text("Emby has no recap marker. Its intro and credits were updated.")
        expect(emby).to_contain_text("Intro 0:11–0:37 · Credits 20:50–21:20")
        expect(_server_card(page, "jf-lab").locator(".mk-plan")).to_have_text("Updated")
        expect(_server_card(page, "jf-lab")).to_contain_text("Recap 0:00–0:40")

    def test_a_server_that_took_nothing_at_all_says_so(self, authed_page: Page, app_url: str) -> None:
        inspector = _Inspector(authed_page, app_url, _with_types(["recap", "preview"]))
        inspector.save_answer = (
            200,
            {
                "markers": {
                    "intro": _saved("intro", 11_000, 37_000),
                    "recap": _saved("recap", 0, 40_000),
                    "credits": _saved("credits", 1_250_000, 1_280_000),
                    "preview": _saved("preview", 1_300_000, _DURATION),
                },
                "servers": [
                    _row("jf-lab", "Jellyfin · Lab", "jellyfin", "written"),
                    _row("emby-den", "Emby · Den", "emby", "written", cant_show=["recap", "preview"]),
                    _row("plex-den", "Plex · Den", "plex", "nothing_to_publish", cant_show=["recap", "preview"]),
                ],
            },
        )
        page = _open_editor(inspector)
        with page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/api/markers/item/markers"), timeout=5000
        ):
            _save_button(page).click()

        plex = _server_card(page, "plex-den")
        expect(plex.locator(".mk-plan")).to_have_text("Nothing sent")
        expect(plex).to_contain_text("Plex has no recap or preview marker, and nothing else changed.")
        emby = _server_card(page, "emby-den")
        expect(emby.locator(".mk-plan")).to_have_text("Recap or preview not sent")
        expect(emby).to_contain_text("Emby has no recap or preview marker. Its intro and credits were updated.")

    def test_the_cards_say_sending_while_the_save_is_in_flight(self, authed_page: Page, app_url: str) -> None:
        inspector = _Inspector(authed_page, app_url, south_park())
        held: list[Route] = []
        authed_page.route("**/api/markers/item/markers", lambda route: held.append(route))
        page = _open_editor(inspector)

        _save_button(page).click()

        expect(page.locator(".mk-edit-actions")).to_contain_text("Your times are saved. Sending them to your servers…")
        expect(_save_button(page)).to_contain_text("Saving…")
        expect(_save_button(page)).to_be_disabled()
        for server_id in ("plex-1", "jf-1", "emby-1"):
            expect(_server_card(page, server_id).locator(".mk-plan")).to_contain_text("Sending…")
        # The times can't be moved while they are on their way.
        expect(_field(page, "intro", "start")).to_be_disabled()
        expect(_handle(page, "opening", "start")).to_be_disabled()

        held[0].fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"markers": {"intro": _saved("intro", 11_000, 37_000)}, "servers": []}),
        )
        expect(page.locator(".mk-saved")).to_be_visible(timeout=5000)


@pytest.mark.e2e
class TestLockAndUnlock:
    def test_lock_sends_the_decided_times_unchanged(self, authed_page: Page, app_url: str) -> None:
        inspector = _Inspector(authed_page, app_url, south_park())
        inspector.save_answer = (
            200,
            {
                "markers": {
                    "intro": _saved("intro", 11_000, 37_000),
                    "credits": _saved("credits", 1_299_000, _DURATION),
                },
                "servers": [_row("plex-1", "Plex", "plex", "unchanged")],
            },
        )
        inspector.open_result()
        page = inspector.open_tab()
        expect(page.locator("#markersLockBtn")).to_have_text("Lock")

        with page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/api/markers/item/markers"), timeout=5000
        ):
            page.locator("#markersLockBtn").click()

        assert inspector.save_bodies == [
            {
                "path": _MEDIA_FILE,
                "markers": [
                    {"type": "intro", "start_ms": 11_000, "end_ms": 37_000},
                    {"type": "credits", "start_ms": 1_299_000, "end_ms": None},
                ],
            }
        ]
        expect(page.locator("#toastNotification")).to_contain_text(
            "Locked — these times stay until you unlock them.", timeout=5000
        )
        expect(page.locator(".mk-chips")).to_contain_text("Locked by you")
        expect(page.locator("#markersLockBtn")).to_have_text("Unlock")

    def test_lock_leaves_out_a_type_no_server_can_show(self, authed_page: Page, app_url: str) -> None:
        # The API refuses the whole save over one unshowable type, so Lock has to filter the same way Adjust does:
        # a recap is decided whatever vendors the user runs, and neither Plex nor Emby has a recap marker.
        payload = south_park()
        payload["decisions"]["recap"] = _decision("decided", ("recap", 0, 40_000))
        payload["servers"] = [
            _server("plex-1", "Plex", "plex", "will_add", []),
            _server("emby-1", "Emby", "emby", "will_add", []),
        ]
        inspector = _Inspector(authed_page, app_url, payload)
        inspector.save_answer = (200, {"markers": {}, "servers": []})
        inspector.open_result()
        page = inspector.open_tab()

        with page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/api/markers/item/markers"), timeout=5000
        ):
            page.locator("#markersLockBtn").click()

        assert [m["type"] for m in inspector.save_bodies[0]["markers"]] == ["intro", "credits"]

    def test_lock_is_out_of_reach_when_no_server_could_take_the_markers(self, authed_page: Page, app_url: str) -> None:
        payload = south_park()
        for server in payload["servers"]:
            server["markers_enabled"] = False
        inspector = _Inspector(authed_page, app_url, payload)
        inspector.open_result()
        page = inspector.open_tab()

        expect(page.locator("#markersLockBtn")).to_be_disabled()
        assert inspector.save_bodies == []

    def test_adjust_waits_while_a_lock_is_in_flight(self, authed_page: Page, app_url: str) -> None:
        inspector = _Inspector(authed_page, app_url, south_park())
        held: list[Route] = []
        authed_page.route("**/api/markers/item/markers", lambda route: held.append(route))
        inspector.open_result()
        page = inspector.open_tab()

        page.locator("#markersLockBtn").click()

        # The lock publishes to every server; starting an edit meanwhile would be thrown away by the answer.
        expect(page.locator("#markersAdjustBtn")).to_be_disabled()
        expect(page.locator("#markersLockBtn")).to_be_disabled()
        expect(page.locator("#markersRedetectBtn")).to_be_disabled()

        held[0].fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"markers": {"intro": _saved("intro", 11_000, 37_000)}, "servers": []}),
        )
        expect(page.locator("#markersAdjustBtn")).to_be_enabled(timeout=5000)
        expect(page.locator("#markersLockBtn")).to_have_text("Unlock")

    def test_a_lock_answering_after_the_file_changed_applies_nothing_and_frees_the_buttons(
        self, authed_page: Page, app_url: str
    ) -> None:
        other = "/data/tv/South Park (1997)/Season 01/South Park S01E04.mkv"
        second = copy.deepcopy(south_park())
        second["canonical_path"] = other
        second["servers"] = [_server("plex-e04", "Plex E04", "plex", "up_to_date", [])]
        held: list[Route] = []
        inspector = _Inspector(
            authed_page,
            app_url,
            south_park(),
            results=[_result(), _result(item_id="9999", media_file=other, title="South Park S01E04")],
        )
        authed_page.route("**/api/markers/item/markers", lambda route: held.append(route))
        inspector.open_result(0)
        page = inspector.open_tab()
        page.locator("#markersLockBtn").click()
        expect(page.locator("#markersAdjustBtn")).to_be_disabled()

        # The user moves to another episode before the lock answers.
        inspector.payload = second
        inspector.open_result(1)
        expect(_server_card(page, "plex-e04")).to_be_visible(timeout=5000)

        held[0].fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"markers": {"intro": _saved("intro", 11_000, 37_000)}, "servers": []}),
        )

        # The other episode's answer neither locks this one nor leaves its buttons stuck.
        expect(page.locator("#markersAdjustBtn")).to_be_enabled(timeout=5000)
        expect(page.locator("#markersInspectorPath")).to_have_text(other)
        expect(page.locator(".mk-chips")).not_to_contain_text("Locked by you")
        expect(page.locator(".mk-saved")).to_have_count(0)

    def test_a_locked_file_opens_with_the_chip_the_lane_lock_and_unlock(self, authed_page: Page, app_url: str) -> None:
        inspector = _Inspector(authed_page, app_url, locked_payload())
        inspector.open_result()
        page = inspector.open_tab()

        expect(page.locator(".mk-chips")).to_contain_text("Locked by you")
        expect(page.locator('.mk-window[data-window="opening"] .mk-lane-decision .mk-lock')).to_have_count(1)
        expect(page.locator("#markersLockBtn")).to_have_text("Unlock")
        expect(page.locator("#markersAdjustBtn")).to_be_enabled()

    def test_unlock_says_what_comes_back_before_it_drops_the_lock(self, authed_page: Page, app_url: str) -> None:
        inspector = _Inspector(authed_page, app_url, locked_payload())
        inspector.unlock_answer = (200, {"unlocked": ["intro", "credits"], "markers": {}, "decisions": {}})
        inspector.open_result()
        page = inspector.open_tab()

        page.locator("#markersLockBtn").click()

        modal = page.locator("#markersUnlockModal")
        expect(modal).to_be_visible()
        expect(modal).to_contain_text("Unlock these markers?")
        expect(modal).to_contain_text("Your times stay on your servers for now:")
        expect(modal.locator("#markersUnlockList li")).to_have_text(["Intro 0:11–0:37", "Credits 21:39 →"])
        expect(modal).to_contain_text(
            "The next time this episode is checked, the sources decide again and may move them."
        )
        expect(modal).to_contain_text("Keep them locked")

        # The next read of the file has nothing locked any more.
        inspector.payload = south_park()
        with page.expect_response(
            lambda r: r.request.method == "DELETE" and r.url.endswith("/api/markers/item/markers"), timeout=5000
        ):
            page.locator("#markersUnlockConfirm").click()

        assert inspector.unlock_bodies == [{"path": _MEDIA_FILE, "types": ["intro", "credits"]}]
        expect(page.locator("#toastNotification")).to_contain_text(
            "Unlocked — the next check decides these times again.", timeout=5000
        )
        expect(page.locator(".mk-chips")).not_to_contain_text("Locked by you")
        expect(page.locator("#markersLockBtn")).to_have_text("Lock")

    def test_an_unlock_that_fails_keeps_the_lock_and_the_dialog(self, authed_page: Page, app_url: str) -> None:
        inspector = _Inspector(authed_page, app_url, locked_payload())
        inspector.unlock_answer = (409, {"error": "This file hasn't been analysed yet. Run Re-detect first."})
        inspector.open_result()
        page = inspector.open_tab()
        page.locator("#markersLockBtn").click()
        modal = page.locator("#markersUnlockModal")
        expect(modal).to_be_visible()

        with page.expect_response(
            lambda r: r.request.method == "DELETE" and r.url.endswith("/api/markers/item/markers"), timeout=5000
        ):
            page.locator("#markersUnlockConfirm").click()

        expect(page.locator("#toastNotification")).to_contain_text("hasn't been analysed yet", timeout=5000)
        expect(modal).to_be_visible()
        expect(page.locator(".mk-chips")).to_contain_text("Locked by you")
        expect(page.locator("#markersLockBtn")).to_have_text("Unlock")
        # The confirm button comes back, so the user can try again without reopening anything.
        expect(page.locator("#markersUnlockConfirm")).to_be_enabled()
        with page.expect_response(
            lambda r: r.request.method == "DELETE" and r.url.endswith("/api/markers/item/markers"), timeout=5000
        ):
            page.locator("#markersUnlockConfirm").click()
        assert len(inspector.unlock_bodies) == 2

    def test_keeping_them_locked_changes_nothing(self, authed_page: Page, app_url: str) -> None:
        inspector = _Inspector(authed_page, app_url, locked_payload())
        inspector.open_result()
        page = inspector.open_tab()
        page.locator("#markersLockBtn").click()
        modal = page.locator("#markersUnlockModal")
        expect(modal).to_be_visible()

        modal.locator("button", has_text="Keep them locked").click()

        expect(modal).to_be_hidden()
        assert inspector.unlock_bodies == []
        expect(page.locator(".mk-chips")).to_contain_text("Locked by you")


@pytest.mark.e2e
class TestEditorTimeHelpers:
    """The editor's pure time helpers, pinned in the browser — the project has no JS test runner."""

    @pytest.mark.parametrize(
        ("text", "ms"),
        [
            ("0:14", 14_000),
            ("40:55", 2_455_000),
            ("1:55:12", 6_912_000),
            ("90", 90_000),
            ("", None),
            ("0:75", None),
            ("abc", None),
            ("1:2:3:4", None),
        ],
    )
    def test_parse_clock_reads_a_time_or_refuses_it(self, authed_page: Page, app_url: str, text: str, ms) -> None:
        authed_page.goto(f"{app_url}/bif-viewer")
        authed_page.wait_for_load_state("domcontentloaded")
        assert authed_page.evaluate("t => window.markersEditor.parseClock(t)", text) == ms

    def test_length_and_spoken_time(self, authed_page: Page, app_url: str) -> None:
        authed_page.goto(f"{app_url}/bif-viewer")
        authed_page.wait_for_load_state("domcontentloaded")
        lengths = authed_page.evaluate(
            "() => [1000, 27000, 119000, 120000, 126000].map((ms) => window.markersEditor.lengthText(ms))"
        )
        assert lengths == [
            "1 second long",
            "27 seconds long",
            "119 seconds long",
            "2 minutes long",
            "2 minutes 6 seconds long",
        ]
        spoken = authed_page.evaluate("() => [14000, 6912000, 61000].map((ms) => window.markersEditor.spokenTime(ms))")
        assert spoken == ["0 minutes 14 seconds", "1 hour 55 minutes 12 seconds", "1 minute 1 second"]


# ---------------------------------------------------------------------------
# Adding a marker by hand (phase 4, built 2026-09-21 to the answers relayed with the go-ahead)
# ---------------------------------------------------------------------------

_STARTING_TIMES = "Starting times, not something we found — drag them to where they really are."


def nothing_found() -> dict:
    """South Park with the intro found and nothing else: credits, recap and preview all have no answer."""
    payload = south_park()
    payload["decisions"]["credits"] = _decision("no_evidence")
    payload["decisions"]["recap"] = _decision("no_evidence")
    payload["evidence"] = [_evidence("chapters", "intro", 11_000, 37_000)]
    return payload


def nothing_found_at_all() -> dict:
    payload = nothing_found()
    payload["decisions"]["intro"] = _decision("no_evidence")
    payload["evidence"] = []
    return payload


def _add(page: Page, mtype: str):
    return page.locator(f'.mk-add[data-add-type="{mtype}"]')


def _remove(page: Page, mtype: str):
    return _strip(page, mtype).locator(".mk-edit-remove")


def _tooltip(locator) -> str | None:
    """A Bootstrap tooltip moves the element's ``title`` into ``data-bs-original-title`` once it is initialised."""
    return locator.get_attribute("data-bs-original-title") or locator.get_attribute("title")


@pytest.mark.e2e
class TestAddAMarker:
    def test_a_type_with_nothing_found_offers_an_add_on_the_row_under_its_lane(
        self, authed_page: Page, app_url: str
    ) -> None:
        inspector = _Inspector(authed_page, app_url, nothing_found())
        page = _open_editor(inspector)

        # The intro was found, so it has a bar and a strip; the other three have an Add on the row under their
        # window's lane.
        expect(_strip(page, "intro")).to_be_visible()
        expect(page.locator(".mk-edit-strip")).to_have_count(1)
        expect(_add(page, "recap")).to_have_text("Add recap")
        expect(_add(page, "credits")).to_have_text("Add credits")
        expect(_add(page, "preview")).to_have_text("Add preview")
        expect(_add(page, "intro")).to_have_count(0)
        # Each Add sits in the window that shows that type.
        expect(page.locator('.mk-window[data-window="opening"] .mk-add')).to_have_count(1)
        expect(page.locator('.mk-window[data-window="ending"] .mk-add')).to_have_count(2)
        assert _tooltip(_add(page, "credits")) == (
            "Puts a marker on the timeline at a starting time. Drag it to where it really is, then save."
        )

    def test_adjust_opens_on_a_file_where_nothing_was_found_at_all(self, authed_page: Page, app_url: str) -> None:
        inspector = _Inspector(authed_page, app_url, nothing_found_at_all())
        inspector.open_result()
        page = inspector.open_tab()

        expect(page.locator("#markersAdjustBtn")).to_be_enabled()
        page.locator("#markersAdjustBtn").click()
        expect(page.locator(".mk-edit-actions")).to_be_visible()

        expect(page.locator(".mk-edit-strip")).to_have_count(0)
        expect(page.locator(".mk-add")).to_have_count(4)
        expect(page.locator(".mk-edit-pending")).to_have_text("Nothing to save yet")
        expect(_save_button(page)).to_have_text("Save")
        expect(_save_button(page)).to_be_disabled()
        assert inspector.save_bodies == []

    @pytest.mark.parametrize(
        ("mtype", "start", "end", "length"),
        [
            ("intro", "0:00", "0:30", "30 seconds long"),
            ("recap", "0:00", "0:30", "30 seconds long"),
            ("credits", "21:02", "22:02", "60 seconds long"),
            ("preview", "21:32", "22:02", "30 seconds long"),
        ],
    )
    def test_each_type_starts_from_its_own_deliberate_times(
        self, authed_page: Page, app_url: str, mtype: str, start: str, end: str, length: str
    ) -> None:
        """Round by design (addSeed): the file is 22:02, so a tail seed lands on an obviously chosen boundary."""
        inspector = _Inspector(authed_page, app_url, nothing_found_at_all())
        page = _open_editor(inspector)

        _add(page, mtype).click()
        expect(_strip(page, mtype)).to_be_visible()
        expect(_field(page, mtype, "start")).to_have_value(start)
        expect(_field(page, mtype, "end")).to_have_value(end)
        expect(_strip(page, mtype)).to_contain_text(length)
        expect(_strip(page, mtype)).to_contain_text(_STARTING_TIMES)
        # The offer is gone now that the marker is on the timeline.
        expect(_add(page, mtype)).to_have_count(0)

    @pytest.mark.parametrize("mtype", ["credits", "preview"])
    def test_a_tail_seed_runs_to_the_end_of_the_file(self, authed_page: Page, app_url: str, mtype: str) -> None:
        inspector = _Inspector(authed_page, app_url, nothing_found_at_all())
        page = _open_editor(inspector)

        _add(page, mtype).click()
        expect(_strip(page, mtype).locator("input[role='switch']")).to_be_checked()
        expect(_field(page, mtype, "end")).to_be_disabled()
        # No "credits usually run to the end" warning, and no "earlier than credits usually start" either.
        expect(_strip(page, mtype)).not_to_contain_text("This will still be saved.")

    def test_the_starting_times_line_goes_as_soon_as_an_edge_moves(self, authed_page: Page, app_url: str) -> None:
        inspector = _Inspector(authed_page, app_url, nothing_found_at_all())
        page = _open_editor(inspector)

        _add(page, "intro").click()
        expect(_strip(page, "intro")).to_contain_text(_STARTING_TIMES)

        _handle(page, "opening", "start").press("ArrowRight")

        expect(_field(page, "intro", "start")).to_have_value("0:01")
        expect(_strip(page, "intro")).not_to_contain_text(_STARTING_TIMES)

    def test_a_nudge_a_bound_swallows_leaves_the_starting_times_line_saying_the_truth(
        self, authed_page: Page, app_url: str
    ) -> None:
        """The intro seed already starts at 0:00, so ArrowLeft clamps back to 0 and nothing actually moved."""
        inspector = _Inspector(authed_page, app_url, nothing_found_at_all())
        page = _open_editor(inspector)

        _add(page, "intro").click()
        _handle(page, "opening", "start").press("ArrowLeft")

        expect(_field(page, "intro", "start")).to_have_value("0:00")
        expect(_strip(page, "intro")).to_contain_text(_STARTING_TIMES)

    def test_typing_a_time_also_takes_the_starting_times_line_away(self, authed_page: Page, app_url: str) -> None:
        inspector = _Inspector(authed_page, app_url, nothing_found_at_all())
        page = _open_editor(inspector)

        _add(page, "credits").click()
        expect(_strip(page, "credits")).to_contain_text(_STARTING_TIMES)

        _field(page, "credits", "start").fill("21:10")

        expect(_field(page, "credits", "start")).to_have_value("21:10")
        expect(_strip(page, "credits")).not_to_contain_text(_STARTING_TIMES)

    def test_typing_into_the_end_box_also_takes_the_starting_times_line_away(
        self, authed_page: Page, app_url: str
    ) -> None:
        """An added intro's end box is open (no "runs to the end" switch), so it is the other typed edge."""
        inspector = _Inspector(authed_page, app_url, nothing_found_at_all())
        page = _open_editor(inspector)

        _add(page, "intro").click()
        _field(page, "intro", "end").fill("0:45")

        expect(_field(page, "intro", "end")).to_have_value("0:45")
        expect(_strip(page, "intro")).to_contain_text("45 seconds long")
        expect(_strip(page, "intro")).not_to_contain_text(_STARTING_TIMES)

    def test_typing_something_that_is_not_a_time_leaves_the_starting_times_line_up(
        self, authed_page: Page, app_url: str
    ) -> None:
        """Nothing moved -- the model keeps its last good value -- so the disclaimer is still true."""
        inspector = _Inspector(authed_page, app_url, nothing_found_at_all())
        page = _open_editor(inspector)

        _add(page, "intro").click()
        _field(page, "intro", "start").fill("abc")

        expect(_field(page, "intro", "start")).to_have_class(re.compile(r"\bis-invalid\b"))
        expect(_strip(page, "intro")).to_contain_text(_STARTING_TIMES)
        expect(_save_button(page)).to_be_disabled()

    def test_the_runs_to_the_end_switch_counts_as_a_move_even_on_the_same_millisecond(
        self, authed_page: Page, app_url: str
    ) -> None:
        """Turning it off changes what the save sends (a time instead of null), so the disclaimer stops being true."""
        inspector = _Inspector(authed_page, app_url, nothing_found_at_all())
        page = _open_editor(inspector)

        _add(page, "credits").click()
        expect(_field(page, "credits", "end")).to_have_value("22:02")
        expect(_strip(page, "credits")).to_contain_text(_STARTING_TIMES)

        _strip(page, "credits").locator("input[role='switch']").uncheck()

        # The end lands on the same millisecond it already had; only what gets sent changed.
        expect(_field(page, "credits", "end")).to_have_value("22:02")
        expect(_strip(page, "credits")).not_to_contain_text(_STARTING_TIMES)

    def test_add_then_adjust_then_save_shows_what_each_server_did(self, authed_page: Page, app_url: str) -> None:
        """The whole path: nothing found, added by hand, dragged, saved, and one row per server."""
        inspector = _Inspector(authed_page, app_url, nothing_found())
        inspector.save_answer = (
            200,
            {
                "canonical_path": _MEDIA_FILE,
                "duration_ms": _DURATION,
                "markers": {
                    "intro": _saved("intro", 11_000, 37_000),
                    "credits": _saved("credits", 1_272_000, _DURATION),
                },
                "servers": [
                    _row("plex-1", "Plex", "plex", "written", message="2 marker(s)."),
                    _row("jf-1", "Jellyfin", "jellyfin", "failed", message="Can't reach this server"),
                    _row("emby-1", "Emby", "emby", "written", message="2 marker(s)."),
                ],
            },
        )
        page = _open_editor(inspector)

        _add(page, "credits").click()
        expect(_field(page, "credits", "start")).to_have_value("21:02")
        # Drag it 10 s earlier with the keyboard, the way a user fixes a starting time.
        _handle(page, "ending", "start").press("Shift+ArrowLeft")
        expect(_field(page, "credits", "start")).to_have_value("20:52")
        expect(page.locator(".mk-edit-pending")).to_have_text("Intro 0:11–0:37 · Credits 20:52 →")
        expect(_save_button(page)).to_have_text("Save and publish to 3 servers")

        with page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/api/markers/item/markers"), timeout=5000
        ):
            _save_button(page).click()

        # The added type is sent as an ordinary user marker, with null meaning "runs to the end of the file".
        [body] = inspector.save_bodies
        assert body["path"] == _MEDIA_FILE
        assert body["markers"] == [
            {"type": "intro", "start_ms": 11_000, "end_ms": 37_000},
            {"type": "credits", "start_ms": 1_252_000, "end_ms": None},
        ]

        expect(_server_card(page, "plex-1").locator(".mk-plan")).to_have_text("Updated")
        expect(_server_card(page, "jf-1").locator(".mk-plan")).to_have_text("Couldn't reach it")
        expect(_server_card(page, "jf-1")).to_contain_text(
            "Your times are saved. This server gets them at the next Check servers run."
        )
        expect(page.locator(".mk-saved")).to_contain_text("Saved and locked. Your times stay until you unlock them.")

    def test_nothing_can_be_added_or_removed_while_the_save_is_in_flight(self, authed_page: Page, app_url: str) -> None:
        """A synthetic click reaches both handlers, so the `editing.saving` guards are what say no."""
        inspector = _Inspector(authed_page, app_url, nothing_found())
        held: list[Route] = []
        page = _open_editor(inspector)
        _add(page, "credits").click()
        expect(page.locator(".mk-edit-strip")).to_have_count(2)

        authed_page.route("**/api/markers/item/markers", lambda route: held.append(route))
        _save_button(page).click()
        expect(page.locator(".mk-edit-save")).to_contain_text("Saving…")

        expect(_add(page, "recap")).to_be_disabled()
        _add(page, "recap").dispatch_event("click")
        expect(page.locator(".mk-edit-strip")).to_have_count(2)

        expect(_remove(page, "credits")).to_be_disabled()
        _remove(page, "credits").dispatch_event("click")
        expect(page.locator(".mk-edit-strip")).to_have_count(2)

        held[0].fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"markers": {"credits": _saved("credits", 1_262_000, _DURATION)}, "servers": []}),
        )
        expect(page.locator(".mk-saved")).to_be_visible(timeout=5000)

    def test_add_on_a_movie_offers_only_the_ending_windows_types(self, authed_page: Page, app_url: str) -> None:
        """A movie has no opening window, so an intro or recap can't be added to one at all."""
        payload = movie()
        payload["decisions"]["credits"] = _decision("no_evidence")
        payload["evidence"] = []
        payload["servers"] = [_server("jf-1", "Jellyfin", "jellyfin", "nothing_to_publish", [])]
        inspector = _Inspector(authed_page, app_url, payload)
        page = _open_editor(inspector)

        expect(page.locator("#markersInspectorBody")).to_contain_text("Adjusting this movie.")
        expect(page.locator('.mk-window[data-window="opening"]')).to_have_count(0)
        expect(page.locator(".mk-add")).to_have_count(2)
        expect(_add(page, "intro")).to_have_count(0)
        expect(_add(page, "recap")).to_have_count(0)

        # 2:00:00 long, so the credits seed is the last minute of it.
        _add(page, "credits").click()
        expect(_field(page, "credits", "start")).to_have_value("1:59:00")
        expect(_field(page, "credits", "end")).to_have_value("2:00:00")
        expect(_save_button(page)).to_have_text("Save and publish to 1 server")

    def test_a_type_no_server_can_show_offers_an_add_that_says_why_it_cant_be_pressed(
        self, authed_page: Page, app_url: str
    ) -> None:
        payload = nothing_found()
        payload["servers"] = [
            _server("s-plex", "Plex", "plex", "will_add", []),
            _server("s-emby", "Emby", "emby", "will_add", []),
        ]
        inspector = _Inspector(authed_page, app_url, payload)
        page = _open_editor(inspector)

        refusal = (
            "Recaps can't be added here: neither Plex nor Emby has a recap marker, and no other server has this file."
        )
        expect(_add(page, "recap")).to_be_disabled()
        assert _tooltip(_add(page, "recap")) == refusal
        expect(page.locator('.mk-window[data-window="opening"]')).to_contain_text(refusal)
        # Credits, which both of them do show, is offered as normal on the same screen.
        expect(_add(page, "credits")).to_be_enabled()

        # A synthetic click reaches the handler even though the button is disabled, so the guard in addType is
        # what actually says no -- not just the browser refusing to fire on a disabled button.
        _add(page, "recap").dispatch_event("click")
        expect(_strip(page, "recap")).to_have_count(0)
        assert inspector.save_bodies == []

    def test_remove_takes_a_just_added_marker_back_out_and_leaves_the_rest(
        self, authed_page: Page, app_url: str
    ) -> None:
        inspector = _Inspector(authed_page, app_url, nothing_found())
        page = _open_editor(inspector)

        _add(page, "credits").click()
        expect(page.locator(".mk-edit-pending")).to_have_text("Intro 0:11–0:37 · Credits 21:02 →")
        assert _tooltip(_remove(page, "credits")) == "Take this one back out. Nothing has been saved yet."
        # A marker detection found has no Remove: Unlock and Re-detect are what drop one of those.
        expect(_remove(page, "intro")).to_have_count(0)

        _remove(page, "credits").click()

        expect(_strip(page, "credits")).to_have_count(0)
        expect(_add(page, "credits")).to_be_visible()
        expect(page.locator(".mk-edit-pending")).to_have_text("Intro 0:11–0:37")
        expect(_strip(page, "intro")).to_be_visible()
        assert inspector.save_bodies == []

    def test_removing_the_only_added_marker_puts_the_action_bar_back_to_empty(
        self, authed_page: Page, app_url: str
    ) -> None:
        """Nothing was found here, so taking the one addition back out leaves the edit with nothing to save."""
        inspector = _Inspector(authed_page, app_url, nothing_found_at_all())
        page = _open_editor(inspector)

        _add(page, "credits").click()
        expect(page.locator(".mk-edit-pending")).to_have_text("Credits 21:02 →")
        expect(_save_button(page)).to_be_enabled()

        _remove(page, "credits").click()

        expect(page.locator(".mk-edit-pending")).to_have_text("Nothing to save yet")
        expect(page.locator(".mk-edit-pending")).to_have_class(re.compile(r"\bmk-edit-empty\b"))
        expect(_save_button(page)).to_have_text("Save")
        expect(_save_button(page)).to_be_disabled()
        expect(_add(page, "credits")).to_be_visible()

    def test_cancel_after_adding_sends_nothing_and_leaves_the_file_as_it_was(
        self, authed_page: Page, app_url: str
    ) -> None:
        inspector = _Inspector(authed_page, app_url, nothing_found())
        page = _open_editor(inspector)

        _add(page, "credits").click()
        expect(_strip(page, "credits")).to_be_visible()

        page.locator(".mk-edit-cancel").click()

        expect(page.locator(".mk-edit-actions")).to_have_count(0)
        expect(page.locator(".mk-add")).to_have_count(0)
        expect(page.locator(".mk-chips")).to_contain_text("Credits: No markers found")
        expect(page.locator(".mk-chips")).not_to_contain_text("Locked by you")
        assert inspector.save_bodies == []

    def test_a_type_whose_detection_is_off_can_still_be_added(self, authed_page: Page, app_url: str) -> None:
        """Spec §5.5 rule 1: a locked user marker wins even for a type whose detection is off."""
        inspector = _Inspector(authed_page, app_url, south_park())
        page = _open_editor(inspector)

        # south_park() has recap and preview switched off, and says so above the timeline.
        expect(page.locator(".mk-chips")).to_contain_text("Recap: Detection off")
        expect(_add(page, "recap")).to_be_enabled()

        _add(page, "recap").click()
        expect(_field(page, "recap", "start")).to_have_value("0:00")
        expect(page.locator(".mk-edit-pending")).to_contain_text("Recap 0:00–0:30")


def known_without_a_length() -> dict:
    """A file a job looked at but ffprobe reported no duration for, so nothing can be placed on its timeline."""
    payload = nothing_found_at_all()
    payload["duration_ms"] = None
    return payload


def short_file(duration_ms: int) -> dict:
    """Nothing found, and shorter than the credits seed's own minute."""
    payload = nothing_found_at_all()
    payload["duration_ms"] = duration_ms
    return payload


@pytest.mark.e2e
class TestAddSeedBounds:
    """The `max(0, ...)` in addSeed, and the intro/recap seed that deliberately has no clamp."""

    @pytest.mark.parametrize(
        ("duration", "mtype", "start", "end"),
        [
            # Long enough that the seed is the plain subtraction.
            (1_322_000, "credits", 1_262_000, 1_322_000),
            (1_322_000, "preview", 1_292_000, 1_322_000),
            # Shorter than the seed: without the clamp these would be -20 s and -10 s, which the API answers 400 for.
            (40_000, "credits", 0, 40_000),
            (20_000, "preview", 0, 20_000),
            # The head types are never clamped -- an end past the file is meant to be refused on screen.
            (1_322_000, "intro", 0, 30_000),
            (20_000, "intro", 0, 30_000),
            (20_000, "recap", 0, 30_000),
        ],
    )
    def test_the_seed_is_clamped_at_zero_for_the_tail_types_only(
        self, authed_page: Page, app_url: str, duration: int, mtype: str, start: int, end: int
    ) -> None:
        inspector = _Inspector(authed_page, app_url, nothing_found_at_all())
        inspector.open_result()
        inspector.open_tab()
        seed = authed_page.evaluate("([d, t]) => window.markersEditor.addSeed({duration_ms: d}, t)", [duration, mtype])
        assert seed == {"start": start, "end": end, "toEnd": mtype in ("credits", "preview")}

    def test_a_credits_seed_on_a_short_file_starts_at_zero_and_warns_rather_than_refusing(
        self, authed_page: Page, app_url: str
    ) -> None:
        """P-R2: the clamp keeps it saveable, and the ordinary "earlier than credits usually start" warning shows."""
        inspector = _Inspector(authed_page, app_url, short_file(40_000))
        page = _open_editor(inspector)

        _add(page, "credits").click()
        expect(_field(page, "credits", "start")).to_have_value("0:00")
        expect(_field(page, "credits", "end")).to_have_value("0:40")
        expect(_strip(page, "credits").locator("input[role='switch']")).to_be_checked()
        expect(_strip(page, "credits")).to_contain_text(
            "That's earlier than credits usually start. This will still be saved."
        )
        expect(_save_button(page)).to_be_enabled()

        with page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/api/markers/item/markers"), timeout=5000
        ):
            _save_button(page).click()
        assert inspector.save_bodies[0]["markers"] == [{"type": "credits", "start_ms": 0, "end_ms": None}]

    def test_an_intro_seed_past_the_end_of_a_very_short_file_is_refused_on_screen(
        self, authed_page: Page, app_url: str
    ) -> None:
        """Deliberately unclamped: a 20 s file gets the shipped refusal, not a silent 400 from the API."""
        inspector = _Inspector(authed_page, app_url, short_file(20_000))
        page = _open_editor(inspector)

        _add(page, "intro").click()
        expect(_strip(page, "intro")).to_contain_text("That's past the end of the file (0:20).")
        expect(_save_button(page)).to_be_disabled()
        assert inspector.save_bodies == []


@pytest.mark.e2e
class TestAddRefusedWithoutALength:
    def test_a_file_whose_length_is_not_known_cannot_be_adjusted_or_added_to(
        self, authed_page: Page, app_url: str
    ) -> None:
        """A job looked at this file but ffprobe gave no duration -- no timeline, so nothing to put a marker on."""
        inspector = _Inspector(authed_page, app_url, known_without_a_length())
        inspector.open_result()
        page = inspector.open_tab()

        expect(page.locator("#markersInspectorBody")).to_contain_text(
            "The file's length isn't known yet, so there is no timeline."
        )
        expect(page.locator("#markersAdjustBtn")).to_be_disabled()
        expect(page.locator(".mk-add")).to_have_count(0)
        expect(page.locator("#markersRedetectBtn")).to_be_enabled()

    def test_no_server_with_intro_and_credits_on_disables_every_add_and_says_why(
        self, authed_page: Page, app_url: str
    ) -> None:
        """The other half of reachNote's refusal: nobody to name, because no enabled owner has the file at all."""
        payload = nothing_found_at_all()
        for server in payload["servers"]:
            server["markers_enabled"] = False
        inspector = _Inspector(authed_page, app_url, payload)
        page = _open_editor(inspector)

        refusal = "Intros can't be added here: no server with Intro & Credits on has this file."
        expect(_add(page, "intro")).to_be_disabled()
        assert _tooltip(_add(page, "intro")) == refusal
        expect(page.locator('.mk-window[data-window="opening"]')).to_contain_text(refusal)
        expect(page.locator(".mk-add:not([disabled])")).to_have_count(0)
        expect(_save_button(page)).to_have_text("Save")
        expect(_save_button(page)).to_be_disabled()

        _add(page, "intro").dispatch_event("click")
        expect(page.locator(".mk-edit-strip")).to_have_count(0)
        assert inspector.save_bodies == []
