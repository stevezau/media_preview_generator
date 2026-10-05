"""Worker group UI contracts through the real settings/dashboard templates."""

from __future__ import annotations

import copy
import re

import pytest
from playwright.sync_api import Page, expect

from ._mocks import (
    capture_settings_save,
    mock_dashboard_defaults,
    mock_settings_backups,
    mock_settings_get,
    mock_setup_status,
    mock_system_status,
)

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


@pytest.fixture
def group_api(authed_page: Page) -> dict:
    state = {
        "groups": [
            {
                "id": "cpu-night",
                "name": "Overnight loudness",
                "enabled": True,
                "resource": "cpu",
                "device": None,
                "count": 2,
                "job_types": ["loudness"],
                "availability": {"mode": "scheduled", "windows": [{"days": [0], "start": "23:00", "end": "07:00"}]},
            },
            {
                "id": "gpu-video",
                "name": 'Video <img src=x onerror="window.injected=1">',
                "enabled": True,
                "resource": "gpu",
                "device": "nvidia0",
                "count": 2,
                "job_types": ["previews", "intro_credits"],
                "availability": {"mode": "always", "windows": []},
            },
        ],
        "revision": 7,
        "timezone": "Australia/Sydney",
        "limits": {"cpu": 32, "gpu": 32},
        "hardware": [{"device": "nvidia0", "name": "NVIDIA card", "type": "nvidia", "status": "ok"}],
        "capacity": {
            "groups": [
                {
                    "id": "cpu-night",
                    "desired": 2,
                    "available": 0,
                    "busy": 2,
                    "finishing": 0,
                    "state": "busy",
                    "next_available_at": None,
                },
                {
                    "id": "gpu-video",
                    "desired": 2,
                    "available": 1,
                    "busy": 1,
                    "finishing": 0,
                    "state": "available",
                    "next_available_at": None,
                },
            ]
        },
        "warnings": [],
    }
    control = {"state": state, "writes": [], "conflict": False}

    def route(request):
        method = request.request.method
        if method == "GET":
            request.fulfill(json=control["state"])
            return
        body = request.request.post_data_json
        control["writes"].append((method, request.request.url, body))
        if control["conflict"] or method == "PUT" and body["revision"] != state["revision"]:
            request.fulfill(status=409, json={"error": "Groups changed elsewhere"})
            return
        if method == "PUT":
            state["groups"] = copy.deepcopy(body["groups"])
        else:
            group_id = request.request.url.split("/")[-2]
            group = next(g for g in state["groups"] if g["id"] == group_id)
            runtime = next(g for g in state["capacity"]["groups"] if g["id"] == group_id)
            if "enabled" in body:
                group["enabled"] = body["enabled"]
            else:
                desired = (group["count"] if group["enabled"] else 0) + body["delta"]
                if desired <= 0:
                    group["enabled"] = False
                else:
                    group["count"] = desired
                    group["enabled"] = True
            runtime.update(
                desired=group["count"] if group["enabled"] else 0,
                busy=min(2, group["count"]) if group["enabled"] else 0,
                finishing=max(0, 2 - group["count"]) if group["enabled"] else 2,
                state="busy" if group["enabled"] else "disabled",
            )
        state["revision"] += 1
        request.fulfill(json=state)

    authed_page.route("**/api/worker-groups**", route)
    return control


def settings_page(page: Page, url: str) -> list:
    mock_settings_get(page)
    mock_setup_status(page, complete=True)
    mock_system_status(page)
    mock_settings_backups(page)
    captured = capture_settings_save(page)
    page.goto(url + "/settings")
    expect(page.locator('#workerGroupRows [data-group-id="cpu-night"]')).to_be_visible()
    return captured


