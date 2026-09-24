import sys
p = sys.argv[1]
s = open(p).read()
def rep(old, new, count=1):
    global s
    assert s.count(old) == count, (s.count(old), old[:90])
    s = s.replace(old, new)
rep('''def _ref_is_sane(c, x):
    d, start = x.duration_ms, c.start_ms''', '''def _ref_is_sane(c, x):
    if c.type is T.INTRO and c.source in (S.INTRODB, S.THEINTRODB) and c.start_ms < 2_000:
        if _ref_end(c, x.duration_ms) - c.start_ms < 10_000:
            return False
    return _ref_times_sane(c, x)


def _ref_times_sane(c, x):
    d, start = x.duration_ms, c.start_ms''')
rep('''    if c.type is T.INTRO and c.source in (S.INTRODB, S.THEINTRODB) and start < 2_000 and end - start < 10_000:
        return False
    if c.type in _REF_START_TYPES:''', '''    if c.type in _REF_START_TYPES:''')
rep('''                if not _ref_is_sane(Candidate(mtype, shorter.start_ms, shorter.end_ms, S.CHAPTERS), x):''',
    '''                if not _ref_times_sane(Candidate(mtype, shorter.start_ms, shorter.end_ms, S.CHAPTERS), x):''')
rep('''        if not _ref_is_sane(Candidate(mtype, result.start_ms, result.end_ms, winner.source), x):''',
    '''        if not _ref_times_sane(Candidate(mtype, result.start_ms, result.end_ms, winner.source), x):''')
rep('''        if not _ref_is_sane(Candidate(mtype, result.start_ms, result.end_ms, proposal.source), x):''',
    '''        if not _ref_times_sane(Candidate(mtype, result.start_ms, result.end_ms, proposal.source), x):''')
rep('''    if not _ref_is_sane(Candidate(mtype, start, result.end_ms, S.CHAPTERS), x):''',
    '''    if not _ref_times_sane(Candidate(mtype, start, result.end_ms, S.CHAPTERS), x):''')
open(p, "w").write(s)
