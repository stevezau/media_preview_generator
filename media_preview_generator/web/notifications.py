"""Notification center source registry.

Assembles the list of active system notifications shown in the dashboard
bell-icon dropdown.  Each notification has a stable ``id`` so users can
dismiss it permanently (persisted in ``settings.json`` via
``settings_manager.dismissed_notifications``) without being unsuppressed
when the warning body evolves between releases.

Current sources:

- ``vulkan_software_fallback`` — Dolby Vision Profile 5 wrong-colour
  thumbnail warning, sourced from ``api_vulkan._get_vulkan_info``.
- ``timezone_misconfigured`` — container is running UTC without an
  explicit TZ env var, sourced from ``api_system._get_timezone_info``.
- ``gpu_keeps_failing_<device>`` — one per GPU whose last files all ran on
  the CPU, sourced from ``jobs.gpu_fallback``.
- ``credits_on_cpu_<device>`` — one per GPU whose credit text self-test failed, so its credits are read on the CPU,
  sourced from ``markers.credits.textdet_helper``.
- ``library_health_not_showing`` / ``library_health_check_failed`` — from the
  last Library health check (stored results; nothing is re-counted here).

Session-only dismissals live in ``_SESSION_DISMISSED`` (process memory,
cleared on restart); permanent dismissals live in ``settings.json``.
"""

from __future__ import annotations

import os
import re
import threading
from typing import Any

from loguru import logger

from ..jobs.gpu_fallback import GPU_FALLBACK_STREAK, get_gpu_fallback_tracker, is_gpu_notification_id, notification_id

VULKAN_SOFTWARE_FALLBACK_ID = "vulkan_software_fallback"
TIMEZONE_MISCONFIGURED_ID = "timezone_misconfigured"
NVIDIA_DRIVER_MISSING_ID = "nvidia_driver_missing"
SCHEMA_MIGRATION_ID = "schema_migration_completed"
DEPRECATED_IMAGE_ID = "deprecated_docker_image_name"
MEDIA_MOUNT_UNHEALTHY_ID = "media_mount_unhealthy"
LIBRARY_HEALTH_NOT_SHOWING_ID = "library_health_not_showing"
LIBRARY_HEALTH_CHECK_FAILED_ID = "library_health_check_failed"

CREDITS_ON_CPU_ID_PREFIX = "credits_on_cpu_"
# ``node:<node id>:<kind>``, one per node in an alert state; the kind is a key of ``_NODE_CARD_TEXT``.
NODE_ID_PREFIX = "node:"
# The ``<state>`` of a Plex database node's card when it reports a new id its key must confirm.
NODE_UNCONFIRMED = "unconfirmed"

# Cards that can be hidden until the next restart but never for good: the problem they report comes and goes, and a
# permanent dismissal would hide the next occurrence too. An id stored by an older version is ignored.
SESSION_ONLY_DISMISSAL_IDS = frozenset({MEDIA_MOUNT_UNHEALTHY_ID, LIBRARY_HEALTH_CHECK_FAILED_ID})


def is_session_only_dismissal(notification_id_value: str) -> bool:
    """Whether a card can only be hidden until the next restart (the GPU cards are per device, so not a fixed id)."""
    return (
        notification_id_value in SESSION_ONLY_DISMISSAL_IDS
        or is_gpu_notification_id(notification_id_value)
        or notification_id_value.startswith(CREDITS_ON_CPU_ID_PREFIX)
        or notification_id_value.startswith(NODE_ID_PREFIX)
    )


# Image names recognised by the deprecation banner. The deprecated image was
# retired — a one-shot tombstone image now sits on its tags. This banner still
# fires for anyone running the last real build of the old image (no auto-update
# yet) to push them to switch to the canonical name.
DEPRECATED_IMAGE_NAME = "stevezzau/plex_generate_vid_previews"
CANONICAL_IMAGE_NAME = "stevezzau/media_preview_generator"


_SESSION_DISMISSED: set[str] = set()
_SESSION_DISMISSED_LOCK = threading.Lock()


def _session_is_dismissed(notification_id: str) -> bool:
    with _SESSION_DISMISSED_LOCK:
        return notification_id in _SESSION_DISMISSED


