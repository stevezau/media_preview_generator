"""Webhook info endpoints for Emby and Jellyfin servers.

Pre-fix the per-server Edit modal had a "Webhook & Scanner" tab for
Plex only — Emby and Jellyfin users had no place to find the webhook
URL their plugin should POST to. The router itself accepts payloads
from all three vendors at the universal /incoming endpoint and a
per-server /server/<id> route; the asymmetry was UI-only.

This module exposes:

* ``GET /api/settings/{emby,jellyfin}_webhook/info?server_id=<id>`` —
  returns the per-server webhook URL plus plugin-specific install
  instructions. The secret is never returned; the UI renders a
  ``[YOUR WEBHOOK SECRET]`` placeholder and points the user at
  Automation → Triggers → Settings, where the secret is set.

A previous ``/test`` endpoint that loopback-POSTed at our own
``/incoming`` route was removed: the only thing it verified was that
the route + auth token worked locally, which is trivially true when
the user is already authenticated to the UI. It did NOT prove the
external media server could reach this app, which was the only
question worth answering — leaving the button live was misleading.
"""

from __future__ import annotations

from typing import cast

from flask import jsonify, request

from ..auth import setup_or_auth_required
from . import api

_PLUGIN_INSTRUCTIONS = {
    "emby": {
        "plugin_name": "Emby Webhooks (built-in, Emby 4.6+)",
        "install_url": "https://emby.media/support/articles/Webhooks.html",
        # Emby's built-in Webhooks support custom HTTP headers via the
        # 'Request Headers' field, so the token never has to ride in the
        # URL. Steps reference [YOUR WEBHOOK SECRET] as a placeholder so
        # the live secret never appears in the surfaced instructions.
        "config_steps": [
            "In Emby: Settings → Notifications → Add Webhook (Emby 4.6+ ships this built-in; no plugin install required).",
            "Set 'Url' to the Webhook URL shown above.",
            "Expand 'Request Headers' and add a header — Name: 'X-Auth-Token', Value: [YOUR WEBHOOK SECRET] (set or generate it under Automation → Triggers → Settings).",
            "Tick 'New Media Added' under Events. Add any other events you want to forward.",
            "Save.",
        ],
        "supports_custom_headers": True,
    },
    "jellyfin": {
        "plugin_name": "Jellyfin Webhook plugin (official)",
        "install_url": "https://github.com/jellyfin/jellyfin-plugin-webhook",
        # Jellyfin's official Webhook plugin's Generic Destination
        # exposes a 'Headers' section — confirmed in the plugin's
        # configuration page since v15.x. Token goes there, not in URL.
        "config_steps": [
            "In Jellyfin: Dashboard → Plugins → Catalog → install 'Webhook' (restart Jellyfin if prompted).",
            "Dashboard → Plugins → Webhook → 'Add Generic Destination'.",
            "Set 'Webhook Url' to the address shown above.",
            "Expand 'Headers', click 'Add Header', set Key: 'X-Auth-Token', Value: [YOUR WEBHOOK SECRET] (set or generate it under Automation → Triggers → Settings).",
            "Under 'Notification Type', tick 'Item Added'.",
            "Set 'Item Type' to Movie + Episode at minimum.",
            "Save.",
        ],
        "supports_custom_headers": True,
    },
}


def webhook_url(server_id: str | None = None) -> str:
    """Webhook URL a media server should POST to, on the host the browser is using.

    ``request.host_url`` makes a session connected via http://10.0.0.5:8080 get a URL the
    media server can reach from its own network. With ``server_id`` the per-server route
    (:func:`webhook_router.webhook_per_server`) is returned, which routes unambiguously
    between servers of the same type; without it, the universal ``/incoming`` route.
    """
    base = request.host_url.rstrip("/")
    if server_id:
        return f"{base}/api/webhooks/server/{server_id}"
    return f"{base}/api/webhooks/incoming"


def find_server_entry(server_id: str, expected_type: str) -> tuple[dict | None, str | None, int | None]:
    """Look up a configured server by id, requiring it to be of ``expected_type``.

    Returns:
        ``(entry, None, None)`` on success, else ``(None, error_message, http_status)``.
    """
    from ..settings_manager import get_settings_manager

    media_servers = get_settings_manager().get("media_servers") or []
    match = next((s for s in media_servers if isinstance(s, dict) and s.get("id") == server_id), None)
    if not match:
        return None, f"Server {server_id!r} not configured", 404
    actual_type = (match.get("type") or "").lower()
    if actual_type != expected_type:
        return (
            None,
            f"Server {server_id!r} is type {actual_type!r}, expected {expected_type!r} (this endpoint is {expected_type.title()}-only)",
            400,
        )
    return match, None, None


def _resolve_vendor_server(server_id: str | None, expected_type: str) -> tuple[dict | None, str | None, int | None]:
    """Resolve a configured Emby / Jellyfin server entry by id."""
    if not server_id:
        return None, "server_id query parameter is required", 400
    return find_server_entry(server_id, expected_type)


def _info_handler(vendor: str):
    server_id = request.args.get("server_id")
    server_entry, err, status = _resolve_vendor_server(server_id, vendor)
    if err:
        return jsonify({"error": err}), status
    instructions = _PLUGIN_INSTRUCTIONS.get(vendor, {})
    webhook_url_per_server = webhook_url(server_id)

    # The plugin authenticates via the X-Auth-Token header. The value
    # (the webhook secret) is never returned by this endpoint — surfacing
    # it would put it in JS state, HTML inspector output, and any
    # screen-share of the Edit modal. The user sets or generates it under
    # Automation → Triggers → Settings. Not the sign-in token: although
    # webhooks accept it, Settings → Authentication never displays it (#432).
    from ..settings_manager import get_settings_manager

    secret_set = bool((get_settings_manager().get("webhook_secret") or "").strip())

    return jsonify(
        {
            "vendor": vendor,
            "server_id": server_id,
            "server_name": cast(dict, server_entry).get("name") or server_id,
            "webhook_url": webhook_url_per_server,
            "webhook_url_per_server": webhook_url_per_server,
            "auth_header_name": "X-Auth-Token",
            "auth_token_placeholder": "[YOUR WEBHOOK SECRET]",
            "auth_token_required": True,
            "webhook_secret_set": secret_set,
            "auth_token_source": "Automation → Triggers → Settings → Webhook Secret",
            "plugin": instructions,
            # Surfaces the auto-register deferral so the UI can render a
            # "Coming soon" hint instead of pretending the button does
            # something it doesn't.
            "auto_register_supported": False,
        }
    )


# ---------------------------------------------------------------------------
# Emby
# ---------------------------------------------------------------------------


@api.route("/settings/emby_webhook/info", methods=["GET"])
@setup_or_auth_required
def emby_webhook_info():
    return _info_handler("emby")


# ---------------------------------------------------------------------------
# Jellyfin
# ---------------------------------------------------------------------------


@api.route("/settings/jellyfin_webhook/info", methods=["GET"])
@setup_or_auth_required
def jellyfin_webhook_info():
    return _info_handler("jellyfin")
