import sys
p = sys.argv[1]
s = open(p).read()
old = '''    cands = [named.get(id(c), c) for c in cands]
    return cands, x, locked
'''
new = '''    cands = [named.get(id(c), c) for c in cands]
    # A tenth of the files have their intros moved into the first seconds of the file, where an online one shorter than
    # 10 s is a logo and is left out; drawn last for the same reason.
    if rng.random() < 0.1:
        moved = {}
        for c in cands:
            if c.type is T.INTRO and id(c) not in moved:
                start = rng.choice((0, 1_999, 2_000))
                moved[id(c)] = replace(c, start_ms=start, end_ms=start + rng.choice((7_000, 9_999, 10_000, 12_000)))
        cands = [moved.get(id(c), c) for c in cands]
    return cands, x, locked
'''
assert s.count(old) == 1
s = s.replace(old, new)
open(p, "w").write(s)
