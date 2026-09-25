"""Credits sets at decide level with one tree's decide(): movies40 + tv40 (the 80 hand-checked) and the 205-movie set
(movie_credit_truth), from the ae3 harness run's details (credit text answers as the app gives them now).

Sources: credit text, Plex's own markers (the lab scale run's prod dump), SkipDB from its dump, and with ``--chapters``
the file's chapters (the truth is the last credits chapter, hand-checked on the 80, so a chapter is right by
construction there: what rules can do is lose it). Verdict: credits.judge_credits (> 10 s early wrong, > 30 s late).

Usage: credits_sets.py <base|work> [--chapters] [--no-skipdb] [--json out.json]
"""

from paths import EVIDENCE_DIR, HERE, LOCAL, REPO  # noqa: F401
import collections
import json
import os
import re
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
from media_preview_generator.markers import decide as D  # noqa: E402
from media_preview_generator.markers.models import Candidate, MarkerType, Source  # noqa: E402
from media_preview_generator.markers.sources import skipdb  # noqa: E402
from media_preview_generator.markers.sources.chapters import chapter_candidates  # noqa: E402
from tools.markers_eval.cache import ProbeCache  # noqa: E402
from tools.markers_eval.credits import judge_credits  # noqa: E402
from tools.markers_eval.online import skipdb_segments  # noqa: E402
from tools.markers_eval.plex import first_marker, load_baseline, server_candidates  # noqa: E402

assert D.__file__.startswith(tree)
if "--variant" in sys.argv:
    sys.path.insert(
        0,
        str(LOCAL),
    )
    import variants  # noqa: E402

    variants.apply(D, sys.argv[sys.argv.index("--variant") + 1])
WITH_CHAPTERS = "--chapters" in sys.argv
WITH_SKIPDB = "--no-skipdb" not in sys.argv
ORDER = ("chapters", "introdb", "skipdb", "season_audio", "season_audio_previous", "credits_text", "server_markers",
         "server_markers_imported")  # fmt: skip
probes = ProbeCache(Path.home() / ".cache/markers_eval", ffprobe="/usr/bin/ffprobe")
baseline = load_baseline(EVIDENCE / "lab/results/scale/prod_plex_markers.json")
details = json.load(open(f"{S}/ae3_gpu.json"))["details"]
IMDB = json.load(open(f"{R}/imdb_by_tvdb.json"))
_BY_KEY = collections.defaultdict(list)
for seg in json.load(open(EVIDENCE / "online/skipdb-dump.json"))["segments"]:
    _BY_KEY[(seg["imdb_id"], seg.get("season"), seg.get("episode"))].append(seg)


def skipdb_credits(path, duration_ms, is_movie):
    if not WITH_SKIPDB:
        return []
    if is_movie:
        m = re.search(r"\{imdb-(tt\d+)\}", path)
        if not m:
            return []
        imdb, season, episode = m.group(1), None, None
    else:
        t = re.search(r"\{tvdb-(\d+)\}", path)
        e = re.search(r"S(\d+)E(\d+)", os.path.basename(path))
        if not t or not e or not IMDB.get(t.group(1)):
            return []
        imdb, season, episode = IMDB[t.group(1)], int(e.group(1)), int(e.group(2))
    case = {"imdb": imdb, "season": season, "episode": episode, "dur": duration_ms / 1000}
    rows = _BY_KEY.get((imdb, season, episode), [])
    return [c for c in skipdb._candidates(skipdb_segments(case, rows)) if c.type is MarkerType.CREDITS]


out = {}
for name, is_movie in (("movies40", True), ("tv40", False), ("movie_credit_truth", True)):
    tally = {"plex": collections.Counter(), "ours": collections.Counter()}
    for row in details[name]:
        path = row["file"]
        duration_ms = round(row["duration"] * 1000)
        cands = []
        if row["text"] is not None:
            end = None if row["text_end"] is None else round(row["text_end"] * 1000)
            cands.append(Candidate(MarkerType.CREDITS, round(row["text"] * 1000), end, Source.CREDITS_TEXT))
        if "--no-plex" not in sys.argv:
            cands += server_candidates(baseline.get(path, []), MarkerType.CREDITS)
        cands += skipdb_credits(path, duration_ms, is_movie)
        if WITH_CHAPTERS:
            cands += [c for c in chapter_candidates(probes.probe(path), is_episode=not is_movie)
                      if c.type is MarkerType.CREDITS]  # fmt: skip
        # Credit text read every file; where it found nothing the pipeline leaves it out of the order (it answered).
        order = ORDER if row["text"] is not None else tuple(s for s in ORDER if s != "credits_text")
        ctx = D.DecisionContext(duration_ms, is_movie, "medium", frozenset({MarkerType.CREDITS}), order)
        d = D.decide(cands, ctx, {})[MarkerType.CREDITS]
        seg = (d.marker.start_ms / 1000, d.marker.end_ms / 1000) if d.status is D.DecisionStatus.DECIDED else None
        verdict = "missed" if seg is None else judge_credits(seg[0], row["truth"])
        first = first_marker(baseline.get(path, []), MarkerType.CREDITS)
        tally["ours"][verdict] += 1
        tally["plex"]["missed" if first is None else judge_credits(first.start_ms / 1000, row["truth"])] += 1
        out[f"{name}|{path}"] = {"verdict": verdict, "seg": seg, "reason": d.reason, "truth": row["truth"],
                                 "by": list(d.marker.decided_by) if seg else None,
                                 "cands": [(c.source.value, c.start_ms, c.end_ms, c.stale) for c in cands]}  # fmt: skip
    for label in ("plex", "ours"):
        c = tally[label]
        print(f"{name:18s} {label:5s} useful {c['useful']:3d} late {c['late']:3d} wrong {c['wrong']:3d} "
              f"missed {c['missed']:3d}")  # fmt: skip
if "--json" in sys.argv:
    json.dump(out, open(sys.argv[sys.argv.index("--json") + 1], "w"), indent=0)
