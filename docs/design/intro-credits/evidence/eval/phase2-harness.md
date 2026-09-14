# Phase 2 accuracy harness results

Harness: `tools/markers_eval` (see its README). Details with file paths stay local (`phase2_reproduce*.json`,
git-ignored).

## Reproduction gate (Task 6)

- Date: 2026-09-14, on `storage`.
- Code: `feat/markers-detection` at `61d5071` plus the Task 6 harness.
- ffmpeg: `ffmpeg version 8.0.1-3ubuntu2` (`/usr/bin/ffmpeg`), run through the app's own fingerprint command
  (`markers/audio/fingerprint.py`) and window.
- Set: 24 seasons, 124 episodes. 118 of them have an intro chapter, and those are the ones the tally judges.

**Result: passed.** The app returns the §5.3 numbers exactly, with the app's own code:

| Mode | Files matched | Useful | Wrong | Missed | Port vs reference | Drift | Gate |
|---|---|---|---|---|---|---|---|
| Spec §5.3 (v3, alg1 stereo) | 124 | 91 | 13 | 14 | | | |
| Eval lists: at most 8 files per season, only files with chapters (how §5.3 was measured) | 124 | **91** | **13** | **14** | **0** | **0** | pass |
| Full folder: every episode file in the season folder (what the app does) | 158 | 91 | 10 | 17 | 0 | 26 | pass |

**Eval lists.** All 124 answers are float-for-float equal to the stored `eval_results_v3.json`: 110 segments plus 14
with no answer. That is stricter than the two-point drift tolerance. The numpy port equals the reference on every
episode. The order of the ffmpeg options doesn't matter here. On 3 spot-checked files (3 shows, 3,998–7,248 points),
the app's `-threads 2 -ss 0 -t W -i file ... -algorithm 1` gives the same points as the prototype's
`-ss 0 -i file -t W`.

**Full folder (plan gap G5).** 6 seasons have more files on disk than the eval matched:

- The Simpsons S03: 23 files vs 8 in the eval.
- Criminal Minds S04: 18 vs 8.
- Fresh Off the Boat S05: 12 vs 8.
- That '90s Show S02: 7 vs 4.
- Interview With The Vampire S03: 4 vs 3.
- The Challenge All Stars S05: 4 vs 3.

The eval dropped files for two reasons: its `[:8]` cap, and files without chapters.

26 answers change in 5 of those seasons:

- **The Simpsons S03: 3 answers go from wrong to missed.** The variable couch gag no longer reaches the 50% quorum of
  22 other episodes.
- Criminal Minds S04, Fresh Off the Boat S05, That '90s Show S02 and Interview With The Vampire S03: 20 answers change
  their support count or median but stay useful. 3 more episodes without an intro chapter also change.

The whole folder is no worse on useful answers and has 3 fewer wrong ones.

Runtime:

- Cold cache: 251 s for the 124 eval files and 96 s for the 34 extra folder files, with 2 ffmpeg at once, `nice -n 19`,
  on a busy host (load about 16–20).
- Warm cache: the gate takes 17 s (eval lists) or 24 s (full folder), including the pure-Python reference.
