"""Verification only: vendor-vs-vendor comparison of verify.py's outputs. Usage: compare.py OUTDIR TAG [TAG...]"""

import json
import os
import sys

import numpy as np

VENDORS = ("vaapi", "cuda", "cpu")
PAIRS = (("vaapi", "cuda"), ("vaapi", "cpu"), ("cuda", "cpu"))
SHAPES = {"tail1": (180, 320), "tail2": (360, 640)}


def load(out, tag, vendor):
    path = os.path.join(out, f"{tag}_{vendor}.json")
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        return json.load(fh)


def planes(out, tag, vendor, phase):
    h, w = SHAPES[phase]
    path = os.path.join(out, f"{tag}_{vendor}_{phase}.u8")
    data = np.fromfile(path, dtype=np.uint8)
    return data.reshape(-1, h, w)


def main() -> None:
    out = sys.argv[1]
    report = {}
    for tag in sys.argv[2:]:
        res = {v: load(out, tag, v) for v in VENDORS}
        entry = {"answers": {}, "timing_vaapi": None, "pairs": {}}
        for v, r in res.items():
            if r is None:
                entry["answers"][v] = "missing"
                continue
            f = r["phases"]["find"]
            entry["answers"][v] = {
                "start_s": f["start_s"],
                "end_s": f["end_s"],
                "scale": f["scale"],
                "error": f["error"],
                "frames": f["frames"],
                "tail_errors": [r["phases"][p]["error"] for p in ("tail1", "tail2")],
            }
        for a, b in PAIRS:
            ra, rb = res[a], res[b]
            if ra is None or rb is None:
                continue
            pair = {}
            for phase in ("tail1", "tail2"):
                pa, pb = planes(out, tag, a, phase), planes(out, tag, b, phase)
                rows_a, rows_b = ra["phases"][phase]["rows"], rb["phases"][phase]["rows"]
                cell = {"frames": [len(pa), len(pb)]}
                if pa.shape == pb.shape and len(pa):
                    diff = np.abs(pa.astype(np.int16) - pb.astype(np.int16))
                    per_frame = diff.reshape(len(diff), -1).max(axis=1)
                    cell["max_abs"] = int(diff.max())
                    cell["frames_differing"] = int((per_frame > 0).sum())
                    cell["pixels_differing"] = int((diff > 0).sum())
                else:
                    cell["max_abs"] = None
                cell["pts_equal"] = [x[0] for x in rows_a] == [x[0] for x in rows_b]
                cell["counts_equal"] = [x[1] for x in rows_a] == [x[1] for x in rows_b]
                cell["boxes_equal"] = [x[3] for x in rows_a] == [x[3] for x in rows_b]
                cell["luma_equal"] = [x[2] for x in rows_a] == [x[2] for x in rows_b]
                cell["boxes_total"] = [sum(x[1] for x in rows_a), sum(x[1] for x in rows_b)]
                if not cell["pts_equal"]:
                    ta, tb = [x[0] for x in rows_a], [x[0] for x in rows_b]
                    cell["pts_first_diff"] = next(
                        (
                            [i, ta[i] if i < len(ta) else None, tb[i] if i < len(tb) else None]
                            for i in range(max(len(ta), len(tb)))
                            if (ta[i] if i < len(ta) else None) != (tb[i] if i < len(tb) else None)
                        ),
                        None,
                    )
                pair[phase] = cell
            fa, fb = ra["phases"]["find"], rb["phases"]["find"]
            pair["find"] = {
                "answer_equal": (fa["start_s"], fa["end_s"], fa["scale"]) == (fb["start_s"], fb["end_s"], fb["scale"]),
                "plane_hashes_equal": fa["hashes"] == fb["hashes"],
                "frames": [len(fa["hashes"]), len(fb["hashes"])],
                "rows_equal": all(
                    fa.get(k) == fb.get(k) for k in ("key_rows", "fine_rows", "end_rows", "run_rows", "overlays")
                ),
            }
            entry["pairs"][f"{a}-{b}"] = pair
        v = res["vaapi"]
        if v is not None:
            entry["timing_vaapi"] = {
                "find_s": v["phases"]["find"]["secs"],
                "find_detect_s": v["phases"]["find"]["detect_s"],
                "tail1_s": v["phases"]["tail1"]["secs"],
                "tail1_detect_s": v["phases"]["tail1"]["detect_s"],
                "tail2_s": v["phases"]["tail2"]["secs"],
                "tail2_detect_s": v["phases"]["tail2"]["detect_s"],
                "mem_peaks_kib": v["mem_peaks_kib"],
                "ru_maxrss_self_kib": v["ru_maxrss_self_kib"],
                "ru_maxrss_children_kib": v["ru_maxrss_children_kib"],
                "extra_hw_frames_in_all_gpu_cmds": all("-extra_hw_frames" in c["argv"] for c in v["commands"]),
                "filters": sorted({c["argv"][c["argv"].index("-vf") + 1] for c in v["commands"]}),
            }
        for vend in ("cuda", "cpu"):
            r = res[vend]
            if r is not None:
                entry[f"timing_{vend}"] = {
                    "find_s": r["phases"]["find"]["secs"],
                    "tail1_s": r["phases"]["tail1"]["secs"],
                    "tail2_s": r["phases"]["tail2"]["secs"],
                    "filters": sorted({c["argv"][c["argv"].index("-vf") + 1] for c in r["commands"]}),
                    "extra_hw_frames_any": any("-extra_hw_frames" in c["argv"] for c in r["commands"]),
                }
        report[tag] = entry
    json.dump(report, sys.stdout, indent=1)


if __name__ == "__main__":
    main()
