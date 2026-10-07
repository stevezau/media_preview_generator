"""Loudness retry lifecycle through real dispatch, publication and persisted job rows."""

from __future__ import annotations

import io
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

from media_preview_generator.jobs.dispatcher import JobDispatcher
from media_preview_generator.jobs.worker import WorkerPool
from media_preview_generator.loudness import job
from media_preview_generator.markers import job_runner as shared_runner
from media_preview_generator.markers.publishers import plex_db as shared_db
from media_preview_generator.output.plex_hash import calculate_plex_hash
from media_preview_generator.servers.base import Library, ServerConfig, ServerType
from media_preview_generator.servers.registry import ServerRegistry
from media_preview_generator.web import jobs
from media_preview_generator.web.job_gate import JobGate
from media_preview_generator.web.jobs import Job, JobManager, JobStatus, is_user_visible_job

from .test_plex_db import FIELDS, FIX, PLEX_ITEM_TRIGGERS


@dataclass
class Lifecycle:
    """Temporary Plex storage; only the Plex/FFmpeg external boundaries are simulated."""

    manager: JobManager
    dispatcher: JobDispatcher
    database: Path
    media: Path
    settings: dict = field(default_factory=lambda: {"webhook_retry_count": 1, "webhook_retry_delay": 10})
    api_ready: bool = False
    verified_paths: set[str] = field(default_factory=set)
    identity_unavailable: bool = False
    corrupt: set[tuple[str, int]] = field(default_factory=set)
    interrupt_publication: set[tuple[str, int]] = field(default_factory=set)
    analyses: list[tuple[str, int]] = field(default_factory=list)

    def add_file(self, name: str, *, tracks: int = 1) -> str:
        path = self.media / name
        path.write_bytes(name.encode())
        with sqlite3.connect(self.database) as conn:
            item_id = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM metadata_items").fetchone()[0]
            conn.execute("INSERT INTO metadata_items (id, metadata_type, title) VALUES (?, 1, ?)", (item_id, name))
            conn.execute(
                "INSERT INTO media_items (id, metadata_item_id, duration) VALUES (?, ?, 1000)", (item_id, item_id)
            )
            conn.execute(
                "INSERT INTO media_parts (id, media_item_id, file, hash, size) VALUES (?, ?, ?, ?, ?)",
                (item_id, item_id, str(path), calculate_plex_hash(path), path.stat().st_size),
            )
            for index in range(1, tracks + 1):
                conn.execute(
                    'INSERT INTO media_streams (id, media_part_id, media_item_id, stream_type_id, "index", codec) '
                    "VALUES (?, ?, ?, 2, ?, 'aac')",
                    (item_id * 10 + index, item_id, item_id, index),
                )
        return str(path)

    def query(self, path: str) -> ET.Element:
        if path == "/identity":
            if self.identity_unavailable:
                raise ConnectionError("Plex temporarily unavailable")
            return ET.Element("MediaContainer", machineIdentifier="lifecycle-plex", version="1.43.4.10903-test")
        item_id = int(path.rsplit("/", 1)[1])
        root = ET.Element("MediaContainer")
        with sqlite3.connect(self.database) as conn:
            for part_id, filename in conn.execute(
                "SELECT mp.id, mp.file FROM media_parts mp JOIN media_items mi ON mp.media_item_id=mi.id "
                "WHERE mi.metadata_item_id=?",
                (item_id,),
            ):
                part = ET.SubElement(root, "Part", id=str(part_id), file=filename)
                for stream_id, index, extra in conn.execute(
                    'SELECT id, "index", extra_data FROM media_streams WHERE media_part_id=?', (part_id,)
                ):
                    attrs = {"id": str(stream_id), "index": str(index), "streamType": "2"}
                    if self.api_ready or filename in self.verified_paths:
                        attrs.update(
                            {
                                key[3:]: value
                                for key, value in shared_db.decode_extra_data(extra)[0].items()
                                if key.startswith("ln:")
                            }
                        )
                        attrs["canNormalizeLoudness"] = "1"
                    ET.SubElement(part, "Stream", **attrs)
        return root

    def popen(self, command: list[str], **kwargs: object) -> SimpleNamespace:
        source = command[command.index("-i") + 1]
        index = int(command[command.index("-map") + 1].split(":")[1])
        target = (source, index)
        self.analyses.append(target)
        if target in self.interrupt_publication:
            self.identity_unavailable = True
        report = {
            "input_i": FIELDS["ln:loudness"],
            "input_tp": FIELDS["ln:peak"],
            "input_lra": FIELDS["ln:lra"],
            "input_thresh": FIELDS["ln:threshold"],
            "target_offset": FIELDS["ln:gainOffset"],
        }
        return SimpleNamespace(
            returncode=1 if target in self.corrupt else 0,
            stdout=io.BytesIO(b""),
            stderr=io.BytesIO(json.dumps(report).encode()),
            wait=lambda **kw: 0,
        )

    def start(self, paths: list[str]) -> Job:
        parent = self.manager.create_job(
            library_name="Loudness lifecycle",
            kind="loudness",
            priority=2,
            config={"kind": "loudness", "source": "manual", "file_paths": paths, "server_id": "plex"},
        )
        job.run_loudness_job(parent.id)
        return parent

    def children(self, parent: Job) -> list[Job]:
        return [entry for entry in self.manager._jobs.values() if entry.config.get("parent_job_id") == parent.id]

    def retry(self, child: Job) -> None:
        self.manager.merge_job_config(child.id, {"retry_not_before": "2000-01-01T00:00:00+00:00"})
        job.run_loudness_job(child.id)

    def rows(self, parent: Job) -> dict[str, dict]:
        return {row["file"]: row for row in self.manager.get_file_results(parent.id, dedup_by_path=True)}


