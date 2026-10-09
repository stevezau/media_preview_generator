class TestPinnedServerWarning:
    """Only a pin to a missing or Plex server deserves the 'no matching Plex entry' warning."""

    _SERVERS = [{"id": "plex-1", "type": "plex"}, {"id": "emby-1", "type": "emby"}, {"id": "jf-1", "type": "jellyfin"}]

    def test_emby_and_jellyfin_pins_are_not_plex(self):
        from media_preview_generator.web.routes.job_runner import _pins_non_plex_server

        assert _pins_non_plex_server(self._SERVERS, "emby-1") is True
        assert _pins_non_plex_server(self._SERVERS, "jf-1") is True

    def test_plex_and_unknown_pins_still_warn(self):
        from media_preview_generator.web.routes.job_runner import _pins_non_plex_server

        assert _pins_non_plex_server(self._SERVERS, "plex-1") is False
        assert _pins_non_plex_server(self._SERVERS, "gone") is False
        assert _pins_non_plex_server(None, "emby-1") is False