def test_settings_draft_apply_payload_and_no_legacy_counts(authed_page: Page, app_url: str, group_api: dict) -> None:
    settings_page(authed_page, app_url)
    page = authed_page
    expect(page.locator("#workerGroupEditor")).to_be_hidden()
    expect(page.locator("#cpuThreads, .gpu-workers, .gpu-enable-toggle")).to_have_count(0)
    page.locator('[data-edit="cpu-night"]').click()
    page.locator("#workerGroupName").fill("CPU audio")
    page.locator("#workerGroupCount").fill("3")
    page.locator('[data-day="1"]').check()
    assert group_api["writes"] == []
    expect(page.locator("#workerGroupEditorApply")).to_be_enabled()
    page.locator("#workerGroupEditorApply").click()
    expect(page.locator("#workerGroupMessage")).to_contain_text("saved")
    assert len(group_api["writes"]) == 1
    method, _, payload = group_api["writes"][0]
    assert method == "PUT"
    assert payload["revision"] == 7
    assert payload["groups"][0]["count"] == 3
    assert payload["groups"][0]["name"] == "CPU audio"
    assert payload["groups"][0]["job_types"] == ["loudness"]
    assert payload["groups"][0]["availability"]["windows"][0] == {"days": [0, 1], "start": "23:00", "end": "07:00"}
    expect(page.locator("#workerGroupEditor")).to_be_hidden()
    page.evaluate("saveAllSettings()")
    # Hardware tuning remains separate from the authoritative group counts.
    assert page.evaluate("collectGpuConfig().every(g => !('workers' in g) && !('enabled' in g))")
    assert page.evaluate("window.injected") is None


def test_invalid_inputs_and_gpu_capability_never_write(authed_page: Page, app_url: str, group_api: dict) -> None:
    settings_page(authed_page, app_url)
    page = authed_page
    page.locator('[data-edit="cpu-night"]').click()
    page.locator("#workerGroupCount").fill("0")
    page.locator("#workerGroupApply").click()
    expect(page.locator("#workerGroupMessage")).to_contain_text("Disable a group")
    page.locator("#workerGroupCount").fill("2")
    page.locator("#wgEnd0").fill("23:00")
    page.locator("#workerGroupApply").click()
    expect(page.locator("#workerGroupMessage")).to_contain_text("different valid")
    page.locator("#workerGroupResource").select_option("nvidia0")
    expect(page.locator('[data-kind="loudness"]')).to_be_disabled()
    expect(page.locator('[data-kind="loudness"]')).not_to_be_checked()
    page.locator("#workerGroupApply").click()
    expect(page.locator("#workerGroupMessage")).to_contain_text("at least one job")
    assert not group_api["writes"]


def test_revision_conflict_preserves_draft_and_discard_reloads(
    authed_page: Page, app_url: str, group_api: dict
) -> None:
    settings_page(authed_page, app_url)
    page = authed_page
    page.locator('[data-edit="cpu-night"]').click()
    page.locator("#workerGroupName").fill("My draft")
    group_api["state"]["revision"] = 8
    group_api["state"]["groups"][0]["count"] = 5
    page.evaluate("WorkerGroups.load()")
    page.locator("#workerGroupApply").click()
    expect(page.locator("#workerGroupMessage")).to_contain_text("draft is preserved")
    expect(page.locator("#workerGroupName")).to_have_value("My draft")
    assert group_api["writes"][0][2]["revision"] == 7
    page.locator("#workerGroupCancel").click()
    expect(page.locator('#workerGroupRows [data-group-id="cpu-night"] .worker-group-count')).to_have_text("5")
    page.locator('[data-edit="cpu-night"]').click()
    expect(page.locator("#workerGroupName")).to_have_value("Overnight loudness")


def test_dashboard_group_configuration_is_read_only_and_links_to_its_editor(
    authed_page: Page, app_url: str, group_api: dict
) -> None:
    page = authed_page
    mock_dashboard_defaults(page)
    page.goto(app_url + "/")
    row = page.locator('#workerGroupDashboard [data-group-id="cpu-night"]')
    expect(row).to_contain_text("2 running")
    expect(row.locator('[data-group-indicator="configured"] summary')).to_have_attribute(
        "aria-label", "2 configured workers. Worker counts set simultaneous tasks, not CPU cores."
    )
    expect(page.locator("#workerGroupDashboard [data-scale], #workerGroupDashboard [data-enable]")).to_have_count(0)
    expect(row.get_by_role("link", name="Edit Overnight loudness")).to_have_attribute(
        "href", "/settings?worker_group=cpu-night#section-workers"
    )
    group_api["state"]["groups"][0]["enabled"] = False
    group_api["state"]["capacity"]["groups"][0].update(busy=0, finishing=2, state="disabled")
    page.evaluate("WorkerGroups.load()")
    expect(row).to_contain_text("Disabled")
    expect(row).to_contain_text("2 finishing")
    assert group_api["writes"] == []


