"""Frame sheets around the new prod decisions (read-only media, nice 19 via sheet.py): Westworld S03 intros decided by
season audio, Game of Thrones credits decided with IntroDB, Somebody Somewhere S03 credits decided by credit text."""

from paths import EVIDENCE_DIR, HERE, LOCAL, REPO  # noqa: F401
import json
import os
import re
import subprocess

R = str(LOCAL)
SHEET = f"{HERE}/sheet.py"
os.makedirs(f"{R}/verify", exist_ok=True)
before = json.load(open(f"{R}/final/prod_text_base.json"))
after = json.load(open(f"{R}/final/prod_text_work.json"))
for key, a in sorted(after.items(), key=lambda kv: kv[1]["path"]):
    b = before[key]
    if a["status"] != "decided" or (b["status"], b["marker"]) == (a["status"], a["marker"]):
        continue
    if b["status"] == "decided" and abs(b["marker"][0] - a["marker"][0]) < 5_000 and key.endswith("credits"):
        continue  # a start moved a few seconds by IntroDB joining: not sheeted
    mtype = key.split(":")[1]
    code = re.search(r"S\d\dE\d\d", a["path"]).group(0)
    show = a["path"].split("/")[-3].split(" (")[0].replace(" ", "_")
    out = f"{R}/verify/{show}_{code}_{mtype}.jpg"
    if os.path.exists(out):
        continue
    start, end = a["marker"][0] / 1000, a["marker"][1] / 1000
    specs = [f"start {start:.1f}:{start - 6:.1f}:{start + 6:.1f}:1"]
    if mtype == "intro":
        specs.append(f"end {end:.1f}:{end - 6:.1f}:{end + 6:.1f}:1")
    subprocess.run(["/home/data/.venv/bin/python", SHEET, out, a["path"], *specs], check=True)
    print(out, a["marker"], a["reason"][:60])
