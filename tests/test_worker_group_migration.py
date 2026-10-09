"""Upgrade preserves capacity, overnight meaning and intended global holds."""

from datetime import UTC, datetime

import pytest

from media_preview_generator import worker_groups
from media_preview_generator.quiet_hours import quiet_hours_weekly_mask
from media_preview_generator.upgrade import _USER_FACING_NOTES, _migrate_to_v21, _migrate_to_v22
from media_preview_generator.web.routes import _helpers as helpers
from media_preview_generator.web.settings_manager import SettingsManager


def test_zero_cpu_and_disabled_gpu_stay_disabled(tmp_path):
    settings = SettingsManager(str(tmp_path))
    settings.update(
        {
            "cpu_threads": 0,
            "gpu_config": [{"device": "cuda:0", "name": "GPU", "workers": 2, "enabled": False, "ffmpeg_threads": 3}],
        }
    )
    _migrate_to_v21(settings)
    assert settings.cpu_threads == settings.gpu_threads == 0
    assert len(settings.worker_groups) == 1
    assert settings.worker_groups[0]["members"][0]["count"] == 2
    assert settings.worker_groups[0]["enabled"] is False
    assert settings.gpu_config[0]["ffmpeg_threads"] == 3
    assert "loudness" not in settings.worker_groups[0]["members"][0]["job_types"]


def test_explicit_empty_groups_survive_a_repeated_migration(tmp_path):
    settings = SettingsManager(str(tmp_path))
    settings.update({"cpu_threads": 12, "worker_groups": [], "worker_groups_revision": 4})
    _migrate_to_v21(settings)
    _migrate_to_v21(settings)
    assert settings.worker_groups == []
    assert settings.worker_groups_revision == 4


def test_ambiguous_existing_pause_remains_manual(tmp_path):
    settings = SettingsManager(str(tmp_path))
    settings.update({"processing_paused": True, "cpu_threads": 2})
    _migrate_to_v21(settings)
    assert settings.processing_pause_reasons == ["manual"]
    assert settings.get("processing_pause_preserved") is True
    settings.set_processing_pause_reason("quiet_hours", False)
    assert settings.processing_paused


_MONDAY_NOON = datetime(2026, 5, 4, 12, 0)
_MONDAY_EVENING = datetime(2026, 5, 4, 20, 0)
_WORKDAY_QUIET_HOURS = {"enabled": True, "windows": [{"start": "08:00", "end": "17:00", "days": ["mon"]}]}


def _migrate_paused_at(settings, monkeypatch, now):
    monkeypatch.setattr(worker_groups, "local_now", lambda _now=None: _now or now.replace(tzinfo=UTC))
    settings.update({"processing_paused": True, "cpu_threads": 2, "quiet_hours": _WORKDAY_QUIET_HOURS})
    _migrate_to_v21(settings)


def test_pause_inside_active_quiet_hours_belongs_to_quiet_hours(tmp_path, monkeypatch):
    settings = SettingsManager(str(tmp_path))
    _migrate_paused_at(settings, monkeypatch, _MONDAY_NOON)
    assert settings.processing_pause_reasons == ["quiet_hours"]
    assert settings.get("processing_pause_preserved") is None
    settings.set_processing_pause_reason("quiet_hours", False)
    assert not settings.processing_paused


def test_pause_outside_quiet_hours_window_remains_manual(tmp_path, monkeypatch):
    settings = SettingsManager(str(tmp_path))
    _migrate_paused_at(settings, monkeypatch, _MONDAY_EVENING)
    assert settings.processing_pause_reasons == ["manual"]
    assert settings.get("processing_pause_preserved") is True


def _detected(*devices):
    return [{"type": "NVIDIA", "device": device, "name": f"GPU {device}"} for device in devices]


