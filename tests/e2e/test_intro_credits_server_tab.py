"""E2E: Servers → Edit → "Intro & Credits" tab and the Plex database-write confirmation.

Every API the tab touches is mocked with ``page.route``: the server list and the single-server GET, the PUT the
Save button sends (captured), ``GET /api/markers/servers/<id>/status`` (the capability matrix) and the shared
Jellyfin/Emby ``POST /api/servers/<id>/install-plugin``. Save tests assert the ``markers`` block the page sends, not
just that a PUT happened.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime

import pytest
from playwright.sync_api import Page, Route, expect

from ._mocks import mock_server_connection_probe, mock_server_previews_readiness

PLEX_DB = "/plex/Library/Application Support/Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db"
PLEX_DB_DIR = "/plex/Library/Application Support/Plex Media Server/Plug-in Support/Databases"
SAME_HOST_HINT = (
    "Map Plex's config folder into both containers from the identical host path "
    "(on unRAID, don't mix /mnt/user and /mnt/cache)."
)


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


def _libraries() -> list[dict]:
    return [
        {"id": "1", "name": "Movies", "kind": "movie", "enabled": True, "remote_paths": []},
        {"id": "2", "name": "TV Shows", "kind": "show", "enabled": True, "remote_paths": []},
        {"id": "3", "name": "Sports", "kind": "show", "enabled": True, "remote_paths": []},
    ]


def _plex_server(markers: dict | None = None) -> dict:
    return {
        "id": "plex-1",
        "type": "plex",
        "name": "Plex Lab",
        "enabled": True,
        "url": "http://plex.invalid:32400",
        "auth": {"method": "token"},
        "verify_ssl": True,
        "timeout": 30,
        "libraries": _libraries(),
        "path_mappings": [],
        "exclude_paths": [],
        "output": {"adapter": "plex_bundle", "plex_config_folder": "/tmp"},
        "markers": markers
        or {
            "enabled": False,
            "library_ids": None,
            "plex": {"db_write_confirmed_at": None, "on_plex_redetect": "restore"},
        },
    }


def _vendor_server(vendor: str, server_id: str, markers: dict | None = None) -> dict:
    return {
        "id": server_id,
        "type": vendor,
        "name": f"{vendor.title()} Lab",
        "enabled": True,
        "url": f"http://{vendor}.invalid:8096",
        "auth": {"method": "api_key"},
        "verify_ssl": True,
        "timeout": 30,
        "libraries": _libraries(),
        "path_mappings": [],
        "exclude_paths": [],
        "output": {},
        "markers": markers or {"enabled": False, "library_ids": None},
    }


def _status(server: dict, state: str, message: str = "", details: dict | None = None) -> dict:
    can_show = {
        "plex": ["intro", "credits"],
        "jellyfin": ["intro", "credits", "recap", "preview"],
        "emby": ["intro", "credits"],
    }[server["type"]]
    return {
        "server_id": server["id"],
        "server_type": server["type"],
        "enabled": server["markers"]["enabled"],
        "settings": server["markers"],
        "capability": {"state": state, "message": message, "details": details or {}, "warning": ""},
        "can_show": can_show,
        "libraries": [
            {"id": lib["id"], "name": lib["name"], "kind": lib["kind"], "default_selected": lib["name"] != "Sports"}
            for lib in server["libraries"]
        ],
    }


def _plex_ready_details(**overrides) -> dict:
    details = {
        "db_path": PLEX_DB,
        "fs_type": "ext4",
        "lock_holder": True,
        "plex_pass": True,
        "plex_version": "1.43.0.10162",
        "detection": {"intro": "scheduled", "credits": "scheduled"},
    }
    details.update(overrides)
    return details


def _fulfill(route: Route, body: object, status: int = 200) -> None:
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


def _mock_server_page(page: Page, server: dict, status: dict | None, *, status_code: int = 200) -> dict:
    """Route the Servers page + Edit dialog for one server; returns the captured requests."""
    captured: dict = {"puts": [], "installs": [], "status_calls": 0}
    state = {"status": status, "status_code": status_code}

    def list_handler(route: Route) -> None:
        if route.request.method == "GET":
            _fulfill(route, {"servers": [server]})
        else:
            route.continue_()

    def single_handler(route: Route) -> None:
        if route.request.method == "GET":
            _fulfill(route, server)
        elif route.request.method == "PUT":
            captured["puts"].append(route.request.post_data_json)
            _fulfill(route, server)
        else:
            route.continue_()

    def status_handler(route: Route) -> None:
        captured["status_calls"] += 1
        if state["status_code"] != 200:
            _fulfill(route, {"error": "Couldn't check this server's Intro & Credits status"}, state["status_code"])
        else:
            _fulfill(route, state["status"])

    captured["install_answer"] = {"ok": True, "steps": []}

    def install_handler(route: Route) -> None:
        captured["installs"].append({"method": route.request.method, "url": route.request.url})
        _fulfill(route, captured["install_answer"])

    mock_server_connection_probe(page, ok=True, server_name=server["name"])
    mock_server_previews_readiness(page, vendor=server["type"], critical_count=0)
    page.route("**/api/servers", list_handler)
    page.route(f"**/api/servers/{server['id']}", single_handler)
    page.route(f"**/api/markers/servers/{server['id']}/status", status_handler)
    page.route(f"**/api/servers/{server['id']}/install-plugin", install_handler)
    captured["state"] = state
    return captured


def _open_tab(page: Page, app_url: str, server: dict) -> None:
    page.goto(f"{app_url}/servers")
    page.wait_for_load_state("domcontentloaded")
    edit_btn = page.locator(f".edit-server-btn[data-id='{server['id']}']")
    edit_btn.wait_for(state="visible", timeout=10000)
    edit_btn.click()
    expect(page.locator("#editServerModal")).to_be_visible(timeout=5000)
    page.locator('#editServerModal [data-bs-target="#edit-tab-markers"]').click()
    expect(page.locator("#edit-tab-markers")).to_be_visible(timeout=5000)


def _lib_toggle(page: Page, lib_id: str):
    return page.locator(f"#markersLibraryList .markers-lib-toggle[data-id='{lib_id}']")


def _flip_switch_on(page: Page) -> None:
    # The switch is a Bootstrap form-switch; click its label so the change event fires like a user's click.
    page.locator("label[for='markersEnabled']").click()


def _save_and_get_put(page: Page, captured: dict) -> dict:
    page.locator("#editServerSave").click()
    expect(page.locator("#editServerModal")).to_be_hidden(timeout=10000)
    assert len(captured["puts"]) == 1, f"expected exactly one PUT, got {len(captured['puts'])}"
    return captured["puts"][0]


@pytest.mark.e2e
class TestPlexTab:
    def test_ready_status_and_default_libraries_render(self, authed_page: Page, app_url: str) -> None:
        server = _plex_server()
        status = _status(server, "ready", "Written into this Plex server's database", _plex_ready_details())
        _mock_server_page(authed_page, server, status)
        _open_tab(authed_page, app_url, server)

        block = authed_page.locator("#markersStatusBlock")
        expect(block).to_contain_text("How markers get here", timeout=5000)
        expect(block).to_contain_text("Written into this Plex server's database")
        expect(block).to_contain_text("✓ Active")
        expect(block).to_contain_text(PLEX_DB_DIR)
        expect(block).to_contain_text("local disk")
        expect(block.locator(".markers-detection")).to_have_text("On")
        expect(block).to_contain_text("it can replace ours; we put them back")
        expect(block.locator(".alert-warning")).to_have_count(0)

        expect(_lib_toggle(authed_page, "1")).to_be_checked()
        expect(_lib_toggle(authed_page, "2")).to_be_checked()
        expect(_lib_toggle(authed_page, "3")).not_to_be_checked()
        # The pills are one named group for screen readers ("Libraries" above them is plain text).
        expect(authed_page.get_by_role("group", name="Libraries that get markers on this server")).to_have_count(1)
        expect(authed_page.locator("#markersPlexRedetectGroup")).to_be_visible()
        expect(authed_page.locator("#markersRedetectRestore")).to_be_checked()
        expect(authed_page.locator("#markersEmbyRedetectGroup")).to_be_hidden()
        expect(authed_page.locator("#markersEnabled")).not_to_be_checked()

    def test_detection_on_with_keep_plex_says_plexs_markers_are_kept(self, authed_page: Page, app_url: str) -> None:
        server = _plex_server(
            {
                "enabled": True,
                "library_ids": None,
                "plex": {"db_write_confirmed_at": "2026-09-01T10:00:00+00:00", "on_plex_redetect": "keep_plex"},
            }
        )
        _mock_server_page(authed_page, server, _status(server, "ready", "", _plex_ready_details()))
        _open_tab(authed_page, app_url, server)
        block = authed_page.locator("#markersStatusBlock")
        expect(block).to_contain_text("it can replace ours; Plex's are kept", timeout=5000)
        expect(block).not_to_contain_text("we put them back")

    def test_detection_never_renders_off(self, authed_page: Page, app_url: str) -> None:
        server = _plex_server()
        details = _plex_ready_details(detection={"intro": "never", "credits": "never"})
        _mock_server_page(
            authed_page, server, _status(server, "ready", "Written into this Plex server's database", details)
        )
        _open_tab(authed_page, app_url, server)
        expect(authed_page.locator("#markersStatusBlock .markers-detection")).to_have_text("Off", timeout=5000)

    def test_ready_plex_whose_plex_pass_couldnt_be_checked_shows_the_warning(
        self, authed_page: Page, app_url: str
    ) -> None:
        server = _plex_server()
        details = _plex_ready_details(plex_pass=None, plex_version=None, detection={"intro": None, "credits": None})
        status = _status(server, "ready", "", details)
        warning = "Can't reach Plex to confirm Plex Pass, so markers wait until Plex answers"
        status["capability"]["warning"] = warning
        _mock_server_page(authed_page, server, status)
        _open_tab(authed_page, app_url, server)

        block = authed_page.locator("#markersStatusBlock")
        expect(block.locator(".alert-warning")).to_have_count(1, timeout=5000)
        expect(block.locator(".alert-warning")).to_contain_text(warning)
        expect(block).not_to_contain_text("✓ Active")
        expect(block).to_contain_text("✓ local disk")

    def test_plex_markers_setting_is_named_and_explained_before_and_after_we_publish(
        self, authed_page: Page, app_url: str
    ) -> None:
        server = _plex_server()
        _mock_server_page(authed_page, server, _status(server, "ready", "", _plex_ready_details()))
        _open_tab(authed_page, app_url, server)
        group = authed_page.locator("#markersPlexRedetectGroup")
        expect(group.locator(".fw-semibold")).to_have_text("When Plex has its own markers")
        expect(group.locator("[role='group']")).to_have_attribute("aria-label", "When Plex has its own markers")
        expect(group.locator("label[for='markersRedetectRestore']")).to_have_text("Use ours")
        expect(group.locator("label[for='markersRedetectKeep']")).to_have_text("Keep Plex's")
        expect(group.locator("#markersRedetectRestore")).to_have_attribute("value", "restore")
        expect(group.locator("#markersRedetectKeep")).to_have_attribute("value", "keep_plex")
        icon = group.locator(".info-icon")
        tooltip = icon.evaluate("el => el.getAttribute('data-bs-original-title') || el.getAttribute('title')")
        assert tooltip == (
            "Plex can show intro and credits markers of its own, from its detection (for example Analyze on an item). "
            "Before we publish: 'Use ours' writes ours over them; 'Keep Plex's' leaves them and writes ours only for "
            "the types Plex has none of. After we publish, Plex's detection can replace ours: 'Use ours' writes ours "
            "again on the next Intro & Credits job that checks the file; 'Keep Plex's' keeps Plex's until you switch "
            "to 'Use ours' or Plex removes them."
        )

    def test_flip_then_cancel_unticks_switch_and_sends_nothing(self, authed_page: Page, app_url: str) -> None:
        server = _plex_server()
        captured = _mock_server_page(
            authed_page,
            server,
            _status(server, "ready", "Written into this Plex server's database", _plex_ready_details()),
        )
        _open_tab(authed_page, app_url, server)
        expect(authed_page.locator("#markersStatusBlock")).to_contain_text("✓ Active", timeout=5000)

        _flip_switch_on(authed_page)
        modal = authed_page.locator("#markersPlexConfirmModal")
        expect(modal).to_be_visible(timeout=5000)
        expect(modal).to_contain_text(
            "Plex has no way for apps to add intro or credits markers, so they are written straight into Plex's "
            "database — the same place Plex stores its own."
        )
        expect(modal).to_contain_text("Tested on Plex 1.43.")
        expect(modal).to_contain_text("Checked: local disk ✓")
        expect(modal).to_contain_text(SAME_HOST_HINT)
        authed_page.locator("#markersPlexConfirmCancel").click()
        expect(modal).to_be_hidden(timeout=5000)
        expect(authed_page.locator("#markersEnabled")).not_to_be_checked()
        authed_page.wait_for_timeout(300)
        assert captured["puts"] == []

    def test_escape_closes_only_the_confirmation(self, authed_page: Page, app_url: str) -> None:
        server = _plex_server()
        captured = _mock_server_page(
            authed_page,
            server,
            _status(server, "ready", "Written into this Plex server's database", _plex_ready_details()),
        )
        _open_tab(authed_page, app_url, server)
        _flip_switch_on(authed_page)
        modal = authed_page.locator("#markersPlexConfirmModal")
        expect(modal).to_be_visible(timeout=5000)
        # Keyboard focus lands in the confirmation, not in the Edit dialog behind it.
        expect(modal).to_contain_text("Enable for Plex")
        authed_page.wait_for_function(
            "document.activeElement && document.activeElement.closest('#markersPlexConfirmModal')"
        )

        authed_page.keyboard.press("Escape")
        expect(modal).to_be_hidden(timeout=5000)
        expect(authed_page.locator("#editServerModal")).to_be_visible()
        expect(authed_page.locator("#markersEnabled")).not_to_be_checked()
        assert captured["puts"] == []

    def test_flip_enable_save_sends_confirmation(self, authed_page: Page, app_url: str) -> None:
        server = _plex_server()
        captured = _mock_server_page(
            authed_page,
            server,
            _status(server, "ready", "Written into this Plex server's database", _plex_ready_details()),
        )
        _open_tab(authed_page, app_url, server)
        expect(authed_page.locator("#markersStatusBlock")).to_contain_text("✓ Active", timeout=5000)

        _flip_switch_on(authed_page)
        expect(authed_page.locator("#markersPlexConfirmModal")).to_be_visible(timeout=5000)
        authed_page.locator("#markersPlexConfirmOk").click()
        expect(authed_page.locator("#markersPlexConfirmModal")).to_be_hidden(timeout=5000)
        expect(authed_page.locator("#markersEnabled")).to_be_checked()

        body = _save_and_get_put(authed_page, captured)
        markers = body["markers"]
        confirmed = markers["plex"]["db_write_confirmed_at"]
        assert isinstance(confirmed, str) and datetime.fromisoformat(confirmed)
        assert markers == {
            "enabled": True,
            "library_ids": None,
            "plex": {"db_write_confirmed_at": confirmed, "on_plex_redetect": "restore"},
        }

    def test_save_with_switch_on_and_no_confirmation_asks_and_cancel_blocks_put(
        self, authed_page: Page, app_url: str
    ) -> None:
        server = _plex_server()
        captured = _mock_server_page(
            authed_page,
            server,
            _status(server, "ready", "Written into this Plex server's database", _plex_ready_details()),
        )
        _open_tab(authed_page, app_url, server)
        # Ticked without a change event (e.g. the modal was dismissed some other way): Save must still ask.
        authed_page.evaluate("document.getElementById('markersEnabled').checked = true")
        authed_page.locator("#editServerSave").click()
        expect(authed_page.locator("#markersPlexConfirmModal")).to_be_visible(timeout=5000)
        authed_page.locator("#markersPlexConfirmCancel").click()
        expect(authed_page.locator("#markersPlexConfirmModal")).to_be_hidden(timeout=5000)
        authed_page.wait_for_timeout(300)
        assert captured["puts"] == []
        expect(authed_page.locator("#editServerModal")).to_be_visible()
        expect(authed_page.locator("#editServerSave")).to_be_enabled()

    def test_untick_library_and_keep_plex_are_sent(self, authed_page: Page, app_url: str) -> None:
        server = _plex_server()
        captured = _mock_server_page(
            authed_page,
            server,
            _status(server, "ready", "Written into this Plex server's database", _plex_ready_details()),
        )
        _open_tab(authed_page, app_url, server)
        expect(_lib_toggle(authed_page, "2")).to_be_checked(timeout=5000)

        authed_page.locator("#markersLibraryList label[for='markersLib-2']").click()
        expect(_lib_toggle(authed_page, "2")).not_to_be_checked()
        authed_page.locator("label[for='markersRedetectKeep']").click()

        body = _save_and_get_put(authed_page, captured)
        assert body["markers"] == {
            "enabled": False,
            "library_ids": ["1"],
            "plex": {"db_write_confirmed_at": None, "on_plex_redetect": "keep_plex"},
        }

    def test_already_confirmed_server_skips_modal(self, authed_page: Page, app_url: str) -> None:
        stored_at = "2026-09-01T10:00:00+00:00"
        server = _plex_server(
            {
                "enabled": False,
                "library_ids": None,
                "plex": {"db_write_confirmed_at": stored_at, "on_plex_redetect": "restore"},
            }
        )
        captured = _mock_server_page(
            authed_page,
            server,
            _status(server, "ready", "Written into this Plex server's database", _plex_ready_details()),
        )
        _open_tab(authed_page, app_url, server)
        expect(authed_page.locator("#markersStatusBlock")).to_contain_text("✓ Active", timeout=5000)

        _flip_switch_on(authed_page)
        authed_page.wait_for_timeout(500)
        expect(authed_page.locator("#markersPlexConfirmModal")).to_be_hidden()
        expect(authed_page.locator("#markersEnabled")).to_be_checked()

        body = _save_and_get_put(authed_page, captured)
        assert body["markers"] == {
            "enabled": True,
            "library_ids": None,
            "plex": {"db_write_confirmed_at": stored_at, "on_plex_redetect": "restore"},
        }

    def test_network_share_warns_and_modal_says_network_share(self, authed_page: Page, app_url: str) -> None:
        server = _plex_server()
        message = (
            "Plex's database is on a network share (nfs4). The app must run on the same machine as Plex to write "
            "markers; Plex stays read-only."
        )
        details = {"db_path": PLEX_DB, "fs_type": "nfs4", "plex_pass": True, "plex_version": "1.43.0.10162"}
        _mock_server_page(authed_page, server, _status(server, "needs_local_db", message, details))
        _open_tab(authed_page, app_url, server)

        block = authed_page.locator("#markersStatusBlock")
        expect(block.locator(".alert-warning")).to_contain_text(message, timeout=5000)
        expect(block).to_contain_text("✕ network share (nfs4)")

        _flip_switch_on(authed_page)
        modal = authed_page.locator("#markersPlexConfirmModal")
        expect(modal).to_be_visible(timeout=5000)
        expect(modal).to_contain_text("Checked: network share ✕ — writes will stay off")
        expect(modal).to_contain_text(SAME_HOST_HINT)

    @pytest.mark.parametrize(
        ("message", "hint_shown"),
        [
            # The backend message already gives the advice: the hint would repeat it.
            (
                f"Plex is running, but not with the database file this app sees at {PLEX_DB}. Mount the exact folder "
                "Plex uses, on the same machine (on unRAID, the same /mnt/cache or /mnt/user path Plex uses).",
                False,
            ),
            # The message doesn't: the hint carries the advice.
            (f"Plex is running, but not with the database file this app sees at {PLEX_DB}.", True),
        ],
    )
    def test_different_path_to_the_database_gives_same_host_path_advice_once(
        self, authed_page: Page, app_url: str, message: str, hint_shown: bool
    ) -> None:
        server = _plex_server()
        details = {
            "db_path": PLEX_DB,
            "fs_type": "fuse.shfs",
            "lock_holder": False,
            "plex_pass": True,
            "plex_version": "1.43.0.10162",
            "hint": SAME_HOST_HINT,
        }
        _mock_server_page(authed_page, server, _status(server, "needs_local_db", message, details))
        _open_tab(authed_page, app_url, server)

        alert = authed_page.locator("#markersStatusBlock .alert-warning")
        expect(alert).to_contain_text(message, timeout=5000)
        text = alert.inner_text()
        assert text.lower().count("unraid") == 1, f"same-host-path advice not shown exactly once: {text!r}"
        assert (SAME_HOST_HINT in text) is hint_shown
        # The disk itself is local; the problem is the path, which the warning explains.
        expect(authed_page.locator("#markersStatusBlock")).to_contain_text("✓ local disk")

    @pytest.mark.parametrize(
        ("state", "message", "details", "expected"),
        [
            (
                "needs_pass",
                "This Plex server has no Plex Pass, so Plex won't show any markers.",
                {
                    "db_path": PLEX_DB,
                    "fs_type": "ext4",
                    "lock_holder": True,
                    "plex_pass": False,
                    "plex_version": "1.43",
                },
                ["✕ Not active"],
            ),
            (
                "needs_plex_detection_once",
                "Plex hasn't created its marker tag yet. Run Plex's own intro or credits detection once on any item, "
                "then try again.",
                _plex_ready_details(detection={"intro": "never", "credits": "never"}),
                ["✓ Active", "✓ local disk"],
            ),
            (
                "unsupported_schema",
                "Plex's database has more than one marker tag row, so it's unclear which one Plex serves; not writing "
                "markers.",
                _plex_ready_details(),
                ["✓ Active"],
            ),
            (
                "unreachable",
                "Plex doesn't have its database open through this folder right now (Plex is stopped, or this app sees "
                "a different path to the file). Markers are only written while Plex is running.",
                {"db_path": PLEX_DB, "fs_type": "ext4", "lock_holder": False},
                ["✓ local disk"],
            ),
            ("unknown", "Couldn't check this server (TimeoutError)", {}, ["Written into this Plex server's database"]),
            (
                "misconfigured",
                f"Plex database not found at {PLEX_DB}",
                {"plex_pass": True, "plex_version": "1.43"},
                ["✓ Active"],
            ),
            ("disabled", "This server is turned off on the Servers page", {}, ["How markers get here"]),
        ],
    )
    def test_not_ready_states_show_the_message(
        self, authed_page: Page, app_url: str, state: str, message: str, details: dict, expected: list[str]
    ) -> None:
        server = _plex_server()
        _mock_server_page(authed_page, server, _status(server, state, message, details))
        _open_tab(authed_page, app_url, server)
        block = authed_page.locator("#markersStatusBlock")
        expect(block.locator(".alert-warning")).to_contain_text(message, timeout=5000)
        for text in expected:
            expect(block).to_contain_text(text)

    def test_status_error_offers_retry(self, authed_page: Page, app_url: str) -> None:
        server = _plex_server()
        captured = _mock_server_page(
            authed_page,
            server,
            _status(server, "ready", "Written into this Plex server's database", _plex_ready_details()),
            status_code=500,
        )
        _open_tab(authed_page, app_url, server)
        block = authed_page.locator("#markersStatusBlock")
        expect(block).to_contain_text("Couldn't check this server right now", timeout=5000)
        calls_before = captured["status_calls"]

        captured["state"]["status_code"] = 200
        block.locator("a", has_text="Retry").click()
        expect(block).to_contain_text("✓ Active", timeout=5000)
        assert captured["status_calls"] == calls_before + 1

    def test_save_without_opening_tab_keeps_stored_markers(self, authed_page: Page, app_url: str) -> None:
        stored = {
            "enabled": True,
            # An explicit list that equals the default selection must not be collapsed to null by a save that
            # never touched the tab.
            "library_ids": ["1", "2"],
            "plex": {"db_write_confirmed_at": "2026-09-01T10:00:00+00:00", "on_plex_redetect": "keep_plex"},
        }
        server = _plex_server(copy.deepcopy(stored))
        captured = _mock_server_page(
            authed_page,
            server,
            _status(server, "ready", "Written into this Plex server's database", _plex_ready_details()),
        )
        authed_page.goto(f"{app_url}/servers")
        edit_btn = authed_page.locator(".edit-server-btn[data-id='plex-1']")
        edit_btn.wait_for(state="visible", timeout=10000)
        edit_btn.click()
        expect(authed_page.locator("#editServerModal")).to_be_visible(timeout=5000)

        body = _save_and_get_put(authed_page, captured)
        assert body["markers"] == stored


@pytest.mark.e2e
class TestJellyfinTab:
    def test_needs_plugin_install_button_posts_to_existing_route(self, authed_page: Page, app_url: str) -> None:
        server = _vendor_server("jellyfin", "jf-1")
        captured = _mock_server_page(
            authed_page, server, _status(server, "needs_plugin", "Install the Media Preview Bridge plugin")
        )
        _open_tab(authed_page, app_url, server)
        block = authed_page.locator("#markersStatusBlock")
        expect(block).to_contain_text("Media Preview Bridge plugin", timeout=5000)
        expect(block).to_contain_text("Not installed")
        btn = authed_page.locator("#markersInstallPluginBtn")
        expect(btn).to_have_text("Install")

        btn.click()
        expect(block).to_contain_text("Jellyfin is restarting", timeout=5000)
        assert captured["installs"] == [
            {"method": "POST", "url": f"{app_url}/api/servers/jf-1/install-plugin"},
        ]
        expect(authed_page.locator("#markersPlexRedetectGroup")).to_be_hidden()
        expect(authed_page.locator("#markersEmbyRedetectGroup")).to_be_hidden()

    def test_plugin_outdated_offers_update(self, authed_page: Page, app_url: str) -> None:
        server = _vendor_server("jellyfin", "jf-1")
        status = _status(
            server,
            "plugin_outdated",
            "Update Media Preview Bridge (installed 1.2.0.0) to get markers support",
            {"plugin_version": "1.2.0.0"},
        )
        _mock_server_page(authed_page, server, status)
        _open_tab(authed_page, app_url, server)
        block = authed_page.locator("#markersStatusBlock")
        expect(block).to_contain_text("Update needed", timeout=5000)
        expect(authed_page.locator("#markersInstallPluginBtn")).to_have_text("Update")

    def test_ready_shows_version_and_what_it_can_show(self, authed_page: Page, app_url: str) -> None:
        server = _vendor_server("jellyfin", "jf-1")
        status = _status(
            server,
            "ready",
            "Media Preview Bridge plugin",
            {"plugin_version": "1.5.0.0", "can_show": ["intro", "credits", "recap", "preview"]},
        )
        _mock_server_page(authed_page, server, status)
        _open_tab(authed_page, app_url, server)
        block = authed_page.locator("#markersStatusBlock")
        expect(block).to_contain_text("1.5.0.0 ✓", timeout=5000)
        expect(block).to_contain_text("Intro · credits · recap · preview")
        expect(authed_page.locator("#markersInstallPluginBtn")).to_have_count(0)
        expect(block.locator(".alert-warning")).to_have_count(0)

    @pytest.mark.parametrize(
        ("state", "message"),
        [
            ("misconfigured", "Jellyfin rejected this server's credentials; reconnect it"),
            (
                "misconfigured",
                "Jellyfin refused the Media Preview Bridge markers endpoint; this server's API key or user needs "
                "administrator rights",
            ),
            ("unreachable", "Can't reach this Jellyfin server"),
            ("unknown", "Couldn't check this server (ConnectionError)"),
        ],
    )
    def test_not_ready_states_show_the_message(self, authed_page: Page, app_url: str, state: str, message: str) -> None:
        server = _vendor_server("jellyfin", "jf-1")
        _mock_server_page(authed_page, server, _status(server, state, message))
        _open_tab(authed_page, app_url, server)
        block = authed_page.locator("#markersStatusBlock")
        expect(block.locator(".alert-warning")).to_contain_text(message, timeout=5000)
        expect(block).to_contain_text("Intro · credits · recap · preview")
        expect(authed_page.locator("#markersInstallPluginBtn")).to_have_count(0)

    def test_jellyfin_save_sends_libraries_without_plex_block(self, authed_page: Page, app_url: str) -> None:
        server = _vendor_server("jellyfin", "jf-1")
        captured = _mock_server_page(
            authed_page, server, _status(server, "ready", "Media Preview Bridge plugin", {"plugin_version": "1.5.0.0"})
        )
        _open_tab(authed_page, app_url, server)
        expect(_lib_toggle(authed_page, "3")).not_to_be_checked(timeout=5000)
        authed_page.locator("#markersLibraryList label[for='markersLib-3']").click()
        _flip_switch_on(authed_page)
        body = _save_and_get_put(authed_page, captured)
        assert body["markers"] == {"enabled": True, "library_ids": ["1", "2", "3"]}


@pytest.mark.e2e
class TestEmbyTab:
    def test_needs_plugin_listed_in_the_catalog_installs_and_rechecks(self, authed_page: Page, app_url: str) -> None:
        server = _vendor_server("emby", "emby-1")
        captured = _mock_server_page(
            authed_page, server,
            _status(server, "needs_plugin", "Install the Media Preview Bridge for Emby plugin", {"catalog_listed": True}),
        )  # fmt: skip
        _open_tab(authed_page, app_url, server)
        block = authed_page.locator("#markersStatusBlock")
        expect(block).to_contain_text("Media Preview Bridge for Emby plugin", timeout=5000)
        expect(block).to_contain_text("Not installed")
        can_show = block.locator("div.markers-kv-label:text-is('Can show') + div")
        expect(can_show).to_have_text("Intro · credits start")
        expect(can_show).not_to_contain_text("left out")
        tooltip = can_show.locator(".info-icon").evaluate(
            "el => el.getAttribute('data-bs-original-title') || el.getAttribute('title')"
        )
        assert tooltip == (
            "Emby has no credits end: Skip Credits always skips to the end of the file, past any scene after the "
            "credits."
        )
        authed_page.locator("#markersInstallPluginBtn").click()
        expect(block).to_contain_text("Emby is restarting", timeout=5000)
        assert captured["installs"] == [{"method": "POST", "url": f"{app_url}/api/servers/emby-1/install-plugin"}]

    def test_needs_plugin_not_in_the_catalog_links_the_manual_install_guide(
        self, authed_page: Page, app_url: str
    ) -> None:
        server = _vendor_server("emby", "emby-1")
        _mock_server_page(
            authed_page, server,
            _status(server, "needs_plugin", "Install the Media Preview Bridge for Emby plugin", {"catalog_listed": False}),
        )  # fmt: skip
        _open_tab(authed_page, app_url, server)
        link = authed_page.locator("#markersStatusBlock a.markers-manual-install")
        expect(link).to_have_text("Install by hand", timeout=5000)
        expect(link).to_have_attribute(
            "href", re.compile(r"docs/guides\.md#emby-the-media-preview-bridge-for-emby-plugin$")
        )
        expect(authed_page.locator("#markersInstallPluginBtn")).to_have_count(0)

    def test_catalog_unreadable_also_links_the_manual_install_guide(self, authed_page: Page, app_url: str) -> None:
        # catalog_listed is null when Emby's own catalog couldn't be read (not False: the plugin just isn't
        # confirmed listed) — same "no Install button, link the guide" treatment as a confirmed False.
        server = _vendor_server("emby", "emby-1")
        _mock_server_page(
            authed_page, server,
            _status(server, "needs_plugin", "Install the Media Preview Bridge for Emby plugin", {"catalog_listed": None}),
        )  # fmt: skip
        _open_tab(authed_page, app_url, server)
        link = authed_page.locator("#markersStatusBlock a.markers-manual-install")
        expect(link).to_have_text("Install by hand", timeout=5000)
        expect(authed_page.locator("#markersInstallPluginBtn")).to_have_count(0)

    def test_install_that_needs_a_manual_install_shows_the_answer(self, authed_page: Page, app_url: str) -> None:
        server = _vendor_server("emby", "emby-1")
        captured = _mock_server_page(
            authed_page, server,
            _status(server, "needs_plugin", "Install the Media Preview Bridge for Emby plugin", {"catalog_listed": True}),
        )  # fmt: skip
        error = "Media Preview Bridge for Emby isn't in the Emby plugin catalog yet; install it by hand (see the Intro & Credits guide)"
        captured["install_answer"] = {
            "ok": False,
            "manual": True,
            "error": error,
            "steps": [{"step": "catalog", "ok": False, "detail": error}],
        }
        _open_tab(authed_page, app_url, server)
        authed_page.locator("#markersInstallPluginBtn").click()
        result = authed_page.locator("#markersInstallResult")
        expect(result).to_contain_text(error, timeout=5000)
        link = result.locator("a.markers-manual-install")
        expect(link).to_have_text("Install by hand")
        expect(link).to_have_attribute(
            "href", re.compile(r"docs/guides\.md#emby-the-media-preview-bridge-for-emby-plugin$")
        )

    def test_install_that_fails_partway_shows_the_error_without_a_manual_link(
        self, authed_page: Page, app_url: str
    ) -> None:
        # manual: false (the catalog listed the plugin fine; a later step — queueing the install or the restart —
        # failed instead) must never get the "Install by hand" link: there's nothing to install by hand here.
        server = _vendor_server("emby", "emby-1")
        captured = _mock_server_page(
            authed_page, server,
            _status(server, "needs_plugin", "Install the Media Preview Bridge for Emby plugin", {"catalog_listed": True}),
        )  # fmt: skip
        error = "queue_install failed: ConnectionError"
        captured["install_answer"] = {
            "ok": False,
            "manual": False,
            "error": error,
            "steps": [
                {"step": "catalog", "ok": True, "detail": "listed"},
                {"step": "queue_install", "ok": False, "detail": error},
            ],
        }
        _open_tab(authed_page, app_url, server)
        authed_page.locator("#markersInstallPluginBtn").click()
        result = authed_page.locator("#markersInstallResult")
        expect(result).to_have_text(error, timeout=5000)
        expect(result.locator("a.markers-manual-install")).to_have_count(0)

    def test_outdated_offers_update(self, authed_page: Page, app_url: str) -> None:
        server = _vendor_server("emby", "emby-1")
        status = _status(
            server,
            "plugin_outdated",
            "Update Media Preview Bridge for Emby (installed 0.9.0.0) to get markers support",
            {"plugin_version": "0.9.0.0"},
        )
        _mock_server_page(authed_page, server, status)
        _open_tab(authed_page, app_url, server)
        expect(authed_page.locator("#markersStatusBlock")).to_contain_text("Update needed", timeout=5000)
        expect(authed_page.locator("#markersInstallPluginBtn")).to_have_text("Update")

    def test_ready_shows_the_version_without_a_warning(self, authed_page: Page, app_url: str) -> None:
        server = _vendor_server("emby", "emby-1")
        _mock_server_page(
            authed_page,
            server,
            _status(server, "ready", "Media Preview Bridge for Emby plugin", {"plugin_version": "1.0.0.0"}),
        )
        _open_tab(authed_page, app_url, server)
        block = authed_page.locator("#markersStatusBlock")
        expect(block).to_contain_text("1.0.0.0 ✓", timeout=5000)
        expect(block.locator(".alert-warning")).to_have_count(0)
        expect(authed_page.locator("#markersInstallPluginBtn")).to_have_count(0)

    @pytest.mark.parametrize(
        ("state", "message", "details"),
        [
            ("ready", "Media Preview Bridge for Emby plugin", {"plugin_version": "1.0.0.0"}),
            ("needs_plugin", "Install the Media Preview Bridge for Emby plugin", {"catalog_listed": True}),
        ],
        ids=["ready", "needs-plugin"],
    )
    def test_no_premiere_key_says_viewers_cant_skip_intros(
        self, authed_page: Page, app_url: str, state: str, message: str, details: dict
    ) -> None:
        server = _vendor_server("emby", "emby-1")
        status = _status(server, state, message, {**details, "intro_skip_registered": False})
        _mock_server_page(authed_page, server, status)
        _open_tab(authed_page, app_url, server)
        premiere = authed_page.locator("#markersStatusBlock div.markers-kv-label:text-is('Emby Premiere') + div")
        note = premiere.locator(".badge.bg-warning-subtle.markers-premiere-note")
        expect(note).to_have_text(
            "Viewers can't skip intros: this Emby server has no Emby Premiere key. Skip Credits (Up Next) still works.",
            timeout=5000,
        )
        tooltip = premiere.locator(".info-icon").evaluate(
            "el => el.getAttribute('data-bs-original-title') || el.getAttribute('title')"
        )
        assert tooltip == "Emby only lets viewers skip intros on servers with Emby Premiere."

    @pytest.mark.parametrize("registered", [True, None], ids=["premiere-key", "couldnt-be-read"])
    def test_a_premiere_key_or_an_unread_registration_shows_no_note(
        self, authed_page: Page, app_url: str, registered: bool | None
    ) -> None:
        server = _vendor_server("emby", "emby-1")
        details = {"plugin_version": "1.0.0.0"}
        if registered is not None:
            details["intro_skip_registered"] = registered
        _mock_server_page(
            authed_page, server, _status(server, "ready", "Media Preview Bridge for Emby plugin", details)
        )
        _open_tab(authed_page, app_url, server)
        block = authed_page.locator("#markersStatusBlock")
        expect(block).to_contain_text("1.0.0.0 ✓", timeout=5000)
        expect(block.locator(".markers-premiere-note")).to_have_count(0)
        expect(block).not_to_contain_text("Emby Premiere")

    def test_emby_save_sends_switch_and_libraries(self, authed_page: Page, app_url: str) -> None:
        server = _vendor_server("emby", "emby-1")
        captured = _mock_server_page(
            authed_page,
            server,
            _status(server, "ready", "Media Preview Bridge for Emby plugin", {"plugin_version": "1.0.0.0"}),
        )
        _open_tab(authed_page, app_url, server)
        expect(_lib_toggle(authed_page, "3")).not_to_be_checked(timeout=5000)  # Sports is unticked by default
        authed_page.locator("#markersLibraryList label[for='markersLib-3']").click()
        _flip_switch_on(authed_page)
        expect(authed_page.locator("#markersPlexConfirmModal")).to_be_hidden()
        body = _save_and_get_put(authed_page, captured)
        assert body["markers"] == {
            "enabled": True,
            "library_ids": ["1", "2", "3"],
            "emby": {"on_emby_redetect": "restore"},
        }

    def test_emby_markers_setting_is_named_and_explained(self, authed_page: Page, app_url: str) -> None:
        server = _vendor_server("emby", "emby-1")
        status = _status(server, "ready", "Media Preview Bridge for Emby plugin", {"plugin_version": "1.0.0.0"})
        _mock_server_page(authed_page, server, status)
        _open_tab(authed_page, app_url, server)
        group = authed_page.locator("#markersEmbyRedetectGroup")
        expect(group).to_be_visible(timeout=5000)
        expect(authed_page.locator("#markersPlexRedetectGroup")).to_be_hidden()
        expect(group.locator(".fw-semibold")).to_have_text("When Emby has its own markers")
        expect(group.locator("[role='group']")).to_have_attribute("aria-label", "When Emby has its own markers")
        expect(group.locator("label[for='markersEmbyRedetectRestore']")).to_have_text("Use ours")
        expect(group.locator("label[for='markersEmbyRedetectKeep']")).to_have_text("Keep Emby's")
        expect(group.locator("#markersEmbyRedetectRestore")).to_have_attribute("value", "restore")
        expect(group.locator("#markersEmbyRedetectKeep")).to_have_attribute("value", "keep_emby")
        expect(group.locator("#markersEmbyRedetectRestore")).to_be_checked()
        icon = group.locator(".info-icon")
        tooltip = icon.evaluate("el => el.getAttribute('data-bs-original-title') || el.getAttribute('title')")
        assert tooltip == (
            "Emby can show intro and credits markers of its own, from its intro detection (Emby Premiere) or another "
            "plugin. Before we publish: 'Use ours' writes ours over them; 'Keep Emby's' leaves them and writes ours "
            "only for the types Emby has none of. After we publish, Emby's detection can replace ours: 'Use ours' "
            "writes ours again on the next Intro & Credits job that checks the file; 'Keep Emby's' keeps Emby's until "
            "you switch to 'Use ours' or Emby removes them."
        )

    @pytest.mark.parametrize(
        ("stored", "click", "sent"), [("restore", "Keep", "keep_emby"), ("keep_emby", "Restore", "restore")]
    )
    def test_emby_markers_setting_loads_the_stored_choice_and_sends_the_new_one(
        self, authed_page: Page, app_url: str, stored: str, click: str, sent: str
    ) -> None:
        markers = {"enabled": True, "library_ids": ["1"], "emby": {"on_emby_redetect": stored}}
        server = _vendor_server("emby", "emby-1", markers)
        captured = _mock_server_page(authed_page, server, _status(server, "ready", "", {"plugin_version": "1.0.0.0"}))
        _open_tab(authed_page, app_url, server)
        checked = "Keep" if stored == "keep_emby" else "Restore"
        expect(authed_page.locator(f"#markersEmbyRedetect{checked}")).to_be_checked(timeout=5000)
        authed_page.locator(f"label[for='markersEmbyRedetect{click}']").click()
        body = _save_and_get_put(authed_page, captured)
        assert body["markers"] == {"enabled": True, "library_ids": ["1"], "emby": {"on_emby_redetect": sent}}

    def test_emby_unknown_state_shows_the_message(self, authed_page: Page, app_url: str) -> None:
        server = _vendor_server("emby", "emby-1")
        _mock_server_page(authed_page, server, _status(server, "unknown", "Couldn't check this server (ValueError)"))
        _open_tab(authed_page, app_url, server)
        block = authed_page.locator("#markersStatusBlock")
        expect(block.locator(".alert-warning")).to_contain_text("Couldn't check this server (ValueError)", timeout=5000)
        expect(block).not_to_contain_text("Not installed")
