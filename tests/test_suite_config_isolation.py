"""The test run never writes into the real ``/config``: on a Docker host that folder can hold live service configs."""

import os
import tempfile

import pytest


@pytest.fixture
def config_dir():
    return os.environ.get("CONFIG_DIR") or ""


def _inside(path: str, folder: str) -> bool:
    return os.path.commonpath([os.path.realpath(path), os.path.realpath(folder)]) == os.path.realpath(folder)


def test_config_dir_is_a_temporary_folder_for_the_whole_run(config_dir):
    assert config_dir, "tests/conftest.py must set CONFIG_DIR before the package is imported"
    assert not _inside(config_dir, "/config")
    assert _inside(config_dir, tempfile.gettempdir())
    assert os.path.isdir(config_dir)


def test_default_locations_resolve_inside_it(config_dir, monkeypatch):
    from media_preview_generator.markers import store
    from media_preview_generator.web import auth, jobs, webhooks

    monkeypatch.setattr(store, "MarkerStore", lambda path: path)
    store._store = None
    try:
        markers_db = store.get_marker_store()
    finally:
        store._store = None

    # auth.py reads CONFIG_DIR when it is first imported, so this also shows the variable was set before that.
    for path in (webhooks._history_file_path(), jobs._resolve_default_config_dir(), auth.get_config_dir(), markers_db):
        assert _inside(str(path), config_dir), path
