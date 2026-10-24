"""Durable loudness resume must retain verified results without trusting UI history."""

import json
import sqlite3
import threading
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from media_preview_generator.job_kinds import ItemOutcome
from media_preview_generator.jobs.checkpoints import item_descriptor, write_checkpoint
from media_preview_generator.loudness import job, resume
from media_preview_generator.loudness.resume import CompletionLedger, source_fingerprint
from media_preview_generator.processing.types import ProcessableItem
from media_preview_generator.web.jobs import JobStatus
from tests.conftest import CTIME_ONLY_CHANGES, SOURCE_CHANGES, change_ctime_only, change_source

from .conftest import Lifecycle


def outcome(key="loudness_written"):
    return ItemOutcome(
        key,
        "Verified native audio",
        [
            {
                "server_id": "plex",
                "server_name": "Plex",
                "server_type": "plex",
                "status": key,
            }
        ],
    )


@pytest.fixture
def selected(tmp_path):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"original audio source")
    return ProcessableItem(str(source), "plex", {"plex": "17"})


def restore(ledger, selected, check=None):
    return ledger.restore(
        [selected], check=check or MagicMock(return_value=outcome("loudness_up_to_date")), cancel_check=lambda: False
    )


@pytest.mark.parametrize("key", ["loudness_written", "loudness_up_to_date"])
def test_committed_result_survives_reopen_and_keeps_original_provenance(tmp_path, selected, key):
    first = CompletionLedger(tmp_path, "resume")
    first.record(selected, outcome(key), source_fingerprint(selected))
    first.close()
    reopened = CompletionLedger(tmp_path, "resume")
    try:
        check = MagicMock(return_value=outcome("loudness_up_to_date"))
        remaining, state = restore(reopened, selected, check)
        assert remaining == []
        assert state["successful"] == 1 and state["outcome_counts"] == {key: 1}
        assert state["publishers_aggregate"]["plex"]["counts"] == {key: 1}
        check.assert_called_once_with(selected)
    finally:
        reopened.close()


@pytest.mark.parametrize("change", ["replacement", "native_cleared", "server_pin", "item_identity", "during_check"])
def test_saved_result_requires_current_source_and_native_readiness(tmp_path, selected, change):
    ledger = CompletionLedger(tmp_path, "resume")
    ledger.record(selected, outcome(), source_fingerprint(selected))
    checked = selected
    check = MagicMock(return_value=outcome("loudness_up_to_date"))
    if change == "replacement":
        Path(selected.canonical_path).write_bytes(b"replacement release")
    elif change == "native_cleared":
        check.return_value = None
    elif change == "server_pin":
        checked = replace(selected, server_id="other")
    elif change == "item_identity":
        checked = replace(selected, item_id_by_server={"plex": "18"})
    else:

        def changed(_item):
            Path(selected.canonical_path).write_bytes(b"changed during native verification")
            return outcome("loudness_up_to_date")

        check.side_effect = changed
    try:
        remaining, state = restore(ledger, checked, check)
        assert remaining == [checked]
        assert state["successful"] == 0 and state["outcome_counts"] == {}
    finally:
        ledger.close()


@pytest.mark.parametrize("how", CTIME_ONLY_CHANGES)
@pytest.mark.parametrize("when", ["before_resume", "during_analysis", "during_check"])
def test_saved_result_survives_a_metadata_only_source_change(tmp_path, selected, how, when):
    """A hard link, chmod or *arr re-import moves the change time, not the audio that was analysed."""
    ledger = CompletionLedger(tmp_path, "resume")
    before = source_fingerprint(selected)
    if when == "during_analysis":
        change_ctime_only(selected.canonical_path, how)
    ledger.record(selected, outcome(), before)
    check = MagicMock(return_value=outcome("loudness_up_to_date"))
    if when == "before_resume":
        change_ctime_only(selected.canonical_path, how)
    elif when == "during_check":

        def relinked(_item):
            change_ctime_only(selected.canonical_path, how)
            return outcome("loudness_up_to_date")

        check.side_effect = relinked
    try:
        remaining, state = restore(ledger, selected, check)
        assert remaining == []
        assert state["successful"] == 1
    finally:
        ledger.close()


