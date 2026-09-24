"""Experiment: do retimed fingerprints make Bones WEB (25 fps) and Blu-ray (23.976) openings match?

Fingerprints each S05 file natively and, for the 25 fps files, with the audio slowed to film speed by three filters:
asetrate (speed + pitch, the inverse of a plain PAL speed-up), atempo (speed only, pitch kept) and none. Caches every
fingerprint as .npy under fp/. Read only on /data; nice -n 19; one ffmpeg at a time.
"""

import glob
import json
import os
import subprocess
import sys

import numpy as np

sys.path.insert(0, "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-ab7b211236d423de3")
from media_preview_generator.markers.audio.matcher import pair_runs  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FP = os.path.join(HERE, "fp")
os.makedirs(FP, exist_ok=True)
ROOT = "/data/TV Shows/Bones (2005) {tvdb-75682}"
FPS = json.load(open(os.path.join(HERE, "fps.json")))
FILM = 24000 / 1001
PAL = 25.0


def fingerprint(path: str, length_s: float, filt: str | None, tag: str) -> np.ndarray:
    name = os.path.basename(path)[:22].replace(" ", "_") + f"_{tag}.npy"
    cache = os.path.join(FP, name)
    if os.path.exists(cache):
        return np.load(cache)
    cmd = [
        "nice",
        "-n",
        "19",
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-threads",
        "2",
        "-ss",
        "0",
        "-t",
        f"{length_s:.3f}",
        "-i",
        path,
        "-vn",
        "-sn",
        "-dn",
        "-ac",
        "2",
    ]
    if filt:
        cmd += ["-af", filt]
    cmd += ["-f", "chromaprint", "-algorithm", "1", "-fp_format", "raw", "-"]
    out = subprocess.run(cmd, capture_output=True, check=True).stdout
    pts = np.frombuffer(out[: len(out) - len(out) % 4], dtype="<u4").copy()
    np.save(cache, pts)
    return pts


def main() -> None:
    season = sys.argv[1] if len(sys.argv) > 1 else "05"
    files = sorted(glob.glob(f"{ROOT}/Season {season}/*.mkv"))
    ratio = PAL / FILM  # 1.0427: the PAL file plays this much faster
    variants = {
        "asetrate": f"asetrate=48000*{FILM}/{PAL},aresample=48000",
        "atempo": f"atempo={FILM / PAL:.6f}",
    }
    native = {}
    retimed = {k: {} for k in variants}
    for f in files:
        dur = FPS[f]["dur"]
        length = min(900.0, 0.35 * dur)
        native[f] = fingerprint(f, length, None, "native")
        if FPS[f]["r"] == "25/1":
            for k, filt in variants.items():
                retimed[k][f] = fingerprint(f, length, filt, k)
    web = [f for f in files if FPS[f]["r"] == "25/1"]
    film = [f for f in files if FPS[f]["r"] != "25/1"]
    print(f"S{season}: {len(web)} WEB, {len(film)} film")

    def matched(a, b):
        runs = [r for r in pair_runs(a, b) if r.a_end_s - r.a_start_s <= 120]
        return runs

    for label, pts in [("native", native), *[(k, {**native, **v}) for k, v in retimed.items()]]:
        cross = 0
        detail = []
        for w in web:
            for fl in film:
                runs = matched(pts[w], pts[fl])
                if runs:
                    cross += 1
                    best = max(runs, key=lambda r: r.a_end_s - r.a_start_s)
                    detail.append((best.a_start_s, best.a_end_s, best.b_start_s, best.b_end_s))
        print(f"  {label:9s} WEB<->film pairs with a run: {cross}/{len(web) * len(film)}")
        for d in detail[:5]:
            print("     ", " ".join(f"{x:7.1f}" for x in d))
    print("ratio", ratio)


main()