@pytest.fixture
def lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    media = tmp_path / "media"
    media.mkdir()
    folder = tmp_path / "Plex Media Server"
    database = Path(shared_db.plex_db_path(str(folder)))
    database.parent.mkdir(parents=True)
    (folder / "Preferences.xml").write_text('<Preferences ProcessedMachineIdentifier="lifecycle-plex"/>')
    with sqlite3.connect(database) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript((FIX / "plex_schema_1_43.sql").read_text() + PLEX_ITEM_TRIGGERS)
    manager = JobManager(config_dir=str(tmp_path))
    dispatcher = JobDispatcher(WorkerPool(cpu_workers=1, gpu_workers=0, selected_gpus=[]))
    env = Lifecycle(manager, dispatcher, database, media)
    config = SimpleNamespace(
        cpu_threads=1,
        gpu_threads=0,
        scan_workers=1,
        ffmpeg_path="ffmpeg",
        regenerate_thumbnails=False,
        server_id_filter=None,
    )
    cfg = ServerConfig(
        id="plex",
        name="Plex",
        type=ServerType.PLEX,
        enabled=True,
        url="http://plex.invalid",
        auth={},
        libraries=[Library(id="1", name="Movies", remote_paths=(str(media),), kind="movie", enabled=True)],
        output={"plex_config_folder": str(folder)},
        loudness={"enabled": True, "library_ids": None},
    )
    registry = ServerRegistry()
    registry._configs = {cfg.id: cfg}
    registry._servers = {cfg.id: SimpleNamespace(_connect=lambda: SimpleNamespace(query=env.query))}
    settings = SimpleNamespace(processing_paused=False, get=lambda key, default=None: env.settings.get(key, default))
    gate = JobGate(lambda: 1)
    for module in (job, shared_runner):
        monkeypatch.setattr(module, "get_job_manager", lambda: manager)
        monkeypatch.setattr(module, "get_settings_manager", lambda: settings)
        monkeypatch.setattr(module, "get_job_gate", lambda: gate)
    monkeypatch.setattr(jobs, "get_job_manager", lambda: manager)
    monkeypatch.setattr(job, "load_config", lambda: config)
    monkeypatch.setattr(job, "_build_multi_server_registry", lambda config: registry)
    monkeypatch.setattr(job, "_ensure_gpu_cache", lambda: [])
    monkeypatch.setattr(job, "_build_selected_gpus", lambda *args, **kwargs: [])
    monkeypatch.setattr(job, "get_or_create_dispatcher", lambda *args: dispatcher)
    # Persist the real retry job, but advance its stored due time and run its runner explicitly in each test.
    monkeypatch.setattr(job, "start_loudness_job_async", lambda *args, **kwargs: None)
    monkeypatch.setattr(shared_db, "shm_lock_held_elsewhere", lambda *args, **kwargs: True)
    monkeypatch.setattr(job.analyze.subprocess, "Popen", env.popen)
    try:
        yield env
    finally:
        dispatcher.shutdown()
        manager.close()