def dismiss_session(notification_id: str) -> None:
    """Mark a notification as dismissed for the current process only.

    Cleared on container restart.  Used by the "Dismiss" button in the
    bell-icon dropdown when users want to hide a notification now but
    might want to see it again next time the app restarts.
    """
    with _SESSION_DISMISSED_LOCK:
        _SESSION_DISMISSED.add(notification_id)
    logger.debug("notifications: session-dismissed {}", notification_id)


def undismiss_session(notification_id: str) -> None:
    """Clear a session-dismissed notification (used by reset button)."""
    with _SESSION_DISMISSED_LOCK:
        _SESSION_DISMISSED.discard(notification_id)


def reset_session() -> None:
    """Clear all session-only dismissals."""
    with _SESSION_DISMISSED_LOCK:
        _SESSION_DISMISSED.clear()


def _vulkan_failures() -> list[tuple[dict[str, Any], Any]]:
    """The detected GPUs that can't use Vulkan, each with its check; empty when none is known to fail."""
    from ..gpu.vulkan_check import get_vulkan_checks
    from .routes._helpers import _ensure_gpu_cache
    from .routes.api_vulkan import _diagnose_vulkan_environment

    try:
        gpus = list(_ensure_gpu_cache())
        checks = get_vulkan_checks(gpus, _diagnose_vulkan_environment)
    except Exception as exc:  # noqa: BLE001 - the card still shows, with the general text
        logger.debug("Couldn't check why Vulkan is unavailable for the notification: {}", exc)
        return []
    return [(gpu, check) for gpu, check in zip(gpus, checks, strict=True) if check is not None and not check.ok]


def _vulkan_card_body(failures: list[tuple[dict[str, Any], Any]]) -> str:
    """The bell card's body: what this means, why, what to do (plain text escaped; NVIDIA keeps its detailed HTML)."""
    from markupsafe import escape

    from ..markers.credits.textdet_helper import _WEBGPU_VENDORS
    from .routes.api_vulkan import _diagnose_vulkan_environment, _nvidia_body_html

    several = len(failures) > 1
    effects = []
    if any(gpu.get("type") in _WEBGPU_VENDORS for gpu, _ in failures):
        effects.append("Credits text is read on the CPU: just as accurate, slower.")
    effects.append("Dolby Vision Profile 5 thumbnails get a green and purple tint.")
    effects.append("Previews and intros still use the GPU.")
    html = "<strong>What this means</strong><ul>" + "".join(f"<li>{escape(e)}</li>" for e in effects) + "</ul>"

    why_items, fix_items = [], []
    for gpu, check in failures:
        name = escape(gpu.get("name") or gpu.get("type") or "GPU")
        if check.cause.startswith("nvidia_"):
            detail = _nvidia_body_html(check.cause, _diagnose_vulkan_environment(), gpu.get("name") or "NVIDIA GPU")
            why_items.append(detail)
            continue
        fix = (
            "Nothing, if the CPU keeps up. A host OS update with a newer kernel fixes it."
            if (check.cause == "kernel_too_old")
            else check.fix
        )
        why_items.append(f"{f'<strong>{name}:</strong> ' if several else ''}{escape(check.why)}")
        fix_items.append(f"{f'<strong>{name}:</strong> ' if several else ''}{escape(fix)}")
    html += "<strong>Why</strong>" + _paragraph_or_list(why_items)
    if fix_items:
        html += "<strong>What to do</strong>" + _paragraph_or_list(fix_items)
    return html


def _paragraph_or_list(items: list[str]) -> str:
    """One item as a paragraph, several as a list: a one-bullet list reads as if something is missing."""
    if len(items) == 1:
        return f"<p class='mb-2'>{items[0]}</p>"
    return "<ul>" + "".join(f"<li>{item}</li>" for item in items) + "</ul>"


