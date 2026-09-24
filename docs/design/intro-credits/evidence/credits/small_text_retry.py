"""The 640x360 read of a tail with no answer at 320x180 (``detector.RETRY_SCALE``), on real files: the app's own
``find_credits`` per file, before (the 320x180 reading alone) and after, judged against frame-checked truth.

One run gives both: ``find_credits`` returns the 320x180 reading whenever it answers, so a result read at scale 1 is
the build before's answer too, and one read at scale 2 is a file the build before had no answer for. Text detection
runs on the CPU in this process (the self-tested GPU path gives identical boxes; small-text-retry.md), decode on the
chosen path. Rows are kept per file for sheets and sims.

Usage (storage, one heavy job at a time)::

    MEDIA_PREVIEW_TEXTDET_MODEL=... nice -n 19 python small_text_retry.py FILES.tsv TRUTH.json OUT.jsonl --decode gpu

``FILES.tsv``: path, duration_ms, is_episode (1/0), is_movie (1/0) -- local-only, it names library files.
``TRUTH.json``: ``{"<substring of the file name>": first credit card in seconds}``, local-only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[5]))

from media_preview_generator.markers.credits import detector, textdet, textdet_helper  # noqa: E402
from media_preview_generator.markers.decide import credits_limits_ms, earliest_credits_start_ms  # noqa: E402
from tools.markers_eval.credits import judge_credits  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("files")
    parser.add_argument("truth")
    parser.add_argument("out")
    parser.add_argument("--decode", choices=("gpu", "cpu"), default="gpu")
    args = parser.parse_args()
    truth = {k: v for k, v in json.loads(Path(args.truth).read_text()).items() if not k.startswith("_")}
    det = textdet.TextDetector(textdet.cpu_session(textdet_helper.model_path()), backend="cpu")
    gpu = "NVIDIA" if args.decode == "gpu" else None
    tally: dict[str, dict[str, int]] = {"before": {}, "after": {}}
    with open(args.out, "a") as out:
        for line in open(args.files):
            path, dur, episode, movie = line.rstrip("\n").split("\t")[:4]
            if not os.path.exists(path):
                # Sonarr/Radarr replace and delete files between runs; a file gone from disk has nothing to read.
                print(f"{os.path.basename(path)[:60]:60s} gone from disk, skipped", flush=True)
                continue
            dur_ms, is_episode, is_movie = int(dur), episode == "1", movie == "1"
            window_ms, cap_ms = credits_limits_ms(is_episode=is_episode, tv_window_s=None, movie_window_s=None)
            earliest = earliest_credits_start_ms(dur_ms, is_movie=is_movie, credits_window_ms=window_ms,
                                                 movie_credits_max_from_end_ms=cap_ms) / 1000.0  # fmt: skip
            frames_by_size: dict[int, int] = {}
            detect_s = [0.0]

            def detect(planes, frames_by_size=frames_by_size, detect_s=detect_s):
                started = time.monotonic()
                boxes = det.detect(planes)
                detect_s[0] += time.monotonic() - started
                frames_by_size[planes.shape[2]] = frames_by_size.get(planes.shape[2], 0) + len(planes)
                return boxes

            started = time.monotonic()
            result = detector.find_credits(path, duration_ms=dur_ms, is_episode=is_episode, ffmpeg="ffmpeg",
                                           detect_boxes=detect, gpu=gpu, gpu_device_path="cuda:0" if gpu else None,
                                           earliest_start_s=earliest)  # fmt: skip
            name = os.path.basename(path)
            key = next((k for k in truth if k in name), None)
            before = result.start_s if result.scale == 1 else None
            row = {"file": name, "truth_s": truth.get(key), "before_s": before, "after_s": result.start_s,
                   "end_s": result.end_s, "scale": result.scale, "secs": round(time.monotonic() - started, 1),
                   "detect_s": round(detect_s[0], 1), "frames": frames_by_size,
                   "key": [[r[0], r[1], r[2], [list(b) for b in r[3]]] for r in result.key_rows]}  # fmt: skip
            if key is not None:
                row["verdict_before"] = judge_credits(before, truth[key])
                row["verdict_after"] = judge_credits(result.start_s, truth[key])
                for which in ("before", "after"):
                    verdict = row[f"verdict_{which}"]
                    tally[which][verdict] = tally[which].get(verdict, 0) + 1
            out.write(json.dumps(row) + "\n")
            out.flush()
            print(f"{name[:60]:60s} truth={row['truth_s']} before={before} after={result.start_s} "
                  f"scale={result.scale} {row['secs']} s ({frames_by_size})", flush=True)  # fmt: skip
    print(json.dumps(tally))


if __name__ == "__main__":
    main()
