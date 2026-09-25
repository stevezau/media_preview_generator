"""Compare ep_sets.py runs: per set tallies, whether two runs give identical answers, the answers that moved, and the
end-picture shares that differ.

    python compare_runs.py <label> ... [--same A B] [--moved A B]

Reads ``<EP_OUT>/<label>.json`` and ``<EP_OUT>/<label>_shares.pkl`` (default ``./local``, gitignored).
"""

import json
import os
import pickle
import sys
from pathlib import Path

RESULTS = Path(os.environ.get("EP_OUT", Path(__file__).parent / "local"))
NAMES = {"lists": "lab 118", "scale_clean": "held-out 175", "accused": "Accused", "chap_clean": "library chapters",
         "bones": "Bones S05-S08"}  # fmt: skip
args = sys.argv[1:]
first_flag = next((i for i, a in enumerate(args) if a.startswith("--")), len(args))
labels = args[:first_flag]
pairs = [(args[i], args[i + 1], args[i + 2]) for i, a in enumerate(args) if a in ("--same", "--moved")]
runs = {label: json.load(open(RESULTS / f"{label}.json")) for label in {*labels, *(x for p in pairs for x in p[1:])}}
shares = {label: pickle.load(open(RESULTS / f"{label}_shares.pkl", "rb")) for label in runs}


def tally(t):
    return f"{t.get('useful', 0)} / {t.get('wrong', 0)} / {t.get('missed', 0)}" + (
        f" (+{t['none-ok']} none-ok)" if t.get("none-ok") else ""
    )


for mode, name in NAMES.items():
    cells = [f"{label}: {tally(runs[label]['sets'][mode]['tally'])}" for label in labels if mode in runs[label]["sets"]]
    print(f"{name:17} " + " | ".join(cells))
for label in labels:
    print(label, "decodes", runs[label]["decodes"], "read failures", len(runs[label]["read_failed"]))

for kind, a, b in pairs:
    print(f"\n== {kind[2:]} {a} -> {b}")
    moved = 0
    for mode, name in NAMES.items():
        rows_a, rows_b = runs[a]["sets"][mode]["rows"], runs[b]["sets"][mode]["rows"]
        assert rows_a.keys() == rows_b.keys(), mode
        for f in sorted(rows_a):
            x, y = rows_a[f], rows_b[f]
            if x["answer"] != y["answer"]:
                moved += 1
                print(f"  {name:17} {os.path.basename(f)[:64]:64} truth {x['truth']} {x['answer']} {x['verdict']} -> "
                      f"{y['answer']} {y['verdict']}")  # fmt: skip
    print(f"  answers moved: {moved}")
    common = shares[a].keys() & shares[b].keys()
    differ = [k for k in common if shares[a][k] != shares[b][k]]
    print(f"  shares: {len(shares[a])} vs {len(shares[b])}, {len(common)} in both, {len(differ)} differ")
    for k in sorted(differ):
        flip = (shares[a][k] is not None and shares[b][k] is not None
                and (shares[a][k] >= 0.75) != (shares[b][k] >= 0.75))  # fmt: skip
        print(f"    {os.path.basename(k[0])[:40]:40} ~ {os.path.basename(k[1])[:40]:40} {k[2]:7.2f}-{k[3]:7.2f} "
              f"off {k[4]:7.2f}: {shares[a][k]} -> {shares[b][k]}{'  (crosses 75%)' if flip else ''}")  # fmt: skip
