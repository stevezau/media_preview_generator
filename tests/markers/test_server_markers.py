"""read_server_markers: markers already on Plex/Jellyfin/Emby as agreement evidence (spec §5.5 item 7)."""

from __future__ import annotations

from unittest.mock import MagicMock, create_autospec

import pytest

from media_preview_generator.markers.models import Candidate, MarkerType, Source
from media_preview_generator.markers.sources.server_markers import (
    imported_detail,
    importer_database,
    importer_plugin,
    read_server_markers,
)
from media_preview_generator.servers.base import ServerConfig, ServerType
from media_preview_generator.servers.emby import EmbyServer
from media_preview_generator.servers.jellyfin import JellyfinServer
from media_preview_generator.servers.plex import PlexServer

T = MarkerType


def _cfg(t):
    return ServerConfig(id=f"{t.value}-1", type=t, name="x", enabled=True, url="http://x", auth={})


def _src(t, start, end, origin):
    return Candidate(t, start, end, Source.SERVER_MARKERS, origin=origin)


class TestPlex:
    # Plex can't tell our markers from its own, so include_ours changes nothing; callers decide whether to read.
    @pytest.mark.parametrize("include_ours", [False, True])
    def test_final_credits_run_to_end(self, include_ours):
        server = create_autospec(PlexServer, instance=True)
        server.get_markers.return_value = [
            {"type": "intro", "start_ms": 990, "end_ms": 29306, "final": False},
            {"type": "credits", "start_ms": 1156521, "end_ms": 1186521, "final": False},
            {"type": "credits", "start_ms": 1294044, "end_ms": 1322272, "final": True},
        ]
        assert read_server_markers(server, _cfg(ServerType.PLEX), "7", include_ours=include_ours) == [
            _src(T.INTRO, 990, 29306, "plex-1"),
            _src(T.CREDITS, 1156521, 1186521, "plex-1"),
            _src(T.CREDITS, 1294044, None, "plex-1"),
        ]
        server.get_markers.assert_called_once_with("7")

    def test_final_flag_only_changes_credits(self):
        # "Runs to the end" only means something for credits; an intro keeps its end whatever the flag says.
        server = create_autospec(PlexServer, instance=True)
        server.get_markers.return_value = [{"type": "intro", "start_ms": 5, "end_ms": 30_000, "final": True}]
        assert read_server_markers(server, _cfg(ServerType.PLEX), "7") == [_src(T.INTRO, 5, 30_000, "plex-1")]

    def test_other_marker_types_are_ignored(self):
        server = create_autospec(PlexServer, instance=True)
        server.get_markers.return_value = [
            {"type": "commercial", "start_ms": 1, "end_ms": 5000, "final": False},
            {"type": "credits", "start_ms": 10, "end_ms": 20, "final": False},
        ]
        assert read_server_markers(server, _cfg(ServerType.PLEX), "7") == [_src(T.CREDITS, 10, 20, "plex-1")]

    def test_no_markers_is_empty(self):
        server = create_autospec(PlexServer, instance=True)
        server.get_markers.return_value = []
        assert read_server_markers(server, _cfg(ServerType.PLEX), "7") == []

    def test_read_failure_is_none(self):
        server = create_autospec(PlexServer, instance=True)
        server.get_markers.return_value = None
        assert read_server_markers(server, _cfg(ServerType.PLEX), "7") is None


