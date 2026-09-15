"""The 289-frame text detection bench (spec §5.4): the vendored detector must find the same boxes as
rapidocr_onnxruntime 1.4.4 on every frame, both the count and the corners. Local-only data: the frames come from real
files and are never committed.

    python -m tools.markers_eval.textdet_bench extract [--out DIR]
    python -m tools.markers_eval.textdet_bench counts --impl rapidocr|vendored --model M [--frames F] --out counts.json
    python -m tools.markers_eval.textdet_bench compare A.json B.json

``extract`` repeats ``evidence/credits/gpu/extract.py``: every 10th file of ``credits/f3.jsonl`` (8 files), CPU
keyframes of the 120 s around the credits truth, scaled to 320×180 grey. ``counts`` records each frame's box count
and box corners; ``compare`` passes only when both match on all 289 frames. ``counts --impl rapidocr`` needs
rapidocr_onnxruntime 1.4.4 (Python ≤ 3.12: run it in Task 1's venv, ``$MARKERS_BENCH_DIR/py312``, with this repo on
``PYTHONPATH``). The frames live in ``$MARKERS_BENCH_DIR`` (default: the evidence folder's git-ignored ``credits/bench``),
a stable local path every lane and later session reads.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np

from .data import evidence_dir

W, H = 320, 180
EXPECTED_FRAMES = 289


def _default_dir() -> Path:
    root = Path(os.environ.get("MARKERS_BENCH_DIR") or evidence_dir() / "credits/bench")
    if str(root.resolve()).startswith("/data"):
        raise SystemExit("the bench folder must not live under /data*")
    return root


def extract(out: Path) -> Path:
    items = [json.loads(line) for line in (evidence_dir() / "credits/f3.jsonl").read_text().splitlines()][::10]
    frames = []
    for item in items:
        start = max(0.0, item["truth"] - 60)
        raw = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-threads", "2", "-skip_frame", "nokey", "-ss", f"{start:.2f}",
             "-t", "120", "-i", item["file"], "-an", "-sn", "-dn", "-fps_mode", "passthrough", "-vf",
             f"scale={W}:{H},format=gray", "-f", "rawvideo", "-"],
            capture_output=True, check=True,
        ).stdout  # fmt: skip
        frames.append(np.frombuffer(raw, np.uint8)[: len(raw) // (W * H) * W * H].reshape(-1, H, W))
    stacked = np.concatenate(frames)
    if len(stacked) != EXPECTED_FRAMES:
        raise SystemExit(f"extracted {len(stacked)} frames, the 2026-09-13 bench had {EXPECTED_FRAMES}")
    out.mkdir(parents=True, exist_ok=True)
    np.save(out / "frames.npy", stacked)
    return out / "frames.npy"


def detect(impl: str, model: str, frames_path: Path) -> list[list]:
    """Each frame's boxes as nested lists (boxes × 4 corners × [x, y]); both implementations give whole pixels."""
    images = [np.stack([frame] * 3, axis=-1) for frame in np.load(frames_path)]
    if impl == "rapidocr":
        from rapidocr_onnxruntime import RapidOCR

        det = RapidOCR(det_limit_side_len=320, det_limit_type="max", intra_op_num_threads=2, inter_op_num_threads=1,
                       det_model_path=model).text_det  # fmt: skip
        return [[] if (b := det(image)[0]) is None else np.asarray(b, dtype=np.float64).tolist() for image in images]
    from media_preview_generator.markers.credits import textdet

    detector = textdet.TextDetector(textdet.cpu_session(model), backend="cpu")
    return [detector.boxes(image).astype(np.float64).tolist() for image in images]


def compare(a: dict, b: dict) -> bool:
    """Print the frames whose box counts differ and whose box corners differ; True when neither does and both runs
    cover all EXPECTED_FRAMES frames."""
    counts = [i for i, (x, y) in enumerate(zip(a["counts"], b["counts"], strict=True)) if x != y]
    corners = [i for i, (x, y) in enumerate(zip(a["boxes"], b["boxes"], strict=True)) if x != y]
    total = len(a["counts"])
    print(f"{total - len(counts)} of {total} frames identical; differing frames: {counts}")
    print(f"{total - len(corners)} of {total} frames with identical box corners; differing frames: {corners}")
    return total == len(b["counts"]) == EXPECTED_FRAMES and not counts and not corners


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.markers_eval.textdet_bench")
    sub = parser.add_subparsers(dest="command", required=True)
    ex = sub.add_parser("extract")
    ex.add_argument("--out", type=Path, default=None)
    co = sub.add_parser("counts")
    co.add_argument("--impl", choices=("rapidocr", "vendored"), required=True)
    co.add_argument("--model", required=True)
    co.add_argument("--frames", type=Path, default=None)
    co.add_argument("--out", type=Path, required=True)
    cmp = sub.add_parser("compare")
    cmp.add_argument("a", type=Path)
    cmp.add_argument("b", type=Path)
    args = parser.parse_args(argv)
    out = getattr(args, "out", None)
    if out is not None and str(out.resolve()).startswith("/data"):
        raise SystemExit("bench output must not be written under /data*")
    if args.command == "extract":
        print(extract(args.out or _default_dir()))
        return 0
    if args.command == "counts":
        boxes = detect(args.impl, args.model, args.frames or _default_dir() / "frames.npy")
        result = {"impl": args.impl, "frames": len(boxes), "counts": [len(b) for b in boxes], "boxes": boxes}
        args.out.write_text(json.dumps(result))
        print(f"{args.impl}: {len(boxes)} frames, {sum(len(b) > 0 for b in boxes)} with text")
        return 0
    return 0 if compare(json.loads(args.a.read_text()), json.loads(args.b.read_text())) else 1


if __name__ == "__main__":
    sys.exit(main())
