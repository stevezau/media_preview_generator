"""Final-review screenshots (PR #241, owner review 2026-09-19): every visible change of the UI lane, on the real app.

Boots the app the way the e2e suite does (``tests/e2e/conftest.py``: its own config dir, ``WEB_AUTH_TOKEN``), signs in
through the real login form (CSRF token included), and answers the page's API calls with the e2e tests' payload shapes
and synthetic names. Nothing here touches a real server or library. Run from the repo root:

    nice -n 19 /home/data/.venv/bin/python docs/design/intro-credits/evidence/screenshots/final-review/capture.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

from playwright.sync_api import Locator, Page, expect, sync_playwright

REPO = Path(__file__).resolve().parents[6]
sys.path.insert(0, str(REPO))

from tests.e2e._mocks import _fulfill_json, mock_dashboard_defaults, mock_media_servers_status  # noqa: E402
from tests.e2e.conftest import _capture_session_cookie, _start_app, get_free_port  # noqa: E402
from tests.e2e.test_intro_credits_inspector import _V1080, _V2160, _Inspector, _result, south_park  # noqa: E402
from tests.e2e.test_intro_credits_jobs_ui import _LIBRARIES, _SERVERS, _markers_job, _serve_jobs  # noqa: E402
from tests.e2e.test_intro_credits_server_tab import (  # noqa: E402
    _mock_server_page,
    _open_tab,
    _plex_ready_details,
    _plex_server,
    _status,
    _vendor_server,
)

OUT = Path(__file__).resolve().parent
TOKEN = "e2e-test-token"
# The e2e suite's width (tests/e2e/conftest.py), taller so a job's expanded row fits in one shot.
VIEWPORT = {"width": 1280, "height": 1000}

# A library run's "Decided by" counts, shaped like markers/source_counts.py stores them.
DECIDED_BY = {
    "intro": {
        "chapters": 58,
        "theintrodb+season_audio": 31,
        "theintrodb+skipdb": 12,
        "skipdb": 6,
        "introdb+server_markers": 2,
    },
    "credits": {
        "chapters": 61,
        "credits_text": 38,
        "theintrodb+server_markers": 5,
        "theintrodb+credits_text": 4,
    },
}
PUBLISHERS = [
    {
        "server_id": "plex-1",
        "server_name": "Home Plex",
        "server_type": "plex",
        "counts": {"markers_written": 104, "markers_up_to_date": 7, "markers_needs_review": 9},
    },
    {
        "server_id": "jf-1",
        "server_name": "Home Jellyfin",
        "server_type": "jellyfin",
        "counts": {"markers_written": 104, "markers_up_to_date": 7, "markers_needs_review": 9},
    },
]


def shot(page: Page, name: str, *parts: Locator, pad: int = 12) -> None:
    """Screenshot the box around every part (a tooltip lives outside its element, so it's passed as a part)."""
    parts[0].scroll_into_view_if_needed()
    page.wait_for_timeout(300)  # a dashboard poll may redraw the part; let it settle
    boxes = [box for part in parts if (box := part.bounding_box())]
    left = max(0, min(b["x"] for b in boxes) - pad)
    top = max(0, min(b["y"] for b in boxes) - pad)
    right = max(b["x"] + b["width"] for b in boxes) + pad
    bottom = max(b["y"] + b["height"] for b in boxes) + pad
    page.screenshot(
        path=str(OUT / f"{name}.png"), clip={"x": left, "y": top, "width": right - left, "height": bottom - top}
    )
    print("wrote", name)


def hover_tooltip(page: Page, icon: Locator) -> Locator:
    icon.hover()
    tip = page.locator(".tooltip.show")
    expect(tip).to_be_visible(timeout=3000)
    page.wait_for_timeout(200)  # the fade-in
    return tip


def dashboard(page: Page, app_url: str, jobs: list[dict]) -> Page:
    mock_dashboard_defaults(page)
    mock_media_servers_status(page, servers=_SERVERS)
    page.route("**/api/libraries", lambda r: _fulfill_json(r, {"libraries": _LIBRARIES}))
    page.route("**/api/processing/state", lambda r: _fulfill_json(r, {"paused": False}))
    _serve_jobs(page, {"jobs": jobs})
    page.goto(f"{app_url}/")
    page.wait_for_load_state("domcontentloaded")
    return page


def decided_by_shots(context, app_url: str) -> None:
    finished = _markers_job(
        "7c1f09aa-0000-4000-8000-000000000002",
        library_name="Intro & Credits: TV Shows",
        config={"kind": "intro_credits", "source": "manual", "libraries": [], "file_paths": []},
        publishers=PUBLISHERS,
        progress={
            "total_items": 120,
            "processed_items": 120,
            "outcome": {"markers_published": 104, "markers_up_to_date": 7, "markers_needs_review": 9},
            "marker_sources": DECIDED_BY,
        },
    )
    page = dashboard(context.new_page(), app_url, [finished])
    row = page.locator(f"#job-row-{finished['id']}")
    expect(row).to_be_visible(timeout=5000)
    page.locator(f"#job-files-toggle-{finished['id']}").click()
    detail = page.locator(f"#job-detail-{finished['id']}")
    expect(detail.locator(".marker-sources")).to_be_visible()
    shot(page, "decided-by-finished-job", detail, row)
    tip = hover_tooltip(page, detail.locator(".marker-sources .info-icon"))
    shot(page, "decided-by-tooltip", detail.locator(".marker-sources"), tip)
    page.close()

    running = _markers_job(
        "8d2e10bb-0000-4000-8000-000000000003",
        library_name="Intro & Credits: TV Shows",
        status="running",
        completed_at=None,
        config={"kind": "intro_credits", "source": "manual", "libraries": [], "file_paths": []},
        publishers=[{**p, "counts": {"markers_written": 37, "markers_needs_review": 3}} for p in PUBLISHERS],
        progress={
            "percent": 33.3,
            "total_items": 120,
            "processed_items": 40,
            "current_item": "Rick and Morty S02E04",
            "outcome": {"markers_published": 37, "markers_needs_review": 3},
            "marker_sources": {
                "intro": {"chapters": 21, "theintrodb+season_audio": 12, "skipdb": 2},
                "credits": {"chapters": 22, "credits_text": 14, "theintrodb+server_markers": 1},
            },
        },
    )
    page = dashboard(context.new_page(), app_url, [running])
    card = page.locator(f"#active-job-{running['id']}")
    expect(card.locator(".marker-sources")).to_be_visible(timeout=5000)
    shot(page, "decided-by-running-job", card)
    page.close()


def inspector_shot(context, app_url: str) -> None:
    results = [
        {**_result(item_id="4321", media_file=_V1080, title="Heat (1995)"), "type": "movie"},
        {
            **_result(item_id="4321", media_file=_V2160, title="Heat (1995)"),
            "type": "movie",
            "preview_path": "",
            "preview_exists": False,
        },
    ]

    def answer(route) -> None:
        if "path=" in route.request.url.replace("version_file=", ""):
            _fulfill_json(route, {"error": "Path is not a file inside any server library"}, status=400)
        else:
            body = {"error": "This version's file isn't on this disk", "reason": "version_not_here"}
            _fulfill_json(route, body, status=404)

    page = context.new_page()
    inspector = _Inspector(page, app_url, south_park(), results=results, item_handler=answer)
    inspector.open_result(1)
    inspector.open_tab()
    expect(page.locator("#markersInspectorBody .mk-version-not-here")).to_be_visible()
    shot(page, "inspector-version-not-on-this-disk", page.locator("#searchResults"), page.locator("#viewerPanel"))
    page.close()


def server_tab_shots(context, app_url: str) -> None:
    for vendor, server_id, version, name in (
        ("jellyfin", "jf-1", "10.11.0.3", "server-tab-jellyfin-update-needed"),
        ("emby", "emby-1", "1.0.0.0", "server-tab-emby-update-needed"),
        ("jellyfin", "jf-1", None, "server-tab-jellyfin-update-needed-version-unknown"),
    ):
        page = context.new_page()
        server = _vendor_server(vendor, server_id)
        label = "Media Preview Bridge" if vendor == "jellyfin" else "Media Preview Bridge for Emby"
        message = f"Update {label} (installed {version or 'unknown'}) to get markers support"
        _mock_server_page(page, server, _status(server, "plugin_outdated", message, {"plugin_version": version}))
        _open_tab(page, app_url, server)
        block = page.locator("#markersStatusBlock")
        expect(block).to_contain_text("Update needed", timeout=5000)
        plugin_row = block.locator(".markers-kv-label", has_text="Plugin").locator("xpath=following-sibling::div[1]")
        tip = hover_tooltip(page, plugin_row.locator(".info-icon"))
        shot(page, name, block, tip)
        page.close()

    for label, name in (
        ("Plex's own detection", "server-tab-plex-detection-tooltip"),
        ("Database location", "server-tab-plex-database-tooltip"),
    ):
        page = context.new_page()
        server = _plex_server()
        _mock_server_page(page, server, _status(server, "ready", "", _plex_ready_details()))
        _open_tab(page, app_url, server)
        block = page.locator("#markersStatusBlock")
        expect(block.locator(".markers-detection")).to_be_visible(timeout=5000)
        value = block.locator(".markers-kv-label", has_text=label).locator("xpath=following-sibling::div[1]")
        tip = hover_tooltip(page, value.locator(".info-icon"))
        shot(page, name, block, tip)
        page.close()


def start_job_shot(context, app_url: str) -> None:
    page = dashboard(context.new_page(), app_url, [])
    page.locator('button:has-text("Start New Job")').first.click()
    expect(page.locator("#newJobForm")).to_be_visible(timeout=3000)
    page.locator("#jobKindMarkers").check()
    tip = hover_tooltip(page, page.locator("#jobKindMarkersInfo"))
    shot(page, "start-job-intro-credits-tooltip", page.locator("#newJobModal .modal-content"), tip)
    page.close()


def login_shot(browser, app_url: str) -> None:
    context = browser.new_context(viewport=VIEWPORT)
    page = context.new_page()
    page.goto(f"{app_url}/login")
    context.clear_cookies()  # the session holding the form's token is gone, as after a restart
    page.locator("#token").fill("not-shown")
    page.locator('button[type="submit"]').click()
    expect(page.locator(".alert-warning")).to_contain_text("This sign-in page expired.", timeout=3000)
    shot(page, "login-page-expired", page.locator(".login-card"))
    context.close()


def main() -> None:
    config_dir = tempfile.mkdtemp(prefix="final-review-")
    port = get_free_port()
    # A developer's exported server settings would otherwise be migrated into the throwaway config on first start.
    no_real_servers = {name: "" for name in ("PLEX_URL", "PLEX_TOKEN", "PLEX_CONFIG_FOLDER", "MEDIA_PATH")}
    proc = _start_app(config_dir, port, extra_env=no_real_servers)
    app_url = f"http://localhost:{port}"
    try:
        request = urllib.request.Request(
            f"{app_url}/api/setup/complete",
            method="POST",
            headers={"X-Auth-Token": TOKEN, "Content-Type": "application/json"},
            data=b"{}",
        )
        urllib.request.urlopen(request, timeout=10).close()  # noqa: S310 (localhost only)
        cookie = _capture_session_cookie(app_url)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context(viewport=VIEWPORT)
            context.add_cookies([cookie])
            decided_by_shots(context, app_url)
            inspector_shot(context, app_url)
            server_tab_shots(context, app_url)
            start_job_shot(context, app_url)
            context.close()
            login_shot(browser, app_url)
            browser.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        shutil.rmtree(config_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
