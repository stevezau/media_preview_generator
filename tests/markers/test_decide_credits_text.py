"""Credits text (spec §5.4) in the decision rules (§5.5): every cell where it meets chapters, SkipDB, markers already on
servers, the publish setting, sanity bounds and the preview overlap check. The owner's answers of 2026-09-16 are pinned:
Q1 (it publishes alone at Medium), Q2 (it and a server's own marker are independent), Q3 (its end, when a scene follows
the roll, is the safer end of any cluster it confirms)."""

from __future__ import annotations

from itertools import permutations

import pytest

from media_preview_generator.markers.decide import DecisionContext, DecisionStatus, decide
from media_preview_generator.markers.models import Candidate, MarkerType, Source

T = MarkerType
MOVIE_MS = 6_000_000
EPISODE_MS = 1_320_000
ORDER = ("chapters", "theintrodb", "introdb", "skipdb", "season_audio", "season_audio_previous", "credits_text",
         "server_markers", "server_markers_imported")  # fmt: skip
SHORTENED = "; start shortened to the server's own marker (plex-1)"
TEXT_FIRST = ("chapters", "credits_text", "theintrodb", "introdb", "skipdb", "season_audio", "season_audio_previous",
              "server_markers", "server_markers_imported")  # fmt: skip
SERVER_FIRST = ("server_markers", "server_markers_imported", "chapters", "theintrodb", "introdb", "skipdb",
                "season_audio", "season_audio_previous", "credits_text")  # fmt: skip


def text(start_ms: int, end_ms: int | None = None) -> Candidate:
    return Candidate(T.CREDITS, start_ms, end_ms, Source.CREDITS_TEXT)


def skipdb(start_ms: int, end_ms: int | None = None) -> Candidate:
    return Candidate(T.CREDITS, start_ms, end_ms, Source.SKIPDB)


def introdb(start_ms: int, end_ms: int | None = None) -> Candidate:
    return Candidate(T.CREDITS, start_ms, end_ms, Source.INTRODB)


def imported(start_ms: int, copied_from: str = "") -> Candidate:
    return Candidate(T.CREDITS, start_ms, None, Source.SERVER_MARKERS_IMPORTED, 1.0, "jellyfin-1", copied_from)


def plex(start_ms: int, end_ms: int | None = MOVIE_MS, origin: str = "plex-1") -> Candidate:
    return Candidate(T.CREDITS, start_ms, end_ms, Source.SERVER_MARKERS, 1.0, origin)


def chapter(start_ms: int) -> Candidate:
    return Candidate(T.CREDITS, start_ms, None, Source.CHAPTERS, 1.0, "End Credits")


def credits(candidates, *, level="high", duration=MOVIE_MS, is_movie=True, order=ORDER, types=(T.CREDITS,)):
    ctx = DecisionContext(duration, is_movie, level, frozenset(types), order)
    return decide(list(candidates), ctx, {})


class TestAlone:
    def test_high_needs_a_second_source(self):
        # "high" is the evaluation harness's level only: the app decides at "medium" (2026-09-24).
        d = credits([text(5_700_000)])[T.CREDITS]
        why = 'only on-screen text found the credits; at "high" a second source must agree'
        assert (d.status, d.reason, d.marker) == (DecisionStatus.NEEDS_REVIEW, why, None)
        assert (d.proposed.start_ms, d.proposed.end_ms, d.proposed.decided_by) == (
            5_700_000,
            MOVIE_MS,
            ("credits_text",),
        )

    def test_medium_publishes_it_alone_q1(self):
        d = credits([text(5_700_000)], level="medium")[T.CREDITS]
        assert (d.status, d.reason) == (DecisionStatus.DECIDED, "single source (credits_text)")
        assert (d.marker.start_ms, d.marker.end_ms, d.marker.decided_by) == (5_700_000, MOVIE_MS, ("credits_text",))

    @pytest.mark.parametrize("level", ["high", "medium"])
    def test_an_episode_alone_follows_the_same_rule(self, level):
        d = credits([text(1_250_000)], level=level, duration=EPISODE_MS, is_movie=False)[T.CREDITS]
        if level == "medium":
            assert (d.status, d.proposed) == (DecisionStatus.DECIDED, None)
            shown = d.marker
        else:
            assert (d.status, d.marker) == (DecisionStatus.NEEDS_REVIEW, None)
            shown = d.proposed
        assert (shown.start_ms, shown.end_ms, shown.decided_by) == (1_250_000, EPISODE_MS, ("credits_text",))


