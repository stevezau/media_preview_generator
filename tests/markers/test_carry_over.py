"""Carry-over (spec §5.5 rule 15): a replaced file's decision kept for the file that replaced it, at the same length,
for a type the new file has no evidence of."""

from __future__ import annotations

import os
import time
from unittest.mock import patch

import pytest

from media_preview_generator.markers import carry_over as co
from media_preview_generator.markers import job_log, missing, pipeline
from media_preview_generator.markers.audio import season
from media_preview_generator.markers.decide import NO_EVIDENCE_REASON, DecisionStatus, TypeDecision
from media_preview_generator.markers.models import Candidate, FileIdentity, Marker, MarkerType, Source
from media_preview_generator.markers.outcomes import FileOutcome
from media_preview_generator.markers.probe import Chapter, MediaProbe
from media_preview_generator.markers.sources.online import LookupResult
from media_preview_generator.markers.store import MarkerStore, PreviousDecision
from media_preview_generator.servers.base import ServerType
from tests.markers.fakes import ready_publisher
from tests.markers.test_pipeline import _clients, _ctx, _registry, _run

T = MarkerType
DUR = 1_417_088  # Tomb Raider King S01E12: the replaced file and the new one are the same length
INTRO = (0, 92_000)
CREDITS = (1_330_000, 1_417_088)
EARLIER, LATER = "2026-09-23T20:51:14+00:00", "2026-09-24T16:42:50+00:00"
WANTED = frozenset({MarkerType.INTRO})
NOTHING = {"sources": [{"id": "theintrodb", "enabled": False}], "detect": {"intro": True, "credits": True}}


def _none(mtype, reason=NO_EVIDENCE_REASON):
    return TypeDecision(mtype, DecisionStatus.NO_EVIDENCE, None, None, reason)


def _decided(mtype, start, end, by=("chapters",), locked=False, reason="chapters"):
    return TypeDecision(mtype, DecisionStatus.DECIDED, Marker(mtype, start, end, by, locked), None, reason)


def _decisions(**by_type):
    out = {mtype: TypeDecision(mtype, DecisionStatus.DISABLED, None, None, "detection off") for mtype in T}
    out.update({T(name): decision for name, decision in by_type.items()})
    return out


def _previous(mtype, marker, duration_ms=DUR, seen_at=EARLIER, decided_by=("chapters",)):
    return PreviousDecision(mtype, marker, duration_ms, seen_at, decided_by if marker else ())


def _carried(mtype, start, end):
    return TypeDecision(mtype, DecisionStatus.DECIDED, Marker(mtype, start, end, (co.CARRIED_OVER,)), None,
                        co.CARRIED_OVER_REASON)  # fmt: skip


