"""E2E: the Inspector's behaviours past the happy path — failed and in-flight saves, the re-read after a save, the
save-result toast, Adjust's switches, refusals and add cards, Back to automatic, Needs your check for every type, the
header actions failing, the /jobs socket, read failures, a late answer, the servers card and lanes in every plan, the
evidence notes, search edge cases, a preview-only open, tooltips, and the close-ups' notes and frame reads.

The API is mocked by ``_inspector_fixtures.install``; every write's body is asserted, and every failure is an answer
the real route gives (a status and an ``error``).
"""

from __future__ import annotations

import copy
import os
import re
from collections.abc import Callable
from urllib.parse import parse_qs, quote, urlparse

import pytest
from playwright.sync_api import Page, Response, expect

from . import _inspector_fixtures as fx


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


# The socket.io client script sets window.io as it loads; this stub keeps its place, records the handlers the
# Inspector registers on '/jobs', and lets a test call them as if the server had sent the event.
_SOCKET_STUB = """
(() => {
    const sockets = [];
    const io = function (namespace, options) {
        const handlers = {};
        const socket = {
            namespace: namespace,
            options: options,
            handlers: handlers,
            on(name, fn) { (handlers[name] = handlers[name] || []).push(fn); return socket; },
            off() { return socket; },
            emit() { return socket; },
            disconnect() { return socket; },
        };
        sockets.push(socket);
        return socket;
    };
    window.__inspectorSockets = sockets;
    Object.defineProperty(window, 'io', { configurable: true, get() { return io; }, set(_ignored) {} });
})();
"""


def _open(page: Page, app_url: str, path: str, *, view: str = "") -> None:
    page.goto(f"{app_url}/inspector?path={quote(path)}" + (f"&view={view}" if view else ""))
    expect(page.locator("#inspLoading")).to_have_count(0, timeout=10_000)


def _api_with(file: dict, item: object) -> fx.InspectorApi:
    api = fx.InspectorApi()
    api.add(file, item, fx.default_kinds(file["canonical_path"]))
    return api


def _is_save(r: Response) -> bool:
    return r.url.endswith("/api/markers/item/markers") and r.request.method == "POST"


def _is_unlock(r: Response) -> bool:
    return r.url.endswith("/api/markers/item/markers") and r.request.method == "DELETE"


def _wait_until(page: Page, condition: Callable[[], bool], timeout_ms: int = 5000) -> None:
    """Wait for ``condition`` while letting Playwright run the route handlers (they only run while the page waits)."""
    waited = 0
    while not condition():
        assert waited < timeout_ms, "timed out waiting for the mocked API"
        page.wait_for_timeout(50)
        waited += 50


def _emit(page: Page, name: str, payload: dict) -> None:
    page.evaluate(
        """([name, payload]) => window.__inspectorSockets
            .filter((s) => s.namespace === '/jobs')
            .forEach((s) => (s.handlers[name] || []).forEach((fn) => fn(payload)))""",
        [name, payload],
    )


def _no_preview(file: dict) -> dict:
    file["preview"] = None
    for row in file["previews"]:
        row.update(exists=False, frame_count=None)
    return file


def _locked(item: dict, *types: str) -> dict:
    out = copy.deepcopy(item)
    for t in types:
        out["decisions"][t]["marker"].update(locked=True, decided_by=["user"])
    return out


def _intro_review() -> fx.InspectorApi:
    """An episode whose intro needs your check: season audio says 2:45-2:59, TheIntroDB 3:10-3:42."""
    file, item = fx.checked_episode()
    item["decisions"]["intro"] = fx.decision("needs_review", reason="sources disagree")
    item["evidence"] = [
        fx.evidence_row("season_audio", "intro", 165_000, 179_000, label="20/20"),
        fx.evidence_row("theintrodb", "intro", 190_000, 222_000),
        fx.evidence_row("chapters", "credits", 1_499_000, None, label="Credits"),
    ]
    return _api_with(file, item)


@pytest.mark.e2e
class TestSaving:
    """Items 1-4: a failed save, a save in flight, the re-read after a save, and the save-result toast."""

    def test_a_failed_adjust_save_keeps_the_edit_and_can_be_sent_again(self, authed_page: Page, app_url: str) -> None:
        api = fx.install(authed_page)
        api.answers["save"] = (409, {"error": "The file changed on disk since this page read it"})
        _open(authed_page, app_url, fx.EPISODE)
        authed_page.locator("#inspAdjust").click()
        authed_page.locator("[data-edge='intro-start']").get_by_role(
            "button", name="Move the intro start one second later"
        ).click()

        save = authed_page.locator("#inspAdjustSave")
        with authed_page.expect_response(_is_save):
            save.click()
        bar = authed_page.locator("#inspAdjustBar")
        expect(bar.locator(".text-danger")).to_have_text(
            "Couldn't save: The file changed on disk since this page read it"
        )
        expect(save).to_be_enabled()
        expect(save).to_have_text("Save and send to Plex and Jellyfin")
        expect(authed_page.locator("[data-edge='intro-start'] .insp-edge-label")).to_have_text("Starts at 2:46")
        expect(bar).to_contain_text("Your times: intro 2:46–2:59 · credits 24:59 → end")
        sent = {
            "path": fx.EPISODE,
            "markers": [
                {"type": "intro", "start_ms": 166_000, "end_ms": 179_000},
                {"type": "credits", "start_ms": 1_499_000, "end_ms": None},
            ],
        }
        assert api.saves == [sent]
        assert api.item_requests == [fx.EPISODE]

        del api.answers["save"]
        with authed_page.expect_response(_is_save):
            save.click()
        expect(authed_page.locator("#inspAdjustBar")).to_have_count(0)
        assert api.saves == [sent, sent]

    def test_a_failed_review_save_keeps_the_choice(self, authed_page: Page, app_url: str) -> None:
        api = fx.install(authed_page)
        api.answers["save"] = (409, {"error": "The file changed on disk since this page read it"})
        _open(authed_page, app_url, fx.REVIEW)
        cards = authed_page.locator("[data-review='credits'] [data-candidate]")
        cards.nth(1).get_by_role("button", name="Credits start at 1:32:37").click()

        save = authed_page.locator("[data-save-review='credits']")
        with authed_page.expect_response(_is_save):
            save.click()
        confirm = authed_page.locator("[data-confirm='credits']")
        expect(confirm.locator(".text-danger")).to_have_text(
            "Couldn't save: The file changed on disk since this page read it"
        )
        expect(confirm).to_contain_text("Selected: credits start at 1:32:37")
        expect(save).to_be_enabled()
        expect(save).to_have_text("Save and send to Plex")
        assert api.saves == [
            {"path": fx.REVIEW, "markers": [{"type": "credits", "start_ms": 5_557_000, "end_ms": None}]}
        ]

    def test_adjust_save_in_flight_says_sending_and_waits(self, authed_page: Page, app_url: str) -> None:
        api = fx.install(authed_page)
        api.hold_writes = {"save"}
        _open(authed_page, app_url, fx.EPISODE)
        authed_page.locator("#inspAdjust").click()
        save = authed_page.locator("#inspAdjustSave")
        save.click()

        expect(save).to_have_text("Sending…")
        expect(save).to_be_disabled()
        _wait_until(authed_page, lambda: len(api.saves) == 1)
        api.release_writes()
        expect(authed_page.locator("#inspAdjustBar")).to_have_count(0)
        assert len(api.saves) == 1

    def test_review_save_in_flight_says_sending_and_waits(self, authed_page: Page, app_url: str) -> None:
        api = fx.install(authed_page)
        api.hold_writes = {"save"}
        _open(authed_page, app_url, fx.REVIEW)
        authed_page.locator("[data-review='credits'] [data-candidate]").nth(0).get_by_role(
            "button", name="Credits start at 1:32:09"
        ).click()
        save = authed_page.locator("[data-save-review='credits']")
        save.click()

        expect(save).to_have_text("Sending…")
        expect(save).to_be_disabled()
        _wait_until(authed_page, lambda: len(api.saves) == 1)
        api.release_writes()
        expect(authed_page.locator("#toastBody")).to_have_text("Plex has them now.")
        assert api.saves == [
            {"path": fx.REVIEW, "markers": [{"type": "credits", "start_ms": 5_529_000, "end_ms": None}]}
        ]

    def test_after_a_save_the_editor_closes_and_the_file_is_read_again(self, authed_page: Page, app_url: str) -> None:
        file, item = fx.checked_episode()
        api = _api_with(file, item)
        api.after_save[fx.EPISODE] = _locked(item, "intro", "credits")
        fx.install(authed_page, api)
        _open(authed_page, app_url, fx.EPISODE)
        authed_page.locator("#inspAdjust").click()
        with authed_page.expect_response(_is_save):
            authed_page.locator("#inspAdjustSave").click()

        expect(authed_page.locator("#inspAdjustBar")).to_have_count(0)
        expect(authed_page.locator("#inspLocked")).to_contain_text(
            "You set the intro and credits, so later checks keep them."
        )
        for t in ("intro", "credits"):
            expect(authed_page.locator(f"#inspCloseups [data-closeup='{t}'] .insp-frames-note")).to_have_text(
                "Set by you: later checks keep these times."
            )
        expect(authed_page.locator("#inspAdjust")).to_have_text("Adjust")
        expect(authed_page.locator("#inspUnlock")).to_be_enabled()
        expect(authed_page.locator("#toastTitle")).to_have_text("Saved")
        expect(authed_page.locator("#toastBody")).to_have_text("Plex has them now.")
        assert api.item_requests == [fx.EPISODE, fx.EPISODE]

    @pytest.mark.parametrize(
        ("rows", "message"),
        [
            ([fx.save_row("plex-1", "Plex", "plex")], "Plex has them now."),
            (
                [fx.save_row("plex-1", "Plex", "plex"), fx.save_row("jf-1", "Jellyfin", "jellyfin", "unchanged")],
                "Plex and Jellyfin have them now.",
            ),
            (
                [fx.save_row("jf-1", "Jellyfin", "jellyfin", "waiting")],
                "Your times are saved; Jellyfin gets them on the next job.",
            ),
            (
                [
                    fx.save_row("jf-1", "Jellyfin", "jellyfin", "waiting"),
                    fx.save_row("emby-1", "Emby", "emby", "failed"),
                ],
                "Your times are saved; Jellyfin and Emby get them on the next job.",
            ),
            (
                [fx.save_row("plex-1", "Plex", "plex", replaced_own=["credits"])],
                "Plex has them now. Replaced Plex's own marker. This server is set to keep Plex's, but a marker you "
                "adjust always wins.",
            ),
            (
                [
                    fx.save_row(
                        "emby-1",
                        "Emby",
                        "emby",
                        notes=[{"type": "credits", "field": "end", "note": "Emby skips to the end of the file"}],
                    )
                ],
                "Emby has them now. Your credits end wasn't sent to Emby. Emby skips to the end of the file, past any "
                "scene after the credits.",
            ),
            # A skipped row says nothing of its own, the same as an answer with no rows.
            ([fx.save_row("plex-1", "Plex", "plex", "skipped")], "Saved."),
        ],
        ids=["written", "two-took", "waiting", "waiting-and-failed", "replaced-own", "emby-end-note", "skipped"],
    )
    def test_the_toast_says_what_each_server_did(
        self, authed_page: Page, app_url: str, rows: list[dict], message: str
    ) -> None:
        api = fx.install(authed_page)
        api.answers["save"] = (200, fx.save_answer(fx.EPISODE, rows))
        _open(authed_page, app_url, fx.EPISODE)
        authed_page.locator("#inspAdjust").click()
        with authed_page.expect_response(_is_save):
            authed_page.locator("#inspAdjustSave").click()
        expect(authed_page.locator("#toastTitle")).to_have_text("Saved")
        expect(authed_page.locator("#toastBody")).to_have_text(message)


