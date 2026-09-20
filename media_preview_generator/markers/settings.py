"""Intro & Credits settings: defaults, validation and typed accessors.

Global detection settings live in ``settings.json["markers"]``; what each server receives lives in
``media_servers[].markers`` (spec §8). Validation normalises both blocks so readers never see partial shapes.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from .sources.theintrodb import sendable_api_key

SOURCE_IDS: tuple[str, ...] = (
    "chapters",
    "theintrodb",
    "introdb",
    "skipdb",
    "season_audio",
    "credits_text",
    "server_markers",
)
PUBLISH_WHEN_VALUES: tuple[str, ...] = ("high", "medium")
ON_PLEX_REDETECT_VALUES: tuple[str, ...] = ("restore", "keep_plex")
ON_EMBY_REDETECT_VALUES: tuple[str, ...] = ("restore", "keep_emby")
SECRET_MASK = "****"
_API_KEY_MAX_LEN = 200
# Sports libraries are excluded by default: no source covers them (spec §4). Name-based because no vendor
# exposes a "sports" library kind; an explicit library_ids choice always wins.
_SPORTS_NAME_RE = re.compile(r"\bsports?\b", re.IGNORECASE)

DEFAULT_GLOBAL_MARKERS: dict[str, Any] = {
    "detect": {"intro": True, "credits": True, "recap": False},
    "publish_when": "high",
    "respect_locks": True,
    "sources": [
        {"id": "chapters", "enabled": True},
        {"id": "theintrodb", "enabled": False, "api_key": ""},
        {"id": "introdb", "enabled": True},
        {"id": "skipdb", "enabled": True},
        {"id": "season_audio", "enabled": True},
        {"id": "credits_text", "enabled": True},
        {"id": "server_markers", "enabled": True},
    ],
}


def default_server_markers(server_type: str) -> dict[str, Any]:
    """Return the default per-server ``markers`` block for a server type.

    Args:
        server_type: ``plex``, ``emby`` or ``jellyfin``.

    Returns:
        A fresh dict; Plex servers also get the ``plex`` sub-block, Emby servers the ``emby`` one.
    """
    block: dict[str, Any] = {"enabled": False, "library_ids": None}
    if server_type == "plex":
        block["plex"] = {"db_write_confirmed_at": None, "on_plex_redetect": "restore"}
    if server_type == "emby":
        block["emby"] = {"on_emby_redetect": "restore"}
    return block


@dataclass(frozen=True)
class SourceSetting:
    """One evidence source as configured by the user."""

    id: str
    enabled: bool
    # repr=False: this dataclass's default repr is used in log lines and test-failure output
    # (e.g. an assertion diff on a GlobalMarkersSettings), and it must never print the secret.
    api_key: str = field(default="", repr=False)


@dataclass(frozen=True)
class GlobalMarkersSettings:
    """Typed view of ``settings.json["markers"]``."""

    detect_intro: bool
    detect_credits: bool
    detect_recap: bool
    publish_when: str
    respect_locks: bool
    sources: tuple[SourceSetting, ...]

    def source(self, source_id: str) -> SourceSetting | None:
        """Return the setting for ``source_id`` or None."""
        return next((s for s in self.sources if s.id == source_id), None)

    def source_enabled(self, source_id: str) -> bool:
        """Whether ``source_id`` is enabled."""
        s = self.source(source_id)
        return bool(s and s.enabled)

    def ordered_enabled_sources(self) -> tuple[str, ...]:
        """Enabled source ids in the user's order (also the timestamp precedence order)."""
        return tuple(s.id for s in self.sources if s.enabled)

    def detection_fingerprint(self) -> str:
        """Hash of everything that changes a decision (excludes secrets).

        ``bool(s.api_key)`` — never the key itself — is part of the hash: TheIntroDB going from
        "no key" to "has a key" (or vice versa) flips it from unusable to usable, which changes
        what evidence a re-detection run can gather, even though ``enabled`` didn't change. Two
        different real keys hash identically, so rotating the key alone doesn't force
        re-detection.

        ``respect_locks`` is deliberately left out, and that is not an oversight. A lock is applied
        after the rules have run — ``decide()`` takes it as a separate argument and a locked marker
        wins whatever the evidence says (spec §5.5 rule 1) — so the setting changes nothing this
        hash is for: whether a file's *stored decision* was reached under different rules and has to
        be reached again. Including it would restamp every decision row in the library, and move
        every ``decided_at``, the first time the switch is flipped, without changing a single
        answer.

        Returns:
            A stable sha1 hex digest.
        """
        payload = {
            "detect": [self.detect_intro, self.detect_credits, self.detect_recap],
            "publish_when": self.publish_when,
            "sources": [[s.id, s.enabled, bool(s.api_key)] for s in self.sources],
        }
        return hashlib.sha1(json.dumps(payload, sort_keys=True).encode(), usedforsecurity=False).hexdigest()


