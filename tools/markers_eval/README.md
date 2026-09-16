# Intro & Credits accuracy harness

Measures the marker detectors on real episodes with known truth (spec §10.2). It runs by hand on `storage`, never in
CI: the truth sets list real library paths and the runs read real media. The unit tests in `tests/markers_eval/` use
synthetic data only.

## Data and resource rules

Every run uses `nice -n 19`, one heavy job at a time on storage. Media files are only read, by ffprobe and ffmpeg.
Fingerprints are cached under `$MARKERS_EVAL_CACHE` (default `~/.cache/markers_eval`), never under `/data*`. The
truth files are local-only (git-ignored) in the main checkout's `docs/design/intro-credits/evidence/`; in a git
worktree, set `MARKERS_EVAL_EVIDENCE` to that folder. Committed summaries hold counts and show names only, never file
paths. Summaries go to `docs/design/intro-credits/evidence/eval/phase2-harness.md`.

## `reproduce`: the season matcher gate

Runs the app's v3 matcher (`markers/audio/matcher.py`) and the pure-Python reference
(`fp3_reference.py`) on the 118-episode intro eval (`evidence/eval/eval_results_v3.json`). The fingerprints come from
the app's own ffmpeg command. It checks these things:

- The port must return exactly what the reference returns for every episode.
- The app's season step (`markers/audio/season.py`: the matcher, the silence guard, and pairs it skips because they
  provably hold no intro) must score at least spec §5.3's 91 / 13 / 14 useful / wrong / missed. "Useful" means the
  end is within 5 s and the start within 15 s of the chapter truth. `matcher_tally` is the matcher alone.
- No skipped pair may hold a run of 120 s or less in the matcher's answer (`skipped_pairs`), and the silence guard may
  not drop an answer that was useful (`silence_dropped`).
- Drift lists the matcher answers that differ from the stored v3 segments by more than two points. It is reported, but
  it doesn't fail the gate.

```bash
cd /home/data/workspace/plex_generate_vid_previews
nice -n 19 /home/data/.venv/bin/python -m tools.markers_eval reproduce --ffmpeg /usr/bin/ffmpeg \
  --json docs/design/intro-credits/evidence/eval/phase2_reproduce.json
```

Exit 0 means the gate passed. `--json` writes the details, which hold file paths, so keep that file local (the
evidence folder ignores `*.json`).