@pytest.mark.e2e
class TestAdjustEditing:
    """Items 5-10: the runs-to-the-end switch, refusals, a non-time, add cards, nothing to send to, and a recap."""

    def test_turning_off_runs_to_the_end_gives_an_end_edge_that_is_sent(self, authed_page: Page, app_url: str) -> None:
        api = fx.install(authed_page)
        _open(authed_page, app_url, fx.EPISODE)
        authed_page.locator("#inspAdjust").click()
        switch = authed_page.locator("#inspToEnd-credits")
        expect(switch).to_be_checked()
        expect(authed_page.locator("[data-edge='credits-end']")).to_have_count(0)

        switch.click()
        # Ten seconds before the end of a 27:36 file.
        expect(authed_page.locator("[data-edge='credits-end'] .insp-edge-label")).to_have_text("Ends at 27:26")
        expect(authed_page.locator("#inspToEnd-credits")).not_to_be_checked()
        expect(authed_page.locator("#inspCloseups [data-closeup='credits'] .insp-closeup-title")).to_have_text(
            "Credits · 24:59 – 27:26"
        )
        expect(authed_page.locator("#inspAdjustBar")).to_contain_text(
            "Your times: intro 2:45–2:59 · credits 24:59–27:26"
        )
        with authed_page.expect_response(_is_save):
            authed_page.locator("#inspAdjustSave").click()
        assert api.saves == [
            {
                "path": fx.EPISODE,
                "markers": [
                    {"type": "intro", "start_ms": 165_000, "end_ms": 179_000},
                    {"type": "credits", "start_ms": 1_499_000, "end_ms": 1_646_000},
                ],
            }
        ]

    def test_credits_ending_before_they_start_say_so_in_the_plural(self, authed_page: Page, app_url: str) -> None:
        fx.install(authed_page)
        _open(authed_page, app_url, fx.EPISODE)
        authed_page.locator("#inspAdjust").click()
        authed_page.locator("#inspToEnd-credits").uncheck()
        end = authed_page.locator("[data-edge='credits-end'] input")
        end.fill("24:00")
        end.press("Enter")

        expect(authed_page.locator("#inspAdjustBar .text-danger")).to_have_text(
            "The credits have to end after they start."
        )
        expect(authed_page.locator("#inspAdjustSave")).to_be_disabled()

    def test_an_intro_ending_before_it_starts_holds_save_back(self, authed_page: Page, app_url: str) -> None:
        api = fx.install(authed_page)
        _open(authed_page, app_url, fx.EPISODE)
        authed_page.locator("#inspAdjust").click()
        start = authed_page.locator("[data-edge='intro-start'] input")
        start.fill("3:10")
        start.press("Enter")

        bar = authed_page.locator("#inspAdjustBar")
        expect(authed_page.locator("[data-edge='intro-start'] .insp-edge-label")).to_have_text("Starts at 3:10")
        expect(bar.locator(".text-danger")).to_have_text("The intro has to end after it starts.")
        expect(authed_page.locator("#inspAdjustSave")).to_be_disabled()

        end = authed_page.locator("[data-edge='intro-end'] input")
        end.fill("3:20")
        end.press("Enter")
        expect(bar.locator(".text-danger")).to_have_count(0)
        expect(authed_page.locator("#inspAdjustSave")).to_be_enabled()
        assert api.saves == []

    @pytest.mark.parametrize("typed", ["abc", "0:75"])
    def test_a_non_time_is_marked_and_changes_nothing(self, authed_page: Page, app_url: str, typed: str) -> None:
        api = fx.install(authed_page)
        _open(authed_page, app_url, fx.EPISODE)
        authed_page.locator("#inspAdjust").click()
        field = authed_page.locator("[data-edge='intro-start'] input")
        field.fill(typed)
        field.press("Enter")

        expect(field).to_have_class(re.compile(r"\bis-invalid\b"))
        expect(field).to_have_attribute("title", "That isn't a time. Try 1:23 or 0:14.")
        expect(authed_page.locator("[data-edge='intro-start'] .insp-edge-label")).to_have_text("Starts at 2:45")
        expect(authed_page.locator("#inspAdjustBar")).to_contain_text("Your times: intro 2:45–2:59")
        assert api.saves == []

    def test_adding_credits_starts_a_minute_before_the_end_and_runs_to_it(
        self, authed_page: Page, app_url: str
    ) -> None:
        file, item = fx.checked_episode()
        item["decisions"]["credits"] = fx.decision("no_evidence")
        api = fx.install(authed_page, _api_with(file, item))
        _open(authed_page, app_url, fx.EPISODE)
        authed_page.locator("#inspAdjust").click()

        expect(authed_page.locator("[data-add='intro']")).to_have_count(0)
        expect(authed_page.locator("[data-add='credits']")).to_contain_text("Credits · not found")
        authed_page.locator("#inspAdd-credits").click()
        expect(authed_page.locator("[data-edge='credits-start'] .insp-edge-label")).to_have_text("Starts at 26:36")
        expect(authed_page.locator("#inspToEnd-credits")).to_be_checked()
        expect(authed_page.locator("[data-edge='credits-end']")).to_have_count(0)
        with authed_page.expect_response(_is_save):
            authed_page.locator("#inspAdjustSave").click()
        assert api.saves == [
            {
                "path": fx.EPISODE,
                "markers": [
                    {"type": "intro", "start_ms": 165_000, "end_ms": 179_000},
                    {"type": "credits", "start_ms": 1_596_000, "end_ms": None},
                ],
            }
        ]

    def test_a_film_is_never_offered_an_intro(self, authed_page: Page, app_url: str) -> None:
        file, item = fx.review_film()
        item["decisions"]["credits"] = fx.decision(
            "decided", fx.marker("credits", 5_557_000, fx.REVIEW_MS, ["credits_text"])
        )
        item["decisions"]["intro"] = fx.decision("no_evidence")
        fx.install(authed_page, _api_with(file, item))
        _open(authed_page, app_url, fx.REVIEW)
        authed_page.locator("#inspAdjust").click()
        expect(authed_page.locator("#inspCloseups [data-closeup='credits']")).to_be_visible()
        expect(authed_page.locator("[data-add='intro']")).to_have_count(0)

    def test_a_type_no_enabled_server_can_show_gets_no_add_card(self, authed_page: Page, app_url: str) -> None:
        file, item = fx.checked_episode()
        item["decisions"]["intro"] = fx.decision("no_evidence")
        item["decisions"]["credits"] = fx.decision("no_evidence")
        item["servers"][0]["can_show"] = ["credits"]
        item["servers"][1]["markers_enabled"] = False
        fx.install(authed_page, _api_with(file, item))
        _open(authed_page, app_url, fx.EPISODE)
        expect(authed_page.locator("#inspSummary")).to_have_text(
            "No intro or credits were found for this file. Adjust adds them by hand."
        )
        authed_page.locator("#inspAdjust").click()

        expect(authed_page.locator("[data-add='credits']")).to_be_visible()
        expect(authed_page.locator("[data-add='intro']")).to_have_count(0)
        expect(authed_page.locator("#inspAdjustBar .fw-semibold")).to_have_text("Nothing to save yet")
        expect(authed_page.locator("#inspAdjustSave")).to_be_disabled()

    def test_with_intro_and_credits_off_everywhere_nothing_can_be_adjusted(
        self, authed_page: Page, app_url: str
    ) -> None:
        file, item = fx.checked_episode()
        for server in item["servers"]:
            server["markers_enabled"] = False
        api = fx.install(authed_page, _api_with(file, item))
        _open(authed_page, app_url, fx.EPISODE)
        authed_page.locator("#inspAdjust").click()

        expect(authed_page.locator("[data-cant-adjust='intro']")).to_contain_text(
            "No server with Intro & Credits on for this file shows intros, so there is nothing to send it to and it "
            "can't be adjusted here."
        )
        expect(authed_page.locator("[data-cant-adjust='credits']")).to_contain_text("shows credits")
        expect(authed_page.locator("#inspCloseups [data-closeup]")).to_have_count(0)
        expect(authed_page.locator("#inspCloseups [data-add]")).to_have_count(0)
        expect(authed_page.locator("#inspAdjustBar .fw-semibold")).to_have_text("Nothing to save yet")
        save = authed_page.locator("#inspAdjustSave")
        expect(save).to_be_disabled()
        expect(save).to_have_text("Save and send to your servers")
        assert api.saves == []

    def test_a_recap_jellyfin_shows_is_edited_and_sent(self, authed_page: Page, app_url: str) -> None:
        file, item = fx.checked_episode()
        item["decisions"]["recap"] = fx.decision("decided", fx.marker("recap", 0, 60_000, ["chapters"]))
        api = fx.install(authed_page, _api_with(file, item))
        _open(authed_page, app_url, fx.EPISODE)
        authed_page.locator("#inspAdjust").click()

        expect(authed_page.locator("[data-edge='recap-start'] .insp-edge-label")).to_have_text("Starts at 0:00")
        expect(authed_page.locator("[data-edge='recap-end'] .insp-edge-label")).to_have_text("Ends at 1:00")
        expect(authed_page.locator("[data-cant-adjust='recap']")).to_have_count(0)
        expect(authed_page.locator("#inspAdjustBar")).to_contain_text("recap 0:00–1:00")
        with authed_page.expect_response(_is_save):
            authed_page.locator("#inspAdjustSave").click()
        assert api.saves == [
            {
                "path": fx.EPISODE,
                "markers": [
                    {"type": "intro", "start_ms": 165_000, "end_ms": 179_000},
                    {"type": "credits", "start_ms": 1_499_000, "end_ms": None},
                    {"type": "recap", "start_ms": 0, "end_ms": 60_000},
                ],
            }
        ]


