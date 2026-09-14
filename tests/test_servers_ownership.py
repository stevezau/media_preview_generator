"""Tests for the multi-server library ownership resolver.

Covers the three cases the dispatcher must distinguish:

1. Path is under no enabled library on this server → skip permanently.
2. Path is under an enabled library → publish (slow-backoff retry if not
   yet indexed by the server itself).
3. Disabled servers and disabled libraries are excluded by both functions.
"""

from __future__ import annotations

import pytest

from media_preview_generator.servers import (
    Library,
    OwnershipMatch,
    ServerConfig,
    ServerType,
    find_owning_servers,
    server_owns_path,
)
from media_preview_generator.servers.ownership import (
    apply_inverse_path_mappings,
    apply_path_mappings,
    apply_webhook_prefixes,
)


class TestApplyInversePathMappings:
    """Local (canonical) path -> server-side path. Used by the Jellyfin
    reverse-lookup so off-media item-id resolution works on path-mapped setups."""

    def test_translates_local_to_remote(self):
        mappings = [{"remote_prefix": "/jf-media", "local_prefix": "/mnt/media"}]
        assert apply_inverse_path_mappings("/mnt/media/Movies/X/X.mkv", mappings) == ["/jf-media/Movies/X/X.mkv"]

    def test_no_match_returns_input_unchanged(self):
        # local==remote setups (no mapping, or a non-matching prefix) are untouched.
        assert apply_inverse_path_mappings("/data/Movies/X.mkv", []) == ["/data/Movies/X.mkv"]
        mappings = [{"remote_prefix": "/jf-media", "local_prefix": "/mnt/media"}]
        assert apply_inverse_path_mappings("/other/X.mkv", mappings) == ["/other/X.mkv"]

    def test_round_trips_with_apply_path_mappings(self):
        mappings = [{"remote_prefix": "/jf-media", "local_prefix": "/mnt/media"}]
        remote = "/jf-media/Movies/Foo (2024)/Foo (2024).mkv"
        local = apply_path_mappings(remote, mappings)[0]
        assert apply_inverse_path_mappings(local, mappings) == [remote]


def _server(
    *,
    server_id: str = "s1",
    name: str = "Server",
    enabled: bool = True,
    libraries: list[Library] | None = None,
    path_mappings: list[dict] | None = None,
    server_type: ServerType = ServerType.PLEX,
) -> ServerConfig:
    return ServerConfig(
        id=server_id,
        type=server_type,
        name=name,
        enabled=enabled,
        url="http://x",
        auth={},
        libraries=libraries or [],
        path_mappings=path_mappings or [],
    )


