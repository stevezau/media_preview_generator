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
from media_preview_generator.loudness import job
from media_preview_generator.loudness.resume import CompletionLedger, source_fingerprint
from media_preview_generator.processing.types import ProcessableItem
from media_preview_generator.web.jobs import JobStatus

from .test_job_lifecycle import Lifecycle, lifecycle  # noqa: F401


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


def test_restarted_job_restores_written_counts_without_relying_on_display_history(lifecycle: Lifecycle):  # noqa: F811
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


def test_partial_stream_failure_resumes_only_missing_stream(lifecycle: Lifecycle):  # noqa: F811
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


def test_all_restored_finishes_without_creating_an_empty_dispatcher_tracker(lifecycle: Lifecycle, monkeypatch):  # noqa: F811
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


def test_failed_ledger_write_fails_the_job_before_success_is_credited(lifecycle: Lifecycle, monkeypatch):  # noqa: F811
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


def test_cleared_native_metadata_reanalyzes_saved_file(lifecycle: Lifecycle):  # noqa: F811
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
def test_retry_restores_committed_result_when_crash_preceded_files_callback(lifecycle: Lifecycle, legacy):  # noqa: F811
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
def test_parked_resume_revalidates_completed_files_outside_checkpoint(lifecycle: Lifecycle, change):  # noqa: F811
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
    lifecycle: Lifecycle,  # noqa: F811
    monkeypatch,
    finish,
):
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
