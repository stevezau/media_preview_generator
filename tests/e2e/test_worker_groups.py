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
                "members": [{"id": "m-cpu", "resource": "cpu", "device": None, "count": 2, "job_types": ["loudness"]}],
                "availability": {"mode": "scheduled", "windows": [{"days": [0], "start": "23:00", "end": "07:00"}]},
            },
            {
                "id": "gpu-video",
                "name": 'Video <img src=x onerror="window.injected=1">',
                "enabled": True,
                "members": [
                    {
                        "id": "m-gpu",
                        "resource": "gpu",
                        "device": "nvidia0",
                        "count": 2,
                        "job_types": ["previews", "intro_credits"],
                    }
                ],
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
                    "members": [
                        {"id": "m-cpu", "resource": "cpu", "device": None, "desired": 2, "available": 0, "busy": 2}
                    ],
                },
                {
                    "id": "gpu-video",
                    "desired": 2,
                    "available": 1,
                    "busy": 1,
                    "finishing": 0,
                    "state": "available",
                    "next_available_at": None,
                    "members": [
                        {"id": "m-gpu", "resource": "gpu", "device": "nvidia0", "desired": 2, "available": 1, "busy": 1}
                    ],
                },
            ]
        },
        "warnings": [],
    }
    control = {"state": state, "writes": [], "conflict": False, "error": None}

    def route(request):
        method = request.request.method
        if method == "GET":
            request.fulfill(json=control["state"])
            return
        body = request.request.post_data_json
        control["writes"].append((method, request.request.url, body))
        if control["error"]:
            request.fulfill(status=400, json={"error": control["error"]})
            return
        if control["conflict"] or method == "PUT" and body["revision"] != state["revision"]:
            request.fulfill(status=409, json={"error": "Groups changed elsewhere"})
            return
        if method == "PUT":
            state["groups"] = copy.deepcopy(body["groups"])
        elif "/members/" in request.request.url:
            tail = request.request.url.split("/api/worker-groups/")[1]
            group_id, _, rest = tail.partition("/members/")
            group = next(g for g in state["groups"] if g["id"] == group_id)
            member = next(m for m in group["members"] if m["id"] == rest.split("/")[0])
            member["count"] += body["delta"]
        else:
            group_id = request.request.url.split("/")[-2]
            group = next(g for g in state["groups"] if g["id"] == group_id)
            group["enabled"] = body["enabled"]
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
    page.locator('[data-member="m-cpu"] [data-count]').fill("3")
    page.locator('[data-day="1"]').check()
    assert group_api["writes"] == []
    expect(page.locator("#workerGroupEditorApply")).to_be_enabled()
    page.locator("#workerGroupEditorApply").click()
    expect(page.locator("#workerGroupMessage")).to_contain_text("saved")
    assert len(group_api["writes"]) == 1
    method, _, payload = group_api["writes"][0]
    assert method == "PUT"
    assert payload["revision"] == 7
    member = payload["groups"][0]["members"][0]
    assert member == {"id": "m-cpu", "resource": "cpu", "device": None, "count": 3, "job_types": ["loudness"]}
    assert payload["groups"][0]["name"] == "CPU audio"
    assert not {"resource", "device", "count", "job_types"} & set(payload["groups"][0])
    assert payload["groups"][1]["members"][0]["id"] == "m-gpu"
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
    page.locator('[data-member="m-cpu"] [data-count]').fill("0")
    page.locator("#workerGroupApply").click()
    expect(page.locator("#workerGroupMessage")).to_have_text("CPU: enter 1–32 workers. Remove the device to use zero.")
    expect(page.locator("#workerGroupEditorError")).to_have_text(
        "CPU: enter 1–32 workers. Remove the device to use zero."
    )
    page.locator('[data-member="m-cpu"] [data-count]').fill("2")
    page.locator("#wgEnd0").fill("23:00")
    page.locator("#workerGroupApply").click()
    expect(page.locator("#workerGroupMessage")).to_contain_text("different valid")
    page.locator('[data-member="m-cpu"] [data-pick]').select_option("nvidia0")
    loudness = page.locator('[data-member="m-cpu"] [data-kind="loudness"]')
    expect(loudness).to_be_disabled()
    expect(loudness).to_have_attribute("aria-pressed", "false")
    expect(page.locator('[data-member="m-cpu"]')).to_contain_text("Loudness removed — it runs on CPU only.")
    page.locator("#workerGroupApply").click()
    expect(page.locator("#workerGroupMessage")).to_have_text("NVIDIA card: choose at least one job type.")
    assert not group_api["writes"]


