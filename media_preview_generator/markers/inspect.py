"""Payloads for the server Edit tab (Intro & Credits status) and the Inspector tab (one file). No Flask here."""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import os
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

from loguru import logger

from ..config.paths import is_path_excluded
from ..servers.base import ServerConfig, ServerType
from ..servers.ownership import OwnershipMatch, apply_path_mappings, find_library_matches
from .models import Marker, MarkerType
from .publishers.base import Capability
from .publishers.factory import publisher_for
from .settings import ServerMarkersSettings, is_sports_library, library_allowed, load_server
from .sources.server_markers import read_server_markers
from .store import FileRecord, MarkerStore

_CAN_SHOW: dict[ServerType, tuple[str, ...]] = {
    ServerType.PLEX: ("intro", "credits"),
    ServerType.JELLYFIN: ("intro", "credits", "recap", "preview"),
    ServerType.EMBY: ("intro", "credits"),
}
EMBY_NEEDS_PLUGIN_MESSAGE = "Emby needs the Media Preview Bridge for Emby plugin (coming in the next phase)"
PLEX_SAME_HOST_PATH_HINT = (
    "Map Plex's config folder into both containers from the identical host path "
    "(on unRAID, don't mix /mnt/user and /mnt/cache)."
)
VERSIONS_DISAGREE_REASON = "versions don't agree yet"
SERVER_OFF_REASON = "Intro & Credits is off for this server"
LIBRARY_OFF_REASON = "This library isn't selected for Intro & Credits on this server"
# Servers report "credits to the end" differently (Plex's final flag, Emby has no end), and Plex stores credits 2 s
# away from what it serves, so shown times are compared within a second.
_SAME_TOLERANCE_MS = 1_000
_END_OF_FILE_MS = 2_000
CAPABILITY_TTL_S = 60.0
# Not a publisher Capability: the check itself failed, so nothing is known about the server.
CAPABILITY_UNKNOWN = "unknown"


class CapabilityCache:
    """Capability answers per server, reused for ``ttl_s``.

    The Edit tab and the Inspector ask on every load, and one Plex check can wait 30 s on a busy database. An entry
    only counts while the server's config is unchanged, so a saved server (switch, libraries, database folder, URL,
    credentials) is checked again at once; there is no settings-saved hook to clear it.
    """

    def __init__(self, ttl_s: float = CAPABILITY_TTL_S, clock: Callable[[], float] = time.monotonic) -> None:
        """Create an empty cache.

        Args:
            ttl_s: How long an answer is reused.
            clock: Monotonic seconds (tests inject a fake clock).
        """
        self.ttl_s = ttl_s
        self._clock = clock
        self._guard = threading.Lock()
        self._entries: dict[tuple[str, str], tuple[float, str, Any]] = {}
        self._locks: dict[tuple[str, str], threading.Lock] = {}

    def get(self, config: ServerConfig, variant: str, compute: Callable[[], Any]) -> Any:
        """The cached answer for ``config``'s server, computing it at most once at a time per server.

        Args:
            config: The server's config (its id is the key; the whole config must match).
            variant: ``status`` (checked as if on) or ``inspector`` (stored settings): different answers.
            compute: Produces the answer on a miss.

        Returns:
            A copy of the answer.
        """
        key = (config.id, variant)
        fingerprint = _config_fingerprint(config)
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            with self._guard:
                entry = self._entries.get(key)
                saved_since = entry is not None and entry[1] != fingerprint
                if saved_since:
                    del self._entries[key]
            if entry is not None and not saved_since and self._clock() - entry[0] < self.ttl_s:
                return copy.deepcopy(entry[2])
            try:
                value = compute()
                with self._guard:
                    self._entries[key] = (self._clock(), fingerprint, value)
            finally:
                if saved_since:
                    # The lock goes with the evicted answer; a thread already waiting on it still finds the new one.
                    with self._guard:
                        if self._locks.get(key) is lock:
                            del self._locks[key]
            return copy.deepcopy(value)

    def clear(self) -> None:
        """Forget every answer and its lock."""
        with self._guard:
            self._entries.clear()
            self._locks.clear()


