"""A file one GPU can't decode goes to another GPU before the CPU (#424)."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, call, patch

import pytest

from media_preview_generator.job_kinds import ItemOutcome, KindHandlers
from media_preview_generator.jobs.dispatcher import (
    GPU_WAIT_RECHECK_S,
    JobDispatcher,
    JobTracker,
    _nodes_off,
    reset_dispatcher,
)
from media_preview_generator.jobs.worker import Worker, WorkerPool
from media_preview_generator.nodes.executor import FileNotVisibleError, NodeUnavailableError, current_placement
from media_preview_generator.nodes.registry import set_registry
from media_preview_generator.processing.generator import (
    FALLBACK_HWACCEL,
    FALLBACK_IO_ERROR,
    FALLBACK_SIGNAL,
    FALLBACK_STALL,
    FALLBACK_STOPPED_PART_WAY,
    GPU_LACKS_FORMAT,
    CodecNotSupportedError,
)
from media_preview_generator.processing.multi_server import MultiServerStatus
from media_preview_generator.processing.types import ProcessableItem
from media_preview_generator.web.settings_manager import reset_settings_manager
from tests.test_dispatcher_node_requeue import _Registry

KEYS = ("markers_published", "failed")
QUADRO = "/dev/nvidia0"
INTEL = "/dev/dri/renderD128"
LACKS_HEVC = f"GPU processing failed ({GPU_LACKS_FORMAT} this video's format) for /m/hevc.mkv (exit code 1)"


@pytest.fixture(autouse=True)
def _reset():
    # A settings manager another test left behind would hand the dispatch loop its saved groups, replacing the pool's.
    reset_settings_manager()
    reset_dispatcher()
    yield
    reset_settings_manager()
    reset_dispatcher()


def _config():
    c = MagicMock()
    c.cpu_threads = 1
    c.gpu_threads = 0
    c.scan_workers = 2
    c.regenerate_thumbnails = False
    c.server_id_filter = None
    c.webhook_source = None
    c.webhook_deleted_paths = None
    return c


def _items(*paths):
    return [ProcessableItem(canonical_path=p, server_id="s1", title=p.rsplit("/", 1)[-1]) for p in paths]


def _pool(*workers: str) -> WorkerPool:
    """A pool with one worker per entry, in order: "quadro", "intel" (GPU workers) or "cpu"."""
    pool = WorkerPool(cpu_workers=0, gpu_workers=0, selected_gpus=[])
    for index, name in enumerate(workers):
        if name == "quadro":
            worker = Worker(index, "GPU", "NVIDIA", QUADRO, 0, "Quadro M1200")
        elif name == "intel":
            worker = Worker(index, "GPU", "INTEL", INTEL, 1, "Intel UHD 630")
        else:
            worker = Worker(index, "CPU")
        worker.display_name = f"Worker {index + 1}"
        worker._done_event = pool._worker_done_event
        pool.workers.append(worker)
    return pool


def _run(pool: WorkerPool, process, *paths: str, check_fn=None) -> tuple[JobTracker, list]:
    """Run ``paths`` through the dispatcher as an Intro & Credits job; returns the tracker and every file row."""
    dispatcher = JobDispatcher(pool)
    check_fn = check_fn or (lambda item, *, cancel_check: None)
    handlers = KindHandlers(check_fn=check_fn, process_fn=process, outcome_keys=KEYS)
    try:
        with (
            patch("media_preview_generator.processing.generator._notify_file_result") as dispatcher_rows,
            patch("media_preview_generator.jobs.worker._notify_file_result") as worker_rows,
            patch("media_preview_generator.web.jobs.get_job_manager"),
        ):
            tracker = dispatcher.submit_items(
                "j1", _items(*paths), _config(), MagicMock(), kind="intro_credits", handlers=handlers
            )
            assert tracker.wait(timeout=15)
    finally:
        dispatcher.shutdown()
    return tracker, dispatcher_rows.call_args_list + worker_rows.call_args_list


class _Decoders:
    """A process_fn whose GPUs refuse the files in ``refuse`` (device -> paths), recording each run's device."""

    def __init__(self, refuse: dict[str, set[str]], *, kind: str = "codec", message: str = LACKS_HEVC) -> None:
        self.refuse = refuse
        self.kind = kind
        self.message = message
        self.calls: list[tuple[str, str | None]] = []
        self.lock = threading.Lock()

    def __call__(self, item, *, gpu_device_path=None, **_kwargs):
        with self.lock:
            self.calls.append((item.canonical_path, gpu_device_path))
        if item.canonical_path in self.refuse.get(gpu_device_path or "", set()):
            raise CodecNotSupportedError(self.message, kind=self.kind)
        return ItemOutcome("markers_published", "ok")

    def runs_of(self, path: str) -> list[str | None]:
        return [device for p, device in self.calls if p == path]


