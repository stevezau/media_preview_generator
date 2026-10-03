"""A season kept on several disks of one library (spec §5.3): the season group is the same show's season folders under
every folder of each enabled server's library that holds the file. The same show is told by the ids its folder name
carries ({tvdb-…}), or, with none in common, by its name. sflix's TV library spans four disks and 8,058 of its seasons
are split across them; Lioness S02E08, alone on one disk, matched nothing until its folders were merged (with them:
63.3-122.7 s, which all 7 other episodes agree with, as IntroDB does)."""

from __future__ import annotations

import errno
import os
import re
import zlib

import numpy as np
import pytest

from media_preview_generator.markers.audio import POINT_S, season
from media_preview_generator.markers.decide import DecisionStatus, TypeDecision
from media_preview_generator.markers.models import FileIdentity, MarkerType, Source
from media_preview_generator.markers.pipeline import DetectorUnavailableError
from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.servers.base import Library, ServerType
from tests.markers.audio.test_season import DUR, INTRO, N_POINTS, SEASON_RAW, _Audio, _spec
from tests.markers.fakes import FakeRegistry, ready_publisher, server_config
from tests.markers.test_pipeline import _ctx, _run

SHOW = "Lioness (2023) {tvdb-1}"
OFFSETS = {e: at for e, at in zip(range(1, 9), (510, 300, 720, 95, 610, 410, 880, 160), strict=True)}


def _library(*folders: str, sid: str = "plex-1", stype: ServerType = ServerType.PLEX, **kwargs):
    return server_config(sid, stype, libraries=[Library("1", "TV Shows", tuple(folders))], **kwargs)


def _episodes(root, season_no: int, *episodes: int, show: str = SHOW, season_folder: str | None = None) -> list[str]:
    folder = root / show / (season_folder or f"Season {season_no:02d}")
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for e in episodes:
        path = folder / f"{show.split(' {')[0]} - S{season_no:02d}E{e:02d}.mkv"
        path.write_bytes(b"x" * (100 + e))
        paths.append(str(path))
    return paths


@pytest.fixture
def store(tmp_path):
    s = MarkerStore(str(tmp_path / "markers.db"))
    yield s
    s.close()


@pytest.fixture
def disks(tmp_path):
    """Two disks of one "TV Shows" library, under the tmp "media" folder."""
    roots = [tmp_path / "media" / f"disk{n}" / "TV Shows" for n in (1, 2)]
    for root in roots:
        root.mkdir(parents=True)
    return roots


