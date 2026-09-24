"""On-screen credit text (spec §5.4) on the credits truth sets: rule J alone against the truth, and our decisions with
it against Plex's own credits markers (``python -m tools.markers_eval credits-text``).

The truth is each file's last credits chapter (3 movies fixed by frame checks, ``credits/adjudicated.json``), so the
rows leave chapters out: these files stand for files without usable chapters. The rows mirror the pipeline, which reads
credit text only for credits the other sources leave undecided (``pipeline._detector_pending``): Plex's markers never
decide alone (rule 7), so every compared file asks it; in the online cases a SkipDB or TheIntroDB decision doesn't.
Summaries hold counts and folder names only; details (``--json``) hold paths and stay local.
"""

from __future__ import annotations

import ast
import contextlib
import dataclasses
import hashlib
import itertools
import json
import math
import os
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from loguru import logger

from media_preview_generator.markers import credits as credits_package
from media_preview_generator.markers.credits import rule_j
from media_preview_generator.markers.credits.detector import CREDITS_TEXT_VERSION, CreditsTextResult, find_credits
from media_preview_generator.markers.credits.frames import FRAME_H, FRAME_W, GpuDecodeError
from media_preview_generator.markers.decide import (
    DecisionContext,
    DecisionStatus,
    credits_limits_ms,
    decide,
    earliest_credits_start_ms,
)
from media_preview_generator.markers.models import Candidate, MarkerType, Source
from media_preview_generator.markers.probe import MediaProbe

from .cache import ProbeCache
from .credits import _name, _truth, judge_credits
from .data import evidence_dir
from .decisions import ORDER
from .decode_cache import DecodeCache, ffmpeg_build, rows_from_json, rows_to_json
from .online import SETTINGS, case_file, case_key, load_online, online_verdicts, tally
from .plex import PlexMarker, first_marker, load_baseline, server_candidates

SPEC_WITHIN_10S = 59
SPEC_EARLY_MAX = 1
# Q4 gate (owner, 2026-09-16: precision first, never looser than Plex): wrong answers (a start more than 10 s early) at
# most this percentage of a set's files, rounded up (a single answer both sources place early must not fail a set of
# fewer than 100 by construction): 80 files → Medium 2, High 1; 205 files → Medium 5, High 3.
MEDIUM_WRONG_PERCENT = 2
HIGH_WRONG_PERCENT = 1
CREDITS = frozenset({MarkerType.CREDITS})
# The gate's sets: the 80 files (movies40 + tv40 together) and the 205 movies.
GATE_SETS = {"80": ("movies40", "tv40"), "205": ("movie_credit_truth",)}
PACKAGE_ROOT = Path(credits_package.__file__).parents[2]
# The credit text detector and the package code outside it that shapes its answers: the tail decode's arguments and
# the probe. tests/markers_eval pins that every package module these files import is listed here or changes no answer.
# ``markers/decide.py`` is here for ``earliest_credits_start_ms``, which bounds the steps before the tail.
DETECTOR_SOURCES = ("markers/credits/*.py", "markers/probe.py", "processing/hwaccel.py", "markers/decide.py")
# The detector files that choose which windows are decoded (so they are in the ffmpeg command, the decode cache's key)
# but never what a decode of a given command returns: :func:`decode_digest` leaves them out.
RULE_FILES = ("markers/credits/rule_j.py", "markers/credits/detector.py", "markers/decide.py")
SHEET_TIMEOUT_S = 300
# Q5: every answer that moves by more than this against an earlier run is frame-checked.
CHANGED_BY_S = 10.0
HDR_PROBE_TIMEOUT_S = 60


class UnknownSetError(ValueError):
    """``--sets`` names a set this harness doesn't have."""


class SweepError(ValueError):
    """``--sweep`` names a constant this harness can't sweep, or one a patch wouldn't reach."""


def detector_files(root: Path = PACKAGE_ROOT, patterns: Sequence[str] = DETECTOR_SOURCES) -> list[Path]:
    """The source files :func:`detector_digest` hashes, sorted.

    Args:
        root: The ``media_preview_generator`` package folder.
        patterns: Globs under ``root``.

    Returns:
        The files.
    """
    return sorted({path for pattern in patterns for path in root.glob(pattern)})


def detector_digest(
    root: Path = PACKAGE_ROOT, patterns: Sequence[str] = DETECTOR_SOURCES, *, leave_out: Sequence[str] = ()
) -> str:
    """A digest of the credit text detector's source (:data:`DETECTOR_SOURCES`), part of every cached answer's key.

    ``CREDITS_TEXT_VERSION`` only moves when stored answers in users' ``markers.db`` must be asked again, so it stays
    put while the detector changes on a branch that never shipped. The harness gates exactly those changes: a cached
    answer must never outlive the code that made it.

    Args:
        root: The ``media_preview_generator`` package folder.
        patterns: Globs under ``root``.
        leave_out: Files under ``root`` not to hash (:func:`decode_digest`).

    Returns:
        16 hex characters.
    """
    digest = hashlib.sha256()
    for path in detector_files(root, patterns):
        relative = path.relative_to(root).as_posix()
        if relative not in leave_out:
            digest.update(relative.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:16]


def decode_digest(root: Path = PACKAGE_ROOT, patterns: Sequence[str] = DETECTOR_SOURCES) -> str:
    """A digest of the code that turns an ffmpeg command into rows, part of every cached decode's key
    (:class:`decode_cache.DecodeCache`): the detector's sources bar :data:`RULE_FILES`.

    The text detection model needs no digest of its own: ``textdet`` refuses a model whose SHA-256 isn't the one it
    pins, and the pin is in these files.

    Args:
        root: The ``media_preview_generator`` package folder.
        patterns: Globs under ``root``.

    Returns:
        16 hex characters.
    """
    return detector_digest(root, patterns, leave_out=RULE_FILES)


@dataclass
class RuleTally:
    """Spec §5.4's metric for one set of answers."""

    files: int = 0
    within_5s: int = 0
    within_10s: int = 0
    within_30s: int = 0
    early: int = 0
    late: int = 0
    none: int = 0

    def add(self, start_s: float | None, truth_s: float) -> None:
        """Count one file's answer against its truth.

        Args:
            start_s: The credits text start, or None when the tail held no roll.
            truth_s: The file's truth.
        """
        self.files += 1
        if start_s is None:
            self.none += 1
            return
        error = start_s - truth_s
        self.within_5s += abs(error) <= 5
        self.within_10s += abs(error) <= 10
        self.within_30s += abs(error) <= 30
        if abs(error) > 30:
            self.early += error < 0
            self.late += error > 0

    def meets_spec(self) -> bool:
        """Whether the tally clears spec §5.4's bar: 59 within 10 s and at most 1 early."""
        return self.within_10s >= SPEC_WITHIN_10S and self.early <= SPEC_EARLY_MAX

    def as_dict(self) -> dict:
        """The counts as a plain dict."""
        return dataclasses.asdict(self)


