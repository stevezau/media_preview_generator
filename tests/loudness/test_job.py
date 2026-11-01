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
from media_preview_generator.markers.publishers.base import CapabilityReport
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


@pytest.mark.parametrize(
    "extra", ["Puffin Rock S01E34-trailer.mp4", "Trailers/Puffin Rock S01E34.mp4"], ids=["suffix", "folder"]
)
def test_a_trailer_or_other_extra_is_skipped_not_a_failed_music_file(ctx, db, media, extra):  # noqa: F811
    # Lab: a movie's trailer, which Plex lists as an extra (metadata_type 12), failed the job as "music analysis is
    # not supported"; markers skip the same files.
    local = Path(media).parent / extra
    local.parent.mkdir(exist_ok=True)
    local.write_bytes(b"trailer")
    conn = sqlite3.connect(db._path())
    conn.execute("UPDATE metadata_items SET metadata_type = 12 WHERE id = 1")
    conn.execute(
        "UPDATE media_parts SET file = ?, hash = ?, size = ? WHERE id = 1",
        (f"/data/kids/{extra}", calculate_plex_hash(str(local)), local.stat().st_size),
    )
    conn.commit()
    conn.close()

    with patch.object(job, "write_line") as write_line:
        outcome = job.check_item(_item(str(local)), ctx=ctx)

    assert (outcome.outcome_key, outcome.message) == (job.SKIPPED, "Extras aren't analysed for loudness")
    assert [(row["server_id"], row["status"]) for row in outcome.publisher_rows] == [("plex1", job.SKIPPED)]
    # The job log names each file as it happens: a skipped extra wrote no line at all.
    write_line.assert_called_once_with(f"{job.display_name(str(local))}: extras aren't analysed for loudness")
    assert job.SKIPPED in job.OUTCOME_KEYS
    # Neither a success nor a failure: a job of nothing else completes without an error or a warning.
    assert job._completion({job.SKIPPED: 2}, []) == (0, None)


def _plex_lists(db, local: Path, plex_path: str, metadata_type: int) -> None:  # noqa: F811
    conn = sqlite3.connect(db._path())
    conn.execute("UPDATE metadata_items SET metadata_type = ? WHERE id = 1", (metadata_type,))
    conn.execute(
        "UPDATE media_parts SET file = ?, hash = ?, size = ? WHERE id = 1",
        (plex_path, calculate_plex_hash(str(local)), local.stat().st_size),
    )
    conn.commit()
    conn.close()


def _flat_library(db, folder: str, local: Path) -> tuple[job.LoudnessContext, ServerConfig]:  # noqa: F811
    cfg = ServerConfig(
        id="plex1",
        type=ServerType.PLEX,
        name="Plex",
        enabled=True,
        url="http://plex",
        auth={},
        libraries=[Library(id="7", name="Home Videos", remote_paths=(f"/data/{folder}",), enabled=True, kind="movie")],
        path_mappings=[{"remote_prefix": f"/data/{folder}", "local_prefix": str(local.parent)}],
        loudness={"enabled": True, "library_ids": None},
        markers={"plex": {"db_write_confirmed_at": "2026-09-29T00:00:00Z"}},
    )
    c = job.LoudnessContext(registry=MagicMock(), ffmpeg="ffmpeg")
    c._dbs[cfg.id], c._ready[cfg.id] = db, ("", time.monotonic())
    return c, cfg


@pytest.mark.parametrize("folder", ["Other", "Shorts", "Trailers"])
def test_a_movie_in_a_library_folder_named_like_an_extras_folder_is_analysed(db, tmp_path, folder):  # noqa: F811
    """Lab (5.1.0 analysed these): a flat "Home Videos" library at /data/Other had every movie skipped as an extra,
    from its folder's name alone, though Plex lists each as a movie."""
    local = tmp_path / folder / "Birthday Party 2019.mp4"
    local.parent.mkdir()
    local.write_bytes(b"home video")
    _plex_lists(db, local, f"/data/{folder}/Birthday Party 2019.mp4", 1)
    c, cfg = _flat_library(db, folder, local)
    with patch.object(job, "owners", return_value=[cfg]), patch.object(job, "write_line") as write_line:
        outcome = job.check_item(_item(str(local)), ctx=c)
    assert outcome is None  # its audio has no loudness yet: a worker analyses it
    write_line.assert_not_called()


