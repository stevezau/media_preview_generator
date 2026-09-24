"""Season step (spec §5.3, §6.2 step 4, §6.4 items 4–5): the v3 matcher over a season's episodes, and the season's intro
chapters (finding F1).

A season group is the episodes of a file's folder with the same season number in their names, at most the 40 nearest by
episode number (a flat folder can hold hundreds). Fingerprinting needs a worker (CPU ffmpeg, at most two at once): the
first episode of a group to get one fingerprints every member that has none, so the rest of the season matches from
cached fingerprints on the checking threads. An episode alone in its folder (a new season's first weekly release) matches against up to four cached
episodes of the previous season; that answer is a hint (``Source.SEASON_AUDIO_PREVIOUS``). Neither decides alone in
phase 2 (owner, 2026-09-14). Episodes whose answer is out of date and whose intro is still undecided, or was decided
with that answer, are handed to the job as follow-ups, which a "Season" job decides again: by the run that fingerprints
the season, and by every run of an episode of the group (one that arrives decided by its chapters never matches).

The matcher's clusters are walked with guards against a network ident or a cold-open music bed ahead of the title card
(``FILE_START_S`` below); the end-picture check among them decodes video, on a worker, and its shares are cached.
"""

from __future__ import annotations

import bisect
import functools
import hashlib
import json
import math
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

import numpy as np
from loguru import logger

from ...plex_client import VIDEO_EXTENSIONS
from ..decide import DecisionStatus, intro_chapter_length_ms, intro_chapter_limit_ms
from ..external_ids import ids_from_path, is_extra
from ..models import Candidate, FileIdentity, MarkerType, Source
from ..outcomes import is_kept_own
from ..probe import MediaProbe, ProbeError, ProbeStalledError, probe_media
from ..sources.chapters import CHAPTER_RULES_VERSION, chapter_candidates
from ..speed import FILM_FPS, PAL_FPS, match_speed, playback_speed, retime_factor
from ..store import EndPictureKey
from . import POINT_S, end_picture
from .fingerprint import (
    FingerprintError,
    FingerprintSkippedError,
    FingerprintStalledError,
    cached_fingerprint,
    chromaprint_ffmpeg,
    ensure_fingerprint,
    has_cached_fingerprint,
    points_of,
)
from .matcher import (
    MAX_BIT_DIFF,
    MAX_GAP_S,
    MAX_INTRO_S,
    TOP_SHIFTS,
    Hit,
    IntroCandidate,
    IntroSegment,
    Run,
    file_hits,
    intro_candidates,
    meets_quorum,
    pair_runs,
)

if TYPE_CHECKING:
    from ..pipeline import DetectorAnswer, LocalDetectorSpec, PipelineContext
    from ..store import FileRecord

# The season step's own version (matcher v3 plus the silence guard, the provable pair skip, the group rule, the guards
# against idents and music beds with where a stretch needs no dense core, and matching a season of 25 fps and film-rate
# releases at one speed): stored with its answers and with cached pairs, so a change to any of them is matched again.
SEASON_AUDIO_VERSION = 7
# A pair's runs are cached under a version naming the speed each side was matched at (its own, or retimed to film or
# to 25 fps), so runs matched from one pair of fingerprints never stand in for another pair's. A file's cached pairs
# also go when its stored frame rate changes (``MarkerStore.set_frame_rate``).
_PAIR_VERSION_STEP = 1_000
_RETIMED_TO = {FILM_FPS: 1, PAL_FPS: 2}
# Guards on the repeated stretches season audio takes for the intro (§5.3, owner 2026-09-24): a network ident at the
# very start of the file, or a music bed under the cold open, repeats in every episode as well as the theme does
# ("Accused: Guilty or Innocent": 13 of 15 intros were the A&E logo, the logo plus the cold-open music, or the music).
# A stretch starting in the first 2 s must be at least 10 s long; one starting in the first 30 s must end on the same
# picture in the episodes it repeats in (end_picture); and every one needs a dense core: in the median partner, 8 s
# where the two recordings match with gaps of at most 4 points (an ident followed by a music bed, merged by the
# matcher's 3.5 s gap bridge, has none).
FILE_START_S = 2.0
MIN_FILE_START_LENGTH_S = 10.0
MIN_DENSE_CORE_S = 8.0
DENSE_CORE_GAP_PTS = 4
# A theme sung or played under dialogue has no dense core either, so two kinds of stretch are let off it (owner
# 2026-09-24): one starting at 2-30 s that is at least 30 s long (an ident or a music bed that long wasn't seen; its end
# picture is still checked), and one starting after 30 s, past the cold open, that is at least 10 s long and found by
# at least 2 other episodes. The limits were set after seeing what they leave out: an 8.8 s music bed after 30 s
# (Accused S04E06) and a recap only one other episode shares (The Fall, season 3).
CORE_FREE_EARLY_MIN_S = 30.0
CORE_FREE_LATER_MIN_S = 10.0
CORE_FREE_LATER_MIN_SUPPORT = 2
MAX_PREVIOUS_SEASON_FILES = 4
MAX_GROUP_EPISODES = 40
# A season member ffprobe couldn't read, or ffmpeg couldn't fingerprint, is left out of other episodes' season steps for
# this long (while unchanged).
UNREADABLE_MEMBER_RETRY = timedelta(days=1)
# A file the end-picture check couldn't read (an error, a non-zero exit, a timeout) isn't read for it again for this
# long while unchanged: a bad partner would otherwise hold a worker for up to 150 s on every sibling's run.
END_PICTURE_RETRY = timedelta(days=1)
# chromaprint gives silence (and noise too quiet to hear) one constant value, so a shared quiet stretch matches like an
# intro would (the v3 matcher keeps that behaviour).
SILENCE_POINT = 0x256DF977
# An intro whose points are more than half silence (points the matcher would take for the silence value) is dropped.
# The 91 useful intros of the 118-episode eval hold at most 17.5 % (tools/markers_eval; task 7 report).
MAX_INTRO_SILENCE = 0.5
# A pair whose shift search expands more point pairs than this (about 60 ms) is matched on a worker rather than a
# checking thread. Two silent 900 s openings are 53 million (about 1.2 s); the most any same-season pair of the
# 118-episode eval has is 3,893.
MAX_INLINE_PAIR_MATCHES = 2_000_000
# The matcher indexes values within ±2 of each other (matcher._VALUE_SHIFTS) and breaks a run after this many points
# that don't match (matcher._GAP_PTS).
_VALUE_SPREAD = 2
_POPCOUNT8 = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)
_GAP_PTS = int(MAX_GAP_S / POINT_S)
# holds_no_intro's bounds: a run of 969 points lasts more than 120 s, and a run that starts and ends within a gap's reach
# of an overlap's edges is at least the overlap minus 55 points long, so an overlap of 1024 points only holds longer ones.
_TOO_LONG_RUN_PTS = math.floor(MAX_INTRO_S / POINT_S) + 1
_TOO_LONG_OVERLAP_PTS = _TOO_LONG_RUN_PTS + 2 * (_GAP_PTS - 1) + 1
_SEASON_FOLDER_RE = re.compile(r"^(?:season|series|staffel|saison)\s*(\d{1,4})$", re.IGNORECASE)
_SEASON_AUDIO_SOURCES = frozenset({Source.SEASON_AUDIO.value, Source.SEASON_AUDIO_PREVIOUS.value})


@dataclass(frozen=True)
class SeasonGroup:
    """A file's season group (sorted, the asking file included)."""

    folder: str
    episodes: tuple[str, ...]


@dataclass(frozen=True)
class FolderVideo:
    """A video file of a folder with the season and episode numbers its name carries."""

    path: str
    season: int | None
    episode: int | None


def _folder_video(path: str) -> FolderVideo:
    ids = ids_from_path(path)
    return FolderVideo(path, ids.season, ids.episode)


def folder_videos(folder: str) -> tuple[FolderVideo, ...]:
    """The video files of a folder that aren't extras (read once, then shared by every group of the folder)."""
    try:
        with os.scandir(folder) as entries:
            return tuple(
                _folder_video(entry.path)
                for entry in entries
                if os.path.splitext(entry.name)[1].lower() in VIDEO_EXTENSIONS
                and not is_extra(entry.path)
                and entry.is_file()
            )
    except OSError:
        return ()


