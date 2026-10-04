"""Native loudness control through real Flask routes; only the remote Plex API is faked."""

# ruff: noqa: F811 - imported pytest fixtures are used as test parameters.

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from unittest.mock import Mock
from xml.etree.ElementTree import Element

import pytest
from playwright.sync_api import Page, expect
from werkzeug.serving import make_server

from media_preview_generator.servers.plex import PlexServer
from media_preview_generator.web.settings_manager import get_settings_manager
from tests.loudness.test_guard import guarded  # noqa: F401
from tests.markers.test_plex_detection_route import _plex
from tests.test_api_inspector import _reset_singletons, app, authed_client, client  # noqa: F401

pytestmark = pytest.mark.e2e


@pytest.fixture
def native_server(app, authed_client, guarded, monkeypatch):
    """Real local WAL schema/identity gates and HTTP routes, with remote prefs kept in memory."""
    db, guarded_cfg, remote = guarded
    state = SimpleNamespace(mode="scheduled", puts=[], prefs={}, cfg=_plex(enabled=False), db=db)
    state.cfg.update(
        name="Plex loudness lab",
        output=guarded_cfg.output,
        loudness={"enabled": True, "library_ids": ["1"]},
    )
    remote.library.sections.return_value = []
    # Simulate Plex holding its own WAL database open in another process.
    holder = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-c",
            "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute('BEGIN'); "
            "c.execute('SELECT * FROM media_streams').fetchall(); print('ready'); sys.stdin.read()",
            db._path(),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout.readline().strip() == "ready"

    def query(path, **kwargs):
        if path in ("/identity", "/"):
            return Element("MediaContainer", machineIdentifier="plex-a", version="1.43.4.12345-abc123")
        assert path == "/:/prefs?LoudnessAnalysisBehavior=never"
        assert kwargs == {"method": remote._session.put}
        state.puts.append(path)
        state.mode = "never"
        return Element("MediaContainer")

    remote.query.side_effect = query
    monkeypatch.setattr(PlexServer, "_connect", lambda self: remote)

    def get(url, **kwargs):
        prefs = {key: value for key, _, value, _, _ in PlexServer._PLEX_RECOMMENDED_PREFS}
        prefs.update(state.prefs)
        prefs["LoudnessAnalysisBehavior"] = state.mode
        container = {
            "machineIdentifier": "plex-a",
            "friendlyName": "Plex loudness lab",
            "version": "1.43.4.12345-abc123",
            "Setting": [{"id": key, "value": value} for key, value in prefs.items()],
        }
        return Mock(raise_for_status=Mock(), json=lambda: {"MediaContainer": container})

    monkeypatch.setattr("media_preview_generator.servers.plex.requests.get", get)
    get_settings_manager().set("media_servers", [state.cfg])
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}"
    cookie = authed_client.get_cookie(app.config["SESSION_COOKIE_NAME"])
    state.cookie = {"name": cookie.key, "value": cookie.value, "url": state.url}
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        holder.communicate(timeout=5)


def _open(page: Page, native_server):
    page.context.add_cookies([native_server.cookie])
    page.goto(f"{native_server.url}/servers")
    page.locator(".edit-server-btn[data-id='plex-1']").click()
    expect(page.locator("#editServerModal")).to_be_visible()
    section = page.locator("#editServerSectionSelect")
    if section.is_visible():
        section.select_option("edit-tab-health")
    else:
        page.locator('#editServerModal [data-bs-target="#edit-tab-health"]').click()
    expect(page.locator("#edit-tab-health")).to_have_css("opacity", "1")
    body = page.locator("#editReadinessBody")
    expect(body).to_contain_text("Plex's own loudness schedule")
    all_good = body.locator("details[data-tier='ok']")
    all_good.evaluate("el => { el.open = true; }")
    return body


@pytest.mark.parametrize("mode,description", [("scheduled", "As a scheduled task"), ("asap", "when media is added")])
def test_native_schedule_is_visible_without_recommending_a_change(page: Page, native_server, mode, description):
    native_server.mode = mode
    body = _open(page, native_server)
    row = body.locator(".d-flex.align-items-start", has_text="Plex's own loudness schedule").first
    expect(row).to_contain_text(description)
    expect(row).not_to_contain_text("Recommended")
    expect(row).not_to_contain_text("override")
    expect(row.get_by_role("button", name="Set to Never", exact=True)).to_be_visible()
    expect(page.locator("#editReadinessBadge")).to_have_text("ready")
    expect(page.locator("#editReadinessFixControls")).to_be_hidden()
    assert native_server.puts == []


