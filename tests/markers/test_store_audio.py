"""markers.db audio tables: fingerprints gated on file identity, pair cache, detector runs, invalidation."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from media_preview_generator.markers.audio import fingerprint
from media_preview_generator.markers.decide import DecisionStatus, TypeDecision
from media_preview_generator.markers.models import Candidate, FileIdentity, Marker, MarkerType, Source
from media_preview_generator.markers.store import (
    SEASON_PAIR_WINDOW,
    CachedShare,
    EndPictureKey,
    MarkerStore,
    StoredFingerprint,
)


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

    @pytest.mark.parametrize(
        ("wanted", "found"),
        [
            ({}, True),
            ({"algorithm": 1, "length_s": 455.0004}, True),
            ({"algorithm": 2}, False),
            ({"length_s": 456.0}, False),
        ],
        ids=["any", "same-to-the-millisecond", "other-algorithm", "other-window"],
    )
    def test_a_fingerprint_made_another_way_is_not_found(self, store, wanted, found):
        rec = _file(store)
        _fp(store, rec)
        assert (store.get_fingerprint(rec.id, "intro", **wanted) is not None) is found
        assert store.has_fingerprint(rec.id, "intro", **wanted) is found

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

    def test_pairs_are_kept_for_the_window_the_season_step_fingerprints(self, store):
        # The store can't import fingerprint.WINDOW (fingerprint imports the store): if the two ever drift, every pair
        # would be refused and each season run would match all its pairs again.
        assert fingerprint.WINDOW == SEASON_PAIR_WINDOW
        a, b = _file(store, "/m/a.mkv"), _file(store, "/m/b.mkv")
        _fp(store, a, window=fingerprint.WINDOW), _fp(store, b, window=fingerprint.WINDOW)
        assert _pair(store, a, b, [(1.0, 2.0, 3.0, 4.0)]) is True

    def test_refused_when_only_another_windows_fingerprint_exists(self, store):
        a, b = _file(store, "/m/a.mkv"), _file(store, "/m/b.mkv")
        _fp(store, a), _fp(store, b, window="credits")
        assert _pair(store, a, b, [(1.0, 2.0, 3.0, 4.0)]) is False


def _share(store, a, b, share, *, start_ms=0, end_ms=30_000, offset_ms=500, version=1, identity_b=None):
    key = EndPictureKey(a.id, b.id, start_ms, end_ms, offset_ms)
    return store.set_end_picture(
        key, version, share, identity_a=(a.size, a.mtime_ns), identity_b=identity_b or (b.size, b.mtime_ns)
    )


class TestEndPictures:
    """Season audio's end-picture shares, cached per file pair and stretch."""

    def test_round_trip_per_pair_stretch_offset_and_version(self, store):
        a, b = _file(store, "/m/a.mkv"), _file(store, "/m/b.mkv")
        assert _share(store, a, b, 0.8333333333333334) is True
        key = EndPictureKey(a.id, b.id, 0, 30_000, 500)
        assert store.get_end_picture(key, 1) == CachedShare(0.8333333333333334)
        assert store.get_end_picture(key, 2) is None  # another way of comparing is measured again
        for other in (key._replace(file_a=b.id, file_b=a.id), key._replace(end_ms=30_001), key._replace(offset_ms=0)):
            assert store.get_end_picture(other, 1) is None

    def test_no_frames_to_compare_is_a_stored_answer(self, store):
        a, b = _file(store, "/m/a.mkv"), _file(store, "/m/b.mkv")
        _share(store, a, b, None)
        assert store.get_end_picture(EndPictureKey(a.id, b.id, 0, 30_000, 500), 1) == CachedShare(None)

    def test_refused_when_a_file_was_replaced_while_it_was_decoded(self, store):
        a, b = _file(store, "/m/a.mkv"), _file(store, "/m/b.mkv")
        assert _share(store, a, b, 1.0, identity_b=(b.size, b.mtime_ns + 1)) is False
        assert store.get_end_picture(EndPictureKey(a.id, b.id, 0, 30_000, 500), 1) is None

    @pytest.mark.parametrize("changed", ["a", "b"])
    def test_a_changed_identity_clears_the_pairs_of_that_file_only(self, store, changed):
        a, b, c = _file(store, "/m/a.mkv"), _file(store, "/m/b.mkv"), _file(store, "/m/c.mkv")
        _share(store, a, b, 1.0), _share(store, b, c, 0.5), _share(store, a, c, 0.0)
        _file(store, f"/m/{changed}.mkv", size=999, mtime=9)
        cached = {
            (x, y): store.get_end_picture(EndPictureKey(ids[0].id, ids[1].id, 0, 30_000, 500), 1)
            for (x, y), ids in {("a", "b"): (a, b), ("b", "c"): (b, c), ("a", "c"): (a, c)}.items()
        }
        assert {pair for pair, share in cached.items() if share is not None} == {
            pair for pair in cached if changed not in pair
        }

    def test_the_fingerprint_sweep_drops_a_gone_files_pairs(self, store):
        a, b = _file(store, "/m/a.mkv"), _file(store, "/m/b.mkv")
        _fp(store, a), _fp(store, b)
        _share(store, a, b, 1.0), _share(store, b, a, 1.0)
        (check_a, _check_b) = store.fingerprint_checks(10)
        store.finish_fingerprint_checks(check_a.file_id, [check_a])
        assert store.get_end_picture(EndPictureKey(a.id, b.id, 0, 30_000, 500), 1) is None
        assert store.get_end_picture(EndPictureKey(b.id, a.id, 0, 30_000, 500), 1) is None

    def test_a_replaced_fingerprint_keeps_the_shares(self, store):
        # A share compares pictures at given times; a fingerprint made another way changes neither.
        a, b = _file(store, "/m/a.mkv"), _file(store, "/m/b.mkv")
        _fp(store, a), _fp(store, b)
        _share(store, a, b, 1.0)
        _fp(store, a, length_s=456.0)
        assert store.get_end_picture(EndPictureKey(a.id, b.id, 0, 30_000, 500), 1) == CachedShare(1.0)