class TestSanity:
    @pytest.mark.parametrize(
        ("start_ms", "duration", "is_movie"),
        [
            (5_000_000, MOVIE_MS, True),     # more than 900 s before the end of a movie
            (4_400_000, MOVIE_MS, False),    # before the last 25 %
            (900_000, EPISODE_MS, False),    # an episode's credits at 68 %
            (5_998_000, MOVIE_MS, True),     # shorter than 3 s
        ],
    )  # fmt: skip
    def test_fails_sanity(self, start_ms, duration, is_movie):
        d = credits([text(start_ms)], level="medium", duration=duration, is_movie=is_movie)[T.CREDITS]
        assert (d.status, d.reason) == (DecisionStatus.NO_EVIDENCE, "1 candidate(s) failed sanity checks")
        assert (d.marker, d.proposed) == (None, None)


class TestWithSkipDb:
    def test_agreeing_within_10_s_decides_with_the_first_source_in_order(self):
        d = credits([text(5_700_000), skipdb(5_695_000, 5_990_000)])[T.CREDITS]
        assert d.status is DecisionStatus.DECIDED
        assert (d.marker.start_ms, d.marker.end_ms, d.marker.decided_by) == (
            5_695_000,
            5_990_000,
            ("skipdb", "credits_text"),
        )

    def test_the_users_order_picks_whose_start_is_published(self):
        d = credits([text(5_700_000), skipdb(5_695_000, 5_990_000)], order=TEXT_FIRST)[T.CREDITS]
        assert (d.marker.start_ms, d.marker.end_ms, d.marker.decided_by) == (
            5_700_000,
            5_990_000,
            ("credits_text", "skipdb"),
        )

    @pytest.mark.parametrize("offset_ms", [-10_000, 10_000, -10_001, 10_001])
    def test_the_10_s_start_tolerance_is_inclusive(self, offset_ms):
        start = 5_700_000 + offset_ms
        d = credits([text(5_700_000), skipdb(start, 5_990_000)])[T.CREDITS]
        if abs(offset_ms) <= 10_000:
            assert (d.status, d.reason) == (DecisionStatus.DECIDED, "sources agree: skipdb, credits_text")
            assert (d.marker.start_ms, d.marker.end_ms, d.marker.decided_by) == (
                start,
                5_990_000,
                ("skipdb", "credits_text"),
            )
        else:
            assert (d.status, d.reason, d.marker) == (
                DecisionStatus.NEEDS_REVIEW,
                "sources disagree: credits_text, skipdb",
                None,
            )

    @pytest.mark.parametrize(
        ("order", "proposed"),
        [(ORDER, (5_730_000, MOVIE_MS, ("skipdb",))), (TEXT_FIRST, (5_700_000, MOVIE_MS, ("credits_text",)))],
        ids=["skipdb-first", "text-first"],
    )
    @pytest.mark.parametrize("level", ["high", "medium"])
    def test_disagreeing_goes_to_review(self, level, order, proposed):
        d = credits([text(5_700_000), skipdb(5_730_000)], level=level, order=order)[T.CREDITS]
        assert (d.status, d.reason, d.marker) == (
            DecisionStatus.NEEDS_REVIEW,
            "sources disagree: credits_text, skipdb",
            None,
        )
        # The Inspector's proposal is the best-ranked source's answer in the user's order.
        assert (d.proposed.start_ms, d.proposed.end_ms, d.proposed.decided_by) == proposed