def _build_vulkan_software_fallback_notification() -> dict[str, Any] | None:
    """Return a notification dict when the Vulkan probe fell back to software.

    Returns ``None`` when Vulkan is healthy (no warning needed).  When a GPU's check says why it can't use Vulkan, the
    card names that GPU and the cause. Otherwise (no GPU known to fail) it keeps the general Dolby Vision text from
    ``api_vulkan._get_vulkan_info``.
    """
    # Deferred import to avoid circular import (api_vulkan imports from
    # gpu_detection which imports loguru which... keep it lazy).
    from .routes.api_vulkan import _get_vulkan_info

    info = _get_vulkan_info()
    warning_html = info.get("warning")
    if not warning_html:
        return None

    card = {
        "id": VULKAN_SOFTWARE_FALLBACK_ID,
        "severity": "warning",
        "title": "Dolby Vision Profile 5 thumbnails will have the wrong colours",
        "body_html": warning_html,
        "dismissable": True,
        "source": "vulkan_probe",
        "device": info.get("device"),
    }
    failures = _vulkan_failures()
    if not failures:
        return card
    if len(failures) == 1:
        gpu, check = failures[0]
        card["title"] = (
            f"{gpu.get('name') or gpu.get('type')} can't use Vulkan: {check.title[:1].lower()}{check.title[1:]}"
        )
    else:
        card["title"] = f"Vulkan isn't working on {len(failures)} GPUs"
    card["body_html"] = _vulkan_card_body(failures)
    card["action"] = {"href": "/settings#section-processing", "label": "Open GPU check"}
    return card


def _build_nvidia_driver_missing_notification() -> dict[str, Any] | None:
    """Return a notification dict when an NVIDIA GPU is passed in without its driver (issue #419), else None."""
    from markupsafe import escape

    from ..gpu.detect import NVIDIA_DRIVER_MISSING_FIX, NVIDIA_DRIVER_MISSING_WHY, nvidia_gpu_without_driver

    if not nvidia_gpu_without_driver():
        return None
    return {
        "id": NVIDIA_DRIVER_MISSING_ID,
        "severity": "warning",
        "title": "NVIDIA GPU passed in without its driver",
        "body_html": (
            f"<p class='mb-2'>{escape(NVIDIA_DRIVER_MISSING_WHY)}</p>"
            f"<strong>What to do</strong><p class='mb-0'>{escape(NVIDIA_DRIVER_MISSING_FIX[:1].upper())}"
            f"{escape(NVIDIA_DRIVER_MISSING_FIX[1:])}</p>"
        ),
        "dismissable": True,
        "source": "gpu_detection",
    }


def _build_timezone_misconfigured_notification() -> dict[str, Any] | None:
    """Return a notification dict when the container is running UTC without TZ.

    Reuses the warning HTML produced by ``api_system._get_timezone_info``
    so there's a single source of truth for the remediation text.
    """
    from .routes.api_system import _get_timezone_info

    info = _get_timezone_info()
    warning_html = info.get("warning")
    if not warning_html:
        return None

    return {
        "id": TIMEZONE_MISCONFIGURED_ID,
        "severity": "warning",
        "title": "Timezone not configured",
        "body_html": warning_html,
        "dismissable": True,
        "source": "timezone_probe",
    }


def _build_schema_migration_notification() -> dict[str, Any] | None:
    """One-shot card shown after run_migrations actually moved the schema.

    Reads ``_pending_migration_notice`` from settings.json — set by
    ``upgrade._migrate_schema`` — and renders a friendly "we migrated your
    config" card with a pointer at the .bak file. Dismissing the card
    clears the flag so it never reappears (see ``dismiss_schema_migration_notice``).
    """
    from .settings_manager import get_settings_manager

    notice = get_settings_manager().get("_pending_migration_notice")
    if not isinstance(notice, dict):
        return None
    from_v = notice.get("from")
    to_v = notice.get("to")
    backup = notice.get("backup") or ""
    # Name the backup only while it is on disk: later saves prune old backups, and notices written by older
    # versions name a file that was never created.
    if not (isinstance(backup, str) and os.path.isfile(backup)):
        backup = ""
    notes = notice.get("notes") or []
    notes_html = ""
    if isinstance(notes, list) and notes:
        # cursor: pointer is wired via .notification-bell-wrapper .notification-details summary
        # in style.css — inline style would be stripped by sanitizeNotificationHtml.
        notes_html = (
            "<details class='mt-2'><summary class='small text-muted'>"
            "What changed</summary><ul class='small mb-0'>"
            + "".join(f"<li>{n}</li>" for n in notes)
            + "</ul></details>"
        )
    backup_html = (
        f"<div class='small mt-2'>A backup of your previous settings is at "
        f"<code>{backup}</code> &mdash; safe to ignore unless you need to roll back.</div>"
        if backup
        else ""
    )
    # A retry boot finishes a migration whose schema bump already happened,
    # so there is no version move to report — printing "from v14 to v14" (or
    # "from v? to v?") reads like a bug. Lead with the change itself instead.
    if from_v is None or to_v is None or from_v == to_v:
        headline = "<p class='mb-0'>Your settings were updated.</p>"
    else:
        headline = (
            f"<p class='mb-0'>Your settings were migrated from schema "
            f"<strong>v{from_v}</strong> to <strong>v{to_v}</strong>.</p>"
        )
    body = f"{headline}{backup_html}{notes_html}"
    return {
        "id": SCHEMA_MIGRATION_ID,
        "severity": "info",
        "title": "Settings migrated",
        "body_html": body,
        "dismissable": True,
        "source": "schema_migration",
    }


