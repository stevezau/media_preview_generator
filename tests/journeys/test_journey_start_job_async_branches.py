"""TEST_AUDIT P1.7 — _start_job_async branch coverage.

The TOP bug-rate function in the repo (4 fixes in 90 days, ZERO direct
tests). Each test below drives a real Job through ``_start_job_async``
and asserts on the exact kwargs forwarded to the orchestrator's
``run_processing`` (the seam where the past bugs landed). External
boundaries (Plex API, FFmpeg, vendor publishers) are mocked; the Flask
app, the JobManager, and ``_start_job_async`` itself run for real.

Branches covered:
  - happy path: orchestrator ``run_processing`` actually invoked with
    the right ``Config`` (legacy plex view) — the wiring still works.
  - retry-spawn branch: ``skipped_file_not_found > 0`` produces a child
    retry job with ``is_retry=True`` and ``retry_attempt=1``.
  - ``regenerate=True`` propagation via overrides → the per-job
    ``Config.regenerate_thumbnails`` flag flips on.
  - ``webhook_item_id_hints`` propagation: the hints land on the
    Config object that ``run_processing`` receives.
  - ``server_id`` filter propagation: Config.server_id_filter set to
    that exact id (so downstream dispatch only fans out to one server).

Each test asserts ``run_processing.call_args.kwargs`` (or
``call_args.args``) on the SUT-controlled values — never just
``called_once()``. Asserting only the call_count would have hidden the
D34 dispatcher → ``process_canonical_path`` regression for months.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.web.app import create_app
from media_preview_generator.web.settings_manager import reset_settings_manager

pytestmark = pytest.mark.journey


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Each test starts with a fresh settings + job + scheduler singleton."""
    reset_settings_manager()
    import media_preview_generator.web.jobs as jobs_mod
    import media_preview_generator.web.scheduler as sched_mod
    import media_preview_generator.web.webhooks as wh_mod

    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    with sched_mod._schedule_lock:
        sched_mod._schedule_manager = None
    wh_mod._recent_dispatches.clear()
    wh_mod._pending_batches.clear()
    for t in list(wh_mod._pending_timers.values()):
        try:
            t.cancel()
        except Exception:
            pass
    wh_mod._pending_timers.clear()
    yield
    reset_settings_manager()
    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    with sched_mod._schedule_lock:
        if sched_mod._schedule_manager is not None:
            try:
                sched_mod._schedule_manager.stop()
            except Exception:
                pass
            sched_mod._schedule_manager = None
    wh_mod._recent_dispatches.clear()
    wh_mod._pending_batches.clear()
    for t in list(wh_mod._pending_timers.values()):
        try:
            t.cancel()
        except Exception:
            pass
    wh_mod._pending_timers.clear()


@pytest.fixture()
def app(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("WEB_AUTH_TOKEN", "test-token-12345678")
    settings_path = config_dir / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "setup_complete": True,
                "webhook_enabled": True,
                "webhook_delay": 0,
                "webhook_retry_count": 3,
                "webhook_retry_delay": 30,
                # Retry chains re-enter _start_job_async from inside a job
                # that already holds a JobGate slot. In production each
                # attempt gets its own daemon thread, so the parent's slot
                # is released while the child waits; under the conftest
                # sync-thread shim the whole chain runs nested on ONE
                # thread and every attempt holds a slot simultaneously.
                # A 3-attempt chain therefore needs 3 concurrent slots —
                # more than the default cap of 3 leaves to normal-priority
                # work once one slot is reserved for high priority.
                "max_concurrent_jobs": 10,
                "media_servers": [
                    {
                        "id": "plex-1",
                        "type": "plex",
                        "name": "Plex Main",
                        "enabled": True,
                        "url": "http://plex:32400",
                        "auth": {"token": "tok"},
                        "libraries": [{"id": "1", "name": "Movies", "enabled": True}],
                        "output": {"adapter": "plex_bundle", "plex_config_folder": str(tmp_path / "plex_cfg")},
                    },
                    {
                        "id": "emby-1",
                        "type": "emby",
                        "name": "Emby Spare",
                        "enabled": True,
                        "url": "http://emby:8096",
                        "auth": {"method": "api_key", "api_key": "k"},
                        "libraries": [{"id": "2", "name": "Movies", "enabled": True}],
                        "output": {"adapter": "emby_sidecar"},
                    },
                ],
            }
        )
    )
    auth_path = config_dir / "auth.json"
    auth_path.write_text(json.dumps({"token": "test-token-12345678"}))
    # Make plex_config_folder pass _validate_plex_config — needs Media/localhost.
    (tmp_path / "plex_cfg" / "Media" / "localhost").mkdir(parents=True, exist_ok=True)
    # CONFIG_DIR is pointed at this folder only once its settings.json exists: a thread still finishing the
    # previous test can create the settings singleton at any moment, and one created for an empty folder
    # would be kept by create_app (same config dir) with none of these settings.
    monkeypatch.setenv("CONFIG_DIR", str(config_dir))
    reset_settings_manager()
    return create_app(config_dir=str(config_dir))


def _wait_for(predicate, timeout=3.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# Branch 1: happy path — _start_job_async actually invokes run_processing
# ---------------------------------------------------------------------------


class TestStartJobAsyncHappyPath:
    """Real job → real ``_start_job_async`` → orchestrator ``run_processing``
    invoked with a real Config carrying the legacy Plex view derived from
    ``media_servers[0]``. This is the bare-bones contract the function has
    promised since day 1. No retry, no overrides, no pin."""

    def test_basic_dispatch_invokes_run_processing_with_real_config(self, app, tmp_path):
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        captured: dict = {}

        def fake_run_processing(config, selected_gpus, **kwargs):
            captured["config"] = config
            captured["selected_gpus"] = list(selected_gpus or [])
            captured["kwargs"] = dict(kwargs)
            return {"outcome": {"generated": 0}}

        with (
            app.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=fake_run_processing,
            ),
        ):
            job = get_job_manager().create_job(library_name="Movies", config={})
            _start_job_async(job.id, config_overrides=None)

        assert "config" in captured, "run_processing was never invoked — _start_job_async wiring broken"

        # Legacy Plex view derived from media_servers[0] — confirms
        # derive_legacy_plex_view actually ran on the path through
        # _start_job_async.
        assert captured["config"].plex_url == "http://plex:32400", (
            f"plex_url should be hydrated from media_servers[0].url; got {captured['config'].plex_url!r}. "
            f"A regression that stops calling derive_legacy_plex_view would leave it empty/None."
        )
        assert captured["config"].plex_token == "tok", (
            f"plex_token should be hydrated from media_servers[0].auth.token; got {captured['config'].plex_token!r}"
        )

        # Critical kwarg: job_id must be threaded all the way through so
        # the dispatcher can route worker outcomes back to this job.
        assert captured["kwargs"].get("job_id") == job.id, (
            f"run_processing must receive job_id={job.id!r} so dispatcher can attribute worker outcomes; "
            f"got {captured['kwargs'].get('job_id')!r}"
        )

        # Cancel + pause checks must be wired so the job is interruptible.
        assert callable(captured["kwargs"].get("cancel_check")), (
            "cancel_check callable must be passed to run_processing; otherwise cancel API can't stop the job"
        )
        assert callable(captured["kwargs"].get("pause_check")), (
            "pause_check callable must be passed to run_processing; otherwise pause API can't quiesce dispatch"
        )

        # Job ended in a terminal state (not stuck pending).
        final = get_job_manager().get_job(job.id)
        assert final is not None
        assert final.status.value in ("completed", "failed", "cancelled"), (
            f"Job must reach a terminal state after run_processing returns; got status={final.status.value!r}"
        )

    def test_job_flips_to_running_when_gate_acquired_not_at_thread_start(self, app, tmp_path):
        """Status transitions must be truthful:

        - PENDING = queued at the concurrency gate, waiting retry
          backoff, or blocked by global pause. Job thread may exist
          but is not doing user-initiated work.
        - RUNNING = slot acquired. Library enumeration, webhook path
          resolution, and dispatch all count as "running" because
          they are real work the user initiated.

        Earlier iterations of this contract flip-flopped: (a) pre-fix
        eagerly flipped to RUNNING at thread-spawn (30 webhook jobs
        all claimed running simultaneously); (b) over-correction
        delayed RUNNING until dispatch-start (a large TV library scan
        spent 30-120s showing PENDING while actively querying Plex).

        Current contract: RUNNING fires right after the concurrency
        gate admits the thread. That's the point where "the job is
        definitely doing something." Tested here by mocking
        ``run_processing`` to record status while running — the job
        must already be RUNNING by then (gate fires first).
        """
        from media_preview_generator.web.jobs import JobStatus, get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        status_snapshots: list[str] = []

        def fake_run_processing(config, selected_gpus, **kwargs):
            # Snapshot status as seen AT the moment run_processing is
            # invoked. By then the gate has admitted us AND
            # start_job has flipped the status — so we must see
            # RUNNING here, NOT PENDING.
            jm = get_job_manager()
            for jid in list(jm._jobs.keys()):
                status_snapshots.append(jm._jobs[jid].status.value)
            return {"outcome": {"generated": 0}}

        with (
            app.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=fake_run_processing,
            ),
        ):
            job = get_job_manager().create_job(library_name="Movies", config={})
            _start_job_async(job.id, config_overrides=None)

        assert status_snapshots, "run_processing never ran — test setup broken"
        # Must be RUNNING by the time run_processing is entered (gate
        # was passed + start_job fired just before). A PENDING value
        # here means the gate-acquire isn't calling start_job and the
        # user would see "Pending" during library enumeration —
        # the exact regression this pins.
        assert all(s == JobStatus.RUNNING.value for s in status_snapshots), (
            f"Job must be RUNNING by the time run_processing is entered "
            f"(gate-acquire transitions PENDING → RUNNING). "
            f"Status snapshots during run_processing: {status_snapshots}. "
            f"A PENDING value here means a large-library scan would sit "
            f"at 'Pending' for 30-120s while actively querying — the "
            f"exact UX regression of job 0c5d65af."
        )

    def test_multi_server_dispatch_fires_on_dispatch_start_when_items_flow(self, app, tmp_path):
        """Regression pin: the multi-server full-scan path must call
        ``on_dispatch_start`` so the job transitions PENDING → RUNNING.

        Live bug (job 91c20505): the webhook / legacy-plex-phase paths
        call the hook via ``_dispatch_items`` (orchestrator.py ~1956),
        but the newer multi-server path uses
        ``_dispatch_processable_items`` which had no hook invocation.
        Result: a Plex full-scan processed 5071/9993 items while the
        DB still showed ``status='pending'`` and ``started_at=None``.

        Test strategy: drive ``_dispatch_processable_items`` directly
        with a synthetic item and an ``on_dispatch_start`` spy, and
        confirm the spy was called. A unit-shaped test because
        wrapping it in a full run_processing integration would require
        mocking three enumeration layers; the direct test pins the
        single contract that matters — the hook fires when items flow.
        """
        from media_preview_generator.jobs.orchestrator import _dispatch_processable_items
        from media_preview_generator.processing.types import ProcessableItem
        from media_preview_generator.servers.base import ServerType

        fake_server_cfg = MagicMock()
        fake_server_cfg.id = "plex-1"
        fake_server_cfg.type = ServerType.PLEX
        fake_server_cfg.name = "Plex Main"

        fake_item = ProcessableItem(
            canonical_path="/fake/Movies/Stub (2020).mkv",
            server_id="plex-1",
            item_id_by_server={"plex-1": "42"},
        )

        def fake_process(canonical_path, config, registry, **kwargs):
            # Terminal SKIPPED so the checking stage records it inline and the
            # dispatch completes (the hook we assert on fires at dispatch start
            # regardless; the run must return for the assertion to be reached).
            from media_preview_generator.processing.multi_server import MultiServerStatus

            return SimpleNamespace(
                canonical_path=canonical_path,
                status=MultiServerStatus.SKIPPED,
                publishers=[],
                message="",
            )

        hook_calls: list[None] = []

        def hook():
            hook_calls.append(None)

        # A realistic config (not a bare MagicMock): the unified dispatcher
        # reads gpu_threads/cpu_threads/scan_workers/regenerate_thumbnails and
        # a MagicMock's auto-attributes break int()/branching.
        fake_config = SimpleNamespace(
            gpu_threads=0,
            cpu_threads=1,
            scan_workers=0,
            regenerate_thumbnails=False,
            server_id_filter=None,
            working_tmp_folder=str(tmp_path),
        )

        with (
            app.app_context(),
            patch(
                "media_preview_generator.processing.multi_server.process_canonical_path",
                side_effect=fake_process,
            ),
        ):
            _dispatch_processable_items(
                [(fake_server_cfg, fake_item)],
                config=fake_config,
                registry=MagicMock(),
                selected_gpus=[],
                on_dispatch_start=hook,
                label="full scan",
            )

        assert len(hook_calls) == 1, (
            f"on_dispatch_start must be called exactly once when items flow through the "
            f"multi-server dispatch path; got {len(hook_calls)} calls. "
            f"Regression of live bug on job 91c20505 (Plex 'Movies' full-scan showed "
            f"status=pending with 5071/9993 items processed)."
        )


