"""Build ``tests/fixtures/markers/credits_rule_j_80.json.gz`` from the local-only evidence (run once, on storage):

    python -m tools.markers_eval.credits_fixture

Anonymised: files become ``movie-01…``/``tv-01…`` in the evidence order, every time is shifted by a whole number of
seconds so the item's tail window starts at 1000 s, rows keep only ``[pts, boxes, luma]``, and the frame-check truth
(``credits/adjudicated.json``) replaces the chapter truth.

The rows carry no box positions, unlike the app's own rows (``rule_j.Row``): the prototype that measured them recorded
how many boxes a frame held and never where they were, and re-measuring these files would replace the very rows the
port is pinned against. A row without positions is read as a frame whose text could be anywhere
(``rule_j.boxes_of``), so none of rule J version 3's three position steps fires here and this fixture still pins every
one of its answers as version 2 gave them; those steps are measured on the harness's decode cache, or on
``credits_synth_lab.json.gz``
(``tools/markers_eval/credits_synth_fixture.py``). Before writing, every item is checked against the prototype
(``credits/eval_rules3.py`` ``detect``, refine span 20 s): the port on the shifted rows must give the prototype's
error within 1.5 ms, or the script stops (an unshifted item would keep real timings; none needed it while planning).

The nine ids in ``PORT_DIVERGENCES`` are exempt from that check, because the port's anchor deliberately no longer
matches the prototype's -- see ``rule_j._run_spacing``, ``rule_j.ANCHOR_MAX_STEPS`` and the anchor's 24 s limit in
``rule_j.coarse_start``. They are reported rather than
stopped, and the fixture stores the prototype's error for them as for every other item;
``tests/markers/credits/test_rule_j.py`` pins the prototype's number *and* the port's number for each of the nine.
Any other item disagreeing still stops the script.
"""

from __future__ import annotations

import gzip
import json
import os
import sys
from pathlib import Path

from media_preview_generator.markers.credits import rule_j

from .data import evidence_dir

OUT = Path(__file__).resolve().parents[2] / "tests/fixtures/markers/credits_rule_j_80.json.gz"
RULE_J_PROTOTYPE = dict(dense=3, min_boxes=1, dark=30, gap=24, run=15, pick="last", anchor=True, bridge_dark=True)
REFINE_SPAN_S = 20.0
TAIL_ORIGIN_S = 1000
# The items the port's anchor deliberately answers differently from the prototype's: ``rule_j._run_spacing`` measures
# the gap between the run's own credit frames in presentation order where the prototype took every row of the whole
# decoded tail in decode order, ``rule_j.ANCHOR_MAX_STEPS`` holds the walk to one frame, and the walk never steps
# over a gap the 24 s join can't bridge. Keep this list in step
# with ``PORT_DIVERGENCES`` in ``tests/markers/credits/test_rule_j.py``, which pins both sides of each.
PORT_DIVERGENCES = {"movie-03", "movie-12", "movie-25", "movie-29", "movie-38", "tv-07", "tv-09", "tv-22", "tv-31"}
ABOUT = (
    "Rule J regression rows for the 80 credits files of spec §5.4, anonymised by tools.markers_eval.credits_fixture. "
    "Each row is [pts, box count, luma]: the prototype these were measured with recorded how many text boxes a frame "
    "held, never where they were, and re-measuring the files would replace the rows the port is pinned against. "
    "These rows carry no positions, so rule J's three position steps find nothing to read here and every answer "
    "below is version 2's. The rule itself does read positions, on rows that carry them: the harness's decode cache "
    "and credits_synth_lab.json.gz."
)


def _prototype(evidence: Path) -> dict:
    source = (evidence / "credits/eval_rules3.py").read_text().split("items = load(sys.argv[1])")[0]
    namespace: dict = {}
    previous = os.getcwd()
    os.chdir(evidence)  # the prototype's load() reads credits/adjudicated.json relative to the evidence folder
    try:
        # The prototype is the owner's local evidence, run once on storage to check the port; never shipped or served.
        exec(compile(source, "eval_rules3.py", "exec"), namespace)  # nosec B102
        namespace["items"] = namespace["load"]("credits/f3.jsonl")
    finally:
        os.chdir(previous)
    return namespace


def _rows(raw: list, shift: int) -> list[list]:
    return [[round(r[0] - shift, 3), int(r[1]), float(r[3])] for r in raw]


def _port_error(item: dict, shift: int) -> tuple[float | None, list, list]:
    key, fine = _rows(item["key"], shift), _rows(item["fine"], shift)
    start = rule_j.credits_start([tuple(r) for r in key], [tuple(r) for r in fine])
    truth = round(item["truth"] - shift, 3)
    return (None if start is None else start - truth), key, fine


def build(evidence: Path) -> dict:
    proto = _prototype(evidence)
    counters = {"movie": 0, "tv": 0}
    items = []
    for item in proto["items"]:
        found = proto["detect"](item["key"], item["fine"], RULE_J_PROTOTYPE, REFINE_SPAN_S)
        expected = None if found is None else round(found - item["truth"], 3)
        shift = round(item["window_start"]) - TAIL_ORIGIN_S
        error, key, fine = _port_error(item, shift)
        counters[item["kind"]] += 1
        item_id = f"{item['kind']}-{counters[item['kind']]:02d}"
        differs = (error is None) != (expected is None) or (error is not None and abs(error - expected) > 0.0015)
        if differs and item_id not in PORT_DIVERGENCES:
            sys.exit(f"the port differs from the prototype on {item_id}")
        if differs:
            print(f"{item_id}: port divergence, prototype {expected} vs port {error}")
        items.append(
            {
                "id": item_id,
                "kind": item["kind"],
                "duration_s": round(item["duration"] - shift, 3),
                "truth_s": round(item["truth"] - shift, 3),
                "key": key,
                "fine": fine,
                "expected_error_s": expected,
            }
        )
    return {"about": ABOUT, "refine_before_s": REFINE_SPAN_S, "items": items}


def main() -> int:
    data = json.dumps(build(evidence_dir()), separators=(",", ":")).encode()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(gzip.compress(data, mtime=0))
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
