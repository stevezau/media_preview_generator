#!/usr/bin/env python3
"""Regenerate the app tour screenshots — dark mode, no real data, WebP, 2x.

Usage:
    python tests/e2e/snapshots/regen_readme.py --out docs/images/

Boots the app in a temp config dir that has been pre-seeded with
fake Plex / Jellyfin / Emby servers (see ``readme_fixture.py``) and a
handful of plausible job rows (the lab's open films), then drives
Playwright to capture one idea per shot, each one element, no full-page
scrolls:

    /automation -> tour-trigger.webp  (Sonarr/Radarr webhook setup)
    /servers    -> tour-resolve.webp  (one card per server)
    /           -> tour-extract.webp  (GPU workers making previews)
    /           -> tour-retry.webp    (a job waiting out a retry)
    /?job=...   -> tour-publish.webp  (one file's per-server publish pills)

Each shot is captured as a PNG at ``device_scale_factor=2`` (2x — CSS size
is half the pixel size) and immediately converted to WebP (quality 88,
method 6) via ``_to_webp()``; the PNG is deleted, not kept alongside it.

All captures are dark-mode + desktop-only — this script targets the
README and the docs site. For the visual-regression matrix (light + dark ×
desktop + mobile × 7 surfaces), use ``collect.py`` instead.

Defense-in-depth against leaking real data: we (a) boot against an
isolated temp config dir with fake servers, (b) stub
``/api/system/media-servers`` so the dashboard's "connected" badges
don't depend on actually reaching the fake hosts, (c) override
``window.location.origin`` to a placeholder so the webhook URL panel
renders ``http://192.168.1.10:8080/...``, and (d) run a
MutationObserver that scrubs any stray IP / ``stevez0`` string that
slips into a text node — belt-and-braces, in case an unseen surface
renders one. The four fake LAN addresses from ``readme_fixture.py``
(``APP_HOST`` + one per vendor) are allow-listed in that scrub so they
render intact instead of being collapsed to a single placeholder.

Requires: ``playwright install chromium`` (one-time setup).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Put the repo root first so ``media_preview_generator.*`` imports work
# when this file is invoked as an absolute path (the common case: see
# scripts/regen_readme_screenshots.sh). Then the parent dir so siblings
# (collect, readme_fixture) import cleanly.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from collect import (  # noqa: E402
    _get_free_port,
    _login_and_get_cookie,
    _mark_setup_complete,
    _start_app,
)
from PIL import Image  # noqa: E402
from playwright.sync_api import BrowserContext, Page, sync_playwright  # noqa: E402
from readme_fixture import (  # noqa: E402
    APP_HOST,
    APP_PORT,
    EMBY_HOST,
    FAKE_CPU_THREADS,
    FAKE_GPU_WORKERS,
    FAKE_GPUS,
    FAKE_SERVERS,
    JELLYFIN_HOST,
    PLEX_HOST,
    seed_jobs,
    write_settings,
)

PLACEHOLDER_ORIGIN = f"http://{APP_HOST}:{APP_PORT}"
# Every fake LAN address the fixture intentionally renders. The init-script
# IP scrub (below) leaves these alone and only rewrites IPs it doesn't
# recognize, so the three vendor cards keep their distinct addresses instead
# of collapsing to one placeholder.
ALLOWED_IPS = [APP_HOST, PLEX_HOST, JELLYFIN_HOST, EMBY_HOST]

# Init script (runs in every page before app JS boots). Two jobs:
# 1. Override window.location.origin so _refreshWebhookUrls() in
#    _automation_triggers.html writes placeholder URLs instead of the
#    capture-host URL.
# 2. Install a MutationObserver that rewrites text nodes + <input>
#    values containing a real IP or "stevez0" to the placeholder host.
INIT_SCRIPT = (
    """