def dismiss_schema_migration_notice() -> None:
    """Clear the one-shot migration flag from settings.

    Called by the notification dismiss endpoint when the user closes the
    "Settings migrated" card. Survives a restart (the flag stays gone),
    unlike session-only dismissals.
    """
    from .settings_manager import get_settings_manager

    sm = get_settings_manager()
    if sm.get("_pending_migration_notice") is not None:
        sm.set("_pending_migration_notice", None)


def _build_deprecated_image_notification() -> dict[str, Any] | None:
    """Banner shown when the running image is the deprecated Docker name.

    The Dockerfile bakes ``DOCKER_IMAGE_NAME`` at build time via a build
    arg; CI sets it to ``stevezzau/plex_generate_vid_previews`` for the
    deprecated mirror image and to ``stevezzau/media_preview_generator``
    for the canonical image. Local dev builds default to ``"local"``.
    Only fires for the deprecated value so users on the canonical name
    (and dev builds) never see it.
    """
    image_name = (os.environ.get("DOCKER_IMAGE_NAME") or "").strip()
    if image_name != DEPRECATED_IMAGE_NAME:
        return None

    body = (
        f"<p class='mb-1'>You're running the Docker image "
        f"<code>{DEPRECATED_IMAGE_NAME}</code>, which has been <strong>retired</strong> "
        f"and renamed to <code>{CANONICAL_IMAGE_NAME}</code> (this app now supports "
        f"Plex, Emby, and Jellyfin).</p>"
        f"<p class='mb-1'>The old image name <strong>no longer receives updates</strong>. "
        f"Update your <code>compose</code> file&apos;s <code>image:</code> line to "
        f"<code>{CANONICAL_IMAGE_NAME}</code> and run "
        f"<code>docker compose pull &amp;&amp; docker compose up -d</code> to keep "
        f"getting updates.</p>"
        f"<p class='mb-0 small text-muted'>Existing volumes, settings, and "
        f"configuration are unchanged — only the image name moves.</p>"
    )
    return {
        "id": DEPRECATED_IMAGE_ID,
        "severity": "warning",
        "title": "Your Docker image has been retired",
        "body_html": body,
        "dismissable": True,
        "source": "image_deprecation",
    }