class TestEndPictureFailures:
    """Files the end-picture check couldn't read, remembered with their identity for a day."""

    NOW = datetime(2026, 9, 24, tzinfo=UTC)

    def test_a_failure_counts_only_for_the_identity_it_was_recorded_with(self, store):
        store.record_end_picture_failure(FileIdentity("/m/a.mkv", 1, 1), self.NOW, forget_before=self.NOW)
        assert store.end_picture_failed_at(FileIdentity("/m/a.mkv", 1, 1)) == self.NOW
        assert store.end_picture_failed_at(FileIdentity("/m/a.mkv", 2, 1)) is None
        assert store.end_picture_failed_at(FileIdentity("/m/b.mkv", 1, 1)) is None

    def test_recording_one_forgets_entries_that_stopped_counting(self, store):
        store.record_end_picture_failure(FileIdentity("/m/a.mkv", 1, 1), self.NOW, forget_before=self.NOW)
        later = self.NOW + timedelta(days=2)
        store.record_end_picture_failure(FileIdentity("/m/b.mkv", 1, 1), later, forget_before=later - timedelta(days=1))
        assert store.end_picture_failed_at(FileIdentity("/m/a.mkv", 1, 1)) is None
        assert store.end_picture_failed_at(FileIdentity("/m/b.mkv", 1, 1)) == later

    def test_the_fingerprint_sweep_forgets_a_gone_files_failure(self, store):
        a = _file(store, "/m/a.mkv")
        _fp(store, a)
        store.record_end_picture_failure(FileIdentity(a.canonical_path, a.size, a.mtime_ns), self.NOW,
                                         forget_before=self.NOW)  # fmt: skip
        (check,) = store.fingerprint_checks(10)
        store.finish_fingerprint_checks(check.file_id, [check])
        assert store.end_picture_failed_at(FileIdentity(a.canonical_path, a.size, a.mtime_ns)) is None


