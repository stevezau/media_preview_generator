"""The server titles the Intro & Credits job log names films by, kept per process and looked up at most once per file.

A film is named by its server's title when the run already has the server's answer, or when a publishing owner's item
id is already known (a hint, or looked up for another step this run) -- never by searching for one: that's a real
network search with its own retries, run only where a file's own outcome can afford to wait for it (``_publish_to``),
not for a log line. Whatever the ask does (raise, answer nothing), the file's run carries on and names the film by
its file name.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterable
from threading import Lock

from loguru import logger

# Files whose title this process remembers (a title is a few dozen bytes: 20,000 is well under 10 MB).
CACHE_SIZE = 20_000
# The cached answer of a file its server was asked about but gave no title for: never asked again this process.
NO_TITLE: tuple[str | None, object] = (None, None)

Title = tuple[str | None, object]


class TitleCache:
    """A bounded, thread-safe LRU of ``canonical path → (title, year)``."""

    def __init__(self, max_entries: int = CACHE_SIZE) -> None:
        self._max = max(1, int(max_entries))
        self._entries: OrderedDict[str, Title] = OrderedDict()
        self._lock = Lock()

    def get(self, path: str) -> Title | None:
        """The title kept for a file, most recently used from now on.

        Args:
            path: The file's local path.

        Returns:
            ``(title, year)``; ``NO_TITLE`` when its server was asked and gave none; None when it wasn't asked.
        """
        with self._lock:
            found = self._entries.get(path)
            if found is not None:
                self._entries.move_to_end(path)
            return found

    def put(self, path: str, title: Title) -> None:
        """Keep a file's title, dropping the least recently used file past the bound.

        Args:
            path: The file's local path.
            title: ``(title, year)``, or ``NO_TITLE``.
        """
        with self._lock:
            self._entries[path] = title
            self._entries.move_to_end(path)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        """Forget every title."""
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


TITLE_CACHE = TitleCache()


def title_of(raw: object) -> Title:
    """The title and year in a server's external ids answer (``MediaServer.get_external_ids``).

    Args:
        raw: The answer.

    Returns:
        ``(title, year)``, or ``NO_TITLE`` when it has no title.
    """
    if isinstance(raw, dict) and str(raw.get("title") or "").strip():
        return str(raw["title"]).strip(), raw.get("year")
    return NO_TITLE


def look_up(path: str, asks: Iterable[Callable[[], object]]) -> Title:
    """Ask the file's already-known servers for its title, once per process. Never raises.

    The file counts as asked as soon as this starts, so a later job never asks again even if every call here fails
    or answers nothing.

    Args:
        path: The file's local path.
        asks: Per server with a known item id for the file, a call that returns its external ids answer.

    Returns:
        ``(title, year)``, or ``NO_TITLE`` when no server gave one (or none could be asked).
    """
    calls = list(asks)
    if not calls:
        return NO_TITLE
    TITLE_CACHE.put(path, NO_TITLE)
    for call in calls:
        try:
            found = title_of(call())
        except Exception as exc:  # noqa: BLE001 - a server's failure only costs the film its title
            logger.debug("Title lookup for {} failed: {}", path, type(exc).__name__)
            continue
        if found[0]:
            TITLE_CACHE.put(path, found)
            return found
    return NO_TITLE
