"""Tests for library_health.plex."""

import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from media_preview_generator.library_health import plex
from media_preview_generator.library_health.models import CellState, CheckCancelled, Feature
from media_preview_generator.loudness.analyze import ANALYSIS_VERSION, ln_fields
from media_preview_generator.loudness.plex_db import VERSION_FIELD
from media_preview_generator.markers.publishers.base import PublishError
from media_preview_generator.markers.publishers.plex_db import LocalPlexDb, encode_extra_data
from media_preview_generator.servers.base import Library, ServerConfig, ServerType

from .plex_fixture import SD_FLAG, FakeLocalDb, build_plex_db, write_bif

_REPORT = {
    "input_i": "-23.00",
    "input_tp": "-5.00",
    "input_lra": "7.00",
    "input_thresh": "-33.00",
    "target_offset": "0.00",
}
_FULL_LOUDNESS = encode_extra_data({**ln_fields(_REPORT), VERSION_FIELD: ANALYSIS_VERSION})
_NATIVE_PARTIAL = encode_extra_data({"ln:loudness": "-23.00"})
_MARKERS_ON = {"enabled": True, "library_ids": None, "plex": {"db_write_confirmed_at": "2026-01-01"}}


def _cfg(tmp_path, libraries, **kwargs) -> ServerConfig:
    return ServerConfig(
        id="s1",
        type=ServerType.PLEX,
        name="Plex",
        enabled=True,
        url="http://x",
        auth={},
        libraries=libraries,
        output={"plex_config_folder": str(tmp_path / "plex")},
        markers=_MARKERS_ON,
        loudness={"enabled": True, "library_ids": None},
        **kwargs,
    )


def _lib(lib_id="1", name="Movies", kind="movie") -> Library:
    return Library(id=lib_id, name=name, remote_paths=(), enabled=True, kind=kind)


def _movie(title, file, bundle_hash=None, extra=None, audio=(), tags=(), section=1, parts=None):
    part = {"file": file, "hash": bundle_hash, "extra_data": extra, "audio": list(audio)}
    return {"section": section, "type": 1, "title": title, "parts": parts or [part], "tags": list(tags)}


def _episode(title, file, section=2, tags=(), bundle_hash=None):
    return {
        "section": section,
        "type": 4,
        "title": title,
        "show": "Show",
        "season_index": 1,
        "index": 3,
        "parts": [{"file": file, "hash": bundle_hash, "extra_data": None, "audio": []}],
        "tags": list(tags),
    }


def _run(monkeypatch, tmp_path, cfg, items, nothing_found=None, cancel=lambda: False):
    db_path = tmp_path / "plex.db"
    build_plex_db(db_path, items)
    monkeypatch.setattr(plex, "_open_db", lambda _cfg: FakeLocalDb(db_path))
    return plex.count_plex(
        cfg, MagicMock(), nothing_found=nothing_found or {}, cancel_check=cancel, progress=lambda *_: None
    )


def _cell(result, library_id, feature):
    return next(lib for lib in result.libraries if lib.library_id == library_id).cells[feature]


def test_counts_previews_loudness_markers(monkeypatch, tmp_path):
    write_bif(tmp_path / "plex", "aa11")
    write_bif(tmp_path / "plex", "bb22")
    items = [
        _movie("A", "/m/a.mkv", "aa11", SD_FLAG, [_FULL_LOUDNESS], ["credits"]),
        _movie("B", "/m/b.mkv", "bb22", '{"ma:container":"mkv"}', [None]),
        _movie("C", "/m/c.mkv", None, None, [None]),
    ]

    result = _run(monkeypatch, tmp_path, _cfg(tmp_path, [_lib()]), items)

    previews = _cell(result, "1", Feature.PREVIEWS)
    assert (previews.total, previews.done, previews.not_showing, previews.todo) == (3, 2, 1, 1)
    loudness = _cell(result, "1", Feature.LOUDNESS)
    assert (loudness.total, loudness.done) == (3, 1)
    assert _cell(result, "1", Feature.INTRO).state == CellState.NOT_APPLICABLE
    credits = _cell(result, "1", Feature.CREDITS)
    assert (credits.total, credits.done, credits.todo) == (3, 1, 2)
    assert result.access == plex.ACCESS_DIRECT
    assert {t.library_id for t in result.todo} == {"1"}
    flagged = [t for t in result.todo if t.feature == Feature.PREVIEWS and t.not_showing]
    assert [t.path for t in flagged] == ["/m/b.mkv"]


def test_incomplete_native_loudness_is_todo(monkeypatch, tmp_path):
    items = [_movie("A", "/m/a.mkv", audio=[_NATIVE_PARTIAL])]

    result = _run(monkeypatch, tmp_path, _cfg(tmp_path, [_lib()]), items)

    loudness = _cell(result, "1", Feature.LOUDNESS)
    assert (loudness.total, loudness.done, loudness.todo) == (1, 0, 1)