class TestCarryOver:
    """The rule on one file's decisions, given what the file it replaced had decided."""

    @pytest.mark.parametrize("mtype", [T.INTRO, T.CREDITS])
    def test_a_type_with_no_evidence_keeps_the_replaced_files_marker_at_the_same_length(self, mtype):
        marker = INTRO if mtype is T.INTRO else CREDITS
        decisions = _decisions(**{mtype.value: _none(mtype)})
        out = co.carry_over(decisions, DUR, lambda wanted: {mtype: _previous(mtype, marker)})
        assert out[mtype] == _carried(mtype, *marker)
        assert {t: d for t, d in out.items() if t is not mtype} == {
            t: d for t, d in decisions.items() if t is not mtype
        }

    @pytest.mark.parametrize(("length_change_ms", "carried"), [(408, True), (1_000, True), (1_001, False),
                                                               (-1_000, True), (-1_001, False)])  # fmt: skip
    def test_only_within_a_second_of_the_replaced_files_length(self, length_change_ms, carried):
        decisions = _decisions(intro=_none(T.INTRO))
        previous = {T.INTRO: _previous(T.INTRO, (285_000, 306_000), duration_ms=DUR - length_change_ms)}
        out = co.carry_over(decisions, DUR, lambda wanted: previous)
        assert out[T.INTRO] == (_carried(T.INTRO, 285_000, 306_000) if carried else decisions[T.INTRO])

    @pytest.mark.parametrize(
        "own",
        [
            _decided(T.INTRO, 64_000, 152_000, ("season_audio",), reason="single source (season_audio)"),
            TypeDecision(
                T.INTRO,
                DecisionStatus.NEEDS_REVIEW,
                None,
                Marker(T.INTRO, 5_000, 30_000, ("skipdb",)),
                "sources disagree",
            ),  # fmt: skip
            _none(T.INTRO, "1 candidate(s) failed sanity checks"),
            _decided(T.INTRO, 1_000, 60_000, ("user",), locked=True, reason="locked by user"),
            TypeDecision(T.INTRO, DecisionStatus.DISABLED, None, None, "detection off"),
            TypeDecision(T.INTRO, DecisionStatus.DISABLED, None, None, "kept Plex's own marker"),
        ],
        ids=["decided", "needs-review", "failed-sanity", "locked", "detection-off", "kept-own"],
    )
    def test_the_new_files_own_evidence_or_setting_always_wins(self, own):
        decisions = _decisions(intro=own)
        asked = []
        out = co.carry_over(decisions, DUR, lambda wanted: asked.append(1) or {T.INTRO: _previous(T.INTRO, INTRO)})
        assert out == decisions and asked == []  # the replaced file isn't even looked up

    def test_a_replaced_file_that_had_no_marker_of_the_type_leaves_none(self):
        decisions = _decisions(intro=_none(T.INTRO))
        out = co.carry_over(decisions, DUR, lambda wanted: {T.INTRO: _previous(T.INTRO, None)})
        assert out == decisions

    def test_a_type_the_replaced_file_never_decided_leaves_none(self):
        decisions = _decisions(intro=_none(T.INTRO), credits=_none(T.CREDITS))
        out = co.carry_over(decisions, DUR, lambda wanted: {T.CREDITS: _previous(T.CREDITS, CREDITS)})
        assert out[T.INTRO] == decisions[T.INTRO] and out[T.CREDITS] == _carried(T.CREDITS, *CREDITS)

    def test_a_carried_end_past_the_new_files_end_is_clamped_to_it(self):
        decisions = _decisions(credits=_none(T.CREDITS))
        out = co.carry_over(decisions, DUR - 408, lambda wanted: {T.CREDITS: _previous(T.CREDITS, CREDITS)})
        assert out[T.CREDITS] == _carried(T.CREDITS, CREDITS[0], DUR - 408)

    def test_an_intro_that_would_run_to_the_new_files_end_is_not_carried(self):
        decisions = _decisions(intro=_none(T.INTRO))
        out = co.carry_over(decisions, 60_000, lambda wanted: {T.INTRO: _previous(T.INTRO, (0, 59_500), 60_500)})
        assert out == decisions

    def test_a_carried_intro_overlapping_the_files_own_recap_is_not_carried(self):
        recap = _decided(T.RECAP, 50_000, 80_000, ("skipdb",), reason="single source (skipdb)")
        decisions = _decisions(intro=_none(T.INTRO), recap=recap)
        out = co.carry_over(decisions, DUR, lambda wanted: {T.INTRO: _previous(T.INTRO, INTRO)})
        assert out == decisions

    def test_a_carried_preview_overlapping_the_files_own_credits_is_not_carried(self):
        credits = _decided(T.CREDITS, 1_330_000, 1_400_000, ("credits_text",), reason="single source (credits_text)")
        decisions = _decisions(credits=credits, preview=_none(T.PREVIEW))
        out = co.carry_over(decisions, DUR, lambda wanted: {T.PREVIEW: _previous(T.PREVIEW, (1_380_000, 1_417_000))})
        assert out == decisions

    def test_when_it_cant_be_told_now_what_was_carried_stays(self):
        # The replaced file's disk didn't answer in time, or a server couldn't name the item: not "nothing replaced".
        carried = _carried(T.INTRO, *INTRO).marker
        decisions = _decisions(intro=_none(T.INTRO), credits=_none(T.CREDITS))
        out = co.carry_over(decisions, DUR, lambda wanted: dict.fromkeys(wanted), kept={T.INTRO: carried})
        assert out[T.INTRO] == _carried(T.INTRO, *INTRO)
        assert out[T.CREDITS] == decisions[T.CREDITS]  # nothing carried before: nothing to keep

    @pytest.mark.parametrize("kept", [Marker(T.INTRO, *INTRO, ("chapters",)), None], ids=["own-marker", "none"])
    def test_when_it_cant_be_told_only_a_carried_marker_stays(self, kept):
        decisions = _decisions(intro=_none(T.INTRO))
        out = co.carry_over(decisions, DUR, lambda wanted: dict.fromkeys(wanted), kept={T.INTRO: kept} if kept else {})
        assert out == decisions

    @pytest.mark.parametrize(
        ("decided_by", "carried"),
        [
            (("theintrodb", "server_markers"), False),  # its only deciding source is off now
            (("chapters",), True),
            (("theintrodb", "chapters"), True),
            (("user",), True),  # the user's own marker
            ((co.CARRIED_OVER,), True),  # carried before: where it came from isn't known
        ],
    )
    def test_a_marker_whose_sources_are_all_off_now_isnt_carried(self, decided_by, carried):
        decisions = _decisions(intro=_none(T.INTRO))
        previous = {T.INTRO: _previous(T.INTRO, INTRO, decided_by=decided_by)}
        out = co.carry_over(decisions, DUR, lambda wanted: previous, enabled=("chapters", "introdb", "server_markers"))
        assert out[T.INTRO] == (_carried(T.INTRO, *INTRO) if carried else decisions[T.INTRO])

    def test_two_carried_markers_that_overlap_carry_only_the_first(self):
        decisions = _decisions(intro=_none(T.INTRO), recap=_none(T.RECAP))
        previous = {T.INTRO: _previous(T.INTRO, INTRO), T.RECAP: _previous(T.RECAP, (60_000, 120_000))}
        out = co.carry_over(decisions, DUR, lambda wanted: previous)
        assert out[T.INTRO] == _carried(T.INTRO, *INTRO) and out[T.RECAP] == decisions[T.RECAP]

    def test_only_the_types_with_no_evidence_are_looked_up(self):
        decisions = _decisions(intro=_none(T.INTRO), credits=_decided(T.CREDITS, *CREDITS))
        asked = []
        co.carry_over(decisions, DUR, lambda wanted: asked.append(wanted) or {})
        assert asked == [frozenset({T.INTRO})]

    def test_whether_a_marker_was_carried_over(self):
        assert co.is_carried_over(Marker(T.INTRO, 0, 1, (co.CARRIED_OVER,))) is True
        assert co.is_carried_over(Marker(T.INTRO, 0, 1, ("chapters",))) is False
        assert co.is_carried_over(None) is False


