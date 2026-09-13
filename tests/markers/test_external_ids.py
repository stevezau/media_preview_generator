"""Tests for media_preview_generator.markers.external_ids (spec §5.2)."""

from __future__ import annotations

import pytest

from media_preview_generator.markers.external_ids import ids_from_path, ids_from_server_dict, merge_ids
from media_preview_generator.markers.models import MediaIds


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (
            "/data_16tb2/TV Shows/Rick and Morty (2013) {tvdb-275274}/Season 01/"
            "Rick and Morty (2013) - S01E01 - Pilot [WEBDL-1080p].mkv",
            MediaIds("episode", tvdb="275274", season=1, episode=1),
        ),
        (
            "/data_16tb/Movies/Toy Story (1995) {tmdb-862}/Toy Story (1995) {imdb-tt0114709} - [Bluray-2160p].mkv",
            MediaIds("movie", tmdb="862", imdb="tt0114709"),
        ),
        (
            "/m/Shows/Lost [tvdbid-73739] [imdbid-tt0411008]/Season 2/Lost S02E03.mkv",
            MediaIds("episode", tvdb="73739", imdb="tt0411008", season=2, episode=3),
        ),
        ("/m/Movies/Up (2009) [tmdbid-14160]/Up (2009).mkv", MediaIds("movie", tmdb="14160")),
        # episode-level ids in a file name are NOT series ids and must be ignored
        (
            "/m/TV/Show {tvdb-1}/Season 01/Show - S01E02 {imdb-tt9999999}.mkv",
            MediaIds("episode", tvdb="1", season=1, episode=2),
        ),
        ("/m/TV/Show/Season 03/show.s03e10.1080p.mkv", MediaIds("episode", season=3, episode=10)),
        ("/m/home videos/birthday.mkv", MediaIds("unknown")),
    ],
)
def test_ids_from_path(path, expected):
    assert ids_from_path(path) == expected


def test_server_dict_normalises():
    raw = {"kind": "episode", "tmdb": 60625, "imdb": "tt2861424", "tvdb": None, "season": "1", "episode": 2}
    assert ids_from_server_dict(raw) == MediaIds("episode", tmdb="60625", imdb="tt2861424", season=1, episode=2)
    assert ids_from_server_dict(None) == MediaIds()
    assert ids_from_server_dict({"kind": "show"}) == MediaIds("unknown")


def test_merge_prefers_primary_fields_and_known_kind():
    primary = MediaIds("unknown", tvdb="275274", season=1, episode=1)
    fallback = MediaIds("episode", tmdb="60625", imdb="tt2861424", tvdb="999", season=9, episode=9)
    assert merge_ids(primary, fallback) == MediaIds(
        "episode", tmdb="60625", imdb="tt2861424", tvdb="275274", season=1, episode=1
    )


