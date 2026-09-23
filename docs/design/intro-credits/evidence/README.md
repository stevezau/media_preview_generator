# Intro & Credits — evidence and lab

Everything the design spec (`../spec.md`) cites lives here. Nothing important lives in a
session scratchpad. **Local-only (gitignored, public repo):** every `*.json`/`*.jsonl` truth set and result (they list
real library file paths), `lab/*_truth.txt`, `credits/spot.txt`, `credits/framechecks/`, `lab/env`, `lab/synth/`,
`online/skipdb-dump.json`, `plugins/emby-4.10/embylibs/`, the phase-3 harness caches
(`eval/phase3_credits_gpu.json`, `eval/phase3_credits_cpu.json` — details with file paths; the counts and folder
names those support are in `eval/phase3-harness.md`), and `credits/bench/` (the phase-3 measurement scripts' raw
outputs, model and Python 3.12 venv; `$MARKERS_BENCH_DIR`). They exist only on `storage`. `lab/env` holds lab-server
tokens: chmod 600, never commit, never paste into docs.

## Map

| Folder | What | Key files |
|---|---|---|
| `lab/` | Throwaway servers on `storage` + clients | `up.sh` (bring-up), `env` (tokens), `jf_client.py`, `emby_client.py` (API + Playwright skip-button checks), `plexweb_client.py` (Plex Web served by the lab Plex: Skip Intro / Skip Credits for phase 2 row 20, signed in with the lab token, every request allowlisted to the lab Plex and plex.tv), `emby_plugin_check.py` (Emby plugin check table → `phase2-results.md`), `phase2-results.md` (phase 2 lab results: Emby plugin checks, Emby publisher runs, Season view API, Check servers), `plexdb.sh` (Plex SQLite in the lab container), `plex_inject.py` (Plex `extra_data` builder), `phase1_matrix.py` + `scale_score.py` (phase 1 matrix and scale run; results in `phase1-results.md`), `phase2_matrix.py` (phase 2 matrix on phase 1's helpers: season audio, Emby, Check servers, version drift, Season view; results in `phase2-results.md`), **`phase3_matrix.py`** (phase 3 matrix on phase 1 and 2's helpers: on-screen credit text — capability, High/Medium publishing, the GPU path, helper crashes, reuse, resources, real movies against the harness; results in **`phase3-results.md`**), **`plex_rows.py`** (phase 3 rows 12–15 only, run inside a throwaway container on the `plex` host under the owner's Q7 terms), `app.sh` (the branch's app as `mlab-app`; `MLAB_APP_GPU=nvidia` adds the NVIDIA runtime and `/dev/dri`, `MLAB_APP_EXTRA_ENV` one more environment variable), `py_write.py` (stock-sqlite write proof), `synth_chapters.sh` (synth episodes with chapters, the two-version movie, staged extra versions), `synth_audio.sh` (synth episodes sharing a theme at different offsets, for season audio), **`synth_credits.sh`** (two synthetic movies with a scrolling credit roll from 540 s — one with a 40 s scene after it, one that ends with the roll — plus a staged AV1 copy for the GPU-decode-failure row), `synth/` (their output; the phase 1 and 2 clips are VP9/Opus — Playwright Chromium has no H.264 — while the credit-roll movies are H.264 with a keyframe every 2 s). The scripts take the lab folder from `MLAB_DIR` (default: their own folder), so a worktree runs against the long-lived lab |
| `plugins/` | Prototype plugins proven in the lab | `jellyfin-10.11/` (net9), `jellyfin-12.0/` (net10), `emby-4.10/` (+ `embylibs/` reference DLLs copied from the Emby container `/system/`), `built/` DLLs |
| `plex-provider-redirect/` | Plex `MetadataProviderUrl` redirect test (not honoured) | `proxy.py`, `log.jsonl`, `pms_marker_strings.txt` |
| `online/` | TheIntroDB / IntroDB / SkipDB accuracy on 43 verified cases | `cases.json`, `query.py`, `online_results.json`, `skipdb-dump.json` (30 MB snapshot — don't commit) |
| `coverage/` | Online coverage on a random prod sample | `cov.py`, `coverage_results.json` |
| `eval/` | TV intro detection eval (118 episodes with studio chapters); phase-3 credits-text accuracy harness | `named_seasons.json`, `run_eval_v3.py`, `eval_results_v3.json` (v1/v2/v3 results), `sweep_intro.*`, `snap_test.py`, `fp_variants.py` + `.log` (alg0/1/2/4, downmix), `fp_alg2.py`, `few_siblings.py` + `.log` (weekly releases / previous season), `phase2-harness.md` (phase 2 harness `tools/markers_eval`: §5.3 reproduction gate, season step gate, full report against Plex's own markers), **`phase3-harness.md`** (phase 3 harness `python -m tools.markers_eval credits-text`: spec §5.4 bar, the owner's Q4 gate per set, ends, frame checks; local-only `phase3_credits_gpu.json`/`phase3_credits_cpu.json` back it with file paths). Fingerprint and credits-text answers are cached per file identity, detector version and decode path — a re-run of either harness costs seconds once cached |
| `detect/` | Detector prototypes | `fp.py`, `fp3.py` (v3 season matcher), credits OCR prototypes |
| `credits/` | Credits-start eval (80 files with chapter truth); phase-3 pre-build measurements | `movie_credit_truth.json` (205 movies), `movies40.json`, `tv40.json`, `features3.py` (GPU extractor → `f3.jsonl`), `eval_rules3.py` (rules + grid), **`rule_j.py` (reproduces spec §5.4 table)**, **`lookback_sweep.py` (reproduces spec §14 2026-09-23: the steps before the credit text tail against the build before them, on 32,384 synthetic files)**, **`empty_window_decode.py` + `empty-window-decode.md` (what the app's keyframe pass gives for a window with no keyframe: the next keyframe after it, exit 0, GPU and CPU)**, `compare_rules.py`, `adjudicated.json` (truth fixed by frame checks), `framechecks/` (contact sheets), `framecheck.py`, `gpu_bench.py` (CPU vs CUDA text detection), `gpu/` (**cross-vendor GPU: WebGPU/Vulkan in the app image, ncnn attempt — see `gpu/RESULTS.md`**), **`phase3/`** (phase-3 measurement scripts: `measure_packages.sh`, `measure_combinations.py`, `measure_cost.py`, `measure_hdr.py`, `measure_webgpu_devices.py`), **`phase3-measurements.md`** (Task 1's pre-build numbers: package sizes, model hashes, WebGPU device selection, decode/detect cost, HDR kinds; local-only `bench/` holds the raw outputs, model and Python 3.12 venv, `$MARKERS_BENCH_DIR`) |
| `design/` | Design report source (artifact https://claude.ai/code/artifact/65394c1a-e878-4fc2-985b-63bc4c307c5d) | `index.html`, `before_*.jpg`, `shot.py`; `phase4/` (the phase-4 mockup pack: `index.html`, `ui-copy.md` = the owner-approved wording the editor and Setup Health copy is taken from, `README.md`) |
| `eval/` (phase 4) | AniSkip and anime chapter measurements | `aniskip-facts.md` (AniSkip measured and not taken; the rule 8 independence test), `phase4-chapters.md` (`Ending` and lone generic `Intro` chapters); the scripts behind the first are in `online/phase4/` |
| `lab/` (phase 4) | The Plex marker agent's lab row | `phase4-row12-agent.md`, `phase4_row12_agent.py`, `phase4_row12_up.sh` (two containers: the app without Plex's config volume, the agent with it) |
| `history/` | Superseded spec revisions and old report copies | `spec-rev2-2026-09-13.md` |
| `screenshots/` | UI and lab screenshots cited by the results files | `phase1/`, `phase2/` (e.g. `task10-r3-*.png` Emby versions in the web player), `final-review/` (the owner's final-review changes of 2026-09-19; `capture.py` retakes them on the real app with the e2e fixtures' synthetic data) |

## Lab servers

```bash
cd docs/design/intro-credits/evidence/lab
./up.sh              # create missing containers (state in docker volumes mlab_*)
./up.sh recreate     # recreate all, volumes kept
set -a; . ./env; set +a
```

| Server | URL | Notes |
|---|---|---|
| Emby 4.10 | http://127.0.0.1:18096 | `emby/embyserver:4.10.0.40`; Media Preview Bridge for Emby (4.10 build) installed. The prototype `MarkersLabEmby` was removed (backup `lab/synth/_backup/`); its old markers stay on Rick and Morty S01 + Synth Show |
| Emby 4.9 | http://127.0.0.1:18099 | `emby/embyserver:4.9.1.90`; Media Preview Bridge for Emby (4.9 build); Synth Chapters library; `EMBY49_TOKEN` / `EMBY49_UID` |
| Jellyfin 10.11 | http://127.0.0.1:18097 | Lab plugin `MarkersLab` installed; `JF_ITEM` has Intro + Outro segments |
| Jellyfin 12.0 | http://127.0.0.1:18098 | net10 build of the lab plugin; `JF12_ITEM` |
| Plex (latest) | http://127.0.0.1:32402 | **Unclaimed → no Plex Pass → markers not served.** For Plex end-to-end tests ask the owner for a https://plex.tv/claim token (valid 4 min), then `PLEX_CLAIM=… ./up.sh recreate`; remove from the owner's account afterwards |

Never test on the prod Plex on `plex`. Prod Plex DB is read-only: `sqlite3 "file:<db>?mode=ro"` over ssh.

## Tools

Resource rule: one heavy job at a time, `nice -n 19`, thread caps, GPU where it helps (owner, 2026-09-13).

```bash
# CPU text detector venv (what the app would ship)
uv venv -p 3.12 ocrvenv && VIRTUAL_ENV=$PWD/ocrvenv uv pip install rapidocr_onnxruntime==1.4.4 pillow numpy

# GPU text detector venv (lab only; Quadro P5000 = Pascal 6.1)
uv venv -p 3.12 ocrgpu
VIRTUAL_ENV=$PWD/ocrgpu uv pip install rapidocr_onnxruntime==1.4.4 pillow
VIRTUAL_ENV=$PWD/ocrgpu uv pip uninstall onnxruntime            # rapidocr pulls the CPU build; it shadows the GPU one
VIRTUAL_ENV=$PWD/ocrgpu uv pip install "onnxruntime-gpu[cuda,cudnn]==1.22.0" "nvidia-cudnn-cu12==9.5.1.17"
# onnxruntime-gpu 1.30 = CUDA 13 (no Pascal); cuDNN 9.26 fails on Pascal with CUDNN_BACKEND_API_FAILED.
# Call onnxruntime.preload_dlls() before creating sessions.

# Chromaprint: storage's /usr/bin/ffmpeg has it; in the app image only /usr/lib/jellyfin-ffmpeg/ffmpeg does.
# Plugin builds: docker run --rm -v "$PWD":/src -w /src mcr.microsoft.com/dotnet/sdk:9.0 dotnet build -c Release
#   (Jellyfin 12.0 build: sdk:10.0)
```
