"""Candidate rules (a)-(e) as monkeypatches on a tree's decide module, for measurement before any is implemented.

apply(D, "a1,c5,d") patches the module in place. Names:
  a1        a lone SkipDB intro/recap never decides (SkipDB joins the agreement-only sources)
  c<N>      an online (IntroDB/TheIntroDB/importer copy, and SkipDB) credits/preview end up to N s past the file's end is
            clamped instead of dropping the answer (c0 = unlimited)
  cI<N>     the same, IntroDB/TheIntroDB/importer copy only
  d         SkipDB credits/preview never block a source that may decide alone when it is the only disagreement
  dw        the same for every source that can't decide the type alone (IntroDB/TheIntroDB, previous season audio)
  e<L>      an IntroDB/TheIntroDB intro that disagrees with season audio but has its length (within 5 s) and a start
            more than 15 s away is dropped when season audio is present (min length L s)
"""

from paths import EVIDENCE_DIR, HERE, LOCAL, REPO  # noqa: F401
from dataclasses import replace


def apply(D, names: str) -> None:
    for name in [n for n in names.split(",") if n]:
        if name == "a1":
            D._AGREEMENT_ONLY = D._AGREEMENT_ONLY | {D.Source.SKIPDB}
        elif name == "xcs":
            _patch_text_start(D)
        elif name in ("br1", "br2"):
            if name == "br2":
                _patch_text_start(D)
            _patch_b_resolve(D)
        elif name == "bt":
            _patch_b_trigger(D)
        elif name == "bt2":
            _patch_b_trigger2(D)
        elif name == "br3":
            _patch_b3(D)
        elif name == "bs3":
            _patch_b3(D, symmetric=True)
        elif name.startswith("cI") or name.startswith("c"):
            only_crowd = name.startswith("cI")
            limit = int(name[2:] if only_crowd else name[1:]) * 1000
            _patch_c(D, limit, only_crowd)
        elif name == "d":
            _patch_d(D, wide=False)
        elif name == "dw":
            _patch_d(D, wide=True)
        elif name.startswith("e"):
            _patch_e(D, int(name[1:] or 0) * 1000)
        else:
            raise ValueError(name)


def _patch_c(D, limit_ms: int, only_crowd: bool) -> None:
    real = D._times_problem

    def times_problem(candidate, ctx):
        problem = real(candidate, ctx)
        if problem != "ends past the end of the file" or candidate.type in D._START_SEGMENTS:
            return problem
        online = D.timed_on_any_release(candidate) or (not only_crowd and candidate.source is D.Source.SKIPDB)
        if not online:
            return problem
        if limit_ms and candidate.end_ms > ctx.duration_ms + limit_ms:
            return problem
        return real(replace(candidate, end_ms=ctx.duration_ms), ctx)

    D._times_problem = times_problem


def _patch_d(D, wide: bool) -> None:
    def weak(c):
        if wide:
            return not D._may_decide_alone(c) and c.source not in D.SERVER_SOURCES
        return c.source is D.Source.SKIPDB and c.type not in D._START_SEGMENTS

    def single(mtype, sane, ctx):
        ranked = sorted(sane, key=D._sort_key(ctx))
        groups = sorted({D._group(c) for c in sane})
        proposal = next((c for c in ranked if D._may_decide_alone(c)), None)
        own = [c for c in sane if proposal is not None and D._group(c) == D._group(proposal)]
        others = [c for c in sane if proposal is None or D._group(c) != D._group(proposal)]
        counted = [o for o in others if not weak(o)]
        servers_only_agree = all(c.source in D.SERVER_SOURCES for c in counted) and all(
            D._agree(o, c, ctx.duration_ms) for o in counted for c in own
        )
        if ctx.publish_when == "medium" and proposal is not None and servers_only_agree:
            if not all(D._agree(a, b, ctx.duration_ms) for a, b in D.combinations(own, 2)):
                return D._review(mtype, D._own_marker(proposal, ctx), "source disagrees with itself")
            other_edge, edge_suppliers = D._safer_other_edge(mtype, own, ctx)
            sources = {proposal.source, *(c.source for c in edge_suppliers)}
            marker = D._composed_marker(mtype, D._agree_value(proposal, ctx.duration_ms), other_edge, sources, ctx)
            if not D._marker_is_sane(marker, ctx):
                return D._review(mtype, D._own_marker(proposal, ctx), "sources disagree on the other edge")
            return D.TypeDecision(
                mtype, D.DecisionStatus.DECIDED, marker, None, f"single source ({proposal.source.value})"
            )
        return real_single(mtype, sane, ctx)

    real_single = D._decide_from_single_source
    D._decide_from_single_source = single


