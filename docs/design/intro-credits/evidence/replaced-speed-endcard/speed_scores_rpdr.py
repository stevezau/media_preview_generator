"""RPDR UK S08E04 AMZN (1987, 23.976, audio untouched) against the 25 fps siblings: own vs retimed scores, prod
fingerprints (e04/fps.txt)."""

import sys

import numpy as np

sys.path.insert(
    0,
    "/tmp/claude-1000/-home-data-workspace-plex-generate-vid-previews/221c1481-5d66-4e86-8c40-a12113e50030/scratchpad/laneL",
)
from speed_scores import report  # noqa: E402

fps = {}
for line in open(sys.argv[1]):
    fid, win, hx = line.strip().split("|")
    fps[(int(fid), win)] = np.frombuffer(bytes.fromhex(hx), dtype="<u4").copy()
FILM, PAL = 24000 / 1001, 25.0
for members in ((1940, 1941, 1942, 1987), (1940, 1941, 1942, 1987, 1995)):
    pts = {str(m): fps[(m, "intro")] for m in members}
    speeds = {str(m): (FILM if m == 1987 else PAL) for m in members}
    report(f"RPDR {members}", pts, speeds, lambda p, f: fps[(int(p), f"intro@{f:.6f}")])
