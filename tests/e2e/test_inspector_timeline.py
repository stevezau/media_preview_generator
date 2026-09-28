"""E2E: the Inspector's Timeline — one strip of every preview frame, the rows under it on the same scale, the overview
bar, the jump chips, the big frame, and Adjust inline under the strip.

The API is mocked by ``_inspector_fixtures.install``; preview frames are small JPEGs drawn per request, and the fixture
records every frame index the page asks for, so the windowing is checked on a three-hour film.
``INSPECTOR_SCREENSHOT_DIR`` saves a screenshot of each screen for checking against the design.
"""

from __future__ import annotations

import re
from urllib.parse import quote

import pytest
from playwright.sync_api import Page, expect

from . import _inspector_fixtures as fx


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


def _api_with(*boards: tuple[dict, dict]) -> fx.InspectorApi:
    api = fx.InspectorApi()
    for file, item in boards:
        api.add(file, item, fx.default_kinds(file["canonical_path"]))
    return api


def _open(page: Page, app_url: str, path: str) -> None:
    page.goto(f"{app_url}/inspector?path={quote(path)}")
    expect(page.locator("#inspLoading")).to_have_count(0, timeout=10_000)
    expect(page.locator("#inspNow")).not_to_have_text("", timeout=5_000)


def _seconds(text: str) -> int:
    total = 0
    for part in text.split(":"):
        total = total * 60 + int(part)
    return total


def _tile(page: Page, key: str):
    return page.locator(f"#inspTiles [data-tile='{key}']")


def _lane(page: Page, key: str):
    return page.locator(f"#inspStrip .insp-lane[data-lane='{key}']")


