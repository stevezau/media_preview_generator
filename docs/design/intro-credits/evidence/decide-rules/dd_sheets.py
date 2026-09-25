"""Frame sheets for the online-set Daredevil S03E06/E12 credits (credit text + SkipDB + Plex vs the chapter truth)."""

from paths import EVIDENCE_DIR, HERE, LOCAL, REPO  # noqa: F401
import subprocess
import sys
from pathlib import Path

R = str(LOCAL)
sys.path.insert(0, f"{R}/base")
EVIDENCE = Path("/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence")
from tools.markers_eval.online import case_file, load_online  # noqa: E402
from tools.markers_eval.plex import load_baseline  # noqa: E402

baseline = load_baseline(EVIDENCE / "lab/results/scale/prod_plex_markers.json")
results, _ = load_online(EVIDENCE)
SHEET = f"{HERE}/sheet.py"
for r in results:
    c = r["case"]
    if c["show"] == "Marvels Daredevil" and c["episode"] in (6, 12):
        path = case_file(c, baseline.keys())
        t = c["credits_start"]
        out = f"{R}/dd_s03e{c['episode']:02d}_credits.jpg"
        subprocess.run(
            ["/home/data/.venv/bin/python", SHEET, out, path, f"around:{t - 24:.1f}:{t + 8:.1f}:1"], check=True
        )
        print(out, "truth", t)