@pytest.fixture
def store(tmp_path):
    s = MarkerStore(str(tmp_path / "markers.db"))
    yield s
    s.close()


def _stored(store, path, *, size=100, mtime=1, duration_ms=DUR):
    return store.upsert_file(FileIdentity(path, size, mtime), duration_ms=duration_ms, season_key="/tv",
                             is_movie=False)  # fmt: skip


class TestReplacedInPlace:
    """A new file at the same path (a Tdarr transcode, a same-named upgrade): the old decisions are cleared with its
    identity, so what they were is kept aside first."""

    def test_the_old_decisions_are_kept_aside_when_the_identity_changes(self, store):
        rec = _stored(store, "/tv/S01E12.mkv")
        store.save_decisions(rec.id, _decisions(intro=_decided(T.INTRO, *INTRO), credits=_none(T.CREDITS)),
                             settings_fingerprint="f")  # fmt: skip
        new = _stored(store, "/tv/S01E12.mkv", size=200, mtime=2, duration_ms=None)
        kept = {d.type: d for d in store.replaced_in_place(new.id)}
        assert set(kept) == {T.INTRO, T.CREDITS}  # detection-off types were no decision of ours
        assert kept[T.INTRO].marker == INTRO and kept[T.INTRO].duration_ms == DUR
        assert kept[T.CREDITS].marker is None
        assert store.get_markers(new.id) == {}  # the new identity's own decisions start empty, as before

    def test_a_lock_survives_as_before_and_is_kept_aside_too(self, store):
        rec = _stored(store, "/tv/S01E12.mkv")
        store.lock_marker(rec.id, Marker(T.INTRO, *INTRO, ("user",), True))
        store.save_decisions(rec.id, _decisions(intro=_decided(T.INTRO, *INTRO, ("user",), locked=True,
                                                               reason="locked by user")), settings_fingerprint="f")  # fmt: skip
        new = _stored(store, "/tv/S01E12.mkv", size=200, mtime=2)
        assert {d.type: d.marker for d in store.replaced_in_place(new.id)}[T.INTRO] == INTRO
        assert T.INTRO in store.get_locked(new.id)  # the lock still wins the new identity's decision (rule 1)

    def test_an_identity_never_decided_keeps_the_earlier_snapshot(self, store):
        rec = _stored(store, "/tv/S01E12.mkv")
        store.save_decisions(rec.id, _decisions(intro=_decided(T.INTRO, *INTRO)), settings_fingerprint="f")
        _stored(store, "/tv/S01E12.mkv", size=200, mtime=2)  # replaced again before its run decided anything
        new = _stored(store, "/tv/S01E12.mkv", size=300, mtime=3)
        assert [(d.type, d.marker, d.duration_ms) for d in store.replaced_in_place(new.id)] == [(T.INTRO, INTRO, DUR)]

    def test_an_identity_of_unknown_length_keeps_nothing_aside(self, store):
        rec = _stored(store, "/tv/S01E12.mkv", duration_ms=None)
        store.save_decisions(rec.id, _decisions(intro=_decided(T.INTRO, *INTRO)), settings_fingerprint="f")
        new = _stored(store, "/tv/S01E12.mkv", size=200, mtime=2)
        assert store.replaced_in_place(new.id) == []


