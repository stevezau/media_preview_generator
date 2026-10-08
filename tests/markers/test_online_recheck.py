"""File selection for online re-check jobs saved by earlier app versions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from media_preview_generator.markers import pipeline
from media_preview_generator.markers.decide import DecisionStatus, TypeDecision
from media_preview_generator.markers.models import Candidate, FileIdentity, Marker, MarkerType, Source
from media_preview_generator.markers.settings import load_global, validate_global
from media_preview_generator.markers.store import MarkerStore

NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)
ALL_ONLINE = ("theintrodb", "introdb", "skipdb")


def _settings(*online: str):
    """Global settings with only these online sources on (chapters and season audio always on)."""
    sources = [{"id": "chapters", "enabled": True}, {"id": "season_audio", "enabled": True}]
    sources += [{"id": source, "enabled": source in online} for source in ALL_ONLINE]
    return load_global(validate_global({"sources": sources}, None)[0])


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock():
    return _Clock(NOW)


@pytest.fixture
def store(tmp_path, clock):
    store = MarkerStore(str(tmp_path / "markers.db"), clock=clock)
    yield store
    store.close()


def _add(
    store,
    clock,
    tmp_path,
    name,
    *,
    lookups=((Source.THEINTRODB, False, 15),),
    decided_by=None,
    status=DecisionStatus.NO_EVIDENCE,
    locked=False,
    on_disk=True,
):
    """A file with its online lookups (source, found, days old) and one intro decision.

    ``decided_by`` is the decided intro's sources (for DECIDED); None stores no decision at all when ``status`` is None.
    """
    path = tmp_path / "media" / f"{name}.mkv"
    path.parent.mkdir(parents=True, exist_ok=True)
    if on_disk:
        path.write_bytes(b"x")
    rec = store.upsert_file(FileIdentity(str(path), 1, 1), duration_ms=1_000_000, season_key=None, is_movie=True)
    for source, found, days in lookups:
        clock.now = NOW - timedelta(days=days)
        candidates = [Candidate(MarkerType.INTRO, 10_000, 40_000, source)] if found else []
        store.replace_evidence(rec.id, source, candidates, version=pipeline.PARSER_VERSIONS[source])
    clock.now = NOW
    if status is not None:
        marker = Marker(MarkerType.INTRO, 10_000, 40_000, tuple(decided_by or ("chapters",)))
        shown = (
            {"marker": marker, "proposed": None}
            if status is DecisionStatus.DECIDED
            else {"marker": None, "proposed": marker}
        )
        decision = TypeDecision(MarkerType.INTRO, status, reason="x", **shown)
        store.save_decisions(rec.id, {MarkerType.INTRO: decision}, settings_fingerprint="f")
        if locked:
            store.lock_marker(rec.id, marker)
    return str(path)


class TestFilesWithOldEmptyLookups:
    """The store's half: a stored "no entry" of one of the given sources, older than the cutoff."""

    @pytest.mark.parametrize(
        ("lookups", "listed"),
        [
            (((Source.THEINTRODB, False, 15),), True),
            (((Source.THEINTRODB, False, 13),), False),  # not due yet
            (((Source.THEINTRODB, True, 30),), False),  # an entry is never asked again for being old
            (((Source.INTRODB, False, 15),), False),  # a source not asked about
            (((Source.INTRODB, False, 15), (Source.THEINTRODB, False, 1)), False),
            (((Source.INTRODB, True, 15), (Source.THEINTRODB, False, 20)), True),  # per source
        ],
        ids=["old-no-entry", "fresh-no-entry", "old-entry", "other-source", "only-other-source-old", "per-source"],
    )
    def test_only_an_old_no_entry_of_a_given_source_is_listed(self, store, clock, tmp_path, lookups, listed):
        path = _add(store, clock, tmp_path, "a", lookups=lookups)
        assert store.files_with_old_empty_lookups([Source.THEINTRODB], NOW - timedelta(days=14)) == (
            [path] if listed else []
        )

    def test_no_source_lists_nothing(self, store, clock, tmp_path):
        _add(store, clock, tmp_path, "a")
        assert store.files_with_old_empty_lookups([], NOW) == []


