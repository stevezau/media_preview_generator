"""Payloads for the server Edit tab (Intro & Credits status) and the Inspector's Intro & Credits tab (one file, or the
Season view of its season). No Flask here."""

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
from .audio.season import folder_videos, season_group, season_size
from .decide import DecisionStatus, shortened_by
from .external_ids import ids_from_path, is_season_folder
from .models import SERVER_SOURCES, Marker, MarkerType, Source
from .outcomes import kept_note, with_kept_note
from .ownership import allowed_matches, owning_servers
from .publishers.base import Capability, versions_agree
from .publishers.emby import CREDENTIALS_REJECTED, credits_note
from .publishers.factory import publisher_for
from .publishers.plex_db import SAME_HOST_PATH_ADVICE
from .settings import ServerMarkersSettings, is_sports_library, load_server
from .sources.server_markers import read_server_markers
from .store import FileRecord, MarkerStore

_CAN_SHOW: dict[ServerType, tuple[str, ...]] = {
    ServerType.PLEX: ("intro", "credits"),
    ServerType.JELLYFIN: ("intro", "credits", "recap", "preview"),
    ServerType.EMBY: ("intro", "credits"),
}
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
    if config.type is ServerType.EMBY:
        block["emby"] = {"on_emby_redetect": settings.on_emby_redetect}
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
        return _capability(Capability.NEEDS_PLUGIN, "No marker publisher for this server type")
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
    # Not asked of an Emby that can't be reached or rejects the credentials: the read would only fail, and slowly.
    emby_answers = report.state is not Capability.UNREACHABLE and report.message != CREDENTIALS_REJECTED
    if config.type is ServerType.EMBY and emby_answers:
        registered = _emby_intro_skip_registered(server, config)
        if registered is not None:
            details["intro_skip_registered"] = registered
    return _capability(report.state, report.message, details, warning)


def _emby_intro_skip_registered(server: Any, config: ServerConfig) -> bool | None:
    # Only a note on the tab: a failed read shows nothing and never fails the status.
    try:
        registered = server.intro_skip_registered()
    except Exception as exc:
        logger.debug("Couldn't read {}'s Emby Premiere registration: {}", config.name, type(exc).__name__)
        return None
    return registered if isinstance(registered, bool) else None


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
    """Local path of a server item: the first path-mapped candidate of one of its versions that is a file on disk.

    A version asked for by its own id (a Jellyfin version) comes first; then the item's versions in the server's
    order, so a Plex item (every version shares its id) whose first version isn't on this disk opens another.

    Args:
        server: Live client for ``config``.
        config: The server's config (its ``path_mappings``).
        item_id: The server's item id.

    Returns:
        The local file path, or None when the server doesn't know the item or no version's file exists here.
    """
    try:
        versions = list(server.resolve_item_to_remote_paths(item_id) or [])
    except Exception as exc:
        logger.debug("Item {} lookup on {} failed: {}", item_id, config.name, type(exc).__name__)
        return None
    versions.sort(key=lambda version: version[0] != item_id)  # stable: the server's order otherwise
    for _version_id, remote in versions:
        if not remote:
            continue
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
    # Only the state is shown: Plex's own detection settings (one more Plex request) are the Edit tab's.
    publisher = publisher_for(server, cfg, ui_details=False)
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


def _kept_on_server(
    current: list[dict],
    wanted: list[Marker],
    ours: tuple[Marker, ...],
    recorded: frozenset[MarkerType],
    duration_ms: int,
    server_type: ServerType,
) -> frozenset[MarkerType]:
    """The types a server set to keep its own markers ("Keep Plex's", "Keep Emby's") leaves as its own on the next run.

    Kept on an earlier run, or a decided (or recorded) type the server shows markers of that are neither what we'd
    write nor what we left there, while the server still shows markers of the type (``plex_db._kept_types``; Emby's
    plugin leaves such a type alone). A recorded type no longer decided whose markers aren't ours is left alone by the
    job, so it is left out here too.
    """
    kept = set()
    for mtype in MarkerType:
        now = [c for c in current if c["type"] == mtype.value]
        if not now:
            continue
        want = [_marker_dict(m) for m in wanted if m.type is mtype]
        mine = [_marker_dict(m) for m in ours if m.type is mtype]
        # _same never matches an empty list against shown markers.
        shows_decision = _same(now, want, duration_ms, server_type)
        provably_ours = shows_decision or _same(now, mine, duration_ms, server_type)
        # Plex keeps a kept type whatever it shows. Emby's plugin writes ours back for a kept type once a refresh has
        # deleted Emby's rows: rows showing the decision are ours again, and the job records them so.
        recorded_kept = mtype in recorded and not (server_type is ServerType.EMBY and shows_decision)
        if recorded_kept or ((want or mine) and not provably_ours):
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
    keep_own: bool = False,
    recorded_kept: frozenset[MarkerType] = frozenset(),
) -> tuple[str, str]:
    if off_reason:
        return "not_enabled", off_reason
    if not wanted:
        return ("will_remove", "") if ours else ("nothing_to_publish", "")
    kept = (
        _kept_on_server(current, wanted, ours, recorded_kept, duration_ms, server_type)
        if keep_own and current is not None
        else frozenset()
    )
    vendor = server_type.value.capitalize()
    note = kept_note(kept, wanted, vendor)
    # Emby's credits skip runs to the end of the file, past a scene after credits that end earlier.
    shown_note = (
        credits_note([m for m in wanted if m.type not in kept], duration_ms) if server_type is ServerType.EMBY else ""
    )
    if waiting_on_versions:
        return "waiting", with_kept_note(VERSIONS_DISAGREE_REASON, note)
    if current is None:
        return "unknown", shown_note
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
    reason = with_kept_note(with_kept_note("", note), shown_note)
    if _same(shown, expected, duration_ms, server_type):
        return (f"keeps_{server_type.value}" if note else "up_to_date"), reason
    return ("will_replace" if shown else "will_add"), reason


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
        # Every job leaves the server's own markers alone, type by type ("Keep Plex's", "Keep Emby's").
        keep_own=settings.keeps_server_markers,
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


