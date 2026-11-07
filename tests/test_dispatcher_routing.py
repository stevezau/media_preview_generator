"""Routing rules label preview files with what they need from a worker, matched to what workers offer (``jobs.routing``).

The checking stage asks the rules a job switches on before it queues a file (``JobDispatcher._queue_for_generation``);
``JobDispatcher._claim_worker`` keeps a file labelled as needing a GPU off CPU workers while a GPU worker runs previews,
busy or not, and hands it to any worker once none does. These tests route by a neutral test rule (AV1 files need a GPU);
the HDR rule's own tests are in test_hdr_detection.py.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from unittest.mock import MagicMock, call, patch

import pytest
from loguru import logger

from media_preview_generator.jobs import routing
from media_preview_generator.jobs.dispatcher import (
    EXCLUDED_SCAN_LIMIT,
    JobDispatcher,
    JobTracker,
    _forget_gpu_waits,
    _nodes_off,
    reset_dispatcher,
)
from media_preview_generator.jobs.routing import GPU, Requirement, Rule, file_requirement
from media_preview_generator.jobs.worker import Worker, WorkerPool
from media_preview_generator.processing.generator import CodecNotSupportedError
from media_preview_generator.processing.multi_server import MultiServerResult, MultiServerStatus
from media_preview_generator.processing.types import ProcessableItem
from media_preview_generator.web.settings_manager import reset_settings_manager

QUADRO = "/dev/nvidia0"
INTEL = "/dev/dri/renderD128"
MEDIAINFO = "media_preview_generator.processing.generator.MediaInfo"
PROCESS = "media_preview_generator.processing.multi_server.process_canonical_path"


def _av1_reason(path: str, media_info) -> str | None:
    tracks = getattr(media_info, "video_tracks", None) or []
    return "AV1" if tracks and tracks[0].format == "AV1" else None


AV1_RULE = Rule(
    id="av1",
    title="AV1 files on GPUs",
    description="Labels AV1 files as needing a GPU.",
    capability=GPU,
    reason=_av1_reason,
)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    # A settings manager another test left behind would hand the dispatch loop its saved groups, replacing the pool's.
    reset_settings_manager()
    reset_dispatcher()
    monkeypatch.setattr(routing, "RULES", (AV1_RULE,))
    yield
    reset_settings_manager()
    reset_dispatcher()


def _config(*, rule_on: bool = True):
    c = MagicMock()
    c.cpu_threads = 1
    c.gpu_threads = 0
    c.scan_workers = 4
    c.regenerate_thumbnails = False
    c.server_id_filter = None
    c.webhook_source = None
    c.webhook_deleted_paths = None
    c.routing_rules = {"av1": rule_on}
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


def _job(*paths: str, labelled: tuple[str, ...] = ()) -> JobTracker:
    """A previews job with ``paths`` queued for the workers, ``labelled`` among them needing a GPU as the check left them."""
    tracker = JobTracker("j1", _items(), _config(), MagicMock())
    tracker.item_queue.extend(_items(*paths))
    tracker.requirements.update({path: _needs_gpu("AV1") for path in labelled})
    return tracker


def _needs_gpu(reason: str) -> Requirement:
    return Requirement(frozenset({GPU}), reason)


def _media_info(video_format: str) -> MagicMock:
    return MagicMock(video_tracks=[MagicMock(format=video_format)])


class TestClaim:
    """Which free worker takes which queued file (``_claim_worker``)."""

    def test_a_labelled_file_takes_the_free_gpu(self):
        pool = _pool("quadro", "cpu")
        tracker = _job("/m/dune.mkv", labelled=("/m/dune.mkv",))

        worker, item = JobDispatcher(pool)._claim_worker(tracker, [])

        assert (worker, item.canonical_path) == (pool.workers[0], "/m/dune.mkv")

    def test_a_labelled_file_waits_for_the_busy_gpu_while_the_cpu_takes_a_later_unlabelled_file(self):
        pool = _pool("quadro", "cpu")
        pool.workers[0].is_busy = True
        tracker = _job("/m/dune.mkv", "/m/news.mkv", labelled=("/m/dune.mkv",))

        worker, item = JobDispatcher(pool)._claim_worker(tracker, [])

        assert (worker, item.canonical_path) == (pool.workers[1], "/m/news.mkv")
        assert [i.canonical_path for i in tracker.item_queue] == ["/m/dune.mkv"]

    def test_the_cpu_stays_free_while_only_labelled_files_wait_and_the_gpu_takes_one_once_free(self):
        pool = _pool("quadro", "cpu")
        quadro = pool.workers[0]
        quadro.is_busy = True
        tracker = _job("/m/dune.mkv", "/m/arrival.mkv", labelled=("/m/dune.mkv", "/m/arrival.mkv"))
        dispatcher = JobDispatcher(pool)

        assert dispatcher._claim_worker(tracker, []) is None
        assert not pool.workers[1].is_busy  # the CPU wasn't claimed and left claimed
        quadro.is_busy = False
        worker, item = dispatcher._claim_worker(tracker, [])

        assert (worker, item.canonical_path) == (quadro, "/m/dune.mkv")

    def test_an_sdr_file_still_takes_the_free_gpu_first(self):
        pool = _pool("cpu", "quadro")
        tracker = _job("/m/news.mkv", "/m/dune.mkv", labelled=("/m/dune.mkv",))

        worker, item = JobDispatcher(pool)._claim_worker(tracker, [])

        assert (worker, item.canonical_path) == (pool.workers[1], "/m/news.mkv")

    def test_an_unmarked_file_goes_to_the_cpu_when_the_gpu_is_busy(self):
        pool = _pool("quadro", "cpu")
        pool.workers[0].is_busy = True

        worker, item = JobDispatcher(pool)._claim_worker(_job("/m/dune.mkv"), [])

        assert (worker, item.canonical_path) == (pool.workers[1], "/m/dune.mkv")

    def test_with_no_gpu_worker_a_labelled_file_goes_to_the_cpu(self):
        pool = _pool("cpu")

        worker, item = JobDispatcher(pool)._claim_worker(_job("/m/dune.mkv", labelled=("/m/dune.mkv",)), [])

        assert (worker, item.canonical_path) == (pool.workers[0], "/m/dune.mkv")

    @pytest.mark.parametrize("why", ["being-removed", "no-previews", "frozen-job"])
    def test_a_labelled_file_doesnt_wait_for_a_gpu_that_wont_take_previews(self, why):
        pool = _pool("quadro", "cpu")
        quadro = pool.workers[0]
        quadro.is_busy = True
        dispatcher = JobDispatcher(pool)
        frozen: frozenset[str] = frozenset()
        if why == "being-removed":
            quadro._pending_removal = True
        elif why == "no-previews":
            quadro.allowed_job_types = frozenset({"intro_credits"})
        else:
            quadro.current_job_id = "night"
            frozen = frozenset({"night"})

        worker, item = dispatcher._claim_worker(_job("/m/dune.mkv", labelled=("/m/dune.mkv",)), [], frozen_jobs=frozen)

        assert (worker, item.canonical_path) == (pool.workers[1], "/m/dune.mkv")

    def test_a_labelled_file_every_gpu_refused_goes_to_the_cpu(self):
        pool = _pool("quadro", "cpu")
        pool.workers[0].is_busy = True
        tracker = _job("/m/dune.mkv", labelled=("/m/dune.mkv",))
        tracker.gpus_refused["/m/dune.mkv"] = {f"gpu:{QUADRO}": "Quadro M1200"}
        tracker.refused_keys["/m/dune.mkv"] = frozenset({f"gpu:{QUADRO}"})

        worker, item = JobDispatcher(pool)._claim_worker(tracker, [])

        assert (worker, item.canonical_path) == (pool.workers[1], "/m/dune.mkv")

    def test_a_labelled_file_one_gpu_refused_waits_for_the_other_gpu(self):
        pool = _pool("quadro", "intel", "cpu")
        intel = pool.workers[1]
        intel.is_busy = True
        tracker = _job("/m/dune.mkv", "/m/news.mkv", labelled=("/m/dune.mkv",))
        tracker.gpus_refused["/m/dune.mkv"] = {f"gpu:{QUADRO}": "Quadro M1200"}
        tracker.refused_keys["/m/dune.mkv"] = frozenset({f"gpu:{QUADRO}"})
        dispatcher = JobDispatcher(pool)

        first = dispatcher._claim_worker(tracker, [])
        second = dispatcher._claim_worker(tracker, [])
        intel.is_busy = False
        third = dispatcher._claim_worker(tracker, [])

        assert (first[0], first[1].canonical_path) == (
            pool.workers[0],
            "/m/news.mkv",
        )  # the Quadro, for the unlabelled file
        assert second is None  # the CPU is free, but the file waits for the Intel
        assert (third[0], third[1].canonical_path) == (intel, "/m/dune.mkv")

    def test_a_job_with_node_limits_keeps_its_labelled_files_off_the_cpu_too(self):
        pool = _pool("quadro", "cpu")
        pool.workers[0].is_busy = True
        tracker = _job("/m/dune.mkv", "/m/news.mkv", labelled=("/m/dune.mkv",))
        tracker.excluded_nodes["/m/elsewhere.mkv"] = {"n1": "NAS"}  # another file's: the job is checked file by file

        worker, item = JobDispatcher(pool)._claim_worker(tracker, [])

        assert (worker, item.canonical_path) == (pool.workers[1], "/m/news.mkv")
        assert [i.canonical_path for i in tracker.item_queue] == ["/m/dune.mkv"]

    def test_a_gpu_freeing_up_is_seen_on_the_next_pass(self):
        pool = _pool("quadro", "cpu")
        quadro = pool.workers[0]
        quadro.is_busy = True
        tracker = _job("/m/dune.mkv", labelled=("/m/dune.mkv",))
        dispatcher = JobDispatcher(pool)

        with patch.object(dispatcher, "_first_free_file", wraps=dispatcher._first_free_file) as sweep:
            assert dispatcher._claim_worker(tracker, []) is None
            assert dispatcher._claim_worker(tracker, []) is None  # nothing changed: no second sweep
            quadro.is_busy = False
            worker, item = dispatcher._claim_worker(tracker, [])

        assert sweep.call_count == 2
        assert (worker, item.canonical_path) == (quadro, "/m/dune.mkv")

    @pytest.mark.parametrize("frozen", [True, False], ids=["frozen", "running"])
    def test_a_dispatch_pass_hands_a_labelled_file_to_the_cpu_only_when_the_busy_gpus_job_is_frozen(self, frozen):
        pool = _pool("quadro", "cpu")
        quadro = pool.workers[0]
        quadro.is_busy, quadro.current_job_id = True, "night"
        dispatcher = JobDispatcher(pool)
        night = JobTracker("night", _items(), _config(), MagicMock(), callbacks={"freeze_check": lambda: frozen})
        dispatcher._trackers.update(night=night, j1=_job("/m/dune.mkv", labelled=("/m/dune.mkv",)))

        with patch.object(Worker, "assign_task", autospec=True) as assign:
            dispatcher._assign_tasks()

        if frozen:  # its schedule may keep the Quadro busy for hours
            worker, item = assign.call_args.args[:2]
            assert (worker, item.canonical_path) == (pool.workers[1], "/m/dune.mkv")
        else:
            assign.assert_not_called()


class TestNodes:
    """A labelled file waits only for a GPU it can go to: never for one on a node that can't see it."""

    @staticmethod
    def _pool_with_node_gpu(*local: str) -> WorkerPool:
        pool = _pool(*local)
        node_gpu = Worker(len(local), "GPU", "INTEL", INTEL, 1, "Intel UHD 630", node_id="n1", node_name="NAS")
        node_gpu.display_name = f"Worker {len(local) + 1}"
        node_gpu._done_event = pool._worker_done_event
        pool.workers.append(node_gpu)
        return pool

    def test_it_waits_for_the_busy_local_gpu_not_the_free_node_gpu_that_cant_see_it(self):
        pool = self._pool_with_node_gpu("quadro", "cpu")
        quadro = pool.workers[0]
        quadro.is_busy = True
        tracker = _job("/m/dune.mkv", labelled=("/m/dune.mkv",))
        tracker.excluded_nodes["/m/dune.mkv"] = {"n1": "NAS"}
        dispatcher = JobDispatcher(pool)

        assert dispatcher._claim_worker(tracker, []) is None
        quadro.is_busy = False
        worker, item = dispatcher._claim_worker(tracker, [])

        assert (worker, item.canonical_path) == (quadro, "/m/dune.mkv")

    def test_it_goes_to_the_cpu_when_the_only_gpu_is_on_a_node_that_cant_see_it(self):
        pool = self._pool_with_node_gpu("cpu")
        tracker = _job("/m/dune.mkv", labelled=("/m/dune.mkv",))
        tracker.excluded_nodes["/m/dune.mkv"] = {"n1": "NAS"}

        worker, item = JobDispatcher(pool)._claim_worker(tracker, [])

        assert (worker, item.canonical_path) == (pool.workers[0], "/m/dune.mkv")

    def test_it_goes_to_the_cpu_when_the_only_gpu_is_busy_on_a_node_that_cant_see_it(self):
        pool = self._pool_with_node_gpu("cpu")
        pool.workers[1].is_busy = True  # the NAS's GPU, the only one
        tracker = _job("/m/dune.mkv", labelled=("/m/dune.mkv",))
        tracker.excluded_nodes["/m/dune.mkv"] = {"n1": "NAS"}

        worker, item = JobDispatcher(pool)._claim_worker(tracker, [])

        assert (worker, item.canonical_path) == (pool.workers[0], "/m/dune.mkv")

    def test_a_free_node_gpu_takes_it(self):
        pool = self._pool_with_node_gpu("cpu")

        worker, item = JobDispatcher(pool)._claim_worker(_job("/m/dune.mkv", labelled=("/m/dune.mkv",)), [])

        assert (worker.node_id, item.canonical_path) == ("n1", "/m/dune.mkv")


