"""Emby username+password → access token exchange.

Thin wrapper over :mod:`._mediabrowser_auth` (Emby and Jellyfin share
the same ``/Users/AuthenticateByName`` endpoint and ``MediaBrowser``
Authorization header). Pulled out of :mod:`emby` so the auth flow can
be exercised by the setup wizard before an :class:`EmbyServer` exists.
"""

from __future__ import annotations

from ._mediabrowser_auth import MediaBrowserAuthResult, authenticate_with_password

EmbyAuthResult = MediaBrowserAuthResult

__all__ = ["EmbyAuthResult", "authenticate_emby_with_password"]


def authenticate_emby_with_password(
    *,
    base_url: str,
    username: str,
    password: str,
    verify_ssl: bool = True,
    timeout: int = 30,
    device_id_override: str | None = None,
) -> EmbyAuthResult:
    """Exchange username+password for an Emby ``AccessToken``."""
    return authenticate_with_password(
        vendor="Emby",
        base_url=base_url,
        username=username,
        password=password,
        verify_ssl=verify_ssl,
        timeout=timeout,
        device_id_override=device_id_override,
    )