@pytest.mark.e2e
class TestBackToAutomatic:
    """Item 11: Keep them locked, a refused unlock, and a successful one that reads the file again."""

    def test_keep_them_locked_sends_nothing_and_a_refused_unlock_stays_locked(
        self, authed_page: Page, app_url: str
    ) -> None:
        file, item = fx.checked_episode()
        api = fx.install(authed_page, _api_with(file, _locked(item, "credits")))
        api.answers["unlock"] = (409, {"error": "These times changed since the page read them"})
        _open(authed_page, app_url, fx.EPISODE)

        authed_page.locator("#inspUnlock").click()
        authed_page.get_by_role("button", name="Keep them locked").click()
        expect(authed_page.locator("#inspUnlockConfirm")).to_have_count(0)
        expect(authed_page.locator("#inspLocked")).not_to_contain_text("Back to automatic?")

        authed_page.locator("#inspUnlock").click()
        with authed_page.expect_response(_is_unlock):
            authed_page.locator("#inspUnlockConfirm").click()
        expect(authed_page.locator("#toastTitle")).to_have_text("Back to automatic")
        expect(authed_page.locator("#toastBody")).to_have_text(
            "Couldn't do it: These times changed since the page read them"
        )
        expect(authed_page.locator("#inspLocked")).to_contain_text("You set the credits, so later checks keep them.")
        # One DELETE: "Keep them locked" sent nothing.
        assert api.unlocks == [{"path": fx.EPISODE, "types": ["credits"]}]
        assert api.item_requests == [fx.EPISODE]

    def test_back_to_automatic_reads_the_file_again(self, authed_page: Page, app_url: str) -> None:
        file, item = fx.checked_episode()
        api = _api_with(file, _locked(item, "intro", "credits"))
        api.after_unlock[fx.EPISODE] = item
        fx.install(authed_page, api)
        _open(authed_page, app_url, fx.EPISODE)

        authed_page.locator("#inspUnlock").click()
        with authed_page.expect_response(_is_unlock):
            authed_page.locator("#inspUnlockConfirm").click()
        expect(authed_page.locator("#toastBody")).to_have_text("The next check decides these times again.")
        expect(authed_page.locator("#inspLocked")).to_have_count(0)
        expect(authed_page.locator("#inspLock")).to_be_visible()
        assert api.unlocks == [{"path": fx.EPISODE, "types": ["intro", "credits"]}]
        assert api.item_requests == [fx.EPISODE, fx.EPISODE]


@pytest.mark.e2e
class TestReviewOtherTypes:
    """Items 12-13: Needs your check for an intro, a credits answer with its own end, and every heading."""

    @pytest.mark.parametrize(
        ("choice", "start", "end"),
        [("Intro starts at 2:45", 165_000, 179_000), ("Intro starts at 3:10", 190_000, 222_000)],
    )
    def test_choosing_an_intro_answer_keeps_its_length(
        self, authed_page: Page, app_url: str, choice: str, start: int, end: int
    ) -> None:
        api = fx.install(authed_page, _intro_review())
        _open(authed_page, app_url, fx.EPISODE)
        panel = authed_page.locator("[data-review='intro']")
        expect(panel.locator(".insp-review-title")).to_have_text(
            "Where does the intro start? Two answers disagree by 25 seconds."
        )
        expect(panel.locator("[data-candidate]").nth(0)).to_contain_text("From the theme music heard across the season")
        expect(panel.locator("[data-candidate]").nth(1)).to_contain_text("From TheIntroDB")
        # No Adjust while something needs your check.
        expect(authed_page.locator("#inspAdjust")).to_have_count(0)

        panel.get_by_role("button", name=choice).click()
        expect(authed_page.locator("[data-confirm='intro']")).to_contain_text(f"Selected: {choice.lower()}")
        save = authed_page.locator("[data-save-review='intro']")
        expect(save).to_have_text("Save and send to Plex and Jellyfin")
        with authed_page.expect_response(_is_save):
            save.click()
        assert api.saves == [{"path": fx.EPISODE, "markers": [{"type": "intro", "start_ms": start, "end_ms": end}]}]

    def test_an_intro_picked_by_hand_is_thirty_seconds_long(self, authed_page: Page, app_url: str) -> None:
        api = fx.install(authed_page, _intro_review())
        _open(authed_page, app_url, fx.EPISODE)
        pick = authed_page.locator("[data-pick-yourself='intro']")
        pick.get_by_role("button", name="Frame at 2:50: the intro starts here").click()
        expect(authed_page.locator("[data-confirm='intro']")).to_contain_text("Selected: intro starts at 2:50")
        with authed_page.expect_response(_is_save):
            authed_page.locator("[data-save-review='intro']").click()
        assert api.saves == [
            {"path": fx.EPISODE, "markers": [{"type": "intro", "start_ms": 170_000, "end_ms": 200_000}]}
        ]

    @pytest.mark.parametrize(
        ("answer_end", "sent_end"),
        # 5,819,000 is within two seconds of the 1:37:00 end: that is "runs to the end", sent as null.
        [(5_700_000, 5_700_000), (5_819_000, None)],
    )
    def test_a_credits_answer_that_ends_before_the_file_sends_its_end(
        self, authed_page: Page, app_url: str, answer_end: int, sent_end: int | None
    ) -> None:
        file, item = fx.review_film()
        item["evidence"][0]["end_ms"] = answer_end
        api = fx.install(authed_page, _api_with(file, item))
        _open(authed_page, app_url, fx.REVIEW)
        authed_page.locator("[data-review='credits']").get_by_role("button", name="Credits start at 1:32:09").click()
        with authed_page.expect_response(_is_save):
            authed_page.locator("[data-save-review='credits']").click()
        assert api.saves == [
            {"path": fx.REVIEW, "markers": [{"type": "credits", "start_ms": 5_529_000, "end_ms": sent_end}]}
        ]

    @pytest.mark.parametrize(
        ("evidence", "proposed", "heading", "starts", "pick_title"),
        [
            (
                [("chapters", 5_529_000)],
                {"start_ms": 5_529_000, "end_ms": None, "decided_by": ["chapters"]},
                "Where do the credits start? Only one answer came in, and it can't decide on its own.",
                ["1:32:09"],
                "Neither is right? Pick the frame yourself.",
            ),
            (
                [],
                None,
                "Where do the credits start? Nothing found an answer to check.",
                [],
                "Pick the frame yourself.",
            ),
            (
                [("chapters", 5_529_000)],
                {"start_ms": 5_600_000, "end_ms": None, "decided_by": ["credits_text"]},
                "Where do the credits start? Two answers disagree by 71 seconds.",
                ["1:32:09", "1:33:20"],
                "Neither is right? Pick the frame yourself.",
            ),
        ],
        ids=["one-answer", "no-answer", "proposed-not-in-evidence"],
    )
    def test_the_heading_says_how_many_answers_came_in(
        self,
        authed_page: Page,
        app_url: str,
        evidence: list[tuple[str, int]],
        proposed: dict | None,
        heading: str,
        starts: list[str],
        pick_title: str,
    ) -> None:
        file, item = fx.review_film()
        item["evidence"] = [fx.evidence_row(src, "credits", at, None) for src, at in evidence]
        item["decisions"]["credits"]["proposed"] = proposed
        fx.install(authed_page, _api_with(file, item))
        _open(authed_page, app_url, fx.REVIEW)

        panel = authed_page.locator("[data-review='credits']")
        expect(panel.locator(".insp-review-title")).to_have_text(heading)
        expect(panel.locator("[data-candidate] > .insp-mono")).to_have_text(starts)
        expect(panel.locator("[data-pick-yourself='credits'] .fw-semibold")).to_have_text(pick_title)
        if proposed and proposed["start_ms"] == 5_600_000:
            expect(panel.locator("[data-candidate='5600000']")).to_contain_text("From the credits read on screen")


