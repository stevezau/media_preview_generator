"""Actual policy API persistence, concurrency guards and legacy compatibility."""

import pytest

from media_preview_generator.web.settings_manager import get_settings_manager
from media_preview_generator.worker_groups import validate_worker_groups

from .test_settings_save_keeps_pause import TOKEN, _reset_singletons, app  # noqa: F401
from .test_worker_group_policy import cpu_group


@pytest.fixture
def client(app):  # noqa: F811 — pytest injects the imported shared fixture.
    client = app.test_client()
    client.environ_base["HTTP_X_AUTH_TOKEN"] = TOKEN
    return client


def save(client, groups):
    state = client.get("/api/worker-groups").get_json()
    response = client.put("/api/worker-groups", json={"groups": groups, "revision": state["revision"]})
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def test_policy_survives_reload_and_stale_edit_cannot_undo_scaling(client):
    state = save(client, [cpu_group()])
    response = client.post("/api/worker-groups/cpu-a/scale", json={"delta": 1})
    assert response.status_code == 200
    assert response.get_json()["groups"][0]["count"] == 3
    stale = client.put("/api/worker-groups", json={"groups": state["groups"], "revision": state["revision"]})
    assert stale.status_code == 409
    assert client.get("/api/worker-groups").get_json()["groups"][0]["count"] == 3


def test_last_group_disable_keeps_manual_and_quiet_holds_independent(client):
    save(client, [cpu_group(count=1)])
    response = client.post("/api/worker-groups/cpu-a/scale", json={"delta": -1}).get_json()
    assert response["groups"][0]["count"] == 1
    assert response["groups"][0]["enabled"] is False
    assert response["processing_paused"] is False
    client.post("/api/processing/pause")
    enabled = client.post("/api/worker-groups/cpu-a/scale", json={"enabled": True}).get_json()
    assert enabled["processing_paused"] is True
    assert enabled["capacity"]["groups"][0]["available"] == 0
    settings = get_settings_manager()
    settings.set_processing_pause_reason("quiet_hours", True)
    resumed = client.post("/api/processing/resume").get_json()
    assert resumed == {"paused": True, "reasons": ["quiet_hours"]}


def test_quick_scale_uses_aggregate_validation_and_preserves_revision_on_failure(client):
    state = save(client, [cpu_group(count=32), cpu_group(id="cpu-b", count=32)])
    response = client.post("/api/worker-groups/cpu-a/scale", json={"delta": 1})
    assert response.status_code == 400
    current = client.get("/api/worker-groups").get_json()
    assert current["revision"] == state["revision"]
    assert current["capacity"]["peak"]["cpu"] == 64


def test_gpu_cannot_accept_loudness_and_bad_policy_is_not_saved(client):
    state = save(client, [cpu_group()])
    response = client.put(
        "/api/worker-groups",
        json={"revision": state["revision"], "groups": [cpu_group(resource="gpu", device="cuda:0")]},
    )
    assert response.status_code == 400
    assert "requires CPU" in response.get_json()["error"]
    assert client.get("/api/worker-groups").get_json()["groups"] == state["groups"]


def test_legacy_count_updates_one_group_but_refuses_ambiguous_resource(client):
    save(client, [cpu_group()])
    response = client.post("/api/settings", json={"cpu_threads": 4})
    assert response.status_code == 200
    assert get_settings_manager().worker_groups[0]["members"][0]["count"] == 4
    assert client.post("/api/settings", json={"cpu_threads": -1}).status_code == 400
    assert client.post("/api/settings", json={"cpu_threads": 65}).status_code == 400
    save(client, [cpu_group(), cpu_group(id="cpu-b")])
    assert client.post("/api/settings", json={"cpu_threads": 1}).status_code == 400
    assert client.post("/api/workers/add", json={"worker_type": "CPU", "count": 1}).status_code == 409


def test_gpu_tuning_does_not_change_group_counts_or_permissions(client):
    group = cpu_group(resource="gpu", device="cuda:0", job_types=["previews"])
    save(client, [group])
    response = client.post("/api/settings", json={"gpu_config": [{"device": "cuda:0", "ffmpeg_threads": 4}]})
    assert response.status_code == 200
    settings = get_settings_manager()
    assert settings.worker_groups == validate_worker_groups([group])
    assert settings.gpu_config[0]["ffmpeg_threads"] == 4


def test_intentional_empty_groups_do_not_fall_back_to_legacy_counts(client):
    response = save(client, [])
    assert response["groups"] == []
    assert response["capacity"]["current"] == {"cpu": 0, "gpu": 0}
    assert response["processing_paused"] is False
    settings = get_settings_manager()
    assert settings.cpu_threads == settings.gpu_threads == 0


