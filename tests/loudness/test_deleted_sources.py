"""Only explicit deletion evidence retires an absent source from loudness retries."""

# ruff: noqa: F811 - imported fixture
import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest

from media_preview_generator.loudness import job
from media_preview_generator.web.jobs import JobStatus

from .test_job_lifecycle import Lifecycle, lifecycle  # noqa: F401


def start_sender(env, path):
    parent = env.manager.create_job(
        kind="loudness", config={"source": "sonarr", "file_paths": [path], "server_id": "plex"}
    )
    job.run_loudness_job(parent.id)
    return parent


def event_time(env, entry, timestamp):
    """Seed immutable event time independently of display-time mutations."""
    entry.created_at = timestamp
    entry.started_at = timestamp
    with env.manager._storage._lock:
        env.manager._storage._conn.execute("UPDATE jobs SET created_at = ? WHERE id = ?", (timestamp, entry.id))


def deletion(env, paths, *, source="sonarr", server_id="plex"):
    return env.manager.create_job(
        kind="previews",
        config={"source": source, "server_id": server_id, "webhook_deleted_paths": paths},
    )


@pytest.mark.parametrize("mapped", [False, True])
def test_explicit_deleted_source_settles_existing_retry_without_analysis(lifecycle: Lifecycle, mapped):
    path = lifecycle.add_file("old-release.mkv")
    Path(path).unlink()
    parent = start_sender(lifecycle, path)
    (child,) = lifecycle.children(parent)
    cfg = job._build_multi_server_registry(None).get_config("plex")
    cfg.path_mappings = [
        {"remote_prefix": "/plex", "local_prefix": str(lifecycle.media), "webhook_prefixes": ["/sender"]}
    ]
    deletion(lifecycle, ["/sender/old-release.mkv" if mapped else path])

    lifecycle.retry(child)

    assert parent.status is JobStatus.COMPLETED
    assert parent.error is None
    assert parent.progress.outcome == {"skipped_source_gone": 1}
    row = lifecycle.rows(parent)[path]
    assert row["outcome"] == "skipped_source_gone"
    assert "deleted" in row["reason"].lower()
    assert len(lifecycle.children(parent)) == 1
    assert lifecycle.analyses == []


@pytest.mark.parametrize("evidence", ["none", "wrong-server", "other-path", "untrusted-source"])
def test_unknown_missing_sources_keep_their_failure_and_retry(lifecycle: Lifecycle, evidence):
    path = lifecycle.add_file("missing.mkv")
    Path(path).unlink()
    if evidence == "wrong-server":
        deletion(lifecycle, [path], server_id="another-plex")
    elif evidence == "other-path":
        deletion(lifecycle, [str(lifecycle.media / "other.mkv")])
    elif evidence == "untrusted-source":
        deletion(lifecycle, [path], source="manual")
    parent = start_sender(lifecycle, path)
    assert parent.status is JobStatus.PENDING
    assert parent.progress.outcome[job.FILE_NOT_FOUND] == 1
    assert len(lifecycle.children(parent)) == 1


def test_recreated_file_is_processed_despite_old_deletion_record(lifecycle: Lifecycle):
    lifecycle.api_ready = True
    path = lifecycle.add_file("recreated.mkv")
    deletion(lifecycle, [path])
    parent = start_sender(lifecycle, path)
    assert parent.status is JobStatus.COMPLETED
    assert parent.progress.outcome[job.WRITTEN] == 1
    assert lifecycle.analyses == [(path, 1)]


def test_deleted_skip_preserves_success_and_unknown_missing_failure_in_chain(lifecycle: Lifecycle):
    lifecycle.api_ready = True
    written = lifecycle.add_file("current.mkv")
    deleted = lifecycle.add_file("deleted.mkv")
    missing = lifecycle.add_file("unknown.mkv")
    Path(deleted).unlink()
    Path(missing).unlink()
    deletion(lifecycle, [deleted])
    parent = lifecycle.manager.create_job(
        kind="loudness", config={"source": "sonarr", "file_paths": [written, deleted, missing], "server_id": "plex"}
    )
    job.run_loudness_job(parent.id)
    (child,) = lifecycle.children(parent)
    assert child.config["file_paths"] == [missing]
    lifecycle.retry(child)
    assert parent.progress.outcome == {job.WRITTEN: 1, job.SOURCE_GONE: 1, job.FILE_NOT_FOUND: 1}
    assert parent.error and "1 file(s) failed or missing" in parent.error
    assert lifecycle.rows(parent)[deleted]["outcome"] == job.SOURCE_GONE
    assert lifecycle.analyses == [(written, 1)]