@pytest.mark.e2e
class TestHeaderActions:
    """Item 14: Regenerate and Re-detect failing, and both held back while one is on its way."""

    @pytest.mark.parametrize(
        ("button", "key", "title", "error"),
        [
            ("#inspRegenerate", "manual", "Regenerate preview", "The job queue is full"),
            ("#inspRedetect", "redetect", "Intro & credits", "A job is already checking this file"),
        ],
    )
    def test_a_refused_job_says_why_and_shows_no_banner(
        self, authed_page: Page, app_url: str, button: str, key: str, title: str, error: str
    ) -> None:
        api = fx.install(authed_page)
        api.answers[key] = (409, {"error": error})
        _open(authed_page, app_url, fx.EPISODE)
        url = "/api/jobs/manual" if key == "manual" else "/api/markers/item/redetect"
        with authed_page.expect_response(lambda r: r.url.endswith(url)):
            authed_page.locator(button).click()

        expect(authed_page.locator("#toastTitle")).to_have_text(title)
        expect(authed_page.locator("#toastBody")).to_have_text(f"Couldn't queue it: {error}")
        expect(authed_page.locator("#inspJobBanner")).to_be_hidden()
        expect(authed_page.locator(button)).to_be_enabled()
        if key == "manual":
            assert api.manual_jobs == [{"file_paths": [fx.EPISODE], "force_regenerate": True, "priority": 1}]
        else:
            assert api.redetects == [{"path": fx.EPISODE}]

    @pytest.mark.parametrize(("button", "key"), [("#inspRegenerate", "manual"), ("#inspRedetect", "redetect")])
    def test_both_actions_wait_while_one_is_queueing(
        self, authed_page: Page, app_url: str, button: str, key: str
    ) -> None:
        api = fx.install(authed_page)
        api.hold_writes = {key}
        _open(authed_page, app_url, fx.EPISODE)
        authed_page.locator(button).click()

        expect(authed_page.locator("#inspRegenerate")).to_be_disabled()
        expect(authed_page.locator("#inspRedetect")).to_be_disabled()
        _wait_until(authed_page, lambda: len(api.held_writes) == 1)
        api.release_writes()
        expect(authed_page.locator("#inspRegenerate")).to_be_enabled()
        expect(authed_page.locator("#inspRedetect")).to_be_enabled()
        expect(authed_page.locator("#inspJobBanner")).to_contain_text("Queued for this file")

    @pytest.mark.parametrize(("button", "key"), [("#inspRegenerate", "manual"), ("#inspRedetect", "redetect")])
    def test_a_late_queue_answer_puts_no_banner_on_the_next_file(
        self, authed_page: Page, app_url: str, button: str, key: str
    ) -> None:
        api = fx.install(authed_page)
        api.hold_writes = {key}
        _open(authed_page, app_url, fx.EPISODE)
        authed_page.locator(button).click()
        _wait_until(authed_page, lambda: len(api.held_writes) == 1)

        authed_page.locator("#inspShowResults").click()
        authed_page.locator("#inspQuery").fill(fx.FILM)
        authed_page.locator("#inspQuery").press("Enter")
        expect(authed_page.locator("#inspTitle")).to_have_text("The Matrix (1999)")
        url = "/api/jobs/manual" if key == "manual" else "/api/markers/item/redetect"
        with authed_page.expect_response(lambda r: r.url.endswith(url)):
            api.release_writes()
        authed_page.wait_for_timeout(300)
        expect(authed_page.locator("#inspJobBanner")).to_be_hidden(timeout=1000)


@pytest.mark.e2e
class TestJobsSocket:
    """Item 15: the /jobs socket's progress, completion, other files' jobs, and a job that touches this file."""

    @staticmethod
    def _running_job_api() -> fx.InspectorApi:
        file, item = fx.checked_episode()
        file["job"] = {
            "id": "job-9",
            "kind": "intro_credits",
            "status": "running",
            "name": "Intro & Credits: 1 file",
            "percent": 42.0,
        }
        return _api_with(file, item)

    @staticmethod
    def _open_with_socket(page: Page, app_url: str, api: fx.InspectorApi) -> None:
        page.add_init_script(_SOCKET_STUB)
        fx.install(page, api)
        _open(page, app_url, fx.EPISODE)

    def test_progress_for_the_tracked_job_updates_the_banner(self, authed_page: Page, app_url: str) -> None:
        self._open_with_socket(authed_page, app_url, self._running_job_api())
        sockets = authed_page.evaluate(
            "window.__inspectorSockets.map((s) => [s.namespace, s.options, Object.keys(s.handlers).sort()])"
        )
        assert sockets == [
            [
                "/jobs",
                {"transports": ["polling"], "reconnection": True},
                sorted(
                    [
                        "job_created",
                        "job_started",
                        "job_updated",
                        "job_completed",
                        "job_failed",
                        "job_cancelled",
                        "job_progress",
                    ]
                ),
            ]
        ]
        text = authed_page.locator("#inspJobBanner .alert > span:not(.spinner-border)")
        expect(text).to_have_text("Working on this file: Intro & Credits job “Intro & Credits: 1 file” · 42%")

        _emit(
            authed_page,
            "job_progress",
            {"job_id": "job-other", "progress": {"percent": 90, "current_file": "/data/tv/Other/Other - S01E01.mkv"}},
        )
        expect(text).to_have_text("Working on this file: Intro & Credits job “Intro & Credits: 1 file” · 42%")
        _emit(
            authed_page, "job_progress", {"job_id": "job-9", "progress": {"percent": 73.4, "current_file": fx.EPISODE}}
        )
        expect(text).to_have_text("Working on this file: Intro & Credits job “Intro & Credits: 1 file” · 73%")

    def test_the_tracked_job_ending_clears_the_banner_and_reads_the_file_again(
        self, authed_page: Page, app_url: str
    ) -> None:
        api = self._running_job_api()
        self._open_with_socket(authed_page, app_url, api)
        api.files[fx.EPISODE]["job"] = None
        with authed_page.expect_response(lambda r: "/api/markers/item?" in r.url):
            _emit(authed_page, "job_completed", {"id": "job-9", "kind": "intro_credits", "status": "completed"})
            expect(authed_page.locator("#inspJobBanner")).to_be_hidden()
        assert api.item_requests == [fx.EPISODE, fx.EPISODE]
        expect(authed_page.locator("#inspJobBanner")).to_be_hidden()

    def test_a_job_ending_while_adjusting_does_not_read_the_file_again(self, authed_page: Page, app_url: str) -> None:
        api = self._running_job_api()
        self._open_with_socket(authed_page, app_url, api)
        authed_page.locator("#inspAdjust").click()
        _emit(authed_page, "job_completed", {"id": "job-9", "kind": "intro_credits", "status": "completed"})
        expect(authed_page.locator("#inspJobBanner")).to_be_hidden()
        # The re-read would come 500 ms after the event; the edit in progress must survive it.
        authed_page.wait_for_timeout(1200)
        assert api.item_requests == [fx.EPISODE]
        expect(authed_page.locator("#inspAdjustBar")).to_be_visible()

    def test_a_job_on_another_file_is_ignored(self, authed_page: Page, app_url: str) -> None:
        file, item = fx.checked_episode()
        api = _api_with(file, item)
        self._open_with_socket(authed_page, app_url, api)
        other = {"file_paths": ["/data/tv/Other/Other - S01E01.mkv"]}
        _emit(
            authed_page,
            "job_updated",
            {"id": "job-x", "kind": "previews", "status": "running", "library_name": "Other", "config": other},
        )
        expect(authed_page.locator("#inspJobBanner")).to_be_hidden()
        _emit(authed_page, "job_completed", {"id": "job-x", "kind": "previews", "config": other})
        authed_page.wait_for_timeout(1200)
        assert api.item_requests == [fx.EPISODE]
        expect(authed_page.locator("#inspJobBanner")).to_be_hidden()

    @pytest.mark.parametrize(
        ("event", "touch", "suffix"),
        [
            ("job_updated", {"config": {"file_paths": [fx.EPISODE]}, "progress": {"percent": 12}}, " · 12%"),
            ("job_created", {"config": {"webhook_paths": [fx.EPISODE]}}, ""),
            ("job_started", {"progress": {"percent": 30, "current_file": fx.EPISODE}}, " · 30%"),
            (
                "job_updated",
                {
                    "progress": {
                        "percent": 55,
                        "workers": [{"current_file": "/data/x.mkv"}, {"current_file": fx.EPISODE}],
                    }
                },
                " · 55%",
            ),
        ],
        ids=["file_paths", "webhook_paths", "current_file", "a-worker"],
    )
    def test_a_job_that_touches_this_file_shows_the_banner(
        self, authed_page: Page, app_url: str, event: str, touch: dict, suffix: str
    ) -> None:
        file, item = fx.checked_episode()
        self._open_with_socket(authed_page, app_url, _api_with(file, item))
        expect(authed_page.locator("#inspJobBanner")).to_be_hidden()
        _emit(
            authed_page,
            event,
            {"id": "job-7", "kind": "previews", "status": "running", "library_name": "Nightly scan", **touch},
        )
        banner = authed_page.locator("#inspJobBanner")
        expect(banner.locator(".alert > span:not(.spinner-border)")).to_have_text(
            f"Working on this file: Preview job “Nightly scan”{suffix}"
        )
        expect(banner.get_by_role("link", name="Open on the Dashboard")).to_have_attribute("href", "/?job=job-7")


