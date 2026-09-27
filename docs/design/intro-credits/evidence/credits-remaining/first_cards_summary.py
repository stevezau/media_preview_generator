"""Summarise first_cards.py output: for early and right answers on a card on black, how many cross each candidate
threshold of a prose-card signal (widest line, lines, time on screen). Overlap means no threshold separates them.

Usage: first_cards_summary.py <first_cards.txt>
"""

import re
import sys

LINE = re.compile(r"^(\w+)\s+lines=\s*(\d+) widest=\s*(\d+)px stays=\s*([\d.]+)s")
rows = [m.groups() for m in map(LINE.match, open(sys.argv[1])) if m]
for verdict in ("early", "right"):
    sub = [(int(n), int(w), float(s)) for v, n, w, s in rows if v == verdict]
    print(f"{verdict}: {len(sub)} answers on a card on black")
    for width in (100, 119, 130, 160):
        print(f"   widest line >= {width} px: {sum(w >= width for _, w, _ in sub)}")
    for stays in (4.0, 6.0, 8.0, 15.0):
        print(f"   same line on screen >= {stays:.0f} s: {sum(s >= stays for _, _, s in sub)}")
    for width, stays in ((119, 4.0), (130, 6.0)):
        both = sum(w >= width and s >= stays for _, w, s in sub)
        print(f"   both >= {width} px and >= {stays:.0f} s: {both}")
