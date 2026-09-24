"""Decision rules (spec §5.5): locks, sanity bounds, chapters, agreeing independent sources, a single source at
"Medium", the cross-type overlap checks, and then shortening credits/previews to the servers' own markers.

Every result depends only on the set of candidates, never on the order they arrive in.
"""

from __future__ import annotations

import math
import re
import statistics
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from itertools import combinations

from .models import SERVER_SOURCES, Candidate, Marker, MarkerType, Source
from .speed import online_time_scale

INTRO_END_TOLERANCE_MS = 5_000
CREDITS_START_TOLERANCE_MS = 10_000
EOF_CLAMP_MS = 2_000
MIN_SEGMENT_MS = 3_000
MAX_INTRO_MS = 300_000
MOVIE_CREDITS_MAX_FROM_END_MS = 900_000
INTRO_RECAP_MAX_OVERLAP_MS = 5_000
PREVIEW_CREDITS_MAX_OVERLAP_MS = 10_000
# Finding F1 (phase-1 scale run): streaming releases can label story "Intro" (Reservation Dogs S01E05: 126 s against
# the season's 3-11 s). With at least this many other episodes of the season carrying an intro chapter, one lasting
# more than max(2 x their median, median + 30 s) needs an agreeing source.
SEASON_INTRO_CHAPTERS_MIN_OTHERS = 2
SEASON_INTRO_CHAPTER_MIN_MARGIN_MS = 30_000
LONG_INTRO_CHAPTER_REASON = "Intro chapter is much longer than the rest of the season's"
# IntroDB data looks partly seeded from other sources (spec §5.5), so IntroDB and TheIntroDB are always one
# independence group -- never two votes, whether or not they agree with each other.
_INTRODB_GROUP = "introdb/theintrodb"
_INDEPENDENCE_GROUP = {
    Source.INTRODB: _INTRODB_GROUP,
    Source.THEINTRODB: _INTRODB_GROUP,
    # An importer plugin whose database can't be told (rule 8): the crowd group, as before the database was read.
    Source.SERVER_MARKERS_IMPORTED: _INTRODB_GROUP,
    # The previous season's audio is the same method on the same show: never a second opinion for season audio.
    Source.SEASON_AUDIO_PREVIOUS: Source.SEASON_AUDIO.value,
}
# Markers an importer plugin wrote on a Jellyfin/Emby server are the database it imports again (rule 8; ruling
# 2026-09-16): a SkipDB importer's copy never agrees with SkipDB. AniSkip isn't a source yet and nothing has measured
# what it copies, so its copies stay with the crowd group until phase 4 does (precision first). Only the SkipDB row
# changes a result today: the other two restate the default above, spelled out so the AniSkip choice is a visible
# decision, not a fallback someone "simplifies" away.
_IMPORTED_GROUP = {"introdb": _INTRODB_GROUP, "skipdb": Source.SKIPDB.value, "aniskip": _INTRODB_GROUP}
# At "Medium" a lone source publishes only when it checks this file's cut itself (rule 6): chapters, SkipDB's
# duration-matched intros and recaps, credits text, which reads this file's own frames (owner, Q1, 2026-09-16), and
# season audio's intros (owner, 2026-09-24, overriding R2: alone 91 useful / 13 wrong / 14 missed on 118 episodes,
# against Plex's own 23 right / 15 wrong; "if it doesn't exist online then use the GPU/CPU check"). IntroDB takes no
# duration, TheIntroDB answers the closest cut it has, and markers already on servers never decide alone (rule 7). The
# previous season's audio stays a hint: alone it was 48 useful / 10 wrong / 24 missed (precision 83 %), and the owner
# ruled on 2026-09-13 that it needs a second source.
_AGREEMENT_ONLY = SERVER_SOURCES | {
    Source.INTRODB,
    Source.THEINTRODB,
    Source.SEASON_AUDIO_PREVIOUS,
}
# Season audio doesn't confirm markers already on a server on its own: a server's own intro detection matches audio across
# episodes too (ruling G3, precision first). Agreement needs a source outside these.
_AUDIO_OR_SERVER = SERVER_SOURCES | {Source.SEASON_AUDIO, Source.SEASON_AUDIO_PREVIOUS}
SEASON_AUDIO_WITH_SERVER_REASON = (
    "Season audio and a server's own marker agree, but both come from matching audio; needs another source"
)
# The level the app decides at: one source that checks the file itself may decide alone (rule 6). The stricter "high"
# (always two agreeing sources) left most of a library in Needs review and was removed from Settings (owner ruling
# 2026-09-24); ``DecisionContext`` still takes it for the evaluation harness.
APP_PUBLISH_WHEN = "medium"
# How a Needs review reason names a source (the words Settings uses for it).
_SOURCE_LABELS = {
    Source.CHAPTERS: "chapters",
    Source.THEINTRODB: "TheIntroDB",
    Source.INTRODB: "IntroDB",
    Source.SKIPDB: "SkipDB",
    Source.SEASON_AUDIO: "season audio",
    Source.SEASON_AUDIO_PREVIOUS: "the previous season's audio",
    Source.CREDITS_TEXT: "on-screen text",
    Source.SERVER_MARKERS: "a server's own marker",
    Source.SERVER_MARKERS_IMPORTED: "a server's imported marker",
}
_READS_THE_FILE = frozenset({Source.CHAPTERS, Source.SEASON_AUDIO, Source.SEASON_AUDIO_PREVIOUS, Source.CREDITS_TEXT})
_NEEDS_ANOTHER_SOURCE = "it needs another source to agree"
_ONLINE_ALONE = "an online answer needs a check against the file"
_SERVER_ALONE = "a server's marker needs another source to agree"
# Why a lone answer from a source that never decides alone (rules 6 and 7) waits for a second one.
_WHY_NOT_ALONE = {
    Source.INTRODB: _ONLINE_ALONE,
    Source.THEINTRODB: _ONLINE_ALONE,
    Source.SKIPDB: _ONLINE_ALONE,
    Source.SEASON_AUDIO: "matching audio needs another source to agree",
    Source.SEASON_AUDIO_PREVIOUS: "matching audio needs another source to agree",
    Source.SERVER_MARKERS: _SERVER_ALONE,
    Source.SERVER_MARKERS_IMPORTED: _SERVER_ALONE,
}
_START_SEGMENTS = (MarkerType.INTRO, MarkerType.RECAP)
# IntroDB and TheIntroDB take no duration: their times come from whichever release their users timed, so on a file at
# the other speed of a PAL speed-up they run 4.3 % off (Bones S07E01, 25 fps: IntroDB 324-354 s from a 23.976 release,
# the file's own theme 310-338 s). An importer plugin's copy of them is the same times. SkipDB matches the file's
# duration, so its times are this file's.
_TIMED_ON_ANY_RELEASE = frozenset({Source.INTRODB, Source.THEINTRODB})
_TIMED_ON_ANY_RELEASE_COPY = "introdb"
# An IntroDB or TheIntroDB intro (or an importer plugin's copy of one) starting in the first 2 s and shorter than 10 s
# is a logo at the start of the file, not the show's intro (The Fixers: IntroDB gives Netflix's "N", 0-7 s, for all
# 10 episodes, where season audio finds the theme at 263.0-284.8 s on E01). Season audio passes over the same
# stretches of its own (``audio.season``'s ``FILE_START_S`` and ``MIN_FILE_START_LENGTH_S``, which a test keeps equal
# to these); none of 43 verified online intros is one.
ONLINE_LOGO_BEFORE_MS = 2_000
MIN_ONLINE_INTRO_AT_START_MS = 10_000
SHORTENED_NOTE = "shortened to the server's own marker"
_SHORTENED_RE = re.compile(r"; start shortened to the server's own marker(?: \(([^)]*)\))?$")
_ENUM_ORDER = {source: i for i, source in enumerate(Source)}