def test_detected_gpu_missing_from_gpu_config_gets_a_one_worker_group(tmp_path, monkeypatch):
    settings = SettingsManager(str(tmp_path))
    settings.update({"setup_complete": True, "cpu_threads": 0, "gpu_config": []})
    monkeypatch.setattr(helpers, "_ensure_gpu_cache", lambda: _detected("cuda:0"))
    notes = _migrate_to_v21(settings)
    (group,) = settings.worker_groups
    assert group["enabled"] is True
    assert group["members"][0]["device"] == "cuda:0"
    assert group["members"][0]["count"] == 1
    assert any("cuda:0" in note for note in notes)


def test_disabled_configured_gpu_stays_disabled_and_is_not_duplicated(tmp_path, monkeypatch):
    settings = SettingsManager(str(tmp_path))
    settings.update(
        {
            "setup_complete": True,
            "cpu_threads": 0,
            "gpu_config": [{"device": "cuda:0", "name": "GPU", "workers": 2, "enabled": False}],
        }
    )
    monkeypatch.setattr(helpers, "_ensure_gpu_cache", lambda: _detected("cuda:0"))
    _migrate_to_v21(settings)
    (group,) = settings.worker_groups
    assert group["enabled"] is False
    assert group["members"][0]["count"] == 2


def test_gpu_detection_failure_adds_nothing_and_does_not_fail_the_migration(tmp_path, monkeypatch):
    settings = SettingsManager(str(tmp_path))
    settings.update({"setup_complete": True, "cpu_threads": 0, "gpu_config": [{"device": "cuda:0", "workers": 2}]})

    def boom():
        raise RuntimeError("no driver")

    monkeypatch.setattr(helpers, "_ensure_gpu_cache", boom)
    _migrate_to_v21(settings)
    assert [g["members"][0]["device"] for g in settings.worker_groups] == ["cuda:0"]


def test_hand_edited_gpu_config_migrates_without_raising(tmp_path, monkeypatch):
    settings = SettingsManager(str(tmp_path))
    monkeypatch.setattr(helpers, "_ensure_gpu_cache", lambda: [])
    settings.update(
        {
            "cpu_threads": 0,
            "gpu_config": [
                {"device": "cuda:0", "workers": 3},
                {"device": "cuda:0", "workers": 5},
                {"device": "cuda:1", "workers": None},
                {"device": "cuda:2", "workers": 80, "enabled": False},
                {"device": "cuda:3"},
            ],
        }
    )
    _migrate_to_v21(settings)
    counts = {g["members"][0]["device"]: g["members"][0]["count"] for g in settings.worker_groups}
    assert counts == {"cuda:0": 3, "cuda:1": 1, "cuda:2": 64, "cuda:3": 1}


def test_proven_no_worker_pause_becomes_resource_wait(tmp_path):
    settings = SettingsManager(str(tmp_path))
    settings.update({"processing_paused": True, "processing_auto_paused": True, "cpu_threads": 0, "gpu_config": []})
    _migrate_to_v21(settings)
    assert not settings.processing_paused
    assert settings.worker_groups == []
    assert not settings.processing_auto_paused


def test_legacy_overnight_week_intervals_are_preserved(tmp_path):
    settings = SettingsManager(str(tmp_path))
    legacy = {"enabled": True, "windows": [{"days": ["mon"], "start": "23:00", "end": "07:00"}]}
    before = quiet_hours_weekly_mask(legacy)
    settings.set("quiet_hours", legacy)
    _migrate_to_v21(settings)
    migrated = settings.get("quiet_hours")
    assert migrated["day_basis"] == "start"
    assert quiet_hours_weekly_mask(migrated) == before
    assert len(migrated["windows"]) == 2


def test_migration_does_not_enable_new_cpu_capacity(tmp_path):
    settings = SettingsManager(str(tmp_path))
    settings.update({"cpu_threads": 0, "gpu_config": [{"device": "cuda:0", "workers": 4, "enabled": True}]})
    _migrate_to_v21(settings)
    assert settings.gpu_threads == 4
    assert settings.cpu_threads == 0
    assert all(
        m["resource"] == "gpu" and g["availability"]["mode"] == "always"
        for g in settings.worker_groups
        for m in g["members"]
    )


