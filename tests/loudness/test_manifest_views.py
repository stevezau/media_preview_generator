"""Large loudness selections retain job details and Inspector membership."""

from pathlib import Path

import pytest

from media_preview_generator.web import jobs
from media_preview_generator.web.job_details import (
    job_file_list,
    job_library_names,
    job_library_scope,
    saved_server_configs,
)
from media_preview_generator.web.routes.api_inspector import active_job_for
from tests.test_job_details import app  # noqa: F401


@pytest.fixture
def selected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    manager = jobs.JobManager(config_dir=str(tmp_path))
    monkeypatch.setattr(jobs, "get_job_manager", lambda: manager)
    paths = [f"/media/movies/{i}.mkv" for i in range(1000)] + ["/media/tv/last.mkv"]
    entry = manager.create_job(kind="loudness", config={"file_paths": paths, "server_id": "plex"})
    configs = saved_server_configs(
        [
            {
                "id": "plex",
                "type": "plex",
                "name": "Plex",
                "libraries": [
                    {"id": "1", "name": "Movies", "remote_paths": ["/media/movies"]},
                    {"id": "2", "name": "TV", "remote_paths": ["/media/tv"]},
                ],
            }
        ]
    )
    try:
        yield manager, entry, paths, configs
    finally:
        manager.close()


def test_manifest_selection_retains_libraries_and_target_file_list(selected) -> None:
    _manager, entry, paths, configs = selected
    assert entry.config["file_paths"] == []
    assert job_library_names(entry, configs) == ["Movies"]
    assert [(row["server_id"], row["library_id"]) for row in job_library_scope(entry, configs)] == [
        ("plex", "1"),
        ("plex", "2"),
    ]
    shown, total = job_file_list(entry, [], limit=2)
    assert total == len(paths)
    assert [row["path"] for row in shown] == paths[:2]


def test_inspector_finds_last_manifest_file_before_processing_starts(selected) -> None:
    _manager, entry, paths, _configs = selected
    assert active_job_for(paths[-1])["id"] == entry.id
    assert active_job_for("/media/tv/unselected.mkv") is None


def test_missing_manifest_does_not_break_job_details_or_match_unselected_files(selected) -> None:
    manager, entry, paths, configs = selected
    (Path(manager.config_dir) / "loudness_inputs" / entry.config["file_paths_ref"]).unlink()
    assert job_library_scope(entry, configs) == []
    assert job_file_list(entry, [], limit=2) == ([], len(paths))
    assert active_job_for(paths[-1]) is None


def test_requested_paths_api_pages_searches_and_requires_auth(app, selected, monkeypatch):  # noqa: F811
    from media_preview_generator.web.routes import api_jobs
    from tests.test_job_details import _HEADERS

    manager, entry, paths, _configs = selected
    monkeypatch.setattr(api_jobs, "get_job_manager", lambda: manager)
    endpoint = f"/api/jobs/{entry.id}/files?view=requested"
    client = app.test_client()
    assert client.get(endpoint).status_code == 401
    response = client.get(endpoint + "&page=3&per_page=500", headers=_HEADERS)
    assert response.status_code == 200
    assert response.json["total"] == len(paths)
    assert response.json["files"] == [{"file": paths[-1]}]
    assert response.json["page"] == 3
    response = client.get(endpoint + "&search=LAST.MKV", headers=_HEADERS)
    assert response.json["filtered_count"] == 1
    assert response.json["files"] == [{"file": paths[-1]}]
    (Path(manager.config_dir) / "loudness_inputs" / entry.config["file_paths_ref"]).unlink()
    response = client.get(endpoint, headers=_HEADERS)
    assert response.status_code == 503
    assert "requested paths" in response.json["error"]


def test_jobs_page_reads_manifest_once_for_names_and_scope(selected):
    from unittest.mock import patch

    from media_preview_generator.loudness import inputs
    from media_preview_generator.web.routes.api_jobs import _job_rows

    manager, entry, _paths, configs = selected
    with patch.object(inputs, "read_file_paths", wraps=inputs.read_file_paths) as read:
        (row,) = _job_rows([entry], configs)
    assert row["library_names"] == ["Movies"]
    assert [pair["library_id"] for pair in row["library_scope"]] == ["1", "2"]
    read.assert_called_once_with(manager.config_dir, entry.config)
