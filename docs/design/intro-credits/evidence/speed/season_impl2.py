"""Apply the review's detect_season_audio changes: the episode's own rate read before matching, the files left out for a
failed retimed fingerprint kept out of ``matched``, and a cancel after matching stopping the run."""

from pathlib import Path

p = Path(
    "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-ab7b211236d423de3/"
    "media_preview_generator/markers/audio/season.py"
)
s = p.read_text()
reps = [
    (
        """    def retimed(member: FileRecord, factor: float) -> np.ndarray | None:
        if cancel_check and cancel_check():
            raise DetectorUnavailableError("cancelled")
        phase("Fingerprinting audio…")  # the worker row's existing words: a retimed file is fingerprinted once more
        return fingerprint_of(member, factor)

    phase("Fingerprinting audio…")
    own = fingerprint_of(rec)
""",
        """    left_out: set[str] = set()

    def retimed(member: FileRecord, factor: float) -> np.ndarray | None:
        if cancel_check and cancel_check():
            raise DetectorUnavailableError("cancelled")
        phase("Fingerprinting audio…")  # the worker row's existing words: a retimed file is fingerprinted once more
        found = fingerprint_of(member, factor)
        if found is None:
            left_out.add(member.canonical_path)
        return found

    phase("Fingerprinting audio…")
    own = fingerprint_of(rec)
    frame_rate_of(ctx, rec)
""",
    ),
    (
        """    matching = _matching(
        ctx, rec, records, points, group_size=len(group.episodes), previous_files=None, retimed=retimed
    )
    phase("Matching season audio…")
""",
        """    matching = _matching(
        ctx, rec, records, points, group_size=len(group.episodes), previous_files=None, retimed=retimed
    )
    if cancel_check and cancel_check():
        # A sibling's fingerprint cancelled part way is left out like a failed one: no answer without it.
        raise DetectorUnavailableError("cancelled")
    phase("Matching season audio…")
""",
    ),
    (
        """    if segment is not None:
        candidates.append(_candidate(segment, len(matching.files) - 1, matching.source))
    previous_used = set(matching.previous) if matching is not None else set()

    matched = {path: records[path] for path in points.keys() | previous_used}
""",
        """    if segment is not None:
        candidates.append(_candidate(segment, len(matching.files) - 1, matching.source))

    # Every file read for the match (the previous season's included, which _matching adds to records) but one left out
    # for want of its retimed fingerprint: that one enters the signature as it is, so its fingerprint made later makes
    # this answer due.
    matched = {path: member for path, member in records.items() if path not in left_out}
""",
    ),
]
for old, new in reps:
    assert s.count(old) == 1, old[:80]
    s = s.replace(old, new, 1)
p.write_text(s)
print("ok")