@pytest.mark.parametrize("how", SOURCE_CHANGES)
def test_saved_result_is_not_reused_after_a_real_source_change(tmp_path, selected, how):
    ledger = CompletionLedger(tmp_path, "resume")
    ledger.record(selected, outcome(), source_fingerprint(selected))
    change_source(selected.canonical_path, how)
    try:
        remaining, state = restore(ledger, selected)
        assert remaining == [selected]
        assert state["successful"] == 0
    finally:
        ledger.close()


@pytest.mark.parametrize("damage", ["json", "version", "servers", "status", "server_type", "missing_status"])
def test_malformed_private_row_fails_closed_without_credit(tmp_path, selected, damage):
    ledger = CompletionLedger(tmp_path, "resume")
    ledger.record(selected, outcome(), source_fingerprint(selected))
    payload = json.loads(ledger._conn.execute("SELECT payload FROM completions").fetchone()[0])
    if damage == "version":
        payload["version"] = 99
    elif damage == "servers":
        payload["servers"] = [None]
    elif damage == "status":
        payload["servers"][0]["status"] = []
    elif damage == "server_type":
        payload["servers"][0]["server_type"] = 42
    elif damage == "missing_status":
        payload["servers"][0].pop("status")
    encoded = "{" if damage == "json" else json.dumps(payload)
    with ledger._conn:
        ledger._conn.execute("UPDATE completions SET payload=?", (encoded,))
    try:
        remaining, state = restore(ledger, selected)
        assert remaining == [selected]
        assert state["successful"] == 0 and state["publishers_aggregate"] == {}
    finally:
        ledger.close()


def test_failed_sqlite_commit_does_not_create_a_resume_success(tmp_path, selected):
    ledger = CompletionLedger(tmp_path, "resume")
    ledger._conn.execute(
        "CREATE TRIGGER fail_write BEFORE INSERT ON completions BEGIN SELECT RAISE(ABORT, 'write failed'); END"
    )
    with pytest.raises(RuntimeError, match="Could not save loudness completion"):
        ledger.record(selected, outcome(), source_fingerprint(selected))
    assert ledger.error
    assert ledger._conn.execute("SELECT COUNT(*) FROM completions").fetchone()[0] == 0
    ledger.close()
    reopened = CompletionLedger(tmp_path, "resume")
    try:
        assert restore(reopened, selected)[0] == [selected]
    finally:
        reopened.close()


def test_restarted_job_restores_written_counts_without_relying_on_display_history(
    crash_keeps_ledger, lifecycle: Lifecycle
):
    lifecycle.api_ready = True
    first = lifecycle.add_file("finished.mkv")
    second = lifecycle.add_file("remaining.mkv")
    lifecycle.corrupt.add((second, 1))
    parent = lifecycle.start([first, second])
    lifecycle.corrupt.clear()
    # Simulate capped/removed display history; it is not the completion ledger.
    Path(lifecycle.manager._file_results_path(parent.id)).unlink()
    parent.status = JobStatus.PENDING
    parent.completed_at = None

    job.run_loudness_job(parent.id)

    assert parent.status is JobStatus.COMPLETED
    assert {key: count for key, count in parent.progress.outcome.items() if count} == {job.WRITTEN: 2}
    assert parent.publishers[0]["counts"] == {job.WRITTEN: 2}
    assert lifecycle.analyses == [(first, 1), (second, 1), (second, 1)]


