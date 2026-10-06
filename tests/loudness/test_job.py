"""The loudness kind's check and worker stages, and its run through the shared dispatcher (loudness.job)."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.jobs.dispatcher import JobDispatcher
from media_preview_generator.jobs.worker import WorkerPool
from media_preview_generator.loudness import job
from media_preview_generator.output.plex_hash import calculate_plex_hash
from media_preview_generator.processing.types import ProcessableItem
from media_preview_generator.servers.base import Library, ServerConfig, ServerType

from .test_plex_db import FIELDS, db  # noqa: F401 - the fixture

FILE = "/data/kids/Puffin Rock S01E34.mp4"


@pytest.fixture
def media(tmp_path, monkeypatch):
    """The fixture DB's file exists on disk (under its own path via a mapping)."""
    local = tmp_path / "media" / "Puffin Rock S01E34.mp4"
    local.parent.mkdir()
    local.write_bytes(b"x")
    return str(local)


def _cfg(local_path: str) -> ServerConfig:
    return ServerConfig(
        id="plex1",
        type=ServerType.PLEX,
        name="Plex",
        enabled=True,
        url="http://plex",
        auth={},
        libraries=[Library(id="4", name="Barn TV", remote_paths=("/data/kids",), enabled=True, kind="show")],
        path_mappings=[{"remote_prefix": "/data/kids", "local_prefix": str(Path(local_path).parent)}],
        loudness={"enabled": True, "library_ids": None},
        markers={"plex": {"db_write_confirmed_at": "2026-09-29T00:00:00Z"}},
    )


@pytest.fixture
def ctx(db, media):  # noqa: F811 - the fixture
    conn = sqlite3.connect(db._path())
    conn.execute(
        "UPDATE media_parts SET hash = ?, size = ? WHERE id = 1",
        (calculate_plex_hash(media), Path(media).stat().st_size),
    )
    conn.commit()
    conn.close()
    cfg = _cfg(media)
    c = job.LoudnessContext(registry=MagicMock(), ffmpeg="ffmpeg")
    c._dbs[cfg.id], c._ready[cfg.id] = db, ("", time.monotonic())
    with patch.object(job, "owners", return_value=[cfg]):
        yield c


def _item(path):
    return ProcessableItem(canonical_path=path, server_id="", title="ep")


def _streams(db):  # noqa: F811 - the fixture
    conn = sqlite3.connect(db._path())
    try:
        return dict(conn.execute("SELECT id, extra_data FROM media_streams WHERE stream_type_id = 2").fetchall())
    finally:
        conn.close()


def test_check_sends_a_file_with_unanalysed_streams_to_a_worker(ctx, media):
    assert job.check_item(_item(media), ctx=ctx) is None


def test_check_settles_a_missing_file_and_a_file_nobody_owns(ctx, tmp_path):
    assert job.check_item(_item(str(tmp_path / "gone.mkv")), ctx=ctx).outcome_key == job.FILE_NOT_FOUND
    with patch.object(job, "owners", return_value=[]):
        assert job.check_item(_item(__file__), ctx=ctx).outcome_key == job.NO_OWNERS


def test_worker_analyses_each_stream_once_and_writes_it(ctx, db, media):  # noqa: F811
    with patch.object(job.analyze, "run", return_value=FIELDS) as run:
        outcome = job.process_item(_item(media), ctx=ctx, gpu="intel")
    assert [c.args[2] for c in run.call_args_list] == [1, 2]  # stream indexes; a GPU worker still runs it plainly
    assert [c.kwargs["codec"] for c in run.call_args_list] == ["aac", "aac"]  # picks the decoder settings
    assert outcome.outcome_key == job.WRITTEN
    row = outcome.publisher_rows[0]
    assert row["status"] == job.WRITTEN and row["message"] == "2 stream(s), item marked analysed"
    assert all(json.loads(v)["ln:loudness"] == "-23.23" for v in _streams(db).values())
    # Done now: the next check never reaches a worker.
    assert job.check_item(_item(media), ctx=ctx).outcome_key == job.UP_TO_DATE


