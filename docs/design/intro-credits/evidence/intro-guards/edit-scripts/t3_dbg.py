import sys
sys.path.insert(0, "tests/markers/audio")
sys.path.insert(0, ".")
from tests.markers.audio import test_season_guards as T
for at, pieces, piece_s in [(T.EARLY_AT, 4, 6.45), (T.EARLY_AT, 4, 6.3), (T.TITLE_AT, 2, 4.1), (T.TITLE_AT, 2, 4.4)]:
    files, points = T._season(T.Choppy(20, at, pieces, piece_s))
    m, g = T._answers(files, points, T.Pictures())
    print(at, pieces, piece_s, [(round(s.start_s,2), round(s.end_s,2), s.support) if s else None for s in m])
for shared_by in (3, 2):
    at = (40.0, 50.0, 60.0)[:shared_by] + (None,) * (3 - shared_by)
    files, points = T._season(T.Choppy(20, at, 3), episodes=3)
    m, g = T._answers(files, points, T.Pictures())
    print(shared_by, [(round(s.start_s,2), round(s.end_s,2), s.support) if s else None for s in m])