def test_a_file_plex_lists_as_an_extra_is_skipped_whatever_its_path(db, tmp_path):  # noqa: F811
    local = tmp_path / "Movies" / "Heat (1995) behind the camera.mp4"
    local.parent.mkdir()
    local.write_bytes(b"featurette")
    _plex_lists(db, local, "/data/Movies/Heat (1995) behind the camera.mp4", 12)
    c, cfg = _flat_library(db, "Movies", local)
    with patch.object(job, "owners", return_value=[cfg]), patch.object(job, "write_line") as write_line:
        outcome = job.check_item(_item(str(local)), ctx=c)
    assert (outcome.outcome_key, outcome.message) == (job.SKIPPED, job.EXTRAS_NOT_ANALYSED)
    write_line.assert_called_once()


@pytest.mark.parametrize(
    ("name", "key"),
    [("Heat (1995)-sample.mkv", job.SKIPPED), ("Heat (1995).mkv", job.NOT_IN_LIBRARY)],
    ids=["extra-by-its-name", "feature"],
)
def test_a_file_plex_doesnt_list_is_judged_an_extra_by_its_path(db, tmp_path, name, key):  # noqa: F811
    """Plex ignores samples and adds the rest later: only a file that looks like an extra isn't waited for."""
    local = tmp_path / "Movies" / name
    local.parent.mkdir()
    local.write_bytes(b"x")
    c, cfg = _flat_library(db, "Movies", local)
    with (
        patch.object(job, "owners", return_value=[cfg]),
        patch.object(job, "write_line"),
        patch.object(c, "request_plex_scan") as scan,
    ):
        outcome = job.check_item(_item(str(local)), ctx=c)
    assert outcome.outcome_key == key
    assert scan.called is (key == job.NOT_IN_LIBRARY)


def test_an_extras_refusal_from_a_plex_database_node_keeps_its_type():
    from media_preview_generator.loudness.plex_db import PLEX_TYPE_EXTRA, NotAnalysedError
    from media_preview_generator.markers.publishers import plex_remote

    sent = NotAnalysedError("trailers and other extras aren't analysed", plex_type=PLEX_TYPE_EXTRA)
    error = plex_remote.error_from_json(plex_remote.error_to_json(sent))
    assert type(error) is NotAnalysedError and (error.plex_type, str(error)) == (PLEX_TYPE_EXTRA, str(sent))


def test_every_outcome_a_job_records_is_in_the_references_table():
    # A script keyed on docs/reference.md's "File outcomes" table needs a meaning for every value a job can record.
    reference = Path(job.__file__).resolve().parents[2] / "docs" / "reference.md"
    text = reference.read_text(encoding="utf-8")
    table = text.split("loudness worker is free. File outcomes:", 1)[1].split("### Plex loudness Endpoints", 1)[0]
    assert [key for key in job.OUTCOME_KEYS if f"| `{key}` |" not in table] == []


def test_check_settles_a_missing_file_and_a_file_nobody_owns(ctx, tmp_path):
    assert job.check_item(_item(str(tmp_path / "gone.mkv")), ctx=ctx).outcome_key == job.FILE_NOT_FOUND
    with patch.object(job, "owners", return_value=[]):
        assert job.check_item(_item(__file__), ctx=ctx).outcome_key == job.NO_OWNERS


def test_a_missing_file_a_newer_one_replaced_is_skipped_not_retried(ctx, tmp_path):
    gone = str(tmp_path / "media" / "Anna Pigeon S01E10 720p.mkv")
    with (
        patch.object(
            job, "source_replaced_reason", return_value="Skipped: replaced by a newer file (S01E10 1080p.mkv)"
        ) as reason,
        patch.object(job, "write_line") as write_line,
    ):
        outcome = job.check_item(_item(gone), ctx=ctx)
    reason.assert_called_once_with(gone, ctx.registry)
    assert outcome.outcome_key == job.SOURCE_GONE
    assert outcome.message == "Skipped: replaced by a newer file (S01E10 1080p.mkv)"
    assert "Skipped: replaced by a newer file (S01E10 1080p.mkv)" in write_line.call_args.args[0]


def test_a_date_named_file_replaced_in_its_folder_is_skipped_not_retried(tmp_path):
    """The real helper end to end: the same air date, another release, in the same folder."""
    folder = tmp_path / "Dateline NBC (1992)" / "Season 2026"
    folder.mkdir(parents=True)
    (folder / "Dateline NBC (1992) - 2026-10-09 - Heartless -RAWR.mkv").write_bytes(b"x")
    gone = folder / "Dateline NBC (1992) - 2026-10-09 - Heartless -EDITH.mkv"
    registry = MagicMock()
    with (
        patch("media_preview_generator.processing.multi_server.disk_roots", return_value=[str(tmp_path)]),
        patch("media_preview_generator.processing.multi_server.gone_from_disk", return_value=True),
        patch("media_preview_generator.processing.multi_server._in_movie_library", return_value=False),
        patch.object(job, "write_line"),
    ):
        ctx = job.LoudnessContext(registry=registry, ffmpeg="ffmpeg")
        outcome = job.check_item(_item(str(gone)), ctx=ctx)
    assert outcome.outcome_key == job.SOURCE_GONE
    assert "Heartless -RAWR.mkv" in outcome.message


