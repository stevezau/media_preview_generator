#!/usr/bin/env python3
"""Compose the site's player 3-up and the HDR before/after pair from the lab captures.

    /home/data/.venv/bin/python tests/e2e/snapshots/compose_site_images.py players
    /home/data/.venv/bin/python tests/e2e/snapshots/compose_site_images.py hdr --video <host path> --seconds 125

Both are laid out in HTML with the docs site's own tokens and rendered by Chromium at 2x, so the type
matches the site. The HDR pair is honest by construction: the left half is a plain FFmpeg frame grab
with no tone mapping (what a generator that ignores HDR produces), the right half is the frame at the
same moment read straight out of the BIF this app wrote next to the video. "Same moment" is checked:
the plain grab is the candidate frame whose picture matches the BIF frame.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageOps
from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))
from media_preview_generator.bif_reader import read_bif_frame, read_bif_metadata  # noqa: E402

CAPTURES = Path("/home/data/mlab-openfilms/captures")
RECORD = REPO_ROOT / "docs/design/site-redesign-lab/results/captures.json"
# docs/assets/css/main.css dark tokens, repeated because this page never loads the site stylesheet.
STYLE = """
  :root { --bg:#08080a; --surface:#121216; --border:rgba(255,255,255,.09); --text:#f4f4f5; --muted:#94949f;
          --amber:#e5a00d; --font:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  * { box-sizing:border-box; } body { margin:0; padding:24px; background:var(--bg); color:var(--text);
  font-family:var(--font); -webkit-font-smoothing:antialiased; display:inline-flex; gap:20px; }
  figure { margin:0; } img { display:block; border-radius:10px; border:1px solid var(--border); }
  figcaption { margin-top:10px; font-size:15px; font-weight:600; color:var(--muted); text-align:center; }
  figcaption b { color:var(--amber); font-weight:700; }