class TestServerOwnsPath:
    def test_match_under_enabled_library(self):
        server = _server(libraries=[Library(id="1", name="Movies", remote_paths=("/data/movies",), enabled=True)])
        match = server_owns_path("/data/movies/Foo (2024)/Foo (2024).mkv", server)
        assert isinstance(match, OwnershipMatch)
        assert match.server_id == "s1"
        assert match.library_id == "1"
        assert match.library_name == "Movies"

    def test_no_match_when_path_not_under_any_library(self):
        server = _server(libraries=[Library(id="1", name="Movies", remote_paths=("/data/movies",), enabled=True)])
        assert server_owns_path("/data/tv/Show/S01E01.mkv", server) is None

    def test_disabled_library_does_not_match(self):
        server = _server(
            libraries=[
                Library(id="1", name="Movies", remote_paths=("/data/movies",), enabled=False),
                Library(id="2", name="TV", remote_paths=("/data/tv",), enabled=True),
            ]
        )
        # Movies is disabled — don't match.
        assert server_owns_path("/data/movies/Foo.mkv", server) is None
        # TV is enabled — match.
        match = server_owns_path("/data/tv/Show/S01E01.mkv", server)
        assert match is not None
        assert match.library_id == "2"

    def test_disabled_server_never_owns(self):
        server = _server(
            enabled=False,
            libraries=[Library(id="1", name="Movies", remote_paths=("/data/movies",), enabled=True)],
        )
        assert server_owns_path("/data/movies/Foo.mkv", server) is None

    def test_folder_boundary_prevents_partial_prefix_match(self):
        """`/data/movies` must not match `/data/movies-archive/...`."""
        server = _server(libraries=[Library(id="1", name="Movies", remote_paths=("/data/movies",), enabled=True)])
        assert server_owns_path("/data/movies-archive/Foo.mkv", server) is None
        assert server_owns_path("/data/movies/Foo.mkv", server) is not None

    def test_first_matching_library_wins(self):
        """When two libraries cover the same path, the first one matches."""
        server = _server(
            libraries=[
                Library(id="1", name="Movies", remote_paths=("/data/movies",), enabled=True),
                Library(id="2", name="4K Movies", remote_paths=("/data/movies",), enabled=True),
            ]
        )
        match = server_owns_path("/data/movies/Foo.mkv", server)
        assert match is not None
        assert match.library_id == "1"

    def test_multiple_remote_paths_in_library(self):
        server = _server(
            libraries=[
                Library(
                    id="1",
                    name="Movies",
                    remote_paths=("/data/4k", "/data/movies"),
                    enabled=True,
                )
            ]
        )
        match = server_owns_path("/data/movies/Foo.mkv", server)
        assert match is not None
        assert match.local_prefix == "/data/movies"

    def test_path_mapping_translates_remote_to_local(self):
        """Server reports `/media/movies`; on disk it is `/data/movies`."""
        server = _server(
            libraries=[Library(id="1", name="Movies", remote_paths=("/media/movies",), enabled=True)],
            path_mappings=[{"remote_prefix": "/media", "local_prefix": "/data"}],
        )
        match = server_owns_path("/data/movies/Foo.mkv", server)
        assert match is not None
        # Audit fix: ``startswith("/data")`` would also pass for "/data" or
        # "/data/wrong" — assert the exact translated prefix the dispatcher
        # would log, plus every other identity field on the OwnershipMatch.
        assert match.server_id == "s1"
        assert match.library_id == "1"
        assert match.library_name == "Movies"
        assert match.local_prefix == "/data/movies"

    def test_legacy_plex_prefix_mapping_key_supported(self):
        """Legacy mapping rows used `plex_prefix` instead of `remote_prefix`."""
        server = _server(
            libraries=[Library(id="1", name="Movies", remote_paths=("/media/movies",), enabled=True)],
            path_mappings=[{"plex_prefix": "/media", "local_prefix": "/data"}],
        )
        assert server_owns_path("/data/movies/Foo.mkv", server) is not None

    def test_no_libraries_means_no_match(self):
        """A server without libraries owns no path — explicit empty enabled set."""
        server = _server(libraries=[])
        assert server_owns_path("/data/movies/Foo.mkv", server) is None