def test_a_missing_file_nothing_replaced_is_still_not_found(ctx, tmp_path):
    gone = str(tmp_path / "Anna Pigeon" / "Season 01" / "Anna Pigeon S01E10.mkv")
    with (
        patch.object(job, "source_replaced_reason", return_value=None),
        patch.object(job, "write_line") as write_line,
    ):
        outcome = job.check_item(_item(gone), ctx=ctx)
    assert outcome.outcome_key == job.FILE_NOT_FOUND
    write_line.assert_called_once_with("Anna Pigeon S01E10: not on disk")


def test_a_missing_file_an_import_webhook_deleted_gets_its_job_log_line(ctx, tmp_path):
    gone = str(tmp_path / "Anna Pigeon" / "Season 01" / "Anna Pigeon S01E10.mkv")
    ctx.deleted_paths = frozenset({gone})
    with patch.object(job, "write_line") as write_line:
        outcome = job.check_item(_item(gone), ctx=ctx)
    assert outcome.outcome_key == job.SOURCE_GONE
    write_line.assert_called_once_with(f"Anna Pigeon S01E10: {outcome.message}")


def _saved_nice_level(monkeypatch, saved) -> None:
    settings = {} if saved is None else {"loudness_nice_level": saved}
    monkeypatch.setattr(job, "get_settings_manager", lambda: SimpleNamespace(get=settings.get))


@pytest.mark.parametrize(
    ("saved", "expected"),
    [(None, 10), ("abc", 10), (25, 19), (-3, 0), (7, 7), ("5", 5), (0, 0)],
)
def test_the_nice_level_is_the_saved_one_clamped_to_0_19(monkeypatch, saved, expected):
    _saved_nice_level(monkeypatch, saved)
    assert job.loudness_nice_level() == expected


def test_the_saved_nice_level_reaches_each_stream_run(ctx, db, media, monkeypatch):  # noqa: F811
    _saved_nice_level(monkeypatch, 19)
    with patch.object(job.analyze, "run", return_value=FIELDS) as run:
        job.process_item(_item(media), ctx=ctx, gpu="intel")
    assert [c.kwargs["nice_level"] for c in run.call_args_list] == [19, 19]


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


def test_worker_reports_percent_scaled_across_streams_in_the_previews_callback_shape(ctx, media):
    calls = []

    def run(ffmpeg, path, index, *, on_progress, **kwargs):
        on_progress(212.0, 2.0)  # half of the 424 s stream
        return FIELDS

    with patch.object(job.analyze, "run", side_effect=run):
        job.process_item(_item(media), ctx=ctx, progress_callback=lambda *a: calls.append(a))
    # half of stream 1, stream 1 done, half of stream 2, stream 2 done
    assert calls == [
        (25.0, 212.0, 424.0, "2.0x", (212.0 + 424.0) / 2.0),
        (50.0, 424.0, 424.0, None, 0.0),
        (75.0, 212.0, 424.0, "2.0x", 106.0),
        (100.0, 424.0, 424.0, None, 0.0),
    ]


def test_progress_reporter_scales_clamps_and_estimates_remaining():
    calls = []
    report = job._stream_progress_reporter(lambda *a: calls.append(a), n=2, streams=2, durations_s=[100.0, 50.0])
    report(25.0, 2.0)
    report(80.0, None)  # past the end of a stream shorter than reported; unknown speed
    assert calls == [(75.0, 25.0, 50.0, "2.0x", 12.5), (100.0, 80.0, 50.0, None, None)]
    first = []
    job._stream_progress_reporter(lambda *a: first.append(a), n=1, streams=2, durations_s=[100.0, 50.0])(50.0, 2.0)
    assert first == [(25.0, 50.0, 100.0, "2.0x", 50.0)]  # (50 left + 50 for the next stream) at 2x


def test_progress_with_unknown_duration_reports_speed_only():
    calls = []
    job._stream_progress_reporter(lambda *a: calls.append(a), n=1, streams=1, durations_s=[0.0])(7.0, 1.5)
    assert calls == [(0.0, 7.0, 0.0, "1.5x", None)]


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


