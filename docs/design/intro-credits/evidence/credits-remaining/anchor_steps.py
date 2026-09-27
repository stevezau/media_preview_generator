"""Every file whose rule J start the anchor moved (the one step over a credit frame the 24 s join may have glued on),
with what it stepped over and the truth, from stored answers (320x180 answers only).

Usage: anchor_steps.py <answers.json> [<answers.json> ...]   (vtext.py or allsets.py output)
"""

import json
import sys

from common import audit_truths, rule_j, rule_rows, verdict

truths = audit_truths()
for name in sys.argv[1:]:
    for path, r in json.load(open(name)).items():
        if "key" not in r or r.get("start_s") is None or r.get("scale", 1) != 1:
            continue
        key, rows, fine = rule_rows(r)
        runs = rule_j.credit_runs(key)
        if not runs:
            continue
        bounds = rule_j._credit_bounds(rows, *runs[-1], rule_j.RULE_J)
        if bounds is None:
            continue
        anchored = rule_j._anchored(rows, *bounds, rule_j.RULE_J)
        if anchored == bounds[0]:
            continue
        first, nxt = rows[bounds[0]], rows[anchored]
        between = [row for row in rows if first[0] < row[0] < nxt[0]]
        all_dark = all(row[2] < rule_j.RULE_J.dark for row in between)
        truth = r.get("truth", truths.get(path))
        v_now = verdict(r["start_s"], truth) if truth is not None else "?"
        v_first = verdict(first[0], truth) if truth is not None else "?"
        t = truth if not isinstance(truth, dict) else truth.get("lo", truth["start"])
        print(f"{r.get('set', 'vtext'):18s} first={first[0]:8.1f} n={first[1]:2d} luma={first[2]:5.1f} "
              f"next={nxt[0]:8.1f} between={len(between):2d} all_dark={int(all_dark)} start={r['start_s']:8.1f} "
              f"truth={t} now={v_now} on_first={v_first} {path.split('/')[-1][:48]}")  # fmt: skip
