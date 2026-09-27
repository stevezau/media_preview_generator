"""Rule J's coarse start and fine rows for one file of vtext_work.json (work tree).

Usage: debug_rule_j.py <substring> [lo hi]
"""

import json
import sys

sys.path.insert(0, __import__("os").environ.get("CREDFIX_WORK", str(__import__("pathlib").Path(__file__).resolve().parents[5])))
from media_preview_generator.markers.credits import rule_j  # noqa: E402
from tools.markers_eval.decode_cache import rows_from_json  # noqa: E402

from common import HERE  # noqa: E402

w = json.load(open(HERE / "vtext_work.json"))
sub = sys.argv[1]
for p, r in w.items():
    if sub not in p:
        continue
    key = rows_from_json(r["key"])
    fine = rows_from_json(r["fine"])
    overlays = [tuple(b) for b in r["overlays"]]
    rows = rule_j.without_overlays(key, overlays)
    coarse = rule_j.coarse_start(key, without=rows)
    print(p.split("/")[-1][:60], "start", r["start_s"], "coarse", coarse)
    lo = float(sys.argv[2]) if len(sys.argv) > 2 else -1
    hi = float(sys.argv[3]) if len(sys.argv) > 3 else 1e9
    for row in sorted(fine, key=lambda x: x[0]):
        if lo <= row[0] <= hi:
            print(f"  fine {row[0]:9.2f} n={row[1]:2d} luma={row[2]:5.1f} credit={rule_j.is_credit(row)}")
