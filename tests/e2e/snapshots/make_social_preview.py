#!/usr/bin/env python3
"""Render docs/images/social-preview.jpg, the card shown when someone shares a docs link.

    /home/data/.venv/bin/python tests/e2e/snapshots/make_social_preview.py

The layout is tests/e2e/snapshots/assets/social_preview.html (edit that, not this). The player strip
is the owner-approved 3-up from the lab, injected at render time. Rendered at exactly 1280x640 CSS px
with device scale 1: that is the pixel size unfurlers want, and they crop anything taller than 2:1.
Saved as a baseline JPEG: some unfurlers read only the first bytes to size an image, and WebP support
among them is still patchy.
"""

from __future__ import annotations

import base64
import io
import sys
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parents[3]
TEMPLATE = Path(__file__).resolve().parent / "assets" / "social_preview.html"
PLAYER_STRIPS = [
    REPO_ROOT / "docs" / "images" / "players-3up.webp",
    Path("/home/data/mlab-openfilms/captures/players-3up.webp"),
]
OUT = REPO_ROOT / "docs" / "images" / "social-preview.jpg"
MAX_BYTES = 300 * 1024


def main() -> int:
    strip = next((path for path in PLAYER_STRIPS if path.is_file()), None)
    if strip is None:
        print("no players-3up.webp in docs/images/ or the lab captures folder (plan Task 5)", file=sys.stderr)
        return 1
    uri = "data:image/webp;base64," + base64.b64encode(strip.read_bytes()).decode()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 640}, device_scale_factor=1)
        page.goto(TEMPLATE.as_uri())
        page.eval_on_selector("#players", "(img, src) => { img.src = src; }", uri)
        page.wait_for_function(
            "() => { const i = document.getElementById('players'); return i.complete && i.naturalWidth > 0; }"
        )
        shot = page.screenshot()
        browser.close()
    image = Image.open(io.BytesIO(shot)).convert("RGB")
    for quality in (90, 86, 82, 78):
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=quality, optimize=True, progressive=False)
        if buffer.tell() <= MAX_BYTES:
            break
    OUT.write_bytes(buffer.getvalue())
    print(
        f"wrote {OUT} ({image.width}x{image.height}, quality {quality}, {OUT.stat().st_size // 1024} KB, strip from {strip})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