def test_configuration_warning_accounts_for_global_quiet_hours(client):
    group = cpu_group(
        job_types=["previews"],
        availability={"mode": "scheduled", "windows": [{"days": [0], "start": "23:00", "end": "07:00"}]},
    )
    save(client, [group])
    get_settings_manager().set(
        "quiet_hours",
        {
            "enabled": True,
            "day_basis": "start",
            "windows": [{"days": ["mon"], "start": "23:00", "end": "07:00"}],
        },
    )
    payload = client.get("/api/worker-groups").get_json()
    assert any(w["code"] == "no_eligible_workers" and w["job_type"] == "previews" for w in payload["warnings"])


@pytest.mark.parametrize(
    ("tz_value", "month", "expected_zone", "expected_label"),
    [
        (None, 10, "Local time", "Local time (UTC+11:00)"),
        (None, 7, "Local time", "Local time (UTC+10:00)"),
        ("UTC", 10, "UTC", "UTC"),
        ("Australia/Sydney", 10, "Australia/Sydney", "Australia/Sydney"),
    ],
)
def test_timezone_payload_labels_actual_mounted_offset(
    client, monkeypatch, tz_value, month, expected_zone, expected_label
):
    import io
    from datetime import UTC, datetime
    from pathlib import Path
    from types import SimpleNamespace

    from media_preview_generator import worker_groups as policy
    from media_preview_generator.web.routes import api_worker_groups

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, month, 5, tzinfo=UTC).astimezone(tz)

    sydney_bytes = Path("/usr/share/zoneinfo/Australia/Sydney").read_bytes()
    monkeypatch.setattr(policy, "open", lambda *args, **kwargs: io.BytesIO(sydney_bytes), raising=False)
    monkeypatch.setattr(
        policy, "Path", lambda _: SimpleNamespace(resolve=lambda: Path("/usr/share/zoneinfo/Etc/UTC")), raising=False
    )
    monkeypatch.setattr(api_worker_groups, "datetime", FixedClock, raising=False)
    if tz_value is None:
        monkeypatch.delenv("TZ", raising=False)
    else:
        monkeypatch.setenv("TZ", tz_value)
    policy._timezone_for_name.cache_clear()
    try:
        response = client.get("/api/worker-groups")
        assert response.status_code == 200
        assert response.json["timezone"] == expected_zone
        assert response.json["timezone_label"] == expected_label
    finally:
        policy._timezone_for_name.cache_clear()


# ---------------------------------------------------------------------------
# v22 shape: members, member scaling, legacy compatibility
# ---------------------------------------------------------------------------

ALWAYS = {"mode": "always", "windows": []}
GPU_JOBS = ["previews", "intro_credits"]


def member(id="m1", resource="cpu", device=None, count=2, job_types=None):
    if job_types is None:
        job_types = ["loudness"] if resource == "cpu" else GPU_JOBS
    return {"id": id, "resource": resource, "device": device, "count": count, "job_types": job_types}


def gpu_member(id="g1", device="cuda:0", count=3, job_types=None):
    return member(id=id, resource="gpu", device=device, count=count, job_types=job_types)


def group(id="off", name="Off-hours", members=None, enabled=True, availability=None):
    return {
        "id": id,
        "name": name,
        "enabled": enabled,
        "availability": availability or ALWAYS,
        "members": members if members is not None else [gpu_member(), member(id="c1", count=10)],
    }


@pytest.fixture
def reconcile(monkeypatch):
    from unittest.mock import MagicMock

    mock = MagicMock(return_value=None)
    monkeypatch.setattr("media_preview_generator.web.routes.api_worker_groups.reconcile_group_settings", mock)
    return mock


@pytest.fixture
def hardware(monkeypatch):
    """Detected GPUs for the payload; the test changes ``devices`` to move hardware in and out."""
    devices = ["cuda:0"]
    monkeypatch.setattr(
        "media_preview_generator.web.routes._helpers._ensure_gpu_cache",
        lambda: [{"device": d, "name": f"GPU {d}", "type": "nvidia", "status": "ok"} for d in devices],
    )
    return devices


@pytest.fixture
def live_pool(monkeypatch):
    """Fake shared pool whose ``member_snapshots`` rows the test fills in."""
    from types import SimpleNamespace

    rows = []
    pool = SimpleNamespace(member_snapshots=lambda: rows)
    monkeypatch.setattr("media_preview_generator.web.routes.api_jobs._get_shared_worker_pool", lambda: pool)
    return rows


def stored(client):
    return client.get("/api/worker-groups").get_json()


