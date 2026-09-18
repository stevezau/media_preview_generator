# Phase 3 lab results

On-screen credit text against the lab servers on `storage` (`up.sh`), real library mounted `:ro`. Tokens in `env`,
scrubbed from every result file. Rows and their evidence: `phase3_matrix.py`; per-row JSON in `results/p3-row-NN.json`
(git-ignored).

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
