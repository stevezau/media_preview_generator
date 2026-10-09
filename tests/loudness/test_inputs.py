"""Large loudness selections stay exact and durable without inflating job updates."""

import json
import sqlite3
from pathlib import Path

import pytest

from media_preview_generator.loudness import inputs, job
from media_preview_generator.web.jobs import RETRY_STATE_CONFIG_KEYS, JobManager, JobStatus

from .conftest import Lifecycle


def _selection() -> list[str]:
    return [f"/media/TV/Show/Season 01/episode-{number}.mkv" for number in range(1001)]


def test_large_job_keeps_exact_selection_after_restart_and_small_progress_events(tmp_path, monkeypatch):
    manager = JobManager(config_dir=str(tmp_path))
    events = []
    monkeypatch.setattr(manager, "_emit_event", lambda name, data: events.append((name, json.dumps(data))))
    paths = _selection()
    created = manager.create_job(kind="loudness", config={"file_paths": paths, "server_id": "plex"})
    manager.set_publishers(created.id, [{"server_id": "plex", "counts": {job.UP_TO_DATE: 1}}])

    assert created.config["file_paths"] == []
    assert created.config["file_paths_count"] == len(paths)
    assert inputs.read_file_paths(tmp_path, created.config) == paths
    assert all(len(payload) < 4096 for _, payload in events)
    assert {name for name, _ in events} >= {"job_created", "job_updated"}

    reloaded = JobManager(config_dir=str(tmp_path))
    restored = reloaded.get_job(created.id)
    assert restored.config["server_id"] == "plex"
    assert inputs.read_file_paths(tmp_path, restored.config) == paths


def test_small_selection_keeps_legacy_inline_format(tmp_path):
    paths = _selection()[:500]
    manager = JobManager(config_dir=str(tmp_path))
    created = manager.create_job(kind="loudness", config={"file_paths": paths})
    assert created.config == {"file_paths": paths}
    assert inputs.read_file_paths(tmp_path, created.config) == paths
    assert not (tmp_path / "loudness_inputs").exists()


def test_large_retry_keeps_accounting_out_of_updates_and_out_of_reruns(tmp_path):
    manager = JobManager(config_dir=str(tmp_path))
    paths = _selection()
    baseline = {
        "outcome": {job.WAITING: len(paths)},
        "publishers": [],
        "files": {path: {"file": path, "outcome": job.WAITING, "servers": []} for path in paths},
    }
    created = manager.create_job(kind="loudness", config={"file_paths": paths, "retry_baseline": baseline})

    assert "retry_baseline" not in created.config
    assert created.config["retry_baseline_in_input"] is True
    assert len(json.dumps(created.to_dict())) < 4096
    expanded = inputs.load_file_input(tmp_path, created.config)
    assert expanded["file_paths"] == paths
    assert expanded["retry_baseline"] == baseline

    rerun = {key: value for key, value in created.config.items() if key not in RETRY_STATE_CONFIG_KEYS}
    assert inputs.read_file_paths(tmp_path, rerun) == paths
    assert "retry_baseline" not in inputs.load_file_input(tmp_path, rerun)


@pytest.mark.parametrize("removal", ["delete", "clear", "retention"])
def test_shared_input_survives_original_deletion_until_last_clone_is_removed(tmp_path, removal):
    manager = JobManager(config_dir=str(tmp_path))
    original = manager.create_job(kind="loudness", config={"file_paths": _selection()})
    clone = manager.create_job(kind="loudness", config=dict(original.config))
    stored = tmp_path / "loudness_inputs" / original.config["file_paths_ref"]
    assert len(list(stored.parent.iterdir())) == 1

    assert manager.delete_job(original.id)
    assert stored.is_file()
    assert inputs.read_file_paths(tmp_path, clone.config) == _selection()

    if removal == "delete":
        assert manager.delete_job(clone.id)
    elif removal == "clear":
        manager.complete_job(clone.id)
        assert manager.clear_completed_jobs() == 1
    else:
        manager.complete_job(clone.id)
        clone.completed_at = "2000-01-01T00:00:00+00:00"
        with manager._lock:
            manager._enforce_log_retention()
        assert manager.get_job(clone.id) is None
    assert not stored.exists()


