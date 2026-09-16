"""Fingerprints of real episodes, cached on local disk (never next to the media) so harness re-runs are cheap."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from pathlib import Path

import numpy as np

from media_preview_generator.markers.audio.fingerprint import ALGORITHM, compute_fingerprint, window_s
from media_preview_generator.markers.probe import Chapter, MediaProbe, probe_media


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

    @property
    def root(self) -> Path:
        """The cache folder."""
        return self._root

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


class ProbeCache:
    """``probe(path)``: ffprobe duration and chapters of a file, cached as JSON per file identity."""

    def __init__(self, root: Path, *, ffprobe: str) -> None:
        """Create the cache.

        Args:
            root: Cache folder (a ``probes`` folder is created in it); must not be under /data*.
            ffprobe: ffprobe binary.

        Raises:
            ValueError: ``root`` is under /data*.
        """
        if str(root.resolve()).startswith("/data"):
            raise ValueError("the probe cache must not live under /data*")
        self._root = root / "probes"
        self._root.mkdir(parents=True, exist_ok=True)
        self._ffprobe = ffprobe

    def probe(self, path: str) -> MediaProbe:
        """Probe (or read the cached probe of) one file.

        Args:
            path: Media file (only read, by ffprobe).

        Returns:
            Its duration and chapters.
        """
        st = os.stat(path)
        key = f"{path}|{st.st_size}|{st.st_mtime_ns}"
        cached = self._root / (hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest() + ".json")
        if cached.exists():
            raw = json.loads(cached.read_text())
            chapters = tuple(Chapter(c["start_ms"], c["end_ms"], c["title"]) for c in raw["chapters"])
            return MediaProbe(raw["duration_ms"], chapters)
        probe = probe_media(path, ffprobe=self._ffprobe)
        chapters = [dataclasses.asdict(c) for c in probe.chapters]
        cached.write_text(json.dumps({"duration_ms": probe.duration_ms, "chapters": chapters}))
        return probe