class TestGetShape:
    def test_group_lists_members_and_limits(self, client, hardware):
        save(client, [group()])
        payload = stored(client)
        assert payload["limits"] == {"cpu": 64, "gpu": 64, "members": 8}
        assert [m["id"] for m in payload["groups"][0]["members"]] == ["g1", "c1"]
        assert payload["groups"][0]["members"][0] == gpu_member()

    def test_single_member_group_echoes_legacy_fields(self, client):
        save(client, [group(id="solo", members=[gpu_member(count=4)])])
        row = stored(client)["groups"][0]
        assert (row["resource"], row["device"], row["count"], row["job_types"]) == ("gpu", "cuda:0", 4, GPU_JOBS)

    def test_multi_member_group_has_no_legacy_echo(self, client):
        save(client, [group()])
        row = stored(client)["groups"][0]
        assert not {"resource", "device", "count", "job_types"} & row.keys()

    def test_capacity_group_row_sums_member_rows(self, client, hardware, live_pool):
        save(client, [group()])
        live_pool.extend(
            [
                {"group_id": "off", "member_id": "g1", "target": 3, "running": 3, "available": 0, "finishing": 0},
                {"group_id": "off", "member_id": "c1", "target": 10, "running": 8, "available": 2, "finishing": 1},
            ]
        )
        capacity = stored(client)["capacity"]
        row = capacity["groups"][0]
        gpu_row, cpu_row = row["members"]
        assert (gpu_row["desired"], gpu_row["target"], gpu_row["available"], gpu_row["busy"]) == (3, 3, 0, 3)
        assert (cpu_row["desired"], cpu_row["target"], cpu_row["available"], cpu_row["busy"]) == (10, 10, 2, 7)
        assert cpu_row["finishing"] == 1
        assert (row["desired"], row["target"], row["available"], row["busy"], row["finishing"]) == (13, 13, 2, 10, 1)
        assert row["state"] == "active"
        assert capacity["current"] == {"cpu": 10, "gpu": 3}
        assert capacity["peak"] == {"cpu": 10, "gpu": 3}

    def test_missing_gpu_marks_only_that_member_and_warns_with_member_id(self, client, hardware):
        hardware.clear()
        save(client, [group()])
        payload = stored(client)
        row = payload["capacity"]["groups"][0]
        states = {m["id"]: m["state"] for m in row["members"]}
        assert states == {"g1": "hardware_unavailable", "c1": "active"}
        assert row["state"] == "active"
        assert row["target"] == 10
        assert payload["warnings"] == [
            {
                "code": "hardware_unavailable",
                "group_id": "off",
                "member_id": "g1",
                "message": "Off-hours: GPU cuda:0 not detected. Its jobs wait; other devices keep working.",
            }
        ]

    def test_group_is_hardware_unavailable_when_every_member_is_missing(self, client, hardware):
        hardware.clear()
        save(client, [group(members=[gpu_member(), gpu_member(id="g2", device="cuda:1")])])
        row = stored(client)["capacity"]["groups"][0]
        assert row["state"] == "hardware_unavailable"
        assert row["target"] == 0

    def test_disabled_group_has_zero_desired_and_disabled_state(self, client, hardware):
        save(client, [group(enabled=False)])
        row = stored(client)["capacity"]["groups"][0]
        assert (row["desired"], row["target"], row["state"]) == (0, 0, "disabled")
        assert {m["state"] for m in row["members"]} == {"disabled"}

    def test_closed_hours_report_off_hours_with_next_opening(self, client, hardware, monkeypatch):
        from datetime import datetime

        from media_preview_generator import worker_groups as policy

        noon = datetime(2026, 10, 5, 12, 0, tzinfo=policy.application_timezone())
        monkeypatch.setattr(policy, "local_now", lambda now=None: now or noon)
        nightly = {"mode": "scheduled", "windows": [{"days": [0, 1, 2, 3, 4, 5, 6], "start": "01:00", "end": "07:00"}]}
        save(client, [group(availability=nightly)])
        row = stored(client)["capacity"]["groups"][0]
        assert (row["state"], row["target"]) == ("off_hours", 0)
        assert row["next_available_at"] is not None
        assert {m["state"] for m in row["members"]} == {"off_hours"}

    def test_removed_member_still_finishing_is_a_draining_member_row(self, client, hardware, live_pool):
        save(client, [group(members=[gpu_member()])])
        live_pool.append(
            {"group_id": "off", "member_id": "gone", "resource": "cpu", "device": None, "target": 0, "running": 2}
        )
        members = stored(client)["capacity"]["groups"][0]["members"]
        gone = next(m for m in members if m["id"] == "gone")
        assert (gone["desired"], gone["finishing"], gone["state"]) == (0, 2, "draining")

    def test_real_pool_reports_a_busy_removed_member_as_draining(self, client, hardware, monkeypatch):
        from media_preview_generator.jobs.worker import WorkerPool

        gpu = [("nvidia", "cuda:0", {"name": "GPU cuda:0", "workers": 3, "ffmpeg_threads": 2})]
        pool = WorkerPool(0, 0, gpu)
        monkeypatch.setattr("media_preview_generator.web.routes.api_jobs._get_shared_worker_pool", lambda: pool)
        both = [group()]
        save(client, both)
        pool.reconcile_groups(both, gpu)
        busy = next(w for w in pool.workers if w.member_id == "c1")
        busy.is_busy = True

        only_gpu = [group(members=[gpu_member()])]
        save(client, only_gpu)
        pool.reconcile_groups(only_gpu, gpu)

        members = stored(client)["capacity"]["groups"][0]["members"]
        gone = next(m for m in members if m["id"] == "c1")
        assert (gone["desired"], gone["finishing"], gone["state"]) == (0, 1, "draining")
        assert next(m for m in members if m["id"] == "g1")["state"] != "draining"
        assert [w for w in pool.workers if w.member_id == "c1"] == [busy]

    def test_removed_group_still_finishing_keeps_a_draining_row(self, client, hardware, live_pool):
        save(client, [group()])
        live_pool.append(
            {"group_id": "old", "member_id": "m1", "name": "Old", "resource": "cpu", "device": None, "running": 1}
        )
        rows = stored(client)["capacity"]["groups"]
        old = next(r for r in rows if r["id"] == "old")
        assert (old["name"], old["state"], old["desired"], old["finishing"]) == ("Old", "draining", 0, 1)
        assert old["members"][0]["id"] == "m1"


