"""Run decide_sets.py on each set, one after another (nice 19), into decide_sets_t3.log."""

import subprocess
from pathlib import Path

HERE = Path(__file__).parent
with open(HERE / "decide_sets_t3.log", "w") as log:
    for name in ("accused", "heldout175", "lab118"):
        subprocess.run(
            ["nice", "-n", "19", "/home/data/.venv/bin/python", str(HERE / "decide_sets.py"), name],
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
        log.flush()