def _patch_e(D, min_len_ms: int) -> None:
    real = D._on_file_clock

    def on_clock(of_type, ctx):
        sane = real(of_type, ctx)
        audio = [c for c in sane if c.source is D.Source.SEASON_AUDIO]
        if not audio:
            return sane
        d = ctx.duration_ms

        def shifted_copy(c):
            if c.type not in D._START_SEGMENTS or not D.timed_on_any_release(c):
                return False
            length = D.resolve_end_ms(c, d) - c.start_ms
            for a in audio:
                a_len = D.resolve_end_ms(a, d) - a.start_ms
                if D._agree(c, a, d) or min(length, a_len) < min_len_ms:
                    continue
                if abs(length - a_len) <= D.INTRO_END_TOLERANCE_MS and abs(c.start_ms - a.start_ms) > 15_000:
                    return True
            return False

        return [c for c in sane if not shifted_copy(c)]

    D._on_file_clock = real_on_clock_wrapper = on_clock  # noqa: F841


def _patch_text_start(D):
    """xcs: with credit text among a credits/preview cluster's confirming candidates, it supplies the start."""

    def compose(cluster, mtype, ctx):
        d = ctx.duration_ms
        confirmed = [c for c in cluster if any(D._group(o) != D._group(c) and D._agree(c, o, d) for o in cluster)]
        confirmed_non_server = [c for c in confirmed if c.source not in D.SERVER_SOURCES]
        rank = D._sort_key(ctx)
        file_edge = mtype in D._START_SEGMENTS and any(c.source in D._READS_THE_FILE for c in confirmed_non_server)
        text_start = mtype not in D._START_SEGMENTS and any(
            c.source is D.Source.CREDITS_TEXT for c in confirmed_non_server
        )

        def key(c):
            return (
                file_edge and D.timed_on_any_release(c),
                text_start and c.source is not D.Source.CREDITS_TEXT,
                rank(c),
            )

        winner = min(confirmed_non_server, key=key)
        other_edge, edge_suppliers = D._safer_other_edge(mtype, confirmed, ctx)
        agreeing_with_winner = [c for c in confirmed if D._agree(winner, c, d)]
        sources = {c.source for c in [winner, *edge_suppliers, *agreeing_with_winner]}
        return D._composed_marker(mtype, D._agree_value(winner, d), other_edge, sources, ctx), winner

    D._compose_cluster = compose


def _patch_b_resolve(D):
    """br: a credits/preview chapter contradicted only by agreeing clusters that hold credit text and another
    non-server independent source loses to them (decided from those clusters) instead of Needs review."""
    real = D._decide_from_chapters

    def from_chapters(mtype, chapters, others, ctx):
        decision = real(mtype, chapters, others, ctx)
        if mtype in D._START_SEGMENTS or "contradicted by agreeing sources" not in decision.reason:
            return decision
        d = ctx.duration_ms
        chosen = min(chapters, key=lambda c: D._chapter_choice_key(c, d))
        value = D._checked_value(mtype, D._own_marker(chosen, ctx))
        contradicting = []
        for cluster in D._agreeing_cliques(others, mtype, ctx):
            marker, _ = D._compose_cluster(cluster, mtype, ctx)
            if abs(D._checked_value(mtype, marker) - value) > D._tolerance_ms(mtype):
                contradicting.append(cluster)
        if not contradicting:
            return decision
        for cluster in contradicting:
            texts = [c for c in cluster if c.source is D.Source.CREDITS_TEXT]
            partners = [
                o
                for o in cluster
                if o.source not in D.SERVER_SOURCES
                and o.source is not D.Source.CREDITS_TEXT
                and any(D._agree(o, t, d) for t in texts)
            ]
            if not texts or not partners:
                return decision
        return D._decide_from_cliques(mtype, contradicting, ctx)

    D._decide_from_chapters = from_chapters


_STALE: list = []


def _patch_b_trigger(D):
    """bt: without a credit text answer, a credits chapter contradicted by an agreeing pair that needs a server marker
    made for an earlier file (stale) goes to Needs review, so credit text is read to check it."""
    real_decide = D.decide
    real_chapters = D._decide_from_chapters

    def decide(candidates, ctx, locked):
        _STALE[:] = [c for c in candidates if c.stale]
        try:
            return real_decide(candidates, ctx, locked)
        finally:
            _STALE[:] = []

    def from_chapters(mtype, chapters, others, ctx):
        decision = real_chapters(mtype, chapters, others, ctx)
        if (
            mtype in D._START_SEGMENTS
            or decision.status is not D.DecisionStatus.DECIDED
            or any(c.source is D.Source.CREDITS_TEXT for c in others)
        ):
            return decision
        stale = [D.replace(c, stale=False) for c in _STALE if c.type is mtype]
        if not stale:
            return decision
        chosen = min(chapters, key=lambda c: D._chapter_choice_key(c, ctx.duration_ms))
        value = D._checked_value(mtype, D._own_marker(chosen, ctx))
        for cluster in D._agreeing_cliques(others + stale, mtype, ctx):
            marker, _ = D._compose_cluster(cluster, mtype, ctx)
            if abs(D._checked_value(mtype, marker) - value) > D._tolerance_ms(mtype):
                names = ", ".join(marker.decided_by)
                return D._review(
                    mtype,
                    decision.marker,
                    f"chapters contradicted by agreeing sources: {names} (a server marker made for an "
                    "earlier file); credit text is read to check",
                )
        return decision

    D.decide = decide
    D._decide_from_chapters = from_chapters