class TestHandOff:
    def test_a_file_the_first_gpu_cant_decode_runs_on_the_other_gpu_not_the_cpu(self):
        process = _Decoders({QUADRO: {"/m/hevc.mkv"}})

        tracker, rows = _run(_pool("quadro", "intel"), process, "/m/hevc.mkv")

        assert process.runs_of("/m/hevc.mkv") == [QUADRO, INTEL]
        assert (tracker.successful, tracker.failed) == (1, 0)
        assert tracker.outcome_counts == {"markers_published": 1, "failed": 0}
        assert tracker.cpu_fallback_files == 0
        assert tracker.gpus_refused == {}  # dropped once the file finished
        assert [r.args[0] for r in rows] == ["/m/hevc.mkv"]
        assert tracker.active_processing == 0

    def test_the_file_waits_for_the_busy_gpu_while_the_free_gpu_and_cpu_take_other_files(self):
        handed_back = threading.Event()
        release_intel = threading.Event()
        process = _Decoders({QUADRO: {"/m/hevc.mkv"}})

        def run(item, *, gpu_device_path=None, **kwargs):
            if item.canonical_path == "/m/long.mkv":
                release_intel.wait(timeout=10)
            if item.canonical_path == "/m/hevc.mkv" and gpu_device_path == QUADRO:
                handed_back.set()
            return process(item, gpu_device_path=gpu_device_path, **kwargs)

        def free_intel_later():
            handed_back.wait(timeout=10)
            time.sleep(0.5)  # the Quadro and the CPU worker are free meanwhile; neither may take the file
            release_intel.set()

        threading.Thread(target=free_intel_later, daemon=True).start()
        paths = ["/m/hevc.mkv", "/m/long.mkv", "/m/a.mkv", "/m/b.mkv", "/m/c.mkv"]
        tracker, rows = _run(_pool("quadro", "intel", "cpu"), run, *paths)

        assert process.runs_of("/m/hevc.mkv") == [QUADRO, INTEL]
        assert (tracker.successful, tracker.failed) == (5, 0)
        assert sorted(r.args[0] for r in rows) == sorted(paths)
        # The files queued behind it didn't wait with it.
        later = [p for p, _ in process.calls[process.calls.index(("/m/hevc.mkv", INTEL)) :]]
        assert not {"/m/a.mkv", "/m/b.mkv", "/m/c.mkv"} & set(later)

    def test_when_every_gpu_refuses_the_last_one_reruns_it_on_its_cpu(self):
        process = _Decoders({QUADRO: {"/m/av1.mkv"}, INTEL: {"/m/av1.mkv"}})

        tracker, rows = _run(_pool("quadro", "intel"), process, "/m/av1.mkv")

        assert process.runs_of("/m/av1.mkv") == [QUADRO, INTEL, None]
        assert (tracker.successful, tracker.failed) == (1, 0)
        assert tracker.cpu_fallback_files == 1
        assert [r.args[0] for r in rows] == ["/m/av1.mkv"]

    def test_one_gpu_falls_back_to_its_cpu_in_place_as_before(self):
        process = _Decoders({QUADRO: {"/m/hevc.mkv"}})

        tracker, _rows = _run(_pool("quadro", "cpu"), process, "/m/hevc.mkv")

        assert process.runs_of("/m/hevc.mkv") == [QUADRO, None]
        assert tracker.gpus_refused == {}
        assert tracker.cpu_fallback_files == 1

    @pytest.mark.parametrize("kind", [FALLBACK_IO_ERROR, FALLBACK_STALL], ids=["io-error", "stall"])
    def test_a_disk_failure_stays_on_the_same_worker(self, kind):
        process = _Decoders({QUADRO: {"/m/hevc.mkv"}}, kind=kind, message="ffmpeg couldn't read the file")

        tracker, _rows = _run(_pool("quadro", "intel"), process, "/m/hevc.mkv")

        assert process.runs_of("/m/hevc.mkv") == [QUADRO, None]
        assert tracker.gpus_refused == {}

    @pytest.mark.parametrize(
        "kind", [FALLBACK_HWACCEL, FALLBACK_STOPPED_PART_WAY, FALLBACK_SIGNAL], ids=["driver", "part-way", "signal"]
    )
    def test_a_gpu_fault_is_handed_on_too(self, kind):
        process = _Decoders({QUADRO: {"/m/hevc.mkv"}}, kind=kind, message="hardware accelerator error")

        tracker, _rows = _run(_pool("quadro", "intel"), process, "/m/hevc.mkv")

        assert process.runs_of("/m/hevc.mkv") == [QUADRO, INTEL]
        assert tracker.cpu_fallback_files == 0

    def test_another_gpu_that_doesnt_run_the_job_type_isnt_waited_for(self):
        pool = _pool("quadro", "intel")
        pool.workers[1].allowed_job_types = frozenset({"previews"})
        process = _Decoders({QUADRO: {"/m/hevc.mkv"}})

        tracker, _rows = _run(pool, process, "/m/hevc.mkv")

        assert process.runs_of("/m/hevc.mkv") == [QUADRO, None]
        assert tracker.gpus_refused == {}