class DecisionStatus(str, Enum):
    """Outcome of deciding one marker type for one file."""

    DECIDED = "decided"
    NEEDS_REVIEW = "needs_review"
    NO_EVIDENCE = "no_evidence"
    DISABLED = "disabled"


@dataclass(frozen=True)
class DecisionContext:
    """Per-file inputs to the rules.

    Attributes:
        intro_chapter_limit_ms: The season's limit on an intro chapter deciding alone (:func:`intro_chapter_limit_ms`
            of the other episodes); None when the season can't tell.
        movie_credits_max_from_end_ms: How far before the end a movie's credits may start. It follows a credits
            window the user set beyond the default, or a longer window would decode more and then discard what it found.
        credits_window_ms: The credits window the user chose for this file's kind, 0 for Automatic. Credits starting
            within it are sane even where the last-25% rule would refuse them, for the same reason: a window the user
            widened must not decode more only for the answer to be discarded.
        frame_rate: The file's video frame rate, None when unknown. On a 25 fps or film-rate file an IntroDB or
            TheIntroDB answer timed on a release at the other speed is read on this file's clock
            (:func:`_on_file_clock`).
    """

    duration_ms: int
    is_movie: bool
    publish_when: str
    enabled_types: frozenset[MarkerType]
    source_order: tuple[str, ...]
    intro_chapter_limit_ms: int | None = None
    movie_credits_max_from_end_ms: int = MOVIE_CREDITS_MAX_FROM_END_MS
    credits_window_ms: int = 0
    frame_rate: float | None = None


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
    problem = _times_problem(candidate, ctx)
    if problem is None and _is_online_logo(candidate, ctx.duration_ms):
        return "online intro shorter than 10 s from the file's first 2 s: a logo at the start of the file"
    return problem


