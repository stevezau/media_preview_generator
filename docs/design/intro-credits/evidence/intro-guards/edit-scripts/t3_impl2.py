import sys
p = sys.argv[1]
s = open(p).read()
def rep(old, new):
    global s
    assert s.count(old) == 1, old[:90]
    s = s.replace(old, new)

rep('''_TIMED_ON_ANY_RELEASE = frozenset({Source.INTRODB, Source.THEINTRODB})
_TIMED_ON_ANY_RELEASE_COPY = "introdb"
''', '''_TIMED_ON_ANY_RELEASE = frozenset({Source.INTRODB, Source.THEINTRODB})
_TIMED_ON_ANY_RELEASE_COPY = "introdb"
# An IntroDB or TheIntroDB intro starting in the first 2 s and shorter than 10 s is a logo at the start of the file,
# not the show's intro (The Fixers: IntroDB gives Netflix's "N", 0-7 s, for all 10 episodes, where season audio finds
# the theme at 263.0-284.8 s on E01). Season audio passes over the same stretches of its own (``audio.season``'s
# ``FILE_START_S`` and ``MIN_FILE_START_LENGTH_S``, which a test keeps equal to these); none of 43 verified online
# intros is one.
ONLINE_LOGO_BEFORE_MS = 2_000
MIN_ONLINE_INTRO_AT_START_MS = 10_000
''')
rep('''        if length > MAX_INTRO_MS:
            return f"{candidate.type.value} too long"
    else:''', '''        if length > MAX_INTRO_MS:
            return f"{candidate.type.value} too long"
        if (
            candidate.type is MarkerType.INTRO
            and candidate.source in _TIMED_ON_ANY_RELEASE
            and start < ONLINE_LOGO_BEFORE_MS
            and length < MIN_ONLINE_INTRO_AT_START_MS
        ):
            return "online intro shorter than 10 s from the file's first 2 s: a logo at the start of the file"
    else:''')
rep('''    """Whether a composed marker passes :func:`sanity_problem` (a candidate's source never affects sanity)."""''',
    '''    """Whether a composed marker passes :func:`sanity_problem`. Only an online intro's source matters there, and a
    composed marker never is one that fails for it: such a candidate is left out before anything is composed."""''')
open(p, "w").write(s)
