"""Tests for media_preview_generator.web.scheduler."""

import os
from collections.abc import Callable
from datetime import UTC, datetime, tzinfo
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

# Imported before any test patches ``web.jobs.get_job_manager``: both bind it at import time, and a first import under
# that patch would keep the fake for every later test in the worker.
from media_preview_generator.markers import reconcile, triggers
from media_preview_generator.web.scheduler import (
    ScheduleManager,
    get_schedule_manager,
)


@pytest.fixture
def start_scheduler_manager(tmp_path, monkeypatch):
    """Factory: build and start a ScheduleManager as the global singleton; stops it after the test.

    A factory rather than an instance so a test can patch the clock or timezone first.
    """
    config_dir = str(tmp_path / "config")
    os.makedirs(config_dir, exist_ok=True)
    started: list[ScheduleManager] = []

    def _start() -> ScheduleManager:
        manager = ScheduleManager(config_dir=config_dir, run_job_callback=None)
        # Make this the global singleton so execute_scheduled_job uses it
        monkeypatch.setattr("media_preview_generator.web.scheduler._schedule_manager", manager)
        manager.start()
        started.append(manager)
        return manager

    yield _start

    for manager in started:
        manager.stop()


@pytest.fixture
def scheduler_manager(start_scheduler_manager):
    """Create and start a ScheduleManager, set as global singleton, clean up after."""
    return start_scheduler_manager()


# ========================================================================
# Schedule CRUD
# ========================================================================


class TestScheduleCRUD:
    """Tests for schedule create/read/update/delete operations."""

    def test_create_schedule_with_cron(self, scheduler_manager):
        """Test creating a schedule with a cron expression."""
        schedule = scheduler_manager.create_schedule(
            name="Daily Backup",
            library_id="123",
            library_name="Movies",
            cron_expression="0 2 * * *",
            enabled=True,
        )

        assert schedule["id"] is not None
        assert schedule["name"] == "Daily Backup"
        assert schedule["library_id"] == "123"
        assert schedule["library_name"] == "Movies"
        assert schedule["trigger_type"] == "cron"
        assert schedule["trigger_value"] == "0 2 * * *"
        assert schedule["enabled"] is True
        assert schedule["last_run"] is None
        assert "next_run" in schedule

    def test_create_schedule_with_interval(self, scheduler_manager):
        """Test creating a schedule with an interval trigger."""
        schedule = scheduler_manager.create_schedule(
            name="Hourly Sync",
            library_id="456",
            library_name="TV Shows",
            interval_minutes=60,
            enabled=True,
        )

        assert schedule["trigger_type"] == "interval"
        assert schedule["trigger_value"] == "60"
        assert schedule["enabled"] is True

    def test_create_schedule_disabled(self, scheduler_manager):
        """Test creating a disabled schedule."""
        schedule = scheduler_manager.create_schedule(
            name="Disabled",
            library_id="123",
            library_name="Movies",
            cron_expression="0 2 * * *",
            enabled=False,
        )

        assert schedule["enabled"] is False
        assert schedule["next_run"] is None

    def test_create_schedule_with_config(self, scheduler_manager):
        """Test creating a schedule with custom config."""
        config = {"quality": "high", "threads": 4}
        schedule = scheduler_manager.create_schedule(
            name="Custom",
            library_id="789",
            library_name="Movies",
            cron_expression="0 3 * * *",
            config=config,
        )

        assert schedule["config"] == {**config, "job_kinds": ["previews"], "job_type": "full_library"}

    def test_create_schedule_no_trigger_raises(self, scheduler_manager):
        """Test that creating a schedule without cron or interval raises ValueError."""
        with pytest.raises(ValueError, match="cron_expression or interval_minutes"):
            scheduler_manager.create_schedule(
                name="Bad",
                library_id="123",
                library_name="Movies",
            )

    def test_get_schedule(self, scheduler_manager):
        """Test getting a single schedule by ID."""
        schedule = scheduler_manager.create_schedule(
            name="Test",
            library_id="123",
            library_name="Movies",
            cron_expression="0 2 * * *",
        )

        retrieved = scheduler_manager.get_schedule(schedule["id"])

        assert retrieved is not None
        assert retrieved["id"] == schedule["id"]
        assert retrieved["name"] == "Test"

    def test_get_nonexistent_schedule(self, scheduler_manager):
        """Test that getting a nonexistent schedule returns None."""
        assert scheduler_manager.get_schedule("nonexistent") is None

    def test_get_all_schedules(self, scheduler_manager):
        """Test getting all schedules."""
        scheduler_manager.create_schedule(
            name="S1",
            library_id="1",
            library_name="Movies",
            cron_expression="0 2 * * *",
        )
        scheduler_manager.create_schedule(
            name="S2",
            library_id="2",
            library_name="TV",
            interval_minutes=60,
        )

        schedules = scheduler_manager.get_all_schedules()

        assert len(schedules) == 2
        names = {s["name"] for s in schedules}
        assert names == {"S1", "S2"}

    def test_update_schedule_name(self, scheduler_manager):
        """Test updating a schedule's name."""
        schedule = scheduler_manager.create_schedule(
            name="Old Name",
            library_id="123",
            library_name="Movies",
            cron_expression="0 2 * * *",
        )

        updated = scheduler_manager.update_schedule(schedule["id"], name="New Name")

        assert updated is not None
        assert updated["name"] == "New Name"
        assert updated["library_id"] == "123"

    @pytest.mark.parametrize(
        "bad", [{"cron_expression": "not a cron"}, {"interval_minutes": 0}, {"stop_time": "99:99"}]
    )
    def test_update_with_invalid_trigger_changes_nothing(self, scheduler_manager, bad):
        schedule = scheduler_manager.create_schedule(
            name="Keep", library_id="1", library_name="Movies", cron_expression="0 2 * * *"
        )
        sid = schedule["id"]

        with pytest.raises(ValueError):
            scheduler_manager.update_schedule(sid, name="Changed", **bad)

        stored = scheduler_manager.get_schedule(sid)
        assert stored["name"] == "Keep"
        assert stored["trigger_value"] == "0 2 * * *"
        assert scheduler_manager.scheduler.get_job(sid) is not None

    def test_update_schedule_trigger_cron_to_interval(self, scheduler_manager):
        """Test changing a schedule from cron to interval trigger."""
        schedule = scheduler_manager.create_schedule(
            name="Test",
            library_id="123",
            library_name="Movies",
            cron_expression="0 2 * * *",
        )

        updated = scheduler_manager.update_schedule(schedule["id"], interval_minutes=120)

        assert updated["trigger_type"] == "interval"
        assert updated["trigger_value"] == "120"

    def test_update_schedule_trigger_interval_to_cron(self, scheduler_manager):
        """Test changing a schedule from interval to cron trigger."""
        schedule = scheduler_manager.create_schedule(
            name="Test",
            library_id="123",
            library_name="Movies",
            interval_minutes=60,
        )

        updated = scheduler_manager.update_schedule(schedule["id"], cron_expression="0 3 * * *")

        assert updated["trigger_type"] == "cron"
        assert updated["trigger_value"] == "0 3 * * *"

    def test_update_schedule_omitting_priority_leaves_the_pin(self, scheduler_manager):
        """Not sending ``priority`` must not disturb an existing pin.

        Every other field follows "None = leave alone", so ``priority``
        needs a sentinel to keep that behaviour while still allowing an
        explicit clear (issue #285).
        """
        schedule = scheduler_manager.create_schedule(
            name="Pinned",
            library_id="123",
            library_name="Movies",
            cron_expression="0 2 * * *",
            priority=3,
        )

        updated = scheduler_manager.update_schedule(schedule["id"], name="Renamed")

        assert updated["priority"] == 3

    def test_update_schedule_null_priority_clears_the_pin(self, scheduler_manager):
        """Explicit ``None`` unpins, so the job falls back to the global
        default — for Recently Added sweeps that is
        ``incoming_job_priority`` (issue #285).

        Before the sentinel, ``if priority is not None`` made this a no-op:
        a user switching a schedule back to "Default (from Settings)" saw
        the modal accept the change and the old pin silently survive.
        """
        schedule = scheduler_manager.create_schedule(
            name="Pinned",
            library_id="123",
            library_name="Movies",
            cron_expression="0 2 * * *",
            priority=1,
        )

        updated = scheduler_manager.update_schedule(schedule["id"], priority=None)

        assert updated["priority"] is None

    def test_update_schedule_can_still_set_a_pin(self, scheduler_manager):
        schedule = scheduler_manager.create_schedule(
            name="Unpinned",
            library_id="123",
            library_name="Movies",
            cron_expression="0 2 * * *",
        )
        assert schedule["priority"] is None

        updated = scheduler_manager.update_schedule(schedule["id"], priority=1)

        assert updated["priority"] == 1

    @pytest.mark.parametrize(
        ("stored_priority", "expected_forwarded"),
        [
            (None, None),  # unpinned -> the recently-added path applies the global default
            (1, 1),
            (3, 3),
        ],
    )
    def test_run_now_forwards_the_stored_priority_for_recently_added(
        self, scheduler_manager, stored_priority, expected_forwarded
    ):
        """Drive the REAL caller: a persisted schedule through ``run_now``.

        Calling ``_start_recently_added_job_async(priority=...)`` directly
        proves nothing about what the app does, because the value comes
        from ``schedules.json``. ``None`` has to survive the whole trip for
        the incoming-priority fallback to ever fire.
        """
        schedule = scheduler_manager.create_schedule(
            name="Sweep",
            library_id="123",
            library_name="Movies",
            cron_expression="*/15 * * * *",
            config={"job_type": "recently_added", "lookback_hours": 1},
            priority=stored_priority,
        )

        with patch(
            "media_preview_generator.web.routes.job_runner._start_recently_added_job_async",
            return_value="job-1",
        ) as mock_start:
            assert scheduler_manager.run_now(schedule["id"]) is True

        mock_start.assert_called_once()
        assert mock_start.call_args.kwargs["priority"] == expected_forwarded, (
            f"stored priority {stored_priority!r} must reach the helper unchanged; "
            f"got {mock_start.call_args.kwargs.get('priority')!r}"
        )

    def test_update_nonexistent_schedule(self, scheduler_manager):
        """Test that updating a nonexistent schedule returns None."""
        assert scheduler_manager.update_schedule("nonexistent", name="X") is None

    def test_delete_schedule(self, scheduler_manager):
        """Test deleting a schedule."""
        schedule = scheduler_manager.create_schedule(
            name="Delete Me",
            library_id="123",
            library_name="Movies",
            cron_expression="0 2 * * *",
        )

        assert scheduler_manager.delete_schedule(schedule["id"]) is True
        assert scheduler_manager.get_schedule(schedule["id"]) is None

    def test_delete_nonexistent_schedule(self, scheduler_manager):
        """Test that deleting a nonexistent schedule returns False."""
        assert scheduler_manager.delete_schedule("nonexistent") is False


# ========================================================================
# Per-schedule stop_time (D20)
# ========================================================================


class TestScheduleStopTime:
    """D20 — optional stop_time pauses jobs at a daily time-of-day."""

    def test_create_with_stop_time_registers_stop_cron(self, scheduler_manager):
        """create_schedule with stop_time MUST register both the start and
        the {id}__stop APScheduler jobs so the daily pause actually fires."""
        schedule = scheduler_manager.create_schedule(
            name="Overnight",
            library_id="1",
            library_name="Movies",
            cron_expression="0 1 * * *",
            stop_time="06:00",
        )
        sid = schedule["id"]
        assert schedule["stop_time"] == "06:00"

        all_ids = {j.id for j in scheduler_manager.scheduler.get_jobs()}
        assert sid in all_ids
        assert f"{sid}__stop" in all_ids

    def test_create_without_stop_time_only_registers_start_cron(self, scheduler_manager):
        schedule = scheduler_manager.create_schedule(
            name="Plain",
            library_id="1",
            library_name="Movies",
            cron_expression="0 1 * * *",
        )
        sid = schedule["id"]
        all_ids = {j.id for j in scheduler_manager.scheduler.get_jobs()}
        assert sid in all_ids
        assert f"{sid}__stop" not in all_ids
        assert schedule.get("stop_time", "") == ""

    def test_update_clearing_stop_time_removes_stop_cron(self, scheduler_manager):
        schedule = scheduler_manager.create_schedule(
            name="Overnight",
            library_id="1",
            library_name="Movies",
            cron_expression="0 1 * * *",
            stop_time="06:00",
        )
        sid = schedule["id"]
        assert f"{sid}__stop" in {j.id for j in scheduler_manager.scheduler.get_jobs()}

        scheduler_manager.update_schedule(schedule_id=sid, stop_time="")
        ids_after = {j.id for j in scheduler_manager.scheduler.get_jobs()}
        assert f"{sid}__stop" not in ids_after
        assert scheduler_manager.get_schedule(sid)["stop_time"] == ""

    def test_update_setting_stop_time_adds_stop_cron(self, scheduler_manager):
        schedule = scheduler_manager.create_schedule(
            name="Overnight",
            library_id="1",
            library_name="Movies",
            cron_expression="0 1 * * *",
        )
        sid = schedule["id"]
        scheduler_manager.update_schedule(schedule_id=sid, stop_time="06:00")
        all_ids = {j.id for j in scheduler_manager.scheduler.get_jobs()}
        assert f"{sid}__stop" in all_ids
        assert scheduler_manager.get_schedule(sid)["stop_time"] == "06:00"

    def test_delete_removes_stop_cron(self, scheduler_manager):
        schedule = scheduler_manager.create_schedule(
            name="Overnight",
            library_id="1",
            library_name="Movies",
            cron_expression="0 1 * * *",
            stop_time="06:00",
        )
        sid = schedule["id"]
        scheduler_manager.delete_schedule(sid)
        ids_after = {j.id for j in scheduler_manager.scheduler.get_jobs()}
        assert sid not in ids_after
        assert f"{sid}__stop" not in ids_after

    def test_interval_trigger_silently_drops_stop_time(self, scheduler_manager):
        """stop_time is meaningless for interval triggers (every X minutes
        has no time-of-day to stop at). create_schedule clears it
        automatically rather than rejecting the request."""
        schedule = scheduler_manager.create_schedule(
            name="Periodic",
            library_id="1",
            library_name="Movies",
            interval_minutes=30,
            stop_time="06:00",
        )
        assert schedule.get("stop_time", "") == ""
        sid = schedule["id"]
        assert f"{sid}__stop" not in {j.id for j in scheduler_manager.scheduler.get_jobs()}

    def test_invalid_stop_time_raises(self, scheduler_manager):
        with pytest.raises(ValueError):
            scheduler_manager.create_schedule(
                name="Bad",
                library_id="1",
                library_name="Movies",
                cron_expression="0 1 * * *",
                stop_time="25:99",
            )

    def test_parse_hhmm_helper_edge_cases(self):
        from media_preview_generator.web.scheduler import _parse_hhmm

        assert _parse_hhmm("") is None
        assert _parse_hhmm(None) is None
        assert _parse_hhmm("06:00") == (6, 0)
        assert _parse_hhmm("23:59") == (23, 59)
        assert _parse_hhmm("0:0") == (0, 0)
        with pytest.raises(ValueError):
            _parse_hhmm("24:00")
        with pytest.raises(ValueError):
            _parse_hhmm("12:60")
        with pytest.raises(ValueError):
            _parse_hhmm("abc")
        with pytest.raises(ValueError):
            _parse_hhmm("12")