class TestWithIntroDb:
    """IntroDB is on by default and ranks above credits text in the default order."""

    @pytest.mark.parametrize("level", ["high", "medium"])
    def test_agreeing_publishes_introdbs_start_with_the_texts_end(self, level):
        d = credits([introdb(5_708_000), text(5_700_000, 5_900_000)], level=level)[T.CREDITS]
        assert (d.status, d.reason) == (DecisionStatus.DECIDED, "sources agree: introdb, credits_text")
        assert (d.marker.start_ms, d.marker.end_ms, d.marker.decided_by) == (
            5_708_000,
            5_900_000,
            ("introdb", "credits_text"),
        )

    @pytest.mark.parametrize("online", [Source.INTRODB, Source.THEINTRODB])
    def test_a_disagreeing_online_answer_stops_text_publishing_alone_at_medium(self, online):
        answer = Candidate(T.CREDITS, 5_730_000, None, online)
        d = credits([text(5_700_000), answer], level="medium")[T.CREDITS]
        assert (d.status, d.reason, d.marker) == (
            DecisionStatus.NEEDS_REVIEW,
            "sources disagree: credits_text, introdb/theintrodb",
            None,
        )
        assert (d.proposed.start_ms, d.proposed.end_ms, d.proposed.decided_by) == (5_730_000, MOVIE_MS, (online.value,))


class TestWithServerMarkers:
    @pytest.mark.parametrize("level", ["high", "medium"])
    def test_a_servers_own_marker_agreeing_decides_but_never_supplies_the_start_q2(self, level):
        d = credits([text(5_700_000), plex(5_705_000)], level=level)[T.CREDITS]
        assert d.status is DecisionStatus.DECIDED
        assert (d.marker.start_ms, d.marker.end_ms, d.marker.decided_by) == (
            5_700_000,
            MOVIE_MS,
            ("credits_text", "server_markers"),
        )
        assert d.reason == "sources agree: credits_text, server_markers"

    @pytest.mark.parametrize("server_source", [Source.SERVER_MARKERS, Source.SERVER_MARKERS_IMPORTED])
    def test_a_server_ranked_above_credits_text_still_never_supplies_the_start_q2(self, server_source):
        server = Candidate(T.CREDITS, 5_705_000, None, server_source, 1.0, "server-1")
        d = credits([text(5_700_000), server], order=SERVER_FIRST)[T.CREDITS]
        assert d.status is DecisionStatus.DECIDED
        assert (d.marker.start_ms, d.marker.end_ms, d.marker.decided_by) == (
            5_700_000,
            MOVIE_MS,
            (server_source.value, "credits_text"),
        )

    def test_a_servers_earlier_credits_end_shortens_the_skip(self):
        d = credits([text(5_700_000), plex(5_705_000, 5_950_000)])[T.CREDITS]
        assert d.status is DecisionStatus.DECIDED
        assert (d.marker.start_ms, d.marker.end_ms, d.marker.decided_by) == (
            5_700_000,
            5_950_000,
            ("credits_text", "server_markers"),
        )

    @pytest.mark.parametrize("level", ["high", "medium"])
    def test_a_server_marker_25_s_later_contradicts_it(self, level):
        d = credits([text(5_700_000), plex(5_725_000)], level=level)[T.CREDITS]
        assert (d.status, d.reason) == (DecisionStatus.NEEDS_REVIEW, "sources disagree: credits_text, server_markers")
        assert d.marker is None
        assert (d.proposed.start_ms, d.proposed.end_ms, d.proposed.decided_by) == (
            5_700_000,
            MOVIE_MS,
            ("credits_text",),
        )

    def test_rule_7_moves_a_decided_start_to_the_servers_later_first_start(self):
        # Text + SkipDB decide at 5 700 s; the server's own credits start 40 s later: its start wins (a shorter skip).
        d = credits([text(5_700_000), skipdb(5_702_000), plex(5_740_000)], order=TEXT_FIRST)[T.CREDITS]
        assert d.status is DecisionStatus.DECIDED
        assert (d.marker.start_ms, d.marker.end_ms, d.marker.decided_by) == (
            5_740_000,
            MOVIE_MS,
            ("credits_text", "skipdb", "server_markers"),
        )
        assert d.reason == "sources agree: credits_text, skipdb; start shortened to the server's own marker (plex-1)"

    @pytest.mark.parametrize("copied_from", ["", "introdb", "skipdb", "aniskip"])
    def test_an_importer_plugins_markers_agree_like_any_independent_source(self, copied_from):
        d = credits([text(5_700_000), imported(5_702_000, copied_from)])[T.CREDITS]
        assert d.status is DecisionStatus.DECIDED
        assert (d.marker.start_ms, d.marker.end_ms, d.marker.decided_by) == (
            5_700_000,
            MOVIE_MS,
            ("credits_text", "server_markers_imported"),
        )

    @pytest.mark.parametrize("level", ["high", "medium"])
    def test_skipdb_and_a_skipdb_importers_copy_are_one_source_against_it(self, level):
        # Rule 8 (ruling 2026-09-16): the copy agreeing with SkipDB 1 s apart is SkipDB again, not a second opinion.
        d = credits([text(5_700_000), skipdb(5_730_000), imported(5_731_000, "skipdb")], level=level)[T.CREDITS]
        assert (d.status, d.reason, d.marker) == (
            DecisionStatus.NEEDS_REVIEW,
            "sources disagree: credits_text, skipdb",
            None,
        )

    @pytest.mark.parametrize(
        "server",
        [plex(4_000_000), plex(5_705_000, 5_706_000), imported(5_000_000)],
        ids=["before-the-last-quarter", "shorter-than-3-s", "imported-too-early"],
    )
    def test_a_server_marker_failing_sanity_leaves_text_alone_at_medium(self, server):
        d = credits([text(5_700_000), server], level="medium")[T.CREDITS]
        assert (d.status, d.reason) == (DecisionStatus.DECIDED, "single source (credits_text)")
        assert (d.marker.start_ms, d.marker.end_ms, d.marker.decided_by) == (5_700_000, MOVIE_MS, ("credits_text",))