@pytest.mark.parametrize("capability_known", [False, True], ids=["first-request", "mid-job"])
def test_a_plex_database_node_that_does_not_answer_keeps_the_file_waiting(media, capability_known):
    """A node that is restarting or unplugged is a Plex this app can't reach now: retried, never a failed file."""
    from .test_guard import _remote_loudness_db

    cfg = _cfg(media)
    c = job.LoudnessContext(registry=MagicMock(), ffmpeg="ffmpeg")
    c._dbs[cfg.id] = _remote_loudness_db(capability_known=capability_known)
    if capability_known:
        c._ready[cfg.id] = ("", time.monotonic())
    with patch.object(job, "owners", return_value=[cfg]):
        outcome = job.check_item(_item(media), ctx=c)
    assert outcome.outcome_key == job.WAITING, outcome
    assert "Can't reach the Plex database node" in outcome.message and "Markers" not in outcome.message


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
        return CapabilityReport(next(answers), "Plex is restarting")

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


def test_a_file_plex_hasnt_added_asks_plex_to_scan_its_folder_once_per_folder(ctx, tmp_path):
    server = MagicMock()
    ctx.registry.get.return_value = server
    first, second = tmp_path / "media" / "New 1.mkv", tmp_path / "media" / "New 2.mkv"
    for f in (first, second):
        f.write_bytes(b"x")
    with patch.object(job, "write_line"):
        assert job.check_item(_item(str(first)), ctx=ctx).outcome_key == job.NOT_IN_LIBRARY
        assert job.check_item(_item(str(second)), ctx=ctx).outcome_key == job.NOT_IN_LIBRARY
    ctx.registry.get.assert_called_once_with("plex1")
    server.trigger_refresh.assert_called_once_with(item_id=None, remote_path=str(first))


def test_a_file_already_in_plex_triggers_no_scan(ctx, media):
    assert job.check_item(_item(media), ctx=ctx) is None
    ctx.registry.get.assert_not_called()


def test_a_scan_request_that_does_not_take_leaves_the_file_waiting_and_is_logged_as_a_request(ctx, tmp_path):
    """``trigger_refresh`` logs per-path failures itself and never raises, so the job log can only say it asked."""
    ctx.registry.get.return_value.trigger_refresh.return_value = None
    new = tmp_path / "media" / "New.mkv"
    new.write_bytes(b"x")
    with patch.object(job, "write_line") as write_line:
        outcome = job.check_item(_item(str(new)), ctx=ctx)
    assert outcome.outcome_key == job.NOT_IN_LIBRARY
    ctx.registry.get.return_value.trigger_refresh.assert_called_once_with(item_id=None, remote_path=str(new))
    assert "requested a scan of its folder from" in str(write_line.call_args)
    assert "asked" not in str(write_line.call_args)


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


def _add_audio_stream(database, stream_id: int, index: int, codec: str = "aac") -> None:
    with sqlite3.connect(database._path()) as conn:
        conn.execute(
            'INSERT INTO media_streams (id, stream_type_id, "index", codec, extra_data, media_part_id, media_item_id) '
            "VALUES (?, 2, ?, ?, NULL, 1, 1)",
            (stream_id, index, codec),
        )


def test_batch_progress_spans_the_batch_once_and_estimates_later_streams():
    calls = []
    first_batch = job._batch_progress_reporter(
        lambda *values: calls.append(values), first=1, count=3, streams=4, duration_s=100.0, later_s=100.0
    )
    first_batch(50.0, 2.0)
    last_stream = job._batch_progress_reporter(
        lambda *values: calls.append(values), first=4, count=1, streams=4, duration_s=100.0, later_s=0.0
    )
    last_stream(50.0, 2.0)

    assert calls == [
        (37.5, 50.0, 100.0, "2.0x", 75.0),
        (87.5, 50.0, 100.0, "2.0x", 25.0),
    ]


@pytest.mark.parametrize("extra_tracks", [0, 1])
def test_worker_batches_two_or_three_missing_streams_by_absolute_index(ctx, db, media, extra_tracks, monkeypatch):  # noqa: F811
    _saved_nice_level(monkeypatch, 19)
    tracks = [(1, "aac", 424000), (2, "aac", 424000)]
    expected_ids = [(11, 1), (12, 2)]
    if extra_tracks:
        _add_audio_stream(db, 13, 7, "eac3")
        tracks.append((7, "eac3", 424000))
        expected_ids.append((13, 7))
    fields_by_index = {index: {**FIELDS, "ln:loudness": f"-{20 + index}.00"} for index, _codec, _duration in tracks}

    with (
        patch.object(job.analyze, "run_many", return_value=fields_by_index) as run_many,
        patch.object(job.analyze, "run") as run,
    ):
        outcome = job.process_item(_item(media), ctx=ctx)

    assert run_many.call_args.args == (ctx.ffmpeg, media, tracks)
    assert run_many.call_args.kwargs == {
        "cancel_check": None,
        "pause_check": None,
        "on_progress": None,
        "nice_level": 19,
    }
    run.assert_not_called()
    assert outcome.outcome_key == job.WRITTEN
    stored = _streams(db)
    for stream_id, index in expected_ids:
        assert json.loads(stored[stream_id])["ln:loudness"] == fields_by_index[index]["ln:loudness"]


