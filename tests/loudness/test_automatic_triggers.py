"""Automatic preview follow-ups use real persisted jobs and configured library ownership."""

import pytest

from media_preview_generator.jobs import orchestrator
from media_preview_generator.loudness import job
from media_preview_generator.loudness.inputs import read_file_paths
from media_preview_generator.markers import triggers
from media_preview_generator.processing.types import ProcessableItem
from media_preview_generator.servers.base import Library, ServerConfig, ServerType
from media_preview_generator.web import jobs


@pytest.fixture
def automatic(tmp_path, monkeypatch):
    manager = jobs.JobManager(config_dir=str(tmp_path))
    monkeypatch.setattr(triggers, "get_job_manager", lambda: manager)
    monkeypatch.setattr(job, "get_job_manager", lambda: manager)
    monkeypatch.setattr(jobs, "get_job_manager", lambda: manager)
    monkeypatch.setattr(job, "start_loudness_job_async", lambda *_: None)
    cfg = ServerConfig(
        id="plex",
        name="Plex",
        type=ServerType.PLEX,
        enabled=True,
        url="http://plex.invalid",
        auth={},
        loudness={"enabled": True},
        libraries=[Library("movie", "Movies", ("/media/movies",), kind="movie")],
    )
    monkeypatch.setattr(triggers, "_server_configs", lambda: [cfg])
    monkeypatch.setattr(triggers, "markers_enabled_anywhere", lambda: False)
    preview = manager.create_job(kind="previews", library_name="Movies", config={"source": "schedule"})
    return manager, preview, cfg


def test_recently_added_also_queues_enabled_loudness(automatic):
    manager, preview, _ = automatic
    orchestrator._queue_intro_credits_follow_ups(preview.id, [ProcessableItem("/media/movies/a.mkv", "plex")], "plex")
    loudness = [entry for entry in manager.get_all_jobs() if entry.kind == "loudness"]
    assert len(loudness) == 1
    assert loudness[0].config["file_paths"] == ["/media/movies/a.mkv"]
    assert loudness[0].config["source"] == "recently_added"
    assert loudness[0].config["server_id"] == "plex"


def test_loudness_does_not_wait_for_its_preview_or_marker_jobs(automatic):
    manager, preview, _ = automatic
    for name in ["a", "b"]:
        manager.create_job(kind="intro_credits", config={"file_paths": [f"/media/movies/{name}.mkv"]})
    triggers._submit_loudness_follow_up(preview.id, ["/media/movies/a.mkv", "/media/movies/b.mkv"], "sonarr", None)
    loudness = next(entry for entry in manager.get_all_jobs() if entry.kind == "loudness")
    assert loudness.config["follows_job_id"] == preview.id
    assert "follows_job_ids" not in loudness.config


def _loudness(manager):
    return [entry for entry in manager.get_all_jobs() if entry.kind == "loudness"]


@pytest.mark.parametrize(
    "change,path,expected",
    [
        ({}, "/media/movies/a.mkv", True),
        ({"enabled": False}, "/media/movies/a.mkv", False),
        ({"loudness": {"enabled": False}}, "/media/movies/a.mkv", False),
        ({"loudness": {"enabled": "true"}}, "/media/movies/a.mkv", False),
        ({"loudness": {"enabled": True, "library_ids": []}}, "/media/movies/a.mkv", False),
        ({"loudness": {"enabled": True, "library_ids": ["other"]}}, "/media/movies/a.mkv", False),
        ({"loudness": {"enabled": True, "library_ids": ["movie"]}}, "/media/movies/a.mkv", True),
        (
            {"exclude_paths": [{"value": "/media/movies/excluded", "type": "path"}]},
            "/media/movies/excluded/a.mkv",
            False,
        ),
        ({}, "/media/movies-elsewhere/a.mkv", False),
        ({"type": ServerType.EMBY, "loudness": {}}, "/media/movies/a.mkv", False),
        ({"libraries": [Library("movie", "Music", ("/media/movies",), kind="artist")]}, "/media/movies/a.mkv", False),
        (
            {"libraries": [Library("movie", "Movies", ("/media/movies",), enabled=False, kind="movie")]},
            "/media/movies/a.mkv",
            True,
        ),
    ],
)
def test_automatic_eligibility_is_independent_of_preview_selection(automatic, change, path, expected):
    manager, preview, cfg = automatic
    for key, value in change.items():
        setattr(cfg, key, value)
    orchestrator._queue_loudness_follow_up(preview.id, [ProcessableItem(path, "plex")], None)
    assert len(_loudness(manager)) == int(expected)
    if expected:
        assert _loudness(manager)[0].config["file_paths"] == [path]