@dataclass(frozen=True)
class ServerMarkersSettings:
    """Typed view of ``media_servers[].markers``."""

    enabled: bool
    library_ids: tuple[str, ...] | None
    db_write_confirmed_at: str | None
    on_plex_redetect: str
    on_emby_redetect: str = "restore"

    @property
    def keeps_server_markers(self) -> bool:
        """Whether the server's own markers of a type stay instead of ours ("Keep Plex's", "Keep Emby's").

        Each server type loads only its own block, so the other vendor's field is always its default.
        """
        return self.on_plex_redetect == "keep_plex" or self.on_emby_redetect == "keep_emby"


def _normalise_sources(raw_sources: Any, existing_sources: Any) -> tuple[list[dict] | None, str]:
    """Validate a posted ``sources`` list and fill in defaults for anything the user omitted.

    Three things happen here that aren't obvious from the validation loop alone:

    1. **Default injection** — any ``SOURCE_IDS`` entry missing from ``raw_sources`` (including
       the whole list being omitted, i.e. ``None``) is appended from ``DEFAULT_GLOBAL_MARKERS``,
       so callers never have to handle a partial list.
    2. **Ordering** — the user's posted order is preserved for entries they sent; only the
       appended defaults fall back to ``DEFAULT_GLOBAL_MARKERS``'s order. This is also the
       source-precedence order (:meth:`GlobalMarkersSettings.ordered_enabled_sources`).
    3. **Mask resolution** — TheIntroDB's ``api_key`` is the one secret in this block.
       ``existing_key`` (read from ``existing_sources``) is what a posted ``****`` resolves to, and
       also what fills in when the ``theintrodb`` entry — or ``api_key`` on it, or the whole
       ``sources`` list — is missing entirely, so a save that doesn't touch this field can never
       silently drop the stored key.

    Args:
        raw_sources: The posted ``sources`` value (``None`` when the whole block was omitted).
        existing_sources: The stored ``sources`` list, used only to recover TheIntroDB's key.

    Returns:
        ``(sources, "")`` on success, ``(None, message)`` on error.
    """
    existing_key = ""
    if isinstance(existing_sources, list):
        for s in existing_sources:
            if isinstance(s, dict) and s.get("id") == "theintrodb":
                existing_key = str(s.get("api_key") or "")
    if raw_sources is None:
        out = copy.deepcopy(DEFAULT_GLOBAL_MARKERS["sources"])
        for s in out:
            if s["id"] == "theintrodb":
                s["api_key"] = existing_key
        return out, ""
    if not isinstance(raw_sources, list):
        return None, "markers.sources must be a list"
    seen: set[str] = set()
    out: list[dict] = []
    for raw in raw_sources:
        if not isinstance(raw, dict):
            return None, "markers.sources entries must be objects"
        sid = raw.get("id")
        if sid not in SOURCE_IDS:
            return None, f"markers.sources: unknown source id {sid!r}"
        if sid in seen:
            return None, f"markers.sources: duplicate source id {sid!r}"
        seen.add(sid)
        entry: dict[str, Any] = {"id": sid, "enabled": bool(raw.get("enabled", True))}
        if sid == "theintrodb":
            key = raw.get("api_key", existing_key)
            key = existing_key if key == SECRET_MASK else str(key or "").strip()
            # Only a new key is checked: a stored one that no longer passes must not make the whole block invalid on
            # load (every Intro & Credits setting would fall back to the defaults) or refuse every later save; the
            # client refuses to send it and the job says so.
            if key and key != existing_key and not (len(key) <= _API_KEY_MAX_LEN and sendable_api_key(key)):
                return (
                    None,
                    "markers.sources: theintrodb api_key must be up to 200 printable ASCII characters with no spaces",
                )
            entry["api_key"] = key
        out.append(entry)
    for default in DEFAULT_GLOBAL_MARKERS["sources"]:
        if default["id"] not in seen:
            entry = copy.deepcopy(default)
            if entry["id"] == "theintrodb":
                entry["api_key"] = existing_key
            out.append(entry)
    return out, ""