class TestReplacedInPlaceEdges:
    def test_the_deciding_sources_are_kept_aside_too(self, store):
        rec = _stored(store, "/tv/S01E12.mkv")
        store.save_decisions(rec.id, _decisions(intro=_decided(T.INTRO, *INTRO, ("introdb", "season_audio"))),
                             settings_fingerprint="f")  # fmt: skip
        new = _stored(store, "/tv/S01E12.mkv", size=200, mtime=2)
        assert [d.decided_by for d in store.replaced_in_place(new.id)] == [("introdb", "season_audio")]

    def test_a_no_marker_of_another_length_doesnt_replace_a_kept_marker(self, store):
        # A run over a file still being copied read a wrong length and found nothing: the intro kept aside stays.
        rec = _stored(store, "/tv/S01E12.mkv")
        store.save_decisions(rec.id, _decisions(intro=_decided(T.INTRO, *INTRO)), settings_fingerprint="f")
        half = _stored(store, "/tv/S01E12.mkv", size=150, mtime=2, duration_ms=300_000)
        store.save_decisions(half.id, _decisions(intro=_none(T.INTRO)), settings_fingerprint="f")
        new = _stored(store, "/tv/S01E12.mkv", size=200, mtime=3)
        assert [(d.marker, d.duration_ms) for d in store.replaced_in_place(new.id)] == [(INTRO, DUR)]

    def test_a_no_marker_of_the_same_length_does_replace_it(self, store):
        rec = _stored(store, "/tv/S01E12.mkv")
        store.save_decisions(rec.id, _decisions(intro=_decided(T.INTRO, *INTRO)), settings_fingerprint="f")
        same = _stored(store, "/tv/S01E12.mkv", size=150, mtime=2, duration_ms=DUR + 400)
        store.save_decisions(same.id, _decisions(intro=_none(T.INTRO)), settings_fingerprint="f")
        new = _stored(store, "/tv/S01E12.mkv", size=200, mtime=3)
        assert [d.marker for d in store.replaced_in_place(new.id)] == [None]

    def test_a_file_moved_here_from_another_path_carries_nothing_of_this_paths_file(self, store):
        # Anime renumbered: E05's file is moved to E04's path (same length to the second). It is E05, not E04's
        # replacement.
        e04 = _stored(store, "/tv/S01E04.mkv")
        store.save_decisions(e04.id, _decisions(intro=_decided(T.INTRO, *INTRO)), settings_fingerprint="f")
        _stored(store, "/tv/S01E05.mkv", size=555, mtime=5)
        moved = _stored(store, "/tv/S01E04.mkv", size=555, mtime=5)
        assert co.previous_decisions(store, moved, [], wanted=WANTED, gone=lambda rec: True) == {}