class TestWithChapters:
    def test_an_agreeing_chapter_decides(self):
        d = credits([chapter(5_698_000), text(5_700_000)])[T.CREDITS]
        assert (d.status, d.reason, d.marker.start_ms, d.marker.end_ms, d.marker.decided_by) == (
            DecisionStatus.DECIDED,
            "chapters",
            5_698_000,
            MOVIE_MS,
            ("chapters",),
        )

    def test_credits_text_alone_never_overrides_a_chapter(self):
        d = credits([chapter(5_640_000), text(5_700_000)], level="medium")[T.CREDITS]
        assert (d.status, d.reason) == (DecisionStatus.DECIDED, "chapters")
        assert (d.marker.start_ms, d.marker.end_ms, d.marker.decided_by) == (5_640_000, MOVIE_MS, ("chapters",))

    @pytest.mark.parametrize(
        ("second", "decided_by"),
        [
            (skipdb(5_702_000), ("chapters", "skipdb", "credits_text")),
            (plex(5_703_000), ("chapters", "credits_text", "server_markers")),
        ],
        ids=["skipdb", "plex"],
    )
    @pytest.mark.parametrize("level", ["high", "medium"])
    def test_two_agreeing_sources_give_the_chapter_the_texts_earlier_end(self, level, second, decided_by):
        # Rule 3: with two independent sources agreeing on the chapter's start, the end takes their safer value.
        d = credits([chapter(5_698_000), text(5_700_000, 5_900_000), second], level=level)[T.CREDITS]
        assert (d.status, d.reason) == (DecisionStatus.DECIDED, "chapters")
        assert (d.marker.start_ms, d.marker.end_ms, d.marker.decided_by) == (5_698_000, 5_900_000, decided_by)

    def test_text_and_a_servers_marker_agreeing_against_the_chapter_send_it_to_review(self):
        d = credits([chapter(5_640_000), text(5_700_000), plex(5_703_000)])[T.CREDITS]
        assert (d.status, d.marker) == (DecisionStatus.NEEDS_REVIEW, None)
        assert d.reason == "chapters contradicted by agreeing sources: credits_text, server_markers"
        assert (d.proposed.start_ms, d.proposed.end_ms, d.proposed.decided_by) == (5_640_000, MOVIE_MS, ("chapters",))

    def test_text_and_skipdb_agreeing_against_the_chapter_send_it_to_review(self):
        d = credits([chapter(5_640_000), text(5_700_000), skipdb(5_698_000)])[T.CREDITS]
        assert (d.status, d.reason) == (
            DecisionStatus.NEEDS_REVIEW,
            "chapters contradicted by agreeing sources: skipdb, credits_text",
        )
        assert d.marker is None
        assert (d.proposed.start_ms, d.proposed.end_ms, d.proposed.decided_by) == (5_640_000, MOVIE_MS, ("chapters",))


