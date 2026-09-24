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
- The app's season step (`markers/audio/season.py`: the matcher, its guards against idents and music beds, the silence
  guard, and pairs it skips because they provably hold no intro) must score at least 91 / 12 / 15 useful / wrong /
  missed (spec §5.3, §14 2026-09-24; the matcher alone was 91 / 13 / 14). "Useful" means the end is within 5 s and the
  start within 15 s of the chapter truth. `matcher_tally` is the matcher alone, and `guards_changed` lists the answers
  the guards changed, with both verdicts.
- No skipped pair may hold a run of 120 s or less in the matcher's answer (`skipped_pairs`), and the silence guard may
  not drop an answer that was useful (`silence_dropped`).
- Drift lists the matcher answers that differ from the stored v3 segments by more than two points. It is reported, but
  it doesn't fail the gate.

The guards' end-picture check decodes the real files, like a worker: `--decode gpu` (the default, NVIDIA on
`--gpu-device cuda:0`) or `--decode cpu`. Each share is measured once per run.

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

## `season-truth`: the season step on any intro truth set

Runs the app's season step on each file of a truth file, matched with its season group (`season_group`, as the app
does), with real end-picture decodes. The truth is local-only JSON, `{"<file>": [start_s, end_s]}`, with `null` for a
file that has no intro (an answer there counts as wrong; none counts as `none_ok`). The Accused set of §14 2026-09-24
(57 episodes of "Accused: Guilty or Innocent", truth from frame checks) goes in `evidence/eval/accused_truth.json`:

```bash
cd /home/data/workspace/plex_generate_vid_previews
nice -n 19 /home/data/.venv/bin/python -m tools.markers_eval season-truth --ffmpeg /usr/bin/ffmpeg \
  --truth docs/design/intro-credits/evidence/eval/accused_truth.json --expect 2,0
```

`--expect useful,wrong` makes it exit 1 below that many useful or above that many wrong (Accused: 2 / 0 / 54, as
measured). `--json`, `--cache`, `--decode` and `--gpu-device` work as for `reproduce`.

## `report`: our decisions against Plex's own markers, online cases, credits chapter rules

```bash
cd /home/data/workspace/plex_generate_vid_previews
nice -n 19 /home/data/.venv/bin/python -m tools.markers_eval report --ffmpeg /usr/bin/ffmpeg \
  --json docs/design/intro-credits/evidence/eval/phase2_report_lists.json
nice -n 19 /home/data/.venv/bin/python -m tools.markers_eval report --ffmpeg /usr/bin/ffmpeg --full-folder \
  --json docs/design/intro-credits/evidence/eval/phase2_report_full_folder.json
```