(() => {
  const PLACEHOLDER_ORIGIN = '__PLACEHOLDER_ORIGIN__';
  const PLACEHOLDER_HOST = '__PLACEHOLDER_HOST__';
  const ALLOWED_IPS = __ALLOWED_IPS__;
  window.__scrubRan = true;
  try { localStorage.setItem('theme', 'dark'); } catch (e) {}

  // Any string matching one of these patterns is a leak risk and gets
  // rewritten to the placeholder. ``http://localhost:NNNN`` covers the
  // webhook URL widget (servers.html:35, _automation_triggers.html:495)
  // which builds its value from ``window.location.origin`` — Location.origin
  // is a getter on the prototype, so defineProperty overrides are brittle;
  // text-level scrub is simpler and safer. IPs in ALLOWED_IPS are the
  // fixture's own fake LAN addresses (readme_fixture.py) and are left
  // alone — everything else looks like a leak and gets rewritten.
  const IP_RX = /\\b(?:\\d{1,3}\\.){3}\\d{1,3}(?::\\d+)?\\b/g;
  const LEAK_RX = /stevez0[a-z0-9.-]*/gi;
  const LOCALHOST_RX = /https?:\\/\\/(?:localhost|127\\.0\\.0\\.1)(?::\\d+)?/gi;

  const scrubIps = (s) => s.replace(IP_RX, (match) => {
    const bareHost = match.split(':')[0];
    return ALLOWED_IPS.includes(bareHost) ? match : PLACEHOLDER_HOST;
  });

  const scrub = (s) => {
    if (typeof s !== 'string') return s;
    return scrubIps(
      s.replace(LOCALHOST_RX, PLACEHOLDER_ORIGIN)
    ).replace(LEAK_RX, PLACEHOLDER_HOST);
  };

  const walkNode = (node) => {
    if (!node) return;
    if (node.nodeType === 3) {
      const after = scrub(node.nodeValue);
      if (after !== node.nodeValue) node.nodeValue = after;
      return;
    }
    if (node.nodeType !== 1) return;
    if (node.tagName === 'INPUT' || node.tagName === 'TEXTAREA') {
      const cur = node.value;
      const after = scrub(cur);
      if (after !== cur) node.value = after;
    }
    for (const child of node.childNodes) walkNode(child);
  };

  const start = () => {
    walkNode(document.body);
    const obs = new MutationObserver((muts) => {
      for (const m of muts) {
        if (m.type === 'characterData') walkNode(m.target);
        for (const n of m.addedNodes) walkNode(n);
      }
    });
    obs.observe(document.body, {
      childList: true,
      subtree: true,
      characterData: true,
    });
    // Direct ``.value = ...`` assignments on <input> don't fire a
    // MutationObserver notification (the value property isn't in the
    // DOM tree), so re-sweep inputs every 250ms. Cheap — fewer than
    // a dozen inputs per page.
    setInterval(() => {
      document.querySelectorAll('input, textarea').forEach((el) => {
        const after = scrub(el.value);
        if (after !== el.value) el.value = after;
      });
    }, 250);
  };
  if (document.body) start();
  else document.addEventListener('DOMContentLoaded', start);
})();
""".replace("__PLACEHOLDER_ORIGIN__", PLACEHOLDER_ORIGIN)
    .replace("__PLACEHOLDER_HOST__", APP_HOST)
    .replace("__ALLOWED_IPS__", json.dumps(ALLOWED_IPS))
)


def _latest_release_tag() -> str:
    """Read the current release version from the latest git tag.

    Excludes ``plugin-v*`` tags — those are Jellyfin-plugin releases in a
    separate tag namespace, not app releases — so the dashboard "Version"
    line matches an actual shipped release instead of a stray dev snapshot
    like ``4.0.2.dev14``.
    """
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0", "--exclude", "plugin-v*"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        return "0.0.0"


def _fake_library_rows(server_id: str | None = None) -> list[dict]:
    """Flatten FAKE_SERVERS' enabled libraries into /api/libraries rows.

    Mirrors the shape ``_libraries_for_all_configured_servers()`` builds in
    ``api_libraries.py`` (id / title / type / server_id / server_name /
    server_type), scoped to one server when ``server_id`` is given.
    """
    rows = []
    for s in FAKE_SERVERS:
        if server_id and s["id"] != server_id:
            continue
        for lib in s["libraries"]:
            if not lib.get("enabled", True):
                continue
            rows.append(
                {
                    "id": lib["id"],
                    "title": lib["title"],
                    "type": lib["type"],
                    "server_id": s["id"],
                    "server_name": s["name"],
                    "server_type": s["type"],
                }
            )
    return rows


# The first three GPU workers are shown mid-extraction, one per lab film, so
# the dashboard tour shot (tour-extract.webp, "GPU workers making previews
# for three films") reads as a real job in flight instead of an idle pool.
# Matches three of ``readme_fixture.seed_jobs``' single-file jobs.
BUSY_WORKER_SPECS: list[dict] = [
    {"current_title": "Tears of Steel (2012)", "library_name": "Movies", "progress_percent": 34.0, "speed": "1.4x"},
    {"current_title": "Sintel (2010)", "library_name": "Movies", "progress_percent": 61.0, "speed": "1.1x"},
    {"current_title": "Big Buck Bunny (2008)", "library_name": "Movies", "progress_percent": 82.0, "speed": "1.6x"},
]


def _fake_worker_statuses() -> list[dict]:
    """Worker rows for the dashboard's "WORKERS" panel — the first three busy, the rest idle.

    ``GET /api/jobs/workers`` is a real API call, but unstubbed it's built
    server-side from ``_ensure_gpu_cache()`` — the SAME real hardware
    detection ``/api/system/status`` hits (see ``handle_system_status``
    above), except this path runs in-process on the capture host and can't
    be reached with a Playwright route stub targeting a different endpoint.
    Left unstubbed, a capture host with a real GPU (e.g. a build box with
    an NVIDIA card) leaks its actual model into the "WORKERS" panel —
    "GPU Worker 1 (Quadro P5000)" instead of the configured
    "GPU Worker 1 (NVIDIA TITAN RTX)". Mirrors the label format from
    ``jobs/worker_naming.py`` (``gpu_worker_label`` / ``cpu_worker_label``).
    """
    idle_entry = {
        "status": "idle",
        "current_title": "",
        "library_name": "",
        "progress_percent": 0,
        "speed": "0.0x",
        "remaining_time": 0.0,
        "fallback_active": False,
        "fallback_reason": None,
        "ffmpeg_started": False,
        "current_phase": "",
    }
    workers = []
    worker_id = 0
    gpu_seq = 0
    busy_index = 0
    for gpu, worker_count in zip(FAKE_GPUS, FAKE_GPU_WORKERS, strict=True):
        for _ in range(worker_count):
            worker_id += 1
            gpu_seq += 1
            entry = dict(idle_entry)
            if busy_index < len(BUSY_WORKER_SPECS):
                spec = BUSY_WORKER_SPECS[busy_index]
                busy_index += 1
                entry.update(status="processing", ffmpeg_started=True, **spec)
            workers.append(
                {
                    "worker_id": worker_id,
                    "worker_type": "GPU",
                    "worker_name": f"GPU Worker {gpu_seq} ({gpu['name']})",
                    **entry,
                }
            )
    for cpu_seq in range(1, FAKE_CPU_THREADS + 1):
        worker_id += 1
        workers.append(
            {
                "worker_id": worker_id,
                "worker_type": "CPU",
                "worker_name": f"CPU Worker {cpu_seq}",
                **idle_entry,
            }
        )
    return workers


def _install_api_stubs(ctx: BrowserContext, current_version: str) -> None:
    """Stub API responses that would otherwise fail / leak / look staged.

    Several endpoints probe the real server over the network — our fake
    hosts don't resolve, so left unstubbed they repaint the cards with
    "Auth failed" / "unreachable" badges. We intercept each relevant
    endpoint and return a synthetic "everything looks great" response.
    """

    def handle_media_servers(route):
        servers = [
            {
                "id": s["id"],
                "name": s["name"],
                "type": s["type"],
                "enabled": s["enabled"],
                "url": s["url"],
                "status": "connected",
                "server_id": s.get("server_identity"),
            }
            for s in FAKE_SERVERS
        ]
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"servers": servers, "cached": False, "ttl": 30}),
        )

    def handle_test_connection(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True, "message": "Connected"}),
        )

    def handle_health_check(route):
        # Full-shape "everything is fine" response so the Edit modal's
        # unified "Previews readiness" panel renders green. Matches the
        # shape consumed in servers.js:1564+ (trickplay_options,
        # activation, libraries, plugin_ok flags).
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "issue_count": 0,
                    "issues": [],
                    "trickplay_options": {"ok": True},
                    "activation": {"ok": True, "summary": "Next scan"},
                    "libraries": {"ok": True},
                    "plugin": {"ok": True, "installed": True, "version": "1.0.0"},
                }
            ),
        )

    def handle_notifications(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"notifications": []}),
        )

    def handle_previews_readiness(route):
        # Empty sections -> no critical/recommended checks -> the Servers
        # page's inline readiness glyph and connection pill both settle to
        # "healthy" instead of hanging on "Checking…". Without this stub,
        # servers.js's sequential per-server probe loop awaits the real
        # (unstubbed) endpoint, which tries to reach the fake host over the
        # network and blocks every server queued after it.
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"vendor": "generic", "overall_ok": True, "sections": []}),
        )

    def handle_version(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "current_version": current_version,
                    "latest_version": current_version,
                    "update_available": False,
                    "install_type": "docker",
                }
            ),
        )

    def handle_libraries(route):
        # Unstubbed, this hits the fake hosts over the network per
        # configured server and leaves the Dashboard's "Counting
        # libraries…" spinner running past the capture window.
        from urllib.parse import parse_qs, urlparse

        server_id = (parse_qs(urlparse(route.request.url).query).get("server_id") or [None])[0]
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"libraries": _fake_library_rows(server_id)}),
        )

    def handle_system_status(route):
        # Real GPU detection (gpu/enumeration.py) returns raw lspci/nvidia-smi
        # strings, which can be long ("Intel Corporation Raptor Lake-S GT1
        # [UHD Graphics 770] (rev 04)") and get ellipsis-truncated by the
        # narrow dashboard/settings columns. Stub with the same clean names
        # FAKE_SERVERS' gpu_config uses so every surface agrees.
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"gpus": FAKE_GPUS, "gpu_stats": [], "running_job": None, "pending_jobs": 0}),
        )

    ctx.route("**/api/system/media-servers", handle_media_servers)
    ctx.route("**/api/servers/*/test-connection", handle_test_connection)
    ctx.route("**/api/servers/*/health-check", handle_health_check)
    ctx.route("**/api/servers/*/previews-readiness", handle_previews_readiness)
    ctx.route("**/api/system/notifications", handle_notifications)

    def handle_jobs_workers(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"workers": _fake_worker_statuses()}),
        )

    ctx.route("**/api/system/version", handle_version)
    ctx.route("**/api/system/status", handle_system_status)
    ctx.route("**/api/libraries*", handle_libraries)
    ctx.route("**/api/jobs/workers", handle_jobs_workers)


def _to_webp(png_path: Path, quality: int = 88, method: int = 6) -> Path:
    """Convert a captured PNG to WebP in place and delete the PNG.

    Docs/README images ship as WebP only -- smaller files, same quality at
    this content type (flat UI screenshots). ``quality=88`` (up from the
    encoder default of 85) keeps small text crisp at 2x capture; ``method=6``
    is Pillow's slowest-but-smallest WebP encoder effort -- fine for a
    handful of one-off regenerations.
    """
    webp_path = png_path.with_suffix(".webp")
    with Image.open(png_path) as im:
        im.save(webp_path, "WEBP", quality=quality, method=method)
    png_path.unlink()
    print(f"[regen_readme] {png_path.name} -> {webp_path.name} ({webp_path.stat().st_size:,} bytes)", file=sys.stderr)
    return webp_path


def _install_retry_job_stub(ctx: BrowserContext) -> None:
    """Add one job waiting out a retry to the dashboard's job list.

    The retry state lasts minutes in real life and can't be seeded (JobManager turns stored RUNNING
    rows into FAILED at startup), so the real /api/jobs response is fetched and one job appended.
    """

    def handle(route) -> None:
        response = route.fetch()
        data = response.json()
        now = datetime.now(UTC)
        data["jobs"].insert(
            0,
            {
                "id": "retry-demo",
                "status": "running",
                "paused": False,
                "library_name": "Sintel (2010)",
                "server_id": "jellyfin-home",
                "server_name": "Home Jellyfin",
                "server_type": "jellyfin",
                "created_at": (now - timedelta(minutes=2)).isoformat(),
                "started_at": (now - timedelta(minutes=2)).isoformat(),
                "progress": {
                    "percent": 0,
                    "total_items": 1,
                    "processed_items": 0,
                    "retry_eta": (now + timedelta(seconds=100)).isoformat(),
                    "retry_wait_total": 120,
                },
                "config": {"trigger": "webhook", "is_retry_chain": True, "max_retries": 5, "path_count": 1},
            },
        )
        route.fulfill(response=response, json=data)

    ctx.route(re.compile(r".*/api/jobs\?page="), handle)


def _capture_element(
    page: Page, app_url: str, path: str, selector: str, out_path: Path, *, ready: str | None = None
) -> None:
    """Screenshot one element: one idea per shot, no full-page scrolls."""
    page.goto(f"{app_url}{path}", wait_until="domcontentloaded", timeout=15_000)
    page.wait_for_selector(selector, state="visible", timeout=15_000)
    if ready:
        page.wait_for_function(ready, timeout=15_000)
    page.wait_for_timeout(1200)
    page.locator(selector).first.screenshot(path=str(out_path), animations="disabled")
    print(f"[regen_readme] wrote {out_path.name}", file=sys.stderr)


TOUR_SHOTS = [
    # (file, app path, element, ready condition)
    ("tour-trigger", "/automation", "#section-webhooks-sonarr-radarr", None),
    ("tour-resolve", "/servers", "#serverList", "() => document.querySelectorAll('#serverList .card').length >= 3"),
    (
        "tour-extract",
        "/",
        "#workerStatusContainer",
        "() => !!document.querySelector('#workerStatusContainer .progress')",
    ),
    (
        "tour-retry",
        "/",
        "#activeJobsContainer",
        "() => /Waiting to retry/.test(document.getElementById('activeJobsContainer').innerText)",
    ),
]


def _capture_publish(page: Page, app_url: str, job_id: str, out_path: Path) -> None:
    """The Tears of Steel job's Files tab: one pill per server that received a preview."""
    page.goto(f"{app_url}/?job={job_id}", wait_until="domcontentloaded", timeout=15_000)
    page.click("#filesTab", timeout=15_000)
    page.wait_for_selector("#filesTabPane .badge", state="visible", timeout=15_000)
    page.wait_for_timeout(800)
    page.locator("#filesTabPane").screenshot(path=str(out_path), animations="disabled")
    print(f"[regen_readme] wrote {out_path.name}", file=sys.stderr)


