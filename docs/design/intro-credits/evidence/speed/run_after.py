"""After the review: the season-audio regression sets (after only) and the Bones season step + decisions, serially."""

import subprocess
from pathlib import Path

HERE = Path(__file__).parent
PY = "/home/data/.venv/bin/python"
with open(HERE / "run_after.log", "w") as log:
    subprocess.run([PY, str(HERE / "regress.py"), "after"], stdout=log, stderr=subprocess.STDOUT, check=False)
    log.flush()
    subprocess.run(
        ["nice", "-n", "19", PY, str(HERE / "bones_eval.py")],
        stdout=open(HERE / "bones_eval2.log", "w"),
        stderr=subprocess.STDOUT,
        check=False,
    )
    log.write("bones done\n")
