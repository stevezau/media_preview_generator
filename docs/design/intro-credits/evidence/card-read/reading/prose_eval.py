"""Scratch: prose rule variants on M2's cards (by kind) and on every card the work run read (ct/logs/ct_work.log).

Usage: prose_eval.py [--all]   (--all: print every run card each variant calls prose)
"""

import ast
import json
import os
import re
import sys

sys.path.insert(0, "/home/data/workspace/plex_generate_vid_previews")
from media_preview_generator.markers.credits import cards  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
WORD = re.compile(r"[^\W\d_]+")


def per_line(lines, min_words=4, words_per_line=3.0):
    """A sentence's words over its lines at least ``words_per_line``: wrapped prose fills its lines, a list doesn't."""
    words = lines_in = 0
    for line in lines:
        tokens = line.split()
        words += len(tokens)
        lines_in += 1
        if tokens and cards._ends_a_sentence(tokens[-1]):
            if words >= min_words and words >= words_per_line * lines_in:
                return True
            words = lines_in = 0
        found = WORD.findall(line)
        if len(found) >= cards.PROSE_WORDS and 2 * sum(w[0].islower() for w in found) > len(found):
            return True
    return False


VARIANTS = {
    "current": cards.is_prose,
    "wpl3": per_line,
    "wpl2.5": lambda lines: per_line(lines, words_per_line=2.5),
    "wpl3.5": lambda lines: per_line(lines, words_per_line=3.5),
}

m2 = json.load(open(os.path.join(HERE, "m2_4.json")))
for name, rule in VARIANTS.items():
    kinds: dict[str, list] = {}
    for r in m2:
        kinds.setdefault(r["kind"], []).append((r["label"], rule(r["lines"])))
    print(f"{name:8}", {k: f"{sum(p for _, p in v)}/{len(v)}" for k, v in kinds.items()},
          "non-epilogue prose", {k: [lab for lab, p in v if p] for k, v in kinds.items() if k != "epilogue"},
          "epilogue missed", [lab for lab, p in kinds.get("epilogue", []) if not p])  # fmt: skip

reads = []
for line in open(os.path.join(HERE, "ct/logs/ct_work.log")):
    m = re.search(r"DEBUG .*? - (.*): the card at ([\d.]+) s reads (\[.*\])$", line.rstrip())
    if m:
        reads.append((m.group(1)[:50], float(m.group(2)), ast.literal_eval(m.group(3))))
print("run cards read", len(reads))
for name, rule in VARIANTS.items():
    if name == "current":
        continue
    flips = [(f, t, lines) for f, t, lines in reads if rule(lines) != cards.is_prose(lines)]
    print(f"== {name}: {len(flips)} cards change")
    for f, t, lines in flips:
        print(f"   {'prose' if rule(lines) else 'NOT  '} {f} {t} {lines[:6]}")
if "--all" in sys.argv:
    for f, t, lines in reads:
        if per_line(lines):
            print("PROSE", f, t, lines[:8])
