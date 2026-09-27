"""What the card an answer starts on looks like, for answers that start on a card on black: right ones against ones
early on epilogue or story text. Looks for a box-level signal that tells a prose card (an epilogue, a verdict) from a
credit card: how many lines, how wide the widest (in 320 px), and how long the card's widest line stays on screen on
the keyframes (IoU >= 0.5).

Usage: first_cards.py <answers.json> [<answers.json> ...]   (vtext.py or allsets.py output)
"""

import json
import sys

from common import audit_truths, rule_j, rule_rows, verdict

truths = audit_truths()
rows_out = []
for name in sys.argv[1:]:
    for path, r in json.load(open(name)).items():
        if "key" not in r or r.get("start_s") is None:
            continue
        truth = r.get("truth", truths.get(path))
        if truth is None:
            continue
        v = verdict(r["start_s"], truth)
        if v == "late" or v == "none":
            continue
        key, rows, _ = rule_rows(r)
        by_time = sorted(rows, key=lambda row: row[0])
        card = next((row for row in by_time if row[0] >= r["start_s"] - 1.0 and row[1] >= 1), None)
        if card is None or card[2] >= rule_j.RULE_J.dark:
            continue
        widest = max(rule_j.boxes_of(card), key=lambda b: b[2] - b[0], default=None)
        if widest is None:
            continue
        stays = card[0]
        for row in by_time:
            if row[0] <= card[0]:
                continue
            if any(rule_j._iou(widest, b) >= 0.5 for b in rule_j.boxes_of(row)):
                stays = row[0]
            else:
                break
        rows_out.append((v, card[1], widest[2] - widest[0] + 1, round(stays - card[0], 1), r.get("set", "vtext"),
                         path.split("/")[-1][:50]))  # fmt: skip
for row in sorted(rows_out):
    print(f"{row[0]:6s} lines={row[1]:2d} widest={row[2]:3d}px stays={row[3]:5.1f}s {row[4]:18s} {row[5]}")