class TestGroups:
    """A GPU group that can't run previews now leaves no GPU worker to wait for (``WorkerPool.reconcile_groups``)."""

    @staticmethod
    def _groups(*, gpu_enabled: bool = True, gpu_job_types: tuple[str, ...] = ("previews",)) -> list[dict]:
        def group(group_id: str, member: dict, enabled: bool = True) -> dict:
            return {
                "id": group_id,
                "name": group_id.upper(),
                "enabled": enabled,
                "availability": {"mode": "always", "windows": []},
                "members": [{"id": "m1", "count": 1, **member}],
            }

        return [
            group("gpu", {"resource": "gpu", "device": QUADRO, "job_types": list(gpu_job_types)}, gpu_enabled),
            group("cpu", {"resource": "cpu", "device": None, "job_types": ["previews"]}),
        ]

    @pytest.mark.parametrize(
        ("groups", "gpu_takes_it"),
        [
            ({}, True),
            ({"gpu_enabled": False}, False),
            ({"gpu_job_types": ("intro_credits",)}, False),
        ],
        ids=["gpu-group-runs-previews", "gpu-group-switched-off", "gpu-group-without-previews"],
    )
    def test_a_labelled_file_waits_only_for_a_gpu_group_that_runs_previews(self, groups, gpu_takes_it):
        selected = [("nvidia", QUADRO, {"name": "Quadro M1200", "ffmpeg_threads": 2})]
        pool = WorkerPool(cpu_workers=0, gpu_workers=0, selected_gpus=selected)
        pool.reconcile_groups(self._groups(**groups), selected)
        gpus = [w for w in pool.workers if w.worker_type == "GPU"]
        for gpu in gpus:
            gpu.is_busy = True
        tracker = _job("/m/dune.mkv", labelled=("/m/dune.mkv",))

        claimed = JobDispatcher(pool)._claim_worker(tracker, [])

        if gpu_takes_it:
            assert claimed is None  # the busy GPU is waited for
        else:
            worker, item = claimed
            assert (worker.worker_type, item.canonical_path) == ("CPU", "/m/dune.mkv")


