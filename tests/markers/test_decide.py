"""Spec §5.5 decision rules as a full matrix."""

import itertools
import math
import random
from dataclasses import replace

import pytest

from media_preview_generator.markers import decide as decide_module
from media_preview_generator.markers.decide import (
    DecisionContext,
    DecisionStatus,
    TypeDecision,
    decide,
    intro_chapter_length_ms,
    intro_chapter_limit_ms,
    sanity_problem,
    shortened_by,
)
from media_preview_generator.markers.models import Candidate, Marker, MarkerType, Source

T = MarkerType
S = Source
DUR = 1_320_000  # 22:00 episode
SHORT_DUR = 240_000  # 4:00 episode -- big enough for a real intro, small enough that "near the end" is reachable
MOVIE_DUR = 6_000_000  # 100:00 movie
ORDER = (
    "chapters",
    "theintrodb",
    "introdb",
    "skipdb",
    "season_audio",
    "credits_text",
    "server_markers",
    "server_markers_imported",
)


def ctx(publish_when="high", is_movie=False, duration=DUR, types=(T.INTRO, T.CREDITS), order=ORDER):
    return DecisionContext(duration, is_movie, publish_when, frozenset(types), order)


def intro(src, start, end, origin=""):
    return Candidate(T.INTRO, start, end, src, origin=origin)


def credits(src, start, end=None, origin=""):
    return Candidate(T.CREDITS, start, end, src, origin=origin)


class TestLockAndDisabled:
    def test_locked_marker_wins_over_everything(self):
        locked = {T.INTRO: Marker(T.INTRO, 1000, 30000, ("user",), locked=True)}
        cands = [intro(S.CHAPTERS, 50000, 80000)]
        d = decide(cands, ctx(), locked)[T.INTRO]
        assert d.status is DecisionStatus.DECIDED and d.marker == locked[T.INTRO]

    def test_locked_marker_wins_even_when_its_type_is_disabled(self):
        locked = {T.INTRO: Marker(T.INTRO, 1000, 30000, ("user",), locked=True)}
        d = decide([], ctx(types=(T.CREDITS,)), locked)[T.INTRO]
        assert d.status is DecisionStatus.DECIDED and d.marker == locked[T.INTRO]

    def test_disabled_type_is_disabled_even_with_chapters(self):
        d = decide([intro(S.CHAPTERS, 0, 30000)], ctx(types=(T.CREDITS,)), {})[T.INTRO]
        assert d.status is DecisionStatus.DISABLED and d.marker is None

    def test_locked_value_of_wrong_type_is_ignored_not_treated_as_a_lock(self):
        # A corrupt/legacy value under the key must not crash or silently "decide" with garbage --
        # fall through to the normal candidate-based decision as if nothing were locked.
        locked = {T.INTRO: "not-a-marker"}
        d = decide([intro(S.CHAPTERS, 11000, 37000)], ctx(), locked)[T.INTRO]
        assert d.status is DecisionStatus.DECIDED
        assert d.marker.decided_by == ("chapters",)
        assert d.marker.start_ms == 11000

    def test_locked_value_with_mismatched_type_for_its_key_is_ignored(self):
        # The value under T.INTRO is itself a real Marker, just the wrong type -- must not be
        # treated as an intro lock either.
        locked = {T.INTRO: Marker(T.CREDITS, 1000, 30000, ("user",), locked=True)}
        d = decide([intro(S.CHAPTERS, 11_000, 37_000)], ctx(), locked)[T.INTRO]
        assert d.status is DecisionStatus.DECIDED
        assert d.marker.decided_by == ("chapters",)

    def test_all_four_types_always_present_in_result(self):
        assert set(decide([], ctx(), {})) == set(T)


class TestChapters:
    def test_chapter_alone_decides_at_high(self):
        d = decide([intro(S.CHAPTERS, 11000, 37000, "Title Sequence")], ctx(), {})[T.INTRO]
        assert d.status is DecisionStatus.DECIDED
        assert (d.marker.start_ms, d.marker.end_ms, d.marker.decided_by) == (11000, 37000, ("chapters",))

    def test_first_intro_chapter_and_last_credits_chapter(self):
        cands = [
            intro(S.CHAPTERS, 5000, 30000),
            intro(S.CHAPTERS, 60000, 90000),
            credits(S.CHAPTERS, 1_200_000, 1_250_000),
            credits(S.CHAPTERS, 1_290_000, None),
        ]
        out = decide(cands, ctx(), {})
        assert out[T.INTRO].marker.start_ms == 5000
        assert (out[T.CREDITS].marker.start_ms, out[T.CREDITS].marker.end_ms) == (1_290_000, DUR)

    def test_chapter_failing_sanity_is_not_used(self):
        # "Opening" scene chapter of 8 minutes is not an intro
        d = decide([intro(S.CHAPTERS, 0, 480_000)], ctx(), {})[T.INTRO]
        assert d.status is DecisionStatus.NO_EVIDENCE
        assert "sanity" in d.reason

    def test_intro_chapter_running_to_file_end_has_no_marker(self):
        # An "Intro" chapter from 0 with no end is a mis-detected scene, not a real intro -- must
        # fail sanity, not decide 0-to-end-of-file.
        d = decide([intro(S.CHAPTERS, 0, None)], ctx(duration=SHORT_DUR), {})[T.INTRO]
        assert d.status is DecisionStatus.NO_EVIDENCE and d.marker is None

    def test_a_later_chapter_agreeing_with_a_source_contradicts_the_first_chapter(self):
        # A second intro chapter that SkipDB agrees with puts the intro somewhere else: that is an agreeing
        # pair from two sources against the chosen chapter, so it goes to review.
        cands = [intro(S.CHAPTERS, 0, 30_000), intro(S.CHAPTERS, 60_000, 100_000), intro(S.SKIPDB, 60_000, 100_000)]
        d = decide(cands, ctx(), {})[T.INTRO]
        assert d.status is DecisionStatus.NEEDS_REVIEW and d.marker is None
        assert d.proposed == Marker(T.INTRO, 0, 30_000, ("chapters",))
        assert d.reason == "agreeing sources contradict the result: chapters, skipdb"

    def test_a_second_chapter_alone_does_not_contradict_the_first(self):
        cands = [intro(S.CHAPTERS, 0, 30_000), intro(S.CHAPTERS, 60_000, 100_000)]
        d = decide(cands, ctx(), {})[T.INTRO]
        assert (d.status, d.marker) == (DecisionStatus.DECIDED, Marker(T.INTRO, 0, 30_000, ("chapters",)))

    def test_chapter_within_tolerance_of_two_disagreeing_groups_is_accepted(self):
        # Groups ending at 95 s and 105 s disagree with each other, but the chapter at 100 s is within 5 s of both.
        cands = [
            intro(S.CHAPTERS, 70_000, 100_000),
            intro(S.SKIPDB, 70_000, 95_000),
            intro(S.SEASON_AUDIO, 70_000, 95_000),
            intro(S.CREDITS_TEXT, 70_000, 105_000),
            intro(S.THEINTRODB, 70_000, 105_000),
        ]
        d = decide(cands, ctx(), {})[T.INTRO]
        assert d.status is DecisionStatus.DECIDED and (d.marker.start_ms, d.marker.end_ms) == (70_000, 100_000)

    @pytest.mark.parametrize(
        ("chapter_start", "others", "marker"),
        [
            (
                10_000,
                [intro(S.SKIPDB, 80_000, 100_000), intro(S.SEASON_AUDIO, 80_000, 100_000)],
                Marker(T.INTRO, 80_000, 100_000, ("chapters", "skipdb", "season_audio")),
            ),
            # one agreeing source never changes chapters
            (10_000, [intro(S.SKIPDB, 80_000, 100_000)], Marker(T.INTRO, 10_000, 100_000, ("chapters",))),
            # markers from two servers are still one source
            (
                10_000,
                [
                    intro(S.SERVER_MARKERS, 80_000, 100_000, "plex-1"),
                    intro(S.SERVER_MARKERS, 80_000, 100_000, "emby-1"),
                ],
                Marker(T.INTRO, 10_000, 100_000, ("chapters",)),
            ),
            # SkipDB + server markers are two groups; only SkipDB supplies the start, the server
            # markers are credited as agreeing evidence
            (
                10_000,
                [
                    intro(S.SERVER_MARKERS, 80_000, 100_000, "plex-1"),
                    intro(S.SERVER_MARKERS, 80_000, 100_000, "emby-1"),
                    intro(S.SKIPDB, 80_000, 100_000),
                ],
                Marker(T.INTRO, 80_000, 100_000, ("chapters", "skipdb", "server_markers")),
            ),
            # a server marker's later start (90s) shortens the skip (rule 7: servers may shorten, never lengthen)
            (
                10_000,
                [intro(S.SERVER_MARKERS, 90_000, 100_000, "plex-1"), intro(S.SKIPDB, 80_000, 100_000)],
                Marker(T.INTRO, 90_000, 100_000, ("chapters", "skipdb", "server_markers")),
            ),
            # a server marker's earlier start (30s) never lengthens it: SkipDB's 80s stays
            (
                10_000,
                [intro(S.SERVER_MARKERS, 30_000, 100_000, "plex-1"), intro(S.SKIPDB, 80_000, 100_000)],
                Marker(T.INTRO, 80_000, 100_000, ("chapters", "skipdb", "server_markers")),
            ),
            # season_audio agrees on the end without supplying the start; it is still credited
            (
                10_000,
                [intro(S.SKIPDB, 80_000, 100_000), intro(S.SEASON_AUDIO, 60_000, 101_000)],
                Marker(T.INTRO, 80_000, 100_000, ("chapters", "skipdb", "season_audio")),
            ),
            # the chapter's own start (85s) is already the safer one
            (
                85_000,
                [intro(S.SKIPDB, 80_000, 100_000), intro(S.SEASON_AUDIO, 80_000, 100_000)],
                Marker(T.INTRO, 85_000, 100_000, ("chapters",)),
            ),
            # season_audio's end exactly 5s from the chapter's still agrees
            (
                10_000,
                [intro(S.SKIPDB, 80_000, 100_000), intro(S.SEASON_AUDIO, 80_000, 105_000)],
                Marker(T.INTRO, 80_000, 100_000, ("chapters", "skipdb", "season_audio")),
            ),
            # 1 ms further and only one source backs the chapter: its own start stays
            (
                10_000,
                [intro(S.SKIPDB, 80_000, 100_000), intro(S.SEASON_AUDIO, 80_000, 105_001)],
                Marker(T.INTRO, 10_000, 100_000, ("chapters",)),
            ),
            # an equal start is not safer, so nothing is replaced or credited
            (
                80_000,
                [intro(S.SKIPDB, 80_000, 100_000), intro(S.SEASON_AUDIO, 80_000, 100_000)],
                Marker(T.INTRO, 80_000, 100_000, ("chapters",)),
            ),
        ],
    )
    def test_agreeing_sources_give_a_chapter_a_safer_intro_start(self, chapter_start, others, marker):
        cands = [intro(S.CHAPTERS, chapter_start, 100_000), *others]
        for order in (cands, cands[::-1]):
            d = decide(order, ctx(), {})[T.INTRO]
            assert d.status is DecisionStatus.DECIDED
            assert (d.marker, d.proposed, d.reason) == (marker, None, "chapters")

    def test_agreeing_sources_give_a_chapter_an_earlier_credits_end(self):
        cands = [
            credits(S.CHAPTERS, 1_200_000),
            credits(S.SKIPDB, 1_205_000, 1_290_000),
            credits(S.SEASON_AUDIO, 1_195_000, 1_300_000),
        ]
        d = decide(cands, ctx(), {})[T.CREDITS]
        assert d.status is DecisionStatus.DECIDED
        assert d.marker == Marker(T.CREDITS, 1_200_000, 1_290_000, ("chapters", "skipdb", "season_audio"))
        assert (d.proposed, d.reason) == (None, "chapters")

    @pytest.mark.parametrize(
        ("server_end", "end"),
        [
            # Plex's non-final credits end before the post-credits scene: the skip stops there
            (1_230_000, 1_230_000),
            # Plex's final credits run to the end: nothing to shorten, SkipDB's end stays
            (None, 1_290_000),
        ],
    )
    def test_an_agreeing_server_marker_can_end_chapter_credits_earlier(self, server_end, end):
        cands = [
            credits(S.CHAPTERS, 1_200_000),
            credits(S.SKIPDB, 1_205_000, 1_290_000),
            credits(S.SERVER_MARKERS, 1_201_000, server_end, "plex-1"),
        ]
        for order in (cands, cands[::-1]):
            d = decide(order, ctx(), {})[T.CREDITS]
            assert d.status is DecisionStatus.DECIDED
            assert d.marker == Marker(T.CREDITS, 1_200_000, end, ("chapters", "skipdb", "server_markers"))
            assert (d.proposed, d.reason) == (None, "chapters")

    def test_chapter_with_a_safer_edge_failing_sanity_needs_review(self):
        # the agreeing sources' latest start (98s) and the chapter's end (100s) leave a 2s segment
        cands = [
            intro(S.CHAPTERS, 10_000, 100_000),
            intro(S.SKIPDB, 98_000, 101_000),
            intro(S.SEASON_AUDIO, 98_000, 102_000),
        ]
        d = decide(cands, ctx(), {})[T.INTRO]
        assert d.status is DecisionStatus.NEEDS_REVIEW
        assert d.marker is None
        assert d.proposed == Marker(T.INTRO, 10_000, 100_000, ("chapters",))
        assert d.reason == "chapters and agreeing sources disagree on the other edge"

    def test_chapters_survive_a_single_contradicting_source(self):
        # A lone disagreeing source is never enough to override chapters.
        cands = [intro(S.CHAPTERS, 11_000, 37_000), intro(S.THEINTRODB, 127_000, 157_000)]
        d = decide(cands, ctx(), {})[T.INTRO]
        assert d.status is DecisionStatus.DECIDED
        assert (d.marker.start_ms, d.marker.end_ms, d.marker.decided_by) == (11_000, 37_000, ("chapters",))

    def test_chapters_contradicted_by_an_agreeing_cluster_needs_review(self):
        cands = [
            intro(S.CHAPTERS, 11_000, 37_000),
            intro(S.THEINTRODB, 127_000, 157_000),
            intro(S.SEASON_AUDIO, 128_000, 158_000),
        ]
        d = decide(cands, ctx(), {})[T.INTRO]
        assert d.status is DecisionStatus.NEEDS_REVIEW
        assert d.marker is None
        assert d.proposed is not None
        assert (d.proposed.start_ms, d.proposed.end_ms, d.proposed.decided_by) == (11_000, 37_000, ("chapters",))
        assert "chapters contradicted by agreeing sources" in d.reason
        assert "theintrodb" in d.reason and "season_audio" in d.reason  # names the contradicting groups

    def test_chapters_contradicted_by_a_cluster_that_includes_a_server_marker(self):
        # A server marker may be one of the two groups in the contradicting cluster.
        cands = [
            intro(S.CHAPTERS, 11_000, 37_000),
            intro(S.THEINTRODB, 127_000, 157_000),
            intro(S.SERVER_MARKERS, 128_000, 158_000, "plex-1"),
        ]
        d = decide(cands, ctx(), {})[T.INTRO]
        assert d.status is DecisionStatus.NEEDS_REVIEW
        assert d.marker is None
        assert d.proposed is not None
        assert (d.proposed.start_ms, d.proposed.end_ms, d.proposed.decided_by) == (11_000, 37_000, ("chapters",))
        assert "chapters contradicted by agreeing sources" in d.reason
        assert "theintrodb" in d.reason and "server_markers" in d.reason

    @pytest.mark.parametrize("mtype", [T.INTRO, T.RECAP])
    @pytest.mark.parametrize("reverse", [False, True])
    def test_start_segment_chapters_tied_on_start_pick_the_earlier_end(self, mtype, reverse):
        cands = [Candidate(mtype, 0, 30_000, S.CHAPTERS), Candidate(mtype, 0, 90_000, S.CHAPTERS)]
        if reverse:
            cands.reverse()
        d = decide(cands, ctx(types=(mtype,)), {})[mtype]
        assert d.status is DecisionStatus.DECIDED
        assert d.marker == Marker(mtype, 0, 30_000, ("chapters",))
        assert d.proposed is None
        assert d.reason == "chapters"

    @pytest.mark.parametrize("mtype", [T.CREDITS, T.PREVIEW])
    @pytest.mark.parametrize("reverse", [False, True])
    def test_end_segment_chapters_tied_on_start_pick_the_earlier_end(self, mtype, reverse):
        cands = [Candidate(mtype, 1_300_000, 1_310_000, S.CHAPTERS), Candidate(mtype, 1_300_000, None, S.CHAPTERS)]
        if reverse:
            cands.reverse()
        d = decide(cands, ctx(types=(mtype,)), {})[mtype]
        assert d.status is DecisionStatus.DECIDED
        assert d.marker == Marker(mtype, 1_300_000, 1_310_000, ("chapters",))
        assert d.proposed is None
        assert d.reason == "chapters"

    def test_chapters_contradicted_by_an_agreeing_pair_without_a_bridge(self):
        cands = [
            credits(S.CHAPTERS, 1_200_000),
            credits(S.SKIPDB, 1_220_000),
            credits(S.SEASON_AUDIO, 1_220_000),
        ]
        d = decide(cands, ctx(), {})[T.CREDITS]
        assert d.status is DecisionStatus.NEEDS_REVIEW
        assert d.marker is None
        assert d.proposed == Marker(T.CREDITS, 1_200_000, DUR, ("chapters",))
        assert d.reason == "chapters contradicted by agreeing sources: skipdb, season_audio"

    def test_chapters_contradicted_by_an_agreeing_pair_behind_a_bridging_source(self):
        # TheIntroDB (10s after the chapter) agrees with both the chapter and the pair, so the
        # three non-chapter sources form one cluster whose winner, TheIntroDB, doesn't contradict
        # the chapter. SkipDB and season_audio still agree with each other 20s after it.
        cands = [
            credits(S.CHAPTERS, 1_200_000),
            credits(S.THEINTRODB, 1_210_000),
            credits(S.SKIPDB, 1_220_000),
            credits(S.SEASON_AUDIO, 1_220_000),
        ]
        d = decide(cands, ctx(), {})[T.CREDITS]
        assert d.status is DecisionStatus.NEEDS_REVIEW
        assert d.marker is None
        assert d.proposed == Marker(T.CREDITS, 1_200_000, DUR, ("chapters",))
        assert d.reason == "agreeing sources contradict the result: season_audio, skipdb"

    @pytest.mark.parametrize(
        ("pair_start", "expected"), [(1_210_000, DecisionStatus.DECIDED), (1_210_001, DecisionStatus.NEEDS_REVIEW)]
    )
    def test_contradicting_pair_must_be_beyond_the_tolerance_of_the_chapter(self, pair_start, expected):
        cands = [
            credits(S.CHAPTERS, 1_200_000),
            credits(S.THEINTRODB, 1_205_000),
            credits(S.SKIPDB, pair_start),
            credits(S.SEASON_AUDIO, pair_start),
        ]
        d = decide(cands, ctx(), {})[T.CREDITS]
        chapter_marker = Marker(T.CREDITS, 1_200_000, DUR, ("chapters",))
        assert d.status is expected
        if expected is DecisionStatus.DECIDED:
            assert (d.marker, d.proposed, d.reason) == (chapter_marker, None, "chapters")
        else:
            assert (d.marker, d.proposed) == (None, chapter_marker)
            assert d.reason == "agreeing sources contradict the result: season_audio, skipdb"

    @pytest.mark.parametrize(
        ("partner", "expected", "reason"),
        [
            (S.INTRODB, DecisionStatus.DECIDED, "chapters"),  # TheIntroDB + IntroDB are one source
            (
                S.SEASON_AUDIO,
                DecisionStatus.NEEDS_REVIEW,
                "agreeing sources contradict the result: introdb/theintrodb, season_audio",
            ),
            (
                S.SERVER_MARKERS,
                DecisionStatus.NEEDS_REVIEW,
                "agreeing sources contradict the result: introdb/theintrodb, server_markers",
            ),
        ],
    )
    def test_pair_contradicting_the_chapter_behind_a_bridge_needs_two_independent_groups(
        self, partner, expected, reason
    ):
        # SkipDB ranks first, so it wins the non-chapter cluster and sits 10s from the chapter; the
        # far pair is TheIntroDB plus a partner 20s after the chapter.
        order = ("chapters", "skipdb", "theintrodb", "introdb", "season_audio", "credits_text", "server_markers")
        cands = [
            credits(S.CHAPTERS, 1_200_000),
            credits(S.SKIPDB, 1_210_000),
            credits(S.THEINTRODB, 1_220_000),
            credits(partner, 1_220_000, origin="plex-1"),
        ]
        d = decide(cands, ctx(order=order), {})[T.CREDITS]
        chapter_marker = Marker(T.CREDITS, 1_200_000, DUR, ("chapters",))
        assert d.status is expected
        assert d.reason == reason
        if expected is DecisionStatus.DECIDED:
            assert (d.marker, d.proposed) == (chapter_marker, None)
        else:
            assert (d.marker, d.proposed) == (None, chapter_marker)


