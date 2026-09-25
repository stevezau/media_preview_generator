"""Per set: verdicts of intros decided by one source alone (decided_by of length 1, or reason 'single source')."""

from paths import EVIDENCE_DIR, HERE, LOCAL, REPO  # noqa: F401
import collections
import json
import os
import sys

rows = json.load(open(sys.argv[1]))
tally = collections.Counter()
for k, r in sorted(rows.items()):
    if r["seg"] is None or not r["reason"].startswith("single source"):
        continue
    src = r["reason"].split("(")[1].rstrip(")")
    tally[(k.split("|")[0], src, r["verdict"])] += 1
    if src in sys.argv[2:]:
        print(k.split("|")[0], os.path.basename(k.split("|", 1)[1])[:60], r["verdict"], r["seg"], "truth", r["truth"],
              r["cands"])  # fmt: skip
for k, v in sorted(tally.items()):
    print(k, v)