class TestIdsFromPathEdgeCases:
    """Additional precision-over-coverage cases: mutation-hardening for the regexes."""

    def test_year_like_number_is_not_mistaken_for_episode(self):
        # A bare 4-digit year in a filename must never parse as S/E.
        assert ids_from_path("/m/Movies/Toy Story (1995)/Toy Story (1995).mkv") == MediaIds("unknown")

    def test_multi_episode_file_uses_first_episode_number(self):
        # S01E01E02: the word-boundary lookahead stops at the first SxxEyy
        # token, so multi-episode files resolve to their first episode
        # rather than spilling into "E02" or matching nothing at all.
        path = "/m/TV/Show {tvdb-1}/Season 01/Show - S01E01E02 - Double.mkv"
        result = ids_from_path(path)
        assert result.kind == "episode"
        assert result.season == 1
        assert result.episode == 1

    def test_specials_season_00(self):
        path = "/m/TV/Show {tvdb-1}/Season 00/Show - S00E01 - Special.mkv"
        assert ids_from_path(path) == MediaIds("episode", tvdb="1", season=0, episode=1)

    def test_episode_filename_ids_never_used_for_series_id(self):
        # Filename-level ids belong to the episode, not the series — every
        # online source wants the series id, so a filename-only id must
        # never surface even when no folder id exists at all.
        path = "/m/TV/Show/Season 01/Show - S01E01 {tvdb-999}.mkv"
        result = ids_from_path(path)
        assert result.tvdb is None

    def test_episode_nearest_folder_wins_over_ancestor_folder(self):
        # Only the immediate show folder (here, the grandparent of the file
        # since the parent is a season folder) is ever consulted; an id
        # further up the tree is never reached at all.
        path = "/m/TV {tvdb-9}/Show {tvdb-1}/Season 01/Show - S01E01.mkv"
        result = ids_from_path(path)
        assert result.tvdb == "1"

    def test_episode_ignores_ids_above_the_immediate_show_folder(self):
        # RULING (fix round 1, HIGH/MED review): a franchise-level folder
        # above a spin-off's own show folder must never leak its id down —
        # every online source wants THIS show's id, not an ancestor's.
        path = "/m/TV/Franchise {tvdb-1}/Spin-off/Season 01/Spin-off - S01E01.mkv"
        result = ids_from_path(path)
        assert result.tvdb is None

    def test_specials_folder_recognised_as_season_like(self):
        path = "/m/TV/Show {tvdb-1}/Specials/Show - S00E01 - Special.mkv"
        assert ids_from_path(path) == MediaIds("episode", tvdb="1", season=0, episode=1)

    def test_movie_filename_id_wins_over_conflicting_folder_id(self):
        # For movies the filename is checked first; a same-kind id in the
        # parent folder only fills in keys the filename didn't provide.
        path = "/m/Movies/X {tmdb-1}/X {tmdb-2}.mkv"
        result = ids_from_path(path)
        assert result.tmdb == "2"

    def test_four_digit_season_supported_for_daily_dated_numbering(self):
        # RULING (fix round 1): season/episode widened to \d{1,4} so
        # date-numbered shows like Doctor Who's "S2005E01" parse correctly.
        path = "/m/TV/Doctor Who {tvdb-78804}/Doctor Who - S2005E01.mkv"
        assert ids_from_path(path) == MediaIds("episode", tvdb="78804", season=2005, episode=1)

    def test_season_number_wider_than_four_digits_does_not_match(self):
        # s(\d{1,4}) caps season width at 4 digits; a 5-digit season token
        # doesn't match SxxEyy at all, so the path degrades to "unknown"
        # rather than guessing a season number outside the supported range.
        path = "/m/TV/Show {tvdb-1}/Season 01/show.s12345e45.mkv"
        assert ids_from_path(path) == MediaIds("unknown")

    def test_sxxeyy_requires_word_boundary_not_embedded_in_longer_digits(self):
        # "s10e100" adjacent digits must not spill into a following/leading number.
        assert ids_from_path("/m/TV/Show/Season 10/show.s10e100.mkv") == MediaIds("episode", season=10, episode=100)

    def test_sxxeyy_lookbehind_blocks_match_when_preceded_by_alphanumeric(self):
        # A bare "s01e02" glued directly onto a preceding letter/digit (no
        # separator) must not be mistaken for a season/episode marker.
        path = "/m/TV/Show {tvdb-1}/Season 01/show.xs01e02.mkv"
        result = ids_from_path(path)
        assert result.season is None
        assert result.episode is None

    def test_sxxeyy_lookahead_rejects_five_digit_trailing_run(self):
        # "e12345" has more than the supported 4 episode digits; the trailing
        # lookahead must reject the whole token rather than truncating to a
        # plausible-looking but wrong 4-digit episode number.
        path = "/m/TV/Show {tvdb-1}/Season 01/show.s01e12345.mkv"
        result = ids_from_path(path)
        assert result.episode is None

    def test_id_braces_and_brackets_both_accepted(self):
        assert ids_from_path("/m/Movies/X {tmdb-1}/X.mkv") == MediaIds("movie", tmdb="1")
        assert ids_from_path("/m/Movies/X [tmdb-1]/X.mkv") == MediaIds("movie", tmdb="1")

    def test_imdb_id_lowercased(self):
        assert ids_from_path("/m/Movies/X {imdb-TT0114709}/X.mkv") == MediaIds("movie", imdb="tt0114709")

    def test_season_folder_variants(self):
        for folder in ("Season 01", "Series 01", "Staffel 01", "Saison 01"):
            path = f"/m/TV/Show {{tvdb-1}}/{folder}/Show - S01E01.mkv"
            assert ids_from_path(path) == MediaIds("episode", tvdb="1", season=1, episode=1)

    def test_absolute_numbered_anime_style_not_parsed_as_season_episode(self):
        # "- 012 -" absolute numbering (e.g. One Piece) has no SxxEyy token.
        # RULING (fix round 1, MED-2): a tvdb token nearby means this is
        # almost certainly TV data whose episode number we can't parse —
        # refuse to guess "movie" from the folder's id; return MediaIds()
        # in full, not just "season/episode unknown".
        path = "/m/Anime/One Piece {tvdb-81797}/One Piece - 1071 - Title.mkv"
        assert ids_from_path(path) == MediaIds()

    def test_tmdb_and_tvdb_not_swapped(self):
        path = "/m/TV/Show {tvdb-111}/Season 01/Show - S01E01.mkv"
        result = ids_from_path(path)
        assert result.tvdb == "111"
        assert result.tmdb is None

    def test_windows_style_backslash_path(self):
        path = "M:\\TV\\Show {tvdb-1}\\Season 01\\Show - S01E01.mkv"
        result = ids_from_path(path)
        assert result == MediaIds("episode", tvdb="1", season=1, episode=1)

    def test_empty_path_returns_unknown(self):
        assert ids_from_path("") == MediaIds()

    def test_movie_id_only_in_filename_not_folder(self):
        assert ids_from_path("/m/Movies/Up (2009)/Up (2009) {tmdb-14160}.mkv") == MediaIds("movie", tmdb="14160")