class TestAgreement:
    @pytest.mark.parametrize(
        ("a", "b", "expected", "start_ms"),
        [
            # "a" always precedes "b" in ORDER, so a's own end (checked edge) wins; start (unchecked
            # edge) is the later/safer of the two, a server marker's included: it may shorten the skip.
            (S.THEINTRODB, S.SKIPDB, DecisionStatus.DECIDED, 128_000),
            (S.THEINTRODB, S.SEASON_AUDIO, DecisionStatus.DECIDED, 128_000),
            (S.SKIPDB, S.SERVER_MARKERS, DecisionStatus.DECIDED, 128_000),
            (S.THEINTRODB, S.INTRODB, DecisionStatus.NEEDS_REVIEW, None),  # dependent pair = one source
            # an importer plugin's copy on a server is the crowd answer again
            (S.INTRODB, S.SERVER_MARKERS_IMPORTED, DecisionStatus.NEEDS_REVIEW, None),
            (S.THEINTRODB, S.SERVER_MARKERS_IMPORTED, DecisionStatus.NEEDS_REVIEW, None),
            (S.SKIPDB, S.SERVER_MARKERS_IMPORTED, DecisionStatus.DECIDED, 128_000),
        ],
    )
    def test_intro_pairs(self, a, b, expected, start_ms):
        cands = [intro(a, 127_000, 157_000), intro(b, 128_000, 160_000)]
        d = decide(cands, ctx(), {})[T.INTRO]
        assert d.status is expected
        if expected is DecisionStatus.DECIDED:
            assert (d.marker.start_ms, d.marker.end_ms) == (start_ms, 157_000)
            assert set(d.marker.decided_by) == {a.value, b.value}
            assert d.proposed is None
        else:
            assert d.marker is None
            assert d.proposed is not None
            assert d.proposed.start_ms == 127_000  # "a" outranks "b"
            assert d.reason == "sources don't agree yet"

    def test_dependent_pair_plus_local_source_decides(self):
        cands = [
            intro(S.THEINTRODB, 127_000, 157_000),
            intro(S.INTRODB, 128_000, 160_000),
            intro(S.SEASON_AUDIO, 126_000, 158_000),
        ]
        d = decide(cands, ctx(), {})[T.INTRO]
        assert d.status is DecisionStatus.DECIDED
        assert d.marker.decided_by == ("theintrodb", "introdb", "season_audio")

    def test_two_server_markers_from_different_servers_are_one_source(self):
        cands = [
            intro(S.SERVER_MARKERS, 127_000, 157_000, "plex-1"),
            intro(S.SERVER_MARKERS, 127_500, 157_200, "emby-1"),
        ]
        d = decide(cands, ctx(), {})[T.INTRO]
        assert d.status is DecisionStatus.NEEDS_REVIEW
        assert d.marker is None
        assert d.proposed is not None and d.proposed.start_ms == 127_000

    @pytest.mark.parametrize(
        ("end_b", "expected"), [(162_000, DecisionStatus.DECIDED), (162_001, DecisionStatus.NEEDS_REVIEW)]
    )
    def test_intro_end_tolerance_is_5s(self, end_b, expected):
        cands = [intro(S.THEINTRODB, 127_000, 157_000), intro(S.SKIPDB, 120_000, end_b)]
        d = decide(cands, ctx(), {})[T.INTRO]
        assert d.status is expected
        if expected is DecisionStatus.DECIDED:
            assert (d.marker.start_ms, d.marker.end_ms) == (127_000, 157_000)
            assert d.marker.decided_by == ("theintrodb", "skipdb")
        else:
            assert d.marker is None
            assert d.proposed is not None
            assert (d.proposed.start_ms, d.proposed.end_ms) == (127_000, 157_000)

    @pytest.mark.parametrize(
        ("start_b", "expected"), [(1_305_000, DecisionStatus.DECIDED), (1_305_001, DecisionStatus.NEEDS_REVIEW)]
    )
    def test_credits_start_tolerance_is_10s(self, start_b, expected):
        cands = [credits(S.THEINTRODB, 1_295_000), credits(S.SKIPDB, start_b, 1_320_000)]
        d = decide(cands, ctx(), {})[T.CREDITS]
        assert d.status is expected
        if expected is DecisionStatus.DECIDED:
            assert (d.marker.start_ms, d.marker.end_ms) == (1_295_000, DUR)
            assert d.marker.decided_by == ("theintrodb", "skipdb")
        else:
            assert d.marker is None
            assert d.proposed is not None
            assert (d.proposed.start_ms, d.proposed.end_ms) == (1_295_000, DUR)

    def test_checked_edge_comes_from_highest_precedence_confirming_source(self):
        cands = [intro(S.SKIPDB, 129_000, 157_800), intro(S.THEINTRODB, 127_894, 156_824)]
        d = decide(cands, ctx(), {})[T.INTRO]
        # end (checked) is TheIntroDB's own -- it wins on precedence; start (unchecked) is the
        # later/safer of the two starts, which happens to be SkipDB's.
        assert (d.marker.start_ms, d.marker.end_ms) == (129_000, 156_824)
        assert d.marker.decided_by == ("theintrodb", "skipdb")

    def test_user_source_order_changes_precedence(self):
        order = ("skipdb", "theintrodb", "introdb", "chapters", "season_audio", "credits_text", "server_markers")
        cands = [intro(S.SKIPDB, 129_000, 157_800), intro(S.THEINTRODB, 127_894, 156_824)]
        d = decide(cands, ctx(order=order), {})[T.INTRO]
        assert d.marker.end_ms == 157_800  # skipdb now outranks theintrodb, so its own end wins

    def test_source_missing_from_user_order_falls_back_to_lowest_precedence(self):
        # season_audio is intentionally left out of the order below; it must still be usable,
        # just sorted after every source the user did rank.
        order = ("theintrodb", "skipdb")
        cands = [intro(S.THEINTRODB, 127_000, 157_000), intro(S.SEASON_AUDIO, 126_000, 158_000)]
        d = decide(cands, ctx(order=order), {})[T.INTRO]
        assert d.status is DecisionStatus.DECIDED
        assert d.marker.decided_by == ("theintrodb", "season_audio")

    def test_agreeing_clusters_compose_checked_and_safer_edges(self):
        # start (unchecked) comes from SkipDB, the later of the two starts; end (checked)
        # comes from TheIntroDB, the source-order winner.
        cands = [intro(S.THEINTRODB, 0, 157_000), intro(S.SKIPDB, 120_000, 158_000)]
        d = decide(cands, ctx(), {})[T.INTRO]
        assert d.status is DecisionStatus.DECIDED
        assert (d.marker.start_ms, d.marker.end_ms) == (120_000, 157_000)
        assert d.marker.decided_by == ("theintrodb", "skipdb")
        assert d.proposed is None
        assert "sources agree" in d.reason

    def test_agreeing_clusters_compose_checked_and_safer_edges_for_credits(self):
        # start (checked) from TheIntroDB; end (unchecked) is the earlier of the two ends,
        # which is SkipDB's -- TheIntroDB's own end is None (runs to EOF).
        cands = [credits(S.THEINTRODB, 1_295_000), credits(S.SKIPDB, 1_296_000, 1_310_000)]
        d = decide(cands, ctx(), {})[T.CREDITS]
        assert d.status is DecisionStatus.DECIDED
        assert (d.marker.start_ms, d.marker.end_ms) == (1_295_000, 1_310_000)
        assert d.marker.decided_by == ("theintrodb", "skipdb")
        assert d.proposed is None
        assert "sources agree" in d.reason

    def test_credits_time_must_come_from_a_confirmed_candidate(self):
        # TheIntroDB's own start (1251s) is 15s from the nearest independent source (Plex, 1266s)
        # -- outside the 10s tolerance -- so it must not win just because it has the highest
        # source precedence. IntroDB's credits end at 1280s, so neither server start is far enough
        # inside the skip to shorten it (rule 7) and the start stays the one the sources decided.
        cands = [
            credits(S.THEINTRODB, 1_251_000),
            credits(S.INTRODB, 1_257_000, 1_280_000),
            credits(S.SERVER_MARKERS, 1_266_000, origin="plex-1"),
            credits(S.SERVER_MARKERS, 1_272_000, origin="jellyfin-1"),
        ]
        d = decide(cands, ctx(), {})[T.CREDITS]
        assert d.status is DecisionStatus.DECIDED
        assert (d.marker.start_ms, d.marker.end_ms) == (1_257_000, 1_280_000)
        assert d.marker.decided_by == ("introdb", "server_markers")

    def test_intro_time_must_come_from_a_confirmed_candidate(self):
        # TheIntroDB's own end (152s) is 6s from the nearest independent source (158s) -- outside
        # the 5s tolerance.
        cands = [
            intro(S.THEINTRODB, 120_000, 152_000),
            intro(S.INTRODB, 120_000, 153_000),
            intro(S.SERVER_MARKERS, 120_000, 158_000, "plex-1"),
            intro(S.SERVER_MARKERS, 120_000, 161_000, "jellyfin-1"),
        ]
        d = decide(cands, ctx(), {})[T.INTRO]
        assert d.status is DecisionStatus.DECIDED
        assert (d.marker.start_ms, d.marker.end_ms) == (120_000, 153_000)
        assert d.marker.decided_by == ("introdb", "server_markers")

    def test_unchecked_edge_supplier_is_credited_in_decided_by(self):
        # season_audio supplies neither the checked edge (TheIntroDB wins on precedence) nor does
        # it directly agree with the winner (diff 8s > 5s tolerance) -- but it DOES supply the
        # published (safer, latest) start, so it must still be credited.
        cands = [
            intro(S.THEINTRODB, 10_000, 40_000),
            intro(S.SKIPDB, 20_000, 44_000),
            intro(S.SEASON_AUDIO, 30_000, 48_000),
        ]
        d = decide(cands, ctx(), {})[T.INTRO]
        assert d.status is DecisionStatus.DECIDED
        assert (d.marker.start_ms, d.marker.end_ms) == (30_000, 40_000)
        assert d.marker.decided_by == ("theintrodb", "skipdb", "season_audio")

    def test_unchecked_credits_end_comes_from_every_confirming_candidate_and_credits_its_supplier(self):
        # TheIntroDB wins the start; season_audio (16s later, so not agreeing with TheIntroDB
        # directly) is confirmed through SkipDB and supplies the earliest end.
        cands = [
            credits(S.THEINTRODB, 1_000_000),
            credits(S.SKIPDB, 1_008_000, 1_300_000),
            credits(S.SEASON_AUDIO, 1_016_000, 1_280_000),
        ]
        d = decide(cands, ctx(), {})[T.CREDITS]
        assert d.status is DecisionStatus.DECIDED
        assert d.marker == Marker(T.CREDITS, 1_000_000, 1_280_000, ("theintrodb", "skipdb", "season_audio"))
        assert d.proposed is None
        assert d.reason == "sources agree: theintrodb, skipdb, season_audio"

    # Season audio with a server's own markers alone doesn't decide (ruling G3): TestSeasonAudioSources.
    @pytest.mark.parametrize(("first", "partner"), [(S.SEASON_AUDIO, S.CREDITS_TEXT), (S.SKIPDB, S.SERVER_MARKERS)])
    def test_agreeing_pair_without_a_bridge_decides(self, first, partner):
        cands = [intro(first, 60_000, 100_000), intro(partner, 60_000, 100_000, "plex-1")]
        d = decide(cands, ctx(), {})[T.INTRO]
        assert d.status is DecisionStatus.DECIDED
        assert d.marker == Marker(T.INTRO, 60_000, 100_000, (first.value, partner.value))
        assert d.proposed is None
        assert d.reason == f"sources agree: {first.value}, {partner.value}"

    @pytest.mark.parametrize(
        ("partner", "proposed_by", "groups"),
        [
            (S.CREDITS_TEXT, ("theintrodb", "skipdb", "season_audio", "credits_text"), "credits_text, season_audio"),
            # 8s from TheIntroDB the server marker doesn't agree with it, but it shares the published (safer) start,
            # which server markers may supply (rule 7: they can shorten a skip), so it is credited
            (
                S.SERVER_MARKERS,
                ("theintrodb", "skipdb", "season_audio", "server_markers"),
                "season_audio, server_markers",
            ),
        ],
    )
    def test_bridging_source_cannot_hide_an_agreeing_pair_that_contradicts_the_result(
        self, partner, proposed_by, groups
    ):
        # SkipDB (104s) agrees with both TheIntroDB (108s) and the pair (100s), so every agreeing
        # set is within 5s of the next and TheIntroDB wins the merged cluster -- but publishing
        # 60-108s would ignore two independent sources agreeing on 100s.
        cands = [
            intro(S.SEASON_AUDIO, 60_000, 100_000),
            intro(partner, 60_000, 100_000, "plex-1"),
            intro(S.THEINTRODB, 60_000, 108_000),
            intro(S.SKIPDB, 60_000, 104_000),
        ]
        d = decide(cands, ctx(), {})[T.INTRO]
        assert d.status is DecisionStatus.NEEDS_REVIEW
        assert d.marker is None
        assert d.proposed == Marker(T.INTRO, 60_000, 108_000, proposed_by)
        assert d.reason == f"agreeing sources contradict the result: {groups}"

    @pytest.mark.parametrize(
        ("pair_end", "expected"), [(100_000, DecisionStatus.DECIDED), (99_999, DecisionStatus.NEEDS_REVIEW)]
    )
    def test_contradicting_pair_must_be_beyond_the_tolerance_of_the_result(self, pair_end, expected):
        cands = [
            intro(S.SEASON_AUDIO, 60_000, pair_end),
            intro(S.CREDITS_TEXT, 60_000, pair_end),
            intro(S.SKIPDB, 60_000, 102_000),
            intro(S.THEINTRODB, 60_000, 105_000),
        ]
        d = decide(cands, ctx(), {})[T.INTRO]
        marker = Marker(T.INTRO, 60_000, 105_000, ("theintrodb", "skipdb", "season_audio", "credits_text"))
        assert d.status is expected
        if expected is DecisionStatus.DECIDED:
            assert (d.marker, d.proposed) == (marker, None)
            assert d.reason == "sources agree: theintrodb, skipdb, season_audio, credits_text"
        else:
            assert (d.marker, d.proposed) == (None, marker)
            assert d.reason == "agreeing sources contradict the result: credits_text, season_audio"

    @pytest.mark.parametrize(
        ("partner", "expected", "reason"),
        [
            (S.INTRODB, DecisionStatus.DECIDED, "sources agree: skipdb, season_audio, theintrodb, introdb"),
            (
                S.CREDITS_TEXT,
                DecisionStatus.NEEDS_REVIEW,
                "agreeing sources contradict the result: credits_text, introdb/theintrodb",
            ),
        ],
    )
    def test_contradicting_pair_behind_a_bridge_needs_two_independent_groups(self, partner, expected, reason):
        # SkipDB ranks first and wins at 108s; season_audio (104s) bridges it to TheIntroDB and a
        # partner at 100s. TheIntroDB + IntroDB are one source, so that pair alone can't contradict.
        order = ("skipdb", "season_audio", "theintrodb", "introdb", "chapters", "credits_text", "server_markers")
        cands = [
            intro(S.SKIPDB, 60_000, 108_000),
            intro(S.SEASON_AUDIO, 60_000, 104_000),
            intro(S.THEINTRODB, 60_000, 100_000),
            intro(partner, 60_000, 100_000),
        ]
        d = decide(cands, ctx(order=order), {})[T.INTRO]
        marker = Marker(T.INTRO, 60_000, 108_000, ("skipdb", "season_audio", "theintrodb", partner.value))
        assert d.status is expected
        assert d.reason == reason
        if expected is DecisionStatus.DECIDED:
            assert (d.marker, d.proposed) == (marker, None)
        else:
            assert (d.marker, d.proposed) == (None, marker)

    def test_two_disagreeing_clusters_with_identical_pair_values_need_review(self):
        # Both members of each pair share the same time -- the clusters conflict on their own,
        # not merely because of a slight per-source offset.
        cands = [
            intro(S.THEINTRODB, 60_000, 90_000),
            intro(S.SKIPDB, 60_000, 90_000),
            intro(S.CREDITS_TEXT, 120_000, 150_000),
            intro(S.SERVER_MARKERS, 120_000, 150_000, "plex-1"),
        ]
        d = decide(cands, ctx(), {})[T.INTRO]
        assert d.status is DecisionStatus.NEEDS_REVIEW
        assert d.marker is None
        assert d.proposed is not None
        assert (d.proposed.start_ms, d.proposed.end_ms) == (60_000, 90_000)
        assert "agreeing sources conflict" in d.reason

    def test_composed_marker_failing_sanity_needs_review(self):
        # The checked edge (end=50_000) and the safer unchecked edge (start=49_000, the later of
        # the two starts) combine into a 1 s segment -- too short to publish.
        cands = [intro(S.THEINTRODB, 10_000, 50_000), intro(S.SKIPDB, 49_000, 52_000)]
        d = decide(cands, ctx(), {})[T.INTRO]
        assert d.status is DecisionStatus.NEEDS_REVIEW
        assert d.marker is None
        assert d.proposed is not None
        assert (d.proposed.start_ms, d.proposed.end_ms) == (10_000, 50_000)  # the winner's own edges
        assert "agreeing sources disagree on the other edge" in d.reason

    def test_two_disagreeing_agreeing_clusters_need_review(self):
        # Two separate clusters each internally agree, but the two clusters disagree with each
        # other -- that is itself contradicting evidence, not something to resolve by preferring
        # the "richer" cluster.
        cands = [
            intro(S.THEINTRODB, 10_000, 40_000),
            intro(S.SKIPDB, 11_000, 41_000),
            intro(S.SEASON_AUDIO, 80_000, 110_000),
            intro(S.CREDITS_TEXT, 81_000, 111_000),
            intro(S.SERVER_MARKERS, 82_000, 112_000),
        ]
        d = decide(cands, ctx(), {})[T.INTRO]
        assert d.status is DecisionStatus.NEEDS_REVIEW
        assert d.marker is None
        assert d.proposed is not None
        assert (d.proposed.start_ms, d.proposed.end_ms) == (11_000, 40_000)
        assert d.proposed.decided_by == ("theintrodb", "skipdb")
        assert "agreeing sources conflict" in d.reason
        assert "theintrodb" in d.reason and "season_audio" in d.reason

    def test_two_disagreeing_agreeing_clusters_need_review_for_credits(self):
        cands = [
            credits(S.THEINTRODB, 1_251_000),
            credits(S.SKIPDB, 1_255_000),
            credits(S.CREDITS_TEXT, 1_290_000),
            credits(S.SERVER_MARKERS, 1_295_000, origin="plex-1"),
        ]
        d = decide(cands, ctx(), {})[T.CREDITS]
        assert d.status is DecisionStatus.NEEDS_REVIEW
        assert d.marker is None
        assert d.proposed is not None
        assert (d.proposed.start_ms, d.proposed.end_ms) == (1_251_000, DUR)
        assert d.proposed.decided_by == ("theintrodb", "skipdb")
        assert "agreeing sources conflict" in d.reason
        assert "theintrodb" in d.reason and "credits_text" in d.reason

    def test_duplicate_candidates_from_one_source_alone_do_not_fake_independence(self):
        cands = [intro(S.THEINTRODB, 127_000, 157_000), intro(S.THEINTRODB, 127_500, 157_500)]
        d = decide(cands, ctx(), {})[T.INTRO]
        assert d.status is DecisionStatus.NEEDS_REVIEW and d.marker is None

    def test_duplicate_candidates_from_one_source_count_as_one_vote(self):
        cands = [
            intro(S.THEINTRODB, 127_000, 157_000),
            intro(S.THEINTRODB, 127_500, 157_500),
            intro(S.SKIPDB, 127_800, 157_800),
        ]
        d = decide(cands, ctx(), {})[T.INTRO]
        assert d.status is DecisionStatus.DECIDED
        assert d.marker.decided_by == ("theintrodb", "skipdb")  # the duplicate is counted once

    def test_confidence_breaks_tie_among_same_precedence_candidates(self):
        # Two candidates from the same source (tied precedence and tied Source-enum order) but
        # different confidence, plus a corroborating independent source so the cluster decides.
        # Confidence is the only thing that picks between the tied pair.
        low_confidence = Candidate(T.INTRO, 127_000, 157_000, S.THEINTRODB, confidence=0.5)
        high_confidence = Candidate(T.INTRO, 127_500, 157_500, S.THEINTRODB, confidence=0.9)
        corroborating = intro(S.SKIPDB, 127_800, 157_800)
        d = decide([low_confidence, high_confidence, corroborating], ctx(), {})[T.INTRO]
        assert d.status is DecisionStatus.DECIDED
        assert d.marker.end_ms == 157_500  # the higher-confidence candidate wins the tie

    @pytest.mark.parametrize(
        ("odd_confidence", "odd_end", "zero_end"),
        [
            (math.nan, 103_000, 100_000),
            (math.inf, 103_000, 100_000),
            (-math.inf, 100_000, 103_000),
            (math.nan, 100_000, 103_000),
        ],
    )
    def test_non_finite_confidence_ranks_as_zero(self, odd_confidence, odd_end, zero_end):
        # Counted as 0.0, the odd candidate ties with the 0.0 one on confidence, so the shorter skip
        # (end 100s) wins in both input orders.
        odd = Candidate(T.INTRO, 60_000, odd_end, S.SKIPDB, confidence=odd_confidence)
        zero = Candidate(T.INTRO, 60_000, zero_end, S.SKIPDB, confidence=0.0)
        corroborating = intro(S.SEASON_AUDIO, 60_000, 101_000)
        for cands in ([odd, zero, corroborating], [corroborating, zero, odd]):
            d = decide(cands, ctx(), {})[T.INTRO]
            assert d.status is DecisionStatus.DECIDED
            assert d.marker == Marker(T.INTRO, 60_000, 100_000, ("skipdb", "season_audio"))

    def test_source_precedence_tie_breaks_by_enum_order_before_confidence(self):
        # season_audio and credits_text are both absent from this order -> tied fallback
        # precedence. season_audio comes first in the Source enum, so it must win the tie even
        # though it has far lower confidence.
        order = ("theintrodb",)
        season_audio = Candidate(T.INTRO, 10_000, 40_000, S.SEASON_AUDIO, confidence=0.1)
        credits_text = Candidate(T.INTRO, 10_500, 40_500, S.CREDITS_TEXT, confidence=0.99)
        d = decide([season_audio, credits_text], ctx(order=order), {})[T.INTRO]
        assert d.status is DecisionStatus.DECIDED
        assert d.marker.end_ms == 40_000  # season_audio's own end, not credits_text's
        assert d.marker.decided_by == ("season_audio", "credits_text")

    def test_season_audio_and_credits_text_are_independent_of_each_other(self):
        cands = [intro(S.SEASON_AUDIO, 10_000, 40_000), intro(S.CREDITS_TEXT, 10_500, 40_500)]
        d = decide(cands, ctx(), {})[T.INTRO]
        assert d.status is DecisionStatus.DECIDED
        assert d.marker.decided_by == ("season_audio", "credits_text")

    def test_transitive_agreement_does_not_credit_a_member_that_supplies_no_edge_and_never_agrees_with_the_winner(self):
        # A agrees with B, B agrees with C, but A does not agree with C. C must not be credited
        # in decided_by just because it reached the merged evidence pool via B -- unless it also
        # happens to supply the published (unchecked) edge, which is a separate rule and not
        # the case here: C's own end (1_320_000) is later than the winner's (1_310_000), so it
        # does not supply the earliest-end edge either.
        a = Candidate(T.CREDITS, 1_000_000, 1_310_000, S.THEINTRODB)
        b = Candidate(T.CREDITS, 1_008_000, 1_312_000, S.SKIPDB)
        c = Candidate(T.CREDITS, 1_016_000, 1_320_000, S.SEASON_AUDIO)
        d = decide([a, b, c], ctx(), {})[T.CREDITS]
        assert d.status is DecisionStatus.DECIDED
        assert (d.marker.start_ms, d.marker.end_ms) == (1_000_000, 1_310_000)
        assert d.marker.decided_by == ("theintrodb", "skipdb")  # season_audio excluded

    def test_server_marker_ranked_first_never_supplies_the_time(self):
        # Even when the user's own order ranks server markers first, they still can't supply a
        # time -- only confirm one.
        order = ("server_markers", "theintrodb", "introdb", "skipdb", "season_audio", "credits_text", "chapters")
        cands = [intro(S.SERVER_MARKERS, 127_000, 157_000, "plex-1"), intro(S.THEINTRODB, 127_500, 157_500)]
        d = decide(cands, ctx(order=order), {})[T.INTRO]
        assert d.status is DecisionStatus.DECIDED
        assert (d.marker.start_ms, d.marker.end_ms) == (127_500, 157_500)  # theintrodb's own edges
        assert d.marker.decided_by == ("server_markers", "theintrodb")  # decided_by still follows the order

    @pytest.mark.parametrize("reverse", [False, True])
    @pytest.mark.parametrize(
        ("crowd_end", "server_end", "marker"),
        [
            # R&M S01E07 shape: the crowd's credits run to the end of the file; Plex's non-final credits end 28s
            # after they start, before the post-credits scene -- the agreeing server marker shortens the skip.
            (None, 1_267_413, Marker(T.CREDITS, 1_239_000, 1_267_413, ("theintrodb", "server_markers"))),
            # Plex's final credits run to the end as well: nothing shorter, the end of the file stays
            (None, None, Marker(T.CREDITS, 1_239_000, DUR, ("theintrodb", "server_markers"))),
            # a server marker that ends later never lengthens the crowd's skip
            (1_280_000, None, Marker(T.CREDITS, 1_239_000, 1_280_000, ("theintrodb", "server_markers"))),
        ],
    )
    def test_an_agreeing_server_marker_shortens_crowd_credits_but_never_lengthens_them(
        self, crowd_end, server_end, marker, reverse
    ):
        # Plex's start (0.4s later) agrees, but the start is the checked edge and a server marker never supplies it.
        cands = [credits(S.THEINTRODB, 1_239_000, crowd_end), credits(S.SERVER_MARKERS, 1_239_413, server_end, "p")]
        d = decide(cands[:: -1 if reverse else 1], ctx(), {})[T.CREDITS]
        assert d.status is DecisionStatus.DECIDED
        assert (d.marker, d.proposed) == (marker, None)

    @pytest.mark.parametrize("reverse", [False, True])
    def test_an_agreeing_server_marker_gives_a_crowd_intro_over_a_cold_open_a_later_start(self, reverse):
        cands = [intro(S.INTRODB, 0, 95_000), intro(S.SERVER_MARKERS, 62_000, 94_500, "plex-1")]
        d = decide(cands[:: -1 if reverse else 1], ctx(), {})[T.INTRO]
        assert d.marker == Marker(T.INTRO, 62_000, 95_000, ("introdb", "server_markers"))

    def test_server_markers_alone_never_supply_both_edges(self):
        # Plex and an imported copy agree, but neither is a source that may publish: review, with no crash on a
        # cluster that has no non-server member.
        cands = [
            intro(S.SERVER_MARKERS, 62_000, 95_000, "plex-1"),
            intro(S.SERVER_MARKERS_IMPORTED, 62_000, 95_500, "jellyfin-1"),
        ]
        for publish_when in ("high", "medium"):
            d = decide(cands, ctx(publish_when), {})[T.INTRO]
            assert d.status is DecisionStatus.NEEDS_REVIEW
            assert d.marker is None
            assert d.proposed == Marker(T.INTRO, 62_000, 95_000, ("server_markers",))
            assert d.reason == "sources don't agree yet"

    def test_an_imported_server_copy_does_not_confirm_its_crowd_source_but_plex_does(self):
        cands = [
            intro(S.INTRODB, 24_046, 114_105),
            intro(S.SERVER_MARKERS_IMPORTED, 24_046, 114_105, "jellyfin-1"),
            intro(S.SERVER_MARKERS, 30_000, 113_900, "plex-1"),
        ]
        d = decide(cands, ctx(), {})[T.INTRO]
        assert d.status is DecisionStatus.DECIDED
        assert d.marker == Marker(T.INTRO, 30_000, 114_105, ("introdb", "server_markers", "server_markers_imported"))

    def test_plex_and_an_imported_copy_agreeing_against_a_chapter_send_it_to_review(self):
        cands = [
            intro(S.CHAPTERS, 105_272, 194_986),
            intro(S.SERVER_MARKERS, 24_500, 113_900, "plex-1"),
            intro(S.SERVER_MARKERS_IMPORTED, 24_046, 114_105, "jellyfin-1"),
        ]
        d = decide(cands, ctx(), {})[T.INTRO]
        assert d.status is DecisionStatus.NEEDS_REVIEW
        assert d.proposed == Marker(T.INTRO, 105_272, 194_986, ("chapters",))
        assert d.reason == "agreeing sources contradict the result: introdb/theintrodb, server_markers"


