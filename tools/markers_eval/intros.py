"""TV intro eval: the v3 matcher and the app's season step on the 118 episodes with studio-chapter truth (spec §5.3)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from media_preview_generator.markers.audio import POINT_S
from media_preview_generator.markers.audio.matcher import (
    MAX_INTRO_S,
    Run,
    file_hits,
    intro_for,
    pair_runs,
    season_intros,
)
from media_preview_generator.markers.audio.season import holds_no_intro, season_group, season_intro

from . import fp3_reference
from .data import EvalEpisode, by_season
from .score import Tally, judge_intro

SPEC_V3 = (91, 13, 14)
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
        silence_dropped: Matcher answers the silence guard dropped, with the matcher answer's verdict.
        seasons: Seasons run.
        episodes: Eval episodes judged or compared.
    """

    tally: Tally = field(default_factory=Tally)
    matcher_tally: Tally = field(default_factory=Tally)
    port_vs_reference: list[dict] = field(default_factory=list)
    drift: list[dict] = field(default_factory=list)
    skipped_pairs: list[dict] = field(default_factory=list)
    silence_dropped: list[dict] = field(default_factory=list)
    seasons: int = 0
    episodes: int = 0

    @property
    def passed(self) -> bool:
        """Port equals the reference, the season step's tally is at least the spec's numbers, no skipped pair held an
        intro-length run, and the silence guard dropped no useful answer."""
        return (
            not self.port_vs_reference
            and self.tally.at_least(SPEC_V3[0], SPEC_V3[1])
            and not any(pair["intro_length_runs"] for pair in self.skipped_pairs)
            and not any(dropped["verdict"] == "useful" for dropped in self.silence_dropped)
        )


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


def _season_folder_episodes(group: list[EvalEpisode]) -> list[str]:
    """The app's season group of the group's folder (spec §5.3: group = season folder), the eval's own files included."""
    return sorted(set(season_group(group[0].file).episodes) | {e.file for e in group})


class _SeasonStep:
    """The app's season step over one season's fingerprints, recording the pairs it skips."""

    def __init__(self, season: str, fps: dict[str, np.ndarray], report: ReproductionReport) -> None:
        self._season, self._fps, self._report = season, fps, report
        self._runs: dict[tuple[str, str], list[Run]] = {}
        self.files = sorted(f for f, points in fps.items() if len(points))

    def runs_between(self, first: str, second: str) -> list[Run]:
        if (first, second) not in self._runs:
            a, b = self._fps[first], self._fps[second]
            runs = pair_runs(a, b)
            if holds_no_intro(a, b):  # what markers.audio.season.season_pair_runs leaves out
                short = [r for r in runs if r.a_end_s - r.a_start_s <= MAX_INTRO_S]
                self._report.skipped_pairs.append(
                    {"season": self._season, "a": first, "b": second, "intro_length_runs": len(short)}
                )
                runs = []
            self._runs[(first, second)] = runs
        return self._runs[(first, second)]

    def answer(self, episode: EvalEpisode) -> tuple | None:
        if episode.file not in self.files or len(self.files) < 2:
            return None
        guarded = season_intro(episode.file, self.files, self._fps, self.runs_between)
        if guarded is None:
            unguarded = intro_for(file_hits(episode.file, self.files, self.runs_between), len(self.files) - 1)
            if unguarded is not None:
                verdict = judge_intro(unguarded[:2], episode.truth_intro) if episode.truth_intro else None
                self._report.silence_dropped.append(
                    {"season": self._season, "file": episode.file, "segment": tuple(unguarded), "verdict": verdict}
                )
        return _as_tuple(guarded)


def reproduce(
    episodes: list[EvalEpisode],
    *,
    points: Callable[[str], np.ndarray],
    with_reference: bool = True,
    full_folder: bool = False,
) -> ReproductionReport:
    """Run the port (and the reference) season by season and judge the eval episodes against their truth.

    Args:
        episodes: From ``load_v3_results``.
        points: Fingerprint of a file.
        with_reference: Also run the slow reference and compare exactly.
        full_folder: Match each season's whole folder (what the app does) instead of the eval's own file lists (at
            most 8 per season, how spec §5.3 was measured). Drift then also lists answers the larger group changed.

    Returns:
        The report.
    """
    report = ReproductionReport()
    for season, group in by_season(episodes).items():
        report.seasons += 1
        files = _season_folder_episodes(group) if full_folder else [e.file for e in group]
        fps = {f: points(f) for f in files}
        port = {f: _as_tuple(seg) for f, seg in season_intros(fps).items()}
        step = _SeasonStep(season, fps, report)
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