class TestPreviousDecisions:
    """Which decision counts as the replaced file's: the latest of the files gone from the new file's server items,
    and of its own path's earlier identity."""

    def _published(self, store, path, marker, *, duration_ms=DUR, item="item-1"):
        rec = _stored(store, path, duration_ms=duration_ms)
        decision = _decided(T.INTRO, *marker) if marker else _none(T.INTRO)
        store.save_decisions(rec.id, _decisions(intro=decision), settings_fingerprint="f")
        store.set_publish_state(rec.id, "plex-1", item_id=item, markers=[], status="written")
        return rec

    def test_the_latest_replaced_file_decides(self, store, monkeypatch):
        clock = iter([EARLIER, LATER])
        monkeypatch.setattr(store, "_now", lambda: next(clock, LATER))
        self._published(store, "/tv/a.mkv", INTRO)  # seen first, then replaced by b
        self._published(store, "/tv/b.mkv", None)  # which found no intro: that was our last decision
        new = _stored(store, "/tv/c.mkv")
        found = co.previous_decisions(store, new, [("plex-1", "item-1")], wanted=WANTED, gone=lambda rec: True)
        assert found[T.INTRO].marker is None

    def test_a_file_still_on_disk_is_another_version_not_a_replaced_one(self, store):
        other = self._published(store, "/tv/a.mkv", INTRO)
        new = _stored(store, "/tv/c.mkv")
        assert (
            co.previous_decisions(
                store, new, [("plex-1", "item-1")], wanted=WANTED, gone=lambda rec: rec.id != other.id
            )
            == {}
        )

    def test_only_files_of_the_new_files_own_items_count(self, store):
        self._published(store, "/tv/a.mkv", INTRO, item="item-2")
        new = _stored(store, "/tv/c.mkv")
        assert co.previous_decisions(store, new, [("plex-1", "item-1")], wanted=WANTED, gone=lambda rec: True) == {}
        assert co.previous_decisions(store, new, [("jellyfin-1", "item-2")], wanted=WANTED, gone=lambda rec: True) == {}

    def test_a_disk_that_cant_tell_is_not_a_file_still_there(self, store):
        self._published(store, "/tv/a.mkv", INTRO)
        new = _stored(store, "/tv/c.mkv")
        found = co.previous_decisions(store, new, [("plex-1", "item-1")], wanted=WANTED, gone=lambda rec: None)
        assert found == {T.INTRO: None}

    def test_a_server_that_couldnt_name_the_item_leaves_it_unknown(self, store):
        new = _stored(store, "/tv/c.mkv")
        found = co.previous_decisions(store, new, [], wanted=WANTED, gone=lambda rec: True, items_known=False)
        assert found == {T.INTRO: None}

    def test_the_disk_is_asked_only_about_files_with_a_decision_of_a_wanted_type(self, store, monkeypatch):
        clock = iter([EARLIER, EARLIER, EARLIER, LATER])
        monkeypatch.setattr(store, "_now", lambda: next(clock, LATER))
        older = self._published(store, "/tv/a.mkv", INTRO)
        newer = _stored(store, "/tv/b.mkv")  # another version, still on disk, stored later: skipped for the older one
        store.save_decisions(newer.id, _decisions(intro=_none(T.INTRO)), settings_fingerprint="f")
        store.set_publish_state(newer.id, "plex-1", item_id="item-1", markers=[], status="written")
        credits_only = _stored(store, "/tv/d.mkv")
        store.save_decisions(credits_only.id, _decisions(credits=_decided(T.CREDITS, *CREDITS)),
                             settings_fingerprint="f")  # fmt: skip
        store.set_publish_state(credits_only.id, "plex-1", item_id="item-1", markers=[], status="written")
        new = _stored(store, "/tv/c.mkv")
        asked = []

        def gone(rec):
            asked.append(rec.id)
            return rec.id == older.id

        found = co.previous_decisions(store, new, [("plex-1", "item-1")], wanted=WANTED, gone=gone)
        assert found[T.INTRO].marker == INTRO
        assert sorted(asked) == sorted([older.id, newer.id])  # never the file with only credits

    def test_the_same_paths_earlier_identity_counts(self, store):
        rec = _stored(store, "/tv/c.mkv")
        store.save_decisions(rec.id, _decisions(intro=_decided(T.INTRO, *INTRO)), settings_fingerprint="f")
        new = _stored(store, "/tv/c.mkv", size=200, mtime=2)
        found = co.previous_decisions(store, new, [], wanted=WANTED, gone=lambda rec: True)
        assert found[T.INTRO].marker == INTRO and found[T.INTRO].duration_ms == DUR