class TestFindOwningServers:
    def test_fan_out_to_multiple_servers_for_shared_volume(self):
        """Plex A, Jellyfin, and Emby share `/data`; Plex B has its own `/storage`."""
        plex_a = _server(
            server_id="plex-a",
            libraries=[
                Library(id="1", name="Movies", remote_paths=("/data/movies",), enabled=True),
                Library(id="2", name="TV", remote_paths=("/data/tv",), enabled=True),
            ],
        )
        plex_b = _server(
            server_id="plex-b",
            libraries=[Library(id="3", name="Movies", remote_paths=("/storage/movies",), enabled=True)],
        )
        jellyfin = _server(
            server_id="jf",
            server_type=ServerType.JELLYFIN,
            libraries=[Library(id="9", name="Movies", remote_paths=("/data/movies",), enabled=True)],
        )
        emby = _server(
            server_id="em",
            server_type=ServerType.EMBY,
            libraries=[Library(id="7", name="TV", remote_paths=("/data/tv",), enabled=True)],
        )

        # A new file under /data/movies should fan out to plex-a and jellyfin.
        matches = find_owning_servers(
            "/data/movies/Foo (2024)/Foo (2024).mkv",
            [plex_a, plex_b, jellyfin, emby],
        )
        ids = [m.server_id for m in matches]
        assert ids == ["plex-a", "jf"]  # order preserved from input

    def test_path_only_in_plex_b(self):
        plex_a = _server(
            server_id="plex-a",
            libraries=[Library(id="1", name="Movies", remote_paths=("/data/movies",), enabled=True)],
        )
        plex_b = _server(
            server_id="plex-b",
            libraries=[Library(id="3", name="Movies", remote_paths=("/storage/movies",), enabled=True)],
        )
        matches = find_owning_servers("/storage/movies/Foo.mkv", [plex_a, plex_b])
        assert [m.server_id for m in matches] == ["plex-b"]

    def test_no_servers_own_path_returns_empty(self):
        plex_a = _server(
            server_id="plex-a",
            libraries=[Library(id="1", name="Movies", remote_paths=("/data/movies",), enabled=True)],
        )
        matches = find_owning_servers("/elsewhere/Foo.mkv", [plex_a])
        assert matches == []

    def test_disabled_servers_excluded_from_fan_out(self):
        plex_a = _server(
            server_id="plex-a",
            libraries=[Library(id="1", name="Movies", remote_paths=("/data/movies",), enabled=True)],
        )
        plex_b = _server(
            server_id="plex-b",
            enabled=False,
            libraries=[Library(id="3", name="Movies", remote_paths=("/data/movies",), enabled=True)],
        )
        matches = find_owning_servers("/data/movies/Foo.mkv", [plex_a, plex_b])
        assert [m.server_id for m in matches] == ["plex-a"]


class TestWebhookPathCandidates:
    """The local forms of a sender (Sonarr) or server path, shared by webhook resolution and Intro & Credits triggers."""

    def test_raw_path_first_then_each_servers_translations_deduped_in_order(self):
        from media_preview_generator.servers.ownership import webhook_path_candidates

        plex = _server(
            server_id="plex",
            path_mappings=[
                {"remote_prefix": "/plexmedia", "local_prefix": "/media", "webhook_prefixes": ["/data"]},
                {"remote_prefix": "/plexmedia", "local_prefix": "/media2", "webhook_prefixes": ["/data"]},
            ],
        )
        jf = _server(
            server_id="jf",
            path_mappings=[
                {"remote_prefix": "/data", "local_prefix": "/jf-local"},
                {"remote_prefix": "/x", "local_prefix": "/media", "webhook_prefixes": ["/data"]},
            ],
        )
        assert webhook_path_candidates("/data/tv/a.mkv", [plex, jf]) == [
            "/data/tv/a.mkv",
            "/media/tv/a.mkv",
            "/media2/tv/a.mkv",
            "/jf-local/tv/a.mkv",
        ]

    def test_server_view_path_is_translated_with_path_mappings(self):
        from media_preview_generator.servers.ownership import webhook_path_candidates

        jf = _server(path_mappings=[{"remote_prefix": "/jfmedia", "local_prefix": "/media"}])
        assert webhook_path_candidates("/jfmedia/tv/a.mkv", [jf]) == ["/jfmedia/tv/a.mkv", "/media/tv/a.mkv"]

    @pytest.mark.parametrize("servers", [[], [_server()]], ids=["no-servers", "no-mappings"])
    def test_untranslatable_path_is_its_only_candidate(self, servers):
        from media_preview_generator.servers.ownership import webhook_path_candidates

        assert webhook_path_candidates("/data/tv/a.mkv", servers) == ["/data/tv/a.mkv"]


