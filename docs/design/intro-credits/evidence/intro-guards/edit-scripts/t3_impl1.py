import sys
p = sys.argv[1]
s = open(p).read()
def rep(old, new):
    global s
    assert s.count(old) == 1, old[:80]
    s = s.replace(old, new)

rep('''# against idents and music beds, and matching a season of 25 fps and film-rate releases at one speed): stored with its
# answers and with cached pairs, so a change to any of them is matched again.
SEASON_AUDIO_VERSION = 6''', '''# against idents and music beds with where a stretch needs no dense core, and matching a season of 25 fps and film-rate
# releases at one speed): stored with its answers and with cached pairs, so a change to any of them is matched again.
SEASON_AUDIO_VERSION = 7''')
rep('''# where the two recordings match with gaps of at most 4 points (an ident followed by a music bed, merged by the
# matcher's 3.5 s gap bridge, has none).
FILE_START_S = 2.0
MIN_FILE_START_LENGTH_S = 10.0
MIN_DENSE_CORE_S = 8.0
DENSE_CORE_GAP_PTS = 4
''', '''# where the two recordings match with gaps of at most 4 points (an ident followed by a music bed, merged by the
# matcher's 3.5 s gap bridge, has none).
FILE_START_S = 2.0
MIN_FILE_START_LENGTH_S = 10.0
MIN_DENSE_CORE_S = 8.0
DENSE_CORE_GAP_PTS = 4
# A theme sung or played under dialogue has no dense core either, so two kinds of stretch are let off it (owner
# 2026-09-24): one starting at 2-30 s that is at least 30 s long (an ident or a music bed that long wasn't seen; its end
# picture is still checked), and one starting after 30 s, past the cold open, that is at least 10 s long and found by
# at least 2 other episodes. The limits were set after seeing what they leave out: an 8.8 s music bed after 30 s
# (Accused S04E06) and a recap only one other episode shares (The Fall, season 3).
CORE_FREE_EARLY_MIN_S = 30.0
CORE_FREE_LATER_MIN_S = 10.0
CORE_FREE_LATER_MIN_SUPPORT = 2
''')
rep('''def _passes_guards(
    target: str,
    candidate: IntroCandidate,
    points: Mapping[str, np.ndarray],
    end_picture_passes: Callable[[IntroCandidate], bool],
) -> bool:
    """Whether a candidate passes the guards against idents and music beds (``FILE_START_S`` above); the end picture,
    the only one that decodes, is asked last."""
    segment = candidate.segment
    if segment.start_s < FILE_START_S and segment.end_s - segment.start_s < MIN_FILE_START_LENGTH_S:
        return False
    if dense_core_s(target, candidate, points) < MIN_DENSE_CORE_S:
        return False
''', '''def needs_dense_core(segment: IntroSegment) -> bool:
    """Whether a stretch must have a dense core to be taken (``CORE_FREE_EARLY_MIN_S`` above).

    Args:
        segment: A cluster's segment, with its support.

    Returns:
        False for a stretch starting at 2-30 s that is at least 30 s long, and for one starting after 30 s that is at
        least 10 s long and found by at least 2 other episodes; True otherwise.
    """
    length_s = segment.end_s - segment.start_s
    if end_picture.is_early(segment.start_s):
        return segment.start_s < FILE_START_S or length_s < CORE_FREE_EARLY_MIN_S
    return length_s < CORE_FREE_LATER_MIN_S or segment.support < CORE_FREE_LATER_MIN_SUPPORT


def _passes_guards(
    target: str,
    candidate: IntroCandidate,
    points: Mapping[str, np.ndarray],
    end_picture_passes: Callable[[IntroCandidate], bool],
) -> bool:
    """Whether a candidate passes the guards against idents and music beds (``FILE_START_S`` above); the end picture,
    the only one that decodes, is asked last."""
    segment = candidate.segment
    if segment.start_s < FILE_START_S and segment.end_s - segment.start_s < MIN_FILE_START_LENGTH_S:
        return False
    if needs_dense_core(segment) and dense_core_s(target, candidate, points) < MIN_DENSE_CORE_S:
        return False
''')
rep('''a quorum cluster that fails a guard (too short at the file's start,
    no dense core, or an early one whose end picture differs) is passed over.''', '''a quorum cluster that fails a guard (too short at the file's start,
    no dense core where one is needed, or an early one whose end picture differs) is passed over.''')
open(p, "w").write(s)