class TestPut:
    def test_v22_members_are_saved_as_given(self, client):
        saved = save(client, [group()])
        assert [m["id"] for m in saved["groups"][0]["members"]] == ["g1", "c1"]
        assert get_settings_manager().worker_groups == [group()]

    def test_flat_v21_group_becomes_member_m1(self, client):
        save(client, [cpu_group(count=5)])
        saved = get_settings_manager().worker_groups[0]
        assert saved["members"] == [member(id="m1", count=5)]
        assert "count" not in saved

    def test_stale_revision_is_409_and_nothing_is_saved(self, client):
        state = save(client, [group()])
        response = client.put("/api/worker-groups", json={"groups": [], "revision": state["revision"] - 1})
        assert response.status_code == 409
        assert response.get_json()["revision"] == state["revision"]
        assert len(get_settings_manager().worker_groups) == 1

    @pytest.mark.parametrize(
        ("bad", "message"),
        [
            (group(members=[]), "Off-hours: add at least one device"),
            (
                group(members=[gpu_member(job_types=["loudness"])]),
                "Off-hours (cuda:0): Plex loudness requires CPU workers",
            ),
            (
                group(members=[member(count=2), member(id="c2", count=2)]),
                "Off-hours: CPU appears twice; use one row per device",
            ),
            (
                group(members=[member(count=65)]),
                "Off-hours (CPU): worker count must be between 1 and 64; remove the device to use zero",
            ),
        ],
    )
    def test_invalid_group_is_400_with_exact_message(self, client, bad, message):
        state = stored(client)
        response = client.put("/api/worker-groups", json={"groups": [bad], "revision": state["revision"]})
        assert response.status_code == 400
        assert response.get_json()["error"] == message

    def test_peak_across_two_groups_members_is_refused_at_65_and_accepted_at_64(self, client):
        state = stored(client)
        over = [group(id="a", members=[member(count=33)]), group(id="b", members=[member(count=32)])]
        refused = client.put("/api/worker-groups", json={"groups": over, "revision": state["revision"]})
        assert refused.status_code == 400
        assert "peak CPU 65/64" in refused.get_json()["error"]
        over[0]["members"][0]["count"] = 32
        assert client.put("/api/worker-groups", json={"groups": over, "revision": state["revision"]}).status_code == 200

    def test_put_reconciles_once(self, client, reconcile):
        save(client, [group()])
        reconcile.assert_called_once_with(get_settings_manager())


