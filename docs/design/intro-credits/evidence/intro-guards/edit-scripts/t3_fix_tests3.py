import sys
p = sys.argv[1]
s = open(p).read()
old = '''    def test_a_marker_composed_with_a_source_that_reads_the_file_is_not_judged_as_a_logo(self):
        # IntroDB's 10 s (kept), ranked first, gives the end; the chapter's later start makes the marker 8.5 s long.
        order = ("introdb", *(o for o in ORDER if o != "introdb"))
        cands = [intro(S.INTRODB, 0, 10_000), intro(S.CHAPTERS, 1_500, 10_000)]
        d = decide(cands, ctx(publish_when="medium", order=order), {})[T.INTRO]
        assert (d.status, d.reason) == (DecisionStatus.DECIDED, "sources agree: introdb, chapters")
        assert d.marker == Marker(T.INTRO, 1_500, 10_000, ("introdb", "chapters"))'''
new = '''    def test_a_marker_composed_from_a_kept_online_intro_is_not_judged_as_a_logo(self):
        # IntroDB's 10 s (kept), ranked first, gives the end; Plex's later start makes the marker 8.5 s long.
        order = ("introdb", *(o for o in ORDER if o != "introdb"))
        cands = [intro(S.INTRODB, 0, 10_000), intro(S.SERVER_MARKERS, 1_500, 10_000, "plex")]
        d = decide(cands, ctx(publish_when="medium", order=order), {})[T.INTRO]
        assert (d.status, d.reason) == (DecisionStatus.DECIDED, "sources agree: introdb, server_markers")
        assert d.marker == Marker(T.INTRO, 1_500, 10_000, ("introdb", "server_markers"))'''
assert s.count(old) == 1
s = s.replace(old, new)
open(p, "w").write(s)
p2 = sys.argv[2]
s = open(p2).read()
old = '''    """Whether a composed marker's times pass :func:`sanity_problem`. The online-logo check judges what a database
    answered, not a marker a source that reads the file agreed with, so it isn't asked here."""'''
new = '''    """Whether a composed marker's times pass :func:`sanity_problem`. The online-logo check judges an online answer as
    it came, not a marker composed from one it kept and another source's edge, so it isn't asked here."""'''
assert s.count(old) == 1
s = s.replace(old, new)
open(p2, "w").write(s)
