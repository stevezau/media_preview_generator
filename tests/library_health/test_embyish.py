"""Tests for library_health.embyish."""

import os
from pathlib import Path

import pytest
import requests

from media_preview_generator.library_health import embyish
from media_preview_generator.library_health.embyish import count_embyish
from media_preview_generator.library_health.models import CellState, CheckCancelled, Feature
from media_preview_generator.servers.base import Library, ServerConfig, ServerType

_MARKERS_ON = {"enabled": True, "library_ids": None}


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class _Server:
    """Stands in for the Emby/Jellyfin client: pages of /Items and per-item segments."""

    def __init__(self, items, segments=None, fail_status=None, fail_first=0, fail_parent=None):
        self.items = items
        self.fail_first = fail_first
        self.fail_parent = fail_parent
        self.segments = segments or {}
        self.fail_status = fail_status
        self.calls = []

    def _request(self, method, path, params=None, **kwargs):
        self.calls.append(dict(params or {}))
        self.timeouts = getattr(self, "timeouts", []) + [kwargs.get("timeout")]
        if self.fail_first:
            self.fail_first -= 1
            raise requests.Timeout("slow")
        if self.fail_parent and params["ParentId"] == self.fail_parent:
            return _Response({}, 500)
        if self.fail_status:
            return _Response({}, self.fail_status)
        start = params["StartIndex"]
        return _Response({"Items": self.items[start : start + params["Limit"]], "TotalRecordCount": len(self.items)})

    def get_media_segments(self, item_id):
        return self.segments.get(item_id)


def _cfg(server_type=ServerType.JELLYFIN, kind="show", markers=None, extra_libraries=(), **kwargs) -> ServerConfig:
    return ServerConfig(
        id="s1",
        type=server_type,
        name="Server",
        enabled=True,
        url="http://x",
        auth={},
        libraries=[Library(id="L1", name="Lib", remote_paths=(), enabled=True, kind=kind), *extra_libraries],
        markers=_MARKERS_ON if markers is None else markers,
        **kwargs,
    )


def _run(cfg, server, nothing_found=None, cancel=lambda: False):
    return count_embyish(cfg, server, nothing_found=nothing_found or {}, cancel_check=cancel, progress=lambda *a: None)


def _item(n, path=None, **extra):
    return {"Id": f"i{n}", "Name": f"Name{n}", "Path": path or f"/media/f{n}.mkv", **extra}


def _cells(result):
    return result.libraries[0].cells


def test_jellyfin_previews_from_trickplay_field():
    server = _Server([_item(1, Trickplay={"i1": {"320": {}}}), _item(2)])
    result = _run(_cfg(markers={"enabled": False}), server)
    cell = _cells(result)[Feature.PREVIEWS]
    assert (cell.total, cell.done, cell.todo) == (2, 1, 1)
    assert [t.item_id for t in result.todo] == ["i2"]
    assert result.todo[0].library_id == "L1"
    assert server.calls[0]["Fields"] == "Path,Trickplay,MediaSources"


def test_jellyfin_segments_intro_outro():
    server = _Server(
        [_item(1), _item(2)],
        segments={"i1": [{"Type": "Intro"}, {"Type": "Outro"}], "i2": [{"Type": "Commercial"}]},
    )
    result = _run(_cfg(), server, nothing_found={"/media/f2.mkv": {Feature.CREDITS}})
    cells = _cells(result)
    assert (cells[Feature.INTRO].total, cells[Feature.INTRO].done) == (2, 1)
    assert (cells[Feature.CREDITS].done, cells[Feature.CREDITS].nothing_found, cells[Feature.CREDITS].todo) == (1, 1, 0)
    assert {(t.feature, t.item_id) for t in result.todo if t.feature != Feature.PREVIEWS} == {(Feature.INTRO, "i2")}


def test_jellyfin_segment_errors_threshold():
    items = [_item(n) for n in range(10)]
    segments = {f"i{n}": [{"Type": "Intro"}] for n in range(8)}  # i8, i9 return None: 20% failed
    result = _run(_cfg(), _Server(items, segments))
    for feature in (Feature.INTRO, Feature.CREDITS):
        assert _cells(result)[feature].state == CellState.ERROR
        assert "didn't answer for 2 files" in _cells(result)[feature].reason
    assert not [t for t in result.todo if t.feature in (Feature.INTRO, Feature.CREDITS)]


