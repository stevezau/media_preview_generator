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
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

import numpy as np
from loguru import logger

from ...plex_client import VIDEO_EXTENSIONS
from ..decide import DecisionStatus, intro_chapter_length_ms, intro_chapter_limit_ms
from ..external_ids import ids_from_path, is_extra
from ..models import Candidate, FileIdentity, MarkerType, Source
from ..probe import ProbeError, probe_media
from ..sources.chapters import CHAPTER_RULES_VERSION, chapter_candidates
from . import POINT_S
from .fingerprint import (
    FingerprintError,
    FingerprintSkippedError,
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
    IntroSegment,
    Run,
    file_hits,
    intro_for,
    pair_runs,
)

if TYPE_CHECKING:
    from ..pipeline import DetectorAnswer, LocalDetectorSpec, PipelineContext
    from ..store import FileRecord

# The season step's own version (matcher v3 plus the silence guard, the provable pair skip and the group rule): stored with
# its answers and with cached pairs, so a change to any of them is matched again.
SEASON_AUDIO_VERSION = 4
MAX_PREVIOUS_SEASON_FILES = 4
MAX_GROUP_EPISODES = 40
# A season member ffprobe couldn't read, or ffmpeg couldn't fingerprint, is left out of other episodes' season steps for
# this long (while unchanged).
UNREADABLE_MEMBER_RETRY = timedelta(days=1)
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


def season_intro(
    target: str,
    files: Sequence[str],
    points: Mapping[str, np.ndarray],
    runs_between: Callable[[str, str], list[Run]],
) -> IntroSegment | None:
    """One episode's intro from its group: the v3 matcher's answer unless it is mostly silence.

    Args:
        target: The episode (one of ``files``).
        files: The group, in matching order.
        points: Fingerprint per file (``target``'s at least).
        runs_between: Runs for ``(earlier, later)`` (:func:`season_pair_runs`, cached).

    Returns:
        The intro with its support, or None.
    """
    segment = intro_for(file_hits(target, files, runs_between), len(files) - 1)
    if segment is not None and _mostly_silence(points[target], segment):
        return None
    return segment


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


def _member_record(ctx: PipelineContext, path: str) -> FileRecord | None:
    """A season member's record, read like its own run would read it.

    The store's record when it matches the disk and holds a duration and current chapters; otherwise the file is probed
    and its duration and chapters are stored (its own run then reads nothing again). None for a file that changed since
    its record was made, or while it was probed (its own run reads it again), or can't be probed. A file that couldn't be
    probed isn't probed again for a day while its identity stays the same, except by a forced re-detect. The season
    step holds only its own file's path lock, so the store refuses to change another file's identity
    (``record_member``).
    """
    identity = _disk_identity(path)
    rec = ctx.store.get_file(path)
    if identity is None or (rec is not None and identity != (rec.size, rec.mtime_ns)):
        return None
    if (
        rec is not None
        and rec.duration_ms
        and ctx.store.evidence_version(rec.id, Source.CHAPTERS) == CHAPTER_RULES_VERSION
    ):
        return rec
    probed_as = FileIdentity(path, *identity)
    failed_at = None if ctx.force else ctx.store.member_probe_failed_at(probed_as)
    if failed_at is not None and ctx.now() - failed_at < UNREADABLE_MEMBER_RETRY:
        return None
    try:
        probe = probe_media(path, ffprobe=ctx.ffprobe)
    except ProbeError as exc:
        logger.debug("The season step skips {}: {}", os.path.basename(path), exc)
        probe = None
    if _disk_identity(path) != identity:
        return None
    if probe is None or not probe.duration_ms:
        now = ctx.now()
        ctx.store.record_member_probe_failure(probed_as, now, forget_before=now - UNREADABLE_MEMBER_RETRY)
        return None
    return ctx.store.record_member(
        probed_as,
        duration_ms=probe.duration_ms,
        season_key=os.path.dirname(path),
        chapters=chapter_candidates(probe),
        chapter_version=CHAPTER_RULES_VERSION,
    )


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
    lengths = {path: _intro_chapter_ms(ctx, _member_record(ctx, path)) for path in group.episodes}

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
    return [path, size, mtime_ns, bool(current and has_cached_fingerprint(ctx.store, rec))]