def test_group_editor_stays_beside_selection_with_focus_and_unsaved_drafts(
    authed_page: Page, app_url: str, group_api: dict
) -> None:
    page = authed_page
    settings_page(page, app_url)
    page.locator('[data-edit="cpu-night"]').click()
    name = page.locator("#workerGroupName")
    name.fill("Audio")
    name.press("End")
    name.press_sequentially(" evenings")
    expect(name).to_be_focused()
    expect(name).to_have_value("Audio evenings")
    assert (
        page.evaluate("document.querySelector('[data-group-id=\"cpu-night\"]').nextElementSibling.id")
        == "workerGroupEditor"
    )
    assert page.evaluate("document.querySelector('#workerGroupName').selectionStart") == len("Audio evenings")
    page.locator('[data-edit="gpu-video"]').click()
    assert (
        page.evaluate("document.querySelector('[data-group-id=\"gpu-video\"]').nextElementSibling.id")
        == "workerGroupEditor"
    )
    page.locator('[data-edit="cpu-night"]').click()
    expect(name).to_have_value("Audio evenings")
    page.locator("#workerGroupClose").click()
    expect(page.locator("#workerGroupEditor")).to_be_hidden()
    expect(page.locator("#workerGroupApply")).to_be_visible()
    page.locator('[data-edit="cpu-night"]').click()
    expect(name).to_have_value("Audio evenings")
    page.locator("#workerGroupCancel").click()
    page.locator('[data-edit="cpu-night"]').click()
    expect(name).to_have_value("Overnight loudness")
    assert group_api["writes"] == []


@pytest.mark.parametrize("width", [1440, 390])
def test_direct_group_link_opens_inline_editor_with_keyboard_focus(
    authed_page: Page, app_url: str, group_api: dict, width: int
) -> None:
    page = authed_page
    page.set_viewport_size({"width": width, "height": 900})
    settings_page(page, app_url)
    page.goto(app_url + "/settings?worker_group=gpu-video#section-workers")
    expect(page.locator("#workerGroupName")).to_be_focused()
    expect(page.locator("#workerGroupName")).to_have_value(group_api["state"]["groups"][1]["name"])
    assert (
        page.evaluate("document.querySelector('[data-group-id=\"gpu-video\"]').nextElementSibling.id")
        == "workerGroupEditor"
    )
    expect(page.locator("#workerGroupEditor")).to_have_count(1)


def test_group_refresh_preserves_worker_nodes_and_per_job_pause_occupancy(
    authed_page: Page, app_url: str, group_api: dict
) -> None:
    page = authed_page
    mock_dashboard_defaults(page)
    # Keep the API worker snapshot consistent with the occupancy and per-job pause.
    worker = {
        "worker_id": "CPU-1",
        "worker_type": "CPU",
        "worker_name": "CPU Worker 1",
        "group_id": "cpu-night",
        "group_name": "Overnight loudness",
        "status": "processing",
        "job_id": "paused-job",
        "job_kind": "loudness",
        "paused": True,
        "retiring": False,
        "current_title": "Movie",
        "current_phase": "Loudness 1/1",
        "progress_percent": 0,
    }
    page.route("**/api/jobs/workers", lambda route: route.fulfill(json={"workers": [worker]}))
    page.goto(app_url + "/")
    row = page.locator('#workerGroupDashboard [data-group-id="cpu-night"]')
    expect(row).to_contain_text("1 running")
    expect(row).to_contain_text("1 paused")
    host = page.locator('[data-group-workers="cpu-night"]')
    expect(host.locator("[data-worker-key]")).to_have_count(1)
    page.evaluate(
        "window.savedWorkerNode = document.querySelector('[data-group-workers=\"cpu-night\"] [data-worker-key]')"
    )
    group_api["state"]["processing_paused"] = True
    page.evaluate("WorkerGroups.load()")
    expect(row).to_contain_text("2 paused")
    group_api["state"]["processing_paused"] = False
    page.evaluate("WorkerGroups.load()")
    expect(row).to_contain_text("1 paused")
    expect(row).to_contain_text("1 running")
    assert page.evaluate(
        "window.savedWorkerNode === document.querySelector('[data-group-workers=\"cpu-night\"] [data-worker-key]')"
    )
    assert group_api["state"]["capacity"]["groups"][0]["available"] == 0


