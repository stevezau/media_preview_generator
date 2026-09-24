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
    "media_preview_generator/markers/audio/season.py",
    [
        (
            """A season group is the episodes of a file's season folders with the same season number in their names, at most the 40
nearest by episode number (a flat folder can hold hundreds). The season folders are the file's own and, in a library
spread over several disks, the same folder under the library's other folders (``season_folders``). Fingerprinting needs a worker (CPU ffmpeg, at most two at once): the
first episode of a group to get one fingerprints every member that has none, so the rest of the season matches from
cached fingerprints on the checking threads. An episode alone in its folder (a new season's first weekly release) matches against up to four cached
episodes of the previous season; that answer is a hint (``Source.SEASON_AUDIO_PREVIOUS``).""",
            """A season group is the episodes of a file's season folders with the same season number in their names, at most the 40
nearest by episode number (a flat folder can hold hundreds). The season folders are the file's own and, in a library
spread over several disks, the same folder under the library's other folders (``season_folders``). Fingerprinting
needs a worker (CPU ffmpeg, at most two at once): the first episode of a group to get one fingerprints every member
that has none, so the rest of the season matches from cached fingerprints on the checking threads. An episode alone in
its group (a new season's first weekly release) matches against up to four cached episodes of the previous season;
that answer is a hint (``Source.SEASON_AUDIO_PREVIOUS``).""",
        )
    ],
)
edit(
    "media_preview_generator/markers/audio/fingerprint.py",
    [
        (
            """    \"\"\"The ``fingerprints`` window a fingerprint is cached under: ``WINDOW`` for the file's own audio, and one per retime
    factor for its audio retimed to another speed (a file has one own speed, so the factor names the group's).\"\"\"""",
            """    \"\"\"The ``fingerprints`` window a fingerprint is cached under: ``WINDOW`` for the file's own audio, and one per
    retime factor for its audio retimed to another speed (a file has one own speed, so the factor names the group's).\"\"\"""",
        )
    ],
)
edit(
    "media_preview_generator/markers/probe.py",
    [
        (
            """    cmd = [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_chapters",
           "-show_entries", "stream=codec_type,avg_frame_rate,r_frame_rate:stream_disposition=attached_pic", path]  # fmt: skip""",
            """    entries = "stream=codec_type,avg_frame_rate,r_frame_rate:stream_disposition=attached_pic"
    cmd = [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_chapters",
           "-show_entries", entries, path]  # fmt: skip""",
        )
    ],
)
edit(
    "media_preview_generator/markers/store.py",
    [
        (
            """        and end-picture checks, detector runs and failures, the season intro-chapter limit, the frame rate, Check
        servers' re-read counts and every server's ``publish_basis``, so the next run offers the markers to every server again
        even when""",
            """        and end-picture checks, detector runs and failures, the season intro-chapter limit, the frame rate, Check
        servers' re-read counts and every server's ``publish_basis``, so the next run offers the markers to every server
        again even when""",
        )
    ],
)
edit(
    "media_preview_generator/upgrade.py",
    [
        (
            """#: Set by v16, v17 and v18: once the job manager runs, the app queues the one job that decides the files in Intro & Credits'
#: Needs review, those waiting for their item's other versions and those whose intro rests on season audio again
#: (``web.app``). Cleared when that job completes""",
            """#: Set by v16, v17 and v18: once the job manager runs, the app queues the one job that decides the files in Intro &
#: Credits' Needs review, those waiting for their item's other versions and those whose intro rests on season audio
#: again (``web.app``). Cleared when that job completes""",
        )
    ],
)
edit(
    "docs/design/intro-credits/spec.md",
    [
        (
            """**Guards against idents and music beds** (season audio v5; the dense-core exemptions v7; §14 2026-09-24). A network ident at the start of the file,
or a music bed under the cold open, repeats in every episode just as the theme does, and outranks a short title card
("Accused: Guilty or Innocent", A&E: 13 of 15 intros were the A&E logo, the logo plus the cold-open music merged across
the 3.5 s gap bridge, or the music).""",
            """**Guards against idents and music beds** (season audio v5; the dense-core exemptions v7; §14 2026-09-24). A network
ident at the start of the file, or a music bed under the cold open, repeats in every episode just as the theme does,
and outranks a short title card ("Accused: Guilty or Innocent", A&E: 13 of 15 intros were the A&E logo, the logo plus
the cold-open music merged across the 3.5 s gap bridge, or the music).""",
        ),
        (
            """and names two speeds only: film (23.976, 24) and PAL (25); any other rate is matched as it plays. A group whose files play at both is matched at the speed most of them play at (film on a tie,
`speed.match_speed`, `season.SeasonClock`);""",
            """and names two speeds only: film (23.976, 24) and PAL (25); any other rate is matched as it plays. A group whose
files play at both is matched at the speed most of them play at (film on a tie, `speed.match_speed`,
`season.SeasonClock`);""",
        ),
    ],
)
edit(
    "docs/guides.md",
    [
        (
            """number). When a library spans several disks and a season is split across them, the same season folder on each of the
library's folders counts as one season, in matching and in the Season view. The first episode of a season a job checks fingerprints every member that has no fingerprint yet, on a
worker""",
            """number). When a library spans several disks and a season is split across them, the same season folder on each of the
library's folders counts as one season, in matching and in the Season view. The first episode of a season a job checks
fingerprints every member that has no fingerprint yet, on a worker""",
        )
    ],
)
edit(
    "docs/reference.md",
    [
        (
            """- `episodes` — the episodes matched as one season (same season folder, on every disk of the library, and season
  number; at most the 40 nearest in a flat folder of hundreds; extras left out), each with `path`, `name`, `episode` (`"E01"`), `known` (the app has looked at
  it),""",
            """- `episodes` — the episodes matched as one season (same season folder, on every disk of the library, and season
  number; at most the 40 nearest in a flat folder of hundreds; extras left out), each with `path`, `name`, `episode`
  (`"E01"`), `known` (the app has looked at it),""",
        )
    ],
)