# ---------------------------------------------------------------------------
# Branch 2: skipped_file_not_found triggers a retry-job spawn (commit e31d051)
# ---------------------------------------------------------------------------


class TestStartJobAsyncRetryBranch:
    """Pin the retry-spawn contract that landed in commit e31d051.

    When ``run_processing`` returns ``outcome.skipped_file_not_found > 0``
    AND the job has webhook_paths AND retry budget remains, a CHILD job
    must be created via ``_spawn_retry_job`` with:
      - ``is_retry=True``
      - ``retry_attempt=1``
      - ``parent_job_id=<original>``
      - ``webhook_paths`` matching the parent's

    Without coverage here, a regression that drops the retry path silently
    abandons every webhook for files mid-copy by Plex (a common Sonarr +
    fast-link race). Asserts on the SHAPE of the child job + assertions
    that the retry was scheduled (not fired immediately, not lost)."""

    def test_skipped_file_not_found_spawns_child_retry_job(self, app, tmp_path):
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        run_calls: list[dict] = []

        # First call: file not found on disk (Plex returned a stale path).
        # Second call (retry): treat as the second invocation — pretend
        # everything generated, so we don't recurse forever. _start_job_async
        # spawns a *new* run_job thread for the retry; the synchronous
        # threading shim runs it inline, so both invocations happen during
        # this test.
        def fake_run_processing(config, selected_gpus, **kwargs):
            run_calls.append(
                {
                    "webhook_paths": list(getattr(config, "webhook_paths", []) or []),
                    "is_retry_in_config": kwargs.get("job_id"),
                }
            )
            if len(run_calls) == 1:
                # Parent job: Plex returned a stale path for the file.
                # Trigger the not-found-on-disk branch.
                jm = get_job_manager()
                job_id = kwargs.get("job_id")
                # Simulate the worker's record_file_result for the missing file.
                jm.record_file_result(
                    job_id,
                    "/data/tv/Show/S01E01.mkv",
                    "skipped_file_not_found",
                    "file gone",
                    "[GPU 0]",
                )
                return {
                    "outcome": {"skipped_file_not_found": 1, "generated": 0, "failed": 0},
                    "webhook_resolution": {
                        "unresolved_paths": [],
                        "skipped_paths": [],
                        "resolved_count": 1,
                        "total_paths": 1,
                        "path_hints": [],
                    },
                }
            # Retry path: write a fresh "resolved" row on the parent's
            # JSONL so the retry-decision scan in job_runner doesn't see
            # the stale skipped_file_not_found row and re-spawn forever.
            # Real workers write a fresh row on each dispatch; this fake
            # has to do the same. Retry children write to the parent's
            # JSONL via the _file_result_cb redirect.
            jm = get_job_manager()
            parent_id = (jm.get_job(kwargs.get("job_id")).config or {}).get("parent_job_id") or kwargs.get("job_id")
            jm.record_file_result(
                parent_id,
                "/data/tv/Show/S01E01.mkv",
                "generated",
                "",
                "[GPU 0]",
            )
            return {
                "outcome": {"generated": 1},
                "webhook_resolution": {
                    "unresolved_paths": [],
                    "skipped_paths": [],
                    "resolved_count": 1,
                    "total_paths": 1,
                    "path_hints": [],
                },
            }

        # Shrink the retry backoff so the test isn't blocked for 30s
        # waiting for the retry job's countdown timer to elapse. The
        # production code clamps webhook_retry_delay to >= 10s and uses
        # BACKOFF_SCHEDULE for the actual wait.
        with (
            app.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=fake_run_processing,
            ),
            # Suppress the partial-scan call (would hit the network).
            patch(
                "media_preview_generator.plex_client.trigger_plex_partial_scan",
                return_value=[],
            ),
            # Replace the slow backoff schedule with 1s entries so the
            # retry's countdown loop completes near-instantly.
            patch(
                "media_preview_generator.processing.retry_queue.BACKOFF_SCHEDULE",
                [1, 1, 1, 1, 1],
            ),
        ):
            job = get_job_manager().create_job(
                library_name="The Show",
                config={"source": "sonarr"},
            )
            _start_job_async(
                job.id,
                config_overrides={
                    "webhook_paths": ["/data/tv/Show/S01E01.mkv"],
                    "webhook_retry_count": 3,
                    "webhook_retry_delay": 30,
                },
            )

        # Both runs happened (parent + retry).
        assert len(run_calls) == 2, (
            f"Expected the parent run plus exactly 1 retry-spawn run; got {len(run_calls)} runs. runs={run_calls!r}"
        )

        # Find the spawned retry job in the JobManager. There must be exactly
        # one with is_retry=True + parent_job_id pointing at the original.
        all_jobs = get_job_manager().get_all_jobs()
        retry_jobs = [
            j
            for j in all_jobs
            if (j.config or {}).get("is_retry") is True and (j.config or {}).get("parent_job_id") == job.id
        ]
        assert len(retry_jobs) == 1, (
            f"Exactly 1 retry job must be spawned with parent_job_id={job.id!r} and is_retry=True; "
            f"found {len(retry_jobs)}. All jobs: "
            f"{[(j.id, j.library_name, (j.config or {}).get('is_retry')) for j in all_jobs]}"
        )
        retry = retry_jobs[0]
        assert retry.config.get("retry_attempt") == 1, (
            f"First retry must carry retry_attempt=1; got {retry.config.get('retry_attempt')!r}"
        )
        # The retry must inherit the PARENT's library_name (so the row in
        # the Jobs UI reads identically — "The Show" → "Retry: The Show",
        # NOT "Retry: S01E01.mkv"). Original behaviour used the raw
        # filename which produced ugly mismatched rows like:
        #   parent: Chelsea vs Nottingham Forest    [Sportarr]
        #   retry:  Retry: English Premier League - S2025E348 - Chelsea vs Nottingham Forest - HDTV-2160p.mkv
        # Inheriting the parent's library_name keeps the two visually
        # aligned and lets the user trace the retry back to its parent
        # at a glance.
        assert retry.library_name == "Retry: The Show", (
            f"Retry library_name must be 'Retry: <parent.library_name>' — got {retry.library_name!r}. "
            f"A regression that falls back to the raw filename produces ugly "
            f"'Retry: episode-file-with-codec-tags.mkv' rows that don't match "
            f"the parent's clean Sonarr-derived title."
        )

        # The retry's run_processing call carried the ORIGINAL webhook path
        # — pinned forward through _spawn_retry_job's retry_paths build.
        retry_run = run_calls[1]
        assert retry_run["webhook_paths"] == ["/data/tv/Show/S01E01.mkv"], (
            f"Retry's run_processing must receive the original webhook_paths verbatim "
            f"(retry_paths logic must not drop them); got {retry_run['webhook_paths']!r}"
        )

    def test_file_missing_when_the_worker_reaches_it_spawns_a_retry(self, app, tmp_path):
        """The file was there for the checking stage and gone when a processing worker ran it. The worker must record
        ``skipped_file_not_found`` (not ``failed``) so job_runner retries it like any other not-found file.
        """
        from media_preview_generator.jobs.worker import Worker
        from media_preview_generator.processing.multi_server import MultiServerResult, MultiServerStatus
        from media_preview_generator.processing.types import ProcessableItem
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        path = "/data/tv/Show/Season 01/Show - S01E01.mkv"
        not_found = MultiServerResult(
            canonical_path=path,
            status=MultiServerStatus.SKIPPED_FILE_NOT_FOUND,
            message=f"Source file not found: {path}",
        )
        published = MultiServerResult(canonical_path=path, status=MultiServerStatus.PUBLISHED)
        run_calls: list[dict] = []

        def fake_run_processing(config, selected_gpus, **kwargs):
            job_id = kwargs.get("job_id")
            run_calls.append({"job_id": job_id, "webhook_paths": list(getattr(config, "webhook_paths", []) or [])})
            worker = Worker(0, "CPU")
            worker.assign_task(
                ProcessableItem(canonical_path=path, server_id="plex-1"), MagicMock(), MagicMock(), job_id=job_id
            )
            worker.current_thread.join(timeout=5)
            return {
                "outcome": {key: count for key, count in worker.outcome_counts.items() if count},
                "webhook_resolution": {
                    "unresolved_paths": [],
                    "skipped_paths": [],
                    "resolved_count": 1,
                    "total_paths": 1,
                    "path_hints": [],
                },
            }

        with (
            app.app_context(),
            patch("media_preview_generator.jobs.orchestrator.run_processing", side_effect=fake_run_processing),
            patch(
                "media_preview_generator.processing.multi_server.process_canonical_path",
                side_effect=[not_found, published],
            ),
            patch("media_preview_generator.plex_client.trigger_plex_partial_scan", return_value=[]),
            patch("media_preview_generator.processing.retry_queue.BACKOFF_SCHEDULE", [1, 1, 1, 1, 1]),
        ):
            job = get_job_manager().create_job(library_name="The Show", config={"source": "sonarr"})
            _start_job_async(
                job.id,
                config_overrides={"webhook_paths": [path], "webhook_retry_count": 3, "webhook_retry_delay": 30},
            )

        retry_jobs = [
            j
            for j in get_job_manager().get_all_jobs()
            if (j.config or {}).get("is_retry") is True and (j.config or {}).get("parent_job_id") == job.id
        ]
        assert len(retry_jobs) == 1
        assert retry_jobs[0].config.get("retry_attempt") == 1
        assert run_calls == [
            {"job_id": job.id, "webhook_paths": [path]},
            {"job_id": retry_jobs[0].id, "webhook_paths": [path]},
        ]
        rows = list(get_job_manager().get_file_results(job.id, dedup_by_path=False))
        assert [(r["file"], r["outcome"]) for r in rows] == [(path, "skipped_file_not_found"), (path, "generated")]


