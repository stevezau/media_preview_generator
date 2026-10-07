"""Upgrade preserves capacity, overnight meaning and intended global holds."""

import pytest

from media_preview_generator.quiet_hours import quiet_hours_weekly_mask
from media_preview_generator.upgrade import _USER_FACING_NOTES, _migrate_to_v21, _migrate_to_v22
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
    assert settings.get("_schema_version") == _CURRENT_SCHEMA_VERSION == 22
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