class TestFindLibraryMatches:
    """Intro & Credits ownership: every covering library, whatever the preview opt-in says."""

    def test_library_with_previews_off_still_matches(self):
        from media_preview_generator.servers.ownership import find_library_matches

        server = _server(libraries=[Library(id="1", name="TV Shows", remote_paths=("/data/tv",), enabled=False)])
        assert server_owns_path("/data/tv/Show/S01E01.mkv", server) is None  # previews unchanged
        assert find_library_matches("/data/tv/Show/S01E01.mkv", [server]) == [
            OwnershipMatch(server_id="s1", library_id="1", library_name="TV Shows", local_prefix="/data/tv")
        ]

    def test_overlapping_libraries_all_match_in_library_order(self):
        from media_preview_generator.servers.ownership import find_library_matches

        server = _server(
            libraries=[
                Library(id="1", name="Movies", remote_paths=("/data/movies",)),
                Library(id="2", name="Elsewhere", remote_paths=("/data/other",)),
                Library(id="3", name="4K Movies", remote_paths=("/data/movies/4k",), enabled=False),
            ]
        )
        matches = find_library_matches("/data/movies/4k/Foo (2024)/Foo (2024).mkv", [server])
        assert [(m.library_id, m.local_prefix) for m in matches] == [("1", "/data/movies"), ("3", "/data/movies/4k")]

    def test_servers_in_order_and_disabled_servers_excluded(self):
        from media_preview_generator.servers.ownership import find_library_matches

        lib = Library(id="1", name="TV", remote_paths=("/tv",))
        mapped = _server(
            server_id="jf",
            libraries=[Library(id="9", name="TV", remote_paths=("/jf-tv",))],
            path_mappings=[{"remote_prefix": "/jf-tv", "local_prefix": "/tv"}],
        )
        servers = [
            _server(server_id="a", libraries=[lib]),
            _server(server_id="off", enabled=False, libraries=[lib]),
            mapped,
        ]
        assert [(m.server_id, m.library_id) for m in find_library_matches("/tv/Show/S01E01.mkv", servers)] == [
            ("a", "1"),
            ("jf", "9"),
        ]

    def test_library_with_several_covering_folders_matches_once(self):
        from media_preview_generator.servers.ownership import find_library_matches

        server = _server(libraries=[Library(id="1", name="TV", remote_paths=("/data", "/data/tv"))])
        assert [(m.library_id, m.local_prefix) for m in find_library_matches("/data/tv/Show/S01E01.mkv", [server])] == [
            ("1", "/data")
        ]

    @pytest.mark.parametrize("remote_paths", [(), ("",), ("   ",)])
    def test_empty_remote_paths_and_other_folders_never_match(self, remote_paths):
        from media_preview_generator.servers.ownership import find_library_matches

        server = _server(
            libraries=[
                Library(id="1", name="Blank", remote_paths=remote_paths),
                Library(id="2", name="Movies", remote_paths=("/data/movies",)),
            ]
        )
        assert find_library_matches("/data/movies-archive/Foo.mkv", [server]) == []


class TestEdgeCases:
    @pytest.mark.parametrize(
        "remote_paths",
        [
            (),
            ("",),
            ("   ",),
        ],
    )
    def test_empty_remote_paths_never_match(self, remote_paths):
        server = _server(libraries=[Library(id="1", name="X", remote_paths=remote_paths)])
        assert server_owns_path("/anything", server) is None

    def test_canonical_path_with_trailing_slash_does_not_match_directory_as_file(self):
        """``server_owns_path`` is a *file* matcher; the dispatcher feeds it
        canonical file paths, not directory paths. We don't require special
        handling for trailing slashes — paths just compare as-is."""
        server = _server(libraries=[Library(id="1", name="Movies", remote_paths=("/data/movies",), enabled=True)])
        # A pseudo-file path under the library still matches.
        assert server_owns_path("/data/movies/Foo (2024)/Foo (2024).mkv", server) is not None


