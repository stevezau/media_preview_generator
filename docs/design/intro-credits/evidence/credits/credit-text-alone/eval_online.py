import json, os, sys, collections
from pathlib import Path
import numpy as np
sys.path.insert(0, "/home/data/workspace/plex_generate_vid_previews"); sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tools.markers_eval.credits_text import CreditsTextCache, _detection_on, decode_digest, text_candidates, undecided_credits
from tools.markers_eval.cache import ProbeCache
from tools.markers_eval.data import evidence_dir
from tools.markers_eval.decode_cache import DecodeCache
from tools.markers_eval.plex import load_baseline, server_candidates, first_marker
from tools.markers_eval.online import load_online, case_file, case_key, online_verdicts, tally, judge_online, DEFAULT_ORDER, THEINTRODB_ORDER
from media_preview_generator.markers.credits.frames import FRAME_H, FRAME_W
from media_preview_generator.markers.models import MarkerType, Marker
import patch_rule
ev = evidence_dir(); root = Path(os.path.expanduser("~/.cache/markers_eval"))
probes = ProbeCache(root, ffprobe="/usr/bin/ffprobe")
baseline = load_baseline(ev / "lab/results/scale/prod_plex_markers.json")
det = _detection_on("gpu", "cuda:0"); det.detect_boxes(np.zeros((1, FRAME_H, FRAME_W), dtype=np.uint8))
decodes = DecodeCache(root, digest=decode_digest(), backend=det.backend)
cache = CreditsTextCache(root, ffmpeg="/usr/bin/ffmpeg", decode="gpu", gpu_device="cuda:0", detect_boxes=det.detect_boxes,
                         backend=det.backend, probe=probes.probe, decodes=decodes)
try:
    results, dump = load_online(ev)
    found = {case_key(r["case"]): case_file(r["case"], baseline.keys()) for r in results}
    servers = {k: server_candidates(baseline[p], MarkerType.CREDITS) for k, p in found.items() if p}
    texts = {k: text_candidates(*(lambda a: (a["start_s"], a["end_s"]))(cache.result(p, is_episode=True))) for k, p in found.items() if p}
finally:
    det.close()
plex = collections.Counter()
for r in results:
    k = case_key(r["case"]); m = first_marker(baseline.get(found.get(k) or "", []), MarkerType.CREDITS)
    if r["case"].get("credits_start") is None: continue
    plex[judge_online(MarkerType.CREDITS, Marker(MarkerType.CREDITS, m.start_ms, m.end_ms, ("x",)), r["case"]) if m else "missed"] += 1
print("cases", len(results), "files", sum(1 for p in found.values() if p), "Plex first credits marker:", dict(plex))
for label, order in (("Default sources, Medium", DEFAULT_ORDER), ("TheIntroDB on, Medium", THEINTRODB_ORDER)):
    for patched in (False, True):
        if patched: patch_rule.enable()
        before = online_verdicts(results, dump, order=order, level="medium", extra=servers)
        asked = undecided_credits(before)
        extra = {k: servers[k] + (texts[k] if k in asked else []) for k in servers}
        v = online_verdicts(results, dump, order=order, level="medium", extra=extra)
        patch_rule.disable()
        print(f"{label:24} {'proposed' if patched else 'current ':8} credits {dict(sorted(tally(v)['credits'].items()))} asked {len(asked & texts.keys())}")
print("decoded", decodes.decoded, "reused", decodes.reused)