def test_worker_limits_batches_to_three_then_uses_the_single_stream_path(ctx, db, media):  # noqa: F811
    for stream_id, index in ((13, 3), (14, 5)):
        _add_audio_stream(db, stream_id, index)

    with (
        patch.object(job.analyze, "run_many", return_value={1: FIELDS, 2: FIELDS, 3: FIELDS}) as run_many,
        patch.object(job.analyze, "run", return_value=FIELDS) as run,
    ):
        outcome = job.process_item(_item(media), ctx=ctx)

    assert run_many.call_args.args[2] == [(1, "aac", 424000), (2, "aac", 424000), (3, "aac", 424000)]
    assert run.call_args.args[2] == 5
    assert outcome.outcome_key == job.WRITTEN


def test_worker_batches_only_uncached_streams_then_keeps_single_stream_path(ctx, db, media):  # noqa: F811
    job.write_stream(db, 11, FIELDS, deadline=1e12)
    _add_audio_stream(db, 13, 7, "eac3")
    expected = {2: FIELDS, 7: FIELDS}

    with (
        patch.object(job.analyze, "run_many", return_value=expected) as run_many,
        patch.object(job.analyze, "run") as run,
    ):
        outcome = job.process_item(_item(media), ctx=ctx)

    assert run_many.call_args.args[2] == [(2, "aac", 424000), (7, "eac3", 424000)]
    run.assert_not_called()
    assert outcome.outcome_key == job.WRITTEN
    assert json.loads(_streams(db)[11])["ln:loudness"] == FIELDS["ln:loudness"]

    _add_audio_stream(db, 14, 9)
    with (
        patch.object(job.analyze, "run_many") as run_many,
        patch.object(job.analyze, "run", return_value=FIELDS) as run,
    ):
        job.process_item(_item(media), ctx=ctx)
    run_many.assert_not_called()
    assert run.call_args.args[2] == 9


def test_worker_analyses_a_repeated_absolute_index_once_and_writes_each_plex_row(ctx, db, media):  # noqa: F811
    _add_audio_stream(db, 13, 2)
    with (
        patch.object(job.analyze, "run_many", return_value={1: FIELDS, 2: FIELDS}) as run_many,
        patch.object(job.analyze, "run") as run,
    ):
        outcome = job.process_item(_item(media), ctx=ctx)

    assert run_many.call_args.args[2] == [(1, "aac", 424000), (2, "aac", 424000)]
    run.assert_not_called()
    assert outcome.outcome_key == job.WRITTEN
    stored = _streams(db)
    assert all("ln:loudness" in stored[stream_id] for stream_id in (11, 12, 13))


def test_worker_falls_back_once_per_stream_after_an_ordinary_batch_failure(ctx, db, media):  # noqa: F811
    _add_audio_stream(db, 13, 7)

    def analyse(_ffmpeg, _path, index, **_kwargs):
        if index == 2:
            raise job.analyze.LoudnessError("one track is corrupt")
        return FIELDS

    with (
        patch.object(job.analyze, "run_many", side_effect=job.analyze.LoudnessError("batch failed")) as run_many,
        patch.object(job.analyze, "run", side_effect=analyse) as run,
    ):
        outcome = job.process_item(_item(media), ctx=ctx)

    assert run_many.call_args.args == (
        ctx.ffmpeg,
        media,
        [(1, "aac", 424000), (2, "aac", 424000), (7, "aac", 424000)],
    )
    assert [call.args[2] for call in run.call_args_list] == [1, 2, 7]
    stored = _streams(db)
    assert "ln:loudness" in stored[11] and stored[12] is None
    assert "ln:loudness" in stored[13]
    assert outcome.outcome_key == job.FAILED
    assert "stream 2 (aac): one track is corrupt" in outcome.message


@pytest.mark.parametrize("error", [job.analyze.LoudnessCancelled, job.analyze.LoudnessTimeout])
def test_worker_does_not_retry_cancelled_or_timed_out_batches(ctx, db, media, error):  # noqa: F811
    _add_audio_stream(db, 13, 7)
    with (
        patch.object(job.analyze, "run_many", side_effect=error("batch stopped")) as run_many,
        patch.object(job.analyze, "run") as run,
    ):
        outcome = job.process_item(_item(media), ctx=ctx)

    assert run_many.call_args.args == (
        ctx.ffmpeg,
        media,
        [(1, "aac", 424000), (2, "aac", 424000), (7, "aac", 424000)],
    )
    run.assert_not_called()
    assert outcome.outcome_key == job.FAILED
    assert not any(json.loads(value or "{}").get("ln:loudness") for value in _streams(db).values())


