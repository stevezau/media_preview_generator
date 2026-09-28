"""Scratch: the work tree's find_credits on one target with every card and read printed (harness caches, GPU).

Usage: debug_walk.py <target key>...
"""

import json
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
tree = "/home/data/workspace/plex_generate_vid_previews"
os.environ.setdefault(
    "MEDIA_PREVIEW_TEXTDET_MODEL",
    "/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/card-read/local/models/ch_PP-OCRv4_det_infer.onnx",
)
os.environ.setdefault(
    "MEDIA_PREVIEW_TEXTREC_MODEL",
    "/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/card-read/local/models/latin_PP-OCRv5_rec_mobile.onnx",
)
sys.path.insert(0, tree)
os.chdir(tree)
from loguru import logger  # noqa: E402

logger.remove()
logger.add(sys.stderr, level="INFO")
import numpy as np  # noqa: E402

from media_preview_generator.markers.credits import cards, detector  # noqa: E402
from media_preview_generator.markers.probe import ffprobe_path_for  # noqa: E402
from tools.markers_eval.cache import ProbeCache  # noqa: E402
from tools.markers_eval.credits_text import _detection_on, _earliest_start_s  # noqa: E402
from tools.markers_eval.decode_cache import DecodeCache  # noqa: E402

T = json.load(open(HERE.parent / "targets.json"))
root = Path.home() / ".cache/markers_eval"
ffmpeg = shutil.which("ffmpeg")
probes = ProbeCache(root, ffprobe=ffprobe_path_for(ffmpeg))
detection = _detection_on("gpu", "cuda:0")
real_cards, real_past = cards.cards, cards.past_prose


def show_cards(rows, start_s):
    found = real_cards(rows, start_s)
    print("   cards from", start_s, [(c.first_s, c.last_s, c.read_s, c.dark) for c in found][:14], flush=True)
    return found


def past(first, rest, read):
    def loud_read(card):
        lines = read(card)
        print(f"   read {card.first_s}-{card.last_s} at {card.read_s} dark {card.dark} "
              f"{'PROSE' if cards.is_prose(lines) else '     '} | {' | '.join(lines)[:150]}", flush=True)  # fmt: skip
        return lines

    return real_past(first, rest, loud_read)


cards.cards, cards.past_prose = show_cards, past
try:
    detection.detect_boxes(np.zeros((1, 180, 320), dtype=np.uint8))
    decodes = DecodeCache(root, digest=open(HERE / "base_decode_digest.txt").read().strip(), backend=detection.backend)
    for key in sys.argv[1:]:
        if key in T:
            e = T[key][0]
        else:
            known = json.load(open(HERE / "ct_work.json"))
            e = {"path": next(p for p in known if key in os.path.basename(p)), "truth": None}
        path = e["path"]
        episode = "Accused" in path or " - S0" in path
        duration_ms = probes.probe(path).duration_ms
        print("==", key, "truth", e["truth"], flush=True)
        with decodes.serving():
            r = detector.find_credits(
                path, duration_ms=duration_ms, is_episode=episode, ffmpeg=ffmpeg, detect_boxes=detection.detect_boxes,
                gpu="NVIDIA", gpu_device_path="cuda:0",
                earliest_start_s=_earliest_start_s(duration_ms, is_episode=episode), read_text=detection.read_text,
            )  # fmt: skip
        print("  ->", r.start_s, "from", r.prose_start_s, "scale", r.scale, flush=True)
finally:
    detection.close()
