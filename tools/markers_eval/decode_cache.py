"""The credit text detector's decodes, cached per exact ffmpeg command, so a change to rule J costs seconds, not an hour.

Rule J is pure (rows in, answer out); the hour a ``credits-text`` run takes on storage is the decoding and text
detection behind the rows. :class:`DecodeCache` keeps each decode's rows keyed on the file's identity, the ffmpeg build
and the exact command the app builds for it (window, keyframes or 1 fps, hwaccel arguments, scaler and keyframe
thinning all included), the text detection backend that read the boxes, and a digest of the code that turns a
command into rows (``credits_text.decode_digest``). The app's own ``find_credits`` then runs unchanged against it: a
rule change re-reads stored rows and decodes only the windows it hasn't read before, while a change to the decode code,
the ffmpeg build, the text detection or the model's pin reads everything again.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path

import numpy as np
from loguru import logger

from media_preview_generator.markers.credits import frames
from media_preview_generator.markers.freeze import Freeze


def rows_to_json(rows: Sequence[frames.Row]) -> list[list]:
    """Rows as JSON: ``[pts, box count, luma, [[left, top, right, bottom], ...]]`` per frame.

    Args:
        rows: From ``frames.decode_rows``.

    Returns:
        The same rows, JSON types only.
    """
    return [[pts, count, luma, [list(box) for box in boxes]] for pts, count, luma, boxes in rows]


def rows_from_json(stored: Iterable) -> list[frames.Row]:
    """Rows back from :func:`rows_to_json`, box positions included.

    Args:
        stored: What :func:`rows_to_json` wrote.

    Returns:
        The rows, as ``frames.decode_rows`` returned them.

    Raises:
        TypeError, ValueError: A row isn't ``[pts, count, luma, boxes]`` with four numbers per box (an entry written
            before rows carried positions). :meth:`DecodeCache.decode_rows` decodes the window again instead of
            serving it; a caller reading an entry itself has to decide what to do.
    """
    return [
        (float(pts), int(count), float(luma), tuple((int(a), int(b), int(c), int(d)) for a, b, c, d in boxes))
        for pts, count, luma, boxes in stored
    ]


def ffmpeg_build(ffmpeg: str) -> str:
    """The first line of ``ffmpeg -version`` (``ffmpeg version 8.0.1-3ubuntu2 Copyright …``).

    Raises:
        subprocess.CalledProcessError: ffmpeg couldn't say its version (it can't decode either).
    """
    result = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True, check=True, timeout=30)
    return result.stdout.splitlines()[0]


class DecodeCache:
    """``decode_rows``, ``container_start_s`` and ``keyframe_thinning`` with :mod:`frames`' own signatures, answered
    from disk when this exact decode or probe of this exact file was run before by the same decode code and ffmpeg
    build and, for rows, the same text detection backend.

    A GPU decode that failed (:class:`frames.GpuDecodeError`: ffmpeg exited non-zero on the GPU or gave no frames, as
    for a codec the card can't decode) is kept as that failure, so the harness's CPU rerun of the file doesn't try the
    GPU again on every run. A one-off failure is kept too, until its entry is deleted or the decode code changes.
    """

    def __init__(
        self,
        root: Path,
        *,
        digest: str,
        backend: Callable[[], str | None],
        build: Callable[[str], str] | None = None,
    ) -> None:
        """Create the cache (``root/credits_decodes``).

        Args:
            root: Cache folder; must not be under /data*.
            digest: The digest of the code that turns a command into rows (``credits_text.decode_digest``).
            backend: The text detection backend reading the boxes right now (``webgpu cuda:0``, ``cpu``), None
                before its first request. It isn't in the command: a GPU run's CPU rerun of a file the card can't
                decode builds the CPU run's command but counts on the GPU helper, and the helper's own self-test can
                put a GPU worker's counting on the CPU.
            build: An ffmpeg binary's build (:func:`ffmpeg_build`, asked once per binary): the same command on another
                build is another decode.

        Raises:
            ValueError: ``root`` is under /data*.
        """
        if str(root.resolve()).startswith("/data"):
            raise ValueError("the decode cache must not live under /data*")
        self._root = root / "credits_decodes"
        self._root.mkdir(parents=True, exist_ok=True)
        self.digest, self._backend = digest, backend
        self._build = build or ffmpeg_build
        self._builds: dict[str, str] = {}
        self.decoded = 0
        self.reused = 0
        # The real functions, taken before :meth:`serving` puts this cache's own in their place on the module.
        self._container_start_s, self._decode_rows = frames.container_start_s, frames.decode_rows
        self._keyframe_thinning = frames.keyframe_thinning

    def _entry(self, path: str, ffmpeg: str, what: str) -> Path:
        if ffmpeg not in self._builds:
            self._builds[ffmpeg] = self._build(ffmpeg)
        st = os.stat(path)
        key = f"{path}|{st.st_size}|{st.st_mtime_ns}|{self.digest}|{self._builds[ffmpeg]}|{what}"
        return self._root / (hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest() + ".json")

    def _write(self, entry: Path, data: dict) -> None:
        """Write an entry whole or not at all: a run killed mid-write must not leave JSON every later run trips on."""
        with tempfile.NamedTemporaryFile("w", dir=self._root, suffix=".tmp", delete=False) as handle:
            json.dump(data, handle)
        os.replace(handle.name, entry)

    def container_start_s(
        self,
        path: str,
        ffmpeg: str,
        *,
        cancel_check: Callable[[], bool] | None = None,
        timeout_s: float = frames.PROBE_TIMEOUT_S,
    ) -> float:
        """:func:`frames.container_start_s`, once per file identity."""
        entry = self._entry(path, ffmpeg, f"start|{ffmpeg}")
        if entry.exists():
            return json.loads(entry.read_text())["start_s"]
        start_s = self._container_start_s(path, ffmpeg, cancel_check=cancel_check, timeout_s=timeout_s)
        self._write(entry, {"start_s": start_s})
        return start_s

    def keyframe_thinning(
        self,
        path: str,
        ffmpeg: str,
        *,
        cancel_check: Callable[[], bool] | None = None,
        timeout_s: float = frames.PROBE_TIMEOUT_S,
    ) -> frames.KeyframeThinning:
        """:func:`frames.keyframe_thinning`, once per file identity (a timeout or a stuck ffprobe raises, uncached)."""
        entry = self._entry(path, ffmpeg, f"thinning|{ffmpeg}")
        if entry.exists():
            return frames.KeyframeThinning(**json.loads(entry.read_text()))
        thinning = self._keyframe_thinning(path, ffmpeg, cancel_check=cancel_check, timeout_s=timeout_s)
        self._write(entry, thinning._asdict())
        return thinning

    def decode_rows(
        self,
        path: str,
        *,
        ffmpeg: str,
        start_s: float,
        length_s: float | None,
        keyframes_only: bool,
        fps: int | None,
        gpu: str | None,
        gpu_device_path: str | None,
        detect_boxes: Callable[[np.ndarray], list[tuple[frames.Box, ...]]],
        cancel_check: Callable[[], bool] | None = None,
        timeout_s: float = frames.DECODE_TIMEOUT_S,
        start_time_s: float | None = None,
        keep_every: int | None = None,
        drop_non_key: bool = False,
        scale: int = 1,
        download_format: str | None = None,
        pause_check: Callable[[], bool] | Freeze | None = None,
        ffmpeg_threads: int | None = None,
    ) -> list[frames.Row]:
        """:func:`frames.decode_rows`, once per file identity, exact command, start time and text detection backend.

        The command carries the frame size and the format GPU surfaces are downloaded in, so a tail's 640x360 read
        (``scale`` 2) is a decode of its own, and so is a stream whose format changed.

        Rows are kept under the backend that counted them. A decode whose backend changed on the way (a GPU helper
        demoted to the CPU mid-decode) or can't be told (no count was made) is used but not kept. A decode that starts
        before the helper's first request is kept under the backend it ends on, so a caller that must not mix backends
        in one entry makes one count first (the harness counts a blank frame before its first file).

        Raises:
            frames.GpuDecodeError: This GPU decode failed, now or on an earlier run.
            Everything else :func:`frames.decode_rows` raises, uncached: a timeout or a cancel is no answer.
        """
        command, _ = frames.decode_command(
            ffmpeg, path, start_s=start_s, length_s=length_s, keyframes_only=keyframes_only, fps=fps, gpu=gpu,
            gpu_device_path=gpu_device_path, keep_every=keep_every, drop_non_key=drop_non_key, scale=scale,
            download_format=download_format, ffmpeg_threads=ffmpeg_threads,
        )  # fmt: skip
        if start_time_s is None:
            start_time_s = self.container_start_s(path, ffmpeg, cancel_check=cancel_check)
        what = f"{start_time_s!r}|{json.dumps(command)}"
        before = self._backend()
        if before is not None:
            entry = self._entry(path, ffmpeg, f"rows|{before}|{what}")
            if entry.exists():
                stored = json.loads(entry.read_text())
                if "gpu_error" in stored:
                    self.reused += 1
                    raise frames.GpuDecodeError(stored["gpu_error"])
                try:
                    rows = rows_from_json(stored["rows"])
                except (TypeError, ValueError) as exc:
                    # An entry from before rows carried positions, or any other shape this code can't read: a cache
                    # miss, not the end of an hour-long run. The decode below overwrites it.
                    logger.warning("Decoding {} again: its stored rows can't be read ({})", os.path.basename(path), exc)
                else:
                    self.reused += 1
                    return rows
        self.decoded += 1
        try:
            rows = self._decode_rows(
                path, ffmpeg=ffmpeg, start_s=start_s, length_s=length_s, keyframes_only=keyframes_only, fps=fps,
                gpu=gpu, gpu_device_path=gpu_device_path, detect_boxes=detect_boxes, cancel_check=cancel_check,
                timeout_s=timeout_s, start_time_s=start_time_s, keep_every=keep_every, drop_non_key=drop_non_key,
                scale=scale, download_format=download_format, pause_check=pause_check, ffmpeg_threads=ffmpeg_threads,
            )  # fmt: skip
        except frames.GpuDecodeError as exc:
            self._keep(path, ffmpeg, what, before, {"gpu_error": str(exc)})
            raise
        self._keep(path, ffmpeg, what, before, {"rows": rows_to_json(rows)})
        return rows

    def _keep(self, path: str, ffmpeg: str, what: str, before: str | None, data: dict) -> None:
        after = self._backend()
        if after is not None and before in (None, after):
            self._write(self._entry(path, ffmpeg, f"rows|{after}|{what}"), data)

    @contextlib.contextmanager
    def serving(self) -> Iterator[None]:
        """Answer the detector's decodes and probes from this cache inside the block (``detector.find_credits`` reads
        these functions off the :mod:`frames` module at call time)."""
        names = ("decode_rows", "container_start_s", "keyframe_thinning")
        saved = {name: getattr(frames, name) for name in names}
        for name in names:
            setattr(frames, name, getattr(self, name))
        try:
            yield
        finally:
            for name, function in saved.items():
                setattr(frames, name, function)
