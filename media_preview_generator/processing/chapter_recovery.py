"""Bounded seek-index checks and source-bound recovery for chapter thumbnails."""

from __future__ import annotations

import bisect
import io
import json
import os
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image

from ..bif_reader import BIF_MAGIC
from ..output.journal import outputs_fresh_for_source
from ..output.plex_hash import SourceFileChangedError, SourceFingerprint, get_source_fingerprint

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from ..servers.plex_chapters import Chapter
    from .chapters import ChapterPlan

_MAX_INDEX_BYTES = 4 * 1024 * 1024
_MAX_FRAMES = 200_000
_MAX_JPEG_BYTES = 8 * 1024 * 1024
_MAX_GAP_MS = 5000


def _vint(data: bytes, at: int, *, element_id: bool = False) -> tuple[int, int]:
    first = data[at]
    length = 1
    while length <= 8 and not first & (1 << (8 - length)):
        length += 1
    if length > 8 or at + length > len(data):
        raise ValueError("Invalid EBML integer")
    value = int.from_bytes(data[at : at + length], "big")
    if not element_id:
        value &= (1 << (7 * length)) - 1
        if value == (1 << (7 * length)) - 1:
            raise ValueError("Unknown EBML size")
    return value, at + length


def _elements(data: bytes) -> Iterator[tuple[int, bytes]]:
    at = 0
    while at < len(data):
        element, start = _vint(data, at, element_id=True)
        size, start = _vint(data, start)
        end = start + size
        if end > len(data):
            raise ValueError("Incomplete EBML element")
        yield element, data[start:end]
        at = end


def _inspect_index(path: str, expected: SourceFingerprint, starts: list[int]) -> str | None:
    """Inspect only bounded headers/index bytes; uncertain formats are not corruption."""
    if get_source_fingerprint(path) != expected:
        raise SourceFileChangedError("Source changed during seek-index inspection")
    result = None
    with open(path, "rb") as source:
        size = os.fstat(source.fileno()).st_size

        def header(offset: int) -> tuple[int, int, int]:
            source.seek(offset)
            data = source.read(12)
            element, at = _vint(data, 0, element_id=True)
            length, at = _vint(data, at)
            return element, offset + at, length

        def payload(offset: int, expected_id: int, limit: int) -> bytes:
            element, at, length = header(offset)
            if element != expected_id or length > limit or at + length > size:
                raise ValueError("Unsupported or incomplete index")
            source.seek(at)
            return source.read(length)

        first, start, length = header(0)
        if first != 0x1A45DFA3:
            return None
        element, segment, _ = header(start + length)
        if element != 0x18538067:
            return None
        offset = segment
        seeks: dict[int, int] = {}
        for _ in range(64):
            element, at, length = header(offset)
            if element == 0x114D9B74:
                for eid, entry in _elements(payload(offset, element, 65536)):
                    if eid == 0x4DBB:
                        fields = dict(_elements(entry))
                        seeks[int.from_bytes(fields[0x53AB], "big")] = segment + int.from_bytes(fields[0x53AC], "big")
            if element == 0x1F43B675:
                break
            offset = at + length
            if offset >= size or offset - segment > 65536:
                break
        cues_at = seeks.get(0x1C53BB6B)
        if cues_at is None:
            return None
        source.seek(cues_at)
        if source.read(4) != bytes.fromhex("1c53bb6b"):
            result = "Matroska seek index points to invalid Cues data"
        else:
            scale = 1_000_000
            info_at = seeks.get(0x1549A966)
            if info_at is not None:
                info = dict(_elements(payload(info_at, 0x1549A966, 65536)))
                scale = int.from_bytes(info.get(0x2AD7B1, b"\x0f\x42\x40"), "big")
            tracks_at = seeks.get(0x1654AE6B)
            if tracks_at is None or not scale:
                return None
            video_track = None
            for eid, entry in _elements(payload(tracks_at, 0x1654AE6B, 65536)):
                if eid == 0xAE:
                    fields = dict(_elements(entry))
                    if int.from_bytes(fields.get(0x83, b""), "big") == 1:
                        video_track = int.from_bytes(fields[0xD7], "big")
                        break
            points = []
            for eid, entry in _elements(payload(cues_at, 0x1C53BB6B, _MAX_INDEX_BYTES)):
                if eid != 0xBB:
                    continue
                fields = list(_elements(entry))
                timestamp = next(
                    int.from_bytes(value, "big") * scale / 1_000_000 for key, value in fields if key == 0xB3
                )
                for key, value in fields:
                    if key == 0xB7:
                        position = dict(_elements(value))
                        if int.from_bytes(position.get(0xF7, b""), "big") == video_track:
                            points.append((timestamp, segment + int.from_bytes(position[0xF1], "big")))
                if len(points) > _MAX_FRAMES:
                    return None
            if not points or any(b[0] < a[0] for a, b in zip(points, points[1:], strict=False)):
                return None
            timestamps = [point[0] for point in points]
            offsets = {points[max(0, bisect.bisect_right(timestamps, stamp) - 1)][1] for stamp in starts}
            if len(offsets) > 256:
                return None
            for offset in offsets:
                source.seek(offset)
                if source.read(4) != bytes.fromhex("1f43b675"):
                    result = "Matroska seek index points to invalid video Cluster data"
                    break
    if get_source_fingerprint(path) != expected:
        raise SourceFileChangedError("Source changed during seek-index inspection")
    return result