def test_running_job_keeps_its_input(tmp_path):
    manager = JobManager(config_dir=str(tmp_path))
    created = manager.create_job(kind="loudness", config={"file_paths": _selection()})
    manager.start_job(created.id)
    assert not manager.delete_job(created.id)
    assert inputs.read_file_paths(tmp_path, created.config) == _selection()


def test_failed_durable_deletion_retains_input_for_record_on_disk(tmp_path, monkeypatch):
    manager = JobManager(config_dir=str(tmp_path))
    created = manager.create_job(kind="loudness", config={"file_paths": _selection()})
    clone = manager.create_job(kind="loudness", config=dict(created.config))
    delete = manager._storage.delete

    def fail_delete(job_id):
        if job_id == created.id:
            raise sqlite3.OperationalError("database is locked")
        delete(job_id)

    monkeypatch.setattr(manager._storage, "delete", fail_delete)
    manager.delete_job(created.id)
    manager.delete_job(clone.id)
    restored = JobManager(config_dir=str(tmp_path)).get_job(created.id)
    assert restored is not None
    assert inputs.read_file_paths(tmp_path, restored.config) == _selection()


@pytest.mark.parametrize("damage", ["missing", "changed", "count", "reference", "symlink"])
def test_invalid_input_never_falls_back_to_all_libraries(tmp_path, damage):
    config = inputs.store_file_paths(tmp_path, {"file_paths": _selection()})
    stored = tmp_path / "loudness_inputs" / config["file_paths_ref"]
    if damage == "missing":
        stored.unlink()
    elif damage == "changed":
        stored.write_text('{"version":1,"paths":["/other/file.mkv"]}')
    elif damage == "count":
        config["file_paths_count"] -= 1
    elif damage == "reference":
        config["file_paths_ref"] = "../settings.json"
    else:
        other = tmp_path / "other.json"
        stored.rename(other)
        stored.symlink_to(other)
    with pytest.raises((OSError, ValueError)):
        inputs.read_file_paths(tmp_path, config)


def test_failed_input_write_creates_no_job_or_partial_selection(tmp_path, monkeypatch):
    manager = JobManager(config_dir=str(tmp_path))

    def fail_replace(_source, _destination):
        raise OSError("disk full")

    monkeypatch.setattr(inputs.os, "replace", fail_replace)
    with pytest.raises(OSError, match="disk full"):
        manager.create_job(kind="loudness", config={"file_paths": _selection()})
    assert manager.get_all_jobs() == []
    assert list((tmp_path / "loudness_inputs").iterdir()) == []


def test_manifest_runner_processes_only_selected_files(lifecycle: Lifecycle, monkeypatch):
    monkeypatch.setattr(inputs, "INLINE_FILE_LIMIT", 2)
    lifecycle.api_ready = True
    paths = [lifecycle.add_file(f"chosen-{number}.mkv") for number in range(3)]
    lifecycle.add_file("not-selected.mkv")
    created = lifecycle.start(paths)

    assert created.config["file_paths"] == []
    assert created.config["file_paths_count"] == 3
    assert created.status is JobStatus.COMPLETED
    assert {key: count for key, count in created.progress.outcome.items() if count} == {job.WRITTEN: 3}
    assert sorted(lifecycle.analyses) == [(path, 1) for path in paths]


def test_missing_manifest_fails_job_without_enumerating_other_libraries(lifecycle: Lifecycle, monkeypatch):
    monkeypatch.setattr(inputs, "INLINE_FILE_LIMIT", 2)
    paths = [lifecycle.add_file(f"chosen-{number}.mkv") for number in range(3)]
    created = job.create_loudness_job(library_name="Selected", priority=2, source="manual", file_paths=paths)
    stored = Path(lifecycle.manager.config_dir) / "loudness_inputs" / created.config["file_paths_ref"]
    stored.unlink()

    job.run_loudness_job(created.id)

    assert created.status is JobStatus.FAILED
    assert lifecycle.analyses == []
