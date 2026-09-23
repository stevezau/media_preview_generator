"""Credits from chapters (spec §5.1 rules) against hand-checked truth, the chapter titles those rules miss, and
Plex's own credits markers on the same files.

The truth of both credits sets is the files' last credits chapter (3 movies corrected by frame checks,
``credits/adjudicated.json``), so chapters are nearly truth by construction here: these rows measure what the chapter
rules and rule 7 (a server's own later credits start shortens ours) do to it, and how Plex's own markers compare.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from media_preview_generator.markers.decide import DecisionContext, DecisionStatus, decide, shortened_by
from media_preview_generator.markers.models import MarkerType
from media_preview_generator.markers.probe import MediaProbe
from media_preview_generator.markers.sources.chapters import chapter_candidates, classify_chapter_title

from .data import without_ids
from .decisions import ORDER
from .plex import PlexMarker, first_marker, server_candidates

EARLY_S = 10.0
LATE_S = 30.0
_ENDING_RE = re.compile(r"\bending\b", re.I)
_SEASON_FOLDER_RE = re.compile(r"^((season|series|staffel|saison)\s*\d+|specials)$", re.I)


@dataclass
class CreditsReport:
    """Counts plus the files worth a human look (named by their folder, never a path)."""

    tally: Counter = field(default_factory=Counter)
    titles_missed: list[str] = field(default_factory=list)
    several_credits: list[str] = field(default_factory=list)
    ending_titles: list[str] = field(default_factory=list)


@dataclass
class CreditsRows:
    """Plex's first credits marker, the chapters decided alone, and our decision (chapters plus Plex's markers) at High
    and Medium. ``chapters`` against ``high`` is what rule 7 (a server's own later credits start) changes.

    Attributes:
        files: Per file: file, name, truth, Plex's start and credits marker count, the file's credits chapters and
            "Ending" titles, each decided start and its reason, and whether rule 7 shortened ours.
    """

    plex: Counter = field(default_factory=Counter)
    chapters: Counter = field(default_factory=Counter)
    high: Counter = field(default_factory=Counter)
    medium: Counter = field(default_factory=Counter)
    files: list[dict] = field(default_factory=list)


def _name(path: str) -> str:
    """The movie folder, or the show folder and season folder of an episode, without id tags."""
    folder = os.path.dirname(path)
    parent = os.path.basename(folder)
    if _SEASON_FOLDER_RE.match(parent):
        return f"{without_ids(os.path.basename(os.path.dirname(folder)))} {parent}"
    return without_ids(parent)


def _uncounted_ending(title: str, *, is_episode: bool) -> bool:
    """An "Ending" title the classifier still doesn't take for credits (ledger L165).

    Since phase 4 Task 15 a bare "Ending" is credits on an episode, so the kind has to be passed in or
    the ledger would keep listing titles the app now reads.
    """
    return (
        bool(_ENDING_RE.search(title))
        and classify_chapter_title(title, is_episode=is_episode) is not MarkerType.CREDITS
    )


def judge_credits(start_s: float | None, truth_s: float) -> str:
    """``missed`` (no answer), ``wrong`` (>10 s early: skips story), ``late`` (>30 s late) or ``useful``."""
    if start_s is None:
        return "missed"
    if start_s < truth_s - EARLY_S:
        return "wrong"
    return "late" if start_s > truth_s + LATE_S else "useful"


def title_coverage(movies: list[dict], *, is_episode: bool = False) -> CreditsReport:
    """Chapter titles only: does the title classifier see a credits chapter in each file's titles?

    Args:
        movies: Rows with ``file`` and ``chapters`` (titles; ``credits/movie_credit_truth.json`` holds the last six).
        is_episode: The rows are TV episodes, so the episode-only titles ("Ending") count as credits.

    Returns:
        found / not_found, the last title of each file without one, the files with several credits titles, and the
        files with an "Ending" title that isn't counted as credits.
    """
    report = CreditsReport()
    for movie in movies:
        titles = list(movie.get("chapters") or [])
        credits = sum(classify_chapter_title(t, is_episode=is_episode) is MarkerType.CREDITS for t in titles)
        report.tally["found" if credits else "not_found"] += 1
        if not credits and titles:
            report.titles_missed.append(titles[-1])
        if credits > 1:
            report.several_credits.append(_name(movie["file"]))
        if any(_uncounted_ending(t, is_episode=is_episode) for t in titles):
            report.ending_titles.append(_name(movie["file"]))
    return report


def _truth(entry: dict, adjudicated: dict[str, dict]) -> float:
    return float((adjudicated.get(os.path.basename(entry["file"])) or {}).get("truth", entry["credits_start"]))


def chapter_rules(
    files: list[dict], adjudicated: dict[str, dict], *, probe: Callable[[str], MediaProbe], is_episode: bool
) -> CreditsReport:
    """Hand-checked files: the chapter source's credits start against the truth (adjudicated wins).

    Args:
        files: ``credits/movies40.json`` or ``tv40.json`` rows -- one kind per call, because the
            episode-only titles ("Ending") depend on it.
        adjudicated: Truth fixed by frame checks, by file name.
        probe: A file's duration and chapters.
        is_episode: The rows are TV episodes.

    Returns:
        The tally of the last credits chapter's start, files with several credits chapters and "Ending" titles.
    """
    report = CreditsReport()
    for entry in files:
        path = entry["file"]
        media = probe(path)
        credits = sorted(
            (c for c in chapter_candidates(media, is_episode=is_episode) if c.type is MarkerType.CREDITS),
            key=lambda c: c.start_ms,
        )
        if len(credits) > 1:
            report.several_credits.append(_name(path))
        start = credits[-1].start_ms / 1000 if credits else None
        report.tally[judge_credits(start, _truth(entry, adjudicated))] += 1
        if any(_uncounted_ending(c.title, is_episode=is_episode) for c in media.chapters):
            report.ending_titles.append(_name(path))
    return report


def compare_credits_with_plex(
    files: list[dict],
    adjudicated: dict[str, dict],
    *,
    probe: Callable[[str], MediaProbe],
    baseline: dict[str, list[PlexMarker]],
    is_movie: bool,
) -> CreditsRows:
    """Plex's first credits start and ``decide()`` on the file's chapters plus Plex's markers, against the truth.

    Args:
        files: Rows with ``file`` and ``credits_start``.
        adjudicated: Truth fixed by frame checks, by file name.
        probe: A file's duration and chapters.
        baseline: Plex's markers by file path.
        is_movie: The files are movies (movie credits start at most 900 s from the end).

    Returns:
        The rows.
    """
    rows = CreditsRows()
    for entry in files:
        path, truth = entry["file"], _truth(entry, adjudicated)
        media = probe(path)
        markers = baseline.get(path, [])
        plex = first_marker(markers, MarkerType.CREDITS)
        chapters = chapter_candidates(media, is_episode=not is_movie)
        detail = {"file": path, "name": _name(path), "truth": truth, "plex": plex.start_ms / 1000 if plex else None,
                  "plex_markers": sum(m.type == "credits" for m in markers),
                  "credits_chapters": sum(c.type is MarkerType.CREDITS for c in chapters),
                  "ending_titles": [c.title for c in media.chapters
                                    if _uncounted_ending(c.title, is_episode=not is_movie)]}  # fmt: skip
        rows.plex[judge_credits(detail["plex"], truth)] += 1
        with_plex = chapters + server_candidates(markers, MarkerType.CREDITS)
        for row, candidates, level in (("chapters", chapters, "high"), ("high", with_plex, "high"),
                                       ("medium", with_plex, "medium")):  # fmt: skip
            ctx = DecisionContext(media.duration_ms or 0, is_movie, level, frozenset({MarkerType.CREDITS}), ORDER)
            d = decide(candidates, ctx, {})[MarkerType.CREDITS]
            start = d.marker.start_ms / 1000 if d.status is DecisionStatus.DECIDED else None
            getattr(rows, row)[judge_credits(start, truth)] += 1
            detail[row] = start
            detail[f"{row}_reason"] = d.reason
        detail["shortened"] = shortened_by(detail["high_reason"]) is not None
        rows.files.append(detail)
    return rows
