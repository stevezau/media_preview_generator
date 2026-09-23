"""M5: SDR / HDR10 / Dolby Vision profile per file of the 80- and 205-file credits sets (ffprobe reads only), and rule J
on the 80 per kind. Prints counts only.

    "$PYTHON" measure_hdr.py        (the dev venv's Python, from the repo root)

A file ffprobe can't read (non-zero exit or 60 s timeout) is counted as "unreadable".
"""

import collections
import json
import os
import subprocess
from pathlib import Path

EVIDENCE = Path(__file__).resolve().parents[2]


def hdr_kind(path: str) -> str:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=color_transfer:stream_side_data=dv_profile", "-of", "json", path],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return "unreadable"
    if result.returncode != 0:
        return "unreadable"
    stream = (json.loads(result.stdout or "{}").get("streams") or [{}])[0]
    profiles = [s.get("dv_profile") for s in stream.get("side_data_list") or [] if "dv_profile" in s]
    if profiles:
        return "dv5" if 5 in profiles else "dv_other"
    return "hdr10" if stream.get("color_transfer") in ("smpte2084", "arib-std-b67") else "sdr"


def main() -> None:
    os.chdir(EVIDENCE)
    namespace: dict = {}
    exec(open("credits/eval_rules3.py").read().split("items = load(sys.argv[1])")[0], namespace)  # noqa: S102
    rule_j = dict(dense=3, min_boxes=1, dark=30, gap=24, run=15, pick="last", anchor=True, bridge_dark=True)
    by_kind = collections.defaultdict(collections.Counter)
    for item in namespace["load"]("credits/f3.jsonl"):
        kind = hdr_kind(item["file"])
        start = namespace["detect"](item["key"], item["fine"], rule_j, 20.0)
        tally = by_kind[kind]
        tally["files"] += 1
        if start is None:
            tally["none"] += 1
        else:
            tally["within_10s"] += abs(start - item["truth"]) <= 10
            tally["early"] += start - item["truth"] < -30
    print("80 files:", {k: dict(v) for k, v in sorted(by_kind.items())})
    set205 = json.loads(Path("credits/movie_credit_truth.json").read_text())
    print("205 movies:", dict(collections.Counter(hdr_kind(r["file"]) for r in set205)))


if __name__ == "__main__":
    main()
