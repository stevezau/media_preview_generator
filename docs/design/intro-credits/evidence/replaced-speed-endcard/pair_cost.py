import sys
import time

import numpy as np

sys.path.insert(0, "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-a247053d59620fedb")
from media_preview_generator.markers.audio import season as S  # noqa: E402

fps = {}
for line in open(sys.argv[1]):
    fid, win, hx = line.strip().split("|")
    fps[(int(fid), win)] = np.frombuffer(bytes.fromhex(hx), dtype="<u4").copy()
keys = list(fps)
t = time.perf_counter()
n = 0
for i in range(len(keys)):
    for j in range(i + 1, len(keys)):
        S.season_pair_runs(fps[keys[i]], fps[keys[j]])
        n += 1
print(n, "pairs", (time.perf_counter() - t) / n * 1000, "ms/pair", [len(v) for v in fps.values()])
