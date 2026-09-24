import sys
p = sys.argv[1]
s = open(p).read()
def rep(old, new):
    global s
    assert s.count(old) == 1, old[:90]
    s = s.replace(old, new)

rep('''def sanity_problem(candidate: Candidate, ctx: DecisionContext) -> str | None:
    """Check whether a candidate is a plausible segment for this file.

    Args:
        candidate: The candidate to check.
        ctx: The file's duration, movie flag, and other per-file context.

    Returns:
        A short reason why the candidate is implausible, or None when it passes every check.
    """
    d = ctx.duration_ms''', '''def sanity_problem(candidate: Candidate, ctx: DecisionContext) -> str | None:
    """Check whether a candidate is a plausible segment for this file.

    Args:
        candidate: The candidate to check.
        ctx: The file's duration, movie flag, and other per-file context.

    Returns:
        A short reason why the candidate is implausible, or None when it passes every check.
    """
    problem = _times_problem(candidate, ctx)
    if problem is None and _is_online_logo(candidate):
        return "online intro shorter than 10 s from the file's first 2 s: a logo at the start of the file"
    return problem


def _is_online_logo(candidate: Candidate) -> bool:
    """An IntroDB or TheIntroDB intro starting in the first 2 s and shorter than 10 s (``ONLINE_LOGO_BEFORE_MS``)."""
    if candidate.type is not MarkerType.INTRO or candidate.source not in _TIMED_ON_ANY_RELEASE:
        return False
    end = candidate.end_ms if candidate.end_ms is not None else candidate.start_ms + MIN_ONLINE_INTRO_AT_START_MS
    return candidate.start_ms < ONLINE_LOGO_BEFORE_MS and end - candidate.start_ms < MIN_ONLINE_INTRO_AT_START_MS


def _times_problem(candidate: Candidate, ctx: DecisionContext) -> str | None:
    """:func:`sanity_problem`'s checks of the times alone, whichever source gave them."""
    d = ctx.duration_ms''')
rep('''        if length > MAX_INTRO_MS:
            return f"{candidate.type.value} too long"
        if (
            candidate.type is MarkerType.INTRO
            and candidate.source in _TIMED_ON_ANY_RELEASE
            and start < ONLINE_LOGO_BEFORE_MS
            and length < MIN_ONLINE_INTRO_AT_START_MS
        ):
            return "online intro shorter than 10 s from the file's first 2 s: a logo at the start of the file"
    else:''', '''        if length > MAX_INTRO_MS:
            return f"{candidate.type.value} too long"
    else:''')
rep('''    """Whether a composed marker passes :func:`sanity_problem`. Only an online intro's source matters there, and a
    composed marker never is one that fails for it: such a candidate is left out before anything is composed."""
    probe = Candidate(marker.type, marker.start_ms, marker.end_ms, Source(marker.decided_by[0]))
    return sanity_problem(probe, ctx) is None''', '''    """Whether a composed marker's times pass :func:`sanity_problem`. The online-logo check judges what a database
    answered, not a marker a source that reads the file agreed with, so it isn't asked here."""
    probe = Candidate(marker.type, marker.start_ms, marker.end_ms, Source(marker.decided_by[0]))
    return _times_problem(probe, ctx) is None''')
open(p, "w").write(s)
