"""Season step: group listing, previous season, whole-season fingerprinting, inline matching, weekly releases, the
silence guard, degenerate-input cost, and the season's intro chapters (finding F1)."""

from __future__ import annotations

import os
import random
import re
import sqlite3
import subprocess
import threading
import time
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from media_preview_generator.markers import pipeline
from media_preview_generator.markers.audio import POINT_S, fingerprint, matcher, season
from media_preview_generator.markers.decide import LONG_INTRO_CHAPTER_REASON, DecisionStatus, TypeDecision
from media_preview_generator.markers.external_ids import ids_from_path
from media_preview_generator.markers.models import Candidate, FileIdentity, Marker, MarkerType, Source
from media_preview_generator.markers.outcomes import FileOutcome, kept_own_reason
from media_preview_generator.markers.probe import Chapter, MediaProbe, ProbeError, ProbeStalledError
from media_preview_generator.markers.sources.chapters import CHAPTER_RULES_VERSION, chapter_candidates
from media_preview_generator.markers.sources.online import LookupResult
from media_preview_generator.markers.store import CachedShare, EndPictureKey, MarkerStore
from media_preview_generator.servers.base import ServerType
from tests.markers.fakes import ready_publisher
from tests.markers.test_pipeline import EPISODE_IDS, MOVIE_IDS, _clients, _ctx, _item, _registry, _run

DUR = 1_321_472
N_POINTS = int(fingerprint.window_s(DUR) / POINT_S)
INTRO = np.random.default_rng(42).integers(0, 2**32, size=240, dtype=np.uint64).astype("<u4")
OFFSETS = {"S01E01": 300, "S01E02": 520, "S01E03": 710, "S01E04": 90, "S02E01": 400, "S02E02": 900, "S02E03": 150}
SEASON_RAW = {"sources": [{"id": "theintrodb", "enabled": False}], "detect": {"intro": True, "credits": False}}
SIL = season.SILENCE_POINT


def fake_points(path: str) -> np.ndarray:
    key = re.search(r"S\d\dE\d\d", path).group(0)
    rng = np.random.default_rng(zlib.crc32(key.encode()))
    body = rng.integers(0, 2**32, size=N_POINTS, dtype=np.uint64).astype("<u4")
    body[OFFSETS[key] : OFFSETS[key] + 240] = INTRO
    return body


def planted_ms(path: str) -> tuple[int, int]:
    at = OFFSETS[re.search(r"S\d\dE\d\d", path).group(0)]
    return round(at * POINT_S * 1000), round((at + 239) * POINT_S * 1000)


def noise(seed: int, size: int) -> np.ndarray:
    return np.random.default_rng(seed).integers(0, 2**32, size=size, dtype=np.uint64).astype("<u4")


def _alike(_candidate) -> bool:
    """An end-picture check whose pictures always match."""
    return True


@pytest.fixture
def store(tmp_path):
    s = MarkerStore(str(tmp_path / "markers.db"))
    yield s
    s.close()


@pytest.fixture
def show(tmp_path):
    root = tmp_path / "media" / "tv" / "Show (2020) {tvdb-1}"

    def make(season_no: int, episodes: int) -> list[str]:
        folder = root / f"Season {season_no:02d}"
        folder.mkdir(parents=True, exist_ok=True)
        paths = []
        for e in range(1, episodes + 1):
            p = folder / f"Show (2020) - S{season_no:02d}E{e:02d}.mkv"
            if not p.exists():  # adding an episode later must not touch the ones already there
                p.write_bytes(b"x" * (100 + e))
            paths.append(str(p))
        return paths

    return make