def _episode(tmp_path, name):
    folder = tmp_path / "media" / "tv" / "Tomb Raider King (2026) {tvdb-452039}" / "Season 01"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(b"x" * (100 + len(name)))
    return str(path)


OLD_NAME = "Tomb Raider King (2026) - S01E12 - TBA [WEBDL-1080p][AAC 2.0][h264].mkv"
NEW_NAME = "Tomb Raider King (2026) - S01E12 - TBA [WEBRip-1080p][AAC 2.0][x265].mkv"
OPENING = (Chapter(0, 92_000, "Intro"), Chapter(92_000, None, "Chapter 2"))


def _probe(chapters=(), duration=DUR):
    return MediaProbe(duration, tuple(chapters))


def _replace(tmp_path, store, *, new_duration=DUR, old_chapters=OPENING, new_chapters=(), settings=NOTHING):
    """The prod case: the old file decided and published, then deleted and replaced by a new file on the same item."""
    old = _episode(tmp_path, OLD_NAME)
    ctx = _ctx(store, _registry(old, ServerType.PLEX), settings_raw=settings)
    first, _ = _run(ctx, old, {"plex-1": ready_publisher()}, probe=_probe(old_chapters), stage="process")
    os.remove(old)
    new = _episode(tmp_path, NEW_NAME)
    ctx = _ctx(store, _registry(new, ServerType.PLEX), settings_raw=settings)
    out, _ = _run(ctx, new, {"plex-1": ready_publisher()}, probe=_probe(new_chapters, new_duration), stage="process")
    return first, out, store.get_file(new)