def test_worker_does_not_retry_an_ordinary_batch_error_if_cancelled_during_failure(ctx, db, media):  # noqa: F811
    cancelled = False

    def fail_batch(*_args, **_kwargs):
        nonlocal cancelled
        cancelled = True
        raise job.analyze.LoudnessError("batch failed")

    _add_audio_stream(db, 13, 7)
    with (
        patch.object(job.analyze, "run_many", side_effect=fail_batch) as run_many,
        patch.object(job.analyze, "run") as run,
    ):
        outcome = job.process_item(_item(media), ctx=ctx, cancel_check=lambda: cancelled)

    assert run_many.call_args.args == (
        ctx.ffmpeg,
        media,
        [(1, "aac", 424000), (2, "aac", 424000), (7, "aac", 424000)],
    )
    run.assert_not_called()
    assert outcome.outcome_key == job.FAILED
    assert "cancelled" in outcome.message


def test_worker_uses_the_real_batch_parser_and_writes_each_named_report_to_its_stream(ctx, db, media, tmp_path):  # noqa: F811
    args_file = tmp_path / "args.txt"
    ffmpeg = tmp_path / "ffmpeg"
    report_1 = {**json.loads(json.dumps(FIELDS)), "ln:loudness": "-21.11", "ln:gainOffset": "0.11"}
    report_2 = {**FIELDS, "ln:loudness": "-32.22", "ln:gainOffset": "0.22"}
    loudnorm_1 = {
        "input_i": report_1["ln:loudness"],
        "input_tp": report_1["ln:peak"],
        "input_lra": report_1["ln:lra"],
        "input_thresh": report_1["ln:threshold"],
        "target_offset": report_1["ln:gainOffset"],
    }
    loudnorm_2 = {
        "input_i": report_2["ln:loudness"],
        "input_tp": report_2["ln:peak"],
        "input_lra": report_2["ln:lra"],
        "input_thresh": report_2["ln:threshold"],
        "target_offset": report_2["ln:gainOffset"],
    }
    ffmpeg.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > '{args_file}'\n"
        "printf '%s\\n' "
        f"'[loudnorm@track2 @ 0x1234] {json.dumps(loudnorm_2)}' "
        f"'[loudnorm@track1 @ 0x1234] {json.dumps(loudnorm_1)}' >&2\n"
    )
    ffmpeg.chmod(0o755)
    ctx.ffmpeg = str(ffmpeg)

    outcome = job.process_item(_item(media), ctx=ctx)

    assert outcome.outcome_key == job.WRITTEN
    stored = _streams(db)
    assert json.loads(stored[11])["ln:loudness"] == "-21.11"
    assert json.loads(stored[12])["ln:loudness"] == "-32.22"
    command = args_file.read_text().splitlines()
    assert command[command.index("-filter_threads") + 1] == "1"
    assert [command[i + 1] for i, arg in enumerate(command) if arg == "-map"] == ["0:1", "0:2"]


def test_worker_does_not_fallback_after_batch_failure_changes_the_source(ctx, db, media):  # noqa: F811
    _add_audio_stream(db, 13, 7)

    def fail_after_change(*_args, **_kwargs):
        Path(media).write_bytes(b"changed before retry")
        raise job.analyze.LoudnessError("batch failed")

    with (
        patch.object(job.analyze, "run_many", side_effect=fail_after_change) as run_many,
        patch.object(job.analyze, "run") as run,
    ):
        outcome = job.process_item(_item(media), ctx=ctx)

    assert run_many.call_args.args == (
        ctx.ffmpeg,
        media,
        [(1, "aac", 424000), (2, "aac", 424000), (7, "aac", 424000)],
    )
    run.assert_not_called()
    assert outcome.outcome_key == job.WAITING
    assert not any(json.loads(value or "{}").get("ln:loudness") for value in _streams(db).values())


def test_worker_checks_source_fingerprint_before_publishing_a_complete_batch(ctx, db, media):  # noqa: F811
    def replace_source(*_args, **_kwargs):
        Path(media).write_bytes(b"changed while batch ran")
        return {1: FIELDS, 2: FIELDS}

    with (
        patch.object(job.analyze, "run_many", side_effect=replace_source) as run_many,
        patch.object(job.analyze, "run") as run,
    ):
        outcome = job.process_item(_item(media), ctx=ctx)

    run.assert_not_called()
    assert run_many.call_args.args == (ctx.ffmpeg, media, [(1, "aac", 424000), (2, "aac", 424000)])
    assert outcome.outcome_key == job.WAITING
    assert not any(json.loads(value or "{}").get("ln:loudness") for value in _streams(db).values())


