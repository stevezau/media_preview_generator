import sys
p = sys.argv[1]
s = open(p).read()
old = '''    def test_the_limits_are_season_audios_own(self):'''
new = '''    def test_a_marker_composed_with_a_source_that_reads_the_file_is_not_judged_as_a_logo(self):
        # IntroDB's 10 s (kept), ranked first, gives the end; the chapter's later start makes the marker 8.5 s long.
        order = ("introdb", *(o for o in ORDER if o != "introdb"))
        cands = [intro(S.INTRODB, 0, 10_000), intro(S.CHAPTERS, 1_500, 10_000)]
        d = decide(cands, ctx(publish_when="medium", order=order), {})[T.INTRO]
        assert (d.status, d.reason) == (DecisionStatus.DECIDED, "sources agree: introdb, chapters")
        assert d.marker == Marker(T.INTRO, 1_500, 10_000, ("introdb", "chapters"))

    def test_the_limits_are_season_audios_own(self):'''
assert s.count(old) == 1
s = s.replace(old, new)
open(p, "w").write(s)