class TestPreviewsHandOff:
    def test_a_preview_the_first_gpu_cant_decode_runs_on_the_other_gpu(self):
        calls = []

        def pcp(**kwargs):
            if kwargs.get("check_only"):
                return MagicMock(status=MultiServerStatus.NEEDS_GENERATION, publishers=[])
            calls.append((kwargs["gpu"], kwargs["gpu_device_path"], kwargs["ffmpeg_threads_override"]))
            if kwargs["gpu_device_path"] == QUADRO:
                raise CodecNotSupportedError(LACKS_HEVC)
            return MagicMock(status=MultiServerStatus.PUBLISHED, publishers=[], message="", slow_path_interval=None)

        pool = _pool("quadro", "intel")
        pool.workers[1].ffmpeg_threads = 3
        dispatcher = JobDispatcher(pool)
        try:
            with (
                patch("media_preview_generator.processing.multi_server.process_canonical_path", side_effect=pcp),
                patch("media_preview_generator.processing.generator._notify_file_result"),
                patch("media_preview_generator.jobs.worker._notify_file_result"),
                patch("media_preview_generator.web.jobs.get_job_manager"),
            ):
                tracker = dispatcher.submit_items("j1", _items("/m/hevc.mkv"), _config(), MagicMock())
                assert tracker.wait(timeout=15)
        finally:
            dispatcher.shutdown()

        assert calls == [("NVIDIA", QUADRO, None), ("INTEL", INTEL, 3)]
        assert (tracker.successful, tracker.failed) == (1, 0)
        assert tracker.cpu_fallback_files == 0


class TestWorkerSelection:
    def test_a_refused_file_skips_the_gpu_that_refused_it_and_the_cpu(self):
        pool = _pool("quadro", "cpu", "intel")
        intel = pool.workers[2]

        assert pool._find_available_worker(kind="intro_credits", exclude_gpus={f"gpu:{QUADRO}"}) is intel
        intel.is_busy = True
        assert pool._find_available_worker(kind="intro_credits", exclude_gpus={f"gpu:{QUADRO}"}) is None
        assert pool._find_available_worker(kind="intro_credits") is pool.workers[0]

    def test_runs_kind_on_other_gpu_counts_busy_gpus_but_not_the_cpu(self):
        pool = _pool("quadro", "intel", "cpu")
        pool.workers[1].is_busy = True

        assert pool.runs_kind_on_other_gpu("intro_credits", {f"gpu:{QUADRO}"})
        assert not pool.runs_kind_on_other_gpu("intro_credits", {f"gpu:{QUADRO}", f"gpu:{INTEL}"})
        assert not pool.runs_kind_on_other_gpu("loudness", {f"gpu:{QUADRO}"})


class TestWaitMessage:
    def test_a_job_whose_only_file_waits_for_a_busy_gpu_says_so(self):
        pool = _pool("quadro", "intel")
        pool.workers[1].is_busy = True
        dispatcher = JobDispatcher(pool)
        tracker = JobTracker("j1", _items(), _config(), MagicMock(), kind="intro_credits")
        (item,) = _items("/m/Dune Part Two (2024).mkv")
        tracker.item_queue.append(item)
        tracker.gpus_refused[item.canonical_path] = {f"gpu:{QUADRO}": "Quadro M1200"}

        assert dispatcher._node_wait(tracker) == (
            "Waiting for another GPU — Quadro M1200 can't decode Dune Part Two (2024).mkv"
        )
        pool.workers[1].allowed_job_types = frozenset({"previews"})
        assert dispatcher._node_wait(tracker) == ""