class TestFilesDecidedByOnlineAndServerMarkers:
    """The files the decide-again job lists after settings v18: an unlocked intro or credits decided by an IntroDB or
    TheIntroDB answer with a server's own marker and no source that reads the file (such a pair can be a marker made
    for an earlier file and online times from a release at the other speed)."""

    @pytest.mark.parametrize(
        ("decided_by", "mtype", "locked", "listed"),
        [
            (("introdb", "server_markers"), MarkerType.INTRO, False, True),
            (("theintrodb", "server_markers"), MarkerType.CREDITS, False, True),
            (("introdb", "theintrodb", "server_markers"), MarkerType.INTRO, False, True),
            (("introdb", "server_markers"), MarkerType.RECAP, False, False),
            (("introdb", "server_markers"), MarkerType.INTRO, True, False),
            (("introdb",), MarkerType.INTRO, False, False),
            (("skipdb", "server_markers"), MarkerType.INTRO, False, False),
            (("introdb", "server_markers_imported"), MarkerType.INTRO, False, False),
            (("chapters", "introdb", "server_markers"), MarkerType.INTRO, False, False),
            (("introdb", "season_audio", "server_markers"), MarkerType.INTRO, False, False),
            (("introdb", "credits_text", "server_markers"), MarkerType.CREDITS, False, False),
        ],
        ids=[
            "introdb-intro",
            "theintrodb-credits",
            "both-databases",
            "recap",
            "locked",
            "no-server-marker",
            "skipdb",
            "importer-copy",
            "with-chapters",
            "with-season-audio",
            "with-credit-text",
        ],
    )
    def test_listed_only_for_an_unlocked_intro_or_credits_resting_on_online_and_server_markers(
        self, store, decided_by, mtype, locked, listed
    ):
        rec = _file(store, "/m/S01E01.mkv")
        marker = Marker(mtype, 10_000, 40_000, decided_by)
        if locked:
            store.lock_marker(rec.id, marker)
        else:
            decision = TypeDecision(mtype, DecisionStatus.DECIDED, marker, None, "x")
            store.save_decisions(rec.id, {mtype: decision}, settings_fingerprint="f")
        assert store.files_decided_by_online_and_server_markers() == (["/m/S01E01.mkv"] if listed else [])

    def test_a_file_missing_from_disk_is_not_listed_and_each_file_is_listed_once(self, store):
        both, gone = _file(store, "/m/S01E01.mkv"), _file(store, "/m/S01E02.mkv")
        for rec in (both, gone):
            decisions = {
                mtype: TypeDecision(
                    mtype,
                    DecisionStatus.DECIDED,
                    Marker(mtype, s, s + 30_000, ("introdb", "server_markers")),
                    None,
                    "x",
                )
                for mtype, s in ((MarkerType.INTRO, 10_000), (MarkerType.CREDITS, 1_200_000))
            }
            store.save_decisions(rec.id, decisions, settings_fingerprint="f")
        store.mark_missing(gone)
        assert store.files_decided_by_online_and_server_markers() == ["/m/S01E01.mkv"]


class TestFilesWithSeasonAudioIntro:
    """The files the decide-again job lists after settings v17: an unlocked intro decided with season audio."""

    @pytest.mark.parametrize(
        ("decided_by", "locked", "listed"),
        [
            (("season_audio",), False, True),
            (("introdb", "season_audio"), False, True),
            (("season_audio_previous", "theintrodb"), False, True),
            (("chapters",), False, False),
            (("season_audio",), True, False),
        ],
        ids=["alone", "with-an-online-source", "previous-season-hint", "other-sources", "locked"],
    )
    def test_listed_only_for_an_unlocked_intro_resting_on_season_audio(self, store, decided_by, locked, listed):
        rec = _file(store, "/m/S01E01.mkv")
        marker = Marker(MarkerType.INTRO, 10_000, 40_000, decided_by)
        if locked:
            store.lock_marker(rec.id, marker)
        else:
            decision = TypeDecision(MarkerType.INTRO, DecisionStatus.DECIDED, marker, None, "x")
            store.save_decisions(rec.id, {MarkerType.INTRO: decision}, settings_fingerprint="f")
        assert store.files_with_season_audio_intro() == (["/m/S01E01.mkv"] if listed else [])

    def test_credits_decided_with_it_and_files_missing_from_disk_are_not_listed(self, store):
        credits_rec, gone = _file(store, "/m/S01E01.mkv"), _file(store, "/m/S01E02.mkv")
        for rec, mtype in ((credits_rec, MarkerType.CREDITS), (gone, MarkerType.INTRO)):
            marker = Marker(mtype, 10_000, 40_000, ("season_audio",))
            decision = TypeDecision(mtype, DecisionStatus.DECIDED, marker, None, "x")
            store.save_decisions(rec.id, {mtype: decision}, settings_fingerprint="f")
        store.mark_missing(gone)
        assert store.files_with_season_audio_intro() == []