def _signature(
    ctx: PipelineContext,
    paths: tuple[str, ...],
    matched: Mapping[str, FileRecord] | None = None,
    on_disk: dict[str, list] | None = None,
) -> str:
    """What an answer is based on: each file's identity and whether it has a fingerprint.

    ``matched`` files (a detector run's) enter with the identity they were matched with, so a sibling replaced while the
    season was matched makes the answer due again; every other file enters as it is on disk now (read once per path
    into ``on_disk`` when a caller builds several signatures).
    """
    on_disk = {} if on_disk is None else on_disk
    items = []
    for path in paths:
        if matched and path in matched:
            items.append([path, matched[path].size, matched[path].mtime_ns, True])
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

    It does while an episode of the folder (this one included) still needs ffmpeg or ffprobe, or while a pair this
    episode hasn't been matched with yet is too slow for a checking thread (:func:`slow_to_match`: long constant
    stretches in both openings that don't provably rule out an intro). Otherwise matching is cheap enough to run inline.
    A sibling ffmpeg failed on lately doesn't count: the step leaves it out.

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
        fingerprinted[path] = (stored, cached)
    if rec.canonical_path not in fingerprinted:
        return False
    if len(group.episodes) == 1:
        pairs = []
        for path in view.previous_season():
            member = _current_record(ctx, path)
            cached = _cached_points(ctx, member)
            if member is not None and cached is not None:
                pairs.append((fingerprinted[rec.canonical_path], (member, cached)))
    else:
        own = fingerprinted[rec.canonical_path]
        pairs = [
            (fingerprinted[path], own) if path < rec.canonical_path else (own, fingerprinted[path])
            for path in fingerprinted
            if path != rec.canonical_path
        ]
    return any(
        ctx.store.get_season_pair(first.id, second.id, SEASON_AUDIO_VERSION) is None and slow_to_match(a, b)
        for (first, a), (second, b) in pairs
    )


def _intro(
    ctx: PipelineContext,
    target: str,
    files: list[str],
    records: dict[str, FileRecord],
    points: dict[str, np.ndarray],
) -> IntroSegment | None:
    def runs_between(first: str, second: str) -> list[Run]:
        a, b = records[first], records[second]
        cached = ctx.store.get_season_pair(a.id, b.id, SEASON_AUDIO_VERSION)
        if cached is not None:
            return [Run(*run) for run in cached]
        runs = season_pair_runs(points[first], points[second])
        ctx.store.set_season_pair(
            a.id,
            b.id,
            SEASON_AUDIO_VERSION,
            [tuple(run) for run in runs],
            identity_a=(a.size, a.mtime_ns),
            identity_b=(b.size, b.mtime_ns),
        )
        return runs

    return season_intro(target, files, points, runs_between)


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