def test_quiet_hours_has_one_editor_and_schedules_links_to_it(authed_page: Page, app_url: str, group_api: dict) -> None:
    settings_page(authed_page, app_url)
    page = authed_page
    expect(page.locator("#quietHoursSaveBtn")).to_have_count(1)
    expect(page.locator("#section-worker-quiet-hours")).to_contain_text("scheduled runs are not caught up")
    page.goto(app_url + "/automation?tab=schedules")
    expect(page.locator("#quietHoursSaveBtn")).to_have_count(0)
    expect(page.locator('#section-schedules-quiet-hours a[href="/settings#section-worker-quiet-hours"]')).to_have_count(
        1
    )


@pytest.mark.parametrize(
    ("state", "enabled", "expected"),
    [
        ("active", True, "Mon 23:00–07:00 next day"),
        ("off_hours", True, "Outside hours"),
        ("draining", True, "Outside hours"),
        ("hardware_unavailable", True, "Hardware unavailable"),
        ("disabled", False, "Disabled"),
    ],
)
def test_worker_wait_states_remain_distinct(
    authed_page: Page, app_url: str, group_api: dict, state: str, enabled: bool, expected: str
) -> None:
    mock_dashboard_defaults(authed_page)
    group_api["state"]["groups"][0]["enabled"] = enabled
    row_data = group_api["state"]["capacity"]["groups"][0]
    row_data.update(state=state, busy=0, finishing=0)
    # An open group may report its current opening; that is not a future appointment.
    row_data["next_available_at"] = "2099-10-05T23:00:00+11:00"
    authed_page.goto(app_url + "/")
    row = authed_page.locator('[data-group-id="cpu-night"]')
    expect(row).to_contain_text(expected)
    if state in ("off_hours", "draining"):
        expect(row).to_contain_text("Next")
        expect(row).to_contain_text("Australia/Sydney")
        expect(row.locator('[data-group-indicator="configured"] summary')).to_have_attribute(
            "aria-label", "2 configured workers. Worker counts set simultaneous tasks, not CPU cores."
        )
    else:
        expect(row).not_to_contain_text("Next")


@pytest.mark.parametrize(
    ("zone", "opening", "expected"),
    [
        ("Local time", "2099-01-05T23:00:00+11:00", "Mon 23:00 · Local time (UTC+11:00)"),
        # The current label says summer +11; this future opening uses winter +10.
        ("Local time", "2099-07-06T23:00:00+10:00", "Mon 23:00 · Local time (UTC+10:00)"),
        ("Unknown/Server", "2099-07-06T23:00:00-04:00", "Mon 23:00 · Local time (UTC-04:00)"),
        ("Local time", "2099-07-06T23:00:00Z", "Mon 23:00 · Local time (UTC+00:00)"),
    ],
)
def test_local_server_opening_uses_encoded_wall_time_not_browser_zone(
    authed_page: Page, app_url: str, group_api: dict, zone: str, opening: str, expected: str
) -> None:
    session = authed_page.context.new_cdp_session(authed_page)
    session.send("Emulation.setTimezoneOverride", {"timezoneId": "America/Los_Angeles"})
    mock_dashboard_defaults(authed_page)
    group_api["state"].update(timezone=zone, timezone_label="Local time (UTC+11:00)")
    group_api["state"]["capacity"]["groups"][0].update(state="off_hours", next_available_at=opening)
    authed_page.goto(app_url + "/")
    assert authed_page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone") == "America/Los_Angeles"
    expect(authed_page.locator('[data-group-id="cpu-night"] .worker-group-state')).to_have_text(
        "Outside hours · Next " + expected
    )


