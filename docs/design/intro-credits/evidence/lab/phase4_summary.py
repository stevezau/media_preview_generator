#!/usr/bin/env python3
"""One line per row from the result files: result, checks passed, when. No secrets are read or printed.

MLAB_DIR=/path/to/lab-folder ./phase4_summary.py           the 17 phase-4 rows
MLAB_DIR=/path/to/lab-folder ./phase4_summary.py row13     the phase 1-3 regression (phase4_row13_run.sh)
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

# Phase 1's regression is phase 2 row 19: its 16 rows, in the order it runs them (11 is unit-only, 12 is phase 2 row 20,
# 15 is phase 2 row 18). Phase 3 rows 12-15 run on the plex host and are not part of it.
ROW13_FILES = [
    *(f"p2-row-{n:02d}.json" for n in (23, 1, 21, 17, 19, 22, 2, 3, 4, 18, 5, 6, 8, 7, 9, 10, 11, 12, 13, 14, 16, 15, 20, 24)),
    *(f"row-{n:02d}.json" for n in (14, 1, 2, 3, 13, 4, 6, 5, 7, 8, 9, 10, 16, 18, 19, 17)),
    *(f"p3-row-{n:02d}.json" for n in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 16)),
]


def main(argv: list[str]) -> int:
    """Print the summary; the exit code is 1 when a row is missing or didn't pass."""
    row_files = ROW13_FILES if argv == ["row13"] else ROW_FILES
    lab = Path(os.environ.get("MLAB_DIR") or Path(__file__).resolve().parent)
    problems = 0
    for name in row_files:
        path = lab / "results" / name
        if not path.exists():
            print(f"{name:16} MISSING")
            problems += 1
            continue
        data = json.loads(path.read_text())
        # Phase 1's rows keep their checks as a list of notes, not a name -> held mapping.
        checks, premise = (
            (data.get(key) if isinstance(data.get(key), dict) else {}) for key in ("checks", "premise")
        )
        passed = sum(1 for held in checks.values() if held is True)
        held = sum(1 for held in premise.values() if held is True)
        print(
            f"{name:16} {data['result']:16} checks {passed}/{len(checks)} premise {held}/{len(premise)} at {data['at']}"
        )
        problems += data["result"] != "pass"
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