def test_unverified_publication_keeps_original_pending_with_hidden_retry(lifecycle: Lifecycle) -> None:
    path = lifecycle.add_file("waiting.mkv")
    parent = lifecycle.start([path])
    assert lifecycle.rows(parent)[path]["outcome"] == job.WAITING
    assert parent.status is JobStatus.PENDING
    assert parent.completed_at is None
    assert parent.config["is_retry_chain"] is True
    assert parent.config["last_outcome"] == "scheduled"
    assert parent.progress.retry_eta
    (child,) = lifecycle.children(parent)
    assert child.kind == "loudness"
    assert child.config["is_retry"] is True
    assert child.config["server_id"] == "plex"
    assert child.config["file_paths"] == [path]
    assert child.config["retry_attempt"] == 1
    assert child.config["max_retries"] == 1
    assert not is_user_visible_job(child)


def test_exhausted_unverified_chain_with_no_success_fails(lifecycle: Lifecycle) -> None:
    path = lifecycle.add_file("waiting.mkv")
    parent = lifecycle.start([path])
    (child,) = lifecycle.children(parent)
    lifecycle.retry(child)
    assert parent.status is JobStatus.FAILED
    assert parent.error
    assert parent.config["last_outcome"] == "exhausted"
    assert parent.progress.outcome[job.WAITING] == 1
    assert len(lifecycle.children(parent)) == 1
    assert lifecycle.analyses == [(path, 1)]


def test_verified_retry_replaces_parent_file_and_outcome_counts(lifecycle: Lifecycle) -> None:
    path = lifecycle.add_file("waiting.mkv")
    parent = lifecycle.start([path])
    (child,) = lifecycle.children(parent)
    lifecycle.api_ready = True
    lifecycle.retry(child)
    assert parent.status is JobStatus.COMPLETED
    assert parent.error is None
    assert parent.config["last_outcome"] == "completed"
    assert lifecycle.rows(parent)[path]["outcome"] == job.UP_TO_DATE
    assert parent.progress.outcome.get(job.WAITING, 0) == 0
    assert parent.progress.outcome[job.UP_TO_DATE] == 1
    assert lifecycle.analyses == [(path, 1)]
    assert parent.publishers[0]["counts"] == {job.UP_TO_DATE: 1}


def test_successful_retry_updates_counts_when_initial_files_panel_was_capped(lifecycle: Lifecycle) -> None:
    lifecycle.manager._FILE_RESULTS_PER_OUTCOME_CAP = 1
    paths = [lifecycle.add_file("first.mkv"), lifecycle.add_file("second.mkv")]
    parent = lifecycle.start(paths)
    (child,) = lifecycle.children(parent)
    assert parent.progress.outcome[job.WAITING] == 2
    lifecycle.api_ready = True
    lifecycle.retry(child)
    assert parent.status is JobStatus.COMPLETED
    assert parent.error is None
    assert parent.progress.outcome.get(job.WAITING, 0) == 0
    assert parent.progress.outcome[job.UP_TO_DATE] == 2


def test_retry_preserves_success_totals_omitted_by_files_panel_cap(lifecycle: Lifecycle) -> None:
    lifecycle.manager._FILE_RESULTS_PER_OUTCOME_CAP = 1
    completed = [lifecycle.add_file("first.mkv"), lifecycle.add_file("second.mkv")]
    waiting = lifecycle.add_file("waiting.mkv")
    lifecycle.verified_paths.update(completed)
    parent = lifecycle.start([*completed, waiting])
    assert parent.progress.outcome[job.WRITTEN] == 2
    (child,) = lifecycle.children(parent)
    assert child.config["file_paths"] == [waiting]
    lifecycle.api_ready = True
    lifecycle.retry(child)
    assert parent.status is JobStatus.COMPLETED
    assert parent.error is None
    assert parent.progress.outcome == {job.WRITTEN: 2, job.UP_TO_DATE: 1}
    assert parent.publishers[0]["counts"] == {job.WRITTEN: 2, job.UP_TO_DATE: 1}


