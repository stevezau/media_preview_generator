"""Decision rules (spec §5.5): locks, sanity bounds, chapters, agreeing independent sources, a single source at
"Medium", and the cross-type overlap checks.

Every result depends only on the set of candidates, never on the order they arrive in.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from itertools import combinations

from .models import SERVER_SOURCES, Candidate, Marker, MarkerType, Source

INTRO_END_TOLERANCE_MS = 5_000
CREDITS_START_TOLERANCE_MS = 10_000
EOF_CLAMP_MS = 2_000
MIN_SEGMENT_MS = 3_000
MAX_INTRO_MS = 300_000
MOVIE_CREDITS_MAX_FROM_END_MS = 900_000
INTRO_RECAP_MAX_OVERLAP_MS = 5_000
PREVIEW_CREDITS_MAX_OVERLAP_MS = 10_000
# IntroDB data looks partly seeded from other sources (spec §5.5), so IntroDB and TheIntroDB are always one
# independence group -- never two votes, whether or not they agree with each other. Markers an intro-DB importer
# plugin wrote on a Jellyfin/Emby server are that crowd data again.
_INTRODB_GROUP = "introdb/theintrodb"
_INDEPENDENCE_GROUP = {
    Source.INTRODB: _INTRODB_GROUP,
    Source.THEINTRODB: _INTRODB_GROUP,
    Source.SERVER_MARKERS_IMPORTED: _INTRODB_GROUP,
}
# At "Medium" a lone source publishes only when it checks this file's cut itself (rule 6): IntroDB takes no duration,
# TheIntroDB answers the closest cut it has, and markers already on servers never decide alone (rule 7).
_AGREEMENT_ONLY = SERVER_SOURCES | {Source.INTRODB, Source.THEINTRODB}
_START_SEGMENTS = (MarkerType.INTRO, MarkerType.RECAP)
_ENUM_ORDER = {source: i for i, source in enumerate(Source)}


class DecisionStatus(str, Enum):
    """Outcome of deciding one marker type for one file."""

    DECIDED = "decided"
    NEEDS_REVIEW = "needs_review"
    NO_EVIDENCE = "no_evidence"
    DISABLED = "disabled"


@dataclass(frozen=True)
class DecisionContext:
    """Per-file inputs to the rules."""

    duration_ms: int
    is_movie: bool
    publish_when: str
    enabled_types: frozenset[MarkerType]
    source_order: tuple[str, ...]


@dataclass(frozen=True)
class TypeDecision:
    """Decision for one marker type."""

    type: MarkerType
    status: DecisionStatus
    marker: Marker | None
    proposed: Marker | None
    reason: str


def resolve_end_ms(candidate: Candidate, duration_ms: int) -> int:
    """Resolve a candidate's end offset against the file duration.

    Clamps to duration_ms when the candidate has no end or overshoots; the <= 2 s bound on how
    much overshoot is tolerable is enforced by :func:`sanity_problem`, not here.

    Args:
        candidate: The candidate whose end offset to resolve.
        duration_ms: The file's duration in milliseconds.

    Returns:
        The end offset in milliseconds, never greater than duration_ms.
    """
    if candidate.end_ms is None:
        return duration_ms
    return min(candidate.end_ms, duration_ms)


def sanity_problem(candidate: Candidate, ctx: DecisionContext) -> str | None:
    """Check whether a candidate is a plausible segment for this file.

    Args:
        candidate: The candidate to check.
        ctx: The file's duration, movie flag, and other per-file context.

    Returns:
        A short reason why the candidate is implausible, or None when it passes every check.
    """
    d = ctx.duration_ms
    start = candidate.start_ms
    if d <= 0:
        return "duration unknown"
    if start < 0:
        return "negative start"
    if start >= d:
        return "starts past the end of the file"
    if candidate.end_ms is not None and candidate.end_ms < start:
        return "ends before it starts"
    if candidate.end_ms is not None and candidate.end_ms > d + EOF_CLAMP_MS:
        return "ends past the end of the file"
    end = resolve_end_ms(candidate, d)
    length = end - start
    if length < MIN_SEGMENT_MS:
        return "segment too short"
    if candidate.type in _START_SEGMENTS:
        # An intro/recap that reaches the file end is a mis-detected chapter/scene, not a skip
        # segment -- reuse the same 2 s-from-end bound as the overshoot clamp above.
        if end >= d - EOF_CLAMP_MS:
            return f"{candidate.type.value} runs to end of file"
        # Integer cross-multiplication (start/d vs 35/100) avoids float error at the exact boundary
        # -- 0.35 * 1_320_000 == 461999.99999999994 in binary floating point.
        if start * 100 > 35 * d:
            return f"{candidate.type.value} starts after 35% of the file"
        if length > MAX_INTRO_MS:
            return f"{candidate.type.value} too long"
    else:
        if start * 100 < 75 * d:
            return f"{candidate.type.value} starts before the last 25% of the file"
        if candidate.type is MarkerType.CREDITS and ctx.is_movie and d - start > MOVIE_CREDITS_MAX_FROM_END_MS:
            return "movie credits start more than 900 s before the end"
    return None


def _group(source: Source) -> str:
    return _INDEPENDENCE_GROUP.get(source, source.value)


def _tolerance_ms(mtype: MarkerType) -> int:
    return INTRO_END_TOLERANCE_MS if mtype in _START_SEGMENTS else CREDITS_START_TOLERANCE_MS


def _agree_value(c: Candidate, duration_ms: int) -> int:
    """The single number agreement is judged on: resolved end for intro/recap, start for credits/preview."""
    return resolve_end_ms(c, duration_ms) if c.type in _START_SEGMENTS else c.start_ms


def _checked_value(mtype: MarkerType, marker: Marker) -> int:
    """The same edge as :func:`_agree_value`, read from a decided or proposed marker."""
    return marker.end_ms if mtype in _START_SEGMENTS else marker.start_ms


def _agree(a: Candidate, b: Candidate, duration_ms: int) -> bool:
    return abs(_agree_value(a, duration_ms) - _agree_value(b, duration_ms)) <= _tolerance_ms(a.type)


def _precedence(source: Source, order: tuple[str, ...]) -> int:
    try:
        return order.index(source.value)
    except ValueError:
        return len(order)


def _sort_key(ctx: DecisionContext) -> Callable[[Candidate], tuple]:
    """Key ranking candidates best-first.

    Order: the user's source order (sources missing from it rank after every listed one), then
    Source enum declaration order, then higher confidence (a NaN or infinite confidence counts as
    0.0, since NaN compares false both ways and would make the order depend on input order), then
    the shorter skip -- the safer checked edge (earlier intro/recap end, later credits/preview
    start), then the safer other edge (later intro/recap start, earlier resolved credits/preview
    end) -- then the raw end (no end first). The shorter-skip levels are a deliberate choice: when
    nothing else separates two candidates, a skip that is too short only shows the viewer more.
    Two candidates only tie when they differ in nothing but `origin`, or in confidences that all rank
    as 0.0 (NaN/inf); either way they publish identical markers -- so no result can depend on input order.
    """

    def key(c: Candidate) -> tuple:
        end = resolve_end_ms(c, ctx.duration_ms)
        shorter_skip = (end, -c.start_ms) if c.type in _START_SEGMENTS else (-c.start_ms, end)
        return (
            _precedence(c.source, ctx.source_order),
            _ENUM_ORDER[c.source],
            -(c.confidence if math.isfinite(c.confidence) else 0.0),
            *shorter_skip,
            -1 if c.end_ms is None else c.end_ms,
        )

    return key


def _source_sort_key(ctx: DecisionContext) -> Callable[[Source], tuple[int, int]]:
    """Key for ranking bare Source values (e.g. a decided_by list): user's order, then enum order."""
    return lambda s: (_precedence(s, ctx.source_order), _ENUM_ORDER[s])


