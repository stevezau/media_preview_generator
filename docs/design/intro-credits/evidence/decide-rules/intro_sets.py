"""Intro sets at decide level with one tree's decide(): lab118, held-out 175, Accused (season audio + IntroDB + Plex's own
markers, as #312 measured them), the #310 library chapter set (chapters + season audio + IntroDB), Bones S05-S08
(season audio + IntroDB + Plex's marker flagged by the "made for an earlier file" rule). ``--skipdb`` adds SkipDB's
intro from its ODbL dump (nearest duration; exact <= 2 s, shifted <= 15 s, as the read API labels it).

Usage: intro_sets.py <base|work> [--skipdb] [--json out.json]
"""

from paths import EVIDENCE_DIR, HERE, LOCAL, REPO  # noqa: F401
import collections
import csv
import json
import os
import re
import sys
from datetime import UTC, datetime

S = str(LOCAL)
R = S
TREES = {
    "base": f"{R}/base",
    "work": str(REPO),
}
tree = TREES[sys.argv[1]]
sys.path.insert(0, tree)
from media_preview_generator.markers import decide as D  # noqa: E402
from media_preview_generator.markers.models import Candidate, MarkerType, Source  # noqa: E402
from media_preview_generator.markers.publishers.plex_db import _Part, _TaggingRow, _types_not_made_for_file  # noqa: E402
from media_preview_generator.markers.sources import skipdb  # noqa: E402
from tools.markers_eval.online import skipdb_segments  # noqa: E402
from tools.markers_eval.score import judge_intro, skips_story  # noqa: E402

assert D.__file__.startswith(tree)
if "--variant" in sys.argv:
    sys.path.insert(0, str(LOCAL))
    import variants  # noqa: E402

    variants.apply(D, sys.argv[sys.argv.index("--variant") + 1])
WITH_SKIPDB = "--skipdb" in sys.argv
ORDER = ("chapters", "introdb", "skipdb", "season_audio", "season_audio_previous", "credits_text", "server_markers",
         "server_markers_imported")  # fmt: skip
EVIDENCE = "/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence"
DUMP = json.load(open(f"{EVIDENCE}/online/skipdb-dump.json"))["segments"]
IMDB = json.load(open(f"{R}/imdb_by_tvdb.json"))
_BY_KEY = collections.defaultdict(list)
for seg in DUMP:
    _BY_KEY[(seg["imdb_id"], seg.get("season"), seg.get("episode"))].append(seg)


def skipdb_intro(imdb, path, duration_ms):
    if not WITH_SKIPDB or not imdb:
        return []
    m = re.search(r"S(\d+)E(\d+)", os.path.basename(path))
    if m is None:
        return []
    case = {"imdb": imdb, "season": int(m.group(1)), "episode": int(m.group(2)), "dur": duration_ms / 1000}
    rows = _BY_KEY.get((imdb, case["season"], case["episode"]), [])
    return [c for c in skipdb._candidates(skipdb_segments(case, rows)) if c.type is MarkerType.INTRO]


def imdb_of(path):
    m = re.search(r"\{tvdb-(\d+)\}", path)
    return IMDB.get(m.group(1)) if m else None


def audio(row):
    a = row["audio"]
    if not a:
        return []
    return [Candidate(MarkerType.INTRO, round(a[0] * 1000), round(a[1] * 1000), Source.SEASON_AUDIO, 1.0, f"{a[2]}")]


def regress_rows(name):
    ev = json.load(open(f"{EVIDENCE_DIR}/intro-end/evidence_{name}.json"))
    for f, row in ev.items():
        cands = [Candidate(MarkerType.INTRO, s, e, Source.INTRODB) for s, e in row["introdb"]]
        cands += [
            Candidate(MarkerType.INTRO, s, e, Source.SERVER_MARKERS, origin="plex") for s, e in row["plex_server"]
        ]
        cands += audio(row)
        cands += skipdb_intro(imdb_of(f), f, row["duration_ms"])
        ctx = D.DecisionContext(row["duration_ms"], False, "medium", frozenset({MarkerType.INTRO}), ORDER,
                                frame_rate=row["speed"])  # fmt: skip
        plex = tuple(x / 1000 for x in row["plex_first"]) if row.get("plex_first") else None
        yield f, cands, ctx, (tuple(row["truth"]) if row["truth"] else None), plex