class TestEndQ3:
    """Owner, Q3: a credits text candidate ends at the roll's last credit frame when more than 30 s follows it."""

    def test_alone_at_medium_the_skip_stops_before_the_scene(self):
        d = credits([text(5_700_000, 5_900_000)], level="medium")[T.CREDITS]
        assert (d.status, d.marker.start_ms, d.marker.end_ms, d.marker.decided_by) == (
            DecisionStatus.DECIDED,
            5_700_000,
            5_900_000,
            ("credits_text",),
        )

    def test_an_open_ended_roll_skips_to_the_end_of_the_file(self):
        d = credits([text(5_700_000)], level="medium")[T.CREDITS]
        assert d.status is DecisionStatus.DECIDED
        assert (d.marker.start_ms, d.marker.end_ms, d.marker.decided_by) == (5_700_000, MOVIE_MS, ("credits_text",))

    @pytest.mark.parametrize("skipdb_end", [5_990_000, None])
    def test_its_earlier_end_shortens_an_agreeing_skipdb_answer(self, skipdb_end):
        d = credits([text(5_700_000, 5_900_000), skipdb(5_695_000, skipdb_end)])[T.CREDITS]
        assert d.status is DecisionStatus.DECIDED
        assert (d.marker.start_ms, d.marker.end_ms, d.marker.decided_by) == (
            5_695_000,
            5_900_000,
            ("skipdb", "credits_text"),
        )

    def test_its_end_wins_over_a_servers_final_credits(self):
        d = credits([text(5_700_000, 5_900_000), plex(5_705_000)])[T.CREDITS]
        assert d.status is DecisionStatus.DECIDED
        assert (d.marker.start_ms, d.marker.end_ms, d.marker.decided_by) == (
            5_700_000,
            5_900_000,
            ("credits_text", "server_markers"),
        )

    def test_a_servers_earlier_end_still_shortens_it(self):
        d = credits([text(5_700_000, 5_950_000), plex(5_705_000, 5_900_000)])[T.CREDITS]
        assert d.status is DecisionStatus.DECIDED
        assert (d.marker.start_ms, d.marker.end_ms, d.marker.decided_by) == (
            5_700_000,
            5_900_000,
            ("credits_text", "server_markers"),
        )

    def test_a_chapter_decision_keeps_the_chapters_end_with_one_agreeing_source(self):
        # Rule 3 needs two agreeing independent sources to move a chapter's end: credits text as the only other source
        # leaves it (TestWithChapters has the two-source cells).
        d = credits([chapter(5_698_000), text(5_700_000, 5_900_000)], level="medium")[T.CREDITS]
        assert (d.status, d.reason) == (DecisionStatus.DECIDED, "chapters")
        assert (d.marker.start_ms, d.marker.end_ms, d.marker.decided_by) == (5_698_000, MOVIE_MS, ("chapters",))

    @pytest.mark.parametrize(
        ("text_end", "server_start", "marker", "reason"),
        [
            # the server's start is within 10 s of the text's end: no offer
            (5_900_000, 5_895_000, (5_702_000, 5_900_000, ("skipdb", "credits_text")), ""),
            # more than 10 s before it: the start moves
            (5_900_000, 5_889_000, (5_889_000, 5_900_000, ("skipdb", "credits_text", "server_markers")), SHORTENED),
            # an open-ended roll ends with the file, 105 s after the server's start
            (None, 5_895_000, (5_895_000, MOVIE_MS, ("skipdb", "credits_text", "server_markers")), SHORTENED),
        ],
        ids=["ended-5895", "ended-5889", "open-ended-5895"],
    )
    def test_rule_7_offers_only_starts_more_than_10_s_before_the_texts_end(
        self, text_end, server_start, marker, reason
    ):
        d = credits([text(5_700_000, text_end), skipdb(5_702_000), plex(server_start)])[T.CREDITS]
        assert (d.status, d.reason) == (DecisionStatus.DECIDED, "sources agree: skipdb, credits_text" + reason)
        assert (d.marker.start_ms, d.marker.end_ms, d.marker.decided_by) == marker

    def test_a_preview_after_the_credits_end_no_longer_overlaps(self):
        preview = Candidate(T.PREVIEW, 5_950_000, 5_990_000, Source.CHAPTERS, 1.0, "Preview")
        ended = credits([text(5_700_000, 5_900_000), skipdb(5_702_000), preview], types=(T.CREDITS, T.PREVIEW))
        c, p = ended[T.CREDITS], ended[T.PREVIEW]
        assert (c.status, c.marker.start_ms, c.marker.end_ms, c.marker.decided_by) == (
            DecisionStatus.DECIDED,
            5_702_000,
            5_900_000,
            ("skipdb", "credits_text"),
        )
        assert (p.status, p.marker.start_ms, p.marker.end_ms, p.marker.decided_by) == (
            DecisionStatus.DECIDED,
            5_950_000,
            5_990_000,
            ("chapters",),
        )
        open_ended = credits([text(5_700_000), skipdb(5_702_000), preview], types=(T.CREDITS, T.PREVIEW))
        c, p = open_ended[T.CREDITS], open_ended[T.PREVIEW]
        assert (c.status, c.marker.start_ms, c.marker.end_ms) == (DecisionStatus.DECIDED, 5_702_000, MOVIE_MS)
        assert (p.status, p.reason, p.marker) == (DecisionStatus.NEEDS_REVIEW, "preview overlaps credits", None)