def text_candidates(start_s: float | None, end_s: float | None = None) -> list[Candidate]:
    """What the detector stores for an answer (``end_s`` None: the skip runs to the end of the file, Q3).

    Args:
        start_s: The credits start, or None when the tail held no roll.
        end_s: Where the skip ends, or None.

    Returns:
        The candidate list the pipeline would hand ``decide()`` (empty without an answer).
    """
    if start_s is None:
        return []
    end_ms = None if end_s is None else int(round(end_s * 1000))
    return [Candidate(MarkerType.CREDITS, int(round(start_s * 1000)), end_ms, Source.CREDITS_TEXT)]


def epilogue_like(
    key_rows: Sequence[rule_j.Row],
    start_s: float | None,
    overlays: Sequence[rule_j.Box] | None = None,
    run_rows: Sequence[rule_j.Row] | None = None,
) -> bool:
    """Whether a frame check must look at an answer for epilogue cards (spec §5.4: they aren't credits; rule J can't
    tell them from a roll they touch, ``test_rule_j.TestEpilogueCards``).

    True when the run's first frame with 3+ text boxes comes more than 10 s after the answer (it starts on sparse
    text), or a credit keyframe in the run's first 30 s is followed by the next one 10 s or more later (cards joined to
    the roll over a gap or black).

    The run and its frames are read the way rule J read them (``rule_j.overlay_boxes``), so the shape judged here is
    the shape the answer came from -- a channel bug's boxes are not what makes a frame look dense. The run is
    ``rule_j._run_rows``, the rule's own slice, so it stays the run whatever the band steps do: bounded by
    ``run_index``, not by ``index``, because version 3 can reach a start back before the run, and slicing from there
    in decode order covers rows that aren't the run's -- and gives nothing at all when the row the walk reached was
    emitted after it.

    Args:
        key_rows: The tail's keyframe rows, in decode order.
        start_s: The answer, or None.
        overlays: The overlays ``find_credits`` found (``CreditsTextResult.overlays``); None gathers them from
            ``key_rows``, which is right only for a file whose rows are the tail's alone. On the branch that reads the
            120 s before the tail they are not, and gathering again would call a roll that began before the tail its
            own overlay -- the one thing the detector refuses to do.
        run_rows: The rows rule J found the runs on (``CreditsTextResult.run_rows``), in place of ``key_rows``: an
            answer from the 640x360 reading found its run without the text the 320x180 reading had boxed, which
            ``key_rows`` still holds. None (or empty) finds them on ``key_rows``.

    Returns:
        Whether the answer is shaped like epilogue cards.
    """
    runs = list(run_rows) if run_rows else list(key_rows)
    found = rule_j.overlay_boxes(key_rows) if overlays is None else tuple(overlays)
    rows = rule_j.without_overlays(runs, found)
    coarse = rule_j.coarse_start(runs, without=rows)
    if coarse is None or start_s is None:
        return False
    run = list(rule_j._run_rows(rows, coarse))
    dense = next((row[0] for row in run if row[1] >= 3), None)
    if dense is not None and dense - start_s > 10:
        return True
    credit = [row for row in run if rule_j.is_credit(row)]
    return any(b[0] - a[0] >= 10 and a[0] < coarse.pts_s + 30 for a, b in zip(credit, credit[1:], strict=False))


@dataclass
class TextRows:
    """Verdict counts per row, the Q3 end counts, and per-file details.

    Attributes:
        plex: Plex's first credits marker.
        text: Credits text alone (the detector's answer, no decision rules).
        high: What the pipeline publishes at High with credits text and Plex's markers (chapters left out).
        medium: The same at Medium.
        text_and_server_only: High decisions resting only on credits text + a server's own marker (Q2's pair).
        ends_found: Credits text answers with an end (more than 30 s of the file after the roll, Q3).
        ends_published: Per row (``high``, ``medium``): decisions whose skip stops before the end of the file.
        files: Per-file details (paths: local-only).
    """

    plex: Counter = field(default_factory=Counter)
    text: Counter = field(default_factory=Counter)
    high: Counter = field(default_factory=Counter)
    medium: Counter = field(default_factory=Counter)
    text_and_server_only: int = 0
    ends_found: int = 0
    ends_published: Counter = field(default_factory=Counter)
    files: list[dict] = field(default_factory=list)


def _earliest_start_s(duration_ms: int, *, is_episode: bool) -> float:
    """The earliest credits start :func:`_decided` keeps (Automatic, and a file that isn't an episode is a movie), which
    bounds the app's steps before the tail exactly as the pipeline's own bound does."""
    window_ms, movie_cap_ms = credits_limits_ms(is_episode=is_episode, tv_window_s=None, movie_window_s=None)
    earliest_ms = earliest_credits_start_ms(
        duration_ms, is_movie=not is_episode, credits_window_ms=window_ms, movie_credits_max_from_end_ms=movie_cap_ms
    )
    return earliest_ms / 1000.0


def _decided(
    candidates: list[Candidate], duration_ms: int, is_movie: bool, level: str
) -> tuple[tuple[float, float] | None, tuple[str, ...], str]:
    decision = decide(candidates, DecisionContext(duration_ms, is_movie, level, CREDITS, ORDER), {})[MarkerType.CREDITS]
    if decision.status is not DecisionStatus.DECIDED:
        return None, (), decision.reason
    marker = decision.marker
    return (marker.start_ms / 1000, marker.end_ms / 1000), marker.decided_by, decision.reason


def compare_text(
    files: list[dict],
    adjudicated: dict[str, dict],
    *,
    answers: Mapping[str, tuple[float | None, float | None]],
    probe: Callable[[str], MediaProbe],
    baseline: Mapping[str, list[PlexMarker]],
    is_movie: bool,
) -> TextRows:
    """Plex's first credits marker, credits text alone, and what the pipeline publishes with both (High, Medium).

    Args:
        files: Truth rows (``file``, ``credits_start``).
        adjudicated: Truth fixed by frame checks, by file name.
        answers: The app's credits text ``(start, end)`` per file (start None: no roll found; end None: open-ended).
        probe: A file's duration.
        baseline: Plex's markers by file path.
        is_movie: The files are movies.

    Returns:
        The rows.
    """
    rows = TextRows()
    for entry in files:
        path, truth = entry["file"], _truth(entry, adjudicated)
        duration = probe(path).duration_ms or 0
        markers = baseline.get(path, [])
        plex = first_marker(markers, MarkerType.CREDITS)
        start_s, end_s = answers.get(path, (None, None))
        text = text_candidates(start_s, end_s)
        servers = server_candidates(markers, MarkerType.CREDITS)
        detail = {"file": path, "name": _name(path), "truth": truth, "text": start_s, "text_end": end_s,
                  "duration": duration / 1000, "plex": plex.start_ms / 1000 if plex else None}  # fmt: skip
        rows.plex[judge_credits(detail["plex"], truth)] += 1
        rows.text[judge_credits(start_s, truth)] += 1
        rows.ends_found += end_s is not None
        for level in ("high", "medium"):
            segment, decided_by, reason = _decided(text + servers, duration, is_movie, level)
            getattr(rows, level)[judge_credits(segment[0] if segment else None, truth)] += 1
            detail[level], detail[f"{level}_reason"] = segment, reason
            if segment is not None and segment[1] * 1000 < duration:
                rows.ends_published[level] += 1
            if level == "high" and set(decided_by) == {"credits_text", "server_markers"}:
                rows.text_and_server_only += 1
        rows.files.append(detail)
    return rows


