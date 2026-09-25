"""Credit text vs SkipDB when both answer and disagree (> 10 s): which one is right against the truth."""

from paths import EVIDENCE_DIR, HERE, LOCAL, REPO  # noqa: F401
import collections
import json
import os
import sys

sys.path.insert(
    0,
    f"{LOCAL}/base",
)
from tools.markers_eval.credits import judge_credits  # noqa: E402

rows = json.load(open(sys.argv[1]))
tally = collections.Counter()
for k, r in sorted(rows.items()):
    ct = [c for c in r["cands"] if c[0] == "credits_text"]
    sk = [c for c in r["cands"] if c[0] == "skipdb"]
    if not ct or not sk or abs(ct[0][1] - sk[0][1]) <= 10_000:
        continue
    v_ct = judge_credits(ct[0][1] / 1000, r["truth"])
    v_sk = judge_credits(sk[0][1] / 1000, r["truth"])
    tally[(v_ct, v_sk)] += 1
    print(f"{k.split('|')[0][:10]:10s} {os.path.basename(k.split('|', 1)[1])[:50]:50s} CT {ct[0][1] / 1000:8.1f} {v_ct:7s} "
          f"SkipDB {sk[0][1] / 1000:8.1f} {v_sk:7s} truth {r['truth']}")  # fmt: skip
print("(CT verdict, SkipDB verdict):", dict(tally))
