"""The app's credit text answer (base tree 40311c3) for each of the 43 online cases' files, through the harness cache
(decodes reused when cached). Media is only read. Writes online_text.json {case key: [start_s, end_s]}."""

from paths import EVIDENCE_DIR, HERE, LOCAL, REPO  # noqa: F401
import json
import os
import sys
from pathlib import Path

import numpy as np

R = str(LOCAL)
sys.path.insert(0, f"{R}/base")
EVIDENCE = Path("/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence")
os.environ["MARKERS_EVAL_EVIDENCE"] = str(EVIDENCE)
from media_preview_generator.markers.credits.frames import FRAME_H, FRAME_W  # noqa: E402
from tools.markers_eval.cache import ProbeCache  # noqa: E402
from tools.markers_eval.credits_text import CreditsTextCache, _detection_on, decode_digest  # noqa: E402
from tools.markers_eval.decode_cache import DecodeCache  # noqa: E402
from tools.markers_eval.online import case_file, case_key, load_online  # noqa: E402
from tools.markers_eval.plex import load_baseline  # noqa: E402

root = Path.home() / ".cache/markers_eval"
probes = ProbeCache(root, ffprobe="/usr/bin/ffprobe")
baseline = load_baseline(EVIDENCE / "lab/results/scale/prod_plex_markers.json")
det = _detection_on("gpu", "cuda:0")
det.detect_boxes(np.zeros((1, FRAME_H, FRAME_W), dtype=np.uint8))
decodes = DecodeCache(root, digest=decode_digest(), backend=det.backend)
cache = CreditsTextCache(root, ffmpeg="/usr/bin/ffmpeg", decode="gpu", gpu_device="cuda:0",
                         detect_boxes=det.detect_boxes, backend=det.backend, probe=probes.probe,
                         decodes=decodes)  # fmt: skip
out = {}
try:
    results, _ = load_online(EVIDENCE)
    for r in results:
        path = case_file(r["case"], baseline.keys())
        if not path:
            continue
        a = cache.result(path, is_episode=True)
        out["|".join(map(str, case_key(r["case"])))] = [a["start_s"], a["end_s"]]
        print(r["case"]["show"], r["case"]["season"], r["case"]["episode"], a["start_s"], a["end_s"], flush=True)
finally:
    det.close()
json.dump(out, open(f"{R}/online_text.json", "w"), indent=0)
print("decoded", decodes.decoded, "reused", decodes.reused, "backend", det.backend())
