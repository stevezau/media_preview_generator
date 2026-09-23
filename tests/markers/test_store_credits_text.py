"""Credit text decode timeouts in the store: kept per file identity, forgotten once old (Task 8, preflight I1)."""

from datetime import UTC, datetime, timedelta

import pytest

from media_preview_generator.markers.models import Candidate, FileIdentity, MarkerType, Source
from media_preview_generator.markers.store import MarkerStore

AT = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    s = MarkerStore(str(tmp_path / "markers.db"))
    yield s
    s.close()


def test_a_timeout_counts_only_for_the_identity_it_was_recorded_with(store):
    store.record_credits_text_timeout(FileIdentity("/m/Movie.mkv", 100, 1), AT, forget_before=AT)
    assert store.credits_text_timed_out_at(FileIdentity("/m/Movie.mkv", 100, 1)) == AT
    assert store.credits_text_timed_out_at(FileIdentity("/m/Movie.mkv", 200, 1)) is None
    assert store.credits_text_timed_out_at(FileIdentity("/m/Movie.mkv", 100, 2)) is None
    assert store.member_fingerprint_failed_at(FileIdentity("/m/Movie.mkv", 100, 1)) is None


def test_a_newer_timeout_replaces_the_paths_entry(store):
    store.record_credits_text_timeout(FileIdentity("/m/Movie.mkv", 100, 1), AT, forget_before=AT - timedelta(days=1))
    later = AT + timedelta(hours=2)
    store.record_credits_text_timeout(
        FileIdentity("/m/Movie.mkv", 300, 3), later, forget_before=later - timedelta(days=1)
    )
    assert store.credits_text_timed_out_at(FileIdentity("/m/Movie.mkv", 100, 1)) is None
    assert store.credits_text_timed_out_at(FileIdentity("/m/Movie.mkv", 300, 3)) == later
    assert store._count("credits_text_timeouts") == 1


@pytest.mark.parametrize(
    ("answers", "forgotten"),
    [
        ({Source.CREDITS_TEXT: [Candidate(MarkerType.CREDITS, 5_000_000, None, Source.CREDITS_TEXT)]}, True),
        ({Source.CREDITS_TEXT: []}, True),  # "looked, no credit roll" is an answer too: the decode finished
        ({Source.SEASON_AUDIO: []}, False),  # another detector's answer says nothing about this decode
    ],
    ids=["credits-found", "no-credits", "other-detector"],
)
def test_a_stored_credit_text_answer_forgets_the_files_timeout(store, answers, forgotten):
    # Audit phase 3 LOW-2: a file that timed out and then decoded fine (a forced run) must not be held back for the
    # rest of the day when it is asked again (a detector version bump).
    identity = FileIdentity("/m/Movie.mkv", 100, 1)
    other = FileIdentity("/m/Other.mkv", 100, 1)
    rec = store.upsert_file(identity, duration_ms=6_000_000, season_key=None, is_movie=True)
    store.record_credits_text_timeout(identity, AT, forget_before=AT - timedelta(days=1))
    store.record_credits_text_timeout(other, AT, forget_before=AT - timedelta(days=1))
    store.replace_detector_answer(rec.id, answers, version=1)
    assert (store.credits_text_timed_out_at(identity) is None) is forgotten
    assert store.credits_text_timed_out_at(other) == AT


def test_recording_a_timeout_forgets_entries_that_stopped_counting_for_any_path(store):
    gone = FileIdentity("/m/gone/Movie.mkv", 100, 1)
    recent = FileIdentity("/m/Other.mkv", 100, 1)
    store.record_credits_text_timeout(gone, AT, forget_before=AT - timedelta(days=1))
    store.record_credits_text_timeout(recent, AT + timedelta(hours=12), forget_before=AT)
    later = AT + timedelta(days=1, hours=6)
    store.record_credits_text_timeout(
        FileIdentity("/m/Third.mkv", 100, 1), later, forget_before=later - timedelta(days=1)
    )
    assert store.credits_text_timed_out_at(gone) is None
    assert store.credits_text_timed_out_at(recent) == AT + timedelta(hours=12)
    assert store._count("credits_text_timeouts") == 2
