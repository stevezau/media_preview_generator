# Intro & Credits — Implementation Roadmap (all phases)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement the phase plans task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Detect Skip Intro / Skip Credits markers once per media file and publish them to every Plex, Jellyfin and
Emby server that has the file — at least as accurate as Plex's own detection on the owner's library.

**Architecture:** A new `media_preview_generator/markers/` package (store, sources, decision rules, detectors,
publishers, pipeline) plugs into the existing job engine as a second job kind (`intro_credits`) that shares the
GPU/CPU worker pool, priorities, gate, cancel and job UI with previews. Servers are projections of one SQLite
store (`markers.db`). Plex is written through its database; Jellyfin and Emby through our Bridge plugins.

**Tech Stack:** Python 3.12 (image) / 3.14 (dev venv), Flask, SQLite, requests, FFmpeg/ffprobe (+ chromaprint in
jellyfin-ffmpeg), numpy, ONNX Runtime (CPU + WebGPU plugin EP), OpenCV headless, C# (.NET 9/10 Jellyfin plugin,
netstandard2.0 Emby plugin), Playwright for UI and skip-button checks.

**Spec:** `docs/design/intro-credits/spec.md` (Revision 3). Evidence map: `docs/design/intro-credits/evidence/README.md`.
Executors read the spec section a task cites before starting it.

## Global Constraints

Every task in every phase implicitly includes these. Values are copied from the spec.

- Feature is **off until turned on per server** (`media_servers[].markers.enabled`, default `false`). Nothing is
  detected for a file with no enabled owner; nothing is written to a server whose switch is off.
- **Precision over coverage:** a missing marker is acceptable; a wrong one is not.
- Default publish rule `publish_when = "high"`: chapters (unless two agreeing independent sources contradict them), or
  two independent sources agree (intro/recap **end** within **5 s**; credits/preview **start** within **10 s**). Only
  agreeing candidates supply times; the unchecked edge takes the safer value (latest intro start, earliest credits end).
  Conflicting groups of agreeing sources, or any agreeing pair from different sources outside tolerance of the result,
  → needs review. `"medium"` also accepts one source after sanity checks when no other independent source (server
  markers included) contradicts it and every pair of its own candidates agrees. Results never depend on input order.
- Sanity bounds (chapters too): inside the file (end ≤ duration + 2 s, clamped); length ≥ **3 s** (intro/recap
  ≤ **300 s**); intro/recap start in the first **35%**, never running to the end of the file; credits/preview start in
  the last **25%** (movie credits also ≤ **900 s** from the end); unknown duration fails.
- IntroDB + TheIntroDB always count as **one** source. Markers already on a server count as agreement evidence,
  **never** as a sole source and never supply times. A **locked** user marker always wins. Intro/recap overlap > 5 s
  and preview/credits overlap > 10 s → needs review.
