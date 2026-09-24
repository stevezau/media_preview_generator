"""Bones S05-S08 at decide level with THIS WORKTREE's decide() (round-2 review applied), from the stored answers
(answers.json: season audio after the speed fix, IntroDB's raw intro, Plex's first intro, real frame rates), against
the visual truth (useful = end within 5 s, start within 15 s; Needs review publishes nothing: missed).

Evidence sets:
  audio+idb+plex   Plex's marker counted as made for this file (this lane alone)
  audio+idb        Plex's marker dropped (what the "made for an earlier file" lane does for these files)
  audio            season audio alone
  idb+plex         no season audio
"""

import collections
import json
import sys
from pathlib import Path

WORKTREE = "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-ab7b211236d423de3"
sys.path.insert(0, WORKTREE)
from media_preview_generator.markers.decide import DecisionContext, DecisionStatus, decide  # noqa: E402
from media_preview_generator.markers.models import Candidate, MarkerType, Source  # noqa: E402
from tools.markers_eval.score import judge_intro, skips_story  # noqa: E402

HERE = Path(__file__).parent
ORDER = ("chapters", "introdb", "skipdb", "season_audio", "season_audio_previous", "credits_text", "server_markers",
         "server_markers_imported")  # fmt: skip
answers = json.load(open(HERE / "answers.json"))
truth = json.load(open(HERE / "truth.json"))
SETS = {
    "audio+idb+plex": ("audio", "idb", "plex"),
    "audio+idb": ("audio", "idb"),
    "audio": ("audio",),
    "idb+plex": ("idb", "plex"),
}
tally = {name: collections.Counter() for name in SETS}
by_rate = {name: collections.defaultdict(collections.Counter) for name in SETS}
story = collections.Counter()
for code, row in sorted(answers.items()):
    t = truth.get(code, {})
    if "end" not in t:
        continue
    tr = (t["start"], t["end"])
    pieces = {}
    if row["audio_after"]:
        s, e, support = row["audio_after"]
        pieces["audio"] = Candidate(
            MarkerType.INTRO, round(s * 1000), round(e * 1000), Source.SEASON_AUDIO, 1.0, f"{support}/x"
        )
    if row["introdb"]:
        pieces["idb"] = Candidate(MarkerType.INTRO, *row["introdb"], Source.INTRODB)
    if row["plex"]:
        pieces["plex"] = Candidate(MarkerType.INTRO, *row["plex"], Source.SERVER_MARKERS, origin="plex")
    rate = "25fps" if row["frame_rate"] and abs(row["frame_rate"] - 25) < 0.05 else "film"
    ctx = DecisionContext(row["duration_ms"], False, "medium", frozenset({MarkerType.INTRO}), ORDER,
                          frame_rate=row["frame_rate"])  # fmt: skip
    for name, parts in SETS.items():
        d = decide([pieces[p] for p in parts if p in pieces], ctx, {})[MarkerType.INTRO]
        seg = (d.marker.start_ms / 1000, d.marker.end_ms / 1000) if d.status is DecisionStatus.DECIDED else None
        verdict = judge_intro(seg, tr)
        tally[name][verdict] += 1
        by_rate[name][rate][verdict] += 1
        if verdict == "wrong" and skips_story(seg, tr):
            story[name] += 1
for name, c in tally.items():
    print(f"{name:15s} useful {c['useful']:3d} wrong {c['wrong']:3d} missed {c['missed']:3d} "
          f"(wrong that skip story {story[name]})  {({k: dict(v) for k, v in by_rate[name].items()})}")  # fmt: skip
