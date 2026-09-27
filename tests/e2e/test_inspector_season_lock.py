"""E2E: the Inspector's Whole season view, Publish season, and Lock / Back to automatic.

The season view reads ``GET /api/markers/season`` and queues ``POST /api/markers/season/publish``; Lock sends the
decided times unchanged through ``POST /api/markers/item/markers`` (save = lock + publish) and Back to automatic is
``DELETE`` on the same route. Every write's body is asserted.
"""

from __future__ import annotations

from urllib.parse import parse_qs, quote, urlparse

import pytest
from playwright.sync_api import Page, expect

from . import _inspector_fixtures as fx


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


def _open(page: Page, app_url: str, path: str, *, view: str = "") -> None:
    page.goto(f"{app_url}/inspector?path={quote(path)}" + (f"&view={view}" if view else ""))
    expect(page.locator("#inspLoading")).to_have_count(0, timeout=10_000)


def _locked_episode() -> fx.InspectorApi:
    api = fx.InspectorApi()
    file, item = fx.checked_episode()
    item["decisions"]["credits"]["marker"].update(locked=True, decided_by=["user"])
    api.add(file, item, fx.default_kinds(fx.EPISODE))
    return api


@pytest.mark.e2e
class TestWholeSeason:
    def test_the_toggle_shows_every_episode_on_one_scale(self, authed_page: Page, app_url: str) -> None:
        api = fx.install(authed_page)
        _open(authed_page, app_url, fx.EPISODE)
        expect(authed_page.locator("#inspScopeEpisode")).to_have_attribute("aria-pressed", "true")
        authed_page.locator("#inspScopeSeason").click()

        season = authed_page.locator("#inspSeason")
        expect(season.locator(".insp-card-title").first).to_have_text("Blood Legacy (2024) · Season 1")
        expect(season).to_contain_text("10 episodes")
        assert api.season_requests == [fx.EPISODE]
        assert parse_qs(urlparse(authed_page.url).query) == {"path": [fx.EPISODE], "view": ["season"]}
        rows = season.locator("button.insp-season-row")
        expect(rows).to_have_count(10)
        expect(rows.nth(0)).to_have_class("insp-season-row is-current")
        expect(rows.nth(0)).to_contain_text("Intro 2:45–2:59 · Credits 24:59 → end")
        expect(rows.nth(1)).to_contain_text("🔒 Locked by you")
        expect(rows.nth(3)).to_contain_text("Credits")
        expect(rows.nth(6)).to_contain_text("Not checked yet")
        expect(rows.nth(9)).to_contain_text("Needs review")
        expect(rows.nth(9).locator(".insp-state-review")).to_have_attribute(
            "title", "Sources disagree: chapters, credits_text"
        )
        expect(rows.nth(0).locator(".insp-season-chips")).to_contain_text("Audio 9/10")
        expect(rows.nth(2).locator(".insp-dot-waiting")).to_have_attribute(
            "title", "Jellyfin: Waiting for Jellyfin to add the file"
        )
        # One scale: the longest episode's track fills the lane, a shorter one's doesn't.
        widths = [rows.nth(i).locator(".insp-season-track").evaluate("n => n.style.width") for i in (1, 2)]
        assert widths[0] == "100%" and float(widths[1].rstrip("%")) < 100
        expect(rows.nth(0).locator(".insp-bar-intro")).to_have_count(1)
        expect(rows.nth(0).locator(".insp-bar-credits")).to_have_count(1)
        # The episode view's own cards are away while the season is on show.
        expect(authed_page.locator("#inspSummaryCard")).to_have_count(0)
        fx.screenshot(authed_page, "20-whole-season")

    def test_a_row_opens_that_episode(self, authed_page: Page, app_url: str) -> None:
        api = fx.InspectorApi()
        file, item = fx.checked_episode()
        api.add(file, item, fx.default_kinds(fx.EPISODE))
        target = fx.season_payload()["episodes"][2]["path"]
        api.add({**file, "canonical_path": target, "title": "Blood Legacy (2024) · S01E03"}, item)
        fx.install(authed_page, api)
        _open(authed_page, app_url, fx.EPISODE, view="season")
        row = authed_page.locator("#inspSeason button.insp-season-row").nth(2)
        expect(row).to_have_attribute("data-path", target)
        row.click()

        expect(authed_page.locator("#inspTitle")).to_have_text("Blood Legacy (2024) · S01E03")
        expect(authed_page.locator("#inspPath")).to_have_text(target)
        assert parse_qs(urlparse(authed_page.url).query) == {"path": [target]}
        expect(authed_page.locator("#inspScopeEpisode")).to_have_attribute("aria-pressed", "true")
        expect(authed_page.locator("#inspSeason")).to_have_count(0)

    def test_publish_season_queues_the_season_job(self, authed_page: Page, app_url: str) -> None:
        api = fx.install(authed_page)
        _open(authed_page, app_url, fx.EPISODE, view="season")
        publish = authed_page.locator("#inspPublishSeason")
        expect(publish).to_have_text("Publish 9 to 2 servers")
        expect(authed_page.locator("#inspSeasonReady")).to_have_text("9 ready")
        expect(authed_page.locator("#inspSeasonReview")).to_have_text("1 need review")

        with authed_page.expect_response(lambda r: r.url.endswith("/api/markers/season/publish")):
            publish.click()
        assert api.season_publishes == [{"path": fx.EPISODE}]
        expect(authed_page.locator("#toastBody")).to_contain_text("Queued")
        expect(authed_page.locator("#inspJobBanner")).to_contain_text("Queued for this file: Intro & Credits job")

    @pytest.mark.parametrize(("ready", "on"), [(0, True), (9, False)])
    def test_publish_is_off_with_nothing_ready_or_no_server_on(
        self, authed_page: Page, app_url: str, ready: int, on: bool
    ) -> None:
        api = fx.install(authed_page)
        api.season = fx.season_payload(ready=ready, markers_on=on)
        _open(authed_page, app_url, fx.EPISODE, view="season")
        expect(authed_page.locator("#inspPublishSeason")).to_be_disabled()

    def test_a_big_season_says_it_shows_the_nearest_forty(self, authed_page: Page, app_url: str) -> None:
        api = fx.install(authed_page)
        api.season = fx.season_payload(total=60)
        _open(authed_page, app_url, fx.EPISODE, view="season")
        expect(authed_page.locator("#inspSeason")).to_contain_text("60 episodes (showing the 40 nearest)")

    def test_a_film_has_no_season_toggle(self, authed_page: Page, app_url: str) -> None:
        fx.install(authed_page)
        _open(authed_page, app_url, fx.FILM)
        expect(authed_page.locator("#inspScopeSeason")).to_have_count(0)


