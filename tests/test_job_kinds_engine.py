"""Per-kind handlers in the shared dispatcher/worker (spec §6.4)."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.job_kinds import ItemOutcome, KindHandlers
from media_preview_generator.jobs.dispatcher import JobDispatcher
from media_preview_generator.jobs.worker import WorkerPool
from media_preview_generator.processing.generator import CancellationError, CodecNotSupportedError
from media_preview_generator.processing.types import ProcessableItem
from media_preview_generator.servers.base import ServerType

KEYS = ("markers_published", "markers_needs_review", "failed")


def _config():
    c = MagicMock()
    c.cpu_threads = 1
    c.gpu_threads = 0
    c.scan_workers = 2
    c.regenerate_thumbnails = False
    c.server_id_filter = None
    return c


def _items(*paths):
    return [ProcessableItem(canonical_path=p, server_id="s1", title=p) for p in paths]


def _row(status="markers_written"):
    return {
        "server_id": "s1",
        "server_name": "S1",
        "server_type": "plex",
        "adapter_name": "markers",
        "status": status,
        "message": "",
        "canonical_path": "/m/a.mkv",
        "frame_source": "",
        "output_paths": [],
    }


def test_terminal_check_outcome_never_uses_a_worker():
    pool = WorkerPool(cpu_workers=1, gpu_workers=0, selected_gpus=[])
    dispatcher = JobDispatcher(pool)
    process = MagicMock()
    handlers = KindHandlers(
        check_fn=lambda item, *, cancel_check: ItemOutcome("markers_published", "ok", [_row()]),
        process_fn=process,
        outcome_keys=KEYS,
    )
    with (
        patch("media_preview_generator.processing.generator._notify_file_result") as notify,
        patch("media_preview_generator.web.jobs.get_job_manager"),
    ):
        tracker = dispatcher.submit_items(
            "j1", _items("/m/a.mkv"), _config(), MagicMock(), kind="intro_credits", handlers=handlers
        )
        assert tracker.wait(timeout=10)
    process.assert_not_called()
    assert tracker.outcome_counts == {"markers_published": 1, "markers_needs_review": 0, "failed": 0}
    assert tracker.successful == 1
    args, kwargs = notify.call_args
    assert args[0] == "/m/a.mkv" and args[1].value == "markers_published" and args[2] == "ok"
    assert args[3] == "Lookup" and kwargs["servers"] == [_row()]
    assert tracker.publishers_aggregate["s1"]["counts"] == {"markers_written": 1}
    dispatcher.shutdown()


def test_none_from_check_routes_item_to_worker_with_kwargs():
    pool = WorkerPool(cpu_workers=1, gpu_workers=0, selected_gpus=[])
    dispatcher = JobDispatcher(pool)
    (worker,) = pool._snapshot_workers()
    seen = {}

    def cancel_cb():
        return False

    def pause_cb():
        return False

    def check(item, *, cancel_check):
        seen["check_cancel_check"] = cancel_check
        return None

    def process(item, **kwargs):
        seen["item"] = item
        seen["kwargs"] = kwargs
        kwargs["phase_callback"]("Detecting intro")
        seen["phase_on_worker"] = worker.current_phase
        return ItemOutcome("markers_needs_review", "no agreement", [_row("markers_needs_review")])

    handlers = KindHandlers(check_fn=check, process_fn=process, outcome_keys=KEYS)
    with (
        patch("media_preview_generator.processing.generator._notify_file_result"),
        patch("media_preview_generator.web.jobs.get_job_manager"),
    ):
        tracker = dispatcher.submit_items(
            "j2",
            _items("/m/b.mkv"),
            _config(),
            MagicMock(),
            callbacks={"cancel_check": cancel_cb, "pause_check": pause_cb},
            kind="intro_credits",
            handlers=handlers,
        )
        assert tracker.wait(timeout=10)
    assert seen["check_cancel_check"] is cancel_cb and tracker.cancel_check is cancel_cb
    assert seen["item"].canonical_path == "/m/b.mkv"
    kwargs = seen["kwargs"]
    assert set(kwargs) == {
        "gpu",
        "gpu_device_path",
        "progress_callback",
        "phase_callback",
        "cancel_check",
        "pause_check",
    }
    assert kwargs["gpu"] is None and kwargs["gpu_device_path"] is None
    assert kwargs["cancel_check"] is cancel_cb
    assert kwargs["pause_check"] is pause_cb
    # The dispatcher hands each worker partial(pool._update_worker_progress, worker); the worker forwards it as-is.
    assert kwargs["progress_callback"].func == pool._update_worker_progress
    assert kwargs["progress_callback"].args == (worker,)
    assert seen["phase_on_worker"] == "Detecting intro"
    assert tracker.outcome_counts["markers_needs_review"] == 1
    dispatcher.shutdown()


def test_check_exception_routes_to_worker():
    pool = WorkerPool(cpu_workers=1, gpu_workers=0, selected_gpus=[])
    dispatcher = JobDispatcher(pool)
    (worker,) = pool._snapshot_workers()
    seen = {}

    def cancel_cb():
        return False

    def pause_cb():
        return False

    def check(item, *, cancel_check):
        seen["check_cancel_check"] = cancel_check
        raise RuntimeError("boom")

    process = MagicMock(return_value=ItemOutcome("markers_published"))
    handlers = KindHandlers(check_fn=check, process_fn=process, outcome_keys=KEYS)
    with (
        patch("media_preview_generator.processing.generator._notify_file_result"),
        patch("media_preview_generator.web.jobs.get_job_manager"),
    ):
        tracker = dispatcher.submit_items(
            "j3",
            _items("/m/c.mkv"),
            _config(),
            MagicMock(),
            callbacks={"cancel_check": cancel_cb, "pause_check": pause_cb},
            kind="intro_credits",
            handlers=handlers,
        )
        assert tracker.wait(timeout=10)
    assert seen["check_cancel_check"] is tracker.cancel_check is cancel_cb
    assert process.call_count == 1
    call = process.call_args
    assert call.args[0].canonical_path == "/m/c.mkv"
    assert call.kwargs["gpu"] is None and call.kwargs["gpu_device_path"] is None
    assert call.kwargs["cancel_check"] is cancel_cb
    assert call.kwargs["pause_check"] is pause_cb
    assert call.kwargs["progress_callback"].func == pool._update_worker_progress
    assert call.kwargs["progress_callback"].args == (worker,)
    assert tracker.outcome_counts == {"markers_published": 1, "markers_needs_review": 0, "failed": 0}
    assert tracker.failed == 0 and tracker.successful == 1
    dispatcher.shutdown()


@pytest.mark.parametrize("bad", [object(), ItemOutcome("not_a_known_key", "?")])
def test_malformed_check_outcome_counts_as_failed_and_job_completes(bad):
    pool = WorkerPool(cpu_workers=1, gpu_workers=0, selected_gpus=[])
    dispatcher = JobDispatcher(pool)
    process = MagicMock()
    handlers = KindHandlers(check_fn=lambda item, *, cancel_check: bad, process_fn=process, outcome_keys=KEYS)
    with (
        patch("media_preview_generator.processing.generator._notify_file_result") as notify,
        patch("media_preview_generator.web.jobs.get_job_manager"),
    ):
        tracker = dispatcher.submit_items(
            "j3b", _items("/m/bad.mkv"), _config(), MagicMock(), kind="intro_credits", handlers=handlers
        )
        assert tracker.wait(timeout=10)
    process.assert_not_called()
    assert tracker.outcome_counts == {"markers_published": 0, "markers_needs_review": 0, "failed": 1}
    assert tracker.failed == 1 and tracker.successful == 0
    assert notify.call_args.args[1].value == "failed"
    dispatcher.shutdown()


def test_recording_error_still_completes_the_item():
    pool = WorkerPool(cpu_workers=1, gpu_workers=0, selected_gpus=[])
    dispatcher = JobDispatcher(pool)
    # publisher_rows=5 makes list() raise inside the recording block — the item must still complete.
    handlers = KindHandlers(
        check_fn=lambda item, *, cancel_check: ItemOutcome("markers_published", "ok", 5),
        process_fn=MagicMock(),
        outcome_keys=KEYS,
    )
    with (
        patch("media_preview_generator.processing.generator._notify_file_result"),
        patch("media_preview_generator.web.jobs.get_job_manager"),
    ):
        tracker = dispatcher.submit_items(
            "j3c", _items("/m/e.mkv"), _config(), MagicMock(), kind="intro_credits", handlers=handlers
        )
        assert tracker.wait(timeout=10)
    assert tracker.completed == 1 and tracker.successful == 1
    assert sum(tracker.outcome_counts.values()) == 1
    dispatcher.shutdown()


@pytest.mark.parametrize(
    "rows",
    [[object()], [{"server_id": "s1", "status": ["unhashable"]}]],
    ids=["non_dict_row", "dict_row_the_fold_rejects"],
)
def test_worker_stage_bad_publisher_rows_still_complete_the_item(rows):
    """The worker is idle once it returns, so a merge that raised before record_completion hung the job forever."""
    pool = WorkerPool(cpu_workers=1, gpu_workers=0, selected_gpus=[])
    dispatcher = JobDispatcher(pool)
    handlers = KindHandlers(
        check_fn=lambda item, *, cancel_check: None,
        process_fn=lambda item, **kw: ItemOutcome("markers_published", "ok", rows),
        outcome_keys=KEYS,
    )
    with patch("media_preview_generator.web.jobs.get_job_manager"):
        tracker = dispatcher.submit_items(
            "j3w", _items("/m/w.mkv"), _config(), MagicMock(), kind="intro_credits", handlers=handlers
        )
        assert tracker.wait(timeout=10)
    assert tracker.completed == 1 and tracker.successful == 1
    assert tracker.outcome_counts == {"markers_published": 1, "markers_needs_review": 0, "failed": 0}
    dispatcher.shutdown()


def test_merge_error_still_completes_the_worker_item():
    pool = WorkerPool(cpu_workers=1, gpu_workers=0, selected_gpus=[])
    dispatcher = JobDispatcher(pool)
    handlers = KindHandlers(
        check_fn=lambda item, *, cancel_check: None,
        process_fn=lambda item, **kw: ItemOutcome("markers_published"),
        outcome_keys=KEYS,
    )
    with patch.object(JobDispatcher, "_merge_worker_outcome", side_effect=RuntimeError("merge slip")):
        tracker = dispatcher.submit_items(
            "j3m", _items("/m/m.mkv"), _config(), MagicMock(), kind="intro_credits", handlers=handlers
        )
        assert tracker.wait(timeout=10)
    assert tracker.completed == 1 and tracker.successful == 1
    dispatcher.shutdown()


def test_worker_outcome_with_unknown_key_counts_as_failed():
    from media_preview_generator.jobs.worker import Worker

    w = Worker(1, "CPU")
    done = threading.Event()
    w._done_event = done
    with patch("media_preview_generator.jobs.worker._notify_file_result") as notify:
        w.assign_task(
            _items("/m/u.mkv")[0],
            _config(),
            MagicMock(),
            job_id="j3d",
            process_fn=lambda item, **kw: ItemOutcome("mystery"),
            outcome_keys=KEYS,
        )
        assert done.wait(timeout=10)
        w.current_thread.join(timeout=5)
    assert w.failed == 1 and w.completed == 0
    delta = w.last_task_outcome_delta()
    assert delta["failed"] == 1 and delta.get("mystery", 0) == 0
    assert notify.call_args.args[1].value == "failed"


def test_normalize_outcome_matrix():
    from media_preview_generator.job_kinds import normalize_outcome

    good = ItemOutcome("markers_published", "ok")
    assert normalize_outcome(good, KEYS) is good
    assert normalize_outcome(ItemOutcome("failed", "x"), KEYS).outcome_key == "failed"
    assert normalize_outcome(ItemOutcome("mystery"), KEYS).outcome_key == "failed"
    assert normalize_outcome("markers_published", KEYS).outcome_key == "failed"
    assert normalize_outcome(None, KEYS).outcome_key == "failed"


@pytest.mark.parametrize(("worker_type", "expect_cpu_rerun"), [("GPU", True), ("CPU", False)])
def test_codec_error_reruns_on_cpu_only_for_gpu_workers(worker_type, expect_cpu_rerun):
    from media_preview_generator.jobs.worker import Worker

    calls = []

    def process(item, **kwargs):
        calls.append((kwargs["gpu"], kwargs["gpu_device_path"]))
        if kwargs["gpu"] is not None:
            raise CodecNotSupportedError("hevc 10-bit unsupported")
        if worker_type == "CPU":
            raise CodecNotSupportedError("cpu codec error")
        return ItemOutcome("markers_published", "cpu ok")

    w = Worker(
        1,
        worker_type,
        gpu="NVIDIA" if worker_type == "GPU" else None,
        gpu_device="cuda:0" if worker_type == "GPU" else None,
    )
    done = threading.Event()
    w._done_event = done
    with patch("media_preview_generator.jobs.worker._notify_file_result"):
        w.assign_task(_items("/m/d.mkv")[0], _config(), MagicMock(), job_id="j4", process_fn=process, outcome_keys=KEYS)
        assert done.wait(timeout=10)
        w.current_thread.join(timeout=5)
    if expect_cpu_rerun:
        assert calls == [("NVIDIA", "cuda:0"), (None, None)]
        assert w.fallback_active is True and "hevc" in w.fallback_reason
        assert w.last_task_outcome_delta()["markers_published"] == 1
        assert w.completed == 1
    else:
        assert calls == [(None, None)]
        assert w.failed == 1 and w.last_task_outcome_delta()["failed"] == 1


@pytest.mark.parametrize(
    ("worker_type", "cancel_requested", "effects", "expected_calls", "expected_message"),
    [
        ("CPU", False, [CancellationError("stop")], [(None, None)], "cancelled by user"),
        ("GPU", True, [CodecNotSupportedError("hevc")], [("NVIDIA", "cuda:0")], "codec error: hevc"),
        (
            "GPU",
            False,
            [CodecNotSupportedError("hevc"), RuntimeError("disk gone")],
            [("NVIDIA", "cuda:0"), (None, None)],
            "CPU fallback failed: disk gone",
        ),
        (
            "GPU",
            False,
            [CodecNotSupportedError("hevc"), CancellationError("stop")],
            [("NVIDIA", "cuda:0"), (None, None)],
            "cancelled during CPU fallback",
        ),
        ("CPU", False, [ValueError("bad frame")], [(None, None)], "bad frame"),
    ],
    ids=[
        "cancelled_on_first_run",
        "gpu_codec_error_after_cancel_skips_cpu_retry",
        "cpu_retry_raises",
        "cpu_retry_cancelled",
        "generic_exception",
    ],
)
def test_custom_item_cancel_and_fallback_failures_count_once_as_failed(
    worker_type, cancel_requested, effects, expected_calls, expected_message
):
    from media_preview_generator.jobs.worker import Worker

    calls = []
    remaining = list(effects)

    def process(item, **kwargs):
        calls.append((kwargs["gpu"], kwargs["gpu_device_path"]))
        raise remaining.pop(0)

    is_gpu = worker_type == "GPU"
    w = Worker(1, worker_type, gpu="NVIDIA" if is_gpu else None, gpu_device="cuda:0" if is_gpu else None)
    done = threading.Event()
    w._done_event = done
    with patch("media_preview_generator.jobs.worker._notify_file_result") as notify:
        w.assign_task(
            _items("/m/f.mkv")[0],
            _config(),
            MagicMock(),
            job_id="j5",
            cancel_check=lambda: cancel_requested,
            process_fn=process,
            outcome_keys=KEYS,
        )
        assert done.wait(timeout=10)
        w.current_thread.join(timeout=5)
    assert calls == expected_calls
    assert w.failed == 1 and w.completed == 0
    assert w.last_task_outcome_delta()["failed"] == 1
    assert notify.call_count == 1
    assert notify.call_args.args[1].value == "failed"
    assert notify.call_args.args[2] == expected_message
    if cancel_requested:
        assert w.fallback_active is False


def test_paused_kind_tracker_does_not_block_other_tracker():
    pool = WorkerPool(cpu_workers=1, gpu_workers=0, selected_gpus=[])
    dispatcher = JobDispatcher(pool)
    ran = []
    handlers = KindHandlers(
        check_fn=lambda item, *, cancel_check: None,
        process_fn=lambda item, **kw: ran.append(item.canonical_path) or ItemOutcome("markers_published"),
        outcome_keys=KEYS,
    )
    paused = {"value": True}
    with (
        patch("media_preview_generator.processing.generator._notify_file_result"),
        patch("media_preview_generator.web.jobs.get_job_manager"),
    ):
        t_paused = dispatcher.submit_items(
            "paused",
            _items("/m/p.mkv"),
            _config(),
            MagicMock(),
            kind="intro_credits",
            handlers=handlers,
            callbacks={"pause_check": lambda: paused["value"]},
        )
        t_live = dispatcher.submit_items(
            "live", _items("/m/l.mkv"), _config(), MagicMock(), kind="intro_credits", handlers=handlers
        )
        assert t_live.wait(timeout=10)
        assert not t_paused.wait(timeout=0.5)
        paused["value"] = False
        assert t_paused.wait(timeout=10)
    assert ran == ["/m/l.mkv", "/m/p.mkv"]
    dispatcher.shutdown()


@pytest.mark.parametrize("kind", ["previews", "intro_credits"])
def test_check_thread_raising_still_releases_global_and_kind_slots(kind):
    """A leaked per-kind count would cap that kind below its share forever (or at zero once it hits the cap)."""
    from media_preview_generator.jobs.dispatcher import JobTracker

    pool = WorkerPool(cpu_workers=0, gpu_workers=0, selected_gpus=[])
    dispatcher = JobDispatcher(pool)
    handlers = None
    if kind == "intro_credits":
        handlers = KindHandlers(check_fn=MagicMock(), process_fn=MagicMock(), outcome_keys=KEYS, check_share=0.5)
    tracker = JobTracker("jr", _items("/m/r.mkv"), _config(), MagicMock(), kind=kind, handlers=handlers)
    dispatcher._checks_in_flight = 1
    dispatcher._checks_by_kind = {kind: 1}
    with (
        patch.object(JobDispatcher, "_run_check", side_effect=RuntimeError("engine slip")),
        pytest.raises(RuntimeError),
    ):
        dispatcher._run_check_and_release(tracker, tracker.check_queue[0])
    assert dispatcher._checks_in_flight == 0
    assert dispatcher._checks_by_kind == {kind: 0}
    dispatcher.shutdown()


def test_failed_check_thread_start_releases_slots_and_keeps_the_item():
    from media_preview_generator.jobs.dispatcher import JobTracker

    pool = WorkerPool(cpu_workers=0, gpu_workers=0, selected_gpus=[])
    dispatcher = JobDispatcher(pool)
    handlers = KindHandlers(
        check_fn=lambda item, *, cancel_check: ItemOutcome("markers_published"),
        process_fn=MagicMock(),
        outcome_keys=KEYS,
    )
    items = _items("/m/t1.mkv", "/m/t2.mkv")
    tracker = JobTracker("jt", items, _config(), MagicMock(), kind="intro_credits", handlers=handlers)
    dispatcher._trackers["jt"] = tracker
    dispatcher._ensure_check_pool_running(_config())

    class _UnstartableThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    with (
        patch("media_preview_generator.jobs.dispatcher.threading", SimpleNamespace(Thread=_UnstartableThread)),
        pytest.raises(RuntimeError),
    ):
        dispatcher._submit_checks()
    assert dispatcher._checks_in_flight == 0
    assert dispatcher._checks_by_kind == {"intro_credits": 0}
    assert list(tracker.check_queue) == items

    # The next tick, with thread creation working again, still checks every item.
    with patch("media_preview_generator.processing.generator._notify_file_result"):
        dispatcher._submit_checks()
        assert tracker.wait(timeout=10)
    assert tracker.outcome_counts["markers_published"] == 2
    dispatcher.shutdown()


def test_pool_loop_ignores_non_preview_outcome_keys():
    """Shared-pool workers can carry Intro & Credits keys; the pool's own loop must not KeyError on them."""
    pool = WorkerPool(cpu_workers=1, gpu_workers=0, selected_gpus=[])
    (worker,) = pool._snapshot_workers()
    worker.outcome_counts["markers_published"] = 1
    worker.outcome_counts["skipped_bif_exists"] = 2
    result = pool.process_items_headless([], _config(), MagicMock(), cancel_check=lambda: True)
    assert result["outcome"]["skipped_bif_exists"] == 2
    assert "markers_published" not in result["outcome"]
    pool.shutdown()


