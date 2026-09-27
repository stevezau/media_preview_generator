"""TV intro eval: the v3 matcher and the app's season step on the 118 episodes with studio-chapter truth (spec §5.3),
and the season step on any other intro truth set (``season_truth``: the Accused set of §14 2026-09-24).

The season step's end-picture check decodes the real files (``DecodedEndPictures``); the app caches its shares in
markers.db, the harness for the run.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

from media_preview_generator.markers.audio import POINT_S, end_picture
from media_preview_generator.markers.audio.matcher import (
    MAX_INTRO_S,
    IntroCandidate,
    Run,
    file_hits,
    intro_for,
    pair_runs,
    season_intros,
)
from media_preview_generator.markers.audio.season import (
    SeasonClock,
    _mostly_silence,
    clock_by_audio,
    guarded_pick,
    heard_in,
    holds_no_intro,
    in_own_time,
    in_own_times,
    season_clock,
    season_group,
)

from . import fp3_reference
from .data import EvalEpisode, by_season
from .score import Tally, judge_intro

# The v3 matcher alone (spec §5.3), and the season step with its guards against idents and music beds (§14 2026-09-24).
SPEC_V3 = (91, 13, 14)
SPEC_SEASON_STEP = (91, 12, 15)
# Stored segments are rounded floats from another ffmpeg build; two points either way is the same answer.
DRIFT_TOLERANCE_S = 2 * POINT_S


@dataclass
class ReproductionReport:
    """What the gate found.

    Attributes:
        tally: The season step's answers (``markers.audio.season``: the matcher plus the silence guard and skipped
            pairs), which is what the app stores as season audio evidence.
        matcher_tally: The v3 matcher's answers alone.
        port_vs_reference: Episodes where the numpy port and the pure-Python reference differ.
        drift: Matcher answers that differ from the stored v3 segments by more than two points.
        skipped_pairs: Pairs the season step left unmatched, with how many intro-length runs (<= 120 s) the matcher
            finds between them (any is a deviation the gate refuses).
        silence_dropped: Answers the silence guard dropped, with the dropped answer's verdict.
        guards_changed: Matcher answers the guards against idents and music beds passed over, with the matcher's and
            the season step's answers and verdicts (reported: the tally gate judges them).
        seasons: Seasons run.
        episodes: Eval episodes judged or compared.
    """

    tally: Tally = field(default_factory=Tally)
    matcher_tally: Tally = field(default_factory=Tally)
    port_vs_reference: list[dict] = field(default_factory=list)
    drift: list[dict] = field(default_factory=list)
    skipped_pairs: list[dict] = field(default_factory=list)
    silence_dropped: list[dict] = field(default_factory=list)
    guards_changed: list[dict] = field(default_factory=list)
    seasons: int = 0
    episodes: int = 0

    @property
    def passed(self) -> bool:
        """Port equals the reference, the season step's tally is at least its spec numbers, no skipped pair held an
        intro-length run, and the silence guard dropped no useful answer."""
        return (
            not self.port_vs_reference
            and self.tally.at_least(SPEC_SEASON_STEP[0], SPEC_SEASON_STEP[1])
            and not any(pair["intro_length_runs"] for pair in self.skipped_pairs)
            and not any(dropped["verdict"] == "useful" for dropped in self.silence_dropped)
        )


class EndPictures(Protocol):
    """The season step's end-picture check, per episode (``season._EndPictures`` in the app)."""

    def for_episode(self, target: str) -> Callable[[IntroCandidate], bool]:
        """The check of one episode's clusters."""
        ...


class DecodedEndPictures:
    """The end-picture check on the real files, each share measured once per run."""

    def __init__(self, reader: end_picture.Reader) -> None:
        """Wrap a reader.

        Args:
            reader: Decodes on the harness's GPU or CPU.
        """
        self._reader = reader
        self._shares: dict[tuple[str, str, float, float, float], float | None] = {}

    def for_episode(self, target: str) -> Callable[[IntroCandidate], bool]:
        """The check of one episode's clusters (a partner gone from disk isn't compared)."""

        def passes(candidate: IntroCandidate) -> bool:
            segment = candidate.segment
            shares = []
            for hit in end_picture.partners([h for h in candidate.members if os.path.exists(h.partner)]):
                offset_s = hit.partner_start_s - hit.start_s
                key = (target, hit.partner, segment.start_s, segment.end_s, offset_s)
                if key not in self._shares:
                    self._shares[key] = self._reader.share(
                        target, hit.partner, segment.start_s, segment.end_s, offset_s
                    )
                shares.append(self._shares[key])
            return end_picture.passes(shares)

        return passes


def _as_tuple(segment: tuple | None) -> tuple | None:
    return tuple(segment) if segment else None


