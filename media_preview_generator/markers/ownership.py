"""Which libraries and files Intro & Credits goes to on each server.

The one rule every caller uses (job enumeration, webhook follow-ups, schedules and the per-file pipeline): an enabled
server with Intro & Credits on, a library its markers selection allows, and a path its exclude rules don't skip.
The preview opt-in (``Library.enabled``) plays no part: Intro & Credits has its own library choice.
"""

from __future__ import annotations

from ..config.paths import is_path_excluded
from ..servers.base import Library, ServerConfig
from ..servers.ownership import OwnershipMatch, find_library_matches
from .settings import library_allowed, load_server


def marker_libraries(cfg: ServerConfig) -> list[Library]:
    """Libraries on a server that Intro & Credits goes to, whatever their preview setting.

    Args:
        cfg: The server's config.

    Returns:
        The server's libraries allowed by its markers selection; empty when Intro & Credits is off there.
    """
    settings = load_server(cfg.markers, cfg.type.value)
    if not settings.enabled:
        return []
    return [
        lib
        for lib in cfg.libraries
        if library_allowed(settings, library_id=lib.id, library_name=lib.name, kind=lib.kind)
    ]


def marker_matches(canonical_path: str, configs: list[ServerConfig]) -> dict[str, list[OwnershipMatch]]:
    """The libraries Intro & Credits goes to that hold a file, per server.

    Args:
        canonical_path: Local path of the file.
        configs: Server configs in registry order.

    Returns:
        ``{server_id: [matches]}`` in server order, only for servers with at least one allowed library.
    """
    by_id = {cfg.id: cfg for cfg in configs}
    kept: dict[str, list[OwnershipMatch]] = {}
    for match in find_library_matches(canonical_path, configs):
        cfg = by_id[match.server_id]
        settings = load_server(cfg.markers, cfg.type.value)
        if not settings.enabled or is_path_excluded(canonical_path, cfg.exclude_paths):
            continue
        kind = next((lib.kind for lib in cfg.libraries if lib.id == match.library_id), None)
        if library_allowed(settings, library_id=match.library_id, library_name=match.library_name, kind=kind):
            kept.setdefault(match.server_id, []).append(match)
    return kept
