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
    from ..settings import ServerMarkersSettings


def publisher_for(
    server: Any,
    config: ServerConfig,
    *,
    sibling_markers: Callable[[str], dict[MarkerType, Marker] | None] | None = None,
    settings: ServerMarkersSettings | None = None,
) -> MarkerPublisher | None:
    """Build the publisher for ``config.type`` with that server's markers settings.

    Args:
        server: Live client for ``config``.
        config: The server's ``ServerConfig``; its ``markers`` block becomes the publisher's settings.
        sibling_markers: Decided markers for another local file (Plex multi-version items).
        settings: Use these settings instead of the stored block (the Edit tab checks a server as if it were on).

    Returns:
        The publisher, or None for server types without one yet (Emby until phase 2).
    """
    if settings is None:
        settings = load_server(config.markers, config.type.value)
    if config.type is ServerType.PLEX:
        from .plex_db import PlexMarkerPublisher

        return PlexMarkerPublisher(server, config, settings, sibling_markers=sibling_markers)
    if config.type is ServerType.JELLYFIN:
        from .jellyfin import JellyfinMarkerPublisher

        return JellyfinMarkerPublisher(server, config, settings)
    return None