def test_cancel_preserves_native_schedule_then_confirm_writes_and_refreshes(page: Page, native_server):
    body = _open(page, native_server)
    action = body.get_by_role("button", name="Set to Never", exact=True)
    action.click()
    modal = page.locator("#readinessConfirmModal")
    for text in ("music", "unselected", "Existing measurements"):
        expect(modal).to_contain_text(text)
    modal.get_by_role("button", name="Cancel", exact=True).click()
    expect(modal).to_be_hidden()
    assert native_server.puts == [] and native_server.mode == "scheduled"
    action.click()
    modal.get_by_role("button", name="Confirm", exact=True).click()
    expect(page.locator(".toast", has_text="Applied")).to_be_visible()
    expect(body.locator(".readiness-currently", has_text="Never")).to_be_visible()
    expect(body.get_by_role("button", name="Set to Never", exact=True)).to_have_count(0)
    assert native_server.puts == ["/:/prefs?LoudnessAnalysisBehavior=never"]
    assert get_settings_manager().get("media_servers")[0]["loudness"] == {
        "enabled": True,
        "library_ids": ["1"],
    }


def test_writer_becoming_unready_after_dialog_open_reports_failure_without_put(page: Page, native_server):
    body = _open(page, native_server)
    body.get_by_role("button", name="Set to Never", exact=True).click()
    (native_server.db.folder / "Preferences.xml").unlink()
    page.locator("#readinessConfirmSubmit").click()
    error = page.locator(".toast", has_text="Action failed")
    expect(error).to_be_visible()
    expect(error).to_contain_text("Cannot prove")
    expect(body.get_by_role("button", name="Set to Never", exact=True)).to_be_enabled()
    assert native_server.puts == [] and native_server.mode == "scheduled"


@pytest.mark.parametrize("blocked", ["empty-selection", "unready-writer"])
def test_native_setting_remains_visible_when_mutation_is_unavailable(page: Page, native_server, blocked):
    if blocked == "empty-selection":
        native_server.cfg["loudness"]["library_ids"] = []
        get_settings_manager().set("media_servers", [native_server.cfg])
    else:
        (native_server.db.folder / "Preferences.xml").unlink()
    body = _open(page, native_server)
    expect(body).to_contain_text("As a scheduled task")
    expect(body.get_by_role("button", name="Set to Never", exact=True)).to_have_count(0)
    assert native_server.puts == []


def test_bulk_fix_plan_excludes_optional_native_loudness_action(page: Page, native_server):
    native_server.prefs["FSEventLibraryUpdatesEnabled"] = False
    _open(page, native_server)
    page.locator("#editReadinessFixAllBtn").click()
    plan = page.locator("#readinessFixPlanList")
    expect(plan.locator("li")).not_to_have_count(0)
    expect(plan).not_to_contain_text("loudness")
    expect(plan).not_to_contain_text("Never")
    assert native_server.puts == []


@pytest.mark.parametrize("width,theme", [(1440, "dark"), (390, "light")])
def test_optional_setting_and_warning_fit_desktop_and_mobile(page: Page, native_server, width, theme):
    page.set_viewport_size({"width": width, "height": 1000})
    body = _open(page, native_server)
    page.evaluate("theme => document.documentElement.dataset.bsTheme = theme", theme)
    action = body.get_by_role("button", name="Set to Never", exact=True)
    action.scroll_into_view_if_needed()
    expect(action).to_be_in_viewport()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    screenshot_dir = os.environ.get("PR362_SCREENSHOTS")
    if screenshot_dir:
        Path(screenshot_dir).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(screenshot_dir) / f"health-{width}-{theme}.png"))
    action.click()
    modal = page.locator("#readinessConfirmModal")
    expect(modal.get_by_role("button", name="Confirm", exact=True)).to_be_in_viewport()
    expect(modal).to_contain_text("unselected movie and TV libraries")
    assert modal.locator(".modal-body").evaluate("el => el.scrollWidth <= el.clientWidth")
    if screenshot_dir:
        page.screenshot(path=str(Path(screenshot_dir) / f"confirm-{width}-{theme}.png"))
    modal.get_by_role("button", name="Cancel", exact=True).click()
    assert native_server.puts == []
