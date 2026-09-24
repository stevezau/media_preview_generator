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
from media_preview_generator.markers.speed import playback_speed


class FingerprintCache:
    """``points(path)``: the app's own fingerprint of a file, computed once per file identity (``retimed``: one made
    at another speed, once per factor); ``speed(path)``: its playback speed, read once per file identity."""

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

    def points(self, path: str, retime: float | None = None) -> np.ndarray:
        """The file's fingerprint (spec window and algorithm), from the cache when the file is unchanged.

        Args:
            path: Media file (only read).
            retime: Its audio retimed by this factor (``fingerprint.fingerprint_command``), None for its own speed.

        Returns:
            The points.
        """
        st = os.stat(path)
        duration_ms = probe_media(path, ffprobe=self._ffprobe).duration_ms
        if not duration_ms:
            raise ValueError(f"no duration for {os.path.basename(path)}")
        key = f"{path}|{st.st_size}|{st.st_mtime_ns}|{window_s(duration_ms):.3f}|{ALGORITHM}"
        if retime is not None:
            key += f"|retime {retime:.6f}"
        cached = self._root / (hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest() + ".npy")
        if cached.exists():
            return np.load(cached)
        points = compute_fingerprint(path, duration_ms, ffmpeg=self._ffmpeg, retime=retime)
        np.save(cached, points)
        return points

    def retimed(self, path: str, retime: float) -> np.ndarray:
        """The file's fingerprint with its audio retimed by ``retime`` (:meth:`points`)."""
        return self.points(path, retime)

    def speed(self, path: str) -> float | None:
        """The file's playback speed (``speed.playback_speed`` of its video frame rate), cached per file identity."""
        return playback_speed(self.frame_rate(path))

    def frame_rate(self, path: str) -> float | None:
        """The file's probed video frame rate (``probe.MediaProbe.frame_rate``), cached per file identity."""
        st = os.stat(path)
        key = f"{path}|{st.st_size}|{st.st_mtime_ns}|frame rate"
        cached = self._root / "rates" / (hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest() + ".json")
        if cached.exists():
            return json.loads(cached.read_text())["frame_rate"]
        frame_rate = probe_media(path, ffprobe=self._ffprobe).frame_rate
        cached.parent.mkdir(exist_ok=True)
        cached.write_text(json.dumps({"frame_rate": frame_rate}))
        return frame_rate


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