def test_a_failed_stream_fails_the_file_but_keeps_the_others(ctx, db, media):  # noqa: F811
    def run(ffmpeg, path, index, **kwargs):
        if index == 2:
            raise job.analyze.LoudnessError("ffmpeg exited 1")
        return FIELDS

    with patch.object(job.analyze, "run", side_effect=run):
        outcome = job.process_item(_item(media), ctx=ctx)
    assert outcome.outcome_key == job.FAILED and "stream 2 (aac): ffmpeg exited 1" in outcome.message
    written = {k: "ln:loudness" in (v or "") for k, v in _streams(db).items()}
    assert written == {11: True, 12: False}


def test_an_unready_database_keeps_the_file_for_the_retry_without_analysing(ctx, media):
    ctx._ready["plex1"] = ("Plex doesn't have its database open", time.monotonic())
    with patch.object(job.analyze, "run") as run:
        outcome = job.process_item(_item(media), ctx=ctx)
    run.assert_not_called()
    assert outcome.outcome_key == job.WAITING and "database open" in outcome.message
    assert job.check_item(_item(media), ctx=ctx).outcome_key == job.WAITING


def test_a_busy_database_read_keeps_the_file_for_the_retry(ctx, media):
    with patch.object(job, "read_streams", side_effect=job.DatabaseBusyError("Plex's database is busy")):
        assert job.check_item(_item(media), ctx=ctx).outcome_key == job.WAITING


def test_a_file_without_audio_is_up_to_date(ctx, db, media):  # noqa: F811
    conn = sqlite3.connect(db._path())
    conn.execute("DELETE FROM media_streams WHERE stream_type_id = 2")
    conn.commit()
    conn.close()
    outcome = job.check_item(_item(media), ctx=ctx)
    assert outcome.outcome_key == job.UP_TO_DATE and outcome.message == "No audio track"


def test_a_pinned_job_leaves_other_servers_alone():
    plex = MagicMock(id="plex1", enabled=True, type=job.ServerType.PLEX, libraries=[])
    other = MagicMock(id="plex2", enabled=True, type=job.ServerType.PLEX, libraries=[])
    match = [MagicMock(library_id="4")]
    with (
        patch.object(job, "owning_servers", return_value=[(plex, None, match), (other, None, match)]),
        patch.object(job, "load_server_loudness", return_value=MagicMock(enabled=True)),
        patch.object(job, "library_chosen", return_value=True),
    ):
        assert [c.id for c in job.owners("/m/a.mkv", None)] == ["plex1", "plex2"]
        assert [c.id for c in job.owners("/m/a.mkv", None, "plex2")] == ["plex2"]


def test_runs_through_the_shared_dispatcher(ctx, db, media):  # noqa: F811
    config = MagicMock(cpu_threads=1, gpu_threads=0, scan_workers=1, regenerate_thumbnails=False, server_id_filter=None)
    dispatcher = JobDispatcher(WorkerPool(cpu_workers=1, gpu_workers=0, selected_gpus=[]))
    try:
        with (
            patch.object(job.analyze, "run", return_value=FIELDS),
            patch("media_preview_generator.processing.generator._notify_file_result"),
            patch("media_preview_generator.web.jobs.get_job_manager"),
        ):
            tracker = dispatcher.submit_items(
                "j1", [_item(media)], config, MagicMock(), kind="loudness", handlers=job.kind_handlers(ctx)
            )
            assert tracker.wait(timeout=20)
        assert tracker.outcome_counts[job.WRITTEN] == 1
        assert tracker.publishers_aggregate["plex1"]["counts"] == {job.WRITTEN: 1}
    finally:
        dispatcher.shutdown()


def test_a_cancel_mid_file_is_not_recorded_as_up_to_date(ctx, media):
    cancelled = False

    def analyse(*args, **kwargs):
        nonlocal cancelled
        cancelled = True
        return FIELDS

    with patch.object(job.analyze, "run", side_effect=analyse):
        outcome = job.process_item(_item(media), ctx=ctx, cancel_check=lambda: cancelled)
    assert outcome.outcome_key == job.FAILED and "cancelled" in outcome.message


def test_an_unwritable_database_is_asked_again_later_a_writable_one_is_kept(media):
    cfg = _cfg(media)
    c = job.LoudnessContext(registry=MagicMock(), ffmpeg="ffmpeg")
    answers = iter([job.Capability.UNREACHABLE, job.Capability.READY])

    def checks(self, *, deadline):
        return SimpleNamespace(state=next(answers), message="Plex is restarting")

    with (
        patch.object(job, "create_loudness_db", return_value=job.LocalPlexDb(lambda: "unused")),
        patch.object(job.LocalPlexDb, "file_checks", checks),
    ):
        assert c.db(cfg)[1] == "Plex is restarting"
        assert c.db(cfg)[1] == "Plex is restarting"  # within RECHECK_S: not asked again
        c._ready[cfg.id] = (c._ready[cfg.id][0], time.monotonic() - job.RECHECK_S - 1)
        assert c.db(cfg)[1] == ""
        assert c.db(cfg)[1] == ""  # writable: kept (the iterator would raise if asked again)


