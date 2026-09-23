"""E2E: Servers → Edit → "Intro & Credits" tab, the Plex database-write confirmation, and the Libraries tab's
Intro & Credits column (where ``markers.library_ids`` is chosen).

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

from ._mocks import mock_server_connection_probe, mock_server_previews_readiness, mock_servers_refresh_libraries

CONFIRMED_AT = "2026-09-01T10:00:00+00:00"
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


def _plex_markers_on(library_ids: list[str] | None = None, redetect: str = "restore") -> dict:
    return {
        "enabled": True,
        "library_ids": library_ids,
        "plex": {"db_write_confirmed_at": CONFIRMED_AT, "on_plex_redetect": redetect},
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


def _open_tab(page: Page, app_url: str, server: dict, tab: str = "markers") -> None:
    page.goto(f"{app_url}/servers")
    page.wait_for_load_state("domcontentloaded")
    edit_btn = page.locator(f".edit-server-btn[data-id='{server['id']}']")
    edit_btn.wait_for(state="visible", timeout=10000)
    edit_btn.click()
    expect(page.locator("#editServerModal")).to_be_visible(timeout=5000)
    _switch_tab(page, tab)


def _switch_tab(page: Page, tab: str) -> None:
    page.locator(f'#editServerModal [data-bs-target="#edit-tab-{tab}"]').click()
    expect(page.locator(f"#edit-tab-{tab}")).to_be_visible(timeout=5000)


def _lib_toggle(page: Page, lib_id: str):
    """A library's Intro & Credits switch on the Libraries tab."""
    return page.locator(f"#editLibraryList .markers-lib-toggle[data-id='{lib_id}']")


def _previews_toggle(page: Page, lib_id: str):
    return page.locator(f"#editLibraryList .edit-lib-toggle[data-id='{lib_id}']")


def _markers_column_header(page: Page):
    return page.locator("#editLibraryTable th.markers-lib-col")


def _wait_for_status(page: Page) -> None:
    # The status answer re-renders an untouched Intro & Credits column; wait for it so a click can't race it.
    expect(page.locator("#markersStatusBlock")).to_contain_text("How markers get here", timeout=5000)