def test_worker_stops_publishing_a_batch_when_cancelled_between_stream_writes(ctx, db, media):  # noqa: F811
    cancelled = False
    write = job.write_stream

    def write_first_then_cancel(*args, **kwargs):
        nonlocal cancelled
        result = write(*args, **kwargs)
        if args[1] == 11:
            cancelled = True
        return result

    with (
        patch.object(job.analyze, "run_many", return_value={1: FIELDS, 2: FIELDS}),
        patch.object(job, "write_stream", side_effect=write_first_then_cancel),
    ):
        outcome = job.process_item(_item(media), ctx=ctx, cancel_check=lambda: cancelled)

    stored = _streams(db)
    assert "ln:loudness" in stored[11] and stored[12] is None
    assert not job.read_streams(db, [FILE], deadline=1e12)[0][0].item_marked
    assert outcome.outcome_key == job.FAILED and "cancelled" in outcome.message


def test_worker_reports_a_bad_batch_stream_without_rereading_the_file_for_it(ctx, db, media):  # noqa: F811
    results = {1: FIELDS, 2: job.analyze.LoudnessError("ffmpeg omitted the loudnorm report for stream 2")}

    with (
        patch.object(job.analyze, "run_many", return_value=results) as run_many,
        patch.object(job.analyze, "run") as run,
    ):
        outcome = job.process_item(_item(media), ctx=ctx)

    run_many.assert_called_once()
    run.assert_not_called()
    stored = _streams(db)
    assert "ln:loudness" in stored[11] and stored[12] is None
    assert not job.read_streams(db, [FILE], deadline=1e12)[0][0].item_marked
    assert outcome.outcome_key == job.FAILED and "stream 2" in outcome.message


@pytest.mark.parametrize(
    "error",
    [
        job.DatabaseBusyError("Plex database busy"),
        job.SourceChangedError("Plex's indexed source differs from the file"),
        job.PublishError("Plex database is no longer open", state=job.Capability.UNREACHABLE),
    ],
)
def test_worker_stops_after_a_retryable_write_error_without_analysing_the_next_group(ctx, db, media, error):  # noqa: F811
    for stream_id, index in ((13, 3), (14, 5)):
        _add_audio_stream(db, stream_id, index)

    with (
        patch.object(job.analyze, "run_many", return_value={1: FIELDS, 2: FIELDS, 3: FIELDS}) as run_many,
        patch.object(job.analyze, "run", return_value=FIELDS) as run,
        patch.object(job, "write_stream", side_effect=error),
    ):
        outcome = job.process_item(_item(media), ctx=ctx)

    run_many.assert_called_once()
    run.assert_not_called()
    assert outcome.outcome_key == job.WAITING
    assert str(error) in outcome.message
    assert not job.read_streams(db, [FILE], deadline=1e12)[0][0].item_marked


def test_a_remote_worker_measures_on_its_node_and_writes_here(ctx, db, media, monkeypatch):  # noqa: F811
    from media_preview_generator.nodes import protocol, remote_ops
    from media_preview_generator.nodes.executor import Placement, placed_on

    calls = []

    def run_on_node(op, app_path, params, **kwargs):
        calls.append((op, app_path, params, kwargs["gpu"]))
        return {"results": [{"index": index, "fields": FIELDS} for index, _codec, _ms in params["tracks"]]}

    monkeypatch.setattr(remote_ops, "run_on_node", run_on_node)
    _saved_nice_level(monkeypatch, 0)
    with (
        patch.object(job.analyze, "run_many") as run_many,
        patch.object(job.analyze, "run") as run,
        placed_on(Placement("n1", "NAS")),
    ):
        outcome = job.process_item(_item(media), ctx=ctx, gpu="intel")
    run_many.assert_not_called()
    run.assert_not_called()
    assert calls == [
        (
            protocol.OP_LOUDNESS_ANALYZE,
            media,
            {"tracks": [[1, "aac", 424000], [2, "aac", 424000]], "batch": True, "nice_level": 0},
            None,
        )
    ]
    assert outcome.outcome_key == job.WRITTEN
    assert all(json.loads(v)["ln:loudness"] == FIELDS["ln:loudness"] for v in _streams(db).values())


def test_a_streams_own_error_in_a_node_batch_is_kept_as_locally(ctx, db, media, monkeypatch):  # noqa: F811
    from media_preview_generator.nodes import remote_ops
    from media_preview_generator.nodes.executor import Placement, placed_on

    calls = []

    def run_on_node(op, app_path, params, **kwargs):
        calls.append(params)
        return {
            "results": [{"index": 1, "fields": FIELDS}, {"index": 2, "error_type": "LoudnessError", "message": "x"}]
        }

    monkeypatch.setattr(remote_ops, "run_on_node", run_on_node)
    with placed_on(Placement("n1", "NAS")):
        outcome = job.process_item(_item(media), ctx=ctx)
    # A single-track run would fail the same way, so stream 2 isn't run again.
    assert [p["batch"] for p in calls] == [True]
    assert outcome.outcome_key == job.FAILED and "stream 2 (aac): x" in outcome.message