def _drifted(stored: tuple[float, float, int] | None, got: tuple | None) -> bool:
    if (stored is None) != (got is None):
        return True
    if stored is None:
        return False
    return (
        abs(stored[0] - got[0]) > DRIFT_TOLERANCE_S
        or abs(stored[1] - got[1]) > DRIFT_TOLERANCE_S
        or stored[2] != got[2]
    )


def season_folder_episodes(group: list[EvalEpisode]) -> list[str]:
    """The app's season group of the group's folder (spec §5.3: group = season folder), the eval's own files included."""
    return sorted(set(season_group(group[0].file).episodes) | {e.file for e in group})


# A file's playback speed (``speed.playback_speed`` of its frame rate) and its fingerprint retimed by a factor.
Speed = Callable[[str], float | None]
Retimed = Callable[[str, float], np.ndarray]


class SeasonStep:
    """The app's season step over one season's fingerprints, recording the pairs it skips and what its guards change.

    With ``speed`` and ``retimed``, a season whose files play at two speeds (25 fps and film-rate releases) is matched
    at one, as the app does (``season.SeasonClock``): each file at the other speed on its retimed fingerprint, its
    answer read back at its own speed.
    """

    def __init__(
        self,
        season: str,
        fps: dict[str, np.ndarray],
        report: ReproductionReport,
        end_pictures: EndPictures,
        *,
        speed: Speed | None = None,
        retimed: Retimed | None = None,
    ) -> None:
        self._season, self._report, self._end_pictures = season, report, end_pictures
        self._runs: dict[tuple[str, str, int], list[Run]] = {}
        audible = {f: points for f, points in fps.items() if len(points)}
        self.files = sorted(audible)
        self.clock = SeasonClock()
        self._fps = dict(fps)
        if speed and retimed:
            speeds = {f: speed(f) for f in audible}
            by_rate = season_clock(speeds)
            stretched = {f: retimed(f, factor) for f, factor in by_rate.factors.items()}

            def heard(path: str, retimed_side: bool, reference: str) -> bool:
                # As the app does (season._matching): matched both ways against the files at the group's speed.
                own = stretched[path] if retimed_side else fps[path]
                first = path < reference
                side = SeasonClock(by_rate.speed, {path: 1.0} if retimed_side else {})
                pair = (path, reference) if first else (reference, path)
                runs = self._pair(*pair, side, {path: own, reference: fps[reference]})
                return heard_in(runs, own, first=first)

            references = [f for f in self.files if speeds[f] == by_rate.speed]
            self.clock = clock_by_audio(by_rate, references, heard)
            self._fps.update({f: stretched[f] for f in self.clock.factors})

    def runs_between(self, first: str, second: str) -> list[Run]:
        return self._pair(first, second, self.clock, self._fps)

    def _pair(self, first: str, second: str, clock: SeasonClock, fps: Mapping[str, np.ndarray]) -> list[Run]:
        key = (first, second, clock.pair_version(first, second))
        if key not in self._runs:
            a, b = fps[first], fps[second]
            runs = pair_runs(a, b)
            if holds_no_intro(a, b):  # what markers.audio.season.season_pair_runs leaves out
                short = [r for r in runs if r.a_end_s - r.a_start_s <= MAX_INTRO_S]
                self._report.skipped_pairs.append(
                    {"season": self._season, "a": first, "b": second, "intro_length_runs": len(short)}
                )
                runs = []
            self._runs[key] = runs
        return self._runs[key]

    def answer(self, episode: EvalEpisode) -> tuple | None:
        if episode.file not in self.files or len(self.files) < 2:
            return None
        factors = self.clock.factors
        passes = self._end_pictures.for_episode(episode.file)
        picked = guarded_pick(
            episode.file,
            self.files,
            self._fps,
            self.runs_between,
            end_picture_passes=lambda candidate: passes(in_own_times(candidate, episode.file, factors)),
        )
        matcher = intro_for(file_hits(episode.file, self.files, self.runs_between), len(self.files) - 1)
        silent = picked is not None and _mostly_silence(self._fps[episode.file], picked)
        own = factors.get(episode.file)
        picked, matcher = (None if found is None else in_own_time(found, own) for found in (picked, matcher))
        truth = episode.truth_intro
        if _as_tuple(picked) != _as_tuple(matcher):
            self._report.guards_changed.append({
                "season": self._season, "file": episode.file,
                "matcher": _as_tuple(matcher), "matcher_verdict": _verdict(matcher, truth),
                "step": _as_tuple(picked), "step_verdict": _verdict(picked, truth),
            })  # fmt: skip
        if silent:
            self._report.silence_dropped.append(
                {
                    "season": self._season,
                    "file": episode.file,
                    "segment": tuple(picked),
                    "verdict": _verdict(picked, truth),
                }
            )
            return None
        return _as_tuple(picked)


