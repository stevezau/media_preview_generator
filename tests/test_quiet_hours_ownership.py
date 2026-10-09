"""Weekly migration and independent holds are actual policy, not cron guesses."""

from datetime import datetime
from unittest.mock import patch

import pytest

from media_preview_generator.quiet_hours import migrate_quiet_hours, quiet_hours_weekly_mask
from media_preview_generator.web.scheduler import _quiet_hours_recompute_and_apply, is_now_in_any_quiet_window
from media_preview_generator.web.settings_manager import SettingsManager


@pytest.mark.parametrize(
    "start,end",
    [
        ("23:00", "07:00"),
        ("08:00", "01:00"),
        ("00:00", "07:00"),
        ("08:00", "17:00"),
        ("08:00", "08:00"),
        ("23:00", "00:00"),
    ],
)
@pytest.mark.parametrize(
    "days", [["mon"], ["sun"], ["mon", "wed", "fri"], ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]]
)
def test_migration_preserves_every_legacy_weekly_minute(start, end, days):
    raw = {"enabled": True, "windows": [{"start": start, "end": end, "days": days}]}
    migrated = migrate_quiet_hours(raw)
    assert migrate_quiet_hours(migrated) == migrated
    mask = quiet_hours_weekly_mask(migrated)
    names = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    for minute in range(10080):
        time = f"{minute % 1440 // 60:02d}:{minute % 60:02d}"
        clock = start <= time < end if start < end else (time >= start or time < end) if start > end else False
        expected = names[minute // 1440] in days and clock
        assert bool(mask & (1 << minute)) == expected, (days, start, end, minute)


def test_new_overnight_days_mean_start_day():
    quiet = {"enabled": True, "day_basis": "start", "windows": [{"start": "23:00", "end": "07:00", "days": ["mon"]}]}
    assert not is_now_in_any_quiet_window(quiet, datetime(2026, 10, 5, 1))
    assert is_now_in_any_quiet_window(quiet, datetime(2026, 10, 6, 1))
    assert not is_now_in_any_quiet_window(quiet, datetime(2026, 10, 6, 7))


def test_quiet_end_retains_manual_hold_and_does_not_drain(tmp_path):
    sm = SettingsManager(str(tmp_path))
    sm.processing_paused = True
    sm.set_processing_pause_reason("quiet_hours", True)
    sm.set("quiet_hours", {"enabled": False, "windows": []})
    with (
        patch("media_preview_generator.web.settings_manager.get_settings_manager", return_value=sm),
        patch("media_preview_generator.web.routes.job_runner.resume_running_and_drain_pending") as drain,
        patch("media_preview_generator.web.jobs.get_job_manager") as manager,
    ):
        _quiet_hours_recompute_and_apply()
    assert sm.processing_paused
    assert sm.get("processing_pause_reasons") == ["manual"]
    drain.assert_not_called()
    manager.assert_not_called()


def test_migrated_monday_overnight_edges_match_effective_windows():
    legacy = {"enabled": True, "windows": [{"start": "23:00", "end": "07:00", "days": ["mon"]}]}
    # Legacy "mon" means Monday 00:00-07:00 plus Monday 23:00-24:00: no latched week-long pause.
    assert is_now_in_any_quiet_window(legacy, datetime(2026, 10, 5, 6, 59))
    assert not is_now_in_any_quiet_window(legacy, datetime(2026, 10, 5, 7, 0))
    assert not is_now_in_any_quiet_window(legacy, datetime(2026, 10, 5, 22, 59))
    assert is_now_in_any_quiet_window(legacy, datetime(2026, 10, 5, 23, 1))
    assert is_now_in_any_quiet_window(legacy, datetime(2026, 10, 5, 23, 59))
    assert not is_now_in_any_quiet_window(legacy, datetime(2026, 10, 6, 0, 0))


def test_apply_quiet_hours_registers_only_the_per_minute_recheck(tmp_path):
    from media_preview_generator.web.scheduler import ScheduleManager

    manager = ScheduleManager(str(tmp_path))
    try:
        with patch("media_preview_generator.web.scheduler._quiet_hours_recompute_and_apply"):
            manager.apply_quiet_hours(
                {"enabled": True, "windows": [{"start": "23:00", "end": "07:00", "days": ["mon"]}]}
            )
        ids = {job.id for job in manager.scheduler.get_jobs()}
        assert "__qh_recheck" in ids
        assert not any(i.startswith(("__qh_pause_", "__qh_resume_")) for i in ids)
    finally:
        manager.stop()