class TestFindWorker:
    def test_requiring_a_gpu_finds_a_free_gpu_worker_and_never_the_cpu(self):
        pool = _pool("cpu", "quadro", "intel")
        quadro, intel = pool.workers[1], pool.workers[2]

        assert pool._find_available_worker(kind="previews", requires=(GPU,)) is quadro
        assert pool._find_available_worker(kind="previews", requires=(GPU,), exclude_gpus={f"gpu:{QUADRO}"}) is intel
        quadro.is_busy = intel.is_busy = True
        assert pool._find_available_worker(kind="previews", requires=(GPU,)) is None
        assert pool._find_available_worker(kind="previews") is pool.workers[0]


class TestCheck:
    """The checking stage marks a file before it queues it (``_queue_for_generation``)."""

    @staticmethod
    def _check(tracker, item, media_info, *, result_path: str | None = None):
        dispatcher = JobDispatcher(_pool("quadro", "cpu"))
        result = MultiServerResult(
            canonical_path=result_path or item.canonical_path, status=MultiServerStatus.NEEDS_GENERATION
        )
        with (
            patch(PROCESS, return_value=result),
            patch(MEDIAINFO) as mediainfo,
        ):
            mediainfo.parse.side_effect = media_info
            dispatcher._run_check(tracker, item)
        return mediainfo.parse

    def test_the_check_queues_a_labelled_file_with_its_requirement_and_reads_only_its_headers(self):
        tracker = JobTracker("j1", _items(), _config(), MagicMock())
        (item,) = _items("/m/Film (2020).mkv")

        parse = self._check(tracker, item, lambda path, **_kw: _media_info("AV1"))

        assert list(tracker.item_queue) == [item]
        assert tracker.requirements == {item.canonical_path: _needs_gpu("AV1")}
        assert parse.call_args_list == [call(item.canonical_path, parse_speed=0)]

    def test_the_check_queues_a_file_no_rule_labels_as_it_is(self):
        tracker = JobTracker("j1", _items(), _config(), MagicMock())
        (item,) = _items("/m/Film (2020).mkv")

        self._check(tracker, item, lambda path, **_kw: _media_info("HEVC"))

        assert list(tracker.item_queue) == [item]
        assert tracker.requirements == {}

    def test_with_the_setting_off_the_check_reads_nothing_and_marks_nothing(self):
        tracker = JobTracker("j1", _items(), _config(rule_on=False), MagicMock())
        (item,) = _items("/m/Film (2020).mkv")

        parse = self._check(tracker, item, lambda path, **_kw: _media_info("AV1"))

        assert list(tracker.item_queue) == [item]
        assert tracker.requirements == {}
        parse.assert_not_called()

    def test_a_config_that_doesnt_say_true_leaves_the_setting_off(self):
        config = _config()
        config.routing_rules = {"av1": MagicMock()}  # truthy, but not True: no stray mock switches a rule on
        tracker = JobTracker("j1", _items(), config, MagicMock())
        (item,) = _items("/m/dune.mkv")

        parse = self._check(tracker, item, lambda path, **_kw: _media_info("AV1"))

        assert tracker.requirements == {}
        parse.assert_not_called()

    def test_the_check_reads_the_file_where_the_check_found_it(self):
        tracker = JobTracker("j1", _items(), _config(), MagicMock())
        (item,) = _items("/data_16tb3/Film (2020).mkv")

        parse = self._check(
            tracker, item, lambda path, **_kw: _media_info("AV1"), result_path="/data_16tb/Film (2020).mkv"
        )

        assert parse.call_args_list == [call("/data_16tb/Film (2020).mkv", parse_speed=0)]
        assert set(tracker.requirements) == {"/data_16tb3/Film (2020).mkv"}  # the queued item's path

    def test_a_file_whose_check_raised_still_gets_its_requirement_and_is_queued(self):
        tracker = JobTracker("j1", _items(), _config(), MagicMock())
        (item,) = _items("/m/dune.mkv")
        dispatcher = JobDispatcher(_pool("quadro", "cpu"))

        with (
            patch(
                PROCESS,
                side_effect=RuntimeError("server API down"),
            ),
            patch(MEDIAINFO) as mediainfo,
        ):
            mediainfo.parse.return_value = _media_info("AV1")
            dispatcher._run_check(tracker, item)

        assert list(tracker.item_queue) == [item]
        assert tracker.requirements == {"/m/dune.mkv": _needs_gpu("AV1")}

    def test_a_job_cancelled_during_the_read_queues_nothing(self):
        tracker = JobTracker("j1", _items(), _config(), MagicMock())
        (item,) = _items("/m/dune.mkv")

        def read_then_cancel(path, **_kw):
            tracker.cancel()
            return _media_info("AV1")

        self._check(tracker, item, read_then_cancel)

        assert list(tracker.item_queue) == []
        assert tracker.requirements == {}


