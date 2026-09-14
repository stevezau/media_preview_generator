"""TV intro eval: the v3 matcher on the 118 episodes with studio-chapter truth (spec §5.3)."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from media_preview_generator.markers.audio import POINT_S
from media_preview_generator.markers.audio.matcher import season_intros
from media_preview_generator.markers.external_ids import ids_from_path, is_extra
from media_preview_generator.plex_client import VIDEO_EXTENSIONS

from . import fp3_reference
from .data import EvalEpisode, by_season
from .score import Tally, judge_intro

SPEC_V3 = (91, 13, 14)
# Stored segments are rounded floats from another ffmpeg build; two points either way is the same answer.
DRIFT_TOLERANCE_S = 2 * POINT_S


@dataclass
class ReproductionReport:
    """What the gate found."""

    tally: Tally = field(default_factory=Tally)
    port_vs_reference: list[dict] = field(default_factory=list)
    drift: list[dict] = field(default_factory=list)
    seasons: int = 0
    episodes: int = 0

    @property
    def passed(self) -> bool:
        """Port equals the reference everywhere and the tally is at least the spec's numbers."""
        return not self.port_vs_reference and self.tally.at_least(SPEC_V3[0], SPEC_V3[1])


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
    """Every episode file in the group's folder, the eval's own files included (spec §5.3: group = season folder).

    Mirrors the plan's Task 7 group rule; switch to ``markers.audio.season.season_group`` once that lands.
    """
    folder = os.path.dirname(group[0].file)
    on_disk = {
        path
        for path in (os.path.join(folder, name) for name in os.listdir(folder))
        if os.path.splitext(path)[1].lower() in VIDEO_EXTENSIONS
        and not is_extra(path)
        and ids_from_path(path).is_episode
        and os.path.isfile(path)
    }
    return sorted(on_disk | {e.file for e in group})


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
            if e.truth_intro is not None:
                report.tally.add(judge_intro(got[:2] if got else None, e.truth_intro))
    return report
