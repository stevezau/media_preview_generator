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

## Full report (Task 15)

- **When and where:** 2026-09-15, on `storage`.
- **Code:** `feat/markers-detection` at `68f08a2` plus the Task 15 harness (`python -m tools.markers_eval report`).
- **Tools:** ffmpeg 8.0.1-3ubuntu2, with the same fingerprint cache as Tasks 6 and 7.
- **Plex's markers:** prod Plex's own markers, read read-only for the lab scale run (2026-09-14).
- **Details:** `phase2_report_lists.json` and `phase2_report_full_folder.json` stay local.
- **Runtime:** about 1 minute per mode with a warm cache.

### Intros: our decisions and Plex's own markers on the 118 episodes

On this set the truth is the intro chapters, so chapters can't be a source, and no online answers are recorded. What
remains is season audio plus Plex's own intro marker (as `server_markers`), decided by the real rules. Two rulings mean
the shipped rules publish nothing here:

- R2: season audio never decides alone.
- G3: season audio and a server's own marker never decide together.

So every row appears with G3 on (shipped) and G3 off (the two count as agreeing independent sources). "Skips story"
counts wrong answers that end more than 5 s late or start more than 15 s early. The other wrong answers stop inside
the intro, so the viewer only sees more of it.

| Row | Eval lists: useful / wrong / missed | Skips story | Full folder: useful / wrong / missed | Skips story |
|---|---|---|---|---|
| Plex's own intro marker (on 38 of 118) | 23 / 15 / 80 | 9 | 23 / 15 / 80 | 9 |
| Season audio alone (what Medium would publish without R2) | 91 / 13 / 14 | 10 | 91 / 10 / 17 | 10 |
| High, G3 on (shipped) | 0 / 0 / 118 | 0 | 0 / 0 / 118 | 0 |
| Medium, G3 on (shipped) | 0 / 0 / 118 | 0 | 0 / 0 / 118 | 0 |
| High, G3 off | 23 / 7 / 88 | 4 | 23 / 4 / 91 | 4 |
| Medium, G3 off | 23 / 7 / 88 | 4 | 23 / 4 / 91 | 4 |

**Gate** (Medium beats Plex: as useful or more, no more wrong; High no more wrong than Plex):

| G3 | Eval lists | Full folder |
|---|---|---|
| On (shipped) | **fail**: Medium has 0 useful against Plex's 23. High passes (0 wrong against 15) | **fail**, for the same reason |
| Off | **pass**: 23 useful each, 7 wrong against 15 | **pass**: 23 useful each, 4 wrong against 15 |

With G3 on, the gate fails by construction. Season audio and Plex's markers are both agreement-only, and G3 stops them
agreeing with each other, so nothing publishes where Plex publishes 38 intros.

**Which episodes G3 off changes.** Every one is an episode where Plex also answered:

| Show | Eval lists: useful / wrong | Full folder: useful / wrong |
|---|---|---|
| Married... with Children S06 | 8 / 0 | 8 / 0 |
| Revenant S01 | 4 / 0 | 4 / 0 |
| Frontier S03 | 4 / 0 | 4 / 0 |
| Marvel's Daredevil S03 | 2 / 3 | 2 / 3 |
| Outlander S08 | 1 / 1 | 1 / 1 |
| The WONDERfools S01 | 2 / 0 | 2 / 0 |
| Interview With The Vampire S03 | 2 / 0 | 2 / 0 |
| The Simpsons S03 | 0 / 3 | — (the full folder gives no answer) |

- **Plex agrees with our audio.** Plex's intro detection fails where season audio fails. On all 23 episodes where
  Plex is useful, season audio is useful too.
- **Shared wrong answers.** On 10 of Plex's 15 wrong answers (7 in full folder), season audio is wrong too. On 7 of
  them (4 in full folder) the two agree within 5 s, and those are G3 off's wrong answers:
  - Daredevil and Outlander run the title card 7–11 s past the chapter end.
  - The Simpsons couch gag makes the intro end early (eval lists only).