def _build_unhealthy_media_mounts_notification() -> dict[str, Any] | None:
    """Dashboard banner when a configured media mount looks unmounted.

    Mirrors the startup WARNING (``app._warn_unhealthy_media_mounts``)
    into the bell dropdown so the operator sees the problem in the UI,
    not just the log. An ``empty`` mount is the stale-bind-mount
    signature that made job be0151d2 fail as "missing on disk" for
    files that existed on the host. See
    project_stale_bindmount_missing_on_disk.
    """
    from ..config import detect_unhealthy_media_mounts
    from .settings_manager import get_settings_manager

    try:
        servers = get_settings_manager().get("media_servers", []) or []
    except Exception as exc:
        logger.debug("media-mount notification skipped: {}", exc)
        return None
    if not isinstance(servers, list):
        return None

    all_mappings: list[dict] = []
    for server in servers:
        if isinstance(server, dict):
            all_mappings.extend(server.get("path_mappings") or [])

    issues = detect_unhealthy_media_mounts(all_mappings)
    if not issues:
        return None

    from markupsafe import escape

    rows = "".join(
        f"<li><code>{escape(i['path'])}</code> — "
        f"{'exists but is empty (volume not mounted?)' if i['issue'] == 'empty' else 'does not exist'}</li>"
        for i in issues
    )
    body = (
        "<p class='mb-1'>One or more configured media paths look unusable inside this container. "
        "Preview jobs for files on these disks will fail as <em>“missing on disk”</em> even when the "
        "files exist on the host:</p>"
        f"<ul class='mb-1'>{rows}</ul>"
        "<p class='mb-0 small text-muted'>This usually means the volume isn’t mounted (stale "
        "bind-mount / unmounted network share). Recreate the container after the share is mounted, "
        "or use slave bind-mount propagation, then re-check Settings → Media Servers.</p>"
    )
    return {
        "id": MEDIA_MOUNT_UNHEALTHY_ID,
        "severity": "warning",
        "title": "Media mount looks unmounted",
        "body_html": body,
        "dismissable": True,
        "permanent_dismissable": False,
        "source": "media_mount_health",
    }


def _credits_on_cpu_devices() -> list[dict[str, Any]]:
    try:
        from ..markers.credits.textdet_helper import credits_on_cpu_devices

        return credits_on_cpu_devices()
    except Exception as exc:  # noqa: BLE001 - a card source must never break the bell
        logger.debug("Couldn't read the credits-on-CPU devices for notifications: {}", exc)
        return []


def _credits_on_cpu_notification_id(key: str) -> str:
    return CREDITS_ON_CPU_ID_PREFIX + re.sub(r"[^A-Za-z0-9_.-]+", "_", key)


def _build_credits_on_cpu_notifications() -> list[dict[str, Any]]:
    """One warning card per GPU whose credit text self-test failed, so its credits are read on the CPU until restart."""
    from markupsafe import escape

    devices = _credits_on_cpu_devices()
    if not devices:
        return []
    names = {state.key: state.name for state in get_gpu_fallback_tracker().states()}
    cards = []
    for device in devices:
        key = device["key"]
        name = names.get(key) or key
        driver_suspect = bool(device.get("driver_suspect"))
        body = (
            f"<p class='{'mb-1' if driver_suspect else 'mb-0'}'>In its accuracy check, {escape(device['reason'])}. "
            "A GPU that reads text wrong could put credits markers in the wrong place, so credits on this GPU are "
            "read on the CPU until the app restarts. That's slower, but the markers are just as accurate. "
            "Previews still use the GPU.</p>"
        )
        if driver_suspect:
            body += (
                "<p class='mb-0 small text-muted'>This usually points to a GPU driver problem. Check the host's "
                'kernel log for GPU hangs (<code>dmesg | grep -iE "i915|amdgpu|GPU HANG"</code>), then restart '
                "the app.</p>"
            )
        cards.append(
            {
                "id": _credits_on_cpu_notification_id(key),
                "severity": "warning",
                "title": f"Credits are being read on the CPU on {name}",
                "body_html": body,
                "dismissable": True,
                "permanent_dismissable": False,
                "source": "credits_text_selftest",
                "device": key,
            }
        )
    return cards


def _build_gpu_keeps_failing_notifications() -> list[dict[str, Any]]:
    """One warning card per GPU whose last ``GPU_FALLBACK_STREAK`` files all needed the CPU fallback.

    The streak lives in ``jobs.gpu_fallback`` (fed by every GPU worker, across jobs); the card goes when that GPU
    finishes a file on its own again, and its session dismissal is lifted then so a later streak shows again.
    """
    from markupsafe import escape

    cards = []
    for state in get_gpu_fallback_tracker().flagged():
        node_id = _gpu_streak_node_id(state.key)
        if node_id is None:
            todo = "Check the GPU driver, and the GPU in Settings → Processing Options → GPU device tuning."
        else:
            todo = f"Check the GPU driver on {_node_name(node_id)}, then Rescan GPUs on its Nodes page."
        body = (
            f"<p class='mb-0'>{escape(state.name)} couldn't process the last {GPU_FALLBACK_STREAK} files, so they "
            f"ran on the CPU (slower). Last reason: {escape(state.last_reason or 'GPU processing failed')}. "
            f"{escape(todo)}</p>"
        )
        card = {
            "id": notification_id(state.key),
            "severity": "warning",
            "title": "GPU keeps failing: files are running on the CPU",
            "body_html": body,
            "dismissable": True,
            "permanent_dismissable": False,
            "source": "gpu_fallback_streak",
            "device": state.key,
        }
        if node_id is not None:
            card["action"] = {"href": f"/nodes#node-{node_id}", "label": "Open node"}
        cards.append(card)
    return cards