class TestMergeIds:
    def test_fallback_kind_used_when_primary_unknown(self):
        primary = MediaIds()
        fallback = MediaIds("movie", tmdb="1")
        assert merge_ids(primary, fallback).kind == "movie"

    def test_primary_kind_wins_but_missing_fields_still_fall_back(self):
        # merge_ids is field-wise: kind resolution and per-field fallback are
        # independent, so a known primary kind doesn't suppress fallback
        # values for fields the primary left unset.
        primary = MediaIds("movie")
        fallback = MediaIds("episode", season=1, episode=1)
        result = merge_ids(primary, fallback)
        assert result.kind == "movie"
        assert result.season == 1
        assert result.episode == 1

    def test_season_zero_from_primary_not_treated_as_falsy(self):
        # Season/episode 0 (specials) must not be overwritten by fallback
        # due to a truthiness check instead of an explicit None check.
        primary = MediaIds("episode", season=0, episode=0)
        fallback = MediaIds("episode", season=5, episode=5)
        result = merge_ids(primary, fallback)
        assert result.season == 0
        assert result.episode == 0

    def test_primary_wins_every_field_when_both_sides_set(self):
        # Fix round 1, LOW: pin merge order for every field at once — every
        # value differs between primary/fallback, so swapping any single
        # `x or y` to `y or x` (or any season/episode fallback) fails this.
        primary = MediaIds("movie", tmdb="1", imdb="tt1", tvdb="10", season=1, episode=1)
        fallback = MediaIds("episode", tmdb="2", imdb="tt2", tvdb="20", season=2, episode=2)
        result = merge_ids(primary, fallback)
        assert result == MediaIds("movie", tmdb="1", imdb="tt1", tvdb="10", season=1, episode=1)


class TestMovieClassificationRuling:
    """Fix round 1, MED-2: "movie" only when tmdb/imdb is present AND no tvdb is present anywhere
    in the considered folders; otherwise MediaIds() and let the server decide."""

    def test_date_based_daily_show_episode_returns_unknown(self):
        # No SxxEyy to parse and the filename shape itself (a bare date) is
        # not a movie convention — refuse to guess "movie" from the nearby id.
        path = "/m/TV/Show {tmdb-60625}/Show - 2024-01-15.mkv"
        assert ids_from_path(path) == MediaIds()

    def test_movie_classification_blocked_when_tvdb_also_present(self):
        # A tvdb token anywhere in the considered folders means this is
        # probably TV data whose SxxEyy failed to parse — never guess "movie".
        path = "/m/Movies/X {tmdb-1} {tvdb-2}/X.mkv"
        assert ids_from_path(path) == MediaIds()

    def test_movie_never_reports_tvdb_even_without_a_conflict(self):
        # Movies read tmdb/imdb only — tvdb is a different id space and is
        # never surfaced for a movie result (dropped, not merely deprioritised).
        assert ids_from_path("/m/Movies/X {tmdb-1}/X.mkv").tvdb is None

    def test_movie_ignores_a_collection_id_above_its_own_folder(self):
        # RULING (fix round 2, LOW/pin): a collection-level id sits at the
        # grandparent of the movie file; movies only ever read the filename
        # and immediate parent folder, so the collection's tmdb must never
        # leak down to a title that has no id of its own.
        path = "/m/Collection {tmdb-10194}/Toy Story 2 (1999)/Toy Story 2 (1999).mkv"
        assert ids_from_path(path) == MediaIds()