def test_partial_stream_failure_resumes_only_missing_stream(lifecycle: Lifecycle):
    lifecycle.api_ready = True
    path = lifecycle.add_file("partial.mkv", tracks=2)
    lifecycle.corrupt.add((path, 2))
    parent = lifecycle.start([path])
    assert lifecycle.rows(parent)[path]["outcome"] == job.FAILED
    assert {key: count for key, count in parent.progress.outcome.items() if count} == {job.FAILED: 1}
    assert parent.status is JobStatus.FAILED
    lifecycle.corrupt.clear()
    parent.status = JobStatus.PENDING
    parent.completed_at = None

    job.run_loudness_job(parent.id)

    assert parent.status is JobStatus.COMPLETED
    assert {key: count for key, count in parent.progress.outcome.items() if count} == {job.WRITTEN: 1}
    # The first batch touches both streams, then ordinary batch failure retries each individually; resume retries only
    # the stream whose separate analysis failed.
    assert lifecycle.analyses == [(path, 1), (path, 2), (path, 1), (path, 2), (path, 2)]


def test_all_restored_finishes_without_creating_an_empty_dispatcher_tracker(
    crash_keeps_ledger, lifecycle: Lifecycle, monkeypatch
):
    lifecycle.api_ready = True
    path = lifecycle.add_file("settled.mkv")
    parent = lifecycle.start([path])
    parent.status = JobStatus.PENDING
    parent.completed_at = None
    submit = MagicMock(side_effect=AssertionError("Empty resumed job must finish without a tracker"))
    monkeypatch.setattr(lifecycle.dispatcher, "submit_items", submit)

    job.run_loudness_job(parent.id)

    assert parent.status is JobStatus.COMPLETED
    assert {key: count for key, count in parent.progress.outcome.items() if count} == {job.WRITTEN: 1}
    assert lifecycle.analyses == [(path, 1)]
    submit.assert_not_called()


def test_failed_ledger_write_fails_the_job_before_success_is_credited(lifecycle: Lifecycle, monkeypatch):
    lifecycle.api_ready = True
    path = lifecycle.add_file("save-failed.mkv")

    def disk_failure(self, item, result, before):
        self.error = "Could not save loudness completion (OperationalError)"
        raise RuntimeError(self.error)

    monkeypatch.setattr(CompletionLedger, "record", disk_failure)
    parent = lifecycle.start([path])

    assert parent.status is JobStatus.FAILED
    assert "Could not save loudness completion" in parent.error
    assert not parent.progress.outcome.get(job.WRITTEN)
    assert lifecycle.analyses == [(path, 1)]


def test_cleared_native_metadata_reanalyzes_saved_file(lifecycle: Lifecycle):
    lifecycle.api_ready = True
    path = lifecycle.add_file("cleared.mkv")
    parent = lifecycle.start([path])
    with sqlite3.connect(lifecycle.database) as conn:
        conn.execute("UPDATE media_streams SET extra_data=NULL")
    parent.status = JobStatus.PENDING
    parent.completed_at = None

    job.run_loudness_job(parent.id)

    assert parent.status is JobStatus.COMPLETED
    assert {key: count for key, count in parent.progress.outcome.items() if count} == {job.WRITTEN: 1}
    assert lifecycle.analyses == [(path, 1), (path, 1)]


def test_handler_commits_before_returning_an_outcome(tmp_path, selected, monkeypatch):
    ledger = CompletionLedger(tmp_path, "ordering")
    result = outcome()
    monkeypatch.setattr(job, "check_item", lambda item, **kwargs: result)
    handlers = job.kind_handlers(None, completion_ledger=ledger)
    try:
        assert handlers.check_fn(selected) is result
        # Reopen before any dispatcher callback/credit exists.
        observer = CompletionLedger(tmp_path, "ordering")
        try:
            assert restore(observer, selected)[1]["outcome_counts"] == {job.WRITTEN: 1}
        finally:
            observer.close()
    finally:
        ledger.close()


