"""Behavioral coverage for shared group routing, draining and continuations."""

import threading
import time
from types import SimpleNamespace

import pytest

from media_preview_generator.job_kinds import ItemOutcome, KindHandlers
from media_preview_generator.jobs.checkpoints import checkpoint_items, read_checkpoint, write_checkpoint
from media_preview_generator.jobs.dispatcher import JobDispatcher, JobTracker
from media_preview_generator.jobs.worker import WorkerPool
from media_preview_generator.processing.types import ProcessableItem

GPU = [("cuda", "cuda:0", {"name": "Test GPU", "workers": 2, "ffmpeg_threads": 2})]


def group(gid, count=1, *, resource="cpu", kinds=None, enabled=True):
    return {
        "id": gid,
        "name": gid,
        "enabled": enabled,
        "resource": resource,
        "device": None if resource == "cpu" else "cuda:0",
        "count": count,
        "job_types": kinds or ["previews", "intro_credits", "loudness"]
        if resource == "cpu"
        else kinds or ["previews", "intro_credits"],
        "availability": {"mode": "always", "windows": []},
    }


def item(name):
    return ProcessableItem("/media/" + name, "plex", {"plex": name}, name)


def wait_until(predicate):
    until = time.monotonic() + 3
    while not predicate():
        if time.monotonic() >= until:
            pytest.fail("runtime did not reach expected state")
        time.sleep(0.005)


def test_group_scale_down_never_retires_other_cpu_group():
    pool = WorkerPool(0, 0, [])
    pool.reconcile_groups([group("a", 2), group("b")], [])
    a = [w for w in pool.workers if w.group_id == "a"]
    b = next(w for w in pool.workers if w.group_id == "b")
    a[0].is_busy = True
    pool.reconcile_groups([group("a"), group("b")], [])
    assert a[0] in pool.workers and not a[0]._pending_removal
    assert a[1] not in pool.workers
    assert b in pool.workers
    pool.reconcile_groups([group("a", enabled=False), group("b")], [])
    assert a[0]._pending_removal and b.is_available()
    pool.reconcile_groups([group("a"), group("b")], [])
    assert a[0] in pool.workers and not a[0]._pending_removal
    assert len(pool.workers) == 2


@pytest.mark.parametrize("resource", ["cpu", "gpu"])
def test_closed_group_draining_uses_replacement_resource_budget(resource):
    pool = WorkerPool(0, 0, GPU)
    pool.reconcile_groups([group("old", 2, resource=resource)], GPU)
    old = list(pool.workers)
    for worker in old:
        worker.is_busy = True
    pool.reconcile_groups([group("new", 2, resource=resource)], GPU)
    assert all(w._pending_removal for w in old)
    assert pool._find_available_worker(kind="previews", claim=True) is None
    old[0].is_busy = False
    pool._apply_deferred_removals()
    fresh = pool._find_available_worker(kind="previews", claim=True)
    assert fresh.group_id == "new"
    assert pool._find_available_worker(kind="previews", claim=True) is None
    assert sum(w.is_busy for w in pool.workers) == 2


def test_changed_resource_keeps_busy_slot_ownership():
    pool = WorkerPool(0, 0, GPU)
    pool.reconcile_groups([group("work")], GPU)
    old = pool.workers[0]
    old.is_busy = True
    pool.reconcile_groups([group("work", resource="gpu")], GPU)
    assert old.group_resource == "cpu" and old._pending_removal
    gpu = pool._find_available_worker(kind="previews", claim=True)
    assert gpu.group_resource == "gpu:cuda:0"
    assert pool._find_available_worker(kind="loudness", claim=True) is None


