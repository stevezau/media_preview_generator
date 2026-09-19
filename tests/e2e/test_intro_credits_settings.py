"""E2E: Settings → "Intro & Credits" (shared detection settings).

``GET /api/settings`` is mocked with a ``markers`` block and every autosave ``POST /api/settings`` is captured, so each
test asserts the ``markers`` block the page sends. ``/api/markers/sources/usage`` is mocked for the TheIntroDB usage
line.
"""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Callable

import pytest
from playwright.sync_api import Page, Route, expect

from ._mocks import mock_settings_backups, mock_setup_status, mock_system_status

SOURCE_ORDER = ["chapters", "theintrodb", "introdb", "skipdb", "season_audio", "credits_text", "server_markers"]


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


def _default_markers() -> dict:
    return {
        "detect": {"intro": True, "credits": True, "recap": False},
        "publish_when": "high",
        "respect_locks": True,
        "sources": [
            {"id": "chapters", "enabled": True},
            {"id": "theintrodb", "enabled": False, "api_key": ""},
            {"id": "introdb", "enabled": True},
            {"id": "skipdb", "enabled": True},
            {"id": "season_audio", "enabled": True},
            {"id": "credits_text", "enabled": True},
            {"id": "server_markers", "enabled": True},
        ],
    }


def _settings_body(markers: dict) -> dict:
    return {
        "cpu_threads": 1,
        "thumbnail_interval": 10,
        "thumbnail_quality": 4,
        "tonemap_algorithm": "hable",
        "log_level": "INFO",
        "log_rotation_size": "10 MB",
        "log_retention_count": 5,
        "job_history_days": 31,
        "gpu_config": [],
        "path_mappings": [],
        "exclude_paths": [],
        "media_servers": [],
        "plex_verify_ssl": True,
        "markers": markers,
    }


def _mock_settings(page: Page, body: dict) -> list[dict]:
    """GET returns ``body``; every POST body is captured.

    One handler for both methods: ``mock_settings_get`` + ``capture_settings_save`` each ``continue_()`` the other
    method to the real backend, so registering both would serve the real settings for GET.
    """
    captured: list[dict] = []

    def handler(route: Route) -> None:
        if route.request.method == "POST":
            captured.append(route.request.post_data_json or {})
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"success": True}))
        else:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route("**/api/settings", handler)
    return captured


def _mock_usage(page: Page, body: object, status: int = 200) -> None:
    def handler(route: Route) -> None:
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    page.route("**/api/markers/sources/usage", handler)


def _mock_local_sources(page: Page, body: object, status: int = 200) -> None:
    def handler(route: Route) -> None:
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    page.route("**/api/markers/sources/local", handler)


AVAILABLE = {
    "season_audio": {"available": True, "ffmpeg": "/usr/lib/jellyfin-ffmpeg/ffmpeg", "message": ""},
    "credits_text": {"available": True, "message": ""},
}


def _open_settings(
    page: Page,
    app_url: str,
    markers: dict,
    usage: object | None = None,
    usage_status: int = 200,
    local: object | None = None,
) -> list[dict]:
    captured = _mock_settings(page, _settings_body(markers))
    mock_setup_status(page, complete=True, plex_authenticated=True)
    mock_system_status(page)
    mock_settings_backups(page)
    _mock_usage(
        page,
        usage
        if usage is not None
        else {"theintrodb": {"day": "2026-09-14", "used": 83, "limit": 500, "has_key": False}},
        usage_status,
    )
    _mock_local_sources(page, local if local is not None else AVAILABLE)
    page.goto(f"{app_url}/settings")
    page.wait_for_load_state("domcontentloaded")
    expect(page.locator("#section-markers")).to_be_visible(timeout=5000)
    # loadSettings() fills job history and Intro & Credits in one synchronous pass, so this value showing means the
    # mocked markers block is applied too (clicking earlier would race the load and be undone by it).
    expect(page.locator("#jobHistoryDays")).to_have_value("31", timeout=5000)
    return captured