- **What G3 off adds.** It publishes exactly Plex's useful answers, and only the wrong answers season audio shares.
  It adds no useful answer Plex doesn't have. It removes 8 of Plex's wrong answers (11 in full folder).
- **Where the remaining useful answers are.** On 66 of the 118 episodes Plex has no intro marker and season audio
  alone is useful. With these sources, only R2 can reach them.

### Online cases

These are the 43 verified cases through the real parsers and rules, with the app's source orders. "+ Plex + audio"
adds Plex's own markers and whole-folder season audio for each case's file.

The first table uses the 32 cases whose truth isn't Plex's own markers. Rick and Morty S01's truth was taken from
Plex. Verdicts follow the phase-1 audit:

- An intro is wrong when it starts or ends more than 5 s outside the truth.
- Credits are wrong when they start more than 10 s early, and late when they start more than 30 s late.

| Setting | Evidence | Intro: useful / wrong / missed | Credits: useful / late / wrong / missed |
|---|---|---|---|
| Plex's own markers | | 8 / 14 / 10 | 13 / 3 / 7 / 9 |
| Default sources, High | online answers only | 0 / 3 / 29 | 0 / 0 / 0 / 32 |
| Default sources, High | + Plex + audio, G3 on | **11 / 10 / 11** | **0 / 0 / 4 / 28** |
| Default sources, High | + Plex + audio, G3 off | 11 / 11 / 10 | 0 / 0 / 4 / 28 |
| TheIntroDB on, High | online answers only | 1 / 1 / 30 | 0 / 0 / 0 / 32 |
| TheIntroDB on, High | + Plex + audio, G3 on | 11 / 6 / 15 | 6 / 0 / 4 / 22 |
| TheIntroDB on, High | + Plex + audio, G3 off | 11 / 7 / 14 | 6 / 0 / 4 / 22 |
| TheIntroDB on, Medium | online answers only | 1 / 1 / 30 | 0 / 0 / 0 / 32 |
| TheIntroDB on, Medium | + Plex + audio, G3 on | 11 / 6 / 15 | 6 / 0 / 4 / 22 |
| TheIntroDB on, Medium | + Plex + audio, G3 off | 11 / 7 / 14 | 6 / 0 / 4 / 22 |

All 43 cases, for comparison with the phase-1 audit. The online-only rows match its "after" output, except for credits
at TheIntroDB on, Medium: that row changed with scale fix S1, since SkipDB alone no longer decides credits.

| Setting | Evidence | Intro | Credits |
|---|---|---|---|
| Plex's own markers | | 19 / 14 / 10 | 24 / 3 / 7 / 9 |
| Default sources, High | online answers only | 10 / 3 / 30 | 1 / 1 / 0 / 41 |
| Default sources, High | + Plex + audio, G3 on | 22 / 10 / 11 | 8 / 2 / 4 / 29 |
| TheIntroDB on, High | online answers only | 11 / 1 / 31 | 1 / 1 / 0 / 41 |
| TheIntroDB on, High | + Plex + audio, G3 on | 22 / 6 / 15 | 14 / 1 / 4 / 24 |
| TheIntroDB on, Medium | online answers only | 11 / 1 / 31 | 1 / 1 / 0 / 41 |
| TheIntroDB on, Medium | + Plex + audio, G3 on | 22 / 6 / 15 | 14 / 1 / 4 / 24 |

**Intros.** With Plex's markers and season audio, ours beats Plex under the shipped rules: 11 useful and 10 wrong,
against Plex's 8 useful and 14 wrong. G3 off changes one case, Outlander S08E05, which goes from missed to wrong.

The 10 wrong answers:

- **South Park S01 (6).** Ours starts at 0–1.6 s against chapters that start at 8–18 s, and ends within 5 s of the
  chapter end. Plex has the same shape on 2 of these and starts 44–75 s too late on the other 4.