def test_gpu_only_never_assigns_loudness_and_skips_incompatible_head():
    pool = WorkerPool(0, 0, GPU)
    pool.reconcile_groups([group("video", resource="gpu")], GPU)
    dispatcher = JobDispatcher(pool)
    taken = []
    release = threading.Event()

    def process(work, **kwargs):
        taken.append((work.title, kwargs["gpu"]))
        release.wait(3)
        return ItemOutcome("done", "ok")

    handlers = KindHandlers(lambda *a, **kw: None, process, ("done",))
    for name, kind in [("first", "loudness"), ("second", "previews")]:
        tracker = JobTracker(name, [], SimpleNamespace(), None, kind=kind, handlers=handlers)
        tracker.item_queue.append(item(name))
        tracker.total_items = 1
        dispatcher._trackers[name] = tracker
    try:
        dispatcher._assign_tasks()
        wait_until(lambda: bool(taken))
        assert taken == [("second", "cuda")]
        assert len(dispatcher._trackers["first"].item_queue) == 1
    finally:
        release.set()
        for worker in pool.workers:
            if worker.current_thread:
                worker.current_thread.join(3)
        dispatcher._check_completions()
    assert dispatcher._trackers["second"].successful == 1


def test_park_waits_for_check_callback_and_does_not_count_unfinished_as_failed(tmp_path):
    pool = WorkerPool(0, 0, [])
    dispatcher = JobDispatcher(pool)
    entered = threading.Event()
    release = threading.Event()

    def check(work, **kwargs):
        entered.set()
        release.wait(3)
        return None

    tracker = JobTracker(
        "job",
        [item("first"), item("second")],
        SimpleNamespace(),
        None,
        kind="loudness",
        handlers=KindHandlers(check, lambda *a, **kw: None, ("done",)),
    )
    dispatcher._trackers["job"] = tracker
    dispatcher._max_checks = 1
    dispatcher._check_pool_started = True
    dispatcher._submit_checks()
    assert entered.wait(3)
    assert dispatcher.request_park("job")
    assert dispatcher.park_snapshot("job") is None
    with pytest.raises(RuntimeError):
        dispatcher.detach_parked("job")
    release.set()
    wait_until(lambda: tracker.active_checks == 0)
    snapshot = dispatcher.park_snapshot("job")
    assert [x["canonical_path"] for x in snapshot["items"]] == ["/media/first", "/media/second"]
    assert snapshot["state"]["failed"] == 0
    reference = write_checkpoint(tmp_path, "job", snapshot, bookkeeping={"retry": ["later"]})
    assert dispatcher.detach_parked("job")
    assert tracker.detached and not tracker.done_event.is_set()
    assert tracker.registry is None and not tracker.item_queue and not tracker.check_queue
    stored = read_checkpoint(tmp_path, "job", reference)
    assert stored["bookkeeping"] == {"retry": ["later"]}
    assert checkpoint_items(stored) == [item("first"), item("second")]


def test_restored_tracker_keeps_success_failure_counts_without_repeating_finished_items():
    state = {
        "successful": 7,
        "failed": 2,
        "failed_paths": ["/media/bad"],
        "outcome_counts": {"done": 7, "failed": 2},
        "publishers_aggregate": {"plex": {"done": 7}},
        "cpu_fallback_files": 3,
        "total_items": 10,
    }
    tracker = JobTracker("same", [item("remaining")], SimpleNamespace(), None, carried_state=state)
    assert tracker.total_items == 10 and tracker.completed == 9
    assert list(tracker.check_queue) == [item("remaining")]
    assert tracker.get_result()["failed"] == 2
    assert tracker.get_result()["failed_paths"] == ["/media/bad"]
    tracker.record_completion(True)
    assert tracker.done_event.is_set() and tracker.completed == 10
    assert tracker.cpu_fallback_files == 3


def test_checkpoint_drops_stale_bundle_metadata_and_rejects_foreign_reference(tmp_path):
    from media_preview_generator.jobs.checkpoints import item_descriptor

    work = ProcessableItem(
        "/media/movie", "plex", {"plex": "42"}, bundle_metadata_by_server={"plex": (("old", "file"),)}
    )
    snapshot = {"kind": "previews", "items": [item_descriptor(work)], "state": {}}
    ref = write_checkpoint(tmp_path, "one", snapshot)
    assert not checkpoint_items(read_checkpoint(tmp_path, "one", ref))[0].bundle_metadata_by_server
    with pytest.raises(ValueError):
        read_checkpoint(tmp_path, "two", ref)
    with pytest.raises(ValueError):
        read_checkpoint(tmp_path, "one", "../settings.json")


