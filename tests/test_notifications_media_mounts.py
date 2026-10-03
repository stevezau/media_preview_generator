"""Notification-center surfacing of unhealthy media mounts.

Mirrors the startup WARNING into the dashboard bell so the operator sees
"this disk looks unmounted" in the UI — not just buried in the log. Born
from job be0151d2; see project_stale_bindmount_missing_on_disk.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from media_preview_generator.web import notifications as notif_mod
from media_preview_generator.web.notifications import (
    DEPRECATED_IMAGE_ID,
    MEDIA_MOUNT_UNHEALTHY_ID,
    SCHEMA_MIGRATION_ID,
    TIMEZONE_MISCONFIGURED_ID,
    VULKAN_SOFTWARE_FALLBACK_ID,
    _build_unhealthy_media_mounts_notification,
    build_active_notifications,
    dismiss_session,
    reset_session,
)


def _settings_with_servers(servers):
    return SimpleNamespace(get=lambda key, default=None: servers if key == "media_servers" else default)


def _mapping(local):
    return {"plex_prefix": local, "local_prefix": local, "webhook_prefixes": ["/data"]}


class TestUnhealthyMediaMountsNotification:
    def test_fires_with_warning_when_a_mount_is_empty(self, tmp_path):
        empty = tmp_path / "data_16tb3"
        empty.mkdir()
        servers = [{"name": "Plex", "path_mappings": [_mapping(str(empty))]}]

        with patch(
            "media_preview_generator.web.settings_manager.get_settings_manager",
            return_value=_settings_with_servers(servers),
        ):
            notif = _build_unhealthy_media_mounts_notification()

        assert notif is not None
        assert notif["id"] == MEDIA_MOUNT_UNHEALTHY_ID
        assert notif["severity"] == "warning"
        assert str(empty) in notif["body_html"]

    def test_returns_none_when_all_mounts_healthy(self, tmp_path):
        good = tmp_path / "data_16tb"
        good.mkdir()
        (good / "TV Shows").mkdir()
        servers = [{"name": "Plex", "path_mappings": [_mapping(str(good))]}]

        with patch(
            "media_preview_generator.web.settings_manager.get_settings_manager",
            return_value=_settings_with_servers(servers),
        ):
            assert _build_unhealthy_media_mounts_notification() is None

    def test_returns_none_when_no_servers_configured(self):
        with patch(
            "media_preview_generator.web.settings_manager.get_settings_manager",
            return_value=_settings_with_servers([]),
        ):
            assert _build_unhealthy_media_mounts_notification() is None


def _mount(tmp_path, state):
    """A media folder in one of the three states the health probe tells apart."""
    path = tmp_path / f"media_{state}"
    if state != "missing":
        path.mkdir()
    if state == "healthy":
        (path / "TV Shows").mkdir()
    return str(path)


class TestMediaMountCardIsNeverDismissedForGood:
    """The mount comes and goes; a card hidden for good would hide the next outage too."""

    @pytest.fixture(autouse=True)
    def _only_the_mount_source(self):
        reset_session()
        with (
            patch.object(notif_mod, "_build_vulkan_software_fallback_notification", return_value=None),
            patch.object(notif_mod, "_build_timezone_misconfigured_notification", return_value=None),
            patch.object(notif_mod, "_build_deprecated_image_notification", return_value=None),
        ):
            yield
        reset_session()

    def _active_ids(self, tmp_path, state, stored):
        servers = [{"name": "Plex", "path_mappings": [_mapping(_mount(tmp_path, state))]}]
        with patch(
            "media_preview_generator.web.settings_manager.get_settings_manager",
            return_value=_settings_with_servers(servers),
        ):
            return [n["id"] for n in build_active_notifications(dismissed_permanent=stored)]

    @pytest.mark.parametrize(
        ("state", "stored", "expected"),
        [
            ("empty", [], [MEDIA_MOUNT_UNHEALTHY_ID]),
            ("empty", [MEDIA_MOUNT_UNHEALTHY_ID], [MEDIA_MOUNT_UNHEALTHY_ID]),
            ("missing", [], [MEDIA_MOUNT_UNHEALTHY_ID]),
            ("missing", [MEDIA_MOUNT_UNHEALTHY_ID], [MEDIA_MOUNT_UNHEALTHY_ID]),
            ("healthy", [], []),
            ("healthy", [MEDIA_MOUNT_UNHEALTHY_ID], []),
        ],
    )
    def test_a_stored_permanent_dismissal_never_hides_the_card(self, tmp_path, state, stored, expected):
        assert self._active_ids(tmp_path, state, stored) == expected

    def test_the_card_can_still_be_dismissed_until_the_next_restart(self, tmp_path):
        dismiss_session(MEDIA_MOUNT_UNHEALTHY_ID)

        assert self._active_ids(tmp_path, "empty", []) == []

    def test_the_card_tells_the_ui_not_to_offer_dismiss_permanently(self, tmp_path):
        servers = [{"name": "Plex", "path_mappings": [_mapping(_mount(tmp_path, "empty"))]}]
        with patch(
            "media_preview_generator.web.settings_manager.get_settings_manager",
            return_value=_settings_with_servers(servers),
        ):
            card = _build_unhealthy_media_mounts_notification()

        assert card["dismissable"] is True
        assert card["permanent_dismissable"] is False

    @pytest.mark.parametrize(
        ("notification_id", "hidden_by_stored_id"),
        [
            (VULKAN_SOFTWARE_FALLBACK_ID, True),
            (TIMEZONE_MISCONFIGURED_ID, True),
            (DEPRECATED_IMAGE_ID, True),
            (SCHEMA_MIGRATION_ID, True),
            (MEDIA_MOUNT_UNHEALTHY_ID, False),
        ],
    )
    def test_only_the_mount_card_ignores_a_stored_permanent_dismissal(self, notification_id, hidden_by_stored_id):
        card = {"id": notification_id, "severity": "warning", "title": "t", "body_html": "b", "dismissable": True}
        with patch.object(notif_mod, "_notification_sources", return_value=[card]):
            active = build_active_notifications(dismissed_permanent=[notification_id])

        assert active == ([] if hidden_by_stored_id else [card])

    def test_the_bell_only_draws_dismiss_permanently_for_cards_that_allow_it(self):
        from pathlib import Path

        js = (Path(notif_mod.__file__).parent / "static" / "js" / "notifications.js").read_text()
        session_button = js.index("dismiss.textContent = 'Dismiss';")
        guard = js.index("if (notif.permanent_dismissable !== false) {")
        permanent_button = js.index("var dismissPerm = document.createElement('button');")

        # The session button is built before the guard opens (always offered); the permanent one right after it.
        assert session_button < guard < permanent_button
        assert js[guard:permanent_button].count("\n") == 1, "the permanent button must be the guard's first statement"