class TestSeasonFolders:
    def test_the_same_season_folder_on_another_disk_of_the_library_is_listed_after_the_files_own(self, disks):
        (e8,) = _episodes(disks[0], 2, 8)
        others = _episodes(disks[1], 2, 1, 2)
        folders = season.season_folders(e8, [_library(str(disks[0]), str(disks[1]))])
        assert folders == (os.path.dirname(e8), os.path.dirname(others[0]))

    @pytest.mark.parametrize("asked", [0, 1], ids=["from-disk1", "from-disk2"])
    def test_either_disk_lists_both(self, disks, asked):
        paths = [_episodes(disks[0], 1, 1)[0], _episodes(disks[1], 1, 2)[0]]
        folders = season.season_folders(paths[asked], [_library(str(disks[0]), str(disks[1]))])
        assert folders == (os.path.dirname(paths[asked]), os.path.dirname(paths[1 - asked]))

    @pytest.mark.parametrize(
        ("show", "other_show", "same"),
        [
            (SHOW, "Lioness {tvdb-1}", True),  # the same tvdb id under another name
            (SHOW, "Lioness (2023) [tvdbid-1]", True),  # Emby's tag style
            (SHOW, "Special Ops Lioness (2023) {tvdb-1} {tmdb-9}", True),  # tvdb names the show
            (SHOW, "Lioness (2023) {tvdb-2}", False),  # same name, another show's id
            (SHOW, "Lioness (2023) {tmdb-5}", False),  # another key: tvdb for one, tmdb for the other
            (SHOW, "LIONESS  (2023)", False),  # an id for one, a name for the other
            ("Lioness (2023)", "LIONESS  (2023)", True),  # no ids at all: the names, case and spacing aside
            ("Lioness (2023)", "Lioness (2024)", False),
        ],
    )
    def test_the_same_show_is_told_by_its_id_else_by_its_name(self, disks, show, other_show, same):
        (e1,) = _episodes(disks[0], 1, 1, show=show)
        (e2,) = _episodes(disks[1], 1, 2, show=other_show)
        configs = [_library(str(disks[0]), str(disks[1]))]
        both = (os.path.dirname(e1), os.path.dirname(e2))
        assert season.season_folders(e1, configs) == (both if same else both[:1])
        assert season.season_folders(e2, configs) == (both[::-1] if same else both[1:])

    @pytest.mark.parametrize("name", ["Season 2", "S02", "Season 002", "Staffel 2"])
    def test_a_season_folder_named_another_way_is_the_same_season(self, disks, name):
        (e1,) = _episodes(disks[0], 2, 1)
        (e2,) = _episodes(disks[1], 2, 2, season_folder=name)
        _episodes(disks[1], 3, 1)  # another season of the show on that disk
        folders = season.season_folders(e1, [_library(str(disks[0]), str(disks[1]))])
        assert folders == (os.path.dirname(e1), os.path.dirname(e2))

    def test_a_second_folder_of_the_season_on_the_files_own_disk_counts_too(self, disks):
        (e1,) = _episodes(disks[0], 1, 1)
        (e2,) = _episodes(disks[0], 1, 2, season_folder="Season 1")
        assert season.season_folders(e1, [_library(str(disks[0]))]) == (os.path.dirname(e1), os.path.dirname(e2))

    def test_a_show_kept_without_season_folders_takes_the_same_show_folder_elsewhere(self, disks):
        e1, e2 = str(disks[0] / SHOW / "Lioness - S01E01.mkv"), str(disks[1] / SHOW / "Lioness - S01E02.mkv")
        for path in (e1, e2):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "wb").close()
        folders = season.season_folders(e1, [_library(str(disks[0]), str(disks[1]))])
        assert folders == (os.path.dirname(e1), os.path.dirname(e2))

    def test_shows_below_a_category_folder_are_looked_for_at_the_same_place(self, disks):
        (e1,) = _episodes(disks[0] / "Drama", 1, 1)
        (e2,) = _episodes(disks[1] / "Drama", 1, 2)
        _episodes(disks[1], 1, 3)  # the same show directly in the library folder is another place
        folders = season.season_folders(e1, [_library(str(disks[0]), str(disks[1]))])
        assert folders == (os.path.dirname(e1), os.path.dirname(e2))

    def test_another_season_or_no_folder_there_adds_nothing(self, disks):
        (e1,) = _episodes(disks[0], 2, 1)
        _episodes(disks[1], 1, 1)  # the show is on disk 2, but only its first season
        assert season.season_folders(e1, [_library(str(disks[0]), str(disks[1]))]) == (os.path.dirname(e1),)

    def test_a_file_under_no_library_or_a_disabled_server_keeps_its_own_folder(self, disks, tmp_path):
        (e1,) = _episodes(disks[0], 1, 1)
        _episodes(disks[1], 1, 2)
        own = (os.path.dirname(e1),)
        assert season.season_folders(e1, []) == own
        assert season.season_folders(e1, [_library(str(tmp_path / "elsewhere"), str(disks[1]))]) == own
        assert season.season_folders(e1, [_library(str(disks[0]), str(disks[1]), enabled=False)]) == own

    def test_only_the_libraries_holding_the_file_count_and_theirs_add_up(self, disks, tmp_path):
        third = tmp_path / "media" / "disk3" / "TV Shows"
        (e1,) = _episodes(disks[0], 1, 1)
        (e2,) = _episodes(disks[1], 1, 2)
        (e3,) = _episodes(third, 1, 3)
        configs = [
            _library(str(disks[0]), str(disks[1])),
            _library(str(disks[0]), str(third), sid="jf-1", stype=ServerType.JELLYFIN),
            _library(str(disks[1]), str(third), sid="emby-1", stype=ServerType.EMBY),  # doesn't hold e1
        ]
        assert season.season_folders(e1, configs) == tuple(os.path.dirname(p) for p in (e1, e2, e3))

    def test_every_disk_finds_the_same_folders_when_libraries_hold_different_disks(self, disks, tmp_path):
        # Plex holds disks 1 and 2, Jellyfin disks 1 and 3: asked from disk 2 or 3 alone, the other would be missed, and
        # each episode's signature would name another group (so each run would ask for the others again).
        third = tmp_path / "media" / "disk3" / "TV Shows"
        paths = [_episodes(disk, 1, n)[0] for n, disk in enumerate((disks[0], disks[1], third), 1)]
        configs = [
            _library(str(disks[0]), str(disks[1])),
            _library(str(disks[0]), str(third), sid="jf-1", stype=ServerType.JELLYFIN),
        ]
        wanted = {os.path.dirname(p) for p in paths}
        for path in paths:
            folders = season.season_folders(path, configs)
            assert folders[0] == os.path.dirname(path) and set(folders) == wanted

    @pytest.mark.parametrize(
        ("code", "passing"),
        [
            (errno.ESTALE, True),
            (errno.EIO, True),
            (errno.ENOTCONN, True),
            (errno.ETIMEDOUT, True),
            (errno.EHOSTDOWN, True),
            (errno.EACCES, False),  # no permission lasts: the folder holds nothing of the season, logged
            (errno.ENOTDIR, False),
        ],
    )
    def test_only_a_folder_that_cant_be_read_for_now_is_reported(
        self, disks, monkeypatch, loguru_caplog, code, passing
    ):
        (e1,) = _episodes(disks[0], 1, 1)
        _episodes(disks[1], 1, 2)
        real_listdir = os.listdir

        def failing(folder):
            if str(folder) == str(disks[1]):
                raise OSError(code, os.strerror(code))
            return real_listdir(folder)

        monkeypatch.setattr(season.os, "listdir", failing)
        unreadable: set[str] = set()
        configs = [_library(str(disks[0]), str(disks[1]))]
        for _ in range(2):
            assert season.season_folders(e1, configs, unreadable) == (os.path.dirname(e1),)
        assert unreadable == ({str(disks[1])} if passing else set())
        warned = [r for r in loguru_caplog.records if "can't list" in r.getMessage()]
        assert len(warned) == (1 if code == errno.EACCES else 0)  # once, however often it is asked

    def test_a_missing_library_folder_is_not_unreadable(self, disks, tmp_path):
        (e1,) = _episodes(disks[0], 1, 1)
        unreadable: set[str] = set()
        folders = season.season_folders(e1, [_library(str(disks[0]), str(tmp_path / "gone"))], unreadable)
        assert folders == (os.path.dirname(e1),) and unreadable == set()

    def test_path_mappings_give_the_library_folders_on_this_side(self, disks):
        (e1,) = _episodes(disks[0], 1, 1)
        (e2,) = _episodes(disks[1], 1, 2)
        mappings = [
            {"remote_prefix": "/tv", "local_prefix": str(disks[0])},
            {"remote_prefix": "/tv", "local_prefix": str(disks[1])},
        ]
        cfg = server_config("plex-1", ServerType.PLEX, libraries=[Library("1", "TV Shows", ("/tv",))])
        cfg.path_mappings = mappings
        assert season.season_folders(e1, [cfg]) == (os.path.dirname(e1), os.path.dirname(e2))

    def test_a_library_folder_inside_or_around_the_one_holding_the_file_is_not_another_disk(self, tmp_path):
        tv = tmp_path / "media" / "tv"
        anime = tv / "Anime"
        (e1,) = _episodes(anime, 1, 1, show="Show {tvdb-1}")
        (_same_name,) = _episodes(tv, 1, 2, show="Show {tvdb-1}")  # tv/Show {tvdb-1}/Season 01: another place
        (_nested,) = _episodes(anime / "Anime", 1, 3, show="Show {tvdb-1}")
        folders = season.season_folders(e1, [_library(str(tv), str(anime))])
        assert folders == (os.path.dirname(e1),)

    def test_a_path_that_isnt_normal_keeps_its_own_folder(self, disks):
        (e1,) = _episodes(disks[0], 1, 1)
        _episodes(disks[1], 1, 2)
        odd = e1.replace(f"{os.sep}{SHOW}{os.sep}", f"{os.sep}{SHOW}{os.sep}..{os.sep}{SHOW}{os.sep}")
        assert ".." in odd.split(os.sep)
        assert season.season_folders(odd, [_library(str(disks[0]), str(disks[1]))]) == (os.path.dirname(odd),)

    def test_a_file_directly_in_a_library_folder_keeps_its_own(self, disks):
        e1 = str(disks[0] / "Loose - S01E01.mkv")
        open(e1, "wb").close()
        open(disks[1] / "Other - S01E01.mkv", "wb").close()
        assert season.season_folders(e1, [_library(str(disks[0]), str(disks[1]))]) == (str(disks[0]),)

    def test_a_disk_that_links_to_one_already_listed_is_not_listed_twice(self, disks, tmp_path):
        (e1,) = _episodes(disks[0], 1, 1)
        link = tmp_path / "media" / "disk1-link"
        link.symlink_to(disks[0])
        assert season.season_folders(e1, [_library(str(disks[0]), str(link))]) == (os.path.dirname(e1),)

    def test_the_root_folder_is_never_a_disk(self, disks):
        (e1,) = _episodes(disks[0], 1, 1)
        assert season.season_folders(e1, [_library("/", str(disks[0]))]) == (os.path.dirname(e1),)

    def test_a_same_named_file_in_place_of_a_folder_is_left_out(self, disks):
        (e1,) = _episodes(disks[0], 1, 1)
        (disks[1] / SHOW).write_bytes(b"not a folder")
        assert season.season_folders(e1, [_library(str(disks[0]), str(disks[1]))]) == (os.path.dirname(e1),)

    @pytest.mark.parametrize("flat", [False, True])
    def test_a_file_carrying_the_shows_id_beside_its_folder_is_not_a_folder_of_it(self, disks, flat):
        folder = disks[0] / SHOW
        folder.mkdir()
        e1 = str(folder / "Lioness - S01E01.mkv") if flat else _episodes(disks[0], 1, 1)[0]
        open(e1, "ab").close()
        (disks[1] / "Lioness {tvdb-1}.nfo").write_bytes(b"<tvshow/>")
        (disks[1] / "Lioness (2023) [tvdbid=1].jpg").write_bytes(b"\xff\xd8")
        unreadable: set[str] = set()
        folders = season.season_folders(e1, [_library(str(disks[0]), str(disks[1]))], unreadable)
        assert folders == (os.path.dirname(e1),) and unreadable == set()


