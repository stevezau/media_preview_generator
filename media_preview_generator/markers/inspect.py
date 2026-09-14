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

from ..servers.base import ServerConfig, ServerType
from ..servers.ownership import OwnershipMatch, apply_path_mappings
from .models import Marker, MarkerType
from .outcomes import kept_note, with_kept_note
from .ownership import allowed_matches, owning_servers
from .publishers.base import Capability
from .publishers.factory import publisher_for
from .publishers.plex_db import SAME_HOST_PATH_ADVICE, versions_agree
from .settings import ServerMarkersSettings, is_sports_library, load_server
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
# A ready Plex whose Plex Pass check didn't answer: jobs wait (and retry) instead of writing.
PLEX_PASS_UNCHECKED_WARNING = "Can't reach Plex to confirm Plex Pass, so markers wait until Plex answers"
VERSIONS_DISAGREE_REASON = "versions don't agree yet"
SERVER_OFF_REASON = "Intro & Credits is off for this server"
LIBRARY_OFF_REASON = "This library isn't selected for Intro & Credits on this server"
# Servers report "credits to the end" differently (Plex leaves out the end of final credits, Emby has no end at all),
# and a file's runtime can differ slightly between servers, so shown times are compared within a second and an end
# within 2 s of the file's end counts as the end.
_SAME_TOLERANCE_MS = 1_000
_END_OF_FILE_MS = 2_000
CAPABILITY_TTL_S = 60.0
# Problems (plugin missing, server restarting after a plugin install) usually clear up soon.
NOT_READY_TTL_S = 5.0
# Not a publisher Capability: the check itself failed, so nothing is known about the server.
CAPABILITY_UNKNOWN = "unknown"


class CapabilityCache:
    """Capability answers per server: ready ones reused for ``ttl_s``, any other state (or a ready one with a warning)
    for ``not_ready_ttl_s``.

    The Edit tab and the Inspector ask on every load, and one Plex check can wait 30 s on a busy database. An entry
    only counts while the server's config is unchanged, so a saved server (switch, libraries, database folder, URL,
    credentials) is checked again at once; there is no settings-saved hook to clear it. A plugin install or uninstall
    isn't a config change, so those routes call ``forget``.
    """

    def __init__(
        self,
        ttl_s: float = CAPABILITY_TTL_S,
        clock: Callable[[], float] = time.monotonic,
        not_ready_ttl_s: float = NOT_READY_TTL_S,
    ) -> None:
        """Create an empty cache.

        Args:
            ttl_s: How long a ready answer is reused.
            clock: Monotonic seconds (tests inject a fake clock).
            not_ready_ttl_s: How long any other answer is reused.
        """
        self.ttl_s = ttl_s
        self.not_ready_ttl_s = not_ready_ttl_s
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
            if entry is not None and not saved_since and self._clock() - entry[0] < self._ttl_for(entry[2]):
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

    def _ttl_for(self, answer: Any) -> float:
        # The status tab caches a capability dict, the Inspector just the state.
        if isinstance(answer, dict):
            ready = answer.get("state") == Capability.READY.value and not answer.get("warning")
        else:
            ready = answer == Capability.READY.value
        return self.ttl_s if ready else self.not_ready_ttl_s

    def forget(self, server_id: str) -> None:
        """Forget one server's answers (both variants), so its next load checks it again.

        Args:
            server_id: The server's id.
        """
        with self._guard:
            for key in [k for k in self._entries if k[0] == server_id]:
                del self._entries[key]

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


def forget_capability(server_id: str) -> None:
    """Forget one server's cached capability answers, after a change its config doesn't show (plugin install).

    Args:
        server_id: The server's id.
    """
    _CAPABILITY_CACHE.forget(server_id)


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


