"""Bones S05-S08 without season audio: IntroDB raw + Plex's own intro only, before (no rate) and after (the file's)."""

import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-ab7b211236d423de3")
from media_preview_generator.markers.decide import DecisionContext, DecisionStatus, decide  # noqa: E402
from media_preview_generator.markers.models import Candidate, MarkerType, Source  # noqa: E402
from tools.markers_eval.score import judge_intro  # noqa: E402

HERE = Path(__file__).parent
ORDER = (
    "chapters",
    "introdb",
    "skipdb",
    "season_audio",
    "season_audio_previous",
    "credits_text",
    "server_markers",
    "server_markers_imported",
)
answers, truth = json.load(open(HERE / "answers.json")), json.load(open(HERE / "truth.json"))
tally = collections.defaultdict(collections.Counter)
for code, row in answers.items():
    tr = (truth[code]["start"], truth[code]["end"])
    cands = [
        Candidate(MarkerType.INTRO, *row["introdb"], Source.INTRODB),
        Candidate(MarkerType.INTRO, *row["plex"], Source.SERVER_MARKERS, origin="plex"),
    ]
    for label, rate in (("before", None), ("after", row["frame_rate"])):
        ctx = DecisionContext(
            row["duration_ms"], False, "medium", frozenset({MarkerType.INTRO}), ORDER, frame_rate=rate
        )
        d = decide(cands, ctx, {})[MarkerType.INTRO]
        seg = (d.marker.start_ms / 1000, d.marker.end_ms / 1000) if d.status is DecisionStatus.DECIDED else None
        tally[label][judge_intro(seg, tr)] += 1
print({k: dict(v) for k, v in tally.items()})
