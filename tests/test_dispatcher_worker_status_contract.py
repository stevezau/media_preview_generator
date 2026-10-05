"""Contract tests for ``JobDispatcher._build_worker_statuses``.

The dispatcher's worker-status dict is the source of every worker row
that reaches the UI — both via the live ``worker_callback`` and the
``GET /api/jobs/workers`` polling fallback. Its key set is a hard
contract with two consumers:

* ``web/jobs.py:WorkerStatus`` (dataclass that mirrors the JSON sent
  to the browser).
* ``web/static/js/app.js:updateWorkerCard`` (renders the worker row).

The user-flagged regression "I never saw worker showing ffmpeg %
and speed" was caused by this method silently dropping
``ffmpeg_started`` + ``current_phase``. Without ``ffmpeg_started``
the UI stays in its pre-FFmpeg "Working…" branch *forever* and
hides the speed/ETA chips even though FFmpeg is actively encoding.

The legacy ``WorkerPool.process_items_headless`` path emitted both
fields (worker.py:1239-1240); only the dispatcher path dropped them
— which is the dominant path. These contract tests pin both fields
on the busy AND idle branch so any future field drop (or the
inverse: dropping a field in the headless path) fails fast at
unit-test time, not as a "stuck Working…" UI bug shipped to prod.
"""

import pytest

from media_preview_generator.jobs.dispatcher import (
    JobDispatcher,
    reset_dispatcher,
)
from media_preview_generator.jobs.worker import WorkerPool