class TestPreviewsJob:
    """A previews job end to end: the dispatch loop, its checking threads and its workers."""

    @staticmethod
    def _run(pool: WorkerPool, paths: list[str], *, rule_on: bool, labelled: set[str], check=None, run=None):
        """Run ``paths`` as a previews job; MediaInfo reads ``labelled`` as AV1 and the rest as HEVC.

        Args:
            check: Called with each file's path and the dispatcher as its check starts.
            run: Called with each file's path and GPU device as a worker runs it.

        Returns:
            The tracker, and each run as ``(path, gpu_device_path)``.
        """
        runs: list[tuple[str, str | None]] = []
        lock = threading.Lock()
        dispatcher = JobDispatcher(pool)

        def pcp(**kwargs):
            path = kwargs["canonical_path"]
            if kwargs.get("check_only"):
                if check is not None:
                    check(path, dispatcher)
                return MultiServerResult(canonical_path=path, status=MultiServerStatus.NEEDS_GENERATION)
            with lock:
                runs.append((path, kwargs["gpu_device_path"]))
            if run is not None:
                run(path, kwargs["gpu_device_path"])
            return MagicMock(status=MultiServerStatus.PUBLISHED, publishers=[], message="", slow_path_interval=None)

        try:
            with (
                patch(PROCESS, side_effect=pcp),
                patch(MEDIAINFO) as mediainfo,
                patch("media_preview_generator.processing.generator._notify_file_result"),
                patch("media_preview_generator.jobs.worker._notify_file_result"),
                patch("media_preview_generator.web.jobs.get_job_manager"),
            ):
                mediainfo.parse.side_effect = lambda path, **_kw: _media_info("AV1" if path in labelled else "HEVC")
                tracker = dispatcher.submit_items("j1", _items(*paths), _config(rule_on=rule_on), MagicMock())
                assert tracker.wait(timeout=15)
        finally:
            dispatcher.shutdown()
        return tracker, runs

    @pytest.mark.parametrize("rule_on", [True, False], ids=["on", "off"])
    def test_while_the_gpu_is_busy_the_cpu_takes_the_unlabelled_file_and_not_the_labelled_one_only_when_on(
        self, rule_on
    ):
        gpu_busy, cpu_ran = threading.Event(), threading.Event()

        def check(path, dispatcher):
            # dune.mkv is queued alone, so the free Quadro takes it; then arrival.mkv, then news.mkv.
            if path == "/m/dune.mkv":
                return
            assert gpu_busy.wait(timeout=10)
            if path == "/m/news.mkv":
                tracker = dispatcher._trackers["j1"]
                deadline = time.monotonic() + 10
                while not ("/m/arrival.mkv" in tracker.requirements or cpu_ran.is_set()):
                    assert time.monotonic() < deadline, "arrival.mkv was never queued"
                    time.sleep(0.01)

        def run(path, device):
            if path == "/m/dune.mkv":
                gpu_busy.set()
                assert cpu_ran.wait(timeout=10), "the CPU worker never ran a file"  # the Quadro stays busy till then
            elif device is None:
                cpu_ran.set()

        tracker, runs = self._run(
            _pool("quadro", "cpu"),
            ["/m/dune.mkv", "/m/arrival.mkv", "/m/news.mkv"],
            rule_on=rule_on,
            labelled={"/m/dune.mkv", "/m/arrival.mkv"},
            check=check,
            run=run,
        )

        devices = dict(runs)
        assert len(runs) == 3
        if rule_on:
            assert devices == {"/m/dune.mkv": QUADRO, "/m/arrival.mkv": QUADRO, "/m/news.mkv": None}
        else:
            assert devices["/m/dune.mkv"] == QUADRO
            assert devices["/m/arrival.mkv"] is None  # the free CPU took the labelled file, as before
        assert (tracker.successful, tracker.failed) == (3, 0)
        assert tracker.requirements == {}  # forgotten once each file finished

    def test_with_no_gpu_worker_the_cpu_runs_the_labelled_files_and_the_job_finishes(self):
        paths = ["/m/dune.mkv", "/m/arrival.mkv", "/m/news.mkv"]

        tracker, runs = self._run(_pool("cpu"), paths, rule_on=True, labelled={"/m/dune.mkv", "/m/arrival.mkv"})

        assert sorted(runs) == sorted((path, None) for path in paths)
        assert (tracker.successful, tracker.failed) == (3, 0)
        assert tracker.requirements == {}

    def test_a_labelled_file_the_gpu_cant_decode_reruns_on_its_cpu_rather_than_waiting(self):
        def run(path, device):
            if device == QUADRO:
                raise CodecNotSupportedError(f"GPU processing failed for {path} (exit code 1)")

        tracker, runs = self._run(
            _pool("quadro", "cpu"), ["/m/dune.mkv"], rule_on=True, labelled={"/m/dune.mkv"}, run=run
        )

        assert runs == [("/m/dune.mkv", QUADRO), ("/m/dune.mkv", None)]
        assert (tracker.successful, tracker.failed, tracker.cpu_fallback_files) == (1, 0, 1)
        assert tracker.requirements == {}


