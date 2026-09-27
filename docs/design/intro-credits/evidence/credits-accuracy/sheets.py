"""Frame sheets (1 fps, nice 19, read-only) around moved decisions, for frame checks.

Usage: sheets.py <out_dir> <path> <kind> <old_s> <new_s> [label]   (one sheet)
       sheets.py <out_dir> --diff base.json work.json [--type credits]  (every moved decision)
Each sheet spans min(old,new)-10 .. max(old,new)+10 s; red tile = new, time labels are offsets from new.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(
    0,
    str(Path(__file__).resolve().parent / "local" / "audit" / "work"),
)
from strips import grab, sheet  # noqa: E402

out_dir = Path(sys.argv[1])
out_dir.mkdir(parents=True, exist_ok=True)


def one(path, kind, old_s, new_s, label, name):
    target = out_dir / f"{name}.jpg"
    if target.exists():
        return target
    lo = max(0.0, min(old_s, new_s) - 10)
    hi = max(old_s, new_s) + 10
    if hi - lo > 70:  # a long move: two windows
        rows = [
            (f"OLD {old_s:.1f}", old_s, grab(path, max(0.0, old_s - 10), 21)),
            (f"NEW {new_s:.1f}", new_s, grab(path, max(0.0, new_s - 10), 21)),
        ]
    else:
        rows = [(f"{kind}: old {old_s:.1f} ({old_s - new_s:+.1f}) new {new_s:.1f}", new_s, grab(path, lo, hi - lo))]
    sheet(f"{label}  {os.path.basename(path)[:70]}", rows, str(target), width=200, cols=9)
    return target


if sys.argv[2] == "--diff":
    a = json.load(open(sys.argv[3]))
    b = json.load(open(sys.argv[4]))
    want = sys.argv[sys.argv.index("--type") + 1] if "--type" in sys.argv else None
    keys = set(sys.argv[sys.argv.index("--keys") + 1].split(",")) if "--keys" in sys.argv else None
    for key in sorted(a):
        if keys is not None and key not in keys:
            continue
        x, y = a[key], b.get(key)
        if y is None or x["status"] != "decided" or y["status"] != "decided":
            continue
        kind = key.split(":")[1]
        if want and kind != want:
            continue
        i = 0 if kind == "credits" else 1
        old_s, new_s = x["marker"][i] / 1000, y["marker"][i] / 1000
        other = 1 - i
        if abs(old_s - new_s) <= 1 and abs(x["marker"][other] - y["marker"][other]) <= 1000:
            continue
        if abs(old_s - new_s) <= 1:
            i = other
            old_s, new_s = x["marker"][i] / 1000, y["marker"][i] / 1000
        print(one(x["path"], kind, old_s, new_s, key, key.replace(":", "_")), flush=True)
else:
    path, kind, old_s, new_s = sys.argv[2], sys.argv[3], float(sys.argv[4]), float(sys.argv[5])
    label = sys.argv[6] if len(sys.argv) > 6 else ""
    print(one(path, kind, old_s, new_s, label, label or f"{os.path.basename(path)[:30]}_{new_s:.0f}"))
