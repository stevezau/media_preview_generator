"""Tests for media_preview_generator.markers.sources.chapters (spec §5.1)."""

import pytest

from media_preview_generator.markers.models import Candidate, MarkerType, Source
from media_preview_generator.markers.probe import Chapter, MediaProbe
from media_preview_generator.markers.sources.chapters import chapter_candidates, classify_chapter_title

T = MarkerType


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Intro", T.INTRO),
        ("intro", T.INTRO),
        (" Introduction ", T.INTRO),
        ("Opening", T.INTRO),
        ("Opening Credits", T.INTRO),
        ("Opening Titles", T.INTRO),
        ("Title Sequence", T.INTRO),
        ("Main Titles", T.INTRO),
        ("Main Title", T.INTRO),
        ("OP", T.INTRO),
        ("Theme Song", T.INTRO),
        ("End Credits", T.CREDITS),
        ("Ending Credits", T.CREDITS),
        ("Closing Credits", T.CREDITS),
        ("Credits", T.CREDITS),
        ("End Titles", T.CREDITS),
        ("Outro", T.CREDITS),
        ("ED", T.CREDITS),
        ("Recap", T.RECAP),
        ("Previously", T.RECAP),
        ("Previously On", T.RECAP),
        ("Previously on Lost", T.RECAP),
        ("Story So Far", T.RECAP),
        ("Preview", T.PREVIEW),
        ("Next Episode", T.PREVIEW),
        ("Next Time", T.PREVIEW),
        ("Next Episode Preview", T.PREVIEW),
        ("Next Time On Naruto", T.PREVIEW),
        ("Theme", T.INTRO),
        ("Opening Title", T.INTRO),
        ("End Title", T.CREDITS),
        ("Opening  Credits", T.INTRO),  # double space
        ("\ufeffIntro", T.INTRO),  # leading BOM
        ("Chapter 1", None),
        ("Scene 2", None),
        ("Part 01", None),
        ("", None),
        ("End", None),
        ("Ending", None),
        ("The Opening Night", None),
        ("Credits Roll Party", None),
        ("00:00:00.000", None),
        ("Ed", None),  # a character named "Ed" must not match "ED"
        ("Op", None),  # symmetric case for "OP"
        ("Previously Onward", None),  # "on" must be a whole word, not a prefix
    ],
)
def test_classify_matrix(title, expected):
    assert classify_chapter_title(title) is expected


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        # German
        ("Vorspann", T.INTRO),
        ("VORSPANN", T.INTRO),
        ("Abspann", T.CREDITS),
        # French -- accent optional, some muxers drop it
        ("Générique", T.INTRO),
        ("Generique", T.INTRO),
        ("Générique de fin", T.CREDITS),
        ("Generique de fin", T.CREDITS),
        # Spanish -- "Intro"/"Credits" already match via the English patterns
        ("Cabecera", T.INTRO),
        ("Créditos", T.CREDITS),
        ("Creditos", T.CREDITS),
        # Italian
        ("Sigla", T.INTRO),
        ("Titoli di coda", T.CREDITS),
        # Portuguese
        ("Abertura", T.INTRO),
        ("Créditos finais", T.CREDITS),
        ("Creditos finais", T.CREDITS),
        # Dutch
        ("Aftiteling", T.CREDITS),
        # Whole-title matches only -- a longer title containing the word is not a hit.
        ("Générique du film", None),
        ("La Sigla", None),
        ("Cabeceras", None),
        ("Vorspannmusik", None),
        ("Aftiteling Muziek", None),
    ],
)
def test_classify_non_english_names(title, expected):
    assert classify_chapter_title(title) is expected


def test_candidates_use_chapter_bounds_and_skip_unnamed():
    probe = MediaProbe(
        duration_ms=1_680_709,
        chapters=(
            Chapter(0, 67_500, "Part 01"),
            Chapter(67_500, 86_250, "Intro"),
            Chapter(86_250, 1_524_450, "Part 02"),
            Chapter(1_524_450, None, "Credits"),
        ),
    )
    assert chapter_candidates(probe) == [
        Candidate(T.INTRO, 67_500, 86_250, Source.CHAPTERS, origin="Intro"),
        Candidate(T.CREDITS, 1_524_450, None, Source.CHAPTERS, origin="Credits"),
    ]


def test_no_chapters():
    assert chapter_candidates(MediaProbe(duration_ms=1000, chapters=())) == []


def test_candidate_origin_is_stripped_of_surrounding_whitespace():
    probe = MediaProbe(duration_ms=100_000, chapters=(Chapter(0, 5_000, "  Intro  "),))
    assert chapter_candidates(probe)[0].origin == "Intro"


def test_candidate_origin_strips_leading_bom():
    probe = MediaProbe(duration_ms=100_000, chapters=(Chapter(0, 5_000, "\ufeffCredits"),))
    assert chapter_candidates(probe)[0].origin == "Credits"


# --- A generic "Intro" next to a specific opening chapter is the cold open, not the theme. ---
# Real-library evidence: Mushoku Tensei S01E06/07/08 (Intro + OP), JJK S02E08/E13 (Intro + Opening).


