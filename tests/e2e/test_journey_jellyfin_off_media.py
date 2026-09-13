"""Backend-real E2E: the Jellyfin "store trickplay off the media drive" toggle.

Drives the real edit-server modal through Playwright and asserts the full
UI wiring for off-media trickplay:

- the toggle + config-folder field render only for Jellyfin servers,
- toggling it reveals the config-folder field (the JS ``change`` handler),
- Save round-trips ``output.save_with_media`` + ``output.jellyfin_config_folder``
  through the PUT and onto disk (API + settings.json — the load-bearing
  "PUT updated memory but didn't flush" guard),
- re-opening a saved off-media server pre-populates the toggle + field.

Mirrors test_journey_edit_existing_server.py (the only other edit-modal E2E).
The adapter's off-media path math is unit-tested separately in
tests/test_output_jellyfin_trickplay.py — this file covers only the
UI -> PUT -> disk wiring.
"""

from __future__ import annotations

import json

import pytest
import requests
from playwright.sync_api import expect

_AUTH_HEADERS = {"X-Auth-Token": "e2e-test-token"}
_API_TIMEOUT = 60


def _jellyfin_server(*, off_media: bool) -> dict:
    """A Jellyfin server entry in the on-disk shape (settings.json schema)."""
    output: dict = {"adapter": "jellyfin_trickplay", "width": 320, "frame_interval": 5}
    if off_media:
        output["save_with_media"] = False
        # /tmp always exists in CI; keep it valid so no save-time path error.
        output["jellyfin_config_folder"] = "/tmp"
    return {
        "id": "jelly-edit-test",
        "type": "jellyfin",
        "name": "Edit Jellyfin",
        "enabled": True,
        "url": "http://jellyfin.invalid:8096",
        "auth": {"method": "api_key", "api_key": "x" * 20},
        "verify_ssl": True,
        "timeout": 60,
        "libraries": [
            {"id": "1", "name": "Movies", "remote_paths": [], "enabled": True},
        ],
        "path_mappings": [],
        "exclude_paths": [],
        "output": output,
    }


@pytest.mark.e2e
@pytest.mark.parametrize(
    "backend_real_app",
    [{"media_servers": [_jellyfin_server(off_media=False)]}],
    indirect=True,
)
class TestJellyfinOffMediaToggleSaves:
    def test_toggle_reveals_config_field_and_save_persists(
        self,
        backend_real_page,
        backend_real_app: tuple[str, str],
    ) -> None:
        """Media-adjacent Jellyfin -> toggle off-media -> field reveals ->
        save -> save_with_media=false + jellyfin_config_folder persist to disk."""
        app_url, config_dir = backend_real_app

        backend_real_page.goto(f"{app_url}/servers")
        backend_real_page.wait_for_load_state("domcontentloaded")

        edit_btn = backend_real_page.locator(".edit-server-btn[data-id='jelly-edit-test']")
        edit_btn.wait_for(state="visible", timeout=10000)
        edit_btn.click()
        expect(backend_real_page.locator("#editServerModal")).to_be_visible(timeout=5000)

        # The off-media controls are Jellyfin-only — the group must be shown.
        off_group = backend_real_page.locator("#editJellyfinOffMediaGroup")
        expect(off_group).to_be_visible(timeout=5000)
        toggle = backend_real_page.locator("#editJellyfinSaveOffMedia")
        expect(toggle).not_to_be_checked()

        # Config-folder field starts hidden; toggling reveals it (JS change handler).
        config_group = backend_real_page.locator("#editJellyfinConfigFolderGroup")
        expect(config_group).to_be_hidden()
        toggle.check()
        expect(config_group).to_be_visible(timeout=5000)

        backend_real_page.locator("#editJellyfinConfigFolder").fill("/tmp")
        backend_real_page.locator("#editServerSave").click()

        try:
            expect(backend_real_page.locator("#editServerModal")).to_be_hidden(timeout=10000)
        except AssertionError:
            err_text = backend_real_page.locator("#editServerResult").inner_text()
            raise AssertionError(f"Save modal did not close. Error element says: {err_text!r}.") from None

        # API round-trip.
        resp = requests.get(f"{app_url}/api/servers/jelly-edit-test", headers=_AUTH_HEADERS, timeout=_API_TIMEOUT)
        assert resp.ok, f"GET failed: {resp.status_code}"
        out = resp.json().get("output") or {}
        assert out.get("save_with_media") is False, f"save_with_media did not persist via API: output={out!r}"
        assert out.get("jellyfin_config_folder") == "/tmp", (
            f"jellyfin_config_folder did not persist via API: output={out!r}"
        )

        # On-disk settings.json — the strongest guard against "PUT didn't flush".
        with open(f"{config_dir}/settings.json") as f:
            on_disk = json.load(f)
        target = next(
            (s for s in (on_disk.get("media_servers") or []) if s.get("id") == "jelly-edit-test"),
            None,
        )
        assert target is not None, "Server vanished from settings.json after PUT"
        disk_out = target.get("output") or {}
        assert disk_out.get("save_with_media") is False, (
            f"On-disk save_with_media wrong: {disk_out!r} — PUT did not flush to disk."
        )
        assert disk_out.get("jellyfin_config_folder") == "/tmp", f"On-disk jellyfin_config_folder wrong: {disk_out!r}."


@pytest.mark.e2e
@pytest.mark.parametrize(
    "backend_real_app",
    [{"media_servers": [_jellyfin_server(off_media=True)]}],
    indirect=True,
)
class TestJellyfinOffMediaPrepopulates:
    def test_saved_off_media_server_prepopulates_modal(
        self,
        backend_real_page,
        backend_real_app: tuple[str, str],
    ) -> None:
        """A server already saved off-media re-opens with the toggle checked,
        the config-folder field revealed, and the saved path filled in."""
        app_url, _ = backend_real_app

        backend_real_page.goto(f"{app_url}/servers")
        backend_real_page.wait_for_load_state("domcontentloaded")

        edit_btn = backend_real_page.locator(".edit-server-btn[data-id='jelly-edit-test']")
        edit_btn.wait_for(state="visible", timeout=10000)
        edit_btn.click()
        expect(backend_real_page.locator("#editServerModal")).to_be_visible(timeout=5000)

        expect(backend_real_page.locator("#editJellyfinSaveOffMedia")).to_be_checked(timeout=5000)
        expect(backend_real_page.locator("#editJellyfinConfigFolderGroup")).to_be_visible()
        expect(backend_real_page.locator("#editJellyfinConfigFolder")).to_have_value("/tmp")
