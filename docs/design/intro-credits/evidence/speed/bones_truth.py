"""Visual intro truth for Bones S05-S08, independent of the audio: the "BONES" logo card and the "created by Hart Hanson"
card, found by correlation with reference frames (64x36 grey) at 4 fps in a window around every source's answer.

Truth end = the last frame of the "created by" card + 0.25 s; truth start = the logo card's first frame - 4 s (the
title sequence opens 2.5-6 s before the logo; "useful" allows 15 s on the start). Media is only read (nice 19, GPU
decode). Writes truth.json.
"""

import json
import re
import subprocess
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
ROOT = "/data/TV Shows/Bones (2005) {tvdb-75682}"
FPS, W, H = 4, 64, 36
FILM_ON_PAL = 24000 / 1001 / 25
REFS = {  # (file, seconds) of each card in two episodes of different seasons, checked on contact sheets
    "logo": [
        (
            "Season 07/Bones (2005) - S07E01 - The Memories in the Shallow Grave [WEBDL-1080p][AAC 2.0][x264]-FUZEER.mkv",
            314.0,
        ),
        ("Season 05/Bones (2005) - S05E01 - Harbingers in the Fountain [WEBDL-1080p][AAC 2.0][x264]-FUZEER.mkv", 387.0),
    ],
    "card": [
        (
            "Season 07/Bones (2005) - S07E01 - The Memories in the Shallow Grave [WEBDL-1080p][AAC 2.0][x264]-FUZEER.mkv",
            338.2,
        ),
        ("Season 05/Bones (2005) - S05E01 - Harbingers in the Fountain [WEBDL-1080p][AAC 2.0][x264]-FUZEER.mkv", 412.4),
    ],
}


def frames(path: str, start: float, length: float, fps: float = FPS) -> np.ndarray:
    cmd = [
        "nice",
        "-n",
        "19",
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-hwaccel",
        "cuda",
        "-ss",
        f"{max(0.0, start):.3f}",
        "-t",
        f"{length:.3f}",
        "-i",
        path,
        "-an",
        "-sn",
        "-vf",
        f"fps={fps},scale={W}:{H}",
        "-pix_fmt",
        "gray",
        "-f",
        "rawvideo",
        "-",
    ]
    out = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype=np.uint8).reshape(-1, H, W).astype(np.float32)


def unit(frame: np.ndarray) -> np.ndarray:
    z = frame.ravel() - frame.mean()
    return z / (np.linalg.norm(z) + 1e-6)


def ncc(block: np.ndarray, ref: np.ndarray) -> np.ndarray:
    flat = block.reshape(len(block), -1)
    z = flat - flat.mean(axis=1, keepdims=True)
    return (z @ ref) / (np.linalg.norm(z, axis=1) + 1e-6)


def main() -> None:
    refs = {kind: [unit(frames(f"{ROOT}/{name}", t, 0.25)[0]) for name, t in items] for kind, items in REFS.items()}
    answers = json.load(open(HERE / "answers.json"))
    truth = {}
    for code, row in sorted(answers.items()):
        season = code[1:3]
        path = f"{ROOT}/Season {season}/{row['file']}"
        ends = [a[1] for a in (row["audio_before"], row["audio_after"]) if a]
        ends += [e / 1000 for e in ([row["plex"][1]] if row["plex"] else [])]
        if row["introdb"]:
            ends += [
                row["introdb"][1] / 1000,
                row["introdb"][1] / 1000 * FILM_ON_PAL,
                row["introdb"][1] / 1000 / FILM_ON_PAL,
            ]
        lo, hi = min(ends) - 60, max(ends) + 20
        block = frames(path, lo, hi - lo)
        logo = np.max([ncc(block, r) for r in refs["logo"]], axis=0)
        card = np.max([ncc(block, r) for r in refs["card"]], axis=0)
        logo_hits, card_hits = np.flatnonzero(logo > 0.6), np.flatnonzero(card > 0.6)
        entry = {"logo_max": round(float(logo.max()), 3), "card_max": round(float(card.max()), 3)}
        # The card closes the sequence 20-40 s after the logo: its first run of matching frames there (a story frame
        # later in the window can correlate with it too, S08E07 at 5:42).
        card_hits = (
            card_hits[(card_hits > logo_hits[0] + 15 * FPS) & (card_hits < logo_hits[0] + 45 * FPS)]
            if len(logo_hits)
            else card_hits[:0]
        )
        if len(logo_hits) and len(card_hits):
            run_end = card_hits[0]
            for hit in card_hits[1:]:
                if hit - run_end > 2:
                    break
                run_end = hit
            first_logo = lo + logo_hits[0] / FPS
            last_card = lo + run_end / FPS + 1 / FPS
            entry.update(start=round(first_logo - 4.0, 2), end=round(last_card, 2), logo_at=round(first_logo, 2))
        np.save(HERE / f"ncc_{code}.npy", np.stack([logo, card]))
        truth[code] = entry
        print(code, entry, flush=True)
    json.dump(truth, open(HERE / "truth.json", "w"), indent=1)


if __name__ == "__main__":
    main()