def _verdict(segment: tuple | None, truth: tuple[float, float] | None) -> str | None:
    return judge_intro(segment[:2] if segment else None, truth) if truth else None


def reproduce(
    episodes: list[EvalEpisode],
    *,
    points: Callable[[str], np.ndarray],
    end_pictures: EndPictures,
    with_reference: bool = True,
    full_folder: bool = False,
    speed: Speed | None = None,
    retimed: Retimed | None = None,
) -> ReproductionReport:
    """Run the port (and the reference) season by season and judge the eval episodes against their truth.

    Args:
        episodes: From ``load_v3_results``.
        points: Fingerprint of a file.
        end_pictures: The season step's end-picture check (:class:`DecodedEndPictures` on real files).
        with_reference: Also run the slow reference and compare exactly.
        full_folder: Match each season's whole folder (what the app does) instead of the eval's own file lists (at
            most 8 per season, how spec §5.3 was measured). Drift then also lists answers the larger group changed.
        speed: A file's playback speed; with ``retimed``, the season step matches a season at one speed (the port and
            the reference always match every file as it plays).
        retimed: A file's fingerprint retimed by a factor.

    Returns:
        The report.
    """
    report = ReproductionReport()
    for season, group in by_season(episodes).items():
        report.seasons += 1
        files = season_folder_episodes(group) if full_folder else [e.file for e in group]
        fps = {f: points(f) for f in files}
        port = {f: _as_tuple(seg) for f, seg in season_intros(fps).items()}
        step = SeasonStep(season, fps, report, end_pictures, speed=speed, retimed=retimed)
        if with_reference:
            reference = fp3_reference.analyse_points(fps, sorted(fps))
            for f in sorted(fps):
                if port[f] != _as_tuple(reference[f]["segment"]):
                    report.port_vs_reference.append({"season": season, "file": f, "port": port[f],
                                                     "reference": reference[f]["segment"]})  # fmt: skip
        for e in group:
            report.episodes += 1
            got = port[e.file]
            if _drifted(e.v3_segment, got):
                report.drift.append({"season": season, "file": e.file, "stored": e.v3_segment, "now": got})
            answer = step.answer(e)
            if e.truth_intro is not None:
                report.matcher_tally.add(judge_intro(got[:2] if got else None, e.truth_intro))
                report.tally.add(judge_intro(answer[:2] if answer else None, e.truth_intro))
    return report


@dataclass
class TruthReport:
    """The season step on an intro truth set (:func:`season_truth`).

    Attributes:
        tally: Useful / wrong / missed over the files with an intro, and a wrong for each answer where there is none.
        none_ok: Files without an intro in the truth that got none.
        details: Per file: the season step's answer, the truth and the verdict.
    """

    tally: Tally = field(default_factory=Tally)
    none_ok: int = 0
    details: dict[str, dict] = field(default_factory=dict)


def season_truth(
    truth: Mapping[str, tuple[float, float] | None],
    *,
    points: Callable[[str], np.ndarray],
    end_pictures: EndPictures,
    speed: Speed | None = None,
    retimed: Retimed | None = None,
) -> TruthReport:
    """The app's season step on each truth file, matched with its season group (``season_group``, as the app does).

    Args:
        truth: Intro (start, end) seconds per file path, None for a file with no intro (a wrong answer if it gets one).
        points: Fingerprint of a file.
        end_pictures: The season step's end-picture check.
        speed: A file's playback speed; with ``retimed``, a season is matched at one speed (:class:`SeasonStep`).
        retimed: A file's fingerprint retimed by a factor.

    Returns:
        The report.
    """
    report = TruthReport()
    groups: dict[tuple[str, ...], list[str]] = {}
    for path in sorted(truth):
        groups.setdefault(season_group(path).episodes, []).append(path)
    for episodes, paths in groups.items():
        files = sorted(set(episodes) | set(paths))
        step = SeasonStep(os.path.dirname(paths[0]), {f: points(f) for f in files}, ReproductionReport(), end_pictures,
                          speed=speed, retimed=retimed)  # fmt: skip
        for path in paths:
            answer = step.answer(EvalEpisode(os.path.dirname(path), path, truth[path], None, None))
            if truth[path] is None:
                verdict = "wrong" if answer else "none-ok"
            else:
                verdict = judge_intro(answer[:2] if answer else None, truth[path])
            if verdict == "none-ok":
                report.none_ok += 1
            else:
                report.tally.add(verdict)
            report.details[path] = {"answer": answer, "truth": truth[path], "verdict": verdict}
    return report