@pytest.mark.e2e
class TestLock:
    def test_lock_sends_the_decided_times_unchanged(self, authed_page: Page, app_url: str) -> None:
        api = fx.install(authed_page)
        _open(authed_page, app_url, fx.EPISODE)
        lock = authed_page.locator("#inspLock")
        expect(lock).to_have_text("Lock")
        # Beside Adjust, with a tooltip saying what it does.
        expect(lock.locator("xpath=following-sibling::button[contains(@class,'info-icon')]")).to_have_attribute(
            "aria-label", fx_lock_tip()
        )
        lock.click()
        confirm = authed_page.locator("#inspLockConfirmRow")
        expect(confirm).to_contain_text("Lock these times? Intro 2:45–2:59 · Credits 24:59 → end")
        expect(confirm).to_contain_text("they go to Plex and Jellyfin now")
        fx.screenshot(authed_page, "21-lock-confirm")

        with authed_page.expect_response(
            lambda r: r.url.endswith("/api/markers/item/markers") and r.request.method == "POST"
        ):
            authed_page.locator("#inspLockConfirm").click()
        assert api.saves == [
            {
                "path": fx.EPISODE,
                "markers": [
                    {"type": "intro", "start_ms": 165_000, "end_ms": 179_000},
                    {"type": "credits", "start_ms": 1_499_000, "end_ms": None},
                ],
            }
        ]
        expect(authed_page.locator("#toastBody")).to_contain_text("These times stay until you go back to automatic.")

    def test_leave_them_sends_nothing(self, authed_page: Page, app_url: str) -> None:
        api = fx.install(authed_page)
        _open(authed_page, app_url, fx.EPISODE)
        authed_page.locator("#inspLock").click()
        authed_page.get_by_role("button", name="Leave them as they are").click()
        expect(authed_page.locator("#inspLockConfirmRow")).to_have_count(0)
        assert api.saves == []

    def test_lock_leaves_out_a_type_no_server_can_show(self, authed_page: Page, app_url: str) -> None:
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
        authed_page.locator("#inspLock").click()
        with authed_page.expect_response(
            lambda r: r.url.endswith("/api/markers/item/markers") and r.request.method == "POST"
        ):
            authed_page.locator("#inspLockConfirm").click()
        assert [m["type"] for m in api.saves[0]["markers"]] == ["intro", "credits"]

    def test_a_locked_file_offers_back_to_automatic_instead(self, authed_page: Page, app_url: str) -> None:
        api = fx.install(authed_page, _locked_episode())
        _open(authed_page, app_url, fx.EPISODE)
        expect(authed_page.locator("#inspLock")).to_have_count(0)
        unlock = authed_page.locator("#inspUnlock")
        expect(unlock).to_have_text("Back to automatic")
        expect(unlock.locator("xpath=following-sibling::button[contains(@class,'info-icon')]")).to_have_attribute(
            "aria-label", fx_unlock_tip()
        )
        expect(authed_page.locator("#inspLocked")).to_contain_text("You set the credits, so later checks keep them.")
        fx.screenshot(authed_page, "22-locked")
        unlock.click()
        expect(authed_page.locator("#inspLocked")).to_contain_text("Back to automatic? Your times stay on your servers")
        fx.screenshot(authed_page, "23-unlock-confirm")

        authed_page.get_by_role("button", name="Keep them locked").click()
        expect(authed_page.locator("#inspUnlockConfirm")).to_have_count(0)
        unlock.click()
        with authed_page.expect_response(
            lambda r: r.url.endswith("/api/markers/item/markers") and r.request.method == "DELETE"
        ):
            authed_page.locator("#inspUnlockConfirm").click()
        assert api.unlocks == [{"path": fx.EPISODE, "types": ["credits"]}]
        assert api.saves == []

    def test_lock_and_adjust_wait_while_adjusting(self, authed_page: Page, app_url: str) -> None:
        fx.install(authed_page)
        _open(authed_page, app_url, fx.EPISODE)
        authed_page.get_by_role("button", name="Adjust", exact=True).click()
        expect(authed_page.locator("#inspLock")).to_be_disabled()

    def test_a_file_with_nothing_decided_has_no_lock(self, authed_page: Page, app_url: str) -> None:
        fx.install(authed_page)
        _open(authed_page, app_url, fx.REVIEW)
        expect(authed_page.locator("#inspLock")).to_have_count(0)
        expect(authed_page.locator("#inspUnlock")).to_have_count(0)


def fx_lock_tip() -> str:
    return "Keep these times exactly as they are. Later checks won't change them, and your servers get them now."


def fx_unlock_tip() -> str:
    return (
        "Let later checks set these times again. What your servers show now stays until the next Intro & Credits job."
    )
