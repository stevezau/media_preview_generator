#!/usr/bin/env python3
"""Capture Plex, Jellyfin and Emby web players mid-scrub, showing thumbnails this app generated.

    /home/data/.venv/bin/python tests/e2e/snapshots/lab_players.py --server all
    /home/data/.venv/bin/python tests/e2e/snapshots/lab_players.py --server emby --at 0.55 --dump

Runs against the site lab (docs/design/site-redesign-lab/, Task 4): the same film on every server,
paused, with the pointer held over the seek bar at --at (a fraction of its width). A capture only
counts if the browser fetched the server's preview data while hovering (Plex: /indexes/sd, Jellyfin:
/Trickplay/), which is the proof the thumbnail on screen came from those files. Emby's thumbnail URLs
don't name a file, so for Emby the image on screen must also be byte-for-byte a frame of our BIF. 2x
device scale, Chrome for Testing (H.264-capable), autoplay allowed. --dump saves the page HTML next to
each screenshot for fixing selectors when a server's web client changes.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from PIL import Image
from playwright.sync_api import Browser, Page, Response, sync_playwright
from playwright.sync_api import Error as PlaywrightError

REPO_ROOT = Path(__file__).resolve().parents[3]
LAB = REPO_ROOT / "docs" / "design" / "site-redesign-lab"
sys.path[:0] = [str(LAB), str(REPO_ROOT)]
import lab_setup  # noqa: E402  (the lab's API helpers and constants)

from media_preview_generator.bif_reader import read_bif_frame, read_bif_metadata  # noqa: E402

CAPTURES = Path(os.environ.get("OPENFILMS_DIR", "/home/data/mlab-openfilms")) / "captures"
FILM = "Tears of Steel"
VIEWPORT = {"width": 1280, "height": 720}
LAB_USER = ("lab", "lab")
# Plex's /indexes/sd and Jellyfin's /Trickplay/ name the preview files themselves. Emby's web client
# never downloads the .bif: it asks for Items/<id>/ThumbnailSet, then Images/Thumbnail?PositionTicks=…,
# which is generic API surface that Emby would also serve from thumbnails it extracted itself. So for
# Emby this pattern only shows preview data was fetched; _prove_emby_thumbnail() is the real proof.
PROOF = {
    "plex": re.compile(r"/indexes/sd", re.I),
    "jellyfin": re.compile(r"/Trickplay/", re.I),
    "emby": re.compile(r"/ThumbnailSet|/Images/Thumbnail", re.I),
}
# Query parameters that carry credentials on Plex, Jellyfin and Emby; compared lower-cased.
SECRET_PARAMS = {"x-plex-token", "x-emby-token", "x-mediabrowser-token", "api_key", "apikey", "token", "accesstoken"}
# The seek bar: the widest range input or ARIA slider in the bottom 40% of the player. Plex Web's seek
# bar is a plain div (only its 12px thumb has role=slider), so it's matched by its class name.
SLIDER_JS = """() => {
  const h = window.innerHeight, w = window.innerWidth;
  const found = [...document.querySelectorAll('input[type=range], [role=slider], [class*="SeekBar-seekBar-"]')]
    .map((el) => el.getBoundingClientRect())
    .filter((r) => r.width > w * 0.4 && r.top > h * 0.6 && r.height > 0)
    .sort((a, b) => b.width - a.width);
  return found.length ? {x: found[0].x, y: found[0].y, width: found[0].width, height: found[0].height} : null;
}"""


SEEK_THEN_PAUSE_JS = """() => new Promise((resolve) => {
  const v = document.querySelector('video');
  v.addEventListener('seeked', () => setTimeout(() => { v.pause(); resolve(); }, 1000), {once: true});
  v.currentTime = v.duration * 0.2;
})"""


class CaptureError(RuntimeError):
    """A server's player couldn't be driven to a proven mid-scrub thumbnail."""


def _scrub(url: str) -> str:
    """The URL with every credential replaced by <redacted>, including inside a nested URL.

    Plex Web asks for seek thumbnails as /photo/:/transcode?url=/library/parts/…/indexes/sd/…?X-Plex-Token=…,
    so a token can sit inside another parameter's value.
    """
    parts = urlsplit(url)
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key.lower() in SECRET_PARAMS:
            value = "<redacted>"
        elif "?" in value:
            value = _scrub(value)
        query.append((key, value))
    return urlunsplit(parts._replace(query=urlencode(query, safe="<>")))


def _wake_controls(page: Page) -> None:
    page.mouse.move(640, 360)
    page.mouse.move(660, 620)
    page.wait_for_timeout(800)


def _scrub_html(html: str) -> str:
    """The page with credentials removed: query parameters by name, and any lab token value outright."""
    names = "|".join(re.escape(name) for name in sorted(SECRET_PARAMS))
    html = re.sub(rf"(?i)\b({names})(=|%3D)[^&%\"'<>\s]+", r"\1\2<redacted>", html)
    for value in lab_setup.ENV.values():
        if len(value) >= 8:
            html = html.replace(value, "<redacted>")
    return html


def _play_and_hover(page: Page, server: str, at: float, out: Path, dump: bool) -> tuple[dict, bytes]:
    page.wait_for_function(
        "() => { const v = document.querySelector('video'); return v && v.readyState >= 2 && v.currentTime > 1; }",
        timeout=90_000,
    )
    # Seek while the controls are up and the video still plays, then pause, as a viewer would: Jellyfin
    # and Emby only redraw their clock from a playing video's time updates while the controls show.
    _wake_controls(page)
    page.evaluate(SEEK_THEN_PAUSE_JS)
    page.wait_for_timeout(1000)
    _wake_controls(page)
    box = page.evaluate(SLIDER_JS)
    if not box:
        raise CaptureError(f"{server}: no seek bar found (run with --dump and inspect {out.with_suffix('.html')})")
    x, y = box["x"] + box["width"] * at, box["y"] + box["height"] / 2
    page.mouse.move(x - 60, y)
    page.mouse.move(x, y, steps=10)
    page.wait_for_timeout(2500)
    if dump:
        out.with_suffix(".html").write_text(_scrub_html(page.content()), encoding="utf-8")
    return {"slider_box": box}, page.screenshot()


def _login_jellyfin_style(page: Page, base: str) -> None:
    page.goto(f"{base}/web/#/login.html")
    page.wait_for_timeout(3000)
    page.fill("#txtManualName", LAB_USER[0])
    page.fill("#txtManualPassword", LAB_USER[1])
    page.click("button[type=submit]")
    page.wait_for_timeout(4000)


def _item_id(call) -> str:
    found = call("GET", f"/Items?Recursive=true&IncludeItemTypes=Movie&SearchTerm={FILM.replace(' ', '%20')}")["Items"]
    if not found:
        raise CaptureError(f"{FILM} is not in the library")
    return found[0]["Id"]


def open_jellyfin(page: Page) -> None:
    _login_jellyfin_style(page, lab_setup.JELLYFIN)
    page.goto(f"{lab_setup.JELLYFIN}/web/#/details?id={_item_id(lab_setup.jellyfin)}")
    page.wait_for_timeout(4000)
    page.click("button.btnPlay, .detailButton.btnPlay, button[data-action=resume], button[title=Play]", timeout=15_000)


def _require_emby_extraction_off() -> None:
    # Precondition, checked rather than assumed: Emby's own thumbnail extraction must stay off for the
    # library (ThumbnailImagesIntervalSeconds -1), so any thumbnail Emby serves for the film came from
    # the sidecar BIF this app wrote. The byte check in _prove_emby_thumbnail() would fail anyway if
    # Emby made its own; this names the cause.
    folder = next(
        (f for f in lab_setup.emby("GET", "/Library/VirtualFolders") if lab_setup.MEDIA in f.get("Locations", [])), None
    )
    if folder is None:
        raise CaptureError(f"emby: no library at {lab_setup.MEDIA}")
    interval = folder.get("LibraryOptions", {}).get("ThumbnailImagesIntervalSeconds")
    if interval is None or interval >= 0:
        raise CaptureError(f"emby: '{folder['Name']}' has Emby's own thumbnail extraction on (interval {interval})")


def _prove_emby_thumbnail(page: Page, served: list[Response]) -> dict:
    """Check that the thumbnail on screen is, byte for byte, a frame of the BIF this app wrote.

    Exact bytes, not a perceptual match: when the requested maxWidth is at least the BIF's width (the
    2x viewport asks for 800, the BIF is 320 wide), Emby sends the JPEG from the BIF unchanged. A
    smaller maxWidth would make Emby re-encode, and this check would then fail loudly rather than pass.
    """
    shown = page.evaluate(
        """() => { const el = document.querySelector('[style*="Images/Thumbnail"]');
                   return el ? el.style.backgroundImage : null; }"""
    )
    ticks = re.search(r"PositionTicks=(\d+)", shown or "")
    if not ticks:
        raise CaptureError("emby: no thumbnail image in the seek bubble")
    item = lab_setup.emby(
        "GET", f"/Items?Recursive=true&IncludeItemTypes=Movie&SearchTerm={FILM.replace(' ', '%20')}&Fields=Path"
    )["Items"][0]
    film = Path(item["Path"])
    sidecars = sorted((lab_setup.MEDIA_HOST / film.parent.name).glob(f"{film.stem}-*.bif"))
    if len(sidecars) != 1:
        raise CaptureError(f"emby: expected one BIF next to {film.name}, found {[p.name for p in sidecars]}")
    bif = str(sidecars[0])
    meta = read_bif_metadata(bif)
    # PositionTicks are 100 ns units, so 10,000 per millisecond.
    index = int(ticks.group(1)) // 10_000 // meta.frame_interval_ms
    matching = [r for r in served if f"PositionTicks={ticks.group(1)}" in r.url]
    if not matching:
        raise CaptureError(f"emby: the browser never fetched the thumbnail it shows ({ticks.group(0)})")
    body = matching[-1].body()
    if body != read_bif_frame(bif, index, meta):
        raise CaptureError(f"emby: the thumbnail on screen is not frame {index} of {sidecars[0].name}")
    return {"bif_match": {"bif": sidecars[0].name, "frame_index": index, "bytes": len(body), "identical": True}}


def open_emby(page: Page) -> None:
    _require_emby_extraction_off()
    page.goto(f"{lab_setup.EMBY}/web/index.html")
    page.wait_for_timeout(6000)
    page.click(f"button.cardMediaInfoItem:has-text('{LAB_USER[0]}')")
    page.wait_for_timeout(2500)
    page.fill("input[type=password]", LAB_USER[1])
    page.keyboard.press("Enter")
    page.wait_for_timeout(6000)
    server_id = lab_setup.emby("GET", "/System/Info")["Id"]
    page.goto(f"{lab_setup.EMBY}/web/index.html#!/item?id={_item_id(lab_setup.emby)}&serverId={server_id}")
    page.wait_for_timeout(5000)
    # Once an earlier run has left a playback position, Emby offers "Resume" first and the main play
    # button becomes "From Beginning".
    page.locator("button.btnResume:visible, button.btnMainPlay:visible").first.click(timeout=15_000)


def open_plex(page: Page) -> None:
    machine = lab_setup.plex("GET", "/identity")["MediaContainer"]["machineIdentifier"]
    items = lab_setup.plex("GET", f"/library/sections/{lab_setup.plex_section()}/all")["MediaContainer"]["Metadata"]
    item = next((m for m in items if m["title"] == FILM), None)
    if item is None:
        raise CaptureError(f"{FILM} is not in the Plex library")
    part = lab_setup.plex("GET", f"/library/metadata/{item['ratingKey']}")["MediaContainer"]["Metadata"][0]["Media"][0][
        "Part"
    ][0]
    if part.get("indexes") != "sd":
        raise CaptureError("Plex has no preview index for the film; run lab_setup.py verify")
    # Plex Web sends every visitor to the plex.tv sign-in, even from a network the server lets in
    # without one. The lab's PLEX_TOKEN belongs to the account that owns the lab server, so hand it to
    # Plex Web the way its own sign-in would. Only the lab Plex origin gets it; the context is in-memory.
    page.add_init_script(
        f"if (location.origin === {json.dumps(lab_setup.PLEX)} && !localStorage.getItem('myPlexAccessToken'))"
        f" localStorage.setItem('myPlexAccessToken', {json.dumps(lab_setup.ENV['PLEX_TOKEN'])});"
        # One stable client ID, so repeat runs reuse one entry in the account's authorized devices.
        f" if (location.origin === {json.dumps(lab_setup.PLEX)}) localStorage.setItem('clientID', 'mlab-site-capture');"
    )
    details = (
        f"{lab_setup.PLEX}/web/index.html#!/server/{machine}/details?key=%2Flibrary%2Fmetadata%2F{item['ratingKey']}"
    )
    page.goto(details)
    page.wait_for_timeout(6000)
    if "app.plex.tv/auth" in page.url:
        raise CaptureError("Plex Web still asks for a sign-in: plex.tv didn't accept the lab PLEX_TOKEN")
    admin = page.locator(".user-select-list-item:has(.admin-icon:not(.hidden)) a")
    if admin.count():  # a Plex Home account opens on "Select User"; the owner is the one with the crown
        admin.first.click()
        page.wait_for_timeout(4000)
        page.goto(details)
        page.wait_for_timeout(6000)
    # "Resume" replaces "Play" once an earlier run has left a playback position, and opens a menu.
    page.get_by_role("button", name=re.compile(r"^(Play|Resume)", re.I)).first.click(timeout=15_000)
    page.wait_for_timeout(1000)
    resume_from = page.get_by_text(re.compile(r"^Resume from", re.I))
    if resume_from.count():
        resume_from.first.click()


OPENERS = {"plex": open_plex, "jellyfin": open_jellyfin, "emby": open_emby}


def capture(browser: Browser, server: str, at: float, dump: bool) -> dict:
    context = browser.new_context(viewport=VIEWPORT, device_scale_factor=2)
    page = context.new_page()
    page.set_default_timeout(60_000)
    requests: list[str] = []
    page.on("request", lambda request: requests.append(request.url))
    thumbnails: list[Response] = []  # Emby's served thumbnails; their bodies are read before the page closes
    page.on("response", lambda response: thumbnails.append(response) if "/Images/Thumbnail" in response.url else None)
    out = CAPTURES / f"player-{server}.webp"
    try:
        OPENERS[server](page)
        hover_from = len(requests)
        details, png = _play_and_hover(page, server, at, out, dump)
        if server == "emby":
            details.update(_prove_emby_thumbnail(page, thumbnails))
    except PlaywrightError as exc:  # a changed web client: report it and go on to the next server
        raise CaptureError(f"{server}: {exc.message.splitlines()[0]} (re-run with --dump)") from exc
    finally:
        context.close()
    # Matched decoded, because Plex Web's thumbnail request carries the index path URL-encoded in ?url=.
    proof = [_scrub(url) for url in requests[hover_from:] if PROOF[server].search(unquote(url))]
    if not proof:
        raise CaptureError(f"{server}: no preview data was fetched while hovering, so the picture proves nothing")
    # Written only now, so a rejected capture never sits where compose_site_images.py would pick it up.
    Image.open(io.BytesIO(png)).convert("RGB").save(out, "WEBP", quality=88, method=6)
    return {"server": server, "file": out.name, "proof_requests": proof[:3], **details}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--server", choices=["plex", "jellyfin", "emby", "all"], default="all")
    parser.add_argument("--at", type=float, default=0.42, help="where on the seek bar to hover, 0-1")
    parser.add_argument("--dump", action="store_true", help="save each page's HTML next to its screenshot")
    args = parser.parse_args()
    CAPTURES.mkdir(parents=True, exist_ok=True)
    servers = ["plex", "jellyfin", "emby"] if args.server == "all" else [args.server]
    record_path = LAB / "results" / "captures.json"
    record = json.loads(record_path.read_text(encoding="utf-8")) if record_path.is_file() else {"captures": []}
    failures = 0
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chromium", args=["--autoplay-policy=no-user-gesture-required"])
        probe = browser.new_page()
        if (
            probe.evaluate("document.createElement('video').canPlayType('video/mp4; codecs=\"avc1.640028\"')")
            != "probably"
        ):
            raise SystemExit("this Chromium can't play H.264; install Playwright's Chrome for Testing build")
        probe.close()
        for server in servers:
            try:
                result = capture(browser, server, args.at, args.dump)
            except CaptureError as exc:
                failures += 1
                print(f"FAIL {exc}")
                continue
            record["captures"] = [c for c in record["captures"] if c["server"] != server] + [result]
            print(f"ok   {server}: {CAPTURES / result['file']}  proof: {result['proof_requests'][0]}")
        browser.close()
    record.update(captured_at=datetime.now(UTC).isoformat(timespec="seconds"), film=FILM, scrub_fraction=args.at)
    record_path.parent.mkdir(exist_ok=True)
    record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