class TestItemWideMarkersOfOtherVersions:
    """Plex serves one marker set per item: with ``duration_ms`` (evidence), an item holding another cut of the file
    gives no evidence (None), since its markers may describe that cut. Emby keeps each version's markers on its own
    item, but its reader applies the same check to the versions Emby lists with the item (with an API key only the
    item's own version, so the check passes)."""

    INTRO = [{"type": "intro", "start_ms": 24_500, "end_ms": 113_900, "final": False}]
    EMBY_INTRO = [
        {"marker_type": "IntroStart", "start_ms": 24_500, "name": ""},
        {"marker_type": "IntroEnd", "start_ms": 113_900, "name": ""},
    ]

    @staticmethod
    def _server(stype, markers, durations):
        if stype is ServerType.PLEX:
            server = create_autospec(PlexServer, instance=True)
            server.get_markers.return_value = markers
            server.get_part_durations.return_value = durations
            return server, server.get_part_durations
        server = create_autospec(EmbyServer, instance=True)
        server.get_chapter_markers.return_value = markers
        server.get_media_source_durations.return_value = durations
        return server, server.get_media_source_durations

    @pytest.mark.parametrize("stype", [ServerType.PLEX, ServerType.EMBY], ids=lambda t: t.value)
    @pytest.mark.parametrize(
        ("durations", "evidence"),
        [
            pytest.param([1_444_574], True, id="one-version"),
            pytest.param([], True, id="no-version-listed"),
            pytest.param([1_444_574, 1_446_574], True, id="two-versions-same-cut-2s"),
            pytest.param([1_442_573, 1_444_574], False, id="two-versions-2001ms-apart"),
            pytest.param([1_444_574, 1_384_574], False, id="web-and-bluray-60s-apart"),
            pytest.param([1_444_574, None], False, id="a-version-without-duration"),
            pytest.param(None, False, id="versions-unreadable"),
        ],
    )
    def test_markers_count_only_when_every_version_is_this_cut(self, stype, durations, evidence):
        markers = self.INTRO if stype is ServerType.PLEX else self.EMBY_INTRO
        server, durations_call = self._server(stype, markers, durations)
        found = read_server_markers(server, _cfg(stype), "777", duration_ms=1_444_574)
        origin = f"{stype.value}-1"
        assert found == ([_src(T.INTRO, 24_500, 113_900, origin)] if evidence else None)
        durations_call.assert_called_once_with("777")

    @pytest.mark.parametrize("stype", [ServerType.PLEX, ServerType.EMBY], ids=lambda t: t.value)
    def test_no_markers_needs_no_version_check(self, stype):
        server, durations_call = self._server(stype, [], None)
        assert read_server_markers(server, _cfg(stype), "777", duration_ms=1_444_574) == []
        durations_call.assert_not_called()

    @pytest.mark.parametrize("stype", [ServerType.PLEX, ServerType.EMBY], ids=lambda t: t.value)
    def test_what_clients_see_ignores_versions(self, stype):
        # The Inspector shows the server's real state, whatever cut it belongs to.
        markers = self.INTRO if stype is ServerType.PLEX else self.EMBY_INTRO
        server, durations_call = self._server(stype, markers, None)
        found = read_server_markers(server, _cfg(stype), "777", include_ours=True)
        assert found == [_src(T.INTRO, 24_500, 113_900, f"{stype.value}-1")]
        durations_call.assert_not_called()

    def test_jellyfin_segments_are_per_version_and_need_no_check(self):
        server = create_autospec(JellyfinServer, instance=True)
        server.get_media_segments.return_value = [
            {"Type": "Intro", "StartTicks": 245_000_000, "EndTicks": 1_139_000_000}
        ]
        server.get_bridge_markers.return_value = []
        found = read_server_markers(server, _cfg(ServerType.JELLYFIN), "bd", duration_ms=1_444_574)
        assert found == [_src(T.INTRO, 24_500, 113_900, "jellyfin-1")]
        server.get_media_source_durations.assert_not_called()


class TestImporterPlugins:
    @pytest.mark.parametrize(
        ("names", "importer"),
        [
            (["TheIntroDB"], "TheIntroDB"),
            (["Media Preview Bridge", "IntroDB"], "IntroDB"),
            (["Intro DB Segments"], "Intro DB Segments"),
            (["SkipDB"], "SkipDB"),
            (["AniSkip"], "AniSkip"),
            (["Ani-Skip Segments", "Trakt", "TheIntroDB"], "Ani-Skip Segments, TheIntroDB"),
            # Intro Skipper fingerprints the server's own files: local detection, not a crowd database
            (["Intro Skipper"], None),
            (["Media Preview Bridge", "Trakt", "Chapter Segments Provider"], None),
            ([], None),
        ],
    )
    def test_importer_plugin_names(self, names, importer):
        assert importer_plugin(names) == importer

    def test_imported_detail_names_the_plugin(self):
        assert imported_detail("TheIntroDB") == (
            "Markers on this server look imported from TheIntroDB; not a second opinion for that database"
        )

    @pytest.mark.parametrize(
        ("names", "database"),
        [
            (["TheIntroDB"], "introdb"),
            (["IntroDB"], "introdb"),
            (["Intro DB Segments"], "introdb"),
            (["SkipDB"], "skipdb"),
            (["Skip-DB Importer"], "skipdb"),
            (["AniSkip"], "aniskip"),
            (["Ani-Skip Segments"], "aniskip"),
            (["IntroDB", "TheIntroDB"], "introdb"),
            # importers of two databases: the server's markers could be a copy of either
            (["SkipDB", "TheIntroDB"], ""),
            (["Ani-Skip Segments", "SkipDB"], ""),
        ],
    )
    def test_importer_database_from_the_names_and_from_the_stored_detail(self, names, database):
        plugins = importer_plugin(names)
        assert importer_database(plugins) == database
        assert importer_database(imported_detail(plugins)) == database

    @pytest.mark.parametrize("text", ["", "Intro Skipper", "imported", "Chapter Segments Provider"])
    def test_importer_database_is_unknown_without_a_database_name(self, text):
        assert importer_database(text) == ""


