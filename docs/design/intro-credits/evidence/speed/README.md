# Two playback speeds (Bones S05–S08) — evidence

Spec §5.3 "Two playback speeds", §5.5 rule 12, §14 2026-09-24/25. A 25 fps release of a film-rate show plays 4.3 %
fast: season audio matched 0 of 85 WEB↔Blu-ray pairs of Bones S05, and IntroDB's film-rate times ran 4.3 % late on
the 25 fps files (S07E01: theme 310–338 s, IntroDB 324–354 s). Fixed by matching a mixed season at one speed
(`asetrate` retimed fingerprints: 85 of 85 cross pairs) and reading online times on the file's own clock.

These scripts ran from the session scratchpad with a lane worktree on `sys.path`; their `HERE`/`WORKTREE` constants
still point there. JSON results, logs and text outputs are local-only (gitignored: they name library files).

## Headline numbers (useful / wrong / missed)

| Set | Plex's own | Before | After |
|---|---|---|---|
| Bones S05–S08 (82, frame-checked truth) | 41 / 41 / 0 | 38 / 0 / 44 | **82 / 0 / 0** |
| lab 118 (season audio + IntroDB + Plex) | 23 / 15 / 80 | 86 / 5 / 27 | 86 / 5 / 27 |
| held-out 175 (same) | 69 / 4 / 102 | 121 / 5 / 49 | 122 / 5 / 48 |
| Accused (same) | 0 / 0 / 56 | 2 / 0 / 54 | 3 / 0 / 53 (with intro guards) |

With Plex's markers counted as this file's (this lane alone, before the "made for an earlier file" lane): Bones
**41 / 41 / 0**. With them flagged stale by that lane's rule (`../stale-plex-markers/`): **82 / 0 / 0**.

## Files

| File | What |
|---|---|
| `bones_truth.py` → `truth.json` | Frame-checked truth: the "BONES" logo card − 4 s to the "created by Hart Hanson" card, by correlation (`ncc_*.npy`, not kept) |
| `probe_fps.py`, `fps.json` | Each episode's frame rate |
| `introdb_fetch.py` → `introdb.json`; `introdb_sets.py` → `introdb_sets.json` | IntroDB answers for Bones and for the three regression sets (the app's client, anonymous) |
| `imdb_ids.py`, `list_shows.py` → `shows.json`, `shows_imdb.json` | imdb ids from Sonarr (read-only; the key is read at run time, never printed) |
| `bones_eval.py` (`bones_eval*.log`), `score_bones.py` (`local/score.txt`) | The season step on Bones, before and after |
| `bones_decide_r2.py`, `run_r2.py` (`run_r2.log`), `answers*.json` | Decide-level Bones sets from the stored answers (audio / IntroDB / Plex) |
| `decide_sets.py`, `run_decide_sets*.py` (`decide_sets*.log`) → `before_*/after_*/r2_*/t3_*.json` | Decide-level regression: lab 118, held-out 175, Accused with real frame rates |
| `regress.py`, `regress_t3.py` (`regress*.log`) | Season-audio harness sets |
| `fp_exp.py`, `mixed_groups.py`, `lib_mixed.py`, `chap_mixed.py`, `other_mixed.py` | Retiming experiments; which library seasons mix speeds (30 for 30 S04, Sort Of S03 besides Bones) |
| `plex_bones.sql`, `plex_bones_when.sql` (`local/plex_bones.txt`) | Plex's own Bones markers and their dates (read-only) |
| `food_wars.py`, `no_audio_cell.py`, `online_coverage.py` | Single-case checks (Food Wars S01E05; a file with no audio answer) |
| `*_impl*.py`, `pipeline_tests.py`, `spec*.py`, `insert.py`, `replace_tail.py`, `sheet.py`, `redecide.py` | Edit scripts the lane applied to its worktree, and one-off helpers |
| `integration-proof/` | The same proofs on the integrated tree (all five lanes): Bones 82 / 0 / 0 with Plex's markers flagged by the stale rule; lab 118 86 / 5 / 27, held-out 175 123 / 5 / 47, Accused 3 / 0 / 53 before the intro-end rule (`../intro-end/`) |