def _compose_text_start(D, cluster, mtype, ctx):
    """_compose_cluster with credit text supplying the credits start when it confirms."""
    d = ctx.duration_ms
    confirmed = [c for c in cluster if any(D._group(o) != D._group(c) and D._agree(c, o, d) for o in cluster)]
    confirmed_non_server = [c for c in confirmed if c.source not in D.SERVER_SOURCES]
    rank = D._sort_key(ctx)
    winner = min(confirmed_non_server, key=lambda c: (c.source is not D.Source.CREDITS_TEXT, rank(c)))
    other_edge, edge_suppliers = D._safer_other_edge(mtype, confirmed, ctx)
    agreeing_with_winner = [c for c in confirmed if D._agree(winner, c, d)]
    sources = {c.source for c in [winner, *edge_suppliers, *agreeing_with_winner]}
    return D._composed_marker(mtype, D._agree_value(winner, d), other_edge, sources, ctx), winner


def _patch_b3(D, symmetric=False):
    """br3: a credits/preview chapter contradicted by agreeing clusters that each hold credit text agreeing with a
    non-server source of another group loses to them; their start is credit text's. bs (symmetric): a chapter credit
    text agrees with stands against contradicting clusters without credit text."""
    real = D._decide_from_chapters

    def from_chapters(mtype, chapters, others, ctx):
        decision = real(mtype, chapters, others, ctx)
        if mtype in D._START_SEGMENTS or "contradicted by agreeing sources" not in decision.reason:
            return decision
        d = ctx.duration_ms
        tol = D._tolerance_ms(mtype)
        chosen = min(chapters, key=lambda c: D._chapter_choice_key(c, d))
        value = D._checked_value(mtype, D._own_marker(chosen, ctx))
        contradicting = []
        for cluster in D._agreeing_cliques(others, mtype, ctx):
            marker, _ = D._compose_cluster(cluster, mtype, ctx)
            if abs(D._checked_value(mtype, marker) - value) > tol:
                contradicting.append(cluster)
        if not contradicting:
            return decision
        texts_ok = []
        for cluster in contradicting:
            texts = [c for c in cluster if c.source is D.Source.CREDITS_TEXT]
            partners = [o for o in cluster if o.source not in D.SERVER_SOURCES and o.source is not D.Source.CREDITS_TEXT
                        and any(D._agree(o, t, d) for t in texts)]
            texts_ok.append(bool(texts and partners))
        if all(texts_ok):
            composed = [_compose_text_start(D, cl, mtype, ctx) for cl in contradicting]
            values = [D._checked_value(mtype, m) for m, _ in composed]
            if max(values) - min(values) > tol:
                return decision
            merged = list({id(c): c for cl in contradicting for c in cl}.values())
            marker, winner = _compose_text_start(D, merged, mtype, ctx)
            if not D._marker_is_sane(marker, ctx):
                return decision
            if abs(marker.start_ms - value) <= tol:
                # credit text agrees with the chapter after all: the chapter stands, confirmed
                return D.TypeDecision(mtype, D.DecisionStatus.DECIDED, D._own_marker(chosen, ctx), None, "chapters")
            return D.TypeDecision(mtype, D.DecisionStatus.DECIDED, marker, None,
                                  "credit text and agreeing sources contradict the chapter: " + ", ".join(marker.decided_by))
        if symmetric and not any(texts_ok):
            texts = [c for c in others if c.source is D.Source.CREDITS_TEXT and abs(c.start_ms - value) <= tol]
            if texts:
                return D.TypeDecision(mtype, D.DecisionStatus.DECIDED, D._own_marker(chosen, ctx), None,
                                      "chapters (credit text agrees)")
        return decision

    D._decide_from_chapters = from_chapters


def _patch_b_trigger2(D):
    """bt2: without a credit text answer (and credit text among the sources), a credits chapter contradicted by a SkipDB
    answer -- alone, or with a server marker, fresh or made for an earlier file -- goes to Needs review so credit text
    is read to check it."""
    real_decide = D.decide
    real_chapters = D._decide_from_chapters

    def decide(candidates, ctx, locked):
        _STALE[:] = [c for c in candidates if c.stale]
        try:
            return real_decide(candidates, ctx, locked)
        finally:
            _STALE[:] = []

    def from_chapters(mtype, chapters, others, ctx):
        decision = real_chapters(mtype, chapters, others, ctx)
        if (
            mtype is not D.MarkerType.CREDITS
            or decision.status is not D.DecisionStatus.DECIDED
            or "credits_text" not in ctx.source_order
            or any(c.source is D.Source.CREDITS_TEXT for c in others)
        ):
            return decision
        chosen = min(chapters, key=lambda c: D._chapter_choice_key(c, ctx.duration_ms))
        value = D._checked_value(mtype, D._own_marker(chosen, ctx))
        far = [c for c in others if c.source is D.Source.SKIPDB and abs(c.start_ms - value) > D._tolerance_ms(mtype)]
        if far:
            return D._review(mtype, decision.marker, "chapters contradicted by skipdb; credit text is read to check")
        return decision

    D.decide = decide
    D._decide_from_chapters = from_chapters
