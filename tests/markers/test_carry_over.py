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
                DecisionStatus.NO_EVIDENCE,
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

    @pytest.mark.parametrize(
        ("mtype", "decided_by", "versions", "read_by", "carried"),
        [
            # Small Prophets S01E05: season audio v9's 0-12 s logo stretch (2009), which v10 (2010) passes over.
            (T.INTRO, ("season_audio",), {"season_audio": 2009}, {"season_audio": co.ReadNow(2010)}, False),
            (T.INTRO, ("season_audio_previous", "server_markers"), {"season_audio_previous": 2009},
             {"season_audio_previous": co.ReadNow(3010)}, False),
            (T.CREDITS, ("credits_text",), {"credits_text": 6}, {"credits_text": co.ReadNow(7, 1_000)}, False),
            # The same version finding nothing on the new file: another encode's audio or picture, rule 15's own case.
            (T.INTRO, ("season_audio",), {"season_audio": 3010}, {"season_audio": co.ReadNow(3010)}, True),
            # Credit text's window rides above its version: the same version read on another window isn't newer.
            (T.CREDITS, ("credits_text",), {"credits_text": 7}, {"credits_text": co.ReadNow(300_007, 1_000)}, True),
            # The version that decided it isn't known.
            (T.INTRO, ("season_audio",), {}, {"season_audio": co.ReadNow(3010)}, True),
            # Kept aside by a build before versions were: at most season audio 2010, credit text 8 (#327's).
            (T.INTRO, ("season_audio",), None, {"season_audio": co.ReadNow(3010)}, False),
            (T.INTRO, ("season_audio",), None, {"season_audio": co.ReadNow(2010)}, True),
            (T.CREDITS, ("credits_text",), None, {"credits_text": co.ReadNow(8, 1_000)}, True),
            (T.CREDITS, ("credits_text",), None, {"credits_text": co.ReadNow(9, 1_000)}, False),
            # A chapter or an online answer still speaks: the new file lacking them says nothing about the intro.
            (T.INTRO, ("season_audio", "chapters"), {"season_audio": 2009}, {"season_audio": co.ReadNow(3010)}, True),
            (T.INTRO, ("introdb", "season_audio"), {"season_audio": 2009}, {"season_audio": co.ReadNow(3010)}, True),
            # Season audio didn't read this file now (an older version, nothing to compare, an answer due again).
            (T.INTRO, ("season_audio",), {"season_audio": 2009}, {}, True),
            (T.INTRO, ("user",), {}, {"season_audio": co.ReadNow(3010)}, True),
            (T.INTRO, (co.CARRIED_OVER,), {}, {"season_audio": co.ReadNow(3010)}, True),
        ],
        ids=["audio-older", "previous-season-with-a-server", "credit-text-older", "audio-same-version",
             "credit-text-other-window", "version-unknown", "before-versions-audio-newer",
             "before-versions-audio-same", "before-versions-text-same", "before-versions-text-newer",
             "audio-and-chapter", "online-and-audio",
             "audio-not-read-now", "user", "carried-before"],
    )  # fmt: skip
    def test_a_marker_only_content_detectors_decided_isnt_carried_once_a_newer_version_found_nothing(
        self, mtype, decided_by, versions, read_by, carried
    ):
        times = INTRO if mtype is T.INTRO else CREDITS
        decisions = _decisions(**{mtype.value: _none(mtype)})
        held = PreviousDecision(mtype, times, DUR, EARLIER, decided_by, versions)
        out = co.carry_over(
            decisions, DUR, lambda wanted: {mtype: held}, read_by=lambda asked: read_by if asked is mtype else {}
        )
        assert out[mtype] == (_carried(mtype, *times) if carried else decisions[mtype])

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

    def test_the_versions_that_decided_each_marker_are_kept_aside_too(self, store):
        rec = _stored(store, "/tv/S01E12.mkv")
        store.replace_evidence(rec.id, Source.SEASON_AUDIO, [Candidate(T.INTRO, *INTRO, Source.SEASON_AUDIO)],
                               version=2009)  # fmt: skip
        store.replace_evidence(rec.id, Source.CHAPTERS, [], version=3)
        store.save_decisions(rec.id, _decisions(intro=_decided(T.INTRO, *INTRO, by=("season_audio", "server_markers")),
                                                credits=_none(T.CREDITS)), settings_fingerprint="f")  # fmt: skip
        new = _stored(store, "/tv/S01E12.mkv", size=200, mtime=2)
        kept = {d.type: d for d in store.replaced_in_place(new.id)}
        assert dict(kept[T.INTRO].versions) == {"season_audio": 2009}  # the deciding sources' only
        assert dict(kept[T.CREDITS].versions) == {}
        assert store.evidence_version(new.id, Source.SEASON_AUDIO) is None  # the new identity's own start empty

    def test_a_row_kept_aside_by_a_build_without_versions_has_none(self, store):
        # A build from before replaced_versions (or a rollback to one) writes replaced_decisions alone, with its own
        # seen_at: the versions of an earlier row at this path don't describe it (carry_over bounds them instead).
        rec = _stored(store, "/tv/S01E12.mkv")
        store.replace_evidence(rec.id, Source.SEASON_AUDIO, [], version=2009)
        store.save_decisions(rec.id, _decisions(intro=_decided(T.INTRO, *INTRO, by=("season_audio",))),
                             settings_fingerprint="f")  # fmt: skip
        new = _stored(store, "/tv/S01E12.mkv", size=200, mtime=2)
        store._conn.execute("UPDATE replaced_decisions SET seen_at='2026-09-29T00:00:00+00:00' WHERE file_id=?",
                            (new.id,))  # fmt: skip
        (kept,) = [d for d in store.replaced_in_place(new.id) if d.type is T.INTRO]
        assert kept.marker == INTRO and kept.versions is None

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

    def test_a_gone_files_decision_carries_the_versions_of_its_deciding_answers(self, store):
        old = _stored(store, "/tv/a.mkv")
        store.replace_evidence(old.id, Source.SEASON_AUDIO, [Candidate(T.INTRO, *INTRO, Source.SEASON_AUDIO)],
                               version=2009)  # fmt: skip
        store.replace_evidence(old.id, Source.CREDITS_TEXT, [], version=7)
        store.save_decisions(old.id, _decisions(intro=_decided(T.INTRO, *INTRO, by=("season_audio",))),
                             settings_fingerprint="f")  # fmt: skip
        store.set_publish_state(old.id, "plex-1", item_id="item-1", markers=[], status="written")
        new = _stored(store, "/tv/c.mkv")
        found = co.previous_decisions(store, new, [("plex-1", "item-1")], wanted=WANTED, gone=lambda rec: True)
        assert found[T.INTRO].marker == INTRO
        assert dict(found[T.INTRO].versions) == {"season_audio": 2009}

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
        # never decides alone (rule 6), so the intro stays undecided on it; the carried marker is gone either way.
        own = LookupResult("ok", (Candidate(T.INTRO, 300, 89_200, Source.SKIPDB, origin="exact"),))
        new = rec.canonical_path
        ctx = _ctx(store, _registry(new, ServerType.PLEX), settings_raw=NOTHING, clients=_clients(skipdb=own),
                   force=True)  # fmt: skip
        _run(ctx, new, {"plex-1": ready_publisher()}, probe=_probe(), stage="process")
        assert T.INTRO not in store.get_markers(rec.id)
        decision = store.get_decisions(rec.id)[T.INTRO]
        assert (decision.status, decision.proposed_start_ms, decision.proposed_end_ms) == (
            DecisionStatus.NO_EVIDENCE,
            300,
            89_200,
        )
        assert decision.reason == "only SkipDB has the intro; an online answer needs a check against the file"

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