@pytest.mark.parametrize("kind", ["previews", "intro_credits", "loudness"])
@pytest.mark.parametrize("resource", ["cpu", "gpu"])
def test_capability_and_policy_matrix(kind, resource):
    pool = WorkerPool(0, 0, GPU)
    allowed = ["previews", "intro_credits", "loudness"]
    pool.reconcile_groups([group("one", resource=resource, kinds=allowed)], GPU)
    worker = pool._find_available_worker(kind=kind, claim=True)
    assert (worker is not None) == (kind != "loudness" or resource == "cpu")
    if worker:
        worker.is_busy = False
    pool.reconcile_groups([group("one", resource=resource, kinds=["intro_credits"])], GPU)
    assert (pool._find_available_worker(kind=kind, claim=True) is not None) == (kind == "intro_credits")


def test_no_eligible_groups_do_not_begin_checking():
    pool = WorkerPool(0, 0, GPU)
    pool.reconcile_groups([group("video", resource="gpu")], GPU)
    dispatcher = JobDispatcher(pool)
    tracker = JobTracker("audio", [item("a")], SimpleNamespace(), None, kind="loudness")
    dispatcher._trackers["audio"] = tracker
    assert dispatcher._get_next_check_item() is None
    assert list(tracker.check_queue) == [item("a")]
    assert tracker.active_checks == 0


def test_resource_capacity_counts_draining_only_once():
    pool = WorkerPool(0, 0, [])
    pool.reconcile_groups([group("old", 2)], [])
    for w in pool.workers:
        w.is_busy = True
    pool.reconcile_groups([group("new", 2)], [])
    assert pool.capacity_for("loudness")["available"] == 0
    pool.workers[0].is_busy = False
    pool._apply_deferred_removals()
    assert pool.capacity_for("loudness")["available"] == 1
    assert next(g for g in pool.member_snapshots() if g["group_id"] == "new")["available"] == 1


def test_current_settings_override_stale_group_config(monkeypatch):
    from media_preview_generator.jobs.group_runtime import refresh_worker_groups

    settings = SimpleNamespace(worker_groups=[group("saved", kinds=["loudness"])])
    settings.get = lambda key: settings.worker_groups if key == "worker_groups" else None
    monkeypatch.setattr("media_preview_generator.web.settings_manager.peek_settings_manager", lambda: settings)
    pool = WorkerPool(0, 0, [])
    stale = SimpleNamespace(worker_groups=[group("old", 4)])
    assert refresh_worker_groups(pool, stale, [], force=True)
    assert [w.group_id for w in pool.workers] == ["saved"]
    settings.worker_groups = []
    assert refresh_worker_groups(pool, stale, [], force=True)
    assert pool.workers == []
    assert pool.capacity_for("loudness")["reason"] == "configuration"


def test_weekly_closure_retires_exact_slots_and_reopen_reuses_busy(monkeypatch):
    from datetime import UTC, datetime

    import media_preview_generator.worker_groups as policy

    clock = [datetime(2026, 10, 5, 22, 59, tzinfo=UTC)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz or UTC)

    monkeypatch.setattr(policy, "datetime", Clock)
    monkeypatch.setenv("TZ", "UTC")
    early = group("early")
    late = group("late")
    early["availability"] = {"mode": "scheduled", "windows": [{"days": [0], "start": "22:00", "end": "23:00"}]}
    late["availability"] = {"mode": "scheduled", "windows": [{"days": [0], "start": "23:00", "end": "07:00"}]}
    pool = WorkerPool(0, 0, [])
    pool.reconcile_groups([early, late], [])
    before = pool._find_available_worker(kind="loudness", claim=True)
    assert before.group_id == "early"
    clock[0] = datetime(2026, 10, 5, 23, 0, tzinfo=UTC)
    # Boundary eligibility is rechecked even before the periodic reconciler.
    assert not pool.has_capacity_for("loudness")
    pool.reconcile_groups([early, late], [])
    assert before._pending_removal
    assert pool.capacity_for("loudness")["open"] == 1
    assert pool._find_available_worker(kind="loudness", claim=True) is None
    before.is_busy = False
    pool._apply_deferred_removals()
    assert pool._find_available_worker(kind="loudness", claim=True).group_id == "late"