class TestClaimCost:
    def test_thousands_of_waiting_labelled_files_cost_one_gpu_check_per_pass(self):
        pool = _pool("quadro", "cpu")
        pool.workers[0].is_busy = True
        dispatcher = JobDispatcher(pool)
        waiting = [f"/m/dv{n}.mkv" for n in range(2000)]
        tracker = _job(*waiting, "/m/news.mkv", labelled=tuple(waiting))
        assert len(waiting) > EXCLUDED_SCAN_LIMIT  # the unlabelled file sits past the usual scan

        with (
            patch.object(pool, "runs_kind_on_other_gpu", wraps=pool.runs_kind_on_other_gpu) as check,
            patch("media_preview_generator.jobs.dispatcher._nodes_off", wraps=_nodes_off) as nodes_off,
        ):
            worker, item = dispatcher._claim_worker(tracker, [])

        assert (worker, item.canonical_path) == (pool.workers[1], "/m/news.mkv")
        assert check.call_args_list == [call("previews", frozenset(), (), ())]
        assert nodes_off.call_count == 0  # the 2,000 waiting files are swept past in one go
        assert len(tracker.item_queue) == 2000

    def test_a_job_whose_labelled_files_finished_takes_the_fast_path_again(self):
        pool = _pool("quadro", "cpu")
        dispatcher = JobDispatcher(pool)
        tracker = _job("/m/news.mkv", labelled=("/m/dune.mkv",))
        with dispatcher._trackers_lock:
            _forget_gpu_waits(tracker, "/m/dune.mkv")

        with patch.object(dispatcher, "_first_free_file") as sweep:
            worker, item = dispatcher._claim_worker(tracker, [])

        sweep.assert_not_called()
        assert (worker, item.canonical_path) == (pool.workers[0], "/m/news.mkv")