class TestRecordMember:
    CHAPTERS = [Candidate(MarkerType.INTRO, 30_000, 40_000, Source.CHAPTERS, origin="Intro")]

    def _record(self, store, size, mtime, chapters=None, frame_rate=25.0):
        return store.record_member(
            FileIdentity("/m/S01E02.mkv", size, mtime),
            duration_ms=1_300_000,
            season_key="/m",
            chapters=self.CHAPTERS if chapters is None else chapters,
            chapter_version=7,
            frame_rate=frame_rate,
        )

    def test_a_file_never_seen_is_added_with_its_chapters(self, store):
        rec = self._record(store, 100, 1)
        assert (rec.canonical_path, rec.size, rec.mtime_ns, rec.duration_ms, rec.season_key, rec.is_movie) == (
            "/m/S01E02.mkv", 100, 1, 1_300_000, "/m", False)  # fmt: skip
        assert store.get_evidence(rec.id) == self.CHAPTERS
        assert store.evidence_version(rec.id, Source.CHAPTERS) == 7
        assert store.get_frame_rate(rec.id) == (True, 25.0)

    def test_a_member_without_a_video_frame_rate_is_stored_as_probed(self, store):
        rec = self._record(store, 100, 1, frame_rate=None)
        assert store.get_frame_rate(rec.id) == (True, None)

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
        assert store.get_frame_rate(newer.id) == (False, None)


class TestMemberProbeFailures:
    AT = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)

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


class TestMemberFingerprintFailures:
    AT = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)

    def test_a_failure_counts_only_for_the_identity_it_was_recorded_with(self, store):
        store.record_member_fingerprint_failure(FileIdentity("/m/S01E02.mkv", 100, 1), self.AT, forget_before=self.AT)
        assert store.member_fingerprint_failed_at(FileIdentity("/m/S01E02.mkv", 100, 1)) == self.AT
        assert store.member_fingerprint_failed_at(FileIdentity("/m/S01E02.mkv", 200, 2)) is None
        assert store.member_probe_failed_at(FileIdentity("/m/S01E02.mkv", 100, 1)) is None

    def test_recording_a_failure_forgets_entries_that_stopped_counting_for_any_path(self, store):
        gone = FileIdentity("/m/gone/S01E02.mkv", 100, 1)
        recent = FileIdentity("/m/S01E03.mkv", 100, 1)
        store.record_member_fingerprint_failure(gone, self.AT, forget_before=self.AT - timedelta(days=1))
        store.record_member_fingerprint_failure(recent, self.AT + timedelta(hours=12), forget_before=self.AT)
        later = self.AT + timedelta(days=1, hours=6)
        store.record_member_fingerprint_failure(
            FileIdentity("/m/S01E04.mkv", 100, 1), later, forget_before=later - timedelta(days=1)
        )
        assert store.member_fingerprint_failed_at(gone) is None
        assert store.member_fingerprint_failed_at(recent) == self.AT + timedelta(hours=12)
        assert store._count("member_fingerprint_failures") == 2


