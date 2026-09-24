# Fix 3 (held): a season on every disk of the library

**Held, not merged** (spec §14 2026-09-25 "Held: a season on every disk"). It comes back as its own lane once the
matcher handles seasons with two openings and start-of-file bumpers (see "Before it comes back").

Patch: `fix3_season_across_disks.patch` (14 files, in this folder). It applies with `git apply` to worktree
`agent-ab7b211236d423de3` in its held state (speed work, fixes 1 and 2, and the round-2 review fixes; rebased
2026-09-25). A fresh copy with the patch applied passes 708 tests (season, season guards, season folders, inspect,
triggers, season follow-ups, missing), `ruff check`, `ruff format --check` and llms-full `--check`. It also passes
`git apply --check` on the integrated tree of 2026-09-25 (all five lanes); its tests weren't run there.

## What it does

- `season.season_folders` takes the season folder relative to the deepest library folder that holds the file. It
  then adds that same folder under every other folder of each enabled server's library holding the file, where it
  exists.
- Paths that are left out:
  - library folders nested inside, or around, the one holding the file;
  - `/`;
  - a folder that is the same directory as one already listed (a linked disk);
  - a path not in normal form (`..`, `.`, doubled or trailing slashes), which keeps its own folder only.
- `season.season_videos` feeds the season group, `_SeasonView`, `detect_season_audio`, `season_audio_answer_outdated`,
  the Season view (`inspect.season_payload`) and Season Publish (`triggers.submit_season_publish`).
- `_request_redecide` asks again for the group's episodes, not the file's own folder.
- The Season follow-up job is named by show and season, so one season on two disks doesn't read "2 seasons".
- `missing.library_folders` is the library-folder walk, which `disk_roots` now also uses.
- Tests: `tests/markers/audio/test_season_folders.py` (new), plus cases in test_inspect, test_triggers and
  test_season_followups.
- Docs: spec §5.3 (group), a §14 entry, guides.md, reference.md, llms-full.

## Why it matters

The TV library is pooled over `/data_16tb`, `/data_16tb2` and `/data_16tb3`, plus `/data_28tb`. 8,069 of 15,478
season folders hold episodes on two or three disks (88,966 files; 5,340 folders span three disks, 2,747 span two). So
today's season groups depend on where the pool put each file. (The investigation's "22 folders" came from markers.db
only.)

## Measured

Season step only, useful/wrong/missed. Whole folders, fix 1 on, speed clock on. Every member was fingerprinted into
the eval cache, so both groupings use the same fingerprints. Own folder → every disk:

| Set | Own folder | Every disk |
|---|---|---|
| Accused (one disk) | 3 / 0 / 53 | 3 / 0 / 53 |
| lab 118 | 91 / 10 / 17 | 90 / 8 / 20 |
| held-out 175 | 124 / 4 / 47 | 121 / 5 / 49 (Plex: 69 / 4 / 102) |
| library chapter set | 106 / 58 / 60 | 113 / 54 / 57 |
| **All 573** | | **+3 useful, −5 wrong, +2 missed** |

Held-out wrong reaches 5 against Plex's 4, which breaks the owner's bar.

## Changed cases

- lab 118:
  - Outlander S08E02 and S08E08: wrong → missed.
  - The Sinner S03E07: missed → useful.
  - **The WONDERfools E02 and E04: useful → missed.** With 8 files the theme is found by 2 of 7 others, below
    the quorum.
- held-out 175:
  - Alias S02 (E02, E04, E07, E11, E18): missed → useful. The group grows from 11 to 22.
  - Bluey S01E02 and E18: missed → useful.
  - **SPY x FAMILY S01: 10 useful → missed** (E02–E07, E09–E12). The 25 episodes hold two openings; 10 of 24
    others is under the quorum. With the folder's 20 files it was 10 of 19.
  - **Dateline NBC 2025-07-11: missed → wrong** (96.6–105.3 s against 97.5–111.1 s).
- chapter set:
  - Sex and the City S04 (E05, E09, E13, E15): wrong → useful.
  - South Park S12E04 and E06: missed → useful.
  - Glass Heart S01E03: missed → useful.
  - **Family Guy S14E03: missed → wrong** (0.0–29.5 s against 0.0–15.2 s).
  - Family Guy S14E07 and E17: wrong → missed.
  - **Turning Point 9/11 S01E02 and E04: missed → wrong.**
  - RuPaul's Drag Race S12E11: wrong → missed.
- Production replay (279 answers in the prod snapshot; 277 replayable):
  - Lioness S02E08, alone on its disk: none → 63.3–122.7 s. All 7 others agree, as does IntroDB.
  - **Star Trek: Strange New Worlds S04E10: title sequence 562.9–669.8 s → 0.0–27.6 s.** That stretch is a
    "Star Trek 60" bumper at the start of every S04 episode (checked on frames). 9 of 9 others share it, and
    matching ranks support before length.
  - Across all of SNW S04 (10 episodes over 3 disks), every episode takes the bumper once the season is whole,
    except E08. E01, E03, E04, E05 and E06 already take it from their own folders.
  - Unchanged within 0.6 s, with more support: Lioness E01–E06, NOVA S53E09/E10, From Old Country Bumpkin
    S02E12, Deadly Influence S01E03.

## Before it comes back

It needs matcher changes:

- For a season with two openings: a quorum over the episodes that share an opening, not the whole season.
- For a start-of-file bumper that outranks the title sequence.

Then re-measure the four sets and the prod replay.

## Scripts and logs (this folder)

The scripts ran from the session scratchpad with the lane's worktree on `sys.path`; their paths still point there.
Logs and row files are local-only (they name library files); pickles, frame tiles and the `fix3_with/` snapshot were
not kept (the patch holds the change).

- `t3_sets_merged.py` / `t3_sets_merged.log` — own folder vs every disk on the four sets.
- `t3_prod.py` / `t3_prod_fill.log` — the production replay in three states (base, fix 1, fix 1+3), with missing
  members fingerprinted (FILL=1). `t3_prod_nofill.log` is the same without them. Rows are in
  `t3_prod_rows.json` (local-only).
- `t3_split.py` / `t3_split.log`, `t3_split_stats.py` — the survey of split season folders.
- `t3_sets_split.py` — which truth files sit in split seasons.
- `t3_snw.py`, `t3_snw_frames.py` — SNW S04 per episode, and the frames of the bumper and the title sequence.
- `fix3_make_patch.py` rebuilt the patch from the `fix3_with/` snapshot; `fix3_check_copy.py` applies it to a fresh
  copy of the worktree and runs the tests it touches.