def _own_marker(candidate: Candidate, ctx: DecisionContext) -> Marker:
    """A marker made of one candidate's own edges, credited to its source alone."""
    return Marker(
        type=candidate.type,
        start_ms=candidate.start_ms,
        end_ms=resolve_end_ms(candidate, ctx.duration_ms),
        decided_by=(candidate.source.value,),
    )


def _review(mtype: MarkerType, proposed: Marker | None, reason: str) -> TypeDecision:
    return TypeDecision(mtype, DecisionStatus.NEEDS_REVIEW, None, proposed, reason)


def _agreeing_cliques(candidates: list[Candidate], mtype: MarkerType, ctx: DecisionContext) -> list[list[Candidate]]:
    """Every maximal set of mutually agreeing candidates that spans >= 2 independent groups and holds a candidate
    that may supply times (not a server marker).

    Agreement compares exactly one number per candidate (end for intro/recap, start for
    credits/preview) against a fixed tolerance, so every maximal agreeing set is a contiguous
    window over candidates sorted by that number: sort, then for each left edge slide the right
    edge out as far as the tolerance allows. A window that doesn't reach further right than the
    last one is a subset of it and is skipped. The windows come back in ascending order of their
    smallest compared value, each sorted best-first. A window of server markers alone (a server's
    own and an importer plugin's copy) publishes nothing; the contradiction guard still counts it.
    """
    tol = _tolerance_ms(mtype)
    rank = _sort_key(ctx)
    by_value = sorted(candidates, key=lambda c: (_agree_value(c, ctx.duration_ms), rank(c)))
    values = [_agree_value(c, ctx.duration_ms) for c in by_value]
    cliques: list[list[Candidate]] = []
    last_hi, hi = -1, 0
    for lo in range(len(by_value)):
        hi = max(hi, lo)
        while hi + 1 < len(by_value) and values[hi + 1] - values[lo] <= tol:
            hi += 1
        if hi <= last_hi:
            continue
        last_hi = hi
        window = by_value[lo : hi + 1]
        if len({_group(c.source) for c in window}) >= 2 and any(c.source not in SERVER_SOURCES for c in window):
            cliques.append(sorted(window, key=rank))
    return cliques


