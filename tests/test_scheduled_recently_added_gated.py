"""Tests for ``_start_recently_added_job_async`` — a scheduled "Recently added" scan is a preview job.

It used to run inline on the APScheduler thread (no gate, no Job row), then on a thread of its own that had no
Files-panel rows, no retries, and after a restart finished green having done nothing (the revival ran it through the
preview runner, which refused it as a webhook job without paths). It now runs through the preview runner
(``_start_job_async``) like every other preview job: the gate, per-file rows, the retry chain for files a server
hasn't indexed yet, and revival after a restart.

These tests pin the contract:
  1. The helper creates a Job with the correct shape.
  2. The job thread acquires the JobGate before invoking the scan and releases it after.
  3. The scan is invoked with the kwargs the operator configured.
  4. Each file's result is a row of the job; files a server hasn't indexed yet get the usual retry.
  5. A restart revives it and runs the scan over the window it was created for.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.web.settings_manager import reset_settings_manager

SCAN = "media_preview_generator.jobs.orchestrator._run_recently_added_multi_server"
EPISODE = "/data/tv/Show/Season 01/Show - S01E01.mkv"


@pytest.fixture(autouse=True)
def _reset_singletons():
    import media_preview_generator.web.job_gate as gate_mod
    import media_preview_generator.web.jobs as jobs_mod
    import media_preview_generator.web.routes.job_runner as jr_mod

    def reset():
        reset_settings_manager()
        with jobs_mod._job_lock:
            jobs_mod._job_manager = None
        gate_mod.reset_job_gate()
        with jr_mod._inflight_lock:
            jr_mod._inflight_jobs.clear()

    reset()
    yield
    reset()


@pytest.fixture()
def app(tmp_path, monkeypatch):
    from media_preview_generator.web.app import create_app

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    plex_cfg = tmp_path / "plex_cfg"
    (plex_cfg / "Media" / "localhost").mkdir(parents=True)
    (config_dir / "settings.json").write_text(
        json.dumps(
            {
                "setup_complete": True,
                "max_concurrent_jobs": 10,
                "webhook_retry_count": 3,
                "webhook_retry_delay": 30,
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
                    },
                    {
                        "id": "jf-1",
                        "type": "jellyfin",
                        "name": "Jellyfin",
                        "enabled": True,
                        "url": "http://jf:8096",
                        "auth": {"method": "api_key", "api_key": "k"},
                        "libraries": [{"id": "2", "name": "TV", "remote_paths": ["/data/tv"], "enabled": True}],
                    },
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
    return flask_app


def _start(**kwargs):
    from media_preview_generator.web.routes.job_runner import _start_recently_added_job_async

    params = {
        "schedule_id": "sched-1",
        "server_id": None,
        "library_ids": None,
        "lookback_hours": 1.0,
        "library_name": "Recently added: all libraries",
    }
    params.update(kwargs)
    return _start_recently_added_job_async(**params)


def _jm():
    from media_preview_generator.web.jobs import get_job_manager

    return get_job_manager()


class TestStartRecentlyAddedJobAsync:
    def test_creates_job_and_runs_the_scan_with_the_schedules_settings(self, app):
        """The Job row carries ``config.source = "scheduled_recently_added"`` (the "Scheduled scan" pill) and the
        scan gets what the schedule set, plus the job's callbacks so it's visible, cancellable and pausable."""
        scans: list[tuple] = []

        with patch(SCAN, side_effect=lambda config, **kw: scans.append((config, kw)) or {}):
            job_id = _start(
                server_id="plex-1", library_ids=["2"], lookback_hours=2.0, library_name="Recently added: TV"
            )

        job = _jm().get_job(job_id)
        assert job.config["source"] == "scheduled_recently_added"
        assert job.config["parent_schedule_id"] == "sched-1"
        assert job.library_name == "Recently added: TV"
        assert job.status.value == "completed"
        (config, kwargs) = scans[0]
        assert len(scans) == 1
        assert kwargs["server_id_filter"] == "plex-1"
        assert kwargs["library_ids"] == ["2"]
        assert 2.0 <= kwargs["lookback_hours"] < 2.1
        assert kwargs["job_id"] == job_id
        for name in ("progress_callback", "cancel_check", "pause_check", "worker_callback", "on_dispatch_start"):
            assert callable(kwargs[name]), name
        assert callable(kwargs["worker_pool_callback"])  # pool reconcile, which the old runner never did

    def test_acquires_gate_before_running_scan_and_releases_at_the_admitted_priority(self, app):
        call_log: list[str] = []
        acquired_at: list[int] = []
        released_at: list[int] = []
        gate = MagicMock()
        gate.acquire.side_effect = lambda **kw: acquired_at.append(kw["priority"]) or call_log.append("acquire") or True
        gate.release.side_effect = lambda priority: released_at.append(priority) or call_log.append("release")

        with (
            patch(SCAN, side_effect=lambda *a, **k: call_log.append("scan") or {}),
            patch("media_preview_generator.web.job_gate.get_job_gate", return_value=gate),
        ):
            _start()

        assert call_log == ["acquire", "scan", "release"]
        assert acquired_at == released_at == [1]  # High: the default incoming_job_priority

    @pytest.mark.parametrize(("passed_priority", "expected"), [(1, 1), (2, 2), (3, 3)])
    def test_priority_forwarded_to_created_job(self, app, passed_priority, expected):
        """The schedule's priority reaches the Job (issue #259); an explicit Normal survives the #285 fallback."""
        with patch(SCAN, return_value={}):
            job_id = _start(priority=passed_priority)
        assert _jm().get_job(job_id).priority == expected

    @pytest.mark.parametrize(("configured", "expected"), [(None, 1), (1, 1), (2, 2), (3, 3)])
    def test_unpinned_schedule_inherits_incoming_job_priority(self, app, configured, expected):
        """A schedule with no priority of its own follows the "Incoming job priority" setting (issue #285)."""
        from media_preview_generator.web.settings_manager import get_settings_manager

        if configured is not None:
            get_settings_manager().set("incoming_job_priority", configured)
        with patch(SCAN, return_value={}):
            job_id = _start()
        assert _jm().get_job(job_id).priority == expected

    @pytest.mark.parametrize(("server_id", "expected_filter"), [("plex-1", "plex-1"), ("jf-1", "jf-1"), (None, None)])
    def test_pinned_server_sets_config_server_id_filter(self, app, server_id, expected_filter):
        """The per-item publish target comes from ``config.server_id_filter`` (``resolve_per_item_pin``): a pinned
        scan publishes to that server only (issue #259); an unpinned one keeps fanning out."""
        captured: dict = {}
        with patch(SCAN, side_effect=lambda config, **kw: captured.setdefault("config", config) and {}):
            _start(server_id=server_id)
        assert captured["config"].server_id_filter == expected_filter

    def test_cancel_during_gate_wait_skips_scan_and_does_not_release(self, app):
        gate = MagicMock()
        gate.acquire.return_value = False
        scan = MagicMock(return_value={})
        with patch(SCAN, scan), patch("media_preview_generator.web.job_gate.get_job_gate", return_value=gate):
            job_id = _start()
        scan.assert_not_called()
        gate.release.assert_not_called()
        assert _jm().get_job(job_id).status.value == "cancelled"

    @pytest.mark.parametrize(
        "outcome",
        [{"generated": 0}, {"generated": 1, "failed": 1}, {"generated": 1, "skipped_not_indexed": 1}],
        ids=["nothing-else", "a-file-failed", "a-file-not-indexed"],
    )
    def test_enumeration_warnings_reach_the_job_whatever_else_it_reports(self, app, outcome):
        def scan(config, **kwargs):
            kwargs["warnings_out"].append("Recently-added enumeration failed for 1 server(s) (Jellyfin)")
            return dict(outcome)

        with patch(SCAN, side_effect=scan):
            job_id = _start()
        job = _jm().get_job(job_id)
        assert job.status.value == "completed"
        assert "Recently-added enumeration failed" in (job.error or "")