def _gpu_streak_node_id(key: str) -> str | None:
    """The node a GPU streak key (``node:<id>:<device>``, from a node worker) belongs to; None for this app's GPU."""
    if not key.startswith("node:"):
        return None
    node_id = key.split(":", 2)[1]
    return node_id or None


def _node_name(node_id: str) -> str:
    """The node's name for a card, or "the node" once it is gone."""
    from ..nodes.registry import get_registry

    try:
        view = get_registry().view(node_id)
    except Exception as exc:
        logger.debug("node name for a GPU card unavailable: {}", exc)
        view = None
    return view.name if view is not None and view.name else "the node"


def _not_showing_by_plex_server() -> dict[str, int]:
    """Previews Plex isn't showing, per Plex server id, from the last stored check."""
    from ..library_health.models import CellState, Feature
    from ..library_health.store import default_store

    counts: dict[str, int] = {}
    for server in default_store().load():
        if server.type != "plex":
            continue
        total = sum(
            cell.not_showing
            for lib in server.libraries
            if (cell := lib.cells.get(Feature.PREVIEWS)) is not None and cell.state is CellState.COUNTED
        )
        if total > 0:
            counts[server.server_id] = total
    return counts


def _build_library_health_not_showing_notification() -> dict[str, Any] | None:
    """Previews made on disk that Plex hasn't flagged, so it doesn't show them; links to Review & fix.

    Hidden while Library health is already asking Plex to re-read them. The body names no libraries or files because
    the notifications endpoint is readable before login.
    """
    from urllib.parse import quote

    from ..library_health.runner import get_runner

    try:
        progress = get_runner().progress()
        if progress is not None and progress.kind == "reread":
            return None
        counts = _not_showing_by_plex_server()
    except Exception as exc:
        logger.debug("library-health notification skipped: {}", exc)
        return None
    if not counts:
        return None

    total = sum(counts.values())
    href = f"/library-health?fix={quote(next(iter(counts)), safe='')}" if len(counts) == 1 else "/library-health"
    body = (
        "<p class='mb-0'>The preview files are on disk, but Plex hasn't re-read those videos since, so it doesn't "
        "know about them. Review &amp; fix asks Plex to re-read them; nothing new is made.</p>"
    )
    return {
        "id": LIBRARY_HEALTH_NOT_SHOWING_ID,
        "severity": "warning",
        "title": f"{total:,} previews are made but Plex isn't showing them",
        "body_html": body,
        "action": {"label": "Review & fix", "href": href},
        "dismissable": True,
        "source": "library_health",
    }


def _build_library_health_check_failed_notification() -> dict[str, Any] | None:
    """The latest Library health check failed, for the whole run or for a server.

    Session-only dismissal: the next check that works clears it anyway. The error text stays on the Library health
    page, which needs login.
    """
    from ..library_health.runner import get_runner

    try:
        runner = get_runner()
        failed = bool(runner.last_error() or runner.failed_servers())
    except Exception as exc:
        logger.debug("library-health failure notification skipped: {}", exc)
        return None
    if not failed:
        return None
    return {
        "id": LIBRARY_HEALTH_CHECK_FAILED_ID,
        "severity": "warning",
        "title": "Library health check failed",
        "body_html": "<p class='mb-0'>The last check couldn't finish. Library health shows why.</p>",
        "action": {"label": "Open Library health", "href": "/library-health"},
        "dismissable": True,
        "permanent_dismissable": False,
        "source": "library_health",
    }


