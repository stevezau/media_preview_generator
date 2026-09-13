"""Core value types for Intro & Credits. All times are integer milliseconds."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class MarkerType(str, Enum):
    """Segment types we can decide and publish."""

    INTRO = "intro"
    CREDITS = "credits"
    RECAP = "recap"
    PREVIEW = "preview"


class Source(str, Enum):
    """Where a candidate came from (spec §1 order)."""

    CHAPTERS = "chapters"
    THEINTRODB = "theintrodb"
    INTRODB = "introdb"
    SKIPDB = "skipdb"
    SEASON_AUDIO = "season_audio"
    CREDITS_TEXT = "credits_text"
    SERVER_MARKERS = "server_markers"
    USER = "user"


LOCAL_SOURCES: frozenset[Source] = frozenset({Source.CHAPTERS, Source.SEASON_AUDIO, Source.CREDITS_TEXT})


@dataclass(frozen=True)
class Candidate:
    """One source's claim about a segment.

    Attributes:
        type: Segment type.
        start_ms: Start offset.
        end_ms: End offset, or None when the segment runs to the end of the file.
        source: Evidence source.
        confidence: Source-reported confidence 0-1. It never outranks source order: candidates
            are ranked by the user's source order, then `Source` enum declaration order (e.g. two
            sources both absent from the user's order), then higher confidence (NaN or infinite
            counts as 0.0), then the shorter skip. Chapter selection ignores confidence (first
            intro/recap chapter, last credits/preview chapter, the earlier end on a tied start).
        origin: Free text: the server id for server markers, the chapter title for chapters.
    """

    type: MarkerType
    start_ms: int
    end_ms: int | None
    source: Source
    confidence: float = 1.0
    origin: str = ""


@dataclass(frozen=True)
class Marker:
    """A decided segment: the desired state servers are projected from."""

    type: MarkerType
    start_ms: int
    end_ms: int
    decided_by: tuple[str, ...]
    locked: bool = False


@dataclass(frozen=True)
class FileIdentity:
    """File identity (spec §6.1): path + size + mtime."""

    canonical_path: str
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class MediaIds:
    """External ids for online lookups."""

    kind: str = "unknown"  # "movie" | "episode" | "unknown"
    tmdb: str | None = None
    imdb: str | None = None
    tvdb: str | None = None
    season: int | None = None
    episode: int | None = None

    @property
    def is_episode(self) -> bool:
        """Whether these ids describe a TV episode."""
        return self.kind == "episode"