@pytest.mark.e2e
class TestCheckedFilm:
    def test_tiles_strip_rows_and_readout(self, authed_page: Page, app_url: str) -> None:
        errors: list[str] = []
        authed_page.on("pageerror", lambda e: errors.append(str(e)))
        fx.install(authed_page, _api_with(fx.checked_film()))
        _open(authed_page, app_url, fx.FILM)

        expect(authed_page.locator("#inspTitle")).to_have_text("The Matrix")
        expect(authed_page.locator("#inspTitleSub")).to_have_text("1999")
        expect(authed_page.locator("#inspChips .insp-chip")).to_have_text(["Film", "2:16:18", "2160p Dolby Vision"])
        expect(_tile(authed_page, "servers").locator(".insp-stat-title")).to_have_text("2 of 3 show ours")
        expect(_tile(authed_page, "servers").locator(".insp-stat-sub")).to_have_text(
            "Plex keeps its own markers. Jellyfin and Emby have it."
        )
        expect(_tile(authed_page, "found").locator(".insp-stat-title")).to_have_text("Credits 2:09:25 → end")
        expect(_tile(authed_page, "found").locator(".insp-stat-sub")).to_have_text("From SkipDB")
        expect(_tile(authed_page, "preview").locator(".insp-stat-title")).to_have_text("4,089 frames · every 2 s")
        expect(_tile(authed_page, "preview").locator(".insp-stat-sub")).to_contain_text("16.6 MB · covers 2:16:18")
        expect(_tile(authed_page, "checked").locator(".insp-stat-sub")).to_have_text("No intro · credits found")

        timeline = authed_page.locator("#inspTimeline")
        expect(timeline.locator(".insp-tl-range")).to_have_text("0:00 – 2:16:18")
        expect(timeline.locator(".insp-tl-title .info-icon")).to_have_attribute(
            "aria-label", re.compile(r"^Every preview frame in order, one every 2 s\.")
        )
        # How to move around the strip is the ⓘ's detail, so the hover says "Click for more.".
        expect(timeline.locator(".insp-tl-title .info-icon")).to_have_class(re.compile(r"\binfo-icon-more\b"))
        expect(authed_page.locator("#inspJumps button")).to_have_text(
            ["No intro", "Credits2:09:25", "Plex2:08:00", "Plex2:09:28"]
        )
        expect(authed_page.locator("#inspJumps button", has_text="No intro")).to_be_disabled()
        expect(authed_page.locator(".insp-lane-names .insp-lane-name")).to_have_text(
            ["We found", "Plex", "Jellyfin", "Emby"]
        )
        # "We found" needs no word of whose it is; each server's band says whose marker it shows.
        found = _lane(authed_page, "found").locator(".insp-band")
        expect(found).to_have_text(["Credits 2:09:25 → end"])
        expect(found).to_have_class(re.compile(r"\bis-ours\b"))
        assert found.get_attribute("aria-label") == "We found: Credits 2:09:25 → end"
        plex = _lane(authed_page, "plex-1").locator(".insp-band")
        expect(plex).to_have_text(["Plex's own · Credits 2:08:00 – 2:08:30", "Plex's own · Credits 2:09:28 → end"])
        expect(plex.first).to_have_class(re.compile(r"\bis-own\b"))
        assert plex.first.get_attribute("aria-label") == "Plex's own · Credits 2:08:00 – 2:08:30"
        assert plex.first.get_attribute("title") == "Plex's own · Credits 2:08:00 – 2:08:30"
        jellyfin = _lane(authed_page, "jf-1").locator(".insp-band")
        expect(jellyfin).to_have_text(["Ours · Credits 2:09:25 → end"])
        expect(jellyfin).to_have_class(re.compile(r"\bis-tint\b"))
        assert jellyfin.get_attribute("aria-label") == "Jellyfin: Ours · Credits 2:09:25 → end"
        assert jellyfin.get_attribute("title") == "Jellyfin: Ours · Credits 2:09:25 → end"
        # The legend stays.
        expect(authed_page.locator("#inspTimeline .insp-legend-item")).to_have_text(["ours", "a server's own"])
        # Opened on our credits: the strip's centre is the credits' start, with its flag and Plex's beside it.
        expect(authed_page.locator("#inspNow")).to_have_text("2:09:26")
        expect(authed_page.locator("#inspFrameText")).to_have_text("preview frame 3,884 of 4,089 · one every 2 s")
        expect(authed_page.locator("#inspNowTag")).to_have_text("Credits · Plex starts at 2:09:28")
        flags = authed_page.locator("#inspStrip .insp-edge-flag:visible")
        expect(flags).to_have_text(
            [
                "Plex credits · 2:08:00",
                "Plex credits end · 2:08:30",
                "Credits start · 2:09:25",
                "Plex credits · 2:09:28",
            ]
        )
        expect(authed_page.locator("#inspStrip .insp-tl-frame.is-now .insp-tl-time")).to_have_text("2:09:26")
        # Each tile is a real preview frame, from the chosen preview, at its own index.
        now_img = authed_page.locator("#inspStrip .insp-tl-frame.is-now img")
        expect(now_img).to_have_attribute("src", re.compile(r"/api/bif/frame\?path=.*&index=3883$"))
        fx.screenshot(authed_page, "01-checked-film")
        assert errors == []

    def test_the_copy_button_puts_the_file_path_on_the_clipboard(self, authed_page: Page, app_url: str) -> None:
        authed_page.context.grant_permissions(["clipboard-read", "clipboard-write"])
        fx.install(authed_page, _api_with(fx.checked_film()))
        _open(authed_page, app_url, fx.FILM)
        authed_page.get_by_role("button", name="Copy file path").click()
        expect(authed_page.locator("#toastBody")).to_have_text("Copied to the clipboard.")
        assert authed_page.evaluate("navigator.clipboard.readText()") == fx.FILM

    def test_jump_chips_and_bands_move_the_strip(self, authed_page: Page, app_url: str) -> None:
        fx.install(authed_page, _api_with(fx.checked_film()))
        _open(authed_page, app_url, fx.FILM)

        chip = authed_page.locator("#inspJumps button", has_text="Plex2:08:00")
        chip.click()
        expect(authed_page.locator("#inspNow")).to_have_text("2:08:00")
        expect(authed_page.locator("#inspNowTag")).to_have_text("Plex's own credits here")
        expect(chip).to_have_class(re.compile(r"\bis-active\b"))

        authed_page.locator("#inspJumps button", has_text="Credits2:09:25").click()
        expect(authed_page.locator("#inspNow")).to_have_text("2:09:26")
        # A band jumps to its edge too.
        _lane(authed_page, "plex-1").locator(".insp-band", has_text="Credits 2:09:28 → end").click()
        expect(authed_page.locator("#inspNow")).to_have_text("2:09:28")
        expect(authed_page.locator("#inspJumps button", has_text="Plex2:09:28")).to_have_class(
            re.compile(r"\bis-active\b")
        )

    def test_step_buttons_move_ten_frames(self, authed_page: Page, app_url: str) -> None:
        fx.install(authed_page, _api_with(fx.checked_film()))
        _open(authed_page, app_url, fx.FILM)
        authed_page.get_by_role("button", name="Forward 10 frames").click()
        expect(authed_page.locator("#inspNow")).to_have_text("2:09:46")
        authed_page.get_by_role("button", name="Back 10 frames").click()
        authed_page.get_by_role("button", name="Back 10 frames").click()
        expect(authed_page.locator("#inspNow")).to_have_text("2:09:06")
        expect(authed_page.locator("#inspNowTag")).to_have_text("Story")

    def test_the_overview_bar_jumps_where_it_is_clicked(self, authed_page: Page, app_url: str) -> None:
        fx.install(authed_page, _api_with(fx.checked_film()))
        _open(authed_page, app_url, fx.FILM)
        bar = authed_page.locator("#inspOverview")
        bar.scroll_into_view_if_needed()
        box = bar.bounding_box()
        assert box
        authed_page.mouse.click(box["x"] + box["width"] * 0.25, box["y"] + box["height"] / 2)
        # A quarter of 2:16:18, to within a pixel of the bar (about 7 s) and one preview frame.
        expect(authed_page.locator("#inspNow")).not_to_have_text("2:09:26")
        authed_page.wait_for_timeout(700)
        assert abs(_seconds(authed_page.locator("#inspNow").inner_text()) - fx.FILM_MS / 4000) <= 10
        expect(authed_page.locator("#inspOvNow")).to_have_text(authed_page.locator("#inspNow").inner_text())
        expect(authed_page.locator("#inspNowTag")).to_have_text("Story")

    @staticmethod
    def _record_bar_clicks(page: Page) -> None:
        # Whether each click that reaches the page from the bar was taken as a jump (not swallowed after a drag).
        page.evaluate(
            """() => {
                window.__barClicks = [];
                document.addEventListener('click', (e) => {
                    if (e.target.closest('#inspOverview')) window.__barClicks.push(!e.defaultPrevented);
                });
            }"""
        )

    def test_dragging_along_the_overview_bar_scrubs_the_strip_and_is_not_a_click(
        self, authed_page: Page, app_url: str
    ) -> None:
        fx.install(authed_page, _api_with(fx.checked_film()))
        _open(authed_page, app_url, fx.FILM)
        self._record_bar_clicks(authed_page)
        bar = authed_page.locator("#inspOverview")
        bar.scroll_into_view_if_needed()
        box = bar.bounding_box()
        assert box
        y = box["y"] + box["height"] / 2
        now = authed_page.locator("#inspNow")

        authed_page.mouse.move(box["x"] + box["width"] * 0.25, y)
        authed_page.mouse.down()
        authed_page.mouse.move(box["x"] + box["width"] * 0.4, y, steps=6)
        # The strip follows the pointer before the button is let go: two fifths of 2:16:18, within a pixel and a frame.
        expect(now).not_to_have_text("2:09:26")
        authed_page.wait_for_timeout(200)
        assert abs(_seconds(now.inner_text()) - fx.FILM_MS * 0.4 / 1000) <= 10
        expect(bar).to_have_class(re.compile(r"\bis-scrubbing\b"))
        authed_page.mouse.move(box["x"] + box["width"] * 0.5, y, steps=6)
        authed_page.mouse.up()

        authed_page.wait_for_timeout(200)
        assert abs(_seconds(now.inner_text()) - fx.FILM_MS * 0.5 / 1000) <= 10
        expect(bar).not_to_have_class(re.compile(r"\bis-scrubbing\b"))
        assert authed_page.evaluate("window.__barClicks") == [False]
        expect(authed_page.locator("#inspFrameDialog")).not_to_be_visible()

        # A plain click afterwards still jumps.
        authed_page.mouse.click(box["x"] + box["width"] * 0.25, y)
        authed_page.wait_for_timeout(700)
        assert abs(_seconds(now.inner_text()) - fx.FILM_MS * 0.25 / 1000) <= 10
        assert authed_page.evaluate("window.__barClicks") == [False, True]

    def test_a_touch_drag_along_the_overview_bar_scrubs_too(self, authed_page: Page, app_url: str) -> None:
        fx.install(authed_page, _api_with(fx.checked_film()))
        _open(authed_page, app_url, fx.FILM)
        bar = authed_page.locator("#inspOverview")
        bar.scroll_into_view_if_needed()
        assert bar.evaluate("el => getComputedStyle(el).touchAction") == "pan-y"
        box = bar.bounding_box()
        assert box
        # A finger's pointer events (Playwright's touchscreen only taps).
        bar.evaluate(
            """(el, [x0, x1, y]) => {
                const at = (type, x) => new PointerEvent(type, {
                    pointerId: 7, pointerType: 'touch', isPrimary: true, clientX: x, clientY: y, bubbles: true,
                });
                el.dispatchEvent(at('pointerdown', x0));
                for (let i = 1; i <= 6; i++) el.dispatchEvent(at('pointermove', x0 + ((x1 - x0) * i) / 6));
                el.dispatchEvent(at('pointerup', x1));
            }""",
            [box["x"] + box["width"] * 0.1, box["x"] + box["width"] * 0.75, box["y"] + box["height"] / 2],
        )

        authed_page.wait_for_timeout(200)
        assert abs(_seconds(authed_page.locator("#inspNow").inner_text()) - fx.FILM_MS * 0.75 / 1000) <= 10
        expect(bar).not_to_have_class(re.compile(r"\bis-scrubbing\b"))

    def test_the_timeline_info_says_the_bar_can_be_dragged(self, authed_page: Page, app_url: str) -> None:
        fx.install(authed_page, _api_with(fx.checked_film()))
        _open(authed_page, app_url, fx.FILM)
        icon = authed_page.locator("#inspTimeline .insp-tl-title .info-icon")
        expect(icon).to_have_attribute(
            "aria-label",
            "Every preview frame in order, one every 2 s. Each row below shows what that server gives viewers.",
        )
        # How to move around the strip is the ⓘ's detail (the app-wide ⓘ rule keeps the hover to what it is).
        icon.click()
        expect(authed_page.locator("#globalInfoBody")).to_have_text(
            "Scroll or drag the strip, or click or drag along the bar above it to jump. Click a frame to see it large.",
            timeout=5000,
        )

    def test_dragging_moves_the_strip_and_is_not_a_click(self, authed_page: Page, app_url: str) -> None:
        fx.install(authed_page, _api_with(fx.checked_film()))
        _open(authed_page, app_url, fx.FILM)
        tile = authed_page.locator("#inspStrip .insp-tl-frame.is-now .insp-tl-img")
        tile.scroll_into_view_if_needed()
        box = tile.bounding_box()
        assert box
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        authed_page.mouse.move(x, y)
        authed_page.mouse.down()
        authed_page.mouse.move(x - 150, y, steps=5)
        authed_page.mouse.move(x - 304, y, steps=5)
        authed_page.mouse.up()
        # Two tiles' width to the left is two frames later, and the frame under the pointer didn't open.
        expect(authed_page.locator("#inspNow")).to_have_text("2:09:30")
        expect(authed_page.locator("#inspFrameDialog")).not_to_be_visible()

    def test_the_wheel_moves_the_strip_sideways(self, authed_page: Page, app_url: str) -> None:
        fx.install(authed_page, _api_with(fx.checked_film()))
        _open(authed_page, app_url, fx.FILM)
        strip = authed_page.locator("#inspStrip")
        strip.scroll_into_view_if_needed()
        box = strip.bounding_box()
        assert box
        authed_page.mouse.move(box["x"] + 200, box["y"] + 60)
        authed_page.mouse.wheel(0, 608)
        # 608 px is four tiles: four frames on from 2:09:26.
        expect(authed_page.locator("#inspNow")).to_have_text("2:09:34")

    def test_a_frame_opens_large_and_steps_with_the_keys(self, authed_page: Page, app_url: str) -> None:
        fx.install(authed_page, _api_with(fx.checked_film()))
        _open(authed_page, app_url, fx.FILM)
        authed_page.locator("#inspStrip .insp-tl-frame.is-now .insp-tl-img").click()

        dialog = authed_page.locator("#inspFrameDialog")
        expect(dialog).to_be_visible()
        expect(authed_page.locator("#inspBigTime")).to_have_text("2:09:26")
        expect(authed_page.locator("#inspBigText")).to_have_text("preview frame 3,884 of 4,089")
        expect(authed_page.locator("#inspBigTag")).to_have_text("Credits · Plex starts at 2:09:28")
        expect(authed_page.locator("#inspBigImg")).to_have_attribute("src", re.compile(r"&index=3883$"))
        authed_page.wait_for_function(
            "() => { const i = document.getElementById('inspBigImg'); return i.complete && i.naturalWidth > 0; }"
        )
        fx.screenshot(authed_page, "03-frame-dialog")

        authed_page.keyboard.press("ArrowRight")
        expect(authed_page.locator("#inspBigTime")).to_have_text("2:09:28")
        expect(authed_page.locator("#inspBigImg")).to_have_attribute("src", re.compile(r"&index=3884$"))
        dialog.get_by_role("button", name="Previous").click()
        dialog.get_by_role("button", name="Previous").click()
        expect(authed_page.locator("#inspBigTime")).to_have_text("2:09:24")
        expect(authed_page.locator("#inspBigTag")).to_have_text("Story")
        # The strip follows the big frame.
        expect(authed_page.locator("#inspNow")).to_have_text("2:09:24")
        authed_page.keyboard.press("Escape")
        expect(dialog).not_to_be_visible()

    def test_the_last_frame_has_no_next(self, authed_page: Page, app_url: str) -> None:
        fx.install(authed_page, _api_with(fx.checked_film()))
        _open(authed_page, app_url, fx.FILM)
        bar = authed_page.locator("#inspOverview")
        bar.scroll_into_view_if_needed()
        box = bar.bounding_box()
        assert box
        authed_page.mouse.click(box["x"] + box["width"] - 1, box["y"] + 10)
        # The bar's last pixel is a few seconds short of the end; the step button goes the rest of the way.
        authed_page.get_by_role("button", name="Forward 10 frames").click()
        expect(authed_page.locator("#inspNow")).to_have_text("2:16:16")
        authed_page.locator("#inspStrip .insp-tl-frame.is-now .insp-tl-img").click()
        expect(authed_page.locator("#inspBigText")).to_have_text("preview frame 4,089 of 4,089")
        expect(authed_page.locator("#inspBigNext")).to_be_disabled()
        expect(authed_page.locator("#inspBigPrev")).to_be_enabled()


