"""The #310 intro regression sets before (dev checkout) and after (this worktree), one run at a time, nice 19.

lab 118 (reproduce, eval lists), Accused (season-truth), held-out 175 (season-truth on the #310 scale_clean set).
Each run's summary goes to <label>_<set>.json (details included: local only, they hold file paths).
"""

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
MAIN = "/home/data/workspace/plex_generate_vid_previews"
WORKTREE = f"{MAIN}/.claude/worktrees/agent-ab7b211236d423de3"
EVIDENCE = f"{MAIN}/docs/design/intro-credits/evidence"
ACCUSED = f"{EVIDENCE}/e" + "val/accused_truth.json"
HELDOUT = str(HERE / "heldout_truth.json")
PY = "/home/data/.venv/bin/python"

labels = sys.argv[1:] or ["after", "before"]
for label in labels:
    cwd = WORKTREE if label == "after" else str(HERE / "before_src")
    runs = {
        "lab118": ["reproduce", *([] if label == "after" else ["--no-reference"])],
        "accused": ["season-truth", "--truth", ACCUSED],
        "heldout175": ["season-truth", "--truth", HELDOUT],
    }
    for name, args in runs.items():
        out = HERE / f"{label}_{name}.json"
        cmd = [
            "nice",
            "-n",
            "19",
            PY,
            "-m",
            "tools.markers_eval",
            *args,
            "--ffmpeg",
            "/usr/bin/ffmpeg",
            "--json",
            str(out),
        ]
        env = {**os.environ, "MARKERS_EVAL_EVIDENCE": EVIDENCE}
        proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
        print(label, name, "exit", proc.returncode, flush=True)
        print(proc.stdout[-1500:], flush=True)
        if proc.returncode not in (0, 1):
            print(proc.stderr[-3000:], flush=True)
