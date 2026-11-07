"""Issue #419: files that need a full decode (keyframes too far apart for the thumbnail interval) are counted per job.

Covers the generator's report, the node round trip, the worker/tracker tally and the job record. The modal note itself
is covered in ``tests/e2e/test_dashboard_modals.py``.
"""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import MagicMock, mock_open, patch

import pytest

from media_preview_generator.jobs.dispatcher import JobDispatcher, JobTracker
from media_preview_generator.jobs.worker import Worker, WorkerPool
from media_preview_generator.processing.generator import generate_images
from media_preview_generator.processing.multi_server import (
    MultiServerStatus,
    PublisherResult,
    PublisherStatus,
    process_canonical_path,
)
from media_preview_generator.servers import Library, ServerRegistry, ServerType
from media_preview_generator.web.jobs import JobManager, JobStorage
from tests.conftest import _ms, _pi
from tests.test_processing_keyframe_probe import _make_integration_config, _wire_integration_mocks
from tests.test_processing_multi_server import _populate_frames, _seed_canonical_file, _server_config

GENERATOR = "media_preview_generator.processing.generator"


def _generate(
    tmp_path,
    *,
    probe_stdout: str,
    interval: int,
    has_duplicates: bool = False,
    duration_ms: int | None = None,
    image_count: int | None = None,
) -> list[int]:
    """Run ``generate_images`` against the boundary mocks; returns one entry per slow-path report."""
    reports: list[int] = []
    mocks: dict[str, MagicMock] = {}
    with ExitStack() as stack:
        for key, target, kwargs in [
            ("mediainfo", f"{GENERATOR}.MediaInfo", {}),
            ("popen", "subprocess.Popen", {}),
            ("run", f"{GENERATOR}.subprocess.run", {}),
            ("rename", f"{GENERATOR}.os.rename", {}),
            ("remove", f"{GENERATOR}.os.remove", {}),
            ("exists", "os.path.exists", {}),
            ("file", "builtins.open", {"new_callable": mock_open}),
            ("sleep", "time.sleep", {}),
            ("glob", f"{GENERATOR}.glob.glob", {}),
            ("detect", f"{GENERATOR}._detect_codec_error", {}),
            ("dupes", f"{GENERATOR}._has_duplicate_thumbnails", {}),
        ]:
            mocks[key] = stack.enter_context(patch(target, **kwargs))
        mocks["dupes"].return_value = has_duplicates
        config = _make_integration_config(tmp_path)
        config.plex_bif_frame_interval = interval
        _wire_integration_mocks(
            mock_run=mocks["run"],
            mock_popen=mocks["popen"],
            mock_mediainfo=mocks["mediainfo"],
            mock_exists=mocks["exists"],
            mock_file=mocks["file"],
            mock_detect=mocks["detect"],
            mock_glob=mocks["glob"],
            probe_stdout=probe_stdout,
            temp_dir=str(tmp_path),
            duration_ms=duration_ms,
            image_count=image_count,
        )
        generate_images(
            "/test/film.mkv", str(tmp_path), None, None, config, slow_path_callback=lambda: reports.append(interval)
        )
    return reports


class TestGeneratorReportsTheSlowPath:
    def test_reports_when_the_upfront_check_finds_keyframes_too_far_apart(self, tmp_path):
        assert _generate(tmp_path, probe_stdout="0.000000,K_\n10.000000,K_\n", interval=6) == [6]

    def test_is_silent_when_the_frame_layout_cannot_be_read(self, tmp_path):
        """That file is fully decoded too, but a longer interval wouldn't help it, so the note mustn't count it."""
        assert _generate(tmp_path, probe_stdout="", interval=6) == []

    def test_reports_when_the_fast_pass_left_duplicate_thumbnails(self, tmp_path):
        reports = _generate(
            tmp_path, probe_stdout="0.000000,K_\n1.000000,K_\n2.000000,K_\n", interval=2, has_duplicates=True
        )
        assert reports == [2]

    @pytest.mark.parametrize("has_duplicates", [False, True])
    def test_is_silent_when_the_fast_pass_came_up_short(self, tmp_path, has_duplicates):
        """Issue #445: one thumbnail for a 10-min film. It is fully decoded, but its snapshot frames can't be decoded
        alone, so a longer interval wouldn't help it and the note mustn't count it, duplicates or not."""
        reports = _generate(
            tmp_path,
            probe_stdout="0.000000,K_\n1.000000,K_\n2.000000,K_\n",
            interval=2,
            has_duplicates=has_duplicates,
            duration_ms=600_000,
            image_count=1,
        )
        assert reports == []

    def test_is_silent_when_the_fast_path_holds(self, tmp_path):
        assert _generate(tmp_path, probe_stdout="0.000000,K_\n1.000000,K_\n2.000000,K_\n", interval=2) == []

    def test_is_silent_inside_the_keyframe_tolerance(self, tmp_path):
        assert _generate(tmp_path, probe_stdout="0.000000,K_\n10.400000,K_\n", interval=10) == []