def validate_global(raw: object, existing: object) -> tuple[dict | None, str]:
    """Validate and normalise a posted global ``markers`` block.

    Args:
        raw: The posted block.
        existing: The stored block (used to keep the TheIntroDB key when ``****`` is posted back).

    Returns:
        ``(block, "")`` on success, ``(None, message)`` on error.
    """
    if not isinstance(raw, dict):
        return None, "markers must be an object"
    detect_raw = raw.get("detect", {})
    if not isinstance(detect_raw, dict):
        return None, "markers.detect must be an object"
    detect = {k: bool(detect_raw.get(k, v)) for k, v in DEFAULT_GLOBAL_MARKERS["detect"].items()}
    publish_when = raw.get("publish_when", "high")
    if publish_when not in PUBLISH_WHEN_VALUES:
        return None, "markers.publish_when must be 'high' or 'medium'"
    existing_sources = existing.get("sources") if isinstance(existing, dict) else None
    sources, err = _normalise_sources(raw.get("sources"), existing_sources)
    if err:
        return None, err
    return {
        "detect": detect,
        "publish_when": publish_when,
        "respect_locks": bool(raw.get("respect_locks", True)),
        "sources": sources,
    }, ""


def validate_server(raw: object, server_type: str) -> tuple[dict | None, str]:
    """Validate and normalise a per-server ``markers`` block.

    Args:
        raw: The posted block (None → defaults).
        server_type: ``plex``, ``emby`` or ``jellyfin``.

    Returns:
        ``(block, "")`` on success, ``(None, message)`` on error.
    """
    if raw is None:
        return default_server_markers(server_type), ""
    if not isinstance(raw, dict):
        return None, "markers must be an object"
    library_ids_raw = raw.get("library_ids")
    library_ids: list[str] | None = None
    if library_ids_raw is not None:
        if not isinstance(library_ids_raw, list):
            return None, "markers.library_ids must be a list or null"
        library_ids = list(dict.fromkeys(str(x) for x in library_ids_raw))
    block: dict[str, Any] = {"enabled": bool(raw.get("enabled", False)), "library_ids": library_ids}
    if server_type == "plex":
        plex_raw = raw.get("plex") or {}
        if not isinstance(plex_raw, dict):
            return None, "markers.plex must be an object"
        redetect = plex_raw.get("on_plex_redetect", "restore")
        if redetect not in ON_PLEX_REDETECT_VALUES:
            return None, "markers.plex.on_plex_redetect must be 'restore' or 'keep_plex'"
        confirmed = plex_raw.get("db_write_confirmed_at")
        confirmed = str(confirmed) if confirmed else None
        if block["enabled"] and not confirmed:
            return None, "Confirm the Plex database write before turning on Intro & Credits for this Plex server"
        block["plex"] = {"db_write_confirmed_at": confirmed, "on_plex_redetect": redetect}
    if server_type == "emby":
        emby_raw = raw.get("emby") or {}
        if not isinstance(emby_raw, dict):
            return None, "markers.emby must be an object"
        redetect = emby_raw.get("on_emby_redetect", "restore")
        if redetect not in ON_EMBY_REDETECT_VALUES:
            return None, "markers.emby.on_emby_redetect must be 'restore' or 'keep_emby'"
        block["emby"] = {"on_emby_redetect": redetect}
    return block, ""


def mask_global(block: object) -> dict:
    """Return a copy of the global block with the TheIntroDB key replaced by ``****`` when set.

    Args:
        block: The stored (unmasked) global ``markers`` block. A non-dict falls back to
            :data:`DEFAULT_GLOBAL_MARKERS` (its TheIntroDB key is already empty).

    Returns:
        A deep copy safe to send to the client — the input is never mutated.
    """
    source = block if isinstance(block, dict) else DEFAULT_GLOBAL_MARKERS
    out = copy.deepcopy(source)
    for s in out.get("sources") or []:
        if isinstance(s, dict) and s.get("id") == "theintrodb":
            s["api_key"] = SECRET_MASK if s.get("api_key") else ""
    return out


