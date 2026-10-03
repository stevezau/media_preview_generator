"""Background Plex notification capacity, coalescing and Analyze API contracts."""

from threading import Event
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from media_preview_generator.processing.plex_refresh import PlexRefreshQueue
from media_preview_generator.servers.plex import PlexServer


def test_queue_coalesces_pending_hints_and_rejects_overflow():
    started, release, finished = Event(), Event(), Event()
    calls = []

    def refresh(path, item_id):
        calls.append((path, item_id))
        if path == "/media/a.mkv":
            started.set()
            assert release.wait(2)
        else:
            finished.set()

    server = SimpleNamespace(id="plex", name="Plex", refresh_preview_metadata=refresh)
    queue = PlexRefreshQueue(capacity=1)
    try:
        assert queue.enqueue(server, "/media/a.mkv", None)
        assert started.wait(2)
        assert queue.enqueue(server, "/media/b.mkv", None)
        assert queue.enqueue(server, "/media/b.mkv", "22")
        assert queue.enqueue(server, "/media/b.mkv", None)
        assert not queue.enqueue(server, "/media/c.mkv", "33")
        # The first request is still blocked, so enqueuing never waited for Plex.
        assert not release.is_set()
        release.set()
        assert finished.wait(2)
        assert calls == [("/media/a.mkv", None), ("/media/b.mkv", "22")]
    finally:
        release.set()
        queue.close()


def test_write_during_inflight_notification_schedules_another_pass():
    started, release, finished = Event(), Event(), Event()
    calls = []

    def refresh(path, item_id):
        calls.append((path, item_id))
        if len(calls) == 1:
            started.set()
            assert release.wait(2)
        else:
            finished.set()

    server = SimpleNamespace(id="plex", name="Plex", refresh_preview_metadata=refresh)
    queue = PlexRefreshQueue()
    try:
        assert queue.enqueue(server, "/media/a.mkv", "1")
        assert started.wait(2)
        assert queue.enqueue(server, "/media/a.mkv", "1")
        release.set()
        assert finished.wait(2)
        assert calls == [("/media/a.mkv", "1"), ("/media/a.mkv", "1")]
    finally:
        release.set()
        queue.close()


def test_offline_notification_does_not_stop_following_work():
    finished = Event()
    calls = []

    def refresh(path, item_id):
        calls.append((path, item_id))
        if item_id == "1":
            raise ConnectionError("offline")
        finished.set()

    server = SimpleNamespace(id="plex", name="Plex", refresh_preview_metadata=refresh)
    queue = PlexRefreshQueue()
    try:
        assert queue.enqueue(server, "/media/a.mkv", "1")
        assert queue.enqueue(server, "/media/b.mkv", "2")
        assert finished.wait(2)
        assert calls == [("/media/a.mkv", "1"), ("/media/b.mkv", "2")]
    finally:
        queue.close()
    assert not queue.enqueue(server, "/media/c.mkv", "3")


@pytest.mark.parametrize("item_id", ["42", "/library/metadata/42"])
def test_known_item_analyze_uses_normalized_id_and_put(mock_config, item_id):
    server = PlexServer(mock_config)
    plex = MagicMock()
    server._plex = plex

    server.refresh_preview_metadata("/media/video.mkv", item_id)

    plex.query.assert_called_once_with("/library/metadata/42/analyze", method=plex._session.put)
    plex.library.sections.assert_not_called()


def test_invalid_item_id_does_not_issue_analyze(mock_config):
    server = PlexServer(mock_config)
    plex = MagicMock()
    server._plex = plex
    with pytest.raises(ValueError, match="numeric item ID"):
        server.refresh_preview_metadata("/media/video.mkv", "not-an-item")
    plex.query.assert_not_called()


@pytest.mark.parametrize("indexed", [False, True])
def test_hintless_notification_scans_then_analyzes_only_when_indexed(mock_config, indexed):
    mock_config.plex_url = "http://plex:32400"
    mock_config.plex_token = "test-token"
    mock_config.plex_verify_ssl = True
    mock_config.plex_library_ids = []
    mock_config.plex_libraries = []
    mock_config.path_mappings = []
    server = PlexServer(mock_config)
    plex = MagicMock()
    plex.library.sections.return_value = [SimpleNamespace(key="1", METADATA_TYPE="movie")]
    item = SimpleNamespace(ratingKey="42", media=[SimpleNamespace(parts=[SimpleNamespace(file="/media/video.mkv")])])
    plex.fetchItems.return_value = [item] if indexed else []
    server._plex = plex
    sections = MagicMock()
    sections.json.return_value = {
        "MediaContainer": {"Directory": [{"key": "1", "title": "Movies", "Location": [{"path": "/media"}]}]}
    }
    scanned = MagicMock(status_code=200)
    with patch("requests.get", side_effect=[sections, scanned]) as request:
        server.refresh_preview_metadata("/media/video.mkv")
    assert request.call_args_list == [
        call(
            "http://plex:32400/library/sections",
            headers={"X-Plex-Token": "test-token", "Accept": "application/json"},
            timeout=10,
            verify=True,
        ),
        call(
            "http://plex:32400/library/sections/1/refresh",
            params={"path": "/media"},
            headers={"X-Plex-Token": "test-token"},
            timeout=10,
            verify=True,
        ),
    ]
    plex.fetchItems.assert_called_once_with("/library/sections/1/all?type=1&file=video.mkv")
    if indexed:
        plex.query.assert_called_once_with("/library/metadata/42/analyze", method=plex._session.put)
    else:
        plex.query.assert_not_called()
