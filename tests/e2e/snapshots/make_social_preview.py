#!/usr/bin/env python3
"""Generate docs/images/social-preview.jpg — the GitHub/OpenGraph social card.

Usage:
    python tests/e2e/snapshots/make_social_preview.py

Composites the app icon + wordmark + tagline on the left against a crop of
the dashboard screenshot (docs/images/home.webp — regenerate that first via
``regen_readme.py`` if it's stale) on the right, over a dark background that
matches the app's own theme (``--plex-gray`` / ``--plex-dark`` in
style.css). Output is 1280x640 (2:1, the size GitHub/Twitter/Slack unfurl
cards expect), baseline (non-progressive) JPEG, kept under ~200KB by
lowering JPEG quality until it fits.

Requires docs/images/icon.png and docs/images/home.webp to already exist.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

_REPO_ROOT = Path(__file__).resolve().parents[3]
IMAGES_DIR = _REPO_ROOT / "docs" / "images"
OUT_PATH = IMAGES_DIR / "social-preview.jpg"

CANVAS_W, CANVAS_H = 1280, 640
MAX_BYTES = 200_000

# Colors lifted from media_preview_generator/web/static/css/style.css
# (--plex-gray / --plex-dark / --plex-orange) so the card matches the app's
# own dark theme instead of introducing a new palette.
BG_TOP = (26, 26, 46)  # --plex-gray #1a1a2e
BG_BOTTOM = (15, 15, 26)  # --plex-dark #0f0f1a
ACCENT = (229, 160, 13)  # --plex-orange #e5a00d
TEXT_PRIMARY = (245, 245, 248)
TEXT_SECONDARY = (184, 188, 200)

FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")
FONT_BOLD = FONT_DIR / "DejaVuSans-Bold.ttf"
FONT_REGULAR = FONT_DIR / "DejaVuSans.ttf"

LEFT_MARGIN = 72
TEXT_WRAP_WIDTH = 500


def _vertical_gradient(size: tuple[int, int], top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    w, h = size
    base = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / max(h - 1, 1)
        px = tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        base.putpixel((0, y), px)
    return base.resize(size)


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _transparent_icon(path: Path, size: tuple[int, int], tolerance: int = 12) -> Image.Image:
    """Load icon.png and key out its flat #1a1a1a square background.

    icon.png ships as a solid square (no alpha) — pasted directly onto any
    background that isn't that exact color, it reads as a visibly different
    tile rather than a logo. Color-keying the flat background to
    transparent lets the glyph sit directly on the card's own background
    instead, regardless of what that background is.
    """
    icon = Image.open(path).convert("RGBA")
    bg = icon.getpixel((0, 0))
    pixels = icon.load()
    for py in range(icon.height):
        for px in range(icon.width):
            r, g, b, a = pixels[px, py]
            if abs(r - bg[0]) <= tolerance and abs(g - bg[1]) <= tolerance and abs(b - bg[2]) <= tolerance:
                pixels[px, py] = (r, g, b, 0)
    return icon.resize(size, Image.LANCZOS)


def _rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([(0, 0), (size[0] - 1, size[1] - 1)], radius=radius, fill=255)
    return mask


def _browser_mockup(screenshot: Image.Image, box_w: int, box_h: int) -> Image.Image:
    """Crop the dashboard screenshot into a small "browser window" card.

    Crops the top-left of the screenshot (navbar + System & Workers card —
    the most recognizable part of the dashboard at a glance) and adds a
    thin macOS-style chrome bar so it reads as a product screenshot rather
    than a random rectangle of UI.
    """
    chrome_h = 30
    crop_box = (0, 0, min(860, screenshot.width), min(660, screenshot.height))
    crop = screenshot.crop(crop_box).convert("RGB")

    target_w = box_w
    target_h = box_h - chrome_h
    scale = min(target_w / crop.width, target_h / crop.height)
    crop = crop.resize((round(crop.width * scale), round(crop.height * scale)), Image.LANCZOS)

    mockup = Image.new("RGB", (box_w, box_h), (36, 36, 58))
    chrome = ImageDraw.Draw(mockup)
    chrome.rectangle([(0, 0), (box_w, chrome_h)], fill=(42, 42, 66))
    for i, color in enumerate([(237, 106, 94), (245, 191, 79), (97, 194, 82)]):
        cx = 16 + i * 20
        chrome.ellipse([(cx, chrome_h // 2 - 5), (cx + 10, chrome_h // 2 + 5)], fill=color)

    paste_x = (box_w - crop.width) // 2
    mockup.paste(crop, (paste_x, chrome_h))

    mask = _rounded_mask((box_w, box_h), radius=14)
    rounded = Image.new("RGBA", (box_w, box_h))
    rounded.paste(mockup, (0, 0))
    rounded.putalpha(mask)
    return rounded


def build(out_path: Path = OUT_PATH) -> Path:
    icon_path = IMAGES_DIR / "icon.png"
    screenshot_path = IMAGES_DIR / "home.webp"
    if not icon_path.exists():
        raise FileNotFoundError(f"missing {icon_path}")
    if not screenshot_path.exists():
        raise FileNotFoundError(f"missing {screenshot_path} — run regen_readme.py first")

    canvas = _vertical_gradient((CANVAS_W, CANVAS_H), BG_TOP, BG_BOTTOM).convert("RGB")
    draw = ImageDraw.Draw(canvas)

    # --- Right side: dashboard screenshot in a small browser mockup -------
    mockup_w, mockup_h = 560, 460
    mockup = _browser_mockup(Image.open(screenshot_path), mockup_w, mockup_h)
    mockup_x = CANVAS_W - mockup_w - 64
    mockup_y = (CANVAS_H - mockup_h) // 2

    # Soft drop shadow behind the mockup.
    shadow = Image.new("RGBA", (mockup_w + 40, mockup_h + 40), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle([(20, 24), (mockup_w + 20, mockup_h + 24)], radius=18, fill=(0, 0, 0, 110))
    shadow = shadow.filter(ImageFilter.GaussianBlur(14))
    canvas.paste(shadow, (mockup_x - 20, mockup_y - 16), shadow)
    canvas.paste(mockup, (mockup_x, mockup_y), mockup)

    # --- Left side: icon + wordmark + tagline, all left-aligned on ---------
    # LEFT_MARGIN and vertically centered as one block against the canvas
    # (same center line the screenshot mockup uses), so the two halves
    # balance instead of the text block floating high.
    icon_size = 96
    title_font = ImageFont.truetype(str(FONT_BOLD), 48)
    tagline_font = ImageFont.truetype(str(FONT_REGULAR), 25)
    title_lines = ["Media Preview", "Generator"]
    title_line_height = 58
    tagline_line_height = 34
    gap_icon_to_title = 40
    gap_title_to_tagline = 24

    tagline = "GPU-accelerated video preview thumbnails for Plex, Emby & Jellyfin"
    tagline_lines = _wrap_text(draw, tagline, tagline_font, TEXT_WRAP_WIDTH)

    block_h = (
        icon_size
        + gap_icon_to_title
        + len(title_lines) * title_line_height
        + gap_title_to_tagline
        + len(tagline_lines) * tagline_line_height
    )
    y = (CANVAS_H - block_h) // 2

    icon = _transparent_icon(icon_path, (icon_size, icon_size))
    canvas.paste(icon, (LEFT_MARGIN, y), icon)
    y += icon_size + gap_icon_to_title

    for line in title_lines:
        draw.text((LEFT_MARGIN, y), line, font=title_font, fill=TEXT_PRIMARY)
        y += title_line_height

    y += gap_title_to_tagline
    for line in tagline_lines:
        draw.text((LEFT_MARGIN, y), line, font=tagline_font, fill=TEXT_SECONDARY)
        y += tagline_line_height

    # --- Encode as baseline JPEG under MAX_BYTES ---------------------------
    quality = 90
    while quality >= 60:
        canvas.save(out_path, "JPEG", quality=quality, progressive=False, optimize=True)
        size = out_path.stat().st_size
        if size <= MAX_BYTES:
            print(f"[make_social_preview] wrote {out_path} ({size:,} bytes, quality={quality})", file=sys.stderr)
            return out_path
        quality -= 5

    print(f"[make_social_preview] wrote {out_path} ({out_path.stat().st_size:,} bytes, min quality)", file=sys.stderr)
    return out_path


if __name__ == "__main__":
    build()