@pytest.mark.e2e
class TestLongFilm:
    def test_a_three_hour_film_keeps_only_the_frames_near_the_view(self, authed_page: Page, app_url: str) -> None:
        api = fx.install(authed_page, _api_with(fx.long_film()))
        _open(authed_page, app_url, fx.LONG_FILM)
        expect(authed_page.locator("#inspFrameText")).to_have_text("preview frame 5,251 of 5,400 · one every 2 s")
        authed_page.wait_for_timeout(600)
        tiles = authed_page.locator("#inspStrip .insp-tl-frame")
        assert tiles.count() <= 40
        # Beside the overview's thumbnails, every image asked for is a tile near 2:55:00 (frame 5,250).
        near = [i for i in api.image_requests if abs(i - 5250) <= 40]
        assert len(near) >= 8
        assert len(api.image_requests) - len(near) <= 24

        before = len(api.image_requests)
        authed_page.locator("#inspOverview").scroll_into_view_if_needed()
        bar = authed_page.locator("#inspOverview").bounding_box()
        assert bar
        authed_page.mouse.click(bar["x"] + 0.5, bar["y"] + 10)
        expect(authed_page.locator("#inspNow")).to_have_text(re.compile(r"^0:0\d$"))
        authed_page.wait_for_timeout(600)
        assert tiles.count() <= 40
        assert len(api.image_requests) - before <= 40
        # The scroll width holds every frame, but the page holds only a window of them.
        width = authed_page.locator("#inspStrip").evaluate("s => s.scrollWidth")
        assert width >= 5400 * 152