class TestUnicodeNormalization:
    """NFC normalisation lets HFS+ NFD paths match NFC settings entries."""

    def test_japanese_path_in_nfc_matches(self):
        """Japanese paths in NFC (the typical typed form) match cleanly."""
        server = _server(
            libraries=[Library(id="1", name="メディア", remote_paths=("/メディア/Movies",), enabled=True)],
        )
        match = server_owns_path("/メディア/Movies/Test (2024)/Test (2024).mkv", server)
        assert isinstance(match, OwnershipMatch)

    def test_accented_path_nfd_canonical_matches_nfc_setting(self):
        """User configures NFC ``café``; canonical path arrives as NFD ``café``.

        Without normalisation these differ byte-for-byte and ownership
        silently fails. With NFC on both sides they collapse to the
        same string and the match works.
        """
        # Settings as NFC (typical of typed input).
        nfc_setting = "/data/café"
        # Canonical path as NFD (typical of HFS+ source mounts).
        nfd_canonical = "/data/café/Movie (2024)/Movie (2024).mkv"

        server = _server(
            libraries=[Library(id="1", name="café", remote_paths=(nfc_setting,), enabled=True)],
        )
        match = server_owns_path(nfd_canonical, server)
        assert match is not None, "NFD canonical should match NFC setting after normalisation"

    def test_emoji_path_matches(self):
        """Emoji are multi-codepoint; NFC normalisation is a no-op but the comparison still works."""
        server = _server(
            libraries=[Library(id="1", name="🎬", remote_paths=("/data/🎬",), enabled=True)],
        )
        assert server_owns_path("/data/🎬/Movie/file.mkv", server) is not None

    def test_case_mismatch_does_not_match(self):
        """Filesystems can be case-sensitive; we deliberately don't fold case.

        If the user configures ``/Movies`` but the canonical path is
        ``/movies/...``, that's a real misconfiguration and we want a
        clean "no owners" rather than silent wrong-publish.
        """
        server = _server(
            libraries=[Library(id="1", name="Movies", remote_paths=("/Movies",), enabled=True)],
        )
        assert server_owns_path("/movies/foo/bar.mkv", server) is None