class TestFilesAndRetries:
    def test_each_files_result_is_a_row_of_the_job(self, app):
        from media_preview_generator.processing.generator import ProcessingResult, _notify_file_result

        def scan(config, **kwargs):
            _notify_file_result(EPISODE, ProcessingResult.GENERATED, "", "[CPU 1]")
            return {"generated": 1}

        with patch(SCAN, side_effect=scan):
            job_id = _start()
        rows = _jm().get_file_results(job_id)
        assert [(r["file"], r["outcome"]) for r in rows] == [(EPISODE, "generated")]

    @pytest.mark.parametrize("pin", [None, "jf-1"])
    def test_a_file_a_server_hasnt_indexed_yet_gets_the_usual_retry(self, app, pin):
        from media_preview_generator.jobs import orchestrator
        from media_preview_generator.processing.generator import ProcessingResult, _notify_file_result

        waiting = [{"server_id": "jf-1", "name": "Jellyfin", "status": "skipped_not_indexed"}]
        retried: list[dict] = []
        real_run_processing = orchestrator.run_processing

        def scan(config, **kwargs):
            _notify_file_result(EPISODE, ProcessingResult.SKIPPED_NOT_INDEXED, "not indexed yet", "", servers=waiting)
            return {"skipped_not_indexed": 1}

        def run_processing(config, selected_gpus, **kwargs):
            if not config.webhook_paths:
                return real_run_processing(config, selected_gpus, **kwargs)
            # The retry: it runs the waiting file as a path, publishing where the scan did.
            retried.append({"paths": list(config.webhook_paths), "pin": config.server_id_filter})
            parent = _jm().get_job(kwargs["job_id"]).config["parent_job_id"]
            _jm().record_file_result(parent, EPISODE, "generated", "", "[CPU 1]")
            return {"outcome": {"generated": 1}}

        with (
            patch(SCAN, side_effect=scan),
            patch("media_preview_generator.jobs.orchestrator.run_processing", side_effect=run_processing),
            patch("media_preview_generator.processing.retry_queue.BACKOFF_SCHEDULE", [1, 1, 1, 1, 1]),
        ):
            job_id = _start(server_id=pin)

        assert retried == [{"paths": [EPISODE], "pin": pin}]
        children = [j for j in _jm().get_all_jobs() if (j.config or {}).get("parent_job_id") == job_id]
        assert len(children) == 1 and children[0].config["is_retry"] is True
        assert children[0].config.get("server_id") == pin
        assert _jm().get_job(job_id).status.value == "completed"

    @pytest.mark.parametrize(
        ("origin", "ids", "retry_hints"),
        [
            ("jf-1", {"jf-1": "7"}, {EPISODE: {"jf-1": "7"}}),  # its previews went to Jellyfin only; so does the retry
            ("jf-1", {"plex-1": "3", "jf-1": "7"}, {EPISODE: {"jf-1": "7", "plex-1": "3"}}),  # the origin leads
            ("plex-1", {"plex-1": "3"}, None),  # a Plex file fans out, first run and retry alike
        ],
        ids=["jellyfin", "jellyfin-also-on-plex", "plex"],
    )
    def test_an_unpinned_scans_retry_publishes_where_the_file_came_from(
        self, app, monkeypatch, origin, ids, retry_hints
    ):
        """``resolve_per_item_pin`` scopes a file listed from Emby or Jellyfin to that server; its retry runs it as
        a path, so the job keeps the file's item ids with the server it came from first, as a vendor webhook's."""
        from media_preview_generator.jobs import orchestrator
        from media_preview_generator.processing.generator import ProcessingResult, _notify_file_result
        from media_preview_generator.processing.types import ProcessableItem

        def processor_for(_server_type):
            processor = MagicMock()
            processor.scan_recently_added.side_effect = lambda cfg, *, lookback_hours, library_ids: iter(
                [ProcessableItem(EPISODE, origin, dict(ids))] if cfg.id == origin else []
            )
            return processor

        waiting = [{"server_id": origin, "name": origin, "status": "skipped_not_indexed"}]

        def dispatch(items, **kwargs):
            _notify_file_result(EPISODE, ProcessingResult.SKIPPED_NOT_INDEXED, "not indexed yet", "", servers=waiting)
            return {"skipped_not_indexed": 1}

        retried: list[dict] = []
        real_run_processing = orchestrator.run_processing

        def run_processing(config, selected_gpus, **kwargs):
            if not config.webhook_paths:
                return real_run_processing(config, selected_gpus, **kwargs)
            retried.append({"hints": config.webhook_item_id_hints, "pin": config.server_id_filter})
            parent = _jm().get_job(kwargs["job_id"]).config["parent_job_id"]
            _jm().record_file_result(parent, EPISODE, "generated", "", "[CPU 1]")
            return {"outcome": {"generated": 1}}

        monkeypatch.setattr("media_preview_generator.processing.get_processor_for", processor_for)
        with (
            patch.object(orchestrator, "_dispatch_processable_items", side_effect=dispatch),
            patch("media_preview_generator.jobs.orchestrator.run_processing", side_effect=run_processing),
            patch("media_preview_generator.processing.retry_queue.BACKOFF_SCHEDULE", [1, 1, 1, 1, 1]),
        ):
            _start()

        assert retried == [{"hints": retry_hints, "pin": None}]
        if retry_hints:
            assert list(retried[0]["hints"][EPISODE]) == list(retry_hints[EPISODE])  # the origin is the first key


