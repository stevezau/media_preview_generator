"""Server-namespace webhook paths must resolve through path mappings.

Regression guard for issue #254. A Plex ``library.new`` webhook resolves the
file path via the ratingKey / ``Part.file`` lookup, which returns Plex's OWN
view of the path (e.g. ``/mnt/Media/TV/...``). When the operator has a path
mapping translating that media-server root to a different local mount
(``/mnt/Media`` → ``/media``), the ownership check translated the library's
``remote_paths`` to the LOCAL form (``/media/TV``) and compared it against the
still-untranslated server-namespace incoming path — which can never match.
Result: "no configured server owns" / "fast-skipping" even though manual runs
(which enumerate the library directly) processed the same file fine.

The resolver only seeded match candidates from ``apply_webhook_prefixes`` (the
Sonarr/Radarr ``/data`` namespace), never from ``apply_path_mappings`` (the
media-server's own remote→local translation). The fix feeds the incoming path
through the server's own path mappings too, so a server-namespace path is
translated to local before the ownership check.
"""

from __future__ import annotations

from unittest.mock import patch

from media_preview_generator.jobs.orchestrator import _resolve_webhook_path_to_canonical
from media_preview_generator.servers.registry import server_config_from_dict


def _plex_with_local_mapping() -> object:
    """Plex reports its TV library at ``/mnt/Media/TV``; this app mounts it at ``/media``."""
    return server_config_from_dict(
        {
            "id": "plex-1",
            "type": "plex",
            "name": "Plex",
            "enabled": True,
            "url": "http://plex:32400",
            "auth": {},
            "libraries": [
                {
                    "id": "2",
                    "name": "TV Shows",
                    "enabled": True,
                    "remote_paths": ["/mnt/Media/TV"],
                }
            ],
            "path_mappings": [
                {"plex_prefix": "/mnt/Media", "local_prefix": "/media", "webhook_prefixes": ["/data"]},
            ],
        }
    )


SERVER_NS_PATH = "/mnt/Media/TV/LBW - Love Beyond Wicket/Season 1/LBW - S01E01.mkv"
LOCAL_PATH = "/media/TV/LBW - Love Beyond Wicket/Season 1/LBW - S01E01.mkv"


class TestServerNamespaceWebhookResolution:
    def test_plex_namespace_path_resolves_through_path_mapping(self):
        """A Plex-namespace webhook path is translated to the local mount and owned (issue #254)."""
        configs = [_plex_with_local_mapping()]

        def _only_local_exists(p):
            return p == LOCAL_PATH

        with patch("media_preview_generator.jobs.orchestrator.os.path.exists", side_effect=_only_local_exists):
            canonical, owners = _resolve_webhook_path_to_canonical(SERVER_NS_PATH, configs)

        assert canonical == LOCAL_PATH, canonical
        assert [m.server_id for m in owners] == ["plex-1"]

    def test_sonarr_namespace_path_still_resolves(self):
        """Regression guard: the existing ``/data`` webhook-sender translation still works."""
        configs = [_plex_with_local_mapping()]
        sonarr_path = "/data/TV/LBW - Love Beyond Wicket/Season 1/LBW - S01E01.mkv"

        def _only_local_exists(p):
            return p == LOCAL_PATH

        with patch("media_preview_generator.jobs.orchestrator.os.path.exists", side_effect=_only_local_exists):
            canonical, owners = _resolve_webhook_path_to_canonical(sonarr_path, configs)

        assert canonical == LOCAL_PATH, canonical
        assert [m.server_id for m in owners] == ["plex-1"]

    def test_no_mapping_server_path_matches_directly(self):
        """Regression guard: with NO path mapping, a server-namespace path matches the
        untranslated library prefix directly (the dominant single-mount install)."""
        cfg = server_config_from_dict(
            {
                "id": "plex-1",
                "type": "plex",
                "name": "Plex",
                "enabled": True,
                "url": "http://plex:32400",
                "auth": {},
                "libraries": [{"id": "2", "name": "TV Shows", "enabled": True, "remote_paths": ["/mnt/Media/TV"]}],
                "path_mappings": [],
            }
        )

        with patch("media_preview_generator.jobs.orchestrator.os.path.exists", return_value=True):
            canonical, owners = _resolve_webhook_path_to_canonical(SERVER_NS_PATH, [cfg])

        assert canonical == SERVER_NS_PATH, canonical
        assert [m.server_id for m in owners] == ["plex-1"]
