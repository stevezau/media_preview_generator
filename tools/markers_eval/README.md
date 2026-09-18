# Intro & Credits accuracy harness

Measures the marker detectors on real episodes with known truth (spec §10.2). It runs by hand on `storage`, never in
CI: the truth sets list real library paths and the runs read real media. The unit tests in `tests/markers_eval/` use
synthetic data only.

## Data and resource rules

Every run uses `nice -n 19`, one heavy job at a time on storage. Media files are only read, by ffprobe and ffmpeg.
Fingerprints are cached under `$MARKERS_EVAL_CACHE` (default `~/.cache/markers_eval`), never under `/data*`. The
truth files are local-only (git-ignored) in the main checkout's `docs/design/intro-credits/evidence/`; in a git
worktree, set `MARKERS_EVAL_EVIDENCE` to that folder. Committed summaries hold counts and show names only, never file
paths. Summaries go to `docs/design/intro-credits/evidence/eval/phase2-harness.md` and, for the credit text rows,
`phase3-harness.md` beside it.

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
- `--no-reference`: skips the slow reference, and with it the gate's first check. Exit 0 then doesn't prove that
  the port returns what the reference returns, and the summary says `"port_vs_reference": "not checked"`. Use it
  while iterating; a gate run for a matcher change keeps the reference.
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

## `credits-text`: on-screen credit text against Plex's own credits markers

Runs the app's own credit text detector (`markers/credits/detector.find_credits`, rule J) over the credits truth sets
and puts what the pipeline would publish with it beside Plex's own credits markers. Results:
`docs/design/intro-credits/evidence/eval/phase3-harness.md`.

The truth is each file's last credits chapter (3 movies corrected by frame checks, `credits/adjudicated.json`), so
**chapters are left out of every row**: these files stand for files without usable chapters. The rows mirror the
pipeline (C7), which reads credit text only for credits the other sources leave undecided, and Plex's markers never
decide alone (rule 7) — so on these sets every file asks credit text, and no chapter-veto row exists.

Rows per set:

- `plex`: Plex's first credits marker.
- `text`: credits text alone, the detector's answer with no decision rules.
- `high` and `medium`: what the pipeline publishes from credits text plus Plex's markers at each level.

Verdicts are `credits.judge_credits`: `wrong` is more than 10 s early (it skips story), `late` is more than 30 s late,
`missed` is no answer, everything else is `useful`. Also reported per set: `text_and_server_only` (High decisions
resting only on credits text and a server's own marker, Q2), `ends_found` (answers with an end, Q3) and
`ends_published` (decisions whose skip stops before the end of the file).

`rule_j_80` is spec §5.4's own metric for credits text alone on the 80 files — within 5 / 10 / 30 s, early or late by
more than 30 s, no answer — with `rule_j_80_by_kind` repeating it per HDR kind (`sdr`, `hdr10`, `dv5`, `dv_other`,
and `unreadable` for a file ffprobe fails on or times out on).

**The gate** (`gate_checks`, the owner's Q4 ruling of 2026-09-16: precision first, never looser than Plex) is judged
per set — the 80 (movies40 + tv40 merged) and the 205 movies — and every check is named in `summary["gate"]`:

- Medium useful ≥ Plex useful
- Medium wrong ≤ 2 % of the set's files, rounded up (80 → 2, 205 → 5)
- Medium wrong ≤ Plex wrong
- High wrong ≤ 1 % of the set's files, rounded up (80 → 1, 205 → 3)
- High wrong ≤ Plex wrong

Exit 0 means every chosen set passed and `rule_j_80` met the spec (59 within 10 s, at most 1 early). A failing check is
never tuned away: the numbers go to the owner as they are measured.

```bash
cd /home/data/workspace/plex_generate_vid_previews
export MEDIA_PREVIEW_TEXTDET_MODEL="$MARKERS_BENCH_DIR/textdet-model/ch_PP-OCRv4_det_infer.onnx"
nice -n 19 /home/data/.venv/bin/python -m tools.markers_eval credits-text --decode gpu --sets 80,205 --online \
  --sheets "$HOME/.cache/markers_eval/sheets-phase3" \
  --json docs/design/intro-credits/evidence/eval/phase3_credits_gpu.json
nice -n 19 /home/data/.venv/bin/python -m tools.markers_eval credits-text --decode cpu --sets 80 \
  --json docs/design/intro-credits/evidence/eval/phase3_credits_cpu.json
```

The GPU run reads 285 files plus the online cases' 43 and takes about an hour on storage; the CPU run on the 80 files
takes about 25 minutes. Both are the reported runs: `--decode gpu` is the product path (NVIDIA decode, text detection
through the helper pool), `--decode cpu` proves the CPU worker path gives the same gate. Answers are cached under
`$MARKERS_EVAL_CACHE/credits_text` per file identity, detector version, decode path and kind, and a digest of the
detector's source (`credits_text.DETECTOR_SOURCES`: `markers/credits/*.py`, the probe and the decode arguments,
reported as `detector_digest`). A re-run of unchanged code is free; any change to that code measures every file
again, even when `CREDITS_TEXT_VERSION` stays the same.

- `--online`: also the 43 verified online cases at the app's three source settings, with credit text added the way the
  pipeline adds it — only to cases whose credits the online answers and Plex's markers leave undecided. The count is
  reported as `credits_text_asked`.
- `--sheets DIR`: a 4×2 contact sheet of the 80 s around every credits text answer worth a look (more than 10 s early,
  more than 30 s late, shaped like epilogue cards, or with an end, which gets a second sheet around the end) for Q5's
  adjudication. The summary always lists which files and why, sheets or not. The sheets hold real frames: keep the
  folder local, never under `/data*`.
- `--decode`, `--gpu-device`, `--sets`, `--ffmpeg`, `--cache`, `--plex-baseline`, `--json` work as in `report`.

## Text detection bench

The vendored detector (`markers/credits/textdet.py`) must find the same boxes as `rapidocr_onnxruntime` 1.4.4 on all
289 bench frames, counts and corners:

```bash
python -m tools.markers_eval.textdet_bench extract
python -m tools.markers_eval.textdet_bench counts --impl rapidocr --model M --out rapidocr.json
python -m tools.markers_eval.textdet_bench counts --impl vendored --model M --out vendored.json
python -m tools.markers_eval.textdet_bench compare rapidocr.json vendored.json
```

`compare` exits 0 only when both runs cover all 289 frames and no frame differs. The frames come from every 10th file
of `credits/f3.jsonl` (8 files), CPU keyframes of the 120 s around each credits truth at 320×180 grey, and live in
`$MARKERS_BENCH_DIR` (default: the git-ignored `evidence/credits/bench/`), never committed. `--impl rapidocr` needs
Python ≤ 3.12: run it in `$MARKERS_BENCH_DIR/py312` with this repo on `PYTHONPATH`.

## Rule J fixture

```bash
python -m tools.markers_eval.credits_fixture
```

Rebuilds `tests/fixtures/markers/credits_rule_j_80.json.gz` from the local-only evidence, so rule J's 80-file
regression runs in CI without any media. It anonymises: files become `movie-01…`/`tv-01…` in the evidence order, every
time is shifted by a whole number of seconds so the item's tail window starts at 1000 s, rows keep only
`[pts, boxes, luma]`, and the frame-check truth (`credits/adjudicated.json`) replaces the chapter truth. Each item is
checked against the prototype (`credits/eval_rules3.py`) before it is written; the seven items in
`ANCHOR_DIVERGENCES`, where the port's anchor deliberately differs, are reported instead of stopping the build.