class TestOneWaitingScanPerSchedule:
    """A schedule's tick doesn't queue a second scan while its last one hasn't started: that one's window already runs
    up to when it starts, so a second would list the same files again."""

    START = "media_preview_generator.web.routes.job_runner._start_job_async"

    def _recently_added_jobs(self):
        return [j for j in _jm().get_all_jobs() if (j.config or {}).get("source") == "scheduled_recently_added"]

    def test_a_tick_while_the_last_scan_waits_reuses_it(self, app):
        with patch(self.START) as start:
            first = _start(lookback_hours=1.0)
            second = _start(lookback_hours=1.0)
        assert second == first
        assert [j.id for j in self._recently_added_jobs()] == [first]
        assert [c.args[0] for c in start.call_args_list] == [first]

    def test_a_longer_lookback_widens_the_waiting_scans_window(self, app):
        with patch(self.START):
            first = _start(lookback_hours=1.0)
            assert _start(lookback_hours=6.0) == first
        # Counted back from the waiting job's creation: its window now starts 6 h before this tick.
        assert 5.99 <= _jm().get_job(first).config["lookback_hours"] <= 6.0

    def test_a_tick_while_the_scan_waits_at_the_gate_widens_the_window_it_lists(self, app):
        # The runner reads the window after the gate, from the saved job: a tick that widened it while the scan waited
        # for a slot isn't lost (the runner is real; only the gate and the scan itself are faked).
        scans: list[dict] = []
        ticks: list[str] = []
        gate = MagicMock()

        def acquire(**kwargs):
            if not ticks:
                ticks.append(_start(lookback_hours=6.0))  # the schedule fires again while this one waits
            return True

        gate.acquire.side_effect = acquire
        with (
            patch(SCAN, side_effect=lambda config, **kw: scans.append(kw) or {}),
            patch("media_preview_generator.web.job_gate.get_job_gate", return_value=gate),
        ):
            first = _start(lookback_hours=1.0)
        assert ticks == [first]
        (scan,) = scans
        assert scan["job_id"] == first
        assert 6.0 <= scan["lookback_hours"] < 6.1

    @pytest.mark.parametrize("state", ["started", "finished", "other-schedule"])
    def test_a_new_scan_is_queued_otherwise(self, app, state):
        with patch(self.START):
            first = _start(lookback_hours=1.0)
            if state == "started":
                _jm().start_job(first)
            elif state == "finished":
                _jm().start_job(first)
                _jm().complete_job(first)
            second = _start(lookback_hours=1.0, schedule_id="sched-2" if state == "other-schedule" else "sched-1")
        assert second != first
        assert len(self._recently_added_jobs()) == 2


