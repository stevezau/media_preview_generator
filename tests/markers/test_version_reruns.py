"""Answers from an older detector version, read again once (``markers.versions``): which files, how many, and never
the same file twice for the same version."""

from __future__ import annotations

import os

import pytest

from media_preview_generator.markers import versions
from media_preview_generator.markers.audio import end_picture
from media_preview_generator.markers.audio.season import SEASON_AUDIO_ANSWER_VERSION, SEASON_AUDIO_VERSION
from media_preview_generator.markers.credits.detector import CREDITS_TEXT_VERSION
from media_preview_generator.markers.decide import DECIDE_RULES, DECIDE_RULES_VERSION, DecisionStatus, TypeDecision
from media_preview_generator.markers.models import SERVER_SOURCES, Candidate, FileIdentity, Marker, MarkerType, Source
from media_preview_generator.markers.pipeline import PARSER_VERSIONS
from media_preview_generator.markers.settings import load_global, validate_global
from media_preview_generator.markers.sources.chapters import CHAPTER_RULES_VERSION
from media_preview_generator.markers.sources.server_markers import READER_VERSION
from media_preview_generator.markers.store import MarkerStore

T = MarkerType
ALL_SOURCES_ON = [
    {"id": s, "enabled": True}
    for s in ("chapters", "theintrodb", "introdb", "skipdb", "season_audio", "credits_text", "server_markers")
]


@pytest.fixture
def store(tmp_path):
    s = MarkerStore(str(tmp_path / "markers.db"))
    yield s
    s.close()


def _settings(*, off=(), credits=True):
    sources = [{**s, "enabled": s["id"] not in off} for s in ALL_SOURCES_ON]
    raw = {"detect": {"intro": True, "credits": credits, "recap": False}, "sources": sources}
    return load_global(validate_global(raw, None)[0])


def _file(store, path):
    return store.upsert_file(
        FileIdentity(path, 1, 1), duration_ms=1_300_000, season_key=os.path.dirname(path), is_movie=False
    )


def _answer(store, rec, source, version, *, origin=""):
    store.replace_evidence(rec.id, source, [], origin=origin, version=version)


def _decided(store, rec, mtype, decided_by, *, locked=False):
    marker = Marker(mtype, 1_200_000, 1_300_000, tuple(decided_by), locked=locked)
    store.save_decisions(
        rec.id, {mtype: TypeDecision(mtype, DecisionStatus.DECIDED, marker, None, "x")}, settings_fingerprint="f"
    )
    if locked:
        store.lock_marker(rec.id, marker)


def _undecided(store, rec, mtype, status):
    proposed = Marker(mtype, 1_200_000, 1_300_000, ("introdb",))
    store.save_decisions(rec.id, {mtype: TypeDecision(mtype, status, None, proposed, "x")}, settings_fingerprint="f")


def _credits_text_file(store, path, version=CREDITS_TEXT_VERSION - 1, decided_by=("credits_text", "chapters")):
    rec = _file(store, path)
    _answer(store, rec, Source.CREDITS_TEXT, version)
    _decided(store, rec, T.CREDITS, decided_by)
    return rec


class TestTheDetectors:
    def test_every_detector_and_reader_with_a_stored_version_is_listed_at_its_version_now(self):
        found = {a.key: (a.sources, a.types, a.version, a.version_step) for a in versions.answer_versions()}
        every_type = frozenset(MarkerType)
        assert found == {
            "credits_text": (frozenset({Source.CREDITS_TEXT}), frozenset({T.CREDITS}), CREDITS_TEXT_VERSION, 1_000),
            "season_audio": (
                frozenset({Source.SEASON_AUDIO, Source.SEASON_AUDIO_PREVIOUS}),
                frozenset({T.INTRO}),
                SEASON_AUDIO_ANSWER_VERSION,
                0,
            ),
            "server_markers": (SERVER_SOURCES, every_type, READER_VERSION, 0),
            "chapters": (frozenset({Source.CHAPTERS}), every_type, CHAPTER_RULES_VERSION, 0),
            "theintrodb": (frozenset({Source.THEINTRODB}), every_type, PARSER_VERSIONS[Source.THEINTRODB], 0),
            "introdb": (frozenset({Source.INTRODB}), every_type, PARSER_VERSIONS[Source.INTRODB], 0),
            "skipdb": (frozenset({Source.SKIPDB}), every_type, PARSER_VERSIONS[Source.SKIPDB], 0),
        }

    def test_the_end_picture_check_rides_in_season_audios_answer_version(self):
        # The check is part of season audio's answer (§5.3 guards): its first version adds nothing, so today's answers
        # stay current, and a new check makes every season audio answer older.
        assert SEASON_AUDIO_ANSWER_VERSION == SEASON_AUDIO_VERSION + (end_picture.CHECK_VERSION - 1) * 1_000
        # Season audio v9 (v8 the end-picture check on one scaler, v9 speed by ear) with check 3 (2 the one scaler, 3 the
        # end card), spec §14 2026-09-25.
        assert (SEASON_AUDIO_VERSION, end_picture.CHECK_VERSION, SEASON_AUDIO_ANSWER_VERSION) == (9, 3, 2009)


