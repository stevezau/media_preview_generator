"""The episode-only chapter names (spec §5.1): "Ending" is the anime ED, and only on an episode.

Measured in `docs/design/intro-credits/evidence/eval/phase4-chapters.md`: on the owner's library the
rule gains 280 anime credits and changes nothing on 11,919 non-anime TV episodes or 9,904 movies.
The one movie that carries a bare "Ending" chapter (a 1988 documentary whose last chapter is its final
scene) was frame-checked: taking it would skip the last 161 s of the film. That file is why the rule
is scoped to episodes and why "End" alone is still nothing anywhere.
"""

import pytest

from media_preview_generator.markers.models import Candidate, MarkerType, Source
from media_preview_generator.markers.probe import Chapter, MediaProbe
from media_preview_generator.markers.sources.chapters import (
    CHAPTER_RULES_VERSION,
    chapter_candidates,
    classify_chapter_title,
)

T = MarkerType

# Goblin Slayer S02E01, frame-checked: story to 1323 s, the ED credit roll 1323-1413 s, then the preview.
GOBLIN_SLAYER_S02E01 = (
    Chapter(0, 97_000, "Prologue"),
    Chapter(97_000, 187_000, "Opening"),
    Chapter(187_000, 687_000, "Part A"),
    Chapter(687_000, 1_323_000, "Part B"),
    Chapter(1_323_000, 1_413_000, "Ending"),
    Chapter(1_413_000, 1_422_000, "Preview"),
)
# Lets Get Lost (1988), frame-checked: "Ending" 6746-7187 s is the film's last scene, not a credit roll.
LETS_GET_LOST = (
    Chapter(0, 608_000, "Opening"),
    Chapter(608_000, 6_018_000, "Meeting Chet Baker"),
    Chapter(6_018_000, 6_746_000, "I Love You, Dad"),
    Chapter(6_746_000, 7_187_000, "Ending"),
)


@pytest.mark.parametrize(
    ("title", "on_episode", "elsewhere"),
    [
        ("Ending", T.CREDITS, None),
        ("ending", T.CREDITS, None),
        ("ENDING", T.CREDITS, None),
        (" Ending ", T.CREDITS, None),
        ("﻿Ending", T.CREDITS, None),
        # "End" alone: no anime file in the measurement used it, and four movies did (a 3 s card at the
        # very end of Blade II and Friends: The Reunion), so it stays a scene name on an episode too.
        ("End", None, None),
        ("The End", None, None),
        # Whole-title matches only, as everywhere else in this module.
        ("Ending Song", None, None),
        ("Ending Theme", None, None),
        ("Happy Ending", None, None),
        ("Endings", None, None),
        ("Ending: Koi no Yukue", None, None),
        # Already credits before this rule, on any kind of file.
        ("Ending Credits", T.CREDITS, T.CREDITS),
        ("End Credits", T.CREDITS, T.CREDITS),
        ("Credits", T.CREDITS, T.CREDITS),
        ("ED", T.CREDITS, T.CREDITS),
        # The episode scope adds names, it never removes or re-types one.
        ("Opening", T.INTRO, T.INTRO),
        ("OP", T.INTRO, T.INTRO),
        ("Intro", T.INTRO, T.INTRO),
        ("Preview", T.PREVIEW, T.PREVIEW),
        ("Recap", T.RECAP, T.RECAP),
        ("Part B", None, None),
        ("Ed", None, None),
    ],
)
def test_classify_matrix_by_kind(title, on_episode, elsewhere):
    assert classify_chapter_title(title, is_episode=True) is on_episode
    assert classify_chapter_title(title, is_episode=False) is elsewhere


def test_unknown_kind_is_not_an_episode():
    # A file whose own path names no season and episode must get the conservative answer, so the default
    # matters: it is what `classify_chapter_title("Ending")` returns with no flag at all.
    assert classify_chapter_title("Ending") is None
    assert chapter_candidates(MediaProbe(1_422_000, GOBLIN_SLAYER_S02E01)) == [
        Candidate(T.INTRO, 97_000, 187_000, Source.CHAPTERS, origin="Opening"),
        Candidate(T.PREVIEW, 1_413_000, 1_422_000, Source.CHAPTERS, origin="Preview"),
    ]