def wrong_cap(files: int, percent: int) -> int:
    """The most wrong answers a set of ``files`` may have at ``percent``, rounded up (controller ruling, 2026-09-16).

    Args:
        files: The set's size.
        percent: The cap's percentage.

    Returns:
        The cap.
    """
    return math.ceil(files * percent / 100)


def gate_checks(rows: TextRows, files: int) -> dict[str, bool]:
    """Q4's gate for one set, check by check (owner, 2026-09-16: precision first, never looser than Plex).

    Args:
        rows: The set's rows.
        files: The set's size.

    Returns:
        Each check's name and whether it passed, in the order they are reported.
    """
    medium_cap, high_cap = wrong_cap(files, MEDIUM_WRONG_PERCENT), wrong_cap(files, HIGH_WRONG_PERCENT)
    return {
        "Medium useful >= Plex useful": rows.medium["useful"] >= rows.plex["useful"],
        f"Medium wrong <= {medium_cap} ({MEDIUM_WRONG_PERCENT}% of {files})": rows.medium["wrong"] <= medium_cap,
        "Medium wrong <= Plex wrong": rows.medium["wrong"] <= rows.plex["wrong"],
        f"High wrong <= {high_cap} ({HIGH_WRONG_PERCENT}% of {files})": rows.high["wrong"] <= high_cap,
        "High wrong <= Plex wrong": rows.high["wrong"] <= rows.plex["wrong"],
    }


def beats_plex(rows: TextRows, files: int) -> bool:
    """Whether a set passes every check of :func:`gate_checks`.

    Args:
        rows: The set's rows.
        files: The set's size.

    Returns:
        Whether the set passes.
    """
    return all(gate_checks(rows, files).values())


def merge_rows(parts: Iterable[TextRows]) -> TextRows:
    """One set's rows from its parts (the 80 = movies40 + tv40).

    Args:
        parts: The parts' rows.

    Returns:
        Their sum.
    """
    merged = TextRows()
    for part in parts:
        for name in ("plex", "text", "high", "medium", "ends_published"):
            getattr(merged, name).update(getattr(part, name))
        merged.text_and_server_only += part.text_and_server_only
        merged.ends_found += part.ends_found
        merged.files.extend(part.files)
    return merged


def sheet_reasons(
    detail: dict,
    key_rows: Sequence[rule_j.Row],
    overlays: Sequence[rule_j.Box] | None = None,
    run_rows: Sequence[rule_j.Row] | None = None,
) -> list[str]:
    """Why a file's credits text answer gets a frame-check sheet (Q5, I7); empty: it doesn't.

    Every answer more than 10 s early (10–30 s early included), more than 30 s late, shaped like epilogue cards, or
    with an end (to check the scene after it is really a scene).

    Args:
        detail: One file's row (``text``, ``text_end``, ``truth``).
        key_rows: The file's keyframe rows.
        overlays: The overlays the run was read without (:func:`epilogue_like`).
        run_rows: The rows the run was found on (:func:`epilogue_like`).

    Returns:
        The reasons, in reporting order.
    """
    start, truth = detail["text"], detail["truth"]
    if start is None:
        return []
    reasons = []
    if start < truth - 30:
        reasons.append("early >30 s")
    elif start < truth - 10:
        reasons.append("early 10-30 s")
    if start > truth + 30:
        reasons.append("late >30 s")
    if epilogue_like(key_rows, start, overlays, run_rows):
        reasons.append("epilogue-like")
    if detail["text_end"] is not None:
        reasons.append("end kept")
    return reasons


def undecided_credits(verdicts: Iterable[dict]) -> set[str]:
    """Online cases whose credits the other sources leave undecided: the only ones the pipeline reads credit text for.

    Args:
        verdicts: ``online.online_verdicts`` rows.

    Returns:
        Their keys.
    """
    return {v["key"] for v in verdicts if v["type"] == MarkerType.CREDITS.value and v["verdict"] == "missed"}


def hdr_kind(path: str, *, ffprobe: str) -> str:
    """``sdr``, ``hdr10`` (PQ or HLG), ``dv5`` (Dolby Vision profile 5), ``dv_other``, or ``unreadable`` (ffprobe reads
    only).

    Args:
        path: The media file (only read).
        ffprobe: ffprobe binary.

    Returns:
        The file's HDR kind (Task 1 M5's grouping, ``evidence/credits/phase3/measure_hdr.py``): a probe that fails,
        times out or answers something other than its JSON is ``unreadable``, never counted as ``sdr``.
    """
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=color_transfer:stream_side_data=dv_profile", "-of", "json", path],
            capture_output=True, text=True, timeout=HDR_PROBE_TIMEOUT_S,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("ffprobe couldn't read the HDR kind of {}: {}", _name(path), type(exc).__name__)
        return "unreadable"
    try:
        answer = json.loads(proc.stdout) if proc.returncode == 0 else None
    except ValueError:
        answer = None
    if not isinstance(answer, dict):
        logger.warning("ffprobe couldn't read the HDR kind of {} (exit {})", _name(path), proc.returncode)
        return "unreadable"
    stream = (answer.get("streams") or [{}])[0]
    profiles = [s.get("dv_profile") for s in stream.get("side_data_list") or [] if "dv_profile" in s]
    if profiles:
        return "dv5" if 5 in profiles else "dv_other"
    return "hdr10" if stream.get("color_transfer") in ("smpte2084", "arib-std-b67") else "sdr"


def sheet_command(ffmpeg: str, path: str, around_s: float, out: str) -> list[str]:
    """A 4×2 contact sheet of the 80 s around a time, one frame every 10 s (for adjudicating an answer).

    Args:
        ffmpeg: ffmpeg binary.
        path: The media file (only read).
        around_s: The time to tile around.
        out: The sheet's path (local-only).

    Returns:
        The argv.
    """
    return [ffmpeg, "-v", "error", "-ss", f"{max(0.0, around_s - 40):.1f}", "-t", "80", "-i", path,
            "-vf", "fps=1/10,scale=320:-2,tile=4x2", "-frames:v", "1", "-y", out]  # fmt: skip