def _open_settings_and_wait_for_local(
    page: Page,
    app_url: str,
    markers: dict,
    *,
    local: object | None = None,
    local_status: int = 200,
    local_abort: bool = False,
) -> list[dict]:
    """Like ``_open_settings``, but returns only once ``/api/markers/sources/local`` has actually answered (or
    failed): a bare ``page.goto`` returns as soon as the page's own ``load`` event fires, which can be before that
    fetch (started from a ``DOMContentLoaded`` handler) resolves. The caller still has to assert the row's resulting
    state itself — this only removes the race against the fetch settling.
    """
    captured = _mock_settings(page, _settings_body(markers))
    mock_setup_status(page, complete=True, plex_authenticated=True)
    mock_system_status(page)
    mock_settings_backups(page)
    _mock_usage(page, {"theintrodb": {"day": "2026-09-14", "used": 83, "limit": 500, "has_key": False}})
    if local_abort:
        page.route("**/api/markers/sources/local", lambda route: route.abort())
        with page.expect_event("requestfailed", lambda r: "/api/markers/sources/local" in r.url):
            page.goto(f"{app_url}/settings")
    else:
        _mock_local_sources(page, local if local is not None else AVAILABLE, local_status)
        with page.expect_response(lambda r: "/api/markers/sources/local" in r.url):
            page.goto(f"{app_url}/settings")
    page.wait_for_load_state("domcontentloaded")
    expect(page.locator("#section-markers")).to_be_visible(timeout=5000)
    expect(page.locator("#jobHistoryDays")).to_have_value("31", timeout=5000)
    return captured


