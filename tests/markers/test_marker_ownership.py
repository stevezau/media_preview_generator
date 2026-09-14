"""Which libraries and files Intro & Credits goes to on each server (the one ownership rule)."""

import pytest

from media_preview_generator.markers import ownership
from media_preview_generator.servers.base import Library, ServerType
from tests.markers.fakes import server_config

PLEX_OFF_UNCONFIRMED = {"enabled": True}  # Plex needs the database-write confirmation before markers are on
TV = Library("tv", "TV Shows", ("/media/tv",))
PATH = "/media/tv/Show/Season 01/S01E01.mkv"


def _ids(matches):
    return {sid: [m.library_id for m in found] for sid, found in matches.items()}


class TestMarkerLibraries:
    def test_libraries_follow_the_markers_selection_not_the_preview_opt_in(self):
        cfg = server_config(
            "jf-1",
            ServerType.JELLYFIN,
            libraries=[
                Library("tv", "TV Shows", ("/media/tv",), enabled=False),
                Library("sports", "Sports", ("/media/sports",)),
                Library("movies", "Movies", ("/media/movies",)),
            ],
        )
        assert [lib.id for lib in ownership.marker_libraries(cfg)] == ["tv", "movies"]

    def test_explicit_markers_library_ids_are_taken_literally(self):
        cfg = server_config(
            "jf-1",
            ServerType.JELLYFIN,
            markers={"enabled": True, "library_ids": ["sports"]},
            libraries=[TV, Library("sports", "Sports", ("/media/sports",))],
        )
        assert [lib.id for lib in ownership.marker_libraries(cfg)] == ["sports"]

    @pytest.mark.parametrize(
        ("stype", "markers"), [(ServerType.JELLYFIN, {"enabled": False}), (ServerType.PLEX, PLEX_OFF_UNCONFIRMED)]
    )
    def test_server_with_markers_off_has_no_marker_libraries(self, stype, markers):
        assert ownership.marker_libraries(server_config("s-1", stype, markers=markers)) == []