class TestOnlineRecheckFiles:
    """Which files the weekly job lists: a due "no entry" of an enabled online source, on a file whose decision an
    online answer could still change."""

    @pytest.mark.parametrize(
        ("status", "decided_by", "locked", "listed"),
        [
            (DecisionStatus.NO_EVIDENCE, None, False, True),
            (None, None, False, True),  # never decided: treated as undecided, as the TheIntroDB recheck does
            (DecisionStatus.DECIDED, ("season_audio",), False, True),
            (DecisionStatus.DECIDED, ("season_audio", "server_markers"), False, True),
            (DecisionStatus.DECIDED, ("season_audio_previous", "season_audio"), False, True),
            (DecisionStatus.DECIDED, ("chapters",), False, False),
            (DecisionStatus.DECIDED, ("chapters", "server_markers"), False, False),
            (DecisionStatus.DECIDED, ("chapters", "theintrodb"), False, False),
            (DecisionStatus.DECIDED, ("season_audio", "credits_text"), False, False),
            (DecisionStatus.DECIDED, ("season_audio",), True, False),  # the user's lock: detection never changes it
        ],
        ids=[
            "nothing-found",
            "never-decided",
            "season-audio-alone",
            "season-audio-and-server",
            "season-audio-and-previous-season",
            "chapters-alone",
            "chapters-and-server",
            "chapters-and-online",
            "season-audio-and-another-source",
            "locked",
        ],
    )
    def test_a_due_file_is_listed_when_an_online_answer_could_change_its_decision(
        self, store, clock, tmp_path, status, decided_by, locked, listed
    ):
        path = _add(store, clock, tmp_path, "a", status=status, decided_by=decided_by, locked=locked)
        assert list(pipeline.online_recheck_files(store, _settings(*ALL_ONLINE), NOW)) == ([path] if listed else [])

    @pytest.mark.parametrize(
        ("lookups", "listed"),
        [
            (((Source.THEINTRODB, False, 15),), True),
            (((Source.THEINTRODB, False, 14),), False),  # _needs_lookup asks again only once it is OLDER than 14 days
            (((Source.THEINTRODB, False, 13),), False),
            (((Source.THEINTRODB, True, 40),), False),
            ((), False),  # never asked: any run asks it, nothing to re-check
        ],
        ids=["due", "exactly-14-days", "not-due", "has-an-entry", "never-asked"],
    )
    def test_only_a_no_entry_older_than_the_retry_is_due(self, store, clock, tmp_path, lookups, listed):
        path = _add(store, clock, tmp_path, "a", lookups=lookups)
        assert list(pipeline.online_recheck_files(store, _settings(*ALL_ONLINE), NOW)) == ([path] if listed else [])

    @pytest.mark.parametrize(
        ("online", "listed"),
        [(("introdb",), True), (("theintrodb", "skipdb"), False), ((), False)],
        ids=["its-source-on", "its-source-off", "every-online-source-off"],
    )
    def test_only_an_enabled_sources_no_entry_counts(self, store, clock, tmp_path, online, listed):
        path = _add(store, clock, tmp_path, "a", lookups=((Source.INTRODB, False, 20), (Source.THEINTRODB, False, 2)))
        assert list(pipeline.online_recheck_files(store, _settings(*online), NOW)) == ([path] if listed else [])

    def test_a_file_gone_from_disk_is_not_listed(self, store, clock, tmp_path):
        kept = _add(store, clock, tmp_path, "kept")
        _add(store, clock, tmp_path, "gone", on_disk=False)
        assert list(pipeline.online_recheck_files(store, _settings(*ALL_ONLINE), NOW)) == [kept]