def libchap_rows():
    ev = json.load(open(f"{EVIDENCE_DIR}/intro-end/evidence_libchap.json"))
    for f, row in sorted(ev.items()):
        cands = [Candidate(MarkerType(t), s, e, Source.CHAPTERS) for t, s, e in row["chapters"]]
        cands += [Candidate(MarkerType.INTRO, s, e, Source.INTRODB) for s, e in row["introdb"]]
        cands += audio(row)
        cands += skipdb_intro(imdb_of(f), f, row["duration_ms"])
        ctx = D.DecisionContext(row["duration_ms"], False, "medium", frozenset({MarkerType.INTRO}), ORDER,
                                intro_chapter_limit_ms=row["intro_chapter_limit_ms"],
                                frame_rate=row["frame_rate"])  # fmt: skip
        yield f, cands, ctx, (tuple(row["truth"]) if row["truth"] else None), None


def _epoch(text):
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC).timestamp()) if text else None


def bones_rows():
    stale_by_code = {}
    with open(f"{S}/bones_plex.tsv", newline="") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            code = f"S{int(row['sn']):02d}E{int(row['en']):02d}"
            rows = []
            for i, piece in enumerate(p.strip() for p in (row["markers"] or "").split("|") if p.strip()):
                text, rest = piece.split(":", 1)
                span, created = rest.split("@", 1)
                start, end = span.split("-")
                rows.append(_TaggingRow(i, i, text, int(start), int(end), None, None, _epoch(created)))
            part = _Part(int(row["mpid"]), int(row["miid"]), row["file"], None, None, _epoch(row["mp_updated"]))
            stale_by_code[code] = _types_not_made_for_file(rows, [part])
    answers = json.load(open(f"{S}/bones/answers.json"))
    truth = json.load(open(f"{S}/bones/truth.json"))
    for code, row in sorted(answers.items()):
        t = truth.get(code, {})
        if "end" not in t:
            continue
        cands = []
        if row["audio_after"]:
            s, e, support = row["audio_after"]
            cands.append(Candidate(MarkerType.INTRO, round(s * 1000), round(e * 1000), Source.SEASON_AUDIO, 1.0,
                                   f"{support}/x"))  # fmt: skip
        if row["introdb"]:
            cands.append(Candidate(MarkerType.INTRO, *row["introdb"], Source.INTRODB))
        if row["plex"]:
            stale = MarkerType.INTRO in stale_by_code.get(code, frozenset())
            cands.append(Candidate(MarkerType.INTRO, *row["plex"], Source.SERVER_MARKERS, origin="plex", stale=stale))
        cands += skipdb_intro("tt0460627", row["file"], row["duration_ms"])
        ctx = D.DecisionContext(row["duration_ms"], False, "medium", frozenset({MarkerType.INTRO}), ORDER,
                                frame_rate=row["frame_rate"])  # fmt: skip
        plex = tuple(x / 1000 for x in row["plex"]) if row["plex"] else None
        yield code, cands, ctx, (t["start"], t["end"]), plex


SETS = {
    "lab118": lambda: regress_rows("lab118"),
    "heldout175": lambda: regress_rows("heldout175"),
    "accused": lambda: regress_rows("accused"),
    "libchap": libchap_rows,
    "bones": bones_rows,
}
out = {}
for name, rows in SETS.items():
    tally = {"plex": collections.Counter(), "ours": collections.Counter()}
    story = collections.Counter()
    for f, cands, ctx, tr, plex in rows():
        d = D.decide(cands, ctx, {})[MarkerType.INTRO]
        seg = (d.marker.start_ms / 1000, d.marker.end_ms / 1000) if d.status is D.DecisionStatus.DECIDED else None
        for label, s in (("ours", seg), ("plex", plex)):
            if tr is None:
                if name == "libchap":
                    verdict = "decided-no-chapter" if s else "none-no-chapter"
                else:
                    verdict = "wrong" if s else "none-ok"
            else:
                verdict = judge_intro(s, tr)
                if verdict == "wrong" and skips_story(s, tr):
                    story[label] += 1
            tally[label][verdict] += 1
            if label == "ours":
                out[f"{name}|{f}"] = {"verdict": verdict, "seg": seg, "reason": d.reason, "truth": tr,
                                      "by": list(d.marker.decided_by) if seg else None,
                                      "cands": [(c.source.value, c.start_ms, c.end_ms, c.stale) for c in cands]}  # fmt: skip
    for label in ("plex", "ours"):
        if name == "libchap" and label == "plex":
            continue
        c = tally[label]
        extra = {k: v for k, v in c.items() if k not in ("useful", "wrong", "missed")}
        print(f"{name:11s} {label:5s} useful {c['useful']:3d} wrong {c['wrong']:3d} missed {c['missed']:3d} {extra} "
              f"story-skipping {story[label]}")  # fmt: skip
if "--json" in sys.argv:
    json.dump(out, open(sys.argv[sys.argv.index("--json") + 1], "w"), indent=0)