@pytest.mark.parametrize("notice", ["same-event", "newer-import", "equal-import", "unknown-import", "batch-overlap"])
@pytest.mark.parametrize("mapped", [False, True])
def test_reimported_absent_path_keeps_retry_instead_of_using_stale_deletion(lifecycle, notice, mapped):
    path = lifecycle.add_file("reused-name.mkv")
    Path(path).unlink()
    cfg = job._build_multi_server_registry(None).get_config("plex")
    cfg.path_mappings = [
        {"remote_prefix": "/plex", "local_prefix": str(lifecycle.media), "webhook_prefixes": ["/sender"]}
    ]
    old = deletion(lifecycle, ["/sender/reused-name.mkv" if mapped else path])
    event_time(lifecycle, old, "2026-10-05T01:00:00+00:00")
    if notice == "same-event":
        old.config["webhook_paths"] = [path]
    else:
        incoming = lifecycle.manager.create_job(
            kind="previews", config={"source": "sonarr", "server_id": "plex", "webhook_paths": [path]}
        )
        event_time(
            lifecycle,
            incoming,
            {
                "newer-import": "2026-10-05T02:00:00+00:00",
                "equal-import": old.created_at,
                "unknown-import": "unknown",
                "batch-overlap": "2026-10-05T00:00:00+00:00",
            }[notice],
        )
        if notice == "batch-overlap":
            incoming.config["webhook_fire_at"] = "2026-10-05T02:00:00+00:00"
    parent = start_sender(lifecycle, path)
    assert parent.status is JobStatus.PENDING
    assert parent.progress.outcome[job.FILE_NOT_FOUND] == 1
    assert len(lifecycle.children(parent)) == 1


@pytest.mark.parametrize("timestamp", [None, "not-a-date"])
def test_unknown_deletion_order_cannot_retire_a_missing_source(lifecycle, timestamp):
    path = lifecycle.add_file("missing.mkv")
    Path(path).unlink()
    old = deletion(lifecycle, [path])
    old.created_at = timestamp
    # JobManager sorts by created_at; direct helper probe isolates legacy invalid evidence.
    from media_preview_generator.loudness.deleted_sources import confirmed_deleted_paths

    registry = job._build_multi_server_registry(None)
    assert confirmed_deleted_paths([old], registry, "plex", event_times={old.id: timestamp}) == frozenset()


@pytest.mark.parametrize("other_server", [False, True])
def test_newer_deletion_still_retires_old_import_without_cross_server_interference(lifecycle, other_server):
    path = lifecycle.add_file("old-import.mkv")
    Path(path).unlink()
    incoming = lifecycle.manager.create_job(
        kind="previews",
        config={"source": "sonarr", "server_id": "other" if other_server else "plex", "webhook_paths": [path]},
    )
    event_time(lifecycle, incoming, "2026-10-05T03:00:00+00:00" if other_server else "2026-10-05T00:00:00+00:00")
    old = deletion(lifecycle, [path])
    event_time(lifecycle, old, "2026-10-05T01:00:00+00:00")
    parent = start_sender(lifecycle, path)
    assert parent.status is JobStatus.COMPLETED
    assert parent.progress.outcome[job.SOURCE_GONE] == 1
    assert lifecycle.children(parent) == []


def test_preview_retry_creation_time_is_not_a_new_import(lifecycle):
    path = lifecycle.add_file("old-retried-import.mkv")
    Path(path).unlink()
    original = lifecycle.manager.create_job(
        kind="previews", config={"source": "sonarr", "server_id": "plex", "webhook_paths": [path]}
    )
    event_time(lifecycle, original, "2026-10-05T00:00:00+00:00")
    removed = deletion(lifecycle, [path])
    event_time(lifecycle, removed, "2026-10-05T01:00:00+00:00")
    later_retry = lifecycle.manager.create_job(
        kind="previews", config={**original.config, "is_retry": True, "parent_job_id": original.id}
    )
    event_time(lifecycle, later_retry, "2026-10-05T02:00:00+00:00")
    parent = start_sender(lifecycle, path)
    assert parent.status is JobStatus.COMPLETED
    assert parent.progress.outcome[job.SOURCE_GONE] == 1


