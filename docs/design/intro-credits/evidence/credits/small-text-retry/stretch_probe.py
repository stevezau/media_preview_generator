"""Investigation: after a 320x180 answer, the stretch from the found run's last credit keyframe to the end of the file,
and what the 320x180 reading saw in it. Decodes through the harness's decode cache (v4 digest), GPU path, CPU text
detection. Usage: stretch_probe.py FILES.tsv OUT.jsonl"""

import functools
import json
import os
import shutil
import sys
from pathlib import Path

WT = "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-a20e6dfd2dd11cf38"
sys.path.insert(0, WT)
from media_preview_generator.markers.credits import detector, frames, rule_j, textdet, textdet_helper  # noqa: E402
from media_preview_generator.markers.decide import credits_limits_ms, earliest_credits_start_ms  # noqa: E402
from tools.markers_eval.credits_text import decode_digest  # noqa: E402
from tools.markers_eval.decode_cache import DecodeCache  # noqa: E402

FF = shutil.which("ffmpeg")
det = textdet.TextDetector(textdet.cpu_session(textdet_helper.model_path()), backend="cpu")
cache = DecodeCache(Path.home() / ".cache/markers_eval", digest=decode_digest(), backend=lambda: "cpu")
out = open(sys.argv[2], "w")
with cache.serving():
    for line in open(sys.argv[1]):
        path, dur, episode, movie = line.rstrip("\n").split("\t")[:4]
        if not os.path.exists(path):
            continue
        dur_ms, is_episode, is_movie = int(dur), episode == "1", movie == "1"
        window_ms, cap_ms = credits_limits_ms(is_episode=is_episode, tv_window_s=None, movie_window_s=None)
        earliest = earliest_credits_start_ms(dur_ms, is_movie=is_movie, credits_window_ms=window_ms,
                                             movie_credits_max_from_end_ms=cap_ms) / 1000.0  # fmt: skip
        start_time = frames.container_start_s(path, FF)
        thinning = frames.keyframe_thinning(path, FF)
        decode = {"ffmpeg": FF, "gpu": "NVIDIA", "gpu_device_path": "cuda:0", "detect_boxes": det.detect,
                  "cancel_check": None, "start_time_s": start_time, "download_format": thinning.download_format}  # fmt: skip
        tail_start = frames.tail_start_s(dur_ms, tail_s=frames.tail_length_s(is_episode=is_episode))
        try:
            found = detector._read_credits(path, duration_ms=dur_ms, tail_start=tail_start, decode=decode,
                                           thinning=thinning, earliest_start_s=earliest, show=lambda _t: None,
                                           scale=1, seen=(), decoded=None)  # fmt: skip
        except frames.GpuDecodeError:
            continue
        row = {"file": path, "dur": dur_ms / 1000.0, "start": found.start_s, "end": found.end_s}
        if found.start_s is not None:
            key = list(found.key_rows)
            own = rule_j.without_overlays(key, found.overlays)
            coarse = rule_j.coarse_start(key, without=own)
            run_end = rule_j.coarse_end_s(own, coarse)
            after = [r for r in own if r[0] > run_end]
            row.update(run_end=run_end, stretch=round(dur_ms / 1000.0 - run_end, 1),
                       text_frames=sum(1 for r in after if r[1] > 0), boxes=sum(r[1] for r in after),
                       credit_frames=sum(1 for r in after if rule_j.is_credit(r)), frames=len(after))  # fmt: skip
        out.write(json.dumps(row) + "\n")
        out.flush()
        print(json.dumps({k: v for k, v in row.items() if k != "file"}), os.path.basename(path)[:50], flush=True)
