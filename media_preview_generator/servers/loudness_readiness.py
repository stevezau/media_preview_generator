"""Setup Health for the independently enabled local Plex loudness writer."""

from __future__ import annotations

from typing import Any

from loguru import logger

from .base import NATIVE_MODES, MediaServer, ServerConfig

# Plex's own server-wide "Analyze audio tracks for loudness": one setting for every library, music included.
PLEX_LOUDNESS_PREF = "LoudnessAnalysisBehavior"
_NEVER_ACTION = {
    "action": "set_plex_loudness_never",
    "args": {},
    "confirm": {
        "kind": "button",
        "phrase": "",
        "body": (
            "Sets Plex's server-wide <em>Analyze audio tracks for loudness</em> to Never (Plex Settings → Library). "
            "Plex stops creating loudness measurements in every library, including music and unselected movie and TV "
            "libraries. This app analyses only its chosen movie and TV libraries and respects your exclusions. "
            "Keep Plex's analysis on if you need it for those other files, music loudness leveling or smart transitions. "
            "Existing measurements remain available. You can restore Plex's schedule in Plex Settings → Library."
        ),
    },
}


def loudness_readiness_section(
    server: MediaServer, server_config: ServerConfig | None, preferences: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Report whether the selected Plex database can receive loudness measurements.

    Args:
        server: Plex client whose identity must match the local database.
        server_config: Saved configuration, including the independent loudness opt-in.
        preferences: Plex's server prefs by id (``/:/prefs``); its own loudness analysis gets a row when known.

    Returns:
        A health section, or None when loudness is disabled.
    """
    if server_config is None:
        return None
    from ..loudness.guard import loudness_capability
    from ..loudness.settings import loudness_libraries, validate_server_loudness

    try:
        block, reason = validate_server_loudness(
            server_config.loudness,
            server_config.type.value,
            server_config.markers,
            library_kinds={lib.id: lib.kind for lib in server_config.libraries},
        )
        if not reason and not block["enabled"]:
            return None
        ready = False
        if not reason:
            report = loudness_capability(server, server_config)
            ready, reason = report.ready, report.message
    except Exception as exc:
        logger.debug("Loudness readiness probe failed for {} ({})", server.id, type(exc).__name__)
        ready = False
        reason = "Could not check loudness analysis. Check the Plex connection and try Setup Health again."
    checks: list[dict[str, Any]] = [
        {
            "id": "loudness_registration",
            "label": "Loudness measurements can be stored" if ready else "Loudness measurements cannot be stored",
            "docs_anchor": "plex-loudness",
            "tooltip": "Checks the local Plex database and server identity without changing them.",
            "explanation": (
                "<p>Loudness requires Plex and this app on the same machine, with Plex's config folder mounted "
                "locally. The database must match the connected server and supported Plex version.</p>"
                "<p>Intro &amp; Credits can stay off. The Plex helper does not support loudness analysis.</p>"
            ),
            "ok": ready,
            "severity": "recommended" if ready else "critical",
            "reason": None if ready else reason,
            "actions": {},
            "meta": {},
        }
    ]
    mode = (preferences or {}).get(PLEX_LOUDNESS_PREF)
    known = isinstance(mode, str) and mode in NATIVE_MODES
    plex_off = mode == "never"
    selected = loudness_libraries(server_config) if ready else []
    from ..web.settings_manager import peek_settings_manager
    from ..worker_groups import effective_worker_groups, future_capacity

    settings = peek_settings_manager()
    saved = settings.get_all() if settings is not None else {}
    workers_ready = bool(future_capacity(effective_worker_groups(saved), saved.get("quiet_hours"), "loudness"))
    can_disable = ready and bool(selected) and workers_ready and not plex_off
    if known:
        unavailable = None
        if not plex_off and not ready:
            unavailable = "Set to Never is unavailable until this app can store loudness measurements."
        elif not plex_off and not selected:
            unavailable = (
                "Choose at least one movie or TV library for this app's loudness analysis to use Set to Never."
            )
        elif not plex_off and not workers_ready:
            unavailable = (
                "Configure CPU workers for Loudness with hours outside global quiet hours before using Set to Never."
            )
        checks.append(
            {
                "id": "loudness_plex_analysis",
                "label": "Plex's own loudness schedule",
                "docs_anchor": "plex-loudness",
                "tooltip": "Plex's server-wide Analyze audio tracks for loudness setting, for every library, music too.",
                "explanation": (
                    "<p>Plex's <em>Analyze audio tracks for loudness</em> schedule applies server-wide, including music. "
                    "For video libraries with Plex's <em>Enable Loudness Analysis</em> on, native analysis can overlap "
                    "this app's work. Keeping Plex's schedule enabled is supported.</p>"
                    "<p>This app analyses only its selected movie and TV libraries and respects your exclusions. "
                    "Keep Plex's analysis on if you need it for unselected files, music loudness leveling or smart "
                    "transitions. Setting Never is optional and leaves existing measurements available.</p>"
                ),
                "ok": True,
                "severity": "info",
                "informational": True,
                "current": NATIVE_MODES[mode],
                "reason": unavailable,
                "help_url": "/settings#section-workers" if not workers_ready else None,
                "help_label": "Configure CPU workers" if not workers_ready else None,
                "actions": {"disable": _NEVER_ACTION} if can_disable else {},
                "optional_action": "disable",
                "optional_label": "Set to Never",
                # Server-wide, music included: never part of a bulk fix.
                "bulk": False,
                "meta": {"flag": PLEX_LOUDNESS_PREF},
            }
        )
    return {
        "id": "plex_loudness",
        "title": "Plex loudness",
        "docs_anchor": "plex-loudness",
        "ok": ready,
        "severity": "recommended" if ready else "critical",
        "checks": checks,
    }