class TestStartJobAsyncRetryBranchPublisherStatuses:
    """Pin the 2026-05-13 refactor's per-publisher status branching.

    The job-level retry collection at job_runner.py:838-852 reads the
    JSONL and checks each row's ``servers[*].status`` for one of three
    "needs another attempt" publisher statuses. If ANY publisher on a
    file reports one of these, the path lands in retry_paths and the
    job-level retry kicks in.

    Matrix cells covered (one row per status, plus a negative case):
      - published_pending_registration (Jellyfin/Emby tile-on-disk but
        not registered yet — the original 756255aa bug)
      - skipped_not_indexed (Plex hasn't analysed the file)
      - skipped_not_in_library (server doesn't see the file at all)
      - generated (success — must NOT trigger retry)

    Without per-cell coverage, a refactor that drops one of the three
    statuses from the trigger set (or adds a 4th that should also
    trigger) goes unnoticed until the 191-pill bug returns in
    production. Mirrors the lesson from the D34 dispatcher matrix:
    'cover the matrix, not one cell'.
    """

    @pytest.mark.parametrize(
        "publisher_status",
        [
            "published_pending_registration",
            "skipped_not_indexed",
            "skipped_not_in_library",
        ],
    )
    def test_publisher_status_triggers_job_level_retry(self, app, tmp_path, publisher_status):
        """Each of the three retry-eligible per-publisher statuses must
        cause a job-level retry to spawn against the same path.
        """
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        run_calls: list[dict] = []

        def fake_run_processing(config, selected_gpus, **kwargs):
            run_calls.append(
                {
                    "webhook_paths": list(getattr(config, "webhook_paths", []) or []),
                    "job_id": kwargs.get("job_id"),
                }
            )
            if len(run_calls) == 1:
                # Parent run: file's publisher returned the retry-eligible
                # status. Seed the JSONL so the orchestrator's post-run
                # scan picks it up via the per-publisher status field.
                jm = get_job_manager()
                jm.record_file_result(
                    kwargs.get("job_id"),
                    "/data/tv/Show/S01E01.mkv",
                    # Aggregate outcome is something benign — the trigger
                    # lives on the per-server pill, not the top-level
                    # outcome string.
                    "generated",
                    "",
                    "[GPU 0]",
                    servers=[
                        {
                            "id": "jelly-1",
                            "name": "Jellyfin",
                            "type": "jellyfin",
                            "status": publisher_status,
                            "message": f"server returned {publisher_status}",
                        }
                    ],
                )
                return {
                    "outcome": {"generated": 1},
                    "webhook_resolution": {
                        "unresolved_paths": [],
                        "skipped_paths": [],
                        "resolved_count": 1,
                        "total_paths": 1,
                        "path_hints": [],
                    },
                }
            # Retry run: simulate the registration succeeding this time.
            # A real worker writes a fresh per-file row on each dispatch
            # — the retry-decision scan in job_runner reads the PARENT's
            # JSONL with dedup_by_path, so the latest row per file is what
            # decides whether to spawn another retry. If the fake doesn't
            # write a fresh "now resolved" row, the dedup keeps the old
            # pending row and the retry spawns again indefinitely.
            jm = get_job_manager()
            # Retry children write to the parent's JSONL via the
            # _file_result_cb redirect, so target the parent here.
            parent_id = (jm.get_job(kwargs.get("job_id")).config or {}).get("parent_job_id") or kwargs.get("job_id")
            jm.record_file_result(
                parent_id,
                "/data/tv/Show/S01E01.mkv",
                "generated",
                "",
                "[GPU 0]",
                servers=[
                    {
                        "id": "jelly-1",
                        "name": "Jellyfin",
                        "type": "jellyfin",
                        "status": "published",  # now resolved
                        "message": "registered after retry",
                    }
                ],
            )
            return {
                "outcome": {"generated": 1},
                "webhook_resolution": {
                    "unresolved_paths": [],
                    "skipped_paths": [],
                    "resolved_count": 1,
                    "total_paths": 1,
                    "path_hints": [],
                },
            }

        with (
            app.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=fake_run_processing,
            ),
            patch(
                "media_preview_generator.plex_client.trigger_plex_partial_scan",
                return_value=[],
            ),
            patch(
                "media_preview_generator.processing.retry_queue.BACKOFF_SCHEDULE",
                [1, 1, 1, 1, 1],
            ),
        ):
            job = get_job_manager().create_job(
                library_name="The Show",
                config={"source": "sonarr"},
            )
            _start_job_async(
                job.id,
                config_overrides={
                    "webhook_paths": ["/data/tv/Show/S01E01.mkv"],
                    "webhook_retry_count": 3,
                    "webhook_retry_delay": 30,
                },
            )

        assert len(run_calls) == 2, (
            f"{publisher_status} must trigger one retry run; got {len(run_calls)} runs: {run_calls!r}"
        )

        all_jobs = get_job_manager().get_all_jobs()
        retry_jobs = [
            j
            for j in all_jobs
            if (j.config or {}).get("is_retry") is True and (j.config or {}).get("parent_job_id") == job.id
        ]
        assert len(retry_jobs) == 1, (
            f"{publisher_status} must spawn exactly 1 retry job; got {len(retry_jobs)}: "
            f"{[(j.id, j.library_name) for j in retry_jobs]}"
        )
        # The retry must carry the affected path — the publisher-status
        # filter at job_runner.py:838-852 is the only thing that
        # determined this. If it broke, the retry would run against an
        # empty webhook_paths list.
        retry_run = run_calls[1]
        assert retry_run["webhook_paths"] == ["/data/tv/Show/S01E01.mkv"], (
            f"{publisher_status} retry must include the affected path; got {retry_run['webhook_paths']!r}"
        )

    def test_last_retry_that_stays_unindexed_says_it_gave_up(self, app, tmp_path):
        """Job c95a5453 (the last retry of chain d10ef2f9) logged "They'll be retried automatically — slow backoff:
        1m → 2m → 5m → 15m → 60m" although it queued no retry and the chain head was marked failed. The last retry
        must say the file wasn't indexed after N retries and that the next scheduled scan picks it up.
        """
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        run_calls: list[str] = []

        def fake_run_processing(config, selected_gpus, **kwargs):
            run_calls.append(kwargs.get("job_id"))
            jm = get_job_manager()
            target = (jm.get_job(kwargs.get("job_id")).config or {}).get("parent_job_id") or kwargs.get("job_id")
            jm.record_file_result(
                target,
                "/data/tv/Show/S01E01.mkv",
                "skipped_not_indexed",
                "not indexed yet",
                "[GPU 0]",
                servers=[
                    {
                        "server_id": "plex-1",
                        "server_name": "Plex Main",
                        "server_type": "plex",
                        "status": "skipped_not_in_library",
                        "message": "This file isn't in Plex's library yet",
                    }
                ],
            )
            return {
                "outcome": {"skipped_not_indexed": 1},
                "webhook_resolution": {
                    "unresolved_paths": [],
                    "skipped_paths": [],
                    "resolved_count": 1,
                    "total_paths": 1,
                    "path_hints": [],
                },
            }

        with (
            app.app_context(),
            patch("media_preview_generator.jobs.orchestrator.run_processing", side_effect=fake_run_processing),
            patch("media_preview_generator.plex_client.trigger_plex_partial_scan", return_value=[]),
            patch("media_preview_generator.processing.retry_queue.BACKOFF_SCHEDULE", [1, 1, 1, 1, 1]),
        ):
            jm = get_job_manager()
            job = jm.create_job(library_name="Signs Of A Psychopath S11E11", config={"source": "sonarr"})
            with patch.object(jm, "upsert_retry_chain_job", wraps=jm.upsert_retry_chain_job) as chain:
                _start_job_async(
                    job.id,
                    config_overrides={
                        "webhook_paths": ["/data/tv/Show/S01E01.mkv"],
                        "webhook_retry_count": 2,
                        "webhook_retry_delay": 30,
                    },
                )
            retries = sorted(
                (j for j in jm.get_all_jobs() if (j.config or {}).get("parent_job_id") == job.id),
                key=lambda j: j.config["retry_attempt"],
            )
            last_logs = jm.get_logs(retries[-1].id) or []
            all_logs = (jm.get_logs(job.id) or []) + [line for r in retries for line in (jm.get_logs(r.id) or [])]

        assert len(run_calls) == 3, f"original + 2 retries expected; got {run_calls!r}"
        gave_up = (
            "1 file(s) still weren't indexed by the media server after 2 retries, so no more retries are queued. "
            "The next scheduled scan will pick them up."
        )
        assert any(line.endswith(f"WARNING - {gave_up}") for line in last_logs), (
            f"the last retry must say it gave up; got {last_logs!r}"
        )
        assert not any("retried automatically" in line or "slow backoff" in line for line in all_logs), all_logs
        exhausted = [c.kwargs for c in chain.call_args_list if c.kwargs.get("outcome") == "exhausted"]
        assert [(c["originating_job_id"], c["reason"]) for c in exhausted] == [
            (job.id, f"{gave_up} Check the Files panel for the affected paths.")
        ]

    def test_retry_spawn_log_is_warning_with_server_attribution(self, app, tmp_path):
        """Retry-spawn log lines must:
          1. Carry the ``WARNING - `` level prefix (not ``INFO - ``).
          2. Name the actual blocker server in the reason text, e.g.
             ``WARNING - JellyTest pending × 1, retry scheduled in 30s``.

        Pre-2026-05-13 this line was INFO and named only generic counts
        — operators couldn't grep their Job log for "what's slow" and
        the warn-color treatment was reserved for actual failures.
        With the per-publisher pending data now flowing through the
        retry-decision scan, the log line carries both the level
        bump AND the server-name attribution.
        """
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        run_calls: list[dict] = []

        def fake_run_processing(config, selected_gpus, **kwargs):
            run_calls.append({"job_id": kwargs.get("job_id")})
            jm = get_job_manager()
            target = (jm.get_job(kwargs.get("job_id")).config or {}).get("parent_job_id") or kwargs.get("job_id")
            # Real workers feed ``record_file_result`` the FULL publisher
            # shape (``server_id`` / ``server_name`` / ``server_type``);
            # ``record_file_result`` then re-maps that to the SLIM
            # ``{id, name, type, status}`` shape it persists in the JSONL.
            # Using FULL keys here is the contract the retry-decision
            # scan and the reason-text aggregation rely on.
            #
            # First TWO calls write Jellyfin-pending so the chain runs
            # across BOTH retry-spawn branches:
            #   - Run 1 (initial)   → first-retry-spawn branch fires
            #     (job_runner.py: "WARNING - …, retry scheduled in Xs")
            #   - Run 2 (retry 1)   → is_retry continuation branch fires
            #     (job_runner.py: "WARNING - Retry N/M: …, next retry in Xs")
            #   - Run 3 (retry 2)   → publisher resolved, chain succeeds
            if len(run_calls) <= 2:
                jm.record_file_result(
                    target,
                    "/data/tv/Show/S01E01.mkv",
                    "generated",
                    "",
                    "[GPU 0]",
                    servers=[
                        {
                            "server_id": "jelly-1",
                            "server_name": "JellyTest",
                            "server_type": "jellyfin",
                            "status": "published_pending_registration",
                            "message": "not indexed yet",
                        }
                    ],
                )
            else:
                jm.record_file_result(
                    target,
                    "/data/tv/Show/S01E01.mkv",
                    "generated",
                    "",
                    "[GPU 0]",
                    servers=[
                        {
                            "server_id": "jelly-1",
                            "server_name": "JellyTest",
                            "server_type": "jellyfin",
                            "status": "published",
                            "message": "registered after 2 retries",
                        }
                    ],
                )
            return {
                "outcome": {"generated": 1},
                "webhook_resolution": {
                    "unresolved_paths": [],
                    "skipped_paths": [],
                    "resolved_count": 1,
                    "total_paths": 1,
                    "path_hints": [],
                },
            }

        with (
            app.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=fake_run_processing,
            ),
            patch(
                "media_preview_generator.plex_client.trigger_plex_partial_scan",
                return_value=[],
            ),
            patch(
                "media_preview_generator.processing.retry_queue.BACKOFF_SCHEDULE",
                [1, 1, 1, 1, 1],
            ),
        ):
            jm = get_job_manager()
            job = jm.create_job(library_name="The Show", config={"source": "sonarr"})
            _start_job_async(
                job.id,
                config_overrides={
                    "webhook_paths": ["/data/tv/Show/S01E01.mkv"],
                    "webhook_retry_count": 3,
                    "webhook_retry_delay": 30,
                },
            )

            parent_logs = jm.get_logs(job.id) or []
            # The is_retry continuation log line lives on the RETRY
            # CHILD's log file — each retry Job has its own log, and
            # ``add_log(job_id, ...)`` is keyed on the LOCAL job_id
            # (the child's UUID when running inside a retry child's
            # run_job thread). Aggregate logs across all retry
            # children of this chain so the assertion can target the
            # continuation branch.
            retry_children = [
                j
                for j in jm.get_all_jobs()
                if (j.config or {}).get("is_retry") is True and (j.config or {}).get("parent_job_id") == job.id
            ]
            child_logs: list[str] = []
            for child in retry_children:
                child_logs.extend(jm.get_logs(child.id) or [])

        # Three run_processing calls expected: initial + 2 retries.
        # Drives both the ``not is_retry`` and the
        # ``is_retry and retry_attempt < effective_max`` log-emission
        # branches.
        assert len(run_calls) == 3, f"Expected 3 runs (initial + 2 retries); got {len(run_calls)}: {run_calls!r}"

        # First-retry-spawn branch (``not is_retry`` block in
        # job_runner.py). Lands on the PARENT's log because the parent
        # is the job whose initial dispatch fired the first retry.
        # Format: "WARNING - {reason}, retry scheduled in Xs".
        first_spawn = [line for line in parent_logs if "WARNING - " in line and "retry scheduled" in line]
        assert first_spawn, (
            f"Expected at least one 'WARNING - … retry scheduled' line on the PARENT job's log "
            f"from the FIRST retry spawn; got parent_logs={parent_logs!r}"
        )
        assert any("JellyTest pending" in line for line in first_spawn), (
            f"First-retry-spawn log must name the blocker server; matching warn lines: {first_spawn!r}"
        )
        # The real first wait (the patched 1 s schedule step scaled by the 30 s setting), not the setting itself.
        assert any(line.endswith("retry scheduled in 1s (retry 1 of 3)") for line in first_spawn), (
            f"First-retry-spawn log must state the real wait and the retry count; got {first_spawn!r}"
        )

        # is_retry continuation branch (in-chain spawn). Lands on the
        # RETRY CHILD's log because the spawning thread is the child's
        # run_job. Format: "WARNING - Retry N/M: {reason}, next retry
        # in Xs". A regression that bumps only the FIRST spawn site and
        # leaves the continuation site at INFO passes the assertions
        # above but fails here.
        continuation = [line for line in child_logs if "WARNING - Retry " in line and "next retry in" in line]
        assert continuation, (
            f"Expected at least one 'WARNING - Retry N/M: …, next retry in …' line on a retry-child "
            f"log from the continuation branch; got child_logs={child_logs!r}"
        )
        assert any("JellyTest pending" in line for line in continuation), (
            f"Continuation-retry log must name the blocker server; matching warn lines: {continuation!r}"
        )

        # Defensive: no surviving INFO-level retry log lines from
        # either spawn site (parent OR child logs). Catches partial
        # regressions where only one of the two literal
        # "INFO - " → "WARNING - " bumps gets reverted.
        all_logs = parent_logs + child_logs
        info_retry_lines = [
            line for line in all_logs if "INFO - " in line and ("retry scheduled" in line or "next retry in" in line)
        ]
        assert not info_retry_lines, (
            f"No INFO-level retry log lines should remain from either spawn site; found: {info_retry_lines!r}"
        )

    def test_plex_source_retry_does_not_inherit_publish_pin(self, app, tmp_path):
        """Plex-source webhooks set the top-level ``server_id`` on the
        parent Job for display attribution ("which server fired the
        webhook"), but the parent's CONFIG.server_id stays unset so the
        worker fans out to all owning servers (Plex+Emby+Jellyfin).
        When the retry chain spawned a child Job for the publish-
        pending case, ``_spawn_retry_job`` was inheriting the parent's
        top-level ``server_id`` straight into ``retry_async_config``,
        which the worker then read as an explicit publish pin —
        re-running ONLY against Plex and leaving Jellyfin's pending
        registration un-retried.

        Live evidence: chain ``2f7132d5`` (Law & Order S04E12, Plex
        media.added source). Initial dispatch hit all 3 servers;
        Jellyfin returned ``published_pending_registration``. The
        retry inherited ``config.server_id=plex-default`` and only
        re-published to Plex (which was already done). Jellyfin's
        pending state was never re-attempted.

        Contract: when the parent has no EXPLICIT ``config.server_id``,
        the retry must also have no ``config.server_id`` regardless
        of the parent's top-level (source-attribution) server_id.
        """
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        run_calls: list[dict] = []

        def fake_run_processing(config, selected_gpus, **kwargs):
            run_calls.append(
                {
                    "job_id": kwargs.get("job_id"),
                    "server_id_filter": getattr(config, "server_id_filter", None),
                }
            )
            jm = get_job_manager()
            target = (jm.get_job(kwargs.get("job_id")).config or {}).get("parent_job_id") or kwargs.get("job_id")
            if len(run_calls) == 1:
                # Initial dispatch — Jellyfin pending; triggers a retry.
                jm.record_file_result(
                    target,
                    "/data/tv/Law & Order S04E12.mkv",
                    "generated",
                    "",
                    "[GPU 0]",
                    servers=[
                        {
                            "server_id": "jelly-1",
                            "server_name": "JellyTest",
                            "server_type": "jellyfin",
                            "status": "published_pending_registration",
                            "message": "not indexed yet",
                        }
                    ],
                )
            else:
                # Retry — succeeds.
                jm.record_file_result(
                    target,
                    "/data/tv/Law & Order S04E12.mkv",
                    "generated",
                    "",
                    "[GPU 0]",
                    servers=[
                        {
                            "server_id": "jelly-1",
                            "server_name": "JellyTest",
                            "server_type": "jellyfin",
                            "status": "published",
                            "message": "registered",
                        }
                    ],
                )
            return {
                "outcome": {"generated": 1},
                "webhook_resolution": {
                    "unresolved_paths": [],
                    "skipped_paths": [],
                    "resolved_count": 1,
                    "total_paths": 1,
                    "path_hints": [],
                },
            }

        with (
            app.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=fake_run_processing,
            ),
            patch(
                "media_preview_generator.plex_client.trigger_plex_partial_scan",
                return_value=[],
            ),
            patch(
                "media_preview_generator.processing.retry_queue.BACKOFF_SCHEDULE",
                [1, 1, 1, 1, 1],
            ),
        ):
            jm = get_job_manager()
            # Simulate the shape of a Plex media.added webhook: the
            # webhook-creation path sets the TOP-LEVEL server_id to
            # "plex-1" (source attribution for the row in the panel)
            # but the parent's CONFIG.server_id stays unset so the
            # initial dispatch fans out to all owning servers.
            job = jm.create_job(
                library_name="Law & Order S04E12",
                config={"source": "plex"},  # no config.server_id — Plex source fans out
                server_id="plex-1",
                server_name="Plex",
                server_type="plex",
            )
            _start_job_async(
                job.id,
                config_overrides={
                    "webhook_paths": ["/data/tv/Law & Order S04E12.mkv"],
                    "webhook_retry_count": 3,
                    "webhook_retry_delay": 30,
                },
            )

        # A retry was spawned.
        retry_children = [
            j
            for j in jm.get_all_jobs()
            if (j.config or {}).get("is_retry") is True and (j.config or {}).get("parent_job_id") == job.id
        ]
        assert len(retry_children) >= 1, (
            f"Plex-source pending_registration must still trigger a retry; got {len(retry_children)} children"
        )

        retry_child = retry_children[0]
        # Top-level server_id is preserved for display — the retry row
        # still attributes to "Plex" in the Jobs panel.
        assert retry_child.server_id == "plex-1", (
            f"Retry must preserve top-level server_id for display attribution; got {retry_child.server_id!r}"
        )
        # CRITICAL: retry's CONFIG.server_id must be absent / None.
        # Setting it pins the worker's publish path to Plex only and
        # the pending Jellyfin registration would never be retried.
        retry_config_pin = (retry_child.config or {}).get("server_id")
        assert not retry_config_pin, (
            f"Retry must NOT carry config.server_id when parent had no explicit pin; "
            f"got config.server_id={retry_config_pin!r}. This would restrict the retry to "
            f"Plex only and leave Jellyfin's pending registration un-retried — the production "
            f"bug observed on chain 2f7132d5."
        )
        # And confirm at the orchestrator boundary: the retry's
        # run_processing call MUST have config.server_id_filter == None
        # (the worker fans out). If the pin leaked through the kwargs
        # plumbing despite config being clean, this catches it.
        retry_run = run_calls[1] if len(run_calls) > 1 else None
        if retry_run is not None:
            assert retry_run["server_id_filter"] is None, (
                f"Retry's run_processing must receive config with no server_id_filter "
                f"so it fans out to all owning servers; got "
                f"server_id_filter={retry_run['server_id_filter']!r}"
            )

    def test_explicit_config_pin_is_preserved_on_retry(self, app, tmp_path):
        """The complement to the previous test: when the parent DOES
        carry an explicit ``config.server_id`` (manual reprocess pinned
        to one server, or a multi-Plex install where the user wants
        the retry scoped), that pin MUST flow through to the retry.

        Dropping inheritance unconditionally would break the
        explicit-pin case — a user who manually pinned a retry to
        EmbyTest would see the retry fan out and write extra trickplay
        on the un-targeted servers.
        """
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        run_calls: list[dict] = []

        def fake_run_processing(config, selected_gpus, **kwargs):
            run_calls.append(
                {
                    "job_id": kwargs.get("job_id"),
                    "server_id_filter": getattr(config, "server_id_filter", None),
                }
            )
            jm = get_job_manager()
            target = (jm.get_job(kwargs.get("job_id")).config or {}).get("parent_job_id") or kwargs.get("job_id")
            if len(run_calls) == 1:
                jm.record_file_result(
                    target,
                    "/data/tv/Show S01E01.mkv",
                    "generated",
                    "",
                    "[GPU 0]",
                    servers=[
                        {
                            "server_id": "emby-1",
                            "server_name": "EmbyTest",
                            "server_type": "emby",
                            "status": "published_pending_registration",
                            "message": "not indexed",
                        }
                    ],
                )
            else:
                jm.record_file_result(
                    target,
                    "/data/tv/Show S01E01.mkv",
                    "generated",
                    "",
                    "[GPU 0]",
                    servers=[
                        {
                            "server_id": "emby-1",
                            "server_name": "EmbyTest",
                            "server_type": "emby",
                            "status": "published",
                            "message": "registered",
                        }
                    ],
                )
            return {
                "outcome": {"generated": 1},
                "webhook_resolution": {
                    "unresolved_paths": [],
                    "skipped_paths": [],
                    "resolved_count": 1,
                    "total_paths": 1,
                    "path_hints": [],
                },
            }

        with (
            app.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=fake_run_processing,
            ),
            patch(
                "media_preview_generator.plex_client.trigger_plex_partial_scan",
                return_value=[],
            ),
            patch(
                "media_preview_generator.processing.retry_queue.BACKOFF_SCHEDULE",
                [1, 1, 1, 1, 1],
            ),
        ):
            jm = get_job_manager()
            # Parent carries an EXPLICIT config.server_id pin — e.g. a
            # manual reprocess scoped to EmbyTest. The retry MUST keep
            # it.
            job = jm.create_job(
                library_name="Show S01E01",
                config={"source": "manual", "server_id": "emby-1"},
                server_id="emby-1",
                server_name="EmbyTest",
                server_type="emby",
            )
            _start_job_async(
                job.id,
                config_overrides={
                    "webhook_paths": ["/data/tv/Show S01E01.mkv"],
                    "server_id": "emby-1",  # the explicit pin from the manual reprocess
                    "webhook_retry_count": 3,
                    "webhook_retry_delay": 30,
                },
            )

        retry_children = [
            j
            for j in jm.get_all_jobs()
            if (j.config or {}).get("is_retry") is True and (j.config or {}).get("parent_job_id") == job.id
        ]
        assert len(retry_children) >= 1, "Expected a retry to spawn"
        retry_child = retry_children[0]
        retry_config_pin = (retry_child.config or {}).get("server_id")
        assert retry_config_pin == "emby-1", (
            f"Explicit config.server_id pin must be preserved on retry; got {retry_config_pin!r}"
        )

    def test_scheduled_scan_without_webhook_retry_count_still_retries(self, app, tmp_path):
        """Scheduled scans, manual reruns, and recently-added scans
        don't inject ``webhook_retry_count`` into their job config
        (only webhook handlers do). Pre-2026-05-13 the per-file retry
        queue handled PENDING_REGISTRATION for ALL job types
        regardless. Post-refactor the per-job retry path must also
        apply to those job types — by falling back to the global
        ``webhook_retry_count`` setting when the job config doesn't
        carry one. Otherwise a scheduled scan that hits PENDING
        registrations would never retry and the chain stays in the
        pending state forever (until Jellyfin's own background scan
        catches up).

        Pin the fallback behaviour so a regression that drops the
        setting fallback would silently break retries for all
        non-webhook job types.
        """
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        run_calls: list[dict] = []

        def fake_run_processing(config, selected_gpus, **kwargs):
            run_calls.append({"job_id": kwargs.get("job_id")})
            jm = get_job_manager()
            target = (jm.get_job(kwargs.get("job_id")).config or {}).get("parent_job_id") or kwargs.get("job_id")
            if len(run_calls) == 1:
                jm.record_file_result(
                    target,
                    "/data/Movies/Foo (2024)/Foo.mkv",
                    "generated",
                    "",
                    "[GPU 0]",
                    servers=[
                        {
                            "id": "jelly-1",
                            "name": "Jellyfin",
                            "type": "jellyfin",
                            "status": "published_pending_registration",
                            "message": "not indexed yet",
                        }
                    ],
                )
            else:
                jm.record_file_result(
                    target,
                    "/data/Movies/Foo (2024)/Foo.mkv",
                    "generated",
                    "",
                    "[GPU 0]",
                    servers=[
                        {
                            "id": "jelly-1",
                            "name": "Jellyfin",
                            "type": "jellyfin",
                            "status": "published",
                            "message": "registered after retry",
                        }
                    ],
                )
            return {
                "outcome": {"generated": 1},
                "webhook_resolution": {
                    "unresolved_paths": [],
                    "skipped_paths": [],
                    "resolved_count": 1,
                    "total_paths": 1,
                    "path_hints": [],
                },
            }

        with (
            app.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=fake_run_processing,
            ),
            patch(
                "media_preview_generator.plex_client.trigger_plex_partial_scan",
                return_value=[],
            ),
            patch(
                "media_preview_generator.processing.retry_queue.BACKOFF_SCHEDULE",
                [1, 1, 1, 1, 1],
            ),
        ):
            # Mimic a scheduled-scan-style job: webhook_paths but NO
            # webhook_retry_count / webhook_retry_delay in config or
            # overrides. The global setting default (3) must drive
            # the retry budget.
            job = get_job_manager().create_job(library_name="Movies", config={})
            _start_job_async(
                job.id,
                config_overrides={"webhook_paths": ["/data/Movies/Foo (2024)/Foo.mkv"]},
            )

        assert len(run_calls) == 2, (
            f"Scheduled-scan-style job must retry via the global default; got {len(run_calls)} runs"
        )
        retry_jobs = [
            j
            for j in get_job_manager().get_all_jobs()
            if (j.config or {}).get("is_retry") is True and (j.config or {}).get("parent_job_id") == job.id
        ]
        assert len(retry_jobs) == 1, (
            f"Exactly 1 retry must spawn for a job without webhook_retry_count in config; got {len(retry_jobs)}"
        )

    def test_only_success_status_does_not_trigger_retry(self, app, tmp_path):
        """Negative case: a row whose publishers all reported
        ``published`` must NOT trigger a retry. Without this assertion,
        a regression that added ``published`` to the trigger set would
        silently retry every successful job.
        """
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        run_calls: list[dict] = []

        def fake_run_processing(config, selected_gpus, **kwargs):
            run_calls.append({"job_id": kwargs.get("job_id")})
            jm = get_job_manager()
            jm.record_file_result(
                kwargs.get("job_id"),
                "/data/tv/Show/S01E01.mkv",
                "generated",
                "",
                "[GPU 0]",
                servers=[
                    {
                        "id": "plex-1",
                        "name": "Plex",
                        "type": "plex",
                        "status": "published",
                        "message": "tiles uploaded",
                    }
                ],
            )
            return {
                "outcome": {"generated": 1},
                "webhook_resolution": {
                    "unresolved_paths": [],
                    "skipped_paths": [],
                    "resolved_count": 1,
                    "total_paths": 1,
                    "path_hints": [],
                },
            }

        with (
            app.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=fake_run_processing,
            ),
            patch(
                "media_preview_generator.plex_client.trigger_plex_partial_scan",
                return_value=[],
            ),
        ):
            job = get_job_manager().create_job(
                library_name="The Show",
                config={"source": "sonarr"},
            )
            _start_job_async(
                job.id,
                config_overrides={
                    "webhook_paths": ["/data/tv/Show/S01E01.mkv"],
                    "webhook_retry_count": 3,
                    "webhook_retry_delay": 30,
                },
            )

        # No retry should fire — only 1 run total.
        assert len(run_calls) == 1, f"Success status must NOT spawn a retry; got {len(run_calls)} runs"
        retry_jobs = [
            j
            for j in get_job_manager().get_all_jobs()
            if (j.config or {}).get("is_retry") is True and (j.config or {}).get("parent_job_id") == job.id
        ]
        assert retry_jobs == [], f"Success must spawn 0 retries; got {len(retry_jobs)}"

    def test_file_gone_from_disk_spawns_no_retry_and_completes_green(self, app, tmp_path):
        """Production 2026-09-24: Plex's webhook named Slow Horses S06E02 ``-CAKES.mkv``, which Sonarr had replaced
        a minute earlier. That row was ``skipped_file_not_found``, so the job rescanned Plex, retried for 16 minutes
        and failed on "check path mapping configuration". A ``skipped_source_gone`` row must end the job green on its
        first run: no Plex rescan, no retry job, no warning or error text.
        """
        from media_preview_generator.web.jobs import JobStatus, get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        webhook_path = "/data/tv/Slow Horses/Season 06/Slow Horses - S06E02-CAKES.mkv"
        reason = "Skipped: replaced by a newer file (Slow Horses - S06E02.mkv)"
        run_calls: list[dict] = []

        def fake_run_processing(config, selected_gpus, **kwargs):
            run_calls.append(
                {
                    "job_id": kwargs.get("job_id"),
                    "webhook_paths": list(getattr(config, "webhook_paths", []) or []),
                }
            )
            get_job_manager().record_file_result(
                kwargs.get("job_id"), webhook_path, "skipped_source_gone", reason, "Checking"
            )
            return {
                "outcome": {"skipped_source_gone": 1, "generated": 0, "failed": 0},
                "webhook_resolution": {
                    "unresolved_paths": [],
                    "skipped_paths": [],
                    "resolved_count": 1,
                    "total_paths": 1,
                    "path_hints": [],
                },
            }

        with (
            app.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=fake_run_processing,
            ),
            patch(
                "media_preview_generator.plex_client.trigger_plex_partial_scan",
                return_value=[],
            ) as mock_rescan,
        ):
            job = get_job_manager().create_job(library_name="Slow Horses S06E02", config={"source": "plex"})
            _start_job_async(
                job.id,
                config_overrides={
                    "webhook_paths": [webhook_path],
                    "webhook_retry_count": 3,
                    "webhook_retry_delay": 30,
                },
            )

        assert run_calls == [{"job_id": job.id, "webhook_paths": [webhook_path]}]
        retry_jobs = [
            j
            for j in get_job_manager().get_all_jobs()
            if (j.config or {}).get("is_retry") is True and (j.config or {}).get("parent_job_id") == job.id
        ]
        assert retry_jobs == []
        mock_rescan.assert_not_called()
        finished = get_job_manager().get_job(job.id)
        assert finished.status is JobStatus.COMPLETED
        assert finished.error is None
        assert "resolution_summary" not in (finished.config or {})
        rows = list(get_job_manager().get_file_results(job.id))
        assert [(r["file"], r["outcome"], r["reason"]) for r in rows] == [(webhook_path, "skipped_source_gone", reason)]


