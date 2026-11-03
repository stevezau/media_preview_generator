"""Setup Health for the independently enabled local Plex loudness writer."""

from __future__ import annotations

import html
from typing import Any, cast

from loguru import logger

from .base import NATIVE_MODES, Library, MediaServer, ServerConfig

# Plex's own server-wide "Analyze audio tracks for loudness": one setting for every library, music included.
PLEX_LOUDNESS_PREF = "LoudnessAnalysisBehavior"
_LIBRARY_PREF = "enableLoudnessAnalysis"
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


def _library_action(libraries_on: list[Library]) -> dict[str, Any]:
    titles = ", ".join(html.escape(lib.name) for lib in libraries_on)
    return {
        "action": "set_plex_loudness_library_off",
        "args": {"library_ids": [lib.id for lib in libraries_on]},
        "confirm": {
            "kind": "button",
            "phrase": "",
            "body": (
                f"Turns off Plex's <em>Enable Loudness Analysis</em> in {titles} (Library settings → Advanced). "
                "Plex keeps analysing music and every other library. Existing measurements, this app's included, "
                "stay in use for playback leveling. You can turn it back on in each library's settings."
            ),
        },
    }


def _plex_library_state(server: MediaServer, selected: list[Library]) -> tuple[list[Library], bool | None]:
    """The selected libraries Plex's own analysis is still on for, and whether Plex has other libraries.

    Other libraries are music, or movie and TV libraries this app doesn't measure; photo libraries don't count.
    A library whose switch can't be read counts as still on. The flag is None when Plex's library list can't be read.
    """
    from ..plex_client import retry_plex_call
    from . import plex_scheduled_jobs

    try:
        sections = retry_plex_call(cast(Any, server)._connect().library.sections, max_retries=0)
    except Exception as exc:
        logger.debug("Plex library list unavailable for {} ({})", server.id, type(exc).__name__)
        return list(selected), None
    selected_ids = {lib.id for lib in selected}
    has_other = any(
        section_type == "artist" or (section_type in ("movie", "show") and str(section.key) not in selected_ids)
        for section in sections
        for section_type in [str(getattr(section, "type", "") or "")]
    )
    try:
        switches = plex_scheduled_jobs.read_library_prefs(cast(Any, server), sections)
    except Exception as exc:
        logger.debug("Plex library loudness switches unavailable for {} ({})", server.id, type(exc).__name__)
        switches = {}
    still_on = [lib for lib in selected if switches.get(lib.id, {}).get(_LIBRARY_PREF) is not False]
    return still_on, has_other


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
            library_kinds={lib.id: lib.kind for lib in server_config.libraries},
        )
        if not reason and not cast(dict, block)["enabled"]:
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
            "tooltip": "Checks Plex's database and server identity without changing them.",
            "explanation": (
                "<p>Loudness requires Plex's config folder mounted locally on the same machine, or a Plex database node "
                "(version 1.2.0 or newer) beside a Plex on another machine. The database must match the connected "
                "server and supported Plex version.</p>"
                "<p>Intro &amp; Credits can stay off.</p>"
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
    workers_gate = ready and bool(selected) and workers_ready
    still_on: list[Library] = []
    has_other: bool | None = False
    if known and not plex_off and ready and selected:
        still_on, has_other = _plex_library_state(server, selected)
    off_in_libraries = not plex_off and ready and bool(selected) and not still_on and known
    analysis_ok = plex_off or off_in_libraries
    if known:
        native_reason: str | None = None
        if not analysis_ok:
            if still_on:
                where = ", ".join(lib.name for lib in still_on)
                native_reason = (
                    f"Plex also analyses loudness on its own schedule in {where}, repeating this app's work on the "
                    "Plex machine's CPU."
                )
            else:
                native_reason = (
                    "Plex also analyses loudness on its own schedule, repeating this app's work on the Plex "
                    "machine's CPU."
                )
            fix_name = "Turn off in this app's libraries" if has_other else "Set to Never"
            if not ready:
                native_reason += f" {fix_name} is unavailable until this app can store loudness measurements."
            elif not selected:
                native_reason += (
                    f" Choose at least one movie or TV library for this app's loudness analysis to use {fix_name}."
                )
            elif not workers_ready:
                native_reason += f" Configure CPU workers for Loudness with hours outside global quiet hours before using {fix_name}."
            if has_other is None:
                native_reason += " Couldn't read Plex's libraries. Try Setup Health again."
        actions: dict[str, Any] = {}
        fix_label = "Set to Never"
        recommended = "Never"
        if not analysis_ok and workers_gate and has_other is not None:
            if has_other:
                actions = {"disable": _library_action(still_on)}
                fix_label = "Turn off in this app's libraries"
            else:
                actions = {"disable": _NEVER_ACTION}
        if has_other and selected:
            recommended = f"Off in {', '.join(lib.name for lib in selected)}"
        current = NATIVE_MODES[cast(str, mode)]
        if still_on and not plex_off:
            current += f"; on in {', '.join(lib.name for lib in still_on)}"
        if plex_off:
            label = "Plex's own loudness analysis is off"
        elif off_in_libraries:
            label = "Plex's own loudness analysis is off in this app's libraries"
        else:
            label = "Plex also analyses loudness"
        checks.append(
            {
                "id": "loudness_plex_analysis",
                "label": label,
                "docs_anchor": "plex-loudness",
                "tooltip": (
                    "Plex's server-wide Analyze audio tracks for loudness setting, or its per-library Enable "
                    "Loudness Analysis switch in this app's libraries."
                ),
                "explanation": (
                    "<p>Plex's <em>Analyze audio tracks for loudness</em> schedule applies server-wide, including music. "
                    "For video libraries with Plex's <em>Enable Loudness Analysis</em> on, Plex analyses the same files "
                    "this app does.</p>"
                    "<p>Either setting stops that repeat work. Never stops Plex's analysis everywhere, so music loudness "
                    "leveling and smart transitions lose new measurements. Turning off <em>Enable Loudness Analysis</em> "
                    "in this app's libraries leaves music and every other library on Plex's schedule. "
                    "Existing measurements stay available either way.</p>"
                ),
                "ok": analysis_ok,
                "severity": "recommended",
                "current": current,
                "recommended": recommended,
                "reason": native_reason,
                "help_url": "/settings#section-workers" if not workers_ready else None,
                "help_label": "Configure CPU workers" if not workers_ready else None,
                "actions": actions,
                "fix_action": "disable",
                "fix_label": fix_label,
                # Changes Plex's own settings: never part of a bulk fix.
                "bulk": False,
                "meta": {"flag": PLEX_LOUDNESS_PREF},
            }
        )
    return {
        "id": "plex_loudness",
        "title": "Plex loudness",
        "docs_anchor": "plex-loudness",
        "ok": ready and (analysis_ok or not known),
        "severity": "recommended" if ready else "critical",
        "checks": checks,
    }