class TestNodes:
    """A file waits only for another GPU that can take it: never for one on a node that can't (review of #424)."""

    @pytest.fixture(autouse=True)
    def registry(self):
        fake = _Registry()
        set_registry(fake)
        yield fake
        set_registry(None)

    @staticmethod
    def _pool_with_node_gpu() -> WorkerPool:
        pool = _pool("quadro", "cpu")
        node_gpu = Worker(2, "GPU", "INTEL", INTEL, 1, "Intel UHD 630", node_id="n1", node_name="NAS")
        node_gpu.display_name = "Worker 3"
        node_gpu._done_event = pool._worker_done_event
        pool.workers.append(node_gpu)
        return pool

    @pytest.mark.parametrize(
        "node_error",
        [
            FileNotVisibleError("n1", "NAS", "missing", "/m/hevc.mkv isn't there"),
            NodeUnavailableError("n1", "NAS", "NAS stopped answering"),
            NodeUnavailableError("n1", "NAS", "NAS couldn't start the task", started=False),
        ],
        ids=["node-cant-see-it", "node-dropped-it", "node-didnt-start-it"],
    )
    def test_a_file_handed_to_a_node_gpu_that_cant_take_it_still_finishes_here(self, node_error):
        runs: list[tuple[str | None, str | None]] = []
        lock = threading.Lock()

        def process(item, *, gpu_device_path=None, **_kwargs):
            placement = current_placement()
            node = None if placement is None else placement.node_id
            with lock:
                runs.append((node, gpu_device_path))
            if node == "n1":
                raise node_error
            if gpu_device_path == QUADRO:
                raise CodecNotSupportedError(LACKS_HEVC)
            return ItemOutcome("markers_published", "ok")

        tracker, rows = _run(self._pool_with_node_gpu(), process, "/m/hevc.mkv")

        # Then this app's CPU, the CPU worker's or the Quadro's rerun: the Quadro never tries it again.
        assert runs == [(None, QUADRO), ("n1", INTEL), (None, None)]
        assert (tracker.successful, tracker.failed) == (1, 0)
        assert [r.args[0] for r in rows] == ["/m/hevc.mkv"]
        assert tracker.active_processing == 0


class TestQueue:
    def test_many_waiting_files_dont_hide_a_file_the_free_gpu_can_run(self):
        hevc = [f"/m/hevc{n}.mkv" for n in range(80)]
        h264_ran = threading.Event()
        process = _Decoders({QUADRO: set(hevc)})

        def run(item, **kwargs):
            if item.canonical_path == "/m/long.mkv":
                assert h264_ran.wait(timeout=10), "the H.264 file stayed hidden behind the waiting files"
            outcome = process(item, **kwargs)
            if item.canonical_path == "/m/h264.mkv":
                h264_ran.set()
            return outcome

        tracker, _rows = _run(_pool("intel", "quadro"), run, "/m/long.mkv", *hevc, "/m/h264.mkv")

        assert process.runs_of("/m/h264.mkv") == [QUADRO]
        assert all(process.runs_of(path) == [QUADRO, INTEL] for path in hevc)
        assert (tracker.successful, tracker.failed) == (82, 0)
        assert tracker.gpus_refused == {}

    def test_cancelling_the_job_while_a_file_waits_ends_it(self):
        handed_back = threading.Event()
        release_intel = threading.Event()
        holder: dict = {}
        process = _Decoders({QUADRO: {"/m/hevc.mkv"}})

        def run(item, *, gpu_device_path=None, **kwargs):
            if item.canonical_path == "/m/long.mkv":
                release_intel.wait(timeout=10)
            if item.canonical_path == "/m/hevc.mkv" and gpu_device_path == QUADRO:
                handed_back.set()
            return process(item, gpu_device_path=gpu_device_path, **kwargs)

        def cancel_while_waiting():
            handed_back.wait(timeout=10)
            time.sleep(0.3)
            holder["tracker"].cancel()
            release_intel.set()

        dispatcher = JobDispatcher(_pool("intel", "quadro"))
        handlers = KindHandlers(check_fn=lambda item, *, cancel_check: None, process_fn=run, outcome_keys=KEYS)
        try:
            with (
                patch("media_preview_generator.processing.generator._notify_file_result"),
                patch("media_preview_generator.jobs.worker._notify_file_result"),
                patch("media_preview_generator.web.jobs.get_job_manager"),
            ):
                holder["tracker"] = dispatcher.submit_items(
                    "j1", _items("/m/long.mkv", "/m/hevc.mkv"), _config(), MagicMock(), kind="intro_credits",
                    handlers=handlers,
                )  # fmt: skip
                threading.Thread(target=cancel_while_waiting, daemon=True).start()
                assert holder["tracker"].wait(timeout=15)
        finally:
            dispatcher.shutdown()

        assert process.runs_of("/m/hevc.mkv") == [QUADRO]
        assert "/m/hevc.mkv" in holder["tracker"].failed_paths