class CreditsTextCache:
    """``result(path)``: the app's credits text rows and start for a file, computed once per identity and version.

    On the GPU path a file whose GPU decode fails is read again on the CPU, which is what the app does with it: the
    detector turns a :class:`frames.GpuDecodeError` into ``CodecNotSupportedError`` and the GPU worker reruns the whole
    item with no GPU (``jobs/worker.py``). The measured library has AV1 files a Pascal card cannot decode, and without
    the same rerun they would leave the set instead of getting the answer a user gets. ``gpu_fallbacks`` names them.
    """

    def __init__(
        self,
        root: Path,
        *,
        ffmpeg: str,
        decode: str,
        gpu_device: str | None,
        detect_boxes: Callable[[np.ndarray], list[tuple[rule_j.Box, ...]]],
        backend: Callable[[], str | None],
        probe: Callable[[str], MediaProbe],
        decodes: DecodeCache | None = None,
    ) -> None:
        """Create the cache (``root/credits_text``).

        Args:
            root: Cache folder; must not be under /data*.
            ffmpeg: ffmpeg binary.
            decode: ``gpu`` or ``cpu``.
            gpu_device: The GPU worker's device (ignored on the CPU path).
            detect_boxes: Text boxes per chunk of luma planes.
            backend: The text detection backend ``detect_boxes`` reads on (:attr:`TextDetection.backend`): answers
                read on another backend, or decoded on another GPU, are other answers.
            probe: A file's duration.
            decodes: Where the detector's decodes are kept across detector changes, or None to decode every time an
                answer isn't cached.

        Raises:
            ValueError: ``root`` is under /data*.
        """
        if str(root.resolve()).startswith("/data"):
            raise ValueError("the credits text cache must not live under /data*")
        self._root = root / "credits_text"
        self._root.mkdir(parents=True, exist_ok=True)
        self._ffmpeg, self._decode, self._gpu_device = ffmpeg, decode, gpu_device
        self._detect_boxes, self._backend, self._probe, self._decodes = detect_boxes, backend, probe, decodes
        self._build: str | None = None
        self.detector_digest = detector_digest()
        self.gpu_fallbacks: set[str] = set()

    def _find(self, path: str, *, is_episode: bool, gpu: str | None) -> CreditsTextResult:
        serving = self._decodes.serving() if self._decodes is not None else contextlib.nullcontext()
        with serving:
            duration_ms = self._probe(path).duration_ms
            return find_credits(
                path, duration_ms=duration_ms, is_episode=is_episode, ffmpeg=self._ffmpeg,
                detect_boxes=self._detect_boxes, gpu=gpu, gpu_device_path=self._gpu_device if gpu else None,
                earliest_start_s=_earliest_start_s(duration_ms, is_episode=is_episode),
            )  # fmt: skip

    def result(self, path: str, *, is_episode: bool) -> dict:
        """The app's own answer for one file.

        Args:
            path: The media file (only read).
            is_episode: The file is a TV episode (a 450 s tail, T-R4).

        Returns:
            ``{"start_s", "end_s", "key", "fine", "end", "overlays", "scale", "runs"}`` (from the cache when this
            identity, detector version and source, ffmpeg build, decode path and GPU, text detection backend and kind
            were read before). ``scale`` is 2 for an answer from the 640x360 reading of a tail the 320x180 one found
            nothing in, and ``runs`` then the keyframe rows rule J found its run on (empty at 1). ``overlays`` and
            ``runs`` are what :func:`epilogue_like` has to be handed rather than gather again. An answer whose text
            detection backend changed while the file was read is returned but not kept.
        """
        if self._build is None:
            self._build = ffmpeg_build(self._ffmpeg)
        st = os.stat(path)
        device = self._gpu_device if self._decode == "gpu" else None
        backend = self._backend()
        key = (
            f"{path}|{st.st_size}|{st.st_mtime_ns}|{CREDITS_TEXT_VERSION}|{self.detector_digest}|{self._build}|"
            f"{self._decode}|{device}|{backend}|{is_episode}"
        )
        cached = self._root / (hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest() + ".json")
        fell_back = cached.with_suffix(".cpu")
        if cached.exists():
            if fell_back.exists():
                self.gpu_fallbacks.add(_name(path))
            return json.loads(cached.read_text())
        gpu = "NVIDIA" if self._decode == "gpu" else None
        try:
            found = self._find(path, is_episode=is_episode, gpu=gpu)
        except GpuDecodeError as exc:
            logger.warning(
                "{} wouldn't decode on the GPU, reading it on the CPU as a worker does: {}", _name(path), exc
            )
            found = self._find(path, is_episode=is_episode, gpu=None)
            fell_back.write_text("")
            self.gpu_fallbacks.add(_name(path))
        data = {"start_s": found.start_s, "end_s": found.end_s, "key": rows_to_json(found.key_rows),
                "fine": rows_to_json(found.fine_rows), "end": rows_to_json(found.end_rows),
                "overlays": [list(box) for box in found.overlays], "scale": found.scale,
                "runs": rows_to_json(found.run_rows)}  # fmt: skip
        if self._backend() == backend:
            cached.write_text(json.dumps(data))
        return data


@dataclass(frozen=True)
class TextDetection:
    """The app's own text box detector for one decode path.

    Attributes:
        detect_boxes: Text boxes per chunk of luma planes.
        backend: What reads them right now: ``cpu``, or the GPU backend and its helper's device (``webgpu cuda:0``);
            None before the first request, when the helper's self-test hasn't chosen yet.
        close: Stops every helper, once, at the end of the whole run.
    """

    detect_boxes: Callable[[np.ndarray], list[tuple[rule_j.Box, ...]]]
    backend: Callable[[], str | None]
    close: Callable[[], None]


def _detection_on(decode: str, gpu_device: str) -> TextDetection:
    """The app's own text box detector for this decode path.

    The pool is the process's own (``get_textdet_pool``), exactly as a worker gets it, and ``close_all`` is called once
    at the end of the whole run: it stops every helper permanently (controller note N3).
    """
    from media_preview_generator.markers.credits import textdet_helper

    pool = textdet_helper.get_textdet_pool()
    gpu = "NVIDIA" if decode == "gpu" else None
    device = gpu_device if gpu else None

    def backend() -> str | None:
        used = pool.backend_of(gpu, device)
        return used if used in (None, "cpu") else f"{used} {textdet_helper.device_key(gpu, device)}"

    return TextDetection(
        lambda planes: pool.detect_boxes(planes, gpu=gpu, gpu_device_path=device), backend, pool.close_all
    )