class TestSingleSource:
    @pytest.mark.parametrize(
        ("cands", "medium"),
        [
            # the source checks this file's cut itself: SkipDB answers only a duration match, credit text reads
            # the file
            ([intro(S.SKIPDB, 127_000, 157_000)], DecisionStatus.DECIDED),
            ([intro(S.CREDITS_TEXT, 127_000, 157_000)], DecisionStatus.DECIDED),
            # season audio (same season or the previous season's hint) only agrees in phase 2 (owner, 2026-09-14)
            ([intro(S.SEASON_AUDIO, 127_000, 157_000)], DecisionStatus.NEEDS_REVIEW),
            ([intro(S.SEASON_AUDIO_PREVIOUS, 127_000, 157_000)], DecisionStatus.NEEDS_REVIEW),
            (
                [intro(S.SEASON_AUDIO, 127_000, 157_000), intro(S.SEASON_AUDIO_PREVIOUS, 127_500, 157_200)],
                DecisionStatus.NEEDS_REVIEW,
            ),
            # chapters are rule 3, the same at both levels
            ([intro(S.CHAPTERS, 127_000, 157_000)], DecisionStatus.DECIDED),
            # IntroDB takes no duration and TheIntroDB answers its closest stored cut: agreement only
            ([intro(S.THEINTRODB, 127_000, 157_000)], DecisionStatus.NEEDS_REVIEW),
            ([intro(S.INTRODB, 127_000, 157_000)], DecisionStatus.NEEDS_REVIEW),
            ([intro(S.THEINTRODB, 127_000, 157_000), intro(S.INTRODB, 127_500, 157_200)], DecisionStatus.NEEDS_REVIEW),
            # markers already on servers never decide alone
            ([intro(S.SERVER_MARKERS, 127_000, 157_000, "plex-1")], DecisionStatus.NEEDS_REVIEW),
            ([intro(S.SERVER_MARKERS_IMPORTED, 127_000, 157_000, "jf-1")], DecisionStatus.NEEDS_REVIEW),
            (
                [intro(S.INTRODB, 127_000, 157_000), intro(S.SERVER_MARKERS_IMPORTED, 127_000, 157_000, "jf-1")],
                DecisionStatus.NEEDS_REVIEW,
            ),
        ],
    )
    def test_single_source_matrix(self, cands, medium):
        high = DecisionStatus.DECIDED if cands[0].source is S.CHAPTERS else DecisionStatus.NEEDS_REVIEW
        for publish_when, expected in (("high", high), ("medium", medium)):
            for order in (cands, cands[::-1]):
                d = decide(order, ctx(publish_when), {})[T.INTRO]
                assert d.status is expected, publish_when
                shown = d.marker if expected is DecisionStatus.DECIDED else d.proposed
                assert (shown.start_ms, shown.end_ms, shown.decided_by) == (127_000, 157_000, (cands[0].source.value,))
                if expected is DecisionStatus.NEEDS_REVIEW:
                    assert (d.marker, d.reason) == (None, "sources don't agree yet")

    def test_no_candidates(self):
        d = decide([], ctx(), {})[T.CREDITS]
        assert d.status is DecisionStatus.NO_EVIDENCE and d.marker is None and d.proposed is None

    def test_medium_single_candidate_failing_sanity_has_no_marker(self):
        d = decide([intro(S.SKIPDB, 10_000, 12_000)], ctx("medium"), {})[T.INTRO]  # 2s, too short
        assert d.status is DecisionStatus.NO_EVIDENCE and d.marker is None


class TestMediumContradiction:
    """Medium may decide a single source only when nothing else contradicts it."""

    @pytest.mark.parametrize("other", [S.THEINTRODB, S.SEASON_AUDIO, S.SERVER_MARKERS, S.SERVER_MARKERS_IMPORTED])
    def test_contradicting_independent_source_blocks_medium_single_source(self, other):
        cands = [intro(S.SKIPDB, 10_000, 40_000), intro(other, 100_000, 130_000, "plex-1")]
        d = decide(cands, ctx("medium"), {})[T.INTRO]
        assert d.status is DecisionStatus.NEEDS_REVIEW
        assert d.marker is None
        first = min(cands, key=lambda c: ORDER.index(c.source.value))
        assert d.proposed == Marker(T.INTRO, first.start_ms, first.end_ms, (first.source.value,))
        crowd = other in (S.THEINTRODB, S.SERVER_MARKERS_IMPORTED)
        groups = sorted({"skipdb", "introdb/theintrodb" if crowd else other.value})
        assert d.reason == f"sources disagree: {', '.join(groups)}"

    def test_agreeing_server_marker_decides_with_any_source(self):
        # Two groups agree, so this is rule 4 at both levels, not the single-source rule.
        cands = [intro(S.THEINTRODB, 127_000, 157_000), intro(S.SERVER_MARKERS, 127_500, 157_200, "plex-1")]
        for publish_when in ("high", "medium"):
            d = decide(cands, ctx(publish_when), {})[T.INTRO]
            assert d.status is DecisionStatus.DECIDED
            assert d.marker == Marker(T.INTRO, 127_500, 157_000, ("theintrodb", "server_markers"))

    @pytest.mark.parametrize(
        ("low_end", "high_end", "expected", "reason"),
        [
            (98_000, 102_000, DecisionStatus.DECIDED, "single source (skipdb)"),
            # each other SkipDB end is within 5s of the first's, but 8s from the other one
            (96_000, 104_000, DecisionStatus.NEEDS_REVIEW, "source disagrees with itself"),
        ],
    )
    def test_every_pair_of_a_single_sources_candidates_must_agree_at_medium(self, low_end, high_end, expected, reason):
        cands = [
            Candidate(T.INTRO, 60_000, 100_000, S.SKIPDB, confidence=1.0),
            Candidate(T.INTRO, 60_000, low_end, S.SKIPDB, confidence=0.5),
            Candidate(T.INTRO, 60_000, high_end, S.SKIPDB, confidence=0.5),
        ]
        d = decide(cands, ctx("medium"), {})[T.INTRO]
        own = Marker(T.INTRO, 60_000, 100_000, ("skipdb",))
        assert d.status is expected
        assert d.reason == reason
        if expected is DecisionStatus.DECIDED:
            assert (d.marker, d.proposed) == (own, None)
        else:
            assert (d.marker, d.proposed) == (None, own)

    @pytest.mark.parametrize("reverse", [False, True])
    @pytest.mark.parametrize(
        ("longer", "shorter"),
        [
            (intro(S.SKIPDB, 10_000, 100_000), intro(S.SKIPDB, 80_000, 100_000)),
            # SkipDB never decides credits alone (rule 6), so a local detector stands in for the credits row
            (credits(S.CREDITS_TEXT, 1_300_000), credits(S.CREDITS_TEXT, 1_300_000, 1_310_000)),
        ],
    )
    def test_same_source_pair_at_medium_publishes_the_safer_other_edge(self, longer, shorter, reverse):
        cands = [shorter, longer] if reverse else [longer, shorter]
        d = decide(cands, ctx("medium", types=(longer.type,)), {})[longer.type]
        name = longer.source.value
        assert d.status is DecisionStatus.DECIDED
        assert d.marker == Marker(shorter.type, shorter.start_ms, shorter.end_ms, (name,))
        assert d.proposed is None
        assert d.reason == f"single source ({name})"

    @pytest.mark.parametrize("reverse", [False, True])
    @pytest.mark.parametrize(
        ("longer", "shorter"),
        [
            (intro(S.SKIPDB, 10_000, 100_000), intro(S.SKIPDB, 80_000, 100_000)),
            (credits(S.SKIPDB, 1_300_000), credits(S.SKIPDB, 1_300_000, 1_310_000)),
        ],
    )
    def test_same_source_tie_on_the_checked_edge_proposes_the_shorter_skip(self, longer, shorter, reverse):
        # At "high" nothing publishes, so the proposal is one candidate's own marker: the tie-break
        # after confidence prefers the shorter skip.
        cands = [shorter, longer] if reverse else [longer, shorter]
        d = decide(cands, ctx("high", types=(longer.type,)), {})[longer.type]
        assert d.status is DecisionStatus.NEEDS_REVIEW
        assert d.marker is None
        assert d.proposed == Marker(shorter.type, shorter.start_ms, shorter.end_ms, ("skipdb",))
        assert d.reason == "sources don't agree yet"

    @pytest.mark.parametrize(
        ("cands", "marker"),
        [
            # the other answer says the intro starts 70s later: that later start wins
            (
                [Candidate(T.INTRO, 10_000, 100_000, S.SKIPDB), Candidate(T.INTRO, 80_000, 100_000, S.SKIPDB, 0.5)],
                Marker(T.INTRO, 80_000, 100_000, ("skipdb",)),
            ),
            (
                [
                    Candidate(T.CREDITS, 1_300_000, None, S.CREDITS_TEXT),
                    Candidate(T.CREDITS, 1_302_000, 1_310_000, S.CREDITS_TEXT, 0.5),
                ],
                Marker(T.CREDITS, 1_300_000, 1_310_000, ("credits_text",)),
            ),
        ],
    )
    def test_medium_other_edge_is_the_safer_value_across_the_sources_candidates(self, cands, marker):
        for order in (cands, cands[::-1]):
            d = decide(order, ctx("medium", types=(marker.type,)), {})[marker.type]
            assert d.status is DecisionStatus.DECIDED
            assert (d.marker, d.proposed) == (marker, None)
            assert d.reason == f"single source ({marker.decided_by[0]})"

    def test_medium_composed_marker_failing_sanity_needs_review(self):
        # the other answer's later start (98s) with the first's end (100s) leaves a 2s segment
        cands = [Candidate(T.INTRO, 60_000, 100_000, S.SKIPDB), Candidate(T.INTRO, 98_000, 101_000, S.SKIPDB, 0.5)]
        d = decide(cands, ctx("medium"), {})[T.INTRO]
        assert d.status is DecisionStatus.NEEDS_REVIEW
        assert d.marker is None
        assert d.proposed == Marker(T.INTRO, 60_000, 100_000, ("skipdb",))
        assert d.reason == "sources disagree on the other edge"

    def test_single_source_disagreeing_with_its_own_duplicate_does_not_decide_at_medium(self):
        cands = [intro(S.SKIPDB, 10_000, 40_000), intro(S.SKIPDB, 100_000, 130_000)]
        d = decide(cands, ctx("medium"), {})[T.INTRO]
        assert d.status is DecisionStatus.NEEDS_REVIEW
        assert d.marker is None
        assert "source disagrees with itself" in d.reason

    def test_high_is_unaffected_by_the_contradiction_rule(self):
        cands = [intro(S.THEINTRODB, 10_000, 40_000), intro(S.SKIPDB, 100_000, 130_000)]
        d = decide(cands, ctx("high"), {})[T.INTRO]
        assert d.status is DecisionStatus.NEEDS_REVIEW and d.marker is None


