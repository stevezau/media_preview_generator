"""Apply the review's pipeline.py changes: the rate stored with the run's own probe, and read lazily only when an
online answer needs it."""

from pathlib import Path

p = Path(
    "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-ab7b211236d423de3/"
    "media_preview_generator/markers/pipeline.py"
)
s = p.read_text()
reps = [
    (
        """    if not unchanged or probe is not None:
        ctx.note_answer_changed()
    _store_frame_rate(ctx, rec, probe)
""",
        """    if not unchanged or probe is not None:
        ctx.note_answer_changed()
""",
    ),
    (
        """    if probe is not None:
        # The episode-only chapter names read the kind from the PATH, not the resolved kind: the path is""",
        """    if probe is not None:
        ctx.store.set_frame_rate(rec.id, probe.frame_rate, identity=(rec.size, rec.mtime_ns))
        # The episode-only chapter names read the kind from the PATH, not the resolved kind: the path is""",
    ),
    (
        """def _store_frame_rate(ctx: PipelineContext, rec: FileRecord, probe: MediaProbe | None) -> None:
    \"\"\"Store the file's video frame rate: the one this run's probe read, or, for a file stored before frame rates were
    read, one read now (season audio matches 25 fps and film-rate releases at one speed, and decisions read online
    times on the file's clock by it: ``markers.speed``).

    A rate read for the first time changes no answer. A probe that fails leaves the rate unknown until a later run
    reads it; the run goes on without it.
    \"\"\"
    if probe is None:
        if ctx.store.get_frame_rate(rec.id)[0]:
            return
        try:
            probe = probe_media(rec.canonical_path, ffprobe=ctx.ffprobe)
        except ProbeError as exc:
            logger.debug("Frame rate of {} unknown for now: {}", os.path.basename(rec.canonical_path), exc)
            return
    ctx.store.set_frame_rate(rec.id, probe.frame_rate, identity=(rec.size, rec.mtime_ns))
""",
        """def _frame_rate(ctx: PipelineContext, rec: FileRecord, evidence: Iterable[Candidate]) -> float | None:
    \"\"\"The frame rate decisions read online times on the file's clock by (``decide`` rule 12): the stored one, read
    first for a file stored before frame rates were (``season.frame_rate_of``) only when an answer that may be timed on
    another release is among its evidence. Season audio reads the rate it needs itself.\"\"\"
    if any(timed_on_any_release(c) for c in evidence):
        return frame_rate_of(ctx, rec, probe=probe_media)
    return ctx.store.get_frame_rate(rec.id)[1]
""",
    ),
    (
        """    locked = ctx.store.get_locked(rec.id)
    frame_rate = ctx.store.get_frame_rate(rec.id)[1]
""",
        """    locked = ctx.store.get_locked(rec.id)
    frame_rate = _frame_rate(ctx, rec, evidence)
""",
    ),
    (
        "from .audio.season import season_audio_spec, season_intro_chapter_limits\n",
        "from .audio.season import frame_rate_of, season_audio_spec, season_intro_chapter_limits\n",
    ),
    (
        "from .probe import MediaProbe, ProbeError, ffprobe_path_for, probe_media\n",
        "from .probe import ProbeError, ffprobe_path_for, probe_media\n",
    ),
]
for old, new in reps:
    assert s.count(old) == 1, old[:80]
    s = s.replace(old, new, 1)
p.write_text(s)
print("ok")