@pytest.mark.parametrize("state", ["pending", "running", "completed", "failed", "cancelled"])
def test_repeated_preview_dispatch_deduplicates_persisted_followups(automatic, monkeypatch, tmp_path, state):
    manager, preview, _ = automatic
    items = [ProcessableItem("/media/movies/a.mkv", "plex")]
    orchestrator._queue_loudness_follow_up(preview.id, items, "plex")
    first = _loudness(manager)[0]
    if state == "running":
        manager.start_job(first.id)
    elif state in ("completed", "failed"):
        manager.complete_job(first.id, error="failed" if state == "failed" else None)
    elif state == "cancelled":
        manager.cancel_job(first.id)
    reloaded = jobs.JobManager(config_dir=str(tmp_path))
    for module in [triggers, job, jobs]:
        monkeypatch.setattr(module, "get_job_manager", lambda: reloaded)
    orchestrator._queue_loudness_follow_up(preview.id, items, "plex")
    assert [entry.id for entry in _loudness(reloaded)] == [first.id]


@pytest.mark.parametrize("config", [{"is_retry": True}, {"is_retry_attempt": True}, {"retry_attempt": 1}])
def test_preview_retries_do_not_spawn_independent_loudness_chains(automatic, config):
    manager, preview, _ = automatic
    manager.merge_job_config(preview.id, config)
    orchestrator._queue_loudness_follow_up(preview.id, [ProcessableItem("/media/movies/a.mkv", "plex")], None)
    assert _loudness(manager) == []


def test_two_preview_arrivals_each_keep_their_own_follow_up_and_source(automatic):
    manager, preview, _ = automatic
    second = manager.create_job(kind="previews", config={"source": "sonarr"})
    items = [ProcessableItem("/media/movies/a.mkv", "plex")]
    orchestrator._queue_loudness_follow_up(preview.id, items, "plex")
    orchestrator._queue_loudness_follow_up(second.id, items, "plex")
    assert {(entry.config["follows_job_id"], entry.config["source"]) for entry in _loudness(manager)} == {
        (preview.id, "schedule"),
        (second.id, "sonarr"),
    }


def test_upfront_webhook_and_dispatch_share_one_job_preserving_sender_path(automatic):
    manager, preview, cfg = automatic
    cfg.path_mappings = [{"remote_prefix": "/sender", "local_prefix": "/media/movies"}]
    manager.merge_job_config(
        preview.id,
        {
            triggers.INTRO_CREDITS_FOLLOW_UP: True,
            "webhook_paths": ["/sender/a.mkv"],
            "source": "sonarr",
            "server_id": "plex",
        },
    )
    triggers.submit_pending_follow_up(preview.id)
    orchestrator._queue_loudness_follow_up(preview.id, [ProcessableItem("/media/movies/a.mkv", "plex")], "plex")
    assert len(_loudness(manager)) == 1
    assert _loudness(manager)[0].config["file_paths"] == ["/sender/a.mkv"]
    assert _loudness(manager)[0].config["source"] == "sonarr"


def test_large_enumeration_uses_one_persisted_job(automatic, monkeypatch, tmp_path):
    manager, preview, _ = automatic
    paths = [f"/media/movies/{n}.mkv" for n in range(1001)]
    items = [ProcessableItem(path, "plex") for path in paths]

    orchestrator._queue_loudness_follow_up(preview.id, items, "plex")

    queued = _loudness(manager)
    assert len(queued) == 1
    follow_up = queued[0]
    assert follow_up.config["file_paths"] == []
    assert read_file_paths(manager.config_dir, follow_up.config) == paths
    assert follow_up.config["server_id"] == "plex"
    assert follow_up.config["source"] == "schedule"
    assert follow_up.config["follows_job_id"] == preview.id
    assert "follows_job_ids" not in follow_up.config
    assert follow_up.library_name == "Plex loudness · Movies"

    reloaded = jobs.JobManager(config_dir=str(tmp_path))
    for module in [triggers, job, jobs]:
        monkeypatch.setattr(module, "get_job_manager", lambda: reloaded)
    orchestrator._queue_loudness_follow_up(preview.id, items, "plex")

    assert [entry.id for entry in _loudness(reloaded)] == [follow_up.id]
    assert read_file_paths(reloaded.config_dir, _loudness(reloaded)[0].config) == paths


