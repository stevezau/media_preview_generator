"""The keyframes from an answer's start to the truth for every answer more than 5 s early (credit text alone), so each
early start can be read against what the frames show: set, file, start, truth, then one line per keyframe.

Usage: early_rows.py <answers.json> [set name]   (allsets.py output; vtext.py output with the audit's truth)
"""

import json
import sys

from common import audit_truths, rule_rows, verdict

data = json.load(open(sys.argv[1]))
only = sys.argv[2] if len(sys.argv) > 2 else None
truths = audit_truths()
for path, r in sorted(data.items(), key=lambda kv: kv[0].split("/")[-1]):
    if "key" not in r or r.get("start_s") is None or (only and r.get("set") != only):
        continue
    truth = r.get("truth", truths.get(path))
    if truth is None or verdict(r["start_s"], truth) != "early":
        continue
    t = truth if not isinstance(truth, dict) else truth.get("lo", truth["start"])
    print(f"{r.get('set', 'vtext')} {path.split('/')[-1][:70]} start={r['start_s']} truth={t} scale={r.get('scale')}")
    _, rows, _ = rule_rows(r)
    for row in sorted(rows, key=lambda x: x[0]):
        if r["start_s"] - 4 <= row[0] <= t + 4:
            widths = ",".join(str(b[2] - b[0] + 1) for b in row[3])
            print(f"    {row[0]:9.2f} n={row[1]:2d} luma={row[2]:5.1f} widths={widths}")