def _rows_summary(rows: TextRows) -> dict:
    return {"plex": dict(sorted(rows.plex.items())), "text": dict(sorted(rows.text.items())),
            "high": dict(sorted(rows.high.items())), "medium": dict(sorted(rows.medium.items())),
            "text_and_server_only": rows.text_and_server_only, "ends_found": rows.ends_found,
            "ends_published": dict(sorted(rows.ends_published.items()))}  # fmt: skip


def _write_sheet(ffmpeg: str, path: str, around_s: float, out: Path) -> None:
    """One frame-check sheet; a failed or stuck ffmpeg is logged and skipped, never ends an hour-long run."""
    try:
        proc = subprocess.run(
            sheet_command(ffmpeg, path, around_s, str(out)), capture_output=True, timeout=SHEET_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("No frame-check sheet {}: {}", out.name, type(exc).__name__)
        return
    if proc.returncode != 0:
        logger.warning("No frame-check sheet {}: ffmpeg exited {}", out.name, proc.returncode)


def changed_answers(
    details: Mapping[str, list[dict]], before: Mapping[str, list[dict]], *, by_s: float = CHANGED_BY_S
) -> list[dict]:
    """Every set row whose credits text start or end moved by more than ``by_s`` against an earlier run (Q5: each is
    frame-checked), or gained or lost one.

    Args:
        details: This run's per-set rows (``text``, ``text_end``).
        before: An earlier run's (its ``--json`` file's ``details``); rows it doesn't have are left out.
        by_s: How far an answer must move to count.

    Returns:
        One entry per changed row: the row, ``moved`` (``start`` and/or ``end``) and the earlier run's answers.
    """

    def moved(old: float | None, new: float | None) -> bool:
        return (old is None) != (new is None) or (old is not None and abs(new - old) > by_s)

    changed = []
    for name, rows in details.items():
        earlier = {f["file"]: f for f in before.get(name, [])}
        for f in rows:
            old = earlier.get(f["file"])
            if old is None:
                continue
            what = [label for label, key in (("start", "text"), ("end", "text_end")) if moved(old[key], f[key])]
            if what:
                changed.append({"set": name, "detail": f, "moved": what, "start_before": old["text"],
                                "end_before": old["text_end"]})  # fmt: skip
    return changed


def _minus(value: float | None, base: float) -> float | None:
    return None if value is None else round(value - base, 1)


def _changed_entry(change: dict) -> dict:
    """One row of the changed-answers table: names and numbers only, never a path."""
    f = change["detail"]
    return {"set": change["set"], "name": f["name"], "moved": change["moved"],
            "start_minus_truth": [_minus(change["start_before"], f["truth"]), _minus(f["text"], f["truth"])],
            "end_minus_duration": [_minus(change["end_before"], f["duration"]), _minus(f["text_end"], f["duration"])],
            "medium_minus_truth": _minus(f["medium"][0] if f["medium"] else None, f["truth"]),
            "high_minus_truth": _minus(f["high"][0] if f["high"] else None, f["truth"])}  # fmt: skip


def _write_sheets(ffmpeg: str, sheets_dir: Path, name: str, detail: dict) -> None:
    """Sheets around one row's start and end, named by the file and the time, so a re-run writes only new ones."""
    sheets_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{name}-{hashlib.sha1(detail['file'].encode(), usedforsecurity=False).hexdigest()[:10]}"
    for label, around in (("", detail["text"]), ("-end", detail["text_end"])):
        if around is None:
            continue
        out = sheets_dir / f"{stem}{label}-{around:.0f}.jpg"
        if not out.exists():
            _write_sheet(ffmpeg, detail["file"], around, out)


def _sheet_entry(name: str, detail: dict, reasons: list[str]) -> dict:
    """One row of the frame-check table: names and numbers only, never a path."""
    end = detail["text_end"]
    return {"set": name, "name": detail["name"], "reasons": reasons,
            "answer_minus_truth": round(detail["text"] - detail["truth"], 1),
            "end_minus_duration": None if end is None else round(end - detail["duration"], 1)}  # fmt: skip


def run_credits_text(
    *,
    decode: str,
    gpu_device: str,
    sets: tuple[str, ...],
    online: bool,
    cache_root: Path,
    ffmpeg: str,
    ffprobe: str,
    baseline_path: Path,
    sheets_dir: Path | None,
    before: Mapping[str, list[dict]] | None = None,
) -> tuple[dict, dict, bool]:
    """Every row for the chosen sets (``80`` = movies40 + tv40, ``205`` = the 205-movie set).

    Args:
        decode: ``gpu`` or ``cpu``.
        gpu_device: The GPU worker's device (``cuda:0``).
        sets: Which of ``80`` and ``205`` to run.
        online: Also the 43 verified online cases.
        cache_root: Where probes and credits text answers are cached (never under /data*).
        ffmpeg: ffmpeg binary.
        ffprobe: ffprobe binary.
        baseline_path: Plex's markers.
        sheets_dir: Where to write frame-check sheets, or None for none.
        before: An earlier run's details (``--changed-since``): every answer that moved against it is listed
            (``changed``), and only those get sheets (the others were looked at on that run).

    Returns:
        The summary (counts and names), details (paths; local-only), and whether the gate passed (rule J on the 80
        meets the spec, and each chosen set passes :func:`gate_checks`).

    Raises:
        UnknownSetError: ``sets`` names a set this harness doesn't have. Checked before anything is measured: a set
            that is silently skipped (``--sets "80, 205"`` splits to ``"80"`` and ``" 205"``) would report a clean gate
            for a set that was never run.
    """
    unknown = sorted(set(sets) - set(GATE_SETS))
    if unknown:
        raise UnknownSetError(f"unknown set(s): {unknown}; choose from {sorted(GATE_SETS)}")
    evidence = evidence_dir()

    def load(name: str) -> list | dict:
        return json.loads((evidence / f"credits/{name}.json").read_text())

    adjudicated = load("adjudicated")
    probes = ProbeCache(cache_root, ffprobe=ffprobe)
    baseline = load_baseline(baseline_path)
    kinds_of = {"movies40": True, "tv40": False, "movie_credit_truth": True}
    details: dict = {}
    rule = RuleTally()
    kinds: dict[str, RuleTally] = {}
    passed = True
    detection = _detection_on(decode, gpu_device)
    detect_boxes = detection.detect_boxes
    try:
        # One blank frame starts the helper and its self-test, so the decode cache knows from the first file on which
        # backend reads the boxes (a GPU's self-test can pick the CPU).
        detect_boxes(np.zeros((1, FRAME_H, FRAME_W), dtype=np.uint8))
        decodes = DecodeCache(cache_root, digest=decode_digest(), backend=detection.backend)
        cache = CreditsTextCache(
            cache_root, ffmpeg=ffmpeg, decode=decode, gpu_device=gpu_device, detect_boxes=detect_boxes,
            backend=detection.backend, probe=probes.probe, decodes=decodes,
        )  # fmt: skip
        summary: dict = {"decode": decode, "detector_version": CREDITS_TEXT_VERSION,
                         "detector_digest": cache.detector_digest, "decode_digest": decodes.digest,
                         "text_detection": detection.backend(), "sets": {},
                         "gate": {}, "sheets": []}  # fmt: skip
        for group in (g for g in ("80", "205") if g in sets):
            parts = []
            for name in GATE_SETS[group]:
                is_movie = kinds_of[name]
                files = load(name)
                results = {f["file"]: cache.result(f["file"], is_episode=not is_movie) for f in files}
                answers = {path: (r["start_s"], r["end_s"]) for path, r in results.items()}
                if group == "80":
                    for f in files:
                        truth = _truth(f, adjudicated)
                        rule.add(answers[f["file"]][0], truth)
                        kinds.setdefault(hdr_kind(f["file"], ffprobe=ffprobe), RuleTally()).add(
                            answers[f["file"]][0], truth
                        )
                rows = compare_text(
                    files, adjudicated, answers=answers, probe=probes.probe, baseline=baseline, is_movie=is_movie
                )
                summary["sets"][name] = {"files": len(files), **_rows_summary(rows)}
                details[name] = rows.files
                parts.append(rows)
                for f in rows.files:
                    # Read without a guard on purpose: the answer cache's key carries detector_digest, which hashes
                    # every credits module, so an entry from before rows held positions can never be served here.
                    stored = results[f["file"]]
                    reasons = sheet_reasons(
                        f,
                        rows_from_json(stored["key"]),
                        [tuple(box) for box in stored["overlays"]],
                        rows_from_json(stored["runs"]),
                    )
                    if not reasons:
                        continue
                    summary["sheets"].append(_sheet_entry(name, f, reasons))
                    # Against an earlier run, only the answers that moved need looking at again (below).
                    if sheets_dir is not None and before is None:
                        _write_sheets(ffmpeg, sheets_dir, name, f)
            merged = merge_rows(parts)
            files_in_group = sum(len(r.files) for r in parts)
            checks = gate_checks(merged, files_in_group)
            summary["gate"][group] = {"files": files_in_group, **_rows_summary(merged), "checks": checks}
            passed = passed and all(checks.values())
        if "80" in sets:
            summary["rule_j_80"] = {**rule.as_dict(), "meets_spec": rule.meets_spec()}
            summary["rule_j_80_by_kind"] = {k: v.as_dict() for k, v in sorted(kinds.items())}
            passed = passed and rule.meets_spec()
        if online:
            summary["online"], details["online"] = _online(evidence, baseline, cache)
        if before is not None:
            changed = changed_answers({name: details[name] for name in kinds_of if name in details}, before)
            summary["changed"] = [_changed_entry(change) for change in changed]
            if sheets_dir is not None:
                for change in changed:
                    _write_sheets(ffmpeg, sheets_dir, change["set"], change["detail"])
        summary["decodes"] = {"decoded": decodes.decoded, "reused": decodes.reused}
    finally:
        detection.close()
    summary["gpu_fallbacks"] = sorted(cache.gpu_fallbacks)
    return summary, details, passed


def _online(evidence: Path, baseline: Mapping[str, list[PlexMarker]], cache: CreditsTextCache) -> tuple[dict, dict]:
    """The verified online cases at the app's three source settings, credit text added the way the pipeline adds it:
    only to cases whose credits the online answers and Plex's markers leave undecided."""
    results, dump = load_online(evidence)
    found = {case_key(r["case"]): case_file(r["case"], baseline.keys()) for r in results}
    servers = {key: server_candidates(baseline[path], MarkerType.CREDITS) for key, path in found.items() if path}
    texts = {}
    for key, path in found.items():
        if path:
            answer = cache.result(path, is_episode=True)
            texts[key] = text_candidates(answer["start_s"], answer["end_s"])
    summary: dict = {"cases": len(results), "files_found": sum(1 for p in found.values() if p)}
    details: dict = {}
    for label, order, level in SETTINGS:
        before = online_verdicts(results, dump, order=order, level=level, extra=servers)
        asked = undecided_credits(before)
        extra = {key: servers[key] + (texts[key] if key in asked else []) for key in servers}
        verdicts = online_verdicts(results, dump, order=order, level=level, extra=extra)
        summary[label] = {t: dict(sorted(c.items())) for t, c in tally(verdicts).items()}
        summary[label]["credits_text_asked"] = len(asked & texts.keys())
        details[label] = verdicts
    return summary, details


# ---------------------------------------------------------------------------- sweeping rule J's own constants


def parse_sweep(spec: str) -> tuple[str, tuple[float, ...]]:
    """One ``--sweep`` argument: ``rule_j.BAND_TOLERANCE_PX=16,24,32,40``.

    Args:
        spec: The argument.

    Returns:
        The constant's name in :mod:`rule_j` and the values to try, each read as the constant's own type.

    Raises:
        SweepError: Not ``rule_j.<NAME>=<value>[,<value>...]``, a constant rule J hasn't got, or a value of the
            wrong type.
    """
    name, _, values = spec.partition("=")
    module, _, constant = name.strip().partition(".")
    if module != "rule_j" or not constant or not values.strip():
        raise SweepError(f"--sweep wants rule_j.<NAME>=<value>[,<value>...], not {spec!r}")
    if constant != constant.upper() or not hasattr(rule_j, constant):
        raise SweepError(f"rule J has no constant named {constant}")
    kind = type(getattr(rule_j, constant))
    if kind not in (int, float):
        raise SweepError(f"rule_j.{constant} is a {kind.__name__}; only numbers can be swept")
    try:
        return constant, tuple(kind(value) for value in values.split(","))
    except ValueError as exc:
        raise SweepError(f"rule_j.{constant} takes {kind.__name__} values: {exc}") from None


def _bound_at_import(tree: ast.Module, reads: Callable[[ast.AST | None], bool], label: str) -> list[str]:
    """Every place in one parsed module that keeps a copy of what ``reads`` matches, taken while the module is imported.

    A patch reaches a name looked up inside a function body, because that lookup happens on every call. It does not
    reach a value read once while the module was being imported, wherever the reading sits, so those are what this
    finds: default arguments, decorator arguments, and assignments (plain, annotated, augmented or walrus) in any
    block that runs at import -- module scope, a class body, an ``if``, a ``try`` body *or its handlers*, a ``with``,
    a ``for``, a ``match`` case. Function bodies are skipped, which is the whole point; a nested function's defaults
    are reported anyway, which refuses a sweep that would in fact have worked rather than measuring a wrong one.

    Args:
        tree: The parsed module.
        reads: True for an expression that reads the constant.
        label: How the module is named in the lines returned.

    Returns:
        One line per place, without repeats.
    """
    found: list[str] = []

    def at_import(body: Sequence[ast.stmt]) -> Iterator[ast.stmt]:
        for stmt in body:
            if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            yield stmt
            for child in ast.iter_child_nodes(stmt):
                if isinstance(child, ast.stmt):
                    yield from at_import([child])
                elif isinstance(child, ast.ExceptHandler | ast.match_case):
                    yield from at_import(child.body)

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) and any(
            reads(default) for default in (*node.args.defaults, *node.args.kw_defaults)
        ):
            found.append(f"{label}.{getattr(node, 'name', '<lambda>')}() binds it as a default argument, "
                         "read once at import")  # fmt: skip
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) and any(
            reads(decorator) for decorator in node.decorator_list
        ):
            found.append(f"{label}.{node.name}'s decorator is given it at import")

    for stmt in at_import(tree.body):
        target = None
        if isinstance(stmt, ast.Assign) and reads(stmt.value):
            target = stmt.targets[0]
        elif isinstance(stmt, ast.AnnAssign | ast.AugAssign) and reads(stmt.value):
            target = stmt.target
        if target is not None:
            found.append(f"{label}.{ast.unparse(target)} is built from it at import")
        for node in ast.walk(stmt):
            if isinstance(node, ast.NamedExpr) and reads(node.value):
                found.append(f"{label}.{ast.unparse(node.target)} is built from it at import")
    return list(dict.fromkeys(found))