def test_checkpoint_write_failure_keeps_live_queues(tmp_path, monkeypatch):
    import media_preview_generator.jobs.checkpoints as checkpoints

    pool = WorkerPool(0, 0, [])
    dispatcher = JobDispatcher(pool)
    tracker = JobTracker("job", [item("a")], SimpleNamespace(), None)
    dispatcher._trackers["job"] = tracker
    dispatcher.request_park("job")
    snapshot = dispatcher.park_snapshot("job")

    def fail_replace(*args):
        raise OSError("disk full")

    monkeypatch.setattr(checkpoints.os, "replace", fail_replace)
    with pytest.raises(OSError):
        checkpoints.write_checkpoint(tmp_path, "job", snapshot)
    assert list(tracker.check_queue) == [item("a")]
    assert not tracker.detached and not tracker.cancelled and tracker.failed == 0
    assert list((tmp_path / "job_checkpoints").iterdir()) == []


def test_park_drains_active_failure_then_restores_remaining_work(tmp_path):
    pool = WorkerPool(0, 0, [])
    pool.reconcile_groups([group("audio", kinds=["loudness"])], [])
    dispatcher = JobDispatcher(pool)
    entered = threading.Event()
    finish = threading.Event()
    processed = []

    def process(work, **kwargs):
        processed.append(work.canonical_path)
        entered.set()
        finish.wait(3)
        return ItemOutcome("failed", "external analysis error")

    handlers = KindHandlers(lambda *a, **kw: None, process, ("done", "failed"))
    tracker = JobTracker("job", [], SimpleNamespace(), None, kind="loudness", handlers=handlers)
    tracker.item_queue.extend([item("first"), item("remaining")])
    tracker.total_items = 2
    dispatcher._trackers["job"] = tracker
    dispatcher._assign_tasks()
    assert entered.wait(3)
    dispatcher.request_park("job")
    assert dispatcher.park_snapshot("job") is None
    finish.set()
    for worker in pool.workers:
        worker.current_thread.join(3)
    dispatcher._check_completions()
    dispatcher._assign_tasks()
    assert processed == ["/media/first"]
    snapshot = dispatcher.park_snapshot("job")
    assert snapshot["state"]["failed"] == 1
    assert snapshot["state"]["outcome_counts"] == {"done": 0, "failed": 1}
    assert [x["canonical_path"] for x in snapshot["items"]] == ["/media/remaining"]
    ref = write_checkpoint(tmp_path, "job", snapshot)
    assert dispatcher.detach_parked("job")
    saved = read_checkpoint(tmp_path, "job", ref)
    resumed = JobTracker(
        "job",
        checkpoint_items(saved),
        SimpleNamespace(),
        None,
        kind="loudness",
        handlers=handlers,
        carried_state=saved["state"],
    )
    assert resumed.total_items == 2 and resumed.completed == 1 and resumed.failed == 1
    assert not resumed.done_event.is_set()


@pytest.mark.parametrize("bad", [-1, "2", True])
def test_invalid_checkpoint_counters_are_not_restored(tmp_path, bad):
    snapshot = {"kind": "loudness", "items": [], "state": {"failed": bad}}
    with pytest.raises(ValueError):
        write_checkpoint(tmp_path, "job", snapshot)


def test_stale_policy_revision_cannot_undo_newer_saved_count():
    pool = WorkerPool(0, 0, [])
    pool.reconcile_groups([group("cpu", 3)], [], revision=5)
    current = list(pool.workers)
    pool.reconcile_groups([group("cpu")], [], revision=4)
    assert pool.workers == current
    assert pool.capacity_for("loudness")["open"] == 3
    pool.reconcile_groups([group("cpu", 2)], [], revision=6)
    assert len(pool.workers) == 2


def test_periodic_policy_refresh_preserves_current_hardware_selection():
    pool = WorkerPool(0, 0, [])
    groups = [group("gpu", resource="gpu")]
    pool.reconcile_groups(groups, [], revision=2)
    assert pool.capacity_for("previews")["reason"] == "hardware"
    pool.reconcile_groups(groups, GPU, revision=2)
    current = list(pool.workers)
    pool.reconcile_groups(groups, None, revision=2)
    assert pool.workers == current
    assert pool.capacity_for("previews")["open"] == 1


