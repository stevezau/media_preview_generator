"""Fixtures shared by the loudness tests: the Flask app for the API tests and the retry-lifecycle harness."""

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
from media_preview_generator.web.jobs import Job, JobManager
from tests.markers.conftest import app, client  # noqa: F401 - fixtures

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
        indices = [int(command[index + 1].split(":")[1]) for index, arg in enumerate(command) if arg == "-map"]
        targets = [(source, index) for index in indices]
        self.analyses.extend(targets)
        if any(target in self.interrupt_publication for target in targets):
            self.identity_unavailable = True
        report = {
            "input_i": FIELDS["ln:loudness"],
            "input_tp": FIELDS["ln:peak"],
            "input_lra": FIELDS["ln:lra"],
            "input_thresh": FIELDS["ln:threshold"],
            "target_offset": FIELDS["ln:gainOffset"],
        }
        stderr = "\n".join(
            f"[loudnorm@track{index} @ 0x1234] {json.dumps(report)}" if len(indices) > 1 else json.dumps(report)
            for index in indices
        )
        return SimpleNamespace(
            returncode=1 if any(target in self.corrupt for target in targets) else 0,
            stdout=io.BytesIO(b""),
            stderr=io.BytesIO(stderr.encode()),
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
    gate = JobGate()
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
