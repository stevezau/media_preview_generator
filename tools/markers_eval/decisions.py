"""Our intro decisions on the 118 episodes against Plex's own markers for the same files (roadmap phase 2 gate).

Chapters are the truth on this set, so they can't also be a source, and no online answers are recorded for these
files. What's left is what a library without chapters or online answers gets: season audio (the app's season step)
and Plex's own intro marker as ``server_markers``, decided by the real ``decide()`` at High and Medium.

Two rulings shape the rows. R2 (until 2026-09-24): season audio never decided alone; since then it decides an intro
alone at Medium when nothing else answers (owner, on these 118 episodes' numbers). G3 (precision first): season audio
and a server's own marker never decide together. With G3 on (shipped), an episode Plex also answers publishes nothing
here, so every row is also reported with G3 off (season audio and a server's own marker count as two agreeing
independent sources), for the owner to rule on G3 with numbers. G3 off never changes the shipped rule: ``g3_rule`` patches it for the
duration of a block only.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from unittest import mock

import numpy as np

from media_preview_generator.markers import decide as decide_module
from media_preview_generator.markers.decide import DecisionContext, DecisionStatus, decide
from media_preview_generator.markers.models import SERVER_SOURCES, Candidate, MarkerType, Source

from .data import EvalEpisode, by_season, episode_label
from .intros import ReproductionReport, SeasonStep, season_folder_episodes
from .plex import PlexMarker, first_marker, server_candidates
from .score import Tally, judge_intro, skips_story

# The app's order with every source on (settings.DEFAULT_GLOBAL_MARKERS plus the pipeline's riders).
ORDER = ("chapters", "theintrodb", "introdb", "skipdb", "season_audio", "season_audio_previous", "credits_text",
         "server_markers", "server_markers_imported")  # fmt: skip
ROW_NAMES = ("plex", "audio", "high", "medium")


@contextmanager
def g3_rule(enabled: bool) -> Iterator[None]:
    """Decide with ruling G3 on (the shipped rule) or off inside the block.

    Off restores the rule before G3: a set of agreeing candidates needs one that isn't a server marker, so season
    audio and a server's own marker can decide together (``decide._agreeing_cliques``).

    Args:
        enabled: True leaves the shipped rule alone.

    Yields:
        Nothing.
    """
    if enabled:
        yield
        return
    with mock.patch.object(decide_module, "_AUDIO_OR_SERVER", SERVER_SOURCES):
        yield


def audio_candidate(segment: tuple[float, float, int, int]) -> Candidate:
    """The season step's answer as the app stores it (``markers/audio/season.py`` ``_candidate``).

    Args:
        segment: (start_s, end_s, support, others).

    Returns:
        The ``season_audio`` candidate.
    """
    start_s, end_s, support, others = segment
    return Candidate(MarkerType.INTRO, int(round(start_s * 1000)), int(round(end_s * 1000)), Source.SEASON_AUDIO,
                     support / others, f"{support}/{others}")  # fmt: skip


@dataclass
class DecisionRows:
    """useful/wrong/missed for Plex's own intro markers, season audio alone, and our decisions at High and Medium.

    Attributes:
        skips_story: Per row name, the wrong answers that skip story (``score.skips_story``).
        episodes: Per judged episode: file, label, each row's verdict, and our decided segments and reasons.
    """

    plex: Tally = field(default_factory=Tally)
    audio: Tally = field(default_factory=Tally)
    high: Tally = field(default_factory=Tally)
    medium: Tally = field(default_factory=Tally)
    skips_story: Counter = field(default_factory=Counter)
    episodes: list[dict] = field(default_factory=list)

    def gate(self) -> bool:
        """Medium at least as good as Plex (as useful or more, no more wrong) and High no more wrong than Plex."""
        return self.medium.at_least(self.plex.useful, self.plex.wrong) and self.high.wrong <= self.plex.wrong

    def as_dict(self) -> dict[str, dict[str, int]]:
        """Each row's counts, with its wrong answers that skip story."""
        return {name: {**getattr(self, name).as_dict(), "skips_story": self.skips_story[name]} for name in ROW_NAMES}