def season_group(canonical_path: str, videos: Sequence[FolderVideo] | None = None) -> SeasonGroup:
    """The episodes a file is matched with (spec §5.3: the season on disk, server-agnostic).

    Files of its folder with the same season number in their names (files without one are a group of their own); in a
    folder with more than 40 of them, the 40 nearest by episode number, ties by name (by position in name order when a
    name carries no episode number).

    Args:
        canonical_path: Local path of one episode.
        videos: The folder's :func:`folder_videos`, when the caller already read them.

    Returns:
        The folder and the group's paths, sorted, the file itself included.
    """
    folder = os.path.dirname(canonical_path)
    listed = folder_videos(folder) if videos is None else videos
    target = _folder_video(canonical_path)
    members = _same_season(target, listed)
    if len(members) > MAX_GROUP_EPISODES:
        position = {path: i for i, path in enumerate(sorted(members))}

        def distance(video: FolderVideo) -> tuple[int, str]:
            if target.episode is not None and video.episode is not None:
                return abs(video.episode - target.episode), video.path
            return abs(position[video.path] - position[canonical_path]), video.path

        members = {v.path: v for v in sorted(members.values(), key=distance)[:MAX_GROUP_EPISODES]}
    return SeasonGroup(folder, tuple(sorted(members)))


def season_size(canonical_path: str, videos: Sequence[FolderVideo] | None = None) -> int:
    """How many episodes a file's season has before :func:`season_group` caps it to the 40 nearest.

    Args:
        canonical_path: Local path of one episode.
        videos: The folder's :func:`folder_videos`, when the caller already read them.

    Returns:
        The count, the file itself included.
    """
    listed = folder_videos(os.path.dirname(canonical_path)) if videos is None else videos
    return len(_same_season(_folder_video(canonical_path), listed))


def _same_season(target: FolderVideo, videos: Sequence[FolderVideo]) -> dict[str, FolderVideo]:
    members = {v.path: v for v in videos if v.season == target.season}
    members[target.path] = target
    return members


def groups_holding(
    canonical_path: str,
    videos: Sequence[FolderVideo],
    group_of: Callable[[str], SeasonGroup] | None = None,
) -> tuple[str, ...]:
    """The other files of a folder whose own season group holds this file (in a flat folder, not only its group's).

    A group is the 40 nearest files, so a file with 39 or more others strictly between it and this one (by episode
    number, or by name order when names carry none) can't hold it: those are all nearer to it. Only the rest are checked.

    Args:
        canonical_path: Local path of one episode.
        videos: The folder's :func:`folder_videos`.
        group_of: :func:`season_group` of another file of the folder, when the caller keeps them.

    Returns:
        Their paths, sorted.
    """
    target = _folder_video(canonical_path)
    members = _same_season(target, videos)
    paths = sorted(members)
    if len(members) <= MAX_GROUP_EPISODES:
        return tuple(path for path in paths if path != canonical_path)  # every group is the whole season
    group_of = group_of or (lambda path: season_group(path, videos))
    if all(v.episode is not None for v in members.values()):
        rank = {path: members[path].episode for path in paths}
    elif all(v.episode is None for v in members.values()):
        rank = {path: i for i, path in enumerate(paths)}
    else:
        rank = None  # mixed numbering: no order to bound by, so every file is checked
    ranks = sorted(rank.values()) if rank else []
    holding = []
    for path in paths:
        if path == canonical_path:
            continue
        if rank is not None:
            low, high = sorted((rank[path], rank[canonical_path]))
            if bisect.bisect_left(ranks, high) - bisect.bisect_right(ranks, low) >= MAX_GROUP_EPISODES - 1:
                continue
        if canonical_path in group_of(path).episodes:
            holding.append(path)
    return tuple(holding)


def previous_season_files(canonical_path: str) -> tuple[str, ...]:
    """The first episodes (by path, at most 4) of the season folder numbered one lower than this file's.

    Args:
        canonical_path: Local path of one episode.

    Returns:
        Their paths; empty for specials, a first season, or a folder that isn't named like a season.
    """
    folder = os.path.dirname(canonical_path)
    match = _SEASON_FOLDER_RE.match(os.path.basename(folder))
    if not match or int(match.group(1)) <= 1:
        return ()
    wanted, show = int(match.group(1)) - 1, os.path.dirname(folder)
    try:
        names = sorted(os.listdir(show))
    except OSError:
        return ()
    for name in names:
        other = _SEASON_FOLDER_RE.match(name)
        previous = os.path.join(show, name)
        if other and int(other.group(1)) == wanted and os.path.isdir(previous):
            episodes = sorted(v.path for v in folder_videos(previous) if v.season is not None)
            return tuple(episodes[:MAX_PREVIOUS_SEASON_FILES])
    return ()


def silence_share(points: np.ndarray) -> float:
    """The share of fingerprint points the matcher would take for chromaprint's silence value (1.0 for no points).

    A point counts when its value is within ±2 of the silence value or differs from it in at most 6 bits: the matcher's
    own tests for two points being alike.
    """
    if len(points) == 0:
        return 1.0
    values = np.ascontiguousarray(points, dtype="<u4")
    near = np.abs(values.astype(np.int64) - SILENCE_POINT) <= _VALUE_SPREAD
    differing_bits = _POPCOUNT8[(values ^ np.uint32(SILENCE_POINT)).view(np.uint8)].reshape(-1, 4).sum(axis=1)
    return float(np.count_nonzero(near | (differing_bits <= MAX_BIT_DIFF))) / len(values)


def pair_value_matches(a: np.ndarray, b: np.ndarray) -> int:
    """How many point pairs the matcher's shift search expands: values of ``a`` and ``b`` within ±2 of each other.

    Args:
        a: The first episode's points.
        b: The second episode's points.

    Returns:
        The count, from sorted searches only (cheap however large it is).
    """
    if len(a) == 0 or len(b) == 0:
        return 0
    sorted_b = np.sort(b).astype(np.int64)
    a64 = a.astype(np.int64)
    upper = np.searchsorted(sorted_b, a64 + _VALUE_SPREAD, side="right")
    lower = np.searchsorted(sorted_b, a64 - _VALUE_SPREAD, side="left")
    return int((upper - lower).sum())


def _longest_stretch(outside: np.ndarray, width: int) -> int:
    """The most ``True`` values any ``width`` consecutive entries hold."""
    if len(outside) <= width:
        return int(np.count_nonzero(outside))
    totals = np.concatenate(([0], np.cumsum(outside, dtype=np.int64)))
    return int((totals[width:] - totals[:-width]).max())


def holds_no_intro(a: np.ndarray, b: np.ndarray) -> bool:
    """Whether the v3 matcher provably finds no run of 120 s or less between two openings (a pair it can skip).

    True for two openings of one constant value with at most short interruptions (a silent or muted file): matching
    them costs up to 1.2 s and can only find runs longer than any intro, which ``file_hits`` drops.

    Proof. Let v be the most common value of ``a``, K the points of ``a`` and ``b`` that aren't v, w the most non-v
    points any 28 consecutive points of ``a`` hold plus the same for ``b``, and m the shorter length. With w <= 27,
    every 28 aligned positions hold one where both are v (an exact match), so at any shift the matches have no gap of
    28 points, where the matcher breaks a run, and form one run from within the first 28 to within the last 28 aligned
    positions: at least the overlap minus 55 points. The 40 shifts closest to full overlap each overlap by at least
    m - 39 points and so count at least m - 39 - K value matches, while a shift overlapping by 1023 points or fewer
    counts at most 1023. With K <= m - 1063 the matcher's 40 best shifts all overlap by at least 1024 points, so every
    run it finds is at least 969 points (just over 120 s) long.

    Args:
        a: The first episode's points.
        b: The second episode's points.

    Returns:
        True only when the proof's conditions hold.
    """
    shorter = min(len(a), len(b))
    if shorter == 0:
        return False
    values, counts = np.unique(a, return_counts=True)
    common = values[np.argmax(counts)]
    outside_a, outside_b = a != common, b != common
    outside = int(np.count_nonzero(outside_a)) + int(np.count_nonzero(outside_b))
    if outside > shorter - (TOP_SHIFTS - 1) - _TOO_LONG_OVERLAP_PTS:
        return False
    return _longest_stretch(outside_a, _GAP_PTS) + _longest_stretch(outside_b, _GAP_PTS) < _GAP_PTS


def slow_to_match(a: np.ndarray, b: np.ndarray) -> bool:
    """Whether matching a pair costs too much for a checking thread (and :func:`holds_no_intro` can't skip it)."""
    return pair_value_matches(a, b) > MAX_INLINE_PAIR_MATCHES and not holds_no_intro(a, b)