# ---------------------------------------------------------------------------
# Branch 3: regenerate=True propagates to Config.regenerate_thumbnails
# ---------------------------------------------------------------------------


class TestStartJobAsyncRegeneratePropagation:
    """``config_overrides['force_generate'] = True`` must flip
    ``Config.regenerate_thumbnails`` on for this job. A regression that
    drops the override would silently re-use stale .meta journal entries
    and skip files the user explicitly asked to regenerate (the Force
    Regenerate button on the Jobs page)."""

    def test_force_generate_override_sets_regenerate_thumbnails_on_config(self, app, tmp_path):
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        captured: dict = {}

        def fake_run_processing(config, selected_gpus, **kwargs):
            captured["regenerate_thumbnails"] = bool(config.regenerate_thumbnails)
            return {"outcome": {"generated": 0}}

        with (
            app.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=fake_run_processing,
            ),
        ):
            job = get_job_manager().create_job(library_name="Movies", config={})
            _start_job_async(job.id, config_overrides={"force_generate": True})

        assert captured.get("regenerate_thumbnails") is True, (
            f"force_generate=True override must set Config.regenerate_thumbnails=True; "
            f"got {captured.get('regenerate_thumbnails')!r}. A regression here silently ignores "
            f"the user clicking Force Regenerate."
        )


