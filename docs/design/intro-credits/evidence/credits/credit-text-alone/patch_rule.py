"""The proposed rule, as a patch of decide._decide_from_single_source (decide.py:576-615)."""
from itertools import combinations
from media_preview_generator.markers import decide as D
from media_preview_generator.markers.models import MarkerType, Source, SERVER_SOURCES

_orig = D._decide_from_single_source

def patched(mtype, sane, ctx):
    ranked = sorted(sane, key=D._sort_key(ctx))
    proposal = next((c for c in ranked if D._may_decide_alone(c)), None)
    if (ctx.publish_when == "medium" and mtype is MarkerType.CREDITS and proposal is not None
            and proposal.source is Source.CREDITS_TEXT):
        own = [c for c in sane if D._group(c) == D._group(proposal)]
        others = [c for c in sane if D._group(c) != D._group(proposal)]
        # Only sources that never decide credits alone disagree: the file's own frames win (proposed rule).
        if others and all(not D._may_decide_alone(c) for c in others):
            if not all(D._agree(a, b, ctx.duration_ms) for a, b in combinations(own, 2)):
                return D._review(mtype, D._own_marker(proposal, ctx), "source disagrees with itself")
            other_edge, suppliers = D._safer_other_edge(mtype, own, ctx)
            sources = {proposal.source, *(c.source for c in suppliers)}
            marker = D._composed_marker(mtype, D._agree_value(proposal, ctx.duration_ms), other_edge, sources, ctx)
            if not D._marker_is_sane(marker, ctx):
                return D._review(mtype, D._own_marker(proposal, ctx), "sources disagree on the other edge")
            return D.TypeDecision(mtype, D.DecisionStatus.DECIDED, marker, None,
                                  f"single source ({proposal.source.value}); disagreeing sources can't decide alone")
    return _orig(mtype, sane, ctx)

def enable():
    D._decide_from_single_source = patched

def disable():
    D._decide_from_single_source = _orig