def test_worker_timezone_label_is_visible_and_escaped_in_settings(
    authed_page: Page, app_url: str, group_api: dict
) -> None:
    label = 'Local time (UTC+11:00) <img src=x onerror="window.injected=1">'
    group_api["state"].update(timezone="Local time", timezone_label=label)
    group_api["state"]["capacity"].update(current={"cpu": 0, "gpu": 2}, peak={"cpu": 2, "gpu": 2})
    settings_page(authed_page, app_url)
    expect(authed_page.locator("#workerGroupCapacity")).to_contain_text(label)
    authed_page.locator('[data-edit="cpu-night"]').click()
    expect(authed_page.locator("#workerGroupWindows .form-text")).to_contain_text(label)
    expect(authed_page.locator("#workerGroupSettings img")).to_have_count(0)
    assert authed_page.evaluate("window.injected") is None


def test_named_server_timezone_still_converts_opening_from_utc(
    authed_page: Page, app_url: str, group_api: dict
) -> None:
    session = authed_page.context.new_cdp_session(authed_page)
    session.send("Emulation.setTimezoneOverride", {"timezoneId": "America/Los_Angeles"})
    mock_dashboard_defaults(authed_page)
    group_api["state"]["capacity"]["groups"][0].update(state="off_hours", next_available_at="2099-07-06T13:00:00Z")
    authed_page.goto(app_url + "/")
    expect(authed_page.locator('[data-group-id="cpu-night"] .worker-group-state')).to_have_text(
        re.compile(r"Outside hours · Next Mon.*11:00.*PM · Australia/Sydney")
    )


def test_global_pause_and_removed_draining_group_remain_visible(
    authed_page: Page, app_url: str, group_api: dict
) -> None:
    mock_dashboard_defaults(authed_page)
    group_api["state"].update(processing_paused=True, pause_reasons=["manual", "quiet_hours"])
    group_api["state"]["capacity"]["groups"].append(
        {"id": "removed", "name": "Old audio", "resource": "cpu", "busy": 0, "finishing": 1}
    )
    authed_page.goto(app_url + "/")
    expect(authed_page.locator("#workerGroupHold")).to_contain_text("manual pause and global pause schedule")
    expect(authed_page.locator("#workerGroupHold")).to_contain_text(
        "Resume processing and wait for the pause schedule to end."
    )
    expect(authed_page.locator('[data-group-id="cpu-night"]')).to_contain_text("2 paused")
    expect(authed_page.locator('[data-retired-group="removed"]')).to_contain_text("1 finishing after resume")
    expect(authed_page.locator('[data-retired-group="removed"] button')).to_have_count(0)


def test_saving_window_union_and_add_duplicate_remove(authed_page: Page, app_url: str, group_api: dict) -> None:
    settings_page(authed_page, app_url)
    page = authed_page
    page.locator('[data-edit="cpu-night"]').click()
    page.locator("#workerGroupAddWindow").click()
    page.locator("#wgStart1").fill("22:00")
    page.locator("#workerGroupDuplicate").click()
    page.locator("#workerGroupName").fill("Other hours")
    page.locator("#workerGroupApply").click()
    expect(page.locator("#workerGroupMessage")).to_contain_text("saved")
    payload = group_api["writes"][-1][2]
    assert len(payload["groups"]) == 3
    assert len(payload["groups"][0]["availability"]["windows"]) == 2
    duplicate = payload["groups"][-1]
    assert duplicate["id"] != "cpu-night"
    assert duplicate["name"] == "Other hours"
    page.locator('[data-edit="' + duplicate["id"] + '"]').click()
    page.locator("#workerGroupRemove").click()
    page.locator("#workerGroupApply").click()
    expect(page.locator("#workerGroupRows .worker-group-row")).to_have_count(2)
    assert len(group_api["writes"][-1][2]["groups"]) == 2


def test_api_failure_keeps_configuration_uneditable(authed_page: Page, app_url: str) -> None:
    authed_page.route("**/api/worker-groups", lambda route: route.fulfill(status=503, json={"error": "Unavailable"}))
    mock_settings_get(authed_page)
    mock_system_status(authed_page)
    authed_page.goto(app_url + "/settings")
    expect(authed_page.locator("#workerGroupSettings")).to_contain_text("Could not load")
    expect(authed_page.locator("#workerGroupAdd")).to_have_count(0)