# ---------------------------------------------------------------------------
# Branch 4: webhook_item_id_hints propagate to Config.webhook_item_id_hints
# ---------------------------------------------------------------------------


class TestStartJobAsyncWebhookHintsPropagation:
    """Vendor webhooks (Plex/Emby/Jellyfin) carry ``{path: {server_id:
    item_id}}`` hints so the orchestrator skips a slow Plex round trip.
    A regression that drops hints silently degrades to "look this file up
    in Plex" — fatal on a Plex-less install (Emby/Jellyfin only) where
    Plex isn't even configured. Pin the propagation contract."""

    def test_webhook_item_id_hints_land_on_config(self, app, tmp_path):
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        captured: dict = {}
        hints_in = {"/data/tv/Show/S01E01.mkv": {"emby-1": "abc123"}}

        def fake_run_processing(config, selected_gpus, **kwargs):
            captured["hints"] = getattr(config, "webhook_item_id_hints", None)
            return {"outcome": {"generated": 0}}

        with (
            app.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=fake_run_processing,
            ),
        ):
            job = get_job_manager().create_job(library_name="Show", config={})
            _start_job_async(
                job.id,
                config_overrides={
                    "webhook_paths": ["/data/tv/Show/S01E01.mkv"],
                    "webhook_item_id_hints": hints_in,
                },
            )

        # Strict equality: the hints dict must arrive byte-for-byte the same.
        assert captured.get("hints") == hints_in, (
            f"webhook_item_id_hints override must reach Config.webhook_item_id_hints unchanged; "
            f"got {captured.get('hints')!r}. A regression here means Emby/Jellyfin webhooks would "
            f"fall back to a Plex lookup and 500 on Plex-less installs."
        )


# ---------------------------------------------------------------------------
# Branch 5: server_id pin → Config.server_id_filter
# ---------------------------------------------------------------------------


class TestStartJobAsyncServerIdPin:
    """``config_overrides['server_id'] = 'emby-1'`` must:
      1. set ``Config.server_id_filter = 'emby-1'`` (downstream dispatch
         filters publishers to this id)
      2. project the legacy Plex view from THE PINNED SERVER (not
         media_servers[0]) — when pinned to a Plex server. Pinning to
         non-Plex (emby) leaves plex_url empty (no Plex view derivable).

    Pre-K1: pinning to a non-Plex server still tried to use media_servers[0]'s
    Plex URL/token, fanning to every server. Pin both halves of the contract."""

    def test_server_id_pin_to_emby_sets_filter_and_clears_plex_view(self, app, tmp_path):
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        captured: dict = {}

        def fake_run_processing(config, selected_gpus, **kwargs):
            captured["server_id_filter"] = getattr(config, "server_id_filter", None)
            captured["plex_url"] = config.plex_url
            return {"outcome": {"generated": 0}}

        with (
            app.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=fake_run_processing,
            ),
        ):
            job = get_job_manager().create_job(library_name="Movies", config={})
            _start_job_async(
                job.id,
                config_overrides={
                    "webhook_paths": ["/data/movies/x.mkv"],
                    "server_id": "emby-1",
                },
            )

        assert captured.get("server_id_filter") == "emby-1", (
            f"server_id override must reach Config.server_id_filter exactly; "
            f"got {captured.get('server_id_filter')!r}. A regression silently fans out to every "
            f"configured server, which is the multi-server publisher-mismatch class of bug."
        )

    def test_server_id_pin_to_plex_projects_pinned_server_view(self, app, tmp_path):
        """Pinning to a specific Plex server must project the legacy plex
        view from THAT entry (not media_servers[0]). With one Plex configured,
        plex-1's url/token are the same as media_servers[0]'s — the test
        still verifies the value lands so a future multi-Plex regression
        that drops the pinned-id path is caught."""
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        captured: dict = {}

        def fake_run_processing(config, selected_gpus, **kwargs):
            captured["server_id_filter"] = getattr(config, "server_id_filter", None)
            captured["plex_url"] = config.plex_url
            captured["plex_token"] = config.plex_token
            return {"outcome": {"generated": 0}}

        with (
            app.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=fake_run_processing,
            ),
        ):
            job = get_job_manager().create_job(library_name="Movies", config={})
            _start_job_async(
                job.id,
                config_overrides={
                    "webhook_paths": ["/data/movies/x.mkv"],
                    "server_id": "plex-1",
                },
            )

        assert captured.get("server_id_filter") == "plex-1"
        # The pinned Plex's url/token still get projected — without this a
        # job pinned to a non-default Plex would try to talk to the wrong server.
        assert captured.get("plex_url") == "http://plex:32400", (
            f"Pinned-to-plex-1 job must project plex-1's URL onto config.plex_url; got {captured.get('plex_url')!r}"
        )
        assert captured.get("plex_token") == "tok"


# ---------------------------------------------------------------------------
# Branch 6: simplified webhook_resolution payload contract (post-K4 unification)
# ---------------------------------------------------------------------------


