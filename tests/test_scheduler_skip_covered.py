"""A scheduled tick is skipped when a running whole-library scan already covers its libraries."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from loguru import logger

from media_preview_generator.markers import triggers
from media_preview_generator.web.jobs import JobStatus
from media_preview_generator.web.scheduler import ScheduleManager, execute_scheduled_job

PLEX = {
    "id": "plex-1",
    "type": "plex",
    "name": "Plex",
    "enabled": True,
    "url": "http://p",
    "auth": {"token": "t"},
    "markers": {"enabled": True, "plex": {"db_write_confirmed_at": "2026-09-13T00:00:00+00:00"}},
    "libraries": [
        {"id": "1", "name": "Movies", "remote_paths": ["/m"]},
        {"id": "2", "name": "TV Shows", "remote_paths": ["/t"]},
    ],
}


def _job(kind="previews", status=JobStatus.RUNNING, library_name="All Libraries", **fields):
    return SimpleNamespace(
        id="d5d8ef03aaaa",
        kind=kind,
        status=status,
        paused=fields.get("paused", False),
        parent_schedule_id="",
        library_id=fields.get("library_id"),
        library_name=library_name,
        server_id=fields.get("server_id"),
        config=fields.get("config", {}),
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    manager = ScheduleManager(config_dir=str(tmp_path), run_job_callback=None)
    monkeypatch.setattr("media_preview_generator.web.scheduler._schedule_manager", manager)
    manager.start()
    jobs: list = []
    fake_jm = MagicMock()
    fake_jm.get_all_jobs.side_effect = lambda: list(jobs)
    monkeypatch.setattr("media_preview_generator.web.jobs.get_job_manager", lambda: fake_jm)
    fake_sm = MagicMock(processing_paused=False)
    fake_sm.get.side_effect = lambda key, default=None: [PLEX] if key == "media_servers" else default
    monkeypatch.setattr("media_preview_generator.web.settings_manager.get_settings_manager", lambda: fake_sm)
    callback = MagicMock()
    manager.set_run_job_callback(callback)
    create = MagicMock(return_value=MagicMock(id="ic-1"))
    monkeypatch.setattr(triggers, "create_intro_credits_job", create)
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), level="INFO")
    yield SimpleNamespace(manager=manager, jobs=jobs, callback=callback, create=create, lines=lines)
    logger.remove(sink)
    manager.stop()


def _previews_schedule(env, library_ids, library_name):
    return env.manager.create_schedule(
        name="TV Daily",
        cron_expression="0 2 * * *",
        library_ids=library_ids,
        library_name=library_name,
        server_id="plex-1",
    )


def _fire(schedule, library_ids, library_name, config=None):
    execute_scheduled_job(schedule["id"], library_ids, library_name, config or {}, None, "plex-1")


class TestPreviewsSkip:
    def test_skipped_when_all_libraries_job_runs(self, env):
        env.jobs.append(_job(config={"selected_libraries": []}))
        schedule = _previews_schedule(env, ["2"], "TV Shows")

        _fire(schedule, ["2"], "TV Shows")

        env.callback.assert_not_called()
        expected = 'Skipped scheduled "TV Daily" run: library TV Shows is already being scanned by job d5d8ef03 (All Libraries)'
        assert any(expected in line for line in env.lines)
        assert env.manager.get_schedule(schedule["id"])["last_run"] is not None

    def test_skipped_when_same_library_job_pending(self, env):
        env.jobs.append(_job(status=JobStatus.PENDING, library_id="2", library_name="TV Shows", server_id="plex-1"))
        schedule = _previews_schedule(env, ["2"], "TV Shows")

        _fire(schedule, ["2"], "TV Shows")

        env.callback.assert_not_called()

    def test_runs_when_other_library_job_runs(self, env):
        env.jobs.append(_job(library_id="1", library_name="Movies", server_id="plex-1"))
        schedule = _previews_schedule(env, ["2"], "TV Shows")

        _fire(schedule, ["2"], "TV Shows")

        env.callback.assert_called_once()

    def test_runs_when_covering_job_is_filtered(self, env):
        env.jobs.append(_job(config={"selected_libraries": [], "added_last_days": 7}))
        schedule = _previews_schedule(env, ["2"], "TV Shows")

        _fire(schedule, ["2"], "TV Shows")

        env.callback.assert_called_once()

    def test_skipped_when_covering_job_has_the_ui_default_filter_values(self, env):
        config = {
            "force_generate": False,
            "added_filter": "all",
            "sort_by": "default",
            "added_last_days": None,
            "added_from": None,
            "added_to": None,
            "latest_seasons": None,
            "movie_year_from": None,
            "movie_year_to": None,
            "selected_libraries": [],
        }
        env.jobs.append(_job(config=config))
        schedule = _previews_schedule(env, ["2"], "TV Shows")

        _fire(schedule, ["2"], "TV Shows")

        env.callback.assert_not_called()

    def test_runs_when_covering_job_filters_last_days_from_the_ui(self, env):
        config = {"added_filter": "last_days", "added_last_days": 7, "latest_seasons": None, "selected_libraries": []}
        env.jobs.append(_job(config=config))
        schedule = _previews_schedule(env, ["2"], "TV Shows")

        _fire(schedule, ["2"], "TV Shows")

        env.callback.assert_called_once()

    def test_runs_when_covering_job_is_paused_by_schedule(self, env):
        env.jobs.append(_job(config={"selected_libraries": [], "pause_reasons": ["schedule"]}))
        schedule = _previews_schedule(env, ["2"], "TV Shows")

        _fire(schedule, ["2"], "TV Shows")

        env.callback.assert_called_once()

    def test_skipped_when_covering_job_is_paused_by_user(self, env):
        env.jobs.append(_job(config={"selected_libraries": [], "pause_reasons": ["manual"]}, paused=True))
        schedule = _previews_schedule(env, ["2"], "TV Shows")

        _fire(schedule, ["2"], "TV Shows")

        env.callback.assert_not_called()

    def test_runs_when_the_only_covering_job_is_this_schedules_own(self, env):
        schedule = _previews_schedule(env, ["2"], "TV Shows")
        own = _job(library_id="2", library_name="TV Shows", server_id="plex-1")
        own.parent_schedule_id = schedule["id"]
        env.jobs.append(own)

        _fire(schedule, ["2"], "TV Shows", {"force": True})

        env.callback.assert_called_once()

    def test_regenerate_tick_runs_when_covering_job_only_scans(self, env):
        env.jobs.append(_job(config={"selected_libraries": [], "force_generate": False}))
        schedule = _previews_schedule(env, ["2"], "TV Shows")

        _fire(schedule, ["2"], "TV Shows", {"force": True})

        env.callback.assert_called_once()

    def test_regenerate_tick_skipped_when_covering_job_also_regenerates(self, env):
        env.jobs.append(_job(config={"selected_libraries": [], "force_generate": True}))
        schedule = _previews_schedule(env, ["2"], "TV Shows")

        _fire(schedule, ["2"], "TV Shows", {"force": True})

        env.callback.assert_not_called()

    def test_runs_when_covering_job_is_webhook_or_retry(self, env):
        env.jobs.append(_job(config={"webhook_paths": ["/m/a.mkv"]}))
        env.jobs.append(_job(config={"is_retry": True}))
        schedule = _previews_schedule(env, ["2"], "TV Shows")

        _fire(schedule, ["2"], "TV Shows")

        env.callback.assert_called_once()

    def test_runs_when_all_libraries_job_is_done(self, env):
        env.jobs.append(_job(status=JobStatus.COMPLETED))
        schedule = _previews_schedule(env, ["2"], "TV Shows")

        _fire(schedule, ["2"], "TV Shows")

        env.callback.assert_called_once()

    def test_runs_when_other_kind_scans_all_libraries(self, env):
        env.jobs.append(_job(kind="loudness", config={"selected_libraries": []}))
        env.jobs.append(_job(kind="intro_credits", config={"source": "manual", "libraries": []}))
        schedule = _previews_schedule(env, ["2"], "TV Shows")

        _fire(schedule, ["2"], "TV Shows")

        env.callback.assert_called_once()

    def test_runs_when_all_libraries_job_is_on_another_server(self, env):
        env.jobs.append(_job(server_id="emby-1", config={"selected_libraries": []}))
        schedule = _previews_schedule(env, ["2"], "TV Shows")

        _fire(schedule, ["2"], "TV Shows")

        env.callback.assert_called_once()

    def test_multi_library_runs_when_only_one_is_covered(self, env):
        env.jobs.append(
            _job(library_id="1", library_name="Movies", server_id="plex-1", config={"selected_library_ids": ["1"]})
        )
        schedule = _previews_schedule(env, ["1", "2"], "Movies, TV Shows")

        _fire(schedule, ["1", "2"], "Movies, TV Shows")

        env.callback.assert_called_once()

    def test_multi_library_skipped_when_all_are_covered(self, env):
        env.jobs.append(
            _job(library_name="3 Libraries", server_id="plex-1", config={"selected_library_ids": ["1", "2", "3"]})
        )
        schedule = _previews_schedule(env, ["1", "2"], "Movies, TV Shows")

        _fire(schedule, ["1", "2"], "Movies, TV Shows")

        env.callback.assert_not_called()


class TestIntroCreditsSkip:
    CONFIG = {"job_type": "intro_credits"}

    def _schedule(self, env):
        return env.manager.create_schedule(
            name="Markers",
            cron_expression="0 3 * * 0",
            library_ids=["2"],
            library_name="TV Shows",
            server_id="plex-1",
            config=self.CONFIG,
        )

    def test_skipped_when_all_libraries_markers_job_runs(self, env):
        env.jobs.append(_job(kind="intro_credits", config={"source": "manual", "libraries": []}))
        schedule = self._schedule(env)

        _fire(schedule, ["2"], "TV Shows", self.CONFIG)

        env.create.assert_not_called()
        assert env.manager.get_schedule(schedule["id"])["last_run"] is not None

    def test_skipped_when_same_library_markers_job_runs(self, env):
        libraries = [{"server_id": "plex-1", "library_id": "2"}]
        env.jobs.append(_job(kind="intro_credits", config={"source": "schedule", "libraries": libraries}))
        schedule = self._schedule(env)

        _fire(schedule, ["2"], "TV Shows", self.CONFIG)

        env.create.assert_not_called()

    def test_runs_when_markers_job_is_for_files_or_other_library(self, env):
        env.jobs.append(
            _job(kind="intro_credits", config={"source": "webhook", "libraries": [], "file_paths": ["/t/a.mkv"]})
        )
        other_library = [{"server_id": "plex-1", "library_id": "1"}]
        env.jobs.append(_job(kind="intro_credits", config={"source": "manual", "libraries": other_library}))
        env.jobs.append(_job(kind="previews", config={"selected_libraries": []}))
        schedule = self._schedule(env)

        _fire(schedule, ["2"], "TV Shows", self.CONFIG)

        env.create.assert_called_once()
