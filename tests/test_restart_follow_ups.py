"""A restart keeps a file's Intro & Credits follow-up wherever it keeps the file's preview job.

Three changes meet at the restart: leftover PENDING preview jobs are failed at boot unless Pause all holds them
(``_fail_unrevived_preview_jobs``); a webhook preview job asks for its follow-up in its saved config, and the preview
runner queues it on every start (``INTRO_CREDITS_FOLLOW_UP``); a scheduled Recently Added scan is a preview job the
restart revives over the window it was created for. Together they must never keep a file's previews and drop its
markers, queue a follow-up twice, or turn a revived Recently Added scan into a scan of everything.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

import media_preview_generator.web.jobs as jobs_mod
from media_preview_generator.job_kinds import INTRO_CREDITS_FOLLOW_UP, JOB_KIND_INTRO_CREDITS
from media_preview_generator.web.jobs import JobManager, JobStatus
from media_preview_generator.web.settings_manager import reset_settings_manager

EPISODE = "/data/tv/Show/Season 01/Show - S01E01.mkv"
SUBMIT = "media_preview_generator.markers.triggers.submit_follow_ups"
SCAN = "media_preview_generator.jobs.orchestrator._run_recently_added_multi_server"
FULL_SCAN = "media_preview_generator.jobs.orchestrator._run_full_scan_multi_server"
# The config key a runner writes when a job that never started begins waiting for a slot (web.jobs.SLOT_WAIT_SINCE).
SLOT_WAIT_KEY = "slot_wait_since"


def _ago(hours: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat()


def _rewrite(jm: JobManager, job, **fields) -> None:
    """Set fields jobs.db keeps from a row's first insert (``created_at``) by inserting the row again."""
    for name, value in fields.items():
        setattr(job, name, value)
    jm._storage.delete(job.id)
    jm._storage.upsert(job)


class TestFollowUpsStayWithTheirPreviewJob:
    """The boot requeue alone, with the job starts recorded."""

    @pytest.fixture(autouse=True)
    def _reset_job_manager(self):
        with jobs_mod._job_lock:
            jobs_mod._job_manager = None
        yield
        with jobs_mod._job_lock:
            jobs_mod._job_manager = None

    def _restart(self, config_dir: str, *, preview: dict, follow_up_age_h: float) -> tuple[JobManager, str, str]:
        """Persist a webhook preview job and the follow-up its first start queued, then load them after a restart."""
        os.makedirs(config_dir, exist_ok=True)
        before = JobManager(config_dir=config_dir)
        job = before.create_job(library_name="Sonarr: S01E01", config={"source": "sonarr", "webhook_paths": [EPISODE]})
        follow_up = before.create_job(
            library_name="Intro & Credits: S01E01",
            kind=JOB_KIND_INTRO_CREDITS,
            config={"source": "sonarr", "follows_job_id": job.id, "file_paths": [EPISODE]},
        )
        _rewrite(before, job, **preview)
        _rewrite(before, follow_up, created_at=_ago(follow_up_age_h))

        after = JobManager(config_dir=config_dir)
        with jobs_mod._job_lock:
            jobs_mod._job_manager = after
        return after, job.id, follow_up.id

    def _boot(self, *, paused: bool, auto_requeue: bool = True) -> list[str]:
        from media_preview_generator.web.app import _requeue_interrupted_on_startup

        settings = {"auto_requeue_on_restart": auto_requeue, "requeue_max_age_minutes": 720}
        with (
            patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
            patch("media_preview_generator.web.routes._start_job_async") as boot_start,
            patch("media_preview_generator.markers.job_runner.pass_on_requests_of_unrevived_jobs"),
        ):
            mock_sm.return_value.get.side_effect = lambda key, default=None: settings.get(key, default)
            mock_sm.return_value.processing_paused = paused
            _requeue_interrupted_on_startup("/unused")
        return [c.args[0] for c in boot_start.call_args_list]

    def _resume(self) -> list[str]:
        from media_preview_generator.web.routes.job_runner import resume_running_and_drain_pending

        with patch("media_preview_generator.web.routes.job_runner._start_job_async") as resume_start:
            resume_running_and_drain_pending()
        return [c.args[0] for c in resume_start.call_args_list]

    def test_a_follow_up_older_than_the_window_waits_with_the_preview_job_the_pause_holds(self, tmp_path):
        # Queued when the webhook's job first started during a long pause: both are 20 h old at the restart.
        jm, preview_id, follow_up_id = self._restart(
            str(tmp_path / "config"), preview={"created_at": _ago(20)}, follow_up_age_h=20
        )

        started_at_boot = self._boot(paused=True)

        assert jm.get_job(preview_id).status is JobStatus.PENDING
        assert jm.get_job(follow_up_id).status is JobStatus.PENDING
        assert started_at_boot == [follow_up_id]  # its runner waits for the pause and the preview job
        assert set(self._resume()) == {preview_id, follow_up_id}

    def test_a_follow_up_older_than_its_running_preview_jobs_start_is_revived_with_it(self, tmp_path):
        # The preview job waited 19 h for a slot and was running when the restart came: its age is its start's.
        jm, preview_id, follow_up_id = self._restart(
            str(tmp_path / "config"),
            preview={"created_at": _ago(20), "started_at": _ago(1), "status": JobStatus.RUNNING},
            follow_up_age_h=20,
        )

        started_at_boot = self._boot(paused=False)

        assert started_at_boot == [preview_id, follow_up_id]
        assert jm.get_job(follow_up_id).status is JobStatus.PENDING

    @pytest.mark.parametrize("auto_requeue", [True, False], ids=["too-old", "auto-requeue-off"])
    def test_a_follow_up_goes_with_a_preview_job_the_restart_drops(self, tmp_path, auto_requeue):
        jm, preview_id, follow_up_id = self._restart(
            str(tmp_path / "config"), preview={"created_at": _ago(20)}, follow_up_age_h=20
        )

        assert self._boot(paused=False, auto_requeue=auto_requeue) == []

        assert jm.get_job(preview_id).status is JobStatus.FAILED
        assert jm.get_job(follow_up_id).status is JobStatus.FAILED
        assert self._resume() == []

    def test_a_fresh_follow_up_is_revived_once(self, tmp_path):
        jm, preview_id, follow_up_id = self._restart(
            str(tmp_path / "config"), preview={"created_at": _ago(1)}, follow_up_age_h=1
        )

        assert self._boot(paused=False) == [preview_id, follow_up_id]
        assert jm.unrevived_interrupted_jobs() == []