def season_pair_runs(a: np.ndarray, b: np.ndarray) -> list[Run]:
    """The matcher's runs between two episodes, or none for a pair that provably holds no intro-length run."""
    return [] if holds_no_intro(a, b) else pair_runs(a, b)


def _mostly_silence(points: np.ndarray, segment: IntroSegment) -> bool:
    first = max(0, round(segment.start_s / POINT_S))
    last = min(len(points) - 1, round(segment.end_s / POINT_S))
    if last < first:
        return False
    return silence_share(points[first : last + 1]) > MAX_INTRO_SILENCE


def dense_core_s(target: str, candidate: IntroCandidate, points: Mapping[str, np.ndarray]) -> float:
    """How long a candidate's dense core is: per partner, the longest stretch of the candidate (its median start and
    end) where the two recordings, aligned as the partner's hit aligns them, differ in at most 6 bits with gaps of at
    most 4 points; the median over the partners (a partner hit more than once counts its longest).

    Args:
        target: The episode.
        candidate: One of its clusters.
        points: Fingerprint per file (``target``'s and its partners').

    Returns:
        Seconds; 0.0 when no partner overlaps the candidate.
    """
    a = np.ascontiguousarray(points[target], dtype="<u4")
    first, last = round(candidate.segment.start_s / POINT_S), round(candidate.segment.end_s / POINT_S)
    longest: dict[str, float] = {}
    for hit in candidate.members:
        b = np.ascontiguousarray(points[hit.partner], dtype="<u4")
        shift = round((hit.partner_start_s - hit.start_s) / POINT_S)
        lo, hi = max(first, -shift, 0), min(last, len(a), len(b) - shift)
        if hi <= lo:
            continue
        differing = _POPCOUNT8[(a[lo:hi] ^ b[lo + shift : hi + shift]).view(np.uint8)].reshape(-1, 4).sum(axis=1)
        alike = np.flatnonzero(differing <= MAX_BIT_DIFF)
        stretch = 0
        if len(alike):
            breaks = np.flatnonzero(np.diff(alike) > DENSE_CORE_GAP_PTS)
            starts = np.concatenate(([alike[0]], alike[breaks + 1]))
            ends = np.concatenate((alike[breaks], [alike[-1]]))
            stretch = int((ends - starts).max())
        longest[hit.partner] = max(longest.get(hit.partner, 0.0), stretch * POINT_S)
    return float(np.median(list(longest.values()))) if longest else 0.0


def needs_dense_core(segment: IntroSegment) -> bool:
    """Whether a stretch must have a dense core to be taken (``CORE_FREE_EARLY_MIN_S`` above).

    Args:
        segment: A cluster's segment, with its support.

    Returns:
        False for a stretch starting at 2-30 s that is at least 30 s long, and for one starting after 30 s that is at
        least 10 s long and found by at least 2 other episodes; True otherwise.
    """
    length_s = segment.end_s - segment.start_s
    if end_picture.is_early(segment.start_s):
        return segment.start_s < FILE_START_S or length_s < CORE_FREE_EARLY_MIN_S
    return length_s < CORE_FREE_LATER_MIN_S or segment.support < CORE_FREE_LATER_MIN_SUPPORT


def _passes_guards(
    target: str,
    candidate: IntroCandidate,
    points: Mapping[str, np.ndarray],
    end_picture_passes: Callable[[IntroCandidate], bool],
) -> bool:
    """Whether a candidate passes the guards against idents and music beds (``FILE_START_S`` above); the end picture,
    the only one that decodes, is asked last."""
    segment = candidate.segment
    if segment.start_s < FILE_START_S and segment.end_s - segment.start_s < MIN_FILE_START_LENGTH_S:
        return False
    if needs_dense_core(segment) and dense_core_s(target, candidate, points) < MIN_DENSE_CORE_S:
        return False
    return not end_picture.is_early(segment.start_s) or end_picture_passes(candidate)


def guarded_pick(
    target: str,
    files: Sequence[str],
    points: Mapping[str, np.ndarray],
    runs_between: Callable[[str, str], list[Run]],
    *,
    end_picture_passes: Callable[[IntroCandidate], bool],
) -> IntroSegment | None:
    """The best ranked of the v3 matcher's clusters for one episode that passes the guards (before the silence guard).

    The clusters are walked in the matcher's ranking order (:func:`matcher.intro_candidates`): the walk ends, with no
    intro, at the first cluster without the quorum; a quorum cluster that fails a guard (too short at the file's start,
    no dense core where one is needed, or an early one whose end picture differs) is passed over.

    Args:
        target: The episode (one of ``files``).
        files: The group, in matching order.
        points: Fingerprint per file (``target``'s and every other file's of ``files``).
        runs_between: Runs for ``(earlier, later)`` (:func:`season_pair_runs`, cached).
        end_picture_passes: The end-picture check of a cluster starting in the first 30 s (:class:`_EndPictures`).

    Returns:
        The cluster's segment with its support, or None.
    """
    others = len(files) - 1
    for candidate in intro_candidates(file_hits(target, files, runs_between)):
        if not meets_quorum(candidate.segment.support, others):
            return None
        if _passes_guards(target, candidate, points, end_picture_passes):
            return candidate.segment
    return None


def season_intro(
    target: str,
    files: Sequence[str],
    points: Mapping[str, np.ndarray],
    runs_between: Callable[[str, str], list[Run]],
    *,
    end_picture_passes: Callable[[IntroCandidate], bool],
) -> IntroSegment | None:
    """One episode's intro from its group: :func:`guarded_pick`, unless it is mostly silence.

    Args:
        target: The episode (one of ``files``).
        files: The group, in matching order.
        points: Fingerprint per file (``target``'s and every other file's of ``files``).
        runs_between: Runs for ``(earlier, later)`` (:func:`season_pair_runs`, cached).
        end_picture_passes: The end-picture check of a cluster starting in the first 30 s (:class:`_EndPictures`).

    Returns:
        The intro with its support, or None.
    """
    segment = guarded_pick(target, files, points, runs_between, end_picture_passes=end_picture_passes)
    if segment is not None and _mostly_silence(points[target], segment):
        return None
    return segment


@dataclass(frozen=True)
class SeasonClock:
    """The speed a season group is matched at, and the retime factor of each file that plays at another speed.

    A 25 fps release of a film-rate show plays 4.3 % fast, pitch raised with it, and its opening fingerprints like
    nothing a film-rate release of the same show plays (Bones season 5: 0 of 85 such pairs matched, against 85 of 85
    with the 25 fps audio slowed to film speed). A group whose files play at two speeds is matched at the speed most of
    them play at (``speed.match_speed``); each file at the other speed is matched on its audio retimed to it, and what
    it matches is read back at its own speed (``factors``: its own seconds per second of the group's).

    Attributes:
        speed: The group's speed, None when nothing is retimed.
        factors: Per retimed file, its :func:`speed.retime_factor`.
    """

    speed: float | None = None
    factors: Mapping[str, float] = field(default_factory=dict)

    def pair_version(self, first: str, second: str) -> int:
        """The version a pair's runs are cached under: :data:`SEASON_AUDIO_VERSION` for two files at their own speed,
        and another for each combination of the two sides' speeds (own, retimed to film, retimed to 25 fps)."""

        def side(path: str) -> int:
            return _RETIMED_TO[self.speed] if path in self.factors else 0

        return SEASON_AUDIO_VERSION + _PAIR_VERSION_STEP * (3 * side(first) + side(second))


def season_clock(speeds: Mapping[str, float | None]) -> SeasonClock:
    """The clock a season group is matched with.

    Args:
        speeds: Each file's :func:`speed.playback_speed` (None: unknown, matched as it plays).

    Returns:
        The group's speed and the files to retime; no speed and nothing to retime for a group at one speed.
    """
    group_speed = match_speed(speeds.values())
    factors = {path: factor for path, own in speeds.items() if (factor := retime_factor(own, group_speed)) is not None}
    return SeasonClock(group_speed, factors)


def in_own_time(segment: IntroSegment, factor: float | None) -> IntroSegment:
    """A segment matched at the group's speed in the file's own seconds (its retime factor; None: not retimed)."""
    if factor is None:
        return segment
    return IntroSegment(segment.start_s * factor, segment.end_s * factor, segment.support)