class TestStartJobAsyncSimplifiedPayload:
    """Pin the contract between the orchestrator's unified webhook phase
    and ``_start_job_async``'s result handler.

    The unification (commit 3edd185) shrunk the ``webhook_resolution``
    payload: ``resolution_source`` was dropped, ``path_hints`` now
    carries the path-mapping mismatch diagnostics that used to come
    from the Plex-first stage, and the job-level
    ``trigger_plex_partial_scan`` call was removed in favour of the
    per-server ``trigger_refresh`` already firing inside the worker.

    Without these tests, a future refactor that re-introduces a field
    under a new name (or drops ``path_hints`` thinking it's unused)
    would silently break the file_result UI's "did you forget a path
    mapping?" diagnostic — the exact UX regression risk the plan
    called out.
    """

    def test_unresolved_path_with_mapping_hint_lands_in_file_result(self, app, tmp_path):
        """When ``run_processing`` returns the unified payload with a
        ``path_hints[0]`` carrying a path-mapping mismatch hint, the
        job_runner must surface that hint as the file_result message.
        """
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        hint = (
            "Possible prefix mismatch: webhook sends '/mnt' but a configured library "
            "uses '/data'. Add a path mapping in Settings: server path = /data, "
            "webhook path = /mnt"
        )

        def fake_run_processing(config, selected_gpus, **kwargs):
            return {
                "outcome": {"generated": 0, "failed": 0},
                "webhook_resolution": {
                    "unresolved_paths": ["/mnt/data/Movies/X.mkv"],
                    "skipped_paths": [],
                    "resolved_count": 0,
                    "total_paths": 1,
                    "path_hints": [hint],
                    # NB: no `resolution_source` — that field is gone post-unification.
                },
            }

        with (
            app.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=fake_run_processing,
            ),
            # Shrink retry backoff so the spawned retry job's countdown
            # doesn't pin the test for 30s; the assertion below only
            # cares about the parent job's file_result row.
            patch(
                "media_preview_generator.processing.retry_queue.BACKOFF_SCHEDULE",
                [1, 1, 1, 1, 1],
            ),
        ):
            job = get_job_manager().create_job(library_name="Movies", config={})
            _start_job_async(
                job.id,
                config_overrides={
                    "webhook_paths": ["/mnt/data/Movies/X.mkv"],
                    "webhook_retry_count": 0,  # no retry — keeps the test focused
                },
            )

        results = get_job_manager().get_file_results(job.id)
        unresolved_rows = [r for r in results if r.get("file") == "/mnt/data/Movies/X.mkv"]
        assert unresolved_rows, (
            "file_result for the unresolved path was never written — "
            "result handler stopped reading webhook_resolution.unresolved_paths"
        )
        row = unresolved_rows[0]
        # The outcome key must match what the file_results UI filters on.
        assert row["outcome"] == "unresolved_vendor", (
            f"Post-unification outcome key must be 'unresolved_vendor' (was 'unresolved_plex' "
            f"in the legacy Plex-first stage); got {row['outcome']!r}"
        )
        # The hint text must surface in the reason field — that's the
        # entire point of porting _detect_path_prefix_mismatches into
        # the unified phase. (file_results store the user-facing string
        # under ``reason``; the row also carries it in case future UIs
        # rename the field.)
        reason = row.get("reason") or row.get("message") or ""
        assert "/mnt" in reason and "/data" in reason, (
            f"Path-mapping mismatch hint did not surface in file_result reason; got {reason!r}"
        )

    def test_multi_path_webhook_each_row_gets_its_own_hint(self, app, tmp_path):
        """Audit P4 — multi-path webhook with DIFFERENT mismatches per
        path: each file_result row must carry the hint for THAT path,
        not slot 0 of the flat list.

        Pre-fix bug: ``unresolved_detail = path_hints[0]`` smeared the
        same hint across every row in the Files panel. A user looking
        at a 2-path webhook (one Movies, one TV with different prefix
        mismatches) saw both rows reporting the Movies hint — wrong
        diagnosis on N-1 of N rows.
        """
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        movies_hint = "Possible prefix mismatch: webhook sends '/mnt/movies' …"
        tv_hint = "Possible prefix mismatch: webhook sends '/mnt/tv' …"

        def fake_run_processing(config, selected_gpus, **kwargs):
            return {
                "outcome": {"generated": 0, "failed": 0},
                "webhook_resolution": {
                    "unresolved_paths": ["/mnt/movies/Foo.mkv", "/mnt/tv/Bar.mkv"],
                    "skipped_paths": [],
                    "resolved_count": 0,
                    "total_paths": 2,
                    "path_hints": [movies_hint, tv_hint],
                    "path_hint_map": {
                        "/mnt/movies/Foo.mkv": movies_hint,
                        "/mnt/tv/Bar.mkv": tv_hint,
                    },
                },
            }

        with (
            app.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=fake_run_processing,
            ),
        ):
            job = get_job_manager().create_job(library_name="Movies", config={})
            _start_job_async(
                job.id,
                config_overrides={
                    "webhook_paths": ["/mnt/movies/Foo.mkv", "/mnt/tv/Bar.mkv"],
                    "webhook_retry_count": 0,
                },
            )

        results = get_job_manager().get_file_results(job.id)
        by_path = {r["file"]: r for r in results}

        assert "/mnt/movies/Foo.mkv" in by_path and "/mnt/tv/Bar.mkv" in by_path, (
            f"both unresolved paths must produce file_result rows; got {list(by_path.keys())!r}"
        )
        movies_reason = by_path["/mnt/movies/Foo.mkv"].get("reason") or ""
        tv_reason = by_path["/mnt/tv/Bar.mkv"].get("reason") or ""
        assert "/mnt/movies" in movies_reason, (
            f"Movies row must show Movies hint, not the TV one. row.reason={movies_reason!r}"
        )
        assert "/mnt/tv" in tv_reason, (
            f"TV row must show TV hint, not the Movies one. row.reason={tv_reason!r}. "
            "This is the audit-P4 regression: pre-fix the row would show whatever "
            "hint landed at path_hints[0] regardless of which path it's about."
        )

    def test_legacy_resolution_source_field_absence_does_not_crash(self, app, tmp_path):
        """A payload missing ``resolution_source`` must not raise — the
        result handler used to do ``resolution.get('resolution_source') or 'plex'``
        and switch on the value. Post-unification, the field is gone;
        a regression that re-introduces a hard ``resolution['resolution_source']``
        access would silently AttributeError on every webhook job.
        """
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        def fake_run_processing(config, selected_gpus, **kwargs):
            # Brand-new minimal payload — no resolution_source, no path_hints.
            return {
                "outcome": {"generated": 0, "failed": 0},
                "webhook_resolution": {
                    "unresolved_paths": ["/data/X.mkv"],
                    "skipped_paths": [],
                    "resolved_count": 0,
                    "total_paths": 1,
                    "path_hints": [],
                },
            }

        with (
            app.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=fake_run_processing,
            ),
        ):
            job = get_job_manager().create_job(library_name="Movies", config={})
            # Must not raise.
            _start_job_async(
                job.id,
                config_overrides={
                    "webhook_paths": ["/data/X.mkv"],
                    "webhook_retry_count": 0,
                },
            )

        # Job reached a terminal state (didn't get stuck on a missing-field crash).
        final = get_job_manager().get_job(job.id)
        assert final is not None
        assert final.status.value in ("completed", "failed", "cancelled"), (
            f"Job must reach terminal state with the simplified payload; got {final.status.value!r}. "
            "An AttributeError on resolution_source would land the job in 'running' or 'failed'."
        )
        # And the file_result for the unresolved path was still written.
        results = get_job_manager().get_file_results(job.id)
        assert any(r.get("file") == "/data/X.mkv" for r in results), (
            "Unresolved path's file_result missing — the result handler bailed before recording it."
        )


class TestRetryReasonPersistedOnSpawn:
    """When a retry is spawned, the structured reason that triggered it
    MUST land on the spawned child's ``config["retry_reason"]``. The
    modal's "Retried N× because…" banner reads this field instead of
    re-deriving from the live ``publishers`` snapshot — so a chain that
    SUCCEEDS still shows why it ever needed to retry (job ``4ad23f43``,
    2026-05-15: chain completed but the modal banner was blank because
    ``merge_chain_publishers_best_per_path`` had refreshed every snapshot
    to ``published`` / ``skipped_output_exists`` and ``_pending_servers()``
    correctly returned ``[]``).

    Three categories the spawner must capture, mirroring the WARNING log
    line built immediately above the ``_spawn_retry_job`` call:
        * ``pending_by_server`` — per-server ``{name: count}`` for files
          stuck in publisher-pending status (the publisher case).
        * ``stale_paths`` — count of paths Plex returned but that don't
          exist on disk (skipped_file_not_found).
        * ``unresolved`` — count of paths Plex couldn't resolve at all.

    Cover the matrix: each non-zero category alone, then a mixed case.
    Without per-cell coverage, a refactor that drops ``stale_paths`` from
    the persisted dict (or only persists ``pending_by_server``) passes the
    publisher-only test and silently breaks the Plex-resolution case the
    UI needs covered.
    """

    @pytest.mark.parametrize(
        "publisher_status",
        [
            "published_pending_registration",
            "skipped_not_indexed",
            "skipped_not_in_library",
        ],
    )
    def test_publisher_pending_persists_pending_by_server(self, app, tmp_path, publisher_status):
        """Each retry-eligible publisher status must persist a
        ``pending_by_server`` entry naming the actual blocker server.
        The modal's chain-summary subtitle aggregates these across
        attempts to render "Retried Nx because <server> was still indexing".
        """
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        run_calls: list[dict] = []

        def fake_run_processing(config, selected_gpus, **kwargs):
            run_calls.append({"job_id": kwargs.get("job_id")})
            jm = get_job_manager()
            target = (jm.get_job(kwargs.get("job_id")).config or {}).get("parent_job_id") or kwargs.get("job_id")
            if len(run_calls) == 1:
                # Initial dispatch: file's publisher returned the
                # retry-eligible status. Use FULL publisher keys
                # (``server_id``/``server_name``/``server_type``) — that's
                # the contract ``record_file_result`` translates from.
                jm.record_file_result(
                    target,
                    "/data/tv/Show/S01E01.mkv",
                    "generated",
                    "",
                    "[GPU 0]",
                    servers=[
                        {
                            "server_id": "jelly-1",
                            "server_name": "JellyTest",
                            "server_type": "jellyfin",
                            "status": publisher_status,
                            "message": f"server returned {publisher_status}",
                        }
                    ],
                )
            else:
                # Retry: registration succeeds — fresh row so the
                # retry-decision scan doesn't loop forever.
                jm.record_file_result(
                    target,
                    "/data/tv/Show/S01E01.mkv",
                    "generated",
                    "",
                    "[GPU 0]",
                    servers=[
                        {
                            "server_id": "jelly-1",
                            "server_name": "JellyTest",
                            "server_type": "jellyfin",
                            "status": "published",
                            "message": "registered after retry",
                        }
                    ],
                )
            return {
                "outcome": {"generated": 1},
                "webhook_resolution": {
                    "unresolved_paths": [],
                    "skipped_paths": [],
                    "resolved_count": 1,
                    "total_paths": 1,
                    "path_hints": [],
                },
            }

        with (
            app.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=fake_run_processing,
            ),
            patch(
                "media_preview_generator.plex_client.trigger_plex_partial_scan",
                return_value=[],
            ),
            patch(
                "media_preview_generator.processing.retry_queue.BACKOFF_SCHEDULE",
                [1, 1, 1, 1, 1],
            ),
        ):
            jm = get_job_manager()
            job = jm.create_job(library_name="The Show", config={"source": "sonarr"})
            _start_job_async(
                job.id,
                config_overrides={
                    "webhook_paths": ["/data/tv/Show/S01E01.mkv"],
                    "webhook_retry_count": 3,
                    "webhook_retry_delay": 30,
                },
            )

        all_jobs = get_job_manager().get_all_jobs()
        retry_children = [
            j
            for j in all_jobs
            if (j.config or {}).get("is_retry") is True and (j.config or {}).get("parent_job_id") == job.id
        ]
        assert len(retry_children) == 1, (
            f"{publisher_status} must spawn exactly 1 retry; got {len(retry_children)}: "
            f"{[(j.id, j.library_name) for j in retry_children]}"
        )
        reason = (retry_children[0].config or {}).get("retry_reason")
        assert reason is not None, (
            f"{publisher_status}: child must carry retry_reason in config so the modal "
            f"banner can render after chain success; got config={retry_children[0].config!r}"
        )
        # Publisher-pending case: pending_by_server populated, the
        # other two categories zero. Asserting on every field — not just
        # pending_by_server — so a regression that fills this dict but
        # also accidentally double-counts paths into stale_paths fails
        # here instead of producing a confusing UI line.
        assert reason["pending_by_server"] == {"JellyTest": 1}, (
            f"pending_by_server must name the actual blocker; got {reason['pending_by_server']!r}"
        )
        assert reason["unresolved"] == 0, (
            f"Publisher-pending case must report 0 unresolved; got {reason['unresolved']!r}"
        )
        assert reason["stale_paths"] == 0, (
            f"Publisher-pending case must report 0 stale_paths; got {reason['stale_paths']!r}"
        )

    def test_skipped_file_not_found_persists_stale_paths(self, app, tmp_path):
        """The not-found-on-disk branch (Plex returned a stale path)
        must persist a non-zero ``stale_paths`` count. This is the
        category that the live-publisher fallback CAN'T cover — there
        IS no publisher status to derive it from — so persisting it
        here is the only way the modal banner can ever render for this
        retry trigger.
        """
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        run_calls: list[dict] = []

        def fake_run_processing(config, selected_gpus, **kwargs):
            run_calls.append({"job_id": kwargs.get("job_id")})
            jm = get_job_manager()
            if len(run_calls) == 1:
                jm.record_file_result(
                    kwargs.get("job_id"),
                    "/data/tv/Show/S01E01.mkv",
                    "skipped_file_not_found",
                    "file gone",
                    "[GPU 0]",
                )
                return {
                    "outcome": {"skipped_file_not_found": 1, "generated": 0, "failed": 0},
                    "webhook_resolution": {
                        "unresolved_paths": [],
                        "skipped_paths": [],
                        "resolved_count": 1,
                        "total_paths": 1,
                        "path_hints": [],
                    },
                }
            parent_id = (jm.get_job(kwargs.get("job_id")).config or {}).get("parent_job_id") or kwargs.get("job_id")
            jm.record_file_result(parent_id, "/data/tv/Show/S01E01.mkv", "generated", "", "[GPU 0]")
            return {
                "outcome": {"generated": 1},
                "webhook_resolution": {
                    "unresolved_paths": [],
                    "skipped_paths": [],
                    "resolved_count": 1,
                    "total_paths": 1,
                    "path_hints": [],
                },
            }

        with (
            app.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=fake_run_processing,
            ),
            patch(
                "media_preview_generator.plex_client.trigger_plex_partial_scan",
                return_value=[],
            ),
            patch(
                "media_preview_generator.processing.retry_queue.BACKOFF_SCHEDULE",
                [1, 1, 1, 1, 1],
            ),
        ):
            job = get_job_manager().create_job(library_name="The Show", config={"source": "sonarr"})
            _start_job_async(
                job.id,
                config_overrides={
                    "webhook_paths": ["/data/tv/Show/S01E01.mkv"],
                    "webhook_retry_count": 3,
                    "webhook_retry_delay": 30,
                },
            )

        retry_children = [
            j
            for j in get_job_manager().get_all_jobs()
            if (j.config or {}).get("is_retry") is True and (j.config or {}).get("parent_job_id") == job.id
        ]
        assert len(retry_children) == 1, f"Expected 1 retry child; got {len(retry_children)}"
        reason = (retry_children[0].config or {}).get("retry_reason")
        assert reason is not None, f"Stale-path case must persist retry_reason; got config={retry_children[0].config!r}"
        # Full-dict assertion (not just stale_paths >= 1) so a
        # regression that double-counts the stale path into both
        # ``stale_paths`` AND ``unresolved`` fails here. Mirrors the
        # publisher-pending test's exact-shape contract.
        assert reason == {
            "unresolved": 0,
            "stale_paths": 1,
            "pending_by_server": {},
        }, f"Stale-path case must produce exactly one stale path with no other categories; got {reason!r}"