def test_preview_continuation_uses_only_unfinished_descriptors_and_exact_accounting(tmp_path, monkeypatch):
    from media_preview_generator.jobs.dispatcher import reset_dispatcher
    from media_preview_generator.jobs.orchestrator import run_processing
    from media_preview_generator.processing.generator import ProcessingResult, set_file_result_callback
    from media_preview_generator.web import settings_manager

    # Real settings, registry construction, dispatcher, checker and file-result
    # delivery. The saved owner was removed during downtime, so its remaining
    # file now settles through the actual no-owner path without external I/O.
    settings = settings_manager.SettingsManager(tmp_path)
    settings.update({"media_servers": [], "worker_groups": [group("cpu")]})
    monkeypatch.setattr(settings_manager, "_settings_manager", settings)
    outcomes = {r.value: 0 for r in ProcessingResult}
    outcomes["generated"] = 5001  # Files-panel history's cap is not recovery state.
    snapshot = {
        "kind": "previews",
        "context": {"warning": "Original scan warning"},
        "items": [
            {
                "canonical_path": "/media/remaining.mkv",
                "server_id": "removed",
                "item_id_by_server": {"removed": "7"},
                "title": "Remaining",
                "library_id": "1",
            }
        ],
        "state": {
            "successful": 5001,
            "failed": 0,
            "total_items": 5002,
            "outcome_counts": outcomes,
            "failed_paths": [],
            "publishers_aggregate": {},
            "cpu_fallback_files": 0,
        },
    }
    reference = write_checkpoint(tmp_path, "preview-resume", snapshot)
    continuation = read_checkpoint(tmp_path, "preview-resume", reference)
    config = SimpleNamespace(
        cpu_threads=1,
        gpu_threads=0,
        scan_workers=1,
        regenerate_thumbnails=True,
        working_tmp_folder=str(tmp_path / "work"),
        worker_groups=[group("cpu")],
        plex_url="",
        plex_token="",
        server_id_filter=None,
    )
    observed, progress = [], []
    reset_dispatcher()
    set_file_result_callback(lambda path, *args: observed.append(path), job_id="preview-resume")
    try:
        result = run_processing(
            config,
            [],
            job_id="preview-resume",
            continuation=continuation,
            progress_callback=lambda current, total, *args, **kwargs: progress.append((current, total)),
        )
        assert result["warning"] == "Original scan warning"
        assert result["outcome"]["generated"] == 5001
        assert sum(result["outcome"].values()) == 5002
        assert observed == ["/media/remaining.mkv"]
        assert progress[0] == (5001, 5002)
        assert progress[-1] == (5002, 5002)
    finally:
        set_file_result_callback(None, job_id="preview-resume")
        reset_dispatcher()


def test_assignment_does_not_read_external_pause_state_under_pool_lock():
    pool = WorkerPool(0, 0, [])
    pool.reconcile_groups([group("cpu")], [])
    dispatcher = JobDispatcher(pool)
    checks, processed = [], []

    def paused():
        assert not pool._workers_lock._is_owned()
        checks.append(True)
        return False

    def process(work, **kwargs):
        processed.append(work.canonical_path)
        return ItemOutcome("done", "ok")

    tracker = JobTracker(
        "job",
        [],
        SimpleNamespace(),
        None,
        callbacks={"pause_check": paused},
        kind="loudness",
        handlers=KindHandlers(lambda *a, **kw: None, process, ("done",)),
    )
    tracker.item_queue.append(item("a"))
    tracker.total_items = 1
    dispatcher._trackers["job"] = tracker
    dispatcher._assign_tasks()
    for worker in pool.workers:
        if worker.current_thread:
            worker.current_thread.join(3)
    dispatcher._check_completions()
    assert checks and processed == ["/media/a"]
    assert tracker.successful == 1 and tracker.done_event.is_set()


def test_grouped_pool_rejects_unpersisted_direct_scaling():
    pool = WorkerPool(0, 0, [])
    pool.reconcile_groups([group("cpu", 2)], [])
    current = list(pool.workers)
    for action in (pool.add_workers, pool.remove_workers):
        with pytest.raises(ValueError, match="saved count"):
            action("CPU", 1)
        assert pool.workers == current