def in_own_times(candidate: IntroCandidate, target: str, factors: Mapping[str, float]) -> IntroCandidate:
    """A cluster matched at the group's speed in each file's own seconds, for the end-picture check.

    The target's stretch and hits are scaled by its factor. A partner's start is set so that its offset (its start
    minus the hit's) is exact at the stretch's end, where the pictures are compared: across the 3 s compared, a 4.3 %
    speed difference drifts 0.13 s, inside the check's half-second grid.

    Args:
        candidate: One of the target's clusters, at the group's speed.
        target: The episode.
        factors: :attr:`SeasonClock.factors`.

    Returns:
        The cluster in own seconds; the same cluster when neither the target nor a partner was retimed.
    """
    if target not in factors and not any(hit.partner in factors for hit in candidate.members):
        return candidate
    own = factors.get(target, 1.0)
    end_s = candidate.segment.end_s
    hits = []
    for hit in candidate.members:
        partner_end_s = (end_s + hit.partner_start_s - hit.start_s) * factors.get(hit.partner, 1.0)
        offset_s = partner_end_s - end_s * own
        hits.append(Hit(hit.start_s * own, hit.end_s * own, hit.partner, hit.start_s * own + offset_s))
    return IntroCandidate(in_own_time(candidate.segment, own), tuple(hits))


def _disk_identity(path: str) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


def _current_record(ctx: PipelineContext, path: str) -> FileRecord | None:
    rec = ctx.store.get_file(path)
    return rec if rec is not None and _disk_identity(path) == (rec.size, rec.mtime_ns) else None


def _changed_since_record(ctx: PipelineContext, path: str) -> bool:
    rec = ctx.store.get_file(path)
    identity = _disk_identity(path)
    return rec is not None and identity is not None and identity != (rec.size, rec.mtime_ns)


def _cached_points(ctx: PipelineContext, rec: FileRecord | None) -> np.ndarray | None:
    stored = cached_fingerprint(ctx.store, rec) if rec is not None else None
    return points_of(stored) if stored is not None else None


def _identity_of(rec: FileRecord) -> FileIdentity:
    return FileIdentity(rec.canonical_path, rec.size, rec.mtime_ns)


def _fingerprint_failed_lately(ctx: PipelineContext, member: FileRecord) -> bool:
    """Whether another episode's season step skips fingerprinting this member: ffmpeg failed on it as it is now less than
    a day ago (a forced re-detect tries anyway)."""
    if ctx.force:
        return False
    failed_at = ctx.store.member_fingerprint_failed_at(_identity_of(member))
    return failed_at is not None and ctx.now() - failed_at < UNREADABLE_MEMBER_RETRY


def _record_fingerprint_failure(ctx: PipelineContext, rec: FileRecord) -> None:
    now = ctx.now()
    ctx.store.record_member_fingerprint_failure(_identity_of(rec), now, forget_before=now - UNREADABLE_MEMBER_RETRY)


def _member_record(ctx: PipelineContext, path: str, *, with_frame_rate: bool = True) -> FileRecord | None:
    """A season member's record, read like its own run would read it.

    The store's record when it matches the disk and holds a duration, current chapters and (``with_frame_rate``, for
    season audio) a frame rate; otherwise the file is probed and its duration, chapters and frame rate are stored (its
    own run then reads nothing again). None for a file that changed since its record was made, or while it was probed
    (its own run reads it again), or can't be probed. A file that couldn't be probed isn't probed again for a day while
    its identity stays the same, except by a forced re-detect; one stored before frame rates were read, with current
    chapters, stays in meanwhile at an unknown speed. The season step holds only its own file's path lock, so the store
    refuses to change another file's identity (``record_member``).

    Raises:
        ProbeStalledError: Earlier ffprobes are still stuck on their files, so this one wasn't read (and nothing is
            recorded against it).
    """
    identity = _disk_identity(path)
    rec = ctx.store.get_file(path)
    if identity is None or (rec is not None and identity != (rec.size, rec.mtime_ns)):
        return None
    read = (
        rec
        if rec is not None
        and rec.duration_ms
        and ctx.store.evidence_version(rec.id, Source.CHAPTERS) == CHAPTER_RULES_VERSION
        else None
    )
    if read is not None and (not with_frame_rate or ctx.store.get_frame_rate(read.id)[0]):
        return read
    probed_as = FileIdentity(path, *identity)
    if _probe_failed_lately(ctx, probed_as):
        return read
    try:
        probe = probe_media(path, ffprobe=ctx.ffprobe)
    except ProbeStalledError:
        raise
    except ProbeError as exc:
        logger.debug("The season step skips {}: {}", os.path.basename(path), exc)
        probe = None
    if _disk_identity(path) != identity:
        return None
    if probe is None or not probe.duration_ms:
        now = ctx.now()
        ctx.store.record_member_probe_failure(probed_as, now, forget_before=now - UNREADABLE_MEMBER_RETRY)
        return read
    return ctx.store.record_member(
        probed_as,
        duration_ms=probe.duration_ms,
        season_key=os.path.dirname(path),
        # Each member's own path decides the episode-only chapter names, exactly as its own run would:
        # a season group can hold a file whose name carries no SxxEyy, and it must not be read as one.
        chapters=chapter_candidates(probe, is_episode=ids_from_path(path).is_episode),
        chapter_version=CHAPTER_RULES_VERSION,
        frame_rate=probe.frame_rate,
    )


def _probe_failed_lately(ctx: PipelineContext, identity: FileIdentity) -> bool:
    """Whether ffprobe failed on a member with this identity less than a day ago (a forced re-detect tries anyway)."""
    failed_at = None if ctx.force else ctx.store.member_probe_failed_at(identity)
    return failed_at is not None and ctx.now() - failed_at < UNREADABLE_MEMBER_RETRY


def _member_record_if_readable(ctx: PipelineContext, path: str) -> FileRecord | None:
    """:func:`_member_record` for the chapter step (no frame rate needed), with a member ffprobe can't be started for
    now counting as unread (its own run reads it)."""
    try:
        return _member_record(ctx, path, with_frame_rate=False)
    except ProbeStalledError as exc:
        logger.debug("The season step leaves out {} for now: {}", os.path.basename(path), exc)
        return None


def _intro_chapter_ms(ctx: PipelineContext, rec: FileRecord | None) -> int | None:
    return intro_chapter_length_ms(ctx.store.get_evidence(rec.id), rec.duration_ms) if rec is not None else None


def season_intro_chapter_limits(ctx: PipelineContext, canonical_path: str) -> tuple[int | None, dict[str, int | None]]:
    """The season step for chapters (finding F1): the intro-chapter limit for this episode and for each of its siblings.

    Members of this episode's group the store never saw, or saw with older chapter rules, are probed and stored, so the
    first episode checked sees its whole group whichever it is. A member changed on disk since its record counts as
    having no intro chapter until its own run reads it again. A sibling's own group can reach past this episode's in a
    flat folder; those extra members are read from the store only. In a flat folder this episode can also be in the
    group of a file outside its own group (the 40 nearest aren't symmetric), so the siblings compared also include
    those files (:func:`groups_holding`).

    Args:
        ctx: The job's context.
        canonical_path: Local path of one episode.

    Returns:
        This episode's limit, and the limit each sibling would be decided with now (None: none applies, because the
        episode has no intro chapter or fewer than two others have one).
    """
    view = _season_view(ctx, canonical_path)
    group = view.group()
    siblings = [path for path in group.episodes if path != canonical_path]
    siblings += [path for path in groups_holding(canonical_path, view.videos, view.group) if path not in group.episodes]
    groups = {path: view.group(path).episodes for path in siblings}
    lengths = {path: _intro_chapter_ms(ctx, _member_record_if_readable(ctx, path)) for path in group.episodes}

    def length(path: str) -> int | None:
        if path not in lengths:
            rec = _current_record(ctx, path)
            fresh = rec is not None and ctx.store.evidence_version(rec.id, Source.CHAPTERS) == CHAPTER_RULES_VERSION
            lengths[path] = _intro_chapter_ms(ctx, rec) if fresh and rec.duration_ms else None
        return lengths[path]

    def limit(path: str, episodes: Sequence[str]) -> int | None:
        if length(path) is None:
            return None
        others = [length(other) for other in episodes if other != path]
        return intro_chapter_limit_ms([ms for ms in others if ms is not None])

    return limit(canonical_path, group.episodes), {path: limit(path, episodes) for path, episodes in groups.items()}


