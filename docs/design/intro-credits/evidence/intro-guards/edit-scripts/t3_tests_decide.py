import sys
p = sys.argv[1]
s = open(p).read()
def rep(old, new, count=1):
    global s
    assert s.count(old) == count, (s.count(old), old[:90])
    s = s.replace(old, new)

rep('''            (Candidate(T.RECAP, 0, SHORT_DUR, S.THEINTRODB), False, SHORT_DUR, "runs to end of file"),  # explicit end
        ],
    )
    def test_matrix(self, cand, is_movie, duration, problem):''', '''            (Candidate(T.RECAP, 0, SHORT_DUR, S.THEINTRODB), False, SHORT_DUR, "runs to end of file"),  # explicit end
            # -- an online intro in the first 2 s shorter than 10 s is a logo (The Fixers: Netflix's "N", 0-7 s) --
            (intro(S.INTRODB, 0, 7_000), False, DUR, "logo at the start of the file"),
            (intro(S.THEINTRODB, 0, 7_000), False, DUR, "logo at the start of the file"),
            (intro(S.INTRODB, 1_999, 11_998), False, DUR, "logo at the start of the file"),  # 9_999 long
            (intro(S.INTRODB, 1_999, 11_999), False, DUR, None),  # exactly 10 s long
            (intro(S.INTRODB, 2_000, 9_000), False, DUR, None),  # starts at 2 s
            (intro(S.SKIPDB, 0, 7_000), False, DUR, None),  # SkipDB matches this file's duration
            (intro(S.CHAPTERS, 0, 7_000), False, DUR, None),  # the file's own chapters
            (intro(S.SERVER_MARKERS, 0, 7_000), False, DUR, None),
            # an importer's copy of IntroDB never decides alone nor confirms IntroDB (one group): left as it comes
            (Candidate(T.INTRO, 0, 7_000, S.SERVER_MARKERS_IMPORTED, copied_from="introdb"), False, DUR, None),
            (Candidate(T.RECAP, 0, 7_000, S.THEINTRODB), False, DUR, None),  # a short "previously on" is a recap
        ],
    )
    def test_matrix(self, cand, is_movie, duration, problem):''')

rep('''class TestRecapAndPreview:
    """Recap/Preview drive the exact same rules as Intro/Credits, end-to-end through decide()."""''', '''class TestOnlineLogoAtTheFileStart:
    """The Fixers (Netflix): IntroDB gives 0-7 s, the "N" logo, for all 10 episodes, and season audio finds the theme
    (E01 263.0-284.8 s, E07 205.0-227.2 s). Prod left every episode in Needs review ("sources disagree"), proposing
    the logo. An IntroDB or TheIntroDB intro starting in the first 2 s and shorter than 10 s is now dropped as a logo,
    the rule season audio applies to itself (``audio.season.FILE_START_S``), so season audio decides alone."""

    @pytest.mark.parametrize(
        ("duration", "audio"),
        [(2_694_464, (263_036, 284_831)), (2_668_736, (204_955, 227_246))],
        ids=["E01", "E07"],
    )
    @pytest.mark.parametrize("online", [S.INTRODB, S.THEINTRODB])
    def test_season_audio_decides_over_an_online_logo(self, duration, audio, online):
        cands = [intro(online, 0, 7_000), Candidate(T.INTRO, *audio, S.SEASON_AUDIO, 1.0, "9/9")]
        d = decide(cands, ctx(publish_when="medium", duration=duration), {})[T.INTRO]
        assert (d.status, d.reason) == (DecisionStatus.DECIDED, "single source (season_audio)")
        assert d.marker == Marker(T.INTRO, *audio, ("season_audio",))

    def test_an_online_logo_alone_is_no_evidence_not_a_proposal(self):
        d = decide([intro(S.INTRODB, 0, 7_000)], ctx(publish_when="medium", duration=2_694_464), {})[T.INTRO]
        assert (d.status, d.marker, d.proposed) == (DecisionStatus.NO_EVIDENCE, None, None)

    def test_a_10_s_online_intro_at_the_start_still_counts(self):
        cands = [intro(S.INTRODB, 0, 10_000), Candidate(T.INTRO, 263_036, 284_831, S.SEASON_AUDIO, 1.0, "9/9")]
        d = decide(cands, ctx(publish_when="medium", duration=2_694_464), {})[T.INTRO]
        assert d.status is DecisionStatus.NEEDS_REVIEW
        assert d.reason.startswith("sources disagree") and (d.proposed.start_ms, d.proposed.end_ms) == (0, 10_000)

    def test_the_limits_are_season_audios_own(self):
        assert decide_module.ONLINE_LOGO_BEFORE_MS == round(season.FILE_START_S * 1000)
        assert decide_module.MIN_ONLINE_INTRO_AT_START_MS == round(season.MIN_FILE_START_LENGTH_S * 1000)


class TestRecapAndPreview:
    """Recap/Preview drive the exact same rules as Intro/Credits, end-to-end through decide()."""''')

rep('''    if c.type in _REF_START_TYPES:
        return end < d - 2_000 and start * 100 <= 35 * d and end - start <= 300_000''', '''    if c.type is T.INTRO and c.source in (S.INTRODB, S.THEINTRODB) and start < 2_000 and end - start < 10_000:
        return False
    if c.type in _REF_START_TYPES:
        return end < d - 2_000 and start * 100 <= 35 * d and end - start <= 300_000''')
rep('''from media_preview_generator.markers.models import Candidate, Marker, MarkerType, Source''', '''from media_preview_generator.markers.audio import season
from media_preview_generator.markers.models import Candidate, Marker, MarkerType, Source''')
open(p, "w").write(s)