@pytest.mark.parametrize("source", ["schedule", "scheduled", "scheduled_recently_added", "sonarr"])
def test_real_dispatch_queues_already_previewed_files_before_checking(automatic, monkeypatch, source):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    manager, preview, cfg = automatic
    manager.merge_job_config(preview.id, {"source": source})
    dispatcher = MagicMock()
    tracker = dispatcher.submit_items.return_value
    tracker.get_result.return_value = {"outcome": {"skipped_bif_exists": 1}}
    monkeypatch.setattr("media_preview_generator.jobs.dispatcher.get_dispatcher", lambda *args: dispatcher)
    seen = []
    dispatcher.submit_items.side_effect = lambda **kwargs: seen.extend(_loudness(manager)) or tracker
    outcome = orchestrator._dispatch_processable_items(
        [(cfg, ProcessableItem("/media/movies/a.mkv", "plex"))],
        config=SimpleNamespace(),
        selected_gpus=[],
        registry=MagicMock(),
        job_id=preview.id,
        server_id_filter="plex",
    )
    assert outcome == {"skipped_bif_exists": 1}
    assert len(seen) == 1
    assert seen[0].config["source"] == source
    assert seen[0].config["server_id"] == "plex"
    assert seen[0].config["follows_job_id"] == preview.id
    assert "follows_job_ids" not in seen[0].config


def test_upfront_directory_defers_to_selected_enumerated_files_without_duplicate(automatic, tmp_path):
    manager, preview, cfg = automatic
    folder = tmp_path / "Movies"
    folder.mkdir()
    included = folder / "a.mkv"
    excluded = folder / "b.mkv"
    included.touch()
    excluded.touch()
    cfg.libraries = [Library("movie", "Movies", (str(folder),), kind="movie")]
    cfg.exclude_paths = [{"value": str(excluded), "type": "path"}]
    cfg.path_mappings = [{"remote_prefix": "/sender", "local_prefix": str(folder)}]
    manager.merge_job_config(
        preview.id,
        {triggers.INTRO_CREDITS_FOLLOW_UP: True, "webhook_paths": ["/sender"], "source": "sonarr", "server_id": "plex"},
    )
    triggers.submit_pending_follow_up(preview.id)
    assert _loudness(manager) == []
    orchestrator._queue_loudness_follow_up(
        preview.id, [ProcessableItem(str(path), "plex") for path in [included, excluded]], "plex"
    )
    assert len(_loudness(manager)) == 1
    assert _loudness(manager)[0].config["file_paths"] == [str(included)]
    assert _loudness(manager)[0].config["follows_job_id"] == preview.id
    assert "follows_job_ids" not in _loudness(manager)[0].config


def test_upfront_missing_file_retains_sender_path_for_loudness_retry(automatic):
    manager, preview, _ = automatic
    missing = "/media/movies/not-arrived.mkv"
    manager.merge_job_config(
        preview.id,
        {triggers.INTRO_CREDITS_FOLLOW_UP: True, "webhook_paths": [missing], "source": "sonarr", "server_id": "plex"},
    )
    triggers.submit_pending_follow_up(preview.id)
    assert len(_loudness(manager)) == 1
    assert _loudness(manager)[0].config["file_paths"] == [missing]
    assert _loudness(manager)[0].config["source"] == "sonarr"


@pytest.mark.parametrize(
    "source,queued",
    [(None, False), ("manual", False), ("schedule", True), ("recently_added", True), ("sonarr", True)],
)
def test_loudness_follow_up_skips_manual_previews_only(automatic, source, queued):
    manager, _, _ = automatic
    config = {} if source is None else {"source": source}
    preview = manager.create_job(kind="previews", library_name="Movies", config=config)
    orchestrator._queue_loudness_follow_up(preview.id, [ProcessableItem("/media/movies/a.mkv", "plex")], None)
    loudness = _loudness(manager)
    assert len(loudness) == int(queued)
    if queued:
        assert loudness[0].config["source"] == (source or "manual")
        assert loudness[0].config["file_paths"] == ["/media/movies/a.mkv"]
