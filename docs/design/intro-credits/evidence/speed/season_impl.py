"""Apply the review's season.py changes (exact replacements)."""

from pathlib import Path

p = Path(
    "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-ab7b211236d423de3/"
    "media_preview_generator/markers/audio/season.py"
)
s = p.read_text()
reps = [
    # MED 1: each side's speed in the pair version.
    (
        """# Pairs matched with a retimed fingerprint on either side are cached under a version of their own per group speed, so
# they never stand in for the two files' own-speed runs, nor for runs at the other speed.
RETIMED_PAIR_VERSIONS = {FILM_FPS: 1_000 + SEASON_AUDIO_VERSION, PAL_FPS: 2_000 + SEASON_AUDIO_VERSION}
""",
        """# A pair's runs are cached under a version naming the speed each side was matched at (its own, or retimed to film or
# to 25 fps), so runs matched from one pair of fingerprints never stand in for another pair's. A file's cached pairs
# also go when its stored frame rate changes (``MarkerStore.set_frame_rate``).
_PAIR_VERSION_STEP = 1_000
_RETIMED_TO = {FILM_FPS: 1, PAL_FPS: 2}
""",
    ),
    (
        """    def pair_version(self, first: str, second: str) -> int:
        \"\"\"The version a pair's runs are cached under: :data:`SEASON_AUDIO_VERSION` for two files at their own speed,
        else the retimed one for the group's speed.\"\"\"
        if first in self.factors or second in self.factors:
            return RETIMED_PAIR_VERSIONS[self.speed]
        return SEASON_AUDIO_VERSION
""",
        """    def pair_version(self, first: str, second: str) -> int:
        \"\"\"The version a pair's runs are cached under: :data:`SEASON_AUDIO_VERSION` for two files at their own speed,
        and another for each combination of the two sides' speeds (own, retimed to film, retimed to 25 fps).\"\"\"

        def side(path: str) -> int:
            return _RETIMED_TO[self.speed] if path in self.factors else 0

        return SEASON_AUDIO_VERSION + _PAIR_VERSION_STEP * (3 * side(first) + side(second))
""",
    ),
    # MED 2: the signature's fingerprint flags come from the store, the retimed fingerprint included.
    (
        """def _signature_item(ctx: PipelineContext, path: str) -> list:
    identity = _disk_identity(path)
    rec = ctx.store.get_file(path) if identity is not None else None
    current = rec is not None and (rec.size, rec.mtime_ns) == identity
    size, mtime_ns = identity or (None, None)
    fingerprinted = bool(current and has_cached_fingerprint(ctx.store, rec))
    return [path, size, mtime_ns, fingerprinted, ctx.store.get_frame_rate(rec.id)[1] if current else None]
""",
        """def _signature_item(ctx: PipelineContext, path: str) -> list:
    identity = _disk_identity(path)
    rec = ctx.store.get_file(path) if identity is not None else None
    current = rec is not None and (rec.size, rec.mtime_ns) == identity
    size, mtime_ns = identity or (None, None)
    return [path, size, mtime_ns, *(_fingerprints_item(ctx, rec) if current else [False, None, None])]


def _fingerprints_item(ctx: PipelineContext, rec: FileRecord) -> list:
    \"\"\"What a file's part of a signature says about its fingerprints: whether it has its own, its frame rate, and
    whether it has the one retimed to its other speed (None at an unknown or other rate), so a retimed fingerprint made
    after an answer left the file out makes that answer due.\"\"\"
    rate = ctx.store.get_frame_rate(rec.id)[1]
    own = playback_speed(rate)
    retimed = None
    if own is not None:
        other = FILM_FPS if own == PAL_FPS else PAL_FPS
        retimed = has_cached_fingerprint(ctx.store, rec, retime_factor(own, other))
    return [has_cached_fingerprint(ctx.store, rec), rate, retimed]
""",
    ),
    (
        """            rec = matched[path]
            items.append([path, rec.size, rec.mtime_ns, True, ctx.store.get_frame_rate(rec.id)[1]])
            continue
""",
        """            rec = matched[path]
            items.append([path, rec.size, rec.mtime_ns, *_fingerprints_item(ctx, rec)])
            continue
""",
    ),
    (
        """    \"\"\"What an answer is based on: each file's identity, whether it has a fingerprint, and its frame rate (a rate read
    later can make the file one to retime).
""",
        """    \"\"\"What an answer is based on: each file's identity, whether it has its fingerprint, its frame rate (a rate read
    later can make the file one to retime) and whether it has its retimed fingerprint (:func:`_fingerprints_item`).
""",
    ),
]
for old, new in reps:
    assert s.count(old) == 1, old[:80]
    s = s.replace(old, new, 1)
p.write_text(s)
print("ok")
