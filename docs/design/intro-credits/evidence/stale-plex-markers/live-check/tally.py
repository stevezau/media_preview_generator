"""Verdicts for the 29 disagreements where Plex's stale marker could fit the file (frame checks + source agreement)."""

import collections
import os
import pickle
import sys

WT = "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-af00f7652b351cf9c"
sys.path.insert(0, WT)
from media_preview_generator.markers.decide import FileLimits  # noqa: E402
from media_preview_generator.markers.models import MarkerType  # noqa: E402
from media_preview_generator.markers.publishers import plex_db  # noqa: E402

D = "/tmp/claude-1000/-home-data-workspace-plex-generate-vid-previews/221c1481-5d66-4e86-8c40-a12113e50030/scratchpad/stale_fresh"
res = pickle.load(open(f"{D}/live_check_replay.pkl", "rb"))

# (substring, type) -> verdict. Frame-checked unless noted.
VERDICT = {
    (
        "Physical 100 Mexico",
        "credits",
    ): "ours",  # E01 frames: credits at 3490 s, Plex ~100 s late; same chapter shape x7
    ("Somebody Somewhere", "credits"): "plex",  # E02 frames: credits at 1648 s, ours ~53 s late; skipdb = Plex x5
    ("A Simple Plan", "credits"): "ours",  # starts agree; Plex ends 21 s before the file's end
    ("A Scanner Darkly", "credits"): "tie",  # Plex starts on the author's note, ours on the crew names
    ("The Little Rascals", "credits"): "plex",  # cast cards over footage from 4712 s; ours 15 s late
    ("28 Days Later", "credits"): "ours",  # Plex 37 s early, in the final scene
    ("Accused Guilty or Innocent (2020) - S03E09", "credits"): "both-wrong",  # ours on an epilogue text card
    ("Accused Guilty or Innocent (2020) - S05E02", "credits"): "both-wrong",  # ours a 3 s verdict text card
    ("Accused Guilty or Innocent (2020) - S07E05", "credits"): "both-wrong",  # ours on the sentencing text card
    ("Accused Guilty or Innocent (2020) - S07E09", "credits"): "both-wrong",  # ours a 6 s mid-episode text card
    ("Bones (2005) - S08E03", "credits"): "ours",  # Plex 84 s early, in the story
    ("The Deliverance", "credits"): "tie",  # Plex ~30 s early (true-story epilogue), ours ~30 s late
    ("Hell House LLC", "credits"): "tie",  # starts agree; ends differ, not checked
    ("Lioness", "intro"): "ours",  # Plex 5 s early, in the story
    ("Doc US", "credits"): "plex",  # credits from 2606 s; ours 12 s late
    ("Sort Of (2021) - S03E05", "credits"): "ours",  # Plex 20 s early, in the story
    ("Deadly Influence", "credits"): "ours",  # Plex 2 min early, in the story
    ("Taskmaster NZ", "credits"): "ours",  # Plex 50 s early, host still talking
    ("Taskmaster NZ", "intro"): "tie",  # 1 s vs 6 s apart at the ends; not decidable from frames
}

tally = collections.Counter()
split = collections.Counter()
impossible = collections.Counter()
for r in res:
    mtype = MarkerType(r["type"])
    if r["agree"]:
        split[("agree", r["type"], r["speed"].split(" (")[0], r["replaced"])] += 1
        continue
    if plex_db._plex_rows_cant_be_right(mtype, [tuple(r["plex"])], ([], [], []), FileLimits(r["dur"])):
        impossible[(r["type"], r["speed"].split(" (")[0], r["replaced"])] += 1
        continue
    name = os.path.basename(r["path"])
    v = next((v for (s, t), v in VERDICT.items() if s in name and t == r["type"]), None)
    assert v is not None, name
    tally[v] += 1
    split[("disagree:" + v, r["type"], r["speed"].split(" (")[0], r["replaced"])] += 1
print("verdicts (fitting disagreements):", dict(tally))
print("ours wrong:", tally["plex"] + tally["both-wrong"], " plex wrong:", tally["ours"] + tally["both-wrong"])
print("impossible Plex (already replaced by round 1):", sum(impossible.values()))
for k, v in sorted(impossible.items()):
    print("  impossible", k, v)
for k, v in sorted(split.items()):
    print("  ", k, v)
