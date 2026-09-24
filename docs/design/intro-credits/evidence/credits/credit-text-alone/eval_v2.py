import json, sys, os, collections
sys.path.insert(0, "/home/data/workspace/plex_generate_vid_previews"); sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tools.markers_eval.credits_text import text_candidates, _decided
from tools.markers_eval.credits import judge_credits
from tools.markers_eval.plex import PlexMarker, server_candidates, first_marker
from media_preview_generator.markers.models import MarkerType
import patch_rule
data = {}
for p in sys.argv[1:]: data.update(json.load(open(p)))
groups = collections.defaultdict(list)
for k, e in data.items(): groups[e["set"]].append(e)
groups["80"] = groups.get("movies40", []) + groups.get("tv40", [])
f = lambda c: f"{c['useful']:3} useful {c['late']:3} late {c['wrong']:3} wrong {c['missed']:3} missed"
for g in [x for x in ("80", "movies40", "tv40", "movie_credit_truth") if groups.get(x)]:
    es = groups[g]
    rows = {k: collections.Counter() for k in ("plex", "text", "medium", "proposed", "proposed+plex_alone")}
    for e in es:
        dur = e["duration_ms"]; is_movie = e["set"] != "tv40"; tr = e["truth"]
        markers = [PlexMarker(*m) for m in e["plex"]]
        plex = first_marker(markers, MarkerType.CREDITS)
        cands = text_candidates(e["text"], e["text_end"]) + server_candidates(markers, MarkerType.CREDITS)
        med, _, _ = _decided(cands, dur, is_movie, "medium")
        patch_rule.enable(); new, _, _ = _decided(cands, dur, is_movie, "medium"); patch_rule.disable()
        ps = plex and plex.start_ms / 1000
        rows["plex"][judge_credits(ps, tr)] += 1
        rows["text"][judge_credits(e["text"], tr)] += 1
        rows["medium"][judge_credits(med and med[0], tr)] += 1
        rows["proposed"][judge_credits(new and new[0], tr)] += 1
        alone = new[0] if new else (ps if e["text"] is None else None)
        rows["proposed+plex_alone"][judge_credits(alone, tr)] += 1
    print(f"== {g}: {len(es)} files")
    for k, c in rows.items(): print(f"  {k:20}", f(c))