def inspect_seek_index(
    path: str,
    expected_fingerprint: SourceFingerprint,
    chapter_starts_ms: list[int],
    *,
    cancel_check: Callable[[], bool] | None = None,
    pause_check: Callable[[], bool] | None = None,
) -> str | None:
    """Check an index in a disposable process; a five-second timeout proves nothing.

    Args:
        path: Current local source.
        expected_fingerprint: Source identity captured by chapter planning.
        chapter_starts_ms: Missing chapter timestamps whose seek targets matter.
        cancel_check: Cooperative job cancellation callback.
        pause_check: Cooperative job pause callback.

    Returns:
        A proven structural defect, or None for a valid or unclassified index.
    """
    from .generator import CancellationError

    if get_source_fingerprint(path) != expected_fingerprint:
        raise SourceFileChangedError("Source changed during seek-index inspection")
    if cancel_check and cancel_check():
        raise CancellationError("Chapter inspection cancelled")
    request = json.dumps([path, expected_fingerprint, chapter_starts_ms[:4096]])
    if len(request.encode()) > 100_000:
        return None
    proc = subprocess.Popen(
        [sys.executable, "-m", __name__, request],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5
        while proc.poll() is None:
            if cancel_check and cancel_check():
                raise CancellationError("Chapter inspection cancelled")
            if pause_check and pause_check():
                return None
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.02)
        result = json.loads(proc.stdout.read(4096)) if proc.returncode == 0 else None
        if get_source_fingerprint(path) != expected_fingerprint:
            raise SourceFileChangedError("Source changed during seek-index inspection")
        return result if isinstance(result, str) else None
    except SourceFileChangedError:
        raise
    except (OSError, ValueError):
        return None
    finally:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            threading.Thread(target=proc.wait, name=f"chapter-inspect-reap-{proc.pid}", daemon=True).start()
        if proc.stdout:
            proc.stdout.close()


def recover_bif_frame(plan: ChapterPlan, chapter: Chapter, output: Path) -> dict | None:
    """Recover only an in-chapter JPEG whose journal binds it to this exact source.

    Returns:
        Explicit original resolution/timestamp provenance, or None if no safe frame exists.
    """
    bif = plan.folder.parent / "Indexes" / "index-sd.bif"
    if get_source_fingerprint(plan.canonical_path) != plan.source_fingerprint:
        raise SourceFileChangedError("Source changed during chapter recovery")
    if not outputs_fresh_for_source([bif], plan.canonical_path, require_source_fingerprint=True):
        return None
    try:
        before = get_source_fingerprint(bif)
        with bif.open("rb") as stream:
            header = stream.read(64)
            if len(header) != 64 or header[:8] != BIF_MAGIC:
                return None
            version, count, multiplier = struct.unpack_from("<III", header, 8)
            table_end = 64 + (count + 1) * 8
            if version != 0 or not 0 < count <= _MAX_FRAMES or table_end > before[2]:
                return None
            table = stream.read((count + 1) * 8)
            if len(table) != (count + 1) * 8:
                return None
            entries = list(struct.iter_unpack("<II", table))
            if entries[-1][0] != 0xFFFFFFFF or entries[-1][1] != before[2]:
                return None
            if any(
                not table_end <= a[1] < b[1] <= before[2] or (i < count - 1 and a[0] >= b[0])
                for i, (a, b) in enumerate(zip(entries, entries[1:], strict=False))
            ):
                return None
            timestamps = [entry[0] * (multiplier or 1000) for entry in entries[:-1]]
            index = bisect.bisect_left(timestamps, chapter.start_ms)
            if index >= count:
                return None
            timestamp = timestamps[index]
            if timestamp >= chapter.end_ms or timestamp - chapter.start_ms > _MAX_GAP_MS:
                return None
            offset = entries[index][1]
            length = entries[index + 1][1] - offset
            if length > _MAX_JPEG_BYTES:
                return None
            stream.seek(offset)
            data = stream.read(length)
            with Image.open(io.BytesIO(data)) as image:
                if image.format != "JPEG" or image.width * image.height > 16_000_000:
                    return None
                image.load()
                dimensions = [image.width, image.height]
                height = max(2, round(image.height * 1280 / image.width / 2) * 2)
                if height > 8192 or 1280 * height > 16_000_000:
                    return None
                image.convert("RGB").resize((1280, height), Image.Resampling.LANCZOS).save(output, "JPEG", quality=90)
        if get_source_fingerprint(bif) != before:
            output.unlink(missing_ok=True)
            return None
        if get_source_fingerprint(plan.canonical_path) != plan.source_fingerprint:
            raise SourceFileChangedError("Source changed during chapter recovery")
        return {"source": "bif", "timestamp_ms": timestamp, "original_dimensions": dimensions}
    except SourceFileChangedError:
        raise
    except (OSError, ValueError, Image.DecompressionBombError):
        return None


if __name__ == "__main__":
    try:
        _path, _expected, _starts = json.loads(sys.argv[1])
        _result = _inspect_index(_path, tuple(_expected), _starts)
    except (OSError, ValueError, KeyError, IndexError, StopIteration):
        _result = None
    print(json.dumps(_result))