def unreachable_by_patch(constant: str, source: str | None = None) -> list[str]:
    """Where setting ``rule_j.<constant>`` would **not** reach, so a sweep of it would measure the shipped value.

    A sweep sets the module attribute, and rule J reads its constants in function bodies -- a fresh lookup on every
    call, which a patch reaches. Two things break that, and a published band sweep was silently wrong for the first
    of them (``evidence/eval/phase3-harness.md``, "What the architecture review changed": ``in_band``'s tolerance was
    bound at import, so sweeping ``BAND_TOLERANCE_PX`` reached only ``same_roll``):

    * :mod:`rule_j` itself taking a copy while it is imported -- a default argument, a decorator's argument, or an
      assignment in any block that runs at import (:func:`_bound_at_import` lists them);
    * another module of ours keeping its own copy: ``from .rule_j import <NAME>``, which gives it a module attribute
      of that name, or the same import-time shapes reading it as ``rule_j.<NAME>``.

    What it cannot see is a module of ours that has not been imported yet when the sweep starts, and anything outside
    ``media_preview_generator`` and ``tools.markers_eval``.

    Args:
        constant: The constant's name in :mod:`rule_j`.
        source: Python source to read as :mod:`rule_j`'s instead of its own (the tests' shapes).

    Returns:
        One line per place a patch wouldn't reach; empty when setting the attribute is the whole story.
    """

    def reads_here(node: ast.AST | None) -> bool:
        return node is not None and any(isinstance(n, ast.Name) and n.id == constant for n in ast.walk(node))

    tree = ast.parse(Path(rule_j.__file__).read_text() if source is None else source)
    escapes = _bound_at_import(tree, reads_here, "rule_j")
    ours = ("media_preview_generator", "tools.markers_eval")
    for name, module in sorted(sys.modules.items()):
        if module is None or module is rule_j or not name.startswith(ours):
            continue
        held = vars(module) if hasattr(module, "__dict__") else {}
        if constant in held:
            escapes.append(f"{name} has its own {constant}: it imported the value, not the module")
            continue
        aliases = {alias for alias, value in held.items() if value is rule_j}
        path = getattr(module, "__file__", None)
        if not aliases or not isinstance(path, str) or not Path(path).is_file():
            continue

        def reads_there(node: ast.AST | None, aliases: set[str] = aliases) -> bool:
            return node is not None and any(
                isinstance(n, ast.Attribute) and n.attr == constant and isinstance(n.value, ast.Name)
                and n.value.id in aliases
                for n in ast.walk(node)
            )  # fmt: skip

        escapes.extend(_bound_at_import(ast.parse(Path(path).read_text()), reads_there, name))
    return escapes


