#!/usr/bin/env python3
"""One line per phase-4 row from the result files: result, checks passed, when. No secrets are read or printed.

MLAB_DIR=/path/to/lab-folder ./phase4_summary.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROW_FILES = [
    *(f"p4-row-{n:02d}.json" for n in range(1, 13)),
    "row-20.json",
    "row-21.json",
    "p2-row-25.json",
    "p2-row-26.json",
    "p3-row-17.json",
]


def main() -> int:
    """Print the summary; the exit code is 1 when a row is missing or didn't pass."""
    lab = Path(os.environ.get("MLAB_DIR") or Path(__file__).resolve().parent)
    problems = 0
    for name in ROW_FILES:
        path = lab / "results" / name
        if not path.exists():
            print(f"{name:16} MISSING")
            problems += 1
            continue
        data = json.loads(path.read_text())
        checks, premise = data.get("checks", {}), data.get("premise", {})
        passed = sum(1 for held in checks.values() if held is True)
        held = sum(1 for held in premise.values() if held is True)
        print(
            f"{name:16} {data['result']:16} checks {passed}/{len(checks)} premise {held}/{len(premise)} at {data['at']}"
        )
        problems += data["result"] != "pass"
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
