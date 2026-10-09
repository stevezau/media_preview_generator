"""The detectors and readers whose stored answers carry a version (``markers.versions``)."""

from __future__ import annotations

from media_preview_generator.markers import versions
from media_preview_generator.markers.audio import end_picture
from media_preview_generator.markers.audio.season import SEASON_AUDIO_ANSWER_VERSION, SEASON_AUDIO_VERSION
from media_preview_generator.markers.credits.detector import CREDITS_TEXT_VERSION
from media_preview_generator.markers.models import SERVER_SOURCES, MarkerType, Source
from media_preview_generator.markers.pipeline import PARSER_VERSIONS
from media_preview_generator.markers.sources.chapters import CHAPTER_RULES_VERSION
from media_preview_generator.markers.sources.server_markers import READER_VERSION

T = MarkerType


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
        # The check is part of season audio's answer: its first version adds nothing, so today's answers
        # stay current, and a new check makes every season audio answer older.
        assert SEASON_AUDIO_ANSWER_VERSION == SEASON_AUDIO_VERSION + (end_picture.CHECK_VERSION - 1) * 1_000
        # Season audio v10 (v9 speed by ear, v10 a season on every disk and the picking rules) with
        # check 4 (2 the one scaler, 3 the end card, 4 a flat frame beside one that isn't compared by correlation).
        assert (SEASON_AUDIO_VERSION, end_picture.CHECK_VERSION, SEASON_AUDIO_ANSWER_VERSION) == (10, 4, 3010)
