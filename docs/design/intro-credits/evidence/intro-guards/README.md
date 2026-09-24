# Season audio's guards: dense-core exemptions and online logos — evidence

Spec §5.3 guards, §5.5 rule 2, §14 2026-09-25 "Two intro fixes" (season audio v7).

1. **Dense-core exemptions** (`season.needs_dense_core`): a cluster starting at 2–30 s that is at least 30 s long, and
   one starting after 30 s that is at least 10 s long and found by at least 2 other episodes, need no dense core (a
   theme under dialogue matches only in patches). Kept out on purpose: Accused S04E06 (an 8.8 s music bed) and The Fall
   season 3 (a recap one other episode shares).
2. **Online logo at the file start** (`decide._is_online_logo`): an IntroDB/TheIntroDB intro starting in the first 2 s
   and shorter than 10 s is dropped. The Fixers (Netflix): IntroDB gives the "N" logo, 0–7 s, for all 10 episodes.

## Headline numbers (season audio alone, useful / wrong / missed)

| Set | Before | After |
|---|---|---|
| lab 118 | 91 / 12 / 15 | 91 / 12 / 15 |
| held-out 175 | 123 / 4 / 48 | 124 / 4 / 47 (Sleepy Hollow S04E08) |
| Accused | 2 / 0 / 54 | 3 / 0 / 53 (S01E01) |
| library chapter set | 107 / 59 / 58 | 111 / 59 / 54 (The Sinner S01E04/E05/E07, Reba S04E22) |

Production replay of 279 season audio answers (277 replayable): 4 new answers, none replaced (Accused S01E01, Far Away
S01E27/E28, Somebody Somewhere S03E06). The logo rule matches exactly the 10 The Fixers rows in production, none of the
43 verified online cases and none of IntroDB's intros for the 350 regression files.

## Files

| File | What |
|---|---|
| `t3_sets.py` (`t3_sets.log`, local-only) | Fix 1 on the four intro sets, before (every stretch needs its dense core) and after, on the #310 fingerprints and cached end-picture shares |
| `t3_fix2_prod.py` | Fix 2 on production: every prod file with an IntroDB/TheIntroDB intro in the first 2 s and under 10 s, decided again from its stored evidence (markers.db snapshot, read-only), with and without the rule |
| `t3_ref_sens.py`, `t3_check_composed.py`, `t3_mutate.py` | Checks of the test reference's sensitivity, a composed-marker case, and mutation runs (apply one mutation, run the tests, restore) |
| `prod-replay/kq_*.py` | The production season step replayed from prod fingerprints (read-only): ranked clusters and guard verdicts (`kq_replay.py`), shipped vs variant season steps on the four sets and on every prod TV file (`kq_variants.py`, `kq_prod_variants.py`), and comparisons |
| `edit-scripts/` | The scripts that applied the change, its tests and its docs to the lane's worktree (a record, not a tool) |

The scripts ran from the session scratchpad (fingerprints, shares and `prod_markers.db` there and in
`~/.cache/markers_eval`); their paths still point there.
