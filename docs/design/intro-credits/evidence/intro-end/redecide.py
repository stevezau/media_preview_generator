"""Re-decide the dumped evidence (dump_evidence.py) with the integration tree's decide(), frame rate = file's own
("after"), and list every wrong file with its candidates, decided_by and reason.

Usage: redecide.py <set> [--variant NAME] [--quiet]
"""

import collections
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-a0d2f9fd9105568a3")
from media_preview_generator.markers import decide as D  # noqa: E402
from media_preview_generator.markers.models import Candidate, MarkerType, Source  # noqa: E402
from tools.markers_eval.score import judge_intro, skips_story  # noqa: E402

HERE = Path(__file__).parent
ORDER = ("chapters", "introdb", "skipdb", "season_audio", "season_audio_previous", "credits_text", "server_markers",
         "server_markers_imported")  # fmt: skip


def candidates(row):
    cands = [Candidate(MarkerType.INTRO, s, e, Source.INTRODB) for s, e in row["introdb"]]
    cands += [Candidate(MarkerType.INTRO, s, e, Source.SERVER_MARKERS, origin="plex") for s, e in row["plex_server"]]
    if row["audio"]:
        a = row["audio"]
        cands.append(Candidate(MarkerType.INTRO, round(a[0] * 1000), round(a[1] * 1000), Source.SEASON_AUDIO, 1.0,
                               f"{a[2]}"))  # fmt: skip
    return cands


def run(name, decide_fn=None, quiet=False):
    decide_fn = decide_fn or D.decide
    ev = json.load(open(HERE / f"evidence_{name}.json"))
    tally = collections.Counter()
    story = 0
    out = {}
    for f, row in ev.items():
        tr = tuple(row["truth"]) if row["truth"] else None
        ctx = D.DecisionContext(row["duration_ms"], False, "medium", frozenset({MarkerType.INTRO}), ORDER,
                                frame_rate=row["speed"])  # fmt: skip
        d = decide_fn(candidates(row), ctx, {})[MarkerType.INTRO]
        seg = (d.marker.start_ms / 1000, d.marker.end_ms / 1000) if d.status is D.DecisionStatus.DECIDED else None
        if tr is None:
            verdict = "wrong" if seg else "none-ok"
        else:
            verdict = judge_intro(seg, tr)
        if verdict == "wrong" and tr and skips_story(seg, tr):
            story += 1
        tally[verdict] += 1
        out[f] = (verdict, seg, d)
        if verdict == "wrong" and not quiet:
            print("WRONG", os.path.basename(f))
            print("   truth", tr, "ours", seg, "decided_by", d.marker.decided_by, "reason", d.reason)
            print("   audio", row["audio"], "introdb", row["introdb"], "plex", row["plex_server"],
                  "speed", row["speed"], "dur", row["duration_ms"])  # fmt: skip
    print(f"{name}: useful {tally['useful']} wrong {tally['wrong']} missed {tally['missed']}"
          + (f" none-ok {tally['none-ok']}" if tally["none-ok"] else "") + f" (story-skipping wrong {story})")  # fmt: skip
    return out


if __name__ == "__main__":
    run(sys.argv[1], quiet="--quiet" in sys.argv)