def test_revision_conflict_preserves_draft_and_discard_reloads(
    authed_page: Page, app_url: str, group_api: dict
) -> None:
    settings_page(authed_page, app_url)
    page = authed_page
    page.locator('[data-edit="cpu-night"]').click()
    page.locator("#workerGroupName").fill("My draft")
    group_api["state"]["revision"] = 8
    group_api["state"]["groups"][0]["members"][0]["count"] = 5
    page.evaluate("WorkerGroups.load()")
    page.locator("#workerGroupApply").click()
    expect(page.locator("#workerGroupMessage")).to_contain_text("draft is preserved")
    expect(page.locator("#workerGroupName")).to_have_value("My draft")
    assert group_api["writes"][0][2]["revision"] == 7
    page.locator("#workerGroupCancel").click()
    expect(page.locator('#workerGroupRows [data-group-id="cpu-night"] .worker-group-count')).to_have_text("5")
    page.locator('[data-edit="cpu-night"]').click()
    expect(page.locator("#workerGroupName")).to_have_value("Overnight loudness")


def test_dashboard_group_header_reports_capacity_and_has_no_edit_pencil(
    authed_page: Page, app_url: str, group_api: dict
) -> None:
    page = authed_page
    mock_dashboard_defaults(page)
    page.goto(app_url + "/")
    row = page.locator('#workerGroupDashboard [data-group-id="cpu-night"]')
    expect(row.locator('[data-group-indicator="configured"]')).to_have_attribute(
        "aria-label", "2 of 2 configured workers busy. Worker counts set simultaneous tasks, not CPU cores."
    )
    expect(page.locator("#workerGroupDashboard [data-scale], #workerGroupDashboard [data-enable]")).to_have_count(0)
    expect(row.get_by_role("button", name="Edit Overnight loudness")).to_have_count(0)
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
        "member_id": "m-cpu",
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
    expect(row.locator('[data-group-indicator="configured"]')).to_have_attribute(
        "aria-label", "2 of 2 configured workers busy. Worker counts set simultaneous tasks, not CPU cores."
    )
    expect(row).to_contain_text("1 paused")
    host = page.locator('[data-group-workers="cpu-night:m-cpu"]')
    expect(host.locator("[data-worker-key]")).to_have_count(1)
    page.evaluate(
        "window.savedWorkerNode = document.querySelector('[data-group-workers=\"cpu-night:m-cpu\"] [data-worker-key]')"
    )
    group_api["state"]["processing_paused"] = True
    page.evaluate("WorkerGroups.load()")
    expect(row).to_contain_text("2 paused")
    group_api["state"]["processing_paused"] = False
    page.evaluate("WorkerGroups.load()")
    expect(row).to_contain_text("1 paused")
    assert page.evaluate(
        "window.savedWorkerNode === document.querySelector('[data-group-workers=\"cpu-night:m-cpu\"] [data-worker-key]')"
    )
    assert group_api["state"]["capacity"]["groups"][0]["available"] == 0