def _wait_for_post(page: Page, captured: list[dict], predicate: Callable[[dict], bool], timeout_s: float = 8.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for body in reversed(captured):
            if "markers" in body and predicate(body["markers"]):
                return body["markers"]
        page.wait_for_timeout(100)
    raise AssertionError(
        f"no POST /api/settings matched; last markers sent: {[b.get('markers') for b in captured][-1:]}"
    )


def _source_ids(page: Page) -> list[str]:
    return page.locator("#markersSourceList .markers-source").evaluate_all("els => els.map((el) => el.dataset.id)")


@pytest.mark.e2e
class TestIntroCreditsSettings:
    def test_defaults_render(self, authed_page: Page, app_url: str) -> None:
        _open_settings(authed_page, app_url, _default_markers())
        expect(authed_page.locator('a[href="#section-markers"]')).to_be_visible()
        expect(authed_page.locator("#markersDetectIntro")).to_be_checked()
        expect(authed_page.locator("#markersDetectCredits")).to_be_checked()
        expect(authed_page.locator("#markersDetectRecap")).not_to_be_checked()
        expect(authed_page.locator("#markersPublishHigh")).to_be_checked()
        expect(authed_page.locator("#markersPublishMedium")).not_to_be_checked()
        expect(authed_page.locator("#markersRespectLocks")).to_be_checked()
        assert _source_ids(authed_page) == SOURCE_ORDER
        theintrodb = authed_page.locator("#markersSourceList .markers-source[data-id='theintrodb']")
        expect(theintrodb.locator(".markers-source-enabled")).not_to_be_checked()
        expect(authed_page.locator("#markersTheIntroDbKey")).to_have_attribute("type", "password")
        expect(authed_page.locator("#markersTheIntroDbKey")).to_have_attribute("placeholder", "(optional)")
        # Every source row explains itself.
        expect(authed_page.locator("#markersSourceList .markers-source .info-icon")).to_have_count(7)
        expect(authed_page.locator("#markersSourceList")).to_contain_text("Second opinion, never copied as-is")

    def test_local_sources_round_trip_their_stored_values(self, authed_page: Page, app_url: str) -> None:
        markers = _default_markers()
        by_id = {s["id"]: s for s in markers["sources"]}
        by_id["season_audio"]["enabled"] = False
        by_id["credits_text"]["enabled"] = True
        # Stored order puts the two local sources first.
        markers["sources"] = [by_id["credits_text"], by_id["season_audio"]] + [
            s for s in markers["sources"] if s["id"] not in ("credits_text", "season_audio")
        ]
        captured = _open_settings(authed_page, app_url, markers)
        expect(
            authed_page.locator("#markersSourceList .markers-source[data-id='season_audio'] .markers-source-enabled")
        ).not_to_be_checked()

        authed_page.locator("label[for='markersDetectRecap']").click()
        sent = _wait_for_post(authed_page, captured, lambda m: m["detect"]["recap"] is True)
        assert sent["sources"] == markers["sources"]

    def test_no_source_is_coming_soon(self, authed_page: Page, app_url: str) -> None:
        _open_settings_and_wait_for_local(authed_page, app_url, _default_markers())
        expect(authed_page.locator("#markersSourceList .markers-source-soon-badge")).to_have_count(0)
        expect(authed_page.locator("#markersSourceList .markers-source-soon")).to_have_count(0)
        for source_id in SOURCE_ORDER:
            switch = authed_page.locator(
                f"#markersSourceList .markers-source[data-id='{source_id}'] .markers-source-enabled"
            )
            expect(switch).to_be_enabled()

    def test_credit_text_is_switchable_and_explains_its_numbers(self, authed_page: Page, app_url: str) -> None:
        captured = _open_settings_and_wait_for_local(authed_page, app_url, _default_markers())
        row = authed_page.locator("#markersSourceList .markers-source[data-id='credits_text']")
        expect(row.locator(".markers-source-unavailable")).to_be_hidden()
        expect(row.locator(".markers-source-reason")).to_be_hidden()
        expect(row).to_contain_text(
            "Reads the end of the file · GPU when that's faster, otherwise CPU · about 10–30 s per file; 4K without "
            "a GPU up to about 2 min"
        )
        # The numbers are the app's own reported GPU run on the 80 (evidence/eval/phase3-harness.md): 63 within 10 s,
        # 1 more than 30 s early, 3 with no answer. "missed" is the no-answer count, never the 8 late answers.
        tooltip = row.locator(".info-icon").evaluate(
            "el => el.getAttribute('data-bs-original-title') || el.getAttribute('title')"
        )
        assert tooltip == (
            "Finds where the credit roll starts from text on screen near the end of the file, and stops the skip at "
            "the last credit when a scene follows. Tested alone on 80 files, on a GPU: within 10 s on 63, more than 30 s early "
            'on 1, missed 3. At "High" another source has to agree; at "Medium" it can publish alone.'
        )
        row.locator(".markers-source-enabled").click()
        sent = _wait_for_post(
            authed_page,
            captured,
            lambda m: next(s for s in m["sources"] if s["id"] == "credits_text")["enabled"] is False,
        )
        assert [s["id"] for s in sent["sources"]] == SOURCE_ORDER

    def test_credit_text_unavailable_says_why_and_keeps_the_stored_choice(
        self, authed_page: Page, app_url: str
    ) -> None:
        reason = (
            "Needs the text detection model, which the Docker image includes; it isn't at "
            "/app/models/ch_PP-OCRv4_det_infer.onnx"
        )
        local = {**AVAILABLE, "credits_text": {"available": False, "message": reason}}
        captured = _open_settings_and_wait_for_local(authed_page, app_url, _default_markers(), local=local)
        row = authed_page.locator("#markersSourceList .markers-source[data-id='credits_text']")
        expect(row.locator(".markers-source-unavailable")).to_have_text("Not available", timeout=5000)
        expect(row.locator(".markers-source-reason")).to_have_text(reason)
        expect(row.locator(".markers-source-enabled")).to_be_checked()
        season = authed_page.locator("#markersSourceList .markers-source[data-id='season_audio']")
        expect(season.locator(".markers-source-unavailable")).to_be_hidden()
        authed_page.locator("label[for='markersDetectRecap']").click()
        sent = _wait_for_post(authed_page, captured, lambda m: m["detect"]["recap"] is True)
        assert next(s for s in sent["sources"] if s["id"] == "credits_text")["enabled"] is True

    @pytest.mark.parametrize(
        ("unknown", "unavailable"), [("season_audio", "credits_text"), ("credits_text", "season_audio")]
    )
    def test_a_check_that_couldnt_run_leaves_its_row_while_the_other_says_why(
        self, authed_page: Page, app_url: str, unknown: str, unavailable: str
    ) -> None:
        # GET /api/markers/sources/local answers "available": null for a check that raised (not known either way).
        reason = "Needs ONNX Runtime and OpenCV, which the Docker image includes; they aren't installed here"
        local = {
            "season_audio": {
                "available": None,
                "ffmpeg": None,
                "message": "Couldn't check whether season audio can run here",
            },
            "credits_text": {"available": None, "message": "Couldn't check whether credit text can run here"},
        }
        local[unavailable] = {**local[unavailable], "available": False, "message": reason}
        _open_settings_and_wait_for_local(authed_page, app_url, _default_markers(), local=local)

        changed = authed_page.locator(f"#markersSourceList .markers-source[data-id='{unavailable}']")
        expect(changed.locator(".markers-source-unavailable")).to_be_visible(timeout=5000)
        expect(changed.locator(".markers-source-reason")).to_have_text(reason)
        left = authed_page.locator(f"#markersSourceList .markers-source[data-id='{unknown}']")
        expect(left.locator(".markers-source-unavailable")).to_be_hidden()
        expect(left.locator(".markers-source-reason")).to_be_hidden()
        expect(left.locator(".markers-source-reason")).to_have_text("")
        expect(left.locator(".markers-source-enabled")).to_be_checked()

    def test_an_answer_without_credit_text_leaves_its_row_as_is(self, authed_page: Page, app_url: str) -> None:
        # An app one version behind answers season audio only.
        _open_settings_and_wait_for_local(
            authed_page, app_url, _default_markers(), local={"season_audio": AVAILABLE["season_audio"]}
        )
        row = authed_page.locator("#markersSourceList .markers-source[data-id='credits_text']")
        expect(row.locator(".markers-source-enabled")).to_be_enabled()
        expect(row.locator(".markers-source-unavailable")).to_be_hidden()

    def test_season_audio_is_switchable_and_explains_its_numbers(self, authed_page: Page, app_url: str) -> None:
        captured = _open_settings_and_wait_for_local(authed_page, app_url, _default_markers())
        row = authed_page.locator("#markersSourceList .markers-source[data-id='season_audio']")
        switch = row.locator(".markers-source-enabled")
        expect(switch).to_be_enabled()
        expect(row.locator(".markers-source-soon-badge")).to_have_count(0)
        expect(row.locator(".markers-source-unavailable")).to_be_hidden()
        expect(row.locator(".markers-source-reason")).to_be_hidden()
        # Bootstrap moves ``title`` into ``data-bs-original-title`` once the tooltip is initialised.
        tooltip = row.locator(".info-icon").evaluate(
            "el => el.getAttribute('data-bs-original-title') || el.getAttribute('title')"
        )
        assert tooltip == (
            "Finds the theme tune a season's episodes share. Tested alone on 118 episodes: 91 right, 13 wrong, 14 "
            "missed — too error-prone to decide by itself, so it never publishes an intro alone; it only confirms "
            "what another source already found. A server's own intro marker doesn't count as that other source."
        )

        switch.click()
        sent = _wait_for_post(
            authed_page,
            captured,
            lambda m: next(s for s in m["sources"] if s["id"] == "season_audio")["enabled"] is False,
        )
        assert [s["id"] for s in sent["sources"]] == SOURCE_ORDER

    def test_season_audio_without_chromaprint_says_why(self, authed_page: Page, app_url: str) -> None:
        reason = "Needs an ffmpeg with the chromaprint muxer (the Docker image's jellyfin-ffmpeg has it)"
        captured = _open_settings_and_wait_for_local(
            authed_page,
            app_url,
            _default_markers(),
            local={"season_audio": {"available": False, "ffmpeg": None, "message": reason}},
        )
        row = authed_page.locator("#markersSourceList .markers-source[data-id='season_audio']")
        expect(row.locator(".markers-source-unavailable")).to_be_visible(timeout=5000)
        expect(row.locator(".markers-source-unavailable")).to_have_text("Not available")
        expect(row.locator(".markers-source-reason")).to_be_visible()
        expect(row.locator(".markers-source-reason")).to_have_text(reason)
        expect(row.locator(".markers-source-enabled")).to_be_checked()

        # An unavailable local check only flags the row; the stored choice round-trips.
        authed_page.locator("label[for='markersDetectRecap']").click()
        sent = _wait_for_post(authed_page, captured, lambda m: m["detect"]["recap"] is True)
        assert next(s for s in sent["sources"] if s["id"] == "season_audio")["enabled"] is True

    def test_local_source_check_failing_leaves_the_row_as_is(self, authed_page: Page, app_url: str) -> None:
        _open_settings_and_wait_for_local(authed_page, app_url, _default_markers(), local={"error": "boom"})
        row = authed_page.locator("#markersSourceList .markers-source[data-id='season_audio']")
        expect(row.locator(".markers-source-enabled")).to_be_enabled()
        expect(row.locator(".markers-source-unavailable")).to_be_hidden()

    def test_local_source_check_500_leaves_the_row_as_is(self, authed_page: Page, app_url: str) -> None:
        # Exercises loadMarkersLocalSources()'s ``if (!response.ok) throw ...`` path.
        _open_settings_and_wait_for_local(
            authed_page, app_url, _default_markers(), local={"error": "boom"}, local_status=500
        )
        row = authed_page.locator("#markersSourceList .markers-source[data-id='season_audio']")
        expect(row.locator(".markers-source-enabled")).to_be_enabled()
        expect(row.locator(".markers-source-unavailable")).to_be_hidden()

    def test_local_source_check_network_failure_leaves_the_row_as_is(self, authed_page: Page, app_url: str) -> None:
        # Exercises loadMarkersLocalSources()'s ``catch`` path: fetch() itself rejects, no response at all.
        _open_settings_and_wait_for_local(authed_page, app_url, _default_markers(), local_abort=True)
        row = authed_page.locator("#markersSourceList .markers-source[data-id='season_audio']")
        expect(row.locator(".markers-source-enabled")).to_be_enabled()
        expect(row.locator(".markers-source-unavailable")).to_be_hidden()

    def test_publish_when_tooltip_describes_high_and_medium(self, authed_page: Page, app_url: str) -> None:
        _open_settings(authed_page, app_url, _default_markers())
        icon = authed_page.locator("#markersPublishWhenLabel + .info-icon")
        # Bootstrap moves ``title`` into ``data-bs-original-title`` once the tooltip is initialised.
        tooltip = icon.evaluate("el => el.getAttribute('data-bs-original-title') || el.getAttribute('title')")
        assert tooltip == (
            "High: chapters publish on their own unless two other independent sources agree on something different; "
            "otherwise two independent sources must agree. Medium: also accepts a single source that checks your own "
            "file — chapters, on-screen credit text (credits), or SkipDB matched to your file's length (intros and "
            "recaps only). A single IntroDB or TheIntroDB answer never publishes on its own, because those don't know "
            "which cut you have. Anything else shows as Needs review."
        )

    def test_publish_when_tooltip_names_the_sources_that_decide_alone_at_medium(
        self, authed_page: Page, app_url: str
    ) -> None:
        # Checked against the decision rules over every source × marker type, not a copy of the string: the list
        # drifted once (credit text left out). A new source fails here until it is mapped below.
        from media_preview_generator.markers.decide import _may_decide_alone
        from media_preview_generator.markers.models import Candidate, MarkerType, Source

        phrases = {
            Source.CHAPTERS: "chapters",
            Source.THEINTRODB: "TheIntroDB",
            Source.INTRODB: "IntroDB",
            Source.SKIPDB: "SkipDB",
            Source.SEASON_AUDIO: "audio",
            Source.SEASON_AUDIO_PREVIOUS: "previous season",
            Source.CREDITS_TEXT: "credit text",
        }
        not_in_the_tooltip = {
            Source.USER: "a marker you locked isn't detection evidence; it wins through 'Never overwrite my edits'",
            Source.SERVER_MARKERS: "markers already on a server only ever confirm (its own row says so)",
            Source.SERVER_MARKERS_IMPORTED: "an importer plugin's copy counts as its database's source, never alone",
        }
        assert set(Source) == phrases.keys() | not_in_the_tooltip.keys()
        # The types a source can answer at all, where that's fewer than every type (spec §5.4: credit text).
        answers = {Source.CREDITS_TEXT: {MarkerType.CREDITS}}
        qualifiers = {
            frozenset({MarkerType.CREDITS}): "(credits)",
            frozenset({MarkerType.INTRO, MarkerType.RECAP}): "(intros and recaps only)",
        }

        _open_settings(authed_page, app_url, _default_markers())
        icon = authed_page.locator("#markersPublishWhenLabel + .info-icon")
        tooltip = icon.evaluate("el => el.getAttribute('data-bs-original-title') || el.getAttribute('title')")
        medium = tooltip.split("Medium:", 1)[1]
        # "also accepts a single source that checks your own file — A, B (x), or C (y). A single … never publishes …"
        accepted = medium.split(" — ", 1)[1].split(". ", 1)[0]
        items = [item.removeprefix("or ") for item in accepted.split(", ")]

        for source, phrase in phrases.items():
            decides = {
                mtype
                for mtype in answers.get(source, set(MarkerType))
                if _may_decide_alone(Candidate(type=mtype, start_ms=0, end_ms=None, source=source))
            }
            named = [item for item in items if phrase in item]
            assert bool(named) is bool(decides), (source, accepted)
            if decides and decides != set(MarkerType):
                assert qualifiers[frozenset(decides)] in named[0], (source, named)
            elif decides:
                assert "(" not in named[0], (source, named)
        assert "never publishes on its own" in medium

    def test_intros_toggle_tooltip_names_season_audio_as_live(self, authed_page: Page, app_url: str) -> None:
        # Season audio shipped before this feature; the tooltip must not still call it "(soon)".
        _open_settings(authed_page, app_url, _default_markers())
        icon = authed_page.locator("label[for='markersDetectIntro'] + .info-icon")
        tooltip = icon.evaluate("el => el.getAttribute('data-bs-original-title') || el.getAttribute('title')")
        assert tooltip == "Found from chapters, online databases, and by matching the theme tune across a season."

    def test_credits_toggle_tooltip_does_not_claim_season_audio(self, authed_page: Page, app_url: str) -> None:
        # Season audio only ever detects intros/recaps (markers/audio/season.py), never credits.
        _open_settings(authed_page, app_url, _default_markers())
        icon = authed_page.locator("label[for='markersDetectCredits'] + .info-icon")
        tooltip = icon.evaluate("el => el.getAttribute('data-bs-original-title') || el.getAttribute('title')")
        assert tooltip == "Found from chapters, online databases, and the on-screen credit roll."

    def test_stored_order_and_values_render(self, authed_page: Page, app_url: str) -> None:
        markers = _default_markers()
        markers["publish_when"] = "medium"
        markers["respect_locks"] = False
        markers["detect"]["intro"] = False
        markers["sources"] = [markers["sources"][3], *markers["sources"][:3], *markers["sources"][4:]]
        _open_settings(authed_page, app_url, markers)
        expect(authed_page.locator("#markersPublishMedium")).to_be_checked(timeout=5000)
        expect(authed_page.locator("#markersRespectLocks")).not_to_be_checked()
        expect(authed_page.locator("#markersDetectIntro")).not_to_be_checked()
        assert _source_ids(authed_page) == ["skipdb", "chapters", "theintrodb", "introdb", *SOURCE_ORDER[4:]]

    def test_toggle_recaps_sends_only_that_change(self, authed_page: Page, app_url: str) -> None:
        captured = _open_settings(authed_page, app_url, _default_markers())
        expect(authed_page.locator("#markersDetectRecap")).not_to_be_checked(timeout=5000)
        authed_page.locator("label[for='markersDetectRecap']").click()

        sent = _wait_for_post(authed_page, captured, lambda m: m["detect"]["recap"] is True)
        expected = _default_markers()
        expected["detect"]["recap"] = True
        assert sent == expected

    def test_publish_when_and_locks_are_sent(self, authed_page: Page, app_url: str) -> None:
        captured = _open_settings(authed_page, app_url, _default_markers())
        expect(authed_page.locator("#markersPublishHigh")).to_be_checked(timeout=5000)
        authed_page.locator("label[for='markersPublishMedium']").click()
        _wait_for_post(authed_page, captured, lambda m: m["publish_when"] == "medium")
        authed_page.locator("label[for='markersRespectLocks']").click()
        sent = _wait_for_post(authed_page, captured, lambda m: m["respect_locks"] is False)
        assert sent["publish_when"] == "medium"

    def test_move_skipdb_to_top_with_up_button(self, authed_page: Page, app_url: str) -> None:
        captured = _open_settings(authed_page, app_url, _default_markers())
        up = authed_page.locator("#markersSourceList .markers-source[data-id='skipdb'] .markers-source-up")
        for _ in range(3):
            up.click()
        assert _source_ids(authed_page)[0] == "skipdb"

        sent = _wait_for_post(authed_page, captured, lambda m: m["sources"][0]["id"] == "skipdb")
        assert sorted(s["id"] for s in sent["sources"]) == sorted(SOURCE_ORDER)
        assert [s["id"] for s in sent["sources"]] == ["skipdb", "chapters", "theintrodb", "introdb", *SOURCE_ORDER[4:]]
        # Moving keeps each source's own switch.
        assert {s["id"]: s["enabled"] for s in sent["sources"]}["theintrodb"] is False

    def test_top_up_and_bottom_down_buttons_are_disabled(self, authed_page: Page, app_url: str) -> None:
        _open_settings(authed_page, app_url, _default_markers())
        expect(
            authed_page.locator("#markersSourceList .markers-source[data-id='chapters'] .markers-source-up")
        ).to_be_disabled()
        expect(
            authed_page.locator("#markersSourceList .markers-source[data-id='server_markers'] .markers-source-down")
        ).to_be_disabled()

    def test_drag_and_drop_reorders_and_saves(self, authed_page: Page, app_url: str) -> None:
        # Tall enough that the whole list is on screen: a drag can't target a row scrolled out of view.
        authed_page.set_viewport_size({"width": 1280, "height": 1400})
        captured = _open_settings(authed_page, app_url, _default_markers())
        authed_page.locator("#markersSourceList").scroll_into_view_if_needed()
        source = authed_page.locator(
            "#markersSourceList .markers-source[data-id='server_markers'] .markers-source-grip"
        )
        target = authed_page.locator("#markersSourceList .markers-source[data-id='chapters']")
        source.drag_to(target, target_position={"x": 20, "y": 2})
        sent = _wait_for_post(authed_page, captured, lambda m: m["sources"][0]["id"] == "server_markers")
        assert [s["id"] for s in sent["sources"]] == ["server_markers", *SOURCE_ORDER[:-1]]

    def test_masked_key_round_trips_as_mask(self, authed_page: Page, app_url: str) -> None:
        markers = _default_markers()
        markers["sources"][1] = {"id": "theintrodb", "enabled": True, "api_key": "****"}
        captured = _open_settings(authed_page, app_url, markers)
        key = authed_page.locator("#markersTheIntroDbKey")
        expect(key).to_have_attribute("placeholder", "****", timeout=5000)
        expect(key).to_have_value("")

        authed_page.locator("label[for='markersDetectRecap']").click()
        sent = _wait_for_post(authed_page, captured, lambda m: m["detect"]["recap"] is True)
        theintrodb = next(s for s in sent["sources"] if s["id"] == "theintrodb")
        assert theintrodb == {"id": "theintrodb", "enabled": True, "api_key": "****"}

    def test_typed_key_is_sent_and_clear_key_sends_empty(self, authed_page: Page, app_url: str) -> None:
        markers = _default_markers()
        markers["sources"][1] = {"id": "theintrodb", "enabled": True, "api_key": "****"}
        captured = _open_settings(authed_page, app_url, markers)
        key = authed_page.locator("#markersTheIntroDbKey")
        expect(key).to_have_attribute("placeholder", "****", timeout=5000)

        key.fill("my-new-key-123")
        key.blur()
        _wait_for_post(
            authed_page,
            captured,
            lambda m: next(s for s in m["sources"] if s["id"] == "theintrodb")["api_key"] == "my-new-key-123",
        )

        authed_page.locator("#markersTheIntroDbClearKey").click()
        sent = _wait_for_post(
            authed_page, captured, lambda m: next(s for s in m["sources"] if s["id"] == "theintrodb")["api_key"] == ""
        )
        assert next(s for s in sent["sources"] if s["id"] == "theintrodb")["enabled"] is True
        expect(key).to_have_value("")
        expect(key).to_have_attribute("placeholder", "(optional)")

    def test_usage_line_shows_todays_lookups(self, authed_page: Page, app_url: str) -> None:
        _open_settings(authed_page, app_url, _default_markers())
        expect(authed_page.locator("#markersTheIntroDbUsage")).to_have_text(
            "83 of today's lookups used · limit set by TheIntroDB", timeout=5000
        )

    def test_usage_line_unknown_shows_dash(self, authed_page: Page, app_url: str) -> None:
        _open_settings(authed_page, app_url, _default_markers(), usage={"error": "boom"}, usage_status=500)
        expect(authed_page.locator("#markersTheIntroDbUsage")).to_have_text("—", timeout=5000)

    def test_usage_line_shows_daily_limit_reached_when_low_priority_is_exhausted(
        self, authed_page: Page, app_url: str
    ) -> None:
        _open_settings(
            authed_page,
            app_url,
            _default_markers(),
            usage={
                "theintrodb": {
                    "day": "2026-09-14",
                    "used": 500,
                    "limit": 500,
                    "remaining": 0,
                    "has_key": False,
                    "low_priority_exhausted": True,
                    "resets_at": "00:00 UTC",
                }
            },
        )
        expect(authed_page.locator("#markersTheIntroDbUsage")).to_have_text(
            "Daily limit reached — lookups resume at 00:00 UTC", timeout=5000
        )

    def test_keyboard_move_down_with_enter(self, authed_page: Page, app_url: str) -> None:
        captured = _open_settings(authed_page, app_url, _default_markers())
        down = authed_page.locator("#markersSourceList .markers-source[data-id='chapters'] .markers-source-down")
        down.focus()
        authed_page.keyboard.press("Enter")
        assert _source_ids(authed_page)[:2] == ["theintrodb", "chapters"]
        # Focus stays on the moved row's button so repeated presses keep moving it.
        authed_page.keyboard.press("Enter")
        assert _source_ids(authed_page)[:3] == ["theintrodb", "introdb", "chapters"]
        sent = _wait_for_post(authed_page, captured, lambda m: m["sources"][2]["id"] == "chapters")
        assert [s["id"] for s in sent["sources"]] == ["theintrodb", "introdb", "chapters", *SOURCE_ORDER[3:]]

    def test_missing_markers_block_falls_back_to_defaults(self, authed_page: Page, app_url: str) -> None:
        body = _settings_body(copy.deepcopy(_default_markers()))
        body.pop("markers")
        _mock_settings(authed_page, body)
        mock_setup_status(authed_page, complete=True, plex_authenticated=True)
        mock_system_status(authed_page)
        mock_settings_backups(authed_page)
        _mock_usage(authed_page, {})
        authed_page.goto(f"{app_url}/settings")
        expect(authed_page.locator("#jobHistoryDays")).to_have_value("31", timeout=5000)
        expect(authed_page.locator("#markersDetectIntro")).to_be_checked()
        expect(authed_page.locator("#markersPublishHigh")).to_be_checked()
        assert _source_ids(authed_page) == SOURCE_ORDER
