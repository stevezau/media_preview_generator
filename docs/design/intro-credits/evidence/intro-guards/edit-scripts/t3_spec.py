import sys

p = sys.argv[1]
s = open(p).read()


def rep(old, new):
    global s
    assert s.count(old) == 1, old[:90]
    s = s.replace(old, new)


rep(
    """≥ 50% of the other episodes in the group (≥ 1 when only one other). Group = the video files in the episode's folder
with the same parsed season number, at most the 40 nearest by episode number (a flat folder can hold hundreds;
server-agnostic); mixed releases in one season work (Rick and Morty S01).""",
    """≥ 50% of the other episodes in the group (≥ 1 when only one other). Group = the video files in the episode's season
folder with the same parsed season number, at most the 40 nearest by episode number (a flat folder can hold hundreds;
server-agnostic); mixed releases in one season work (Rick and Morty S01). The season folder is read on every disk of
the library (§14 2026-09-24): the same folder, relative to the deepest library folder holding the file (at least one
folder down), under each other folder of every enabled server's library that holds the file, where it exists
(`season.season_folders`, `season_videos`; library folders nested in or around that one, `/`, a folder that is the
same directory as one already listed, and a path not in normal form are left out). The Season view and its Publish
list the same group.""",
)
rep(
    """**Guards against idents and music beds** (season audio v5, §14 2026-09-24).""",
    """**Guards against idents and music beds** (season audio v5; the dense-core exemptions v7; §14 2026-09-24).""",
)
rep(
    """  the partners must be at least 8 s (a partner hit twice counts its longest). The matcher's runs bridge 3.5 s gaps; the
  core doesn't.""",
    """  the partners must be at least 8 s (a partner hit twice counts its longest). The matcher's runs bridge 3.5 s gaps; the
  core doesn't. A theme sung or played under dialogue has no dense core either, so two kinds of cluster need none
  (`season.needs_dense_core`): one starting at 2–30 s that is at least 30 s long (its end picture is still checked),
  and one starting after 30 s that is at least 10 s long and found by at least 2 other episodes. The limits were set
  after seeing what they keep out: an 8.8 s music bed after 30 s (Accused S04E06) and a recap only one other episode
  shares (The Fall season 3).""",
)
rep(
    """| **v3 + guards** (season audio v5: file start, dense core, end picture; §14 2026-09-24) | **91 (77%)** | **12** | **15** |
""",
    """| **v3 + guards** (season audio v5: file start, dense core, end picture; §14 2026-09-24) | **91 (77%)** | **12** | **15** |
| **v3 + guards, dense-core exemptions** (season audio v7; §14 2026-09-24) | **91 (77%)** | **12** | **15** |
""",
)
rep(
    """   credits window moves both, §8; the earliest credits start they keep is `decide.earliest_credits_start_ms`, which
   the credit text detector's reads before its tail are bounded by, §5.4 step 8.)""",
    """   credits window moves both, §8; the earliest credits start they keep is `decide.earliest_credits_start_ms`, which
   the credit text detector's reads before its tail are bounded by, §5.4 step 8.) An IntroDB or TheIntroDB intro that
   starts in the first 2 s and is shorter than 10 s fails too: it is a logo at the start of the file, not the show's
   intro (The Fixers: IntroDB gives Netflix's "N", 0–7 s, for all 10 episodes), the stretch season audio passes over
   in its own clusters (§5.3 "File start"; §14 2026-09-24). A marker composed from agreeing sources is judged on its
   times alone.""",
)
open(p, "w").write(s)