- **Outlander S08 (3).** The title card runs about 11 s past the chapter.
- **Daredevil S03E12 (1).** The same title card case.

**Credits.** Ours publishes fewer wrong credits than Plex, but also fewer useful ones:

- With default sources, ours has 0 useful against Plex's 13 on the 32 cases. With TheIntroDB on, it has 6.
- The 4 wrong answers are all Daredevil S03, where SkipDB and Plex's own marker agree on a start 13–16 s before the
  chapter. A frame check of all 4 episodes (2026-09-15) shows SkipDB and Plex are right: their start is the first
  producer card right after the story fades, and the studio "credits" chapter lands about 14 s into the same crawl
  in every episode. These 4 are errors in the chapter truth, not wrong answers.

### Credits: chapter rules, rule 7 and Plex's own markers

The truth of these sets is the last credits chapter, and 3 movies have truth fixed by frame checks. So the chapters
row is close to the truth by construction. Rows:

- **Plex:** Plex's first credits marker (the first skip it offers).
- **Chapters:** the chapters decided alone.
- **Ours:** chapters plus Plex's markers. Rule 7 then moves our start later to Plex's own start.

G3 doesn't apply to credits. Medium equals High on every row.

| Set | Row | Useful / late / wrong / missed |
|---|---|---|
| 80 hand-checked files: last credits chapter (`chapter_rules`) | | 78 / 1 / 1 / 0 |
| 40 movies (Plex has credits on 38; 7 have several credits markers) | Plex | 28 / 1 / 9 / 2 |
| | Chapters | 38 / 1 / 1 / 0 |
| | **Ours, High** | **37 / 3 / 0 / 0** (rule 7 moved 4) |
| 40 TV episodes (Plex has credits on 23) | Plex | 19 / 0 / 4 / 17 |
| | Chapters | 40 / 0 / 0 / 0 |
| | **Ours, High** | **40 / 0 / 0 / 0** (rule 7 moved 0) |
| 205 movies (Plex has credits on 197; 33 have several) | Plex | 124 / 11 / 62 / 8 |
| | Chapters | 201 / 1 / 1 / 2 |
| | **Ours, High** | **187 / 15 / 0 / 3** (rule 7 moved 24) |

**How early Plex's wrong starts are** (205 movies): 12 are under 30 s early, 6 under 60 s, 16 under 120 s, 21 under
300 s, and 7 are 300 s or more. Judged by its last credits marker instead, Plex scores 120 / 28 / 49 / 8.

**What rule 7 does on the 205 movies.** It moved 24 starts:

- It fixed 1 wrong chapter (Innerspace).
- It made 14 correct chapter answers late, by 30–507 s. The largest: Poolhall Junkies +507 s, Rocky Aur Rani Kii
  Prem Kahaani +340 s, Road Diary: Bruce Springsteen and The E Street Band +265 s, Attention Attention +169 s, Den of
  Thieves 2 +131 s. Four of the 14 are late by less than 37 s.
- It moved the other 9 within the useful window.

**Tom Clancy's Without Remorse** goes to Needs review. Its first "End Credits" chapter and Plex's marker agree
against the last chapter.

**Title coverage** (205 movies): the classifier sees a credits title in 205 of 205.

**Ledger L165.** The 80- and 205-file sets hold no "Ending" titles and no anime. The lab scale run's chapters (787
files) do:

- **"Ending" titles not counted as credits:** 16 of them ("Ending", or "ENDING" in one show), in Chainsaw Man S01,
  Food Wars! S01, JUJUTSU KAISEN S02 and SPY x FAMILY S01. "ED" is counted as credits: SPY x FAMILY S01, Re:ZERO S02,
  Mushoku Tensei S01 and Chainsaw Man S01.
- **Files with two credits chapters:** Mr. Robot S04 (1 file) and Tom Clancy's Without Remorse (the rules pick the
  last one).