def _config_fingerprint(config: ServerConfig) -> str:
    raw = json.dumps(dataclasses.asdict(config), sort_keys=True, default=str)
    return hashlib.sha1(raw.encode(), usedforsecurity=False).hexdigest()


_CAPABILITY_CACHE = CapabilityCache()


def clear_capability_cache() -> None:
    """Forget cached capability answers (tests, or after a change the config doesn't show)."""
    _CAPABILITY_CACHE.clear()


def _marker_dict(marker: Any) -> dict:
    return {"type": marker.type.value, "start_ms": marker.start_ms, "end_ms": marker.end_ms}


def _settings_block(config: ServerConfig, settings: ServerMarkersSettings) -> dict:
    block: dict[str, Any] = {
        "enabled": settings.enabled,
        "library_ids": list(settings.library_ids) if settings.library_ids is not None else None,
    }
    if config.type is ServerType.PLEX:
        block["plex"] = {
            "db_write_confirmed_at": settings.db_write_confirmed_at,
            "on_plex_redetect": settings.on_plex_redetect,
        }
    return block


def _capability(state: Capability | str, message: str, details: dict | None = None) -> dict:
    return {
        "state": state.value if isinstance(state, Capability) else state,
        "message": message,
        "details": details or {},
    }


def _plex_status(server: Any) -> dict:
    try:
        status = server.get_server_status()
    except Exception as exc:
        logger.debug("Plex status for the Intro & Credits tab failed: {}", type(exc).__name__)
        status = None
    status = status or {}
    return {"plex_pass": status.get("plex_pass"), "plex_version": status.get("version")}


def _preview_capability(server: Any, config: ServerConfig, settings: ServerMarkersSettings) -> dict:
    if not config.enabled:
        # A server turned off on the Servers page is never contacted.
        return _capability(Capability.DISABLED, "This server is turned off on the Servers page")
    if server is None:
        return _capability(Capability.MISCONFIGURED, "Couldn't set up a connection to this server; check its settings")
    try:
        return _CAPABILITY_CACHE.get(config, "status", lambda: _checked_as_if_on(server, config, settings))
    except Exception as exc:
        # Not cached: the next load checks again.
        logger.warning("Couldn't check whether {} can receive markers: {}", config.name, type(exc).__name__)
        return _capability(CAPABILITY_UNKNOWN, f"Couldn't check this server ({type(exc).__name__})")


def _checked_as_if_on(server: Any, config: ServerConfig, settings: ServerMarkersSettings) -> dict:
    # Checked as if on, so the tab can show Plex Pass, the database or the plugin before the switch is turned on.
    preview = dataclasses.replace(
        settings, enabled=True, db_write_confirmed_at=settings.db_write_confirmed_at or "preview"
    )
    publisher = publisher_for(server, config, settings=preview)
    if publisher is None:
        return _capability(Capability.NEEDS_PLUGIN, EMBY_NEEDS_PLUGIN_MESSAGE)
    report = publisher.capability()
    details = dict(report.details)
    if config.type is ServerType.PLEX:
        if "plex_version" not in details:
            details.update(_plex_status(server))
        if report.state is Capability.NEEDS_LOCAL_DB and details.get("lock_holder") is False:
            details["hint"] = PLEX_SAME_HOST_PATH_HINT
    return _capability(report.state, report.message, details)