@pytest.mark.e2e
class TestNotChecked:
    def test_the_same_page_with_nothing_of_ours_yet(self, authed_page: Page, app_url: str) -> None:
        api = fx.install(authed_page)
        _open(authed_page, app_url, fx.FILM)

        expect(authed_page.locator("#inspRedetect")).to_have_text("Check intro & credits now")
        expect(authed_page.locator("#inspAdjust")).to_have_count(0)
        expect(_tile(authed_page, "servers").locator(".insp-stat-title")).to_have_text("Plex's own only")
        expect(_tile(authed_page, "found").locator(".insp-stat-title")).to_have_text("Not checked yet")
        expect(_tile(authed_page, "checked").locator(".insp-stat-title")).to_have_text("Never")
        expect(_tile(authed_page, "checked").locator(".insp-stat-sub")).to_have_text(
            "Plex shows its own markers meanwhile"
        )
        expect(_lane(authed_page, "found")).to_have_text("Not checked yet")
        expect(_lane(authed_page, "found").locator(".insp-band")).to_have_count(0)
        expect(_lane(authed_page, "plex-1").locator(".insp-band")).to_have_text(
            ["Plex's own · Credits 2:08:00 – 2:08:30", "Plex's own · Credits 2:09:28 → end"]
        )
        # Nothing of ours to jump to, and no "No intro" before anything was checked.
        expect(authed_page.locator("#inspJumps button")).to_have_text(["Plex2:08:00", "Plex2:09:28"])
        expect(authed_page.locator("#inspNow")).to_have_text("2:08:00")
        expect(authed_page.locator("#inspNowTag")).to_have_text("Plex's own credits here")
        card = authed_page.locator("#inspNotChecked")
        expect(card.locator(".insp-card-heading")).to_have_text("Not checked by Intro & Credits yet")
        expect(card.locator(".insp-notchecked-text")).to_have_text(
            "Plex shows 2 credits markers of its own today, drawn in grey on the timeline. Checking the film decides "
            "ours. With “Keep Plex's markers” on, Plex keeps its own either way."
        )
        expect(authed_page.locator("#inspEvidence")).to_have_count(0)
        expect(authed_page.locator("#inspServersPill")).to_have_text("Not checked yet")
        fx.screenshot(authed_page, "05-not-checked")

        with authed_page.expect_response(lambda r: r.url.endswith("/api/markers/item/redetect")):
            card.get_by_role("button", name="Check intro & credits now").click()
        assert api.redetects == [{"path": fx.FILM}]