class TestMediumSkipDbAlone:
    """Rule 6: at "Medium" SkipDB alone may decide an intro or recap, never credits or a preview (lab scale run: every
    lone SkipDB credits answer started early, Battlestar Galactica S04E05 by 6.7 min)."""

    @pytest.mark.parametrize(
        ("cand", "medium"),
        [
            (intro(S.SKIPDB, 127_000, 157_000), DecisionStatus.DECIDED),
            (Candidate(T.RECAP, 20_000, 60_000, S.SKIPDB), DecisionStatus.DECIDED),
            (credits(S.SKIPDB, 1_239_000, 1_300_000), DecisionStatus.NEEDS_REVIEW),
            (Candidate(T.PREVIEW, 1_290_000, 1_310_000, S.SKIPDB), DecisionStatus.NEEDS_REVIEW),
        ],
        ids=["intro", "recap", "credits", "preview"],
    )
    def test_skipdb_alone_decides_only_intros_and_recaps_at_medium(self, cand, medium):
        own = Marker(cand.type, cand.start_ms, cand.end_ms, ("skipdb",))
        for publish_when, expected in (("high", DecisionStatus.NEEDS_REVIEW), ("medium", medium)):
            d = decide([cand], ctx(publish_when, types=(cand.type,)), {})[cand.type]
            assert d.status is expected, publish_when
            if expected is DecisionStatus.DECIDED:
                assert (d.marker, d.proposed, d.reason) == (own, None, "single source (skipdb)")
            else:
                assert (d.marker, d.proposed, d.reason) == (None, own, "sources don't agree yet")

    def test_skipdb_credits_still_decide_with_an_agreeing_independent_source_at_medium(self):
        cands = [credits(S.SKIPDB, 1_239_000), credits(S.CREDITS_TEXT, 1_243_000, 1_300_000)]
        for order in (cands, cands[::-1]):
            d = decide(order, ctx("medium"), {})[T.CREDITS]
            assert d.status is DecisionStatus.DECIDED
            assert d.marker == Marker(T.CREDITS, 1_239_000, 1_300_000, ("skipdb", "credits_text"))


def _permutations(cands):
    return [list(p) for p in itertools.permutations(cands)]


class TestServerMarkersShortenTheDecidedEdge:
    """Rule 7: once credits or a preview are decided, the servers' own markers pull the start later, toward a shorter
    skip. Intros and recaps never move (lab scale run frame check: every intro end was already right)."""

    PLEX_CREDITS = credits(S.SERVER_MARKERS, 1_255_500, None, "plex-1")  # 16.5 s after 1_239_000

    @pytest.mark.parametrize(
        ("decided", "server", "marker", "reason"),
        [
            # Avatar (2009): the "End Credits" chapter starts on the last story shot; Plex's own credits start 16.5 s
            # later, on the roll
            (
                [credits(S.CHAPTERS, 5_500_000)],
                credits(S.SERVER_MARKERS, 5_516_500, None, "plex-1"),
                Marker(T.CREDITS, 5_516_500, MOVIE_DUR, ("chapters", "server_markers")),
                "chapters; start shortened to the server's own marker (plex-1)",
            ),
            (
                [credits(S.CHAPTERS, 1_239_000)],
                PLEX_CREDITS,
                Marker(T.CREDITS, 1_255_500, DUR, ("chapters", "server_markers")),
                "chapters; start shortened to the server's own marker (plex-1)",
            ),
            (
                [credits(S.THEINTRODB, 1_239_000), credits(S.SKIPDB, 1_241_000)],
                PLEX_CREDITS,
                Marker(T.CREDITS, 1_255_500, DUR, ("theintrodb", "skipdb", "server_markers")),
                "sources agree: theintrodb, skipdb; start shortened to the server's own marker (plex-1)",
            ),
            # a Jellyfin server's own preview segment starts 15 s after a "Preview" chapter
            (
                [Candidate(T.PREVIEW, 1_280_000, None, S.CHAPTERS)],
                Candidate(T.PREVIEW, 1_295_000, None, S.SERVER_MARKERS, origin="jf-1"),
                Marker(T.PREVIEW, 1_295_000, DUR, ("chapters", "server_markers")),
                "chapters; start shortened to the server's own marker (jf-1)",
            ),
        ],
        ids=["movie-chapter-credits", "chapter-credits", "agreed-credits", "chapter-preview"],
    )
    def test_the_start_moves_to_the_servers_own_marker(self, decided, server, marker, reason):
        movie = decided[0].start_ms >= 5_000_000
        x = ctx(is_movie=movie, duration=MOVIE_DUR if movie else DUR, types=(marker.type,))
        for order in _permutations([*decided, server]):
            d = decide(order, x, {})[marker.type]
            assert d.status is DecisionStatus.DECIDED
            assert (d.marker, d.proposed, d.reason) == (marker, None, reason)
        assert shortened_by(d.reason) == (server.origin,)
        # Without the server's marker the same sources decide the longer skip.
        d = decide(decided, x, {})[marker.type]
        assert d.status is DecisionStatus.DECIDED
        assert d.marker.start_ms < marker.start_ms
        assert "server_markers" not in d.marker.decided_by

    @pytest.mark.parametrize(
        ("decided", "servers", "marker", "reason"),
        [
            # Plex's intro ends 7 s before the chapter's (it stops partway through the opening)
            (
                [intro(S.CHAPTERS, 126_771, 157_000)],
                [intro(S.SERVER_MARKERS, 127_000, 150_000, "plex-1")],
                Marker(T.INTRO, 126_771, 157_000, ("chapters",)),
                "chapters",
            ),
            (
                [intro(S.THEINTRODB, 126_000, 157_000), intro(S.SKIPDB, 126_500, 158_000)],
                [
                    intro(S.SERVER_MARKERS, 127_000, 150_000, "plex-1"),
                    intro(S.SERVER_MARKERS, 127_000, 146_000, "jf-1"),
                ],
                Marker(T.INTRO, 126_500, 157_000, ("theintrodb", "skipdb")),
                "sources agree: theintrodb, skipdb",
            ),
            (
                [Candidate(T.RECAP, 20_000, 60_000, S.CHAPTERS)],
                [Candidate(T.RECAP, 20_000, 45_000, S.SERVER_MARKERS, origin="jf-1")],
                Marker(T.RECAP, 20_000, 60_000, ("chapters",)),
                "chapters",
            ),
        ],
        ids=["chapter-intro", "agreed-intro-two-servers", "chapter-recap"],
    )
    def test_an_intro_or_recap_is_never_shortened_by_an_earlier_server_end(self, decided, servers, marker, reason):
        x = ctx(types=(marker.type,))
        for order in _permutations([*decided, *servers]):
            d = decide(order, x, {})[marker.type]
            assert d.status is DecisionStatus.DECIDED
            assert (d.marker, d.proposed, d.reason) == (marker, None, reason)

    @pytest.mark.parametrize(
        ("server_start", "shortened"),
        [
            (1_249_000, False),  # exactly the 10 s tolerance after the start
            (1_249_001, True),
            (1_290_000, False),  # exactly 10 s before the end: the skip would all but vanish
            (1_289_999, True),
            (1_230_000, False),  # an earlier start would lengthen the skip
            (1_200_000, False),
            (1_310_000, False),  # starts after the decided credits end
        ],
    )
    def test_credits_bounds(self, server_start, shortened):
        chapter = credits(S.CHAPTERS, 1_239_000, 1_300_000)
        server = credits(S.SERVER_MARKERS, server_start, None, "plex-1")
        d = decide([chapter, server], ctx(), {})[T.CREDITS]
        assert d.status is DecisionStatus.DECIDED
        if shortened:
            assert d.marker == Marker(T.CREDITS, server_start, 1_300_000, ("chapters", "server_markers"))
            assert d.reason == "chapters; start shortened to the server's own marker (plex-1)"
        else:
            assert (d.marker, d.reason) == (Marker(T.CREDITS, 1_239_000, 1_300_000, ("chapters",)), "chapters")

    @pytest.mark.parametrize(
        ("servers", "marker", "reason"),
        [
            # the latest start wins, whichever server it came from
            (
                [
                    credits(S.SERVER_MARKERS, 1_255_000, None, "plex-1"),
                    credits(S.SERVER_MARKERS, 1_262_000, None, "jf-1"),
                ],
                Marker(T.CREDITS, 1_262_000, DUR, ("chapters", "server_markers")),
                "chapters; start shortened to the server's own marker (jf-1)",
            ),
            (
                [
                    credits(S.SERVER_MARKERS, 1_262_000, None, "plex-1"),
                    credits(S.SERVER_MARKERS, 1_262_000, None, "jf-1"),
                ],
                Marker(T.CREDITS, 1_262_000, DUR, ("chapters", "server_markers")),
                "chapters; start shortened to the server's own marker (jf-1, plex-1)",
            ),
            # servers that split their credits: each offers its first piece inside the skip, the latest offer wins
            # (the latest piece overall, 1290 s, would skip Jellyfin's gap)
            (
                [
                    credits(S.SERVER_MARKERS, 1_255_000, 1_275_000, "plex-1"),
                    credits(S.SERVER_MARKERS, 1_280_000, None, "plex-1"),
                    credits(S.SERVER_MARKERS, 1_262_000, 1_285_000, "jf-1"),
                    credits(S.SERVER_MARKERS, 1_290_000, None, "jf-1"),
                ],
                Marker(T.CREDITS, 1_262_000, DUR, ("chapters", "server_markers")),
                "chapters; start shortened to the server's own marker (jf-1)",
            ),
            # a server whose own credits agree with the decided start blocks it, whatever another server says
            (
                [
                    credits(S.SERVER_MARKERS, 1_242_000, None, "plex-1"),
                    credits(S.SERVER_MARKERS, 1_262_000, None, "jf-1"),
                ],
                Marker(T.CREDITS, 1_239_000, DUR, ("chapters",)),
                "chapters",
            ),
            # an open-ended own marker (Emby's CreditsStart, Plex's final credits) that started 19 s before the decided
            # start covers it: that server says the credits are running, so another server's later start is ignored
            (
                [
                    credits(S.SERVER_MARKERS, 1_220_000, None, "emby-1"),
                    credits(S.SERVER_MARKERS, 1_262_000, None, "plex-1"),
                ],
                Marker(T.CREDITS, 1_239_000, DUR, ("chapters",)),
                "chapters",
            ),
            # an importer plugin's copy is crowd data, not checked against this cut: it never shortens
            (
                [credits(S.SERVER_MARKERS_IMPORTED, 1_265_000, None, "jf-1")],
                Marker(T.CREDITS, 1_239_000, DUR, ("chapters",)),
                "chapters",
            ),
            (
                [
                    credits(S.SERVER_MARKERS, 1_252_000, None, "plex-1"),
                    credits(S.SERVER_MARKERS_IMPORTED, 1_265_000, None, "jf-1"),
                ],
                Marker(T.CREDITS, 1_252_000, DUR, ("chapters", "server_markers")),
                "chapters; start shortened to the server's own marker (plex-1)",
            ),
        ],
        ids=[
            "latest-start",
            "tied-start",
            "split-credits-latest-first-piece",
            "one-server-agrees",
            "open-ended-own-marker-covers",
            "imported-credits-never",
            "imported-beside-own",
        ],
    )
    def test_several_servers_give_the_shortest_skip_whatever_the_order(self, servers, marker, reason):
        for order in _permutations([credits(S.CHAPTERS, 1_239_000), *servers]):
            d = decide(order, ctx(), {})[marker.type]
            assert d.status is DecisionStatus.DECIDED
            assert (d.marker, d.proposed, d.reason) == (marker, None, reason)

    # Avengers Infinity War's shape in a 100-minute movie: Plex splits its credits around a gap (a non-final piece,
    # then the final 12 s), and the chapter's credits run to the end of the file.
    AVENGERS_PLEX = (
        credits(S.SERVER_MARKERS, 5_293_500, 5_897_500, "plex-1"),
        credits(S.SERVER_MARKERS, 5_987_500, None, "plex-1"),
    )

    @pytest.mark.parametrize(
        ("chapter_start", "plex", "start", "reason"),
        [
            # Plex's first piece starts 4.5 s after the chapter: Plex agrees the credits have begun
            (5_289_000, AVENGERS_PLEX, 5_289_000, "chapters"),
            # the chapter starts 33.5 s before Plex's first piece: that piece, not the final 12 s, is the new start
            (
                5_260_000,
                AVENGERS_PLEX,
                5_293_500,
                "chapters; start shortened to the server's own marker (plex-1)",
            ),
            # a piece that covers the chapter's start blocks it
            (
                5_289_000,
                (credits(S.SERVER_MARKERS, 5_250_000, 5_897_500, "plex-1"), AVENGERS_PLEX[1]),
                5_289_000,
                "chapters",
            ),
        ],
        ids=["first-piece-agrees", "first-piece-later", "piece-covers-start"],
    )
    def test_a_server_that_splits_its_credits_never_pulls_the_start_to_its_last_piece(
        self, chapter_start, plex, start, reason
    ):
        x = ctx(is_movie=True, duration=MOVIE_DUR)
        for order in _permutations([credits(S.CHAPTERS, chapter_start), *plex]):
            d = decide(order, x, {})[T.CREDITS]
            assert d.status is DecisionStatus.DECIDED
            by = ("chapters",) if start == chapter_start else ("chapters", "server_markers")
            assert (d.marker, d.reason) == (Marker(T.CREDITS, start, MOVIE_DUR, by), reason)

    # Review repro: a "Preview" chapter overlapping decided credits by more than 10 s is held back by rule 10.
    PREVIEW_CHAPTER = Candidate(T.PREVIEW, 7_111_998, 7_500_000, S.CHAPTERS)
    CREDITS_CHAPTER = credits(S.CHAPTERS, 7_177_449, 7_238_398)

    @pytest.mark.parametrize(
        ("preview", "server", "credits_marker"),
        [
            # the Jellyfin preview segment would pull the preview start past the credits end
            (
                PREVIEW_CHAPTER,
                Candidate(T.PREVIEW, 7_387_142, None, S.SERVER_MARKERS, origin="jf-1"),
                Marker(T.CREDITS, 7_177_449, 7_238_398, ("chapters",)),
            ),
            # Plex's own credits would pull the credits start to 5 s before a shorter preview ends
            (
                Candidate(T.PREVIEW, 7_111_998, 7_200_000, S.CHAPTERS),
                credits(S.SERVER_MARKERS, 7_195_000, None, "plex-1"),
                Marker(T.CREDITS, 7_195_000, 7_238_398, ("chapters", "server_markers")),
            ),
        ],
        ids=["preview-clamp", "credits-clamp"],
    )
    def test_shortening_never_publishes_a_preview_the_overlap_check_held_back(self, preview, server, credits_marker):
        x = ctx(is_movie=True, duration=7_500_000, types=(T.CREDITS, T.PREVIEW))
        for order in _permutations([preview, self.CREDITS_CHAPTER, server]):
            got = decide(order, x, {})
            assert got[T.PREVIEW].status is DecisionStatus.NEEDS_REVIEW
            assert got[T.PREVIEW].marker is None
            assert got[T.PREVIEW].proposed == Marker(T.PREVIEW, preview.start_ms, preview.end_ms, ("chapters",))
            assert got[T.PREVIEW].reason == "preview overlaps credits"
            assert (got[T.CREDITS].status, got[T.CREDITS].marker) == (DecisionStatus.DECIDED, credits_marker)

    def test_contradiction_guard_judges_the_edge_the_sources_decided(self):
        # TheIntroDB and SkipDB agree on 1239 s; Plex's 1257 s shortens it. Judged after shortening, the two
        # sources would be an agreeing pair outside the tolerance of their own result.
        cands = [
            credits(S.THEINTRODB, 1_239_000),
            credits(S.SKIPDB, 1_245_000),
            credits(S.SERVER_MARKERS, 1_257_000, None, "plex-1"),
        ]
        for order in _permutations(cands):
            d = decide(order, ctx(), {})[T.CREDITS]
            assert d.status is DecisionStatus.DECIDED
            assert d.marker == Marker(T.CREDITS, 1_257_000, DUR, ("theintrodb", "skipdb", "server_markers"))

    def test_a_shortened_marker_failing_sanity_needs_review(self, monkeypatch):
        # Unreachable today (shortened credits keep > 10 s, sanity asks >= 3 s), so the minimum is raised to reach it.
        monkeypatch.setattr("media_preview_generator.markers.decide.MIN_SEGMENT_MS", 12_000)
        chapter = credits(S.CHAPTERS, 1_250_000, 1_300_000)
        server = credits(S.SERVER_MARKERS, 1_289_000, None, "plex-1")  # 31 s to the end of the file: sane itself
        d = decide([chapter, server], ctx(), {})[T.CREDITS]
        assert d.status is DecisionStatus.NEEDS_REVIEW
        assert d.marker is None
        assert d.proposed == Marker(T.CREDITS, 1_250_000, 1_300_000, ("chapters",))
        assert d.reason == "start shortened to the server's own marker (plex-1) fails sanity checks"

    def test_a_locked_marker_is_never_shortened(self):
        lock = Marker(T.CREDITS, 1_239_000, DUR, ("user",), locked=True)
        d = decide([self.PLEX_CREDITS, credits(S.CHAPTERS, 1_239_000)], ctx(), {T.CREDITS: lock})[T.CREDITS]
        assert (d.status, d.marker, d.reason) == (DecisionStatus.DECIDED, lock, "locked by user")

    def test_a_server_marker_of_another_type_changes_nothing(self):
        cands = [credits(S.CHAPTERS, 1_239_000), Candidate(T.PREVIEW, 1_255_500, 1_300_000, S.SERVER_MARKERS)]
        d = decide(cands, ctx(types=(T.CREDITS, T.PREVIEW)), {})[T.CREDITS]
        assert (d.marker, d.reason) == (Marker(T.CREDITS, 1_239_000, DUR, ("chapters",)), "chapters")


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("chapters; start shortened to the server's own marker (plex-1)", ("plex-1",)),
        ("sources agree: theintrodb, skipdb; start shortened to the server's own marker (a, b)", ("a", "b")),
        ("chapters; start shortened to the server's own marker", ()),
        ("chapters", None),
        ("start shortened to the server's own marker (plex-1) fails sanity checks", None),
        ("", None),
    ],
)
def test_shortened_by_reads_back_the_reason_decide_writes(reason, expected):
    assert shortened_by(reason) == expected


