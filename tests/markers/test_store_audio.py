"""markers.db audio tables: fingerprints gated on file identity, pair cache, detector runs, invalidation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from media_preview_generator.markers.models import Candidate, FileIdentity, MarkerType, Source
from media_preview_generator.markers.store import MarkerStore, StoredFingerprint


@pytest.fixture
def store(tmp_path):
    s = MarkerStore(str(tmp_path / "markers.db"))
    yield s
    s.close()


def _file(store, name="/m/S01E01.mkv", size=100, mtime=1):
    return store.upsert_file(FileIdentity(name, size, mtime), duration_ms=1_300_000, season_key="/m", is_movie=False)


def _fp(store, rec, points=b"\x01\x00\x00\x00\x02\x00\x00\x00", **overrides):
    kwargs = dict(size=rec.size, mtime_ns=rec.mtime_ns, window="intro", start_s=0.0, length_s=455.0, algorithm=1)
    kwargs.update(overrides)
    return store.set_fingerprint(rec.id, points=points, **kwargs)


def _pair(store, a, b, runs, version=3):
    return store.set_season_pair(
        a.id, b.id, version, runs, identity_a=(a.size, a.mtime_ns), identity_b=(b.size, b.mtime_ns)
    )


class TestFingerprints:
    def test_round_trip(self, store):
        rec = _file(store)
        assert _fp(store, rec) is True
        assert store.get_fingerprint(rec.id, "intro") == StoredFingerprint(
            rec.id, "intro", 0.0, 455.0, 1, b"\x01\x00\x00\x00\x02\x00\x00\x00"
        )
        assert store.get_fingerprint(rec.id, "other") is None

    def test_refused_when_the_file_row_has_another_identity(self, store):
        rec = _file(store)
        _file(store, size=200, mtime=2)  # the file was replaced while ffmpeg ran
        assert _fp(store, rec) is False
        assert store.get_fingerprint(rec.id, "intro") is None

    def test_empty_points_record_a_file_without_audio(self, store):
        rec = _file(store)
        assert _fp(store, rec, points=b"") is True
        assert store.get_fingerprint(rec.id, "intro").points == b""

    def test_identity_change_clears_fingerprint_pairs_and_detector_runs(self, store):
        a, b = _file(store, "/m/S01E01.mkv"), _file(store, "/m/S01E02.mkv")
        _fp(store, a), _fp(store, b)
        assert _pair(store, a, b, [(1.0, 2.0, 3.0, 4.0)]) is True
        store.set_detector_run(b.id, Source.SEASON_AUDIO, "sig")
        _file(store, "/m/S01E01.mkv", size=999, mtime=9)
        assert store.get_fingerprint(a.id, "intro") is None
        assert store.get_season_pair(a.id, b.id, 3) is None
        # b itself didn't change: its own fingerprint and run stay, the pair with the changed file doesn't.
        assert store.get_fingerprint(b.id, "intro") is not None
        assert store.get_detector_run(b.id, Source.SEASON_AUDIO) == "sig"
        _file(store, "/m/S01E02.mkv", size=5, mtime=5)
        assert store.get_detector_run(b.id, Source.SEASON_AUDIO) is None


class TestSeasonPairs:
    def test_round_trip_keeps_float_runs_exactly(self, store):
        a, b = _file(store, "/m/a.mkv"), _file(store, "/m/b.mkv")
        _fp(store, a), _fp(store, b)
        runs = [(0.12383900928792571, 24.891640866873065, 10.030959752321982, 34.79876160990712)]
        assert _pair(store, a, b, runs) is True
        assert store.get_season_pair(a.id, b.id, 3) == runs
        assert store.get_season_pair(b.id, a.id, 3) is None  # order matters: the matcher isn't symmetric
        assert store.get_season_pair(a.id, b.id, 4) is None  # another matcher version is recomputed

    def test_empty_run_list_is_a_stored_answer(self, store):
        a, b = _file(store, "/m/a.mkv"), _file(store, "/m/b.mkv")
        _fp(store, a), _fp(store, b)
        _pair(store, a, b, [])
        assert store.get_season_pair(a.id, b.id, 3) == []

    def test_refused_when_a_fingerprint_is_gone(self, store):
        a, b = _file(store, "/m/a.mkv"), _file(store, "/m/b.mkv")
        _fp(store, a)
        assert _pair(store, a, b, [(1.0, 2.0, 3.0, 4.0)]) is False
        assert store.get_season_pair(a.id, b.id, 3) is None

    def test_refused_when_a_file_was_replaced_after_it_was_matched(self, store):
        a, b = _file(store, "/m/a.mkv"), _file(store, "/m/b.mkv")
        _fp(store, a), _fp(store, b)
        b_new = _file(store, "/m/b.mkv", size=555, mtime=5)  # replaced while the season was matched
        _fp(store, b_new)  # and its own run already fingerprinted the new file
        assert _pair(store, a, b, [(1.0, 2.0, 3.0, 4.0)]) is False
        assert store.get_season_pair(a.id, b.id, 3) is None
        assert _pair(store, a, b_new, []) is True


class TestRecordMember:
    CHAPTERS = [Candidate(MarkerType.INTRO, 30_000, 40_000, Source.CHAPTERS, origin="Intro")]

    def _record(self, store, size, mtime, chapters=None):
        return store.record_member(
            FileIdentity("/m/S01E02.mkv", size, mtime),
            duration_ms=1_300_000,
            season_key="/m",
            chapters=self.CHAPTERS if chapters is None else chapters,
            chapter_version=7,
        )

    def test_a_file_never_seen_is_added_with_its_chapters(self, store):
        rec = self._record(store, 100, 1)
        assert (rec.canonical_path, rec.size, rec.mtime_ns, rec.duration_ms, rec.season_key, rec.is_movie) == (
            "/m/S01E02.mkv", 100, 1, 1_300_000, "/m", False)  # fmt: skip
        assert store.get_evidence(rec.id) == self.CHAPTERS
        assert store.evidence_version(rec.id, Source.CHAPTERS) == 7

    def test_a_file_with_the_probed_identity_is_refreshed_keeping_its_decisions(self, store):
        old = store.upsert_file(
            FileIdentity("/m/S01E02.mkv", 100, 1), duration_ms=None, season_key="/x", is_movie=False
        )
        store.set_intro_chapter_limit(old.id, 41_000)
        rec = self._record(store, 100, 1)
        assert (rec.id, rec.duration_ms, rec.season_key) == (old.id, 1_300_000, "/x")
        assert store.get_evidence(rec.id) == self.CHAPTERS
        assert store.get_intro_chapter_limit(rec.id) == (True, 41_000)

    def test_a_file_whose_row_has_another_identity_is_never_touched(self, store):
        newer = store.upsert_file(FileIdentity("/m/S01E02.mkv", 999, 9), duration_ms=5, season_key="/m", is_movie=False)
        store.replace_evidence(newer.id, Source.CHAPTERS, [], version=7)
        assert self._record(store, 100, 1) is None
        assert store.get_file("/m/S01E02.mkv") == newer
        assert store.get_evidence(newer.id) == []


class TestMemberProbeFailures:
    AT = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)

    def test_recording_a_failure_forgets_entries_that_stopped_counting_for_any_path(self, store):
        gone = FileIdentity("/m/gone/S01E02.mkv", 100, 1)  # deleted since: never probed or recorded again
        recent = FileIdentity("/m/S01E03.mkv", 100, 1)
        store.record_member_probe_failure(gone, self.AT, forget_before=self.AT - timedelta(days=1))
        store.record_member_probe_failure(recent, self.AT + timedelta(hours=12), forget_before=self.AT)

        later = self.AT + timedelta(days=1, hours=6)
        store.record_member_probe_failure(
            FileIdentity("/m/S01E04.mkv", 100, 1), later, forget_before=later - timedelta(days=1)
        )

        assert store.member_probe_failed_at(gone) is None
        assert store.member_probe_failed_at(recent) == self.AT + timedelta(hours=12)
        assert store._count("member_probe_failures") == 2


class TestIntroChapterLimits:
    def test_round_trip_none_and_identity_change(self, store):
        rec = _file(store)
        assert store.get_intro_chapter_limit(rec.id) == (False, None)
        store.set_intro_chapter_limit(rec.id, None)
        assert store.get_intro_chapter_limit(rec.id) == (True, None)
        store.set_intro_chapter_limit(rec.id, 44_000)
        assert store.get_intro_chapter_limit(rec.id) == (True, 44_000)
        _file(store, size=200, mtime=2)
        assert store.get_intro_chapter_limit(rec.id) == (False, None)


class TestDetectorRuns:
    def test_round_trip_and_replace(self, store):
        rec = _file(store)
        assert store.get_detector_run(rec.id, Source.SEASON_AUDIO) is None
        store.set_detector_run(rec.id, Source.SEASON_AUDIO, "one")
        store.set_detector_run(rec.id, Source.SEASON_AUDIO, "two")
        assert store.get_detector_run(rec.id, Source.SEASON_AUDIO) == "two"
        assert store.get_detector_run(rec.id, Source.CREDITS_TEXT) is None
