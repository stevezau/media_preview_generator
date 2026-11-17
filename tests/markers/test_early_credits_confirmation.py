"""Early episode credits: OCR + online confirmation may use the already-scanned tail."""

from dataclasses import replace

import pytest

from media_preview_generator.markers.decide import (
    DecisionContext,
    DecisionStatus,
    decide,
    sanity_problem,
)
from media_preview_generator.markers.models import Candidate, MarkerType, Source

CREDITS = MarkerType.CREDITS
DURATION_MS = 1_461_000  # 24:21; the last-quarter boundary is 18:15.750
SCAN_WINDOW_MS = 450_000  # automatic TV scan: 7:30, starts at 16:51
TEXT_START_MS = 1_085_000  # 18:05
ONLINE_START_MS = 1_080_000  # 18:00
CREDITS_END_MS = 1_200_000  # 20:00; later story must remain playable
ORDER = tuple(s.value for s in (Source.CREDITS_TEXT, Source.INTRODB, Source.THEINTRODB, Source.SKIPDB))


def context(*, scan_window_ms: int = SCAN_WINDOW_MS, window_ms: int = 0) -> DecisionContext:
    return DecisionContext(
        duration_ms=DURATION_MS,
        is_movie=False,
        publish_when="medium",
        enabled_types=frozenset({CREDITS}),
        source_order=ORDER,
        credits_window_ms=window_ms,
        credits_scan_window_ms=scan_window_ms,
    )


def candidate(source: Source, start_ms: int, end_ms: int | None = None) -> Candidate:
    return Candidate(CREDITS, start_ms, end_ms, source)


def verdict(*candidates: Candidate, decision_context: DecisionContext | None = None):
    return decide(list(candidates), decision_context or context(), {})[CREDITS]


def test_matching_ocr_and_online_confirm_credits_before_last_quarter() -> None:
    online = candidate(Source.INTRODB, ONLINE_START_MS)
    ocr = candidate(Source.CREDITS_TEXT, TEXT_START_MS, CREDITS_END_MS)

    # The existing sanity rule rejects each timestamp on its own.
    assert "last 25%" in (sanity_problem(online, context()) or "")
    assert "last 25%" in (sanity_problem(ocr, context()) or "")

    result = verdict(online, ocr)
    assert result.status is DecisionStatus.DECIDED
    assert result.marker is not None
    assert result.marker.start_ms == TEXT_START_MS
    # The online hint has no end; the locally detected end protects the post-credit scene.
    assert result.marker.end_ms == CREDITS_END_MS
    assert {Source.CREDITS_TEXT.value, Source.INTRODB.value} <= set(result.marker.decided_by)


@pytest.mark.parametrize("source", [Source.INTRODB, Source.THEINTRODB, Source.SKIPDB])
def test_each_independent_online_provider_can_confirm_early_ocr(source: Source) -> None:
    result = verdict(
        candidate(source, ONLINE_START_MS),
        candidate(Source.CREDITS_TEXT, TEXT_START_MS, CREDITS_END_MS),
    )
    assert result.status is DecisionStatus.DECIDED
    assert result.marker is not None
    assert (result.marker.start_ms, result.marker.end_ms) == (TEXT_START_MS, CREDITS_END_MS)


@pytest.mark.parametrize(
    "candidates",
    [
        (candidate(Source.CREDITS_TEXT, TEXT_START_MS, CREDITS_END_MS),),
        (candidate(Source.INTRODB, ONLINE_START_MS),),
        (candidate(Source.INTRODB, ONLINE_START_MS), candidate(Source.SKIPDB, TEXT_START_MS)),
        (candidate(Source.CHAPTERS, ONLINE_START_MS), candidate(Source.INTRODB, TEXT_START_MS)),
        (candidate(Source.CREDITS_TEXT, TEXT_START_MS, CREDITS_END_MS), candidate(Source.INTRODB, 1_070_000)),
    ],
)
def test_no_early_exception_without_independent_agreement(candidates: tuple[Candidate, ...]) -> None:
    result = verdict(*candidates)
    assert result.status is DecisionStatus.NO_EVIDENCE
    assert result.marker is None


def test_online_confirmation_cannot_bypass_actual_scan_window() -> None:
    result = verdict(
        candidate(Source.INTRODB, 1_000_000),
        candidate(Source.CREDITS_TEXT, 1_005_000, CREDITS_END_MS),
    )
    assert result.status is DecisionStatus.NO_EVIDENCE
    assert result.marker is None


def test_exact_ten_second_agreement_is_accepted() -> None:
    result = verdict(
        candidate(Source.INTRODB, 1_080_000),
        candidate(Source.CREDITS_TEXT, 1_090_000, CREDITS_END_MS),
    )
    assert result.status is DecisionStatus.DECIDED
    assert result.marker is not None and result.marker.end_ms == CREDITS_END_MS


def test_no_exception_without_known_episode_scan_window() -> None:
    result = verdict(
        candidate(Source.INTRODB, ONLINE_START_MS),
        candidate(Source.CREDITS_TEXT, TEXT_START_MS, CREDITS_END_MS),
        decision_context=context(scan_window_ms=0),
    )
    assert result.status is DecisionStatus.NO_EVIDENCE
    assert result.marker is None


def test_existing_manual_search_window_still_allows_unconfirmed_early_credits() -> None:
    result = verdict(
        candidate(Source.CREDITS_TEXT, TEXT_START_MS, CREDITS_END_MS),
        decision_context=context(window_ms=480_000, scan_window_ms=480_000),
    )
    assert result.status is DecisionStatus.DECIDED
    assert result.marker is not None and result.marker.start_ms == TEXT_START_MS


def test_earlier_unrelated_chapter_remains_excluded() -> None:
    # This chapter is before the normal last-quarter boundary, yet independently
    # corroborated OCR/online timestamps must not make the chapter sane too.
    result = verdict(
        candidate(Source.CHAPTERS, 1_055_000),
        candidate(Source.INTRODB, ONLINE_START_MS),
        candidate(Source.CREDITS_TEXT, TEXT_START_MS, CREDITS_END_MS),
    )
    assert result.status is DecisionStatus.DECIDED
    assert result.marker is not None
    assert (result.marker.start_ms, result.marker.end_ms) == (TEXT_START_MS, CREDITS_END_MS)


def test_unchanged_last_quarter_rule_for_unconfirmed_credits() -> None:
    late = candidate(Source.CREDITS_TEXT, 1_100_000, 1_240_000)
    assert verdict(late).status is DecisionStatus.DECIDED

    # A wider scan window affects only independently confirmed early starts.
    assert verdict(
        candidate(Source.CREDITS_TEXT, TEXT_START_MS, CREDITS_END_MS),
        decision_context=replace(context(), credits_scan_window_ms=600_000),
    ).status is DecisionStatus.NO_EVIDENCE