class TestQuietHours:
    """D21 — global queue pause/resume schedule."""

    def test_apply_quiet_hours_enabled_registers_only_the_recheck_job(self, scheduler_manager):
        scheduler_manager.apply_quiet_hours({"enabled": True, "start": "08:00", "end": "01:00"})
        ids = {j.id for j in scheduler_manager.scheduler.get_jobs() if j.id.startswith("__qh_")}
        assert ids == {"__qh_recheck"}

    @pytest.mark.parametrize("name", ["_quiet_hours_pause", "_quiet_hours_resume"])
    def test_jobs_stored_by_older_releases_still_resolve_and_recompute(self, name):
        from media_preview_generator.web import scheduler

        with patch.object(scheduler, "_quiet_hours_recompute_and_apply") as recompute:
            getattr(scheduler, name)()
        recompute.assert_called_once_with()

    def test_apply_quiet_hours_disabled_removes_both_crons(self, scheduler_manager):
        scheduler_manager.apply_quiet_hours({"enabled": True, "start": "08:00", "end": "01:00"})
        scheduler_manager.apply_quiet_hours({"enabled": False, "start": "08:00", "end": "01:00"})
        ids = {j.id for j in scheduler_manager.scheduler.get_jobs()}
        assert "__qh_recheck" not in ids
        assert "__quiet_hours_pause" not in ids
        assert "__quiet_hours_resume" not in ids

    def test_apply_quiet_hours_malformed_times_do_not_raise(self, scheduler_manager):
        scheduler_manager.apply_quiet_hours({"enabled": True, "start": "25:00", "end": "01:00"})
        ids = {j.id for j in scheduler_manager.scheduler.get_jobs()}
        assert not any(jid.startswith(("__qh_pause_", "__qh_resume_")) for jid in ids)

    def test_execute_scheduled_job_skipped_when_processing_paused(self, scheduler_manager, monkeypatch):
        """D21 — when the global queue is paused (manual or quiet hours),
        a scheduled trigger MUST NOT spawn a new Job. Otherwise an
        every-15-min recently_added schedule would balloon the queue."""
        from media_preview_generator.web import scheduler as sched_mod

        called = []
        scheduler_manager.set_run_job_callback(lambda **kw: called.append(kw))

        schedule = scheduler_manager.create_schedule(
            name="GatedSchedule",
            library_id="1",
            library_name="Movies",
            cron_expression="0 2 * * *",
        )
        # Stub settings_manager.processing_paused = True
        fake_sm = MagicMock()
        fake_sm.processing_paused = True
        monkeypatch.setattr(
            "media_preview_generator.web.settings_manager.get_settings_manager",
            lambda: fake_sm,
        )
        sched_mod.execute_scheduled_job(
            schedule["id"],
            ["1"],
            "Movies",
            {},
            None,
            None,
        )
        assert called == [], "callback fired despite processing_paused=True"

    def test_run_now_bypasses_the_pause_gate(self, scheduler_manager, monkeypatch):
        called = []
        scheduler_manager.set_run_job_callback(lambda **kw: called.append(kw))
        schedule = scheduler_manager.create_schedule(
            name="Manual", library_id="1", library_name="Movies", cron_expression="0 2 * * *"
        )
        fake_sm = MagicMock()
        fake_sm.processing_paused = True
        monkeypatch.setattr(
            "media_preview_generator.web.settings_manager.get_settings_manager",
            lambda: fake_sm,
        )

        assert scheduler_manager.run_now(schedule["id"]) is True
        assert called, "Run now did nothing while processing was paused"