@pytest.mark.e2e
class TestPreviewGaps:
    def test_no_preview_shows_no_preview_tiles(self, authed_page: Page, app_url: str) -> None:
        file, item = fx.checked_film()
        file["preview"] = None
        for row in file["previews"]:
            row.update(exists=False, frame_count=None)
        api = fx.install(authed_page, _api_with((file, item)))
        _open(authed_page, app_url, fx.FILM)

        expect(_tile(authed_page, "preview").locator(".insp-stat-title")).to_have_text("No preview yet")
        expect(_tile(authed_page, "preview").locator(".insp-stat-sub")).to_have_text("Regenerate preview to make one")
        expect(authed_page.locator("#inspFrameText")).to_have_text("no preview frame here")
        tiles = authed_page.locator("#inspStrip .insp-tl-frame")
        expect(tiles.first.locator(".insp-tl-none")).to_have_text("No preview")
        expect(authed_page.locator("#inspStrip .insp-tl-img")).to_have_count(0)
        expect(authed_page.locator("#inspOverview img")).to_have_count(0)
        # The rows still say where everything is, a place every 10 s.
        expect(authed_page.locator("#inspNow")).to_have_text("2:09:30")
        expect(_lane(authed_page, "found").locator(".insp-band")).to_have_text(["Credits 2:09:25 → end"])
        expect(authed_page.locator("#inspServers .insp-server[data-server-id='plex-1']")).to_contain_text(
            "No preview yet"
        )
        fx.screenshot(authed_page, "06-no-preview")
        assert api.image_requests == []

    def test_a_preview_shorter_than_the_film_says_where_it_stops(self, authed_page: Page, app_url: str) -> None:
        file, item = fx.checked_film()
        file["preview"]["frame_count"] = 3960
        file["previews"][0]["frame_count"] = 3960
        fx.install(authed_page, _api_with((file, item)))
        _open(authed_page, app_url, fx.FILM)

        expect(_tile(authed_page, "preview").locator(".insp-stat-title")).to_have_text("Stops at 2:12:00")
        expect(_tile(authed_page, "preview").locator(".insp-stat-sub")).to_have_text(
            "Covers 2:12:00 of 2:16:18 · Regenerate preview to finish it"
        )
        authed_page.locator("#inspOverview").scroll_into_view_if_needed()
        bar = authed_page.locator("#inspOverview").bounding_box()
        assert bar
        authed_page.mouse.click(bar["x"] + bar["width"] * 0.99, bar["y"] + 10)
        expect(authed_page.locator("#inspNow")).to_have_text(re.compile(r"^2:1[45]:"))
        expect(authed_page.locator("#inspFrameText")).to_have_text("no preview frame here")
        expect(authed_page.locator("#inspStrip .insp-tl-frame.is-now .insp-tl-none")).to_have_text("No preview")
        fx.screenshot(authed_page, "14-partial-preview")