class TestIdFormatValidation:
    """Fix round 1, LOW: tmdb/tvdb are bare digits, imdb requires "tt"; a mismatched shape is
    ignored rather than accepted, and Emby-style "=" delimiters are supported."""

    def test_tmdb_with_tt_prefix_is_invalid_and_ignored(self):
        assert ids_from_path("/m/Movies/X {tmdb-tt0114709}/X.mkv") == MediaIds()

    def test_imdb_without_tt_prefix_is_invalid_and_ignored(self):
        assert ids_from_path("/m/Movies/X {imdb-0114709}/X.mkv") == MediaIds()

    def test_equals_sign_delimiter_supported_for_emby_style_ids(self):
        assert ids_from_path("/m/Movies/X [tmdbid=862]/X.mkv") == MediaIds("movie", tmdb="862")

    def test_emby_style_imdbid_and_tvdbid_with_equals(self):
        assert ids_from_path("/m/Movies/X [imdbid=tt0114709]/X.mkv") == MediaIds("movie", imdb="tt0114709")
        path = "/m/TV/Show [tvdbid=1]/Season 01/Show - S01E01.mkv"
        assert ids_from_path(path) == MediaIds("episode", tvdb="1", season=1, episode=1)

    def test_zero_ids_are_rejected_like_server_results(self):
        # RULING (fix round 2, LOW): a "0" id in a path must be rejected the
        # same way ids_from_server_dict already rejects a "0" server value.
        assert ids_from_path("/m/Movies/X {tmdb-0}/X.mkv") == MediaIds()
        assert ids_from_path("/m/Movies/X {imdb-tt0}/X.mkv") == MediaIds()
        assert ids_from_path("/m/Movies/X {tmdb-00}/X.mkv") == MediaIds()  # padded zero also rejected
        path = "/m/TV/Show {tvdb-0}/Season 01/Show - S01E01.mkv"
        assert ids_from_path(path) == MediaIds("episode", season=1, episode=1)