@pytest.fixture
def config(mock_config, tmp_path):
    mock_config.working_tmp_folder = str(tmp_path / "tmp")
    mock_config.plex_bif_frame_interval = 6
    return mock_config


def _registry(tmp_path, *, with_plex: bool = False) -> ServerRegistry:
    movies = str(tmp_path / "data" / "movies")
    libraries = [Library(id="1", name="Movies", remote_paths=(movies,), enabled=True)]
    configs = [
        _server_config(
            server_id="emby-1",
            server_type=ServerType.EMBY,
            libraries=libraries,
            output={"adapter": "emby_sidecar", "width": 320, "frame_interval": 10},
        )
    ]
    if with_plex:
        configs.append(
            _server_config(
                server_id="plex-1",
                server_type=ServerType.PLEX,
                libraries=libraries,
                output={"adapter": "plex_bundle", "plex_config_folder": str(tmp_path / "plex")},
            )
        )
    return ServerRegistry.from_settings(configs)


class TestResultCarriesTheInterval:
    @pytest.mark.parametrize("slow", [True, False])
    def test_local_extraction(self, config, tmp_path, slow):
        media_file = _seed_canonical_file(tmp_path / "data" / "movies" / "Test (2024)")

        def extract(video_file, output_folder, *args, slow_path_callback=None, **kwargs):
            _populate_frames(output_folder, count=3)
            if slow:
                slow_path_callback()
                slow_path_callback()  # a post-extract rerun after the upfront check: still one file
            return (True, 3, False, 1.0, "1.0x", None)

        with patch("media_preview_generator.processing.multi_server.generate_images", side_effect=extract):
            result = process_canonical_path(
                canonical_path=str(media_file), registry=_registry(tmp_path, with_plex=True), config=config
            )

        assert result.status is MultiServerStatus.PUBLISHED
        assert len(result.publishers) == 2
        assert result.slow_path_interval == (6 if slow else None)

    @pytest.mark.parametrize(
        ("node_fields", "expected"),
        [({"slow_path": True}, 6), ({"slow_path": False}, None), ({}, None)],
        ids=["slow", "fast", "older-node-without-the-field"],
    )
    def test_node_extraction(self, config, tmp_path, node_fields, expected):
        from media_preview_generator.nodes import remote_ops
        from media_preview_generator.nodes.executor import Placement, placed_on

        media_file = _seed_canonical_file(tmp_path / "data" / "movies" / "Test (2024)")

        def run_on_node(op, app_path, params, *, gpu, artifact_to, **kwargs):
            _populate_frames(artifact_to, count=5)
            return {"generate_images": [True, 5, False, 1.0, "1.0x", None], "failures": [], **node_fields}

        with (
            patch.object(remote_ops, "run_on_node", side_effect=run_on_node),
            placed_on(Placement("n1", "NAS")),
        ):
            result = process_canonical_path(canonical_path=str(media_file), registry=_registry(tmp_path), config=config)

        assert result.status is MultiServerStatus.PUBLISHED
        assert result.slow_path_interval == expected

    def test_the_node_reports_its_own_slow_path(self, tmp_path):
        from media_preview_generator.nodes.server import ops

        ctx = MagicMock(temp_dir=str(tmp_path), gpu=None)
        params = {
            "frame_interval": 6,
            "tonemap_algorithm": "hable",
            "thumbnail_quality": 4,
            "ffmpeg_threads": 2,
        }

        def fake_generate(*args, slow_path_callback, **kwargs):
            slow_path_callback()
            return (True, 0, False, 1.0, "1.0x", None)

        with (
            patch(f"{GENERATOR}.generate_images", side_effect=fake_generate),
            patch.object(ops, "_node_ffmpeg", return_value="/usr/bin/ffmpeg"),
        ):
            assert ops.preview_frames(ctx, "/media/film.mkv", params)["slow_path"] is True


def _published(frame_source: str, slow_path_interval: int | None, status=PublisherStatus.PUBLISHED, servers=2):
    result = _ms("generated")
    result.publishers = [
        PublisherResult(f"srv-{n}", f"Server {n}", "bif", status, frame_source=frame_source) for n in range(servers)
    ]
    result.slow_path_interval = slow_path_interval
    return result


def _run_file(worker: Worker, result) -> None:
    with patch("media_preview_generator.processing.multi_server.process_canonical_path", return_value=result):
        worker.assign_task(_pi("f", title="Film"), MagicMock(cpu_threads=0), MagicMock())
        worker.current_thread.join(timeout=5)
    worker.check_completion()


