"""For every late credit text answer of the verdict and Plex sets: rule J's coarse start, where the 1 fps refine window
starts, whether the refined start sits on that floor, and the keyframes between the truth and the coarse start.

Usage: late_shapes.py <vtext.json> [--rows]
"""

import json
import os
import sys

sys.path.insert(0, os.environ.get("CREDFIX_WORK", str(__import__("pathlib").Path(__file__).resolve().parents[5])))
from common import ACCURACY as HERE  # noqa: E402

from media_preview_generator.markers.credits import rule_j  # noqa: E402
from tools.markers_eval.decode_cache import rows_from_json  # noqa: E402

text = json.load(open(sys.argv[1]))
items = json.load(open(HERE / "items.json"))
checks = json.load(open(HERE / "checks.json")) if (HERE / "checks.json").exists() else {}
done = set()
for it in items["verdict"] + items["plex"]:
    if it["type"] != "credits" or it["path"] in done:
        continue
    truth = checks.get(it["id"], it.get("truth"))
    r = text.get(it["path"])
    if truth is None or not r or r.get("start_s") is None or "key" not in r:
        continue
    hi = truth.get("hi", truth["start"])
    lo = truth.get("lo", truth["start"])
    off = r["start_s"] - hi if r["start_s"] > hi else r["start_s"] - lo if r["start_s"] < lo else 0.0
    if abs(off) <= 5:
        continue
    done.add(it["path"])
    overlays = [tuple(b) for b in r["overlays"]]
    key = rows_from_json(r["key"])
    rows = rule_j.without_overlays(key, overlays)
    coarse = rule_j.coarse_start(key, without=rows)
    fine = rule_j.without_overlays(rows_from_json(r["fine"]), overlays)
    floor = coarse.pts_s - rule_j.REFINE_BEFORE_S
    window = [row for row in fine if floor <= row[0] <= coarse.pts_s + 1]
    on_floor = bool(window) and abs(r["start_s"] - min(row[0] for row in window)) < 0.01
    between = [row for row in rows if truth["start"] - 1 <= row[0] < coarse.pts_s]
    texted = sum(1 for row in between if row[1] >= 1)
    print(f"{it['id']:5s} off={off:+7.1f} text={r['start_s']} coarse={coarse.pts_s:.1f} floor={floor:.1f} "
          f"on_floor={on_floor} scale={r.get('scale')} keys_between={len(between)} texted={texted} "
          f"{it['path'].split('/')[-1][:50]}")  # fmt: skip
    if "--rows" in sys.argv:
        for row in sorted(between, key=lambda x: x[0]):
            print(f"      {row[0]:9.2f} n={row[1]:2d} luma={row[2]:5.1f} credit={int(rule_j.is_credit(row))}")
