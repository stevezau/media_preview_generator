"""Chapter names → candidates (spec §5.1). Whole-title matches only: "The Opening Night" is a scene, not an intro.

A plain "Intro" chapter next to a specific opening chapter ("OP", "Opening", "Opening
Credits/Titles", "Title Sequence", "Main Title(s)", "Theme Song") is the cold open, not the theme
song -- Mushoku Tensei S01E06 has "Intro" 0-274.7 s (the cold open) followed by "OP" 274.7-363.9 s
(the real theme); publishing the "Intro" chapter would skip 4.5 min of story. When a file has both,
only the specific one becomes a candidate. "OP"/"ED" are two-letter
genre abbreviations that collide with real words in any other case (a character named "Ed"), so
they match only written exactly uppercase; every other name matches case-insensitively. A leading
U+FEFF byte-order mark (seen in some muxers' embedded titles) is stripped before matching or use as
`origin`.
"""

from __future__ import annotations

import re

from ..models import Candidate, MarkerType, Source
from ..probe import Chapter, MediaProbe

# "End"/"Ending" alone are common final-scene names in movies, so they are deliberately not credits.
# "OP"/"ED" are handled separately, case-sensitively -- see module docstring.
_PATTERNS: tuple[tuple[MarkerType, re.Pattern[str]], ...] = (
    (
        MarkerType.INTRO,
        re.compile(r"^(intro(duction)?|opening( credits| titles?)?|title sequence|main titles?|theme( song)?)$", re.I),
    ),
    (MarkerType.CREDITS, re.compile(r"^((end|ending|closing) credits|credits|end titles?|outro)$", re.I)),
    (MarkerType.RECAP, re.compile(r"^(recap|previously( on\b.*)?|story so far)$", re.I)),
    (MarkerType.PREVIEW, re.compile(r"^(preview|next (episode|time)( preview)?( on\b.*)?)$", re.I)),
)
_EXACT_CASE_PATTERNS: tuple[tuple[MarkerType, re.Pattern[str]], ...] = (
    (MarkerType.INTRO, re.compile(r"^OP$")),
    (MarkerType.CREDITS, re.compile(r"^ED$")),
)
_GENERIC_INTRO = re.compile(r"^(intro|introduction)$", re.I)


def _normalize(title: str) -> str:
    """Whitespace-collapsed title for matching, after stripping a leading BOM."""
    return " ".join((title or "").lstrip("\ufeff").split())


def classify_chapter_title(title: str) -> MarkerType | None:
    """Map a chapter title to a marker type, or None for ordinary chapters."""
    text = _normalize(title)
    for mtype, pattern in _EXACT_CASE_PATTERNS:
        if pattern.match(text):
            return mtype
    for mtype, pattern in _PATTERNS:
        if pattern.match(text):
            return mtype
    return None


def _is_generic_intro(title: str) -> bool:
    """Whether `title` is a generic "Intro"/"Introduction" name rather than a specific opening name
    ("OP", "Opening", "Title Sequence", ...) -- see module docstring."""
    return bool(_GENERIC_INTRO.match(_normalize(title)))


def _clamped_ends(chapters: tuple[Chapter, ...]) -> list[int | None]:
    """Each chapter's end, clamped to the first later chapter's start (spec §5.1: a chapter runs to
    the next chapter's start).

    Chapters that share a start (e.g. a studio-logo chapter and the real first chapter both at 0)
    must not clamp each other to zero length -- only a *strictly* later start counts.
    """
    order = sorted(range(len(chapters)), key=lambda i: chapters[i].start_ms)
    ends: list[int | None] = [c.end_ms for c in chapters]
    for pos, i in enumerate(order):
        own_start = chapters[i].start_ms
        later_starts = (chapters[order[j]].start_ms for j in range(pos + 1, len(order)))
        next_start = next((s for s in later_starts if s > own_start), None)
        if next_start is not None:
            ends[i] = next_start if ends[i] is None else min(ends[i], next_start)
    return ends


def chapter_candidates(probe: MediaProbe) -> list[Candidate]:
    """Candidates for every named intro/credits/recap/preview chapter.

    Ends are clamped to the next chapter's start (spec §5.1). A generic "Intro"/"Introduction"
    chapter is dropped when the file also has a specific opening chapter (the cold open) -- see
    module docstring.
    """
    chapters = probe.chapters
    ends = _clamped_ends(chapters)
    types = [classify_chapter_title(c.title) for c in chapters]
    has_specific_opening = any(
        t is MarkerType.INTRO and not _is_generic_intro(c.title) for c, t in zip(chapters, types, strict=True)
    )
    out = []
    for chapter, mtype, end in zip(chapters, types, ends, strict=True):
        if mtype is None:
            continue
        if mtype is MarkerType.INTRO and has_specific_opening and _is_generic_intro(chapter.title):
            continue
        origin = chapter.title.lstrip("\ufeff").strip()
        out.append(Candidate(mtype, chapter.start_ms, end, Source.CHAPTERS, origin=origin))
    return out
