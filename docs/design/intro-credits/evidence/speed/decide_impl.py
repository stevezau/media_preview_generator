"""Apply the review's decide.py changes (exact replacements)."""

from pathlib import Path

p = Path(
    "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-ab7b211236d423de3/"
    "media_preview_generator/markers/decide.py"
)
s = p.read_text()
reps = [
    (
        "from .speed import online_time_scale\n",
        "from .speed import PAL_FPS, online_time_scale, playback_speed\n",
    ),
    (
        """# IntroDB and TheIntroDB take no duration: their times come from whichever release their users timed, so on a file at
# the other speed of a PAL speed-up they run 4.3 % off (Bones S07E01, 25 fps: IntroDB 324-354 s from a 23.976 release,
# the file's own theme 310-338 s). SkipDB matches the file's duration, so its times are this file's.
_TIMED_ON_ANY_RELEASE = frozenset({Source.INTRODB, Source.THEINTRODB})
""",
        """# IntroDB and TheIntroDB take no duration: their times come from whichever release their users timed, so on a file at
# the other speed of a PAL speed-up they run 4.3 % off (Bones S07E01, 25 fps: IntroDB 324-354 s from a 23.976 release,
# the file's own theme 310-338 s). An importer plugin's copy of them is the same times. SkipDB matches the file's
# duration, so its times are this file's.
_TIMED_ON_ANY_RELEASE = frozenset({Source.INTRODB, Source.THEINTRODB})
_TIMED_ON_ANY_RELEASE_COPY = "introdb"
""",
    ),
]
for old, new in reps:
    assert s.count(old) == 1, old[:70]
    s = s.replace(old, new, 1)

start = s.index("def _on_file_clock(")
end = s.index("def _decide_type(")
s = (
    s[:start]
    + '''def timed_on_any_release(candidate: Candidate) -> bool:
    """Whether a candidate's times may come from another release than this file: IntroDB, TheIntroDB, or an importer
    plugin's copy of them (they take no duration)."""
    if candidate.source is Source.SERVER_MARKERS_IMPORTED:
        return candidate.copied_from == _TIMED_ON_ANY_RELEASE_COPY
    return candidate.source in _TIMED_ON_ANY_RELEASE


def _on_file_clock(sane: list[Candidate], ctx: DecisionContext) -> list[Candidate]:
    """Online times read on the file's own clock where they were timed on a release at the other speed
    (``speed.online_time_scale``: a 25 fps file and a film-rate one), spec §5.5 rule 12.

    1. On a 25 fps file, a server's own marker that agrees with an online answer's raw times and with no source that
       reads the file is left out: it can have been made for another release of the item (Plex's own intros on the
       25 fps Bones files, made in 2025, sit at the film-rate times, as IntroDB's do), so it is the same mistake again,
       not a second opinion. An online answer it alone would have confirmed then goes where a lone online answer goes.
    2. An answer :func:`timed_on_any_release` is then read scaled only when, as it is, it agrees with no candidate of
       another independent group and, scaled, it is sane and agrees with one. A raw reading that agrees is always kept:
       an early intro can agree both ways (4.3 % of a 60 s end is 2.6 s), and scaling it then would move an edge no
       source reported. An answer that agrees neither way stays as it is, so it still disagrees.

    Each answer is judged against the candidates as they came, so the result doesn't depend on their order.

    Args:
        sane: One type's candidates that passed :func:`sanity_problem`.
        ctx: The file's context (its frame rate).

    Returns:
        The candidates, a scaled answer in place of its raw one, without the server markers step 1 leaves out.
    """
    scale = online_time_scale(ctx.frame_rate)
    if scale is None:
        return sane
    d = ctx.duration_ms
    if playback_speed(ctx.frame_rate) == PAL_FPS:
        online = [c for c in sane if timed_on_any_release(c)]
        readers = [c for c in sane if c.source in _READS_THE_FILE]
        sane = [
            c
            for c in sane
            if not (
                c.source is Source.SERVER_MARKERS
                and any(_agree(c, o, d) for o in online)
                and not any(_agree(c, r, d) for r in readers)
            )
        ]
    out = []
    for c in sane:
        if timed_on_any_release(c):
            others = [o for o in sane if _group(o) != _group(c)]
            end_ms = None if c.end_ms is None else round(c.end_ms * scale)
            scaled = replace(c, start_ms=round(c.start_ms * scale), end_ms=end_ms)
            if (
                not any(_agree(c, o, d) for o in others)
                and sanity_problem(scaled, ctx) is None
                and any(_agree(scaled, o, d) for o in others)
            ):
                c = scaled
        out.append(c)
    return out


'''
    + s[end:]
)
p.write_text(s)
print("ok")