class TestRestart:
    def _restart(self, app, age_hours):
        import media_preview_generator.web.jobs as jobs_mod
        from media_preview_generator.web import app as app_mod

        with jobs_mod._job_lock:
            jobs_mod._job_manager = None
        after = jobs_mod.get_job_manager(app.config_dir)
        stamp = (datetime.now(UTC) - timedelta(hours=age_hours)).isoformat()
        for job in after.get_all_jobs():
            job.created_at = stamp  # jobs.db never rewrites created_at: age the loaded rows as a wait would
        app_mod._requeue_interrupted_on_startup(app.config_dir)
        return after

    def test_a_revived_scan_runs_again_over_the_window_it_was_created_for(self, app):
        """Revival runs it through the preview runner again: before, that runner refused it as a webhook job without
        paths and it ended green having done nothing. Its window still starts where the scheduled run's did."""
        from media_preview_generator.web.jobs import JobStatus

        gate = MagicMock()
        gate.acquire.return_value = False  # the restart comes while it waits for a slot
        with (
            patch(SCAN, return_value={}) as never,
            patch("media_preview_generator.web.job_gate.get_job_gate", return_value=gate),
        ):
            job_id = _start(server_id="jf-1", library_ids=["2"], lookback_hours=1.0)
        never.assert_not_called()
        _jm().get_job(job_id).status = JobStatus.PENDING  # as a restart finds it, never admitted
        _jm()._persist_job(_jm().get_job(job_id))

        scans: list[dict] = []
        with patch(SCAN, side_effect=lambda config, **kw: scans.append(kw) or {"generated": 1}):
            after = self._restart(app, age_hours=3)

        assert len(scans) == 1
        assert scans[0]["job_id"] == job_id
        assert scans[0]["server_id_filter"] == "jf-1"
        assert scans[0]["library_ids"] == ["2"]
        assert 4.0 <= scans[0]["lookback_hours"] < 4.1  # the hour before it was created, and the 3 hours since
        job = after.get_job(job_id)
        assert job.status is JobStatus.COMPLETED
        assert job.progress.outcome.get("generated") == 1


