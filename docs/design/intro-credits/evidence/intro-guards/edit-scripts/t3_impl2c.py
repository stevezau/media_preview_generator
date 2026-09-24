import sys
p = sys.argv[1]
s = open(p).read()
def rep(old, new):
    global s
    assert s.count(old) == 1, old[:90]
    s = s.replace(old, new)
rep('''    if problem is None and _is_online_logo(candidate):''', '''    if problem is None and _is_online_logo(candidate, ctx.duration_ms):''')
rep('''def _is_online_logo(candidate: Candidate) -> bool:
    """An IntroDB or TheIntroDB intro starting in the first 2 s and shorter than 10 s (``ONLINE_LOGO_BEFORE_MS``)."""
    if candidate.type is not MarkerType.INTRO or candidate.source not in _TIMED_ON_ANY_RELEASE:
        return False
    end = candidate.end_ms if candidate.end_ms is not None else candidate.start_ms + MIN_ONLINE_INTRO_AT_START_MS
    return candidate.start_ms < ONLINE_LOGO_BEFORE_MS and end - candidate.start_ms < MIN_ONLINE_INTRO_AT_START_MS''', '''def _is_online_logo(candidate: Candidate, duration_ms: int) -> bool:
    """An IntroDB or TheIntroDB intro starting in the first 2 s and shorter than 10 s (``ONLINE_LOGO_BEFORE_MS``)."""
    if candidate.type is not MarkerType.INTRO or candidate.source not in _TIMED_ON_ANY_RELEASE:
        return False
    length = resolve_end_ms(candidate, duration_ms) - candidate.start_ms
    return candidate.start_ms < ONLINE_LOGO_BEFORE_MS and length < MIN_ONLINE_INTRO_AT_START_MS''')
open(p, "w").write(s)