def test_ending_chapter_is_credits_on_an_episode():
    assert chapter_candidates(MediaProbe(1_422_000, GOBLIN_SLAYER_S02E01), is_episode=True) == [
        Candidate(T.INTRO, 97_000, 187_000, Source.CHAPTERS, origin="Opening"),
        Candidate(T.CREDITS, 1_323_000, 1_413_000, Source.CHAPTERS, origin="Ending"),
        Candidate(T.PREVIEW, 1_413_000, 1_422_000, Source.CHAPTERS, origin="Preview"),
    ]


def test_ending_chapter_is_not_credits_on_a_movie():
    # The film's "Opening" chapter is still an intro candidate here (§5.5 rule 2's 300 s cap drops it
    # later); what must not appear is a credits candidate from its last scene.
    assert chapter_candidates(MediaProbe(7_187_000, LETS_GET_LOST), is_episode=False) == [
        Candidate(T.INTRO, 0, 608_000, Source.CHAPTERS, origin="Opening")
    ]


def test_an_ending_chapter_end_clamps_to_the_next_chapter():
    # The ED's own end is often the file's end in the container; the preview after it is the real edge.
    chapters = (
        Chapter(1_323_000, 1_422_000, "Ending"),
        Chapter(1_413_000, 1_422_000, "Preview"),
    )
    assert chapter_candidates(MediaProbe(1_422_000, chapters), is_episode=True)[0] == Candidate(
        T.CREDITS, 1_323_000, 1_413_000, Source.CHAPTERS, origin="Ending"
    )


def test_an_ending_chapter_with_no_later_chapter_keeps_its_own_end():
    chapters = (Chapter(0, 1_323_000, "Part B"), Chapter(1_323_000, None, "Ending"))
    assert chapter_candidates(MediaProbe(1_422_000, chapters), is_episode=True) == [
        Candidate(T.CREDITS, 1_323_000, None, Source.CHAPTERS, origin="Ending")
    ]


def test_the_cold_open_rule_still_applies_beside_an_ending_chapter():
    # Both rules on one file: the generic "Intro" is the cold open (dropped for "OP"), "Ending" is the ED.
    chapters = (
        Chapter(0, 45_045, "Intro"),
        Chapter(45_045, 135_010, "OP"),
        Chapter(135_010, 1_300_000, "Part A"),
        Chapter(1_300_000, None, "Ending"),
    )
    assert chapter_candidates(MediaProbe(1_400_000, chapters), is_episode=True) == [
        Candidate(T.INTRO, 45_045, 135_010, Source.CHAPTERS, origin="OP"),
        Candidate(T.CREDITS, 1_300_000, None, Source.CHAPTERS, origin="Ending"),
    ]


def test_a_lone_generic_intro_still_decides_on_an_episode():
    # Measured and not taken: dropping these would lose 569 anime intros (and 857 non-anime TV ones) to
    # remove the 7 that Plex's own intro marker says are cold opens. See phase4-chapters.md.
    chapters = (Chapter(0, 90_000, "Intro"), Chapter(1_300_000, None, "Ending"))
    assert chapter_candidates(MediaProbe(1_400_000, chapters), is_episode=True) == [
        Candidate(T.INTRO, 0, 90_000, Source.CHAPTERS, origin="Intro"),
        Candidate(T.CREDITS, 1_300_000, None, Source.CHAPTERS, origin="Ending"),
    ]


def test_the_rules_version_was_bumped_for_the_ending_rule():
    # Without the bump the rule never reaches a file the app has already probed: nothing re-reads it.
    # `tests/markers/test_pipeline.py::TestRulesVersions` proves the re-probe itself.
    assert CHAPTER_RULES_VERSION >= 2