def _capability(state: Capability | str, message: str, details: dict | None = None, warning: str = "") -> dict:
    return {
        "state": state.value if isinstance(state, Capability) else state,
        "message": message,
        "details": details or {},
        "warning": warning,
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
    warning = ""
    if config.type is ServerType.PLEX:
        if "plex_version" not in details:
            details.update(_plex_status(server))
        needs_same_path = report.state is Capability.NEEDS_LOCAL_DB and details.get("lock_holder") is False
        if needs_same_path and SAME_HOST_PATH_ADVICE not in report.message:
            details["hint"] = PLEX_SAME_HOST_PATH_HINT
        if report.ready and details.get("plex_pass") is None:
            warning = PLEX_PASS_UNCHECKED_WARNING
    return _capability(report.state, report.message, details, warning)


def server_status_payload(server: Any, config: ServerConfig) -> dict:
    """Status block for one server's Intro & Credits tab.

    Args:
        server: Live client for ``config`` (None when the registry couldn't build one).
        config: The server's config.

    Returns:
        ``server_id``, ``server_type``, ``enabled``, ``settings`` (as the app reads them), ``capability`` (checked as
        if Intro & Credits were on; state ``unknown`` when the check itself failed; ``warning`` set when a ready server
        still won't be written, i.e. Plex Pass couldn't be checked), ``can_show`` and every library with its default
        selection.

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
    # The pipeline's owners: every covering library whatever its preview opt-in, excluded paths left out.
    yield from owning_servers(canonical_path, registry)


def _off_reason(cfg: ServerConfig, settings: ServerMarkersSettings, matches: list[OwnershipMatch]) -> str:
    if not settings.enabled:
        return SERVER_OFF_REASON
    # The library rule marker_matches applies, on the matches already found.
    return "" if allowed_matches(cfg, matches) else LIBRARY_OFF_REASON


def _capability_state(server: Any, cfg: ServerConfig) -> str:
    try:
        return _CAPABILITY_CACHE.get(cfg, "inspector", lambda: _checked_state(server, cfg))
    except Exception as exc:
        logger.warning("Couldn't check whether {} can receive markers: {}", cfg.name, type(exc).__name__)
        return CAPABILITY_UNKNOWN


def _checked_state(server: Any, cfg: ServerConfig) -> str:
    publisher = publisher_for(server, cfg)
    return Capability.NEEDS_PLUGIN.value if publisher is None else publisher.capability().state.value


def _item_id(
    server: Any, cfg: ServerConfig, canonical_path: str, known: str | None, matches: list[OwnershipMatch]
) -> str | None:
    if known:
        return known
    try:
        # The libraries holding the file, as the pipeline looks it up (Plex otherwise searches preview libraries only).
        return server.resolve_remote_path_to_item_id(canonical_path, library_ids=[m.library_id for m in matches])
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


def _version_count(server: Any, cfg: ServerConfig, item_id: str | None) -> int | None:
    """How many versions a Plex item has (Plex shows one marker set for all of them); None when not known."""
    if cfg.type is not ServerType.PLEX or not item_id:
        return None
    try:
        return server.get_version_count(item_id)
    except Exception as exc:
        logger.debug("Reading the versions of item {} on {} failed: {}", item_id, cfg.name, type(exc).__name__)
        return None


def _same_end(shown_end: int | None, wanted_end: int, duration_ms: int, server_type: ServerType) -> bool:
    to_the_end = wanted_end >= duration_ms - _END_OF_FILE_MS
    if shown_end is None:
        # Plex leaves out the end of credits that run to the end; Emby's markers never have an end to compare.
        return server_type is ServerType.EMBY or to_the_end
    if to_the_end and shown_end >= duration_ms - _END_OF_FILE_MS:
        return True
    return abs(shown_end - wanted_end) <= _SAME_TOLERANCE_MS


def _same(current: list[dict], wanted: list[dict], duration_ms: int, server_type: ServerType) -> bool:
    if len(current) != len(wanted):
        return False

    def order(x: dict) -> tuple[str, int]:
        return x["type"], x["start_ms"]

    for c, w in zip(sorted(current, key=order), sorted(wanted, key=order), strict=True):
        if c["type"] != w["type"] or abs(c["start_ms"] - w["start_ms"]) > _SAME_TOLERANCE_MS:
            return False
        if not _same_end(c["end_ms"], w["end_ms"], duration_ms, server_type):
            return False
    return True


def _matching(shown: list[dict], expected: list[dict], duration_ms: int, server_type: ServerType) -> list[dict] | None:
    """The shown markers that match ``expected`` one for one; None when one of ``expected`` isn't shown."""
    pool = list(shown)
    matched = []
    for want in expected:
        found = next((c for c in pool if _same([c], [want], duration_ms, server_type)), None)
        if found is None:
            return None
        pool.remove(found)
        matched.append(found)
    return matched


def _kept_on_plex(
    current: list[dict],
    wanted: list[Marker],
    ours: tuple[Marker, ...],
    recorded: frozenset[MarkerType],
    duration_ms: int,
) -> frozenset[MarkerType]:
    """The types a Plex server set to "Keep Plex's" leaves as its own on the next run (``plex_db._kept_types``).

    Kept on an earlier run, or a decided (or recorded) type Plex shows markers of that are neither what we'd write nor
    what we left there, while Plex still shows markers of the type. A recorded type no longer decided whose markers
    aren't ours is left alone by the job, so it is left out here too.
    """
    kept = set()
    for mtype in MarkerType:
        now = [c for c in current if c["type"] == mtype.value]
        if not now:
            continue
        want = [_marker_dict(m) for m in wanted if m.type is mtype]
        mine = [_marker_dict(m) for m in ours if m.type is mtype]
        # _same never matches an empty list against shown markers.
        provably_ours = _same(now, want, duration_ms, ServerType.PLEX) or _same(now, mine, duration_ms, ServerType.PLEX)
        if mtype in recorded or ((want or mine) and not provably_ours):
            kept.add(mtype)
    return frozenset(kept)


def _expected(
    server_type: ServerType, wanted: list[Marker], ours: tuple[Marker, ...], shown: list[dict], duration_ms: int
) -> list[dict]:
    """What the server should show once published, per type.

    The Plex publisher keeps (and writes back) what is already ours on the item when it agrees with the decision
    within its version tolerance, so that is what the server should show, whatever it shows now.
    """
    expected = []
    for mtype in dict.fromkeys(m.type for m in wanted):
        want = [m for m in wanted if m.type is mtype]
        kept = [m for m in ours if m.type is mtype]
        if server_type is ServerType.PLEX and kept and versions_agree(kept, want):
            expected.extend(_marker_dict(m) for m in kept)
        else:
            expected.extend(_marker_dict(m) for m in want)
    return expected


def _plan(
    *,
    server_type: ServerType,
    off_reason: str,
    wanted: list[Marker],
    ours: tuple[Marker, ...],
    current: list[dict] | None,
    waiting_on_versions: bool,
    duration_ms: int,
    keep_plex: bool = False,
    recorded_kept: frozenset[MarkerType] = frozenset(),
) -> tuple[str, str]:
    if off_reason:
        return "not_enabled", off_reason
    if not wanted:
        return ("will_remove", "") if ours else ("nothing_to_publish", "")
    kept = (
        _kept_on_plex(current, wanted, ours, recorded_kept, duration_ms)
        if keep_plex and current is not None
        else frozenset()
    )
    note = kept_note(kept, wanted)
    if waiting_on_versions:
        return "waiting", with_kept_note(VERSIONS_DISAGREE_REASON, note)
    if current is None:
        return "unknown", ""
    # Plex's own markers of a kept type stay whatever this file decided: compare only the other types.
    wanted = [m for m in wanted if m.type not in kept]
    ours = tuple(m for m in ours if m.type not in kept)
    # Only the types we manage here: Plex keeps its own marker of a type we didn't decide.
    managed = {m.type.value for m in wanted} | {m.type.value for m in ours}
    shown = [c for c in current if c["type"] in managed]
    expected = _expected(server_type, wanted, ours, shown, duration_ms)
    if server_type is ServerType.JELLYFIN:
        # Jellyfin serves every provider's segments side by side: another provider's beside ours changes nothing
        # (the job reads ours as shown and sends nothing).
        matched = _matching(shown, expected, duration_ms, server_type)
        if matched is not None:
            shown = matched
    # Compared within _SAME_TOLERANCE_MS where a job compares Plex's rows exactly: a Plex marker within a second of
    # ours reads "Up to date" here while the job counts it as Plex's. Plex's own detection doesn't land that close.
    if _same(shown, expected, duration_ms, server_type):
        return ("keeps_plex", with_kept_note("", note)) if note else ("up_to_date", "")
    return ("will_replace" if shown else "will_add"), with_kept_note("", note)


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
    item_id = _item_id(server, cfg, canonical_path, file_state.item_id if file_state else None, matches)
    item_state = store.get_item_publish_state(cfg.id, item_id) if item_id else None
    if item_state is not None:
        ours = item_state.markers
    elif file_state is not None:
        ours = file_state.markers
    else:
        ours = ()
    # Nothing changed since this file last published there (the pipeline's "unchanged" test).
    published_unchanged = bool(
        rec is not None
        and item_state is not None
        and store.get_publish_basis(rec.id, cfg.id) == (MarkerStore.markers_hash(wanted), item_state.version)
    )
    # Plex shows one set per item: a type this file decided but the item doesn't show after this file's last publish
    # waits for the item's other versions to agree.
    # Kept types are Plex's own, not missing from ours.
    waiting_on_versions = bool(
        cfg.type is ServerType.PLEX
        and published_unchanged
        and {m.type for m in wanted} - {m.type for m in item_state.markers} - item_state.kept_types
    )
    current = _current(server, cfg, item_id, can_show)
    plan, reason = _plan(
        server_type=cfg.type,
        off_reason=off_reason,
        wanted=wanted,
        ours=ours,
        current=current,
        waiting_on_versions=waiting_on_versions,
        duration_ms=(rec.duration_ms or 0) if rec else 0,
        # Every job leaves Plex's own re-detected markers alone, type by type ("Keep Plex's").
        keep_plex=cfg.type is ServerType.PLEX and settings.on_plex_redetect == "keep_plex",
        recorded_kept=item_state.kept_types if item_state is not None else frozenset(),
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
        # This file's last attempt; the item row can also reflect another version's attempt on a shared Plex item.
        "publish_status": file_state.status if file_state else None,
        "publish_message": file_state.message if file_state else "",
        "item_status": item_state.status if item_state else None,
        "plan": plan,
        "plan_reason": reason,
        "version_count": _version_count(server, cfg, item_id),
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
        "item_status": None,
        "plan": "unknown",
        "plan_reason": "",
        "version_count": None,
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
        failed), what is ours there, this file's last publish (``publish_status``, ``publish_message``), the server
        item's last publish (``item_status``; another version of a shared Plex item may have written or failed
        since), and the ``plan``: ``will_add``, ``will_replace``, ``will_remove``, ``up_to_date``, ``waiting`` (Plex
        versions disagree), ``keeps_plex`` (Plex's own detection replaced ours and the server is set to keep them;
        ``plan_reason`` names the kept types, also on other plans),
        ``not_enabled``, ``nothing_to_publish``, or ``unknown`` (the server's markers couldn't be read, or its row
        failed: then ``error`` says why), with ``plan_reason``, and ``version_count`` (a Plex item's versions, which
        share one marker set; None for other servers or when it couldn't be read).
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