def test_worker_card_names_group_and_retiring_phase(authed_page: Page, app_url: str, group_api: dict) -> None:
    mock_dashboard_defaults(authed_page)
    worker = {
        "worker_id": "CPU-1",
        "worker_type": "CPU",
        "worker_name": "CPU Worker 1",
        "group_name": "Overnight loudness",
        "group_id": "cpu-night",
        "retiring": True,
        "status": "processing",
        "current_title": "Movie",
        "current_phase": "Loudness 1/1",
        "ffmpeg_started": False,
        "progress_percent": 0,
    }
    authed_page.route("**/api/jobs/workers", lambda route: route.fulfill(json={"workers": [worker]}))
    authed_page.goto(app_url + "/")
    expect(authed_page.locator('[data-group-id="cpu-night"] strong')).to_have_text("Overnight loudness")
    expect(authed_page.locator("[data-status-badge]")).to_have_text("Finishing current file")
    expect(authed_page.locator("[data-title]")).to_contain_text("Movie")


def test_resume_processing_keeps_server_reported_quiet_hold(authed_page: Page, app_url: str, group_api: dict) -> None:
    page = authed_page
    mock_dashboard_defaults(page)
    page.route(
        "**/api/processing/state",
        lambda route: route.fulfill(json={"paused": True, "reasons": ["quiet_hours", "manual"]}),
    )
    page.route(
        "**/api/processing/resume", lambda route: route.fulfill(json={"paused": True, "reasons": ["quiet_hours"]})
    )
    group_api["state"].update(processing_paused=True, pause_reasons=["quiet_hours"])
    page.goto(app_url + "/")
    page.get_by_role("button", name="Resume all processing", exact=True).click()
    expect(page.locator("#toastBody")).to_contain_text("global pause schedule is still active")
    expect(page.get_by_role("button", name="Resume all processing", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Pause all processing, including current files", exact=True)).to_have_count(
        0
    )


@pytest.mark.parametrize("status", ["pending", "running"])
def test_preview_job_pause_is_independent_and_schedule_resume_remains_held(
    authed_page: Page, app_url: str, group_api: dict, status: str
) -> None:
    page = authed_page
    mock_dashboard_defaults(page)
    job = {
        "id": "preview-pause",
        "kind": "previews",
        "status": status,
        "library_name": "Movies",
        "config": {},
        "paused": False,
        "priority": 2,
        "created_at": "2026-10-05T01:00:00Z",
        "progress": {"percent": 0, "total_items": 1, "processed_items": 0, "current_item": "Waiting"},
    }
    calls = []
    page.route("**/api/jobs?**", lambda route: route.fulfill(json={"jobs": [job], "total": 1, "page": 1}))

    def change(route):
        calls.append(route.request.url)
        job["paused"] = True
        job["config"] = {"pause_reasons": ["schedule"]}
        route.fulfill(json={**job, "processing_paused": False})

    page.route("**/api/jobs/preview-pause/pause", change)
    page.route("**/api/jobs/preview-pause/resume", change)
    page.route("**/api/processing/pause", lambda route: (calls.append("GLOBAL"), route.fulfill(json={"paused": True})))
    page.goto(app_url + "/")
    row = page.locator("#job-row-preview-pause")
    row.get_by_role("button", name="Pause job", exact=True).click()
    expect(row.get_by_role("button", name="Resume job", exact=True)).to_be_visible()
    page.locator("#toastNotification .btn-close").click()
    expect(page.locator("#toastNotification")).to_be_hidden()
    row.get_by_role("button", name="Resume job", exact=True).click()
    expect(page.locator("#toastBody")).to_contain_text("still paused by its schedule")
    assert calls == [app_url + "/api/jobs/preview-pause/pause", app_url + "/api/jobs/preview-pause/resume"]


@pytest.mark.parametrize(("width", "theme"), [(1440, "dark"), (390, "light")])
def test_worker_group_editor_and_dashboard_fit_supported_sizes(
    authed_page: Page, app_url: str, group_api: dict, width: int, theme: str
) -> None:
    import os
    from pathlib import Path

    page = authed_page
    page.set_viewport_size({"width": width, "height": 1000})
    group_api["state"]["groups"][1]["name"] = "NVIDIA TITAN RTX"
    group_api["state"]["hardware"][0]["name"] = "NVIDIA TITAN RTX"
    intel = copy.deepcopy(group_api["state"]["groups"][1])
    intel.update(
        id="intel-video", name="Intel Corporation Raptor Lake-S GT1 [UHD Graphics 770] (rev 04)", device="intel0"
    )
    group_api["state"]["groups"].append(intel)
    group_api["state"]["hardware"].append({"device": "intel0", "name": intel["name"], "status": "ok"})
    group_api["state"]["capacity"]["groups"].append(
        {"id": "intel-video", "desired": 2, "available": 2, "busy": 0, "finishing": 0, "state": "active"}
    )
    screenshots = os.environ.get("WORKER_GROUP_SCREENSHOTS")
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    settings_page(page, app_url)
    page.evaluate("theme => document.documentElement.dataset.bsTheme = theme", theme)
    page.evaluate("document.fonts.ready")
    expect(page.locator("#workerGroupEditor")).to_be_hidden()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    if screenshots:
        Path(screenshots).mkdir(parents=True, exist_ok=True)
        page.locator("#section-workers").screenshot(path=str(Path(screenshots) / f"workers-{width}-{theme}.png"))
    page.locator('[data-edit="cpu-night"]').click()
    expect(page.locator("#workerGroupApplyRow")).to_be_hidden()
    expect(page.locator("#workerGroupName")).to_be_focused()
    expect(page.locator("#workerGroupWindows")).to_contain_text("ends Tuesday")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    if screenshots:
        if width < 576:
            # A viewport capture preserves the real sticky header while the focused editor is in view.
            page.screenshot(path=str(Path(screenshots) / f"editor-{width}-{theme}.png"))
        else:
            page.locator("#section-workers").screenshot(path=str(Path(screenshots) / f"editor-{width}-{theme}.png"))
    page.locator("#workerGroupClose").click()
    mock_dashboard_defaults(page)
    page.goto(app_url + "/")
    page.evaluate("theme => document.documentElement.dataset.bsTheme = theme", theme)
    expect(page.locator("#workerGroupDashboard")).to_contain_text("Overnight loudness")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    if screenshots:
        page.locator(".dashboard-system-card").screenshot(
            path=str(Path(screenshots) / f"dashboard-{width}-{theme}.png")
        )
    group_api["state"].update(processing_paused=True, pause_reasons=["manual"])
    for row in group_api["state"]["capacity"]["groups"]:
        row.update(busy=0, finishing=0, available=0, state="active")
    page.evaluate("WorkerGroups.load()")
    expect(page.locator("#workerGroupHold")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    if screenshots:
        page.locator(".dashboard-system-card").screenshot(
            path=str(Path(screenshots) / f"dashboard-paused-{width}-{theme}.png")
        )
    assert errors == []


def test_paused_idle_groups_show_one_hold_and_no_redundant_zero_activity(
    authed_page: Page, app_url: str, group_api: dict
) -> None:
    mock_dashboard_defaults(authed_page)
    state = group_api["state"]
    state.update(processing_paused=True, pause_reasons=["manual"])
    for row in state["capacity"]["groups"]:
        row.update(busy=0, finishing=0, available=0, state="active")
    state["groups"][1]["name"] = state["hardware"][0]["name"]
    authed_page.goto(app_url + "/")
    expect(authed_page.locator("#workerGroupHold")).to_be_visible()
    expect(authed_page.locator("#workerGroupHold")).to_have_text(
        "Processing paused: manual pause. Resume processing to use available groups."
    )
    expect(authed_page.locator("#workerGroupDashboard .worker-group-counts")).to_have_count(0)
    expect(authed_page.locator("#workerGroupDashboard")).not_to_contain_text("Globally paused")
    gpu = authed_page.locator('[data-group-id="gpu-video"]')
    assert gpu.locator(".worker-group-description").inner_text().count("NVIDIA card") == 1
    expect(gpu.locator('[data-group-indicator="configured"] summary')).to_have_attribute(
        "aria-label", "2 configured workers. Worker counts set simultaneous tasks, not CPU cores."
    )
    expect(gpu.get_by_role("link", name="Edit NVIDIA card", exact=True)).to_have_attribute(
        "href", "/settings?worker_group=gpu-video#section-workers"
    )


def test_named_gpu_keeps_full_hardware_accessible_on_demand(authed_page: Page, app_url: str, group_api: dict) -> None:
    mock_dashboard_defaults(authed_page)
    group_api["state"]["groups"][1]["name"] = "Video work"
    authed_page.goto(app_url + "/")
    gpu = authed_page.locator('[data-group-id="gpu-video"]')
    expect(gpu.locator(".worker-group-hardware > span")).not_to_be_visible()
    gpu.locator(".worker-group-hardware summary").click()
    expect(gpu.locator(".worker-group-hardware > span")).to_be_visible()
    expect(gpu.locator(".worker-group-hardware > span")).to_have_text("NVIDIA card")


@pytest.mark.parametrize("group_id", ["cpu-night", "gpu-video", "deleted-group"])
def test_dashboard_edit_target_survives_reload_and_handles_deleted_group(
    authed_page: Page, app_url: str, group_api: dict, group_id: str
) -> None:
    page = authed_page
    mock_settings_get(page)
    mock_setup_status(page, complete=True)
    mock_system_status(page)
    mock_settings_backups(page)
    page.goto(app_url + "/settings?worker_group=" + group_id + "#section-workers")
    for reload in (False, True):
        if reload:
            page.reload()
        if group_id == "deleted-group":
            expect(page.locator("#workerGroupEditor")).to_be_hidden()
            expect(page.locator("#workerGroupMessage")).to_contain_text("no longer exists")
        else:
            expected = next(group["name"] for group in group_api["state"]["groups"] if group["id"] == group_id)
            expect(page.locator("#workerGroupEditor")).to_be_visible()
            expect(page.locator("#workerGroupName")).to_have_value(expected)
            expect(page.locator("#workerGroupName")).to_be_focused()
            expect(page.locator("#workerGroupEditorApply")).to_be_disabled()
        assert group_api["writes"] == []


def test_live_loudness_worker_shows_audio_activity_without_fabricated_progress(
    authed_page: Page, app_url: str, group_api: dict
) -> None:
    mock_dashboard_defaults(authed_page)
    worker = {
        "worker_id": "CPU-audio",
        "worker_type": "CPU",
        "worker_name": "CPU Worker 1",
        "group_name": "CPU loudness",
        "status": "processing",
        "current_title": "Two audio tracks",
        "current_phase": "Loudness 1/2",
        "ffmpeg_started": False,
        "progress_percent": 0,
        "speed": "0.0x",
        "eta": "-",
    }
    authed_page.route("**/api/jobs/workers", lambda route: route.fulfill(json={"workers": [worker]}))
    authed_page.goto(app_url + "/")
    expect(authed_page.locator("[data-percent]")).to_have_text("Analyzing audio · stream 1/2")
    bar = authed_page.get_by_role("progressbar", name="Loudness analysis", exact=True)
    expect(bar).to_be_visible()
    assert bar.get_attribute("aria-valuenow") is None
    expect(bar.locator(".progress-bar")).to_have_class(re.compile("progress-bar-striped"))
    expect(authed_page.locator("[data-speed]")).not_to_be_visible()
    expect(authed_page.locator("[data-eta]")).not_to_be_visible()
    worker["current_phase"] = "Loudness 2/2"
    authed_page.evaluate("loadWorkerStatuses()")
    expect(authed_page.locator("[data-percent]")).to_have_text("Analyzing audio · stream 2/2")
    worker.update(status="idle", current_phase="")
    authed_page.evaluate("loadWorkerStatuses()")
    idle_bar = authed_page.locator('[data-worker-key="CPU_CPU-audio"] [role="progressbar"]')
    expect(idle_bar).to_have_count(1)
    expect(idle_bar).not_to_be_visible()