@pytest.fixture()
def app(tmp_path, monkeypatch):
    """A real app (settings, jobs.db, the preview runner run inline by conftest) with one Plex server."""
    import media_preview_generator.web.job_gate as gate_mod
    import media_preview_generator.web.routes.job_runner as jr_mod
    from media_preview_generator.web.app import create_app

    def reset():
        reset_settings_manager()
        with jobs_mod._job_lock:
            jobs_mod._job_manager = None
        gate_mod.reset_job_gate()
        with jr_mod._inflight_lock:
            jr_mod._inflight_jobs.clear()

    reset()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    plex_cfg = tmp_path / "plex_cfg"
    (plex_cfg / "Media" / "localhost").mkdir(parents=True)
    (config_dir / "settings.json").write_text(
        json.dumps(
            {
                "setup_complete": True,
                "max_concurrent_jobs": 10,
                "media_servers": [
                    {
                        "id": "plex-1",
                        "type": "plex",
                        "name": "Plex",
                        "enabled": True,
                        "url": "http://plex:32400",
                        "auth": {"token": "tok"},
                        "libraries": [{"id": "1", "name": "TV", "remote_paths": ["/data/tv"], "enabled": True}],
                        "output": {"adapter": "plex_bundle", "plex_config_folder": str(plex_cfg)},
                    }
                ],
            }
        )
    )
    (config_dir / "auth.json").write_text(json.dumps({"token": "test-token-12345678"}))
    monkeypatch.setenv("WEB_AUTH_TOKEN", "test-token-12345678")
    monkeypatch.setenv("CONFIG_DIR", str(config_dir))
    reset_settings_manager()
    flask_app = create_app(config_dir=str(config_dir))
    flask_app.config_dir = str(config_dir)
    yield flask_app
    reset()


