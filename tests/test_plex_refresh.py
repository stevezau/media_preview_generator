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

    assert _analyzed(plex) == ["42"]
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
        assert _analyzed(plex) == ["42"]
    else:
        plex.query.assert_not_called()


def _analyzed(plex) -> list[str]:
    """Item ids that got an Analyze PUT, in order."""
    return [
        c.args[0].split("/")[3]
        for c in plex.query.call_args_list
        if c.args[0].endswith("/analyze") and c.kwargs.get("method") is plex._session.put
    ]


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


MULTI = "/tv/Show/Season 01/Show - S01E01-E02.mkv"


class _FakePlex:
    """Plex's item and season listings. Analyze flags the item's parts whose file has a preview, unless Plex
    'skips' that request; ``broken`` makes every read fail."""

    def __init__(self, items: dict[str, dict], *, skipped: dict[str, int] | None = None, broken: bool = False):
        self.items = items
        self.skipped = dict(skipped or {})
        self.broken = broken
        self._session = SimpleNamespace(put=object())
        self.query = MagicMock(side_effect=self._query)

    def _video(self, key: str) -> str:
        item = self.items[key]
        parts = "".join(f'<Part file="{file}"{FLAG if file in item["flagged"] else ""} />' for file in item["files"])
        parent = item.get("parent", "")
        return (
            f'<Video ratingKey="{key}" type="{item["type"]}" parentRatingKey="{parent}"><Media>{parts}</Media></Video>'
        )

    def _query(self, path: str, method=None):
        import xml.etree.ElementTree as ET

        key = path.split("/")[3]
        if path.endswith("/analyze"):
            if self.skipped.get(key, 0) > 0:
                self.skipped[key] -= 1
            else:
                item = self.items[key]
                item["flagged"] |= set(item.get("previews", item["files"]))
            return None
        if self.broken:
            raise ConnectionError("Plex is busy")
        if path.endswith("/children"):
            body = "".join(self._video(k) for k, item in self.items.items() if item.get("parent") == key)
        else:
            body = self._video(key)
        return ET.fromstring(f"<MediaContainer>{body}</MediaContainer>")


FLAG = ' indexes="sd"'


def _episode(*files, parent="7"):
    return {"type": "episode", "parent": parent, "files": list(files), "flagged": set()}


def _movie(*files, previews=None):
    return {"type": "movie", "files": list(files), "flagged": set(), "previews": previews or list(files)}


@pytest.fixture
def no_sleep():
    with patch("media_preview_generator.servers.plex.time.sleep") as sleep:
        yield sleep


class TestAnalyzeIsConfirmed:
    def _server(self, mock_config, fake):
        mock_config.path_mappings = []
        server = PlexServer(mock_config)
        server._plex = fake
        return server

    def test_every_dark_episode_sharing_the_file_is_analyzed(self, mock_config, no_sleep):
        fake = _FakePlex(
            {"41": _episode(MULTI), "42": _episode(MULTI), "43": _episode("/tv/Show/Season 01/Show - S01E03.mkv")}
        )

        assert self._server(mock_config, fake).refresh_preview_metadata(MULTI, "41") is True

        assert _analyzed(fake) == ["41", "42"]
        no_sleep.assert_not_called()

    @pytest.mark.parametrize("sibling", ["shown", "own-file"])
    def test_an_episode_already_showing_or_in_its_own_file_is_left_alone(self, mock_config, no_sleep, sibling):
        second = _episode(MULTI if sibling == "shown" else "/tv/Show/Season 01/Show - S01E02.mkv")
        if sibling == "shown":
            second["flagged"].add(MULTI)
        fake = _FakePlex({"41": _episode(MULTI), "42": second})

        assert self._server(mock_config, fake).refresh_preview_metadata(MULTI, "41") is True

        assert _analyzed(fake) == ["41"]

    def test_a_movie_never_lists_a_season(self, mock_config, no_sleep):
        fake = _FakePlex({"5": _movie("/m/Film/Film.mkv")})

        assert self._server(mock_config, fake).refresh_preview_metadata("/m/Film/Film.mkv", "5") is True

        assert _analyzed(fake) == ["5"]
        assert not [c for c in fake.query.call_args_list if c.args[0].endswith("/children")]

    def test_a_skipped_analyze_is_sent_again_for_the_dark_items_only(self, mock_config, no_sleep):
        fake = _FakePlex({"41": _episode(MULTI), "42": _episode(MULTI)}, skipped={"42": 1})

        assert self._server(mock_config, fake).refresh_preview_metadata(MULTI, "41") is True

        assert _analyzed(fake) == ["41", "42", "42"]
        no_sleep.assert_called_once()

    def test_still_dark_after_the_retry_still_counts_as_analyzed(self, mock_config, no_sleep):
        # Analyze finished either way, so chapters may register; Library health lists the file.
        fake = _FakePlex({"5": _movie("/m/Film/Film.mkv")}, skipped={"5": 9})

        assert self._server(mock_config, fake).refresh_preview_metadata("/m/Film/Film.mkv", "5") is True

        assert _analyzed(fake) == ["5", "5"]

    def test_another_version_without_a_preview_is_not_this_file(self, mock_config, no_sleep):
        hd, uhd = "/movies/Film (2000)/Film (2000).mkv", "/movies-4k/Film (2000)/Film (2000).mkv"
        fake = _FakePlex({"5": _movie(hd, uhd, previews=[hd])})

        assert self._server(mock_config, fake).refresh_preview_metadata(hd, "5") is True

        assert _analyzed(fake) == ["5"]
        no_sleep.assert_not_called()

    def test_unmapped_roots_match_on_folder_and_name(self, mock_config, no_sleep):
        fake = _FakePlex({"5": _movie("/plex/m/Film/Film.mkv")}, skipped={"5": 1})

        self._server(mock_config, fake).refresh_preview_metadata("/data/m/Film/Film.mkv", "5")

        assert _analyzed(fake) == ["5", "5"]

    @pytest.mark.parametrize("case", ["plex-unreadable", "file-not-on-item"])
    def test_when_plex_cannot_say_one_analyze_is_enough(self, mock_config, no_sleep, case):
        fake = _FakePlex({"5": _movie("/m/Other/Other.mkv")}, broken=case == "plex-unreadable")

        assert self._server(mock_config, fake).refresh_preview_metadata("/m/Film/Film.mkv", "5") is True

        assert _analyzed(fake) == ["5"]