@pytest.mark.e2e
class TestServersMixed:
    def test_a_server_that_cant_be_read_and_one_waiting_for_the_next_job(self, authed_page: Page, app_url: str) -> None:
        fx.install(authed_page, _api_with(fx.mixed_film()))
        _open(authed_page, app_url, fx.FILM)

        servers = _tile(authed_page, "servers")
        expect(servers.locator(".insp-stat-title")).to_have_text("2 need attention")
        expect(servers.locator(".insp-stat-title")).to_have_class(re.compile(r"\bis-warn\b"))
        expect(servers.locator(".insp-stat-sub")).to_have_text(
            "Plex keeps its own markers. Jellyfin couldn't be read just now. Emby gets it on the next job."
        )
        expect(authed_page.locator("#inspServersPill")).to_have_text("2 need attention")
        expect(_lane(authed_page, "jf-1")).to_have_text("Couldn't read what it shows now")
        expect(_lane(authed_page, "emby-1")).to_have_text("Nothing yet · the next job adds credits 2:09:25 → end")
        expect(authed_page.locator(".insp-lane-name[data-lane='jf-1'] .insp-dot")).to_have_class(
            re.compile(r"\bis-bad\b")
        )
        expect(authed_page.locator(".insp-lane-name[data-lane='emby-1'] .insp-dot")).to_have_class(
            re.compile(r"\bis-wait\b")
        )
        jf = authed_page.locator("#inspServers .insp-server[data-server-id='jf-1']")
        expect(jf.locator(".insp-server-shows")).to_have_text("Couldn't read what it shows now")
        expect(jf).to_contain_text("Couldn't read this server's Intro & Credits state (ConnectionError)")
        emby = authed_page.locator("#inspServers .insp-server[data-server-id='emby-1']")
        expect(emby.locator(".insp-server-shows")).to_have_text("Nothing yet · the next job adds credits 2:09:25 → end")
        # Credits here aren't on every server: only Jellyfin's own row is unknown, Emby's is empty.
        expect(authed_page.locator("#inspNowTag")).to_have_text("Credits · Plex starts at 2:09:28")
        fx.screenshot(authed_page, "07-servers-mixed")


