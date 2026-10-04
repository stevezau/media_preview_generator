"""Setup Health rows for opt-in Plex chapter thumbnails."""

from __future__ import annotations

from typing import Any

from loguru import logger

from .base import MediaServer, ServerConfig

CHAPTER_PREF = "GenerateChapterThumbBehavior"
_NATIVE_MODES = {
    "never": "Never",
    "scheduled": "As a scheduled task",
    "asap": "As a scheduled task and when media is added",
}


def chapter_readiness_section(
    server: MediaServer, server_config: ServerConfig | None, preferences: dict[str, Any]
) -> dict[str, Any] | None:
    """Check chapter registration prerequisites and competing native generation.

    Args:
        server: Plex client being checked.
        server_config: Persisted server configuration, including the opt-in.
        preferences: Existing server-wide preference response; never queried again here.

    Returns:
        A readiness section, or None when chapter generation is disabled.
    """
    if server_config is None or server_config.output.get("chapter_thumbnails") is not True:
        return None

    from .plex_chapters import chapter_capability

    try:
        capability = chapter_capability(server, server_config)
        ready = capability.ready
        reason = capability.message
    except Exception as exc:
        logger.debug("Chapter readiness probe failed for {} ({})", server.id, type(exc).__name__)
        ready = False
        reason = "Could not check chapter registration. Check the Plex connection and try Setup Health again."

    registration = {
        "id": "chapter_registration",
        "label": "Chapter thumbnails can be registered" if ready else "Chapter thumbnails cannot be registered",
        "docs_anchor": "chapter-thumbnails",
        "tooltip": "Checks the local Plex database or the configured Plex helper without changing it.",
        "explanation": (
            "<p>This app writes chapter JPEGs and updates the image references on Plex's existing chapters. "
            "It does not create chapters or change their timing.</p>"
            "<p>The database must be local to the writer and belong to this Plex server. "
            "When Plex runs on another machine, configure a compatible Plex helper beside it. "
            "Intro &amp; Credits can stay off.</p>"
        ),
        "ok": ready,
        "severity": "recommended" if ready else "critical",
        "reason": None if ready else reason,
        "actions": {},
        "meta": {},
    }
    if not ready:
        registration["note"] = {
            "text": (
                "The Plex helper connection is shared by chapter thumbnails and Intro & Credits. "
                "You can configure it without enabling Intro & Credits."
            ),
            "configure_plex_helper": True,
        }

    mode = preferences.get(CHAPTER_PREF)
    known = isinstance(mode, str) and mode in _NATIVE_MODES
    native_off = known and mode == "never"
    if not known:
        label = "Could not check Plex's chapter setting"
        native_reason = "Check Plex Settings → Library → Generate chapter thumbnails."
    elif native_off:
        label = "Plex's own chapter generation is off"
        native_reason = None
    else:
        label = "Plex also generates chapter thumbnails"
        native_reason = (
            "Plex may repeat this app's work and replace its chapter images. "
            "Review Generate chapter thumbnails in Plex Settings → Library; "
            "Never stops Plex's own generation for all libraries."
        )
    native = {
        "id": "chapter_native_generation",
        "label": label,
        "docs_anchor": "chapter-thumbnails",
        "tooltip": "Plex's server-wide Generate chapter thumbnails setting.",
        "explanation": (
            "<p>This is separate from Plex's video preview thumbnail setting. Both scheduled modes "
            "let Plex generate chapter images itself. Never stops that native work; "
            "it does not switch off this app's chapter generation.</p>"
            "<p>This check does not change Plex settings. A change in Plex affects all its libraries.</p>"
        ),
        "ok": native_off,
        "severity": "recommended",
        "current": _NATIVE_MODES[mode] if known else "Unable to verify",
        "recommended": "Never",
        "reason": native_reason,
        "actions": {},
        "meta": {"flag": CHAPTER_PREF},
    }
    return {
        "id": "chapter_thumbnails",
        "title": "Chapter thumbnails",
        "docs_anchor": "chapter-thumbnails",
        "ok": ready and native_off,
        "severity": "recommended" if ready else "critical",
        "checks": [registration, native],
    }
