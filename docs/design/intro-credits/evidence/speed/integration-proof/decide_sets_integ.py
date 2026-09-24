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

tally = collections.defaultdict(collections.Counter)
story = collections.Counter()
changed, rates = [], collections.Counter()
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
        rates[speed] += 1
        duration = probes.probe(f).duration_ms
        cands = [
            Candidate(MarkerType.INTRO, c[1], c[2], Source.INTRODB)
            for c in introdb.get(f, {}).get("candidates", [])
            if c[0] == "intro"
        ][:1]
        cands += server_candidates(plex.get(f, []), MarkerType.INTRO)
        if answer:
            cands.append(
                Candidate(
                    MarkerType.INTRO,
                    round(answer[0] * 1000),
                    round(answer[1] * 1000),
                    Source.SEASON_AUDIO,
                    1.0,
                    f"{answer[2]}",
                )
            )
        got = {}
        for label, rate in (("before", None), ("after", speed)):
            ctx = DecisionContext(duration, False, "medium", frozenset({MarkerType.INTRO}), ORDER, frame_rate=rate)
            d = decide(cands, ctx, {})[MarkerType.INTRO]
            got[label] = (
                (d.marker.start_ms / 1000, d.marker.end_ms / 1000) if d.status is DecisionStatus.DECIDED else None
            )
        first = first_marker(plex.get(f, []), MarkerType.INTRO)
        got["plex"] = (first.start_ms / 1000, first.end_ms / 1000) if first else None
        for label, seg in got.items():
            if tr is None:
                verdict = "wrong" if seg else "none-ok"
            else:
                verdict = judge_intro(seg, tr)
                if verdict == "wrong" and skips_story(seg, tr):
                    story[label] += 1
            tally[label][verdict] += 1
        if got["before"] != got["after"]:
            changed.append((os.path.basename(f)[:60], speed, got["before"], got["after"], tr))
print(
    name,
    "files",
    sum(len(r) for _, r in groups),
    "rates",
    dict(rates),
    "with an IntroDB intro",
    sum(1 for _, r in groups for f in r if any(c[0] == "intro" for c in introdb.get(f, {}).get("candidates", []))),
)
for label in ("plex", "before", "after"):
    c = tally[label]
    print(
        f"  {label:7s} useful {c['useful']:3d} wrong {c['wrong']:3d} missed {c['missed']:3d}"
        + (f" none-ok {c['none-ok']}" if c["none-ok"] else "")
        + f"  (wrong that skip story {story[label]})"
    )
print("  decisions changed by the frame rate:", len(changed))
for row in changed:
    print("   ", row)