class TestFilesToReadAgain:
    def test_a_decided_type_resting_on_an_older_answer_is_listed_with_the_version_it_is_read_for(self, store):
        _credits_text_file(store, "/tv/A/S01/e1.mkv")

        assert versions.files_to_read_again(store, _settings()) == {
            "/tv/A/S01/e1.mkv": {"credits_text": CREDITS_TEXT_VERSION}
        }

    @pytest.mark.parametrize(
        ("status", "listed"),
        [
            (DecisionStatus.NEEDS_REVIEW, True),
            (DecisionStatus.NO_EVIDENCE, True),
            (DecisionStatus.DISABLED, False),  # detection off, or kept as the server's own
        ],
    )
    def test_an_undecided_type_with_an_older_answer(self, store, status, listed):
        rec = _file(store, "/tv/A/S01/e1.mkv")
        _answer(store, rec, Source.CREDITS_TEXT, CREDITS_TEXT_VERSION - 1)
        _undecided(store, rec, T.CREDITS, status)

        assert bool(versions.files_to_read_again(store, _settings())) is listed

    @pytest.mark.parametrize(
        ("decided_by", "locked", "version"),
        [
            (("chapters",), False, CREDITS_TEXT_VERSION - 1),  # decided without it: its next run reads it anyway
            (("credits_text", "chapters"), True, CREDITS_TEXT_VERSION - 1),  # the user's own
            (("credits_text", "chapters"), False, CREDITS_TEXT_VERSION),  # current
            (("credits_text", "chapters"), False, CREDITS_TEXT_VERSION + 1),  # newer (a downgrade): not older
        ],
        ids=["decided-by-others", "locked", "current", "newer"],
    )
    def test_not_listed(self, store, decided_by, locked, version):
        rec = _file(store, "/tv/A/S01/e1.mkv")
        _answer(store, rec, Source.CREDITS_TEXT, version)
        _decided(store, rec, T.CREDITS, decided_by, locked=locked)

        assert versions.files_to_read_again(store, _settings()) == {}

    @pytest.mark.parametrize(
        ("stored", "listed"),
        [(CREDITS_TEXT_VERSION - 1 + 300 * 1_000, True), (CREDITS_TEXT_VERSION + 300 * 1_000, False)],
        ids=["older-at-a-chosen-window", "current-at-a-chosen-window"],
    )
    def test_credit_text_compares_the_version_below_the_users_window(self, store, stored, listed):
        _credits_text_file(store, "/tv/A/S01/e1.mkv", version=stored)

        assert bool(versions.files_to_read_again(store, _settings())) is listed

    def test_each_detector_lists_its_own_sources_and_types(self, store):
        audio = _file(store, "/tv/A/S01/e1.mkv")
        _answer(store, audio, Source.SEASON_AUDIO_PREVIOUS, SEASON_AUDIO_ANSWER_VERSION - 1)
        _decided(store, audio, T.INTRO, ("season_audio_previous", "introdb"))
        plex = _file(store, "/tv/A/S01/e2.mkv")
        _answer(store, plex, Source.SERVER_MARKERS, READER_VERSION - 1, origin="plex-1")
        _decided(store, plex, T.INTRO, ("introdb", "server_markers"))
        chapters = _file(store, "/movies/B/b.mkv")
        _answer(store, chapters, Source.CHAPTERS, CHAPTER_RULES_VERSION - 1)
        _undecided(store, chapters, T.CREDITS, DecisionStatus.NEEDS_REVIEW)
        other_type = _file(store, "/tv/A/S01/e3.mkv")
        _answer(store, other_type, Source.SEASON_AUDIO, SEASON_AUDIO_ANSWER_VERSION - 1)
        _undecided(store, other_type, T.CREDITS, DecisionStatus.NEEDS_REVIEW)  # season audio answers intros only

        assert versions.files_to_read_again(store, _settings()) == {
            "/tv/A/S01/e1.mkv": {"season_audio": SEASON_AUDIO_ANSWER_VERSION},
            "/tv/A/S01/e2.mkv": {"server_markers": READER_VERSION},
            "/movies/B/b.mkv": {"chapters": CHAPTER_RULES_VERSION},
        }

    @pytest.mark.parametrize("stored", [4, 5, 7])  # production's season audio versions (audit copy, 2026-09-25)
    @pytest.mark.parametrize("decided", [True, False], ids=["decided-by-season-audio", "needs-review"])
    def test_a_season_audio_answer_from_before_todays_check_is_listed(self, store, stored, decided):
        # Its end pictures were compared on each vendor's scaler without the end card (check 1): the re-run makes the
        # season step decode them again (the share cache is keyed by the check version too).
        rec = _file(store, "/tv/A/S01/e1.mkv")
        _answer(store, rec, Source.SEASON_AUDIO, stored)
        if decided:
            _decided(store, rec, T.INTRO, ("season_audio", "introdb"))
        else:
            _undecided(store, rec, T.INTRO, DecisionStatus.NEEDS_REVIEW)

        assert versions.files_to_read_again(store, _settings()) == {"/tv/A/S01/e1.mkv": {"season_audio": 2009}}

    def test_a_file_resting_on_two_older_answers_is_listed_once_for_both(self, store):
        rec = _credits_text_file(store, "/tv/A/S01/e1.mkv", decided_by=("credits_text", "server_markers"))
        _answer(store, rec, Source.SERVER_MARKERS, READER_VERSION - 1, origin="plex-1")

        assert versions.files_to_read_again(store, _settings()) == {
            "/tv/A/S01/e1.mkv": {"credits_text": CREDITS_TEXT_VERSION, "server_markers": READER_VERSION}
        }

    @pytest.mark.parametrize(
        "settings",
        [_settings(off=("credits_text",)), _settings(credits=False)],
        ids=["source-off", "credits-detection-off"],
    )
    def test_a_detector_a_run_wouldnt_ask_lists_nothing(self, store, settings):
        _credits_text_file(store, "/tv/A/S01/e1.mkv")

        assert versions.files_to_read_again(store, settings) == {}

    def test_a_file_marked_missing_from_disk_isnt_listed(self, store):
        rec = _credits_text_file(store, "/tv/A/S01/e1.mkv")
        assert store.mark_missing(rec)

        assert versions.files_to_read_again(store, _settings()) == {}

    def test_a_file_taken_for_this_version_isnt_listed_again_but_is_for_the_next(self, store, monkeypatch):
        _credits_text_file(store, "/tv/A/S01/e1.mkv")
        store.record_version_reruns([("/tv/A/S01/e1.mkv", "credits_text", CREDITS_TEXT_VERSION)])
        assert versions.files_to_read_again(store, _settings()) == {}

        monkeypatch.setattr(versions, "CREDITS_TEXT_VERSION", CREDITS_TEXT_VERSION + 1)
        assert versions.files_to_read_again(store, _settings()) == {
            "/tv/A/S01/e1.mkv": {"credits_text": CREDITS_TEXT_VERSION + 1}
        }

    def test_a_file_stored_again_forgets_what_it_was_taken_for(self, store):
        # A replaced file loses its answers with its identity, so what it was taken for goes too.
        rec = _credits_text_file(store, "/tv/A/S01/e1.mkv")
        store.record_version_reruns([("/tv/A/S01/e1.mkv", "credits_text", CREDITS_TEXT_VERSION)])

        store.upsert_file(FileIdentity(rec.canonical_path, 2, 2), duration_ms=1, season_key=None, is_movie=False)

        assert store.version_rerun(rec.id, "credits_text") is None