def _shortened_by(decision: Any, registry: Any) -> dict | None:
    """The servers whose own markers shortened a decided credits/preview start (spec §5.5 rule 7), by name."""
    server_ids = shortened_by(decision.reason) if decision.status is DecisionStatus.DECIDED else None
    if server_ids is None:
        return None
    names = []
    for server_id in server_ids:
        cfg = registry.get_config(server_id)
        names.append(cfg.name if cfg is not None and cfg.name else server_id)
    return {"servers": names}


def _decision_dict(decision: Any, marker: Marker | None) -> dict:
    """One type's stored decision in the Inspector's shape (``status`` None when nothing is stored)."""
    return {
        "status": decision.status.value if decision else None,
        "reason": decision.reason if decision else "",
        "marker": {**_marker_dict(marker), "decided_by": list(marker.decided_by), "locked": marker.locked}
        if marker
        else None,
        "proposed": (
            {"start_ms": decision.proposed_start_ms, "end_ms": decision.proposed_end_ms}
            if decision and decision.proposed_start_ms is not None
            else None
        ),
    }


def item_payload(canonical_path: str, *, registry: Any, store: MarkerStore) -> dict:
    """Decisions, evidence and per-server state for one file (the Inspector's Intro & Credits tab).

    Args:
        canonical_path: Local path of the file (already validated by the caller).
        registry: The ``ServerRegistry``.
        store: The markers store.

    Returns:
        ``known``, ``canonical_path``, ``duration_ms``, ``is_movie``, ``decisions`` by type (``shortened_by``:
        ``{"servers": [names]}`` when the servers' own markers shortened a decided credits/preview start, else None), ``evidence`` rows (empty
        lookups have ``type`` None) and one row per owning server with what it shows now (read live; None when that
        failed), what is ours there, this file's last publish (``publish_status``, ``publish_message``), the server
        item's last publish (``item_status``; another version of a shared Plex item may have written or failed
        since), and the ``plan``: ``will_add``, ``will_replace``, ``will_remove``, ``up_to_date``, ``waiting`` (Plex
        versions disagree), ``keeps_plex`` / ``keeps_emby`` (the server shows its own markers of a decided type and is
        set to keep them; ``plan_reason`` names the kept types, also on other plans, and on Emby says when decided
        credits end before the file does),
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
        payload["decisions"][mtype.value] = {
            **_decision_dict(d, markers.get(mtype)),
            "shortened_by": _shortened_by(d, registry) if d else None,
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
                "label": r.label,
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


_SEASON_TYPES = (MarkerType.INTRO, MarkerType.CREDITS)
_SEASON_AUDIO_SOURCES = frozenset({Source.SEASON_AUDIO, Source.SEASON_AUDIO_PREVIOUS})
_DOT_STATES = {"written": "ok", "waiting": "waiting", "failed": "failed", "skipped": "skipped"}


def _episode_label(path: str) -> str | None:
    ids = ids_from_path(path)
    return f"E{ids.episode:02d}" if ids.is_episode and ids.episode is not None else None


def _chips(rows: list[Any]) -> list[dict]:
    """Evidence chips: each source with intro or credits evidence, once, in stored order.

    Markers already on servers are the dots, not chips. Only season audio's label ("10/10") is shown; a chapter title
    belongs to the episode tab.
    """
    chips: dict[str, str] = {}
    for row in rows:
        if row.type in _SEASON_TYPES and row.source not in SERVER_SOURCES:
            chips.setdefault(row.source.value, row.label if row.source in _SEASON_AUDIO_SOURCES else "")
    return [{"source": source, "label": label} for source, label in chips.items()]


def _season_header(episode: str, folder: str) -> tuple[str, str]:
    """The show and season the Season view names, as the Publish job names them (``triggers._season_job_name``).

    The show is the season folder's parent, or the folder itself when episodes sit straight in the show folder; the
    season comes from the episode's name ("Season 2" / "Specials"), so two seasons of one flat folder read apart.
    """
    show_folder = os.path.dirname(folder) if is_season_folder(os.path.basename(folder)) else folder
    season = ids_from_path(episode).season
    if season is None:
        label = os.path.basename(folder)
    else:
        label = "Specials" if season == 0 else f"Season {season}"
    return os.path.basename(show_folder), label


def _dot(cfg: ServerConfig, matches: list[OwnershipMatch], rec: FileRecord | None, store: MarkerStore) -> dict:
    """One server's dot for one episode, from its last publish there."""
    if not allowed_matches(cfg, matches):
        return {"state": "off", "message": ""}
    row = store.get_publish_state(rec.id, cfg.id) if rec else None
    if row is None:
        return {"state": "none", "message": ""}
    state = _DOT_STATES.get(row.status, "none")
    if state == "ok" and not row.markers:
        state = "none"
    return {"state": state, "message": row.message}