@pytest.mark.parametrize("pin", [None, "plex-a", "plex-b"])
@pytest.mark.parametrize("existing_follow_up", [False, True])
def test_preview_continuation_preserves_follow_up_scope_with_two_owners(tmp_path, monkeypatch, pin, existing_follow_up):
    from media_preview_generator.jobs import orchestrator
    from media_preview_generator.jobs.checkpoints import item_descriptor
    from media_preview_generator.jobs.dispatcher import reset_dispatcher
    from media_preview_generator.loudness import job as loudness_job
    from media_preview_generator.markers import triggers
    from media_preview_generator.processing.generator import ProcessingResult
    from media_preview_generator.web import jobs, settings_manager

    settings = settings_manager.SettingsManager(tmp_path)
    root = str(tmp_path / "movies")
    servers = [
        {
            "id": sid,
            "name": sid,
            "type": "plex",
            "url": "http://plex.invalid",
            "output": {"plex_config_folder": str(tmp_path / sid)},
            "enabled": True,
            "loudness": {"enabled": True},
            "libraries": [{"id": "1", "name": "Movies", "kind": "movie", "remote_paths": [root]}],
        }
        for sid in ("plex-a", "plex-b")
    ]
    settings.update({"media_servers": servers, "worker_groups": [group("cpu")]})
    monkeypatch.setattr(settings_manager, "_settings_manager", settings)
    manager = jobs.JobManager(config_dir=str(tmp_path))
    monkeypatch.setattr(jobs, "_job_manager", manager)
    # Keep the real persisted follow-up queued; its separate execution is not
    # part of resuming a preview and must not contact either Plex server here.
    monkeypatch.setattr(loudness_job, "start_loudness_job_async", lambda *_: None)
    preview = manager.create_job(kind="previews", config={"source": "manual", "server_id": pin})
    work = ProcessableItem(root + "/remaining.mkv", "plex-a", {"plex-a": "7", "plex-b": "8"})
    assert {cfg.id for cfg in triggers._server_configs()} == {"plex-a", "plex-b"}
    if existing_follow_up:
        orchestrator._queue_loudness_follow_up(preview.id, [work], pin)
    previous_ids = {entry.id for entry in manager.get_all_jobs() if entry.kind == "loudness"}
    state = {
        "successful": 0,
        "failed": 0,
        "total_items": 1,
        "outcome_counts": {r.value: 0 for r in ProcessingResult},
        "failed_paths": [],
        "publishers_aggregate": {},
        "cpu_fallback_files": 0,
    }
    ref = write_checkpoint(tmp_path, preview.id, {"kind": "previews", "items": [item_descriptor(work)], "state": state})
    config = SimpleNamespace(
        cpu_threads=1,
        gpu_threads=0,
        scan_workers=1,
        regenerate_thumbnails=True,
        working_tmp_folder=str(tmp_path / "work"),
        worker_groups=[group("cpu")],
        server_id_filter=pin,
        plex_url="",
        plex_token="",
    )
    reset_dispatcher()
    try:
        result = orchestrator.run_processing(
            config, [], job_id=preview.id, continuation=read_checkpoint(tmp_path, preview.id, ref)
        )
        queued = [entry for entry in manager.get_all_jobs() if entry.kind == "loudness"]
        assert len(queued) == 1
        assert queued[0].config.get("server_id") == pin
        assert queued[0].config["file_paths"] == [work.canonical_path]
        assert queued[0].config["follows_job_ids"] == [preview.id]
        if existing_follow_up:
            assert {queued[0].id} == previous_ids
        assert result["outcome"]["skipped_file_not_found"] == 1
    finally:
        reset_dispatcher()


def member_group(gid="g", *, gpu=3, cpu=2, enabled=True, availability=None):
    members = []
    if gpu:
        members.append({"id": "gpu1", "resource": "gpu", "device": "cuda:0", "count": gpu, "job_types": ["previews"]})
    if cpu:
        members.append({"id": "cpu1", "resource": "cpu", "device": None, "count": cpu, "job_types": ["loudness"]})
    return {
        "id": gid,
        "name": gid,
        "enabled": enabled,
        "availability": availability or {"mode": "always", "windows": []},
        "members": members,
    }