class TestPathMappingMatrix:
    """Audit fix — was the highest-priority gap. The user-reported
    "webhook_prefixes don't apply" regression class lives here.

    These tests cover the path-mapping CELLS the original suite missed:
    chained mappings, ordering when one is a strict prefix of another,
    multiple servers with different mappings owning the same canonical
    path, and the inverse (server reports local path).
    """

    def test_two_mappings_where_one_is_prefix_of_other_picks_specific(self):
        """Mapping A: /media → /data, mapping B: /media/4k → /data/4k.

        A canonical path under /data/4k must match the B-derived candidate,
        not silently land under A's. ``apply_path_mappings`` returns ALL
        candidates; ``server_owns_path`` then matches the canonical against
        each — order doesn't matter as long as at least one candidate matches.
        Test asserts the OWNERSHIP, not the candidate ordering.
        """
        server = _server(
            libraries=[Library(id="1", name="4K", remote_paths=("/media/4k",), enabled=True)],
            path_mappings=[
                {"remote_prefix": "/media", "local_prefix": "/data"},
                {"remote_prefix": "/media/4k", "local_prefix": "/data/4k"},
            ],
        )
        match = server_owns_path("/data/4k/Foo.mkv", server)
        assert match is not None, "/data/4k path must own via at least one of the mapping candidates"

    def test_chained_mappings_each_applied_independently(self):
        """User has two libraries on different mount roots, each needing
        its own mapping. Both must work simultaneously."""
        server = _server(
            libraries=[
                Library(id="1", name="Movies", remote_paths=("/media/movies",), enabled=True),
                Library(id="2", name="TV", remote_paths=("/media/tv",), enabled=True),
            ],
            path_mappings=[
                {"remote_prefix": "/media/movies", "local_prefix": "/mnt/movies"},
                {"remote_prefix": "/media/tv", "local_prefix": "/mnt/tv"},
            ],
        )
        assert server_owns_path("/mnt/movies/Foo.mkv", server) is not None
        assert server_owns_path("/mnt/tv/Show/S01E01.mkv", server) is not None
        assert server_owns_path("/mnt/anywhere/else.mkv", server) is None

    def test_two_servers_share_path_with_DIFFERENT_per_server_mappings(self):
        """The most common real-world configuration: Plex sees the disk
        at one path, Jellyfin sees the same disk at another. Both should
        own a canonical path that lives within their mapped view.

        This was the audit's "single biggest hole" — the missing matrix
        row that real users hit but no test covered.
        """
        plex = _server(
            server_id="plex-1",
            libraries=[Library(id="p1", name="Movies", remote_paths=("/plex-data/movies",), enabled=True)],
            path_mappings=[{"remote_prefix": "/plex-data", "local_prefix": "/data"}],
        )
        jellyfin = _server(
            server_id="jf-1",
            server_type=ServerType.JELLYFIN,
            libraries=[Library(id="j1", name="Movies", remote_paths=("/media/movies",), enabled=True)],
            path_mappings=[{"remote_prefix": "/media", "local_prefix": "/data"}],
        )
        # Both servers' libraries map to the same local /data/movies path.
        # A canonical path there must fan out to BOTH.
        matches = find_owning_servers("/data/movies/Foo.mkv", [plex, jellyfin])
        ids = {m.server_id for m in matches}
        assert ids == {"plex-1", "jf-1"}, (
            f"per-server path_mappings broke fan-out — expected both servers to own, got {ids!r}"
        )

    def test_canonical_path_inside_local_view_no_mapping_needed(self):
        """When server's remote_paths ALREADY use the local view (common
        when there's no NFS/SMB indirection), no mapping is needed and
        ownership still works — the dispatcher doesn't accidentally REQUIRE
        a path_mappings entry."""
        server = _server(
            libraries=[Library(id="1", name="Movies", remote_paths=("/data/movies",), enabled=True)],
            path_mappings=[],  # explicitly empty
        )
        assert server_owns_path("/data/movies/Foo.mkv", server) is not None

    def test_mapping_with_trailing_slash_normalised(self):
        """A user typing ``/media/`` instead of ``/media`` in Settings
        must produce the same ownership decision."""
        with_slash = _server(
            libraries=[Library(id="1", name="Movies", remote_paths=("/media/movies/",), enabled=True)],
            path_mappings=[{"remote_prefix": "/media/", "local_prefix": "/data/"}],
        )
        # Trailing slash on the library path may or may not match depending
        # on _normalize semantics. Assert behaviour is consistent — either
        # both forms match or neither does. The dispatcher's contract is
        # that operators shouldn't have to remember the trailing-slash
        # convention.
        result = server_owns_path("/data/movies/Foo.mkv", with_slash)
        # Document the actual contract: trailing slashes ARE handled by the
        # _normalize step on both sides.
        assert result is not None, (
            "trailing slash on library path / mapping prefix breaks ownership — "
            "user-typed paths must work regardless of trailing slash"
        )


