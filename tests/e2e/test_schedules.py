"""E2E tests for the Schedules UI on /automation.

Phase E regression: the create-schedule and update-schedule endpoints used
to reject any schedule pinned to a non-Plex server with a 400. After the
multi-server completion every vendor's processor implements
scan_recently_added + list_canonical_paths, so non-Plex schedules now
save and run.

These tests exercise the UI end-to-end against mocked /api/schedules and
/api/servers responses to confirm the previously-blocked options now go
through cleanly.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, Route, expect

from ._mocks import _fulfill_json, mock_dashboard_defaults, mock_media_servers_status, mock_servers_list


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


def _seed_servers_for_schedule_modal(page: Page) -> None:
    """Common setup for the schedule modal: dashboard defaults + server list +
    /api/schedules with one Jellyfin schedule and one Plex schedule already in
    place so the table renders with vendor badges."""
    mock_dashboard_defaults(page)
    servers = [
        {
            "id": "plex-1",
            "name": "Home Plex",
            "type": "plex",
            "enabled": True,
            "status": "connected",
            "url": "http://plex.local:32400",
        },
        {
            "id": "jf-1",
            "name": "Home Jellyfin",
            "type": "jellyfin",
            "enabled": True,
            "status": "connected",
            "url": "http://jf.local:8096",
        },
        {
            "id": "emby-1",
            "name": "Home Emby",
            "type": "emby",
            "enabled": True,
            "status": "connected",
            "url": "http://emby.local:8096",
        },
    ]
    mock_media_servers_status(page, servers=servers)
    # The schedule modal's server picker reads /api/servers — separate
    # from /api/system/media-servers/status which feeds the status panel.
    mock_servers_list(page, servers=servers)
    # Libraries endpoint — returns a mixed set so the modal's library
    # picker has something to render regardless of which server is pinned.
    page.route(
        "**/api/libraries**",
        lambda r: _fulfill_json(
            r,
            {
                "libraries": [
                    {"id": "lib-1", "name": "Movies", "type": "movie", "server_id": "jf-1"},
                ]
            },
        ),
    )
    # Schedules list (empty initially so the table renders without
    # cluttering up the assertions).
    page.route(
        "**/api/schedules",
        lambda r: _fulfill_json(r, {"schedules": []}) if r.request.method == "GET" else r.continue_(),
    )


@pytest.mark.e2e
class TestScheduleNonPlex:
    def test_save_recently_added_schedule_against_jellyfin_succeeds(self, authed_page: Page, app_url: str) -> None:
        """Phase E regression: api_schedules.create_schedule used to 400 on
        non-Plex pins for recently_added. Now it saves cleanly."""
        _seed_servers_for_schedule_modal(authed_page)

        captured: list[dict] = []

        def handler(route: Route) -> None:
            method = route.request.method
            if method == "POST":
                try:
                    captured.append(route.request.post_data_json or {})
                except Exception:
                    captured.append({})
                _fulfill_json(
                    route,
                    {"id": "sch-1", "name": "Recent JF", "server_id": "jf-1", "enabled": True},
                    status=201,
                )
            elif method == "GET":
                _fulfill_json(route, {"schedules": []})
            else:
                route.continue_()

        authed_page.route("**/api/schedules", handler)

        authed_page.goto(f"{app_url}/automation#schedules")
        authed_page.wait_for_load_state("domcontentloaded")
        # Click "Add Schedule" — opens the modal.
        authed_page.locator('button:has-text("Add Schedule")').first.click()
        expect(authed_page.locator("#newScheduleForm")).to_be_visible(timeout=2000)

        # Fill the form: name + Recently Added scan mode + Jellyfin server.
        authed_page.locator("#scheduleName").fill("Recent JF")
        authed_page.locator("#scanModeRecent").check()
        # Wait deterministically for the JS populator to add the
        # jf-1 option before selecting it.
        expect(authed_page.locator('#scheduleServer option[value="jf-1"]')).to_be_attached(timeout=3000)
        authed_page.locator("#scheduleServer").select_option("jf-1")
        # Submit — the form's primary button.
        save_btn = authed_page.locator(
            "#newScheduleModal button.btn-primary, #newScheduleModal button:has-text('Save')"
        ).last
        # Wait for POST to fire so the assertion below isn't racing it.
        with authed_page.expect_request("**/api/schedules") as req_info:
            save_btn.click()
        req_info.value  # noqa: B018 — ensures the request landed

        assert captured, "POST /api/schedules never fired"
        body = captured[0]
        assert body.get("server_id") == "jf-1", body
        cfg = body.get("config") or {}
        assert cfg.get("job_type") == "recently_added", body

    def test_save_full_library_schedule_against_emby_succeeds(self, authed_page: Page, app_url: str) -> None:
        """Phase E regression: api_schedules.create_schedule used to 400 on
        non-Plex pins for full_library too."""
        _seed_servers_for_schedule_modal(authed_page)

        captured: list[dict] = []

        def handler(route: Route) -> None:
            method = route.request.method
            if method == "POST":
                try:
                    captured.append(route.request.post_data_json or {})
                except Exception:
                    captured.append({})
                _fulfill_json(
                    route,
                    {"id": "sch-2", "name": "Nightly Emby", "server_id": "emby-1", "enabled": True},
                    status=201,
                )
            elif method == "GET":
                _fulfill_json(route, {"schedules": []})
            else:
                route.continue_()

        authed_page.route("**/api/schedules", handler)

        authed_page.goto(f"{app_url}/automation#schedules")
        authed_page.wait_for_load_state("domcontentloaded")
        authed_page.locator('button:has-text("Add Schedule")').first.click()
        expect(authed_page.locator("#newScheduleForm")).to_be_visible(timeout=2000)

        authed_page.locator("#scheduleName").fill("Nightly Emby")
        # Full library is the default scanMode; wait for the server
        # populator before selecting.
        expect(authed_page.locator('#scheduleServer option[value="emby-1"]')).to_be_attached(timeout=3000)
        authed_page.locator("#scheduleServer").select_option("emby-1")

        save_btn = authed_page.locator(
            "#newScheduleModal button.btn-primary, #newScheduleModal button:has-text('Save')"
        ).last
        with authed_page.expect_request("**/api/schedules") as req_info:
            save_btn.click()
        req_info.value  # noqa: B018 — ensures the request landed

        assert captured, "POST /api/schedules never fired"
        body = captured[0]
        assert body.get("server_id") == "emby-1", body
        cfg = body.get("config") or {}
        # Either omitted (defaults to full_library) or explicitly set.
        assert cfg.get("job_type") in (None, "full_library"), body


@pytest.mark.e2e
class TestScheduleServerDropdownVendorBadges:
    def test_schedule_server_dropdown_shows_vendor_in_option_text(self, authed_page: Page, app_url: str) -> None:
        """Phase F regression: the schedule server picker labels each option
        with a vendor suffix so users can disambiguate same-named servers."""
        _seed_servers_for_schedule_modal(authed_page)
        authed_page.goto(f"{app_url}/automation#schedules")
        authed_page.wait_for_load_state("domcontentloaded")
        authed_page.locator('button:has-text("Add Schedule")').first.click()
        expect(authed_page.locator("#scheduleServer")).to_be_visible(timeout=2000)
        # Wait for the async populator to inject the test fixture's
        # known server ids. The seeded mock from _seed_servers_for_schedule_modal
        # always adds at least "plex-1" — pin on that.
        expect(authed_page.locator('#scheduleServer option[value="plex-1"]')).to_be_attached(timeout=3000)

        option_texts = authed_page.locator("#scheduleServer option").all_text_contents()
        joined = " | ".join(option_texts)
        assert "(PLEX)" in joined, f"PLEX badge missing: {option_texts}"
        assert "(EMBY)" in joined, f"EMBY badge missing: {option_texts}"
        assert "(JELLYFIN)" in joined, f"JELLYFIN badge missing: {option_texts}"


_MARKERS_SCHEDULE = {
    "id": "sch-ic",
    "name": "Weekly Intro & Credits",
    "enabled": True,
    "trigger_type": "cron",
    "trigger_value": "0 3 * * 6",
    "library_id": None,
    "library_ids": [],
    "library_name": "All Libraries",
    "server_id": None,
    "priority": None,
    "stop_time": "",
    "config": {"job_type": "intro_credits"},
    "next_run": None,
}


def _capture_schedule_writes(page: Page, schedules: list[dict]) -> list[tuple[str, dict]]:
    """GET /api/schedules serves ``schedules``; POST and PUT bodies are captured as ``(method, body)``."""
    captured: list[tuple[str, dict]] = []

    def handler(route: Route) -> None:
        method = route.request.method
        if method in ("POST", "PUT"):
            captured.append((method, route.request.post_data_json or {}))
            _fulfill_json(route, {"id": "sch-ic", "enabled": True}, status=201 if method == "POST" else 200)
        else:
            _fulfill_json(route, {"schedules": schedules})

    page.route("**/api/schedules", handler)
    page.route("**/api/schedules/*", handler)
    return captured


def _save_schedule(page: Page, url_glob: str) -> None:
    with page.expect_request(url_glob):
        page.locator("#scheduleSubmitBtn").click()


@pytest.mark.e2e
class TestScheduleIntroCredits:
    def test_intro_credits_mode_hides_lookback_and_order_and_saves_its_job_type(
        self, authed_page: Page, app_url: str
    ) -> None:
        _seed_servers_for_schedule_modal(authed_page)
        captured = _capture_schedule_writes(authed_page, [])
        authed_page.goto(f"{app_url}/automation#schedules")
        authed_page.wait_for_load_state("domcontentloaded")
        authed_page.locator('button:has-text("Add Schedule")').first.click()
        expect(authed_page.locator("#newScheduleForm")).to_be_visible(timeout=2000)
        authed_page.locator("#scheduleName").fill("Weekly Intro & Credits")
        # Recently added first, so switching away proves the lookback hides again.
        authed_page.locator("#scanModeRecent").check()
        expect(authed_page.locator("#scheduleLookbackGroup")).to_be_visible()

        authed_page.locator("#scanModeMarkers").check()

        expect(authed_page.locator("#scheduleLookbackGroup")).to_be_hidden()
        expect(authed_page.locator("#scheduleSortByGroup")).to_be_hidden()
        info = authed_page.locator("#scanModeMarkersInfo")
        assert (info.get_attribute("data-bs-original-title") or info.get_attribute("title")) == (
            "Checks the chosen libraries for intro and credits markers. Files already done are skipped. "
            "Low priority unless you pick otherwise."
        )
        _save_schedule(authed_page, "**/api/schedules")

        assert [method for method, _ in captured] == ["POST"]
        body = captured[0][1]
        assert body["config"] == {"job_type": "intro_credits"}
        assert body["priority"] is None

    def test_list_badge_and_edit_round_trip_keep_the_mode(self, authed_page: Page, app_url: str) -> None:
        _seed_servers_for_schedule_modal(authed_page)
        captured = _capture_schedule_writes(authed_page, [_MARKERS_SCHEDULE])
        authed_page.goto(f"{app_url}/automation#schedules")
        authed_page.wait_for_load_state("domcontentloaded")
        row = authed_page.locator("#scheduleList tr", has_text="Weekly Intro & Credits")
        expect(row.locator(".schedule-kind-badge")).to_have_text("Intro & Credits", timeout=3000)
        assert "Low" in (row.locator(".priority-badge").get_attribute("title") or "")

        row.locator('button[aria-label="Edit schedule"]').click()
        expect(authed_page.locator("#newScheduleForm")).to_be_visible(timeout=2000)

        expect(authed_page.locator("#scanModeMarkers")).to_be_checked()
        expect(authed_page.locator("#scheduleLookbackGroup")).to_be_hidden()
        expect(authed_page.locator("#scheduleSortByGroup")).to_be_hidden()
        _save_schedule(authed_page, "**/api/schedules/sch-ic")

        assert [method for method, _ in captured] == ["PUT"]
        assert captured[0][1]["config"] == {"job_type": "intro_credits"}

    def test_check_servers_hides_the_server_and_library_pickers_and_saves_its_mode(
        self, authed_page: Page, app_url: str
    ) -> None:
        _seed_servers_for_schedule_modal(authed_page)
        captured = _capture_schedule_writes(authed_page, [])
        authed_page.goto(f"{app_url}/automation#schedules")
        authed_page.wait_for_load_state("domcontentloaded")
        authed_page.locator('button:has-text("Add Schedule")').first.click()
        expect(authed_page.locator("#newScheduleForm")).to_be_visible(timeout=2000)
        authed_page.locator("#scheduleName").fill("Check servers nightly")
        expect(authed_page.locator("#scheduleMarkersModeGroup")).to_be_hidden()

        authed_page.locator("#scanModeMarkers").check()
        expect(authed_page.locator("#scheduleMarkersModeGroup")).to_be_visible()
        expect(authed_page.locator("#scheduleMarkersFind")).to_be_checked()
        expect(authed_page.locator("#scheduleServerGroup")).to_be_visible()
        expect(authed_page.locator("#scheduleLibrariesGroup")).to_be_visible()

        authed_page.locator("#scheduleMarkersCheckServers").check()
        expect(authed_page.locator("#scheduleServerGroup")).to_be_hidden()
        expect(authed_page.locator("#scheduleLibrariesGroup")).to_be_hidden()
        info = authed_page.locator("#scheduleMarkersCheckServersInfo.info-icon")
        assert (info.get_attribute("data-bs-original-title") or info.get_attribute("title")) == (
            "Checks that every server with Intro & Credits on still shows the markers this app sent, and sends them again "
            "where they're missing or changed (unless that server is set to keep its own). "
            "Covers all servers and libraries. Low priority unless you pick otherwise."
        )
        # A library pick left from before doesn't block the save: Check servers has no libraries.
        authed_page.evaluate("document.getElementById('scheduleLibraryAll').checked = false")
        _save_schedule(authed_page, "**/api/schedules")

        assert [method for method, _ in captured] == ["POST"]
        body = captured[0][1]
        assert body["config"] == {"job_type": "intro_credits", "reconcile": True}
        assert (body["library_ids"], body["library_id"], body["server_id"]) == ([], None, None)
        assert body["library_name"] == "All servers"
        assert body["priority"] is None

    def test_leaving_intro_and_credits_hides_its_modes_and_shows_the_pickers_again(
        self, authed_page: Page, app_url: str
    ) -> None:
        _seed_servers_for_schedule_modal(authed_page)
        _capture_schedule_writes(authed_page, [])
        authed_page.goto(f"{app_url}/automation#schedules")
        authed_page.wait_for_load_state("domcontentloaded")
        authed_page.locator('button:has-text("Add Schedule")').first.click()
        expect(authed_page.locator("#newScheduleForm")).to_be_visible(timeout=2000)
        authed_page.locator("#scanModeMarkers").check()
        authed_page.locator("#scheduleMarkersCheckServers").check()

        authed_page.locator("#scanModeFull").check()

        expect(authed_page.locator("#scheduleMarkersModeGroup")).to_be_hidden()
        expect(authed_page.locator("#scheduleServerGroup")).to_be_visible()
        expect(authed_page.locator("#scheduleLibrariesGroup")).to_be_visible()

    def test_check_servers_badge_and_edit_round_trip_keep_the_mode(self, authed_page: Page, app_url: str) -> None:
        _seed_servers_for_schedule_modal(authed_page)
        schedule = {
            **_MARKERS_SCHEDULE,
            "name": "Check servers nightly",
            "library_name": "All servers",
            "config": {"job_type": "intro_credits", "reconcile": True},
        }
        captured = _capture_schedule_writes(authed_page, [schedule])
        authed_page.goto(f"{app_url}/automation#schedules")
        authed_page.wait_for_load_state("domcontentloaded")
        row = authed_page.locator("#scheduleList tr", has_text="Check servers nightly")
        expect(row.locator(".schedule-kind-badge")).to_have_text("Intro & Credits · Check servers", timeout=3000)

        row.locator('button[aria-label="Edit schedule"]').click()
        expect(authed_page.locator("#newScheduleForm")).to_be_visible(timeout=2000)
        expect(authed_page.locator("#scanModeMarkers")).to_be_checked()
        expect(authed_page.locator("#scheduleMarkersCheckServers")).to_be_checked()
        expect(authed_page.locator("#scheduleServerGroup")).to_be_hidden()
        expect(authed_page.locator("#scheduleLibrariesGroup")).to_be_hidden()
        _save_schedule(authed_page, "**/api/schedules/sch-ic")

        assert [method for method, _ in captured] == ["PUT"]
        assert captured[0][1]["config"] == {"job_type": "intro_credits", "reconcile": True}

    def test_a_new_schedule_after_editing_check_servers_starts_on_find_markers(
        self, authed_page: Page, app_url: str
    ) -> None:
        _seed_servers_for_schedule_modal(authed_page)
        schedule = {**_MARKERS_SCHEDULE, "config": {"job_type": "intro_credits", "reconcile": True}}
        _capture_schedule_writes(authed_page, [schedule])
        authed_page.goto(f"{app_url}/automation#schedules")
        authed_page.wait_for_load_state("domcontentloaded")
        row = authed_page.locator("#scheduleList tr", has_text="Weekly Intro & Credits")
        row.locator('button[aria-label="Edit schedule"]').click()
        expect(authed_page.locator("#scheduleMarkersCheckServers")).to_be_checked(timeout=2000)
        # Bootstrap ignores a close click while the modal is still fading in.
        authed_page.wait_for_function(
            "() => getComputedStyle(document.getElementById('newScheduleModal')).opacity === '1'"
        )
        authed_page.locator("#newScheduleModal .btn-close").click()
        expect(authed_page.locator("#newScheduleModal")).to_be_hidden(timeout=3000)

        authed_page.locator('button:has-text("Add Schedule")').first.click()
        expect(authed_page.locator("#newScheduleForm")).to_be_visible(timeout=2000)
        authed_page.locator("#scanModeMarkers").check()

        expect(authed_page.locator("#scheduleMarkersFind")).to_be_checked()
        expect(authed_page.locator("#scheduleLibrariesGroup")).to_be_visible()

    def test_full_library_schedule_still_saves_its_sort_order(self, authed_page: Page, app_url: str) -> None:
        _seed_servers_for_schedule_modal(authed_page)
        captured = _capture_schedule_writes(authed_page, [])
        authed_page.goto(f"{app_url}/automation#schedules")
        authed_page.wait_for_load_state("domcontentloaded")
        authed_page.locator('button:has-text("Add Schedule")').first.click()
        expect(authed_page.locator("#newScheduleForm")).to_be_visible(timeout=2000)
        authed_page.locator("#scheduleName").fill("Nightly")
        authed_page.locator("#scanModeMarkers").check()
        authed_page.locator("#scanModeFull").check()
        expect(authed_page.locator("#scheduleSortByGroup")).to_be_visible()
        authed_page.locator("#scheduleSortBy").select_option("random")

        _save_schedule(authed_page, "**/api/schedules")

        assert captured[0][1]["config"] == {"job_type": "full_library", "sort_by": "random"}
