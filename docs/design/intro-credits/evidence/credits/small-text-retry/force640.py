"""Investigation only: what the 640x360 reading would answer on files the 320x180 reading answered (forced), with the
app's own _read_credits. Usage: force640.py FILES.tsv TRUTH.json"""

import functools
import json
import os
import sys

sys.path.insert(0, "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-a20e6dfd2dd11cf38")
from media_preview_generator.markers.credits import detector, frames, textdet, textdet_helper  # noqa: E402
from media_preview_generator.markers.decide import credits_limits_ms, earliest_credits_start_ms  # noqa: E402

truth = {k: v for k, v in json.load(open(sys.argv[2])).items() if not k.startswith("_")}
det = textdet.TextDetector(textdet.cpu_session(textdet_helper.model_path()), backend="cpu")
for line in open(sys.argv[1]):
    path, dur, episode, movie = line.rstrip("\n").split("\t")[:4]
    dur_ms, is_episode, is_movie = int(dur), episode == "1", movie == "1"
    window_ms, cap_ms = credits_limits_ms(is_episode=is_episode, tv_window_s=None, movie_window_s=None)
    earliest = earliest_credits_start_ms(dur_ms, is_movie=is_movie, credits_window_ms=window_ms,
                                         movie_credits_max_from_end_ms=cap_ms) / 1000.0  # fmt: skip
    start_time = frames.container_start_s(path, "ffmpeg")
    thinning = frames.keyframe_thinning(path, "ffmpeg")
    decode = {"ffmpeg": "ffmpeg", "gpu": "NVIDIA", "gpu_device_path": "cuda:0", "detect_boxes": det.detect,
              "cancel_check": None, "start_time_s": start_time, "download_format": thinning.download_format}  # fmt: skip
    tail_start = frames.tail_start_s(dur_ms, tail_s=frames.tail_length_s(is_episode=is_episode))
    read = functools.partial(detector._read_credits, path, duration_ms=dur_ms, tail_start=tail_start, decode=decode,
                             thinning=thinning, earliest_start_s=earliest, show=lambda _t: None)  # fmt: skip
    seen: list = []
    small = read(scale=1, seen=(), decoded=seen)
    large = read(scale=2, seen=seen, decoded=None)
    name = os.path.basename(path)
    t = truth.get(next((k for k in truth if k in name), ""))
    print(json.dumps({"file": name[40:52], "truth": t, "at_320": [small.start_s, small.end_s],
                      "at_640": [large.start_s, large.end_s]}), flush=True)  # fmt: skip