class TestMarkerMatches:
    @pytest.mark.parametrize("stype", [ServerType.PLEX, ServerType.EMBY, ServerType.JELLYFIN])
    def test_every_server_type_with_markers_on_matches_its_library(self, stype):
        cfg = server_config("s-1", stype, libraries=[TV])
        matches = ownership.marker_matches(PATH, [cfg])
        assert _ids(matches) == {"s-1": ["tv"]}
        match = matches["s-1"][0]
        assert (match.server_id, match.library_name) == ("s-1", "TV Shows")

    def test_library_with_previews_off_still_matches(self):
        cfg = server_config("jf-1", ServerType.JELLYFIN, libraries=[Library("tv", "TV Shows", ("/media/tv",), False)])
        assert _ids(ownership.marker_matches(PATH, [cfg])) == {"jf-1": ["tv"]}

    @pytest.mark.parametrize(
        "cfg",
        [
            server_config("jf-1", ServerType.JELLYFIN, libraries=[TV], enabled=False),
            server_config("jf-1", ServerType.JELLYFIN, libraries=[TV], markers={"enabled": False}),
            server_config("plex-1", ServerType.PLEX, libraries=[TV], markers=PLEX_OFF_UNCONFIRMED),
            server_config("jf-1", ServerType.JELLYFIN, libraries=[TV], markers={"enabled": True, "library_ids": ["x"]}),
            server_config("jf-1", ServerType.JELLYFIN, libraries=[Library("tv", "Sports", ("/media/tv",))]),
            server_config(
                "jf-1", ServerType.JELLYFIN, libraries=[Library("tv", "Match Day", ("/media/tv",), kind="sports")]
            ),
            server_config(
                "jf-1", ServerType.JELLYFIN, libraries=[TV], exclude_paths=[{"type": "path", "value": "/media/tv/Show"}]
            ),
            server_config(
                "jf-1", ServerType.JELLYFIN, libraries=[TV], exclude_paths=[{"type": "regex", "value": r"S01E0\d"}]
            ),
            server_config("jf-1", ServerType.JELLYFIN, libraries=[Library("m", "Movies", ("/media/movies",))]),
        ],
        ids=[
            "server-off",
            "markers-off",
            "plex-unconfirmed",
            "not-selected",
            "sports",
            "sports-kind",
            "excluded",
            "regex",
            "no-lib",
        ],
    )
    def test_server_that_wont_take_markers_for_the_file_has_no_match(self, cfg):
        assert ownership.marker_matches(PATH, [cfg]) == {}

    def test_sports_library_chosen_explicitly_matches(self):
        cfg = server_config(
            "jf-1",
            ServerType.JELLYFIN,
            markers={"enabled": True, "library_ids": ["sp"]},
            libraries=[Library("sp", "Sports", ("/media/tv",))],
        )
        assert _ids(ownership.marker_matches(PATH, [cfg])) == {"jf-1": ["sp"]}

    def test_overlapping_libraries_keep_only_the_selected_ones(self):
        cfg = server_config(
            "jf-1",
            ServerType.JELLYFIN,
            markers={"enabled": True, "library_ids": ["4k"]},
            libraries=[Library("hd", "TV", ("/media/tv",)), Library("4k", "TV 4K", ("/media/tv/Show",))],
        )
        assert _ids(ownership.marker_matches(PATH, [cfg])) == {"jf-1": ["4k"]}

    def test_every_covering_library_is_listed(self):
        cfg = server_config(
            "jf-1",
            ServerType.JELLYFIN,
            libraries=[Library("hd", "TV", ("/media/tv",)), Library("4k", "TV 4K", ("/media/tv/Show",))],
        )
        assert _ids(ownership.marker_matches(PATH, [cfg])) == {"jf-1": ["hd", "4k"]}

    @pytest.mark.parametrize(
        ("libraries", "library_ids", "expected"),
        [
            ([Library("1", "TV Shows", ("/media",), enabled=False)], ["1"], ["1"]),
            ([Library("1", "TV Shows", ("/media",), enabled=False)], None, ["1"]),
            ([Library("1", "Media", ("/media",)), Library("2", "TV", ("/media/tv",))], ["2"], ["2"]),
            ([Library("1", "Media", ("/media",)), Library("2", "TV", ("/media/tv",))], ["1"], ["1"]),
            ([Library("1", "Media", ("/media",)), Library("2", "TV", ("/media/tv",))], ["3"], []),
            ([Library("1", "Sports", ("/media",)), Library("2", "TV", ("/media/tv",))], None, ["2"]),
            ([Library("1", "Sports", ("/media",)), Library("2", "Sports Extra", ("/media/tv",))], None, []),
        ],
        ids=[
            "previews-off-ticked",
            "previews-off-default",
            "overlapping-inner-ticked",
            "overlapping-outer-ticked",
            "overlapping-none-ticked",
            "sports-overlapping-normal",
            "sports-only",
        ],
    )
    def test_every_matching_library_counts_then_the_markers_selection_decides(self, libraries, library_ids, expected):
        cfg = server_config("plex-1", ServerType.PLEX, libraries=libraries)
        cfg.markers["library_ids"] = library_ids
        assert _ids(ownership.marker_matches(PATH, [cfg])) == ({"plex-1": expected} if expected else {})

    def test_server_paths_are_mapped_to_local_paths(self):
        cfg = server_config(
            "jf-1",
            ServerType.JELLYFIN,
            libraries=[Library("tv", "TV", ("/jf/tv",))],
        )
        cfg.path_mappings = [{"remote_prefix": "/jf", "local_prefix": "/media"}]
        assert _ids(ownership.marker_matches(PATH, [cfg])) == {"jf-1": ["tv"]}

    def test_several_servers_are_kept_in_order_and_others_left_out(self):
        configs = [
            server_config("jf-1", ServerType.JELLYFIN, libraries=[TV]),
            server_config("emby-1", ServerType.EMBY, libraries=[TV], markers={"enabled": False}),
            server_config("plex-1", ServerType.PLEX, libraries=[TV]),
        ]
        assert list(ownership.marker_matches(PATH, configs)) == ["jf-1", "plex-1"]