class TestSeasonGroupAcrossDisks:
    def test_the_group_and_its_size_take_every_disks_episodes_sorted(self, disks):
        (e8,) = _episodes(disks[0], 2, 8)
        others = _episodes(disks[1], 2, 1, 2, 3)
        configs = [_library(str(disks[0]), str(disks[1]))]
        videos = season.season_videos(e8, configs)
        assert season.season_group(e8, videos) == season.SeasonGroup(os.path.dirname(e8), tuple(sorted([e8, *others])))
        assert season.season_size(e8, videos) == 4
        assert season.groups_holding(e8, videos) == tuple(sorted(others))

    def test_the_previous_season_is_read_from_every_disk_by_episode_number(self, disks):
        (s2e1,) = _episodes(disks[0], 2, 1)
        near = _episodes(disks[0], 1, 3, 6)
        far = _episodes(disks[1], 1, 1, 2, 4, 5)
        configs = [_library(str(disks[0]), str(disks[1]))]
        assert season.previous_season_files(s2e1, configs) == (far[0], far[1], near[0], far[2])
        assert season.previous_season_files(s2e1) == tuple(near)  # without the library: its own show folder only
        odd = s2e1.replace(f"{os.sep}{SHOW}{os.sep}", f"{os.sep}{SHOW}{os.sep}..{os.sep}{SHOW}{os.sep}")
        assert season.previous_season_files(odd, configs) == ()  # a path not in normal form reads nothing else


