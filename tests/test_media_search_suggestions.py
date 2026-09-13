"""Container-level ``search_suggestions`` for the Manual Generation picker.

Distinct from ``search_items`` (which the Preview Inspector uses and which
expands every show into its episodes), ``search_suggestions`` keeps shows
whole: a show hit carries its folder(s) so the dispatcher can expand the
folder into episodes, while movies and standalone episodes stay leaf hits.

Matrix coverage per .claude/rules/testing.md:
  * vendor: Plex (own hub search), Emby + Jellyfin (shared _embyish path)
  * kind: show (kept whole), movie, episode (S##E## drilled/filtered)
  * the contract that matters: a show is ONE suggestion with its folder
    paths and is NEVER expanded into per-episode rows.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.servers.base import ServerConfig, ServerType
from media_preview_generator.servers.emby import EmbyServer
from media_preview_generator.servers.jellyfin import JellyfinServer
from media_preview_generator.servers.plex import PlexServer


def _plex() -> PlexServer:
    return PlexServer(
        ServerConfig(
            id="plex-1",
            type=ServerType.PLEX,
            name="Plex",
            enabled=True,
            url="http://plex:32400",
            auth={"token": "t"},
        )
    )


def _emby() -> EmbyServer:
    return EmbyServer(
        ServerConfig(
            id="emby-1",
            type=ServerType.EMBY,
            name="EmbyTest",
            enabled=True,
            url="http://emby:8096",
            auth={"method": "api_key", "api_key": "k", "user_id": "u"},
        )
    )


def _jelly() -> JellyfinServer:
    return JellyfinServer(
        ServerConfig(
            id="jelly-1",
            type=ServerType.JELLYFIN,
            name="JellyTest",
            enabled=True,
            url="http://jelly:8096",
            auth={"method": "api_key", "api_key": "k"},
        )
    )


# ---------------------------------------------------------------------------
# Plex
# ---------------------------------------------------------------------------


class TestPlexSearchSuggestions:
    def test_show_query_keeps_show_whole_with_all_folders(self):
        """A show hit becomes ONE ``show`` suggestion carrying every disk
        folder (Plex spreads a show across mounts) — never expanded into
        episodes. This is the whole point of the picker vs. the inspector.
        """
        server = _plex()
        show = MagicMock()
        show.type = "show"
        show.title = "Ben 10 Ultimate Alien"
        show.year = 2010
        show.leafCount = 52
        show.librarySectionID = 3
        show.locations = [
            "/data_16tb/TV Shows/Ben 10 - Ultimate Alien",
            "/data_16tb2/TV Shows/Ben 10 - Ultimate Alien",
        ]
        fake_plex = MagicMock()
        fake_plex.search.return_value = [show]

        with patch.object(PlexServer, "_connect", return_value=fake_plex):
            results = server.search_suggestions("Ben 10 Ultimate Alien", limit=10)

        assert len(results) == 1
        s = results[0]
        assert s.kind == "show"
        assert s.remote_paths == (
            "/data_16tb/TV Shows/Ben 10 - Ultimate Alien",
            "/data_16tb2/TV Shows/Ben 10 - Ultimate Alien",
        )
        assert s.child_count == 52
        assert s.library_id == "3"
        # The defining contract: a show is kept whole, not expanded.
        show.episodes.assert_not_called()

    def test_movie_query_returns_movie_file(self):
        server = _plex()
        movie = MagicMock()
        movie.type = "movie"
        movie.title = "Inception"
        movie.year = 2010
        movie.librarySectionID = 1
        movie.locations = ["/data_16tb/Movies/Inception (2010)/Inception.mkv"]
        fake_plex = MagicMock()
        fake_plex.search.return_value = [movie]

        with patch.object(PlexServer, "_connect", return_value=fake_plex):
            results = server.search_suggestions("Inception", limit=10)

        assert len(results) == 1
        assert results[0].kind == "movie"
        assert results[0].remote_paths == ("/data_16tb/Movies/Inception (2010)/Inception.mkv",)

    def test_season_episode_query_drills_to_single_episode(self):
        """``S##E##`` in the query drills the matched show straight to that
        one episode file instead of returning the whole show.
        """
        server = _plex()
        show = MagicMock()
        show.type = "show"
        show.title = "The Office"
        show.year = 2005
        show.librarySectionID = 4
        show.locations = ["/data/TV/The Office"]
        episode = MagicMock()
        episode.locations = ["/data/TV/The Office/Season 02/The Office S02E01.mkv"]
        episode.year = 2005
        show.episode.return_value = episode
        fake_plex = MagicMock()
        fake_plex.search.return_value = [show]

        with (
            patch.object(PlexServer, "_connect", return_value=fake_plex),
            patch("media_preview_generator.plex_client._build_episode_title", return_value="The Office S02E01"),
        ):
            results = server.search_suggestions("The Office S02E01", limit=10)

        assert len(results) == 1
        s = results[0]
        assert s.kind == "episode"
        assert s.title == "The Office S02E01"
        assert s.remote_paths == ("/data/TV/The Office/Season 02/The Office S02E01.mkv",)
        show.episode.assert_called_once_with(season=2, episode=1)

    def test_empty_query_returns_empty_without_connecting(self):
        server = _plex()
        with patch.object(PlexServer, "_connect") as conn:
            results = server.search_suggestions("", limit=10)
        assert results == []
        conn.assert_not_called()


# ---------------------------------------------------------------------------
# Emby / Jellyfin (shared _embyish path)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("server_factory", [_emby, _jelly])
class TestEmbyishSearchSuggestions:
    def test_show_query_keeps_series_whole(self, server_factory):
        """A Series hit becomes ONE ``show`` suggestion with its folder Path
        and episode count — NOT drilled into /Shows/{id}/Episodes.
        """
        server = server_factory()
        series = {
            "Id": "s1",
            "Type": "Series",
            "Name": "Ben 10",
            "Path": "/data/TV/Ben 10",
            "ProductionYear": 2005,
            "RecursiveItemCount": 52,
        }

        def router(params):
            if params.get("NameStartsWith") == "ben 10":
                return [series]
            if params.get("searchTerm") == "ben 10":
                return [series]  # also surfaces in fallback; must dedup
            return []

        with (
            patch.object(type(server), "query_items", side_effect=router),
            patch.object(type(server), "_request") as req,
        ):
            results = server.search_suggestions("ben 10", limit=10)

        shows = [r for r in results if r.kind == "show"]
        assert len(shows) == 1, f"expected exactly one show row, got {results}"
        assert shows[0].remote_paths == ("/data/TV/Ben 10",)
        assert shows[0].child_count == 52
        assert shows[0].year == 2005
        # Never expand the series into episodes for the picker.
        req.assert_not_called()

    def test_movie_query_returns_movie_file(self, server_factory):
        server = server_factory()
        movie = {
            "Id": "m1",
            "Type": "Movie",
            "Name": "Inception",
            "Path": "/data/Movies/Inception/Inception.mkv",
            "ProductionYear": 2010,
        }

        def router(params):
            if params.get("searchTerm") == "inception":
                return [movie]
            return []

        with patch.object(type(server), "query_items", side_effect=router):
            results = server.search_suggestions("inception", limit=10)

        movies = [r for r in results if r.kind == "movie"]
        assert len(movies) == 1
        assert movies[0].remote_paths == ("/data/Movies/Inception/Inception.mkv",)

    def test_episode_query_returns_only_requested_episode_as_leaf(self, server_factory):
        """``S01E02`` skips the Series-first pass and returns just that
        episode file — no show row, no other episodes.
        """
        server = server_factory()
        episodes = [
            {
                "Id": "ep1",
                "Type": "Episode",
                "Name": "Ep 1",
                "SeriesName": "Ben 10",
                "ParentIndexNumber": 1,
                "IndexNumber": 1,
                "Path": "/data/TV/Ben 10/S01/E01.mkv",
            },
            {
                "Id": "ep2",
                "Type": "Episode",
                "Name": "Ep 2",
                "SeriesName": "Ben 10",
                "ParentIndexNumber": 1,
                "IndexNumber": 2,
                "Path": "/data/TV/Ben 10/S01/E02.mkv",
            },
        ]

        calls = {"namestartswith": 0}

        def router(params):
            if "NameStartsWith" in params:
                calls["namestartswith"] += 1
                return []
            if params.get("searchTerm") == "ben 10":
                return episodes
            return []

        with patch.object(type(server), "query_items", side_effect=router):
            results = server.search_suggestions("ben 10 s01e02", limit=10)

        assert calls["namestartswith"] == 0, "S##E## query must skip the Series-first pass"
        assert len(results) == 1
        assert results[0].kind == "episode"
        assert results[0].remote_paths == ("/data/TV/Ben 10/S01/E02.mkv",)

    def test_empty_query_returns_empty(self, server_factory):
        server = server_factory()
        with patch.object(type(server), "query_items") as qi:
            results = server.search_suggestions("", limit=10)
        assert results == []
        qi.assert_not_called()