def test_full_upgrade_chain_preserves_pause_without_auto_provenance(tmp_path):
    from media_preview_generator.upgrade import _migrate_schema

    settings = SettingsManager(str(tmp_path))
    settings.update(
        {
            "_schema_version": 18,
            "processing_paused": True,
            "cpu_threads": 0,
            "gpu_config": [{"device": "cuda:0", "enabled": False, "workers": 0}],
        }
    )
    _migrate_schema(settings)
    assert settings.processing_pause_reasons == ["manual"]
    assert settings.processing_paused
    assert settings.get("processing_pause_preserved") is True
    assert settings.cpu_threads == settings.gpu_threads == 0


def test_full_upgrade_chain_clears_only_preexisting_valid_auto_pause(tmp_path):
    from media_preview_generator.upgrade import _migrate_schema

    settings = SettingsManager(str(tmp_path))
    settings.update(
        {
            "_schema_version": 18,
            "processing_paused": True,
            "processing_auto_paused": True,
            "cpu_threads": 0,
            "gpu_config": [{"device": "cuda:0", "enabled": False, "workers": 0}],
        }
    )
    _migrate_schema(settings)
    assert settings.processing_pause_reasons == []
    assert not settings.processing_paused
    assert settings.cpu_threads == settings.gpu_threads == 0


V21_GROUPS = [
    {
        "id": "legacy-gpu-1f3a",
        "name": "NVIDIA RTX 4090",
        "enabled": True,
        "resource": "gpu",
        "device": "cuda:0",
        "count": 3,
        "job_types": ["previews", "intro_credits"],
        "availability": {"mode": "always", "windows": []},
    },
    {
        "id": "cpu-loudness",
        "name": "Overnight audio",
        "enabled": False,
        "resource": "cpu",
        "device": None,
        "count": 10,
        "job_types": ["loudness"],
        "availability": {
            "mode": "scheduled",
            "windows": [{"days": [0, 1, 2, 3, 4, 5, 6], "start": "01:00", "end": "07:00"}],
        },
    },
]


def _store_v21(tmp_path, revision=6):
    settings = SettingsManager(str(tmp_path))
    settings.update({"worker_groups": V21_GROUPS, "worker_groups_revision": revision})
    return settings


def test_v22_wraps_each_group_in_member_m1_and_bumps_revision(tmp_path):
    settings = _store_v21(tmp_path)
    notes = _migrate_to_v22(settings)
    assert len(notes) == 1
    assert notes[0].startswith("v22:")
    stored = settings.get("worker_groups")
    assert [(g["id"], g["name"], g["enabled"], g["availability"]) for g in stored] == [
        (g["id"], g["name"], g["enabled"], g["availability"]) for g in V21_GROUPS
    ]
    assert [g["members"] for g in stored] == [
        [{"id": "m1", "resource": "gpu", "device": "cuda:0", "count": 3, "job_types": ["previews", "intro_credits"]}],
        [{"id": "m1", "resource": "cpu", "device": None, "count": 10, "job_types": ["loudness"]}],
    ]
    assert all("count" not in g and "resource" not in g for g in stored)
    assert settings.worker_groups_revision == 7


def test_v22_is_idempotent_and_adds_no_second_note_or_revision(tmp_path):
    settings = _store_v21(tmp_path)
    _migrate_to_v22(settings)
    before = settings.get("worker_groups")
    assert _migrate_to_v22(settings) == []
    assert settings.get("worker_groups") == before
    assert settings.worker_groups_revision == 7


def test_v22_without_stored_groups_changes_nothing(tmp_path):
    settings = SettingsManager(str(tmp_path))
    assert _migrate_to_v22(settings) == []
    assert settings.get("worker_groups") is None