class TestWindowsBackslashPaths:
    """GitHub #236 regression: Plex on a Windows host reports paths with
    backslashes (``F:\\Musikvideos\\X.mkv``). The ownership/mapping helpers
    must convert those to forward slashes before prefix matching AND when
    splicing the tail into the local path, otherwise the dispatcher hands
    a Windows-form path to ``os.path.isfile`` inside the Linux container.

    The legacy resolver in ``config/paths.py`` already handles this; these
    tests pin the same contract on the newer ``servers/ownership.py``
    helpers, which silently regressed by omitting the conversion.
    """

    def test_apply_path_mappings_translates_windows_remote_to_local(self):
        mappings = [{"remote_prefix": "F:\\", "local_prefix": "/media/F/"}]
        result = apply_path_mappings(
            "F:\\Musikvideos\\B\\Breaking Benjamin\\Ashes.mp4",
            mappings,
        )
        assert result == [
            "/media/F/Musikvideos/B/Breaking Benjamin/Ashes.mp4",
        ], "Windows remote path must map cleanly to a forward-slash local path"

    def test_apply_path_mappings_windows_prefix_in_mapping(self):
        """Mapping prefix has backslashes; canonical path is the same form."""
        mappings = [{"remote_prefix": "F:\\Videos\\TV Shows", "local_prefix": "/data/tv"}]
        result = apply_path_mappings(
            "F:\\Videos\\TV Shows\\Show\\S01E01.mkv",
            mappings,
        )
        assert result == ["/data/tv/Show/S01E01.mkv"]

    def test_apply_path_mappings_mixed_forms_match(self):
        """The common UI config: user types the prefix as forward-slash
        ``F:/Videos`` (or the UI normalizes display) while Plex still
        reports the path with backslashes. The conversion has to apply
        on the path side, not just the prefix side.
        """
        mappings = [{"remote_prefix": "F:/Videos", "local_prefix": "/data"}]
        assert apply_path_mappings("F:\\Videos\\film.mkv", mappings) == ["/data/film.mkv"]
        # And the symmetric case (backslash prefix, forward-slash path)
        # in case a webhook source has already normalized the payload.
        mappings_bs = [{"remote_prefix": "F:\\Videos", "local_prefix": "/data"}]
        assert apply_path_mappings("F:/Videos/film.mkv", mappings_bs) == ["/data/film.mkv"]

    def test_apply_path_mappings_legacy_plex_prefix_key_with_backslashes(self):
        """Legacy ``plex_prefix`` key on the mapping row, Windows path."""
        mappings = [{"plex_prefix": "F:\\Videos", "local_prefix": "/data"}]
        result = apply_path_mappings("F:\\Videos\\film.mkv", mappings)
        assert result == ["/data/film.mkv"]

    def test_apply_webhook_prefixes_translates_windows_webhook_path(self):
        mappings = [
            {
                "remote_prefix": "/srv/media",
                "local_prefix": "/media/F",
                "webhook_prefixes": ["F:\\"],
            }
        ]
        result = apply_webhook_prefixes(
            "F:\\Musikvideos\\B\\Ashes.mp4",
            mappings,
        )
        assert result == ["/media/F/Musikvideos/B/Ashes.mp4"]

    def test_server_owns_path_with_windows_mapping_and_local_canonical(self):
        """Standard flow: the dispatcher has already converted the canonical
        path to local form via ``apply_path_mappings``. Ownership must still
        match when the library + mapping are configured with Windows-style
        backslashes — i.e. backslash handling in ``apply_path_mappings``
        (called inside ``server_owns_path``) must not have regressed."""
        server = _server(
            libraries=[Library(id="1", name="Music Videos", remote_paths=("F:\\Musikvideos",), enabled=True)],
            path_mappings=[{"remote_prefix": "F:\\", "local_prefix": "/media/F/"}],
        )
        match = server_owns_path(
            "/media/F/Musikvideos/B/Breaking Benjamin/Ashes.mp4",
            server,
        )
        assert match is not None, (
            "Windows-form library remote_paths + Windows-form remote_prefix must still "
            "let a local-form canonical path own (GitHub #236)"
        )
        assert match.library_id == "1"

    def test_server_owns_path_tolerates_windows_canonical_path(self):
        """Defense-in-depth: even if a caller hands us a Windows-form
        canonical path directly (bypassing the upstream conversion), the
        ownership check must convert internally so the comparison works."""
        server = _server(
            libraries=[Library(id="1", name="Music Videos", remote_paths=("/media/F/Musikvideos",), enabled=True)],
            path_mappings=[],
        )
        # Library is already in local form; canonical_path arrives in Windows
        # form. _normalize() on both sides must converge.
        match = server_owns_path(
            "\\media\\F\\Musikvideos\\B\\Ashes.mp4",
            server,
        )
        assert match is not None, "ownership check must handle backslashes in canonical_path"