def _is_online_logo(candidate: Candidate, duration_ms: int) -> bool:
    """An intro of IntroDB, TheIntroDB or an importer plugin's copy of them (:func:`timed_on_any_release`) starting in
    the first 2 s and shorter than 10 s (``ONLINE_LOGO_BEFORE_MS``)."""
    if candidate.type is not MarkerType.INTRO or not timed_on_any_release(candidate):
        return False
    length = resolve_end_ms(candidate, duration_ms) - candidate.start_ms
    return candidate.start_ms < ONLINE_LOGO_BEFORE_MS and length < MIN_ONLINE_INTRO_AT_START_MS


def _times_problem(candidate: Candidate, ctx: DecisionContext) -> str | None:
    """:func:`sanity_problem`'s checks of the times alone, whichever source gave them."""
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
        window_ms = ctx.credits_window_ms if candidate.type is MarkerType.CREDITS else 0
        if start < _earliest_placed_ms(d, window_ms):
            return f"{candidate.type.value} starts before the last 25% of the file"
        if candidate.type is MarkerType.CREDITS and start < earliest_credits_start_ms(
            d,
            is_movie=ctx.is_movie,
            credits_window_ms=ctx.credits_window_ms,
            movie_credits_max_from_end_ms=ctx.movie_credits_max_from_end_ms,
        ):
            return f"movie credits start more than {ctx.movie_credits_max_from_end_ms // 1000} s before the end"
    return None


@dataclass(frozen=True)
class FileLimits:
    """The facts about one file that a server's own marker is checked against (:func:`unusable_server_marker`).

    Plain values so they travel to the Plex marker agent with a write; a new reason's facts are added here.

    Attributes:
        duration_ms: The file's duration; 0 or less when unknown.
    """

    duration_ms: int


def unusable_server_marker(candidate: Candidate, limits: FileLimits) -> str | None:
    """Why a server's own marker can't be right for this file at all, or None when it may be.

    "Keep Plex's" means using Plex's marker when Plex has already processed the file, and a server's marker stops our
    detection of its type for the same reason. Only an impossible marker is refused: one that starts before 0 or past
    the end of the file, ends past it (beyond the same 2 s clamp our answers get), ends before it starts, or is
    shorter than any real segment. That came from another cut of the file or a bad analysis, so the server counts as
    not having processed it. The placement rules our own answers must also pass (where an intro or credits may sit)
    are left out: the server may know its file better. Each new reason is added here, which the pipeline and Plex's
    publisher both call.

    Args:
        candidate: The server's marker, in the times the server serves.
        limits: This file's limits.

    Returns:
        The reason, or None when the marker may be kept (always, for a file of unknown length).
    """
    d = limits.duration_ms
    if d <= 0:
        return None
    start, end = candidate.start_ms, candidate.end_ms
    if start < 0:
        return "negative start"
    if start >= d:
        return "starts past the end of the file"
    if end is not None and end < start:
        return "ends before it starts"
    if end is not None and end > d + EOF_CLAMP_MS:
        return "ends past the end of the file"
    if resolve_end_ms(candidate, d) - start < MIN_SEGMENT_MS:
        return "segment too short"
    return None