def test_quiet_hours_has_one_editor_and_schedules_links_to_it(authed_page: Page, app_url: str, group_api: dict) -> None:
    settings_page(authed_page, app_url)
    page = authed_page
    expect(page.locator("#quietHoursSaveBtn")).to_have_count(1)
    # The rule sits behind the section's info icon (a template for its dialog), not as a visible line.
    assert (
        "scheduled runs are not caught up"
        in page.evaluate("document.getElementById('infoPauseRulesTpl').content.textContent").lower()
    )
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
    # The group's hours ride in the clock icon's label; only exceptional states get words in the header.
    expect(row.get_by_role("img", name="Mon 23:00–07:00 next day")).to_be_visible()
    if state != "active":
        expect(row).to_contain_text(expected)
    if state in ("off_hours", "draining"):
        expect(row).to_contain_text("Next")
        expect(row).to_contain_text("Australia/Sydney")
        expect(row.locator('[data-group-indicator="configured"]')).to_have_attribute(
            "aria-label", "0 of 2 configured workers busy. Worker counts set simultaneous tasks, not CPU cores."
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
        "member_id": "m-cpu",
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
    intel.update(id="intel-video", name="Intel Corporation Raptor Lake-S GT1 [UHD Graphics 770] (rev 04)")
    intel["members"][0]["device"] = "intel0"
    group_api["state"]["groups"].append(intel)
    group_api["state"]["hardware"].append({"device": "intel0", "name": intel["name"], "status": "ok"})
    group_api["state"]["capacity"]["groups"].append(
        {
            "id": "intel-video",
            "desired": 2,
            "available": 2,
            "busy": 0,
            "finishing": 0,
            "state": "active",
            "members": [
                {"id": "m-gpu", "resource": "gpu", "device": "intel0", "desired": 2, "available": 2, "busy": 0}
            ],
        }
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
    expect(page.locator("#workerGroupWindows .tz-note .info-icon")).to_have_attribute(
        "data-explain-html", re.compile("ends Tuesday")
    )
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
    expect(gpu.locator("xpath=..").locator(".devname .nm")).to_have_text("NVIDIA card")
    expect(gpu.locator('[data-group-indicator="configured"]')).to_have_attribute(
        "aria-label", "0 of 2 configured workers busy. Worker counts set simultaneous tasks, not CPU cores."
    )
    expect(gpu.get_by_role("button", name="Edit NVIDIA card", exact=True)).to_have_count(0)


def test_named_gpu_shows_full_hardware_name_on_its_member_row(authed_page: Page, app_url: str, group_api: dict) -> None:
    mock_dashboard_defaults(authed_page)
    group_api["state"]["groups"][1]["name"] = "Video work"
    authed_page.goto(app_url + "/")
    gpu = authed_page.locator('[data-group-id="gpu-video"]')
    expect(gpu.locator("xpath=..").locator(".devname .nm")).to_have_text("NVIDIA card")
    expect(authed_page.locator('[data-system-group="gpu-video"] small')).to_have_text("2 GPU")


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


def test_live_loudness_worker_shows_audio_activity_until_ffmpeg_reports_real_progress(
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
    worker.update(ffmpeg_started=True, progress_percent=62.5, speed="1.4x", eta="3m 20s")
    authed_page.evaluate("loadWorkerStatuses()")
    expect(authed_page.locator("[data-percent]")).to_have_text("62.5%")
    expect(authed_page.locator("[data-speed]")).to_have_text("1.4x")
    expect(authed_page.locator("[data-eta]")).to_have_text("3m 20s")
    assert bar.get_attribute("aria-valuenow") == "62.5"
    worker.update(status="idle", current_phase="")
    authed_page.evaluate("loadWorkerStatuses()")
    idle_bar = authed_page.locator('[data-worker-key="CPU_CPU-audio"] [role="progressbar"]')
    expect(idle_bar).to_have_count(1)
    expect(idle_bar).not_to_be_visible()


def _add_gpu_member(group_api: dict) -> None:
    group_api["state"]["groups"][0]["members"].append(
        {"id": "m-gpu2", "resource": "gpu", "device": "nvidia0", "count": 1, "job_types": ["previews"]}
    )


def test_settings_row_shows_one_chip_per_member(authed_page: Page, app_url: str, group_api: dict) -> None:
    _add_gpu_member(group_api)
    settings_page(authed_page, app_url)
    row = authed_page.locator('#workerGroupRows [data-group-id="cpu-night"]')
    expect(row.locator(".mchip")).to_have_count(2)
    expect(row.locator(".mchip").nth(0)).to_contain_text("CPU")
    expect(row.locator(".mchip").nth(0)).to_contain_text("×2")
    expect(row.locator(".mchip").nth(1)).to_contain_text("NVIDIA card")
    expect(row.locator(".worker-group-count")).to_have_text("3")


def test_device_picker_disables_devices_used_by_other_rows(authed_page: Page, app_url: str, group_api: dict) -> None:
    _add_gpu_member(group_api)
    settings_page(authed_page, app_url)
    authed_page.locator('[data-edit="cpu-night"]').click()
    cpu_row = authed_page.locator('[data-member="m-cpu"] [data-pick]')
    expect(cpu_row.locator('option[value="nvidia0"]')).to_be_disabled()
    expect(cpu_row.locator('option[value="nvidia0"]')).to_have_text("NVIDIA card (already in this group)")
    gpu_row = authed_page.locator('[data-member="m-gpu2"] [data-pick]')
    expect(gpu_row.locator('option[value="cpu"]')).to_be_disabled()
    expect(authed_page.locator("#workerGroupAddDevice")).to_be_disabled()
    expect(authed_page.locator("#workerGroupAddDeviceInfo")).to_be_visible()


def test_add_device_picks_next_unused_and_apply_sends_member_payload(
    authed_page: Page, app_url: str, group_api: dict
) -> None:
    settings_page(authed_page, app_url)
    page = authed_page
    page.locator('[data-edit="cpu-night"]').click()
    page.locator("#workerGroupAddDevice").click()
    expect(page.locator("#workerGroupMembers .mrow")).to_have_count(2)
    expect(page.locator("#workerGroupAddDevice")).to_be_disabled()
    page.locator("#workerGroupEditorApply").click()
    expect(page.locator("#workerGroupMessage")).to_contain_text("saved")
    members = group_api["writes"][-1][2]["groups"][0]["members"]
    assert members[0] == {"id": "m-cpu", "resource": "cpu", "device": None, "count": 2, "job_types": ["loudness"]}
    added = members[1]
    assert added["id"] and added["id"] != "m-cpu"
    assert (added["resource"], added["device"], added["count"]) == ("gpu", "nvidia0", 1)
    assert added["job_types"] == ["previews", "intro_credits"]


def test_gpu_row_loudness_chip_is_disabled_with_explanation(authed_page: Page, app_url: str, group_api: dict) -> None:
    settings_page(authed_page, app_url)
    authed_page.locator('[data-edit="gpu-video"]').click()
    chip = authed_page.locator('[data-member="m-gpu"] [data-kind="loudness"]')
    expect(chip).to_be_disabled()
    expect(chip).to_have_attribute("aria-pressed", "false")
    info = authed_page.locator('[data-member="m-gpu"] .chipwrap .info-icon')
    expect(info).to_have_attribute("data-bs-original-title", "Plex loudness runs on CPU workers only")
    # The CPU chip stays toggleable.
    authed_page.locator('[data-edit="cpu-night"]').click()
    cpu_chip = authed_page.locator('[data-member="m-cpu"] [data-kind="loudness"]')
    expect(cpu_chip).to_be_enabled()
    expect(cpu_chip).to_have_attribute("aria-pressed", "true")


def test_remove_device_and_last_device_is_refused(authed_page: Page, app_url: str, group_api: dict) -> None:
    _add_gpu_member(group_api)
    settings_page(authed_page, app_url)
    page = authed_page
    page.locator('[data-edit="cpu-night"]').click()
    page.locator('[data-member="m-gpu2"] [data-remove-member]').click()
    expect(page.locator("#workerGroupMembers .mrow")).to_have_count(1)
    expect(page.locator("#workerGroupAddDevice")).to_be_enabled()
    page.locator('[data-member="m-cpu"] [data-remove-member]').click()
    expect(page.locator("#workerGroupMembers .mrow")).to_have_count(1)
    expect(page.locator("#workerGroupLastDevice")).to_have_text(
        "A group needs at least one device. Remove the group instead."
    )
    assert group_api["writes"] == []


def test_stepper_buttons_bound_the_count_at_one(authed_page: Page, app_url: str, group_api: dict) -> None:
    settings_page(authed_page, app_url)
    authed_page.locator('[data-edit="cpu-night"]').click()
    row = authed_page.locator('[data-member="m-cpu"]')
    row.locator('[data-step="-1"]').click()
    expect(row.locator("[data-count]")).to_have_value("1")
    expect(row.locator('[data-step="-1"]')).to_be_disabled()
    expect(row.locator('[data-step="-1"]')).to_have_attribute("title", "Remove the device to use zero")
    row.locator('[data-step="1"]').click()
    expect(row.locator("[data-count]")).to_have_value("2")


def test_server_peak_error_is_shown_in_the_editor_foot(authed_page: Page, app_url: str, group_api: dict) -> None:
    settings_page(authed_page, app_url)
    page = authed_page
    page.locator('[data-edit="cpu-night"]').click()
    page.locator('[data-member="m-cpu"] [data-count]').fill("30")
    message = "Overlapping groups exceed capacity: peak CPU 30/32, GPU 35/32. Reduce counts or use different hours."
    group_api["error"] = message
    page.locator("#workerGroupEditorApply").click()
    expect(page.locator("#workerGroupEditorError")).to_have_text(message)
    expect(page.locator("#workerGroupEditor")).to_be_visible()


def test_week_graph_has_one_lane_per_member_coloured_by_device(
    authed_page: Page, app_url: str, group_api: dict
) -> None:
    _add_gpu_member(group_api)
    settings_page(authed_page, app_url)
    graph = authed_page.locator("#workerWeekGraph")
    cpu = graph.locator('.wkg-seg[title^="Overnight loudness · CPU ×2 · Mon"]').first
    gpu = graph.locator('.wkg-seg[title^="Overnight loudness · NVIDIA card ×1 · Mon"]').first
    expect(cpu).to_have_attribute("style", re.compile(r"--c:var\(--ok\)"))
    expect(gpu).to_have_attribute("style", re.compile(r"--c:var\(--run\)"))
    # The other group's NVIDIA lane shares the colour: same device, same colour everywhere.
    other = graph.locator('.wkg-seg[title^="Video <img"]').first
    expect(other).to_have_attribute("style", re.compile(r"--c:var\(--run\)"))
    legend = authed_page.locator("#workerWeekLegend")
    expect(legend).to_contain_text("CPU")
    expect(legend).to_contain_text("NVIDIA card")
    expect(legend).to_contain_text("Global pause")


def test_editor_at_390px_has_no_horizontal_scroll_and_44px_targets(
    authed_page: Page, app_url: str, group_api: dict
) -> None:
    _add_gpu_member(group_api)
    page = authed_page
    page.set_viewport_size({"width": 390, "height": 900})
    settings_page(page, app_url)
    page.locator('[data-edit="cpu-night"]').click()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    small = page.evaluate(
        """() => [...document.querySelectorAll(
            '#workerGroupMembers button, #workerGroupMembers select, #workerGroupMembers input, #workerGroupAddDevice')]
            .filter(el => el.offsetParent !== null)
            .map(el => [el.getAttribute('aria-label') || el.id || el.className, el.getBoundingClientRect()])
            .filter(([, box]) => box.height < 43.5 || box.width < 43.5)
            .map(([name]) => name)"""
    )
    assert small == []
