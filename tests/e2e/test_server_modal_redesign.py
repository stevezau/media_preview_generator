"""E2E: the redesigned Servers page and Add/Edit server modal.

Every API the page touches is mocked with ``page.route``: the server list and single-server GET, the PUT Save sends
(captured and compared whole), the Add flow's test-connection and create POSTs, the readiness probe, the marker status,
the Plex webhook and the Jellyfin Quick Connect endpoints. Payload assertions compare the full body the page sends, not
just that a request happened. The per-vendor parity check reads ``tests/data/server_modal_ids.txt`` (the committed
146-row inventory of the modal).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from playwright.sync_api import Page, Route, expect

from .conftest import expect_modal_shown, touch_edit_form, watch_modal_shown

pytestmark = pytest.mark.e2e

DATA_FILE = Path(__file__).resolve().parents[1] / "data" / "server_modal_ids.txt"
VENDORS = ("plex", "emby", "jellyfin")
LETTER = {"plex": "P", "emby": "E", "jellyfin": "J"}
CONFIRMED_AT = "2026-09-01T10:00:00+00:00"
EDIT_TABS = ("general", "health", "processing", "libraries", "paths", "excludes", "automation")
URLS = {"plex": "http://plex.invalid:32400", "emby": "http://emby.invalid:8096", "jellyfin": "http://jf.invalid:8096"}
PHONE = {"width": 390, "height": 844}


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


# ---------------------------------------------------------------------------
# Fixtures: servers and mocked API
# ---------------------------------------------------------------------------


def _libraries() -> list[dict]:
    return [
        {"id": "1", "name": "Movies", "kind": "movie", "enabled": True, "remote_paths": []},
        {"id": "2", "name": "TV Shows", "kind": "show", "enabled": True, "remote_paths": []},
        {"id": "3", "name": "Sports", "kind": "show", "enabled": True, "remote_paths": []},
    ]


def _server(vendor: str, server_id: str | None = None, **overrides) -> dict:
    server = {
        "id": server_id or f"{vendor}-1",
        "type": vendor,
        "name": f"{vendor.title()} Lab",
        "enabled": True,
        "url": URLS[vendor],
        "verify_ssl": True,
        "timeout": 30,
        "auth": {"method": "token" if vendor == "plex" else "api_key"},
        "libraries": _libraries(),
        "path_mappings": [
            {
                "remote_prefix": "/data/movies",
                "plex_prefix": "/data/movies",
                "local_prefix": "/mnt/movies",
                "webhook_prefixes": ["/media"],
            }
        ],
        "exclude_paths": [{"value": "/data/Trailers/", "type": "path"}],
        "output": {
            "plex": {"adapter": "plex_bundle", "plex_config_folder": "/tmp", "frame_interval": 10},
            "emby": {},
            "jellyfin": {"save_with_media": True, "width": 320},
        }[vendor],
        "markers": {
            "plex": {
                "enabled": False,
                "library_ids": None,
                "plex": {"db_write_confirmed_at": CONFIRMED_AT, "on_plex_redetect": "restore"},
            },
            "emby": {"enabled": False, "library_ids": None},
            "jellyfin": {"enabled": False, "library_ids": None},
        }[vendor],
    }
    if vendor == "plex":
        server["loudness"] = {"enabled": False, "library_ids": None}
    server.update(overrides)
    return server


def _fulfill(route: Route, body: object, status: int = 200) -> None:
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


def _check(cid: str, label: str, severity: str, current: object, recommended: object, reason: str) -> dict:
    action = {"action": "apply_flag", "args": {"value": recommended}, "confirm": None}
    return {
        "id": cid,
        "label": label,
        "tooltip": f"About {label}",
        "explanation": f"<p>{label}: details.</p>",
        "ok": False,
        "severity": severity,
        "current": current,
        "recommended": recommended,
        "actions": {"enable": action, "disable": action},
        "fix_action": "enable" if recommended else "disable",
        "reason": reason,
        "meta": {},
    }


def _readiness(vendor: str) -> dict:
    if vendor == "plex":
        checks = [
            _check(
                "scan-auto",
                "Scan my library automatically",
                "recommended",
                False,
                True,
                "Plex reacts to filesystem changes.",
            ),
            _check(
                "scan-partial",
                "Run a partial scan when changes are detected",
                "recommended",
                False,
                True,
                "Re-scans only what changed.",
            ),
        ]
        section = {
            "id": "library_settings",
            "title": "Library settings",
            "ok": False,
            "severity": "recommended",
            "checks": checks,
        }
    elif vendor == "jellyfin":
        checks = [
            _check(
                "trickplay",
                "Trickplay enabled in Jellyfin",
                "critical",
                False,
                True,
                "Jellyfin ignores our previews when this is off.",
            ),
            _check(
                "next-to-media",
                "Look for trickplay next to the media file",
                "critical",
                False,
                True,
                "Reads previews from the media folder.",
            ),
        ]
        section = {
            "id": "library_settings",
            "title": "Library settings",
            "ok": False,
            "severity": "critical",
            "checks": checks,
        }
    else:
        checks = [
            {
                **_check("reachable", "Server is reachable", "critical", "reachable", "reachable", ""),
                "ok": True,
                "actions": {},
            }
        ]
        section = {"id": "status", "title": "Status", "ok": True, "severity": "info", "checks": checks}
    return {"vendor": vendor, "overall_ok": vendor == "emby", "sections": [section]}


def _marker_status(server: dict) -> dict:
    vendor = server["type"]
    details = (
        {
            "db_path": "/plex/Library/Plug-in Support/Databases/com.plexapp.plugins.library.db",
            "fs_type": "ext4",
            "lock_holder": True,
            "plex_pass": True,
            "detection": {"intro": "never", "credits": "never"},
        }
        if vendor == "plex"
        else {"plugin_version": "1.0.0.0"}
    )
    return {
        "server_id": server["id"],
        "server_type": vendor,
        "enabled": server["markers"]["enabled"],
        "settings": server["markers"],
        "capability": {"state": "ready", "message": "", "details": details, "warning": ""},
        "can_show": ["intro", "credits"],
        "libraries": [
            {"id": lib["id"], "name": lib["name"], "kind": lib["kind"], "default_selected": True}
            for lib in server["libraries"]
        ],
    }


def _mock_api(page: Page, servers: list[dict], *, unreachable: tuple[str, ...] = ()) -> dict:
    """Route every endpoint the Servers page and its modals call; returns the captured request bodies."""
    captured: dict = {"puts": [], "posts": [], "tests": [], "patches": []}
    by_id = {server["id"]: server for server in servers}

    def list_handler(route: Route) -> None:
        if route.request.method == "POST":
            captured["posts"].append(route.request.post_data_json)
            _fulfill(route, {"id": "new-1", **route.request.post_data_json})
        else:
            _fulfill(route, {"servers": servers})

    def single_handler(route: Route) -> None:
        server_id = route.request.url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
        server = by_id.get(server_id, {})
        if route.request.method == "PUT":
            captured["puts"].append(route.request.post_data_json)
        _fulfill(route, server)

    def probe_handler(route: Route) -> None:
        server_id = route.request.url.split("/servers/")[1].split("/")[0]
        if server_id in unreachable:
            _fulfill(route, {"ok": False, "message": "Connection refused"})
        else:
            version = {"plex": "Plex 1.43.4", "emby": "Emby 4.9.1", "jellyfin": "Jellyfin 10.11.1"}
            _fulfill(route, {"ok": True, "message": "Connected", "version": version[by_id[server_id]["type"]]})

    def enabled_handler(route: Route) -> None:
        captured["patches"].append(route.request.post_data_json)
        _fulfill(route, {"enabled": route.request.post_data_json["enabled"]})

    def probe_new_handler(route: Route) -> None:
        captured["tests"].append(route.request.post_data_json)
        _fulfill(route, {"ok": True, "server_name": "Home Server", "version": "1.2.3"})

    for server_id in by_id:
        page.route(f"**/api/servers/{server_id}", single_handler)
    page.route("**/api/servers", list_handler)
    page.route("**/api/servers/*/test-connection", probe_handler)
    page.route("**/api/servers/*/enabled", enabled_handler)
    page.route("**/api/servers/test-connection", probe_new_handler)
    page.route(
        "**/api/servers/*/previews-readiness",
        lambda route: _fulfill(route, _readiness(by_id[route.request.url.split("/servers/")[1].split("/")[0]]["type"])),
    )
    page.route("**/api/servers/*/refresh-libraries", lambda route: _fulfill(route, {"ok": True}))
    page.route(
        "**/api/markers/servers/*/status*",
        lambda route: _fulfill(route, _marker_status(by_id[route.request.url.split("/servers/")[1].split("/")[0]])),
    )
    page.route(
        "**/api/settings/plex_webhook/status*",
        lambda route: _fulfill(
            route,
            {
                "registered_in_plex": False,
                "has_plex_pass": True,
                "default_url": "http://localhost/api/webhooks/server/x",
            },
        ),
    )
    page.route(
        "**/api/settings/*_webhook/info*",
        lambda route: _fulfill(
            route,
            {
                "webhook_url_per_server": "http://localhost/api/webhooks/server/x",
                "auth_header_name": "X-Auth-Token",
                "plugin": {
                    "plugin_name": "Webhook plugin",
                    "install_url": "https://example.com",
                    "config_steps": ["Step one", "Step two"],
                },
            },
        ),
    )
    page.route("**/api/schedules", lambda route: _fulfill(route, {"schedules": []}))
    page.route("**/api/settings/validate-*", lambda route: _fulfill(route, {"exists": True, "detail": "valid folder"}))
    return captured


def _open_edit(page: Page, app_url: str, server_id: str) -> None:
    page.goto(f"{app_url}/servers")
    button = page.locator(f".edit-server-btn[data-id='{server_id}']")
    button.wait_for(state="visible", timeout=10000)
    watch_modal_shown(page, "editServerModal")
    button.click()
    expect(page.locator("#editServerModal")).to_be_visible(timeout=5000)
    expect_modal_shown(page, "editServerModal")


def _tab(page: Page, tab: str) -> None:
    page.locator(f'#editServerModal [data-bs-target="#edit-tab-{tab}"]').click()
    expect(page.locator(f"#edit-tab-{tab}")).to_be_visible(timeout=5000)


def _save(page: Page, server_id: str) -> dict:
    with page.expect_response(
        lambda r: r.url.endswith(f"/api/servers/{server_id}") and r.request.method == "PUT"
    ) as answered:
        page.locator("#editServerSave").click()
    expect(page.locator("#editServerModal")).to_be_hidden(timeout=10000)
    return answered.value.request.post_data_json


def _open_add(page: Page, app_url: str) -> None:
    page.goto(f"{app_url}/servers")
    page.locator(".page-actions button").wait_for(state="visible", timeout=10000)
    watch_modal_shown(page, "addServerModal")
    page.locator(".page-actions button").click()
    expect(page.locator("#addServerModal")).to_be_visible(timeout=5000)
    expect_modal_shown(page, "addServerModal")


def _entries() -> list[tuple[int, str, str, str, str]]:
    entries = []
    for line in DATA_FILE.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            row, vendors, tab, mode, selector = line.split("\t")
            entries.append((int(row), vendors, tab, mode, selector))
    return entries


def _check_entries(page: Page, vendor: str, entries: list[tuple[int, str, str, str, str]]) -> None:
    for row, vendors, _tab_name, mode, selector in entries:
        located = page.locator(selector).first
        if mode == "attached":
            expect(located, f"row {row}: {selector}").to_be_attached(timeout=5000)
        elif LETTER[vendor] in vendors:
            expect(located, f"row {row}: {selector} should show for {vendor}").to_be_visible(timeout=5000)
        else:
            expect(located, f"row {row}: {selector} should be hidden for {vendor}").not_to_be_visible(timeout=5000)


# ---------------------------------------------------------------------------
# Parity: every inventoried control is where the inventory says, per vendor
# ---------------------------------------------------------------------------


class TestInventoryParity:
    @pytest.mark.parametrize("vendor", VENDORS)
    def test_edit_modal_shows_each_inventoried_control_for_its_vendors_when_every_tab_is_opened(
        self, authed_page: Page, app_url: str, vendor: str
    ) -> None:
        server = _server(vendor)
        _mock_api(authed_page, [server])
        _open_edit(authed_page, app_url, server["id"])
        entries = _entries()
        _check_entries(authed_page, vendor, [e for e in entries if e[2] == "frame"])
        for tab in EDIT_TABS:
            _tab(authed_page, tab)
            _check_entries(authed_page, vendor, [e for e in entries if e[2] == tab])

    @pytest.mark.parametrize("vendor", VENDORS)
    def test_add_flow_shows_each_inventoried_control_for_its_vendors_when_every_step_is_reached(
        self, authed_page: Page, app_url: str, vendor: str
    ) -> None:
        _mock_api(authed_page, [_server("plex")])
        _open_add(authed_page, app_url)
        entries = _entries()
        _check_entries(authed_page, vendor, [e for e in entries if e[2] == "add-type"])

        authed_page.locator(f'.server-type-btn[data-type="{vendor}"]').click()
        expect(authed_page.locator("#step-connect")).to_be_visible(timeout=3000)
        _check_entries(authed_page, vendor, [e for e in entries if e[2] == "add-connect"])

        authed_page.locator("#serverUrl").fill(URLS[vendor])
        authed_page.locator("#serverName").fill("Home Server")
        if vendor == "plex":
            authed_page.evaluate("document.querySelector('#plexToken').value = 'tok'")
        else:
            authed_page.locator('label[for="auth-key"]').click()
            authed_page.locator("#authApiKey").fill("key")
        authed_page.locator("#step-connect-test").click()
        expect(authed_page.locator("#step-result")).to_be_visible(timeout=3000)
        _check_entries(authed_page, vendor, [e for e in entries if e[2] == "add-result"])


# ---------------------------------------------------------------------------
# Edit: exact PUT payloads per vendor
# ---------------------------------------------------------------------------


def _expected_libraries(disabled: tuple[str, ...] = ()) -> list[dict]:
    libraries = _libraries()
    for library in libraries:
        library["enabled"] = library["id"] not in disabled
    return libraries


class TestEditPayloads:
    def test_plex_edit_sends_the_complete_put_when_every_section_is_changed(
        self, authed_page: Page, app_url: str
    ) -> None:
        server = _server("plex")
        captured = _mock_api(authed_page, [server])
        _open_edit(authed_page, app_url, server["id"])

        authed_page.locator("#editServerDisplayName").fill("Plex Renamed")
        authed_page.locator("#editServerUrl").fill("http://plex.renamed:32400")
        authed_page.locator("label[for='editServerVerifySsl']").click()
        authed_page.locator("#editServerEnabled").uncheck()
        authed_page.locator("#editPlexConfigFolder").fill("/plex-config")
        authed_page.locator("#editCredToggle").click()
        authed_page.locator("#editReauthPlexToken").fill("rotated-token")
        _tab(authed_page, "processing")
        authed_page.locator("#editPlexChapterThumbnails").check()
        authed_page.locator("#loudnessEnabled").check()
        authed_page.locator("#markersEnabled").check()
        authed_page.locator("label[for='markersRedetectKeep']").click()
        _tab(authed_page, "libraries")
        authed_page.locator("#editLibraryList .edit-lib-toggle[data-id='3']").uncheck()
        _tab(authed_page, "paths")
        authed_page.locator(".pm-local").first.fill("/mnt/new-movies")
        _tab(authed_page, "excludes")
        authed_page.locator("#editAddExcludePath").click()
        authed_page.locator(".ep-value").nth(1).fill("^/data/Samples/")
        authed_page.locator(".ep-type").nth(1).select_option("regex")

        assert _save(authed_page, server["id"]) == {
            "name": "Plex Renamed",
            "url": "http://plex.renamed:32400",
            "verify_ssl": False,
            "enabled": False,
            "auth": {"method": "token", "token": "rotated-token"},
            "path_mappings": [
                {
                    "remote_prefix": "/data/movies",
                    "plex_prefix": "/data/movies",
                    "local_prefix": "/mnt/new-movies",
                    "webhook_prefixes": ["/media"],
                }
            ],
            "exclude_paths": [
                {"value": "/data/Trailers/", "type": "path"},
                {"value": "^/data/Samples/", "type": "regex"},
            ],
            "libraries": _expected_libraries(disabled=("3",)),
            "output": {
                "adapter": "plex_bundle",
                "plex_config_folder": "/plex-config",
                "frame_interval": 10,
                "chapter_thumbnails": True,
            },
            "markers": {
                "enabled": True,
                "library_ids": None,
                "plex": {"db_write_confirmed_at": CONFIRMED_AT, "on_plex_redetect": "keep_plex"},
            },
            "loudness": {"enabled": True, "library_ids": None},
        }
        assert captured["puts"] != []

    def test_emby_edit_sends_the_complete_put_when_credentials_and_markers_are_changed(
        self, authed_page: Page, app_url: str
    ) -> None:
        server = _server("emby")
        _mock_api(authed_page, [server])
        _open_edit(authed_page, app_url, server["id"])

        authed_page.locator("#editServerDisplayName").fill("Emby Renamed")
        authed_page.locator("#editCredToggle").click()
        authed_page.locator("label[for='editReauthEmbyKey']").click()
        authed_page.locator("#editReauthEmbyApiKey").fill("emby-new-key")
        _tab(authed_page, "processing")
        authed_page.locator("#markersEnabled").check()
        authed_page.locator("label[for='markersEmbyRedetectKeep']").click()

        assert _save(authed_page, server["id"]) == {
            "name": "Emby Renamed",
            "url": URLS["emby"],
            "verify_ssl": True,
            "enabled": True,
            "auth": {"method": "api_key", "api_key": "emby-new-key"},
            "path_mappings": server["path_mappings"],
            "exclude_paths": server["exclude_paths"],
            "libraries": _expected_libraries(),
            "markers": {"enabled": True, "library_ids": None, "emby": {"on_emby_redetect": "keep_emby"}},
            "loudness": {},
        }

    def test_jellyfin_edit_sends_the_complete_put_when_trickplay_moves_off_media_and_password_is_verified(
        self, authed_page: Page, app_url: str
    ) -> None:
        server = _server("jellyfin")
        _mock_api(authed_page, [server])
        authed_page.route(
            "**/api/servers/auth/jellyfin/password",
            lambda route: _fulfill(route, {"ok": True, "access_token": "jf-token", "user_id": "jf-user"}),
        )
        _open_edit(authed_page, app_url, server["id"])

        authed_page.locator("#editJellyfinSaveOffMedia").check()
        expect(authed_page.locator("#editJellyfinConfigFolderGroup")).to_be_visible()
        authed_page.locator("#editJellyfinConfigFolder").fill("/jf-config")
        authed_page.locator("#editCredToggle").click()
        authed_page.locator("label[for='editReauthJfPw']").click()
        authed_page.locator("#editReauthJfUsername").fill("admin")
        authed_page.locator("#editReauthJfPassword").fill("hunter2")
        authed_page.locator("#editReauthJfPwSubmit").click()
        expect(authed_page.locator("#editReauthJfPwStatus")).to_contain_text("Verified", timeout=5000)

        assert _save(authed_page, server["id"]) == {
            "name": server["name"],
            "url": URLS["jellyfin"],
            "verify_ssl": True,
            "enabled": True,
            "auth": {"method": "password", "access_token": "jf-token", "user_id": "jf-user"},
            "path_mappings": server["path_mappings"],
            "exclude_paths": server["exclude_paths"],
            "libraries": _expected_libraries(),
            "output": {"save_with_media": False, "width": 320, "jellyfin_config_folder": "/jf-config"},
            "markers": {"enabled": False, "library_ids": None},
            "loudness": {},
        }

    def test_delete_in_the_edit_modal_removes_the_server_when_confirmed(self, authed_page: Page, app_url: str) -> None:
        server = _server("emby")
        _mock_api(authed_page, [server])
        deleted: list[str] = []
        authed_page.route(
            f"**/api/servers/{server['id']}",
            lambda route: (
                (deleted.append(route.request.url), _fulfill(route, {"ok": True}))
                if route.request.method == "DELETE"
                else route.fallback()
            ),
        )
        _open_edit(authed_page, app_url, server["id"])
        authed_page.locator("#editDeleteServerBtn").click()
        expect(authed_page.locator("#appConfirmModal")).to_be_visible(timeout=5000)
        authed_page.locator("#appConfirmModalOkBtn").click()
        expect(authed_page.locator("#editServerModal")).to_be_hidden(timeout=10000)
        assert len(deleted) == 1

    def test_test_connection_shows_a_status_pill_in_the_header_when_it_succeeds(
        self, authed_page: Page, app_url: str
    ) -> None:
        server = _server("plex")
        _mock_api(authed_page, [server])
        _open_edit(authed_page, app_url, server["id"])
        authed_page.locator("#editTestConnectionBtn").click()
        expect(authed_page.locator("#editTestConnectionResult")).to_contain_text("Connected", timeout=5000)
        expect(authed_page.locator("#editTestConnectionResult")).to_contain_text("Plex 1.43.4")
        expect(authed_page.locator("#editServerStatusPill")).to_contain_text("Connected")


# ---------------------------------------------------------------------------
# Tabs, Setup Health fix bar, unsaved-changes guard, mobile sheet
# ---------------------------------------------------------------------------


class TestEditModalBehaviour:
    def test_modal_keeps_one_height_when_tabs_change(self, authed_page: Page, app_url: str) -> None:
        server = _server("plex")
        _mock_api(authed_page, [server])
        _open_edit(authed_page, app_url, server["id"])
        heights = set()
        for tab in EDIT_TABS:
            _tab(authed_page, tab)
            box = authed_page.locator("#editServerModal .modal-content").bounding_box()
            assert box is not None
            heights.add(round(box["height"]))
        assert len(heights) == 1, f"the dialog changed height between tabs: {sorted(heights)}"

    def test_setup_health_fix_bar_stays_in_view_and_badge_counts_issues_when_probe_finds_problems(
        self, authed_page: Page, app_url: str
    ) -> None:
        server = _server("jellyfin")
        _mock_api(authed_page, [server])
        _open_edit(authed_page, app_url, server["id"])
        marker = authed_page.locator("#editHealthTabMarker")
        expect(marker).to_have_text("2", timeout=5000)
        expect(marker).to_have_class(re.compile(r"\bbad\b"))

        _tab(authed_page, "health")
        bar = authed_page.locator("#editReadinessFixControls")
        expect(bar).to_be_visible(timeout=5000)
        expect(authed_page.locator("#editReadinessFixCriticalBtn")).to_be_visible()
        expect(authed_page.locator("#editReadinessFixAllBtn")).to_be_visible()
        panes = authed_page.locator("#editServerModal .sm-panes").bounding_box()
        bar_box = bar.bounding_box()
        assert panes is not None and bar_box is not None
        assert bar_box["y"] + bar_box["height"] <= panes["y"] + panes["height"] + 1, "the fix bar is cut off"
        assert bar_box["y"] + bar_box["height"] >= panes["y"] + panes["height"] - 30, (
            "the fix bar is not pinned to the bottom"
        )

        authed_page.locator("#editReadinessFixAllBtn").click()
        expect(authed_page.locator("#readinessFixPlanList")).to_contain_text(
            "Trickplay enabled in Jellyfin", timeout=5000
        )

    def test_save_is_disabled_until_a_change_and_close_asks_before_discarding_edits(
        self, authed_page: Page, app_url: str
    ) -> None:
        server = _server("emby")
        _mock_api(authed_page, [server])
        _open_edit(authed_page, app_url, server["id"])
        save = authed_page.locator("#editServerSave")
        expect(save).to_be_disabled()
        expect(authed_page.locator("#editServerDirty")).to_be_hidden()

        authed_page.locator("#editServerDisplayName").fill("Changed name")
        expect(save).to_be_enabled()
        expect(authed_page.locator("#editServerDirty")).to_be_visible()

        authed_page.locator("#editServerCancel").click()
        expect(authed_page.locator("#editServerDiscardBar")).to_be_visible()
        expect(authed_page.locator("#editServerModal")).to_be_visible()
        authed_page.locator("#editServerKeepEditing").click()
        expect(authed_page.locator("#editServerDiscardBar")).to_be_hidden()
        expect(authed_page.locator("#editServerDisplayName")).to_have_value("Changed name")

        authed_page.locator("#editServerCancel").click()
        authed_page.locator("#editServerDiscardConfirm").click()
        expect(authed_page.locator("#editServerModal")).to_be_hidden(timeout=5000)

        authed_page.locator(f".edit-server-btn[data-id='{server['id']}']").click()
        expect(authed_page.locator("#editServerModal")).to_be_visible(timeout=5000)
        expect(authed_page.locator("#editServerDisplayName")).to_have_value(server["name"])
        expect(save).to_be_disabled()

    def test_clean_modal_closes_without_asking_when_nothing_was_changed(self, authed_page: Page, app_url: str) -> None:
        server = _server("emby")
        _mock_api(authed_page, [server])
        _open_edit(authed_page, app_url, server["id"])
        authed_page.locator("#editServerCancel").click()
        expect(authed_page.locator("#editServerModal")).to_be_hidden(timeout=5000)

    def test_ctrl_enter_saves_when_the_form_has_changes(self, authed_page: Page, app_url: str) -> None:
        server = _server("jellyfin")
        _mock_api(authed_page, [server])
        _open_edit(authed_page, app_url, server["id"])
        authed_page.locator("#editServerDisplayName").fill("Saved with keys")
        with authed_page.expect_response(
            lambda r: r.url.endswith(f"/api/servers/{server['id']}") and r.request.method == "PUT"
        ) as answered:
            authed_page.locator("#editServerDisplayName").press("Control+Enter")
        assert answered.value.request.post_data_json["name"] == "Saved with keys"
        expect(authed_page.locator("#editServerModal")).to_be_hidden(timeout=10000)

    def test_save_error_shows_in_the_footer_when_the_server_rejects_the_save(
        self, authed_page: Page, app_url: str
    ) -> None:
        server = _server("emby")
        _mock_api(authed_page, [server])
        authed_page.route(
            f"**/api/servers/{server['id']}",
            lambda route: (
                _fulfill(route, {"error": "URL is not reachable"}, 400)
                if route.request.method == "PUT"
                else _fulfill(route, server)
            ),
        )
        _open_edit(authed_page, app_url, server["id"])
        touch_edit_form(authed_page)
        authed_page.locator("#editServerSave").click()
        result = authed_page.locator("#editServerModal .modal-footer #editServerResult")
        expect(result).to_contain_text("URL is not reachable", timeout=5000)
        expect(authed_page.locator("#editServerModal")).to_be_visible()
        expect(authed_page.locator("#editServerSave")).to_be_enabled()

    def test_phone_sheet_is_full_screen_and_usable_when_viewport_is_390(self, authed_page: Page, app_url: str) -> None:
        authed_page.set_viewport_size(PHONE)
        server = _server("plex")
        _mock_api(authed_page, [server])
        _open_edit(authed_page, app_url, server["id"])

        content = authed_page.locator("#editServerModal .modal-content").bounding_box()
        assert content is not None
        assert content["width"] >= 389 and content["height"] >= 843, content
        rail = authed_page.locator("#editServerModal .sm-rail")
        assert rail.evaluate("el => el.scrollWidth > el.clientWidth"), "the tab strip should scroll sideways"
        assert authed_page.evaluate("document.documentElement.scrollWidth") <= PHONE["width"]
        first_tab = authed_page.locator("#editServerModal .sm-rail .nav-link").first.bounding_box()
        assert first_tab is not None and first_tab["height"] >= 44, first_tab

        save = authed_page.locator("#editServerSave").bounding_box()
        cancel = authed_page.locator("#editServerCancel").bounding_box()
        assert save is not None and cancel is not None
        assert abs(save["width"] - cancel["width"]) <= 2, "the footer buttons should be equal width"
        assert save["y"] + save["height"] <= PHONE["height"]

        for tab in ("health", "processing", "libraries", "paths", "excludes", "automation"):
            _tab(authed_page, tab)
            assert authed_page.evaluate("document.documentElement.scrollWidth") <= PHONE["width"], tab
            pane_ok = authed_page.locator("#editServerModal .sm-panes").evaluate(
                "el => el.scrollWidth <= el.clientWidth + 1"
            )
            assert pane_ok, f"the {tab} pane scrolls sideways at 390px"
            # The pane clips what overflows it, so check the controls themselves stay inside the sheet.
            clipped = authed_page.evaluate(
                """() => [...document.querySelectorAll('#editServerModal .tab-pane.active input, #editServerModal .tab-pane.active select, #editServerModal .tab-pane.active table')]
                    .filter(el => el.offsetParent !== null && el.type !== 'hidden' && el.type !== 'checkbox' && el.type !== 'radio')
                    .filter(el => el.getBoundingClientRect().right > window.innerWidth)
                    .map(el => el.id || el.className)"""
            )
            assert clipped == [], f"controls run off the {tab} pane at 390px: {clipped}"

        _tab(authed_page, "paths")
        expect(authed_page.locator(".pm-remote").first).to_be_visible()
        expect(authed_page.locator("#edit-tab-paths .sm-cell-lab").first).to_be_visible()
        expect(authed_page.locator("#editPathMappingsTable thead")).to_be_hidden()

    def test_section_select_follows_the_tab_strip_when_tabs_change_on_a_phone(
        self, authed_page: Page, app_url: str
    ) -> None:
        authed_page.set_viewport_size(PHONE)
        server = _server("emby")
        _mock_api(authed_page, [server])
        _open_edit(authed_page, app_url, server["id"])
        _tab(authed_page, "libraries")
        expect(authed_page.locator("#editServerSectionSelect")).to_have_value("edit-tab-libraries")
        authed_page.locator("#editServerSectionSelect").select_option("edit-tab-paths")
        expect(authed_page.locator("#edit-tab-paths")).to_be_visible()
        expect(authed_page.locator('#editServerModal [data-bs-target="#edit-tab-paths"]')).to_have_class(
            re.compile(r"\bactive\b")
        )

    def test_tab_badges_show_counts_when_the_server_has_libraries_rules_and_mappings(
        self, authed_page: Page, app_url: str
    ) -> None:
        server = _server("plex")
        _mock_api(authed_page, [server])
        _open_edit(authed_page, app_url, server["id"])
        expect(authed_page.locator("#editLibrariesTabBadge")).to_have_text("3/3")
        expect(authed_page.locator("#editPathsTabBadge")).to_have_text("1")
        expect(authed_page.locator("#editExcludesTabBadge")).to_have_text("1")
        _tab(authed_page, "libraries")
        authed_page.locator("#editLibraryList .edit-lib-toggle[data-id='2']").uncheck()
        expect(authed_page.locator("#editLibrariesTabBadge")).to_have_text("2/3")


# ---------------------------------------------------------------------------
# Add flow: every vendor end to end, auth panels, Quick Connect states
# ---------------------------------------------------------------------------


class TestAddFlow:
    def test_plex_add_sends_the_test_and_create_payloads_when_a_manual_token_is_used(
        self, authed_page: Page, app_url: str
    ) -> None:
        captured = _mock_api(authed_page, [_server("emby")])
        _open_add(authed_page, app_url)
        rail = authed_page.locator("#addServerProgress")
        expect(rail.locator("[data-step='type']")).to_have_class(re.compile(r"\bon\b"))

        authed_page.locator('.server-type-btn[data-type="plex"]').click()
        expect(authed_page.locator("#serverModalTitle")).to_have_text("Add Plex server")
        expect(rail.locator("[data-step='connect']")).to_have_class(re.compile(r"\bon\b"))
        expect(rail.locator("[data-step='type']")).to_have_class(re.compile(r"\bdone\b"))
        expect(authed_page.locator("#plexOAuthStart")).to_be_visible()
        authed_page.locator("#serverUrl").fill("http://plex.lan:32400")
        authed_page.locator("#serverName").fill("Home Plex")
        authed_page.locator("#auth-fields-token-plex summary").click()
        authed_page.locator("#plexToken").fill("plex-token")
        authed_page.locator("#plexConfigFolder").fill("/config/plex")
        authed_page.locator("#step-connect-test").click()

        expect(authed_page.locator("#connectResult")).to_contain_text("Connected to", timeout=5000)
        expect(authed_page.locator("#connectResult")).to_contain_text("Home Server")
        expect(rail.locator("[data-step='result']")).to_have_class(re.compile(r"\bon\b"))
        expected = {
            "type": "plex",
            "name": "Home Plex",
            "url": "http://plex.lan:32400",
            "auth": {"method": "token", "token": "plex-token"},
            "output": {"adapter": "plex_bundle", "plex_config_folder": "/config/plex", "frame_interval": 10},
        }
        assert captured["tests"] == [expected]
        with authed_page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/api/servers")):
            authed_page.locator("#step-result-save").click()
        assert captured["posts"] == [expected]
        expect(authed_page.locator("#addServerModal")).to_be_hidden(timeout=5000)

    def test_emby_add_sends_the_test_and_create_payloads_when_a_login_is_used(
        self, authed_page: Page, app_url: str
    ) -> None:
        captured = _mock_api(authed_page, [_server("plex")])
        authed_page.route(
            "**/api/servers/auth/emby/password",
            lambda route: _fulfill(route, {"ok": True, "access_token": "emby-tok", "user_id": "emby-user"}),
        )
        _open_add(authed_page, app_url)
        authed_page.locator('.server-type-btn[data-type="emby"]').click()
        expect(authed_page.locator('label[for="auth-quick"]')).to_be_hidden()
        expect(authed_page.locator("#auth-pw")).to_be_checked()
        authed_page.locator("#serverUrl").fill("http://emby.lan:8096")
        authed_page.locator("#serverName").fill("Home Emby")
        authed_page.locator("#authUsername").fill("admin")
        authed_page.locator("#authPassword").fill("hunter2")
        authed_page.locator("#step-connect-test").click()
        expect(authed_page.locator("#connectResult")).to_contain_text("Connected to", timeout=5000)
        expected = {
            "type": "emby",
            "name": "Home Emby",
            "url": "http://emby.lan:8096",
            "auth": {"method": "password", "access_token": "emby-tok", "user_id": "emby-user"},
        }
        assert captured["tests"] == [expected]
        with authed_page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/api/servers")):
            authed_page.locator("#step-result-save").click()
        assert captured["posts"] == [expected]

    def test_jellyfin_add_sends_the_test_and_create_payloads_when_quick_connect_is_approved(
        self, authed_page: Page, app_url: str
    ) -> None:
        captured = _mock_api(authed_page, [_server("plex")])
        polls = {"n": 0}

        def poll(route: Route) -> None:
            polls["n"] += 1
            _fulfill(route, {"ok": True, "authenticated": polls["n"] >= 2, "message": "Pending"})

        authed_page.route(
            "**/api/servers/auth/jellyfin/quick-connect/initiate",
            lambda route: _fulfill(route, {"ok": True, "code": "482913", "secret": "qc-secret"}),
        )
        authed_page.route("**/api/servers/auth/jellyfin/quick-connect/poll", poll)
        authed_page.route(
            "**/api/servers/auth/jellyfin/quick-connect/exchange",
            lambda route: _fulfill(
                route, {"ok": True, "access_token": "jf-token", "user_id": "jf-user", "server_name": "Jellyfin"}
            ),
        )
        authed_page.add_init_script("window.open = () => null;")
        _open_add(authed_page, app_url)
        authed_page.locator('.server-type-btn[data-type="jellyfin"]').click()
        expect(authed_page.locator("#auth-quick")).to_be_checked()
        expect(authed_page.locator("#auth-jellyfin-trickplay-note")).to_be_visible()
        authed_page.locator("#serverUrl").fill("http://jf.lan:8096")
        authed_page.locator("#serverName").fill("Home Jellyfin")

        authed_page.locator("#quickConnectStart").click()
        code = authed_page.locator("#quickConnectCode")
        expect(code).to_contain_text("482913")
        expect(code).to_contain_text("Waiting for approval")
        expect(code).to_contain_text("Approved", timeout=10000)
        authed_page.locator("#step-connect-test").click()
        expect(authed_page.locator("#connectResult")).to_contain_text("Connected to", timeout=5000)
        expected = {
            "type": "jellyfin",
            "name": "Home Jellyfin",
            "url": "http://jf.lan:8096",
            "auth": {"method": "quick_connect", "access_token": "jf-token", "user_id": "jf-user"},
        }
        assert captured["tests"] == [expected]
        with authed_page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/api/servers")):
            authed_page.locator("#step-result-save").click()
        assert captured["posts"] == [expected]

    def test_jellyfin_auth_panel_follows_the_method_when_the_method_is_switched(
        self, authed_page: Page, app_url: str
    ) -> None:
        _mock_api(authed_page, [_server("plex")])
        _open_add(authed_page, app_url)
        authed_page.locator('.server-type-btn[data-type="jellyfin"]').click()
        panels = {
            "quick": "#auth-fields-quick-connect",
            "pw": "#auth-fields-password",
            "key": "#auth-fields-api-key",
        }

        def only(visible: str) -> None:
            for name, selector in panels.items():
                if name == visible:
                    expect(authed_page.locator(selector)).to_be_visible()
                else:
                    expect(authed_page.locator(selector)).to_be_hidden()

        only("quick")
        authed_page.locator('label[for="auth-pw"]').click()
        only("pw")
        authed_page.locator('label[for="auth-key"]').click()
        only("key")
        authed_page.locator('label[for="auth-quick"]').click()
        only("quick")

    def test_api_key_can_be_shown_and_hidden_when_the_eye_button_is_used(self, authed_page: Page, app_url: str) -> None:
        _mock_api(authed_page, [_server("plex")])
        _open_add(authed_page, app_url)
        authed_page.locator('.server-type-btn[data-type="emby"]').click()
        authed_page.locator('label[for="auth-key"]').click()
        field = authed_page.locator("#authApiKey")
        expect(field).to_have_attribute("type", "password")
        authed_page.locator("#authApiKeyShow").click()
        expect(field).to_have_attribute("type", "text")
        authed_page.locator("#authApiKeyShow").click()
        expect(field).to_have_attribute("type", "password")

    def test_quick_connect_card_walks_expired_retry_and_cancel_when_the_code_times_out(
        self, authed_page: Page, app_url: str
    ) -> None:
        _mock_api(authed_page, [_server("plex")])
        initiates = {"n": 0}

        def initiate(route: Route) -> None:
            initiates["n"] += 1
            _fulfill(route, {"ok": True, "code": f"CODE{initiates['n']}", "secret": "s"})

        authed_page.route("**/api/servers/auth/jellyfin/quick-connect/initiate", initiate)
        authed_page.route(
            "**/api/servers/auth/jellyfin/quick-connect/poll",
            lambda route: _fulfill(
                route,
                {
                    "ok": True,
                    "authenticated": False,
                    "message": "Quick Connect session not found: the secret may have expired",
                },
            ),
        )
        authed_page.add_init_script("window.open = () => null;")
        _open_add(authed_page, app_url)
        authed_page.locator('.server-type-btn[data-type="jellyfin"]').click()
        authed_page.locator("#serverUrl").fill("http://jf.lan:8096")

        authed_page.locator("#quickConnectStart").click()
        code = authed_page.locator("#quickConnectCode")
        expect(code).to_contain_text("CODE1")
        expect(code).to_contain_text("Code expired", timeout=10000)
        expect(code.locator(".sm-qc-code")).to_have_css("text-decoration-line", "line-through")
        code.locator(".sm-qc-retry").click()
        expect(code).to_contain_text("CODE2")
        expect(code).to_contain_text("Waiting for approval")
        code.locator(".sm-qc-cancel").click()
        expect(code).to_be_hidden()
        expect(authed_page.locator("#quickConnectStart")).to_be_visible()

    def test_failed_test_connection_explains_the_problem_when_the_server_is_unreachable(
        self, authed_page: Page, app_url: str
    ) -> None:
        _mock_api(authed_page, [_server("plex")])
        authed_page.route(
            "**/api/servers/test-connection",
            lambda route: _fulfill(route, {"ok": False, "message": "Connection refused at the URL"}),
        )
        _open_add(authed_page, app_url)
        authed_page.locator('.server-type-btn[data-type="emby"]').click()
        authed_page.locator('label[for="auth-key"]').click()
        authed_page.locator("#serverUrl").fill("http://emby.lan:8096")
        authed_page.locator("#serverName").fill("Home Emby")
        authed_page.locator("#authApiKey").fill("key")
        authed_page.locator("#step-connect-test").click()
        result = authed_page.locator("#connectResult")
        expect(result).to_contain_text("Could not connect", timeout=5000)
        expect(result).to_contain_text("Connection refused at the URL")
        expect(authed_page.locator("#serverModalSubtitle")).to_have_text("Fix the connection and try again")
        authed_page.locator("#step-result-back").click()
        expect(authed_page.locator("#step-connect")).to_be_visible()

    def test_phone_add_flow_fits_the_screen_when_viewport_is_390(self, authed_page: Page, app_url: str) -> None:
        authed_page.set_viewport_size(PHONE)
        _mock_api(authed_page, [_server("plex")])
        _open_add(authed_page, app_url)
        content = authed_page.locator("#addServerModal .modal-content").bounding_box()
        assert content is not None and content["width"] >= 389 and content["height"] >= 843, content
        authed_page.locator('.server-type-btn[data-type="jellyfin"]').click()
        expect(authed_page.locator("#step-connect-test")).to_be_in_viewport()
        assert authed_page.evaluate("document.documentElement.scrollWidth") <= PHONE["width"]
        assert authed_page.locator("#addServerModal .modal-body").evaluate("el => el.scrollWidth <= el.clientWidth + 1")


# ---------------------------------------------------------------------------
# Servers page: rows for one or two servers, a grid from three, quiet states
# ---------------------------------------------------------------------------


class TestServersPageLayouts:
    @pytest.mark.parametrize("count", [1, 2])
    def test_one_or_two_servers_render_as_rows_with_an_add_row_when_the_list_is_short(
        self, authed_page: Page, app_url: str, count: int
    ) -> None:
        servers = [_server("plex"), _server("emby")][:count]
        _mock_api(authed_page, servers)
        authed_page.goto(f"{app_url}/servers")
        expect(authed_page.locator("#serverList .srv-card")).to_have_count(count, timeout=10000)
        assert authed_page.locator("#serverList").get_attribute("class") == "srv-rows"
        assert authed_page.locator("#serverList .srv-card[data-layout='row']").count() == count
        expect(authed_page.locator("#serverList .srv-addrow")).to_have_count(1)
        expect(authed_page.locator("#serverList .srv-addtile")).to_have_count(0)
        expect(authed_page.locator("#serverSummary")).to_contain_text(f"{count} server")
        for server in servers:
            row = authed_page.locator(f".srv-card[data-id='{server['id']}']")
            expect(row.locator(".srv-lib-num")).to_have_text("3")
            expect(row.locator(".srv-r-sends")).to_contain_text("Previews")
            expect(row.locator(".srv-r-sends")).to_contain_text("Intro & Credits")
        plex_row = authed_page.locator(".srv-card[data-id='plex-1']")
        expect(plex_row.locator(".srv-r-sends")).to_contain_text("Loudness")
        expect(plex_row.locator(".srv-r-sends")).to_contain_text("Chapter thumbnails")
        authed_page.locator("#serverList .srv-addrow").click()
        expect(authed_page.locator("#addServerModal")).to_be_visible(timeout=5000)

    def test_five_servers_render_as_a_grid_with_an_add_tile_when_the_list_is_long(
        self, authed_page: Page, app_url: str
    ) -> None:
        servers = [
            _server("plex", "plex-1"),
            _server("jellyfin", "jf-1"),
            _server("jellyfin", "jf-2"),
            _server("emby", "emby-1"),
            _server("plex", "plex-2", enabled=False),
        ]
        _mock_api(authed_page, servers, unreachable=("jf-2",))
        authed_page.goto(f"{app_url}/servers")
        expect(authed_page.locator("#serverList .srv-card")).to_have_count(5, timeout=10000)
        assert authed_page.locator("#serverList").get_attribute("class") == "srv-grid"
        assert authed_page.locator("#serverList .srv-card[data-layout='row']").count() == 0
        expect(authed_page.locator("#serverList .srv-addtile")).to_have_count(1)
        expect(authed_page.locator("#server-error-jf-2")).to_contain_text("Connection refused", timeout=10000)
        expect(authed_page.locator(".srv-card[data-id='plex-2']")).to_have_class(re.compile(r"\boff\b"))
        expect(authed_page.locator("#serverSummary")).to_contain_text("5 servers")
        expect(authed_page.locator("#serverSummaryStatus")).to_contain_text("need", timeout=10000)

        offsets = authed_page.evaluate(
            """() => [...document.querySelectorAll('.srv-card')].map(card => {
                const top = card.getBoundingClientRect().top;
                return ['.srv-top', '.srv-host', '.srv-issue', '.srv-lib', '.srv-foot'].map(
                    sel => Math.round(card.querySelector(sel).getBoundingClientRect().top - top));
            })"""
        )
        assert len(offsets) == 5
        assert all(row == offsets[0] for row in offsets), offsets

    @pytest.mark.parametrize("count", [1, 2, 5])
    def test_page_has_no_horizontal_overflow_when_viewport_is_390(
        self, authed_page: Page, app_url: str, count: int
    ) -> None:
        authed_page.set_viewport_size(PHONE)
        servers = [
            _server("plex", "plex-1"),
            _server("emby", "emby-1"),
            _server("jellyfin", "jf-1"),
            _server("jellyfin", "jf-2"),
            _server("plex", "plex-2", enabled=False),
        ][:count]
        servers[0]["name"] = "A server with a very long name that must be truncated rather than overflow the card"
        _mock_api(authed_page, servers, unreachable=(servers[-1]["id"],) if count > 1 else ())
        authed_page.goto(f"{app_url}/servers")
        expect(authed_page.locator("#serverList .srv-card")).to_have_count(count, timeout=10000)
        assert authed_page.evaluate("document.documentElement.scrollWidth") <= PHONE["width"]
        for card in authed_page.locator("#serverList .srv-card").all():
            box = card.bounding_box()
            assert box is not None and box["x"] >= 0 and box["x"] + box["width"] <= PHONE["width"], box

    def test_enable_toggle_still_patches_and_dims_the_row_when_switched_off(
        self, authed_page: Page, app_url: str
    ) -> None:
        servers = [_server("plex")]
        captured = _mock_api(authed_page, servers)
        authed_page.goto(f"{app_url}/servers")
        toggle = authed_page.locator("#server-enabled-plex-1")
        expect(toggle).to_be_visible(timeout=10000)
        toggle.uncheck()
        expect(authed_page.locator(".srv-card[data-id='plex-1']")).to_have_class(re.compile(r"\boff\b"), timeout=5000)
        assert captured["patches"] == [{"enabled": False}]

    def test_health_pill_opens_the_modal_on_setup_health_when_clicked_in_a_row(
        self, authed_page: Page, app_url: str
    ) -> None:
        server = _server("jellyfin")
        _mock_api(authed_page, [server])
        authed_page.goto(f"{app_url}/servers")
        pill = authed_page.locator(".server-readiness-glyph").first
        expect(pill).to_contain_text("must fix", timeout=10000)
        watch_modal_shown(authed_page, "editServerModal")
        pill.click()
        expect(authed_page.locator("#editServerModal")).to_be_visible(timeout=5000)
        expect(authed_page.locator("#edit-tab-health")).to_be_visible(timeout=5000)

    def test_empty_state_keeps_its_add_button_and_hides_the_summary_when_no_servers_exist(
        self, authed_page: Page, app_url: str
    ) -> None:
        _mock_api(authed_page, [])
        authed_page.goto(f"{app_url}/servers")
        expect(authed_page.locator("#serverList .srv-empty")).to_contain_text("No servers yet", timeout=10000)
        expect(authed_page.locator("#serverSummary")).to_be_empty()