@pytest.mark.e2e
class TestReadFailures:
    """Items 16, 17 and 23: Intro & Credits unreadable, the file unreadable, a late answer, and an unknown length."""

    def test_intro_and_credits_unreadable_offers_a_check(self, authed_page: Page, app_url: str) -> None:
        file, _item = fx.checked_episode()
        fx.install(authed_page, _api_with(file, (502, {"error": "Plex timed out"})))
        _open(authed_page, app_url, fx.EPISODE)
        card = authed_page.locator("#inspSummaryCard")
        expect(card.locator(".fw-semibold").first).to_have_text("Intro & Credits couldn't be read for this file")
        expect(card.locator(":scope > .insp-small")).to_have_text("Plex timed out")
        expect(authed_page.locator("#inspRedetect")).to_have_text("Check intro & credits now")
        expect(authed_page.locator("#inspAdjust")).to_have_count(0)
        expect(authed_page.locator("#inspLock")).to_have_count(0)

    def test_the_file_unreadable_says_so_with_no_actions(self, authed_page: Page, app_url: str) -> None:
        api = fx.install(authed_page)
        api.files[fx.EPISODE] = (500, {"error": "Couldn't read the library: database is locked"})
        _open(authed_page, app_url, fx.EPISODE)
        card = authed_page.locator('[data-state="Couldn\'t open this file"]')
        expect(card).to_contain_text("Couldn't read the library: database is locked")
        expect(authed_page.locator("#inspRegenerate")).to_have_count(0)
        expect(authed_page.locator("#inspSummaryCard")).to_have_count(0)

    def test_a_late_answer_does_not_replace_the_file_now_open(self, authed_page: Page, app_url: str) -> None:
        api = fx.install(authed_page)
        api.hold_paths = {fx.FILM}
        authed_page.goto(f"{app_url}/inspector?path={quote(fx.FILM)}")
        expect(authed_page.locator("#inspLoading")).to_be_visible()
        _wait_until(authed_page, lambda: fx.FILM in api.held_paths)

        authed_page.locator("#inspShowResults").click()
        authed_page.locator("#inspQuery").fill(fx.EPISODE)
        authed_page.locator("#inspQuery").press("Enter")
        expect(authed_page.locator("#inspTitle")).to_have_text("Blood Legacy (2024) · S01E01")
        expect(authed_page.locator("#inspSummary")).to_contain_text("Skip Intro runs 2:45 – 2:59")

        with authed_page.expect_response(
            lambda r: "/api/inspector/file?" in r.url and parse_qs(urlparse(r.url).query).get("path") == [fx.FILM]
        ):
            api.release_path(fx.FILM)
        authed_page.wait_for_timeout(300)
        expect(authed_page.locator("#inspTitle")).to_have_text("Blood Legacy (2024) · S01E01")
        expect(authed_page.locator("#inspPath")).to_have_text(fx.EPISODE)
        expect(authed_page.locator("#inspSummary")).to_contain_text("Skip Intro runs 2:45 – 2:59")
        assert parse_qs(urlparse(authed_page.url).query) == {"path": [fx.EPISODE]}

    def test_a_file_of_unknown_length_has_no_timeline_adjust_or_close_ups(
        self, authed_page: Page, app_url: str
    ) -> None:
        file, item = fx.checked_episode()
        _no_preview(file)
        file["duration_ms"] = None
        item["duration_ms"] = None
        fx.install(authed_page, _api_with(file, item))
        _open(authed_page, app_url, fx.EPISODE)
        expect(authed_page.locator("#inspWholeFile")).to_contain_text(
            "This file's length isn't known yet, so there is no timeline. Checking its intro & credits reads it."
        )
        expect(authed_page.locator("#inspRedetect")).to_be_visible()
        expect(authed_page.locator("#inspAdjust")).to_have_count(0)
        expect(authed_page.locator("#inspCloseups")).to_have_count(0)


def _plan_cells() -> list[tuple[str, str, str, str, str, str | None]]:
    return [
        (
            "will_replace",
            "plex-1",
            "Plex",
            "plex",
            "Intro & credits: the next job replaces what it shows",
            "Plex shows other times; the next job replaces them.",
        ),
        (
            "will_remove",
            "plex-1",
            "Plex",
            "plex",
            "Intro & credits: the next job removes the ones this app sent",
            "Plex loses the markers this app sent, on the next job.",
        ),
        (
            "waiting",
            "plex-1",
            "Plex",
            "plex",
            "Intro & credits: waiting for its other versions to agree",
            "Plex is waiting for its other versions to agree.",
        ),
        ("keeps_plex", "plex-1", "Plex", "plex", "Intro & credits: keeps Plex's own", "Plex keeps its own markers."),
        ("keeps_emby", "emby-1", "Emby", "emby", "Intro & credits: keeps Emby's own", "Emby keeps its own markers."),
        (
            "not_enabled",
            "plex-1",
            "Plex",
            "plex",
            "Intro & Credits is off here",
            "Plex has Intro & Credits turned off.",
        ),
        ("nothing_to_publish", "plex-1", "Plex", "plex", "Intro & credits: nothing to send yet", None),
        (
            "unknown",
            "plex-1",
            "Plex",
            "plex",
            "Intro & credits: couldn't read what it shows",
            "Plex couldn't be read just now.",
        ),
    ]


