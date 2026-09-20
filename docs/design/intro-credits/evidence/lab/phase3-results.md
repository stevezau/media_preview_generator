# Phase 3 lab results

On-screen credit text against the lab servers on `storage` (`up.sh`), real library mounted `:ro`. Tokens in `env`,
scrubbed from every result file. Rows and their evidence: `phase3_matrix.py`; per-row JSON in `results/p3-row-NN.json`
(git-ignored).

## PR image check (pr-241, 003a8d1) (2026-09-20)

**Every row run on the published image passes.** Pulled from GHCR, not built here:
`ghcr.io/stevezau/media_preview_generator@sha256:813f67cb5550d1ce0abd564c95b88b42379dbdb02ae48fbc4e589f5c677a9042`
(`linux/amd64`, image label `revision` 1ec71ba7e7b363aa5dde4eb86fe14a4813f6d7fd).

**In the image:**
- `textdet_helper --check` exits 0 (Python 3.12.3).
- The pinned model `/app/models/ch_PP-OCRv4_det_infer.onnx` is sha256
  `d2a7720d45a54257208b1e13e36a8479894cb74155a5efe29462512d42f49da9`.
- `ffmpeg 8.1.2`.
- `CREDITS_TEXT_VERSION` 2, `SKIP_FRAME_IGNORED` `frozenset({'vp9'})`, `server_markers.READER_VERSION` 4.

**Lab:** `mlab-app` recreated on this image with `MLAB_APP_GPU=nvidia` and a fresh `mlab_app_config`, then
`phase2_matrix.py configure` and `phase3_matrix.py configure`. Raw results kept in `results/pr-241/` (git-ignored):
`p2-row-02.json`, `p2-row-24.json`, `p3-row-{01,02,03,16}.json` and `synth-audio-credits-text.json`.

| Row | Result | Evidence |
|---|---|---|
| P3 1 Capability and Settings row | pass | Available in the CPU and GPU containers; a broken model path reports "Not available" naming it. |
| P3 2 Scene after the credits, High then Medium | pass | High: Needs review, nothing published. Medium: published from credit text alone; both Embys say it skips to the end; the Inspector's Credit text lane shows start-end. |
| P3 3 GPU decode and WebGPU | pass (on the second run, see below) | `-hwaccel cuda`, `scale_cuda=320:180`, `-threads 2`; self-test "GPU (median 11.78 ms per frame, CPU 17.92 ms; GPU/CPU 0.654 per round), pinned to 0000:02:00.0"; the GPU and CPU answers agree within 2 s at both edges. |
| P3 16 A roll to the end of the file | pass | No end stored; the decision runs to the end of the file; 2 decodes, 1 refine window; neither Emby says it skips to the end. |
| P2 24 Jellyfin store file | pass | Both Jellyfins: 145-byte complete JSON with the item's Intro, Outro and file size; served after publish, after a container restart and after a library scan; no `.tmp` left; strace shows write → `fsync` on the same descriptor → rename. |

**Synth Audio re-check (credit text must give no answer): confirmed.** Phase 2 row 2 passes, and its own notes read
"credits published: {}" at High and at Medium. The Inspector payload of all five episodes, read after that row and
kept in `results/pr-241/synth-audio-credits-text.json`, shows one `credits_text` row with no start on each, and
credits `no_evidence` on each.

**Row 3 failed its first run, on GPU contention, not on the image.**
- The self-test read "CPU (the GPU wasn't at least 10% faster than the CPU (median 17.08 vs 17.04 ms per frame;
  GPU/CPU 0.977 per round))", so the row's "the self-test kept the GPU" check failed.
- At that moment the host's own `whisper-asr` service held the GPU at 92–100% with 3.2 GiB in use, for the whole run.
- Once it finished and the GPU read 0%, the same row passed with GPU/CPU 0.654. Everything else in the row (the CUDA
  decode flags, the WebGPU helper, both answers) passed in both runs.
- The app behaved correctly: it measured, found no gain, and moved text detection to the CPU. The same flip under
  another load on the card is recorded on `plex` in "Side-by-side on `plex` (roadmap checkpoint 2, owner,
  2026-09-19)".
- The failing run's own result file was overwritten by the passing re-run, and the app container is recreated inside
  the row, so its log went with it. The numbers and the `nvidia-smi` readings above are from watching that run, not
  from a kept artefact. `results/pr-241/p3-row-03.json` is the passing run.

**Harness fix (row 24, `phase2_matrix.py`).** Row 24 read the file's decided intro and credits *before* dropping the
markers, so on a fresh config, where nothing had decided that file yet, its premise failed and every comparison
against an empty expectation failed with it. It now decides the file once first. Verified by wiping
`mlab_app_config`, configuring, and running row 24 alone: pass, with the seeding job's id and status in the row's
own evidence (`seed_job`).

## Final (final-2, bd9e561) (2026-09-19)

