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

## Season step gate (Task 7)

- Date: 2026-09-15, on `storage`, with the same fingerprint cache.
- Code: `feat/markers-detection` at `f4799a9` plus Task 7 (after its review fixes). The harness now takes each
  season's files from the app's `season_group` (the folder's episodes with the same season number in their names, at
  most the 40 nearest) and judges the app's season step (`markers/audio/season.py`): the v3 matcher, a silence
  guard, and pairs skipped because they provably hold no intro.

**Result: passed, unchanged in both modes.**

| Mode | Season step: useful / wrong / missed | Matcher alone | Port vs reference | Drift | Skipped pairs | Silence dropped |
|---|---|---|---|---|---|---|
| Eval lists | **91 / 13 / 14** | 91 / 13 / 14 | 0 | 0 | 0 | 0 |
| Full folder | **91 / 10 / 17** | 91 / 10 / 17 | 0 | 26 (the same 26 as Task 6) | 0 | 0 |

**Silence guard.** chromaprint gives silence one constant value, so two episodes' shared quiet can match like an
intro. The guard drops an intro whose points are more than half silence, counting every point the matcher would take
for the silence value (within ±2 of it, or at most 6 bits different). Swept on both modes, same counts in each:

| Guard drops an intro above this silence share | Useful answers lost | Wrong answers dropped |
|---|---|---|
| 0 % | 36 | 5 |
| 5 % | 5 | 0 |
| 10 %, 12 %, 14 %, 15 % | 4 | 0 |
| 25 %, **50 % (chosen)**, 75 % | 0 | 0 |

The most silent useful intro holds 17.5 % silence by that count (14.3 % counting only the exact value; Revenant S01).
No wrong answer holds more than 2.1 %, so the guard changes nothing here; the synthetic tests cover the shared-silence
case.

**Degenerate openings.** Matching two silent 900 s openings takes the matcher about 1.25 s per pair (about 7 minutes
for a 26-episode season). The season step skips a pair only when a proof shows the matcher can find no run of 120 s or
less in it (one constant value with at most short interruptions in both openings); a fuzz of 100,918 near-constant
pairs found no counterexample (4,368 accepted). Any other pair with more than 2,000,000 value matches (about 60 ms)
is matched exactly on a worker instead of a checking thread. On this set no pair comes near: the most value matches
of any same-season pair is 3,893, and the most silent opening is 0.8 % silence. A skipped silent 26-episode season
now takes 4 ms.

A plain match-count or silence-share limit was not used: 250 s of the same constant in both openings, a few seconds
apart, then a shared 40 s intro, has millions of value matches (and a 90 %-silent pair can be built the same way), yet
the matcher finds the intro.

**Pipeline run.** The real pipeline, store and season audio detector ran on the eval lists through symlinked season
folders, reading fingerprints from the cache: all 124 stored season audio candidates equal the port's segments, each
file was fingerprinted once, and every season's later episodes (100 checks) matched on the checking thread.