def test_a_preview_overlapping_text_decided_credits_goes_to_review():
    preview = Candidate(T.PREVIEW, 5_750_000, 5_800_000, Source.CHAPTERS, 1.0, "Preview")
    out = credits([text(5_700_000), skipdb(5_702_000), preview], types=(T.CREDITS, T.PREVIEW))
    c, p = out[T.CREDITS], out[T.PREVIEW]
    assert (c.status, c.marker.start_ms, c.marker.end_ms, c.marker.decided_by) == (
        DecisionStatus.DECIDED,
        5_702_000,
        MOVIE_MS,
        ("skipdb", "credits_text"),
    )
    assert (p.status, p.reason, p.marker) == (DecisionStatus.NEEDS_REVIEW, "preview overlaps credits", None)
    assert (p.proposed.start_ms, p.proposed.end_ms) == (5_750_000, 5_800_000)


# Cells that collapse into others:
# - Two credits text candidates are one independence group: rule 6's self-agreement check (test_decide) covers them.
# - A locked user marker wins over every source the same way (rule 1; TestLockAndDisabled in test_decide).
# - Season audio never answers credits (spec §5.3: 54 % precision, rejected), so it never meets credits text.


@pytest.mark.parametrize(
    "candidates",
    [
        [text(5_700_000), skipdb(5_695_000, 5_990_000)],
        [text(5_700_000), plex(5_705_000, 5_950_000)],
        [chapter(5_640_000), text(5_700_000), plex(5_703_000)],
        [text(5_700_000), skipdb(5_702_000), plex(5_740_000)],
        [text(5_700_000, 5_950_000), skipdb(5_695_000, 5_990_000), plex(5_705_000, 5_900_000)],
        [text(5_700_000), skipdb(5_730_000), imported(5_731_000, "skipdb")],
        [chapter(5_698_000), text(5_700_000, 5_900_000), skipdb(5_702_000), plex(5_889_000)],
    ],
)
@pytest.mark.parametrize("level", ["high", "medium"])
def test_results_never_depend_on_input_order(candidates, level):
    results = {repr(credits(list(p), level=level)[T.CREDITS]) for p in permutations(candidates)}
    assert len(results) == 1
