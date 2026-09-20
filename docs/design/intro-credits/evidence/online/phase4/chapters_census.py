"""What the three populations' chapter titles actually look like, and what today's classifier makes of them.

Reads ``chapters_probe.jsonl`` (git-ignored) and prints counts and titles only -- never a path.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, os.environ.get("MARKERS_REPO", str(Path(__file__).resolve().parents[6])))

from media_preview_generator.markers.models import MarkerType  # noqa: E402
from media_preview_generator.markers.probe import Chapter, MediaProbe  # noqa: E402
from media_preview_generator.markers.sources.chapters import (  # noqa: E402
    _is_generic_intro,
    chapter_candidates,
    classify_chapter_title,
)

HERE = Path(__file__).resolve().parent
POPS = ("anime", "tv", "movie")
_END_WORD = re.compile(r"\b(end|ending|ed)\b", re.I)
# The exact whole titles a scoped "Ending" rule could take, as a census bucket.
_END_EXACT = re.compile(r"^(end|ending|endcard|end card|ending song|ending theme|ed\s?\d*)$", re.I)


def rows() -> list[dict]:
    """Every probed file that ffprobe read."""
    out = []
    for line in (HERE / "chapters_probe.jsonl").read_text().splitlines():
        if line.strip():
            entry = json.loads(line)
            if "err" not in entry:
                out.append(entry)
    return out


def probe_of(entry: dict) -> MediaProbe:
    """A MediaProbe from a probed row (duration is filled in by the scorer, not needed here)."""
    return MediaProbe(
        duration_ms=None,
        chapters=tuple(Chapter(c["start_ms"], c["end_ms"], c["name"]) for c in entry["chapters"]),
    )


_BARE_END = re.compile(r"^(end|ending)$", re.I)
_OP_EXACT = re.compile(r"^OP$")
_ED_EXACT = re.compile(r"^ED$")
_OPENING_WORD = re.compile(r"^opening", re.I)


def _end_chapter_company(entries: list[dict]) -> None:
    """What else is in a file that carries a bare "End"/"Ending" chapter -- the scope question."""
    shapes = Counter()
    titles_seen = Counter()
    files = 0
    for entry in entries:
        texts = [" ".join((c["name"] or "").lstrip("﻿").split()) for c in entry["chapters"]]
        bare = [t for t in texts if _BARE_END.match(t)]
        if not bare:
            continue
        files += 1
        titles_seen[bare[0].lower()] += 1
        shapes["has an exact OP chapter"] += any(_OP_EXACT.match(t) for t in texts)
        shapes["has an exact ED chapter"] += any(_ED_EXACT.match(t) for t in texts)
        shapes["has an Opening* chapter"] += any(_OPENING_WORD.match(t) for t in texts)
        shapes["has a preview chapter"] += any(classify_chapter_title(t) is MarkerType.PREVIEW for t in texts)
        shapes["has a recap chapter"] += any(classify_chapter_title(t) is MarkerType.RECAP for t in texts)
        shapes["has another credits chapter"] += any(classify_chapter_title(t) is MarkerType.CREDITS for t in texts)
        shapes["has any intro chapter"] += any(classify_chapter_title(t) is MarkerType.INTRO for t in texts)
        shapes[f"{'<=6' if len(texts) <= 6 else '>6'} chapters"] += 1
    print(f"  files with a bare 'End'/'Ending' chapter: {files}  titles: {dict(titles_seen)}")
    for shape, count in sorted(shapes.items()):
        print(f"    {shape}: {count}")


def main() -> None:
    """Print the census."""
    entries = rows()
    per_pop = defaultdict(list)
    for entry in entries:
        per_pop[entry["pop"]].append(entry)

    for pop in POPS:
        files = per_pop[pop]
        with_chapters = [f for f in files if f["chapters"]]
        unnamed = Counter()
        named = Counter()
        classified = Counter()
        ending_like = Counter()
        has_specific_opening = has_any_intro = has_ending_like = 0
        intro_files = credits_files = 0
        for entry in with_chapters:
            titles = [c["name"] for c in entry["chapters"]]
            unnamed["unnamed" if not any(t.strip() for t in titles) else "named"] += 1
            specific = False
            any_intro = False
            file_ending = False
            for title in titles:
                text = " ".join(title.lstrip("﻿").split())
                if not text:
                    continue
                named[text.lower()] += 1
                mtype = classify_chapter_title(text)
                classified[mtype.value if mtype else "-"] += 1
                if mtype is MarkerType.INTRO:
                    any_intro = True
                    if not _is_generic_intro(text):
                        specific = True
                if mtype is None and _END_WORD.search(text):
                    ending_like[text.lower()] += 1
                    file_ending = True
            has_specific_opening += specific
            has_any_intro += any_intro
            has_ending_like += file_ending
            cands = chapter_candidates(probe_of(entry))
            intro_files += any(c.type is MarkerType.INTRO for c in cands)
            credits_files += any(c.type is MarkerType.CREDITS for c in cands)

        print(f"\n=== {pop}: {len(files)} read, {len(with_chapters)} with chapters ===")
        print(f"  files whose chapters are all unnamed: {unnamed['unnamed']}")
        print(f"  files with an intro candidate today:   {intro_files}")
        print(f"  files with a credits candidate today:  {credits_files}")
        print(f"  files with a specific opening chapter: {has_specific_opening}")
        print(f"  files with any intro chapter:          {has_any_intro}")
        print(f"  files with an unclassified 'end*' title: {has_ending_like}")
        print("  top unclassified titles with an 'end'/'ending'/'ed' word:")
        for title, n in ending_like.most_common(25):
            flag = "  <- whole-title candidate" if _END_EXACT.match(title) else ""
            print(f"    {n:6d}  {title!r}{flag}")
        _end_chapter_company(with_chapters)


if __name__ == "__main__":
    main()