def _request_redecide(ctx: PipelineContext, rec: FileRecord, members: dict[str, FileRecord], signature: str) -> None:
    """Ask again for the siblings whose intro is undecided, or decided with a season audio answer, and whose answer
    was based on other season files than this run's. A sibling season audio never answered for is left alone: its own
    run matches the whole group, and one no server takes for Intro & Credits never runs."""
    folder = os.path.dirname(rec.canonical_path)
    stale = []
    for path, member in members.items():
        if member.id == rec.id or os.path.dirname(path) != folder:
            continue
        answer = ctx.store.get_detector_run(member.id, Source.SEASON_AUDIO)
        if answer is None or answer == signature:
            continue
        intro = ctx.store.get_decisions(member.id).get(MarkerType.INTRO)
        if (
            intro is not None
            and intro.status is DecisionStatus.DECIDED
            and not intro_rests_on_season_audio(ctx, member)
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
        intro = ctx.store.get_decisions(member.id).get(MarkerType.INTRO)
        if (
            intro is not None
            and intro.status is DecisionStatus.DECIDED
            and not intro_rests_on_season_audio(ctx, member)
        ):
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
    unchanged; this episode's own fingerprint is always tried.

    Args:
        rec: The episode (its identity matches the disk: the pipeline just checked).
        ctx: The job's context.
        gpu: Unused (chromaprint is CPU only).
        gpu_device_path: Unused.
        phase_callback: Worker row step text.
        cancel_check: True once the job is cancelled.
        pause_check: Unused (a paused job doesn't block a worker).

    Returns:
        At most one intro candidate (season audio, or the previous-season hint), with the signature of the season files
        it was matched with (the pipeline stores both together).

    Raises:
        DetectorUnavailableError: No chromaprint ffmpeg, this episode couldn't be fingerprinted, or cancelled.
    """
    from ..pipeline import DetectorAnswer, DetectorUnavailableError

    phase = phase_callback or (lambda _text: None)
    ffmpeg = chromaprint_ffmpeg(getattr(ctx.config, "ffmpeg_path", None))
    if ffmpeg is None:
        raise DetectorUnavailableError("no ffmpeg with chromaprint")
    group = season_group(rec.canonical_path)
    phase("Fingerprinting audio…")
    try:
        # A failure is recorded while the file's lock is held, so siblings' steps waiting on it skip it (for a day).
        own = ensure_fingerprint(
            ctx.store,
            rec,
            ffmpeg=ffmpeg,
            cancel_check=cancel_check,
            on_failure=functools.partial(_record_fingerprint_failure, ctx, rec),
        )
    except FingerprintError as exc:
        if not (cancel_check and cancel_check()):
            # Siblings' runs don't ask for this file again until its season changes (season_audio_followups).
            attempted = _signature(ctx, _signature_paths(rec.canonical_path, group))
            ctx.store.set_detector_failure(rec.id, Source.SEASON_AUDIO, attempted)
        raise DetectorUnavailableError(str(exc)) from exc
    if own is None:
        raise DetectorUnavailableError("the file changed while it was fingerprinted")
    records: dict[str, FileRecord] = {rec.canonical_path: rec}
    points: dict[str, np.ndarray] = {rec.canonical_path: own}
    others = [p for p in group.episodes if p != rec.canonical_path]
    left_out_changed = False
    for n, path in enumerate(others, 1):
        if cancel_check and cancel_check():
            raise DetectorUnavailableError("cancelled")
        member = _member_record(ctx, path)
        if member is None:
            left_out_changed = left_out_changed or _changed_since_record(ctx, path)
            continue
        cached = _cached_points(ctx, member)
        if cached is None:
            phase(f"Fingerprinting season audio {n}/{len(others)}…")
            try:
                cached = ensure_fingerprint(
                    ctx.store,
                    member,
                    ffmpeg=ffmpeg,
                    cancel_check=cancel_check,
                    skip=functools.partial(_fingerprint_failed_lately, ctx, member),
                    on_failure=functools.partial(_record_fingerprint_failure, ctx, member),
                )
            except FingerprintSkippedError:
                logger.debug("Season audio leaves out {}: it couldn't be fingerprinted lately", os.path.basename(path))
                continue
            except FingerprintError as exc:
                logger.info("Season audio leaves out {} this time: {}", os.path.basename(path), exc)
                continue
        if cached is not None:
            records[path], points[path] = member, cached

    phase("Matching season audio…")
    candidates: list[Candidate] = []
    previous_used: set[str] = set()
    audible = sorted(p for p, pts in points.items() if len(pts))
    if len(own) and len(audible) > 1:
        segment = _intro(ctx, rec.canonical_path, audible, records, points)
        if segment is not None:
            candidates.append(_candidate(segment, len(audible) - 1, Source.SEASON_AUDIO))
    elif len(own) and len(group.episodes) == 1:
        previous: dict[str, np.ndarray] = {}
        for path in previous_season_files(rec.canonical_path):
            member = _current_record(ctx, path)
            cached = _cached_points(ctx, member)
            if member is not None and cached is not None and len(cached):
                records[path], previous[path] = member, cached
        previous_used = set(previous)
        if previous:
            # The order few_siblings.py measured: this episode first, then the previous season's files by path.
            files = [rec.canonical_path, *sorted(previous)]
            segment = _intro(ctx, rec.canonical_path, files, records, {**points, **previous})
            if segment is not None:
                candidates.append(_candidate(segment, len(previous), Source.SEASON_AUDIO_PREVIOUS))

    matched = {path: records[path] for path in points.keys() | previous_used}
    signature = _signature(ctx, _signature_paths(rec.canonical_path, group), matched)
    _request_redecide(ctx, rec, records, signature)
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
    if answer is None:
        return False
    intro = ctx.store.get_decisions(rec.id).get(MarkerType.INTRO)
    if intro is not None and intro.status is DecisionStatus.DECIDED and not intro_rests_on_season_audio(ctx, rec):
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