class TestSanity:
    @pytest.mark.parametrize(
        ("cand", "is_movie", "duration", "problem"),
        [
            (intro(S.SKIPDB, 0, 30_000), False, DUR, None),
            (intro(S.SKIPDB, 462_000, 490_000), False, DUR, None),  # exactly 35% is allowed
            (intro(S.SKIPDB, 462_001, 490_000), False, DUR, "after 35%"),
            (intro(S.SKIPDB, 10_000, 12_000), False, DUR, "too short"),
            (intro(S.SKIPDB, 10_000, 320_000), False, DUR, "too long"),
            (intro(S.SKIPDB, -1, 20_000), False, DUR, "negative"),
            (credits(S.SKIPDB, 990_000), False, DUR, None),
            (credits(S.SKIPDB, 989_999), False, DUR, "before the last 25%"),
            (credits(S.SKIPDB, 1_300_000, 1_302_500), False, DUR, "too short"),  # 2.5s -- too short, any type
            (credits(S.SKIPDB, 5_100_000), True, MOVIE_DUR, None),  # exactly 900 s from the end is allowed
            (credits(S.SKIPDB, 5_099_999), True, MOVIE_DUR, "more than 900 s"),
            (credits(S.SKIPDB, 4_000_000), False, MOVIE_DUR, "before the last 25%"),
            (credits(S.SKIPDB, 1_380_000, 1_410_534), False, DUR, "starts past the end"),
            (credits(S.SKIPDB, 1_300_000, 1_322_000), False, DUR, None),  # end ≤ 2 s past EOF is clamped
            (credits(S.SKIPDB, 1_300_000, 1_322_001), False, DUR, "ends past the end"),
            (Candidate(T.RECAP, 0, 60_000, S.THEINTRODB), False, DUR, None),
            (Candidate(T.PREVIEW, 1_300_000, 1_320_000, S.THEINTRODB), False, DUR, None),
            # -- preview has the credits 10s start tolerance and no 900s movie limit --
            (Candidate(T.PREVIEW, 4_600_000, 5_000_000, S.THEINTRODB), True, MOVIE_DUR, None),
            # -- boundary cells --
            (intro(S.SKIPDB, 10_000, 13_000), False, DUR, None),  # length exactly 3_000 is allowed
            (intro(S.SKIPDB, 10_000, 12_999), False, DUR, "too short"),  # length 2_999 is rejected
            (intro(S.SKIPDB, 10_000, 310_000), False, DUR, None),  # length exactly 300_000 is allowed
            (intro(S.SKIPDB, 10_000, 310_001), False, DUR, "too long"),  # length 300_001 is rejected
            (intro(S.SKIPDB, DUR, DUR + 1000), False, DUR, "past the end"),  # start exactly at duration
            (intro(S.SKIPDB, 0, 20_000), False, 0, "duration unknown"),  # zero duration
            (intro(S.SKIPDB, 0, 20_000), False, -100, "duration unknown"),  # negative duration
            (intro(S.SKIPDB, 50_000, 40_000), False, DUR, "ends before it starts"),  # end before start
            (credits(S.SKIPDB, 1_300_000, 1_290_000), False, DUR, "ends before it starts"),
            # -- intro/recap must not run to the end of the file --
            (intro(S.SKIPDB, 0, SHORT_DUR - 2_000), False, SHORT_DUR, "runs to end of file"),
            (intro(S.SKIPDB, 0, SHORT_DUR - 2_001), False, SHORT_DUR, None),
            (Candidate(T.RECAP, 0, SHORT_DUR, S.THEINTRODB), False, SHORT_DUR, "runs to end of file"),  # explicit end
        ],
    )
    def test_matrix(self, cand, is_movie, duration, problem):
        found = sanity_problem(cand, ctx(is_movie=is_movie, duration=duration))
        if problem is None:
            assert found is None
        else:
            assert found is not None and problem in found

    @pytest.mark.parametrize(
        ("start_b", "expected"),
        [
            (1_300_000, DecisionStatus.DECIDED),
            (1_305_000, DecisionStatus.DECIDED),
            (1_305_001, DecisionStatus.NEEDS_REVIEW),
        ],
    )
    def test_preview_agreement_uses_the_10s_start_tolerance(self, start_b, expected):
        # Ends differ by 10s (> the 5s intro/recap tolerance) to prove preview's agreement check
        # is purely start-based, not end-based.
        cands = [
            Candidate(T.PREVIEW, 1_295_000, 1_310_000, S.THEINTRODB),
            Candidate(T.PREVIEW, start_b, 1_320_000, S.SKIPDB),
        ]
        d = decide(cands, ctx(types=(T.PREVIEW,)), {})[T.PREVIEW]
        assert d.status is expected
        if expected is DecisionStatus.DECIDED:
            assert (d.marker.start_ms, d.marker.end_ms) == (1_295_000, 1_310_000)
            assert d.marker.decided_by == ("theintrodb", "skipdb")
        else:
            assert d.marker is None
            assert d.proposed is not None
            assert (d.proposed.start_ms, d.proposed.end_ms) == (1_295_000, 1_310_000)

    def test_plex_south_park_late_intro_confirms_nothing(self):
        # Prod example (spec §3.1): Plex intro 76.5-112.7 s vs chapters 11-37 s. Chapter decides; Plex disagrees.
        cands = [intro(S.CHAPTERS, 11_000, 37_000), intro(S.SERVER_MARKERS, 76_508, 112_748, "plex-1")]
        d = decide(cands, ctx(), {})[T.INTRO]
        assert (d.marker.start_ms, d.marker.end_ms) == (11_000, 37_000)

    def test_clamped_end_is_used_in_marker(self):
        d = decide([credits(S.CHAPTERS, 1_300_000, 1_321_500)], ctx(), {})[T.CREDITS]
        assert d.marker.end_ms == DUR


class TestRecapAndPreview:
    """Recap/Preview drive the exact same rules as Intro/Credits, end-to-end through decide()."""

    def test_recap_chapters_pick_first(self):
        cands = [Candidate(T.RECAP, 5_000, 30_000, S.CHAPTERS), Candidate(T.RECAP, 60_000, 90_000, S.CHAPTERS)]
        d = decide(cands, ctx(types=(T.RECAP,)), {})[T.RECAP]
        assert d.status is DecisionStatus.DECIDED
        assert (d.marker.start_ms, d.marker.end_ms, d.marker.decided_by) == (5_000, 30_000, ("chapters",))

    def test_preview_chapters_pick_last(self):
        cands = [
            Candidate(T.PREVIEW, 1_200_000, 1_250_000, S.CHAPTERS),
            Candidate(T.PREVIEW, 1_290_000, None, S.CHAPTERS),
        ]
        d = decide(cands, ctx(types=(T.PREVIEW,)), {})[T.PREVIEW]
        assert d.status is DecisionStatus.DECIDED
        assert (d.marker.start_ms, d.marker.end_ms, d.marker.decided_by) == (1_290_000, DUR, ("chapters",))

    @pytest.mark.parametrize(
        ("end_b", "expected"), [(45_000, DecisionStatus.DECIDED), (45_001, DecisionStatus.NEEDS_REVIEW)]
    )
    def test_recap_end_tolerance_is_5s(self, end_b, expected):
        cands = [Candidate(T.RECAP, 10_000, 40_000, S.THEINTRODB), Candidate(T.RECAP, 11_000, end_b, S.SKIPDB)]
        d = decide(cands, ctx(types=(T.RECAP,)), {})[T.RECAP]
        assert d.status is expected
        if expected is DecisionStatus.DECIDED:
            assert (d.marker.start_ms, d.marker.end_ms) == (11_000, 40_000)
        else:
            assert d.marker is None
            assert d.proposed is not None
            assert (d.proposed.start_ms, d.proposed.end_ms) == (10_000, 40_000)


class TestCrossTypeOverlap:
    def test_intro_and_recap_overlap_demotes_both(self):
        cands = [intro(S.CHAPTERS, 0, 40_000), Candidate(T.RECAP, 30_000, 70_000, S.CHAPTERS)]
        out = decide(cands, ctx(types=(T.INTRO, T.RECAP)), {})
        assert out[T.INTRO].status is DecisionStatus.NEEDS_REVIEW
        assert "intro and recap overlap" in out[T.INTRO].reason
        assert out[T.INTRO].proposed == Marker(T.INTRO, 0, 40_000, ("chapters",))
        assert out[T.RECAP].status is DecisionStatus.NEEDS_REVIEW
        assert "intro and recap overlap" in out[T.RECAP].reason
        assert out[T.RECAP].proposed == Marker(T.RECAP, 30_000, 70_000, ("chapters",))

    @pytest.mark.parametrize(
        ("recap_start", "expected"), [(35_000, DecisionStatus.DECIDED), (34_999, DecisionStatus.NEEDS_REVIEW)]
    )
    def test_intro_and_recap_overlap_boundary_is_5s(self, recap_start, expected):
        cands = [intro(S.CHAPTERS, 0, 40_000), Candidate(T.RECAP, recap_start, 70_000, S.CHAPTERS)]
        out = decide(cands, ctx(types=(T.INTRO, T.RECAP)), {})
        intro_marker = Marker(T.INTRO, 0, 40_000, ("chapters",))
        recap_marker = Marker(T.RECAP, recap_start, 70_000, ("chapters",))
        assert out[T.INTRO].status is expected
        assert out[T.RECAP].status is expected
        if expected is DecisionStatus.DECIDED:
            assert (out[T.INTRO].marker, out[T.RECAP].marker) == (intro_marker, recap_marker)
        else:
            assert (out[T.INTRO].proposed, out[T.RECAP].proposed) == (intro_marker, recap_marker)
            assert out[T.INTRO].reason == out[T.RECAP].reason == "intro and recap overlap"

    def test_preview_overlapping_credits_demotes_preview_only(self):
        cands = [credits(S.CHAPTERS, 1_200_000, None), Candidate(T.PREVIEW, 1_190_000, 1_215_000, S.CHAPTERS)]
        out = decide(cands, ctx(types=(T.PREVIEW, T.CREDITS)), {})
        assert out[T.CREDITS].status is DecisionStatus.DECIDED
        assert out[T.PREVIEW].status is DecisionStatus.NEEDS_REVIEW
        assert "preview overlaps credits" in out[T.PREVIEW].reason
        assert out[T.PREVIEW].proposed == Marker(T.PREVIEW, 1_190_000, 1_215_000, ("chapters",))

    def test_locked_marker_is_never_demoted_by_overlap(self):
        locked = {T.INTRO: Marker(T.INTRO, 0, 40_000, ("user",), locked=True)}
        cands = [Candidate(T.RECAP, 30_000, 70_000, S.CHAPTERS)]
        out = decide(cands, ctx(types=(T.INTRO, T.RECAP)), locked)
        assert out[T.INTRO].status is DecisionStatus.DECIDED and out[T.INTRO].marker.locked is True
        assert out[T.RECAP].status is DecisionStatus.NEEDS_REVIEW  # the unlocked side is still demoted
        assert out[T.RECAP].proposed == Marker(T.RECAP, 30_000, 70_000, ("chapters",))

    def test_lock_forces_locked_true_even_if_the_caller_forgot(self):
        # A caller-supplied lock must never be demoted by the overlap checks just because they
        # forgot to also set Marker.locked=True on the value itself.
        locked = {T.INTRO: Marker(T.INTRO, 0, 40_000, ("user",))}  # locked=False, the dataclass default
        cands = [Candidate(T.RECAP, 30_000, 70_000, S.CHAPTERS)]
        out = decide(cands, ctx(types=(T.INTRO, T.RECAP)), locked)
        assert out[T.INTRO].status is DecisionStatus.DECIDED and out[T.INTRO].marker.locked is True
        assert out[T.RECAP].status is DecisionStatus.NEEDS_REVIEW  # the unlocked side is still demoted

    def test_locked_recap_overlapping_a_decided_intro_is_never_demoted(self):
        locked = {T.RECAP: Marker(T.RECAP, 30_000, 70_000, ("user",), locked=True)}
        cands = [intro(S.CHAPTERS, 0, 40_000)]
        out = decide(cands, ctx(types=(T.INTRO, T.RECAP)), locked)
        assert out[T.RECAP].status is DecisionStatus.DECIDED and out[T.RECAP].marker.locked is True
        assert out[T.INTRO].status is DecisionStatus.NEEDS_REVIEW
        assert out[T.INTRO].proposed == Marker(T.INTRO, 0, 40_000, ("chapters",))

    def test_locked_preview_overlapping_credits_is_never_demoted(self):
        locked = {T.PREVIEW: Marker(T.PREVIEW, 1_190_000, 1_215_000, ("user",), locked=True)}
        cands = [credits(S.CHAPTERS, 1_200_000, None)]
        out = decide(cands, ctx(types=(T.PREVIEW, T.CREDITS)), locked)
        assert out[T.PREVIEW].status is DecisionStatus.DECIDED and out[T.PREVIEW].marker.locked is True
        assert out[T.CREDITS].status is DecisionStatus.DECIDED  # credits always keep anyway

    @pytest.mark.parametrize(
        ("preview_end", "expected"), [(1_210_000, DecisionStatus.DECIDED), (1_210_001, DecisionStatus.NEEDS_REVIEW)]
    )
    def test_preview_credits_overlap_boundary_is_10s(self, preview_end, expected):
        cands = [credits(S.CHAPTERS, 1_200_000, None), Candidate(T.PREVIEW, 1_190_000, preview_end, S.CHAPTERS)]
        out = decide(cands, ctx(types=(T.PREVIEW, T.CREDITS)), {})
        assert out[T.PREVIEW].status is expected
        assert out[T.CREDITS].status is DecisionStatus.DECIDED
        if expected is DecisionStatus.NEEDS_REVIEW:
            assert out[T.PREVIEW].proposed == Marker(T.PREVIEW, 1_190_000, preview_end, ("chapters",))


class TestAgreementSearch:
    """Every maximal agreeing set is found, whatever the candidates' values or input order."""

    def test_pair_behind_a_same_source_neighbour_still_conflicts(self):
        # IntroDB 113 agrees with credits_text 117 even though its IntroDB neighbour at 111
        # doesn't; that pair (IntroDB wins, 113) conflicts with credits_text + TheIntroDB (119).
        # Both winners are the IntroDB/TheIntroDB group, which the reason names once.
        cands = [
            intro(S.INTRODB, 60_000, 111_000),
            intro(S.INTRODB, 60_000, 113_000),
            intro(S.CREDITS_TEXT, 60_000, 117_000),
            intro(S.THEINTRODB, 60_000, 119_000),
        ]
        d = decide(cands, ctx(), {})[T.INTRO]
        assert d.status is DecisionStatus.NEEDS_REVIEW
        assert d.marker is None
        assert d.proposed == Marker(T.INTRO, 60_000, 119_000, ("theintrodb", "credits_text"))
        assert d.reason == "agreeing sources conflict: introdb/theintrodb"

    def test_chapter_contradiction_behind_a_neighbour_is_found(self):
        # server (106) + SkipDB (110) agree and their cluster's winner, SkipDB, is 10s from the
        # chapter -- even though IntroDB (103) sits between the chapter and the server marker.
        cands = [
            intro(S.CHAPTERS, 60_000, 100_000),
            intro(S.SERVER_MARKERS, 60_000, 106_000, "plex-1"),
            intro(S.SKIPDB, 60_000, 110_000),
            intro(S.SKIPDB, 60_000, 114_000),
            intro(S.INTRODB, 60_000, 103_000),
        ]
        d = decide(cands, ctx(), {})[T.INTRO]
        assert d.status is DecisionStatus.NEEDS_REVIEW
        assert d.marker is None
        assert d.proposed == Marker(T.INTRO, 60_000, 100_000, ("chapters",))
        assert d.reason == "chapters contradicted by agreeing sources: skipdb, server_markers"

    def test_conflict_is_found_under_a_custom_source_order(self):
        order = ("chapters", "theintrodb", "skipdb", "introdb", "season_audio", "credits_text", "server_markers")
        cands = [
            intro(S.SEASON_AUDIO, 10_000, 40_000),
            intro(S.CREDITS_TEXT, 10_000, 41_000),
            intro(S.THEINTRODB, 120_000, 150_000),
            intro(S.SKIPDB, 130_000, 160_000),
            intro(S.SKIPDB, 135_000, 165_000),
            intro(S.INTRODB, 125_000, 155_000),
        ]
        d = decide(cands, ctx(order=order), {})[T.INTRO]
        assert d.status is DecisionStatus.NEEDS_REVIEW
        assert d.marker is None
        assert d.proposed == Marker(T.INTRO, 130_000, 160_000, ("skipdb", "introdb"))
        assert d.reason == "agreeing sources conflict: skipdb vs season_audio"

    def test_permutation_invariance_of_overlapping_windows(self):
        # Two overlapping windows (sharing the server marker and one credits_text candidate) that
        # agree with each other -- must merge into the same single decision regardless of the
        # order the 4 candidates are given in (24 = 4! permutations).
        base = [
            intro(S.CREDITS_TEXT, 60_000, 105_000),
            intro(S.CREDITS_TEXT, 60_000, 109_000),
            intro(S.SERVER_MARKERS, 60_000, 107_000, "plex-1"),
            intro(S.CREDITS_TEXT, 60_000, 111_000),
        ]
        results = set()
        for perm in itertools.permutations(base):
            d = decide(list(perm), ctx(), {})[T.INTRO]
            results.add((d.status, d.marker, d.proposed, d.reason))
        assert len(results) == 1
        ((status, marker, proposed, _reason),) = results
        assert status is DecisionStatus.DECIDED
        assert proposed is None
        assert (marker.start_ms, marker.end_ms) == (60_000, 105_000)
        assert marker.decided_by == ("credits_text", "server_markers")