class TestDecideRules:
    """The rules that decide from stored answers have a version too (``decide.DECIDE_RULES_VERSION``): a file decided
    under older rules is decided again from what is stored, and every run that decides a file records the rules it was
    decided under (``pipeline._attempt``)."""

    PATH = "/tv/A/S01/e1.mkv"
    DUE = {PATH: {DECIDE_RULES: DECIDE_RULES_VERSION}}

    def _with_candidate(self, store, mtype=T.INTRO):
        rec = _file(store, self.PATH)
        store.replace_evidence(
            rec.id,
            Source.SKIPDB,
            [Candidate(mtype, 20_000, 80_000, Source.SKIPDB)],
            version=PARSER_VERSIONS[Source.SKIPDB],
        )
        return rec

    def test_the_2026_09_25_rules_are_version_1(self):
        # SkipDB never decides alone, credit text checks a credits chapter SkipDB disagrees with, online credits may
        # end up to 5 s past the file, and another release's intro beside season audio (spec §5.5 rules 2, 3, 6, 14).
        assert (DECIDE_RULES, DECIDE_RULES_VERSION) == ("decide_rules", 1)

    @pytest.mark.parametrize(
        ("status", "reason", "locked", "listed"),
        [
            (DecisionStatus.DECIDED, "x", False, True),
            (DecisionStatus.DECIDED, "x", True, False),  # the user's own
            (DecisionStatus.NEEDS_REVIEW, "x", False, True),
            (DecisionStatus.NO_EVIDENCE, "1 candidate(s) failed sanity checks", False, True),  # a rule can admit it
            (DecisionStatus.DISABLED, "kept Plex's own marker", False, True),  # undecided, left to the server
            (DecisionStatus.DISABLED, "detection off", False, False),
        ],
        ids=["decided", "locked", "needs-review", "no-evidence-insane", "kept-own", "detection-off"],
    )
    def test_a_type_with_a_stored_answer_decided_under_older_rules(self, store, status, reason, locked, listed):
        rec = self._with_candidate(store)
        marker = Marker(T.INTRO, 20_000, 80_000, ("skipdb",), locked=locked)
        decided = status is DecisionStatus.DECIDED
        store.save_decisions(
            rec.id,
            {T.INTRO: TypeDecision(T.INTRO, status, marker if decided else None, None if decided else marker, reason)},
            settings_fingerprint="f",
        )
        if locked:
            store.lock_marker(rec.id, marker)

        assert versions.files_to_read_again(store, _settings()) == (self.DUE if listed else {})

    def test_a_type_with_no_stored_answer_of_its_type_isnt_listed(self, store):
        # Nothing stored can decide it differently: only a new answer can.
        rec = self._with_candidate(store, T.CREDITS)
        _undecided(store, rec, T.INTRO, DecisionStatus.NO_EVIDENCE)

        assert versions.files_to_read_again(store, _settings()) == {}

    @pytest.mark.parametrize(
        ("recorded", "listed"), [(None, True), (DECIDE_RULES_VERSION - 1, True), (DECIDE_RULES_VERSION, False)]
    )
    def test_a_file_decided_under_todays_rules_isnt_listed(self, store, recorded, listed):
        rec = self._with_candidate(store)
        _undecided(store, rec, T.INTRO, DecisionStatus.NEEDS_REVIEW)
        if recorded is not None:
            store.record_version_reruns([(self.PATH, DECIDE_RULES, recorded)])

        assert versions.files_to_read_again(store, _settings()) == (self.DUE if listed else {})

    def test_a_file_marked_missing_from_disk_isnt_listed(self, store):
        rec = self._with_candidate(store)
        _undecided(store, rec, T.INTRO, DecisionStatus.NEEDS_REVIEW)
        assert store.mark_missing(rec)

        assert versions.files_to_read_again(store, _settings()) == {}

    def test_listed_whatever_sources_are_on(self, store):
        # Every run decides: the rules apply to whatever answers are stored.
        rec = self._with_candidate(store)
        _undecided(store, rec, T.INTRO, DecisionStatus.NEEDS_REVIEW)

        assert versions.files_to_read_again(store, _settings(off=[s["id"] for s in ALL_SOURCES_ON])) == self.DUE

    def test_listed_beside_an_older_detector_answer_once_for_both(self, store):
        rec = _credits_text_file(store, self.PATH)
        store.replace_evidence(
            rec.id,
            Source.CHAPTERS,
            [Candidate(T.CREDITS, 1_200_000, None, Source.CHAPTERS)],
            version=CHAPTER_RULES_VERSION,
        )

        assert versions.files_to_read_again(store, _settings()) == {
            self.PATH: {"credits_text": CREDITS_TEXT_VERSION, DECIDE_RULES: DECIDE_RULES_VERSION}
        }


