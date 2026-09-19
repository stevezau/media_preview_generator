#!/usr/bin/env python3
"""Plex Web, served by the lab Plex, in headless Chromium: play one episode and record its Skip Intro and Skip Credits
buttons (phase 2 row 20).

    ./plexweb_client.py <rating_key> <screenshot_prefix> [intro_until_s] [credits_seek_s] < token

The lab Plex's token comes on stdin (never argv, which `ps` shows). Plex Web keeps its sign-in in localStorage
(`myPlexAccessToken`), so the token is put there before the page loads: no plex.tv password is needed. The token's
account is a Plex Home, so Plex Web first asks "Select User"; the client picks the Home's admin (plex.tv
`/api/v2/home/users`). A fixed `clientID` in localStorage makes every run the same Plex Web device on plex.tv.

The same account owns other Plex servers, the production server included, and Plex Web tries every server it can
see. Every request the browser makes therefore goes through an allowlist, enforced in `route` (and `route_socket` for
WebSockets): the lab Plex (127.0.0.1:32402, and its own plex.direct names from plex.tv, resolved to its container
address) and plex.tv. Anything else is aborted and listed in the output's `blocked`. The Python side calls only the lab
Plex and plex.tv as well.

Prints one JSON line: when each skip button showed, where each click took the player (the player's own seek events),
the screenshots, the blocked hosts, and whether a token appeared in the page's URL or the lab token in its HTML (every
token the browser held is masked in the output). Screenshots go to
MLAB_SHOTS (phase1_matrix.SHOTS).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import Page, Route, WebSocketRoute, async_playwright

LAB_PLEX = "http://127.0.0.1:32402"
LAB_CONTAINER = "mlab-plex"
PLEX_TV = "https://plex.tv"
CLIENT_ID = "mlab-plexweb-row20"
SHOTS = Path(os.environ.get("MLAB_SHOTS", "/tmp/mlab-phase1-shots"))

# Visible "Skip Intro" / "Skip Credits" buttons, and the player's video element.
SKIP_BUTTONS_JS = """() => Array.from(document.querySelectorAll('button'))
    .filter(b => b.offsetParent !== null && /^Skip (Intro|Credits)$/.test(b.innerText.trim()))
    .map(b => b.innerText.trim())"""
VIDEO_JS = """() => { const v = document.querySelector('video');
    return v ? {t: v.currentTime, duration: v.duration, part: (v.currentSrc.split('?')[0].split('/parts/')[1] || '')} : null }"""
# In-page log, every 25 ms: when a skip button shows and hides, and every seek the player makes (its target time).
OBSERVER_JS = """() => {
    window.__mlab = {buttons: [], seeks: []};
    let shown = new Set();
    const part = v => (v.currentSrc.split('?')[0].split('/parts/')[1] || '');
    setInterval(() => {
        const v = document.querySelector('video');
        if (!v) return;
        if (!v.__mlabSeeks) {
            v.__mlabSeeks = true;
            v.addEventListener('seeking', () => window.__mlab.seeks.push({to: v.currentTime, part: part(v)}));
        }
        const now = new Set(Array.from(document.querySelectorAll('button'))
            .filter(b => b.offsetParent !== null && /^Skip (Intro|Credits)$/.test(b.innerText.trim()))
            .map(b => b.innerText.trim()));
        for (const label of now) if (!shown.has(label)) window.__mlab.buttons.push({label, shown_at: v.currentTime, part: part(v)});
        for (const label of shown) if (!now.has(label)) window.__mlab.buttons.push({label, hidden_at: v.currentTime, part: part(v)});
        shown = now;
    }, 25);
}"""
DURATION_LABEL_JS = "() => (document.querySelector('[data-testid=mediaDuration]') || {}).innerText || ''"


def plex_tv(path: str, token: str) -> Any:
    request = urllib.request.Request(
        f"{PLEX_TV}{path}",
        headers={"X-Plex-Token": token, "Accept": "application/json", "X-Plex-Client-Identifier": CLIENT_ID,
                 "X-Plex-Product": "mlab row 20"},
    )  # fmt: skip
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def lab_identity(token: str) -> dict[str, Any]:
    """The lab Plex's machine id, container address, its plex.direct names, Plex Web's version, the Home admin."""
    request = urllib.request.Request(f"{LAB_PLEX}/identity", headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        machine_id = json.load(response)["MediaContainer"]["machineIdentifier"]
    address = subprocess.run(
        ["docker", "inspect", LAB_CONTAINER, "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}"],
        capture_output=True, text=True, check=True, timeout=30,
    ).stdout.split()[0]  # fmt: skip
    resource = next(
        r for r in plex_tv("/api/v2/resources?includeHttps=1", token) if r["clientIdentifier"] == machine_id
    )
    # Only the connections at the lab container's own address: a name plex.tv lists for any other address is refused.
    direct = sorted({urlparse(c["uri"]).hostname for c in resource["connections"] if c["address"] == address})
    admin = next(u["title"] for u in plex_tv("/api/v2/home/users", token)["users"] if u.get("admin"))
    with urllib.request.urlopen(f"{LAB_PLEX}/web/index.html", timeout=10) as response:
        page = response.read().decode()
    web_version = page.split("-plex-", 1)[1].split("-", 1)[0] if "-plex-" in page else ""
    return {"machine_id": machine_id, "address": address, "direct": direct, "admin": admin, "web": web_version}


class Allowlist:
    """Aborts every browser request that isn't the lab Plex or plex.tv; remembers what it aborted."""

    def __init__(self, direct_hosts: list[str]) -> None:
        self.direct_hosts = set(direct_hosts)
        self.blocked: set[str] = set()
        self.allowed_count = 0

    def allows(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme in ("data", "blob", "about"):
            return True
        host = parsed.hostname or ""
        if host == "127.0.0.1" and parsed.port == 32402:
            return True
        if host in self.direct_hosts:
            return True
        return host == "plex.tv" or host.endswith(".plex.tv")

    def _block(self, url: str) -> None:
        parsed = urlparse(url)
        self.blocked.add(f"{parsed.scheme}://{parsed.hostname}:{parsed.port or ''}")

    async def route(self, route: Route) -> None:
        url = route.request.url
        if self.allows(url):
            self.allowed_count += 1
            await route.continue_()
        else:
            self._block(url)
            await route.abort()

    async def route_socket(self, socket: WebSocketRoute) -> None:
        """WebSockets bypass ``route``: an allowed one is connected through, any other closed unconnected."""
        if self.allows(socket.url):
            self.allowed_count += 1
            socket.connect_to_server()
        else:
            self._block(socket.url)
            await socket.close()


class Run:
    def __init__(self, page: Page, prefix: str) -> None:
        self.page = page
        self.prefix = prefix
        self.events: list[dict] = []
        self.shots: list[str] = []

    def event(self, **fields: Any) -> None:
        self.events.append(fields)
        print(json.dumps(fields), file=sys.stderr, flush=True)

    async def shot(self, name: str) -> None:
        # A mouse move shows the player's controls (and the skip button with them) for the screenshot.
        await self.page.mouse.move(640, 300)
        await self.page.wait_for_timeout(120)
        await self.page.mouse.move(660, 320)
        await self.page.wait_for_timeout(400)
        path = SHOTS / f"{self.prefix}-{name}.png"
        await self.page.screenshot(path=str(path))
        self.shots.append(str(path))

    async def wait_for_button(self, label: str, until_s: float, max_wait_s: float) -> dict | None:
        """Poll until ``label`` shows or the video passes ``until_s``; the video state when it showed, else None."""
        deadline = time.monotonic() + max_wait_s
        while time.monotonic() < deadline:
            video = await self.page.evaluate(VIDEO_JS)
            if label in await self.page.evaluate(SKIP_BUTTONS_JS):
                return video
            if video and video["t"] >= until_s:
                return None
            await self.page.wait_for_timeout(100)
        return None

    async def click_and_follow(self, label: str) -> dict:
        """Click a skip button: the video state just before, the first state that moved on (time or part), and the
        seeks the player made from the click on (``seeks``: their target times)."""
        seeks_before = await self.page.evaluate("() => window.__mlab.seeks.length")
        before = await self.page.evaluate(VIDEO_JS)
        await self.page.locator("button").filter(has_text=label).first.click()
        after = None
        for _ in range(60):
            video = await self.page.evaluate(VIDEO_JS)
            if video and (video["part"] != before["part"] or video["t"] > before["t"] + 5):
                after = video
                break
            await self.page.wait_for_timeout(50)
        await self.page.wait_for_timeout(300)
        seeks = await self.page.evaluate(f"() => window.__mlab.seeks.slice({seeks_before})")
        duration_label = await self.page.evaluate(DURATION_LABEL_JS)
        return {"before": before, "after": after, "seeks": seeks, "duration_label": duration_label}


async def play(
    rating_key: str, prefix: str, intro_until: float, credits_seek: float, token: str
) -> tuple[dict, set[str]]:
    """Play the item and watch its skip buttons.

    Returns:
        The observations, and every token the browser held (the lab token, and the one plex.tv hands back for the user
        picked on Select User), which the caller masks in anything it prints.
    """
    tokens = {token}
    lab = lab_identity(token)
    allowlist = Allowlist(lab["direct"])
    resolver = ",".join(f"MAP {host} {lab['address']}" for host in lab["direct"])
    args = ["--autoplay-policy=no-user-gesture-required"] + ([f"--host-resolver-rules={resolver}"] if resolver else [])
    SHOTS.mkdir(parents=True, exist_ok=True)
    out: dict[str, Any] = {"plex_web": lab["web"], "rating_key": rating_key, "lab_direct_names": len(lab["direct"])}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(args=args)
        try:
            # A service worker's requests would bypass ``route``, so none may register.
            context = await browser.new_context(viewport={"width": 1280, "height": 720}, service_workers="block")
            await context.route("**/*", allowlist.route)
            await context.route_web_socket(lambda _url: True, allowlist.route_socket)
            sign_in = (
                f"try {{ if (location.origin === {json.dumps(LAB_PLEX)}) {{"
                f" localStorage.setItem('myPlexAccessToken', {json.dumps(token)});"
                f" localStorage.setItem('clientID', {json.dumps(CLIENT_ID)}); }} }} catch (e) {{}}"
            )
            await context.add_init_script(sign_in)
            page = await context.new_page()
            run = Run(page, prefix)
            await page.goto(f"{LAB_PLEX}/web/index.html#!/")
            await page.wait_for_timeout(6000)
            if "Select User" in await page.evaluate("() => document.body.innerText"):
                await page.get_by_text(lab["admin"], exact=True).click()
                await page.wait_for_timeout(6000)
                run.event(step="picked the Home admin on Select User")
            switched = await page.evaluate("() => localStorage.getItem('myPlexAccessToken')")
            if switched:
                tokens.add(switched)
            details = f"{LAB_PLEX}/web/index.html#!/server/{lab['machine_id']}/details?key=%2Flibrary%2Fmetadata%2F{rating_key}"
            await page.goto(details)
            await page.wait_for_timeout(8000)
            await page.evaluate(OBSERVER_JS)
            await page.locator("button[data-testid=preplay-play]").first.click(timeout=15000)
            await page.wait_for_timeout(2000)
            run.event(step="playing", video=await page.evaluate(VIDEO_JS))

            shown = await run.wait_for_button("Skip Intro", intro_until, max_wait_s=90)
            out["skip_intro_shown"] = shown
            if shown:
                await page.wait_for_timeout(1000)
                await run.shot("skip-intro-shown")
                out["skip_intro_click"] = await run.click_and_follow("Skip Intro")
                await run.shot("after-skip-intro")
            else:
                await run.shot("no-skip-intro")

            await page.evaluate(f"() => {{ document.querySelector('video').currentTime = {credits_seek}; }}")
            await page.wait_for_timeout(500)
            run.event(step="seeked for credits", video=await page.evaluate(VIDEO_JS))
            shown = await run.wait_for_button("Skip Credits", credits_seek + 20, max_wait_s=60)
            out["skip_credits_shown"] = shown
            if shown:
                await page.wait_for_timeout(1000)
                await run.shot("skip-credits-shown")
                out["skip_credits_click"] = await run.click_and_follow("Skip Credits")
                await page.wait_for_timeout(1500)
                out["after_skip_credits"] = await page.evaluate(VIDEO_JS)
                await run.shot("after-skip-credits")
            else:
                await run.shot("no-skip-credits")

            out["in_page"] = await page.evaluate("() => window.__mlab")
            html = await page.content()
            # Plex Web itself writes the picked user's token into its image URLs; the lab token must appear nowhere.
            out["token_in_url"] = any(t in page.url for t in tokens)
            out["lab_token_in_html"] = token in html
            out["user_token_in_html"] = any(t in html for t in tokens - {token})
            try:
                # Closing the player ends the playback session on the lab Plex.
                await page.locator("button[data-testid=closeButton]").first.click(timeout=5000)
                await page.wait_for_timeout(2000)
                out["player_closed"] = True
            except Exception as exc:  # the row checks the sessions itself; a missing button is recorded, not fatal
                out["player_closed"] = f"{type(exc).__name__}"
            out["events"] = run.events
            out["screenshots"] = run.shots
        finally:
            await browser.close()
    out["blocked"] = sorted(allowlist.blocked)
    out["allowed_requests"] = allowlist.allowed_count
    out["tokens_held"] = len(tokens)
    return out, tokens


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    token = sys.stdin.read().strip()
    intro_until = float(argv[2]) if len(argv) > 2 else 30.0
    credits_seek = float(argv[3]) if len(argv) > 3 else 96.0
    result, tokens = asyncio.run(play(argv[0], argv[1], intro_until, credits_seek, token))
    text = json.dumps(result)
    for held in tokens:
        text = text.replace(held, "****")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
