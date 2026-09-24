import sys
sys.path.insert(0, ".")
from media_preview_generator.markers import decide as D
from media_preview_generator.markers.models import Candidate, MarkerType as T, Source as S
ORDER = ("introdb", "chapters", "theintrodb", "skipdb", "season_audio", "credits_text", "server_markers", "server_markers_imported")
x = D.DecisionContext(1_320_000, False, "medium", frozenset({T.INTRO}), ORDER)
c = [Candidate(T.INTRO, 0, 10_000, S.INTRODB), Candidate(T.INTRO, 1_500, 10_000, S.SERVER_MARKERS, origin="plex")]
print("now:", D.decide(c, x, {})[T.INTRO])
D._times_problem_orig = D._times_problem
D._marker_is_sane = lambda m, ctx: D.sanity_problem(Candidate(m.type, m.start_ms, m.end_ms, S(m.decided_by[0])), ctx) is None
print("with the logo check on composed markers:", D.decide(c, x, {})[T.INTRO])