class TestFingerprintChecks:
    def test_checks_go_on_after_the_last_file_checked_and_wrap_round(self, store):
        recs = [_file(store, f"/m/S01E{n:02d}.mkv") for n in range(1, 6)]
        for rec in recs:
            _fp(store, rec)
        _file(store, "/m/movie.mkv")  # no fingerprint: never listed
        paths = [r.canonical_path for r in recs]

        def listed(limit):
            return [check.canonical_path for check in store.fingerprint_checks(limit)]

        assert listed(2) == paths[0:2]
        assert listed(2) == paths[0:2]  # listing alone doesn't move on
        store.finish_fingerprint_checks(recs[0].id, [])  # only the first was checked
        assert listed(2) == paths[1:3]
        store.finish_fingerprint_checks(recs[3].id, [])
        assert listed(3) == [paths[4], paths[0], paths[1]]
        assert listed(10) == [paths[4], *paths[:4]]

    def test_finishing_drops_the_gone_files_cache_and_keeps_their_rows_and_other_files_cache(self, store):
        a, b = _file(store, "/m/S01E01.mkv"), _file(store, "/m/S01E02.mkv")
        _fp(store, a), _fp(store, b)
        _pair(store, a, b, [(1.0, 2.0, 3.0, 4.0)])
        (check_a, check_b) = store.fingerprint_checks(5)
        assert (check_a.file_id, check_a.size, check_a.mtime_ns) == (a.id, a.size, a.mtime_ns)
        assert store.finish_fingerprint_checks(check_b.file_id, [check_a]) == 1
        assert store.get_fingerprint(a.id, "intro") is None and store.get_season_pair(a.id, b.id, 3) is None
        assert store.get_file(a.canonical_path) == a and store.get_fingerprint(b.id, "intro") is not None
        assert [check.canonical_path for check in store.fingerprint_checks(5)] == [b.canonical_path]

    def test_a_file_that_came_back_with_another_identity_after_its_check_keeps_its_new_fingerprint(self, store):
        a = _file(store, "/m/S01E01.mkv")
        _fp(store, a)
        now = datetime(2026, 9, 13, tzinfo=UTC)
        (check,) = store.fingerprint_checks(5)  # the sweep finds it gone...
        back = _file(store, "/m/S01E01.mkv", size=200, mtime=2)  # ...then a new file lands there and is fingerprinted
        _fp(store, back)
        store.record_member_fingerprint_failure(FileIdentity(back.canonical_path, 200, 2), now, forget_before=now)
        assert store.finish_fingerprint_checks(check.file_id, [check]) == 0
        assert store.get_fingerprint(back.id, "intro") is not None
        assert store.member_fingerprint_failed_at(FileIdentity(back.canonical_path, 200, 2)) == now
        assert [c.size for c in store.fingerprint_checks(5)] == [200]  # the cursor moved on anyway


class TestDetectorAnswer:
    AUDIO = [Candidate(MarkerType.INTRO, 10_000, 40_000, Source.SEASON_AUDIO, 1.0, "2/2")]

    def test_the_answer_of_every_source_and_its_basis_are_stored_together(self, store):
        rec = _file(store)
        answers = {Source.SEASON_AUDIO: self.AUDIO, Source.SEASON_AUDIO_PREVIOUS: []}
        store.replace_detector_answer(rec.id, answers, version=4, run=(Source.SEASON_AUDIO, "sig"))
        assert store.get_evidence(rec.id) == self.AUDIO
        assert {r.source for r in store.evidence_rows(rec.id)} == {Source.SEASON_AUDIO, Source.SEASON_AUDIO_PREVIOUS}
        assert store.evidence_version(rec.id, Source.SEASON_AUDIO_PREVIOUS) == 4
        assert store.get_detector_run(rec.id, Source.SEASON_AUDIO) == "sig"

    def test_a_failure_part_way_keeps_the_old_answer_and_basis(self, store):
        rec = _file(store)
        store.replace_detector_answer(rec.id, {Source.SEASON_AUDIO: []}, version=3, run=(Source.SEASON_AUDIO, "old"))
        broken = [Candidate(MarkerType.INTRO, 1, 2, Source.SEASON_AUDIO_PREVIOUS, 1.0, "4/4")]
        with pytest.raises(AttributeError):  # the second source's rows can't be written
            store.replace_detector_answer(
                rec.id,
                {Source.SEASON_AUDIO: self.AUDIO, Source.SEASON_AUDIO_PREVIOUS: [*broken, None]},
                version=4,
                run=(Source.SEASON_AUDIO, "new"),
            )
        assert store.get_evidence(rec.id) == []
        assert store.evidence_version(rec.id, Source.SEASON_AUDIO) == 3
        assert store.get_detector_run(rec.id, Source.SEASON_AUDIO) == "old"


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


