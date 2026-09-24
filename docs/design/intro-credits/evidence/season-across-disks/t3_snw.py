"""Star Trek SNW S04, every episode: the worktree's season step in its own folder vs the folders on every disk."""

import glob
import os
import re
import subprocess
import sys

os.environ["FILL"] = "1"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import t3_prod as P  # noqa: E402

paths = sorted(glob.glob("/data_16tb*/TV Shows/Star Trek Strange New Worlds (2022) {tvdb-382389}/Season 04/*.mkv"))
for path in paths:
    own, n_own = P.replay(path, merged=False, exempt=True)
    merged, n_merged = P.replay(path, merged=True, exempt=True)
    name = re.search(r"S04E\d\d", path).group(0)
    print(
        name, path.split("/")[1], "own folder", n_own, P.show(own), "| every disk", n_merged, P.show(merged), flush=True
    )
e10 = next(p for p in paths if "S04E10" in p)
for t in (566, 600, 665, 672):
    out = os.path.join(P.HERE, f"snw_e10_title_{t}.jpg")
    subprocess.run(
        ["nice", "-n", "19", "/usr/bin/ffmpeg", "-v", "error", "-y", "-ss", str(t), "-i", e10, "-frames:v", "1",
         "-vf", "scale=480:-2", out],
        check=True,
    )  # fmt: skip
subprocess.run(
    ["/usr/bin/ffmpeg", "-v", "error", "-y",
     *sum((["-i", os.path.join(P.HERE, f"snw_e10_title_{t}.jpg")] for t in (566, 600, 665, 672)), []),
     "-filter_complex", "xstack=inputs=4:layout=0_0|w0_0|w0+w1_0|w0+w1+w2_0",
     os.path.join(P.HERE, "snw_e10_title_tiles.jpg")],
    check=True,
)  # fmt: skip