def _points(path: str) -> np.ndarray:
    episode = int(re.search(r"S\d\dE(\d\d)", path).group(1))
    body = np.random.default_rng(zlib.crc32(path.encode())).integers(0, 2**32, size=N_POINTS, dtype=np.uint64)
    body = body.astype("<u4")
    body[OFFSETS[episode] : OFFSETS[episode] + 240] = INTRO
    return body


def _planted_ms(path: str) -> tuple[int, int]:
    at = OFFSETS[int(re.search(r"S\d\dE(\d\d)", path).group(1))]
    return round(at * POINT_S * 1000), round((at + 239) * POINT_S * 1000)


class TestSeasonAudioAcrossDisks:
    """The Lioness shape: E08 alone on disk 1, E01-E07 on disk 2."""

    @pytest.fixture
    def lioness(self, disks, store):
        (e8,) = _episodes(disks[0], 2, 8)
        others = _episodes(disks[1], 2, *range(1, 8))
        registry = FakeRegistry({"plex-1": _library(str(disks[0]), str(disks[1]))})
        ctx = _ctx(store, registry, detectors=(_spec(),), settings_raw=SEASON_RAW)
        return e8, others, ctx

    def test_the_episode_alone_on_its_disk_matches_the_other_disks_episodes(self, lioness, store):
        e8, others, ctx = lioness
        with _Audio(points=_points) as audio:
            _run(ctx, e8, {"plex-1": ready_publisher()}, stage="process")
        assert sorted(audio.computed) == sorted([e8, *others])
        (cand,) = [c for c in store.get_evidence(store.get_file(e8).id) if c.source is Source.SEASON_AUDIO]
        start, end = _planted_ms(e8)
        assert abs(cand.start_ms - start) <= 500 and abs(cand.end_ms - end) <= 500
        assert (cand.origin, cand.confidence) == ("7/7", 1.0)
        # The previous season is asked only for an episode alone in its group.
        assert not [c for c in store.get_evidence(store.get_file(e8).id) if c.source is Source.SEASON_AUDIO_PREVIOUS]

    def test_an_episode_on_the_other_disk_counts_the_lone_one_too(self, lioness, store):
        e8, others, ctx = lioness
        with _Audio(points=_points):
            _run(ctx, others[0], {"plex-1": ready_publisher()}, stage="process")
        (cand,) = [c for c in store.get_evidence(store.get_file(others[0]).id) if c.source is Source.SEASON_AUDIO]
        assert cand.origin == "7/7"
        assert store.get_file(e8) is not None and store.get_fingerprint(store.get_file(e8).id, "intro") is not None

    def test_an_answer_made_before_the_other_disks_episode_arrived_is_asked_again(self, disks, store):
        others = _episodes(disks[1], 2, *range(1, 8))
        registry = FakeRegistry({"plex-1": _library(str(disks[0]), str(disks[1]))})
        ctx = _ctx(store, registry, detectors=(_spec(),), settings_raw=SEASON_RAW)
        with _Audio(points=_points):
            _run(ctx, others[0], {"plex-1": ready_publisher()}, stage="process")
            assert ctx.take_followups() == []
            (e8,) = _episodes(disks[0], 2, 8)
            _run(ctx, e8, {"plex-1": ready_publisher()}, stage="process")
        assert ctx.take_followups() == [others[0]]

    def test_a_run_asks_again_for_a_sibling_on_the_other_disk_but_not_for_the_previous_season(self, disks, store):
        (e8,) = _episodes(disks[0], 2, 8)
        (e1,) = _episodes(disks[1], 2, 1)
        (s1e1,) = _episodes(disks[1], 1, 1)
        registry = FakeRegistry({"plex-1": _library(str(disks[0]), str(disks[1]))})
        ctx = _ctx(store, registry, detectors=(_spec(),), settings_raw=SEASON_RAW)
        recs = {}
        for path in (e8, e1, s1e1):
            st = os.stat(path)
            recs[path] = store.upsert_file(
                FileIdentity(path, st.st_size, st.st_mtime_ns), duration_ms=DUR, season_key=None, is_movie=False
            )
            store.set_detector_run(recs[path].id, Source.SEASON_AUDIO, "older")
            undecided = TypeDecision(MarkerType.INTRO, DecisionStatus.NO_EVIDENCE, None, None, "sources disagree")
            store.save_decisions(recs[path].id, {MarkerType.INTRO: undecided}, settings_fingerprint="x")
        configs = registry.configs()
        videos = season.season_videos(e8, configs)
        group = season.season_group(e8, videos)
        assert group.episodes == tuple(sorted([e8, e1]))

        season._request_redecide(
            ctx,
            recs[e8],
            {e1: recs[e1], s1e1: recs[s1e1]},
            "newer",
            matched={},
            group=group,
            videos=videos,
            configs=configs,
        )

        assert ctx.take_followups() == [e1]

    def test_the_stored_signature_names_every_disks_episodes(self, lioness, store):
        e8, others, ctx = lioness
        with _Audio(points=_points):
            _run(ctx, e8, {"plex-1": ready_publisher()}, stage="process")
            answer = store.get_detector_run(store.get_file(e8).id, Source.SEASON_AUDIO)
            group = season.SeasonGroup(os.path.dirname(e8), tuple(sorted([e8, *others])))
            assert answer == season._signature(ctx, season._signature_paths(e8, group, ctx.registry.configs()))
            assert season.season_audio_answer_outdated(ctx, e8) is False
            os.utime(others[3], ns=(1, 1))  # a sibling on the other disk changed
            assert season.season_audio_answer_outdated(ctx, e8) is True

    def test_compared_counts_a_fingerprinted_episode_on_the_other_disk(self, lioness, store):
        e8, others, ctx = lioness
        with _Audio(points=_points):
            _run(ctx, others[0], {"plex-1": ready_publisher()}, stage="process")
        rec = store.upsert_file(
            FileIdentity(e8, *(lambda st: (st.st_size, st.st_mtime_ns))(os.stat(e8))),
            duration_ms=DUR,
            season_key=None,
            is_movie=False,
        )
        assert season.season_audio_compared(rec, ctx) is True


