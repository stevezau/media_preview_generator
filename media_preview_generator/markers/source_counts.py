"""How many files each source decided in a job, per marker type: the job summary's "Decided by" counts (spec §7 item 5).

A file counts once per decided marker type, under one source group (:func:`source_group`). Only a run that decided the
file counts it: files needing review count only their decided types, and files that failed, weren't on disk, had no
server with Intro & Credits on, or were extras never count.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping

from .decide import DecisionStatus, TypeDecision
from .models import SERVER_SOURCES, Marker, MarkerType, Source
from .store import MarkerStore

_SERVER_SOURCE_IDS = frozenset(source.value for source in SERVER_SOURCES)


def source_group(marker: Marker) -> str:
    """The group one decided marker counts under: its sources' ids in the user's source order, joined by ``+``.

    Simplified so a job shows a few readable groups instead of every combination of sources:

    * The user's own (locked) marker is ``user``, whatever it was built from.
    * A marker a chapter set is ``chapters``: a chapter decides alone, and the other sources only confirmed or trimmed
      it.
    * Markers already on a server can only confirm, so they're named only when they were the one other source a single
      source needed (``theintrodb+server_markers``); both kinds count as ``server_markers``.

    Args:
        marker: A decided marker.

    Returns:
        The group, e.g. ``chapters``, ``credits_text``, ``theintrodb+introdb`` or ``theintrodb+server_markers``.
    """
    if marker.locked:
        return Source.USER.value
    sources = list(dict.fromkeys(marker.decided_by))
    if Source.CHAPTERS.value in sources:
        return Source.CHAPTERS.value
    deciding = [source for source in sources if source not in _SERVER_SOURCE_IDS]
    if len(deciding) == 1 and len(sources) > 1:
        return f"{deciding[0]}+{Source.SERVER_MARKERS.value}"
    return "+".join(deciding or [Source.SERVER_MARKERS.value])


def decided_groups(decisions: Mapping[MarkerType, TypeDecision]) -> dict[MarkerType, str]:
    """The group of every decided type of one file's decisions.

    Args:
        decisions: The file's decisions by type.

    Returns:
        The group per decided type; a type needing review, with no evidence or turned off is left out.
    """
    return {
        mtype: source_group(decision.marker)
        for mtype, decision in decisions.items()
        if decision.status is DecisionStatus.DECIDED and decision.marker is not None
    }


def stored_groups(store: MarkerStore, canonical_path: str) -> dict[MarkerType, str]:
    """:func:`decided_groups` from what the store holds for a file (a revived job's files settled before a restart).

    Args:
        store: The markers store.
        canonical_path: The file.

    Returns:
        The group per decided type; empty when the store doesn't know the file.
    """
    rec = store.get_file(canonical_path)
    if rec is None:
        return {}
    decided = {mtype for mtype, row in store.get_decisions(rec.id).items() if row.status is DecisionStatus.DECIDED}
    markers = store.get_markers(rec.id)
    return {mtype: source_group(markers[mtype]) for mtype in decided if mtype in markers}


class DecidedByTally:
    """One job's counts of files per marker type and source group; safe to add to from several threads."""

    def __init__(self) -> None:
        self._counts: dict[MarkerType, dict[str, int]] = {}
        self._lock = threading.Lock()

    def add(self, groups: Mapping[MarkerType, str]) -> None:
        """Count one file.

        Args:
            groups: The file's group per decided type (:func:`decided_groups`).
        """
        with self._lock:
            for mtype, group in groups.items():
                per_type = self._counts.setdefault(mtype, {})
                per_type[group] = per_type.get(group, 0) + 1

    def snapshot(self) -> dict[str, dict[str, int]]:
        """The counts so far, as stored on the job (``JobProgress.marker_sources``).

        Returns:
            ``{type: {group: files}}`` in marker type order, holding only types with a count.
        """
        with self._lock:
            return {mtype.value: dict(self._counts[mtype]) for mtype in MarkerType if self._counts.get(mtype)}