class TestJellyfin:
    @pytest.mark.parametrize(
        "kwargs", [pytest.param({}, id="default"), pytest.param({"include_ours": False}, id="false")]
    )
    def test_excludes_our_own_segments(self, kwargs):
        server = create_autospec(JellyfinServer, instance=True)
        server.get_media_segments.return_value = [
            {"Type": "Intro", "StartTicks": 1_267_710_000, "EndTicks": 1_570_680_000},
            {"Type": "Outro", "StartTicks": 12_950_000_000, "EndTicks": 13_214_720_000},
            {"Type": "Commercial", "StartTicks": 1, "EndTicks": 2},
        ]
        server.get_bridge_markers.return_value = [
            {"type": "Intro", "startTicks": 1_267_710_000, "endTicks": 1_570_680_000}
        ]
        assert read_server_markers(server, _cfg(ServerType.JELLYFIN), "abc", **kwargs) == [
            _src(T.CREDITS, 1_295_000, 1_321_472, "jellyfin-1")
        ]
        server.get_media_segments.assert_called_once_with("abc")
        server.get_bridge_markers.assert_called_once_with("abc")

    def test_include_ours_returns_everything_jellyfin_serves(self):
        # What clients see now (the Inspector's "current"), ours included; the plugin store isn't consulted.
        server = create_autospec(JellyfinServer, instance=True)
        server.get_media_segments.return_value = [
            {"Type": "Intro", "StartTicks": 1_267_710_000, "EndTicks": 1_570_680_000},
            {"Type": "Outro", "StartTicks": 12_950_000_000, "EndTicks": 13_214_720_000},
            {"Type": "Commercial", "StartTicks": 1, "EndTicks": 2},
        ]
        server.get_bridge_markers.return_value = None
        assert read_server_markers(server, _cfg(ServerType.JELLYFIN), "abc", include_ours=True) == [
            _src(T.INTRO, 126_771, 157_068, "jellyfin-1"),
            _src(T.CREDITS, 1_295_000, 1_321_472, "jellyfin-1"),
        ]
        server.get_media_segments.assert_called_once_with("abc")
        server.get_bridge_markers.assert_not_called()

    def test_include_ours_segments_read_failure_is_none(self):
        server = create_autospec(JellyfinServer, instance=True)
        server.get_media_segments.return_value = None
        assert read_server_markers(server, _cfg(ServerType.JELLYFIN), "abc", include_ours=True) is None

    @pytest.mark.parametrize(
        "ours",
        [
            pytest.param({"type": "Outro", "startTicks": 1_267_710_000, "endTicks": 1_570_680_000}, id="other-type"),
            pytest.param({"type": "Intro", "startTicks": 1_267_700_000, "endTicks": 1_570_680_000}, id="other-start"),
            pytest.param({"type": "Intro", "startTicks": 1_267_710_000, "endTicks": 1_570_690_000}, id="other-end"),
        ],
    )
    def test_only_an_exact_match_is_ours(self, ours):
        server = create_autospec(JellyfinServer, instance=True)
        server.get_media_segments.return_value = [
            {"Type": "Intro", "StartTicks": 1_267_710_000, "EndTicks": 1_570_680_000}
        ]
        server.get_bridge_markers.return_value = [ours]
        assert read_server_markers(server, _cfg(ServerType.JELLYFIN), "abc") == [
            _src(T.INTRO, 126_771, 157_068, "jellyfin-1")
        ]

    def test_all_four_types_from_other_providers(self):
        server = create_autospec(JellyfinServer, instance=True)
        server.get_media_segments.return_value = [
            {"Type": "Recap", "StartTicks": 0, "EndTicks": 100_000},
            {"Type": "Intro", "StartTicks": 200_000, "EndTicks": 300_000},
            {"Type": "Outro", "StartTicks": 400_000, "EndTicks": 500_000},
            {"Type": "Preview", "StartTicks": 600_000, "EndTicks": 700_000},
            {"Type": "Intro", "StartTicks": None, "EndTicks": 300_000},
        ]
        server.get_bridge_markers.return_value = []
        assert read_server_markers(server, _cfg(ServerType.JELLYFIN), "abc") == [
            _src(T.RECAP, 0, 10, "jellyfin-1"),
            _src(T.INTRO, 20, 30, "jellyfin-1"),
            _src(T.CREDITS, 40, 50, "jellyfin-1"),
            _src(T.PREVIEW, 60, 70, "jellyfin-1"),
        ]

    @pytest.mark.parametrize(
        "rows",
        [
            pytest.param([], id="no-segments"),
            pytest.param(
                [{"Type": "Commercial", "StartTicks": 1, "EndTicks": 2}, {"Type": "Intro", "StartTicks": None}],
                id="none-of-our-types",
            ),
        ],
    )
    def test_nothing_to_compare_skips_the_store_read(self, rows):
        server = create_autospec(JellyfinServer, instance=True)
        server.get_media_segments.return_value = rows
        server.get_bridge_markers.return_value = None
        assert read_server_markers(server, _cfg(ServerType.JELLYFIN), "abc") == []
        server.get_bridge_markers.assert_not_called()

    def test_segments_read_failure_is_none(self):
        server = create_autospec(JellyfinServer, instance=True)
        server.get_media_segments.return_value = None
        server.get_bridge_markers.return_value = []
        assert read_server_markers(server, _cfg(ServerType.JELLYFIN), "abc") is None

    def test_unknown_own_segments_is_none(self):
        # Without knowing what we stored, our own published markers could come back as "independent" agreement.
        server = create_autospec(JellyfinServer, instance=True)
        server.get_media_segments.return_value = [
            {"Type": "Intro", "StartTicks": 1_267_710_000, "EndTicks": 1_570_680_000}
        ]
        server.get_bridge_markers.return_value = None
        assert read_server_markers(server, _cfg(ServerType.JELLYFIN), "abc") is None