def test_jellyfin_few_segment_errors_count_as_todo():
    items = [_item(n) for n in range(20)]
    segments = {f"i{n}": [{"Type": "Intro"}, {"Type": "Outro"}] for n in range(19)}
    result = _run(_cfg(), _Server(items, segments))
    assert _cells(result)[Feature.INTRO].state == CellState.COUNTED
    assert _cells(result)[Feature.INTRO].todo == 1


def test_emby_markers_from_chapters():
    chapters = [{"MarkerType": "Chapter"}, {"MarkerType": "IntroStart"}, {"MarkerType": "IntroEnd"}]
    server = _Server([_item(1, Chapters=chapters), _item(2, Chapters=[{"MarkerType": "CreditsStart"}])])
    result = _run(_cfg(ServerType.EMBY), server)
    cells = _cells(result)
    assert (cells[Feature.INTRO].done, cells[Feature.INTRO].todo) == (1, 1)
    assert (cells[Feature.CREDITS].done, cells[Feature.CREDITS].todo) == (1, 1)
    assert server.calls[0]["Fields"] == "Path,Chapters"
    assert cells[Feature.LOUDNESS].state == CellState.NOT_APPLICABLE


def _emby_media(tmp_path: Path) -> list[dict]:
    (tmp_path / "Film (2000).mkv").write_bytes(b"v")
    (tmp_path / "Film (2000)-320-10.bif").write_bytes(b"bif")
    (tmp_path / "Other (2001).mkv").write_bytes(b"v")
    (tmp_path / "Empty (2002).mkv").write_bytes(b"v")
    (tmp_path / "Empty (2002)-320-10.bif").write_bytes(b"")
    return [
        _item(n, str(tmp_path / f"{name}.mkv"))
        for n, name in enumerate(["Film (2000)", "Other (2001)", "Empty (2002)"])
    ]


def test_emby_previews_from_sidecar_files(tmp_path):
    server = _Server(_emby_media(tmp_path))
    result = _run(_cfg(ServerType.EMBY, kind="movies", markers={"enabled": False}, output={"width": 320}), server)
    cell = _cells(result)[Feature.PREVIEWS]
    assert (cell.total, cell.done, cell.todo) == (3, 1, 2)
    assert {t.item_id for t in result.todo} == {"i1", "i2"}


def test_emby_scans_each_folder_once(tmp_path, monkeypatch):
    items = []
    for n in range(30):
        (tmp_path / f"v{n}.mkv").write_bytes(b"v")
        items.append(_item(n, str(tmp_path / f"v{n}.mkv")))
    scanned = []
    real = os.scandir
    monkeypatch.setattr(embyish.os, "scandir", lambda p: scanned.append(str(p)) or real(p))
    _run(_cfg(ServerType.EMBY, kind="movies", markers={"enabled": False}), _Server(items))
    assert scanned == [str(tmp_path)]


def test_unreadable_folder_counts_todo(tmp_path):
    items = [_item(1, str(tmp_path / "missing" / "a.mkv")), _item(2, str(tmp_path / "missing" / "b.mkv"))]
    result = _run(_cfg(ServerType.EMBY, kind="movies", markers={"enabled": False}), _Server(items))
    cell = _cells(result)[Feature.PREVIEWS]
    assert (cell.state, cell.total, cell.done, cell.todo) == (CellState.COUNTED, 2, 0, 2)


def test_paging_reads_all_pages(monkeypatch):
    monkeypatch.setattr(embyish, "_LIST_ITEMS_PAGE_SIZE", 2)
    server = _Server([_item(n, Trickplay={f"i{n}": {"1": {}}}) for n in range(5)])
    result = _run(_cfg(markers={"enabled": False}), server)
    assert [c["StartIndex"] for c in server.calls] == [0, 2, 4]
    assert _cells(result)[Feature.PREVIEWS].total == 5


def test_items_without_path_or_excluded_are_skipped():
    items = [{"Id": "x", "Name": "No path"}, _item(1, "/skip/a.mkv"), _item(2, "/media/b.mkv")]
    cfg = _cfg(markers={"enabled": False}, exclude_paths=[{"value": "/skip", "type": "path"}])
    result = _run(cfg, _Server(items))
    assert _cells(result)[Feature.PREVIEWS].total == 1


def test_episode_title():
    raw = _item(1, SeriesName="Show", ParentIndexNumber=2, IndexNumber=3, Name="Pilot")
    result = _run(_cfg(markers={"enabled": False}), _Server([raw]))
    assert result.todo[0].title == "Show – S02E03 – Pilot"