# ---------------------------------------------------------------------------
# How a job and its retry chain end: badge, message, and which files a retry runs
# ---------------------------------------------------------------------------


def _all_resolved(count: int, input_by_canonical: dict | None = None) -> dict:
    payload = {
        "unresolved_paths": [],
        "skipped_paths": [],
        "resolved_count": count,
        "total_paths": count,
        "path_hints": [],
    }
    if input_by_canonical is not None:
        payload["input_by_canonical"] = input_by_canonical
    return payload


def _chain_head_id(jm, job_id: str) -> str:
    return (jm.get_job(job_id).config or {}).get("parent_job_id") or job_id


def _retries_of(jm, head_id: str) -> list:
    return sorted(
        (j for j in jm.get_all_jobs() if (j.config or {}).get("parent_job_id") == head_id),
        key=lambda j: j.config["retry_attempt"],
    )


def _plex_row(status: str) -> list[dict]:
    return [{"server_id": "plex-1", "server_name": "Plex Main", "server_type": "plex", "status": status}]


@contextmanager
def _chain_ends(jm):
    """Record how each chain end left the chain head: ``(outcome, status, error, successes)``, the successes
    being those an exhausted chain was told of (None for a completed one: its status says what they were).

    Read at the moment the chain ends, not afterwards: under the sync-thread shim a retry runs nested inside the
    job that queued it, so that job's own completion lands after its chain has already ended (in production the
    retry starts on its own thread, after its delay).
    """
    ends: list[tuple] = []
    real = jm.upsert_retry_chain_job

    def record(**kwargs):
        job = real(**kwargs)
        if kwargs["outcome"] in ("completed", "exhausted"):
            successes = kwargs.get("successes", 0) if kwargs["outcome"] == "exhausted" else None
            ends.append((kwargs["outcome"], job.status.value, job.error, successes))
        return job

    with patch.object(jm, "upsert_retry_chain_job", side_effect=record):
        yield ends


@pytest.fixture()
def no_retry_wait():
    """Retries start at once instead of counting down their backoff."""
    with (
        patch("media_preview_generator.plex_client.trigger_plex_partial_scan", return_value=[]),
        patch("media_preview_generator.web.routes.job_runner._is_force_fire_now_set", return_value=True),
    ):
        yield


class TestFilesNotFoundBesideSuccesses:
    """Nightly scan c091d637: 0 generated, 115,831 already existed, 6 not found, 2 failed. It ended red "Failed …
    check path mapping configuration", though working path mappings had found 115,831 files."""

    def test_scan_with_a_few_files_not_found_ends_amber_with_a_plain_message(self, app, tmp_path):
        from media_preview_generator.web.jobs import JobStatus, get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        def fake_run_processing(config, selected_gpus, **kwargs):
            get_job_manager().record_file_result(
                kwargs["job_id"], "/data/tv/Gone/S01E01.mkv", "skipped_file_not_found", "file gone", "Library scan"
            )
            return {"outcome": {"skipped_bif_exists": 115831, "skipped_file_not_found": 6, "failed": 2}}

        with (
            app.app_context(),
            patch("media_preview_generator.jobs.orchestrator.run_processing", side_effect=fake_run_processing),
        ):
            jm = get_job_manager()
            job = jm.create_job(library_name="TV Shows", config={})
            _start_job_async(job.id, config_overrides=None)
            finished = jm.get_job(job.id)
            logs = jm.get_logs(job.id) or []
            retries = _retries_of(jm, job.id)

        assert finished.status is JobStatus.COMPLETED
        summary = "2 file(s) failed. 6 file(s) weren't found on disk. 115,831 of 115,839 file(s) were fine"
        assert finished.error == summary
        # Said once in the job log, not clause by clause and then again joined.
        assert [line.split(" - ", 1)[1] for line in logs if "weren't found on disk" in line or "failed" in line] == [
            summary,
            f"Job {job.id} completed with warnings: {summary}",
        ], logs
        assert not any("path mapping" in line for line in logs), logs
        # A scan's own not-found files have no webhook path to resubmit: no retry is queued for them.
        assert retries == []

    def test_scan_where_nothing_succeeded_still_fails_and_points_at_path_mappings(self, app, tmp_path):
        from media_preview_generator.web.jobs import JobStatus, get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        def fake_run_processing(config, selected_gpus, **kwargs):
            return {"outcome": {"skipped_file_not_found": 6}}

        with (
            app.app_context(),
            patch("media_preview_generator.jobs.orchestrator.run_processing", side_effect=fake_run_processing),
        ):
            jm = get_job_manager()
            job = jm.create_job(library_name="TV Shows", config={})
            _start_job_async(job.id, config_overrides=None)
            finished = jm.get_job(job.id)

        assert finished.status is JobStatus.FAILED
        assert finished.error == "6 of 6 items skipped (file not found locally) — check path mapping configuration"


class TestRetryRunsOnlyTheUnresolvedFiles:
    """Production chains 861c0385 ("10 files": 6 done, 4 not found, every retry re-ran all 10 and the chain ended
    "did not register 10 file(s)") and f928a863 ("Retry: 18 files" ran 6)."""

    @pytest.mark.parametrize("mapped", [False, True], ids=["same-path", "webhook-path-mapped"])
    def test_one_done_one_not_found_retries_just_the_missing_file(self, app, tmp_path, no_retry_wait, mapped):
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        raw = {"done": "/data/tv/Show/S01E01.mkv", "missing": "/data/tv/Show/S01E02.mkv"}
        prefix = "/mnt/disk3" if mapped else ""
        canonical = {key: prefix + path for key, path in raw.items()}
        run_calls: list[list[str]] = []

        def resolution(keys):
            # The same-path cell also covers a payload without the map (file rows keyed by the webhook's own path).
            return _all_resolved(len(keys), {canonical[k]: raw[k] for k in keys} if mapped else None)

        def fake_run_processing(config, selected_gpus, **kwargs):
            run_calls.append(list(config.webhook_paths or []))
            jm = get_job_manager()
            head_id = _chain_head_id(jm, kwargs["job_id"])
            if len(run_calls) == 1:
                jm.record_file_result(head_id, canonical["done"], "skipped_bif_exists", "", "Checking")
                jm.record_file_result(head_id, canonical["missing"], "skipped_file_not_found", "file gone", "Checking")
                return {
                    "outcome": {"skipped_bif_exists": 1, "skipped_file_not_found": 1},
                    "webhook_resolution": resolution(["done", "missing"]),
                }
            jm.record_file_result(head_id, canonical["missing"], "generated", "", "[GPU 0]")
            return {"outcome": {"generated": 1}, "webhook_resolution": resolution(["missing"])}

        with (
            app.app_context(),
            patch("media_preview_generator.jobs.orchestrator.run_processing", side_effect=fake_run_processing),
        ):
            jm = get_job_manager()
            job = jm.create_job(library_name="2 files", config={"source": "sonarr"})
            with _chain_ends(jm) as ends:
                _start_job_async(
                    job.id,
                    config_overrides={
                        "webhook_paths": list(raw.values()),
                        "webhook_retry_count": 3,
                        "webhook_retry_delay": 30,
                    },
                )
            head_logs = jm.get_logs(job.id) or []
            retries = _retries_of(jm, job.id)

        assert run_calls == [list(raw.values()), [raw["missing"]]]
        assert [r.library_name for r in retries] == ["Retry: 1 file"]
        assert retries[0].config["path_count"] == 1
        assert retries[0].config["retry_reason"] == {"unresolved": 0, "stale_paths": 1, "pending_by_server": {}}
        assert any(line.endswith("1 file(s) weren't found on disk. 1 path(s) sent for retry") for line in head_logs), (
            head_logs
        )
        assert not any("2 path(s)" in line or "path mapping" in line for line in head_logs), head_logs
        # The job's own log is for its user: job bookkeeping stays out of it (the retry is announced in plain words).
        assert any("(retry 1 of 3)" in line for line in head_logs), head_logs
        assert not any("Created job" in line or "chain is active" in line for line in head_logs), head_logs
        assert ends == [("completed", "completed", None, None)]

    def test_a_scans_own_not_found_files_do_not_make_its_retry_run_again(self, app, tmp_path, no_retry_wait):
        """Scan 55b098af had 27 files not on disk and 1 not indexed. Its retries ran the 1 file, but each one saw
        the scan's 27 not-found rows, resubmitted its own path and the chain ran out. The retry of the indexed-late
        file ends the chain; the scan's not-found files stay on its row as a warning, not wiped to green.
        """
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        late = "/data/tv/Dateline/S34E01.mkv"
        run_calls: list[list[str]] = []

        def fake_run_processing(config, selected_gpus, **kwargs):
            run_calls.append(list(getattr(config, "webhook_paths", None) or []))
            jm = get_job_manager()
            head_id = _chain_head_id(jm, kwargs["job_id"])
            if len(run_calls) == 1:
                jm.record_file_result(head_id, "/data/tv/Old/S01E01.mkv", "skipped_bif_exists", "", "Library scan")
                jm.record_file_result(
                    head_id, "/data/tv/Gone/S01E01.mkv", "skipped_file_not_found", "file gone", "Library scan"
                )
                jm.record_file_result(
                    head_id, late, "skipped_not_indexed", "", "[GPU 0]", servers=_plex_row("skipped_not_indexed")
                )
                return {"outcome": {"skipped_bif_exists": 1, "skipped_file_not_found": 1, "skipped_not_indexed": 1}}
            jm.record_file_result(
                head_id, late, "skipped_bif_exists", "", "Checking", servers=_plex_row("skipped_output_exists")
            )
            return {"outcome": {"skipped_bif_exists": 1}, "webhook_resolution": _all_resolved(1, {late: late})}

        with (
            app.app_context(),
            patch("media_preview_generator.jobs.orchestrator.run_processing", side_effect=fake_run_processing),
        ):
            jm = get_job_manager()
            job = jm.create_job(library_name="TV Shows", config={})
            with _chain_ends(jm) as ends:
                _start_job_async(job.id, config_overrides=None)
            head_logs = jm.get_logs(job.id) or []
            retries = _retries_of(jm, job.id)

        assert run_calls == [[], [late]]
        assert [r.config["retry_reason"] for r in retries] == [
            {"unresolved": 0, "stale_paths": 0, "pending_by_server": {"Plex Main": 1}}
        ]
        assert any(line.endswith("Plex Main pending × 1, retry scheduled in 60s (retry 1 of 3)") for line in head_logs)
        assert ends == [("completed", "completed", "1 file(s) weren't found on disk", None)]


