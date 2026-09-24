"""Candidate rule variants, monkeypatched onto the integration tree's decide module, measured on the dumped sets
(heldout175, lab118, accused) and Bones S05-S08 (lane A's answers.json/truth.json, Plex marker with F's stale flag).

Usage: variants.py VARIANT
"""
import collections, json, os, sys
from pathlib import Path
sys.path.insert(0, "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-a0d2f9fd9105568a3")
from media_preview_generator.markers import decide as D
from media_preview_generator.markers.models import SERVER_SOURCES, Candidate, MarkerType, Source
from tools.markers_eval.score import judge_intro, skips_story
import redecide

ORIG_COMPOSE = D._compose_cluster


def compose_file_edge(cluster, mtype, ctx):
    """R1: an answer timed on any release (IntroDB/TheIntroDB/their copy) never supplies the checked edge when a
    confirming candidate from a source that reads this file is in the cluster."""
    d = ctx.duration_ms
    confirmed = [c for c in cluster if any(D._group(o) != D._group(c) and D._agree(c, o, d) for o in cluster)]
    confirmed_non_server = [c for c in confirmed if c.source not in SERVER_SOURCES]
    has_file = any(c.source in D._READS_THE_FILE for c in confirmed_non_server)
    rank = D._sort_key(ctx)
    winner = min(confirmed_non_server, key=lambda c: (has_file and D.timed_on_any_release(c), rank(c)))
    other_edge, edge_suppliers = D._safer_other_edge(mtype, confirmed, ctx)
    agreeing_with_winner = [c for c in confirmed if D._agree(winner, c, d)]
    sources = {c.source for c in [winner, *edge_suppliers, *agreeing_with_winner]}
    return D._composed_marker(mtype, D._agree_value(winner, d), other_edge, sources, ctx), winner


VARIANTS = {"base": None, "file_edge": compose_file_edge}


def bones(variant_label):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "integ_proof"))
    import importlib, io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        if "bones_decide_integ" in sys.modules:
            importlib.reload(sys.modules["bones_decide_integ"])
        else:
            import bones_decide_integ  # noqa: F401
    for line in buf.getvalue().splitlines():
        if line.startswith("audio+idb+plex(F-rule)") or line.startswith("audio+idb ") or line.startswith("idb+plex"):
            print("  bones", line[:110])


name = sys.argv[1]
if VARIANTS[name]:
    D._compose_cluster = VARIANTS[name]
print("variant", name)
res = {}
for s in ("heldout175", "lab118", "accused"):
    res[s] = redecide.run(s, quiet=True)
bones(name)
json.dump({s: {f: [v, seg] for f, (v, seg, d) in r.items()} for s, r in res.items()}, open(f"verdicts_{name}.json", "w"))
