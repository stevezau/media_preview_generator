"""Update the spec's §5.3 and §5.5 rule 12 text for the review's changes."""

from pathlib import Path

p = Path(
    "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-ab7b211236d423de3/"
    "docs/design/intro-credits/spec.md"
)
s = p.read_text()
reps = [
    (
        """step reads each file's video frame rate (`probe_media`, stored in `markers.db` `frame_rates`; a sibling stored before
it is probed once, on a worker) and names two speeds only: film (23.976, 24) and PAL (25); any other rate is matched as
it plays.""",
        """step reads each file's video frame rate (`probe_media`, stored in `markers.db` `frame_rates` with the identity it was
read from; a file stored before frame rates were read is probed once: the episode inline, like its own probe, a sibling
on a worker; a failed read is remembered for a day like a member's failed probe, and the file is matched as it plays)
and names two speeds only: film (23.976, 24) and PAL (25); any other rate is matched as it plays.""",
    ),
    (
        """matches is read back at its own speed (its answer, and its end-picture instants, aligned at the stretch's end). Retimed
pairs are cached under their own version per group speed (`RETIMED_PAIR_VERSIONS`). Measured on Bones S05: retimed so""",
        """matches is read back at its own speed (its answer, and its end-picture instants, aligned at the stretch's end). A
pair's runs are cached under a version naming each side's speed (own, retimed to film, retimed to 25 fps:
`SeasonClock.pair_version`), and a file's pairs go when its stored rate changes (a first rate included). A sibling whose
retimed fingerprint fails is left out of the match (a day, like a failed fingerprint) and enters the answer's signature
without it, so the answer is due again once it is made. Measured on Bones S05: retimed so""",
    ),
    (
        """    says 324–354 s), and the other way round on a film-rate file. On a 25 fps or film-rate file (`frame_rates`) such an
    answer is read scaled by 23.976/25 (or 25/23.976) when, scaled, it is sane and agrees with another independent
    source more closely than it does as it is (an intro early in the file can agree both ways); it then counts, and
    supplies times, on the file's own clock, never at its raw times. The sources that read the file (chapters, season
    audio, credit text) settle which reading applies before any other is asked: a server's marker can have been made
    for another release of the item (Plex's own Bones S05–S08 intros, made in 2025, sit at the film-rate times on the
    25 fps files, as IntroDB's do) and must not pull the raw times through. An entry timed at the file's own speed
    keeps its times, one that agrees neither way still disagrees, and no other rate is ever scaled. SkipDB matches the
    file's duration and is never scaled.""",
        """    says 324–354 s), and the other way round on a film-rate file. On a 25 fps or film-rate file (`frame_rates`, read
    for a file stored before them only when such an answer is among its evidence) an answer of IntroDB, TheIntroDB or
    an importer plugin's copy of them (`decide.timed_on_any_release`) is read scaled by 23.976/25 (or 25/23.976) only
    when, as it is, it agrees with no other independent source and, scaled, it is sane and agrees with one; it then
    counts, and supplies times, on the file's own clock, never at its raw times. A raw reading that agrees is always
    kept (an early intro agrees both ways: 4.3 % of a 60 s end is 2.6 s, and scaling it would move an edge no source
    reported), and one that agrees neither way still disagrees. On a 25 fps file a server's own marker that agrees with
    an online answer's raw times and with no source that reads the file is left out first: it can have been made for
    another release of the item (Plex's own Bones S05–S08 intros, made in 2025, sit at the film-rate times on the
    25 fps files, as IntroDB's do: 41 of 41 wrong), so it is the same mistake again, not a second source; an online
    answer only it confirmed goes where a lone online answer goes (rule 6). No other rate is ever scaled. SkipDB
    matches the file's duration and is never scaled.""",
    ),
]
for old, new in reps:
    assert s.count(old) == 1, old[:80]
    s = s.replace(old, new, 1)
p.write_text(s)
print("ok")