def test_a_stream_whose_extra_data_doesnt_decode_fails_that_server_not_the_job(ctx, media, db):  # noqa: F811
    conn = sqlite3.connect(db._path())
    conn.execute("UPDATE media_streams SET extra_data = '{not json' WHERE stream_type_id = 2")
    conn.commit()
    conn.close()
    outcome = job.check_item(_item(media), ctx=ctx)
    assert outcome.outcome_key == job.FAILED
    assert outcome.publisher_rows[0]["message"].startswith("Couldn't read the file's audio streams in Plex")


def test_a_file_analysed_but_its_item_unmarked_is_marked_without_analysing_again(ctx, db, media):  # noqa: F811
    for stream_id in (11, 12):
        job.write_stream(db, stream_id, FIELDS, deadline=1e12)
    assert job.check_item(_item(media), ctx=ctx) is None  # Plex would analyse it again: to a worker
    with patch.object(job.analyze, "run") as run:
        outcome = job.process_item(_item(media), ctx=ctx)
    run.assert_not_called()
    assert outcome.outcome_key == job.UP_TO_DATE
    assert outcome.publisher_rows[0]["status"] == job.UP_TO_DATE
    assert outcome.publisher_rows[0]["message"] == "Existing loudness reused; item marked analysed"
    assert job.check_item(_item(media), ctx=ctx).outcome_key == job.UP_TO_DATE


def test_a_file_plex_hasnt_added_waits_for_the_retry(ctx, tmp_path):
    other = tmp_path / "media" / "New episode.mkv"
    other.write_bytes(b"x")
    assert job.check_item(_item(str(other)), ctx=ctx).outcome_key == job.NOT_IN_LIBRARY
    assert job.process_item(_item(str(other)), ctx=ctx).outcome_key == job.NOT_IN_LIBRARY


def test_an_item_that_cant_be_marked_fails_the_file(ctx, db, media):  # noqa: F811
    with (
        patch.object(job.analyze, "run", return_value=FIELDS),
        patch.object(job, "mark_item", side_effect=job.PublishError("Plex's item 1 is gone")),
    ):
        outcome = job.process_item(_item(media), ctx=ctx)
    assert outcome.outcome_key == job.FAILED and "marking item 1" in outcome.message


def test_a_server_with_loudness_off_owns_nothing():
    plex = MagicMock(id="plex1", enabled=True, type=job.ServerType.PLEX, libraries=[])
    with (
        patch.object(job, "owning_servers", return_value=[(plex, None, [MagicMock(library_id="4")])]),
        patch.object(job, "load_server_loudness", return_value=MagicMock(enabled=False)),
    ):
        assert job.owners("/m/a.mkv", None) == []


@pytest.mark.parametrize("stage", ["write_stream", "mark_item"])
@pytest.mark.parametrize(
    "error",
    [
        job.DatabaseBusyError("Plex database busy"),
        job.PublishError("Plex database is no longer open", state=job.Capability.UNREACHABLE),
    ],
)
def test_temporarily_unavailable_publication_remains_retryable(ctx, media, monkeypatch, stage, error):
    monkeypatch.setattr(job, stage, MagicMock(side_effect=error))
    with patch.object(job.analyze, "run", return_value=FIELDS):
        result = job.process_item(_item(media), ctx=ctx)
    assert result.outcome_key == job.WAITING
    assert str(error) in result.message


def test_same_size_source_replacement_waits_instead_of_accepting_native_analysis(ctx, db, media):  # noqa: F811
    for stream_id in (11, 12):
        job.write_stream(db, stream_id, FIELDS, deadline=1e12)
    job.mark_item(db, 1, deadline=1e12)
    assert job.check_item(_item(media), ctx=ctx).outcome_key == job.UP_TO_DATE
    Path(media).write_bytes(b"y")
    assert job.check_item(_item(media), ctx=ctx).outcome_key == job.WAITING
    with patch.object(job.analyze, "run") as run:
        assert job.process_item(_item(media), ctx=ctx).outcome_key == job.WAITING
    run.assert_not_called()


