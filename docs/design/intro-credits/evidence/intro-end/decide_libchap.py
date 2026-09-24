"""The library chapter set (evidence_libchap.json) decided before and after the intro-end rule, at Medium with the
app's default order and each file's probed frame rate. "before" is the composition as it was (a verbatim copy below),
"after" is whatever the integration tree's decide.py composes now (``--variant`` monkeypatches the proposed rule for a
run before it is implemented).

Truth is the file's own intro chapter, so a chapter answer is useful by construction: what the rule can move is a
chapter "contradicted by agreeing sources" (Needs review: missed), and a file without an intro chapter.

Usage: decide_libchap.py [--variant]
"""

import collections
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-a0d2f9fd9105568a3")
from media_preview_generator.markers import decide as D  # noqa: E402
from media_preview_generator.markers.models import SERVER_SOURCES, Candidate, MarkerType, Source  # noqa: E402
from tools.markers_eval.score import judge_intro, skips_story  # noqa: E402

HERE = Path(__file__).parent
ORDER = ("chapters", "introdb", "skipdb", "season_audio", "season_audio_previous", "credits_text", "server_markers",
         "server_markers_imported")  # fmt: skip


def compose_before(cluster, mtype, ctx):
    """``_compose_cluster`` as it was before the intro-end rule."""
    confirmed = [
        c for c in cluster if any(D._group(o) != D._group(c) and D._agree(c, o, ctx.duration_ms) for o in cluster)
    ]
    confirmed_non_server = [c for c in confirmed if c.source not in SERVER_SOURCES]
    winner = min(confirmed_non_server, key=D._sort_key(ctx))
    other_edge, edge_suppliers = D._safer_other_edge(mtype, confirmed, ctx)
    agreeing_with_winner = [c for c in confirmed if D._agree(winner, c, ctx.duration_ms)]
    sources = {c.source for c in [winner, *edge_suppliers, *agreeing_with_winner]}
    return D._composed_marker(mtype, D._agree_value(winner, ctx.duration_ms), other_edge, sources, ctx), winner


def compose_variant(cluster, mtype, ctx):
    """The proposed rule: with a source that reads the file among the confirming ones, an answer timed on any release
    doesn't supply an intro's or recap's agreed end."""
    d = ctx.duration_ms
    confirmed = [c for c in cluster if any(D._group(o) != D._group(c) and D._agree(c, o, d) for o in cluster)]
    confirmed_non_server = [c for c in confirmed if c.source not in SERVER_SOURCES]
    has_file = mtype in D._START_SEGMENTS and any(c.source in D._READS_THE_FILE for c in confirmed_non_server)
    rank = D._sort_key(ctx)
    winner = min(confirmed_non_server, key=lambda c: (has_file and D.timed_on_any_release(c), rank(c)))
    other_edge, edge_suppliers = D._safer_other_edge(mtype, confirmed, ctx)
    agreeing_with_winner = [c for c in confirmed if D._agree(winner, c, d)]
    sources = {c.source for c in [winner, *edge_suppliers, *agreeing_with_winner]}
    return D._composed_marker(mtype, D._agree_value(winner, d), other_edge, sources, ctx), winner


def candidates(row):
    out = [Candidate(MarkerType(t), s, e, Source.CHAPTERS) for t, s, e in row["chapters"]]
    out += [Candidate(MarkerType.INTRO, s, e, Source.INTRODB) for s, e in row["introdb"]]
    if row["audio"]:
        a = row["audio"]
        out.append(Candidate(MarkerType.INTRO, round(a[0] * 1000), round(a[1] * 1000), Source.SEASON_AUDIO, 1.0,
                             f"{a[2]}"))  # fmt: skip
    return out


def run(compose, label, ev):
    D._compose_cluster = compose
    tally, paths, story = collections.Counter(), collections.Counter(), 0
    out = {}
    for f, row in sorted(ev.items()):
        tr = tuple(row["truth"]) if row["truth"] else None
        ctx = D.DecisionContext(row["duration_ms"], False, "medium", frozenset({MarkerType.INTRO}), ORDER,
                                intro_chapter_limit_ms=row["intro_chapter_limit_ms"],
                                frame_rate=row["frame_rate"])  # fmt: skip
        d = D.decide(candidates(row), ctx, {})[MarkerType.INTRO]
        seg = (d.marker.start_ms / 1000, d.marker.end_ms / 1000) if d.status is D.DecisionStatus.DECIDED else None
        if tr is None:
            verdict = "decided-no-chapter" if seg else "none-no-chapter"
        else:
            verdict = judge_intro(seg, tr)
            if verdict == "wrong" and skips_story(seg, tr):
                story += 1
        tally[verdict] += 1
        if "contradicted" in d.reason:
            paths["chapters contradicted"] += 1
        if d.status is D.DecisionStatus.DECIDED and "chapters" in d.marker.decided_by:
            paths["decided with chapters"] += 1
            if "introdb" in d.marker.decided_by:
                paths["chapters + introdb agree"] += 1
        out[f] = (verdict, seg, d.reason)
    print(f"{label:7s} {dict(sorted(tally.items()))} story-skipping wrong {story}  paths {dict(sorted(paths.items()))}")
    return out


ev = json.load(open(HERE / "evidence_libchap.json"))
print("files", len(ev), "with an intro chapter", sum(1 for r in ev.values() if r["truth"]),
      "with IntroDB", sum(1 for r in ev.values() if r["introdb"]),
      "with season audio", sum(1 for r in ev.values() if r["audio"]),
      "chapter + IntroDB", sum(1 for r in ev.values() if r["truth"] and r["introdb"]),
      "IntroDB + season audio", sum(1 for r in ev.values() if r["introdb"] and r["audio"]))  # fmt: skip
current = D._compose_cluster
before = run(compose_before, "before", ev)
after = run(compose_variant if "--variant" in sys.argv else current, "after", ev)
for f in sorted(before):
    if before[f] != after[f]:
        print("CHANGED", os.path.basename(f)[:70], before[f], "->", after[f])