def _safer_other_edge(
    mtype: MarkerType, suppliers: list[Candidate], ctx: DecisionContext
) -> tuple[int, list[Candidate]]:
    """The safer unchecked edge across `suppliers` and the candidates that have it.

    Intro/recap: the latest start. Credits/preview: the earliest end (a missing/EOF end resolves to
    duration first). Both shorten the skip rather than grow it.
    """
    if mtype in _START_SEGMENTS:
        value = max(c.start_ms for c in suppliers)
        return value, [c for c in suppliers if c.start_ms == value]
    value = min(resolve_end_ms(c, ctx.duration_ms) for c in suppliers)
    return value, [c for c in suppliers if resolve_end_ms(c, ctx.duration_ms) == value]


def _edges(mtype: MarkerType, checked: int, other: int) -> tuple[int, int]:
    """(start_ms, end_ms) from the checked edge and the unchecked edge."""
    return (other, checked) if mtype in _START_SEGMENTS else (checked, other)


def _composed_marker(mtype: MarkerType, checked: int, other: int, sources: set[Source], ctx: DecisionContext) -> Marker:
    """A marker from its checked and unchecked edges, credited to `sources` in source order."""
    start_ms, end_ms = _edges(mtype, checked, other)
    decided_by = tuple(s.value for s in sorted(sources, key=_source_sort_key(ctx)))
    return Marker(type=mtype, start_ms=start_ms, end_ms=end_ms, decided_by=decided_by)


def _marker_is_sane(marker: Marker, ctx: DecisionContext) -> bool:
    """Whether a composed marker passes :func:`sanity_problem` (a candidate's source never affects sanity)."""
    probe = Candidate(marker.type, marker.start_ms, marker.end_ms, Source(marker.decided_by[0]))
    return sanity_problem(probe, ctx) is None