def test_a_failed_node_batch_is_retried_one_track_at_a_time_on_the_node(ctx, db, media, monkeypatch):  # noqa: F811
    from media_preview_generator.nodes import remote_ops
    from media_preview_generator.nodes.executor import NodeTaskError, Placement, placed_on

    calls = []

    def run_on_node(op, app_path, params, **kwargs):
        calls.append(params)
        if params["batch"]:
            raise NodeTaskError("failed", "reports can't be attributed", error_type="LoudnessError")
        return {"fields": FIELDS}

    monkeypatch.setattr(remote_ops, "run_on_node", run_on_node)
    with placed_on(Placement("n1", "NAS")):
        outcome = job.process_item(_item(media), ctx=ctx)
    assert [(p["batch"], p["tracks"]) for p in calls] == [
        (True, [[1, "aac", 424000], [2, "aac", 424000]]),
        (False, [[1, "aac", 424000]]),
        (False, [[2, "aac", 424000]]),
    ]
    assert outcome.outcome_key == job.WRITTEN


def test_a_node_drop_leaves_the_file_unwritten_for_the_worker_to_give_back(ctx, db, media, monkeypatch):  # noqa: F811
    from media_preview_generator.nodes import remote_ops
    from media_preview_generator.nodes.executor import NodeUnavailableError, Placement, placed_on

    def run_on_node(op, app_path, params, **kwargs):
        raise NodeUnavailableError("n1", "NAS", "NAS stopped answering")

    monkeypatch.setattr(remote_ops, "run_on_node", run_on_node)
    before = _streams(db)
    with placed_on(Placement("n1", "NAS")), pytest.raises(NodeUnavailableError):
        job.process_item(_item(media), ctx=ctx)
    assert _streams(db) == before


@pytest.fixture
def health_store(tmp_path, monkeypatch):
    from media_preview_generator.library_health import failures
    from media_preview_generator.library_health.store import HealthStore

    opened = HealthStore(str(tmp_path / "health.db"))
    monkeypatch.setattr(failures, "default_store", lambda: opened)
    return opened


def _loudness_failures(store):
    from media_preview_generator.library_health.models import Feature

    return {path for feature, path in store.failures() if feature == Feature.LOUDNESS}


def test_a_track_ffmpeg_cant_read_records_the_file_for_library_health(ctx, db, media, health_store):  # noqa: F811
    with patch.object(job.analyze, "run", side_effect=job.analyze.LoudnessError("Conversion failed!")):
        outcome = job.process_item(_item(media), ctx=ctx)

    assert outcome.outcome_key == job.FAILED
    assert _loudness_failures(health_store) == {media}


@pytest.mark.parametrize("error", [job.analyze.LoudnessCancelled, job.analyze.LoudnessTimeout])
def test_a_cancelled_or_timed_out_track_is_not_recorded(ctx, db, media, health_store, error):  # noqa: F811
    with patch.object(job.analyze, "run", side_effect=error("stopped")):
        outcome = job.process_item(_item(media), ctx=ctx)

    assert outcome.outcome_key == job.FAILED
    assert _loudness_failures(health_store) == set()


def test_a_track_failure_with_a_busy_write_is_not_recorded(ctx, db, media, monkeypatch, health_store):  # noqa: F811
    def run(ffmpeg, path, index, **kwargs):
        if index == 1:
            raise job.analyze.LoudnessError("corrupt first track")
        return FIELDS

    monkeypatch.setattr(job, "write_stream", MagicMock(side_effect=job.DatabaseBusyError("Plex busy")))
    with patch.object(job.analyze, "run", side_effect=run):
        outcome = job.process_item(_item(media), ctx=ctx)

    assert outcome.outcome_key == job.FAILED and outcome.publisher_rows[0]["retryable"] is True
    assert _loudness_failures(health_store) == set()


def test_a_file_measured_later_clears_its_record(ctx, db, media, health_store):  # noqa: F811
    with patch.object(job.analyze, "run", side_effect=job.analyze.LoudnessError("Conversion failed!")):
        job.process_item(_item(media), ctx=ctx)
    assert _loudness_failures(health_store) == {media}

    with patch.object(job.analyze, "run", return_value=FIELDS):
        outcome = job.process_item(_item(media), ctx=ctx)

    assert outcome.outcome_key == job.WRITTEN
    assert _loudness_failures(health_store) == set()
