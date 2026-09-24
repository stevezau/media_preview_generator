import sys

root = sys.argv[1]


def edit(rel, pairs):
    p = f"{root}/{rel}"
    s = open(p).read()
    for old, new in pairs:
        assert s.count(old) == 1, (rel, old[:90])
        s = s.replace(old, new)
    open(p, "w").write(s)


edit(
    "docs/guides.md",
    [
        (
            """Season audio compares an episode with the other episodes of its season on disk: the video files in the same folder
with the same season number in their names (in a folder holding more than 40 of them, the 40 nearest by episode
number).""",
            """Season audio compares an episode with the other episodes of its season on disk: the video files in the same folder
with the same season number in their names (in a folder holding more than 40 of them, the 40 nearest by episode
number). When a library spans several disks and a season is split across them, the same season folder on each of the
library's folders counts as one season, in matching and in the Season view.""",
        ),
        (
            """season audio passes over such a stretch: one in the first 2 seconds must last at least 10 seconds, every one needs
about 8 seconds where the episodes' audio matches closely, and one starting in the first 30 seconds must end on the
same picture in the episodes it repeats in.""",
            """season audio passes over such a stretch: one in the first 2 seconds must last at least 10 seconds, every one needs
about 8 seconds where the episodes' audio matches closely (except one lasting 30 seconds or more, or one after the first
30 seconds that lasts 10 seconds or more and that at least 2 other episodes share: a theme under dialogue matches only
in patches), and one starting in the first 30 seconds must end on the same picture in the episodes it repeats in. An
online database's intro that starts in the first 2 seconds and lasts under 10 seconds (a streaming service's logo) is
ignored for the same reason.""",
        ),
    ],
)
edit(
    "docs/reference.md",
    [
        (
            """- `episodes` — the episodes matched as one season (same folder and season number; at most the 40 nearest in a flat
  folder of hundreds; extras left out),""",
            """- `episodes` — the episodes matched as one season (same season folder, on every disk of the library, and season
  number; at most the 40 nearest in a flat folder of hundreds; extras left out),""",
        ),
    ],
)
