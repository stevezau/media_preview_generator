"""Per-server loudness settings, their save route and the job route."""

from __future__ import annotations

import pytest

from media_preview_generator.loudness import settings as ls
from media_preview_generator.servers.base import Library, ServerConfig, ServerType

CONFIRMED = {"plex": {"db_write_confirmed_at": "2026-09-29T00:00:00+00:00"}}


def _cfg(loudness, markers=CONFIRMED, type_=ServerType.PLEX):
    return ServerConfig(
        id="p1",
        type=type_,
        name="Plex",
        enabled=True,
        url="http://plex",
        auth={},
        libraries=[
            Library(id="1", name="Filmer", remote_paths=(), enabled=True, kind="movie"),
            Library(id="2", name="TV", remote_paths=(), enabled=True, kind="episode"),
            Library(id="3", name="Musik", remote_paths=(), enabled=True, kind="track"),
        ],
        loudness=loudness,
        markers=markers,
    )


def test_off_by_default_and_for_a_missing_block():
    assert ls.default_server_loudness() == {"enabled": False, "library_ids": None}
    assert ls.load_server_loudness(_cfg({})).enabled is False
    assert ls.loudness_libraries(_cfg({})) == []


def test_default_libraries_are_movies_and_tv():
    assert [lib.id for lib in ls.loudness_libraries(_cfg({"enabled": True}))] == ["1", "2"]


def test_explicit_choice_is_taken_literally_including_music():
    assert [lib.id for lib in ls.loudness_libraries(_cfg({"enabled": True, "library_ids": ["3"]}))] == ["3"]


@pytest.mark.parametrize(
    ("raw", "server_type", "markers", "error"),
    [
        ({"enabled": True}, "plex", {}, ls.CONFIRM_FIRST),
        ({"enabled": True}, "plex", {"plex": {**CONFIRMED["plex"], "agent": {"enabled": True}}}, "marker agent"),
        ({"enabled": True}, "jellyfin", CONFIRMED, "Plex servers only"),
        ({"library_ids": "all"}, "plex", CONFIRMED, "list or null"),
        ("on", "plex", CONFIRMED, "must be an object"),
    ],
)
def test_invalid_blocks_are_refused(raw, server_type, markers, error):
    block, err = ls.validate_server_loudness(raw, server_type, markers)
    assert block is None and error in err


def test_an_unconfirmed_stored_block_reads_as_off_and_is_warned_about_once(monkeypatch):
    monkeypatch.setattr(ls, "_warned", set())
    warnings = []
    sink = ls.logger.add(warnings.append, level="WARNING")
    try:
        for _ in range(3):
            assert ls.load_server_loudness(_cfg({"enabled": True}, markers={})).enabled is False
    finally:
        ls.logger.remove(sink)
    assert len(warnings) == 1


def test_enabled_anywhere_skips_disabled_servers():
    cfg = _cfg({"enabled": True})
    assert ls.loudness_enabled_anywhere([cfg]) is True
    cfg.enabled = False
    assert ls.loudness_enabled_anywhere([cfg]) is False
