"""Season audio alone on the #310/#312 intro sets, with the end-picture check decoded by one tree's
``end_picture.Reader`` on the NVIDIA GPU or the CPU (the end-picture scaler change, spec §14 2026-09-25).

    nice -n 19 python ep_sets.py <tree> <label> <nvidia|cpu> [set ...]

Sets: ``lists`` (lab 118), ``scale_clean`` (held-out 175), ``accused``, ``chap_clean`` (library chapter set) through the
#310 harness (``intro-pick``: its fingerprints and truth, ``lab.Season``, the tree's ``season.season_intro``, scored as
``intro-guards/t3_sets.py`` does), and ``bones`` (Bones S05-S08 through the tree's ``tools.markers_eval.SeasonStep`` with
the speed clock, frame-checked truth, as ``speed/bones_eval.py`` does). Every share is decoded fresh by this run's
reader (no seed cache), and every decode is counted by where it ran, so a GPU decode that fell back to the CPU shows.

Read-only on /data. Writes ``<OUT>/<label>.json`` (tallies, per-file answers, decode counts; lists library paths:
local-only) and ``<OUT>/<label>_shares.pkl``.
"""

from __future__ import annotations

import collections
import glob
import json
import os
import pickle
import re
import sys
import time
from pathlib import Path

TREE, LABEL, DEVICE = sys.argv[1], sys.argv[2], sys.argv[3]
SETS = sys.argv[4:] or ["lists", "scale_clean", "accused", "chap_clean", "bones"]
OUT = Path(os.environ.get("EP_OUT", Path(__file__).parent / "local"))
INTRO_PICK = "/tmp/claude-1000/-home-data-workspace-plex-generate-vid-previews/a98735d3-c717-46ce-a02b-854ad26af8ae/scratchpad/intro-pick"
BONES_ROOT = "/data/TV Shows/Bones (2005) {tvdb-75682}"
BONES_TRUTH = "/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/speed/truth.json"
FFMPEG, FFPROBE = "/usr/bin/ffmpeg", "/usr/bin/ffprobe"

sys.path.insert(0, TREE)
# Everything from the tree first: intro-pick's lab.py puts the main checkout on sys.path when imported.
import media_preview_generator.markers.audio.season as S  # noqa: E402
from media_preview_generator.markers.audio import end_picture  # noqa: E402
from media_preview_generator.markers.credits import frames  # noqa: E402
from tools.markers_eval.cache import FingerprintCache  # noqa: E402
from tools.markers_eval.data import EvalEpisode  # noqa: E402
from tools.markers_eval.intros import ReproductionReport, SeasonStep  # noqa: E402
from tools.markers_eval.score import judge_intro  # noqa: E402

for module in (S, end_picture, frames, sys.modules["tools.markers_eval.intros"]):
    assert module.__file__.startswith(TREE), module.__file__
sys.path.insert(1, INTRO_PICK)
from evaluate import eval_groups  # noqa: E402
from lab import Season, judge  # noqa: E402

GPU, GPU_DEVICE = {"nvidia": ("NVIDIA", "cuda:0"), "cpu": (None, None)}[DEVICE]
reader = end_picture.Reader(ffmpeg=FFMPEG, gpu=GPU, gpu_device_path=GPU_DEVICE)
decodes = collections.Counter()
_run_decode = frames.run_decode


def counted_run_decode(command, *, hw_active, **kwargs):
    try:
        rows = _run_decode(command, hw_active=hw_active, **kwargs)
    except frames.GpuDecodeError:
        decodes["gpu_failed"] += 1
        raise
    except Exception:
        decodes[f"{'gpu' if hw_active else 'cpu'}_error"] += 1
        raise
    decodes["gpu" if hw_active else "cpu"] += 1
    decodes["filter:" + command[command.index("-vf") + 1]] += 1
    return rows


frames.run_decode = counted_run_decode
shares: dict[tuple, float | None] = {}
read_failed: list[str] = []


def share_of(target, hit, segment):
    offset = hit.partner_start_s - hit.start_s
    key = (target, hit.partner, round(segment.start_s, 3), round(segment.end_s, 3), round(offset, 3))
    if key not in shares:
        try:
            shares[key] = reader.share(target, hit.partner, segment.start_s, segment.end_s, offset)
        except end_picture.ReadFailedError as exc:
            read_failed.append(f"{exc.path}: {exc}")
            shares[key] = None
    return shares[key]


def oracle(target):
    def check(candidate):
        members = [h for h in candidate.members if os.path.exists(h.partner)]
        return end_picture.passes([share_of(target, hit, candidate.segment) for hit in end_picture.partners(members)])

    return check


class EndPictures:
    """``tools.markers_eval.intros.EndPictures`` on this run's shares."""

    def for_episode(self, target):
        return oracle(target)


def harness_set(mode):
    tally, rows = collections.Counter(), {}
    for _season, files, fps, truth in eval_groups(mode):
        season = Season(files, fps)
        for f, tr in truth.items():
            ok = len(season.files) >= 2 and f in season.files
            seg = S.season_intro(f, season.files, fps, season.runs, end_picture_passes=oracle(f)) if ok else None
            verdict = ("wrong" if seg else "none-ok") if tr is None else judge(seg[:2] if seg else None, tr)
            tally[verdict] += 1
            rows[f] = {"truth": None if tr is None else [float(x) for x in tr],
                       "answer": None if seg is None else [float(x) for x in seg], "verdict": verdict}  # fmt: skip
    return tally, rows


def bones_set():
    cache = FingerprintCache(Path.home() / ".cache/markers_eval", ffmpeg=FFMPEG, ffprobe=FFPROBE)
    truth = json.load(open(BONES_TRUTH))
    tally, rows = collections.Counter(), {}
    for number in ("05", "06", "07", "08"):
        files = sorted(glob.glob(f"{BONES_ROOT}/Season {number}/*.mkv"))
        points = {f: cache.points(f) for f in files}
        step = SeasonStep(number, points, ReproductionReport(), EndPictures(), speed=cache.speed, retimed=cache.retimed)
        for f in files:
            code = re.search(r"S\d\dE\d\d", f).group(0)
            if "end" not in truth.get(code, {}):
                continue
            tr = (truth[code]["start"], truth[code]["end"])
            seg = step.answer(EvalEpisode(number, f, None, None, None))
            verdict = judge_intro(seg[:2] if seg else None, tr)
            tally[verdict] += 1
            rows[f] = {
                "truth": list(tr),
                "answer": None if seg is None else [float(x) for x in seg],
                "verdict": verdict,
            }
    return tally, rows


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    out = {"tree": TREE, "device": DEVICE, "sets": {}}
    for mode in SETS:
        t0 = time.time()
        before = sum(decodes[k] for k in ("gpu", "cpu"))
        tally, rows = bones_set() if mode == "bones" else harness_set(mode)
        out["sets"][mode] = {"tally": dict(tally), "rows": rows}
        print(f"{LABEL:14} {mode:12} useful {tally['useful']:3} wrong {tally['wrong']:3} missed {tally['missed']:3} "
              f"none-ok {tally['none-ok']}  ({time.time() - t0:.0f}s, "
              f"{sum(decodes[k] for k in ('gpu', 'cpu')) - before} decodes)", flush=True)  # fmt: skip
        out["decodes"], out["read_failed"] = dict(decodes), read_failed
        json.dump(out, open(OUT / f"{LABEL}.json", "w"), indent=1)
        pickle.dump(shares, open(OUT / f"{LABEL}_shares.pkl", "wb"))
    print(LABEL, dict(decodes), f"read failures {len(read_failed)}", flush=True)


main()
