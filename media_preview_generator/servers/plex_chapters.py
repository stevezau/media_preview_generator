"""Public Plex chapter gateway; the agent imports the lightweight shared backend directly."""

from ..markers.publishers.plex_chapters import (
    Chapter,
    ChapterError,
    ChapterTarget,
    chapter_capability,
    register_chapters,
    resolve_chapter_target,
    verify_chapters,
)

__all__ = [
    "Chapter",
    "ChapterError",
    "ChapterTarget",
    "chapter_capability",
    "register_chapters",
    "resolve_chapter_target",
    "verify_chapters",
]