@pytest.mark.parametrize("legacy", [False, True], ids=["immutable-baseline", "legacy-recount"])
def test_retry_restores_committed_result_when_crash_preceded_files_callback(
    crash_keeps_ledger, lifecycle: Lifecycle, legacy
):
    path = lifecycle.add_file("retry-crash.mkv")
    parent = lifecycle.start([path])
    (child,) = lifecycle.children(parent)
    assert parent.progress.outcome[job.WAITING] == 1
    assert lifecycle.rows(child) == {}
    if legacy:
        child.config.pop("retry_baseline")
    lifecycle.api_ready = True
    item = ProcessableItem(path, "plex")
    # Native data and the private commit survived; Files/UI accounting did not.
    ledger = CompletionLedger(lifecycle.manager.config_dir, child.id)
    ledger.save_selection([item], [], {})
    ledger.record(item, outcome(), source_fingerprint(item))
    ledger.close()

    lifecycle.retry(child)

    assert parent.status is JobStatus.COMPLETED
    assert parent.progress.outcome == {job.WRITTEN: 1}
    assert parent.publishers[0]["counts"] == {job.WRITTEN: 1}
    assert lifecycle.rows(child)[path]["outcome"] == job.WRITTEN
    assert lifecycle.rows(parent)[path]["outcome"] == job.WRITTEN
    history = {entry.id: Path(lifecycle.manager._file_results_path(entry.id)).read_bytes() for entry in (parent, child)}
    assert lifecycle.analyses == [(path, 1)]
    # Replaying completion cannot increment the immutable chain baseline twice.
    child.status = JobStatus.PENDING
    child.completed_at = None
    lifecycle.retry(child)
    assert parent.progress.outcome == {job.WRITTEN: 1}
    for entry in (parent, child):
        assert Path(lifecycle.manager._file_results_path(entry.id)).read_bytes() == history[entry.id]


@pytest.mark.parametrize("change", ["native_cleared", "replacement"])
def test_parked_resume_revalidates_completed_files_outside_checkpoint(lifecycle: Lifecycle, change):
    lifecycle.api_ready = True
    first = lifecycle.add_file("parked-finished.mkv")
    second = lifecycle.add_file("parked-remaining.mkv")
    lifecycle.corrupt.add((second, 1))
    parent = lifecycle.start([first, second])
    lifecycle.corrupt.clear()
    snapshot = {
        "items": [item_descriptor(ProcessableItem(second, "plex"))],
        "state": {"successful": 1, "failed": 0, "total_items": 2, "outcome_counts": {job.WRITTEN: 1}},
    }
    reference = write_checkpoint(lifecycle.manager.config_dir, parent.id, snapshot)
    lifecycle.manager.merge_job_config(parent.id, {"parked_checkpoint": reference})
    if change == "native_cleared":
        with sqlite3.connect(lifecycle.database) as conn:
            conn.execute("UPDATE media_streams SET extra_data=NULL WHERE media_part_id=1")
    else:
        Path(first).write_bytes(b"replacement source not yet indexed by Plex")
    parent.status = JobStatus.PENDING
    parent.completed_at = None

    job.run_loudness_job(parent.id)

    if change == "native_cleared":
        assert parent.status is JobStatus.COMPLETED
        assert parent.progress.outcome[job.WRITTEN] == 2
        assert lifecycle.analyses.count((first, 1)) == 2
    else:
        assert parent.status is JobStatus.PENDING
        assert parent.progress.outcome[job.WAITING] == 1
        assert parent.progress.outcome[job.WRITTEN] == 1
        assert lifecycle.analyses.count((first, 1)) == 1
        (child,) = lifecycle.children(parent)
        assert child.config["file_paths"] == [first]