**The 12 storage rows (1–11 and 16) pass on the first run.** Rows 12–15 run on `plex` (another agent) and are not part
of this pass.

- **Image:** `media_preview_generator:final-2` (`sha256:7050dc5d1385…`, `GIT_SHA` bd9e561). It adds rule J version 2
  with the text-all-through guard, the VP9 keyframe pass fix, and reading 120 s before the tail for a roll the tail
  cuts into.
- **Run:** `MLAB_APP_IMAGE=media_preview_generator:final-2`, `./phase3_matrix.py configure`, then `run` 1–11 and 16,
  one row per call (09:05–09:19 UTC).
  - It ran right after the phase 2 pass, on that app's config.
  - The rows that recreate the app used the same image (row 11's premise checks it).
- **Raw results:** `results/final-2/p3-row-*.json`.

| Row | Result | Evidence |
|---|---|---|
| 1 Capability and Settings row | pass | Available in the CPU and GPU containers; a broken model path reports "Not available" naming it. |
| 2 Scene after the credits, High then Medium | pass | Answer 541 000 / 659 000 ms. High: Needs review, nothing published. Medium: decided 541 000–659 000 ms from credit text alone. |
| 3 GPU decode and WebGPU | pass | Self-test "GPU (median 12.35 ms per frame, CPU 19.92 ms; GPU/CPU 0.6628 per round), pinned to 0000:02:00.0"; GPU and CPU answers equal (541 000 / 659 000). |
| 4 GPU decode failure | pass | The AV1 copy: "couldn't process … on the GPU and is retrying on CPU" (ffmpeg exit 69), then 541 000 / 659 000 on the CPU. |
| 5 Helper killed | pass | One WARNING "moves to the CPU for the rest of this run of the app … Broken pipe"; both jobs completed with the right answer. |
| 6 Cancel mid-decode | pass | Decode gone within 10 s, job `cancelled`, stored answer untouched. |
| 7 Reuse and forced | pass | A normal job decoded nothing; a forced one decoded again (tail + 21 s refine window). |
| 8 Detection unavailable | pass | A stored answer doesn't decide while detection is unavailable; it decides again once back. |
| 9 Resources | pass | Every decode `-threads 2`; peak 1 helper; the WebGPU helper has 48 threads (recorded). |
| 10 The app reproduces the harness | pass | All ten movies: start and end equal to Task 11's GPU harness answers (e.g. Avengers Infinity War 8252 / 8851 s; Summit of the Gods 5423 s, no end). Rule J version 2 didn't move any of these ten. |
| 11 Phase 1 and 2 regressions | pass | `phase2_matrix.py run 1 3 8` and `phase1_matrix.py run 2 7` exited 0 on final-2. |
| 16 A roll to the end of the file | pass | No end stored; decided 541 000–660 000 ms (to the end of the file); 2 decodes, 1 refine window. |

**Image under test:** `media_preview_generator:intro-credits-p3`,
`sha256:f7a77a46f94c27ccdfb98ecfd48f215cbb51b9438bdba30614123544dca29ec2`, built from `feat/markers-detection`
`e24b3fc` (version `4.4.3.dev104`). `mlab-app` runs it with the NVIDIA runtime and `/dev/dri`
(`MLAB_APP_GPU=nvidia`). GPU: one Quadro P5000 (`cuda:0`, PCI `0000:02:00.0`).

## How it was run

```bash
cd /home/data/workspace/plex_generate_vid_previews            # or a worktree, with MLAB_DIR set to this lab folder
VER=$(/home/data/.venv/bin/python -m setuptools_scm)
nice -n 19 docker build --build-arg SETUPTOOLS_SCM_PRETEND_VERSION="$VER" \
  -t media_preview_generator:intro-credits-p3 . > "$MARKERS_BENCH_DIR/logs/build-p3-lab.log" 2>&1
cd docs/design/intro-credits/evidence/lab
export MLAB_DIR="$PWD"                                        # only needed when running from a worktree
./synth_credits.sh
docker rm -f mlab-plex mlab-jellyfin mlab-jf12 mlab-emby mlab-emby49 && ./up.sh   # new mount; volumes kept
export MLAB_APP_IMAGE=media_preview_generator:intro-credits-p3
export MLAB_SHOTS=/tmp/mlab-phase3-shots
MLAB_APP_GPU=nvidia ./app.sh recreate
./phase3_matrix.py configure
./phase3_matrix.py run 1 2 3 4 5 6 7 8 9 10 11 16
```

Rows 2–9 build on row 2's stored answer, so that order matters. Rows 12–15 are refused by this script (and asking for
only those exits 3, so "nothing ran" can't be read as a pass); they run on the `plex` host through `plex_rows.py`
(Task 13 Step 6, owner decision Q7).

**Row 10's harness answers.** The row reads Task 11's GPU run from `evidence/eval/phase3_credits_gpu.json`
(git-ignored, local to `storage`; `MLAB_HARNESS_ANSWERS` overrides the path). That file is the `--json` output of:

```bash
export MARKERS_EVAL_EVIDENCE=<repo>/docs/design/intro-credits/evidence
export MEDIA_PREVIEW_TEXTDET_MODEL="$MARKERS_EVAL_EVIDENCE/credits/bench/textdet-model/ch_PP-OCRv4_det_infer.onnx"
nice -n 19 /home/data/.venv/bin/python -m tools.markers_eval credits-text --decode gpu --sets 80,205 --online \
  --sheets "$HOME/.cache/markers_eval/sheets-phase3" \
  --json docs/design/intro-credits/evidence/eval/phase3_credits_gpu.json
```

The results below are **one clean run of that exact command** (2026-09-18, exit 0, twelve of twelve rows pass), from
deleted `results/p3-row-*.json` and an empty `$MLAB_SHOTS`. Earlier passes, whose failures are what "What the first
runs turned up" below records, were discarded.

## The synthetic movies

`synth_credits.sh` writes two H.264 movies (keyframe every 2 s) plus a staged AV1 copy, all 640×360:

| File | Shape | Duration | Truth |
|---|---|---|---|
| `Synth Credits (2024).mkv` | 540 s gradients, 120 s credit roll, 40 s scene | 700 s | credits start 540 s, skip ends at the last credit |
| `Synth Credits Open (2025).mkv` | 540 s gradients, 120 s credit roll | 660 s | credits start 540 s, skip runs to the end of the file |
| `_staging/Synth Credits (2024) - AV1.mkv` | same as the first, AV1 | 700 s | the Pascal card has no AV1 NVDEC |

The app's own `find_credits` on the host (shared venv, CPU path) answers **541.0 s / 659.0 s** on the scene movie,
**541.0 s / no end** on the open movie and **541.0 s / 659.0 s** on the AV1 copy — the truth the rows check against.

**Why 540 s of gradients and only a 40 s scene** (a change from the brief's 420/120/60): the first run of row 2 had
perfect detection (421 s / 539 s on a 600 s file) and *no decision at all* —
`decide.sanity_problem` throws out a credits candidate that starts before the last 25 % of the file, and 420 s of 600 s
is 70 %. Q3 in turn keeps an end only when more than 30 s follows the roll. Both hold together only when the gradients
run to at least three times the roll plus the scene, so the shape moved to 540/120/40 (77.3 % of the file) and
660 s / 700 s durations. Nothing in the app changed; the fixture was outside the app's own plausible range.

## Rows

Every row asserts its premise before its checks; a row whose premise doesn't hold is `fail (premise)`, never a pass.
Screenshots are under `$MLAB_SHOTS` (`/tmp/mlab-phase3-shots` for this run).

| Row | Premise (held) | Checks | Result |
|---|---|---|---|
| 1 Capability and the Settings row | the CPU container, the GPU container and a container with a broken model path all answered `GET /api/markers/sources/local` | available in the CPU container and in the GPU container; with `MEDIA_PREVIEW_TEXTDET_MODEL=/nonexistent/model.onnx` not available and the reason names that path; the Settings source row shows the "Not available" badge (`p3-row01-settings-unavailable.png`) | **pass** |
| 2 Scene after the credits, High then Medium | the app reads the 700 s movie; credit text answered 541 000 ms (within 10 s of 540 s); the stored end is 659 000 ms (inside 656–661 s) | High: credits `needs_review`, nothing of ours published on any of the five servers. Medium: the decision keeps the text's end; Plex, Jellyfin 10.11 and Jellyfin 12 publish 541 000–659 000 ms; both Embys publish the start and say "Emby skips to the end of the file"; the Inspector's Credit text lane reads `9:01–10:59` (`p3-row02-inspector.png`) | **pass** |
| 3 GPU decode and WebGPU text detection | a GPU worker ran the job (pool of one GPU worker, no CPU workers); a credit-text decode was seen; the CPU-only run answered; **the CPU-only run really decoded on the CPU** (3 decodes, none with `-hwaccel`) and **detected text on a `--backend cpu` helper** | all three decodes used `-hwaccel cuda` and `scale_cuda=320:180`, all used `-threads 2`; a `--backend webgpu` helper ran; the self-test kept the GPU (`Credit text detection on cuda:0: GPU (12.68 ms per frame, CPU 19.47 ms), pinned to 0000:02:00.0`); the GPU answer equals the CPU answer exactly (541 000 / 659 000 both ways); the dashboard shows "GPU Worker 1 (Quadro P5000)" (`p3-row03-workers.png`) | **pass** |
| 4 GPU decode failure reruns on the CPU | a CUDA decode of the AV1 copy was tried | the worker logged "couldn't process … on the GPU and is retrying on CPU"; a decode without `-hwaccel` followed; the answer landed within 10 s of 540 s | **pass** |
| 5 Helper killed during a request | a `--backend webgpu` helper was running; the job was decoding when it died (`kill -STOP`, start a forced job, wait for a decode, `kill -KILL`) | exactly one WARNING "Credit text detection on cuda:0 moves to the CPU for the rest of this run of the app: its GPU helper failed: couldn't send the frames: [Errno 32] Broken pipe"; the job still completed with the right answer; a second forced job started no new WebGPU helper **and still completed with the right answer** — without that last pair, credit text dying outright would read the same as falling back to the CPU | **pass** |
| 6 Cancel mid-decode | one answer was stored before the row; the job was decoding when it was cancelled | the decode process was gone within 10 s; the job is `cancelled`; the stored answer's `fetched_at` is unchanged | **pass** |
| 7 Reuse, and a forced run reads again | the row's own forced run stored one answer | a normal job decoded nothing and left `fetched_at` alone; a forced job decoded again and stored a newer answer | **pass** |
| 8 Detection unavailable | one answer was stored; the app (recreated with the broken model path) reports credit text unavailable | at Medium the stored answer did not decide credits, and the evidence row is still stored; once the app is recreated without the broken path, the next normal job decides credits again | **pass** |
| 9 Resources during a GPU job | a forced GPU job completed; a decode and a WebGPU helper were seen | every decode used `-threads 2`; no more than two text detection helpers ran at once — a ceiling this row stays under rather than exercises: **one helper was ever running (peak 1)**, one GPU worker over one file. Recorded, not checked: the WebGPU helper's `Threads:` was 48 — ONNX Runtime and Dawn start their own threads, `THREADS = 2` sets only the intra-op pool | **pass** |
| 10 The app reproduces the harness on real movies | ten movies from Task 11's GPU harness run are exposed by the scale mounts (three with an end, seven without, so both Q3 cells are covered); every one stored exactly one credit-text answer | every start is within 1 s of the harness's — eight identical to the millisecond, "Midas Man" 6 706 000 ms against 6 705.0 s and "Land of the Dead" 5 544 000 ms against 5 544.007 s; every end **matches the harness's value**, None for None (three movies have one, and all three agree to the millisecond). The comparison is against Task 11's `--decode gpu` run on the app's own GPU path: the CPU harness disagrees with the app on three of these same ten titles, so this row proves "the app reproduces the harness on the same decode path", not unconditionally. Online sources were off and `publish_when` Medium for the row, on one GPU worker | **pass** |
| 11 Phase 1 and 2 regressions on the phase-3 image | `docker inspect mlab-app` runs `media_preview_generator:intro-credits-p3` | `phase2_matrix.py run 1 3 8` exited 0 (capability on five servers; High alone — season audio + server markers never decide, G3; Emby web Skip Intro) and `phase1_matrix.py run 2 7` exited 0 (Plex `includeMarkers=1` equals the decisions; a touched mtime re-probes and the servers end up showing our markers). Their own `results/p2-row-NN.json` / `results/row-NN.json` carry the detail | **pass** |
| 16 A roll that runs to the end of the file (Q3) | the app reads the 660 s movie; credit text answered 541 000 ms; the sampler saw the tail decode and a refine window, so it was watching while the file's windows were refined | the stored answer's `end_ms` is None; the decision is 541 000–660 000 ms (`decided_by: credits_text`), i.e. the skip runs to the end of the file; Plex and both Jellyfins publish credits ending at 660 000 ms; neither Emby row carries "Emby skips to the end of the file"; no decode past the credits start was seen (2 decodes, 1 refine window) | **pass** |

## What the first runs turned up

Nothing here is an app change; the first four are facts about the app the rows had to be written around, the fifth is
a fact about `docker logs`, and each one made a row fail loudly first.

1. **The 75 % sanity bound decides whether credit text can ever be used** (`decide.sanity_problem`, "credits starts
   before the last 25 % of the file"). The brief's 420/120/60 movie detected perfectly and then got
   `no_evidence — 1 candidate(s) failed sanity checks`. The fixture moved to 540/120/40; see the table above.
2. **A library the app first sees on `refresh-libraries` comes back off**, and the Inspector's search skips off
   libraries, so row 2's screenshot found no result to open. `configure()` now turns the Synth Credits library on for
   every server after the refresh.
3. **The worker pool is built once per process and kept.** Posting `gpu_config`/`cpu_threads` mid-run reconciles
   (`GPU reconciliation: added=1`) but leaves the workers already there, and the dispatcher hands the next file to
   whichever is free — so row 3's "GPU" job ran on the CPU worker the CPU-only block had created, and the row's
   CPU-vs-GPU comparison was two CPU runs agreeing with each other. `CpuOnly` and `GpuWorker` now recreate the app, so
   the pool holds exactly the worker the row names. A related trap: a GPU missing from `gpu_config` is included *with
   defaults* (`job_runner._build_selected_gpus`), so "off" needs an explicit row per detected device, not an empty list.
4. **A cancelled forced run leaves the answer due again**, so the next normal run reads the file. Row 7 followed row
   6's cancel and saw a decode it read as a reuse failure; it now takes its own forced baseline first.
5. **`docker logs` only covers the current container.** Once `CpuOnly` / `GpuWorker` began recreating the app on exit
   (so the live pool matches the settings they put back), rows 3 and 4 read their log lines *after* the block and got
   nothing — row 3's self-test line and row 4's GPU-retry line had gone with the replaced container. Both reads now
   happen inside the block, and `app_log_lines`' docstring says why they have to.

Also recorded, not a check: the 0.5 s process sampler misses a 21 s refine window's decode about half the time (row 3
saw all three decodes, row 16 saw two of two, the first row-3 run saw two of three). Row 16's "no window past the
credits was seen decoding" is therefore corroborating. The stored `end_ms` being None is decisive for **what was
stored**; it implies no end window was decoded only because this fixture's coarse end sits inside the 30 s bound
(`rule_j.KEEP_AFTER_CREDITS_S`). In general `credits_end` can also return None *after* decoding the window, when the
refined end fails that same check — so the implication is a property of this fixture, not of the code.

## Rows 12–15 (the `plex` host, owner decision Q7, 2026-09-16)

Run in a throwaway container on `plex` after telling the owner immediately beforehand (per the controller's standing
instruction): no `/data*` mount, no Plex config, no access to the prod Plex; the container, `/tmp/p3lab` and the
loaded image were removed right after (confirmed: `ls /tmp/p3lab` fails, the image is gone from `docker images`, no
leftover `p3lab` container).

**Row 12 — NVIDIA on real hardware: pass.** Real TITAN RTX, self-test picked `webgpu` (4.94 ms/frame vs CPU 6.18 ms),
pinned to `0000:01:00.0`, box counts identical to CPU.

**Row 13 — Intel on real hardware: pass.** The self-test picked `cpu` ("the GPU was slower than the CPU") for the
RPL-S integrated GPU — the correct, safe call on a weak iGPU, and the first time this decision has been proven on
real Intel hardware rather than reasoned about.

**Row 14 — the host's view of both GPUs while the Intel self-test runs, 14 rounds over ~40 s: partial.** The row's
own documented pass bar needs both halves to hold: no NVIDIA-side PID during the Intel self-test, **and**
`intel_gpu_top` showing render work while it runs. Only the first half is confirmed. Every round independently chose
`cpu`. Live process capture during the run:
- `docker top p3lab`: at different moments, either the CPU-only helper (`--backend cpu ... --no-selftest`) or, mid
  self-test, a `--backend webgpu ... --pci-bus-id 0000:00:02.0` helper — the Intel iGPU's own PCI address, distinct
  from the NVIDIA card's `0000:01:00.0` from row 12. Per-device pinning is real, not assumed.
- `nvidia-smi pmon -c 5 -s u`: no `p3lab` process appears on the NVIDIA GPU at any sample, only `Xorg`, an unrelated
  host `python`, and the real Plex Transcoder already running on this host. **This half of the bar holds**: the
  Intel self-test never touches the NVIDIA GPU.
- `intel_gpu_top -J -s 500`: all four engines (Render/3D, Blitter, Video, VideoEnhance) read 0.0% busy across every
  sampled window, including one taken while the `webgpu` helper process was confirmed live via `docker top` in the
  same second. **This half of the bar does not hold** — no render work was observed, so by the row's own bar this
  is not a clean pass. Two candidate reasons, neither confirmed: the self-test's brief inference workload (a handful
  of 320×180 frames) may be too short for a 500 ms sample window to catch, or Mesa's compute submission on this
  iGPU may not register under `intel_gpu_top`'s engine-busy categories. This does not contradict the pinning
  evidence above (`docker top` + the helper's own `--pci-bus-id` argument establish that independently, and both
  hold) -- it just means the render-activity half of row 14's own bar is unconfirmed, not that the pinning is in
  doubt.

**Row 15 — VAAPI decode on the real Intel render node, against the milestone-audit fix: pass.** `duration_ms: 700000`
confirms the row's own duration bug (N1, found by the deep review before this ever reached `plex`) is fixed: it
probes the real fixture length rather than a copied constant. VAAPI and CPU decode agree exactly: start 541.0 s /
end 659.0 s both ways.

**Taken together, rows 12–15 close the NVIDIA-isolation half of Task 5's disclosed cross-vendor gap**: a GPU
self-test and its own PCI pin are now proven on real NVIDIA hardware (rows 3, 12), and on real Intel hardware the
self-test correctly chose CPU with no NVIDIA-side contamination and the right PCI address in the helper's own
argument (rows 13, 14). The Intel-side render-activity half of the gap -- direct hardware confirmation that the
Intel GPU itself executed work, independent of process arguments -- remains open per row 14's partial result.
(Closed on the final image: see "Final (final-2, bd9e561) — plex rows" below, where row 14 reads the helpers' own
DRM counters.)

## PR image check

`pr-241` pulled from GHCR: `ghcr.io/stevezau/media_preview_generator@sha256:f96684678a6fb40a2dfbb3000ea32e958410dcfcc61cec5224cf84fc57f223a8`,
built from `a3c6c32`. `textdet_helper --check` exits 0; the pinned model's sha256 matches the one recorded in the spec
and Task 7's evidence. CI is green on this commit (lint, tests, e2e shards, plugins); its arm64 image build only
runs on `dev` pushes and tags, so arm64 hasn't been built from this branch. Every runtime package the branch adds,
with its dependencies, has a prebuilt aarch64 wheel for the image's Python 3.12 (checked with `pip download
--platform manylinux_2_28_aarch64 --only-binary=:all:`), and the WebGPU wheel is x86_64 only by marker. The lab app recreated on this
image (`MLAB_APP_GPU=nvidia`) and re-ran rows 1, 2, 3 and 16 after removing their old result files:

- **Row 1 — capability and the Settings row: pass.** Available in the CPU and GPU containers; a broken model path
  reports "Not available" naming the path; the Settings source row shows the badge.
- **Row 2 — scene after the credits, High then Medium: pass.** High: `needs_review`, nothing published. Medium: the
  text's end is kept; Plex and both Jellyfins publish start and end; both Embys publish the start and say "skips to
  the end".
- **Row 3 — GPU decode and WebGPU text detection: pass.** The GPU run used `-hwaccel cuda`/`scale_cuda` and
  `-threads 2`; a WebGPU helper ran and the self-test kept the GPU; the GPU and CPU-only runs agree within 2 s on
  both the start and the end.
- **Row 16 — a roll that runs to the end of the file: pass.** No end stored; the decision runs to the end of the
  file; Plex and both Jellyfins publish accordingly; neither Emby row says "skips to the end"; no decode was seen
  past the credits start.

All four pass on the exact image the PR ships.

## Side-by-side on `plex` (roadmap checkpoint 2, owner, 2026-09-19)

The same `pr-241` digest, pulled on `plex`, ran as a second container next to production: one real season (Rick and
Morty S01, 11 episodes) mounted read-only (a write into it was refused), its own config volume, no Plex config
folder mounted, and both GPUs. The app itself won't turn Intro & Credits on for a Plex server until the database
write is confirmed, and that step was not taken, so the shipped detector ran directly in the container
(`plex_season_detect.py`, NVIDIA decode on the TITAN RTX) and nothing was published anywhere. Everything was removed
afterwards (container, volume, temp folder, images); production containers were not touched.

This season is the case credit text exists for: every episode has chapters, but they are untitled (timestamps or
"Chapter NN"), so the app's chapter rules decide nothing (checked with `chapter_candidates`: no candidate on any
episode). The chapter boundaries still frame the roll, so they serve as the truth here: a ~25–30 s credits chapter
followed by the after-credits scene, except the pilot, whose roll runs to the end of the file.

| Episode | Credits chapter (s) | Credit text (s) | Start − truth | End − truth |
|---|---|---|---:|---:|
| E01 | 1295.5 → end of file | 1295.0 → no end | −0.5 | runs to the end, as the chapter does |
| E02 | 1246.7 – 1276.7 | 1247.0 – 1273.0 | +0.3 | −3.7 |
| E03 | 1228.7 – 1258.7 | 1238.0 – 1258.0 | +9.3 | −0.7 |
| E04 | 1162.2 – 1186.5 | no answer | | |
| E05 | 1203.0 – 1232.8 | 1211.0 – 1232.0 | +8.0 | −0.8 |
| E06 | 1191.9 – 1220.6 | 1198.0 – 1219.0 | +6.1 | −1.6 |
| E07 | 1239.2 – 1269.3 | 1240.0 – 1269.0 | +0.8 | −0.3 |
| E08 | 1249.9 – 1279.8 | 1256.0 – 1279.0 | +6.1 | −0.8 |
| E09 | 1271.0 – 1301.8 | 1278.0 – 1301.0 | +7.0 | −0.8 |
| E10 | 1264.3 – 1294.3 | 1269.0 – 1293.0 | +4.7 | −1.3 |
| E11 | 1274.9 – 1305.8 | 1275.0 – 1305.0 | +0.1 | −0.8 |

- **10 of 11 starts within 10 s**: the only early one is E01, by 0.5 s (far inside the harness's 10 s "wrong" bar);
  the rest are 0.1–9.3 s late.
- **After-credits scenes kept**: E02–E11 all have a scene after the roll. All nine of them that got an answer got an
  end within 4 s of the roll's end, so the skip stops before the scene instead of running to the end of the file
  (Q3 on real footage). The pilot, whose roll ends the file, correctly got no end.
- **E04: no answer** (the detector returned no start and raised nothing), so no skip at all from this source: the
  safe outcome. Not investigated further; this run didn't capture the detector's warnings (the script now prints
  them), so a re-run is the first step.
- **Cost**: 1.9–5.2 s per episode after the first (13.2 s, including the self-test), NVIDIA decode.
- **Device choice**: the self-test picked the CPU for text detection on the TITAN this time ("the GPU was slower than
  the CPU"), where row 12 two days earlier picked the GPU (4.9 vs 6.2 ms per frame). The GPU has to be at least
  10 % faster to be kept, and this host also runs the production Plex Transcoder on that card (seen in row 14).
  `nvidia-smi` a few minutes after the run showed 0 % GPU and decoder use, but load during the self-test itself
  wasn't captured, so why it flipped isn't established. The device choice isn't expected to change the answer: the
  self-test only keeps a GPU whose box counts equal the CPU's on its 20 synthetic frames, and row 3 found the GPU
  and CPU answers within 2 s of each other on real decode paths.

## Final (final-2, bd9e561) — plex rows (2026-09-19)

Rows 12–15 again, on the final image, under the same Q7 terms, plus a VP9 keyframe-pass check per GPU vendor.

**Image:** `media_preview_generator:final-2`. On `storage` its id is `sha256:7050dc5d1385…`; on `plex` the loaded copy
reads `sha256:2fbaa634aa88…`. It is the same image: `storage` runs Docker's containerd image store, which shows the
manifest digest, and `plex` the classic store, which shows the config digest. The 24 layer ids are identical on both
hosts. The app package inside matches `git archive bd9e561 media_preview_generator` file for file (the only extra is
the generated `release_notes.json`). The package reports version `0.0.0+unknown` because the image was built without
`SETUPTOOLS_SCM_PRETEND_VERSION`, so the file comparison is what ties it to `bd9e561`.

**How it ran.** Each row ran in its own container, `p3final`, started with `--rm --network none --cpus 2`, the NVIDIA
runtime and `/dev/dri`. The only mount was `/tmp/p3final` on `plex`, read-only at `/work`. It held the scripts and
`Synth Credits (2024).mkv` (same sha256 as on `storage`). Everything ran under `nice -n 19` inside the container.
There was no `/data*` mount, no Plex config and no production volume. There were two sessions: rows 12–15 first, then
the VP9 check again after the Architecture Review tightened it (the table below is from that second run). The image
was loaded before each session. After each one, `/tmp/p3final` and the image were removed from `plex`, and no
`p3final` container was left. `docker ps` on `plex` was the same before the first session and after the second: the
same container ids, and `plex-generate-previews` and `plex` kept their start times with no restarts. The hardware was
a TITAN RTX (`0000:01:00.0`) and a UHD 770 (`0000:00:02.0`, `i915`, kernel 6.17). The GPUs were idle (0 %). A Plex
transcode was using the CPU, and the load average was about 14 on 32 threads.

```bash
docker save media_preview_generator:final-2 | ssh plex 'nice -n 19 docker load'
ssh plex 'mkdir /tmp/p3final'
scp plex_rows.py plex_vp9_keyframes.py "synth/Synth Credits (2024)/Synth Credits (2024).mkv" plex:/tmp/p3final/
for script in "plex_rows.py 12" "plex_rows.py 13" "plex_rows.py 14" "plex_rows.py 15" "plex_vp9_keyframes.py NVIDIA INTEL"; do
  ssh plex "docker run --rm --name p3final --network none --cpus 2 --runtime=nvidia -e NVIDIA_VISIBLE_DEVICES=all \
    -e NVIDIA_DRIVER_CAPABILITIES=all --device /dev/dri:/dev/dri -v /tmp/p3final:/work:ro \
    --entrypoint nice media_preview_generator:final-2 -n 19 python3 /work/$script"
done
ssh plex 'rm -rf /tmp/p3final; docker rmi media_preview_generator:final-2'
```

During row 14, the host also ran `nvidia-smi pmon -c 36 -d 1 -s u` and `docker top p3final` every 2 s.

| Row | Result | Key numbers | 2026-09-16 run |
|---|---|---|---|
| 12 NVIDIA | **pass** | The self-test kept WebGPU: 5.15 ms per frame against 6.19 ms on the CPU (0.836), pinned to `0000:01:00.0`. Box counts equal the CPU's | pass (4.94 vs 6.18 ms) |
| 13 Intel | **pass** | The self-test kept the CPU: the GPU took 17.61 ms per frame against 11.11 ms on the CPU (1.59) | pass (CPU) |
| 14 Both GPUs during the Intel self-test | **pass** (all five checks) | 8 self-tests in 40 s, and all 8 chose the CPU. Every WebGPU helper (8 of 8) had `--pci-bus-id 0000:00:02.0` and 2.08–2.81 s of `drm-engine-render` time on the Intel GPU in its own DRM fdinfo. No helper had a DRM file open on any other device. The sampler ran to the end | partial: `intel_gpu_top` read 0 % |
| 15 VAAPI decode | **pass** | `duration_ms` 700 000. VAAPI and CPU both answered 541.0 s start and 659.0 s end | pass (same numbers) |

- **Row 12:** the TITAN kept the GPU again. The switch to the CPU seen in the side-by-side run above did not happen
  here.
- **Row 14, NVIDIA: not observable through fdinfo.** The NVIDIA driver publishes no DRM fdinfo counters (its
  `/dev/nvidia*` nodes aren't DRM files), so the fdinfo half of the check can't see NVIDIA work. On the host,
  `pmon` listed no text detection helper on the TITAN in any of its 36 samples. The only container process it listed
  was the app's own Vulkan startup probe (`ffmpeg -init_hw_device vulkan`: one sample, type G, no SM use). That probe
  checks NVIDIA on purpose for Dolby Vision tone-mapping; it isn't text detection. The fdinfo sampler also caught
  three of those probe processes, with zero Intel counters.
- **This closes the gap row 14 left open on 2026-09-16.** The Intel GPU's own counters show the WebGPU helpers
  running work on it: about 2 s of render-engine time per self-test. `intel_gpu_top`'s 500 ms whole-engine samples
  had missed that work.

### VP9 keyframe pass per vendor

`plex_vp9_keyframes.py` encodes a 30 s 640×360 VP9 clip (`libvpx-vp9`, 24 fps, `-g 48`) into a temporary folder
inside the container. ffprobe finds 15 packets flagged as keyframes, at 0, 2, … 28 s. `keyframe_thinning` answers
`drop_non_key=True, keep_every=None`. For each device, the script builds the app's keyframe-pass command with
`decode_command` over 10–30 s (20 s, which holds 10 flagged keyframes) and runs it. Two controls run beside it. The
same command without the drop must give more frames. On a GPU, the same command without its hwaccel arguments must
fail: that shows the GPU scale filter takes only GPU surfaces, so a GPU run that exits 0 decoded on the GPU. The run
fails if the window holds no keyframe or if a vendor named on the command line (`NVIDIA INTEL` here) is missing. It
passed every check.

| Device | Hardware decode arguments (the app's builder) | The app's command | Without the drop (`-skip_frame nokey` only) | Without the hwaccel arguments |
|---|---|---|---|---|
| CPU | none | 10 frames, all `iskey:1`, at 10, 12 … 28 s | 480 frames | n/a |
| NVIDIA TITAN RTX | `-hwaccel cuda -hwaccel_device 0 -hwaccel_output_format cuda` | 10 frames, all `iskey:1`, at 10, 12 … 28 s | 480 frames | exit 218 (`-38`, Function not implemented: the filter graph can't take software frames), 0 frames |
| Intel UHD 770 | `-hwaccel vaapi -hwaccel_device /dev/dri/renderD128 -hwaccel_output_format vaapi` | 10 frames, all `iskey:1`, at 10, 12 … 28 s | 480 frames | exit 218 (`-38`, Function not implemented: the filter graph can't take software frames), 0 frames |
| AMD | no AMD GPU on `plex` (`lspci`) or on `storage` (its only render node is NVIDIA's) | not run | not run | not run |

- **Both GPUs decode VP9 in hardware.** The app's command exited 0 with 10 frames on each, and the software-frames
  control failed on each. No vendor present needs the CPU fallback.
- **If a GPU couldn't decode VP9**, the app would handle it as it handles any other GPU decode failure: ffmpeg exits
  non-zero or gives no frames, the GPU decode error becomes `CodecNotSupportedError`, and the worker reruns the file
  on the CPU (row 4 shows this path with AV1 on the P5000).
- **The drop is what gives 10 frames.** Without it, `-skip_frame nokey` let all 480 frames through on the CPU and on
  both hwaccels: 48 times the decode and text detection work (one keyframe per 48 frames).

## Resetting the lab

```bash
cd docs/design/intro-credits/evidence/lab
export MLAB_DIR="$PWD"
docker rm -f mlab-plex mlab-jellyfin mlab-jf12 mlab-emby mlab-emby49 && ./up.sh   # volumes (claim, keys) kept
MLAB_APP_IMAGE=media_preview_generator:intro-credits-p3 MLAB_APP_GPU=nvidia ./app.sh recreate
rm -f results/p3-row-*.json
```

The rows leave the lab at `publish_when` High with the worker settings they found, and `CpuOnly` / `GpuWorker`
recreate the app when they restore those settings, so the live pool matches them again: the running app
live-reconciles GPU workers but never `cpu_threads`, so posting the settings back alone would leave a pool the
settings don't describe. `synth/` and `results/` are
git-ignored; re-encoding the movies (`./synth_credits.sh force`) changes their size, so every server needs a library
refresh afterwards before the rows run again.