@pytest.mark.e2e
class TestAdjustInline:
    def test_adjust_opens_under_the_strip_with_frames_from_the_video(self, authed_page: Page, app_url: str) -> None:
        api = fx.install(authed_page, _api_with(fx.checked_film()))
        _open(authed_page, app_url, fx.FILM)
        authed_page.locator("#inspAdjust").click()

        panel = authed_page.locator("#inspTimeline #inspAdjustPanel")
        expect(panel).to_be_visible()
        expect(panel.locator(".insp-adjust-title")).to_have_text("Adjust credits")
        expect(panel.locator("[data-adjust='credits'] .insp-adjust-range")).to_have_text("2:09:25 → end of file")
        start = panel.locator("[data-edge='credits-start']")
        expect(start.locator(".insp-frame-time")).to_have_text(
            ["2:09:22", "2:09:23", "2:09:24", "2:09:25", "2:09:26", "2:09:27", "2:09:28", "2:09:29"]
        )
        expect(start.locator(".insp-frame img")).to_have_count(8)
        assert {"path": fx.FILM, "start_ms": 7_762_000, "count": 8} in api.frame_requests
        # Our edge before the 2:09:25 frame, Plex's own before 2:09:28, and the frames inside the credits in amber.
        expect(start.locator(".insp-adj-line.is-ours .insp-adj-flag")).to_have_text("Credits start · 2:09:25")
        expect(start.locator(".insp-adj-line.is-own")).to_have_attribute("title", "Plex · 2:09:28")
        expect(start.locator(".insp-frame.is-inside")).to_have_count(5)
        expect(start.locator("input")).to_have_value("2:09:25")
        expect(authed_page.locator("#inspToEnd-credits")).to_be_checked()
        expect(panel.locator(".insp-toend")).to_have_text("Runs to the end of the file (2:16:18)")
        expect(authed_page.locator("#inspAdjustSave")).to_have_text("Save and send to Plex, Jellyfin and Emby")
        # Adjust took the strip to the credits and keeps it there.
        expect(authed_page.locator("#inspNow")).to_have_text("2:09:26")
        fx.screenshot(authed_page, "04-adjust")

        start.get_by_role("button", name="Move the credits start one second later").click()
        expect(panel.locator("[data-adjust='credits'] .insp-adjust-range")).to_have_text("2:09:26 → end of file")
        start = panel.locator("[data-edge='credits-start']")
        start.get_by_role("button", name="Frame at 2:09:24: set the credits start here").click()
        expect(panel.locator("[data-adjust='credits'] .insp-adjust-range")).to_have_text("2:09:24 → end of file")
        with authed_page.expect_response(
            lambda r: r.url.endswith("/api/markers/item/markers") and r.request.method == "POST"
        ):
            authed_page.locator("#inspAdjustSave").click()
        assert api.saves == [{"path": fx.FILM, "markers": [{"type": "credits", "start_ms": 7_764_000, "end_ms": None}]}]
        expect(authed_page.locator("#inspAdjustPanel")).to_have_count(0)

    def test_the_strip_keeps_its_place_while_editing(self, authed_page: Page, app_url: str) -> None:
        fx.install(authed_page, _api_with(fx.checked_film()))
        _open(authed_page, app_url, fx.FILM)
        authed_page.locator("#inspAdjust").click()
        expect(authed_page.locator("#inspNow")).to_have_text("2:09:26")
        authed_page.get_by_role("button", name="Back 10 frames").click()
        expect(authed_page.locator("#inspNow")).to_have_text("2:09:06")
        # An edit draws the panel again; the strip, its images and its place stay as they were.
        img = authed_page.locator("#inspStrip .insp-tl-frame.is-now img")
        img.evaluate("i => { i.dataset.kept = '1'; }")
        authed_page.locator("[data-edge='credits-start']").get_by_role(
            "button", name="Move the credits start one second later"
        ).click()
        expect(authed_page.locator("[data-adjust='credits'] .insp-adjust-range")).to_have_text("2:09:26 → end of file")
        expect(authed_page.locator("#inspNow")).to_have_text("2:09:06")
        expect(authed_page.locator("#inspStrip .insp-tl-frame.is-now img")).to_have_attribute("data-kept", "1")


@pytest.mark.e2e
class TestEpisodeTimeline:
    def test_an_episode_opens_on_its_intro_and_jumps_to_its_credits(self, authed_page: Page, app_url: str) -> None:
        fx.install(authed_page)
        _open(authed_page, app_url, fx.EPISODE)
        expect(authed_page.locator("#inspNow")).to_have_text("2:46")
        expect(authed_page.locator("#inspNowTag")).to_have_text("Intro")
        expect(authed_page.locator("#inspJumps button")).to_have_text(["Intro2:45", "Credits24:59"])
        expect(_lane(authed_page, "found").locator(".insp-band")).to_have_text(
            ["Intro 2:45 – 2:59", "Credits 24:59 → end"]
        )
        expect(_lane(authed_page, "found").locator(".insp-band").first).to_have_class(re.compile(r"\bis-intro\b"))
        expect(_lane(authed_page, "jf-1")).to_have_text(
            "Nothing yet · the next job adds intro 2:45 – 2:59 and credits 24:59 → end"
        )
        authed_page.locator("#inspJumps button", has_text="Credits24:59").click()
        expect(authed_page.locator("#inspNow")).to_have_text("25:00")
        expect(authed_page.locator("#inspNowTag")).to_have_text("Credits")
        fx.screenshot(authed_page, "02-strip-at-credits")


