import sys

p = sys.argv[1]
s = open(p, encoding="utf-8").read()
anchor = """  (30 for 30 S04, Sort Of S03) get no season audio answer before or after. A release sped up with its pitch kept, and
  rates other than 23.976/24/25, are matched as they play, as before.
"""
assert s.endswith(anchor), s[-300:]
s += """- 2026-09-25 · **Three intro fixes: dense-core exemptions, online logos, a season on every disk** (season audio v7;
  owner: automatic, at par or better than Plex). Numbers are useful / wrong / missed.
  1. **Dense-core exemptions** (§5.3 guards, `season.needs_dense_core`): a cluster starting at 2–30 s that is at least
     30 s long, and one starting after 30 s that is at least 10 s long and found by at least 2 other episodes, need no
     dense core (a theme under dialogue matches only in patches). The limits were chosen after seeing the failures
     they keep out (Accused S04E06, an 8.8 s music bed; The Fall season 3, a recap one other episode shares). Season
     audio alone, on top of the two-speed change: lab 118 **91 / 12 / 15** unchanged; held-out 175 **123 / 4 / 48 →
     124 / 4 / 47** (Sleepy Hollow S04E08); Accused **2 / 0 / 54 → 3 / 0 / 53** (S01E01); library chapter set
     **107 / 59 / 58 → 111 / 59 / 54** (The Sinner S01E04, E05, E07; Reba S04E22). Replaying production's 279 season
     audio answers (277 replayable): 4 new answers where there were none, none replaced: Accused S01E01, Far Away
     S01E27 and E28 (16.1–102.3 s, as E26), Somebody Somewhere S03E06 (66.1–76.9 s; SkipDB says 66.3–76.7 s).
  2. **Online logo at the file start** (§5.5 rule 2, `decide._is_online_logo`): an IntroDB or TheIntroDB intro
     starting in the first 2 s and shorter than 10 s is dropped, the rule season audio applies to its own clusters.
     The Fixers (Netflix): IntroDB gives the "N" logo, 0–7 s, for all 10 episodes; E01 and E07, which have no
     chapters, go from Needs review ("sources disagree") to season audio alone (263.0–284.8 s, 205.0–227.2 s). In
     production the rule matches exactly those 10 rows (the other 8 stay decided by their chapters); none of the 43
     verified online cases and none of IntroDB's intros for the 350 regression files match it. A marker composed from
     agreeing sources is judged on its times only.
  3. **A season on every disk** (§5.3 group, `season.season_folders`): the owner's TV library is spread over three
     disks by a pool (and a fourth disk), so a season's episodes are scattered: **8,069 of 15,478** season folders hold
     episodes on two or three disks (88,966 files), not the 22 first counted from markers.db. The group now takes the
     same season folder on each of the library's folders; the Season view, its Publish and the Season follow-up
     job's name follow. Measured in app mode (whole folders; members without a cached fingerprint fingerprinted, so
     both groupings use the same fingerprints): Accused **3 / 0 / 53** unchanged (one disk); lab 118 **91 / 10 / 17
     → 90 / 8 / 20** (Outlander S08E02/E08 wrong → missed; The Sinner S03E07 missed → useful; The WONDERfools E02/E04
     useful → missed); held-out 175 **124 / 4 / 47 → 121 / 5 / 49** (Alias S02 +5 useful, Bluey S01 +2, Dateline
     missed → wrong; SPY x FAMILY S01 10 useful → missed: its 25 episodes hold two openings, each now found by fewer
     than half the others); chapter set **106 / 58 / 60 → 113 / 54 / 57** (Sex and the City S04 4 wrong → useful,
     South Park S12 +2, Glass Heart +1; Family Guy S14 and Turning Point 9/11 +3 wrong, 3 wrong → missed). All 573:
     **+3 useful, −5 wrong, +2 missed**. Production replay: Lioness S02E08 (alone on its disk) none → **63.3–122.7 s**
     (7 of 7, as IntroDB); Star Trek: Strange New Worlds S04E10 its title sequence 562.9–669.8 s → **0.0–27.6 s**, a
     "Star Trek 60" bumper at the start of every S04 episode that 9 of 9 others share and that outranks the title
     sequence (found by fewer) once the season is whole — as it already did on the S04 episodes whose own folder held
     three others. Other answers keep their times (Lioness E01–E06, NOVA S53, From Old Country Bumpkin S02E12,
     Deadly Influence S01E03: more support, within 0.6 s).
"""
open(p, "w", encoding="utf-8").write(s)
