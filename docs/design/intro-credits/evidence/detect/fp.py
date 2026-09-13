"""Throwaway experiment: chromaprint cross-episode intro/credits detection.

Usage: python fp.py <season_dir> [<season_dir>...] --out results.json
"""

import argparse
import concurrent.futures as cf
import json
import re
import subprocess
import sys
from pathlib import Path

import numpy as np

POINT_S = 4096 / 11025 / 3  # seconds per chromaprint point (hop 1365 @ 11025 Hz)
MAX_BIT_DIFF = 6
MAX_GAP_S = 3.5
MIN_INTRO_S = 15
MAX_INTRO_S = 120
INTRO_WINDOW_S = 600
CREDITS_WINDOW_S = 450
FFMPEG = "ffmpeg"

_POP = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)


def popcount32(x: np.ndarray) -> np.ndarray:
    b = x.view(np.uint8).reshape(-1, 4)
    return _POP[b].sum(axis=1)


def duration_s(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return float(out.strip())


def fingerprint(path: str, start: float, length: float) -> np.ndarray:
    cmd = [
        FFMPEG,
        "-v",
        "error",
        "-ss",
        f"{start:.3f}",
        "-i",
        path,
        "-t",
        f"{length:.3f}",
        "-vn",
        "-sn",
        "-dn",
        "-ac",
        "2",
        "-f",
        "chromaprint",
        "-fp_format",
        "raw",
        "-",
    ]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype="<u4")


def best_run(a: np.ndarray, b: np.ndarray) -> tuple[float, float, float, float] | None:
    """Longest matching run between a and b over candidate shifts. Returns (a0,a1,b0,b1) seconds."""
    index: dict[int, list[int]] = {}
    for i, v in enumerate(b.tolist()):
        index.setdefault(v, []).append(i)
    shifts: dict[int, int] = {}
    for i, v in enumerate(a.tolist()):
        for d in (-2, -1, 0, 1, 2):
            for j in index.get(v + d, ()):
                shifts[j - i] = shifts.get(j - i, 0) + 1
    # Only examine shifts with repeated support; exact-hash hits are sparse but clustered.
    candidates = [s for s, c in sorted(shifts.items(), key=lambda kv: -kv[1])[:40]]
    gap = int(MAX_GAP_S / POINT_S)
    best = None
    best_len = 0
    for s in candidates:
        a0, b0 = (0, s) if s >= 0 else (-s, 0)
        n = min(len(a) - a0, len(b) - b0)
        if n <= 0:
            continue
        ok = popcount32(a[a0 : a0 + n] ^ b[b0 : b0 + n]) <= MAX_BIT_DIFF
        idx = np.flatnonzero(ok)
        if len(idx) < 2:
            continue
        breaks = np.flatnonzero(np.diff(idx) > gap)
        starts = np.concatenate(([idx[0]], idx[breaks + 1]))
        ends = np.concatenate((idx[breaks], [idx[-1]]))
        k = int(np.argmax(ends - starts))
        length = int(ends[k] - starts[k])
        if length > best_len:
            best_len = length
            best = (a0 + starts[k], a0 + ends[k], b0 + starts[k], b0 + ends[k])
    if best is None:
        return None
    return tuple(round(float(x) * POINT_S, 2) for x in best)  # type: ignore[return-value]


def consensus(matches: list[tuple[float, float]], tol: float = 4.0) -> tuple[float, float, int] | None:
    """Pick the (start,end) supported by the most pair matches (within tol seconds)."""
    if not matches:
        return None
    best = None
    for s, e in matches:
        support = [(s2, e2) for s2, e2 in matches if abs(s2 - s) <= tol and abs(e2 - e) <= tol]
        if best is None or len(support) > best[2]:
            best = (float(np.median([m[0] for m in support])), float(np.median([m[1] for m in support])), len(support))
    return best


def analyse(files: list[str], window: str) -> dict[str, dict]:
    durs = {f: duration_s(f) for f in files}

    def fp(f: str) -> np.ndarray:
        if window == "intro":
            return fingerprint(f, 0, min(INTRO_WINDOW_S, durs[f] * 0.25 if durs[f] >= 300 else durs[f]))
        start = max(0.0, durs[f] - CREDITS_WINDOW_S)
        return fingerprint(f, start, durs[f] - start)

    with cf.ThreadPoolExecutor(8) as ex:
        fps = dict(zip(files, ex.map(fp, files)))
    pair_hits: dict[str, list[tuple[float, float]]] = {f: [] for f in files}
    min_len = MIN_INTRO_S if window == "intro" else 15
    for i, fa in enumerate(files):
        for fb in files[i + 1 :]:
            r = best_run(fps[fa], fps[fb])
            if r is None:
                continue
            a0, a1, b0, b1 = r
            if a1 - a0 < min_len or (window == "intro" and a1 - a0 > MAX_INTRO_S):
                continue
            off_a = 0.0 if window == "intro" else max(0.0, durs[fa] - CREDITS_WINDOW_S)
            off_b = 0.0 if window == "intro" else max(0.0, durs[fb] - CREDITS_WINDOW_S)
            pair_hits[fa].append((a0 + off_a, a1 + off_a))
            pair_hits[fb].append((b0 + off_b, b1 + off_b))
    out = {}
    for f in files:
        c = consensus(pair_hits[f])
        out[f] = {"duration": durs[f], "pairs": len(pair_hits[f]), "segment": c}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dirs", nargs="+")
    ap.add_argument("--out", required=True)
    ap.add_argument("--ffmpeg", default="ffmpeg")
    args = ap.parse_args()
    global FFMPEG
    FFMPEG = args.ffmpeg
    result = {}
    for d in args.dirs:
        files = sorted(str(p) for p in Path(d).iterdir() if p.suffix in (".mkv", ".mp4"))
        result[d] = {"intro": analyse(files, "intro"), "credits": analyse(files, "credits")}
        print(f"done {d}", file=sys.stderr)
    Path(args.out).write_text(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()