# Independent reference for TestMatchesReference, written from the spec §5.5 rules rather than from
# decide.py: its own sanity bounds, brute-force (every subset) search for maximal agreeing sets,
# and no decide.py helpers.
_REF_START_TYPES = (T.INTRO, T.RECAP)
_REF_SOURCES = list(S)
# Rule 7: markers already on servers (an importer plugin's copy included) confirm and may shorten, never decide alone;
# they only move the checked edge of an already decided marker toward a shorter skip.
_REF_SERVER = (S.SERVER_MARKERS, S.SERVER_MARKERS_IMPORTED)
# Rule 6: at "Medium" these only agree -- they don't check this file's cut, or (season audio, owner 2026-09-14) are
# not trusted alone yet.
_REF_AGREEMENT_ONLY = (*_REF_SERVER, S.INTRODB, S.THEINTRODB, S.SEASON_AUDIO, S.SEASON_AUDIO_PREVIOUS)
_REF_LONG_INTRO_CHAPTER = "Intro chapter is much longer than the rest of the season's"
_REF_AUDIO = (S.SEASON_AUDIO, S.SEASON_AUDIO_PREVIOUS)
_REF_AUDIO_WITH_SERVER = (
    "Season audio and a server's own marker agree, but both come from matching audio; needs another source"
)


def _ref_group(c):
    # Rule 8: an importer plugin's copy on a server is the database it imports: SkipDB's is SkipDB again; any other
    # (IntroDB/TheIntroDB, AniSkip until phase 4 measures it, or one that can't be told) is the crowd source.
    if c.source is S.SERVER_MARKERS_IMPORTED and c.copied_from == "skipdb":
        return "skipdb"
    # IntroDB, TheIntroDB and the other importer copies are one crowd source.
    if c.source in (S.INTRODB, S.THEINTRODB, S.SERVER_MARKERS_IMPORTED):
        return "introdb/theintrodb"
    # The previous season's audio is the same method on the same show as this season's.
    return "season_audio" if c.source is S.SEASON_AUDIO_PREVIOUS else c.source.value


def _ref_end(c, duration):
    return duration if c.end_ms is None else min(c.end_ms, duration)


def _ref_value(c, duration):
    return _ref_end(c, duration) if c.type in _REF_START_TYPES else c.start_ms


def _ref_marker_value(m):
    return m.end_ms if m.type in _REF_START_TYPES else m.start_ms


def _ref_tolerance(mtype):
    return 5_000 if mtype in _REF_START_TYPES else 10_000


def _ref_agree(a, b, duration):
    return abs(_ref_value(a, duration) - _ref_value(b, duration)) <= _ref_tolerance(a.type)


def _ref_is_sane(c, x):
    d, start = x.duration_ms, c.start_ms
    if d <= 0 or start < 0 or start >= d:
        return False
    if c.end_ms is not None and (c.end_ms < start or c.end_ms > d + 2_000):
        return False
    end = _ref_end(c, d)
    if end - start < 3_000:
        return False
    if c.type in _REF_START_TYPES:
        return end < d - 2_000 and start * 100 <= 35 * d and end - start <= 300_000
    if start * 100 < 75 * d:
        return False
    return not (c.type is T.CREDITS and x.is_movie and d - start > 900_000)


def _ref_source_rank(source, x):
    position = x.source_order.index(source.value) if source.value in x.source_order else len(x.source_order)
    return position, _REF_SOURCES.index(source)


def _ref_rank(c, x):
    """Best first: source rank, higher confidence (non-finite = 0.0), then the shorter skip (checked edge first)."""
    confidence = c.confidence if math.isfinite(c.confidence) else 0.0
    length_order = (_ref_end(c, x.duration_ms), -c.start_ms)
    if c.type not in _REF_START_TYPES:
        length_order = (-c.start_ms, _ref_end(c, x.duration_ms))
    return (*_ref_source_rank(c.source, x), -confidence, *length_order, -1 if c.end_ms is None else c.end_ms)


def _ref_shorter(mtype, checked, candidates, x, sources):
    """A marker keeping `checked` and taking the latest start / earliest end among `candidates`.

    Returns the marker (credited to `sources` plus the candidates holding that edge) and the edge value.
    """
    d = x.duration_ms
    if mtype in _REF_START_TYPES:
        other = max(c.start_ms for c in candidates)
        holders = {c.source for c in candidates if c.start_ms == other}
        start, end = other, checked
    else:
        other = min(_ref_end(c, d) for c in candidates)
        holders = {c.source for c in candidates if _ref_end(c, d) == other}
        start, end = checked, other
    credited = sorted(set(sources) | holders, key=lambda s: _ref_source_rank(s, x))
    return Marker(mtype, start, end, tuple(s.value for s in credited)), other


def _ref_own_marker(c, x):
    return Marker(c.type, c.start_ms, _ref_end(c, x.duration_ms), (c.source.value,))


def _ref_agreeing_sets(cands, x):
    """Maximal mutually agreeing subsets spanning >= 2 groups, ascending by their smallest compared value."""
    n, d = len(cands), x.duration_ms
    neighbours = [sum(1 << j for j in range(n) if _ref_agree(cands[i], cands[j], d)) for i in range(n)]
    cliques = [m for m in range(1, 1 << n) if all(neighbours[i] & m == m for i in range(n) if m >> i & 1)]
    maximal = [m for m in cliques if not any(not m >> k & 1 and neighbours[k] & m == m for k in range(n))]
    sets = [[cands[i] for i in range(n) if m >> i & 1] for m in maximal]
    sets = [
        s
        for s in sets
        if len({_ref_group(c) for c in s}) >= 2
        # ruling G3: season audio and markers already on servers never agree on their own
        and any(c.source not in (*_REF_SERVER, S.SEASON_AUDIO, S.SEASON_AUDIO_PREVIOUS) for c in s)
    ]
    return sorted(sets, key=lambda s: min(_ref_value(c, d) for c in s))


def _ref_compose(members, mtype, x):
    d = x.duration_ms
    confirmed = [c for c in members if any(_ref_group(o) != _ref_group(c) and _ref_agree(c, o, d) for o in members)]
    suppliers = [c for c in confirmed if c.source not in _REF_SERVER]
    # Checked edge: the best-ranked agreeing non-server candidate (source order first); other edge: the safer value of
    # every confirmed candidate, server markers included (they may shorten the skip).
    winner = min(suppliers, key=lambda c: _ref_rank(c, x))
    if mtype in _REF_START_TYPES:
        start, end = max(c.start_ms for c in confirmed), _ref_end(winner, d)
        edge = [c for c in confirmed if c.start_ms == start]
    else:
        start, end = winner.start_ms, min(_ref_end(c, d) for c in confirmed)
        edge = [c for c in confirmed if _ref_end(c, d) == end]
    credited = {winner.source, *(c.source for c in edge), *(c.source for c in confirmed if _ref_agree(winner, c, d))}
    decided_by = tuple(s.value for s in sorted(credited, key=lambda s: _ref_source_rank(s, x)))
    return Marker(mtype, start, end, decided_by), winner


def _ref_decide_type(mtype, cands, x):
    d, tol = x.duration_ms, _ref_tolerance(mtype)
    of_type = [c for c in cands if c.type is mtype]
    sane = [c for c in of_type if _ref_is_sane(c, x)]

    def review(proposed, reason):
        return TypeDecision(mtype, DecisionStatus.NEEDS_REVIEW, None, proposed, reason)

    if not sane:
        reason = f"{len(of_type)} candidate(s) failed sanity checks" if of_type else "no evidence"
        return TypeDecision(mtype, DecisionStatus.NO_EVIDENCE, None, None, reason)
    chapters = [c for c in sane if c.source is S.CHAPTERS]
    others = [c for c in sane if c.source is not S.CHAPTERS]
    agreeing_sets = _ref_agreeing_sets(others if chapters else sane, x)
    guard_pool = others
    if chapters:
        if mtype in _REF_START_TYPES:
            chosen = min(chapters, key=lambda c: (c.start_ms, _ref_end(c, d)))
        else:
            chosen = max(chapters, key=lambda c: (c.start_ms, -_ref_end(c, d)))
        guard_pool = [c for c in sane if c is not chosen]
        result, reason = _ref_own_marker(chosen, x), "chapters"
        for members in agreeing_sets:
            marker, _ = _ref_compose(members, mtype, x)
            if abs(_ref_marker_value(marker) - _ref_marker_value(result)) > tol:
                return review(result, "chapters contradicted by agreeing sources: " + ", ".join(marker.decided_by))
        backing = [c for c in others if abs(_ref_value(c, d) - _ref_marker_value(result)) <= tol]
        # Finding F1: an intro chapter far longer than the season's other intro chapters needs one agreeing source.
        limit = x.intro_chapter_limit_ms
        suspect = mtype is T.INTRO and limit is not None and result.end_ms - result.start_ms > limit
        if suspect and not [c for c in backing if c.source not in _REF_SERVER]:
            return review(result, _REF_LONG_INTRO_CHAPTER)
        if len({_ref_group(c) for c in backing}) >= (1 if suspect else 2):
            chapter = result
            everyone = {S.CHAPTERS, *(c.source for c in backing)}
            shorter, other = _ref_shorter(mtype, _ref_marker_value(chapter), backing, x, everyone)
            own_other = chapter.start_ms if mtype in _REF_START_TYPES else chapter.end_ms
            if (other > own_other) if mtype in _REF_START_TYPES else (other < own_other):
                if not _ref_is_sane(Candidate(mtype, shorter.start_ms, shorter.end_ms, S.CHAPTERS), x):
                    return review(chapter, "chapters and agreeing sources disagree on the other edge")
                result = Marker(mtype, shorter.start_ms, shorter.end_ms, shorter.decided_by)
            elif suspect:
                credited = sorted(everyone, key=lambda s: _ref_source_rank(s, x))
                result = Marker(mtype, chapter.start_ms, chapter.end_ms, tuple(s.value for s in credited))
    elif agreeing_sets:
        composed = [_ref_compose(members, mtype, x) for members in agreeing_sets]
        values = [_ref_marker_value(marker) for marker, _ in composed]
        if any(abs(a - b) > tol for a, b in itertools.combinations(values, 2)):
            ranked = [
                composed[i] for i in sorted(range(len(composed)), key=lambda i: (_ref_rank(composed[i][1], x), i))
            ]
            names = " vs ".join(dict.fromkeys(_ref_group(winner) for _, winner in ranked))
            return review(ranked[0][0], f"agreeing sources conflict: {names}")
        merged = list({id(c): c for members in agreeing_sets for c in members}.values())
        result, winner = _ref_compose(merged, mtype, x)
        if not _ref_is_sane(Candidate(mtype, result.start_ms, result.end_ms, winner.source), x):
            return review(_ref_own_marker(winner, x), "agreeing sources disagree on the other edge")
        reason = "sources agree: " + ", ".join(result.decided_by)
    else:
        groups = sorted({_ref_group(c) for c in sane})
        ranked = sorted(sane, key=lambda c: _ref_rank(c, x))

        def may_decide_alone(c):
            # SkipDB alone: intros and recaps only
            return c.source not in _REF_AGREEMENT_ONLY and (mtype in _REF_START_TYPES or c.source is not S.SKIPDB)

        proposal = next((c for c in ranked if may_decide_alone(c)), None)
        if x.publish_when != "medium" or proposal is None or len(groups) > 1:
            disagree = any(
                _ref_group(a) != _ref_group(b) and not _ref_agree(a, b, d) for a, b in itertools.combinations(sane, 2)
            )
            kinds = {c.source for c in sane}
            # ruling G3 again: only season audio and markers already on servers, and they agree
            audio_with_server = (
                kinds <= {*_REF_AUDIO, *_REF_SERVER} and kinds & set(_REF_AUDIO) and kinds & set(_REF_SERVER)
            )
            if disagree:
                reason = f"sources disagree: {', '.join(groups)}"
            else:
                reason = _REF_AUDIO_WITH_SERVER if audio_with_server else "sources don't agree yet"
            return review(_ref_own_marker(ranked[0], x), reason)
        if not all(_ref_agree(a, b, d) for a, b in itertools.combinations(sane, 2)):
            return review(_ref_own_marker(proposal, x), "source disagrees with itself")
        result, _ = _ref_shorter(mtype, _ref_value(proposal, d), sane, x, {proposal.source})
        if not _ref_is_sane(Candidate(mtype, result.start_ms, result.end_ms, proposal.source), x):
            return review(_ref_own_marker(proposal, x), "sources disagree on the other edge")
        reason = f"single source ({proposal.source.value})"

    far = [c for c in guard_pool if abs(_ref_value(c, d) - _ref_marker_value(result)) > tol]
    contradicting = {
        _ref_group(c)
        for a, b in itertools.combinations(far, 2)
        if _ref_group(a) != _ref_group(b) and _ref_agree(a, b, d)
        for c in (a, b)
    }
    if contradicting:
        return review(result, "agreeing sources contradict the result: " + ", ".join(sorted(contradicting)))

    return TypeDecision(mtype, DecisionStatus.DECIDED, result, None, reason)


def _ref_shorten(decision, cands, x):
    """Rule 7, run on decisions that are still decided after the overlap checks: a credits/preview start moves later
    to a server's own detection. Any own marker covering or agreeing with the start blocks it; each server offers its
    first start inside the skip, the latest offer wins."""
    mtype, result, d = decision.type, decision.marker, x.duration_ms
    tol = _ref_tolerance(mtype)
    if mtype in _REF_START_TYPES or result.locked:
        return decision
    own = [c for c in cands if c.type is mtype and _ref_is_sane(c, x) and c.source is S.SERVER_MARKERS]
    if any(c.start_ms <= result.start_ms <= _ref_end(c, d) or abs(c.start_ms - result.start_ms) <= tol for c in own):
        return decision
    offers = {}
    for c in own:
        if result.start_ms + tol < c.start_ms < result.end_ms - tol:
            offers[c.origin] = min(c.start_ms, offers.get(c.origin, c.start_ms))
    if not offers:
        return decision
    start = max(offers.values())
    credited = {S(v) for v in result.decided_by} | {S.SERVER_MARKERS}
    decided_by = tuple(s.value for s in sorted(credited, key=lambda s: _ref_source_rank(s, x)))
    origins = sorted(o for o, v in offers.items() if v == start and o)
    note = "start shortened to the server's own marker" + (f" ({', '.join(origins)})" if origins else "")
    if not _ref_is_sane(Candidate(mtype, start, result.end_ms, S.CHAPTERS), x):
        return TypeDecision(mtype, DecisionStatus.NEEDS_REVIEW, None, result, f"{note} fails sanity checks")
    shortened = Marker(mtype, start, result.end_ms, decided_by)
    return TypeDecision(mtype, DecisionStatus.DECIDED, shortened, None, f"{decision.reason}; {note}")


def _ref_overlap(a, b):
    return max(0, min(a.end_ms, b.end_ms) - max(a.start_ms, b.start_ms))


def _ref_decide(cands, x, locked):
    out = {}
    for mtype in T:
        lock = locked.get(mtype)
        if isinstance(lock, Marker) and lock.type is mtype:
            out[mtype] = TypeDecision(
                mtype,
                DecisionStatus.DECIDED,
                Marker(lock.type, lock.start_ms, lock.end_ms, lock.decided_by, True),
                None,
                "locked by user",
            )
        elif mtype not in x.enabled_types:
            out[mtype] = TypeDecision(mtype, DecisionStatus.DISABLED, None, None, "detection off")
        else:
            out[mtype] = _ref_decide_type(mtype, cands, x)

    def demote(decision, reason):
        return TypeDecision(decision.type, DecisionStatus.NEEDS_REVIEW, None, decision.marker, reason)

    decided = {t: o.marker for t, o in out.items() if o.status is DecisionStatus.DECIDED}
    if T.INTRO in decided and T.RECAP in decided and _ref_overlap(decided[T.INTRO], decided[T.RECAP]) > 5_000:
        for t in (T.INTRO, T.RECAP):
            if not decided[t].locked:
                out[t] = demote(out[t], "intro and recap overlap")
    if T.PREVIEW in decided and T.CREDITS in decided and _ref_overlap(decided[T.PREVIEW], decided[T.CREDITS]) > 10_000:
        if not decided[T.PREVIEW].locked:
            out[T.PREVIEW] = demote(out[T.PREVIEW], "preview overlaps credits")
    # Rule 7 runs last, on what is still decided.
    return {t: _ref_shorten(o, cands, x) if o.status is DecisionStatus.DECIDED else o for t, o in out.items()}


_EVERY_REASON = (
    "locked by user",
    "detection off",
    "no evidence",
    "failed sanity checks",
    "chapters",
    "chapters contradicted by agreeing sources",
    "agreeing sources conflict",
    "agreeing sources disagree on the other edge",
    "chapters and agreeing sources disagree on the other edge",
    "sources disagree on the other edge",
    "agreeing sources contradict the result",
    "sources agree",
    "single source",
    "source disagrees with itself",
    "sources disagree",
    "sources don't agree yet",
    "shortened to the server's own marker",
    "intro and recap overlap",
    "preview overlaps credits",
    _REF_LONG_INTRO_CHAPTER,
    _REF_AUDIO_WITH_SERVER,
)


def _reason_kind(reason):
    """The longest known reason fragment in `reason`, so "chapters contradicted..." isn't counted as "chapters"."""
    return max((r for r in _EVERY_REASON if r in reason), key=len)


# Server markers come up twice as often, so several servers' markers often land in one file.
_NON_CHAPTER_SOURCES = [s for s in S if s not in (S.USER, S.CHAPTERS)] + [S.SERVER_MARKERS]


def _random_candidates(rng, mtype, duration, anchor):
    """Candidates on a coarse grid near `anchor`, so agreeing sets bridge and conflict, and compared
    values and cross-type overlaps often land exactly on their tolerance boundaries."""
    step = _ref_tolerance(mtype) // 4
    if rng.random() < 0.35:
        # A bridge: the middle centre agrees with both outer ones, which don't agree with each other.
        offsets = [0, 3, 6]
    else:
        offsets = list(itertools.accumulate(rng.choice((3, 4, 5, 6, 8)) for _ in range(rng.choice((0, 1, 2, 2)))))
        offsets.insert(0, 0)
    first = anchor + step * rng.randint(-4, 4)
    centres = [first + step * offset for offset in offsets]
    if rng.random() < 0.25:
        # One source (or the IntroDB pair) only: what "medium" decides on. The sources that may decide alone come up
        # more often: credit text three times (besides chapters the only one for credits and previews), SkipDB twice
        # (intros and recaps). Season audio only agrees in phase 2.
        crowd = [S.THEINTRODB, S.INTRODB, S.SERVER_MARKERS_IMPORTED]
        alone = [*_NON_CHAPTER_SOURCES, S.CHAPTERS, S.CREDITS_TEXT, S.CREDITS_TEXT, S.SKIPDB]
        sources = rng.choice((crowd, [rng.choice(alone)]))
        count, chapter_count = rng.randint(1, 4), 0
    else:
        sources, count, chapter_count = _NON_CHAPTER_SOURCES, rng.randint(2, 7), rng.choice((0, 0, 1, 2))
    out = []
    for i in range(count):
        source = S.CHAPTERS if i < chapter_count else rng.choice(sources)
        value = rng.choice(centres) + step * rng.choice((-1, 0, 0, 1))
        if source is S.CHAPTERS and rng.random() < 0.5:
            value = centres[0]  # a chapter at one end of a bridge, the far pair at the other
        if mtype in _REF_START_TYPES:
            end = None if rng.random() < 0.04 else value
            start = max(0, value - rng.choice((2_000, 5_000, 20_000, 30_000, 45_000, 60_000)))
        else:
            start = value
            end = rng.choice(
                (None, None, start + 2_000, start + 5_000, start + 30_000, duration + 2_000, duration + 2_001)
            )
        confidence = rng.choice((0.5, 0.9, 1.0, 1.0, 1.0, 0.0, math.nan, math.inf))
        c = Candidate(mtype, start, end, source, confidence, f"server-{rng.randint(1, 3)}")
        out.append(c)
        roll = rng.random()
        if roll < 0.08:
            out.append(c)  # the same object twice
        elif roll < 0.16:
            out.append(Candidate(mtype, start, end, source, c.confidence, "another-server"))
        elif roll < 0.3 and source is S.CHAPTERS:
            other_end = start + 30_000 if end is None else end + rng.choice((-15_000, 15_000))
            out.append(Candidate(mtype, start, other_end, source, c.confidence))  # tied chapter start
    return out