class TestExhaustedChainEnd:
    """An exhausted chain is red only when none of its files succeeded, and counts only the files still missing."""

    GAVE_UP_ONE = (
        "1 file(s) still weren't indexed by the media server after 2 retries, so no more retries are queued. "
        "The next scheduled scan will pick them up."
    )

    def _run_chain(self, app, first_rows: dict[str, str], retry_rows: dict[int, dict[str, str]], retry_count: int):
        """Run a webhook job whose files end as ``first_rows`` ({path: outcome}), then ``retry_rows[attempt]``."""
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        run_calls: list[list[str]] = []

        def fake_run_processing(config, selected_gpus, **kwargs):
            run_calls.append(list(config.webhook_paths or []))
            jm = get_job_manager()
            head_id = _chain_head_id(jm, kwargs["job_id"])
            rows = first_rows if len(run_calls) == 1 else retry_rows[len(run_calls) - 1]
            outcome: dict[str, int] = {}
            for path, result in rows.items():
                servers = _plex_row("skipped_not_in_library") if result == "skipped_not_indexed" else None
                jm.record_file_result(head_id, path, result, "", "[GPU 0]", servers=servers)
                outcome[result] = outcome.get(result, 0) + 1
            return {"outcome": outcome, "webhook_resolution": _all_resolved(len(rows))}

        with (
            app.app_context(),
            patch("media_preview_generator.jobs.orchestrator.run_processing", side_effect=fake_run_processing),
        ):
            jm = get_job_manager()
            job = jm.create_job(library_name=f"{len(first_rows)} files", config={"source": "sonarr"})
            with _chain_ends(jm) as ends:
                _start_job_async(
                    job.id,
                    config_overrides={
                        "webhook_paths": list(first_rows),
                        "webhook_retry_count": retry_count,
                        "webhook_retry_delay": 30,
                    },
                )
            logs = {r.config["retry_attempt"]: jm.get_logs(r.id) or [] for r in _retries_of(jm, job.id)}
        return run_calls, ends, logs

    @pytest.mark.parametrize("still", ["skipped_file_not_found", "skipped_not_indexed"])
    @pytest.mark.parametrize(
        ("done_outcome", "status", "successes"),
        [("skipped_bif_exists", "completed", 1), ("generated", "completed", 1), (None, "failed", 0)],
        ids=["one-already-existed-amber", "one-generated-amber", "nothing-succeeded-red"],
    )
    def test_chain_head_badge_and_count(self, app, tmp_path, no_retry_wait, still, done_outcome, status, successes):
        a, b = "/data/tv/Show/S01E01.mkv", "/data/tv/Show/S01E02.mkv"
        first = {a: done_outcome, b: still} if done_outcome else {b: still}

        run_calls, ends, _logs = self._run_chain(app, first, {1: {b: still}, 2: {b: still}}, retry_count=2)

        assert run_calls == [list(first), [b], [b]]
        assert ends == [
            ("exhausted", status, f"{self.GAVE_UP_ONE} Check the Files panel for the affected paths.", successes)
        ]

    def test_a_file_a_retry_finished_counts_as_a_success_of_the_chain(self, app, tmp_path, no_retry_wait):
        """Chain ca328838: 8 files none indexed at first, 3 finished on retries, 5 never were. The head's own run
        had no successes, but the chain did: amber, counting the files still waiting."""
        a, b = "/data/tv/Show/S01E01.mkv", "/data/tv/Show/S01E02.mkv"
        waiting = "skipped_not_indexed"

        run_calls, ends, _logs = self._run_chain(
            app, {a: waiting, b: waiting}, {1: {a: "generated", b: waiting}, 2: {b: waiting}}, retry_count=2
        )

        assert run_calls == [[a, b], [a, b], [b]]
        assert ends == [
            ("exhausted", "completed", f"{self.GAVE_UP_ONE} Check the Files panel for the affected paths.", 1)
        ]

    @pytest.mark.parametrize(
        ("done_outcome", "status"),
        [("generated", "completed"), (None, "failed")],
        ids=["one-generated-amber", "nothing-succeeded-red"],
    )
    def test_a_chain_whose_retry_failed_its_file_is_red_only_when_nothing_succeeded(
        self, app, tmp_path, no_retry_wait, done_outcome, status
    ):
        """A webhook's file wasn't found, the retry found it and FFmpeg failed: nothing is left to retry, so the chain
        ends "completed" with the failure as its leftover. With no success in the chain that is a total failure."""
        a, b = "/data/tv/Show/S01E01.mkv", "/data/tv/Show/S01E02.mkv"
        first = {a: done_outcome, b: "skipped_file_not_found"} if done_outcome else {b: "skipped_file_not_found"}

        run_calls, ends, _logs = self._run_chain(app, first, {1: {b: "failed"}}, retry_count=2)

        assert run_calls == [list(first), [b]]
        assert ends == [("completed", status, "1 file(s) failed", None)]

    def test_the_gave_up_line_is_the_chains_one_warning(self, app, tmp_path, no_retry_wait):
        """The last retry says once, as a WARNING, that it gave up; the earlier retry doesn't say it at all."""
        b = "/data/tv/Show/S01E02.mkv"
        waiting = "skipped_not_indexed"

        _runs, _ends, logs = self._run_chain(app, {b: waiting}, {1: {b: waiting}, 2: {b: waiting}}, 2)

        # complete_job's own "Job <id> completed with warnings: …" app-log line repeats the text at INFO.
        said = {
            attempt: [
                line.split("] ", 1)[1]
                for line in lines
                if "still weren't indexed" in line and "completed with warnings" not in line
            ]
            for attempt, lines in logs.items()
        }
        assert said == {1: [], 2: [f"WARNING - {self.GAVE_UP_ONE}"]}
        warnings = [line for line in logs[2] if "WARNING" in line and "still weren't indexed" in line]
        assert len(warnings) == 1, warnings


class TestRetryRowPastTheFileResultsCap:
    """Scan 55b098af: retries 2 and 3 found the late file's preview done, but the chain head's Files-panel bucket
    for "already existed" was full, their row was dropped, and the stale "not indexed" row kept the chain retrying
    until it failed."""

    def test_a_retrys_result_is_recorded_on_a_full_bucket_and_ends_the_chain(self, app, tmp_path, no_retry_wait):
        from media_preview_generator.processing.generator import ProcessingResult, _notify_file_result
        from media_preview_generator.web.jobs import JobManager, get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        late = "/data/tv/Dateline/S34E01.mkv"
        run_calls: list[str] = []

        def fake_run_processing(config, selected_gpus, **kwargs):
            # Through the job's own file-result callback, as the workers report: the retry's rows must reach the
            # chain head's file past its cap.
            run_calls.append(kwargs["job_id"])
            if len(run_calls) == 1:
                for i in range(4):
                    _notify_file_result(f"/data/tv/Old/S01E0{i}.mkv", ProcessingResult.SKIPPED_BIF_EXISTS, "", "Scan")
                _notify_file_result(
                    late, ProcessingResult.SKIPPED_NOT_INDEXED, "", "[GPU 0]", servers=_plex_row("skipped_not_indexed")
                )
                return {"outcome": {"skipped_bif_exists": 4, "skipped_not_indexed": 1}}
            _notify_file_result(
                late, ProcessingResult.SKIPPED_BIF_EXISTS, "", "Checking", servers=_plex_row("skipped_output_exists")
            )
            return {"outcome": {"skipped_bif_exists": 1}, "webhook_resolution": _all_resolved(1, {late: late})}

        with (
            app.app_context(),
            patch("media_preview_generator.jobs.orchestrator.run_processing", side_effect=fake_run_processing),
            patch.object(JobManager, "_FILE_RESULTS_PER_OUTCOME_CAP", 2),
        ):
            jm = get_job_manager()
            job = jm.create_job(library_name="TV Shows", config={})
            with _chain_ends(jm) as ends:
                _start_job_async(job.id, config_overrides=None)
            latest = {r["file"]: r["outcome"] for r in jm.get_file_results(job.id) if r["file"]}

        assert len(run_calls) == 2, f"the scan plus one retry expected; got {len(run_calls)} runs"
        assert latest[late] == "skipped_bif_exists"
        assert ends == [("completed", "completed", None, None)]

    @pytest.mark.parametrize(
        ("failed", "not_found", "warning"),
        [
            (3, 0, "3 file(s) failed"),
            (0, 3, "3 file(s) weren't found on disk"),
            (4, 3, "4 file(s) failed. 3 file(s) weren't found on disk"),
            (2, 1, "2 file(s) failed. 1 file(s) weren't found on disk"),  # at and under the cap of 2
        ],
    )
    def test_the_chains_leftover_warning_counts_files_whose_rows_the_cap_dropped(
        self, app, tmp_path, no_retry_wait, failed, not_found, warning
    ):
        """The Files panel lists 5,000 rows per outcome; a scan with more failures than that still failed them all."""
        from media_preview_generator.processing.generator import ProcessingResult, _notify_file_result
        from media_preview_generator.web.jobs import JobManager, get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        late = "/data/tv/Dateline/S34E01.mkv"
        run_calls: list[str] = []

        def fake_run_processing(config, selected_gpus, **kwargs):
            run_calls.append(kwargs["job_id"])
            if len(run_calls) == 1:
                _notify_file_result("/data/tv/Old/S01E01.mkv", ProcessingResult.SKIPPED_BIF_EXISTS, "", "Scan")
                for i in range(failed):
                    _notify_file_result(f"/data/tv/Bad/S01E0{i}.mkv", ProcessingResult.FAILED, "boom", "[GPU 0]")
                for i in range(not_found):
                    _notify_file_result(
                        f"/data/tv/Gone/S01E0{i}.mkv", ProcessingResult.SKIPPED_FILE_NOT_FOUND, "", "Scan"
                    )
                _notify_file_result(
                    late, ProcessingResult.SKIPPED_NOT_INDEXED, "", "[GPU 0]", servers=_plex_row("skipped_not_indexed")
                )
                return {
                    "outcome": {
                        "skipped_bif_exists": 1,
                        "failed": failed,
                        "skipped_file_not_found": not_found,
                        "skipped_not_indexed": 1,
                    }
                }
            _notify_file_result(
                late, ProcessingResult.SKIPPED_BIF_EXISTS, "", "Checking", servers=_plex_row("skipped_output_exists")
            )
            return {"outcome": {"skipped_bif_exists": 1}, "webhook_resolution": _all_resolved(1, {late: late})}

        with (
            app.app_context(),
            patch("media_preview_generator.jobs.orchestrator.run_processing", side_effect=fake_run_processing),
            patch.object(JobManager, "_FILE_RESULTS_PER_OUTCOME_CAP", 2),
        ):
            jm = get_job_manager()
            job = jm.create_job(library_name="TV Shows", config={})
            with _chain_ends(jm) as ends:
                _start_job_async(job.id, config_overrides=None)

        assert len(run_calls) == 2, f"the scan plus one retry expected; got {len(run_calls)} runs"
        assert ends == [("completed", "completed", warning, None)]


class TestJobStartedLoggedOnce:
    """Every job log had two "Job started" lines: one when the job took its slot, one when its files began."""

    def test_one_job_started_line(self, app, tmp_path):
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        def fake_run_processing(config, selected_gpus, **kwargs):
            kwargs["on_dispatch_start"]()
            return {"outcome": {"generated": 1}}

        with (
            app.app_context(),
            patch("media_preview_generator.jobs.orchestrator.run_processing", side_effect=fake_run_processing),
        ):
            jm = get_job_manager()
            job = jm.create_job(library_name="Movies", config={})
            _start_job_async(job.id, config_overrides=None)
            logs = jm.get_logs(job.id) or []

        assert len([line for line in logs if line.endswith("INFO - Job started")]) == 1, logs


class TestJobCreationIsLoggedForTheAppNotTheJob:
    """A job that queues a retry creates it on its own thread. That belongs in app.log (a job that never starts leaves
    no other trace there), not in the Logs panel of the job that queued it."""

    def test_a_retry_queued_by_a_job_is_in_the_app_log_and_in_no_jobs_own_log(self, app, tmp_path, no_retry_wait):
        from loguru import logger

        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        late = "/data/tv/Dateline/S34E01.mkv"
        run_calls: list[str] = []

        def fake_run_processing(config, selected_gpus, **kwargs):
            run_calls.append(kwargs["job_id"])
            jm = get_job_manager()
            done = len(run_calls) > 1
            outcome = "skipped_bif_exists" if done else "skipped_not_indexed"
            row = _plex_row("skipped_output_exists" if done else "skipped_not_indexed")
            jm.record_file_result(_chain_head_id(jm, kwargs["job_id"]), late, outcome, "", "[GPU 0]", servers=row)
            return {"outcome": {outcome: 1}, "webhook_resolution": _all_resolved(1, {late: late})}

        app_log: list[tuple[str, str]] = []
        sink = logger.add(lambda m: app_log.append((m.record["level"].name, m.record["message"])), level="INFO")
        try:
            with (
                app.app_context(),
                patch("media_preview_generator.jobs.orchestrator.run_processing", side_effect=fake_run_processing),
            ):
                jm = get_job_manager()
                job = jm.create_job(library_name="TV Shows", server_name="Plex", config={})
                _start_job_async(job.id, config_overrides=None)
                (retry,) = _retries_of(jm, job.id)
                job_logs = (jm.get_logs(job.id) or []) + (jm.get_logs(retry.id) or [])
        finally:
            logger.remove(sink)

        assert len(run_calls) == 2, f"the scan plus one retry expected; got {len(run_calls)} runs"
        assert [message for level, message in app_log if level == "INFO" and message.startswith("Created ")] == [
            f"Created previews job {job.id} for library TV Shows (server=Plex)",
            f"Created previews job {retry.id} for library Retry: TV Shows (server=Plex)",
        ]
        assert job_logs, "the jobs' own logs were captured"
        assert not [line for line in job_logs if "Created " in line], job_logs


class TestDefaultRetryCount:
    """An install that never stored a retry count gets 5 retries: the whole backoff schedule (about 83 minutes)."""

    def test_a_job_with_no_stored_retry_count_retries_five_times(self, app, tmp_path, no_retry_wait):
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async
        from media_preview_generator.web.settings_manager import get_settings_manager

        late = "/data/tv/Dateline/S34E01.mkv"
        run_calls: list[str] = []

        def fake_run_processing(config, selected_gpus, **kwargs):
            run_calls.append(kwargs["job_id"])
            jm = get_job_manager()
            jm.record_file_result(
                _chain_head_id(jm, kwargs["job_id"]),
                late,
                "skipped_not_indexed",
                "",
                "[GPU 0]",
                servers=_plex_row("skipped_not_indexed"),
            )
            return {"outcome": {"skipped_not_indexed": 1}, "webhook_resolution": _all_resolved(1, {late: late})}

        with (
            app.app_context(),
            patch("media_preview_generator.jobs.orchestrator.run_processing", side_effect=fake_run_processing),
        ):
            get_settings_manager().delete("webhook_retry_count")
            jm = get_job_manager()
            job = jm.create_job(library_name="TV Shows", config={})
            with _chain_ends(jm) as ends:
                _start_job_async(job.id, config_overrides=None)
            retries = _retries_of(jm, job.id)

        assert len(run_calls) == 6, f"the scan plus 5 retries expected; got {len(run_calls)} runs"
        assert [(r.config["retry_attempt"], r.config["max_retries"]) for r in retries] == [(n, 5) for n in range(1, 6)]
        assert ends == [
            (
                "exhausted",
                "failed",
                "1 file(s) still weren't indexed by the media server after 5 retries, so no more retries are queued. "
                "The next scheduled scan will pick them up. Check the Files panel for the affected paths.",
                0,
            )
        ]
