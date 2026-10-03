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

    assert server.refresh_preview_metadata("/media/video.mkv", item_id) is True

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
        assert server.refresh_preview_metadata("/media/video.mkv") is indexed
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


def _pending_output(tmp_path):
    from media_preview_generator.output.journal import mark_plex_refresh_pending, write_meta
    from media_preview_generator.output.plex_hash import get_source_fingerprint

    source = tmp_path / "video.mkv"
    source.write_bytes(b"source")
    output = tmp_path / "index-sd.bif"
    output.write_bytes(b"preview")
    fingerprint = get_source_fingerprint(str(source))
    write_meta([output], str(source), publisher="plex_bundle", source_fingerprint=fingerprint)
    token = mark_plex_refresh_pending([output], str(source), "plex", source_fingerprint=fingerprint)
    assert token is not None
    return str(source), (output,), fingerprint, token


def _join_worker(queue):
    worker = queue._worker
    if worker is not None:
        worker.join(2)
        assert not worker.is_alive(), "notification worker did not finish"


@pytest.mark.parametrize("outcome", [True, False, "offline"])
def test_only_successful_analyze_acknowledges_the_persisted_notification(tmp_path, outcome):
    from media_preview_generator.output.journal import get_plex_refresh_pending

    path, outputs, fingerprint, token = _pending_output(tmp_path)

    def refresh(canonical_path, item_id):
        assert (canonical_path, item_id) == (path, "42")
        if outcome == "offline":
            raise ConnectionError("offline")
        return outcome

    server = SimpleNamespace(id="plex", name="Plex", refresh_preview_metadata=refresh)
    queue = PlexRefreshQueue(idle_seconds=0.01)
    try:
        assert queue.enqueue(
            server,
            path,
            "42",
            output_paths=outputs,
            notification_token=token,
            source_fingerprint=fingerprint,
        )
        _join_worker(queue)
        pending = get_plex_refresh_pending(outputs, path, "plex", source_fingerprint=fingerprint)
        assert pending == (None if outcome is True else token)
    finally:
        queue.close()


def test_overflow_notification_survives_queue_restart_and_retries(tmp_path):
    from media_preview_generator.output.journal import get_plex_refresh_pending

    path, outputs, fingerprint, token = _pending_output(tmp_path)
    server = SimpleNamespace(id="plex", name="Plex", refresh_preview_metadata=MagicMock(return_value=True))
    full_queue = PlexRefreshQueue(capacity=0)
    assert not full_queue.enqueue(
        server,
        path,
        None,
        output_paths=outputs,
        notification_token=token,
        source_fingerprint=fingerprint,
    )
    full_queue.close()
    server.refresh_preview_metadata.assert_not_called()
    persisted = get_plex_refresh_pending(outputs, path, "plex", source_fingerprint=fingerprint)
    assert persisted == token
    restarted = PlexRefreshQueue(idle_seconds=0.01)
    try:
        assert restarted.enqueue(
            server,
            path,
            "42",
            output_paths=outputs,
            notification_token=persisted,
            source_fingerprint=fingerprint,
        )
        _join_worker(restarted)
        server.refresh_preview_metadata.assert_called_once_with(path, "42")
        assert get_plex_refresh_pending(outputs, path, "plex", source_fingerprint=fingerprint) is None
    finally:
        restarted.close()


def test_old_inflight_ack_does_not_clear_new_publication_token(tmp_path):
    from media_preview_generator.output.journal import get_plex_refresh_pending, mark_plex_refresh_pending

    path, outputs, fingerprint, token = _pending_output(tmp_path)
    first_started, release_first, second_started, release_second = Event(), Event(), Event(), Event()
    calls = []

    def refresh(canonical_path, item_id):
        calls.append((canonical_path, item_id))
        if len(calls) == 1:
            first_started.set()
            assert release_first.wait(2)
        else:
            second_started.set()
            assert release_second.wait(2)
        return True

    server = SimpleNamespace(id="plex", name="Plex", refresh_preview_metadata=refresh)
    queue = PlexRefreshQueue(idle_seconds=0.01)
    try:
        assert queue.enqueue(
            server,
            path,
            "42",
            output_paths=outputs,
            notification_token=token,
            source_fingerprint=fingerprint,
        )
        assert first_started.wait(2)
        newer = mark_plex_refresh_pending(outputs, path, "plex", source_fingerprint=fingerprint)
        assert newer and newer != token
        assert queue.enqueue(
            server,
            path,
            "42",
            output_paths=outputs,
            notification_token=newer,
            source_fingerprint=fingerprint,
        )
        # Duplicate pending work coalesces without losing the latest token or hint.
        assert queue.enqueue(
            server,
            path,
            None,
            output_paths=outputs,
            notification_token=newer,
            source_fingerprint=fingerprint,
        )
        release_first.set()
        assert second_started.wait(2)
        assert get_plex_refresh_pending(outputs, path, "plex", source_fingerprint=fingerprint) == newer
        release_second.set()
        _join_worker(queue)
        assert calls == [(path, "42"), (path, "42")]
        assert get_plex_refresh_pending(outputs, path, "plex", source_fingerprint=fingerprint) is None
    finally:
        release_first.set()
        release_second.set()
        queue.close()
