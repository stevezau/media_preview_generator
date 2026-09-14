"""Pick the marker publisher for a server."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ...servers.base import ServerType
from ..settings import load_server
from .base import MarkerPublisher

if TYPE_CHECKING:
    from ...servers.base import ServerConfig
    from ..models import Marker, MarkerType


def publisher_for(
    server: Any,
    config: ServerConfig,
    *,
    sibling_markers: Callable[[str], dict[MarkerType, Marker] | None] | None = None,
) -> MarkerPublisher | None:
    """Build the publisher for ``config.type`` with that server's markers settings.

    Args:
        server: Live client for ``config``.
        config: The server's ``ServerConfig``; its ``markers`` block becomes the publisher's settings.
        sibling_markers: Decided markers for another local file (Plex multi-version items).

    Returns:
        The publisher, or None for server types without one yet (Emby until phase 2).
    """
    settings = load_server(config.markers, config.type.value)
    if config.type is ServerType.PLEX:
        from .plex_db import PlexMarkerPublisher

        return PlexMarkerPublisher(server, config, settings, sibling_markers=sibling_markers)
    if config.type is ServerType.JELLYFIN:
        from .jellyfin import JellyfinMarkerPublisher

        return JellyfinMarkerPublisher(server, config, settings)
    return None