def test_part_with_one_unanalysed_audio_stream_is_todo(monkeypatch, tmp_path):
    items = [_movie("A", "/m/a.mkv", audio=[_FULL_LOUDNESS, None])]

    result = _run(monkeypatch, tmp_path, _cfg(tmp_path, [_lib()]), items)

    assert _cell(result, "1", Feature.LOUDNESS).done == 0


def test_null_index_audio_stream_ignored_for_loudness(monkeypatch, tmp_path):
    part = {"file": "/m/a.mkv", "hash": None, "extra_data": None, "audio": [_FULL_LOUDNESS], "unindexed_audio": [None]}
    items = [_movie("A", "", parts=[part])]

    result = _run(monkeypatch, tmp_path, _cfg(tmp_path, [_lib()]), items)

    loudness = _cell(result, "1", Feature.LOUDNESS)
    assert (loudness.total, loudness.done) == (1, 1)


def test_url_encoded_loudness_counts_as_done(monkeypatch, tmp_path):
    url_form = encode_extra_data({**ln_fields(_REPORT), VERSION_FIELD: ANALYSIS_VERSION}, url_form=True)
    assert "ln%3A" in url_form
    items = [_movie("A", "/m/a.mkv", audio=[url_form])]

    result = _run(monkeypatch, tmp_path, _cfg(tmp_path, [_lib()]), items)

    assert _cell(result, "1", Feature.LOUDNESS).done == 1


def test_part_without_audio_excluded_from_loudness_total(monkeypatch, tmp_path):
    items = [_movie("A", "/m/a.mkv", audio=[_FULL_LOUDNESS]), _movie("Silent", "/m/s.mkv")]

    result = _run(monkeypatch, tmp_path, _cfg(tmp_path, [_lib()]), items)

    assert _cell(result, "1", Feature.LOUDNESS).total == 1
    assert _cell(result, "1", Feature.PREVIEWS).total == 2


def test_nothing_found_from_markers_db(monkeypatch, tmp_path):
    items = [_episode("Pilot", "/tv/e.mkv"), _episode("Two", "/tv/f.mkv", tags=["intro"])]
    found = {"/tv/e.mkv": {Feature.INTRO}}

    result = _run(monkeypatch, tmp_path, _cfg(tmp_path, [_lib("2", "TV", "show")]), items, nothing_found=found)

    intro = _cell(result, "2", Feature.INTRO)
    assert (intro.total, intro.done, intro.nothing_found, intro.todo) == (2, 1, 1, 0)
    credits = _cell(result, "2", Feature.CREDITS)
    assert (credits.nothing_found, credits.todo) == (0, 2)
    titles = {t.title for t in result.todo}
    assert "Show – S01E03 – Pilot" in titles


def test_excluded_paths_left_out(monkeypatch, tmp_path):
    items = [_movie("A", "/m/a.mkv"), _movie("Trash", "/m/skip/b.mkv")]
    cfg = _cfg(tmp_path, [_lib()], exclude_paths=[{"value": "/m/skip", "type": "path"}])

    result = _run(monkeypatch, tmp_path, cfg, items)

    assert _cell(result, "1", Feature.PREVIEWS).total == 1
    assert all("skip" not in t.path for t in result.todo)


def test_library_not_in_settings_ignored(monkeypatch, tmp_path):
    items = [_movie("A", "/m/a.mkv"), _movie("Other", "/o/x.mkv", section=99)]

    result = _run(monkeypatch, tmp_path, _cfg(tmp_path, [_lib()]), items)

    assert [lib.library_id for lib in result.libraries] == ["1"]
    assert _cell(result, "1", Feature.PREVIEWS).total == 1
    assert all(t.path != "/o/x.mkv" for t in result.todo)


def test_unmapped_path_still_counted(monkeypatch, tmp_path):
    items = [_movie("A", "/plex/only/a.mkv")]
    cfg = _cfg(tmp_path, [_lib()], path_mappings=[{"plex_prefix": "/other", "local_prefix": "/mnt"}])

    result = _run(monkeypatch, tmp_path, cfg, items)

    assert _cell(result, "1", Feature.PREVIEWS).total == 1
    assert result.todo[0].path == "/plex/only/a.mkv"


def test_multi_part_item_counts_each_part_markers_shared(monkeypatch, tmp_path):
    parts = [
        {"file": "/m/cd1.mkv", "hash": None, "extra_data": None, "audio": []},
        {"file": "/m/cd2.mkv", "hash": None, "extra_data": None, "audio": []},
    ]
    items = [_movie("Two parts", "", tags=["credits"], parts=parts)]

    result = _run(monkeypatch, tmp_path, _cfg(tmp_path, [_lib()]), items)

    assert _cell(result, "1", Feature.PREVIEWS).total == 2
    credits = _cell(result, "1", Feature.CREDITS)
    assert (credits.total, credits.done) == (2, 2)


_AGENT_ON = {
    **_MARKERS_ON,
    "plex": {"db_write_confirmed_at": "2026-01-01", "agent": {"enabled": True, "url": "http://agent:9", "token": "k"}},
}


