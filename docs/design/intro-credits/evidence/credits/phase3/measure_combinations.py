"""M0: rule J with refine spans 10/15/20 s, and credits text + Plex through decide() on the 80 files (counts only)."""

import collections
import os
import sys
from pathlib import Path

EVIDENCE = Path(__file__).resolve().parents[2]
REPO = EVIDENCE.parents[3]
os.chdir(EVIDENCE)
sys.path.insert(0, str(REPO))
namespace: dict = {}
exec(open("credits/eval_rules3.py").read().split("items = load(sys.argv[1])")[0], namespace)  # noqa: S102

from media_preview_generator.markers import decide as D  # noqa: E402
from media_preview_generator.markers.models import Candidate, MarkerType, Source  # noqa: E402
from tools.markers_eval.credits import judge_credits  # noqa: E402
from tools.markers_eval.decisions import ORDER  # noqa: E402
from tools.markers_eval.plex import first_marker, load_baseline, server_candidates  # noqa: E402

RULE_J = dict(dense=3, min_boxes=1, dark=30, gap=24, run=15, pick="last", anchor=True, bridge_dark=True)
items = namespace["load"]("credits/f3.jsonl")
detect, runs = namespace["detect"], namespace["runs"]
for span in (10.0, 15.0, 20.0):
    tally = collections.Counter()
    for it in items:
        start = detect(it["key"], it["fine"], RULE_J, span)
        if start is None:
            tally["none"] += 1
            continue
        err = start - it["truth"]
        tally["within_10s"] += abs(err) <= 10
        tally["early_10_30"] += -30 <= err < -10
        if abs(err) > 30:
            tally["early" if err < 0 else "late"] += 1
    print("span", span, dict(tally))

baseline = load_baseline(EVIDENCE / "lab/results/scale/prod_plex_markers.json")
rows = {name: collections.Counter() for name in ("plex", "text", "high", "medium_alone", "medium_agree_only")}
tail_gaps = []
for it in items:
    start = detect(it["key"], it["fine"], RULE_J, 20.0)
    found = runs(it["key"], RULE_J)
    if found:
        tail_gaps.append(it["duration"] - it["key"][found[-1][1]][0])
    markers = baseline.get(it["file"], [])
    plex = first_marker(markers, MarkerType.CREDITS)
    rows["plex"][judge_credits(plex.start_ms / 1000 if plex else None, it["truth"])] += 1
    rows["text"][judge_credits(start, it["truth"])] += 1
    candidates = server_candidates(markers, MarkerType.CREDITS)
    if start is not None:
        candidates.append(Candidate(MarkerType.CREDITS, int(start * 1000), None, Source.CREDITS_TEXT))
    for name, level, alone in (("high", "high", True), ("medium_alone", "medium", True), ("medium_agree_only", "medium", False)):
        saved = D._AGREEMENT_ONLY
        if not alone:
            D._AGREEMENT_ONLY = saved | {Source.CREDITS_TEXT}
        ctx = D.DecisionContext(int(it["duration"] * 1000), it["kind"] == "movie", level, frozenset({MarkerType.CREDITS}), ORDER)
        decision = D.decide(candidates, ctx, {})[MarkerType.CREDITS]
        D._AGREEMENT_ONLY = saved
        decided = decision.marker.start_ms / 1000 if decision.status is D.DecisionStatus.DECIDED else None
        rows[name][judge_credits(decided, it["truth"])] += 1
for name, tally in rows.items():
    print(name, dict(sorted(tally.items())))
tail_gaps.sort()
print("last run end to end of file: n", len(tail_gaps), "within 30 s", sum(g <= 30 for g in tail_gaps), "max", round(tail_gaps[-1]))