class TestRestartWhilePausedThenResume:
    """The real start paths: boot requeue while paused, then Resume all."""

    def _restart(self, app, *, age_hours: float) -> JobManager:
        from media_preview_generator.web import app as app_mod

        with jobs_mod._job_lock:
            jobs_mod._job_manager = None
        after = jobs_mod.get_job_manager(app.config_dir)
        for job in after.get_all_jobs():
            job.created_at = _ago(age_hours)  # jobs.db never rewrites created_at: age the loaded rows
        app_mod._requeue_interrupted_on_startup(app.config_dir)
        return after

    def _resume(self) -> None:
        from media_preview_generator.web.routes.job_runner import resume_running_and_drain_pending
        from media_preview_generator.web.settings_manager import get_settings_manager

        get_settings_manager().processing_paused = False
        resume_running_and_drain_pending()

    def test_a_webhook_job_saved_in_its_debounce_queues_its_follow_up_once_across_the_restart_and_resume(self, app):
        from media_preview_generator.web.settings_manager import get_settings_manager

        jm = jobs_mod.get_job_manager()
        job = jm.create_job(
            library_name="Sonarr: S01E01",
            config={
                "source": "sonarr",
                "webhook_paths": [EPISODE],
                "server_id": "plex-1",
                INTRO_CREDITS_FOLLOW_UP: True,
            },
        )
        get_settings_manager().processing_paused = True

        with (
            patch(SUBMIT, return_value=["ic-1"]) as submit,
            patch("media_preview_generator.jobs.orchestrator.run_processing", return_value={"outcome": {}}) as run,
        ):
            after = self._restart(app, age_hours=1)
            assert after.get_job(job.id).status is JobStatus.PENDING  # held by the pause
            run.assert_not_called()
            self._resume()

        submit.assert_called_once()
        kwargs = submit.call_args.kwargs
        assert kwargs["preview_job_id"] == job.id
        assert [item.canonical_path for item in kwargs["items"]] == [EPISODE]
        assert kwargs["pin"] == "plex-1"
        assert INTRO_CREDITS_FOLLOW_UP not in after.get_job(job.id).config
        run.assert_called_once()
        assert run.call_args.args[0].webhook_paths == [EPISODE]

    def test_a_recently_added_scan_held_by_the_pause_runs_its_own_window_on_resume_not_a_full_scan(self, app):
        from media_preview_generator.web.routes.job_runner import _start_recently_added_job_async
        from media_preview_generator.web.settings_manager import get_settings_manager

        get_settings_manager().processing_paused = True
        job_id = _start_recently_added_job_async(
            schedule_id="sched-1", server_id="plex-1", library_ids=["1"], lookback_hours=1.0, library_name="RA: TV"
        )

        scans: list[dict] = []
        with (
            patch(SCAN, side_effect=lambda config, **kw: scans.append(kw) or {"generated": 1}),
            patch(FULL_SCAN, MagicMock(side_effect=AssertionError("a Recently Added job must never scan everything"))),
        ):
            # Older than the revival window: only the pause keeps it (without it the restart fails it).
            after = self._restart(app, age_hours=20)
            assert after.get_job(job_id).status is JobStatus.PENDING
            assert scans == []
            self._resume()

        assert len(scans) == 1
        assert scans[0]["job_id"] == job_id
        assert scans[0]["server_id_filter"] == "plex-1"
        assert scans[0]["library_ids"] == ["1"]
        assert 21.0 <= scans[0]["lookback_hours"] < 21.1  # the hour before it was created, and the 20 since
        assert after.get_job(job_id).status is JobStatus.COMPLETED


class TestAgedFromWhenAJobCouldRun:
    """A job that never started is as old as the moment it could first run, not its ``created_at``: a retry from its
    due time, a follow-up from its preview job's end, any job from when it began waiting for a slot."""

    @pytest.fixture(autouse=True)
    def _reset_job_manager(self):
        with jobs_mod._job_lock:
            jobs_mod._job_manager = None
        yield
        with jobs_mod._job_lock:
            jobs_mod._job_manager = None

    def _restart(self, config_dir: str, jobs: list[dict]) -> tuple[JobManager, list[str]]:
        """Persist the jobs (``created_h`` hours old, ``config``, ``kind``, ``completed_h``), then restart."""
        os.makedirs(config_dir, exist_ok=True)
        before = JobManager(config_dir=config_dir)
        ids = []
        for spec in jobs:
            config = dict(spec.get("config") or {})
            if "follows" in spec:
                config["follows_job_id"] = ids[spec["follows"]]
            job = before.create_job(library_name="j", kind=spec.get("kind", "previews"), config=config)
            fields = {"created_at": _ago(spec["created_h"])}
            if "completed_h" in spec:
                fields.update(status=JobStatus.COMPLETED, started_at=_ago(spec["created_h"]),
                              completed_at=_ago(spec["completed_h"]))  # fmt: skip
            _rewrite(before, job, **fields)
            ids.append(job.id)
        after = JobManager(config_dir=config_dir)
        with jobs_mod._job_lock:
            jobs_mod._job_manager = after
        return after, ids

    @pytest.mark.parametrize(
        ("config", "revived"),
        [
            ({"is_retry": True, "retry_attempt": 1, "scheduled_at": "{1}"}, True),  # due an hour ago
            ({"is_retry": True, "retry_attempt": 1, "scheduled_at": "{20}"}, False),
            ({SLOT_WAIT_KEY: "{1}"}, True),  # revived at a boot an hour ago, still waiting for a slot
            ({SLOT_WAIT_KEY: "{20}"}, False),
            ({}, False),  # nothing says it could run since: its creation
        ],
        ids=["retry-due-lately", "retry-due-long-ago", "slot-wait-lately", "slot-wait-long-ago", "only-created"],
    )
    def test_a_never_started_job_is_aged_from_when_it_could_run(self, tmp_path, config, revived):
        config = {
            k: (_ago(float(v[1:-1])) if isinstance(v, str) and v.startswith("{") else v) for k, v in config.items()
        }
        jm, (job_id,) = self._restart(str(tmp_path / "config"), [{"created_h": 20, "config": config}])
        assert [job.id for job in jm.requeue_interrupted_jobs(max_age_minutes=720)] == ([job_id] if revived else [])

    @pytest.mark.parametrize(("completed_h", "revived"), [(1, True), (20, False)])
    def test_a_follow_up_is_aged_from_its_finished_preview_jobs_end(self, tmp_path, completed_h, revived):
        # The preview job ran for hours (a big batch behind long scans) and finished before the restart: its
        # follow-up only became runnable then.
        jm, (_preview_id, follow_up_id) = self._restart(
            str(tmp_path / "config"),
            [
                {"created_h": 20, "completed_h": completed_h, "config": {"source": "sonarr"}},
                {"created_h": 20, "kind": JOB_KIND_INTRO_CREDITS, "follows": 0, "config": {"source": "sonarr"}},
            ],
        )
        assert [job.id for job in jm.requeue_interrupted_jobs(max_age_minutes=720)] == (
            [follow_up_id] if revived else []
        )