def _compose_cluster(cluster: list[Candidate], mtype: MarkerType, ctx: DecisionContext) -> tuple[Marker, Candidate]:
    """Build the marker a cluster of >= 2 independent groups would publish, and its time winner.

    Only confirming candidates (those agreeing with a member of a different independent group)
    may supply times. The checked edge comes from the best-ranked confirming non-server candidate
    (source order first, see :func:`_sort_key`). The unchecked edge is the safer value across every confirming candidate, server markers
    included -- intro/recap start = the latest start, credits/preview end = the earliest end (a
    missing/EOF end resolves to duration first) -- so a server marker can shorten the skip but
    never lengthen it or set the checked edge (spec §5.5 rule 7). :func:`_agreeing_cliques` only
    returns clusters holding a non-server candidate, and every member of one confirms.

    decided_by names the winner, whichever candidate(s) supplied the unchecked edge, and every
    other confirming candidate that directly agrees with the winner, in source order.
    """
    confirmed = [
        c
        for c in cluster
        if any(_group(o.source) != _group(c.source) and _agree(c, o, ctx.duration_ms) for o in cluster)
    ]
    confirmed_non_server = [c for c in confirmed if c.source not in SERVER_SOURCES]
    winner = min(confirmed_non_server, key=_sort_key(ctx))
    other_edge, edge_suppliers = _safer_other_edge(mtype, confirmed, ctx)
    agreeing_with_winner = [c for c in confirmed if _agree(winner, c, ctx.duration_ms)]
    sources = {c.source for c in [winner, *edge_suppliers, *agreeing_with_winner]}
    return _composed_marker(mtype, _agree_value(winner, ctx.duration_ms), other_edge, sources, ctx), winner


def _contradicting_groups(marker: Marker, pool: list[Candidate], ctx: DecisionContext) -> list[str]:
    """Groups of every agreeing cross-group pair in `pool` whose members are both beyond tolerance of marker.

    Such a pair is two independent sources saying the published checked edge is wrong. Agreement
    isn't transitive, so a third candidate agreeing with both the result and the pair (a bridge)
    can merge everything into one cluster without the pair ever agreeing with the result itself.
    """
    d = ctx.duration_ms
    value = _checked_value(marker.type, marker)
    far = [c for c in pool if abs(_agree_value(c, d) - value) > _tolerance_ms(marker.type)]
    pairs = [(a, b) for a, b in combinations(far, 2) if _group(a.source) != _group(b.source) and _agree(a, b, d)]
    return sorted({_group(c.source) for pair in pairs for c in pair})


def _chapter_choice_key(c: Candidate, duration_ms: int) -> tuple[int, int]:
    """First intro/recap chapter or last credits/preview chapter; on a start tie, the earlier end (shorter skip)."""
    start = c.start_ms if c.type in _START_SEGMENTS else -c.start_ms
    return start, resolve_end_ms(c, duration_ms)


def _decide_from_chapters(
    mtype: MarkerType, chapters: list[Candidate], others: list[Candidate], ctx: DecisionContext
) -> TypeDecision:
    """Accept the first intro/recap or last credits/preview chapter unless agreeing sources contradict it.

    When >= 2 independent non-chapter groups agree with the chapter's checked edge, and the safer
    unchecked edge across those agreeing candidates (server markers included: they may shorten the
    skip) is safer than the chapter's own, it replaces the chapter's; decided_by then adds every
    agreeing source. A replacement that fails sanity sends the type to review.
    """
    chosen = min(chapters, key=lambda c: _chapter_choice_key(c, ctx.duration_ms))
    chapter_marker = _own_marker(chosen, ctx)
    chapter_value = _checked_value(mtype, chapter_marker)
    tol = _tolerance_ms(mtype)
    for cluster in _agreeing_cliques(others, mtype, ctx):
        other_marker, _ = _compose_cluster(cluster, mtype, ctx)
        if abs(_checked_value(mtype, other_marker) - chapter_value) > tol:
            names = ", ".join(other_marker.decided_by)
            return _review(mtype, chapter_marker, f"chapters contradicted by agreeing sources: {names}")

    marker = chapter_marker
    agreeing = [c for c in others if abs(_agree_value(c, ctx.duration_ms) - chapter_value) <= tol]
    if len({_group(c.source) for c in agreeing}) >= 2:
        other_edge, _ = _safer_other_edge(mtype, agreeing, ctx)
        if mtype in _START_SEGMENTS:
            safer = other_edge > chapter_marker.start_ms
        else:
            safer = other_edge < chapter_marker.end_ms
        if safer:
            marker = _composed_marker(
                mtype, chapter_value, other_edge, {Source.CHAPTERS, *(c.source for c in agreeing)}, ctx
            )
            if not _marker_is_sane(marker, ctx):
                return _review(mtype, chapter_marker, "chapters and agreeing sources disagree on the other edge")
    return TypeDecision(mtype, DecisionStatus.DECIDED, marker, None, "chapters")


