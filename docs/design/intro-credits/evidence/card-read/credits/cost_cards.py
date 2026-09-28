"""Scratch: the CPU cost of reading the card at the credits start, per file: find_credits on a CPU worker (in-process
models, ffmpeg on the CPU, no caches) without and with the reader. Read-only on media.

Usage: cost_cards.py "<name substring>" ...
"""

import json
import os
import resource
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
tree = "/home/data/workspace/plex_generate_vid_previews"
sys.path.insert(0, tree)
from loguru import logger  # noqa: E402

logger.remove()
from media_preview_generator.markers.credits import detector, textdet, textrec  # noqa: E402
from media_preview_generator.markers.probe import ffprobe_path_for, probe_media  # noqa: E402

MODELS = "/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/card-read/local/models"
det = textdet.TextDetector(textdet.cpu_session(f"{MODELS}/ch_PP-OCRv4_det_infer.onnx"), backend="cpu")
reader = textrec.TextReader(det, textrec.cpu_session(f"{MODELS}/latin_PP-OCRv5_rec_mobile.onnx", 4))
known = json.load(open(HERE / "ct_base.json"))
ffmpeg = shutil.which("ffmpeg")
reads = []


def read_text(planes):
    t = time.process_time()
    out = reader.read(planes)
    reads.append(time.process_time() - t)
    return out


def cpu() -> float:
    s, c = resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)
    return s.ru_utime + s.ru_stime + c.ru_utime + c.ru_stime


for name in sys.argv[1:]:
    path = next(p for p in known if name in os.path.basename(p))
    episode = " - S0" in path or "Accused" in path
    duration_ms = probe_media(path, ffprobe=ffprobe_path_for(ffmpeg)).duration_ms
    got = {}
    for label, reading in (("without", None), ("with", read_text)):
        reads.clear()
        before, wall = cpu(), time.monotonic()
        r = detector.find_credits(path, duration_ms=duration_ms, is_episode=episode, ffmpeg=ffmpeg,
                                  detect_boxes=det.detect, gpu=None, gpu_device_path=None,
                                  read_text=reading)  # fmt: skip
        got[label] = (r.start_s, cpu() - before, time.monotonic() - wall, len(reads), sum(reads))
    (s0, c0, w0, _, _), (s1, c1, w1, n, rc) = got["without"], got["with"]
    print(f"{name[:30]:30} start {s0} -> {s1}  CPU {c0:.1f} -> {c1:.1f} s (+{c1 - c0:.1f})  wall {w0:.1f} -> {w1:.1f} s"
          f"  {n} reads, {rc:.1f} s of it reading")  # fmt: skip
