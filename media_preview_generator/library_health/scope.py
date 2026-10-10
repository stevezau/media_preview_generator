"""Which libraries and features a health check should count, from a server's settings."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..config.paths import is_path_excluded
from ..loudness.settings import library_chosen, load_server_loudness
from ..markers.settings import library_allowed, load_server
from ..processing.multi_server import MOVIE_LIBRARY_KINDS
from ..servers.base import ServerConfig, ServerType
from .models import Cell, CellState, Feature


@dataclass(frozen=True)
class LibraryScope:
    """One library and, per feature, either None (count it) or a fixed cell."""

    library_id: str
    name: str
    kind: str | None
    is_movie_library: bool
    features: dict[Feature, Cell | None]


def library_scopes(cfg: ServerConfig) -> list[LibraryScope]:
    """Work out what to count for each library of a server.

    Args:
        cfg: The server's saved configuration.

    Returns:
        One scope per library; a feature mapped to None is to be counted, a Cell is its fixed state.
    """
    markers = load_server(cfg.markers, cfg.type.value)
    loudness = load_server_loudness(cfg) if cfg.type == ServerType.PLEX else None
    scopes: list[LibraryScope] = []
    for lib in cfg.libraries:
        is_movie = lib.kind in MOVIE_LIBRARY_KINDS
        features: dict[Feature, Cell | None] = {}

        if not lib.enabled:
            features[Feature.PREVIEWS] = Cell(CellState.OFF, reason="Previews are off for this library")
        else:
            features[Feature.PREVIEWS] = None

        if loudness is None:
            features[Feature.LOUDNESS] = Cell(CellState.NOT_APPLICABLE, reason="Plex only")
        elif not loudness.enabled or not library_chosen(loudness, library_id=lib.id, kind=lib.kind):
            features[Feature.LOUDNESS] = Cell(CellState.OFF, reason="Loudness is off for this library")
        else:
            features[Feature.LOUDNESS] = None

        markers_off = not markers.enabled or not library_allowed(
            markers, library_id=lib.id, library_name=lib.name, kind=lib.kind
        )
        off_reason = "Intro & credits are off for this library"
        features[Feature.INTRO] = Cell(CellState.OFF, reason=off_reason) if markers_off else None
        features[Feature.CREDITS] = Cell(CellState.OFF, reason=off_reason) if markers_off else None
        if is_movie:
            features[Feature.INTRO] = Cell(CellState.NOT_APPLICABLE, reason="Films don't get intro markers")

        scopes.append(LibraryScope(lib.id, lib.name, lib.kind, is_movie, features))
    return scopes


def excluded(cfg: ServerConfig) -> Callable[[str], bool]:
    """A predicate for paths this server's exclude rules skip."""
    rules = cfg.exclude_paths
    return lambda path: is_path_excluded(path, rules)
