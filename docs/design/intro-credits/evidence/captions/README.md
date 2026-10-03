# A captioned story's own captions are not a roll (credit text v9, 2026-09-29)

The 2026-09-28 final audit found *I Live Alone* (a Korean variety show, 48 episodes S2024E527–E576 on sflix) with 47
of 48 credits markers wrong, 29 skipping 10–400 s of story, every one decided by credit text alone. This lane found
why, measured one guard, and checked it on every credits set and every sflix file with a credit text answer. Spec:
§5.4 "Version 9, captioned stories", §14 2026-09-29.

Local-only (gitignored, library paths): every `*.json` here, `logs/`, and the base tree. The scripts ran from one
scratch folder beside the audits' own folders (`final-audit/`, `accuracy-after/`, the v8 lane's `cardread/`), which
is the layout their relative paths expect.

## Cause

The show captions most of its story: big burned-in captions anywhere on the frame, on most shots but not all. On the
tail's keyframes (1 s apart in these WEB-DLs):

- 43–69 % of the story's keyframes carry a text box, under the 80 % at which `text_all_through` refuses a file.
- Wherever three caption boxes land on one lit keyframe, rule J has a credit frame. They come every few seconds, so
  the 24 s join chains them into one run of 30–515 s. It ends on the real credits (a framed name panel over the last
  seconds of the next-episode preview) and starts mid-story, wherever the last gap over 24 s happened to be.
- Between its caption credit frames the run holds lit keyframes without any text -- the story itself -- adding up to
  34–153 s on 28 of the 29 episodes where the answer skips story. A roll after such a story shows text or black.

Not a separating signal, measured on the show's 48 answers against the 324 right answers of the sets: box height
(median 14–28 px against 7–26 px), the share of the run's keyframes that are credit frames (0.04–0.71 against
0.14–1.0: credits over footage read as sparsely), the share of its credit frames in its band (0.09–0.83 against
0.0–1.0), and the story's text share alone (0.43–0.69 against up to 0.50). Each alone overlaps right answers
(`feat2.py`, `table.py`).

## The guard

`rule_j.captions_all_through`: no answer when **40 % or more of the keyframes before the start carry any text** (as
decoded, the share `text_all_through` counts) **and the lit keyframes without any text from the start to the run's
latest credit keyframe add up to more than one join (24 s)** (read without the overlays, so a channel bug boxed on a
story keyframe is no text on it). The tail is then read at 640×360, as any tail without an answer is.

Review 2026-10-03: each such keyframe counts for the footage up to the next one at most at the run's usual (median)
keyframe spacing, so one stray text-free keyframe before a long gap can't refuse a roll by itself. The longest
*contiguous* text-free stretch was tried first and loses 39 of the show's 40 catches (its captions come every few
seconds: longest stretch 1–27 s, only S2024E537 over 24 s). The cap keeps all 40 (keyframes 1.001 s apart at both
readings, so cap = sum), refuses nothing on Killer Cases (30 answers at 320×180) or the 1,539 sflix files still on disk
(1,334 at 320×180; 32 reach the share, the largest 16.5 s by gap length → 8.4 s by spacing, Nai Nai and Wai Po), and
the three 640×360 changes re-run the same (Beavis and Butt-Head S03E03/S03E15 no answer, Legend of the White Dragon
5305–5564 s).