@pytest.mark.e2e
class TestServersCard:
    """Items 18-19: every plan's words on the servers card and in the summary, and each row's extra lines."""

    @pytest.mark.parametrize(
        ("plan", "sid", "name", "stype", "card_words", "sentence"),
        _plan_cells(),
        ids=[c[0] for c in _plan_cells()],
    )
    def test_each_plan_reads_the_same_on_the_card_and_in_the_summary(
        self,
        authed_page: Page,
        app_url: str,
        plan: str,
        sid: str,
        name: str,
        stype: str,
        card_words: str,
        sentence: str | None,
    ) -> None:
        file, item = fx.checked_episode()
        item["servers"][0] = fx.server_row(sid, name, stype, plan=plan, current=[])
        fx.install(authed_page, _api_with(file, item))
        _open(authed_page, app_url, fx.EPISODE)

        row = authed_page.locator(f"#inspServers .insp-server[data-server-id='{sid}']")
        expect(row.locator(".d-flex > div").first).to_have_text(card_words)
        said = f" {sentence}" if sentence else ""
        expect(authed_page.locator("#inspSummary")).to_have_text(
            f"Skip Intro runs 2:45 – 2:59 and Skip Credits starts at 24:59.{said} Jellyfin gets them on the next job."
        )

    @pytest.mark.parametrize(
        ("plan", "sentence"),
        [
            ("keeps_plex", "Plex and Emby keep their own markers."),
            ("waiting", "Plex and Emby are waiting for their other versions to agree."),
            ("will_add", "Plex and Emby get them on the next job."),
        ],
    )
    def test_servers_in_the_same_state_share_one_sentence(
        self, authed_page: Page, app_url: str, plan: str, sentence: str
    ) -> None:
        file, item = fx.checked_episode()
        emby_plan = "keeps_emby" if plan == "keeps_plex" else plan
        item["servers"] = [
            fx.server_row("plex-1", "Plex", "plex", plan=plan, current=[]),
            fx.server_row("emby-1", "Emby", "emby", plan=emby_plan, current=[]),
        ]
        fx.install(authed_page, _api_with(file, item))
        _open(authed_page, app_url, fx.EPISODE)

        expect(authed_page.locator("#inspSummary")).to_have_text(
            f"Skip Intro runs 2:45 – 2:59 and Skip Credits starts at 24:59. {sentence}"
        )

    def test_reasons_publish_messages_versions_and_file_locations(self, authed_page: Page, app_url: str) -> None:
        file, item = fx.checked_episode()
        emby_bif = f"{os.path.dirname(fx.EPISODE)}/Blood Legacy (2024) - S01E01 - [WEBDL-1080p]-320-10.bif"
        file["previews"].append(
            fx.preview_row("emby-1", "Emby", "emby", path=emby_bif, exists=True, frame_count=166, interval_ms=10_000)
        )
        item["servers"] = [
            fx.server_row(
                "plex-1",
                "Plex",
                "plex",
                plan="will_add",
                plan_reason="The file was replaced since the last job",
                publish_status="failed",
                publish_message="Plex answered HTTP 500 when the markers were sent",
                version_count=2,
            ),
            fx.server_row(
                "jf-1",
                "Jellyfin",
                "jellyfin",
                plan="waiting",
                publish_status="waiting",
                publish_message="Waiting for Jellyfin to add the file",
                version_count=2,
            ),
            fx.server_row(
                "emby-1",
                "Emby",
                "emby",
                plan="nothing_to_publish",
                publish_status="skipped",
                publish_message="Skipped: Emby shows no recaps",
            ),
            fx.server_row(
                "plex-2",
                "Plex 4K",
                "plex",
                plan="up_to_date",
                publish_status="written",
                publish_message="Sent 2 markers",
            ),
        ]
        fx.install(authed_page, _api_with(file, item))
        _open(authed_page, app_url, fx.EPISODE)

        plex = authed_page.locator("#inspServers .insp-server[data-server-id='plex-1']")
        expect(plex).to_contain_text("The file was replaced since the last job")
        expect(plex).to_contain_text("Plex answered HTTP 500 when the markers were sent")
        expect(plex).to_contain_text("All versions of this item share one set of markers")

        jf = authed_page.locator("#inspServers .insp-server[data-server-id='jf-1']")
        expect(jf).to_contain_text("Waiting for Jellyfin to add the file")
        # Only Plex keeps one set of markers for every version.
        expect(jf).not_to_contain_text("All versions of this item")
        jf.locator("summary", has_text="File locations").click()
        expect(jf.locator("details div")).to_have_text(f"Trickplay folder: {file['previews'][1]['path']}")

        emby = authed_page.locator("#inspServers .insp-server[data-server-id='emby-1']")
        expect(emby).to_contain_text("Skipped: Emby shows no recaps")
        expect(emby).to_contain_text("Preview in place · 166 frames")
        emby.locator("summary", has_text="File locations").click()
        expect(emby.locator("details div")).to_have_text(f"Preview (next to the video): {emby_bif}")

        plex4k = authed_page.locator("#inspServers .insp-server[data-server-id='plex-2']")
        expect(plex4k).to_contain_text("Intro & credits up to date · sent by this app")
        # A written publish needs no message of its own.
        expect(plex4k).not_to_contain_text("Sent 2 markers")
        expect(plex4k).not_to_contain_text("All versions of this item")
        expect(plex4k).to_contain_text("Preview: not looked up")


@pytest.mark.e2e
class TestLanesAndNotChecked:
    """Items 20-21: server lanes and close-ups in every state, and the not-checked summary's variants."""

    def test_lanes_and_close_ups_say_where_each_marker_came_from(self, authed_page: Page, app_url: str) -> None:
        file, item = fx.unchecked_film()
        item["servers"] = [
            fx.server_row(
                "plex-1",
                "Plex",
                "plex",
                current=[{"type": "credits", "start_ms": 7_768_000, "end_ms": None, "ours": False, "stale": True}],
                duration_ms=fx.FILM_MS,
            ),
            fx.server_row(
                "jf-1",
                "Jellyfin",
                "jellyfin",
                current=[{"type": "intro", "start_ms": 60_000, "end_ms": 90_000, "ours": True, "stale": False}],
            ),
            fx.server_row("emby-1", "Emby", "emby", markers_enabled=False, current=[]),
            fx.server_row("jf-2", "Jellyfin Kids", "jellyfin", current=[]),
        ]
        fx.install(authed_page, _api_with(file, item))
        _open(authed_page, app_url, fx.FILM)

        lanes = authed_page.locator("#inspWholeFile")
        expect(lanes.locator(".insp-lane[data-server-id='emby-1']")).to_have_text(
            "Nothing here · Intro & Credits is off for it"
        )
        expect(lanes.locator(".insp-lane[data-server-id='jf-2']")).to_have_text("Nothing yet")
        expect(lanes.locator(".insp-lane[data-server-id='plex-1'] .insp-lane-label")).to_have_text(
            "Credits 2:09:28 → end (made for an earlier file)"
        )
        expect(lanes.locator(".insp-lane[data-server-id='jf-1'] .insp-lane-label")).to_have_text("Intro 1:00–1:30")

        closeups = authed_page.locator("#inspCloseups [data-closeup='server']")
        expect(closeups).to_have_count(2)
        expect(closeups.nth(0).locator(".insp-closeup-title")).to_have_text("Jellyfin ① starts 1:00")
        expect(closeups.nth(0).locator(".insp-frames-note")).to_have_text("Sent there by this app earlier.")
        expect(closeups.nth(1).locator(".insp-closeup-title")).to_have_text("Plex ① starts 2:09:28")
        expect(closeups.nth(1).locator(".insp-frames-note")).to_have_text(
            "Plex's own marker, made for an earlier file at this path."
        )

    def test_the_decided_lane_says_nothing_yet_for_a_server_that_shows_none_of_the_decided_types(
        self, authed_page: Page, app_url: str
    ) -> None:
        file, item = fx.checked_episode()
        item["servers"][1]["can_show"] = ["recap"]
        fx.install(authed_page, _api_with(file, item))
        _open(authed_page, app_url, fx.EPISODE)
        expect(authed_page.locator("#inspWholeFile .insp-lane[data-server-id='jf-1']")).to_have_text("Nothing yet")

    @pytest.mark.parametrize(
        ("servers", "text"),
        [
            (
                [fx.server_row("plex-1", "Plex", "plex", keeps_server_markers=True, current=[])],
                "No server shows intro or credits markers for this file yet. Checking the film decides its own.",
            ),
            (
                [
                    fx.server_row("plex-1", "Plex", "plex", current=[]),
                    fx.server_row("jf-1", "Jellyfin", "jellyfin", current=None, error="Couldn't reach Jellyfin"),
                ],
                "Checking the film decides its own.",
            ),
            (
                [
                    fx.server_row(
                        "emby-1",
                        "Emby Den",
                        "emby",
                        keeps_server_markers=True,
                        current=[
                            {"type": "intro", "start_ms": 60_000, "end_ms": 90_000, "ours": False, "stale": False}
                        ],
                    ),
                    fx.server_row(
                        "emby-2",
                        "Emby Loft",
                        "emby",
                        keeps_server_markers=True,
                        markers_enabled=False,
                        current=[
                            {"type": "intro", "start_ms": 60_000, "end_ms": 90_000, "ours": False, "stale": False}
                        ],
                    ),
                ],
                "Emby Den shows 1 intro marker today and Emby Loft shows 1 intro marker today, drawn in grey on the "
                "frames below so you can see where they land. Checking the film decides its own. With “Keep Emby's” "
                "on, Emby Den's markers stay as they are.",
            ),
        ],
        ids=["every-server-read-none-shown", "one-server-unread", "emby-keeps-its-own"],
    )
    def test_the_not_checked_summary(self, authed_page: Page, app_url: str, servers: list[dict], text: str) -> None:
        file, item = fx.unchecked_film()
        item["servers"] = servers
        fx.install(authed_page, _api_with(file, item))
        _open(authed_page, app_url, fx.FILM)
        expect(authed_page.locator("#inspSummaryCard > div.mt-1")).to_have_text(text)


