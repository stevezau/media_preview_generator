import sys
import numpy as np
sys.path.insert(0, "/home/data/workspace/plex_generate_vid_previews")
from media_preview_generator.markers.audio import season as S
from media_preview_generator.markers.audio.matcher import file_hits, intro_candidates
fps = {}
for line in open(sys.argv[1]):
    fid, win, hx = line.strip().split("|")
    fps[(int(fid), win)] = np.frombuffer(bytes.fromhex(hx), dtype="<u4").copy()
def run(target_key):
    pts = {"1940": fps[(1940, "intro")], "1941": fps[(1941, "intro")], "1942": fps[(1942, "intro")], "T": fps[target_key]}
    files = sorted(pts)
    rb = lambda a, b: S.season_pair_runs(pts[a], pts[b])
    hits = file_hits("T", files, rb)
    cands = intro_candidates(hits)
    print(target_key, "candidates:", [(round(c.segment.start_s,1), round(c.segment.end_s,1), c.segment.support) for c in cands])
    seg = S.season_intro("T", files, pts, rb, end_picture_passes=lambda c: True)
    print(target_key, "season_intro ->", seg)
run((1987, "intro@1.042708"))
run((1987, "intro"))
run((1995, "intro"))
