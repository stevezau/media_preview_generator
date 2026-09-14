"""markers.db audio tables: fingerprints gated on file identity, pair cache, detector runs, invalidation."""

from __future__ import annotations

import pytest

from media_preview_generator.markers.models import FileIdentity, Source
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
        assert store.set_season_pair(a.id, b.id, 3, [(1.0, 2.0, 3.0, 4.0)]) is True
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
        assert store.set_season_pair(a.id, b.id, 3, runs) is True
        assert store.get_season_pair(a.id, b.id, 3) == runs
        assert store.get_season_pair(b.id, a.id, 3) is None  # order matters: the matcher isn't symmetric
        assert store.get_season_pair(a.id, b.id, 4) is None  # another matcher version is recomputed

    def test_empty_run_list_is_a_stored_answer(self, store):
        a, b = _file(store, "/m/a.mkv"), _file(store, "/m/b.mkv")
        _fp(store, a), _fp(store, b)
        store.set_season_pair(a.id, b.id, 3, [])
        assert store.get_season_pair(a.id, b.id, 3) == []

    def test_refused_when_a_fingerprint_is_gone(self, store):
        a, b = _file(store, "/m/a.mkv"), _file(store, "/m/b.mkv")
        _fp(store, a)
        assert store.set_season_pair(a.id, b.id, 3, [(1.0, 2.0, 3.0, 4.0)]) is False
        assert store.get_season_pair(a.id, b.id, 3) is None


class TestDetectorRuns:
    def test_round_trip_and_replace(self, store):
        rec = _file(store)
        assert store.get_detector_run(rec.id, Source.SEASON_AUDIO) is None
        store.set_detector_run(rec.id, Source.SEASON_AUDIO, "one")
        store.set_detector_run(rec.id, Source.SEASON_AUDIO, "two")
        assert store.get_detector_run(rec.id, Source.SEASON_AUDIO) == "two"
        assert store.get_detector_run(rec.id, Source.CREDITS_TEXT) is None