def regenerate(out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    config_dir = tempfile.mkdtemp(prefix="regen_readme_")
    print(f"[regen_readme] seeding fixture in {config_dir}", file=sys.stderr)
    write_settings(config_dir)
    seeded = seed_jobs(config_dir)
    print(f"[regen_readme] seeded {seeded['count']} jobs", file=sys.stderr)

    current_version = _latest_release_tag()
    print(f"[regen_readme] stubbing version as {current_version} (no update banner)", file=sys.stderr)

    port = _get_free_port()
    print(f"[regen_readme] booting app on :{port}", file=sys.stderr)
    proc = _start_app(config_dir, port)
    app_url = f"http://localhost:{port}"

    try:
        # Setup is already complete via settings.json, but the helper is
        # idempotent and the login flow below assumes the setup gate is
        # closed. Safe to call either way.
        try:
            _mark_setup_complete(app_url)
        except Exception:
            # settings.json already sets setup_complete=True so the
            # endpoint may 204 or noop; ignore.
            pass
        cookie = _login_and_get_cookie(app_url)

        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                ctx = browser.new_context(viewport={"width": 1280, "height": 720}, device_scale_factor=2)
                ctx.add_cookies([cookie])
                ctx.add_init_script(INIT_SCRIPT)
                _install_api_stubs(ctx, current_version)
                _install_retry_job_stub(ctx)
                page = ctx.new_page()

                # Prime localStorage by visiting any page once; init
                # script already sets theme=dark before every page but
                # the initial load needs a rendered DOM first.
                page.goto(f"{app_url}/", wait_until="domcontentloaded", timeout=15_000)
                page.wait_for_timeout(500)

                written_pngs = []
                for name, path, selector, ready in TOUR_SHOTS:
                    png_path = out_dir / f"{name}.png"
                    _capture_element(page, app_url, path, selector, png_path, ready=ready)
                    written_pngs.append(png_path)

                publish_png = out_dir / "tour-publish.png"
                _capture_publish(page, app_url, seeded["tears_job_id"], publish_png)
                written_pngs.append(publish_png)

                ctx.close()

                for png_path in written_pngs:
                    _to_webp(png_path)
            finally:
                browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--out",
        type=Path,
        default=Path("docs/images"),
        help="Directory to write PNGs into (default: docs/images)",
    )
    args = ap.parse_args()
    return regenerate(args.out)


if __name__ == "__main__":
    raise SystemExit(main())