class TestContentDetectorsReadingTheNewFile:
    """A marker only season audio or credit text decided for the replaced file isn't carried once a newer version of
    it read the new file and found nothing (Small Prophets S01E05 and E06 on sflix, 2026-09-28); the same version
    finding nothing, a read that fails and a cancel leave it carried."""

    LOGO = Candidate(T.INTRO, 0, 12_012, Source.SEASON_AUDIO, 1.0, "1/1")
    ROLL = Candidate(T.CREDITS, 1_330_000, None, Source.CREDITS_TEXT)
    CASES = {
        "season-audio": (Source.SEASON_AUDIO, T.INTRO, LOGO, Marker(T.INTRO, 0, 12_012, (co.CARRIED_OVER,))),
        "credit-text": (Source.CREDITS_TEXT, T.CREDITS, ROLL, Marker(T.CREDITS, 1_330_000, DUR, (co.CARRIED_OVER,))),
    }

    def _spec(self, case, answers, version, compared=True, due=None, version_of=None, followups=None):
        from media_preview_generator.markers.pipeline import LocalDetectorSpec

        source, mtype, _found, _carried_marker = self.CASES[case]

        def detect(rec, **kwargs):
            answer = answers.pop(0)
            if callable(answer):
                answer = answer(rec, **kwargs)
            if isinstance(answer, BaseException):
                raise answer
            return answer

        stores = frozenset({Source.SEASON_AUDIO, Source.SEASON_AUDIO_PREVIOUS}) if mtype is T.INTRO else frozenset()
        return LocalDetectorSpec(source, frozenset({mtype}), detect, stores=stores, version=version,
                                 version_of=version_of, version_step=1_000 if mtype is T.CREDITS else 0,
                                 compared=lambda rec, ctx: compared, due=due, followups=followups)  # fmt: skip

    def _run_with(self, path, store, spec, **kwargs):
        ctx = _ctx(store, _registry(path, ServerType.PLEX), settings_raw=NOTHING, detectors=(spec,))
        out, _ = _run(ctx, path, {"plex-1": ready_publisher()}, probe=_probe(), stage="process", **kwargs)
        return out

    def _replaced_in_place(self, tmp_path, store, case, *, old_version, new_spec, old_version_of=None, legacy=False):
        source, mtype, found, _carried_marker = self.CASES[case]
        path = _episode(tmp_path, NEW_NAME)
        self._run_with(path, store, self._spec(case, [[found]], old_version, version_of=old_version_of))
        rec = store.get_file(path)
        assert store.get_markers(rec.id)[mtype].decided_by == (source.value,)
        with open(path, "ab") as f:
            f.write(b"replaced")
        if legacy:
            # The replacement seen by a build without replaced_versions (82dc2bc): its snapshot has no versions.
            st = os.stat(path)
            store.upsert_file(FileIdentity(path, st.st_size, st.st_mtime_ns), duration_ms=DUR,
                              season_key=os.path.dirname(path), is_movie=False)  # fmt: skip
            store._conn.execute("DELETE FROM replaced_versions")
            (held,) = [d for d in store.replaced_in_place(rec.id) if d.type is mtype]
            assert held.versions is None
        out = self._run_with(path, store, new_spec)
        return store.get_file(path), out

    @pytest.mark.parametrize("case", ["season-audio", "credit-text"])
    def test_a_newer_version_finding_nothing_takes_it_off(self, tmp_path, store, case):
        source, mtype, _found, _carried_marker = self.CASES[case]
        rec, _out = self._replaced_in_place(tmp_path, store, case, old_version=6, new_spec=self._spec(case, [[]], 7))
        assert store.evidence_version(rec.id, source) == 7
        assert mtype not in store.get_markers(rec.id)
        decision = store.get_decisions(rec.id)[mtype]
        assert (decision.status, decision.reason) == (DecisionStatus.NO_EVIDENCE, NO_EVIDENCE_REASON)

    @pytest.mark.parametrize("case", ["season-audio", "credit-text"])
    def test_the_same_version_finding_nothing_keeps_it_carried(self, tmp_path, store, case):
        # Rule 15's own case: another encode of the same episode that today's detector doesn't read the same way.
        _source, mtype, _found, carried_marker = self.CASES[case]
        rec, _out = self._replaced_in_place(tmp_path, store, case, old_version=7, new_spec=self._spec(case, [[]], 7))
        assert store.get_markers(rec.id)[mtype] == carried_marker
        assert store.get_decisions(rec.id)[mtype].reason == co.CARRIED_OVER_REASON

    @pytest.mark.parametrize(
        ("old_version", "legacy"), [(8, False), (None, True)], ids=["same-version-on-automatic", "kept-before-versions"]
    )
    def test_credit_texts_window_is_not_a_newer_version(self, tmp_path, store, old_version, legacy):
        # The user chose a window (300 s): credit text 8 stores 300_008. The replaced file's answer was 8 read on
        # Automatic, or a snapshot from a build without versions (at most 8): the same detector either way.
        _source, _mtype, _found, carried_marker = self.CASES["credit-text"]
        window = lambda rec, ctx: 300_008  # noqa: E731
        spec = self._spec("credit-text", [[]], 8, version_of=window)
        rec, _out = self._replaced_in_place(tmp_path, store, "credit-text", old_version=old_version or 8,
                                            new_spec=spec, legacy=legacy)  # fmt: skip
        assert store.evidence_version(rec.id, Source.CREDITS_TEXT) == 300_008
        assert store.get_markers(rec.id)[T.CREDITS] == carried_marker

    def test_credit_texts_spec_compares_below_the_window(self):
        from media_preview_generator.markers.credits import detector

        assert detector.credits_text_spec().version_step == detector._WINDOW_VERSION_STEP == 1_000

    def test_a_due_answer_read_again_in_the_run_is_todays_verdict(self, tmp_path, store):
        # Like season audio's own hooks, ``due`` reads a season view the run keeps in its memo, made before the
        # detector ran (the follow-ups ask it first). The read stores a new signature; the memo is dropped after a
        # detector ran, so the view is made again and the answer isn't due: it counts, and the older marker goes.
        from media_preview_generator.markers.pipeline import DetectorAnswer

        state = {"season": "before"}

        def view(rec, ctx):
            return ctx.run_memo(rec.canonical_path).setdefault("season", state["season"])

        def read(rec, **_kwargs):
            state["season"] = "after"  # the read fingerprints a sibling: the season's signature moves
            return DetectorAnswer((), "after")

        spec = self._spec("season-audio", [read], 7,
                          due=lambda rec, ctx: view(rec, ctx) != ctx.store.get_detector_run(rec.id, Source.SEASON_AUDIO),
                          followups=lambda rec, ctx: (view(rec, ctx), [])[1])  # fmt: skip
        rec, _out = self._replaced_in_place(tmp_path, store, "season-audio", old_version=6, new_spec=spec)
        assert store.get_detector_run(rec.id, Source.SEASON_AUDIO) == "after"
        assert T.INTRO not in store.get_markers(rec.id)
        assert store.get_decisions(rec.id)[T.INTRO].status is DecisionStatus.NO_EVIDENCE

    def test_nothing_to_compare_with_carries_it(self, tmp_path, store):
        # Season audio finds nothing whatever the file holds without another episode to match: no verdict on it.
        spec = self._spec("season-audio", [[]], 7, compared=False)
        rec, _out = self._replaced_in_place(tmp_path, store, "season-audio", old_version=6, new_spec=spec)
        assert store.get_markers(rec.id)[T.INTRO] == self.CASES["season-audio"][3]

    @pytest.mark.parametrize("case", ["season-audio", "credit-text"])
    def test_a_read_that_fails_keeps_it_carried(self, tmp_path, store, case):
        from media_preview_generator.markers.pipeline import DetectorUnavailableError

        _source, mtype, _found, carried_marker = self.CASES[case]
        spec = self._spec(case, [DetectorUnavailableError("the season's disk can't be read")], 7)
        rec, _out = self._replaced_in_place(tmp_path, store, case, old_version=6, new_spec=spec)
        assert store.get_markers(rec.id)[mtype] == carried_marker

    def test_an_answer_due_again_whose_read_fails_keeps_it_carried(self, tmp_path, store):
        # Season audio stored "nothing" while no other episode had a fingerprint (carried); a sibling is fingerprinted
        # later, so the answer is due and "compared" would now say yes, but the read again fails: the old "nothing" is
        # no verdict on the file.
        from media_preview_generator.markers.pipeline import DetectorUnavailableError

        spec = self._spec("season-audio", [[]], 7, compared=False)
        rec, _out = self._replaced_in_place(tmp_path, store, "season-audio", old_version=6, new_spec=spec)
        assert store.get_markers(rec.id)[T.INTRO] == self.CASES["season-audio"][3]
        again = self._spec("season-audio", [DetectorUnavailableError("a stalled mount")], 7, compared=True,
                           due=lambda rec, ctx: True)  # fmt: skip
        self._run_with(rec.canonical_path, store, again)
        assert store.get_markers(rec.id)[T.INTRO] == self.CASES["season-audio"][3]
        assert store.evidence_version(rec.id, Source.SEASON_AUDIO) == 7

    def test_a_cancel_during_the_read_changes_nothing(self, tmp_path, store):
        from media_preview_generator.markers.pipeline import DetectorUnavailableError

        rec, _out = self._replaced_in_place(tmp_path, store, "season-audio", old_version=7,
                                            new_spec=self._spec("season-audio", [[]], 7))  # fmt: skip
        before = (store.get_markers(rec.id), store.get_decisions(rec.id)[T.INTRO])
        assert before[0][T.INTRO] == self.CASES["season-audio"][3]
        cancel = {"on": False}

        def cancelled_part_way(rec, **_kwargs):
            cancel["on"] = True
            return DetectorUnavailableError("cancelled")

        spec = self._spec("season-audio", [cancelled_part_way], 8)
        out = self._run_with(rec.canonical_path, store, spec, cancel_check=lambda: cancel["on"])
        assert cancel["on"] is True  # the read started, then the cancel landed
        assert out.outcome_key == FileOutcome.FAILED.value
        assert (store.get_markers(rec.id), store.get_decisions(rec.id)[T.INTRO]) == before
        assert store.evidence_version(rec.id, Source.SEASON_AUDIO) == 7

    def test_a_cancel_changes_nothing(self, tmp_path, store):
        rec, _out = self._replaced_in_place(tmp_path, store, "season-audio", old_version=7,
                                            new_spec=self._spec("season-audio", [[]], 7))  # fmt: skip
        before = (store.get_markers(rec.id), store.get_decisions(rec.id)[T.INTRO])
        assert before[0][T.INTRO] == self.CASES["season-audio"][3]
        out = self._run_with(rec.canonical_path, store, self._spec("season-audio", [[]], 8), cancel_check=lambda: True)
        assert out.outcome_key == FileOutcome.FAILED.value
        assert (store.get_markers(rec.id), store.get_decisions(rec.id)[T.INTRO]) == before
        assert store.evidence_version(rec.id, Source.SEASON_AUDIO) == 7


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