def test_open_db_is_none_when_the_marker_agent_is_on(tmp_path):
    cfg = _cfg(tmp_path, [_lib()])
    cfg.markers = _AGENT_ON
    assert plex._open_db(cfg) is None


def test_open_db_is_none_without_a_plex_config_folder(tmp_path):
    cfg = _cfg(tmp_path, [_lib()])
    cfg.output = {}
    assert plex._open_db(cfg) is None


def test_open_db_is_the_local_database_with_agent_off_and_a_folder(tmp_path):
    db = plex._open_db(_cfg(tmp_path, [_lib()]))
    assert isinstance(db, LocalPlexDb)


def test_show_library_uses_episode_type_and_movie_library_uses_movie_type(tmp_path):
    server = MagicMock()
    server._connect.return_value.query.return_value = ET.fromstring("<MediaContainer/>")
    cfg = _cfg(tmp_path, [_lib("1"), _lib("2", "Shows", "show")])
    cfg.output = {}

    plex.count_plex(cfg, server, nothing_found={}, cancel_check=lambda: False, progress=lambda *_: None)

    queried = [call.args[0] for call in server._connect.return_value.query.call_args_list]
    assert queried == ["/library/sections/1/all?type=1", "/library/sections/2/all?type=4"]
    result = plex.count_plex(cfg, server, nothing_found={}, cancel_check=lambda: False, progress=lambda *_: None)
    loudness = _cell(result, "1", Feature.LOUDNESS)
    assert loudness.state == CellState.UNAVAILABLE
    assert "Plex's database" in loudness.reason


def test_agent_mode_uses_api_and_marks_unavailable(tmp_path):
    root = ET.fromstring(
        '<MediaContainer><Video ratingKey="7" type="movie" title="A"><Media>'
        '<Part file="/m/a.mkv" indexes="sd"/><Part file="/m/b.mkv"/></Media></Video></MediaContainer>'
    )
    server = MagicMock()
    server._connect.return_value.query.return_value = root

    cfg = _cfg(tmp_path, [_lib()])
    cfg.markers = _AGENT_ON
    result = plex.count_plex(cfg, server, nothing_found={}, cancel_check=lambda: False, progress=lambda *_: None)

    previews = _cell(result, "1", Feature.PREVIEWS)
    assert (previews.total, previews.done, previews.todo) == (2, 1, 1)
    server._connect.return_value.query.assert_called_once_with("/library/sections/1/all?type=1")
    loudness = _cell(result, "1", Feature.LOUDNESS)
    assert loudness.state == CellState.OFF  # the real settings turn loudness off when an agent is used
    assert _cell(result, "1", Feature.INTRO).state == CellState.NOT_APPLICABLE
    assert result.access == plex.ACCESS_API_ONLY
    assert result.todo[0].library_id == "1"


def test_db_error_marks_cells_error(monkeypatch, tmp_path):
    class LockedDb:
        @contextmanager
        def _database(self, *, read_only, deadline):
            raise PublishError("locked")
            yield

    monkeypatch.setattr(plex, "_open_db", lambda _cfg: LockedDb())

    result = plex.count_plex(
        _cfg(tmp_path, [_lib()]), MagicMock(), nothing_found={}, cancel_check=lambda: False, progress=lambda *_: None
    )

    previews = _cell(result, "1", Feature.PREVIEWS)
    assert previews.state == CellState.ERROR
    assert "locked" in previews.reason
    assert _cell(result, "1", Feature.INTRO).state == CellState.NOT_APPLICABLE


def test_cancel_stops_early(monkeypatch, tmp_path):
    items = [_movie("A", "/m/a.mkv", "aa11")]

    with pytest.raises(CheckCancelled):
        _run(monkeypatch, tmp_path, _cfg(tmp_path, [_lib()]), items, cancel=lambda: True)


def test_large_library_counts_quickly(monkeypatch, tmp_path):
    total = 20_000
    items = []
    for i in range(total):
        bundle_hash = f"{i:040x}"
        if i % 20 == 0:
            write_bif(tmp_path / "plex", bundle_hash)
        items.append(_movie(f"M{i}", f"/m/{i}.mkv", bundle_hash, SD_FLAG, [_FULL_LOUDNESS if i % 2 else None]))
    db_path = tmp_path / "plex.db"
    build_plex_db(db_path, items)
    monkeypatch.setattr(plex, "_open_db", lambda _cfg: FakeLocalDb(db_path))

    started = time.monotonic()
    result = plex.count_plex(
        _cfg(tmp_path, [_lib()]), MagicMock(), nothing_found={}, cancel_check=lambda: False, progress=lambda *_: None
    )
    elapsed = time.monotonic() - started

    assert _cell(result, "1", Feature.PREVIEWS).total == total
    assert _cell(result, "1", Feature.PREVIEWS).done == total // 20
    assert _cell(result, "1", Feature.LOUDNESS).done == total // 2
    assert elapsed < 5