@pytest.mark.e2e
class TestEvidenceNotes:
    """Item 22: agreement at the 5 s (intro) and 10 s (credits) edges, a review answer, and the empty card."""

    def test_answers_agree_up_to_the_tolerance_and_not_past_it(self, authed_page: Page, app_url: str) -> None:
        file, item = fx.checked_episode()
        item["evidence"] = [
            fx.evidence_row("season_audio", "intro", 165_000, 179_000, label="20/20"),
            fx.evidence_row("chapters", "intro", 170_000, 184_000),
            fx.evidence_row("theintrodb", "intro", 171_000, 185_000),
            fx.evidence_row("chapters", "credits", 1_499_000, None, label="Credits"),
            fx.evidence_row("credits_text", "credits", 1_502_000, None),
            fx.evidence_row("skipdb", "credits", 1_509_000, None),
            fx.evidence_row("introdb", "credits", 1_510_000, None),
        ]
        fx.install(authed_page, _api_with(file, item))
        _open(authed_page, app_url, fx.EPISODE)

        evidence = authed_page.locator("#inspEvidence")
        cells = [
            (
                evidence.locator(".insp-ev[data-source='chapters']", has_text="Intro 2:50–3:04"),
                "Agrees with the season audio, so that answer is kept",
                "bi-check-lg",
            ),
            (
                evidence.locator(".insp-ev[data-source='theintrodb']"),
                "6 s from the decision, so not used",
                "bi-exclamation-triangle",
            ),
            (
                evidence.locator(".insp-ev[data-source='skipdb']"),
                "Agrees with the chapters and the credit text, so those answers are kept",
                "bi-check-lg",
            ),
            (
                evidence.locator(".insp-ev[data-source='introdb']"),
                "11 s from the decision, so not used",
                "bi-exclamation-triangle",
            ),
        ]
        for row, note, icon in cells:
            expect(row.locator(":scope > div").nth(2).locator(".insp-small")).to_have_text(note)
            expect(row.locator(".insp-ev-icon i")).to_have_class(f"bi {icon}")

    def test_a_review_answer_says_it_needs_your_check(self, authed_page: Page, app_url: str) -> None:
        fx.install(authed_page)
        _open(authed_page, app_url, fx.REVIEW)
        row = authed_page.locator("#inspEvidence .insp-ev[data-source='credits_text']")
        expect(row.locator(":scope > div").nth(2).locator(".insp-small")).to_have_text(
            "One of the answers that needs your check"
        )

    @pytest.mark.parametrize(
        ("make", "path", "text"),
        [
            (fx.checked_episode, fx.EPISODE, "No source answered for this file."),
            (fx.unchecked_film, fx.FILM, "Nothing has been asked yet. Check intro & credits asks every source."),
        ],
        ids=["checked", "unchecked"],
    )
    def test_an_empty_card_says_why(
        self, authed_page: Page, app_url: str, make: Callable[[], tuple[dict, dict]], path: str, text: str
    ) -> None:
        file, item = make()
        item["evidence"] = []
        fx.install(authed_page, _api_with(file, item))
        _open(authed_page, app_url, path)
        expect(authed_page.locator("#inspEvidence .insp-small")).to_have_text(text)


@pytest.mark.e2e
class TestSearchEdges:
    """Item 24: nothing found, a failed search, the scope, row states, a show's read error, and the address bar."""

    def test_nothing_found(self, authed_page: Page, app_url: str) -> None:
        fx.install(authed_page)
        authed_page.goto(f"{app_url}/inspector")
        authed_page.locator("#inspQuery").fill("zzz")
        expect(authed_page.locator("#inspResults")).to_have_text("Nothing found for “zzz”.")

    @pytest.mark.parametrize(
        ("body", "text"),
        [({"error": "Plex timed out"}, "Plex timed out"), ({}, "Search failed (HTTP 502)")],
        ids=["with-error", "without-error"],
    )
    def test_a_failed_search_says_why(self, authed_page: Page, app_url: str, body: dict, text: str) -> None:
        api = fx.install(authed_page)
        api.answers["search"] = (502, body)
        authed_page.goto(f"{app_url}/inspector")
        authed_page.locator("#inspQuery").fill("matrix")
        expect(authed_page.locator("#inspResults .text-danger")).to_have_text(text)

    def test_the_scope_searches_one_server(self, authed_page: Page, app_url: str) -> None:
        api = fx.install(authed_page)
        api.servers_list.append({"id": "emby-off", "name": "Emby", "type": "emby", "enabled": False})
        authed_page.goto(f"{app_url}/inspector")
        # A disabled server isn't offered.
        expect(authed_page.locator("#inspScope option")).to_have_text(["All servers", "Plex", "Jellyfin"])
        authed_page.locator("#inspQuery").fill("matrix")
        expect(authed_page.locator("#inspResults button.insp-row")).to_have_count(3)
        with authed_page.expect_response(lambda r: "/api/media/search?" in r.url):
            authed_page.locator("#inspScope").select_option("jf-1")
        assert api.search_requests == [{"q": "matrix"}, {"q": "matrix", "server_id": "jf-1"}]

    def test_rows_say_when_a_status_couldnt_be_checked_or_isnt_in_a_library(
        self, authed_page: Page, app_url: str
    ) -> None:
        api = fx.install(authed_page)
        api.status_items = {
            fx.FILM: {"in_library": True, "error": "Plex timed out"},
            fx.search_results()[2]["paths"][0]: {"in_library": False},
        }
        authed_page.goto(f"{app_url}/inspector")
        authed_page.locator("#inspQuery").fill("matrix")
        rows = authed_page.locator("#inspResults button.insp-row")
        expect(rows.nth(0).locator(".insp-cell-preview")).to_have_text("Couldn't check")
        expect(rows.nth(0).locator(".insp-cell-markers")).to_have_text("—")
        expect(rows.nth(1).locator(".insp-cell-preview")).to_have_text("Not in a library")
        expect(rows.nth(1).locator(".insp-cell-markers")).to_have_text("—")
        expect(rows.nth(2).locator(".insp-cell-markers")).to_have_text("Needs review")

    def test_a_failed_status_read_marks_every_row(self, authed_page: Page, app_url: str) -> None:
        api = fx.install(authed_page)
        api.answers["status"] = (500, {"error": "Plex timed out"})
        authed_page.goto(f"{app_url}/inspector")
        authed_page.locator("#inspQuery").fill("matrix")
        expect(authed_page.locator("#inspResults button.insp-row .insp-cell-preview")).to_have_text(
            ["Couldn't check"] * 3
        )

    def test_a_show_that_cant_be_read_says_why(self, authed_page: Page, app_url: str) -> None:
        api = fx.install(authed_page)
        api.answers["show"] = (500, {"error": "Permission denied reading the show's folder"})
        authed_page.goto(f"{app_url}/inspector")
        authed_page.locator("#inspQuery").fill("blood")
        authed_page.locator("#inspResults button.insp-row").first.click()
        expect(authed_page.locator(".insp-show")).to_have_text(
            "Couldn't read this show's episodes: Permission denied reading the show's folder"
        )

    def test_fewer_than_two_characters_hide_the_results(self, authed_page: Page, app_url: str) -> None:
        fx.install(authed_page)
        authed_page.goto(f"{app_url}/inspector")
        authed_page.locator("#inspQuery").fill("matrix")
        expect(authed_page.locator("#inspResults button.insp-row")).to_have_count(3)
        authed_page.locator("#inspQuery").fill("m")
        expect(authed_page.locator("#inspResults")).to_be_hidden()

    def test_a_q_in_the_address_runs_that_search(self, authed_page: Page, app_url: str) -> None:
        api = fx.install(authed_page)
        authed_page.goto(f"{app_url}/inspector?q=matrix")
        expect(authed_page.locator("#inspQuery")).to_have_value("matrix")
        expect(authed_page.locator("#inspResults button.insp-row")).to_have_count(3)
        assert api.search_requests == [{"q": "matrix"}]

    def test_browser_back_returns_to_the_search_and_forward_reopens_the_file(
        self, authed_page: Page, app_url: str
    ) -> None:
        fx.install(authed_page)
        authed_page.goto(f"{app_url}/inspector")
        authed_page.locator("#inspQuery").fill("matrix")
        authed_page.locator("#inspResults button.insp-row").first.click()
        expect(authed_page.locator("#inspTitle")).to_have_text("The Matrix (1999)")

        authed_page.go_back()
        expect(authed_page.locator("#inspSearch")).to_be_visible()
        expect(authed_page.locator("#inspFile")).to_be_hidden()
        expect(authed_page.locator("#inspResults button.insp-row")).to_have_count(3)
        assert urlparse(authed_page.url).query == ""

        authed_page.go_forward()
        expect(authed_page.locator("#inspTitle")).to_have_text("The Matrix (1999)")
        expect(authed_page.locator("#inspSearch")).to_be_hidden()


@pytest.mark.e2e
class TestPreviewOnly:
    """Item 25: ``?bif=`` shows a preview file on its own, with no actions and no Intro & Credits read."""

    def test_a_bif_opens_its_frames_and_nothing_else(self, authed_page: Page, app_url: str) -> None:
        api = fx.install(authed_page)
        api.bif_info[fx.PLEX_BIF] = {
            "path": fx.PLEX_BIF,
            "version": 0,
            "frame_count": 40,
            "frame_interval_ms": 2000,
            "file_size": 400_000,
            "created_at": "2026-09-27T14:19:00+00:00",
        }
        authed_page.goto(f"{app_url}/inspector?bif={quote(fx.PLEX_BIF)}")
        expect(authed_page.locator("#inspLoading")).to_have_count(0, timeout=10_000)

        expect(authed_page.locator("#inspTitle")).to_have_text("index-sd.bif")
        expect(authed_page.locator("#inspPath")).to_have_text(fx.PLEX_BIF)
        expect(authed_page.locator("#inspAllFrames .insp-allframes button")).to_have_count(40)
        expect(authed_page.locator("#inspAllFramesLabel")).to_have_text("Frame 0 of 39 · 0:00")
        expect(authed_page.locator(".insp-actions")).to_have_count(0)
        expect(authed_page.locator("#inspSummaryCard")).to_have_count(0)
        assert api.item_requests == []
        assert api.file_requests == []

    def test_a_bif_that_cant_be_read_says_so(self, authed_page: Page, app_url: str) -> None:
        api = fx.install(authed_page)
        authed_page.goto(f"{app_url}/inspector?bif={quote('/plex/elsewhere/index-sd.bif')}")
        expect(authed_page.locator('[data-state="Couldn\'t open this file"]')).to_contain_text("Not a preview file")
        expect(authed_page.locator(".insp-actions")).to_have_count(0)
        assert api.item_requests == []