def season_segments(
    episodes: list[EvalEpisode], *, points: Callable[[str], np.ndarray], full_folder: bool
) -> dict[str, tuple[float, float, int, int] | None]:
    """The app's season step per eval episode: (start_s, end_s, support, others) or None.

    Args:
        episodes: The eval episodes.
        points: Fingerprint of a file.
        full_folder: Match against the app's season group of each folder (``season_group``) instead of the eval's own
            file lists (at most 8 per season, how spec §5.3 was measured).

    Returns:
        By eval file path.
    """
    out: dict[str, tuple[float, float, int, int] | None] = {}
    for season, group in by_season(episodes).items():
        files = season_folder_episodes(group) if full_folder else [e.file for e in group]
        step = SeasonStep(season, {f: points(f) for f in files}, ReproductionReport())
        others = len(step.files) - 1
        for e in group:
            seg = step.answer(e)
            out[e.file] = (seg[0], seg[1], seg[2], others) if seg else None
    return out


def _add(rows: DecisionRows, name: str, segment: tuple[float, float] | None, truth: tuple[float, float]) -> str:
    verdict = judge_intro(segment, truth)
    getattr(rows, name).add(verdict)
    if verdict == "wrong" and skips_story(segment, truth):
        rows.skips_story[name] += 1
    return verdict


def compare_with_plex(
    episodes: list[EvalEpisode],
    segments: dict[str, tuple[float, float, int, int] | None],
    baseline: dict[str, list[PlexMarker]],
    *,
    g3: bool = True,
) -> DecisionRows:
    """Judge Plex's intro, season audio alone, and ``decide()`` on both at High and Medium, per episode with a truth.

    Args:
        episodes: The eval episodes (those without an intro truth or a duration are skipped).
        segments: ``season_segments``.
        baseline: Plex's markers by file path.
        g3: Ruling G3 on (shipped) or off.

    Returns:
        The rows.
    """
    rows = DecisionRows()
    for e in episodes:
        if e.truth_intro is None or e.duration_s is None:
            continue
        markers = baseline.get(e.file, [])
        plex = first_marker(markers, MarkerType.INTRO)
        seg = segments.get(e.file)
        detail = {"file": e.file, "label": episode_label(e.file), "truth": e.truth_intro,
                  "plex_segment": (plex.start_ms / 1000, plex.end_ms / 1000) if plex else None,
                  "audio_segment": seg}  # fmt: skip
        detail["plex"] = _add(rows, "plex", detail["plex_segment"], e.truth_intro)
        detail["audio"] = _add(rows, "audio", seg[:2] if seg else None, e.truth_intro)
        candidates = server_candidates(markers, MarkerType.INTRO)
        if seg:
            candidates.append(audio_candidate(seg))
        for level in ("high", "medium"):
            ctx = DecisionContext(round(e.duration_s * 1000), False, level, frozenset({MarkerType.INTRO}), ORDER)
            with g3_rule(g3):
                d = decide(candidates, ctx, {})[MarkerType.INTRO]
            decided = (d.marker.start_ms / 1000, d.marker.end_ms / 1000) if d.status is DecisionStatus.DECIDED else None
            detail[level] = _add(rows, level, decided, e.truth_intro)
            detail[f"{level}_segment"], detail[f"{level}_reason"] = decided, d.reason
        rows.episodes.append(detail)
    return rows


def g3_differences(on: DecisionRows, off: DecisionRows) -> list[dict]:
    """Episodes whose High or Medium outcome differs between G3 on and off, with the G3-off verdict.

    Args:
        on: Rows with G3 on.
        off: The same episodes with G3 off.

    Returns:
        One entry per changed episode (file, label, G3-on and G3-off High/Medium verdicts, Plex's verdict).
    """
    before = {row["file"]: row for row in on.episodes}
    out = []
    for row in off.episodes:
        old = before[row["file"]]
        if (old["high"], old["medium"]) != (row["high"], row["medium"]):
            out.append({"file": row["file"], "label": row["label"], "plex": row["plex"], "audio": row["audio"],
                        "g3_on": old["medium"], "g3_off": row["medium"], "g3_off_high": row["high"],
                        "g3_off_segment": row["medium_segment"], "truth": row["truth"]})  # fmt: skip
    return out
