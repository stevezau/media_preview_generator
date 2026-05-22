"""Stage-1 tests for the dispatcher's checking stage (issue #243 unification).

The shared :class:`JobDispatcher` now runs a ``check_only`` scan pass on
submitted items BEFORE they can claim a GPU/CPU processing worker:

* items already fresh (or otherwise terminal) are recorded straight away —
  no processing worker is ever used,
* only items that report NEEDS_GENERATION reach the processing workers,
* the processing-worker count still caps concurrent FFmpeg work even though
  the checking pool sweeps at higher concurrency,
* accounting matches the item count regardless of which stage finished each
  item.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

from media_preview_generator.jobs.dispatcher import JobDispatcher
from media_preview_generator.jobs.worker import WorkerPool
from media_preview_generator.processing.multi_server import (
    MultiServerResult,
    MultiServerStatus,
)
from tests.conftest import _ms, _pi_list_or_passthrough  # noqa: F401

PCP = "media_preview_generator.processing.multi_server.process_canonical_path"


def _make_config(cpu_threads=1, scan_workers=0):
    config = MagicMock()
    config.cpu_threads = cpu_threads
    config.gpu_threads = 0
    config.scan_workers = scan_workers
    config.worker_pool_timeout = 5
    config.regenerate_thumbnails = False
    config.server_id_filter = None
    return config


def _needs_gen(canonical_path):
    return MultiServerResult(canonical_path=canonical_path, status=MultiServerStatus.NEEDS_GENERATION)


def test_fresh_item_recorded_without_processing_worker():
    pool = WorkerPool(cpu_workers=1, gpu_workers=0, selected_gpus=[])
    dispatcher = JobDispatcher(pool)
    gen_calls: list[str] = []

    def pcp(**kwargs):
        if kwargs.get("check_only"):
            return _ms("skipped", canonical_path=kwargs["canonical_path"])
        gen_calls.append(kwargs["canonical_path"])
        return _ms("generated", canonical_path=kwargs["canonical_path"])

    with patch(PCP, side_effect=pcp):
        tracker = dispatcher.submit_items(
            job_id="j-fresh",
            items=_pi_list_or_passthrough([("k1", "Fresh", "movie")]),
            config=_make_config(),
            registry=MagicMock(),
        )
        assert tracker.wait(timeout=10)

    assert gen_calls == [], "a fresh item must never reach a processing/generation call"
    assert tracker.successful == 1
    assert tracker.outcome_counts.get("skipped_bif_exists", 0) == 1
    dispatcher.shutdown()


def test_needs_generation_item_reaches_processing_worker():
    pool = WorkerPool(cpu_workers=1, gpu_workers=0, selected_gpus=[])
    dispatcher = JobDispatcher(pool)
    gen_calls: list[str] = []

    def pcp(**kwargs):
        if kwargs.get("check_only"):
            return _needs_gen(kwargs["canonical_path"])
        gen_calls.append(kwargs["canonical_path"])
        return _ms("generated", canonical_path=kwargs["canonical_path"])

    with patch(PCP, side_effect=pcp):
        tracker = dispatcher.submit_items(
            job_id="j-gen",
            items=_pi_list_or_passthrough([("k1", "New", "movie")]),
            config=_make_config(),
            registry=MagicMock(),
        )
        assert tracker.wait(timeout=10)

    assert len(gen_calls) == 1, "a NEEDS_GENERATION item must be processed by a worker"
    assert tracker.successful == 1
    assert tracker.outcome_counts.get("generated", 0) == 1
    dispatcher.shutdown()


def test_mixed_skip_and_generate_accounting_matches_item_count():
    pool = WorkerPool(cpu_workers=2, gpu_workers=0, selected_gpus=[])
    dispatcher = JobDispatcher(pool)

    def pcp(**kwargs):
        path = kwargs["canonical_path"]
        if kwargs.get("check_only"):
            return _ms("skipped", canonical_path=path) if "skip" in path else _needs_gen(path)
        return _ms("generated", canonical_path=path)

    items = _pi_list_or_passthrough(
        [(f"skip{i}", f"Skip {i}", "movie") for i in range(3)] + [(f"gen{i}", f"Gen {i}", "movie") for i in range(2)]
    )
    with patch(PCP, side_effect=pcp):
        tracker = dispatcher.submit_items(
            job_id="j-mixed",
            items=items,
            config=_make_config(cpu_threads=2),
            registry=MagicMock(),
        )
        assert tracker.wait(timeout=10)

    assert tracker.completed == 5, "every item must be recorded exactly once"
    assert tracker.outcome_counts.get("skipped_bif_exists", 0) == 3
    assert tracker.outcome_counts.get("generated", 0) == 2
    dispatcher.shutdown()


def test_processing_cap_holds_under_high_check_concurrency():
    """Core #243 property on the dispatcher: even though the checking pool is
    wide, simultaneous generation never exceeds the processing-worker count.
    """
    cap = 2
    pool = WorkerPool(cpu_workers=cap, gpu_workers=0, selected_gpus=[])
    dispatcher = JobDispatcher(pool)

    active = 0
    max_active = 0
    lock = threading.Lock()

    def pcp(**kwargs):
        nonlocal active, max_active
        if kwargs.get("check_only"):
            return _needs_gen(kwargs["canonical_path"])
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.02)
        finally:
            with lock:
                active -= 1
        return _ms("generated", canonical_path=kwargs["canonical_path"])

    items = _pi_list_or_passthrough([(f"k{i}", f"F{i}", "movie") for i in range(20)])
    with patch(PCP, side_effect=pcp):
        tracker = dispatcher.submit_items(
            job_id="j-cap",
            items=items,
            config=_make_config(cpu_threads=cap, scan_workers=16),
            registry=MagicMock(),
        )
        assert tracker.wait(timeout=20)

    assert max_active <= cap, f"generation concurrency {max_active} exceeded processing cap {cap}"
    assert tracker.completed == 20
    dispatcher.shutdown()


def test_checked_item_file_result_routes_to_its_job():
    """HIGH-regression: the check thread must run inside failure_scope so a
    checked item's Files-panel row routes to THIS job's callback. Without it
    (register_job_thread alone routes only logs), _notify_file_result lands in
    the anonymous "" bucket and the per-file row is dropped — the data-loss
    the Architecture Review flagged.
    """
    from media_preview_generator.processing.generator import set_file_result_callback

    pool = WorkerPool(cpu_workers=1, gpu_workers=0, selected_gpus=[])
    dispatcher = JobDispatcher(pool)
    received: list = []
    set_file_result_callback(lambda *a, **k: received.append((a, k)), job_id="j-route")
    try:
        with patch(PCP, side_effect=lambda **kw: _ms("skipped", canonical_path=kw["canonical_path"])):
            tracker = dispatcher.submit_items(
                job_id="j-route",
                items=_pi_list_or_passthrough([("k1", "F", "movie")]),
                config=_make_config(),
                registry=MagicMock(),
            )
            assert tracker.wait(timeout=10)
        assert received, "checked item's file result must route to the job's callback (needs failure_scope)"
    finally:
        set_file_result_callback(None, job_id="j-route")
        dispatcher.shutdown()


def test_check_forwards_resolved_pin_to_process_canonical_path():
    """D34 shape: the check call must forward the resolved per-item pin as
    server_id_filter, exactly like the processing worker — a wrong pin here
    means a wrong skip decision (missing preview). resolve_per_item_pin's
    own matrix is covered in test_worker; here we lock in that the check
    path forwards whatever it returns.
    """
    pool = WorkerPool(cpu_workers=1, gpu_workers=0, selected_gpus=[])
    dispatcher = JobDispatcher(pool)
    captured: dict = {}

    def pcp(**kwargs):
        if kwargs.get("check_only"):
            captured["server_id_filter"] = kwargs.get("server_id_filter")
            return _ms("skipped", canonical_path=kwargs["canonical_path"])
        return _ms("generated", canonical_path=kwargs["canonical_path"])

    with (
        patch("media_preview_generator.jobs.worker.resolve_per_item_pin", return_value="pinned-srv"),
        patch(PCP, side_effect=pcp),
    ):
        tracker = dispatcher.submit_items(
            job_id="j-pin",
            items=_pi_list_or_passthrough([("k1", "F", "movie")]),
            config=_make_config(),
            registry=MagicMock(),
        )
        assert tracker.wait(timeout=10)

    assert captured.get("server_id_filter") == "pinned-srv", (
        f"check call must forward the resolved pin; got {captured.get('server_id_filter')!r}"
    )
    dispatcher.shutdown()


def test_cancel_during_checking_completes_cleanly():
    pool = WorkerPool(cpu_workers=1, gpu_workers=0, selected_gpus=[])
    dispatcher = JobDispatcher(pool)
    cancelled = {"v": False}

    def pcp(**kwargs):
        if kwargs.get("check_only"):
            time.sleep(0.01)
            return _ms("skipped", canonical_path=kwargs["canonical_path"])
        return _ms("generated", canonical_path=kwargs["canonical_path"])

    items = _pi_list_or_passthrough([(f"k{i}", f"F{i}", "movie") for i in range(50)])
    with patch(PCP, side_effect=pcp):
        tracker = dispatcher.submit_items(
            job_id="j-cancel",
            items=items,
            config=_make_config(),
            registry=MagicMock(),
            callbacks={"cancel_check": lambda: cancelled["v"]},
        )
        cancelled["v"] = True
        # Must terminate (not hang) and mark done.
        assert tracker.wait(timeout=10)
        assert tracker.done_event.is_set()
    # Accounting invariant on cancel: no item is recorded more than once
    # (over-count would mean a check + a cancel-drain both counted it). An
    # item in-flight in a check thread at cancel time may legitimately be
    # uncounted (cancel can't see it), so the bound is <=, not ==.
    assert tracker.successful + tracker.failed <= tracker.total_items, (
        f"over-counted on cancel: {tracker.successful}+{tracker.failed} > {tracker.total_items}"
    )
    dispatcher.shutdown()


def test_reused_outputs_counts_cache_hit_publishers():
    """Cross-server reuse: publisher rows whose frames came from cache
    (frame_source == "cache_hit", i.e. reused instead of re-running FFmpeg)
    increment tracker.reused_outputs — the source of the "Reused across N
    outputs" stat. extracted publishers don't count."""
    from media_preview_generator.processing.multi_server import (
        MultiServerResult,
        MultiServerStatus,
        PublisherResult,
        PublisherStatus,
    )

    pool = WorkerPool(cpu_workers=1, gpu_workers=0, selected_gpus=[])
    dispatcher = JobDispatcher(pool)

    def _pub(sid, frame_source):
        return PublisherResult(
            server_id=sid,
            server_name=sid.upper(),
            adapter_name="a",
            status=PublisherStatus.PUBLISHED,
            frame_source=frame_source,
        )

    def pcp(**kwargs):
        if kwargs.get("check_only"):
            return MultiServerResult(canonical_path=kwargs["canonical_path"], status=MultiServerStatus.NEEDS_GENERATION)
        # Generation fanned out to 3 servers: one freshly extracted, two reused.
        return MultiServerResult(
            canonical_path=kwargs["canonical_path"],
            status=MultiServerStatus.PUBLISHED,
            publishers=[_pub("s1", "extracted"), _pub("s2", "cache_hit"), _pub("s3", "cache_hit")],
        )

    with patch(PCP, side_effect=pcp):
        tracker = dispatcher.submit_items(
            job_id="j-reuse",
            items=_pi_list_or_passthrough([("k1", "Movie", "movie")]),
            config=_make_config(),
            registry=MagicMock(),
        )
        assert tracker.wait(timeout=10)

    assert tracker.reused_outputs == 2, "two cache_hit publishers should count as 2 reused outputs"
    assert tracker.outcome_counts.get("generated", 0) == 1, "the file itself is one generated item"
    dispatcher.shutdown()
