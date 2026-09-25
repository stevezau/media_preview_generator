"""Bones S05-S08, season audio alone: the season step with speeds told by frame rate (shipped, season audio v7) and by
ear (this worktree's clock_by_audio), scored against the frame-checked truth (bones/truth.json). Read-only on /data;
fingerprints and retimed fingerprints from the harness cache (~/.cache/markers_eval)."""

import collections
import glob
import json
import re
import sys
from pathlib import Path
from unittest.mock import patch

WORKTREE = "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-a247053d59620fedb"
sys.path.insert(0, WORKTREE)
from media_preview_generator.markers.audio import end_picture  # noqa: E402
from tools.markers_eval import intros  # noqa: E402
from tools.markers_eval.cache import FingerprintCache  # noqa: E402
from tools.markers_eval.data import EvalEpisode  # noqa: E402
from tools.markers_eval.intros import DecodedEndPictures, ReproductionReport, SeasonStep  # noqa: E402
from tools.markers_eval.score import judge_intro  # noqa: E402

assert intros.__file__.startswith(WORKTREE)
SCR = Path(
    "/tmp/claude-1000/-home-data-workspace-plex-generate-vid-previews/221c1481-5d66-4e86-8c40-a12113e50030/scratchpad"
)
ROOT = "/data/TV Shows/Bones (2005) {tvdb-75682}"
truth = json.load(open(SCR / "bones/truth.json"))
cache = FingerprintCache(Path.home() / ".cache/markers_eval", ffmpeg="/usr/bin/ffmpeg", ffprobe="/usr/bin/ffprobe")
end_pictures = DecodedEndPictures(end_picture.Reader(ffmpeg="/usr/bin/ffmpeg", gpu="NVIDIA", gpu_device_path="cuda:0"))
tally = {label: collections.Counter() for label in ("by_rate", "by_ear")}
changed = 0
for season in ("05", "06", "07", "08"):
    files = sorted(glob.glob(f"{ROOT}/Season {season}/*.mkv"))
    points = {f: cache.points(f) for f in files}
    steps = {"by_ear": SeasonStep(season, points, ReproductionReport(), end_pictures, speed=cache.speed,
                                  retimed=cache.retimed)}  # fmt: skip
    with patch.object(intros, "clock_by_audio", lambda clock, references, heard: clock):
        steps["by_rate"] = SeasonStep(season, points, ReproductionReport(), end_pictures, speed=cache.speed,
                                      retimed=cache.retimed)  # fmt: skip
    assert steps["by_ear"].clock == steps["by_rate"].clock, (season, steps["by_ear"].clock, steps["by_rate"].clock)
    for f in files:
        code = re.search(r"S\d\dE\d\d", f).group(0)
        t = truth.get(code, {})
        episode = EvalEpisode(season, f, None, None, None)
        answers = {label: step.answer(episode) for label, step in steps.items()}
        if answers["by_rate"] != answers["by_ear"]:
            changed += 1
            print("CHANGED", code, answers)
        if "end" in t:
            for label, answer in answers.items():
                tally[label][judge_intro(answer[:2] if answer else None, (t["start"], t["end"]))] += 1
    print(season, {label: dict(step.clock.factors) and len(step.clock.factors) for label, step in steps.items()},
          flush=True)  # fmt: skip
for label, t in tally.items():
    print(f"{label:8} useful {t['useful']} wrong {t['wrong']} missed {t['missed']}")
print("answers changed:", changed)
