"""publisher_for: which publisher each server type gets, with that server's markers settings."""

from __future__ import annotations

from unittest.mock import MagicMock, create_autospec

import pytest

from media_preview_generator.markers.publishers.factory import publisher_for
from media_preview_generator.markers.publishers.jellyfin import JellyfinMarkerPublisher
from media_preview_generator.markers.publishers.plex_db import PlexMarkerPublisher
from media_preview_generator.servers.base import ServerConfig, ServerType
from media_preview_generator.servers.emby import EmbyServer
from media_preview_generator.servers.jellyfin import JellyfinServer
from media_preview_generator.servers.plex import PlexServer

_SERVER_CLASS = {ServerType.PLEX: PlexServer, ServerType.JELLYFIN: JellyfinServer, ServerType.EMBY: EmbyServer}

_MARKERS = {
    "enabled": True,
    "library_ids": ["1"],
    "plex": {"db_write_confirmed_at": "t", "on_plex_redetect": "keep_plex"},
}


def _cfg(stype, markers):
    return ServerConfig(id="s", type=stype, name="s", enabled=True, url="http://s", auth={}, markers=markers)


@pytest.mark.parametrize(
    ("stype", "cls"),
    [
        (ServerType.PLEX, PlexMarkerPublisher),
        (ServerType.JELLYFIN, JellyfinMarkerPublisher),
        (ServerType.EMBY, type(None)),
    ],
)
def test_factory_matrix_and_settings_passthrough(stype, cls):
    cfg = _cfg(stype, _MARKERS)
    server = create_autospec(_SERVER_CLASS[stype], instance=True)
    sibling_lookup = MagicMock(name="sibling_markers")
    pub = publisher_for(server, cfg, sibling_markers=sibling_lookup)
    assert type(pub) is cls
    if pub is None:
        return
    assert pub._server is server
    assert pub._config is cfg
    assert pub._settings.enabled is True
    assert pub._settings.library_ids == ("1",)
    if stype is ServerType.PLEX:
        assert pub._settings.db_write_confirmed_at == "t"
        assert pub._settings.on_plex_redetect == "keep_plex"
        assert pub._sibling_markers is sibling_lookup  # multi-version items can only publish once siblings are known


@pytest.mark.parametrize("stype", [ServerType.PLEX, ServerType.JELLYFIN])
def test_missing_or_invalid_block_gives_a_disabled_publisher(stype):
    for markers in ({}, {"enabled": True, "library_ids": "not-a-list"}):
        pub = publisher_for(create_autospec(_SERVER_CLASS[stype], instance=True), _cfg(stype, markers))
        assert pub is not None
        assert pub._settings.enabled is False


def test_plex_without_confirmation_stays_disabled():
    pub = publisher_for(
        create_autospec(PlexServer, instance=True), _cfg(ServerType.PLEX, {"enabled": True, "library_ids": None})
    )
    assert pub._settings.enabled is False


@pytest.mark.parametrize(
    ("stype", "cls"),
    [
        (ServerType.PLEX, PlexMarkerPublisher),
        (ServerType.JELLYFIN, JellyfinMarkerPublisher),
        (ServerType.EMBY, type(None)),
    ],
)
def test_settings_override_replaces_the_stored_block(stype, cls):
    from media_preview_generator.markers.settings import ServerMarkersSettings

    override = ServerMarkersSettings(True, ("9",), "preview", "keep_plex")
    stored_off = {"enabled": False, "library_ids": None}
    pub = publisher_for(
        create_autospec(_SERVER_CLASS[stype], instance=True), _cfg(stype, stored_off), settings=override
    )
    assert type(pub) is cls
    if pub is not None:
        assert pub._settings is override