class TestGpuStreak:
    @pytest.mark.parametrize(
        ("kind", "message", "blamed"),
        [("codec", LACKS_HEVC, False), (FALLBACK_HWACCEL, "hardware accelerator error", True)],
        ids=["lacks-the-format", "driver-error"],
    )
    def test_a_hand_off_counts_against_the_gpu_only_for_a_fault(self, kind, message, blamed):
        tracker_mock = MagicMock()
        process = _Decoders({QUADRO: {"/m/hevc.mkv"}}, kind=kind, message=message)

        with patch("media_preview_generator.jobs.worker.get_gpu_fallback_tracker", return_value=tracker_mock):
            _run(_pool("quadro", "intel"), process, "/m/hevc.mkv")

        by_device = {c.args[0]: c for c in tracker_mock.record.call_args_list}
        assert by_device[INTEL].kwargs == {"fell_back": False, "reason": None}
        if blamed:
            assert by_device[QUADRO].args[1] == "Quadro M1200"
            assert by_device[QUADRO].kwargs == {"fell_back": True, "reason": message}
        else:
            assert QUADRO not in by_device

    def test_a_gpu_being_removed_isnt_waited_for(self):
        pool = _pool("quadro", "intel")
        pool.workers[1]._pending_removal = True
        process = _Decoders({QUADRO: {"/m/hevc.mkv"}})

        tracker, _rows = _run(pool, process, "/m/hevc.mkv")

        assert process.runs_of("/m/hevc.mkv") == [QUADRO, None]


class TestGpuGoesAway:
    """The GPU a file waits for goes away: the GPU that failed it reruns it on its CPU, not its GPU (review of #424)."""

    @pytest.mark.parametrize(
        ("kind", "message", "blamed"),
        [("codec", LACKS_HEVC, False), (FALLBACK_HWACCEL, "hardware accelerator error", True)],
        ids=["lacks-the-format", "driver-error"],
    )
    def test_the_gpu_that_failed_it_runs_it_on_its_cpu(self, kind, message, blamed):
        pool = _pool("intel", "quadro")
        intel = pool.workers[0]
        handed_back = threading.Event()
        long_started = threading.Event()
        process = _Decoders({QUADRO: {"/m/hevc.mkv"}}, kind=kind, message=message)

        def check(item, *, cancel_check):
            # Checks run in parallel and queue in finishing order; hold hevc.mkv so long.mkv takes the Intel worker.
            if item.canonical_path == "/m/hevc.mkv":
                assert long_started.wait(timeout=10)

        def run(item, *, gpu_device_path=None, **kwargs):
            if item.canonical_path == "/m/long.mkv":
                long_started.set()
                assert handed_back.wait(timeout=10)
                time.sleep(0.2)
                intel._pending_removal = True  # removed from Settings while the file waits for it
            if gpu_device_path == QUADRO:
                handed_back.set()
            return process(item, gpu_device_path=gpu_device_path, **kwargs)

        streak = MagicMock()
        with patch("media_preview_generator.jobs.worker.get_gpu_fallback_tracker", return_value=streak):
            tracker, _rows = _run(pool, run, "/m/long.mkv", "/m/hevc.mkv", check_fn=check)

        assert process.runs_of("/m/hevc.mkv") == [QUADRO, None]
        assert (tracker.successful, tracker.failed) == (2, 0)
        assert tracker.cpu_fallback_files == 1
        quadro_records = [c for c in streak.record.call_args_list if c.args[0] == QUADRO]
        assert len(quadro_records) == (1 if blamed else 0)  # counted once, for the GPU's own try

    def test_a_preview_reruns_on_the_cpu_of_the_gpu_that_failed_it(self):
        pool = _pool("intel", "quadro")
        intel = pool.workers[0]
        handed_back = threading.Event()
        long_started = threading.Event()
        calls: list[tuple[str, str | None]] = []

        def pcp(**kwargs):
            if kwargs.get("check_only"):
                # Checks run in parallel and queue in finishing order; hold hevc.mkv so long.mkv takes the Intel worker.
                if kwargs["canonical_path"] == "/m/hevc.mkv":
                    assert long_started.wait(timeout=10)
                return MagicMock(status=MultiServerStatus.NEEDS_GENERATION, publishers=[])
            path, device = kwargs["canonical_path"], kwargs["gpu_device_path"]
            calls.append((path, device))
            if path == "/m/long.mkv":
                long_started.set()
                assert handed_back.wait(timeout=10)
                time.sleep(0.2)
                intel._pending_removal = True  # removed from Settings while the file waits for it
            elif device == QUADRO:
                handed_back.set()
                raise CodecNotSupportedError("hardware accelerator error", kind=FALLBACK_HWACCEL)
            return MagicMock(status=MultiServerStatus.PUBLISHED, publishers=[], message="", slow_path_interval=None)

        streak = MagicMock()
        dispatcher = JobDispatcher(pool)
        try:
            with (
                patch("media_preview_generator.processing.multi_server.process_canonical_path", side_effect=pcp),
                patch("media_preview_generator.processing.generator._notify_file_result"),
                patch("media_preview_generator.jobs.worker._notify_file_result"),
                patch("media_preview_generator.web.jobs.get_job_manager"),
                patch("media_preview_generator.jobs.worker.get_gpu_fallback_tracker", return_value=streak),
            ):
                tracker = dispatcher.submit_items("j1", _items("/m/long.mkv", "/m/hevc.mkv"), _config(), MagicMock())
                assert tracker.wait(timeout=15)
        finally:
            dispatcher.shutdown()

        assert [device for path, device in calls if path == "/m/hevc.mkv"] == [QUADRO, None]
        assert (tracker.successful, tracker.failed, tracker.cpu_fallback_files) == (2, 0, 1)
        assert len([c for c in streak.record.call_args_list if c.args[0] == QUADRO]) == 1