def _decide_from_cliques(mtype: MarkerType, cliques: list[list[Candidate]], ctx: DecisionContext) -> TypeDecision:
    composed = [_compose_cluster(cl, mtype, ctx) for cl in cliques]
    values = [_checked_value(mtype, m) for m, _ in composed]
    if max(values) - min(values) > _tolerance_ms(mtype):
        rank = _sort_key(ctx)
        # Stable sort: clusters sharing a winner keep ascending compared-value order.
        ranked = sorted(composed, key=lambda mw: rank(mw[1]))
        names = " vs ".join(dict.fromkeys(_group(winner.source) for _, winner in ranked))
        return _review(mtype, ranked[0][0], f"agreeing sources conflict: {names}")

    merged = list({id(c): c for cl in cliques for c in cl}.values())
    marker, winner = _compose_cluster(merged, mtype, ctx)
    if not _marker_is_sane(marker, ctx):
        return _review(mtype, _own_marker(winner, ctx), "agreeing sources disagree on the other edge")
    return TypeDecision(mtype, DecisionStatus.DECIDED, marker, None, "sources agree: " + ", ".join(marker.decided_by))


def _decide_from_single_source(mtype: MarkerType, sane: list[Candidate], ctx: DecisionContext) -> TypeDecision:
    """No two independent sources agree. Only "medium" may publish, and only a lone, self-consistent group that
    checks this file's cut itself (not IntroDB/TheIntroDB, not markers already on servers).

    A second independent group here (server markers included) contradicts the first unless both are
    server markers (a server's own and an importer plugin's copy, which never form a cluster); a group
    whose own candidates don't all agree pairwise contradicts itself. The checked edge comes from the
    best-ranked candidate, the unchecked edge is the safer value across the group's candidates, and
    decided_by names the sources that supplied either edge.
    """
    ranked = sorted(sane, key=_sort_key(ctx))
    groups = sorted({_group(c.source) for c in sane})
    proposal = next((c for c in ranked if c.source not in _AGREEMENT_ONLY), None)
    if ctx.publish_when == "medium" and proposal is not None and len(groups) == 1:
        if not all(_agree(a, b, ctx.duration_ms) for a, b in combinations(sane, 2)):
            return _review(mtype, _own_marker(proposal, ctx), "source disagrees with itself")
        other_edge, edge_suppliers = _safer_other_edge(mtype, sane, ctx)
        sources = {proposal.source, *(c.source for c in edge_suppliers)}
        marker = _composed_marker(mtype, _agree_value(proposal, ctx.duration_ms), other_edge, sources, ctx)
        if not _marker_is_sane(marker, ctx):
            return _review(mtype, _own_marker(proposal, ctx), "sources disagree on the other edge")
        return TypeDecision(mtype, DecisionStatus.DECIDED, marker, None, f"single source ({proposal.source.value})")
    disagree = any(
        _group(a.source) != _group(b.source) and not _agree(a, b, ctx.duration_ms) for a, b in combinations(sane, 2)
    )
    reason = f"sources disagree: {', '.join(groups)}" if disagree else "sources don't agree yet"
    return _review(mtype, _own_marker(ranked[0], ctx), reason)