def test_members_own_their_slots_and_job_types():
    pool = WorkerPool(0, 0, GPU)
    pool.reconcile_groups([member_group()], GPU)
    owners = {(w.group_id, w.member_id, w.policy_id, w.allowed_job_types) for w in pool.workers}
    assert owners == {
        ("g", "gpu1", "g:gpu1", frozenset(["previews"])),
        ("g", "cpu1", "g:cpu1", frozenset(["loudness"])),
    }
    assert sum(w.member_id == "gpu1" for w in pool.workers) == 3
    assert sum(w.member_id == "cpu1" for w in pool.workers) == 2
    assert pool.capacity_for("loudness")["open"] == 2
    assert pool.capacity_for("previews")["open"] == 3


def test_member_reduction_retires_idle_and_keeps_busy_until_finished():
    pool = WorkerPool(0, 0, GPU)
    pool.reconcile_groups([member_group()], GPU)
    busy = next(w for w in pool.workers if w.member_id == "cpu1")
    busy.is_busy = True
    summary = pool.reconcile_groups([member_group(cpu=1)], GPU)
    assert summary["removed"] == 1 and not busy._pending_removal
    assert sum(w.member_id == "cpu1" for w in pool.workers) == 1
    pool.reconcile_groups([member_group(cpu=0)], GPU)
    assert busy in pool.workers and busy._pending_removal
    assert sum(w.member_id == "gpu1" and not w._pending_removal for w in pool.workers) == 3


def test_removed_member_with_busy_worker_finishes_under_its_owner():
    pool = WorkerPool(0, 0, GPU)
    pool.reconcile_groups([member_group()], GPU)
    busy = next(w for w in pool.workers if w.member_id == "gpu1")
    busy.is_busy = True
    pool.reconcile_groups([member_group(gpu=0)], GPU)
    assert busy in pool.workers and busy._pending_removal
    assert (busy.group_id, busy.member_id) == ("g", "gpu1")
    assert sum(w.member_id == "gpu1" for w in pool.workers) == 1
    row = next(r for r in pool.member_snapshots() if r["member_id"] == "gpu1")
    assert (row["group_id"], row["state"], row["finishing"], row["count"]) == ("g", "draining", 1, 0)
    assert pool.capacity_for("previews")["open"] == 0


def test_unchanged_member_groups_retire_nothing():
    pool = WorkerPool(0, 0, GPU)
    pool.reconcile_groups([member_group()], GPU)
    before = list(pool.workers)
    summary = pool.reconcile_groups([member_group()], GPU)
    assert (summary["added"], summary["removed"], summary["retiring"]) == (0, 0, 0)
    assert pool.workers == before


def test_missing_gpu_zeroes_only_that_member_and_closed_group_zeroes_all():
    pool = WorkerPool(0, 0, [])
    pool.reconcile_groups([member_group()], [])
    assert {w.member_id for w in pool.workers} == {"cpu1"}
    rows = {r["member_id"]: r for r in pool.member_snapshots()}
    assert rows["gpu1"]["state"] == "hardware_unavailable" and rows["gpu1"]["target"] == 0
    assert rows["cpu1"]["state"] == "active" and rows["cpu1"]["target"] == 2
    assert rows["cpu1"]["group_id"] == "g" and rows["cpu1"]["id"] == "g:cpu1"
    pool.reconcile_groups([member_group(enabled=False)], [])
    assert pool.capacity_for("loudness")["open"] == 0


def test_two_groups_share_one_gpu_budget_during_handover():
    pool = WorkerPool(0, 0, GPU)
    pool.reconcile_groups([member_group("a", gpu=2, cpu=0)], GPU)
    busy = list(pool.workers)
    for worker in busy:
        worker.is_busy = True
    pool.reconcile_groups([member_group("b", gpu=2, cpu=0)], GPU)
    assert all(w._pending_removal for w in busy)
    assert pool._find_available_worker(kind="previews", claim=True) is None
