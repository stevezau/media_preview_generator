"""ffprobe wrapper for duration and chapters."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass


class ProbeError(Exception):
    """ffprobe could not read the file."""


@dataclass(frozen=True)
class Chapter:
    """A container chapter (times in ms)."""

    start_ms: int
    end_ms: int | None
    title: str


@dataclass(frozen=True)
class MediaProbe:
    """What the marker pipeline needs from ffprobe."""

    duration_ms: int | None
    chapters: tuple[Chapter, ...]


def ffprobe_path_for(ffmpeg_path: str | None) -> str:
    """Prefer the ffprobe shipped next to the configured ffmpeg (jellyfin-ffmpeg in the image)."""
    if ffmpeg_path:
        sibling = os.path.join(os.path.dirname(ffmpeg_path), "ffprobe")
        if os.path.isfile(sibling) and os.access(sibling, os.X_OK):
            return sibling
    return shutil.which("ffprobe") or "ffprobe"


def _ms(value: object) -> int | None:
    try:
        return int(round(float(value) * 1000))
    except (TypeError, ValueError, OverflowError):
        # Defensive: non-finite values can't become ms (nan raises ValueError, inf raises
        # OverflowError on round()); not a claim that ffprobe actually emits either.
        return None


def probe_media(path: str, *, ffprobe: str, timeout_s: float = 60.0) -> MediaProbe:
    """Read duration and chapters.

    Args:
        path: Media file.
        ffprobe: ffprobe binary.
        timeout_s: Hard timeout (hung mounts must not hold a check thread forever).

    Returns:
        Duration (None if unknown) and chapters in container order.

    Raises:
        ProbeError: ffprobe missing, timed out, failed or returned invalid JSON.
    """
    cmd = [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_chapters", path]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise ProbeError(f"ffprobe failed for {path}: {type(exc).__name__}: {exc}") from exc
    if proc.returncode != 0:
        raise ProbeError(f"ffprobe exited {proc.returncode} for {path}: {(proc.stderr or '').strip()[:300]}")
    try:
        data = json.loads(proc.stdout or "")
    except ValueError as exc:
        raise ProbeError(f"ffprobe returned invalid JSON for {path}") from exc
    if not isinstance(data, dict):
        raise ProbeError(f"ffprobe returned unexpected JSON for {path}: top level was {type(data).__name__}")
    chapters = []
    for raw in data.get("chapters") or []:
        tags = {str(k).lower(): v for k, v in (raw.get("tags") or {}).items()}
        start = _ms(raw.get("start_time"))
        if start is None:
            continue
        chapters.append(Chapter(start, _ms(raw.get("end_time")), str(tags.get("title") or "")))
    return MediaProbe(duration_ms=_ms((data.get("format") or {}).get("duration")), chapters=tuple(chapters))