def _earliest_placed_ms(duration_ms: int, window_ms: int) -> int:
    """The earliest credits or preview start the position rule keeps: the last 25 % of the file, or anywhere inside a
    window the user chose (0 for none) that doesn't reach before the middle of the file -- so a window longer than a
    short file can't switch the rule off.

    Integer ceilings, so ``start >= this`` is exactly the cross-multiplication ``start * 100 >= 75 * d`` (and
    ``start * 2 >= d``), with no float rounding at the boundary.
    """
    last_quarter = -(-75 * duration_ms // 100)
    inside_window = max(duration_ms - window_ms, -(-duration_ms // 2))
    return min(last_quarter, inside_window)


def earliest_credits_start_ms(
    duration_ms: int, *, is_movie: bool, credits_window_ms: int, movie_credits_max_from_end_ms: int
) -> int:
    """The earliest credits start rule 2 keeps for a file; :func:`sanity_problem` refuses every earlier one.

    The credit text detector reads before its tail no further than this allows (``detector.find_credits``), so the
    two read the same numbers: a start it could find earlier would only be refused here.

    Args:
        duration_ms: The file's duration.
        is_movie: The file is a movie (the movie cap applies).
        credits_window_ms: The credits window the user chose for the file's kind, 0 for Automatic.
        movie_credits_max_from_end_ms: How far before the end a movie's credits may start.

    Returns:
        Milliseconds from the start of the file: the last 25 % (or a chosen window, never before the middle), and for
        a movie no more than ``movie_credits_max_from_end_ms`` before the end.
    """
    earliest = _earliest_placed_ms(duration_ms, credits_window_ms)
    if is_movie:
        earliest = max(earliest, duration_ms - movie_credits_max_from_end_ms)
    return earliest


def credits_limits_ms(*, is_episode: bool, tv_window_s: int | None, movie_window_s: int | None) -> tuple[int, int]:
    """A file's credits window and movie cap, from the windows the user chose (``markers.credits_window``).

    Args:
        is_episode: The file is a TV episode (it has a season); anything else follows the movie window.
        tv_window_s: The window for TV episodes, None for Automatic.
        movie_window_s: The window for movies and files of unknown kind, None for Automatic.

    Returns:
        ``(credits_window_ms, movie_credits_max_from_end_ms)`` as :class:`DecisionContext` takes them: the window of
        the file's kind (0 for Automatic), and the movie cap, which follows a movie window above
        ``MOVIE_CREDITS_MAX_FROM_END_MS`` but never drops below it.
    """
    chosen = tv_window_s if is_episode else movie_window_s
    return (chosen or 0) * 1000, max(MOVIE_CREDITS_MAX_FROM_END_MS, (movie_window_s or 0) * 1000)


def _group(candidate: Candidate) -> str:
    if candidate.source is Source.SERVER_MARKERS_IMPORTED and candidate.copied_from in _IMPORTED_GROUP:
        return _IMPORTED_GROUP[candidate.copied_from]
    return _INDEPENDENCE_GROUP.get(candidate.source, candidate.source.value)


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
    Two candidates only tie when they differ in nothing but `origin` or `copied_from`, or in confidences that
    all rank as 0.0 (NaN/inf); either way they publish identical markers (an importer copy, the only candidate
    `copied_from` sets, never supplies an edge alone) -- so no result can depend on input order.
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
    that is neither a server marker nor season audio (so it may supply times, and season audio and a server's own
    detection never decide together).

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
        if len({_group(c) for c in window}) >= 2 and any(c.source not in _AUDIO_OR_SERVER for c in window):
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
    """Whether a composed marker's times pass :func:`sanity_problem`. The online-logo check judges an online answer as
    it came, not a marker composed from one it kept and another source's edge, so it isn't asked here."""
    probe = Candidate(marker.type, marker.start_ms, marker.end_ms, Source(marker.decided_by[0]))
    return _times_problem(probe, ctx) is None


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

    An intro's or recap's end: with a confirming source that reads this file (``_READS_THE_FILE``), an answer
    :func:`timed_on_any_release` doesn't supply it, whatever the order -- its times come from another release (South
    Park S01: IntroDB's 30.0 s and season audio's 33.6 s agree, the theme ends at 35.5 s). Credits keep the order.
    """
    confirmed = [c for c in cluster if any(_group(o) != _group(c) and _agree(c, o, ctx.duration_ms) for o in cluster)]
    confirmed_non_server = [c for c in confirmed if c.source not in SERVER_SOURCES]
    rank = _sort_key(ctx)
    file_edge = mtype in _START_SEGMENTS and any(c.source in _READS_THE_FILE for c in confirmed_non_server)
    winner = min(confirmed_non_server, key=lambda c: (file_edge and timed_on_any_release(c), rank(c)))
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
    pairs = [(a, b) for a, b in combinations(far, 2) if _group(a) != _group(b) and _agree(a, b, d)]
    return sorted({_group(c) for pair in pairs for c in pair})


def _chapter_choice_key(c: Candidate, duration_ms: int) -> tuple[int, int]:
    """First intro/recap chapter or last credits/preview chapter; on a start tie, the earlier end (shorter skip)."""
    start = c.start_ms if c.type in _START_SEGMENTS else -c.start_ms
    return start, resolve_end_ms(c, duration_ms)


def intro_chapter_length_ms(candidates: Iterable[Candidate], duration_ms: int) -> int | None:
    """How long the intro chapter the rules would decide from lasts (one episode's input to the season check).

    Args:
        candidates: The episode's evidence (only its sane intro chapters count).
        duration_ms: The episode's duration.

    Returns:
        The chosen intro chapter's length, or None when the episode has no sane intro chapter.
    """
    sanity = DecisionContext(duration_ms, False, "high", frozenset({MarkerType.INTRO}), ())
    chapters = [
        c
        for c in candidates
        if c.source is Source.CHAPTERS and c.type is MarkerType.INTRO and sanity_problem(c, sanity) is None
    ]
    if not chapters:
        return None
    chosen = min(chapters, key=lambda c: _chapter_choice_key(c, duration_ms))
    return resolve_end_ms(chosen, duration_ms) - chosen.start_ms


def intro_chapter_limit_ms(other_lengths_ms: Sequence[int]) -> int | None:
    """The longest intro chapter that may still decide alone, from the season's other intro chapters (finding F1).

    Args:
        other_lengths_ms: :func:`intro_chapter_length_ms` of every other episode of the season that has one.

    Returns:
        ``max(2 x median, median + 30 s)`` rounded down (lengths are whole ms, so "longer than" is unchanged), or None
        with fewer than two other episodes.
    """
    if len(other_lengths_ms) < SEASON_INTRO_CHAPTERS_MIN_OTHERS:
        return None
    median = statistics.median(other_lengths_ms)
    return math.floor(max(2 * median, median + SEASON_INTRO_CHAPTER_MIN_MARGIN_MS))


def _decide_from_chapters(
    mtype: MarkerType, chapters: list[Candidate], others: list[Candidate], ctx: DecisionContext
) -> TypeDecision:
    """Accept the first intro/recap or last credits/preview chapter unless agreeing sources contradict it.

    When >= 2 independent non-chapter groups agree with the chapter's checked edge, and the safer
    unchecked edge across those agreeing candidates (server markers included: they may shorten the
    skip) is safer than the chapter's own, it replaces the chapter's; decided_by then adds every
    agreeing source. A replacement that fails sanity sends the type to review.

    An intro chapter longer than the season's limit (``ctx.intro_chapter_limit_ms``) needs one agreeing group instead
    of none, and not markers already on a server (a chapter they alone confirm still counts as chapters alone, rule
    7); the agreeing candidates then shorten the skip the same way and are always credited.
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
    limit = ctx.intro_chapter_limit_ms
    suspect = (
        mtype is MarkerType.INTRO and limit is not None and chapter_marker.end_ms - chapter_marker.start_ms > limit
    )
    if suspect and all(c.source in SERVER_SOURCES for c in agreeing):
        return _review(mtype, chapter_marker, LONG_INTRO_CHAPTER_REASON)
    if len({_group(c) for c in agreeing}) >= (1 if suspect else 2):
        other_edge, _ = _safer_other_edge(mtype, agreeing, ctx)
        if mtype in _START_SEGMENTS:
            safer = other_edge > chapter_marker.start_ms
        else:
            safer = other_edge < chapter_marker.end_ms
        sources = {Source.CHAPTERS, *(c.source for c in agreeing)}
        if safer:
            marker = _composed_marker(mtype, chapter_value, other_edge, sources, ctx)
            if not _marker_is_sane(marker, ctx):
                return _review(mtype, chapter_marker, "chapters and agreeing sources disagree on the other edge")
        elif suspect:  # an intro: the chapter keeps its own start
            marker = _composed_marker(mtype, chapter_value, chapter_marker.start_ms, sources, ctx)
    return TypeDecision(mtype, DecisionStatus.DECIDED, marker, None, "chapters")


def _decide_from_cliques(mtype: MarkerType, cliques: list[list[Candidate]], ctx: DecisionContext) -> TypeDecision:
    composed = [_compose_cluster(cl, mtype, ctx) for cl in cliques]
    values = [_checked_value(mtype, m) for m, _ in composed]
    if max(values) - min(values) > _tolerance_ms(mtype):
        rank = _sort_key(ctx)
        # Stable sort: clusters sharing a winner keep ascending compared-value order.
        ranked = sorted(composed, key=lambda mw: rank(mw[1]))
        names = " vs ".join(dict.fromkeys(_group(winner) for _, winner in ranked))
        return _review(mtype, ranked[0][0], f"agreeing sources conflict: {names}")

    merged = list({id(c): c for cl in cliques for c in cl}.values())
    marker, winner = _compose_cluster(merged, mtype, ctx)
    if not _marker_is_sane(marker, ctx):
        return _review(mtype, _own_marker(winner, ctx), "agreeing sources disagree on the other edge")
    return TypeDecision(mtype, DecisionStatus.DECIDED, marker, None, "sources agree: " + ", ".join(marker.decided_by))


def _may_decide_alone(candidate: Candidate) -> bool:
    """Whether a candidate's source checks this file's cut well enough to publish alone at "Medium" (rule 6): chapters,
    credits text, season audio for intros, and SkipDB for intros and recaps.

    SkipDB's duration match holds for intros and recaps, not for credits or previews: on the lab scale run every lone
    SkipDB credits answer started early, some by minutes (Battlestar Galactica S04E05: 6.7 min of story). Season audio
    was only ever measured on intros (its credits were rejected at 54 % precision), so only an intro of it decides.
    """
    if candidate.source in _AGREEMENT_ONLY:
        return False
    if candidate.source is Source.SEASON_AUDIO:
        return candidate.type is MarkerType.INTRO
    return candidate.type in _START_SEGMENTS or candidate.source is not Source.SKIPDB


def _decide_from_single_source(mtype: MarkerType, sane: list[Candidate], ctx: DecisionContext) -> TypeDecision:
    """No two independent sources agree. Only "medium" may publish, and only a lone, self-consistent group that
    checks this file's cut itself (not IntroDB/TheIntroDB, not the previous season's audio, not markers already on
    servers; SkipDB only for an intro or recap, season audio only for an intro).

    Any second independent group here stops "medium" when it disagrees, or when it agrees without being able to
    form a cluster (a server's own markers and an importer plugin's copy). The one exception is G3's: markers already
    on a server that agree with season audio are no second source (a server's intro detection matches audio too), but
    they don't hold it back either (owner's rule 2026-09-24, "use the file check if nothing else"), so season audio
    decides exactly as it would alone and decided_by doesn't name them. Only season audio gets here that way: any
    other source that may decide alone forms a cluster with an agreeing server marker. A group whose own candidates
    don't all agree pairwise contradicts itself. The checked edge comes from the best-ranked candidate, the unchecked
    edge is the safer value across the group's candidates, and decided_by names the sources that supplied either edge.
    """
    ranked = sorted(sane, key=_sort_key(ctx))
    groups = sorted({_group(c) for c in sane})
    proposal = next((c for c in ranked if _may_decide_alone(c)), None)
    own = [c for c in sane if proposal is not None and _group(c) == _group(proposal)]
    others = [c for c in sane if proposal is None or _group(c) != _group(proposal)]
    servers_only_agree = all(c.source in SERVER_SOURCES for c in others) and all(
        _agree(o, c, ctx.duration_ms) for o in others for c in own
    )
    if ctx.publish_when == "medium" and proposal is not None and servers_only_agree:
        if not all(_agree(a, b, ctx.duration_ms) for a, b in combinations(own, 2)):
            return _review(mtype, _own_marker(proposal, ctx), "source disagrees with itself")
        other_edge, edge_suppliers = _safer_other_edge(mtype, own, ctx)
        sources = {proposal.source, *(c.source for c in edge_suppliers)}
        marker = _composed_marker(mtype, _agree_value(proposal, ctx.duration_ms), other_edge, sources, ctx)
        if not _marker_is_sane(marker, ctx):
            return _review(mtype, _own_marker(proposal, ctx), "sources disagree on the other edge")
        return TypeDecision(mtype, DecisionStatus.DECIDED, marker, None, f"single source ({proposal.source.value})")
    disagree = any(_group(a) != _group(b) and not _agree(a, b, ctx.duration_ms) for a, b in combinations(sane, 2))
    if disagree:
        reason = f"sources disagree: {', '.join(groups)}"
    elif _only_audio_and_server_markers({c.source for c in sane}):
        reason = SEASON_AUDIO_WITH_SERVER_REASON
    else:
        alone_allowed = proposal is not None and ctx.publish_when != "medium"
        reason = _lone_answer_reason(mtype, sane, ranked[0], alone_allowed=alone_allowed)
    return _review(mtype, _own_marker(ranked[0], ctx), reason)


def _lone_answer_reason(mtype: MarkerType, sane: list[Candidate], best: Candidate, *, alone_allowed: bool) -> str:
    """Why a type nothing disagrees about still needs review: only one source (or only copies of one) answered.

    Args:
        mtype: The marker type.
        sane: Its candidates that passed the sanity checks.
        best: The best-ranked of them.
        alone_allowed: One of them could decide alone, so only ``publish_when`` "high" held it back (the evaluation
            harness; the app decides at :data:`APP_PUBLISH_WHEN`).

    Returns:
        E.g. ``only IntroDB has the intro; an online answer needs a check against the file``, or
        ``only season audio found the intro; matching audio needs another source to agree``.
    """
    sources = sorted({c.source for c in sane}, key=_ENUM_ORDER.__getitem__)
    names = " and ".join(dict.fromkeys(_SOURCE_LABELS.get(s, s.value) for s in sources))
    if alone_allowed:
        return f'only {names} found the {mtype.value}; at "high" a second source must agree'
    if set(sources) <= _READS_THE_FILE:
        found = "found"
    else:
        found = "has" if len(sources) == 1 else "have"
    return f"only {names} {found} the {mtype.value}; {_WHY_NOT_ALONE.get(best.source, _NEEDS_ANOTHER_SOURCE)}"


def _only_audio_and_server_markers(sources: set[Source]) -> bool:
    """Whether the sources are season audio plus markers already on a server and nothing else (ruling G3)."""
    return bool(sources <= _AUDIO_OR_SERVER and sources & SERVER_SOURCES and sources - SERVER_SOURCES)


def timed_on_any_release(candidate: Candidate) -> bool:
    """Whether a candidate's times may come from another release than this file: IntroDB, TheIntroDB, or an importer
    plugin's copy of them (they take no duration)."""
    if candidate.source is Source.SERVER_MARKERS_IMPORTED:
        return candidate.copied_from == _TIMED_ON_ANY_RELEASE_COPY
    return candidate.source in _TIMED_ON_ANY_RELEASE


def file_clock_may_matter(candidates: Iterable[Candidate], duration_ms: int) -> bool:
    """Whether reading online times on the file's clock (:func:`_on_file_clock`) can change a decision: an answer
    :func:`timed_on_any_release` has a candidate of its type from another independent group to agree with, and as it
    is it agrees with none of them or its times fall outside the file. A raw reading that agrees is kept unscaled, so
    the file's frame rate can't move it.

    Args:
        candidates: A file's evidence (of the sources turned on).
        duration_ms: The file's duration; 0 or less when unknown (then any disagreement may matter).

    Returns:
        True when the file's frame rate is worth reading for its decision.
    """
    found = list(candidates)
    for c in found:
        if not timed_on_any_release(c):
            continue
        others = [o for o in found if o.type is c.type and _group(o) != _group(c)]
        if not others:
            continue
        outside = duration_ms > 0 and (
            c.start_ms >= duration_ms or (c.end_ms is not None and c.end_ms > duration_ms + EOF_CLAMP_MS)
        )
        if outside or not any(_agree(c, o, duration_ms) for o in others):
            return True
    return False


def _on_file_clock(of_type: list[Candidate], ctx: DecisionContext) -> list[Candidate]:
    """One type's candidates that pass :func:`sanity_problem`, with online times read on the file's own clock where
    they were timed on a release at the other speed (``speed.online_time_scale``: a 25 fps file and a film-rate one),
    spec §5.5 rule 12.

    An answer :func:`timed_on_any_release` is read scaled only when, as it is, it agrees with no sane candidate of
    another independent group and, scaled, it is sane and agrees with one; that includes an answer that fails sanity as
    it is (credits timed on the longer film-rate release can start after a 25 fps file ends). A raw reading that agrees
    is always kept: an early intro can agree both ways (4.3 % of a 60 s end is 2.6 s), and scaling it then would move an
    edge no source reported. An answer that agrees neither way stays as it is, so it still disagrees (or, failing
    sanity, is left out). A server's own marker counts as on any file: one made for an earlier file of the item
    (``Candidate.stale``) is dropped as :func:`decide` starts, before this runs, and an older version's answer from a
    Plex server showing our markers isn't stored as evidence (``pipeline._drop_older_reader_answer``).

    Each answer is judged against the sane candidates as they came, so the result doesn't depend on their order.

    Args:
        of_type: One type's candidates.
        ctx: The file's context (its duration and frame rate).

    Returns:
        The sane candidates, a scaled answer in place of its raw one.
    """
    sane = [c for c in of_type if sanity_problem(c, ctx) is None]
    scale = online_time_scale(ctx.frame_rate)
    if scale is None:
        return sane
    d = ctx.duration_ms
    out = []
    for c in of_type:
        raw_sane = any(c is kept for kept in sane)
        if timed_on_any_release(c):
            others = [o for o in sane if _group(o) != _group(c)]
            end_ms = None if c.end_ms is None else round(c.end_ms * scale)
            scaled = replace(c, start_ms=round(c.start_ms * scale), end_ms=end_ms)
            if (
                not (raw_sane and any(_agree(c, o, d) for o in others))
                and sanity_problem(scaled, ctx) is None
                and any(_agree(scaled, o, d) for o in others)
            ):
                out.append(scaled)
                continue
        if raw_sane:
            out.append(c)
    return out


def _decide_type(
    mtype: MarkerType, of_type: list[Candidate], sane: list[Candidate], ctx: DecisionContext
) -> TypeDecision:
    """One type's decision from its candidates (``of_type``) and those of them read on the file's clock (``sane``,
    :func:`_on_file_clock`)."""
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


def _shorten_to_server_markers(decision: TypeDecision, sane: list[Candidate], ctx: DecisionContext) -> TypeDecision:
    """Move a decided credits/preview start later to the servers' own detection when it gives a shorter skip (rule 7).

    Intros and recaps are never moved: on the lab scale run every checked intro end was already right, and Plex's own
    intro ended partway through the opening. Only ``server_markers`` count: an importer plugin's copy isn't checked
    against this file's cut. When any server's own marker covers the decided start or starts within the tolerance of
    it, that server says the credits are already running there, so nothing moves. Otherwise each server offers its
    first start more than the tolerance after the decided start and more than the tolerance before the decided end --
    so a server that splits its credits into pieces can't pull the start to its last piece -- and the latest offer
    wins. Our own markers and another cut's never reach here: the pipeline doesn't store them as evidence. Runs last,
    after the contradiction guard and the overlap checks have judged the unshortened markers, so it can only shorten a
    decided marker and never turn Needs review into decided.

    Args:
        decision: A decided, unlocked type.
        sane: The type's candidates as the rules read them (:func:`_on_file_clock`).
        ctx: The file's context.

    Returns:
        The decision unchanged, the shortened marker with ``server_markers`` added to decided_by, or Needs review
        (the unshortened marker proposed) when the shortened marker fails sanity.
    """
    marker = decision.marker
    mtype = decision.type
    if mtype in _START_SEGMENTS:
        return decision
    tol = _tolerance_ms(mtype)
    own = [c for c in sane if c.source is Source.SERVER_MARKERS]
    if any(
        c.start_ms <= marker.start_ms <= resolve_end_ms(c, ctx.duration_ms) or abs(c.start_ms - marker.start_ms) <= tol
        for c in own
    ):
        return decision
    first_start: dict[str, int] = {}
    for c in own:
        if marker.start_ms + tol < c.start_ms < marker.end_ms - tol:
            first_start[c.origin] = min(c.start_ms, first_start.get(c.origin, c.start_ms))
    if not first_start:
        return decision
    start = max(first_start.values())
    sources = {Source(s) for s in marker.decided_by} | {Source.SERVER_MARKERS}
    shortened = _composed_marker(mtype, start, marker.end_ms, sources, ctx)
    servers = sorted(origin for origin, value in first_start.items() if value == start and origin)
    note = f"start {SHORTENED_NOTE}" + (f" ({', '.join(servers)})" if servers else "")
    if not _marker_is_sane(shortened, ctx):
        return _review(mtype, marker, f"{note} fails sanity checks")
    return replace(decision, marker=shortened, reason=f"{decision.reason}; {note}")


def shortened_by(reason: str) -> tuple[str, ...] | None:
    """Read back which servers' own markers shortened a decided credits/preview start (rule 7).

    Args:
        reason: A decision's reason.

    Returns:
        The server ids (empty when the reason names none), or None when the marker wasn't shortened.
    """
    found = _SHORTENED_RE.search(reason or "")
    if found is None:
        return None
    return tuple(found.group(1).split(", ")) if found.group(1) else ()


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
            doesn't match its key, is ignored. The flag carries on to the publishers, which read it to
            override "Keep Plex's" / "Keep Emby's" for that type (spec §5.5 rule 1, §14 2026-09-20) —
            so nothing here may drop it from a decided marker.

    Returns:
        A decision for every :class:`MarkerType`.
    """
    # A server's own marker made for an earlier file at this path (``Candidate.stale``: Plex keeps an item's markers
    # when its file is replaced) counts for nothing, as an unusable server answer: it confirms, decides and shortens
    # nothing. Every rule below reads ``candidates``, so dropping it here is the one place decide reads the flag.
    candidates = [c for c in candidates if not c.stale]
    out: dict[MarkerType, TypeDecision] = {}
    on_clock: dict[MarkerType, list[Candidate]] = {}
    for mtype in MarkerType:
        lock = locked.get(mtype)
        if isinstance(lock, Marker) and lock.type is mtype:
            out[mtype] = TypeDecision(mtype, DecisionStatus.DECIDED, replace(lock, locked=True), None, "locked by user")
        elif mtype not in ctx.enabled_types:
            out[mtype] = TypeDecision(mtype, DecisionStatus.DISABLED, None, None, "detection off")
        else:
            of_type = [c for c in candidates if c.type is mtype]
            on_clock[mtype] = _on_file_clock(of_type, ctx)
            out[mtype] = _decide_type(mtype, of_type, on_clock[mtype], ctx)

    _apply_overlap_demotions(out)
    for mtype, decision in out.items():
        if decision.status is DecisionStatus.DECIDED and not decision.marker.locked:
            out[mtype] = _shorten_to_server_markers(decision, on_clock[mtype], ctx)
    return out