class TestQuietHoursMultiWindow:
    """D26 — multi-window Quiet Hours with per-window day-of-week filter."""

    def test_normalise_legacy_single_window_form(self):
        from media_preview_generator.web.scheduler import normalise_quiet_hours

        out = normalise_quiet_hours({"enabled": True, "start": "08:00", "end": "01:00"})
        assert out["enabled"] is True
        assert len(out["windows"]) == 2
        assert out["windows"][0]["start"] == "00:00"
        assert out["windows"][0]["end"] == "01:00"
        assert out["windows"][1]["start"] == "08:00"
        assert out["windows"][1]["end"] == "00:00"
        # Legacy form has no day filter — should default to all 7 days.
        assert set(out["windows"][0]["days"]) == {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}

    def test_normalise_multi_window_passes_through(self):
        from media_preview_generator.web.scheduler import normalise_quiet_hours

        raw = {
            "enabled": True,
            "windows": [
                {"start": "08:00", "end": "17:00", "days": ["mon", "tue", "wed", "thu", "fri"]},
                {"start": "22:00", "end": "06:00", "days": ["sat", "sun"]},
            ],
        }
        out = normalise_quiet_hours(raw)
        assert len(out["windows"]) == 3
        assert out["windows"][0]["days"] == ["mon", "tue", "wed", "thu", "fri"]
        assert out["windows"][1]["days"] == ["sat", "sun"]

    def test_normalise_strips_unknown_day_names(self):
        from media_preview_generator.web.scheduler import normalise_quiet_hours

        raw = {"enabled": True, "windows": [{"start": "08:00", "end": "17:00", "days": ["mon", "BOGUS", "fri"]}]}
        out = normalise_quiet_hours(raw)
        assert out["windows"][0]["days"] == ["mon", "fri"]

    def test_normalise_empty_days_list_falls_back_to_all_seven(self):
        from media_preview_generator.web.scheduler import normalise_quiet_hours

        out = normalise_quiet_hours({"enabled": True, "windows": [{"start": "08:00", "end": "17:00", "days": []}]})
        assert len(out["windows"][0]["days"]) == 7

    def test_normalise_handles_none_input(self):
        from media_preview_generator.web.scheduler import normalise_quiet_hours

        out = normalise_quiet_hours(None)
        assert out == {"enabled": False, "windows": [], "day_basis": "start"}

    def test_is_now_in_any_quiet_window_respects_day_of_week(self):
        from datetime import datetime as _dt

        from media_preview_generator.web.scheduler import is_now_in_any_quiet_window

        qh = {"enabled": True, "windows": [{"start": "08:00", "end": "17:00", "days": ["mon"]}]}
        # 2026-05-04 is a Monday, 2026-05-05 a Tuesday.
        mon_inside = _dt(2026, 5, 4, 12, 0)
        tue_inside_clock = _dt(2026, 5, 5, 12, 0)
        assert is_now_in_any_quiet_window(qh, mon_inside) is True
        assert is_now_in_any_quiet_window(qh, tue_inside_clock) is False

    def test_is_now_in_any_quiet_window_two_windows_either_active(self):
        from datetime import datetime as _dt

        from media_preview_generator.web.scheduler import is_now_in_any_quiet_window

        qh = {
            "enabled": True,
            "windows": [
                # Weekday daytime
                {"start": "08:00", "end": "17:00", "days": ["mon", "tue", "wed", "thu", "fri"]},
                # Weekend overnight (cross-midnight)
                {"start": "22:00", "end": "06:00", "days": ["sat", "sun"]},
            ],
        }
        # 2026-05-04 Mon 12:00 → first window active
        assert is_now_in_any_quiet_window(qh, _dt(2026, 5, 4, 12, 0)) is True
        # 2026-05-09 Sat 02:00 → second window active (cross-midnight)
        assert is_now_in_any_quiet_window(qh, _dt(2026, 5, 9, 2, 0)) is True
        # 2026-05-04 Mon 22:00 → no active window (weekday-night not covered)
        assert is_now_in_any_quiet_window(qh, _dt(2026, 5, 4, 22, 0)) is False

    def test_is_now_in_any_quiet_window_disabled_returns_false(self):
        from datetime import datetime as _dt

        from media_preview_generator.web.scheduler import is_now_in_any_quiet_window

        qh = {
            "enabled": False,
            "windows": [{"start": "08:00", "end": "17:00", "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}],
        }
        assert is_now_in_any_quiet_window(qh, _dt(2026, 5, 4, 12, 0)) is False

    def test_apply_quiet_hours_reapply_leaves_a_single_recheck_job(self, scheduler_manager):
        for end in ("10:00", "11:00"):
            scheduler_manager.apply_quiet_hours(
                {"enabled": True, "windows": [{"start": "08:00", "end": end, "days": ["mon"]}]}
            )
        qh_ids = [j.id for j in scheduler_manager.scheduler.get_jobs() if j.id.startswith("__qh_")]
        assert qh_ids == ["__qh_recheck"]


class TestLibraryHealthNightly:
    """The internal 03:00 library-health job."""

    def test_registered_at_three_am_and_hidden_from_user_schedules(self, scheduler_manager):
        from apscheduler.triggers.cron import CronTrigger

        job = scheduler_manager.scheduler.get_job("__library_health_nightly")
        assert job is not None
        assert isinstance(job.trigger, CronTrigger)
        fields = {f.name: str(f) for f in job.trigger.fields}
        assert (fields["hour"], fields["minute"]) == ("3", "0")
        assert all(s["id"] != "__library_health_nightly" for s in scheduler_manager.get_all_schedules())

    def test_runs_one_at_a_time_and_coalesces_missed_runs(self, scheduler_manager):
        job = scheduler_manager.scheduler.get_job("__library_health_nightly")
        assert job.coalesce is True
        assert job.max_instances == 1
        assert "__library_health_nightly" not in [s["id"] for s in scheduler_manager.get_all_schedules()]

    def test_registering_twice_keeps_one_job(self, scheduler_manager):
        scheduler_manager._register_library_health_nightly()
        ids = [j.id for j in scheduler_manager.scheduler.get_jobs() if j.id == "__library_health_nightly"]
        assert ids == ["__library_health_nightly"]

    def test_start_survives_a_failed_nightly_import(self, tmp_path, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def failing_import(name, *args, **kwargs):
            if name.endswith("library_health.runner"):
                raise ImportError("boom")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", failing_import)
        manager = ScheduleManager(config_dir=str(tmp_path / "config"), run_job_callback=None)
        try:
            manager.start()
            assert manager.scheduler.running
            assert manager.scheduler.get_job("__library_health_nightly") is None
        finally:
            manager.stop()

    def test_job_next_run_reports_the_fire_time(self, scheduler_manager):
        from media_preview_generator.web.scheduler import job_next_run

        assert job_next_run("__library_health_nightly") is not None
        assert job_next_run("missing") is None


class TestExecuteScheduleStop:
    """D20 — the stop handler pauses the right jobs and ignores others."""

    def test_pauses_only_jobs_from_this_schedule(self, scheduler_manager, monkeypatch):
        from media_preview_generator.web import scheduler as sched_mod
        from media_preview_generator.web.jobs import JobStatus

        # Build a fake JobManager whose get_all_jobs returns three jobs:
        # one running from this schedule, one running from a sibling
        # schedule, one already paused. Only the first should be paused.
        sid = "sched-A"
        other_sid = "sched-B"

        target_job = MagicMock(
            id="job-1",
            parent_schedule_id=sid,
            status=JobStatus.RUNNING,
            paused=False,
        )
        sibling_job = MagicMock(
            id="job-2",
            parent_schedule_id=other_sid,
            status=JobStatus.RUNNING,
            paused=False,
        )
        already_paused = MagicMock(
            id="job-3",
            parent_schedule_id=sid,
            status=JobStatus.RUNNING,
            paused=True,
        )

        fake_jm = MagicMock()
        fake_jm.get_all_jobs.return_value = [target_job, sibling_job, already_paused]
        fake_jm.request_pause.return_value = True
        monkeypatch.setattr(
            "media_preview_generator.web.jobs.get_job_manager",
            lambda: fake_jm,
        )
        scheduler_manager._schedules[sid] = {"id": sid, "name": "Overnight", "stop_time": "06:00"}
        monkeypatch.setattr(sched_mod, "_schedule_manager", scheduler_manager)

        sched_mod.execute_schedule_stop(sid)

        assert fake_jm.request_pause.call_args_list == [
            call("job-1", by_schedule=True),
            call("job-3", by_schedule=True),
        ]
        # Adding a schedule hold must also cover an independently manual-paused job.
        assert all(call.args[0] != "job-2" for call in fake_jm.request_pause.call_args_list)


# ========================================================================
# Enable / Disable
# ========================================================================


class TestScheduleEnableDisable:
    """Tests for toggling schedule enabled state."""

    def test_enable_schedule(self, scheduler_manager):
        """Test enabling a disabled schedule."""
        schedule = scheduler_manager.create_schedule(
            name="Test",
            library_id="123",
            library_name="Movies",
            cron_expression="0 2 * * *",
            enabled=False,
        )

        updated = scheduler_manager.enable_schedule(schedule["id"])

        assert updated is not None
        assert updated["enabled"] is True

    def test_disable_schedule(self, scheduler_manager):
        """Test disabling an enabled schedule."""
        schedule = scheduler_manager.create_schedule(
            name="Test",
            library_id="123",
            library_name="Movies",
            cron_expression="0 2 * * *",
            enabled=True,
        )

        updated = scheduler_manager.disable_schedule(schedule["id"])

        assert updated is not None
        assert updated["enabled"] is False

    def test_enable_nonexistent(self, scheduler_manager):
        """Test that enabling a nonexistent schedule returns None."""
        assert scheduler_manager.enable_schedule("nonexistent") is None

    def test_disable_nonexistent(self, scheduler_manager):
        """Test that disabling a nonexistent schedule returns None."""
        assert scheduler_manager.disable_schedule("nonexistent") is None


# ========================================================================
# Run Now
# ========================================================================


class TestScheduleRunNow:
    """Tests for immediate schedule execution."""

    def test_run_now_with_callback(self, scheduler_manager):
        """Test run_now invokes the run_job_callback."""
        mock_callback = MagicMock()
        scheduler_manager.set_run_job_callback(mock_callback)

        schedule = scheduler_manager.create_schedule(
            name="Test",
            library_id="123",
            library_name="Movies",
            cron_expression="0 2 * * *",
        )

        result = scheduler_manager.run_now(schedule["id"])

        assert result is True
        # D20 — every scheduled callback now carries parent_schedule_id so
        # the spawned Job can later be paused by the schedule's stop_time
        # cron and resumed by the next start tick.
        mock_callback.assert_called_once_with(
            library_id="123",
            library_name="Movies",
            config={
                "job_type": "full_library",
                "schedule_scope": '{"config":{"job_type":"full_library"},"libraries":["123"],"priority":null,"server_id":null}',
            },
            parent_schedule_id=schedule["id"],
        )

    def test_run_now_nonexistent(self, scheduler_manager):
        """Test that run_now on a nonexistent schedule returns False."""
        assert scheduler_manager.run_now("nonexistent") is False

    def test_run_now_updates_last_run(self, scheduler_manager):
        """Test that run_now sets the last_run timestamp."""
        mock_callback = MagicMock()
        scheduler_manager.set_run_job_callback(mock_callback)

        schedule = scheduler_manager.create_schedule(
            name="Test",
            library_id="123",
            library_name="Movies",
            cron_expression="0 2 * * *",
        )
        assert schedule["last_run"] is None

        scheduler_manager.run_now(schedule["id"])

        updated = scheduler_manager.get_schedule(schedule["id"])
        assert updated["last_run"] is not None


def _freeze_apscheduler_clock(monkeypatch: pytest.MonkeyPatch, now: datetime, tz: tzinfo) -> Callable[[datetime], None]:
    """Pin APScheduler's wall clock and local timezone so cron math depends on neither today's date
    nor the machine's zone.

    ``now`` must be aware. Patches the ``datetime`` name in the scheduler loop (due-job selection,
    next fire time) and the executor (misfire check), and ``tzlocal.get_localzone`` everywhere
    APScheduler imports it: the scheduler (its ``timezone``) and the cron/interval triggers, which
    resolve the local zone on their own when ``CronTrigger.from_crontab`` is given none.

    Returns a setter that moves the frozen clock, so a test can arm a run in the future and then
    jump to the instant it is due instead of racing the scheduler thread.
    """
    clock = [now]

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return clock[0].astimezone(tz)

    def set_now(new_now: datetime) -> None:
        clock[0] = new_now

    for module in ("apscheduler.schedulers.base", "apscheduler.triggers.cron", "apscheduler.triggers.interval"):
        monkeypatch.setattr(f"{module}.get_localzone", lambda: tz)
    monkeypatch.setattr("apscheduler.schedulers.base.datetime", FrozenDatetime)
    monkeypatch.setattr("apscheduler.executors.base.datetime", FrozenDatetime)
    return set_now


class TestNextRunAfterAFire:
    """Live case: TV Daily ("0 2 * * *") fired at 02:00; APScheduler moved on to
    the next day's 02:00, but ``schedules.json`` kept ``next_run`` at the 02:00
    that had just fired — ``_update_last_run`` saved the copy last refreshed by
    a page read, which was still the upcoming (now fired) run."""

    @pytest.mark.parametrize(
        ("frozen_now", "dst_case"),
        [
            pytest.param("2026-06-10T12:00:00", "ordinary day", id="ordinary-day"),
            # Sydney starts DST on 2026-10-04: 02:00 doesn't exist, the clock jumps 02:00 -> 03:00.
            pytest.param("2026-10-03T12:00:00", "DST gap", id="dst-gap"),
            # Sydney ends DST on 2027-04-04: 02:00-03:00 happens twice.
            pytest.param("2027-04-03T12:00:00", "DST overlap", id="dst-overlap"),
        ],
    )
    def test_stored_next_run_matches_the_scheduler_when_a_cron_schedule_fires(
        self, start_scheduler_manager, monkeypatch, frozen_now: str, dst_case: str
    ):
        import json
        import time
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        def instant(iso: str) -> datetime:
            return datetime.fromisoformat(iso).astimezone(UTC)

        tz = ZoneInfo("Australia/Sydney")
        now = datetime.fromisoformat(frozen_now).replace(tzinfo=tz)
        fire_at = now - timedelta(seconds=1)
        # Start an hour early so the armed run is still in the future while the page "polls";
        # the clock then jumps to ``now`` and the scheduler is woken with the run due.
        set_now = _freeze_apscheduler_clock(monkeypatch, now - timedelta(hours=1), tz)
        monkeypatch.setattr(
            "media_preview_generator.web.settings_manager.get_settings_manager",
            lambda: MagicMock(processing_paused=False),
        )
        scheduler_manager = start_scheduler_manager()
        callback = MagicMock()
        scheduler_manager.set_run_job_callback(callback)
        schedule = scheduler_manager.create_schedule(
            name="TV Daily", library_id="2", library_name="TV Shows", cron_expression="0 2 * * *"
        )
        sid = schedule["id"]
        scheduler_manager.scheduler.modify_job(sid, next_run_time=fire_at)
        # The Schedules page polls, which copies the upcoming run into memory.
        assert [instant(s["next_run"]) for s in scheduler_manager.get_all_schedules()] == [fire_at.astimezone(UTC)]

        set_now(now)
        scheduler_manager.scheduler.wakeup()

        def stored() -> dict:
            with open(scheduler_manager.schedules_file) as fh:
                return json.load(fh)["schedules"][sid]

        def stored_next_run_utc() -> datetime:
            return instant(stored()["next_run"])

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            live = scheduler_manager.scheduler.get_job(sid).next_run_time
            if callback.called and stored()["last_run"] and stored_next_run_utc() == live.astimezone(UTC):
                break
            time.sleep(0.05)

        callback.assert_called_once()
        assert callback.call_args.kwargs["parent_schedule_id"] == sid
        live = scheduler_manager.scheduler.get_job(sid).next_run_time
        assert live > fire_at
        local = live.astimezone(tz)
        assert (local.date(), local.hour, local.minute) == ((now + timedelta(days=1)).date(), 2, 0), dst_case
        assert stored()["last_run"] is not None
        # A datetime inside a DST gap or overlap compares unequal to any other zone's datetime (PEP 495),
        # so compare the instants in UTC rather than the ISO string's fixed offset against the scheduler's zone.
        assert stored_next_run_utc() == live.astimezone(UTC), (
            f"{dst_case}: schedules.json next_run {stored()['next_run']} is the run that just fired; "
            f"the scheduler holds {live}"
        )
        assert instant(scheduler_manager.get_schedule(sid)["next_run"]) == live.astimezone(UTC)

    def _dispatch(self, manager, code: int, job_id: str) -> None:
        from datetime import datetime

        from apscheduler.events import JobSubmissionEvent

        manager.scheduler._dispatch_event(JobSubmissionEvent(code, job_id, "default", [datetime.now(UTC)]))

    def test_stored_next_run_moves_on_when_a_fire_is_skipped_because_the_last_run_is_still_going(
        self, scheduler_manager
    ):
        """With ``max_instances=1`` APScheduler still advances the job past a
        skipped fire, and reports it with EVENT_JOB_MAX_INSTANCES instead."""
        import json
        from datetime import datetime, timedelta

        from apscheduler.events import EVENT_JOB_MAX_INSTANCES

        schedule = scheduler_manager.create_schedule(name="Every 15", library_id="2", interval_minutes=15)
        sid = schedule["id"]
        moved_on = datetime.now(UTC) + timedelta(minutes=30)
        scheduler_manager.scheduler.modify_job(sid, next_run_time=moved_on)

        self._dispatch(scheduler_manager, EVENT_JOB_MAX_INSTANCES, sid)

        with open(scheduler_manager.schedules_file) as fh:
            assert datetime.fromisoformat(json.load(fh)["schedules"][sid]["next_run"]) == moved_on

    def test_does_not_save_when_the_job_is_not_a_schedule(self, scheduler_manager):
        from apscheduler.events import EVENT_JOB_SUBMITTED

        with patch.object(scheduler_manager, "_save_schedules") as save:
            self._dispatch(scheduler_manager, EVENT_JOB_SUBMITTED, "quiet_hours_pause_0")

        save.assert_not_called()

    def test_does_not_save_when_next_run_is_unchanged(self, scheduler_manager):
        """Each save rotates a backup; a no-op save would push real restore points out."""
        from apscheduler.events import EVENT_JOB_SUBMITTED

        schedule = scheduler_manager.create_schedule(name="TV", library_id="2", cron_expression="0 2 * * *")

        with patch.object(scheduler_manager, "_save_schedules") as save:
            self._dispatch(scheduler_manager, EVENT_JOB_SUBMITTED, schedule["id"])

        save.assert_not_called()


# ========================================================================
# Persistence
# ========================================================================


class TestSchedulePersistence:
    """Tests for schedule file persistence."""

    def test_schedules_survive_restart(self, tmp_path, monkeypatch):
        """Test that schedules persist after manager restart."""
        config_dir = str(tmp_path / "config")
        os.makedirs(config_dir, exist_ok=True)

        manager1 = ScheduleManager(config_dir=config_dir)
        monkeypatch.setattr("media_preview_generator.web.scheduler._schedule_manager", manager1)
        manager1.start()
        schedule = manager1.create_schedule(
            name="Persistent",
            library_id="123",
            library_name="Movies",
            cron_expression="0 2 * * *",
        )
        sid = schedule["id"]
        manager1.stop()

        manager2 = ScheduleManager(config_dir=config_dir)
        monkeypatch.setattr("media_preview_generator.web.scheduler._schedule_manager", manager2)
        manager2.start()

        retrieved = manager2.get_schedule(sid)
        assert retrieved is not None
        assert retrieved["name"] == "Persistent"
        assert retrieved["library_id"] == "123"

        manager2.stop()

    def test_handles_missing_file(self, tmp_path):
        """Test graceful handling of missing schedules.json."""
        config_dir = str(tmp_path / "config")
        os.makedirs(config_dir, exist_ok=True)

        manager = ScheduleManager(config_dir=config_dir)
        manager.start()

        assert manager.get_all_schedules() == []

        manager.stop()

    def test_handles_corrupt_file(self, tmp_path):
        """Test graceful handling of corrupt schedules.json."""
        config_dir = str(tmp_path / "config")
        os.makedirs(config_dir, exist_ok=True)

        with open(os.path.join(config_dir, "schedules.json"), "w") as f:
            f.write("{invalid json")

        manager = ScheduleManager(config_dir=config_dir)
        manager.start()

        assert manager.get_all_schedules() == []

        manager.stop()

    def test_schedules_re_register_with_apscheduler_after_jobstore_wipe(self, tmp_path, monkeypatch):
        """D30 — schedules.json is the source of truth; the SQLAlchemy
        jobstore is just a derived cache. If scheduler.db is wiped (or
        was never persisted because of an earlier bug), the schedules
        should ALL re-register with APScheduler at next load. Without
        this, the canary observed 3 schedules in JSON but 0 jobs in
        apscheduler_jobs and crons silently never fired."""
        config_dir = str(tmp_path / "config")
        os.makedirs(config_dir, exist_ok=True)

        manager1 = ScheduleManager(config_dir=config_dir)
        monkeypatch.setattr("media_preview_generator.web.scheduler._schedule_manager", manager1)
        manager1.start()
        s1 = manager1.create_schedule(
            name="TV Daily",
            library_id="2",
            library_name="TV Shows",
            cron_expression="0 14 * * *",
        )
        s2 = manager1.create_schedule(
            name="Movies Daily",
            library_id="1",
            library_name="Movies",
            cron_expression="0 1 * * *",
        )
        s3 = manager1.create_schedule(
            name="Sports",
            library_id="12",
            library_name="Sports",
            cron_expression="0 2 * * *",
            enabled=False,  # disabled — must NOT register
        )
        manager1.stop()

        # Simulate jobstore wipe — delete scheduler.db so the next manager
        # boots with an empty jobstore. schedules.json is preserved.
        sched_db = os.path.join(config_dir, "scheduler.db")
        if os.path.exists(sched_db):
            os.remove(sched_db)

        manager2 = ScheduleManager(config_dir=config_dir)
        monkeypatch.setattr("media_preview_generator.web.scheduler._schedule_manager", manager2)
        manager2.start()

        # Both enabled schedules must be re-registered with APScheduler.
        registered_ids = {j.id for j in manager2.scheduler.get_jobs()}
        assert s1["id"] in registered_ids, f"Enabled schedule s1 missing after jobstore wipe; got {registered_ids}"
        assert s2["id"] in registered_ids, f"Enabled schedule s2 missing after jobstore wipe; got {registered_ids}"
        # Disabled one must NOT be registered.
        assert s3["id"] not in registered_ids, (
            f"Disabled schedule s3 should not have been re-registered; got {registered_ids}"
        )
        # And each registered cron should have a future next_run_time.
        for j in manager2.scheduler.get_jobs():
            if j.id in (s1["id"], s2["id"]):
                assert j.next_run_time is not None, f"Schedule {j.id} has no next_run_time"

        # The persisted next_run in schedules.json must also be refreshed
        # so the UI shows future fire times instead of the stale snapshot
        # (the canary observed "Next: 3 days ago" because next_run wasn't
        # re-computed on load, only when the cron actually fired).
        from datetime import datetime as _dt

        s1_after = manager2.get_schedule(s1["id"])
        assert s1_after["next_run"] is not None, "next_run not refreshed during re-registration"
        nr = _dt.fromisoformat(s1_after["next_run"])
        if nr.tzinfo is None:
            nr = nr.replace(tzinfo=UTC)
        assert nr > _dt.now(UTC), f"next_run {nr.isoformat()} is in the past — UI would show stale"

        manager2.stop()

    def test_schedules_re_register_preserves_stop_time_cron(self, tmp_path, monkeypatch):
        """D30 — re-registration must also restore the per-schedule D20
        stop-cron, otherwise schedules with a stop_time would lose their
        nightly pause behaviour after a restart."""
        config_dir = str(tmp_path / "config")
        os.makedirs(config_dir, exist_ok=True)

        manager1 = ScheduleManager(config_dir=config_dir)
        monkeypatch.setattr("media_preview_generator.web.scheduler._schedule_manager", manager1)
        manager1.start()
        s = manager1.create_schedule(
            name="WithStop",
            library_id="2",
            library_name="TV Shows",
            cron_expression="0 1 * * *",
            stop_time="06:00",
        )
        manager1.stop()

        sched_db = os.path.join(config_dir, "scheduler.db")
        if os.path.exists(sched_db):
            os.remove(sched_db)

        manager2 = ScheduleManager(config_dir=config_dir)
        monkeypatch.setattr("media_preview_generator.web.scheduler._schedule_manager", manager2)
        manager2.start()

        registered_ids = {j.id for j in manager2.scheduler.get_jobs()}
        assert s["id"] in registered_ids
        assert f"{s['id']}__stop" in registered_ids, f"D20 stop-cron missing after jobstore wipe; got {registered_ids}"
        manager2.stop()


# ========================================================================
# get_schedule_manager singleton
# ========================================================================


class TestGetScheduleManager:
    """Tests for the get_schedule_manager singleton factory."""

    def test_returns_singleton(self, tmp_path, monkeypatch):
        """Test that get_schedule_manager returns the same instance."""
        monkeypatch.setattr("media_preview_generator.web.scheduler._schedule_manager", None)

        config_dir = str(tmp_path / "config")
        os.makedirs(config_dir, exist_ok=True)

        m1 = get_schedule_manager(config_dir=config_dir)
        m2 = get_schedule_manager(config_dir=config_dir)

        assert m1 is m2
        m1.stop()

    def test_sets_callback_on_existing(self, tmp_path, monkeypatch):
        """Test that a callback can be set on an existing singleton."""
        monkeypatch.setattr("media_preview_generator.web.scheduler._schedule_manager", None)

        config_dir = str(tmp_path / "config")
        os.makedirs(config_dir, exist_ok=True)

        mock_cb = MagicMock()
        m1 = get_schedule_manager(config_dir=config_dir)
        m2 = get_schedule_manager(run_job_callback=mock_cb)

        assert m1 is m2
        assert m2.run_job_callback is mock_cb


# ========================================================================
# Scheduled job dispatch (full_library vs recently_added)
# ========================================================================


class TestExecuteScheduledJobDispatch:
    """Verify that execute_scheduled_job branches correctly on config.job_type."""

    def test_dispatches_full_library_by_default(self, scheduler_manager):
        """A schedule with no job_type in config goes through run_job_callback."""
        from media_preview_generator.web.scheduler import execute_scheduled_job

        mock_callback = MagicMock()
        scheduler_manager.set_run_job_callback(mock_callback)

        schedule = scheduler_manager.create_schedule(
            name="Test",
            library_id="123",
            library_name="Movies",
            cron_expression="0 2 * * *",
        )

        execute_scheduled_job(
            schedule["id"],
            library_id="123",
            library_name="Movies",
            config={},
        )

        mock_callback.assert_called_once()
        kwargs = mock_callback.call_args.kwargs
        assert kwargs["library_id"] == "123"
        assert kwargs["library_name"] == "Movies"
        # Audit fix — D20 contract: ``parent_schedule_id`` MUST be forwarded
        # so the schedule's stop_time can cancel its dispatched job. Without
        # this, the job runs unkillable past stop_time.
        assert kwargs.get("parent_schedule_id") == schedule["id"], (
            f"parent_schedule_id missing — schedule's stop_time can't cancel this job. kwargs={kwargs!r}"
        )
        # Issue #285: ABSENCE is the contract. An unpinned full-library scan
        # must let run_scheduled_job fall back to PRIORITY_NORMAL — it must
        # NOT inherit the global incoming-priority setting the way a
        # Recently Added sweep does. Forwarding a value here (even None)
        # would eventually put full scans on the reserved High slot and
        # cancel out the reservation the whole feature depends on.
        assert "priority" not in kwargs, (
            f"unpinned full_library schedules must not forward a priority at all; kwargs={kwargs!r}"
        )

    def test_dispatches_full_library_with_an_explicit_pin(self, scheduler_manager):
        """A pinned full-library schedule still forwards its priority."""
        from media_preview_generator.web.scheduler import execute_scheduled_job

        mock_callback = MagicMock()
        scheduler_manager.set_run_job_callback(mock_callback)

        schedule = scheduler_manager.create_schedule(
            name="Pinned scan",
            library_id="123",
            library_name="Movies",
            cron_expression="0 2 * * *",
            priority=3,
        )

        execute_scheduled_job(
            schedule["id"],
            library_id="123",
            library_name="Movies",
            config={},
            priority=3,
        )

        assert mock_callback.call_args.kwargs["priority"] == 3

    def test_dispatches_recently_added_calls_multi_server_scan(self, scheduler_manager, monkeypatch):
        """A schedule with job_type='recently_added' dispatches via the
        gated ``_start_recently_added_job_async`` helper (works for
        any vendor). The scan is not inline:
        the helper creates a Job row, queues at the JobGate, and runs
        the scan only after admission."""
        from media_preview_generator.web import scheduler as sched_mod

        # Patch the gated helper directly — the underlying multi-server
        # scan is still covered by ``test_recently_added_helper_runs_scan_after_gate_admit``
        # below. Patching here keeps THIS test focused on the
        # scheduler-to-helper contract (kwargs forwarding).
        mock_helper = MagicMock(return_value="fake-job-id")
        monkeypatch.setattr(
            "media_preview_generator.web.routes.job_runner._start_recently_added_job_async",
            mock_helper,
        )

        schedule = scheduler_manager.create_schedule(
            name="Scanner",
            library_id="2",
            library_name="TV",
            interval_minutes=15,
            config={"job_type": "recently_added", "lookback_hours": 2},
        )

        sched_mod.execute_scheduled_job(
            schedule["id"],
            library_id="2",
            library_name="TV",
            config={"job_type": "recently_added", "lookback_hours": 2},
        )

        mock_helper.assert_called_once()
        kwargs = mock_helper.call_args.kwargs
        assert kwargs["library_ids"] == ["2"]
        assert kwargs["lookback_hours"] == 2.0
        assert kwargs["schedule_id"] == schedule["id"]
        # ``library_name`` is the user-facing Job row title — should
        # carry the schedule's library label so the operator can match
        # rows to schedules at a glance.
        assert "TV" in kwargs["library_name"]

    def test_dispatches_recently_added_with_no_library_passes_none(self, scheduler_manager, monkeypatch):
        """No library_id = library_ids=None reaches the gated helper."""
        from media_preview_generator.web import scheduler as sched_mod

        mock_helper = MagicMock(return_value="fake-job-id")
        monkeypatch.setattr(
            "media_preview_generator.web.routes.job_runner._start_recently_added_job_async",
            mock_helper,
        )

        schedule = scheduler_manager.create_schedule(
            name="Scanner",
            library_id=None,
            library_name="",
            interval_minutes=15,
            config={"job_type": "recently_added", "lookback_hours": 1},
        )

        sched_mod.execute_scheduled_job(
            schedule["id"],
            library_id=None,
            library_name="",
            config={"job_type": "recently_added", "lookback_hours": 1},
        )

        mock_helper.assert_called_once()
        assert mock_helper.call_args.kwargs["library_ids"] is None
        assert mock_helper.call_args.kwargs["lookback_hours"] == 1.0

    def test_recently_added_dispatch_clamps_invalid_lookback(self, scheduler_manager, monkeypatch):
        """Garbage lookback_hours values are coerced to a safe default."""
        from media_preview_generator.web import scheduler as sched_mod

        mock_helper = MagicMock(return_value="fake-job-id")
        monkeypatch.setattr(
            "media_preview_generator.web.routes.job_runner._start_recently_added_job_async",
            mock_helper,
        )

        schedule = scheduler_manager.create_schedule(
            name="Scanner",
            interval_minutes=15,
            config={"job_type": "recently_added", "lookback_hours": "nope"},
        )

        sched_mod.execute_scheduled_job(
            schedule["id"],
            library_id=None,
            library_name="",
            config={"job_type": "recently_added", "lookback_hours": "nope"},
        )

        # Falls back to 1.0 hour default
        mock_helper.assert_called_once()
        assert mock_helper.call_args.kwargs["lookback_hours"] == 1.0

    def test_recently_added_dispatch_updates_last_run(self, scheduler_manager, monkeypatch):
        """After dispatching a recently_added scan the schedule's last_run updates."""
        from media_preview_generator.web import scheduler as sched_mod

        monkeypatch.setattr(
            "media_preview_generator.web.routes.job_runner._start_recently_added_job_async",
            MagicMock(return_value="fake-job-id"),
        )

        schedule = scheduler_manager.create_schedule(
            name="Scanner",
            interval_minutes=15,
            config={"job_type": "recently_added", "lookback_hours": 1},
        )
        assert schedule["last_run"] is None

        sched_mod.execute_scheduled_job(
            schedule["id"],
            library_id=None,
            library_name="",
            config={"job_type": "recently_added", "lookback_hours": 1},
        )

        updated = scheduler_manager.get_schedule(schedule["id"])
        assert updated["last_run"] is not None


class TestExecuteScheduledIntroCreditsJob:
    """``config.job_type == "intro_credits"`` schedules create Intro & Credits jobs (LOW unless pinned)."""

    @pytest.fixture
    def env(self, scheduler_manager, monkeypatch):
        jobs = []
        fake_jm = MagicMock()
        fake_jm.get_all_jobs.side_effect = lambda: list(jobs)
        fake_jm.request_resume.return_value = True
        monkeypatch.setattr("media_preview_generator.web.jobs.get_job_manager", lambda: fake_jm)
        settings = {"media_servers": []}
        fake_sm = MagicMock(processing_paused=False)
        fake_sm.get.side_effect = lambda key, default=None: settings.get(key, default)
        monkeypatch.setattr("media_preview_generator.web.settings_manager.get_settings_manager", lambda: fake_sm)
        callback = MagicMock()
        scheduler_manager.set_run_job_callback(callback)
        create = MagicMock(return_value=MagicMock(id="ic-1"))
        monkeypatch.setattr(triggers, "create_intro_credits_job", create)
        return {
            "jobs": jobs,
            "jm": fake_jm,
            "settings": settings,
            "sm": fake_sm,
            "callback": callback,
            "create": create,
        }

    def _schedule(self, scheduler_manager, **kwargs):
        return scheduler_manager.create_schedule(
            name="Markers", cron_expression="0 3 * * 0", config={"job_type": "intro_credits"}, **kwargs
        )

    PLEX_1 = {
        "id": "plex-1",
        "type": "plex",
        "enabled": True,
        "markers": {"enabled": True, "plex": {"db_write_confirmed_at": "2026-09-13T00:00:00+00:00"}},
        "libraries": [
            {"id": "1", "name": "TV Shows", "remote_paths": ["/m/tv"]},
            {"id": "2", "name": "Anime", "remote_paths": ["/m/anime"], "enabled": False},
            {"id": "3", "name": "Sports", "remote_paths": ["/m/sports"]},
        ],
    }

    @pytest.mark.parametrize(("priority", "expected"), [(None, 3), (1, 1), (2, 2), ("high", 1)])
    def test_dispatches_intro_credits_job(self, scheduler_manager, env, priority, expected):
        from media_preview_generator.web.scheduler import execute_scheduled_job

        env["settings"]["media_servers"] = [self.PLEX_1]
        schedule = self._schedule(
            scheduler_manager, library_ids=["1", "2"], library_name="TV Shows", server_id="plex-1"
        )
        execute_scheduled_job(schedule["id"], ["1", "2"], "TV Shows", {"job_type": "intro_credits"}, priority, "plex-1")

        env["callback"].assert_not_called()
        env["create"].assert_called_once_with(
            library_name="Intro & Credits: TV Shows",
            priority=expected,
            source="schedule",
            libraries=[{"server_id": "plex-1", "library_id": "1"}, {"server_id": "plex-1", "library_id": "2"}],
            parent_schedule_id=schedule["id"],
            scan_filters={},
            # Pinned like a scheduled preview job for one server: its markers go to that server only.
            server_id="plex-1",
        )
        assert scheduler_manager.get_schedule(schedule["id"])["last_run"] is not None

    def test_user_cron_runs_find_markers_without_forcing_cached_answers(self, scheduler_manager, env):
        schedule = scheduler_manager.create_schedule(
            name="My weekly marker check",
            cron_expression="0 3 * * sun",
            library_ids=["1"],
            library_name="TV Shows",
            server_id="plex-1",
            config={"job_type": "intro_credits"},
        )
        env["settings"]["media_servers"] = [self.PLEX_1]
        tick = scheduler_manager.scheduler.get_job(schedule["id"])
        assert tick is not None
        assert str(tick.trigger) == "cron[month='*', day='*', day_of_week='sun', hour='3', minute='0']"
        env["create"].assert_not_called()

        tick.func(*tick.args, **tick.kwargs)

        env["create"].assert_called_once_with(
            library_name="Intro & Credits: TV Shows",
            priority=3,
            source="schedule",
            libraries=[{"server_id": "plex-1", "library_id": "1"}],
            parent_schedule_id=schedule["id"],
            scan_filters={},
            server_id="plex-1",
        )
        assert scheduler_manager.get_schedule(schedule["id"])["last_run"] is not None

    def test_intro_credits_schedule_without_server_checks_every_enabled_server(self, scheduler_manager, env):
        from media_preview_generator.web.scheduler import execute_scheduled_job

        schedule = self._schedule(scheduler_manager)
        execute_scheduled_job(schedule["id"], None, "", {"job_type": "intro_credits"}, None, None)

        kwargs = env["create"].call_args.kwargs
        assert kwargs["libraries"] == []
        assert kwargs["library_name"] == "Intro & Credits: all libraries"
        assert kwargs["priority"] == 3 and kwargs["parent_schedule_id"] == schedule["id"]
        assert kwargs.get("server_id") is None  # every server with Intro & Credits on

    def test_server_pinned_schedule_without_libraries_takes_that_servers_marker_libraries(self, scheduler_manager, env):
        from media_preview_generator.web.scheduler import execute_scheduled_job

        env["settings"]["media_servers"] = [
            {
                "id": "jf-1",
                "type": "jellyfin",
                "enabled": True,
                "markers": {"enabled": True},
                "libraries": [
                    {"id": "tv", "name": "TV Shows", "remote_paths": ["/m/tv"], "enabled": False},
                    {"id": "sp", "name": "Sports", "remote_paths": ["/m/sp"], "enabled": True},
                ],
            },
            {"id": "jf-2", "type": "jellyfin", "enabled": True, "markers": {"enabled": True}, "libraries": []},
        ]
        schedule = self._schedule(scheduler_manager, server_id="jf-1")
        execute_scheduled_job(schedule["id"], [], "Jellyfin", {"job_type": "intro_credits"}, None, "jf-1")

        assert env["create"].call_args.kwargs["libraries"] == [{"server_id": "jf-1", "library_id": "tv"}]
        assert env["create"].call_args.kwargs["server_id"] == "jf-1"

    @pytest.mark.parametrize(
        "servers",
        [
            [],
            [{"id": "jf-1", "type": "jellyfin", "enabled": True, "markers": {"enabled": False}, "libraries": []}],
            [
                {
                    "id": "jf-1",
                    "type": "jellyfin",
                    "enabled": True,
                    "markers": {"enabled": True},
                    "libraries": [{"id": "sp", "name": "Sports", "remote_paths": ["/m/sp"]}],
                }
            ],
        ],
    )
    def test_server_pinned_schedule_with_nothing_to_check_starts_no_job(self, scheduler_manager, env, servers):
        from media_preview_generator.web.scheduler import execute_scheduled_job

        env["settings"]["media_servers"] = servers
        schedule = self._schedule(scheduler_manager, server_id="jf-1")
        execute_scheduled_job(schedule["id"], None, "", {"job_type": "intro_credits"}, None, "jf-1")

        env["create"].assert_not_called()
        env["callback"].assert_not_called()

    def test_libraries_without_a_server_are_pinned_to_the_server_that_owns_them(self, scheduler_manager, env):
        from media_preview_generator.web.scheduler import execute_scheduled_job

        env["settings"]["media_servers"] = [
            self.PLEX_1,
            {"id": "jf-1", "type": "jellyfin", "enabled": True, "libraries": [{"id": "abc"}]},
        ]
        schedule = self._schedule(scheduler_manager, library_ids=["1", "2"])
        execute_scheduled_job(schedule["id"], ["1", "2"], "TV", {"job_type": "intro_credits"}, None, None)

        assert env["create"].call_args.kwargs["libraries"] == [
            {"server_id": "plex-1", "library_id": "1"},
            {"server_id": "plex-1", "library_id": "2"},
        ]

    def test_schedule_libraries_outside_the_markers_selection_are_left_out(self, scheduler_manager, env):
        from media_preview_generator.web import scheduler as sched_mod

        env["settings"]["media_servers"] = [self.PLEX_1]
        schedule = self._schedule(scheduler_manager, library_ids=["1", "3", "9"], server_id="plex-1")
        with patch.object(sched_mod, "logger") as log:
            sched_mod.execute_scheduled_job(
                schedule["id"], ["1", "3", "9"], "TV", {"job_type": "intro_credits"}, None, "plex-1"
            )

        assert env["create"].call_args.kwargs["libraries"] == [{"server_id": "plex-1", "library_id": "1"}]
        warned = [c.args for c in log.warning.call_args_list]
        assert any("isn't on" in args[0] and ["Sports", "9"] in args for args in warned), warned

    @pytest.mark.parametrize("library_ids", [["3"], ["9"]], ids=["sports", "unknown"])
    def test_schedule_with_no_library_markers_go_to_starts_no_job(self, scheduler_manager, env, library_ids):
        from media_preview_generator.web.scheduler import execute_scheduled_job

        env["settings"]["media_servers"] = [self.PLEX_1]
        schedule = self._schedule(scheduler_manager, library_ids=library_ids, server_id="plex-1")
        execute_scheduled_job(schedule["id"], library_ids, "TV", {"job_type": "intro_credits"}, None, "plex-1")

        env["create"].assert_not_called()
        env["callback"].assert_not_called()
        assert scheduler_manager.get_schedule(schedule["id"])["last_run"] is None

    @pytest.mark.parametrize(
        "servers",
        [
            [],  # unknown id
            [
                {"id": "plex-1", "type": "plex", "enabled": True, "libraries": [{"id": "1"}]},
                {"id": "plex-2", "type": "plex", "enabled": True, "libraries": [{"id": "1"}]},
            ],  # ambiguous id across two Plex servers
        ],
    )
    def test_libraries_that_cant_be_pinned_to_one_server_start_no_job(self, scheduler_manager, env, servers):
        from media_preview_generator.web.scheduler import execute_scheduled_job

        env["settings"]["media_servers"] = servers
        schedule = self._schedule(scheduler_manager, library_ids=["1"])
        execute_scheduled_job(schedule["id"], ["1"], "TV", {"job_type": "intro_credits"}, None, None)

        env["create"].assert_not_called()
        env["callback"].assert_not_called()
        assert scheduler_manager.get_schedule(schedule["id"])["last_run"] is None

    @pytest.mark.parametrize("status", ["pending", "running"])
    def test_unfinished_job_from_the_last_run_is_not_started_again(self, scheduler_manager, env, status):
        from media_preview_generator.web.jobs import JobStatus
        from media_preview_generator.web.scheduler import execute_scheduled_job

        schedule = self._schedule(scheduler_manager)
        env["jobs"].append(
            MagicMock(
                id="old",
                parent_schedule_id=schedule["id"],
                kind="intro_credits",
                status=JobStatus(status),
                paused=False,
                config={"libraries": []},
            )
        )
        execute_scheduled_job(schedule["id"], None, "", {"job_type": "intro_credits"}, None, None)

        env["create"].assert_not_called()
        env["callback"].assert_not_called()
        env["jm"].request_resume.assert_not_called()

    @pytest.mark.parametrize(
        ("other", "starts"),
        [
            ({"status": "completed"}, True),
            ({"status": "failed"}, True),
            ({"status": "cancelled"}, True),
            ({"status": "running", "parent_schedule_id": "another-schedule"}, True),
            ({"status": "running", "kind": "previews"}, True),  # the schedule used to scan previews
            ({"status": "running", "config": {"reconcile": True}}, True),  # the schedule used to check servers
            # Its own run is done; only its retry chain (files a server hadn't added yet) is left.
            ({"status": "pending", "config": {"is_retry_chain": True, "last_outcome": "scheduled"}}, True),
            ({"status": "running", "config": {"is_retry_chain": True, "last_outcome": "running"}}, True),
        ],
    )
    def test_other_jobs_do_not_block_a_new_run(self, scheduler_manager, env, other, starts):
        from media_preview_generator.web.jobs import JobStatus
        from media_preview_generator.web.scheduler import execute_scheduled_job

        schedule = self._schedule(scheduler_manager)
        fields = {"id": "old", "parent_schedule_id": schedule["id"], "kind": "intro_credits", "paused": False,
                  "config": {"libraries": []}, **other}  # fmt: skip
        fields["status"] = JobStatus(fields["status"])
        env["jobs"].append(MagicMock(**fields))
        execute_scheduled_job(schedule["id"], None, "", {"job_type": "intro_credits"}, None, None)

        assert env["create"].called is starts
        env["jm"].request_resume.assert_not_called()

    @pytest.mark.parametrize(("job_type", "kind"), [("intro_credits", "intro_credits"), ("full_library", "previews")])
    @pytest.mark.parametrize("by_stop_time", [True, False], ids=["stop-time-pause", "pause-by-hand"])
    def test_paused_job_from_this_schedule_is_resumed_instead_only_when_its_stop_time_paused_it(
        self, scheduler_manager, env, job_type, kind, by_stop_time
    ):
        from media_preview_generator.web.jobs import JobStatus
        from media_preview_generator.web.scheduler import execute_scheduled_job

        schedule = self._schedule(scheduler_manager)
        config = {"libraries": [], **({"paused_by_schedule": True} if by_stop_time else {})}
        env["jobs"].append(
            MagicMock(
                id="old", parent_schedule_id=schedule["id"], kind=kind, status=JobStatus.RUNNING, paused=True,
                config=config,
            )
        )  # fmt: skip
        execute_scheduled_job(schedule["id"], None, "", {"job_type": job_type}, None, None)

        if by_stop_time:
            # Asked to resume only while the stop time's pause is still the job's (checked under the manager's lock).
            env["jm"].request_resume.assert_called_once_with("old", only_paused_by_schedule=True)
            env["create"].assert_not_called()
            env["callback"].assert_not_called()
            return
        env["jm"].request_resume.assert_not_called()
        # An Intro & Credits schedule's own Find markers job, paused by hand, is still unfinished: nothing new.
        env["create"].assert_not_called()
        if job_type != "intro_credits":
            # A preview schedule starts its next scan beside the job paused by hand, as before.
            env["callback"].assert_called_once_with(
                library_id=None,
                library_name="",
                config={
                    "job_type": "full_library",
                    "schedule_scope": '{"config":{"job_type":"full_library"},"libraries":[],"priority":null,"server_id":null}',
                },
                parent_schedule_id=schedule["id"],
            )

    @pytest.mark.parametrize(
        ("job_type", "paused_kind"), [("intro_credits", "previews"), ("full_library", "intro_credits")]
    )
    def test_paused_job_of_the_other_kind_is_not_resumed(self, scheduler_manager, env, job_type, paused_kind):
        from media_preview_generator.web.jobs import JobStatus
        from media_preview_generator.web.scheduler import execute_scheduled_job

        schedule = self._schedule(scheduler_manager)
        env["jobs"].append(
            MagicMock(
                id="old",
                parent_schedule_id=schedule["id"],
                kind=paused_kind,
                status=JobStatus.RUNNING,
                paused=True,
                config={"paused_by_schedule": True},
            )
        )
        execute_scheduled_job(schedule["id"], None, "", {"job_type": job_type}, None, None)

        env["jm"].request_resume.assert_not_called()
        if job_type == "intro_credits":
            env["create"].assert_called_once()
        else:
            env["callback"].assert_called_once()

    def test_processing_paused_skips_the_tick(self, scheduler_manager, env):
        from media_preview_generator.web.scheduler import execute_scheduled_job

        env["sm"].processing_paused = True
        schedule = self._schedule(scheduler_manager)
        execute_scheduled_job(schedule["id"], None, "", {"job_type": "intro_credits"}, None, None)

        env["create"].assert_not_called()

    def test_create_failure_is_logged_and_the_tick_ends_quietly(self, scheduler_manager, env):
        from media_preview_generator.web.scheduler import execute_scheduled_job

        env["create"].side_effect = RuntimeError("jobs.db locked")
        schedule = self._schedule(scheduler_manager)
        execute_scheduled_job(schedule["id"], None, "", {"job_type": "intro_credits"}, None, None)

        env["callback"].assert_not_called()
        assert scheduler_manager.get_schedule(schedule["id"])["last_run"] is None


class TestRetiredCheckServersSchedules:
    """Check servers is automatic now: a saved Check servers schedule is deleted at startup, naming it."""

    LEGACY = {"job_type": "intro_credits", "reconcile": True}

    def _write(self, config_dir, schedules):
        import json

        os.makedirs(config_dir, exist_ok=True)
        with open(os.path.join(config_dir, "schedules.json"), "w") as f:
            json.dump({"schedules": schedules}, f)

    @staticmethod
    def _entry(sched_id, config, name="Check servers"):
        return {
            "id": sched_id,
            "name": name,
            "trigger_type": "interval",
            "trigger_value": "720",
            "library_ids": [],
            "library_name": "",
            "config": config,
            "enabled": True,
            "stop_time": "06:00",
        }

    def test_the_schedule_is_deleted_with_a_warning_and_its_jobstore_entries_go(self, tmp_path):
        from loguru import logger

        config_dir = str(tmp_path / "config")
        # A first run registers the legacy schedule's start and stop jobs in scheduler.db, as an older build did.
        self._write(config_dir, {"old": self._entry("old", self.LEGACY)})
        first = ScheduleManager(config_dir=config_dir)
        first.scheduler.add_job("media_preview_generator.web.scheduler:execute_schedule_stop", "interval",
                                minutes=5, id="old", args=["old"], replace_existing=True)  # fmt: skip
        first.scheduler.add_job("media_preview_generator.web.scheduler:execute_schedule_stop", "interval",
                                minutes=5, id="old__stop", args=["old"], replace_existing=True)  # fmt: skip
        first.start()
        first.stop()
        self._write(config_dir, {"old": self._entry("old", self.LEGACY), "keep": self._entry("keep", {})})

        warnings: list[str] = []
        handler = logger.add(warnings.append, level="WARNING", format="{message}")
        try:
            manager = ScheduleManager(config_dir=config_dir)
        finally:
            logger.remove(handler)
        try:
            manager.start()
            assert [s["id"] for s in manager.get_all_schedules()] == ["keep"]
            ids = {job.id for job in manager.scheduler.get_jobs()}
            assert "old" not in ids and "old__stop" not in ids and "keep" in ids
            assert len([w for w in warnings if "'Check servers'" in w and "old" in w]) == 1
            with open(manager.schedules_file) as f:
                assert list(__import__("json").load(f)["schedules"]) == ["keep"]
        finally:
            manager.stop()

    def test_a_stale_tick_of_a_retired_schedule_does_nothing(self, scheduler_manager):
        from media_preview_generator.web.scheduler import execute_scheduled_job

        callback = MagicMock()
        scheduler_manager.set_run_job_callback(callback)
        with patch.object(triggers, "create_intro_credits_job") as create:
            execute_scheduled_job("old", [], "", dict(self.LEGACY), None, None)

        callback.assert_not_called()
        create.assert_not_called()


class TestNormalizeScheduleConfig:
    @pytest.mark.parametrize(
        ("legacy", "kinds", "files"),
        [
            ({}, ["previews"], "full_library"),
            ({"job_type": "full_library", "sort_by": "newest"}, ["previews"], "full_library"),
            ({"job_type": "intro_credits"}, ["intro_credits"], "full_library"),
            ({"job_type": "recently_added", "lookback_hours": 6}, ["previews", "intro_credits", "loudness"],
             "recently_added"),
            ({"job_type": "something-else"}, ["previews"], "full_library"),
            ({"job_kinds": ["loudness", "previews"]}, ["previews", "loudness"], "full_library"),
            ({"job_kinds": ["intro_credits", "intro_credits"], "job_type": "recently_added"}, ["intro_credits"],
             "recently_added"),
        ],
    )  # fmt: skip
    def test_legacy_and_current_configs(self, legacy, kinds, files):
        from media_preview_generator.web.scheduler import normalize_schedule_config

        result = normalize_schedule_config(legacy)

        assert result["job_kinds"] == kinds and result["job_type"] == files
        assert {k: v for k, v in result.items() if k not in ("job_kinds", "job_type")} == {
            k: v for k, v in legacy.items() if k not in ("job_kinds", "job_type")
        }
        assert normalize_schedule_config(result) == result

    def test_reconcile_is_dropped_and_the_input_is_untouched(self):
        from media_preview_generator.web.scheduler import normalize_schedule_config

        legacy = {"job_type": "intro_credits", "reconcile": True}
        assert "reconcile" not in normalize_schedule_config(legacy)
        assert legacy == {"job_type": "intro_credits", "reconcile": True}

    @pytest.mark.parametrize("bad", [[], "previews", ["previews", "check_servers"], None])
    def test_bad_job_kinds_raise(self, bad):
        from media_preview_generator.web.scheduler import normalize_schedule_config

        with pytest.raises(ValueError, match="job_kinds"):
            normalize_schedule_config({"job_kinds": bad})

    def test_loading_schedules_json_normalises_and_saves_legacy_configs(self, tmp_path):
        import json

        config_dir = str(tmp_path / "config")
        os.makedirs(config_dir)
        entry = {
            "id": "s1", "name": "Markers", "trigger_type": "interval", "trigger_value": "60", "library_ids": [],
            "library_name": "", "config": {"job_type": "intro_credits"}, "enabled": False,
        }  # fmt: skip
        with open(os.path.join(config_dir, "schedules.json"), "w") as f:
            json.dump({"schedules": {"s1": entry}}, f)

        manager = ScheduleManager(config_dir=config_dir)

        assert manager.get_schedule("s1")["config"] == {"job_kinds": ["intro_credits"], "job_type": "full_library"}
        with open(manager.schedules_file) as f:
            assert json.load(f)["schedules"]["s1"]["config"] == {
                "job_kinds": ["intro_credits"],
                "job_type": "full_library",
            }

    def test_create_and_update_normalise_and_reject_bad_kinds(self, scheduler_manager):
        schedule = scheduler_manager.create_schedule(
            name="x", cron_expression="0 3 * * *", config={"job_type": "recently_added"}
        )
        assert schedule["config"]["job_kinds"] == ["previews", "intro_credits", "loudness"]

        updated = scheduler_manager.update_schedule(schedule["id"], config={"job_kinds": ["loudness"]})
        assert updated["config"] == {"job_kinds": ["loudness"], "job_type": "full_library"}

        with pytest.raises(ValueError, match="job_kinds"):
            scheduler_manager.update_schedule(schedule["id"], config={"job_kinds": []})
        with pytest.raises(ValueError, match="job_kinds"):
            scheduler_manager.create_schedule(name="y", cron_expression="0 3 * * *", config={"job_kinds": ["nope"]})


class TestDailyCheckServers:
    """Check servers runs by itself once a day, whenever Intro & Credits is on anywhere."""

    def test_it_is_registered_at_04_30_and_hidden_from_user_schedules(self, scheduler_manager):
        job = scheduler_manager.scheduler.get_job("__markers_check_servers_daily")

        assert job is not None
        assert "hour='4'" in str(job.trigger) and "minute='30'" in str(job.trigger)
        assert job.coalesce and job.max_instances == 1 and job.misfire_grace_time == 3600
        assert scheduler_manager.get_all_schedules() == []

    def test_it_queues_check_servers(self, monkeypatch):
        from media_preview_generator.web.scheduler import run_markers_check_servers_daily

        run = MagicMock()
        monkeypatch.setattr(reconcile, "run_markers_reconcile", run)
        monkeypatch.setattr(
            "media_preview_generator.web.settings_manager.get_settings_manager",
            lambda: MagicMock(processing_paused=False),
        )

        run_markers_check_servers_daily()

        run.assert_called_once_with()

    def test_it_queues_check_servers_even_while_processing_is_paused(self, monkeypatch):
        """A queued job waits out a pause at admission; skipping would lose the check for good if 04:30 is quiet."""
        from media_preview_generator.web.scheduler import run_markers_check_servers_daily

        run = MagicMock()
        monkeypatch.setattr(reconcile, "run_markers_reconcile", run)
        monkeypatch.setattr(
            "media_preview_generator.web.settings_manager.get_settings_manager",
            lambda: MagicMock(processing_paused=True),
        )

        run_markers_check_servers_daily()

        run.assert_called_once_with()

    def test_a_failure_is_logged_not_raised(self, monkeypatch):
        from media_preview_generator.web.scheduler import run_markers_check_servers_daily

        monkeypatch.setattr(reconcile, "run_markers_reconcile", MagicMock(side_effect=RuntimeError("jobs.db locked")))
        monkeypatch.setattr(
            "media_preview_generator.web.settings_manager.get_settings_manager",
            lambda: MagicMock(processing_paused=False),
        )

        run_markers_check_servers_daily()


class TestExecuteScheduledMultiKind:
    """A whole-library tick queues one job per ticked kind; a Recently Added tick queues one scan with its follow-ups."""

    @pytest.fixture
    def env(self, scheduler_manager, monkeypatch):
        from media_preview_generator.loudness import job as loudness_job

        jobs: list = []
        fake_jm = MagicMock()
        fake_jm.get_all_jobs.side_effect = lambda: list(jobs)
        fake_jm.request_resume.return_value = True
        monkeypatch.setattr("media_preview_generator.web.jobs.get_job_manager", lambda: fake_jm)
        fake_sm = MagicMock(processing_paused=False)
        fake_sm.get.side_effect = lambda key, default=None: default
        monkeypatch.setattr("media_preview_generator.web.settings_manager.get_settings_manager", lambda: fake_sm)
        callback = MagicMock()
        scheduler_manager.set_run_job_callback(callback)
        intro = MagicMock(return_value=MagicMock(id="ic-1"))
        loudness = MagicMock(return_value=MagicMock(id="ld-1"))
        monkeypatch.setattr(triggers, "create_intro_credits_job", intro)
        monkeypatch.setattr(loudness_job, "create_loudness_job", loudness)
        recent = MagicMock(return_value="ra-1")
        monkeypatch.setattr("media_preview_generator.web.routes.job_runner._start_recently_added_job_async", recent)
        return SimpleNamespace(jobs=jobs, jm=fake_jm, callback=callback, intro=intro, loudness=loudness, recent=recent)

    @staticmethod
    def _tick(manager, config, **kwargs):
        from media_preview_generator.web.scheduler import execute_scheduled_job

        schedule = manager.create_schedule(name="multi", cron_expression="0 3 * * *", config=config)
        execute_scheduled_job(schedule["id"], [], "All", config, kwargs.get("priority"), None)
        return manager.get_schedule(schedule["id"])

    def test_every_ticked_kind_is_queued_with_the_schedules_priority_and_filters(self, scheduler_manager, env):
        filters = {"added_filter": "last_days", "added_last_days": 7}
        schedule = self._tick(
            scheduler_manager,
            {"job_kinds": ["previews", "intro_credits", "loudness"], "job_type": "full_library", **filters},
            priority=1,
        )

        env.callback.assert_called_once()
        assert "job_kinds" not in env.callback.call_args.kwargs["config"]
        assert env.callback.call_args.kwargs["config"]["added_last_days"] == 7
        env.intro.assert_called_once_with(
            library_name="Intro & Credits: All", priority=1, source="schedule", libraries=[],
            parent_schedule_id=schedule["id"], scan_filters=filters,
        )  # fmt: skip
        env.loudness.assert_called_once_with(
            library_name="Plex loudness: All", priority=1, source="schedule", libraries=[], server_id=None,
            parent_schedule_id=schedule["id"], scan_filters=filters, sort_by=None,
        )  # fmt: skip
        env.recent.assert_not_called()
        assert schedule["last_run"] is not None

    @pytest.mark.parametrize("sort_by", ["random", "newest", None])
    def test_loudness_gets_the_schedules_processing_order(self, scheduler_manager, env, sort_by):
        config = {"job_kinds": ["loudness"], "job_type": "full_library"}
        if sort_by:
            config["sort_by"] = sort_by
        self._tick(scheduler_manager, config)

        assert env.loudness.call_args.kwargs["sort_by"] == sort_by

    def test_loudness_defaults_to_low_priority(self, scheduler_manager, env):
        self._tick(scheduler_manager, {"job_kinds": ["loudness"]})

        assert env.loudness.call_args.kwargs["priority"] == 3
        env.callback.assert_not_called()
        env.intro.assert_not_called()

    def test_one_kind_failing_does_not_stop_the_others(self, scheduler_manager, env):
        env.callback.side_effect = RuntimeError("runner down")
        env.intro.side_effect = RuntimeError("jobs.db locked")

        schedule = self._tick(scheduler_manager, {"job_kinds": ["previews", "intro_credits", "loudness"]})

        env.loudness.assert_called_once_with(
            library_name="Plex loudness: All", priority=3, source="schedule", libraries=[], server_id=None,
            parent_schedule_id=schedule["id"], scan_filters={}, sort_by=None,
        )  # fmt: skip
        env.intro.assert_called_once_with(
            library_name="Intro & Credits: All", priority=3, source="schedule", libraries=[],
            parent_schedule_id=schedule["id"], scan_filters={},
        )  # fmt: skip
        env.callback.assert_called_once()
        assert env.callback.call_args.kwargs["parent_schedule_id"] == schedule["id"]
        assert schedule["last_run"] is not None

    def test_last_run_stays_unset_when_nothing_was_queued(self, scheduler_manager, env):
        env.loudness.side_effect = RuntimeError("jobs.db locked")

        schedule = self._tick(scheduler_manager, {"job_kinds": ["loudness"]})

        assert schedule["last_run"] is None

    def test_a_loudness_job_of_this_schedule_still_unfinished_is_not_started_again(self, scheduler_manager, env):
        from media_preview_generator.web.jobs import JobStatus
        from media_preview_generator.web.scheduler import execute_scheduled_job

        schedule = scheduler_manager.create_schedule(name="multi", cron_expression="0 3 * * *", config={})
        env.jobs.append(
            MagicMock(id="ld-0", kind="loudness", config={}, status=JobStatus.RUNNING, paused=False,
                      parent_schedule_id=schedule["id"])
        )  # fmt: skip
        execute_scheduled_job(schedule["id"], [], "All", {"job_kinds": ["loudness"]}, None, None)

        env.loudness.assert_not_called()

    @pytest.mark.parametrize(
        ("kinds", "follow_ups", "previews"),
        [
            (["previews"], [], True),
            (["previews", "intro_credits", "loudness"], ["intro_credits", "loudness"], True),
            (["intro_credits"], ["intro_credits"], False),
            (["loudness", "intro_credits"], ["intro_credits", "loudness"], False),
        ],
    )
    def test_recently_added_is_one_scan_carrying_the_follow_up_kinds(
        self, scheduler_manager, env, kinds, follow_ups, previews
    ):
        schedule = self._tick(
            scheduler_manager, {"job_kinds": kinds, "job_type": "recently_added", "lookback_hours": 6}
        )

        env.recent.assert_called_once()
        kwargs = env.recent.call_args.kwargs
        assert kwargs["lookback_hours"] == 6.0
        assert kwargs["follow_up_kinds"] == follow_ups and kwargs["make_previews"] is previews
        env.callback.assert_not_called()
        env.intro.assert_not_called()
        env.loudness.assert_not_called()
        assert schedule["last_run"] is not None

    @pytest.mark.parametrize(
        ("config", "paused_kind", "resumes"),
        [
            ({"job_kinds": ["previews", "intro_credits"]}, "intro_credits", True),
            ({"job_kinds": ["previews", "intro_credits"]}, "previews", True),
            ({"job_kinds": ["previews", "intro_credits"]}, "loudness", False),
            ({"job_kinds": ["loudness"]}, "loudness", True),
            ({"job_kinds": ["loudness"]}, "previews", False),
        ],
    )
    def test_a_stop_time_pause_is_resumed_for_any_ticked_kind_and_the_tick_queues_nothing_else(
        self, scheduler_manager, env, config, paused_kind, resumes
    ):
        from media_preview_generator.web.jobs import JobStatus

        schedule = scheduler_manager.create_schedule(name="multi", cron_expression="0 3 * * *", config=config)
        env.jobs.append(
            MagicMock(id="old-0", kind=paused_kind, config={"paused_by_schedule": True}, status=JobStatus.RUNNING,
                      paused=True, parent_schedule_id=schedule["id"])
        )  # fmt: skip
        from media_preview_generator.web.scheduler import execute_scheduled_job

        execute_scheduled_job(schedule["id"], [], "All", dict(config), None, None)

        if resumes:
            env.jm.request_resume.assert_called_once_with("old-0", only_paused_by_schedule=True)
            env.callback.assert_not_called()
            env.intro.assert_not_called()
            env.loudness.assert_not_called()
        else:
            env.jm.request_resume.assert_not_called()
            queued_kinds = {
                kind
                for kind, mock in (("previews", env.callback), ("intro_credits", env.intro), ("loudness", env.loudness))
                if mock.call_args_list
            }
            assert queued_kinds == set(config["job_kinds"])
            for mock in (env.callback, env.intro, env.loudness):
                for queued in mock.call_args_list:
                    assert queued.kwargs["parent_schedule_id"] == schedule["id"]

    def test_recently_added_resumes_its_paused_scan(self, scheduler_manager, env):
        from media_preview_generator.web.jobs import JobStatus
        from media_preview_generator.web.scheduler import execute_scheduled_job

        config = {"job_kinds": ["loudness"], "job_type": "recently_added"}
        schedule = scheduler_manager.create_schedule(name="multi", cron_expression="0 3 * * *", config=config)
        env.jobs.append(
            MagicMock(id="scan-0", kind="previews", config={"paused_by_schedule": True, "source": "scheduled_recently_added"},
                      status=JobStatus.RUNNING, paused=True, parent_schedule_id=schedule["id"])
        )  # fmt: skip
        execute_scheduled_job(schedule["id"], [], "All", dict(config), None, None)

        env.jm.request_resume.assert_called_once_with("scan-0", only_paused_by_schedule=True)
        env.recent.assert_not_called()


class TestScheduleExactLibraryPicks:
    """``config.libraries`` ({server_id, library_id} pairs, as Start new job sends them) reach the job creators as picked."""

    PLEX_1 = TestExecuteScheduledIntroCreditsJob.PLEX_1
    JF_1 = {
        "id": "jf-1",
        "type": "jellyfin",
        "enabled": True,
        "markers": {"enabled": True},
        "libraries": [{"id": "f1", "name": "Films", "remote_paths": ["/m/films"]}],
    }
    ONE_SERVER = [{"server_id": "plex-1", "library_id": "1"}, {"server_id": "plex-1", "library_id": "3"}]
    TWO_SERVERS = [{"server_id": "plex-1", "library_id": "1"}, {"server_id": "jf-1", "library_id": "f1"}]
    INTRO_ONE_SERVER = [{"server_id": "plex-1", "library_id": "1"}]

    @pytest.fixture
    def env(self, scheduler_manager, monkeypatch):
        from media_preview_generator.loudness import job as loudness_job

        fake_jm = MagicMock()
        fake_jm.get_all_jobs.side_effect = lambda: []
        monkeypatch.setattr("media_preview_generator.web.jobs.get_job_manager", lambda: fake_jm)
        settings = {"media_servers": [self.PLEX_1, self.JF_1]}
        fake_sm = MagicMock(processing_paused=False)
        fake_sm.get.side_effect = lambda key, default=None: settings.get(key, default)
        monkeypatch.setattr("media_preview_generator.web.settings_manager.get_settings_manager", lambda: fake_sm)
        callback = MagicMock()
        scheduler_manager.set_run_job_callback(callback)
        intro = MagicMock(return_value=MagicMock(id="ic-1"))
        loudness = MagicMock(return_value=MagicMock(id="ld-1"))
        monkeypatch.setattr(triggers, "create_intro_credits_job", intro)
        monkeypatch.setattr(loudness_job, "create_loudness_job", loudness)
        return SimpleNamespace(callback=callback, intro=intro, loudness=loudness)

    @staticmethod
    def _tick(manager, kind, *, pairs=None, library_ids=(), server_id=None):
        from media_preview_generator.web.scheduler import execute_scheduled_job

        config = {"job_kinds": [kind], **({"libraries": pairs} if pairs else {})}
        schedule = manager.create_schedule(
            name="picks", cron_expression="0 3 * * *", config=config, library_ids=list(library_ids), server_id=server_id
        )
        execute_scheduled_job(schedule["id"], list(library_ids), "Picks", dict(schedule["config"]), None, server_id)
        return schedule

    def test_loudness_on_one_server_is_pinned_to_it(self, scheduler_manager, env):
        schedule = self._tick(scheduler_manager, "loudness", pairs=self.ONE_SERVER, library_ids=["1", "3"])

        env.loudness.assert_called_once_with(
            library_name="Plex loudness: Picks", priority=3, source="schedule", libraries=self.ONE_SERVER,
            server_id="plex-1", parent_schedule_id=schedule["id"], scan_filters={}, sort_by=None,
        )  # fmt: skip

    def test_loudness_on_two_servers_gets_the_pairs_unpinned(self, scheduler_manager, env):
        schedule = self._tick(scheduler_manager, "loudness", pairs=self.TWO_SERVERS, library_ids=["1", "f1"])

        env.loudness.assert_called_once_with(
            library_name="Plex loudness: Picks", priority=3, source="schedule", libraries=self.TWO_SERVERS,
            server_id=None, parent_schedule_id=schedule["id"], scan_filters={}, sort_by=None,
        )  # fmt: skip

    def test_intro_credits_on_one_server_is_pinned_to_it(self, scheduler_manager, env):
        schedule = self._tick(scheduler_manager, "intro_credits", pairs=self.INTRO_ONE_SERVER, library_ids=["1"])

        env.intro.assert_called_once_with(
            library_name="Intro & Credits: Picks", priority=3, source="schedule", libraries=self.INTRO_ONE_SERVER,
            parent_schedule_id=schedule["id"], scan_filters={}, server_id="plex-1",
        )  # fmt: skip

    def test_intro_credits_on_two_servers_gets_the_pairs_unpinned(self, scheduler_manager, env):
        schedule = self._tick(scheduler_manager, "intro_credits", pairs=self.TWO_SERVERS, library_ids=["1", "f1"])

        env.intro.assert_called_once_with(
            library_name="Intro & Credits: Picks", priority=3, source="schedule", libraries=self.TWO_SERVERS,
            parent_schedule_id=schedule["id"], scan_filters={},
        )  # fmt: skip

    def test_previews_on_two_servers_start_one_unpinned_job_carrying_the_pairs(self, scheduler_manager, env):
        schedule = self._tick(scheduler_manager, "previews", pairs=self.TWO_SERVERS, library_ids=["1", "f1"])

        env.callback.assert_called_once()
        kwargs = env.callback.call_args.kwargs
        assert "server_id" not in kwargs
        assert kwargs["parent_schedule_id"] == schedule["id"]
        assert kwargs["config"]["libraries"] == self.TWO_SERVERS
        assert "job_kinds" not in kwargs["config"]
        assert "selected_library_ids" not in kwargs["config"]

    def test_previews_on_one_server_stay_pinned_without_pairs(self, scheduler_manager, env):
        self._tick(scheduler_manager, "previews", pairs=self.ONE_SERVER, library_ids=["1", "3"])

        kwargs = env.callback.call_args.kwargs
        assert kwargs["server_id"] == "plex-1"
        assert "libraries" not in kwargs["config"]

    def test_legacy_loudness_with_flat_ids_and_a_server_keeps_that_pin(self, scheduler_manager, env):
        schedule = self._tick(scheduler_manager, "loudness", library_ids=["1", "3"], server_id="plex-1")

        env.loudness.assert_called_once_with(
            library_name="Plex loudness: Picks", priority=3, source="schedule", libraries=self.ONE_SERVER,
            server_id="plex-1", parent_schedule_id=schedule["id"], scan_filters={}, sort_by=None,
        )  # fmt: skip

    def test_legacy_loudness_with_ids_on_two_servers_still_starts_nothing(self, scheduler_manager, env):
        self._tick(scheduler_manager, "loudness", library_ids=["1", "f1"])

        env.loudness.assert_not_called()

    def test_legacy_intro_credits_infers_the_one_server_owning_the_ids(self, scheduler_manager, env):
        schedule = self._tick(scheduler_manager, "intro_credits", library_ids=["1"])

        env.intro.assert_called_once_with(
            library_name="Intro & Credits: Picks", priority=3, source="schedule",
            libraries=self.INTRO_ONE_SERVER,
            parent_schedule_id=schedule["id"], scan_filters={}, server_id="plex-1",
        )  # fmt: skip

    @pytest.mark.parametrize(
        ("libraries", "message"),
        [
            ("x", "libraries must be"),
            ([{"server_id": "a"}], "libraries must be"),
            ([{"server_id": "a", "library_id": ""}], "libraries must be"),
            ([{"server_id": "a", "library_id": 1}], "libraries must be"),
            (["a:1"], "libraries must be"),
        ],
    )
    def test_malformed_picks_are_rejected(self, libraries, message):
        from media_preview_generator.web.scheduler import normalize_schedule_config

        with pytest.raises(ValueError, match=message):
            normalize_schedule_config({"job_kinds": ["loudness"], "libraries": libraries})

    def test_picks_are_kept_trimmed_and_deduplicated_and_an_empty_list_means_absent(self):
        from media_preview_generator.web.scheduler import normalize_schedule_config

        kept = normalize_schedule_config(
            {"libraries": [{"server_id": " a ", "library_id": "1"}, {"server_id": "a", "library_id": "1"}]}
        )
        assert kept["libraries"] == [{"server_id": "a", "library_id": "1"}]
        assert "libraries" not in normalize_schedule_config({"libraries": []})
        assert "libraries" not in normalize_schedule_config({})


class TestDailyCheckServersOrphans:
    """The daily check frees a Check servers job a deleted schedule's stop time left paused, so it can't block it."""

    @pytest.fixture
    def jm(self, scheduler_manager, tmp_path, monkeypatch):
        from media_preview_generator.web.jobs import JobManager

        jm = JobManager(config_dir=str(tmp_path / "jobs"))
        monkeypatch.setattr("media_preview_generator.web.jobs.get_job_manager", lambda: jm)
        monkeypatch.setattr(reconcile, "get_job_manager", lambda: jm)
        run = MagicMock()
        monkeypatch.setattr(reconcile, "run_markers_reconcile", run)
        jm.run = run
        return jm

    @staticmethod
    def _check_servers_job(jm, schedule_id):
        job = jm.create_job(
            library_name="Intro & Credits · Check servers",
            config={"reconcile": True, "kind": "intro_credits", "libraries": []},
            kind="intro_credits",
            parent_schedule_id=schedule_id,
        )
        jm.start_job(job.id)
        return job

    def test_a_job_held_by_a_deleted_schedule_is_resumed_with_a_log_line(self, jm):
        from media_preview_generator.web.scheduler import execute_schedule_stop, run_markers_check_servers_daily

        job = self._check_servers_job(jm, "deleted-schedule")
        execute_schedule_stop("deleted-schedule")
        assert jm.get_job(job.id).paused

        run_markers_check_servers_daily()

        assert jm.get_job(job.id).paused is False
        assert "paused_by_schedule" not in jm.get_job(job.id).config
        assert [line for line in jm.get_logs(job.id) if "its schedule 'deleted-schedule' no longer exists" in line]
        jm.run.assert_called_once_with()

    def test_a_job_held_by_an_existing_schedule_stays_paused(self, scheduler_manager, jm):
        from media_preview_generator.web.scheduler import execute_schedule_stop, run_markers_check_servers_daily

        schedule = scheduler_manager.create_schedule(name="x", cron_expression="0 3 * * *", stop_time="06:00")
        job = self._check_servers_job(jm, schedule["id"])
        execute_schedule_stop(schedule["id"])

        run_markers_check_servers_daily()

        assert jm.get_job(job.id).paused is True
        assert jm.get_job(job.id).config["paused_by_schedule"] is True

    def test_a_pause_by_hand_on_the_job_of_a_deleted_schedule_is_kept(self, jm):
        from media_preview_generator.web.scheduler import run_markers_check_servers_daily

        job = self._check_servers_job(jm, "deleted-schedule")
        assert jm.request_pause(job.id)

        run_markers_check_servers_daily()

        assert jm.get_job(job.id).paused is True


class TestStartRemovesLeftoverScheduleJobs:
    """A crash between saving schedules.json and dropping the jobstore entries leaves crons for deleted schedules."""

    def test_crons_of_schedules_missing_from_the_file_are_removed_and_the_rest_kept(self, tmp_path):
        from media_preview_generator.web.scheduler import (
            execute_schedule_stop,
            execute_scheduled_job,
            run_markers_check_servers_daily,
        )

        config_dir = str(tmp_path / "config")
        first = ScheduleManager(config_dir=config_dir, run_job_callback=None)
        first.start()
        try:
            kept = first.create_schedule(name="kept", cron_expression="0 3 * * *", stop_time="06:00")
        finally:
            first.stop()
        # The app died after a delete saved schedules.json but before it dropped the jobstore entries (the tests'
        # in-memory jobstore doesn't survive a manager, so the leftovers are queued on the next one).
        second = ScheduleManager(config_dir=config_dir, run_job_callback=None)
        second.scheduler.add_job(execute_scheduled_job, "cron", hour=4, id="gone", args=["gone"])
        second.scheduler.add_job(execute_schedule_stop, "cron", hour=5, id="gone__stop", args=["gone"])
        second.scheduler.add_job(run_markers_check_servers_daily, "cron", hour=1, id="not-a-schedule-cron")
        second.start()
        try:
            jobs = {job.id: job.func for job in second.scheduler.get_jobs()}
        finally:
            second.stop()

        assert jobs == {
            kept["id"]: execute_scheduled_job,
            f"{kept['id']}__stop": execute_schedule_stop,
            "not-a-schedule-cron": run_markers_check_servers_daily,
            "__library_health_nightly": jobs["__library_health_nightly"],
            "__markers_check_servers_daily": run_markers_check_servers_daily,
            "__plex_held_rereads": jobs["__plex_held_rereads"],
        }

    def test_nothing_is_removed_when_schedules_json_failed_to_load(self, tmp_path):
        from media_preview_generator.web.scheduler import execute_scheduled_job

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "schedules.json").write_text("{not json")
        manager = ScheduleManager(config_dir=str(config_dir), run_job_callback=None)
        manager.scheduler.add_job(execute_scheduled_job, "cron", hour=4, id="existing", args=["existing"])
        manager.start()
        try:
            ids = {job.id for job in manager.scheduler.get_jobs()}
        finally:
            manager.stop()

        assert manager.load_status["status"] != "ok"
        assert "existing" in ids


class TestPausedIntroCreditsJobsAfterAScheduleChanges:
    """Jobs a schedule's stop time paused, by hand, or not at all, after the schedule switches mode or is deleted.

    Real JobManager and ScheduleManager.
    """

    FIND = {"job_kinds": ["intro_credits"], "job_type": "full_library"}

    @pytest.fixture
    def jm(self, scheduler_manager, tmp_path, monkeypatch):
        from media_preview_generator.web.jobs import JobManager

        jm = JobManager(config_dir=str(tmp_path / "jobs"))
        monkeypatch.setattr("media_preview_generator.web.jobs.get_job_manager", lambda: jm)
        monkeypatch.setattr(reconcile, "get_job_manager", lambda: jm)
        monkeypatch.setattr(triggers, "get_job_manager", lambda: jm)
        monkeypatch.setattr(triggers, "start_intro_credits_job_async", lambda job_id: None)
        monkeypatch.setattr(triggers, "markers_enabled_anywhere", lambda: True)
        sm = MagicMock(processing_paused=False)
        sm.get.side_effect = lambda key, default=None: default
        monkeypatch.setattr("media_preview_generator.web.settings_manager.get_settings_manager", lambda: sm)
        return jm

    @staticmethod
    def _tick(schedule_id, config):
        from media_preview_generator.web.scheduler import execute_scheduled_job

        execute_scheduled_job(schedule_id, [], "All", dict(config), None, None)

    @staticmethod
    def _paused_by_stop_time(jm, schedule_id, job_id):
        from media_preview_generator.web.scheduler import execute_schedule_stop

        jm.start_job(job_id)
        execute_schedule_stop(schedule_id)
        assert jm.get_job(job_id).paused and jm.get_job(job_id).config["paused_by_schedule"] is True

    @staticmethod
    def _live(jm):
        return [job for job in jm.get_all_jobs() if job.status.value in ("pending", "running")]

    def _schedule(self, scheduler_manager, config):
        return scheduler_manager.create_schedule(
            name="nightly", cron_expression="0 2 * * *", config=dict(config), stop_time="06:00"
        )

    @pytest.mark.parametrize("mode", ["FIND"])
    @pytest.mark.parametrize("trigger", ["start-tick", "run-now"])
    @pytest.mark.parametrize("pause", ["stop-time", "by-hand", "stop-time-then-by-hand"])
    def test_only_a_stop_time_pause_is_resumed_by_a_start_tick_or_run_now(
        self, scheduler_manager, jm, mode, trigger, pause
    ):
        config = getattr(self, mode)
        schedule = self._schedule(scheduler_manager, config)
        self._tick(schedule["id"], config)
        (job,) = jm.get_all_jobs()
        if pause == "by-hand":
            jm.start_job(job.id)
            assert jm.request_pause(job.id)  # POST /api/jobs/<id>/pause
        else:
            self._paused_by_stop_time(jm, schedule["id"], job.id)
            if pause == "stop-time-then-by-hand":
                assert jm.request_pause(job.id)

        if trigger == "run-now":
            assert scheduler_manager.run_now(schedule["id"]) is True
        else:
            self._tick(schedule["id"], config)

        resumed = pause == "stop-time"
        assert jm.get_job(job.id).paused is not resumed
        assert jm.get_job(job.id).config.get("paused_by_schedule", False) is False
        # Either way the job is still this mode's unfinished job, so no second one is queued beside it.
        assert [j.id for j in self._live(jm)] == [job.id]
        resume_lines = [line for line in jm.get_logs(job.id) if "Resumed by schedule" in line]
        assert len(resume_lines) == int(resumed)

    def test_pause_causes_are_independent_and_each_resume_clears_only_its_owner(self, jm):
        job = jm.create_job(library_name="x", config={"libraries": []}, kind="intro_credits")
        jm.start_job(job.id)
        last_active_at = job.config["last_active_at"]
        assert jm.request_pause(job.id, by_schedule=True)
        assert jm.request_pause(job.id)
        assert job.config["pause_reasons"] == ["manual", "schedule"]
        assert jm.request_resume(job.id)
        assert job.paused and job.config["pause_reasons"] == ["schedule"]
        assert jm.request_resume(job.id, only_paused_by_schedule=True)
        assert job.config == {"libraries": [], "last_active_at": last_active_at} and not job.paused

    @pytest.mark.parametrize("ending", ["completed", "cancelled"])
    def test_the_pause_record_goes_when_a_job_paused_by_its_stop_time_ends(self, scheduler_manager, jm, ending):
        from media_preview_generator.web.jobs import JobManager

        schedule = self._schedule(scheduler_manager, self.FIND)
        self._tick(schedule["id"], self.FIND)
        (job,) = jm.get_all_jobs()
        self._paused_by_stop_time(jm, schedule["id"], job.id)

        # The last items finish while it is paused, or the user cancels it.
        jm.complete_job(job.id) if ending == "completed" else jm.cancel_job(job.id)

        assert "paused_by_schedule" not in jm.get_job(job.id).config
        assert "paused_by_schedule" not in JobManager(config_dir=jm.config_dir).get_job(job.id).config

    def test_a_pause_by_hand_between_the_stop_times_check_and_its_pause_stays_a_pause_by_hand(
        self, scheduler_manager, jm, monkeypatch
    ):
        import copy

        from media_preview_generator.web.scheduler import execute_schedule_stop

        schedule = self._schedule(scheduler_manager, self.FIND)
        self._tick(schedule["id"], self.FIND)
        (job,) = jm.get_all_jobs()
        jm.start_job(job.id)
        listed = jm.get_all_jobs

        def listed_then_paused_by_hand():
            snapshot = [copy.copy(j) for j in listed()]  # what the stop time saw: running, not paused
            assert jm.request_pause(job.id)  # POST /api/jobs/<id>/pause lands just after
            return snapshot

        monkeypatch.setattr(jm, "get_all_jobs", listed_then_paused_by_hand)
        execute_schedule_stop(schedule["id"])
        monkeypatch.setattr(jm, "get_all_jobs", listed)
        assert jm.get_job(job.id).paused and jm.get_job(job.id).config["pause_reasons"] == ["manual", "schedule"]

        self._tick(schedule["id"], self.FIND)
        assert jm.get_job(job.id).paused  # the start tick doesn't resume a pause by hand

    def test_a_pause_by_hand_between_the_start_ticks_check_and_its_resume_is_kept(
        self, scheduler_manager, jm, monkeypatch
    ):
        import copy

        schedule = self._schedule(scheduler_manager, self.FIND)
        self._tick(schedule["id"], self.FIND)
        (job,) = jm.get_all_jobs()
        self._paused_by_stop_time(jm, schedule["id"], job.id)
        listed = jm.get_all_jobs

        def listed_then_paused_by_hand():
            snapshot = [copy.deepcopy(j) for j in listed()]  # what the tick saw: paused by its stop time
            assert jm.request_pause(job.id)  # the user pauses it again by hand just after
            return snapshot

        monkeypatch.setattr(jm, "get_all_jobs", listed_then_paused_by_hand)
        self._tick(schedule["id"], self.FIND)

        assert jm.get_job(job.id).paused
        assert not [line for line in jm.get_logs(job.id) if "Resumed by schedule" in line]

    def test_two_start_ticks_at_once_queue_one_find_markers_job(self, scheduler_manager, jm, monkeypatch):
        import threading

        schedule = self._schedule(scheduler_manager, self.FIND)
        entered, go = threading.Event(), threading.Event()
        real_create = triggers.create_intro_credits_job

        def slow_create(**kwargs):
            entered.set()
            go.wait(5)
            return real_create(**kwargs)

        monkeypatch.setattr(triggers, "create_intro_credits_job", slow_create)
        first = threading.Thread(target=self._tick, args=(schedule["id"], self.FIND))
        second = threading.Thread(target=self._tick, args=(schedule["id"], self.FIND))  # Run now beside the cron tick
        first.start()
        assert entered.wait(5)
        second.start()
        second.join(0.5)  # without serialising, the second tick is inside create by now
        go.set()
        first.join(5)
        second.join(5)

        assert len(jm.get_all_jobs()) == 1

    PREVIEWS = {"job_kinds": ["previews"], "job_type": "full_library"}
    RECENTLY_ADDED = {"job_kinds": ["previews"], "job_type": "recently_added"}
    BOTH = {"job_kinds": ["previews", "intro_credits"], "job_type": "full_library"}

    @pytest.mark.parametrize(
        ("before", "after", "warned"),
        [
            ("FIND", "PREVIEWS", True),
            ("FIND", "RECENTLY_ADDED", True),
            ("FIND", "BOTH", False),  # a tick that still runs Intro & Credits resumes its paused job
            ("PREVIEWS", "BOTH", False),
            ("FIND", "FIND", False),
            ("PREVIEWS", "FIND", True),
            ("PREVIEWS", "RECENTLY_ADDED", True),
            ("PREVIEWS", "PREVIEWS", False),
            # Recently Added resumes nothing either way; the job left paused is a preview job, like PREVIEWS' rows.
            ("RECENTLY_ADDED", "PREVIEWS", False),
        ],
    )
    def test_a_schedule_switched_to_jobs_that_dont_resume_its_stop_paused_job_says_so(
        self, scheduler_manager, jm, before, after, warned
    ):
        from loguru import logger

        before_cfg, after_cfg = getattr(self, before), getattr(self, after)
        schedule = self._schedule(scheduler_manager, before_cfg)
        kind = "intro_credits" if before_cfg["job_kinds"] == ["intro_credits"] else "previews"
        job = jm.create_job(library_name="nightly run", kind=kind, parent_schedule_id=schedule["id"], config={})
        self._paused_by_stop_time(jm, schedule["id"], job.id)
        warnings: list[str] = []
        handler = logger.add(warnings.append, level="WARNING", format="{message}")
        try:
            scheduler_manager.update_schedule(schedule["id"], config=dict(after_cfg))
        finally:
            logger.remove(handler)

        expected = [
            f"Schedule 'nightly' ({schedule['id']}) no longer runs the kind of job its stop time paused: job "
            f"{job.id[:8]} ({job.library_name}) stays paused, and no later start tick of this schedule will resume it. "
            "Resume or cancel it on the dashboard."
        ]
        assert [w.strip() for w in warnings] == (expected if warned else [])
        assert jm.get_job(job.id).paused

    def test_deleting_a_schedule_names_its_paused_job_and_leaves_it_paused(self, scheduler_manager, jm):
        from loguru import logger

        schedule = self._schedule(scheduler_manager, self.FIND)
        self._tick(schedule["id"], self.FIND)
        (check,) = jm.get_all_jobs()
        self._paused_by_stop_time(jm, schedule["id"], check.id)
        other = jm.create_job(library_name="done", config={"libraries": []}, parent_schedule_id=schedule["id"],
                              kind="intro_credits")  # fmt: skip
        jm.start_job(other.id)
        jm.complete_job(other.id)
        warnings: list[str] = []
        handler = logger.add(warnings.append, level="WARNING", format="{message}")
        try:
            assert scheduler_manager.delete_schedule(schedule["id"]) is True
        finally:
            logger.remove(handler)

        assert [w.strip() for w in warnings] == [
            f"Schedule 'nightly' ({schedule['id']}) was deleted while its job {check.id[:8]} "
            "(Intro & Credits: All) is paused; no later start tick of this schedule will resume it. Resume "
            "or cancel it on the dashboard."
        ]
        assert jm.get_job(check.id).paused and jm.get_job(check.id).status.value == "running"


def test_nothing_is_scheduled_after_a_fresh_start(tmp_path):
    """A new install has no user schedule; the only jobs are the internal ``__`` ones.

    The internal ``__`` jobs (library-health nightly, daily Check servers, held Plex re-reads) are not user schedules.
    """
    manager = ScheduleManager(config_dir=str(tmp_path / "config"), run_job_callback=None)
    manager.start()
    try:
        manager.apply_quiet_hours({})
        assert manager.get_all_schedules() == []
        assert sorted(j.id for j in manager.scheduler.get_jobs()) == [
            "__library_health_nightly",
            "__markers_check_servers_daily",
            "__plex_held_rereads",
        ]
    finally:
        manager.stop()


class TestMultiLibrarySchedules:
    """Phase H7: Schedule.library_ids list + back-compat migration."""

    def test_create_schedule_with_multiple_libraries(self, scheduler_manager):
        schedule = scheduler_manager.create_schedule(
            name="Multi",
            interval_minutes=60,
            library_ids=["1", "2", "3"],
            library_name="Movies, TV, Anime",
        )
        assert schedule["library_ids"] == ["1", "2", "3"]
        # library_id stays None when there's more than one library.
        assert schedule["library_id"] is None

    def test_create_schedule_with_single_library_keeps_back_compat(self, scheduler_manager):
        schedule = scheduler_manager.create_schedule(
            name="Single",
            interval_minutes=60,
            library_ids=["7"],
            library_name="Movies",
        )
        assert schedule["library_ids"] == ["7"]
        assert schedule["library_id"] == "7"  # mirrored for legacy readers

    def test_legacy_library_id_arg_still_works(self, scheduler_manager):
        # Older callers that still pass library_id= keep working.
        schedule = scheduler_manager.create_schedule(
            name="Legacy",
            interval_minutes=60,
            library_id="42",
            library_name="Movies",
        )
        assert schedule["library_id"] == "42"
        assert schedule["library_ids"] == ["42"]

    def test_load_migrates_legacy_library_id_to_library_ids(self, tmp_path):
        """Schedules persisted in pre-H7 shape (only library_id) should migrate
        to library_ids on read so the rest of the codebase stays uniform."""
        import json

        config_dir = str(tmp_path / "config")
        os.makedirs(config_dir, exist_ok=True)
        legacy = {
            "schedules": {
                "abc": {
                    "id": "abc",
                    "name": "Legacy",
                    "trigger_type": "interval",
                    "trigger_value": "60",
                    "library_id": "5",
                    "library_name": "Movies",
                    "config": {},
                    "enabled": True,
                    "created_at": "2025-01-01T00:00:00+00:00",
                    "last_run": None,
                    "next_run": None,
                    "priority": None,
                }
            }
        }
        with open(os.path.join(config_dir, "schedules.json"), "w") as f:
            json.dump(legacy, f)

        manager = ScheduleManager(config_dir=config_dir, run_job_callback=None)
        sched = manager.get_schedule("abc")
        assert sched["library_ids"] == ["5"]  # promoted on load
        assert sched["library_id"] == "5"  # original kept

    def test_update_schedule_with_library_ids(self, scheduler_manager):
        sched = scheduler_manager.create_schedule(
            name="X",
            interval_minutes=60,
            library_id="1",
            library_name="Movies",
        )
        updated = scheduler_manager.update_schedule(
            sched["id"],
            library_ids=["1", "2", "3"],
            library_name="Movies, TV, Anime",
        )
        assert updated["library_ids"] == ["1", "2", "3"]
        assert updated["library_id"] is None  # cleared when multi


class TestQuietHoursRecomputeDrainsPending:
    """A quiet-hours window ending must START the pending backlog, not just
    flip the flag — otherwise jobs revived PENDING-while-paused at boot (pause
    survives restarts now) sit forever when the window closes."""

    def _run(self, in_window: bool, currently_paused: bool):
        from media_preview_generator.web import scheduler as sched

        sm = MagicMock()
        sm.processing_paused = currently_paused
        sm.get.return_value = {"enabled": True, "windows": []}

        def set_reason(reason, value):
            assert reason == "quiet_hours"
            changed = sm.processing_paused != value
            sm.processing_paused = value
            return changed

        sm.set_processing_pause_reason.side_effect = set_reason
        with (
            patch.object(sched, "is_now_in_any_quiet_window", return_value=in_window),
            patch("media_preview_generator.web.settings_manager.get_settings_manager", return_value=sm),
            patch("media_preview_generator.web.jobs.get_job_manager", return_value=MagicMock()),
            patch("media_preview_generator.web.routes.job_runner.resume_running_and_drain_pending") as mock_drain,
        ):
            sched._quiet_hours_recompute_and_apply()
        return sm, mock_drain

    def test_window_end_resumes_and_drains_pending(self):
        sm, mock_drain = self._run(in_window=False, currently_paused=True)
        assert sm.processing_paused is False, "window end clears the pause"
        mock_drain.assert_called_once_with()

    def test_window_start_pauses_without_draining(self):
        sm, mock_drain = self._run(in_window=True, currently_paused=False)
        assert sm.processing_paused is True, "window start sets the pause"
        mock_drain.assert_not_called()
