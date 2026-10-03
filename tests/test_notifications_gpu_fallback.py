"""Bell card for a GPU whose last files all ran on the CPU (jobs.gpu_fallback)."""

from __future__ import annotations

import pytest

from media_preview_generator.jobs.gpu_fallback import get_gpu_fallback_tracker, notification_id
from media_preview_generator.web import notifications as notif_mod
from media_preview_generator.web.notifications import (
    _build_gpu_keeps_failing_notifications,
    build_active_notifications,
    dismiss_session,
    is_session_only_dismissal,
    reset_session,
)

KEY = "/dev/dri/renderD128"


@pytest.fixture(autouse=True)
def _fresh():
    get_gpu_fallback_tracker().reset()
    reset_session()
    yield
    get_gpu_fallback_tracker().reset()
    reset_session()


def _flag(key: str = KEY, name: str = "Intel UHD Graphics 770", reason: str = "hevc not supported", n: int = 5):
    for _ in range(n):
        get_gpu_fallback_tracker().record(key, name, fell_back=True, reason=reason)


class TestGpuKeepsFailingNotification:
    def test_absent_while_no_gpu_is_flagged(self):
        _flag(n=4)
        assert _build_gpu_keeps_failing_notifications() == []

    def test_one_card_per_flagged_gpu_with_the_approved_wording(self):
        _flag()
        (card,) = _build_gpu_keeps_failing_notifications()
        assert card == {
            "id": "gpu_keeps_failing__dev_dri_renderD128",
            "severity": "warning",
            "title": "GPU keeps failing: files are running on the CPU",
            "body_html": (
                "<p class='mb-0'>Intel UHD Graphics 770 couldn't process the last 5 files, so they ran on the CPU "
                "(slower). Last reason: hevc not supported. Check the GPU driver in Settings → GPU.</p>"
            ),
            "dismissable": True,
            "permanent_dismissable": False,
            "source": "gpu_fallback_streak",
            "device": KEY,
        }

    def test_two_flagged_gpus_give_two_cards_in_key_order(self):
        _flag("/dev/dri/renderD129", "GPU B")
        _flag("/dev/dri/renderD128", "GPU A")
        cards = _build_gpu_keeps_failing_notifications()
        assert [c["id"] for c in cards] == [
            notification_id("/dev/dri/renderD128"),
            notification_id("/dev/dri/renderD129"),
        ]
        assert cards[0]["body_html"].startswith("<p class='mb-0'>GPU A couldn't")

    def test_the_reason_and_name_are_escaped_and_secrets_masked(self):
        _flag(name="GPU <b>A</b>", reason="GET http://plex:32400/?X-Plex-Token=secret123 <failed>")
        (card,) = _build_gpu_keeps_failing_notifications()
        assert "secret123" not in card["body_html"]
        assert "<b>" not in card["body_html"]
        assert "&lt;failed&gt;" in card["body_html"]

    def test_clears_once_the_gpu_finishes_a_file_without_falling_back(self):
        _flag()
        get_gpu_fallback_tracker().record(KEY, "GPU A", fell_back=False, reason=None)
        assert _build_gpu_keeps_failing_notifications() == []

    def test_it_is_in_the_active_list_and_hidden_only_for_the_session(self, monkeypatch):
        for name in (
            "_build_vulkan_software_fallback_notification",
            "_build_timezone_misconfigured_notification",
            "_build_schema_migration_notification",
            "_build_deprecated_image_notification",
            "_build_unhealthy_media_mounts_notification",
        ):
            monkeypatch.setattr(notif_mod, name, lambda: None)
        _flag()
        card_id = notification_id(KEY)
        assert [n["id"] for n in build_active_notifications()] == [card_id]
        # A stored permanent dismissal is ignored: the problem comes and goes, like an unmounted media path.
        assert [n["id"] for n in build_active_notifications(dismissed_permanent=[card_id])] == [card_id]
        dismiss_session(card_id)
        assert build_active_notifications() == []

    def test_a_recovered_gpu_that_fails_again_shows_its_card_again(self, monkeypatch):
        for name in (
            "_build_vulkan_software_fallback_notification",
            "_build_timezone_misconfigured_notification",
            "_build_schema_migration_notification",
            "_build_deprecated_image_notification",
            "_build_unhealthy_media_mounts_notification",
        ):
            monkeypatch.setattr(notif_mod, name, lambda: None)
        _flag()
        dismiss_session(notification_id(KEY))
        get_gpu_fallback_tracker().record(KEY, "GPU A", fell_back=False, reason=None)
        _flag()
        assert [n["id"] for n in build_active_notifications()] == [notification_id(KEY)]

    def test_session_only_dismissal_covers_gpu_cards_and_the_media_mount_card(self):
        assert is_session_only_dismissal(notification_id(KEY)) is True
        assert is_session_only_dismissal(notif_mod.MEDIA_MOUNT_UNHEALTHY_ID) is True
        assert is_session_only_dismissal(notif_mod.VULKAN_SOFTWARE_FALLBACK_ID) is False
