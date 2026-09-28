"""Scratch: base vs work credit text for files by name, with what each card read (from the work log).

Usage: show_moved.py name-substring...   (no args: every file whose text start moved)
"""

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
base = json.load(open(os.path.join(HERE, "ct_base.json")))
work = json.load(open(os.path.join(HERE, os.environ.get("WORK", "ct_work.json"))))
log = open(os.path.join(HERE, "logs", os.environ.get("WORK", "ct_work.json").replace(".json", ".log"))).read().splitlines()
names = sys.argv[1:]
for path in sorted(set(base) & set(work)):
    b, w = base[path], work[path]
    if "error" in b or "error" in w:
        continue
    name = os.path.basename(path)
    if names and not any(n in name for n in names):
        continue
    if not names and b.get("start_s") == w.get("start_s"):
        continue
    print(name[:70])
    print("   base", b.get("start_s"), b.get("end_s"), "| work", w.get("start_s"), w.get("end_s"), "prose from",
          w.get("prose_start_s"), "| truth", w.get("truth"))  # fmt: skip
    for line in log:
        if name in line and "reads" in line:
            print("     ", re.sub(r".*: the card", "card", line)[:160])