@dataclass(frozen=True)
class SweepCell:
    """One combination of swept values and what the sets say about it.

    Attributes:
        values: The value each swept constant was set to for this cell.
        shipped: Every constant is at the value the tree ships.
        rule: Spec §5.4's metric for rule J alone on the 80 (all zero when the 80 weren't run).
        rows: Each group's rows (``80``, ``205``).
        decoded: Windows this cell had to decode for real, having not been read before.
        reused: Windows served from the decode cache.
    """

    values: tuple[tuple[str, float], ...]
    shipped: bool
    rule: RuleTally
    rows: dict[str, TextRows]
    decoded: int
    reused: int


def sweep_table(cells: Sequence[SweepCell]) -> str:
    """The cells as a markdown table, carrying the columns ``phase3-harness.md``'s sweep tables carry.

    The published tables are written from this one by hand: their constant column is prose rather than the
    attribute's name, and they leave out the decode count, which says how much of a cell was measured rather
    than what it answered.

    Args:
        cells: The cells, in the order they were run; at least one.

    Returns:
        The table, one row per cell, with the shipped cell's values in bold.
    """
    names = [name for name, _ in cells[0].values]
    header = [*names, "80: rule J / Medium useful / wrong", "205: Medium useful / wrong",
              "205: alone useful / wrong", "decoded / reused"]  # fmt: skip
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * len(header)) + "|"]
    for cell in cells:
        eighty, two_oh_five = cell.rows.get("80"), cell.rows.get("205")
        row = [f"**{value}**" if cell.shipped else f"{value}" for _, value in cell.values]
        row.append(
            "-" if eighty is None else f"{cell.rule.within_10s} / {eighty.medium['useful']} / {eighty.medium['wrong']}"
        )
        row.append("-" if two_oh_five is None else f"{two_oh_five.medium['useful']} / {two_oh_five.medium['wrong']}")
        row.append("-" if two_oh_five is None else f"{two_oh_five.text['useful']} / {two_oh_five.text['wrong']}")
        row.append(f"{cell.decoded} / {cell.reused}")
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _sweep_answer(
    path: str,
    *,
    is_episode: bool,
    decode: str,
    gpu_device: str,
    ffmpeg: str,
    detect_boxes: Callable[[np.ndarray], list[tuple[rule_j.Box, ...]]],
    probe: Callable[[str], MediaProbe],
    decodes: DecodeCache,
) -> tuple[float | None, float | None]:
    """One file's ``(start, end)`` for the constants rule J is set to right now, from the decode cache.

    A GPU decode the card can't do is read again on the CPU, exactly as :meth:`CreditsTextCache.result` does it, so a
    swept cell's population is the reported run's population.
    """
    gpu = "NVIDIA" if decode == "gpu" else None
    duration_ms = probe(path).duration_ms
    earliest_s = _earliest_start_s(duration_ms, is_episode=is_episode)
    with decodes.serving():
        try:
            found = find_credits(path, duration_ms=duration_ms, is_episode=is_episode, ffmpeg=ffmpeg,
                                 detect_boxes=detect_boxes, gpu=gpu, gpu_device_path=gpu_device if gpu else None,
                                 earliest_start_s=earliest_s)  # fmt: skip
        except GpuDecodeError:
            found = find_credits(path, duration_ms=duration_ms, is_episode=is_episode, ffmpeg=ffmpeg,
                                 detect_boxes=detect_boxes, gpu=None, gpu_device_path=None,
                                 earliest_start_s=earliest_s)  # fmt: skip
    return found.start_s, found.end_s