@pytest.mark.parametrize("finish", ["resume", "cancel"])
def test_global_pause_between_native_resume_checks_waits_for_resume_or_cancel(
    crash_keeps_ledger,
    lifecycle: Lifecycle,
    monkeypatch,
    finish,
):
    monkeypatch.setattr(resume, "RESTORE_CHECK_THREADS", 1)  # one at a time: the pause is seen between two checks
    lifecycle.api_ready = True
    paths = [lifecycle.add_file("pause-first.mkv"), lifecycle.add_file("pause-second.mkv")]
    parent = lifecycle.start(paths)
    parent.status = JobStatus.PENDING
    parent.completed_at = None
    settings = job.get_settings_manager()
    gate = job.get_job_gate()
    checked = []
    paused = threading.Event()
    original = job.check_item

    def check(item, **kwargs):
        result = original(item, **kwargs)
        checked.append(item.canonical_path)
        if len(checked) == 1:
            settings.processing_paused = True
            paused.set()
        return result

    monkeypatch.setattr(job, "check_item", check)
    runner = threading.Thread(target=job.run_loudness_job, args=(parent.id,))
    runner.start()
    try:
        assert paused.wait(2)
        deadline = time.monotonic() + 2
        while gate.snapshot()[0] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert gate.snapshot()[0] == 0, "a validation wait must not hold a start-up slot"
        assert checked == paths[:1], "no file is checked while processing is paused"
        if finish == "cancel":
            lifecycle.manager.request_cancellation(parent.id)
        else:
            settings.processing_paused = False
        runner.join(3)
        assert not runner.is_alive()
        if finish == "resume":
            assert checked == paths
            assert parent.status is JobStatus.COMPLETED
            assert parent.progress.outcome == {job.WRITTEN: 2}
        else:
            assert checked == paths[:1]
            assert parent.status is JobStatus.CANCELLED
        assert gate.snapshot()[0] == 0
    finally:
        settings.processing_paused = False
        lifecycle.manager.request_cancellation(parent.id)
        runner.join(3)


def test_saved_files_are_rechecked_side_by_side_and_judged_in_order(tmp_path):
    ledger = CompletionLedger(tmp_path, "resume")
    items = []
    for n in range(20):
        source = tmp_path / f"source{n}.mkv"
        source.write_bytes(b"audio %d" % n)
        item = ProcessableItem(str(source), "plex", {"plex": str(n)})
        ledger.record(item, outcome(), source_fingerprint(item))
        items.append(item)
    running, peak, lock = 0, 0, threading.Lock()
    release = threading.Event()

    def check(item):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
            if running > 1:
                release.set()
        release.wait(2)
        with lock:
            running -= 1
        return outcome("loudness_up_to_date") if item is not items[3] else None

    try:
        remaining, state = ledger.restore(items, check=check, cancel_check=lambda: False)
    finally:
        ledger.close()

    assert peak > 1
    assert remaining == [items[3]]
    assert state["successful"] == 19


def _many_saved(tmp_path, count=200):
    ledger = CompletionLedger(tmp_path, "resume")
    items = []
    for n in range(count):
        source = tmp_path / f"source{n}.mkv"
        source.write_bytes(b"audio %d" % n)
        item = ProcessableItem(str(source), "plex", {"plex": str(n)})
        ledger.record(item, outcome(), source_fingerprint(item))
        items.append(item)
    return ledger, items


def test_cancelling_a_resume_stops_within_one_check_per_thread(tmp_path):
    ledger, items = _many_saved(tmp_path)
    cancelled = threading.Event()
    started, lock = [], threading.Lock()

    def check(item):
        with lock:
            started.append(item)
            if len(started) >= resume.RESTORE_CHECK_THREADS:
                cancelled.set()
        time.sleep(0.05)
        return outcome("loudness_up_to_date")

    try:
        with pytest.raises(InterruptedError):
            ledger.restore(items, check=check, cancel_check=cancelled.is_set)
    finally:
        ledger.close()

    assert len(started) <= 2 * resume.RESTORE_CHECK_THREADS