def test_plex_without_complete_source_fingerprint_waits(ctx, db, media):  # noqa: F811
    with sqlite3.connect(db._path()) as conn:
        conn.execute("UPDATE media_parts SET hash = NULL WHERE id = 1")
    conn.close()
    assert job.check_item(_item(media), ctx=ctx).outcome_key == job.WAITING


def test_source_changed_by_analysis_is_never_published(ctx, db, media):  # noqa: F811
    def run(*args, **kwargs):
        Path(media).write_bytes(b"changed while decoding")
        return FIELDS

    with patch.object(job.analyze, "run", side_effect=run):
        assert job.process_item(_item(media), ctx=ctx).outcome_key == job.WAITING
    assert all("ln:loudness" not in (extra or "") for extra in _streams(db).values())


def test_mixed_corrupt_track_and_busy_write_preserves_both_failure_and_retry(ctx, db, media, monkeypatch):  # noqa: F811
    holder = sqlite3.connect(db._path(), isolation_level=None)
    monkeypatch.setattr(job, "BUSY_TIMEOUT_S", 0.02)

    def analyse(ffmpeg, path, index, **kwargs):
        if index == 1:
            raise job.analyze.LoudnessError("corrupt first track")
        holder.execute("BEGIN IMMEDIATE")
        return FIELDS

    try:
        with patch.object(job.analyze, "run", side_effect=analyse):
            outcome = job.process_item(_item(media), ctx=ctx)
    finally:
        holder.rollback()
        holder.close()
    assert outcome.outcome_key == job.FAILED
    assert "corrupt first track" in outcome.message and "busy" in outcome.message
    assert outcome.publisher_rows[0]["retryable"] is True


def test_already_cancelled_marking_only_file_does_not_write(ctx, db, media):  # noqa: F811
    for stream_id in (11, 12):
        job.write_stream(db, stream_id, FIELDS, deadline=1e12)
    with patch.object(job.analyze, "run") as analyse:
        outcome = job.process_item(_item(media), ctx=ctx, cancel_check=lambda: True)
    assert outcome.outcome_key == job.FAILED and "cancelled" in outcome.message
    assert not job.read_streams(db, [FILE], deadline=1e12)[0][0].item_marked
    analyse.assert_not_called()


@pytest.mark.parametrize("stage", ["check", "worker"])
@pytest.mark.parametrize("retryable_failure", [False, True])
def test_dispatcher_preserves_pending_publisher_details_in_callback(ctx, media, stage, retryable_failure):
    from media_preview_generator.processing.generator import set_file_result_callback

    rows = [
        {
            "server_id": "plex1",
            "server_name": "Plex",
            "server_type": "plex",
            "status": job.FAILED,
            "message": "failed track",
        }
    ]
    if retryable_failure:
        rows[0]["retryable"] = True
    else:
        rows.append(
            {
                "server_id": "plex2",
                "server_name": "Other Plex",
                "server_type": "plex",
                "status": job.WAITING,
                "message": "busy",
            }
        )
    outcome = job._settle(rows)
    handlers = job.KindHandlers(
        check_fn=lambda item, **kwargs: outcome if stage == "check" else None,
        process_fn=lambda item, **kwargs: outcome,
        outcome_keys=job.OUTCOME_KEYS,
    )
    config = MagicMock(cpu_threads=1, gpu_threads=0, scan_workers=1, regenerate_thumbnails=False, server_id_filter=None)
    dispatcher = JobDispatcher(WorkerPool(cpu_workers=1, gpu_workers=0, selected_gpus=[]))
    notify = MagicMock()
    set_file_result_callback(notify, job_id="mixed")
    try:
        with patch("media_preview_generator.web.jobs.get_job_manager"):
            tracker = dispatcher.submit_items(
                "mixed", [_item(media)], config, MagicMock(), kind=job.JOB_KIND_LOUDNESS, handlers=handlers
            )
            assert tracker.wait(timeout=20)
        notify.assert_called_once()
        assert notify.call_args.args[0] == media
        assert notify.call_args.args[1] == job.FAILED
        assert notify.call_args.args[4] == rows
    finally:
        dispatcher.shutdown()
        set_file_result_callback(None, job_id="mixed")
