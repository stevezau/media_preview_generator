"""Parse Preview Inspector search strings into normalised query objects.

The user types things like:

* ``"the boys s01e01"`` — show name + season/episode hint
* ``"the.boys.s01e01.1080p.web.h264-rarbg"`` — release-style filename paste
* ``"the boys 1x01"`` — alternate season/episode notation
* ``"wonder boys"`` — movie title only

Vendors substring-match whatever they are given, so passing the raw string
returns every "Boys" item for "the boys s01e01" and nothing for a release
name with tags glued to the title.

:class:`SearchQuery` does the parsing once. Every vendor adapter sees
``query.title``, ``query.season``, ``query.episode``, and
``query.tokens`` instead of the raw mess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Matches S01E01, s1e1, S01E001, S00E00, etc. Captures season + episode.
_SEASON_EPISODE_RE = re.compile(r"\b[Ss](\d{1,3})[Ee](\d{1,4})\b")
# Matches 1x01, 12x345 — alternate notation common in older release names.
_ALT_SEASON_EPISODE_RE = re.compile(r"\b(\d{1,3})[xX](\d{1,4})\b")
# Release-style separators that should be treated as spaces when
# cleaning the title (so "the.boys" → "the boys").
_TITLE_SEPARATORS = re.compile(r"[._]+")
# Junk tokens the user typically leaves at the end of a release-style
# filename paste — strip them once the title is isolated. Entries with a
# space are two-word tags ("web-dl", "h.264", "5.1") that separator
# normalisation has already split into two words. This list is
# intentionally conservative; we drop ONLY the ones we're certain are
# release metadata, not English words that might appear in a real title.
_JUNK_TOKENS = frozenset(
    {
        "1080p",
        "2160p",
        "720p",
        "480p",
        "web",
        "webrip",
        "webdl",
        "web dl",
        "bluray",
        "blu ray",
        "bdrip",
        "brrip",
        "hdrip",
        "hdtv",
        "dvdrip",
        "x264",
        "x265",
        "h264",
        "h265",
        "h 264",
        "h 265",
        "hevc",
        "avc",
        "aac",
        "ac3",
        "dts",
        "eac3",
        "atmos",
        "ddp5",
        "ddp7",
        "remux",
        "proper",
        "repack",
        "extended",
        "uncut",
        "rarbg",
        "yify",
        "yts",
        "ettv",
        "eztv",
        "10bit",
        "8bit",
        "hdr",
        "hdr10",
        "dv",
        "sdr",
        "5 1",
        "7 1",
        "2 0",
        "ddp5 1",
        "ddp7 1",
        "ddp2 0",
        "dd5 1",
        "dd2 0",
        "aac2 0",
        "aac5 1",
    }
)


@dataclass(frozen=True)
class SearchQuery:
    """Normalised representation of a Preview Inspector search string.

    Attributes:
        raw: The input string verbatim (for logging/echo).
        title: Cleaned title — separators normalised to spaces,
            release-junk tokens stripped, lowercased. e.g. for
            ``"the.boys.s01e01.1080p.web.h264-rarbg"`` this is
            ``"the boys"``.
        season: Season number if a S##E## or NxN pattern was present.
        episode: Episode number if a S##E## or NxN pattern was present.
        tokens: Tuple of lowercase title tokens for ranking. e.g.
            ``("the", "boys")``.
    """

    raw: str
    title: str
    season: int | None
    episode: int | None
    tokens: tuple[str, ...]

    @classmethod
    def parse(cls, text: str | None) -> SearchQuery:
        """Parse ``text`` into a :class:`SearchQuery`.

        Empty / whitespace-only input yields a query with empty
        ``title`` and ``tokens`` — callers should treat that as "no
        search to run" and return early.
        """
        raw = text or ""
        # Normalise separators FIRST so the regex picks up "S01E01"
        # whether it arrived as "the.boys.s01e01" or "the boys s01e01".
        normalised = _TITLE_SEPARATORS.sub(" ", raw).strip()

        season: int | None = None
        episode: int | None = None
        # Try canonical S##E## notation first; fall back to NxN.
        m = _SEASON_EPISODE_RE.search(normalised)
        if not m:
            m = _ALT_SEASON_EPISODE_RE.search(normalised)
        if m:
            try:
                season = int(m.group(1))
                episode = int(m.group(2))
            except (ValueError, IndexError):
                season = None
                episode = None
            # Strip the season/episode marker out of the title so the
            # downstream search isn't passed "the boys s01e01" as a
            # literal title — Plex's library.search would never match it.
            normalised = (normalised[: m.start()] + normalised[m.end() :]).strip()

        # Now drop release-junk tokens at the END of the string. We
        # don't strip them anywhere — "Boys" is a perfectly valid title
        # token, but "1080p" and "rarbg" are not.
        # Also pre-split tokens on "-" because release-group suffixes
        # commonly arrive glued to a codec ("h264-rarbg", "x265-NTb").
        # Without the split, the junk-detector sees "h264-rarbg" as a
        # single non-junk token and stops, leaving the entire codec +
        # release-group tail in the title.
        raw_tokens = normalised.split()
        words: list[str] = []
        for tok in raw_tokens:
            words.extend(p for p in tok.split("-") if p)
        # Strip from the right edge: as soon as we hit a token that
        # ISN'T junk, stop. Preserves real titles like "Boys State"
        # while killing ".1080p.web.h264-rarbg" tails. Always keep at
        # least one word so all-caps titles ("LOST") survive.
        while len(words) > 1:
            if _is_junk(words[-1], words[-2]):
                words.pop()
            elif " ".join(words[-2:]).lower() in _JUNK_TOKENS:
                del words[-2:]
            else:
                break

        cleaned = " ".join(words).lower().strip()
        # Normalise whitespace again in case stripping junk left a
        # double-space.
        cleaned = re.sub(r"\s+", " ", cleaned)

        return cls(
            raw=raw,
            title=cleaned,
            season=season,
            episode=episode,
            tokens=tuple(t for t in cleaned.split(" ") if t),
        )

    @property
    def has_episode(self) -> bool:
        """True when the user expressed an interest in a specific episode."""
        return self.season is not None and self.episode is not None

    @property
    def is_empty(self) -> bool:
        """True when there's nothing meaningful to search for."""
        return not self.title and not self.tokens


def _is_junk(token: str, previous: str) -> bool:
    """Return True if ``token`` looks like release metadata (not a real title word).

    Args:
        token: The right-most remaining word.
        previous: The word to its left.

    The caller pre-splits on hyphens so a release-group tail like
    ``h264-rarbg`` arrives as two separate tokens (``h264`` + ``rarbg``).

    Unknown tokens are treated as a release-group tag (``RARBG``, ``CAKES``,
    ``NTb``) only when the word before them is already a curated junk token.
    A bare uppercase word is indistinguishable from an all-caps title
    ("THE BOYS", "LOST"), so it is never stripped on its own.
    """
    t = token.lower().strip("[]()")
    if not t or t in _JUNK_TOKENS:
        return True
    if previous.lower().strip("[]()") not in _JUNK_TOKENS:
        return False
    raw = token.strip("[]()")
    if 3 <= len(raw) <= 12 and raw.isalpha():
        upper_ratio = sum(1 for c in raw if c.isupper()) / len(raw)
        return upper_ratio >= 0.5
    return False
