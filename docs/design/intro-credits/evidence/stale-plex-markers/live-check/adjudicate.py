"""Non-impossible disagreements: frame sheets around the earlier and later credit start (read-only ffmpeg)."""

import os
import pickle
import subprocess
import sys

from PIL import Image, ImageDraw

WT = "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-af00f7652b351cf9c"
sys.path.insert(0, WT)
from media_preview_generator.markers.decide import FileLimits  # noqa: E402
from media_preview_generator.markers.models import MarkerType  # noqa: E402
from media_preview_generator.markers.publishers import plex_db  # noqa: E402

D = "/tmp/claude-1000/-home-data-workspace-plex-generate-vid-previews/221c1481-5d66-4e86-8c40-a12113e50030/scratchpad/stale_fresh"
OUT = f"{D}/frames"
res = pickle.load(open(f"{D}/live_check_replay.pkl", "rb"))
SETTLED = ("Physical 100 Mexico", "Somebody Somewhere", "The Big Bang Theory")
mode = sys.argv[1] if len(sys.argv) > 1 else "list"
todo = []
impossible = 0
for r in res:
    if r["agree"]:
        continue
    mtype = MarkerType(r["type"])
    if plex_db._plex_rows_cant_be_right(mtype, [tuple(r["plex"])], ([], [], []), FileLimits(r["dur"])):
        impossible += 1
        continue
    name = os.path.basename(r["path"])
    tag = "settled" if any(s in name for s in SETTLED) else "check"
    print(tag, r["type"], r["plex"], r["ours"], r["dur"], name[:80])
    if tag == "check":
        todo.append(r)
print("impossible (already dropped by Keep Plex's):", impossible, "to check:", len(todo))
if mode != "frames":
    sys.exit()
for i, r in enumerate(todo):
    early, late = sorted((r["plex"][0] // 1000, r["ours"][0] // 1000))
    times = [early - 6, early + 3, (early + late) // 2 if late - early > 12 else early + 8, late + 3]
    tiles = []
    for t in times:
        f = f"{OUT}/adj{i}_{t}.jpg"
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                str(max(t, 0)),
                "-i",
                r["path"],
                "-frames:v",
                "1",
                "-vf",
                "scale=400:-2",
                f,
            ],
            check=True,
        )
        im = Image.open(f).convert("RGB")
        who = "plex" if t - 3 == r["plex"][0] // 1000 else ("ours" if t - 3 == r["ours"][0] // 1000 else "")
        ImageDraw.Draw(im).rectangle([0, 0, 120, 20], fill="black")
        ImageDraw.Draw(im).text((4, 4), f"{t}s {who}", fill="yellow")
        tiles.append(im)
    w, h = tiles[0].size
    sheet = Image.new("RGB", (w * 4, h))
    for j, im in enumerate(tiles):
        sheet.paste(im, (j * w, 0))
    sheet.save(f"{OUT}/adj{i}_sheet.jpg", quality=75)
    print(
        i,
        f"{OUT}/adj{i}_sheet.jpg",
        r["type"],
        "plex",
        r["plex"][0] // 1000,
        "ours",
        r["ours"][0] // 1000,
        os.path.basename(r["path"])[:60],
    )