Swept on the stored rows of every answered file of the sets and the show (`feat2.py`; the rule applied to the stored
rows reproduces the detector's refusals file for file, `predict.py`):

- 1,387 answers of the sets and sflix read at 320×180, plus the show's 48. Runs holding more than 24 s of text-free lit
  keyframes follow a story texted on at most 37 % of its keyframes (45 files: credits over closing footage, A Trip to
  Infinity's roll holds 77 s of footage after a story at 4 %; the nearest, 90 Day Fiancé S12E12, 24.02 s at 37 %); the
  show's are at 43–69 %. Every share from 0.37 to 0.43 gives the same answers: the margin is thin on both sides, and
  90 Day Fiancé S12E12 sits at the corner (just over a join, just under the share).
- At a share of 0.4, every length from 17 to 27 s gives the same answers (Nai Nai and Wai Po, share 0.42, holds 16.5 s;
  the show's shortest refused run, 27.5 s). The join (24 s) is the rule's own measure.
- The 640×360 reading runs the same guard; its answers can't be predicted from stored rows, so the work tree was run
  on every file (below).

## Results

Base `dev` 3b3c023 (credit text v8) against this branch, on the same decodes (GPU on storage's P5000). Useful = right
marker, wrong = any wrong marker, harm = more than 5 s early (skips story).

| Set | Base useful / wrong / harm | Work useful / wrong / harm |
|---|---|---|
| *I Live Alone*, 48 (the final audit's census) | 1 / 47 / 29 | **1 / 7 / 1** |
| Killer Cases, 58 files (48 with an answer) | no answer changes | S04E05 and S03E04 still wrong |
| The final audit's other verdicts on today's markers (115) | 107 / 7 / 1 | 107 / 7 / 1 |
| Fresh sample 2026-09-28 (75 judged) | 63 / 10 / 1 | 63 / 10 / 1 |
| 09-27 answer key, credits (88; held out 27) | wrong 12 (held out 4), 2 skip story | the same |
| Plex comparison (81) | wrong 11, 0 skip story | the same |

Credit text alone, start within 5 s of the set's truth (right / early / late / none), every one unchanged: the 80
(movies40 25 / 4 / 10 / 0, tv40 29 / 7 / 3 / 0), the 205 (76 / 38 / 41 / 5), Accused (45 / 0 / 8 / 0), I Survived
(13 / 1 / 0 / 1), the answer key's files and the chapter files (426, 0 moved).

**Every sflix file with a credit text answer** (1,452 present: 474 in the sets, 96 of the two shows, 882 more): 43
answers change. The show's 40 (no answer, nothing found at 640×360 either), and three the 640×360 reading gave,
frame-checked:

- *Beavis and Butt-Head* (2022) S03E03 and S03E15: 265–375 s and 443–510 s on the episode's story (a music-video
  segment; a backyard scene) → no answer. Both already failed the decision's sanity checks, so nothing published moves.
- *Legend of the White Dragon* (2026): 5570 s (a behind-the-scenes montage beside the crawl, 265 s after the crawl
  starts) → 5305–5564 s: the 320×180 answer, on the first card after the dedication, kept once the montage's run is
  refused. Its skip now stops at the montage.

Replayed on the final audit's `markers.db` (the tree's re-run listing, the carry-over, the rule-change keep): 41
credits decisions change, the show's 40 markers off and Legend of the White Dragon moved; no intro decision changes.
Intros can't move: credit text answers only credits. The re-run lists **1,631 files** for credit text on that copy
(every file with a stored answer or a decided type credit text checks), as each credit text version has.

**Left wrong, other shapes:** the show's six answers on the next-episode preview (they skip no story), its S2024E576
(a 23 s run on the studio talk 64 s before the preview, answered with an end), Killer Cases S04E05 (two lower-third frames 20 s apart make a 20 s run
at 38:00, 290 s before the end, answered with an end) and S03E04 (a courtroom sign's eight credit frames 22 s before
the roll, glued to it by the join). None has a captioned story around it, so this guard doesn't reach them.

**Lab**: phase 3's credit rows need the lab servers and this branch's image, so they are left for the owner: rows 2
(credit text alone publishes), 7 (reuse and force: every stored answer is read again), 10 (real movies against the
harness), 16 (a roll to the end) and 17. The lab's two synthetic credit movies keep their answer offline
(`test_rule_j.TestTextAllThrough.test_a_synthetic_roll_after_story_keeps_its_answer`).

## Scripts

| Script | What |
|---|---|
| `targets.py` | The show's and Killer Cases' files, from the final audit's `markers.db` copy |
| `lib_files.py` | Every sflix file with a stored credit text start (1,452 present), less those two shows (`more_files.json`) |
| `ctrun.py`, `run.sh`, `queue_base.sh`, `queue_work.sh`, `queue_final.sh` | One tree's `find_credits` through the harness caches (GPU decode on storage's P5000, the worker's CPU rerun, card reads), niced; the sets reuse the v8 lane's decodes (`--decode-digest 1614b22dd1ed9f23`: no decode changed since) |
| `truth.py` | path → truth from the 09-27 answer key (`items.json`, `checks.json`), the final audit's positional verdicts and the 09-28 fresh sample |
| `feat2.py`, `table.py` | Features of each answered file's run and the story before it, by verdict |
| `predict.py` | The guard applied to a base run's stored rows |
| `ct_eval.py` | Base vs work per set: right (within 5 s of the truth), early, late, none; every changed file |
| `rp/pos_eval.py` | The final audit's and the fresh sample's positional verdicts against two replays |
| `rp/replay_all.sh` | The final replay (base and work), `metrics.py` and `pos_eval.py` on it |
| `sheet_jobs.py` | Contact-sheet jobs (the final audit's `sheets.py`) for every file whose answer changed |
| `listing.py` | How many files a tree's version re-run lists on the audit's `markers.db` copy |

The answer-key replay is `../credits-accuracy/replay.py` and `metrics.py`, run from a copy beside `items.json`,
`split.json` and `checks.json`, with `--db` the final audit's `markers.db` (2026-09-29 01:58) and `--text` each tree's
credit text on every file this lane read.