Its season audio answers come from the same season step, end-picture decodes included (`--decode`, `--gpu-device`).

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
  anime. Since phase 4 Task 15 a bare "Ending" **is** credits on a TV episode (spec §5.1), so every row is scored with
  a kind. The three hand-checked sets pass one kind per set, because `is_movie` is also their decision context (a
  movie's credits must start within 900 s of the end): the 40 movies and the 205 movies as movies, the 40 TV files as
  episodes. That is safe only while each set is uniform, and it was checked — `ids_from_path` reads 40 of 40 tv40 rows
  as episodes and 0 of 40 + 0 of 205 movie rows as episodes. The scale run's set holds both, so it is split row by row
  by `ids_from_path` instead. What the ledger still lists is what the rule does not take: "Ending" on a movie, and
  titles like "Ending Theme".

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
`$MARKERS_EVAL_CACHE/credits_text` per file identity, detector version, ffmpeg build (the first line of
`ffmpeg -version`), decode path and GPU device, the text detection backend the helper pool actually used (as for the
decodes below; an answer whose backend changed while its file was read isn't kept), kind, and a digest of the
detector's source (`credits_text.DETECTOR_SOURCES`: `markers/credits/*.py`, the probe and the decode arguments,
reported as `detector_digest`). A re-run of unchanged code is free; any change to that code runs the app's detector
on every file again, even when `CREDITS_TEXT_VERSION` stays the same. A stored answer keeps the boxes rule J read the
run without (`overlays`, from `CreditsTextResult`) beside its rows, because `epilogue_like` has to be handed them
rather than gather them again: on the branch that reads the 120 s before the tail, `key` is the joined rows, and a
roll that began before the tail is exactly the shape that must not be read as its own overlay.

Each stored row is `[pts, box count, luma, [[left, top, right, bottom], ...]]`: the frame's time in seconds from the
start of the file, how many text boxes it holds, its mean luma, and where those boxes are in the frame's own 320×180
pixels (spec §5.4 "What a row holds"). `decode_cache.rows_from_json` reads a stored row back into the tuple
`frames.decode_rows` returns, so a rule can be measured on the boxes without decoding anything:
`DecodeCache(...).serving()` puts the cache behind the app's own `find_credits`, and a row's fourth field is the
positions. Rule J version 3 reads the fourth field in `overlay_boxes`, `same_roll` and `reach_back` (spec §5.4
steps 4, 5 and 7); everything else it does reads the first three.

That re-run doesn't decode again unless it has to. Every decode the detector asks for (the tail's keyframes, each
1 fps window) is kept under `$MARKERS_EVAL_CACHE/credits_decodes` (`decode_cache.DecodeCache`) keyed on the file's
identity, the ffmpeg build (the first line of `ffmpeg -version`), the exact ffmpeg command the app builds (window,
decode path, hwaccel arguments, scaler and keyframe thinning), the text detection backend that actually counted the
boxes, and a digest of the code that turns a command
into rows: the detector's sources bar `rule_j.py` and `detector.py` (`credits_text.RULE_FILES`), reported as
`decode_digest`. The backend is what the helper pool's self-test chose (`webgpu <device>` or `cpu`, reported as
`text_detection`), not what the run asked for: a GPU run whose self-test fell back to the CPU shares a CPU run's rows,
and a GPU run's CPU reruns still count on the GPU, so they never serve a CPU run. Rows are kept only when the backend
was the same before and after the decode. The two per-file probes the detector makes, the container's start and the
keyframe thinning (intra-only stride, VP9's drop of non-key packets), are kept the same way (a probe that timed out or
stalled is not; an ffprobe error on the packet probe reads as "no thinning", so a VP9 or intra-only file's keyframe
pass reads every frame, and that is kept until its entry is deleted). So a change to rule J re-runs the app's own
`find_credits` against stored rows in seconds and decodes only the windows the new rule asks for that no earlier run
did (the summary's `decodes` counts both); a change to the decode code, the ffmpeg build, the text detection or its
model pin decodes everything again. A GPU decode that failed is kept as that failure, so the CPU rerun doesn't retry the GPU each time;
a one-off failure stays until its entry is deleted. Entries are written whole or not at all.

- `--changed-since OLD.json`: an earlier run's `--json` file. Every set row whose credits text start or end moved by
  more than 10 s, or gained or lost one, is listed in `changed` (names, both answers against the truth or the file's
  end, and what High and Medium now publish). With `--sheets`, only those rows get sheets: the rest were looked at
  on the earlier run. This is Q5's frame check of a rule change: every answer it moves is looked at.

- `--online`: also the 43 verified online cases at the app's three source settings, with credit text added the way the
  pipeline adds it — only to cases whose credits the online answers and Plex's markers leave undecided. The count is
  reported as `credits_text_asked`.
- `--sheets DIR`: a 4×2 contact sheet of the 80 s around every credits text answer worth a look (more than 10 s early,
  more than 30 s late, shaped like epilogue cards, or with an end, which gets a second sheet around the end) for Q5's
  adjudication. The summary always lists which files and why, sheets or not. The sheets hold real frames: keep the
  folder local, never under `/data*`. A sheet is named by the file and the second it tiles around
  (`<set>-<hash>-<s>.jpg`, `<set>-<hash>-end-<s>.jpg`), and one that exists isn't written again, so a re-run into
  the same folder adds only sheets for answers that moved.
- `--sweep rule_j.NAME=v1,v2,...` (repeatable): measure one of rule J's own constants at each of those values over
  the chosen sets instead of reporting a run, and print a markdown table carrying the columns
  `phase3-harness.md`'s sweep tables carry — the 80's rule J / Medium useful / wrong and the 205's Medium and
  alone rows — plus what each cell decoded. The published tables are written from it by hand: they name the
  constant in prose and drop the decode count. Several
  `--sweep` arguments are a cross product, which is how the band's two numbers were tabled. Each cell runs the
  **whole rule** through `DecodeCache(...).serving()`, so both halves of version 3 are live in every cell, and the
  answer cache is deliberately bypassed: its key is the detector's *source* digest, which a swept attribute doesn't
  move, so every cell would otherwise be served the shipped cell's answers. A constant a `setattr` wouldn't reach is
  refused before anything is measured (`unreachable_by_patch`): anything that takes a copy of it while a module of
  ours is imported — a default argument, a decorator's argument, or an assignment in any block that runs at import
  (module scope, a class body, an `if`, a `try` body or its handlers, a `with`, a `for`, a `match` case) — in
  `rule_j.py` itself or in another module of ours, whether that module did `from .rule_j import <NAME>` or read it
  as `rule_j.<NAME>`. What it cannot see is a module of ours that nothing has imported yet when the sweep starts.
  That is not hypothetical: a published band sweep
  reached `same_roll` and nothing else, because `in_band` bound `BAND_TOLERANCE_PX` as a default argument and so
  read the shipped value there (`phase3-harness.md`, "What the architecture review changed"). `--online`, `--sheets`
  and `--changed-since` are refused with it rather than ignored: a sweep has many answer sets, not the one these
  three read.

  ```bash
  nice -n 19 /home/data/.venv/bin/python -m tools.markers_eval credits-text --decode gpu --sets 80,205 \
    --sweep rule_j.BAND_TOLERANCE_PX=16,24,32,40,48
  ```

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
checked against the prototype (`credits/eval_rules3.py`) before it is written; the nine items in
`PORT_DIVERGENCES`, where the port deliberately differs, are reported instead of stopping the build.

These rows carry **no box positions**: the prototype that measured them recorded how many boxes a frame held and never
where they were, and re-measuring the files would replace the rows the port is pinned against. Rule J reads no
positions, so the fixture still pins every one of its answers. A rule that reads positions is measured on the decode
cache above, or on the lab fixture below.

## Lab fixture (with box positions)

```bash
MEDIA_PREVIEW_TEXTDET_MODEL=... python -m tools.markers_eval.credits_synth_fixture [--decode gpu|cpu]
```

Rebuilds `tests/fixtures/markers/credits_synth_lab.json.gz` from the lab's synthetic files
(`evidence/lab/synth`, git-ignored) through the app's own `find_credits`, with `rule_j.text_all_through` and
`rule_j.overlay_boxes` patched off — the view rule J version 1 had when the fixture's `version_1_start_s` was
measured. Every file is decoded again and
checked against the fixture it replaces: each row's time, box count and mean luma must match row for row, or the build
stops. Only the positions are new, and they come from the same text detection call the counts came from.