class TestPipeline:
    def test_tomb_raider_king_s01e12_keeps_its_opening_after_the_replacement(self, tmp_path, store):
        # The old file's Intro chapter decided 0-92 s; the new WEBRip, identical in length, has no chapters and no
        # source answered for it. Before: the intro was removed from Plex. Now it is kept, marked as carried over.
        first, out, rec = _replace(tmp_path, store)
        assert first.outcome_key == FileOutcome.PUBLISHED.value
        intro = store.get_decisions(rec.id)[T.INTRO]
        assert (intro.status, intro.reason) == (DecisionStatus.DECIDED, co.CARRIED_OVER_REASON)
        assert store.get_markers(rec.id)[T.INTRO] == Marker(T.INTRO, *INTRO, (co.CARRIED_OVER,))
        # The item still shows the intro we sent for the old file: nothing to remove, nothing to write.
        assert out.outcome_key == FileOutcome.UP_TO_DATE.value
        assert store.get_decisions(rec.id)[T.CREDITS].status is DecisionStatus.NO_EVIDENCE  # the old file had none

    def test_rupauls_drag_race_uk_s08e04_keeps_its_intro_across_a_408_ms_change(self, tmp_path, store):
        chapters = (Chapter(0, 285_000, "Chapter 1"), Chapter(285_000, 306_000, "Intro"),
                    Chapter(306_000, 4_055_000, "Chapter 2"), Chapter(4_055_000, None, "Credits"))  # fmt: skip
        new_chapters = (Chapter(0, 4_055_000, "Chapter 1"), Chapter(4_055_000, None, "Credits"))
        old = _episode(tmp_path, OLD_NAME)
        ctx = _ctx(store, _registry(old, ServerType.PLEX), settings_raw=NOTHING)
        _run(ctx, old, {"plex-1": ready_publisher()}, probe=_probe(chapters, 4_086_040), stage="process")
        os.remove(old)
        new = _episode(tmp_path, NEW_NAME)
        ctx = _ctx(store, _registry(new, ServerType.PLEX), settings_raw=NOTHING)
        _run(ctx, new, {"plex-1": ready_publisher()}, probe=_probe(new_chapters, 4_085_632), stage="process")
        markers = store.get_markers(store.get_file(new).id)
        assert markers[T.INTRO] == Marker(T.INTRO, 285_000, 306_000, (co.CARRIED_OVER,))
        assert markers[T.CREDITS] == Marker(T.CREDITS, 4_055_000, 4_085_632, ("chapters",))  # its own evidence

    def test_a_length_change_over_a_second_carries_nothing(self, tmp_path, store):
        _first, _out, rec = _replace(tmp_path, store, new_duration=DUR + 1_001)
        assert store.get_decisions(rec.id)[T.INTRO].status is DecisionStatus.NO_EVIDENCE
        assert T.INTRO not in store.get_markers(rec.id)

    def test_the_new_files_own_evidence_wins(self, tmp_path, store):
        own = (Chapter(0, 88_000, "Opening"), Chapter(88_000, None, "Chapter 2"))
        _first, _out, rec = _replace(tmp_path, store, new_chapters=own)
        assert store.get_markers(rec.id)[T.INTRO] == Marker(T.INTRO, 0, 88_000, ("chapters",))

    def test_a_carried_intro_gives_way_once_the_new_file_has_evidence(self, tmp_path, store):
        _first, _out, rec = _replace(tmp_path, store)
        assert store.get_markers(rec.id)[T.INTRO].decided_by == (co.CARRIED_OVER,)
        # SkipDB, asked again after its "no entry" went stale, now matches this file's length: its own evidence. SkipDB
        # never decides alone (rule 6), so the intro waits in Needs review on it; the carried marker is gone either way.
        own = LookupResult("ok", (Candidate(T.INTRO, 300, 89_200, Source.SKIPDB, origin="exact"),))
        new = rec.canonical_path
        ctx = _ctx(store, _registry(new, ServerType.PLEX), settings_raw=NOTHING, clients=_clients(skipdb=own),
                   force=True)  # fmt: skip
        _run(ctx, new, {"plex-1": ready_publisher()}, probe=_probe(), stage="process")
        assert T.INTRO not in store.get_markers(rec.id)
        decision = store.get_decisions(rec.id)[T.INTRO]
        assert (decision.status, decision.proposed_start_ms, decision.proposed_end_ms) == (
            DecisionStatus.NEEDS_REVIEW,
            300,
            89_200,
        )

    def test_a_file_replaced_in_place_keeps_its_intro(self, tmp_path, store):
        # A transcode at the same path (Tdarr) drops the chapters and keeps the length.
        path = _episode(tmp_path, NEW_NAME)
        ctx = _ctx(store, _registry(path, ServerType.PLEX), settings_raw=NOTHING)
        _run(ctx, path, {"plex-1": ready_publisher()}, probe=_probe(OPENING), stage="process")
        with open(path, "ab") as f:
            f.write(b"transcoded")
        _run(_ctx(store, _registry(path, ServerType.PLEX), settings_raw=NOTHING), path,
             {"plex-1": ready_publisher()}, probe=_probe(), stage="process")  # fmt: skip
        assert store.get_markers(store.get_file(path).id)[T.INTRO] == Marker(T.INTRO, *INTRO, (co.CARRIED_OVER,))

    def test_an_older_file_still_on_disk_is_another_version_and_carries_nothing(self, tmp_path, store):
        old = _episode(tmp_path, OLD_NAME)
        ctx = _ctx(store, _registry(old, ServerType.PLEX), settings_raw=NOTHING)
        _run(ctx, old, {"plex-1": ready_publisher()}, probe=_probe(OPENING), stage="process")
        new = _episode(tmp_path, NEW_NAME)  # the old one stays: two versions of one item
        _run(_ctx(store, _registry(new, ServerType.PLEX), settings_raw=NOTHING), new, {"plex-1": ready_publisher()},
             probe=_probe(), stage="process")  # fmt: skip
        assert T.INTRO not in store.get_markers(store.get_file(new).id)


