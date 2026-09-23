# Intro & Credits — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Files covered by chapters or online sources get correct Skip Intro / Skip Credits markers on Plex (database
write) and Jellyfin (Bridge plugin), through a new "Intro & Credits" job kind that shares the existing engine and UI.

**Architecture:** `media_preview_generator/markers/` holds settings, models, the §5.5 decision rules, a SQLite store,
sources (chapters, TheIntroDB, IntroDB.app, SkipDB, markers already on servers), publishers (Plex DB, Jellyfin Bridge)
and a two-stage pipeline (`check_item` on the dispatcher's checking threads, `process_item` on GPU/CPU workers).
The dispatcher and worker gain generic per-kind handlers (`job_kinds.py`) so previews keep their exact code path.

**Tech Stack:** Python, Flask, SQLite (stdlib `sqlite3`), requests, ffprobe, C# .NET 9 / .NET 10 (Jellyfin plugin),
Bootstrap 5 + vanilla JS (existing UI), pytest + Playwright.

**Spec:** `docs/design/intro-credits/spec.md` — read §0, §1, §3.1, §3.2, §4, §5.1, §5.2, §5.5, §6, §7, §8 before
starting. Roadmap + Global Constraints: `docs/design/intro-credits/plan-roadmap.md` (they apply to every task here).

## Global Constraints

See `plan-roadmap.md` → Global Constraints. Phase-1-specific additions:
- Phase 1 has **no local detectors**. `process_item` exists and is wired, but only `check_item` decides; items that
  would need season audio or credit text end as "needs review" until phase 2/3 add those sources.
- SkipDB is queried through its read API (`GET https://api.skipdb.tv/api/segments`), not the daily dump (decision
  2026-09-13, §14: open read API, 120 req/min, duration matching, no 29 MB daily download; reciprocity term exempts
  read-only API use). Accept only `match` ∈ {`exact`, `shifted`}.
- TheIntroDB v3 accepts `tmdb_id`, `tvdb_id` or `imdb_id`; auth is `Authorization: Bearer <key>` when a key is set.

## Task order and dependencies

| # | Task | Depends on |
|---|---|---|
| 1 | Settings, per-server block, schema v15 | — |
| 2 | Job kinds in the engine (dispatcher, worker, Job.kind, per-job pause) | — |
| 3 | Models + decision rules | — |
| 4 | Marker store (`markers.db`) | 3 |
| 5 | Media probe + chapters source | 3 |
| 6 | External ids (path + Plex/Emby/Jellyfin metadata) | 3 |
| 7 | Rate limiter + TheIntroDB / IntroDB / SkipDB clients | 3, 6 |
| 8 | Publisher base + network-fs check + Plex DB publisher | 3, 4 |
| 9 | Jellyfin Bridge plugin: markers endpoint + provider, 10.11 + 12.0 builds | — |
| 10 | Jellyfin publisher + server-markers evidence readers | 3, 8, 9 |
| 11 | Pipeline (`check_item` / `process_item`) + outcomes | 1–8, 10 |
| 12 | Intro & Credits job runner + triggers (API, webhooks, schedules) | 2, 11 |
| 13 | Markers API (server status, source usage, inspector data) | 8, 10, 11 |
| 14 | UI: server Edit → Intro & Credits tab + Plex confirmation | 1, 13 |
| 15 | UI: Settings → Intro & Credits section | 1, 13 |
| 16 | UI: jobs (start modal, schedule modal, queue rows, files, pause) | 12 |
| 17 | UI: Inspector → Intro & Credits tab (read-only) | 13 |
| 18 | Docs | 1–17 |
| 19 | Lab end-to-end (plugins, app container, Plex claim, wipe matrix, skip buttons) | 1–18 |
| 20 | PR: body, `build-docker` label, CI green, side-by-side on `plex` (owner OK) | 19 |

Tasks 1, 2, 3, 9 are independent and can run in parallel. UI tasks 14–17 need a running app for Playwright tests.

---

## Task 1: Settings, per-server block, schema v15

**Files:**
- Create: `media_preview_generator/markers/__init__.py`, `media_preview_generator/markers/settings.py`
- Modify: `media_preview_generator/upgrade.py` (constant `_CURRENT_SCHEMA_VERSION` line ~24, docstring of
  `_migrate_schema` ~322-365, chain ~390-427, new `_migrate_to_v15`)
- Modify: `media_preview_generator/servers/base.py:729` (`ServerConfig`), `media_preview_generator/servers/registry.py:84-96`
- Modify: `media_preview_generator/web/routes/api_servers.py:290-420` (`_validate_server_payload`)
- Modify: `media_preview_generator/web/routes/api_settings.py` (`get_settings` ~284-340, `_SAVE_SETTINGS_ALLOWED_FIELDS`
  ~344, `_validate_and_coerce_settings_updates` ~403)
- Test: `tests/markers/__init__.py`, `tests/markers/conftest.py`, `tests/markers/test_settings.py`, `tests/test_upgrade.py` (new `TestMigrateToV15`,
  update hard-coded `14` assertions near lines ~2345 and ~2507 to `_CURRENT_SCHEMA_VERSION`),
  `tests/markers/test_settings_routes.py`

**Interfaces:**
- Produces (used by tasks 7, 11–16):
```python
# media_preview_generator/markers/settings.py
SOURCE_IDS: tuple[str, ...]            # ("chapters","theintrodb","introdb","skipdb","season_audio","credits_text","server_markers")
SECRET_MASK: str                       # "****"
DEFAULT_GLOBAL_MARKERS: dict
def default_server_markers(server_type: str) -> dict
@dataclass(frozen=True) class SourceSetting: id: str; enabled: bool; api_key: str = ""
@dataclass(frozen=True) class GlobalMarkersSettings:
    detect_intro: bool; detect_credits: bool; detect_recap: bool
    publish_when: str; respect_locks: bool; sources: tuple[SourceSetting, ...]
    def source(self, source_id: str) -> SourceSetting | None
    def source_enabled(self, source_id: str) -> bool
    def ordered_enabled_sources(self) -> tuple[str, ...]
    def detection_fingerprint(self) -> str          # sha1 hex of detect/publish/sources(id,enabled) — no api key
@dataclass(frozen=True) class ServerMarkersSettings:
    enabled: bool; library_ids: tuple[str, ...] | None
    db_write_confirmed_at: str | None; on_plex_redetect: str
def load_global(raw: object) -> GlobalMarkersSettings
def load_server(raw: object, server_type: str) -> ServerMarkersSettings
def validate_global(raw: object, existing: object) -> tuple[dict | None, str]
def validate_server(raw: object, server_type: str) -> tuple[dict | None, str]
def mask_global(block: object) -> dict
def is_sports_library(name: str, kind: str | None) -> bool
def library_allowed(server: ServerMarkersSettings, *, library_id: str | None, library_name: str, kind: str | None) -> bool
def get_global_settings() -> GlobalMarkersSettings        # reads settings_manager "markers"
```
- `ServerConfig.markers: dict[str, Any]` (new field, default `{}`), round-tripped by `server_config_from_dict/to_dict`.

- [ ] **Step 1: Write the failing settings tests**

```python
# tests/markers/test_settings.py
import pytest

from media_preview_generator.markers import settings as ms


class TestValidateGlobal:
    def test_defaults_when_raw_is_empty_dict(self):
        block, err = ms.validate_global({}, None)
        assert err == ""
        assert block == ms.DEFAULT_GLOBAL_MARKERS

    def test_rejects_non_object(self):
        block, err = ms.validate_global("nope", None)
        assert block is None
        assert "object" in err

    @pytest.mark.parametrize("value", ["high", "medium"])
    def test_accepts_publish_when(self, value):
        block, err = ms.validate_global({"publish_when": value}, None)
        assert err == "" and block["publish_when"] == value

    def test_rejects_unknown_publish_when(self):
        block, err = ms.validate_global({"publish_when": "low"}, None)
        assert block is None and "publish_when" in err

    def test_rejects_unknown_source_id(self):
        block, err = ms.validate_global({"sources": [{"id": "bogus", "enabled": True}]}, None)
        assert block is None and "bogus" in err

    def test_rejects_duplicate_source_id(self):
        raw = {"sources": [{"id": "chapters", "enabled": True}, {"id": "chapters", "enabled": False}]}
        block, err = ms.validate_global(raw, None)
        assert block is None and "duplicate" in err

    def test_keeps_user_order_and_appends_missing_sources_in_default_order(self):
        raw = {"sources": [{"id": "skipdb", "enabled": False}, {"id": "chapters", "enabled": True}]}
        block, err = ms.validate_global(raw, None)
        assert err == ""
        assert [s["id"] for s in block["sources"]] == [
            "skipdb", "chapters", "theintrodb", "introdb", "season_audio", "credits_text", "server_markers",
        ]
        assert block["sources"][0]["enabled"] is False

    def test_masked_api_key_keeps_existing_key(self):
        existing = {"sources": [{"id": "theintrodb", "enabled": True, "api_key": "real-key-123"}]}
        raw = {"sources": [{"id": "theintrodb", "enabled": True, "api_key": ms.SECRET_MASK}]}
        block, _ = ms.validate_global(raw, existing)
        tidb = next(s for s in block["sources"] if s["id"] == "theintrodb")
        assert tidb["api_key"] == "real-key-123"

    def test_new_api_key_is_stripped_and_stored(self):
        raw = {"sources": [{"id": "theintrodb", "enabled": True, "api_key": "  new-key  "}]}
        block, _ = ms.validate_global(raw, None)
        assert next(s for s in block["sources"] if s["id"] == "theintrodb")["api_key"] == "new-key"

    def test_rejects_api_key_with_whitespace_inside(self):
        raw = {"sources": [{"id": "theintrodb", "enabled": True, "api_key": "a b"}]}
        block, err = ms.validate_global(raw, None)
        assert block is None and "api_key" in err

    def test_api_key_only_on_theintrodb(self):
        raw = {"sources": [{"id": "introdb", "enabled": True, "api_key": "x"}]}
        block, _ = ms.validate_global(raw, None)
        assert "api_key" not in next(s for s in block["sources"] if s["id"] == "introdb")


class TestMaskGlobal:
    def test_masks_set_key(self):
        block = ms.validate_global({"sources": [{"id": "theintrodb", "enabled": True, "api_key": "k"}]}, None)[0]
        masked = ms.mask_global(block)
        assert next(s for s in masked["sources"] if s["id"] == "theintrodb")["api_key"] == ms.SECRET_MASK
        assert next(s for s in block["sources"] if s["id"] == "theintrodb")["api_key"] == "k"  # input untouched

    def test_empty_key_stays_empty(self):
        masked = ms.mask_global(ms.DEFAULT_GLOBAL_MARKERS)
        assert next(s for s in masked["sources"] if s["id"] == "theintrodb")["api_key"] == ""


class TestValidateServer:
    @pytest.mark.parametrize("server_type", ["plex", "emby", "jellyfin"])
    def test_none_gives_defaults(self, server_type):
        block, err = ms.validate_server(None, server_type)
        assert err == ""
        assert block == ms.default_server_markers(server_type)
        assert ("plex" in block) is (server_type == "plex")

    def test_plex_enable_requires_confirmation(self):
        block, err = ms.validate_server({"enabled": True}, "plex")
        assert block is None and "confirm" in err.lower()

    def test_plex_enable_with_confirmation(self):
        raw = {"enabled": True, "plex": {"db_write_confirmed_at": "2026-09-13T10:00:00+00:00"}}
        block, err = ms.validate_server(raw, "plex")
        assert err == "" and block["enabled"] is True
        assert block["plex"] == {"db_write_confirmed_at": "2026-09-13T10:00:00+00:00", "on_plex_redetect": "restore"}

    @pytest.mark.parametrize("server_type", ["emby", "jellyfin"])
    def test_non_plex_enable_needs_no_confirmation_and_drops_plex_block(self, server_type):
        raw = {"enabled": True, "plex": {"db_write_confirmed_at": "x"}}
        block, err = ms.validate_server(raw, server_type)
        assert err == "" and block == {"enabled": True, "library_ids": None}

    def test_rejects_bad_on_plex_redetect(self):
        raw = {"plex": {"on_plex_redetect": "sometimes"}}
        block, err = ms.validate_server(raw, "plex")
        assert block is None and "on_plex_redetect" in err

    def test_library_ids_normalised_to_unique_strings(self):
        block, err = ms.validate_server({"library_ids": [1, "2", "2"]}, "jellyfin")
        assert err == "" and block["library_ids"] == ["1", "2"]

    def test_rejects_library_ids_non_list(self):
        block, err = ms.validate_server({"library_ids": "1"}, "emby")
        assert block is None and "library_ids" in err

    def test_load_server_treats_unconfirmed_plex_as_disabled(self):
        s = ms.load_server({"enabled": True, "plex": {"db_write_confirmed_at": None}}, "plex")
        assert s.enabled is False

    def test_load_server_confirmed_plex_enabled(self):
        s = ms.load_server({"enabled": True, "plex": {"db_write_confirmed_at": "t"}}, "plex")
        assert s.enabled is True and s.db_write_confirmed_at == "t" and s.on_plex_redetect == "restore"


class TestLibraryAllowed:
    @pytest.mark.parametrize(
        ("library_ids", "lib_id", "name", "expected"),
        [
            (None, "1", "Movies", True),
            (None, "2", "Sports", False),
            (None, "3", "NFL Sport", False),
            (None, "4", "Transport Docs", True),
            (("1",), "1", "Movies", True),
            (("1",), "2", "TV Shows", False),
            (("2",), "2", "Sports", True),  # explicit choice beats the sports default
        ],
    )
    def test_matrix(self, library_ids, lib_id, name, expected):
        s = ms.ServerMarkersSettings(True, library_ids, None, "restore")
        assert ms.library_allowed(s, library_id=lib_id, library_name=name, kind=None) is expected

    def test_unknown_library_id_uses_default_rule(self):
        s = ms.ServerMarkersSettings(True, None, None, "restore")
        assert ms.library_allowed(s, library_id=None, library_name="", kind=None) is True


class TestLoadGlobal:
    def test_garbage_falls_back_to_defaults(self):
        g = ms.load_global(["x"])
        assert g.publish_when == "high" and g.detect_intro and not g.detect_recap
        assert g.ordered_enabled_sources() == (
            "chapters", "introdb", "skipdb", "season_audio", "credits_text", "server_markers",
        )

    def test_fingerprint_ignores_api_key_but_tracks_enabled(self):
        a = ms.load_global(ms.validate_global({"sources": [{"id": "theintrodb", "enabled": True, "api_key": "1"}]}, None)[0])
        b = ms.load_global(ms.validate_global({"sources": [{"id": "theintrodb", "enabled": True, "api_key": "2"}]}, None)[0])
        c = ms.load_global(ms.validate_global({"sources": [{"id": "theintrodb", "enabled": False}]}, None)[0])
        assert a.detection_fingerprint() == b.detection_fingerprint() != c.detection_fingerprint()
```

- [ ] **Step 2: Run to verify failure**

Run: `/home/data/.venv/bin/python -m pytest --no-cov tests/markers/test_settings.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'media_preview_generator.markers'`

- [ ] **Step 3: Implement `markers/settings.py`**

```python
# media_preview_generator/markers/__init__.py
"""Intro & Credits (skip markers): detect once per file, publish to every owning server."""
```

```python
# media_preview_generator/markers/settings.py
"""Intro & Credits settings: defaults, validation and typed accessors.

Global detection settings live in ``settings.json["markers"]``; what each server receives lives in
``media_servers[].markers`` (spec §8). Validation normalises both blocks so readers never see partial shapes.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

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
SECRET_MASK = "****"
_API_KEY_RE = re.compile(r"^[^\s]{1,200}$")
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
        A fresh dict; Plex servers also get the ``plex`` sub-block.
    """
    block: dict[str, Any] = {"enabled": False, "library_ids": None}
    if server_type == "plex":
        block["plex"] = {"db_write_confirmed_at": None, "on_plex_redetect": "restore"}
    return block


@dataclass(frozen=True)
class SourceSetting:
    """One evidence source as configured by the user."""

    id: str
    enabled: bool
    api_key: str = ""


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
        """Hash of everything that changes a decision (excludes secrets)."""
        payload = {
            "detect": [self.detect_intro, self.detect_credits, self.detect_recap],
            "publish_when": self.publish_when,
            "sources": [[s.id, s.enabled] for s in self.sources],
        }
        return hashlib.sha1(json.dumps(payload, sort_keys=True).encode(), usedforsecurity=False).hexdigest()


@dataclass(frozen=True)
class ServerMarkersSettings:
    """Typed view of ``media_servers[].markers``."""

    enabled: bool
    library_ids: tuple[str, ...] | None
    db_write_confirmed_at: str | None
    on_plex_redetect: str


def _normalise_sources(raw_sources: Any, existing_sources: Any) -> tuple[list[dict] | None, str]:
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
            if key and not _API_KEY_RE.match(key):
                return None, "markers.sources: theintrodb api_key must not contain whitespace"
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
    return block, ""


def mask_global(block: object) -> dict:
    """Return a copy of the global block with the TheIntroDB key replaced by ``****`` when set."""
    source = block if isinstance(block, dict) else DEFAULT_GLOBAL_MARKERS
    out = copy.deepcopy(source)
    for s in out.get("sources") or []:
        if isinstance(s, dict) and s.get("id") == "theintrodb":
            s["api_key"] = SECRET_MASK if s.get("api_key") else ""
    return out


def load_global(raw: object) -> GlobalMarkersSettings:
    """Build typed global settings; anything invalid falls back to defaults."""
    block, err = validate_global(raw if isinstance(raw, dict) else {}, raw)
    if err or block is None:
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
    """Build typed per-server settings; invalid blocks (e.g. Plex enabled without confirmation) mean "disabled"."""
    block, err = validate_server(raw, server_type)
    if err or block is None:
        block = default_server_markers(server_type)
    plex = block.get("plex") or {}
    ids = block["library_ids"]
    return ServerMarkersSettings(
        enabled=block["enabled"],
        library_ids=tuple(ids) if ids is not None else None,
        db_write_confirmed_at=plex.get("db_write_confirmed_at"),
        on_plex_redetect=plex.get("on_plex_redetect", "restore"),
    )


def is_sports_library(name: str, kind: str | None) -> bool:
    """Heuristic sports-library check used for the default library selection."""
    return bool(_SPORTS_NAME_RE.search(name or "")) or (kind or "").lower() in {"sport", "sports"}


def library_allowed(
    server: ServerMarkersSettings, *, library_id: str | None, library_name: str, kind: str | None
) -> bool:
    """Whether markers go to this library on this server.

    ``library_ids=None`` means every library except sports-type ones; an explicit list is taken literally.
    """
    if server.library_ids is not None:
        return library_id is not None and library_id in server.library_ids
    return not is_sports_library(library_name, kind)


def get_global_settings() -> GlobalMarkersSettings:
    """Read the global markers settings from the settings manager."""
    from ..web.settings_manager import get_settings_manager

    return load_global(get_settings_manager().get("markers"))
```

- [ ] **Step 4: Run settings tests** — `pytest --no-cov tests/markers/test_settings.py -q` → PASS.

- [ ] **Step 5: Write failing migration + round-trip tests**

```python
# tests/test_upgrade.py — append
class TestMigrateToV15:
    def test_seeds_global_and_per_server_blocks(self, settings_manager):
        from media_preview_generator.markers.settings import DEFAULT_GLOBAL_MARKERS, default_server_markers
        from media_preview_generator.upgrade import _migrate_to_v15

        settings_manager.apply_changes(updates={"media_servers": [
            {"id": "p1", "type": "plex", "name": "P"},
            {"id": "j1", "type": "jellyfin", "name": "J"},
            {"id": "e1", "type": "emby", "name": "E", "markers": {"enabled": False, "library_ids": ["9"]}},
        ]})
        notes = _migrate_to_v15(settings_manager)
        assert settings_manager.get("markers") == DEFAULT_GLOBAL_MARKERS
        servers = {s["id"]: s for s in settings_manager.get("media_servers")}
        assert servers["p1"]["markers"] == default_server_markers("plex")
        assert servers["j1"]["markers"] == default_server_markers("jellyfin")
        assert servers["e1"]["markers"] == {"enabled": False, "library_ids": ["9"]}  # untouched
        assert len(notes) == 2

    def test_idempotent(self, settings_manager):
        from media_preview_generator.upgrade import _migrate_to_v15

        settings_manager.apply_changes(updates={"media_servers": [{"id": "p1", "type": "plex", "name": "P"}]})
        _migrate_to_v15(settings_manager)
        assert _migrate_to_v15(settings_manager) == []

    def test_schema_chain_reaches_15_without_user_note(self, settings_manager):
        from media_preview_generator.upgrade import _CURRENT_SCHEMA_VERSION, _migrate_schema

        settings_manager.apply_changes(updates={"_schema_version": 14})
        _migrate_schema(settings_manager)
        assert _CURRENT_SCHEMA_VERSION == 15
        assert settings_manager.get("_schema_version") == 15
        notice = settings_manager.get("_pending_migration_notice") or {}
        assert not any("Intro" in n for n in notice.get("notes", []))
```

```python
# tests/markers/conftest.py
"""Shared fixtures for Intro & Credits tests."""

import pytest


@pytest.fixture
def app(tmp_path):
    """Same app fixture as tests/test_routes.py (setup complete, fixed API token)."""
    import json
    import os
    from unittest.mock import patch

    from media_preview_generator.web import settings_manager as sm_mod
    from media_preview_generator.web.app import create_app

    sm_mod.reset_settings_manager()
    (tmp_path / "settings.json").write_text(json.dumps({"setup_complete": True}))
    with patch.dict(os.environ, {"CONFIG_DIR": str(tmp_path), "WEB_AUTH_TOKEN": "test-token-12345678"}):
        flask_app = create_app(config_dir=str(tmp_path))
        flask_app.config["TESTING"] = True
        flask_app.config["WTF_CSRF_ENABLED"] = False
        yield flask_app
    sm_mod.reset_settings_manager()


@pytest.fixture
def client(app):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["authenticated"] = True
    return c


def api_headers():
    return {"Authorization": "Bearer test-token-12345678", "Content-Type": "application/json"}


@pytest.fixture(autouse=True)
def _reset_marker_singletons():
    yield
    try:
        from media_preview_generator.markers.sources.ratelimit import reset_limiters
        from media_preview_generator.markers.store import reset_marker_store
    except ImportError:
        return
    reset_marker_store()
    reset_limiters()
```
(The `try/except ImportError` keeps Task 1 green before Tasks 4 and 7 create those modules.)

```python
# tests/markers/test_settings_routes.py
"""Round-trip of markers settings through the settings and servers APIs."""

import pytest

from media_preview_generator.markers.settings import SECRET_MASK


def test_settings_get_masks_theintrodb_key(client):
    from media_preview_generator.web.settings_manager import get_settings_manager

    get_settings_manager().set("markers", {
        "detect": {"intro": True, "credits": True, "recap": False}, "publish_when": "high", "respect_locks": True,
        "sources": [{"id": "theintrodb", "enabled": True, "api_key": "secret-abc"}],
    })
    body = client.get("/api/settings").get_json()
    tidb = next(s for s in body["markers"]["sources"] if s["id"] == "theintrodb")
    assert tidb["api_key"] == SECRET_MASK
    assert "secret-abc" not in client.get("/api/settings").get_data(as_text=True)


def test_settings_post_masked_key_keeps_stored_key(client):
    from media_preview_generator.web.settings_manager import get_settings_manager

    get_settings_manager().set("markers", {"sources": [{"id": "theintrodb", "enabled": True, "api_key": "k1"}]})
    resp = client.post("/api/settings", json={"markers": {
        "publish_when": "medium", "sources": [{"id": "theintrodb", "enabled": True, "api_key": SECRET_MASK}],
    }})
    assert resp.status_code == 200
    stored = get_settings_manager().get("markers")
    assert stored["publish_when"] == "medium"
    assert next(s for s in stored["sources"] if s["id"] == "theintrodb")["api_key"] == "k1"


def test_settings_post_invalid_markers_is_400(client):
    resp = client.post("/api/settings", json={"markers": {"publish_when": "sometimes"}})
    assert resp.status_code == 400
    assert "publish_when" in resp.get_json()["error"]


def _add_server(client, server_type, **extra):
    from media_preview_generator.web.settings_manager import get_settings_manager

    entry = {"id": f"{server_type}-1", "type": server_type, "name": server_type, "enabled": True,
             "url": "http://x:1", "auth": {}, "libraries": [], "path_mappings": [], "exclude_paths": [],
             "output": {"plex_config_folder": "/tmp"} if server_type == "plex" else {}, **extra}
    get_settings_manager().set("media_servers", [entry])
    return entry["id"]


@pytest.mark.parametrize("server_type", ["plex", "emby", "jellyfin"])
def test_server_save_without_markers_key_carries_block_forward(client, server_type):
    from media_preview_generator.web.settings_manager import get_settings_manager

    block = {"enabled": False, "library_ids": ["7"]}
    if server_type == "plex":
        block["plex"] = {"db_write_confirmed_at": None, "on_plex_redetect": "keep_plex"}
    sid = _add_server(client, server_type, markers=block)
    resp = client.put(f"/api/servers/{sid}", json={"name": "renamed"})
    assert resp.status_code == 200, resp.get_json()
    assert get_settings_manager().get("media_servers")[0]["markers"] == block
    assert resp.get_json()["markers"] == block


def test_plex_enable_without_confirmation_is_400(client):
    sid = _add_server(client, "plex")
    resp = client.put(f"/api/servers/{sid}", json={"markers": {"enabled": True}})
    assert resp.status_code == 400
    assert "Confirm" in resp.get_json()["error"]


def test_jellyfin_enable_saves(client):
    from media_preview_generator.web.settings_manager import get_settings_manager

    sid = _add_server(client, "jellyfin")
    resp = client.put(f"/api/servers/{sid}", json={"markers": {"enabled": True, "library_ids": None}})
    assert resp.status_code == 200
    assert get_settings_manager().get("media_servers")[0]["markers"] == {"enabled": True, "library_ids": None}
```

Run: `pytest --no-cov tests/test_upgrade.py::TestMigrateToV15 tests/markers/test_settings_routes.py -q` → FAIL.

- [ ] **Step 6: Implement migration, ServerConfig field, API wiring**

`upgrade.py`:
```python
_CURRENT_SCHEMA_VERSION = 15
```
Add to the `_migrate_schema` docstring version list: `15: seed Intro & Credits (markers) defaults, disabled per server.`
Add after the v14 step: `if current < 15: _run(15, _migrate_to_v15)`. No `_USER_FACING_NOTES[15]` (the feature is off).
```python
def _migrate_to_v15(sm) -> list:
    """Seed the Intro & Credits global block and a disabled per-server block (spec §8)."""
    from .markers.settings import DEFAULT_GLOBAL_MARKERS, default_server_markers

    notes: list[str] = []
    if not isinstance(sm.get("markers"), dict):
        sm.apply_changes(updates={"markers": copy.deepcopy(DEFAULT_GLOBAL_MARKERS)})
        notes.append("v15: seeded Intro & Credits defaults (off until enabled per server)")
    media_servers = sm.get("media_servers") or []
    if not isinstance(media_servers, list):
        return notes
    updated: list = []
    added = 0
    for entry in media_servers:
        if isinstance(entry, dict) and not isinstance(entry.get("markers"), dict):
            entry = {**entry, "markers": default_server_markers(str(entry.get("type") or "").lower())}
            added += 1
        updated.append(entry)
    if added:
        sm.update({"media_servers": updated})
        notes.append(f"v15: added a disabled Intro & Credits block to {added} server(s)")
    return notes
```
(`import copy` at the top of `upgrade.py` if not present.) Update the two existing tests that hard-code
`_schema_version == 14` after a full migration to compare with `_CURRENT_SCHEMA_VERSION`.

`servers/base.py` `ServerConfig`: add after `server_identity`:
```python
    # Intro & Credits per-server block (spec §8); validated by markers.settings.validate_server.
    markers: dict[str, Any] = field(default_factory=dict)
```
`servers/registry.py` `server_config_from_dict`: pass `markers=dict(data.get("markers") or {})`.

`web/routes/api_servers.py` `_validate_server_payload`, after `health_dismissals` is computed:
```python
    from ...markers.settings import validate_server as _validate_markers

    markers_raw = data.get("markers") if "markers" in data else base.get("markers")
    markers_block, err = _validate_markers(markers_raw, type_value)
    if err:
        return None, err
```
and add `"markers": markers_block,` to the `entry` dict.

`web/routes/api_settings.py`:
- `get_settings()` response: `"markers": mask_global(settings.get("markers")),`
- `_SAVE_SETTINGS_ALLOWED_FIELDS`: add `"markers"`.
- `_validate_and_coerce_settings_updates`, next to the `frame_reuse` block:
```python
    if "markers" in updates:
        from ...markers.settings import validate_global

        block, err = validate_global(updates["markers"], get_settings_manager().get("markers"))
        if err:
            return None, (jsonify({"error": err}), 400)
        updates["markers"] = block
```
(import `mask_global` at the top of the module; if `_validate_and_coerce_settings_updates` is documented as pure,
pass the stored block in from `save_settings` instead of reading the settings manager inside it.)

- [ ] **Step 7: Run tests** — `pytest --no-cov tests/markers tests/test_upgrade.py tests/test_api_servers.py -q` → PASS.
Then full suite `pytest` → PASS (no regressions; watch `tests/e2e/test_journey_schema_migration_boot.py` in the e2e run).

- [ ] **Step 8: Commit**

```bash
git add media_preview_generator/markers media_preview_generator/upgrade.py media_preview_generator/servers/base.py \
  media_preview_generator/servers/registry.py media_preview_generator/web/routes/api_servers.py \
  media_preview_generator/web/routes/api_settings.py tests/markers tests/test_upgrade.py
# Architecture Review agent on the staged diff first
PATH="/home/data/.venv/bin:$PATH" git commit -m "feat(markers): Intro & Credits settings, per-server block and schema v15"
git push origin feat/markers-detection
```

---
## Task 2: Job kinds in the engine

Spec §6.4 items 1–3, 6, 8. Previews must keep their exact code path and kwargs (D34 lesson).

**Files:**
- Create: `media_preview_generator/job_kinds.py` (top-level on purpose: `web/jobs.py` and `jobs/*` both import it;
  putting it under `jobs/` would import the dispatcher from `web/jobs.py` → circular import)
- Modify: `media_preview_generator/web/jobs.py` (`Job` ~291, `to_dict` ~350, `JobStorage._SCHEMA_SQL` ~387,
  `_migrate_schema` ~477, `upsert` ~496, `_row_to_job` ~567, `create_job` ~1102)
- Modify: `media_preview_generator/jobs/dispatcher.py` (`JobTracker.__init__`, `record_completion` label,
  new `record_custom_check_result`, `submit_items`, `_assign_tasks`, `_run_check`, new `_run_custom_check`,
  `JobDispatcher.__init__` (`_checks_by_kind`), `_get_next_check_item`, `_submit_checks`, `_run_check_and_release`,
  `_on_check_done`, new `_kind_check_cap`)
- Modify: `media_preview_generator/jobs/worker.py` (`assign_task`, `_process_item`, new `_process_custom_item`)
- Modify: `media_preview_generator/web/routes/api_jobs.py` (`pause_job` ~1096, `resume_job` ~1106)
- Test: `tests/test_job_kinds_engine.py`, `tests/test_jobs_kind_persistence.py`, `tests/test_routes.py` (pause matrix)

**Interfaces:**
- Produces:
```python
# media_preview_generator/job_kinds.py
JOB_KIND_PREVIEWS = "previews"
JOB_KIND_INTRO_CREDITS = "intro_credits"
JOB_KINDS: tuple[str, ...]
def parse_job_kind(value: object) -> str
@dataclass class ItemOutcome:
    outcome_key: str; message: str = ""; publisher_rows: list[dict] = field(default_factory=list)
    @property failed -> bool                       # outcome_key == "failed"
def outcome_value(key: str) -> object              # object with .value == key (for _notify_file_result)
@dataclass(frozen=True) class KindHandlers:
    check_fn: Callable[..., ItemOutcome | None]    # check_fn(item, *, cancel_check) ; None → needs a worker
    process_fn: Callable[..., ItemOutcome]         # process_fn(item, *, gpu, gpu_device_path, progress_callback,
                                                   #            phase_callback, cancel_check, pause_check)
    outcome_keys: tuple[str, ...]
    check_label: str = "Checking…"                 # progress text before the first worker item
    check_worker_label: str = "Lookup"             # "worker" column for items finished in the check stage
    check_share: float = 1.0                       # max fraction of checking threads this kind holds (floor 1)
def normalize_outcome(value: object, outcome_keys: tuple[str, ...]) -> ItemOutcome   # invalid → "failed" outcome

# web/jobs.py
Job.kind: str = "previews"
JobManager.create_job(..., kind: str = JOB_KIND_PREVIEWS) -> Job

# jobs/dispatcher.py
JobDispatcher.submit_items(..., priority=..., kind: str = JOB_KIND_PREVIEWS, handlers: KindHandlers | None = None)
JobTracker.kind: str ; JobTracker.handlers: KindHandlers | None
JobTracker.record_custom_check_result(item, outcome: ItemOutcome) -> None
JobDispatcher._on_check_done(kind: str = JOB_KIND_PREVIEWS) -> None ; JobDispatcher._kind_check_cap(tracker) -> int | None

# jobs/worker.py
Worker.assign_task(..., pause_check=None, process_fn: Callable[..., ItemOutcome] | None = None,
                   outcome_keys: tuple[str, ...] | None = None)
```

- [ ] **Step 1: Write failing engine tests**

```python
# tests/test_job_kinds_engine.py
"""Per-kind handlers in the shared dispatcher/worker (spec §6.4)."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.job_kinds import ItemOutcome, KindHandlers
from media_preview_generator.jobs.dispatcher import JobDispatcher
from media_preview_generator.jobs.worker import WorkerPool
from media_preview_generator.processing.generator import CodecNotSupportedError
from media_preview_generator.processing.types import ProcessableItem

KEYS = ("markers_published", "markers_needs_review", "failed")


def _config():
    c = MagicMock()
    c.cpu_threads = 1
    c.gpu_threads = 0
    c.scan_workers = 2
    c.regenerate_thumbnails = False
    c.server_id_filter = None
    return c


def _items(*paths):
    return [ProcessableItem(canonical_path=p, server_id="s1", title=p) for p in paths]


def _row(status="markers_written"):
    return {"server_id": "s1", "server_name": "S1", "server_type": "plex", "adapter_name": "markers",
            "status": status, "message": "", "canonical_path": "/m/a.mkv", "frame_source": "", "output_paths": []}


def test_terminal_check_outcome_never_uses_a_worker():
    pool = WorkerPool(cpu_workers=1, gpu_workers=0, selected_gpus=[])
    dispatcher = JobDispatcher(pool)
    process = MagicMock()
    handlers = KindHandlers(
        check_fn=lambda item, *, cancel_check: ItemOutcome("markers_published", "ok", [_row()]),
        process_fn=process,
        outcome_keys=KEYS,
    )
    with patch("media_preview_generator.processing.generator._notify_file_result") as notify, \
         patch("media_preview_generator.web.jobs.get_job_manager"):
        tracker = dispatcher.submit_items("j1", _items("/m/a.mkv"), _config(), MagicMock(),
                                          kind="intro_credits", handlers=handlers)
        assert tracker.wait(timeout=10)
    process.assert_not_called()
    assert tracker.outcome_counts == {"markers_published": 1, "markers_needs_review": 0, "failed": 0}
    assert tracker.successful == 1
    args, kwargs = notify.call_args
    assert args[0] == "/m/a.mkv" and args[1].value == "markers_published" and args[2] == "ok"
    assert args[3] == "Lookup" and kwargs["servers"] == [_row()]
    assert tracker.publishers_aggregate["s1"]["counts"] == {"markers_written": 1}
    dispatcher.shutdown()


def test_none_from_check_routes_item_to_worker_with_kwargs():
    pool = WorkerPool(cpu_workers=1, gpu_workers=0, selected_gpus=[])
    dispatcher = JobDispatcher(pool)
    seen = {}

    def process(item, **kwargs):
        seen["item"] = item
        seen["kwargs"] = kwargs
        return ItemOutcome("markers_needs_review", "no agreement", [_row("markers_needs_review")])

    handlers = KindHandlers(check_fn=lambda item, *, cancel_check: None, process_fn=process, outcome_keys=KEYS)
    with patch("media_preview_generator.processing.generator._notify_file_result"), \
         patch("media_preview_generator.web.jobs.get_job_manager"):
        tracker = dispatcher.submit_items("j2", _items("/m/b.mkv"), _config(), MagicMock(),
                                          kind="intro_credits", handlers=handlers)
        assert tracker.wait(timeout=10)
    assert seen["item"].canonical_path == "/m/b.mkv"
    assert seen["kwargs"]["gpu"] is None and seen["kwargs"]["gpu_device_path"] is None
    assert set(seen["kwargs"]) == {"gpu", "gpu_device_path", "progress_callback", "phase_callback",
                                   "cancel_check", "pause_check"}
    assert tracker.outcome_counts["markers_needs_review"] == 1
    dispatcher.shutdown()


def test_check_exception_routes_to_worker():
    pool = WorkerPool(cpu_workers=1, gpu_workers=0, selected_gpus=[])
    dispatcher = JobDispatcher(pool)

    def check(item, *, cancel_check):
        raise RuntimeError("boom")

    process = MagicMock(return_value=ItemOutcome("markers_published"))
    handlers = KindHandlers(check_fn=check, process_fn=process, outcome_keys=KEYS)
    with patch("media_preview_generator.processing.generator._notify_file_result"), \
         patch("media_preview_generator.web.jobs.get_job_manager"):
        tracker = dispatcher.submit_items("j3", _items("/m/c.mkv"), _config(), MagicMock(),
                                          kind="intro_credits", handlers=handlers)
        assert tracker.wait(timeout=10)
    process.assert_called_once()
    assert process.call_args.args[0].canonical_path == "/m/c.mkv"
    assert tracker.outcome_counts == {"markers_published": 1, "markers_needs_review": 0, "failed": 0}
    assert tracker.failed == 0 and tracker.successful == 1
    dispatcher.shutdown()


@pytest.mark.parametrize("bad", [object(), ItemOutcome("not_a_known_key", "?")])
def test_malformed_check_outcome_counts_as_failed_and_job_completes(bad):
    pool = WorkerPool(cpu_workers=1, gpu_workers=0, selected_gpus=[])
    dispatcher = JobDispatcher(pool)
    process = MagicMock()
    handlers = KindHandlers(check_fn=lambda item, *, cancel_check: bad, process_fn=process, outcome_keys=KEYS)
    with patch("media_preview_generator.processing.generator._notify_file_result") as notify, \
         patch("media_preview_generator.web.jobs.get_job_manager"):
        tracker = dispatcher.submit_items("j3b", _items("/m/bad.mkv"), _config(), MagicMock(),
                                          kind="intro_credits", handlers=handlers)
        assert tracker.wait(timeout=10)
    process.assert_not_called()
    assert tracker.outcome_counts == {"markers_published": 0, "markers_needs_review": 0, "failed": 1}
    assert tracker.failed == 1 and tracker.successful == 0
    assert notify.call_args.args[1].value == "failed"
    dispatcher.shutdown()


def test_recording_error_still_completes_the_item():
    pool = WorkerPool(cpu_workers=1, gpu_workers=0, selected_gpus=[])
    dispatcher = JobDispatcher(pool)
    # publisher_rows=5 makes list() raise inside the recording block — the item must still complete.
    handlers = KindHandlers(check_fn=lambda item, *, cancel_check: ItemOutcome("markers_published", "ok", 5),
                            process_fn=MagicMock(), outcome_keys=KEYS)
    with patch("media_preview_generator.processing.generator._notify_file_result"), \
         patch("media_preview_generator.web.jobs.get_job_manager"):
        tracker = dispatcher.submit_items("j3c", _items("/m/e.mkv"), _config(), MagicMock(),
                                          kind="intro_credits", handlers=handlers)
        assert tracker.wait(timeout=10)
    assert tracker.completed == 1 and tracker.successful == 1
    assert sum(tracker.outcome_counts.values()) == 1
    dispatcher.shutdown()


def test_worker_outcome_with_unknown_key_counts_as_failed():
    from media_preview_generator.jobs.worker import Worker

    w = Worker(1, "CPU")
    done = threading.Event()
    w._done_event = done
    with patch("media_preview_generator.jobs.worker._notify_file_result") as notify:
        w.assign_task(_items("/m/u.mkv")[0], _config(), MagicMock(), job_id="j3d",
                      process_fn=lambda item, **kw: ItemOutcome("mystery"), outcome_keys=KEYS)
        assert done.wait(timeout=10)
        w.current_thread.join(timeout=5)
    assert w.failed == 1 and w.completed == 0
    delta = w.last_task_outcome_delta()
    assert delta["failed"] == 1 and delta.get("mystery", 0) == 0
    assert notify.call_args.args[1].value == "failed"


def test_normalize_outcome_matrix():
    from media_preview_generator.job_kinds import normalize_outcome

    good = ItemOutcome("markers_published", "ok")
    assert normalize_outcome(good, KEYS) is good
    assert normalize_outcome(ItemOutcome("failed", "x"), KEYS).outcome_key == "failed"
    assert normalize_outcome(ItemOutcome("mystery"), KEYS).outcome_key == "failed"
    assert normalize_outcome("markers_published", KEYS).outcome_key == "failed"
    assert normalize_outcome(None, KEYS).outcome_key == "failed"


@pytest.mark.parametrize(("worker_type", "expect_cpu_rerun"), [("GPU", True), ("CPU", False)])
def test_codec_error_reruns_on_cpu_only_for_gpu_workers(worker_type, expect_cpu_rerun):
    from media_preview_generator.jobs.worker import Worker

    calls = []

    def process(item, **kwargs):
        calls.append((kwargs["gpu"], kwargs["gpu_device_path"]))
        if kwargs["gpu"] is not None:
            raise CodecNotSupportedError("hevc 10-bit unsupported")
        if worker_type == "CPU":
            raise CodecNotSupportedError("cpu codec error")
        return ItemOutcome("markers_published", "cpu ok")

    w = Worker(1, worker_type, gpu="NVIDIA" if worker_type == "GPU" else None,
               gpu_device="cuda:0" if worker_type == "GPU" else None)
    done = threading.Event()
    w._done_event = done
    with patch("media_preview_generator.jobs.worker._notify_file_result"):
        w.assign_task(_items("/m/d.mkv")[0], _config(), MagicMock(), job_id="j4", process_fn=process,
                      outcome_keys=KEYS)
        assert done.wait(timeout=10)
        w.current_thread.join(timeout=5)
    if expect_cpu_rerun:
        assert calls == [("NVIDIA", "cuda:0"), (None, None)]
        assert w.fallback_active is True and "hevc" in w.fallback_reason
        assert w.last_task_outcome_delta()["markers_published"] == 1
        assert w.completed == 1
    else:
        assert calls == [(None, None)]
        assert w.failed == 1 and w.last_task_outcome_delta()["failed"] == 1


def test_paused_kind_tracker_does_not_block_other_tracker():
    pool = WorkerPool(cpu_workers=1, gpu_workers=0, selected_gpus=[])
    dispatcher = JobDispatcher(pool)
    ran = []
    handlers = KindHandlers(check_fn=lambda item, *, cancel_check: None,
                            process_fn=lambda item, **kw: ran.append(item.canonical_path) or ItemOutcome("markers_published"),
                            outcome_keys=KEYS)
    paused = {"value": True}
    with patch("media_preview_generator.processing.generator._notify_file_result"), \
         patch("media_preview_generator.web.jobs.get_job_manager"):
        t_paused = dispatcher.submit_items("paused", _items("/m/p.mkv"), _config(), MagicMock(), kind="intro_credits",
                                           handlers=handlers, callbacks={"pause_check": lambda: paused["value"]})
        t_live = dispatcher.submit_items("live", _items("/m/l.mkv"), _config(), MagicMock(), kind="intro_credits",
                                         handlers=handlers)
        assert t_live.wait(timeout=10)
        assert not t_paused.wait(timeout=0.5)
        paused["value"] = False
        assert t_paused.wait(timeout=10)
    assert ran == ["/m/l.mkv", "/m/p.mkv"]
    dispatcher.shutdown()


def test_kind_check_share_caps_in_flight_checks_and_leaves_room_for_previews():
    """Online lookups can sleep on rate limits inside check_fn; a backfill must not fill every checking thread."""
    from tests.conftest import _ms

    pool = WorkerPool(cpu_workers=1, gpu_workers=0, selected_gpus=[])
    dispatcher = JobDispatcher(pool)
    release = threading.Event()
    lock = threading.Lock()
    in_flight = []
    peak = {"n": 0}

    def slow_check(item, *, cancel_check):
        with lock:
            in_flight.append(item.canonical_path)
            peak["n"] = max(peak["n"], len(in_flight))
        release.wait(timeout=10)
        with lock:
            in_flight.remove(item.canonical_path)
        return ItemOutcome("markers_published")

    handlers = KindHandlers(check_fn=slow_check, process_fn=MagicMock(), outcome_keys=KEYS, check_share=0.25)
    config = _config()
    config.scan_workers = 8  # 8 checking threads → Intro & Credits may hold 2
    with patch("media_preview_generator.processing.generator._notify_file_result"), \
         patch("media_preview_generator.web.jobs.get_job_manager"), \
         patch("media_preview_generator.processing.multi_server.process_canonical_path",
               side_effect=lambda **kw: _ms("skipped", canonical_path=kw["canonical_path"])):
        try:
            paths = [f"/m/ic{i}.mkv" for i in range(6)]
            t_ic = dispatcher.submit_items("ic", _items(*paths), config, MagicMock(), kind="intro_credits",
                                           handlers=handlers)
            for _ in range(100):
                if len(in_flight) == 2:
                    break
                time.sleep(0.05)
            time.sleep(0.3)
            assert peak["n"] == 2
            t_prev = dispatcher.submit_items("prev", _items("/m/p1.mkv", "/m/p2.mkv"), config, MagicMock())
            assert t_prev.wait(timeout=10)  # previews still get checking threads while lookups are stuck
            release.set()
            assert t_ic.wait(timeout=10)
        finally:
            release.set()
            dispatcher.shutdown()
    assert peak["n"] == 2
    assert t_ic.outcome_counts["markers_published"] == 6


def test_previews_tracker_without_handlers_still_calls_process_canonical_path_with_same_kwargs():
    from tests.conftest import _ms

    pool = WorkerPool(cpu_workers=1, gpu_workers=0, selected_gpus=[])
    dispatcher = JobDispatcher(pool)
    calls = []

    def pcp(**kwargs):
        calls.append(kwargs)
        return _ms("skipped", canonical_path=kwargs["canonical_path"])

    with patch("media_preview_generator.processing.multi_server.process_canonical_path", side_effect=pcp):
        tracker = dispatcher.submit_items("prev", _items("/m/x.mkv"), _config(), MagicMock())
        assert tracker.wait(timeout=10)
    assert tracker.kind == "previews" and tracker.handlers is None
    assert calls[0]["check_only"] is True and calls[0]["canonical_path"] == "/m/x.mkv"
    assert calls[0]["server_id_filter"] is None and calls[0]["gpu"] is None
    dispatcher.shutdown()
```

```python
# tests/test_jobs_kind_persistence.py
import sqlite3

from media_preview_generator.job_kinds import JOB_KIND_INTRO_CREDITS, JOB_KIND_PREVIEWS, parse_job_kind
from media_preview_generator.web.jobs import JobManager, JobStorage


def test_parse_job_kind_defaults_to_previews():
    assert parse_job_kind("intro_credits") == JOB_KIND_INTRO_CREDITS
    assert parse_job_kind(None) == JOB_KIND_PREVIEWS
    assert parse_job_kind("bogus") == JOB_KIND_PREVIEWS


def test_kind_round_trips_through_storage(tmp_path):
    jm = JobManager(config_dir=str(tmp_path))
    job = jm.create_job(library_name="R&M S01", kind=JOB_KIND_INTRO_CREDITS)
    assert job.to_dict()["kind"] == JOB_KIND_INTRO_CREDITS
    reloaded = JobStorage(str(tmp_path / "jobs.db")).all_jobs()
    assert [j.kind for j in reloaded if j.id == job.id] == [JOB_KIND_INTRO_CREDITS]


def test_legacy_row_without_kind_column_loads_as_previews(tmp_path):
    db = tmp_path / "jobs.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL, "
                 "started_at TEXT, completed_at TEXT, library_id TEXT, library_name TEXT, server_id TEXT, "
                 "server_name TEXT, server_type TEXT, priority INTEGER NOT NULL DEFAULT 2, paused INTEGER NOT NULL "
                 "DEFAULT 0, error TEXT, progress_json TEXT NOT NULL DEFAULT '{}', config_json TEXT NOT NULL DEFAULT "
                 "'{}', publishers_json TEXT NOT NULL DEFAULT '[]')")
    conn.execute("INSERT INTO jobs (id, status, created_at) VALUES ('old', 'completed', '2026-01-01T00:00:00')")
    conn.commit()
    conn.close()
    jobs = JobStorage(str(db)).all_jobs()
    assert jobs[0].kind == JOB_KIND_PREVIEWS
```


Add pause route tests to `tests/test_routes.py`, next to `test_pause_resume_job` (same `client` fixture and
`_api_headers()`):
```python
    @pytest.mark.parametrize("kind", ["previews", "intro_credits"])
    def test_pause_resume_job_matrix_by_kind(self, client, kind):
        """Intro & Credits jobs pause on their own; preview jobs keep the global pause (spec §6.4 item 8)."""
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.settings_manager import get_settings_manager

        sm = get_settings_manager()
        sm.processing_paused = False
        jm = get_job_manager()
        job = jm.create_job(library_name="x", kind=kind)
        jm.start_job(job.id)

        resp = client.post(f"/api/jobs/{job.id}/pause", headers=_api_headers())
        assert resp.status_code == 200
        if kind == "previews":
            assert sm.processing_paused is True
        else:
            assert sm.processing_paused is False
            assert jm.is_pause_requested(job.id) is True
            assert resp.get_json()["paused"] is True

        resp = client.post(f"/api/jobs/{job.id}/resume", headers=_api_headers())
        assert resp.status_code == 200
        assert sm.processing_paused is False
        assert jm.is_pause_requested(job.id) is False

    def test_pause_pending_intro_credits_job_is_409(self, client):
        from media_preview_generator.web.jobs import get_job_manager

        job = get_job_manager().create_job(library_name="x", kind="intro_credits")
        resp = client.post(f"/api/jobs/{job.id}/pause", headers=_api_headers())
        assert resp.status_code == 409
```

- [ ] **Step 2: Run** `pytest --no-cov tests/test_job_kinds_engine.py tests/test_jobs_kind_persistence.py -q` → FAIL
(`ModuleNotFoundError: media_preview_generator.job_kinds`).

- [ ] **Step 3: Implement `job_kinds.py`**

```python
# media_preview_generator/job_kinds.py
"""Job kinds and the per-item outcome contract for non-preview kinds.

Previews keep their original ``process_canonical_path`` flow. Other kinds (Intro & Credits) plug into the same
dispatcher and worker pool through :class:`KindHandlers`, so they share workers, priorities, pause and cancel.
This module imports nothing from the package so both ``web.jobs`` and ``jobs.*`` can use it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace

JOB_KIND_PREVIEWS = "previews"
JOB_KIND_INTRO_CREDITS = "intro_credits"
JOB_KINDS: tuple[str, ...] = (JOB_KIND_PREVIEWS, JOB_KIND_INTRO_CREDITS)


def parse_job_kind(value: object) -> str:
    """Return a known job kind, defaulting to previews."""
    return value if isinstance(value, str) and value in JOB_KINDS else JOB_KIND_PREVIEWS


@dataclass
class ItemOutcome:
    """Result of one item for a non-preview job kind.

    Attributes:
        outcome_key: Per-file outcome counted on the job (must be in the kind's ``outcome_keys``).
        message: Human-readable detail for the Files panel.
        publisher_rows: Per-server rows in the shape ``fold_publisher_rows_into_aggregate`` expects.
    """

    outcome_key: str
    message: str = ""
    publisher_rows: list[dict] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        """Whether the item counts as a failure."""
        return self.outcome_key == "failed"


def outcome_value(key: str) -> object:
    """Wrap an outcome key for ``_notify_file_result``, which reads ``outcome.value``."""
    return SimpleNamespace(value=key)


def normalize_outcome(value: object, outcome_keys: tuple[str, ...]) -> ItemOutcome:
    """Return ``value`` if it is a valid outcome for the kind, else a ``failed`` outcome.

    A handler bug must still count the item exactly once; an unknown key would otherwise be dropped by the
    counters and leave "X processed" and the outcome breakdown disagreeing.
    """
    if isinstance(value, ItemOutcome) and value.outcome_key in outcome_keys:
        return value
    shown = value.outcome_key if isinstance(value, ItemOutcome) else type(value).__name__
    return ItemOutcome("failed", f"internal error: invalid item outcome {shown!r}")


@dataclass(frozen=True)
class KindHandlers:
    """Per-kind item functions used by the dispatcher (check stage) and workers (process stage)."""

    check_fn: Callable[..., ItemOutcome | None]
    process_fn: Callable[..., ItemOutcome]
    outcome_keys: tuple[str, ...]
    check_label: str = "Checking…"
    check_worker_label: str = "Lookup"
    # Largest fraction of the dispatcher's checking threads this kind may hold at once (floor 1). A kind whose
    # check_fn can block (rate-limited online lookups) sets < 1 so it can't starve preview checks.
    check_share: float = 1.0
```

- [ ] **Step 4: `web/jobs.py` changes**

```python
from ..job_kinds import JOB_KIND_PREVIEWS, parse_job_kind
```
`Job`: add field after `parent_schedule_id`: `kind: str = JOB_KIND_PREVIEWS`; in `__post_init__`:
`self.kind = parse_job_kind(self.kind)`; in `to_dict` add `"kind": self.kind`.

`JobStorage._SCHEMA_SQL` jobs table: add `kind TEXT NOT NULL DEFAULT 'previews'` after `parent_schedule_id`.
`_migrate_schema`: add
```python
            try:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN kind TEXT NOT NULL DEFAULT 'previews'")
            except sqlite3.OperationalError:
                pass
```
`upsert`: add `kind` to the column list, one more `?`, `kind=excluded.kind` in `DO UPDATE SET`, and
`d.get("kind") or JOB_KIND_PREVIEWS` as the last parameter. `_row_to_job`: read `row["kind"]` guarded exactly like
`parent_schedule_id` (fallback `JOB_KIND_PREVIEWS`) and pass `kind=`.
`create_job`: add parameter `kind: str = JOB_KIND_PREVIEWS` (docstring: "Job kind — previews or intro_credits.") and
pass `kind=parse_job_kind(kind)` to `Job(...)`.

- [ ] **Step 5: Dispatcher changes (`jobs/dispatcher.py`)**

Imports: `from ..job_kinds import JOB_KIND_PREVIEWS, ItemOutcome, KindHandlers, normalize_outcome, outcome_value`.

`JobTracker.__init__` signature adds `kind: str = JOB_KIND_PREVIEWS, handlers: KindHandlers | None = None`; body:
```python
        self.kind = kind
        self.handlers = handlers
        ...
        if handlers is not None:
            self.outcome_counts = {key: 0 for key in handlers.outcome_keys}
        else:
            self.outcome_counts = {r.value: 0 for r in ProcessingResult}
```
(replace the existing `self.outcome_counts = ...` line). In `record_completion`, replace the hard-coded
`"Checking existing previews…"` with
`(self.handlers.check_label if self.handlers else "Checking existing previews…")`.

New method on `JobTracker`:
```python
    def record_custom_check_result(self, item, outcome: ItemOutcome) -> None:
        """Record an item a kind's ``check_fn`` finished without a worker.

        Mirrors :meth:`record_check_result` for non-preview kinds: count, fold per-server rows, write the
        Files-panel row, complete. Must run inside ``failure_scope(job_id)``.
        """
        if self.done_event.is_set():
            return
        from ..processing.generator import _notify_file_result
        from .orchestrator import fold_publisher_rows_into_aggregate

        label = self.handlers.check_worker_label if self.handlers else "Library scan"
        title = getattr(item, "title", "") or item.canonical_path
        valid = normalize_outcome(outcome, self.handlers.outcome_keys if self.handlers else ())
        if valid is not outcome:
            logger.warning("Dispatcher: {} check for {!r} returned {}; counting it as failed.",
                           self.kind, item.canonical_path, valid.message)
        # record_completion must run exactly once whatever happens above it — a skipped completion leaves
        # tracker.wait() blocked forever while the job holds its gate slot.
        with self._counts_lock:
            self.outcome_counts[valid.outcome_key] = self.outcome_counts.get(valid.outcome_key, 0) + 1
        try:
            rows = list(valid.publisher_rows or [])
            publishers_snapshot = None
            with self._counts_lock:
                if rows:
                    try:
                        fold_publisher_rows_into_aggregate(self.publishers_aggregate, rows)
                        publishers_snapshot = list(self.publishers_aggregate.values())
                    except Exception as exc:
                        logger.debug("Could not fold publisher rows for {}: {}", item.canonical_path, exc)
            if publishers_snapshot is not None:
                try:
                    from ..web.jobs import get_job_manager

                    get_job_manager().set_publishers(self.job_id, publishers_snapshot)
                except Exception as exc:
                    logger.debug("Could not set publisher aggregate for job {}: {}", self.job_id, exc)
            try:
                _notify_file_result(
                    item.canonical_path, outcome_value(valid.outcome_key), valid.message, label, servers=rows
                )
            except Exception as exc:
                logger.debug("Could not notify file result for {}: {}", item.canonical_path, exc)
        except Exception as exc:
            logger.warning("Dispatcher: recording the {} result for {!r} failed: {}", self.kind, item.canonical_path, exc)
        finally:
            self.record_completion(not valid.failed, label, title)
```

`JobDispatcher.submit_items` adds `kind: str = JOB_KIND_PREVIEWS, handlers: KindHandlers | None = None` and passes
both to `JobTracker(...)`; add `kind` to the "submitted" log line.

`_assign_tasks`: add `process_fn=tracker.handlers.process_fn if tracker.handlers else None,` and
`outcome_keys=tracker.handlers.outcome_keys if tracker.handlers else None,` to `worker.assign_task(...)`.

Per-kind check cap. `JobDispatcher.__init__`: `self._checks_by_kind: dict[str, int] = {}` next to
`_checks_in_flight`. New helper:
```python
    def _kind_check_cap(self, tracker: JobTracker) -> int | None:
        """In-flight check limit for the tracker's kind, or None when the kind may use every checking thread."""
        if tracker.handlers is None or tracker.handlers.check_share >= 1:
            return None
        return max(1, int(self._max_checks * tracker.handlers.check_share))
```
`_get_next_check_item`: before taking `_trackers_lock`, snapshot `by_kind = dict(self._checks_by_kind)` under
`_checks_in_flight_lock` (never hold both locks at once); in the `for tracker in eligible` loop, skip a tracker when
`(cap := self._kind_check_cap(tracker)) is not None and by_kind.get(tracker.kind, 0) >= cap`. Only the dispatch loop
picks and increments, so the snapshot can't be overtaken by another picker. `_submit_checks`: in the block that does `self._checks_in_flight += 1`, also do
`self._checks_by_kind[tracker.kind] = self._checks_by_kind.get(tracker.kind, 0) + 1`.
`_run_check_and_release` calls `self._on_check_done(tracker.kind)`; `_on_check_done(self, kind: str = JOB_KIND_PREVIEWS)`
decrements `_checks_by_kind[kind]` (floor 0) in the same locked block as `_checks_in_flight`.

`_run_check`: directly after the cancel/done guard:
```python
        if tracker.handlers is not None:
            self._run_custom_check(tracker, item)
            return
```
New method:
```python
    def _run_custom_check(self, tracker: JobTracker, item) -> None:
        """Check stage for a non-preview kind: terminal outcome → record; None or an exception → worker queue."""
        from ..processing.generator import failure_scope
        from .worker import register_job_thread

        register_job_thread(tracker.job_id)
        with failure_scope(tracker.job_id):
            try:
                outcome = tracker.handlers.check_fn(item, cancel_check=tracker.cancel_check)
            except Exception as exc:
                logger.debug(
                    "Dispatcher: {} check raised for {!r} ({}: {}); routing to a worker.",
                    tracker.kind, item.canonical_path, type(exc).__name__, exc,
                )
                outcome = None
            if outcome is None:
                if not tracker.is_cancelled() and not tracker.done_event.is_set():
                    tracker.item_queue.append(item)
                return
            tracker.record_custom_check_result(item, outcome)
```

- [ ] **Step 6: Worker changes (`jobs/worker.py`)**

Import `from ..job_kinds import ItemOutcome, normalize_outcome, outcome_value`. `Worker.__init__`:
`self.process_fn = None`, `self.outcome_keys: tuple[str, ...] = ()`.
`assign_task(..., pause_check=None, process_fn=None, outcome_keys=None)`: set `self.process_fn = process_fn` and
`self.outcome_keys = tuple(outcome_keys or ())` next to `self.pause_check = pause_check`.

At the very top of `_process_item` (before `from ..processing.multi_server import ...`):
```python
        if self.process_fn is not None:
            try:
                self._process_custom_item(item, progress_callback)
            finally:
                if self._done_event is not None:
                    self._done_event.set()
            return
```
New method:
```python
    def _process_custom_item(self, item, progress_callback) -> None:
        """Run a non-preview kind's ``process_fn`` with the same GPU→CPU fallback previews use."""
        register_job_thread(self.current_job_id or "")
        display_name = self.media_file or self.media_title or item.canonical_path
        with failure_scope(self.current_job_id):
            logger.info("{} picked up: {}", self.display_name, display_name)

            def _phase_cb(text: str) -> None:
                self.current_phase = text or ""

            def _run(gpu, gpu_device):
                return self.process_fn(
                    item,
                    gpu=gpu,
                    gpu_device_path=gpu_device,
                    progress_callback=progress_callback,
                    phase_callback=_phase_cb,
                    cancel_check=self.cancel_check,
                    pause_check=self.pause_check,
                )

            try:
                outcome = _run(self.gpu, self.gpu_device)
            except CancellationError:
                outcome = ItemOutcome("failed", "cancelled by user")
            except CodecNotSupportedError as exc:
                if self.worker_type == "GPU" and not (self.cancel_check and self.cancel_check()):
                    self.fallback_active = True
                    self.fallback_reason = str(exc) or "GPU processing failed"
                    logger.warning(
                        "{} couldn't process {} on the GPU and is retrying on CPU. Reason: {}",
                        self.display_name, display_name, self.fallback_reason,
                    )
                    try:
                        outcome = _run(None, None)
                    except CancellationError:
                        outcome = ItemOutcome("failed", "cancelled during CPU fallback")
                    except Exception as fallback_exc:
                        logger.exception("{} also failed on CPU for {}", self.display_name, display_name)
                        outcome = ItemOutcome("failed", f"CPU fallback failed: {fallback_exc}")
                else:
                    outcome = ItemOutcome("failed", f"codec error: {exc}")
            except Exception as exc:
                logger.exception("{} failed on {}; other items keep processing.", self.display_name, display_name)
                outcome = ItemOutcome("failed", str(exc) or type(exc).__name__)

            valid = normalize_outcome(outcome, self.outcome_keys)
            if valid is not outcome:
                logger.warning("{} got {} for {}; counting it as failed.", self.display_name, valid.message, display_name)
            outcome = valid
            # Counters first: nothing below may skip them, or the job's outcome breakdown drops this item.
            self.outcome_counts[outcome.outcome_key] = self.outcome_counts.get(outcome.outcome_key, 0) + 1
            if outcome.failed:
                self.failed += 1
            else:
                self.completed += 1
            try:
                rows = list(outcome.publisher_rows or [])
            except TypeError:
                logger.warning("{} got unusable publisher rows for {}", self.display_name, display_name)
                rows = []
            self.last_publishers = rows
            self.last_ms_message = outcome.message or ""
            try:
                _notify_file_result(
                    item.canonical_path,
                    outcome_value(outcome.outcome_key),
                    outcome.message,
                    self.display_name,
                    servers=rows,
                )
            except Exception as persist_exc:
                logger.warning("Failed to persist result for {}: {}", item.canonical_path, persist_exc)
```

- [ ] **Step 7: Per-job pause for Intro & Credits (`web/routes/api_jobs.py`)**

```python
@api.route("/jobs/<job_id>/pause", methods=["POST"])
@api_token_required
def pause_job(job_id):
    """Pause a job. Intro & Credits jobs pause on their own; preview jobs pause all processing (legacy)."""
    job_manager = get_job_manager()
    job = job_manager.get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job.kind == JOB_KIND_INTRO_CREDITS:
        if not job_manager.request_pause(job_id):
            return jsonify({"error": "Only running jobs can be paused"}), 409
        return jsonify(job_manager.get_job(job_id).to_dict())
    return pause_processing()
```
`resume_job` mirrors it with `request_resume`. Import `JOB_KIND_INTRO_CREDITS` from `...job_kinds`.

- [ ] **Step 8: Run** `pytest --no-cov tests/test_job_kinds_engine.py tests/test_jobs_kind_persistence.py tests/test_dispatcher_checking_stage.py tests/test_job_dispatcher.py tests/test_dispatcher_kwargs_matrix.py tests/test_jobs.py -q` → PASS, then full `pytest` → PASS.

- [ ] **Step 9: Commit** — `feat(jobs): per-kind handlers in the shared dispatcher and worker, Job.kind, per-job pause for Intro & Credits`

---
## Task 3: Models + decision rules

Spec §5.5, design artifact "How it decides". Pure code, no I/O.

**Files:**
- Create: `media_preview_generator/markers/models.py`, `media_preview_generator/markers/decide.py`
- Test: `tests/markers/test_decide.py`

**Interfaces:**
- Produces (used by every later markers task):
```python
# markers/models.py
class MarkerType(str, Enum): INTRO="intro"; CREDITS="credits"; RECAP="recap"; PREVIEW="preview"
class Source(str, Enum): CHAPTERS="chapters"; THEINTRODB="theintrodb"; INTRODB="introdb"; SKIPDB="skipdb"
                         SEASON_AUDIO="season_audio"; CREDITS_TEXT="credits_text"; SERVER_MARKERS="server_markers"; USER="user"
LOCAL_SOURCES: frozenset[Source]          # chapters, season_audio, credits_text
@dataclass(frozen=True) class Candidate: type: MarkerType; start_ms: int; end_ms: int | None; source: Source
                                         confidence: float = 1.0; origin: str = ""
@dataclass(frozen=True) class Marker: type: MarkerType; start_ms: int; end_ms: int; decided_by: tuple[str, ...]; locked: bool = False
@dataclass(frozen=True) class FileIdentity: canonical_path: str; size: int; mtime_ns: int
@dataclass(frozen=True) class MediaIds: kind: str = "unknown"; tmdb: str | None = None; imdb: str | None = None
                                        tvdb: str | None = None; season: int | None = None; episode: int | None = None
    @property is_episode -> bool          # kind == "episode"
# markers/decide.py
class DecisionStatus(str, Enum): DECIDED="decided"; NEEDS_REVIEW="needs_review"; NO_EVIDENCE="no_evidence"; DISABLED="disabled"
@dataclass(frozen=True) class DecisionContext: duration_ms: int; is_movie: bool; publish_when: str
                                              enabled_types: frozenset[MarkerType]; source_order: tuple[str, ...]
@dataclass(frozen=True) class TypeDecision: type: MarkerType; status: DecisionStatus; marker: Marker | None
                                           proposed: Marker | None; reason: str
def resolve_end_ms(candidate: Candidate, duration_ms: int) -> int
def sanity_problem(candidate: Candidate, ctx: DecisionContext) -> str | None     # None = passes
def decide(candidates: list[Candidate], ctx: DecisionContext, locked: dict[MarkerType, Marker]) -> dict[MarkerType, TypeDecision]
```

- [ ] **Step 1: Write the failing decision matrix tests**

```python
# tests/markers/test_decide.py
"""Spec §5.5 decision rules as a full matrix."""

import pytest

from media_preview_generator.markers.decide import DecisionContext, DecisionStatus, decide, sanity_problem
from media_preview_generator.markers.models import Candidate, Marker, MarkerType, Source

T = MarkerType
S = Source
DUR = 1_320_000  # 22:00 episode
MOVIE_DUR = 6_000_000  # 100:00 movie
ORDER = ("chapters", "theintrodb", "introdb", "skipdb", "season_audio", "credits_text", "server_markers")


def ctx(publish_when="high", is_movie=False, duration=DUR, types=(T.INTRO, T.CREDITS), order=ORDER):
    return DecisionContext(duration, is_movie, publish_when, frozenset(types), order)


def intro(src, start, end, origin=""):
    return Candidate(T.INTRO, start, end, src, origin=origin)


def credits(src, start, end=None, origin=""):
    return Candidate(T.CREDITS, start, end, src, origin=origin)


class TestLockAndDisabled:
    def test_locked_marker_wins_over_everything(self):
        locked = {T.INTRO: Marker(T.INTRO, 1000, 30000, ("user",), locked=True)}
        cands = [intro(S.CHAPTERS, 50000, 80000)]
        d = decide(cands, ctx(), locked)[T.INTRO]
        assert d.status is DecisionStatus.DECIDED and d.marker == locked[T.INTRO]

    def test_disabled_type_is_disabled_even_with_chapters(self):
        d = decide([intro(S.CHAPTERS, 0, 30000)], ctx(types=(T.CREDITS,)), {})[T.INTRO]
        assert d.status is DecisionStatus.DISABLED and d.marker is None

    def test_all_four_types_always_present_in_result(self):
        assert set(decide([], ctx(), {})) == set(T)


class TestChapters:
    def test_chapter_alone_decides_at_high(self):
        d = decide([intro(S.CHAPTERS, 11000, 37000, "Title Sequence")], ctx(), {})[T.INTRO]
        assert d.status is DecisionStatus.DECIDED
        assert (d.marker.start_ms, d.marker.end_ms, d.marker.decided_by) == (11000, 37000, ("chapters",))

    def test_first_intro_chapter_and_last_credits_chapter(self):
        cands = [intro(S.CHAPTERS, 5000, 30000), intro(S.CHAPTERS, 60000, 90000),
                 credits(S.CHAPTERS, 1_200_000, 1_250_000), credits(S.CHAPTERS, 1_290_000, None)]
        out = decide(cands, ctx(), {})
        assert out[T.INTRO].marker.start_ms == 5000
        assert (out[T.CREDITS].marker.start_ms, out[T.CREDITS].marker.end_ms) == (1_290_000, DUR)

    def test_chapter_failing_sanity_is_not_used(self):
        # "Opening" scene chapter of 8 minutes is not an intro
        d = decide([intro(S.CHAPTERS, 0, 480_000)], ctx(), {})[T.INTRO]
        assert d.status is DecisionStatus.NO_EVIDENCE
        assert "sanity" in d.reason


class TestAgreement:
    @pytest.mark.parametrize(
        ("a", "b", "expected"),
        [
            (S.THEINTRODB, S.SKIPDB, DecisionStatus.DECIDED),
            (S.THEINTRODB, S.SEASON_AUDIO, DecisionStatus.DECIDED),
            (S.SKIPDB, S.SERVER_MARKERS, DecisionStatus.DECIDED),  # server markers may confirm
            (S.THEINTRODB, S.INTRODB, DecisionStatus.NEEDS_REVIEW),  # dependent pair = one source
        ],
    )
    def test_intro_pairs(self, a, b, expected):
        cands = [intro(a, 127_000, 157_000), intro(b, 128_000, 160_000)]
        assert decide(cands, ctx(), {})[T.INTRO].status is expected

    def test_dependent_pair_plus_local_source_decides(self):
        cands = [intro(S.THEINTRODB, 127_000, 157_000), intro(S.INTRODB, 128_000, 160_000),
                 intro(S.SEASON_AUDIO, 126_000, 158_000)]
        d = decide(cands, ctx(), {})[T.INTRO]
        assert d.status is DecisionStatus.DECIDED
        assert d.marker.decided_by == ("theintrodb", "introdb", "season_audio")

    def test_two_server_markers_from_different_servers_are_one_source(self):
        cands = [intro(S.SERVER_MARKERS, 127_000, 157_000, "plex-1"), intro(S.SERVER_MARKERS, 127_500, 157_200, "emby-1")]
        assert decide(cands, ctx(), {})[T.INTRO].status is DecisionStatus.NEEDS_REVIEW

    @pytest.mark.parametrize(("end_b", "expected"), [(162_000, DecisionStatus.DECIDED), (162_001, DecisionStatus.NEEDS_REVIEW)])
    def test_intro_end_tolerance_is_5s(self, end_b, expected):
        cands = [intro(S.THEINTRODB, 127_000, 157_000), intro(S.SKIPDB, 120_000, end_b)]
        assert decide(cands, ctx(), {})[T.INTRO].status is expected

    @pytest.mark.parametrize(("start_b", "expected"), [(1_305_000, DecisionStatus.DECIDED), (1_305_001, DecisionStatus.NEEDS_REVIEW)])
    def test_credits_start_tolerance_is_10s(self, start_b, expected):
        cands = [credits(S.THEINTRODB, 1_295_000), credits(S.SKIPDB, start_b, 1_320_000)]
        assert decide(cands, ctx(), {})[T.CREDITS].status is expected

    def test_times_come_from_highest_precedence_source_in_cluster(self):
        cands = [intro(S.SKIPDB, 129_000, 157_800), intro(S.THEINTRODB, 127_894, 156_824)]
        d = decide(cands, ctx(), {})[T.INTRO]
        assert (d.marker.start_ms, d.marker.end_ms) == (127_894, 156_824)
        assert d.marker.decided_by == ("theintrodb", "skipdb")

    def test_user_source_order_changes_precedence(self):
        order = ("skipdb", "theintrodb", "introdb", "chapters", "season_audio", "credits_text", "server_markers")
        cands = [intro(S.SKIPDB, 129_000, 157_800), intro(S.THEINTRODB, 127_894, 156_824)]
        assert decide(cands, ctx(order=order), {})[T.INTRO].marker.start_ms == 129_000

    def test_best_cluster_prefers_more_independent_sources(self):
        cands = [
            intro(S.THEINTRODB, 10_000, 40_000), intro(S.SKIPDB, 11_000, 41_000),
            intro(S.SEASON_AUDIO, 80_000, 110_000), intro(S.CREDITS_TEXT, 81_000, 111_000),
            intro(S.SERVER_MARKERS, 82_000, 112_000),
        ]
        d = decide(cands, ctx(), {})[T.INTRO]
        assert d.marker.end_ms == 110_000


class TestSingleSource:
    @pytest.mark.parametrize(("publish_when", "expected"), [("high", DecisionStatus.NEEDS_REVIEW), ("medium", DecisionStatus.DECIDED)])
    def test_single_online_source(self, publish_when, expected):
        d = decide([intro(S.THEINTRODB, 127_000, 157_000)], ctx(publish_when), {})[T.INTRO]
        assert d.status is expected
        shown = d.marker if expected is DecisionStatus.DECIDED else d.proposed
        assert shown is not None and shown.start_ms == 127_000
        assert (d.marker is None) is (expected is DecisionStatus.NEEDS_REVIEW)

    def test_server_markers_alone_never_decide_even_at_medium(self):
        d = decide([intro(S.SERVER_MARKERS, 127_000, 157_000, "plex-1")], ctx("medium"), {})[T.INTRO]
        assert d.status is DecisionStatus.NEEDS_REVIEW and d.marker is None

    def test_no_candidates(self):
        d = decide([], ctx(), {})[T.CREDITS]
        assert d.status is DecisionStatus.NO_EVIDENCE and d.marker is None and d.proposed is None


class TestSanity:
    @pytest.mark.parametrize(
        ("cand", "is_movie", "duration", "problem"),
        [
            (intro(S.SKIPDB, 0, 30_000), False, DUR, None),
            (intro(S.SKIPDB, 462_000, 490_000), False, DUR, None),  # exactly 35% is allowed
            (intro(S.SKIPDB, 462_001, 490_000), False, DUR, "after 35%"),
            (intro(S.SKIPDB, 10_000, 12_000), False, DUR, "too short"),
            (intro(S.SKIPDB, 10_000, 320_000), False, DUR, "too long"),
            (intro(S.SKIPDB, -1, 20_000), False, DUR, "negative"),
            (credits(S.SKIPDB, 990_000), False, DUR, None),
            (credits(S.SKIPDB, 989_999), False, DUR, "before the last 25%"),
            (credits(S.SKIPDB, 5_100_000), True, MOVIE_DUR, None),  # exactly 900 s from the end is allowed
            (credits(S.SKIPDB, 5_099_999), True, MOVIE_DUR, "more than 900 s"),
            (credits(S.SKIPDB, 4_000_000), False, MOVIE_DUR, "before the last 25%"),
            (credits(S.SKIPDB, 1_380_000, 1_410_534), False, DUR, "starts past the end"),
            (credits(S.SKIPDB, 1_300_000, 1_322_000), False, DUR, None),  # end ≤ 2 s past EOF is clamped
            (credits(S.SKIPDB, 1_300_000, 1_322_001), False, DUR, "ends past the end"),
            (Candidate(T.RECAP, 0, 60_000, S.THEINTRODB), False, DUR, None),
            (Candidate(T.PREVIEW, 1_300_000, 1_320_000, S.THEINTRODB), False, DUR, None),
        ],
    )
    def test_matrix(self, cand, is_movie, duration, problem):
        found = sanity_problem(cand, ctx(is_movie=is_movie, duration=duration))
        if problem is None:
            assert found is None
        else:
            assert found is not None and problem in found

    def test_plex_south_park_late_intro_confirms_nothing(self):
        # Prod example (spec §3.1): Plex intro 76.5–112.7 s vs chapters 11–37 s. Chapter decides; Plex disagrees.
        cands = [intro(S.CHAPTERS, 11_000, 37_000), intro(S.SERVER_MARKERS, 76_508, 112_748, "plex-1")]
        d = decide(cands, ctx(), {})[T.INTRO]
        assert (d.marker.start_ms, d.marker.end_ms) == (11_000, 37_000)

    def test_clamped_end_is_used_in_marker(self):
        d = decide([credits(S.CHAPTERS, 1_300_000, 1_321_500)], ctx(), {})[T.CREDITS]
        assert d.marker.end_ms == DUR
```

- [ ] **Step 2: Run** `pytest --no-cov tests/markers/test_decide.py -q` → FAIL (module missing).

- [ ] **Step 3: Implement `markers/models.py`**

```python
# media_preview_generator/markers/models.py
"""Core value types for Intro & Credits. All times are integer milliseconds."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class MarkerType(str, Enum):
    """Segment types we can decide and publish."""

    INTRO = "intro"
    CREDITS = "credits"
    RECAP = "recap"
    PREVIEW = "preview"


class Source(str, Enum):
    """Where a candidate came from (spec §1 order)."""

    CHAPTERS = "chapters"
    THEINTRODB = "theintrodb"
    INTRODB = "introdb"
    SKIPDB = "skipdb"
    SEASON_AUDIO = "season_audio"
    CREDITS_TEXT = "credits_text"
    SERVER_MARKERS = "server_markers"
    USER = "user"


LOCAL_SOURCES: frozenset[Source] = frozenset({Source.CHAPTERS, Source.SEASON_AUDIO, Source.CREDITS_TEXT})


@dataclass(frozen=True)
class Candidate:
    """One source's claim about a segment.

    Attributes:
        type: Segment type.
        start_ms: Start offset.
        end_ms: End offset, or None when the segment runs to the end of the file.
        source: Evidence source.
        confidence: Source-reported confidence 0–1 (informational; rules don't weight it).
        origin: Free text: the server id for server markers, the chapter title for chapters.
    """

    type: MarkerType
    start_ms: int
    end_ms: int | None
    source: Source
    confidence: float = 1.0
    origin: str = ""


@dataclass(frozen=True)
class Marker:
    """A decided segment: the desired state servers are projected from."""

    type: MarkerType
    start_ms: int
    end_ms: int
    decided_by: tuple[str, ...]
    locked: bool = False


@dataclass(frozen=True)
class FileIdentity:
    """File identity (spec §6.1): path + size + mtime."""

    canonical_path: str
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class MediaIds:
    """External ids for online lookups."""

    kind: str = "unknown"  # "movie" | "episode" | "unknown"
    tmdb: str | None = None
    imdb: str | None = None
    tvdb: str | None = None
    season: int | None = None
    episode: int | None = None

    @property
    def is_episode(self) -> bool:
        """Whether these ids describe a TV episode."""
        return self.kind == "episode"
```

- [ ] **Step 4: Implement `markers/decide.py`**

```python
# media_preview_generator/markers/decide.py
"""Decision rules (spec §5.5): locks, chapters, two independent sources, single source at Medium, sanity bounds."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .models import LOCAL_SOURCES, Candidate, Marker, MarkerType, Source

INTRO_END_TOLERANCE_MS = 5_000
CREDITS_START_TOLERANCE_MS = 10_000
EOF_CLAMP_MS = 2_000
MIN_SEGMENT_MS = 3_000
MAX_INTRO_MS = 300_000
MOVIE_CREDITS_MAX_FROM_END_MS = 900_000
# IntroDB data looks partly seeded from TheIntroDB, so their agreement is one vote (spec §5.5.7).
_INDEPENDENCE_GROUP = {Source.INTRODB: Source.THEINTRODB.value}
_START_SEGMENTS = (MarkerType.INTRO, MarkerType.RECAP)


class DecisionStatus(str, Enum):
    """Outcome of deciding one marker type for one file."""

    DECIDED = "decided"
    NEEDS_REVIEW = "needs_review"
    NO_EVIDENCE = "no_evidence"
    DISABLED = "disabled"


@dataclass(frozen=True)
class DecisionContext:
    """Per-file inputs to the rules."""

    duration_ms: int
    is_movie: bool
    publish_when: str
    enabled_types: frozenset[MarkerType]
    source_order: tuple[str, ...]


@dataclass(frozen=True)
class TypeDecision:
    """Decision for one marker type."""

    type: MarkerType
    status: DecisionStatus
    marker: Marker | None
    proposed: Marker | None
    reason: str


def resolve_end_ms(candidate: Candidate, duration_ms: int) -> int:
    """End offset with "runs to end of file" and ≤ 2 s overshoot both mapped to the duration."""
    if candidate.end_ms is None:
        return duration_ms
    return min(candidate.end_ms, duration_ms)


def sanity_problem(candidate: Candidate, ctx: DecisionContext) -> str | None:
    """Return why a candidate is implausible for this file, or None when it passes."""
    d = ctx.duration_ms
    start = candidate.start_ms
    if start < 0:
        return "negative start"
    if start >= d:
        return "starts past the end of the file"
    if candidate.end_ms is not None and candidate.end_ms > d + EOF_CLAMP_MS:
        return "ends past the end of the file"
    end = resolve_end_ms(candidate, d)
    length = end - start
    if length < MIN_SEGMENT_MS:
        return "segment too short"
    if candidate.type in _START_SEGMENTS:
        if start > 0.35 * d:
            return f"{candidate.type.value} starts after 35% of the file"
        if length > MAX_INTRO_MS:
            return f"{candidate.type.value} too long"
    else:
        if start < 0.75 * d:
            return f"{candidate.type.value} starts before the last 25% of the file"
        if candidate.type is MarkerType.CREDITS and ctx.is_movie and d - start > MOVIE_CREDITS_MAX_FROM_END_MS:
            return "movie credits start more than 900 s before the end"
    return None


def _group(source: Source) -> str:
    return _INDEPENDENCE_GROUP.get(source, source.value)


def _agree(a: Candidate, b: Candidate, duration_ms: int) -> bool:
    if a.type in _START_SEGMENTS:
        return abs(resolve_end_ms(a, duration_ms) - resolve_end_ms(b, duration_ms)) <= INTRO_END_TOLERANCE_MS
    return abs(a.start_ms - b.start_ms) <= CREDITS_START_TOLERANCE_MS


def _precedence(source: Source, order: tuple[str, ...]) -> int:
    try:
        return order.index(source.value)
    except ValueError:
        return len(order)


def _marker_from(best: Candidate, sources: list[Source], ctx: DecisionContext) -> Marker:
    ordered = sorted(dict.fromkeys(sources), key=lambda s: _precedence(s, ctx.source_order))
    return Marker(
        type=best.type,
        start_ms=best.start_ms,
        end_ms=resolve_end_ms(best, ctx.duration_ms),
        decided_by=tuple(s.value for s in ordered),
    )


def _decide_type(mtype: MarkerType, candidates: list[Candidate], ctx: DecisionContext) -> TypeDecision:
    of_type = [c for c in candidates if c.type is mtype]
    sane = [c for c in of_type if sanity_problem(c, ctx) is None]
    if not sane:
        reason = "no evidence" if not of_type else f"{len(of_type)} candidate(s) failed sanity checks"
        return TypeDecision(mtype, DecisionStatus.NO_EVIDENCE, None, None, reason)

    by_precedence = sorted(sane, key=lambda c: (_precedence(c.source, ctx.source_order), -c.confidence))

    chapters = [c for c in sane if c.source is Source.CHAPTERS]
    if chapters:
        chosen = min(chapters, key=lambda c: c.start_ms) if mtype in _START_SEGMENTS else max(chapters, key=lambda c: c.start_ms)
        return TypeDecision(mtype, DecisionStatus.DECIDED, _marker_from(chosen, [Source.CHAPTERS], ctx), None, "chapters")

    best_key = None
    best_cluster: list[Candidate] = []
    for anchor in by_precedence:
        cluster = [c for c in sane if _agree(anchor, c, ctx.duration_ms)]
        groups = {_group(c.source) for c in cluster}
        if len(groups) < 2:
            continue
        edge = [resolve_end_ms(c, ctx.duration_ms) if mtype in _START_SEGMENTS else c.start_ms for c in cluster]
        key = (len(groups), any(c.source in LOCAL_SOURCES for c in cluster), -(max(edge) - min(edge)))
        if best_key is None or key > best_key:
            best_key, best_cluster = key, cluster
    if best_cluster:
        best = min(best_cluster, key=lambda c: (_precedence(c.source, ctx.source_order), -c.confidence))
        marker = _marker_from(best, [c.source for c in best_cluster], ctx)
        return TypeDecision(mtype, DecisionStatus.DECIDED, marker, None, "sources agree: " + ", ".join(marker.decided_by))

    proposal_source = next((c for c in by_precedence if c.source is not Source.SERVER_MARKERS), None)
    if ctx.publish_when == "medium" and proposal_source is not None:
        marker = _marker_from(proposal_source, [proposal_source.source], ctx)
        return TypeDecision(mtype, DecisionStatus.DECIDED, marker, None, f"single source ({proposal_source.source.value})")
    proposed = _marker_from(by_precedence[0], [by_precedence[0].source], ctx)
    return TypeDecision(mtype, DecisionStatus.NEEDS_REVIEW, None, proposed, "sources don't agree yet")


def decide(
    candidates: list[Candidate], ctx: DecisionContext, locked: dict[MarkerType, Marker]
) -> dict[MarkerType, TypeDecision]:
    """Decide every marker type for one file.

    Args:
        candidates: All evidence gathered so far (any types, any sources).
        ctx: File duration, movie flag, publish setting, enabled types and the user's source order.
        locked: User-locked markers by type; they always win.

    Returns:
        A decision for every :class:`MarkerType`.
    """
    out: dict[MarkerType, TypeDecision] = {}
    for mtype in MarkerType:
        if mtype in locked:
            out[mtype] = TypeDecision(mtype, DecisionStatus.DECIDED, locked[mtype], None, "locked by user")
        elif mtype not in ctx.enabled_types:
            out[mtype] = TypeDecision(mtype, DecisionStatus.DISABLED, None, None, "detection off")
        else:
            out[mtype] = _decide_type(mtype, candidates, ctx)
    return out
```

- [ ] **Step 5: Run** `pytest --no-cov tests/markers/test_decide.py -q` → PASS. Fix the implementation, not the tests,
unless a test contradicts spec §5.5 (then stop and ask).

- [ ] **Step 6: Commit** — `feat(markers): decision rules (locks, chapters, independent agreement, sanity bounds)`

---
## Task 4: Marker store (`markers.db`)

Spec §6.1. One SQLite file in `CONFIG_DIR`; `markers` is the single source of truth, servers are projections.

**Files:**
- Create: `media_preview_generator/markers/store.py`
- Test: `tests/markers/test_store.py`

**Interfaces:**
- Consumes: `Candidate, Marker, MarkerType, Source, FileIdentity` (Task 3), `DecisionStatus, TypeDecision` (Task 3).
- Produces:
```python
@dataclass(frozen=True) class FileRecord: id: int; canonical_path: str; size: int; mtime_ns: int
    duration_ms: int | None; season_key: str | None; is_movie: bool; created: bool; changed: bool
@dataclass(frozen=True) class EvidenceRow: source: Source; origin: str; type: MarkerType | None; start_ms: int | None
    end_ms: int | None; confidence: float | None; detail: str; fetched_at: str
@dataclass(frozen=True) class DecisionRow: type: MarkerType; status: DecisionStatus; reason: str
    proposed_start_ms: int | None; proposed_end_ms: int | None; settings_fingerprint: str; decided_at: str
@dataclass(frozen=True) class PublishStateRow: server_id: str; item_id: str | None; markers_hash: str | None
    markers: tuple[Marker, ...]; status: str; message: str; updated_at: str; verified_at: str | None
class MarkerStore:
    def __init__(self, db_path: str, *, clock: Callable[[], datetime] | None = None) -> None   # clock: UTC now (tests inject)
    def close(self) -> None
    def upsert_file(self, identity: FileIdentity, *, duration_ms: int | None, season_key: str | None, is_movie: bool) -> FileRecord
    def get_file(self, canonical_path: str) -> FileRecord | None
    def get_file_by_id(self, file_id: int) -> FileRecord | None
    def files_in_season(self, season_key: str) -> list[FileRecord]
    def replace_evidence(self, file_id: int, source: Source, candidates: list[Candidate], *, origin: str = "", detail: str = "") -> None
    def evidence_fetched_at(self, file_id: int, source: Source, origin: str = "") -> datetime | None
    def get_evidence(self, file_id: int) -> list[Candidate]
    def evidence_rows(self, file_id: int) -> list[EvidenceRow]
    def save_decisions(self, file_id: int, decisions: dict[MarkerType, TypeDecision], *, settings_fingerprint: str) -> None
    def get_markers(self, file_id: int) -> dict[MarkerType, Marker]
    def get_locked(self, file_id: int) -> dict[MarkerType, Marker]
    def lock_marker(self, file_id: int, marker: Marker) -> None        # user-locked marker (stored with locked=1)
    def get_decisions(self, file_id: int) -> dict[MarkerType, DecisionRow]
    def set_publish_state(self, file_id: int, server_id: str, *, item_id: str | None, markers: list[Marker] | None, status: str, message: str = "", verified: bool = False) -> None
        # markers=None keeps the previously published set (e.g. a failed attempt); a list replaces it and its hash
    def get_publish_state(self, file_id: int, server_id: str) -> PublishStateRow | None
    def publish_states(self, file_id: int) -> list[PublishStateRow]
    def record_source_usage(self, source_id: str, *, day: str, used: int | None, limit: int | None, remaining: int | None) -> None
    def source_usage(self, source_id: str, day: str) -> dict | None
    @staticmethod
    def markers_hash(markers: Iterable[Marker]) -> str
def get_marker_store(config_dir: str | None = None) -> MarkerStore     # singleton: <CONFIG_DIR>/markers.db
def reset_marker_store() -> None                                       # tests
```

- [ ] **Step 1: Write failing tests**

```python
# tests/markers/test_store.py
import sqlite3
import threading

import pytest

from media_preview_generator.markers.decide import DecisionStatus, TypeDecision
from media_preview_generator.markers.models import Candidate, FileIdentity, Marker, MarkerType, Source
from media_preview_generator.markers.store import MarkerStore

T = MarkerType


@pytest.fixture
def store(tmp_path):
    s = MarkerStore(str(tmp_path / "markers.db"))
    yield s
    s.close()


def _ident(path="/m/Show/Season 01/S01E01.mkv", size=100, mtime_ns=1):
    return FileIdentity(path, size, mtime_ns)


def _decided(mtype, start, end, by=("chapters",)):
    return TypeDecision(mtype, DecisionStatus.DECIDED, Marker(mtype, start, end, by), None, "chapters")


class _FailOnce:
    """Connection proxy that raises once on one SQL statement (sqlite3.Connection attributes are read-only)."""

    def __init__(self, conn, statement):
        self._conn = conn
        self._statement = statement
        self.fired = False

    def execute(self, sql, *args):
        if sql == self._statement and not self.fired:
            self.fired = True
            raise sqlite3.OperationalError(f"injected failure on {sql}")
        return self._conn.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_failed_begin_releases_the_lock_for_other_threads(store):
    store._conn = _FailOnce(store._conn, "BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.OperationalError):
        store.upsert_file(_ident("/m/a.mkv"), duration_ms=1, season_key=None, is_movie=False)
    done = threading.Event()

    def other_thread():
        store.upsert_file(_ident("/m/b.mkv"), duration_ms=1, season_key=None, is_movie=False)
        done.set()

    threading.Thread(target=other_thread, daemon=True).start()
    assert done.wait(timeout=5), "store lock was never released after BEGIN failed"
    assert store.get_file("/m/b.mkv") is not None


def test_failed_commit_is_rolled_back_and_the_next_write_works(store):
    store._conn = _FailOnce(store._conn, "COMMIT")
    with pytest.raises(sqlite3.OperationalError):
        store.upsert_file(_ident("/m/a.mkv"), duration_ms=1, season_key=None, is_movie=False)
    assert store.get_file("/m/a.mkv") is None
    store.upsert_file(_ident("/m/b.mkv"), duration_ms=1, season_key=None, is_movie=False)
    assert store.get_file("/m/b.mkv") is not None


def test_new_file_then_same_identity_is_not_changed(store):
    a = store.upsert_file(_ident(), duration_ms=1_320_000, season_key="/m/Show/Season 01", is_movie=False)
    b = store.upsert_file(_ident(), duration_ms=1_320_000, season_key="/m/Show/Season 01", is_movie=False)
    assert a.created and not a.changed
    assert b.id == a.id and not b.created and not b.changed
    assert store.get_file(_ident().canonical_path).duration_ms == 1_320_000


@pytest.mark.parametrize("change", [{"size": 101}, {"mtime_ns": 2}])
def test_identity_change_invalidates_evidence_decisions_and_unlocked_markers(store, change):
    rec = store.upsert_file(_ident(), duration_ms=1_000_000, season_key=None, is_movie=True)
    store.replace_evidence(rec.id, Source.SKIPDB, [Candidate(T.CREDITS, 900_000, None, Source.SKIPDB)])
    store.save_decisions(rec.id, {T.CREDITS: _decided(T.CREDITS, 900_000, 1_000_000)}, settings_fingerprint="f")
    store.lock_marker(rec.id, Marker(T.INTRO, 1000, 30_000, ("user",), locked=True))
    published = [Marker(T.CREDITS, 900_000, 1_000_000, ("chapters",))]
    store.set_publish_state(rec.id, "plex-1", item_id="7", markers=published, status="written")

    new = store.upsert_file(_ident(**change), duration_ms=1_100_000, season_key=None, is_movie=True)

    assert new.id == rec.id and new.changed and not new.created
    assert store.get_evidence(rec.id) == []
    assert store.get_decisions(rec.id) == {}
    assert store.get_markers(rec.id) == {T.INTRO: Marker(T.INTRO, 1000, 30_000, ("user",), locked=True)}
    kept = store.get_publish_state(rec.id, "plex-1")  # kept, so the next publish knows what to replace
    assert kept.markers == tuple(published) and kept.markers_hash == MarkerStore.markers_hash(published)
    assert store.get_file_by_id(rec.id).duration_ms == 1_100_000


def test_replace_evidence_is_scoped_to_source_and_origin(store):
    rec = store.upsert_file(_ident(), duration_ms=1_320_000, season_key=None, is_movie=False)
    plex = Candidate(T.INTRO, 76_508, 112_748, Source.SERVER_MARKERS, origin="plex-1")
    emby = Candidate(T.INTRO, 11_000, 37_000, Source.SERVER_MARKERS, origin="emby-1")
    store.replace_evidence(rec.id, Source.SERVER_MARKERS, [plex], origin="plex-1")
    store.replace_evidence(rec.id, Source.SERVER_MARKERS, [emby], origin="emby-1")
    store.replace_evidence(rec.id, Source.SERVER_MARKERS, [], origin="plex-1", detail="no markers")
    assert store.get_evidence(rec.id) == [emby]
    assert store.evidence_fetched_at(rec.id, Source.SERVER_MARKERS, "plex-1") is not None
    rows = store.evidence_rows(rec.id)
    assert {(r.origin, r.type, r.detail) for r in rows} == {("plex-1", None, "no markers"), ("emby-1", T.INTRO, "")}


def test_never_looked_up_source_has_no_fetched_at(store):
    rec = store.upsert_file(_ident(), duration_ms=1, season_key=None, is_movie=False)
    assert store.evidence_fetched_at(rec.id, Source.THEINTRODB) is None


def test_open_ended_candidate_round_trips(store):
    rec = store.upsert_file(_ident(), duration_ms=1_320_000, season_key=None, is_movie=False)
    c = Candidate(T.CREDITS, 1_298_000, None, Source.THEINTRODB, confidence=0.5)
    store.replace_evidence(rec.id, Source.THEINTRODB, [c])
    assert store.get_evidence(rec.id) == [c]


def test_save_decisions_matrix(store):
    rec = store.upsert_file(_ident(), duration_ms=1_320_000, season_key=None, is_movie=False)
    store.save_decisions(rec.id, {T.INTRO: _decided(T.INTRO, 11_000, 37_000), T.CREDITS: _decided(T.CREDITS, 1_299_000, 1_320_000)}, settings_fingerprint="f1")
    proposed = Marker(T.CREDITS, 1_290_000, 1_320_000, ("skipdb",))
    store.save_decisions(rec.id, {
        T.INTRO: _decided(T.INTRO, 12_000, 38_000, ("theintrodb", "skipdb")),
        T.CREDITS: TypeDecision(T.CREDITS, DecisionStatus.NEEDS_REVIEW, None, proposed, "sources don't agree yet"),
        T.RECAP: TypeDecision(T.RECAP, DecisionStatus.DISABLED, None, None, "detection off"),
    }, settings_fingerprint="f2")
    markers = store.get_markers(rec.id)
    assert markers == {T.INTRO: Marker(T.INTRO, 12_000, 38_000, ("theintrodb", "skipdb"))}
    d = store.get_decisions(rec.id)
    assert d[T.CREDITS].status is DecisionStatus.NEEDS_REVIEW
    assert (d[T.CREDITS].proposed_start_ms, d[T.CREDITS].proposed_end_ms) == (1_290_000, 1_320_000)
    assert d[T.RECAP].status is DecisionStatus.DISABLED and d[T.INTRO].settings_fingerprint == "f2"


def test_save_decisions_never_overwrites_locked_marker(store):
    rec = store.upsert_file(_ident(), duration_ms=1_320_000, season_key=None, is_movie=False)
    locked = Marker(T.INTRO, 1000, 30_000, ("user",), locked=True)
    store.lock_marker(rec.id, locked)
    store.save_decisions(rec.id, {T.INTRO: _decided(T.INTRO, 11_000, 37_000)}, settings_fingerprint="f")
    assert store.get_markers(rec.id)[T.INTRO] == locked
    assert store.get_locked(rec.id) == {T.INTRO: locked}


def test_markers_hash_is_order_independent_and_sensitive_to_times(store):
    a = Marker(T.INTRO, 1, 2, ("x",))
    b = Marker(T.CREDITS, 3, 4, ("y",))
    assert MarkerStore.markers_hash([a, b]) == MarkerStore.markers_hash([b, a])
    assert MarkerStore.markers_hash([a]) != MarkerStore.markers_hash([Marker(T.INTRO, 1, 3, ("x",))])
    assert MarkerStore.markers_hash([a]) == MarkerStore.markers_hash([Marker(T.INTRO, 1, 2, ("other",))])


def test_publish_state_upsert_and_list(store):
    rec = store.upsert_file(_ident(), duration_ms=1, season_key=None, is_movie=False)
    first = [Marker(T.INTRO, 1, 5000, ("chapters",))]
    store.set_publish_state(rec.id, "jf-1", item_id="abc", markers=first, status="written", verified=True)
    store.set_publish_state(rec.id, "jf-1", item_id="abc", markers=None, status="failed", message="plugin missing")
    row = store.get_publish_state(rec.id, "jf-1")
    assert (row.status, row.message, row.markers) == ("failed", "plugin missing", tuple(first))
    assert row.markers_hash == MarkerStore.markers_hash(first)
    second = [Marker(T.INTRO, 2, 6000, ("chapters",))]
    store.set_publish_state(rec.id, "jf-1", item_id="abc", markers=second, status="written", verified=True)
    row = store.get_publish_state(rec.id, "jf-1")
    assert (row.status, row.message, row.markers) == ("written", "", tuple(second)) and row.verified_at is not None
    assert [r.server_id for r in store.publish_states(rec.id)] == ["jf-1"]
    assert store.get_publish_state(rec.id, "other") is None


def test_files_in_season(store):
    for i in range(3):
        store.upsert_file(_ident(f"/m/S/Season 01/E0{i}.mkv"), duration_ms=1, season_key="/m/S/Season 01", is_movie=False)
    store.upsert_file(_ident("/m/S/Season 02/E01.mkv"), duration_ms=1, season_key="/m/S/Season 02", is_movie=False)
    assert [f.canonical_path for f in store.files_in_season("/m/S/Season 01")] == [
        "/m/S/Season 01/E00.mkv", "/m/S/Season 01/E01.mkv", "/m/S/Season 01/E02.mkv"]


def test_source_usage_round_trip(store):
    store.record_source_usage("theintrodb", day="2026-09-13", used=83, limit=500, remaining=417)
    assert store.source_usage("theintrodb", "2026-09-13") == {"used": 83, "limit": 500, "remaining": 417}
    assert store.source_usage("theintrodb", "2026-09-14") is None


def test_concurrent_writers(store):
    errors = []

    def work(n):
        try:
            for i in range(20):
                rec = store.upsert_file(_ident(f"/m/{n}/{i}.mkv"), duration_ms=1_000_000, season_key=None, is_movie=True)
                store.replace_evidence(rec.id, Source.SKIPDB, [Candidate(T.CREDITS, 900_000, None, Source.SKIPDB)])
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert store._count("files") == 160 and store._count("evidence") == 160


def test_persists_across_reopen(tmp_path):
    path = str(tmp_path / "markers.db")
    s = MarkerStore(path)
    rec = s.upsert_file(_ident(), duration_ms=5, season_key=None, is_movie=True)
    s.close()
    s2 = MarkerStore(path)
    assert s2.get_file_by_id(rec.id).canonical_path == _ident().canonical_path
    s2.close()
```

- [ ] **Step 2: Run** `pytest --no-cov tests/markers/test_store.py -q` → FAIL.

- [ ] **Step 3: Implement `markers/store.py`**

```python
# media_preview_generator/markers/store.py
"""SQLite store for Intro & Credits (spec §6.1).

One connection shared by the web threads, checking threads and workers: every call takes a lock, multi-statement
changes run in ``BEGIN IMMEDIATE`` transactions. WAL keeps readers unblocked.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

from loguru import logger

from .decide import DecisionStatus, TypeDecision
from .models import Candidate, FileIdentity, Marker, MarkerType, Source

SCHEMA_VERSION = 1

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS files (
        id INTEGER PRIMARY KEY,
        canonical_path TEXT NOT NULL UNIQUE,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        duration_ms INTEGER,
        season_key TEXT,
        is_movie INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_files_season ON files(season_key)",
    """CREATE TABLE IF NOT EXISTS fingerprints (
        file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        window TEXT NOT NULL,
        start_s REAL NOT NULL,
        length_s REAL NOT NULL,
        algorithm INTEGER NOT NULL,
        points BLOB NOT NULL,
        PRIMARY KEY (file_id, window))""",
    """CREATE TABLE IF NOT EXISTS evidence (
        id INTEGER PRIMARY KEY,
        file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        source TEXT NOT NULL,
        origin TEXT NOT NULL DEFAULT '',
        type TEXT,
        start_ms INTEGER,
        end_ms INTEGER,
        confidence REAL,
        detail TEXT NOT NULL DEFAULT '',
        fetched_at TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_evidence_file ON evidence(file_id, source, origin)",
    """CREATE TABLE IF NOT EXISTS markers (
        file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        type TEXT NOT NULL,
        start_ms INTEGER NOT NULL,
        end_ms INTEGER NOT NULL,
        decided_by TEXT NOT NULL,
        locked INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (file_id, type))""",
    """CREATE TABLE IF NOT EXISTS decisions (
        file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        type TEXT NOT NULL,
        status TEXT NOT NULL,
        reason TEXT NOT NULL,
        proposed_start_ms INTEGER,
        proposed_end_ms INTEGER,
        settings_fingerprint TEXT NOT NULL,
        decided_at TEXT NOT NULL,
        PRIMARY KEY (file_id, type))""",
    """CREATE TABLE IF NOT EXISTS publish_state (
        file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        server_id TEXT NOT NULL,
        item_id TEXT,
        markers_hash TEXT,
        markers_json TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL,
        message TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL,
        verified_at TEXT,
        PRIMARY KEY (file_id, server_id))""",
    """CREATE TABLE IF NOT EXISTS source_usage (
        source TEXT NOT NULL,
        day TEXT NOT NULL,
        used INTEGER,
        limit_ INTEGER,
        remaining INTEGER,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (source, day))""",
)


@dataclass(frozen=True)
class FileRecord:
    """A row of ``files`` plus whether the last upsert created or invalidated it."""

    id: int
    canonical_path: str
    size: int
    mtime_ns: int
    duration_ms: int | None
    season_key: str | None
    is_movie: bool
    created: bool = False
    changed: bool = False


@dataclass(frozen=True)
class EvidenceRow:
    """One stored evidence row; ``type`` is None for "looked it up, nothing there"."""

    source: Source
    origin: str
    type: MarkerType | None
    start_ms: int | None
    end_ms: int | None
    confidence: float | None
    detail: str
    fetched_at: str


@dataclass(frozen=True)
class DecisionRow:
    """Stored decision for one marker type."""

    type: MarkerType
    status: DecisionStatus
    reason: str
    proposed_start_ms: int | None
    proposed_end_ms: int | None
    settings_fingerprint: str
    decided_at: str


@dataclass(frozen=True)
class PublishStateRow:
    """What we last sent to one server for one file."""

    server_id: str
    item_id: str | None
    markers_hash: str | None
    markers: tuple[Marker, ...]
    status: str
    message: str
    updated_at: str
    verified_at: str | None


class MarkerStore:
    """Thread-safe access to ``markers.db``."""

    def __init__(self, db_path: str, *, clock: Callable[[], datetime] | None = None) -> None:
        """Open (and create) the store.

        Args:
            db_path: Path to the SQLite file.
            clock: Returns the current UTC datetime (tests inject a fake clock).
        """
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.db_path = db_path
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            for stmt in _SCHEMA:
                self._conn.execute(stmt)
            self._conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
            )

    def _now(self) -> str:
        return self._clock().isoformat()

    def close(self) -> None:
        """Close the connection."""
        with self._lock:
            self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """One ``BEGIN IMMEDIATE`` transaction under the store lock; the lock is released on every path."""
        with self._lock:
            if self._conn.in_transaction:
                # A COMMIT that failed (disk full, I/O error) can leave the transaction open; without this every
                # later BEGIN raises "cannot start a transaction within a transaction".
                self._rollback_quietly()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._rollback_quietly()
                raise
            try:
                self._conn.execute("COMMIT")
            except BaseException:
                self._rollback_quietly()
                raise

    def _rollback_quietly(self) -> None:
        if not self._conn.in_transaction:
            return
        try:
            self._conn.execute("ROLLBACK")
        except sqlite3.Error as exc:
            logger.warning("markers.db rollback failed: {}", exc)

    @staticmethod
    def _file(row: sqlite3.Row, *, created: bool = False, changed: bool = False) -> FileRecord:
        return FileRecord(
            id=row["id"],
            canonical_path=row["canonical_path"],
            size=row["size"],
            mtime_ns=row["mtime_ns"],
            duration_ms=row["duration_ms"],
            season_key=row["season_key"],
            is_movie=bool(row["is_movie"]),
            created=created,
            changed=changed,
        )

    def upsert_file(
        self, identity: FileIdentity, *, duration_ms: int | None, season_key: str | None, is_movie: bool
    ) -> FileRecord:
        """Insert or refresh a file; a size/mtime change invalidates derived data (locked markers survive).

        Returns:
            The record, with ``created``/``changed`` describing what happened.
        """
        now = self._now()
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM files WHERE canonical_path=?", (identity.canonical_path,)).fetchone()
            if row is None:
                cur = conn.execute(
                    "INSERT INTO files (canonical_path, size, mtime_ns, duration_ms, season_key, is_movie, updated_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (identity.canonical_path, identity.size, identity.mtime_ns, duration_ms, season_key, int(is_movie), now),
                )
                file_id, created, changed = cur.lastrowid, True, False
            else:
                file_id, created = row["id"], False
                changed = (row["size"], row["mtime_ns"]) != (identity.size, identity.mtime_ns)
                if changed:
                    for table in ("evidence", "fingerprints", "decisions"):
                        conn.execute(f"DELETE FROM {table} WHERE file_id=?", (file_id,))  # noqa: S608 - fixed names
                    conn.execute("DELETE FROM markers WHERE file_id=? AND locked=0", (file_id,))
                conn.execute(
                    "UPDATE files SET size=?, mtime_ns=?, duration_ms=COALESCE(?, duration_ms), season_key=?, "
                    "is_movie=?, updated_at=? WHERE id=?",
                    (identity.size, identity.mtime_ns, duration_ms, season_key, int(is_movie), now, file_id),
                )
            new_row = conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        return self._file(new_row, created=created, changed=changed)

    def get_file(self, canonical_path: str) -> FileRecord | None:
        """Look a file up by canonical path."""
        with self._lock:
            row = self._conn.execute("SELECT * FROM files WHERE canonical_path=?", (canonical_path,)).fetchone()
        return self._file(row) if row else None

    def get_file_by_id(self, file_id: int) -> FileRecord | None:
        """Look a file up by id."""
        with self._lock:
            row = self._conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        return self._file(row) if row else None

    def files_in_season(self, season_key: str) -> list[FileRecord]:
        """All known files of a season folder, sorted by path."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM files WHERE season_key=? ORDER BY canonical_path", (season_key,)
            ).fetchall()
        return [self._file(r) for r in rows]

    def replace_evidence(
        self, file_id: int, source: Source, candidates: list[Candidate], *, origin: str = "", detail: str = ""
    ) -> None:
        """Replace one source's evidence (per origin). An empty list records "looked it up, nothing there"."""
        now = self._now()
        with self._tx() as conn:
            conn.execute("DELETE FROM evidence WHERE file_id=? AND source=? AND origin=?", (file_id, source.value, origin))
            if not candidates:
                conn.execute(
                    "INSERT INTO evidence (file_id, source, origin, detail, fetched_at) VALUES (?,?,?,?,?)",
                    (file_id, source.value, origin, detail, now),
                )
            for c in candidates:
                conn.execute(
                    "INSERT INTO evidence (file_id, source, origin, type, start_ms, end_ms, confidence, detail, fetched_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (file_id, source.value, origin or c.origin, c.type.value, c.start_ms, c.end_ms, c.confidence, detail, now),
                )

    def evidence_fetched_at(self, file_id: int, source: Source, origin: str = "") -> datetime | None:
        """When a source was last looked up for this file (None = never)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(fetched_at) AS t FROM evidence WHERE file_id=? AND source=? AND origin=?",
                (file_id, source.value, origin),
            ).fetchone()
        return datetime.fromisoformat(row["t"]) if row and row["t"] else None

    def evidence_rows(self, file_id: int) -> list[EvidenceRow]:
        """Every evidence row for a file, including empty lookups."""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM evidence WHERE file_id=? ORDER BY id", (file_id,)).fetchall()
        return [
            EvidenceRow(
                source=Source(r["source"]),
                origin=r["origin"],
                type=MarkerType(r["type"]) if r["type"] else None,
                start_ms=r["start_ms"],
                end_ms=r["end_ms"],
                confidence=r["confidence"],
                detail=r["detail"],
                fetched_at=r["fetched_at"],
            )
            for r in rows
        ]

    def get_evidence(self, file_id: int) -> list[Candidate]:
        """Candidates for the decision rules (empty lookups excluded)."""
        return [
            Candidate(r.type, r.start_ms, r.end_ms, r.source, r.confidence if r.confidence is not None else 1.0, r.origin)
            for r in self.evidence_rows(file_id)
            if r.type is not None and r.start_ms is not None
        ]

    def save_decisions(
        self, file_id: int, decisions: dict[MarkerType, TypeDecision], *, settings_fingerprint: str
    ) -> None:
        """Store decisions; DECIDED writes the marker, anything else removes an unlocked marker of that type."""
        now = self._now()
        with self._tx() as conn:
            for mtype, d in decisions.items():
                locked = conn.execute(
                    "SELECT 1 FROM markers WHERE file_id=? AND type=? AND locked=1", (file_id, mtype.value)
                ).fetchone()
                proposed = d.proposed
                conn.execute(
                    "INSERT OR REPLACE INTO decisions (file_id, type, status, reason, proposed_start_ms, proposed_end_ms, "
                    "settings_fingerprint, decided_at) VALUES (?,?,?,?,?,?,?,?)",
                    (file_id, mtype.value, d.status.value, d.reason, proposed.start_ms if proposed else None,
                     proposed.end_ms if proposed else None, settings_fingerprint, now),
                )
                if locked:
                    continue
                if d.status is DecisionStatus.DECIDED and d.marker is not None:
                    conn.execute(
                        "INSERT OR REPLACE INTO markers (file_id, type, start_ms, end_ms, decided_by, locked, updated_at) "
                        "VALUES (?,?,?,?,?,0,?)",
                        (file_id, mtype.value, d.marker.start_ms, d.marker.end_ms, json.dumps(list(d.marker.decided_by)), now),
                    )
                else:
                    conn.execute("DELETE FROM markers WHERE file_id=? AND type=? AND locked=0", (file_id, mtype.value))

    def _markers(self, file_id: int, locked_only: bool) -> dict[MarkerType, Marker]:
        sql = "SELECT * FROM markers WHERE file_id=?" + (" AND locked=1" if locked_only else "")
        with self._lock:
            rows = self._conn.execute(sql, (file_id,)).fetchall()
        return {
            MarkerType(r["type"]): Marker(
                MarkerType(r["type"]), r["start_ms"], r["end_ms"], tuple(json.loads(r["decided_by"])), bool(r["locked"])
            )
            for r in rows
        }

    def get_markers(self, file_id: int) -> dict[MarkerType, Marker]:
        """Desired markers (decided + locked) for a file."""
        return self._markers(file_id, locked_only=False)

    def get_locked(self, file_id: int) -> dict[MarkerType, Marker]:
        """User-locked markers for a file."""
        return self._markers(file_id, locked_only=True)

    def lock_marker(self, file_id: int, marker: Marker) -> None:
        """Store a user-locked marker; detection never replaces it (the Inspector editor calls this in phase 4)."""
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO markers (file_id, type, start_ms, end_ms, decided_by, locked, updated_at) "
                "VALUES (?,?,?,?,?,1,?)",
                (file_id, marker.type.value, marker.start_ms, marker.end_ms, json.dumps(list(marker.decided_by)), self._now()),
            )

    def get_decisions(self, file_id: int) -> dict[MarkerType, DecisionRow]:
        """Stored decisions by type."""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM decisions WHERE file_id=?", (file_id,)).fetchall()
        return {
            MarkerType(r["type"]): DecisionRow(
                MarkerType(r["type"]), DecisionStatus(r["status"]), r["reason"], r["proposed_start_ms"],
                r["proposed_end_ms"], r["settings_fingerprint"], r["decided_at"],
            )
            for r in rows
        }

    def set_publish_state(
        self,
        file_id: int,
        server_id: str,
        *,
        item_id: str | None,
        markers: list[Marker] | None,
        status: str,
        message: str = "",
        verified: bool = False,
    ) -> None:
        """Record a publish attempt. ``markers=None`` keeps the last successfully published set."""
        now = self._now()
        with self._tx() as conn:
            if markers is None:
                prev = conn.execute(
                    "SELECT markers_hash, markers_json, verified_at FROM publish_state WHERE file_id=? AND server_id=?",
                    (file_id, server_id),
                ).fetchone()
                markers_hash = prev["markers_hash"] if prev else None
                markers_json = prev["markers_json"] if prev else "[]"
                verified_at = prev["verified_at"] if prev else None
            else:
                markers_hash = self.markers_hash(markers)
                markers_json = json.dumps(
                    [[m.type.value, m.start_ms, m.end_ms, list(m.decided_by), m.locked] for m in markers]
                )
                verified_at = now if verified else None
            conn.execute(
                "INSERT OR REPLACE INTO publish_state (file_id, server_id, item_id, markers_hash, markers_json, status, "
                "message, updated_at, verified_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (file_id, server_id, item_id, markers_hash, markers_json, status, message, now, verified_at),
            )

    @staticmethod
    def _publish_row(r: sqlite3.Row) -> PublishStateRow:
        markers = tuple(
            Marker(MarkerType(t), start, end, tuple(by), bool(locked))
            for t, start, end, by, locked in json.loads(r["markers_json"] or "[]")
        )
        return PublishStateRow(r["server_id"], r["item_id"], r["markers_hash"], markers, r["status"], r["message"],
                               r["updated_at"], r["verified_at"])

    def get_publish_state(self, file_id: int, server_id: str) -> PublishStateRow | None:
        """Last publish record for one server."""
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM publish_state WHERE file_id=? AND server_id=?", (file_id, server_id)
            ).fetchone()
        return self._publish_row(r) if r else None

    def publish_states(self, file_id: int) -> list[PublishStateRow]:
        """Publish records for every server, sorted by server id."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM publish_state WHERE file_id=? ORDER BY server_id", (file_id,)
            ).fetchall()
        return [self._publish_row(r) for r in rows]

    def record_source_usage(
        self, source_id: str, *, day: str, used: int | None, limit: int | None, remaining: int | None
    ) -> None:
        """Persist today's usage for a rate-limited source (shown in Settings)."""
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO source_usage (source, day, used, limit_, remaining, updated_at) VALUES (?,?,?,?,?,?)",
                (source_id, day, used, limit, remaining, self._now()),
            )

    def source_usage(self, source_id: str, day: str) -> dict | None:
        """Usage for a source on a UTC day (YYYY-MM-DD)."""
        with self._lock:
            r = self._conn.execute("SELECT * FROM source_usage WHERE source=? AND day=?", (source_id, day)).fetchone()
        return {"used": r["used"], "limit": r["limit_"], "remaining": r["remaining"]} if r else None

    def _count(self, table: str) -> int:
        with self._lock:
            return int(self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608 - tests only

    @staticmethod
    def markers_hash(markers: Iterable[Marker]) -> str:
        """Stable hash of what a server should show (type + times only)."""
        payload = sorted((m.type.value, m.start_ms, m.end_ms) for m in markers)
        return hashlib.sha1(json.dumps(payload).encode(), usedforsecurity=False).hexdigest()


_store: MarkerStore | None = None
_store_lock = threading.Lock()


def get_marker_store(config_dir: str | None = None) -> MarkerStore:
    """Process-wide store at ``<CONFIG_DIR>/markers.db``."""
    global _store
    with _store_lock:
        if _store is None:
            base = config_dir or os.environ.get("CONFIG_DIR", "/config")
            _store = MarkerStore(os.path.join(base, "markers.db"))
        return _store


def reset_marker_store() -> None:
    """Close and forget the singleton (tests)."""
    global _store
    with _store_lock:
        if _store is not None:
            _store.close()
        _store = None
```


- [ ] **Step 4: Run** `pytest --no-cov tests/markers/test_store.py -q` → PASS.
- [ ] **Step 5: Commit** — `feat(markers): markers.db store (files, evidence, decisions, markers, publish state)`

---
## Task 5: Media probe + chapters source

Spec §5.1. Chapters are exact when present; names the published plugins miss must be included.

**Files:**
- Create: `media_preview_generator/markers/probe.py`, `media_preview_generator/markers/sources/__init__.py`,
  `media_preview_generator/markers/sources/chapters.py`
- Test: `tests/markers/test_probe.py`, `tests/markers/test_chapters.py`, `tests/markers/test_chapters_integration.py`
  (`@pytest.mark.integration`, real ffmpeg)

**Interfaces:**
- Produces:
```python
# markers/probe.py
@dataclass(frozen=True) class Chapter: start_ms: int; end_ms: int | None; title: str
@dataclass(frozen=True) class MediaProbe: duration_ms: int | None; chapters: tuple[Chapter, ...]
class ProbeError(Exception)
def ffprobe_path_for(ffmpeg_path: str | None) -> str
def probe_media(path: str, *, ffprobe: str, timeout_s: float = 60.0) -> MediaProbe   # raises ProbeError
# markers/sources/chapters.py
def classify_chapter_title(title: str) -> MarkerType | None
def chapter_candidates(probe: MediaProbe) -> list[Candidate]
```

- [ ] **Step 1: Write failing tests**

```python
# tests/markers/test_chapters.py
import pytest

from media_preview_generator.markers.models import Candidate, MarkerType, Source
from media_preview_generator.markers.probe import Chapter, MediaProbe
from media_preview_generator.markers.sources.chapters import chapter_candidates, classify_chapter_title

T = MarkerType


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Intro", T.INTRO), ("intro", T.INTRO), (" Introduction ", T.INTRO), ("Opening", T.INTRO),
        ("Opening Credits", T.INTRO), ("Opening Titles", T.INTRO), ("Title Sequence", T.INTRO),
        ("Main Titles", T.INTRO), ("Main Title", T.INTRO), ("OP", T.INTRO), ("Theme Song", T.INTRO),
        ("End Credits", T.CREDITS), ("Ending Credits", T.CREDITS), ("Closing Credits", T.CREDITS),
        ("Credits", T.CREDITS), ("End Titles", T.CREDITS), ("Outro", T.CREDITS), ("ED", T.CREDITS),
        ("Recap", T.RECAP), ("Previously", T.RECAP), ("Previously On", T.RECAP), ("Previously on Lost", T.RECAP),
        ("Story So Far", T.RECAP),
        ("Preview", T.PREVIEW), ("Next Episode", T.PREVIEW), ("Next Time", T.PREVIEW), ("Next Episode Preview", T.PREVIEW),
        ("Chapter 1", None), ("Scene 2", None), ("Part 01", None), ("", None), ("End", None), ("Ending", None),
        ("The Opening Night", None), ("Credits Roll Party", None), ("00:00:00.000", None),
    ],
)
def test_classify_matrix(title, expected):
    assert classify_chapter_title(title) is expected


def test_candidates_use_chapter_bounds_and_skip_unnamed():
    probe = MediaProbe(
        duration_ms=1_680_709,
        chapters=(
            Chapter(0, 67_500, "Part 01"),
            Chapter(67_500, 86_250, "Intro"),
            Chapter(86_250, 1_524_450, "Part 02"),
            Chapter(1_524_450, None, "Credits"),
        ),
    )
    assert chapter_candidates(probe) == [
        Candidate(T.INTRO, 67_500, 86_250, Source.CHAPTERS, origin="Intro"),
        Candidate(T.CREDITS, 1_524_450, None, Source.CHAPTERS, origin="Credits"),
    ]


def test_no_chapters():
    assert chapter_candidates(MediaProbe(duration_ms=1000, chapters=())) == []
```

```python
# tests/markers/test_probe.py
import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.markers.probe import Chapter, ProbeError, ffprobe_path_for, probe_media

RUN = "media_preview_generator.markers.probe.subprocess.run"


def _ok(payload):
    return MagicMock(returncode=0, stdout=json.dumps(payload), stderr="")


def test_parses_duration_and_chapters_and_passes_exact_args():
    payload = {
        "format": {"duration": "1321.472000"},
        "chapters": [
            {"start_time": "0.000000", "end_time": "127.961000", "tags": {"title": "Chapter 1"}},
            {"start_time": "127.961000", "end_time": "159.826000", "tags": {"TITLE": "Title Sequence"}},
            {"start_time": "159.826000", "end_time": "1321.472000", "tags": {}},
        ],
    }
    with patch(RUN, return_value=_ok(payload)) as run:
        probe = probe_media("/m/a.mkv", ffprobe="/usr/bin/ffprobe")
    args, kwargs = run.call_args
    assert args[0] == ["/usr/bin/ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_chapters", "/m/a.mkv"]
    assert kwargs["timeout"] == 60.0 and kwargs["capture_output"] is True and kwargs["text"] is True
    assert probe.duration_ms == 1_321_472
    assert probe.chapters == (
        Chapter(0, 127_961, "Chapter 1"),
        Chapter(127_961, 159_826, "Title Sequence"),
        Chapter(159_826, 1_321_472, ""),
    )


def test_missing_duration_is_none():
    with patch(RUN, return_value=_ok({"format": {}, "chapters": []})):
        assert probe_media("/m/a.mkv", ffprobe="ffprobe").duration_ms is None


@pytest.mark.parametrize(
    "side_effect",
    [
        subprocess.TimeoutExpired(cmd="ffprobe", timeout=60),
        FileNotFoundError("ffprobe"),
        None,  # non-zero return code
    ],
)
def test_failures_raise_probe_error(side_effect):
    ret = MagicMock(returncode=1, stdout="", stderr="Invalid data found when processing input")
    with patch(RUN, side_effect=side_effect, return_value=ret):
        with pytest.raises(ProbeError):
            probe_media("/m/a.mkv", ffprobe="ffprobe")


def test_invalid_json_raises():
    with patch(RUN, return_value=MagicMock(returncode=0, stdout="not json", stderr="")):
        with pytest.raises(ProbeError):
            probe_media("/m/a.mkv", ffprobe="ffprobe")


def test_ffprobe_path_prefers_sibling_of_ffmpeg(tmp_path):
    ffmpeg = tmp_path / "ffmpeg"
    ffprobe = tmp_path / "ffprobe"
    ffmpeg.write_text("")
    ffprobe.write_text("")
    ffprobe.chmod(0o755)
    assert ffprobe_path_for(str(ffmpeg)) == str(ffprobe)


def test_ffprobe_path_falls_back_to_path_lookup(tmp_path):
    with patch("media_preview_generator.markers.probe.shutil.which", return_value="/usr/bin/ffprobe"):
        assert ffprobe_path_for(str(tmp_path / "ffmpeg")) == "/usr/bin/ffprobe"
        assert ffprobe_path_for(None) == "/usr/bin/ffprobe"
```

```python
# tests/markers/test_chapters_integration.py
"""Real ffmpeg/ffprobe: a generated file with named chapters decodes to the right candidates."""

import shutil
import subprocess

import pytest

from media_preview_generator.markers.models import MarkerType
from media_preview_generator.markers.probe import probe_media
from media_preview_generator.markers.sources.chapters import chapter_candidates

pytestmark = pytest.mark.integration


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="needs ffmpeg")
def test_generated_mkv_with_chapters(tmp_path):
    meta = tmp_path / "meta.txt"
    meta.write_text(
        ";FFMETADATA1\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=5000\ntitle=Previously\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=5000\nEND=20000\ntitle=Opening Credits\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=20000\nEND=50000\ntitle=Chapter 2\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=50000\nEND=60000\ntitle=End Credits\n"
    )
    out = tmp_path / "x.mkv"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=64x36:d=60", "-f", "lavfi", "-i",
         "anullsrc=r=48000:cl=stereo", "-i", str(meta), "-map_metadata", "2", "-map_chapters", "2",
         "-shortest", "-c:v", "libx264", "-c:a", "aac", str(out)],
        check=True,
    )
    probe = probe_media(str(out), ffprobe="ffprobe")
    assert abs(probe.duration_ms - 60_000) < 200
    types = [(c.type, c.start_ms, c.end_ms) for c in chapter_candidates(probe)]
    assert types == [(MarkerType.RECAP, 0, 5000), (MarkerType.INTRO, 5000, 20000), (MarkerType.CREDITS, 50000, 60000)]
```

- [ ] **Step 2: Run** `pytest --no-cov tests/markers/test_probe.py tests/markers/test_chapters.py -q` → FAIL.

- [ ] **Step 3: Implement**

```python
# media_preview_generator/markers/probe.py
"""ffprobe wrapper for duration and chapters."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass


class ProbeError(Exception):
    """ffprobe could not read the file."""


@dataclass(frozen=True)
class Chapter:
    """A container chapter (times in ms)."""

    start_ms: int
    end_ms: int | None
    title: str


@dataclass(frozen=True)
class MediaProbe:
    """What the marker pipeline needs from ffprobe."""

    duration_ms: int | None
    chapters: tuple[Chapter, ...]


def ffprobe_path_for(ffmpeg_path: str | None) -> str:
    """Prefer the ffprobe shipped next to the configured ffmpeg (jellyfin-ffmpeg in the image)."""
    if ffmpeg_path:
        sibling = os.path.join(os.path.dirname(ffmpeg_path), "ffprobe")
        if os.path.isfile(sibling) and os.access(sibling, os.X_OK):
            return sibling
    return shutil.which("ffprobe") or "ffprobe"


def _ms(value: object) -> int | None:
    try:
        return int(round(float(value) * 1000))
    except (TypeError, ValueError):
        return None


def probe_media(path: str, *, ffprobe: str, timeout_s: float = 60.0) -> MediaProbe:
    """Read duration and chapters.

    Args:
        path: Media file.
        ffprobe: ffprobe binary.
        timeout_s: Hard timeout (hung mounts must not hold a check thread forever).

    Returns:
        Duration (None if unknown) and chapters in container order.

    Raises:
        ProbeError: ffprobe missing, timed out, failed or returned invalid JSON.
    """
    cmd = [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_chapters", path]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise ProbeError(f"ffprobe failed for {path}: {type(exc).__name__}: {exc}") from exc
    if proc.returncode != 0:
        raise ProbeError(f"ffprobe exited {proc.returncode} for {path}: {(proc.stderr or '').strip()[:300]}")
    try:
        data = json.loads(proc.stdout or "")
    except ValueError as exc:
        raise ProbeError(f"ffprobe returned invalid JSON for {path}") from exc
    chapters = []
    for raw in data.get("chapters") or []:
        tags = {str(k).lower(): v for k, v in (raw.get("tags") or {}).items()}
        start = _ms(raw.get("start_time"))
        if start is None:
            continue
        chapters.append(Chapter(start, _ms(raw.get("end_time")), str(tags.get("title") or "")))
    return MediaProbe(duration_ms=_ms((data.get("format") or {}).get("duration")), chapters=tuple(chapters))
```

```python
# media_preview_generator/markers/sources/__init__.py
"""Evidence sources for Intro & Credits."""
```

```python
# media_preview_generator/markers/sources/chapters.py
"""Chapter names → candidates (spec §5.1). Whole-title matches only: "The Opening Night" is a scene, not an intro."""

from __future__ import annotations

import re

from ..models import Candidate, MarkerType, Source
from ..probe import MediaProbe

# "End"/"Ending" alone are common final-scene names in movies, so they are deliberately not credits.
_PATTERNS: tuple[tuple[MarkerType, re.Pattern[str]], ...] = (
    (MarkerType.INTRO, re.compile(
        r"^(intro(duction)?|opening( credits| titles?)?|title sequence|main titles?|op|theme( song)?)$", re.I)),
    (MarkerType.CREDITS, re.compile(r"^((end|ending|closing) credits|credits|end titles?|outro|ed)$", re.I)),
    (MarkerType.RECAP, re.compile(r"^(recap|previously( on\b.*)?|story so far)$", re.I)),
    (MarkerType.PREVIEW, re.compile(r"^(preview|next (episode|time)( preview)?( on\b.*)?)$", re.I)),
)


def classify_chapter_title(title: str) -> MarkerType | None:
    """Map a chapter title to a marker type, or None for ordinary chapters."""
    text = " ".join((title or "").split())
    for mtype, pattern in _PATTERNS:
        if pattern.match(text):
            return mtype
    return None


def chapter_candidates(probe: MediaProbe) -> list[Candidate]:
    """Candidates for every named intro/credits/recap/preview chapter (end None = to end of file)."""
    out = []
    for chapter in probe.chapters:
        mtype = classify_chapter_title(chapter.title)
        if mtype is not None:
            out.append(Candidate(mtype, chapter.start_ms, chapter.end_ms, Source.CHAPTERS, origin=chapter.title.strip()))
    return out
```

Note `test_candidates_use_chapter_bounds_and_skip_unnamed` expects `origin="Intro"`.

- [ ] **Step 4: Run** unit tests → PASS; run the integration test explicitly on storage:
`pytest --no-cov -m integration tests/markers/test_chapters_integration.py -q` → PASS.
- [ ] **Step 5: Commit** — `feat(markers): ffprobe probe and chapter-name source`

---

## Task 6: External ids (path + server metadata)

Spec §5.2. Owner's library uses `{tvdb-…}` show folders and `{tmdb-…}`/`{imdb-…}` movie names, so paths often give
ids without any server call.

**Files:**
- Create: `media_preview_generator/markers/external_ids.py`
- Modify: `media_preview_generator/servers/base.py` (`MediaServer.get_external_ids`, default returns None),
  `media_preview_generator/servers/plex.py` (implementation), `media_preview_generator/servers/_embyish.py`
  (implementation shared by Emby and Jellyfin)
- Test: `tests/markers/test_external_ids.py`, `tests/test_servers_plex.py` (append), `tests/test_servers_jellyfin.py`
  and `tests/test_servers_emby.py` (append)

**Interfaces:**
- Produces:
```python
# markers/external_ids.py
def ids_from_path(canonical_path: str) -> MediaIds
def ids_from_server_dict(raw: dict | None) -> MediaIds
def merge_ids(primary: MediaIds, fallback: MediaIds) -> MediaIds
# servers/base.py
def get_external_ids(self, item_id: str) -> dict[str, Any] | None
    # {"kind": "movie"|"episode"|"unknown", "tmdb": str|None, "imdb": str|None, "tvdb": str|None,
    #  "season": int|None, "episode": int|None}; series-level ids for episodes
```

- [ ] **Step 1: Write failing tests**

```python
# tests/markers/test_external_ids.py
import pytest

from media_preview_generator.markers.external_ids import ids_from_path, ids_from_server_dict, merge_ids
from media_preview_generator.markers.models import MediaIds


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/data_16tb2/TV Shows/Rick and Morty (2013) {tvdb-275274}/Season 01/Rick and Morty (2013) - S01E01 - Pilot [WEBDL-1080p].mkv",
         MediaIds("episode", tvdb="275274", season=1, episode=1)),
        ("/data_16tb/Movies/Toy Story (1995) {tmdb-862}/Toy Story (1995) {imdb-tt0114709} - [Bluray-2160p].mkv",
         MediaIds("movie", tmdb="862", imdb="tt0114709")),
        ("/m/Shows/Lost [tvdbid-73739] [imdbid-tt0411008]/Season 2/Lost S02E03.mkv",
         MediaIds("episode", tvdb="73739", imdb="tt0411008", season=2, episode=3)),
        ("/m/Movies/Up (2009) [tmdbid-14160]/Up (2009).mkv", MediaIds("movie", tmdb="14160")),
        # episode-level ids in a file name are NOT series ids and must be ignored
        ("/m/TV/Show {tvdb-1}/Season 01/Show - S01E02 {imdb-tt9999999}.mkv", MediaIds("episode", tvdb="1", season=1, episode=2)),
        ("/m/TV/Show/Season 03/show.s03e10.1080p.mkv", MediaIds("episode", season=3, episode=10)),
        ("/m/home videos/birthday.mkv", MediaIds("unknown")),
    ],
)
def test_ids_from_path(path, expected):
    assert ids_from_path(path) == expected


def test_server_dict_normalises():
    raw = {"kind": "episode", "tmdb": 60625, "imdb": "tt2861424", "tvdb": None, "season": "1", "episode": 2}
    assert ids_from_server_dict(raw) == MediaIds("episode", tmdb="60625", imdb="tt2861424", season=1, episode=2)
    assert ids_from_server_dict(None) == MediaIds()
    assert ids_from_server_dict({"kind": "show"}) == MediaIds("unknown")


def test_merge_prefers_primary_fields_and_known_kind():
    primary = MediaIds("unknown", tvdb="275274", season=1, episode=1)
    fallback = MediaIds("episode", tmdb="60625", imdb="tt2861424", tvdb="999", season=9, episode=9)
    assert merge_ids(primary, fallback) == MediaIds("episode", tmdb="60625", imdb="tt2861424", tvdb="275274", season=1, episode=1)
```

Plex (append to `tests/test_servers_plex.py`, reuse its existing `PlexServer` construction helper/fixture):
```python
class TestGetExternalIds:
    def _xml(self, text):
        import xml.etree.ElementTree as ET
        return ET.fromstring(text)

    def test_episode_uses_show_guids_and_indexes(self, plex_server_under_test):
        episode = self._xml('<MediaContainer><Video type="episode" parentIndex="1" index="3" grandparentRatingKey="99">'
                            '<Guid id="imdb://tt5555555"/></Video></MediaContainer>')
        show = self._xml('<MediaContainer><Directory type="show"><Guid id="imdb://tt2861424"/><Guid id="tmdb://60625"/>'
                         '<Guid id="tvdb://275274"/></Directory></MediaContainer>')
        conn = plex_server_under_test._connect.return_value
        conn.query.side_effect = [episode, show]
        ids = plex_server_under_test.get_external_ids("/library/metadata/123")
        assert ids == {"kind": "episode", "tmdb": "60625", "imdb": "tt2861424", "tvdb": "275274", "season": 1, "episode": 3}
        assert [c.args[0] for c in conn.query.call_args_list] == [
            "/library/metadata/123?includeGuids=1", "/library/metadata/99?includeGuids=1"]

    def test_movie(self, plex_server_under_test):
        movie = self._xml('<MediaContainer><Video type="movie"><Guid id="tmdb://862"/><Guid id="imdb://tt0114709"/>'
                          '</Video></MediaContainer>')
        plex_server_under_test._connect.return_value.query.return_value = movie
        assert plex_server_under_test.get_external_ids("862") == {
            "kind": "movie", "tmdb": "862", "imdb": "tt0114709", "tvdb": None, "season": None, "episode": None}

    def test_query_failure_returns_none(self, plex_server_under_test):
        plex_server_under_test._connect.return_value.query.side_effect = RuntimeError("down")
        assert plex_server_under_test.get_external_ids("1") is None
```
(`plex_server_under_test`: a `PlexServer` whose `_connect` is a `MagicMock`; build it like the existing tests in that
file do, and patch `media_preview_generator.plex_client.retry_plex_call` to call through:
`side_effect=lambda f, *a, **k: f(*a, **k)`.)

Emby/Jellyfin (append to both vendor test files; parametrise over `EmbyServer`/`JellyfinServer`):
```python
@pytest.mark.parametrize("user_id", [None, "u1"])
def test_get_external_ids_episode_fetches_series_provider_ids(make_server, user_id):
    server = make_server(user_id=user_id)
    episode = {"Type": "Episode", "ParentIndexNumber": 1, "IndexNumber": 2, "SeriesId": "series-9",
               "ProviderIds": {"Imdb": "tt7777777"}}
    series = {"Type": "Series", "ProviderIds": {"Tmdb": "60625", "Imdb": "tt2861424", "Tvdb": "275274"}}
    server._fetch_item_fields = MagicMock(side_effect=[episode, series])
    assert server.get_external_ids("ep-1") == {"kind": "episode", "tmdb": "60625", "imdb": "tt2861424",
                                              "tvdb": "275274", "season": 1, "episode": 2}
    assert [c.args for c in server._fetch_item_fields.call_args_list] == [
        ("ep-1", "ProviderIds,ParentIndexNumber,IndexNumber,SeriesId"), ("series-9", "ProviderIds")]


@pytest.mark.parametrize(("user_id", "expected_path", "expected_params"), [
    (None, "/Items", {"Ids": "x", "Fields": "ProviderIds"}),
    ("u1", "/Users/u1/Items/x", {"Fields": "ProviderIds"}),
])
def test_fetch_item_fields_endpoint_by_auth_shape(make_server, user_id, expected_path, expected_params):
    server = make_server(user_id=user_id)
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"Items": [{"Id": "x"}]} if user_id is None else {"Id": "x"}
    server._request = MagicMock(return_value=resp)
    assert server._fetch_item_fields("x", "ProviderIds") == {"Id": "x"}
    server._request.assert_called_once_with("GET", expected_path, params=expected_params)
```
(`make_server`: local fixture building the vendor client with `ServerConfig(auth={"token": "t", "user_id": user_id})`
the same way the existing tests in those files construct clients.)

- [ ] **Step 2: Run** the new tests → FAIL.

- [ ] **Step 3: Implement**

```python
# media_preview_generator/markers/external_ids.py
"""External ids for online lookups: parsed from the path, completed from server metadata."""

from __future__ import annotations

import os
import re
from typing import Any

from .models import MediaIds

_ID_RE = re.compile(r"[\{\[](tmdb|tvdb|imdb)(?:id)?-((?:tt)?\d+)[\}\]]", re.IGNORECASE)
_SXXEYY_RE = re.compile(r"(?<![a-z0-9])s(\d{1,2})e(\d{1,3})(?![0-9])", re.IGNORECASE)
_SEASON_DIR_RE = re.compile(r"^(season|series|staffel|saison)\s*(\d{1,3})$", re.IGNORECASE)


def _ids_in(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for scheme, value in _ID_RE.findall(text):
        found.setdefault(scheme.lower(), value.lower() if scheme.lower() == "imdb" else value)
    return found


def ids_from_path(canonical_path: str) -> MediaIds:
    """Parse ids, season and episode from a TRaSH/Jellyfin-style path.

    For episodes only folders above the season folder are read: ids in an episode file name belong to the episode,
    while every online source wants the series id.
    """
    parts = [p for p in canonical_path.replace("\\", "/").split("/") if p]
    if not parts:
        return MediaIds()
    filename = parts[-1]
    folders = parts[:-1]
    m = _SXXEYY_RE.search(os.path.splitext(filename)[0])
    if m:
        season, episode = int(m.group(1)), int(m.group(2))
        show_folders = folders[:-1] if folders and _SEASON_DIR_RE.match(folders[-1]) else folders
        found: dict[str, str] = {}
        for folder in reversed(show_folders):
            for k, v in _ids_in(folder).items():
                found.setdefault(k, v)
        return MediaIds("episode", found.get("tmdb"), found.get("imdb"), found.get("tvdb"), season, episode)
    found = _ids_in(filename)
    if folders:
        for k, v in _ids_in(folders[-1]).items():
            found.setdefault(k, v)
    if found:
        return MediaIds("movie", found.get("tmdb"), found.get("imdb"), found.get("tvdb"))
    return MediaIds()


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def ids_from_server_dict(raw: dict | None) -> MediaIds:
    """Normalise a ``MediaServer.get_external_ids`` result."""
    if not isinstance(raw, dict):
        return MediaIds()
    kind = raw.get("kind") if raw.get("kind") in ("movie", "episode") else "unknown"

    def _s(key: str) -> str | None:
        value = raw.get(key)
        return str(value) if value not in (None, "") else None

    return MediaIds(kind, _s("tmdb"), _s("imdb"), _s("tvdb"), _int(raw.get("season")), _int(raw.get("episode")))


def merge_ids(primary: MediaIds, fallback: MediaIds) -> MediaIds:
    """Field-wise merge: primary wins; kind comes from whichever side knows it (primary first)."""
    kind = primary.kind if primary.kind != "unknown" else fallback.kind
    return MediaIds(
        kind,
        primary.tmdb or fallback.tmdb,
        primary.imdb or fallback.imdb,
        primary.tvdb or fallback.tvdb,
        primary.season if primary.season is not None else fallback.season,
        primary.episode if primary.episode is not None else fallback.episode,
    )
```

`servers/base.py` `MediaServer`:
```python
    def get_external_ids(self, item_id: str) -> dict[str, Any] | None:
        """Return tmdb/imdb/tvdb ids plus season/episode for an item (series ids for episodes).

        Default: unsupported → None. Vendors override.
        """
        return None
```

`servers/plex.py` `PlexServer`:
```python
    def get_external_ids(self, item_id: str) -> dict[str, Any] | None:
        """Plex guids (``includeGuids=1``); episodes read the show's guids via ``grandparentRatingKey``."""
        from ..plex_client import retry_plex_call

        bare_id = str(item_id or "").strip().rsplit("/", 1)[-1]
        if not bare_id:
            return None

        def _first_node(key: str):
            root = retry_plex_call(self._connect().query, f"/library/metadata/{key}?includeGuids=1")
            return next(iter(root), None) if root is not None else None

        try:
            node = _first_node(bare_id)
            if node is None:
                return None
            kind = node.get("type")
            guid_node = node
            if kind == "episode" and node.get("grandparentRatingKey"):
                guid_node = _first_node(node.get("grandparentRatingKey")) or node
        except Exception as exc:
            logger.debug("Plex external-id lookup failed for {}: {}", bare_id, exc)
            return None
        out: dict[str, Any] = {"kind": kind if kind in ("movie", "episode") else "unknown",
                               "tmdb": None, "imdb": None, "tvdb": None, "season": None, "episode": None}
        if kind == "episode":
            out["season"] = int(node.get("parentIndex")) if (node.get("parentIndex") or "").isdigit() else None
            out["episode"] = int(node.get("index")) if (node.get("index") or "").isdigit() else None
        for guid in guid_node.findall("Guid"):
            scheme, _, value = (guid.get("id") or "").partition("://")
            if scheme in ("tmdb", "imdb", "tvdb") and value:
                out[scheme] = value
        return out
```

`servers/_embyish.py` `EmbyApiClient`:
```python
    def _fetch_item_fields(self, item_id: str, fields: str) -> dict[str, Any] | None:
        """Fetch one item with extra ``Fields`` using the endpoint that works for this auth shape."""
        user_id = self._user_id()
        try:
            if user_id:
                resp = self._request("GET", f"/Users/{user_id}/Items/{item_id}", params={"Fields": fields})
                resp.raise_for_status()
                data = resp.json()
            else:
                resp = self._request("GET", "/Items", params={"Ids": item_id, "Fields": fields})
                resp.raise_for_status()
                items = resp.json().get("Items") or []
                data = items[0] if items else None
        except Exception as exc:
            logger.debug("{} item field lookup failed for {}: {}", self.vendor_name, item_id, exc)
            return None
        return data if isinstance(data, dict) else None

    def get_external_ids(self, item_id: str) -> dict[str, Any] | None:
        """ProviderIds (series ids for episodes) plus season/episode numbers."""
        item = self._fetch_item_fields(item_id, "ProviderIds,ParentIndexNumber,IndexNumber,SeriesId")
        if item is None:
            return None
        item_type = str(item.get("Type") or "")
        kind = {"Movie": "movie", "Episode": "episode"}.get(item_type, "unknown")
        providers_source = item
        if kind == "episode" and item.get("SeriesId"):
            providers_source = self._fetch_item_fields(str(item["SeriesId"]), "ProviderIds") or item
        providers = {str(k).lower(): str(v) for k, v in (providers_source.get("ProviderIds") or {}).items() if v}
        return {
            "kind": kind,
            "tmdb": providers.get("tmdb"),
            "imdb": providers.get("imdb"),
            "tvdb": providers.get("tvdb"),
            "season": item.get("ParentIndexNumber") if kind == "episode" else None,
            "episode": item.get("IndexNumber") if kind == "episode" else None,
        }
```

- [ ] **Step 4: Run** `pytest --no-cov tests/markers/test_external_ids.py tests/test_servers_plex.py tests/test_servers_jellyfin.py tests/test_servers_emby.py -q` → PASS.
- [ ] **Step 5: Lab check (storage, read-only):** for one Rick and Morty S01 item on the lab Jellyfin 10.11, lab Emby and
lab Plex, call `get_external_ids` from a Python shell using a `ServerConfig` built from `evidence/lab/env` and confirm
`tmdb=60625`, `imdb=tt2861424`, season/episode correct. Record the output in `evidence/lab/phase1-results.md`.
- [ ] **Step 6: Commit** — `feat(markers): external ids from paths and Plex/Emby/Jellyfin metadata`

---
## Task 7: Rate limiter + TheIntroDB / IntroDB.app / SkipDB clients

Spec §4 (accuracy, limits), §5.2, §5.6. Pace from response headers, never hard-code daily numbers; back off on 429;
circuit-break on repeated 5xx; webhook jobs (priority ≤ NORMAL) get first call on the daily budget.

**Files:**
- Create: `media_preview_generator/markers/sources/ratelimit.py`, `media_preview_generator/markers/sources/online.py`
  (shared `LookupResult`, session, user agent), `.../sources/theintrodb.py`, `.../sources/introdb.py`, `.../sources/skipdb.py`
- Test: `tests/markers/test_ratelimit.py`, `tests/markers/test_online_sources.py`,
  `tests/markers/test_online_sources_live.py` (`@pytest.mark.integration`, real HTTP, 3 requests total)

**Interfaces:**
- Consumes: `Candidate, MarkerType, Source, MediaIds` (Task 3).
- Produces:
```python
# sources/ratelimit.py
PRIORITY_LOW = 3                                     # mirrors web.jobs.PRIORITY_LOW (no web import here)
class Acquire(str, Enum): ALLOWED="allowed"; BUDGET_EXHAUSTED="budget_exhausted"; BLOCKED="blocked"; CANCELLED="cancelled"
class SourceLimiter:
    def __init__(self, source_id: str, *, min_interval_s: float, reserve_fraction: float = 0.2, failure_threshold: int = 5,
                 circuit_open_s: float = 600.0, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, utc_day: Callable[[], str] = <UTC YYYY-MM-DD>,
                 on_usage: Callable[[str, str, int, int | None, int | None], None] | None = None) -> None
    def acquire(self, *, priority: int, cancel_check: Callable[[], bool] | None = None, max_wait_s: float = 60.0) -> Acquire
    def record(self, status_code: int | None, headers: Mapping[str, str] | None) -> None
    def usage(self) -> dict      # {"day","used","limit","remaining","blocked_until_s"}
def get_limiter(source_id: str) -> SourceLimiter     # theintrodb 0.34 s, introdb 0.5 s, skipdb 0.5 s
def reset_limiters() -> None
# sources/online.py
@dataclass(frozen=True) class LookupResult: status: str; candidates: tuple[Candidate, ...] = (); detail: str = ""
    # status: "ok" | "no_data" | "unavailable" | "not_applicable"
def http_session() -> requests.Session               # shared, User-Agent "MediaPreviewGenerator/<version>"
# sources/theintrodb.py / introdb.py / skipdb.py
class TheIntroDbClient:  def __init__(self, api_key: str = "", *, limiter: SourceLimiter | None = None, session=None)
                         def lookup(self, ids: MediaIds, *, duration_ms: int | None, priority: int, cancel_check=None) -> LookupResult
class IntroDbClient:     def __init__(self, *, limiter=None, session=None); def lookup(...same...) -> LookupResult
class SkipDbClient:      def __init__(self, *, limiter=None, session=None); def lookup(...same...) -> LookupResult
```
Result semantics used by the pipeline: `ok` and `no_data` are stored as evidence (no_data = empty row, re-queried after
14 days); `unavailable` and `not_applicable` store nothing (retry next time).

- [ ] **Step 1: Write failing limiter tests**

```python
# tests/markers/test_ratelimit.py
import pytest

from media_preview_generator.markers.sources.ratelimit import Acquire, SourceLimiter


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(round(s, 3))
        self.t += s


def _limiter(clock, **kw):
    return SourceLimiter("theintrodb", min_interval_s=0.34, clock=clock.now, sleep=clock.sleep,
                         utc_day=lambda: "2026-09-13", **kw)


def test_spaces_requests_by_min_interval():
    c = FakeClock()
    lim = _limiter(c)
    assert lim.acquire(priority=2) is Acquire.ALLOWED
    assert lim.acquire(priority=2) is Acquire.ALLOWED
    assert sum(c.slept) == pytest.approx(0.34, abs=0.01)


def test_ratelimit_headers_remaining_zero_waits_for_reset():
    c = FakeClock()
    lim = _limiter(c)
    lim.acquire(priority=2)
    lim.record(200, {"x-ratelimit-limit": "30", "x-ratelimit-remaining": "0", "x-ratelimit-reset": "10"})
    assert lim.acquire(priority=2) is Acquire.ALLOWED
    assert sum(c.slept) == pytest.approx(10, abs=0.5)


def test_429_blocks_using_retry_after_and_returns_blocked_when_wait_too_long():
    c = FakeClock()
    lim = _limiter(c)
    lim.acquire(priority=2)
    lim.record(429, {"Retry-After": "300"})
    assert lim.acquire(priority=2, max_wait_s=60) is Acquire.BLOCKED
    c.t += 301
    assert lim.acquire(priority=2) is Acquire.ALLOWED


def test_usage_headers_reserve_budget_for_webhook_priorities():
    c = FakeClock()
    lim = _limiter(c)
    lim.acquire(priority=3)
    lim.record(200, {"x-usagelimit-limit": "500", "x-usagelimit-remaining": "100"})
    assert lim.acquire(priority=3) is Acquire.BUDGET_EXHAUSTED  # 100 <= 20% of 500
    assert lim.acquire(priority=1) is Acquire.ALLOWED
    assert lim.acquire(priority=2) is Acquire.ALLOWED


def test_daily_budget_zero_exhausts_every_priority_until_next_day():
    c = FakeClock()
    day = {"v": "2026-09-13"}
    lim = SourceLimiter("theintrodb", min_interval_s=0.0, clock=c.now, sleep=c.sleep, utc_day=lambda: day["v"])
    lim.acquire(priority=1)
    lim.record(200, {"x-usagelimit-limit": "500", "x-usagelimit-remaining": "0"})
    assert lim.acquire(priority=1) is Acquire.BUDGET_EXHAUSTED
    day["v"] = "2026-09-14"
    assert lim.acquire(priority=3) is Acquire.ALLOWED


@pytest.mark.parametrize("failure", [500, 502, None])
def test_circuit_opens_after_consecutive_failures_and_closes_after_timeout(failure):
    c = FakeClock()
    lim = _limiter(c, failure_threshold=3, circuit_open_s=600)
    for _ in range(3):
        assert lim.acquire(priority=2) is Acquire.ALLOWED
        lim.record(failure, None)
    assert lim.acquire(priority=2, max_wait_s=5) is Acquire.BLOCKED
    c.t += 601
    assert lim.acquire(priority=2) is Acquire.ALLOWED


def test_success_resets_failure_count():
    c = FakeClock()
    lim = _limiter(c, failure_threshold=2)
    lim.acquire(priority=2)
    lim.record(500, None)
    lim.acquire(priority=2)
    lim.record(200, {})
    lim.acquire(priority=2)
    lim.record(500, None)
    assert lim.acquire(priority=2, max_wait_s=1) is Acquire.ALLOWED


def test_cancel_while_waiting():
    c = FakeClock()
    lim = _limiter(c)
    lim.acquire(priority=2)
    lim.record(200, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "10"})
    assert lim.acquire(priority=2, cancel_check=lambda: True) is Acquire.CANCELLED


def test_usage_callback_receives_header_values():
    c = FakeClock()
    seen = []
    lim = _limiter(c, on_usage=lambda *a: seen.append(a))
    lim.acquire(priority=2)
    lim.record(200, {"x-usagelimit-limit": "500", "x-usagelimit-remaining": "417"})
    assert seen[-1] == ("theintrodb", "2026-09-13", 1, 500, 417)
    assert lim.usage()["used"] == 1 and lim.usage()["remaining"] == 417
```

- [ ] **Step 2: Write failing client tests**

```python
# tests/markers/test_online_sources.py
from unittest.mock import MagicMock

import pytest
import requests

from media_preview_generator.markers.models import Candidate, MarkerType, MediaIds, Source
from media_preview_generator.markers.sources.introdb import IntroDbClient
from media_preview_generator.markers.sources.ratelimit import Acquire
from media_preview_generator.markers.sources.skipdb import SkipDbClient
from media_preview_generator.markers.sources.theintrodb import TheIntroDbClient

T = MarkerType
RM_S01E01 = MediaIds("episode", tmdb="60625", imdb="tt2861424", tvdb="275274", season=1, episode=1)
TOY_STORY = MediaIds("movie", tmdb="862", imdb="tt0114709")


def _resp(status=200, body=None, headers=None):
    r = MagicMock(status_code=status, headers=headers or {})
    r.json.return_value = body
    return r


def _limiter(result=Acquire.ALLOWED):
    lim = MagicMock()
    lim.acquire.return_value = result
    return lim


class TestTheIntroDb:
    def test_episode_request_params_and_parsing(self):
        session = MagicMock()
        session.get.return_value = _resp(200, {
            "tmdb_id": 60625, "type": "tv", "season": 1, "episode": 1,
            "intro": [{"start_ms": 127894, "end_ms": 156824}], "recap": [], "preview": [],
            "credits": [{"start_ms": 1298000, "end_ms": None}],
        }, {"x-usagelimit-remaining": "417"})
        lim = _limiter()
        result = TheIntroDbClient("", limiter=lim, session=session).lookup(RM_S01E01, duration_ms=1_321_472, priority=2)
        args, kwargs = session.get.call_args
        assert args[0] == "https://api.theintrodb.org/v3/media"
        assert kwargs["params"] == {"tmdb_id": "60625", "season": 1, "episode": 1, "duration_ms": 1_321_472}
        assert "Authorization" not in kwargs["headers"] and kwargs["timeout"] == 15
        lim.acquire.assert_called_once()
        assert lim.acquire.call_args.kwargs["priority"] == 2
        lim.record.assert_called_once_with(200, {"x-usagelimit-remaining": "417"})
        assert result.status == "ok"
        assert result.candidates == (
            Candidate(T.INTRO, 127_894, 156_824, Source.THEINTRODB),
            Candidate(T.CREDITS, 1_298_000, None, Source.THEINTRODB),
        )

    def test_key_sent_as_bearer_and_null_start_is_zero(self):
        session = MagicMock()
        session.get.return_value = _resp(200, {"intro": [{"start_ms": None, "end_ms": 30000}], "credits": []})
        result = TheIntroDbClient("k-123", limiter=_limiter(), session=session).lookup(RM_S01E01, duration_ms=None, priority=3)
        assert session.get.call_args.kwargs["headers"]["Authorization"] == "Bearer k-123"
        assert "duration_ms" not in session.get.call_args.kwargs["params"]
        assert result.candidates == (Candidate(T.INTRO, 0, 30_000, Source.THEINTRODB),)

    @pytest.mark.parametrize(("ids", "param"), [
        (MediaIds("episode", tvdb="275274", season=1, episode=1), {"tvdb_id": "275274"}),
        (MediaIds("episode", imdb="tt2861424", season=1, episode=1), {"imdb_id": "tt2861424"}),
        (TOY_STORY, {"tmdb_id": "862"}),
    ])
    def test_id_preference(self, ids, param):
        session = MagicMock()
        session.get.return_value = _resp(404, {"error": "not found"})
        result = TheIntroDbClient(limiter=_limiter(), session=session).lookup(ids, duration_ms=1000, priority=2)
        params = session.get.call_args.kwargs["params"]
        assert {k: v for k, v in params.items() if k.endswith("_id")} == param
        assert ("season" in params) is ids.is_episode
        assert result.status == "no_data"

    @pytest.mark.parametrize("ids", [MediaIds("episode", season=1, episode=1), MediaIds("episode", tmdb="1"), MediaIds()])
    def test_not_applicable_without_ids_makes_no_request(self, ids):
        session = MagicMock()
        lim = _limiter()
        assert TheIntroDbClient(limiter=lim, session=session).lookup(ids, duration_ms=1, priority=2).status == "not_applicable"
        session.get.assert_not_called()
        lim.acquire.assert_not_called()

    @pytest.mark.parametrize(("status", "detail"), [(401, "API key"), (403, "API key"), (500, "HTTP 500"), (429, "HTTP 429")])
    def test_error_statuses_are_unavailable(self, status, detail):
        session = MagicMock()
        session.get.return_value = _resp(status, {})
        result = TheIntroDbClient("k", limiter=_limiter(), session=session).lookup(RM_S01E01, duration_ms=1, priority=2)
        assert result.status == "unavailable" and detail in result.detail
        assert "k" not in result.detail.replace("key", "")  # never echo the key

    def test_network_error_records_failure(self):
        session = MagicMock()
        session.get.side_effect = requests.ConnectionError("dns")
        lim = _limiter()
        result = TheIntroDbClient(limiter=lim, session=session).lookup(RM_S01E01, duration_ms=1, priority=2)
        assert result.status == "unavailable"
        lim.record.assert_called_once_with(None, None)

    @pytest.mark.parametrize("acq", [Acquire.BLOCKED, Acquire.BUDGET_EXHAUSTED, Acquire.CANCELLED])
    def test_limiter_refusal_is_unavailable_without_request(self, acq):
        session = MagicMock()
        result = TheIntroDbClient(limiter=_limiter(acq), session=session).lookup(RM_S01E01, duration_ms=1, priority=3)
        assert result.status == "unavailable" and acq.value in result.detail
        session.get.assert_not_called()

    def test_200_with_no_segments_is_no_data(self):
        session = MagicMock()
        session.get.return_value = _resp(200, {"intro": [], "credits": [], "recap": [], "preview": []})
        assert TheIntroDbClient(limiter=_limiter(), session=session).lookup(RM_S01E01, duration_ms=1, priority=2).status == "no_data"


class TestIntroDb:
    def test_request_and_parsing(self):
        session = MagicMock()
        session.get.return_value = _resp(200, {
            "imdb_id": "tt2861424", "season": 1, "episode": 1,
            "intro": {"start_ms": 128000, "end_ms": 160000, "confidence": 1},
            "recap": None,
            "outro": {"start_ms": 1295000, "end_ms": 1321000, "confidence": 0.5},
        })
        result = IntroDbClient(limiter=_limiter(), session=session).lookup(RM_S01E01, duration_ms=1_321_472, priority=2)
        assert session.get.call_args.args[0] == "https://api.introdb.app/segments"
        assert session.get.call_args.kwargs["params"] == {"imdb_id": "tt2861424", "season": 1, "episode": 1}
        assert result.candidates == (
            Candidate(T.INTRO, 128_000, 160_000, Source.INTRODB, confidence=1.0),
            Candidate(T.CREDITS, 1_295_000, 1_321_000, Source.INTRODB, confidence=0.5),
        )

    @pytest.mark.parametrize("ids", [TOY_STORY, MediaIds("episode", tmdb="60625", season=1, episode=1)])
    def test_movies_and_missing_imdb_not_applicable(self, ids):
        session = MagicMock()
        assert IntroDbClient(limiter=_limiter(), session=session).lookup(ids, duration_ms=1, priority=2).status == "not_applicable"
        session.get.assert_not_called()


class TestSkipDb:
    def test_episode_request_params_and_match_filter(self):
        session = MagicMock()
        session.get.return_value = _resp(200, {"segments": {
            "intro": {"start_ms": 129000, "end_ms": 157800, "match": "exact", "confidence": 0.93},
            "recap": {"start_ms": 0, "end_ms": 20000, "match": "out-of-range", "confidence": 0.2},
            "outro": {"start_ms": 1296000, "end_ms": 1320000, "match": "shifted", "confidence": 0.8},
            "preview": None,
        }})
        result = SkipDbClient(limiter=_limiter(), session=session).lookup(RM_S01E01, duration_ms=1_321_472, priority=2)
        assert session.get.call_args.args[0] == "https://api.skipdb.tv/api/segments"
        assert session.get.call_args.kwargs["params"] == {
            "imdb_id": "tt2861424", "season": 1, "episode": 1, "duration": 1321.472, "adjust": "conservative"}
        assert result.candidates == (
            Candidate(T.INTRO, 129_000, 157_800, Source.SKIPDB, confidence=0.93),
            Candidate(T.CREDITS, 1_296_000, 1_320_000, Source.SKIPDB, confidence=0.8),
        )

    def test_movie_has_no_season_params(self):
        session = MagicMock()
        session.get.return_value = _resp(200, {"segments": {"intro": None, "recap": None, "outro": None, "preview": None}})
        result = SkipDbClient(limiter=_limiter(), session=session).lookup(TOY_STORY, duration_ms=4_866_050, priority=2)
        assert session.get.call_args.kwargs["params"] == {"imdb_id": "tt0114709", "duration": 4866.05, "adjust": "conservative"}
        assert result.status == "no_data"

    def test_requires_duration(self):
        session = MagicMock()
        assert SkipDbClient(limiter=_limiter(), session=session).lookup(RM_S01E01, duration_ms=None, priority=2).status == "not_applicable"
        session.get.assert_not_called()
```

```python
# tests/markers/test_online_sources_live.py
"""Live smoke test against the three public APIs (3 requests). Run manually: pytest -m integration."""

import pytest

from media_preview_generator.markers.models import MarkerType, MediaIds
from media_preview_generator.markers.sources.introdb import IntroDbClient
from media_preview_generator.markers.sources.skipdb import SkipDbClient
from media_preview_generator.markers.sources.theintrodb import TheIntroDbClient

pytestmark = pytest.mark.integration
RM = MediaIds("episode", tmdb="60625", imdb="tt2861424", season=1, episode=1)


@pytest.mark.parametrize("client", [TheIntroDbClient(), IntroDbClient(), SkipDbClient()], ids=["tidb", "introdb", "skipdb"])
def test_rick_and_morty_s01e01_has_an_intro_near_2m07(client):
    result = client.lookup(RM, duration_ms=1_321_472, priority=1)
    assert result.status == "ok", result.detail
    intro = next(c for c in result.candidates if c.type is MarkerType.INTRO)
    assert 120_000 <= intro.start_ms <= 135_000 and 150_000 <= intro.end_ms <= 165_000
```
(The autouse `_fail_unmocked_network_fast` fixture blocks real sockets; this file must opt out the same way other
integration tests in `tests/integration/` do — check `tests/conftest.py:43` for the marker/fixture name and apply it.)

- [ ] **Step 3: Run** → FAIL (modules missing).

- [ ] **Step 4: Implement the limiter**

```python
# media_preview_generator/markers/sources/ratelimit.py
"""Header-driven pacing for online sources (spec §4 "Limits").

One limiter per source, shared by every job. Requests are serialised through a reserved time slot so concurrent
check threads never burst past the per-window limit. Daily budgets come from ``x-usagelimit-*`` headers; LOW
priority (backfill) stops at a 20% reserve so webhook-triggered jobs keep first call on the day's quota.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from enum import Enum

PRIORITY_LOW = 3
_MAX_BLOCK_S = 3600.0
_DEFAULT_429_S = 300.0


def _utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class Acquire(str, Enum):
    """Outcome of asking for a request slot."""

    ALLOWED = "allowed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


def _header(headers: Mapping[str, str] | None, name: str) -> int | None:
    if not headers:
        return None
    lowered = {str(k).lower(): v for k, v in headers.items()}
    try:
        return int(float(lowered[name]))
    except (KeyError, TypeError, ValueError):
        return None


class SourceLimiter:
    """Paces one online source."""

    def __init__(
        self,
        source_id: str,
        *,
        min_interval_s: float,
        reserve_fraction: float = 0.2,
        failure_threshold: int = 5,
        circuit_open_s: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        utc_day: Callable[[], str] = _utc_day,
        on_usage: Callable[[str, str, int, int | None, int | None], None] | None = None,
    ) -> None:
        """Create a limiter.

        Args:
            source_id: Source id for logs/usage.
            min_interval_s: Minimum spacing between requests.
            reserve_fraction: Share of the daily budget LOW priority may not use.
            failure_threshold: Consecutive network/5xx failures that open the circuit.
            circuit_open_s: How long an open circuit blocks requests.
            clock: Monotonic clock (tests inject a fake).
            sleep: Sleep function (tests inject a fake).
            utc_day: Returns the current UTC day string.
            on_usage: Called after each response with (source, day, used, limit, remaining).
        """
        self.source_id = source_id
        self._min_interval = min_interval_s
        self._reserve = reserve_fraction
        self._failure_threshold = failure_threshold
        self._circuit_open_s = circuit_open_s
        self._clock = clock
        self._sleep = sleep
        self._utc_day = utc_day
        self._on_usage = on_usage
        self._lock = threading.Lock()
        self._next_slot = 0.0
        self._blocked_until = 0.0
        self._failures = 0
        self._day = utc_day()
        self._used = 0
        self._limit: int | None = None
        self._remaining: int | None = None

    def _roll_day(self) -> None:
        day = self._utc_day()
        if day != self._day:
            self._day, self._used, self._remaining = day, 0, None

    def acquire(self, *, priority: int, cancel_check: Callable[[], bool] | None = None, max_wait_s: float = 60.0) -> Acquire:
        """Reserve the next request slot, sleeping until it arrives.

        Returns:
            ALLOWED when the caller may send one request now; otherwise why not.
        """
        with self._lock:
            self._roll_day()
            now = self._clock()
            if self._remaining is not None:
                if self._remaining <= 0:
                    return Acquire.BUDGET_EXHAUSTED
                if priority >= PRIORITY_LOW and self._limit and self._remaining <= self._reserve * self._limit:
                    return Acquire.BUDGET_EXHAUSTED
            slot = max(now, self._next_slot, self._blocked_until)
            if slot - now > max_wait_s:
                return Acquire.BLOCKED
            self._next_slot = slot + self._min_interval
            self._used += 1
            if self._remaining is not None:
                self._remaining -= 1
        while True:
            wait = slot - self._clock()
            if wait <= 0:
                return Acquire.ALLOWED
            if cancel_check and cancel_check():
                return Acquire.CANCELLED
            self._sleep(min(wait, 0.5))

    def record(self, status_code: int | None, headers: Mapping[str, str] | None) -> None:
        """Update pacing from a response (status None = network error)."""
        with self._lock:
            now = self._clock()
            if status_code is None or status_code >= 500:
                self._failures += 1
                if self._failures >= self._failure_threshold:
                    self._blocked_until = max(self._blocked_until, now + self._circuit_open_s)
                    self._failures = 0
                return
            self._failures = 0
            limit = _header(headers, "x-usagelimit-limit")
            remaining = _header(headers, "x-usagelimit-remaining")
            if limit is not None:
                self._limit = limit
            if remaining is not None:
                self._remaining = remaining
            rate_remaining = _header(headers, "x-ratelimit-remaining")
            rate_reset = _header(headers, "x-ratelimit-reset")
            if rate_remaining == 0 and rate_reset:
                self._next_slot = max(self._next_slot, now + min(rate_reset, _MAX_BLOCK_S))
            if status_code == 429:
                retry = _header(headers, "retry-after") or rate_reset or _DEFAULT_429_S
                self._blocked_until = max(self._blocked_until, now + min(float(retry), _MAX_BLOCK_S))
            day, used, lim, rem = self._day, self._used, self._limit, self._remaining
        if self._on_usage:
            try:
                self._on_usage(self.source_id, day, used, lim, rem)
            except Exception:
                pass

    def usage(self) -> dict:
        """Snapshot for the Settings page."""
        with self._lock:
            self._roll_day()
            return {
                "day": self._day,
                "used": self._used,
                "limit": self._limit,
                "remaining": self._remaining,
                "blocked_until_s": max(0.0, self._blocked_until - self._clock()),
            }


_MIN_INTERVALS = {"theintrodb": 10.0 / 30.0 + 0.01, "introdb": 0.5, "skipdb": 0.5}
_limiters: dict[str, SourceLimiter] = {}
_limiters_lock = threading.Lock()


def _persist_usage(source_id: str, day: str, used: int, limit: int | None, remaining: int | None) -> None:
    from ..store import get_marker_store

    get_marker_store().record_source_usage(source_id, day=day, used=used, limit=limit, remaining=remaining)


def get_limiter(source_id: str) -> SourceLimiter:
    """Process-wide limiter for a source."""
    with _limiters_lock:
        if source_id not in _limiters:
            _limiters[source_id] = SourceLimiter(
                source_id, min_interval_s=_MIN_INTERVALS.get(source_id, 1.0), on_usage=_persist_usage
            )
        return _limiters[source_id]


def reset_limiters() -> None:
    """Forget all limiters (tests)."""
    with _limiters_lock:
        _limiters.clear()
```

Walk the limiter tests against this code before running: `test_usage_headers_reserve_budget...` — after
`record(remaining=100)`, LOW acquire sees 100 ≤ 0.2×500 → BUDGET_EXHAUSTED; priority 1 allowed (remaining→99).
`test_success_resets_failure_count` with threshold 2: 500 → failures 1; 200 → 0; 500 → 1 → not open → ALLOWED.

- [ ] **Step 5: Implement the clients**

```python
# media_preview_generator/markers/sources/online.py
"""Shared bits for online lookups."""

from __future__ import annotations

import threading
from dataclasses import dataclass

import requests

from ..models import Candidate

REQUEST_TIMEOUT_S = 15
_session: requests.Session | None = None
_session_lock = threading.Lock()


@dataclass(frozen=True)
class LookupResult:
    """Outcome of one online lookup.

    ``ok``/``no_data`` are stored as evidence; ``unavailable``/``not_applicable`` are not (tried again next run).
    """

    status: str
    candidates: tuple[Candidate, ...] = ()
    detail: str = ""


def http_session() -> requests.Session:
    """Shared session with an identifying User-Agent."""
    global _session
    with _session_lock:
        if _session is None:
            from ... import __version__

            _session = requests.Session()
            _session.headers["User-Agent"] = f"MediaPreviewGenerator/{__version__} (+https://github.com/stevezau/media_preview_generator)"
        return _session
```
(Check `media_preview_generator/__init__.py` exports `__version__`; if not, use `importlib.metadata.version` with a
`"dev"` fallback.)

```python
# media_preview_generator/markers/sources/theintrodb.py
"""TheIntroDB v3 client (spec §4). Used without written permission — user key optional; must degrade gracefully."""

from __future__ import annotations

import requests
from loguru import logger

from ..models import Candidate, MarkerType, MediaIds, Source
from .online import REQUEST_TIMEOUT_S, LookupResult, http_session
from .ratelimit import Acquire, SourceLimiter, get_limiter

BASE_URL = "https://api.theintrodb.org/v3/media"
_KEYS = (("intro", MarkerType.INTRO), ("recap", MarkerType.RECAP), ("credits", MarkerType.CREDITS), ("preview", MarkerType.PREVIEW))


class TheIntroDbClient:
    """Looks up segments by tmdb/tvdb/imdb id (+ season/episode) and the file's duration."""

    def __init__(self, api_key: str = "", *, limiter: SourceLimiter | None = None, session=None) -> None:
        self._api_key = (api_key or "").strip()
        self._limiter = limiter or get_limiter("theintrodb")
        self._session = session or http_session()

    def lookup(self, ids: MediaIds, *, duration_ms: int | None, priority: int, cancel_check=None) -> LookupResult:
        """Query TheIntroDB for one movie or episode."""
        params: dict[str, object] = {}
        if ids.tmdb:
            params["tmdb_id"] = ids.tmdb
        elif ids.tvdb:
            params["tvdb_id"] = ids.tvdb
        elif ids.imdb:
            params["imdb_id"] = ids.imdb
        else:
            return LookupResult("not_applicable", detail="no tmdb/tvdb/imdb id")
        if ids.is_episode:
            if ids.season is None or ids.episode is None:
                return LookupResult("not_applicable", detail="episode without season/episode numbers")
            params["season"] = ids.season
            params["episode"] = ids.episode
        if duration_ms:
            params["duration_ms"] = int(duration_ms)
        acquired = self._limiter.acquire(priority=priority, cancel_check=cancel_check)
        if acquired is not Acquire.ALLOWED:
            return LookupResult("unavailable", detail=f"TheIntroDB {acquired.value}")
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            resp = self._session.get(BASE_URL, params=params, headers=headers, timeout=REQUEST_TIMEOUT_S)
        except requests.RequestException as exc:
            self._limiter.record(None, None)
            return LookupResult("unavailable", detail=f"TheIntroDB network error: {type(exc).__name__}")
        self._limiter.record(resp.status_code, resp.headers)
        if resp.status_code == 404:
            return LookupResult("no_data")
        if resp.status_code in (401, 403):
            return LookupResult("unavailable", detail="TheIntroDB rejected the API key")
        if resp.status_code != 200:
            return LookupResult("unavailable", detail=f"TheIntroDB HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError:
            return LookupResult("unavailable", detail="TheIntroDB returned invalid JSON")
        candidates = []
        for key, mtype in _KEYS:
            for seg in (body or {}).get(key) or []:
                if not isinstance(seg, dict) or (seg.get("start_ms") is None and seg.get("end_ms") is None):
                    continue
                start = int(seg["start_ms"]) if seg.get("start_ms") is not None else 0
                end = int(seg["end_ms"]) if seg.get("end_ms") is not None else None
                candidates.append(Candidate(mtype, start, end, Source.THEINTRODB))
        logger.debug("TheIntroDB {} → {} segment(s)", {k: v for k, v in params.items()}, len(candidates))
        return LookupResult("ok", tuple(candidates)) if candidates else LookupResult("no_data")
```
Note the candidate order in the test (intro, credits) follows `_KEYS` order intro, recap, credits, preview.

```python
# media_preview_generator/markers/sources/introdb.py
"""IntroDB.app client (TV only, imdb id, anonymous)."""

from __future__ import annotations

import requests

from ..models import Candidate, MarkerType, MediaIds, Source
from .online import REQUEST_TIMEOUT_S, LookupResult, http_session
from .ratelimit import Acquire, SourceLimiter, get_limiter

BASE_URL = "https://api.introdb.app/segments"
_KEYS = (("intro", MarkerType.INTRO), ("recap", MarkerType.RECAP), ("outro", MarkerType.CREDITS))


class IntroDbClient:
    """Looks up one episode by series imdb id + season/episode."""

    def __init__(self, *, limiter: SourceLimiter | None = None, session=None) -> None:
        self._limiter = limiter or get_limiter("introdb")
        self._session = session or http_session()

    def lookup(self, ids: MediaIds, *, duration_ms: int | None, priority: int, cancel_check=None) -> LookupResult:
        """Query IntroDB.app (duration is not supported by the API)."""
        if not ids.is_episode or not ids.imdb or ids.season is None or ids.episode is None:
            return LookupResult("not_applicable", detail="IntroDB needs a TV episode with an imdb id")
        acquired = self._limiter.acquire(priority=priority, cancel_check=cancel_check)
        if acquired is not Acquire.ALLOWED:
            return LookupResult("unavailable", detail=f"IntroDB {acquired.value}")
        params = {"imdb_id": ids.imdb, "season": ids.season, "episode": ids.episode}
        try:
            resp = self._session.get(BASE_URL, params=params, headers={"Accept": "application/json"}, timeout=REQUEST_TIMEOUT_S)
        except requests.RequestException as exc:
            self._limiter.record(None, None)
            return LookupResult("unavailable", detail=f"IntroDB network error: {type(exc).__name__}")
        self._limiter.record(resp.status_code, resp.headers)
        if resp.status_code == 404:
            return LookupResult("no_data")
        if resp.status_code != 200:
            return LookupResult("unavailable", detail=f"IntroDB HTTP {resp.status_code}")
        try:
            body = resp.json() or {}
        except ValueError:
            return LookupResult("unavailable", detail="IntroDB returned invalid JSON")
        candidates = []
        for key, mtype in _KEYS:
            seg = body.get(key)
            if not isinstance(seg, dict):
                continue
            start = seg.get("start_ms", None if seg.get("start_sec") is None else int(float(seg["start_sec"]) * 1000))
            end = seg.get("end_ms", None if seg.get("end_sec") is None else int(float(seg["end_sec"]) * 1000))
            if start is None:
                continue
            candidates.append(Candidate(mtype, int(start), int(end) if end is not None else None, Source.INTRODB,
                                        confidence=float(seg.get("confidence", 1.0))))
        return LookupResult("ok", tuple(candidates)) if candidates else LookupResult("no_data")
```

```python
# media_preview_generator/markers/sources/skipdb.py
"""SkipDB read API (ODbL; read-only API use is exempt from the reciprocity term)."""

from __future__ import annotations

import requests

from ..models import Candidate, MarkerType, MediaIds, Source
from .online import REQUEST_TIMEOUT_S, LookupResult, http_session
from .ratelimit import Acquire, SourceLimiter, get_limiter

BASE_URL = "https://api.skipdb.tv/api/segments"
_KEYS = (("intro", MarkerType.INTRO), ("recap", MarkerType.RECAP), ("outro", MarkerType.CREDITS), ("preview", MarkerType.PREVIEW))
# "agnostic"/"out-of-range" answers are for a different cut of the video; SkipDB R&M "outros" were 7 s tails
# when matched loosely (spec §4), so only duration-confirmed answers count.
_ACCEPTED_MATCHES = frozenset({"exact", "shifted"})


class SkipDbClient:
    """Looks up a movie or episode by imdb id with the file's duration."""

    def __init__(self, *, limiter: SourceLimiter | None = None, session=None) -> None:
        self._limiter = limiter or get_limiter("skipdb")
        self._session = session or http_session()

    def lookup(self, ids: MediaIds, *, duration_ms: int | None, priority: int, cancel_check=None) -> LookupResult:
        """Query SkipDB."""
        if not ids.imdb or not duration_ms:
            return LookupResult("not_applicable", detail="SkipDB needs an imdb id and the file duration")
        params: dict[str, object] = {"imdb_id": ids.imdb}
        if ids.is_episode:
            if ids.season is None or ids.episode is None:
                return LookupResult("not_applicable", detail="episode without season/episode numbers")
            params["season"] = ids.season
            params["episode"] = ids.episode
        params["duration"] = round(duration_ms / 1000.0, 3)
        params["adjust"] = "conservative"
        acquired = self._limiter.acquire(priority=priority, cancel_check=cancel_check)
        if acquired is not Acquire.ALLOWED:
            return LookupResult("unavailable", detail=f"SkipDB {acquired.value}")
        try:
            resp = self._session.get(BASE_URL, params=params, headers={"Accept": "application/json"}, timeout=REQUEST_TIMEOUT_S)
        except requests.RequestException as exc:
            self._limiter.record(None, None)
            return LookupResult("unavailable", detail=f"SkipDB network error: {type(exc).__name__}")
        self._limiter.record(resp.status_code, resp.headers)
        if resp.status_code == 404:
            return LookupResult("no_data")
        if resp.status_code != 200:
            return LookupResult("unavailable", detail=f"SkipDB HTTP {resp.status_code}")
        try:
            segments = (resp.json() or {}).get("segments") or {}
        except ValueError:
            return LookupResult("unavailable", detail="SkipDB returned invalid JSON")
        candidates = []
        for key, mtype in _KEYS:
            seg = segments.get(key)
            if not isinstance(seg, dict) or seg.get("match") not in _ACCEPTED_MATCHES or seg.get("start_ms") is None:
                continue
            if seg["start_ms"] == 0 and seg.get("end_ms") == 0:
                continue  # sentinel: confirmed "no segment of this type"
            end = seg.get("end_ms")
            candidates.append(Candidate(mtype, int(seg["start_ms"]), int(end) if end is not None else None,
                                        Source.SKIPDB, confidence=float(seg.get("confidence", 1.0))))
        return LookupResult("ok", tuple(candidates)) if candidates else LookupResult("no_data")
```

- [ ] **Step 6: Run** unit tests → PASS. Then the live smoke test once on storage:
`pytest --no-cov -m integration tests/markers/test_online_sources_live.py -q` → PASS (record the three responses'
rate/usage headers in `evidence/lab/phase1-results.md`; if the owner has a TheIntroDB key, run once with it and record
`x-usagelimit-limit` to close spec §13 item 9).
- [ ] **Step 7: Commit** — `feat(markers): paced TheIntroDB, IntroDB.app and SkipDB clients`

---
## Task 8: Publisher base, network-filesystem check, Plex database publisher

> **Superseded in part (2026-09-14, Task 8/11 reviews).** Plex publishing is per server item, not per file:
> `WaitingForVersionsError` no longer exists, `write()` returns the markers that are ours on the item, publishers have
> `atomic_writes`, `write()` takes `own_previous`, and `markers.db` has `item_publish_state`. The code blocks below that
> mention the old contract are historical; the binding design is `plex-item-publishing.md` in this folder.

Spec §3.1 (every row matters), §6.3 PlexMarkerPublisher, §13 items 2, 3, 6. The DB write is the riskiest code in the
feature: every SQL statement's parameters are asserted in tests, `tags` is never written, and each failure mode maps
to a capability state the UI can explain.

**Files:**
- Create: `media_preview_generator/markers/fs.py`, `media_preview_generator/markers/publishers/__init__.py`,
  `media_preview_generator/markers/publishers/base.py`, `media_preview_generator/markers/publishers/plex_db.py`
- Modify: `media_preview_generator/servers/plex.py` (`has_plex_pass`, `get_marker_detection_prefs`, `get_markers`)
- Test: `tests/markers/test_fs.py`, `tests/markers/test_plex_db_publisher.py`,
  `tests/fixtures/markers/plex_part_extra_data_native.json` (copy of
  `docs/design/intro-credits/plex_part_extra_data_native.txt` — a real `media_parts.extra_data` from lab Plex 1.43.4,
  item 6 part 4; no paths in it), `tests/fixtures/markers/plex_schema_1_43.sql` (below)

**Interfaces:**
- Consumes: `Marker, MarkerType` (Task 3), `ServerMarkersSettings` (Task 1), `ServerConfig` (+`path_mappings`),
  `servers.ownership.apply_path_mappings(remote_path, mappings) -> list[str]`.
- Produces:
```python
# markers/fs.py
NETWORK_FS_TYPES: frozenset[str]
def filesystem_type(path: str, *, mountinfo_path: str = "/proc/self/mountinfo") -> str | None
def is_network_filesystem(fs_type: str | None) -> bool
# markers/publishers/base.py
class Capability(str, Enum): READY; DISABLED; NEEDS_CONFIRMATION; NEEDS_PLUGIN; PLUGIN_OUTDATED; NEEDS_PASS; NEEDS_LOCAL_DB
                             NEEDS_PLEX_DETECTION_ONCE; UNSUPPORTED_SCHEMA; UNREACHABLE; MISCONFIGURED
@dataclass(frozen=True) class CapabilityReport: state: Capability; message: str; details: dict = {}
    @property ready -> bool
class PublishError(Exception): def __init__(self, message: str, *, state: Capability | None = None)
class ItemNotFoundError(PublishError)
class WaitingForVersionsError(PublishError)
class MarkerPublisher(ABC):
    supported_types: frozenset[MarkerType]
    name: str                                            # "plex_db" | "jellyfin_bridge" | "emby_bridge"
    def capability(self) -> CapabilityReport
    def read(self, item_id: str) -> list[Marker]         # as clients see them, supported types only
    def write(self, item_id: str, markers: list[Marker], *, previous: list[Marker], duration_ms: int, canonical_path: str) -> None
    def project(self, markers: Iterable[Marker]) -> list[Marker]   # supported types, sorted by start
# markers/publishers/plex_db.py
def plex_db_path(plex_config_folder: str) -> str
def encode_extra_data(d: dict[str, str]) -> str          # sorted keys + Plex url field
def merge_part_extra_data(existing: str | None, wanted: list[Marker], managed: set[MarkerType], duration_ms: int) -> str
class PlexMarkerPublisher(MarkerPublisher):
    def __init__(self, server, config: ServerConfig, settings: ServerMarkersSettings, *,
                 sibling_markers: Callable[[str], dict[MarkerType, Marker] | None] | None = None,
                 mountinfo_path: str = "/proc/self/mountinfo") -> None
# servers/plex.py
PlexServer.has_plex_pass(self) -> bool | None               # root myPlexSubscription; None if unreachable
PlexServer.get_marker_detection_prefs(self) -> dict[str, str | None]   # {"intro": ..., "credits": ...}
PlexServer.get_markers(self, item_id: str) -> list[dict] | None        # served: [{"type","start_ms","end_ms","final"}]
```

- [ ] **Step 1: Write the schema fixture** `tests/fixtures/markers/plex_schema_1_43.sql` — the exact `CREATE TABLE`
statements from the lab Plex 1.43.4 DB for `tags`, `taggings`, `media_parts`, `media_items`, `metadata_items`
(dump them with
`docker exec mlab-plex "/usr/lib/plexmediaserver/Plex SQLite" "file:<db>?mode=ro" ".schema tags" ".schema taggings" ".schema media_parts" ".schema media_items" ".schema metadata_items"`
and delete the `fts4_*` trigger statements — they need Plex's ICU tokenizer, and our code never touches `tags`).

- [ ] **Step 2: Write failing fs tests**

```python
# tests/markers/test_fs.py
import pytest

from media_preview_generator.markers.fs import filesystem_type, is_network_filesystem

MOUNTINFO = """\
22 1 259:2 / / rw,relatime shared:1 - ext4 /dev/nvme0n1p2 rw
100 22 0:50 / /config rw,relatime - zfs pool/config rw
101 22 0:51 / /config/plex rw,relatime - nfs4 plex:/config rw
102 22 0:52 / /mnt/My\\040Share rw - cifs //nas/share rw
103 22 0:53 / /mnt/user rw - fuse.shfs shfs rw
104 22 0:54 / /host_mnt rw - fakeowner /dev rw
"""


@pytest.fixture
def mountinfo(tmp_path):
    p = tmp_path / "mountinfo"
    p.write_text(MOUNTINFO)
    return str(p)


@pytest.mark.parametrize(
    ("path", "fs"),
    [
        ("/config/Library/db", "zfs"),
        ("/config/plex/Library/Application Support", "nfs4"),
        ("/config/plexish", "zfs"),  # prefix must be folder-bounded
        ("/mnt/My Share/Plex", "cifs"),
        ("/mnt/user/appdata/plex", "fuse.shfs"),
        ("/host_mnt/Users/me", "fakeowner"),
        ("/srv", "ext4"),
    ],
)
def test_longest_mount_point_wins(mountinfo, path, fs):
    assert filesystem_type(path, mountinfo_path=mountinfo) == fs


def test_missing_mountinfo_returns_none(tmp_path):
    assert filesystem_type("/x", mountinfo_path=str(tmp_path / "nope")) is None


@pytest.mark.parametrize(
    ("fs", "network"),
    [("nfs", True), ("nfs4", True), ("cifs", True), ("smb3", True), ("9p", True), ("fuse.sshfs", True),
     ("virtiofs", True), ("fakeowner", True), ("fuse.grpcfuse", True), ("fuse.rclone", True),
     ("ext4", False), ("zfs", False), ("xfs", False), ("btrfs", False), ("fuse.shfs", False), ("overlay", False),
     (None, False)],
)
def test_network_matrix(fs, network):
    assert is_network_filesystem(fs) is network
```

- [ ] **Step 3: Write failing Plex publisher tests**

```python
# tests/markers/test_plex_db_publisher.py
"""PlexMarkerPublisher against a real SQLite file with Plex 1.43's schema (spec §3.1)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from media_preview_generator.markers.models import Marker, MarkerType
from media_preview_generator.markers.publishers.base import Capability, PublishError, WaitingForVersionsError
from media_preview_generator.markers.publishers.plex_db import (
    PlexMarkerPublisher,
    encode_extra_data,
    merge_part_extra_data,
    plex_db_path,
)
from media_preview_generator.markers.settings import ServerMarkersSettings
from media_preview_generator.servers.base import ServerConfig, ServerType

T = MarkerType
FIX = Path(__file__).resolve().parents[1] / "fixtures" / "markers"
DUR = 1_320_000
INTRO = Marker(T.INTRO, 11_000, 37_000, ("chapters",))
CREDITS_FINAL = Marker(T.CREDITS, 1_299_000, DUR, ("chapters",))
CREDITS_NONFINAL = Marker(T.CREDITS, 1_200_000, 1_250_000, ("theintrodb", "skipdb"))
NATIVE_INTROS = ('{"MediaPartMarkersArray":{"attributeName":"intros","version":5,'
                 '"MediaPartMarker":[{"startTimeOffset":990,"endTimeOffset":29306}]}}')
NATIVE_CREDITS = ('{"MediaPartMarkersArray":{"attributeName":"credits","version":4,'
                  '"MediaPartMarker":[{"startTimeOffset":1154521,"endTimeOffset":1188521}]}}')


def _plex_json(d: dict) -> str:
    # Plex stores compact JSON; the capability sample's LIKE '%"pv:intros":"{%' only matches that form.
    return json.dumps(d, separators=(",", ":"))


BOTH_NATIVE = _plex_json({"pv:credits": NATIVE_CREDITS, "pv:intros": NATIVE_INTROS, "url": "z"})
INTROS_V6 = _plex_json({"pv:intros": NATIVE_INTROS.replace('"version":5', '"version":6')})


def _make_db(folder: Path, *, tag_row: str | None = "''", parts=(("/data/tv/S01E01.mkv", None),), metadata_item_id=7):
    db = Path(plex_db_path(str(folder)))
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.executescript((FIX / "plex_schema_1_43.sql").read_text())
    conn.execute("INSERT INTO metadata_items (id, metadata_type, title) VALUES (?, 4, 'Ep')", (metadata_item_id,))
    if tag_row is not None:
        conn.execute("INSERT INTO tags (id, tag, tag_type) VALUES (562, NULL, 12)")
        if tag_row == "''":
            conn.execute("INSERT INTO tags (id, tag, tag_type) VALUES (563, '', 12)")
    for n, (file, extra) in enumerate(parts, start=1):
        conn.execute("INSERT INTO media_items (id, metadata_item_id) VALUES (?, ?)", (n, metadata_item_id))
        conn.execute("INSERT INTO media_parts (id, media_item_id, file, extra_data) VALUES (?, ?, ?, ?)", (n, n, file, extra))
    conn.commit()
    conn.close()
    return db


def _mountinfo(tmp_path, fs="ext4"):
    p = tmp_path / "mountinfo"
    p.write_text(f"22 1 259:2 / / rw - {fs} /dev/root rw\n")
    return str(p)


def _publisher(tmp_path, folder, *, enabled=True, confirmed="2026-09-13T00:00:00+00:00", fs="ext4", plex_pass=True,
               sibling_markers=None, mappings=None):
    server = MagicMock()
    server.has_plex_pass.return_value = plex_pass
    server.get_marker_detection_prefs.return_value = {"intro": "never", "credits": "never"}
    cfg = ServerConfig(id="plex-1", type=ServerType.PLEX, name="Plex", enabled=True, url="http://p", auth={},
                       output={"plex_config_folder": str(folder)}, path_mappings=mappings or [])
    settings = ServerMarkersSettings(enabled, None, confirmed, "restore")
    return PlexMarkerPublisher(server, cfg, settings, sibling_markers=sibling_markers, mountinfo_path=_mountinfo(tmp_path, fs))


def _rows(db, sql, *params):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


class TestExtraDataEncoding:
    def test_rebuilding_native_extra_data_is_byte_identical(self):
        native = (FIX / "plex_part_extra_data_native.json").read_text().strip()
        d = json.loads(native)
        d.pop("url")
        assert encode_extra_data(d) == native

    def test_merge_writes_both_keys_and_rebuilds_url(self):
        native = (FIX / "plex_part_extra_data_native.json").read_text().strip()
        out = json.loads(merge_part_extra_data(native, [INTRO, CREDITS_FINAL], {T.INTRO, T.CREDITS}, DUR))
        assert out["pv:intros"] == ('{"MediaPartMarkersArray":{"attributeName":"intros","version":5,'
                                    '"MediaPartMarker":[{"startTimeOffset":11000,"endTimeOffset":37000}]}}')
        assert out["pv:credits"] == ('{"MediaPartMarkersArray":{"attributeName":"credits","version":4,'
                                     '"MediaPartMarker":[{"startTimeOffset":1297000,"endTimeOffset":1320000,"final":true}]}}')
        assert out["ma:container"] == "mkv" and out["pv:deepAnalysisDate"] == "1651985592"
        assert list(out) == sorted(k for k in out if k != "url") + ["url"]
        rebuilt = dict(out)
        rebuilt.pop("url")
        assert json.loads(encode_extra_data(rebuilt))["url"] == out["url"]
        assert "pv%3Aintros=%7B%22MediaPartMarkersArray" in out["url"]

    @pytest.mark.parametrize(
        ("existing", "wanted", "managed", "intros", "credits"),
        [
            (None, [INTRO], {T.INTRO}, "set", "absent"),
            ("", [CREDITS_FINAL], {T.CREDITS}, "absent", "set"),
            (BOTH_NATIVE, [INTRO], {T.INTRO}, "set", "keep"),
            (_plex_json({"pv:intros": NATIVE_INTROS, "url": "z"}), [], {T.INTRO}, "empty", "absent"),  # we removed our intro
            (_plex_json({"pv:intros": "", "url": "z"}), [INTRO], {T.INTRO}, "set", "absent"),  # cleared earlier
            (BOTH_NATIVE, [INTRO, CREDITS_NONFINAL], {T.INTRO, T.CREDITS}, "set", "set"),
        ],
    )
    def test_merge_matrix(self, existing, wanted, managed, intros, credits):
        out = json.loads(merge_part_extra_data(existing, wanted, managed, DUR))
        expect = {"set": lambda v: v.startswith('{"MediaPartMarkersArray"') and v not in (NATIVE_INTROS, NATIVE_CREDITS),
                  "absent": lambda v: v is None, "keep": lambda v: v == NATIVE_CREDITS, "empty": lambda v: v == ""}
        assert expect[intros](out.get("pv:intros"))
        assert expect[credits](out.get("pv:credits"))
        assert "url" in out

    def test_non_final_credits_shifts_end_and_omits_final(self):
        out = json.loads(merge_part_extra_data(None, [CREDITS_NONFINAL], {T.CREDITS}, DUR))
        marker = json.loads(out["pv:credits"])["MediaPartMarkersArray"]["MediaPartMarker"]
        assert marker == [{"startTimeOffset": 1_198_000, "endTimeOffset": 1_252_000}]

    @pytest.mark.parametrize(
        "existing",
        [
            "{not json",
            "[1, 2]",
            INTROS_V6,                                                             # the type we write
            _plex_json({"pv:credits": NATIVE_CREDITS.replace('"version":4', '"version":5')}),  # a type we only keep
            _plex_json({"pv:credits": NATIVE_CREDITS.replace('"credits","version"', '"outros","version"')}),
            _plex_json({"pv:intros": "not json"}),
        ],
        ids=["not-json", "not-object", "intro-v6", "credits-v5-kept", "wrong-attribute", "marker-value-not-json"],
    )
    def test_unknown_existing_data_raises_unsupported_schema(self, existing):
        with pytest.raises(PublishError) as ei:
            merge_part_extra_data(existing, [INTRO], {T.INTRO}, DUR)
        assert ei.value.state is Capability.UNSUPPORTED_SCHEMA


class TestWrite:
    def test_writes_taggings_and_parts_and_never_touches_tags(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        tags_before = _rows(db, "SELECT * FROM tags ORDER BY id")
        pub = _publisher(tmp_path, folder)
        pub.write("7", [CREDITS_FINAL, INTRO], previous=[], duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv")
        rows = _rows(db, "SELECT metadata_item_id, tag_id, [index], text, time_offset, end_time_offset, thumb_url, "
                         "typeof(created_at), extra_data FROM taggings ORDER BY [index]")
        assert rows == [
            (7, 563, 0, "intro", 11_000, 37_000, "", "integer", '{"pv:version":"5","url":"pv%3Aversion=5"}'),
            (7, 563, 1, "credits", 1_297_000, DUR, "", "integer",
             '{"pv:final":"1","pv:version":"4","url":"pv%3Afinal=1&pv%3Aversion=4"}'),
        ]
        assert _rows(db, "SELECT * FROM tags ORDER BY id") == tags_before
        extra = json.loads(_rows(db, "SELECT extra_data FROM media_parts WHERE id=1")[0][0])
        assert json.loads(extra["pv:intros"])["MediaPartMarkersArray"]["MediaPartMarker"] == [
            {"startTimeOffset": 11_000, "endTimeOffset": 37_000}]

    def test_replaces_native_rows_of_managed_types_only(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO taggings (metadata_item_id, tag_id, [index], text, time_offset, end_time_offset, "
                     "thumb_url, created_at, extra_data) VALUES (7, 563, 0, 'intro', 76508, 112748, '', 1, 'n'), "
                     "(7, 563, 1, 'credits', 1264953, 1296953, '', 1, 'n'), (7, 999, 0, 'bookmark', 5, 6, '', 1, 'b'), "
                     "(8, 563, 0, 'intro', 1, 2, '', 1, 'other item')")
        conn.commit()
        conn.close()
        _publisher(tmp_path, folder).write("7", [INTRO], previous=[], duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv")
        rows = _rows(db, "SELECT metadata_item_id, tag_id, text, time_offset, extra_data FROM taggings ORDER BY metadata_item_id, tag_id, text")
        assert rows == [
            (7, 563, "credits", 1264953, "n"),      # Plex's own credits stay: we have no credits decision
            (7, 563, "intro", 11_000, '{"pv:version":"5","url":"pv%3Aversion=5"}'),
            (7, 999, "bookmark", 5, "b"),
            (8, 563, "intro", 1, "other item"),
        ]

    def test_previously_published_type_now_undecided_is_removed(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        pub = _publisher(tmp_path, folder)
        pub.write("7", [INTRO, CREDITS_FINAL], previous=[], duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv")
        pub.write("7", [INTRO], previous=[INTRO, CREDITS_FINAL], duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv")
        assert [r[0] for r in _rows(db, "SELECT text FROM taggings")] == ["intro"]
        extra = json.loads(_rows(db, "SELECT extra_data FROM media_parts WHERE id=1")[0][0])
        assert extra["pv:credits"] == ""

    @pytest.mark.parametrize(("tag_row", "state"), [(None, Capability.NEEDS_PLEX_DETECTION_ONCE),
                                                    ("null-only", Capability.NEEDS_PLEX_DETECTION_ONCE)])
    def test_missing_marker_tag_row_never_creates_one(self, tmp_path, tag_row, state):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, tag_row=tag_row)
        before = _rows(db, "SELECT COUNT(*) FROM tags")[0][0]
        with pytest.raises(PublishError) as ei:
            _publisher(tmp_path, folder).write("7", [INTRO], previous=[], duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv")
        assert ei.value.state is state
        assert _rows(db, "SELECT COUNT(*) FROM tags")[0][0] == before
        assert _rows(db, "SELECT COUNT(*) FROM taggings")[0][0] == 0

    def test_unknown_item_raises_not_found(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        _make_db(folder)
        with pytest.raises(PublishError, match="not found"):
            _publisher(tmp_path, folder).write("999", [INTRO], previous=[], duration_ms=DUR, canonical_path="/x.mkv")

    def test_missing_column_is_unsupported_schema_and_writes_nothing(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        conn = sqlite3.connect(db)
        conn.execute("ALTER TABLE taggings RENAME COLUMN end_time_offset TO end_offset_ms")
        conn.commit()
        conn.close()
        with pytest.raises(PublishError) as ei:
            _publisher(tmp_path, folder).write("7", [INTRO], previous=[], duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv")
        assert ei.value.state is Capability.UNSUPPORTED_SCHEMA

    def test_unknown_native_marker_version_on_the_item_is_unsupported_schema(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, parts=(("/data/tv/S01E01.mkv", INTROS_V6),))
        with pytest.raises(PublishError) as ei:
            _publisher(tmp_path, folder).write("7", [INTRO], previous=[], duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv")
        assert ei.value.state is Capability.UNSUPPORTED_SCHEMA
        assert _rows(db, "SELECT extra_data FROM media_parts WHERE id=1") == [(INTROS_V6,)]

    def test_failure_mid_transaction_rolls_back(self, tmp_path, monkeypatch):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO taggings (metadata_item_id, tag_id, [index], text, time_offset, end_time_offset, "
                     "thumb_url, created_at, extra_data) VALUES (7, 563, 0, 'intro', 76508, 112748, '', 1, 'n')")
        conn.commit()
        conn.close()
        from media_preview_generator.markers.publishers import plex_db

        # Raises after the DELETE of Plex's own intro row: the DELETE must be rolled back too.
        monkeypatch.setattr(plex_db, "_tagging_extra", MagicMock(side_effect=RuntimeError("boom")))
        with pytest.raises(RuntimeError):
            _publisher(tmp_path, folder).write("7", [INTRO], previous=[], duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv")
        assert _rows(db, "SELECT text, time_offset FROM taggings") == [("intro", 76508)]
        assert _rows(db, "SELECT extra_data FROM media_parts WHERE id=1") == [(None,)]

    def test_merges_extra_data_plex_wrote_while_we_waited_for_the_lock(self, tmp_path):
        # Plex analyses a new file right when the webhook follow-up runs; its fresh ma:* keys must survive our write.
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, parts=(("/data/tv/S01E01.mkv", '{"ma:container":"mkv","url":"ma%3Acontainer=mkv"}'),))
        locker = sqlite3.connect(db, check_same_thread=False, isolation_level=None)
        locker.execute("BEGIN IMMEDIATE")
        locker.execute("UPDATE media_parts SET extra_data=? WHERE id=1",
                       ('{"ma:container":"mkv","ma:x":"1","url":"ma%3Acontainer=mkv&ma%3Ax=1"}',))
        threading.Timer(0.5, lambda: (locker.execute("COMMIT"), locker.close())).start()
        start = time.monotonic()
        _publisher(tmp_path, folder).write("7", [INTRO], previous=[], duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv")
        assert time.monotonic() - start >= 0.4
        assert _rows(db, "SELECT COUNT(*) FROM taggings")[0][0] == 1
        extra = json.loads(_rows(db, "SELECT extra_data FROM media_parts WHERE id=1")[0][0])
        assert extra["ma:x"] == "1" and extra["ma:container"] == "mkv"
        assert json.loads(extra["pv:intros"])["MediaPartMarkersArray"]["MediaPartMarker"] == [
            {"startTimeOffset": 11_000, "endTimeOffset": 37_000}]

    def test_parts_changed_while_waiting_for_the_lock_writes_nothing(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        locker = sqlite3.connect(db, check_same_thread=False, isolation_level=None)
        locker.execute("BEGIN IMMEDIATE")
        locker.execute("UPDATE media_parts SET file='/data/tv/S01E01.PROPER.mkv' WHERE id=1")
        threading.Timer(0.3, lambda: (locker.execute("COMMIT"), locker.close())).start()
        with pytest.raises(WaitingForVersionsError, match="changed"):
            _publisher(tmp_path, folder).write("7", [INTRO], previous=[], duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv")
        assert _rows(db, "SELECT COUNT(*) FROM taggings")[0][0] == 0
        assert _rows(db, "SELECT extra_data FROM media_parts WHERE id=1") == [(None,)]


class TestMultiVersion:
    PARTS = (("/data/tv/S01E01 - 1080p.mkv", None), ("/data/tv/S01E01 - 2160p.mkv", None))

    def _write(self, tmp_path, sibling):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, parts=self.PARTS)
        pub = _publisher(tmp_path, folder, sibling_markers=lambda path: sibling if path.endswith("2160p.mkv") else None)
        return db, pub

    def test_all_versions_agree_within_2s_writes_every_part(self, tmp_path):
        sibling = {T.INTRO: Marker(T.INTRO, 12_500, 38_900, ("chapters",))}
        db, pub = self._write(tmp_path, sibling)
        pub.write("7", [INTRO], previous=[], duration_ms=DUR, canonical_path="/data/tv/S01E01 - 1080p.mkv")
        assert all("pv:intros" in json.loads(r[0]) for r in _rows(db, "SELECT extra_data FROM media_parts"))

    @pytest.mark.parametrize("sibling", [None, {}, {T.INTRO: Marker(T.INTRO, 14_000, 40_000, ("chapters",))}])
    def test_waiting_or_disagreeing_versions_write_nothing(self, tmp_path, sibling):
        db, pub = self._write(tmp_path, sibling)
        with pytest.raises(WaitingForVersionsError):
            pub.write("7", [INTRO], previous=[], duration_ms=DUR, canonical_path="/data/tv/S01E01 - 1080p.mkv")
        assert _rows(db, "SELECT COUNT(*) FROM taggings")[0][0] == 0

    def test_stacked_parts_in_one_version_are_refused(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO media_parts (id, media_item_id, file) VALUES (2, 1, '/data/tv/S01E01-cd2.mkv')")
        conn.commit()
        conn.close()
        with pytest.raises(PublishError, match="stacked"):
            _publisher(tmp_path, folder).write("7", [INTRO], previous=[], duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv")

    def test_part_paths_are_path_mapped_before_comparing(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, parts=(("/plexmedia/tv/A.mkv", None), ("/plexmedia/tv/B.mkv", None)))
        seen = []
        sibling = {T.INTRO: INTRO}
        pub = _publisher(tmp_path, folder, mappings=[{"plex_prefix": "/plexmedia", "local_prefix": "/data"}],
                         sibling_markers=lambda p: (seen.append(p), sibling)[1])
        pub.write("7", [INTRO], previous=[], duration_ms=DUR, canonical_path="/data/tv/A.mkv")
        assert seen == ["/data/tv/B.mkv"]


class TestRead:
    def test_read_returns_served_times(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        _make_db(folder)
        pub = _publisher(tmp_path, folder)
        pub.write("7", [INTRO, CREDITS_NONFINAL], previous=[], duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv")
        got = pub.read("7")
        assert [(m.type, m.start_ms, m.end_ms) for m in got] == [
            (T.INTRO, 11_000, 37_000), (T.CREDITS, 1_200_000, 1_250_000)]


class TestCapability:
    @pytest.mark.parametrize(
        ("kwargs", "db", "state"),
        [
            ({"enabled": False}, True, Capability.DISABLED),
            ({"confirmed": None}, True, Capability.NEEDS_CONFIRMATION),
            ({}, False, Capability.MISCONFIGURED),
            ({"fs": "nfs4"}, True, Capability.NEEDS_LOCAL_DB),
            ({"plex_pass": False}, True, Capability.NEEDS_PASS),
            ({}, "no-tag", Capability.NEEDS_PLEX_DETECTION_ONCE),
            ({}, True, Capability.READY),
            ({"plex_pass": None}, True, Capability.READY),  # Plex unreachable right now: DB checks still pass
        ],
    )
    def test_matrix(self, tmp_path, kwargs, db, state):
        folder = tmp_path / "Plex Media Server"
        if db:
            _make_db(folder, tag_row=None if db == "no-tag" else "''")
        report = _publisher(tmp_path, folder, **kwargs).capability()
        assert report.state is state, report.message
        if state is Capability.READY:
            assert report.details["fs_type"] == "ext4" and report.details["db_path"].endswith("com.plexapp.plugins.library.db")
            assert report.details["detection"] == {"intro": "never", "credits": "never"}

    def test_empty_config_folder_is_misconfigured(self, tmp_path):
        pub = _publisher(tmp_path, "")
        assert pub.capability().state is Capability.MISCONFIGURED

    def test_unknown_marker_version_anywhere_in_the_library_blocks_writes(self, tmp_path):
        # The library-wide sample runs here (cached per job), not on every write.
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO metadata_items (id, metadata_type, title) VALUES (8, 4, 'Other')")
        conn.execute("INSERT INTO media_items (id, metadata_item_id) VALUES (50, 8)")
        conn.execute("INSERT INTO media_parts (id, media_item_id, file, extra_data) VALUES (50, 50, '/data/tv/o.mkv', ?)",
                     (INTROS_V6,))
        conn.commit()
        conn.close()
        report = _publisher(tmp_path, folder).capability()
        assert report.state is Capability.UNSUPPORTED_SCHEMA and "version 6" in report.message
```

Plex server helper tests (append to `tests/test_servers_plex.py`):
```python
class TestPlexMarkerHelpers:
    @pytest.mark.parametrize(("attr", "expected"), [(True, True), (False, False)])
    def test_has_plex_pass(self, plex_server_under_test, attr, expected):
        plex_server_under_test._connect.return_value.myPlexSubscription = attr
        assert plex_server_under_test.has_plex_pass() is expected

    def test_has_plex_pass_unreachable(self, plex_server_under_test):
        plex_server_under_test._connect.side_effect = RuntimeError("down")
        assert plex_server_under_test.has_plex_pass() is None

    def test_get_markers_parses_served_markers(self, plex_server_under_test):
        import xml.etree.ElementTree as ET
        xml = ET.fromstring('<MediaContainer><Video ratingKey="7"><Marker type="intro" startTimeOffset="990" '
                            'endTimeOffset="29306"/><Marker type="credits" startTimeOffset="1156521" endTimeOffset="1186521"/>'
                            '<Marker type="credits" startTimeOffset="1294044" endTimeOffset="1322272" final="1"/>'
                            '<Marker type="bookmark" startTimeOffset="5" endTimeOffset="6"/></Video></MediaContainer>')
        plex_server_under_test._connect.return_value.query.return_value = xml
        assert plex_server_under_test.get_markers("7") == [
            {"type": "intro", "start_ms": 990, "end_ms": 29306, "final": False},
            {"type": "credits", "start_ms": 1156521, "end_ms": 1186521, "final": False},
            {"type": "credits", "start_ms": 1294044, "end_ms": 1322272, "final": True},
        ]
        assert plex_server_under_test._connect.return_value.query.call_args.args[0] == "/library/metadata/7?includeMarkers=1"
```

- [ ] **Step 4: Run** → FAIL.

- [ ] **Step 5: Implement `markers/fs.py`**

```python
# media_preview_generator/markers/fs.py
"""Filesystem type of a path, for the "Plex DB must be on this host" rule (SQLite WAL needs a local filesystem)."""

from __future__ import annotations

import os

# Docker Desktop shares (fakeowner, grpcfuse, virtiofs) and WSL 9p are network-backed even when they look local.
NETWORK_FS_TYPES: frozenset[str] = frozenset({
    "nfs", "nfs4", "cifs", "smb3", "smbfs", "9p", "fuse.sshfs", "sshfs", "fuse.rclone", "virtiofs", "fakeowner",
    "fuse.grpcfuse", "ceph", "fuse.ceph", "glusterfs", "fuse.glusterfs", "afs", "davfs", "fuse.davfs", "fuse.s3fs",
    "lustre", "fuse.juicefs",
})


def _unescape(field: str) -> str:
    return field.replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n").replace("\\134", "\\")


def filesystem_type(path: str, *, mountinfo_path: str = "/proc/self/mountinfo") -> str | None:
    """Return the filesystem type of the mount holding ``path`` (longest mount-point prefix), or None."""
    try:
        with open(mountinfo_path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return None
    target = os.path.normpath(path)
    best_len, best_fs = -1, None
    for line in lines:
        left, sep, right = line.partition(" - ")
        if not sep:
            continue
        fields = left.split()
        if len(fields) < 5:
            continue
        mount_point = os.path.normpath(_unescape(fields[4]))
        fs_type = right.split()[0] if right.split() else None
        bounded = target == mount_point or target.startswith(mount_point.rstrip("/") + "/") or mount_point == "/"
        if bounded and len(mount_point) > best_len:
            best_len, best_fs = len(mount_point), fs_type
    return best_fs


def is_network_filesystem(fs_type: str | None) -> bool:
    """Whether SQLite locking over this filesystem type is unsafe."""
    return bool(fs_type) and fs_type in NETWORK_FS_TYPES
```

- [ ] **Step 6: Implement `publishers/base.py`**

```python
# media_preview_generator/markers/publishers/__init__.py
"""Marker publishers: project decided markers onto each server."""
```

```python
# media_preview_generator/markers/publishers/base.py
"""Publisher contract (spec §6.3)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum

from ..models import Marker, MarkerType


class Capability(str, Enum):
    """Whether a server can receive markers right now, and if not, why."""

    READY = "ready"
    DISABLED = "disabled"
    NEEDS_CONFIRMATION = "needs_confirmation"
    NEEDS_PLUGIN = "needs_plugin"
    PLUGIN_OUTDATED = "plugin_outdated"
    NEEDS_PASS = "needs_pass"
    NEEDS_LOCAL_DB = "needs_local_db"
    NEEDS_PLEX_DETECTION_ONCE = "needs_plex_detection_once"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    UNREACHABLE = "unreachable"
    MISCONFIGURED = "misconfigured"


@dataclass(frozen=True)
class CapabilityReport:
    """Capability plus a user-facing message and details for the Edit dialog status block."""

    state: Capability
    message: str
    details: dict = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        """Whether writes may be attempted."""
        return self.state is Capability.READY


class PublishError(Exception):
    """A write did not happen; ``state`` says which capability problem caused it (if any)."""

    def __init__(self, message: str, *, state: Capability | None = None) -> None:
        super().__init__(message)
        self.state = state


class ItemNotFoundError(PublishError):
    """The server doesn't know the item (yet)."""


class WaitingForVersionsError(PublishError):
    """Plex shares one marker set per item; other versions are undecided or disagree."""


class MarkerPublisher(ABC):
    """Writes and reads markers on one server."""

    supported_types: frozenset[MarkerType] = frozenset()
    name: str = ""

    @abstractmethod
    def capability(self) -> CapabilityReport:
        """Check whether this server can receive markers."""

    @abstractmethod
    def read(self, item_id: str) -> list[Marker]:
        """Markers currently on the server for an item, as clients see them (supported types only)."""

    @abstractmethod
    def write(
        self, item_id: str, markers: list[Marker], *, previous: list[Marker], duration_ms: int, canonical_path: str
    ) -> None:
        """Make the server show exactly ``markers`` for the types we manage.

        Args:
            item_id: Server item id.
            markers: Decided markers already projected to this server's supported types.
            previous: Markers we last published here (types in it but not in ``markers`` get removed).
            duration_ms: File duration (for "runs to the end" semantics).
            canonical_path: Local path of the file (multi-version checks).

        Raises:
            PublishError: Nothing was written.
        """

    def project(self, markers: Iterable[Marker]) -> list[Marker]:
        """Keep supported types, ordered by start."""
        return sorted((m for m in markers if m.type in self.supported_types), key=lambda m: (m.start_ms, m.type.value))
```

- [ ] **Step 7: Implement `publishers/plex_db.py`**

```python
# media_preview_generator/markers/publishers/plex_db.py
"""Plex publisher: direct writes into Plex's library database (spec §3.1, §6.3).

Plex has no API for intro/credits markers. It serves markers from ``taggings`` rows on its single
``tags(tag_type=12, tag='')`` row, and rebuilds those rows from ``media_parts.extra_data`` when it re-detects, so
both places are written in one short transaction. Proven on PMS 1.43.4 in the lab; anything unexpected stops writes.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.parse
from collections.abc import Callable

from loguru import logger

from ...servers.ownership import apply_path_mappings
from ..fs import filesystem_type, is_network_filesystem
from ..models import Marker, MarkerType
from .base import (
    Capability,
    CapabilityReport,
    ItemNotFoundError,
    MarkerPublisher,
    PublishError,
    WaitingForVersionsError,
)

MARKER_TAG_TYPE = 12
# Plex serves credits starting 2 s later than stored and non-final credits ending 2 s earlier (spec §3.1).
CREDITS_SERVE_SHIFT_MS = 2_000
FINAL_TOLERANCE_MS = 2_000
VERSION_AGREEMENT_MS = 2_000
INTRO_JSON_VERSION = 5
CREDITS_JSON_VERSION = 4
_TYPE_TEXT = {MarkerType.INTRO: "intro", MarkerType.CREDITS: "credits"}
_PART_KEY = {MarkerType.INTRO: "pv:intros", MarkerType.CREDITS: "pv:credits"}
_REQUIRED_COLUMNS = {
    "tags": {"id", "tag", "tag_type"},
    "taggings": {"id", "metadata_item_id", "tag_id", "index", "text", "time_offset", "end_time_offset",
                 "thumb_url", "created_at", "extra_data"},
    "media_parts": {"id", "media_item_id", "file", "extra_data", "deleted_at"},
    "media_items": {"id", "metadata_item_id"},
    "metadata_items": {"id"},
}


def plex_db_path(plex_config_folder: str) -> str:
    """Library DB path under the "Plex Media Server" folder the app already uses for previews."""
    return os.path.join(plex_config_folder, "Plug-in Support", "Databases", "com.plexapp.plugins.library.db")


def _plex_quote(value: str) -> str:
    # Matches Plex's own encoder: everything but alphanumerics and -_~ is %-escaped, including "." (verified
    # byte-for-byte against 28 native extra_data rows in the lab).
    return urllib.parse.quote(value, safe="").replace(".", "%2E")


def encode_extra_data(d: dict[str, str]) -> str:
    """Serialise an extra_data dict the way Plex does: sorted keys, compact JSON, trailing ``url`` field."""
    ordered = {k: d[k] for k in sorted(d) if k != "url"}
    ordered["url"] = "&".join(f"{_plex_quote(k)}={_plex_quote(str(v))}" for k, v in ordered.items())
    return json.dumps(ordered, separators=(",", ":"), ensure_ascii=False)


def _is_final(marker: Marker, duration_ms: int) -> bool:
    return marker.end_ms >= duration_ms - FINAL_TOLERANCE_MS


def _stored_times(marker: Marker, duration_ms: int) -> tuple[int, int, bool]:
    if marker.type is not MarkerType.CREDITS:
        return marker.start_ms, marker.end_ms, False
    final = _is_final(marker, duration_ms)
    start = max(0, marker.start_ms - CREDITS_SERVE_SHIFT_MS)
    end = marker.end_ms if final else marker.end_ms + CREDITS_SERVE_SHIFT_MS
    return start, end, final


def _check_marker_array(key: str, value: str) -> None:
    """Raise unless a stored ``pv:intros``/``pv:credits`` value is the tested shape and version (empty = cleared)."""
    if value == "":
        return
    attribute, version = ("intros", INTRO_JSON_VERSION) if key == "pv:intros" else ("credits", CREDITS_JSON_VERSION)
    try:
        arr = json.loads(value)["MediaPartMarkersArray"]
        found_attribute, found_version = arr.get("attributeName"), arr.get("version")
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise PublishError(f"Plex {key} data has an unknown shape; not writing markers.",
                           state=Capability.UNSUPPORTED_SCHEMA) from exc
    if found_attribute != attribute or found_version != version:
        raise PublishError(
            f"Plex stores {attribute} markers as {found_attribute} version {found_version} (tested: {version}); "
            "not writing markers.",
            state=Capability.UNSUPPORTED_SCHEMA,
        )


def merge_part_extra_data(existing: str | None, wanted: list[Marker], managed: set[MarkerType], duration_ms: int) -> str:
    """Rewrite ``pv:intros``/``pv:credits`` for managed types, keep every other key, rebuild ``url``.

    Raises:
        PublishError: existing extra_data is not a JSON object, or holds marker data of an untested shape/version.
    """
    try:
        d = json.loads(existing) if existing else {}
    except ValueError as exc:
        raise PublishError("Plex media_parts.extra_data is not JSON", state=Capability.UNSUPPORTED_SCHEMA) from exc
    if not isinstance(d, dict):
        raise PublishError("Plex media_parts.extra_data is not a JSON object", state=Capability.UNSUPPORTED_SCHEMA)
    for key in _PART_KEY.values():
        if key in d:
            _check_marker_array(key, str(d[key]))
    for mtype in (MarkerType.INTRO, MarkerType.CREDITS):
        if mtype not in managed:
            continue
        of_type = [m for m in wanted if m.type is mtype]
        if not of_type:
            d[_PART_KEY[mtype]] = ""
            continue
        entries = []
        for m in of_type:
            start, end, final = _stored_times(m, duration_ms)
            entry: dict[str, object] = {"startTimeOffset": start, "endTimeOffset": end}
            if final:
                entry["final"] = True
            entries.append(entry)
        payload = {
            "MediaPartMarkersArray": {
                "attributeName": "intros" if mtype is MarkerType.INTRO else "credits",
                "version": INTRO_JSON_VERSION if mtype is MarkerType.INTRO else CREDITS_JSON_VERSION,
                "MediaPartMarker": entries,
            }
        }
        d[_PART_KEY[mtype]] = json.dumps(payload, separators=(",", ":"))
    return encode_extra_data(d)


def _tagging_extra(marker: Marker, final: bool) -> str:
    if marker.type is MarkerType.INTRO:
        return encode_extra_data({"pv:version": str(INTRO_JSON_VERSION)})
    fields = {"pv:version": str(CREDITS_JSON_VERSION)}
    if final:
        fields["pv:final"] = "1"
    return encode_extra_data(fields)


class PlexMarkerPublisher(MarkerPublisher):
    """Writes markers into one Plex server's database."""

    supported_types = frozenset({MarkerType.INTRO, MarkerType.CREDITS})
    name = "plex_db"

    def __init__(
        self,
        server,
        config,
        settings,
        *,
        sibling_markers: Callable[[str], dict[MarkerType, Marker] | None] | None = None,
        mountinfo_path: str = "/proc/self/mountinfo",
    ) -> None:
        """Create the publisher.

        Args:
            server: Live ``PlexServer`` client.
            config: That server's ``ServerConfig``.
            settings: That server's ``ServerMarkersSettings``.
            sibling_markers: Looks up decided markers for another local file (multi-version items).
            mountinfo_path: For tests.
        """
        self._server = server
        self._config = config
        self._settings = settings
        self._sibling_markers = sibling_markers or (lambda _path: None)
        self._mountinfo_path = mountinfo_path

    def db_path(self) -> str | None:
        """DB path, or None when the Plex config folder isn't set."""
        folder = str((self._config.output or {}).get("plex_config_folder") or "").strip()
        return plex_db_path(folder) if folder else None

    def _connect(self, *, read_only: bool) -> sqlite3.Connection:
        mode = "ro" if read_only else "rw"
        conn = sqlite3.connect(f"file:{self.db_path()}?mode={mode}", uri=True, timeout=30, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @staticmethod
    def _check_columns(conn: sqlite3.Connection) -> None:
        for table, required in _REQUIRED_COLUMNS.items():
            cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}  # noqa: S608 - fixed table names
            missing = required - cols
            if missing:
                raise PublishError(
                    f"Plex database looks different from the tested version (table {table} lacks {sorted(missing)}); "
                    "not writing markers.",
                    state=Capability.UNSUPPORTED_SCHEMA,
                )

    @staticmethod
    def _check_library_marker_versions(conn: sqlite3.Connection) -> None:
        # Two unindexed LIKE scans of media_parts: run from capability() (cached per job), never per write. Each
        # write still validates the parts it touches in merge_part_extra_data.
        for key in _PART_KEY.values():
            rows = conn.execute(
                "SELECT extra_data FROM media_parts WHERE extra_data LIKE ? LIMIT 50", (f'%"{key}":"{{%',)
            ).fetchall()
            for (extra,) in rows:
                try:
                    value = json.loads(extra)[key]
                except (ValueError, KeyError, TypeError):
                    continue
                _check_marker_array(key, str(value))

    @staticmethod
    def _item_parts(conn: sqlite3.Connection, rating_key: int) -> list[tuple]:
        return conn.execute(
            "SELECT mp.id, mp.media_item_id, mp.file, mp.extra_data FROM media_parts mp JOIN media_items mi "
            "ON mi.id = mp.media_item_id WHERE mi.metadata_item_id=? AND mp.deleted_at IS NULL ORDER BY mp.id",
            (rating_key,),
        ).fetchall()

    @staticmethod
    def _marker_tag_id(conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT id FROM tags WHERE tag_type=? AND tag='' ORDER BY id LIMIT 1", (MARKER_TAG_TYPE,)
        ).fetchone()
        if row is None:
            raise PublishError(
                "Plex hasn't created its marker tag yet. Run Plex's own intro or credits detection once on any "
                "item, then try again.",
                state=Capability.NEEDS_PLEX_DETECTION_ONCE,
            )
        return int(row[0])

    def capability(self) -> CapabilityReport:
        """Check settings, DB location, Plex Pass, schema and the marker tag row."""
        if not self._settings.enabled:
            return CapabilityReport(Capability.DISABLED, "Intro & Credits is off for this server")
        if not self._settings.db_write_confirmed_at:
            return CapabilityReport(Capability.NEEDS_CONFIRMATION, "Confirm the Plex database write to turn this on")
        db = self.db_path()
        if not db or not os.path.isfile(db):
            return CapabilityReport(
                Capability.MISCONFIGURED, f"Plex database not found at {db or '(Plex config folder not set)'}"
            )
        fs_type = filesystem_type(os.path.dirname(db), mountinfo_path=self._mountinfo_path)
        details: dict = {"db_path": db, "fs_type": fs_type}
        if is_network_filesystem(fs_type):
            return CapabilityReport(
                Capability.NEEDS_LOCAL_DB,
                f"Plex's database is on a network share ({fs_type}). The app must run on the same machine as Plex "
                "to write markers; Plex stays read-only.",
                details,
            )
        plex_pass = self._server.has_plex_pass()
        details["plex_pass"] = plex_pass
        if plex_pass is False:
            return CapabilityReport(
                Capability.NEEDS_PASS, "This Plex server has no Plex Pass, so Plex won't show any markers.", details
            )
        try:
            details["detection"] = self._server.get_marker_detection_prefs()
        except Exception:
            details["detection"] = {"intro": None, "credits": None}
        try:
            conn = self._connect(read_only=True)
            try:
                self._check_columns(conn)
                self._check_library_marker_versions(conn)
                self._marker_tag_id(conn)
            finally:
                conn.close()
        except PublishError as exc:
            return CapabilityReport(exc.state or Capability.MISCONFIGURED, str(exc), details)
        except sqlite3.Error as exc:
            return CapabilityReport(Capability.MISCONFIGURED, f"Can't open Plex's database: {exc}", details)
        return CapabilityReport(Capability.READY, "Written into this Plex server's database", details)

    def read(self, item_id: str) -> list[Marker]:
        """Our view of the item's intro/credits rows, converted to served times."""
        rating_key = int(str(item_id).rsplit("/", 1)[-1])
        conn = self._connect(read_only=True)
        try:
            tag_id = self._marker_tag_id(conn)
            rows = conn.execute(
                "SELECT text, time_offset, end_time_offset, extra_data FROM taggings WHERE metadata_item_id=? AND "
                "tag_id=? AND text IN ('intro','credits') ORDER BY time_offset",
                (rating_key, tag_id),
            ).fetchall()
        finally:
            conn.close()
        out = []
        for text, start, end, extra in rows:
            mtype = MarkerType.INTRO if text == "intro" else MarkerType.CREDITS
            if mtype is MarkerType.CREDITS:
                final = '"pv:final":"1"' in (extra or "")
                start = start + CREDITS_SERVE_SHIFT_MS
                end = end if final else end - CREDITS_SERVE_SHIFT_MS
            out.append(Marker(mtype, int(start), int(end), ("plex",)))
        return out

    def _local_candidates(self, plex_path: str) -> list[str]:
        return apply_path_mappings(plex_path, list(self._config.path_mappings or [])) or [plex_path]

    def _check_versions(self, parts: list[tuple], wanted: list[Marker], canonical_path: str) -> None:
        media_items = [p[1] for p in parts]
        if len(set(media_items)) != len(media_items):
            raise PublishError("Plex item uses stacked multi-part files; markers for those are not supported")
        others = [p for p in parts if canonical_path not in self._local_candidates(p[2])]
        for _part_id, _mi, plex_file, _extra in others:
            local = next((c for c in self._local_candidates(plex_file) if c != canonical_path), plex_file)
            sibling = self._sibling_markers(local)
            if not sibling:
                raise WaitingForVersionsError(
                    "Plex shares one marker set per item; waiting for the other version(s) to be decided"
                )
            sib = {t: m for t, m in sibling.items() if t in self.supported_types}
            mine = {m.type: m for m in wanted}
            if set(sib) != set(mine) or any(
                abs(sib[t].start_ms - mine[t].start_ms) > VERSION_AGREEMENT_MS
                or abs(sib[t].end_ms - mine[t].end_ms) > VERSION_AGREEMENT_MS
                for t in mine
            ):
                raise WaitingForVersionsError(
                    "Versions of this Plex item have different markers; Plex can only show one set"
                )

    def write(
        self, item_id: str, markers: list[Marker], *, previous: list[Marker], duration_ms: int, canonical_path: str
    ) -> None:
        """Replace our managed types for this item in ``taggings`` and every part's ``extra_data``."""
        wanted = self.project(markers)
        managed = {m.type for m in wanted} | {m.type for m in previous if m.type in self.supported_types}
        if not managed:
            return
        rating_key = int(str(item_id).rsplit("/", 1)[-1])
        conn = self._connect(read_only=False)
        try:
            # Outside Plex's write lock: fail-fast checks and the multi-version agreement, whose sibling lookups take
            # markers.db's lock — never hold that while Plex waits on us.
            self._check_columns(conn)
            self._marker_tag_id(conn)
            parts = self._item_parts(conn, rating_key)
            if not parts:
                raise ItemNotFoundError(f"Plex item {rating_key} not found in the database")
            self._check_versions(parts, wanted, canonical_path)
            created_at = int(time.time())
            texts = sorted(_TYPE_TEXT[t] for t in managed)
            # BEGIN IMMEDIATE can wait up to 30 s — exactly while Plex is writing, often to this item. Everything we
            # merge is re-read after it, or Plex's fresh extra_data keys would be overwritten from the stale read.
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._check_columns(conn)
                tag_id = self._marker_tag_id(conn)
                fresh = self._item_parts(conn, rating_key)
                if [p[:3] for p in fresh] != [p[:3] for p in parts]:
                    raise WaitingForVersionsError(
                        "Plex changed this item's files while we were writing; trying again on the next run"
                    )
                new_extra = [
                    (merge_part_extra_data(extra, wanted, managed, duration_ms), part_id)
                    for part_id, _mi, _file, extra in fresh
                ]
                conn.execute(
                    f"DELETE FROM taggings WHERE metadata_item_id=? AND tag_id=? AND text IN ({','.join('?' * len(texts))})",  # noqa: S608
                    (rating_key, tag_id, *texts),
                )
                for index, marker in enumerate(wanted):
                    start, end, final = _stored_times(marker, duration_ms)
                    conn.execute(
                        "INSERT INTO taggings (metadata_item_id, tag_id, [index], text, time_offset, end_time_offset, "
                        "thumb_url, created_at, extra_data) VALUES (?,?,?,?,?,?,'',?,?)",
                        (rating_key, tag_id, index, _TYPE_TEXT[marker.type], start, end, created_at,
                         _tagging_extra(marker, final)),
                    )
                conn.executemany("UPDATE media_parts SET extra_data=? WHERE id=?", new_extra)
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
        logger.info("Plex {}: wrote {} marker(s) for item {}", self._config.name, len(wanted), rating_key)
```

Walk-through checks before running tests: in `test_writes_taggings...` `wanted` is sorted by start so intro gets index 0;
credits DB start 1_299_000−2000 = 1_297_000, final (end == duration) → end unchanged; `created_at` stored as INTEGER.
In the two lock-wait tests the fixture DB uses SQLite's default rollback journal: the locker's uncommitted UPDATE is
invisible to our pre-lock read (readers see the last commit), which is exactly the stale read the re-select guards.

- [ ] **Step 8: Implement the three `PlexServer` helpers**

```python
    def has_plex_pass(self) -> bool | None:
        """Whether the server has an active Plex Pass (``myPlexSubscription``); None when unreachable."""
        try:
            return bool(getattr(self._connect(), "myPlexSubscription", False))
        except Exception as exc:
            logger.debug("Plex Pass check failed for {}: {}", self.name, exc)
            return None

    def get_marker_detection_prefs(self) -> dict[str, str | None]:
        """Plex's own intro/credits detection prefs (hidden on servers without Plex Pass)."""
        out: dict[str, str | None] = {"intro": None, "credits": None}
        try:
            settings = self._connect().settings
            for key, pref in (("intro", "GenerateIntroMarkerBehavior"), ("credits", "GenerateCreditsMarkerBehavior")):
                try:
                    out[key] = str(settings.get(pref).value)
                except Exception:
                    out[key] = None
        except Exception as exc:
            logger.debug("Plex marker prefs unavailable for {}: {}", self.name, exc)
        return out

    def get_markers(self, item_id: str) -> list[dict] | None:
        """Intro/credits markers Plex serves for an item (``includeMarkers=1``); None on error."""
        from ..plex_client import retry_plex_call

        bare_id = str(item_id or "").strip().rsplit("/", 1)[-1]
        try:
            root = retry_plex_call(self._connect().query, f"/library/metadata/{bare_id}?includeMarkers=1")
        except Exception as exc:
            logger.debug("Plex marker read failed for {}: {}", bare_id, exc)
            return None
        node = next(iter(root), None) if root is not None else None
        if node is None:
            return None
        out = []
        for m in node.findall("Marker"):
            if m.get("type") not in ("intro", "credits"):
                continue
            out.append({"type": m.get("type"), "start_ms": int(m.get("startTimeOffset") or 0),
                        "end_ms": int(m.get("endTimeOffset") or 0), "final": m.get("final") in ("1", "true")})
        return out
```
The pref ids `GenerateIntroMarkerBehavior`/`GenerateCreditsMarkerBehavior` are verified on the claimed lab Plex in
Task 19; if they differ, fix them there and record the real ids in spec §3.1.

- [ ] **Step 9: Run** `pytest --no-cov tests/markers/test_fs.py tests/markers/test_plex_db_publisher.py tests/test_servers_plex.py -q` → PASS.
- [ ] **Step 10: Commit** — `feat(markers): Plex database publisher with schema, tag-row, network-fs and multi-version guards`

---
## Task 9: Jellyfin Bridge plugin — markers endpoint + segment provider, 10.11 and 12.0 builds

Spec §3.2, §6.3 JellyfinMarkerPublisher, §10.4. Prototype: `evidence/plugins/jellyfin-10.11/`, `-12.0/`.
Verified in Jellyfin source (`Jellyfin.Server.Implementations/MediaSegments/MediaSegmentManager.cs`, 10.11.0 and
master): `RunSegmentPluginProviders(item, options, forceOverwrite:false)` compares each provider's new segments with
its existing rows, deletes only that provider's rows when they changed, and deletes them when the provider returns
none — so updates and removals work without touching other providers' segments.

**Files:**
- Create: `jellyfin-plugin/Markers/MarkerStore.cs`, `jellyfin-plugin/Markers/BridgeSegmentProvider.cs`,
  `jellyfin-plugin/PluginServiceRegistrator.cs`, `jellyfin-plugin/Api/MarkersController.cs`,
  `.github/workflows/plugins-ci.yml`
- Modify: `jellyfin-plugin/Jellyfin.Plugin.MediaPreviewBridge.csproj` (ABI switch), `jellyfin-plugin/Api/TrickplayBridgeController.cs`
  (Ping `features`), `jellyfin-plugin/Plugin.cs` (description), `jellyfin-plugin/README.md`,
  `.github/workflows/jellyfin-plugin.yml` (build both ABIs, manifest with both `targetAbi`s)

**Interfaces:**
- Produces (HTTP, admin auth like the existing controller):
  - `GET /MediaPreviewBridge/Ping` → adds `"features": ["trickplay", "markers"]` (anonymous, unchanged otherwise)
  - `GET /MediaPreviewBridge/Markers/{itemId:guid}` → `200 {"itemId", "segments": [{"type","startTicks","endTicks"}]}`
    (404 `{"error":"item not found"}`)
  - `POST /MediaPreviewBridge/Markers/{itemId:guid}` body `{"segments": [{"type": "Intro"|"Outro"|"Recap"|"Preview",
    "startTicks": long, "endTicks": long}]}` → `200 {"itemId", "stored": n}`; 400 `{"error"}` on bad input; 404 unknown item
  - `DELETE /MediaPreviewBridge/Markers/{itemId:guid}` → 204; 404 unknown item
  - Segment provider name **"Media Preview Bridge"** (Jellyfin's provider id = MD5 of the lower-cased name)
- Versions: 10.11 build `10.11.1.0` (net9.0, Jellyfin.Controller 10.11.0); 12.0 build `12.0.1.0` (net10.0,
  Jellyfin.Controller 12.0.0, `JF12` define). Tag scheme at release: `plugin-v10.11.1.0` builds both (12.0 build
  version = `12.0.` + last two components).

- [ ] **Step 1: csproj ABI switch**

```xml
<Project Sdk="Microsoft.NET.Sdk">

  <PropertyGroup>
    <!-- Build per Jellyfin ABI: `dotnet build -p:JellyfinAbi=12.0` for Jellyfin 12 (net10 + CleanupExtractedData). -->
    <JellyfinAbi Condition="'$(JellyfinAbi)' == ''">10.11</JellyfinAbi>
    <RootNamespace>Jellyfin.Plugin.MediaPreviewBridge</RootNamespace>
    <AssemblyName>Jellyfin.Plugin.MediaPreviewBridge</AssemblyName>
    <Nullable>enable</Nullable>
    <TreatWarningsAsErrors>true</TreatWarningsAsErrors>
    <GenerateAssemblyInfo>true</GenerateAssemblyInfo>
  </PropertyGroup>

  <PropertyGroup Condition="'$(JellyfinAbi)' == '10.11'">
    <TargetFramework>net9.0</TargetFramework>
    <JellyfinControllerVersion>10.11.0</JellyfinControllerVersion>
    <SkiaSharpVersion>2.88.9</SkiaSharpVersion>
    <Version>10.11.1.0</Version>
  </PropertyGroup>

  <PropertyGroup Condition="'$(JellyfinAbi)' == '12.0'">
    <TargetFramework>net10.0</TargetFramework>
    <JellyfinControllerVersion>12.0.0</JellyfinControllerVersion>
    <!-- Jellyfin 12.0 ships SkiaSharp 3.119.4 (checked in the lab container's jellyfin.deps.json). -->
    <SkiaSharpVersion>3.119.4</SkiaSharpVersion>
    <Version>12.0.1.0</Version>
    <DefineConstants>$(DefineConstants);JF12</DefineConstants>
  </PropertyGroup>

  <PropertyGroup>
    <FileVersion>$(Version)</FileVersion>
    <AssemblyVersion>$(Version)</AssemblyVersion>
  </PropertyGroup>

  <ItemGroup>
    <PackageReference Include="Jellyfin.Controller" Version="$(JellyfinControllerVersion)" />
    <PackageReference Include="SkiaSharp" Version="$(SkiaSharpVersion)" PrivateAssets="all" />
  </ItemGroup>

</Project>
```

- [ ] **Step 2: Store, provider, registrator**

```csharp
// jellyfin-plugin/Markers/MarkerStore.cs
using System;
using System.Collections.Generic;
using System.IO;
using System.Text.Json;
using Jellyfin.Database.Implementations.Enums;

namespace Jellyfin.Plugin.MediaPreviewBridge.Markers;

/// <summary>One stored segment (ticks).</summary>
/// <param name="Type">Segment type.</param>
/// <param name="StartTicks">Start in ticks.</param>
/// <param name="EndTicks">End in ticks.</param>
public record StoredSegment(MediaSegmentType Type, long StartTicks, long EndTicks);

/// <summary>
/// Markers pushed by Media Preview Generator, one JSON file per item in the plugin data folder, so the provider can
/// hand the same segments back whenever Jellyfin re-runs segment providers (scans, refreshes, restarts).
/// </summary>
public static class MarkerStore
{
    private static readonly object Gate = new();

    private static string Dir => Path.Combine(Plugin.Instance!.DataFolderPath, "markers");

    private static string FileFor(Guid itemId) => Path.Combine(Dir, itemId.ToString("N") + ".json");

    /// <summary>Replace the stored segments for an item.</summary>
    /// <param name="itemId">Item id.</param>
    /// <param name="segments">Segments to store.</param>
    public static void Save(Guid itemId, IReadOnlyList<StoredSegment> segments)
    {
        lock (Gate)
        {
            Directory.CreateDirectory(Dir);
            var tmp = FileFor(itemId) + ".tmp";
            File.WriteAllText(tmp, JsonSerializer.Serialize(segments));
            File.Move(tmp, FileFor(itemId), overwrite: true);
        }
    }

    /// <summary>Stored segments for an item (empty when none).</summary>
    /// <param name="itemId">Item id.</param>
    /// <returns>The segments.</returns>
    public static IReadOnlyList<StoredSegment> Load(Guid itemId)
    {
        lock (Gate)
        {
            var path = FileFor(itemId);
            if (!File.Exists(path))
            {
                return Array.Empty<StoredSegment>();
            }

            return JsonSerializer.Deserialize<List<StoredSegment>>(File.ReadAllText(path)) ?? new List<StoredSegment>();
        }
    }

    /// <summary>Forget an item.</summary>
    /// <param name="itemId">Item id.</param>
    public static void Delete(Guid itemId)
    {
        lock (Gate)
        {
            var path = FileFor(itemId);
            if (File.Exists(path))
            {
                File.Delete(path);
            }
        }
    }
}
```

```csharp
// jellyfin-plugin/Markers/BridgeSegmentProvider.cs
using System.Collections.Generic;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.MediaSegments;
using MediaBrowser.Model;
using MediaBrowser.Model.MediaSegments;

namespace Jellyfin.Plugin.MediaPreviewBridge.Markers;

/// <summary>
/// Hands stored markers to Jellyfin. Jellyfin only serves segments whose provider is registered, and deletes a
/// provider's rows when it returns none — so this provider is what makes pushed markers visible and durable.
/// </summary>
public class BridgeSegmentProvider : IMediaSegmentProvider
{
    /// <summary>Provider name; Jellyfin derives the provider id from it, so never rename.</summary>
    public const string ProviderName = "Media Preview Bridge";

    /// <inheritdoc />
    public string Name => ProviderName;

    /// <inheritdoc />
    public Task<IReadOnlyList<MediaSegmentDto>> GetMediaSegments(MediaSegmentGenerationRequest request, CancellationToken cancellationToken)
    {
        IReadOnlyList<MediaSegmentDto> segments = MarkerStore.Load(request.ItemId)
            .Select(s => new MediaSegmentDto { ItemId = request.ItemId, Type = s.Type, StartTicks = s.StartTicks, EndTicks = s.EndTicks })
            .ToList();
        return Task.FromResult(segments);
    }

    /// <inheritdoc />
    public ValueTask<bool> Supports(BaseItem item) => ValueTask.FromResult(item is Video);

#if JF12
    /// <inheritdoc />
    public Task CleanupExtractedData(System.Guid itemId, CancellationToken cancellationToken)
    {
        MarkerStore.Delete(itemId);
        return Task.CompletedTask;
    }
#endif
}
```

```csharp
// jellyfin-plugin/PluginServiceRegistrator.cs
using Jellyfin.Plugin.MediaPreviewBridge.Markers;
using MediaBrowser.Controller;
using MediaBrowser.Controller.MediaSegments;
using MediaBrowser.Controller.Plugins;
using Microsoft.Extensions.DependencyInjection;

namespace Jellyfin.Plugin.MediaPreviewBridge;

/// <summary>Registers the markers segment provider with Jellyfin's DI container.</summary>
public class PluginServiceRegistrator : IPluginServiceRegistrator
{
    /// <inheritdoc />
    public void RegisterServices(IServiceCollection serviceCollection, IServerApplicationHost applicationHost)
    {
        serviceCollection.AddSingleton<IMediaSegmentProvider, BridgeSegmentProvider>();
    }
}
```

- [ ] **Step 3: Markers controller**

```csharp
// jellyfin-plugin/Api/MarkersController.cs
using System;
using System.Collections.Generic;
using System.Linq;
using System.Net.Mime;
using System.Threading;
using System.Threading.Tasks;
using Jellyfin.Database.Implementations.Enums;
using Jellyfin.Plugin.MediaPreviewBridge.Markers;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Library;
using MediaBrowser.Controller.MediaSegments;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.MediaPreviewBridge.Api;

/// <summary>One segment in a markers push.</summary>
public class MarkerSegmentDto
{
    /// <summary>Gets or sets the type: Intro, Outro, Recap or Preview.</summary>
    public string Type { get; set; } = string.Empty;

    /// <summary>Gets or sets the start in ticks.</summary>
    public long StartTicks { get; set; }

    /// <summary>Gets or sets the end in ticks.</summary>
    public long EndTicks { get; set; }
}

/// <summary>Markers push body.</summary>
public class MarkersRequest
{
    /// <summary>Gets or sets the segments to show for the item (replaces what we stored before).</summary>
    public List<MarkerSegmentDto> Segments { get; set; } = new();
}

/// <summary>Stores Skip Intro / Skip Credits markers pushed by Media Preview Generator and publishes them as media segments.</summary>
[ApiController]
[Authorize(Policy = "RequiresElevation")]
[Route("MediaPreviewBridge/Markers")]
[Produces(MediaTypeNames.Application.Json)]
public class MarkersController : ControllerBase
{
    private static readonly MediaSegmentType[] AllowedTypes =
    {
        MediaSegmentType.Intro, MediaSegmentType.Outro, MediaSegmentType.Recap, MediaSegmentType.Preview,
    };

    private readonly ILibraryManager _libraryManager;
    private readonly IMediaSegmentManager _segmentManager;
    private readonly ILogger<MarkersController> _logger;

    /// <summary>Initializes a new instance of the <see cref="MarkersController"/> class.</summary>
    /// <param name="libraryManager">Library manager.</param>
    /// <param name="segmentManager">Media segment manager.</param>
    /// <param name="logger">Logger.</param>
    public MarkersController(ILibraryManager libraryManager, IMediaSegmentManager segmentManager, ILogger<MarkersController> logger)
    {
        _libraryManager = libraryManager;
        _segmentManager = segmentManager;
        _logger = logger;
    }

    /// <summary>Stored markers for an item.</summary>
    /// <param name="itemId">Item id.</param>
    /// <returns>Stored segments.</returns>
    [HttpGet("{itemId:guid}")]
    public IActionResult Get([FromRoute] Guid itemId)
    {
        if (_libraryManager.GetItemById(itemId) is null)
        {
            return NotFound(new { error = "item not found" });
        }

        var segments = MarkerStore.Load(itemId).Select(s => new { type = s.Type.ToString(), startTicks = s.StartTicks, endTicks = s.EndTicks });
        return Ok(new { itemId, segments });
    }

    /// <summary>Replace the markers for an item and publish them.</summary>
    /// <param name="itemId">Item id.</param>
    /// <param name="request">Segments.</param>
    /// <param name="cancellationToken">Cancellation token.</param>
    /// <returns>Count stored.</returns>
    [HttpPost("{itemId:guid}")]
    public async Task<IActionResult> Post([FromRoute] Guid itemId, [FromBody] MarkersRequest request, CancellationToken cancellationToken)
    {
        var item = _libraryManager.GetItemById(itemId);
        if (item is null)
        {
            return NotFound(new { error = "item not found" });
        }

        if (item is not Video)
        {
            return BadRequest(new { error = "item is not a video" });
        }

        var stored = new List<StoredSegment>();
        foreach (var s in request.Segments ?? new List<MarkerSegmentDto>())
        {
            if (!Enum.TryParse<MediaSegmentType>(s.Type, ignoreCase: true, out var type) || !AllowedTypes.Contains(type))
            {
                return BadRequest(new { error = $"unsupported segment type '{s.Type}'" });
            }

            if (s.StartTicks < 0 || s.EndTicks <= s.StartTicks)
            {
                return BadRequest(new { error = $"invalid ticks {s.StartTicks}..{s.EndTicks}" });
            }

            stored.Add(new StoredSegment(type, s.StartTicks, s.EndTicks));
        }

        MarkerStore.Save(itemId, stored);
        await _segmentManager.RunSegmentPluginProviders(item, _libraryManager.GetLibraryOptions(item), false, cancellationToken).ConfigureAwait(false);
        _logger.LogInformation("Media Preview Bridge stored {Count} marker segment(s) for {ItemId}", stored.Count, itemId);
        return Ok(new { itemId, stored = stored.Count });
    }

    /// <summary>Remove our markers for an item.</summary>
    /// <param name="itemId">Item id.</param>
    /// <param name="cancellationToken">Cancellation token.</param>
    /// <returns>204.</returns>
    [HttpDelete("{itemId:guid}")]
    public async Task<IActionResult> Delete([FromRoute] Guid itemId, CancellationToken cancellationToken)
    {
        var item = _libraryManager.GetItemById(itemId);
        if (item is null)
        {
            return NotFound(new { error = "item not found" });
        }

        MarkerStore.Delete(itemId);
        await _segmentManager.RunSegmentPluginProviders(item, _libraryManager.GetLibraryOptions(item), false, cancellationToken).ConfigureAwait(false);
        return NoContent();
    }
}
```

In `TrickplayBridgeController.Ping` add `features = new[] { "trickplay", "markers" },` to the anonymous object. Update
`Plugin.Description` to mention markers.

- [ ] **Step 4: Build both ABIs locally (storage, low priority)**

```bash
cd /home/data/workspace/plex_generate_vid_previews/jellyfin-plugin
nice -n 19 docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD":/src -w /src mcr.microsoft.com/dotnet/sdk:9.0 \
  sh -c 'dotnet build -c Release -p:JellyfinAbi=10.11 -o /src/out/10.11'
nice -n 19 docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD":/src -w /src mcr.microsoft.com/dotnet/sdk:10.0 \
  sh -c 'dotnet build -c Release -p:JellyfinAbi=12.0 -o /src/out/12.0'
ls -la out/10.11/Jellyfin.Plugin.MediaPreviewBridge.dll out/12.0/Jellyfin.Plugin.MediaPreviewBridge.dll
```
Expected: both builds succeed with 0 warnings (TreatWarningsAsErrors). Add `out/` to `jellyfin-plugin/.gitignore`.
(`obj/` must be cleaned between ABIs: `rm -rf obj` before the second build.)

- [ ] **Step 5: Install into both lab Jellyfins and prove the endpoint** (lab only)

```bash
cd /home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/lab && set -a && . ./env && set +a
# 10.11: replace the lab plugin and the old Bridge (if present) with the new build
docker exec mlab-jellyfin sh -c 'rm -rf /config/plugins/MarkersLab* /config/plugins/MediaPreviewBridge_*; mkdir -p /config/plugins/MediaPreviewBridge_10.11.1.0'
docker cp ../../../../../jellyfin-plugin/out/10.11/Jellyfin.Plugin.MediaPreviewBridge.dll mlab-jellyfin:/config/plugins/MediaPreviewBridge_10.11.1.0/
docker restart mlab-jellyfin
# 12.0: same with the net10 build into mlab-jf12 (/config/plugins/MediaPreviewBridge_12.0.1.0)
curl -s http://127.0.0.1:18097/MediaPreviewBridge/Ping          # expect "features":["trickplay","markers"]
curl -s -X POST "http://127.0.0.1:18097/MediaPreviewBridge/Markers/$JF_ITEM" -H "Authorization: MediaBrowser Token=\"$JF_TOKEN\"" \
  -H 'Content-Type: application/json' -d '{"segments":[{"type":"Intro","startTicks":1268000000,"endTicks":1570000000}]}'
curl -s "http://127.0.0.1:18097/MediaSegments/$JF_ITEM" -H "Authorization: MediaBrowser Token=\"$JF_TOKEN\""
```
Matrix to run on **both** 10.11 and 12.0 and record in `evidence/lab/phase1-results.md` (pass/fail + raw output):
1. POST intro → `/MediaSegments` shows it.  2. POST changed times → old row replaced (not duplicated).
3. POST intro+outro+recap+preview → all four.  4. POST `[]` → our rows gone.  5. DELETE → gone.
6. Library scan → rows kept.  7. Item refresh `ReplaceAllMetadata=true` → kept.  8. `docker restart` → kept.
9. Media Segment Scan scheduled task → kept.  10. Trickplay POST still works (regression of the existing endpoint,
especially on 12.0 with SkiaSharp 3.x).  11. Bad type → 400; end ≤ start → 400; unknown item → 404.
12. Playwright: `lab/jf_client.py` on the synth VP9 episode shows **Skip Intro** from our provider.
Remove the lab `MarkersLab` plugin afterwards (the Bridge now does its job).

- [ ] **Step 6: CI builds**

`.github/workflows/plugins-ci.yml`:
```yaml
name: Plugins — build
on:
  pull_request:
    paths: ["jellyfin-plugin/**", ".github/workflows/plugins-ci.yml"]
  workflow_dispatch:
jobs:
  jellyfin:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        include:
          - abi: "10.11"
            dotnet: "9.0.x"
          - abi: "12.0"
            dotnet: "10.0.x"
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-dotnet@v4
        with:
          dotnet-version: ${{ matrix.dotnet }}
      - name: Build
        working-directory: jellyfin-plugin
        run: dotnet build -c Release -p:JellyfinAbi=${{ matrix.abi }}
```
`jellyfin-plugin.yml`: turn `build-release` into a matrix over the same two ABIs. Version: 10.11 row uses the resolved
version; 12.0 row uses `12.0.$(echo "$VERSION" | cut -d. -f3-4)`. Zip names `media-preview-bridge_${ROW_VERSION}.zip`
from `bin/Release/net9.0` or `net10.0`. Collect both `{version, checksum, zip}` as job outputs (one output name per
row). `publish-pages` adds two `versions` entries: `targetAbi` `"10.11.0.0"` and `"12.0.0.0"`, each with its own
`sourceUrl`/`checksum`. Keep the MD5 comment and the de-dupe. Do **not** push a tag in this phase (public plugin
releases happen at merge, spec §11).

- [ ] **Step 7: Commit** — `feat(jellyfin-plugin): markers endpoint and segment provider, builds for Jellyfin 10.11 and 12.0`

---

## Task 10: Jellyfin publisher + "markers already on servers" readers

> **Superseded in part (2026-09-14, Task 8/11 reviews).** Plex publishing is per server item, not per file:
> `WaitingForVersionsError` no longer exists, `write()` returns the markers that are ours on the item, publishers have
> `atomic_writes`, `write()` takes `own_previous`, and `markers.db` has `item_publish_state`. The code blocks below that
> mention the old contract are historical; the binding design is `plex-item-publishing.md` in this folder.

Spec §5.5 item 5, §6.3. Server markers are agreement evidence only, and only read before we have published to that
server (after that the server shows our own markers).

> **Amendment (Task 9 review ruling, 2026-09-14).** The plugin's POST body takes an optional `fileSize` (bytes of the
> file the markers were detected on); the plugin serves nothing when that differs from the item's current on-disk
> length, so a replaced file never shows the old file's markers on Jellyfin 10.11 (which keeps our stored copy). So:
> `put_bridge_markers(item_id, segments, file_size: int | None = None)` sends `{"segments": [...], "fileSize": n}`
> (key omitted when None); `JellyfinMarkerPublisher.write` passes `os.stat(canonical_path).st_size` (None on
> `OSError`). The pipeline (Task 11) must still compare the analysed file identity with the current one right before
> publishing and re-detect instead of publishing when it changed. Tests cover: size sent, size omitted on stat error.

**Files:**
- Create: `media_preview_generator/markers/publishers/jellyfin.py`, `media_preview_generator/markers/sources/server_markers.py`,
  `media_preview_generator/markers/publishers/factory.py`
- Modify: `media_preview_generator/servers/jellyfin.py` (`get_bridge_info`, `get_bridge_markers`, `put_bridge_markers`,
  `delete_bridge_markers`, `get_media_segments`), `media_preview_generator/servers/_embyish.py` (`get_chapter_markers`)
- Test: `tests/markers/test_jellyfin_publisher.py`, `tests/markers/test_server_markers.py`, `tests/markers/test_publisher_factory.py`

**Interfaces:**
- Consumes: Task 8 base classes, `JellyfinServer`, `EmbyServer`, `PlexServer.get_markers` (Task 8).
- Produces:
```python
# servers/jellyfin.py
JellyfinServer.get_bridge_info(self) -> dict | None       # {"installed": bool, "version": str|None, "features": list[str]}; None = unreachable
JellyfinServer.get_bridge_markers(self, item_id: str) -> list[dict] | None       # [{"type","startTicks","endTicks"}]
JellyfinServer.put_bridge_markers(self, item_id: str, segments: list[dict]) -> requests.Response
JellyfinServer.delete_bridge_markers(self, item_id: str) -> requests.Response
JellyfinServer.get_media_segments(self, item_id: str) -> list[dict] | None       # core /MediaSegments Items
# servers/_embyish.py
EmbyApiClient.get_chapter_markers(self, item_id: str) -> list[dict] | None       # [{"marker_type","start_ms","name"}]
# publishers/jellyfin.py
TICKS_PER_MS = 10_000
class JellyfinMarkerPublisher(MarkerPublisher): name = "jellyfin_bridge"; supported_types = all four
    def __init__(self, server, config: ServerConfig, settings: ServerMarkersSettings) -> None
# sources/server_markers.py
def read_server_markers(server, config: ServerConfig, item_id: str) -> list[Candidate] | None   # None = couldn't read
# publishers/factory.py
def publisher_for(server, config: ServerConfig, *, sibling_markers=None) -> MarkerPublisher | None   # None for types without a publisher yet (Emby until phase 2)
```

- [ ] **Step 1: Write failing tests**

```python
# tests/markers/test_jellyfin_publisher.py
from unittest.mock import MagicMock

import pytest

from media_preview_generator.markers.models import Marker, MarkerType
from media_preview_generator.markers.publishers.base import Capability, ItemNotFoundError, PublishError
from media_preview_generator.markers.publishers.jellyfin import JellyfinMarkerPublisher
from media_preview_generator.markers.settings import ServerMarkersSettings
from media_preview_generator.servers.base import ServerConfig, ServerType

T = MarkerType
CFG = ServerConfig(id="jf-1", type=ServerType.JELLYFIN, name="JF", enabled=True, url="http://j", auth={})


def _pub(server, enabled=True):
    return JellyfinMarkerPublisher(server, CFG, ServerMarkersSettings(enabled, None, None, "restore"))


def _resp(status, body=None):
    r = MagicMock(status_code=status)
    r.json.return_value = body
    return r


@pytest.mark.parametrize(
    ("enabled", "info", "state"),
    [
        (False, {"installed": True, "version": "10.11.1.0", "features": ["trickplay", "markers"]}, Capability.DISABLED),
        (True, None, Capability.UNREACHABLE),
        (True, {"installed": False, "version": None, "features": []}, Capability.NEEDS_PLUGIN),
        (True, {"installed": True, "version": "10.11.0.3", "features": []}, Capability.PLUGIN_OUTDATED),
        (True, {"installed": True, "version": "10.11.1.0", "features": ["trickplay", "markers"]}, Capability.READY),
    ],
)
def test_capability_matrix(enabled, info, state):
    server = MagicMock()
    server.get_bridge_info.return_value = info
    report = _pub(server, enabled).capability()
    assert report.state is state
    if state is Capability.READY:
        assert report.details == {"plugin_version": "10.11.1.0", "can_show": ["intro", "credits", "recap", "preview"]}


def test_write_posts_all_types_in_ticks():
    server = MagicMock()
    server.put_bridge_markers.return_value = _resp(200, {"stored": 3})
    markers = [Marker(T.CREDITS, 1_295_000, 1_321_472, ("a",)), Marker(T.INTRO, 126_771, 157_068, ("a",)),
               Marker(T.RECAP, 0, 20_000, ("a",))]
    _pub(server).write("abc", markers, previous=[], duration_ms=1_321_472, canonical_path="/x.mkv")
    server.put_bridge_markers.assert_called_once_with("abc", [
        {"type": "Recap", "startTicks": 0, "endTicks": 200_000_000},
        {"type": "Intro", "startTicks": 1_267_710_000, "endTicks": 1_570_680_000},
        {"type": "Outro", "startTicks": 12_950_000_000, "endTicks": 13_214_720_000},
    ])
    server.delete_bridge_markers.assert_not_called()


def test_write_empty_with_previous_deletes():
    server = MagicMock()
    server.delete_bridge_markers.return_value = _resp(204)
    _pub(server).write("abc", [], previous=[Marker(T.INTRO, 1, 5000, ("a",))], duration_ms=1, canonical_path="/x.mkv")
    server.delete_bridge_markers.assert_called_once_with("abc")
    server.put_bridge_markers.assert_not_called()


def test_write_empty_without_previous_does_nothing():
    server = MagicMock()
    _pub(server).write("abc", [], previous=[], duration_ms=1, canonical_path="/x.mkv")
    server.put_bridge_markers.assert_not_called()
    server.delete_bridge_markers.assert_not_called()


@pytest.mark.parametrize(("status", "body", "exc", "state"), [
    (404, {"error": "item not found"}, ItemNotFoundError, None),
    (404, None, PublishError, Capability.NEEDS_PLUGIN),
    (400, {"error": "unsupported segment type"}, PublishError, None),
    (500, None, PublishError, None),
])
def test_write_errors(status, body, exc, state):
    server = MagicMock()
    server.put_bridge_markers.return_value = _resp(status, body)
    with pytest.raises(exc) as ei:
        _pub(server).write("abc", [Marker(T.INTRO, 1, 5000, ("a",))], previous=[], duration_ms=1, canonical_path="/x.mkv")
    assert ei.value.state is state


def test_read_maps_types_back():
    server = MagicMock()
    server.get_bridge_markers.return_value = [{"type": "Outro", "startTicks": 12_950_000_000, "endTicks": 13_214_720_000},
                                              {"type": "Commercial", "startTicks": 1, "endTicks": 2}]
    assert _pub(server).read("abc") == [Marker(T.CREDITS, 1_295_000, 1_321_472, ("jellyfin",))]
```

```python
# tests/markers/test_server_markers.py
from unittest.mock import MagicMock

from media_preview_generator.markers.models import Candidate, MarkerType, Source
from media_preview_generator.markers.sources.server_markers import read_server_markers
from media_preview_generator.servers.base import ServerConfig, ServerType

T = MarkerType


def _cfg(t):
    return ServerConfig(id=f"{t.value}-1", type=t, name="x", enabled=True, url="http://x", auth={})


def _src(t, start, end, origin):
    return Candidate(t, start, end, Source.SERVER_MARKERS, origin=origin)


def test_plex_final_credits_run_to_end():
    server = MagicMock()
    server.get_markers.return_value = [
        {"type": "intro", "start_ms": 990, "end_ms": 29306, "final": False},
        {"type": "credits", "start_ms": 1156521, "end_ms": 1186521, "final": False},
        {"type": "credits", "start_ms": 1294044, "end_ms": 1322272, "final": True},
    ]
    assert read_server_markers(server, _cfg(ServerType.PLEX), "7") == [
        _src(T.INTRO, 990, 29306, "plex-1"), _src(T.CREDITS, 1156521, 1186521, "plex-1"),
        _src(T.CREDITS, 1294044, None, "plex-1"),
    ]
    server.get_markers.assert_called_once_with("7")


def test_jellyfin_excludes_our_own_segments():
    server = MagicMock()
    server.get_media_segments.return_value = [
        {"Type": "Intro", "StartTicks": 1_267_710_000, "EndTicks": 1_570_680_000},
        {"Type": "Outro", "StartTicks": 12_950_000_000, "EndTicks": 13_214_720_000},
        {"Type": "Commercial", "StartTicks": 1, "EndTicks": 2},
    ]
    server.get_bridge_markers.return_value = [{"type": "Intro", "startTicks": 1_267_710_000, "endTicks": 1_570_680_000}]
    assert read_server_markers(server, _cfg(ServerType.JELLYFIN), "abc") == [
        _src(T.CREDITS, 1_295_000, 1_321_472, "jellyfin-1")]


def test_emby_chapter_markers_pair_intro_start_end():
    server = MagicMock()
    server.get_chapter_markers.return_value = [
        {"marker_type": "Chapter", "start_ms": 0, "name": "Chapter 1"},
        {"marker_type": "IntroStart", "start_ms": 126_771, "name": "Intro"},
        {"marker_type": "IntroEnd", "start_ms": 157_068, "name": "Intro End"},
        {"marker_type": "CreditsStart", "start_ms": 1_295_000, "name": "Credits"},
    ]
    assert read_server_markers(server, _cfg(ServerType.EMBY), "42") == [
        _src(T.INTRO, 126_771, 157_068, "emby-1"), _src(T.CREDITS, 1_295_000, None, "emby-1")]


def test_emby_intro_start_without_end_is_ignored():
    server = MagicMock()
    server.get_chapter_markers.return_value = [{"marker_type": "IntroStart", "start_ms": 5, "name": ""}]
    assert read_server_markers(server, _cfg(ServerType.EMBY), "42") == []


def test_read_failure_is_none():
    server = MagicMock()
    server.get_markers.return_value = None
    assert read_server_markers(server, _cfg(ServerType.PLEX), "7") is None
```

```python
# tests/markers/test_publisher_factory.py
from unittest.mock import MagicMock

import pytest

from media_preview_generator.markers.publishers.factory import publisher_for
from media_preview_generator.markers.publishers.jellyfin import JellyfinMarkerPublisher
from media_preview_generator.markers.publishers.plex_db import PlexMarkerPublisher
from media_preview_generator.servers.base import ServerConfig, ServerType


@pytest.mark.parametrize(("stype", "cls"), [(ServerType.PLEX, PlexMarkerPublisher), (ServerType.JELLYFIN, JellyfinMarkerPublisher), (ServerType.EMBY, type(None))])
def test_factory_matrix_and_settings_passthrough(stype, cls):
    cfg = ServerConfig(id="s", type=stype, name="s", enabled=True, url="http://s", auth={},
                       markers={"enabled": True, "library_ids": ["1"], "plex": {"db_write_confirmed_at": "t", "on_plex_redetect": "keep_plex"}})
    sibling_lookup = MagicMock(name="sibling_markers")
    pub = publisher_for(MagicMock(), cfg, sibling_markers=sibling_lookup)
    assert isinstance(pub, cls)
    if pub is not None:
        assert pub._settings.enabled is True and pub._settings.library_ids == ("1",)
    if stype is ServerType.PLEX:
        assert pub._sibling_markers is sibling_lookup  # multi-version items can only publish once siblings are known
```

Vendor client tests (append to `tests/test_servers_jellyfin.py` / `tests/test_servers_emby.py`), asserting the exact
`_request` calls:
```python
def test_bridge_calls(make_jellyfin):
    server = make_jellyfin()
    ok = MagicMock(status_code=200)
    ok.json.return_value = {"plugin": "MediaPreviewBridge", "version": "10.11.1.0", "ok": True, "features": ["trickplay", "markers"]}
    server._request = MagicMock(return_value=ok)
    assert server.get_bridge_info() == {"installed": True, "version": "10.11.1.0", "features": ["trickplay", "markers"]}
    server._request.assert_called_with("GET", "/MediaPreviewBridge/Ping", timeout=10)

    server._request = MagicMock(return_value=MagicMock(status_code=200))
    server.put_bridge_markers("abc", [{"type": "Intro", "startTicks": 1, "endTicks": 2}])
    server._request.assert_called_with("POST", "/MediaPreviewBridge/Markers/abc",
                                       json_body={"segments": [{"type": "Intro", "startTicks": 1, "endTicks": 2}]})

    server._request = MagicMock(return_value=MagicMock(status_code=204))
    server.delete_bridge_markers("abc")
    server._request.assert_called_with("DELETE", "/MediaPreviewBridge/Markers/abc")

@pytest.mark.parametrize(("side_effect", "status", "expected"), [
    (None, 404, {"installed": False, "version": None, "features": []}),
    (requests.ConnectionError("x"), None, None),
])
def test_bridge_info_not_installed_vs_unreachable(make_jellyfin, side_effect, status, expected):
    server = make_jellyfin()
    server._request = MagicMock(side_effect=side_effect, return_value=MagicMock(status_code=status))
    assert server.get_bridge_info() == expected

def test_get_media_segments(make_jellyfin):
    server = make_jellyfin()
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"Items": [{"Type": "Intro", "StartTicks": 1, "EndTicks": 2}]}
    server._request = MagicMock(return_value=resp)
    assert server.get_media_segments("abc") == [{"Type": "Intro", "StartTicks": 1, "EndTicks": 2}]
    server._request.assert_called_once_with("GET", "/MediaSegments/abc")
```
```python
def test_emby_chapter_markers(make_emby):
    server = make_emby()
    server._fetch_item_fields = MagicMock(return_value={"Chapters": [
        {"StartPositionTicks": 1_267_710_000, "MarkerType": "IntroStart", "Name": "Intro"},
        {"StartPositionTicks": 0, "Name": "Chapter 1"}]})
    assert server.get_chapter_markers("42") == [
        {"marker_type": "IntroStart", "start_ms": 126_771, "name": "Intro"},
        {"marker_type": "Chapter", "start_ms": 0, "name": "Chapter 1"}]
    server._fetch_item_fields.assert_called_once_with("42", "Chapters")
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement vendor helpers**

`servers/jellyfin.py` `JellyfinServer`:
```python
    def get_bridge_info(self) -> dict[str, Any] | None:
        """Bridge plugin presence, version and feature list; None when the server can't be reached."""
        try:
            resp = self._request("GET", "/MediaPreviewBridge/Ping", timeout=10)
        except requests.RequestException as exc:
            logger.debug("Bridge ping failed on {}: {}", self.name, exc)
            return None
        if resp.status_code != 200:
            return {"installed": False, "version": None, "features": []}
        try:
            payload = resp.json() or {}
        except ValueError:
            return {"installed": False, "version": None, "features": []}
        features = payload.get("features") if isinstance(payload.get("features"), list) else []
        return {"installed": bool(payload.get("ok")), "version": payload.get("version"), "features": [str(f) for f in features]}

    def get_bridge_markers(self, item_id: str) -> list[dict] | None:
        """Markers our plugin stored for an item; None on error."""
        try:
            resp = self._request("GET", f"/MediaPreviewBridge/Markers/{item_id}")
            if resp.status_code != 200:
                return None
            return list((resp.json() or {}).get("segments") or [])
        except (requests.RequestException, ValueError):
            return None

    def put_bridge_markers(self, item_id: str, segments: list[dict]) -> requests.Response:
        """Replace our stored markers for an item (plugin publishes them)."""
        return self._request("POST", f"/MediaPreviewBridge/Markers/{item_id}", json_body={"segments": segments})

    def delete_bridge_markers(self, item_id: str) -> requests.Response:
        """Remove our markers for an item."""
        return self._request("DELETE", f"/MediaPreviewBridge/Markers/{item_id}")

    def get_media_segments(self, item_id: str) -> list[dict] | None:
        """Segments Jellyfin serves for an item (all providers); None on error."""
        try:
            resp = self._request("GET", f"/MediaSegments/{item_id}")
            if resp.status_code != 200:
                return None
            return list((resp.json() or {}).get("Items") or [])
        except (requests.RequestException, ValueError):
            return None
```
(Check `_request`'s signature: it takes `json_body=` and `timeout=`; adapt the tests if the keyword names differ.)

`servers/_embyish.py` `EmbyApiClient`:
```python
    def get_chapter_markers(self, item_id: str) -> list[dict] | None:
        """Chapter rows with Emby marker types (IntroStart/IntroEnd/CreditsStart/Chapter); None on error."""
        item = self._fetch_item_fields(item_id, "Chapters")
        if item is None:
            return None
        out = []
        for ch in item.get("Chapters") or []:
            if not isinstance(ch, dict):
                continue
            out.append({"marker_type": str(ch.get("MarkerType") or "Chapter"),
                        "start_ms": int(ch.get("StartPositionTicks") or 0) // 10_000,
                        "name": str(ch.get("Name") or "")})
        return out
```

- [ ] **Step 4: Implement publisher, reader, factory**

```python
# media_preview_generator/markers/publishers/jellyfin.py
"""Jellyfin publisher via the Media Preview Bridge plugin (spec §3.2)."""

from __future__ import annotations

from ..models import Marker, MarkerType
from .base import Capability, CapabilityReport, ItemNotFoundError, MarkerPublisher, PublishError

TICKS_PER_MS = 10_000
MARKERS_FEATURE = "markers"
_TO_JF = {MarkerType.INTRO: "Intro", MarkerType.CREDITS: "Outro", MarkerType.RECAP: "Recap", MarkerType.PREVIEW: "Preview"}
_FROM_JF = {v: k for k, v in _TO_JF.items()}


class JellyfinMarkerPublisher(MarkerPublisher):
    """Pushes markers to the Bridge plugin, which serves them as media segments."""

    supported_types = frozenset(_TO_JF)
    name = "jellyfin_bridge"

    def __init__(self, server, config, settings) -> None:
        self._server = server
        self._config = config
        self._settings = settings

    def capability(self) -> CapabilityReport:
        """Plugin installed with the markers feature?"""
        if not self._settings.enabled:
            return CapabilityReport(Capability.DISABLED, "Intro & Credits is off for this server")
        info = self._server.get_bridge_info()
        if info is None:
            return CapabilityReport(Capability.UNREACHABLE, "Can't reach this Jellyfin server")
        if not info.get("installed"):
            return CapabilityReport(Capability.NEEDS_PLUGIN, "Install the Media Preview Bridge plugin")
        if MARKERS_FEATURE not in (info.get("features") or []):
            return CapabilityReport(
                Capability.PLUGIN_OUTDATED,
                f"Update Media Preview Bridge (installed {info.get('version') or 'unknown'}) to get markers support",
                {"plugin_version": info.get("version")},
            )
        return CapabilityReport(
            Capability.READY,
            "Media Preview Bridge plugin",
            {"plugin_version": info.get("version"), "can_show": ["intro", "credits", "recap", "preview"]},
        )

    def read(self, item_id: str) -> list[Marker]:
        """Markers our plugin currently stores for the item."""
        out = []
        for seg in self._server.get_bridge_markers(item_id) or []:
            mtype = _FROM_JF.get(str(seg.get("type")))
            if mtype is None:
                continue
            out.append(Marker(mtype, int(seg["startTicks"]) // TICKS_PER_MS, int(seg["endTicks"]) // TICKS_PER_MS, ("jellyfin",)))
        return out

    def write(self, item_id: str, markers: list[Marker], *, previous: list[Marker], duration_ms: int, canonical_path: str) -> None:
        """Replace our stored segments; an empty set with a previous publish deletes them."""
        wanted = self.project(markers)
        if not wanted:
            if previous:
                self._check(self._server.delete_bridge_markers(item_id), item_id)
            return
        segments = [{"type": _TO_JF[m.type], "startTicks": m.start_ms * TICKS_PER_MS, "endTicks": m.end_ms * TICKS_PER_MS} for m in wanted]
        self._check(self._server.put_bridge_markers(item_id, segments), item_id)

    @staticmethod
    def _check(resp, item_id: str) -> None:
        if resp.status_code in (200, 204):
            return
        body = None
        try:
            body = resp.json()
        except Exception:
            body = None
        if resp.status_code == 404:
            if isinstance(body, dict) and body.get("error") == "item not found":
                raise ItemNotFoundError(f"Jellyfin doesn't know item {item_id} (yet)")
            raise PublishError("Media Preview Bridge markers endpoint missing — update the plugin", state=Capability.NEEDS_PLUGIN)
        detail = body.get("error") if isinstance(body, dict) else ""
        raise PublishError(f"Jellyfin Bridge returned HTTP {resp.status_code} {detail}".strip())
```

```python
# media_preview_generator/markers/sources/server_markers.py
"""Markers already on a server, as agreement evidence (never a sole source)."""

from __future__ import annotations

from ...servers.base import ServerType
from ..models import Candidate, MarkerType, Source

_JF_TYPES = {"Intro": MarkerType.INTRO, "Outro": MarkerType.CREDITS, "Recap": MarkerType.RECAP, "Preview": MarkerType.PREVIEW}


def _c(mtype: MarkerType, start: int, end: int | None, origin: str) -> Candidate:
    return Candidate(mtype, int(start), None if end is None else int(end), Source.SERVER_MARKERS, origin=origin)


def read_server_markers(server, config, item_id: str) -> list[Candidate] | None:
    """Read one server's current markers for an item.

    Args:
        server: Live client.
        config: Its ``ServerConfig`` (type + id).
        item_id: The server's item id.

    Returns:
        Candidates (origin = server id), [] when the server has none, None when it couldn't be read.
    """
    origin = config.id
    if config.type is ServerType.PLEX:
        rows = server.get_markers(item_id)
        if rows is None:
            return None
        return [
            _c(MarkerType.INTRO if r["type"] == "intro" else MarkerType.CREDITS, r["start_ms"],
               None if (r["type"] == "credits" and r.get("final")) else r["end_ms"], origin)
            for r in rows
        ]
    if config.type is ServerType.JELLYFIN:
        rows = server.get_media_segments(item_id)
        if rows is None:
            return None
        ours = {(s.get("type"), s.get("startTicks"), s.get("endTicks")) for s in (server.get_bridge_markers(item_id) or [])}
        out = []
        for r in rows:
            mtype = _JF_TYPES.get(str(r.get("Type")))
            if mtype is None or (r.get("Type"), r.get("StartTicks"), r.get("EndTicks")) in ours:
                continue
            out.append(_c(mtype, int(r["StartTicks"]) // 10_000, int(r["EndTicks"]) // 10_000, origin))
        return out
    if config.type is ServerType.EMBY:
        rows = server.get_chapter_markers(item_id)
        if rows is None:
            return None
        by_type = {r["marker_type"]: r["start_ms"] for r in rows}
        out = []
        if "IntroStart" in by_type and "IntroEnd" in by_type:
            out.append(_c(MarkerType.INTRO, by_type["IntroStart"], by_type["IntroEnd"], origin))
        if "CreditsStart" in by_type:
            out.append(_c(MarkerType.CREDITS, by_type["CreditsStart"], None, origin))
        return out
    return None
```

```python
# media_preview_generator/markers/publishers/factory.py
"""Pick the publisher for a server type."""

from __future__ import annotations

from ...servers.base import ServerType
from ..settings import load_server
from .base import MarkerPublisher


def publisher_for(server, config, *, sibling_markers=None) -> MarkerPublisher | None:
    """Return the publisher for ``config.type`` with that server's markers settings, or None (Emby until phase 2)."""
    settings = load_server(config.markers, config.type.value)
    if config.type is ServerType.PLEX:
        from .plex_db import PlexMarkerPublisher

        return PlexMarkerPublisher(server, config, settings, sibling_markers=sibling_markers)
    if config.type is ServerType.JELLYFIN:
        from .jellyfin import JellyfinMarkerPublisher

        return JellyfinMarkerPublisher(server, config, settings)
    return None
```

- [ ] **Step 5: Run** new tests + vendor test files → PASS.
- [ ] **Step 6: Commit** — `feat(markers): Jellyfin Bridge publisher and readers for markers already on servers`

---
## Task 11: Pipeline (`check_item` / `process_item`) and outcomes

> **Superseded in part (2026-09-14, Task 8/11 reviews).** Plex publishing is per server item, not per file:
> `WaitingForVersionsError` no longer exists, `write()` returns the markers that are ours on the item, publishers have
> `atomic_writes`, `write()` takes `own_previous`, and `markers.db` has `item_publish_state`. The code blocks below that
> mention the old contract are historical; the binding design is `plex-item-publishing.md` in this folder.

Spec §6.2 steps 2–7, §6.4 items 3–4, design artifact "How it works per file". One function family runs in both
stages; the check stage returns None only when a registered local detector could still decide an undecided type
(none are registered in phase 1).

**Files:**
- Create: `media_preview_generator/markers/outcomes.py`, `media_preview_generator/markers/pipeline.py`
- Test: `tests/markers/test_pipeline.py`, `tests/markers/fakes.py`

**Interfaces:**
- Consumes: everything from Tasks 1, 3–8, 10; `ServerRegistry.find_owning_servers/get/get_config/configs`,
  `config.paths.is_path_excluded(local_path, exclude_paths)`, `MediaServer.resolve_remote_path_to_item_id(path)`.
- Produces:
```python
# markers/outcomes.py
class FileOutcome(str, Enum): PUBLISHED="markers_published"; UP_TO_DATE="markers_up_to_date"; WAITING="markers_waiting"
                              NEEDS_REVIEW="markers_needs_review"; SKIPPED="markers_skipped"; NO_MARKERS="markers_none"
                              NO_OWNERS="markers_no_owners"; FILE_NOT_FOUND="skipped_file_not_found"; FAILED="failed"
OUTCOME_KEYS: tuple[str, ...]
class ServerStatus(str, Enum): WRITTEN="markers_written"; UP_TO_DATE="markers_up_to_date"; NEEDS_REVIEW="markers_needs_review"
                               SKIPPED="markers_skipped"; WAITING="markers_waiting"; NONE="markers_none"; FAILED="failed"
def file_outcome(statuses: set[str], *, needs_review: bool) -> FileOutcome   # precedence across owners, see docstring
# markers/pipeline.py
NO_DATA_RETRY = timedelta(days=14)
LocalDetector = Callable[..., list[Candidate]]       # detector(file: FileRecord, *, ctx, gpu, gpu_device_path, phase_callback, cancel_check, pause_check)
@dataclass(frozen=True) class LocalDetectorSpec: source: Source; types: frozenset[MarkerType]; detect: LocalDetector
@dataclass class PipelineContext:
    registry: ServerRegistry; config: Config; settings: GlobalMarkersSettings; store: MarkerStore; priority: int
    ffprobe: str; force: bool = False
    clients: dict[str, object] = field(default_factory=dict)            # source id → client with .lookup(); built by build_clients()
    local_detectors: tuple[LocalDetectorSpec, ...] = ()
    now: Callable[[], datetime] = <utc now>
    capability_ttl_s: float = 300.0
def build_clients(settings: GlobalMarkersSettings) -> dict[str, object]
def build_context(*, registry, config, priority: int, force: bool = False) -> PipelineContext
def check_item(item: ProcessableItem, *, ctx: PipelineContext, cancel_check=None) -> ItemOutcome | None
def process_item(item: ProcessableItem, *, ctx: PipelineContext, gpu=None, gpu_device_path=None, progress_callback=None,
                 phase_callback=None, cancel_check=None, pause_check=None) -> ItemOutcome
def kind_handlers(ctx: PipelineContext) -> KindHandlers      # check_label "Looking up markers…", check_worker_label "Intro & Credits"
def markers_for_path(store: MarkerStore, canonical_path: str) -> dict[MarkerType, Marker] | None   # None = never decided
```

- [ ] **Step 1: Write the test fakes**

```python
# tests/markers/fakes.py
"""Small fakes for pipeline tests: registry, servers, publishers, online clients."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import MagicMock

from media_preview_generator.markers.publishers.base import Capability, CapabilityReport
from media_preview_generator.markers.sources.online import LookupResult
from media_preview_generator.servers.base import Library, ServerConfig, ServerType
from media_preview_generator.servers.ownership import OwnershipMatch


def server_config(sid, stype, *, enabled=True, markers=None, libraries=None, exclude_paths=None):
    if markers is None:
        markers = {"enabled": True, "library_ids": None}
        if stype is ServerType.PLEX:
            markers["plex"] = {"db_write_confirmed_at": "2026-09-13T00:00:00+00:00", "on_plex_redetect": "restore"}
    return ServerConfig(id=sid, type=stype, name=sid.upper(), enabled=enabled, url=f"http://{sid}", auth={},
                        libraries=libraries or [Library("1", "TV Shows", ("/media/tv",))], markers=markers,
                        exclude_paths=exclude_paths or [])


@dataclass
class FakeRegistry:
    configs_by_id: dict[str, ServerConfig]
    servers_by_id: dict[str, MagicMock] = field(default_factory=dict)
    owners: dict[str, list[str]] = field(default_factory=dict)     # path → server ids
    library_for: dict[str, tuple[str, str]] = field(default_factory=dict)  # server id → (library id, name)

    def configs(self):
        return list(self.configs_by_id.values())

    def get_config(self, sid):
        return self.configs_by_id.get(sid)

    def get(self, sid):
        if sid not in self.servers_by_id:
            server = MagicMock(name=f"server-{sid}")
            server.id = sid
            server.resolve_remote_path_to_item_id.return_value = f"item-{sid}"
            server.get_external_ids.return_value = None
            self.servers_by_id[sid] = server
        return self.servers_by_id[sid]

    def find_owning_servers(self, path):
        out = []
        for sid in self.owners.get(path, []):
            lib_id, lib_name = self.library_for.get(sid, ("1", "TV Shows"))
            out.append(OwnershipMatch(sid, lib_id, lib_name, "/media/tv"))
        return out


class FakeClient:
    def __init__(self, result: LookupResult):
        self.result = result
        self.calls = []

    def lookup(self, ids, *, duration_ms, priority, cancel_check=None):
        self.calls.append({"ids": ids, "duration_ms": duration_ms, "priority": priority, "cancel_check": cancel_check})
        return self.result


def ready_publisher(name="plex_db", types=("intro", "credits")):
    from media_preview_generator.markers.models import MarkerType
    from media_preview_generator.markers.publishers.base import MarkerPublisher

    pub = MagicMock(spec=MarkerPublisher)
    pub.name = name
    pub.supported_types = frozenset(MarkerType(t) for t in types)
    pub.capability.return_value = CapabilityReport(Capability.READY, "ok")
    pub.project.side_effect = lambda ms: sorted((m for m in ms if m.type in pub.supported_types), key=lambda m: m.start_ms)
    pub.read.return_value = []
    return pub
```

- [ ] **Step 2: Write failing pipeline tests**

```python
# tests/markers/test_pipeline.py
"""Pipeline matrix: owners, identity, evidence order + early stop, decisions, publish fan-out, outcomes."""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.markers import pipeline
from media_preview_generator.markers.models import Candidate, FileIdentity, Marker, MarkerType, Source
from media_preview_generator.markers.outcomes import FileOutcome, ServerStatus, file_outcome
from media_preview_generator.markers.pipeline import LocalDetectorSpec, PipelineContext, check_item, process_item
from media_preview_generator.markers.probe import Chapter, MediaProbe, ProbeError
from media_preview_generator.markers.publishers.base import (
    Capability, CapabilityReport, ItemNotFoundError, PublishError, WaitingForVersionsError,
)
from media_preview_generator.markers.settings import load_global
from media_preview_generator.markers.sources.online import LookupResult
from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.processing.types import ProcessableItem
from media_preview_generator.servers.base import Library, ServerType
from tests.markers.fakes import FakeClient, FakeRegistry, ready_publisher, server_config

T = MarkerType
DUR = 1_321_472


@pytest.fixture
def media(tmp_path):
    folder = tmp_path / "media" / "tv" / "Rick and Morty (2013) {tvdb-275274}" / "Season 01"
    folder.mkdir(parents=True)
    f = folder / "Rick and Morty (2013) - S01E01 - Pilot.mkv"
    f.write_bytes(b"x" * 100)
    return str(f)


@pytest.fixture
def store(tmp_path):
    s = MarkerStore(str(tmp_path / "markers.db"))
    yield s
    s.close()


def _probe(chapters=()):
    return MediaProbe(DUR, tuple(chapters))


CHAPTERS_BOTH = (Chapter(0, 126_771, "Chapter 1"), Chapter(126_771, 157_068, "Intro"),
                 Chapter(157_068, 1_295_324, "Chapter 2"), Chapter(1_295_324, None, "Credits"))


def _ctx(store, registry, *, settings_raw=None, clients=None, detectors=(), force=False, now=None):
    raw = settings_raw or {"sources": [{"id": "theintrodb", "enabled": True}]}
    from media_preview_generator.markers.settings import validate_global

    settings = load_global(validate_global(raw, None)[0])
    return PipelineContext(
        registry=registry, config=MagicMock(), settings=settings, store=store, priority=2, ffprobe="ffprobe",
        force=force,
        clients=clients if clients is not None else {
            "theintrodb": FakeClient(LookupResult("no_data")),
            "introdb": FakeClient(LookupResult("no_data")),
            "skipdb": FakeClient(LookupResult("no_data")),
        },
        local_detectors=detectors,
        now=now or (lambda: datetime(2026, 9, 13, tzinfo=timezone.utc)),
    )


def _registry(media, *server_types):
    configs = {f"{t.value}-1": server_config(f"{t.value}-1", t) for t in server_types}
    return FakeRegistry(configs, owners={media: list(configs)})


def _item(path, hints=None):
    return ProcessableItem(canonical_path=path, server_id="plex-1", item_id_by_server=hints or {}, title="R&M S01E01")


def _run(ctx, media, publishers, probe=None, stage="check"):
    with patch.object(pipeline, "probe_media", return_value=probe or _probe()) as probe_mock, \
         patch.object(pipeline, "publisher_for", side_effect=lambda server, cfg, **kw: publishers.get(cfg.id)):
        fn = check_item if stage == "check" else process_item
        out = fn(_item(media), ctx=ctx)
    return out, probe_mock


class TestOwners:
    @pytest.mark.parametrize(
        "mutate",
        [
            lambda reg, media: reg.owners.update({media: []}),
            lambda reg, media: setattr(reg.configs_by_id["plex-1"], "enabled", False),
            lambda reg, media: reg.configs_by_id["plex-1"].markers.update({"enabled": False}),
            lambda reg, media: reg.configs_by_id["plex-1"].markers.update({"plex": {"db_write_confirmed_at": None}}),
            lambda reg, media: reg.library_for.update({"plex-1": ("9", "Sports")}),
            lambda reg, media: reg.configs_by_id["plex-1"].markers.update({"library_ids": ["2"]}),
            lambda reg, media: reg.configs_by_id["plex-1"].exclude_paths.append({"value": os.path.dirname(media), "type": "path"}),
        ],
        ids=["no-owner", "server-disabled", "markers-off", "plex-unconfirmed", "sports-default", "library-not-selected", "excluded"],
    )
    def test_no_marker_owner_cells(self, store, media, mutate):
        reg = _registry(media, ServerType.PLEX)
        mutate(reg, media)
        out, probe = _run(_ctx(store, reg), media, {"plex-1": ready_publisher()})
        assert out.outcome_key == FileOutcome.NO_OWNERS.value
        probe.assert_not_called()

    def test_owner_without_a_publisher_is_skipped_not_failed(self, store, media):
        # Emby has no publisher until phase 2: the owner counts, its row says why nothing was written.
        reg = _registry(media, ServerType.EMBY)
        out, _ = _run(_ctx(store, reg), media, {}, probe=_probe(CHAPTERS_BOTH))
        assert [r["status"] for r in out.publisher_rows] == [ServerStatus.SKIPPED.value]
        assert out.outcome_key == FileOutcome.SKIPPED.value
        assert store.get_publish_state(store.get_file(media).id, "emby-1").status == "skipped"


class TestIdentityAndProbe:
    def test_missing_file(self, store, tmp_path):
        path = str(tmp_path / "gone.mkv")
        reg = FakeRegistry({"plex-1": server_config("plex-1", ServerType.PLEX)}, owners={path: ["plex-1"]})
        out, _ = _run(_ctx(store, reg), path, {"plex-1": ready_publisher()})
        assert out.outcome_key == FileOutcome.FILE_NOT_FOUND.value

    def test_probe_error_fails_item(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        with patch.object(pipeline, "probe_media", side_effect=ProbeError("bad file")), \
             patch.object(pipeline, "publisher_for", return_value=ready_publisher()):
            out = check_item(_item(media), ctx=_ctx(store, reg))
        assert out.outcome_key == "failed" and "bad file" in out.message

    def test_unchanged_file_is_not_probed_twice_and_changed_file_is(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        pubs = {"plex-1": ready_publisher()}
        ctx = _ctx(store, reg)
        _, probe1 = _run(ctx, media, pubs, probe=_probe(CHAPTERS_BOTH))
        _, probe2 = _run(ctx, media, pubs, probe=_probe(CHAPTERS_BOTH))
        assert probe1.call_count == 1 and probe2.call_count == 0
        os.utime(media, ns=(1, 2))
        _, probe3 = _run(ctx, media, pubs, probe=_probe(CHAPTERS_BOTH))
        assert probe3.call_count == 1


class TestEvidenceAndDecisions:
    def test_chapters_decide_both_and_skip_online_lookups(self, store, media):
        reg = _registry(media, ServerType.PLEX, ServerType.JELLYFIN)
        plex, jf = ready_publisher(), ready_publisher("jellyfin_bridge", ("intro", "credits", "recap", "preview"))
        ctx = _ctx(store, reg)
        out, _ = _run(ctx, media, {"plex-1": plex, "jellyfin-1": jf}, probe=_probe(CHAPTERS_BOTH))
        assert out.outcome_key == FileOutcome.PUBLISHED.value
        assert all(c.calls == [] for c in ctx.clients.values())
        expected = [Marker(T.INTRO, 126_771, 157_068, ("chapters",)), Marker(T.CREDITS, 1_295_324, DUR, ("chapters",))]
        for pub, sid in ((plex, "plex-1"), (jf, "jellyfin-1")):
            args, kwargs = pub.write.call_args
            assert args == (f"item-{sid}", expected)
            assert kwargs == {"previous": [], "duration_ms": DUR, "canonical_path": media}
        assert {r["server_id"]: r["status"] for r in out.publisher_rows} == {
            "plex-1": ServerStatus.WRITTEN.value, "jellyfin-1": ServerStatus.WRITTEN.value}
        assert out.publisher_rows[0]["canonical_path"] == media and out.publisher_rows[0]["adapter_name"] in ("plex_db", "jellyfin_bridge")

    def test_second_run_is_up_to_date_without_writes(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        ctx = _ctx(store, reg)
        _run(ctx, media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
        plex.write.reset_mock()
        out, _ = _run(ctx, media, {"plex-1": plex})
        plex.write.assert_not_called()
        assert out.outcome_key == FileOutcome.UP_TO_DATE.value
        assert out.publisher_rows[0]["status"] == ServerStatus.UP_TO_DATE.value

    def test_changed_file_republishes_with_previous_markers(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        ctx = _ctx(store, reg)
        _run(ctx, media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
        os.utime(media, ns=(5, 6))
        new_chapters = (Chapter(0, 10_000, "Chapter 1"), Chapter(10_000, 40_000, "Intro"), Chapter(40_000, None, "Chapter 2"))
        _run(ctx, media, {"plex-1": plex}, probe=_probe(new_chapters))
        args, kwargs = plex.write.call_args
        assert args[1] == [Marker(T.INTRO, 10_000, 40_000, ("chapters",))]
        assert [m.type for m in kwargs["previous"]] == [T.INTRO, T.CREDITS]

    def test_online_order_early_stop_and_dependent_sources(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        intro_tidb = LookupResult("ok", (Candidate(T.INTRO, 127_894, 156_824, Source.THEINTRODB),))
        intro_idb = LookupResult("ok", (Candidate(T.INTRO, 128_000, 160_000, Source.INTRODB),))
        intro_skip = LookupResult("ok", (Candidate(T.INTRO, 129_000, 157_800, Source.SKIPDB),
                                         Candidate(T.CREDITS, 1_296_000, 1_320_000, Source.SKIPDB)))
        clients = {"theintrodb": FakeClient(intro_tidb), "introdb": FakeClient(intro_idb), "skipdb": FakeClient(intro_skip)}
        raw = {"sources": [{"id": "theintrodb", "enabled": True}], "detect": {"intro": True, "credits": False}}
        ctx = _ctx(store, reg, clients=clients, settings_raw=raw)
        plex = ready_publisher()
        out, _ = _run(ctx, media, {"plex-1": plex})
        assert [len(c.calls) for c in clients.values()] == [1, 1, 1]
        call = clients["theintrodb"].calls[0]
        assert call["ids"].tvdb == "275274" and call["ids"].season == 1 and call["ids"].episode == 1
        assert call["duration_ms"] == DUR and call["priority"] == 2
        assert plex.write.call_args.args[1] == [Marker(T.INTRO, 127_894, 156_824, ("theintrodb", "introdb", "skipdb"))]
        assert out.outcome_key == FileOutcome.PUBLISHED.value

    def test_stops_querying_once_everything_is_decided(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        both = LookupResult("ok", (Candidate(T.INTRO, 127_894, 156_824, Source.THEINTRODB),))
        clients = {"theintrodb": FakeClient(both), "introdb": FakeClient(LookupResult("no_data")),
                   "skipdb": FakeClient(LookupResult("no_data"))}
        raw = {"sources": [{"id": "theintrodb", "enabled": True}], "detect": {"intro": True, "credits": False},
               "publish_when": "medium"}
        _run(_ctx(store, reg, clients=clients, settings_raw=raw), media, {"plex-1": ready_publisher()})
        assert [len(c.calls) for c in clients.values()] == [1, 0, 0]

    def test_single_source_at_high_needs_review_and_writes_nothing(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        clients = {"theintrodb": FakeClient(LookupResult("ok", (Candidate(T.INTRO, 127_894, 156_824, Source.THEINTRODB),))),
                   "introdb": FakeClient(LookupResult("no_data")), "skipdb": FakeClient(LookupResult("no_data"))}
        raw = {"sources": [{"id": "theintrodb", "enabled": True}], "detect": {"intro": True, "credits": False}}
        plex = ready_publisher()
        out, _ = _run(_ctx(store, reg, clients=clients, settings_raw=raw), media, {"plex-1": plex})
        assert out.outcome_key == FileOutcome.NEEDS_REVIEW.value
        plex.write.assert_not_called()
        assert out.publisher_rows[0]["status"] == ServerStatus.NEEDS_REVIEW.value

    def test_disabled_source_is_not_queried_and_its_stored_evidence_is_ignored(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        st = os.stat(media)
        rec = store.upsert_file(FileIdentity(media, st.st_size, st.st_mtime_ns), duration_ms=DUR,
                                season_key=os.path.dirname(media), is_movie=False)
        store.replace_evidence(rec.id, Source.SKIPDB, [Candidate(T.INTRO, 127_000, 157_000, Source.SKIPDB)])
        clients = {"theintrodb": FakeClient(LookupResult("ok", (Candidate(T.INTRO, 127_894, 156_824, Source.THEINTRODB),))),
                   "introdb": FakeClient(LookupResult("no_data")), "skipdb": FakeClient(LookupResult("no_data"))}
        raw = {"sources": [{"id": "theintrodb", "enabled": True}, {"id": "skipdb", "enabled": False}],
               "detect": {"intro": True, "credits": False}}
        plex = ready_publisher()
        out, _ = _run(_ctx(store, reg, clients=clients, settings_raw=raw), media, {"plex-1": plex})
        assert clients["skipdb"].calls == []
        assert out.outcome_key == FileOutcome.NEEDS_REVIEW.value  # skipdb's stored agreement doesn't count

    @pytest.mark.parametrize(
        ("first", "days_later", "force", "queried_again"),
        [
            (LookupResult("unavailable", detail="blocked"), 0, False, True),
            (LookupResult("not_applicable"), 0, False, True),
            (LookupResult("no_data"), 1, False, False),
            (LookupResult("no_data"), 15, False, True),
            (LookupResult("no_data"), 1, True, True),
            (LookupResult("ok", (Candidate(T.INTRO, 127_894, 156_824, Source.THEINTRODB),)), 30, False, False),
        ],
    )
    def test_lookup_caching_matrix(self, tmp_path, media, first, days_later, force, queried_again):
        clock = {"t": datetime(2026, 9, 13, tzinfo=timezone.utc)}
        store = MarkerStore(str(tmp_path / "clocked.db"), clock=lambda: clock["t"])
        reg = _registry(media, ServerType.PLEX)
        client = FakeClient(first)
        clients = {"theintrodb": client, "introdb": FakeClient(LookupResult("no_data")), "skipdb": FakeClient(LookupResult("no_data"))}
        raw = {"sources": [{"id": "theintrodb", "enabled": True}], "detect": {"intro": True, "credits": False}}
        _run(_ctx(store, reg, clients=clients, settings_raw=raw, now=lambda: clock["t"]), media, {"plex-1": ready_publisher()})
        clock["t"] += timedelta(days=days_later)
        _run(_ctx(store, reg, clients=clients, settings_raw=raw, now=lambda: clock["t"], force=force), media,
             {"plex-1": ready_publisher()})
        assert len(client.calls) == (2 if queried_again else 1)
        store.close()

    def test_server_markers_confirm_online_source_and_are_read_once_before_publishing(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        plex_server = reg.get("plex-1")
        clients = {"theintrodb": FakeClient(LookupResult("ok", (Candidate(T.INTRO, 127_894, 156_824, Source.THEINTRODB),))),
                   "introdb": FakeClient(LookupResult("no_data")), "skipdb": FakeClient(LookupResult("no_data"))}
        raw = {"sources": [{"id": "theintrodb", "enabled": True}], "detect": {"intro": True, "credits": False}}
        reader = MagicMock(return_value=[Candidate(T.INTRO, 127_000, 158_000, Source.SERVER_MARKERS, origin="plex-1")])
        plex = ready_publisher()
        with patch.object(pipeline, "read_server_markers", reader):
            out, _ = _run(_ctx(store, reg, clients=clients, settings_raw=raw), media, {"plex-1": plex})
            assert out.outcome_key == FileOutcome.PUBLISHED.value
            reader.assert_called_once_with(plex_server, reg.get_config("plex-1"), "item-plex-1")
            _run(_ctx(store, reg, clients=clients, settings_raw=raw, force=True), media, {"plex-1": plex})
            assert reader.call_count == 1  # never re-read a server we've published to

    def test_server_markers_read_from_owner_without_markers_enabled(self, store, media):
        reg = _registry(media, ServerType.PLEX, ServerType.JELLYFIN)
        reg.configs_by_id["plex-1"].markers.update({"enabled": False})
        reader = MagicMock(return_value=[])
        with patch.object(pipeline, "read_server_markers", reader):
            _run(_ctx(store, reg), media, {"jellyfin-1": ready_publisher("jellyfin_bridge")})
        assert {c.args[1].id for c in reader.call_args_list} == {"plex-1", "jellyfin-1"}

    def test_movie_detects_credits_only(self, store, tmp_path):
        folder = tmp_path / "media" / "movies" / "Toy Story (1995) {tmdb-862}"
        folder.mkdir(parents=True)
        path = folder / "Toy Story (1995) {imdb-tt0114709}.mkv"
        path.write_bytes(b"x")
        reg = FakeRegistry({"plex-1": server_config("plex-1", ServerType.PLEX)}, owners={str(path): ["plex-1"]})
        chapters = (Chapter(0, 60_000, "Intro"), Chapter(60_000, 4_629_000, "Movie"), Chapter(4_629_000, None, "End Credits"))
        plex = ready_publisher()
        with patch.object(pipeline, "probe_media", return_value=MediaProbe(4_866_050, chapters)), \
             patch.object(pipeline, "publisher_for", return_value=plex):
            check_item(_item(str(path)), ctx=_ctx(store, reg))
        assert plex.write.call_args.args[1] == [Marker(T.CREDITS, 4_629_000, 4_866_050, ("chapters",))]

    def test_path_without_ids_uses_server_metadata(self, store, tmp_path):
        folder = tmp_path / "media" / "tv" / "Some Show" / "Season 02"
        folder.mkdir(parents=True)
        path = folder / "Some Show - S02E05.mkv"
        path.write_bytes(b"x")
        reg = FakeRegistry({"plex-1": server_config("plex-1", ServerType.PLEX)}, owners={str(path): ["plex-1"]})
        reg.get("plex-1").get_external_ids.return_value = {"kind": "episode", "tmdb": "1", "imdb": "tt1", "tvdb": None, "season": 2, "episode": 5}
        client = FakeClient(LookupResult("no_data"))
        clients = {"theintrodb": client, "introdb": FakeClient(LookupResult("no_data")), "skipdb": FakeClient(LookupResult("no_data"))}
        with patch.object(pipeline, "probe_media", return_value=_probe()), patch.object(pipeline, "publisher_for", return_value=ready_publisher()):
            check_item(_item(str(path)), ctx=_ctx(store, reg, clients=clients))
        reg.get("plex-1").get_external_ids.assert_called_once_with("item-plex-1")
        assert client.calls[0]["ids"].tmdb == "1"


class TestPublishFanOut:
    def _decided(self, store, media, reg, pubs):
        return _run(_ctx(store, reg), media, pubs, probe=_probe(CHAPTERS_BOTH))[0]

    @pytest.mark.parametrize(
        ("setup", "status"),
        [
            (lambda pub, server: setattr(pub.capability, "return_value", CapabilityReport(Capability.NEEDS_PLUGIN, "Install the plugin")), ServerStatus.SKIPPED),
            (lambda pub, server: setattr(server.resolve_remote_path_to_item_id, "return_value", None), ServerStatus.WAITING),
            (lambda pub, server: setattr(pub.write, "side_effect", ItemNotFoundError("not indexed")), ServerStatus.WAITING),
            (lambda pub, server: setattr(pub.write, "side_effect", WaitingForVersionsError("versions")), ServerStatus.WAITING),
            (lambda pub, server: setattr(pub.write, "side_effect", PublishError("boom")), ServerStatus.FAILED),
            (lambda pub, server: setattr(pub.write, "side_effect", RuntimeError("bug")), ServerStatus.FAILED),
        ],
        ids=["capability", "no-item-id", "item-not-found", "versions", "publish-error", "unexpected"],
    )
    def test_one_server_problem_does_not_block_the_other(self, store, media, setup, status):
        reg = _registry(media, ServerType.PLEX, ServerType.JELLYFIN)
        plex, jf = ready_publisher(), ready_publisher("jellyfin_bridge")
        setup(plex, reg.get("plex-1"))
        out = self._decided(store, media, reg, {"plex-1": plex, "jellyfin-1": jf})
        rows = {r["server_id"]: r for r in out.publisher_rows}
        assert rows["plex-1"]["status"] == status.value and rows["plex-1"]["message"]
        assert rows["jellyfin-1"]["status"] == ServerStatus.WRITTEN.value
        assert out.outcome_key == FileOutcome.PUBLISHED.value
        state = store.get_publish_state(store.get_file(media).id, "plex-1")
        assert state.status in ("skipped", "waiting", "failed") and state.markers == ()

    def test_nothing_found_is_markers_none_and_writes_nothing(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        out, _ = _run(_ctx(store, reg), media, {"plex-1": plex}, probe=_probe())
        plex.write.assert_not_called()
        assert [r["status"] for r in out.publisher_rows] == [ServerStatus.NONE.value]
        assert out.outcome_key == FileOutcome.NO_MARKERS.value

    def test_item_not_in_server_yet_is_markers_waiting(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        reg.get("plex-1").resolve_remote_path_to_item_id.return_value = None
        plex = ready_publisher()
        out, _ = _run(_ctx(store, reg), media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
        plex.write.assert_not_called()
        assert [r["status"] for r in out.publisher_rows] == [ServerStatus.WAITING.value]
        assert out.outcome_key == FileOutcome.WAITING.value
        assert store.get_publish_state(store.get_file(media).id, "plex-1").status == "waiting"

    def test_hint_item_id_is_used_without_lookup(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        with patch.object(pipeline, "probe_media", return_value=_probe(CHAPTERS_BOTH)), \
             patch.object(pipeline, "publisher_for", return_value=plex):
            check_item(_item(media, hints={"plex-1": "4242"}), ctx=_ctx(store, reg))
        assert plex.write.call_args.args[0] == "4242"
        reg.get("plex-1").resolve_remote_path_to_item_id.assert_not_called()

    def test_all_servers_failed_is_failed(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        plex.write.side_effect = PublishError("boom")
        out = self._decided(store, media, reg, {"plex-1": plex})
        assert out.outcome_key == FileOutcome.FAILED.value

    def test_capability_is_cached_per_context(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        ctx = _ctx(store, reg)
        _run(ctx, media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
        os.utime(media, ns=(9, 9))
        _run(ctx, media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
        assert plex.capability.call_count == 1

    def test_capability_is_fetched_once_when_many_checks_start_together(self, store, media):
        # 32 checking threads hit an empty cache at job start (and at every TTL expiry); Plex's check reads prefs
        # over HTTP and opens its DB, so it must run once per server, not once per thread.
        reg = _registry(media, ServerType.PLEX)
        ctx = _ctx(store, reg)
        cfg = reg.get_config("plex-1")
        plex = ready_publisher()

        def slow_capability():
            time.sleep(0.05)
            return CapabilityReport(Capability.READY, "ok")

        plex.capability.side_effect = slow_capability
        barrier = threading.Barrier(16)
        reports = []

        def check():
            barrier.wait()
            reports.append(pipeline._capability(ctx, cfg, plex))

        threads = [threading.Thread(target=check) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert plex.capability.call_count == 1
        assert len(reports) == 16 and all(r.ready for r in reports)

    def test_publisher_gets_a_sibling_lookup_backed_by_the_store(self, store, media):
        # Without this lookup every multi-version Plex item waits forever (WaitingForVersionsError).
        reg = _registry(media, ServerType.PLEX)
        with patch.object(pipeline, "probe_media", return_value=_probe(CHAPTERS_BOTH)), \
             patch.object(pipeline, "publisher_for", return_value=ready_publisher()) as factory:
            check_item(_item(media), ctx=_ctx(store, reg))
        assert factory.call_args.args == (reg.get("plex-1"), reg.get_config("plex-1"))
        lookup = factory.call_args.kwargs["sibling_markers"]
        assert lookup(media) == {T.INTRO: Marker(T.INTRO, 126_771, 157_068, ("chapters",)),
                                 T.CREDITS: Marker(T.CREDITS, 1_295_324, DUR, ("chapters",))}
        assert lookup("/elsewhere/never-decided.mkv") is None

    def test_locked_marker_is_published_even_when_detection_is_off(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        raw = {"detect": {"intro": False, "credits": False}}
        st = os.stat(media)
        rec = store.upsert_file(FileIdentity(media, st.st_size, st.st_mtime_ns), duration_ms=DUR, season_key=None, is_movie=False)
        store.lock_marker(rec.id, Marker(T.INTRO, 5_000, 30_000, ("user",), locked=True))
        _run(_ctx(store, reg, settings_raw=raw), media, {"plex-1": plex})
        assert plex.write.call_args.args[1] == [Marker(T.INTRO, 5_000, 30_000, ("user",), locked=True)]


class TestStages:
    def test_check_defers_to_worker_when_a_local_detector_can_help(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        detector = MagicMock(return_value=[])
        spec = LocalDetectorSpec(Source.SEASON_AUDIO, frozenset({T.INTRO}), detector)
        ctx = _ctx(store, reg, detectors=(spec,))
        out, _ = _run(ctx, media, {"plex-1": ready_publisher()})
        assert out is None
        detector.assert_not_called()

    def test_process_forwards_worker_gpu_and_callbacks_to_detectors_and_lookups(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        detected = [Candidate(T.INTRO, 126_000, 158_000, Source.SEASON_AUDIO)]
        detector = MagicMock(return_value=detected)
        spec = LocalDetectorSpec(Source.SEASON_AUDIO, frozenset({T.INTRO}), detector)
        clients = {"theintrodb": FakeClient(LookupResult("ok", (Candidate(T.INTRO, 127_894, 156_824, Source.THEINTRODB),))),
                   "introdb": FakeClient(LookupResult("no_data")), "skipdb": FakeClient(LookupResult("no_data"))}
        raw = {"sources": [{"id": "theintrodb", "enabled": True}], "detect": {"intro": True, "credits": False}}
        ctx = _ctx(store, reg, detectors=(spec,), clients=clients, settings_raw=raw)
        plex = ready_publisher()
        cancel = MagicMock(return_value=False)
        pause = MagicMock(return_value=False)
        phase = MagicMock()
        with patch.object(pipeline, "probe_media", return_value=_probe()), patch.object(pipeline, "publisher_for", return_value=plex):
            out = process_item(_item(media), ctx=ctx, gpu="NVIDIA", gpu_device_path="cuda:0",
                               cancel_check=cancel, pause_check=pause, phase_callback=phase)
        kwargs = detector.call_args.kwargs
        assert kwargs["gpu"] == "NVIDIA" and kwargs["gpu_device_path"] == "cuda:0"
        assert kwargs["cancel_check"] is cancel and kwargs["pause_check"] is pause and kwargs["phase_callback"] is phase
        assert clients["theintrodb"].calls[0]["cancel_check"] is cancel
        assert out.outcome_key == FileOutcome.PUBLISHED.value

    def test_process_never_returns_none(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        spec = LocalDetectorSpec(Source.SEASON_AUDIO, frozenset({T.INTRO}), MagicMock(return_value=[]))
        out, _ = _run(_ctx(store, reg, detectors=(spec,)), media, {"plex-1": ready_publisher()}, stage="process")
        assert out is not None

    def test_cancel_before_work(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        with patch.object(pipeline, "probe_media") as probe:
            out = check_item(_item(media), ctx=_ctx(store, reg), cancel_check=lambda: True)
        assert out.outcome_key == "failed" and "cancel" in out.message
        probe.assert_not_called()


def test_markers_for_path(store, media):
    assert pipeline.markers_for_path(store, media) is None


W, U, R, S, A, F, N = (ServerStatus.WRITTEN, ServerStatus.UP_TO_DATE, ServerStatus.NEEDS_REVIEW, ServerStatus.SKIPPED,
                       ServerStatus.WAITING, ServerStatus.FAILED, ServerStatus.NONE)


@pytest.mark.parametrize(
    ("statuses", "needs_review", "expected"),
    [
        # Every pair of per-server statuses for two owners (NEEDS_REVIEW rows only exist when needs_review is True).
        ({W}, False, FileOutcome.PUBLISHED), ({W, U}, False, FileOutcome.PUBLISHED),
        ({W, S}, False, FileOutcome.PUBLISHED), ({W, A}, False, FileOutcome.PUBLISHED),
        ({W, F}, False, FileOutcome.PUBLISHED), ({W, N}, False, FileOutcome.PUBLISHED),
        ({U}, False, FileOutcome.UP_TO_DATE), ({U, S}, False, FileOutcome.UP_TO_DATE),
        ({U, A}, False, FileOutcome.UP_TO_DATE), ({U, F}, False, FileOutcome.FAILED),
        ({U, N}, False, FileOutcome.UP_TO_DATE),
        ({S}, False, FileOutcome.SKIPPED), ({S, A}, False, FileOutcome.WAITING),
        ({S, F}, False, FileOutcome.FAILED), ({S, N}, False, FileOutcome.NO_MARKERS),
        ({A}, False, FileOutcome.WAITING), ({A, F}, False, FileOutcome.FAILED), ({A, N}, False, FileOutcome.WAITING),
        ({F}, False, FileOutcome.FAILED), ({F, N}, False, FileOutcome.FAILED),
        ({N}, False, FileOutcome.NO_MARKERS),
        ({R}, True, FileOutcome.NEEDS_REVIEW), ({R, W}, True, FileOutcome.PUBLISHED),
        ({R, U}, True, FileOutcome.UP_TO_DATE), ({R, S}, True, FileOutcome.NEEDS_REVIEW),
        ({R, A}, True, FileOutcome.WAITING), ({R, F}, True, FileOutcome.FAILED),
        ({R, N}, True, FileOutcome.NEEDS_REVIEW), ({S}, True, FileOutcome.NEEDS_REVIEW),
        ({N}, True, FileOutcome.NEEDS_REVIEW), ({A}, True, FileOutcome.WAITING),
    ],
)
def test_file_outcome_precedence(statuses, needs_review, expected):
    assert file_outcome({s.value for s in statuses}, needs_review=needs_review) is expected
```

- [ ] **Step 3: Run** → FAIL.

- [ ] **Step 4: Implement `outcomes.py`**

```python
# media_preview_generator/markers/outcomes.py
"""Per-file outcomes (job counters, Files panel) and per-server row statuses for Intro & Credits jobs."""

from __future__ import annotations

from enum import Enum


class FileOutcome(str, Enum):
    """What happened to one file."""

    PUBLISHED = "markers_published"
    UP_TO_DATE = "markers_up_to_date"
    WAITING = "markers_waiting"
    NEEDS_REVIEW = "markers_needs_review"
    SKIPPED = "markers_skipped"
    NO_MARKERS = "markers_none"
    NO_OWNERS = "markers_no_owners"
    FILE_NOT_FOUND = "skipped_file_not_found"
    FAILED = "failed"


OUTCOME_KEYS: tuple[str, ...] = tuple(o.value for o in FileOutcome)


class ServerStatus(str, Enum):
    """What happened on one server for one file."""

    WRITTEN = "markers_written"
    UP_TO_DATE = "markers_up_to_date"
    NEEDS_REVIEW = "markers_needs_review"
    SKIPPED = "markers_skipped"
    WAITING = "markers_waiting"
    NONE = "markers_none"
    FAILED = "failed"


def file_outcome(statuses: set[str], *, needs_review: bool) -> FileOutcome:
    """Fold one file's per-server statuses into its job outcome.

    Precedence, first match wins: any server written → published; any server failed → failed (so a broken write is
    never hidden behind another server's up-to-date row); any up to date → up to date; any waiting for the server to
    index the file → waiting; sources disagree → needs review; any server with nothing to publish → no markers;
    every server skipped (no publisher, plugin missing) → skipped.

    Args:
        statuses: ``ServerStatus`` values of the file's rows.
        needs_review: Whether any enabled marker type is waiting for agreement.

    Returns:
        The file outcome counted on the job.
    """
    for status, outcome in (
        (ServerStatus.WRITTEN, FileOutcome.PUBLISHED),
        (ServerStatus.FAILED, FileOutcome.FAILED),
        (ServerStatus.UP_TO_DATE, FileOutcome.UP_TO_DATE),
        (ServerStatus.WAITING, FileOutcome.WAITING),
    ):
        if status.value in statuses:
            return outcome
    if needs_review:
        return FileOutcome.NEEDS_REVIEW
    if ServerStatus.NONE.value in statuses or not statuses:
        return FileOutcome.NO_MARKERS
    return FileOutcome.SKIPPED


# publish_state.status values persisted per (file, server)
STATE_BY_STATUS = {
    ServerStatus.WRITTEN: "written",
    ServerStatus.UP_TO_DATE: "written",
    ServerStatus.SKIPPED: "skipped",
    ServerStatus.WAITING: "waiting",
    ServerStatus.FAILED: "failed",
    ServerStatus.NEEDS_REVIEW: "needs_review",
}
```

- [ ] **Step 5: Implement `pipeline.py`**

```python
# media_preview_generator/markers/pipeline.py
"""Intro & Credits per-file pipeline (spec §6.2).

owners → identity → evidence in the user's source order (stop once every enabled type is decided) → decide → store →
publish to each owner. ``check_item`` runs on the dispatcher's checking threads (no worker slot); it returns None only
when a registered local detector (season audio, credit text) could still decide something, which sends the item to a
GPU/CPU worker where ``process_item`` runs the same steps plus the detectors.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from loguru import logger

from ..config.paths import is_path_excluded
from ..job_kinds import ItemOutcome, KindHandlers
from .decide import DecisionContext, DecisionStatus, TypeDecision, decide
from .external_ids import ids_from_path, ids_from_server_dict, merge_ids
from .models import Candidate, FileIdentity, Marker, MarkerType, MediaIds, Source
from .outcomes import OUTCOME_KEYS, STATE_BY_STATUS, FileOutcome, ServerStatus, file_outcome
from .probe import ProbeError, ffprobe_path_for, probe_media
from .publishers.base import CapabilityReport, ItemNotFoundError, PublishError, WaitingForVersionsError
from .publishers.factory import publisher_for
from .settings import GlobalMarkersSettings, ServerMarkersSettings, get_global_settings, library_allowed, load_server
from .sources.chapters import chapter_candidates
from .sources.server_markers import read_server_markers
from .store import FileRecord, MarkerStore, get_marker_store

NO_DATA_RETRY = timedelta(days=14)
_ONLINE_SOURCES = ("theintrodb", "introdb", "skipdb")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class LocalDetectorSpec:
    """A detector that needs a worker slot (phase 2: season audio; phase 3: credit text)."""

    source: Source
    types: frozenset[MarkerType]
    detect: Callable[..., list[Candidate]]


@dataclass
class PipelineContext:
    """Everything one Intro & Credits job needs to process items."""

    registry: object
    config: object
    settings: GlobalMarkersSettings
    store: MarkerStore
    priority: int
    ffprobe: str
    force: bool = False
    clients: dict[str, object] = field(default_factory=dict)
    local_detectors: tuple[LocalDetectorSpec, ...] = ()
    now: Callable[[], datetime] = _utcnow
    capability_ttl_s: float = 300.0
    _capabilities: dict[str, tuple[float, CapabilityReport]] = field(default_factory=dict)
    _capability_locks: dict[str, threading.Lock] = field(default_factory=dict)
    _capability_locks_guard: threading.Lock = field(default_factory=threading.Lock)


@dataclass(frozen=True)
class _Owner:
    server: object
    config: object
    settings: ServerMarkersSettings
    item_id_hint: str | None


def build_clients(settings: GlobalMarkersSettings) -> dict[str, object]:
    """Online clients for the enabled online sources."""
    from .sources.introdb import IntroDbClient
    from .sources.skipdb import SkipDbClient
    from .sources.theintrodb import TheIntroDbClient

    clients: dict[str, object] = {}
    if settings.source_enabled("theintrodb"):
        clients["theintrodb"] = TheIntroDbClient(settings.source("theintrodb").api_key)
    if settings.source_enabled("introdb"):
        clients["introdb"] = IntroDbClient()
    if settings.source_enabled("skipdb"):
        clients["skipdb"] = SkipDbClient()
    return clients


def build_context(*, registry, config, priority: int, force: bool = False) -> PipelineContext:
    """Context from live settings (used by the job runner)."""
    settings = get_global_settings()
    return PipelineContext(
        registry=registry,
        config=config,
        settings=settings,
        store=get_marker_store(),
        priority=priority,
        ffprobe=ffprobe_path_for(getattr(config, "ffmpeg_path", None)),
        force=force,
        clients=build_clients(settings),
    )


def markers_for_path(store: MarkerStore, canonical_path: str) -> dict[MarkerType, Marker] | None:
    """Decided markers for another local file (None = never decided)."""
    rec = store.get_file(canonical_path)
    if rec is None or not store.get_decisions(rec.id):
        return None
    return store.get_markers(rec.id)


def _owning_servers(item, ctx: PipelineContext) -> list[tuple[object, object, object]]:
    out = []
    seen = set()
    for match in ctx.registry.find_owning_servers(item.canonical_path):
        cfg = ctx.registry.get_config(match.server_id)
        if cfg is None or not cfg.enabled or cfg.id in seen:
            continue
        if cfg.exclude_paths and is_path_excluded(item.canonical_path, cfg.exclude_paths):
            continue
        seen.add(cfg.id)
        out.append((ctx.registry.get(cfg.id), cfg, match))
    return out


def _marker_owners(item, ctx: PipelineContext, owning) -> list[_Owner]:
    owners = []
    for server, cfg, match in owning:
        settings = load_server(cfg.markers, cfg.type.value)
        if not settings.enabled:
            continue
        lib = next((lib for lib in cfg.libraries if lib.id == match.library_id), None)
        if not library_allowed(settings, library_id=match.library_id, library_name=match.library_name, kind=lib.kind if lib else None):
            continue
        owners.append(_Owner(server, cfg, settings, (item.item_id_by_server or {}).get(cfg.id)))
    return owners


def _item_id(server, cfg, hint: str | None, path: str, cache: dict) -> str | None:
    if cfg.id not in cache:
        if hint:
            cache[cfg.id] = hint
        else:
            try:
                cache[cfg.id] = server.resolve_remote_path_to_item_id(path)
            except Exception as exc:
                logger.debug("Item id lookup failed on {} for {}: {}", cfg.name, path, exc)
                cache[cfg.id] = None
    return cache[cfg.id]


def _enabled_types(settings: GlobalMarkersSettings, ids: MediaIds) -> frozenset[MarkerType]:
    types = set()
    if settings.detect_credits:
        types.add(MarkerType.CREDITS)
    if ids.is_episode:
        if settings.detect_intro:
            types.add(MarkerType.INTRO)
        if settings.detect_recap:
            types.add(MarkerType.RECAP)
    return frozenset(types)


def _needs_lookup(store: MarkerStore, file_id: int, source: Source, origin: str, ctx: PipelineContext) -> bool:
    fetched = store.evidence_fetched_at(file_id, source, origin)
    if fetched is None or ctx.force:
        return True
    rows = [r for r in store.evidence_rows(file_id) if r.source is source and r.origin == origin]
    return all(r.type is None for r in rows) and ctx.now() - fetched > NO_DATA_RETRY


def _decide(store: MarkerStore, rec: FileRecord, ctx: PipelineContext, types: frozenset[MarkerType]) -> dict[MarkerType, TypeDecision]:
    enabled = set(ctx.settings.ordered_enabled_sources())
    candidates = [c for c in store.get_evidence(rec.id) if c.source.value in enabled]
    dctx = DecisionContext(rec.duration_ms or 0, rec.is_movie, ctx.settings.publish_when, types, ctx.settings.ordered_enabled_sources())
    locked = store.get_locked(rec.id) if ctx.settings.respect_locks else {}
    return decide(candidates, dctx, locked)


def _all_decided(decisions: dict[MarkerType, TypeDecision], types: frozenset[MarkerType]) -> bool:
    return all(decisions[t].status is DecisionStatus.DECIDED for t in types)


def _row(owner_cfg, publisher_name: str, status: ServerStatus, message: str, path: str) -> dict:
    return {
        "server_id": owner_cfg.id,
        "server_name": owner_cfg.name,
        "server_type": owner_cfg.type.value,
        "adapter_name": publisher_name or "markers",
        "status": status.value,
        "message": message,
        "canonical_path": path,
        "frame_source": "",
        "output_paths": [],
    }


def _capability(ctx: PipelineContext, cfg, publisher) -> CapabilityReport:
    """Per-server capability, cached for ``capability_ttl_s``; one fetch per server even when every check thread misses."""
    cached = ctx._capabilities.get(cfg.id)
    if cached and time.monotonic() - cached[0] < ctx.capability_ttl_s:
        return cached[1]
    with ctx._capability_locks_guard:
        lock = ctx._capability_locks.setdefault(cfg.id, threading.Lock())
    with lock:
        cached = ctx._capabilities.get(cfg.id)
        if cached and time.monotonic() - cached[0] < ctx.capability_ttl_s:
            return cached[1]
        report = publisher.capability()
        ctx._capabilities[cfg.id] = (time.monotonic(), report)
        return report


def _fmt(ms: int) -> str:
    s = ms // 1000
    return f"{s // 60}:{s % 60:02d}"


def _summary(decisions: dict[MarkerType, TypeDecision], types: frozenset[MarkerType]) -> str:
    parts = []
    for t in (MarkerType.INTRO, MarkerType.CREDITS, MarkerType.RECAP):
        if t not in types and not (decisions[t].marker and decisions[t].marker.locked):
            continue
        d = decisions[t]
        if d.status is DecisionStatus.DECIDED and d.marker:
            parts.append(f"{t.value} {_fmt(d.marker.start_ms)}–{_fmt(d.marker.end_ms)} ({', '.join(d.marker.decided_by)})")
        elif d.status is DecisionStatus.NEEDS_REVIEW:
            parts.append(f"{t.value} needs review")
        else:
            parts.append(f"{t.value}: none")
    return "; ".join(parts)


def _run(item, ctx: PipelineContext, *, local: bool, gpu=None, gpu_device_path=None, phase_callback=None,
         cancel_check=None, pause_check=None) -> ItemOutcome | None:
    path = item.canonical_path
    if cancel_check and cancel_check():
        return ItemOutcome(FileOutcome.FAILED.value, "cancelled by user")
    phase = phase_callback or (lambda _text: None)

    owning = _owning_servers(item, ctx)
    owners = _marker_owners(item, ctx, owning)
    if not owners:
        return ItemOutcome(FileOutcome.NO_OWNERS.value, "No server with Intro & Credits turned on has this file")
    if not os.path.isfile(path):
        return ItemOutcome(FileOutcome.FILE_NOT_FOUND.value, "File not found on disk")

    st = os.stat(path)
    path_ids = ids_from_path(path)
    existing = ctx.store.get_file(path)
    unchanged = existing is not None and (existing.size, existing.mtime_ns) == (st.st_size, st.st_mtime_ns) and existing.duration_ms
    probe = None
    if not unchanged:
        phase("Reading chapters…")
        try:
            probe = probe_media(path, ffprobe=ctx.ffprobe)
        except ProbeError as exc:
            return ItemOutcome(FileOutcome.FAILED.value, f"Couldn't read the file: {exc}")
    identity = FileIdentity(path, st.st_size, st.st_mtime_ns)
    rec = ctx.store.upsert_file(
        identity,
        duration_ms=probe.duration_ms if probe else None,
        season_key=os.path.dirname(path) if path_ids.is_episode else None,
        is_movie=path_ids.kind == "movie",
    )
    if probe is not None:
        ctx.store.replace_evidence(rec.id, Source.CHAPTERS, chapter_candidates(probe))
    if not rec.duration_ms:
        return ItemOutcome(FileOutcome.FAILED.value, "Couldn't read the file's duration")

    item_ids: dict[str, str | None] = {}
    ids = path_ids
    server_ids_fetched = False

    def _complete_ids() -> None:
        # Server metadata costs an HTTP call, so it is only fetched when the path alone can't answer.
        nonlocal ids, server_ids_fetched
        if server_ids_fetched:
            return
        server_ids_fetched = True
        for server, cfg, _match in owning:
            item_id = _item_id(server, cfg, (item.item_id_by_server or {}).get(cfg.id), path, item_ids)
            if item_id:
                ids = merge_ids(ids, ids_from_server_dict(server.get_external_ids(item_id)))
                return

    if ids.kind == "unknown":
        _complete_ids()
    if (ids.kind == "movie") != rec.is_movie or (os.path.dirname(path) if ids.is_episode else None) != rec.season_key:
        rec = ctx.store.upsert_file(identity, duration_ms=None, season_key=os.path.dirname(path) if ids.is_episode else None,
                                    is_movie=ids.kind == "movie")
    types = _enabled_types(ctx.settings, ids)

    decisions = _decide(ctx.store, rec, ctx, types)
    for source_id in ctx.settings.ordered_enabled_sources():
        if _all_decided(decisions, types):
            break
        if cancel_check and cancel_check():
            return ItemOutcome(FileOutcome.FAILED.value, "cancelled by user")
        source = Source(source_id)
        if source_id in _ONLINE_SOURCES:
            client = ctx.clients.get(source_id)
            if client is None or not _needs_lookup(ctx.store, rec.id, source, "", ctx):
                continue
            if not (ids.tmdb or ids.imdb or ids.tvdb) or (ids.is_episode and not ids.imdb):
                _complete_ids()
            phase(f"Looking up {source_id}…")
            result = client.lookup(ids, duration_ms=rec.duration_ms, priority=ctx.priority, cancel_check=cancel_check)
            if result.status in ("ok", "no_data"):
                ctx.store.replace_evidence(rec.id, source, list(result.candidates), detail=result.detail)
            else:
                logger.debug("{} lookup for {}: {} {}", source_id, path, result.status, result.detail)
        elif source is Source.SERVER_MARKERS:
            for server, cfg, _match in owning:
                published = ctx.store.get_publish_state(rec.id, cfg.id)
                if published and published.markers:
                    continue
                if ctx.store.evidence_fetched_at(rec.id, source, cfg.id) is not None:
                    continue
                item_id = _item_id(server, cfg, (item.item_id_by_server or {}).get(cfg.id), path, item_ids)
                if not item_id:
                    continue
                found = read_server_markers(server, cfg, item_id)
                if found is not None:
                    ctx.store.replace_evidence(rec.id, source, found, origin=cfg.id)
        else:
            specs = [s for s in ctx.local_detectors if s.source is source and any(
                t in s.types and decisions[t].status is not DecisionStatus.DECIDED for t in types)]
            if not specs:
                continue
            if not local:
                return None
            for spec in specs:
                found = spec.detect(rec, ctx=ctx, gpu=gpu, gpu_device_path=gpu_device_path, phase_callback=phase,
                                    cancel_check=cancel_check, pause_check=pause_check)
                ctx.store.replace_evidence(rec.id, source, list(found))
        decisions = _decide(ctx.store, rec, ctx, types)

    ctx.store.save_decisions(rec.id, decisions, settings_fingerprint=ctx.settings.detection_fingerprint())
    markers = ctx.store.get_markers(rec.id)
    needs_review = any(decisions[t].status is DecisionStatus.NEEDS_REVIEW for t in types)
    rows = [_publish_to(owner, rec, markers, needs_review, item_ids, ctx) for owner in owners]

    key = file_outcome({r["status"] for r in rows}, needs_review=needs_review)
    return ItemOutcome(key.value, _summary(decisions, types), rows)


def _publish_to(owner: _Owner, rec: FileRecord, markers: dict[MarkerType, Marker], needs_review: bool,
                item_ids: dict[str, str | None], ctx: PipelineContext) -> dict:
    cfg = owner.config
    path = rec.canonical_path
    publisher = publisher_for(owner.server, cfg, sibling_markers=lambda p: markers_for_path(ctx.store, p))
    if publisher is None:
        ctx.store.set_publish_state(rec.id, cfg.id, item_id=None, markers=None, status="skipped",
                                    message="Not supported for this server type yet")
        return _row(cfg, "", ServerStatus.SKIPPED, "Not supported for this server type yet", path)

    def _finish(status: ServerStatus, message: str, *, item_id=None, published=None) -> dict:
        ctx.store.set_publish_state(rec.id, cfg.id, item_id=item_id, markers=published, status=STATE_BY_STATUS[status],
                                    message=message, verified=False)
        return _row(cfg, publisher.name, status, message, path)

    report = _capability(ctx, cfg, publisher)
    if not report.ready:
        return _finish(ServerStatus.SKIPPED, report.message)
    item_id = _item_id(owner.server, cfg, owner.item_id_hint, path, item_ids)
    if not item_id:
        return _finish(ServerStatus.WAITING, "Not in this server's library yet")
    wanted = publisher.project(markers.values())
    previous = ctx.store.get_publish_state(rec.id, cfg.id)
    previous_markers = list(previous.markers) if previous else []
    if previous and previous.status == "written" and previous.item_id == item_id and previous.markers_hash == MarkerStore.markers_hash(wanted):
        if not wanted:
            status = ServerStatus.NEEDS_REVIEW if needs_review else ServerStatus.UP_TO_DATE
            return _row(cfg, publisher.name, status, "No markers to show yet" if needs_review else "Up to date", path)
        return _row(cfg, publisher.name, ServerStatus.UP_TO_DATE, "Up to date", path)
    if not wanted and not previous_markers:
        if needs_review:
            return _row(cfg, publisher.name, ServerStatus.NEEDS_REVIEW, "Sources don't agree yet", path)
        return _row(cfg, publisher.name, ServerStatus.NONE, "No markers found for this file", path)
    try:
        publisher.write(item_id, wanted, previous=previous_markers, duration_ms=rec.duration_ms, canonical_path=path)
    except (ItemNotFoundError, WaitingForVersionsError) as exc:
        return _finish(ServerStatus.WAITING, str(exc), item_id=item_id)
    except PublishError as exc:
        return _finish(ServerStatus.FAILED, str(exc), item_id=item_id)
    except Exception as exc:
        logger.exception("Publishing markers to {} failed for {}", cfg.name, path)
        return _finish(ServerStatus.FAILED, f"{type(exc).__name__}: {exc}", item_id=item_id)
    return _finish(ServerStatus.WRITTEN, f"{len(wanted)} marker(s)", item_id=item_id, published=wanted)


def check_item(item, *, ctx: PipelineContext, cancel_check=None) -> ItemOutcome | None:
    """Check stage: decide from cheap sources and publish; None = needs a worker (local detector)."""
    return _run(item, ctx, local=False, cancel_check=cancel_check)


def process_item(item, *, ctx: PipelineContext, gpu=None, gpu_device_path=None, progress_callback=None,
                 phase_callback=None, cancel_check=None, pause_check=None) -> ItemOutcome:
    """Worker stage: same steps plus local detectors on the worker's GPU/CPU."""
    return _run(item, ctx, local=True, gpu=gpu, gpu_device_path=gpu_device_path, phase_callback=phase_callback,
                cancel_check=cancel_check, pause_check=pause_check)


def kind_handlers(ctx: PipelineContext) -> KindHandlers:
    """Dispatcher handlers bound to one job's context."""
    return KindHandlers(
        check_fn=lambda item, *, cancel_check=None: check_item(item, ctx=ctx, cancel_check=cancel_check),
        process_fn=lambda item, **kw: process_item(item, ctx=ctx, **kw),
        outcome_keys=OUTCOME_KEYS,
        check_label="Looking up markers…",
        check_worker_label="Intro & Credits",
        check_share=0.25,
    )
```

Notes for the implementer (these are behaviours the tests pin, not optional):
- `_needs_lookup` must use `ctx.now()`; the caching test drives both the store clock and `ctx.now` from one fake clock.
- In `test_one_server_problem_does_not_block_the_other[capability]` the publish state row for a skipped server has
  `markers == ()` — `set_publish_state(markers=None)` on a server we never published to stores `[]`.
- `_publish_to` for "UP_TO_DATE" must not rewrite `publish_state` (keeps `verified_at` from reconcile later).
- The check stage's fan-out may run many files in parallel (up to a quarter of the checking threads, `check_share`);
  every store call is locked, and `_capability` takes a per-server lock and re-checks the cache inside it, so each
  server's capability is fetched once per TTL however many checks miss at the same moment.

- [ ] **Step 6: Run** `pytest --no-cov tests/markers/test_pipeline.py -q` → PASS; then all markers tests.
- [ ] **Step 7: Commit** — `feat(markers): per-file pipeline with ordered evidence, early stop and per-server publish`

---
## Task 12: Intro & Credits job runner + triggers (API, webhooks, schedules, restart)

Spec §6.2 step 1, §6.4 items 6, 9, 10; design artifact "How it fits with previews". Webhook batches submit the
preview job as today, then an Intro & Credits job for the same files at NORMAL; backfill and schedules at LOW.

**Files:**
- Create: `media_preview_generator/markers/job_runner.py`, `media_preview_generator/markers/triggers.py`,
  `media_preview_generator/web/routes/api_markers.py`
- Modify: `media_preview_generator/jobs/dispatcher.py` (`get_or_create_dispatcher`),
  `media_preview_generator/web/routes/job_runner.py` (`_start_job_async` delegation at the top),
  `media_preview_generator/web/routes/__init__.py` (import `api_markers`),
  `media_preview_generator/web/webhooks.py` (`_execute_webhook_job` after `_start_job_async`, `create_vendor_webhook_job`
  after `_start_job_async`), `media_preview_generator/web/scheduler.py` (`execute_scheduled_job` branch),
  `tests/conftest.py` (`_sync_start_job_async` also shims `markers.job_runner.threading`)
- Test: `tests/markers/test_job_runner.py`, `tests/markers/test_triggers.py`, `tests/markers/test_api_markers_jobs.py`,
  append to `tests/test_scheduler.py`

**Interfaces:**
- Consumes: Task 2 (`KindHandlers`, `submit_items(kind=, handlers=)`, `create_job(kind=)`), Task 11
  (`build_context`, `kind_handlers`), orchestrator helpers `_enumerate_items_for_servers`,
  `_resolve_webhook_path_to_canonical`, `_build_multi_server_registry`, `plex_client._expand_directory_to_media_files`,
  `web.routes.job_runner._build_selected_gpus`, `web.job_gate.get_job_gate/format_wait_message`.
- Produces:
```python
# jobs/dispatcher.py
def get_or_create_dispatcher(config, selected_gpus: list) -> JobDispatcher
# markers/job_runner.py
def build_items(job_config: dict, *, registry, cancel_check=None, progress_callback=None) -> tuple[list[ProcessableItem], list[str]]
def run_intro_credits_job(job_id: str) -> None                        # synchronous body
def start_intro_credits_job_async(job_id: str, config_overrides: dict | None = None) -> None
# markers/triggers.py
def markers_enabled_anywhere() -> bool
def create_intro_credits_job(*, library_name: str, priority: int, source: str, libraries: list[dict] | None = None,
                             file_paths: list[str] | None = None, follows_job_id: str | None = None,
                             parent_schedule_id: str = "", force: bool = False,
                             item_id_hints: dict[str, dict[str, str]] | None = None) -> Job
def submit_webhook_follow_up(*, preview_job_id: str, paths: list[str], source: str,
                             item_id_hints: dict[str, dict[str, str]] | None = None) -> str | None
# web/routes/api_markers.py
POST /api/markers/jobs  body {"libraries": [{"server_id","library_id"}], "file_paths": [str], "priority": 1|2|3|"low"…,
                              "force": bool, "library_name": str} → 201 job.to_dict()
```
Intro & Credits job config keys: `kind`, `source`, `libraries`, `file_paths`, `follows_job_id`, `force`,
`webhook_item_id_hints`. Schedules use `config.job_type == "intro_credits"`.

- [ ] **Step 1: Extend the sync-thread test shim** — in `tests/conftest.py::_sync_start_job_async`, after the existing
`monkeypatch.setattr(jr_mod, "threading", ...)`, add:
```python
    try:
        import media_preview_generator.markers.job_runner as markers_jr
    except ImportError:
        markers_jr = None
    if markers_jr is not None:
        monkeypatch.setattr(markers_jr, "threading", _ThreadingShim(), raising=True)
```
(The import fails until Step 4 creates the module; the guard keeps the suite green in between.)

- [ ] **Step 2: Write failing tests**

```python
# tests/markers/test_job_runner.py
import os
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.job_kinds import JOB_KIND_INTRO_CREDITS
from media_preview_generator.markers import job_runner
from media_preview_generator.processing.types import ProcessableItem
from media_preview_generator.servers.base import ServerType
from tests.markers.fakes import FakeRegistry, server_config


class TestBuildItems:
    def test_file_paths_are_resolved_expanded_deduped_and_season_sorted(self, tmp_path):
        season = tmp_path / "tv" / "Show" / "Season 01"
        season.mkdir(parents=True)
        for name in ("S01E02.mkv", "S01E01.mkv"):
            (season / name).write_bytes(b"x")
        other = tmp_path / "tv" / "Another" / "Season 01"
        other.mkdir(parents=True)
        (other / "S01E01.mkv").write_bytes(b"x")
        reg = FakeRegistry({"jf-1": server_config("jf-1", ServerType.JELLYFIN)})
        cfg = {"file_paths": [str(season), str(season / "S01E01.mkv"), str(other / "S01E01.mkv")],
               "webhook_item_id_hints": {str(other / "S01E01.mkv"): {"jf-1": "abc"}}}
        with patch("media_preview_generator.jobs.orchestrator._resolve_webhook_path_to_canonical",
                   side_effect=lambda p, configs, log_resolution=True: (p, [])) as resolve:
            items, warnings = job_runner.build_items(cfg, registry=reg)
        assert [i.canonical_path for i in items] == [
            str(other / "S01E01.mkv"), str(season / "S01E01.mkv"), str(season / "S01E02.mkv")]
        assert items[0].item_id_by_server == {"jf-1": "abc"} and items[1].item_id_by_server == {}
        assert warnings == []
        assert all(c.kwargs.get("log_resolution") is False for c in resolve.call_args_list)

    def test_libraries_are_enumerated_per_server_with_their_ids(self):
        reg = FakeRegistry({"plex-1": server_config("plex-1", ServerType.PLEX), "jf-1": server_config("jf-1", ServerType.JELLYFIN)})
        seen = {}

        def fake_enumerate(candidates, *, enumerate_one, cancel_check=None, label, progress_callback=None):
            processor = MagicMock()
            processor.list_canonical_paths.side_effect = lambda cfg, **kw: seen.setdefault(cfg.id, kw["library_ids"]) and []
            for cfg in candidates:
                list(enumerate_one(processor, cfg) or [])
            return [], [("JF-1", "TimeoutError: slow")]

        cfg = {"libraries": [{"server_id": "plex-1", "library_id": "1"}, {"server_id": "plex-1", "library_id": "2"}]}
        with patch("media_preview_generator.jobs.orchestrator._enumerate_items_for_servers", side_effect=fake_enumerate):
            items, warnings = job_runner.build_items(cfg, registry=reg)
        assert seen == {"plex-1": ["1", "2"]}
        assert warnings == ["Couldn't list JF-1: TimeoutError: slow"]

    def test_no_selection_enumerates_markers_enabled_servers_only(self):
        reg = FakeRegistry({"plex-1": server_config("plex-1", ServerType.PLEX),
                            "jf-1": server_config("jf-1", ServerType.JELLYFIN, markers={"enabled": False})})
        with patch("media_preview_generator.jobs.orchestrator._enumerate_items_for_servers", return_value=([], [])) as enum:
            job_runner.build_items({}, registry=reg)
        assert [c.id for c in enum.call_args.args[0]] == ["plex-1"]


class TestRun:
    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        jm = MagicMock()
        job = MagicMock(id="j1", kind=JOB_KIND_INTRO_CREDITS, priority=3, config={"libraries": [], "force": True})
        jm.get_job.return_value = job
        jm.is_cancellation_requested.return_value = False
        monkeypatch.setattr(job_runner, "get_job_manager", lambda: jm)
        sm = MagicMock(processing_paused=False)
        sm.get.return_value = "INFO"
        monkeypatch.setattr(job_runner, "get_settings_manager", lambda: sm)
        gate = MagicMock()
        gate.acquire.return_value = True
        monkeypatch.setattr(job_runner, "get_job_gate", lambda: gate)
        monkeypatch.setattr(job_runner, "load_config", lambda: MagicMock(ffmpeg_path="/usr/bin/ffmpeg"))
        registry = MagicMock()
        monkeypatch.setattr(job_runner, "_build_multi_server_registry", lambda config: registry)
        monkeypatch.setattr(job_runner, "_build_selected_gpus", lambda settings: [])
        ctx = MagicMock()
        monkeypatch.setattr(job_runner, "build_context", MagicMock(return_value=ctx))
        handlers = MagicMock()
        monkeypatch.setattr(job_runner, "kind_handlers", lambda c: handlers)
        dispatcher = MagicMock()
        tracker = MagicMock()
        tracker.done_event = threading.Event()  # a MagicMock's is_set() is truthy and would fake "job finished"
        tracker.get_result.return_value = {"completed": 1, "failed": 0, "total": 1, "cancelled": False,
                                           "outcome": {"markers_published": 1}}
        dispatcher.submit_items.return_value = tracker
        monkeypatch.setattr(job_runner, "get_or_create_dispatcher", lambda config, gpus: dispatcher)
        return {"jm": jm, "job": job, "gate": gate, "dispatcher": dispatcher, "handlers": handlers, "registry": registry,
                "sm": sm, "build_context": job_runner.build_context}

    def test_submits_items_with_kind_handlers_and_job_priority_then_completes(self, env):
        items = [ProcessableItem(canonical_path="/m/a.mkv", server_id="")]
        with patch.object(job_runner, "build_items", return_value=(items, ["Couldn't list X"])):
            job_runner.run_intro_credits_job("j1")
        kwargs = env["dispatcher"].submit_items.call_args.kwargs
        assert kwargs["job_id"] == "j1" and kwargs["items"] == items and kwargs["registry"] is env["registry"]
        assert kwargs["kind"] == JOB_KIND_INTRO_CREDITS and kwargs["handlers"] is env["handlers"] and kwargs["priority"] == 3
        assert set(kwargs["callbacks"]) == {"progress_callback", "worker_callback", "cancel_check", "pause_check"}
        env["build_context"].assert_called_once()
        assert env["build_context"].call_args.kwargs["force"] is True and env["build_context"].call_args.kwargs["priority"] == 3
        env["jm"].set_job_outcome.assert_called_once_with("j1", {"markers_published": 1})
        env["jm"].complete_job.assert_called_once_with("j1", warning="Couldn't list X")
        env["gate"].release.assert_called_once_with(3)

    def test_pause_check_honours_job_and_global_pause(self, env):
        seen = {}

        def during_wait(timeout=None):
            pause_check = env["dispatcher"].submit_items.call_args.kwargs["callbacks"]["pause_check"]
            for job_flag, global_flag in ((False, False), (True, False), (False, True), (True, True)):
                env["jm"].is_pause_requested.return_value = job_flag
                env["sm"].processing_paused = global_flag
                seen[(job_flag, global_flag)] = pause_check()
            env["jm"].is_pause_requested.return_value = False
            env["sm"].processing_paused = False
            return True

        env["dispatcher"].submit_items.return_value.wait.side_effect = during_wait
        with patch.object(job_runner, "build_items", return_value=([ProcessableItem("/m/a.mkv", "")], [])):
            job_runner.run_intro_credits_job("j1")
        assert seen == {(False, False): False, (True, False): True, (False, True): True, (True, True): True}

    def test_paused_job_hands_back_its_slot_so_a_high_preview_job_is_admitted(self, env, monkeypatch):
        from media_preview_generator.web.job_gate import JobGate

        gate = JobGate(lambda: 1)
        monkeypatch.setattr(job_runner, "get_job_gate", lambda: gate)
        state = {"paused": False}
        env["jm"].is_pause_requested.side_effect = lambda jid: state["paused"]
        steps = []

        def during_wait(timeout=None):
            pause_check = env["dispatcher"].submit_items.call_args.kwargs["callbacks"]["pause_check"]
            step = len(steps)
            steps.append(step)
            if step == 0:
                assert gate.snapshot()[0] == 1 and pause_check() is False
                state["paused"] = True
                return False
            if step == 1:
                # Slot handed back: a HIGH preview job gets in at cap 1, and nothing of ours is dispatched.
                assert gate.snapshot()[0] == 0 and pause_check() is True
                assert gate.acquire(1, cancel_check=lambda: True) is True
                gate.release(1)
                state["paused"] = False
                return False
            # Resumed: slot re-acquired before dispatch continues.
            assert gate.snapshot()[0] == 1 and pause_check() is False
            return True

        env["dispatcher"].submit_items.return_value.wait.side_effect = during_wait
        with patch.object(job_runner, "build_items", return_value=([ProcessableItem("/m/a.mkv", "")], [])):
            job_runner.run_intro_credits_job("j1")
        assert steps == [0, 1, 2]
        assert gate.snapshot()[0] == 0
        env["jm"].complete_job.assert_called_once_with("j1", warning=None)

    def test_job_that_finished_while_paused_does_not_queue_for_a_slot(self, env, monkeypatch):
        from media_preview_generator.web.job_gate import JobGate

        gate = JobGate(lambda: 1)
        monkeypatch.setattr(job_runner, "get_job_gate", lambda: gate)
        state = {"paused": False}
        env["jm"].is_pause_requested.side_effect = lambda jid: state["paused"]
        tracker = env["dispatcher"].submit_items.return_value
        calls = []

        def during_wait(timeout=None):
            calls.append(len(calls))
            if len(calls) == 1:
                state["paused"] = True
                return False
            if len(calls) == 2:
                # Slot handed back; a long scan takes it, the last in-flight item finishes, the user resumes.
                assert gate.acquire(2, cancel_check=lambda: True) is True
                tracker.done_event.set()
                state["paused"] = False
                return False
            return True

        tracker.wait.side_effect = during_wait
        with patch.object(job_runner, "build_items", return_value=([ProcessableItem("/m/a.mkv", "")], [])):
            job_runner.run_intro_credits_job("j1")
        assert calls == [0, 1, 2]
        assert gate.snapshot()[0] == 1  # only the scan's slot; ours was never re-taken
        env["jm"].complete_job.assert_called_once_with("j1", warning=None)

    @pytest.mark.parametrize("preview_priority", [1, 2, 3])
    def test_follow_up_waits_for_its_preview_job_whatever_its_priority(self, env, monkeypatch, preview_priority):
        from media_preview_generator.web.jobs import JobStatus

        env["job"].config = {"libraries": [], "follows_job_id": "prev-1"}
        preview = MagicMock(id="prev-1", priority=preview_priority, status=JobStatus.RUNNING,
                            progress=SimpleNamespace(retry_eta=None))
        env["jm"].get_job.side_effect = lambda jid: preview if jid == "prev-1" else env["job"]
        sleeps = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            env["gate"].acquire.assert_not_called()
            if len(sleeps) == 2:
                preview.status = JobStatus.COMPLETED

        monkeypatch.setattr(job_runner, "time", SimpleNamespace(sleep=fake_sleep))
        with patch.object(job_runner, "build_items", return_value=([ProcessableItem("/m/a.mkv", "")], [])):
            job_runner.run_intro_credits_job("j1")
        assert len(sleeps) == 2
        env["gate"].acquire.assert_called_once()
        env["dispatcher"].submit_items.assert_called_once()
        assert "preview job" in env["jm"].update_progress.call_args_list[0].kwargs["current_item"]

    def test_follow_up_cancelled_while_waiting_never_takes_a_slot(self, env, monkeypatch):
        from media_preview_generator.web.jobs import JobStatus

        env["job"].config = {"follows_job_id": "prev-1"}
        preview = MagicMock(status=JobStatus.PENDING, progress=SimpleNamespace(retry_eta=None))
        env["jm"].get_job.side_effect = lambda jid: preview if jid == "prev-1" else env["job"]

        def fake_sleep(_seconds):
            env["jm"].is_cancellation_requested.return_value = True

        monkeypatch.setattr(job_runner, "time", SimpleNamespace(sleep=fake_sleep))
        job_runner.run_intro_credits_job("j1")
        env["jm"].cancel_job.assert_called_once_with("j1")
        env["gate"].acquire.assert_not_called()
        env["gate"].release.assert_not_called()
        env["dispatcher"].submit_items.assert_not_called()

    def test_follow_up_does_not_wait_through_a_preview_retry_countdown(self, env, monkeypatch):
        from media_preview_generator.web.jobs import JobStatus

        env["job"].config = {"follows_job_id": "prev-1"}
        preview = MagicMock(status=JobStatus.PENDING, progress=SimpleNamespace(retry_eta="2026-09-13T10:00:00+00:00"))
        env["jm"].get_job.side_effect = lambda jid: preview if jid == "prev-1" else env["job"]
        monkeypatch.setattr(job_runner, "time", SimpleNamespace(sleep=MagicMock(side_effect=AssertionError("slept"))))
        with patch.object(job_runner, "build_items", return_value=([ProcessableItem("/m/a.mkv", "")], [])):
            job_runner.run_intro_credits_job("j1")
        env["dispatcher"].submit_items.assert_called_once()
        env["jm"].complete_job.assert_called_once_with("j1", warning=None)

    def test_follow_up_of_a_deleted_preview_job_starts_straight_away(self, env, monkeypatch):
        env["job"].config = {"follows_job_id": "gone"}
        env["jm"].get_job.side_effect = lambda jid: None if jid == "gone" else env["job"]
        monkeypatch.setattr(job_runner, "time", SimpleNamespace(sleep=MagicMock(side_effect=AssertionError("slept"))))
        with patch.object(job_runner, "build_items", return_value=([ProcessableItem("/m/a.mkv", "")], [])):
            job_runner.run_intro_credits_job("j1")
        env["dispatcher"].submit_items.assert_called_once()
        env["jm"].complete_job.assert_called_once_with("j1", warning=None)

    @pytest.mark.parametrize("others_running", [True, False])
    def test_teardown_clears_per_job_state(self, env, others_running):
        env["jm"].get_running_jobs.return_value = [MagicMock()] if others_running else []
        with patch.object(job_runner, "build_items", return_value=([ProcessableItem("/m/a.mkv", "")], [])), \
             patch.object(job_runner, "set_file_result_callback") as set_cb, \
             patch.object(job_runner, "clear_failures") as clear_failures:
            job_runner.run_intro_credits_job("j1")
        env["jm"].clear_pause_flag.assert_called_once_with("j1")
        env["jm"].clear_cancellation_flag.assert_called_once_with("j1")
        assert set_cb.call_args_list[-1].args == (None,) and set_cb.call_args_list[-1].kwargs == {"job_id": "j1"}
        clear_failures.assert_called_once()
        assert env["jm"].clear_worker_statuses.called is (not others_running)

    def test_nothing_to_do_completes_with_warning_and_never_submits(self, env):
        with patch.object(job_runner, "build_items", return_value=([], [])):
            job_runner.run_intro_credits_job("j1")
        env["dispatcher"].submit_items.assert_not_called()
        assert "no files" in env["jm"].complete_job.call_args.kwargs["warning"].lower()

    def test_paused_processing_leaves_job_pending(self, env):
        env["sm"].processing_paused = True
        job_runner.run_intro_credits_job("j1")
        env["gate"].acquire.assert_not_called()
        env["jm"].start_job.assert_not_called()

    def test_cancel_while_waiting_for_gate(self, env):
        env["gate"].acquire.return_value = False
        job_runner.run_intro_credits_job("j1")
        env["jm"].cancel_job.assert_called_once_with("j1")
        env["gate"].release.assert_not_called()

    def test_cancelled_job_is_not_marked_complete(self, env):
        env["dispatcher"].submit_items.return_value.get_result.return_value["cancelled"] = True
        env["jm"].is_cancellation_requested.return_value = True
        with patch.object(job_runner, "build_items", return_value=([ProcessableItem("/m/a.mkv", "")], [])):
            job_runner.run_intro_credits_job("j1")
        env["jm"].complete_job.assert_not_called()

    def test_crash_marks_job_failed_and_releases_gate(self, env):
        with patch.object(job_runner, "build_items", side_effect=RuntimeError("enumeration exploded")):
            job_runner.run_intro_credits_job("j1")
        assert "enumeration exploded" in env["jm"].complete_job.call_args.kwargs["error"]
        env["gate"].release.assert_called_once_with(3)


@pytest.mark.parametrize(("kind", "delegated"), [("intro_credits", True), ("previews", False)])
def test_start_job_async_delegates_by_kind(kind, delegated, monkeypatch):
    from media_preview_generator.web.routes import job_runner as preview_runner

    jm = MagicMock()
    jm.get_job.return_value = MagicMock(kind=kind, config={})
    monkeypatch.setattr(preview_runner, "get_job_manager", lambda: jm)
    job_id = f"job-delegation-{kind}"
    with patch("media_preview_generator.markers.job_runner.start_intro_credits_job_async") as start, \
         patch.object(preview_runner, "threading") as threading_mod:
        preview_runner._start_job_async(job_id, {"a": 1})
    preview_runner._inflight_jobs.discard(job_id)
    if delegated:
        start.assert_called_once_with(job_id, {"a": 1})
        threading_mod.Thread.assert_not_called()
    else:
        start.assert_not_called()
        threading_mod.Thread.assert_called_once()
        threading_mod.Thread.return_value.start.assert_called_once()
```
(`get_job_manager` import name in `web/routes/job_runner.py`: check and monkeypatch the module attribute it uses.)

```python
# tests/markers/test_triggers.py
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.job_kinds import JOB_KIND_INTRO_CREDITS
from media_preview_generator.markers import triggers


@pytest.mark.parametrize(("servers", "expected"), [
    ([], False),
    ([{"type": "plex", "enabled": True, "markers": {"enabled": False}}], False),
    ([{"type": "jellyfin", "enabled": False, "markers": {"enabled": True}}], False),
    ([{"type": "plex", "enabled": True, "markers": {"enabled": True}}], False),  # unconfirmed Plex = off
    ([{"type": "plex", "enabled": True, "markers": {"enabled": True, "plex": {"db_write_confirmed_at": "t"}}}], True),
    ([{"type": "jellyfin", "enabled": True, "markers": {"enabled": True}}], True),
])
def test_markers_enabled_anywhere_matrix(servers, expected, monkeypatch):
    sm = MagicMock()
    sm.get.side_effect = lambda key, default=None: servers if key == "media_servers" else default
    monkeypatch.setattr(triggers, "get_settings_manager", lambda: sm)
    assert triggers.markers_enabled_anywhere() is expected


def test_create_job_config_and_start(monkeypatch):
    jm = MagicMock()
    jm.create_job.return_value = MagicMock(id="new")
    monkeypatch.setattr(triggers, "get_job_manager", lambda: jm)
    with patch.object(triggers, "start_intro_credits_job_async") as start:
        triggers.create_intro_credits_job(library_name="Intro & Credits · R&M S01", priority=2, source="sonarr",
                                          file_paths=["/data/tv/a.mkv"], follows_job_id="prev-1",
                                          item_id_hints={"/data/tv/a.mkv": {"jf-1": "x"}})
    kwargs = jm.create_job.call_args.kwargs
    assert kwargs["kind"] == JOB_KIND_INTRO_CREDITS and kwargs["priority"] == 2
    assert kwargs["library_name"] == "Intro & Credits · R&M S01" and kwargs["parent_schedule_id"] == ""
    assert kwargs["config"] == {"kind": "intro_credits", "source": "sonarr", "libraries": [], "file_paths": ["/data/tv/a.mkv"],
                                "follows_job_id": "prev-1", "force": False,
                                "webhook_item_id_hints": {"/data/tv/a.mkv": {"jf-1": "x"}}}
    start.assert_called_once_with("new")


@pytest.mark.parametrize("enabled", [True, False])
def test_webhook_follow_up(enabled, monkeypatch):
    monkeypatch.setattr(triggers, "markers_enabled_anywhere", lambda: enabled)
    jm = MagicMock()
    jm.get_job.return_value = MagicMock(library_name="Rick and Morty S01 · 11 files")
    monkeypatch.setattr(triggers, "get_job_manager", lambda: jm)
    with patch.object(triggers, "create_intro_credits_job", return_value=MagicMock(id="ic-1")) as create:
        out = triggers.submit_webhook_follow_up(preview_job_id="prev-1", paths=["/d/a.mkv"], source="sonarr")
    if not enabled:
        assert out is None
        create.assert_not_called()
        return
    assert out == "ic-1"
    kwargs = create.call_args.kwargs
    assert kwargs["library_name"] == "Intro & Credits · Rick and Morty S01 · 11 files"
    assert kwargs["priority"] == 2 and kwargs["follows_job_id"] == "prev-1" and kwargs["file_paths"] == ["/d/a.mkv"]
    assert kwargs["source"] == "sonarr"
```

Webhook wiring tests — new file `tests/markers/test_webhook_follow_up_wiring.py`, reusing the fixtures and the
synchronous debounce pattern from `tests/test_webhooks_source_in_overrides.py` (copy its `captured_overrides`,
`_isolate_settings`, `_isolate_jobs` fixtures):
```python
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.web import webhooks as wh

SONARR_PATH = "/data/TV Shows/Show (2023)/Season 02/Show - S02E04.mkv"


def _fire_debounced(source, path):
    captured = []

    def fake_timer(_delay, fn, *positional, args=None, kwargs=None):
        t = MagicMock()
        t.start = lambda: captured.append((fn, list(positional) + list(args or []), kwargs or {}))
        return t

    with patch.object(wh, "_check_and_record_dedup", return_value=None), \
         patch("media_preview_generator.web.webhooks.threading.Timer", side_effect=fake_timer):
        assert wh._schedule_webhook_job(source, "Show S02E04", path) is True
        fn, args, kwargs = captured[-1]
        fn(*args, **kwargs)


def test_debounced_batch_queues_follow_up_after_preview_job(captured_overrides):
    with patch("media_preview_generator.markers.triggers.submit_webhook_follow_up") as follow_up:
        _fire_debounced("sonarr", SONARR_PATH)
    preview = captured_overrides[-1]
    kwargs = follow_up.call_args.kwargs
    assert kwargs == {"preview_job_id": preview["job_id"], "paths": preview["overrides"]["webhook_paths"], "source": "sonarr"}


def test_debounced_follow_up_failure_never_breaks_preview_job(captured_overrides):
    with patch("media_preview_generator.markers.triggers.submit_webhook_follow_up", side_effect=RuntimeError("boom")):
        _fire_debounced("radarr", "/data/Movies/Foo (2024)/Foo (2024).mkv")
    assert captured_overrides and captured_overrides[-1]["overrides"]["source"] == "radarr"


def test_vendor_webhook_follow_up_carries_item_id_hints(captured_overrides):
    with patch.object(wh, "_check_and_record_dedup", return_value=None), \
         patch("media_preview_generator.markers.triggers.submit_webhook_follow_up") as follow_up:
        job_id = wh.create_vendor_webhook_job(
            source="jellyfin", title="Foo", canonical_path="/data/Movies/Foo.mkv",
            item_id_by_server={"jf-1": "abc"}, server_id=None,
        )
    assert follow_up.call_args.kwargs == {
        "preview_job_id": job_id, "paths": ["/data/Movies/Foo.mkv"], "source": "jellyfin",
        "item_id_hints": {"/data/Movies/Foo.mkv": {"jf-1": "abc"}},
    }


def test_vendor_follow_up_failure_never_breaks_preview_job(captured_overrides):
    with patch.object(wh, "_check_and_record_dedup", return_value=None), \
         patch("media_preview_generator.markers.triggers.submit_webhook_follow_up", side_effect=RuntimeError("boom")):
        job_id = wh.create_vendor_webhook_job(source="plex", title="Foo", canonical_path="/data/Movies/Foo.mkv", server_id=None)
    assert captured_overrides[-1]["job_id"] == job_id
```
(If `create_vendor_webhook_job` sanitises `source` into `safe_source`, assert the sanitised value.)

Scheduler (append to `tests/test_scheduler.py`, class `TestExecuteScheduledJobDispatch`, using its
`scheduler_manager` fixture):
```python
    @pytest.mark.parametrize(("priority", "expected"), [(None, 3), (1, 1)])
    def test_dispatches_intro_credits_job(self, scheduler_manager, priority, expected):
        from media_preview_generator.web.scheduler import execute_scheduled_job

        mock_callback = MagicMock()
        scheduler_manager.set_run_job_callback(mock_callback)
        schedule = scheduler_manager.create_schedule(
            name="Markers", library_ids=["1", "2"], library_name="TV Shows", server_id="plex-1",
            cron_expression="0 3 * * 0", config={"job_type": "intro_credits"},
        )
        with patch("media_preview_generator.markers.triggers.create_intro_credits_job") as create:
            execute_scheduled_job(schedule["id"], ["1", "2"], "TV Shows", {"job_type": "intro_credits"}, priority, "plex-1")
        mock_callback.assert_not_called()
        kwargs = create.call_args.kwargs
        assert kwargs["libraries"] == [{"server_id": "plex-1", "library_id": "1"}, {"server_id": "plex-1", "library_id": "2"}]
        assert kwargs["priority"] == expected and kwargs["parent_schedule_id"] == schedule["id"]
        assert kwargs["source"] == "schedule" and kwargs["library_name"] == "Intro & Credits: TV Shows"

    def test_intro_credits_schedule_without_server_checks_every_enabled_server(self, scheduler_manager):
        from media_preview_generator.web.scheduler import execute_scheduled_job

        schedule = scheduler_manager.create_schedule(name="All", cron_expression="0 3 * * 0", config={"job_type": "intro_credits"})
        with patch("media_preview_generator.markers.triggers.create_intro_credits_job") as create:
            execute_scheduled_job(schedule["id"], None, "", {"job_type": "intro_credits"}, None, None)
        assert create.call_args.kwargs["libraries"] == []
        assert create.call_args.kwargs["library_name"] == "Intro & Credits: all libraries"
```
(Check `execute_scheduled_job`'s positional parameter order — `(schedule_id, library_ids_or_id, library_name, config,
priority, server_id)` per `web/scheduler.py:262` — and patch `get_settings_manager().processing_paused` to False the way
`test_execute_scheduled_job_skipped_when_processing_paused` does if the fixture leaves it unset.)

```python
# tests/markers/test_api_markers_jobs.py
import pytest

from tests.markers.conftest import api_headers as _api_headers


class _FakeJob:
    def __init__(self, kw):
        self.kw = kw

    def to_dict(self):
        return {"id": "x", "kind": "intro_credits"}


@pytest.mark.parametrize(("body", "status"), [
    ({"libraries": [{"server_id": "plex-1", "library_id": "1"}]}, 201),
    ({"file_paths": ["/data/tv/a.mkv"], "priority": "high"}, 201),
    ({}, 201),                                            # all libraries on servers with markers on
    ({"libraries": [{"library_id": "1"}]}, 400),
    ({"libraries": "1"}, 400),
    ({"file_paths": "x"}, 400),
    ({"libraries": [{"server_id": "a", "library_id": "1"}], "file_paths": ["/x"]}, 400),
])
def test_create_marker_job_validation(client, body, status, monkeypatch):
    from media_preview_generator.markers import triggers

    created = []
    monkeypatch.setattr(triggers, "create_intro_credits_job", lambda **kw: created.append(kw) or _FakeJob(kw))
    resp = client.post("/api/markers/jobs", json=body, headers=_api_headers())
    assert resp.status_code == status, resp.get_json()
    if status == 201:
        kw = created[0]
        assert kw["source"] == "manual" and kw["priority"] == (1 if body.get("priority") == "high" else 3)
        assert kw["libraries"] == body.get("libraries", []) and kw["file_paths"] == body.get("file_paths", [])
        assert resp.get_json()["kind"] == "intro_credits"
```

- [ ] **Step 3: Run** → FAIL.

- [ ] **Step 4: Implement `get_or_create_dispatcher`**

```python
# jobs/dispatcher.py (module level, after get_dispatcher)
def get_or_create_dispatcher(config, selected_gpus: list) -> JobDispatcher:
    """Return the shared dispatcher, creating its worker pool from the user's worker settings if needed.

    Same sizing rule the preview scan path uses: GPU workers only when a GPU is selected, CPU workers from
    ``cpu_threads``. An existing pool is reconciled to the current GPU selection.
    """
    existing = get_dispatcher()
    selected = list(selected_gpus or [])
    if existing is not None:
        if selected:
            try:
                existing.worker_pool.reconcile_gpu_workers(selected)
            except Exception as exc:
                logger.debug("Could not reconcile GPU workers: {}", exc)
        return existing
    pool = WorkerPool(
        gpu_workers=int(getattr(config, "gpu_threads", 0) or 0) if selected else 0,
        cpu_workers=int(getattr(config, "cpu_threads", 0) or 0),
        selected_gpus=selected,
    )
    return get_dispatcher(pool)
```
Test (append to `tests/test_job_dispatcher.py`): no dispatcher → pool sized `(gpu_threads if gpus else 0, cpu_threads)`;
existing → returned and `reconcile_gpu_workers` called with the selection; existing + empty selection → not called.

- [ ] **Step 5: Implement `markers/job_runner.py`**

```python
# media_preview_generator/markers/job_runner.py
"""Intro & Credits job thread: gate → enumerate/resolve files → shared dispatcher with marker handlers → complete."""

from __future__ import annotations

import os
import threading
import time

from loguru import logger

from ..config import load_config
from ..job_kinds import JOB_KIND_INTRO_CREDITS
from ..jobs.dispatcher import get_or_create_dispatcher
from ..jobs.orchestrator import _build_multi_server_registry
from ..processing.generator import clear_failures, failure_scope, set_file_result_callback
from ..processing.types import ProcessableItem
from ..web.job_gate import format_wait_message, get_job_gate
from ..web.jobs import JobStatus, WorkerStatus, get_job_manager
from ..web.routes.job_runner import _build_selected_gpus, _format_eta, _inflight_jobs, _inflight_lock
from ..web.settings_manager import get_settings_manager
from .pipeline import build_context, kind_handlers
from .settings import load_server


def build_items(job_config: dict, *, registry, cancel_check=None, progress_callback=None) -> tuple[list[ProcessableItem], list[str]]:
    """Files for a job: explicit paths (webhook/manual/Inspector) or library enumeration.

    Returns:
        Items sorted by season folder then path (so a season's episodes run together), and warnings.
    """
    from ..jobs import orchestrator
    from ..plex_client import _expand_directory_to_media_files

    warnings: list[str] = []
    items: list[ProcessableItem] = []
    file_paths = [str(p).strip() for p in (job_config.get("file_paths") or []) if str(p).strip()]
    if file_paths:
        configs = list(registry.configs())
        mappings = [m for cfg in configs for m in (cfg.path_mappings or [])]
        hints = job_config.get("webhook_item_id_hints") or {}
        for raw in _expand_directory_to_media_files(file_paths, mappings):
            canonical, _matches = orchestrator._resolve_webhook_path_to_canonical(raw, configs, log_resolution=False)
            items.append(ProcessableItem(canonical_path=canonical, server_id="", item_id_by_server=dict(hints.get(raw) or {}),
                                         title=os.path.basename(canonical)))
    else:
        by_server: dict[str, list[str]] = {}
        for entry in job_config.get("libraries") or []:
            by_server.setdefault(str(entry["server_id"]), []).append(str(entry["library_id"]))
        if by_server:
            candidates = [cfg for cfg in registry.configs() if cfg.enabled and cfg.id in by_server]
        else:
            candidates = [cfg for cfg in registry.configs() if cfg.enabled and load_server(cfg.markers, cfg.type.value).enabled]
        pairs, errors = orchestrator._enumerate_items_for_servers(
            candidates,
            enumerate_one=lambda processor, cfg: processor.list_canonical_paths(
                cfg, library_ids=by_server.get(cfg.id), cancel_check=cancel_check, progress_callback=progress_callback
            ),
            cancel_check=cancel_check,
            label="Intro & Credits",
            progress_callback=progress_callback,
        )
        items = [item for _cfg, item in pairs]
        warnings = [f"Couldn't list {name}: {err}" for name, err in errors]
    unique: dict[str, ProcessableItem] = {}
    for item in sorted(items, key=lambda i: (os.path.dirname(i.canonical_path), i.canonical_path)):
        unique.setdefault(item.canonical_path, item)
    return list(unique.values()), warnings


_POLL_S = 1.0
_FINISHED = frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED})


def _wait_for_preceding_job(job_id: str, follows_job_id: str | None, cancel_check) -> bool:
    """Hold a webhook follow-up until its preview job has finished.

    Priority alone can't order them: users can set incoming preview jobs to Normal or Low, and then this job
    (which does less work before the gate) would take the slot first and run before the previews for the same
    files. Runs before the gate, so waiting costs no slot. A preview job counting down to a retry
    (PENDING with ``progress.retry_eta``) counts as finished: its backoff can run for over an hour and markers
    don't need the previews.

    Returns:
        False if the job was cancelled while waiting.
    """
    if not follows_job_id:
        return True
    jm = get_job_manager()
    announced = False
    while True:
        if cancel_check():
            return False
        preceding = jm.get_job(follows_job_id)
        if preceding is None or preceding.status in _FINISHED:
            return True
        progress = getattr(preceding, "progress", None)
        if preceding.status is JobStatus.PENDING and progress is not None and progress.retry_eta:
            return True
        if not announced:
            jm.update_progress(job_id, percent=0, processed_items=0, total_items=0,
                               current_item="Queued — waiting for the preview job for these files to finish")
            announced = True
        time.sleep(_POLL_S)


def _wait_releasing_slot_while_paused(tracker, *, job_id: str, slot: dict, slot_priority: int, cancel_check,
                                      on_wait) -> None:  # on_wait: shows the queue text without touching counters
    """Wait for the tracker; while this job is paused on its own, give its gate slot back.

    A paused job does no work, so holding its slot would block every other job — at max_concurrent_jobs=1 a
    paused backfill would stop all webhook preview jobs. The tracker's pause_check also reads ``slot["held"]``,
    so no item is dispatched between resume and re-admission.
    """
    jm = get_job_manager()
    gate = get_job_gate()
    while not tracker.wait(timeout=_POLL_S):
        if cancel_check():
            continue
        paused = jm.is_pause_requested(job_id)
        if paused and slot["held"]:
            gate.release(slot_priority)
            slot["held"] = False
            jm.add_log(job_id, "INFO - Paused; active slot handed back until resume")
        elif not paused and not slot["held"]:
            # In-flight items can finish the job while its slot is handed back; don't queue a finished job.
            if gate.acquire(priority=slot_priority,
                            cancel_check=lambda: cancel_check() or jm.is_pause_requested(job_id) or tracker.done_event.is_set(),
                            on_wait=on_wait):
                slot["held"] = True


def run_intro_credits_job(job_id: str) -> None:
    """Run one Intro & Credits job to completion (called on its own thread)."""
    from loguru import logger as loguru_logger

    from ..jobs.worker import is_job_thread_for, register_job_thread, unregister_job_thread

    jm = get_job_manager()
    job = jm.get_job(job_id)
    if job is None:
        return
    settings = get_settings_manager()
    if settings.processing_paused:
        logger.info("Intro & Credits job {} not started — processing is paused; job stays pending", job_id)
        return
    register_job_thread(job_id)
    handler_id = loguru_logger.add(
        lambda message: jm.add_log(job_id, f"{message.record['level'].name} - {message.record['message']}"),
        level=str(settings.get("log_level", "INFO")).upper(),
        format="{message}",
        filter=lambda record: is_job_thread_for(record["thread"].id, job_id),
        enqueue=True,
    )
    # Captured once: the user can re-prioritise a running job, and release() must settle at the acquired value.
    slot_priority = job.priority
    slot = {"held": False}
    cfg = dict(job.config or {})
    cancel_check = lambda: jm.is_cancellation_requested(job_id)  # noqa: E731

    def on_wait(active, cap, eff):
        jm.update_progress(job_id, percent=0, processed_items=0, total_items=0,
                           current_item=format_wait_message(active, cap, eff))

    try:
        with failure_scope(job_id):
            try:
                if not _wait_for_preceding_job(job_id, cfg.get("follows_job_id"), cancel_check):
                    jm.add_log(job_id, "WARNING - Job cancelled while waiting for its preview job")
                    jm.cancel_job(job_id)
                    return
                if not get_job_gate().acquire(priority=slot_priority, cancel_check=cancel_check, on_wait=on_wait):
                    jm.add_log(job_id, "WARNING - Job cancelled while waiting for active slot")
                    jm.cancel_job(job_id)
                    return
                slot["held"] = True
                jm.start_job(job_id)
                jm.add_log(job_id, "INFO - Intro & Credits job started")

                def progress_callback(current, total, message, percent_override=None):
                    percent = percent_override if percent_override is not None else ((current / total * 100) if total else 0)
                    jm.update_progress(job_id, percent=percent, processed_items=current, total_items=total,
                                       current_item=message)

                def worker_callback(workers_list):
                    keys = set()
                    for w in workers_list:
                        key = f"{w['worker_type']}_{w['worker_id']}"
                        keys.add(key)
                        remaining = w.get("remaining_time")
                        jm.update_worker_status(key, WorkerStatus(
                            worker_id=w["worker_id"], worker_type=w["worker_type"], worker_name=w["worker_name"],
                            status=w["status"], current_title=w.get("current_title", ""),
                            library_name=w.get("library_name", ""), progress_percent=w.get("progress_percent", 0),
                            speed=w.get("speed", "0.0x"),
                            eta=_format_eta(float(remaining)) if isinstance(remaining, int | float) and remaining > 0 else "",
                            ffmpeg_started=bool(w.get("ffmpeg_started", False)),
                            current_phase=w.get("current_phase", "") or "",
                        ))
                    jm.prune_worker_statuses(keys)
                    jm.emit_worker_statuses()

                config = load_config()
                registry = _build_multi_server_registry(config)
                if registry is None:
                    jm.complete_job(job_id, error="Couldn't load the media servers configuration")
                    return
                ctx = build_context(registry=registry, config=config, priority=job.priority, force=bool(cfg.get("force")))
                items, warnings = build_items(cfg, registry=registry, cancel_check=cancel_check,
                                              progress_callback=progress_callback)
                if not items:
                    note = "; ".join(warnings) if warnings else ""
                    jm.complete_job(job_id, warning=("No files to check. " + note).strip())
                    return
                set_file_result_callback(
                    lambda file_path, outcome, reason, worker, servers=None: jm.record_file_result(
                        job_id, file_path, outcome, reason, worker, servers=servers
                    ),
                    job_id=job_id,
                )
                dispatcher = get_or_create_dispatcher(config, _build_selected_gpus(settings))
                tracker = dispatcher.submit_items(
                    job_id=job_id,
                    items=items,
                    config=config,
                    registry=registry,
                    title_max_width=200,
                    library_name="",
                    callbacks={
                        "progress_callback": progress_callback,
                        "worker_callback": worker_callback,
                        "cancel_check": cancel_check,
                        "pause_check": lambda: (not slot["held"] or jm.is_pause_requested(job_id)
                                                or get_settings_manager().processing_paused),
                    },
                    priority=job.priority,
                    kind=JOB_KIND_INTRO_CREDITS,
                    handlers=kind_handlers(ctx),
                )
                _wait_releasing_slot_while_paused(
                    tracker, job_id=job_id, slot=slot, slot_priority=slot_priority, cancel_check=cancel_check,
                    on_wait=lambda active, cap, eff: jm.update_progress(
                        job_id, current_item=format_wait_message(active, cap, eff)),
                )
                result = tracker.get_result()
                jm.set_job_outcome(job_id, result["outcome"])
                if result["cancelled"] or jm.is_cancellation_requested(job_id):
                    return
                jm.complete_job(job_id, warning=" | ".join(warnings) if warnings else None)
            finally:
                clear_failures()
    except Exception as exc:
        logger.exception("Intro & Credits job {} failed", job_id)
        try:
            jm.complete_job(job_id, error=f"{type(exc).__name__}: {exc}")
        except Exception:
            pass
    finally:
        # Same teardown as the preview runner (web/routes/job_runner.py run_job finally): slot first so the next
        # waiter admits quickly, then per-job flags, then worker cards once nothing else is running.
        if slot["held"]:
            try:
                get_job_gate().release(slot_priority)
            except Exception as exc:
                logger.debug("Could not release job gate for {}: {}", job_id, exc)
            slot["held"] = False
        set_file_result_callback(None, job_id=job_id)
        jm.clear_pause_flag(job_id)
        jm.clear_cancellation_flag(job_id)
        try:
            if not jm.get_running_jobs():
                jm.clear_worker_statuses()
        except Exception as exc:
            logger.debug("Could not clear worker statuses after {}: {}", job_id, exc)
        unregister_job_thread()
        try:
            loguru_logger.complete()
            loguru_logger.remove(handler_id)
        except (ValueError, TypeError):
            pass


def start_intro_credits_job_async(job_id: str, config_overrides: dict | None = None) -> None:
    """Start the job on a daemon thread (duplicate starts for an in-flight job are ignored)."""
    if config_overrides:
        jm = get_job_manager()
        job = jm.get_job(job_id)
        if job is not None:
            jm.update_job_config(job_id, {**(job.config or {}), **config_overrides})
    with _inflight_lock:
        if job_id in _inflight_jobs:
            logger.info("Skipping duplicate Intro & Credits start for {} — already in flight", job_id)
            return
        _inflight_jobs.add(job_id)

    def _run() -> None:
        try:
            run_intro_credits_job(job_id)
        finally:
            with _inflight_lock:
                _inflight_jobs.discard(job_id)

    threading.Thread(target=_run, daemon=True, name=f"run_job_intro_credits_{job_id}").start()
```
`test_submits_items...` asserts `complete_job("j1", warning="Couldn't list X")`; with no warnings the call is
`complete_job("j1", warning=None)`. Webhook follow-ups stay NORMAL (spec §6.4 item 6); ordering behind their preview
job comes from `_wait_for_preceding_job`, not from priority.

- [ ] **Step 6: Delegate from `_start_job_async`** — first lines of `_start_job_async` in `web/routes/job_runner.py`:
```python
    from ...job_kinds import JOB_KIND_INTRO_CREDITS

    queued = get_job_manager().get_job(job_id)
    if queued is not None and queued.kind == JOB_KIND_INTRO_CREDITS:
        from ...markers.job_runner import start_intro_credits_job_async

        start_intro_credits_job_async(job_id, config_overrides)
        return
```
This covers manual resume, pending-drain, and startup requeue, which all call `_start_job_async(job.id, job.config)`.

- [ ] **Step 7: Implement `markers/triggers.py`**

```python
# media_preview_generator/markers/triggers.py
"""Create Intro & Credits jobs from the API, webhook batches and schedules."""

from __future__ import annotations

from loguru import logger

from ..job_kinds import JOB_KIND_INTRO_CREDITS
from ..web.jobs import PRIORITY_NORMAL, Job, get_job_manager
from ..web.settings_manager import get_settings_manager
from .job_runner import start_intro_credits_job_async
from .settings import load_server


def markers_enabled_anywhere() -> bool:
    """Whether any enabled server has Intro & Credits turned on (Plex also needs the DB-write confirmation)."""
    for entry in get_settings_manager().get("media_servers") or []:
        if isinstance(entry, dict) and entry.get("enabled", True):
            if load_server(entry.get("markers"), str(entry.get("type") or "").lower()).enabled:
                return True
    return False


def create_intro_credits_job(
    *,
    library_name: str,
    priority: int,
    source: str,
    libraries: list[dict] | None = None,
    file_paths: list[str] | None = None,
    follows_job_id: str | None = None,
    parent_schedule_id: str = "",
    force: bool = False,
    item_id_hints: dict[str, dict[str, str]] | None = None,
) -> Job:
    """Create and start an Intro & Credits job."""
    jm = get_job_manager()
    job = jm.create_job(
        library_name=library_name,
        config={
            "kind": JOB_KIND_INTRO_CREDITS,
            "source": source,
            "libraries": list(libraries or []),
            "file_paths": list(file_paths or []),
            "follows_job_id": follows_job_id,
            "force": bool(force),
            "webhook_item_id_hints": dict(item_id_hints or {}),
        },
        priority=priority,
        kind=JOB_KIND_INTRO_CREDITS,
        parent_schedule_id=parent_schedule_id,
    )
    start_intro_credits_job_async(job.id)
    logger.info("Created Intro & Credits job {} ({}, source={})", job.id[:8], library_name, source)
    return job


def submit_webhook_follow_up(
    *, preview_job_id: str, paths: list[str], source: str, item_id_hints: dict[str, dict[str, str]] | None = None
) -> str | None:
    """Queue the Intro & Credits job that follows a webhook preview job (NORMAL; the runner waits for that job to finish)."""
    if not paths or not markers_enabled_anywhere():
        return None
    preview = get_job_manager().get_job(preview_job_id)
    name = f"Intro & Credits · {preview.library_name}" if preview is not None and preview.library_name else "Intro & Credits"
    job = create_intro_credits_job(
        library_name=name,
        priority=PRIORITY_NORMAL,
        source=source,
        file_paths=paths,
        follows_job_id=preview_job_id,
        item_id_hints=item_id_hints,
    )
    return job.id
```

- [ ] **Step 8: Webhook and scheduler wiring**

`web/webhooks.py` `_execute_webhook_job`, directly after `_start_job_async(job.id, overrides)`:
```python
        try:
            from ..markers.triggers import submit_webhook_follow_up

            submit_webhook_follow_up(preview_job_id=job.id, paths=list(webhook_paths), source=source)
        except Exception:
            logger.exception("Could not queue the Intro & Credits job that follows webhook job {}", job.id)
```
`create_vendor_webhook_job`, directly after `_start_job_async(job.id, overrides)`:
```python
    try:
        from ..markers.triggers import submit_webhook_follow_up

        hints = overrides.get("webhook_item_id_hints") or None
        submit_webhook_follow_up(preview_job_id=job.id, paths=[canonical_path], source=safe_source, item_id_hints=hints)
    except Exception:
        logger.exception("Could not queue the Intro & Credits job that follows webhook job {}", job.id)
```
`web/scheduler.py` `execute_scheduled_job`, immediately before `if job_type == "recently_added":`:
```python
    if job_type == "intro_credits":
        try:
            from ..markers.triggers import create_intro_credits_job
            from .jobs import PRIORITY_LOW, parse_priority

            libraries = [{"server_id": server_id, "library_id": str(lid)} for lid in (library_ids or [])] if server_id else []
            create_intro_credits_job(
                library_name=f"Intro & Credits: {library_name or 'all libraries'}",
                priority=parse_priority(priority) if priority is not None else PRIORITY_LOW,
                source="schedule",
                libraries=libraries,
                parent_schedule_id=schedule_id,
            )
            manager._update_last_run(schedule_id)
        except Exception:
            logger.exception("Scheduled Intro & Credits job {} could not start", schedule_id)
        return
```
(The D20 "resume a paused job from this schedule" block above it already applies to this job type.)

- [ ] **Step 9: `POST /api/markers/jobs`**

```python
# media_preview_generator/web/routes/api_markers.py
"""Intro & Credits API."""

from __future__ import annotations

from flask import jsonify, request

from ..auth import api_token_required
from ..jobs import PRIORITY_LOW, parse_priority
from . import api


@api.route("/markers/jobs", methods=["POST"])
@api_token_required
def create_marker_job():
    """Start an Intro & Credits job for chosen libraries, chosen files, or every library with markers turned on."""
    from ...markers.triggers import create_intro_credits_job

    data = request.get_json(silent=True) or {}
    libraries = data.get("libraries", [])
    file_paths = data.get("file_paths", [])
    if not isinstance(libraries, list) or not all(
        isinstance(x, dict) and x.get("server_id") and x.get("library_id") for x in libraries
    ):
        return jsonify({"error": "libraries must be a list of {server_id, library_id}"}), 400
    if not isinstance(file_paths, list) or not all(isinstance(p, str) and p.strip() for p in file_paths):
        return jsonify({"error": "file_paths must be a list of paths"}), 400
    if libraries and file_paths:
        return jsonify({"error": "choose libraries or file_paths, not both"}), 400
    count = len(file_paths) or len(libraries)
    default_name = "Intro & Credits: all libraries" if not count else f"Intro & Credits: {count} {'files' if file_paths else 'libraries'}"
    job = create_intro_credits_job(
        library_name=str(data.get("library_name") or default_name),
        priority=parse_priority(data["priority"]) if "priority" in data else PRIORITY_LOW,
        source="manual",
        libraries=[{"server_id": str(x["server_id"]), "library_id": str(x["library_id"])} for x in libraries],
        file_paths=[p.strip() for p in file_paths],
        force=bool(data.get("force", False)),
    )
    return jsonify(job.to_dict()), 201
```
Register the module in `web/routes/__init__.py` imports (`api_markers,  # noqa: F401`). Also check whether
`_config_unwritable_response()` guards job creation in `api_jobs.create_job`; if so, apply the same guard here.

- [ ] **Step 10: Run** `pytest --no-cov tests/markers tests/test_scheduler.py tests/test_job_dispatcher.py <webhook test file> tests/test_routes.py -q` → PASS; full `pytest` → PASS.
- [ ] **Step 11: Commit** — `feat(markers): Intro & Credits job runner, webhook follow-ups, schedules and job API`

---
## Task 13: Markers API — server status, plugin install, source usage, Inspector data, re-detect

Spec §7 items 1–3. Everything the UI tasks read. All routes `@api_token_required` (session or bearer token).

**Files:**
- Modify: `media_preview_generator/web/routes/api_markers.py`, `media_preview_generator/markers/publishers/factory.py`
  (optional `settings=` override)
- Create: `media_preview_generator/markers/inspect.py` (pure payload builders, testable without Flask)
- Test: `tests/markers/test_inspect.py`, `tests/markers/test_api_markers.py`

**Interfaces:**
- Produces:
```python
# markers/publishers/factory.py
def publisher_for(server, config, *, sibling_markers=None, settings: ServerMarkersSettings | None = None) -> MarkerPublisher | None
# markers/inspect.py
def server_status_payload(server, config) -> dict
def item_payload(canonical_path: str, *, registry, store: MarkerStore) -> dict
def resolve_local_path(server, config, item_id: str) -> str | None
```
HTTP:
```
GET  /api/markers/servers/<server_id>/status
  → {"server_id","server_type","enabled": bool,
     "settings": {"enabled","library_ids","plex"?: {...}},
     "capability": {"state","message","details"},            # computed as if enabled, so the tab can show Pass/DB/plugin before turning on
     "can_show": ["intro","credits",...],
     "libraries": [{"id","name","kind","default_selected": bool}]}
POST /api/markers/servers/<server_id>/install-plugin → Jellyfin: install_plugin() result; other types 400
GET  /api/markers/sources/usage → {"theintrodb": {"day","used","limit","remaining","has_key": bool}, "introdb": {...}, "skipdb": {...}}
GET  /api/markers/item?path=<local path> | ?server_id=&item_id=
  → {"known": bool, "canonical_path","duration_ms","is_movie",
     "decisions": {"intro": {"status","reason","marker": {start_ms,end_ms,decided_by,locked}|null,"proposed": {...}|null}, ...},
     "evidence": [{"source","origin","type","start_ms","end_ms","confidence","detail","fetched_at"}],
     "servers": [{"server_id","server_name","server_type","markers_enabled": bool,"capability_state","can_show": [...],
                  "current": [{"type","start_ms","end_ms"}] | null,      # read live from the server now
                  "published": [{"type","start_ms","end_ms"}], "publish_status","publish_message",
                  "plan": "will_add"|"will_replace"|"up_to_date"|"not_enabled"|"nothing_to_publish"}]}
POST /api/markers/item/redetect {"path"} → 202 {"job_id"}     # single-file job, force=True, priority HIGH
```

- [ ] **Step 1: Write failing tests** — `tests/markers/test_inspect.py` (pure functions with `FakeRegistry`, a real
`MarkerStore`, `MagicMock` servers) covering:
  1. `server_status_payload` for Plex disabled-but-configured: capability computed with a preview settings object
     (`enabled=True`, `db_write_confirmed_at="preview"` when unset) — assert `publisher_for` received
     `settings.enabled is True`, and the payload's `enabled` is False; `libraries` marks `Sports` as
     `default_selected: False`, others True; `can_show == ["intro","credits"]`.
  2. Jellyfin READY → `can_show == ["intro","credits","recap","preview"]`.
  3. Emby → `capability.state == "needs_plugin"`, message mentions the Emby plugin, `can_show == ["intro","credits"]`.
  4. `item_payload` for an unknown file → `known: False`, `servers` lists each owning server with
     `plan: "nothing_to_publish"` or `"not_enabled"`.
  5. `item_payload` after a decided + published run (drive the store directly): decisions serialised, evidence rows
     include the empty "looked it up" row (`type: null`), servers: plan `up_to_date` when current == published == decided;
     `will_replace` when current differs (South Park case: Plex current intro 76.5–112.7 s, decided 11–37 s);
     `will_add` when current is empty; `current: null` when the live read fails.
  6. `resolve_local_path` maps the server path through `path_mappings` and returns the first candidate that exists on
     disk; None when none exists.

`tests/markers/test_api_markers.py` (Flask client, `tests/test_routes.py` fixture pattern):
  1. status 404 for an unknown server; 200 shape for a known one (patch `inspect.server_status_payload`, assert it got
     the registry's client + config for that id).
  2. install-plugin: Jellyfin → calls `server.install_plugin()` once and returns its dict; Plex → 400.
  3. usage: returns three sources; `has_key` true only when the stored TheIntroDB key is non-empty; the key itself is
     never in the body.
  4. item: `path` outside every server's allowed roots → 400 (reuse `_validate_path_under_any_server` from `api_bif`);
     `server_id`+`item_id` resolving to nothing → 404; OK → payload from `item_payload`.
  5. redetect: creates a job via `triggers.create_intro_credits_job` with `file_paths=[path]`, `force=True`,
     `priority=1`, `source="inspector"`; invalid path → 400.
  6. Unauthenticated requests → 401 for every route (loop over the five routes).

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement**

Factory change:
```python
def publisher_for(server, config, *, sibling_markers=None, settings=None) -> MarkerPublisher | None:
    settings = settings or load_server(config.markers, config.type.value)
    ...
```

```python
# media_preview_generator/markers/inspect.py
"""Payloads for the server Edit tab and the Inspector tab (no Flask here)."""

from __future__ import annotations

import os

from ..config.paths import expand_path_mapping_candidates
from ..servers.base import ServerType
from .decide import DecisionStatus
from .models import MarkerType
from .publishers.base import Capability
from .publishers.factory import publisher_for
from .settings import ServerMarkersSettings, is_sports_library, load_server, validate_server
from .sources.server_markers import read_server_markers
from .store import MarkerStore

_CAN_SHOW = {
    ServerType.PLEX: ["intro", "credits"],
    ServerType.JELLYFIN: ["intro", "credits", "recap", "preview"],
    ServerType.EMBY: ["intro", "credits"],
}


def _marker_dict(m) -> dict:
    return {"type": m.type.value, "start_ms": m.start_ms, "end_ms": m.end_ms}


def server_status_payload(server, config) -> dict:
    """Status block for one server's Intro & Credits tab."""
    settings = load_server(config.markers, config.type.value)
    preview = ServerMarkersSettings(True, settings.library_ids, settings.db_write_confirmed_at or "preview", settings.on_plex_redetect)
    publisher = publisher_for(server, config, settings=preview)
    if publisher is None:
        capability = {"state": Capability.NEEDS_PLUGIN.value,
                      "message": "Emby needs the Media Preview Bridge for Emby plugin (coming in the next phase)",
                      "details": {}}
    else:
        report = publisher.capability()
        capability = {"state": report.state.value, "message": report.message, "details": report.details}
    block, _err = validate_server(config.markers or None, config.type.value)
    return {
        "server_id": config.id,
        "server_type": config.type.value,
        "enabled": settings.enabled,
        "settings": block,
        "capability": capability,
        "can_show": _CAN_SHOW.get(config.type, []),
        "libraries": [
            {"id": lib.id, "name": lib.name, "kind": lib.kind, "default_selected": not is_sports_library(lib.name, lib.kind)}
            for lib in config.libraries
        ],
    }


def resolve_local_path(server, config, item_id: str) -> str | None:
    """Local canonical path for a server item (first mapped candidate that exists)."""
    remote = server.resolve_item_to_remote_path(item_id)
    if not remote:
        return None
    for candidate in expand_path_mapping_candidates(remote, list(config.path_mappings or [])):
        if os.path.isfile(candidate):
            return candidate
    return None


def item_payload(canonical_path: str, *, registry, store: MarkerStore) -> dict:
    """Decision, evidence and per-server state for one file."""
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
            "marker": ({**_marker_dict(m), "decided_by": list(m.decided_by), "locked": m.locked} if m else None),
            "proposed": ({"start_ms": d.proposed_start_ms, "end_ms": d.proposed_end_ms}
                         if d and d.proposed_start_ms is not None else None),
        }
    if rec:
        payload["evidence"] = [
            {"source": r.source.value, "origin": r.origin, "type": r.type.value if r.type else None, "start_ms": r.start_ms,
             "end_ms": r.end_ms, "confidence": r.confidence, "detail": r.detail, "fetched_at": r.fetched_at}
            for r in store.evidence_rows(rec.id)
        ]
    decided = [m for t, m in markers.items() if decisions.get(t) is None or decisions[t].status is DecisionStatus.DECIDED]
    for match in registry.find_owning_servers(canonical_path):
        cfg = registry.get_config(match.server_id)
        if cfg is None or not cfg.enabled:
            continue
        server = registry.get(cfg.id)
        settings = load_server(cfg.markers, cfg.type.value)
        can_show = set(_CAN_SHOW.get(cfg.type, []))
        wanted = sorted((m for m in decided if m.type.value in can_show), key=lambda m: m.start_ms)
        state = store.get_publish_state(rec.id, cfg.id) if rec else None
        item_id = state.item_id if state and state.item_id else None
        if item_id is None:
            try:
                item_id = server.resolve_remote_path_to_item_id(canonical_path)
            except Exception:
                item_id = None
        current = None
        if item_id:
            found = read_server_markers(server, cfg, item_id, include_ours=True)  # Inspector "current" shows what clients see, ours included (Task 10 review)
            current = None if found is None else [
                {"type": c.type.value, "start_ms": c.start_ms, "end_ms": c.end_ms} for c in found if c.type.value in can_show
            ]
        wanted_dicts = [_marker_dict(m) for m in wanted]
        if not settings.enabled:
            plan = "not_enabled"
        elif not wanted_dicts:
            plan = "nothing_to_publish"
        elif current is not None and _same(current, wanted_dicts, rec.duration_ms if rec else 0):
            plan = "up_to_date"
        elif current:
            plan = "will_replace"
        else:
            plan = "will_add"
        payload["servers"].append({
            "server_id": cfg.id, "server_name": cfg.name, "server_type": cfg.type.value,
            "markers_enabled": settings.enabled, "can_show": sorted(can_show),
            "current": current,
            "published": [_marker_dict(m) for m in (state.markers if state else ())],
            "publish_status": state.status if state else None,
            "publish_message": state.message if state else "",
            "plan": plan,
        })
    return payload


def _same(current: list[dict], wanted: list[dict], duration_ms: int) -> bool:
    # Servers report "credits to the end" differently (Plex final flag, Emby has no end): compare starts, and ends
    # only when both are known and not at the end of the file.
    if len(current) != len(wanted):
        return False
    for c, w in zip(sorted(current, key=lambda x: (x["type"], x["start_ms"])), sorted(wanted, key=lambda x: (x["type"], x["start_ms"]))):
        if c["type"] != w["type"] or abs(c["start_ms"] - w["start_ms"]) > 1000:
            return False
        if c["end_ms"] is not None and w["end_ms"] < duration_ms - 2000 and abs(c["end_ms"] - w["end_ms"]) > 1000:
            return False
    return True
```

Routes (append to `api_markers.py`):
```python
def _registry():
    from ...servers import ServerRegistry
    from ..settings_manager import get_settings_manager

    return ServerRegistry.from_settings(list(get_settings_manager().get("media_servers") or []))


@api.route("/markers/servers/<server_id>/status", methods=["GET"])
@api_token_required
def marker_server_status(server_id):
    """Intro & Credits status for the server Edit dialog."""
    from ...markers.inspect import server_status_payload

    registry = _registry()
    cfg = registry.get_config(server_id)
    if cfg is None:
        return jsonify({"error": "server not found"}), 404
    return jsonify(server_status_payload(registry.get(server_id), cfg))


@api.route("/markers/servers/<server_id>/install-plugin", methods=["POST"])
@api_token_required
def marker_install_plugin(server_id):
    """Install or update the Jellyfin Bridge plugin."""
    from ...servers.base import ServerType

    registry = _registry()
    cfg = registry.get_config(server_id)
    if cfg is None:
        return jsonify({"error": "server not found"}), 404
    if cfg.type is not ServerType.JELLYFIN:
        return jsonify({"error": "plugin install is only available for Jellyfin in this version"}), 400
    return jsonify(registry.get(server_id).install_plugin())


@api.route("/markers/sources/usage", methods=["GET"])
@api_token_required
def marker_source_usage():
    """Today's lookups per online source (TheIntroDB numbers come from its own limit headers)."""
    from datetime import datetime, timezone

    from ...markers.settings import get_global_settings
    from ...markers.sources.ratelimit import get_limiter
    from ...markers.store import get_marker_store

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    settings = get_global_settings()
    out = {}
    for source_id in ("theintrodb", "introdb", "skipdb"):
        usage = get_limiter(source_id).usage()
        stored = get_marker_store().source_usage(source_id, day) or {}
        out[source_id] = {
            "day": day,
            "used": max(usage.get("used") or 0, stored.get("used") or 0),
            "limit": usage.get("limit") if usage.get("limit") is not None else stored.get("limit"),
            "remaining": usage.get("remaining") if usage.get("remaining") is not None else stored.get("remaining"),
            "has_key": bool(source_id == "theintrodb" and (settings.source("theintrodb") or None) and settings.source("theintrodb").api_key),
        }
    return jsonify(out)


def _allowed_path(path: str) -> str | None:
    from .api_bif import _allowed_roots_for_server, _validate_path_under_any_server

    registry = _registry()
    roots = [root for cfg in registry.configs() for root in _allowed_roots_for_server(cfg)]
    return _validate_path_under_any_server(path, roots)


@api.route("/markers/item", methods=["GET"])
@api_token_required
def marker_item():
    """Inspector data for one file."""
    from ...markers.inspect import item_payload, resolve_local_path
    from ...markers.store import get_marker_store

    registry = _registry()
    path = request.args.get("path")
    if not path:
        server_id, item_id = request.args.get("server_id"), request.args.get("item_id")
        cfg = registry.get_config(server_id) if server_id else None
        if cfg is None or not item_id:
            return jsonify({"error": "give path, or server_id and item_id"}), 400
        path = resolve_local_path(registry.get(server_id), cfg, item_id)
        if not path:
            return jsonify({"error": "file not found on disk for that item"}), 404
    safe = _allowed_path(path)
    if not safe:
        return jsonify({"error": "path is not inside any server library"}), 400
    return jsonify(item_payload(safe, registry=registry, store=get_marker_store()))


@api.route("/markers/item/redetect", methods=["POST"])
@api_token_required
def marker_item_redetect():
    """Run Intro & Credits again for one file, ignoring cached lookups."""
    from ...markers.triggers import create_intro_credits_job
    from ..jobs import PRIORITY_HIGH

    data = request.get_json(silent=True) or {}
    safe = _allowed_path(str(data.get("path") or ""))
    if not safe:
        return jsonify({"error": "path is not inside any server library"}), 400
    job = create_intro_credits_job(
        library_name=f"Intro & Credits: {os.path.basename(safe)}",
        priority=PRIORITY_HIGH,
        source="inspector",
        file_paths=[safe],
        force=True,
    )
    return jsonify({"job_id": job.id}), 202
```
(Check the exact signatures of `_allowed_roots_for_server` / `_validate_path_under_any_server` in `api_bif.py:675-720`
and adapt; `import os` at the top of `api_markers.py`.)

- [ ] **Step 4: Run** → PASS. **Step 5: Commit** — `feat(markers): API for server status, source usage, Inspector data and re-detect`

---

## Task 14: UI — server Edit → "Intro & Credits" tab + Plex confirmation

Spec §7.1. Layout and wording: design artifact section "Each server → Edit → Intro & Credits tab" and "Turning on Plex"
(`evidence/design/index.html`). The Servers page cards don't change.

**Files:**
- Modify: `media_preview_generator/web/templates/servers.html` (tab button after `editTabAutomationLi`, pane after
  `edit-tab-automation`, confirmation modal after `#editServerModal`), `media_preview_generator/web/static/js/servers.js`
  (`openEditModal` → `loadMarkersTab(server)`; `tabMap.markers`; `saveEditedServer` → `payload.markers`)
- Create: `media_preview_generator/web/static/js/markers_server_tab.js` (loaded by `servers.html`)
- Test: `tests/e2e/test_intro_credits_server_tab.py`

**Interfaces:**
- Consumes: `GET /api/markers/servers/<id>/status`, `POST /api/servers/<id>/install-plugin` (existing route; Task 13 review ruling), `PUT /api/servers/<id>`
  with `markers` (Task 1).
- Produces (window globals for servers.js): `loadMarkersTab(server)`, `readMarkersFromForm(server) -> object`,
  `markersNeedsPlexConfirmation(server) -> bool`, `confirmPlexMarkers() -> Promise<boolean>`.

- [ ] **Step 1: Markup** — tab button:
```html
<li class="nav-item" id="editTabMarkersLi">
    <button class="nav-link" data-bs-toggle="tab" data-bs-target="#edit-tab-markers" type="button">
        <i class="bi bi-skip-forward me-1"></i>Intro &amp; Credits
    </button>
</li>
```
Pane (ids are the test contract):
```html
<div class="tab-pane fade" id="edit-tab-markers">
    <div class="form-check form-switch mb-1">
        <input class="form-check-input" type="checkbox" id="markersEnabled">
        <label class="form-check-label fw-semibold" for="markersEnabled">Send intro &amp; credits markers to this server</label>
        <button type="button" class="info-icon ms-1" tabindex="0" data-bs-toggle="tooltip"
                title="Markers are detected once per file and shared by every server that has the file. Turn this on to send them here. Detection settings live in Settings → Intro &amp; Credits."><i class="bi bi-info-circle"></i></button>
    </div>
    <div class="small text-muted mb-3">Viewers on this server see Skip Intro / Skip Credits</div>

    <div class="card mb-3"><div class="card-body py-2" id="markersStatusBlock">
        <div class="text-muted small"><span class="spinner-border spinner-border-sm me-1"></span>Checking…</div>
    </div></div>

    <div class="mb-3">
        <div class="fw-semibold small mb-1">Libraries
            <button type="button" class="info-icon ms-1" tabindex="0" data-bs-toggle="tooltip"
                    title="Which libraries get markers on this server. Sports libraries start unticked: no online source covers sports and detection isn't reliable there."><i class="bi bi-info-circle"></i></button>
        </div>
        <div id="markersLibraryList" class="d-flex flex-wrap gap-2"></div>
    </div>

    <div class="mb-2 d-none" id="markersPlexRedetectGroup">
        <div class="fw-semibold small mb-1">If Plex re-detects and replaces our markers
            <button type="button" class="info-icon ms-1" tabindex="0" data-bs-toggle="tooltip"
                    title="Plex's own detection (for example Analyze on an item) replaces markers in its database. 'Put ours back' re-applies ours on the next check; 'Keep Plex's' leaves Plex's markers alone and uses them as evidence."><i class="bi bi-info-circle"></i></button>
        </div>
        <div class="btn-group btn-group-sm" role="group">
            <input type="radio" class="btn-check" name="markersPlexRedetect" id="markersRedetectRestore" value="restore">
            <label class="btn btn-outline-secondary" for="markersRedetectRestore">Put ours back</label>
            <input type="radio" class="btn-check" name="markersPlexRedetect" id="markersRedetectKeep" value="keep_plex">
            <label class="btn btn-outline-secondary" for="markersRedetectKeep">Keep Plex's</label>
        </div>
    </div>
</div>
```
Confirmation modal (copy from the artifact "Turning on Plex", verbatim wording):
```html
<div class="modal fade" id="markersPlexConfirmModal" tabindex="-1" aria-hidden="true">
  <div class="modal-dialog modal-dialog-centered"><div class="modal-content">
    <div class="modal-header"><h5 class="modal-title">Send intro &amp; credits markers to Plex?</h5></div>
    <div class="modal-body small">
      <p>Plex has no way for apps to add intro or credits markers, so they are written straight into Plex's database — the same place Plex stores its own.</p>
      <ul>
        <li>Tested on Plex 1.43. A future Plex update could change this; we stop writing if the database looks different.</li>
        <li>The app must run on the same machine as Plex. <span id="markersPlexConfirmLocal"></span></li>
        <li>If Plex re-detects an item itself, it replaces ours. We put them back on the next check.</li>
        <li>Viewers need Plex Pass (or Plex Home) to see skip buttons.</li>
      </ul>
    </div>
    <div class="modal-footer">
      <button type="button" class="btn btn-secondary" data-bs-dismiss="modal" id="markersPlexConfirmCancel">Cancel</button>
      <button type="button" class="btn btn-primary" id="markersPlexConfirmOk">Enable for Plex</button>
    </div>
  </div></div>
</div>
```

- [ ] **Step 2: JS** (`markers_server_tab.js`, all strings escaped with the existing `escapeHtml` helper exposed on
`window` by servers.js — expose it if it isn't):
  - `loadMarkersTab(server)`: sets `#markersEnabled.checked = !!(server.markers||{}).enabled`; shows
    `#markersPlexRedetectGroup` for Plex with the stored value (default `restore`); renders
    `#markersLibraryList` as pill checkboxes (`.markers-lib-toggle`, `data-id`) — checked when
    `library_ids === null ? lib.default_selected : library_ids.includes(lib.id)` using `status.libraries`; fetches
    status and renders `#markersStatusBlock` rows:
    - Plex: "How markers get here · Written into this Plex server's database ⓘ"; "Plex Pass · ✓ Active / ✕ Not active";
      "Database location · <db dir> · ✓ local disk / ✕ network share (<fs>)"; "Plex's own detection · On — it can
      replace ours; we put them back / Off" (from `details.detection`: any value other than `never`/null = On); when
      `capability.state` is not `ready`, an alert-warning line with `capability.message`.
    - Jellyfin: "How markers get here · Media Preview Bridge plugin"; "Plugin · <version> ✓ / Update needed [Update] /
      Not installed [Install]" (button `#markersInstallPluginBtn` → POST install-plugin, spinner, then re-fetch status
      after 20 s since Jellyfin restarts); "Can show · Intro · credits · recap · preview".
    - Emby: "How markers get here · Media Preview Bridge for Emby plugin"; "Plugin · Not available yet" (phase 2);
      "Can show · Intro · credits start (no credits end)".
    - Fetch failure → "Couldn't check this server right now" with a Retry link.
    - Call `window._initBootstrapTooltips(pane)` after rendering.
  - `readMarkersFromForm(server)`: `{enabled, library_ids, plex?: {db_write_confirmed_at, on_plex_redetect}}` where
    `library_ids` is `null` if the ticked set equals the default selection, else the ticked ids;
    `db_write_confirmed_at` = stored value, or `server._markersConfirmedAt` set by the confirmation flow.
  - `markersNeedsPlexConfirmation(server)`: Plex && switch on && no stored/just-given confirmation.
  - `confirmPlexMarkers()`: shows the modal, fills `#markersPlexConfirmLocal` from the last status (`Checked: local disk
    ✓` or `Checked: network share ✕ — writes will stay off`), resolves true on OK (sets
    `server._markersConfirmedAt = new Date().toISOString()`), false on Cancel/close (and unticks the switch).
- `servers.js`: in `openEditModal` after `renderEditExcludePaths(...)`: `if (window.loadMarkersTab) loadMarkersTab(server);`;
  add `markers: 'edit-tab-markers'` to `tabMap`; in `saveEditedServer` before the PUT:
```js
if (window.markersNeedsPlexConfirmation && markersNeedsPlexConfirmation(server)) {
    const ok = await confirmPlexMarkers(server);
    if (!ok) { saveBtn.disabled = false; saveBtn.innerHTML = orig; return; }
}
if (window.readMarkersFromForm) payload.markers = readMarkersFromForm(server);
```
  The switch's `change` handler also triggers `confirmPlexMarkers` immediately for Plex (so the question appears when
  the user flips it, as in the mockup); Save re-checks in case the modal was dismissed.

- [ ] **Step 3: E2E tests** (`tests/e2e/test_intro_credits_server_tab.py`, `@pytest.mark.e2e`, `authed_page`, mocks via
`page.route`), each asserting the request body the page sends (not just that it sent one):
  1. Plex, status READY (pass ✓, ext4, detection `scheduled`): tab shows "Written into this Plex server's database",
     "✓ Active", "local disk", "On"; libraries: Movies ✓, TV Shows ✓, Sports ✕.
  2. Flip the switch on a Plex server → confirmation modal text visible → Cancel → switch off, no PUT.
  3. Flip → Enable for Plex → Save → captured PUT body `markers == {"enabled": true, "library_ids": null,
     "plex": {"db_write_confirmed_at": <ISO string>, "on_plex_redetect": "restore"}}`.
  4. Untick TV Shows → Save → `library_ids == ["<movies id>"]`; choose "Keep Plex's" → `on_plex_redetect == "keep_plex"`.
  5. Already-confirmed Plex server → flip on → no modal → PUT carries the stored confirmation.
  6. Jellyfin NEEDS_PLUGIN → Install button → POST `/api/servers/jf-1/install-plugin` captured.
  7. Jellyfin PLUGIN_OUTDATED → "Update needed" + Update button; READY → version and "Intro · credits · recap · preview".
  8. Emby → "Not available yet"; saving still sends `markers.enabled` as toggled (no plex block).
  9. Plex NEEDS_LOCAL_DB → warning line with the message; confirmation modal says "network share ✕".
  10. Status endpoint 500 → "Couldn't check this server right now".
  11. Save with the tab never opened → PUT still carries the markers block unchanged from the server (no accidental reset).
Run: `pytest -m e2e -n 0 --no-cov tests/e2e/test_intro_credits_server_tab.py` → PASS.

- [ ] **Step 4: Visual check** — run the app locally with a lab-style `settings.json` (Plex + Jellyfin + Emby entries),
screenshot the tab for each vendor with Playwright (`page.screenshot`) and compare with the mockup; fix spacing/wording
drift. Attach the three screenshots to the PR in Task 20.
- [ ] **Step 5: Commit** — `feat(ui): Intro & Credits tab in the server Edit dialog with Plex database-write confirmation`

---

## Task 15: UI — Settings → "Intro & Credits" section

Spec §7.2; artifact "Settings → Intro & Credits (shared)". Only shared detection settings live here.

**Files:**
- Modify: `media_preview_generator/web/templates/settings.html` (sidebar link after Processing Options, section card),
  `loadSettings()` / `saveAllSettings()` in the same template
- Test: `tests/e2e/test_intro_credits_settings.py`, extend `tests/e2e/test_journey_settings_save_reload.py`

- [ ] **Step 1: Markup** (`#section-markers`, sidebar `<a class="nav-link" href="#section-markers"><i class="bi bi-skip-forward me-2"></i>Intro &amp; Credits</a>`):
  - "What to detect": three switches `#markersDetectIntro` ("Intros" · muted "TV episodes"), `#markersDetectCredits`
    ("Credits" · "TV episodes and movies"), `#markersDetectRecap` ("Recaps" · "Jellyfin only"), each with ⓘ:
    intros — "Found from chapters, online databases and (soon) by matching the theme tune across a season.";
    credits — "Found from chapters and online databases; on-screen credit text detection arrives in a later update.";
    recaps — "Only Jellyfin shows a Skip Recap button. Needs chapters or an online source."
  - "Publish when" segmented radio `name="markersPublishWhen"`: `#markersPublishHigh` "High" (value `high`),
    `#markersPublishMedium` "Medium" (`medium`); muted "Stricter means fewer, safer markers"; ⓘ "High: chapters in the
    file, or two independent sources agree (intro end within 5 s, credits start within 10 s). Medium: also accepts a
    single source that passes sanity checks. Anything else shows as Needs review."
  - `#markersRespectLocks` "Never overwrite my edits" ⓘ "Markers you adjust or lock in the Inspector always win and are
    never replaced by detection."
  - "Where evidence comes from" ordered list `#markersSourceList` of `li.markers-source[data-id]` rows with a drag
    handle (`⋮⋮`), a switch `.markers-source-enabled`, name, muted cost line, ⓘ, and ▲/▼ buttons
    (`.markers-source-up/.markers-source-down`, keyboard-accessible; drag-and-drop uses native HTML5 DnD). Copy:
    - chapters: "Chapters inside the file" · "Free · exact when present" · ⓘ "Streaming releases often name chapters
      Intro, Title Sequence or End Credits. About 1 in 9 seasons in a sampled library."
    - theintrodb: "Online: TheIntroDB" · key input `#markersTheIntroDbKey` (password type, placeholder "(optional)") ·
      usage line `#markersTheIntroDbUsage` "83 of today's lookups used · limit set by TheIntroDB" (from
      `/api/markers/sources/usage`; "—" when unknown) · ⓘ "When it has data: intros 35 of 43 right, credits 23 right /
      4 wrong. Has data for about 1 in 4 TV episodes and 1 in 100 movies. Works without a key (500 lookups a day); your
      own free key raises that. Used without the site's written permission — keys can be revoked."
    - introdb: "Online: IntroDB.app" · "No key needed · TV only" · ⓘ "Intros 26 of 43 right, credits 17 right / 4 wrong
      on a verified set. Agreement with TheIntroDB counts as one vote because its data looks partly copied."
    - skipdb: "Online: SkipDB" · "Free lookups · matched to your file's length" · ⓘ "Only answers matched to the length
      of your file are used. Intros 16 of 43 right on a verified set, so it mostly serves as a second opinion."
    - season_audio: "Matching audio across a season" · "CPU · about 2 s per episode" · ⓘ "Coming in the next update:
      finds the theme tune episodes share (77% right, 11% wrong on 118 test episodes)."
    - credits_text: "On-screen credit text" · "About 10–30 s per file" · ⓘ "Coming in a later update: finds the start
      of the credit roll from on-screen text (within 10 s on 59 of 80 test files)."
    - server_markers: "Markers already on your servers" · "Second opinion, never copied as-is" · ⓘ "Plex's own markers
      are sometimes wrong (for example ~80 s late on South Park S01), so they can only confirm another source."
  (Wording note: the artifact said "SkipDB · Daily download · no lookups"; the build uses SkipDB's read API instead —
  see §14 2026-09-13. This is the one intentional copy change; call it out in the PR screenshots.)

- [ ] **Step 2: JS** in `settings.html`: `loadSettings()` fills the controls from `settings.markers` (reorders
`#markersSourceList` to match `sources` order; key input shows `****` placeholder when masked, value empty);
`saveAllSettings()` adds:
```js
markers: {
    detect: {intro: $('#markersDetectIntro').checked, credits: $('#markersDetectCredits').checked, recap: $('#markersDetectRecap').checked},
    publish_when: document.querySelector('input[name="markersPublishWhen"]:checked')?.value || 'high',
    respect_locks: $('#markersRespectLocks').checked,
    sources: [...document.querySelectorAll('#markersSourceList .markers-source')].map((li) => {
        const entry = {id: li.dataset.id, enabled: li.querySelector('.markers-source-enabled').checked};
        if (li.dataset.id === 'theintrodb') {
            const typed = $('#markersTheIntroDbKey').value.trim();
            entry.api_key = typed || (li.dataset.hasKey === '1' ? '****' : '');
        }
        return entry;
    }),
},
```
Reordering (drag or ▲/▼) must trigger autosave: dispatch a `change` event on the list after a move. A "Clear key"
link sets `data-has-key="0"`, empties the input and saves (so `api_key: ""`).

- [ ] **Step 3: E2E** (`mock_settings_get` with a markers block; capture POST `/api/settings`):
  1. Defaults render: intros ✓ credits ✓ recaps ✕, High selected, locks ✓, order chapters → server_markers, TheIntroDB off.
  2. Toggle Recaps → POST body `markers.detect.recap == true` and every other key unchanged.
  3. Move SkipDB to the top with ▲ (twice/thrice) → POST `sources[0].id == "skipdb"`, all 7 ids present once.
  4. Masked key: settings has `api_key: "****"` → saving any other change posts `api_key: "****"` (server keeps key).
  5. Type a new key → posts it; "Clear key" → posts `""`.
  6. Usage line shows "83 of today's lookups used" from a mocked usage endpoint; unknown → "—".
  7. Keyboard: focus ▼ on Chapters, press Enter → order changes (accessibility).
  8. Journey (backend-real): change Publish when to Medium + reorder → reload page → state persisted.
- [ ] **Step 4: Commit** — `feat(ui): Settings → Intro & Credits (detection, publish rule, ordered sources, TheIntroDB key)`

---

## Task 16: UI — jobs: start modal, schedules, queue rows, Files panel, per-job pause

Spec §7.5; artifact "Dashboard → job queue" and "How it fits with previews".

**Files:**
- Modify: `media_preview_generator/web/templates/index.html` (Start job modal job-type choice; `#fileOutcomeFilter`
  options), `media_preview_generator/web/static/js/app.js` (`showNewJobModal`, `startNewJob`, `STATUS_META`,
  `_buildOutcomeTooltip`, `_renderJobFileIssues`, `updateJobQueue` row rendering, `_renderPublishersBlock`),
  `media_preview_generator/web/static/js/job_modal.js` (`_fileOutcomeMeta`, header chips),
  `media_preview_generator/web/templates/_automation_schedules.html` + `static/js/schedule_modal.js` + `schedules.js`
- Test: `tests/e2e/test_intro_credits_jobs_ui.py`, extend `tests/e2e/test_schedules.py`

- [ ] **Step 1: Start job modal** — above the library picker add radio `name="jobKind"`: `#jobKindPreviews` "Previews"
(checked) and `#jobKindMarkers` "Intro & Credits" ⓘ "Finds Skip Intro / Skip Credits markers for the chosen libraries
and sends them to every server with Intro & Credits turned on. Runs at low priority on the same workers as previews."
When Intro & Credits is chosen: hide the "processing mode" (missing only / regenerate) and sort controls, show a
"Re-check files already done" checkbox `#jobMarkersForce`, default priority select to Low. `startNewJob()` branches:
```js
if (document.getElementById('jobKindMarkers').checked) {
    const libraries = [...document.querySelectorAll('.job-library-checkbox:checked')]
        .map((cb) => ({server_id: cb.dataset.serverId, library_id: cb.value}));
    const allTicked = document.getElementById('jobLibraryAll').checked;
    const body = {libraries: allTicked ? [] : libraries, priority: Number($('#jobPriority').value),
                  force: $('#jobMarkersForce').checked, library_name: <same label logic as previews>};
    return postJson('/api/markers/jobs', body);   // existing fetch helper used by startNewJob
}
```
(Check the checkbox `value` attribute holds the library id; use the attribute the existing code reads.)

- [ ] **Step 2: Queue rows and details** — `app.js`:
  - `STATUS_META` entries: `markers_published` "Markers written" (success), `markers_up_to_date` "Up to date"
    (secondary), `markers_needs_review` "Needs review" (warning), `markers_none` "No markers found" (secondary),
    `markers_no_owners` "No server with Intro & Credits on" (secondary), `markers_written` "Markers written" (success),
    `markers_skipped` "Skipped" (secondary), `markers_waiting` "Waiting" (info). `markers_up_to_date`, `markers_none`,
    `markers_skipped` and `markers_waiting` are used both as file outcomes and as per-server row statuses (one label
    each; precedence in `markers.outcomes.file_outcome`).
  - Rows with `job.kind === 'intro_credits'`: a small "Intro & Credits" badge before the library name
    (`<span class="badge text-bg-dark me-1 job-kind-badge">Intro &amp; Credits</span>`); when
    `job.config.follows_job_id` matches a job in the current list, render the row directly under that job with a `↳`
    prefix and muted "follows <short id>" text; otherwise render normally.
  - `_renderPublishersBlock` for `intro_credits` jobs renders per server: `counts` in order written → up to date →
    needs review → waiting → skipped → no markers → failed, each "Label × N"; for a server whose every row is `markers_skipped`
    with one shared message, append " · <message>". Skip the frame-source (Generated/Reused) badges for this kind.
  - "Sources:" footer line is phase 2 (needs per-source counts); do not add a placeholder.
  - `_buildOutcomeTooltip` and `_renderJobFileIssues`: include the new outcome keys.
  - Pause/Resume buttons: for `intro_credits` jobs the tooltip says "Pause this job" (per-job) instead of "Pause all
    processing"; the click handler calls the same endpoints (the server decides per kind).
- `job_modal.js`: `_fileOutcomeMeta` new keys; header chip "Intro & Credits" for the kind; the Files table "Servers"
  pills show `markers_*` statuses with the `STATUS_META` labels.
- `index.html` `#fileOutcomeFilter`: add the new outcome options.

- [ ] **Step 3: Schedules** — `_automation_schedules.html`: third `scanMode` radio `#scanModeMarkers` value
`intro_credits` "Intro & Credits" with ⓘ "Checks the chosen libraries for intro and credits markers. Files already done
are skipped. Low priority unless you pick otherwise."; `schedule_modal.js::onScanModeChange` hides lookback + sort for
it; `saveSchedule()` sends `config: {job_type: 'intro_credits'}`; `showEditScheduleModal` restores it;
`schedules.js` list badge "Intro & Credits".

- [ ] **Step 4: E2E** (mocked `/api/jobs`, `/api/markers/jobs`, `/api/schedules`):
  1. Start job → Intro & Credits → tick two libraries on one server → POST `/api/markers/jobs` body
     `{"libraries": [{"server_id": "plex-1", "library_id": "1"}, {"server_id": "plex-1", "library_id": "2"}], "priority": 3, "force": false, ...}`;
     "All libraries" → `libraries: []`; Previews still posts to `/api/jobs` with the old body (regression).
  2. Queue with a preview job `e64567e1` and an `intro_credits` job whose `config.follows_job_id == "e64567e1"` → the
     marker row renders immediately after it with "↳" and "follows e64567e1"; publishers block shows "Markers written × 8",
     "Needs review × 3", and "Skipped × 11 · Emby plugin not installed".
  3. Files panel filter "Needs review" → request carries `outcome=markers_needs_review`.
  4. Pause on an Intro & Credits job → POST `/api/jobs/<id>/pause` → the global "Resume all" banner does not appear
     (mock returns the job with `paused: true` and processing state unpaused).
  5. Schedule modal: Intro & Credits mode → POST body `config.job_type == "intro_credits"`, no `lookback_hours`;
     edit round-trips the mode.
- [ ] **Step 5: Commit** — `feat(ui): Intro & Credits jobs in the start modal, queue, files panel and schedules`

---

## Task 17: UI — Inspector → "Intro & Credits" tab (read-only)

Spec §7.3; artifact "Inspector → Intro & Credits tab". Phase 1 is read-only plus Re-detect; Adjust/Lock/Publish arrive
in phase 4 and are not shown.

**Files:**
- Modify: `media_preview_generator/web/templates/bif_viewer.html` (tabs inside `#viewerPanel`, pane, keep `item_id` +
  `server_id` + `media_file` from `renderResults`/`loadPreviewFromResult`)
- Create: `media_preview_generator/web/static/js/markers_inspector.js`, `media_preview_generator/web/static/css/pages/markers_inspector.css`
- Test: `tests/e2e/test_intro_credits_inspector.py`

- [ ] **Step 1: Markup** — inside `#viewerPanel` after `#metaBadges`:
```html
<ul class="nav nav-tabs mb-3" id="inspectorTabs" role="tablist">
  <li class="nav-item"><button class="nav-link active" data-bs-toggle="tab" data-bs-target="#inspector-tab-frames" type="button">Frames</button></li>
  <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#inspector-tab-markers" type="button" id="inspectorMarkersTabBtn">Intro &amp; Credits</button></li>
</ul>
<div class="tab-content">
  <div class="tab-pane fade show active" id="inspector-tab-frames"><!-- existing row :92-137 moves here unchanged --></div>
  <div class="tab-pane fade" id="inspector-tab-markers">
    <div class="d-flex justify-content-between align-items-center mb-2 flex-wrap gap-2">
      <div class="small text-muted" id="markersInspectorPath"></div>
      <button class="btn btn-sm btn-outline-secondary" id="markersRedetectBtn"><i class="bi bi-arrow-repeat me-1"></i>Re-detect</button>
    </div>
    <div id="markersInspectorBody"></div>
  </div>
</div>
```
- [ ] **Step 2: JS** — `markers_inspector.js` exposes `loadMarkersInspector({server_id, item_id, media_file})`, called when
the tab is shown (lazy; cache per item). Renders from `GET /api/markers/item`:
  - Two zoom windows: "Opening · 0:00 – 3:00" and "Ending · <duration−3:00> – <duration> (end of file)" (for movies:
    ending window only). Each window: a time ruler (4 ticks) and lanes: "Decision", then one lane per evidence source
    present (Chapters "<chapter title>", TheIntroDB, IntroDB, SkipDB, "<server name> now" from `servers[].current`),
    each lane a bar positioned by `left = (start − windowStart)/windowLen`, width by duration, clamped to the window
    with an arrow "→" when the segment runs past it. Disagreeing lanes (not within the §5.5 tolerance of the decision)
    get a ✕ and the class `lane-disagree`. Lane labels show times "0:11–0:37" / "21:39 →".
  - Status chips per type: "Intro 0:11–0:37" (decided), "Needs review", "No markers found", "Detection off".
  - "On your servers" list: one row per `servers[]` with vendor icon letter, `server_name`, plan label
    (`will_add` "Will add", `will_replace` "Will replace", `up_to_date` "Up to date", `not_enabled` "Intro & Credits
    off", `nothing_to_publish` "Nothing to send yet") and a muted detail line (e.g. "Intro 1:16–1:52 → 0:11–0:37",
    "Emby has no “credits end”" for Emby, `publish_message` when failed/waiting/skipped).
  - `known: false` → "Not checked yet" + Re-detect button enabled.
  - Re-detect → POST `/api/markers/item/redetect` `{path}` → toast "Queued — see the Dashboard" with the job id link.
  - All text via `textContent`/escaped templates.
- [ ] **Step 3: E2E** (mocked search result with `item_id`, mocked `/api/markers/item`):
  1. South Park S01E03 fixture (decided intro 11 000–37 000 from chapters; Plex current 76 508–112 748; TheIntroDB end
     35 000; credits decided 1 299 000 → end; Plex credits 1 265 000–1 297 000) → Decision lane "0:11–0:37"; "Plex now"
     lane has `lane-disagree`; Plex row "Will replace" with "Intro 1:16–1:52 → 0:11–0:37"; Jellyfin "Will add".
  2. Needs-review fixture → chip "Needs review", proposed bar dashed (`lane-proposed`).
  3. Movie fixture → only the Ending window renders.
  4. `known: false` → "Not checked yet"; Re-detect posts `{"path": "<media_file>"}` and shows the toast.
  5. The Frames tab still works exactly as before (existing inspector e2e tests stay green; run them).
  6. The markers request is only made after the tab is opened (network spy: zero calls on page load).
- [ ] **Step 4: Commit** — `feat(ui): read-only Intro & Credits tab in the Preview Inspector`

---

## Task 18: Docs

**Files:** `docs/reference.md` (settings keys, per-server block, API endpoints from Tasks 12–13, job kinds, outcome
keys), `docs/guides.md` (new "Intro & Credits" guide: what it does, per-server switch, Plex database write + Plex Pass +
same-machine rule, Jellyfin plugin install/update, Emby status, sources and the publish rule, Needs review, sports
libraries, TheIntroDB key, troubleshooting table keyed by capability state), `README.md` (feature bullet),
`jellyfin-plugin/README.md` (markers endpoints, two ABI builds), `docs/design/intro-credits/spec.md` §14 (dated lines for
every decision taken during phase 1: SkipDB read API; server-markers evidence read from every owning server; Plex writes
only decided types; per-job pause scope), §0 status.

- [ ] **Step 1:** Write the docs. Every capability `state` from Task 8/10 gets a troubleshooting row ("What you see",
"Why", "What to do").
- [ ] **Step 2:** `grep -n "intro_credits\|markers" docs/reference.md` covers every route in `api_markers.py`
(`grep -n "@api.route" media_preview_generator/web/routes/api_markers.py`) — no route undocumented.
- [ ] **Step 3: Commit** — `docs: Intro & Credits guide, reference and plugin notes`

---

## Task 19: Lab end-to-end (storage) — the phase-1 "done when"

Spec §10.3, §3 wipe matrix, §13 items 3, 6, 8. Owner checkpoint: **Plex claim token** (ask the owner for
https://plex.tv/claim right before step 2; it expires in 4 minutes).

**Files:**
- Create: `docs/design/intro-credits/evidence/lab/app.sh` (bring up `mlab-app` from the branch image),
  `docs/design/intro-credits/evidence/lab/synth_chapters.sh` (VP9/Opus synth episodes with named chapters),
  `docs/design/intro-credits/evidence/lab/phase1_matrix.py` (API-driven matrix runner, writes JSON results),
  `docs/design/intro-credits/evidence/lab/phase1-results.md` (human summary; raw JSON gitignored)
- Modify: `docs/design/intro-credits/evidence/lab/up.sh` (mount the new synth folder; Plex also mounts
  `mlab_plex_config` path unchanged)

- [ ] **Step 1: Build the branch image locally** (storage, foreground, low priority):
```bash
cd /home/data/workspace/plex_generate_vid_previews
VER=$(/home/data/.venv/bin/python -m setuptools_scm)
nice -n 19 docker build --build-arg SETUPTOOLS_SCM_PRETEND_VERSION="$VER" -t media_preview_generator:intro-credits . > /tmp/claude-1000/build-intro-credits.log 2>&1
tail -3 /tmp/claude-1000/build-intro-credits.log
```
- [ ] **Step 2: Claim the lab Plex** — ask the owner for a claim token, then
`PLEX_CLAIM=claim-xxxx ./up.sh recreate` (volumes kept) and confirm `myPlexSubscription="1"` at
`http://127.0.0.1:32402/`. Record Plex's marker pref ids from `/:/prefs` (fix Task 8's pref names if they differ).
- [ ] **Step 3: Synth episodes with chapters** — `synth_chapters.sh` creates `lab/synth/Synth Chapters (2021)/Season 01/`
with 3 × 2-minute VP9/Opus episodes, chapters `Chapter 1` / `Intro` (0:10–0:40) / `Chapter 2` / `Credits` (1:40–2:00),
and different intro offsets per episode; add the folder to `up.sh` mounts, rescan the lab libraries on all four servers.
- [ ] **Step 4: App container** — `app.sh` runs:
```bash
docker run -d --name mlab-app --network mlab -p 127.0.0.1:18080:8080 \
  -e PUID=1000 -e PGID=1000 -e TZ=UTC -e WEB_AUTH_TOKEN="$MLAB_APP_TOKEN" \
  -v mlab_app_config:/config -v mlab_plex_config:/plexcfg \
  "${MV[@]}" media_preview_generator:intro-credits
```
(`MV` = the same read-only media mounts as `up.sh`; `MLAB_APP_TOKEN` in `lab/env`.) Configure it through its API:
servers Plex (`http://mlab-plex:32400`, config folder
`/plexcfg/Library/Application Support/Plex Media Server`), Jellyfin 10.11, Jellyfin 12.0, Emby; libraries refreshed;
Intro & Credits on for Plex (with confirmation), both Jellyfins; TheIntroDB enabled without key.
- [ ] **Step 5: Run the matrix** (`phase1_matrix.py`, each row = pass/fail + evidence):
  1. Backfill job on Synth Chapters + Rick and Morty S01 → job completes; outcome counts; per-server rows.
  2. Plex API `includeMarkers=1` for each synth episode shows intro/credits equal to the chapters (credits start
     exact after the −2 s DB shift); Rick and Morty episodes decided by TheIntroDB+IntroDB/SkipDB agreement show
     markers; episodes without agreement show none and the job lists them as Needs review.
  3. Both Jellyfins `/MediaSegments/{id}` show Intro/Outro for the same files (ticks match).
  4. Second backfill → all "Up to date", zero writes (Plex DB `taggings` rowids unchanged, plugin files unchanged mtime).
  5. Wipe matrix on Plex: metadata refresh (force), section scan, Analyze with detection off, non-forced detection →
     markers still served; forced credits detection (`PUT …/credits?force=1`) → Plex replaces ours (expected; reconcile
     is phase 2 — record the behaviour).
  6. Wipe matrix on both Jellyfins: library scan, refresh with ReplaceAllMetadata, Media Segment Scan task, restart → kept.
  7. File change: touch a synth file's mtime (it's in our lab folder, not `/data`) → next job re-probes and republishes.
  8. Multi-version item: add a second encode of one synth episode to the same Plex item folder (lab synth folder) →
     Plex publish waits until both decided, then writes both parts; Jellyfin alternate version check (§13 item 6).
  9. Webhook: POST a Sonarr-style payload for one synth episode to `mlab-app` → preview job HIGH then Intro & Credits
     job NORMAL with `follows_job_id`; dashboard shows the linked row.
  10. Per-job pause: pause a running Intro & Credits backfill → previews webhook job still runs → resume.
  11. Plex DB on a network share: bind the Plex config through an NFS-style mount type is not reproducible in the lab
      without root; instead run the capability endpoint with a fake mountinfo via a unit test (already covered) and
      record "not lab-tested".
  12. Plex client display (§13 item 3): open Plex Web (`http://127.0.0.1:32402/web`) with Playwright signed in as the
      claimed account is not possible headless without credentials → ask the owner to open one synth episode in any
      Plex app and confirm Skip Intro appears; record the answer.
  13. Jellyfin 10.11 and 12.0 web players: `lab/jf_client.py` shows Skip Intro on a synth episode (our segment).
  14. Security: every `/api/markers/*` route without auth → 401; path traversal on `/api/markers/item?path=/etc/passwd` → 400.
  15. Resource: during the Rick and Morty backfill, `docker stats mlab-app` peak CPU and RSS recorded; checking threads
      don't exceed the TheIntroDB pace (count requests in app logs per 10 s ≤ 30).
- [ ] **Step 6:** Write `phase1-results.md` (table: row, result, evidence link/snippet). Any failure → back to the owning
task (systematic-debugging first), fix, re-run the whole matrix.
- [ ] **Step 7:** Remove the Plex lab server from the owner's account only if the owner asks (they want the lab kept).
- [ ] **Step 8: Commit** — `test(markers): phase 1 lab matrix scripts and results`

---

## Task 20: PR — body, image, CI, side-by-side on `plex`

Spec §11. Owner checkpoint before touching the `plex` host.

- [ ] **Step 1:** Merge `origin/dev` into the branch if GitHub reports the PR out-of-date; run the full suite:
`/home/data/.venv/bin/python -m pytest` and `pytest -m e2e -n 8 --no-cov` → both PASS (paste counts in the PR).
- [ ] **Step 2:** Update PR #241's title to `feat: Intro & Credits (skip markers) for Plex, Jellyfin and Emby` and body:
Summary (what users get), phase status checklist (phase 1 done items; phases 2–4 pending), how it works (link spec),
screenshots (Task 14–17), lab results table (Task 19), test counts, known limitations (Emby publishing in phase 2;
reconcile in phase 2; season audio phase 2; credit text phase 3), and how to try the PR image. Keep the PR draft.
- [ ] **Step 3:** Add label `build-docker` → `ghcr.io/stevezau/media_preview_generator:pr-241` builds; wait for the
workflow, confirm the image digest and that the container starts (`docker run --rm ... --entrypoint python3 <image> -c
"import media_preview_generator.markers.pipeline"`).
- [ ] **Step 4:** Ask the owner: "Run `pr-241` as a second container on `plex` against one real show?" Only after yes:
second container with its own config dir (`/config/plex-generate-previews-pr241`), port 127.0.0.1:18081, flags from
`/home/data/scripts/containers/plex.sh` minus the port/config mounts; Plex writes stay OFF until the owner enables them
for a server; Jellyfin/Emby as the owner chooses. Record what the owner saw in `phase1-results.md`.
- [ ] **Step 5:** Phase-1 close-out: update spec §0 status + §14, tick the phase-1 boxes in `plan-roadmap.md`, write
`plan-phase2.md` with writing-plans against the code as it exists, commit and push.
