"""Intro matching when a season has few episodes (weekly releases).

For every chapter-truth episode, compare three setups (v3 matcher, alg1 stereo fingerprints):
  season_all  : all eval siblings from the same season (baseline)
  one_sibling : only one other episode of the same season (the E02-just-arrived case)
  prev_season : no same-season siblings; up to 4 episodes from the previous season folder
"""

import functools
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

LAB = "/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence"
sys.path.insert(0, f"{LAB}/detect")
sys.path.insert(0, f"{LAB}/eval")
import fp  # noqa: E402
import fp3  # noqa: E402

fp.duration_s = functools.lru_cache(None)(fp.duration_s)
import hashlib  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402

import numpy as np  # noqa: E402

CACHE = f"{LAB}/detect/fpcache_var"


def make_fp(opts):
    # Same cache key as fp_variants.py so already-computed fingerprints are reused.
    def fingerprint(path, start, length):
        k = hashlib.sha1(f"{path}|{start}|{length}|{opts}".encode()).hexdigest()
        pth = os.path.join(CACHE, k + ".npy")
        if os.path.exists(pth):
            return np.load(pth)
        cmd = ["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-i", path, "-t", f"{length:.3f}", "-vn", "-sn", "-dn",
               *opts, "-fp_format", "raw", "-"]
        a = np.frombuffer(subprocess.run(cmd, capture_output=True).stdout, dtype="<u4")
        np.save(pth, a)
        return a

    return fingerprint


fp.POINT_S = 4096 / 11025 / 3
fp3.cached_fp = make_fp(["-ac", "2", "-f", "chromaprint", "-algorithm", "1"])

R = json.load(open(f"{LAB}/eval/eval_results_v3.json"))
seasons = defaultdict(list)
truth = {}
for r in R:
    seasons[r["season"]].append(r["file"])
    truth[r["file"]] = r["truth"].get("intro")

VIDEO = {".mkv", ".mp4", ".avi", ".m4v", ".ts"}


def prev_season_files(season_dir: str) -> list[str]:
    m = re.search(r"^/data_16tb\d?/(TV Shows/.+)/Season (\d+)$", season_dir)
    if not m or int(m.group(2)) <= 1:
        return []
    rel = f"{m.group(1)}/Season {int(m.group(2)) - 1:02d}"
    for tier in ("/data_16tb", "/data_16tb2", "/data_16tb3", "/data_28tb"):
        d = Path(tier) / rel
        if d.is_dir():
            files = sorted(str(p) for p in d.iterdir() if p.suffix.lower() in VIDEO)
            if files:
                return files[:4]
    return []


def judge(seg, t):
    if not seg:
        return "missed"
    return "useful" if abs(seg[1] - t[1]) <= 5 and abs(seg[0] - t[0]) <= 15 else "wrong"


tally = {k: defaultdict(int) for k in ("season_all", "one_sibling", "prev_season")}
for sn, files in seasons.items():
    base = fp3.analyse(files, "intro")
    prev = prev_season_files(sn)
    for f in files:
        t = truth[f]
        if not t:
            continue
        tally["season_all"][judge(base[f]["segment"], t)] += 1
        sib = [g for g in files if g != f][0]
        tally["one_sibling"][judge(fp3.analyse([f, sib], "intro")[f]["segment"], t)] += 1
        if prev:
            tally["prev_season"][judge(fp3.analyse([f] + prev, "intro")[f]["segment"], t)] += 1
        else:
            tally["prev_season"]["no_prev_season"] += 1
    print(sn.split("TV Shows/")[1], "prev:", len(prev), json.dumps({k: dict(v) for k, v in tally.items()}), flush=True)
print("FINAL", json.dumps({k: dict(v) for k, v in tally.items()}))