def sweep_credits_text(
    *,
    decode: str,
    gpu_device: str,
    sets: tuple[str, ...],
    specs: Sequence[str],
    cache_root: Path,
    ffmpeg: str,
    ffprobe: str,
    baseline_path: Path,
) -> tuple[dict, list[SweepCell]]:
    """Rule J's own constants, swept over the gate's sets with the **whole rule** live on every cell.

    Each cell sets the constants on :mod:`rule_j` and runs the app's own ``find_credits`` over every file of the
    chosen sets through the decode cache, so a cell costs rule J's own microseconds per file plus a stored-rows read
    -- unless the swept value leaves a file with no answer at 320x180 that no earlier run left without one: its tail is
    then decoded at 640x360 (``detector.RETRY_SCALE``) once, and stored like any decode. The answer cache is
    deliberately not used: its key carries the detector's *source* digest, which a swept attribute doesn't move, so it
    would serve the shipped cell's answers to every cell.

    Several ``--sweep`` arguments are a cross product, which is the shape the band's two numbers were tabled in.

    Args:
        decode: ``gpu`` or ``cpu``.
        gpu_device: The GPU worker's device (``cuda:0``).
        sets: Which of ``80`` and ``205`` to run.
        specs: ``--sweep`` arguments (:func:`parse_sweep`).
        cache_root: Where probes and decodes are cached (never under /data*).
        ffmpeg: ffmpeg binary.
        ffprobe: ffprobe binary.
        baseline_path: Plex's markers.

    Returns:
        A summary (the table and the run's own facts) and the cells.

    Raises:
        UnknownSetError: ``sets`` names a set this harness doesn't have.
        SweepError: A spec is malformed, or names a constant a patch wouldn't reach. Both are checked before
            anything is measured: a sweep that silently measures the shipped value in every cell is worse than no
            sweep, and one has already been published (``phase3-harness.md``).
    """
    unknown = sorted(set(sets) - set(GATE_SETS))
    if unknown:
        raise UnknownSetError(f"unknown set(s): {unknown}; choose from {sorted(GATE_SETS)}")
    if not specs:
        # One cell with nothing set is a table of the shipped run wearing a sweep's clothes.
        raise SweepError("a sweep needs at least one rule_j.<NAME>=<value>[,<value>...]")
    swept = [parse_sweep(spec) for spec in specs]
    escapes = [line for constant, _ in swept for line in unreachable_by_patch(constant)]
    if escapes:
        raise SweepError("setting the attribute wouldn't reach every read: " + "; ".join(escapes))
    evidence = evidence_dir()
    adjudicated = json.loads((evidence / "credits/adjudicated.json").read_text())
    probes = ProbeCache(cache_root, ffprobe=ffprobe)
    baseline = load_baseline(baseline_path)
    kinds_of = {"movies40": True, "tv40": False, "movie_credit_truth": True}
    files_of = {
        name: json.loads((evidence / f"credits/{name}.json").read_text()) for group in sets for name in GATE_SETS[group]
    }
    original = {constant: getattr(rule_j, constant) for constant, _ in swept}
    detection = _detection_on(decode, gpu_device)
    cells: list[SweepCell] = []
    try:
        # One blank frame starts the helper and its self-test, so the decode cache knows which backend read the boxes.
        detection.detect_boxes(np.zeros((1, FRAME_H, FRAME_W), dtype=np.uint8))
        decodes = DecodeCache(cache_root, digest=decode_digest(), backend=detection.backend)
        for combination in itertools.product(*(values for _, values in swept)):
            values = tuple(zip((constant for constant, _ in swept), combination, strict=True))
            for constant, value in values:
                setattr(rule_j, constant, value)
            counted = (decodes.decoded, decodes.reused)
            rule, rows = RuleTally(), {}
            # The 40 movies of the 80 are all in the 205, so a cell reads them once and both sets see that answer.
            cell_answers: dict[tuple[str, bool], tuple[float | None, float | None]] = {}
            for group in (g for g in ("80", "205") if g in sets):
                parts = []
                for name in GATE_SETS[group]:
                    is_movie = kinds_of[name]
                    answers = {}
                    for entry in files_of[name]:
                        key = (entry["file"], not is_movie)
                        if key not in cell_answers:
                            cell_answers[key] = _sweep_answer(
                                entry["file"], is_episode=not is_movie, decode=decode, gpu_device=gpu_device,
                                ffmpeg=ffmpeg, detect_boxes=detection.detect_boxes, probe=probes.probe,
                                decodes=decodes,
                            )  # fmt: skip
                        answers[entry["file"]] = cell_answers[key]
                        if group == "80":
                            rule.add(answers[entry["file"]][0], _truth(entry, adjudicated))
                    parts.append(
                        compare_text(
                            files_of[name],
                            adjudicated,
                            answers=answers,
                            probe=probes.probe,
                            baseline=baseline,
                            is_movie=is_movie,
                        )  # fmt: skip
                    )
                rows[group] = merge_rows(parts)
            cells.append(
                SweepCell(
                    values=values,
                    shipped=all(value == original[constant] for constant, value in values),
                    rule=rule,
                    rows=rows,
                    decoded=decodes.decoded - counted[0],
                    reused=decodes.reused - counted[1],
                )
            )
            logger.info("swept {}: {}", dict(values), sweep_table(cells[-1:]).splitlines()[-1])
    finally:
        for constant, value in original.items():
            setattr(rule_j, constant, value)
        backend = detection.backend()
        detection.close()
    return {
        "decode": decode,
        "decode_digest": decode_digest(),
        "detector_digest": detector_digest(),
        "text_detection": backend,
        "sets": sorted(sets),
        "swept": list(specs),
        "cells": len(cells),
        "table": sweep_table(cells),
    }, cells