@pytest.mark.parametrize("finish", ["resume", "cancel"])
def test_eight_threads_stop_at_a_cancel_and_wait_out_a_pause(
    crash_keeps_ledger, lifecycle: Lifecycle, monkeypatch, finish
):
    threads = resume.RESTORE_CHECK_THREADS
    lifecycle.api_ready = True
    paths = [lifecycle.add_file(f"many-{n}.mkv") for n in range(3 * threads)]
    parent = lifecycle.start(paths)
    parent.status = JobStatus.PENDING
    parent.completed_at = None
    settings = job.get_settings_manager()
    in_flight, checked, guard = threading.Event(), [], threading.Lock()
    hold = threading.Event()
    original = job.check_item

    def check(item, **kwargs):
        with guard:
            checked.append(item.canonical_path)
            if len(checked) == threads:
                in_flight.set()
        if finish == "cancel":
            hold.wait(3)  # every thread is inside a check when the cancel arrives
        elif len(checked) == 1:
            settings.processing_paused = True  # seen by every check not yet started
        return original(item, **kwargs)

    monkeypatch.setattr(job, "check_item", check)
    runner = threading.Thread(target=job.run_loudness_job, args=(parent.id,))
    runner.start()
    try:
        if finish == "cancel":
            assert in_flight.wait(3)
            lifecycle.manager.request_cancellation(parent.id)
            hold.set()
        else:
            deadline = time.monotonic() + 3
            while not checked and time.monotonic() < deadline:
                time.sleep(0.01)
            time.sleep(0.3)
            assert 1 <= len(checked) < len(paths), "no new check starts while processing is paused"
            assert runner.is_alive()
            settings.processing_paused = False
        runner.join(5)
        assert not runner.is_alive()
        if finish == "cancel":
            assert len(checked) == threads, "only the checks already running finish"
            assert parent.status is JobStatus.CANCELLED
        else:
            assert sorted(checked) == sorted(paths)
            assert parent.status is JobStatus.COMPLETED
    finally:
        settings.processing_paused = False
        hold.set()
        lifecycle.manager.request_cancellation(parent.id)
        runner.join(3)


def _make_ledger(config_dir, job_id, age_s=3600):
    ledger = CompletionLedger(config_dir, job_id)
    ledger.close()
    path = resume._path(config_dir, job_id)
    old = time.time() - age_s
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{path}{suffix}")
        if candidate.exists():
            import os

            os.utime(candidate, (old, old))
    return path


def test_sweep_removes_finished_ledgers_but_keeps_needed_and_recent_ones(tmp_path):
    finished = _make_ledger(tmp_path, "finished")
    running = _make_ledger(tmp_path, "running")
    just_written = _make_ledger(tmp_path, "settling", age_s=0)

    removed = resume.delete_unneeded_ledgers(tmp_path, {"running"})

    assert removed == 1
    assert not finished.exists()
    assert running.exists()
    assert just_written.exists()


def test_sweep_without_a_ledger_folder_is_a_no_op(tmp_path):
    assert resume.delete_unneeded_ledgers(tmp_path, set()) == 0


def test_retention_sweep_keeps_running_and_pending_jobs(tmp_path):
    from media_preview_generator.web.jobs import JobManager

    manager = JobManager(str(tmp_path))
    try:
        running = manager.create_job(kind="loudness", config={"kind": "loudness"})
        manager.start_job(running.id)
        pending = manager.create_job(kind="loudness", config={"kind": "loudness"})
        done = manager.create_job(kind="loudness", config={"kind": "loudness"})
        manager.complete_job(done.id)
        paths = {name: _make_ledger(tmp_path, name) for name in (running.id, pending.id, done.id, "orphan")}

        with manager._lock:
            manager._enforce_log_retention()

        assert paths[running.id].exists() and paths[pending.id].exists()
        assert not paths[done.id].exists() and not paths["orphan"].exists()
    finally:
        manager.close()
