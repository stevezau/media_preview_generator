import sys
import numpy as np
sys.path.insert(0, "/home/data/workspace/plex_generate_vid_previews")
from media_preview_generator.markers.audio.season import season_pair_runs
fps = {}
for line in open(sys.argv[1]):
    fid, win, hx = line.strip().split("|")
    fps[(int(fid), win)] = np.frombuffer(bytes.fromhex(hx), dtype="<u4").copy()
def show(a, b):
    runs = season_pair_runs(fps[a], fps[b])
    print(f"{a} vs {b}: " + ("; ".join(f"a {r[0]:.1f}-{r[1]:.1f}  b {r[2]:.1f}-{r[3]:.1f}" for r in runs) or "[]"))
for sib in (1940, 1941, 1942):
    show((sib, "intro"), (1987, "intro@1.042708"))   # what prod ran (pair version 2007)
    show((sib, "intro"), (1987, "intro"))            # own speed
    show((sib, "intro"), (1995, "intro"))            # 25 fps release of same episode