"""


def _uri(path: Path) -> str:
    kind = "webp" if path.suffix == ".webp" else "jpeg" if path.suffix in (".jpg", ".jpeg") else "png"
    return f"data:image/{kind};base64," + base64.b64encode(path.read_bytes()).decode()


def _render(html: str, out: Path) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(device_scale_factor=2, viewport={"width": 3200, "height": 1200})
        page.set_content(f"<style>{STYLE}</style>{html}")
        page.wait_for_function("() => [...document.images].every((i) => i.complete && i.naturalWidth > 0)")
        shot = page.locator("body").screenshot()
        browser.close()
    Image.open(io.BytesIO(shot)).convert("RGB").save(out, "WEBP", quality=88, method=6)
    print(f"wrote {out} ({out.stat().st_size // 1024} KB)")


def players(allow_two: bool) -> None:
    shots = [
        (name, CAPTURES / f"player-{server}.webp")
        for name, server in (("Plex", "plex"), ("Jellyfin", "jellyfin"), ("Emby", "emby"))
    ]
    present = [(name, path) for name, path in shots if path.is_file()]
    if len(present) < 3 and not allow_two:
        raise SystemExit(f"only {[n for n, _ in present]} captured; pass --allow-two to compose without Emby")
    figures = "".join(
        f'<figure><img src="{_uri(path)}" width="520"><figcaption><b>{name}</b> web player</figcaption></figure>'
        for name, path in present
    )
    _render(figures, CAPTURES / "players-3up.webp")


def _ffmpeg_grab(video: Path, seconds: float, width: int, out: Path) -> None:
    """One frame at exactly `seconds`, scaled to `width`, with no tone mapping and no colour tags."""
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-ss", f"{seconds:.6f}", "-i", str(video), "-frames:v", "1",
         "-vf", f"scale={width}:-2", str(out)],
        check=True,
    )  # fmt: skip
    # FFmpeg 8 copies the video's PQ transfer into the PNG (a cICP chunk), and Chromium would then
    # tone-map the "without" half itself. Re-saving drops the tags, so it shows the way an untagged
    # BIF frame from a generator that ignores HDR does.
    Image.open(out).convert("RGB").save(out)


def _keyframe_times(video: Path, start: float, end: float) -> list[float]:
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey",
         "-read_intervals", f"{start:.3f}%{end:.3f}", "-show_entries", "frame=pts_time", "-of", "csv=p=0", str(video)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return [float(line.split(",")[0]) for line in probe.stdout.splitlines() if line.strip()]


def _shape(path: Path) -> list[float]:
    """Small greyscale pixels scaled to zero mean and unit spread, so one frame matches itself tone-mapped or not."""
    pixels = list(ImageOps.grayscale(Image.open(path)).resize((64, 27)).getdata())
    mean = sum(pixels) / len(pixels)
    spread = (sum((p - mean) ** 2 for p in pixels) / len(pixels)) ** 0.5 or 1.0
    return [(p - mean) / spread for p in pixels]


def _likeness(a: list[float], b: list[float]) -> float:
    """Correlation of two _shape() results: 1.0 is the same picture."""
    return sum(x * y for x, y in zip(a, b, strict=True)) / len(a)


def hdr(video: Path, seconds: float) -> None:
    bifs = sorted(video.parent.glob("*.bif"))
    if not bifs:
        raise SystemExit(f"no BIF next to {video}: run lab_setup.py generate first")
    meta = read_bif_metadata(str(bifs[0]))
    index = min(round(seconds * 1000 / meta.frame_interval_ms), meta.frame_count - 1)
    slot = index * meta.frame_interval_ms / 1000
    with tempfile.TemporaryDirectory() as tmp:
        ours = Path(tmp) / "ours.jpg"
        ours.write_bytes(read_bif_frame(str(bifs[0]), index, meta))
        width = Image.open(ours).width
        # Frame N of the BIF is not always the picture at N x interval: when the app can decode keyframes
        # only, it is the last keyframe at or before that moment. Grab every candidate plainly and keep
        # the one that is the same picture, so both halves show one moment.
        interval = meta.frame_interval_ms / 1000
        candidates = [t for t in _keyframe_times(video, max(0.0, slot - interval), slot + 1) if t <= slot] + [slot]
        target = _shape(ours)
        scored = []
        for n, at in enumerate(candidates):
            grab = Path(tmp) / f"naive-{n}.png"
            _ffmpeg_grab(video, at, width, grab)
            scored.append((_likeness(target, _shape(grab)), at, grab))
        likeness, at, naive = max(scored, key=lambda row: row[0])
        if likeness < 0.9:
            raise SystemExit(f"no plain frame near {slot:.1f}s matches BIF frame {index} (best {likeness:.2f})")
        figures = (
            f'<figure><img src="{_uri(naive)}" width="{width}"><figcaption>Without tone mapping</figcaption></figure>'
            f'<figure><img src="{_uri(ours)}" width="{width}"><figcaption><b>Media Preview Generator</b></figcaption></figure>'
        )
        _render(figures, CAPTURES / "hdr-before-after.webp")
    print(f"BIF frame {index} is the picture at {at:.3f}s (likeness {likeness:.3f})")
    record = json.loads(RECORD.read_text(encoding="utf-8")) if RECORD.is_file() else {}
    record["hdr"] = {
        "title": video.parent.name,
        "seconds": round(at, 3),
        "bif": bifs[0].name,
        "frame_index": index,
        "bif_width": width,
        "likeness": round(likeness, 3),
    }
    RECORD.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    players_parser = sub.add_parser("players")
    players_parser.add_argument("--allow-two", action="store_true")
    hdr_parser = sub.add_parser("hdr")
    hdr_parser.add_argument("--video", type=Path, required=True)
    hdr_parser.add_argument("--seconds", type=float, required=True)
    args = parser.parse_args()
    if args.command == "players":
        players(args.allow_two)
    else:
        hdr(args.video, args.seconds)


if __name__ == "__main__":
    main()
