"""The 43 verified online cases at decide level with one tree's decide(), Medium, both app orders (TheIntroDB off/on).

Rows: the recorded online answers alone ("online"), and with the file's chapters, Plex's own markers (the lab scale
run's prod dump) and its credit text answer (the harness cache from the ae3 run; a miss is reported, never decoded)
("full"). Verdicts follow the audit's rules (online.judge_online): intro wrong > 5 s outside the truth; credits wrong
> 10 s early, late > 30 s late.

Usage: online_set.py <base|work> [--json out.json]
"""

from paths import EVIDENCE_DIR, HERE, LOCAL, REPO  # noqa: F401
import collections
import json
import os
import sys
from pathlib import Path

S = str(LOCAL)
R = S
TREES = {
    "base": f"{R}/base",
    "work": str(REPO),
}
tree = TREES[sys.argv[1]]
sys.path.insert(0, tree)
EVIDENCE = Path("/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence")
os.environ["MARKERS_EVAL_EVIDENCE"] = str(EVIDENCE)
from loguru import logger  # noqa: E402

from media_preview_generator.markers import decide as D  # noqa: E402
from media_preview_generator.markers.models import MarkerType  # noqa: E402
from media_preview_generator.markers.sources.chapters import chapter_candidates  # noqa: E402
from tools.markers_eval.cache import FingerprintCache, ProbeCache  # noqa: E402
from tools.markers_eval.credits_text import CreditsTextCache, text_candidates  # noqa: E402
from tools.markers_eval.online import (  # noqa: E402
    DEFAULT_ORDER,
    THEINTRODB_ORDER,
    case_file,
    case_key,
    load_online,
    online_verdicts,
    tally,
)
from tools.markers_eval.plex import load_baseline, server_candidates  # noqa: E402

logger.remove()
assert D.__file__.startswith(tree)
if "--variant" in sys.argv:
    sys.path.insert(
        0,
        str(LOCAL),
    )
    import variants  # noqa: E402

    variants.apply(D, sys.argv[sys.argv.index("--variant") + 1])
ROOT = Path.home() / ".cache/markers_eval"
probes = ProbeCache(ROOT, ffprobe="/usr/bin/ffprobe")
fps = FingerprintCache(ROOT, ffmpeg="/usr/bin/ffmpeg", ffprobe="/usr/bin/ffprobe")
baseline = load_baseline(EVIDENCE / "lab/results/scale/prod_plex_markers.json")
TEXT = json.load(open(f"{R}/online_text.json"))
results, dump = load_online(EVIDENCE)
found = {case_key(r["case"]): case_file(r["case"], baseline.keys()) for r in results}
extra, rates, misses = {}, {}, 0
for key, path in found.items():
    if not path:
        continue
    cands = server_candidates(baseline[path])
    cands += chapter_candidates(probes.probe(path), is_episode=True)
    answer = TEXT.get("|".join(map(str, key)))
    if answer is None:
        misses += 1
    else:
        cands += text_candidates(answer[0], answer[1])
    extra[key] = cands
    rates[key] = fps.frame_rate(path)
print("cases", len(results), "files", sum(1 for p in found.values() if p), "credit text cache misses", misses)
out = {}
for label, order in (("default", DEFAULT_ORDER), ("theintrodb", THEINTRODB_ORDER)):
    for kind, more in (("online", None), ("full", extra)):
        v = online_verdicts(results, dump, order=order, level="medium", extra=more, frame_rates=rates)
        t = tally(v)
        print(
            f"{label:10s} {kind:6s} intro {dict(sorted(t['intro'].items()))}  credits {dict(sorted(t['credits'].items()))}"
        )
        for row in v:
            k = f"{label}/{kind}|{row['show']} S{row['season']:02d}E{row['episode']:02d} {row['type']}"
            out[k] = {"verdict": row["verdict"], "seg": row["segment"], "reason": row["reason"],
                      "truth": None, "by": list(row["decided_by"]), "cands": []}  # fmt: skip
if "--json" in sys.argv:
    json.dump(out, open(sys.argv[sys.argv.index("--json") + 1], "w"), indent=0)
collections.Counter()