_NODE_CARD_TEXT = {
    # kind (the node's state, or its cause when that has a card of its own, ``_node_card_kind``):
    # (title, what to do when the node has no reason, action label, where on the node dialog the action lands)
    "offline": (
        "{name} is offline",
        "Check that the node is running and that this app can reach it.",
        "Open Nodes",
        "",
    ),
    # It answered: offline only until its restart is done.
    "restarting": (
        "{name} is restarting",
        "It gets no files until it is back.",
        "Open Nodes",
        "",
    ),
    # It answered too: it only holds this app back for a while (too many wrong keys from here).
    "rate_limited": (
        "{name} asks this app to wait",
        "Something else at this app's address has an old key: give it the right one, or stop it.",
        "Open Nodes",
        "",
    ),
    "update_needed": (
        "{name} needs an update",
        "Update the node so its version matches this app.",
        "Open Nodes",
        "health",
    ),
    # A worker node ahead of this app: this app is the one to update. Setup Health's version row says so too.
    "app_behind": (
        "Update this app for {name}",
        "The node runs a newer version. Update this app so its version matches the node.",
        "Open Nodes",
        "health",
    ),
    "key_refused": (
        "{name} refused the key",
        "Copy the node's key into its settings on the Nodes page.",
        "Fix key",
        "settings",
    ),
    "access_problem": (
        "{name} can't see your media",
        "Its workers are paused until it can see your media again (checked every 5 minutes). Run Test access and fix "
        "its folder mappings.",
        "Test access",
        # The Media tab, with Test access started.
        "media/test",
    ),
    "gpu_lost": (
        "{name} lost its GPU",
        "Its CPU workers keep running. Check the GPU driver on the node.",
        "Open Nodes",
        "health",
    ),
    "no_ffmpeg": (
        "{name} can't find FFmpeg",
        "Nothing is sent to it until FFmpeg is installed there.",
        "Open Nodes",
        "health",
    ),
    "duplicate": (
        "{name} is added twice",
        "Nothing is sent to it until one of the two is removed.",
        "Open Nodes",
        # Its Remove button.
        "settings",
    ),
    "temp_problem": (
        "{name} can't use its temp folder",
        "Nothing is sent to it until it can write to its temp folder again.",
        "Open Nodes",
        "health",
    ),
}


# Every ``<kind>`` a node card id can end in (``_is_known_notification_id`` accepts these).
NODE_CARD_KINDS = tuple(_NODE_CARD_TEXT)


def node_notification_id(node_id: str, kind: str) -> str:
    """The bell card id for one node in one state (``_node_card_kind``)."""
    return f"{NODE_ID_PREFIX}{node_id}:{kind}"


def _node_card_kind(state: str, cause: str) -> str:
    """Which card a node gets: its state, or its cause when that has a card of its own.

    An offline node that answered (restarting, asks this app to wait) and a worker node ahead of this app get their own
    card and id, so dismissing "is restarting" doesn't hide the "is offline" that follows when the node never comes
    back.
    """
    from ..nodes.registry import (
        OFFLINE_RATE_LIMITED,
        OFFLINE_RESTARTING,
        STATE_OFFLINE,
        STATE_UPDATE_NEEDED,
        UPDATE_APP_BEHIND,
    )

    carded = {
        OFFLINE_RESTARTING: STATE_OFFLINE,
        OFFLINE_RATE_LIMITED: STATE_OFFLINE,
        UPDATE_APP_BEHIND: STATE_UPDATE_NEEDED,
    }
    return cause if cause and carded.get(cause) == state else state