def _random_file(rng):
    duration = rng.choice((240_000, 1_320_000, 1_320_000, 2_700_000, 6_000_000))
    is_movie = rng.random() < (0.8 if duration == 6_000_000 else 0.1)
    order = ORDER if rng.random() < 0.4 else tuple(rng.sample(ORDER, rng.randint(2, len(ORDER))))
    enabled = frozenset(t for t in T if rng.random() < 0.85)
    x = DecisionContext(duration, is_movie, rng.choice(("high", "medium")), enabled, order)
    # Intro and recap share an anchor, as do credits and preview, so their decided markers overlap.
    start_anchor = rng.randrange(50_000, min(150_000, duration // 2), 5_000)
    end_anchor = duration * rng.choice((80, 86, 93)) // 100 // 5_000 * 5_000
    cands = [
        c
        for t in T
        if rng.random() < 0.8
        for c in _random_candidates(rng, t, duration, start_anchor if t in _REF_START_TYPES else end_anchor)
    ]
    locked = {}
    for t in T:
        roll = rng.random()
        start = start_anchor - 20_000 if t in _REF_START_TYPES else end_anchor
        if roll < 0.06:
            locked[t] = Marker(t, start, start + 40_000, ("user",), locked=rng.random() < 0.5)
        elif roll < 0.08:
            wrong_type = T.CREDITS if t is T.INTRO else T.INTRO
            locked[t] = Marker(wrong_type, start, start + 40_000, ("user",))
    # The season's intro-chapter limit lands inside the 2-60 s spread of generated intro lengths half of the time. Drawn
    # last, so every other draw of a seed stays what it was before the limit existed.
    if rng.random() < 0.5:
        x = replace(x, intro_chapter_limit_ms=rng.choice((20_000, 35_000, 50_000)))
    # The database an importer plugin's copy names (rule 8), drawn after everything else for the same reason. A copy
    # that appears twice (the same object) gets one name.
    named = {}
    for c in cands:
        if c.source is S.SERVER_MARKERS_IMPORTED and id(c) not in named:
            named[id(c)] = replace(c, copied_from=rng.choice(("", "introdb", "skipdb", "aniskip")))
    cands = [named.get(id(c), c) for c in cands]
    return cands, x, locked


class TestMatchesReference:
    """decide() against the independent reference above, over dense random files.

    Clustered values make agreeing sets bridge, overlap and conflict; files also mix chapters with
    tied starts, locks (including mistyped ones), both publish settings, all four types, missing
    and overshooting ends, movies, partial/shuffled source orders, duplicate candidates and several
    server markers. Every result must equal the reference exactly and survive shuffling the input.
    """

    @pytest.mark.parametrize("seed", [20260913, 7, 1234])
    def test_decide_matches_reference_and_ignores_input_order(self, seed):
        rng = random.Random(seed)
        seen = set()
        for _ in range(1000):
            cands, x, locked = _random_file(rng)
            got = decide(cands, x, locked)
            assert got == _ref_decide(cands, x, locked), (cands, x, locked)
            shuffled = cands[:]
            rng.shuffle(shuffled)
            assert decide(shuffled, x, locked) == got, (cands, shuffled, x, locked)
            seen.update(_reason_kind(dec.reason) for dec in got.values())
        # A generator that stopped reaching a rule would make this test pass vacuously.
        assert seen == set(_EVERY_REASON)


def _checked_edge(marker):
    return marker.end_ms if marker.type in _REF_START_TYPES else marker.start_ms


def _decide_unshortened(cands, x, locked):
    """decide() without rule 7's last step (a server's own credits/preview start moving a decided start later)."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(decide_module, "_shorten_to_server_markers", lambda decision, sane, ctx: decision)
        return decide(cands, x, locked)


def _unlocked_decided(decision):
    return decision.status is DecisionStatus.DECIDED and not decision.marker.locked


class TestProperties:
    """Properties of the finished decide() over the same dense random files as the reference (chapters, every crowd
    source, SkipDB, season audio and its hint, credit text, several servers' own markers and importer copies naming each
    database, locks, both levels). The phase-1 deep review's fuzz checked the first three; the rest pin rulings R2, G3
    and rules 6-7 as properties rather than cells."""

    SEEDS = (20260913, 7, 1234)
    FILES = 1000

    def _files(self, seed):
        rng = random.Random(seed)
        for _ in range(self.FILES):
            yield _random_file(rng)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_rule_7_only_moves_a_decided_credits_or_preview_start_later(self, seed):
        moved = 0
        for cands, x, locked in self._files(seed):
            got, raw = decide(cands, x, locked), _decide_unshortened(cands, x, locked)
            for mtype in T:
                shortened, before = got[mtype], raw[mtype]
                if mtype in _REF_START_TYPES:  # intros and recaps are never moved
                    assert shortened == before, (cands, x)
                elif _unlocked_decided(shortened):  # never turns Needs review into decided, never lengthens
                    assert _unlocked_decided(before), (cands, x)
                    assert shortened.marker.start_ms >= before.marker.start_ms, (cands, x)
                    assert shortened.marker.end_ms == before.marker.end_ms, (cands, x)
                    moved += shortened.marker != before.marker
                elif _unlocked_decided(before):  # the moved start failed sanity: the unmoved marker is proposed
                    assert shortened.status is DecisionStatus.NEEDS_REVIEW, (cands, x)
                    assert shortened.proposed == before.marker and shortened.reason.endswith("fails sanity checks")
                else:
                    assert shortened == before, (cands, x)
        assert moved  # a generator that stopped reaching rule 7 would pass this vacuously

    @pytest.mark.parametrize("seed", SEEDS)
    def test_season_audio_and_markers_on_servers_never_decide_on_their_own(self, seed):
        only_agree = {*_REF_AUDIO, *_REF_SERVER}
        for cands, x, locked in self._files(seed):
            for decision in decide(cands, x, locked).values():
                if _unlocked_decided(decision):
                    assert not {S(s) for s in decision.marker.decided_by} <= only_agree, (cands, x, decision)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_markers_on_servers_never_supply_the_checked_edge_unless_they_shortened_it(self, seed):
        for cands, x, locked in self._files(seed):
            for mtype, decision in decide(cands, x, locked).items():
                if not _unlocked_decided(decision):
                    continue
                sane = [c for c in cands if c.type is mtype and _ref_is_sane(c, x)]
                edge = _checked_edge(decision.marker)
                from_a_source = any(_ref_value(c, x.duration_ms) == edge for c in sane if c.source not in _REF_SERVER)
                moved_by_a_server = shortened_by(decision.reason) is not None and any(
                    c.start_ms == edge for c in sane if c.source is S.SERVER_MARKERS
                )
                assert from_a_source or moved_by_a_server, (cands, x, decision)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_markers_on_servers_only_shorten_a_skip_decided_on_the_same_edge(self, seed):
        compared = 0
        for cands, x, locked in self._files(seed):
            got = decide(cands, x, locked)
            without = decide([c for c in cands if c.source not in _REF_SERVER], x, locked)
            for mtype in T:
                with_servers, bare = got[mtype], without[mtype]
                if not (_unlocked_decided(with_servers) and _unlocked_decided(bare)):
                    continue
                if shortened_by(with_servers.reason) is None and (
                    _checked_edge(with_servers.marker) != _checked_edge(bare.marker)
                ):
                    continue  # a server's agreement confirmed another source's edge: another decision, not a longer one
                compared += 1
                assert with_servers.marker.start_ms >= bare.marker.start_ms, (cands, x)
                assert with_servers.marker.end_ms <= bare.marker.end_ms, (cands, x)
        assert compared

    @pytest.mark.parametrize("seed", SEEDS)
    def test_medium_only_adds_single_source_decisions_to_what_high_decides(self, seed):
        for cands, x, locked in self._files(seed):
            high = decide(cands, replace(x, publish_when="high"), locked)
            medium = decide(cands, replace(x, publish_when="medium"), locked)
            for mtype in T:
                if medium[mtype] == high[mtype]:
                    continue
                if high[mtype].status is DecisionStatus.DECIDED:
                    # Only a decision Medium adds of the other type in the pair can take it back (rules 9-10).
                    assert medium[mtype].status is DecisionStatus.NEEDS_REVIEW, (cands, x)
                    assert medium[mtype].reason in ("intro and recap overlap", "preview overlaps credits")
                elif medium[mtype].status is DecisionStatus.DECIDED:
                    assert medium[mtype].reason.startswith("single source ("), (cands, x)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_every_decided_marker_passes_the_sanity_checks(self, seed):
        for cands, x, locked in self._files(seed):
            for mtype, decision in decide(cands, x, locked).items():
                if _unlocked_decided(decision):
                    marker = decision.marker
                    probe = Candidate(mtype, marker.start_ms, marker.end_ms, S.CHAPTERS)
                    assert sanity_problem(probe, x) is None, (cands, x, decision)


class TestSeasonAudioSources:
    """Spec §5.3/§5.5 and the owner decision of 2026-09-14: season audio never decides alone in phase 2, whether it
    matched this season or is the previous season's hint, and the two are one independent source."""

    DUR = 1_321_472
    ORDER = ("chapters", "theintrodb", "introdb", "skipdb", "season_audio", "season_audio_previous", "credits_text",
             "server_markers", "server_markers_imported")  # fmt: skip

    def _ctx(self, publish_when):
        return DecisionContext(self.DUR, False, publish_when, frozenset({MarkerType.INTRO}), self.ORDER)

    @pytest.mark.parametrize("publish_when", ["high", "medium"])
    @pytest.mark.parametrize(("source", "origin"), [(S.SEASON_AUDIO, "10/10"), (S.SEASON_AUDIO_PREVIOUS, "4/4")])
    def test_season_audio_alone_never_decides(self, publish_when, source, origin):
        c = [Candidate(MarkerType.INTRO, 126_000, 157_000, source, 1.0, origin)]
        d = decide(c, self._ctx(publish_when), {})[MarkerType.INTRO]
        assert (d.status, d.reason) == (DecisionStatus.NEEDS_REVIEW, "sources don't agree yet")
        assert d.proposed == Marker(MarkerType.INTRO, 126_000, 157_000, (source.value,))

    @pytest.mark.parametrize("publish_when", ["high", "medium"])
    def test_hint_and_same_season_audio_are_one_source(self, publish_when):
        c = [
            Candidate(MarkerType.INTRO, 126_000, 157_000, Source.SEASON_AUDIO_PREVIOUS, 1.0, "4/4"),
            Candidate(MarkerType.INTRO, 126_500, 157_500, Source.SEASON_AUDIO, 1.0, "1/1"),
        ]
        assert decide(c, self._ctx(publish_when), {})[MarkerType.INTRO].status is DecisionStatus.NEEDS_REVIEW

    def test_hint_confirmed_by_an_independent_source_decides_at_high(self):
        c = [
            Candidate(MarkerType.INTRO, 126_000, 157_000, Source.SEASON_AUDIO_PREVIOUS, 1.0, "4/4"),
            Candidate(MarkerType.INTRO, 127_000, 158_800, Source.SKIPDB, 0.9),
        ]
        d = decide(c, self._ctx("high"), {})[MarkerType.INTRO]
        assert d.status is DecisionStatus.DECIDED
        assert d.marker.decided_by == ("skipdb", "season_audio_previous")
        # The agreed end comes from the first agreeing source in the user's order (SkipDB), the start is the later one.
        assert (d.marker.start_ms, d.marker.end_ms) == (127_000, 158_800)

    @pytest.mark.parametrize("publish_when", ["high", "medium"])
    @pytest.mark.parametrize(
        ("audio", "server"),
        [
            (S.SEASON_AUDIO, S.SERVER_MARKERS),
            (S.SEASON_AUDIO_PREVIOUS, S.SERVER_MARKERS),
            (S.SEASON_AUDIO, S.SERVER_MARKERS_IMPORTED),
        ],
        ids=["audio-and-server", "hint-and-server", "audio-and-importer-copy"],
    )
    def test_season_audio_and_markers_on_servers_alone_need_review(self, publish_when, audio, server):
        # Ruling G3: a server's own intro detection matches audio across episodes too, so they aren't independent.
        c = [
            Candidate(MarkerType.INTRO, 126_000, 157_000, audio, 1.0, "9/9"),
            Candidate(MarkerType.INTRO, 125_000, 158_000, server, 1.0, "plex-1"),
        ]
        d = decide(c, self._ctx(publish_when), {})[MarkerType.INTRO]
        assert (d.status, d.marker) == (DecisionStatus.NEEDS_REVIEW, None)
        # Said plainly: "sources don't agree yet" would be wrong, they do agree.
        assert d.reason == (
            "Season audio and a server's own marker agree, but both come from matching audio; needs another source"
        )

    @pytest.mark.parametrize(
        ("others", "reason"),
        [
            ([], "sources don't agree yet"),  # season audio alone (ruling R2)
            ([(S.SEASON_AUDIO_PREVIOUS, 126_500)], "sources don't agree yet"),  # this season's audio and the hint
            ([(S.SERVER_MARKERS, 140_000)], "sources disagree: season_audio, server_markers"),
        ],
        ids=["alone", "with-the-hint", "server-disagrees"],
    )
    def test_other_audio_reviews_keep_their_reasons(self, others, reason):
        c = [
            Candidate(MarkerType.INTRO, 126_000, 157_000, S.SEASON_AUDIO, 1.0, "9/9"),
            *(Candidate(MarkerType.INTRO, start, start + 31_000, source, 1.0, "x") for source, start in others),
        ]
        d = decide(c, self._ctx("medium"), {})[MarkerType.INTRO]
        assert (d.status, d.reason) == (DecisionStatus.NEEDS_REVIEW, reason)

    def test_season_audio_and_markers_on_servers_publish_once_an_outside_source_agrees(self):
        c = [
            Candidate(MarkerType.INTRO, 126_000, 157_000, Source.SEASON_AUDIO, 1.0, "9/9"),
            Candidate(MarkerType.INTRO, 125_000, 158_000, Source.SERVER_MARKERS, 1.0, "plex-1"),
            Candidate(MarkerType.INTRO, 124_000, 156_000, Source.THEINTRODB),
        ]
        d = decide(c, self._ctx("high"), {})[MarkerType.INTRO]
        assert d.status is DecisionStatus.DECIDED
        assert d.marker == Marker(MarkerType.INTRO, 126_000, 156_000, ("theintrodb", "season_audio", "server_markers"))

    @pytest.mark.parametrize("server", [S.SERVER_MARKERS, S.SERVER_MARKERS_IMPORTED])
    def test_theintrodb_and_markers_on_servers_are_unchanged(self, server):
        c = [
            Candidate(MarkerType.INTRO, 124_000, 156_000, Source.THEINTRODB),
            Candidate(MarkerType.INTRO, 125_000, 158_000, server, 1.0, "plex-1"),
        ]
        d = decide(c, self._ctx("high"), {})[MarkerType.INTRO]
        expected = DecisionStatus.NEEDS_REVIEW if server is S.SERVER_MARKERS_IMPORTED else DecisionStatus.DECIDED
        assert d.status is expected  # an importer plugin's copy is TheIntroDB's own data (rule 8)

    def test_same_season_audio_confirmed_by_an_agreement_only_source_decides_at_high(self):
        c = [
            Candidate(MarkerType.INTRO, 126_000, 157_000, Source.SEASON_AUDIO, 1.0, "7/9"),
            Candidate(MarkerType.INTRO, 125_000, 160_000, Source.INTRODB),
        ]
        d = decide(c, self._ctx("high"), {})[MarkerType.INTRO]
        assert d.status is DecisionStatus.DECIDED
        assert (d.marker.start_ms, d.marker.end_ms, d.marker.decided_by) == (
            126_000,
            160_000,
            ("introdb", "season_audio"),
        )


class TestSeasonIntroChapterLimit:
    """Finding F1 (phase-1 scale run): an intro chapter far longer than the season's other intro chapters is story, not
    an intro, so it needs an agreeing source."""

    REASON = "Intro chapter is much longer than the rest of the season's"

    @pytest.mark.parametrize(
        ("others", "limit"),
        [
            ((), None),
            ((14_000,), None),  # one other episode isn't a season's habit
            ((14_000, 14_000), 44_000),  # median + 30 s is the larger bound for short intros
            ((13_000, 14_000, 15_000), 44_000),
            ((60_000, 70_000, 80_000), 140_000),  # twice the median is the larger bound for long intros
            ((3_000, 6_931, 7_000, 86_545), 36_965),  # an even count takes the mean of the middle two (6_965.5)
        ],
    )
    def test_limit_needs_two_other_episodes(self, others, limit):
        assert intro_chapter_limit_ms(others) == limit

    def test_the_chosen_intro_chapter_length_uses_the_decision_rules(self):
        chapters = [
            intro(S.CHAPTERS, 60_000, 90_000),
            intro(S.CHAPTERS, 5_000, 30_000),  # the first intro chapter is the one decided
            intro(S.CHAPTERS, 0, 480_000),  # an 8-minute "Intro" fails sanity and never counts
            credits(S.CHAPTERS, 1_290_000),
            intro(S.SKIPDB, 1_000, 100_000),
        ]
        assert intro_chapter_length_ms(chapters, DUR) == 25_000
        assert intro_chapter_length_ms([intro(S.CHAPTERS, 0, None)], SHORT_DUR) is None
        assert intro_chapter_length_ms([], DUR) is None

    @pytest.mark.parametrize(
        ("length", "others"),
        [
            (88_000, (13_000, 14_000, 15_000)),  # Mr. Robot S04E01: 88 s against 14 s
            (125_834, (3_000, 6_931, 7_000, 86_545)),  # Reservation Dogs S01E05: 126 s of story
            (86_545, (3_000, 6_931, 7_000, 125_834)),  # Reservation Dogs S01E06: 87 s of story
        ],
        ids=["mr-robot-s04e01", "reservation-dogs-s01e05", "reservation-dogs-s01e06"],
    )
    @pytest.mark.parametrize("publish_when", ["high", "medium"])
    def test_flagged_cells_need_review_alone(self, length, others, publish_when):
        chapter = intro(S.CHAPTERS, 30_000, 30_000 + length, "Intro")
        limit = intro_chapter_limit_ms(others)
        d = decide(
            [chapter], ctx(publish_when, types=(T.INTRO,)) if limit is None else _limited(publish_when, limit), {}
        )
        assert (d[T.INTRO].status, d[T.INTRO].reason) == (DecisionStatus.NEEDS_REVIEW, self.REASON)
        assert d[T.INTRO].proposed == Marker(T.INTRO, 30_000, 30_000 + length, ("chapters",))

    def test_a_chapter_exactly_at_the_limit_is_not_flagged(self):
        d = decide([intro(S.CHAPTERS, 30_000, 74_000)], _limited("high", 44_000), {})[T.INTRO]
        assert (d.status, d.reason) == (DecisionStatus.DECIDED, "chapters")
        d = decide([intro(S.CHAPTERS, 30_000, 74_001)], _limited("high", 44_000), {})[T.INTRO]
        assert d.status is DecisionStatus.NEEDS_REVIEW

    def test_no_limit_means_no_check(self):
        d = decide([intro(S.CHAPTERS, 30_000, 118_000)], ctx(types=(T.INTRO,)), {})[T.INTRO]
        assert (d.status, d.marker) == (DecisionStatus.DECIDED, Marker(T.INTRO, 30_000, 118_000, ("chapters",)))

    @pytest.mark.parametrize(
        ("other", "expected"),
        [
            # an online source agreeing on the end publishes; its later start shortens the suspect chapter
            (intro(S.SKIPDB, 100_000, 118_500), Marker(T.INTRO, 100_000, 118_000, ("chapters", "skipdb"))),
            # an agreeing source with an earlier start still confirms; the chapter keeps its own start
            (intro(S.INTRODB, 10_000, 116_000), Marker(T.INTRO, 30_000, 118_000, ("chapters", "introdb"))),
            # season audio counts as agreement too
            (intro(S.SEASON_AUDIO, 101_000, 117_000), Marker(T.INTRO, 101_000, 118_000, ("chapters", "season_audio"))),
            # a server marker may still shorten the skip once another source confirms
            (
                [intro(S.SKIPDB, 90_000, 118_500), intro(S.SERVER_MARKERS, 99_000, 119_000, "plex-1")],
                Marker(T.INTRO, 99_000, 118_000, ("chapters", "skipdb", "server_markers")),
            ),
        ],
        ids=["skipdb-later-start", "introdb-earlier-start", "season-audio", "skipdb-and-server-markers"],
    )
    @pytest.mark.parametrize("publish_when", ["high", "medium"])
    def test_a_flagged_chapter_with_an_agreeing_source_publishes(self, other, expected, publish_when):
        cands = [intro(S.CHAPTERS, 30_000, 118_000, "Intro"), *(other if isinstance(other, list) else [other])]
        for order in (cands, cands[::-1]):
            d = decide(order, _limited(publish_when, 44_000), {})[T.INTRO]
            assert (d.status, d.marker, d.reason) == (DecisionStatus.DECIDED, expected, "chapters")

    @pytest.mark.parametrize(
        "servers",
        [
            [intro(S.SERVER_MARKERS, 99_000, 119_000, "plex-1")],
            [intro(S.SERVER_MARKERS_IMPORTED, 99_000, 119_000, "jf-1")],
            [
                intro(S.SERVER_MARKERS, 99_000, 119_000, "plex-1"),
                intro(S.SERVER_MARKERS_IMPORTED, 98_000, 117_000, "jf-1"),
            ],
        ],
        ids=["server", "importer-copy", "both"],
    )
    def test_a_flagged_chapter_confirmed_only_by_markers_on_servers_needs_review(self, servers):
        # Rule 7: a chapter that markers already on servers alone confirm still counts as chapters alone.
        d = decide([intro(S.CHAPTERS, 30_000, 118_000, "Intro"), *servers], _limited("medium", 44_000), {})[T.INTRO]
        assert (d.status, d.reason) == (DecisionStatus.NEEDS_REVIEW, self.REASON)
        assert d.proposed == Marker(T.INTRO, 30_000, 118_000, ("chapters",))

    def test_a_flagged_chapter_with_a_source_that_disagrees_needs_review(self):
        cands = [intro(S.CHAPTERS, 30_000, 118_000, "Intro"), intro(S.SKIPDB, 90_000, 124_000)]
        d = decide(cands, _limited("medium", 44_000), {})[T.INTRO]
        assert (d.status, d.reason) == (DecisionStatus.NEEDS_REVIEW, self.REASON)

    def test_a_flagged_chapter_backed_by_a_start_that_leaves_too_little_goes_to_review(self):
        cands = [intro(S.CHAPTERS, 30_000, 118_000, "Intro"), intro(S.SKIPDB, 116_000, 120_000)]
        d = decide(cands, _limited("high", 44_000), {})[T.INTRO]
        assert (d.status, d.reason) == (
            DecisionStatus.NEEDS_REVIEW,
            "chapters and agreeing sources disagree on the other edge",
        )

    def test_only_intro_chapters_are_checked(self):
        recap = Candidate(T.RECAP, 0, 100_000, S.CHAPTERS, origin="Recap")
        out = decide([recap], DecisionContext(DUR, False, "high", frozenset({T.RECAP}), ORDER, 20_000), {})
        assert (out[T.RECAP].status, out[T.RECAP].reason) == (DecisionStatus.DECIDED, "chapters")

    def test_a_lock_still_wins(self):
        lock = Marker(T.INTRO, 30_000, 118_000, ("user",))
        d = decide([intro(S.CHAPTERS, 30_000, 118_000)], _limited("high", 44_000), {T.INTRO: lock})[T.INTRO]
        assert (d.status, d.reason) == (DecisionStatus.DECIDED, "locked by user")


def _limited(publish_when, limit):
    return DecisionContext(DUR, False, publish_when, frozenset({T.INTRO}), ORDER, limit)


def _agreed(start, end, decided_by, reason):
    return (DecisionStatus.DECIDED, start, end, decided_by, reason)


_NOT_YET = (DecisionStatus.NEEDS_REVIEW, None, None, None, "sources don't agree yet")
_SKIPDB_COPY_INTRO = _agreed(
    128_000, 157_000, ("skipdb", "server_markers_imported"), "sources agree: skipdb, server_markers_imported"
)
_INTRODB_COPY_INTRO = _agreed(
    128_000, 157_000, ("introdb", "server_markers_imported"), "sources agree: introdb, server_markers_imported"
)
_SKIPDB_COPY_CREDITS = _agreed(
    1_241_000, DUR, ("skipdb", "server_markers_imported"), "sources agree: skipdb, server_markers_imported"
)
_INTRODB_COPY_CREDITS = _agreed(
    1_241_000, DUR, ("introdb", "server_markers_imported"), "sources agree: introdb, server_markers_imported"
)
# At Medium SkipDB may decide an intro alone (rule 6); its copy is the same source and may still give the later start.
_SKIPDB_ALONE_INTRO = _agreed(128_000, 157_000, ("skipdb", "server_markers_imported"), "single source (skipdb)")


class TestImportedCopiesJoinTheDatabaseTheyImport:
    """Rule 8 (ruling 2026-09-16): an importer plugin's markers are in the independence group of the database it
    imports. AniSkip's, and one whose database can't be told (""), stay with IntroDB/TheIntroDB."""

    @pytest.mark.parametrize(
        ("crowd", "copied_from", "high", "medium"),
        [
            (S.SKIPDB, "skipdb", _NOT_YET, _SKIPDB_ALONE_INTRO),
            (S.SKIPDB, "introdb", _SKIPDB_COPY_INTRO, _SKIPDB_COPY_INTRO),
            (S.SKIPDB, "aniskip", _SKIPDB_COPY_INTRO, _SKIPDB_COPY_INTRO),
            (S.SKIPDB, "", _SKIPDB_COPY_INTRO, _SKIPDB_COPY_INTRO),
            (S.INTRODB, "introdb", _NOT_YET, _NOT_YET),
            (S.THEINTRODB, "introdb", _NOT_YET, _NOT_YET),
            (S.INTRODB, "skipdb", _INTRODB_COPY_INTRO, _INTRODB_COPY_INTRO),
            # AniSkip copies stay with the crowd group until phase 4 measures what they copy
            (S.INTRODB, "aniskip", _NOT_YET, _NOT_YET),
            (S.INTRODB, "", _NOT_YET, _NOT_YET),
        ],
        ids=[
            "skipdb+skipdb-copy",
            "skipdb+introdb-copy",
            "skipdb+aniskip-copy",
            "skipdb+unknown-copy",
            "introdb+introdb-copy",
            "theintrodb+introdb-copy",
            "introdb+skipdb-copy",
            "introdb+aniskip-copy",
            "introdb+unknown-copy",
        ],
    )
    def test_intro(self, crowd, copied_from, high, medium):
        copy = Candidate(T.INTRO, 128_000, 160_000, S.SERVER_MARKERS_IMPORTED, origin="jf-1", copied_from=copied_from)
        for level, expected in (("high", high), ("medium", medium)):
            d = decide([intro(crowd, 127_000, 157_000), copy], ctx(level, types=(T.INTRO,)), {})[T.INTRO]
            got = (d.status, *((d.marker.start_ms, d.marker.end_ms, d.marker.decided_by) if d.marker else (None,) * 3))
            assert (*got, d.reason) == expected, level

    @pytest.mark.parametrize(
        ("crowd", "copied_from", "high", "medium"),
        [
            # SkipDB and its copy are one source, and SkipDB alone never decides credits, at Medium either (rule 6)
            (S.SKIPDB, "skipdb", _NOT_YET, _NOT_YET),
            (S.SKIPDB, "introdb", _SKIPDB_COPY_CREDITS, _SKIPDB_COPY_CREDITS),
            (S.SKIPDB, "aniskip", _SKIPDB_COPY_CREDITS, _SKIPDB_COPY_CREDITS),
            (S.SKIPDB, "", _SKIPDB_COPY_CREDITS, _SKIPDB_COPY_CREDITS),
            (S.INTRODB, "introdb", _NOT_YET, _NOT_YET),
            (S.THEINTRODB, "introdb", _NOT_YET, _NOT_YET),
            (S.INTRODB, "skipdb", _INTRODB_COPY_CREDITS, _INTRODB_COPY_CREDITS),
            (S.INTRODB, "aniskip", _NOT_YET, _NOT_YET),
            (S.INTRODB, "", _NOT_YET, _NOT_YET),
        ],
        ids=[
            "skipdb+skipdb-copy",
            "skipdb+introdb-copy",
            "skipdb+aniskip-copy",
            "skipdb+unknown-copy",
            "introdb+introdb-copy",
            "theintrodb+introdb-copy",
            "introdb+skipdb-copy",
            "introdb+aniskip-copy",
            "introdb+unknown-copy",
        ],
    )
    def test_credits(self, crowd, copied_from, high, medium):
        copy = Candidate(T.CREDITS, 1_242_000, None, S.SERVER_MARKERS_IMPORTED, origin="jf-1", copied_from=copied_from)
        for level, expected in (("high", high), ("medium", medium)):
            d = decide([credits(crowd, 1_241_000), copy], ctx(level, types=(T.CREDITS,)), {})[T.CREDITS]
            got = (d.status, *((d.marker.start_ms, d.marker.end_ms, d.marker.decided_by) if d.marker else (None,) * 3))
            assert (*got, d.reason) == expected, level

    def test_copied_from_means_nothing_on_a_servers_own_markers(self):
        own = Candidate(T.INTRO, 128_000, 160_000, S.SERVER_MARKERS, origin="plex-1", copied_from="skipdb")
        d = decide([intro(S.SKIPDB, 127_000, 157_000), own], ctx("high", types=(T.INTRO,)), {})[T.INTRO]
        assert (d.status, d.marker.start_ms, d.marker.end_ms) == (DecisionStatus.DECIDED, 128_000, 157_000)
        assert d.marker.decided_by == ("skipdb", "server_markers")


# Every source alone and in every pair (an importer plugin's copy once per database it can name), for every marker type
# at both levels: the cells rules 3, 4, 6, 7 and 8, R2 and G3 give, written out plainly rather than through decide.py.
_MATRIX_KINDS = (
    (S.CHAPTERS, ""),
    (S.THEINTRODB, ""),
    (S.INTRODB, ""),
    (S.SKIPDB, ""),
    (S.SEASON_AUDIO, ""),
    (S.SEASON_AUDIO_PREVIOUS, ""),
    (S.CREDITS_TEXT, ""),
    (S.SERVER_MARKERS, ""),
    (S.SERVER_MARKERS_IMPORTED, ""),
    (S.SERVER_MARKERS_IMPORTED, "introdb"),
    (S.SERVER_MARKERS_IMPORTED, "skipdb"),
    (S.SERVER_MARKERS_IMPORTED, "aniskip"),
)
# Two sane places per type, far enough apart that answers at the two never agree.
_MATRIX_NEAR = {T.INTRO: (60_000, 90_000), T.RECAP: (10_000, 40_000), T.CREDITS: (1_250_000, None),
                T.PREVIEW: (1_290_000, 1_310_000)}  # fmt: skip
_MATRIX_FAR = {T.INTRO: (150_000, 200_000), T.RECAP: (100_000, 140_000), T.CREDITS: (1_100_000, None),
               T.PREVIEW: (1_000_000, 1_020_000)}  # fmt: skip
_MATRIX_ONLY_AGREE = {S.SEASON_AUDIO, S.SEASON_AUDIO_PREVIOUS, S.SERVER_MARKERS, S.SERVER_MARKERS_IMPORTED}


def _kind_id(kind):
    source, copied_from = kind
    return source.value + (f"[{copied_from}]" if copied_from else "")


def _matrix_candidate(kind, mtype, place):
    source, copied_from = kind
    start, end = place[mtype]
    return Candidate(
        mtype, start, end, source, origin="plex-1" if source in _REF_SERVER else "", copied_from=copied_from
    )


def _plain_group(kind):
    source, copied_from = kind
    if source in (S.INTRODB, S.THEINTRODB):
        return "crowd"
    if source is S.SERVER_MARKERS_IMPORTED:
        return "skipdb" if copied_from == "skipdb" else "crowd"  # AniSkip's and an unknown copy: crowd until phase 4
    return "season_audio" if source is S.SEASON_AUDIO_PREVIOUS else source.value


def _alone_at_medium(source, mtype):
    """Rule 6 (and Q1, F3): credit text alone, SkipDB alone for intros and recaps."""
    return source is S.CREDITS_TEXT or (source is S.SKIPDB and mtype in (T.INTRO, T.RECAP))


def _credited(*sources):
    rank = {s: (ORDER.index(s.value) if s.value in ORDER else len(ORDER), list(S).index(s)) for s in sources}
    return tuple(s.value for s in sorted(set(sources), key=rank.__getitem__))


def _own(c):
    return (c.start_ms, DUR if c.end_ms is None else c.end_ms)


def _expected_alone(c, level):
    if c.source is S.CHAPTERS:
        return DecisionStatus.DECIDED, _own(c), ("chapters",), "chapters"
    if level == "medium" and _alone_at_medium(c.source, c.type):
        return DecisionStatus.DECIDED, _own(c), (c.source.value,), f"single source ({c.source.value})"
    return DecisionStatus.NEEDS_REVIEW, None, None, "sources don't agree yet"


def _expected_agreeing(kinds, a, b, level):
    sources = {a.source, b.source}
    if S.CHAPTERS in sources:  # one agreeing source leaves a chapter as it is (rule 3)
        return DecisionStatus.DECIDED, _own(a), ("chapters",), "chapters"
    if _plain_group(kinds[0]) == _plain_group(kinds[1]):  # one source twice (rule 8): as if alone
        deciders = [c.source for c in (a, b) if _alone_at_medium(c.source, c.type)]
        if level == "medium" and deciders:
            return DecisionStatus.DECIDED, _own(a), _credited(*sources), f"single source ({deciders[0].value})"
        return DecisionStatus.NEEDS_REVIEW, None, None, "sources don't agree yet"
    if sources <= _MATRIX_ONLY_AGREE:  # R2, G3, rule 7: nothing here may supply the times
        if sources & set(_REF_SERVER) and sources - set(_REF_SERVER):
            return DecisionStatus.NEEDS_REVIEW, None, None, _REF_AUDIO_WITH_SERVER
        return DecisionStatus.NEEDS_REVIEW, None, None, "sources don't agree yet"
    return DecisionStatus.DECIDED, _own(a), _credited(*sources), "sources agree: " + ", ".join(_credited(*sources))


def _expected_disagreeing(kinds, near, far, level):
    chapter = next((c for c in (near, far) if c.source is S.CHAPTERS), None)
    if chapter is not None:  # one contradicting source never overrides a chapter (rule 3)
        other = far if chapter is near else near
        start, end = _own(chapter)
        # Rule 7: a server's own credits/preview starting inside the decided skip, more than 10 s from both ends,
        # moves the start to it.
        if other.source is S.SERVER_MARKERS and chapter.type in (T.CREDITS, T.PREVIEW):
            if start + 10_000 < other.start_ms < end - 10_000:
                note = "chapters; start shortened to the server's own marker (plex-1)"
                return DecisionStatus.DECIDED, (other.start_ms, end), ("chapters", "server_markers"), note
        return DecisionStatus.DECIDED, (start, end), ("chapters",), "chapters"
    if _plain_group(kinds[0]) == _plain_group(kinds[1]):
        if level == "medium" and any(_alone_at_medium(c.source, c.type) for c in (near, far)):
            return DecisionStatus.NEEDS_REVIEW, None, None, "source disagrees with itself"
        return DecisionStatus.NEEDS_REVIEW, None, None, "sources don't agree yet"
    return DecisionStatus.NEEDS_REVIEW, None, None, "sources disagree: "


def _got(decision):
    marker = decision.marker
    shown = (marker.start_ms, marker.end_ms) if marker else None
    return decision.status, shown, marker.decided_by if marker else None, decision.reason


class TestDecisionMatrix:
    """publish_when x every source alone and in every pair x every marker type (testing.md "Cover the matrix")."""

    @pytest.mark.parametrize("level", ["high", "medium"])
    @pytest.mark.parametrize("mtype", list(T), ids=lambda t: t.value)
    @pytest.mark.parametrize("kind", _MATRIX_KINDS, ids=_kind_id)
    def test_alone(self, kind, mtype, level):
        c = _matrix_candidate(kind, mtype, _MATRIX_NEAR)
        assert _got(decide([c], ctx(level, types=(mtype,)), {})[mtype]) == _expected_alone(c, level)

    @pytest.mark.parametrize("level", ["high", "medium"])
    @pytest.mark.parametrize("mtype", list(T), ids=lambda t: t.value)
    @pytest.mark.parametrize(
        "kinds",
        list(itertools.combinations_with_replacement(_MATRIX_KINDS, 2)),
        ids=lambda ks: "+".join(_kind_id(k) for k in ks),
    )
    def test_agreeing_pair(self, kinds, mtype, level):
        a, b = (_matrix_candidate(k, mtype, _MATRIX_NEAR) for k in kinds)
        expected = _expected_agreeing(kinds, a, b, level)
        for order in ([a, b], [b, a]):
            assert _got(decide(order, ctx(level, types=(mtype,)), {})[mtype]) == expected

    @pytest.mark.parametrize("level", ["high", "medium"])
    @pytest.mark.parametrize("mtype", list(T), ids=lambda t: t.value)
    @pytest.mark.parametrize(
        "kinds",
        list(itertools.product(_MATRIX_KINDS, repeat=2)),
        ids=lambda ks: "near-" + _kind_id(ks[0]) + "+far-" + _kind_id(ks[1]),
    )
    def test_disagreeing_pair(self, kinds, mtype, level):
        near = _matrix_candidate(kinds[0], mtype, _MATRIX_NEAR)
        far = _matrix_candidate(kinds[1], mtype, _MATRIX_FAR)
        status, shown, decided_by, reason = _expected_disagreeing(kinds, near, far, level)
        for order in ([near, far], [far, near]):
            got = _got(decide(order, ctx(level, types=(mtype,)), {})[mtype])
            assert got[:3] == (status, shown, decided_by)
            assert got[3].startswith(reason) if reason.endswith(": ") else got[3] == reason