def test_kind_check_share_caps_in_flight_checks_and_leaves_room_for_previews():
    """Online lookups can sleep on rate limits inside check_fn; a backfill must not fill every checking thread."""
    from tests.conftest import _ms

    pool = WorkerPool(cpu_workers=1, gpu_workers=0, selected_gpus=[])
    dispatcher = JobDispatcher(pool)
    release = threading.Event()
    lock = threading.Lock()
    in_flight = []
    peak = {"n": 0}

    def slow_check(item, *, cancel_check):
        with lock:
            in_flight.append(item.canonical_path)
            peak["n"] = max(peak["n"], len(in_flight))
        release.wait(timeout=10)
        with lock:
            in_flight.remove(item.canonical_path)
        return ItemOutcome("markers_published")

    handlers = KindHandlers(check_fn=slow_check, process_fn=MagicMock(), outcome_keys=KEYS, check_share=0.25)
    config = _config()
    config.scan_workers = 8  # 8 checking threads → Intro & Credits may hold 2
    with (
        patch("media_preview_generator.processing.generator._notify_file_result"),
        patch("media_preview_generator.web.jobs.get_job_manager"),
        patch(
            "media_preview_generator.processing.multi_server.process_canonical_path",
            side_effect=lambda **kw: _ms("skipped", canonical_path=kw["canonical_path"]),
        ),
    ):
        try:
            paths = [f"/m/ic{i}.mkv" for i in range(6)]
            t_ic = dispatcher.submit_items(
                "ic", _items(*paths), config, MagicMock(), kind="intro_credits", handlers=handlers
            )
            for _ in range(100):
                if len(in_flight) == 2:
                    break
                time.sleep(0.05)
            time.sleep(0.3)
            assert peak["n"] == 2
            t_prev = dispatcher.submit_items("prev", _items("/m/p1.mkv", "/m/p2.mkv"), config, MagicMock())
            assert t_prev.wait(timeout=10)  # previews still get checking threads while lookups are stuck
            release.set()
            assert t_ic.wait(timeout=10)
        finally:
            release.set()
            dispatcher.shutdown()
    assert peak["n"] == 2
    assert t_ic.outcome_counts["markers_published"] == 6


def test_previews_tracker_without_handlers_still_calls_process_canonical_path_with_same_kwargs():
    from tests.conftest import _ms

    pool = WorkerPool(cpu_workers=1, gpu_workers=0, selected_gpus=[])
    dispatcher = JobDispatcher(pool)
    calls = []

    def pcp(**kwargs):
        calls.append(kwargs)
        return _ms("skipped", canonical_path=kwargs["canonical_path"])

    # A Plex originator fans out (no pin); a bare MagicMock registry would read as non-Plex and pin to "s1".
    registry = MagicMock()
    registry.get_config.return_value = MagicMock(type=ServerType.PLEX)
    with patch("media_preview_generator.processing.multi_server.process_canonical_path", side_effect=pcp):
        tracker = dispatcher.submit_items("prev", _items("/m/x.mkv"), _config(), registry)
        assert tracker.wait(timeout=10)
    assert tracker.kind == "previews" and tracker.handlers is None
    assert calls[0]["check_only"] is True and calls[0]["canonical_path"] == "/m/x.mkv"
    assert calls[0]["server_id_filter"] is None and calls[0]["gpu"] is None
    dispatcher.shutdown()
