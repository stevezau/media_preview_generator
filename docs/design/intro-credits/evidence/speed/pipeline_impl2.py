"""Read the frame rate for decide only for an answer of a source the user has on."""

from pathlib import Path

p = Path(
    "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-ab7b211236d423de3/"
    "media_preview_generator/markers/pipeline.py"
)
s = p.read_text()
reps = [
    (
        """def _frame_rate(ctx: PipelineContext, rec: FileRecord, evidence: Iterable[Candidate]) -> float | None:
    \"\"\"The frame rate decisions read online times on the file's clock by (``decide`` rule 12): the stored one, read
    first for a file stored before frame rates were (``season.frame_rate_of``) only when an answer that may be timed on
    another release is among its evidence. Season audio reads the rate it needs itself.\"\"\"
    if any(timed_on_any_release(c) for c in evidence):
        return frame_rate_of(ctx, rec, probe=probe_media)
    return ctx.store.get_frame_rate(rec.id)[1]
""",
        """def _frame_rate(
    ctx: PipelineContext, rec: FileRecord, evidence: Iterable[Candidate], order: tuple[str, ...]
) -> float | None:
    \"\"\"The frame rate decisions read online times on the file's clock by (``decide`` rule 12): the stored one, read
    first for a file stored before frame rates were (``season.frame_rate_of``) only when an answer of a source in
    ``order`` (the ones turned on) that may be timed on another release is among its evidence. Season audio reads the
    rate it needs itself.\"\"\"
    if any(timed_on_any_release(c) and c.source.value in order for c in evidence):
        return frame_rate_of(ctx, rec, probe=probe_media)
    return ctx.store.get_frame_rate(rec.id)[1]
""",
    ),
    (
        "    frame_rate = _frame_rate(ctx, rec, evidence)\n",
        "    frame_rate = _frame_rate(ctx, rec, evidence, order)\n",
    ),
]
for old, new in reps:
    assert s.count(old) == 1, old[:80]
    s = s.replace(old, new, 1)
p.write_text(s)
print("ok")
