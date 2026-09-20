"""Rebuild ``tests/fixtures/markers/credits_synth_lab.json.gz`` from the lab's synthetic files (run on storage):

    MEDIA_PREVIEW_TEXTDET_MODEL=... python -m tools.markers_eval.credits_synth_fixture [--decode gpu|cpu]

The rows are the app's own decode of ``evidence/lab/synth`` (git-ignored; set ``MARKERS_EVAL_EVIDENCE`` from a
worktree), read through ``detector.find_credits`` with ``rule_j.text_all_through`` patched off, which is the view rule
J version 1 had when the fixture's ``version_1_start_s`` was measured.

Every file is decoded again and checked against the fixture it replaces: each row's time, box count and mean luma must
match row for row, or the build stops. The pinned answers were measured on exactly these rows; only the boxes'
positions are new (``rule_j.Row``), and they come from the same text detection call the counts came from.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from pathlib import Path

import numpy as np

from media_preview_generator.markers.credits import frames, rule_j
from media_preview_generator.markers.credits.detector import find_credits

from .cache import ProbeCache
from .credits_text import _detection_on
from .data import evidence_dir
from .decode_cache import rows_to_json

OUT = Path(__file__).resolve().parents[2] / "tests/fixtures/markers/credits_synth_lab.json.gz"
SYNTH = "lab/synth"
SKIP_FOLDERS = ("_staging", "_backup")
ABOUT = (
    "The lab's synthetic files (evidence/lab/synth), keyframe rows from the app's own GPU decode and text detection: "
    "Synth Audio (VP9) has a test pattern with a burnt-in running timecode and no credits, its keyframe pass dropping "
    "the packets that aren't keyframes before the decoder (frames.keyframe_thinning); the Synth Credits files (H.264) "
    "have a synthetic roll. Each row is [pts, box count, luma, [[left, top, right, bottom], ...]] in the frame's own "
    "320x180 pixels. fine and version_1_start_s are rule J version 1's 1 fps refine rows and answer on these rows. "
    "Rebuilt by tools.markers_eval.credits_synth_fixture."
)


def media_for(name: str, root: Path) -> Path:
    """The lab file an item was measured on.

    Args:
        name: The item's name (the file's own stem).
        root: The evidence folder.

    Returns:
        Its path.

    Raises:
        FileNotFoundError: No such file, or more than one.
    """
    found = [
        path
        for path in (root / SYNTH).rglob(f"{name}.*")
        if path.suffix in (".mkv", ".mp4", ".webm") and not any(part in SKIP_FOLDERS for part in path.parts)
    ]
    if len(found) != 1:
        raise FileNotFoundError(f"{name}: {len(found)} files under {root / SYNTH}")
    return found[0]


def rows_of(item: dict, path: Path, *, decode: str, detect_boxes, probe) -> tuple[list, list]:
    """The file's keyframe and refine rows, as rule J version 1 saw them.

    Args:
        item: The stored item (for its name).
        path: The media file (only read).
        decode: ``gpu`` or ``cpu``.
        detect_boxes: The app's own text detector for this decode path.
        probe: A file's duration.

    Returns:
        The keyframe rows and the 1 fps refine rows.
    """
    guard = rule_j.text_all_through
    rule_j.text_all_through = lambda rows, coarse: False
    try:
        gpu = "NVIDIA" if decode == "gpu" else None
        found = find_credits(
            str(path),
            duration_ms=probe(str(path)).duration_ms,
            is_episode=" - S" in item["name"],
            ffmpeg="ffmpeg",
            detect_boxes=detect_boxes,
            gpu=gpu,
            gpu_device_path="cuda:0" if gpu else None,
        )
    finally:
        rule_j.text_all_through = guard
    return list(found.key_rows), list(found.fine_rows)


def same_measurements(stored: list, fresh: list) -> bool:
    """Whether the rows agree on everything the fixture pinned: time, box count and mean luma, row for row."""
    return len(stored) == len(fresh) and all(
        (float(old[0]), int(old[1]), float(old[2])) == (row[0], row[1], row[2])
        for old, row in zip(stored, fresh, strict=True)
    )


def build(decode: str, evidence: Path) -> dict:
    """The fixture, rebuilt from the lab files.

    Args:
        decode: ``gpu`` or ``cpu``.
        evidence: The evidence folder.

    Returns:
        The new fixture.

    Raises:
        SystemExit: A file's rows no longer match the fixture's.
    """
    old = json.loads(gzip.decompress(OUT.read_bytes()))
    detection = _detection_on(decode, "cuda:0")
    probes = ProbeCache(
        Path(os.environ.get("MARKERS_EVAL_CACHE", Path.home() / ".cache/markers_eval")), ffprobe="ffprobe"
    )
    items = []
    try:
        # One blank frame starts the helper and its self-test before the first file, as the harness does.
        detection.detect_boxes(np.zeros((1, frames.FRAME_H, frames.FRAME_W), dtype=np.uint8))
        print(f"text detection on {detection.backend()}", flush=True)
        for item in old["items"]:
            path = media_for(item["name"], evidence)
            key, fine = rows_of(item, path, decode=decode, detect_boxes=detection.detect_boxes, probe=probes.probe)
            for what, stored, fresh in (("key", item["key"], key), ("fine", item["fine"], fine)):
                if not same_measurements(stored, fresh):
                    sys.exit(f"{item['name']}: the {what} rows are no longer the fixture's ({len(stored)} stored)")
            items.append({**item, "key": rows_to_json(key), "fine": rows_to_json(fine)})
            print(f"{item['name']}: {len(key)} keyframe rows, {len(fine)} refine rows", flush=True)
    finally:
        detection.close()
    return {"about": ABOUT, "items": items}


def main(argv: list[str] | None = None) -> int:
    """Rebuild the fixture in place."""
    parser = argparse.ArgumentParser(prog="python -m tools.markers_eval.credits_synth_fixture")
    parser.add_argument("--decode", choices=("gpu", "cpu"), default="gpu")
    args = parser.parse_args(argv)
    data = json.dumps(build(args.decode, evidence_dir()), separators=(",", ":")).encode()
    OUT.write_bytes(gzip.compress(data, mtime=0))
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