def test_permanent_failure_survives_other_files_successful_retry(lifecycle: Lifecycle) -> None:
    bad = lifecycle.add_file("bad.mkv")
    waiting = lifecycle.add_file("waiting.mkv")
    lifecycle.corrupt.add((bad, 1))
    parent = lifecycle.start([bad, waiting])
    assert parent.status is JobStatus.PENDING
    (child,) = lifecycle.children(parent)
    assert child.config["file_paths"] == [waiting]
    lifecycle.api_ready = True
    lifecycle.retry(child)
    assert parent.status is JobStatus.COMPLETED
    assert parent.error
    assert lifecycle.rows(parent)[bad]["outcome"] == job.FAILED
    assert lifecycle.rows(parent)[waiting]["outcome"] == job.UP_TO_DATE
    assert parent.progress.outcome[job.FAILED] == 1
    assert parent.progress.outcome[job.UP_TO_DATE] == 1


def test_same_file_failure_keeps_transient_publisher_flag_in_storage(lifecycle: Lifecycle) -> None:
    path = lifecycle.add_file("mixed.mkv", tracks=2)
    lifecycle.corrupt.add((path, 1))
    lifecycle.interrupt_publication.add((path, 2))
    parent = lifecycle.start([path])
    row = lifecycle.rows(parent)[path]
    assert row["outcome"] == job.FAILED
    assert row["servers"][0]["retryable"] is True
    assert parent.status is JobStatus.PENDING
    (child,) = lifecycle.children(parent)
    assert child.config["file_paths"] == [path]
    lifecycle.identity_unavailable = False
    lifecycle.interrupt_publication.clear()
    lifecycle.api_ready = True
    lifecycle.retry(child)
    assert parent.status is JobStatus.FAILED
    assert parent.error
    assert len(lifecycle.children(parent)) == 1


def test_global_retry_off_fails_unverified_work_without_creating_retry(lifecycle: Lifecycle) -> None:
    lifecycle.settings["webhook_retry_count"] = 0
    path = lifecycle.add_file("waiting.mkv")
    parent = lifecycle.start([path])
    assert parent.status is JobStatus.FAILED
    assert parent.error
    assert lifecycle.children(parent) == []


def test_cancelling_parent_cancels_child_and_prevents_late_resurrection(lifecycle: Lifecycle) -> None:
    path = lifecycle.add_file("waiting.mkv")
    parent = lifecycle.start([path])
    (child,) = lifecycle.children(parent)
    lifecycle.manager.cancel_job(parent.id)
    assert parent.status is JobStatus.CANCELLED
    assert child.status is JobStatus.CANCELLED
    lifecycle.api_ready = True
    lifecycle.retry(child)
    assert parent.status is JobStatus.CANCELLED
    assert child.status is JobStatus.CANCELLED
    assert lifecycle.rows(parent)[path]["outcome"] == job.WAITING
    assert len(lifecycle.children(parent)) == 1


