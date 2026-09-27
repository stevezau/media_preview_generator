"""The Inspector's search-row words: quality from a file's name, Intro & Credits state, titles and a show's episodes."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from media_preview_generator.inspector.statuses import (
    BOTH,
    CREDITS_ONLY,
    CREDITS_SET,
    INTRO_ONLY,
    NEEDS_REVIEW,
    NOT_CHECKED,
    NOTHING_FOUND,
    file_title,
    markers_state,
    quality_from_name,
    show_seasons,
)
from media_preview_generator.markers.decide import DecisionStatus, TypeDecision
from media_preview_generator.markers.external_ids import is_extra
from media_preview_generator.markers.models import FileIdentity, Marker, MarkerType
from media_preview_generator.markers.store import MarkerStore

T = MarkerType
INTRO = Marker(T.INTRO, 10_000, 40_000, ("chapters",))
CREDITS = Marker(T.CREDITS, 1_200_000, 1_300_000, ("chapters",))


@pytest.fixture
def store(tmp_path):
    s = MarkerStore(str(tmp_path / "markers.db"), clock=lambda: datetime(2026, 9, 14, tzinfo=UTC))
    yield s
    s.close()


def _decided(marker: Marker) -> TypeDecision:
    return TypeDecision(marker.type, DecisionStatus.DECIDED, marker, None, "agreed")


def _not_decided(mtype: MarkerType, status: DecisionStatus) -> TypeDecision:
    return TypeDecision(mtype, status, None, None, status.value)


def _known(store: MarkerStore, path: str, decisions: dict | None = None, *, is_movie: bool = False):
    rec = store.upsert_file(FileIdentity(path, 100, 200), duration_ms=1_300_000, season_key=None, is_movie=is_movie)
    if decisions:
        store.save_decisions(rec.id, decisions, settings_fingerprint="f")
    return rec


class TestQualityFromName:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Film (2020) [Bluray-2160p].mkv", "2160p"),
            ("Film.2020.4K.WEB-DL.mkv", "2160p"),
            ("Film UHD.mkv", "2160p"),
            ("film.2160P.mkv", "2160p"),
            ("Film.2020.1080p.WEB-DL.mkv", "1080p"),
            ("Film.2020.1080i.HDTV.mkv", "1080p"),
            ("Film.2020.720p.mkv", "720p"),
            ("Film.576p.mkv", "576p"),
            ("Film.480i.mkv", "480p"),
            ("Film (2020) [Bluray-2160p][DV HDR10].mkv", "2160p Dolby Vision"),
            ("Film.DoVi.mkv", "Dolby Vision"),
            ("Film.Dolby.Vision.mkv", "Dolby Vision"),
            ("Film [DolbyVision].mkv", "Dolby Vision"),
            ("Film.2160p.HDR10+.mkv", "2160p HDR10+"),
            ("Film.HDR10Plus.mkv", "HDR10+"),
            ("Film.1080p.HDR10.mkv", "1080p HDR10"),
            ("Film.HDR.mkv", "HDR"),
            ("Film.HLG.mkv", "HLG"),
            ("Film (1999).mkv", ""),
            # "DVD" and "HDRip" carry DV and HDR inside a longer word: neither is Dolby Vision nor HDR.
            ("Film.DVD.mkv", ""),
            ("Film.DVDRip.mkv", ""),
            ("Film.HDRip.mkv", ""),
            ("Film.720p.HDRip.mkv", "720p"),
        ],
    )
    def test_quality_from_name_when_named(self, name, expected):
        assert quality_from_name(f"/media/movies/Film/{name}") == expected

    def test_quality_from_name_reads_only_the_file_name_not_its_folders(self):
        assert quality_from_name("/media/4K HDR Movies/Film (2020)/Film (2020).mkv") == ""


class TestMarkersState:
    PATH = "/media/tv/Show/Season 01/Show - S01E01.mkv"

    def test_no_record_is_not_checked(self, store):
        assert markers_state(store, self.PATH) == {"state": "not_checked", "label": NOT_CHECKED}

    def test_record_without_decisions_is_not_checked(self, store):
        _known(store, self.PATH)

        assert markers_state(store, self.PATH) == {"state": "not_checked", "label": NOT_CHECKED}

    def test_needs_review_wins_over_decided(self, store):
        _known(
            store,
            self.PATH,
            {
                T.INTRO: _decided(INTRO),
                T.CREDITS: _not_decided(T.CREDITS, DecisionStatus.NEEDS_REVIEW),
            },
        )

        assert markers_state(store, self.PATH) == {"state": "needs_review", "label": NEEDS_REVIEW}

    def test_intro_and_credits_decided_is_both(self, store):
        _known(store, self.PATH, {T.INTRO: _decided(INTRO), T.CREDITS: _decided(CREDITS)})

        assert markers_state(store, self.PATH) == {"state": "both", "label": BOTH}

    def test_credits_only_on_a_movie_is_credits_set(self, store):
        path = "/media/movies/Film (2020)/Film (2020).mkv"
        _known(
            store,
            path,
            {T.CREDITS: _decided(CREDITS), T.INTRO: _not_decided(T.INTRO, DecisionStatus.NO_EVIDENCE)},
            is_movie=True,
        )

        assert markers_state(store, path) == {"state": "credits", "label": CREDITS_SET}

    def test_credits_only_on_an_episode_is_credits_only(self, store):
        _known(
            store,
            self.PATH,
            {T.CREDITS: _decided(CREDITS), T.INTRO: _not_decided(T.INTRO, DecisionStatus.NO_EVIDENCE)},
            is_movie=False,
        )

        assert markers_state(store, self.PATH) == {"state": "credits", "label": CREDITS_ONLY}

    def test_intro_only(self, store):
        _known(
            store,
            self.PATH,
            {T.INTRO: _decided(INTRO), T.CREDITS: _not_decided(T.CREDITS, DecisionStatus.NO_EVIDENCE)},
        )

        assert markers_state(store, self.PATH) == {"state": "intro", "label": INTRO_ONLY}

    def test_only_no_evidence_or_disabled_is_nothing_found(self, store):
        _known(
            store,
            self.PATH,
            {
                T.INTRO: _not_decided(T.INTRO, DecisionStatus.NO_EVIDENCE),
                T.CREDITS: _not_decided(T.CREDITS, DecisionStatus.NO_EVIDENCE),
                T.PREVIEW: _not_decided(T.PREVIEW, DecisionStatus.DISABLED),
            },
        )

        assert markers_state(store, self.PATH) == {"state": "none", "label": NOTHING_FOUND}


class TestFileTitle:
    def test_movie_title_is_its_folder_without_id_tags(self):
        path = "/media/movies/The Matrix (1999) {tmdb-603}/The Matrix (1999) {tmdb-603} [Bluray-1080p].mkv"

        assert file_title(path) == "The Matrix (1999)"

    def test_episode_in_a_season_folder_is_show_and_code(self):
        path = "/media/tv/Blood Legacy (2024) {tvdb-123}/Season 01/Blood Legacy (2024) - S01E01 - Pilot.mkv"

        assert file_title(path) == "Blood Legacy (2024) · S01E01"

    def test_episode_in_a_flat_show_folder_is_show_and_code(self):
        path = "/media/tv/Show (2020) [imdb-tt1234]/Show - S02E13.mkv"

        assert file_title(path) == "Show (2020) · S02E13"

    def test_file_at_the_root_falls_back_to_its_own_name(self):
        assert file_title("/Film (2020) {tmdb-1}.mkv") == "Film (2020)"


class TestShowSeasons:
    @pytest.fixture
    def show(self, tmp_path):
        def touch(rel: str) -> str:
            p = tmp_path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x")
            return str(p)

        files = {
            "s1e2": touch("disk1/Show/Season 01/Show - S01E02.mkv"),
            "s1e1": touch("disk1/Show/Season 01/Show - S01E01.mkv"),
            "trailer": touch("disk1/Show/Season 01/Show - S01E01-trailer.mkv"),
            "subs": touch("disk1/Show/Season 01/Show - S01E01.srt"),
            "s2e1": touch("disk1/Show/Season 02/Show - S02E01.mkv"),
            "s2bonus": touch("disk1/Show/Season 02/Bonus Reel.mkv"),
            "s0e1": touch("disk1/Show/Specials/Show - S00E01.mkv"),
            # The same show on a second disk: its Season 01 episode joins the first disk's Season 1.
            "s1e3": touch("disk2/Show/Season 01/Show - S01E03.mkv"),
        }
        folders = [str(tmp_path / "disk1" / "Show"), str(tmp_path / "disk2" / "Show")]
        return folders, files

    def test_trailer_file_is_an_extra(self, show):
        _folders, files = show
        assert is_extra(files["trailer"])

    def test_seasons_in_order_with_specials_last(self, show, store):
        folders, _files = show

        seasons = show_seasons(folders, store)

        assert [(s["season"], s["label"]) for s in seasons] == [(1, "Season 1"), (2, "Season 2"), (0, "Specials")]

    def test_episodes_ordered_merged_across_disks_without_extras(self, show, store):
        folders, files = show

        season_1 = show_seasons(folders, store)[0]

        assert [(e["code"], e["episode"], e["path"]) for e in season_1["episodes"]] == [
            ("E01", 1, files["s1e1"]),
            ("E02", 2, files["s1e2"]),
            ("E03", 3, files["s1e3"]),
        ]

    def test_episode_without_a_number_takes_its_folders_season_and_its_name_as_code(self, show, store):
        folders, files = show

        season_2 = show_seasons(folders, store)[1]

        assert [(e["code"], e["episode"], e["path"]) for e in season_2["episodes"]] == [
            ("E01", 1, files["s2e1"]),
            ("Bonus Reel", None, files["s2bonus"]),
        ]

    def test_each_episode_carries_its_markers_state(self, show, store):
        folders, files = show
        _known(store, files["s1e2"], {T.INTRO: _decided(INTRO), T.CREDITS: _decided(CREDITS)})

        by_path = {e["path"]: e["markers"] for s in show_seasons(folders, store) for e in s["episodes"]}

        assert by_path[files["s1e2"]] == {"state": "both", "label": BOTH}
        assert by_path[files["s1e1"]] == {"state": "not_checked", "label": NOT_CHECKED}

    def test_flat_show_folder_holds_its_episodes_itself(self, tmp_path, store):
        show = tmp_path / "Flat Show"
        show.mkdir()
        for name in ("Flat Show - S01E02.mkv", "Flat Show - S01E01.mkv"):
            (show / name).write_bytes(b"x")

        seasons = show_seasons([str(show)], store)

        assert [(s["season"], [e["code"] for e in s["episodes"]]) for s in seasons] == [(1, ["E01", "E02"])]

    def test_same_folder_listed_twice_lists_each_episode_once(self, show, store):
        folders, _files = show

        seasons = show_seasons([folders[0], folders[0]], store)

        assert [e["code"] for e in seasons[0]["episodes"]] == ["E01", "E02"]