class TestEmby:
    @pytest.mark.parametrize("include_ours", [False, True])
    def test_chapter_markers_pair_intro_start_end(self, include_ours):
        server = create_autospec(EmbyServer, instance=True)
        server.get_chapter_markers.return_value = [
            {"marker_type": "Chapter", "start_ms": 0, "name": "Chapter 1"},
            {"marker_type": "IntroStart", "start_ms": 126_771, "name": "Intro"},
            {"marker_type": "IntroEnd", "start_ms": 157_068, "name": "Intro End"},
            {"marker_type": "CreditsStart", "start_ms": 1_295_000, "name": "Credits"},
        ]
        assert read_server_markers(server, _cfg(ServerType.EMBY), "42", include_ours=include_ours) == [
            _src(T.INTRO, 126_771, 157_068, "emby-1"),
            _src(T.CREDITS, 1_295_000, None, "emby-1"),
        ]
        server.get_chapter_markers.assert_called_once_with("42")

    @pytest.mark.parametrize(
        "rows",
        [
            pytest.param([{"marker_type": "IntroStart", "start_ms": 5, "name": ""}], id="start-only"),
            pytest.param([{"marker_type": "IntroEnd", "start_ms": 5, "name": ""}], id="end-only"),
            pytest.param([{"marker_type": "Chapter", "start_ms": 5, "name": "Intro"}], id="plain-chapter"),
        ],
    )
    def test_unpaired_intro_is_ignored(self, rows):
        server = create_autospec(EmbyServer, instance=True)
        server.get_chapter_markers.return_value = rows
        assert read_server_markers(server, _cfg(ServerType.EMBY), "42") == []

    def test_ambiguous_duplicates_are_ignored(self):
        # Two IntroStart (or CreditsStart) rows can't be paired without guessing; precision over coverage.
        server = create_autospec(EmbyServer, instance=True)
        server.get_chapter_markers.return_value = [
            {"marker_type": "IntroStart", "start_ms": 1_000, "name": ""},
            {"marker_type": "IntroStart", "start_ms": 90_000, "name": ""},
            {"marker_type": "IntroEnd", "start_ms": 120_000, "name": ""},
            {"marker_type": "CreditsStart", "start_ms": 1_200_000, "name": ""},
            {"marker_type": "CreditsStart", "start_ms": 1_250_000, "name": ""},
        ]
        assert read_server_markers(server, _cfg(ServerType.EMBY), "42") == []

    def test_credits_without_intro(self):
        server = create_autospec(EmbyServer, instance=True)
        server.get_chapter_markers.return_value = [{"marker_type": "CreditsStart", "start_ms": 7, "name": ""}]
        assert read_server_markers(server, _cfg(ServerType.EMBY), "42") == [_src(T.CREDITS, 7, None, "emby-1")]

    def test_read_failure_is_none(self):
        server = create_autospec(EmbyServer, instance=True)
        server.get_chapter_markers.return_value = None
        assert read_server_markers(server, _cfg(ServerType.EMBY), "42") is None


def test_unknown_server_type_is_none():
    config = MagicMock(type="kodi", id="k-1")
    assert read_server_markers(MagicMock(), config, "1") is None
    assert read_server_markers(MagicMock(), config, "1", include_ours=True) is None