def load_global(raw: object) -> GlobalMarkersSettings:
    """Build typed global settings, falling back to defaults when the stored block is invalid.

    Args:
        raw: The stored ``settings.json["markers"]`` value (any shape — a fresh install has none).

    Returns:
        A :class:`GlobalMarkersSettings`. Falls back to :data:`DEFAULT_GLOBAL_MARKERS` (logged as a
        warning, since a settings.json a previous release wrote should always validate) when
        ``raw`` is a dict that fails validation.
    """
    block, err = validate_global(raw if isinstance(raw, dict) else {}, raw)
    if err or block is None:
        if err:
            logger.warning("Ignoring invalid Intro & Credits settings ({}); using defaults instead.", err)
        block = copy.deepcopy(DEFAULT_GLOBAL_MARKERS)
    return GlobalMarkersSettings(
        detect_intro=block["detect"]["intro"],
        detect_credits=block["detect"]["credits"],
        detect_recap=block["detect"]["recap"],
        publish_when=block["publish_when"],
        respect_locks=block["respect_locks"],
        sources=tuple(SourceSetting(s["id"], s["enabled"], s.get("api_key", "")) for s in block["sources"]),
    )


def load_server(raw: object, server_type: str) -> ServerMarkersSettings:
    """Build typed per-server settings, falling back to disabled defaults when invalid.

    Args:
        raw: The stored ``media_servers[].markers`` value (any shape; ``None`` for a server added
            before this field existed).
        server_type: ``plex``, ``emby`` or ``jellyfin``.

    Returns:
        A :class:`ServerMarkersSettings`. Falls back to :func:`default_server_markers` — i.e.
        disabled — (logged as a warning when ``raw`` was a dict that failed validation, e.g. a
        Plex block with ``enabled: true`` but no confirmation) when invalid.
    """
    block, err = validate_server(raw, server_type)
    if err or block is None:
        if err:
            logger.warning(
                "Ignoring invalid per-server Intro & Credits settings for a {} server ({}); using disabled defaults.",
                server_type,
                err,
            )
        block = default_server_markers(server_type)
    plex = block.get("plex") or {}
    emby = block.get("emby") or {}
    ids = block["library_ids"]
    return ServerMarkersSettings(
        enabled=block["enabled"],
        library_ids=tuple(ids) if ids is not None else None,
        db_write_confirmed_at=plex.get("db_write_confirmed_at"),
        on_plex_redetect=plex.get("on_plex_redetect", "restore"),
        on_emby_redetect=emby.get("on_emby_redetect", "restore"),
    )


def is_sports_library(name: str, kind: str | None) -> bool:
    """Heuristic sports-library check used for the default library selection.

    Args:
        name: The library's display name (checked for a whole-word "sport"/"sports").
        kind: The vendor-reported library kind/type, when one exists (no vendor exposes an actual
            "sports" kind today, but a future one might).

    Returns:
        True if either signal says "sports".
    """
    return bool(_SPORTS_NAME_RE.search(name or "")) or (kind or "").lower() in {"sport", "sports"}


def library_allowed(
    server: ServerMarkersSettings, *, library_id: str | None, library_name: str, kind: str | None
) -> bool:
    """Whether markers go to this library on this server.

    ``library_ids=None`` means every library except sports-type ones; an explicit list is taken
    literally (including a deliberate choice to include a sports library).

    Args:
        server: The server's typed markers settings.
        library_id: The library's id, or None when unknown.
        library_name: The library's display name (used by the sports-library heuristic).
        kind: The vendor-reported library kind, when one exists.

    Returns:
        True if this library should receive markers.
    """
    if server.library_ids is not None:
        return library_id is not None and library_id in server.library_ids
    return not is_sports_library(library_name, kind)


def get_global_settings() -> GlobalMarkersSettings:
    """Read the global markers settings from the settings manager.

    Returns:
        A :class:`GlobalMarkersSettings` built from the currently-stored ``markers`` block.
    """
    from ..web.settings_manager import get_settings_manager

    return load_global(get_settings_manager().get("markers"))
