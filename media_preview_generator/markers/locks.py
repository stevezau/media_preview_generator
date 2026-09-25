"""Per-key process locks (one app process serves everything: a single gunicorn worker)."""

from __future__ import annotations

import threading
from collections.abc import Callable, Hashable, Iterator
from contextlib import contextmanager
from typing import Any

# How often a caller waiting for a key asks whether to stop waiting (``KeyedLocks.hold``).
WAIT_POLL_S = 0.1


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
    def hold(
        self, key: Hashable, *, while_waiting: Callable[[], None] | None = None, poll_s: float = WAIT_POLL_S
    ) -> Iterator[None]:
        """Hold ``key``'s lock for the duration of the block.

        Args:
            key: The lock's key.
            while_waiting: Called every ``poll_s`` while another caller holds the lock; it ends the wait by raising
                (the caller's job was cancelled, or the holder has held it longer than a running one takes: a job
                frozen by its schedule's stop time holds its keys until the next start). None waits as long as it
                takes.
            poll_s: How often ``while_waiting`` is called.

        Raises:
            Whatever ``while_waiting`` raises; nothing is held then.
        """
        with self._guard:
            entry = self._locks.get(key)
            if entry is None:
                entry = self._locks[key] = [self._lock_factory(), 0]
            entry[1] += 1
        try:
            if while_waiting is None:
                with entry[0]:
                    yield
            else:
                while not entry[0].acquire(timeout=poll_s):
                    while_waiting()
                try:
                    yield
                finally:
                    entry[0].release()
        finally:
            with self._guard:
                entry[1] -= 1
                if entry[1] == 0:
                    del self._locks[key]

    @contextmanager
    def try_hold(self, key: Hashable, timeout: float) -> Iterator[bool]:
        """Hold ``key``'s lock for the block when it can be taken within ``timeout``.

        For callers that must not wait on a job (a web request can't block a thread behind a file whose run may take
        minutes), or that wait in slices they can stop between (a job's worker, on a cancel).

        Args:
            key: The lock's key.
            timeout: Seconds to wait for the lock.

        Yields:
            True while holding the lock, False when it couldn't be taken in time (nothing is held then).
        """
        with self._guard:
            entry = self._locks.get(key)
            if entry is None:
                entry = self._locks[key] = [self._lock_factory(), 0]
            entry[1] += 1
        acquired = False
        try:
            acquired = bool(entry[0].acquire(timeout=timeout))
            yield acquired
        finally:
            if acquired:
                entry[0].release()
            with self._guard:
                entry[1] -= 1
                if entry[1] == 0:
                    del self._locks[key]


# One lock per file path, held for a job's whole run of the file (``pipeline``). Two jobs on the same file (a backfill
# and a webhook after a replacement) take turns, so an answer gathered for the old file can never be written over the
# new file's; the missing-file checks outside a job skip a file a job holds (``missing``).
FILE_RUN_LOCKS = KeyedLocks()
