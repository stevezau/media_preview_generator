"""A GPU that keeps falling back to the CPU: the per-GPU streak tracker, its worker hook and the job's tally."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.job_kinds import ItemOutcome
from media_preview_generator.jobs import gpu_fallback
from media_preview_generator.jobs.dispatcher import JobDispatcher, JobTracker
from media_preview_generator.jobs.gpu_fallback import (
    GPU_FALLBACK_STREAK,
    GpuFallbackTracker,
    get_gpu_fallback_tracker,
    notification_id,
)
from media_preview_generator.jobs.worker import Worker, WorkerPool
from media_preview_generator.processing import CancellationError, CodecNotSupportedError
from media_preview_generator.processing.multi_server import PublisherResult, PublisherStatus
from media_preview_generator.processing.types import ProcessableItem
from tests.conftest import _ms, _pi

KEYS = ("markers_published", "failed")


@pytest.fixture(autouse=True)
def _fresh_tracker():
    get_gpu_fallback_tracker().reset()
    yield
    get_gpu_fallback_tracker().reset()


def _fallbacks(tracker: GpuFallbackTracker, key: str, n: int, reason: str = "hevc") -> None:
    for _ in range(n):
        tracker.record(key, "GPU A", fell_back=True, reason=reason)


class TestGpuFallbackTracker:
    def test_five_fallbacks_in_a_row_flag_the_gpu(self):
        tracker = GpuFallbackTracker()
        _fallbacks(tracker, "/dev/dri/renderD128", GPU_FALLBACK_STREAK - 1)
        assert tracker.flagged() == []

        tracker.record("/dev/dri/renderD128", "GPU A", fell_back=True, reason="hevc not supported")

        (state,) = tracker.flagged()
        assert (state.key, state.name, state.streak, state.last_reason) == (
            "/dev/dri/renderD128",
            "GPU A",
            5,
            "hevc not supported",
        )

    def test_an_ok_file_in_between_resets_the_count(self):
        tracker = GpuFallbackTracker()
        _fallbacks(tracker, "k", 4)
        tracker.record("k", "GPU A", fell_back=False, reason=None)
        _fallbacks(tracker, "k", 4)
        assert tracker.flagged() == []
        tracker.record("k", "GPU A", fell_back=True, reason="x")
        assert [s.streak for s in tracker.flagged()] == [5]

    def test_an_ok_file_after_the_flag_clears_it(self):
        tracker = GpuFallbackTracker()
        _fallbacks(tracker, "k", 7)
        assert [s.streak for s in tracker.flagged()] == [7]
        tracker.record("k", "GPU A", fell_back=False, reason=None)
        assert tracker.flagged() == []

    def test_gpus_are_tracked_apart(self):
        tracker = GpuFallbackTracker()
        _fallbacks(tracker, "a", 5)
        _fallbacks(tracker, "b", 3)
        assert [s.key for s in tracker.flagged()] == ["a"]
        tracker.record("b", "GPU B", fell_back=False, reason=None)
        assert [s.key for s in tracker.flagged()] == ["a"]

    def test_the_streak_keeps_the_latest_reason_redacted(self):
        tracker = GpuFallbackTracker()
        _fallbacks(tracker, "k", 4, reason="old")
        tracker.record("k", "GPU A", fell_back=True, reason="http://plex:32400/x?X-Plex-Token=abc123 refused")
        (state,) = tracker.flagged()
        assert "abc123" not in state.last_reason
        assert state.last_reason.startswith("http://plex:32400/x?X-Plex-Token=")

    def test_records_from_many_threads_are_all_counted(self):
        tracker = GpuFallbackTracker()
        barrier = threading.Barrier(8)

        def run():
            barrier.wait()
            for _ in range(50):
                tracker.record("k", "GPU A", fell_back=True, reason="r")

        threads = [threading.Thread(target=run) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert [s.streak for s in tracker.flagged()] == [400]

    def test_clearing_a_flag_undismisses_its_notification(self):
        tracker = GpuFallbackTracker()
        _fallbacks(tracker, "/dev/dri/renderD128", 5)
        with patch("media_preview_generator.web.notifications.undismiss_session") as undismiss:
            tracker.record("/dev/dri/renderD128", "GPU A", fell_back=False, reason=None)
        undismiss.assert_called_once_with(notification_id("/dev/dri/renderD128"))

    def test_notification_id_is_stable_and_url_safe(self):
        assert notification_id("/dev/dri/renderD128") == "gpu_keeps_failing__dev_dri_renderD128"
        assert notification_id("/dev/dri/renderD128") == notification_id("/dev/dri/renderD128")
        assert notification_id("cuda:0") == "gpu_keeps_failing_cuda_0"


def _run_preview(worker: Worker, process_fn, cancel_check=None) -> None:
    with patch("media_preview_generator.processing.multi_server.process_canonical_path", side_effect=process_fn):
        worker.assign_task(_pi("f", title="Film"), MagicMock(cpu_threads=0), MagicMock(), cancel_check=cancel_check)
        worker.current_thread.join(timeout=5)
    worker.check_completion()  # what the dispatcher does before it hands the worker its next item


def _run_custom(worker: Worker, process_fn, cancel_check=None) -> None:
    with patch("media_preview_generator.jobs.worker._notify_file_result"):
        worker.assign_task(
            ProcessableItem(canonical_path="/m/t.mkv", server_id="s1", title="t"),
            MagicMock(cpu_threads=0),
            MagicMock(),
            job_id="j",
            cancel_check=cancel_check,
            process_fn=process_fn,
            outcome_keys=KEYS,
        )
        worker.current_thread.join(timeout=5)
    worker.check_completion()


def _published(frame_source: str, status: PublisherStatus = PublisherStatus.PUBLISHED):
    """A preview result whose one publisher row says where its frames came from."""
    result = _ms("generated")
    result.publishers = [PublisherResult("plex-1", "Plex", "bif", status, frame_source=frame_source)]
    return result


def _gpu_then_cpu(*args, gpu=None, **kwargs):
    if gpu is not None:
        raise CodecNotSupportedError("Codec not supported by GPU")
    return _published("extracted")


def _gpu_ok(*args, **kwargs):
    return _published("extracted")


def _cache_hit(*args, **kwargs):
    return _published("cache_hit")


def _output_existed(*args, **kwargs):
    return _published("output_existed", PublisherStatus.SKIPPED_OUTPUT_EXISTS)


def _blows_up(*args, **kwargs):
    raise RuntimeError("ffprobe exited 1")


class TestWorkerFeedsTheTracker:
    def test_a_preview_file_rerun_on_the_cpu_counts_as_a_fallback(self):
        worker = Worker(0, "GPU", "NVIDIA", "/dev/dri/renderD128", 0, "NVIDIA TITAN RTX")
        for _ in range(5):
            _run_preview(worker, _gpu_then_cpu)
        (state,) = get_gpu_fallback_tracker().flagged()
        assert (state.key, state.name, state.streak) == ("/dev/dri/renderD128", "NVIDIA TITAN RTX", 5)
        assert "Codec not supported by GPU" in state.last_reason
        assert worker.last_task_cpu_fallback is True

    def test_a_preview_file_that_stays_on_the_gpu_clears_the_flag(self):
        worker = Worker(0, "GPU", "NVIDIA", "/dev/dri/renderD128", 0, "NVIDIA TITAN RTX")
        for _ in range(5):
            _run_preview(worker, _gpu_then_cpu)
        _run_preview(worker, _gpu_ok)
        assert get_gpu_fallback_tracker().flagged() == []
        assert worker.last_task_cpu_fallback is False

    def test_the_gpu_name_shown_is_the_worker_cards_label(self):
        worker = Worker(
            0, "GPU", "intel", "/dev/dri/renderD128", 0, "Intel Corporation Raptor Lake-S GT1 [UHD Graphics 770]"
        )
        _run_preview(worker, _gpu_then_cpu)
        (state,) = get_gpu_fallback_tracker().states()
        assert state.name == "Intel UHD Graphics 770"

    def test_two_workers_on_one_gpu_share_its_streak(self):
        a = Worker(0, "GPU", "NVIDIA", "/dev/dri/renderD128", 0, "GPU A")
        b = Worker(1, "GPU", "NVIDIA", "/dev/dri/renderD128", 0, "GPU A")
        for worker in (a, b, a, b, a):
            _run_preview(worker, _gpu_then_cpu)
        assert [s.streak for s in get_gpu_fallback_tracker().flagged()] == [5]
        _run_preview(b, _gpu_ok)
        assert get_gpu_fallback_tracker().flagged() == []

    def test_a_cancelled_file_counts_neither_way(self):
        worker = Worker(0, "GPU", "NVIDIA", "/dev/dri/renderD128", 0, "GPU A")
        for _ in range(5):
            _run_preview(worker, _gpu_then_cpu)

        def cancelled(*args, **kwargs):
            raise CancellationError("stop")

        _run_preview(worker, cancelled)
        assert [s.streak for s in get_gpu_fallback_tracker().flagged()] == [5]
        assert worker.last_task_cpu_fallback is False

    def test_a_file_cancelled_during_its_cpu_rerun_counts_neither_way(self):
        worker = Worker(0, "GPU", "NVIDIA", "/dev/dri/renderD128", 0, "GPU A")
        _fallbacks(get_gpu_fallback_tracker(), "/dev/dri/renderD128", 5)

        def cancelled_on_cpu(*args, gpu=None, **kwargs):
            if gpu is not None:
                raise CodecNotSupportedError("hevc")
            raise CancellationError("stop")

        _run_preview(worker, cancelled_on_cpu)
        assert [s.streak for s in get_gpu_fallback_tracker().flagged()] == [5]
        assert worker.last_task_cpu_fallback is False

    def test_a_cpu_worker_never_counts(self):
        worker = Worker(0, "CPU")
        for _ in range(6):
            _run_preview(worker, _gpu_ok)
        assert get_gpu_fallback_tracker().states() == []
        assert worker.last_task_cpu_fallback is False

    def test_a_kinds_whole_item_rerun_counts_as_a_fallback(self):
        worker = Worker(0, "GPU", "NVIDIA", "cuda:0", 0, "GPU A")

        def process(item, **kwargs):
            if kwargs["gpu"] is not None:
                raise CodecNotSupportedError("hevc 10-bit unsupported")
            return ItemOutcome("markers_published")

        for _ in range(5):
            _run_custom(worker, process)
        (state,) = get_gpu_fallback_tracker().flagged()
        assert (state.key, state.streak, state.last_reason) == ("cuda:0", 5, "hevc 10-bit unsupported")
        assert worker.last_task_cpu_fallback is True

    def test_a_kinds_step_level_fallback_counts_as_a_fallback(self):
        worker = Worker(0, "GPU", "NVIDIA", "cuda:0", 0, "GPU A")

        def process(item, **kwargs):
            kwargs["fallback_callback"]("decoded t.mkv on the CPU: the GPU read no frames")
            return ItemOutcome("markers_published")

        for _ in range(5):
            _run_custom(worker, process)
        (state,) = get_gpu_fallback_tracker().flagged()
        assert (state.streak, state.last_reason) == (5, "decoded t.mkv on the CPU: the GPU read no frames")
        assert worker.last_task_cpu_fallback is True

    def test_a_kinds_file_cancelled_before_its_rerun_counts_neither_way(self):
        worker = Worker(0, "GPU", "NVIDIA", "cuda:0", 0, "GPU A")
        _fallbacks(get_gpu_fallback_tracker(), "cuda:0", 5)

        def process(item, **kwargs):
            raise CodecNotSupportedError("hevc")

        _run_custom(worker, process, cancel_check=lambda: True)
        assert [s.streak for s in get_gpu_fallback_tracker().flagged()] == [5]
        assert worker.last_task_cpu_fallback is False

    # Only a file the GPU decoded says the GPU works: frames from the cache, an output already there or a file that
    # failed outright say nothing, so they neither end a streak nor add to it.
    @pytest.mark.parametrize(
        "process",
        [_cache_hit, _output_existed, _blows_up],
        ids=["frames-from-cache", "output-already-there", "failed-outright"],
    )
    def test_a_preview_file_the_gpu_did_not_decode_is_not_recorded(self, process):
        worker = Worker(0, "GPU", "NVIDIA", "/dev/dri/renderD128", 0, "GPU A")
        _fallbacks(get_gpu_fallback_tracker(), "/dev/dri/renderD128", 4)
        _run_preview(worker, process)
        (state,) = get_gpu_fallback_tracker().states()
        assert state.streak == 4  # not reset
        assert worker.last_task_cpu_fallback is False
        _run_preview(worker, _gpu_then_cpu)
        assert [s.streak for s in get_gpu_fallback_tracker().flagged()] == [5]
        _run_preview(worker, process)
        assert [s.streak for s in get_gpu_fallback_tracker().flagged()] == [5]  # still flagged

    def test_a_preview_file_the_gpu_decoded_ends_the_streak(self):
        worker = Worker(0, "GPU", "NVIDIA", "/dev/dri/renderD128", 0, "GPU A")
        _fallbacks(get_gpu_fallback_tracker(), "/dev/dri/renderD128", 5)
        _run_preview(worker, _gpu_ok)
        assert get_gpu_fallback_tracker().flagged() == []

    @pytest.mark.parametrize("failure", ["outcome", "exception"])
    def test_a_kinds_file_that_failed_is_not_recorded(self, failure):
        worker = Worker(0, "GPU", "NVIDIA", "cuda:0", 0, "GPU A")
        _fallbacks(get_gpu_fallback_tracker(), "cuda:0", 5)

        def process(item, **kwargs):
            if failure == "exception":
                raise RuntimeError("boom")
            return ItemOutcome("failed", "boom")

        _run_custom(worker, process)
        assert [s.streak for s in get_gpu_fallback_tracker().flagged()] == [5]
        assert worker.last_task_cpu_fallback is False

    def test_a_kinds_file_that_finished_on_the_gpu_ends_the_streak(self):
        worker = Worker(0, "GPU", "NVIDIA", "cuda:0", 0, "GPU A")
        _fallbacks(get_gpu_fallback_tracker(), "cuda:0", 5)
        _run_custom(worker, lambda item, **kwargs: ItemOutcome("markers_published"))
        assert get_gpu_fallback_tracker().flagged() == []


class TestTrackerFailureNeverStopsTheWorker:
    """The streak is bookkeeping: a tracker that raises must not hang the worker or lose the file's counts."""

    @pytest.fixture
    def broken_tracker(self):
        tracker = MagicMock()
        tracker.record.side_effect = RuntimeError("notifications import failed")
        with patch("media_preview_generator.jobs.worker.get_gpu_fallback_tracker", return_value=tracker):
            yield tracker

    def test_a_preview_file_is_still_counted_and_the_done_event_set(self, broken_tracker):
        worker = Worker(0, "GPU", "NVIDIA", "cuda:0", 0, "GPU A")
        worker._done_event = threading.Event()
        _run_preview(worker, _gpu_then_cpu)
        assert worker.current_thread is not None and not worker.current_thread.is_alive()
        assert worker._done_event.is_set()
        assert (worker.completed, worker.failed, worker.outcome_counts["generated"]) == (1, 0, 1)
        assert worker.last_task_cpu_fallback is True
        broken_tracker.record.assert_called_once()

    def test_a_kinds_file_is_still_counted_and_the_done_event_set(self, broken_tracker):
        worker = Worker(0, "GPU", "NVIDIA", "cuda:0", 0, "GPU A")
        worker._done_event = threading.Event()
        _run_custom(worker, lambda item, **kwargs: ItemOutcome("markers_published"))
        assert worker.current_thread is not None and not worker.current_thread.is_alive()
        assert worker._done_event.is_set()
        assert (worker.completed, worker.failed, worker.outcome_counts["markers_published"]) == (1, 0, 1)
        broken_tracker.record.assert_called_once()


