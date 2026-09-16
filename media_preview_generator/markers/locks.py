"""Per-key process locks (one app process serves everything: a single gunicorn worker)."""

from __future__ import annotations

import threading
from collections.abc import Callable, Hashable, Iterator
from contextlib import contextmanager
from typing import Any


class KeyedLocks:
    """One lock per key, kept only while a caller holds or waits for it.

    Lock order used by Intro & Credits, never taken the other way round: a path's lock (the whole run on one file), then
    an item's lock (one server item's publish), then a file's fingerprint lock, then Plex's database lock (inside the
    publisher), then markers.db's own lock (every store call).
    """

    def __init__(self, lock_factory: Callable[[], Any] = threading.Lock) -> None:
        """Create the lock table.

        Args:
            lock_factory: Builds the lock for a new key (tests pass spies).
        """
        self._lock_factory = lock_factory
        self._guard = threading.Lock()
        self._locks: dict[Hashable, list] = {}  # key → [lock, callers holding or waiting]

    @contextmanager
    def hold(self, key: Hashable) -> Iterator[None]:
        """Hold ``key``'s lock for the duration of the block."""
        with self._guard:
            entry = self._locks.get(key)
            if entry is None:
                entry = self._locks[key] = [self._lock_factory(), 0]
            entry[1] += 1
        try:
            with entry[0]:
                yield
        finally:
            with self._guard:
                entry[1] -= 1
                if entry[1] == 0:
                    del self._locks[key]
