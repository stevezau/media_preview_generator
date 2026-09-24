"""Round-2 re-measure, one job at a time (nice 19): the season-audio harness sets, the decide-level sets with real
frame rates, then Bones (season step + decisions), then the Bones decide-level evidence sets."""

import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).parent
PY = "/home/data/.venv/bin/python"
shutil.copy2(HERE / "answers.json", HERE / "answers_before_r2.json")
with open(HERE / "run_r2.log", "w") as log:
    for name, cmd in (
        ("harness sets", [PY, str(HERE / "regress_t3.py"), "r2"]),
        ("decide sets", [PY, str(HERE / "run_decide_sets_t3.py")]),
        ("bones season step", ["nice", "-n", "19", PY, str(HERE / "bones_eval.py")]),
        ("bones decide", ["nice", "-n", "19", PY, str(HERE / "bones_decide_r2.py")]),
    ):
        log.write(f"=== {name}\n")
        log.flush()
        subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, check=False)
        log.flush()
    log.write("=== done\n")
