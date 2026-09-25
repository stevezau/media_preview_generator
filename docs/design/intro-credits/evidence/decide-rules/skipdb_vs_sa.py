"""Season audio vs SkipDB intros that disagree on the end (> 5 s): start difference, which ends first, who is right."""

from paths import EVIDENCE_DIR, HERE, LOCAL, REPO  # noqa: F401
import collections
import json
import os
import sys

sys.path.insert(
    0,
    f"{LOCAL}/base",
)
from tools.markers_eval.score import judge_intro  # noqa: E402

rows = json.load(open(sys.argv[1]))
tally = collections.Counter()
for k, r in sorted(rows.items()):
    sa = [c for c in r["cands"] if c[0] == "season_audio"]
    sk = [c for c in r["cands"] if c[0] == "skipdb"]
    if not sa or not sk or abs(sa[0][2] - sk[0][2]) <= 5000:
        continue
    s, k2 = sa[0], sk[0]
    tr = r["truth"]
    v_sa = judge_intro((s[1] / 1000, s[2] / 1000), tr) if tr else "no-truth"
    v_sk = judge_intro((k2[1] / 1000, k2[2] / 1000), tr) if tr else "no-truth"
    shape = "skipdb-inside" if abs(s[1] - k2[1]) <= 5000 and k2[2] < s[2] else "other"
    tally[(shape, v_sa, v_sk)] += 1
    print(f"{k.split('|')[0][:10]:10s} {os.path.basename(k.split('|', 1)[1])[:50]:50s} SA {s[1] / 1000:6.1f}-{s[2] / 1000:6.1f} "
          f"SkipDB {k2[1] / 1000:6.1f}-{k2[2] / 1000:6.1f} {shape:13s} SA {v_sa:8s} SkipDB {v_sk:8s} truth {tr} ours {r['verdict']}")  # fmt: skip
for k, v in sorted(tally.items()):
    print(k, v)