class TestOneVersionPlexItemsShowingOtherTimes:
    """Until 2026-09-25 a one-version Plex item kept times within 2 s of a moved decision (``publishers.base``)."""

    def _published(self, store, path, shown, decided, files):
        rec = _file(store, path)
        _decided(store, rec, T.INTRO, ("introdb", "season_audio"))
        store.save_decisions(
            rec.id,
            {T.INTRO: TypeDecision(T.INTRO, DecisionStatus.DECIDED, decided, None, "x")},
            settings_fingerprint="f",
        )
        store.set_publish_state(rec.id, "plex-1", item_id="7", markers=[shown], status="written")
        store.set_item_publish_state("plex-1", "7", [shown], "written", item_files=files)
        return rec

    SHOWN = Marker(T.INTRO, 6_000, 113_000, ("introdb", "season_audio"))
    DECIDED = Marker(T.INTRO, 6_000, 112_075, ("introdb", "season_audio"))

    @pytest.mark.parametrize(
        ("shown", "files", "listed"),
        [
            (SHOWN, ("/plex/a.mkv",), True),
            (SHOWN, ("/plex/a.mkv", "/plex/b.mkv"), False),  # another version: the agreement keeps it on purpose
            (DECIDED, ("/plex/a.mkv",), False),
            (SHOWN, None, False),  # versions not recorded: the next run records them with a write anyway
        ],
        ids=["one-version-other-times", "two-versions", "same-times", "versions-unknown"],
    )
    def test_listed_to_be_published_again(self, store, shown, files, listed):
        self._published(store, "/tv/GoT/S03/e4.mkv", shown, self.DECIDED, files)

        expected = {"/tv/GoT/S03/e4.mkv": {versions.PUBLISHED_TIMES: versions.PUBLISHED_TIMES_VERSION}}
        assert versions.files_to_read_again(store, _settings()) == (expected if listed else {})

    def test_taken_once(self, store):
        self._published(store, "/tv/GoT/S03/e4.mkv", self.SHOWN, self.DECIDED, ("/plex/a.mkv",))
        store.record_version_reruns(
            [("/tv/GoT/S03/e4.mkv", versions.PUBLISHED_TIMES, versions.PUBLISHED_TIMES_VERSION)]
        )

        assert versions.files_to_read_again(store, _settings()) == {}


