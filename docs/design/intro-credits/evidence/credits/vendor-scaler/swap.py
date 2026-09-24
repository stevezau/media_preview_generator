"""Coarse rule-J start from the key rows, swapping luma vs boxes between two vendors' rows (same pts)."""
import json, os, sys
sys.path.insert(0, "/home/data/workspace/plex_generate_vid_previews")
from media_preview_generator.markers.credits import rule_j
a_name, b_name = sys.argv[2].split(",")
def rows(d): return [(r[0], r[1], r[2], tuple(tuple(b) for b in r[3])) for r in d["decodes"][0]] if False else [(r[0], r[1], r[2], tuple(tuple(b) for b in r[3])) for r in d["key"][:len(d["decodes"][0])]]
def coarse(rs):
    ov = rule_j.overlay_boxes(rs); c = rule_j.coarse_start(rs, without=rule_j.without_overlays(rs, ov)); return c.pts_s if c else None
for line in open(sys.argv[1]):
    tag = line.split("\t")[0]
    fa, fb = f"{tag}_{a_name}.json", f"{tag}_{b_name}.json"
    if not (os.path.exists(fa) and os.path.exists(fb)): continue
    A, B = rows(json.load(open(fa))), rows(json.load(open(fb)))
    if [r[0] for r in A] != [r[0] for r in B]: print(tag, "pts differ"); continue
    a_boxes_b_luma = [(ra[0], ra[1], rb[2], ra[3]) for ra, rb in zip(A, B)]
    b_boxes_a_luma = [(rb[0], rb[1], ra[2], rb[3]) for ra, rb in zip(A, B)]
    print(f"{tag:10s} coarse {a_name}={coarse(A)}  {b_name}={coarse(B)}  |  {a_name} boxes+{b_name} luma={coarse(a_boxes_b_luma)}  {b_name} boxes+{a_name} luma={coarse(b_boxes_a_luma)}")
