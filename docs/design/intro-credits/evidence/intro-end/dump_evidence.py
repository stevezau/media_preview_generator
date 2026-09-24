"""Decide-level regression with real frame rates: season audio (the app's step, matched at one speed as the app now
does) + IntroDB's raw intro + Plex's own intro marker, decided by the worktree's decide() at the app's rules, before
(no frame rate: what dev decides) and after (the file's own rate). Chapters are left out (they are the truth on the lab
set). Media is only read (nice 19); IntroDB answers come from introdb_sets.json.

Usage: decide_sets.py lab118|heldout175|accused
"""

import collections
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-a0d2f9fd9105568a3")
EVIDENCE = "/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence"
os.environ.setdefault("MARKERS_EVAL_EVIDENCE", EVIDENCE)
from loguru import logger  # noqa: E402

from media_preview_generator.markers.audio import end_picture  # noqa: E402
from media_preview_generator.markers.audio.season import season_group  # noqa: E402
from media_preview_generator.markers.decide import DecisionContext, DecisionStatus, decide  # noqa: E402
from media_preview_generator.markers.models import Candidate, MarkerType, Source  # noqa: E402
from tools.markers_eval.cache import FingerprintCache, ProbeCache  # noqa: E402
from tools.markers_eval.data import EvalEpisode, load_v3_results  # noqa: E402
from tools.markers_eval.intros import DecodedEndPictures, ReproductionReport, SeasonStep  # noqa: E402
from tools.markers_eval.plex import first_marker, load_baseline, server_candidates  # noqa: E402
from tools.markers_eval.score import judge_intro, skips_story  # noqa: E402

logger.remove()
HERE = Path("/tmp/claude-1000/-home-data-workspace-plex-generate-vid-previews/221c1481-5d66-4e86-8c40-a12113e50030/scratchpad/bones")
FFMPEG = "/usr/bin/ffmpeg"
ORDER = (
    "chapters",
    "introdb",
    "skipdb",
    "season_audio",
    "season_audio_previous",
    "credits_text",
    "server_markers",
    "server_markers_imported",
)  # the app's default order
ROOT = Path.home() / ".cache/markers_eval"
cache = FingerprintCache(ROOT, ffmpeg=FFMPEG, ffprobe="/usr/bin/ffprobe")
probes = ProbeCache(ROOT, ffprobe="/usr/bin/ffprobe")
end_pictures = DecodedEndPictures(end_picture.Reader(ffmpeg=FFMPEG, gpu="NVIDIA", gpu_device_path="cuda:0"))
plex = load_baseline(Path(EVIDENCE) / "lab/results/scale/prod_plex_markers.json")
introdb = json.load(open(HERE / "introdb_sets.json"))

name = sys.argv[1]
if name == "lab118":
    by_season = collections.defaultdict(list)
    for e in load_v3_results():
        by_season[e.season].append(e)
    groups = [
        ([e.file for e in eps], {e.file: e.truth_intro for e in eps if e.truth_intro}) for eps in by_season.values()
    ]
else:
    path = HERE / "heldout_truth.json" if name == "heldout175" else Path(EVIDENCE) / ("eval/accused_truth.json")
    truth = {f: tuple(t) if t else None for f, t in json.load(open(path)).items()}
    grouped = collections.defaultdict(dict)
    for f, t in truth.items():
        grouped[season_group(f).episodes][f] = t
    groups = [(sorted(set(episodes) | set(rows)), rows) for episodes, rows in grouped.items()]


rows_out = {}
for files, rows in groups:
    step = SeasonStep(
        os.path.dirname(files[0]),
        {f: cache.points(f) for f in files},
        ReproductionReport(),
        end_pictures,
        speed=cache.speed,
        retimed=cache.retimed,
    )
    for f, tr in rows.items():
        answer = step.answer(EvalEpisode(os.path.dirname(f), f, tr, None, None))
        speed = cache.speed(f)
        duration = probes.probe(f).duration_ms
        idb = [[c[1], c[2]] for c in introdb.get(f, {}).get("candidates", []) if c[0] == "intro"][:1]
        srv = [[c.start_ms, c.end_ms] for c in server_candidates(plex.get(f, []), MarkerType.INTRO)]
        first = first_marker(plex.get(f, []), MarkerType.INTRO)
        rows_out[f] = {
            "truth": list(tr) if tr else None,
            "audio": list(answer) if answer else None,
            "speed": speed,
            "duration_ms": duration,
            "introdb": idb,
            "plex_server": srv,
            "plex_first": [first.start_ms, first.end_ms] if first else None,
            "group": files,
        }
json.dump(rows_out, open(f"evidence_{name}.json", "w"), indent=1, default=str)
print(name, len(rows_out))
