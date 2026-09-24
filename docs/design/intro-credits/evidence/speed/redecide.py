"""Re-decide Bones S05-S08 from answers.json (season audio before/after already matched) with the worktree's decide(),
with and without Plex's own intro as server evidence, and score against truth.json (useful = end within 5 s, start
within 15 s; Needs review publishes nothing and counts as missed). Before = dev's season audio and no frame rate."""

import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-ab7b211236d423de3")
from media_preview_generator.markers.decide import DecisionContext, DecisionStatus, decide  # noqa: E402
from media_preview_generator.markers.models import Candidate, MarkerType, Source  # noqa: E402
from tools.markers_eval.score import judge_intro, skips_story  # noqa: E402

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
answers = json.load(open(HERE / "answers.json"))
truth = json.load(open(HERE / "truth.json"))
skip = set(sys.argv[1:])  # episodes whose truth failed a frame check


def ours(row, label, with_plex):
    cands = []
    if row["introdb"]:
        cands.append(Candidate(MarkerType.INTRO, *row["introdb"], Source.INTRODB))
    if with_plex and row["plex"]:
        cands.append(Candidate(MarkerType.INTRO, *row["plex"], Source.SERVER_MARKERS, origin="plex"))
    audio = row[f"audio_{label}"]
    if audio:
        cands.append(Candidate(MarkerType.INTRO, round(audio[0] * 1000), round(audio[1] * 1000), Source.SEASON_AUDIO))
    rate = row["frame_rate"] if label == "after" else None
    ctx = DecisionContext(row["duration_ms"], False, "medium", frozenset({MarkerType.INTRO}), ORDER, frame_rate=rate)
    d = decide(cands, ctx, {})[MarkerType.INTRO]
    seg = (d.marker.start_ms / 1000, d.marker.end_ms / 1000) if d.status is DecisionStatus.DECIDED else None
    return seg, d


tallies = collections.defaultdict(collections.Counter)
story = collections.Counter()
reviews = collections.Counter()
changed = []
for code, row in sorted(answers.items()):
    if code in skip:
        continue
    tr = (truth[code]["start"], truth[code]["end"])
    group = "25fps" if abs(row["frame_rate"] - 25) < 0.05 else "film"
    plex = (row["plex"][0] / 1000, row["plex"][1] / 1000) if row["plex"] else None
    results = {"plex": plex}
    for label in ("before", "after"):
        for with_plex in (False, True):
            seg, d = ours(row, label, with_plex)
            name = f"ours_{label}{'_with_plex_evidence' if with_plex else ''}"
            results[name] = seg
            if d.status is not DecisionStatus.DECIDED:
                reviews[name] += 1
    for name, seg in results.items():
        verdict = judge_intro(seg, tr)
        for key in ("all", group):
            tallies[(key, name)][verdict] += 1
        if verdict == "wrong" and skips_story(seg, tr):
            story[name] += 1
    b, a = judge_intro(results["ours_before"], tr), judge_intro(results["ours_after"], tr)
    if b != a:
        changed.append(f"{code} {group} {b}->{a}")

for (key, name), c in sorted(tallies.items()):
    extra = f" story-skipping wrong {story[name]} needs-review {reviews[name]}" if key == "all" else ""
    print(f"{key:6s} {name:34s} useful {c['useful']:3d} wrong {c['wrong']:3d} missed {c['missed']:3d}{extra}")
print(len(changed), "changed:", ", ".join(changed))