class TestFrozenGpu:
    """A GPU busy on a job its schedule froze can stay busy for hours: no file waits for it (review of #424)."""

    @staticmethod
    def _night_job(dispatcher, frozen):
        night = JobTracker(
            "night", _items(), _config(), MagicMock(), kind="intro_credits", callbacks={"freeze_check": lambda: frozen}
        )
        dispatcher._trackers["night"] = night
        intel = dispatcher.worker_pool.workers[1]
        intel.is_busy = True
        intel.current_job_id = "night"

    @staticmethod
    def _waiting_job():
        tracker = JobTracker("j1", _items(), _config(), MagicMock(), kind="intro_credits")
        (item,) = _items("/m/hevc.mkv")
        tracker.item_queue.append(item)
        tracker.gpus_refused[item.canonical_path] = {f"gpu:{QUADRO}": "Quadro M1200"}
        tracker.refused_keys[item.canonical_path] = frozenset({f"gpu:{QUADRO}"})
        return tracker

    @pytest.mark.parametrize("frozen", [True, False], ids=["frozen", "running"])
    def test_a_gpu_on_a_frozen_job_isnt_waited_for(self, frozen):
        dispatcher = JobDispatcher(_pool("quadro", "intel"))
        self._night_job(dispatcher, frozen)
        tracker = self._waiting_job()

        hands_on = dispatcher._other_gpu_runs("intro_credits", {f"gpu:{QUADRO}"})
        claimed = dispatcher._claim_worker(tracker, [], frozen_jobs=dispatcher._frozen_jobs())
        banner = dispatcher._node_wait(tracker)

        assert hands_on is not frozen
        if frozen:
            assert claimed[0] is dispatcher.worker_pool.workers[0]  # the Quadro, for its CPU rerun
            assert banner == ""
        else:
            assert claimed is None
            assert banner.startswith("Waiting for another GPU")

    @pytest.mark.parametrize("frozen", [True, False], ids=["frozen", "running"])
    def test_a_dispatch_pass_gives_the_waiting_file_back_to_its_gpu_only_when_the_other_is_frozen(self, frozen):
        dispatcher = JobDispatcher(_pool("quadro", "intel"))
        self._night_job(dispatcher, frozen)
        tracker = self._waiting_job()
        tracker.gpu_errors["/m/hevc.mkv"] = {f"gpu:{QUADRO}": CodecNotSupportedError(LACKS_HEVC)}
        dispatcher._trackers["j1"] = tracker

        with patch.object(Worker, "assign_task", autospec=True) as assign:
            dispatcher._assign_tasks()

        if frozen:
            worker, item = assign.call_args.args[:2]
            assert (worker, item.canonical_path) == (dispatcher.worker_pool.workers[0], "/m/hevc.mkv")
            assert str(assign.call_args.kwargs["gpu_error"]) == LACKS_HEVC  # straight to the Quadro's CPU
        else:
            assign.assert_not_called()


class TestEnds:
    def test_a_file_that_fails_another_way_forgets_its_gpus(self):
        dispatcher = JobDispatcher(_pool("quadro", "intel"))
        tracker = JobTracker("j1", _items(), _config(), MagicMock(), kind="intro_credits")
        (item,) = _items("/m/hevc.mkv")
        tracker.gpus_refused[item.canonical_path] = {f"gpu:{QUADRO}": "Quadro M1200"}
        tracker.gpu_errors[item.canonical_path] = {f"gpu:{QUADRO}": CodecNotSupportedError(LACKS_HEVC)}

        with patch("media_preview_generator.processing.generator._notify_file_result"):
            dispatcher._record_failed_item(tracker, item, "", "NAS can't see it")

        assert (tracker.gpus_refused, tracker.gpu_errors) == ({}, {})

    def test_the_waiting_count_drops_the_moment_a_gpu_takes_the_file(self):
        pool = _pool("quadro", "intel")
        dispatcher = JobDispatcher(pool)
        tracker = self._started_job(dispatcher)

        with patch.object(Worker, "assign_task") as assign:
            dispatcher._assign_tasks()

        assert assign.call_args.args[0].canonical_path == "/m/hevc.mkv"
        assert assign.call_args.kwargs["gpu_error"] is None  # the Intel never failed it
        assert tracker.gpu_waiting == 0
        assert tracker.progress_message(3) == "3/10 completed"

    @staticmethod
    def _started_job(dispatcher):
        tracker = JobTracker("j1", _items(), _config(), MagicMock(), kind="intro_credits")
        tracker.total_items = 10
        tracker.work_started = tracker.generation_started = True
        (item,) = _items("/m/hevc.mkv")
        tracker.item_queue.append(item)
        tracker.gpus_refused[item.canonical_path] = {f"gpu:{QUADRO}": "Quadro M1200"}
        tracker.gpu_waiting = 1
        dispatcher.worker_pool.workers[0].is_busy = True  # the Quadro, so only the Intel can take it
        dispatcher._trackers["j1"] = tracker
        return tracker