class TestExtrasExcluded:
    """Fix round 1, MED-4: Plex extra suffixes and extras folders never get ids."""

    def test_plex_trailer_suffix_returns_unknown(self):
        path = "/m/Movies/Toy Story (1995) {tmdb-862}/Toy Story (1995)-trailer.mkv"
        assert ids_from_path(path) == MediaIds()

    def test_all_plex_extra_suffixes_excluded(self):
        suffixes = (
            "trailer",
            "featurette",
            "behindthescenes",
            "deleted",
            "interview",
            "scene",
            "short",
            "other",
            "sample",
        )
        for suffix in suffixes:
            path = f"/m/Movies/X (2020) {{tmdb-1}}/X (2020)-{suffix}.mkv"
            assert ids_from_path(path) == MediaIds(), suffix
            assert ids_from_path(path.upper()) == MediaIds(), suffix  # case-insensitive

    def test_extras_folder_excludes_even_a_valid_looking_episode_file(self):
        path = "/m/TV/Show {tvdb-1}/Season 01/Extras/Show - S01E01 - Making Of.mkv"
        assert ids_from_path(path) == MediaIds()

    def test_all_extras_folder_names_excluded(self):
        # Uses an episode-shaped path (SxxEyy present) rather than a
        # movie-shaped one: a movie-branch id one folder above an extras
        # folder is unreachable either way (single-parent-folder rule), so
        # that shape can't tell "detected as extra" apart from "not
        # detected" for a single dropped folder name. Here, if the extras
        # folder isn't recognised, ids_from_path falls through to the
        # episode branch and reports season/episode anyway — an observable
        # difference from the fully-empty MediaIds() we expect.
        folders = (
            "Trailers",
            "Featurettes",
            "Extras",
            "Behind The Scenes",
            "Deleted Scenes",
            "Interviews",
            "Scenes",
            "Shorts",
            "Other",
            "Samples",
        )
        for folder in folders:
            path = f"/m/TV/Show {{tvdb-1}}/Season 01/{folder}/Show - S01E01.mkv"
            assert ids_from_path(path) == MediaIds(), folder

    def test_title_containing_trailer_word_not_treated_as_extra(self):
        # "Trailer Park" is a title, not the Plex "-trailer" suffix or an
        # exact "Trailers" folder match — must not be excluded.
        path = "/m/TV/Trailer Park Boys {tvdb-1}/Season 01/Trailer Park Boys - S01E01.mkv"
        assert ids_from_path(path) == MediaIds("episode", tvdb="1", season=1, episode=1)

    def test_extras_named_ancestor_above_the_id_folder_is_not_flagged(self):
        # RULING (fix round 2, LOW): only the immediate PARENT folder is
        # checked for an extras name — ids themselves only ever come from
        # the parent or the grandparent above a season folder, so an
        # ancestor further up sharing a name with a real-world library
        # category ("Other", "Shorts", ...) must never suppress a real
        # movie's ids. This was a real bug in the round-1 "any depth" check.
        path = "/media/Other/Movies/Movie (2020) {tmdb-1}/Movie (2020).mkv"
        assert ids_from_path(path) == MediaIds("movie", tmdb="1")
        path = "/data/Shorts/Movie (2020) {tmdb-1}/Movie (2020).mkv"
        assert ids_from_path(path) == MediaIds("movie", tmdb="1")

    def test_episode_title_containing_the_trailer_word_not_treated_as_extra(self):
        # RULING (fix round 2, LOW/pin): "S01E05 - The Trailer" is a real
        # episode title — the suffix check requires a hyphen immediately
        # before the keyword at the very end of the filename; "- The
        # Trailer" has a space there, not a hyphen.
        path = "/m/TV/Show {tvdb-1}/Season 01/Show - S01E05 - The Trailer.mkv"
        assert ids_from_path(path) == MediaIds("episode", tvdb="1", season=1, episode=5)

    def test_episode_title_containing_the_deleted_word_not_treated_as_extra(self):
        # RULING (fix round 2, LOW/pin): "Pre-Deleted World" has a hyphen
        # before "Deleted", but "Deleted" is not at the very end of the
        # filename — the suffix check is anchored there, so this must not match.
        path = "/m/TV/Show {tvdb-1}/Season 01/Show - S01E05 - Pre-Deleted World.mkv"
        assert ids_from_path(path) == MediaIds("episode", tvdb="1", season=1, episode=5)

    def test_show_title_containing_scenes_word_not_treated_as_extra(self):
        # RULING (fix round 2, LOW/pin): the show folder's own title
        # contains the word "Scenes" — the extras-folder check is an exact
        # match against the whole folder name, not a substring/contains check.
        path = "/m/TV/Scenes from a Marriage (1973) {tvdb-1}/Season 01/Scenes from a Marriage - S01E01.mkv"
        assert ids_from_path(path) == MediaIds("episode", tvdb="1", season=1, episode=1)


class TestIdsFromServerDict:
    def test_empty_string_values_treated_as_missing(self):
        assert ids_from_server_dict({"kind": "movie", "tmdb": ""}) == MediaIds("movie")

    def test_non_numeric_season_episode_become_none(self):
        raw = {"kind": "episode", "season": "not-a-number", "episode": None}
        result = ids_from_server_dict(raw)
        assert result.season is None
        assert result.episode is None

    def test_zero_and_whitespace_values_treated_as_missing_and_trimmed(self):
        # Fix round 1, LOW: rejects "0"/empty and trims whitespace.
        raw = {"kind": "movie", "tmdb": "0", "imdb": "  ", "tvdb": "  862  "}
        result = ids_from_server_dict(raw)
        assert result.tmdb is None
        assert result.imdb is None
        assert result.tvdb == "862"

    def test_unrecognised_kind_drops_all_fields_not_just_kind(self):
        # Fix round 1, MED-3: an unrecognised kind returns a fully empty
        # MediaIds — a partial dict with kind="unknown" but stray ids would
        # still be wrong, since nothing downstream should trust those ids.
        raw = {"kind": "show", "tmdb": "1", "imdb": "tt1", "tvdb": "1", "season": 1, "episode": 1}
        assert ids_from_server_dict(raw) == MediaIds()