class TestTheWindow:
    """The scan lists from the schedule's window before the job was created up to when it runs, and no wider."""

    @pytest.fixture
    def listed_hours(self, monkeypatch):
        """The window each server's real Recently Added listing is asked for (the listing itself returns nothing)."""
        seen: list[float] = []

        def processor_for(_server_type):
            processor = MagicMock()
            processor.scan_recently_added.side_effect = lambda cfg, *, lookback_hours, library_ids: (
                seen.append(lookback_hours) or iter(())
            )
            return processor

        monkeypatch.setattr("media_preview_generator.processing.get_processor_for", processor_for)
        return seen

    def test_a_run_that_starts_at_once_lists_exactly_its_window(self, app, listed_hours):
        _start(lookback_hours=1.0)
        assert listed_hours and all(1.0 <= h < 1.01 for h in listed_hours), listed_hours

    @pytest.mark.parametrize("lookback", [1.5, 24.0])
    def test_a_window_is_not_rounded(self, app, listed_hours, lookback):
        _start(lookback_hours=lookback)
        assert listed_hours and all(lookback <= h < lookback + 0.01 for h in listed_hours), listed_hours

    def test_a_run_admitted_hours_after_it_was_created_lists_back_to_before_it(self, app, listed_hours, monkeypatch):
        """Waiting for a slot (a long scan holding it) doesn't cost the files added in the hour before the schedule
        fired."""
        from media_preview_generator.jobs import orchestrator

        later = {"by": timedelta(0)}
        monkeypatch.setattr(orchestrator, "_utcnow", lambda: datetime.now(UTC) + later["by"])
        gate = MagicMock()
        gate.acquire.side_effect = lambda **kw: later.update(by=timedelta(hours=3)) or True
        with patch("media_preview_generator.web.job_gate.get_job_gate", return_value=gate):
            _start(lookback_hours=1.0)
        assert listed_hours and all(4.0 <= h < 4.01 for h in listed_hours), listed_hours

    def test_a_full_scan_config_carrying_a_lookback_stays_a_full_scan(self, app):
        from media_preview_generator.web.routes.job_runner import _start_job_async

        with (
            patch("media_preview_generator.jobs.orchestrator._run_full_scan_multi_server", return_value={}) as full,
            patch(SCAN) as scan,
        ):
            job = _jm().create_job(library_name="TV", config={"lookback_hours": 2.0, "library_ids": ["2"]})
            _start_job_async(job.id, dict(job.config))
        scan.assert_not_called()
        full.assert_called_once()
