"""Tests for library_health.scope."""

from media_preview_generator.library_health.models import CellState, Feature
from media_preview_generator.library_health.scope import excluded, library_scopes
from media_preview_generator.servers.base import Library, ServerConfig, ServerType

# Plex refuses to enable markers until the user has confirmed the database write
_MARKERS_ON = {"enabled": True, "library_ids": None, "plex": {"db_write_confirmed_at": "2026-01-01"}}


def _cfg(server_type=ServerType.PLEX, libraries=None, **kwargs) -> ServerConfig:
    return ServerConfig(
        id="s1",
        type=server_type,
        name="Server",
        enabled=True,
        url="http://x",
        auth={},
        libraries=libraries or [],
        **kwargs,
    )


def _lib(lib_id="1", name="Movies", kind="movie", enabled=True) -> Library:
    return Library(id=lib_id, name=name, remote_paths=(), enabled=enabled, kind=kind)


def test_previews_off_when_library_disabled():
    scope = library_scopes(_cfg(libraries=[_lib(enabled=False)]))[0]
    assert scope.features[Feature.PREVIEWS].state == CellState.OFF


def test_previews_counted_when_library_enabled():
    scope = library_scopes(_cfg(libraries=[_lib()]))[0]
    assert scope.features[Feature.PREVIEWS] is None
    assert scope.is_movie_library is True


def test_loudness_not_applicable_on_jellyfin():
    scope = library_scopes(_cfg(ServerType.JELLYFIN, [_lib()]))[0]
    cell = scope.features[Feature.LOUDNESS]
    assert cell.state == CellState.NOT_APPLICABLE
    assert cell.reason == "Plex only"


def test_loudness_off_when_disabled_plex():
    cfg = _cfg(libraries=[_lib()], loudness={"enabled": False, "library_ids": None})
    assert library_scopes(cfg)[0].features[Feature.LOUDNESS].state == CellState.OFF


def test_loudness_counted_when_library_chosen():
    cfg = _cfg(libraries=[_lib("2", "TV", "show")], loudness={"enabled": True, "library_ids": ["2"]})
    assert library_scopes(cfg)[0].features[Feature.LOUDNESS] is None


def test_markers_default_skips_sports():
    cfg = _cfg(
        libraries=[_lib("1", "Sports", "show"), _lib("2", "TV Shows", "show")],
        markers=_MARKERS_ON,
    )
    sports, tv = library_scopes(cfg)
    assert sports.features[Feature.INTRO].state == CellState.OFF
    assert sports.features[Feature.CREDITS].state == CellState.OFF
    assert tv.features[Feature.INTRO] is None
    assert tv.features[Feature.CREDITS] is None


def test_intro_not_applicable_on_movie_library():
    cfg = _cfg(libraries=[_lib()], markers=_MARKERS_ON)
    features = library_scopes(cfg)[0].features
    assert features[Feature.INTRO].state == CellState.NOT_APPLICABLE
    assert features[Feature.CREDITS] is None


def test_excluded_uses_server_rules():
    cfg = _cfg(exclude_paths=[{"value": "/data/skip", "type": "path"}])
    check = excluded(cfg)
    assert check("/data/skip/a.mkv") is True
    assert check("/data/keep/a.mkv") is False


def test_emby_movies_kind_is_a_movie_library():
    scope = library_scopes(_cfg(ServerType.EMBY, [_lib(kind="movies")]))[0]
    assert scope.is_movie_library is True
    assert scope.features[Feature.INTRO].state == CellState.NOT_APPLICABLE


def test_jellyfin_movies_kind_is_a_movie_library():
    scope = library_scopes(_cfg(ServerType.JELLYFIN, [_lib(kind="movies")]))[0]
    assert scope.is_movie_library is True
    assert scope.features[Feature.INTRO].state == CellState.NOT_APPLICABLE


def test_tvshows_kind_is_not_a_movie_library():
    scope = library_scopes(_cfg(ServerType.EMBY, [_lib(kind="tvshows")]))[0]
    assert scope.is_movie_library is False


def test_intro_and_credits_off_cells_are_separate_objects():
    cfg = _cfg(libraries=[_lib(kind="show")], markers={"enabled": False})
    scope = library_scopes(cfg)[0]
    assert scope.features[Feature.INTRO].state == CellState.OFF
    assert scope.features[Feature.INTRO] is not scope.features[Feature.CREDITS]