class TestNextBatch:
    @pytest.fixture
    def library(self, tmp_path, store):
        for show, episode in [("B", 2), ("A", 2), ("B", 1), ("A", 1), ("A", 3)]:
            folder = tmp_path / "tv" / show / "S01"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"e{episode}.mkv"
            path.write_bytes(b"x")
            _credits_text_file(store, str(path))
        return tmp_path / "tv"

    def _names(self, batch, library):
        return [os.path.relpath(p, library) for p in batch]

    def test_a_batch_is_the_first_files_by_folder_then_path_with_the_versions_they_are_read_for(self, store, library):
        batch = versions.next_batch(store, _settings(), [], limit=3)

        assert self._names(batch, library) == ["A/S01/e1.mkv", "A/S01/e2.mkv", "A/S01/e3.mkv"]
        assert set(map(str, batch.values())) == {str({"credits_text": CREDITS_TEXT_VERSION})}

    def test_a_batch_taken_is_never_listed_again_for_the_same_versions(self, store, library):
        first = versions.next_batch(store, _settings(), [], limit=3)
        assert versions.next_batch(store, _settings(), [], limit=3) == first  # listing alone takes nothing

        versions.record_taken(store, first)
        second = versions.next_batch(store, _settings(), [], limit=3)
        versions.record_taken(store, second)

        assert self._names(second, library) == ["B/S01/e1.mkv", "B/S01/e2.mkv"]
        assert versions.next_batch(store, _settings(), [], limit=3) == {}
        assert store.version_rerun(store.get_file(next(iter(first))).id, "credits_text") == CREDITS_TEXT_VERSION

    def test_a_file_not_on_disk_now_is_left_out_and_stays_listed(self, store, library):
        gone = str(library / "A" / "S01" / "e1.mkv")
        os.unlink(gone)

        batch = versions.next_batch(store, _settings(), [], limit=10)
        versions.record_taken(store, batch)

        assert gone not in batch and len(batch) == 4
        assert store.version_rerun(store.get_file(gone).id, "credits_text") is None
        assert gone in versions.files_to_read_again(store, _settings())

    def test_a_file_not_on_disk_is_checked_for_being_gone_from_it(self, store, library, monkeypatch):
        checked = []
        monkeypatch.setattr(versions, "mark_missing_files", lambda _s, paths, configs: checked.append((paths, configs)))
        gone = str(library / "B" / "S01" / "e2.mkv")
        os.unlink(gone)

        versions.next_batch(store, _settings(), ["configs"], limit=10)

        assert checked == [([gone], ["configs"])]

    def test_only_as_many_files_as_the_batch_takes_are_looked_for_on_disk(self, store, library, monkeypatch):
        looked = []
        real = versions.files_on_disk
        monkeypatch.setattr(versions, "files_on_disk", lambda paths, **kw: looked.append(kw) or real(paths, **kw))

        versions.next_batch(store, _settings(), [], limit=2)

        assert looked == [{"limit": 2}]