class TestRules:
    """Adding a rule takes one function and its entry in ``routing.RULES``."""

    @staticmethod
    def _vc1_rule(reason=None) -> Rule:
        def vc1_reason(path: str, media_info) -> str | None:
            tracks = getattr(media_info, "video_tracks", None) or []
            return "VC-1" if tracks and tracks[0].codec_id == "WVC1" else None

        return Rule(
            id="vc1",
            title="VC-1 files on GPUs",
            description="Labels VC-1 files as needing a GPU.",
            capability=GPU,
            reason=reason or vc1_reason,
        )

    def test_a_new_rule_needs_only_its_function_and_its_entry(self, monkeypatch):
        monkeypatch.setattr(routing, "RULES", (AV1_RULE, self._vc1_rule()))
        config = _config(rule_on=False)
        config.routing_rules = {"vc1": True}
        tracker = JobTracker("j1", _items(), config, MagicMock())
        (item,) = _items("/m/Film (2020).mkv")
        pool = _pool("quadro", "cpu")
        quadro = pool.workers[0]
        quadro.is_busy = True
        dispatcher = JobDispatcher(pool)
        result = MultiServerResult(canonical_path=item.canonical_path, status=MultiServerStatus.NEEDS_GENERATION)

        with patch(PROCESS, return_value=result), patch(MEDIAINFO) as mediainfo:
            mediainfo.parse.return_value = MagicMock(video_tracks=[MagicMock(format="VC-1", codec_id="WVC1")])
            dispatcher._run_check(tracker, item)

        assert tracker.requirements == {item.canonical_path: _needs_gpu("VC-1")}
        assert dispatcher._claim_worker(tracker, []) is None  # the busy Quadro is waited for, not the free CPU
        quadro.is_busy = False
        assert dispatcher._claim_worker(tracker, []) == (quadro, item)

    def test_each_rule_that_gives_a_reason_adds_it(self, monkeypatch):
        monkeypatch.setattr(routing, "RULES", (AV1_RULE, self._vc1_rule(lambda path, media_info: "VC-1")))
        config = _config()
        config.routing_rules = {"av1": True, "vc1": True}

        with patch(MEDIAINFO) as mediainfo:
            mediainfo.parse.return_value = _media_info("AV1")
            assert file_requirement("/m/Film (2020).mkv", config) == _needs_gpu("AV1, VC-1")

    def test_every_rule_starts_off_and_only_a_saved_true_switches_one_on(self, monkeypatch):
        monkeypatch.setattr(routing, "RULES", (AV1_RULE, self._vc1_rule()))

        assert routing.rule_states(None) == {"av1": False, "vc1": False}
        assert routing.rule_states({"av1": True, "vc1": "true", "gone": True}) == {"av1": True, "vc1": False}

    def test_a_rule_whose_switch_is_off_isnt_asked_and_nothing_is_read(self, monkeypatch):
        asked: list[str] = []
        monkeypatch.setattr(routing, "RULES", (self._vc1_rule(lambda path, media_info: asked.append(path) or "VC-1"),))

        with patch(MEDIAINFO) as mediainfo:
            assert file_requirement("/m/Film (2020).mkv", _config()) is None  # only av1 is on, and it isn't listed

        assert asked == []
        mediainfo.parse.assert_not_called()

    def test_a_rule_that_raises_is_skipped_and_the_file_is_still_queued(self, monkeypatch):
        def broken(path: str, media_info) -> str | None:
            raise ValueError("a bug in a rule")

        monkeypatch.setattr(routing, "RULES", (self._vc1_rule(broken), AV1_RULE))
        config = _config()
        config.routing_rules = {"av1": True, "vc1": True}
        tracker = JobTracker("j1", _items(), config, MagicMock())
        (item,) = _items("/m/Film (2020).mkv")
        result = MultiServerResult(canonical_path=item.canonical_path, status=MultiServerStatus.NEEDS_GENERATION)

        with patch(PROCESS, return_value=result), patch(MEDIAINFO) as mediainfo:
            mediainfo.parse.return_value = _media_info("AV1")
            JobDispatcher(_pool("quadro", "cpu"))._run_check(tracker, item)

        assert list(tracker.item_queue) == [item]
        assert tracker.requirements == {item.canonical_path: _needs_gpu("AV1")}  # the rule after it still answered


