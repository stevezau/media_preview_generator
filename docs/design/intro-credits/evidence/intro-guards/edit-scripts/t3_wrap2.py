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
    "docs/reference.md",
    [
        (
            """  (`"E01"`), `known` (the app has looked at it), `duration_ms`, `intro` and `credits` (`{status, reason, marker, proposed}` as in `GET /api/markers/item`),
""",
            """  (`"E01"`), `known` (the app has looked at it), `duration_ms`, `intro` and `credits`
  (`{status, reason, marker, proposed}` as in `GET /api/markers/item`),
""",
        )
    ],
)
edit(
    "tests/markers/audio/test_season.py",
    [
        (
            '''    """Own-speed fingerprints: film-rate episodes hold the theme at OFFSETS, the 25 fps ones (``pal``) the sped-up one."""''',
            '''    """Own-speed fingerprints: film-rate episodes hold the theme at OFFSETS, the 25 fps ones (``pal``) the sped-up
    one."""''',
        ),
        (
            """            points = speed_points({s1[3]})(path)
            store.set_fingerprint(rec.id, size=rec.size, mtime_ns=rec.mtime_ns, window="intro", start_s=0.0,
                                  length_s=fingerprint.window_s(DUR), algorithm=1, points=points.tobytes())  # fmt: skip""",
            """            data = speed_points({s1[3]})(path).tobytes()
            store.set_fingerprint(rec.id, size=rec.size, mtime_ns=rec.mtime_ns, window="intro", start_s=0.0,
                                  length_s=fingerprint.window_s(DUR), algorithm=1, points=data)  # fmt: skip""",
        ),
        (
            """        s2e1_points = lambda path: retimed_points("x", TO_FILM) if path == s2e1 else speed_points({s1[3]})(path)  # noqa: E731""",
            """        def s2e1_points(path):
            return retimed_points("x", TO_FILM) if path == s2e1 else speed_points({s1[3]})(path)
""",
        ),
    ],
)
edit(
    "tests/markers/test_decide.py",
    [
        (
            """        assert d.marker == Marker(MarkerType.INTRO, round(324_000 * self.FILM_ON_PAL), round(354_000 * self.FILM_ON_PAL),
                                  ("introdb", "server_markers"))  # fmt: skip""",
            """        start, end = round(324_000 * self.FILM_ON_PAL), round(354_000 * self.FILM_ON_PAL)
        assert d.marker == Marker(MarkerType.INTRO, start, end, ("introdb", "server_markers"))""",
        )
    ],
)