def _build_node_notifications() -> list[dict[str, Any]]:
    """One card per node in use that is offline, needs an update, refused the key, can't see media, lost its GPU,
    has no FFmpeg, is added twice or can't use its temp folder, plus one for a Plex database node whose new id waits
    for its key to be typed again.

    A node nothing uses (no switched-on worker group with a member on it, no switched-on Plex server with its Plex
    database node switched on pointing at it) raises no card: whatever its state, no work waits on it.

    The id carries the card's kind (``_node_card_kind``), so a dismissed "offline" card doesn't hide a later "refused
    the key" one; the session dismissal of a kind the node has left is lifted, so the next time it happens the card
    shows again.
    """
    from markupsafe import escape

    from ..nodes.registry import get_registry
    from ..nodes.settings import node_users
    from .settings_manager import get_settings_manager

    try:
        views = get_registry().views()
        settings = get_settings_manager()
        groups, servers = settings.get("worker_groups"), settings.get("media_servers")
    except Exception as exc:
        logger.debug("node notifications skipped: {}", exc)
        return []
    cards = []
    for view in views:
        kind = _node_card_kind(view.state, view.cause)
        for other in NODE_CARD_KINDS:
            if other != kind:
                undismiss_session(node_notification_id(view.id, other))
        users = node_users(view.id, groups, servers, switched_on_only=True)
        if not view.plex_db_unconfirmed:
            undismiss_session(node_notification_id(view.id, NODE_UNCONFIRMED))
        elif users["servers"]:
            cards.append(_unconfirmed_node_card(view.id, view.name or "A node"))
        text = _NODE_CARD_TEXT.get(kind)
        if text is None or not any(users.values()):
            continue
        title, todo, label, target = text
        name = view.name or "A node"
        # The node's reason already ends with what to do, so the card's own line is only for a node without one.
        body = view.reason or todo
        href = f"/nodes#node-{view.id}/{target}" if target else f"/nodes#node-{view.id}"
        cards.append(
            {
                "id": node_notification_id(view.id, kind),
                "severity": "warning",
                "title": title.format(name=name),
                "body_html": f"<p class='mb-0'>{escape(body)}</p>",
                "dismissable": True,
                "permanent_dismissable": False,
                "source": "nodes",
                "action": {"href": href, "label": label},
            }
        )
    return cards


def _unconfirmed_node_card(node_id: str, name: str) -> dict[str, Any]:
    """The card for a Plex database node that reports a new id (``NodeRegistry.plex_db_refusal``)."""
    return {
        "id": node_notification_id(node_id, NODE_UNCONFIRMED),
        "severity": "warning",
        "title": f"{name} reports a new id",
        "body_html": "<p class='mb-0'>This app sends it no Plex database work until you confirm it is the same Plex. "
        "Type the node's key again on its Settings tab.</p>",
        "dismissable": True,
        "permanent_dismissable": False,
        "source": "nodes",
        "action": {"href": f"/nodes#node-{node_id}/settings", "label": "Type the key again"},
    }


def _notification_sources() -> list[dict[str, Any] | None]:
    """All notification builders.  Add new sources here as they arrive."""
    return [
        _build_nvidia_driver_missing_notification(),
        _build_vulkan_software_fallback_notification(),
        _build_timezone_misconfigured_notification(),
        _build_schema_migration_notification(),
        _build_deprecated_image_notification(),
        _build_unhealthy_media_mounts_notification(),
        _build_library_health_not_showing_notification(),
        _build_library_health_check_failed_notification(),
        *_build_credits_on_cpu_notifications(),
        *_build_gpu_keeps_failing_notifications(),
        *_build_node_notifications(),
    ]


def build_active_notifications(
    dismissed_permanent: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Return the list of notifications the UI should currently display.

    Filters out any notification whose ID is in ``dismissed_permanent``
    (from ``settings.json``) or in the in-process session dismissal set.
    A session-only card (``is_session_only_dismissal``) is never hidden by ``dismissed_permanent``.
    Notifications that are not currently firing (builder returned
    ``None``) are simply not included.

    Args:
        dismissed_permanent: IDs the user has permanently dismissed.

    Returns:
        List of active notification dicts, each with at minimum
        ``id``, ``severity``, ``title``, ``body_html``, ``dismissable``.
    """
    persisted = set(dismissed_permanent or [])
    notifications: list[dict[str, Any]] = []
    for entry in _notification_sources():
        if entry is None:
            continue
        notif_id = entry.get("id")
        if not notif_id:
            continue
        if notif_id in persisted and not is_session_only_dismissal(notif_id):
            logger.debug("notifications: {} suppressed (permanently dismissed)", notif_id)
            continue
        if _session_is_dismissed(notif_id):
            logger.debug("notifications: {} suppressed (session dismissed)", notif_id)
            continue
        notifications.append(entry)
    return notifications