def _length_unknown() -> tuple[dict, dict]:
    """An unchecked film whose only preview is Jellyfin trickplay with no stated interval, and no server gives its
    length: there are frames, but no times to put them at."""
    file, item = fx.unchecked_film()
    folder = f"{fx.FILM.rsplit('/', 1)[0]}/trickplay/The Matrix (1999)/320 - 10x10"
    trickplay = fx.preview_row(
        "jf-1",
        "Jellyfin",
        "jellyfin",
        path=folder,
        sheets_dir=folder,
        exists=True,
        frame_count=818,
        interval_ms=None,
        tile_width=10,
        tile_height=10,
    )
    file.update(duration_ms=None, previews=[trickplay], preview=dict(trickplay))
    item["servers"][0]["duration_ms"] = None
    return file, item


@pytest.mark.e2e
class TestLengthUnknown:
    def test_frames_are_shown_by_number_and_open_large(self, authed_page: Page, app_url: str) -> None:
        fx.install(authed_page, _api_with(_length_unknown()))
        _open(authed_page, app_url, fx.FILM)

        timeline = authed_page.locator("#inspTimeline")
        expect(timeline.locator("#inspLengthNote")).to_have_text(
            "This file's length isn't known yet, so frames are shown by number."
        )
        expect(timeline.locator(".insp-tl-range")).to_have_text("818 frames")
        expect(timeline.locator(".insp-tl-title .info-icon")).to_have_attribute(
            "aria-label", re.compile(r"^Every preview frame in order, by number\.")
        )
        expect(authed_page.locator("#inspNow")).to_have_text("Frame 1")
        expect(authed_page.locator("#inspOvNow")).to_have_text("Frame 1")
        expect(authed_page.locator("#inspFrameText")).to_have_text("preview frame 1 of 818")
        expect(authed_page.locator("#inspNowTag")).to_have_text("")
        times = authed_page.locator("#inspStrip .insp-tl-frame .insp-tl-time")
        expect(times.nth(0)).to_have_text("Frame 1")
        expect(times.nth(1)).to_have_text("Frame 2")
        first = authed_page.locator("#inspStrip .insp-tl-frame[data-index='0']")
        expect(first.get_by_role("button", name="See frame 1 large")).to_be_visible()
        expect(first.locator("img")).to_have_attribute("src", re.compile(r"^/api/bif/trickplay/frame\?.*&index=0&"))
        # No times, so nothing that needs one: no rows, edge lines, jump chips or time axis, and no Adjust.
        expect(authed_page.locator("#inspStrip .insp-lane")).to_have_count(0)
        expect(authed_page.locator("#inspStrip .insp-edge")).to_have_count(0)
        expect(authed_page.locator(".insp-lane-names .insp-lane-name")).to_have_count(0)
        expect(authed_page.locator("#inspJumps button")).to_have_count(0)
        expect(timeline.locator(".insp-ov-axis")).to_have_count(0)
        expect(authed_page.locator("#inspAdjust")).to_have_count(0)
        expect(_tile(authed_page, "preview").locator(".insp-stat-title")).to_have_text("818 frames")
        expect(authed_page.locator("#inspNotChecked .insp-notchecked-text")).to_have_text(
            "Plex shows 2 credits markers of its own today. Checking the film decides ours. With “Keep Plex's "
            "markers” on, Plex keeps its own either way."
        )

        authed_page.get_by_role("button", name="Forward 10 frames").click()
        expect(authed_page.locator("#inspNow")).to_have_text("Frame 11")
        expect(authed_page.locator("#inspFrameText")).to_have_text("preview frame 11 of 818")
        fx.screenshot(authed_page, "23-length-unknown")

        authed_page.locator("#inspStrip .insp-tl-frame.is-now .insp-tl-img").click()
        dialog = authed_page.locator("#inspFrameDialog")
        expect(dialog).to_be_visible()
        expect(authed_page.locator("#inspBigTime")).to_have_text("Frame 11")
        expect(authed_page.locator("#inspBigText")).to_have_text("preview frame 11 of 818")
        expect(authed_page.locator("#inspBigTag")).to_have_text("")
        expect(authed_page.locator("#inspBigImg")).to_have_attribute("src", re.compile(r"&index=10&"))
        authed_page.keyboard.press("ArrowRight")
        expect(authed_page.locator("#inspBigTime")).to_have_text("Frame 12")
        expect(authed_page.locator("#inspBigImg")).to_have_attribute("src", re.compile(r"&index=11&"))
        dialog.get_by_role("button", name="Previous").click()
        expect(authed_page.locator("#inspBigTime")).to_have_text("Frame 11")
        authed_page.keyboard.press("Escape")
        expect(dialog).not_to_be_visible()
        # The strip followed the big frame.
        expect(authed_page.locator("#inspNow")).to_have_text("Frame 11")