class TestJobTally:
    def test_each_file_counts_once_however_many_servers_it_published_to(self):
        pool = WorkerPool(gpu_workers=0, cpu_workers=1, selected_gpus=[])
        worker = pool.workers[0]
        dispatcher = JobDispatcher(pool)
        config = MagicMock(cpu_threads=1, gpu_threads=0, scan_workers=1, regenerate_thumbnails=False)
        tracker = JobTracker(
            "job-1", [_pi("a"), _pi("b")], config, MagicMock(), callbacks={"progress_callback": MagicMock()}
        )
        dispatcher._trackers[tracker.job_id] = tracker

        _run_file(worker, _published("extracted", 6, servers=3))
        assert worker.last_task_slow_path_interval == 6
        dispatcher._merge_worker_outcome(worker, tracker)
        _run_file(worker, _published("extracted", None))
        assert worker.last_task_slow_path_interval is None
        dispatcher._merge_worker_outcome(worker, tracker)

        assert (tracker.slow_path_files, tracker.slow_path_interval) == (1, 6)

    @pytest.mark.parametrize(
        ("frame_source", "status"),
        [
            ("cache_hit", PublisherStatus.PUBLISHED),
            ("output_existed", PublisherStatus.SKIPPED_OUTPUT_EXISTS),
        ],
    )
    def test_files_that_used_no_fresh_extraction_never_count(self, frame_source, status):
        worker = WorkerPool(gpu_workers=0, cpu_workers=1, selected_gpus=[]).workers[0]
        _run_file(worker, _published(frame_source, 6, status))
        assert worker.last_task_slow_path_interval is None

    def test_the_tally_is_checkpointed_and_restored(self):
        config = MagicMock(cpu_threads=1, gpu_threads=0, scan_workers=1, regenerate_thumbnails=False)
        state = {"successful": 2, "slow_path_files": 4, "slow_path_interval": 6}
        tracker = JobTracker(
            "job-1",
            [_pi("a")],
            config,
            MagicMock(),
            callbacks={"progress_callback": MagicMock()},
            carried_state=state,
        )
        assert (tracker.slow_path_files, tracker.slow_path_interval) == (4, 6)


class TestJobRecord:
    def test_the_tally_round_trips_through_storage(self, tmp_path):
        manager = JobManager(config_dir=str(tmp_path))
        job = manager.create_job(library_name="Movies")
        manager.set_job_slow_path_files(job.id, 3, 6)
        manager._persist_job(manager._jobs[job.id])

        (reloaded,) = [j for j in JobStorage(str(tmp_path / "jobs.db")).all_jobs() if j.id == job.id]
        assert (reloaded.progress.slow_path_files, reloaded.progress.slow_path_interval) == (3, 6)
        assert manager.set_job_slow_path_files("nope", 1, 6) is None

    def test_a_job_saved_before_the_tally_existed_loads_with_none(self, tmp_path):
        import json
        import sqlite3

        manager = JobManager(config_dir=str(tmp_path))
        job = manager.create_job(library_name="Movies")
        manager._persist_job(manager._jobs[job.id])
        db = tmp_path / "jobs.db"
        with sqlite3.connect(db) as conn:
            (raw,) = conn.execute("SELECT progress_json FROM jobs WHERE id = ?", (job.id,)).fetchone()
            progress = json.loads(raw)
            progress.pop("slow_path_files")
            progress.pop("slow_path_interval")
            conn.execute("UPDATE jobs SET progress_json = ? WHERE id = ?", (json.dumps(progress), job.id))

        (reloaded,) = [j for j in JobStorage(str(db)).all_jobs() if j.id == job.id]
        assert (reloaded.progress.slow_path_files, reloaded.progress.slow_path_interval) == (0, 0)


class TestCheckpointAccounting:
    @staticmethod
    def _snapshot(**extra) -> dict:
        state = {"successful": 2, "failed": 0, "cpu_fallback_files": 0, "total_items": 5, **extra}
        return {"state": state, "items": [{}, {}, {}]}

    def test_a_checkpoint_from_before_the_tally_is_accepted(self):
        from media_preview_generator.jobs.checkpoints import _validate_accounting

        _validate_accounting(self._snapshot())

    @pytest.mark.parametrize("bad", [{"slow_path_files": -1}, {"slow_path_interval": "6"}, {"slow_path_files": 1.5}])
    def test_a_corrupt_tally_is_rejected(self, bad):
        from media_preview_generator.jobs.checkpoints import _validate_accounting

        with pytest.raises(ValueError):
            _validate_accounting(self._snapshot(**bad))