class TestCantTell:
    """A check that can't answer now (a disk that doesn't answer in time, a server that can't name the item) never takes
    a carried marker off the servers."""

    @pytest.mark.parametrize("failure", ["disk", "server"])
    def test_a_carried_intro_stays_published_when_the_replaced_file_cant_be_checked(self, tmp_path, store, failure):
        _first, _out, rec = _replace(tmp_path, store)
        plex = ready_publisher()
        new = rec.canonical_path
        reg = _registry(new, ServerType.PLEX)
        if failure == "server":
            reg.get("plex-1").resolve_remote_path_to_item_id.side_effect = RuntimeError("Plex is busy")
        with patch.object(pipeline, "gone_now", return_value=None):
            _run(_ctx(store, reg, settings_raw=NOTHING, force=True), new, {"plex-1": plex}, probe=_probe(),
                 stage="process")  # fmt: skip
        assert store.get_markers(rec.id)[T.INTRO] == Marker(T.INTRO, *INTRO, (co.CARRIED_OVER,))
        assert all(call.args[1] for call in plex.write.call_args_list)  # never written empty

    def test_the_replaced_files_disk_answers_in_three_ways(self, tmp_path, store):
        path = _episode(tmp_path, OLD_NAME)
        rec = _stored(store, path)
        configs = _registry(path, ServerType.PLEX).configs()
        assert missing.gone_now(rec, configs) is False  # there
        os.remove(path)
        assert missing.gone_now(rec, configs) is True
        assert missing.gone_now(rec, []) is None  # under no library: its disk can't tell
        with (
            patch.object(missing, "_missing", side_effect=lambda *a: time.sleep(0.5) or True),
            patch.object(missing, "CHECK_TIMEOUT_S", 0.05),
        ):
            assert missing.gone_now(rec, configs) is None  # no answer in time
        store.mark_missing(rec)
        assert missing.gone_now(store.get_file(path), []) is True  # marked, and still not there


class TestBudgetRecheck:
    def test_a_carried_type_is_checked_again_after_theintrodbs_limit_resets(self, tmp_path, store):
        from media_preview_generator.markers import job_runner

        _first, _out, rec = _replace(tmp_path, store, settings={**NOTHING, "detect": {"intro": True, "credits": False}})
        assert job_runner._still_undecided(store, rec.canonical_path) is True

    def test_the_summary_keeps_the_daily_limit_note_for_a_carried_type(self):
        decisions = _decisions(intro=_carried(T.INTRO, *INTRO))
        text = pipeline._summary(decisions, frozenset({T.INTRO}), ("TheIntroDB",))
        assert text.endswith("TheIntroDB not checked (daily limit reached)")


class TestNotSettled:
    """A carried marker stands only until the new file has evidence: whatever asks whether new evidence could change a
    type treats it as undecided."""

    def test_a_carried_intro_is_not_settled_for_season_audio(self, tmp_path, store):
        _first, _out, rec = _replace(tmp_path, store)
        ctx = _ctx(store, _registry(rec.canonical_path, ServerType.PLEX), settings_raw=NOTHING)
        assert season._intro_settled(ctx, rec) is False

    def test_a_carried_marker_can_be_decided_by_an_online_answer(self, tmp_path, store):
        intro_only = {**NOTHING, "detect": {"intro": True, "credits": False}}  # no other type left undecided
        _first, _out, rec = _replace(tmp_path, store, settings=intro_only)
        assert store.get_markers(rec.id)[T.INTRO].decided_by == (co.CARRIED_OVER,)
        assert pipeline._online_answer_could_decide(store, rec.canonical_path) is True


def test_the_job_log_names_the_file_it_replaced():
    assert job_log.type_phrase(_carried(T.INTRO, *INTRO)) == "intro 0:00–1:32 (from the file it replaced)"
