import random
import sys
sys.path.insert(0, ".")
from tests.markers import test_decide as TD
from media_preview_generator.markers import decide as D
from media_preview_generator.markers.models import Candidate, Source

def mismatches(seed):
    rng = random.Random(seed)
    n = 0
    for _ in range(1000):
        cands, x, locked = TD._random_file(rng)
        if D.decide(cands, x, locked) != TD._ref_decide(cands, x, locked):
            n += 1
    return n

print("as is:", [mismatches(s) for s in (20260913, 7, 1234)])
orig = D._is_online_logo
D._is_online_logo = lambda c, d: False
print("logo rule off:", [mismatches(s) for s in (20260913, 7, 1234)])
D._is_online_logo = orig
orig_sane = D._marker_is_sane
D._marker_is_sane = lambda m, ctx: D.sanity_problem(Candidate(m.type, m.start_ms, m.end_ms, Source(m.decided_by[0])), ctx) is None
print("logo rule on composed markers:", [mismatches(s) for s in (20260913, 7, 1234)])