def server_status_payload(server: Any, config: ServerConfig) -> dict:
    """Status block for one server's Intro & Credits tab.

    Args:
        server: Live client for ``config`` (None when the registry couldn't build one).
        config: The server's config.

    Returns:
        ``server_id``, ``server_type``, ``enabled``, ``settings`` (as the app reads them), ``capability`` (checked as
        if Intro & Credits were on; state ``unknown`` when the check itself failed), ``can_show`` and every library
        with its default selection.

    Raises:
        Exception: Only when the stored settings or libraries can't be read; a failing capability check doesn't.
    """
    settings = load_server(config.markers, config.type.value)
    return {
        "server_id": config.id,
        "server_type": config.type.value,
        "enabled": settings.enabled,
        "settings": _settings_block(config, settings),
        "capability": _preview_capability(server, config, settings),
        "can_show": list(_CAN_SHOW.get(config.type, ())),
        "libraries": [
            {
                "id": lib.id,
                "name": lib.name,
                "kind": lib.kind,
                "default_selected": not is_sports_library(lib.name, lib.kind),
            }
            for lib in config.libraries
        ],
    }


def resolve_local_path(server: Any, config: ServerConfig, item_id: str) -> str | None:
    """Local path of a server item: the first path-mapped candidate that is a file on disk.

    Args:
        server: Live client for ``config``.
        config: The server's config (its ``path_mappings``).
        item_id: The server's item id.

    Returns:
        The local file path, or None when the server doesn't know the item or no candidate exists here.
    """
    try:
        remote = server.resolve_item_to_remote_path(item_id)
    except Exception as exc:
        logger.debug("Item {} lookup on {} failed: {}", item_id, config.name, type(exc).__name__)
        return None
    if not remote:
        return None
    # apply_path_mappings reads both the current remote_prefix and the legacy plex_prefix mapping keys.
    for candidate in apply_path_mappings(remote, list(config.path_mappings or [])):
        if os.path.isfile(candidate):
            return candidate
    return None


def _owners(canonical_path: str, registry: Any) -> Iterator[tuple[ServerConfig, Any, list[OwnershipMatch]]]:
    # Same owners as the pipeline: every covering library whatever its preview opt-in, excluded paths left out.
    by_server: dict[str, list[OwnershipMatch]] = {}
    for match in find_library_matches(canonical_path, registry.configs()):
        by_server.setdefault(match.server_id, []).append(match)
    for server_id, matches in by_server.items():
        cfg = registry.get_config(server_id)
        if cfg is None or (cfg.exclude_paths and is_path_excluded(canonical_path, cfg.exclude_paths)):
            continue
        server = registry.get(server_id)
        if server is not None:
            yield cfg, server, matches


def _off_reason(cfg: ServerConfig, settings: ServerMarkersSettings, matches: list[OwnershipMatch]) -> str:
    if not settings.enabled:
        return SERVER_OFF_REASON
    kinds = {lib.id: lib.kind for lib in cfg.libraries}
    if any(
        library_allowed(settings, library_id=m.library_id, library_name=m.library_name, kind=kinds.get(m.library_id))
        for m in matches
    ):
        return ""
    return LIBRARY_OFF_REASON


def _capability_state(server: Any, cfg: ServerConfig) -> str:
    try:
        return _CAPABILITY_CACHE.get(cfg, "inspector", lambda: _checked_state(server, cfg))
    except Exception as exc:
        logger.warning("Couldn't check whether {} can receive markers: {}", cfg.name, type(exc).__name__)
        return CAPABILITY_UNKNOWN


def _checked_state(server: Any, cfg: ServerConfig) -> str:
    publisher = publisher_for(server, cfg)
    return Capability.NEEDS_PLUGIN.value if publisher is None else publisher.capability().state.value


def _item_id(server: Any, cfg: ServerConfig, canonical_path: str, known: str | None) -> str | None:
    if known:
        return known
    try:
        return server.resolve_remote_path_to_item_id(canonical_path)
    except Exception as exc:
        logger.debug("Item id lookup on {} failed for {}: {}", cfg.name, canonical_path, type(exc).__name__)
        return None