def _signature_paths(canonical_path: str, group: SeasonGroup) -> tuple[str, ...]:
    if len(group.episodes) == 1:
        return group.episodes + previous_season_files(canonical_path)
    return group.episodes


def _signature_item(ctx: PipelineContext, path: str) -> list:
    identity = _disk_identity(path)
    rec = ctx.store.get_file(path) if identity is not None else None
    current = rec is not None and (rec.size, rec.mtime_ns) == identity
    size, mtime_ns = identity or (None, None)
    return [path, size, mtime_ns, *(_fingerprints_item(ctx, rec) if current else [False, None, None])]


def _fingerprints_item(ctx: PipelineContext, rec: FileRecord, rates: Mapping[str, float | None] | None = None) -> list:
    """What a file's part of a signature says about its fingerprints: whether it has its own, its frame rate (the one
    it was matched at, given ``rates``; else the stored one), and whether it has the one retimed to its other speed
    (None at an unknown or other rate), so a retimed fingerprint made after an answer left the file out makes that
    answer due."""
    if rates is not None and rec.canonical_path in rates:
        rate = rates[rec.canonical_path]
    else:
        rate = ctx.store.get_frame_rate(rec.id)[1]
    own = playback_speed(rate)
    retimed = None
    if own is not None:
        other = FILM_FPS if own == PAL_FPS else PAL_FPS
        retimed = has_cached_fingerprint(ctx.store, rec, retime_factor(own, other))
    return [has_cached_fingerprint(ctx.store, rec), rate, retimed]


def _signature(
    ctx: PipelineContext,
    paths: tuple[str, ...],
    matched: Mapping[str, FileRecord] | None = None,
    on_disk: dict[str, list] | None = None,
    rates: Mapping[str, float | None] | None = None,
) -> str:
    """What an answer is based on: each file's identity, whether it has its fingerprint, its frame rate (a rate read
    later can make the file one to retime) and whether it has its retimed fingerprint (:func:`_fingerprints_item`).

    ``matched`` files (a detector run's) enter with the identity they were matched with, and with the frame rate they
    were matched at (``rates``, :class:`_Matching`), so a sibling replaced, or whose rate was read, while the season was
    matched makes the answer due again; every other file enters as it is on disk now (read once per path into
    ``on_disk`` when a caller builds several signatures).
    """
    on_disk = {} if on_disk is None else on_disk
    items = []
    for path in paths:
        if matched and path in matched:
            rec = matched[path]
            items.append([path, rec.size, rec.mtime_ns, *_fingerprints_item(ctx, rec, rates)])
            continue
        if path not in on_disk:
            on_disk[path] = _signature_item(ctx, path)
        items.append(on_disk[path])
    payload = json.dumps([SEASON_AUDIO_VERSION, items])
    return hashlib.sha1(payload.encode(), usedforsecurity=False).hexdigest()


class _SeasonView:
    """What one run of an episode reads of its season: the folder listing, groups, previous-season files, and each
    file's signature item and signature. Kept for the run in ``ctx.run_memo`` (the pipeline drops it once a detector
    ran), so the chapter step, the follow-ups hook, the early-stop and pending checks (``season_audio_due``) and
    ``needs_worker`` read the season once."""

    def __init__(self, ctx: PipelineContext, canonical_path: str) -> None:
        self._ctx = ctx
        self.canonical_path = canonical_path
        self.videos = folder_videos(os.path.dirname(canonical_path))
        self.on_disk: dict[str, list] = {}
        self._groups: dict[str, SeasonGroup] = {}
        self._signatures: dict[str, str] = {}
        self._previous: tuple[str, ...] | None = None

    def group(self, path: str | None = None) -> SeasonGroup:
        """The season group of this run's episode, or of another file of its folder."""
        path = path or self.canonical_path
        if path not in self._groups:
            self._groups[path] = season_group(path, self.videos)
        return self._groups[path]

    def previous_season(self) -> tuple[str, ...]:
        """This run's episode's :func:`previous_season_files`."""
        if self._previous is None:
            self._previous = previous_season_files(self.canonical_path)
        return self._previous

    def signature(self, path: str) -> str:
        """What an answer for ``path`` made from the files on disk now would be based on."""
        if path not in self._signatures:
            group = self.group(path)
            if path == self.canonical_path and len(group.episodes) == 1:
                paths = group.episodes + self.previous_season()
            else:
                paths = _signature_paths(path, group)
            self._signatures[path] = _signature(self._ctx, paths, on_disk=self.on_disk)
        return self._signatures[path]


def _season_view(ctx: PipelineContext, canonical_path: str) -> _SeasonView:
    memo = ctx.run_memo(canonical_path)
    if "season" not in memo:
        memo["season"] = _SeasonView(ctx, canonical_path)
    return memo["season"]


def season_audio_due(rec: FileRecord, ctx: PipelineContext) -> bool:
    """Whether the season's files or their fingerprints changed since this episode's answer (asked once per run).

    Args:
        rec: The episode.
        ctx: The job's context.

    Returns:
        True when the detector should run again.
    """
    current = _season_view(ctx, rec.canonical_path).signature(rec.canonical_path)
    return ctx.store.get_detector_run(rec.id, Source.SEASON_AUDIO) != current


def season_audio_needs_worker(rec: FileRecord, ctx: PipelineContext) -> bool:
    """Whether this episode's season audio has to run on a worker.

    It does while an episode of the folder (this one included) still needs ffmpeg or ffprobe (a sibling's frame rate
    included; this episode's own is read here, inline like its own probe, when it was stored before frame rates were
    read), while a file that plays at another speed than the group has no retimed fingerprint yet
    (:class:`SeasonClock`), while a pair this episode hasn't been matched with yet is too slow for a checking thread
    (:func:`slow_to_match`: long constant stretches in both openings that don't provably rule out an intro), or while
    the season step would meet a cluster starting in the first 30 s whose end picture isn't checked yet (decoding is a
    worker's job). That last question is the season step itself, run here with the end-picture check reading only
    markers.db: the pairs it matches are cached for the detector, whichever thread it then runs on. Otherwise matching
    is cheap enough to run inline. A sibling ffmpeg or ffprobe failed on lately doesn't count: the step leaves it out,
    or keeps it at an unknown speed.

    Args:
        rec: The episode.
        ctx: The job's context.

    Returns:
        True when the detector has to run on a worker.
    """
    view = _season_view(ctx, rec.canonical_path)
    group = view.group()
    fingerprinted: dict[str, tuple[FileRecord, np.ndarray]] = {}
    for path in group.episodes:
        stored = ctx.store.get_file(path)
        if stored is None:
            return True
        if _disk_identity(path) != (stored.size, stored.mtime_ns):
            continue
        cached = _cached_points(ctx, stored)
        if cached is None:
            if path != rec.canonical_path and _fingerprint_failed_lately(ctx, stored):
                continue
            return True
        if path != rec.canonical_path and _frame_rate_unread(ctx, stored):
            return True
        fingerprinted[path] = (stored, cached)
    if rec.canonical_path not in fingerprinted:
        return False
    # Read here, inline as the file's own probe is, so the speed matched at is the one the detector will match at. A
    # rate left unread (ffprobes stuck on earlier files) is read on a worker, not guessed at here.
    frame_rate_of(ctx, rec)
    if _frame_rate_unread(ctx, rec):
        return True
    records = {path: member for path, (member, _) in fingerprinted.items()}
    points = {path: cached for path, (_, cached) in fingerprinted.items()}
    try:
        matching = _matching(
            ctx,
            rec,
            records,
            points,
            group_size=len(group.episodes),
            previous_files=view.previous_season(),
            retimed=functools.partial(_cached_retimed, ctx, rec),
        )
    except _RetimedUnmadeError:
        return True
    if matching is None:
        return False
    target = rec.canonical_path
    pairs = [(path, target) if path < target and not matching.previous else (target, path) for path in matching.files]
    if any(
        ctx.store.get_season_pair(records[first].id, records[second].id, matching.clock.pair_version(first, second))
        is None
        and slow_to_match(matching.points[first], matching.points[second])
        for first, second in pairs
        if first != second
    ):
        return True
    try:
        _intro(ctx, target, matching, records, _EndPictures(ctx, rec, records, decode=False))
    except _EndPictureUncheckedError:
        return True
    except end_picture.CheckUnavailableError:
        return False  # this episode's own file couldn't be read lately: the detector gives up without decoding
    return False