def test_listing_failure_gives_error_cells_for_that_library(monkeypatch):
    monkeypatch.setattr(embyish.time, "sleep", lambda _s: None)
    result = _run(_cfg(), _Server([], fail_status=500))
    cells = _cells(result)
    assert cells[Feature.PREVIEWS].state == CellState.ERROR
    assert cells[Feature.LOUDNESS].state == CellState.NOT_APPLICABLE


def test_cancel_raises():
    with pytest.raises(CheckCancelled):
        _run(_cfg(), _Server([_item(1)]), cancel=lambda: True)


def test_first_page_timeout_is_retried(monkeypatch):
    slept = []
    monkeypatch.setattr(embyish.time, "sleep", slept.append)
    server = _Server([_item(1, Trickplay={"i1": {"1": {}}})], fail_first=1)
    result = _run(_cfg(markers={"enabled": False}), server)
    assert _cells(result)[Feature.PREVIEWS].state == CellState.COUNTED
    assert _cells(result)[Feature.PREVIEWS].total == 1
    assert sum(slept) == embyish._LIST_ITEMS_RETRY_BASE_WAIT_S
    assert server.timeouts == [embyish._LIST_ITEMS_TIMEOUT_S] * 2


def test_failing_library_does_not_hide_the_other(monkeypatch):
    monkeypatch.setattr(embyish.time, "sleep", lambda _s: None)
    other = Library(id="L2", name="Other", remote_paths=(), enabled=True, kind="show")
    cfg = _cfg(markers={"enabled": False}, extra_libraries=[other])
    result = _run(cfg, _Server([_item(1, Trickplay={"i1": {"1": {}}})], fail_parent="L1"))
    first, second = result.libraries
    assert first.cells[Feature.PREVIEWS].state == CellState.ERROR
    assert (second.cells[Feature.PREVIEWS].state, second.cells[Feature.PREVIEWS].total) == (CellState.COUNTED, 1)


def test_cancel_during_backoff_stops_retrying(monkeypatch):
    slept = []
    monkeypatch.setattr(embyish.time, "sleep", slept.append)
    server = _Server([_item(1)], fail_status=500)
    polls = {"n": 0}

    def cancel():
        polls["n"] += 1
        return polls["n"] > 3  # passes the library check, the first attempt, then trips inside the backoff

    with pytest.raises(CheckCancelled):
        _run(_cfg(markers={"enabled": False}), server, cancel=cancel)
    assert len(server.calls) == 1
    assert sum(slept) < embyish._LIST_ITEMS_RETRY_BASE_WAIT_S


def test_cancel_between_attempts_stops_before_next_request(monkeypatch):
    monkeypatch.setattr(embyish.time, "sleep", lambda _s: None)
    server = _Server([_item(1)], fail_status=500)
    state = {"cancelled": False}
    real_request = server._request

    def request(*args, **kwargs):
        state["cancelled"] = True
        return real_request(*args, **kwargs)

    server._request = request
    with pytest.raises(CheckCancelled):
        _run(_cfg(markers={"enabled": False}), server, cancel=lambda: state["cancelled"])
    assert len(server.calls) == 1


def test_jellyfin_versions_counted_once_per_source_path():
    merged = _item(
        1,
        Trickplay={"s1": {"320": {}}},
        MediaSources=[{"Id": "s1", "Path": "/media/a-1080.mkv"}, {"Id": "s2", "Path": "/media/a-4k.mkv"}],
    )
    duplicate_source = _item(
        2, MediaSources=[{"Id": "s3", "Path": "/media/b.mkv"}, {"Id": "s4", "Path": "/media/b.mkv"}]
    )
    server = _Server([merged, duplicate_source])
    result = _run(_cfg(markers={"enabled": False}), server)
    assert "MediaSources" in server.calls[0]["Fields"]
    cell = _cells(result)[Feature.PREVIEWS]
    assert (cell.total, cell.done) == (3, 1)
    assert sorted(t.path for t in result.todo) == ["/media/a-4k.mkv", "/media/b.mkv"]


def test_emby_does_not_fan_out_on_media_sources(tmp_path):
    raw = _item(
        1,
        str(tmp_path / "a.mkv"),
        MediaSources=[{"Id": "x", "Path": "/other/1.mkv"}, {"Id": "y", "Path": "/other/2.mkv"}],
    )
    result = _run(_cfg(ServerType.EMBY, kind="movies", markers={"enabled": False}), _Server([raw]))
    assert _cells(result)[Feature.PREVIEWS].total == 1