class TestMemberScale:
    def url(self, group_id="off", member_id="c1"):
        return f"/api/worker-groups/{group_id}/members/{member_id}/scale"

    def test_plus_one_changes_only_that_member_and_bumps_revision(self, client, reconcile):
        state = save(client, [group()])
        reconcile.reset_mock()
        response = client.post(self.url(), json={"delta": 1})
        assert response.status_code == 200
        body = response.get_json()
        assert body["success"] is True
        saved = get_settings_manager().worker_groups[0]["members"]
        assert [(m["id"], m["count"]) for m in saved] == [("g1", 3), ("c1", 11)]
        assert body["revision"] == state["revision"] + 1
        reconcile.assert_called_once_with(get_settings_manager())

    def test_minus_one_changes_only_that_member(self, client, reconcile):
        save(client, [group()])
        assert client.post(self.url("off", "g1"), json={"delta": -1}).status_code == 200
        saved = get_settings_manager().worker_groups[0]["members"]
        assert [(m["id"], m["count"]) for m in saved] == [("g1", 2), ("c1", 10)]

    @pytest.mark.parametrize(("count", "delta"), [(1, -1), (64, 1)])
    def test_count_outside_1_to_64_is_400_and_unsaved(self, client, reconcile, count, delta):
        state = save(client, [group(members=[member(count=count)])])
        reconcile.reset_mock()
        response = client.post(self.url(member_id="m1"), json={"delta": delta})
        assert response.status_code == 400
        assert response.get_json()["error"] == "A device needs 1\u201364 workers. Remove it in Settings to use zero."
        assert stored(client)["revision"] == state["revision"]
        reconcile.assert_not_called()

    def test_disabled_group_is_409(self, client, reconcile):
        save(client, [group(enabled=False)])
        response = client.post(self.url(), json={"delta": 1})
        assert response.status_code == 409
        assert response.get_json()["error"] == "Enable the group first"

    def test_unknown_group_is_404(self, client):
        response = client.post(self.url("nope", "c1"), json={"delta": 1})
        assert response.status_code == 404
        assert response.get_json()["error"] == "Worker group no longer exists"

    def test_unknown_member_is_404(self, client):
        save(client, [group()])
        response = client.post(self.url(member_id="nope"), json={"delta": 1})
        assert response.status_code == 404
        assert response.get_json()["error"] == "That device is no longer in this group"

    def test_peak_overflow_is_400_and_revision_kept(self, client, reconcile):
        state = save(client, [group(id="a", members=[member(count=32)]), group(id="b", members=[member(count=32)])])
        response = client.post(self.url("a", "m1"), json={"delta": 1})
        assert response.status_code == 400
        assert "peak CPU 65/64" in response.get_json()["error"]
        assert stored(client)["revision"] == state["revision"]

    @pytest.mark.parametrize("body", [{}, {"delta": 2}, {"delta": 0}, {"delta": "1"}, {"delta": 1, "x": 1}, [1]])
    def test_bad_body_is_400(self, client, body):
        save(client, [group()])
        assert client.post(self.url(), json=body).status_code == 400


class TestGroupScale:
    def test_single_member_delta_scales_that_member(self, client, reconcile):
        save(client, [group(id="solo", members=[member(count=2)])])
        assert client.post("/api/worker-groups/solo/scale", json={"delta": 1}).status_code == 200
        assert get_settings_manager().worker_groups[0]["members"][0]["count"] == 3
        reconcile.assert_called_with(get_settings_manager())

    def test_single_member_minus_one_at_one_disables_group_and_keeps_count(self, client, reconcile):
        save(client, [group(id="solo", members=[member(count=1)])])
        body = client.post("/api/worker-groups/solo/scale", json={"delta": -1}).get_json()
        saved = get_settings_manager().worker_groups[0]
        assert saved["enabled"] is False
        assert saved["members"][0]["count"] == 1
        assert body["groups"][0]["enabled"] is False

    def test_multi_member_delta_is_409_listing_member_ids(self, client, reconcile):
        state = save(client, [group()])
        response = client.post("/api/worker-groups/off/scale", json={"delta": 1})
        assert response.status_code == 409
        assert response.get_json() == {"error": "Choose a device to scale", "members": ["g1", "c1"]}
        assert stored(client)["revision"] == state["revision"]

    @pytest.mark.parametrize("members", [[member()], [gpu_member(), member(id="c1")]])
    def test_enabled_toggles_every_member_group(self, client, reconcile, members):
        save(client, [group(id="g", members=members)])
        client.post("/api/worker-groups/g/scale", json={"enabled": False})
        assert get_settings_manager().worker_groups[0]["enabled"] is False
        client.post("/api/worker-groups/g/scale", json={"enabled": True})
        assert get_settings_manager().worker_groups[0]["enabled"] is True

    def test_unknown_group_is_404(self, client):
        assert client.post("/api/worker-groups/nope/scale", json={"delta": 1}).status_code == 404
