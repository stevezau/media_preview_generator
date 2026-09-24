"""Score Bones S05-S08: Plex's own intro, IntroDB raw/scaled, season audio alone and our decisions before/after, against
the visual truth (useful = end within 5 s, start within 15 s). Needs review publishes nothing: it counts as missed."""

import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-ab7b211236d423de3")
from tools.markers_eval.score import judge_intro, skips_story  # noqa: E402

HERE = Path(__file__).parent
FILM_ON_PAL = 24000 / 1001 / 25
answers = json.load(open(HERE / "answers.json"))
truth = json.load(open(HERE / "truth.json"))


def seconds(pair):
    return None if pair is None else (pair[0] / 1000, pair[1] / 1000)


rows = collections.defaultdict(lambda: collections.Counter())
story = collections.Counter()
per_group = collections.defaultdict(lambda: collections.defaultdict(collections.Counter))
statuses = collections.Counter()
reasons = collections.Counter()
lines = []
for code, row in sorted(answers.items()):
    t = truth.get(code, {})
    if "end" not in t:
        lines.append(f"{code} no truth {t}")
        continue
    tr = (t["start"], t["end"])
    pal = row["frame_rate"] and abs(row["frame_rate"] - 25) < 0.05
    got = {
        "plex": seconds(row["plex"]),
        "introdb_raw": seconds(row["introdb"]),
        "introdb_on_file_clock": None
        if row["introdb"] is None
        else (
            (row["introdb"][0] / 1000 * FILM_ON_PAL, row["introdb"][1] / 1000 * FILM_ON_PAL)
            if pal
            else seconds(row["introdb"])
        ),
        "audio_before": None if row["audio_before"] is None else tuple(row["audio_before"][:2]),
        "audio_after": None if row["audio_after"] is None else tuple(row["audio_after"][:2]),
        "ours_before": seconds(row["decision_before"]["marker"]),
        "ours_after": seconds(row["decision_after"]["marker"]),
    }
    group = "25fps" if pal else "film"
    for name, seg in got.items():
        verdict = judge_intro(seg, tr)
        rows[name][verdict] += 1
        per_group[group][name][verdict] += 1
        if verdict == "wrong" and skips_story(seg, tr):
            story[name] += 1
    for label in ("before", "after"):
        d = row[f"decision_{label}"]
        statuses[(label, d["status"])] += 1
        if d["status"] != "decided":
            reasons[(label, d["reason"])] += 1
    fmt = lambda s: "-" if s is None else f"{s[0]:6.1f}-{s[1]:6.1f}"  # noqa: E731
    lines.append(
        f"{code} {group:5s} truth {fmt(tr)} plex {fmt(got['plex'])} idb {fmt(got['introdb_raw'])} "
        f"before {fmt(got['ours_before'])} after {fmt(got['ours_after'])} "
        f"[{judge_intro(got['ours_before'], tr)}->{judge_intro(got['ours_after'], tr)}] "
        f"{row['decision_after']['reason']}"
    )
print("\n".join(lines))
print()
for name, c in rows.items():
    print(
        f"{name:22s} useful {c['useful']:3d}  wrong {c['wrong']:3d}  missed {c['missed']:3d}  "
        f"(wrong that skip story {story[name]})"
    )
for group, table in per_group.items():
    print(group, {name: dict(c) for name, c in table.items() if name in ("plex", "ours_before", "ours_after")})
print(dict(statuses))
for (label, reason), n in sorted(reasons.items()):
    print(label, n, reason)
