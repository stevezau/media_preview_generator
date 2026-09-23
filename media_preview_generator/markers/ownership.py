"""Which servers own a file, and which libraries and files Intro & Credits goes to on each server.

The one rule every caller uses (job enumeration, webhook follow-ups, schedules, the per-file pipeline and the
Inspector): an enabled server with Intro & Credits on, a library its markers selection allows, and a path its exclude
rules don't skip. The preview opt-in (``Library.enabled``) plays no part: Intro & Credits has its own library choice.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..config.paths import is_path_excluded
from ..servers.base import Library, ServerConfig
from ..servers.ownership import OwnershipMatch, find_library_matches
from .settings import library_allowed, load_server


def owning_servers(canonical_path: str, registry: Any) -> list[tuple[ServerConfig, Any, list[OwnershipMatch]]]:
    """The enabled servers with a library holding a file, whatever their Intro & Credits settings.

    Markers already on these servers are read as evidence even where Intro & Credits is off; :func:`allowed_matches`
    says which of them are published to.

    Args:
        canonical_path: Local path of the file.
        registry: The ``ServerRegistry``.

    Returns:
        ``(config, live client, every covering library's match)`` in registry order; servers that exclude the path or
        whose client couldn't be built are left out.
    """
    by_server: dict[str, list[OwnershipMatch]] = {}
    for match in find_library_matches(canonical_path, registry.configs()):
        by_server.setdefault(match.server_id, []).append(match)
    owners = []
    for server_id, matches in by_server.items():
        cfg = registry.get_config(server_id)
        if cfg is None or (cfg.exclude_paths and is_path_excluded(canonical_path, cfg.exclude_paths)):
            continue
        server = registry.get(server_id)
        if server is not None:
            owners.append((cfg, server, matches))
    return owners


def allowed_matches(cfg: ServerConfig, matches: Iterable[OwnershipMatch]) -> list[OwnershipMatch]:
    """The matches on one server that Intro & Credits goes to: markers on there and the library selected.

    Args:
        cfg: The server's config.
        matches: That server's library matches for a file (exclude rules already applied).

    Returns:
        The allowed matches, in order.
    """
    settings = load_server(cfg.markers, cfg.type.value)
    if not settings.enabled:
        return []
    kinds = {lib.id: lib.kind for lib in cfg.libraries}
    return [
        m
        for m in matches
        if library_allowed(settings, library_id=m.library_id, library_name=m.library_name, kind=kinds.get(m.library_id))
    ]


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
        if is_path_excluded(canonical_path, cfg.exclude_paths):
            continue
        if allowed_matches(cfg, [match]):
            kept.setdefault(match.server_id, []).append(match)
    return kept
