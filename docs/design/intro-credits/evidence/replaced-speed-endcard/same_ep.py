import sys
import numpy as np
sys.path.insert(0, "/home/data/workspace/plex_generate_vid_previews")
from media_preview_generator.markers.audio.matcher import pair_runs
fps = {}
for line in open(sys.argv[1]):
    fid, win, hx = line.strip().split("|")
    fps[(int(fid), win)] = np.frombuffer(bytes.fromhex(hx), dtype="<u4").copy()
for a, b in [((1995, "intro"), (1987, "intro")), ((1995, "intro"), (1987, "intro@1.042708"))]:
    runs = pair_runs(fps[a], fps[b])
    tot = sum(r.a_end_s - r.a_start_s for r in runs)
    offs = sorted({round(r.b_start_s - r.a_start_s, 1) for r in runs})
    print(f"{a[0]}:{a[1]} vs {b[0]}:{b[1]}: {len(runs)} runs, {tot:.0f}s matched of 900s, offsets {offs[:6]}")