def _tracker(dispatcher: JobDispatcher, items, **kwargs) -> JobTracker:
    config = MagicMock(cpu_threads=0, gpu_threads=1, scan_workers=1, regenerate_thumbnails=False, server_id_filter=None)
    tracker = JobTracker("job-1", items, config, MagicMock(), callbacks={"progress_callback": MagicMock()}, **kwargs)
    dispatcher._trackers[tracker.job_id] = tracker
    return tracker


class TestJobTally:
    def test_a_preview_job_counts_files_that_ran_on_the_cpu(self):
        pool = WorkerPool(gpu_workers=1, cpu_workers=0, selected_gpus=[("NVIDIA", "cuda", {"name": "GPU A"})])
        worker = pool.workers[0]
        dispatcher = JobDispatcher(pool)
        tracker = _tracker(dispatcher, [_pi("a"), _pi("b")])
        pushed: list[int] = []
        with patch.object(JobTracker, "_push_cpu_fallback_files", lambda self, n: pushed.append(n)):
            _run_preview(worker, _gpu_then_cpu)
            dispatcher._merge_worker_outcome(worker, tracker)
            tracker.record_completion(True, worker.display_name, "a", canonical_path="/data/a.mkv")
            _run_preview(worker, _gpu_ok)
            dispatcher._merge_worker_outcome(worker, tracker)
            tracker.record_completion(True, worker.display_name, "b", canonical_path="/data/b.mkv")
        assert tracker.cpu_fallback_files == 1
        assert tracker.get_result()["outcome"]["generated"] == 2
        assert pushed[-1] == 1

    def test_a_kinds_job_counts_step_level_fallbacks(self):
        pool = WorkerPool(gpu_workers=1, cpu_workers=0, selected_gpus=[("NVIDIA", "cuda", {"name": "GPU A"})])
        worker = pool.workers[0]
        dispatcher = JobDispatcher(pool)
        handlers = MagicMock(outcome_keys=KEYS)
        tracker = _tracker(
            dispatcher,
            [ProcessableItem(canonical_path="/m/t.mkv", server_id="s1", title="t")],
            kind="intro_credits",
            handlers=handlers,
        )

        def process(item, **kwargs):
            kwargs["fallback_callback"]("decoded on the CPU")
            return ItemOutcome("markers_published")

        _run_custom(worker, process)
        dispatcher._merge_worker_outcome(worker, tracker)
        assert tracker.cpu_fallback_files == 1
        assert tracker.outcome_counts["markers_published"] == 1

    def test_the_tally_reaches_the_job_record(self):
        from media_preview_generator.web.jobs import JobManager

        manager = JobManager.__new__(JobManager)
        manager._lock = threading.RLock()
        job = MagicMock()
        manager._jobs = {"job-1": job}
        assert manager.set_job_cpu_fallback_files("job-1", 3) is job
        assert job.progress.cpu_fallback_files == 3
        assert manager.set_job_cpu_fallback_files("nope", 3) is None


def test_the_process_wide_tracker_is_one_object():
    assert get_gpu_fallback_tracker() is gpu_fallback.get_gpu_fallback_tracker()