def frame_rate_of(
    ctx: PipelineContext, rec: FileRecord, *, probe: Callable[..., MediaProbe] | None = None
) -> float | None:
    """A file's video frame rate: the stored one, or, for a file stored before frame rates were read, one read now.

    A read that fails is remembered with the file's identity (the season members' probe failures) and not tried again
    for a day, except by a forced re-detect; ffprobes stuck on earlier files leave it unread, blaming nothing. A rate
    stored for the first time is noted as a changed answer: the season's other episodes may now match this file at
    another speed.

    Args:
        ctx: The job's context.
        rec: The file (its identity matches the disk: callers checked).
        probe: The ffprobe reader (the caller's module's ``probe_media``); :func:`probe_media` by default.

    Returns:
        The rate, or None when the file has none or it couldn't be read.
    """
    stored, rate = ctx.store.get_frame_rate(rec.id)
    if stored:
        return rate
    identity = _identity_of(rec)
    if _probe_failed_lately(ctx, identity):
        return None
    try:
        probed = (probe if probe is not None else probe_media)(rec.canonical_path, ffprobe=ctx.ffprobe)
    except ProbeStalledError as exc:
        logger.debug("Frame rate of {} unread for now: {}", os.path.basename(rec.canonical_path), exc)
        return None
    except ProbeError as exc:
        logger.debug("Frame rate of {} unknown: {}", os.path.basename(rec.canonical_path), exc)
        now = ctx.now()
        ctx.store.record_member_probe_failure(identity, now, forget_before=now - UNREADABLE_MEMBER_RETRY)
        return None
    if ctx.store.set_frame_rate(rec.id, probed.frame_rate, identity=(rec.size, rec.mtime_ns)):
        ctx.note_answer_changed()
    return probed.frame_rate


def _frame_rate_unread(ctx: PipelineContext, member: FileRecord) -> bool:
    """Whether a sibling's frame rate still has to be read (``_member_record`` probes it), unless ffprobe failed on it
    lately."""
    return not ctx.store.get_frame_rate(member.id)[0] and not _probe_failed_lately(ctx, _identity_of(member))


class _RetimedUnmadeError(Exception):
    """A file of the group needs a retimed fingerprint that isn't made yet (``needs_worker``: ffmpeg is a worker's
    job)."""


def _cached_retimed(ctx: PipelineContext, rec: FileRecord, member: FileRecord, factor: float) -> np.ndarray | None:
    """A group file's cached retimed fingerprint for ``needs_worker``: None for a sibling ffmpeg failed on lately (the
    step leaves it out).

    Raises:
        _RetimedUnmadeError: It isn't made yet.
    """
    stored = cached_fingerprint(ctx.store, member, factor)
    if stored is not None:
        return points_of(stored)
    if member.id != rec.id and _fingerprint_failed_lately(ctx, member):
        return None
    raise _RetimedUnmadeError(os.path.basename(member.canonical_path))


@dataclass(frozen=True)
class _Matching:
    """What one episode's season step matches: its files in matching order, their points at the group's speed, the
    clock, the source an answer is stored as, the previous season's files it was matched with (a lone episode), and
    the frame rate each audible file was matched at (the answer's signature names these, not rates read later)."""

    files: list[str]
    points: dict[str, np.ndarray]
    clock: SeasonClock
    source: Source
    previous: tuple[str, ...] = ()
    rates: dict[str, float | None] = field(default_factory=dict)


def _matching(
    ctx: PipelineContext,
    rec: FileRecord,
    records: dict[str, FileRecord],
    points: Mapping[str, np.ndarray],
    *,
    group_size: int,
    previous_files: Sequence[str] | None,
    retimed: Callable[[FileRecord, float], np.ndarray | None],
) -> _Matching | None:
    """The files this episode is matched with, at one speed: its group's audible files, or, alone in its folder, the
    previous season's cached ones (their records are added to ``records``).

    Every file that plays at another speed than most of them (:func:`season_clock`) is matched on its retimed
    fingerprint (``retimed``: None leaves a sibling out, which only a file other than this episode may be).

    Args:
        ctx: The job's context.
        rec: The episode.
        records: Record per path of the group's fingerprinted files.
        points: Their own-speed fingerprints.
        group_size: How many episodes the group has.
        previous_files: The previous season's files (None: listed here).
        retimed: A file's fingerprint retimed by a factor.

    Returns:
        The matching, or None when there is nothing to match (this episode is silent, or has no audible partner).
    """
    target = rec.canonical_path
    if not len(points[target]):
        return None
    audible = {path: found for path, found in points.items() if len(found)}
    previous: dict[str, np.ndarray] = {}
    source = Source.SEASON_AUDIO
    if len(audible) < 2:
        if group_size != 1:
            return None
        previous = _previous_season_points(ctx, target, records, previous_files)
        if not previous:
            return None
        audible, source = {target: points[target], **previous}, Source.SEASON_AUDIO_PREVIOUS
    rates = {path: ctx.store.get_frame_rate(records[path].id)[1] for path in audible}
    clock = season_clock({path: playback_speed(rate) for path, rate in rates.items()})
    matched = dict(audible)
    for path, factor in clock.factors.items():
        found = retimed(records[path], factor)
        if found is None or not len(found):
            del matched[path]
        else:
            matched[path] = found
    if target not in matched or len(matched) < 2:
        return None
    # A lone episode goes first, then the previous season's files by path: the order few_siblings.py measured.
    files = [target, *sorted(path for path in previous if path in matched)] if previous else sorted(matched)
    return _Matching(files, matched, clock, source, tuple(previous), rates)


class _EndPictureUncheckedError(Exception):
    """A cluster's end picture isn't checked yet and the check was asked not to decode (``needs_worker``)."""


class _EndPictures:
    """The end-picture check (:mod:`end_picture`) of one episode's clusters, each partner's share cached in markers.db
    per file pair and stretch.

    With ``decode`` off (``season_audio_needs_worker``), a share not cached yet raises
    :class:`_EndPictureUncheckedError` instead: decoding is a worker's job. A forced re-detect reads no cached share and
    no remembered failure, so every share is measured again.

    A file ffprobe or ffmpeg couldn't read (``end_picture.ReadFailedError``) is remembered with its identity for a day
    (``END_PICTURE_RETRY``) and not read for the check meanwhile, by this episode's run or any sibling's. It is never a
    pass: this episode's own file makes the check unavailable (no season audio answer this time), and a partner has no
    share, so the other partner decides; with no share at all the check is unavailable too. Only a share, or "certainly
    no frames" (None), is cached. A cancel or stalled ffprobes raise ``end_picture.CheckUnavailableError`` with nothing
    stored.
    """

    def __init__(
        self,
        ctx: PipelineContext,
        target: FileRecord,
        records: Mapping[str, FileRecord],
        *,
        decode: bool,
        gpu: str | None = None,
        gpu_device_path: str | None = None,
        cancel_check: Callable[[], bool] | None = None,
        phase: Callable[[str], None] | None = None,
    ) -> None:
        self._ctx, self._target, self._records, self._decode = ctx, target, records, decode
        self._phase = phase or (lambda _text: None)
        self._reader = end_picture.Reader(
            ffmpeg=getattr(ctx.config, "ffmpeg_path", None) or "ffmpeg",
            gpu=gpu,
            gpu_device_path=gpu_device_path,
            cancel_check=cancel_check,
        )
        self._shares: dict[EndPictureKey, float | None] = {}

    def __call__(self, candidate: IntroCandidate) -> bool:
        segment = candidate.segment
        shares: list[float | None] = []
        unread = False
        for hit in end_picture.partners(candidate.members):
            partner = self._records[hit.partner]
            offset_s = hit.partner_start_s - hit.start_s
            key = EndPictureKey(
                self._target.id,
                partner.id,
                round(segment.start_s * 1000),
                round(segment.end_s * 1000),
                round(offset_s * 1000),
            )
            if key not in self._shares:
                cached = None if self._ctx.force else self._ctx.store.get_end_picture(key, end_picture.CHECK_VERSION)
                if cached is not None:
                    self._shares[key] = cached.share
                else:
                    if self._failed_lately(self._target):
                        raise end_picture.CheckUnavailableError(
                            f"{os.path.basename(self._target.canonical_path)} couldn't be read for its end picture "
                            "lately"
                        )
                    if self._failed_lately(partner):
                        unread = True
                        continue
                    if not self._decode:
                        raise _EndPictureUncheckedError(os.path.basename(self._target.canonical_path))
                    try:
                        self._shares[key] = self._measure(key, segment, partner, offset_s)
                    except end_picture.ReadFailedError as exc:
                        failed = self._target if exc.path == self._target.canonical_path else partner
                        self._record_failure(failed)
                        if failed is self._target:
                            raise end_picture.CheckUnavailableError(str(exc)) from exc
                        unread = True
                        continue
            shares.append(self._shares[key])
        if unread and all(share is None for share in shares):
            raise end_picture.CheckUnavailableError(
                f"the end picture of {os.path.basename(self._target.canonical_path)} has no partner to compare with: "
                "its partners couldn't be read lately"
            )
        return end_picture.passes(shares)

    def _failed_lately(self, rec: FileRecord) -> bool:
        if self._ctx.force:
            return False
        failed_at = self._ctx.store.end_picture_failed_at(_identity_of(rec))
        return failed_at is not None and self._ctx.now() - failed_at < END_PICTURE_RETRY

    def _record_failure(self, rec: FileRecord) -> None:
        now = self._ctx.now()
        self._ctx.store.record_end_picture_failure(_identity_of(rec), now, forget_before=now - END_PICTURE_RETRY)

    def _measure(self, key: EndPictureKey, segment: IntroSegment, partner: FileRecord, offset_s: float) -> float | None:
        self._phase("Comparing the intro's end picture…")
        share = self._reader.share(
            self._target.canonical_path, partner.canonical_path, segment.start_s, segment.end_s, offset_s
        )
        self._ctx.store.set_end_picture(
            key,
            end_picture.CHECK_VERSION,
            share,
            identity_a=(self._target.size, self._target.mtime_ns),
            identity_b=(partner.size, partner.mtime_ns),
        )
        return share


