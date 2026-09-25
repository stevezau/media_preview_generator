"""The file where 'servers only shorten a skip' fails with the new generator, decided by the work tree and by base."""

from paths import EVIDENCE_DIR, HERE, LOCAL, REPO  # noqa: F401
import importlib.util
import pickle
import random
import sys

W = str(REPO)
R = str(LOCAL)
sys.path.insert(0, W)
sys.path.insert(0, f"{W}/tests/markers")
import test_decide as t  # noqa: E402

T, S = t.T, t.S
for seed in (20260913, 7, 1234):
    rng = random.Random(seed)
    for _ in range(1000):
        cands, x, locked = t._random_file(rng)
        got = t.decide(cands, x, locked)
        without = t.decide([c for c in cands if c.source not in t._REF_SERVER], x, locked)
        for mtype in T:
            a, b = got[mtype], without[mtype]
            if not (t._unlocked_decided(a) and t._unlocked_decided(b)):
                continue
            if t.shortened_by(a.reason) is None and t._checked_edge(a.marker) != t._checked_edge(b.marker):
                continue
            if a.marker.start_ms < b.marker.start_ms or a.marker.end_ms > b.marker.end_ms:
                print("seed", seed, mtype, x)
                print(" with   ", a)
                print(" without", b)
                for c in cands:
                    if c.type is mtype:
                        print("   ", c.source.value, c.start_ms, c.end_ms, c.confidence, c.copied_from)
                pickle.dump((cands, x, locked), open(f"{R}/prop_case.pkl", "wb"))
                sys.exit()