class _Audio:
    """Patches ffmpeg (fingerprints), ffprobe of other episodes, the chromaprint check and the end-picture decode (every
    pair of pictures alike unless ``share`` says otherwise)."""

    def __init__(self, fail: set[str] | None = None, points=fake_points, share=lambda *_args: 1.0):
        self.computed: list[str] = []
        self.fail = fail or set()
        self.points = points
        self.share = share
        self.compared: list[tuple] = []

    def _share(self, reader, target, partner, start_s, end_s, offset_s):
        self.compared.append((target, partner, start_s, end_s, offset_s))
        return self.share(target, partner, start_s, end_s, offset_s)

    def compute(self, path, duration_ms, *, ffmpeg, cancel_check=None):
        self.computed.append(path)
        if path in self.fail:
            raise fingerprint.FingerprintError("ffmpeg exited 1")
        return self.points(path)

    def __enter__(self):
        self._patches = [
            patch.object(fingerprint, "compute_fingerprint", side_effect=self.compute),
            patch.object(season, "probe_media", return_value=MediaProbe(DUR, ())),
            patch.object(season, "chromaprint_ffmpeg", return_value="/usr/lib/jellyfin-ffmpeg/ffmpeg"),
            patch.object(season.end_picture.Reader, "share", autospec=True, side_effect=self._share),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()


def _spec():
    with patch.object(season, "chromaprint_ffmpeg", return_value="/usr/lib/jellyfin-ffmpeg/ffmpeg"):
        return season.season_audio_spec("/usr/lib/jellyfin-ffmpeg/ffmpeg")


def _season_ctx(store, path, raw=SEASON_RAW, clients=None):
    return _ctx(store, _registry(path, ServerType.PLEX), detectors=(_spec(),), settings_raw=raw, clients=clients)


def _evidence(store, path, source):
    rec = store.get_file(path)
    return [c for c in store.get_evidence(rec.id) if c.source is source]


def _detect_and_store(ctx, rec):
    """The season audio detector on one file, its answer stored the way the pipeline stores it."""
    pipeline._run_detector(
        ctx, rec, _spec(), gpu=None, gpu_device_path=None, phase=lambda _t: None, cancel_check=None, pause_check=None
    )
    return [c for c in ctx.store.get_evidence(rec.id) if c.source is Source.SEASON_AUDIO]


def _introdb_answer(start_ms, end_ms):
    return _clients(introdb=LookupResult("ok", (Candidate(MarkerType.INTRO, start_ms, end_ms, Source.INTRODB),)))


class TestGroup:
    def test_episodes_of_the_folder_sorted_without_extras_or_other_files(self, show):
        e1, e2 = show(2, 2)
        folder = os.path.dirname(e1)
        for name in ("Show (2020) - S02E02-sample.mkv", "notes.txt", "Show - Making Of.mkv"):
            open(os.path.join(folder, name), "wb").close()
        os.mkdir(os.path.join(folder, "Extras"))
        assert season.season_group(e2) == season.SeasonGroup(folder, (e1, e2))

    def test_episodes_are_grouped_by_the_season_number_in_their_names(self, tmp_path):
        folder = tmp_path / "media" / "tv" / "Show {tvdb-1}"
        folder.mkdir(parents=True)
        names = ("Show - S01E01.mkv", "Show - S01E02.mkv", "Show - S02E01.mkv", "Show - 101.mkv", "Show - 102.mkv")
        s01e01, s01e02, s02e01, a101, a102 = (str(folder / n) for n in names)
        for path in (s01e01, s01e02, s02e01, a101, a102):
            Path(path).write_bytes(b"x")
        assert season.season_group(s01e02).episodes == (s01e01, s01e02)
        assert season.season_group(s02e01).episodes == (s02e01,)
        assert season.season_group(a102).episodes == (a101, a102)  # files without a season number are their own group

    @pytest.mark.parametrize(
        ("episode", "first", "last"),
        [(150, 130, 169), (1, 1, 40), (300, 261, 300), (21, 1, 40)],
    )
    def test_a_flat_folder_group_is_the_40_nearest_episodes(self, tmp_path, episode, first, last):
        folder = tmp_path / "media" / "anime" / "Show {tvdb-1}"
        folder.mkdir(parents=True)
        for e in range(1, 301):
            (folder / f"Show - S01E{e:03d}.mkv").write_bytes(b"x")
        group = season.season_group(str(folder / f"Show - S01E{episode:03d}.mkv"))
        # Ties go by name: E130 (distance 20) comes before E170 for E150, E1 before E41 for E21.
        assert group.episodes == tuple(str(folder / f"Show - S01E{e:03d}.mkv") for e in range(first, last + 1))

    def test_a_flat_folder_without_episode_numbers_takes_the_nearest_names(self, tmp_path):
        folder = tmp_path / "media" / "tv" / "Show {tvdb-1}"
        folder.mkdir(parents=True)
        paths = [str(folder / f"Show - Part {n:03d}.mkv") for n in range(100)]
        for path in paths:
            Path(path).write_bytes(b"x")
        assert season.season_group(paths[50]).episodes == tuple(paths[30:70])

    def test_previous_season_is_the_first_four_episodes_of_the_season_numbered_one_lower(self, show):
        s1 = show(1, 6)
        (s2e1,) = show(2, 1)
        assert season.previous_season_files(s2e1) == tuple(s1[:4])
        assert season.previous_season_files(s1[0]) == ()

    def test_previous_season_folder_with_other_padding_is_found(self, tmp_path):
        prev = tmp_path / "media" / "tv" / "Show {tvdb-1}" / "Season 1"
        cur = tmp_path / "media" / "tv" / "Show {tvdb-1}" / "Season 02"
        prev.mkdir(parents=True), cur.mkdir(parents=True)
        (prev / "Show - S01E01.mkv").write_bytes(b"x")
        (cur / "Show - S02E01.mkv").write_bytes(b"x")
        assert season.previous_season_files(str(cur / "Show - S02E01.mkv")) == (str(prev / "Show - S01E01.mkv"),)

    def test_specials_have_no_previous_season(self, tmp_path):
        folder = tmp_path / "media" / "tv" / "Show {tvdb-1}" / "Specials"
        folder.mkdir(parents=True)
        (folder / "Show - S00E01.mkv").write_bytes(b"x")
        assert season.previous_season_files(str(folder / "Show - S00E01.mkv")) == ()


class TestSeasonAudio:
    def test_first_episode_fingerprints_the_season_and_finds_its_intro(self, store, show):
        e1, e2, e3 = show(1, 3)
        ctx = _season_ctx(store, e1)
        with _Audio() as audio:
            out, _ = _run(ctx, e1, {"plex-1": ready_publisher()}, stage="process")
        assert sorted(audio.computed) == [e1, e2, e3]
        (cand,) = _evidence(store, e1, Source.SEASON_AUDIO)
        start, end = planted_ms(e1)
        assert abs(cand.start_ms - start) <= 500 and abs(cand.end_ms - end) <= 500
        assert cand.origin == "2/2" and cand.confidence == 1.0
        # Season audio decides an intro alone (owner 2026-09-24, overriding the 2026-09-14 ruling R2).
        assert out.outcome_key == FileOutcome.PUBLISHED.value
        assert ctx.take_followups() == []  # e2 and e3 never had an answer: their own runs match the whole season
        assert all(store.get_fingerprint(store.get_file(p).id, "intro") is not None for p in (e1, e2, e3))

    def test_the_rest_of_the_season_matches_on_the_checking_thread(self, store, show):
        e1, e2, e3 = show(1, 3)
        ctx = _season_ctx(store, e1)
        with _Audio() as audio:
            _run(ctx, e1, {"plex-1": ready_publisher()}, stage="process")
            ctx.take_followups()
            audio.computed.clear()
            out, _ = _run(ctx, e2, {"plex-1": ready_publisher()})  # check stage
        assert out is not None and audio.computed == []
        (cand,) = _evidence(store, e2, Source.SEASON_AUDIO)
        assert cand.origin == "2/2"
        assert ctx.take_followups() == []  # e1 already ran with this season's fingerprints; e3 never had an answer

    def test_a_lone_audio_match_publishes_the_intro(self, store, show):
        # "If it doesn't exist online then use the GPU/CPU check" (owner, 2026-09-24): no online source answers here.
        e1, _, _ = show(1, 3)
        pub = ready_publisher()
        with _Audio():
            out, _ = _run(_season_ctx(store, e1), e1, {"plex-1": pub}, stage="process")
        assert out.outcome_key == FileOutcome.PUBLISHED.value
        rec = store.get_file(e1)
        decision = store.get_decisions(rec.id)[MarkerType.INTRO]
        assert (decision.status, decision.reason) == (DecisionStatus.DECIDED, "single source (season_audio)")
        (cand,) = _evidence(store, e1, Source.SEASON_AUDIO)
        marker = Marker(MarkerType.INTRO, cand.start_ms, cand.end_ms, ("season_audio",))
        assert store.get_markers(rec.id)[MarkerType.INTRO] == marker
        assert pub.write.call_args.args[1] == [marker]

    def test_an_agreeing_online_source_publishes_the_audio_intro(self, store, show):
        e1, _, _ = show(1, 3)
        start, end = planted_ms(e1)
        pub = ready_publisher()
        ctx = _season_ctx(store, e1, SEASON_RAW, clients=_introdb_answer(start - 2_000, end + 3_000))
        with _Audio():
            out, _ = _run(ctx, e1, {"plex-1": pub}, stage="process")
        assert out.outcome_key == FileOutcome.PUBLISHED.value
        (cand,) = _evidence(store, e1, Source.SEASON_AUDIO)
        marker = store.get_markers(store.get_file(e1).id)[MarkerType.INTRO]
        # IntroDB ranks first in the user's order and sets the agreed end; season audio's later start shortens the skip.
        assert marker == Marker(MarkerType.INTRO, cand.start_ms, end + 3_000, ("introdb", "season_audio"))
        assert pub.write.call_args.args[1] == [marker]

    def test_a_current_answer_is_not_matched_again(self, store, show):
        e1, _, _ = show(1, 3)
        ctx = _season_ctx(store, e1, SEASON_RAW)
        with _Audio(), patch.object(season, "pair_runs", wraps=season.pair_runs) as runs:
            _run(ctx, e1, {"plex-1": ready_publisher()}, stage="process")
            first = runs.call_count
            _run(ctx, e1, {"plex-1": ready_publisher()}, stage="process")
        assert first == 2 and runs.call_count == 2

    def test_pairs_are_cached_the_same_way_round(self, store, show):
        e1, e2 = show(1, 2)
        ctx = _season_ctx(store, e1, SEASON_RAW)
        with _Audio():
            _run(ctx, e1, {"plex-1": ready_publisher()}, stage="process")
            with patch.object(season, "pair_runs", side_effect=AssertionError("recomputed")):
                _run(ctx, e2, {"plex-1": ready_publisher()})
        a, b = store.get_file(e1), store.get_file(e2)
        assert (
            store.get_season_pair(a.id, b.id, season.SEASON_AUDIO_VERSION) is not None
            and store.get_season_pair(b.id, a.id, season.SEASON_AUDIO_VERSION) is None
        )

    @pytest.mark.parametrize(
        ("decided_by", "locked", "asked"),
        [
            (("skipdb",), False, False),
            (("introdb", "season_audio"), False, True),
            (("introdb", "season_audio_previous"), False, True),
            (("introdb", "season_audio"), True, False),
        ],
        ids=["online", "season-audio", "previous-season-hint", "locked"],
    )
    def test_a_decided_sibling_is_asked_again_only_when_its_intro_rests_on_season_audio(
        self, store, show, decided_by, locked, asked
    ):
        e1, e2, e3 = show(1, 3)
        rec = store.upsert_file(FileIdentity(e2, *_identity(e2)), duration_ms=DUR, season_key=None, is_movie=False)
        decision = _decided(MarkerType.INTRO, 10_000, 40_000, decided_by)
        store.save_decisions(rec.id, {MarkerType.INTRO: decision}, settings_fingerprint="x")
        store.set_detector_run(rec.id, Source.SEASON_AUDIO, "an earlier season")
        if locked:
            store.lock_marker(rec.id, decision.marker)
        store.set_intro_chapter_limit(rec.id, None)  # no intro chapter: the chapter step has nothing to ask about
        ctx = _season_ctx(store, e1)
        with _Audio():
            _run(ctx, e1, {"plex-1": ready_publisher()}, stage="process")
        assert ctx.take_followups() == ([e2] if asked else [])  # e3 never had an answer

    def test_a_member_no_server_takes_is_never_queued_for_a_season_job(self, store, show):
        paths = show(1, 4)
        excluded = paths[2]
        registry = _registry(paths[0], ServerType.PLEX)
        registry.configs_by_id["plex-1"].exclude_paths.append({"value": r"S01E03", "type": "regex"})

        def job(items):
            ctx = _ctx(store, registry, settings_raw=SEASON_RAW, detectors=(_spec(),))
            for path in items:
                if _run(ctx, path, {"plex-1": ready_publisher()})[0] is None:
                    _run(ctx, path, {"plex-1": ready_publisher()}, stage="process")
            return [p for p in ctx.take_followups() if p not in items]

        with _Audio(points=_episode_noise) as audio:
            assert job([p for p in paths if p != excluded]) == []
            assert excluded in audio.computed  # it still counts in its siblings' matches
            (e5,) = show(1, 5)[4:]
            assert excluded not in job([e5])
            (e6,) = show(1, 6)[5:]
            assert excluded not in job([e6])

    def test_a_run_reads_its_season_once(self, store, show):
        e1, e2, e3 = show(1, 3)
        start, end = planted_ms(e1)
        ctx = _season_ctx(store, e1, SEASON_RAW, clients=_introdb_answer(start - 2_000, end + 3_000))
        with _Audio():
            for path, stage in ((e1, "process"), (e2, "check"), (e3, "check")):
                assert _run(ctx, path, {"plex-1": ready_publisher()}, stage=stage)[0] is not None
            ctx.take_followups()
            assert store.get_markers(store.get_file(e1).id)[MarkerType.INTRO].decided_by == ("introdb", "season_audio")
            (e4,) = show(1, 4)[3:]  # never seen: E2's answer is due and needs a worker
            with (
                patch.object(season, "folder_videos", wraps=season.folder_videos) as scans,
                patch.object(season, "season_group", wraps=season.season_group) as groups,
                patch.object(season, "_signature_item", wraps=season._signature_item) as items,
            ):
                # E1: decided with its season audio answer: chapter step, follow-ups hook, early-stop check.
                assert _run(ctx, e1, {"plex-1": ready_publisher()})[0] is None
                runs = {"E1": (scans.call_count, sorted(c.args[0] for c in groups.call_args_list))}
                assert sorted(c.args[1] for c in items.call_args_list) == [e1, e2, e3, e4]
                for mock in (scans, groups, items):
                    mock.reset_mock()
                # E2: undecided: chapter step, follow-ups hook, pending check, needs_worker.
                assert _run(ctx, e2, {"plex-1": ready_publisher()})[0] is None
                runs["E2"] = (scans.call_count, sorted(c.args[0] for c in groups.call_args_list))
                assert sorted(c.args[1] for c in items.call_args_list) == [e1, e2, e3, e4]
        assert runs == {"E1": (1, [e1, e2, e3, e4]), "E2": (1, [e1, e2, e3, e4])}

    def test_the_detector_is_registered_with_the_season_audio_source_and_version(self):
        spec = _spec()
        assert (spec.source, spec.types, spec.version) == (Source.SEASON_AUDIO, frozenset({MarkerType.INTRO}), 5)
        assert spec.stored_sources == {Source.SEASON_AUDIO, Source.SEASON_AUDIO_PREVIOUS}
        assert (spec.due, spec.needs_worker) == (season.season_audio_due, season.season_audio_needs_worker)

    def test_a_never_seen_sibling_needs_a_worker_and_a_fingerprinted_season_does_not(self, store, show):
        e1, e2 = show(1, 2)
        ctx = _season_ctx(store, e1)
        with _Audio():
            _run(ctx, e1, {"plex-1": ready_publisher()}, stage="process")
            assert season.season_audio_needs_worker(store.get_file(e1), ctx) is False
            (e3,) = show(1, 3)[2:]
            assert season.season_audio_needs_worker(store.get_file(e1), ctx) is True


def _identity(path):
    st = os.stat(path)
    return st.st_size, st.st_mtime_ns


EARLY_OFFSETS = {"S01E01": 80, "S01E02": 100, "S01E03": 120}  # 9.9, 12.4 and 14.9 s: all checked


def early_points(path: str) -> np.ndarray:
    key = re.search(r"S\d\dE\d\d", path).group(0)
    body = noise(zlib.crc32(key.encode()), N_POINTS)
    body[EARLY_OFFSETS[key] : EARLY_OFFSETS[key] + 240] = INTRO
    return body


def _early_ms(path: str) -> int:
    return round(EARLY_OFFSETS[re.search(r"S\d\dE\d\d", path).group(0)] * POINT_S * 1000)


class TestEndPictureCheck:
    """The season step's end-picture check in the app: decoded on a worker, its shares cached in markers.db."""

    def test_an_early_intro_needs_a_worker_until_its_end_picture_is_checked(self, store, show):
        e1, e2, e3 = show(1, 3)
        ctx = _season_ctx(store, e1)
        with _Audio(points=early_points) as audio:
            _run(ctx, e1, {"plex-1": ready_publisher()}, stage="process")  # fingerprints the season, checks e1
            assert {c[:2] for c in audio.compared} == {(e1, e2), (e1, e3)}
            ctx.take_followups()
            assert season.season_audio_needs_worker(store.get_file(e1), ctx) is False
            assert season.season_audio_needs_worker(store.get_file(e2), ctx) is True  # e2's own isn't checked
            audio.compared.clear()
            assert _run(ctx, e2, {"plex-1": ready_publisher()})[0] is None  # handed to a worker, nothing decoded
            assert audio.compared == []
            _run(ctx, e2, {"plex-1": ready_publisher()}, stage="process")
            assert {c[:2] for c in audio.compared} == {(e2, e1), (e2, e3)}
            assert season.season_audio_needs_worker(store.get_file(e2), ctx) is False
        (cand,) = _evidence(store, e2, Source.SEASON_AUDIO)
        assert abs(cand.start_ms - _early_ms(e2)) <= 500

    def test_the_check_compares_this_episodes_stretch_with_its_partners_at_their_aligned_offsets(self, store, show):
        e1, e2, e3 = show(1, 3)
        with _Audio(points=early_points) as audio:
            _run(_season_ctx(store, e1), e1, {"plex-1": ready_publisher()}, stage="process")
        (cand,) = _evidence(store, e1, Source.SEASON_AUDIO)
        by_partner = {partner: (start, end, offset) for _t, partner, start, end, offset in audio.compared}
        for partner in (e2, e3):
            start, end, offset = by_partner[partner]
            assert (round(start * 1000), round(end * 1000)) == (cand.start_ms, cand.end_ms)
            assert offset == pytest.approx((_early_ms(partner) - _early_ms(e1)) / 1000, abs=POINT_S)

    def test_a_checked_share_is_read_from_markers_db_and_not_decoded_again(self, store, show):
        e1, e2, e3 = show(1, 3)
        ctx = _season_ctx(store, e1)
        with _Audio(points=early_points, share=lambda *_a: 0.8) as audio:
            _run(ctx, e1, {"plex-1": ready_publisher()}, stage="process")
            (first,) = _evidence(store, e1, Source.SEASON_AUDIO)
            asked = list(audio.compared)
            rec = store.get_file(e1)
            _detect_and_store(ctx, rec)
        assert audio.compared == asked and len(asked) == 2
        (again,) = _evidence(store, e1, Source.SEASON_AUDIO)
        assert (again.start_ms, again.end_ms) == (first.start_ms, first.end_ms)
        for _t, partner, start, end, offset in asked:
            key = EndPictureKey(
                rec.id, store.get_file(partner).id, round(start * 1000), round(end * 1000), round(offset * 1000)
            )
            assert store.get_end_picture(key, season.end_picture.CHECK_VERSION) == CachedShare(0.8)

    @pytest.mark.parametrize(("shares", "found"), [((1.0, 0.5), True), ((0.8, 0.5), False)])
    def test_the_intro_holds_when_the_median_share_is_at_least_75_percent(self, store, show, shares, found):
        e1, e2, e3 = show(1, 3)
        by_partner = {e2: shares[0], e3: shares[1]}
        with _Audio(points=early_points, share=lambda _t, partner, *_a: by_partner[partner]):
            _run(_season_ctx(store, e1), e1, {"plex-1": ready_publisher()}, stage="process")
        assert bool(_evidence(store, e1, Source.SEASON_AUDIO)) is found

    def test_a_partner_that_cant_be_compared_is_stored_and_doesnt_count(self, store, show):
        e1, e2, e3 = show(1, 3)
        by_partner = {e2: None, e3: 0.9}
        with _Audio(points=early_points, share=lambda _t, partner, *_a: by_partner[partner]) as audio:
            _run(_season_ctx(store, e1), e1, {"plex-1": ready_publisher()}, stage="process")
        assert _evidence(store, e1, Source.SEASON_AUDIO)
        stored = [
            store.get_end_picture(
                EndPictureKey(
                    store.get_file(t).id, store.get_file(p).id, round(s * 1000), round(e * 1000), round(o * 1000)
                ),
                season.end_picture.CHECK_VERSION,
            )
            for t, p, s, e, o in audio.compared
        ]
        assert sorted(stored, key=lambda c: c.share is None) == [CachedShare(0.9), CachedShare(None)]

    def test_a_stalled_read_gives_no_answer_this_time_and_stores_nothing(self, store, show):
        e1, e2, e3 = show(1, 3)
        ctx = _season_ctx(store, e1)

        def stalled(*_args):
            raise season.end_picture.CheckUnavailableError("2 earlier ffprobes are still stuck")

        with _Audio(points=early_points, share=stalled):
            _run(ctx, e1, {"plex-1": ready_publisher()}, stage="process")
            rec = store.get_file(e1)
            assert _evidence(store, e1, Source.SEASON_AUDIO) == []
            assert store.get_detector_run(rec.id, Source.SEASON_AUDIO) is None
            assert season.season_audio_needs_worker(rec, ctx) is True  # still unchecked: the next run decodes

    def test_the_worker_s_gpu_and_the_job_s_cancel_reach_the_decode(self, store, show):
        e1, _e2, _e3 = show(1, 3)
        ctx = _season_ctx(store, e1)
        cancel = MagicMock(return_value=False)
        with _Audio(points=early_points), patch.object(season.end_picture, "Reader") as reader:
            reader.return_value.share.return_value = 1.0
            rec = store.upsert_file(FileIdentity(e1, *_identity(e1)), duration_ms=DUR, season_key=None, is_movie=False)
            pipeline._run_detector(
                ctx, rec, _spec(), gpu="NVIDIA", gpu_device_path="cuda:0", phase=lambda _t: None,
                cancel_check=cancel, pause_check=None,
            )  # fmt: skip
        assert reader.call_args.kwargs == {
            "ffmpeg": ctx.config.ffmpeg_path, "gpu": "NVIDIA", "gpu_device_path": "cuda:0", "cancel_check": cancel,
        }  # fmt: skip
        assert reader.return_value.share.call_count == 2

    def test_intros_after_the_first_30_s_are_never_decoded(self, store, show):
        e1, _e2, _e3 = show(1, 3)
        with _Audio() as audio:  # fake_points: the intros start at 37, 64 and 88 s
            _run(_season_ctx(store, e1), e1, {"plex-1": ready_publisher()}, stage="process")
        assert audio.compared == [] and _evidence(store, e1, Source.SEASON_AUDIO)


def _decided(mtype, start, end, decided_by=("skipdb",)):
    return TypeDecision(mtype, DecisionStatus.DECIDED, Marker(mtype, start, end, decided_by), None, "single source")


class TestWeeklyReleases:
    def _cache_previous(self, store, paths):
        for p in paths:
            st = os.stat(p)
            rec = store.upsert_file(FileIdentity(p, st.st_size, st.st_mtime_ns), duration_ms=DUR,
                                    season_key=os.path.dirname(p), is_movie=False)  # fmt: skip
            store.set_fingerprint(rec.id, size=rec.size, mtime_ns=rec.mtime_ns, window="intro", start_s=0.0,
                                  length_s=fingerprint.window_s(DUR), algorithm=1, points=fake_points(p).tobytes())  # fmt: skip

    def test_a_new_seasons_only_episode_gets_a_hint_from_the_cached_previous_season(self, store, show):
        s1 = show(1, 4)
        (s2e1,) = show(2, 1)
        self._cache_previous(store, s1)
        ctx = _season_ctx(store, s2e1)
        with _Audio() as audio:
            out, _ = _run(ctx, s2e1, {"plex-1": ready_publisher()}, stage="process")
        assert audio.computed == [s2e1]
        assert _evidence(store, s2e1, Source.SEASON_AUDIO) == []
        (hint,) = _evidence(store, s2e1, Source.SEASON_AUDIO_PREVIOUS)
        assert hint.origin == "4/4"
        start, end = planted_ms(s2e1)
        assert abs(hint.start_ms - start) <= 500 and abs(hint.end_ms - end) <= 500
        assert out.outcome_key == FileOutcome.NEEDS_REVIEW.value  # a hint needs a second source even at Medium
        # This episode is the matcher's first file in every pair (the order few_siblings.py measured), and the previous
        # season's episodes are never asked about again: they aren't this season's.
        new = store.get_file(s2e1)
        for path in s1:
            old = store.get_file(path)
            assert (
                store.get_season_pair(new.id, old.id, season.SEASON_AUDIO_VERSION) is not None
                and store.get_season_pair(old.id, new.id, season.SEASON_AUDIO_VERSION) is None
            )
        assert ctx.take_followups() == []

    def test_a_previous_season_file_replaced_while_it_is_matched_makes_the_hint_due_again(self, store, show):
        s1 = show(1, 4)
        (s2e1,) = show(2, 1)
        self._cache_previous(store, s1)
        ctx = _season_ctx(store, s2e1)
        real_runs = season.season_pair_runs

        def runs(a, b):
            if store.get_file(s1[0]).size < 500:
                _write(s1[0], 999)  # a new release of S01E01 lands and its own run records it
                store.upsert_file(FileIdentity(s1[0], *_identity(s1[0])), duration_ms=DUR,
                                  season_key=os.path.dirname(s1[0]), is_movie=False)  # fmt: skip
            return real_runs(a, b)

        with _Audio(), patch.object(season, "season_pair_runs", side_effect=runs):
            _run(ctx, s2e1, {"plex-1": ready_publisher()}, stage="process")
            (hint,) = _evidence(store, s2e1, Source.SEASON_AUDIO_PREVIOUS)
            assert hint.origin == "4/4"  # matched with the fingerprint S01E01 had
            assert season.season_audio_due(store.get_file(s2e1), ctx) is True

    def test_one_cached_episode_of_the_previous_season_is_enough_for_a_hint(self, store, show):
        self._cache_previous(store, show(1, 1))
        (s2e1,) = show(2, 1)
        with _Audio():
            _run(_season_ctx(store, s2e1), s2e1, {"plex-1": ready_publisher()}, stage="process")
        (hint,) = _evidence(store, s2e1, Source.SEASON_AUDIO_PREVIOUS)
        assert (hint.origin, hint.confidence) == ("1/1", 1.0)

    def test_a_hint_confirmed_by_an_online_source_publishes(self, store, show):
        self._cache_previous(store, show(1, 4))
        (s2e1,) = show(2, 1)
        start, end = planted_ms(s2e1)
        ctx = _season_ctx(store, s2e1, SEASON_RAW, clients=_introdb_answer(start, end - 1_000))
        with _Audio():
            out, _ = _run(ctx, s2e1, {"plex-1": ready_publisher()}, stage="process")
        assert out.outcome_key == FileOutcome.PUBLISHED.value
        marker = store.get_markers(store.get_file(s2e1).id)[MarkerType.INTRO]
        assert marker.decided_by == ("introdb", "season_audio_previous")

    def test_no_cached_previous_season_gives_no_hint_and_fingerprints_nothing_else(self, store, show):
        show(1, 4)
        (s2e1,) = show(2, 1)
        with _Audio() as audio:
            _run(_season_ctx(store, s2e1), s2e1, {"plex-1": ready_publisher()}, stage="process")
        assert audio.computed == [s2e1] and _evidence(store, s2e1, Source.SEASON_AUDIO_PREVIOUS) == []

    def test_a_sibling_without_a_fingerprint_is_not_a_reason_to_use_the_previous_season(self, store, show):
        self._cache_previous(store, show(1, 4))
        s2e1, s2e2 = show(2, 2)
        with _Audio(fail={s2e2}):
            _run(_season_ctx(store, s2e1), s2e1, {"plex-1": ready_publisher()}, stage="process")
        assert _evidence(store, s2e1, Source.SEASON_AUDIO_PREVIOUS) == []
        assert _evidence(store, s2e1, Source.SEASON_AUDIO) == []

    def test_the_second_episode_arriving_re_decides_the_first(self, store, show):
        self._cache_previous(store, show(1, 4))
        (s2e1,) = show(2, 1)
        ctx = _season_ctx(store, s2e1)
        with _Audio():
            _run(ctx, s2e1, {"plex-1": ready_publisher()}, stage="process")
            s2e2 = show(2, 2)[1]
            assert season.season_audio_due(store.get_file(s2e1), ctx) is True
            _run(ctx, s2e2, {"plex-1": ready_publisher()}, stage="process")
            assert ctx.take_followups() == [s2e1]
            out, _ = _run(ctx, s2e1, {"plex-1": ready_publisher()})  # the Season follow-up job's check stage
        (cand,) = _evidence(store, s2e1, Source.SEASON_AUDIO)
        assert cand.origin == "1/1" and _evidence(store, s2e1, Source.SEASON_AUDIO_PREVIOUS) == []
        # The hint alone kept it in review; this season's audio decides it alone.
        assert out.outcome_key == FileOutcome.PUBLISHED.value
        marker = store.get_markers(store.get_file(s2e1).id)[MarkerType.INTRO]
        assert marker == Marker(MarkerType.INTRO, cand.start_ms, cand.end_ms, ("season_audio",))
        assert season.season_audio_due(store.get_file(s2e1), ctx) is False


class TestFailures:
    def test_the_episodes_own_fingerprint_failing_stores_nothing(self, store, show):
        e1, _, _ = show(1, 3)
        ctx = _season_ctx(store, e1)
        with _Audio(fail={e1}):
            out, _ = _run(ctx, e1, {"plex-1": ready_publisher()}, stage="process")
        rec = store.get_file(e1)
        assert out.outcome_key == FileOutcome.NO_MARKERS.value
        assert store.evidence_version(rec.id, Source.SEASON_AUDIO) is None
        assert store.get_detector_run(rec.id, Source.SEASON_AUDIO) is None

    def test_a_sibling_that_fails_is_left_out_and_makes_the_answer_due_again(self, store, show):
        e1, e2, e3 = show(1, 3)
        ctx = _season_ctx(store, e1)
        with _Audio(fail={e3}):
            _run(ctx, e1, {"plex-1": ready_publisher()}, stage="process")
        (cand,) = _evidence(store, e1, Source.SEASON_AUDIO)
        assert cand.origin == "1/1"
        assert season.season_audio_due(store.get_file(e1), ctx) is False
        # e3 has no fingerprint, but ffmpeg failed on it just now: other episodes' steps leave it out for a day.
        assert season.season_audio_needs_worker(store.get_file(e1), ctx) is False
        with _Audio():
            _run(ctx, e3, {"plex-1": ready_publisher()}, stage="process")  # e3's own run fingerprints it
        assert season.season_audio_due(store.get_file(e1), ctx) is True

    def test_a_sibling_changed_on_disk_is_not_read_or_rewritten(self, store, show):
        e1, e2, _ = show(1, 3)
        old = store.upsert_file(FileIdentity(e2, 1, 1), duration_ms=DUR, season_key=os.path.dirname(e2), is_movie=False)
        with _Audio() as audio:
            _run(_season_ctx(store, e1), e1, {"plex-1": ready_publisher()}, stage="process")
        assert e2 not in audio.computed
        assert store.get_file(e2) == old

    def test_siblings_ffprobe_cant_read_are_left_out(self, store, show):
        e1, e2, e3 = show(1, 3)
        answers = {e2: MediaProbe(None, ()), e3: ProbeError("ffprobe exited 1")}

        def probe(path, *, ffprobe, timeout_s=60.0):
            if isinstance(answers.get(path), Exception):
                raise answers[path]
            return answers.get(path, MediaProbe(DUR, ()))

        with _Audio() as audio, patch.object(season, "probe_media", side_effect=probe):
            _run(_season_ctx(store, e1), e1, {"plex-1": ready_publisher()}, stage="process")
        assert audio.computed == [e1] and store.get_file(e2) is None and store.get_file(e3) is None
        assert _evidence(store, e1, Source.SEASON_AUDIO) == [] == _evidence(store, e1, Source.SEASON_AUDIO_PREVIOUS)

    def test_a_sibling_that_cant_be_fingerprinted_is_asked_again_once_per_change_of_its_season(self, store, show):
        e1, e2, e3, e4 = show(1, 4)
        registry = _registry(e1, ServerType.PLEX)
        # E4 has an answer from an earlier run, and its audio can't be fingerprinted now.
        victim = store.upsert_file(FileIdentity(e4, *_identity(e4)), duration_ms=DUR, season_key=None, is_movie=False)
        store.set_detector_run(victim.id, Source.SEASON_AUDIO, "an earlier season")
        review = TypeDecision(MarkerType.INTRO, DecisionStatus.NEEDS_REVIEW, None, None, "sources don't agree yet")
        store.save_decisions(victim.id, {MarkerType.INTRO: review}, settings_fingerprint="x")

        def run(ctx, path):
            if _run(ctx, path, {"plex-1": ready_publisher()})[0] is None:
                _run(ctx, path, {"plex-1": ready_publisher()}, stage="process")

        def job(items):
            ctx = _ctx(store, registry, settings_raw=SEASON_RAW, detectors=(_spec(),))
            for path in items:
                run(ctx, path)
            requested = ctx.take_followups()
            season_job = _ctx(store, registry, settings_raw=SEASON_RAW, detectors=(_spec(),))
            for path in requested:
                if path not in items:
                    run(season_job, path)  # E4 fails again
            return requested

        with _Audio(fail={e4}, points=_episode_noise):
            assert e4 in job([e1, e2, e3])
            assert store.get_detector_failure(victim.id, Source.SEASON_AUDIO) is not None
            assert job([e1]) == [] and job([e2]) == []  # its season is the one it failed on
            (e5,) = show(1, 5)[4:]
            assert e4 in job([e5])  # a new episode: asked once more
            assert job([e1]) == []

    def test_a_sibling_that_cant_be_fingerprinted_is_not_fingerprinted_again_by_every_episode(self, store, show):
        paths = show(1, 10)
        broken = paths[4]
        registry = _registry(paths[0], ServerType.PLEX)
        clock = {"now": datetime(2026, 9, 13, tzinfo=UTC)}

        def run(ctx, path):
            if _run(ctx, path, {"plex-1": ready_publisher()})[0] is None:
                _run(ctx, path, {"plex-1": ready_publisher()}, stage="process")

        def job(items):
            """A job over ``items`` and its Season job; returns how many times ffmpeg ran on the broken file."""
            audio.computed.clear()
            ctx = _ctx(store, registry, settings_raw=SEASON_RAW, detectors=(_spec(),), now=lambda: clock["now"])
            for path in items:
                run(ctx, path)
            season_job = _ctx(store, registry, settings_raw=SEASON_RAW, detectors=(_spec(),), now=lambda: clock["now"])
            for path in ctx.take_followups():
                if path not in items:
                    run(season_job, path)
            return audio.computed.count(broken)

        with _Audio(fail={broken}, points=_episode_noise) as audio:
            # The first episode's step, then the broken file's own run (which always tries); was once per episode.
            assert job(paths) == 2
            assert job([paths[0]]) == 0
            (e11,) = show(1, 11)[10:]
            assert job([e11]) == 0  # a new episode and the Season job of its siblings, within the day
            clock["now"] += timedelta(days=1)
            (e12,) = show(1, 12)[11:]
            assert job([e12]) == 1  # a day later, one step tries it again

    @pytest.mark.parametrize(
        ("target", "failed_ago", "same_identity", "force", "tried"),
        [
            ("sibling", timedelta(hours=23), True, False, False),
            ("sibling", timedelta(days=1), True, False, True),
            ("sibling", timedelta(hours=23), False, False, True),
            ("sibling", timedelta(hours=23), True, True, True),
            ("own", timedelta(hours=23), True, False, True),
        ],
        ids=["sibling-within-a-day", "sibling-a-day-later", "sibling-changed-since", "sibling-forced", "own-run"],
    )
    def test_a_fingerprint_failure_is_skipped_only_by_other_episodes_for_a_day_while_the_file_is_unchanged(
        self, store, show, target, failed_ago, same_identity, force, tried
    ):
        e1, e2, e3 = show(1, 3)
        now = datetime(2026, 9, 13, tzinfo=UTC)
        failed_as = FileIdentity(e2, *(_identity(e2) if same_identity else (1, 1)))
        store.record_member_fingerprint_failure(failed_as, now - failed_ago, forget_before=now - timedelta(days=30))
        ctx = _ctx(
            store,
            _registry(e1, ServerType.PLEX),
            settings_raw=SEASON_RAW,
            detectors=(_spec(),),
            force=force,
            now=lambda: now,
        )
        with _Audio() as audio:
            _run(ctx, e1 if target == "sibling" else e2, {"plex-1": ready_publisher()}, stage="process")
        assert (e2 in audio.computed) is tried
        assert e3 in audio.computed

    def test_a_files_own_failed_run_keeps_a_later_siblings_step_from_fingerprinting_it(self, store, show):
        e1, e2 = show(1, 2)
        with _Audio(fail={e1}) as audio:
            _run(_season_ctx(store, e1), e1, {"plex-1": ready_publisher()}, stage="process")
            assert store.member_fingerprint_failed_at(FileIdentity(e1, *_identity(e1))) is not None
            audio.computed.clear()
            _run(_season_ctx(store, e2), e2, {"plex-1": ready_publisher()}, stage="process")
        assert audio.computed == [e2]

    def test_siblings_waiting_on_a_file_that_fails_meanwhile_dont_run_ffmpeg_on_it_again(self, store, show):
        paths = show(1, 5)
        broken, siblings = paths[4], paths[:4]
        for path in paths:
            store.upsert_file(FileIdentity(path, *_identity(path)), duration_ms=DUR, season_key=None, is_movie=False)
        waiting, guard, all_waiting = [], threading.Lock(), threading.Event()
        real_ensure = fingerprint.ensure_fingerprint

        def ensure(store_, rec_, **kwargs):
            if rec_.canonical_path == broken:
                with guard:
                    waiting.append(rec_.canonical_path)
                    if len(waiting) == len(siblings):
                        all_waiting.set()
            return real_ensure(store_, rec_, **kwargs)

        def compute(path, duration_ms, *, ffmpeg, cancel_check=None):
            audio.computed.append(path)
            if path == broken:
                all_waiting.wait(10)  # every sibling's step is at the broken file before ffmpeg fails on it
                raise fingerprint.FingerprintError("ffmpeg exited 1")
            return _episode_noise(path)

        errors = []
        real_record = season._record_fingerprint_failure

        def slow_record(ctx, rec):  # a slow store write between ffmpeg failing and the failure being recorded
            time.sleep(0.05)
            real_record(ctx, rec)

        def detect(path):
            try:
                season.detect_season_audio(store.get_file(path), ctx=_season_ctx(store, path))
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        with (
            _Audio() as audio,
            patch.object(fingerprint, "compute_fingerprint", side_effect=compute),
            patch.object(season, "ensure_fingerprint", side_effect=ensure),
            patch.object(season, "_record_fingerprint_failure", side_effect=slow_record),
        ):
            threads = [threading.Thread(target=detect, args=(path,)) for path in siblings]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(20)
        assert errors == [] and all_waiting.is_set()
        assert audio.computed.count(broken) == 1

    def test_a_sibling_whose_fingerprint_is_cancelled_is_not_remembered_as_a_failure(self, store, show):
        e1, e2, e3 = show(1, 3)
        rec = store.upsert_file(FileIdentity(e1, *_identity(e1)), duration_ms=DUR, season_key=None, is_movie=False)
        cancelled = threading.Event()

        def compute(path, duration_ms, *, ffmpeg, cancel_check=None):
            if path == e2:
                cancelled.set()  # the user cancels while ffmpeg reads E2
                raise fingerprint.FingerprintError("Fingerprinting cancelled")
            return fake_points(path)

        with (
            _Audio(),
            patch.object(fingerprint, "compute_fingerprint", side_effect=compute),
            pytest.raises(pipeline.DetectorUnavailableError),
        ):
            season.detect_season_audio(rec, ctx=_season_ctx(store, e1), cancel_check=cancelled.is_set)
        assert store.member_fingerprint_failed_at(FileIdentity(e2, *_identity(e2))) is None

    def test_stalled_fingerprint_ffmpegs_stop_the_season_at_that_sibling_and_blame_no_file(self, store, show):
        e1, e2, e3 = show(1, 3)
        rec = store.upsert_file(FileIdentity(e1, *_identity(e1)), duration_ms=DUR, season_key=None, is_movie=False)

        def compute(path, duration_ms, *, ffmpeg, cancel_check=None):
            audio.computed.append(path)
            if path == e2:
                raise fingerprint.FingerprintStalledError("2 earlier fingerprint ffmpegs are still stuck")
            return fake_points(path)

        with (
            _Audio() as audio,
            patch.object(fingerprint, "compute_fingerprint", side_effect=compute),
            pytest.raises(pipeline.DetectorUnavailableError, match="still stuck"),
        ):
            season.detect_season_audio(rec, ctx=_season_ctx(store, e1))
        assert audio.computed == [e1, e2]  # E3 would meet the same stalled mount: not tried
        assert store.member_fingerprint_failed_at(FileIdentity(e2, *_identity(e2))) is None
        assert store.get_detector_failure(rec.id, Source.SEASON_AUDIO) is None

    def test_stalled_ffprobes_stop_the_season_at_that_sibling_and_blame_no_file(self, store, show):
        e1, e2, e3 = show(1, 3)
        rec = store.upsert_file(FileIdentity(e1, *_identity(e1)), duration_ms=DUR, season_key=None, is_movie=False)
        probed: list[str] = []

        def probe(path, **kwargs):
            probed.append(path)
            if path == e2:
                raise ProbeStalledError(f"Not reading {path}: 2 earlier ffprobes are still stuck")
            return MediaProbe(DUR, ())

        with (
            _Audio(),
            patch.object(season, "probe_media", side_effect=probe),
            pytest.raises(pipeline.DetectorUnavailableError, match="still stuck"),
        ):
            season.detect_season_audio(rec, ctx=_season_ctx(store, e1))
        assert probed == [e2]  # E3 would meet the same stall: not tried
        assert store.member_probe_failed_at(FileIdentity(e2, *_identity(e2))) is None
        assert store.get_detector_failure(rec.id, Source.SEASON_AUDIO) is None

    def test_a_member_ffprobe_cant_start_for_counts_as_unread_for_the_chapter_check(self, store, show):
        e1, e2, e3 = show(1, 3)

        def probe(path, **kwargs):
            if path == e2:
                raise ProbeStalledError(f"Not reading {path}: 2 earlier ffprobes are still stuck")
            return MediaProbe(DUR, ())

        with patch.object(season, "probe_media", side_effect=probe):
            limit, siblings = season.season_intro_chapter_limits(_season_ctx(store, e1), e1)
        assert (limit, siblings) == (None, {e2: None, e3: None})
        assert store.member_probe_failed_at(FileIdentity(e2, *_identity(e2))) is None  # read again next time
        assert store.get_file(e3) is not None and store.get_file(e2) is None

    def test_stalled_fingerprint_ffmpegs_are_not_this_episodes_failure(self, store, show):
        e1, _ = show(1, 2)
        rec = store.upsert_file(FileIdentity(e1, *_identity(e1)), duration_ms=DUR, season_key=None, is_movie=False)
        stalled = fingerprint.FingerprintStalledError("2 earlier fingerprint ffmpegs are still stuck")
        with (
            _Audio(),
            patch.object(fingerprint, "compute_fingerprint", side_effect=stalled),
            pytest.raises(pipeline.DetectorUnavailableError, match="still stuck"),
        ):
            season.detect_season_audio(rec, ctx=_season_ctx(store, e1))
        assert store.get_detector_failure(rec.id, Source.SEASON_AUDIO) is None
        assert store.member_fingerprint_failed_at(FileIdentity(e1, *_identity(e1))) is None

    def test_a_file_whose_own_fingerprint_failed_lately_still_needs_a_worker(self, store, show):
        e1, e2 = show(1, 2)
        with _Audio(fail={e1}):
            _run(_season_ctx(store, e2), e2, {"plex-1": ready_publisher()}, stage="process")  # e2's step fails on e1
        rec = store.get_file(e1)
        assert store.member_fingerprint_failed_at(FileIdentity(e1, rec.size, rec.mtime_ns)) is not None
        ctx = _season_ctx(store, e1)
        assert season.season_audio_needs_worker(rec, ctx) is True
        assert season.season_audio_needs_worker(store.get_file(e2), ctx) is False

    def test_a_cancelled_fingerprint_is_not_remembered_as_a_failure(self, store, show):
        e1, _ = show(1, 2)
        rec = store.upsert_file(FileIdentity(e1, *_identity(e1)), duration_ms=DUR, season_key=None, is_movie=False)
        cancelled = fingerprint.FingerprintError("Fingerprinting cancelled")
        with (
            _Audio(),
            patch.object(season, "ensure_fingerprint", side_effect=cancelled),
            pytest.raises(pipeline.DetectorUnavailableError),
        ):
            season.detect_season_audio(rec, ctx=_season_ctx(store, e1), cancel_check=lambda: True)
        assert store.get_detector_failure(rec.id, Source.SEASON_AUDIO) is None

    def test_chromaprint_gone_at_run_time_stores_nothing(self, store, show):
        e1, _ = show(1, 2)
        ctx = _season_ctx(store, e1)
        with _Audio() as audio, patch.object(season, "chromaprint_ffmpeg", return_value=None):
            _run(ctx, e1, {"plex-1": ready_publisher()}, stage="process")
        rec = store.get_file(e1)
        assert audio.computed == [] and store.evidence_version(rec.id, Source.SEASON_AUDIO) is None

    def test_cancelling_stops_before_the_next_sibling(self, store, show):
        e1, _, _ = show(1, 3)
        ctx = _season_ctx(store, e1)
        rec = store.upsert_file(FileIdentity(e1, *_identity(e1)), duration_ms=DUR, season_key=None, is_movie=False)
        with _Audio() as audio, pytest.raises(pipeline.DetectorUnavailableError):
            season.detect_season_audio(rec, ctx=ctx, cancel_check=lambda: True)
        assert audio.computed == [e1]
        assert store.get_detector_run(rec.id, Source.SEASON_AUDIO) is None

    def test_a_failure_while_the_answer_is_stored_leaves_it_due(self, store, show):
        e1, _ = show(1, 2)
        real_write = MarkerStore._write_evidence

        def failing(conn, file_id, source, *args):
            if source is Source.SEASON_AUDIO:
                raise sqlite3.OperationalError("disk I/O error")  # or the process ends here
            return real_write(conn, file_id, source, *args)

        with _Audio():
            _run(_season_ctx(store, e1), e1, {"plex-1": ready_publisher()}, stage="process")
            (before,) = _evidence(store, e1, Source.SEASON_AUDIO)
            show(1, 3)
            with (
                patch.object(MarkerStore, "_write_evidence", staticmethod(failing)),
                pytest.raises(sqlite3.OperationalError),
            ):
                _run(_season_ctx(store, e1), e1, {"plex-1": ready_publisher()}, stage="process")
            ctx = _season_ctx(store, e1)
            assert season.season_audio_due(store.get_file(e1), ctx) is True
            _run(ctx, e1, {"plex-1": ready_publisher()}, stage="process")
            (after,) = _evidence(store, e1, Source.SEASON_AUDIO)
        assert (before.origin, after.origin) == ("1/1", "2/2")

    @staticmethod
    def _decided_with_season_audio(store, show):
        """E1 decided by IntroDB and a 1/1 season audio match; E3-E6 then arrive with another opening."""
        e1, e2 = show(1, 2)
        intro = noise(11, 240)

        def points(path):
            body = _episode_noise(path)
            if os.path.basename(path).endswith(("E01.mkv", "E02.mkv")):
                body[200:440] = intro
            return body

        clients = _introdb_answer(round(200 * POINT_S * 1000), round(439 * POINT_S * 1000))
        ctx = _ctx(
            store, _registry(e1, ServerType.PLEX), settings_raw=SEASON_RAW, detectors=(_spec(),), clients=clients
        )
        with _Audio(points=points):
            for path in (e1, e2):
                _run(ctx, path, {"plex-1": ready_publisher()}, stage="process")
        show(1, 6)  # against E3-E6 its answer would fall below quorum, but it can't be matched again
        return e1, clients

    @staticmethod
    def _decided_with_the_previous_season_hint(store, show):
        """A new season's first episode decided by IntroDB and the previous season's hint; its season then arrives."""
        TestWeeklyReleases()._cache_previous(store, show(1, 4))
        (s2e1,) = show(2, 1)
        start, end = planted_ms(s2e1)
        clients = _introdb_answer(start, end - 1_000)
        ctx = _ctx(
            store, _registry(s2e1, ServerType.PLEX), settings_raw=SEASON_RAW, detectors=(_spec(),), clients=clients
        )
        with _Audio():
            _run(ctx, s2e1, {"plex-1": ready_publisher()}, stage="process")
        show(2, 3)
        return s2e1, clients

    @pytest.mark.parametrize(
        ("decided", "answer"),
        [
            ("_decided_with_season_audio", Source.SEASON_AUDIO),
            ("_decided_with_the_previous_season_hint", Source.SEASON_AUDIO_PREVIOUS),
        ],
        ids=["season-audio", "previous-season-hint"],
    )
    def test_without_chromaprint_a_stored_season_audio_answer_no_longer_decides(self, store, show, decided, answer):
        target, clients = getattr(self, decided)(store, show)
        registry = _registry(target, ServerType.PLEX)
        assert store.get_markers(store.get_file(target).id)[MarkerType.INTRO].decided_by == ("introdb", answer.value)
        with patch.object(season, "chromaprint_ffmpeg", return_value=None):
            detectors = pipeline.default_local_detectors(
                _ctx(store, registry, settings_raw=SEASON_RAW).settings, MagicMock()
            )
        assert detectors == ()
        ctx = _ctx(store, registry, settings_raw=SEASON_RAW, detectors=detectors, clients=clients)
        ctx.chromaprint = fingerprint.ChromaprintState.ABSENT  # what build_context records for this container
        with _Audio():
            out, _ = _run(ctx, target, {"plex-1": ready_publisher()})
        rec = store.get_file(target)
        # The write took our intro off Plex, so the file counts as written; the intro in review is in its reason.
        assert out.outcome_key == FileOutcome.PUBLISHED.value
        assert "intro needs review" in out.message
        assert store.get_decisions(rec.id)[MarkerType.INTRO].status is DecisionStatus.NEEDS_REVIEW
        assert MarkerType.INTRO not in store.get_markers(rec.id)
        assert _evidence(store, target, answer)  # kept for when chromaprint is back

    def test_a_chromaprint_check_that_didnt_answer_keeps_stored_season_audio_answers_deciding(
        self, store, show, tmp_path
    ):
        target, clients = self._decided_with_season_audio(store, show)
        registry = _registry(target, ServerType.PLEX)
        ffmpeg = tmp_path / "ffmpeg"
        ffmpeg.write_text("")
        ffmpeg.chmod(0o755)
        settings = _ctx(store, registry, settings_raw=SEASON_RAW).settings
        fingerprint.forget_chromaprint_answers()
        try:
            with (
                patch.object(fingerprint, "JELLYFIN_FFMPEG", str(tmp_path / "missing")),
                patch.object(fingerprint.shutil, "which", return_value=None),
                patch.object(fingerprint.subprocess, "run", side_effect=subprocess.TimeoutExpired("ffmpeg", 20)),
            ):
                state = fingerprint.chromaprint_state(str(ffmpeg))
                detectors = pipeline.default_local_detectors(settings, SimpleNamespace(ffmpeg_path=str(ffmpeg)), state)
        finally:
            fingerprint.forget_chromaprint_answers()
        assert (state, detectors) == (fingerprint.ChromaprintState.UNKNOWN, ())
        ctx = _ctx(store, registry, settings_raw=SEASON_RAW, detectors=detectors, clients=clients)
        ctx.chromaprint = state
        with _Audio():
            _run(ctx, target, {"plex-1": ready_publisher()})
        rec = store.get_file(target)
        assert store.get_decisions(rec.id)[MarkerType.INTRO].status is DecisionStatus.DECIDED
        assert store.get_markers(rec.id)[MarkerType.INTRO].decided_by == ("introdb", "season_audio")

    @pytest.mark.parametrize(
        ("detectors", "chromaprint"),
        [("registered", "available"), ("none", "absent"), ("none", "unknown")],
        ids=["registered", "absent", "unknown"],
    )
    def test_a_stale_season_audio_answer_can_still_hold_a_chapter_in_review(self, store, show, detectors, chromaprint):
        (path,) = show(1, 1)
        rec = store.upsert_file(FileIdentity(path, *_identity(path)), duration_ms=DUR, season_key=None, is_movie=False)
        intro_chapter = chapter_candidates(_chapter_probe(60_000, at_ms=0))  # an "Intro" chapter at 0-60 s
        store.replace_evidence(rec.id, Source.CHAPTERS, intro_chapter, version=CHAPTER_RULES_VERSION)
        store.replace_evidence(rec.id, Source.INTRODB, [Candidate(MarkerType.INTRO, 300_000, 390_000, Source.INTRODB)])
        audio = Candidate(MarkerType.INTRO, 300_000, 390_000, Source.SEASON_AUDIO, 1.0, "2/2")
        store.replace_evidence(rec.id, Source.SEASON_AUDIO, [audio])
        ctx = _ctx(
            store,
            _registry(path, ServerType.PLEX),
            settings_raw=SEASON_RAW,
            detectors=(_spec(),) if detectors == "registered" else (),
        )
        ctx.chromaprint = fingerprint.ChromaprintState(chromaprint)

        decision = pipeline._decide(ctx, rec, frozenset({MarkerType.INTRO}))[MarkerType.INTRO]

        # IntroDB and season audio agree against the chapter; without the detector the chapter alone would decide.
        assert decision.status is DecisionStatus.NEEDS_REVIEW and decision.marker is None

    def test_no_chromaprint_registers_no_detector(self, loguru_caplog):
        from media_preview_generator.markers.settings import load_global, validate_global

        settings = load_global(validate_global({}, None)[0])
        with patch.object(season, "chromaprint_ffmpeg", return_value=None):
            assert season.season_audio_spec("/usr/local/bin/ffmpeg") is None
            assert (
                pipeline.default_local_detectors(settings, type("Cfg", (), {"ffmpeg_path": "/usr/local/bin/ffmpeg"})())
                == ()
            )
        assert "chromaprint" in loguru_caplog.text

    def test_season_audio_switched_off_registers_no_detector(self):
        from media_preview_generator.markers.settings import load_global, validate_global

        settings = load_global(validate_global({"sources": [{"id": "season_audio", "enabled": False}]}, None)[0])
        with patch.object(season, "chromaprint_ffmpeg", return_value="/ffmpeg") as found:
            assert pipeline.default_local_detectors(settings, type("Cfg", (), {"ffmpeg_path": "/ffmpeg"})()) == ()
        found.assert_not_called()

    def test_season_audio_on_with_chromaprint_registers_the_detector(self):
        from media_preview_generator.markers.settings import load_global, validate_global

        settings = load_global(validate_global({}, None)[0])
        with patch.object(season, "chromaprint_ffmpeg", return_value="/usr/lib/jellyfin-ffmpeg/ffmpeg") as found:
            (spec,) = pipeline.default_local_detectors(settings, type("Cfg", (), {"ffmpeg_path": "/x/ffmpeg"})())
        found.assert_called_once_with("/x/ffmpeg")
        assert spec.source is Source.SEASON_AUDIO


def _silent(size: int) -> np.ndarray:
    return np.full(size, SIL, dtype="<u4")


class TestSilenceGuard:
    """chromaprint turns silence into one constant, so two episodes' shared quiet matches like an intro would."""

    def _season_with_shared_silence(self):
        points = {}
        for e, at in ((1, 200), (2, 450), (3, 700)):
            body = noise(e, 2_000)
            body[at : at + 200] = SIL  # 25 s of silence, nothing else in common
            points[f"/tv/Show/Season 01/Show - S01E{e:02d}.mkv"] = body
        return points

    def test_a_silence_shared_by_the_season_is_not_an_intro(self):
        points = self._season_with_shared_silence()
        files = sorted(points)
        raw = matcher.season_intros(points)
        assert all(raw[f] is not None for f in files)  # what the v3 matcher alone answers

        def runs_between(a, b):
            return season.season_pair_runs(points[a], points[b])

        found = [season.season_intro(f, files, points, runs_between, end_picture_passes=_alike) for f in files]
        assert found == [None, None, None]

    def test_an_intro_with_some_silence_in_it_is_kept(self):
        intro = noise(99, 240)
        intro[::3] = SIL  # a third of the intro's points are quiet (the eval's useful intros hold at most 14 %)
        points = {}
        for e, at in ((1, 100), (2, 300), (3, 500)):
            body = noise(e, 2_000)
            body[at : at + 240] = intro
            points[f"/tv/Show/Season 01/Show - S01E{e:02d}.mkv"] = body
        files = sorted(points)

        def runs_between(a, b):
            return season.season_pair_runs(points[a], points[b])

        found = [season.season_intro(f, files, points, runs_between, end_picture_passes=_alike) for f in files]
        assert found == [matcher.season_intros(points)[f] for f in files] and all(found)

    def test_points_the_matcher_takes_for_silence_count_as_silence(self):
        points = np.array(
            [SIL, SIL + 2, SIL - 2, SIL ^ 0x3F, SIL ^ 0x7F, 0, SIL + 3, SIL ^ 0x1FF00], dtype=np.uint64
        ).astype("<u4")
        # within ±2, or at most 6 bits different (SIL + 3 flips 3 bits); 7 or 9 bits apart and far in value, or 0, no
        assert [season.silence_share(points[i : i + 1]) for i in range(len(points))] == [1, 1, 1, 1, 0, 0, 1, 0]
        assert season.silence_share(points) == 5 / 8

    @pytest.mark.parametrize(("silent_points", "dropped"), [(50, False), (51, True)])
    def test_mostly_means_more_than_half_of_the_intros_points(self, silent_points, dropped):
        points = noise(7, 1_000)
        points[100 : 100 + silent_points] = SIL
        segment = matcher.IntroSegment(100 * POINT_S, 199 * POINT_S, 2)  # points 100..199
        with patch.object(season, "guarded_pick", return_value=segment):
            got = season.season_intro("a", ["a", "b", "c"], {"a": points}, lambda x, y: [], end_picture_passes=_alike)
        assert got == (None if dropped else segment)


def _constant_then_intro(seed: int, value: int, gap: int, size: int = 3_000, constant: int = 2_000) -> np.ndarray:
    """``constant`` points of one value, ``gap`` points of this episode's own noise, then the shared 40 s intro."""
    body = noise(seed, size)
    body[:constant] = value
    body[constant + gap : constant + gap + 323] = noise(77, 323)
    return body


def _sparse(size: int, value: int, every: int, count: int, start: int = 0) -> np.ndarray:
    """One value with ``count`` noise points ``every`` points apart."""
    body = np.full(size, value, dtype="<u4")
    at = np.arange(count) * every + start
    body[at] = noise(size + start, count) & 0x0FFFFFFF  # never the constant itself
    return body


def _store_fingerprint(store, path, points):
    rec = store.upsert_file(FileIdentity(path, *_identity(path)), duration_ms=DUR, season_key=os.path.dirname(path),
                            is_movie=False)  # fmt: skip
    store.set_fingerprint(rec.id, size=rec.size, mtime_ns=rec.mtime_ns, window="intro", start_s=0.0,
                          length_s=fingerprint.window_s(DUR), algorithm=1, points=points.tobytes())  # fmt: skip
    return rec


class TestDegenerateInput:
    """Matching runs inline on checking threads: silent or constant openings must not cost about 1 s per pair there,
    and a pair is left unmatched only when the matcher provably finds no intro-length run in it."""

    def test_two_silent_openings_are_not_matched(self):
        with patch.object(season, "pair_runs") as runs:
            assert season.season_pair_runs(_silent(7_266), _silent(7_266)) == []
        runs.assert_not_called()

    def test_one_silent_opening_is_still_matched(self):
        with patch.object(season, "pair_runs", return_value=[]) as runs:
            season.season_pair_runs(_silent(7_266), noise(3, 7_266))
        runs.assert_called_once()

    @pytest.mark.parametrize("value", [SIL, 0x12345678], ids=["silence", "tone"])
    def test_a_long_shared_constant_before_an_intro_is_matched_exactly(self, value):
        # Why no match-count or silence-share limit may skip a pair: 250 s of the same constant in both openings, a
        # few seconds apart, then a shared 40 s intro. Millions of value matches, and the matcher finds the intro.
        a, b = _constant_then_intro(1, value, gap=40), _constant_then_intro(2, value, gap=60)
        assert season.pair_value_matches(a, b) > season.MAX_INLINE_PAIR_MATCHES
        assert (season.holds_no_intro(a, b), season.slow_to_match(a, b)) == (False, True)
        runs = season.season_pair_runs(a, b)
        assert runs == matcher.pair_runs(a, b)
        assert any(r.a_end_s - r.a_start_s <= matcher.MAX_INTRO_S for r in runs)

    @pytest.mark.parametrize(("b_count", "proven"), [(68, True), (69, False)])
    def test_the_proof_allows_at_most_the_shorter_length_minus_1063_other_points(self, b_count, proven):
        a = _sparse(1_200, SIL, every=14, count=69)
        b = _sparse(1_200, SIL, every=14, count=b_count, start=5)
        assert 69 + b_count - (1_200 - 1_063) == (0 if proven else 1)
        assert season.holds_no_intro(a, b) is proven

    @pytest.mark.parametrize(("b_stretch", "proven"), [(13, True), (14, False)])
    def test_the_proof_needs_every_28_points_to_hold_an_exact_match(self, b_stretch, proven):
        a, b = _silent(1_500), _silent(1_500)
        a[400:414] = noise(1, 14)
        b[900 : 900 + b_stretch] = noise(2, b_stretch)
        assert season.holds_no_intro(a, b) is proven

    def test_whatever_the_proof_accepts_the_matcher_finds_no_intro_length_run(self):
        rng = np.random.default_rng(20260915)
        accepted = 0
        for case in range(40):
            value = SIL if case % 2 else int(rng.integers(0, 2**32))
            pair = []
            for side in range(2):
                size = int(rng.integers(1_064, 1_400))
                body = np.full(size, value, dtype="<u4")
                for _ in range(int(rng.integers(0, 10))):
                    at, length = int(rng.integers(0, size)), int(rng.integers(1, 14))
                    body[at : at + length] = noise(case * 100 + side * 10 + at, len(body[at : at + length]))
                pair.append(body)
            if season.holds_no_intro(*pair):
                accepted += 1
                runs = matcher.pair_runs(*pair)
                assert all(r.a_end_s - r.a_start_s > matcher.MAX_INTRO_S for r in runs), case
        assert accepted >= 10

    def test_value_matches_count_what_the_matcher_would_expand(self):
        a = np.array([5, 6, 7, 100, 2**32 - 1], dtype="<u4")
        b = np.array([3, 5, 9, 102, 2**32 - 2, 0], dtype="<u4")
        expected = sum(1 for x in a.tolist() for y in b.tolist() if abs(x - y) <= 2)
        assert season.pair_value_matches(a, b) == expected == 7

    def test_a_fully_silent_26_episode_season_matches_quickly(self):
        points = {f"/tv/Show/Season 01/Show - S01E{e:02d}.mkv": _silent(7_266) for e in range(1, 27)}
        files = sorted(points)
        cache = {}

        def runs_between(a, b):
            if (a, b) not in cache:
                cache[(a, b)] = season.season_pair_runs(points[a], points[b])
            return cache[(a, b)]

        started = time.perf_counter()
        found = [season.season_intro(f, files, points, runs_between, end_picture_passes=_alike) for f in files]
        elapsed = time.perf_counter() - started
        assert found == [None] * 26 and len(cache) == 325
        # The v3 matcher alone takes about 1.25 s per pair here: about 7 minutes for the season.
        assert elapsed < 5.0

    def test_a_slow_pair_not_matched_yet_needs_a_worker(self, store, show):
        e1, e2 = show(1, 2)
        ctx = _season_ctx(store, e1)
        a = _store_fingerprint(store, e1, _constant_then_intro(1, 0x12345678, gap=40))
        b = _store_fingerprint(store, e2, _constant_then_intro(2, 0x12345678, gap=60))
        assert season.season_audio_needs_worker(a, ctx) is True
        assert season.season_audio_needs_worker(b, ctx) is True
        store.set_season_pair(
            a.id,
            b.id,
            season.SEASON_AUDIO_VERSION,
            [],
            identity_a=(a.size, a.mtime_ns),
            identity_b=(b.size, b.mtime_ns),
        )
        assert season.season_audio_needs_worker(a, ctx) is False
        assert season.season_audio_needs_worker(b, ctx) is False

    def test_a_silent_season_matches_on_the_checking_thread(self, store, show):
        e1, e2, e3 = show(1, 3)
        recs = [_store_fingerprint(store, p, _silent(7_266)) for p in (e1, e2, e3)]
        ctx = _season_ctx(store, e1)
        assert [season.season_audio_needs_worker(r, ctx) for r in recs] == [False, False, False]

    def test_a_slow_pair_with_the_previous_season_needs_a_worker(self, store, show):
        (p1,) = show(1, 1)
        (s2e1,) = show(2, 1)
        _store_fingerprint(store, p1, _constant_then_intro(1, SIL, gap=40))
        rec = _store_fingerprint(store, s2e1, _constant_then_intro(2, SIL, gap=60))
        assert season.season_audio_needs_worker(rec, _season_ctx(store, s2e1)) is True


def _chapter_probe(intro_ms: int | None, at_ms: int = 30_000) -> MediaProbe:
    if intro_ms is None:
        return MediaProbe(DUR, (Chapter(0, 600_000, "Chapter 1"), Chapter(600_000, None, "Chapter 2")))
    return MediaProbe(
        DUR,
        (
            Chapter(0, at_ms, "Chapter 1"),
            Chapter(at_ms, at_ms + intro_ms, "Intro"),
            Chapter(at_ms + intro_ms, None, "Chapter 2"),
        ),
    )


class _Chapters:
    """Each episode's ffprobe answer by episode number, for the pipeline and the season step."""

    def __init__(self, lengths: dict[int, int | None]):
        self.lengths = lengths
        self.probed: list[str] = []

    def probe(self, path, *, ffprobe, timeout_s=60.0):
        self.probed.append(path)
        return _chapter_probe(self.lengths[int(re.search(r"E(\d\d)", os.path.basename(path)).group(1))])

    def __enter__(self):
        self._patch = patch.object(season, "probe_media", side_effect=self.probe)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()


def _check(ctx, path, chapters, publisher=None):
    out, _ = _run(ctx, path, {"plex-1": publisher or ready_publisher()}, probe_effect=chapters.probe)
    return out


def _intro_decision(store, path):
    rec = store.get_file(path)
    row = store.get_decisions(rec.id)[MarkerType.INTRO]
    return row.status, row.reason, store.get_markers(rec.id).get(MarkerType.INTRO)


class TestSeasonIntroChapters:
    """Finding F1: an intro chapter far longer than the rest of its season's may not decide alone, whatever order the
    season's episodes are checked or arrive in."""

    # Reservation Dogs S01 (Disney+): two "Intro" chapters are story, the others 3-11 s.
    RESERVATION_DOGS = {1: 3_000, 2: 6_931, 3: 7_000, 4: 11_000, 5: 125_834, 6: 86_545, 7: None}

    def test_the_story_chapters_need_review_and_the_others_publish(self, store, show):
        paths = show(1, 7)
        ctx = _ctx(store, _registry(paths[0], ServerType.PLEX), settings_raw=SEASON_RAW)
        with _Chapters(self.RESERVATION_DOGS) as chapters:
            outs = {i + 1: _check(ctx, p, chapters) for i, p in enumerate(paths)}
        # Every episode here resolves to the same Plex item, so E05's publish takes E04's intro off it: that write
        # counts the file as written. E06 then has nothing of ours there and nothing to send.
        assert [outs[e].outcome_key for e in (5, 6)] == [FileOutcome.PUBLISHED.value, FileOutcome.NEEDS_REVIEW.value]
        for e in (5, 6):
            assert f"intro needs review ({LONG_INTRO_CHAPTER_REASON})" in outs[e].message
            assert _intro_decision(store, paths[e - 1])[:2] == (DecisionStatus.NEEDS_REVIEW, LONG_INTRO_CHAPTER_REASON)
        for e in (1, 2, 3, 4):
            assert outs[e].outcome_key == FileOutcome.PUBLISHED.value
            assert _intro_decision(store, paths[e - 1])[:2] == (DecisionStatus.DECIDED, "chapters")

    def test_the_first_episode_checked_already_sees_the_whole_folder(self, store, show):
        # Mr. Robot S04E01 (88 s against 14 s) comes first in a library backfill: its siblings aren't known yet.
        paths = show(4, 4)
        ctx = _ctx(store, _registry(paths[0], ServerType.PLEX), settings_raw=SEASON_RAW)
        with _Chapters({1: 88_000, 2: 14_000, 3: 13_000, 4: 15_000}) as chapters:
            out = _check(ctx, paths[0], chapters)
            assert sorted(chapters.probed) == sorted(paths)
            chapters.probed.clear()
            _check(ctx, paths[1], chapters)
        assert out.outcome_key == FileOutcome.NEEDS_REVIEW.value
        assert chapters.probed == []  # the season step stored its siblings' chapters: nothing is read twice

    def test_fewer_than_two_other_intro_chapters_is_no_check(self, store, show):
        paths = show(1, 3)
        ctx = _ctx(store, _registry(paths[0], ServerType.PLEX), settings_raw=SEASON_RAW)
        with _Chapters({1: 10_000, 2: 126_000, 3: None}) as chapters:
            for p in paths:
                _check(ctx, p, chapters)
        assert _intro_decision(store, paths[1]) == (
            DecisionStatus.DECIDED,
            "chapters",
            Marker(MarkerType.INTRO, 30_000, 156_000, ("chapters",)),
        )

    def test_a_flagged_episode_with_an_agreeing_online_source_publishes(self, store, show):
        paths = show(1, 6)
        clients = _introdb_answer(140_000, 153_000)  # the season's usual short intro, at the end of the long chapter
        ctx = _ctx(store, _registry(paths[0], ServerType.PLEX), settings_raw=SEASON_RAW, clients=clients)
        with _Chapters(self.RESERVATION_DOGS) as chapters:
            out = _check(ctx, paths[4], chapters)
        assert out.outcome_key == FileOutcome.PUBLISHED.value
        assert _intro_decision(store, paths[4]) == (
            DecisionStatus.DECIDED,
            "chapters",
            Marker(MarkerType.INTRO, 140_000, 155_834, ("chapters", "introdb")),
        )

    def test_intro_detection_off_reads_no_siblings(self, store, show):
        paths = show(1, 4)
        raw = {**SEASON_RAW, "detect": {"intro": False, "credits": True}}
        ctx = _ctx(store, _registry(paths[0], ServerType.PLEX), settings_raw=raw)
        with _Chapters({1: 88_000, 2: 14_000, 3: 13_000, 4: 15_000}) as chapters:
            _check(ctx, paths[0], chapters)
        assert chapters.probed == [paths[0]]

    def test_a_sibling_changed_on_disk_is_left_out(self, store, show):
        paths = show(1, 3)
        stale = store.upsert_file(FileIdentity(paths[2], 1, 1), duration_ms=DUR, season_key=None, is_movie=False)
        ctx = _ctx(store, _registry(paths[0], ServerType.PLEX), settings_raw=SEASON_RAW)
        with _Chapters({1: 88_000, 2: 14_000, 3: 13_000}) as chapters:
            _check(ctx, paths[0], chapters)
        assert paths[2] not in chapters.probed and store.get_file(paths[2]) == stale
        assert _intro_decision(store, paths[0])[:2] == (DecisionStatus.DECIDED, "chapters")  # one other isn't a season

    def test_a_new_episode_asks_again_for_the_siblings_whose_decision_it_changes(self, store, show):
        e1, e2 = show(1, 2)
        ctx = _ctx(store, _registry(e1, ServerType.PLEX), settings_raw=SEASON_RAW)
        lengths = {1: 10_000, 2: 126_000, 3: 12_000}
        with _Chapters(lengths) as chapters:
            _check(ctx, e1, chapters)
            _check(ctx, e2, chapters)
            assert ctx.take_followups() == []
            (e3,) = show(1, 3)[2:]
            _check(ctx, e3, chapters)
            assert ctx.take_followups() == [e2]  # E1 stays decided either way
            assert _intro_decision(store, e2)[0] is DecisionStatus.DECIDED
            _check(ctx, e2, chapters)  # the Season follow-up job
        assert _intro_decision(store, e2)[:2] == (DecisionStatus.NEEDS_REVIEW, LONG_INTRO_CHAPTER_REASON)
        assert ctx.take_followups() == []

    @staticmethod
    def _season_run(tmp_path, arrivals, lengths):
        """Each batch of ``arrivals`` is written to disk and checked as one job, in order. Its follow-ups run the way the
        Season job (Task 8) runs them: only files that weren't items of the job, once, queuing nothing further."""
        folder = tmp_path / "media" / "tv" / "Show (2020) {tvdb-1}" / "Season 01"
        folder.mkdir(parents=True)
        store = MarkerStore(str(tmp_path / "markers.db"))
        paths = {e: str(folder / f"Show (2020) - S01E{e:02d}.mkv") for e in lengths}
        registry = _registry(paths[1], ServerType.PLEX)
        try:
            with _Chapters(lengths) as chapters:
                for batch in arrivals:
                    for e in batch:
                        with open(paths[e], "wb") as f:
                            f.write(b"x" * (100 + e))
                    job = _ctx(store, registry, settings_raw=SEASON_RAW)
                    for e in batch:
                        _check(job, paths[e], chapters)
                    season_job = _ctx(store, registry, settings_raw=SEASON_RAW)
                    for path in job.take_followups():
                        if path not in {paths[e] for e in batch}:
                            _check(season_job, path, chapters)
            return {e: _intro_decision(store, paths[e]) for e in lengths}
        finally:
            store.close()

    @pytest.mark.parametrize(
        "lengths",
        [
            {1: 3_000, 2: 6_931, 3: 7_000, 4: 11_000, 5: 125_834, 6: 86_545},
            {1: 10_000, 2: 12_000, 3: 90_000, 4: 95_000, 5: 100_000},  # later long chapters make the first one usual
            {1: 88_000, 2: 14_000, 3: 13_000, 4: 15_000, 5: None},
        ],
        ids=["reservation-dogs", "long-chapters-become-the-habit", "mr-robot"],
    )
    def test_any_order_gives_the_same_decisions(self, tmp_path, lengths):
        episodes = sorted(lengths)
        baseline = self._season_run(tmp_path / "all", [episodes], lengths)
        rng = random.Random(20260915)
        orders = [episodes[::-1], *(rng.sample(episodes, len(episodes)) for _ in range(3))]
        for i, order in enumerate(orders):
            weekly = [[e] for e in order]  # one episode a week
            assert self._season_run(tmp_path / f"weekly{i}", weekly, lengths) == baseline, order
            one_job = [order]  # all on disk, checked in this order
            assert self._season_run(tmp_path / f"job{i}", one_job, lengths) == baseline, order


def _write(path, size):
    with open(path, "wb") as f:
        f.write(b"x" * size)


def _episode_noise(path):
    return noise(int(re.search(r"E(\d+)", os.path.basename(path)).group(1)), 3_000)


def _job(store, registry, items, chapters, *, detectors=(), season_job=True):
    """One Intro & Credits job over ``items`` (a check, then the worker when the check hands it over), then the Season
    job Task 8 queues: the requested files that weren't items, once, queuing nothing further. Returns the requests."""

    def run(ctx, path):
        if _check(ctx, path, chapters) is None:
            _run(ctx, path, {"plex-1": ready_publisher()}, stage="process", probe_effect=chapters.probe)

    job = _ctx(store, registry, settings_raw=SEASON_RAW, detectors=detectors)
    for path in items:
        run(job, path)
    requested = job.take_followups()
    if season_job:
        follow = _ctx(store, registry, settings_raw=SEASON_RAW, detectors=detectors)
        for path in requested:
            if path not in items:
                run(follow, path)
    return requested


class TestSeasonChapterState:
    """The F1 limit a decision used is stored, and every season step compares it with the one each sibling would get
    now, so changes reach siblings however they happen."""

    LENGTHS = {1: 10_000, 2: 12_000, 3: 126_000, 4: 200_000, 5: 210_000}

    @pytest.fixture
    def season_folder(self, tmp_path):
        folder = tmp_path / "media" / "tv" / "Show (2020) {tvdb-1}" / "Season 01"
        folder.mkdir(parents=True)
        return folder

    def _paths(self, folder, episodes):
        return {e: str(folder / f"Show (2020) - S01E{e:02d}.mkv") for e in episodes}

    @pytest.mark.parametrize("with_audio", [False, True], ids=["chapters-only", "season-audio-worker"])
    def test_a_replaced_episode_without_an_intro_chapter_asks_again_for_its_sibling(
        self, store, season_folder, with_audio
    ):
        paths = self._paths(season_folder, self.LENGTHS)
        for e, path in paths.items():
            _write(path, 100 + e)
        registry = _registry(paths[1], ServerType.PLEX)
        lengths = dict(self.LENGTHS)
        detectors = (_spec(),) if with_audio else ()
        with _Audio(points=_episode_noise), _Chapters(lengths) as chapters:
            _job(store, registry, [paths[e] for e in sorted(paths)], chapters)
            assert _intro_decision(store, paths[3])[0] is DecisionStatus.DECIDED  # 126 s against a median of 106 s
            lengths[4] = None  # E4 replaced by a release without an intro chapter: E3's others are 10/12/210 s now
            _write(paths[4], 999)
            ctx = _ctx(store, registry, settings_raw=SEASON_RAW, detectors=detectors)
            handed_over = _check(ctx, paths[4], chapters) is None
            requested = ctx.take_followups()
            assert handed_over is with_audio
            assert paths[3] in requested  # asked before the worker handoff, so nothing is lost
            if handed_over:
                _run(ctx, paths[4], {"plex-1": ready_publisher()}, stage="process", probe_effect=chapters.probe)
            _job(store, registry, [paths[3]], chapters, detectors=detectors)  # the Season job
        assert _intro_decision(store, paths[3])[:2] == (DecisionStatus.NEEDS_REVIEW, LONG_INTRO_CHAPTER_REASON)

    def test_a_member_another_episodes_step_probed_is_noticed_by_the_next_run(self, store, season_folder):
        paths = self._paths(season_folder, (1, 2, 3))
        registry = _registry(paths[1], ServerType.PLEX)
        with _Chapters({1: 10_000, 2: 126_000, 3: 12_000}) as chapters:
            for e in (1, 2):
                _write(paths[e], 100 + e)
                _job(store, registry, [paths[e]], chapters)
            assert _intro_decision(store, paths[2])[0] is DecisionStatus.DECIDED  # one other: no check yet
            _write(paths[3], 103)
            # E1 runs again first (a user or webhook re-run): its step probes E3 and E2's limit is 41 s now.
            assert _job(store, registry, [paths[1]], chapters) == [paths[2]]
        assert _intro_decision(store, paths[2])[:2] == (DecisionStatus.NEEDS_REVIEW, LONG_INTRO_CHAPTER_REASON)

    def test_follow_ups_lost_to_a_restart_are_asked_again_by_the_next_run(self, tmp_path, season_folder):
        paths = self._paths(season_folder, (1, 2, 3))
        registry = _registry(paths[1], ServerType.PLEX)
        db = str(tmp_path / "restart.db")
        store = MarkerStore(db)
        with _Chapters({1: 10_000, 2: 126_000, 3: 12_000}) as chapters:
            for e in (1, 2):
                _write(paths[e], 100 + e)
                _job(store, registry, [paths[e]], chapters)
            _write(paths[3], 103)
            assert _job(store, registry, [paths[3]], chapters, season_job=False) == [paths[2]]
            store.close()  # the app restarts before the Season job runs
            store = MarkerStore(db)
            assert _intro_decision(store, paths[2])[0] is DecisionStatus.DECIDED
            assert _job(store, registry, [paths[1]], chapters) == [paths[2]]  # any later run of the season
        assert _intro_decision(store, paths[2])[:2] == (DecisionStatus.NEEDS_REVIEW, LONG_INTRO_CHAPTER_REASON)
        store.close()

    def test_a_deleted_episode_asks_again_for_the_siblings_it_changes(self, store, season_folder):
        paths = self._paths(season_folder, (1, 2, 3))
        for e, path in paths.items():
            _write(path, 100 + e)
        registry = _registry(paths[1], ServerType.PLEX)
        with _Chapters({1: 10_000, 2: 12_000, 3: 126_000}) as chapters:
            _job(store, registry, list(paths.values()), chapters)
            assert _intro_decision(store, paths[3])[0] is DecisionStatus.NEEDS_REVIEW
            os.remove(paths[1])  # only E2 is left beside E3: one other is no season
            assert _job(store, registry, [paths[2]], chapters) == [paths[3]]
        assert _intro_decision(store, paths[3])[:2] == (DecisionStatus.DECIDED, "chapters")

    def test_the_limit_is_stored_with_the_decision_and_unchanged_siblings_are_not_decided_again(
        self, store, season_folder
    ):
        paths = self._paths(season_folder, (1, 2, 3))
        for e, path in paths.items():
            _write(path, 100 + e)
        registry = _registry(paths[1], ServerType.PLEX)
        with _Chapters({1: 10_000, 2: 40_000, 3: 75_000}) as chapters:
            _job(store, registry, list(paths.values()), chapters)
            limits = [store.get_intro_chapter_limit(store.get_file(paths[e]).id) for e in (1, 2, 3)]
            with patch.object(pipeline, "_decide", wraps=pipeline._decide) as decided:
                assert _job(store, registry, [paths[1]], chapters) == []
        # E3's others are 10 s and 40 s (median 25 s, limit 55 s). Its own 75 s isn't one of them: with it, the median
        # would be 40 s and the limit 80 s.
        assert limits == [(True, 115_000), (True, 85_000), (True, 55_000)]
        assert _intro_decision(store, paths[3])[:2] == (DecisionStatus.NEEDS_REVIEW, LONG_INTRO_CHAPTER_REASON)
        assert all(c.args[1].canonical_path == paths[1] for c in decided.call_args_list)  # siblings' limits held

    @pytest.mark.parametrize("stored_limit", [True, False], ids=["decided-with-a-limit", "decided-without-one"])
    def test_a_sibling_changed_on_disk_is_asked_again_and_one_run_reads_the_new_file(
        self, store, season_folder, stored_limit
    ):
        paths = self._paths(season_folder, (1, 2, 3, 4))
        registry = _registry(paths[1], ServerType.PLEX)
        lengths = {1: 10_000, 2: 126_000 if stored_limit else None, 3: 12_000, 4: 13_000}
        with _Chapters(lengths) as chapters:
            for e in (1, 2, 3):
                _write(paths[e], 100 + e)
            _job(store, registry, [paths[e] for e in (1, 2, 3)], chapters)
            assert store.get_intro_chapter_limit(store.get_file(paths[2]).id) == (
                True,
                41_000 if stored_limit else None,
            )
            lengths[2] = 11_000  # replaced by a release whose intro chapter is like the season's
            _write(paths[2], 555)
            _write(paths[4], 104)
            # Its stored decision is the old file's (its limit can't be known until the new file is read): asked again.
            assert _job(store, registry, [paths[4]], chapters) == [paths[2]]
            assert _intro_decision(store, paths[2]) == (
                DecisionStatus.DECIDED,
                "chapters",
                Marker(MarkerType.INTRO, 30_000, 41_000, ("chapters",)),
            )
            assert _job(store, registry, [paths[1]], chapters) == []  # one run healed it

    def test_a_changed_sibling_never_decided_is_left_alone(self, store, season_folder):
        paths = self._paths(season_folder, (1, 2, 3))
        for e, path in paths.items():
            _write(path, 100 + e)
        registry = _registry(paths[1], ServerType.PLEX)
        with _Chapters({1: 10_000, 2: 12_000, 3: 126_000}) as chapters:
            _job(store, registry, [paths[1]], chapters)  # E1's step records E2 and E3 without deciding them
            assert store.get_decisions(store.get_file(paths[3]).id) == {}
            _write(paths[3], 999)
            assert _job(store, registry, [paths[1]], chapters, season_job=False) == []  # its own run sees the group

    def test_a_member_read_with_older_chapter_rules_is_probed_again(self, store, season_folder):
        paths = self._paths(season_folder, (1, 2))
        for e, path in paths.items():
            _write(path, 100 + e)
        old = store.upsert_file(FileIdentity(paths[2], *_identity(paths[2])), duration_ms=DUR, season_key=None,
                                is_movie=False)  # fmt: skip
        store.replace_evidence(old.id, Source.CHAPTERS, [], version=CHAPTER_RULES_VERSION - 1)
        ctx = _ctx(store, _registry(paths[1], ServerType.PLEX), settings_raw=SEASON_RAW)
        with _Chapters({1: 10_000, 2: 12_000}) as chapters:
            season.season_intro_chapter_limits(ctx, paths[1])
        assert paths[2] in chapters.probed
        assert store.evidence_version(old.id, Source.CHAPTERS) == CHAPTER_RULES_VERSION
        assert [c.start_ms for c in store.get_evidence(old.id)] == [30_000]

    def test_a_member_replaced_while_it_is_probed_is_not_recorded(self, store, season_folder):
        paths = self._paths(season_folder, (1, 2))
        for e, path in paths.items():
            _write(path, 100 + e)
        ctx = _ctx(store, _registry(paths[1], ServerType.PLEX), settings_raw=SEASON_RAW)
        with _Chapters({1: 10_000, 2: 12_000}) as chapters:

            def probe(path, **kwargs):
                answer = chapters.probe(path, **kwargs)
                if path == paths[2]:
                    _write(path, 777)  # a new release lands while ffprobe reads the old one
                return answer

            with patch.object(season, "probe_media", side_effect=probe):
                season.season_intro_chapter_limits(ctx, paths[1])
        assert store.get_file(paths[2]) is None


class TestConcurrentChanges:
    """The season step holds only its own file's path lock: another file's newer state must win."""

    def test_a_sibling_replaced_while_another_episodes_step_probes_it_keeps_its_new_file(self, store, tmp_path):
        folder = tmp_path / "media" / "tv" / "Show {tvdb-1}" / "Season 01"
        folder.mkdir(parents=True)
        e1, e2, e3 = (str(folder / f"Show - S01E{e:02d}.mkv") for e in (1, 2, 3))
        for i, path in enumerate((e1, e2, e3), 1):
            _write(path, 100 + i)

        def probe(path, **kwargs):  # the old E2 has a 10 s intro at 30 s, the new release a 35 s intro at 60 s
            start, length = (30_000, 10_000) if os.path.getsize(path) < 500 else (60_000, 35_000)
            chapters = (
                Chapter(0, start, "C1"),
                Chapter(start, start + length, "Intro"),
                Chapter(start + length, None, "C2"),
            )
            return MediaProbe(DUR, chapters)

        a_read, a_go, b_stored, b_go = (threading.Event() for _ in range(4))
        real_identity = season._disk_identity
        calls = {"A": 0, "B": 0}

        def identity(path):
            found = real_identity(path)
            name = threading.current_thread().name
            if path == e2 and name in calls:
                calls[name] += 1
                if name == "A" and calls["A"] == 2:  # A re-read E2's identity after probing the old file
                    a_read.set()
                    a_go.wait(10)
                elif name == "B" and calls["B"] == 1:  # B stored the new E2 and reached its own season step
                    b_stored.set()
                    b_go.wait(10)
            return found

        ctx = _ctx(store, _registry(e1, ServerType.PLEX), settings_raw=SEASON_RAW)
        results = {}
        pubs = {"plex-1": ready_publisher()}
        with (
            patch.object(pipeline, "probe_media", side_effect=probe),
            patch.object(season, "probe_media", side_effect=probe),
            patch.object(pipeline, "publisher_for", side_effect=lambda server, cfg, **kw: pubs.get(cfg.id)),
            patch.object(season, "_disk_identity", side_effect=identity),
        ):
            a = threading.Thread(name="A", target=lambda: results.update(a=pipeline.check_item(_item(e1), ctx=ctx)))
            a.start()
            assert a_read.wait(10)
            _write(e2, 999)
            b = threading.Thread(name="B", target=lambda: results.update(b=pipeline.check_item(_item(e2), ctx=ctx)))
            b.start()
            assert b_stored.wait(10)
            a_go.set()
            a.join(10)
            b_go.set()
            b.join(10)
        rec = store.get_file(e2)
        st = os.stat(e2)
        assert (rec.size, rec.mtime_ns) == (st.st_size, st.st_mtime_ns)
        assert store.get_markers(rec.id)[MarkerType.INTRO] == Marker(MarkerType.INTRO, 60_000, 95_000, ("chapters",))

    def test_a_sibling_replaced_while_the_season_is_fingerprinted_makes_the_answer_due_again(self, store, tmp_path):
        folder = tmp_path / "media" / "tv" / "Show {tvdb-1}" / "Season 01"
        folder.mkdir(parents=True)
        paths = [str(folder / f"Show - S01E{e:02d}.mkv") for e in (1, 2, 3)]
        for i, path in enumerate(paths):
            _write(path, 100 + i)
        intro = noise(500, 240)

        def planted(seed, at):
            body = noise(seed, 3_000)
            body[at : at + 240] = intro
            return body

        a_pts, old_b, new_b, c_pts = planted(1, 300), planted(2, 400), planted(22, 1_400), planted(3, 700)
        current = {paths[0]: a_pts, paths[1]: old_b, paths[2]: c_pts}
        ctx = _ctx(store, _registry(paths[0], ServerType.PLEX), settings_raw=SEASON_RAW, detectors=(_spec(),))
        for path in paths[:2]:
            store.upsert_file(FileIdentity(path, *_identity(path)), duration_ms=DUR, season_key=str(folder),
                              is_movie=False)  # fmt: skip
        real_ensure = fingerprint.ensure_fingerprint
        replaced = []

        def ensure(store_, rec_, **kwargs):
            if rec_.canonical_path == paths[2] and not replaced:
                # While A's worker fingerprints C, B is replaced (the intro two minutes later) and B's own run
                # fingerprints the new file.
                replaced.append(True)
                _write(paths[1], 555)
                current[paths[1]] = new_b
                b_new = store.upsert_file(FileIdentity(paths[1], *_identity(paths[1])), duration_ms=DUR,
                                          season_key=str(folder), is_movie=False)  # fmt: skip
                real_ensure(store_, b_new, **kwargs)
            return real_ensure(store_, rec_, **kwargs)

        with (
            patch.object(fingerprint, "compute_fingerprint", side_effect=lambda path, d, **kw: current[path]),
            patch.object(season, "ensure_fingerprint", side_effect=ensure),
            patch.object(season, "probe_media", return_value=MediaProbe(DUR, ())),
            patch.object(season, "chromaprint_ffmpeg", return_value="/ffmpeg"),
        ):
            a = store.get_file(paths[0])
            _detect_and_store(ctx, a)
            b = store.get_file(paths[1])
            cached = store.get_season_pair(a.id, b.id, season.SEASON_AUDIO_VERSION)
            assert cached is None or cached == [tuple(r) for r in matcher.pair_runs(a_pts, new_b)]
            assert season.season_audio_due(a, ctx) is True
            (b_intro,) = _detect_and_store(ctx, b)
            (a_intro,) = _detect_and_store(ctx, a)
        assert abs(b_intro.start_ms - 1_400 * POINT_S * 1000) <= 500 and b_intro.origin == "2/2"
        assert abs(a_intro.start_ms - 300 * POINT_S * 1000) <= 500 and a_intro.origin == "2/2"
        assert season.season_audio_due(a, ctx) is False


CHAPTER_CHOICES = [3_000, 7_000, 10_000, 12_000, 14_000, 40_000, 60_000, 88_000, 126_000, 200_000, None]


def _fuzz_decisions(root, lengths, events, detectors):
    """Run ``events`` (arrive / rerun / replace / delete, each job optionally losing its follow-ups to a restart), then
    one more run of an episode; return the final intro decisions."""
    folder = root / "media" / "tv" / "Show (2020) {tvdb-1}" / "Season 01"
    folder.mkdir(parents=True)
    db = str(root / "markers.db")
    store = MarkerStore(db)
    paths = {e: str(folder / f"Show (2020) - S01E{e:02d}.mkv") for e in lengths}
    registry = _registry(paths[1], ServerType.PLEX)
    current = dict(lengths)
    sizes = {e: 100 + e for e in lengths}
    try:
        with _Audio(points=_episode_noise), _Chapters(current) as chapters:
            for kind, e, restart in events:
                if kind == "delete":
                    os.remove(paths[e])
                    continue
                if kind == "replace":
                    current[e] = None if current[e] else 60_000
                    sizes[e] += 1000
                if kind in ("arrive", "replace"):
                    _write(paths[e], sizes[e])
                _job(store, registry, [paths[e]], chapters, detectors=detectors, season_job=not restart)
                if restart:
                    store.close()
                    store = MarkerStore(db)
        on_disk = {e for e in lengths if os.path.exists(paths[e])}
        return {e: _intro_decision(store, paths[e])[:2] for e in sorted(on_disk)}, current, on_disk
    finally:
        store.close()


def _baseline(root, lengths, on_disk):
    folder = root / "media" / "tv" / "Show (2020) {tvdb-1}" / "Season 01"
    folder.mkdir(parents=True)
    store = MarkerStore(str(root / "markers.db"))
    paths = {e: str(folder / f"Show (2020) - S01E{e:02d}.mkv") for e in on_disk}
    try:
        for e, path in paths.items():
            _write(path, 100 + e)
        with _Chapters(lengths) as chapters:
            _job(store, _registry(next(iter(paths.values())), ServerType.PLEX), sorted(paths.values()), chapters)
        return {e: _intro_decision(store, paths[e])[:2] for e in sorted(on_disk)}
    finally:
        store.close()


def _fuzz_case(rng):
    lengths = {e: rng.choice(CHAPTER_CHOICES) for e in range(1, rng.randint(3, 6) + 1)}
    order = rng.sample(sorted(lengths), len(lengths))
    events, arrived = [], []
    for e in order:
        events.append(("arrive", e, False))
        arrived.append(e)
        roll = rng.random()
        if roll < 0.3:
            events.append(("rerun", rng.choice(arrived), False))  # an unchanged episode checked again
        elif roll < 0.45:
            events.append(("replace", rng.choice(arrived), False))  # a new release with or without an intro chapter
        elif roll < 0.5 and len(arrived) > 2:
            gone = rng.choice(arrived)
            events.append(("delete", gone, False))
            arrived.remove(gone)
        if rng.random() < 0.2:
            events[-1] = (*events[-1][:2], events[-1][0] != "delete")  # the app restarts before the Season job
    events.append(("rerun", rng.choice(arrived), False))  # the season runs again some time later
    return lengths, events


@pytest.mark.timeout(120)  # 40 fuzz cases take ~26 s alone; addopts' --timeout=30 killed the worker under load
@pytest.mark.parametrize("with_audio", [False, True], ids=["chapters-only", "season-audio-worker"])
def test_arrivals_reruns_replacements_deletions_and_restarts_end_with_the_all_at_once_decisions(tmp_path, with_audio):
    rng = random.Random(20260915 + with_audio)
    detectors = (_spec(),) if with_audio else ()
    mismatches = []
    for case in range(40):
        lengths, events = _fuzz_case(rng)
        got, final, on_disk = _fuzz_decisions(tmp_path / f"run{case}", lengths, events, detectors)
        want = _baseline(tmp_path / f"base{case}", final, on_disk)
        if got != want:
            mismatches.append((lengths, events, got, want))
    assert mismatches == []


class TestFlatFolder:
    def test_siblings_whose_answers_match_their_own_groups_are_not_asked_again(self, store, tmp_path):
        folder = tmp_path / "media" / "anime" / "Show {tvdb-1}"
        folder.mkdir(parents=True)
        paths = [str(folder / f"Show - S01E{e:02d}.mkv") for e in range(1, 46)]
        for path in paths:
            _write(path, 100)
        ctx = _season_ctx(store, paths[0])
        review = TypeDecision(MarkerType.INTRO, DecisionStatus.NEEDS_REVIEW, None, None, "sources don't agree yet")
        recs = []
        for path in paths:
            rec = store.upsert_file(
                FileIdentity(path, *_identity(path)), duration_ms=DUR, season_key=None, is_movie=False
            )
            store.save_decisions(rec.id, {MarkerType.INTRO: review}, settings_fingerprint="x")
            store.set_detector_run(
                rec.id, Source.SEASON_AUDIO, season._signature(ctx, season.season_group(path).episodes)
            )
            recs.append(rec)
        assert season.season_group(paths[39]).episodes != season.season_group(paths[0]).episodes
        assert season.season_audio_followups(recs[0], ctx) == []

    def test_a_300_episode_folder_matches_and_compares_40_episodes(self, store, tmp_path):
        folder = tmp_path / "media" / "anime" / "Show {tvdb-1}"
        folder.mkdir(parents=True)
        paths = [str(folder / f"Show - S01E{e:03d}.mkv") for e in range(1, 301)]
        for i, path in enumerate(paths):
            _write(path, 100 + i % 7)
        target = paths[149]
        probed = []

        def probe(path, **kwargs):  # no chapters, so the intro stays undecided and season audio runs
            probed.append(path)
            return _chapter_probe(None)

        ctx = _ctx(store, _registry(target, ServerType.PLEX), settings_raw=SEASON_RAW, detectors=(_spec(),))
        with _Audio(points=_episode_noise) as audio, patch.object(season, "probe_media", side_effect=probe):
            started = time.perf_counter()
            _run(ctx, target, {"plex-1": ready_publisher()}, stage="process", probe_effect=probe)
            first = time.perf_counter() - started
            started = time.perf_counter()
            assert _run(ctx, target, {"plex-1": ready_publisher()}, probe_effect=probe)[0] is not None
            again = time.perf_counter() - started
        group = set(paths[129:169])
        assert set(audio.computed) == group and len(audio.computed) == 40
        assert set(probed) == group and len(probed) == 40
        assert first < 10.0 and again < 2.0  # the whole folder would be 300 fingerprints and 44,850 pairs

    def test_a_change_reaches_a_file_whose_group_holds_the_changed_episode_but_not_the_other_way_round(
        self, store, tmp_path
    ):
        # 45 episodes: E6's group is E1-E40, E41's is E6-E45. Replacing E6 changes E41's limit though E41 isn't in E6's.
        folder = tmp_path / "media" / "tv" / "Show (2020) {tvdb-1}"
        folder.mkdir(parents=True)
        paths = {e: str(folder / f"Show (2020) - S01E{e:02d}.mkv") for e in range(1, 46)}
        for e, path in paths.items():
            _write(path, 100 + e)
        assert paths[6] in season.season_group(paths[41]).episodes
        assert paths[41] not in season.season_group(paths[6]).episodes
        lengths = {e: 60_000 if e >= 6 and e % 2 == 0 else 10_000 for e in paths}
        lengths[41] = 100_000
        registry = _registry(paths[1], ServerType.PLEX)
        with _Chapters(lengths) as chapters:
            _job(store, registry, list(paths.values()), chapters)
            # E41's others: 20 of 60 s, 19 of 10 s (median 60 s, limit 120 s).
            assert _intro_decision(store, paths[41])[:2] == (DecisionStatus.DECIDED, "chapters")
            lengths[6] = None  # E6 replaced by a release without an intro chapter: E41's limit drops to 70 s
            _write(paths[6], 999)
            assert _job(store, registry, [paths[6]], chapters) == [paths[41]]
        assert _intro_decision(store, paths[41])[:2] == (DecisionStatus.NEEDS_REVIEW, LONG_INTRO_CHAPTER_REASON)

    def test_a_change_reaches_a_file_whose_group_holds_the_changed_episode_for_season_audio_too(self, store, tmp_path):
        # E41's group is E6-E45. The intro is in E41 and 20 of its 39 others (E6-E25), the quorum's edge: E6's replacement
        # drops E41's answer, though E41 isn't in E6's group.
        folder = tmp_path / "media" / "tv" / "Show (2020) {tvdb-1}"
        folder.mkdir(parents=True)
        paths = {e: str(folder / f"Show (2020) - S01E{e:02d}.mkv") for e in range(1, 46)}
        for e, path in paths.items():
            _write(path, 100 + e)
        intro = noise(7, 240)
        with_intro = set(range(6, 26)) | {41}
        replaced = set()

        def points(path):
            e = int(re.search(r"E(\d+)", os.path.basename(path)).group(1))
            body = noise(e, 1_500)
            if e in with_intro and e not in replaced:
                body[200:440] = intro
            return body

        clients = _introdb_answer(round(200 * POINT_S * 1000), round(439 * POINT_S * 1000))
        registry = _registry(paths[1], ServerType.PLEX)

        def job(items):
            ctx = _ctx(store, registry, settings_raw=SEASON_RAW, detectors=(_spec(),), clients=clients)
            for path in items:
                if _run(ctx, path, {"plex-1": ready_publisher()})[0] is None:
                    _run(ctx, path, {"plex-1": ready_publisher()}, stage="process")
            requested = ctx.take_followups()
            season_job = _ctx(store, registry, settings_raw=SEASON_RAW, detectors=(_spec(),), clients=clients)
            for path in requested:
                if path not in items and _run(season_job, path, {"plex-1": ready_publisher()})[0] is None:
                    _run(season_job, path, {"plex-1": ready_publisher()}, stage="process")
            return requested

        with _Audio(points=points):
            job(list(paths.values()))
            e41 = store.get_file(paths[41])
            assert store.get_markers(e41.id)[MarkerType.INTRO].decided_by == ("introdb", "season_audio")
            replaced.add(6)
            _write(paths[6], 999)  # a release whose opening doesn't match
            assert paths[41] in job([paths[6]])
        assert store.get_decisions(e41.id)[MarkerType.INTRO].status is DecisionStatus.NEEDS_REVIEW
        assert MarkerType.INTRO not in store.get_markers(e41.id)


def _record_intro_chapter(store, path, length_ms, chapter_version=CHAPTER_RULES_VERSION):
    rec = store.upsert_file(FileIdentity(path, *_identity(path)), duration_ms=DUR, season_key=os.path.dirname(path),
                            is_movie=False)  # fmt: skip
    store.replace_evidence(
        rec.id, Source.CHAPTERS, chapter_candidates(_chapter_probe(length_ms)), version=chapter_version
    )
    return rec


class TestGroupsHolding:
    """The files whose own group holds an episode: in a flat folder, not only the episode's own group."""

    @staticmethod
    def _videos(names):
        return tuple(season._folder_video(f"/tv/Show {{tvdb-1}}/{name}") for name in names)

    @staticmethod
    def _searched(target, videos):
        return tuple(
            sorted(
                v.path for v in videos if v.path != target and target in season.season_group(v.path, videos).episodes
            )
        )

    def test_every_file_of_a_season_of_40_or_fewer_holds_it(self):
        videos = self._videos([f"Show - S01E{e:02d}.mkv" for e in range(1, 41)] + ["Show - S02E01.mkv"])
        assert season.groups_holding(videos[0].path, videos) == tuple(v.path for v in videos[1:40])

    def test_a_file_with_38_others_between_can_still_hold_it(self):
        videos = self._videos([f"Show - S01E{e:02d}.mkv" for e in range(1, 81)])
        e1, e40 = videos[0].path, videos[39].path
        holding = season.groups_holding(e40, videos)
        assert e1 in holding and holding == self._searched(e40, videos)  # E1's group is E1-E40

    def test_it_finds_every_one_with_duplicate_sparse_or_no_episode_numbers(self):
        rng = random.Random(3)
        for trial in range(40):
            numbered = trial % 5 != 4
            names, episode = [], 1
            for k in range(rng.randint(41, 140)):
                episode += rng.choice([0, 0, 1, 1, 1, 2, 5, 20]) if rng.random() < 0.5 else 1
                names.append(f"Show - S01E{episode:03d} - {k}.mkv" if numbered else f"Show - Part {k:03d}.mkv")
            videos = self._videos(names)
            target = rng.choice(videos).path
            assert season.groups_holding(target, videos) == self._searched(target, videos), (trial, target)


class TestSiblingLimits:
    """What ``season_intro_chapter_limits`` gives a sibling in a flat folder: its own group's limit, from current chapter
    reads only."""

    @pytest.fixture
    def flat(self, tmp_path):
        folder = tmp_path / "media" / "anime" / "Show {tvdb-1}"
        folder.mkdir(parents=True)
        paths = {e: str(folder / f"Show - S01E{e:02d}.mkv") for e in range(1, 81)}
        for e, path in paths.items():
            _write(path, 100 + e)
        return paths

    def test_a_siblings_limit_comes_from_its_own_group(self, store, flat):
        # E1's group is E1-E40, E40's is E20-E59. E1-E20 have 100 s intro chapters, the rest 10 s (E40 30 s).
        assert season.season_group(flat[40]).episodes == tuple(flat[e] for e in range(20, 60))
        for e, path in flat.items():
            _record_intro_chapter(store, path, 100_000 if e <= 20 else 30_000 if e == 40 else 10_000)
        ctx = _ctx(store, _registry(flat[1], ServerType.PLEX), settings_raw=SEASON_RAW)
        with _Chapters({}) as chapters:
            _, siblings = season.season_intro_chapter_limits(ctx, flat[1])
        assert chapters.probed == []
        # Its own others: one 100 s and 38 of 10 s (limit 40 s). E1's group would give a median of 100 s (limit 200 s).
        assert siblings[flat[40]] == 40_000
        # E41-E80 are among E1's 80 nearest, but none of their groups holds E1.
        assert set(siblings) == {flat[e] for e in range(2, 41)}

    def test_members_outside_this_episodes_group_count_only_with_current_chapter_rules(self, store, flat):
        # E40's others outside E1's group (E41-E59) hold 200 s chapters read with older rules: they don't count.
        for e, path in flat.items():
            old = 41 <= e <= 59
            length = 200_000 if old or e == 20 else 30_000 if e == 40 else 10_000
            _record_intro_chapter(store, path, length, CHAPTER_RULES_VERSION - 1 if old else CHAPTER_RULES_VERSION)
        ctx = _ctx(store, _registry(flat[1], ServerType.PLEX), settings_raw=SEASON_RAW)
        with _Chapters({}) as chapters:
            _, siblings = season.season_intro_chapter_limits(ctx, flat[1])
        assert chapters.probed == []  # members outside this episode's group are read from the store only
        # Current: one 200 s and 19 of 10 s (median 10 s, limit 40 s); with the old reads the median would be 200 s.
        assert siblings[flat[40]] == 40_000


class TestLimitStored:
    def test_the_limit_is_stored_when_it_changes_though_the_decision_does_not(self, store, tmp_path):
        folder = tmp_path / "media" / "tv" / "Show (2020) {tvdb-1}" / "Season 01"
        folder.mkdir(parents=True)
        paths = {e: str(folder / f"Show (2020) - S01E{e:02d}.mkv") for e in (1, 2, 3, 4)}
        registry = _registry(paths[1], ServerType.PLEX)
        with _Chapters({1: 10_000, 2: 12_000, 3: 14_000, 4: 16_000}) as chapters:
            for e in (1, 2, 3):
                _write(paths[e], 100 + e)
            _job(store, registry, [paths[e] for e in (1, 2, 3)], chapters)
            e1 = store.get_file(paths[1])
            assert store.get_intro_chapter_limit(e1.id) == (True, 43_000)  # others 12 s and 14 s
            decided_at = store.get_decisions(e1.id)[MarkerType.INTRO].decided_at
            _write(paths[4], 104)
            assert _job(store, registry, [paths[4]], chapters) == []  # E1's decision doesn't change
            _job(store, registry, [paths[1]], chapters)
        assert store.get_decisions(e1.id)[MarkerType.INTRO].decided_at == decided_at  # not saved again
        assert store.get_intro_chapter_limit(e1.id) == (True, 44_000)  # others 12, 14 and 16 s


class TestServerKindOfSiblings:
    """A sibling is re-decided with the kind its own run used: a file whose name has no SxxEyy is an episode only
    because its server says so."""

    @pytest.mark.parametrize("kind", ["episode", "movie"])
    def test_a_sibling_whose_decision_doesnt_change_is_not_asked_again(self, store, tmp_path, kind):
        folder = tmp_path / "media" / "anime" / "Show {tvdb-1}" / "Season 01"
        folder.mkdir(parents=True)
        lengths = {"101": 10_000, "102": 12_000, "103": 14_000, "104": 16_000}
        paths = {n: str(folder / f"Show - {n}.mkv") for n in lengths}
        assert not any(ids_from_path(p).is_episode for p in paths.values())
        registry = _registry(paths["101"], ServerType.PLEX)
        registry.get("plex-1").get_external_ids.return_value = EPISODE_IDS if kind == "episode" else MOVIE_IDS

        def probe(path, **kwargs):
            return _chapter_probe(lengths[os.path.basename(path)[7:10]])

        def job(names):
            ctx = _ctx(store, registry, settings_raw=SEASON_RAW)
            for name in names:
                _run(ctx, paths[name], {"plex-1": ready_publisher()}, probe_effect=probe)
            return ctx.take_followups()

        with patch.object(season, "probe_media", side_effect=probe):
            for name in ("101", "102", "103"):
                _write(paths[name], 100)
            job(["101", "102", "103"])
            expected = DecisionStatus.DECIDED if kind == "episode" else DecisionStatus.DISABLED
            assert store.get_decisions(store.get_file(paths["101"]).id)[MarkerType.INTRO].status is expected
            _write(paths["104"], 100)
            # E101's limit changes (43 s to 44 s) and its 10 s chapter still decides: nothing to ask. A movie's run has
            # no intro to compare.
            assert job(["104"]) == []


class TestMemberEpisodeOnlyChapterNames:
    """The season step stores each member's chapter evidence, so it decides the episode-only chapter names
    (spec §5.1's "Ending") for files no run of their own has reached yet -- and it reads each member's own
    path for that, never the asking episode's kind."""

    ENDING = MediaProbe(
        DUR,
        (
            Chapter(0, 30_000, "Chapter 1"),
            Chapter(30_000, 120_000, "Intro"),
            Chapter(120_000, 1_200_000, "Part B"),
            Chapter(1_200_000, None, "Ending"),
        ),
    )

    def _limits(self, store, tmp_path, folder_name, names):
        folder = tmp_path / "media" / "tv" / folder_name / "Season 01"
        folder.mkdir(parents=True)
        paths = [str(folder / name) for name in names]
        for i, path in enumerate(paths):
            _write(path, 100 + i)
        ctx = _ctx(store, _registry(paths[0], ServerType.PLEX), settings_raw=SEASON_RAW)
        with patch.object(season, "probe_media", side_effect=lambda path, **kw: self.ENDING):
            season.season_intro_chapter_limits(ctx, paths[0])
        return paths

    @staticmethod
    def _chapter_rows(store, path):
        rec = store.get_file(path)
        return [(r.type, r.label) for r in store.evidence_rows(rec.id) if r.source is Source.CHAPTERS]

    def test_a_member_named_like_an_episode_gets_the_ending_credits_candidate(self, store, tmp_path):
        paths = self._limits(
            store, tmp_path, "Show (2020) {tvdb-1}", ["Show - S01E01.mkv", "Show - S01E02.mkv", "Show - S01E03.mkv"]
        )
        # The sibling the season step probed on E01's behalf, before any run of its own.
        assert self._chapter_rows(store, paths[1]) == [(MarkerType.INTRO, "Intro"), (MarkerType.CREDITS, "Ending")]
        assert store.evidence_version(store.get_file(paths[1]).id, Source.CHAPTERS) == CHAPTER_RULES_VERSION

    def test_a_member_whose_name_has_no_sxxeyy_does_not(self, store, tmp_path):
        # A flat/absolute-numbered folder: season_group takes every file whose parsed season is None, so a
        # member can be a file its own run would not read as an episode. Handing the asking episode's kind
        # down would give such a member the episode-only reading of "Ending" -- the last-scene mistake the
        # scope exists to prevent.
        paths = self._limits(store, tmp_path, "Show {tvdb-1}", ["Show - 101.mkv", "Show - 102.mkv"])
        assert not any(ids_from_path(p).is_episode for p in paths)
        assert self._chapter_rows(store, paths[1]) == [(MarkerType.INTRO, "Intro")]


class TestUnreadableMembers:
    """A member ffprobe can't read isn't probed again on every sibling's checking thread (up to 60 s each)."""

    @pytest.mark.parametrize("failure", ["probe-error", "no-duration"])
    def test_it_is_probed_again_after_a_day_or_once_it_changes(self, store, show, failure):
        e1, e2, e3 = show(1, 3)
        clock = [datetime(2026, 9, 13, 12, 0, tzinfo=UTC)]
        ctx = _ctx(store, _registry(e1, ServerType.PLEX), settings_raw=SEASON_RAW, now=lambda: clock[0])
        probed = []

        def probe(path, **kwargs):
            probed.append(path)
            if path == e2 and os.path.getsize(path) < 500:
                if failure == "probe-error":
                    raise ProbeError("ffprobe timed out")
                return MediaProbe(None, ())
            return _chapter_probe(10_000)

        def step():
            probed.clear()
            season.season_intro_chapter_limits(ctx, e1)
            return probed.count(e2)

        with patch.object(season, "probe_media", side_effect=probe):
            assert step() == 1 and store.get_file(e2) is None
            assert step() == 0
            clock[0] += timedelta(hours=23, minutes=59)
            assert step() == 0
            clock[0] += timedelta(minutes=1)
            assert step() == 1  # a day later
            assert step() == 0
            _write(e2, 600)  # a new release: readable now
            assert step() == 1
        assert store.get_file(e2).size == 600

    def test_the_entry_of_a_member_deleted_since_is_forgotten_once_it_stops_counting(self, store, show):
        e1, e2 = show(1, 2)
        clock = [datetime(2026, 9, 13, 12, 0, tzinfo=UTC)]
        ctx = _ctx(store, _registry(e1, ServerType.PLEX), settings_raw=SEASON_RAW, now=lambda: clock[0])

        def probe(path, **kwargs):
            if path != e1:
                raise ProbeError("ffprobe timed out")
            return _chapter_probe(10_000)

        with patch.object(season, "probe_media", side_effect=probe):
            season.season_intro_chapter_limits(ctx, e1)
            assert store._count("member_probe_failures") == 1
            os.remove(e2)  # never probed, or recorded, again
            e3 = e2.replace("E02", "E03")
            _write(e3, 103)  # a new episode the season step can't read either
            clock[0] += timedelta(days=1, minutes=1)
            season.season_intro_chapter_limits(ctx, e1)
        assert store.member_probe_failed_at(FileIdentity(e3, *_identity(e3))) == clock[0]
        assert store._count("member_probe_failures") == 1  # E2's entry went with E3's write

    def test_a_forced_re_detect_probes_it_again_within_the_day(self, store, show):
        e1, e2, _ = show(1, 3)
        registry = _registry(e1, ServerType.PLEX)
        network_down = [True]
        probed = []

        def probe(path, **kwargs):
            probed.append(path)
            if path == e2 and network_down[0]:
                raise ProbeError("ffprobe timed out")
            return _chapter_probe(10_000)

        with patch.object(season, "probe_media", side_effect=probe):
            season.season_intro_chapter_limits(_ctx(store, registry, settings_raw=SEASON_RAW), e1)
            network_down[0] = False
            probed.clear()
            season.season_intro_chapter_limits(_ctx(store, registry, settings_raw=SEASON_RAW), e1)
            assert e2 not in probed
            season.season_intro_chapter_limits(_ctx(store, registry, settings_raw=SEASON_RAW, force=True), e1)
        assert probed == [e2] and store.get_file(e2) is not None

    def test_a_member_its_own_run_recorded_is_read_by_a_siblings_step_within_the_day(self, store, show):
        e1, e2, e3 = show(1, 3)
        ctx = _ctx(store, _registry(e1, ServerType.PLEX), settings_raw=SEASON_RAW)
        lengths = {e1: 10_000, e2: 12_000, e3: 14_000}

        def season_probe(path, **kwargs):  # the season step can't read E2 (a network blip)
            if path == e2:
                raise ProbeError("ffprobe timed out")
            return _chapter_probe(lengths[path])

        with patch.object(season, "probe_media", side_effect=season_probe):
            _, before = season.season_intro_chapter_limits(ctx, e1)
            assert store.get_file(e2) is None and before[e3] is None  # E3 has one other intro chapter: no limit
            _run(ctx, e2, {"plex-1": ready_publisher()}, probe_effect=lambda path, **kw: _chapter_probe(lengths[path]))
            _, after = season.season_intro_chapter_limits(ctx, e1)
        assert after[e3] == 41_000  # E1's 10 s and E2's 12 s: E2's own run recorded it, so the failure doesn't apply

    def test_a_member_replaced_while_it_is_probed_is_not_remembered_as_unreadable(self, store, show):
        e1, e2 = show(1, 2)
        ctx = _ctx(store, _registry(e1, ServerType.PLEX), settings_raw=SEASON_RAW)
        answers = iter([ProbeError("the file was being written"), _chapter_probe(10_000)])

        def probe(path, **kwargs):
            if path != e2:
                return _chapter_probe(10_000)
            answer = next(answers)
            if isinstance(answer, Exception):
                os.utime(path, ns=(5, 5))
                raise answer
            return answer

        with patch.object(season, "probe_media", side_effect=probe):
            season.season_intro_chapter_limits(ctx, e1)
            assert store.get_file(e2) is None
            season.season_intro_chapter_limits(ctx, e1)
        assert store.get_file(e2) is not None


SEASON_INTRO_AT, COLD_OPEN_AT = 900, 100
SEASON_INTRO, COLD_OPEN = noise(1, 240), noise(2, 240)


def _point_ms(at):
    return round(at * POINT_S * 1000), round((at + 239) * POINT_S * 1000)


def _cold_open_points(episodes):
    def points(path):
        e = int(re.search(r"E(\d+)", os.path.basename(path)).group(1))
        body = noise(100 + e, N_POINTS)
        if episodes[e]["y"]:
            body[COLD_OPEN_AT : COLD_OPEN_AT + 240] = COLD_OPEN
        if episodes[e]["x"]:
            body[SEASON_INTRO_AT : SEASON_INTRO_AT + 240] = SEASON_INTRO
        return body

    return points


def _cold_open_season(root, episodes, arrivals, reruns=None):
    """A season where some episodes share the season's intro (``x``), some a cold open (``y``), and some have an intro
    chapter on one of them (``chapter``: "x", "y" or None). IntroDB (wrongly) gives the cold open as every episode's
    intro. Each batch of ``arrivals`` is written and run as one job, after an optional re-run of an episode already on
    disk (``reruns``), and each job's follow-ups run as the Season job (Task 8) runs them.

    Returns:
        The final intro decisions of the episodes that arrived, every job's follow-up requests, and whether each
        arrived episode's own run ever ran season audio.
    """
    folder = root / "media" / "tv" / "Show (2020) {tvdb-1}" / "Season 01"
    folder.mkdir(parents=True)
    store = MarkerStore(str(root / "markers.db"))
    paths = {e: str(folder / f"Show (2020) - S01E{e:02d}.mkv") for e in episodes}
    registry = _registry(paths[min(paths)], ServerType.PLEX)
    clients = _introdb_answer(*_point_ms(COLD_OPEN_AT))
    requests = []

    def probe(path, **kwargs):
        chapter = episodes[int(re.search(r"E(\d+)", os.path.basename(path)).group(1))]["chapter"]
        if chapter is None:
            return _chapter_probe(None)
        start, end = _point_ms(SEASON_INTRO_AT if chapter == "x" else COLD_OPEN_AT)
        return _chapter_probe(end - start, at_ms=start)

    def run(ctx, path):
        if _run(ctx, path, {"plex-1": ready_publisher()}, probe_effect=probe)[0] is None:
            _run(ctx, path, {"plex-1": ready_publisher()}, stage="process", probe_effect=probe)

    def job(items):
        ctx = _ctx(store, registry, settings_raw=SEASON_RAW, detectors=(_spec(),), clients=clients)
        for path in items:
            run(ctx, path)
        requested = ctx.take_followups()
        requests.append(requested)
        follow = _ctx(store, registry, settings_raw=SEASON_RAW, detectors=(_spec(),), clients=clients)
        for path in requested:
            if path not in items:
                run(follow, path)

    on_disk = []
    try:
        with _Audio(points=_cold_open_points(episodes)), patch.object(season, "probe_media", side_effect=probe):
            for batch, rerun in zip(arrivals, reruns or [None] * len(arrivals), strict=True):
                for e in batch:
                    _write(paths[e], 100 + e)
                if rerun is not None and on_disk:
                    job([paths[on_disk[rerun % len(on_disk)]]])
                job([paths[e] for e in batch])
                on_disk.extend(batch)
        decisions = {e: _intro_decision(store, paths[e]) for e in sorted(on_disk)}
        ran = {e: store.get_detector_run(store.get_file(paths[e]).id, Source.SEASON_AUDIO) is not None for e in on_disk}
        return decisions, requests, ran
    finally:
        store.close()


class TestAnswersRestingOnSeasonAudio:
    """An intro decided with a season audio answer is decided again when the season's audio changes, so the order the
    episodes arrive in doesn't decide what is published."""

    COLD_OPEN_MARKER = Marker(MarkerType.INTRO, *_point_ms(COLD_OPEN_AT), ("introdb", "season_audio"))

    def test_an_early_match_agreeing_with_a_wrong_online_answer_is_asked_again_when_the_season_grows(self, tmp_path):
        # E1 and E2 share a cold open IntroDB lists as the intro; E1, E3 and E4 have the season's intro.
        episodes = {
            1: {"x": True, "y": True, "chapter": None},
            2: {"x": False, "y": True, "chapter": None},
            3: {"x": True, "y": False, "chapter": None},
            4: {"x": True, "y": False, "chapter": None},
        }
        early, _, _ = _cold_open_season(tmp_path / "early", episodes, [[1], [2]])
        assert early[1] == (DecisionStatus.DECIDED, "sources agree: introdb, season_audio", self.COLD_OPEN_MARKER)
        weekly, requests, _ = _cold_open_season(tmp_path / "weekly", episodes, [[1], [2], [3, 4]])
        at_once, _, _ = _cold_open_season(tmp_path / "at-once", episodes, [[1, 2, 3, 4]])
        idb_alone = "only IntroDB has the intro; an online answer needs a check against the file"
        assert weekly[1] == at_once[1] == (DecisionStatus.NEEDS_REVIEW, idb_alone, None)
        assert weekly == at_once
        assert "Show (2020) - S01E01.mkv" in {os.path.basename(p) for p in requests[-1]}

    def test_an_episode_decided_by_its_chapter_still_asks_again_for_a_sibling_resting_on_season_audio(self, tmp_path):
        # E3 arrives with an intro chapter: it decides before season audio, so its own run never matches the season.
        episodes = {
            1: {"x": True, "y": True, "chapter": None},
            2: {"x": False, "y": True, "chapter": None},
            3: {"x": True, "y": False, "chapter": "x"},
            4: {"x": True, "y": False, "chapter": "x"},
        }
        early, _, _ = _cold_open_season(tmp_path / "early", episodes, [[1], [2]])
        assert early[1][2] == self.COLD_OPEN_MARKER
        weekly, requests, ran = _cold_open_season(tmp_path / "weekly", episodes, [[1], [2], [3], [4]])
        at_once, _, _ = _cold_open_season(tmp_path / "at-once", episodes, [[1, 2, 3, 4]])
        assert (ran[3], ran[4]) == (False, False)
        assert weekly == at_once and weekly[1][2] != self.COLD_OPEN_MARKER
        assert any("S01E01" in p for p in requests[2])  # E3's job, before E3 ever runs season audio

    @pytest.mark.parametrize(
        ("state", "listed"),
        [
            ("no-answer", False),
            ("answer-current", False),
            ("undecided", True),
            ("decided-by-an-online-source", False),
            ("decided-with-season-audio", True),
            # Left to every server's own intro: season audio never runs for it, so its answer never catches up.
            ("kept-own", False),
            ("changed-on-disk", False),
            ("resized-with-the-same-mtime", False),
        ],
    )
    def test_the_season_step_lists_siblings_whose_outdated_answer_could_change_their_intro(
        self, store, show, state, listed
    ):
        e1, e2, e3 = show(1, 3)
        recs = {
            p: store.upsert_file(FileIdentity(p, *_identity(p)), duration_ms=DUR, season_key=None, is_movie=False)
            for p in (e1, e2, e3)
        }
        ctx = _season_ctx(store, e1)
        sibling = recs[e2]
        if state != "no-answer":
            current = season._signature(ctx, season.season_group(e2).episodes)
            store.set_detector_run(sibling.id, Source.SEASON_AUDIO, current if state == "answer-current" else "older")
        decided_by = {
            "decided-by-an-online-source": ("skipdb",),
            "decided-with-season-audio": ("introdb", "season_audio"),
        }
        if state in decided_by:
            decision = _decided(MarkerType.INTRO, 10_000, 40_000, decided_by[state])
        elif state == "kept-own":
            decision = TypeDecision(MarkerType.INTRO, DecisionStatus.DISABLED, None, None, kept_own_reason(["Plex"]))
        else:
            decision = TypeDecision(
                MarkerType.INTRO, DecisionStatus.NEEDS_REVIEW, None, None, "sources don't agree yet"
            )
        store.save_decisions(sibling.id, {MarkerType.INTRO: decision}, settings_fingerprint="x")
        if state == "changed-on-disk":
            _write(e2, 999)
        if state == "resized-with-the-same-mtime":
            _write(e2, 999)
            os.utime(e2, ns=(sibling.mtime_ns, sibling.mtime_ns))
        assert season.season_audio_followups(recs[e1], ctx) == ([e2] if listed else [])
        if state in ("undecided", "kept-own"):
            # The two other predicates on the same conditions: after a job, and a run re-deciding its siblings.
            assert season.season_audio_answer_outdated(ctx, e2) is listed
            season._request_redecide(ctx, recs[e1], {e2: sibling}, "newer", matched={})
            assert ctx.take_followups() == ([e2] if listed else [])

    def test_a_new_episode_of_a_long_season_asks_again_only_for_siblings_it_changed(self, store, show):
        # Found on the owner's server (Daily Show S31, re-checked 11 times in 12 h, publishing nothing): past 40
        # episodes each file's group is its own 40 nearest, so a sibling's answer never equals the new episode's and
        # every sibling it matched was asked again, including the 20 whose group doesn't hold it.
        def points(path):
            key = re.search(r"S\d\dE\d\d", path).group(0)
            body = noise(zlib.crc32(key.encode()), N_POINTS)
            body[300:540] = INTRO
            return body

        paths = show(1, 45)
        with _Audio(points=points):
            for path in paths:
                _run(_season_ctx(store, path), path, {"plex-1": ready_publisher()}, stage="process")
            assert not any(season.season_audio_answer_outdated(_season_ctx(store, p), p) for p in paths)
            new = show(1, 46)[-1]
            ctx = _season_ctx(store, new)
            _run(ctx, new, {"plex-1": ready_publisher()}, stage="process")
        requested = ctx.take_followups()
        outdated = [p for p in paths if season.season_audio_answer_outdated(_season_ctx(store, p), p)]
        assert len(outdated) == 19  # E27..E45 hold E46 in their group; E07..E26, also in E46's, don't
        assert requested == outdated


FUZZ_SEASONS, FUZZ_CHUNKS = 120, 4


def _fuzz_cases(mode):
    """120 seasons of 3-6 episodes, each arriving one a week in a random order.

    The chapter modes are the re-reviewer's generator (seed 7): an intro chapter length or none per episode, and with
    re-runs an unchanged episode checked before about half the arrivals. The cold-open mode (seed 11) draws which
    episodes have the season's intro, a cold open IntroDB wrongly lists, and an intro chapter on either.
    """
    cases = []
    if mode == "cold-open":
        rng = random.Random(11)
        for _ in range(FUZZ_SEASONS):
            count = rng.randint(3, 6)
            episodes = {
                e: {"x": rng.random() < 0.75, "y": rng.random() < 0.4, "chapter": rng.choice([None] * 5 + ["x", "y"])}
                for e in range(1, count + 1)
            }
            order = rng.sample(sorted(episodes), count)
            cases.append((episodes, order, [rng.randrange(10) if rng.random() < 0.4 else None for _ in order]))
        return cases
    rng = random.Random(7)
    for _ in range(FUZZ_SEASONS):
        count = rng.randint(3, 6)
        lengths = {e: rng.choice(CHAPTER_CHOICES) for e in range(1, count + 1)}
        order = rng.sample(list(lengths), count)
        cases.append(
            (lengths, order, [rng.randrange(10) if mode == "reruns" and rng.random() < 0.5 else None for _ in order])
        )
    return cases


def _chapter_season(root, lengths, arrivals, reruns, detectors):
    folder = root / "media" / "tv" / "Show (2020) {tvdb-1}" / "Season 01"
    folder.mkdir(parents=True)
    store = MarkerStore(str(root / "markers.db"))
    paths = {e: str(folder / f"Show (2020) - S01E{e:02d}.mkv") for e in lengths}
    registry = _registry(paths[1], ServerType.PLEX)
    on_disk = []
    try:
        with _Audio(points=_episode_noise), _Chapters(lengths) as chapters:
            for batch, rerun in zip(arrivals, reruns, strict=True):
                for e in batch:
                    _write(paths[e], 100 + e)
                if rerun is not None and on_disk:
                    _job(store, registry, [paths[on_disk[rerun % len(on_disk)]]], chapters, detectors=detectors)
                _job(store, registry, [paths[e] for e in batch], chapters, detectors=detectors)
                on_disk.extend(batch)
        return {e: _intro_decision(store, paths[e]) for e in lengths}
    finally:
        store.close()


@pytest.mark.parametrize("chunk", range(FUZZ_CHUNKS))
@pytest.mark.parametrize("mode", ["weekly", "reruns", "season-audio", "cold-open"])
def test_weekly_arrivals_end_with_the_all_at_once_decisions(tmp_path, mode, chunk):
    size = FUZZ_SEASONS // FUZZ_CHUNKS
    mismatches = []
    for i, (inputs, order, reruns) in enumerate(_fuzz_cases(mode)[chunk * size : (chunk + 1) * size]):
        weekly = [[e] for e in order]
        if mode == "cold-open":
            want, _, _ = _cold_open_season(tmp_path / f"all{i}", inputs, [sorted(inputs)])
            got, _, _ = _cold_open_season(tmp_path / f"weekly{i}", inputs, weekly, reruns)
        else:
            detectors = (_spec(),) if mode == "season-audio" else ()
            want = _chapter_season(tmp_path / f"all{i}", inputs, [sorted(inputs)], [None], detectors)
            got = _chapter_season(tmp_path / f"weekly{i}", inputs, weekly, reruns, detectors)
        if got != want:
            mismatches.append((inputs, order, reruns, {e: (want[e], got[e]) for e in want if want[e] != got[e]}))
    assert mismatches == []