def _save_and_read_put(page: Page, server_id: str) -> dict:
    """Click Save and return the body of the PUT the page sent, read off its answered request."""
    with page.expect_response(
        lambda r: r.url.endswith(f"/api/servers/{server_id}") and r.request.method == "PUT"
    ) as answered:
        page.locator("#editServerSave").click()
    expect(page.locator("#editServerModal")).to_be_hidden(timeout=10000)
    return answered.value.request.post_data_json


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
    def test_ready_status_renders(self, authed_page: Page, app_url: str) -> None:
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

        expect(authed_page.locator("#markersPlexRedetectGroup")).to_be_visible()
        expect(authed_page.locator("#markersRedetectRestore")).to_be_checked()
        expect(authed_page.locator("#markersEmbyRedetectGroup")).to_be_hidden()
        expect(authed_page.locator("#markersEnabled")).not_to_be_checked()

    def test_tab_points_to_the_libraries_tab_instead_of_listing_libraries(
        self, authed_page: Page, app_url: str
    ) -> None:
        server = _plex_server(_plex_markers_on())
        _mock_server_page(authed_page, server, _status(server, "ready", "", _plex_ready_details()))
        _open_tab(authed_page, app_url, server)

        tab = authed_page.locator("#edit-tab-markers")
        expect(tab.locator("input[type='checkbox'][data-id]")).to_have_count(0)
        pointer = authed_page.locator("#markersLibrariesPointer")
        expect(pointer).to_have_text("Choose which libraries get markers on the Libraries tab once this is on.")

        pointer.get_by_role("link", name="Libraries").click()
        expect(authed_page.locator("#edit-tab-libraries")).to_be_visible(timeout=5000)
        expect(tab).to_be_hidden()
        expect(authed_page.locator('#editServerModal [data-bs-target="#edit-tab-libraries"]')).to_have_class(
            re.compile(r"\bactive\b")
        )
        expect(_lib_toggle(authed_page, "1")).to_be_visible()

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

    @pytest.mark.parametrize(
        ("detection", "badge"),
        [({"intro": "scheduled", "credits": "scheduled"}, "On"), ({"intro": "never", "credits": "never"}, "Off")],
        ids=["detection-on", "detection-off"],
    )
    def test_detection_and_database_rows_explain_themselves(
        self, authed_page: Page, app_url: str, detection, badge
    ) -> None:
        server = _plex_server()
        details = _plex_ready_details(detection=detection)
        _mock_server_page(authed_page, server, _status(server, "ready", "", details))
        _open_tab(authed_page, app_url, server)
        block = authed_page.locator("#markersStatusBlock")
        expect(block.locator(".markers-detection")).to_have_text(badge, timeout=5000)

        def tip_of(label: str) -> str:
            value = block.locator(".markers-kv-label", has_text=label).locator("xpath=following-sibling::div[1]")
            icon = value.locator(".info-icon")
            expect(icon).to_have_count(1)
            return icon.get_attribute("data-bs-original-title") or icon.get_attribute("title")

        assert tip_of("Plex's own detection") == (
            "Whether Plex finds intros and credits itself (Plex settings → Library → Generate intro / credits video "
            'markers). When on, Plex can analyse a file again and replace our markers; "When Plex has its own '
            'markers" below decides what happens then.'
        )
        assert tip_of("Database location") == (
            "The folder of Plex's library database, as this app sees it. Markers are written straight into this "
            "database, so it has to be on a local disk of the machine Plex runs on: a database on a network share "
            "can't be written safely."
        )

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
        server = _plex_server(_plex_markers_on())
        _mock_server_page(
            authed_page,
            server,
            _status(server, "ready", "Written into this Plex server's database", _plex_ready_details()),
        )
        _open_tab(authed_page, app_url, server, tab="libraries")
        _wait_for_status(authed_page)
        expect(_lib_toggle(authed_page, "2")).to_be_checked()

        _lib_toggle(authed_page, "2").click()
        expect(_lib_toggle(authed_page, "2")).not_to_be_checked()
        _switch_tab(authed_page, "markers")
        authed_page.locator("label[for='markersRedetectKeep']").click()

        body = _save_and_read_put(authed_page, "plex-1")
        assert body["markers"] == {
            "enabled": True,
            "library_ids": ["1"],
            "plex": {"db_write_confirmed_at": CONFIRMED_AT, "on_plex_redetect": "keep_plex"},
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
        expect(block.locator(".markers-plugin-installed")).to_have_text("— installed 1.2.0.0")
        expect(authed_page.locator("#markersInstallPluginBtn")).to_have_text("Update")

    @pytest.mark.parametrize(("vendor", "server_id"), [("jellyfin", "jf-1"), ("emby", "emby-1")])
    def test_plugin_outdated_without_a_version_says_it_isnt_known(
        self, authed_page: Page, app_url: str, vendor, server_id
    ) -> None:
        server = _vendor_server(vendor, server_id)
        status = _status(
            server,
            "plugin_outdated",
            "Update Media Preview Bridge (installed unknown) to get markers support",
            {"plugin_version": None},
        )
        _mock_server_page(authed_page, server, status)
        _open_tab(authed_page, app_url, server)
        block = authed_page.locator("#markersStatusBlock")
        expect(block.locator(".markers-plugin-installed")).to_have_text("— installed version unknown", timeout=5000)
        plugin_row = block.locator(".markers-kv-label", has_text="Plugin").locator("xpath=following-sibling::div[1]")
        tip = plugin_row.locator(".info-icon")
        assert (tip.get_attribute("data-bs-original-title") or tip.get_attribute("title")) == (
            "This version of the plugin can't take intro and credits markers. Update installs the newest version."
        )

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
        _mock_server_page(
            authed_page, server, _status(server, "ready", "Media Preview Bridge plugin", {"plugin_version": "1.5.0.0"})
        )
        _open_tab(authed_page, app_url, server)
        _wait_for_status(authed_page)
        _flip_switch_on(authed_page)
        _switch_tab(authed_page, "libraries")
        expect(_lib_toggle(authed_page, "3")).not_to_be_checked()
        _lib_toggle(authed_page, "3").click()
        body = _save_and_read_put(authed_page, "jf-1")
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
        expect(authed_page.locator("#markersStatusBlock .markers-plugin-installed")).to_have_text("— installed 0.9.0.0")
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
        _mock_server_page(
            authed_page,
            server,
            _status(server, "ready", "Media Preview Bridge for Emby plugin", {"plugin_version": "1.0.0.0"}),
        )
        _open_tab(authed_page, app_url, server)
        _wait_for_status(authed_page)
        _flip_switch_on(authed_page)
        expect(authed_page.locator("#markersPlexConfirmModal")).to_be_hidden()
        _switch_tab(authed_page, "libraries")
        expect(_lib_toggle(authed_page, "3")).not_to_be_checked()  # Sports is off by default
        _lib_toggle(authed_page, "3").click()
        body = _save_and_read_put(authed_page, "emby-1")
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


@pytest.mark.e2e
class TestLibrariesTabIntroCreditsColumn:
    """Servers → Edit → Libraries: the Previews switch per library and, while Intro & Credits is on, its own column.

    The two columns write different keys of one PUT: Previews → ``libraries[].enabled``, Intro & Credits →
    ``markers.library_ids``. Save tests assert both, so a switch that leaked into the other key would fail.
    """

    def test_column_is_hidden_while_intro_and_credits_is_off(self, authed_page: Page, app_url: str) -> None:
        server = _plex_server()
        _mock_server_page(authed_page, server, _status(server, "ready", "", _plex_ready_details()))
        _open_tab(authed_page, app_url, server, tab="libraries")
        _wait_for_status(authed_page)

        expect(authed_page.locator("#editLibraryTable thead th")).to_have_text(
            ["Library", "Previews", "Intro & Credits"]
        )
        expect(_previews_toggle(authed_page, "1")).to_be_visible()
        expect(_markers_column_header(authed_page)).to_be_hidden()
        for lib_id in ("1", "2", "3"):
            expect(_lib_toggle(authed_page, lib_id)).to_be_hidden()

    def test_column_shows_with_its_explanation_when_intro_and_credits_is_on(
        self, authed_page: Page, app_url: str
    ) -> None:
        server = _plex_server(_plex_markers_on())
        _mock_server_page(authed_page, server, _status(server, "ready", "", _plex_ready_details()))
        _open_tab(authed_page, app_url, server, tab="libraries")

        header = _markers_column_header(authed_page)
        expect(header).to_be_visible()
        expect(header).to_have_text("Intro & Credits")
        icon = header.locator(".info-icon")
        tooltip = icon.evaluate("el => el.getAttribute('data-bs-original-title') || el.getAttribute('title')")
        assert tooltip == (
            "Which libraries get intro and credits markers on this server, whatever their Previews switch says. "
            "Sports libraries start off: no online source covers sports and detection isn't reliable there."
        )
        for lib_id in ("1", "2", "3"):
            expect(_lib_toggle(authed_page, lib_id)).to_be_visible()
        expect(_lib_toggle(authed_page, "1")).to_have_attribute("aria-label", "Intro & Credits for Movies")
        expect(_previews_toggle(authed_page, "1")).to_have_attribute("aria-label", "Previews for Movies")

    def test_column_follows_the_switch_without_a_reload(self, authed_page: Page, app_url: str) -> None:
        server = _vendor_server("jellyfin", "jf-1")
        _mock_server_page(authed_page, server, _status(server, "ready", "", {"plugin_version": "1.5.0.0"}))
        _open_tab(authed_page, app_url, server, tab="libraries")
        expect(_markers_column_header(authed_page)).to_be_hidden()

        _switch_tab(authed_page, "markers")
        _flip_switch_on(authed_page)
        _switch_tab(authed_page, "libraries")
        expect(_markers_column_header(authed_page)).to_be_visible()
        expect(_lib_toggle(authed_page, "2")).to_be_visible()

        _switch_tab(authed_page, "markers")
        authed_page.locator("label[for='markersEnabled']").click()
        expect(authed_page.locator("#markersEnabled")).not_to_be_checked()
        _switch_tab(authed_page, "libraries")
        expect(_markers_column_header(authed_page)).to_be_hidden()
        expect(_lib_toggle(authed_page, "2")).to_be_hidden()

    def test_cancelled_plex_confirmation_leaves_the_column_hidden(self, authed_page: Page, app_url: str) -> None:
        server = _plex_server()
        _mock_server_page(authed_page, server, _status(server, "ready", "", _plex_ready_details()))
        _open_tab(authed_page, app_url, server)
        _flip_switch_on(authed_page)
        modal = authed_page.locator("#markersPlexConfirmModal")
        expect(modal).to_be_visible(timeout=5000)
        authed_page.locator("#markersPlexConfirmCancel").click()
        expect(modal).to_be_hidden(timeout=5000)

        _switch_tab(authed_page, "libraries")
        expect(_markers_column_header(authed_page)).to_be_hidden()

    @pytest.mark.parametrize(
        ("library_ids", "checked"),
        [
            # null = the default set: every library but sports.
            (None, {"1": True, "2": True, "3": False}),
            # An explicit list is taken literally, a sports library included.
            (["3"], {"1": False, "2": False, "3": True}),
        ],
        ids=["null-default", "explicit-list"],
    )
    def test_column_shows_the_stored_choice(
        self, authed_page: Page, app_url: str, library_ids: list[str] | None, checked: dict[str, bool]
    ) -> None:
        server = _plex_server(_plex_markers_on(library_ids))
        _mock_server_page(authed_page, server, _status(server, "ready", "", _plex_ready_details()))
        _open_tab(authed_page, app_url, server, tab="libraries")
        _wait_for_status(authed_page)
        for lib_id, on in checked.items():
            if on:
                expect(_lib_toggle(authed_page, lib_id)).to_be_checked()
            else:
                expect(_lib_toggle(authed_page, lib_id)).not_to_be_checked()

    @pytest.mark.parametrize(
        ("stored", "flip", "sent"),
        [
            # The first change from the default set writes an explicit list.
            (None, "1", ["2"]),
            (None, "3", ["1", "2", "3"]),
            # A change that lands back on exactly the default set is sent as null, as the old tab did.
            (["1"], "2", None),
        ],
        ids=["untick-from-default", "tick-sports", "back-to-default"],
    )
    def test_flipping_a_library_sends_library_ids_and_leaves_previews_alone(
        self, authed_page: Page, app_url: str, stored: list[str] | None, flip: str, sent: list[str] | None
    ) -> None:
        server = _plex_server(_plex_markers_on(stored))
        server["libraries"][1]["enabled"] = False  # TV Shows has previews off; the save must keep it off
        _mock_server_page(authed_page, server, _status(server, "ready", "", _plex_ready_details()))
        _open_tab(authed_page, app_url, server, tab="libraries")
        _wait_for_status(authed_page)

        _lib_toggle(authed_page, flip).click()
        body = _save_and_read_put(authed_page, "plex-1")

        assert body["markers"]["library_ids"] == sent
        assert body["markers"]["enabled"] is True
        assert [(lib["id"], lib["enabled"]) for lib in body["libraries"]] == [
            ("1", True),
            ("2", False),
            ("3", True),
        ]

    @pytest.mark.parametrize(
        "stored",
        # An explicit list equal to the default set must stay a list, not collapse to null.
        [None, ["1", "2"]],
        ids=["null-default", "explicit-list"],
    )
    def test_flipping_previews_leaves_library_ids_alone(
        self, authed_page: Page, app_url: str, stored: list[str] | None
    ) -> None:
        server = _plex_server(_plex_markers_on(stored))
        _mock_server_page(authed_page, server, _status(server, "ready", "", _plex_ready_details()))
        _open_tab(authed_page, app_url, server, tab="libraries")
        _wait_for_status(authed_page)

        _previews_toggle(authed_page, "1").click()
        expect(_previews_toggle(authed_page, "1")).not_to_be_checked()
        expect(_lib_toggle(authed_page, "1")).to_be_checked()
        body = _save_and_read_put(authed_page, "plex-1")

        assert [(lib["id"], lib["enabled"]) for lib in body["libraries"]] == [
            ("1", False),
            ("2", True),
            ("3", True),
        ]
        assert body["markers"]["library_ids"] == stored

    def test_a_flipped_library_survives_refresh_libraries(self, authed_page: Page, app_url: str) -> None:
        server = _plex_server(_plex_markers_on())
        _mock_server_page(authed_page, server, _status(server, "ready", "", _plex_ready_details()))
        refreshed = mock_servers_refresh_libraries(authed_page, count=4)
        _open_tab(authed_page, app_url, server, tab="libraries")
        _wait_for_status(authed_page)

        _lib_toggle(authed_page, "1").click()
        # The server now reports a fourth library: the single-server GET after the refresh returns it.
        server["libraries"].append({"id": "4", "name": "Kids", "kind": "movie", "enabled": True, "remote_paths": []})
        authed_page.locator("#editRefreshLibrariesBtn").click()
        expect(_lib_toggle(authed_page, "4")).to_be_visible(timeout=5000)
        assert refreshed == [f"{app_url}/api/servers/plex-1/refresh-libraries"]

        expect(_lib_toggle(authed_page, "1")).not_to_be_checked()
        expect(_lib_toggle(authed_page, "4")).to_be_checked()
        body = _save_and_read_put(authed_page, "plex-1")
        assert body["markers"]["library_ids"] == ["2", "4"]

    def test_fits_a_phone_screen(self, authed_page: Page, app_url: str) -> None:
        authed_page.set_viewport_size({"width": 390, "height": 844})
        server = _plex_server(_plex_markers_on())
        server["libraries"][0]["name"] = "Movies — Ultra HD Remux Collection"
        _mock_server_page(authed_page, server, _status(server, "ready", "", _plex_ready_details()))
        _open_tab(authed_page, app_url, server, tab="libraries")
        expect(_markers_column_header(authed_page)).to_be_visible()

        assert authed_page.evaluate("document.documentElement.scrollWidth") <= 390
        wrapper = authed_page.locator("#edit-tab-libraries .table-responsive")
        assert wrapper.evaluate("el => el.scrollWidth <= el.clientWidth"), "the table scrolls sideways at 390 px"
        box = authed_page.locator("#editLibraryTable").bounding_box()
        assert box is not None
        assert box["x"] >= 16 and box["x"] + box["width"] <= 390 - 16, box
        for lib_id in ("1", "2", "3"):
            expect(_lib_toggle(authed_page, lib_id)).to_be_in_viewport()
            expect(_previews_toggle(authed_page, lib_id)).to_be_in_viewport()


@pytest.mark.e2e
class TestSetupHealthMarkerRows:
    """Servers → Edit → Setup Health: where the Intro & Credits rows land, and what badge they carry.

    The payload is built by the real backend builder rather than hand-copied JSON, so a change to a row's
    severity or copy shows up here as a bucket or badge change instead of passing against a stale fixture.
    """

    def _open_health(self, page: Page, app_url: str, server: dict, payload: dict | list) -> dict:
        """Open Setup Health with ``payload`` served for the readiness probe.

        A list serves its first envelope until the page installs a plugin, then one envelope per probe with
        the last repeating — the shape a "click, then poll while the server restarts" flow needs. (The page
        probes readiness more than once before that: once per card on /servers, once on opening the modal.)
        """
        details = _plex_ready_details() if server["type"] == "plex" else {}
        captured = _mock_server_page(page, server, _status(server, "ready", "", details))
        envelopes = payload if isinstance(payload, list) else [payload]
        captured["probes_after_install"] = 0

        def readiness(route: Route) -> None:
            if not captured["installs"]:
                _fulfill(route, envelopes[0])
                return
            index = min(1 + captured["probes_after_install"], len(envelopes) - 1)
            captured["probes_after_install"] += 1
            _fulfill(route, envelopes[index])

        # Registered last, so it wins over the generic readiness stub inside _mock_server_page.
        page.route("**/api/servers/*/previews-readiness", readiness)
        page.goto(f"{app_url}/servers")
        page.wait_for_load_state("domcontentloaded")
        edit_btn = page.locator(f".edit-server-btn[data-id='{server['id']}']")
        edit_btn.wait_for(state="visible", timeout=10000)
        edit_btn.click()
        expect(page.locator("#editServerModal")).to_be_visible(timeout=5000)
        page.locator('#editServerModal [data-bs-target="#edit-tab-health"]').click()
        expect(page.locator("#edit-tab-health")).to_be_visible(timeout=5000)
        return captured

    def test_failing_rows_split_across_must_fix_and_recommended(self, authed_page: Page, app_url: str) -> None:
        from media_preview_generator.markers.readiness import MarkerFacts, plex_section

        # The library's own switch couldn't be read: the server-wide row, with nothing to click.
        facts = MarkerFacts(
            enabled=True,
            state="needs_pass",
            details={
                "plex_pass": False,
                "lock_holder": True,
                "fs_type": "ext4",
                "detection": {"intro": "scheduled", "credits": "never"},
            },
            libraries=(("2", "TV Shows"),),
            library_detection=None,
        )
        payload = {"vendor": "plex", "overall_ok": False, "sections": [plex_section(facts)]}

        self._open_health(authed_page, app_url, _plex_server(), payload)

        must_fix = authed_page.locator("#editReadinessBody details[data-tier='critical']")
        expect(must_fix).to_contain_text("Intro & Credits", timeout=5000)
        expect(must_fix).to_contain_text("Skip buttons need Plex Pass")
        expect(must_fix).to_contain_text("Markers are still written, but nobody sees a skip button.")
        expect(must_fix).to_contain_text("not active")
        expect(must_fix).to_contain_text("active")
        # Nothing this app can toggle → the shipped badge, and no "Manual" chip.
        expect(must_fix).to_contain_text("Change in Plex UI")
        expect(must_fix).not_to_contain_text("Manual")

        # The Recommended bucket stays folded while there is anything to fix — open it like a user would.
        recommended = authed_page.locator("#editReadinessBody details[data-tier='recommended']")
        recommended.locator("summary").click()
        expect(recommended).to_contain_text("Plex's own detection can replace your markers")
        expect(recommended).to_contain_text("Change in Plex UI")
        # Every recommended row carries the shipped Dismiss link.
        expect(recommended.get_by_text("Dismiss")).to_be_visible()

        expect(authed_page.locator("#editReadinessBadge")).to_have_text("action needed")

    @staticmethod
    def _detection_envelope(library_detection: dict | None, *, keep_plex: bool = False) -> dict:
        """Plex ready in every way but its own detection: on server-wide for both types, TV Shows and Movies."""
        from media_preview_generator.markers.readiness import MarkerFacts, plex_section

        facts = MarkerFacts(
            enabled=True,
            state="ready",
            details={
                "plex_pass": True,
                "lock_holder": True,
                "fs_type": "ext4",
                "detection": {"intro": "scheduled", "credits": "asap"},
            },
            keep_plex=keep_plex,
            libraries=(("2", "TV Shows"), ("1", "Movies")),
            library_detection=library_detection,
        )
        return {"vendor": "plex", "overall_ok": True, "sections": [plex_section(facts)]}

    def test_detection_lists_each_library_with_its_own_turn_off(self, authed_page: Page, app_url: str) -> None:
        both_on = {
            "2": {"type": "show", "intro": True, "credits": True},
            "1": {"type": "movie", "intro": None, "credits": True},
        }
        tv_off = {**both_on, "2": {"type": "show", "intro": False, "credits": False}}
        before, after = self._detection_envelope(both_on), self._detection_envelope(tv_off)
        self._open_health(authed_page, app_url, _plex_server(), before)
        turned_off: list = []

        # Registered after the page's own stub: once the Turn off POST has been answered, Plex reads TV Shows off.
        authed_page.route(
            "**/api/servers/*/previews-readiness", lambda route: _fulfill(route, after if turned_off else before)
        )

        def turn_off(route: Route) -> None:
            turned_off.append(route.request.post_data_json)
            _fulfill(route, {"ok": True, "library_id": "2", "prefs": turned_off[-1]["prefs"]})

        authed_page.route("**/api/servers/plex-1/plex-marker-detection", turn_off)

        recommended = authed_page.locator("#editReadinessBody details[data-tier='recommended']")
        expect(recommended).to_contain_text("Plex's own detection can replace your markers", timeout=5000)
        expect(recommended).to_contain_text("Plex detects on its own in these libraries:")
        tv = recommended.locator(".readiness-library[data-library-id='2']")
        movies = recommended.locator(".readiness-library[data-library-id='1']")
        expect(tv).to_contain_text("TV Shows")
        expect(tv).to_contain_text("intro, credits")
        expect(movies).to_contain_text("Movies")
        expect(movies).to_have_text(re.compile(r"Movies\s*credits\s*Turn off"))
        expect(recommended).to_contain_text(
            "Turn off = Edit library → Advanced → Enable intro / credits detection, that library only."
        )
        # A fixable row: the Recommended badge, not "Change in Plex UI", and it can still be dismissed.
        expect(recommended).not_to_contain_text("Change in Plex UI")
        expect(recommended.get_by_text("Dismiss")).to_be_visible()

        tv.get_by_role("button", name="Turn off").click()
        expect(authed_page.locator("#readinessConfirmBody")).to_contain_text("TV Shows only", timeout=5000)
        with authed_page.expect_response(
            lambda r: r.url.endswith("/api/servers/plex-1/plex-marker-detection") and r.request.method == "POST"
        ) as answered:
            authed_page.locator("#readinessConfirmSubmit").click()

        assert answered.value.request.post_data_json == {
            "library_id": "2",
            "prefs": ["enableIntroMarkerGeneration", "enableCreditsMarkerGeneration"],
        }
        # The row refreshes: TV Shows is gone, Movies still offers its own Turn off.
        expect(authed_page.locator(".toast", has_text="Applied")).to_be_visible(timeout=10000)
        expect(authed_page.locator("#editReadinessBody .readiness-library[data-library-id='2']")).to_have_count(0)
        expect(authed_page.locator("#editReadinessBody .readiness-library[data-library-id='1']")).to_be_visible()
        assert len(turned_off) == 1

    def test_keep_plex_shows_an_all_good_row_and_no_turn_off(self, authed_page: Page, app_url: str) -> None:
        both_on = {
            "2": {"type": "show", "intro": True, "credits": True},
            "1": {"type": "movie", "intro": None, "credits": True},
        }
        self._open_health(authed_page, app_url, _plex_server(), self._detection_envelope(both_on, keep_plex=True))

        all_good = authed_page.locator("#editReadinessBody details[data-tier='ok']")
        expect(all_good).to_contain_text("Keeping Plex's own markers: its detection can stay on", timeout=5000)
        expect(authed_page.locator("#editReadinessBody details[data-tier='recommended']")).to_have_count(0)
        expect(authed_page.locator("#editReadinessBody .readiness-library")).to_have_count(0)
        expect(authed_page.locator("#editReadinessBody")).not_to_contain_text("Plex's own detection can replace")

    def test_the_feature_off_row_lands_in_all_good(self, authed_page: Page, app_url: str) -> None:
        """P-R6: emitted as recommended + ok, so it reads as a passing row rather than being dropped."""
        from media_preview_generator.markers.readiness import off_section

        payload = {"vendor": "plex", "overall_ok": True, "sections": [off_section()]}

        self._open_health(authed_page, app_url, _plex_server(), payload)

        all_good = authed_page.locator("#editReadinessBody details[data-tier='ok']")
        expect(all_good).to_contain_text("Intro & Credits", timeout=5000)
        expect(all_good).to_contain_text("Intro & Credits is off for this server")
        expect(all_good).to_contain_text("Nothing here is checked until you switch it on.")
        # No value pair on this row — the label already says the state.
        expect(all_good).not_to_contain_text("Currently")
        expect(authed_page.locator("#editReadinessBody details[data-tier='critical']")).to_have_count(0)
        expect(authed_page.locator("#editReadinessBadge")).to_have_text("ready")

    def test_embys_markers_plugin_does_not_relabel_the_card(self, authed_page: Page, app_url: str) -> None:
        """Emby's markers rows share the ``plugin`` section id with Jellyfin's previews plugin.

        That id drives the header's sub-label, which says how PREVIEWS activate. Emby previews never touch a
        plugin, so a healthy Emby must still read "ready" — not Jellyfin's "ready (instant)".
        """
        from media_preview_generator.markers.readiness import MarkerFacts, emby_plugin_section

        facts = MarkerFacts(enabled=True, state="ready", details={"plugin_version": "1.4.0"})
        payload = {"vendor": "emby", "overall_ok": True, "sections": [emby_plugin_section(facts)]}

        self._open_health(authed_page, app_url, _vendor_server("emby", "emby-1"), payload)

        all_good = authed_page.locator("#editReadinessBody details[data-tier='ok']")
        expect(all_good).to_contain_text("Media Preview Bridge for Emby plugin", timeout=5000)
        expect(authed_page.locator("#editReadinessBadge")).to_have_text("ready")

    def test_updating_an_outdated_plugin_waits_for_the_server_to_come_back(
        self, authed_page: Page, app_url: str
    ) -> None:
        """The update button must not report success while the server is still restarting.

        The plugin is already installed, so "is it installed" is true from the first poll; and while the
        server restarts it can't be read, so the "too old" row can't be built either — both halves of the
        convergence test have to hold before the toast.
        """
        from media_preview_generator.markers.readiness import MarkerFacts, emby_plugin_section

        outdated = MarkerFacts(enabled=True, state="plugin_outdated", details={"plugin_version": "1.2.0"})
        updated = MarkerFacts(enabled=True, state="ready", details={"plugin_version": "1.4.0"})
        envelopes = [
            {
                "vendor": "emby",
                "overall_ok": True,
                "sections": [emby_plugin_section(outdated, catalog_listed=True)],
            },
            # Mid-restart: Emby can't be read, so no plugin section is built at all.
            {"vendor": "emby", "overall_ok": True, "sections": []},
            {"vendor": "emby", "overall_ok": True, "sections": [emby_plugin_section(updated)]},
        ]

        captured = self._open_health(authed_page, app_url, _vendor_server("emby", "emby-1"), envelopes)
        recommended = authed_page.locator("#editReadinessBody details[data-tier='recommended']")
        expect(recommended).to_contain_text("The plugin is too old", timeout=5000)
        recommended.get_by_text("Apply recommended").click()

        # The confirmation modal, then the update itself.
        submit = authed_page.locator("#readinessConfirmSubmit")
        expect(submit).to_be_visible(timeout=5000)
        submit.click()
        expect(authed_page.locator(".toast", has_text="Applied")).to_be_visible(timeout=30000)

        assert captured["installs"], "the update must POST the install-plugin route"
        # The first probe after the POST saw a restarting server and must NOT have been accepted.
        assert captured["probes_after_install"] >= 2, (
            f"converged after {captured['probes_after_install']} probe(s) — the restarting one was accepted"
        )
        expect(authed_page.locator("#editReadinessBody")).not_to_contain_text("The plugin is too old")