def _current(server: Any, cfg: ServerConfig, item_id: str | None, can_show: tuple[str, ...]) -> list[dict] | None:
    if not item_id:
        return None
    try:
        # What clients see now, ours included (for Jellyfin too): the Inspector shows the server's real state.
        found = read_server_markers(server, cfg, item_id, include_ours=True)
    except Exception as exc:
        logger.debug("Reading markers on {} failed for item {}: {}", cfg.name, item_id, type(exc).__name__)
        return None
    if found is None:
        return None
    return [_marker_dict(c) for c in found if c.type.value in can_show]


def _same(current: list[dict], wanted: list[dict], duration_ms: int) -> bool:
    if len(current) != len(wanted):
        return False

    def order(x: dict) -> tuple[str, int]:
        return x["type"], x["start_ms"]

    for c, w in zip(sorted(current, key=order), sorted(wanted, key=order), strict=True):
        if c["type"] != w["type"] or abs(c["start_ms"] - w["start_ms"]) > _SAME_TOLERANCE_MS:
            return False
        ends_inside = w["end_ms"] < duration_ms - _END_OF_FILE_MS
        if c["end_ms"] is not None and ends_inside and abs(c["end_ms"] - w["end_ms"]) > _SAME_TOLERANCE_MS:
            return False
    return True


def _plan(
    *,
    off_reason: str,
    wanted: list[Marker],
    ours: tuple[Marker, ...],
    current: list[dict] | None,
    waiting_on_versions: bool,
    duration_ms: int,
) -> tuple[str, str]:
    if off_reason:
        return "not_enabled", off_reason
    if not wanted:
        return ("will_remove", "") if ours else ("nothing_to_publish", "")
    if waiting_on_versions:
        return "waiting", VERSIONS_DISAGREE_REASON
    if current is None:
        return "unknown", ""
    # Only the types we manage here: Plex keeps its own marker of a type we didn't decide.
    managed = {m.type.value for m in wanted} | {m.type.value for m in ours}
    shown = [c for c in current if c["type"] in managed]
    if _same(shown, [_marker_dict(m) for m in wanted], duration_ms):
        return "up_to_date", ""
    return ("will_replace", "") if shown else ("will_add", "")


def _server_row(
    cfg: ServerConfig,
    server: Any,
    matches: list[OwnershipMatch],
    *,
    canonical_path: str,
    rec: FileRecord | None,
    markers: dict[MarkerType, Marker],
    store: MarkerStore,
) -> dict:
    settings = load_server(cfg.markers, cfg.type.value)
    off_reason = _off_reason(cfg, settings, matches)
    can_show = _CAN_SHOW.get(cfg.type, ())
    wanted = sorted((m for m in markers.values() if m.type.value in can_show), key=lambda m: (m.start_ms, m.type.value))
    file_state = store.get_publish_state(rec.id, cfg.id) if rec else None
    item_id = _item_id(server, cfg, canonical_path, file_state.item_id if file_state else None)
    item_state = store.get_item_publish_state(cfg.id, item_id) if item_id else None
    if item_state is not None:
        ours, publish_status = item_state.markers, item_state.status
    elif file_state is not None:
        ours, publish_status = file_state.markers, file_state.status
    else:
        ours, publish_status = (), None
    # Plex shows one set per item: a type this file decided but the item doesn't show after this file's last publish
    # (nothing changed since) waits for the item's other versions to agree.
    waiting_on_versions = bool(
        cfg.type is ServerType.PLEX
        and rec is not None
        and item_state is not None
        and store.get_publish_basis(rec.id, cfg.id) == (MarkerStore.markers_hash(wanted), item_state.version)
        and {m.type for m in wanted} - {m.type for m in item_state.markers}
    )
    current = _current(server, cfg, item_id, can_show)
    plan, reason = _plan(
        off_reason=off_reason,
        wanted=wanted,
        ours=ours,
        current=current,
        waiting_on_versions=waiting_on_versions,
        duration_ms=(rec.duration_ms or 0) if rec else 0,
    )
    return {
        "server_id": cfg.id,
        "server_name": cfg.name,
        "server_type": cfg.type.value,
        "markers_enabled": not off_reason,
        "capability_state": _capability_state(server, cfg),
        "can_show": list(can_show),
        "current": current,
        "published": [_marker_dict(m) for m in ours],
        "publish_status": publish_status,
        "publish_message": file_state.message if file_state else "",
        "plan": plan,
        "plan_reason": reason,
        "error": None,
    }