def _intro(
    ctx: PipelineContext,
    target: str,
    matching: _Matching,
    records: dict[str, FileRecord],
    end_pictures: _EndPictures,
) -> IntroSegment | None:
    """:func:`season_intro` on the matching's points, pairs cached in markers.db; the answer, and each end picture
    checked, in the files' own seconds."""
    points, clock = matching.points, matching.clock

    def runs_between(first: str, second: str) -> list[Run]:
        a, b = records[first], records[second]
        version = clock.pair_version(first, second)
        cached = ctx.store.get_season_pair(a.id, b.id, version)
        if cached is not None:
            return [Run(*run) for run in cached]
        runs = season_pair_runs(points[first], points[second])
        ctx.store.set_season_pair(
            a.id,
            b.id,
            version,
            [tuple(run) for run in runs],
            identity_a=(a.size, a.mtime_ns),
            identity_b=(b.size, b.mtime_ns),
        )
        return runs

    segment = season_intro(
        target,
        matching.files,
        points,
        runs_between,
        end_picture_passes=lambda candidate: end_pictures(in_own_times(candidate, target, clock.factors)),
    )
    return None if segment is None else in_own_time(segment, clock.factors.get(target))


def _previous_season_points(
    ctx: PipelineContext,
    canonical_path: str,
    records: dict[str, FileRecord],
    previous_files: Sequence[str] | None = None,
) -> dict[str, np.ndarray]:
    """The previous season's files a lone episode is matched with: current records with a cached fingerprint that
    isn't empty (each record is added to ``records``)."""
    previous: dict[str, np.ndarray] = {}
    for path in previous_season_files(canonical_path) if previous_files is None else previous_files:
        member = _current_record(ctx, path)
        cached = _cached_points(ctx, member)
        if member is not None and cached is not None and len(cached):
            records[path], previous[path] = member, cached
    return previous


def _candidate(segment: IntroSegment, others: int, source: Source) -> Candidate:
    return Candidate(
        MarkerType.INTRO,
        int(round(segment.start_s * 1000)),
        int(round(segment.end_s * 1000)),
        source,
        confidence=segment.support / others,
        origin=f"{segment.support}/{others}",
    )


def intro_rests_on_season_audio(ctx: PipelineContext, rec: FileRecord) -> bool:
    """Whether a file's published intro was decided with its season audio answer or previous-season hint.

    Args:
        ctx: The job's context.
        rec: The file.

    Returns:
        True for such a marker (a user's lock never counts).
    """
    marker = ctx.store.get_markers(rec.id).get(MarkerType.INTRO)
    return marker is not None and not marker.locked and not _SEASON_AUDIO_SOURCES.isdisjoint(marker.decided_by)


def _intro_settled(ctx: PipelineContext, rec: FileRecord) -> bool:
    """Whether a new season audio answer can't change a file's intro: decided by other sources, or left to every
    server's own marker (the kept status, ``outcomes.is_kept_own``: season audio never runs for it, so its stored
    answer never catches up, and its own run checks the servers again)."""
    intro = ctx.store.get_decisions(rec.id).get(MarkerType.INTRO)
    if intro is None:
        return False
    if is_kept_own(intro.status, intro.reason):
        return True
    return intro.status is DecisionStatus.DECIDED and not intro_rests_on_season_audio(ctx, rec)


def _request_redecide(
    ctx: PipelineContext,
    rec: FileRecord,
    members: dict[str, FileRecord],
    signature: str,
    *,
    matched: Mapping[str, FileRecord],
) -> None:
    """Ask again for the siblings whose intro is undecided, or decided with a season audio answer, and whose answer
    was based on other season files than this run's. A sibling season audio never answered for is left alone: its own
    run matches the whole group, and one no server takes for Intro & Credits never runs.

    ``signature`` is this run's answer's; a sibling whose own group is another (a season of more than 40 episodes gives
    each file its 40 nearest) is compared with the signature its own group has with the files this run ``matched``,
    since its answer never equals this run's even when nothing it used changed.
    """
    folder = os.path.dirname(rec.canonical_path)
    videos: tuple[FolderVideo, ...] | None = None
    own_group: SeasonGroup | None = None
    on_disk: dict[str, list] = {}
    stale = []
    for path, member in members.items():
        if member.id == rec.id or os.path.dirname(path) != folder:
            continue
        answer = ctx.store.get_detector_run(member.id, Source.SEASON_AUDIO)
        if answer is None or answer == signature:
            continue
        if _intro_settled(ctx, member):
            continue
        if videos is None:
            videos = folder_videos(folder)
            own_group = season_group(rec.canonical_path, videos)
        group = season_group(path, videos)
        if group.episodes != own_group.episodes and answer == _signature(
            ctx, _signature_paths(path, group), matched, on_disk
        ):
            continue
        stale.append(path)
    if stale:
        ctx.request_followups(stale)


def season_audio_followups(rec: FileRecord, ctx: PipelineContext) -> list[str]:
    """The siblings whose season audio answer is out of date and whose intro could change with it.

    State, not events: every run of an episode compares each sibling's answer with the season files on disk now, so an
    episode that arrives already decided (by its chapters, say) and never runs season audio still reaches a sibling
    decided on a 1/1 match made before it arrived. A sibling counts when it has an answer, its record matches the disk
    (otherwise its own run reads it again), its intro is undecided or was decided with that answer, and its own last
    attempt didn't already fail on the season as it is now (so a file that can't be fingerprinted is asked again once
    per change of its season, not on every sibling's run). In a flat folder the siblings also include the files whose
    own group holds this episode though this episode's group doesn't hold them (:func:`groups_holding`).

    Args:
        rec: The episode being run.
        ctx: The job's context.

    Returns:
        The siblings' paths, sorted.
    """
    view = _season_view(ctx, rec.canonical_path)
    group = view.group()
    siblings = [*group.episodes]
    siblings += [
        path for path in groups_holding(rec.canonical_path, view.videos, view.group) if path not in group.episodes
    ]
    stale = []
    for path in siblings:
        member = ctx.store.get_file(path) if path != rec.canonical_path else None
        answer = ctx.store.get_detector_run(member.id, Source.SEASON_AUDIO) if member is not None else None
        if answer is None:
            continue
        if _intro_settled(ctx, member):
            continue
        if path not in view.on_disk:
            view.on_disk[path] = _signature_item(ctx, path)
        if view.on_disk[path][1:3] != [member.size, member.mtime_ns]:
            continue
        signature = view.signature(path)
        if answer != signature and ctx.store.get_detector_failure(member.id, Source.SEASON_AUDIO) != signature:
            stale.append(path)
    return sorted(stale)