- File identity = canonical path + size + mtime. A change invalidates fingerprints, evidence and unlocked markers.
- All marker times are integer **milliseconds** internally. Jellyfin/Emby ticks = ms × 10,000.
- Plex: write both `taggings` (on Plex's existing `tags` row with `tag_type=12 AND tag=''`) **and**
  `media_parts.extra_data`; never write or create `tags` rows; credits start written as served − **2000 ms**;
  non-final credits end written as served + 2000 ms; unknown schema → stop writing; DB on a network filesystem →
  read-only with a message; first enable per Plex server needs the database-write confirmation
  (`markers.plex.db_write_confirmed_at`).
- Resource rules: marker items run on the **existing** WorkerPool (no extra workers); ffmpeg `-threads 2`;
  fingerprints ≤ **2** in parallel; ONNX Runtime `intra_op_num_threads=2`; online lookups paced from response
  headers; webhook-triggered Intro & Credits jobs submit at **NORMAL** (previews are HIGH); backfill and schedules at
  **LOW**. Never parallel per-frame seeks.
- TheIntroDB API key is a secret: masked as `****` in every API response, never logged, round-trips unchanged when
  `****` is posted back. Everything must work with any online source disabled or failing.
- Media paths are read-only. Never write under `/data*` on either host. Prove server behaviour on the **lab servers
  on storage** only; the prod Plex DB is read-only (`sqlite3 "file:<db>?mode=ro"`).
- Tests follow `.claude/rules/testing.md`: mock Plex/FFmpeg/HTTP/filesystem in unit tests, assert the kwargs/SQL the
  code controls (not call counts), cover every cell of branchy matrices. Integration tests that need real ffmpeg or
  real servers are marked `integration`/`gpu` and never run in default CI.
- Code style: ruff (line 120), type hints everywhere, Google docstrings on public APIs, `from loguru import logger`,
  comments explain *why* only.
- UI: wording and layout from the owner-approved design artifact (`evidence/design/index.html`); every non-obvious
  control gets an ⓘ tooltip (`.info-icon` pattern, `_initBootstrapTooltips`). Any deviation from the mockup → ask
  the owner first.
- Git: branch `feat/markers-detection` (PR #241 → `dev`). Conventional Commits. Before every commit run the
  `Architecture Review` agent on the staged diff; HIGH blocks. Commit with
  `PATH="/home/data/.venv/bin:$PATH" git commit`. Push after each task. Never commit to `dev`/`main`, never merge,
  never release. Merge `origin/dev` into the branch whenever GitHub says the PR is out-of-date.
- Test commands (storage): `/home/data/.venv/bin/python -m pytest --no-cov <files>` for a task;
  `/home/data/.venv/bin/python -m pytest` (full, ~40 s) before each push; `pytest -m e2e -n 8 --no-cov` for UI tasks.

## File map (whole feature)

```
media_preview_generator/
  job_kinds.py                     # P1  kind constants, ItemOutcome, KindHandlers (no package imports)
  markers/
    __init__.py
    settings.py                    # P1  defaults, validation, typed accessors, sports-library rule
    models.py                      # P1  MarkerType, Source, Candidate, Marker, FileIdentity, MediaIds
    decide.py                      # P1  §5.5 decision rules + sanity checks
    store.py                       # P1  markers.db (files, evidence, markers, decisions, publish_state, …)
    probe.py                       # P1  ffprobe duration + chapters
    external_ids.py                # P1  ids from path + merge with server metadata
    outcomes.py                    # P1  per-file outcome keys + per-server row statuses
    pipeline.py                    # P1  check_item / process_item: owners → identity → evidence → decide → publish
    job_runner.py                  # P1  Intro & Credits job thread (gate, logs, enumeration, dispatcher submit)
    triggers.py                    # P1  webhook follow-up jobs, schedule job creation
    fs.py                          # P1  network-filesystem detection from /proc/self/mountinfo
    sources/
      chapters.py                  # P1
      ratelimit.py                 # P1  header pacing, 429 backoff, circuit breaker, daily budget reserve
      theintrodb.py introdb.py skipdb.py   # P1
      server_markers.py            # P1  markers already on servers (evidence only)
    publishers/
      base.py                      # P1  MarkerPublisher, Capability, CapabilityReport, PublishError
      plex_db.py                   # P1  Plex database writer/reader
      jellyfin.py                  # P1  Bridge plugin publisher
      emby.py                      # P2  Emby Bridge plugin publisher
    audio/
      fingerprint.py               # P2  chromaprint via jellyfin-ffmpeg, cached in store
      matcher.py                   # P2  v3 season matcher (vectorised port of evidence/detect/fp3.py)
      season.py                    # P2  season step: siblings, previous season, re-decide
    credits/
      frames.py                    # P3  keyframe tail decode (worker GPU hwaccel) + 1 fps refine
      textdet.py                   # P3  vendored PP-OCRv4 det pre/post-processing on ONNX Runtime
      textdet_helper.py            # P3  per-device helper subprocess, self-test, CPU fallback
      rule_j.py                    # P3  credits rule J
    reconcile.py                   # P2  periodic read-back + re-publish
  web/routes/api_markers.py        # P1  status, usage, inspector data; P2 season; P4 edit/lock/redetect
jellyfin-plugin/                   # P1  Markers endpoint + IMediaSegmentProvider, 10.11 + 12.0 builds
emby-plugin/                       # P2  Media Preview Bridge for Emby (4.9 + 4.10 builds)
tools/markers_eval/                # P2  accuracy harness (intros, credits, online, Plex baseline) — not CI
tests/markers/                     # P1+ unit tests;  tests/e2e/test_intro_credits_*.py  UI journeys
```

## Phases

Each phase ends usable, tested in the lab, and owner-reviewed. A detailed plan is written at the start of each phase
(`plan-phase<N>.md`) against the code as it then exists.

### Phase 1 — Store, chapters + online sources, Plex & Jellyfin, job kind, UI (plan: `plan-phase1.md`)
Done when: chapter- and online-covered files get correct markers on lab Plex (claimed) and both lab Jellyfins,
survive the spec §3 wipe matrix, the full pytest + e2e suites pass, the PR image builds, and a lab scale run on real
library folders mounted read-only shows no failures (owner, 2026-09-14: no side-by-side on `plex`; the storage lab is
the full test bed).

**Status 2026-09-15: done.** Lab matrix 18/19 pass (row 11 unit-tested), PR image `pr-241` checked on the lab, scale
run on 715 real files (0 failed; High: intros 253 right / 2 wrong of 262, credits 467 right / 2 early of 502; far more
right than prod Plex's own markers on the same files). Of the scale run's findings, F2, F3 and F4 are fixed, F1 moved into
phase 2 Task 7, and F5 (a late credits start on the end card after a post-credits scene) is safe as is: it skips
less, never story. Evidence: `evidence/lab/phase1-results.md`.

### Phase 2 — Season audio intros, Emby, reconcile, Season view, eval harness (plan: `plan-phase2.md`)
- [x] `audio/fingerprint.py`: `ffmpeg -ss 0 -t W -i <file> -vn -ac 2 -f chromaprint -algorithm 1 -fp_format raw -`,
  `W = min(900 s, 35% of duration)`, 0.1238 s/point, uses `/usr/lib/jellyfin-ffmpeg/ffmpeg` (capability-probe
  `-muxers` for chromaprint; arm64 image lacks it → source unavailable with a message). Cached per identity.
- [x] `audio/matcher.py`: v3 (inverted index ±2, `popcount(a^b) ≤ 6`, gaps ≤ 3.5 s, runs 8–120 s, all non-overlapping
  runs, cluster ±4 s, rank (len ≥ 15 s, support, len), quorum ≥ 50% of others, ≥ 1 when one other). Pair results
  cached in `season_pairs`. Must reproduce `evidence/eval/eval_results_v3.json` segment-for-segment on cached
  fingerprints before use.
- [x] `audio/season.py`: group = season folder (shipped as the folder's episodes with the same season number, at most
  the 40 nearest); one sibling is enough; first episode uses ≤ 4 episodes of the previous season as a hint that needs
  a second source.
- *(superseded)* `audio/season.py`: re-decide siblings without an intro when a new episode arrives — superseded, not built as
  written: spec §14 2026-09-14 R3 and 2026-09-15 season step (a job matches inline and queues a Season follow-up job
  for changed episodes outside it).
- [x] Emby plugin (`emby-plugin/`, `MediaBrowser.Server.Core` NuGet refs): `POST/GET/DELETE
  /MediaPreviewBridge/Markers/{id}`, `GET /MediaPreviewBridge/Ping` with `features`, `SaveChapters` keeping Chapter
  rows, re-apply on `ItemUpdated` from `IServerEntryPoint`. Builds 4.9 + 4.10. Catalog submission text + manual
  install docs. `publishers/emby.py` (IntroStart, IntroEnd, CreditsStart).
- [ ] Emby catalog submission itself — deferred: the text is in `emby-plugin/README.md`, but the entry needs the
  owner's Emby forum thread / developer id (owner checkpoint 4).
- [x] `reconcile.py`: read back each published server, re-publish on drift — shipped as the Check servers job, which
  the user starts or schedules (spec §14 2026-09-14 R5).
- *(superseded)* `reconcile.py`: APScheduler job every 12 h — superseded: spec §14 2026-09-14 (R5, nothing scheduled by default).
- *(superseded)* `reconcile.py`: reconcile after each Intro & Credits job — superseded: spec §14 2026-09-14 (phase 1's read-back
  before "Up to date" and the delayed verify job cover it).
- *(superseded)* Plex `on_plex_redetect=keep_plex` stores Plex's set as evidence instead — superseded: spec §14 2026-09-14 (Keep
  Plex's keeps Plex's markers on the server, read as `server_markers` evidence like any server's).
- [x] Inspector Season view + `GET /api/markers/season`.
- [x] `tools/markers_eval/`: intros (118 eps), credits (80 files + 205-movie chapter set), online (43 cases), and a
  **Plex baseline** column read-only from the prod Plex DB for the same files. Report useful/wrong/missed per
  source, per publish setting, and for Plex's own markers.
Done when: harness ≥ spec §5.3 numbers (91/13/14 of 118 or better), **our decided markers are at least as good as
Plex's native markers on the same files (more useful, not more wrong)**, Emby skip button in the lab.

**Status 2026-09-15: built, audited and lab-proven; owner review of PR #241 next.** Harness reproduces §5.3 exactly
(91/13/14 eval lists, 91/10/17 full folder). Against Plex's own markers: on the 118 intro episodes the shipped rules
(R2, and G3 kept on by the owner) publish 0 useful / 0 wrong against Plex's 23 / 15, so "Medium beats Plex" fails by
construction there (High passes; G3 off would pass); on the online cases ours has 11 useful / 10 wrong intros against
Plex's 8 / 14, but fewer useful credits there (0 with default sources, 6 with TheIntroDB, against Plex's 13); chapter
credits beat Plex on the 40-movie, 40-episode and 205-movie sets (more useful, 0 wrong each). Emby web
shows Skip Intro in the lab (row 8; skipping needs Emby Premiere on the server). Milestone audit 0 critical/high, 7
medium fixed; lab matrix 23/23; `pr-241` image `sha256:3afed8e7…` from `8a8b92e` re-ran rows 1, 2 and 8. Evidence:
`evidence/lab/phase2-results.md`, `evidence/eval/phase2-harness.md`.

### Phase 3 — Credits text detection (plan: `plan-phase3.md`)
- [x] `credits/frames.py`: `ffmpeg -threads 2 [hwaccel args from the worker's GPU] -skip_frame nokey -ss <tail> -copyts
  -i <file> -an -sn -dn -fps_mode passthrough -vf "<gpu scale to 320x180>,showinfo" -f rawvideo -`; tail 900 s movies,
  450 s TV; 1 fps refine over the 20 s before the coarse answer. Hwaccel args extracted from
  `processing/ffmpeg_runner.py` into a shared helper without changing preview command lines (golden-args test).
- [x] `credits/textdet.py`: PP-OCRv4 det ONNX at `det_limit_side_len=320`, `det_limit_type="max"`; DBNet post-processing
  vendored (OpenCV contours; ~~unclip computed analytically~~ **superseded by T-R2**: pyclipper is kept for the
  unclip step, since an analytic replacement can't match its integer rounding exactly and identical box counts is
  the gate) — must give **identical box counts** to `rapidocr_onnxruntime==1.4.4` on the 289-frame bench set before
  use.
- [x] `credits/textdet_helper.py`: one helper subprocess per GPU device (plus one shared CPU helper), env from
  `get_vulkan_env_overrides()`, WebGPU plugin EP when `get_vulkan_device_info()` is hardware and a 20-frame
  self-test counts exactly the CPU's boxes, faster; else CPU. Crash/hang → CPU for the process lifetime; cancel
  between chunks.
- [x] `credits/rule_j.py`: rule J exactly as spec §5.4 (luma < 30 & boxes ≥ 1, or boxes ≥ 3; runs over gaps ≤ 24 s with
  dark bridging; runs ≥ 15 s; last run; anchor; refine), version 2 after the final review (the anchor's 24 s limit,
  the end's step back over scene text, no answer when text is on screen all through the tail, and reading before the
  tail for a roll it cuts into). Regression test reproduces 64/80 within 10 s, 1 early, from an anonymised copy of
  `evidence/credits/f3.jsonl` committed as a test fixture.
- [x] Docker: numpy, onnxruntime, onnxruntime-ep-webgpu, opencv-python-headless, **and pyclipper** (T-R2); model
  downloaded at build with sha256.

Plan: `plan-phase3.md` (14 tasks).

Done when: harness ≥ spec §5.4 numbers with 0–1 early per 80, GPU path proven on storage NVIDIA and plex NVIDIA +
Intel (self-test picks CPU on the iGPU), and combined credits decisions beat Plex's native credits markers on the
same files. Owner's Q5 answer: rule J ships as measured (no tuning attempted); a later tuning pass is a follow-up
that must beat rule J on both the 80-file and 205-movie sets with no more early answers.

**Status 2026-09-20: built, audited, final-reviewed and lab-proven on the final image; owner review of PR #241 next.**
Rule J version 2 alone on the 80: 64 within 10 s and 1 early on the GPU decode, 59 and 1 on the CPU — it now meets
§5.4 on **both** paths. Version 2 also refuses a tail with text on screen all through it (a burnt-in timecode, a
station logo) and reads 120 s before the tail for a roll the tail cuts into. Against Plex's own credits markers: the
80 pass all five gate checks on both decode paths; the 205-movie set passes both "never looser than Plex" checks and
fails the usefulness floor and the two wrong caps (credit text answers late on split rolls, spec §13 item 14). Owner,
2026-09-18: ships at Medium now, the late-answer gap is a follow-up. A new broadcast-TV set (51 frame-checked
recordings) shows credit text about as accurate as Plex's own detection there, not better — a known limit (§13 item
15, `evidence/eval/broadcast-tv.md`). GPU path proven on storage NVIDIA (rows 3–5, 9) and on `plex`: NVIDIA picks the
GPU, Intel's self-test picks the CPU on the iGPU (rows 12–13), and row 14 now passes outright, with the Intel GPU's
own per-process counters showing the helpers' render work and none on any other GPU. Lab matrix **16 of 16** on
`final-2` (`bd9e561`), phase 2 24 of 24 and phase 1 16 of 16 on the same image, plus a scale run over 711 real files
(credits 468 useful / 6 wrong; intros 246 / 7, the regressions from the lab's own TheIntroDB budget running out) and
one real season on `plex` (10 of 11 starts within 10 s; every answered episode's after-credits scene kept). The final
review also fixed two bugs the scale run exposed: Plex parts rewritten by Plex's own database migration could never
be updated again, and the app counted its own earlier markers as a server's second opinion. Evidence:
`evidence/lab/phase1-results.md`, `evidence/lab/phase2-results.md`, `evidence/lab/phase3-results.md`,
`evidence/eval/phase3-harness.md`, `evidence/eval/broadcast-tv.md`.

### Phase 4 — Polish (plan: `plan-phase4.md`)
Adjust/Lock editor in the Inspector (drag handles, keyboard nudge, lock, publish to every owner immediately),
AniSkip source (anime; MAL id + episodeLength), Setup Health checks (plugin missing/outdated, Plex Pass missing,
marker tag row absent, DB not local, Plex detection overwrite risk), helper container for Plex on another machine,
docs (README, `docs/reference.md`, `docs/guides.md`), trim evidence before release.
Decide how a locked marker meets **Keep Plex's** / **Keep Emby's**: today the server's own markers of a kept type win
over a locked one (spec §6.2 step 6); a locked type could bypass kept types, or go to Emby with `ReplaceOwn`.

## Owner checkpoints (ask, don't assume)
1. Plex lab claim token (https://plex.tv/claim, valid 4 min) — phase 1 lab tasks.
2. Running the `pr-241` image as a second container on `plex` against one real show — after phase 1 lab passes.
3. Any UI wording/layout that differs from the approved design artifact.
4. Emby catalog forum thread / developer id — phase 2.
5. Enabling the prod Plex DB write for a real server — only when the owner says so.
