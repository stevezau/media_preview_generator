"""Scratch: M2's cards read through the app path, per size: prose calls by kind, time, and disagreements with 6."""

import json
import os
import statistics

HERE = os.path.dirname(os.path.abspath(__file__))
by = {s: {r["label"]: r for r in json.load(open(os.path.join(HERE, f"m2_{s}.json")))} for s in (6, 4, 3)}
for s, rows in by.items():
    kinds: dict[str, list[bool]] = {}
    for r in rows.values():
        kinds.setdefault(r["kind"], []).append(r["prose"])
    summary = {k: f"{sum(v)}/{len(v)}" for k, v in kinds.items()}
    print(s, summary, "median s", round(statistics.median(r["s"] for r in rows.values()), 2),
          "prose credit", [r["label"] for r in rows.values() if r["kind"] == "credit" and r["prose"]],
          "early?", [r["label"] for r in rows.values() if r["kind"] == "early?" and r["prose"]],
          "epilogue missed", [r["label"] for r in rows.values() if r["kind"] == "epilogue" and not r["prose"]])  # fmt: skip
for s in (4, 3):
    diff = [k for k in by[6] if by[6][k]["prose"] != by[s][k]["prose"]]
    print(s, "calls differing from 6:", diff)
