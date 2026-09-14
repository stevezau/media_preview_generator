"""E2E: Preview Inspector → Intro & Credits tab (read-only + Re-detect).

The search result, BIF endpoints and ``GET /api/markers/item`` are mocked with the payload shape
``markers.inspect.item_payload`` builds, so each test pins how one decision/evidence/server state renders.
"""

from __future__ import annotations

import copy
import json
import re

import pytest
from playwright.sync_api import Page, Route, expect

from ._mocks import _fulfill_json, mock_servers_list

_MEDIA_FILE = "/data/tv/South Park (1997)/Season 01/South Park S01E03.mkv"
_DURATION = 1_322_000  # 22:02


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


def _decision(status: str | None, marker: tuple[str, int, int] | None = None, **extra) -> dict:
    return {
        "status": status,
        "reason": extra.get("reason", ""),
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
            plan_reason="Plex's own markers are kept (Keep Plex's)",
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
    ) -> None:
        self.page = page
        self.app_url = app_url
        self.payload = payload
        self.item_requests: list[str] = []
        self.info_requests: list[str] = []
        self.redetect_bodies: list[dict] = []
        self.item_handler = item_handler
        mock_servers_list(
            page,
            servers=[{"id": "plex-1", "name": "Plex", "type": "plex", "enabled": True, "url": "http://p:32400"}],
        )
        page.route(
            "**/api/bif/servers/*/search**",
            lambda r: _fulfill_json(
                r, {"server_id": "plex-1", "server_type": "plex", "results": results or [_result()]}
            ),
        )
        page.route("**/api/bif/info**", self._info)
        page.route("**/api/bif/frame**", lambda r: r.fulfill(status=200, body=b""))
        page.route("**/api/markers/item?**", self._item)
        page.route("**/api/markers/item/redetect", self._redetect)

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

        with page.expect_request("**/api/markers/item/redetect") as req:
            button.click()

        assert req.value.method == "POST"
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
            "s-keep": ("Keeps Plex's", ["Plex's own markers are kept (Keep Plex's)"]),
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
        assert "path=" not in inspector.item_requests[1]
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