class TestFrameRates:
    def test_round_trip_of_a_rate_and_of_no_rate(self, store):
        rec = _file(store)
        assert store.get_frame_rate(rec.id) == (False, None)
        assert store.set_frame_rate(rec.id, 24000 / 1001, identity=(rec.size, rec.mtime_ns)) is True
        assert store.get_frame_rate(rec.id) == (True, 24000 / 1001)
        assert store.set_frame_rate(rec.id, None, identity=(rec.size, rec.mtime_ns)) is True
        assert store.get_frame_rate(rec.id) == (True, None)

    def test_refused_when_the_file_was_replaced_while_it_was_probed(self, store):
        rec = _file(store)
        _file(store, size=200, mtime=2)
        assert store.set_frame_rate(rec.id, 25.0, identity=(rec.size, rec.mtime_ns)) is False
        assert store.get_frame_rate(rec.id) == (False, None)

    def test_a_changed_identity_forgets_the_rate(self, store):
        rec = _file(store)
        store.set_frame_rate(rec.id, 25.0, identity=(rec.size, rec.mtime_ns))
        _file(store)  # the same identity again keeps it
        assert store.get_frame_rate(rec.id) == (True, 25.0)
        _file(store, size=200, mtime=2)
        assert store.get_frame_rate(rec.id) == (False, None)

    def test_a_rate_read_for_another_identity_than_the_rows_is_unread(self, store, tmp_path):
        rec = _file(store)
        store.set_frame_rate(rec.id, 25.0, identity=(rec.size, rec.mtime_ns))
        with sqlite3.connect(tmp_path / "markers.db") as other_writer:  # a row changed without clearing the rate
            other_writer.execute("UPDATE files SET size=999 WHERE id=?", (rec.id,))
        assert store.get_frame_rate(rec.id) == (False, None)

    @staticmethod
    def _paired(store):
        a, b = _file(store), _file(store, name="/m/S01E02.mkv")
        _fp(store, a), _fp(store, b)
        assert _pair(store, a, b, [(1.0, 20.0, 3.0, 22.0)], version=6)
        return a, b

    @pytest.mark.parametrize(
        ("first", "second", "dropped"),
        [(None, 25.0, True), (25.0, 24000 / 1001, True), (25.0, None, True), (25.0, 25.0, False)],
        ids=["unread-to-25", "25-to-film", "25-to-none", "same-rate"],
    )
    def test_a_changed_rate_drops_the_files_matched_pairs(self, store, first, second, dropped):
        # Its pairs may have been matched at another speed (spec §5.3 "Two playback speeds").
        a, b = self._paired(store)
        if first is not None:
            store.set_frame_rate(b.id, first, identity=(b.size, b.mtime_ns))
            _pair(store, a, b, [(1.0, 20.0, 3.0, 22.0)], version=6)
        store.set_frame_rate(b.id, second, identity=(b.size, b.mtime_ns))
        assert (store.get_season_pair(a.id, b.id, 6) is None) is dropped

    def test_a_member_recorded_with_a_new_rate_drops_its_matched_pairs(self, store):
        a, b = self._paired(store)
        store.record_member(FileIdentity(b.canonical_path, b.size, b.mtime_ns), duration_ms=1_300_000,
                            season_key="/m", chapters=[], chapter_version=7, frame_rate=25.0)  # fmt: skip
        assert store.get_season_pair(a.id, b.id, 6) is None
        _pair(store, a, b, [(1.0, 20.0, 3.0, 22.0)], version=6)
        store.record_member(FileIdentity(b.canonical_path, b.size, b.mtime_ns), duration_ms=1_300_000,
                            season_key="/m", chapters=[], chapter_version=7, frame_rate=25.0)  # fmt: skip
        assert store.get_season_pair(a.id, b.id, 6) is not None  # the same rate again


class TestDetectorRuns:
    def test_round_trip_and_replace(self, store):
        rec = _file(store)
        assert store.get_detector_run(rec.id, Source.SEASON_AUDIO) is None
        store.set_detector_run(rec.id, Source.SEASON_AUDIO, "one")
        store.set_detector_run(rec.id, Source.SEASON_AUDIO, "two")
        assert store.get_detector_run(rec.id, Source.SEASON_AUDIO) == "two"
        assert store.get_detector_run(rec.id, Source.CREDITS_TEXT) is None