@pytest.mark.parametrize("head_notice", ["delete", "import"])
def test_retry_head_display_bump_preserves_original_event_order(lifecycle, head_notice):
    path = lifecycle.add_file("reimported-head.mkv")
    Path(path).unlink()
    removed = deletion(lifecycle, [path])
    event_time(lifecycle, removed, "2026-10-05T01:00:00+00:00")
    incoming = lifecycle.manager.create_job(
        kind="previews", config={"source": "sonarr", "server_id": "plex", "webhook_paths": [path]}
    )
    event_time(lifecycle, incoming, "2026-10-05T02:00:00+00:00")
    head = removed if head_notice == "delete" else incoming
    original = head.created_at
    lifecycle.manager.upsert_retry_chain_job(
        canonical_path=path,
        basename="reimported-head",
        attempt=1,
        max_attempts=5,
        next_run_at=None,
        wait_seconds=None,
        outcome="completed",
        originating_job_id=head.id,
    )
    assert head.created_at != original
    assert lifecycle.manager._storage.original_job_times()[head.id] == original
    assert head.config["is_retry"] and head.config["is_retry_chain"]
    parent = start_sender(lifecycle, path)
    assert parent.status is JobStatus.PENDING
    assert parent.progress.outcome[job.FILE_NOT_FOUND] == 1


@pytest.mark.parametrize("started", [None, "2026-10-05T02:05:00+00:00"])
def test_fired_batch_without_fire_metadata_uses_safe_import_upper_bound(lifecycle, started):
    path = lifecycle.add_file("late-batch-import.mkv")
    Path(path).unlink()
    incoming = lifecycle.manager.create_job(
        kind="previews", config={"source": "sonarr", "server_id": "plex", "webhook_paths": [path]}
    )
    event_time(lifecycle, incoming, "2026-10-05T00:00:00+00:00")
    incoming.started_at = started
    removed = deletion(lifecycle, [path])
    event_time(lifecycle, removed, "2026-10-05T01:00:00+00:00")
    parent = start_sender(lifecycle, path)
    assert parent.status is JobStatus.PENDING
    assert parent.progress.outcome[job.FILE_NOT_FOUND] == 1


def test_missing_immutable_event_evidence_keeps_missing_source_retry(lifecycle, monkeypatch):
    path = lifecycle.add_file("no-event-evidence.mkv")
    Path(path).unlink()
    deletion(lifecycle, [path])
    reader = Mock(return_value={})
    monkeypatch.setattr(lifecycle.manager._storage, "original_job_times", reader)
    parent = start_sender(lifecycle, path)
    assert parent.status is JobStatus.PENDING
    assert parent.progress.outcome[job.FILE_NOT_FOUND] == 1
    reader.assert_called_once_with()


def test_event_time_storage_error_fails_closed(lifecycle, monkeypatch):
    connection = Mock()
    connection.execute.side_effect = sqlite3.OperationalError("unavailable")
    with monkeypatch.context() as patch:
        patch.setattr(lifecycle.manager._storage, "_conn", connection)
        assert lifecycle.manager._storage.original_job_times() == {}
    connection.execute.assert_called_once_with("SELECT id, created_at FROM jobs")


def test_memory_only_manager_keeps_missing_source_retry(lifecycle, monkeypatch):
    path = lifecycle.add_file("memory-only.mkv")
    Path(path).unlink()
    deletion(lifecycle, [path])
    with monkeypatch.context() as patch:
        patch.setattr(lifecycle.manager, "_storage", None)
        parent = start_sender(lifecycle, path)
    assert parent.status is JobStatus.PENDING
    assert parent.progress.outcome[job.FILE_NOT_FOUND] == 1
    assert len(lifecycle.children(parent)) == 1