def test_generic_intro_dropped_when_op_chapter_present():
    probe = MediaProbe(
        duration_ms=2_000_000,
        chapters=(Chapter(0, 45_045, "Intro"), Chapter(45_045, 135_010, "OP")),
    )
    assert chapter_candidates(probe) == [Candidate(T.INTRO, 45_045, 135_010, Source.CHAPTERS, origin="OP")]


def test_generic_introduction_dropped_when_op_chapter_present():
    probe = MediaProbe(
        duration_ms=2_000_000,
        chapters=(Chapter(0, 45_045, "Introduction"), Chapter(45_045, 135_010, "OP")),
    )
    assert chapter_candidates(probe) == [Candidate(T.INTRO, 45_045, 135_010, Source.CHAPTERS, origin="OP")]


def test_generic_intro_uppercase_dropped_when_op_chapter_present():
    probe = MediaProbe(
        duration_ms=2_000_000,
        chapters=(Chapter(0, 45_045, "INTRO"), Chapter(45_045, 135_010, "OP")),
    )
    assert chapter_candidates(probe) == [Candidate(T.INTRO, 45_045, 135_010, Source.CHAPTERS, origin="OP")]


def test_generic_intro_dropped_when_opening_chapter_present():
    probe = MediaProbe(
        duration_ms=2_000_000,
        chapters=(Chapter(0, 163_041, "Intro"), Chapter(163_041, 253_000, "Opening")),
    )
    assert chapter_candidates(probe) == [Candidate(T.INTRO, 163_041, 253_000, Source.CHAPTERS, origin="Opening")]


def test_generic_intro_kept_when_alone():
    probe = MediaProbe(duration_ms=1_000_000, chapters=(Chapter(0, 90_000, "Intro"),))
    assert chapter_candidates(probe) == [Candidate(T.INTRO, 0, 90_000, Source.CHAPTERS, origin="Intro")]


def test_generic_intro_and_credits_both_kept():
    probe = MediaProbe(
        duration_ms=1_000_000,
        chapters=(Chapter(0, 90_000, "Intro"), Chapter(900_000, None, "Credits")),
    )
    assert chapter_candidates(probe) == [
        Candidate(T.INTRO, 0, 90_000, Source.CHAPTERS, origin="Intro"),
        Candidate(T.CREDITS, 900_000, None, Source.CHAPTERS, origin="Credits"),
    ]


# --- A chapter's end clamps to the next chapter's start (spec §5.1). ---
# Real-library evidence: Desperate Housewives S01E07/E11/E16 "Previously On" chapters overrun into
# the next chapter (their own end_time is wrong / far too late).


def test_chapter_end_clamped_to_next_chapter_start():
    probe = MediaProbe(
        duration_ms=5_000_000,
        chapters=(Chapter(0, 2_603_000, "Previously On"), Chapter(30_000, 60_000, "Part 1")),
    )
    assert chapter_candidates(probe) == [Candidate(T.RECAP, 0, 30_000, Source.CHAPTERS, origin="Previously On")]


def test_equal_starts_do_not_clamp_each_other():
    # "Previously On" listed before its same-start sibling: a >= (instead of >) bug in the clamp
    # would wrongly treat "Studio Logo"'s start as the clamp target and zero out this chapter.
    probe = MediaProbe(
        duration_ms=5_000_000,
        chapters=(
            Chapter(0, 2_603_000, "Previously On"),
            Chapter(0, 5_000, "Studio Logo"),
            Chapter(30_000, 60_000, "Part 1"),
        ),
    )
    assert chapter_candidates(probe) == [Candidate(T.RECAP, 0, 30_000, Source.CHAPTERS, origin="Previously On")]


def test_last_chapter_with_none_end_stays_none():
    probe = MediaProbe(
        duration_ms=5_000_000,
        chapters=(Chapter(0, 30_000, "Recap"), Chapter(30_000, None, "Opening Credits")),
    )
    assert chapter_candidates(probe) == [
        Candidate(T.RECAP, 0, 30_000, Source.CHAPTERS, origin="Recap"),
        Candidate(T.INTRO, 30_000, None, Source.CHAPTERS, origin="Opening Credits"),
    ]


def test_open_ended_chapter_clamped_to_next_chapter_start():
    # An open (None) end must still clamp to the next chapter's start, not stay open.
    probe = MediaProbe(
        duration_ms=5_000_000,
        chapters=(Chapter(0, None, "Recap"), Chapter(30_000, 60_000, "Part 1")),
    )
    assert chapter_candidates(probe) == [Candidate(T.RECAP, 0, 30_000, Source.CHAPTERS, origin="Recap")]


def test_unsorted_chapter_input_still_clamps_correctly():
    probe = MediaProbe(
        duration_ms=5_000_000,
        chapters=(Chapter(30_000, 60_000, "Part 1"), Chapter(0, 2_603_000, "Previously On")),
    )
    assert chapter_candidates(probe) == [Candidate(T.RECAP, 0, 30_000, Source.CHAPTERS, origin="Previously On")]
