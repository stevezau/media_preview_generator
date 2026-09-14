"""Fingerprints of real episodes, cached on local disk (never next to the media) so harness re-runs are cheap."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import numpy as np

from media_preview_generator.markers.audio.fingerprint import ALGORITHM, compute_fingerprint, window_s
from media_preview_generator.markers.probe import probe_media


class FingerprintCache:
    """``points(path)``: the app's own fingerprint of a file, computed once per file identity."""

    def __init__(self, root: Path, *, ffmpeg: str, ffprobe: str) -> None:
        """Create the cache.

        Args:
            root: Cache folder (created); must not be under /data*.
            ffmpeg: An ffmpeg with chromaprint.
            ffprobe: ffprobe for durations.
        """
        if str(root.resolve()).startswith("/data"):
            raise ValueError("the fingerprint cache must not live under /data*")
        root.mkdir(parents=True, exist_ok=True)
        self._root, self._ffmpeg, self._ffprobe = root, ffmpeg, ffprobe

    def points(self, path: str) -> np.ndarray:
        """The file's fingerprint (spec window and algorithm), from the cache when the file is unchanged."""
        st = os.stat(path)
        duration_ms = probe_media(path, ffprobe=self._ffprobe).duration_ms
        if not duration_ms:
            raise ValueError(f"no duration for {os.path.basename(path)}")
        key = f"{path}|{st.st_size}|{st.st_mtime_ns}|{window_s(duration_ms):.3f}|{ALGORITHM}"
        cached = self._root / (hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest() + ".npy")
        if cached.exists():
            return np.load(cached)
        points = compute_fingerprint(path, duration_ms, ffmpeg=self._ffmpeg)
        np.save(cached, points)
        return points