def test_v22_keeps_an_explicit_empty_list_and_its_revision(tmp_path):
    settings = SettingsManager(str(tmp_path))
    settings.update({"worker_groups": [], "worker_groups_revision": 4})
    assert _migrate_to_v22(settings) == []
    assert settings.worker_groups_revision == 4


def test_v22_refuses_a_stored_list_that_fails_validation(tmp_path):
    settings = SettingsManager(str(tmp_path))
    settings.update({"worker_groups": [{**V21_GROUPS[0], "count": 0}], "worker_groups_revision": 1})
    with pytest.raises(ValueError, match="worker count"):
        _migrate_to_v22(settings)
    assert settings.worker_groups_revision == 1


def test_full_chain_from_v21_gives_the_v22_note_and_revision_bump(tmp_path):
    from media_preview_generator.upgrade import _CURRENT_SCHEMA_VERSION, _migrate_schema

    settings = _store_v21(tmp_path)
    settings.set("_schema_version", 21)
    _migrate_schema(settings)
    assert settings.get("_schema_version") == _CURRENT_SCHEMA_VERSION == 23
    assert settings.worker_groups_revision == 7
    assert settings.get("_pending_migration_notice")["notes"] == [_USER_FACING_NOTES[22]]
    assert settings.worker_groups[0]["members"][0]["id"] == "m1"


def test_full_chain_from_v20_runs_v21_then_a_noop_v22(tmp_path):
    from media_preview_generator.upgrade import _migrate_schema

    settings = SettingsManager(str(tmp_path))
    settings.update({"_schema_version": 20, "cpu_threads": 3, "gpu_config": []})
    _migrate_schema(settings)
    assert settings.worker_groups_revision == 0
    assert settings.worker_groups[0]["id"] == "legacy-cpu"
    assert settings.worker_groups[0]["members"][0]["count"] == 3
    assert settings.get("_pending_migration_notice")["notes"] == [_USER_FACING_NOTES[21]]


def test_crash_after_v22_wrote_groups_but_before_the_stamp_only_restamps(tmp_path):
    from media_preview_generator.upgrade import _CURRENT_SCHEMA_VERSION, _migrate_schema

    settings = _store_v21(tmp_path)
    _migrate_to_v22(settings)
    converted = settings.get("worker_groups")
    settings.set("_schema_version", 21)

    _migrate_schema(settings)

    assert settings.get("_schema_version") == _CURRENT_SCHEMA_VERSION
    assert settings.get("worker_groups") == converted
    assert settings.worker_groups_revision == 7
    assert settings.get("_pending_migration_notice") is None


def test_legacy_gpus_past_the_total_cap_are_trimmed_not_fatal() -> None:
    """Five GPUs at the old per-GPU maximum of 16 exceed the 64 total; the app must still start."""
    from media_preview_generator.worker_groups import groups_from_legacy

    gpu_config = [{"device": f"/dev/dri/renderD{128 + i}", "workers": 16, "enabled": True} for i in range(5)]
    groups = groups_from_legacy({"cpu_threads": 0, "gpu_config": gpu_config})

    enabled = [(g["enabled"], g["members"][0]["count"]) for g in groups]
    assert enabled == [(True, 16), (True, 16), (True, 16), (True, 16), (False, 16)]


def test_legacy_gpu_with_zero_workers_stays_off() -> None:
    from media_preview_generator.worker_groups import groups_from_legacy

    groups = groups_from_legacy({"cpu_threads": 0, "gpu_config": [{"device": "/dev/dri/renderD128", "workers": 0}]})

    assert groups[0]["enabled"] is False


def test_fresh_install_does_not_detect_gpus_during_migration(tmp_path, monkeypatch):
    settings = SettingsManager(str(tmp_path))
    settings.update({"cpu_threads": 1})

    def must_not_detect():
        raise AssertionError("fresh installs choose GPUs in setup")

    monkeypatch.setattr(helpers, "_ensure_gpu_cache", must_not_detect)
    _migrate_to_v21(settings)
    assert [g["members"][0]["resource"] for g in settings.worker_groups] == ["cpu"]
