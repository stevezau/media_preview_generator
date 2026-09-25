"""Chapter names → candidates (spec §5.1). Whole-title matches only: "The Opening Night" is a scene, not an intro.

A plain "Intro" chapter next to a specific opening chapter ("OP", "Opening", "Opening
Credits/Titles", "Title Sequence", "Main Title(s)", "Theme Song") is the cold open, not the theme
song -- one anime episode has "Intro" 0-274.7 s (the cold open) followed by "OP" 274.7-363.9 s
(the real theme); publishing the "Intro" chapter would skip 4.5 min of story. When a file has both,
only the specific one becomes a candidate. "OP"/"ED" are two-letter
genre abbreviations that collide with real words in any other case (a character named "Ed"), so
they match only written exactly uppercase; every other name matches case-insensitively. A leading
U+FEFF byte-order mark (seen in some muxers' embedded titles) is stripped before matching or use as
`origin`.

"Ending" is credits on an **episode** only. On anime it names the ED; in a film it names the last
scene. Measured on the owner's library (`evidence/eval/phase4-chapters.md`): 282 of 4,346 anime
episodes carry a bare "Ending" chapter, and so does 1 of 9,904 movies -- a documentary whose "Ending"
is its closing scene, where taking it would skip the film's last 161 s. No chapter shape tells the two
apart, so the kind does. Callers pass the kind the file's **path** gives (`ids_from_path`), never a
kind a server resolved: the path is part of the file's identity, so chapter evidence cached under
`CHAPTER_RULES_VERSION` can never disagree with the input that derived it. All 282 of those anime files
name a season and episode in the path, so the stricter input costs nothing measured.
"End" alone stays a scene name everywhere: no anime file used it, four movies and five non-anime TV
episodes did.
"""

from __future__ import annotations

import re

from ..models import Candidate, MarkerType, Source
from ..probe import Chapter, MediaProbe

# Bump whenever a change here -- or to `external_ids.ids_from_path`'s episode rule, which callers pass as
# `is_episode` -- changes the candidates a file's chapters give, so files probed before are read again.
# 2: "Ending" reads as credits on an episode (phase 4, Task 15).
# 3: non-English intro/credits chapter names (portability pass).
CHAPTER_RULES_VERSION = 3

# "End" alone is a common final-scene name, so it is deliberately not credits anywhere; "Ending" is
# credits on an episode only (`_EPISODE_PATTERNS`).
# "OP"/"ED" are handled separately, case-sensitively -- see module docstring.
# Non-English names (whole-title, case-insensitive, same as English): German (Vorspann/Abspann), French
# (Générique/Générique de fin -- accent optional, some muxers drop it), Spanish (Cabecera/Créditos;
# Spanish also reuses the English "Intro"/"Credits", already matched), Italian (Sigla/Titoli di coda),
# Portuguese (Abertura/Créditos finais), Dutch (Aftiteling). Anime's romanised "OP"/"ED" are the
# case-sensitive pair above, not here.
_PATTERNS: tuple[tuple[MarkerType, re.Pattern[str]], ...] = (
    (
        MarkerType.INTRO,
        re.compile(
            r"^(intro(duction)?|opening( credits| titles?)?|title sequence|main titles?|theme( song)?"
            r"|vorspann|g[eé]n[eé]rique|cabecera|sigla|abertura)$",
            re.I,
        ),
    ),
    (
        MarkerType.CREDITS,
        re.compile(
            r"^((end|ending|closing) credits|credits|end titles?|outro"
            r"|abspann|g[eé]n[eé]rique de fin|cr[eé]ditos( finais)?|titoli di coda|aftiteling)$",
            re.I,
        ),
    ),
    (MarkerType.RECAP, re.compile(r"^(recap|previously( on\b.*)?|story so far)$", re.I)),
    (MarkerType.PREVIEW, re.compile(r"^(preview|next (episode|time)( preview)?( on\b.*)?)$", re.I)),
)
_EXACT_CASE_PATTERNS: tuple[tuple[MarkerType, re.Pattern[str]], ...] = (
    (MarkerType.INTRO, re.compile(r"^OP$")),
    (MarkerType.CREDITS, re.compile(r"^ED$")),
)
# Names that only mean a marker on a TV episode -- see the module docstring for why "Ending" is one.
_EPISODE_PATTERNS: tuple[tuple[MarkerType, re.Pattern[str]], ...] = (
    (MarkerType.CREDITS, re.compile(r"^ending$", re.I)),
)
_GENERIC_INTRO = re.compile(r"^(intro|introduction)$", re.I)


def _normalize(title: str) -> str:
    """Whitespace-collapsed title for matching, after stripping a leading BOM."""
    return " ".join((title or "").lstrip("\ufeff").split())


def classify_chapter_title(title: str, *, is_episode: bool = False) -> MarkerType | None:
    """Map a chapter title to a marker type, or None for ordinary chapters.

    Args:
        title: The chapter's embedded title.
        is_episode: The file's own path names a season and episode. A path that doesn't counts as not
            an episode, so the extra names stay off -- see the module docstring for why the path, and
            not a kind a server resolved, is the input.

    Returns:
        The marker type the name means, or None.
    """
    text = _normalize(title)
    for mtype, pattern in _EXACT_CASE_PATTERNS:
        if pattern.match(text):
            return mtype
    patterns = (_PATTERNS + _EPISODE_PATTERNS) if is_episode else _PATTERNS
    for mtype, pattern in patterns:
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


def chapter_candidates(probe: MediaProbe, *, is_episode: bool = False) -> list[Candidate]:
    """Candidates for every named intro/credits/recap/preview chapter.

    Ends are clamped to the next chapter's start (spec §5.1). A generic "Intro"/"Introduction"
    chapter is dropped when the file also has a specific opening chapter (the cold open) -- see
    module docstring.

    Args:
        probe: The file's duration and chapters.
        is_episode: The file's own path names a season and episode, which lets the episode-only names
            ("Ending") match.

    Returns:
        One candidate per named chapter, in the file's chapter order.
    """
    chapters = probe.chapters
    ends = _clamped_ends(chapters)
    types = [classify_chapter_title(c.title, is_episode=is_episode) for c in chapters]
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