class TestRunnersRecordWhenASlotWaitBegins:
    def test_the_preview_runner_records_it_before_it_asks_for_a_slot(self, app):
        from media_preview_generator.web.routes.job_runner import _start_job_async

        jm = jobs_mod.get_job_manager()
        job = jm.create_job(library_name="Movies", config={})
        seen: list[dict] = []
        gate = MagicMock()
        gate.acquire.side_effect = lambda **kwargs: seen.append(dict(jm.get_job(job.id).config)) or False
        with patch("media_preview_generator.web.job_gate.get_job_gate", return_value=gate):
            _start_job_async(job.id, {})
        (config,) = seen
        waited_from = datetime.fromisoformat(config[SLOT_WAIT_KEY])
        assert abs((datetime.now(UTC) - waited_from).total_seconds()) < 60

    def test_a_job_waiting_longer_than_the_revival_window_behind_a_long_scan_is_revived_after_a_restart(
        self, app, monkeypatch
    ):
        # It began waiting for a slot 13 h ago (a full scan holds every slot) and was still waiting when the app went
        # down: its age at the restart is the downtime, not the 13 h it waited, so it's revived, not failed.
        import media_preview_generator.web.routes.job_runner as jr_mod
        from media_preview_generator.web import app as app_mod

        real_now = datetime.now(UTC)
        clock = {"now": real_now - timedelta(hours=13)}

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock["now"]

        monkeypatch.setattr(jobs_mod, "_now", lambda: clock["now"], raising=False)
        monkeypatch.setattr(jr_mod, "datetime", Clock)
        jm = jobs_mod.get_job_manager()
        job = jm.create_job(library_name="Sonarr: S01E01", config={"source": "sonarr", "webhook_paths": [EPISODE]})
        _rewrite(jm, job, created_at=clock["now"].isoformat())
        revived_after_restart: list[str] = []
        gate = MagicMock()

        def acquire(*, on_wait, **kwargs):
            clock["now"] = real_now  # 13 h later, still waiting behind the scan
            on_wait(4, 4, 3)
            # The app goes down now: a restarted process reads jobs.db as it is at this moment.
            after = JobManager(config_dir=app.config_dir)
            with (
                patch("media_preview_generator.web.settings_manager.get_settings_manager") as sm,
                patch("media_preview_generator.web.routes._start_job_async") as start,
                patch.object(jobs_mod, "_job_manager", after),
            ):
                sm.return_value.get.side_effect = lambda key, default=None: {"requeue_max_age_minutes": 720}.get(
                    key, default
                )
                sm.return_value.processing_paused = False
                app_mod._requeue_interrupted_on_startup(app.config_dir)
            revived_after_restart.extend(c.args[0] for c in start.call_args_list)
            return False

        gate.acquire.side_effect = acquire
        with patch("media_preview_generator.web.job_gate.get_job_gate", return_value=gate):
            jr_mod._start_job_async(job.id, dict(jm.get_job(job.id).config))
        assert revived_after_restart == [job.id]

    def test_reprocess_drops_the_old_jobs_slot_wait(self, app):
        from media_preview_generator.web.routes.api_jobs import reprocess_job

        jm = jobs_mod.get_job_manager()
        job = jm.create_job(library_name="Movies", config={"webhook_paths": [EPISODE], SLOT_WAIT_KEY: _ago(3)})
        jm.complete_job(job.id)
        with app.test_request_context(), patch("media_preview_generator.web.routes.api_jobs._start_job_async"):
            response, status = reprocess_job.__wrapped__(job.id)
        assert status == 201
        assert SLOT_WAIT_KEY not in jm.get_job(response.get_json()["id"]).config