def _degraded_row(cfg: ServerConfig, exc: Exception) -> dict:
    """A row for a server whose state couldn't be read, so one server never breaks the whole Inspector."""
    # Exception text can carry paths or credentials: the row and the log name the exception type only.
    logger.warning("Inspector row for {} failed: {}", cfg.name, type(exc).__name__)
    return {
        "server_id": cfg.id,
        "server_name": cfg.name,
        "server_type": cfg.type.value,
        "markers_enabled": False,
        "capability_state": CAPABILITY_UNKNOWN,
        "can_show": list(_CAN_SHOW.get(cfg.type, ())),
        "current": None,
        "published": [],
        "publish_status": None,
        "publish_message": "",
        "plan": "unknown",
        "plan_reason": "",
        "error": f"Couldn't read this server's Intro & Credits state ({type(exc).__name__})",
    }


def item_payload(canonical_path: str, *, registry: Any, store: MarkerStore) -> dict:
    """Decisions, evidence and per-server state for one file (the Inspector's Intro & Credits tab).

    Args:
        canonical_path: Local path of the file (already validated by the caller).
        registry: The ``ServerRegistry``.
        store: The markers store.

    Returns:
        ``known``, ``canonical_path``, ``duration_ms``, ``is_movie``, ``decisions`` by type, ``evidence`` rows (empty
        lookups have ``type`` None) and one row per owning server with what it shows now (read live; None when that
        failed), what is ours there, and the ``plan``: ``will_add``, ``will_replace``, ``will_remove``, ``up_to_date``,
        ``waiting`` (Plex versions disagree), ``not_enabled``, ``nothing_to_publish``, or ``unknown`` (the server's
        markers couldn't be read, or its row failed: then ``error`` says why), with ``plan_reason``.
    """
    rec = store.get_file(canonical_path)
    decisions = store.get_decisions(rec.id) if rec else {}
    markers = store.get_markers(rec.id) if rec else {}
    payload: dict = {
        "known": rec is not None,
        "canonical_path": canonical_path,
        "duration_ms": rec.duration_ms if rec else None,
        "is_movie": rec.is_movie if rec else None,
        "decisions": {},
        "evidence": [],
        "servers": [],
    }
    for mtype in MarkerType:
        d = decisions.get(mtype)
        m = markers.get(mtype)
        payload["decisions"][mtype.value] = {
            "status": d.status.value if d else None,
            "reason": d.reason if d else "",
            "marker": {**_marker_dict(m), "decided_by": list(m.decided_by), "locked": m.locked} if m else None,
            "proposed": (
                {"start_ms": d.proposed_start_ms, "end_ms": d.proposed_end_ms}
                if d and d.proposed_start_ms is not None
                else None
            ),
        }
    if rec:
        payload["evidence"] = [
            {
                "source": r.source.value,
                "origin": r.origin,
                "type": r.type.value if r.type else None,
                "start_ms": r.start_ms,
                "end_ms": r.end_ms,
                "confidence": r.confidence,
                "detail": r.detail,
                "fetched_at": r.fetched_at,
            }
            for r in store.evidence_rows(rec.id)
        ]
    for cfg, server, matches in _owners(canonical_path, registry):
        try:
            row = _server_row(
                cfg, server, matches, canonical_path=canonical_path, rec=rec, markers=markers, store=store
            )
        except Exception as exc:
            row = _degraded_row(cfg, exc)
        payload["servers"].append(row)
    return payload
