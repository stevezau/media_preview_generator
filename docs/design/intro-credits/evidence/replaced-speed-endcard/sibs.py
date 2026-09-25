import sys
import numpy as np
sys.path.insert(0, "/home/data/workspace/plex_generate_vid_previews")
from media_preview_generator.markers.audio import season as S
fps = {}
for line in open(sys.argv[1]):
    fid, win, hx = line.strip().split("|")
    fps[(int(fid), win)] = np.frombuffer(bytes.fromhex(hx), dtype="<u4").copy()
for a, b in [(1940, 1941), (1940, 1942), (1941, 1942)]:
    runs = S.season_pair_runs(fps[(a, "intro")], fps[(b, "intro")])
    print(a, b, [tuple(round(x, 1) for x in r) for r in runs])
# whole group at own speed, each episode's answer
pts = {str(k[0]): v for k, v in fps.items() if k[1] == "intro" and k[0] in (1940, 1941, 1942, 1987)}
files = sorted(pts)
rb = lambda x, y: S.season_pair_runs(pts[x], pts[y])
for t in files:
    print("own-speed group, target", t, "->", S.season_intro(t, files, pts, rb, end_picture_passes=lambda c: True))