class TestLibrariesOnDifferentDisks:
    def test_runs_on_every_disk_agree_on_the_season_and_ask_nothing_again(self, disks, store, tmp_path):
        # Plex holds disks 1 and 2, Jellyfin disks 1 and 3: each episode's run must build the same group, or each run's
        # signatures of its siblings differ from their stored answers and it asks for them again, every run.
        third = tmp_path / "media" / "disk3" / "TV Shows"
        paths = [*_episodes(disks[0], 2, 1, 2, 3), *_episodes(disks[1], 2, 4, 5), *_episodes(third, 2, 6, 7, 8)]
        registry = FakeRegistry(
            {
                "plex-1": _library(str(disks[0]), str(disks[1])),
                "jf-1": _library(str(disks[0]), str(third), sid="jf-1", stype=ServerType.JELLYFIN),
            }
        )
        ctx = _ctx(store, registry, detectors=(_spec(),), settings_raw=SEASON_RAW)
        publishers = {"plex-1": ready_publisher(), "jf-1": ready_publisher()}
        with _Audio(points=_points):
            for path in paths:
                _run(ctx, path, publishers, stage="process")
            ctx.take_followups()
            for path in (paths[3], paths[5]):  # one on each disk only one library holds
                _run(ctx, path, publishers, stage="process")
                assert ctx.take_followups() == []
        origins = {
            c.origin for p in paths for c in store.get_evidence(store.get_file(p).id) if c.source is Source.SEASON_AUDIO
        }
        assert origins == {"7/7"}