def test_pending_retry_is_revived_without_restarting_its_chain_head(
    lifecycle: Lifecycle, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = lifecycle.add_file("waiting.mkv")
    parent = lifecycle.start([path])
    (child,) = lifecycle.children(parent)
    lifecycle.manager.close()
    restored = JobManager(config_dir=lifecycle.manager.config_dir)
    try:
        revived = restored.requeue_interrupted_jobs()
        assert [entry.id for entry in revived] == [child.id]
        assert [entry.id for entry in restored.interrupted_retry_chains()] == [parent.id]
        lifecycle.manager = restored
        for module in (job, shared_runner, jobs):
            monkeypatch.setattr(module, "get_job_manager", lambda: restored)
        lifecycle.api_ready = True
        lifecycle.retry(restored.get_job(child.id))
        finished = restored.get_job(parent.id)
        assert finished.status is JobStatus.COMPLETED
        assert finished.error is None
        assert finished.progress.outcome[job.UP_TO_DATE] == 1
        assert lifecycle.analyses == [(path, 1)]
    finally:
        restored.close()


def test_retry_failure_before_listing_closes_parent_chain(
    lifecycle: Lifecycle, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = lifecycle.add_file("waiting.mkv")
    parent = lifecycle.start([path])
    (child,) = lifecycle.children(parent)
    monkeypatch.setattr(job, "_build_multi_server_registry", lambda config: None)
    lifecycle.retry(child)
    assert child.status is JobStatus.FAILED
    assert parent.status is JobStatus.FAILED
    assert "media servers configuration" in parent.error
    assert parent.progress.retry_eta is None


def test_retry_with_missing_parent_stops_before_processing(lifecycle: Lifecycle) -> None:
    path = lifecycle.add_file("waiting.mkv")
    parent = lifecycle.start([path])
    (child,) = lifecycle.children(parent)
    # An orphaned persisted attempt must not publish or create a replacement chain head.
    lifecycle.manager._jobs.pop(parent.id)
    lifecycle.manager._persist_delete(parent.id)
    lifecycle.api_ready = True
    lifecycle.retry(child)
    assert child.status is JobStatus.CANCELLED
    assert lifecycle.manager.get_job(parent.id) is None
    assert lifecycle.rows(child) == {}
    assert lifecycle.analyses == [(path, 1)]


def test_retry_keeps_sender_path_for_mapping_on_next_attempt(lifecycle: Lifecycle) -> None:
    path = lifecycle.add_file("waiting.mkv")
    sender = "/sender/waiting.mkv"
    cfg = job._build_multi_server_registry(None).get_config("plex")
    cfg.path_mappings = [{"remote_prefix": "/sender", "local_prefix": str(lifecycle.media)}]
    cfg.libraries = [Library(id="1", name="Movies", remote_paths=("/sender",), kind="movie", enabled=True)]
    with sqlite3.connect(lifecycle.database) as conn:
        conn.execute("UPDATE media_parts SET file=? WHERE file=?", (sender, path))
    parent = lifecycle.start([sender])
    assert lifecycle.rows(parent)[path]["outcome"] == job.WAITING
    (child,) = lifecycle.children(parent)
    assert child.config["file_paths"] == [sender]
    lifecycle.api_ready = True
    lifecycle.retry(child)
    assert parent.status is JobStatus.COMPLETED
    assert parent.error is None
    assert lifecycle.rows(parent)[path]["outcome"] == job.UP_TO_DATE


def test_large_remapped_retry_survives_restart_without_omitting_waiting_files(
    lifecycle: Lifecycle, monkeypatch: pytest.MonkeyPatch
) -> None:
    from media_preview_generator.loudness.inputs import load_file_input

    paths = [lifecycle.add_file(f"waiting-{index:03}.mkv") for index in range(501)]
    senders = [f"/sender/{Path(path).name}" for path in paths]
    cfg = job._build_multi_server_registry(None).get_config("plex")
    cfg.path_mappings = [{"remote_prefix": "/sender", "local_prefix": str(lifecycle.media)}]
    cfg.libraries = [Library(id="1", name="Movies", remote_paths=("/sender",), kind="movie", enabled=True)]
    with sqlite3.connect(lifecycle.database) as conn:
        conn.executemany("UPDATE media_parts SET file=? WHERE file=?", zip(senders, paths, strict=True))
    parent = lifecycle.start(senders)
    (child,) = lifecycle.children(parent)
    assert parent.progress.outcome[job.WAITING] == len(paths)
    expanded = load_file_input(lifecycle.manager.config_dir, child.config)
    assert expanded["file_paths"] == senders
    assert len(expanded["retry_baseline"]["files"]) == len(paths)
    assert len(json.dumps(child.config)) < 4096

    # Resume from disk with a changed local mount, preserving original file identities.
    relocated = lifecycle.media.with_name("relocated")
    lifecycle.media.rename(relocated)
    cfg.path_mappings = [{"remote_prefix": "/sender", "local_prefix": str(relocated)}]
    lifecycle.manager.close()
    restored = JobManager(config_dir=lifecycle.manager.config_dir)
    lifecycle.manager = restored
    for module in (job, shared_runner, jobs):
        monkeypatch.setattr(module, "get_job_manager", lambda: restored)
    lifecycle.api_ready = True
    child = restored.get_job(child.id)
    lifecycle.retry(child)
    finished = restored.get_job(parent.id)
    assert finished.status is JobStatus.COMPLETED
    assert finished.error is None
    assert finished.progress.outcome == {job.UP_TO_DATE: len(paths)}
    assert finished.publishers[0]["counts"] == {job.UP_TO_DATE: len(paths)}
    assert len(lifecycle.analyses) == len(paths)
    assert set(lifecycle.rows(child)) == set(paths)
    assert "retry_sender_paths" not in child.config
    assert len(json.dumps(child.config)) < 4096
    expanded = load_file_input(restored.config_dir, child.config)
    for _ in range(2):
        assert job._recount_chain(restored, parent.id, expanded, child.id) == {job.UP_TO_DATE: len(paths)}


@pytest.mark.parametrize("restart", [False, True], ids=["same-process", "restart"])
def test_capacity_closure_parks_same_job_and_resumes_only_unfinished_work(lifecycle, monkeypatch, restart):
    import threading
    import time

    from media_preview_generator.jobs import group_runtime

    groups = [
        {
            "id": "cpu",
            "name": "Loudness",
            "resource": "cpu",
            "device": None,
            "count": 1,
            "enabled": True,
            "job_types": ["loudness"],
            "availability": {"mode": "always"},
        }
    ]
    monkeypatch.setattr(group_runtime, "current_groups", lambda config=None: groups)
    monkeypatch.setattr(group_runtime, "current_group_policy", lambda config=None: (groups, None))
    monkeypatch.setattr(
        group_runtime, "runtime_capacity", lambda kind: group_runtime.capacity_for_groups(groups, [], kind)
    )
    monkeypatch.setattr(job, "runtime_capacity", group_runtime.runtime_capacity)
    lifecycle.dispatcher.worker_pool.reconcile_groups(groups, [])
    lifecycle.api_ready = True
    paths = [lifecycle.add_file("first.mkv"), lifecycle.add_file("second.mkv")]
    original = lifecycle.popen

    def analyze(command, **kwargs):
        answer = original(command, **kwargs)
        if len(lifecycle.analyses) == 1:
            groups[0]["enabled"] = False
            lifecycle.dispatcher.worker_pool.reconcile_groups(groups, [])
            group_runtime.wake_group_runtime()
        return answer

    monkeypatch.setattr(job.analyze.subprocess, "Popen", analyze)
    parent = lifecycle.manager.create_job(
        kind="loudness", config={"kind": "loudness", "source": "manual", "file_paths": paths}
    )
    runner = threading.Thread(target=job._run_loudness_pass if restart else job.run_loudness_job, args=(parent.id,))
    runner.start()
    try:
        deadline = time.monotonic() + 8
        while not parent.config.get("parked_checkpoint") and time.monotonic() < deadline:
            time.sleep(0.02)
        assert parent.config.get("parked_checkpoint"), parent.to_dict()
        assert parent.status is JobStatus.PENDING
        assert parent.progress.processed_items == 1
        assert lifecycle.manager.get_running_jobs() == []
        assert parent.id not in lifecycle.dispatcher._trackers
        assert len(lifecycle.analyses) == 1
        assert lifecycle.children(parent) == []
        if restart:
            runner.join(3)
            assert not runner.is_alive()
            old_manager = lifecycle.manager
            old_manager.close()
            lifecycle.manager = JobManager(old_manager.config_dir)
            for module in (job, shared_runner, jobs):
                monkeypatch.setattr(module, "get_job_manager", lambda: lifecycle.manager)
            assert [item.id for item in lifecycle.manager.requeue_interrupted_jobs()] == [parent.id]
            parent = lifecycle.manager.get_job(parent.id)
        groups[0]["enabled"] = True
        group_runtime.wake_group_runtime()
        if restart:
            runner = threading.Thread(target=job.run_loudness_job, args=(parent.id,))
            runner.start()
        runner.join(8)
        assert not runner.is_alive()
        assert parent.status is JobStatus.COMPLETED, parent.error
        assert {key: value for key, value in parent.progress.outcome.items() if value} == {job.WRITTEN: 2}
        assert sorted(lifecycle.analyses) == [(path, 1) for path in sorted(paths)]
        assert "parked_checkpoint" not in parent.config and "resource_wait" not in parent.config
    finally:
        lifecycle.manager.request_cancellation(parent.id)
        group_runtime.wake_group_runtime()
        runner.join(3)