- `--full-folder`: matches every episode file in each season folder (`season_group`, the app's rule), which is what
  the app does. The default matches the eval's own file lists, at most 8 files per season, which is how §5.3 was
  measured. In this mode, drift also counts the answers that the larger group changed.
- `--no-reference`: skips the slow reference.
- `--cache DIR`: overrides the cache folder.

A cold cache fingerprints about 124 files (158 with `--full-folder`), one ffmpeg at a time, at 3–14 s each.

## `report`: our decisions against Plex's own markers, online cases, credits chapter rules

```bash
cd /home/data/workspace/plex_generate_vid_previews
nice -n 19 /home/data/.venv/bin/python -m tools.markers_eval report --ffmpeg /usr/bin/ffmpeg \
  --json docs/design/intro-credits/evidence/eval/phase2_report_lists.json
nice -n 19 /home/data/.venv/bin/python -m tools.markers_eval report --ffmpeg /usr/bin/ffmpeg --full-folder \
  --json docs/design/intro-credits/evidence/eval/phase2_report_full_folder.json
```

It prints a summary with counts and show or movie names only. `--json` writes per-file details, which hold file paths,
so keep that file local. With a warm cache a run takes about a minute. Exit 0 means the shipped rules pass the gate
below and season audio still scores spec §5.3.

**What "at least as good as Plex" is measured on.** On the 118 intro episodes the truth is their intro chapters, so
chapters can't also be a source, and no online answers are recorded for these files. Each episode's decision runs the
real `decide()` on two inputs: season audio (the app's season step) and Plex's own intro marker for the same file,
read as `server_markers`. It runs at High and at Medium. This is a floor for what a real library gets.

Two rulings mean the shipped rules can't publish anything on this set:

- R2: season audio never decides alone.
- G3: season audio and a server's own marker never decide together.

So every decision row is reported twice. **G3 on** is the shipped rule. **G3 off** counts season audio and a server's
own marker as two agreeing independent sources. `decisions.g3_rule` patches this inside one block only; the app's
rule doesn't change. The gate is **Medium beats Plex** (as many useful or more, no more wrong) **and High is no more
wrong than Plex**. It is judged for both G3 settings, and the exit code follows G3 on. Rows:

- `plex`: Plex's first intro marker.
- `audio`: season audio alone. This is what Medium would publish if R2 allowed it.
- `high` and `medium`: our decisions.

`skips_story` counts the wrong answers that end more than 5 s late or start more than 15 s early.

**Online cases** (`online.py`): the 43 verified cases run through the real parsers and `decide()` at three settings.
The sources are the app's own orders: default, TheIntroDB on at High, and TheIntroDB on at Medium. Each setting has
three rows:

- The recorded online answers alone. This reproduces the phase-1 audit.
- The answers plus Plex's markers and season audio for the case's file, with G3 on.
- The same inputs with G3 off.

Season audio here always uses the whole folder. A case's file is the one library file of its show and episode code.
Rick and Morty S01's truth is Plex's own markers (`online/build_cases.py`), so Plex is also compared on the other 32
cases alone. Verdicts follow the audit's rules:

- An intro is wrong when it starts or ends more than 5 s outside the truth.
- Credits are wrong when they start more than 10 s early, and late when they start more than 30 s late.

**Credits** (`credits.py`): the truth of both credits sets is the last credits chapter, and 3 movies have truth
corrected by frame checks. Chapters are therefore close to the truth by construction. What the rows measure is how
Plex's own first credits marker compares, and what rule 7 changes: a server's own later credits start shortens our
decided start.

- The 80 hand-checked files (`movies40.json`, `tv40.json`): `chapter_rules` and `compare_credits_with_plex`.
- The 205-movie set: `compare_credits_with_plex` and `title_coverage`.
- Ledger L165: files whose "Ending" chapter titles aren't counted as credits, and files with two credits chapters. This
  also covers the lab scale run's chapters (`lab/results/scale/truth.json`, when present), the only stored set with
  anime.

Chapters come from ffprobe, cached as JSON next to the fingerprints (`ProbeCache`).

**Plex baseline** (`plex.py`): Plex's markers for the files the report reads come from one of two sources.

- Default: the lab scale run's read-only dump of prod Plex, `lab/results/scale/prod_plex_markers.json` with
  `prod_plex_parts.json` next to it. It covers the whole library.
- A fresh export of the eval's folders, read-only:

```bash
/home/data/.venv/bin/python -m tools.markers_eval plex-sql > "$SCRATCH/plex_baseline.sql"
grep -Eic 'insert|update|delete|create|attach|pragma|vacuum|replace' "$SCRATCH/plex_baseline.sql"   # must print 0
PROD_PLEX_DB="/config/plex/Library/Application Support/Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db"
ssh -o BatchMode=yes plex "nice -n 19 sqlite3 -separator '|' 'file:${PROD_PLEX_DB}?mode=ro'" < "$SCRATCH/plex_baseline.sql" \
  > docs/design/intro-credits/evidence/lab/prod_plex_baseline.txt
```

Pass that file as `--plex-baseline`. The `?mode=ro` URI is what keeps the query read-only, so never drop it. A path
containing `|` would split wrongly; the prod library has none. A fresh export only covers the eval and credits folders,
so the online cases find no file in it. Their Plex and season audio rows then fall back to the online answers alone.
Plex keeps one marker set per item. None of the files the report reads belong to an item with a second version, so
the app's "another cut" rule never drops Plex's markers here.