def _record(store, path: str):
    st = os.stat(path)
    return store.upsert_file(
        FileIdentity(path, st.st_size, st.st_mtime_ns), duration_ms=DUR, season_key=None, is_movie=False
    )


class TestUnreadableDisk:
    def test_a_season_disk_that_cant_be_read_holds_season_audio_until_it_can(self, disks, store, monkeypatch):
        (e8,) = _episodes(disks[0], 2, 8)
        _episodes(disks[1], 2, *range(1, 8))
        registry = FakeRegistry({"plex-1": _library(str(disks[0]), str(disks[1]))})
        ctx = _ctx(store, registry, detectors=(_spec(),), settings_raw=SEASON_RAW)
        with _Audio(points=_points):
            _run(ctx, e8, {"plex-1": ready_publisher()}, stage="process")
            rec = store.get_file(e8)
            answer = store.get_detector_run(rec.id, Source.SEASON_AUDIO)
            (e9,) = _episodes(disks[0], 2, 9)  # the season changed: e8's answer is out of date
            assert season.season_audio_due(rec, ctx) is True
            real_listdir, real_scandir = os.listdir, os.scandir

            def stale(real):
                def call(folder):
                    if str(folder).startswith(str(disks[1])):
                        raise OSError(116, "Stale file handle")
                    return real(folder)

                return call

            with monkeypatch.context() as broken:
                broken.setattr(season.os, "listdir", stale(real_listdir))
                broken.setattr(season.os, "scandir", stale(real_scandir))
                assert season.season_audio_due(rec, ctx) is False
                assert season.season_audio_needs_worker(rec, ctx) is False
                assert season.season_audio_followups(rec, ctx) == []
                assert season.season_audio_answer_outdated(ctx, e8) is False
                assert season.season_audio_compared(rec, ctx) is False
                with pytest.raises(DetectorUnavailableError, match="can't be read") as unreadable:
                    season.detect_season_audio(rec, ctx=ctx)
                assert unreadable.value.this_file is False  # the siblings' disk, not this file: not a failed read
                assert store.get_detector_run(rec.id, Source.SEASON_AUDIO) == answer
            assert season.season_audio_due(rec, ctx) is True
            assert e9 in season.season_group(e8, season.season_videos(e8, ctx.registry.configs())).episodes

    @pytest.mark.parametrize("stored", [None, 41_000])
    def test_a_season_disk_that_cant_be_read_keeps_the_episodes_chapter_limit(self, disks, store, monkeypatch, stored):
        (e8,) = _episodes(disks[0], 2, 8)
        _episodes(disks[1], 2, *range(1, 8))
        registry = FakeRegistry({"plex-1": _library(str(disks[0]), str(disks[1]))})
        ctx = _ctx(store, registry, detectors=(_spec(),), settings_raw=SEASON_RAW)
        rec = _record(store, e8)
        if stored is not None:
            store.set_intro_chapter_limit(rec.id, stored)
        real_listdir = os.listdir

        def stale(folder):
            if str(folder).startswith(str(disks[1])):
                raise OSError(errno.ESTALE, "Stale file handle")
            return real_listdir(folder)

        monkeypatch.setattr(season.os, "listdir", stale)
        assert season.season_intro_chapter_limits(ctx, e8) == (stored, {})

    def test_the_wait_is_logged_once_however_often_it_is_asked(self, disks, store, monkeypatch, loguru_caplog):
        (e8,) = _episodes(disks[0], 2, 8)
        _episodes(disks[1], 2, *range(1, 8))
        registry = FakeRegistry({"plex-1": _library(str(disks[0]), str(disks[1]))})
        ctx = _ctx(store, registry, detectors=(_spec(),), settings_raw=SEASON_RAW)
        rec = _record(store, e8)
        real_listdir = os.listdir

        def stale(folder):
            if str(folder).startswith(str(disks[1])):
                raise OSError(errno.ESTALE, "Stale file handle")
            return real_listdir(folder)

        monkeypatch.setattr(season.os, "listdir", stale)
        for _ in range(3):
            assert season.season_audio_due(rec, ctx) is False
        assert sum("waits for" in r.getMessage() for r in loguru_caplog.records) == 1
