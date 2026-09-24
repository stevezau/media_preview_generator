"""Bones S05-S08 at decide level with the INTEGRATION tree's decide() (lanes C+F+B+A+D), from lane A's stored answers
(bones/answers.json) against the visual truth (bones/truth.json).

Plex's intro is flagged stale by lane F's own rule (plex_db._types_not_made_for_file) run on each file's Plex rows from
bones_plex.tsv (marker rows' created_at, the part's updated_at; no pv: record, so extra_data is None).

Evidence sets:
  audio+idb+plex(F-rule)  Plex's marker with the stale flag F's rule gives it  <- the integrated behaviour
  audio+idb+plex(fresh)   Plex's marker forced to count as this file's (lane A alone)
  audio+idb               Plex's marker dropped
  audio                   season audio alone
  idb+plex(F-rule)        no season audio
"""

import collections
import csv
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

WORKTREE = "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-a0d2f9fd9105568a3"
sys.path.insert(0, WORKTREE)
from media_preview_generator.markers.decide import DecisionContext, DecisionStatus, decide  # noqa: E402
from media_preview_generator.markers.models import Candidate, MarkerType, Source  # noqa: E402
from media_preview_generator.markers.publishers.plex_db import (  # noqa: E402
    _Part,
    _TaggingRow,
    _types_not_made_for_file,
)
from tools.markers_eval.score import judge_intro, skips_story  # noqa: E402

SCRATCH = Path(__file__).resolve().parent.parent
BONES = SCRATCH / "bones"
ORDER = ("chapters", "introdb", "skipdb", "season_audio", "season_audio_previous", "credits_text", "server_markers",
         "server_markers_imported")  # fmt: skip


def epoch(text: str) -> int | None:
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC).timestamp()) if text else None


def stale_types_by_code() -> dict[str, frozenset[MarkerType]]:
    out = {}
    with open(SCRATCH / "bones_plex.tsv", newline="") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            code = f"S{int(row['sn']):02d}E{int(row['en']):02d}"
            rows = []
            for i, piece in enumerate(p.strip() for p in (row["markers"] or "").split("|") if p.strip()):
                text, rest = piece.split(":", 1)
                span, created = rest.split("@", 1)
                start, end = span.split("-")
                rows.append(_TaggingRow(i, i, text, int(start), int(end), None, None, epoch(created)))
            part = _Part(int(row["mpid"]), int(row["miid"]), row["file"], None, None, epoch(row["mp_updated"]))
            out[code] = _types_not_made_for_file(rows, [part])
    return out


answers = json.load(open(BONES / "answers.json"))
truth = json.load(open(BONES / "truth.json"))
stale_by_code = stale_types_by_code()
SETS = {
    "audio+idb+plex(F-rule)": ("audio", "idb", "plex_f"),
    "audio+idb+plex(fresh)": ("audio", "idb", "plex_fresh"),
    "audio+idb": ("audio", "idb"),
    "audio": ("audio",),
    "idb+plex(F-rule)": ("idb", "plex_f"),
}
tally = {name: collections.Counter() for name in SETS}
by_rate = {name: collections.defaultdict(collections.Counter) for name in SETS}
story = collections.Counter()
flagged = collections.Counter()
for code, row in sorted(answers.items()):
    t = truth.get(code, {})
    if "end" not in t:
        continue
    tr = (t["start"], t["end"])
    rate = "25fps" if row["frame_rate"] and abs(row["frame_rate"] - 25) < 0.05 else "film"
    is_stale = MarkerType.INTRO in stale_by_code.get(code, frozenset())
    flagged[(rate, is_stale)] += 1
    pieces = {}
    if row["audio_after"]:
        s, e, support = row["audio_after"]
        pieces["audio"] = Candidate(
            MarkerType.INTRO, round(s * 1000), round(e * 1000), Source.SEASON_AUDIO, 1.0, f"{support}/x"
        )
    if row["introdb"]:
        pieces["idb"] = Candidate(MarkerType.INTRO, *row["introdb"], Source.INTRODB)
    if row["plex"]:
        plex = Candidate(MarkerType.INTRO, *row["plex"], Source.SERVER_MARKERS, origin="plex")
        pieces["plex_fresh"] = plex
        pieces["plex_f"] = Candidate(MarkerType.INTRO, *row["plex"], Source.SERVER_MARKERS, origin="plex",
                                     stale=is_stale)  # fmt: skip
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
print("Plex intro flagged stale by F's rule (rate, stale): count ->", dict(flagged))
for name, c in tally.items():
    print(f"{name:24s} useful {c['useful']:3d} wrong {c['wrong']:3d} missed {c['missed']:3d} "
          f"(wrong that skip story {story[name]})  {({k: dict(v) for k, v in by_rate[name].items()})}")  # fmt: skip
