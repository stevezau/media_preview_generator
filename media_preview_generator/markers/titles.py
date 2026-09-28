"""The server titles the Intro & Credits job log names films by, kept per process and looked up at most once per file.

A film is named by its server's title when the run already has the server's answer. Otherwise the log asks one server
once per file per process, on a thread of its own and for at most ``LOOKUP_TIMEOUT_S``. Whatever the lookup does (hang,
raise, answer nothing), the file's run carries on and names the film by its file name.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable

from loguru import logger

# The longest a file's run waits for a title lookup; the lookup itself may go on in the background.
LOOKUP_TIMEOUT_S = 2.0
# Files whose title this process remembers (a title is a few dozen bytes: 20,000 is well under 10 MB).
CACHE_SIZE = 20_000
# Lookups in flight at once. A server that doesn't answer holds them until its own request timeout; meanwhile further
# files skip the lookup at once instead of each waiting ``LOOKUP_TIMEOUT_S``.
_MAX_IN_FLIGHT = 4
# The cached answer of a file its server was asked about but gave no title for: never asked again this process.
NO_TITLE: tuple[str | None, object] = (None, None)

Title = tuple[str | None, object]


class TitleCache:
    """A bounded, thread-safe LRU of ``canonical path → (title, year)``."""

    def __init__(self, max_entries: int = CACHE_SIZE) -> None:
        self._max = max(1, int(max_entries))
        self._entries: OrderedDict[str, Title] = OrderedDict()
        self._lock = threading.Lock()

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
_IN_FLIGHT = threading.BoundedSemaphore(_MAX_IN_FLIGHT)


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


def look_up(path: str, asks: Iterable[Callable[[], object]], *, timeout_s: float | None = None) -> Title:
    """Ask the file's servers for its title, once per process, waiting at most ``timeout_s``. Never raises.

    The file counts as asked as soon as the lookup starts, so a later job never asks again, even while this one is
    still waiting for its server; a title that arrives after the wait is kept for those jobs.

    Args:
        path: The file's local path.
        asks: Per server with a known item id for the file, a call that returns its external ids answer.
        timeout_s: The longest wait; None: ``LOOKUP_TIMEOUT_S``.

    Returns:
        ``(title, year)``, or ``NO_TITLE`` when no server gave one in time (or none could be asked).
    """
    calls = list(asks)
    if not calls or not _IN_FLIGHT.acquire(blocking=False):
        return NO_TITLE
    answer: dict[str, Title] = {}
    done = threading.Event()

    def ask_each() -> None:
        try:
            for call in calls:
                try:
                    found = title_of(call())
                except Exception as exc:  # noqa: BLE001 - a server's failure only costs the film its title
                    logger.debug("Title lookup for {} failed: {}", path, type(exc).__name__)
                    continue
                if found[0]:
                    TITLE_CACHE.put(path, found)
                    answer["title"] = found
                    return
        finally:
            _IN_FLIGHT.release()
            done.set()

    try:
        TITLE_CACHE.put(path, NO_TITLE)
        threading.Thread(target=ask_each, name="ic-title-lookup", daemon=True).start()
    except Exception as exc:  # noqa: BLE001 - no thread, no title: the run goes on
        _IN_FLIGHT.release()
        logger.debug("Title lookup for {} not started: {}", path, type(exc).__name__)
        return NO_TITLE
    done.wait(LOOKUP_TIMEOUT_S if timeout_s is None else timeout_s)
    return answer.get("title", NO_TITLE)
