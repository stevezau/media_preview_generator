"""Shoot every surface of the phase-4 mockup pack, light and dark, into ``shots/``.

Run with the repo's Playwright:  /home/data/.venv/bin/python shot.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

HERE = Path(__file__).resolve().parent
PAGE = (HERE / "index.html").as_uri()
OUT = HERE / "shots"

# Section id -> file stem. The phone surface is shot at phone width instead.
SURFACES = {
    "shot-s1": "01-buttons",
    "shot-s2": "02-editor-opening",
    "shot-s3": "03-editor-ending",
    "shot-s5": "05-saving",
    "shot-s6": "06-server-results",
    "shot-s7": "07-locked",
    "shot-s8": "08-unlock",
    "shot-s9": "09-season-edit",
    "shot-s10": "10-settings-tooltip",
    "shot-s11": "11-health-plex",
    "shot-s11b": "11b-health-markers-off",
    "shot-s12": "12-health-jellyfin-emby",
    "shot-s13": "13-plex-helper",
}


async def shoot(page, theme: str) -> None:
    await page.evaluate("t => document.documentElement.setAttribute('data-bs-theme', t)", theme)
    await page.wait_for_timeout(250)
    suffix = "" if theme == "dark" else "-light"
    for sel, stem in SURFACES.items():
        await page.locator(f"#{sel}").screenshot(path=str(OUT / f"{stem}{suffix}.png"))


async def main() -> None:
    OUT.mkdir(exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch()

        desktop = await browser.new_page(viewport={"width": 1440, "height": 1000})
        await desktop.goto(PAGE)
        await desktop.wait_for_timeout(600)
        await shoot(desktop, "dark")
        await shoot(desktop, "light")
        await desktop.evaluate("document.documentElement.setAttribute('data-bs-theme', 'dark')")
        await desktop.wait_for_timeout(250)
        await desktop.screenshot(path=str(OUT / "00-whole-pack.png"), full_page=True)

        phone = await browser.new_page(viewport={"width": 390, "height": 900})
        await phone.goto(PAGE)
        await phone.wait_for_timeout(600)
        await phone.locator("#shot-s4").screenshot(path=str(OUT / "04-editor-phone.png"))

        await browser.close()


asyncio.run(main())
