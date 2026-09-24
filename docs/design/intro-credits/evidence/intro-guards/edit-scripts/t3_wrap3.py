import sys

root = sys.argv[1]


def edit(rel, pairs):
    p = f"{root}/{rel}"
    s = open(p, encoding="utf-8").read()
    for old, new in pairs:
        assert s.count(old) == 1, (rel, old[:90])
        s = s.replace(old, new)
    open(p, "w", encoding="utf-8").write(s)


edit(
    "docs/design/intro-credits/spec.md",
    [
        (
            """the cold-open music merged across the 3.5 s gap bridge, or the music). The season step walks the matcher's clusters in its ranking order
(`matcher.intro_candidates`) and takes the first that passes all three guards; the first one left must have the
quorum, as today, and the silence guard then runs on it (`season.guarded_pick`, `season_intro`):""",
            """the cold-open music merged across the 3.5 s gap bridge, or the music). The season step walks the matcher's clusters
in its ranking order (`matcher.intro_candidates`) and takes the first that passes all three guards; the first one left
must have the quorum, as today, and the silence guard then runs on it (`season.guarded_pick`, `season_intro`):""",
        )
    ],
)
edit(
    "docs/guides.md",
    [
        (
            """library's folders counts as one season, in matching and in the Season view. The first episode of a season a job checks
fingerprints every member that has no fingerprint yet, on a worker (once per file; a replaced file is fingerprinted again). The rest of the season then matches from those saved
fingerprints, normally without taking a worker.""",
            """library's folders counts as one season, in matching and in the Season view. The first episode of a season a job checks
fingerprints every member that has no fingerprint yet, on a worker (once per file; a replaced file is fingerprinted
again). The rest of the season then matches from those saved fingerprints, normally without taking a worker.""",
        )
    ],
)
edit(
    "tests/markers/audio/test_season.py",
    [
        (
            """            store.set_fingerprint(rec.id, size=rec.size, mtime_ns=rec.mtime_ns, window="intro", start_s=0.0,
                                  length_s=fingerprint.window_s(DUR), algorithm=1, points=fake_points(path).tobytes())  # fmt: skip
            store.set_frame_rate(rec.id, FILM, identity=(rec.size, rec.mtime_ns))""",
            """            data = fake_points(path).tobytes()
            store.set_fingerprint(rec.id, size=rec.size, mtime_ns=rec.mtime_ns, window="intro", start_s=0.0,
                                  length_s=fingerprint.window_s(DUR), algorithm=1, points=data)  # fmt: skip
            store.set_frame_rate(rec.id, FILM, identity=(rec.size, rec.mtime_ns))""",
        )
    ],
)
