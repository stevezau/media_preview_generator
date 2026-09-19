"""The credit text detector's decodes, cached per exact ffmpeg command, so a change to rule J costs seconds, not an hour.

Rule J is pure (rows in, answer out); the hour a ``credits-text`` run takes on storage is the decoding and text
detection behind the rows. :class:`DecodeCache` keeps each decode's rows keyed on the file's identity, the exact ffmpeg
command the app builds for it (window, keyframes or 1 fps, hwaccel arguments and scaler all included), the text
detection that counted the boxes, and a digest of the code that turns a command into rows
(``credits_text.decode_digest``). The app's own ``find_credits`` then runs unchanged against it: a rule change re-reads
stored rows and decodes only the windows it hasn't read before, while a change to the decode code, the text detection
or the model's pin reads everything again.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np

from media_preview_generator.markers.credits import frames


class DecodeCache:
    """``decode_rows`` and ``container_start_s`` with :mod:`frames`' own signatures, answered from disk when this exact
    decode of this exact file was run before by the same decode code and the same text detection.

    A GPU decode that failed (:class:`frames.GpuDecodeError`: ffmpeg exited non-zero on the GPU or gave no frames, as
    for a codec the card can't decode) is kept as that failure, so the harness's CPU rerun of the file doesn't try the
    GPU again on every run. A one-off failure is kept too, until its entry is deleted or the decode code changes.
    """

    def __init__(self, root: Path, *, digest: str, counter: str) -> None:
        """Create the cache (``root/credits_decodes``).

        Args:
            root: Cache folder; must not be under /data*.
            digest: The digest of the code that turns a command into rows (``credits_text.decode_digest``).
            counter: Which text detection counts the boxes (``gpu cuda:0``, ``cpu``). It isn't in the command: a GPU
                run's CPU rerun of a file the card can't decode builds the CPU run's command but counts on the GPU.

        Raises:
            ValueError: ``root`` is under /data*.
        """
        if str(root.resolve()).startswith("/data"):
            raise ValueError("the decode cache must not live under /data*")
        self._root = root / "credits_decodes"
        self._root.mkdir(parents=True, exist_ok=True)
        self.digest, self.counter = digest, counter
        self.decoded = 0
        self.reused = 0
        # The real functions, taken before :meth:`serving` puts this cache's own in their place on the module.
        self._container_start_s, self._decode_rows = frames.container_start_s, frames.decode_rows

    def _entry(self, path: str, what: str) -> Path:
        st = os.stat(path)
        key = f"{path}|{st.st_size}|{st.st_mtime_ns}|{self.digest}|{what}"
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
        entry = self._entry(path, f"start|{ffmpeg}")
        if entry.exists():
            return json.loads(entry.read_text())["start_s"]
        start_s = self._container_start_s(path, ffmpeg, cancel_check=cancel_check, timeout_s=timeout_s)
        self._write(entry, {"start_s": start_s})
        return start_s

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
        count_boxes: Callable[[np.ndarray], list[int]],
        cancel_check: Callable[[], bool] | None = None,
        timeout_s: float = frames.DECODE_TIMEOUT_S,
        start_time_s: float | None = None,
    ) -> list[frames.Row]:
        """:func:`frames.decode_rows`, once per file identity, exact command, start time and text detection.

        Raises:
            frames.GpuDecodeError: This GPU decode failed, now or on an earlier run.
            Everything else :func:`frames.decode_rows` raises, uncached: a timeout or a cancel is no answer.
        """
        command, _ = frames.decode_command(
            ffmpeg, path, start_s=start_s, length_s=length_s, keyframes_only=keyframes_only, fps=fps, gpu=gpu,
            gpu_device_path=gpu_device_path,
        )  # fmt: skip
        if start_time_s is None:
            start_time_s = self.container_start_s(path, ffmpeg, cancel_check=cancel_check)
        entry = self._entry(path, f"rows|{self.counter}|{start_time_s!r}|{json.dumps(command)}")
        if entry.exists():
            stored = json.loads(entry.read_text())
            self.reused += 1
            if "gpu_error" in stored:
                raise frames.GpuDecodeError(stored["gpu_error"])
            return [(float(pts), int(boxes), float(luma)) for pts, boxes, luma in stored["rows"]]
        self.decoded += 1
        try:
            rows = self._decode_rows(
                path, ffmpeg=ffmpeg, start_s=start_s, length_s=length_s, keyframes_only=keyframes_only, fps=fps,
                gpu=gpu, gpu_device_path=gpu_device_path, count_boxes=count_boxes, cancel_check=cancel_check,
                timeout_s=timeout_s, start_time_s=start_time_s,
            )  # fmt: skip
        except frames.GpuDecodeError as exc:
            self._write(entry, {"gpu_error": str(exc)})
            raise
        self._write(entry, {"rows": [list(row) for row in rows]})
        return rows

    @contextlib.contextmanager
    def serving(self) -> Iterator[None]:
        """Answer the detector's decodes from this cache inside the block (``detector.find_credits`` reads both
        functions off the :mod:`frames` module at call time)."""
        saved = frames.decode_rows, frames.container_start_s
        frames.decode_rows, frames.container_start_s = self.decode_rows, self.container_start_s
        try:
            yield
        finally:
            frames.decode_rows, frames.container_start_s = saved
