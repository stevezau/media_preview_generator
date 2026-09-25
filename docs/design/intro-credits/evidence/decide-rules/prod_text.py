"""The app's credit text answer (base tree 40311c3) for prod files named by a path substring (post.db), through the
harness cache. Media is only read. Writes prod_text.json {canonical_path: [start_s, end_s]} (merged with any earlier)."""

from paths import EVIDENCE_DIR, HERE, LOCAL, REPO  # noqa: F401
import json
import os
import sqlite3
import sys
from pathlib import Path

import numpy as np

R = str(LOCAL)
A = str(LOCAL)
sys.path.insert(0, f"{R}/base")
EVIDENCE = Path("/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence")
os.environ["MARKERS_EVAL_EVIDENCE"] = str(EVIDENCE)
from media_preview_generator.markers.credits.frames import FRAME_H, FRAME_W  # noqa: E402
from tools.markers_eval.cache import ProbeCache  # noqa: E402
from tools.markers_eval.credits_text import CreditsTextCache, _detection_on, decode_digest  # noqa: E402
from tools.markers_eval.decode_cache import DecodeCache  # noqa: E402

db = sqlite3.connect(f"file:{A}/post.db?mode=ro", uri=True)
paths = []
for like in sys.argv[1:]:
    paths += [p for (p,) in db.execute(
        "select canonical_path from files where missing_since is null and canonical_path like ? order by 1", (like,))]  # fmt: skip
root = Path.home() / ".cache/markers_eval"
probes = ProbeCache(root, ffprobe="/usr/bin/ffprobe")
det = _detection_on("gpu", "cuda:0")
det.detect_boxes(np.zeros((1, FRAME_H, FRAME_W), dtype=np.uint8))
decodes = DecodeCache(root, digest=decode_digest(), backend=det.backend)
cache = CreditsTextCache(root, ffmpeg="/usr/bin/ffmpeg", decode="gpu", gpu_device="cuda:0",
                         detect_boxes=det.detect_boxes, backend=det.backend, probe=probes.probe,
                         decodes=decodes)  # fmt: skip
out_path = Path(f"{R}/prod_text.json")
out = json.loads(out_path.read_text()) if out_path.exists() else {}
try:
    for path in paths:
        a = cache.result(path, is_episode="Season" in path)
        out[path] = [a["start_s"], a["end_s"]]
        print(os.path.basename(path)[:60], a["start_s"], a["end_s"], flush=True)
finally:
    det.close()
out_path.write_text(json.dumps(out, indent=0))
print("decoded", decodes.decoded, "reused", decodes.reused, "backend", det.backend())