def _decide_type(mtype: MarkerType, candidates: list[Candidate], ctx: DecisionContext) -> TypeDecision:
    of_type = [c for c in candidates if c.type is mtype]
    sane = [c for c in of_type if sanity_problem(c, ctx) is None]
    if not sane:
        reason = "no evidence" if not of_type else f"{len(of_type)} candidate(s) failed sanity checks"
        return TypeDecision(mtype, DecisionStatus.NO_EVIDENCE, None, None, reason)

    chapters = [c for c in sane if c.source is Source.CHAPTERS]
    others = [c for c in sane if c.source is not Source.CHAPTERS]
    guard_pool = others
    if chapters:
        decision = _decide_from_chapters(mtype, chapters, others, ctx)
        # Another chapter of this type can be one side of a contradicting pair (e.g. a second "Intro"
        # chapter that SkipDB agrees with); only the chosen chapter itself is left out.
        chosen = min(chapters, key=lambda c: _chapter_choice_key(c, ctx.duration_ms))
        guard_pool = [c for c in sane if c is not chosen]
    elif cliques := _agreeing_cliques(sane, mtype, ctx):
        decision = _decide_from_cliques(mtype, cliques, ctx)
    else:
        decision = _decide_from_single_source(mtype, sane, ctx)

    if decision.status is DecisionStatus.DECIDED:
        contradicting = _contradicting_groups(decision.marker, guard_pool, ctx)
        if contradicting:
            reason = f"agreeing sources contradict the result: {', '.join(contradicting)}"
            return _review(mtype, decision.marker, reason)
    return decision


def _overlap_ms(a: Marker, b: Marker) -> int:
    return max(0, min(a.end_ms, b.end_ms) - max(a.start_ms, b.start_ms))


def _demote(decision: TypeDecision, reason: str) -> TypeDecision:
    return _review(decision.type, decision.marker, reason)


def _apply_overlap_demotions(out: dict[MarkerType, TypeDecision]) -> None:
    """Cross-type overlap checks, run once every per-type decision is already made.

    Both markers in a pair are read before either is possibly rewritten, so demoting one can
    never change what the other pair-check sees.
    """
    intro_d, recap_d = out[MarkerType.INTRO], out[MarkerType.RECAP]
    if (
        intro_d.status is DecisionStatus.DECIDED
        and recap_d.status is DecisionStatus.DECIDED
        and _overlap_ms(intro_d.marker, recap_d.marker) > INTRO_RECAP_MAX_OVERLAP_MS
    ):
        if not intro_d.marker.locked:
            out[MarkerType.INTRO] = _demote(intro_d, "intro and recap overlap")
        if not recap_d.marker.locked:
            out[MarkerType.RECAP] = _demote(recap_d, "intro and recap overlap")

    preview_d, credits_d = out[MarkerType.PREVIEW], out[MarkerType.CREDITS]
    if (
        preview_d.status is DecisionStatus.DECIDED
        and credits_d.status is DecisionStatus.DECIDED
        and _overlap_ms(preview_d.marker, credits_d.marker) > PREVIEW_CREDITS_MAX_OVERLAP_MS
        and not preview_d.marker.locked
    ):
        # Credits always keep -- only preview is ever demoted by this check.
        out[MarkerType.PREVIEW] = _demote(preview_d, "preview overlaps credits")


def decide(
    candidates: list[Candidate], ctx: DecisionContext, locked: dict[MarkerType, Marker]
) -> dict[MarkerType, TypeDecision]:
    """Decide every marker type for one file.

    Args:
        candidates: All evidence gathered so far (any types, any sources, any order).
        ctx: File duration, movie flag, publish setting, enabled types and the user's source order.
        locked: User-locked markers by type. A lock always wins, including over a disabled type, and
            is returned with `locked=True` so the cross-type overlap checks never demote it, whatever
            the passed value's own flag says. A value that isn't a :class:`Marker`, or whose type
            doesn't match its key, is ignored.

    Returns:
        A decision for every :class:`MarkerType`.
    """
    out: dict[MarkerType, TypeDecision] = {}
    for mtype in MarkerType:
        lock = locked.get(mtype)
        if isinstance(lock, Marker) and lock.type is mtype:
            out[mtype] = TypeDecision(mtype, DecisionStatus.DECIDED, replace(lock, locked=True), None, "locked by user")
        elif mtype not in ctx.enabled_types:
            out[mtype] = TypeDecision(mtype, DecisionStatus.DISABLED, None, None, "detection off")
        else:
            out[mtype] = _decide_type(mtype, candidates, ctx)

    _apply_overlap_demotions(out)
    return out
