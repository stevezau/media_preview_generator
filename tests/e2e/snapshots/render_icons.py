#!/usr/bin/env python3
"""Render the logo and the favicon to the PNG sizes the site, the app and Unraid use.

    /home/data/.venv/bin/python tests/e2e/snapshots/render_icons.py

Chromium draws the SVGs (the engine that shows them in a browser tab), so the PNGs match what people
see. Also writes docs/design/logo/favicon-check.png: both marks at 16, 32 and 64 px on dark and light,
at 1x, for judging the favicon at the sizes it is actually shown.
"""

from __future__ import annotations

import base64
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

REPO_ROOT = Path(__file__).resolve().parents[3]
LOGO = REPO_ROOT / "docs/assets/img/logo.svg"
FAVICON = REPO_ROOT / "docs/assets/img/favicon.svg"
APP_IMAGES = REPO_ROOT / "media_preview_generator/web/static/images"
RENDERS = [
    (FAVICON, 32, REPO_ROOT / "docs/assets/img/favicon-32.png"),
    (LOGO, 180, REPO_ROOT / "docs/assets/img/apple-touch-icon.png"),
    (LOGO, 512, REPO_ROOT / "docs/images/icon.png"),
    (LOGO, 512, APP_IMAGES / "icon.png"),
    (FAVICON, 32, APP_IMAGES / "favicon-32.png"),
]
CHECK_SHEET = REPO_ROOT / "docs/design/logo/favicon-check.png"
CELL = 96


def _data_uri(svg: Path) -> str:
    return "data:image/svg+xml;base64," + base64.b64encode(svg.read_bytes()).decode()


def _render(page: Page, svg: Path, size: int, out: Path) -> None:
    page.set_viewport_size({"width": size, "height": size})
    page.set_content(
        f'<body style="margin:0"><img src="{_data_uri(svg)}" width="{size}" height="{size}" style="display:block"></body>'
    )
    page.wait_for_function("() => document.images[0].complete && document.images[0].naturalWidth > 0")
    page.screenshot(path=str(out), omit_background=True, clip={"x": 0, "y": 0, "width": size, "height": size})
    print(f"wrote {out.relative_to(REPO_ROOT)} ({size}x{size})")


def _check_sheet(page: Page) -> None:
    cells = "".join(
        f'<div class="cell" style="background:{background}"><img src="{_data_uri(svg)}" width="{px}" height="{px}"></div>'
        for background in ("#0f0f1a", "#ffffff")
        for svg in (LOGO, FAVICON)
        for px in (16, 32, 64)
    )
    page.set_viewport_size({"width": 6 * CELL, "height": 2 * CELL})
    page.set_content(
        f"<style>body{{margin:0;display:grid;grid-template-columns:repeat(6,{CELL}px)}}"
        f".cell{{width:{CELL}px;height:{CELL}px;display:grid;place-items:center}}</style>{cells}"
    )
    page.wait_for_function("() => [...document.images].every((i) => i.complete && i.naturalWidth > 0)")
    page.screenshot(path=str(CHECK_SHEET))
    print(f"wrote {CHECK_SHEET.relative_to(REPO_ROOT)}")


def main() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(device_scale_factor=1)
        for svg, size, out in RENDERS:
            _render(page, svg, size, out)
        _check_sheet(page)
        browser.close()


if __name__ == "__main__":
    main()