class TestQueueing:
    """A checking thread queues a file together with its requirement, under the locks the dispatch loop takes it under."""

    def test_a_file_never_reaches_the_queue_without_its_requirement(self):
        dispatcher = JobDispatcher(_pool("quadro", "cpu"))
        tracker = JobTracker("j1", _items(), _config(), MagicMock())
        seen: list[tuple[bool, bool]] = []

        class Recording(deque):
            def append(self, item):
                seen.append((item.canonical_path in tracker.requirements, dispatcher._trackers_lock._is_owned()))
                super().append(item)

        tracker.item_queue = Recording()
        formats = {"/m/dune.mkv": "AV1", "/m/news.mkv": "HEVC"}

        with patch(MEDIAINFO) as mediainfo:
            mediainfo.parse.side_effect = lambda path, **_kw: _media_info(formats[path])
            for item in _items("/m/dune.mkv", "/m/news.mkv"):
                dispatcher._queue_for_generation(tracker, item, item.canonical_path)

        assert seen == [(True, True), (False, False)]  # an unlabelled file is appended without the lock, as before

    def test_a_cancel_while_the_file_waits_for_a_dispatch_pass_keeps_it_out(self):
        dispatcher = JobDispatcher(_pool("quadro", "cpu"))
        checked = threading.Event()

        def cancel_check() -> bool:
            checked.set()
            return False

        tracker = JobTracker("j1", _items(), _config(), MagicMock(), callbacks={"cancel_check": cancel_check})
        (item,) = _items("/m/dune.mkv")

        with patch(MEDIAINFO) as mediainfo:
            mediainfo.parse.return_value = _media_info("AV1")
            with dispatcher._trackers_lock:  # a dispatch pass holds it
                thread = threading.Thread(
                    target=dispatcher._queue_for_generation, args=(tracker, item, item.canonical_path), daemon=True
                )
                thread.start()
                assert checked.wait(timeout=5)  # past the cancel check, now waiting for the lock
                time.sleep(0.1)
                tracker.cancel()
            thread.join(timeout=5)

        assert not thread.is_alive()
        assert list(tracker.item_queue) == []
        assert tracker.requirements == {}


class TestSiblingMount:
    def test_a_file_found_on_a_sibling_mount_drops_its_requirement_when_it_finishes(self):
        dispatcher = JobDispatcher(_pool("quadro", "cpu"))

        def pcp(**kwargs):
            if kwargs.get("check_only"):
                return MultiServerResult(
                    canonical_path=kwargs["canonical_path"], status=MultiServerStatus.NEEDS_GENERATION
                )
            # FFmpeg's progress names the file where it found it, as ffmpeg_runner reports it.
            kwargs["progress_callback"](50.0, 1.0, 2.0, "1.0x", media_file="/data_16tb/Film (2020).mkv")
            return MagicMock(status=MultiServerStatus.PUBLISHED, publishers=[], message="", slow_path_interval=None)

        try:
            with (
                patch(PROCESS, side_effect=pcp),
                patch(MEDIAINFO) as mediainfo,
                patch("media_preview_generator.processing.generator._notify_file_result"),
                patch("media_preview_generator.jobs.worker._notify_file_result"),
                patch("media_preview_generator.web.jobs.get_job_manager"),
            ):
                mediainfo.parse.return_value = _media_info("AV1")
                tracker = dispatcher.submit_items("j1", _items("/data_16tb3/Film (2020).mkv"), _config(), MagicMock())
                assert tracker.wait(timeout=15)
        finally:
            dispatcher.shutdown()

        assert (tracker.successful, tracker.failed) == (1, 0)
        assert tracker.requirements == {}


class TestNodeLimits:
    """One file's node limits (a node that can't see it, or dropped it) don't hide a job's unlabelled files from a free CPU."""

    def test_a_free_cpu_still_reaches_an_sdr_file_behind_many_files_that_need_a_gpu(self):
        pool = _pool("quadro", "cpu")
        pool.workers[0].is_busy = True
        waiting = [f"/m/dv{n}.mkv" for n in range(EXCLUDED_SCAN_LIMIT + 36)]
        tracker = _job(*waiting, "/m/news.mkv", labelled=tuple(waiting))
        tracker.excluded_nodes["/m/elsewhere.mkv"] = {"n1": "NAS"}  # another file's, long finished

        worker, item = JobDispatcher(pool)._claim_worker(tracker, [])

        assert (worker, item.canonical_path) == (pool.workers[1], "/m/news.mkv")

    def test_thousands_of_waiting_files_cost_one_gpu_check_per_pass(self):
        pool = _pool("quadro", "cpu")
        pool.workers[0].is_busy = True
        dispatcher = JobDispatcher(pool)
        waiting = [f"/m/dv{n}.mkv" for n in range(2000)]
        tracker = _job(*waiting, "/m/hidden.mkv", "/m/news.mkv", labelled=tuple(waiting))
        tracker.excluded_nodes["/m/hidden.mkv"] = {"n1": "NAS"}  # queued, and the node can't see it

        with (
            patch.object(pool, "runs_kind_on_other_gpu", wraps=pool.runs_kind_on_other_gpu) as check,
            patch("media_preview_generator.jobs.dispatcher._nodes_off", wraps=_nodes_off) as nodes_off,
        ):
            worker, item = dispatcher._claim_worker(tracker, [])

        assert (worker, item.canonical_path) == (pool.workers[1], "/m/hidden.mkv")  # this app's CPU can see it
        assert check.call_args_list == [call("previews", frozenset(), (), ())]
        assert nodes_off.call_count == 0  # the 2,000 waiting files are swept past in one go

    def test_with_no_free_worker_a_file_no_worker_can_see_still_fails(self):
        pool = WorkerPool(cpu_workers=0, gpu_workers=0, selected_gpus=[])
        nas = Worker(0, "CPU", node_id="n1", node_name="NAS")
        nas.display_name = "Worker 1"
        nas.is_busy = True
        pool.workers.append(nas)
        tracker = _job("/m/hidden.mkv", "/m/dune.mkv", labelled=("/m/dune.mkv",))
        tracker.excluded_nodes["/m/hidden.mkv"] = {"n1": "NAS"}
        stranded: list = []

        assert JobDispatcher(pool)._claim_worker(tracker, stranded) is None

        assert [(job, item.canonical_path) for job, item, _why in stranded] == [(tracker, "/m/hidden.mkv")]