@pytest.mark.e2e
class TestTooltipsAndFrames:
    """Item 26: info icons on every header action and toggle, tooltips disposed on re-render, and frame stepping."""

    def test_every_header_action_and_toggle_has_an_info_icon(self, authed_page: Page, app_url: str) -> None:
        fx.install(authed_page)
        _open(authed_page, app_url, fx.EPISODE)
        actions = authed_page.locator(".insp-actions > span")
        expect(actions.locator("button:not(.info-icon)")).to_have_text(
            ["Regenerate preview", "Re-detect intro & credits", "Lock", "Adjust"]
        )
        labels = [actions.nth(i).locator(".info-icon").get_attribute("aria-label") or "" for i in range(4)]
        words = ("preview", "every source", "Keep these times", "one second at a time")
        for label, word in zip(labels, words, strict=True):
            assert word in label
        toggles = authed_page.locator("span:has(> .insp-seg) > .info-icon")
        expect(toggles).to_have_count(2)
        for i in range(2):
            assert (toggles.nth(i).get_attribute("aria-label") or "").strip()

    def test_a_tooltip_open_during_a_re_render_is_removed(self, authed_page: Page, app_url: str) -> None:
        fx.install(authed_page)
        _open(authed_page, app_url, fx.EPISODE)
        authed_page.locator("#inspWholeFile .info-icon").hover()
        tooltip = authed_page.locator(".tooltip")
        expect(tooltip).to_have_count(1)
        expect(tooltip).to_contain_text("Every frame of the preview")
        shown = tooltip.get_attribute("id")
        assert shown
        # A click that doesn't move the mouse: the icon under it is replaced by the re-render. (Chrome may show the
        # new icon's own tooltip, since it lands under the cursor; the old one must be gone.)
        authed_page.locator("#inspViewAll").dispatch_event("click")
        expect(authed_page.locator("#inspAllFrames")).to_be_visible()
        expect(authed_page.locator(f"#{shown}")).to_have_count(0)
        authed_page.mouse.move(0, 0)
        expect(tooltip).to_have_count(0)

    def test_previous_and_next_frame_buttons_step(self, authed_page: Page, app_url: str) -> None:
        fx.install(authed_page)
        _open(authed_page, app_url, fx.EPISODE)
        authed_page.locator("#inspViewAll").click()
        grid = authed_page.locator("#inspAllFrames .insp-allframes button")
        label = authed_page.locator("#inspAllFramesLabel")
        grid.nth(100).click()
        expect(label).to_have_text("Frame 100 of 827 · 3:20")

        authed_page.get_by_role("button", name="Next frame").click()
        expect(label).to_have_text("Frame 101 of 827 · 3:22")
        authed_page.get_by_role("button", name="Previous frame").click()
        authed_page.get_by_role("button", name="Previous frame").click()
        expect(label).to_have_text("Frame 99 of 827 · 3:18")
        expect(grid.nth(99)).to_have_class(re.compile(r"\bis-active\b"))

        grid.nth(0).click()
        authed_page.get_by_role("button", name="Previous frame").click()
        expect(label).to_have_text("Frame 0 of 827 · 0:00")
        assert "index=0" in (authed_page.locator("#inspAllFramesBig").get_attribute("src") or "")


@pytest.mark.e2e
class TestCloseupDetail:
    """Item 27: the runs-to-the-end note, a failing frame read, and a busy one asked again."""

    @pytest.mark.parametrize(
        ("kind", "first_line", "last_frame"),
        [
            (
                "episode",
                "Runs to the end of the file. Skipping credits jumps to the next episode.",
                "The last story frame is 24:58; the credits start on the next frame.",
            ),
            (
                "film",
                "Runs to the end of the file.",
                "The last story frame is 1:32:35; the credits start on the next frame.",
            ),
        ],
    )
    def test_credits_to_the_end_say_where_the_story_stops(
        self, authed_page: Page, app_url: str, kind: str, first_line: str, last_frame: str
    ) -> None:
        if kind == "episode":
            file, item = fx.checked_episode()
        else:
            file, item = fx.review_film()
            item["decisions"]["credits"] = fx.decision(
                "decided", fx.marker("credits", 5_557_000, None, ["credits_text"])
            )
        fx.install(authed_page, _api_with(file, item))
        _open(authed_page, app_url, file["canonical_path"])
        note = authed_page.locator("#inspCloseups [data-closeup='credits'] .insp-card")
        expect(note.locator(":scope > div").nth(0)).to_have_text(first_line)
        expect(note.locator(":scope > div").nth(1)).to_have_text(last_frame)

    def test_a_failing_frame_read_says_why(self, authed_page: Page, app_url: str) -> None:
        file, item = fx.checked_episode()
        api = _api_with(_no_preview(file), item)
        api.frames_error = (500, {"error": "ffmpeg couldn't seek in this file"})
        fx.install(authed_page, api)
        _open(authed_page, app_url, fx.EPISODE)
        expect(authed_page.locator("#inspCloseups [data-closeup='intro'] .insp-frames").first).to_have_text(
            "Couldn't read frames here: ffmpeg couldn't seek in this file"
        )

    def test_a_busy_frame_read_is_asked_again(self, authed_page: Page, app_url: str) -> None:
        file, item = fx.checked_episode()
        api = _api_with(_no_preview(file), item)
        api.frames_busy = {162_000: 1}
        fx.install(authed_page, api)
        _open(authed_page, app_url, fx.EPISODE)
        first = authed_page.locator("#inspCloseups [data-closeup='intro'] .insp-frames").first
        expect(first.locator(".insp-frame img")).to_have_count(7)
        expect(first).not_to_contain_text("Couldn't read frames")
        asks = [r for r in api.frame_requests if r["start_ms"] == 162_000]
        assert asks == [{"path": fx.EPISODE, "start_ms": 162_000, "count": 7}] * 2


@pytest.mark.e2e
class TestSeasonExtras:
    """The old Season view's checks not yet covered: a lazy season read, its error, the dots, a refused publish."""

    def test_the_season_is_read_only_when_chosen_and_its_dots_are_labelled(
        self, authed_page: Page, app_url: str
    ) -> None:
        api = fx.install(authed_page)
        _open(authed_page, app_url, fx.EPISODE)
        expect(authed_page.locator("#inspSummary")).to_be_visible()
        assert api.season_requests == []

        authed_page.locator("#inspScopeSeason").click()
        dots = authed_page.locator("#inspSeason button.insp-season-row").nth(0).locator(".insp-dot")
        expect(dots).to_have_count(2)
        assert api.season_requests == [fx.EPISODE]
        for i, title in enumerate(("Plex: shows our markers", "Jellyfin: Waiting for Jellyfin to add the file")):
            expect(dots.nth(i)).to_have_attribute("title", title)
            expect(dots.nth(i)).to_have_attribute("aria-label", title)
            expect(dots.nth(i)).to_have_attribute("role", "img")

    def test_a_season_that_cant_be_read_says_why(self, authed_page: Page, app_url: str) -> None:
        api = fx.install(authed_page)
        api.answers["season"] = (500, {"error": "Couldn't list the season folder"})
        _open(authed_page, app_url, fx.EPISODE, view="season")
        season = authed_page.locator("#inspSeason")
        expect(season.locator(".fw-semibold")).to_have_text("Couldn't load this season")
        expect(season.locator(".insp-small")).to_have_text("Couldn't list the season folder")

    def test_a_refused_publish_says_why(self, authed_page: Page, app_url: str) -> None:
        api = fx.install(authed_page)
        api.answers["publish"] = (409, {"error": "A season job is already queued"})
        _open(authed_page, app_url, fx.EPISODE, view="season")
        publish = authed_page.locator("#inspPublishSeason")
        with authed_page.expect_response(lambda r: r.url.endswith("/api/markers/season/publish")):
            publish.click()
        expect(authed_page.locator("#toastTitle")).to_have_text("Publish season")
        expect(authed_page.locator("#toastBody")).to_have_text("Couldn't queue it: A season job is already queued")
        expect(publish).to_have_text("Publish 9 to 2 servers")
        expect(publish).to_be_enabled()
        expect(authed_page.locator("#inspJobBanner")).to_be_hidden()
        assert api.season_publishes == [{"path": fx.EPISODE}]
