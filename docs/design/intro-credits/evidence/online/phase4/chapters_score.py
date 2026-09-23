"""Task 15's before/after: what the chapter source decides on anime, non-anime TV and movies.

Each variant's candidates go through the app's own ``decide()`` at High with chapters as the only
source, so a row is what the app would publish from the file's chapters alone (spec §5.5 rule 2's
sanity checks included). Plex's own markers, from the phase-2 read-only dump, are the independent
cross-check for every answer a variant gains or moves -- they are not truth, and the write-up says so.

Prints counts and show/movie names only; never a path.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, os.environ.get("MARKERS_REPO", str(Path(__file__).resolve().parents[6])))

from media_preview_generator.markers.decide import DecisionContext, DecisionStatus, decide  # noqa: E402
from media_preview_generator.markers.external_ids import ids_from_path  # noqa: E402
from media_preview_generator.markers.models import Candidate, MarkerType, Source  # noqa: E402
from media_preview_generator.markers.probe import Chapter, MediaProbe  # noqa: E402
from media_preview_generator.markers.sources.chapters import (  # noqa: E402
    _clamped_ends,
    _is_generic_intro,
    chapter_candidates,
    classify_chapter_title,
)

HERE = Path(__file__).resolve().parent
PARTS = Path(os.environ.get("MARKERS_PARTS_DUMP") or HERE / "../../lab/results/scale/prod_plex_parts.json")
PLEX = Path(os.environ.get("MARKERS_PLEX_DUMP") or HERE / "../../lab/results/scale/prod_plex_markers.json")
POPS = ("anime", "tv", "movie")
ORDER = ("chapters",)
AGREE_S = 10.0  # spec §5.5 rule 4's credits tolerance
LATE_S = 30.0  # tools/markers_eval/credits.py judge_credits

# The anime ED chapter names the classifier misses today, as whole titles.
_ENDING_TITLE = re.compile(r"^(ending|end)$", re.I)
_ENDING_ONLY = re.compile(r"^ending$", re.I)
_SEASON_FOLDER = re.compile(r"^((season|series|staffel|saison)\s*\d+|specials)$", re.I)
_IDS = re.compile(r"\s*[{\[(](?:tvdb|tmdb|imdb|anidb|mal|anilist)-[^}\])]*[}\])]", re.I)


def name_of(path: str) -> str:
    """The movie folder, or "<show> <season folder>" for an episode, with id tags stripped."""
    folder = os.path.dirname(path)
    parent = os.path.basename(folder)
    if _SEASON_FOLDER.match(parent):
        return _IDS.sub("", f"{os.path.basename(os.path.dirname(folder))} {parent}").strip()
    return _IDS.sub("", parent).strip()


def show_of(path: str) -> str:
    """The show (or movie) folder name, ids stripped -- the per-show grouping Task 1 used."""
    folder = os.path.dirname(path)
    if _SEASON_FOLDER.match(os.path.basename(folder)):
        folder = os.path.dirname(folder)
    return _IDS.sub("", os.path.basename(folder)).strip()


# --- the variants -----------------------------------------------------------------------------


def _candidates(probe: MediaProbe, *, ending_scope: str) -> list[Candidate]:
    """The shipped rules, plus an "Ending"/"End" chapter counted as credits under ``ending_scope``.

    Scopes: ``off`` (today), ``global`` (any file), ``opening`` (only a file that also has a
    specific opening chapter), ``any_intro`` (only a file that also has any intro chapter).
    """
    chapters = probe.chapters
    ends = _clamped_ends(chapters)
    types = [classify_chapter_title(c.title) for c in chapters]
    has_specific_opening = any(
        t is MarkerType.INTRO and not _is_generic_intro(c.title) for c, t in zip(chapters, types, strict=True)
    )
    has_any_intro = any(t is MarkerType.INTRO for t in types)
    scoped = {
        "off": False,
        "global": True,
        "opening": has_specific_opening,
        "any_intro": has_any_intro,
    }[ending_scope]
    out = []
    for chapter, mtype, end in zip(chapters, types, ends, strict=True):
        text = " ".join((chapter.title or "").lstrip("﻿").split())
        if mtype is None and scoped and _ENDING_TITLE.match(text):
            mtype = MarkerType.CREDITS
        if mtype is None:
            continue
        if mtype is MarkerType.INTRO and has_specific_opening and _is_generic_intro(chapter.title):
            continue
        out.append(Candidate(mtype, chapter.start_ms, end, Source.CHAPTERS, origin=text))
    return out


def _candidates_ending_only(probe: MediaProbe, *, scope: str) -> list[Candidate]:
    """As ``_candidates`` but only the exact title "Ending" -- "End" alone stays a final scene."""
    out = []
    for cand in _candidates(probe, ending_scope=scope):
        if cand.type is MarkerType.CREDITS and _ENDING_TITLE.match(cand.origin or ""):
            if not _ENDING_ONLY.match(cand.origin or ""):
                continue
        out.append(cand)
    return out


def _drop_lone_generic_intro(probe: MediaProbe) -> list[Candidate]:
    """The shipped rules minus any intro from a lone generic "Intro" chapter (finding 2's change)."""
    cands = chapter_candidates(probe)
    lone = [
        c
        for c in probe.chapters
        if classify_chapter_title(c.title) is MarkerType.INTRO and not _is_generic_intro(c.title)
    ]
    if lone:
        return cands
    return [c for c in cands if not (c.type is MarkerType.INTRO and _is_generic_intro(c.origin or ""))]


def _episode_only(build):
    """A variant that only fires on an episode -- the library-kind scope."""
    return lambda p, is_episode: build(p) if is_episode else chapter_candidates(p)


VARIANTS = {
    "base": lambda p, is_episode: chapter_candidates(p),
    "end_global": lambda p, is_episode: _candidates(p, ending_scope="global"),
    "end_opening": lambda p, is_episode: _candidates(p, ending_scope="opening"),
    "end_any_intro": lambda p, is_episode: _candidates(p, ending_scope="any_intro"),
    "ending_only_global": lambda p, is_episode: _candidates_ending_only(p, scope="global"),
    "ending_only_opening": lambda p, is_episode: _candidates_ending_only(p, scope="opening"),
    "end_episode": _episode_only(lambda p: _candidates(p, ending_scope="global")),
    "ending_only_episode": _episode_only(lambda p: _candidates_ending_only(p, scope="global")),
    "drop_lone_intro": lambda p, is_episode: _drop_lone_generic_intro(p),
    "drop_lone_intro_episode": _episode_only(_drop_lone_generic_intro),
    # The shipped code itself, so the write-up's "after" is the app's answer and not the script's model.
    "shipped": lambda p, is_episode: chapter_candidates(p, is_episode=is_episode),
}


# --- scoring ----------------------------------------------------------------------------------


def _decided(cands: list[Candidate], duration_ms: int, is_movie: bool) -> dict[MarkerType, tuple[int, int] | None]:
    """What the app publishes from these candidates alone, at High."""
    ctx = DecisionContext(
        duration_ms,
        is_movie,
        "high",
        frozenset({MarkerType.INTRO, MarkerType.CREDITS, MarkerType.RECAP, MarkerType.PREVIEW}),
        ORDER,
    )
    out = {}
    for mtype, decision in decide(cands, ctx, {}).items():
        marker = decision.marker if decision.status is DecisionStatus.DECIDED else None
        out[mtype] = (marker.start_ms, marker.end_ms) if marker else None
    return out


def _plex_markers() -> dict[str, dict[str, list[tuple[int, int]]]]:
    """Plex's own intro/credits markers by path, from the read-only phase-2 dump."""
    out: dict[str, dict[str, list[tuple[int, int]]]] = defaultdict(lambda: {"intro": [], "credits": []})
    for row in json.loads(PLEX.read_text()):
        if row.get("type") in ("intro", "credits"):
            out[row["file"]][row["type"]].append((row["start"], row["end"]))
    for markers in out.values():
        for key in markers:
            markers[key].sort()
    return dict(out)


def _verdict(start_ms: int, plex: list[tuple[int, int]]) -> str:
    """How an answer sits against Plex's own first marker of that type."""
    if not plex:
        return "plex silent"
    delta = (start_ms - plex[0][0]) / 1000
    if abs(delta) <= AGREE_S:
        return "agrees with Plex"
    if delta < -AGREE_S:
        return "earlier than Plex (would skip story)"
    return "later than Plex by >30 s" if delta > LATE_S else "later than Plex (10-30 s)"


def _lone_generic_intro(entry: dict) -> Chapter | None:
    """The file's only intro chapter when it is a generic "Intro"/"Introduction" (finding 2's files)."""
    intros = [
        Chapter(c["start_ms"], c["end_ms"], c["name"])
        for c in entry["chapters"]
        if classify_chapter_title(c["name"]) is MarkerType.INTRO
    ]
    if len(intros) != 1 or not _is_generic_intro(intros[0].title):
        return None
    return intros[0]


def lone_intro_report(entries: list[dict], plex: dict, label: str) -> None:
    """Is a lone generic "Intro" chapter the theme song or the cold open?

    Plex's own intro marker is found by audio matching across the season, so it lands on the theme.
    A chapter that ends where Plex's intro ends is the theme; one that ends well before Plex's intro
    starts is the cold open, and publishing it would skip story.
    """
    verdicts = Counter()
    per_show = defaultdict(Counter)
    for entry in entries:
        chapter = _lone_generic_intro(entry)
        if chapter is None:
            continue
        cands = [c for c in chapter_candidates(_probe_of(entry)) if c.type is MarkerType.INTRO]
        if not cands:
            continue
        start, end = cands[0].start_ms, cands[0].end_ms or entry["duration"]
        markers = plex.get(entry["file"], {}).get("intro", [])
        if not markers:
            verdict = "plex silent"
        else:
            p_start, p_end = markers[0]
            if abs(end - p_end) <= 5_000:
                verdict = "theme (ends where Plex's intro ends)"
            elif end <= p_start + 5_000:
                verdict = "cold open (ends before Plex's intro starts)"
            elif start >= p_end - 5_000:
                verdict = "after Plex's intro"
            else:
                verdict = "overlaps Plex's intro, edges differ"
        verdicts[verdict] += 1
        per_show[show_of(entry["file"])][verdict] += 1
    total = sum(verdicts.values())
    print(f"--- {label}: {total} files whose only intro chapter is a generic 'Intro' ---")
    for verdict, count in verdicts.most_common():
        print(f"    {verdict}: {count}")
    uniform = Counter()
    for show, counts in per_show.items():
        top, n = counts.most_common(1)[0]
        uniform[top if n == sum(counts.values()) else "mixed within the show"] += 1
    print(f"    across {len(per_show)} shows; per-show verdict: {dict(uniform)}")


def _probe_of(entry: dict) -> MediaProbe:
    """A MediaProbe for a probed row."""
    return MediaProbe(
        duration_ms=entry["duration"],
        chapters=tuple(Chapter(c["start_ms"], c["end_ms"], c["name"]) for c in entry["chapters"]),
    )


def main() -> None:
    """Print the baseline and every variant's gain and cost, per population."""
    durations = {p["file"]: p.get("duration") for p in json.loads(PARTS.read_text())}
    plex = _plex_markers()

    files = defaultdict(list)
    skipped = Counter()
    for line in (HERE / "chapters_probe.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if "err" in entry:
            skipped[f"{entry['pop']}: ffprobe error"] += 1
            continue
        duration = durations.get(entry["file"])
        if not duration:
            skipped[f"{entry['pop']}: no duration in the parts dump"] += 1
            continue
        entry["duration"] = duration
        files[entry["pop"]].append(entry)
    by_path = {e["file"]: e for rows in files.values() for e in rows}
    print(f"skipped: {dict(skipped)}\n")

    for pop in POPS:
        pop_files = files[pop]
        is_movie = pop == "movie"
        decided = {name: {} for name in VARIANTS}
        for entry in pop_files:
            probe = _probe_of(entry)
            # The shipped scope reads the file's own path, so every episode-scoped variant uses the same
            # input rather than the population label.
            is_episode = ids_from_path(entry["file"]).is_episode
            for name, build in VARIANTS.items():
                decided[name][entry["file"]] = _decided(build(probe, is_episode), entry["duration"], is_movie)

        base = decided["base"]
        print(f"=== {pop}: {len(pop_files)} files ===")
        for mtype in (MarkerType.INTRO, MarkerType.CREDITS):
            answered = {path: v[mtype] for path, v in base.items() if v[mtype]}
            print(f"  baseline {mtype.value:8s} published from chapters alone: {len(answered)}")
            # The control for every "vs Plex" number below: how today's accepted chapters already sit
            # against Plex on this population. Plex is a cross-check, not truth.
            verdicts = Counter(
                _verdict(start, plex.get(path, {}).get(mtype.value, [])) for path, (start, _) in answered.items()
            )
            for verdict, count in verdicts.most_common():
                print(f"      {verdict}: {count}")
        print()

        for name in VARIANTS:
            if name == "base":
                continue
            gained, moved, lost = [], [], []
            for path, answers in decided[name].items():
                for mtype in (MarkerType.INTRO, MarkerType.CREDITS):
                    before, after = base[path][mtype], answers[mtype]
                    if before == after:
                        continue
                    if before is None:
                        gained.append((mtype, path, after))
                    elif after is None:
                        lost.append((mtype, path, before))
                    else:
                        moved.append((mtype, path, before, after))
            if not (gained or moved or lost):
                print(f"  {name}: no change")
                continue
            print(f"  {name}: +{len(gained)} gained, {len(moved)} moved, -{len(lost)} lost")
            for label, rows in (("gained", gained), ("moved", moved)):
                verdicts = Counter()
                for row in rows:
                    mtype, path = row[0], row[1]
                    after = row[2] if label == "gained" else row[3]
                    verdicts[(mtype.value, _verdict(after[0], plex.get(path, {}).get(mtype.value, [])))] += 1
                for (mtype, verdict), count in sorted(verdicts.items()):
                    print(f"    {label:6s} {mtype:8s} {verdict}: {count}")
            shows = Counter(show_of(path) for _, path, *_ in gained + moved)
            print(f"    across {len(shows)} shows/movies; top: {[n for n, _ in shows.most_common(6)]}")
            # A 4 s "Ending" chapter is a "to be continued" card, not a credit roll (One Piece S21),
            # so the gained answers' lengths say how much of the gain is noise.
            lengths = Counter()
            for mtype, path, after in gained:
                seconds = (after[1] - after[0]) / 1000
                lengths["under 30 s" if seconds < 30 else ("30-60 s" if seconds < 60 else "60 s or more")] += 1
            if lengths:
                print(f"    gained answer length: {dict(lengths)}")
            if pop != "anime":
                for mtype, path, after in gained:
                    entry = by_path[path]
                    plex_markers = plex.get(path, {}).get(mtype.value, [])
                    plex_at = f"{plex_markers[0][0] / 1000:.0f}s" if plex_markers else "silent"
                    print(
                        f"      gained {mtype.value} {name_of(path)}: {after[0] / 1000:.0f}s of "
                        f"{entry['duration'] / 1000:.0f}s, plex {plex_at}"
                    )
                    print(f"        titles: {[c['name'] for c in entry['chapters']]}")
            for mtype, path, before, after in moved[:12]:
                print(f"      moved {mtype.value} {name_of(path)}: {before[0] / 1000:.0f}s -> {after[0] / 1000:.0f}s")
            for mtype, path, before in lost[:12]:
                print(f"      lost  {mtype.value} {name_of(path)}: was {before[0] / 1000:.0f}s")
        same = decided["shipped"] == decided["ending_only_episode"]
        print(f"  shipped code matches the ending_only_episode variant exactly: {same}")
        print()
        lone_intro_report(pop_files, plex, pop)
        print()


if __name__ == "__main__":
    main()
