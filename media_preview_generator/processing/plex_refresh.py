"""Bounded Plex notifications, with acknowledgements stored in output journals."""

from __future__ import annotations

import atexit
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from ..output.journal import clear_plex_refresh_pending
from ..output.plex_hash import SourceFingerprint

if TYPE_CHECKING:
    from ..servers.plex import PlexServer


@dataclass(frozen=True)
class _Refresh:
    server: PlexServer
    canonical_path: str
    item_id: str | None
    output_paths: tuple[Path, ...]
    notification_token: str | None
    source_fingerprint: SourceFingerprint | None


class PlexRefreshQueue:
    """Coalesce pending file notifications without blocking generation on Plex.

    One daemon worker drains a bounded queue and exits after an idle period.
    An in-flight key is removed before its request starts so another write
    during that request schedules a second notification.
    """

    def __init__(self, *, capacity: int = 128, idle_seconds: float = 30.0) -> None:
        self._capacity = capacity
        self._idle_seconds = idle_seconds
        self._pending: OrderedDict[tuple[str, str], _Refresh] = OrderedDict()
        self._condition = threading.Condition()
        self._worker: threading.Thread | None = None
        self._closed = False

    def enqueue(
        self,
        server: PlexServer,
        canonical_path: str,
        item_id: str | None,
        *,
        output_paths: tuple[Path, ...] = (),
        notification_token: str | None = None,
        source_fingerprint: SourceFingerprint | None = None,
    ) -> bool:
        """Queue a notification, returning False when capacity is exhausted."""
        key = (server.id, canonical_path)
        with self._condition:
            if self._closed:
                return False
            if key not in self._pending and len(self._pending) >= self._capacity:
                logger.warning(
                    "Plex notification queue is full; previews for {} were saved, but {} must scan/analyze "
                    "the file later to advertise them to clients. Pending notifications retry on the next job.",
                    canonical_path,
                    server.name,
                )
                return False
            previous = self._pending.get(key)
            self._pending[key] = _Refresh(
                server,
                canonical_path,
                item_id or (previous.item_id if previous else None),
                output_paths,
                notification_token,
                source_fingerprint,
            )
            if self._worker is None:
                self._worker = threading.Thread(target=self._run, name="plex-preview-notifications", daemon=True)
                self._worker.start()
            self._condition.notify()
            return True

    def _run(self) -> None:
        while True:
            with self._condition:
                if not self._pending and not self._closed:
                    self._condition.wait(timeout=self._idle_seconds)
                if self._closed or not self._pending:
                    self._worker = None
                    return
                _, request = self._pending.popitem(last=False)
            try:
                notified = request.server.refresh_preview_metadata(request.canonical_path, request.item_id)
                if notified is True and request.notification_token is not None:
                    clear_plex_refresh_pending(
                        request.output_paths,
                        request.canonical_path,
                        request.server.id,
                        request.notification_token,
                        source_fingerprint=request.source_fingerprint,
                    )
            except Exception as exc:
                logger.warning(
                    "Previews saved for {}, but {} could not be notified: {}. Plex can activate them on a later analyze.",
                    request.canonical_path,
                    request.server.name,
                    exc,
                )

    def close(self) -> None:
        """Discard memory work; journal markers retry on a later job after restart."""
        with self._condition:
            self._closed = True
            self._pending.clear()
            self._condition.notify_all()


_queue = PlexRefreshQueue()
atexit.register(_queue.close)


def enqueue_plex_refresh(
    server: PlexServer,
    canonical_path: str,
    item_id: str | None,
    *,
    output_paths: tuple[Path, ...] = (),
    notification_token: str | None = None,
    source_fingerprint: SourceFingerprint | None = None,
) -> bool:
    """Notify Plex asynchronously, acknowledging only this journal token."""
    return _queue.enqueue(
        server,
        canonical_path,
        item_id,
        output_paths=output_paths,
        notification_token=notification_token,
        source_fingerprint=source_fingerprint,
    )
