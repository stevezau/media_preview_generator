"""The app's credit text answer, with its rows, for the files this lane measures, from one tree, through the harness's
caches (GPU decode on cuda:0, the worker's CPU rerun; run it under nice).

Sets: targets (the lane's named files), 80 (movies40 + tv40), items (the audit's verdict and Plex files and the chapter
files its replay reads), 205, accused, isurvived. Files already in the output are skipped (resume).

Usage: ctrun.py <tree> <out.json> <set,set,...> [--decode-digest D]

--decode-digest keys the decode cache with another tree's digest: only for a tree whose changes leave every decode's
rows as they were (new functions beside decode_rows, the helper's read requests), so its run reuses the base's decodes.
"""

import json
import os
import shutil
import sys
import time
from pathlib import Path

tree = sys.argv[1]
out_path = Path(sys.argv[2]).resolve()
wanted = sys.argv[3].split(",")
HERE = Path(__file__).resolve().parent
EVIDENCE = "/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence"
os.environ.setdefault("MARKERS_EVAL_EVIDENCE", EVIDENCE)
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
import numpy as np  # noqa: E402

import media_preview_generator  # noqa: E402

assert media_preview_generator.__file__.startswith(tree), media_preview_generator.__file__
from media_preview_generator.markers.credits.frames import FRAME_H, FRAME_W  # noqa: E402
from media_preview_generator.markers.probe import ffprobe_path_for  # noqa: E402
from tools.markers_eval.cache import ProbeCache  # noqa: E402
from tools.markers_eval.credits_text import (  # noqa: E402
    REGRESSION_SETS,
    CreditsTextCache,
    _detection_on,
    _truth,
    decode_digest,
    on_disk,
)
from tools.markers_eval.decode_cache import DecodeCache  # noqa: E402


def load(name: str):
    return json.loads(Path(EVIDENCE, "credits", f"{name}.json").read_text())


files: list[tuple[str, str, float | None, bool]] = []
if "targets" in wanted:
    for key, entries in json.load(open(HERE.parent / "targets.json")).items():
        for e in entries:
            files.append((f"target:{key}", e["path"], e["truth"], "Accused" in e["path"]))
if "80" in wanted or "205" in wanted:
    adjudicated = load("adjudicated")
    names = (("movies40", False), ("tv40", True)) if "80" in wanted else ()
    names += (("movie_credit_truth", False),) if "205" in wanted else ()
    for name, episode in names:
        for f in load(name):
            files.append((name, f["file"], _truth(f, adjudicated), episode))
if "items" in wanted:
    items = json.load(open(HERE / "items.json"))
    for it in items["verdict"] + items["plex"]:
        if it["type"] == "credits" and it["on_disk"]:
            files.append(("items", it["path"], None, it["episode"]))
    for path, episode in json.load(open(HERE / "extra_files.json")).items():
        files.append(("items", path, None, episode))
for name, rel in REGRESSION_SETS.items():
    if name in wanted:
        for path, start in json.loads(Path(EVIDENCE, rel).read_text()).items():
            if not path.startswith("_"):
                files.append((name, path, float(start), True))

root = Path(os.environ.get("MARKERS_EVAL_CACHE") or Path.home() / ".cache/markers_eval")
ffmpeg = shutil.which("ffmpeg")
probes = ProbeCache(root, ffprobe=ffprobe_path_for(ffmpeg))
detection = _detection_on("gpu", "cuda:0")
out = json.load(open(out_path)) if out_path.exists() else {}
digest = sys.argv[sys.argv.index("--decode-digest") + 1] if "--decode-digest" in sys.argv else decode_digest()
reader = getattr(detection, "read_text", None)
try:
    detection.detect_boxes(np.zeros((1, FRAME_H, FRAME_W), dtype=np.uint8))
    decodes = DecodeCache(root, digest=digest, backend=detection.backend)
    extra = {"read_text": reader} if reader is not None else {}
    cache = CreditsTextCache(root, ffmpeg=ffmpeg, decode="gpu", gpu_device="cuda:0",
                             detect_boxes=detection.detect_boxes, backend=detection.backend, probe=probes.probe,
                             decodes=decodes, **extra)  # fmt: skip
    print("tree", tree, "decode digest", digest, "own", decode_digest(), "backend", detection.backend(),
          "reads cards", reader is not None, flush=True)  # fmt: skip
    for n, (name, path, truth, episode) in enumerate(files, 1):
        if path in out or not on_disk(path):
            continue
        t0 = time.monotonic()
        try:
            r = cache.result(path, is_episode=episode)
        except Exception as exc:  # noqa: BLE001 - scratch runner: record and go on
            out[path] = {"set": name, "truth": truth, "episode": episode, "error": f"{type(exc).__name__}: {exc}"}
            print(n, len(files), "ERROR", os.path.basename(path)[:60], exc, flush=True)
            continue
        out[path] = {"set": name, "truth": truth, "episode": episode, "duration": probes.probe(path).duration_ms,
                     "took_s": round(time.monotonic() - t0, 2), **r}  # fmt: skip
        moved = r.get("prose_start_s")
        print(n, len(files), name, f"{time.monotonic() - t0:.1f}s", r["start_s"], "" if moved is None else
              f"(moved from {moved})", os.path.basename(path)[:60], flush=True)  # fmt: skip
        if n % 20 == 0:
            json.dump(out, open(out_path, "w"))
    print("files", len(out), "decodes", decodes.decoded, "reused", decodes.reused, flush=True)
finally:
    detection.close()
json.dump(out, open(out_path, "w"))