def season_payload(canonical_path: str, *, registry: Any, store: MarkerStore) -> dict:
    """The Season view: every episode of a file's season group, from markers.db only (no live server reads).

    Args:
        canonical_path: An episode's local path (already validated by the caller).
        registry: The ``ServerRegistry``.
        store: The markers store.

    Returns:
        ``folder``; ``show`` and ``season`` as the Publish job names them (the show folder's name; "Season N" or
        "Specials"); ``servers`` (the enabled servers owning the asked file, in registry order, with
        ``markers_enabled``: Intro & Credits on there and its library selected); ``episodes`` (the season group's
        files, sorted; each with ``path``, ``name``, ``episode`` "E01", ``known``, ``duration_ms``, ``intro`` and
        ``credits`` in ``item_payload``'s decision shape without ``shortened_by``, ``needs_review`` (any marker type
        in Needs review) with ``review_reason`` (the first such type's reason, intro first), ``evidence`` chips
        ``{source, label}``, and ``servers`` dots ``{server_id: {state, message}}``: ``off`` (Intro & Credits off
        there, or this episode's library not selected or excluded), ``ok`` (last publish wrote markers of ours),
        ``none`` (written with nothing of ours, or never published), ``waiting``, ``failed`` or ``skipped``); and
        ``counts``: ``episodes`` (the files listed), ``total_episodes`` (the season's size before the 40-nearest
        cap), ``ready`` (at least one decided marker of any type: what Publish sends, even when another type is in
        Needs review) and ``needs_review`` (any type in Needs review).
    """
    videos = folder_videos(os.path.dirname(canonical_path))
    group = season_group(canonical_path, videos)
    owners = list(_owners(canonical_path, registry))
    servers = [
        {
            "server_id": cfg.id,
            "server_name": cfg.name,
            "server_type": cfg.type.value,
            "markers_enabled": bool(allowed_matches(cfg, matches)),
        }
        for cfg, _server, matches in owners
    ]
    episodes, ready, review = [], 0, 0
    for path in group.episodes:
        rec = store.get_file(path)
        decisions = store.get_decisions(rec.id) if rec else {}
        markers = store.get_markers(rec.id) if rec else {}
        types = {mtype.value: _decision_dict(decisions.get(mtype), markers.get(mtype)) for mtype in _SEASON_TYPES}
        # Counted over every type, not only the two columns: a job publishes each decided type of a file even while
        # another type (a recap, say) is in Needs review, and that file's job row then says Needs review.
        in_review = [
            decisions[mtype]
            for mtype in MarkerType
            if mtype in decisions and decisions[mtype].status is DecisionStatus.NEEDS_REVIEW
        ]
        review += int(bool(in_review))
        ready += int(bool(markers))
        # Per episode: a server's exclude rules can leave out single files.
        matches_by_server = {cfg.id: matches for cfg, _server, matches in _owners(path, registry)}
        episodes.append(
            {
                "path": path,
                "name": os.path.basename(path),
                "episode": _episode_label(path),
                "known": rec is not None,
                "duration_ms": rec.duration_ms if rec else None,
                **types,
                "needs_review": bool(in_review),
                "review_reason": next((d.reason for d in in_review if d.reason), ""),
                "evidence": _chips(store.evidence_rows(rec.id)) if rec else [],
                "servers": {
                    cfg.id: _dot(cfg, matches_by_server.get(cfg.id, []), rec, store) for cfg, _server, _m in owners
                },
            }
        )
    show, season = _season_header(canonical_path, group.folder)
    return {
        "folder": group.folder,
        "show": show,
        "season": season,
        "servers": servers,
        "episodes": episodes,
        "counts": {
            "episodes": len(episodes),
            "total_episodes": season_size(canonical_path, videos),
            "ready": ready,
            "needs_review": review,
        },
    }
