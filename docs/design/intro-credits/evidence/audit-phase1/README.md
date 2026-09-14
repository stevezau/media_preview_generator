# Phase 1 milestone audit B (detection): repro scripts

Repro scripts for audit B's findings (`.superpowers/sdd/plan-phase1/audit-B-report.md`) and the fix lane F1.
Each script runs the real pipeline, `MarkerStore` and `decide()` with fake servers, publishers and online clients
(`tests/markers/fakes.py`). Nothing is written under `/data*`: the scripts only `stat`/`ffprobe` real files.

## Run

```bash
cd docs/design/intro-credits/evidence/audit-phase1
/home/data/.venv/bin/python s1_early_stop_skips_chapter_veto.py
```

- `MPG_REPO`: the checkout whose code runs (default: the repo holding this folder). Point it at a
  `git archive <commit>` export to see the behaviour of another commit.
- `MPG_EVIDENCE`: the local-only evidence data (default: `<repo>/docs/design/intro-credits/evidence`). A worktree
  doesn't have the gitignored files, so set it to the main checkout's evidence folder there.

Data the scripts read (local only, on `storage`): `online/online_results.json` and `online/skipdb-dump.json`
(recorded answers, no live calls), `lab/prod_plex_truth.txt` (prod Plex markers). `s2` and `s10` also read the
owner's Demon Slayer S03E05 and Rick and Morty S01 files (ffprobe / stat). No script calls an online API.

## Scripts

| Script | Finding | What it shows |
|---|---|---|
| `s1_early_stop_skips_chapter_veto.py` | HIGH-2 | A generic "Intro" chapter over the cold open; IntroDB and SkipDB agree on the real theme |
| `s2_medium_duration_agnostic.py` | MED-3 | "Medium" with a lone IntroDB / TheIntroDB answer for another cut |
| `s3_server_markers_copied_from_crowd.py` | MED-1 | Jellyfin segments written by the TheIntroDB importer plugin agreeing with IntroDB |
| `s8_budget_restart.py` | LOW-1 | TheIntroDB's daily reserve and "used today" across a restart |
| `s9_post_credits_real_data.py` | HIGH-1 | All of R&M S01 through the real parsers and `decide()` |
| `s10_pipeline_skips_post_credits.py` | HIGH-1 | R&M S01E06-E08 through the pipeline (real files, prod Plex markers) |
| `s12_stale_chapter_rules.py` | MED-4 | Chapters stored by an older rule set, then a normal run |
| `s13_plex_item_wide_markers_other_version.py` | MED-2 | Plex's item-wide intro from the WEB version read for the Blu-ray version |
| `online43_decide.py` | MED-3, LOW-6 (reverted) | The 43 verified online cases through `decide()` at three settings |

`harness.py` is the shared setup. The fix lane adapted a few scripts so they run on both the audited commit and the
fixed code: `s3` sets the importer plugin, `s13` sets the item's two part durations (the WEB cut is modeled 81 s
shorter), `s9` uses the public `read_server_markers`, `s12` marks the old scan as unversioned, `s8` restarts through
`get_limiter`. The old code never asks for the added values, so its output is unchanged.

## Before (14d8777) and after (fix lane F1)

Full output in `out/before/` and `out/after/`.

| Script | 14d8777 | F1 |
|---|---|---|
| s1 | first normal run publishes the cold open 0-95 s from chapters; only a forced run puts it in review | first normal run asks both sources: review (chapters contradicted by IntroDB + SkipDB); later runs agree |
| s2 | Medium publishes IntroDB 24.0-114.1 s / TheIntroDB 24.9-123.2 s (81 s of cold open) | Medium: review for both |
| s3 | decided 24.0-114.1 s (IntroDB + its copy on Jellyfin) | review; the Jellyfin rows are `server_markers_imported` |
| s8 | after restart LOW allowed, remaining unknown, stored `used` restarts at 2 | after restart LOW `budget_exhausted`, remaining 11, stored `used` 81 |
| s9 | 6 of 10 decided episodes publish past Plex's credits end | none do; E02 starts 0.7 s inside a stinger's last second (IntroDB's start, Plex can't set it) |
| s10 | credits E06 1191.0-1288.0, E07 1239.0-1321.0, E08 1249.0-1335.0 | E06 1191.0-1218.0, E07 1239.0-1267.4, E08 1249.0-1286.2 |
| s12 | normal run: ffprobe 0x, intro 0.0-274.7 s (cold open) | ffprobe 1x, intro 274.7-363.9 s (OP) |
| s13 | Blu-ray decided 24.0-114.1 s, written to Jellyfin | review, nothing written |
| online43 default (high) | intros 10 ok / 3 wrong, credits 1 ok / 1 late | unchanged |
| online43 TheIntroDB on, high | intros 11 ok / 1 wrong / 31 review (Daredevil S03E02 review) | unchanged |
| online43 TheIntroDB on, medium | credits 22 ok / 5 early / 2 late, intros 24 ok / 5 wrong | credits 1 ok / 5 early / 2 late, intros 11 ok / 1 wrong |

LOW-6 (the agreed edge taken as the safer agreeing value instead of source order) was tried in this lane and reverted
by the controller: it published Daredevil S03E02 (TheIntroDB on) 238.0-299.6 s against a 287.7 s truth by removing a
conflict, and flipped chapter vetoes. The table above is the code without it.