class TestClaimCost:
    def test_thousands_of_waiting_files_cost_one_gpu_check_per_pass(self):
        pool = _pool("quadro", "intel")
        pool.workers[1].is_busy = True
        dispatcher = JobDispatcher(pool)
        tracker = JobTracker("j1", _items(), _config(), MagicMock(), kind="intro_credits")
        waiting = _items(*(f"/m/hevc{n}.mkv" for n in range(2000)))
        tracker.item_queue.extend([*waiting, *_items("/m/h264.mkv")])
        for item in waiting:  # as _requeue_for_another_gpu leaves them
            tracker.gpus_refused[item.canonical_path] = {f"gpu:{QUADRO}": "Quadro M1200"}
            tracker.refused_keys[item.canonical_path] = frozenset({f"gpu:{QUADRO}"})

        with (
            patch.object(pool, "runs_kind_on_other_gpu", wraps=pool.runs_kind_on_other_gpu) as check,
            patch("media_preview_generator.jobs.dispatcher._nodes_off", wraps=_nodes_off) as nodes_off,
        ):
            worker, item = dispatcher._claim_worker(tracker, [])

        assert (worker, item.canonical_path) == (pool.workers[0], "/m/h264.mkv")
        assert check.call_args_list == [call("intro_credits", frozenset({f"gpu:{QUADRO}"}), (), ())]
        assert nodes_off.call_count == 0  # the 2,000 waiting files are swept past in one go
        assert len(tracker.item_queue) == 2000

    def test_the_banner_counts_files_waiting_for_the_same_gpu(self):
        pool = _pool("quadro", "intel")
        pool.workers[1].is_busy = True
        dispatcher = JobDispatcher(pool)
        tracker = JobTracker("j1", _items(), _config(), MagicMock(), kind="intro_credits")
        for item in _items("/m/a.mkv", "/m/b.mkv", "/m/c.mkv"):
            tracker.item_queue.append(item)
            tracker.gpus_refused[item.canonical_path] = {f"gpu:{QUADRO}": "Quadro M1200"}

        assert dispatcher._node_wait(tracker) == "Waiting for another GPU — Quadro M1200 can't decode 3 files"

    def test_the_job_card_counts_waiting_files_while_others_run(self):
        pool = _pool("quadro", "intel")
        pool.workers[1].is_busy = True
        dispatcher = JobDispatcher(pool)
        tracker = JobTracker("j1", _items(), _config(), MagicMock(), kind="intro_credits")
        tracker.total_items = 40
        tracker.work_started = tracker.generation_started = True
        tracker.active_processing = 1
        (dune,) = _items("/m/Dune Part Two (2024).mkv")
        tracker.item_queue.extend([dune, *_items("/m/h264.mkv")])
        tracker.gpus_refused[dune.canonical_path] = {f"gpu:{QUADRO}": "Quadro M1200"}
        dispatcher._trackers["j1"] = tracker

        dispatcher._emit_progress_updates()
        assert tracker.progress_message(15) == "1 file waiting for another GPU · 15/40 completed"

        tracker.item_queue.popleft()  # the Intel worker picked it up
        tracker.node_wait_checked_at = 0.0
        dispatcher._emit_progress_updates()
        assert tracker.progress_message(15) == "15/40 completed"

    @staticmethod
    def _all_waiting(pool):
        dispatcher = JobDispatcher(pool)
        tracker = JobTracker("j1", _items(), _config(), MagicMock(), kind="intro_credits")
        for item in _items("/m/a.mkv", "/m/b.mkv"):
            tracker.item_queue.append(item)
            tracker.gpus_refused[item.canonical_path] = {f"gpu:{QUADRO}": "Quadro M1200"}
            tracker.refused_keys[item.canonical_path] = frozenset({f"gpu:{QUADRO}"})
        return dispatcher, tracker

    def test_a_gpu_freeing_up_is_seen_on_the_next_pass(self):
        pool = _pool("quadro", "intel")
        intel = pool.workers[1]
        intel.is_busy = True
        dispatcher, tracker = self._all_waiting(pool)

        with patch.object(dispatcher, "_first_free_file", wraps=dispatcher._first_free_file) as sweep:
            assert dispatcher._claim_worker(tracker, []) is None
            assert dispatcher._claim_worker(tracker, []) is None  # nothing changed: no second sweep
            intel.is_busy = False
            worker, item = dispatcher._claim_worker(tracker, [])

        assert sweep.call_count == 2
        assert (worker, item.canonical_path) == (intel, "/m/a.mkv")

    def test_a_gpu_removed_while_nothing_else_changes_is_seen_within_the_recheck(self):
        pool = _pool("quadro", "intel")
        intel = pool.workers[1]
        intel.is_busy = True
        dispatcher, tracker = self._all_waiting(pool)
        assert dispatcher._claim_worker(tracker, []) is None

        intel._pending_removal = True
        assert dispatcher._claim_worker(tracker, []) is None  # within GPU_WAIT_RECHECK_S of the last sweep
        tracker.all_waiting_at -= GPU_WAIT_RECHECK_S
        worker, item = dispatcher._claim_worker(tracker, [])

        assert (worker, item.canonical_path) == (pool.workers[0], "/m/a.mkv")  # the Quadro, for its CPU rerun

    def test_a_free_gpu_isnt_missed_when_another_gets_busy_at_the_same_time(self):
        pool = _pool("quadro", "intel")
        intel = pool.workers[1]
        intel.is_busy = True
        dispatcher, waiting = self._all_waiting(pool)
        other = JobTracker("x", _items(), _config(), MagicMock(), kind="intro_credits")
        other.item_queue.extend(_items(*(f"/m/x{n}.mkv" for n in range(5))))
        waiting.last_picked, other.last_picked = 0, 1  # the waiting job is ahead
        dispatcher._trackers.update(j1=waiting, x=other)
        picks = []

        def assign(worker, item, *_args, **_kwargs):
            picks.append((worker, item.canonical_path))

        with patch.object(Worker, "assign_task", autospec=True, side_effect=assign):
            dispatcher._assign_tasks()  # the other job takes the free Quadro; the waiting files all wait
            intel.is_busy = False  # the Intel frees up: as many workers are free as before, but not the same one
            dispatcher._assign_tasks()

        assert (intel, "/m/a.mkv") in picks

    def test_the_sweep_takes_the_first_file_whose_refused_gpus_have_a_free_one(self):
        pool = _pool("quadro", "intel")
        arc = Worker(2, "GPU", "INTEL", "/dev/dri/renderD129", 2, "Arc A380")
        arc.display_name = "Worker 3"
        arc._done_event = pool._worker_done_event
        pool.workers.append(arc)
        pool.workers[1].is_busy = True  # the Intel; the Quadro and the Arc are free
        dispatcher = JobDispatcher(pool)
        tracker = JobTracker("j1", _items(), _config(), MagicMock(), kind="intro_credits")
        refusals = {
            "/m/busy.mkv": {f"gpu:{QUADRO}": "Quadro M1200", "gpu:/dev/dri/renderD129": "Arc"},  # only the Intel left
            "/m/free.mkv": {f"gpu:{QUADRO}": "Quadro M1200"},  # the third GPU is free
        }
        for item in _items("/m/busy.mkv", "/m/free.mkv", "/m/h264.mkv"):
            tracker.item_queue.append(item)
        for path, refused in refusals.items():
            tracker.gpus_refused[path] = refused
            tracker.refused_keys[path] = frozenset(refused)

        worker, item = dispatcher._claim_worker(tracker, [])

        assert arc.group_resource == "gpu:/dev/dri/renderD129"
        assert (worker, item.canonical_path) == (arc, "/m/free.mkv")

    def test_a_requeue_keeps_the_sweeps_key_with_the_refused_gpus(self):
        pool = _pool("quadro", "intel")
        dispatcher = JobDispatcher(pool)
        tracker = JobTracker("j1", _items(), _config(), MagicMock(), kind="intro_credits")
        (item,) = _items("/m/hevc.mkv")
        for worker in pool.workers:
            worker.last_task_handed_off = CodecNotSupportedError(LACKS_HEVC)
            worker.media_title = "hevc.mkv"
            dispatcher._requeue_for_another_gpu(worker, tracker, item)
            tracker.item_queue.popleft()

        assert tracker.refused_keys == {"/m/hevc.mkv": frozenset(tracker.gpus_refused["/m/hevc.mkv"])}
        assert len(tracker.refused_keys["/m/hevc.mkv"]) == 2