def detect_season_audio(
    rec: FileRecord,
    *,
    ctx: PipelineContext,
    gpu: str | None = None,
    gpu_device_path: str | None = None,
    phase_callback: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    pause_check: Callable[[], bool] | None = None,
) -> DetectorAnswer:
    """Match this episode's opening against its season (or, alone, the previous season's cached episodes).

    A sibling ffmpeg can't fingerprint is remembered, and other episodes' steps leave it out for a day while it stays
    unchanged; this episode's own fingerprint is always tried. A cluster starting in the first 30 s whose end picture
    isn't checked yet is decoded here (:class:`_EndPictures`).

    Args:
        rec: The episode (its identity matches the disk: the pipeline just checked).
        ctx: The job's context.
        gpu: The worker's GPU type for the end-picture decode (chromaprint is CPU only), None on the CPU.
        gpu_device_path: The worker's device.
        phase_callback: Worker row step text.
        cancel_check: True once the job is cancelled.
        pause_check: Unused (a paused job doesn't block a worker).

    Returns:
        At most one intro candidate (season audio, or the previous-season hint), with the signature of the season files
        it was matched with (the pipeline stores both together).

    Raises:
        DetectorUnavailableError: No chromaprint ffmpeg, this episode couldn't be fingerprinted, earlier fingerprint
            ffmpegs or ffprobes are still stuck on their files (the season stops there), an end-picture read stalled or
            timed out, or cancelled.
    """
    from ..pipeline import DetectorAnswer, DetectorUnavailableError

    phase = phase_callback or (lambda _text: None)
    ffmpeg = chromaprint_ffmpeg(getattr(ctx.config, "ffmpeg_path", None))
    if ffmpeg is None:
        raise DetectorUnavailableError("no ffmpeg with chromaprint")
    group = season_group(rec.canonical_path)

    def fingerprint_of(member: FileRecord, retime: float | None = None) -> np.ndarray | None:
        """A group file's fingerprint (retimed by ``retime``), computed when missing; None leaves a sibling out."""
        own_file = member.id == rec.id
        try:
            # A failure is recorded while the file's lock is held, so siblings' steps waiting on it skip it (for a day).
            found = ensure_fingerprint(
                ctx.store,
                member,
                ffmpeg=ffmpeg,
                cancel_check=cancel_check,
                skip=None if own_file else functools.partial(_fingerprint_failed_lately, ctx, member),
                on_failure=functools.partial(_record_fingerprint_failure, ctx, member),
                retime=retime,
            )
        except FingerprintSkippedError:
            logger.debug(
                "Season audio leaves out {}: it couldn't be fingerprinted lately",
                os.path.basename(member.canonical_path),
            )
            return None
        except FingerprintStalledError as exc:
            # The mount's fault, not this file's: nothing recorded. Every later sibling would meet the same stalled
            # mount: no answer this run rather than one matched against whichever siblings were fingerprinted already.
            raise DetectorUnavailableError(str(exc)) from exc
        except FingerprintError as exc:
            if not own_file:
                logger.info("Season audio leaves out {} this time: {}", os.path.basename(member.canonical_path), exc)
                return None
            if not (cancel_check and cancel_check()):
                # Siblings' runs don't ask for this file again until its season changes (season_audio_followups).
                attempted = _signature(ctx, _signature_paths(rec.canonical_path, group))
                ctx.store.set_detector_failure(rec.id, Source.SEASON_AUDIO, attempted)
            raise DetectorUnavailableError(str(exc)) from exc
        if found is None and own_file:
            raise DetectorUnavailableError("the file changed while it was fingerprinted")
        return found

    left_out: set[str] = set()

    def retimed(member: FileRecord, factor: float) -> np.ndarray | None:
        if cancel_check and cancel_check():
            raise DetectorUnavailableError("cancelled")
        phase("Fingerprinting audio…")  # the worker row's existing words: a retimed file is fingerprinted once more
        found = fingerprint_of(member, factor)
        if found is None:
            left_out.add(member.canonical_path)
        return found

    phase("Fingerprinting audio…")
    own = fingerprint_of(rec)
    frame_rate_of(ctx, rec)
    records: dict[str, FileRecord] = {rec.canonical_path: rec}
    points: dict[str, np.ndarray] = {rec.canonical_path: own}
    others = [p for p in group.episodes if p != rec.canonical_path]
    left_out_changed = False
    for n, path in enumerate(others, 1):
        if cancel_check and cancel_check():
            raise DetectorUnavailableError("cancelled")
        try:
            member = _member_record(ctx, path)
        except ProbeStalledError as exc:
            raise DetectorUnavailableError(str(exc)) from exc  # every later sibling would meet the same stall
        if member is None:
            left_out_changed = left_out_changed or _changed_since_record(ctx, path)
            continue
        cached = _cached_points(ctx, member)
        if cached is None:
            phase(f"Fingerprinting season audio {n}/{len(others)}…")
            cached = fingerprint_of(member)
        if cached is not None:
            records[path], points[path] = member, cached

    matching = _matching(
        ctx, rec, records, points, group_size=len(group.episodes), previous_files=None, retimed=retimed
    )
    if cancel_check and cancel_check():
        # A sibling's fingerprint cancelled part way is left out like a failed one: no answer without it.
        raise DetectorUnavailableError("cancelled")
    phase("Matching season audio…")
    candidates: list[Candidate] = []
    end_pictures = _EndPictures(
        ctx,
        rec,
        records,
        decode=True,
        gpu=gpu,
        gpu_device_path=gpu_device_path,
        cancel_check=cancel_check,
        phase=phase,
    )
    try:
        segment = None if matching is None else _intro(ctx, rec.canonical_path, matching, records, end_pictures)
    except end_picture.CheckUnavailableError as exc:
        raise DetectorUnavailableError(str(exc)) from exc
    if segment is not None:
        candidates.append(_candidate(segment, len(matching.files) - 1, matching.source))

    # Every file read for the match (the previous season's included, which _matching adds to records) but one left out
    # for want of its retimed fingerprint: that one enters the signature as it is, so its fingerprint made later makes
    # this answer due.
    matched = {path: member for path, member in records.items() if path not in left_out}
    rates = matching.rates if matching is not None else None
    signature = _signature(ctx, _signature_paths(rec.canonical_path, group), matched, rates=rates)
    _request_redecide(ctx, rec, records, signature, matched=matched)
    if left_out_changed:
        # The sibling's own run in this job reads it again, and its request for this file would be dropped as one of
        # the job's own items: the job asks for this file after it finishes (season_audio_answer_outdated).
        ctx.note_changed_sibling_left_out(rec.canonical_path)
    return DetectorAnswer(tuple(candidates), signature)


def season_audio_answer_outdated(ctx: PipelineContext, canonical_path: str) -> bool:
    """Whether an episode's stored season audio answer is out of date with its season on disk now, and its intro could
    change with it (the conditions ``season_audio_followups`` asks a sibling again on).

    Read after a job finished, for its own episodes whose answer left out a sibling changed on disk: once the job read
    that sibling again, the episode goes into the job's Season follow-up instead of waiting for its own next run. While
    the sibling is still unread, the answer is as current as a new run's would be.

    Args:
        ctx: The job's context.
        canonical_path: Local path of the episode.

    Returns:
        True when a run now would match the season with other files than the stored answer.
    """
    rec = _current_record(ctx, canonical_path)
    answer = ctx.store.get_detector_run(rec.id, Source.SEASON_AUDIO) if rec is not None else None
    if answer is None or _intro_settled(ctx, rec):
        return False
    signature = _signature(ctx, _signature_paths(canonical_path, season_group(canonical_path)))
    return answer != signature and ctx.store.get_detector_failure(rec.id, Source.SEASON_AUDIO) != signature


def season_audio_spec(ffmpeg_path: str | None) -> LocalDetectorSpec | None:
    """The season audio detector, or None when no ffmpeg with chromaprint exists (source unavailable).

    Args:
        ffmpeg_path: The configured ffmpeg.

    Returns:
        The detector spec, or None.
    """
    from ..pipeline import LocalDetectorSpec

    if chromaprint_ffmpeg(ffmpeg_path) is None:
        return None
    return LocalDetectorSpec(
        source=Source.SEASON_AUDIO,
        types=frozenset({MarkerType.INTRO}),
        detect=detect_season_audio,
        stores=frozenset({Source.SEASON_AUDIO, Source.SEASON_AUDIO_PREVIOUS}),
        version=SEASON_AUDIO_VERSION,
        due=season_audio_due,
        needs_worker=season_audio_needs_worker,
        followups=season_audio_followups,
    )