class TestWhyItWaits:
    """The reason a file waits for a GPU is in the job log and on the job card."""

    @staticmethod
    def _started_job(dispatcher: JobDispatcher) -> JobTracker:
        tracker = _job("/m/dune.mkv", "/m/news.mkv", "/m/arrival.mkv", "/m/mad-max.mkv")
        tracker.requirements.update(
            {
                "/m/dune.mkv": _needs_gpu("AV1"),
                "/m/arrival.mkv": _needs_gpu("VC-1"),
                "/m/mad-max.mkv": _needs_gpu("AV1"),
            }
        )
        tracker.total_items = 10
        tracker.work_started = tracker.generation_started = True
        dispatcher._trackers["j1"] = tracker
        return tracker

    def test_the_job_card_counts_the_files_waiting_for_a_gpu_and_says_why(self):
        pool = _pool("quadro", "cpu")
        pool.workers[0].is_busy = True
        dispatcher = JobDispatcher(pool)
        tracker = self._started_job(dispatcher)

        dispatcher._emit_progress_updates()

        assert tracker.progress_message(3) == "3 files waiting for a GPU (AV1, VC-1) · 3/10 completed"

    def test_the_job_card_says_nothing_of_it_once_no_gpu_worker_runs_previews(self):
        pool = _pool("quadro", "cpu")
        pool.workers[0]._pending_removal = True
        dispatcher = JobDispatcher(pool)
        tracker = self._started_job(dispatcher)

        dispatcher._emit_progress_updates()

        assert tracker.progress_message(3) == "3/10 completed"

    def test_the_job_log_says_why_a_file_waits_for_a_gpu(self):
        lines: list[str] = []
        sink = logger.add(lambda message: lines.append(message.record["message"]), level="INFO")
        tracker = JobTracker("j1", _items(), _config(), MagicMock())
        (item,) = _items("/m/Film (2020).mkv")
        try:
            TestCheck._check(tracker, item, lambda path, **_kw: _media_info("AV1"))
        finally:
            logger.remove(sink)

        assert "Film (2020).mkv matches a routing rule (AV1), so it goes to a GPU worker" in lines


class TestRoutingLog:
    """Where each labelled file went, in its job's log; and the worker card's reason."""

    @staticmethod
    def _assign(pool: WorkerPool) -> tuple[JobDispatcher, list[tuple[str, str]]]:
        dispatcher = JobDispatcher(pool)
        dispatcher._trackers["j1"] = _job("/m/dune.mkv", labelled=("/m/dune.mkv",))
        lines: list[tuple[str, str]] = []
        sink = logger.add(lambda message: lines.append((message.record["level"].name, message.record["message"])))
        try:
            with patch.object(Worker, "_process_item"):  # the worker takes the file and does nothing with it
                dispatcher._assign_tasks()
        finally:
            logger.remove(sink)
        return dispatcher, lines

    def test_a_gpu_worker_that_takes_a_labelled_file_shows_why_and_the_match_is_logged(self):
        dispatcher, lines = self._assign(_pool("quadro", "cpu"))

        rows = {row["worker_type"]: row for row in dispatcher._build_worker_statuses()}
        assert (rows["GPU"]["routing_reason"], rows["CPU"]["routing_reason"]) == ("AV1", "")
        assert ("DEBUG", "dune.mkv goes to Worker 1, as its routing rule asks (AV1)") in lines

    def test_with_no_gpu_worker_the_file_falls_back_to_the_cpu_and_that_is_logged_at_info(self):
        dispatcher, lines = self._assign(_pool("cpu"))

        (row,) = dispatcher._build_worker_statuses()
        assert row["routing_reason"] == ""  # its card claims no GPU it doesn't have
        assert (
            "INFO",
            "dune.mkv matches a routing rule (AV1), but no GPU worker is available. Worker 1 takes it.",
        ) in lines

    @pytest.mark.parametrize(
        ("paths", "said"),
        [
            (("/m/dune.mkv",), "1 file is waiting for a GPU worker (AV1)"),
            (("/m/dune.mkv", "/m/arrival.mkv"), "2 files are waiting for a GPU worker (AV1)"),
        ],
    )
    def test_the_wait_is_logged_once_when_files_start_waiting(self, paths, said):
        pool = _pool("quadro", "cpu")
        pool.workers[0].is_busy = True
        dispatcher = JobDispatcher(pool)
        tracker = _job(*paths, labelled=paths)
        tracker.work_started = tracker.generation_started = True
        dispatcher._trackers["j1"] = tracker
        lines: list[str] = []
        sink = logger.add(lambda message: lines.append(message.record["message"]), level="DEBUG")
        try:
            dispatcher._emit_progress_updates()
            tracker.node_wait_checked_at = 0.0
            dispatcher._emit_progress_updates()  # still waiting: nothing new to say
        finally:
            logger.remove(sink)

        assert [line for line in lines if "waiting for a GPU worker" in line] == [said]