class TestBuildWorkerStatusesContract:
    @pytest.fixture(autouse=True)
    def _reset(self):
        reset_dispatcher()
        yield
        reset_dispatcher()

    def test_busy_worker_payload_has_all_ui_required_fields(self):
        """A busy worker's status dict must contain every key the UI
        relies on, including the two fields the regression dropped.

        Using a key-presence assertion (not just a value match) so the
        test fails both ways:
        * Field omitted from the dict literal → ``"ffmpeg_started" in d``
          fails clearly.
        * Field renamed (e.g. ``ffmpegStarted``) → same failure surface.
        """
        pool = WorkerPool(cpu_workers=1, gpu_workers=0, selected_gpus=[])
        dispatcher = JobDispatcher(pool)
        try:
            worker = pool._snapshot_workers()[0]
            worker.is_busy = True
            worker.current_job_id = "preview-job"
            worker.media_file = "/media/Test Movie.mkv"
            worker.current_task = worker.media_file
            worker.media_title = "Test Movie"
            worker.library_name = "Movies"
            worker.progress_percent = 42.5
            worker.speed = "5.2x"
            worker.remaining_time = 30.0
            worker.ffmpeg_started = True
            worker.current_phase = "Encoding frames"
            worker.chapter_progress = {"stage": "extracting", "processed": 2, "total": 5, "ready": 1, "failed": 1}

            statuses = dispatcher._build_worker_statuses()
            assert len(statuses) == 1
            payload = statuses[0]

            # Every key the WorkerStatus dataclass + UI consume.
            required_keys = {
                "worker_id",
                "worker_type",
                "worker_name",
                "status",
                "current_title",
                "library_name",
                "progress_percent",
                "speed",
                "remaining_time",
                "fallback_active",
                "fallback_reason",
                "fallback_title",
                "ffmpeg_started",
                "current_phase",
                "chapter_progress",
            }
            missing = required_keys - payload.keys()
            assert not missing, (
                f"_build_worker_statuses() dropped {missing!r} from busy-worker payload. "
                f"Without ffmpeg_started the UI stays in its pre-FFmpeg 'Working…' branch "
                f"forever and never renders progress %/speed (user-flagged regression). "
                f"Got payload keys={sorted(payload.keys())!r}"
            )

            # Value contract on the regression-class fields: when the
            # underlying worker has ffmpeg_started=True + a phase string,
            # the dispatcher must propagate them — not hard-code False/"".
            assert payload["ffmpeg_started"] is True, (
                f"ffmpeg_started must reflect worker.ffmpeg_started; got {payload['ffmpeg_started']!r}"
            )
            assert payload["current_phase"] == "Encoding frames", (
                f"current_phase must reflect worker.current_phase; got {payload['current_phase']!r}"
            )
            assert payload["chapter_progress"] == worker.chapter_progress
            assert payload["job_id"] == "preview-job"
            assert payload["current_file"] == "/media/Test Movie.mkv"
        finally:
            dispatcher.shutdown()

    def test_idle_worker_payload_clears_phase_and_ffmpeg_started(self):
        """Inverse cell: idle worker must report ffmpeg_started=False
        and an empty current_phase so a stale phase string from the
        previous task can't bleed through and mis-render an idle row.
        """
        pool = WorkerPool(cpu_workers=1, gpu_workers=0, selected_gpus=[])
        dispatcher = JobDispatcher(pool)
        try:
            worker = pool._snapshot_workers()[0]
            # Simulate the residue of a finished task: the fields are
            # still set on the Worker object but is_busy is False.
            worker.is_busy = False
            worker.current_job_id = "finished-job"
            worker.media_file = "/media/previous.mkv"
            worker.ffmpeg_started = True  # leftover from prior task
            worker.current_phase = "Encoding frames"  # leftover
            worker.chapter_progress = {"stage": "complete", "processed": 5, "total": 5, "ready": 5, "failed": 0}

            statuses = dispatcher._build_worker_statuses()
            assert len(statuses) == 1
            payload = statuses[0]

            assert payload["status"] == "idle"
            assert payload["ffmpeg_started"] is False, (
                "Idle workers must report ffmpeg_started=False — otherwise "
                "the row inherits the previous task's pre-FFmpeg 'Working…' branch."
            )
            assert payload["current_phase"] == "", (
                f"Idle workers must clear current_phase; got {payload['current_phase']!r}"
            )
            assert payload["chapter_progress"] is None
            assert payload["job_id"] is None
            assert payload["current_file"] == ""
        finally:
            dispatcher.shutdown()

    @pytest.mark.parametrize("is_busy", [True, False], ids=["mid-rerun", "rerun-finished"])
    def test_fallback_payload_names_the_file_that_fell_back(self, is_busy):
        """The "Switched to CPU" toast quotes ``fallback_title``. It must survive
        the task finishing: a short clip's CPU rerun ends inside one 1s poll, so
        the first poll that sees ``fallback_active`` already finds the worker
        idle with ``current_title`` blanked (live: ``fell back to CPU for "this file"``).
        """
        pool = WorkerPool(cpu_workers=0, gpu_workers=1, selected_gpus=[("nvidia", "/dev/nvidia0", {})])
        dispatcher = JobDispatcher(pool)
        try:
            worker = pool._snapshot_workers()[0]
            worker.is_busy = is_busy
            worker.media_title = "AV1 Clip 3 (2019)"
            worker.current_task = "/media/AV1 Clip 3.mkv" if is_busy else None
            worker.fallback_active = True
            worker.fallback_reason = "GPU processing failed (exit code 255)"

            payload = dispatcher._build_worker_statuses()[0]

            assert payload["fallback_active"] is True
            assert payload["fallback_reason"] == "GPU processing failed (exit code 255)"
            assert payload["fallback_title"] == "AV1 Clip 3 (2019)"
            assert payload["current_title"] == ("AV1 Clip 3 (2019)" if is_busy else "")
        finally:
            dispatcher.shutdown()

    def test_fallback_title_is_blank_without_a_fallback(self):
        pool = WorkerPool(cpu_workers=1, gpu_workers=0, selected_gpus=[])
        dispatcher = JobDispatcher(pool)
        try:
            worker = pool._snapshot_workers()[0]
            worker.is_busy = True
            worker.media_title = "Some movie"

            payload = dispatcher._build_worker_statuses()[0]

            assert payload["fallback_active"] is False
            assert payload["fallback_title"] == ""
        finally:
            dispatcher.shutdown()

    def test_reserved_slot_never_reports_the_previous_task_as_running(self):
        pool = WorkerPool(cpu_workers=1, gpu_workers=0, selected_gpus=[])
        dispatcher = JobDispatcher(pool)
        try:
            worker = pool._snapshot_workers()[0]
            worker.is_busy = True
            worker.current_task = None
            worker.current_job_id = "previous-job"
            worker.media_file = "/media/previous.mkv"
            worker.media_title = "Previous movie"
            worker.library_name = "Previous library"
            worker.progress_percent = 99
            worker.ffmpeg_started = True
            worker.current_phase = "Publishing"
            payload = dispatcher._build_worker_statuses()[0]
            assert payload["status"] == "processing"
            assert payload["job_id"] is None and payload["current_file"] == ""
            assert payload["current_title"] == "" and payload["library_name"] == ""
            assert payload["progress_percent"] == 0
            assert payload["current_phase"] == "" and not payload["ffmpeg_started"]
        finally:
            worker.is_busy = False
            dispatcher.shutdown()
