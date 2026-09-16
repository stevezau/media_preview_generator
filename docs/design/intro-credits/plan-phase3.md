# Intro & Credits — Phase 3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Files whose credits no chapter or online source settles get a Skip Credits start read from the on-screen
credit roll in the file's ending (rule J), and an end at the roll's last credit frame when a scene follows it (owner,
Q3), detected on the worker's GPU (any vendor, through ONNX Runtime WebGPU) or on the CPU, stored as `credits_text`
evidence and combined with the other sources under the §5.5 rules; Settings shows whether it can run here, and the
harness proves the numbers against spec §5.4 and against Plex's own credits markers.

**Architecture:** A new `markers/credits/` package. `rule_j.py` is a line-for-line port of the measured prototype.
`frames.py` decodes the keyframes of the tail and 1 fps windows around the coarse answer with ffmpeg, using the same
hwaccel arguments as previews (moved into `processing/hwaccel.py`, preview command lines proven unchanged by a golden
test). `textdet.py` runs the PP-OCRv4 detection model on ONNX Runtime with rapidocr_onnxruntime 1.4.4's DBNet pre- and
post-processing vendored. `textdet_helper.py` owns one helper subprocess per GPU device and one CPU helper; a GPU
helper uses the WebGPU plugin EP only after a 20-frame self-test gives the same boxes faster than the CPU. `detector.py`
registers a `LocalDetectorSpec` for `Source.CREDITS_TEXT` that always runs on a worker, versioned by
`CREDITS_TEXT_VERSION`, with a three-state availability check like chromaprint's and a one-day wait after a decode
timeout. The web process never imports
onnxruntime or OpenCV. The Docker image pins the new wheels and fetches the model with sha256 checks.
`tools/markers_eval` gains `credits-text` and a text detection bench.

**Tech Stack:** Python 3.12 (image) / 3.14 (dev venv), numpy, onnxruntime 1.30.0 (CPU EP), onnxruntime-ep-webgpu
0.3.0 (Linux x86_64 only; Dawn → Vulkan), opencv-python-headless 5.0.0.93, pyclipper 1.4.0, ffmpeg 8.1 (CUDA / VAAPI /
software decode), SQLite, Flask, Bootstrap 5 + vanilla JS, pytest, Playwright.

**Spec:** `docs/design/intro-credits/spec.md` (Revision 3) — read §0 first, then §5.4, §5.5 (rules 2–7, 11), §5.6,
§6.2 step 3, §6.4 items 4, 7, 10, §7.2–§7.3, §10, §12 phase 3, §13 item 7, §14 (the 2026-09-13 lines on credits and
text detection, 2026-09-14 "Local detectors", 2026-09-15 "Phase 2 audit, detection" for the chromaprint tri-state).
Roadmap and its Global Constraints: `docs/design/intro-credits/plan-roadmap.md` ("Phase 3 — Credits text detection").
Evidence: `evidence/credits/` (`eval_rules3.py`, `rule_j.py`, `features3.py`, `gpu/RESULTS.md`, `gpu/bench.py`,
`gpu/extract.py`; `f3.jsonl`, `adjudicated.json`, `movies40.json`, `tv40.json`, `movie_credit_truth.json` are
local-only). `plan-phase2.md` is history; where its code blocks disagree with the branch, the branch and spec §14 win.
Phase-2 ledger: `.superpowers/sdd/plan-phase2/progress.md`.

## Global Constraints

Every task implicitly includes these. The roadmap's Global Constraints still bind; the lines that matter in phase 3
are repeated so an implementer who sees only one task has them.

**Carried over (owner rules, spec §0, §1, §5.6 and the roadmap):**
- **Precision over coverage:** a missing marker is acceptable; a wrong one is not. Credits text is one source: under
  the default publish rule (`publish_when = "high"`) it needs a second independent source (spec §5.4, §5.5 rule 4:
  credits/preview agree on their **start within 10 s**). Server markers never decide alone and never supply times
  (they may shorten a skip, rule 7). A locked marker always wins. Results never depend on input order.
- Feature is **off until turned on per server**; nothing is detected for a file with no enabled owner.
- File identity = canonical path + size + mtime. A change invalidates evidence (credits text answers included) and
  unlocked markers. All marker times are integer **milliseconds**.
- **Lab on storage only** (`docs/design/intro-credits/evidence/lab/`: `mlab-plex`, `mlab-jellyfin`, `mlab-jf12`,
  `mlab-emby`, `mlab-emby49`, `mlab-app`), **never the prod Plex on `plex`**. The prod Plex DB is read-only
  (`sqlite3 "file:<db>?mode=ro"` over ssh). Anything that runs on the `plex` host needs the owner's OK first. The one
  OK given (owner, 2026-09-16, Q7): a throwaway container for Task 13 rows 12–15 only — synthetic movie, no `/data*` or
  Plex config mounts, no prod Plex access, removed right after.
- **Never delete or write files under `/data*`** (the owner's library, both hosts). Lab mounts are `:ro`; the harness
  and bench only `stat`/`ffprobe`/`ffmpeg`-read real files and write caches under `~/.cache/markers_eval` or
  `$MARKERS_BENCH_DIR`.
- **No real library paths, file names or release groups in committed files.** Committed summaries hold counts and
  show/movie names at most; fixtures are anonymised (`movie-01`, `tv-01`). Truth sets, per-file JSON, bench frames and
  frame-check sheets stay local (git-ignored).
- **Tokens are never logged** (Plex, Jellyfin, Emby, `evidence/lab/env`); lab scripts scrub them with
  `phase1_matrix.scrub`. Never commit `evidence/lab/env` or any other secret.
- **Resource rule:** storage is shared. One heavy job at a time, `nice -n 19` for harness, bench and lab encodes.
  Marker items run on the **existing** WorkerPool; ffmpeg `-threads 2`; ONNX Runtime `intra_op_num_threads=2`,
  `inter_op_num_threads=1`; never parallel per-frame seeks or full-rate decode of the tail (spec §5.4, §5.6).
- Tests follow `.claude/rules/testing.md`: mock FFmpeg/HTTP/filesystem/subprocesses in unit tests, assert the argv and
  kwargs the code controls (not call counts), cover every cell of a branchy matrix. Tests needing real ffmpeg, the
  model, a GPU or real media are marked `integration` (or `gpu`) and never run in default CI.
- Code style: ruff (line 120), type hints everywhere, Google docstrings on public APIs, `from loguru import logger`,
  comments explain *why* only. Implementers run `pre-commit run --files <staged files>` before reporting.
- **UI checkpoint (owner):** new visible wording or layout → screenshot to the owner **before** the commit. The copy
  this phase adds is listed under "UI copy" below so the owner can confirm it with the plan review; the screenshots
  still go to the owner from Task 10 and Task 13.
- Git: branch `feat/markers-detection` (PR #241 → `dev`). Conventional Commits. **Before every commit run the
  `Architecture Review` agent** (`.claude/agents/architecture-review.md`) on the staged diff; HIGH blocks, MED is
  discussed. Commit with `PATH="/home/data/.venv/bin:$PATH" git commit`. Never commit to `dev`/`main`. **Nothing
  merges into `dev` until the owner says it is fully tested.** Releases only on the owner's explicit word "release".
- Update the spec (not just code) whenever a decision changes; add a dated line to spec §14.
- Test commands (storage): `/home/data/.venv/bin/python -m pytest --no-cov <files>` for a task;
  `/home/data/.venv/bin/python -m pytest` (full) before each push; `/home/data/.venv/bin/python -m pytest -m e2e -n 8
  --no-cov` for UI tasks (never `-n auto`). In a worktree lane: `PYTHONPATH=<worktree> … -n 4`, with
  `MARKERS_EVAL_EVIDENCE` and `MARKERS_BENCH_DIR` pointing at the main checkout (below).
- **Stable local paths, never a session scratchpad** (lanes, subagents and a task after `/clear` don't share one). The
  shared venv is `/home/data/.venv`. Local-only phase-3 artifacts live in the main checkout's git-ignored
  `docs/design/intro-credits/evidence/credits/bench/` (Task 1 adds the ignore rule), exported as
  `MARKERS_BENCH_DIR=/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/credits/bench`:
  `py312/` (Task 1's Python 3.12 venv with rapidocr_onnxruntime 1.4.4), `textdet-model/` (the wheel and
  `ch_PP-OCRv4_det_infer.onnx`), `frames.npy` and `counts/` (Task 4's bench), `m3-*.txt` / `m4-*.jsonl` (Task 1),
  `logs/` (image builds). Owner-bound screenshots and the PR body draft go to `.superpowers/sdd/plan-phase3/` (git
  excluded). Lane worktrees live under `/home/data/workspace/p3-lanes/` (outside the checkout). The Docker image never
  reads these: it downloads the model with both sha256 checks (Task 7).

**Phase-3 additions:**
- New runtime dependencies, pinned exactly (`pyproject.toml`, Task 4): `onnxruntime==1.30.0`,
  `opencv-python-headless==5.0.0.93`, `pyclipper==1.4.0`, and `onnxruntime-ep-webgpu==0.3.0; platform_machine ==
  "x86_64"` (PyPI has no Linux aarch64 wheel: the arm64 image detects text on the CPU only). Test extra:
  `shapely==2.1.2` (the verbatim rapidocr reference only).
- **The web process never imports `onnxruntime`, `onnxruntime_ep_webgpu`, `cv2` or `pyclipper`.** Only
  `markers/credits/textdet.py` imports them, and only the helper process, the harness, the bench and tests import
  `textdet.py`. A unit test (Task 8) imports the pipeline and the web app and asserts none of these modules is loaded.
- Model: `ch_PP-OCRv4_det_infer.onnx`, 4,745,517 bytes, sha256
  `d2a7720d45a54257208b1e13e36a8479894cb74155a5efe29462512d42f49da9`, taken from the rapidocr_onnxruntime 1.4.4 wheel
  (sha256 `971d7d5f223a7a808662229df1ef69893809d8457d834e6373d3854bc1782cbf`, 14,915,192 bytes, Apache-2.0). Image path
  `/app/models/ch_PP-OCRv4_det_infer.onnx`; `MEDIA_PREVIEW_TEXTDET_MODEL` overrides it for dev and harness runs.
- **Equivalence gates before use:** the vendored detector must give identical box counts to rapidocr_onnxruntime 1.4.4
  on the 289-frame bench set (Task 4 Step 9), and the rule J port must reproduce the prototype item for item and spec
  §5.4's 59 within 10 s / 1 early on the anonymised 80-file fixture (Task 3).
- **GPU text detection only after a self-test:** the helper uses WebGPU only when `get_vulkan_device_info()` reports a
  hardware device and 20 synthetic frames give exactly the CPU's box counts faster than the CPU; otherwise CPU. The
  result is kept per device for the process lifetime. Measured: storage P5000 13.3 vs 18.7 ms; plex TITAN RTX 4.8 vs
  7.7 ms; plex Intel UHD 770 16.1 vs 8.0 ms (→ CPU). AMD is untested (no hardware).
- Decode failures on a GPU raise `CodecNotSupportedError` so the worker reruns the item on the CPU (spec §6.4 item 4);
  a GPU text detection helper that crashes (during a request or between requests), hangs or fails its start moves that
  device to the CPU helper for the process lifetime, with one WARNING (spec §6.4 item 7).
- **Nothing blocks a worker past its time limit.** ffmpeg and the helpers run in their own session; on a timeout,
  cancel or failure the whole process group is killed with a bounded wait, and pipes a stuck process still holds are
  handed to a daemon reaper instead of being closed on the worker thread (preflight I1, reproduced: a held pipe kept
  `run_decode(timeout_s=2)` for 25 s). A file whose decode timed out isn't decoded again for a day unless it changes or
  the run is forced (`credits_text_timeouts`, like phase 2's `member_fingerprint_failures`).

**Technical rulings taken while planning (each with its cost if wrong):**
- T-R1 · The helper module is `media_preview_generator.markers.credits.textdet_helper` (roadmap name), not spec §6.4's
  `markers.textdet`. Task 12 aligns the spec. Cost if wrong: a rename.
- T-R2 · Post-processing keeps **pyclipper** for the unclip step and replaces only shapely's area/length with direct
  formulas. The roadmap's "unclip computed analytically" can't match pyclipper's integer rounding and arc points
  exactly, and identical box counts are the gate. If the fuzz equality test in Task 4 finds any box that differs,
  `shapely==2.1.2` becomes a runtime dependency (+≈10 MB) and computes area/length as rapidocr does. Cost if wrong:
  1 MB (or 10 MB) of image.
- T-R3 · A GPU helper runs on the WebGPU EP device whose `pci_bus_id` is the worker GPU's; with no PCI match it uses
  the CPU, except on a host with a single WebGPU device when the worker's PCI address is unknown. The EP lists every
  display PCI device from sysfs, not only Vulkan-capable ones (storage lists its ASPEED BMC VGA beside the P5000), so
  "several devices → CPU" would disable the GPU on ordinary servers. Whether Dawn then runs on that device on a host
  with two real GPUs is proven by Task 13 row 14 (plex host, Q7's throwaway container). NVIDIA helpers are safe regardless: their
  Vulkan overrides load only NVIDIA's ICD. Cost if wrong: an Intel or AMD worker's text detection may run on another
  GPU of the host (resource use only; the self-test still demands the CPU's boxes).
- T-R4 · Tail length: an episode (the file's record has a `season_key`) reads the last **450 s**; every other file
  (movies and files of unknown kind) reads the last **900 s**. Cost if wrong: extra decode for unknown-kind TV files.
- T-R5 · Keyframe rows are **not sorted**: rule J reads them in ffmpeg's output order, as measured. Dropping
  non-increasing rows (284 of 20,829) changed one answer (59 → 58 within 10 s). Cost if wrong: none measured.
- T-R6 · Mean luma is rounded to 0.1 and pts to 0.001 before rule J, as the prototype recorded them (rule J compares
  luma against 30 and 12). `-copyts` keeps a file's own start time in `pts_time` while `-ss` seeks from the start of
  the file, so Task 6 subtracts the container's `format.start_time` from every row. `decode_rows` probes the file
  itself for it; a caller passes `start_time_s` **only** when it holds a probe taken this run. `pipeline._attempt`
  (pipeline.py:1456-1461) leaves `probe` None for an unchanged file with a stored `duration_ms`, and the store never
  keeps a start time, so Task 8 must not pass 0.0 (or `probe.start_time_ms`) on that path: that is exactly the re-run
  path, and it would put every recording's answers back out by the container's offset. This is not a small late shift: a measured MPEG-TS recording with a PCR base reports `pts_time` 30000 s into a
  40 s file, which would put the tail window, rule J and the published marker completely out of range. Files that
  start at 0 (what the prototype measured) are unchanged. Cost if wrong: recordings get nonsense credits answers.
- T-R7 · A GPU decode that exits non-zero or yields no frames is a GPU failure (`GpuDecodeError` → worker CPU rerun);
  a decode timeout (600 s, on either worker: `DecodeTimeoutError`) is "no answer" and waits a day (Global Constraints);
  a CPU decode failure is "no answer this time" (`DetectorUnavailableError`, asked again next run); a CPU decode that
  yields no frames stores "nothing found". Cost if wrong: a file that fails every CPU decode (not by timing out) takes
  a worker slot on each run while its credits stay undecided.
- T-R8 · Text detection helpers exit after 10 minutes without requests (exit code 75) and are started again on
  demand without a new self-test. A helper within 5 s of its idle exit is replaced before a request instead of racing
  its timer, and an exit code is read with a 2 s wait so an idle exit is never taken for a crash. Cost if wrong: a
  ≈1–2 s helper start after an idle gap.
- T-R9 · The detector ignores `pause_check` like season audio: a paused job doesn't block a worker, and one file's
  decode is bounded (spec §6.4 item 8 pauses the job between files). Cost if wrong: a paused job finishes the file in
  progress (≈10–30 s).

## Measured while planning (2026-09-16, storage)

Numbers the plan relies on. Task 1 copies them into `evidence/credits/phase3-measurements.md` with how they were
produced.

- **Rule J reproduction.** `evidence/credits/rule_j.py`: keyframes of the tail 59 within 10 s / 66 within 30 s / 1 early
  / 9 late / 4 none of 80 (refine span 10 s, the prototype's keyframe mode). With a refine span of 15 s or 20 s (spec
  §5.4 says 20 s): **59 / 1 early / 8 late / 4 none**. Two TV answers start 10–30 s early (−21 s, −16 s); the one
  early movie is −34 s.
- **Keyframe rows:** median 232 frames per file (max 682); median keyframe gap 2.0 s, largest 83 s; rows are not always
  increasing (T-R5).
- **Credits text and Plex on the 80 files** (evidence rows through the branch's `decide()`, chapters left out because
  they are the truth; verdicts as the phase-2 harness: wrong = starts > 10 s early, late = > 30 s late):

  | Row | Useful | Late | Wrong | Missed |
  |---|---|---|---|---|
  | Plex's first credits marker | 47 | 1 | 13 | 19 |
  | Credits text alone | 65 | 8 | 3 | 4 |
  | Credits text + Plex markers, High | 39 | 0 | 1 | 40 |
  | Credits text + Plex markers, Medium (text may decide alone, as the code allows today) | 56 | 0 | 1 | 23 |
  | Credits text + Plex markers, Medium (text never alone) | 39 | 0 | 1 | 40 |

  The one wrong at High is a TV episode whose credits run 23 s: text −21 s and Plex −30 s agree.
- **Credits end.** The last credit run ends within 30 s of the end of the file on 72 of 76 files with a run (median
  4.5 s, max 171 s). Under Q3 the other 4 get an end: two with a right start keep 31 s and 42 s after the roll, one TV
  answer 13 s late keeps 59 s, and one wrong run (243 s late) keeps 171 s. On a generated movie (420 s gradients, 120 s
  of scrolling names, 60 s scene) the app's detector gave start 421.0 s and end 539.0 s (the last credit keyframe was
  537.7 s); without the scene, end None.
- **Epilogue cards** (prototype `detect` and the port agree): 10 s of white-on-black cards touching the roll, or joined
  to it over 30 s of black, become the start (90 s instead of 100 s / 130 s); split from the roll by a 30 s lit scene
  they don't. Pinned in Task 3; Task 11 frame-checks every answer shaped like this.
- **Preflight reproductions** (plan code, 2026-09-16): `frames.run_decode` with a 64-frame chunk taking 1.15 s lost its
  end marker on tails of 192, 256 and 320 keyframes and reported a 600 s timeout (fixed in Task 6: 8 new cells fail on
  the old code, pass on the new); a grandchild holding stdout kept `run_decode(timeout_s=2)` for 25 s (fixed: returns in
  ≈4 s); a GPU helper that exited between requests was restarted on the GPU silently (fixed in Task 5).
- **Q4 gate on today's numbers** (the table above, 80 files; caps rounded up): Medium useful 56 ≥ Plex 47, Medium
  wrong 1 ≤ cap 2 and ≤ Plex 13, High wrong 1 ≤ cap 1 and ≤ Plex 13 → **pass**. The one wrong answer is the 23 s TV
  credits above. The 205-movie numbers come from Task 11.
- **Tail lengths.** Lab scale run chapter truth: TV credits (400 episodes) median 72 s, p95 267 s, 390 within 450 s (the
  10 beyond are 462–463 s and chapter mislabels at 1,365–2,578 s); movies (102) p95 563 s, max 852 s, all within 900 s.
- **Packages (PyPI, 2026-09-16).** onnxruntime 1.30.0: cp312 x86_64 23.6 MB / aarch64 21.3 MB wheels, cp314 wheels
  exist. onnxruntime-ep-webgpu 0.3.0: only `manylinux_2_28_x86_64` (6.6 MB wheel, 16 MB installed), needs onnxruntime
  ≥ 1.24.4. opencv-python-headless 5.0.0.93: abi3 manylinux_2_28 x86_64 61.2 MB / aarch64 39.6 MB. pyclipper 1.4.0:
  1.0 MB, cp312/cp314. The 2026-09-13 bench ran onnxruntime 1.30.0, the EP 0.3.0, opencv-python-headless 5.0.0.93,
  pyclipper 1.4.0, shapely 2.1.2, numpy 2.5.3.
- **rapidocr 1.4.4 pre-processing:** with `limit_type="max"` its `TextDetector.get_preprocess` raises the limit to 960
  for frames under 960 px, so a 320×180 frame keeps ratio 1.0 and is resized to **320×192** (multiples of 32). Session
  options: `log_severity_level=4`, `enable_cpu_mem_arena=False`, `ORT_ENABLE_ALL`, CPU provider
  `arena_extend_strategy=kSameAsRequested`.
- **Bench set:** `evidence/credits/gpu/extract.py` = `f3.jsonl[::10]` (8 files), CPU keyframes of the 120 s around the
  truth, `scale=320:180,format=gray` → 289 frames. Neither `frames.npy` nor the model survives anywhere durable (only
  an old session's `/tmp` scratchpad), so Task 4 regenerates the frames from the local truth list.
- **Storage GPU:** `nvidia-smi --query-gpu=index,name,pci.bus_id` → `0, Quadro P5000, 00000000:02:00.0`. ffmpeg on
  storage has `libsvtav1` (Pascal has no AV1 NVDEC: Task 13's GPU-decode-failure row uses an AV1 synthetic file).
- **WebGPU EP devices on the storage host** (onnxruntime 1.30.0 + onnxruntime-ep-webgpu 0.3.0, Python 3.14 venv):
  `WebGpuExecutionProvider` lists two GPU devices — vendor `0x10de` device `0x1bb0` with metadata
  `{"Discrete": "1", "card_idx": "0", "pci_bus_id": "0000:02:00.0"}` (the P5000) and vendor `0x1a03` device `0x2000`
  with `{"card_idx": "1", "pci_bus_id": "0000:07:00.0"}` (the board's ASPEED BMC VGA). The PCI metadata key is
  `pci_bus_id`, already normalised. Task 1 M3 repeats this inside the app image.
- **Revision dry runs after the pre-flight** (2026-09-16, scratch copy of the branch, nothing committed): Task 3 34
  cells and the fixture's end count; Task 5 34 helper and 29 device tests, and the real-model integration cells with
  `EXPECT_WEBGPU=1` on the P5000; Task 6 28 unit tests (the 8 new ones fail on the earlier code) and the 3 real-ffmpeg
  cells (CPU and CUDA); Task 8 detector 17, store 3, pipeline 31, the whole markers suite 3,747, and both generated
  clips on the CPU helper; Task 9 41 cells; Task 11 the `tests/markers_eval` folder, 85.
- **Planning dry runs** (scratch copies, nothing committed): Task 2's golden capture against the current runner (462
  cases); Task 3's port and fixture converter (59 / 1 / 8 / 4, 159 KB); Task 4's detector against rapidocr's verbatim
  post-processing (0 of 2,000 maps differ; area/length equal shapely on 20,000 boxes; 17.2 ms/frame on the CPU);
  Task 9's matrix against the branch's `decide.py` (31 passed); Task 5's device, helper and pool tests (56 passed) and
  its real-model integration tests on the storage host, where the GPU helper picked the P5000 by `pci_bus_id` and its
  self-test on the 20 synthetic frames gave **GPU 16.9 vs CPU 18.7 ms per frame** (WebGPU kept; a thin margin, as spec
  §5.4's "honest gain" says).

## Open questions for the owner — answered 2026-09-16

Each question keeps what the plan asked, then the owner's decision. The tasks that pin a decision carry the same dated
paragraph.

- **Q1 · Credits text alone at "Medium"?** Spec §5.5 rule 6 lists only chapters and SkipDB as sources that may decide
  alone, but §5.4 says credits text "needs a second source" only "under the default publish rule", and `decide.py`
  lets it decide alone at Medium today. Measured on 80: alone 65 useful / 3 wrong (>10 s early); Medium with Plex 56 / 1
  against Plex's 47 / 13; "never alone" makes Medium equal High (39 / 1).
  **OWNER DECISION (2026-09-16): yes at Medium** (High needs agreement). Pinned in Task 9 (matrix, `decide.py` comments,
  spec §5.5 rule 6 and §14 in Task 9's commit) and Task 8 (`test_medium_publishes_it_alone_q1`).
- **Q2 · Credits text + a server's own credits marker = two agreeing sources?** Plex also detects credits from the
  picture, like ruling G3 for audio; a "G3 for credits" would leave text + Plex unable to decide (High 0 useful from
  that pair). **OWNER DECISION (2026-09-16): yes, independent** (rule 7 still shortens). Pinned in Task 9; Task 11
  reports how many decisions rest on that pair alone.
- **Q3 · End of a credits text candidate.** Rule J defines only the start; the planner recommended no end.
  **OWNER DECISION (2026-09-16), changed from the recommendation: a credits text candidate ends at the last credit frame
  of the chosen run (refined like the start, at the last contiguous credit frame) when that run ends more than 30 s
  before the end of the file; otherwise it runs to the end of the file (no end).** Measured: 4 of the 76 runs end more
  than 30 s early. In a cluster the earliest end wins (rule 4's safer other edge), so a text end shortens an agreeing
  SkipDB answer or a server's final credits; a chapter decision keeps the chapter's end. Emby is unchanged (R1: it gets
  the start and "Emby skips to the end of the file"). Pinned in Tasks 3 (`rule_j.credits_end`), 8 (detector end
  window, candidate end, pipeline and Emby tests), 9 (`TestEndQ3`), 10 (Inspector lane and copy), 11 (end counts and
  sheets), 12 (docs), 13 (lab rows 2 and 16).
- **Q4 · What "combined credits decisions beat Plex's native credits markers" means.**
  **OWNER DECISION (2026-09-16): precision first, never looser than Plex.** On each set, the 80 (movies40 + tv40) and
  the 205 movies, chapters left out (they are the truth), "wrong" = a start more than 10 s early:
  - Medium wrong ≤ 2 % of the set's files, rounded up, and ≤ Plex's own wrong on that set: **80 → ≤ 2; 205 → ≤ 5**.
  - High wrong ≤ 1 %, rounded up, and ≤ Plex's wrong: **80 → ≤ 1; 205 → ≤ 3**.
  - Medium useful ≥ Plex useful on that set.
  - Rule J alone on the 80: ≥ 59 within 10 s and ≤ 1 early (> 30 s).
  **Controller ruling (2026-09-16): caps round up** (`math.ceil`), so a 1 % cap on 80 files allows 1, not 0 — rounding
  down would fail a set by construction on a single answer both sources place early; the caps stay far stricter than
  Plex's 13. Measured today on the 80: every check passes (see "Measured while planning"). Pinned in Task 11
  (`gate_checks`, `wrong_cap`); a failing check stops Task 11 and goes to the owner with the numbers.
- **Q5 · Rule tuning.** **OWNER DECISION (2026-09-16, recommended default adopted): ship rule J as measured.** Task 11
  writes frame-check sheets for every answer more than 10 s early (10–30 s included), more than 30 s late, shaped like
  epilogue cards, or with an end, on both sets, and records adjudications; tuning is a follow-up that must beat J on
  both sets with no more early answers (and bumps `CREDITS_TEXT_VERSION`).
- **Q6 · AMD GPUs.** **OWNER DECISION (2026-09-16, recommended default adopted): the self-test decides** (it demands
  identical box counts as well as speed, so a wrong RADV result falls back to the CPU).
- **Q7 · Plex host GPU proof.** The done-when needs the TITAN RTX and the Intel UHD 770 on `plex`, and T-R3 needs
  Dawn's device choice proven on a host with two real GPUs.
  **OWNER DECISION (2026-09-16): a throwaway container on `plex` is allowed for Task 13 rows 12–15 only** — synthetic
  movie, no `/data*` or Plex config mounts, no prod Plex access, removed right after. Nothing else runs on `plex`.
- **Q8 · Image size budget.** **OWNER DECISION (2026-09-16, recommended default adopted): ≤ +250 MB uncompressed over
  the image built from the commit before Task 7**, measured in Task 7. Over budget, Task 7 stops and reports the
  numbers; it never raises the budget.

## Spec contradictions found while planning

- C1 · §5.5 rule 6's list of lone deciders (chapters, SkipDB for intros/recaps) vs §5.4 "under the default publish rule
  it needs a second source" and `decide.py`, which lets credits text decide alone at Medium → Q1 (owner: yes).
- C2 · §5.4 (AMD: "self-test decides") vs `evidence/credits/gpu/RESULTS.md` ("default to CPU until proven") → Q6.
- C3 · §12 phase 3 includes "larger hand-checked set, tuning"; the roadmap's phase-3 bullets don't → Q5.
- C7 · §6.2 step 3 says "a chapter decision keeps asking" the later sources; the landed `_detector_pending` runs a
  local detector only for undecided types (or answers it supplied, other versions, forced runs), so credits text never
  reads a file whose credits a chapter decided. The plan follows the code (Task 8 pins it; Task 11's rows mirror it)
  and Task 12 states it in §6.2.
- C4 · §6.4 item 7 names the helper `python -m media_preview_generator.markers.textdet`; the roadmap's file map says
  `credits/textdet_helper.py` → T-R1.
- C5 · §5.4's table was measured with a 10 s refine span (late 9); §5.4's text says 20 s, which gives late 8 (within
  10 s and early are unchanged). Task 12 corrects the table note.
- C6 · §5.4 says "at the frame's own 320 px" with `det_limit_side_len=320`; in rapidocr 1.4.4 that setting is ignored
  under `limit_type="max"` (the limit becomes 960), which is why the frame keeps its own size. The effect is as the spec
  says; Task 12 adds the reason so nobody "fixes" the limit.

## UI copy (owner confirms with the plan review; screenshots follow in Tasks 10 and 13)

- Settings → Intro & Credits → "On-screen credit text": the "Coming soon" badge goes and the switch works.
  - ⓘ (Q1, Q3): "Finds where the credit roll starts from text on screen near the end of the file, and stops the skip
    at the last credit when a scene follows. Tested alone on 80 files: within 10 s on 59, more than 30 s early on 1,
    missed 4. At "High" another source has to agree; at "Medium" it can publish alone." (Task 10 takes the numbers
    from Task 11's run of the app when Task 11 lands first; otherwise these planning numbers, corrected in Task 12.)
  - Subtitle: "Reads the end of the file · GPU when that's faster, otherwise CPU · about 10–30 s per file" (the range is
    replaced by Task 1's measured CPU and GPU figures if they fall outside it).
  - Unavailable: badge "Not available" and one of these reasons: "Needs ONNX Runtime and OpenCV, which the Docker image
    includes; they aren't installed here" · "Needs the text detection model, which the Docker image includes; it isn't
    at <path>" · "The text detection model at <path> isn't the expected file" · "The text detection check didn't
    answer; it is checked again in 10 minutes".
- Settings → Intro & Credits section ⓘ (today "Found from chapters, online databases and (soon) by matching the theme
  tune across a season."): "Found from chapters, online databases, the theme tune a season's episodes share, and the
  on-screen credit roll."
- Dashboard worker row steps: "Reading the credits…", "Refining the credits start…", and "Finding where the credits
  end…" (only when more than 30 s of the file follows the roll).
- Inspector: the existing "Credit text" lane shows the bar: "1:55:12 →" when it runs to the end, "1:55:12–1:59:40" when
  a scene follows; an empty answer shows "Nothing found".

## Execution model

- **Lanes (owner: parallel worktree lanes).** `[lane-parallel]` tasks share no files with the other tasks of their
  wave (see the conflict table) and run in git worktrees under `/home/data/workspace/p3-lanes/` with
  `PYTHONPATH=<worktree>`, `MARKERS_EVAL_EVIDENCE` and `MARKERS_BENCH_DIR` set to the main checkout's folders. The controller
  commits in the lane branch, cherry-picks onto `feat/markers-detection`, runs the full suite, pushes. `[sequential]`
  tasks start from the branch head after their dependencies landed.
- **Reviews.** One combined review per task (spec compliance + Architecture Review shapes). `[high-risk]` tasks get a
  deep adversarial review (opus; mutants, real data or lab, fuzz where it fits) and a scoped re-review only after a
  HIGH. **Milestone audit** over the whole phase-3 diff after Tasks 10 and 11 land and before Task 12 (phase-2 lesson:
  the after-Task-12 audit slipped in speed mode; this one is a task gate, not a suggestion).
- **Architecture Review agent** runs on every staged diff before its commit, in parallel with the task review.
- **Shared venv:** Task 4 installs only the new pins into `/home/data/.venv` once, never an editable install from a
  lane (that would point the shared venv at a worktree that is later removed): `uv pip install --python
  /home/data/.venv/bin/python onnxruntime==1.30.0 opencv-python-headless==5.0.0.93 pyclipper==1.4.0
  onnxruntime-ep-webgpu==0.3.0 shapely==2.1.2`. Lanes started before that don't need them (Tasks 2, 3 and 9 import
  none of them).
- **Wave 0:** before the wave-1 lanes start, the controller runs Task 1 Steps 2–3 in the main checkout (≈5 min): the
  py312 venv and the model at `$MARKERS_BENCH_DIR`, which Task 4's Steps 7–8 read.

## Task order and dependencies

| # | Task | Depends on | Wave · lane | Risk | Files (C = create, M = modify) |
|---|---|---|---|---|---|
| 1 | Pre-build measurements (Steps 2–3 first: wave 0) | — | 0 → 1 · lane-parallel | | C `evidence/credits/phase3/measure_{combinations.py,packages.sh,webgpu_devices.py,cost.py,hdr.py}`, `evidence/credits/phase3-measurements.md`; M `.gitignore`; local `$MARKERS_BENCH_DIR/{py312,textdet-model}` |
| 2 | Shared hwaccel decode args + golden preview argv | — | 1 · lane-parallel | high-risk | C `processing/hwaccel.py`, `tests/test_processing_hwaccel.py`, `tests/test_ffmpeg_runner_golden_args.py`, `tests/fixtures/ffmpeg_runner_golden_args.json`; M `processing/ffmpeg_runner.py` |
| 3 | Rule J port + anonymised 80-file fixture | — | 1 · lane-parallel | | C `markers/credits/__init__.py`, `markers/credits/rule_j.py`, `tools/markers_eval/credits_fixture.py`, `tests/fixtures/markers/credits_rule_j_80.json.gz`, `tests/markers/credits/__init__.py`, `tests/markers/credits/test_rule_j.py` |
| 4 | Vendored text detector (`textdet.py`) + bench gate | 1 Steps 2–3 (wave 0) | 1 · lane-parallel | high-risk | C `markers/credits/__init__.py`, `markers/credits/textdet.py`, `markers/credits/PP-OCRv4-det-NOTICE.txt`, `tools/markers_eval/textdet_bench.py`, `tests/markers/credits/__init__.py`, `tests/markers/credits/rapidocr_reference.py`, `tests/markers/credits/test_textdet.py`; M `pyproject.toml` |
| 9 | Decision rules for credits text (matrix) + the owner's rulings in the spec | — (owner Q1–Q3 answered) | 1 · lane-parallel | high-risk | C `tests/markers/test_decide_credits_text.py`; M `markers/decide.py` (comments), `docs/design/intro-credits/spec.md` (§5.4, §5.5 rule 6, §14) |
| 5 | Text detection helpers, self-test, availability | 1, 4 | 2 · lane-parallel | high-risk | C `markers/credits/devices.py`, `markers/credits/textdet_helper.py`, `tests/markers/credits/fake_textdet_helper.py`, `tests/markers/credits/test_devices.py`, `tests/markers/credits/test_textdet_helper.py`, `tests/markers/credits/test_textdet_helper_integration.py` |
| 6 | Frame decode (`frames.py`) | 2, 3 | 2 · lane-parallel | high-risk | C `markers/credits/frames.py`, `tests/markers/credits/test_frames.py`, `tests/markers/credits/test_frames_integration.py` |
| 7 | Docker: pinned deps, model with sha256, size budget | 1, 4, 5 | 3 · lane-parallel | | C `scripts/fetch_textdet_model.py`, `tests/test_dockerfile_textdet_model.py`; M `Dockerfile` |
| 8 | Credits text detector + pipeline integration | 3, 5, 6, 9 | 3 · lane-parallel | high-risk | C `markers/credits/detector.py`, `tests/markers/credits/test_detector.py`, `tests/markers/test_pipeline_credits_text.py`, `tests/markers/test_store_credits_text.py`; M `markers/pipeline.py`, `markers/store.py`, `tests/markers/conftest.py` (existing pipeline, season and store tests pass unchanged) |
| 10 | API + Settings row live + Inspector lane | 8 | 4 · lane-parallel | | M `web/routes/api_markers.py`, `web/templates/settings.html`, `web/static/css/pages/settings.css`, `tests/markers/test_api_markers.py`, `tests/e2e/test_intro_credits_settings.py`, `tests/e2e/test_intro_credits_inspector.py` |
| 11 | Harness: credits text vs Plex (owner's gate), bench docs | 8 | 4 · lane-parallel | | C `tools/markers_eval/credits_text.py`, `tests/markers_eval/test_credits_text.py`, `evidence/eval/phase3-harness.md`; M `tools/markers_eval/__main__.py`, `tools/markers_eval/README.md` |
| — | Milestone audit (phase-3 diff) | 10, 11 | gate | | fixes in the owning task's files |
| 12 | Docs, spec amendments | 9, 10, 11, audit (and the owner's ruling if a gate check failed) | 5 · sequential | | M `docs/design/intro-credits/spec.md`, `plan-roadmap.md`, `evidence/README.md`, `docs/reference.md`, `docs/guides.md`, `README.md`; only if Task 11's numbers differ from Task 10's copy: `web/templates/settings.html`, `tests/e2e/test_intro_credits_settings.py` |
| 13 | Phase-3 lab matrix | 7, 12 | 6 · sequential | high-risk | C `evidence/lab/synth_credits.sh`, `evidence/lab/phase3_matrix.py`, `evidence/lab/plex_rows.py`, `evidence/lab/phase3-results.md`; M `evidence/lab/app.sh`, `evidence/lab/up.sh`, `evidence/README.md` |
| 14 | PR, image, close-out | 13 | 7 · sequential | | M `spec.md` §0/§12/§14, `plan-roadmap.md`, `evidence/lab/phase3-results.md`, `.superpowers/sdd/plan-phase3/progress.md`; PR body (REST) |

All paths under `markers/`, `processing/`, `web/` are inside `media_preview_generator/`; `evidence/` is
`docs/design/intro-credits/evidence/`.

**Waves.** Wave 0: Task 1 Steps 2–3 in the main checkout (the py312 venv and the model at `$MARKERS_BENCH_DIR`,
≈5 min). Wave 1: Task 1 (the rest), 2, 3, 4, 9 together. Wave 2: 5 (after 1 and 4) and 6 (after 2 and 3), in parallel.
Wave 3: 7 (after 1, 4 and 5) and 8 (after 3, 5, 6 and 9), in parallel (different files). Wave 4: 10 and 11 in parallel.
Then the milestone audit, 12, 13, 14 in order. Task 7 moved from wave 2 to wave 3 because its test and image check
import Task 5's helper (preflight C3): the smallest change, and it costs no time since Task 13 is the first task that
needs the image. The tasks below appear in wave order (Task 9 sits after Task 4), numbered as in this table.

## Pre-flight conflict table

| Tasks | Shared file | Resolution |
|---|---|---|
| 3, 4 (then 5, 6, 8 import it) | `media_preview_generator/markers/credits/__init__.py` | Identical content in both lanes: the one-line docstring `"""Credits from on-screen text (spec §5.4)."""`. The second cherry-pick keeps one copy. |
| 3, 4, 5, 6, 8 | `tests/markers/credits/__init__.py` | Identical empty file. |
| 2, 6 | `media_preview_generator/processing/hwaccel.py` | Created by 2, only imported by 6 (6 starts after 2 lands). |
| 4, 7 | `pyproject.toml` | Only 4 edits it; 7 reads the pins (7 starts after 4 lands). |
| 5, 7 | `markers/credits/textdet_helper.py` (imported by 7's test and in-image check) | 7 starts after 5 lands (wave 3, preflight C3). |
| 1, 4 | `$MARKERS_BENCH_DIR/py312`, `$MARKERS_BENCH_DIR/textdet-model` (local, git-ignored) | Task 1 Steps 2–3 run first (wave 0); 4 only reads them. Task 4's bench writes `frames.npy` and `counts/` there; nothing else writes those. |
| 1, any | `.gitignore` | Only 1 edits it (the bench folder rule). |
| 3, 6 | `markers/credits/rule_j.py` (`Row`, imported by 6) | 6 starts after 3 lands (wave 2). |
| 7, 8 | none (7: `Dockerfile`, `scripts/`; 8: `markers/`, `tests/markers/`) | Parallel in wave 3. |
| 8, phase-2 store tests | `markers/store.py` | Only 8 edits it (one table, two methods); `tests/markers/test_store*.py` pass unchanged. |
| 4, 11 | `tools/markers_eval/textdet_bench.py`, `tools/markers_eval/README.md` | 4 creates the bench module (own `__main__` guard, no `__main__.py` edit); only 11 edits the README and `__main__.py`. |
| 3, 11 | `tools/markers_eval/credits_fixture.py` | Created by 3; 11 documents it in the README only. |
| 5, 8, 10 | `media_preview_generator/markers/credits/textdet_helper.py` | Only 5 edits it; 8 and 10 import `TextDetState`, `text_detection_state`, `text_detection_status`, `get_textdet_pool`. |
| 8, existing phase-2 tests | `tests/markers/conftest.py`; `tests/markers/test_pipeline.py`, `tests/markers/test_pipeline_detectors.py`, `tests/markers/audio/test_season.py` read only | Only 8 edits `conftest.py` (autouse `_no_text_detection_check`); the existing tests must pass unchanged, which is Task 8's regression check (`default_local_detectors`' new `credits_text` argument defaults to `TextDetState.ABSENT`). |
| 8, 9 | none (8: `pipeline.py`; 9: `decide.py`, `spec.md`) | 8's pipeline tests rest on 9's cells (Q1 alone at Medium, Q3's earliest end); 9 lands first (8 depends on it), so 8's review reads the spec with the owner's rulings. |
| 9, 12, 14 | `docs/design/intro-credits/spec.md` | 9 (wave 1) writes §5.4's credits text paragraph, §5.5 rule 6 and the owner's §14 lines; 12 (wave 5) the other sections; 14 §0/§12. Sequential. |
| 10, 12 | `web/templates/settings.html`, `tests/e2e/test_intro_credits_settings.py` | 12 edits them only when Task 11's numbers differ from the copy 10 shipped; 12 starts after 10 lands. |
| 12, 13 | `docs/design/intro-credits/evidence/README.md` | 12 adds the `credits/phase3` and harness rows, 13 the lab rows; 13 starts after 12 lands. |
| 12, 14 | `docs/design/intro-credits/spec.md` §0, `plan-roadmap.md` | Sequential. |
| 1, 12 | `evidence/credits/phase3-measurements.md` | Created by 1; 12 only links it. |

## File map (phase 3)

```
media_preview_generator/
  processing/hwaccel.py                 # T2  hwaccel decode args per worker GPU (previews + credits frames)
  processing/ffmpeg_runner.py           # T2  uses hwaccel.py; preview argv unchanged (golden test)
  markers/
    credits/
      __init__.py                       # T3/T4 identical docstring
      rule_j.py                         # T3  rule J port (pure Python)
      textdet.py                        # T4  PP-OCRv4 det on ONNX Runtime, vendored DBNet pre/post (helper-only import)
      PP-OCRv4-det-NOTICE.txt           # T4  model and vendored code attribution (Apache-2.0)
      devices.py                        # T5  worker device → PCI bus id → WebGPU EP device
      textdet_helper.py                 # T5  helper process main, parent pool, self-test, availability tri-state
      frames.py                         # T6  keyframe tail + 1 fps refine decodes, chunked luma planes, kill + reaper
      detector.py                       # T8  find_credits (start and Q3 end), detect_credits_text, LocalDetectorSpec
    pipeline.py                         # T8  register the detector, ctx.credits_text, _decide UNKNOWN handling
    store.py                            # T8  credits_text_timeouts (a timed-out file waits a day)
    decide.py                           # T9  comments (rules unchanged: Q1–Q3 are what the code already does)
  web/routes/api_markers.py             # T10 credits_text in GET /api/markers/sources/local
  web/templates/settings.html           # T10 credit text row live
scripts/fetch_textdet_model.py          # T7  download wheel, verify both sha256, extract the model
Dockerfile                              # T7
tools/markers_eval/
  credits_fixture.py                    # T3  anonymised rule J fixture from local evidence
  textdet_bench.py                      # T4  289-frame bench: extract / counts / compare
  credits_text.py                       # T11 credits text vs Plex, the owner's gate (80, 205, online cases)
tests/ …                                # per task
docs/design/intro-credits/evidence/
  credits/phase3/, credits/phase3-measurements.md   # T1
  eval/phase3-harness.md                            # T11
  credits/bench/                                    # T1  git-ignored: py312 venv, model, bench frames, logs
  lab/synth_credits.sh, lab/phase3_matrix.py, lab/plex_rows.py, lab/phase3-results.md   # T13
```

---
## Task 1: Pre-build measurements

`[lane-parallel]` (Steps 2–3 first, as wave 0, in the main checkout) — spec §5.4 (packages, sizes, GPU guard, AMD
untested, CPU cost), §6.4 item 7 (device mapping must be proven, shutdown hang), roadmap phase 3 ("Docker: … model
downloaded at build with sha256"), preflight I3 (stable paths), I4 (wheel sizes), M6 (M3 exit timing, M4 refine cost).
No product code.

What the later tasks need measured before they are built: the exact installed sizes (Task 7's budget), the model's
download and hashes (Task 7), what the WebGPU EP reports about each device and whether a process that used it exits
promptly (Task 5), the per-file cost on CPU-only and GPU hosts (Settings copy in Task 10), and how many HDR10 / Dolby
Vision Profile 5 files the truth sets hold (Task 11 reports accuracy per kind).

**Files:**
- Create: `docs/design/intro-credits/evidence/credits/phase3/measure_combinations.py`,
  `docs/design/intro-credits/evidence/credits/phase3/measure_packages.sh`,
  `docs/design/intro-credits/evidence/credits/phase3/measure_webgpu_devices.py`,
  `docs/design/intro-credits/evidence/credits/phase3/measure_cost.py`,
  `docs/design/intro-credits/evidence/credits/phase3/measure_hdr.py`,
  `docs/design/intro-credits/evidence/credits/phase3-measurements.md`
- Modify: `.gitignore` (one line under the Intro & Credits evidence block:
  `docs/design/intro-credits/evidence/credits/bench/`)
- Test: none (evidence scripts; their outputs are the deliverable)

**Interfaces:**
- Consumes: local-only truth sets `evidence/credits/{movies40,tv40,movie_credit_truth,adjudicated}.json`,
  `evidence/credits/f3.jsonl`; the phase-2 lab image `media_preview_generator:intro-credits`; storage's P5000.
- Produces (read by Tasks 4, 5, 7, 10, 11): `$MARKERS_BENCH_DIR/py312/` and `$MARKERS_BENCH_DIR/textdet-model/`
  (Steps 2–3, before any other lane); `phase3-measurements.md` sections **M1 packages** (wheel and installed MB per
  package),
  **M2 model** (URL, both sha256, sizes), **M3 WebGPU devices** (the metadata key holding a PCI address, its format,
  accepted provider options, exit time), **M4 cost** (seconds per movie/episode: decode, detection, total; CPU 2 threads,
  CPU pinned to 2 cores, GPU), **M5 HDR kinds** (counts of SDR / HDR10 / DV profile 5 / other DV in the 80 and 205
  sets, and rule J's within-10 s / early on the 80 per kind), **M0 planning numbers** (the "Measured while planning"
  section of this plan).

- [ ] **Step 1: M0 — record the planning numbers**

Copy this plan's "Measured while planning" section into `phase3-measurements.md` under "M0 planning numbers" with the
commands that reproduced them: `cd docs/design/intro-credits/evidence && nice -n 19 /home/data/.venv/bin/python
credits/rule_j.py` (span 10) and the span/combination checks, re-run with this snippet saved as
`evidence/credits/phase3/measure_combinations.py`:

```python
"""M0: rule J with refine spans 10/15/20 s, and credits text + Plex through decide() on the 80 files (counts only)."""

import collections
import os
import sys
from pathlib import Path

EVIDENCE = Path(__file__).resolve().parents[2]
REPO = EVIDENCE.parents[3]
os.chdir(EVIDENCE)
sys.path.insert(0, str(REPO))
namespace: dict = {}
exec(open("credits/eval_rules3.py").read().split("items = load(sys.argv[1])")[0], namespace)  # noqa: S102

from media_preview_generator.markers import decide as D  # noqa: E402
from media_preview_generator.markers.models import Candidate, MarkerType, Source  # noqa: E402
from tools.markers_eval.credits import judge_credits  # noqa: E402
from tools.markers_eval.decisions import ORDER  # noqa: E402
from tools.markers_eval.plex import first_marker, load_baseline, server_candidates  # noqa: E402

RULE_J = dict(dense=3, min_boxes=1, dark=30, gap=24, run=15, pick="last", anchor=True, bridge_dark=True)
items = namespace["load"]("credits/f3.jsonl")
detect, runs = namespace["detect"], namespace["runs"]
for span in (10.0, 15.0, 20.0):
    tally = collections.Counter()
    for it in items:
        start = detect(it["key"], it["fine"], RULE_J, span)
        if start is None:
            tally["none"] += 1
            continue
        err = start - it["truth"]
        tally["within_10s"] += abs(err) <= 10
        tally["early_10_30"] += -30 <= err < -10
        if abs(err) > 30:
            tally["early" if err < 0 else "late"] += 1
    print("span", span, dict(tally))

baseline = load_baseline(EVIDENCE / "lab/results/scale/prod_plex_markers.json")
rows = {name: collections.Counter() for name in ("plex", "text", "high", "medium_alone", "medium_agree_only")}
tail_gaps = []
for it in items:
    start = detect(it["key"], it["fine"], RULE_J, 20.0)
    found = runs(it["key"], RULE_J)
    if found:
        tail_gaps.append(it["duration"] - it["key"][found[-1][1]][0])
    markers = baseline.get(it["file"], [])
    plex = first_marker(markers, MarkerType.CREDITS)
    rows["plex"][judge_credits(plex.start_ms / 1000 if plex else None, it["truth"])] += 1
    rows["text"][judge_credits(start, it["truth"])] += 1
    candidates = server_candidates(markers, MarkerType.CREDITS)
    if start is not None:
        candidates.append(Candidate(MarkerType.CREDITS, int(start * 1000), None, Source.CREDITS_TEXT))
    for name, level, alone in (("high", "high", True), ("medium_alone", "medium", True), ("medium_agree_only", "medium", False)):
        saved = D._AGREEMENT_ONLY
        if not alone:
            D._AGREEMENT_ONLY = saved | {Source.CREDITS_TEXT}
        ctx = D.DecisionContext(int(it["duration"] * 1000), it["kind"] == "movie", level, frozenset({MarkerType.CREDITS}), ORDER)
        decision = D.decide(candidates, ctx, {})[MarkerType.CREDITS]
        D._AGREEMENT_ONLY = saved
        decided = decision.marker.start_ms / 1000 if decision.status is D.DecisionStatus.DECIDED else None
        rows[name][judge_credits(decided, it["truth"])] += 1
for name, tally in rows.items():
    print(name, dict(sorted(tally.items())))
tail_gaps.sort()
print("last run end to end of file: n", len(tail_gaps), "within 30 s", sum(g <= 30 for g in tail_gaps), "max", round(tail_gaps[-1]))
```

Run: `cd /home/data/workspace/plex_generate_vid_previews && nice -n 19 /home/data/.venv/bin/python docs/design/intro-credits/evidence/credits/phase3/measure_combinations.py`
Expected: span 20 → `within_10s 59, early 1, late 8, none 4, early_10_30 2`; rows as in the plan's table; last run
within 30 s on 72 of 76. Any difference: stop and report it (the fixture and Q1/Q2 numbers rest on these).

- [ ] **Step 2 (wave 0): M1 — packages in a Python 3.12 venv (the image's Python)**

First, in the main checkout: add `docs/design/intro-credits/evidence/credits/bench/` to `.gitignore` under the
"Intro & Credits evidence: local-only files" block, then
`export MARKERS_BENCH_DIR=/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/credits/bench`
and `mkdir -p "$MARKERS_BENCH_DIR"`; `git check-ignore "$MARKERS_BENCH_DIR/x"` must print the path.

```bash
#!/bin/bash
# M1: the phase-3 wheels in a Python 3.12 venv (the app image's Python): installed sizes, and rapidocr_onnxruntime 1.4.4
# beside them for the bench reference (installed without its opencv-python dependency, which clashes with headless).
#
#   ./measure_packages.sh <venv dir>
set -euo pipefail

readonly VENV="${1:?usage: measure_packages.sh <venv dir>}"

uv venv -p 3.12 "$VENV"
uv pip install --python "$VENV/bin/python" onnxruntime==1.30.0 onnxruntime-ep-webgpu==0.3.0 \
    opencv-python-headless==5.0.0.93 pyclipper==1.4.0 numpy==2.5.3 shapely==2.1.2 six PyYAML Pillow tqdm
uv pip install --python "$VENV/bin/python" --no-deps rapidocr_onnxruntime==1.4.4
# Wheel sizes too (preflight I4): the image copies the builder's wheels in their own layer, which stays in the image.
"$VENV/bin/python" - <<'PY'
import json, urllib.request
for name, version in (("onnxruntime", "1.30.0"), ("onnxruntime-ep-webgpu", "0.3.0"),
                      ("opencv-python-headless", "5.0.0.93"), ("pyclipper", "1.4.0")):
    files = json.load(urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=30))["urls"]
    for f in files:
        wanted = "manylinux" in f["filename"] and ("cp312" in f["filename"] or "abi3" in f["filename"])
        if f["packagetype"] == "bdist_wheel" and wanted:
            print(f"wheel {f['filename']} {f['size'] / 1e6:.1f} MB")
PY
site="$("$VENV/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
for pkg in onnxruntime onnxruntime_ep_webgpu cv2 opencv_python_headless.libs pyclipper numpy numpy.libs shapely shapely.libs; do
    [[ -e "$site/$pkg" ]] && du -sm "$site/$pkg"
done
"$VENV/bin/python" -c 'import onnxruntime, cv2, pyclipper, onnxruntime_ep_webgpu; print(onnxruntime.__version__, cv2.__version__)'
```

Run: `nice -n 19 docs/design/intro-credits/evidence/credits/phase3/measure_packages.sh "$MARKERS_BENCH_DIR/py312"`
Expected: versions `1.30.0 5.0.0`; record each wheel size and each `du -sm` line in M1, and both sums over
onnxruntime + onnxruntime_ep_webgpu + cv2 + opencv_python_headless.libs + pyclipper (the image adds these; numpy is
already there since phase 2). Wheels ≈ 92 MB (PyPI listing) plus installed size is the expected image growth Task 7
checks against 250 MB.

- [ ] **Step 3 (wave 0): M2 — model download and hashes**

```bash
python3 - "$MARKERS_BENCH_DIR/textdet-model" <<'PY'
import hashlib, sys, urllib.request, zipfile, pathlib
out = pathlib.Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
url = "https://files.pythonhosted.org/packages/ba/12/1e5497183bdbe782dbb91bad1d0d2297dba4d2831b2652657f7517bfc6df/rapidocr_onnxruntime-1.4.4-py3-none-any.whl"
wheel = out / "rapidocr_onnxruntime-1.4.4-py3-none-any.whl"
urllib.request.urlretrieve(url, wheel)
print("wheel", wheel.stat().st_size, hashlib.sha256(wheel.read_bytes()).hexdigest())
with zipfile.ZipFile(wheel) as z:
    data = z.read("rapidocr_onnxruntime/models/ch_PP-OCRv4_det_infer.onnx")
    print("licence files", [n for n in z.namelist() if "LICENSE" in n.upper()])
(out / "ch_PP-OCRv4_det_infer.onnx").write_bytes(data)
print("model", len(data), hashlib.sha256(data).hexdigest())
PY
```

Expected: `wheel 14915192 971d7d5f223a7a808662229df1ef69893809d8457d834e6373d3854bc1782cbf` and `model 4745517
d2a7720d45a54257208b1e13e36a8479894cb74155a5efe29462512d42f49da9`. Record the licence file names (Task 4's notice
quotes them). If either hash differs, stop: Tasks 4 and 7 pin these values.

- [ ] **Step 4: M3 — WebGPU EP devices and exit time inside the app image**

```python
"""M3 (run inside the app image): what onnxruntime-ep-webgpu 0.3.0 lists per device, which provider options it accepts,
and how long a process that created a WebGPU session takes to exit (ORT PR #29591: shutdown hang without adapters).

    python3 measure_webgpu_devices.py <model.onnx>
"""

import json
import os
import subprocess
import sys
import time

if os.environ.get("USE_APP_VULKAN_PROBE"):
    from media_preview_generator.gpu import get_vulkan_device_info, get_vulkan_env_overrides

    print("probe:", get_vulkan_device_info(), "overrides:", get_vulkan_env_overrides(), flush=True)
    os.environ.update(get_vulkan_env_overrides())

import numpy as np
import onnxruntime as ort
import onnxruntime_ep_webgpu as webgpu_ep

MODEL = sys.argv[1]
ort.register_execution_provider_library("webgpu", webgpu_ep.get_library_path())
devices = [d for d in ort.get_ep_devices() if d.ep_name == webgpu_ep.get_ep_name()]
print("devices:", json.dumps([
    {"type": str(d.device.type), "vendor": d.device.vendor, "vendor_id": hex(d.device.vendor_id),
     "device_id": hex(d.device.device_id), "metadata": dict(d.device.metadata), "ep_metadata": dict(d.ep_metadata),
     "ep_options": dict(d.ep_options)}
    for d in devices
], indent=1), flush=True)
feed = np.zeros((1, 3, 192, 320), dtype=np.float32)
for index, device in enumerate(devices):
    for options in ({}, {"powerPreference": "high-performance"}, {"deviceId": str(index)}):
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2
        try:
            opts.add_provider_for_devices([device], options)
            session = ort.InferenceSession(MODEL, sess_options=opts)
            started = time.perf_counter()
            for _ in range(20):
                session.run(None, {session.get_inputs()[0].name: feed})
            print(f"device {index} options {options}: ok {1000 * (time.perf_counter() - started) / 20:.1f} ms/frame", flush=True)
        except Exception as exc:  # noqa: BLE001 - measuring what the EP rejects
            print(f"device {index} options {options}: {type(exc).__name__}: {exc}", flush=True)
if "--child" in sys.argv:
    print("SESSIONS_DONE", flush=True)  # the parent times the exit from here, not the sessions and runs above
else:
    child = subprocess.Popen([sys.executable, __file__, MODEL, "--child"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    for line in child.stdout:
        if line.startswith("SESSIONS_DONE"):
            break
    done = time.monotonic()
    try:
        code = child.wait(timeout=120)
        print(f"child exit {code} {time.monotonic() - done:.1f} s after its sessions were done")
    except subprocess.TimeoutExpired:
        child.kill()
        print("child still running 120 s after its sessions were done: the shutdown hang (killed)")
```

Run on storage (NVIDIA runtime, then without a GPU):

```bash
cd /home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/credits/phase3
B="$MARKERS_BENCH_DIR"
SITE="$("$B/py312/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
for gpu in with without; do
  extra=(); [[ $gpu == with ]] && extra=(--runtime=nvidia -e NVIDIA_VISIBLE_DEVICES=all -e NVIDIA_DRIVER_CAPABILITIES=all --device /dev/dri)
  nice -n 19 docker run --rm "${extra[@]}" -e USE_APP_VULKAN_PROBE=1 -e PYTHONPATH=/pkgs:/app \
    -v "$SITE":/pkgs:ro -v "$PWD":/work:ro -v "$B/textdet-model":/model:ro \
    --entrypoint python3 media_preview_generator:intro-credits /work/measure_webgpu_devices.py /model/ch_PP-OCRv4_det_infer.onnx \
    > "$B/m3-$gpu.txt" 2>&1 || true
  tail -40 "$B/m3-$gpu.txt"
done
```

Expected with the GPU: the host venv lists the P5000 (`0x10de`, `pci_bus_id` `0000:02:00.0`) and the ASPEED BMC VGA
(`0x1a03`, `0000:07:00.0`); the container should list the same (sysfs is visible). Sessions on the P5000 at ≈13
ms/frame; the BMC device's sessions fail or crawl. Record in M3: the device JSON (vendor/device ids, metadata), which
option sets fail on which device, and the child's exit time after its sessions were done, with and without a GPU (a
"still running" line is the shutdown hang: Task 5's `close()` kills after `CLOSE_GRACE_S`). If the image's EP uses another
metadata key than `pci_bus_id`, Task 5 adds it to `devices.PCI_METADATA_KEYS`.

- [ ] **Step 5: M4 — cost per file (CPU 2 threads, CPU on 2 cores, GPU)**

```python
"""M4: seconds per file for the spec §5.4 frames + text detection, three ways (prototype code paths, no app code): the
keyframe tail, then the two 1 fps windows the detector adds (21 s before the credits start, and 21 s after the roll when
a scene follows it, Q3; timed here around the truth start for every file, an upper bound).

    measure_cost.py <model.onnx> <n movies> <n episodes>     (run with the Python 3.12 venv from M1)
"""

import json
import re
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort
import onnxruntime_ep_webgpu as webgpu_ep
from rapidocr_onnxruntime import RapidOCR

EVIDENCE = Path(__file__).resolve().parents[2]
W, H = 320, 180
PTS = re.compile(rb"pts_time:\s*(-?[\d.]+)")


def decode(path: str, start: float, gpu: bool, length: float | None = None) -> list[np.ndarray]:
    vf = f"scale_cuda={W}:{H}:format=nv12,hwdownload,format=nv12" if gpu else f"scale={W}:{H},format=nv12"
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-threads", "2"]
    if gpu:
        cmd += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
    if length is None:
        cmd += ["-skip_frame", "nokey", "-ss", f"{start:.3f}"]
    else:
        cmd += ["-ss", f"{start:.3f}", "-t", f"{length:.3f}"]
        vf = f"fps=1,{vf}"
    cmd += ["-copyts", "-i", path, "-an", "-sn", "-dn",
            "-fps_mode", "passthrough", "-vf", f"{vf},showinfo", "-f", "rawvideo", "-"]
    out = subprocess.run(cmd, capture_output=True).stdout
    size = W * H * 3 // 2
    return [np.frombuffer(out, np.uint8, W * H, i * size).reshape(H, W) for i in range(len(out) // size)]


def detector(model: str, webgpu: bool):
    ocr = RapidOCR(det_limit_side_len=320, det_limit_type="max", intra_op_num_threads=2, inter_op_num_threads=1)
    if webgpu:
        ort.register_execution_provider_library("webgpu", webgpu_ep.get_library_path())
        devices = [d for d in ort.get_ep_devices() if d.ep_name == webgpu_ep.get_ep_name()]
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2
        opts.add_provider_for_devices(devices[:1], {})
        session = ort.InferenceSession(model, sess_options=opts)
        name = session.get_inputs()[0].name
        ocr.text_det.infer = lambda x: session.run(None, {name: x})
    return ocr.text_det


def main() -> None:
    model, movies, episodes = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    files = [(r, 900.0) for r in json.loads((EVIDENCE / "credits/movies40.json").read_text())[:movies]]
    files += [(r, 450.0) for r in json.loads((EVIDENCE / "credits/tv40.json").read_text())[:episodes]]
    mode = sys.argv[4] if len(sys.argv) > 4 else "cpu"
    det = detector(model, webgpu=mode == "gpu")
    for n, (row, tail) in enumerate(files, 1):
        started = time.perf_counter()
        frames = decode(row["file"], max(0.0, row["duration"] - tail), gpu=mode == "gpu")
        decoded = time.perf_counter()
        for frame in frames:
            det(np.stack([frame] * 3, axis=-1))
        done = time.perf_counter()
        refine = []
        for window_start in (row["credits_start"] - 20, row["credits_start"] + 60):
            refine += decode(row["file"], max(0.0, window_start), gpu=mode == "gpu", length=21.0)
        for frame in refine:
            det(np.stack([frame] * 3, axis=-1))
        refined = time.perf_counter()
        kind = "movie" if tail == 900.0 else "episode"
        print(json.dumps({"n": n, "kind": kind, "mode": mode, "frames": len(frames), "refine_frames": len(refine),
                          "decode_s": round(decoded - started, 1), "detect_s": round(done - decoded, 1),
                          "refine_s": round(refined - done, 1), "total_s": round(refined - started, 1)}), flush=True)


if __name__ == "__main__":
    main()
```

Run (one at a time, storage shared):

```bash
cd /home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/credits/phase3
B="$MARKERS_BENCH_DIR"; M="$B/textdet-model/ch_PP-OCRv4_det_infer.onnx"
nice -n 19 "$B/py312/bin/python" measure_cost.py "$M" 5 5 cpu | tee "$B/m4-cpu.jsonl"
nice -n 19 taskset -c 0,1 "$B/py312/bin/python" measure_cost.py "$M" 5 5 cpu | tee "$B/m4-cpu-2cores.jsonl"
nice -n 19 "$B/py312/bin/python" measure_cost.py "$M" 5 5 gpu | tee "$B/m4-gpu.jsonl"
```

Expected: GPU movie totals near spec §5.4's 8–13 s decode + detection; CPU decode of a movie tail near 26.5 s; the
refine windows add a few seconds (42 frames). Record median and max of `total_s` (and `refine_s`) per kind and mode in
M4 (no file names): Task 10's Settings range is the CPU-on-2-cores and GPU medians of `total_s`. **Gate:** if the CPU
on 2 cores takes more than 60 s for a movie, tell the owner before Task 10 (the Settings subtitle says "about 10–30 s
per file").

- [ ] **Step 6: M5 — HDR kinds in the truth sets**

```python
"""M5: SDR / HDR10 / Dolby Vision profile per file of the 80- and 205-file credits sets (ffprobe reads only), and rule J
on the 80 per kind. Prints counts only.

    measure_hdr.py        (run with /home/data/.venv/bin/python from the repo root)
"""

import collections
import json
import os
import subprocess
from pathlib import Path

EVIDENCE = Path(__file__).resolve().parents[2]


def hdr_kind(path: str) -> str:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=color_transfer:stream_side_data=dv_profile", "-of", "json", path],
        capture_output=True, text=True, timeout=60,
    ).stdout
    stream = (json.loads(out or "{}").get("streams") or [{}])[0]
    profiles = [s.get("dv_profile") for s in stream.get("side_data_list") or [] if "dv_profile" in s]
    if profiles:
        return "dv5" if 5 in profiles else "dv_other"
    return "hdr10" if stream.get("color_transfer") in ("smpte2084", "arib-std-b67") else "sdr"


def main() -> None:
    os.chdir(EVIDENCE)
    namespace: dict = {}
    exec(open("credits/eval_rules3.py").read().split("items = load(sys.argv[1])")[0], namespace)  # noqa: S102
    rule_j = dict(dense=3, min_boxes=1, dark=30, gap=24, run=15, pick="last", anchor=True, bridge_dark=True)
    by_kind = collections.defaultdict(collections.Counter)
    for item in namespace["load"]("credits/f3.jsonl"):
        kind = hdr_kind(item["file"])
        start = namespace["detect"](item["key"], item["fine"], rule_j, 20.0)
        tally = by_kind[kind]
        tally["files"] += 1
        if start is None:
            tally["none"] += 1
        else:
            tally["within_10s"] += abs(start - item["truth"]) <= 10
            tally["early"] += start - item["truth"] < -30
    print("80 files:", {k: dict(v) for k, v in sorted(by_kind.items())})
    set205 = json.loads(Path("credits/movie_credit_truth.json").read_text())
    print("205 movies:", dict(collections.Counter(hdr_kind(r["file"]) for r in set205)))


if __name__ == "__main__":
    main()
```

Run: `cd /home/data/workspace/plex_generate_vid_previews && nice -n 19 /home/data/.venv/bin/python docs/design/intro-credits/evidence/credits/phase3/measure_hdr.py`
Expected: counts per kind. Record them in M5. **If neither set holds a DV profile 5 file**, add to M5: "DV profile 5
credits text is unmeasured; the detector reads the base layer's luma like any file" (Task 12 copies the line into
spec §13).

- [ ] **Step 7: Write `phase3-measurements.md` and commit**

Sections M0–M5 as listed under Interfaces, each with the command, the date, the host (storage, P5000), and counts or
timings only. `grep -nE '/data|\.mkv|\{(tmdb|tvdb|imdb)-' docs/design/intro-credits/evidence/credits/phase3-measurements.md`
must print nothing. Stage the five scripts, the markdown and `.gitignore` (nothing under `credits/bench/` shows in
`git status`); Architecture Review on
the staged diff; then
`PATH="/home/data/.venv/bin:$PATH" git commit -m "docs(intro-credits): phase 3 pre-build measurements"`.

---
## Task 2: Shared hwaccel decode args + golden preview argv

`[lane-parallel]` `[high-risk]` — spec §5.4 ("Decode uses the app's existing per-GPU ffmpeg hwaccel selection …"),
§6.4 item 4 ("the same hwaccel argument builder as previews"), roadmap phase 3 bullet 1 ("Hwaccel args extracted from
`processing/ffmpeg_runner.py` into a shared helper without changing preview command lines (golden-args test)").

High-risk because previews are the product in production: a changed argv on any vendor/path cell breaks thumbnails.
The golden file is written from the runner **before** the move and committed with it.

**Files:**
- Create: `media_preview_generator/processing/hwaccel.py`, `tests/test_processing_hwaccel.py`,
  `tests/test_ffmpeg_runner_golden_args.py`, `tests/fixtures/ffmpeg_runner_golden_args.json`
- Modify: `media_preview_generator/processing/ffmpeg_runner.py` (`_run_ffmpeg`: the `hw_decode_active = False` block
  ~277–332, the NVIDIA branch and the `elif use_gpu and not init_vulkan:` branch; the DV5 VAAPI branch and the
  `elif use_gpu and init_vulkan:` branch stay as they are)

**Interfaces:**
- Consumes: nothing new.
- Produces (Task 6 relies on these):
```python
# media_preview_generator/processing/hwaccel.py
@dataclass(frozen=True)
class HwDecode:
    args: tuple[str, ...]      # tokens that go before -i
    active: bool               # decode runs on the GPU
def hwaccel_decode_args(gpu: str | None, gpu_device_path: str | None, *, keep_on_gpu: bool) -> HwDecode
```

- [ ] **Step 1: Write the golden capture test (against the unmodified runner)**

```python
# tests/test_ffmpeg_runner_golden_args.py
"""Preview ffmpeg command lines stay byte for byte what they were before the hwaccel arguments moved to hwaccel.py.

The golden file was written from the runner before the move (``UPDATE_GOLDEN=1``). It covers every input the
runner's argv branches on: vendor × device path × path kind × Vulkan/DV5 flags × keyframe skip, plus thread caps and
the DEBUG log level.
"""

from __future__ import annotations

import functools
import itertools
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.processing import ffmpeg_runner
from media_preview_generator.processing.filter_chain import (
    DV5_PATH_INTEL_OPENCL,
    DV5_PATH_LIBPLACEBO,
    DV5_PATH_VAAPI_VULKAN,
)

GOLDEN = Path(__file__).parent / "fixtures" / "ffmpeg_runner_golden_args.json"
DEVICES = (
    (None, None),
    ("NVIDIA", "cuda:0"),
    ("NVIDIA", "cuda:1"),
    ("NVIDIA", "cuda:"),
    ("NVIDIA", None),
    ("INTEL", "/dev/dri/renderD128"),
    ("INTEL", None),
    ("AMD", "/dev/dri/renderD129"),
    ("WINDOWS_GPU", None),
    ("APPLE", None),
    ("OTHER", "/dev/dri/renderD130"),
)
KINDS = ("sdr", "hdr10_zscale", DV5_PATH_LIBPLACEBO, DV5_PATH_VAAPI_VULKAN, DV5_PATH_INTEL_OPENCL)


def _cases() -> dict[str, dict]:
    cases: dict[str, dict] = {}
    for (gpu, device), kind, init_vulkan, use_skip, no_dv5 in itertools.product(
        DEVICES, KINDS, (False, True), (False, True), (False, True)
    ):
        cases[f"{gpu}|{device}|{kind}|vulkan={init_vulkan}|skip={use_skip}|no_vaapi_dv5={no_dv5}"] = dict(
            gpu=gpu, device=device, kind=kind, init_vulkan=init_vulkan, use_skip=use_skip, no_dv5=no_dv5,
            threads=2, log_level="INFO",
        )  # fmt: skip
    for gpu, device in DEVICES:
        for threads, level in ((0, "INFO"), (2, "DEBUG")):
            cases[f"{gpu}|{device}|sdr|threads={threads}|{level}"] = dict(
                gpu=gpu, device=device, kind="sdr", init_vulkan=False, use_skip=True, no_dv5=False,
                threads=threads, log_level=level,
            )  # fmt: skip
    return cases


CASES = _cases()


def _argv(case: dict) -> dict:
    config = SimpleNamespace(
        ffmpeg_path="/usr/bin/ffmpeg", thumbnail_quality=4, log_level=case["log_level"], ffmpeg_threads=case["threads"]
    )
    runner = ffmpeg_runner.create_ffmpeg_runner(
        video_file="/media/Movie (2020)/Movie.mkv",
        output_folder="/tmp/golden-out",
        gpu=case["gpu"],
        gpu_device_path=case["device"],
        config=config,
        progress_callback=None,
        ffmpeg_threads_override=None,
        cancel_check=None,
        pause_check=None,
        path_kind=case["kind"],
        libplacebo_vf="LIBPLACEBO_VF",
        use_libplacebo=case["init_vulkan"],
        dv5_software_fallback=False,
        base_scale="scale=w=320:h=240:force_original_aspect_ratio=decrease",
        fps_filter="fps=fps=0.1:round=up",
        hdr10_zscale_chain="ZSCALE_CHAIN",
    )
    proc = MagicMock(returncode=0, pid=4242)
    proc.poll.return_value = 0
    with (
        patch.object(ffmpeg_runner.subprocess, "Popen", return_value=proc) as popen,
        patch.object(ffmpeg_runner.time, "sleep"),
        patch("media_preview_generator.gpu.vulkan_probe.get_vulkan_env_overrides", return_value={"VK_DRIVER_FILES": "/icd.json"}),
    ):
        runner(use_skip=case["use_skip"], init_vulkan=case["init_vulkan"], disable_vaapi_dv5=case["no_dv5"])
    env = popen.call_args.kwargs["env"]
    return {"args": list(popen.call_args.args[0]), "vk_driver_files": None if env is None else env.get("VK_DRIVER_FILES")}


@functools.lru_cache(maxsize=1)
def _golden() -> dict:
    return json.loads(GOLDEN.read_text())


def test_write_golden_file() -> None:
    if os.environ.get("UPDATE_GOLDEN") != "1":
        pytest.skip("UPDATE_GOLDEN=1 rewrites the golden file from the current runner (only before the hwaccel move)")
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(json.dumps({case_id: _argv(case) for case_id, case in sorted(CASES.items())}, indent=1) + "\n")


def test_golden_file_covers_every_case() -> None:
    assert sorted(_golden()) == sorted(CASES)


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_preview_argv_is_unchanged(case_id: str) -> None:
    assert _argv(CASES[case_id]) == _golden()[case_id]
```

- [ ] **Step 2: Write the golden file from the unmodified runner**

Run: `UPDATE_GOLDEN=1 /home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/test_ffmpeg_runner_golden_args.py::test_write_golden_file -q`
then `/home/data/.venv/bin/python -m pytest --no-cov tests/test_ffmpeg_runner_golden_args.py -q`
Expected: 1 skipped on the second run (the writer), 463 passed (462 cases + coverage). Sanity-check three entries by
eye: `NVIDIA|cuda:1|sdr|vulkan=False|skip=True|no_vaapi_dv5=False` holds `-hwaccel cuda -hwaccel_device 1
-hwaccel_output_format cuda`; `INTEL|/dev/dri/renderD128|sdr|…` holds `-hwaccel vaapi -hwaccel_device
/dev/dri/renderD128 -hwaccel_output_format vaapi`; `APPLE|None|sdr|…` holds `-hwaccel videotoolbox`.
`git stash` nothing: the runner is still unmodified at this point.

- [ ] **Step 3: Write the failing helper tests**

```python
# tests/test_processing_hwaccel.py
"""hwaccel_decode_args: every vendor × device path × keep-on-GPU cell (the same branches the preview runner had)."""

import pytest

from media_preview_generator.processing.hwaccel import HwDecode, hwaccel_decode_args

RENDER = "/dev/dri/renderD128"


@pytest.mark.parametrize(
    ("gpu", "device", "keep", "expected"),
    [
        (None, None, True, HwDecode((), False)),
        (None, RENDER, True, HwDecode((), False)),
        ("NVIDIA", "cuda:0", True, HwDecode(("-hwaccel", "cuda", "-hwaccel_device", "0", "-hwaccel_output_format", "cuda"), True)),
        ("NVIDIA", "cuda:3", False, HwDecode(("-hwaccel", "cuda", "-hwaccel_device", "3"), True)),
        ("NVIDIA", "cuda:", True, HwDecode(("-hwaccel", "cuda", "-hwaccel_output_format", "cuda"), True)),
        ("NVIDIA", None, False, HwDecode(("-hwaccel", "cuda"), True)),
        ("NVIDIA", RENDER, True, HwDecode(("-hwaccel", "cuda", "-hwaccel_output_format", "cuda"), True)),
        ("INTEL", RENDER, True, HwDecode(("-hwaccel", "vaapi", "-hwaccel_device", RENDER, "-hwaccel_output_format", "vaapi"), True)),
        ("AMD", "/dev/dri/renderD129", False, HwDecode(("-hwaccel", "vaapi", "-hwaccel_device", "/dev/dri/renderD129"), True)),
        ("INTEL", None, True, HwDecode((), False)),
        ("INTEL", "cuda:0", True, HwDecode((), False)),
        ("WINDOWS_GPU", RENDER, True, HwDecode(("-hwaccel", "d3d11va"), True)),
        ("APPLE", None, False, HwDecode(("-hwaccel", "videotoolbox"), True)),
        ("OTHER", RENDER, False, HwDecode(("-hwaccel", "vaapi", "-hwaccel_device", RENDER), True)),
    ],
)  # fmt: skip
def test_hwaccel_decode_args(gpu, device, keep, expected):
    assert hwaccel_decode_args(gpu, device, keep_on_gpu=keep) == expected
```

Run: `/home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/test_processing_hwaccel.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'media_preview_generator.processing.hwaccel'`.

- [ ] **Step 4: Implement `hwaccel.py`**

```python
# media_preview_generator/processing/hwaccel.py
"""FFmpeg hardware-decode arguments for a worker's GPU, shared by preview thumbnails and Intro & Credits frame decoding.

The Dolby Vision Profile 5 device-init paths (VAAPI → OpenCL / Vulkan) stay in ``ffmpeg_runner``: only previews use
them.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HwDecode:
    """Input-side hwaccel arguments.

    Attributes:
        args: Tokens that go before ``-i``.
        active: Whether decode runs on the GPU.
    """

    args: tuple[str, ...]
    active: bool


def hwaccel_decode_args(gpu: str | None, gpu_device_path: str | None, *, keep_on_gpu: bool) -> HwDecode:
    """The ``-hwaccel`` arguments for a worker's GPU.

    Args:
        gpu: The worker's GPU type (``NVIDIA``, ``INTEL``, ``AMD``, ``WINDOWS_GPU``, ``APPLE``…), None on a CPU worker.
        gpu_device_path: ``cuda:<index>`` for NVIDIA, a ``/dev/dri`` render node for VAAPI GPUs.
        keep_on_gpu: Keep decoded frames as GPU surfaces so a GPU scale filter (``scale_cuda`` / ``scale_vaapi``) can
            shrink them before download.

    Returns:
        The arguments and whether decode runs on the GPU; no arguments for a CPU worker or a GPU without a usable
        device.
    """
    if gpu is None:
        return HwDecode((), False)
    if gpu == "NVIDIA":
        args = ["-hwaccel", "cuda"]
        # Multi-GPU hosts register each NVIDIA card as cuda:<index>; without -hwaccel_device work lands on GPU 0
        # (issue #221).
        if gpu_device_path and gpu_device_path.startswith("cuda:"):
            index = gpu_device_path.split(":", 1)[1]
            if index:
                args += ["-hwaccel_device", index]
        if keep_on_gpu:
            # Without it ffmpeg silently downloads every full-size frame to host RAM (~990 MB RSS per worker on 4K
            # HDR10, issue #218).
            args += ["-hwaccel_output_format", "cuda"]
        return HwDecode(tuple(args), True)
    if gpu == "WINDOWS_GPU":
        return HwDecode(("-hwaccel", "d3d11va"), True)
    if gpu == "APPLE":
        return HwDecode(("-hwaccel", "videotoolbox"), True)
    if gpu_device_path and gpu_device_path.startswith("/dev/dri/"):
        # -hwaccel_device (not the deprecated -vaapi_device) pairs with -hwaccel_output_format vaapi (issue #218).
        args = ["-hwaccel", "vaapi", "-hwaccel_device", gpu_device_path]
        if keep_on_gpu:
            args += ["-hwaccel_output_format", "vaapi"]
        return HwDecode(tuple(args), True)
    return HwDecode((), False)
```

- [ ] **Step 5: Use it in the runner**

In `media_preview_generator/processing/ffmpeg_runner.py` add `from .hwaccel import hwaccel_decode_args` to the local
imports, then replace the two branches (keep the DV5 VAAPI branch between them and the `elif use_gpu and init_vulkan:`
branch after them byte for byte):

```python
        hw_decode_active = False
        if use_gpu and effective_gpu == "NVIDIA":
            decode = hwaccel_decode_args(effective_gpu, effective_gpu_device_path, keep_on_gpu=keep_on_gpu)
            args += list(decode.args)
            hw_decode_active = decode.active
        elif use_intel_opencl_dv5 or use_vaapi_dv5:
            # (unchanged block)
            ...
        elif use_gpu and not init_vulkan:
            decode = hwaccel_decode_args(effective_gpu, effective_gpu_device_path, keep_on_gpu=keep_on_gpu)
            args += list(decode.args)
            hw_decode_active = decode.active
        elif use_gpu and init_vulkan:
            # (unchanged block)
            ...
```

The issue #218/#221 comments now live in `hwaccel.py`; delete them from the runner branches. The `...` above stands
for the existing, untouched lines — don't type an ellipsis into the file.

- [ ] **Step 6: Run the helper, golden and existing runner tests**

Run: `/home/data/.venv/bin/python -m pytest --no-cov tests/test_processing_hwaccel.py tests/test_ffmpeg_runner_golden_args.py tests/test_ffmpeg_runner_progress_decode.py tests/test_media_processing.py tests/test_processing_vendors.py tests/test_retry_cascade.py -q`
Expected: PASS, the golden file unchanged (`git diff --stat tests/fixtures/ffmpeg_runner_golden_args.json` shows
nothing beyond the new file). Then prove the golden test bites: temporarily change `"-hwaccel_device", index` to
`"-hwaccel_device", "0"` in `hwaccel.py`, rerun the golden test (expect failures on every `cuda:1` case), revert.

- [ ] **Step 7: Commit**

Stage the four new files and the runner; Architecture Review on the staged diff; then
`PATH="/home/data/.venv/bin:$PATH" git commit -m "refactor(processing): share hwaccel decode args, golden preview argv"`.

---
## Task 3: Rule J port + anonymised 80-file fixture

`[lane-parallel]` — spec §5.4 (rule J steps 1–5, the table), roadmap phase 3 bullet 4 ("Regression test reproduces
59/80 within 10 s, 1 early, from an anonymised copy of `evidence/credits/f3.jsonl` committed as a test fixture"),
T-R5, T-R6, C5, Q3, preflight I7 (epilogue cards), M16 (converter fallback).

**OWNER DECISION (2026-09-16), Q3:** a credits text candidate ends at the last credit frame of the chosen run (refined
like the start, at the last contiguous credit frame) when that run ends more than 30 s before the end of the file;
otherwise it runs to the end of the file. This task adds `credits_end` (the prototype has no end: the cells in
`TestEnd` define it; the fixture pins how many of the 80 get an end, 4 of 76 runs, measured while planning).

The prototype's `detect()` (`evidence/credits/eval_rules3.py`) with rule J's parameters in keyframe mode, refined over
the 20 s before the coarse answer, is the reference. The port keeps its order of operations, including two quirks: the
anchor's "median" gap indexes `len(rows) // 2` into the `len(rows) - 1` gaps, and a refine window without credit frames
returns the coarse time without stepping back over the fade. One deliberate difference: with exactly two rows the
prototype's index doesn't exist (IndexError); the port clamps it (tested).

**Files:**
- Create: `media_preview_generator/markers/credits/__init__.py` (content: `"""Credits from on-screen text (spec §5.4)."""`),
  `media_preview_generator/markers/credits/rule_j.py`, `tools/markers_eval/credits_fixture.py`,
  `tests/fixtures/markers/credits_rule_j_80.json.gz`, `tests/markers/credits/__init__.py` (empty),
  `tests/markers/credits/test_rule_j.py`
- Test: `tests/markers/credits/test_rule_j.py`

**Interfaces:**
- Consumes: local-only `evidence/credits/f3.jsonl`, `evidence/credits/adjudicated.json`, `evidence/credits/eval_rules3.py`
  (only `credits_fixture.py`, once, on storage).
- Produces (Tasks 8 and 11 rely on these):
```python
# media_preview_generator/markers/credits/rule_j.py
Row = tuple[float, int, float]                       # (pts s, text boxes, mean luma rounded to 0.1)
@dataclass(frozen=True) class RuleParams:
    dense: int = 3; min_boxes: int = 1; dark: float = 30.0; gap_s: float = 24.0; run_s: float = 15.0
    anchor: bool = True; bridge_dark: bool = True
RULE_J: RuleParams
FADE_LUMA = 12.0; FADE_STEP_S = 4.0; REFINE_BEFORE_S = 20.0; REFINE_AFTER_S = 1.0; REFINE_GAP_S = 2.5
ANCHOR_SPACING_FACTOR = 1.5
KEEP_AFTER_CREDITS_S = 30.0; REFINE_END_BEFORE_S = 1.0; REFINE_END_AFTER_S = 20.0      # Q3
@dataclass(frozen=True) class Coarse: index: int; end_index: int; pts_s: float
def is_credit(row: Row, params: RuleParams = RULE_J) -> bool
def credit_runs(rows: Sequence[Row], params: RuleParams = RULE_J) -> list[tuple[int, int]]
def coarse_start(rows: Sequence[Row], params: RuleParams = RULE_J) -> Coarse | None
def fade_back(rows: Sequence[Row], index: int, floor_s: float) -> float
def refine_start(rows: Sequence[Row], coarse: Coarse, fine_rows: Sequence[Row], *,
                 before_s: float = REFINE_BEFORE_S, params: RuleParams = RULE_J) -> float
def credits_start(rows: Sequence[Row], fine_rows: Sequence[Row], *,
                  before_s: float = REFINE_BEFORE_S, params: RuleParams = RULE_J) -> float | None
def coarse_end_s(rows: Sequence[Row], coarse: Coarse) -> float                    # the run's last credit keyframe
def keeps_a_scene_after(end_s: float, duration_s: float) -> bool                  # duration − end > 30 s
def refine_end(rows: Sequence[Row], coarse: Coarse, fine_rows: Sequence[Row], *,
               after_s: float = REFINE_END_AFTER_S, params: RuleParams = RULE_J) -> float
def credits_end(rows: Sequence[Row], coarse: Coarse, fine_rows: Sequence[Row], duration_s: float, *,
                params: RuleParams = RULE_J) -> float | None                     # None: no end (runs to EOF)
```
Fixture JSON (gzip, `mtime=0`): `{"about": str, "refine_before_s": 20.0, "items": [{"id": "movie-01"|"tv-01",
"kind": "movie"|"tv", "duration_s": float, "truth_s": float, "key": [[pts, boxes, luma]], "fine": [[pts, boxes,
luma]], "expected_error_s": float | null}]}`.

- [ ] **Step 1: Write the failing cell tests**

```python
# tests/markers/credits/test_rule_j.py
"""Rule J (spec §5.4): each step as a matrix of small row sets, then the 80-file regression fixture."""

from __future__ import annotations

import gzip
import json
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path

import pytest

from media_preview_generator.markers.credits import rule_j
from media_preview_generator.markers.credits.rule_j import Coarse

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "markers" / "credits_rule_j_80.json.gz"


def dark(t: float, boxes: int = 0, luma: float = 10.0) -> tuple[float, int, float]:
    return (t, boxes, luma)


def bright(t: float, boxes: int = 0, luma: float = 120.0) -> tuple[float, int, float]:
    return (t, boxes, luma)


class TestCreditFrame:
    @pytest.mark.parametrize(
        ("row", "expected"),
        [
            ((0.0, 1, 29.9), True),     # dark: one box is enough
            ((0.0, 0, 29.9), False),
            ((0.0, 2, 30.0), False),    # 30 is bright: needs 3
            ((0.0, 3, 30.0), True),
            ((0.0, 3, 200.0), True),
        ],
    )  # fmt: skip
    def test_dark_frames_need_one_box_bright_frames_three(self, row, expected):
        assert rule_j.is_credit(row) is expected


class TestRuns:
    def test_a_24_s_gap_joins_and_a_longer_one_splits(self):
        joined = [dark(0, 1), dark(10, 1), dark(20, 1), bright(30), dark(44, 1), dark(54, 1)]
        assert rule_j.credit_runs(joined) == [(0, 5)]
        split = [dark(0, 1), dark(10, 1), dark(20, 1), bright(30), dark(44.5, 1), dark(54.5, 1), dark(64.5, 1)]
        assert rule_j.credit_runs(split) == [(0, 2), (4, 6)]

    def test_dark_empty_frames_never_break_a_run_but_bright_ones_do(self):
        bridged = [dark(0, 1), dark(10, 1), dark(20, 1), dark(30), dark(40), dark(50, 1), dark(60, 1)]
        assert rule_j.credit_runs(bridged) == [(0, 6)]
        broken = [dark(0, 1), dark(10, 1), dark(20, 1), bright(30), dark(40), dark(50, 1), dark(60, 1)]
        assert rule_j.credit_runs(broken) == [(0, 2)]  # 50–60 s is only 10 s long

    @pytest.mark.parametrize(("length", "kept"), [(14.9, False), (15.0, True)])
    def test_runs_shorter_than_15_s_are_dropped(self, length, kept):
        rows = [dark(100, 1), dark(100 + length, 1)]
        assert rule_j.credit_runs(rows) == ([(0, 1)] if kept else [])


class TestCoarse:
    def test_the_last_run_is_picked(self):
        rows = [dark(0, 1), dark(20, 1), bright(30), bright(60), dark(100, 1), dark(116, 1)]
        assert rule_j.coarse_start(rows) == Coarse(index=4, end_index=5, pts_s=100.0)

    def test_a_lone_text_frame_before_the_roll_is_not_the_start(self):
        # Undisputed: one story-scene text frame 20 s before the roll joins it over the 24 s gap; the anchor skips it.
        story = [bright(t) for t in range(0, 20, 2)]
        rows = [*story, dark(20, 1), *[bright(t) for t in range(22, 40, 2)], *[dark(t, 2) for t in range(40, 70, 2)]]
        coarse = rule_j.coarse_start(rows)
        assert coarse is not None and coarse.pts_s == 40.0

    def test_two_rows_do_not_raise(self):
        assert rule_j.coarse_start([dark(0, 1), dark(20, 1)]) == Coarse(index=0, end_index=1, pts_s=0.0)

    def test_no_run_means_no_answer(self):
        assert rule_j.coarse_start([bright(0), bright(10, 2), dark(20)]) is None
        assert rule_j.credits_start([bright(0)], []) is None


class TestRefine:
    ROWS = [bright(0), bright(60), dark(100, 1), dark(102, 1), dark(110, 1), dark(125, 1)]

    def test_the_walk_back_stops_at_a_gap_longer_than_2_5_s(self):
        coarse = rule_j.coarse_start(self.ROWS)
        # Credit frames at luma 20 (dark, but not a fade): 101 → 100 → 98, then 95 is 3 s earlier.
        fine = [bright(85), dark(86, 0, 5.0), dark(87, 0, 8.0), dark(88, 1, 20.0), dark(90, 1, 20.0), dark(92, 2, 20.0),
                dark(95, 1, 20.0), dark(98, 1, 20.0), dark(100, 1, 20.0), dark(101, 1, 20.0)]  # fmt: skip
        assert rule_j.refine_start(self.ROWS, coarse, fine) == 98.0

    def test_walks_back_through_contiguous_credit_frames_then_over_the_fade(self):
        coarse = rule_j.coarse_start(self.ROWS)
        fine = [bright(85), dark(86, 0, 5.0), dark(87, 0, 8.0), *[dark(t, 1, 20.0) for t in (88, 90, 92, 94, 96, 98, 100, 101)]]
        assert rule_j.refine_start(self.ROWS, coarse, fine) == 86.0

    def test_the_fade_includes_dark_credit_frames(self):
        # Luma 10 is below the fade threshold too: the fade step walks over dark credit cards as well (prototype).
        coarse = rule_j.coarse_start(self.ROWS)
        fine = [bright(85), dark(86, 0, 5.0), dark(87, 0, 8.0), dark(88, 1), dark(90, 1), dark(92, 2), dark(95, 1),
                dark(98, 1), dark(100, 1), dark(101, 1)]  # fmt: skip
        assert rule_j.refine_start(self.ROWS, coarse, fine) == 86.0

    def test_fade_back_steps_over_dark_frames_up_to_4_s_apart(self):
        coarse = rule_j.coarse_start(self.ROWS)
        fine = [bright(90), dark(93, 0, 6.0), dark(96, 0, 4.0), dark(99, 1), dark(100, 1)]
        assert rule_j.refine_start(self.ROWS, coarse, fine) == 93.0

    def test_fade_back_stops_at_the_window_floor(self):
        coarse = rule_j.coarse_start(self.ROWS)
        # Floor = 100 − 20 = 80 s: the walk reaches 83, the fade steps to 81 and not to 79 or 77.
        fine = [dark(77, 0, 6.0), dark(79, 0, 6.0), dark(81, 0, 6.0), *[dark(t, 1, 20.0) for t in (83, 85, 87, 89, 91, 93, 95, 97, 99, 100)]]
        assert rule_j.refine_start(self.ROWS, coarse, fine) == 81.0

    def test_no_credit_frame_in_the_window_keeps_the_coarse_time(self):
        coarse = rule_j.coarse_start(self.ROWS)
        assert rule_j.refine_start(self.ROWS, coarse, [bright(95), bright(96)]) == 100.0

    def test_no_fine_rows_in_the_window_fades_back_on_the_keyframes(self):
        rows = [bright(0), dark(96, 0, 5.0), dark(100, 1), dark(102, 1), dark(116, 1)]
        coarse = rule_j.coarse_start(rows)
        assert rule_j.refine_start(rows, coarse, [bright(10)]) == 96.0


class TestEpilogueCards:
    """Spec §5.4's owner rule says epilogue text cards aren't credits; rule J can't tell them apart when they touch the
    roll. These cells pin what it does (checked against the prototype's ``detect``); Task 11's frame-check sheets look at
    every candidate shaped like this."""

    STORY = [bright(t) for t in range(0, 90, 2)]
    CARDS = [dark(t, 1) for t in range(90, 100, 2)]  # 10 s of white-on-black cards

    def test_cards_right_before_the_roll_become_the_start(self):
        rows = [*self.STORY, *self.CARDS, *[dark(t, 2) for t in range(100, 162, 2)]]
        fine = [*[bright(t) for t in range(80, 90)], *[dark(t, 1) for t in range(90, 100)], dark(100, 2)]
        assert rule_j.credits_start(rows, fine) == 90.0

    def test_cards_bridged_to_the_roll_by_black_become_the_start(self):
        rows = [*self.STORY, *self.CARDS, *[dark(t) for t in range(100, 130, 2)], *[dark(t, 2) for t in range(130, 190, 2)]]
        fine = [*[dark(t) for t in range(110, 130)], dark(130, 2)]
        assert rule_j.credits_start(rows, fine) == 90.0

    def test_cards_split_from_the_roll_by_a_lit_scene_are_not_the_start(self):
        rows = [*self.STORY, *self.CARDS, *[bright(t) for t in range(100, 130, 2)], *[dark(t, 2) for t in range(130, 190, 2)]]
        fine = [*[bright(t) for t in range(110, 130)], dark(130, 2)]
        assert rule_j.credits_start(rows, fine) == 130.0


class TestEnd:
    """Owner, Q3 (2026-09-16): the skip ends at the roll's last credit frame when more than 30 s of the file follows it."""

    ROWS = [bright(0), bright(60), dark(100, 1), dark(102, 1), dark(110, 1), dark(125, 1)]  # run 100–125 s

    def _coarse(self):
        return rule_j.coarse_start(self.ROWS)

    def test_a_roll_that_runs_to_the_end_of_the_file_is_open_ended(self):
        # 25 s after the last credit keyframe: a logo or black, not a scene. The fine rows aren't read.
        assert rule_j.credits_end(self.ROWS, self._coarse(), [dark(126, 1), dark(127, 1)], 150.0) is None

    def test_a_scene_after_the_roll_is_kept_the_skip_ends_at_the_last_contiguous_credit_frame(self):
        fine = [dark(124, 1), dark(125, 1), dark(126, 1), dark(127, 2), bright(128), bright(129), bright(130), dark(131, 1), bright(132)]
        assert rule_j.credits_end(self.ROWS, self._coarse(), fine, 200.0) == 127.0  # 131 is 4 s after 127

    @pytest.mark.parametrize(("after_s", "expected"), [(30.0, None), (30.1, 125.0)])
    def test_more_than_30_s_must_follow(self, after_s, expected):
        assert rule_j.credits_end(self.ROWS, self._coarse(), [], 125.0 + after_s) == expected

    def test_a_refined_end_within_30_s_of_the_end_is_open_ended(self):
        fine = [dark(t, 1) for t in range(124, 129)]  # the roll goes on to 128 s; 157 − 128 = 29 s
        assert rule_j.credits_end(self.ROWS, self._coarse(), fine, 157.0) is None

    def test_no_credit_frame_near_the_last_keyframe_keeps_the_keyframe(self):
        assert rule_j.refine_end(self.ROWS, self._coarse(), [bright(126), bright(127)]) == 125.0

    def test_the_walk_only_reads_the_window_around_the_last_keyframe(self):
        # 123 s is before the window (end − 1 s); 146 s is past it (end + 20 s).
        fine = [dark(123, 3), dark(124, 1), dark(146, 1)]
        assert rule_j.refine_end(self.ROWS, self._coarse(), fine) == 124.0


@lru_cache(maxsize=1)
def _fixture() -> dict:
    return json.loads(gzip.decompress(FIXTURE.read_bytes()))


def _rows(raw: list) -> list[tuple[float, int, float]]:
    return [(float(r[0]), int(r[1]), float(r[2])) for r in raw]


class TestEightyFiles:
    def test_reproduces_the_spec_table(self):
        tally = Counter()
        for item in _fixture()["items"]:
            start = rule_j.credits_start(_rows(item["key"]), _rows(item["fine"]))
            if start is None:
                tally["none"] += 1
                continue
            error = start - item["truth_s"]
            tally["within_10s"] += abs(error) <= 10
            if abs(error) > 30:
                tally["early" if error < 0 else "late"] += 1
        assert len(_fixture()["items"]) == 80
        assert (tally["within_10s"], tally["early"], tally["late"], tally["none"]) == (59, 1, 8, 4)

    def test_matches_the_prototype_item_for_item(self):
        for item in _fixture()["items"]:
            start = rule_j.credits_start(_rows(item["key"]), _rows(item["fine"]))
            expected = item["expected_error_s"]
            if expected is None:
                assert start is None, item["id"]
            else:
                assert start is not None and abs((start - item["truth_s"]) - expected) <= 0.0015, item["id"]

    def test_four_of_the_76_runs_end_more_than_30_s_before_the_end_of_the_file(self):
        # Measured while planning, on coarse ends (the fixture's 1 fps rows sit around the start, so no end refine here).
        kept = []
        for item in _fixture()["items"]:
            key = _rows(item["key"])
            coarse = rule_j.coarse_start(key)
            if coarse is not None and rule_j.keeps_a_scene_after(rule_j.coarse_end_s(key, coarse), item["duration_s"]):
                kept.append(item["id"])
        assert len(kept) == 4, kept

    def test_fixture_is_anonymised(self):
        raw = gzip.decompress(FIXTURE.read_bytes()).decode()
        assert "/" not in raw.replace("\\/", "")
        assert not re.search(r"\.(mkv|mp4|avi)|\{(tmdb|tvdb|imdb)-|\[[^\]]*p\]", raw)
        for item in _fixture()["items"]:
            assert re.fullmatch(r"(movie|tv)-\d\d", item["id"])
            assert set(item) == {"id", "kind", "duration_s", "truth_s", "key", "fine", "expected_error_s"}
```

Run: `/home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/markers/credits/test_rule_j.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'media_preview_generator.markers.credits'`.

Every expected value in `TestRuns`, `TestCoarse`, `TestRefine` and `TestEpilogueCards` was checked against the
prototype's `runs()` and `detect()` (refine span 20 s) while planning.

- [ ] **Step 2: Implement `rule_j.py`**

```python
# media_preview_generator/markers/credits/rule_j.py
"""Rule "J": where the credit roll starts, and where it ends when a scene follows it, from the text boxes and brightness
of a file's ending (spec §5.4; the end is the owner's Q3 ruling, 2026-09-16).

A port of the measured prototype (``evidence/credits/eval_rules3.py`` ``detect`` with rule J's parameters, keyframe
mode, refined over the 20 s before the coarse answer). Same inputs, same order, same comparisons, so it reproduces the
spec's table on the 80 files (``tests/fixtures/markers/credits_rule_j_80.json.gz``). Rows stay in ffmpeg's output order:
the measured keyframe rows aren't always increasing, and sorting them changes an answer.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

Row = tuple[float, int, float]

FADE_LUMA = 12.0
FADE_STEP_S = 4.0
REFINE_BEFORE_S = 20.0
REFINE_AFTER_S = 1.0
REFINE_GAP_S = 2.5
ANCHOR_SPACING_FACTOR = 1.5
# Owner, Q3: the skip ends at the roll's last credit frame only when more than this much of the file follows it;
# otherwise it runs to the end of the file (no end), so a closing logo or a few seconds of black never become a stop.
KEEP_AFTER_CREDITS_S = 30.0
REFINE_END_BEFORE_S = 1.0
REFINE_END_AFTER_S = 20.0


@dataclass(frozen=True)
class RuleParams:
    """Rule J's thresholds.

    Attributes:
        dense: Text boxes a bright frame needs to count as a credit frame (signage in a lit scene gave 3+ boxes for
            minutes: Checkin' It Twice).
        min_boxes: Text boxes a dark frame needs (cards on black often show only 1–2 boxes at 320 px).
        dark: Mean luma below which a frame is dark.
        gap_s: Credit frames join a run across gaps up to this long.
        run_s: The shortest run kept.
        anchor: Start only where two credit samples sit next to each other (Undisputed: a lone scene-text frame 24 s
            before the roll).
        bridge_dark: Dark frames without text never break a run (Summit of the Gods: 24 s of dark keyframes).
    """

    dense: int = 3
    min_boxes: int = 1
    dark: float = 30.0
    gap_s: float = 24.0
    run_s: float = 15.0
    anchor: bool = True
    bridge_dark: bool = True


RULE_J = RuleParams()


@dataclass(frozen=True)
class Coarse:
    """The last credit run's start after the anchor step.

    Attributes:
        index: The start's row in the keyframe rows.
        end_index: The run's last credit row.
        pts_s: The start's time.
    """

    index: int
    end_index: int
    pts_s: float


def is_credit(row: Row, params: RuleParams = RULE_J) -> bool:
    """Whether a frame shows credits: a dark frame with text, or a bright frame with a lot of it."""
    if row[2] < params.dark:
        return row[1] >= params.min_boxes
    return row[1] >= params.dense


def credit_runs(rows: Sequence[Row], params: RuleParams = RULE_J) -> list[tuple[int, int]]:
    """Runs of credit frames (first and last row index) at least ``run_s`` long, in row order."""
    runs: list[list[int]] = []
    last_bright = -1.0
    for i, row in enumerate(rows):
        if is_credit(row, params):
            joined = bool(runs) and row[0] - rows[runs[-1][1]][0] <= params.gap_s
            if params.bridge_dark and runs and last_bright < rows[runs[-1][1]][0]:
                joined = True
            if joined:
                runs[-1][1] = i
            else:
                runs.append([i, i])
        elif row[2] >= params.dark:
            last_bright = row[0]
    return [(first, last) for first, last in runs if rows[last][0] - rows[first][0] >= params.run_s]


def _typical_spacing(rows: Sequence[Row]) -> float:
    gaps = sorted(rows[i + 1][0] - rows[i][0] for i in range(len(rows) - 1))
    # The prototype indexes len(rows) // 2 into len(rows) - 1 gaps; with two rows that index doesn't exist.
    return gaps[min(len(rows) // 2, len(gaps) - 1)]


def coarse_start(rows: Sequence[Row], params: RuleParams = RULE_J) -> Coarse | None:
    """The last credit run's anchored start, or None when the rows hold no run.

    Args:
        rows: Keyframe rows of the tail, in decode order.
        params: Rule thresholds.

    Returns:
        The coarse start, or None.
    """
    runs = credit_runs(rows, params)
    if not runs:
        return None
    first, last = runs[-1]
    if params.anchor:
        spacing = _typical_spacing(rows)
        while first < last:
            following = next(k for k in range(first + 1, last + 1) if is_credit(rows[k], params))
            if rows[following][0] - rows[first][0] <= ANCHOR_SPACING_FACTOR * spacing:
                break
            first = following
    return Coarse(index=first, end_index=last, pts_s=rows[first][0])


def fade_back(rows: Sequence[Row], index: int, floor_s: float) -> float:
    """Step back from ``rows[index]`` over the fade to black: frames darker than 12, at most 4 s apart, not before
    ``floor_s``.

    Returns:
        The time of the earliest frame reached.
    """
    i = index
    while (
        i > 0
        and rows[i - 1][0] >= floor_s
        and rows[i - 1][2] < FADE_LUMA
        and rows[i][0] - rows[i - 1][0] <= FADE_STEP_S
    ):
        i -= 1
    return rows[i][0]


def refine_start(
    rows: Sequence[Row],
    coarse: Coarse,
    fine_rows: Sequence[Row],
    *,
    before_s: float = REFINE_BEFORE_S,
    params: RuleParams = RULE_J,
) -> float:
    """Refine the coarse start with the 1 fps rows decoded just before it.

    Args:
        rows: The keyframe rows the coarse start came from.
        coarse: The coarse start.
        fine_rows: 1 fps rows (any window; only ``[coarse − before_s, coarse + 1 s]`` is read).
        before_s: How far before the coarse start the refinement may reach.
        params: Rule thresholds.

    Returns:
        The refined start in seconds.
    """
    t = coarse.pts_s
    floor_s = t - before_s
    window = [j for j, row in enumerate(fine_rows) if floor_s <= row[0] <= t + REFINE_AFTER_S]
    if not window:
        return fade_back(rows, coarse.index, -1.0)
    credit = [j for j in window if is_credit(fine_rows[j], params)]
    if not credit:
        return t
    j = credit[-1]
    while True:
        earlier = [k for k in credit if k < j and fine_rows[j][0] - fine_rows[k][0] <= REFINE_GAP_S]
        if not earlier:
            break
        j = earlier[0]
    return fade_back(fine_rows, j, floor_s)


def credits_start(
    rows: Sequence[Row],
    fine_rows: Sequence[Row],
    *,
    before_s: float = REFINE_BEFORE_S,
    params: RuleParams = RULE_J,
) -> float | None:
    """Coarse start then refinement on rows already decoded (the harness and the fixture test; the detector decodes the
    fine rows only after it knows the coarse start).

    Returns:
        The credits start in seconds, or None when the keyframe rows hold no credit run.
    """
    coarse = coarse_start(rows, params)
    return None if coarse is None else refine_start(rows, coarse, fine_rows, before_s=before_s, params=params)


def coarse_end_s(rows: Sequence[Row], coarse: Coarse) -> float:
    """The chosen run's last credit keyframe."""
    return rows[coarse.end_index][0]


def keeps_a_scene_after(end_s: float, duration_s: float) -> bool:
    """Whether more than 30 s of the file follows an end (Q3), so the skip stops there."""
    return duration_s - end_s > KEEP_AFTER_CREDITS_S


def refine_end(
    rows: Sequence[Row],
    coarse: Coarse,
    fine_rows: Sequence[Row],
    *,
    after_s: float = REFINE_END_AFTER_S,
    params: RuleParams = RULE_J,
) -> float:
    """Refine the run's last credit keyframe with 1 fps rows, the start's walk in the other direction.

    From the first credit frame in ``[end − 1 s, end + after_s]``, walk forward over credit frames at most 2.5 s apart;
    the end is the last one reached (no fade step: the skip stops on the roll's last frame, never inside the scene).

    Args:
        rows: The keyframe rows the coarse start came from.
        coarse: The coarse start (its ``end_index`` is the run's last credit keyframe).
        fine_rows: 1 fps rows (any window; only ``[end − 1 s, end + after_s]`` is read).
        after_s: How far past the last credit keyframe the refinement may reach.
        params: Rule thresholds.

    Returns:
        The refined end in seconds (the keyframe's time when the window holds no credit frame).
    """
    t = coarse_end_s(rows, coarse)
    window = [j for j, row in enumerate(fine_rows) if t - REFINE_END_BEFORE_S <= row[0] <= t + after_s]
    credit = [j for j in window if is_credit(fine_rows[j], params)]
    if not credit:
        return t
    j = credit[0]
    while True:
        later = [k for k in credit if k > j and fine_rows[k][0] - fine_rows[j][0] <= REFINE_GAP_S]
        if not later:
            break
        j = later[-1]
    return fine_rows[j][0]


def credits_end(
    rows: Sequence[Row],
    coarse: Coarse,
    fine_rows: Sequence[Row],
    duration_s: float,
    *,
    params: RuleParams = RULE_J,
) -> float | None:
    """Where the credits skip ends (Q3): the refined last credit frame when more than 30 s of the file follows it.

    Args:
        rows: The keyframe rows the coarse start came from.
        coarse: The coarse start.
        fine_rows: 1 fps rows around the run's last credit keyframe (read only when an end can be kept).
        duration_s: The file's duration.
        params: Rule thresholds.

    Returns:
        The end in seconds, or None: the skip runs to the end of the file.
    """
    if not keeps_a_scene_after(coarse_end_s(rows, coarse), duration_s):
        return None
    end = refine_end(rows, coarse, fine_rows, params=params)
    return end if keeps_a_scene_after(end, duration_s) else None
```

- [ ] **Step 3: Run the cell tests**

Run: `/home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/markers/credits/test_rule_j.py -q -k "not EightyFiles"`
Expected: PASS (the `TestEightyFiles` class fails until Step 5 writes the fixture). The end cells in `TestEnd` have no
prototype to check against: they encode the owner's Q3 wording (walk forward from the first credit frame near the last
credit keyframe over gaps ≤ 2.5 s, no fade step; an end is kept only when more than 30 s of the file follows it, checked
on the keyframe and again on the refined frame).

- [ ] **Step 4: Write the fixture converter**

```python
# tools/markers_eval/credits_fixture.py
"""Build ``tests/fixtures/markers/credits_rule_j_80.json.gz`` from the local-only evidence (run once, on storage):

    python -m tools.markers_eval.credits_fixture

Anonymised: files become ``movie-01…``/``tv-01…`` in the evidence order, every time is shifted by a whole number of
seconds so the item's tail window starts at 1000 s, rows keep only ``[pts, boxes, luma]``, and the frame-check truth
(``credits/adjudicated.json``) replaces the chapter truth. Before writing, every item is checked against the prototype
(``credits/eval_rules3.py`` ``detect``, refine span 20 s): the port on the shifted rows must give the prototype's
error within 1.5 ms, or the script stops (an unshifted item would keep real timings; none needed it while planning).
"""

from __future__ import annotations

import gzip
import json
import os
import sys
from pathlib import Path

from media_preview_generator.markers.credits import rule_j

from .data import evidence_dir

OUT = Path(__file__).resolve().parents[2] / "tests/fixtures/markers/credits_rule_j_80.json.gz"
RULE_J_PROTOTYPE = dict(dense=3, min_boxes=1, dark=30, gap=24, run=15, pick="last", anchor=True, bridge_dark=True)
REFINE_SPAN_S = 20.0
TAIL_ORIGIN_S = 1000


def _prototype(evidence: Path) -> dict:
    source = (evidence / "credits/eval_rules3.py").read_text().split("items = load(sys.argv[1])")[0]
    namespace: dict = {}
    previous = os.getcwd()
    os.chdir(evidence)  # the prototype's load() reads credits/adjudicated.json relative to the evidence folder
    try:
        exec(compile(source, "eval_rules3.py", "exec"), namespace)  # noqa: S102 - trusted repo evidence, local one-shot
        namespace["items"] = namespace["load"]("credits/f3.jsonl")
    finally:
        os.chdir(previous)
    return namespace


def _rows(raw: list, shift: int) -> list[list]:
    return [[round(r[0] - shift, 3), int(r[1]), float(r[3])] for r in raw]


def _port_error(item: dict, shift: int) -> tuple[float | None, list, list]:
    key, fine = _rows(item["key"], shift), _rows(item["fine"], shift)
    start = rule_j.credits_start([tuple(r) for r in key], [tuple(r) for r in fine])
    truth = round(item["truth"] - shift, 3)
    return (None if start is None else start - truth), key, fine


def build(evidence: Path) -> dict:
    proto = _prototype(evidence)
    counters = {"movie": 0, "tv": 0}
    items = []
    for item in proto["items"]:
        found = proto["detect"](item["key"], item["fine"], RULE_J_PROTOTYPE, REFINE_SPAN_S)
        expected = None if found is None else round(found - item["truth"], 3)
        shift = round(item["window_start"]) - TAIL_ORIGIN_S
        error, key, fine = _port_error(item, shift)
        if (error is None) != (expected is None) or (error is not None and abs(error - expected) > 0.0015):
            sys.exit(f"the port differs from the prototype on {item['kind']} item {counters[item['kind']] + 1}")
        counters[item["kind"]] += 1
        items.append(
            {
                "id": f"{item['kind']}-{counters[item['kind']]:02d}",
                "kind": item["kind"],
                "duration_s": round(item["duration"] - shift, 3),
                "truth_s": round(item["truth"] - shift, 3),
                "key": key,
                "fine": fine,
                "expected_error_s": expected,
            }
        )
    return {
        "about": "Rule J regression rows for the 80 credits files of spec §5.4, anonymised by tools.markers_eval.credits_fixture",
        "refine_before_s": REFINE_SPAN_S,
        "items": items,
    }


def main() -> int:
    data = json.dumps(build(evidence_dir()), separators=(",", ":")).encode()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(gzip.compress(data, mtime=0))
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Build the fixture and run every test**

Run (main checkout's evidence; in a worktree set `MARKERS_EVAL_EVIDENCE` to it):
`cd <lane root> && MARKERS_EVAL_EVIDENCE=/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence nice -n 19 /home/data/.venv/bin/python -m tools.markers_eval.credits_fixture`
then `/home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/markers/credits/test_rule_j.py -q`
Expected: `wrote …credits_rule_j_80.json.gz` (≈160 KB; a dry run while planning gave 159,428 bytes and 59 / 1 / 8 / 4), then
PASS including `TestEightyFiles`. If
`test_reproduces_the_spec_table` gives a different tuple while the item-for-item test passes, the planning
measurement was wrong: report the tuple, don't edit the expectation.

Mutation checks (report each result, then revert): `gap_s: float = 24.0` → `23.0` and `dark` → `31.0`, one at a time,
must fail `TestEightyFiles`; `KEEP_AFTER_CREDITS_S = 30.0` → `29.0` must fail `test_more_than_30_s_must_follow[30.0-None]`;
`j = credit[0]` → `credit[-1]` in `refine_end` must fail
`test_a_scene_after_the_roll_is_kept_the_skip_ends_at_the_last_contiguous_credit_frame`; `REFINE_GAP_S` read as 5 s in
`refine_end` must fail the same cell. (While planning the port passed all 34 cells and the fixture's end count; the
prototype-fallback branch of the converter was never taken on the 80 items.)

- [ ] **Step 6: Commit**

`gzip -dc tests/fixtures/markers/credits_rule_j_80.json.gz | grep -cE '/data|\.mkv|tmdb-|imdb-'` must print `0`. Stage
the six files; Architecture Review on the staged diff; then
`PATH="/home/data/.venv/bin:$PATH" git commit -m "feat(markers): rule J credits start and end with the 80-file regression fixture"`.

---
## Task 4: Vendored text detector (`textdet.py`) + bench gate

`[lane-parallel]` `[high-risk]` — spec §5.4 ("RapidOCR detection model only … `det_limit_side_len=320,
det_limit_type="max"`"; "vendor its det pre/post-processing"), roadmap phase 3 bullet 2 ("identical box counts to
`rapidocr_onnxruntime==1.4.4` on the 289-frame bench set before use"), T-R2, C6, Global Constraints "Phase-3 additions".

High-risk because every credits answer rests on these box counts, and the equivalence can drift silently in float
details (normalisation dtype, resize rounding, contour order, unclip distance).

**The bench set is not committed** (movie frames: copyright and library content). `tools/markers_eval/textdet_bench.py`
regenerates it from the local truth list exactly as `evidence/credits/gpu/extract.py` did, and records box counts per
implementation; the gate compares the two count files. CI instead compares the vendored code with a verbatim test copy
of rapidocr's code on generated inputs (no model needed).

**Files:**
- Create: `media_preview_generator/markers/credits/__init__.py` (identical to Task 3's),
  `media_preview_generator/markers/credits/textdet.py`, `media_preview_generator/markers/credits/PP-OCRv4-det-NOTICE.txt`,
  `tools/markers_eval/textdet_bench.py`, `tests/markers/credits/__init__.py` (empty),
  `tests/markers/credits/rapidocr_reference.py`, `tests/markers/credits/test_textdet.py`
- Modify: `pyproject.toml` (`dependencies`, `[project.optional-dependencies] test`)
- Test: `tests/markers/credits/test_textdet.py`

**Interfaces:**
- Consumes: Task 1 M1 (packages install on 3.12 and 3.14), M2 (model hashes); Task 1 Steps 2–3's
  `$MARKERS_BENCH_DIR/py312` venv and `$MARKERS_BENCH_DIR/textdet-model/` (wave 0: they exist before this lane starts;
  Steps 1–6 don't need them, Steps 7–8 do).
- Produces (Tasks 5, 7, 11 rely on these):
```python
# media_preview_generator/markers/credits/textdet.py   (imports cv2, onnxruntime, pyclipper at module top)
MODEL_FILE = "ch_PP-OCRv4_det_infer.onnx"; MODEL_SHA256: str; MODEL_SIZE = 4_745_517
FRAME_HEIGHT = 180; FRAME_WIDTH = 320; INTRA_OP_THREADS = 2
class TextDetError(Exception)
class ModelError(TextDetError)
def verify_model(path: str) -> None                                   # raises ModelError
def preprocess(image: np.ndarray) -> np.ndarray | None                # (H, W, 3) uint8 → (1, 3, H', W') float32
def postprocess(pred: np.ndarray, src_hw: tuple[int, int]) -> np.ndarray   # (1, 1, H', W') → (n, 4, 2) boxes
def cpu_session(model_path: str, intra_op_threads: int = INTRA_OP_THREADS) -> ort.InferenceSession
def webgpu_devices() -> list                                          # OrtEpDevice of the WebGPU plugin EP ([] if absent)
def webgpu_session(model_path: str, device, intra_op_threads: int = INTRA_OP_THREADS) -> ort.InferenceSession
class TextDetector:
    def __init__(self, session: ort.InferenceSession, *, backend: str) -> None     # backend "cpu" | "webgpu"
    backend: str
    def boxes(self, image: np.ndarray) -> np.ndarray
    def count(self, planes: np.ndarray) -> list[int]                  # (n, H, W) uint8 luma → boxes per frame
def synthetic_frames(count: int = 20) -> np.ndarray                   # (count, 180, 320) uint8, deterministic
```
```text
python -m tools.markers_eval.textdet_bench extract [--out DIR]                       → DIR/frames.npy (289, 180, 320); DIR defaults to $MARKERS_BENCH_DIR
python -m tools.markers_eval.textdet_bench counts --impl rapidocr|vendored --model M [--frames F] --out counts.json
python -m tools.markers_eval.textdet_bench compare A.json B.json                     → exit 0 when identical
```

- [ ] **Step 1: Dependencies**

`pyproject.toml` `dependencies`, after numpy:

```toml
    # Credit text detection (markers/credits/textdet.py). Imported only by its helper process, never by the web app.
    "onnxruntime==1.30.0",
    # No Linux aarch64 wheel: the arm64 image detects text on the CPU.
    "onnxruntime-ep-webgpu==0.3.0; platform_machine == 'x86_64'",
    "opencv-python-headless==5.0.0.93",
    "pyclipper==1.4.0",
```

and in `[project.optional-dependencies] test`:

```toml
    # tests/markers/credits/rapidocr_reference.py: the verbatim rapidocr post-processing uses shapely.
    "shapely==2.1.2",
```

Run (the pins only; never an editable install from a lane, which would point the shared venv at a worktree that is
later removed, preflight M11): `uv pip install --python /home/data/.venv/bin/python onnxruntime==1.30.0
opencv-python-headless==5.0.0.93 pyclipper==1.4.0 onnxruntime-ep-webgpu==0.3.0 shapely==2.1.2` then
`/home/data/.venv/bin/python -c "import onnxruntime, cv2, pyclipper, shapely, onnxruntime_ep_webgpu; print(onnxruntime.__version__, cv2.__version__)"`
Expected: `1.30.0 5.0.0`.

- [ ] **Step 2: Copy the reference (verbatim, test-only)**

`tests/markers/credits/rapidocr_reference.py`: copy from the rapidocr_onnxruntime 1.4.4 wheel (Task 1 M2 downloaded it
to `$MARKERS_BENCH_DIR/textdet-model/`; `unzip -p <wheel> rapidocr_onnxruntime/ch_ppocr_det/utils.py` and
`…/text_detect.py`) the classes `DetPreProcess` and `DBPostProcess` unchanged, and `TextDetector`'s `get_preprocess`,
`filter_tag_det_res`, `order_points_clockwise`, `clip_det_res` as module functions (drop `self`, pass the limit
settings as arguments). Keep the Apache-2.0 header of both files and add one line under it:
`# Verbatim test reference for media_preview_generator/markers/credits/textdet.py (rapidocr_onnxruntime 1.4.4).`
Add at the end:

```python
def reference_boxes(pred, ori_shape):
    """rapidocr 1.4.4's post-processing with the det config defaults (config.yaml ``Det``)."""
    post = DBPostProcess(thresh=0.3, box_thresh=0.5, max_candidates=1000, unclip_ratio=1.6, score_mode="fast",
                         use_dilation=True)  # fmt: skip
    boxes, _ = post(pred, ori_shape)
    return filter_tag_det_res(boxes, ori_shape)


def reference_preprocess(image):
    return get_preprocess(max(image.shape[0], image.shape[1]), "max", 320, [0.5, 0.5, 0.5], [0.5, 0.5, 0.5])(image)
```

(`get_preprocess(max_wh, limit_type, limit_side_len, mean, std)` returns `DetPreProcess(...)` with the 960/1500/2000
rule copied from `TextDetector.get_preprocess`.)

- [ ] **Step 3: Write the failing tests**

```python
# tests/markers/credits/test_textdet.py
"""The vendored detector against a verbatim copy of rapidocr_onnxruntime 1.4.4 on generated inputs, and its wiring."""

from __future__ import annotations

import hashlib
import os

import cv2
import numpy as np
import pytest
from shapely.geometry import Polygon

from media_preview_generator.markers.credits import textdet
from tests.markers.credits import rapidocr_reference as ref

SHAPES = ((192, 320), (96, 160), (64, 96))


def _probability_maps(count: int, seed: int = 241):
    rng = np.random.default_rng(seed)
    for _ in range(count):
        h, w = SHAPES[int(rng.integers(len(SHAPES)))]
        prob = (rng.random((h, w)) * 0.2).astype(np.float32)
        for _ in range(int(rng.integers(0, 12))):
            center = (float(rng.uniform(0, w)), float(rng.uniform(0, h)))
            size = (float(rng.uniform(1, 60)), float(rng.uniform(1, 20)))
            box = cv2.boxPoints((center, size, float(rng.uniform(-30, 30)))).astype(np.int32)
            cv2.fillPoly(prob, [box], float(rng.uniform(0.25, 0.95)))
        if rng.random() < 0.5:
            prob = cv2.GaussianBlur(prob, (3, 3), 0)
        yield prob[None, None, :, :], (int(rng.integers(90, 400)), int(rng.integers(160, 700)))


class TestAgainstRapidocr:
    @pytest.mark.parametrize("shape", [(180, 320, 3), (192, 320, 3), (1080, 1920, 3), (100, 50, 3), (2160, 3840, 3)])
    def test_preprocess_is_identical(self, shape):
        image = np.random.default_rng(7).integers(0, 256, shape, dtype=np.uint8)
        ours, theirs = textdet.preprocess(image), ref.reference_preprocess(image)
        assert ours.dtype == theirs.dtype == np.float32
        assert ours.shape == theirs.shape and np.array_equal(ours, theirs)

    def test_a_320x180_frame_is_fed_at_320x192(self):
        assert textdet.preprocess(np.zeros((180, 320, 3), np.uint8)).shape == (1, 3, 192, 320)

    def test_postprocess_gives_identical_boxes_on_2000_generated_maps(self):
        differing = []
        for n, (pred, src_hw) in enumerate(_probability_maps(2000)):
            ours, theirs = textdet.postprocess(pred, src_hw), ref.reference_boxes(pred, src_hw)
            if ours.shape != theirs.shape or not np.array_equal(ours, theirs):
                differing.append(n)
        assert differing == []

    def test_area_and_perimeter_equal_shapely(self):
        rng = np.random.default_rng(11)
        for _ in range(20_000):
            box = rng.uniform(0, 400, (4, 2)).astype(np.float32)
            box = cv2.boxPoints(cv2.minAreaRect(box))  # a real rotated rectangle, as the unclip step receives
            polygon = Polygon(box)
            assert textdet._area_and_length(box) == (polygon.area, polygon.length)


class FakeSession:
    def __init__(self, pred: np.ndarray) -> None:
        self.pred = pred
        self.feeds: list[np.ndarray] = []

    def get_inputs(self):
        return [type("Input", (), {"name": "x"})()]

    def run(self, outputs, feed):
        assert outputs is None
        self.feeds.append(feed["x"])
        return [self.pred]


class TestTextDetector:
    def test_count_feeds_each_luma_plane_as_three_equal_channels(self):
        pred = np.zeros((1, 1, 192, 320), np.float32)
        pred[0, 0, 40:60, 30:200] = 0.9
        session = FakeSession(pred)
        detector = textdet.TextDetector(session, backend="cpu")
        planes = np.stack([np.full((180, 320), 10, np.uint8), np.full((180, 320), 200, np.uint8)])
        assert detector.count(planes) == [1, 1]
        assert [f.shape for f in session.feeds] == [(1, 3, 192, 320)] * 2
        first = session.feeds[0][0]
        assert np.array_equal(first[0], first[1]) and np.array_equal(first[1], first[2])
        assert detector.backend == "cpu"

    def test_no_text_gives_zero(self):
        detector = textdet.TextDetector(FakeSession(np.zeros((1, 1, 192, 320), np.float32)), backend="webgpu")
        assert detector.count(np.zeros((3, 180, 320), np.uint8)) == [0, 0, 0]


class TestModel:
    def test_wrong_size_or_hash_is_refused(self, tmp_path, monkeypatch):
        model = tmp_path / "m.onnx"
        model.write_bytes(b"not a model")
        with pytest.raises(textdet.ModelError, match="isn't the expected file"):
            textdet.verify_model(str(model))
        monkeypatch.setattr(textdet, "MODEL_SIZE", len(b"not a model"))
        monkeypatch.setattr(textdet, "MODEL_SHA256", hashlib.sha256(b"not a model").hexdigest())
        textdet.verify_model(str(model))
        with pytest.raises(textdet.ModelError, match="isn't at"):
            textdet.verify_model(str(tmp_path / "missing.onnx"))


def test_synthetic_frames_are_deterministic_and_mixed():
    first, second = textdet.synthetic_frames(20), textdet.synthetic_frames(20)
    assert first.shape == (20, 180, 320) and first.dtype == np.uint8
    assert np.array_equal(first, second)
    means = first.reshape(20, -1).mean(axis=1)
    assert (means < 30).sum() >= 8 and (means >= 30).sum() >= 8


@pytest.mark.integration
def test_real_model_finds_text_on_the_dark_synthetic_cards():
    path = os.environ.get("MEDIA_PREVIEW_TEXTDET_MODEL", "/app/models/ch_PP-OCRv4_det_infer.onnx")
    if not os.path.isfile(path):
        pytest.skip("no text detection model (set MEDIA_PREVIEW_TEXTDET_MODEL)")
    detector = textdet.TextDetector(textdet.cpu_session(path), backend="cpu")
    frames = textdet.synthetic_frames(20)
    counts = detector.count(frames)
    dark = [c for c, f in zip(counts, frames, strict=True) if f.mean() < 30]
    assert all(c >= 1 for c in dark)
```

Run: `/home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/markers/credits/test_textdet.py -q`
Expected: FAIL — `ImportError: cannot import name 'textdet'`.

- [ ] **Step 4: Implement `textdet.py`**

```python
# media_preview_generator/markers/credits/textdet.py
# Pre- and post-processing below are adapted from rapidocr_onnxruntime 1.4.4 (ch_ppocr_det/text_detect.py and
# ch_ppocr_det/utils.py): Copyright (c) 2020 PaddlePaddle Authors, Licensed under the Apache License, Version 2.0
# (http://www.apache.org/licenses/LICENSE-2.0). See PP-OCRv4-det-NOTICE.txt.
"""Text boxes on credit frames: PP-OCRv4's detection model on ONNX Runtime (spec §5.4).

Vendored because rapidocr_onnxruntime 1.4.4 caps Python below 3.13 and is no longer maintained. It must give the same
boxes: ``tests/markers/credits/test_textdet.py`` compares it with a verbatim copy on generated inputs and
``tools/markers_eval/textdet_bench.py`` on the 289-frame bench set. Left out: recognition, angle classification, the
"slow" box score. Shapely's polygon area and length are computed directly (a box is a four-point polygon).

Imported only by the text detection helper process, the harness, the bench and tests: never by the web app.
"""

from __future__ import annotations

import hashlib
import math
import os

import cv2
import numpy as np
import onnxruntime as ort
import pyclipper

MODEL_FILE = "ch_PP-OCRv4_det_infer.onnx"
MODEL_SHA256 = "d2a7720d45a54257208b1e13e36a8479894cb74155a5efe29462512d42f49da9"
MODEL_SIZE = 4_745_517
FRAME_HEIGHT = 180
FRAME_WIDTH = 320
INTRA_OP_THREADS = 2
MEAN = (0.5, 0.5, 0.5)
STD = (0.5, 0.5, 0.5)
THRESH = 0.3
BOX_THRESH = 0.5
MAX_CANDIDATES = 1000
UNCLIP_RATIO = 1.6
MIN_SIZE = 3
_DILATION_KERNEL = np.array([[1, 1], [1, 1]])
_webgpu_registered = False


class TextDetError(Exception):
    """Text detection can't run."""


class ModelError(TextDetError):
    """The model file is missing or isn't the pinned file."""


def verify_model(path: str) -> None:
    """Check the model is the pinned file (size, then sha256).

    Raises:
        ModelError: Missing, or another file.
    """
    if not os.path.isfile(path):
        raise ModelError(f"Needs the text detection model, which the Docker image includes; it isn't at {path}")
    if os.path.getsize(path) != MODEL_SIZE:
        raise ModelError(f"The text detection model at {path} isn't the expected file")
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    if digest.hexdigest() != MODEL_SHA256:
        raise ModelError(f"The text detection model at {path} isn't the expected file")


def _resize_limit(max_side: int) -> int:
    # rapidocr's TextDetector.get_preprocess: under limit type "max" the configured 320 is replaced by 960, 1500 or
    # 2000, so frames under 960 px keep their own size (spec §5.4's "at the frame's own 320 px").
    if max_side < 960:
        return 960
    return 1500 if max_side < 1500 else 2000


def preprocess(image: np.ndarray) -> np.ndarray | None:
    """rapidocr's DetPreProcess under limit type "max": resize to multiples of 32, normalise, CHW, batch of one.

    Args:
        image: (H, W, 3) uint8.

    Returns:
        (1, 3, H', W') float32, or None when a side would be 0.
    """
    h, w = image.shape[:2]
    limit = _resize_limit(max(h, w))
    ratio = (float(limit) / h if h > w else float(limit) / w) if max(h, w) > limit else 1.0
    resize_h = int(round(int(h * ratio) / 32) * 32)
    resize_w = int(round(int(w * ratio) / 32) * 32)
    if resize_w <= 0 or resize_h <= 0:
        return None
    resized = cv2.resize(image, (resize_w, resize_h))
    normalised = (resized.astype("float32") * (1 / 255.0) - np.array(MEAN)) / np.array(STD)
    return np.expand_dims(normalised.transpose((2, 0, 1)), axis=0).astype(np.float32)


def _mini_box(contour: np.ndarray) -> tuple[np.ndarray, float]:
    bounding_box = cv2.minAreaRect(contour)
    points = sorted(list(cv2.boxPoints(bounding_box)), key=lambda x: x[0])
    first, fourth = (0, 1) if points[1][1] > points[0][1] else (1, 0)
    second, third = (2, 3) if points[3][1] > points[2][1] else (3, 2)
    return np.array([points[first], points[second], points[third], points[fourth]]), min(bounding_box[1])


def _box_score_fast(bitmap: np.ndarray, points: np.ndarray) -> float:
    h, w = bitmap.shape[:2]
    box = points.copy()
    xmin = np.clip(np.floor(box[:, 0].min()).astype(np.int32), 0, w - 1)
    xmax = np.clip(np.ceil(box[:, 0].max()).astype(np.int32), 0, w - 1)
    ymin = np.clip(np.floor(box[:, 1].min()).astype(np.int32), 0, h - 1)
    ymax = np.clip(np.ceil(box[:, 1].max()).astype(np.int32), 0, h - 1)
    mask = np.zeros((ymax - ymin + 1, xmax - xmin + 1), dtype=np.uint8)
    box[:, 0] = box[:, 0] - xmin
    box[:, 1] = box[:, 1] - ymin
    cv2.fillPoly(mask, box.reshape(1, -1, 2).astype(np.int32), 1)
    return cv2.mean(bitmap[ymin : ymax + 1, xmin : xmax + 1], mask)[0]


def _area_and_length(points: np.ndarray) -> tuple[float, float]:
    """A simple polygon's area and perimeter with the same float operations as shapely 2.1 (GEOS): shoelace relative to
    the first x, and summed ``sqrt(dx² + dy²)``. Other orderings (``np.hypot``, the textbook shoelace) differ in the last
    bit on 9–347 of 20,000 boxes, which moves pyclipper's offset."""
    ring = [(float(p[0]), float(p[1])) for p in points]
    ring.append(ring[0])
    x0 = ring[0][0]
    area = 0.0
    for i in range(1, len(ring) - 1):
        area += (ring[i][0] - x0) * (ring[i - 1][1] - ring[i + 1][1])
    length = 0.0
    for i in range(1, len(ring)):
        dx, dy = ring[i][0] - ring[i - 1][0], ring[i][1] - ring[i - 1][1]
        length += math.sqrt(dx * dx + dy * dy)
    return abs(area / 2.0), length


def _unclip(box: np.ndarray) -> np.ndarray:
    area, length = _area_and_length(box)
    offset = pyclipper.PyclipperOffset()
    offset.AddPath(box, pyclipper.JT_ROUND, pyclipper.ET_CLOSEDPOLYGON)
    return np.array(offset.Execute(area * UNCLIP_RATIO / length)).reshape((-1, 1, 2))


def _boxes_from_bitmap(pred: np.ndarray, bitmap: np.ndarray, dest_width: int, dest_height: int) -> np.ndarray:
    height, width = bitmap.shape
    found = cv2.findContours((bitmap * 255).astype(np.uint8), cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    contours = found[1] if len(found) == 3 else found[0]
    boxes = []
    for contour in contours[:MAX_CANDIDATES]:
        points, short_side = _mini_box(contour)
        if short_side < MIN_SIZE:
            continue
        if BOX_THRESH > _box_score_fast(pred, points.reshape(-1, 2)):
            continue
        box, short_side = _mini_box(_unclip(points))
        if short_side < MIN_SIZE + 2:
            continue
        box[:, 0] = np.clip(np.round(box[:, 0] / width * dest_width), 0, dest_width)
        box[:, 1] = np.clip(np.round(box[:, 1] / height * dest_height), 0, dest_height)
        boxes.append(box.astype(np.int32))
    return np.array(boxes, dtype=np.int32)


def _order_points_clockwise(pts: np.ndarray) -> np.ndarray:
    x_sorted = pts[np.argsort(pts[:, 0]), :]
    left, right = x_sorted[:2, :], x_sorted[2:, :]
    top_left, bottom_left = left[np.argsort(left[:, 1]), :]
    top_right, bottom_right = right[np.argsort(right[:, 1]), :]
    return np.array([top_left, top_right, bottom_right, bottom_left], dtype="float32")


def _clip(points: np.ndarray, img_height: int, img_width: int) -> np.ndarray:
    for n in range(points.shape[0]):
        points[n, 0] = int(min(max(points[n, 0], 0), img_width - 1))
        points[n, 1] = int(min(max(points[n, 1], 0), img_height - 1))
    return points


def postprocess(pred: np.ndarray, src_hw: tuple[int, int]) -> np.ndarray:
    """rapidocr's DBPostProcess (threshold 0.3, box threshold 0.5, unclip 1.6, dilation, fast score) and its box filter.

    Args:
        pred: The model's probability map, (1, 1, H', W').
        src_hw: The original image's height and width.

    Returns:
        The kept boxes, (n, 4, 2) float32 (empty array when none).
    """
    src_h, src_w = src_hw
    prob = pred[:, 0, :, :]
    mask = cv2.dilate(np.array(prob[0] > THRESH).astype(np.uint8), _DILATION_KERNEL)
    kept = []
    for box in _boxes_from_bitmap(prob[0], mask, src_w, src_h):
        box = _clip(_order_points_clockwise(box), src_h, src_w)
        if int(np.linalg.norm(box[0] - box[1])) <= 3 or int(np.linalg.norm(box[0] - box[3])) <= 3:
            continue
        kept.append(box)
    return np.array(kept)


def _session_options(intra_op_threads: int) -> ort.SessionOptions:
    # rapidocr's OrtInferSession options: graph optimisations and arena settings change the numbers at the margin.
    opts = ort.SessionOptions()
    opts.log_severity_level = 4
    opts.enable_cpu_mem_arena = False
    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    opts.intra_op_num_threads = intra_op_threads
    opts.inter_op_num_threads = 1
    return opts


def cpu_session(model_path: str, intra_op_threads: int = INTRA_OP_THREADS) -> ort.InferenceSession:
    """An ONNX Runtime CPU session of the model (verified first).

    Raises:
        ModelError: The model is missing or not the pinned file.
    """
    verify_model(model_path)
    return ort.InferenceSession(
        model_path,
        sess_options=_session_options(intra_op_threads),
        providers=[("CPUExecutionProvider", {"arena_extend_strategy": "kSameAsRequested"})],
    )


def webgpu_devices() -> list:
    """The WebGPU plugin EP's devices (``OrtEpDevice``), or [] when the plugin isn't installed (arm64)."""
    global _webgpu_registered
    try:
        import onnxruntime_ep_webgpu as webgpu_ep
    except ImportError:
        return []
    if not _webgpu_registered:
        ort.register_execution_provider_library("webgpu", webgpu_ep.get_library_path())
        _webgpu_registered = True
    return [d for d in ort.get_ep_devices() if d.ep_name == webgpu_ep.get_ep_name()]


def webgpu_session(model_path: str, device, intra_op_threads: int = INTRA_OP_THREADS) -> ort.InferenceSession:
    """A session of the model on one WebGPU EP device (the caller applied the Vulkan env before this process started).

    Raises:
        ModelError: The model is missing or not the pinned file.
    """
    verify_model(model_path)
    opts = _session_options(intra_op_threads)
    opts.add_provider_for_devices([device], {})
    return ort.InferenceSession(model_path, sess_options=opts)


class TextDetector:
    """Box counts per frame from one ONNX Runtime session."""

    def __init__(self, session: ort.InferenceSession, *, backend: str) -> None:
        """Wrap a session.

        Args:
            session: From :func:`cpu_session` or :func:`webgpu_session`.
            backend: ``cpu`` or ``webgpu`` (for logs and the helper's ready line).
        """
        self._session = session
        self._input = session.get_inputs()[0].name
        self.backend = backend

    def boxes(self, image: np.ndarray) -> np.ndarray:
        """Text boxes on one (H, W, 3) uint8 image."""
        tensor = preprocess(image)
        if tensor is None:
            return np.zeros((0, 4, 2), dtype=np.float32)
        return postprocess(self._session.run(None, {self._input: tensor})[0], image.shape[:2])

    def count(self, planes: np.ndarray) -> list[int]:
        """Box counts for (n, H, W) uint8 luma planes, each fed as three equal channels (as rule J was measured)."""
        return [len(self.boxes(np.stack([plane] * 3, axis=-1))) for plane in planes]


def synthetic_frames(count: int = 20) -> np.ndarray:
    """Deterministic 320×180 luma frames for the GPU self-test: dark cards with white names, bright gradients with and
    without a caption. No real footage."""
    frames = np.zeros((count, FRAME_HEIGHT, FRAME_WIDTH), dtype=np.uint8)
    for i in range(count):
        frame = frames[i]
        if i % 2 == 0:
            frame[:] = 8
            for line in range(1 + i % 4):
                cv2.putText(frame, f"NAME {i:02d} {line}", (40 + 3 * line, 40 + 30 * line), cv2.FONT_HERSHEY_SIMPLEX,
                            0.6, 235, 1, cv2.LINE_AA)  # fmt: skip
        else:
            frame[:] = np.linspace(60, 200, FRAME_WIDTH, dtype=np.uint8)[None, :]
            if i % 3 == 0:
                cv2.putText(frame, "CAPTION", (100, 160), cv2.FONT_HERSHEY_DUPLEX, 0.7, 20, 2, cv2.LINE_AA)
    return frames
```

`PP-OCRv4-det-NOTICE.txt` (plain text so `.dockerignore`'s `*.md` rule doesn't drop it):

```text
The text detection model ch_PP-OCRv4_det_infer.onnx (PP-OCRv4, PaddlePaddle Authors, Apache License 2.0) is taken
unchanged from the rapidocr_onnxruntime 1.4.4 wheel on PyPI (RapidAI, Apache License 2.0) at image build time and
verified by sha256 (d2a7720d45a54257208b1e13e36a8479894cb74155a5efe29462512d42f49da9).
textdet.py adapts the detection pre- and post-processing of rapidocr_onnxruntime 1.4.4 (ch_ppocr_det/text_detect.py,
ch_ppocr_det/utils.py; Copyright (c) 2020 PaddlePaddle Authors; Licensed under the Apache License, Version 2.0,
http://www.apache.org/licenses/LICENSE-2.0).
```

- [ ] **Step 5: Run the tests**

Run: `/home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/markers/credits/test_textdet.py -q`
Expected: PASS (the integration test is deselected by default). While planning, this exact code and test file ran
against the wheel's verbatim `utils.py` in a Python 3.14 venv (onnxruntime 1.30.0, opencv-python-headless 5.0.0.93,
pyclipper 1.4.0, shapely 2.1.2): 0 of 2,000 maps differed and area/length matched shapely on 20,000 boxes.
**If `test_area_and_perimeter_equal_shapely` or the 2,000-map test fails** on another platform, apply T-R2's fallback: add `"shapely==2.1.2"` to runtime `dependencies`, compute
`poly = Polygon(box); area, length = poly.area, poly.length` in `_unclip`, delete `_area_and_length` and its test,
rerun, and record the fallback in the task report.

Mutation checks (each must fail a test; revert after): `BOX_THRESH = 0.51`; drop `.astype(np.float32)` in
`preprocess`; `UNCLIP_RATIO = 1.5`; the dilation removed.

- [ ] **Step 6: Write the bench tool**

```python
# tools/markers_eval/textdet_bench.py
"""The 289-frame text detection bench (spec §5.4): the vendored detector must count the same boxes as
rapidocr_onnxruntime 1.4.4 on every frame. Local-only data: the frames come from real files and are never committed.

    python -m tools.markers_eval.textdet_bench extract [--out DIR]
    python -m tools.markers_eval.textdet_bench counts --impl rapidocr|vendored --model M [--frames F] --out counts.json
    python -m tools.markers_eval.textdet_bench compare A.json B.json

``extract`` repeats ``evidence/credits/gpu/extract.py``: every 10th file of ``credits/f3.jsonl`` (8 files), CPU
keyframes of the 120 s around the credits truth, scaled to 320×180 grey. ``counts --impl rapidocr`` needs
rapidocr_onnxruntime 1.4.4 (Python ≤ 3.12: run it in Task 1's venv, ``$MARKERS_BENCH_DIR/py312``, with this repo on
``PYTHONPATH``). The frames live in ``$MARKERS_BENCH_DIR`` (default: the evidence folder's git-ignored ``credits/bench``),
a stable local path every lane and later session reads.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np

from .data import evidence_dir

W, H = 320, 180
EXPECTED_FRAMES = 289


def _default_dir() -> Path:
    root = Path(os.environ.get("MARKERS_BENCH_DIR") or evidence_dir() / "credits/bench")
    if str(root.resolve()).startswith("/data"):
        raise SystemExit("the bench folder must not live under /data*")
    return root


def extract(out: Path) -> Path:
    items = [json.loads(line) for line in (evidence_dir() / "credits/f3.jsonl").read_text().splitlines()][::10]
    frames = []
    for item in items:
        start = max(0.0, item["truth"] - 60)
        raw = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-threads", "2", "-skip_frame", "nokey", "-ss", f"{start:.2f}",
             "-t", "120", "-i", item["file"], "-an", "-sn", "-dn", "-fps_mode", "passthrough", "-vf",
             f"scale={W}:{H},format=gray", "-f", "rawvideo", "-"],
            capture_output=True, check=True,
        ).stdout  # fmt: skip
        frames.append(np.frombuffer(raw, np.uint8)[: len(raw) // (W * H) * W * H].reshape(-1, H, W))
    stacked = np.concatenate(frames)
    if len(stacked) != EXPECTED_FRAMES:
        raise SystemExit(f"extracted {len(stacked)} frames, the 2026-09-13 bench had {EXPECTED_FRAMES}")
    out.mkdir(parents=True, exist_ok=True)
    np.save(out / "frames.npy", stacked)
    return out / "frames.npy"


def counts(impl: str, model: str, frames_path: Path) -> list[int]:
    frames = np.load(frames_path)
    if impl == "rapidocr":
        from rapidocr_onnxruntime import RapidOCR

        det = RapidOCR(det_limit_side_len=320, det_limit_type="max", intra_op_num_threads=2, inter_op_num_threads=1,
                       det_model_path=model).text_det  # fmt: skip
        return [0 if (b := det(np.stack([f] * 3, axis=-1))[0]) is None else len(b) for f in frames]
    from media_preview_generator.markers.credits import textdet

    return textdet.TextDetector(textdet.cpu_session(model), backend="cpu").count(frames)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.markers_eval.textdet_bench")
    sub = parser.add_subparsers(dest="command", required=True)
    ex = sub.add_parser("extract")
    ex.add_argument("--out", type=Path, default=None)
    co = sub.add_parser("counts")
    co.add_argument("--impl", choices=("rapidocr", "vendored"), required=True)
    co.add_argument("--model", required=True)
    co.add_argument("--frames", type=Path, default=None)
    co.add_argument("--out", type=Path, required=True)
    cmp = sub.add_parser("compare")
    cmp.add_argument("a", type=Path)
    cmp.add_argument("b", type=Path)
    args = parser.parse_args(argv)
    for out in (getattr(args, "out", None),):
        if out is not None and str(out.resolve()).startswith("/data"):
            raise SystemExit("bench output must not be written under /data*")
    if args.command == "extract":
        print(extract(args.out or _default_dir()))
        return 0
    if args.command == "counts":
        result = counts(args.impl, args.model, args.frames or _default_dir() / "frames.npy")
        args.out.write_text(json.dumps({"impl": args.impl, "frames": len(result), "counts": result}))
        print(f"{args.impl}: {len(result)} frames, {sum(c > 0 for c in result)} with text")
        return 0
    a, b = (json.loads(p.read_text())["counts"] for p in (args.a, args.b))
    differing = [i for i, (x, y) in enumerate(zip(a, b, strict=True)) if x != y]
    print(f"{len(a) - len(differing)} of {len(a)} frames identical; differing frames: {differing}")
    return 0 if len(a) == len(b) == EXPECTED_FRAMES and not differing else 1


if __name__ == "__main__":
    sys.exit(main())
```

(`RapidOCR(det_model_path=…)` is how 1.4.4 takes a model path through its keyword parameters; if it doesn't accept it,
pass nothing — the wheel's own model has the same sha256.)

- [ ] **Step 7: Run the bench gate (storage, local data)**

```bash
cd /home/data/workspace/plex_generate_vid_previews   # or the lane worktree, with MARKERS_EVAL_EVIDENCE set
B="$MARKERS_BENCH_DIR"; M="$B/textdet-model/ch_PP-OCRv4_det_infer.onnx"; mkdir -p "$B/counts"
nice -n 19 /home/data/.venv/bin/python -m tools.markers_eval.textdet_bench extract
PYTHONPATH="$PWD" nice -n 19 "$B/py312/bin/python" -m tools.markers_eval.textdet_bench counts --impl rapidocr --model "$M" --out "$B/counts/rapidocr.json"
PYTHONPATH="$PWD" nice -n 19 "$B/py312/bin/python" -m tools.markers_eval.textdet_bench counts --impl vendored --model "$M" --out "$B/counts/vendored-py312.json"
nice -n 19 /home/data/.venv/bin/python -m tools.markers_eval.textdet_bench counts --impl vendored --model "$M" --out "$B/counts/vendored-py314.json"
/home/data/.venv/bin/python -m tools.markers_eval.textdet_bench compare "$B/counts/rapidocr.json" "$B/counts/vendored-py312.json"
/home/data/.venv/bin/python -m tools.markers_eval.textdet_bench compare "$B/counts/rapidocr.json" "$B/counts/vendored-py314.json"
```

Expected: `extract` prints the frames path (289 frames); both compares print `289 of 289 frames identical;
differing frames: []` and exit 0. The py312 compare is the gate (same ONNX Runtime build on both sides); a py314
difference is reported, not a failure, unless the py312 one also differs. Paste both compare lines into the task
report (they go into `phase3-harness.md` in Task 11).

- [ ] **Step 8: Integration test with the real model**

Run: `MEDIA_PREVIEW_TEXTDET_MODEL="$MARKERS_BENCH_DIR/textdet-model/ch_PP-OCRv4_det_infer.onnx" /home/data/.venv/bin/python -m pytest --no-cov -n 0 -m integration tests/markers/credits/test_textdet.py -q`
Expected: PASS.

- [ ] **Step 9: Commit**

Stage the seven files and `pyproject.toml`; Architecture Review on the staged diff; then
`PATH="/home/data/.venv/bin:$PATH" git commit -m "feat(markers): vendored PP-OCRv4 text detector with rapidocr equivalence tests"`.

---
## Task 9: Decision rules for credits text (matrix)

`[lane-parallel]` `[high-risk]` — spec §5.4 ("Credits text alone is one source: under the default publish rule it needs
a second source"), §5.5 rules 2 (sanity), 3 (chapter veto), 4 (agreement, times from the first source in order, safer
other edge), 6 (Medium), 7 (server markers: agreement only, may shorten), 8 (importer copies), 10 (preview overlap),
11; owner answers Q1, Q2, Q3; contradiction C1; preflight M8 (write the owner's rulings into the spec before the
tasks that are reviewed against it), M9 (the "owner says no" variants are moot).

**OWNER DECISION (2026-09-16), Q1:** credits text may publish alone at Medium (High needs agreement).
**OWNER DECISION (2026-09-16), Q2:** credits text and a server's own credits marker are independent sources (rule 7
still shortens).
**OWNER DECISION (2026-09-16), Q3:** a credits text candidate ends at the roll's last credit frame when more than 30 s
of the file follows it; in a cluster, the earliest end wins (rule 4's safer other edge, unchanged code).

`decide.py` already treats `Source.CREDITS_TEXT` as an independent source that may decide alone at Medium, and takes
the earliest end of a credits cluster; nothing tests a credits-text cell today. This task pins every cell with the
owner's answers. The only production change is documentation: comments in `decide.py`, and the owner's rulings
written into the spec in this task's commit, so Tasks 8 and 10 are reviewed against a spec that already says them.
Every expectation in the test file was run against the branch's `decide.py` while planning (41 passed).

**Files:**
- Create: `tests/markers/test_decide_credits_text.py`
- Modify: `media_preview_generator/markers/decide.py` (the `_AGREEMENT_ONLY` comment ~43–46 and `_may_decide_alone`
  docstring ~456; no code), `docs/design/intro-credits/spec.md` (§5.4 the credits text paragraph ~311, §5.5 rule 6
  ~344, §14 dated lines)
- Test: `tests/markers/test_decide_credits_text.py`, `tests/markers/test_decide.py` (must pass unchanged)

**Interfaces:**
- Consumes: `decide(candidates, DecisionContext, locked)`, `DecisionStatus`, `Candidate`, `Source` (phase 1).
- Produces: no new code. Task 8's pipeline tests (`test_medium_publishes_it_alone_q1`, `test_a_scene_after_the_credits_is_kept_q3`),
  Task 11's harness rows and Task 12's docs rest on these cells; spec §5.4/§5.5/§14 carry the rulings from this commit.

- [ ] **Step 1: Write the matrix**

```python
# tests/markers/test_decide_credits_text.py
"""Credits text (spec §5.4) in the decision rules (§5.5): every cell where it meets chapters, SkipDB, markers already on
servers, the publish setting, sanity bounds and the preview overlap check. The owner's answers of 2026-09-16 are pinned:
Q1 (it publishes alone at Medium), Q2 (it and a server's own marker are independent), Q3 (its end, when a scene follows
the roll, is the safer end of any cluster it confirms)."""

from __future__ import annotations

from itertools import permutations

import pytest

from media_preview_generator.markers.decide import DecisionContext, DecisionStatus, decide
from media_preview_generator.markers.models import Candidate, MarkerType, Source

T = MarkerType
MOVIE_MS = 6_000_000
EPISODE_MS = 1_320_000
ORDER = ("chapters", "theintrodb", "introdb", "skipdb", "season_audio", "season_audio_previous", "credits_text",
         "server_markers", "server_markers_imported")  # fmt: skip
TEXT_FIRST = ("chapters", "credits_text", "theintrodb", "introdb", "skipdb", "season_audio", "season_audio_previous",
              "server_markers", "server_markers_imported")  # fmt: skip


def text(start_ms: int, end_ms: int | None = None) -> Candidate:
    return Candidate(T.CREDITS, start_ms, end_ms, Source.CREDITS_TEXT)


def skipdb(start_ms: int, end_ms: int | None = None) -> Candidate:
    return Candidate(T.CREDITS, start_ms, end_ms, Source.SKIPDB)


def plex(start_ms: int, end_ms: int | None = MOVIE_MS, origin: str = "plex-1") -> Candidate:
    return Candidate(T.CREDITS, start_ms, end_ms, Source.SERVER_MARKERS, 1.0, origin)


def chapter(start_ms: int) -> Candidate:
    return Candidate(T.CREDITS, start_ms, None, Source.CHAPTERS, 1.0, "End Credits")


def credits(candidates, *, level="high", duration=MOVIE_MS, is_movie=True, order=ORDER, types=(T.CREDITS,)):
    ctx = DecisionContext(duration, is_movie, level, frozenset(types), order)
    return decide(list(candidates), ctx, {})


class TestAlone:
    def test_high_needs_a_second_source(self):
        d = credits([text(5_700_000)])[T.CREDITS]
        assert (d.status, d.reason) == (DecisionStatus.NEEDS_REVIEW, "sources don't agree yet")
        assert (d.proposed.start_ms, d.proposed.end_ms, d.proposed.decided_by) == (5_700_000, MOVIE_MS, ("credits_text",))

    def test_medium_publishes_it_alone_q1(self):
        d = credits([text(5_700_000)], level="medium")[T.CREDITS]
        assert (d.status, d.reason) == (DecisionStatus.DECIDED, "single source (credits_text)")
        assert (d.marker.start_ms, d.marker.end_ms, d.marker.decided_by) == (5_700_000, MOVIE_MS, ("credits_text",))

    @pytest.mark.parametrize("level", ["high", "medium"])
    def test_an_episode_alone_follows_the_same_rule(self, level):
        d = credits([text(1_250_000)], level=level, duration=EPISODE_MS, is_movie=False)[T.CREDITS]
        expected = DecisionStatus.DECIDED if level == "medium" else DecisionStatus.NEEDS_REVIEW
        assert d.status is expected


class TestSanity:
    @pytest.mark.parametrize(
        ("start_ms", "duration", "is_movie"),
        [
            (5_000_000, MOVIE_MS, True),     # more than 900 s before the end of a movie
            (4_400_000, MOVIE_MS, False),    # before the last 25 %
            (900_000, EPISODE_MS, False),    # an episode's credits at 68 %
            (5_998_000, MOVIE_MS, True),     # shorter than 3 s
        ],
    )  # fmt: skip
    def test_fails_sanity(self, start_ms, duration, is_movie):
        d = credits([text(start_ms)], level="medium", duration=duration, is_movie=is_movie)[T.CREDITS]
        assert (d.status, d.reason) == (DecisionStatus.NO_EVIDENCE, "1 candidate(s) failed sanity checks")


class TestWithSkipDb:
    def test_agreeing_within_10_s_decides_with_the_first_source_in_order(self):
        d = credits([text(5_700_000), skipdb(5_695_000, 5_990_000)])[T.CREDITS]
        assert d.status is DecisionStatus.DECIDED
        assert (d.marker.start_ms, d.marker.end_ms, d.marker.decided_by) == (5_695_000, 5_990_000, ("skipdb", "credits_text"))

    def test_the_users_order_picks_whose_start_is_published(self):
        d = credits([text(5_700_000), skipdb(5_695_000, 5_990_000)], order=TEXT_FIRST)[T.CREDITS]
        assert (d.marker.start_ms, d.marker.end_ms, d.marker.decided_by) == (5_700_000, 5_990_000, ("credits_text", "skipdb"))

    @pytest.mark.parametrize("level", ["high", "medium"])
    def test_disagreeing_goes_to_review(self, level):
        d = credits([text(5_700_000), skipdb(5_730_000)], level=level)[T.CREDITS]
        assert (d.status, d.reason) == (DecisionStatus.NEEDS_REVIEW, "sources disagree: credits_text, skipdb")


class TestWithServerMarkers:
    def test_a_servers_own_marker_agreeing_decides_but_never_supplies_the_start_q2(self):
        d = credits([text(5_700_000), plex(5_705_000)])[T.CREDITS]
        assert d.status is DecisionStatus.DECIDED
        assert (d.marker.start_ms, d.marker.end_ms, d.marker.decided_by) == (5_700_000, MOVIE_MS, ("credits_text", "server_markers"))
        assert d.reason == "sources agree: credits_text, server_markers"

    def test_a_servers_earlier_credits_end_shortens_the_skip(self):
        d = credits([text(5_700_000), plex(5_705_000, 5_950_000)])[T.CREDITS]
        assert (d.marker.start_ms, d.marker.end_ms) == (5_700_000, 5_950_000)

    @pytest.mark.parametrize("level", ["high", "medium"])
    def test_a_server_marker_25_s_later_contradicts_it(self, level):
        d = credits([text(5_700_000), plex(5_725_000)], level=level)[T.CREDITS]
        assert (d.status, d.reason) == (DecisionStatus.NEEDS_REVIEW, "sources disagree: credits_text, server_markers")

    def test_rule_7_moves_a_decided_start_to_the_servers_later_first_start(self):
        # Text + SkipDB decide at 5 700 s; the server's own credits start 40 s later: its start wins (a shorter skip).
        d = credits([text(5_700_000), skipdb(5_702_000), plex(5_740_000)], order=TEXT_FIRST)[T.CREDITS]
        assert d.status is DecisionStatus.DECIDED
        assert d.marker.start_ms == 5_740_000
        assert d.reason.endswith("start shortened to the server's own marker (plex-1)")

    def test_an_importer_plugins_markers_agree_like_any_independent_source(self):
        imported = Candidate(T.CREDITS, 5_702_000, None, Source.SERVER_MARKERS_IMPORTED, 1.0, "jellyfin-1")
        d = credits([text(5_700_000), imported])[T.CREDITS]
        assert d.status is DecisionStatus.DECIDED
        assert (d.marker.start_ms, d.marker.decided_by) == (5_700_000, ("credits_text", "server_markers_imported"))


class TestWithChapters:
    def test_an_agreeing_chapter_decides(self):
        d = credits([chapter(5_698_000), text(5_700_000)])[T.CREDITS]
        assert (d.status, d.reason, d.marker.start_ms, d.marker.decided_by) == (DecisionStatus.DECIDED, "chapters", 5_698_000, ("chapters",))

    def test_credits_text_alone_never_overrides_a_chapter(self):
        d = credits([chapter(5_640_000), text(5_700_000)], level="medium")[T.CREDITS]
        assert (d.status, d.marker.start_ms, d.marker.decided_by) == (DecisionStatus.DECIDED, 5_640_000, ("chapters",))

    def test_text_and_a_servers_marker_agreeing_against_the_chapter_send_it_to_review(self):
        d = credits([chapter(5_640_000), text(5_700_000), plex(5_703_000)])[T.CREDITS]
        assert d.status is DecisionStatus.NEEDS_REVIEW
        assert d.reason == "chapters contradicted by agreeing sources: credits_text, server_markers"

    def test_text_and_skipdb_agreeing_against_the_chapter_send_it_to_review(self):
        d = credits([chapter(5_640_000), text(5_700_000), skipdb(5_698_000)])[T.CREDITS]
        assert (d.status, d.reason) == (DecisionStatus.NEEDS_REVIEW, "chapters contradicted by agreeing sources: skipdb, credits_text")


class TestEndQ3:
    """Owner, Q3: a credits text candidate ends at the roll's last credit frame when more than 30 s follows it."""

    def test_alone_at_medium_the_skip_stops_before_the_scene(self):
        d = credits([text(5_700_000, 5_900_000)], level="medium")[T.CREDITS]
        assert (d.status, d.marker.start_ms, d.marker.end_ms, d.marker.decided_by) == (DecisionStatus.DECIDED, 5_700_000, 5_900_000, ("credits_text",))

    def test_an_open_ended_roll_skips_to_the_end_of_the_file(self):
        d = credits([text(5_700_000)], level="medium")[T.CREDITS]
        assert d.marker.end_ms == MOVIE_MS

    @pytest.mark.parametrize("skipdb_end", [5_990_000, None])
    def test_its_earlier_end_shortens_an_agreeing_skipdb_answer(self, skipdb_end):
        d = credits([text(5_700_000, 5_900_000), skipdb(5_695_000, skipdb_end)])[T.CREDITS]
        assert (d.marker.start_ms, d.marker.end_ms, d.marker.decided_by) == (5_695_000, 5_900_000, ("skipdb", "credits_text"))

    def test_its_end_wins_over_a_servers_final_credits(self):
        d = credits([text(5_700_000, 5_900_000), plex(5_705_000)])[T.CREDITS]
        assert (d.status, d.marker.end_ms) == (DecisionStatus.DECIDED, 5_900_000)

    def test_a_servers_earlier_end_still_shortens_it(self):
        d = credits([text(5_700_000, 5_950_000), plex(5_705_000, 5_900_000)])[T.CREDITS]
        assert d.marker.end_ms == 5_900_000

    def test_a_chapter_decision_keeps_the_chapters_end(self):
        # A normal run never reads credits text for credits a chapter decided (Task 8); a forced run's text end doesn't
        # move the chapter's.
        d = credits([chapter(5_698_000), text(5_700_000, 5_900_000)], level="medium")[T.CREDITS]
        assert (d.marker.start_ms, d.marker.end_ms, d.marker.decided_by) == (5_698_000, MOVIE_MS, ("chapters",))

    def test_a_preview_after_the_credits_end_no_longer_overlaps(self):
        preview = Candidate(T.PREVIEW, 5_950_000, 5_990_000, Source.CHAPTERS, 1.0, "Preview")
        ended = credits([text(5_700_000, 5_900_000), skipdb(5_702_000), preview], types=(T.CREDITS, T.PREVIEW))
        assert (ended[T.CREDITS].marker.end_ms, ended[T.PREVIEW].status) == (5_900_000, DecisionStatus.DECIDED)
        open_ended = credits([text(5_700_000), skipdb(5_702_000), preview], types=(T.CREDITS, T.PREVIEW))
        assert open_ended[T.PREVIEW].reason == "preview overlaps credits"


def test_a_preview_overlapping_text_decided_credits_goes_to_review():
    preview = Candidate(T.PREVIEW, 5_750_000, 5_800_000, Source.CHAPTERS, 1.0, "Preview")
    out = credits([text(5_700_000), skipdb(5_702_000), preview], types=(T.CREDITS, T.PREVIEW))
    assert out[T.CREDITS].status is DecisionStatus.DECIDED
    assert (out[T.PREVIEW].status, out[T.PREVIEW].reason) == (DecisionStatus.NEEDS_REVIEW, "preview overlaps credits")


@pytest.mark.parametrize(
    "candidates",
    [
        [text(5_700_000), skipdb(5_695_000, 5_990_000)],
        [text(5_700_000), plex(5_705_000, 5_950_000)],
        [chapter(5_640_000), text(5_700_000), plex(5_703_000)],
        [text(5_700_000), skipdb(5_702_000), plex(5_740_000)],
        [text(5_700_000, 5_950_000), skipdb(5_695_000, 5_990_000), plex(5_705_000, 5_900_000)],
    ],
)
@pytest.mark.parametrize("level", ["high", "medium"])
def test_results_never_depend_on_input_order(candidates, level):
    results = {repr(credits(list(p), level=level)[T.CREDITS]) for p in permutations(candidates)}
    assert len(results) == 1
```

- [ ] **Step 2: Run it**

Run: `/home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/markers/test_decide_credits_text.py tests/markers/test_decide.py -q`
Expected: PASS on the branch as it is (the matrix documents existing behaviour; a failure means `decide.py` changed
since planning: stop and report which cell).

Mutation checks (each must fail the named cell; revert after):
- add `Source.CREDITS_TEXT` to `_AGREEMENT_ONLY` → `test_medium_publishes_it_alone_q1`, `TestEndQ3::test_alone_at_medium_the_skip_stops_before_the_scene`;
- `CREDITS_START_TOLERANCE_MS` → 5 000 → `test_agreeing_within_10_s_decides_with_the_first_source_in_order`;
- in `_safer_other_edge`, credits take `max` instead of `min` → `TestEndQ3::test_its_earlier_end_shortens_an_agreeing_skipdb_answer`,
  `test_its_end_wins_over_a_servers_final_credits`, `test_a_servers_earlier_end_still_shortens_it`.

- [ ] **Step 3: Document the rulings in `decide.py` and the spec**

Replace the comment above `_AGREEMENT_ONLY` with:

```python
# At "Medium" a lone source publishes only when it checks this file's cut itself (rule 6): chapters, SkipDB's
# duration-matched intros and recaps, and credits text, which reads this file's own frames (owner, Q1, 2026-09-16).
# IntroDB takes no duration, TheIntroDB answers the closest cut it has, and markers already on servers never decide
# alone (rule 7). Season audio only agrees (R2: 13 wrong of 104 answered alone), and the previous season's is a hint.
```

and the first line of `_may_decide_alone`'s docstring with "Whether a candidate's source checks this file's cut well
enough to publish alone at "Medium" (rule 6): chapters, credits text, and SkipDB for intros and recaps."

In `docs/design/intro-credits/spec.md`:
- §5.4, replace "Credits text alone is one source: under the default publish rule it needs a second source (§5.5)."
  with "Credits text alone is one source: at "High" it needs a second source; at "Medium" it may publish alone, since
  it reads this file's own frames (§5.5 rule 6; owner, 2026-09-16). It also agrees with a server's own credits marker
  as an independent source (rule 7 still shortens). The skip ends at the roll's last credit frame, refined at 1 fps
  like the start, when more than 30 s of the file follows the roll (a scene after the credits); otherwise it runs to
  the end of the file. Emby still gets the start only (§6.3 R1)."
- §5.5 rule 6, the list of lone deciders: "— chapters, credits text (it reads this file's own frames), or SkipDB
  `exact`/`shifted` matches for an intro or recap (…".
- §14, three lines dated 2026-09-16: "Phase 3 owner answers: Q1 credits text alone at Medium (yes; measured on 80:
  alone 65 useful / 3 wrong, Medium with Plex 56 / 1 vs Plex 47 / 13) · Q2 credits text and a server's own marker are
  independent (yes) · Q3 credits text ends at the last credit frame when the roll ends more than 30 s before the end
  of the file (4 of 76 runs), else no end; Emby unchanged (R1)"; "Phase 3 owner answers: Q4 gate — per set, Medium
  wrong ≤ 2 % and ≤ Plex, High wrong ≤ 1 % and ≤ Plex (caps rounded up: 80 → 2 and 1, 205 → 5 and 3), Medium useful ≥
  Plex, rule J ≥ 59 / ≤ 1 early of 80 · Q5 rule J
  ships as measured, frame-checked · Q6 AMD: self-test decides · Q8 ≤ +250 MB image"; "Phase 3 owner answer Q7: a
  throwaway container on `plex` for the lab's GPU rows 12–15 only (synthetic movie, no `/data*` or Plex config, no
  prod Plex access, removed after)".

`grep -n "needs a second source (§5.5)" docs/design/intro-credits/spec.md` must print nothing afterwards.

- [ ] **Step 4: Commit**

Stage the test file, `decide.py` and `spec.md`; Architecture Review on the staged diff; then
`PATH="/home/data/.venv/bin:$PATH" git commit -m "test(markers): decision matrix for credits text with the owner's phase 3 rulings"`.

---
## Task 5: Text detection helpers, GPU self-test, availability

`[lane-parallel]` (after Tasks 1 and 4) `[high-risk]` — spec §5.4 ("GPU … only after applying the app's Vulkan probe
env … Guard: use the GPU only when `get_vulkan_device_info()` reports a hardware device and a 20-frame self-test on that
device beats CPU … Otherwise CPU"), §6.4 item 7 (one long-lived helper per GPU device, requests serialized, env reason,
crash/hang → CPU, self-test cached per device, device → PCI → EP device), §6.4 item 10 (cancel between requests),
2026-09-15 §14 "Phase 2 audit, detection" (the chromaprint tri-state this mirrors), T-R1, T-R3, T-R8, Q6, preflight
I1 (never block on a stuck helper's pipes), I2 (a crash between requests → CPU for good), M3, M4, M5.

High-risk: subprocess lifecycles, pipes that can block, GPU drivers that hang, and a check that decides whether a whole
source is registered.

**OWNER DECISION (2026-09-16), Q6:** AMD GPUs go through the same self-test as every vendor (identical boxes and
faster, or the CPU).

Lifecycle rules this task pins (each with a test in Step 3):
- Helpers start in their own session; `kill()` signals the process group and waits at most `KILL_WAIT_S`.
- `close()` never closes stdin while a write thread is stuck in it (that would wait on the pipe's buffer lock); a
  helper that doesn't die when killed is left to a daemon reaper (`test_a_helper_whose_pipe_outlives_the_kill_never_blocks_the_worker`).
- A GPU helper found exited between requests with any code but `IDLE_EXIT_CODE` moves its device to the CPU for the
  process lifetime with one WARNING (`test_a_gpu_helper_that_exits_between_requests_moves_the_device_to_the_cpu_for_good`);
  a CPU helper is just started again.
- An exit code is read with a `EXIT_CODE_WAIT_S` wait, so a helper leaving on its idle timer as a request arrives is
  restarted on the GPU, not taken for a crash (`test_an_idle_exit_as_a_request_arrives_starts_the_helper_again_on_the_gpu`).
- A helper idle for more than `IDLE_EXIT_S − IDLE_RESTART_MARGIN_S` is replaced before a request
  (`test_a_helper_close_to_its_idle_exit_is_replaced_before_a_request`).

**Files:**
- Create: `media_preview_generator/markers/credits/devices.py`, `media_preview_generator/markers/credits/textdet_helper.py`,
  `tests/markers/credits/fake_textdet_helper.py`, `tests/markers/credits/test_devices.py`,
  `tests/markers/credits/test_textdet_helper.py`, `tests/markers/credits/test_textdet_helper_integration.py`
- Test: the three test files

**Interfaces:**
- Consumes: Task 4 `textdet.TextDetector`, `cpu_session`, `webgpu_session`, `webgpu_devices`, `synthetic_frames`,
  `ModelError` (only inside the helper process); `markers.locks.KeyedLocks`; `gpu.vulkan_probe.get_vulkan_device_info()`
  (`VulkanProbeResult.device`, `.is_software`), `get_vulkan_env_overrides()`; sysfs `/sys/class/drm/<node>/device`
  (resolved here: the landed `gpu.enumeration._get_pci_address_from_drm_device` matches decimal bus ids only, M5);
  Task 1 M3 (the PCI metadata key).
- Produces (Tasks 8, 10, 11 rely on these):
```python
# media_preview_generator/markers/credits/devices.py
PCI_METADATA_KEYS: tuple[str, ...] = ("pci_bus_id",)
SYSFS_DRM = "/sys/class/drm"
def normalise_pci_bus_id(value: str | None) -> str | None
def drm_pci_bus_id(render_node: str, *, sysfs_drm: str | None = None) -> str | None   # hex buses (0a:00.0, c1:00.0) too
def nvidia_pci_bus_ids() -> dict[str, str]                 # lru_cache; .cache_clear() in tests
def worker_pci_bus_id(gpu: str | None, gpu_device_path: str | None) -> str | None
def ep_device_pci_bus_id(metadata: Mapping[str, str]) -> str | None
def choose_ep_device(metadatas: Sequence[Mapping[str, str]], pci_bus_id: str | None) -> int | None

# media_preview_generator/markers/credits/textdet_helper.py   (no onnxruntime / cv2 at import)
MODULE: str; MODEL_ENV = "MEDIA_PREVIEW_TEXTDET_MODEL"; DEFAULT_MODEL_PATH = "/app/models/ch_PP-OCRv4_det_infer.onnx"
CPU_KEY = "cpu"; IDLE_EXIT_CODE = 75; CHECK_ABSENT_CODE = 3; IDLE_EXIT_S = 600.0
KILL_WAIT_S = 5.0; EXIT_CODE_WAIT_S = 2.0; IDLE_RESTART_MARGIN_S = 5.0
class TextDetState(str, Enum): AVAILABLE = "available"; ABSENT = "absent"; UNKNOWN = "unknown"
class HelperError(Exception)
class TextDetUnavailableError(Exception)
def model_path() -> str
def text_detection_state() -> TextDetState                  # cached like chromaprint_state; UNKNOWN retried after 10 min
def text_detection_status() -> tuple[bool, str]             # (available, reason for Settings; "" when available)
def forget_text_detection_state() -> None                   # tests
@dataclass(frozen=True) class SelfTest:
    gpu_ms: float; cpu_ms: float; same_boxes: bool
    @property use_gpu -> bool
def self_test(gpu, cpu, frames: np.ndarray, *, clock: Callable[[], float] = time.perf_counter, warmup: int = 3) -> SelfTest
@dataclass(frozen=True) class HelperSpec: key: str; backend: str; pci_bus_id: str | None; selftest: bool; env: dict[str, str]
def device_key(gpu: str | None, gpu_device_path: str | None) -> str
def helper_command(spec: HelperSpec) -> list[str]
class TextDetectorPool:
    def __init__(self, *, command: Callable[[HelperSpec], list[str]] = helper_command, popen=subprocess.Popen,
                 start_timeout_s: float = 120.0, request_timeout_s: float = 60.0,
                 vulkan_info: Callable[[], Any] | None = None, vulkan_env: Callable[[], dict[str, str]] | None = None) -> None
    def count_boxes(self, planes: np.ndarray, *, gpu: str | None, gpu_device_path: str | None) -> list[int]
    def backend_of(self, gpu: str | None, gpu_device_path: str | None) -> str | None     # "webgpu" | "cpu" | None (not started)
    def close_all(self) -> None
def get_textdet_pool() -> TextDetectorPool                  # process singleton, closed at exit
def main(argv: list[str] | None = None) -> int              # python -m …textdet_helper
```

- [ ] **Step 1: Write the failing device tests**

```python
# tests/markers/credits/test_devices.py
"""Worker device → PCI address → WebGPU EP device (spec §6.4 item 7, T-R3)."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from media_preview_generator.markers.credits import devices


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("00000000:02:00.0", "0000:02:00.0"),
        ("0000:0A:1F.3", "0000:0a:1f.3"),
        ("02:00.0", "0000:02:00.0"),
        ("0001:65:00.1", "0001:65:00.1"),
        ("", None),
        (None, None),
        ("pci-0000:02:00.0", None),
        ("0000:02:00.8", None),
    ],
)
def test_normalise_pci_bus_id(value, expected):
    assert devices.normalise_pci_bus_id(value) == expected


class TestWorkerPci:
    def setup_method(self):
        devices.nvidia_pci_bus_ids.cache_clear()

    def teardown_method(self):
        devices.nvidia_pci_bus_ids.cache_clear()

    def _smi(self, stdout, returncode=0):
        return patch.object(devices.subprocess, "run", return_value=SimpleNamespace(returncode=returncode, stdout=stdout))

    def test_nvidia_index_maps_through_nvidia_smi(self):
        with self._smi("0, 00000000:02:00.0\n1, 00000000:65:00.0\n") as run:
            assert devices.worker_pci_bus_id("NVIDIA", "cuda:1") == "0000:65:00.0"
            assert devices.worker_pci_bus_id("NVIDIA", "cuda:0") == "0000:02:00.0"
        assert run.call_args.args[0] == ["nvidia-smi", "--query-gpu=index,pci.bus_id", "--format=csv,noheader"]
        assert run.call_count == 1  # cached for the process

    @pytest.mark.parametrize(("stdout", "expected"), [("0, 00000000:02:00.0\n", "0000:02:00.0"), ("0, 00000000:02:00.0\n1, 00000000:65:00.0\n", None)])
    def test_nvidia_without_an_index_is_known_only_on_a_one_gpu_host(self, stdout, expected):
        with self._smi(stdout):
            assert devices.worker_pci_bus_id("NVIDIA", "cuda") == expected

    def test_nvidia_smi_failing_gives_none(self):
        with patch.object(devices.subprocess, "run", side_effect=OSError("nope")):
            assert devices.worker_pci_bus_id("NVIDIA", "cuda:0") is None

    @pytest.mark.parametrize("address", ["0000:00:02.0", "0000:0a:00.0", "0000:c1:00.0"])
    def test_render_node_maps_through_sysfs_with_hex_buses(self, tmp_path, monkeypatch, address):
        device = tmp_path / "devices" / "pci0000:00" / "0000:00:01.1" / address
        device.mkdir(parents=True)
        node = tmp_path / "drm" / "renderD129"
        node.mkdir(parents=True)
        (node / "device").symlink_to(device)
        monkeypatch.setattr(devices, "SYSFS_DRM", str(tmp_path / "drm"))
        assert devices.worker_pci_bus_id("AMD", "/dev/dri/renderD129") == address
        assert devices.worker_pci_bus_id("INTEL", "/dev/dri/renderD128") is None  # no such node in the fake tree


    @pytest.mark.parametrize(("gpu", "path"), [(None, None), ("APPLE", "videotoolbox"), ("WINDOWS_GPU", "d3d11va")])
    def test_others_have_none(self, gpu, path):
        assert devices.worker_pci_bus_id(gpu, path) is None


# Storage's EP list, measured 2026-09-16: the P5000 and the board's ASPEED BMC VGA.
P5000 = {"Discrete": "1", "card_idx": "0", "pci_bus_id": "0000:02:00.0"}
BMC = {"card_idx": "1", "pci_bus_id": "0000:07:00.0"}
IGPU = {"card_idx": "0", "pci_bus_id": "0000:00:02.0"}


@pytest.mark.parametrize(
    ("metadatas", "pci", "expected"),
    [
        ([], "0000:02:00.0", None),
        ([P5000, BMC], "0000:02:00.0", 0),          # storage
        ([BMC, P5000], "0000:02:00.0", 1),
        ([P5000, IGPU], "0000:00:02.0", 1),         # plex: the Intel worker's helper
        ([P5000, BMC], "0000:65:00.0", None),       # no device is this worker's GPU
        ([P5000, BMC], None, None),                 # address unknown, several devices
        ([P5000], None, 0),                         # address unknown, one device
        ([P5000], "0000:65:00.0", None),            # the only device is another GPU
        ([{}], "0000:02:00.0", 0),                  # one device without an address
        ([{}, {}], "0000:02:00.0", None),
        ([{"pci_bus_id": "0000:02:00.0"}, {"pci_bus_id": "0000:02:00.0"}], "0000:02:00.0", None),
    ],
)
def test_choose_ep_device(metadatas, pci, expected):
    assert devices.choose_ep_device(metadatas, pci) == expected
```

Run: `/home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/markers/credits/test_devices.py -q`
Expected: FAIL — `ImportError: cannot import name 'devices'`.

- [ ] **Step 2: Implement `devices.py`**

```python
# media_preview_generator/markers/credits/devices.py
"""Which physical GPU a worker's device is, and which WebGPU EP device matches it (spec §6.4 item 7)."""

from __future__ import annotations

import functools
import os
import re
import subprocess
from collections.abc import Mapping, Sequence

_PCI_RE = re.compile(r"^(?:([0-9a-fA-F]{1,8}):)?([0-9a-fA-F]{2}):([0-9a-fA-F]{2})\.([0-7])$")
# The metadata key onnxruntime-ep-webgpu 0.3.0 puts a device's PCI address under (measured on storage, 2026-09-16).
PCI_METADATA_KEYS = ("pci_bus_id",)
SYSFS_DRM = "/sys/class/drm"


def normalise_pci_bus_id(value: str | None) -> str | None:
    """A PCI address in one spelling: ``0000:02:00.0`` (domain optional on input, lower-case hex).

    Args:
        value: ``00000000:02:00.0`` (nvidia-smi), ``0000:02:00.0`` (sysfs) or ``02:00.0``.

    Returns:
        The normalised address, or None when the value isn't one.
    """
    if not value:
        return None
    match = _PCI_RE.match(value.strip())
    if match is None:
        return None
    domain, bus, device, function = match.groups()
    return f"{int(domain or '0', 16):04x}:{bus.lower()}:{device.lower()}.{function}"


def drm_pci_bus_id(render_node: str, *, sysfs_drm: str | None = None) -> str | None:
    """The PCI address of a ``/dev/dri`` render node, from its sysfs device link.

    Resolved here rather than with ``gpu.enumeration``'s helper, whose pattern takes decimal digits only and misses
    buses such as ``0a:00.0`` or ``c1:00.0``.

    Args:
        render_node: ``/dev/dri/renderD128``.
        sysfs_drm: The sysfs DRM class folder (default ``SYSFS_DRM``).

    Returns:
        The normalised address, or None when sysfs doesn't say.
    """
    try:
        link = os.path.join(sysfs_drm or SYSFS_DRM, os.path.basename(render_node), "device")
        real = os.path.realpath(link, strict=True)
    except OSError:
        return None
    for part in reversed(real.split(os.sep)):
        found = normalise_pci_bus_id(part)
        if found:
            return found
    return None


@functools.lru_cache(maxsize=1)
def nvidia_pci_bus_ids() -> dict[str, str]:
    """NVIDIA GPU index → PCI address from nvidia-smi, read once per process ({} when it can't answer)."""
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,pci.bus_id", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if proc.returncode != 0:
        return {}
    found: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        pci = normalise_pci_bus_id(parts[1]) if len(parts) == 2 else None
        if pci:
            found[parts[0]] = pci
    return found


def worker_pci_bus_id(gpu: str | None, gpu_device_path: str | None) -> str | None:
    """The PCI address of a worker's GPU.

    Args:
        gpu: The worker's GPU type.
        gpu_device_path: ``cuda:<index>`` or a ``/dev/dri`` render node.

    Returns:
        The address, or None when it can't be told (a CPU worker, an NVIDIA device without an index on a multi-GPU
        host, nvidia-smi or sysfs not answering).
    """
    if gpu == "NVIDIA":
        ids = nvidia_pci_bus_ids()
        index = gpu_device_path.split(":", 1)[1] if gpu_device_path and gpu_device_path.startswith("cuda:") else ""
        if index:
            return ids.get(index)
        return next(iter(ids.values())) if len(ids) == 1 else None
    if gpu_device_path and gpu_device_path.startswith("/dev/dri/"):
        return drm_pci_bus_id(gpu_device_path)
    return None


def ep_device_pci_bus_id(metadata: Mapping[str, str]) -> str | None:
    """The PCI address an EP device's metadata carries, normalised (None when it carries none)."""
    for key in PCI_METADATA_KEYS:
        found = normalise_pci_bus_id(metadata.get(key))
        if found:
            return found
    return None


def choose_ep_device(metadatas: Sequence[Mapping[str, str]], pci_bus_id: str | None) -> int | None:
    """Which of the WebGPU EP's devices to run on for a worker's GPU (T-R3).

    The EP lists every display PCI device from sysfs, Vulkan-capable or not (a server's BMC VGA too), so the worker's
    PCI address picks the device; several devices are normal.

    Args:
        metadatas: Each EP device's metadata, in the EP's order.
        pci_bus_id: The worker GPU's PCI address (``worker_pci_bus_id``), or None when unknown.

    Returns:
        The device's index; None means "use the CPU": no device, no device with the worker's address, or an unknown
        address on a host with several devices.
    """
    if not metadatas:
        return None
    if pci_bus_id is not None:
        matches = [i for i, metadata in enumerate(metadatas) if ep_device_pci_bus_id(metadata) == pci_bus_id]
        if len(matches) == 1:
            return matches[0]
        if matches or any(ep_device_pci_bus_id(metadata) is not None for metadata in metadatas):
            return None
    return 0 if len(metadatas) == 1 else None
```

After Task 1 M3 (inside the app image): if the image's EP reports the address under another key, add it to
`PCI_METADATA_KEYS` and add a row with that key to `test_choose_ep_device`.

Run: `/home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/markers/credits/test_devices.py -q` — Expected: PASS.

- [ ] **Step 3: Write the fake helper and the failing pool / state / self-test tests**

```python
# tests/markers/credits/fake_textdet_helper.py
"""Stand-in for the text detection helper process: the same protocol, boxes = bright pixels // 100 (at most 9).

    python fake_textdet_helper.py --backend cpu|webgpu --mode MODE [--idle-exit-s S] [--no-selftest]

Modes: ok, selftest-cpu, crash-on-request, crash-after-reply, idle-exit-on-request, hang-on-request, hang-start,
bad-ready, hang-on-exit, error-reply, hold-stdin (never reads; a process in its own session keeps stdin open after a
kill, its pid written to $FAKE_HOLDER_PID_FILE).
"""

import argparse
import json
import os
import select
import subprocess
import sys
import time

import numpy as np


def send(out, message):
    out.write((json.dumps(message) + "\n").encode())
    out.flush()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", default="cpu")
    parser.add_argument("--mode", default="ok")
    parser.add_argument("--idle-exit-s", type=float, default=600.0)
    parser.add_argument("--no-selftest", action="store_true")
    args, _ = parser.parse_known_args()
    out = sys.stdout.buffer
    if args.mode == "hang-start":
        time.sleep(3600)
    if args.mode == "bad-ready":
        out.write(b"hello\n")
        out.flush()
        time.sleep(3600)
    webgpu = args.backend == "webgpu"
    backend = "cpu" if args.mode == "selftest-cpu" else args.backend
    selftest = None if (not webgpu or args.no_selftest) else {"gpu_ms": 20.0 if backend == "cpu" else 9.0, "cpu_ms": 18.0, "same_boxes": True}
    send(out, {"ready": True, "backend": backend, "selftest": selftest, "reason": "the GPU was slower than the CPU" if backend != args.backend else ""})
    stdin = sys.stdin.buffer
    if args.mode == "hold-stdin":
        holder = subprocess.Popen(["sleep", "60"], start_new_session=True)
        with open(os.environ["FAKE_HOLDER_PID_FILE"], "w") as fh:
            fh.write(str(holder.pid))
        time.sleep(3600)
    while True:
        readable, _, _ = select.select([stdin], [], [], args.idle_exit_s)
        if not readable:
            return 75
        header = stdin.readline()
        if header and args.mode == "idle-exit-on-request":
            os._exit(75)  # its idle timer fired just as the request arrived
        if not header:
            if args.mode == "hang-on-exit":
                time.sleep(3600)
            return 0
        request = json.loads(header)
        size = request["frames"] * request["height"] * request["width"]
        planes = np.frombuffer(stdin.read(size), np.uint8).reshape(request["frames"], request["height"], request["width"])
        if args.mode == "crash-on-request":
            os._exit(9)
        if args.mode == "hang-on-request":
            time.sleep(3600)
        if args.mode == "error-reply":
            send(out, {"id": request["id"], "error": "boom"})
            continue
        send(out, {"id": request["id"], "boxes": [min(9, int((p > 200).sum()) // 100) for p in planes]})
        if args.mode == "crash-after-reply":
            os._exit(9)


if __name__ == "__main__":
    sys.exit(main())
```

```python
# tests/markers/credits/test_textdet_helper.py
"""Text detection helpers: GPU/CPU choice, self-test, crashes, hangs, idle exits, serialisation, availability."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from media_preview_generator.markers.credits import textdet_helper as th
from media_preview_generator.markers.credits.textdet_helper import TextDetState

FAKE = Path(__file__).with_name("fake_textdet_helper.py")
HARDWARE = SimpleNamespace(device="Quadro P5000 (NVIDIA)", is_software=False)
SOFTWARE = SimpleNamespace(device="llvmpipe (LLVM 19.1.1, 256 bits)", is_software=True)
PLANES = np.stack([np.zeros((180, 320), np.uint8), np.full((180, 320), 255, np.uint8)])
ANSWER = [0, 9]


class Env:
    """A pool whose helpers are the fake, with a mode per backend that tests can change between calls."""

    def __init__(self, monkeypatch, *, modes=None, vulkan=HARDWARE, idle_exit_s=600.0, **timeouts):
        self.modes = {"cpu": "ok", "webgpu": "ok", **(modes or {})}
        self.specs: list[th.HelperSpec] = []
        self.procs: list[subprocess.Popen] = []
        monkeypatch.setattr(th, "worker_pci_bus_id", lambda gpu, path: "0000:02:00.0" if gpu == "NVIDIA" else None)
        monkeypatch.delenv("VK_DRIVER_FILES", raising=False)

        def command(spec):
            self.specs.append(spec)
            mode = self.modes[spec.backend]
            if isinstance(mode, list):  # one mode per start, the last one repeating
                mode = mode.pop(0) if len(mode) > 1 else mode[0]
            cmd = [sys.executable, str(FAKE), "--backend", spec.backend, "--mode", mode,
                   "--idle-exit-s", str(idle_exit_s)]  # fmt: skip
            return cmd + ([] if spec.selftest else ["--no-selftest"])

        def popen(*args, **kwargs):
            proc = subprocess.Popen(*args, **kwargs)
            self.procs.append(proc)
            return proc

        self.pool = th.TextDetectorPool(
            command=command,
            popen=popen,
            vulkan_info=lambda: vulkan,
            vulkan_env=lambda: {"VK_DRIVER_FILES": "/etc/vulkan/icd.d/nvidia_icd.json"},
            start_timeout_s=timeouts.get("start_timeout_s", 20.0),
            request_timeout_s=timeouts.get("request_timeout_s", 20.0),
        )

    def backends(self):
        return [(s.key, s.backend, s.selftest) for s in self.specs]


@pytest.fixture
def envs(monkeypatch):
    made: list[Env] = []

    def make(**kwargs):
        made.append(Env(monkeypatch, **kwargs))
        return made[-1]

    yield make
    for env in made:
        env.pool.close_all()


class TestRouting:
    def test_a_cpu_worker_uses_the_cpu_helper(self, envs):
        env = envs()
        assert env.pool.count_boxes(PLANES, gpu=None, gpu_device_path=None) == ANSWER
        assert env.backends() == [("cpu", "cpu", False)]
        assert env.specs[0].pci_bus_id is None
        assert "VK_DRIVER_FILES" not in env.specs[0].env

    def test_an_nvidia_worker_starts_one_webgpu_helper_with_the_vulkan_env_and_keeps_it(self, envs, loguru_caplog):
        env = envs()
        for _ in range(3):
            assert env.pool.count_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0") == ANSWER
        assert env.backends() == [("cuda:0", "webgpu", True)]
        assert env.specs[0].pci_bus_id == "0000:02:00.0"
        assert env.specs[0].env["VK_DRIVER_FILES"] == "/etc/vulkan/icd.d/nvidia_icd.json"
        assert env.pool.backend_of("NVIDIA", "cuda:0") == "webgpu"
        assert "Credit text detection on cuda:0: GPU (9.0 ms per frame, CPU 18.0 ms)" in loguru_caplog.text

    @pytest.mark.parametrize("gpu", ["INTEL", "AMD"])
    def test_a_vaapi_worker_gets_no_nvidia_overrides(self, envs, gpu):
        env = envs()
        assert env.pool.count_boxes(PLANES, gpu=gpu, gpu_device_path="/dev/dri/renderD128") == ANSWER
        assert env.backends() == [("/dev/dri/renderD128", "webgpu", True)]
        assert "VK_DRIVER_FILES" not in env.specs[0].env

    @pytest.mark.parametrize(("gpu", "path"), [("APPLE", "videotoolbox"), ("WINDOWS_GPU", "d3d11va"), ("ARM", "/dev/dri/renderD128")])
    def test_gpus_without_a_webgpu_path_use_the_cpu(self, envs, gpu, path):
        env = envs()
        assert env.pool.count_boxes(PLANES, gpu=gpu, gpu_device_path=path) == ANSWER
        assert env.backends() == [("cpu", "cpu", False)]
        assert env.pool.backend_of(gpu, path) == "cpu"

    def test_software_vulkan_never_starts_a_gpu_helper(self, envs, loguru_caplog):
        env = envs(vulkan=SOFTWARE)
        assert env.pool.count_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0") == ANSWER
        assert env.backends() == [("cpu", "cpu", False)]
        assert "Credit text detection on cuda:0: CPU (Vulkan reports no hardware GPU)" in loguru_caplog.text


class TestFallback:
    def test_a_self_test_that_picks_the_cpu_moves_the_device_to_the_cpu_helper(self, envs, loguru_caplog):
        env = envs(modes={"webgpu": "selftest-cpu"})
        for _ in range(2):
            assert env.pool.count_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0") == ANSWER
        assert env.backends() == [("cuda:0", "webgpu", True), ("cpu", "cpu", False)]
        assert env.procs[0].wait(timeout=10) is not None  # the GPU helper was closed
        assert "Credit text detection on cuda:0: CPU (the GPU was slower than the CPU)" in loguru_caplog.text

    @pytest.mark.parametrize(
        ("mode", "timeouts"),
        [
            ("crash-on-request", {}),
            ("hang-on-request", {"request_timeout_s": 1.0}),
            ("hang-start", {"start_timeout_s": 1.0}),
            ("bad-ready", {}),
            ("error-reply", {}),
        ],
    )
    def test_a_failing_gpu_helper_hands_the_same_frames_to_the_cpu_for_good(self, envs, loguru_caplog, mode, timeouts):
        env = envs(modes={"webgpu": mode}, **timeouts)
        assert env.pool.count_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0") == ANSWER
        assert env.pool.count_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0") == ANSWER
        assert env.backends() == [("cuda:0", "webgpu", True), ("cpu", "cpu", False)]
        assert env.procs[0].poll() is not None  # killed, not left running
        warnings = [r.getMessage() for r in loguru_caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1 and "cuda:0" in warnings[0] and "CPU" in warnings[0]

    def test_a_failing_cpu_helper_is_unavailable_and_the_next_call_starts_a_new_one(self, envs):
        env = envs(modes={"cpu": "crash-on-request"})
        with pytest.raises(th.TextDetUnavailableError, match="Text detection failed"):
            env.pool.count_boxes(PLANES, gpu=None, gpu_device_path=None)
        env.modes["cpu"] = "ok"
        assert env.pool.count_boxes(PLANES, gpu=None, gpu_device_path=None) == ANSWER
        assert env.backends() == [("cpu", "cpu", False), ("cpu", "cpu", False)]

    def test_a_gpu_helper_that_exits_between_requests_moves_the_device_to_the_cpu_for_good(self, envs, loguru_caplog):
        env = envs(modes={"webgpu": "crash-after-reply"})
        assert env.pool.count_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0") == ANSWER
        assert env.procs[0].wait(timeout=10) == 9
        for _ in range(2):
            assert env.pool.count_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0") == ANSWER
        assert env.backends() == [("cuda:0", "webgpu", True), ("cpu", "cpu", False)]
        assert env.pool.backend_of("NVIDIA", "cuda:0") == "cpu"
        warnings = [r.getMessage() for r in loguru_caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1 and "exited 9 between requests" in warnings[0] and "CPU" in warnings[0]

    def test_a_cpu_helper_that_exits_between_requests_is_started_again(self, envs):
        env = envs(modes={"cpu": "crash-after-reply"})
        for _ in range(2):
            assert env.pool.count_boxes(PLANES, gpu=None, gpu_device_path=None) == ANSWER
            env.procs[-1].wait(timeout=10)
        assert env.backends() == [("cpu", "cpu", False), ("cpu", "cpu", False)]

    def test_an_idle_exit_as_a_request_arrives_starts_the_helper_again_on_the_gpu(self, envs, loguru_caplog):
        env = envs(modes={"webgpu": ["idle-exit-on-request", "ok"]})
        assert env.pool.count_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0") == ANSWER
        assert env.procs[0].wait(timeout=10) == th.IDLE_EXIT_CODE
        assert env.backends() == [("cuda:0", "webgpu", True), ("cuda:0", "webgpu", False)]
        assert not [r for r in loguru_caplog.records if r.levelname == "WARNING"]

    def test_a_helper_close_to_its_idle_exit_is_replaced_before_a_request(self, envs, monkeypatch):
        env = envs()
        assert env.pool.count_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0") == ANSWER
        helper = env.pool._helpers["cuda:0"]
        helper.last_used -= th.IDLE_EXIT_S
        assert env.pool.count_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0") == ANSWER
        assert env.backends() == [("cuda:0", "webgpu", True), ("cuda:0", "webgpu", False)]
        assert env.procs[0].wait(timeout=10) is not None

    def test_a_helper_whose_pipe_outlives_the_kill_never_blocks_the_worker(self, envs, monkeypatch, tmp_path):
        pid_file = tmp_path / "holder.pid"
        monkeypatch.setenv("FAKE_HOLDER_PID_FILE", str(pid_file))
        env = envs(modes={"cpu": "hold-stdin"}, request_timeout_s=1.0)
        big = np.zeros((20, 180, 320), np.uint8)  # more than a pipe buffer: the write blocks
        started = time.monotonic()
        try:
            with pytest.raises(th.TextDetUnavailableError, match="didn't read a request"):
                env.pool.count_boxes(big, gpu=None, gpu_device_path=None)
            assert time.monotonic() - started < 1.0 + th.KILL_WAIT_S + th.EXIT_CODE_WAIT_S + 3
            assert env.procs[0].poll() is not None
        finally:
            if pid_file.exists():
                os.kill(int(pid_file.read_text()), signal.SIGKILL)

    def test_an_idle_exit_starts_the_helper_again_without_a_new_self_test(self, envs, loguru_caplog):
        env = envs(idle_exit_s=0.3)
        assert env.pool.count_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0") == ANSWER
        assert env.procs[0].wait(timeout=10) == th.IDLE_EXIT_CODE
        assert env.pool.count_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0") == ANSWER
        assert env.backends() == [("cuda:0", "webgpu", True), ("cuda:0", "webgpu", False)]
        assert not [r for r in loguru_caplog.records if r.levelname == "WARNING"]


def test_requests_for_one_device_never_overlap(envs):
    env = envs()
    results: list[list[int]] = []
    errors: list[BaseException] = []

    def work():
        try:
            for _ in range(15):
                results.append(env.pool.count_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0"))
        except BaseException as exc:  # noqa: BLE001 - collected for the assertion
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert errors == [] and results == [ANSWER] * 60
    assert env.backends() == [("cuda:0", "webgpu", True)]


def test_close_all_kills_a_helper_that_hangs_at_exit(envs, monkeypatch):
    monkeypatch.setattr(th, "CLOSE_GRACE_S", 0.5)
    env = envs(modes={"cpu": "hang-on-exit"})
    env.pool.count_boxes(PLANES, gpu=None, gpu_device_path=None)
    started = time.monotonic()
    env.pool.close_all()
    assert env.procs[0].poll() is not None and time.monotonic() - started < 10


class FakeDetector:
    def __init__(self, counts, per_frame_s, clock):
        self.counts, self.per_frame_s, self.clock = counts, per_frame_s, clock

    def count(self, frames):
        self.clock.now += self.per_frame_s * len(frames)
        return self.counts[: len(frames)]


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


@pytest.mark.parametrize(
    ("gpu_counts", "gpu_s", "use_gpu", "same"),
    [([1, 0, 3], 0.005, True, True), ([1, 0, 3], 0.030, False, True), ([1, 1, 3], 0.005, False, False)],
)
def test_self_test_needs_the_same_boxes_and_more_speed(gpu_counts, gpu_s, use_gpu, same):
    clock = Clock()
    frames = np.zeros((3, 180, 320), np.uint8)
    result = th.self_test(FakeDetector(gpu_counts, gpu_s, clock), FakeDetector([1, 0, 3], 0.018, clock), frames, clock=clock, warmup=1)
    assert (result.use_gpu, result.same_boxes) == (use_gpu, same)
    assert result.cpu_ms == pytest.approx(18.0) and result.gpu_ms == pytest.approx(gpu_s * 1000)


class TestAvailability:
    @pytest.fixture(autouse=True)
    def _fresh(self, monkeypatch, tmp_path):
        th.forget_text_detection_state()
        self.model = tmp_path / "model.onnx"
        self.model.write_bytes(b"m")
        monkeypatch.setenv(th.MODEL_ENV, str(self.model))
        monkeypatch.setattr(th, "_find_spec", lambda name: object())
        self.now = [1000.0]
        monkeypatch.setattr(th, "_monotonic", lambda: self.now[0])
        self.runs: list[list[str]] = []
        yield
        th.forget_text_detection_state()

    def _run(self, monkeypatch, result):
        def run(cmd, **kwargs):
            self.runs.append(cmd)
            if isinstance(result, BaseException):
                raise result
            return result

        monkeypatch.setattr(th.subprocess, "run", run)

    def test_a_passing_check_is_available_and_kept(self, monkeypatch):
        self._run(monkeypatch, SimpleNamespace(returncode=0, stdout="", stderr=""))
        assert th.text_detection_state() is TextDetState.AVAILABLE
        assert th.text_detection_status() == (True, "")
        self.now[0] += 10_000
        assert th.text_detection_state() is TextDetState.AVAILABLE
        assert len(self.runs) == 1
        assert self.runs[0][1:] == ["-m", th.MODULE, "--check", "--model", str(self.model)]

    def test_missing_packages_are_absent_without_running_anything(self, monkeypatch):
        monkeypatch.setattr(th, "_find_spec", lambda name: None if name == "onnxruntime" else object())
        self._run(monkeypatch, AssertionError("must not run"))
        assert th.text_detection_status() == (False, "Needs ONNX Runtime and OpenCV, which the Docker image includes; they aren't installed here")
        assert th.text_detection_state() is TextDetState.ABSENT

    def test_a_missing_model_is_absent(self, monkeypatch):
        self.model.unlink()
        self._run(monkeypatch, AssertionError("must not run"))
        assert th.text_detection_status() == (False, f"Needs the text detection model, which the Docker image includes; it isn't at {self.model}")

    def test_a_check_that_says_absent_is_absent_with_its_reason(self, monkeypatch):
        self._run(monkeypatch, SimpleNamespace(returncode=th.CHECK_ABSENT_CODE, stdout=f"The text detection model at {self.model} isn't the expected file\n", stderr=""))
        assert th.text_detection_status() == (False, f"The text detection model at {self.model} isn't the expected file")
        self.now[0] += 10_000
        th.text_detection_state()
        assert len(self.runs) == 1  # absent is kept for the process

    @pytest.mark.parametrize("result", [subprocess.TimeoutExpired("python", 30), OSError("exec"), SimpleNamespace(returncode=1, stdout="", stderr="Traceback")])
    def test_a_check_that_does_not_answer_is_unknown_and_asked_again_after_10_minutes(self, monkeypatch, result):
        self._run(monkeypatch, result)
        assert th.text_detection_status() == (False, "The text detection check didn't answer; it is checked again in 10 minutes")
        self.now[0] += 599
        assert th.text_detection_state() is TextDetState.UNKNOWN and len(self.runs) == 1
        self.now[0] += 2
        th.text_detection_state()
        assert len(self.runs) == 2


def test_importing_the_helper_module_loads_no_onnxruntime_or_opencv():
    code = ("import sys, media_preview_generator.markers.credits.textdet_helper; "
            "print(sorted(m for m in ('onnxruntime', 'cv2', 'pyclipper', 'onnxruntime_ep_webgpu') if m in sys.modules))")  # fmt: skip
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
    assert out == "[]"
```

Run: `/home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/markers/credits/test_textdet_helper.py -q`
Expected: FAIL — `ImportError: cannot import name 'textdet_helper'`.

- [ ] **Step 4: Implement `textdet_helper.py`**

```python
# media_preview_generator/markers/credits/textdet_helper.py
"""Credit text detection in helper processes: one per GPU device and one for the CPU (spec §6.4 item 7).

Why processes: the Vulkan loader reads its environment once per process and NVIDIA needs overrides that hide other
GPUs (the plex host has NVIDIA + Intel); a driver crash or hang can't take the web app down; the WebGPU plugin can hang
at shutdown (ORT PR #29591).

Protocol, one request at a time per helper:
- helper → parent, once: ``{"ready": true, "backend": "webgpu"|"cpu", "selftest": {...}|null, "reason": str}``
- parent → helper: ``{"id": n, "frames": N, "height": H, "width": W}`` and a newline, then N×H×W bytes of luma
- helper → parent: ``{"id": n, "boxes": [N ints]}`` or ``{"id": n, "error": str}``
- stdin closed → the helper exits 0; no request for ``--idle-exit-s`` → it exits 75.

The web app imports this module; it loads no ONNX Runtime or OpenCV (only :func:`main`, in the helper, imports
``textdet``).
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
import importlib.util
import json
import os
import queue
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, BinaryIO

import numpy as np
from loguru import logger

from ..locks import KeyedLocks
from .devices import choose_ep_device, worker_pci_bus_id

MODULE = "media_preview_generator.markers.credits.textdet_helper"
MODEL_ENV = "MEDIA_PREVIEW_TEXTDET_MODEL"
DEFAULT_MODEL_PATH = "/app/models/ch_PP-OCRv4_det_infer.onnx"
CPU_KEY = "cpu"
THREADS = 2
SELFTEST_FRAMES = 20
START_TIMEOUT_S = 120.0
REQUEST_TIMEOUT_S = 60.0
IDLE_EXIT_S = 600.0
IDLE_EXIT_CODE = 75
CHECK_ABSENT_CODE = 3
CHECK_TIMEOUT_S = 30.0
CHECK_RETRY_S = 600.0
CLOSE_GRACE_S = 5.0
KILL_WAIT_S = 5.0
EXIT_CODE_WAIT_S = 2.0
# A helper this close to its idle exit is replaced before a request instead of racing its exit timer.
IDLE_RESTART_MARGIN_S = 5.0
_WEBGPU_VENDORS = frozenset({"NVIDIA", "INTEL", "AMD"})
NOT_INSTALLED = "Needs ONNX Runtime and OpenCV, which the Docker image includes; they aren't installed here"
NO_ANSWER = "The text detection check didn't answer; it is checked again in 10 minutes"
_monotonic = time.monotonic


class TextDetState(str, Enum):
    """Whether credit text detection can run in this process's container."""

    AVAILABLE = "available"
    # Packages or model missing, or the check said so: kept for the process.
    ABSENT = "absent"
    # The check timed out, couldn't start or crashed: asked again after CHECK_RETRY_S.
    UNKNOWN = "unknown"


class HelperError(Exception):
    """A helper process didn't start, answer or read a request."""


class TextDetUnavailableError(Exception):
    """Text detection can't answer this time (the CPU helper failed)."""


def model_path() -> str:
    """The model file: ``MEDIA_PREVIEW_TEXTDET_MODEL`` (dev and harness runs), else the image's copy."""
    return os.environ.get(MODEL_ENV) or DEFAULT_MODEL_PATH


_state_lock = threading.Lock()
_state: tuple[TextDetState, str, float | None] | None = None


def _find_spec(name: str) -> Any:
    """``importlib.util.find_spec`` (tests replace this one name, never the process-wide function)."""
    return importlib.util.find_spec(name)


def _run_state_check() -> tuple[TextDetState, str]:
    if any(_find_spec(name) is None for name in ("onnxruntime", "cv2", "pyclipper")):
        return TextDetState.ABSENT, NOT_INSTALLED
    path = model_path()
    if not os.path.isfile(path):
        return TextDetState.ABSENT, f"Needs the text detection model, which the Docker image includes; it isn't at {path}"
    try:
        proc = subprocess.run(
            [sys.executable, "-m", MODULE, "--check", "--model", path],
            capture_output=True,
            text=True,
            timeout=CHECK_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return TextDetState.UNKNOWN, NO_ANSWER
    if proc.returncode == 0:
        return TextDetState.AVAILABLE, ""
    if proc.returncode == CHECK_ABSENT_CODE:
        lines = [line for line in (proc.stdout or "").splitlines() if line.strip()]
        return TextDetState.ABSENT, lines[-1] if lines else NOT_INSTALLED
    logger.debug("Text detection check exited {}: {}", proc.returncode, (proc.stderr or "")[-300:])
    return TextDetState.UNKNOWN, NO_ANSWER


def _cached_state() -> tuple[TextDetState, str]:
    global _state
    with _state_lock:
        if _state is not None and (_state[2] is None or _monotonic() < _state[2]):
            return _state[0], _state[1]
        state, reason = _run_state_check()
        _state = (state, reason, _monotonic() + CHECK_RETRY_S if state is TextDetState.UNKNOWN else None)
        return state, reason


def text_detection_state() -> TextDetState:
    """Whether credit text detection can run here (checked once per process; UNKNOWN is asked again after 10 min)."""
    return _cached_state()[0]


def text_detection_status() -> tuple[bool, str]:
    """``(available, reason)`` for Settings; the reason is "" when available."""
    state, reason = _cached_state()
    return state is TextDetState.AVAILABLE, reason


def forget_text_detection_state() -> None:
    """Forget the cached check (tests)."""
    global _state
    with _state_lock:
        _state = None


@dataclass(frozen=True)
class SelfTest:
    """A GPU session against the CPU on the same frames (milliseconds per frame)."""

    gpu_ms: float
    cpu_ms: float
    same_boxes: bool

    @property
    def use_gpu(self) -> bool:
        """The GPU is used only when it counts exactly the CPU's boxes, faster."""
        return self.same_boxes and self.gpu_ms < self.cpu_ms


def self_test(gpu: Any, cpu: Any, frames: np.ndarray, *, clock: Callable[[], float] = time.perf_counter, warmup: int = 3) -> SelfTest:
    """Time both detectors on the same frames after a warm-up and compare their box counts.

    Args:
        gpu: A detector with ``count(frames) -> list[int]`` on the GPU.
        cpu: The same on the CPU.
        frames: (n, H, W) uint8 luma.
        clock: Seconds (tests pass a fake).
        warmup: Frames each detector runs before timing (session start-up and shader compilation).

    Returns:
        Milliseconds per frame each way and whether every count matched.
    """
    for detector in (gpu, cpu):
        detector.count(frames[:warmup])

    def timed(detector: Any) -> tuple[list[int], float]:
        started = clock()
        counts = detector.count(frames)
        return counts, (clock() - started) * 1000.0 / len(frames)

    gpu_counts, gpu_ms = timed(gpu)
    cpu_counts, cpu_ms = timed(cpu)
    return SelfTest(round(gpu_ms, 2), round(cpu_ms, 2), list(gpu_counts) == list(cpu_counts))


@dataclass(frozen=True)
class HelperSpec:
    """How to start one helper.

    Attributes:
        key: The device key (``device_key``) or ``cpu``.
        backend: ``webgpu`` or ``cpu``.
        pci_bus_id: The worker GPU's PCI address, for picking the EP device.
        selftest: Run the 20-frame self-test (the first start for a device).
        env: The helper's environment (NVIDIA: with the app's Vulkan overrides).
    """

    key: str
    backend: str
    pci_bus_id: str | None
    selftest: bool
    env: dict[str, str]


def device_key(gpu: str | None, gpu_device_path: str | None) -> str:
    """The helper a worker's requests go to: its device path (or GPU type), ``cpu`` for a CPU worker."""
    return CPU_KEY if gpu is None else (gpu_device_path or gpu)


def helper_command(spec: HelperSpec) -> list[str]:
    """``python -m …textdet_helper`` with the spec's arguments."""
    command = [sys.executable, "-m", MODULE, "--backend", spec.backend, "--model", model_path(),
               "--threads", str(THREADS), "--idle-exit-s", f"{IDLE_EXIT_S:g}"]  # fmt: skip
    if spec.pci_bus_id:
        command += ["--pci-bus-id", spec.pci_bus_id]
    if not spec.selftest:
        command.append("--no-selftest")
    return command


class _Helper:
    """One running helper process and its answer queue."""

    def __init__(self, proc: subprocess.Popen, stderr_file: BinaryIO, request_timeout_s: float) -> None:
        self.proc = proc
        self.ready: dict[str, Any] = {}
        self._stderr = stderr_file
        self._timeout = request_timeout_s
        self._lines: queue.Queue[bytes] = queue.Queue()
        self._next_id = 0
        self._writer: threading.Thread | None = None
        self.last_used = _monotonic()
        threading.Thread(target=self._pump, daemon=True, name="textdet-helper-out").start()

    def _pump(self) -> None:
        for line in iter(self.proc.stdout.readline, b""):
            self._lines.put(line)
        self._lines.put(b"")

    def read_message(self, timeout_s: float) -> dict[str, Any]:
        try:
            line = self._lines.get(timeout=timeout_s)
        except queue.Empty:
            raise HelperError(f"no answer within {timeout_s:g} s") from None
        if not line:
            raise HelperError(f"the helper exited ({self.proc.poll()}){self._stderr_tail()}")
        try:
            message = json.loads(line)
        except ValueError:
            raise HelperError(f"unreadable answer {line[:80]!r}") from None
        if not isinstance(message, dict):
            raise HelperError(f"unreadable answer {line[:80]!r}")
        return message

    def request(self, planes: np.ndarray) -> list[int]:
        frames = np.ascontiguousarray(planes, dtype=np.uint8)
        count, height, width = frames.shape
        self._next_id += 1
        header = json.dumps({"id": self._next_id, "frames": count, "height": height, "width": width}).encode()
        self._write(header + b"\n" + frames.tobytes())
        reply = self.read_message(self._timeout)
        self.last_used = _monotonic()
        boxes = reply.get("boxes")
        if reply.get("id") != self._next_id or "error" in reply or not isinstance(boxes, list) or len(boxes) != count:
            raise HelperError(f"bad answer: {str(reply.get('error') or reply)[:200]}")
        return [int(b) for b in boxes]

    def _write(self, data: bytes) -> None:
        failed: list[BaseException] = []

        def write() -> None:
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (OSError, ValueError) as exc:
                failed.append(exc)

        writer = threading.Thread(target=write, daemon=True, name="textdet-helper-in")
        self._writer = writer
        writer.start()
        writer.join(self._timeout)
        if writer.is_alive():
            self.kill()  # a helper that stopped reading would hold this write forever
            raise HelperError(f"the helper didn't read a request within {self._timeout:g} s")
        if failed:
            raise HelperError(f"couldn't send the frames: {failed[0]}")

    def exit_code(self) -> int | None:
        return self.proc.poll()

    def wait_exit(self, timeout_s: float) -> int | None:
        """The exit code once the helper has exited, waiting up to ``timeout_s`` (None: still running).

        Right after its output ends, a helper leaving on its idle timer may not have been reaped yet; reading ``poll()``
        at once would take that for a crash.
        """
        with contextlib.suppress(subprocess.TimeoutExpired):
            return self.proc.wait(timeout=timeout_s)
        return None

    def _stderr_tail(self) -> str:
        with contextlib.suppress(OSError, ValueError):
            self._stderr.seek(0, os.SEEK_END)
            size = self._stderr.tell()
            self._stderr.seek(max(0, size - 300))
            tail = self._stderr.read().decode("utf-8", "replace").strip()
            return f": {tail}" if tail else ""
        return ""

    def kill(self) -> None:
        """Kill the helper's process group (it runs in its own session) and wait a bounded time for it."""
        if self.proc.poll() is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self.proc.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.proc.wait(timeout=KILL_WAIT_S)

    def close(self, grace_s: float) -> None:
        """Stop the helper without ever blocking on its pipes.

        A write stuck in ``stdin.write`` (a helper that stopped reading, a driver hang) holds the pipe's buffer lock, so
        closing stdin then would block this thread; and a process that doesn't die when killed keeps its pipes. Both
        go to a daemon reaper that closes them once they let go.
        """
        writer = self._writer
        if writer is not None and writer.is_alive():
            self.kill()
        else:
            with contextlib.suppress(OSError, ValueError):
                self.proc.stdin.close()
            try:
                self.proc.wait(timeout=grace_s)
            except subprocess.TimeoutExpired:
                self.kill()  # the WebGPU plugin can hang at exit
        if self.proc.poll() is None or (writer is not None and writer.is_alive()):
            logger.warning("A text detection helper didn't stop when killed; leaving it to finish on its own")
            threading.Thread(target=self._reap, daemon=True, name="textdet-helper-reaper").start()
            return
        with contextlib.suppress(OSError, ValueError):
            self._stderr.close()

    def _reap(self) -> None:
        if self._writer is not None:
            self._writer.join()
        with contextlib.suppress(OSError, ValueError):
            self.proc.stdin.close()
        with contextlib.suppress(OSError):
            self.proc.wait()
        with contextlib.suppress(OSError, ValueError):
            self._stderr.close()


def _start(spec: HelperSpec, *, command: Callable[[HelperSpec], list[str]], popen: Any, start_timeout_s: float,
           request_timeout_s: float) -> _Helper:  # fmt: skip
    stderr_file = tempfile.TemporaryFile()
    try:
        proc = popen(command(spec), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr_file, env=spec.env,
                     start_new_session=True)  # fmt: skip
    except OSError as exc:
        stderr_file.close()
        raise HelperError(f"couldn't start: {exc}") from exc
    helper = _Helper(proc, stderr_file, request_timeout_s)
    try:
        ready = helper.read_message(start_timeout_s)
        if ready.get("ready") is not True or ready.get("backend") not in ("webgpu", "cpu"):
            raise HelperError(f"unexpected start line: {str(ready)[:200]}")
    except HelperError:
        helper.kill()
        helper.close(0)
        raise
    helper.ready = ready
    return helper


def _vulkan_device_info() -> Any:
    from ...gpu.vulkan_probe import get_vulkan_device_info

    return get_vulkan_device_info()


def _vulkan_env_overrides() -> dict[str, str]:
    from ...gpu.vulkan_probe import get_vulkan_env_overrides

    return get_vulkan_env_overrides()


class TextDetectorPool:
    """Box counts for luma planes on a worker's device: its GPU helper when that is proven faster, else the CPU helper."""

    def __init__(
        self,
        *,
        command: Callable[[HelperSpec], list[str]] = helper_command,
        popen: Any = subprocess.Popen,
        start_timeout_s: float = START_TIMEOUT_S,
        request_timeout_s: float = REQUEST_TIMEOUT_S,
        vulkan_info: Callable[[], Any] | None = None,
        vulkan_env: Callable[[], dict[str, str]] | None = None,
    ) -> None:
        """Create an empty pool (helpers start on first use).

        Args:
            command: Builds a helper's argv (tests start a fake).
            popen: Starts a process.
            start_timeout_s: Longest wait for a helper's ready line (a WebGPU start with the self-test included).
            request_timeout_s: Longest wait for one request's answer.
            vulkan_info: The app's Vulkan probe result.
            vulkan_env: The app's Vulkan env overrides (applied to NVIDIA helpers only).
        """
        self._command, self._popen = command, popen
        self._start_timeout_s, self._request_timeout_s = start_timeout_s, request_timeout_s
        self._vulkan_info = vulkan_info or _vulkan_device_info
        self._vulkan_env = vulkan_env or _vulkan_env_overrides
        self._helpers: dict[str, _Helper] = {}
        self._backends: dict[str, str] = {}  # device key → "webgpu" | "cpu", for the process lifetime
        self._locks = KeyedLocks()
        self._guard = threading.Lock()

    def count_boxes(self, planes: np.ndarray, *, gpu: str | None, gpu_device_path: str | None) -> list[int]:
        """Text boxes per luma plane on the worker's device.

        Args:
            planes: (n, H, W) uint8.
            gpu: The worker's GPU type, None on a CPU worker.
            gpu_device_path: The worker's device.

        Returns:
            One count per plane.

        Raises:
            TextDetUnavailableError: The CPU helper failed (the next call starts a new one).
        """
        key = device_key(gpu, gpu_device_path)
        if key != CPU_KEY and self._gpu_allowed(key, gpu):
            with self._locks.hold(key):
                try:
                    counts = self._on_helper(key, planes, lambda: self._gpu_spec(key, gpu, gpu_device_path))
                except HelperError as exc:
                    self._use_cpu(key, f"its GPU helper failed: {exc}", warning=True)
                else:
                    if counts is not None:
                        return counts
        with self._locks.hold(CPU_KEY):
            try:
                counts = self._on_helper(CPU_KEY, planes, self._cpu_spec)
            except HelperError as exc:
                raise TextDetUnavailableError(f"Text detection failed: {exc}") from exc
        assert counts is not None  # a CPU helper always serves
        return counts

    def backend_of(self, gpu: str | None, gpu_device_path: str | None) -> str | None:
        """What a worker's text detection runs on: ``webgpu``, ``cpu``, or None before its first request."""
        key = device_key(gpu, gpu_device_path)
        if key == CPU_KEY:
            return "cpu"
        with self._guard:
            return self._backends.get(key)

    def close_all(self) -> None:
        """Stop every helper (killed after CLOSE_GRACE_S)."""
        with self._guard:
            helpers, self._helpers = list(self._helpers.values()), {}
        for helper in helpers:
            helper.close(CLOSE_GRACE_S)

    def _gpu_allowed(self, key: str, gpu: str | None) -> bool:
        with self._guard:
            known = self._backends.get(key)
        if known is not None:
            return known == "webgpu"
        if gpu not in _WEBGPU_VENDORS:
            self._use_cpu(key, f"{gpu} GPUs have no WebGPU path here")
            return False
        info = self._vulkan_info()
        if info.device is None or info.is_software:
            self._use_cpu(key, "Vulkan reports no hardware GPU")
            return False
        return True

    def _on_helper(self, key: str, planes: np.ndarray, spec_for: Callable[[], HelperSpec]) -> list[int] | None:
        for attempt in (1, 2):
            helper = self._helper(key, spec_for)
            if helper is None:
                return None  # its self-test chose the CPU
            try:
                return helper.request(planes)
            except HelperError:
                code = helper.wait_exit(EXIT_CODE_WAIT_S)
                self._drop(key)
                if code == IDLE_EXIT_CODE and attempt == 1:
                    continue  # it went idle just as the request was sent: start it again
                raise
        raise HelperError("the helper went idle twice in a row")

    def _helper(self, key: str, spec_for: Callable[[], HelperSpec]) -> _Helper | None:
        with self._guard:
            helper = self._helpers.get(key)
        if helper is not None:
            code = helper.exit_code()
            if code is not None:
                self._drop(key)
                helper = None
                if key != CPU_KEY and code != IDLE_EXIT_CODE:
                    # Crashed between requests: the device moves to the CPU for the process lifetime (spec §6.4 item 7).
                    self._use_cpu(key, f"its GPU helper exited {code} between requests", warning=True)
                    return None
            elif _monotonic() - helper.last_used > IDLE_EXIT_S - IDLE_RESTART_MARGIN_S:
                self._drop(key)  # about to leave on its idle timer (T-R8): start a new one rather than race it
                helper = None
        if helper is None:
            helper = _start(spec_for(), command=self._command, popen=self._popen,
                            start_timeout_s=self._start_timeout_s, request_timeout_s=self._request_timeout_s)  # fmt: skip
            with self._guard:
                self._helpers[key] = helper
            if key != CPU_KEY:
                self._record(key, helper.ready)
                if helper.ready["backend"] != "webgpu":
                    self._drop(key)
                    return None
        return helper

    def _record(self, key: str, ready: dict[str, Any]) -> None:
        backend = ready["backend"]
        with self._guard:
            first = key not in self._backends
            self._backends[key] = backend
        if not first:
            return
        test = ready.get("selftest") or {}
        if backend == "webgpu":
            logger.info("Credit text detection on {}: GPU ({} ms per frame, CPU {} ms)", key, test.get("gpu_ms"), test.get("cpu_ms"))
        else:
            logger.info("Credit text detection on {}: CPU ({})", key, ready.get("reason") or "the GPU self-test chose the CPU")

    def _use_cpu(self, key: str, reason: str, *, warning: bool = False) -> None:
        with self._guard:
            already = self._backends.get(key) == "cpu"
            self._backends[key] = "cpu"
        if already:
            return
        if warning:
            logger.warning("Credit text detection on {} moves to the CPU for the rest of this run of the app: {}", key, reason)
        else:
            logger.info("Credit text detection on {}: CPU ({})", key, reason)

    def _gpu_spec(self, key: str, gpu: str | None, gpu_device_path: str | None) -> HelperSpec:
        env = dict(os.environ)
        if gpu == "NVIDIA":
            # NVIDIA's ICD only loads with these (spec §5.4); they hide other vendors' GPUs, so only NVIDIA helpers get them.
            env.update(self._vulkan_env())
        with self._guard:
            known = key in self._backends
        return HelperSpec(key, "webgpu", worker_pci_bus_id(gpu, gpu_device_path), not known, env)

    def _cpu_spec(self) -> HelperSpec:
        return HelperSpec(CPU_KEY, "cpu", None, False, dict(os.environ))

    def _drop(self, key: str) -> None:
        with self._guard:
            helper = self._helpers.pop(key, None)
        if helper is not None:
            helper.kill()
            helper.close(0)


_pool: TextDetectorPool | None = None
_pool_lock = threading.Lock()


def get_textdet_pool() -> TextDetectorPool:
    """The process's helper pool (its helpers are stopped at exit)."""
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = TextDetectorPool()
            atexit.register(_pool.close_all)
        return _pool


# ----------------------------------------------------------------------------------------------- the helper process


def _send(protocol: BinaryIO, message: dict[str, Any]) -> None:
    protocol.write(json.dumps(message).encode() + b"\n")


def _start_detector(textdet: Any, args: argparse.Namespace) -> tuple[Any, dict[str, Any]]:
    cpu = textdet.TextDetector(textdet.cpu_session(args.model, args.threads), backend="cpu")
    if args.backend == "cpu":
        return cpu, {"backend": "cpu", "selftest": None, "reason": ""}
    try:
        found = textdet.webgpu_devices()
        index = choose_ep_device([dict(d.device.metadata) for d in found], args.pci_bus_id)
        if index is None:
            return cpu, {"backend": "cpu", "selftest": None, "reason": f"no WebGPU device is this GPU ({len(found)} found)"}
        gpu = textdet.TextDetector(textdet.webgpu_session(args.model, found[index], args.threads), backend="webgpu")
        if not args.selftest:
            return gpu, {"backend": "webgpu", "selftest": None, "reason": ""}
        result = self_test(gpu, cpu, textdet.synthetic_frames(SELFTEST_FRAMES))
    except textdet.ModelError:
        raise
    except Exception as exc:  # noqa: BLE001 - any EP or driver failure means the CPU
        return cpu, {"backend": "cpu", "selftest": None, "reason": f"the WebGPU session failed: {type(exc).__name__}: {exc}"}
    if result.use_gpu:
        return gpu, {"backend": "webgpu", "selftest": asdict(result), "reason": ""}
    why = "slower than the CPU" if result.same_boxes else "counting different boxes than the CPU"
    return cpu, {"backend": "cpu", "selftest": asdict(result), "reason": f"the GPU was {why}"}


def _serve(detector: Any, protocol: BinaryIO, idle_exit_s: float) -> int:
    stdin = sys.stdin.buffer
    while True:
        # The parent waits for each answer before it sends again, so nothing sits in stdin's buffer between requests.
        readable, _, _ = select.select([stdin], [], [], idle_exit_s)
        if not readable:
            return IDLE_EXIT_CODE
        header = stdin.readline()
        if not header:
            return 0
        request = json.loads(header)
        shape = (int(request["frames"]), int(request["height"]), int(request["width"]))
        data = stdin.read(shape[0] * shape[1] * shape[2])
        if len(data) != shape[0] * shape[1] * shape[2]:
            return 0
        try:
            _send(protocol, {"id": request["id"], "boxes": detector.count(np.frombuffer(data, np.uint8).reshape(shape))})
        except Exception as exc:  # noqa: BLE001 - the parent decides what a failed request means
            _send(protocol, {"id": request["id"], "error": f"{type(exc).__name__}: {exc}"})


def main(argv: list[str] | None = None) -> int:
    """The helper process: ``--check`` once, or serve box counts until stdin closes or it has been idle.

    Returns:
        0, IDLE_EXIT_CODE, or CHECK_ABSENT_CODE when the packages or the model aren't usable.
    """
    parser = argparse.ArgumentParser(prog=f"python -m {MODULE}")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--backend", choices=("webgpu", "cpu"), default="cpu")
    parser.add_argument("--model", default=model_path())
    parser.add_argument("--threads", type=int, default=THREADS)
    parser.add_argument("--pci-bus-id", default=None)
    parser.add_argument("--no-selftest", dest="selftest", action="store_false")
    parser.add_argument("--idle-exit-s", type=float, default=IDLE_EXIT_S)
    args = parser.parse_args(argv)
    protocol = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)  # a native library printing to stdout must not corrupt the protocol
    try:
        from . import textdet
    except ImportError as exc:
        _send_text(protocol, f"{NOT_INSTALLED} ({exc})")
        return CHECK_ABSENT_CODE
    try:
        if args.check:
            textdet.TextDetector(textdet.cpu_session(args.model, args.threads), backend="cpu").count(textdet.synthetic_frames(1))
            return 0
        detector, ready = _start_detector(textdet, args)
    except textdet.ModelError as exc:
        _send_text(protocol, str(exc))
        return CHECK_ABSENT_CODE
    _send(protocol, {"ready": True, **ready})
    return _serve(detector, protocol, args.idle_exit_s)


def _send_text(protocol: BinaryIO, text: str) -> None:
    protocol.write(text.encode() + b"\n")


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Run the unit tests**

Run: `/home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/markers/credits/test_devices.py tests/markers/credits/test_textdet_helper.py -q`
Expected: PASS (≈10 s with `-n 4`: the fake helpers are real processes; 34 helper tests and 29 device tests passed
while planning, and the two mutations marked † below were run). Mutation checks (each must fail the named test;
revert after):
- remove `with self._locks.hold(key):` → `test_requests_for_one_device_never_overlap`;
- apply the Vulkan env to every GPU → `test_a_vaapi_worker_gets_no_nvidia_overrides`;
- make `use_gpu` ignore `same_boxes` → `test_self_test_needs_the_same_boxes_and_more_speed`;
- † `if key != CPU_KEY and code != IDLE_EXIT_CODE:` → `if False:` → `test_a_gpu_helper_that_exits_between_requests_moves_the_device_to_the_cpu_for_good`;
- † `close()` closes stdin even while the writer is stuck → `test_a_helper_whose_pipe_outlives_the_kill_never_blocks_the_worker`
  (it hangs until pytest-timeout);
- `wait_exit(EXIT_CODE_WAIT_S)` → `exit_code()` → `test_an_idle_exit_as_a_request_arrives_starts_the_helper_again_on_the_gpu`
  (by timing: run it 20 times with `--count` or a loop and report how often it fails);
- drop the idle-margin branch → `test_a_helper_close_to_its_idle_exit_is_replaced_before_a_request`;
- `drm_pci_bus_id` back to `gpu.enumeration._get_pci_address_from_drm_device` → `test_render_node_maps_through_sysfs_with_hex_buses[0000:0a:00.0|0000:c1:00.0]`.

- [ ] **Step 6: Integration test with the real model (CPU helper, and WebGPU on storage)**

```python
# tests/markers/credits/test_textdet_helper_integration.py
"""The real helper process with the real model: the protocol round trip gives the in-process detector's counts."""

import os
import shutil

import pytest

from media_preview_generator.markers.credits import textdet_helper as th

pytestmark = pytest.mark.integration


@pytest.fixture
def model(monkeypatch):
    path = os.environ.get(th.MODEL_ENV, th.DEFAULT_MODEL_PATH)
    if not os.path.isfile(path):
        pytest.skip("no text detection model (set MEDIA_PREVIEW_TEXTDET_MODEL)")
    monkeypatch.setenv(th.MODEL_ENV, path)
    return path


def test_the_cpu_helper_counts_what_the_detector_counts(model):
    from media_preview_generator.markers.credits import textdet

    frames = textdet.synthetic_frames(20)
    expected = textdet.TextDetector(textdet.cpu_session(model), backend="cpu").count(frames)
    pool = th.TextDetectorPool()
    try:
        assert pool.count_boxes(frames, gpu=None, gpu_device_path=None) == expected
    finally:
        pool.close_all()


def test_the_check_passes(model):
    th.forget_text_detection_state()
    assert th.text_detection_status() == (True, "")


@pytest.mark.gpu
def test_webgpu_on_this_hosts_nvidia_gpu_matches_the_cpu(model):
    if shutil.which("nvidia-smi") is None:
        pytest.skip("no NVIDIA GPU")
    from media_preview_generator.markers.credits import textdet

    frames = textdet.synthetic_frames(20)
    expected = textdet.TextDetector(textdet.cpu_session(model), backend="cpu").count(frames)
    pool = th.TextDetectorPool()
    try:
        assert pool.count_boxes(frames, gpu="NVIDIA", gpu_device_path="cuda:0") == expected
        backend = pool.backend_of("NVIDIA", "cuda:0")
        # The self-test may pick the CPU on a busy host (the log line says why); EXPECT_WEBGPU=1 on storage demands the GPU.
        assert backend == "webgpu" if os.environ.get("EXPECT_WEBGPU") == "1" else backend in ("webgpu", "cpu")
    finally:
        pool.close_all()
```

Run: `MEDIA_PREVIEW_TEXTDET_MODEL="$MARKERS_BENCH_DIR/textdet-model/ch_PP-OCRv4_det_infer.onnx" nice -n 19 /home/data/.venv/bin/python -m pytest --no-cov -n 0 -m "integration" tests/markers/credits/test_textdet_helper_integration.py -q -s`
then on storage `EXPECT_WEBGPU=1 … -m "integration and gpu" …` (preflight M3: without it the GPU cell can't fail on
the GPU question).
Expected: PASS; with `-s`, the GPU run prints `Credit text detection on cuda:0: GPU (… ms per frame, CPU … ms)` on
storage's P5000 (host venv, as `gpu/RESULTS.md` measured 13.5 vs 18.1 ms; the planning run with this task's code
passed all three cells with `EXPECT_WEBGPU=1`). Record the line in the task report. If the self-test picks the CPU on
a busy host, rerun once when the host is quiet before reporting a failure.

- [ ] **Step 7: Commit**

Stage the six files; Architecture Review on the staged diff; then
`PATH="/home/data/.venv/bin:$PATH" git commit -m "feat(markers): text detection helper processes with GPU self-test and CPU fallback"`.

---
## Task 6: Frame decode (`frames.py`)

`[lane-parallel]` (after Tasks 2 and 3) `[high-risk]` — spec §5.4 "Frames" (the command, tail 900 s / 450 s, 1 fps
refine over the 20 s before the coarse answer, decode with the worker's hwaccel and CPU fallback, never per-frame
seeks), §5.6 (`-threads 2`), §6.4 items 4 and 10 (GPU error → CPU rerun; cancel), T-R4, T-R5, T-R6, T-R7, preflight
C2 (end of stream lost under slow text detection), I1 (a stuck child blocking the worker), I8 (bug-blind error cells),
M2 (`proc.wait` timeout).

High-risk: two pipes read at once (stdout carries up to ~40 MB of raw frames per tail, stderr the pts), a helper call
between chunks that can fail, cancellation and a hard timeout that must kill ffmpeg every time.

**Design.** `decode_command()` builds the argv. `run_decode()` streams stdout frames from a reader thread through a
bounded queue, hands every 64 luma planes to `count_boxes` (a text detection call, checked for cancel between chunks),
keeps `(boxes, luma)` per frame, and pairs them with the `pts_time` values `showinfo` wrote to stderr (a temp file)
once ffmpeg exits. Rows keep ffmpeg's order (T-R5), luma is rounded to 0.1 and pts to 0.001 (T-R6).

The queue holds two chunks. The reader waits for room for every frame **and for the end-of-stream sentinel** as long
as it takes (only the consumer's `stop` ends a wait), so text detection slower than the decoder never drops the end
(preflight C2 reproduced the old `put(_END, timeout=1.0)` dropping it on tails of 192/256/320 keyframes, each reported
as a 600 s timeout). ffmpeg runs in its own session; every failure path kills the process group with a bounded wait,
joins the reader for at most 2 s, and hands pipes something still holds to a daemon reaper instead of closing them on
the worker thread (preflight I1). Timeouts raise `DecodeTimeoutError` on either worker (never `GpuDecodeError`).

| Worker | Input args | Filter (then `,showinfo`) |
|---|---|---|
| NVIDIA `cuda:N` | `-hwaccel cuda -hwaccel_device N -hwaccel_output_format cuda` | `scale_cuda=320:180:format=nv12,hwdownload,format=nv12` (as measured) |
| INTEL/AMD `/dev/dri/renderD*` | `-hwaccel vaapi -hwaccel_device <node> -hwaccel_output_format vaapi` | `scale_vaapi=w=320:h=180:format=nv12,hwdownload,format=nv12` |
| WINDOWS_GPU / APPLE | `-hwaccel d3d11va` / `videotoolbox` | `scale=320:180,format=nv12` |
| CPU, or a GPU without a usable device | none | `scale=320:180,format=nv12` |

**Files:**
- Create: `media_preview_generator/markers/credits/frames.py`, `tests/markers/credits/test_frames.py`,
  `tests/markers/credits/test_frames_integration.py`
- Test: both test files

**Interfaces:**
- Consumes: Task 2 `processing.hwaccel.hwaccel_decode_args(gpu, gpu_device_path, *, keep_on_gpu) -> HwDecode`;
  Task 3 `rule_j.Row`.
- Produces (Task 8 and Task 11 rely on these):
```python
# media_preview_generator/markers/credits/frames.py
FRAME_W = 320; FRAME_H = 180; MOVIE_TAIL_S = 900.0; EPISODE_TAIL_S = 450.0; FFMPEG_THREADS = 2
CHUNK_FRAMES = 64; DECODE_TIMEOUT_S = 600.0
class FrameDecodeError(Exception)            # the decode failed (not a GPU problem)
class GpuDecodeError(FrameDecodeError)       # the GPU decode failed or gave no frames: rerun on the CPU
class DecodeTimeoutError(FrameDecodeError)   # past timeout_s on either worker: no answer, the detector waits a day
class DecodeCancelledError(Exception)
def tail_start_s(duration_ms: int, *, is_episode: bool) -> float
def decode_command(ffmpeg: str, path: str, *, start_s: float, length_s: float | None, keyframes_only: bool,
                   fps: int | None, gpu: str | None, gpu_device_path: str | None) -> tuple[list[str], bool]
def run_decode(command: list[str], *, hw_active: bool, count_boxes: Callable[[np.ndarray], list[int]],
               cancel_check: Callable[[], bool] | None = None, timeout_s: float = DECODE_TIMEOUT_S,
               chunk_frames: int = CHUNK_FRAMES, name: str = "") -> list[Row]
def decode_rows(path: str, *, ffmpeg: str, start_s: float, length_s: float | None, keyframes_only: bool,
                fps: int | None, gpu: str | None, gpu_device_path: str | None,
                count_boxes: Callable[[np.ndarray], list[int]], cancel_check: Callable[[], bool] | None = None,
                timeout_s: float = DECODE_TIMEOUT_S) -> list[Row]
```

- [ ] **Step 1: Write the failing tests**

```python
# tests/markers/credits/test_frames.py
"""Frame decode for credit text: the argv per worker, streaming, pts pairing, failures, cancel and timeout.

``run_decode`` runs a real child process (a small Python script standing in for ffmpeg), so pipes, the reader thread
and kills are exercised for real.
"""

from __future__ import annotations

import os
import signal
import sys
import textwrap
import threading
import time

import numpy as np
import pytest

from media_preview_generator.markers.credits import frames
from media_preview_generator.markers.credits.frames import (
    DecodeCancelledError,
    DecodeTimeoutError,
    FrameDecodeError,
    GpuDecodeError,
)

FF = "/usr/lib/jellyfin-ffmpeg/ffmpeg"
MOVIE = "/media/Movie (2020)/Movie.mkv"
TAIL = ["-an", "-sn", "-dn", "-fps_mode", "passthrough"]


class TestCommand:
    def test_nvidia_keyframes_of_the_tail(self):
        cmd, hw = frames.decode_command(FF, MOVIE, start_s=5100.0, length_s=None, keyframes_only=True, fps=None, gpu="NVIDIA", gpu_device_path="cuda:1")
        assert hw is True
        assert cmd == [FF, "-nostdin", "-hide_banner", "-loglevel", "info", "-threads", "2",
                       "-hwaccel", "cuda", "-hwaccel_device", "1", "-hwaccel_output_format", "cuda",
                       "-skip_frame", "nokey", "-ss", "5100.000", "-copyts", "-i", MOVIE, *TAIL,
                       "-vf", "scale_cuda=320:180:format=nv12,hwdownload,format=nv12,showinfo", "-f", "rawvideo", "-"]  # fmt: skip

    def test_vaapi_refine_window_at_1_fps(self):
        cmd, hw = frames.decode_command(FF, MOVIE, start_s=5680.5, length_s=21.0, keyframes_only=False, fps=1, gpu="INTEL", gpu_device_path="/dev/dri/renderD128")
        assert hw is True
        assert cmd == [FF, "-nostdin", "-hide_banner", "-loglevel", "info", "-threads", "2",
                       "-hwaccel", "vaapi", "-hwaccel_device", "/dev/dri/renderD128", "-hwaccel_output_format", "vaapi",
                       "-ss", "5680.500", "-t", "21.000", "-copyts", "-i", MOVIE, *TAIL,
                       "-vf", "fps=1,scale_vaapi=w=320:h=180:format=nv12,hwdownload,format=nv12,showinfo", "-f", "rawvideo", "-"]  # fmt: skip

    @pytest.mark.parametrize(
        ("gpu", "device", "hw_args", "hw"),
        [
            (None, None, [], False),
            ("INTEL", None, [], False),
            ("APPLE", "videotoolbox", ["-hwaccel", "videotoolbox"], True),
            ("WINDOWS_GPU", "d3d11va", ["-hwaccel", "d3d11va"], True),
        ],
    )
    def test_software_scaling_cells(self, gpu, device, hw_args, hw):
        cmd, active = frames.decode_command(FF, MOVIE, start_s=0.0, length_s=None, keyframes_only=True, fps=None, gpu=gpu, gpu_device_path=device)
        assert active is hw
        assert cmd == [FF, "-nostdin", "-hide_banner", "-loglevel", "info", "-threads", "2", *hw_args,
                       "-skip_frame", "nokey", "-ss", "0.000", "-copyts", "-i", MOVIE, *TAIL,
                       "-vf", "scale=320:180,format=nv12,showinfo", "-f", "rawvideo", "-"]  # fmt: skip

    @pytest.mark.parametrize(("duration_ms", "episode", "expected"), [(6_000_000, False, 5100.0), (1_320_000, True, 870.0), (300_000, True, 0.0), (600_000, False, 0.0)])
    def test_tail_start(self, duration_ms, episode, expected):
        assert frames.tail_start_s(duration_ms, is_episode=episode) == expected


def _fake_ffmpeg(frame_values: list[int], pts: list[str], *, exit_code: int = 0, sleep_s: float = 0.0, extra_bytes: int = 0) -> list[str]:
    """A child that writes NV12 frames (Y plane filled with each value) to stdout and showinfo lines to stderr."""
    script = textwrap.dedent(f"""
        import sys, time
        out = sys.stdout.buffer
        pts = {pts!r}
        for i, value in enumerate({frame_values!r}):
            if i < len(pts):
                sys.stderr.write("[Parsed_showinfo_3 @ 0x1] n:%d pts:%d pts_time:%s duration:1\\n" % (i, i, pts[i]))
            out.write(bytes([value]) * (320 * 180) + bytes([128]) * (320 * 90))
            out.flush()
            time.sleep({sleep_s})
        out.write(b"x" * {extra_bytes})
        sys.exit({exit_code})
    """)
    return [sys.executable, "-c", script]


def _counter(calls: list[np.ndarray]):
    def count(planes: np.ndarray) -> list[int]:
        calls.append(planes.copy())
        return [int(p[0, 0] > 200) * 3 for p in planes]

    return count


class TestRunDecode:
    def test_rows_pair_boxes_luma_and_pts_in_decode_order(self):
        calls: list[np.ndarray] = []
        values = [10, 250, 250, 120, 5]
        pts = ["5100.1234", "5102.5", "5101.9", "5104", "5106.0005"]
        rows = frames.run_decode(_fake_ffmpeg(values, pts), hw_active=False, count_boxes=_counter(calls), chunk_frames=2)
        assert rows == [(5100.123, 0, 10.0), (5102.5, 3, 250.0), (5101.9, 3, 250.0), (5104.0, 0, 120.0), (5106.001, 0, 5.0)]
        assert [c.shape for c in calls] == [(2, 180, 320), (2, 180, 320), (1, 180, 320)]
        assert calls[0].dtype == np.uint8 and int(calls[0][1, 5, 5]) == 250  # the Y plane, not the chroma

    def test_luma_is_the_mean_rounded_to_a_tenth(self):
        rows = frames.run_decode(_fake_ffmpeg([33], ["1"]), hw_active=False, count_boxes=lambda p: [0])
        assert rows == [(1.0, 0, 33.0)]

    def test_fewer_pts_than_frames_keeps_the_paired_frames(self):
        rows = frames.run_decode(_fake_ffmpeg([10, 10, 10], ["1", "2"]), hw_active=False, count_boxes=lambda p: [0] * len(p))
        assert [r[0] for r in rows] == [1.0, 2.0]

    def test_a_partial_trailing_frame_is_ignored(self):
        rows = frames.run_decode(_fake_ffmpeg([10], ["1"], extra_bytes=1000), hw_active=False, count_boxes=lambda p: [0] * len(p))
        assert rows == [(1.0, 0, 10.0)]

    @pytest.mark.parametrize(("hw", "error"), [(True, GpuDecodeError), (False, FrameDecodeError)])
    def test_a_non_zero_exit(self, hw, error):
        # Exact classes: a CPU exit must not look like a GPU failure (the worker would rerun it on the CPU again).
        with pytest.raises(FrameDecodeError, match="exited 3") as excinfo:
            frames.run_decode(_fake_ffmpeg([10], ["1"], exit_code=3), hw_active=hw, count_boxes=lambda p: [0] * len(p), name="Movie.mkv")
        assert type(excinfo.value) is error

    def test_no_frames_on_the_gpu_is_a_gpu_failure(self):
        with pytest.raises(GpuDecodeError, match="no frames") as excinfo:
            frames.run_decode(_fake_ffmpeg([], []), hw_active=True, count_boxes=lambda p: [0] * len(p))
        assert type(excinfo.value) is GpuDecodeError

    def test_no_frames_on_the_cpu_is_an_empty_answer(self):
        assert frames.run_decode(_fake_ffmpeg([], []), hw_active=False, count_boxes=lambda p: [0] * len(p)) == []

    def test_cancel_between_chunks_kills_ffmpeg(self):
        cancelled = threading.Event()
        calls: list[np.ndarray] = []

        def count(planes):
            calls.append(planes)
            cancelled.set()
            return [0] * len(planes)

        started = time.monotonic()
        with pytest.raises(DecodeCancelledError):
            frames.run_decode(_fake_ffmpeg([10] * 50, ["1"] * 50, sleep_s=0.2), hw_active=False, count_boxes=count, cancel_check=cancelled.is_set, chunk_frames=2)
        assert len(calls) == 1 and time.monotonic() - started < 5

    @pytest.mark.parametrize("hw", [True, False])
    def test_a_decode_past_the_timeout_is_killed_and_is_never_a_gpu_failure(self, hw):
        # T-R7: a stalled read times out the same on either worker; a GpuDecodeError would add a CPU rerun of the stall.
        started = time.monotonic()
        with pytest.raises(FrameDecodeError, match="timed out") as excinfo:
            frames.run_decode(_fake_ffmpeg([10] * 50, ["1"] * 50, sleep_s=1.0), hw_active=hw, count_boxes=lambda p: [0] * len(p), timeout_s=1.0)
        assert type(excinfo.value) is DecodeTimeoutError
        assert time.monotonic() - started < 6

    def test_ffmpeg_that_closes_its_output_but_never_exits_times_out(self):
        script = "import os, sys, time; sys.stdout.flush(); os.close(1); time.sleep(30)"
        started = time.monotonic()
        with pytest.raises(DecodeTimeoutError, match="timed out"):
            frames.run_decode([sys.executable, "-c", script], hw_active=False, count_boxes=lambda p: [0] * len(p), timeout_s=2.0)
        assert time.monotonic() - started < 9

    def test_a_process_holding_the_pipe_after_the_kill_never_blocks_the_worker(self, tmp_path):
        # A grandchild in its own session keeps stdout open after ffmpeg's group is killed (like a read stuck on a stalled
        # mount): the decode still returns within its limit plus the bounded waits, and the pipe is left to a reaper.
        pid_file = tmp_path / "holder.pid"
        script = textwrap.dedent(f"""
            import subprocess, sys, time
            holder = subprocess.Popen(["sleep", "30"], start_new_session=True)
            open({str(pid_file)!r}, "w").write(str(holder.pid))
            time.sleep(30)
        """)
        started = time.monotonic()
        try:
            with pytest.raises(DecodeTimeoutError):
                frames.run_decode([sys.executable, "-c", script], hw_active=False, count_boxes=lambda p: [0] * len(p), timeout_s=2.0)
            assert time.monotonic() - started < 2.0 + 6
        finally:
            if pid_file.exists():
                os.kill(int(pid_file.read_text()), signal.SIGKILL)

    def test_slow_text_detection_never_loses_the_end_of_the_stream(self):
        def slow(planes):
            time.sleep(1.5)
            return [0] * len(planes)

        started = time.monotonic()
        rows = frames.run_decode(_fake_ffmpeg([10, 10, 10], ["1", "2", "3"]), hw_active=False, count_boxes=slow, chunk_frames=1, timeout_s=12)
        assert [r[0] for r in rows] == [1.0, 2.0, 3.0]
        assert time.monotonic() - started < 8

    @pytest.mark.parametrize("count", [192, 256, 320])
    def test_whole_chunks_with_a_slow_detector_finish(self, count):
        # Reproduced before the fix: a tail with a multiple of 64 frames filled the queue while the last chunk was being
        # counted, dropped the end marker and was reported as a timeout.
        def slow(planes):
            time.sleep(1.2)
            return [0] * len(planes)

        started = time.monotonic()
        rows = frames.run_decode(_fake_ffmpeg([10] * count, [str(i) for i in range(count)]), hw_active=True, count_boxes=slow, timeout_s=20)
        assert len(rows) == count
        assert time.monotonic() - started < 1.2 * count / 64 + 6

    def test_a_failing_box_count_kills_ffmpeg_and_propagates(self):
        def count(planes):
            raise RuntimeError("helper gone")

        started = time.monotonic()
        with pytest.raises(RuntimeError, match="helper gone"):
            frames.run_decode(_fake_ffmpeg([10] * 50, ["1"] * 50, sleep_s=0.2), hw_active=False, count_boxes=count, chunk_frames=1)
        assert time.monotonic() - started < 5
```

```python
# tests/markers/credits/test_frames_integration.py
"""Real ffmpeg on a generated clip: keyframe rows of the tail and 1 fps rows, on the CPU and (when present) CUDA."""

import shutil
import subprocess

import pytest

from media_preview_generator.markers.credits import frames

pytestmark = pytest.mark.integration
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("no ffmpeg")
    path = tmp_path_factory.mktemp("credits") / "clip.mkv"
    # 0–60 s bright moving pattern without text, 60–90 s white names on black; a keyframe every 2 s.
    story = "mandelbrot=size=640x360:rate=24,trim=duration=60,setpts=PTS-STARTPTS"
    roll = f"color=c=black:size=640x360:rate=24:duration=30,drawtext=fontfile={FONT}:text='DIRECTED BY A NAME':fontcolor=white:fontsize=28:x=(w-tw)/2:y=h-40*t"
    subprocess.run(
        [ffmpeg, "-v", "error", "-f", "lavfi", "-i", story, "-f", "lavfi", "-i", roll, "-filter_complex",
         "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]", "-c:v", "libx264", "-g", "48", "-pix_fmt", "yuv420p",
         str(path)],
        check=True,
    )  # fmt: skip
    return ffmpeg, str(path)


def _rows(clip, **kwargs):
    ffmpeg, path = clip
    return frames.decode_rows(path, ffmpeg=ffmpeg, count_boxes=lambda planes: [0] * len(planes), **kwargs)


def test_cpu_keyframes_of_the_tail(clip):
    rows = _rows(clip, start_s=30.0, length_s=None, keyframes_only=True, fps=None, gpu=None, gpu_device_path=None)
    pts = [r[0] for r in rows]
    assert 25 <= len(rows) <= 32 and pts == sorted(pts) and 28.0 <= pts[0] <= 32.0
    assert all(r[2] < 30 for r in rows if r[0] >= 61)  # the roll is dark


def test_cpu_one_frame_a_second_before_a_time(clip):
    rows = _rows(clip, start_s=50.0, length_s=21.0, keyframes_only=False, fps=1, gpu=None, gpu_device_path=None)
    assert [round(r[0]) for r in rows] == list(range(50, 71))


@pytest.mark.gpu
def test_cuda_gives_the_same_timestamps(clip):
    if shutil.which("nvidia-smi") is None:
        pytest.skip("no NVIDIA GPU")
    cpu = _rows(clip, start_s=30.0, length_s=None, keyframes_only=True, fps=None, gpu=None, gpu_device_path=None)
    gpu = _rows(clip, start_s=30.0, length_s=None, keyframes_only=True, fps=None, gpu="NVIDIA", gpu_device_path="cuda:0")
    assert [r[0] for r in gpu] == [r[0] for r in cpu]
    assert all(abs(a[2] - b[2]) < 3 for a, b in zip(cpu, gpu, strict=True))
```

Run: `/home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/markers/credits/test_frames.py -q`
Expected: FAIL — `ImportError: cannot import name 'frames'`.

- [ ] **Step 2: Implement `frames.py`**

```python
# media_preview_generator/markers/credits/frames.py
"""Frames of a file's ending for credit text detection (spec §5.4): the keyframes of the tail, then one frame a second
just before the coarse answer. Decoded with the worker's GPU through the same hwaccel arguments as previews.

Only 320×180 luma leaves ffmpeg; each chunk of frames goes to text detection as it arrives, so memory stays bounded
whatever the file's keyframe spacing.
"""

from __future__ import annotations

import contextlib
import os
import queue
import re
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from typing import BinaryIO

import numpy as np
from loguru import logger

from ...processing.hwaccel import hwaccel_decode_args
from .rule_j import Row

FRAME_W = 320
FRAME_H = 180
MOVIE_TAIL_S = 900.0
EPISODE_TAIL_S = 450.0
FFMPEG_THREADS = 2
CHUNK_FRAMES = 64
DECODE_TIMEOUT_S = 600.0
_Y_BYTES = FRAME_W * FRAME_H
_NV12_BYTES = _Y_BYTES * 3 // 2
_PTS_RE = re.compile(rb"pts_time:\s*(-?[\d.]+)")
_POLL_S = 0.1
_KILL_WAIT_S = 5.0
_READER_JOIN_S = 2.0
_END = object()


class FrameDecodeError(Exception):
    """ffmpeg couldn't decode the frames."""


class GpuDecodeError(FrameDecodeError):
    """The GPU decode failed or gave no frames; the worker reruns the file on the CPU."""


class DecodeTimeoutError(FrameDecodeError):
    """The decode ran past its time limit (on the GPU or the CPU: never a reason to rerun on the CPU, T-R7)."""


class DecodeCancelledError(Exception):
    """The job was cancelled during the decode."""


def tail_start_s(duration_ms: int, *, is_episode: bool) -> float:
    """Where the tail starts: the last 450 s of an episode, the last 900 s of anything else (T-R4)."""
    tail = EPISODE_TAIL_S if is_episode else MOVIE_TAIL_S
    return max(0.0, duration_ms / 1000.0 - tail)


def _scale_filter(gpu: str | None, hw_active: bool, keep_on_gpu: bool) -> str:
    if hw_active and keep_on_gpu and gpu == "NVIDIA":
        return f"scale_cuda={FRAME_W}:{FRAME_H}:format=nv12,hwdownload,format=nv12"
    if hw_active and keep_on_gpu:
        return f"scale_vaapi=w={FRAME_W}:h={FRAME_H}:format=nv12,hwdownload,format=nv12"
    return f"scale={FRAME_W}:{FRAME_H},format=nv12"


def decode_command(
    ffmpeg: str,
    path: str,
    *,
    start_s: float,
    length_s: float | None,
    keyframes_only: bool,
    fps: int | None,
    gpu: str | None,
    gpu_device_path: str | None,
) -> tuple[list[str], bool]:
    """The spec §5.4 ffmpeg command for one decode.

    Args:
        ffmpeg: ffmpeg binary.
        path: The media file (read only).
        start_s: Where decoding starts (``-ss`` before the input, timestamps kept with ``-copyts``).
        length_s: How long to decode, or None to the end.
        keyframes_only: Decode keyframes only (the tail).
        fps: Frames per second to keep (the refine window), or None for every decoded frame.
        gpu: The worker's GPU type, None on a CPU worker.
        gpu_device_path: The worker's device.

    Returns:
        The argv, and whether decode runs on the GPU.
    """
    keep_on_gpu = gpu == "NVIDIA" or bool(gpu_device_path and gpu_device_path.startswith("/dev/dri/"))
    decode = hwaccel_decode_args(gpu, gpu_device_path, keep_on_gpu=keep_on_gpu)
    video_filter = _scale_filter(gpu, decode.active, keep_on_gpu)
    if fps:
        video_filter = f"fps={fps},{video_filter}"
    command = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "info", "-threads", str(FFMPEG_THREADS), *decode.args]
    if keyframes_only:
        command += ["-skip_frame", "nokey"]
    command += ["-ss", f"{start_s:.3f}"]
    if length_s is not None:
        command += ["-t", f"{length_s:.3f}"]
    command += ["-copyts", "-i", path, "-an", "-sn", "-dn", "-fps_mode", "passthrough",
                "-vf", f"{video_filter},showinfo", "-f", "rawvideo", "-"]  # fmt: skip
    return command, decode.active


def _read_frames(stream: BinaryIO, frames: queue.Queue, stop: threading.Event) -> None:
    """Reader thread: each whole frame's Y plane into the bounded queue, then ``_END``.

    Every put waits for room for as long as it takes (text detection on the consumer side can take longer per chunk than
    the reader takes to fill the queue), so neither a frame nor the end marker is ever dropped. Only ``stop`` (the
    consumer gave up) ends a wait.
    """

    def put(item: object) -> bool:
        while not stop.is_set():
            try:
                frames.put(item, timeout=_POLL_S)
                return True
            except queue.Full:
                continue
        return False

    try:
        while not stop.is_set():
            data = stream.read(_NV12_BYTES)
            if len(data) < _NV12_BYTES:
                break  # end of stream (a partial trailing frame is dropped)
            if not put(data[:_Y_BYTES]):
                return
    except (OSError, ValueError):
        pass
    put(_END)


def _kill(proc: subprocess.Popen) -> None:
    """Kill ffmpeg's whole process group (it runs in its own session) and wait a bounded time for it."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=_KILL_WAIT_S)


def _reap(proc: subprocess.Popen, reader: threading.Thread) -> None:
    reader.join()
    with contextlib.suppress(OSError, ValueError):
        proc.stdout.close()
    with contextlib.suppress(OSError):
        proc.wait()


def _release(proc: subprocess.Popen, reader: threading.Thread, stop: threading.Event, name: str) -> None:
    """Let go of the decode without ever blocking the worker on its pipe.

    A process stuck in an uninterruptible read (a stalled network mount) or anything else still holding the pipe keeps
    the reader blocked in ``read()``; closing the pipe then would wait on the reader's buffer lock. Such handles go to
    a daemon reaper that closes them once the reader lets go.
    """
    stop.set()
    reader.join(timeout=_READER_JOIN_S)
    if reader.is_alive() or proc.poll() is None:
        logger.warning(
            "ffmpeg for {} still holds its output after being stopped; leaving it to finish on its own", name
        )
        threading.Thread(target=_reap, args=(proc, reader), daemon=True, name="credits-frames-reaper").start()
        return
    with contextlib.suppress(OSError, ValueError):
        proc.stdout.close()


def run_decode(
    command: list[str],
    *,
    hw_active: bool,
    count_boxes: Callable[[np.ndarray], list[int]],
    cancel_check: Callable[[], bool] | None = None,
    timeout_s: float = DECODE_TIMEOUT_S,
    chunk_frames: int = CHUNK_FRAMES,
    name: str = "",
) -> list[Row]:
    """Run one decode and count text boxes chunk by chunk.

    Args:
        command: From :func:`decode_command`.
        hw_active: Decode runs on the GPU (a failure is then a :class:`GpuDecodeError`).
        count_boxes: Box counts for (n, 180, 320) uint8 luma planes.
        cancel_check: True once the job is cancelled; checked while waiting and between chunks.
        timeout_s: Hard limit for the whole decode (a stalled network mount must not hold a worker).
        chunk_frames: Frames per text detection request.
        name: The file's name for messages.

    Returns:
        ``(pts, boxes, luma)`` per frame, in decode order.

    Raises:
        DecodeCancelledError: Cancelled (ffmpeg is killed).
        GpuDecodeError: The GPU decode exited non-zero or gave no frames.
        DecodeTimeoutError: The decode (on the GPU or the CPU) ran past ``timeout_s``; ffmpeg is killed.
        FrameDecodeError: A CPU decode exited non-zero.
    """
    boxes: list[int] = []
    luma: list[float] = []
    pending: list[bytes] = []
    stop = threading.Event()
    frames: queue.Queue = queue.Queue(maxsize=chunk_frames * 2)
    deadline = time.monotonic() + timeout_s

    def flush() -> None:
        planes = np.frombuffer(b"".join(pending), dtype=np.uint8).reshape(len(pending), FRAME_H, FRAME_W)
        counts = count_boxes(planes)
        if len(counts) != len(pending):
            raise FrameDecodeError(f"text detection answered {len(counts)} counts for {len(pending)} frames")
        boxes.extend(int(c) for c in counts)
        luma.extend(round(float(plane.mean()), 1) for plane in planes)
        pending.clear()

    with tempfile.TemporaryFile() as stderr_file:
        # Its own session, so a kill reaches everything ffmpeg started; the app's process group is never signalled.
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=stderr_file, start_new_session=True)
        reader = threading.Thread(
            target=_read_frames, args=(proc.stdout, frames, stop), daemon=True, name="credits-frames"
        )
        reader.start()
        try:
            while True:
                if cancel_check and cancel_check():
                    raise DecodeCancelledError(f"cancelled while decoding {name}")
                if time.monotonic() > deadline:
                    raise DecodeTimeoutError(f"decoding {name} timed out after {timeout_s:g} s")
                try:
                    item = frames.get(timeout=_POLL_S)
                except queue.Empty:
                    if not reader.is_alive() and frames.empty():
                        break  # the reader died without its end marker (it only returns early once stopped)
                    continue
                if item is _END:
                    break
                pending.append(item)
                if len(pending) == chunk_frames:
                    flush()
            if pending:
                flush()
            try:
                returncode = proc.wait(timeout=max(1.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as exc:
                raise DecodeTimeoutError(f"decoding {name} timed out after {timeout_s:g} s") from exc
        except BaseException:
            _kill(proc)
            raise
        finally:
            _release(proc, reader, stop, name)
        stderr_file.seek(0)
        stderr = stderr_file.read()
    if returncode != 0:
        tail = stderr.decode("utf-8", "replace").strip()[-300:]
        where = "on the GPU" if hw_active else "on the CPU"
        error = GpuDecodeError if hw_active else FrameDecodeError
        raise error(f"ffmpeg exited {returncode} decoding {name} {where}: {tail}")
    if not boxes and hw_active:
        raise GpuDecodeError(f"the GPU decoded no frames from {name}")
    pts = [round(float(value), 3) for value in _PTS_RE.findall(stderr)]
    paired = min(len(boxes), len(pts))
    return [(pts[i], boxes[i], luma[i]) for i in range(paired)]


def decode_rows(
    path: str,
    *,
    ffmpeg: str,
    start_s: float,
    length_s: float | None,
    keyframes_only: bool,
    fps: int | None,
    gpu: str | None,
    gpu_device_path: str | None,
    count_boxes: Callable[[np.ndarray], list[int]],
    cancel_check: Callable[[], bool] | None = None,
    timeout_s: float = DECODE_TIMEOUT_S,
) -> list[Row]:
    """:func:`decode_command` then :func:`run_decode` (arguments as there)."""
    command, hw_active = decode_command(
        ffmpeg, path, start_s=start_s, length_s=length_s, keyframes_only=keyframes_only, fps=fps, gpu=gpu,
        gpu_device_path=gpu_device_path,
    )  # fmt: skip
    return run_decode(command, hw_active=hw_active, count_boxes=count_boxes, cancel_check=cancel_check,
                      timeout_s=timeout_s, name=os.path.basename(path))  # fmt: skip
```

- [ ] **Step 3: Run the tests**

Run: `/home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/markers/credits/test_frames.py -q`
Expected: PASS (28 tests, ≈30 s: the slow-detector cells sleep on purpose). While planning, the 8 cells added for
C2/I1/I8/M2 failed on the plan's earlier `frames.py` and pass on this one; the real-ffmpeg integration file passed on
the CPU and CUDA.

Mutation checks (each must fail the named test; revert after):
- `put(_END)` → `with contextlib.suppress(queue.Full): frames.put(_END, timeout=1.0)` →
  `test_slow_text_detection_never_loses_the_end_of_the_stream`, `test_whole_chunks_with_a_slow_detector_finish[192|256|320]`;
- `_release` closes `proc.stdout` without checking the reader → `test_a_process_holding_the_pipe_after_the_kill_never_blocks_the_worker`;
- drop `start_new_session=True` → the same test (the kill no longer reaches a child's group);
- raise `GpuDecodeError if hw_active else DecodeTimeoutError` on the deadline →
  `test_a_decode_past_the_timeout_is_killed_and_is_never_a_gpu_failure[True]`;
- let `proc.wait`'s `TimeoutExpired` escape → `test_ffmpeg_that_closes_its_output_but_never_exits_times_out`;
- swap the GPU/CPU exit classes → `test_a_non_zero_exit` (exact-type assert);
- `round(…, 1)` → no rounding → `test_luma_is_the_mean_rounded_to_a_tenth`.

Run: `nice -n 19 /home/data/.venv/bin/python -m pytest --no-cov -n 0 -m integration tests/markers/credits/test_frames_integration.py -q`
then `… -m "integration and gpu" …` on storage.
Expected: PASS. If the CUDA luma differs by 3 or more on any row, report the numbers (scale_cuda vs swscale); Task 11
measures the effect on rule J.

- [ ] **Step 4: Commit**

Stage the three files; Architecture Review on the staged diff; then
`PATH="/home/data/.venv/bin:$PATH" git commit -m "feat(markers): keyframe tail and refine decode for credit text"`.

---
## Task 7: Docker — pinned dependencies, model with sha256, size budget

`[lane-parallel]` (after Tasks 1, 4 and 5; beside Task 8) — roadmap phase 3 bullet 5 ("Docker: numpy, onnxruntime,
onnxruntime-ep-webgpu, opencv-python-headless; model downloaded at build with sha256"), spec §5.4 (sizes; "ICDs and
loader already in the image"), `.claude/rules/docker.md` (pin, verify, combine layers, clean up), Q8, preflight C3
(this task imports Task 5: it starts once Task 5 has landed), I4 (count every layer), M19 (the right baseline).

**OWNER DECISION (2026-09-16), Q8:** the image may grow by at most 250 MB uncompressed over the image built from the
commit before this task. Over that, this task stops and reports the numbers to the controller; it never raises the
budget or trims dependencies on its own.

**Files:**
- Create: `scripts/fetch_textdet_model.py`, `tests/test_dockerfile_textdet_model.py`
- Modify: `Dockerfile` (builder stage before "Layer A"; runtime stage after the wheel install)
- Test: `tests/test_dockerfile_textdet_model.py`

**Interfaces:**
- Consumes: Task 4 `textdet.MODEL_FILE`, `MODEL_SHA256`, `MODEL_SIZE`; Task 5 `textdet_helper.DEFAULT_MODEL_PATH`,
  `CHECK_ABSENT_CODE`; Task 1 M1 (installed sizes), M2 (URL and hashes).
- Produces: the image layout Tasks 10 and 13 rely on — `/app/models/ch_PP-OCRv4_det_infer.onnx`, the four wheels
  installed, `python3 -m media_preview_generator.markers.credits.textdet_helper --check` exiting 0.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_dockerfile_textdet_model.py
"""The image gets the pinned text detection model where the app looks for it, verified by sha256 at build."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import re
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fetch_textdet_model", ROOT / "scripts/fetch_textdet_model.py")
fetch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch)


def test_pins_match_the_detector_and_the_helper():
    from media_preview_generator.markers.credits import textdet, textdet_helper

    assert fetch.MODEL_SHA256 == textdet.MODEL_SHA256
    assert fetch.MODEL_SIZE == textdet.MODEL_SIZE
    assert fetch.MODEL_MEMBER.endswith("/" + textdet.MODEL_FILE)
    assert textdet_helper.DEFAULT_MODEL_PATH == f"/app/models/{textdet.MODEL_FILE}"
    assert re.fullmatch(r"[0-9a-f]{64}", fetch.WHEEL_SHA256)


def test_the_dockerfile_fetches_in_the_builder_and_copies_to_app_models():
    text = (ROOT / "Dockerfile").read_text()
    builder, runtime = text.split("# Stage 2: Runtime", 1)
    assert "COPY scripts/fetch_textdet_model.py /tmp/fetch_textdet_model.py" in builder
    assert "RUN python3 /tmp/fetch_textdet_model.py --out /models" in builder
    assert "COPY --from=builder /models/ch_PP-OCRv4_det_infer.onnx /app/models/ch_PP-OCRv4_det_infer.onnx" in runtime


def _wheel(model: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr(fetch.MODEL_MEMBER, model)
    return buffer.getvalue()


def test_verified_wheel_and_model_are_written(tmp_path, monkeypatch):
    model = b"model-bytes"
    wheel = _wheel(model)
    monkeypatch.setattr(fetch, "WHEEL_SHA256", hashlib.sha256(wheel).hexdigest())
    monkeypatch.setattr(fetch, "MODEL_SHA256", hashlib.sha256(model).hexdigest())
    monkeypatch.setattr(fetch, "MODEL_SIZE", len(model))
    monkeypatch.setattr(fetch, "_download", lambda url: wheel)
    assert fetch.main(["--out", str(tmp_path)]) == 0
    assert (tmp_path / "ch_PP-OCRv4_det_infer.onnx").read_bytes() == model


@pytest.mark.parametrize("broken", ["wheel", "model"])
def test_a_hash_mismatch_fails_the_build(tmp_path, monkeypatch, broken):
    model = b"model-bytes"
    wheel = _wheel(model)
    monkeypatch.setattr(fetch, "WHEEL_SHA256", "0" * 64 if broken == "wheel" else hashlib.sha256(wheel).hexdigest())
    monkeypatch.setattr(fetch, "MODEL_SHA256", "0" * 64 if broken == "model" else hashlib.sha256(model).hexdigest())
    monkeypatch.setattr(fetch, "MODEL_SIZE", len(model))
    monkeypatch.setattr(fetch, "_download", lambda url: wheel)
    with pytest.raises(SystemExit, match="sha256"):
        fetch.main(["--out", str(tmp_path)])
    assert not (tmp_path / "ch_PP-OCRv4_det_infer.onnx").exists()
```

Run: `/home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/test_dockerfile_textdet_model.py -q`
Expected: FAIL — `FileNotFoundError: …scripts/fetch_textdet_model.py`.

- [ ] **Step 2: Write the fetch script**

```python
# scripts/fetch_textdet_model.py
"""Fetch the credit text detection model for the Docker image (builder stage; stdlib only).

Downloads the pinned rapidocr_onnxruntime 1.4.4 wheel from PyPI, checks its sha256, takes the PP-OCRv4 detection model
out of it, checks that too, and writes it to --out. Any mismatch fails the build.

    python3 scripts/fetch_textdet_model.py --out /models
"""

from __future__ import annotations

import argparse
import hashlib
import io
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

WHEEL_URL = (
    "https://files.pythonhosted.org/packages/ba/12/1e5497183bdbe782dbb91bad1d0d2297dba4d2831b2652657f7517bfc6df/"
    "rapidocr_onnxruntime-1.4.4-py3-none-any.whl"
)
WHEEL_SHA256 = "971d7d5f223a7a808662229df1ef69893809d8457d834e6373d3854bc1782cbf"
MODEL_MEMBER = "rapidocr_onnxruntime/models/ch_PP-OCRv4_det_infer.onnx"
MODEL_SHA256 = "d2a7720d45a54257208b1e13e36a8479894cb74155a5efe29462512d42f49da9"
MODEL_SIZE = 4_745_517


def _download(url: str) -> bytes:
    for attempt in range(1, 6):
        try:
            with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - pinned https URL, hash-checked
                return response.read()
        except OSError:
            if attempt == 5:
                raise
            time.sleep(3 * attempt)
    raise AssertionError("unreachable")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    out = Path(parser.parse_args(argv).out)
    wheel = _download(WHEEL_URL)
    if hashlib.sha256(wheel).hexdigest() != WHEEL_SHA256:
        sys.exit("rapidocr_onnxruntime wheel sha256 mismatch")
    model = zipfile.ZipFile(io.BytesIO(wheel)).read(MODEL_MEMBER)
    if len(model) != MODEL_SIZE or hashlib.sha256(model).hexdigest() != MODEL_SHA256:
        sys.exit("text detection model sha256 mismatch")
    out.mkdir(parents=True, exist_ok=True)
    (out / MODEL_MEMBER.rsplit("/", 1)[1]).write_bytes(model)
    print(f"text detection model: {len(model)} bytes, sha256 {MODEL_SHA256}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Dockerfile**

Builder stage, right after `ENV PIP_BREAK_SYSTEM_PACKAGES=1` and before the "Layer A" comment block (its own layer, so
source and dependency changes never download the model again):

```dockerfile
# Credit text detection model (Intro & Credits, spec §5.4): PP-OCRv4 detection from the pinned rapidocr_onnxruntime
# 1.4.4 wheel, wheel and model both verified by sha256 (scripts/fetch_textdet_model.py).
COPY scripts/fetch_textdet_model.py /tmp/fetch_textdet_model.py
RUN python3 /tmp/fetch_textdet_model.py --out /models && rm /tmp/fetch_textdet_model.py
```

Runtime stage, directly after the `RUN pip3 install --no-cache-dir --no-index /tmp/wheels/*.whl …` layer:

```dockerfile
# Loaded only by the credit text detection helper process (markers/credits/textdet_helper.py).
COPY --from=builder /models/ch_PP-OCRv4_det_infer.onnx /app/models/ch_PP-OCRv4_det_infer.onnx
```

Nothing else changes: the four wheels come in through `pyproject.toml` (Task 4) and the existing builder `pip3 wheel`
layer; `mesa-vulkan-drivers` already brings the Vulkan loader and ICDs the WebGPU EP needs (spec §5.4).

Run: `/home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/test_dockerfile_textdet_model.py -q` — Expected: PASS.

- [ ] **Step 4: Build and check the image (storage)**

```bash
cd /home/data/workspace/plex_generate_vid_previews
VER=$(/home/data/.venv/bin/python -m setuptools_scm)
LOGS="$MARKERS_BENCH_DIR/logs"; mkdir -p "$LOGS"
# Baseline (preflight M19): the same Dockerfile rules at the commit this task starts from, not an older lab image.
BASE=$(git rev-parse HEAD)   # before this task commits: the commit it starts from
git worktree add /home/data/workspace/p3-lanes/docker-baseline "$BASE"
nice -n 19 docker build --build-arg SETUPTOOLS_SCM_PRETEND_VERSION="$VER" -t media_preview_generator:p3-baseline \
  /home/data/workspace/p3-lanes/docker-baseline > "$LOGS/build-p3-baseline.log" 2>&1; tail -3 "$LOGS/build-p3-baseline.log"
git worktree remove /home/data/workspace/p3-lanes/docker-baseline
nice -n 19 docker build --build-arg SETUPTOOLS_SCM_PRETEND_VERSION="$VER" -t media_preview_generator:intro-credits-p3 . \
  > "$LOGS/build-intro-credits-p3.log" 2>&1; tail -3 "$LOGS/build-intro-credits-p3.log"
BEFORE=$(docker image inspect -f '{{.Size}}' media_preview_generator:p3-baseline)
AFTER=$(docker image inspect -f '{{.Size}}' media_preview_generator:intro-credits-p3)
echo "image grew by $(( (AFTER - BEFORE) / 1000000 )) MB"
# Every layer counts (preflight I4): the builder's wheels are COPYed into the runtime stage in their own layer and stay
# in the image even though the next layer deletes them.
docker history --format '{{.Size}}\t{{.CreatedBy}}' media_preview_generator:intro-credits-p3 | head -25
docker history --format '{{.Size}}\t{{.CreatedBy}}' media_preview_generator:p3-baseline | head -25
IMG=media_preview_generator:intro-credits-p3
docker run --rm --entrypoint sha256sum "$IMG" /app/models/ch_PP-OCRv4_det_infer.onnx
docker run --rm --entrypoint python3 "$IMG" -c "import onnxruntime, cv2, pyclipper, onnxruntime_ep_webgpu; print(onnxruntime.__version__, cv2.__version__)"
docker run --rm --entrypoint python3 "$IMG" -m media_preview_generator.markers.credits.textdet_helper --check; echo "check exit $?"
docker run --rm --runtime=nvidia -e NVIDIA_VISIBLE_DEVICES=all -e NVIDIA_DRIVER_CAPABILITIES=all --device /dev/dri \
  --entrypoint python3 "$IMG" -c "
from media_preview_generator.markers.credits import textdet, textdet_helper as th
pool = th.TextDetectorPool()
print(pool.count_boxes(textdet.synthetic_frames(20), gpu='NVIDIA', gpu_device_path='cuda:0'), pool.backend_of('NVIDIA', 'cuda:0'))
pool.close_all()"
```

Expected: `image grew by N MB` with **N ≤ 250** (Q8). **If N > 250, stop here**: don't commit, and report to the
controller N, both `docker history` listings (the wheels layer's size before and after), and Task 1 M1's installed
sizes per package. The controller takes it to the owner (a known option to measure then, not to apply now: install
the wheels through `RUN --mount=type=bind,from=builder,source=/wheels,target=/tmp/wheels` instead of the COPY layer).
Otherwise:
the model's sha256 `d2a7720d…9da9`; `1.30.0 5.0.0`; `check exit 0`; the GPU run logs `Credit text detection on
cuda:0: GPU (…)` and prints the counts and `webgpu` (inside the image the NVIDIA ICD loads only with the app's probe
env, which the pool applies: this line is the in-image proof of spec §5.4's 13.3 ms path). The arm64 image builds in
CI's platform matrix (`ci.yml`); Task 14 checks it.

- [ ] **Step 5: Commit**

Stage `scripts/fetch_textdet_model.py`, the test and the `Dockerfile`; Architecture Review on the staged diff; then
`PATH="/home/data/.venv/bin:$PATH" git commit -m "build(docker): credit text detection model and runtime"`.

---
## Task 8: Credits text detector + pipeline integration

`[lane-parallel]` (after Tasks 3, 5, 6 and 9; beside Task 7) `[high-risk]` — spec §5.4 (the whole detector), §6.2 step 3 (evidence in source
order, stored answers carry their detector version; a chapter decision does **not** keep asking a local detector:
the landed `_detector_pending` runs one only for undecided types, C7), §6.4 items 2, 4, 7, 10 (same pool, worker
stage, GPU error → CPU rerun, cancel), §6.3 R1 (Emby: credits start only), 2026-09-14 §14 "Local detectors"
(versioned answers, `due`, `needs_worker`), 2026-09-15 §14 (the chromaprint tri-state: absent answers may hold a type
in review but never help decide it; unknown keeps them counting), phase-2 audit MED-1 (a failing file retried every
run), Q1, Q3, T-R4, T-R7, T-R9.

High-risk: it decides when a worker slot is spent per file and whether a stored answer can publish.

**OWNER DECISION (2026-09-16), Q1:** credits text publishes alone at Medium (`test_medium_publishes_it_alone_q1`).
**OWNER DECISION (2026-09-16), Q3:** the candidate ends at the chosen run's last credit frame (refined at 1 fps like the
start) when more than 30 s of the file follows it, else it has no end (`test_a_scene_after_the_roll_is_read_for_where_the_credits_end`,
`test_a_scene_after_the_credits_is_kept_q3`, `test_emby_gets_the_credits_start_and_skips_to_the_end_of_the_file_r1`).

**Files:**
- Create: `media_preview_generator/markers/credits/detector.py`, `tests/markers/credits/test_detector.py`,
  `tests/markers/test_pipeline_credits_text.py`, `tests/markers/test_store_credits_text.py`
- Modify: `media_preview_generator/markers/pipeline.py` (imports; `PipelineContext` new field `credits_text` after
  `chromaprint` ~234 with its docstring line; `default_local_detectors` ~414; `build_context` ~445; `_decide` ~631),
  `media_preview_generator/markers/store.py` (table `credits_text_timeouts` after `member_fingerprint_failures` ~183;
  two methods after `member_fingerprint_failed_at` ~1236), `tests/markers/conftest.py` (autouse fixture)
- Test: `tests/markers/credits/test_detector.py`, `tests/markers/test_pipeline_credits_text.py`,
  `tests/markers/test_store_credits_text.py`; existing `tests/markers/test_pipeline.py`,
  `tests/markers/test_pipeline_detectors.py`, `tests/markers/audio/test_season.py`, `tests/markers/test_store*.py`
  must pass unchanged

**Interfaces:**
- Consumes: Task 3 `rule_j.coarse_start`, `refine_start`, `coarse_end_s`, `keeps_a_scene_after`, `credits_end`,
  `REFINE_BEFORE_S`, `REFINE_AFTER_S`, `REFINE_END_BEFORE_S`, `REFINE_END_AFTER_S`, `Row`; Task 5 `TextDetState`,
  `text_detection_state()`, `get_textdet_pool().count_boxes(planes, *, gpu, gpu_device_path)`,
  `TextDetUnavailableError`; Task 6 `frames.decode_rows(...)`, `tail_start_s`, `FrameDecodeError`, `GpuDecodeError`,
  `DecodeTimeoutError`, `DecodeCancelledError`; phase 2 `LocalDetectorSpec`, `DetectorUnavailableError`,
  `PipelineContext.now/store/force`, `models.FileIdentity`, `processing.generator.CodecNotSupportedError`; phase 2 test
  helpers `tests.markers.test_emby_publisher.FakeEmby/_publisher/_write`, `publishers.emby.CREDITS_BEFORE_END_NOTE`.
- Produces (Tasks 10, 11, 13 rely on these):
```python
# media_preview_generator/markers/credits/detector.py
CREDITS_TEXT_VERSION = 1
READING_PHASE = "Reading the credits…"; REFINING_PHASE = "Refining the credits start…"
REFINING_END_PHASE = "Finding where the credits end…"
TIMEOUT_RETRY = timedelta(days=1)
@dataclass(frozen=True) class CreditsTextResult:
    start_s: float | None; end_s: float | None
    key_rows: tuple[Row, ...]; fine_rows: tuple[Row, ...]; end_rows: tuple[Row, ...]
def find_credits(path: str, *, duration_ms: int, is_episode: bool, ffmpeg: str,
                 count_boxes: Callable[[np.ndarray], list[int]], gpu: str | None, gpu_device_path: str | None,
                 cancel_check: Callable[[], bool] | None = None,
                 phase: Callable[[str], None] | None = None) -> CreditsTextResult
def detect_credits_text(rec: FileRecord, *, ctx: PipelineContext, gpu: str | None = None, gpu_device_path: str | None = None,
                        phase_callback: Callable[[str], None] | None = None, cancel_check: Callable[[], bool] | None = None,
                        pause_check: Callable[[], bool] | None = None) -> list[Candidate]
def credits_text_spec() -> LocalDetectorSpec
# pipeline.py
PipelineContext.credits_text: TextDetState = TextDetState.AVAILABLE
def default_local_detectors(settings, config, chromaprint=ChromaprintState.ABSENT,
                            credits_text: TextDetState = TextDetState.ABSENT) -> tuple[LocalDetectorSpec, ...]
# store.py
MarkerStore.record_credits_text_timeout(identity: FileIdentity, failed_at: datetime, *, forget_before: datetime) -> None
MarkerStore.credits_text_timed_out_at(identity: FileIdentity) -> datetime | None
```
A credits text candidate: `Candidate(MarkerType.CREDITS, round(start_s * 1000), None or round(end_s * 1000),
Source.CREDITS_TEXT)` (Q3); no roll found → `[]` (stored "nothing found"). A decode timeout stores nothing and records
the file's identity; until a day has passed (or the file changes, or the run is forced) the detector raises
`DetectorUnavailableError` on the worker without decoding. It still takes the worker hand-off (a few milliseconds):
deciding on the checking thread could, at the boundary, start a 600 s decode there.

- [ ] **Step 1: Write the failing detector tests**

```python
# tests/markers/credits/test_detector.py
"""The credit text detector: which decodes it asks for, what it stores, and how failures leave the detector."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from media_preview_generator.markers.credits import detector, frames, rule_j
from media_preview_generator.markers.credits.textdet_helper import TextDetUnavailableError
from media_preview_generator.markers.models import Candidate, FileIdentity, MarkerType, Source
from media_preview_generator.markers.pipeline import DetectorUnavailableError
from media_preview_generator.markers.store import FileRecord, MarkerStore
from media_preview_generator.processing.generator import CodecNotSupportedError

MOVIE = FileRecord(7, "/media/movies/Movie (2020)/Movie (2020).mkv", 100, 1, 6_000_000, None, True)
EPISODE = FileRecord(8, "/media/tv/Show/Season 01/Show - S01E01.mkv", 100, 1, 1_320_000, "/media/tv/Show/Season 01", False)
STORY = [(5100.0 + 2 * i, 0, 120.0) for i in range(300)]                  # 5100–5698 s, bright, no text
ROLL = [(5700.0 + 2 * i, 2, 12.0) for i in range(100)]                    # 5700–5898 s, dark cards
FINE = [(float(t), 0, 120.0) for t in range(5680, 5690)] + [(float(t), 2, 12.0) for t in range(5690, 5702)]
SCENE = [(5900.0 + 2 * i, 0, 120.0) for i in range(50)]                   # 5900–5998 s: a scene after the roll
END = [(5897.0, 2, 12.0), (5898.0, 2, 12.0), (5899.0, 1, 12.0)] + [(float(t), 0, 120.0) for t in range(5900, 5918)]


def count(planes):
    return [0] * len(planes)


class Decodes:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[dict] = []

    def __call__(self, path, **kwargs):
        self.calls.append({"path": path, **kwargs})
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


class TestFindCredits:
    def test_a_file_without_a_roll_decodes_only_the_tail(self, monkeypatch):
        decodes = Decodes(STORY)
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        phases: list[str] = []
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=6_000_000, is_episode=False, ffmpeg="/ff",
                                       count_boxes=count, gpu="NVIDIA", gpu_device_path="cuda:0", phase=phases.append)  # fmt: skip
        assert (result.start_s, result.end_s, result.fine_rows, result.end_rows) == (None, None, (), ())
        (call,) = decodes.calls
        assert call == {"path": MOVIE.canonical_path, "ffmpeg": "/ff", "start_s": 5100.0, "length_s": None,
                        "keyframes_only": True, "fps": None, "gpu": "NVIDIA", "gpu_device_path": "cuda:0",
                        "count_boxes": count, "cancel_check": None}  # fmt: skip
        assert phases == ["Reading the credits…"]

    def test_a_roll_to_the_end_of_the_file_is_refined_before_it_and_left_open_ended(self, monkeypatch):
        cancel = lambda: False  # noqa: E731
        decodes = Decodes(STORY + ROLL, FINE)
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        phases: list[str] = []
        # The roll's last keyframe is at 5898 s and the file ends at 5910 s: no scene follows, no end decode (Q3).
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=5_910_000, is_episode=False, ffmpeg="/ff",
                                       count_boxes=count, gpu=None, gpu_device_path=None, cancel_check=cancel,
                                       phase=phases.append)  # fmt: skip
        assert (result.start_s, result.end_s) == (rule_j.credits_start(STORY + ROLL, FINE), None) == (5690.0, None)
        assert len(decodes.calls) == 2
        fine = decodes.calls[1]
        assert (fine["start_s"], fine["length_s"], fine["keyframes_only"], fine["fps"], fine["cancel_check"]) == (5680.0, 21.0, False, 1, cancel)
        assert phases == ["Reading the credits…", "Refining the credits start…"]

    def test_a_scene_after_the_roll_is_read_for_where_the_credits_end(self, monkeypatch):
        decodes = Decodes(STORY + ROLL + SCENE, FINE, END)
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        phases: list[str] = []
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=6_000_000, is_episode=False, ffmpeg="/ff",
                                       count_boxes=count, gpu="NVIDIA", gpu_device_path="cuda:0", phase=phases.append)  # fmt: skip
        assert (result.start_s, result.end_s, result.end_rows) == (5690.0, 5899.0, tuple(END))
        end = decodes.calls[2]
        assert (end["start_s"], end["length_s"], end["keyframes_only"], end["fps"], end["gpu"]) == (5897.0, 21.0, False, 1, "NVIDIA")
        assert phases == ["Reading the credits…", "Refining the credits start…", "Finding where the credits end…"]

    def test_an_episode_reads_the_last_450_s(self, monkeypatch):
        decodes = Decodes([])
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        detector.find_credits(EPISODE.canonical_path, duration_ms=1_320_000, is_episode=True, ffmpeg="/ff",
                              count_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        assert decodes.calls[0]["start_s"] == 870.0

    def test_a_roll_at_the_start_of_the_file_refines_from_0(self, monkeypatch):
        roll = [(10.0 + 2 * i, 2, 12.0) for i in range(20)]
        decodes = Decodes(roll, [])
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        detector.find_credits("/m/short.mkv", duration_ms=50_000, is_episode=False, ffmpeg="/ff", count_boxes=count, gpu=None, gpu_device_path=None)
        assert (decodes.calls[1]["start_s"], decodes.calls[1]["length_s"]) == (0.0, 11.0)


class FakePool:
    def __init__(self):
        self.calls = []

    def count_boxes(self, planes, *, gpu, gpu_device_path):
        self.calls.append((gpu, gpu_device_path))
        return [0] * len(planes)


@pytest.fixture
def pool(monkeypatch):
    fake = FakePool()
    monkeypatch.setattr(detector, "get_textdet_pool", lambda: fake)
    return fake


NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def ctx(tmp_path):
    store = MarkerStore(str(tmp_path / "markers.db"))
    clock = SimpleNamespace(now=NOW)
    yield SimpleNamespace(config=SimpleNamespace(ffmpeg_path="/usr/lib/jellyfin-ffmpeg/ffmpeg"), force=False, store=store,
                          now=lambda: clock.now, clock=clock)  # fmt: skip
    store.close()


class TestDetect:
    def _find(self, monkeypatch, answer):
        seen: list[dict] = []

        def find(path, **kwargs):
            seen.append({"path": path, **kwargs})
            if isinstance(answer, BaseException):
                raise answer
            start, end = answer if isinstance(answer, tuple) else (answer, None)
            return detector.CreditsTextResult(start, end, (), (), ())

        monkeypatch.setattr(detector, "find_credits", find)
        return seen

    def test_a_roll_to_the_end_becomes_one_credits_candidate_without_an_end(self, monkeypatch, pool, ctx):
        seen = self._find(monkeypatch, 5690.4996)
        phase = []
        cancel = lambda: False  # noqa: E731 — identity is asserted: the detector must forward this exact callable
        assert detector.detect_credits_text(MOVIE, ctx=ctx, gpu="NVIDIA", gpu_device_path="cuda:0", phase_callback=phase.append, cancel_check=cancel) == [
            Candidate(MarkerType.CREDITS, 5_690_500, None, Source.CREDITS_TEXT)
        ]
        (call,) = seen
        assert (call["path"], call["duration_ms"], call["is_episode"], call["ffmpeg"], call["gpu"], call["gpu_device_path"]) == (
            MOVIE.canonical_path, 6_000_000, False, "/usr/lib/jellyfin-ffmpeg/ffmpeg", "NVIDIA", "cuda:0")  # fmt: skip
        assert call["cancel_check"] is cancel
        assert call["phase"] is not None
        call["count_boxes"](frames.np.zeros((2, 180, 320), frames.np.uint8))
        assert pool.calls == [("NVIDIA", "cuda:0")]

    def test_a_scene_after_the_roll_gives_the_candidate_its_end(self, monkeypatch, pool, ctx):
        self._find(monkeypatch, (5690.4996, 5899.0004))
        assert detector.detect_credits_text(MOVIE, ctx=ctx) == [Candidate(MarkerType.CREDITS, 5_690_500, 5_899_000, Source.CREDITS_TEXT)]

    @pytest.mark.parametrize(
        ("rec", "episode"),
        [
            (EPISODE, True),
            (MOVIE, False),
            # Neither a movie nor in a season (a "Pilot" file on its own): read as a movie. Only this cell tells
            # ``season_key is not None`` from ``not is_movie``.
            (FileRecord(9, "/m/Some Show - Pilot.mkv", 100, 1, 1_320_000, None, False), False),
        ],
    )
    def test_only_a_file_in_a_season_is_read_as_an_episode(self, monkeypatch, pool, ctx, rec, episode):
        seen = self._find(monkeypatch, None)
        assert detector.detect_credits_text(rec, ctx=ctx) == []
        assert seen[0]["is_episode"] is episode

    def test_a_gpu_decode_failure_is_a_codec_error_for_the_workers_cpu_rerun(self, monkeypatch, pool, ctx):
        self._find(monkeypatch, frames.GpuDecodeError("the GPU decoded no frames from Movie (2020).mkv"))
        with pytest.raises(CodecNotSupportedError, match="no frames"):
            detector.detect_credits_text(MOVIE, ctx=ctx, gpu="NVIDIA", gpu_device_path="cuda:0")

    @pytest.mark.parametrize(
        ("error", "message"),
        [
            (frames.FrameDecodeError("ffmpeg exited 1 decoding Movie (2020).mkv on the CPU"), "exited 1"),
            (frames.DecodeCancelledError("cancelled while decoding"), "cancelled"),
            (TextDetUnavailableError("Text detection failed: the helper exited"), "Text detection failed"),
        ],
    )
    def test_other_failures_are_no_answer_this_time_and_not_remembered(self, monkeypatch, pool, ctx, error, message):
        self._find(monkeypatch, error)
        with pytest.raises(DetectorUnavailableError, match=message):
            detector.detect_credits_text(MOVIE, ctx=ctx)
        assert ctx.store.credits_text_timed_out_at(FileIdentity(MOVIE.canonical_path, MOVIE.size, MOVIE.mtime_ns)) is None

    def test_a_timed_out_decode_waits_a_day_unless_forced_or_the_file_changes(self, monkeypatch, pool, ctx):
        seen = self._find(monkeypatch, frames.DecodeTimeoutError("decoding Movie (2020).mkv timed out after 600 s"))
        with pytest.raises(DetectorUnavailableError, match="timed out after 600 s"):
            detector.detect_credits_text(MOVIE, ctx=ctx)
        assert ctx.store.credits_text_timed_out_at(FileIdentity(MOVIE.canonical_path, MOVIE.size, MOVIE.mtime_ns)) == NOW
        ctx.clock.now = NOW + timedelta(hours=23)
        with pytest.raises(DetectorUnavailableError, match="less than a day ago"):
            detector.detect_credits_text(MOVIE, ctx=ctx)
        assert len(seen) == 1  # not decoded again
        changed = FileRecord(MOVIE.id, MOVIE.canonical_path, MOVIE.size + 1, MOVIE.mtime_ns, MOVIE.duration_ms, None, True)
        for rec, force in ((changed, False), (MOVIE, True)):
            ctx.force = force
            with pytest.raises(DetectorUnavailableError, match="timed out after"):
                detector.detect_credits_text(rec, ctx=ctx)
        assert len(seen) == 3  # each timed out again, so the latest entry is from 23 h
        ctx.force, ctx.clock.now = False, NOW + timedelta(hours=23) + timedelta(days=1, minutes=1)
        with pytest.raises(DetectorUnavailableError, match="timed out after"):
            detector.detect_credits_text(MOVIE, ctx=ctx)
        assert len(seen) == 4

    def test_an_unknown_duration_is_no_answer(self, pool, ctx):
        rec = FileRecord(9, "/m/x.mkv", 1, 1, None, None, True)
        with pytest.raises(DetectorUnavailableError, match="duration"):
            detector.detect_credits_text(rec, ctx=ctx)


def test_spec_always_needs_a_worker_and_carries_the_version():
    spec = detector.credits_text_spec()
    assert (spec.source, spec.types, spec.version) == (Source.CREDITS_TEXT, frozenset({MarkerType.CREDITS}), detector.CREDITS_TEXT_VERSION)
    assert (spec.stored_sources, spec.due, spec.needs_worker, spec.followups) == (frozenset({Source.CREDITS_TEXT}), None, None, None)
```

```python
# tests/markers/test_store_credits_text.py
"""Credit text decode timeouts in the store: kept per file identity, forgotten once old (Task 8, preflight I1)."""

from datetime import datetime, timedelta, timezone

import pytest

from media_preview_generator.markers.models import FileIdentity
from media_preview_generator.markers.store import MarkerStore

AT = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    s = MarkerStore(str(tmp_path / "markers.db"))
    yield s
    s.close()


def test_a_timeout_counts_only_for_the_identity_it_was_recorded_with(store):
    store.record_credits_text_timeout(FileIdentity("/m/Movie.mkv", 100, 1), AT, forget_before=AT)
    assert store.credits_text_timed_out_at(FileIdentity("/m/Movie.mkv", 100, 1)) == AT
    assert store.credits_text_timed_out_at(FileIdentity("/m/Movie.mkv", 200, 1)) is None
    assert store.credits_text_timed_out_at(FileIdentity("/m/Movie.mkv", 100, 2)) is None
    assert store.member_fingerprint_failed_at(FileIdentity("/m/Movie.mkv", 100, 1)) is None


def test_a_newer_timeout_replaces_the_paths_entry(store):
    store.record_credits_text_timeout(FileIdentity("/m/Movie.mkv", 100, 1), AT, forget_before=AT - timedelta(days=1))
    later = AT + timedelta(hours=2)
    store.record_credits_text_timeout(
        FileIdentity("/m/Movie.mkv", 300, 3), later, forget_before=later - timedelta(days=1)
    )
    assert store.credits_text_timed_out_at(FileIdentity("/m/Movie.mkv", 100, 1)) is None
    assert store.credits_text_timed_out_at(FileIdentity("/m/Movie.mkv", 300, 3)) == later
    assert store._count("credits_text_timeouts") == 1


def test_recording_a_timeout_forgets_entries_that_stopped_counting_for_any_path(store):
    gone = FileIdentity("/m/gone/Movie.mkv", 100, 1)
    recent = FileIdentity("/m/Other.mkv", 100, 1)
    store.record_credits_text_timeout(gone, AT, forget_before=AT - timedelta(days=1))
    store.record_credits_text_timeout(recent, AT + timedelta(hours=12), forget_before=AT)
    later = AT + timedelta(days=1, hours=6)
    store.record_credits_text_timeout(
        FileIdentity("/m/Third.mkv", 100, 1), later, forget_before=later - timedelta(days=1)
    )
    assert store.credits_text_timed_out_at(gone) is None
    assert store.credits_text_timed_out_at(recent) == AT + timedelta(hours=12)
    assert store._count("credits_text_timeouts") == 2
```

Run: `/home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/markers/credits/test_detector.py tests/markers/test_store_credits_text.py -q`
Expected: FAIL — `ImportError: cannot import name 'detector'` and `AttributeError: 'MarkerStore' object has no attribute
'record_credits_text_timeout'`.

**Before Step 2 (preflight M18): these tests and Step 3's weren't all dry-run while planning against the branch.** Run
each file once against the branch with a stub `detector.py` that only defines the names, and report any failure that
isn't "not implemented" (a changed helper in `tests/markers/test_pipeline.py` or `test_emby_publisher.py`) before
writing the code.

- [ ] **Step 2: Implement `detector.py` and the store's timeout table**

In `media_preview_generator/markers/store.py`, after the `member_fingerprint_failures` table in the schema list:

```python
    # A file whose credit text decode timed out (a stalled read, spec §5.4), with the identity it had then: its credit
    # text isn't decoded again (up to 600 s on a worker) until that identity changes, the entry is a day old, or a
    # forced re-detect. Not tied to a file row, like the member failures above.
    """CREATE TABLE IF NOT EXISTS credits_text_timeouts (
        canonical_path TEXT PRIMARY KEY,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        failed_at TEXT NOT NULL)""",
```

and after `member_fingerprint_failed_at`:

```python
    def record_credits_text_timeout(
        self, identity: FileIdentity, failed_at: datetime, *, forget_before: datetime
    ) -> None:
        """Remember that a file with this identity timed out decoding its credit text (replaces the path's older entry).

        Entries that no longer hold a file back are forgotten in the same write, as for member failures.

        Args:
            identity: The file as it was when the decode timed out.
            failed_at: When (the job's clock).
            forget_before: Entries of any path that timed out before this are removed.
        """
        with self._tx() as conn:
            conn.execute("DELETE FROM credits_text_timeouts WHERE failed_at < ?", (forget_before.isoformat(),))
            conn.execute(
                "INSERT OR REPLACE INTO credits_text_timeouts (canonical_path, size, mtime_ns, failed_at) VALUES (?,?,?,?)",
                (identity.canonical_path, identity.size, identity.mtime_ns, failed_at.isoformat()),
            )

    def credits_text_timed_out_at(self, identity: FileIdentity) -> datetime | None:
        """When decoding this identity's credit text last timed out, or None (never, or another identity)."""
        with self._lock:
            r = self._conn.execute(
                "SELECT failed_at FROM credits_text_timeouts WHERE canonical_path=? AND size=? AND mtime_ns=?",
                (identity.canonical_path, identity.size, identity.mtime_ns),
            ).fetchone()
        return datetime.fromisoformat(r["failed_at"]) if r else None
```

(`CREATE TABLE IF NOT EXISTS` in the schema list: existing stores get the table on open; no migration.)

```python
# media_preview_generator/markers/credits/detector.py
"""The credit text detector (spec §5.4): keyframes of the tail → rule J → one frame a second just before its start (and
around its end when a scene follows the roll, Q3) → one credits candidate from ``credits_text``. The pipeline registers
it as a local detector that runs on a worker."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

import numpy as np
from loguru import logger

from ..models import Candidate, FileIdentity, MarkerType, Source
from . import frames, rule_j
from .textdet_helper import TextDetUnavailableError, get_textdet_pool

if TYPE_CHECKING:
    from ..pipeline import LocalDetectorSpec, PipelineContext
    from ..store import FileRecord

# Stored with every answer. Bump it when rule J, the tail lengths, the frame format or the model change: stored answers
# of another version are asked again, even for decided types (spec §14 2026-09-14 "Local detectors").
CREDITS_TEXT_VERSION = 1
READING_PHASE = "Reading the credits…"
REFINING_PHASE = "Refining the credits start…"
REFINING_END_PHASE = "Finding where the credits end…"
# A file whose decode timed out isn't decoded again for this long unless it changes or the run is forced (I1).
TIMEOUT_RETRY = timedelta(days=1)


@dataclass(frozen=True)
class CreditsTextResult:
    """What one file's ending gave.

    Attributes:
        start_s: The credits start, or None when the tail holds no credit run.
        end_s: Where the skip ends (Q3), or None: it runs to the end of the file.
        key_rows: The tail's keyframe rows (the harness keeps them).
        fine_rows: The 1 fps rows before the coarse start (empty without a run).
        end_rows: The 1 fps rows around the run's last credit keyframe (empty unless more than 30 s follows it).
    """

    start_s: float | None
    end_s: float | None
    key_rows: tuple[rule_j.Row, ...]
    fine_rows: tuple[rule_j.Row, ...]
    end_rows: tuple[rule_j.Row, ...]


def find_credits(
    path: str,
    *,
    duration_ms: int,
    is_episode: bool,
    ffmpeg: str,
    count_boxes: Callable[[np.ndarray], list[int]],
    gpu: str | None,
    gpu_device_path: str | None,
    cancel_check: Callable[[], bool] | None = None,
    phase: Callable[[str], None] | None = None,
) -> CreditsTextResult:
    """Decode the tail, find the roll, refine its start and, when a scene follows it, its end (the app and the harness
    run exactly this).

    Args:
        path: The media file (read only).
        duration_ms: Its duration.
        is_episode: Read the last 450 s instead of 900 s (T-R4).
        ffmpeg: ffmpeg binary.
        count_boxes: Text boxes per chunk of luma planes.
        gpu: The worker's GPU type, None on a CPU worker.
        gpu_device_path: The worker's device.
        cancel_check: True once the job is cancelled.
        phase: Shows the step on the worker row.

    Returns:
        The start, the end, and the rows they came from.

    Raises:
        frames.GpuDecodeError, frames.DecodeTimeoutError, frames.FrameDecodeError, frames.DecodeCancelledError,
        TextDetUnavailableError.
    """
    show = phase or (lambda _text: None)
    decode = {"ffmpeg": ffmpeg, "gpu": gpu, "gpu_device_path": gpu_device_path, "count_boxes": count_boxes,
              "cancel_check": cancel_check}  # fmt: skip
    show(READING_PHASE)
    tail_start = frames.tail_start_s(duration_ms, is_episode=is_episode)
    key_rows = frames.decode_rows(path, start_s=tail_start, length_s=None, keyframes_only=True, fps=None, **decode)
    coarse = rule_j.coarse_start(key_rows)
    if coarse is None:
        return CreditsTextResult(None, None, tuple(key_rows), (), ())
    show(REFINING_PHASE)
    fine_start = max(0.0, coarse.pts_s - rule_j.REFINE_BEFORE_S)
    fine_length = coarse.pts_s + rule_j.REFINE_AFTER_S - fine_start
    fine_rows = frames.decode_rows(path, start_s=fine_start, length_s=fine_length, keyframes_only=False, fps=1, **decode)
    start_s = rule_j.refine_start(key_rows, coarse, fine_rows)
    duration_s = duration_ms / 1000.0
    last_keyframe = rule_j.coarse_end_s(key_rows, coarse)
    if not rule_j.keeps_a_scene_after(last_keyframe, duration_s):
        return CreditsTextResult(start_s, None, tuple(key_rows), tuple(fine_rows), ())
    show(REFINING_END_PHASE)
    end_start = max(0.0, last_keyframe - rule_j.REFINE_END_BEFORE_S)
    end_length = last_keyframe + rule_j.REFINE_END_AFTER_S - end_start  # more than 30 s of the file follows
    end_rows = frames.decode_rows(path, start_s=end_start, length_s=end_length, keyframes_only=False, fps=1, **decode)
    end_s = rule_j.credits_end(key_rows, coarse, end_rows, duration_s)
    return CreditsTextResult(start_s, end_s, tuple(key_rows), tuple(fine_rows), tuple(end_rows))


def _timed_out_lately(rec: FileRecord, ctx: PipelineContext) -> bool:
    if ctx.force:
        return False
    failed_at = ctx.store.credits_text_timed_out_at(FileIdentity(rec.canonical_path, rec.size, rec.mtime_ns))
    return failed_at is not None and ctx.now() - failed_at < TIMEOUT_RETRY


def detect_credits_text(
    rec: FileRecord,
    *,
    ctx: PipelineContext,
    gpu: str | None = None,
    gpu_device_path: str | None = None,
    phase_callback: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    pause_check: Callable[[], bool] | None = None,
) -> list[Candidate]:
    """The local detector: a credits candidate from the file's on-screen credit roll.

    A paused job doesn't block the worker here (T-R9): one file's decode is bounded, and the job pauses between files.

    Args:
        rec: The file (its identity matches the disk: the pipeline just checked).
        ctx: The job's context (``config.ffmpeg_path``, ``store``, ``now``, ``force``).
        gpu: The worker's GPU type, None on a CPU worker.
        gpu_device_path: The worker's device.
        phase_callback: Worker row step text.
        cancel_check: True once the job is cancelled.
        pause_check: Unused.

    Returns:
        One candidate (its end None when the roll runs to the end of the file, Q3), or [] when the tail holds no credit
        roll ("nothing found").

    Raises:
        CodecNotSupportedError: The GPU decode failed; the worker reruns the file on the CPU.
        DetectorUnavailableError: No answer this time (duration unknown, decode failed, timed out now or in the last
            day, cancelled, text detection failed); nothing is stored.
    """
    from ...processing.generator import CodecNotSupportedError
    from ..pipeline import DetectorUnavailableError

    if not rec.duration_ms:
        raise DetectorUnavailableError("the file's duration is unknown")
    if _timed_out_lately(rec, ctx):
        raise DetectorUnavailableError("reading its ending timed out less than a day ago; a forced re-detect tries now")
    pool = get_textdet_pool()
    try:
        result = find_credits(
            rec.canonical_path,
            duration_ms=rec.duration_ms,
            is_episode=rec.season_key is not None,
            ffmpeg=getattr(ctx.config, "ffmpeg_path", None) or "ffmpeg",
            count_boxes=lambda planes: pool.count_boxes(planes, gpu=gpu, gpu_device_path=gpu_device_path),
            gpu=gpu,
            gpu_device_path=gpu_device_path,
            cancel_check=cancel_check,
            phase=phase_callback,
        )
    except frames.GpuDecodeError as exc:
        raise CodecNotSupportedError(str(exc)) from exc
    except frames.DecodeCancelledError as exc:
        raise DetectorUnavailableError("cancelled") from exc
    except frames.DecodeTimeoutError as exc:
        now = ctx.now()
        identity = FileIdentity(rec.canonical_path, rec.size, rec.mtime_ns)
        ctx.store.record_credits_text_timeout(identity, now, forget_before=now - TIMEOUT_RETRY)
        raise DetectorUnavailableError(str(exc)) from exc
    except (frames.FrameDecodeError, TextDetUnavailableError) as exc:
        raise DetectorUnavailableError(str(exc)) from exc
    if result.start_s is None:
        logger.debug("No credit roll in the end of {}", os.path.basename(rec.canonical_path))
        return []
    end_ms = None if result.end_s is None else int(round(result.end_s * 1000))
    return [Candidate(MarkerType.CREDITS, int(round(result.start_s * 1000)), end_ms, Source.CREDITS_TEXT)]


def credits_text_spec() -> LocalDetectorSpec:
    """The detector as the pipeline registers it: credits only, always on a worker, answers kept per file identity."""
    from ..pipeline import LocalDetectorSpec

    return LocalDetectorSpec(
        source=Source.CREDITS_TEXT,
        types=frozenset({MarkerType.CREDITS}),
        detect=detect_credits_text,
        version=CREDITS_TEXT_VERSION,
    )
```

Run: `/home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/markers/credits/test_detector.py tests/markers/test_store_credits_text.py -q` — Expected: PASS.

- [ ] **Step 3: Write the failing pipeline tests**

```python
# tests/markers/test_pipeline_credits_text.py
"""Credits text in the pipeline: worker hand-off, stored versioned answers, forced runs, the tri-state, registration."""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.markers import pipeline
from media_preview_generator.markers.credits import detector, frames
from media_preview_generator.markers.credits.textdet_helper import TextDetState
from media_preview_generator.markers.decide import DecisionStatus
from media_preview_generator.markers.models import Candidate, Marker, MarkerType, Source
from media_preview_generator.markers.outcomes import FileOutcome
from media_preview_generator.markers.probe import Chapter
from media_preview_generator.markers.settings import load_global, validate_global
from media_preview_generator.markers.sources.online import LookupResult
from media_preview_generator.processing.generator import CodecNotSupportedError
from media_preview_generator.servers.base import ServerType
from tests.markers import test_pipeline
from tests.markers.fakes import ready_publisher
from tests.markers.test_pipeline import DUR, _clients, _ctx, _probe, _registry, _run

media = test_pipeline.media
store = test_pipeline.store
T = MarkerType
START_S = 1_290.25


def settings(level="high", **sources):
    enabled = {"chapters": True, "theintrodb": False, "introdb": False, "skipdb": False, "season_audio": False,
               "credits_text": True, "server_markers": False, **sources}  # fmt: skip
    return {"detect": {"intro": False, "credits": True}, "publish_when": level,
            "sources": [{"id": k, "enabled": v} for k, v in enabled.items()]}  # fmt: skip


@pytest.fixture
def find(monkeypatch):
    """The detector's decode + rule J, answering START_S with no end; tests change ``find.answer`` (a start, a
    ``(start, end)`` pair, None or an exception)."""
    calls: list[dict] = []

    def fake(path, **kwargs):
        calls.append({"path": path, **kwargs})
        if isinstance(fake.answer, BaseException):
            raise fake.answer
        start, end = fake.answer if isinstance(fake.answer, tuple) else (fake.answer, None)
        return detector.CreditsTextResult(start, end, (), (), ())

    fake.answer = START_S
    fake.calls = calls
    monkeypatch.setattr(detector, "find_credits", fake)
    monkeypatch.setattr(detector, "get_textdet_pool", MagicMock)
    return fake


def ctx_for(store, media, level="high", *, force=False, clients=None, **sources):
    return _ctx(store, _registry(media, ServerType.PLEX), settings_raw=settings(level, **sources),
                detectors=(detector.credits_text_spec(),), force=force, clients=clients)  # fmt: skip


def pubs():
    return {"plex-1": ready_publisher()}


class TestWorkerHandOff:
    def test_the_check_stage_hands_an_undecided_file_to_a_worker(self, store, media, find):
        out, _ = _run(ctx_for(store, media), media, pubs(), stage="check")
        assert out is None and find.calls == []

    def test_the_worker_runs_it_on_its_gpu_and_stores_a_versioned_answer(self, store, media, find):
        ctx = ctx_for(store, media)
        ctx.config.ffmpeg_path = "/usr/lib/jellyfin-ffmpeg/ffmpeg"
        out, _ = _run(ctx, media, pubs(), stage="process", gpu="NVIDIA", gpu_device_path="cuda:0")
        (call,) = find.calls
        assert (call["path"], call["duration_ms"], call["is_episode"], call["ffmpeg"], call["gpu"], call["gpu_device_path"]) == (
            media, DUR, True, "/usr/lib/jellyfin-ffmpeg/ffmpeg", "NVIDIA", "cuda:0")  # fmt: skip
        rec = store.get_file(media)
        assert store.get_evidence(rec.id) == [Candidate(T.CREDITS, 1_290_250, None, Source.CREDITS_TEXT)]
        assert store.evidence_version(rec.id, Source.CREDITS_TEXT) == detector.CREDITS_TEXT_VERSION
        assert out.outcome_key == FileOutcome.NEEDS_REVIEW.value  # one source at High

    def test_medium_publishes_it_alone_q1(self, store, media, find):
        plex = ready_publisher()
        out, _ = _run(ctx_for(store, media, "medium"), media, {"plex-1": plex}, stage="process")
        assert out.outcome_key == FileOutcome.PUBLISHED.value
        assert plex.write.call_args.args == ("item-plex-1", [Marker(T.CREDITS, 1_290_250, DUR, ("credits_text",))])

    def test_a_scene_after_the_credits_is_kept_q3(self, store, media, find):
        find.answer = (1_200.0, 1_260.0)  # the roll ends 61 s before the end of the file
        plex = ready_publisher()
        out, _ = _run(ctx_for(store, media, "medium"), media, {"plex-1": plex}, stage="process")
        assert store.get_evidence(store.get_file(media).id) == [Candidate(T.CREDITS, 1_200_000, 1_260_000, Source.CREDITS_TEXT)]
        assert plex.write.call_args.args == ("item-plex-1", [Marker(T.CREDITS, 1_200_000, 1_260_000, ("credits_text",))])

    def test_emby_gets_the_credits_start_and_skips_to_the_end_of_the_file_r1(self, store, media, find, tmp_path):
        # Spec §6.3 R1 is unchanged by Q3: Emby has no credits end, so it gets the start and the row says so.
        from media_preview_generator.markers.publishers.emby import CREDITS_BEFORE_END_NOTE
        from tests.markers.test_emby_publisher import FakeEmby, _publisher, _write

        find.answer = (1_200.0, 1_260.0)
        plex = ready_publisher()
        _run(ctx_for(store, media, "medium"), media, {"plex-1": plex}, stage="process")
        (decided,) = plex.write.call_args.args[1]
        emby = FakeEmby(str(media))
        emby.publisher = _publisher(emby)
        assert _write(emby, [decided]) == [decided]
        sent = emby.server.put_emby_markers.call_args.kwargs
        assert sent["credits_start_ticks"] == 12_000_000_000 and not any("end" in k for k in sent if k.startswith("credits"))
        assert emby.publisher.projection_note([decided], duration_ms=DUR) == CREDITS_BEFORE_END_NOTE

    def test_credits_decided_by_other_sources_never_take_a_worker(self, store, media, find):
        clients = _clients(skipdb=LookupResult("ok", (Candidate(T.CREDITS, 1_291_000, None, Source.SKIPDB),)),
                           introdb=LookupResult("ok", (Candidate(T.CREDITS, 1_289_000, None, Source.INTRODB),)))  # fmt: skip
        out, _ = _run(ctx_for(store, media, clients=clients, skipdb=True, introdb=True), media, pubs(), stage="check")
        assert out is not None and out.outcome_key == FileOutcome.PUBLISHED.value
        assert find.calls == []

    def test_credits_a_chapter_decided_never_take_a_worker(self, store, media, find):
        # Local detectors run only for undecided types or answers they supplied (``_detector_pending``); online sources
        # still confirm or veto the chapter (rule 3) without a worker.
        chapters = _probe((Chapter(0, 1_290_000, "Episode"), Chapter(1_290_000, None, "Credits")))
        out, _ = _run(ctx_for(store, media), media, pubs(), probe=chapters, stage="check")
        assert out is not None and out.outcome_key == FileOutcome.PUBLISHED.value
        assert find.calls == []


class TestStoredAnswers:
    def test_a_current_answer_is_not_read_again_but_a_forced_run_reads_it(self, store, media, find):
        _run(ctx_for(store, media), media, pubs(), stage="process")
        out, _ = _run(ctx_for(store, media), media, pubs(), stage="check")
        assert out is not None and len(find.calls) == 1
        _run(ctx_for(store, media, force=True), media, pubs(), stage="process")
        assert len(find.calls) == 2

    def test_a_changed_file_is_read_again(self, store, media, find):
        _run(ctx_for(store, media), media, pubs(), stage="process")
        with open(media, "ab") as fh:
            fh.write(b"more")
        _run(ctx_for(store, media), media, pubs(), stage="process")
        assert len(find.calls) == 2

    def test_no_roll_is_stored_as_nothing_found_and_not_read_again(self, store, media, find):
        find.answer = None
        out, _ = _run(ctx_for(store, media), media, pubs(), stage="process")
        rec = store.get_file(media)
        assert [(r.source, r.type) for r in store.evidence_rows(rec.id) if r.source is Source.CREDITS_TEXT] == [(Source.CREDITS_TEXT, None)]
        assert _run(ctx_for(store, media), media, pubs(), stage="check")[0] is not None
        assert len(find.calls) == 1

    @pytest.mark.parametrize(
        "error",
        [frames.FrameDecodeError("timed out"), frames.DecodeCancelledError("cancelled"),
         detector.TextDetUnavailableError("helper gone")],
    )  # fmt: skip
    def test_no_answer_this_time_stores_nothing(self, store, media, find, error):
        find.answer = error
        _run(ctx_for(store, media), media, pubs(), stage="process")
        rec = store.get_file(media)
        assert store.evidence_version(rec.id, Source.CREDITS_TEXT) is None
        assert _run(ctx_for(store, media), media, pubs(), stage="check")[0] is None  # asked again

    def test_a_timed_out_file_is_not_decoded_again_for_a_day_unless_forced(self, store, media, find):
        find.answer = frames.DecodeTimeoutError("decoding S01E01.mkv timed out after 600 s")
        _run(ctx_for(store, media), media, pubs(), stage="process")
        _run(ctx_for(store, media), media, pubs(), stage="process")
        assert len(find.calls) == 1
        rec = store.get_file(media)
        assert store.evidence_version(rec.id, Source.CREDITS_TEXT) is None
        _run(ctx_for(store, media, force=True), media, pubs(), stage="process")
        assert len(find.calls) == 2

    def test_a_gpu_decode_failure_reaches_the_workers_cpu_rerun(self, store, media, find):
        find.answer = frames.GpuDecodeError("the GPU decoded no frames")
        with pytest.raises(CodecNotSupportedError):
            _run(ctx_for(store, media), media, pubs(), stage="process", gpu="NVIDIA", gpu_device_path="cuda:0")
        find.answer = START_S
        out, _ = _run(ctx_for(store, media), media, pubs(), stage="process", gpu=None, gpu_device_path=None)
        assert find.calls[-1]["gpu"] is None and out.outcome_key == FileOutcome.NEEDS_REVIEW.value


class TestAvailability:
    def _stored(self, store, media, find):
        _run(ctx_for(store, media, "medium"), media, pubs(), stage="process")
        return store.get_file(media)

    @pytest.mark.parametrize(
        ("state", "expected"),
        [(TextDetState.AVAILABLE, DecisionStatus.DECIDED), (TextDetState.UNKNOWN, DecisionStatus.DECIDED),
         (TextDetState.ABSENT, DecisionStatus.NO_EVIDENCE)],
    )  # fmt: skip
    def test_stored_answers_decide_only_while_detection_can_run_or_its_check_did_not_answer(self, store, media, find, state, expected):
        rec = self._stored(store, media, find)
        ctx = _ctx(store, _registry(media, ServerType.PLEX), settings_raw=settings("medium"),
                   detectors=(detector.credits_text_spec(),) if state is TextDetState.AVAILABLE else ())  # fmt: skip
        ctx.credits_text = state
        assert pipeline._decide(ctx, rec, frozenset({T.CREDITS}))[T.CREDITS].status is expected

    @pytest.mark.parametrize("state", list(TextDetState))
    @pytest.mark.parametrize(("source_on", "credits_on"), [(True, True), (False, True), (True, False)])
    def test_the_detector_is_registered_only_when_it_can_run(self, loguru_caplog, state, source_on, credits_on):
        raw = {"detect": {"intro": False, "credits": credits_on}, "sources": [{"id": "credits_text", "enabled": source_on}, {"id": "season_audio", "enabled": False}]}
        found = pipeline.default_local_detectors(load_global(validate_global(raw, None)[0]), MagicMock(), credits_text=state)
        registered = source_on and credits_on and state is TextDetState.AVAILABLE
        assert [s.source for s in found] == ([Source.CREDITS_TEXT] if registered else [])
        warned = source_on and credits_on and state is not TextDetState.AVAILABLE
        assert ("On-screen credit text is on" in loguru_caplog.text) is warned

    @pytest.mark.parametrize(("source_on", "credits_on", "asked"), [(True, True, True), (False, True, False), (True, False, False)])
    def test_build_context_checks_text_detection_only_when_it_could_run(self, store, source_on, credits_on, asked):
        raw = {"detect": {"intro": False, "credits": credits_on}, "sources": [{"id": "credits_text", "enabled": source_on}, {"id": "season_audio", "enabled": False}]}
        with (
            patch.object(pipeline, "get_global_settings", return_value=load_global(validate_global(raw, None)[0])),
            patch.object(pipeline, "get_marker_store", return_value=store),
            patch.object(pipeline, "build_clients", return_value={}),
            patch.object(pipeline, "text_detection_state", return_value=TextDetState.AVAILABLE) as checked,
        ):
            ctx = pipeline.build_context(registry=MagicMock(), config=MagicMock(ffmpeg_path=None), priority=3)
        assert checked.called is asked
        assert ctx.credits_text is (TextDetState.AVAILABLE if asked else TextDetState.ABSENT)
        assert [s.source for s in ctx.local_detectors] == ([Source.CREDITS_TEXT] if asked else [])


@pytest.mark.timeout(150)  # the subprocess may take up to 120 s on a slow runner; addopts has --timeout=30
def test_the_web_app_and_the_pipeline_never_load_onnxruntime_or_opencv(tmp_path):
    code = (
        "import sys, os\n"
        f"os.environ['CONFIG_DIR'] = {str(tmp_path)!r}\n"
        "import media_preview_generator.markers.pipeline\n"
        "from media_preview_generator.web.app import create_app\n"
        f"create_app(config_dir={str(tmp_path)!r})\n"
        "print(sorted(m for m in ('onnxruntime', 'onnxruntime_ep_webgpu', 'cv2', 'pyclipper') if m in sys.modules))\n"
        "sys.stdout.flush()\n"
        "os._exit(0)\n"  # the app's background threads would keep the interpreter alive
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert out.stdout.strip().splitlines()[-1] == "[]", out.stderr[-2000:]
```

Run: `/home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/markers/test_pipeline_credits_text.py -q`
Expected: FAIL — `AttributeError: … has no attribute 'text_detection_state'` (and `default_local_detectors() got an
unexpected keyword argument 'credits_text'`).

- [ ] **Step 4: Wire the detector into the pipeline**

In `media_preview_generator/markers/pipeline.py`:

Imports (with the other local imports):

```python
from .credits.detector import credits_text_spec
from .credits.textdet_helper import TextDetState, text_detection_state
```

`PipelineContext`: add the field after `chromaprint` and a docstring entry after the `chromaprint` entry:

```python
        credits_text: What the job's check for credit text detection found. ABSENT keeps stored credits text answers
            from helping decide (they may still hold a type in review); UNKNOWN (the check didn't answer) registers no
            detector, but stored answers count as if it were there.
```
```python
    credits_text: TextDetState = TextDetState.AVAILABLE
```

`default_local_detectors` — new keyword and block after the season audio block; extend its docstring ("… and credit
text when its source and credits detection are on and text detection can run here"; `credits_text:` arg line):

```python
def default_local_detectors(
    settings: GlobalMarkersSettings,
    config: Any,
    chromaprint: ChromaprintState = ChromaprintState.ABSENT,
    credits_text: TextDetState = TextDetState.ABSENT,
) -> tuple[LocalDetectorSpec, ...]:
    ...  # the season audio block stays as it is
    if settings.detect_credits and settings.source_enabled(Source.CREDITS_TEXT.value):
        if credits_text is TextDetState.AVAILABLE:
            detectors.append(credits_text_spec())
        elif credits_text is TextDetState.UNKNOWN:
            logger.warning(
                "On-screen credit text is on, but the text detection check didn't answer; this job reads no credit "
                "text, saved credit text answers still count, and the check runs again in 10 minutes"
            )
        else:
            logger.warning(
                "On-screen credit text is on, but text detection isn't available here (Settings → Intro & Credits says "
                "why); credits come from the other sources only"
            )
    return tuple(detectors)
```

`build_context`, after the `chromaprint = (...)` assignment:

```python
    credits_text = (
        text_detection_state()
        if settings.detect_credits and settings.source_enabled(Source.CREDITS_TEXT.value)
        else TextDetState.ABSENT
    )
```

and in the `PipelineContext(...)` call: `local_detectors=default_local_detectors(settings, config, chromaprint,
credits_text),` plus `credits_text=credits_text,`.

`_decide`, right after the chromaprint UNKNOWN line:

```python
    if ctx.credits_text is TextDetState.UNKNOWN:
        unavailable -= {Source.CREDITS_TEXT.value}
```

and add "(or credit text without a text detector)" to its docstring sentence about stored answers of a detector the
job doesn't have.

`tests/markers/conftest.py` — append:

```python
@pytest.fixture(autouse=True)
def _no_text_detection_check(monkeypatch, request):
    """Jobs these tests build don't start the text detection check (a subprocess); tests of the check itself live in
    tests/markers/credits and patch what they need."""
    if request.node.get_closest_marker("integration"):
        return
    from media_preview_generator.markers import pipeline

    monkeypatch.setattr(pipeline, "text_detection_state", lambda: __import__(
        "media_preview_generator.markers.credits.textdet_helper", fromlist=["TextDetState"]).TextDetState.ABSENT)
```

(`test_build_context_checks_text_detection_only_when_it_could_run` patches `pipeline.text_detection_state` again inside
its own `with`, which wins over the fixture.)

- [ ] **Step 5: Run the task's tests and the markers suite**

Run: `/home/data/.venv/bin/python -m pytest --no-cov tests/markers -q`
Expected: PASS, including `test_pipeline.py`, `test_pipeline_detectors.py` and `audio/test_season.py` unchanged
(`default_local_detectors(settings, cfg)` without the new argument registers no credits text detector, so their
`== ()` and `(spec,) = …` assertions hold). Mutation checks (each must fail the named test; revert after):
- drop the `_decide` UNKNOWN line → the UNKNOWN cell of `test_stored_answers_decide_only_while_detection_can_run…`;
- register the detector for any state → `test_the_detector_is_registered_only_when_it_can_run`;
- `is_episode=not rec.is_movie` → `test_only_a_file_in_a_season_is_read_as_an_episode[rec2-False]` (the "Pilot" file of
  unknown kind: the only cell where the two differ; checked while planning);
- drop `if _timed_out_lately(rec, ctx):` → `test_a_timed_out_decode_waits_a_day_unless_forced_or_the_file_changes` and
  `test_a_timed_out_file_is_not_decoded_again_for_a_day_unless_forced`;
- `if not rule_j.keeps_a_scene_after(...)` → `if False:` → `test_a_roll_to_the_end_of_the_file_is_refined_before_it_and_left_open_ended`
  (a third decode it doesn't expect);
- `end_ms = None` always → `test_a_scene_after_the_roll_gives_the_candidate_its_end` and `test_a_scene_after_the_credits_is_kept_q3`.

While planning, Steps 1–3's files passed against the branch with this task's code in a scratch copy (detector 17,
store 3, pipeline 31; the whole markers suite 3,747; both integration clips on the CPU helper).

- [ ] **Step 6: Real-media smoke on the storage host (integration, local only)**

```python
# appended to tests/markers/credits/test_detector.py
@pytest.mark.integration
@pytest.mark.timeout(600)
@pytest.mark.parametrize(("scene_s", "expected_end"), [(0, None), (60, 540.0)])
def test_real_decode_and_detection_on_a_generated_roll(tmp_path, monkeypatch, scene_s, expected_end):
    import os
    import shutil
    import subprocess as sp

    from media_preview_generator.markers.credits import textdet_helper

    model = os.environ.get(textdet_helper.MODEL_ENV, textdet_helper.DEFAULT_MODEL_PATH)
    ffmpeg = shutil.which("ffmpeg")
    if not (ffmpeg and os.path.isfile(model)):
        pytest.skip("needs ffmpeg and the text detection model")
    monkeypatch.setenv(textdet_helper.MODEL_ENV, model)
    font = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    clip = tmp_path / "movie.mkv"
    # 420 s of text-free gradients, then 120 s of names scrolling up over black (a new line every 3 s, 20 px/s), then
    # optionally a 60 s scene after the credits (Q3).
    roll = ",".join(
        f"drawtext=fontfile={font}:text='NAME {i}':fontcolor=white:fontsize=24:x=(w-tw)/2:y=h-20*t+{i * 60}" for i in range(40)
    )
    inputs = ["-f", "lavfi", "-i", "gradients=size=640x360:rate=24:speed=0.02,trim=duration=420,setpts=PTS-STARTPTS",
              "-f", "lavfi", "-i", f"color=c=black:size=640x360:rate=24:duration=120,{roll}"]  # fmt: skip
    if scene_s:
        inputs += ["-f", "lavfi", "-i", f"gradients=size=640x360:rate=24:speed=0.05:seed=9,trim=duration={scene_s},setpts=PTS-STARTPTS"]
    streams = "".join(f"[{i}:v]" for i in range(len(inputs) // 4))
    sp.run([ffmpeg, "-v", "error", *inputs, "-filter_complex", f"{streams}concat=n={len(inputs) // 4}:v=1:a=0[v]", "-map", "[v]",
            "-c:v", "libx264", "-preset", "veryfast", "-g", "48", "-pix_fmt", "yuv420p", str(clip)], check=True)  # fmt: skip
    pool = textdet_helper.TextDetectorPool()
    try:
        result = detector.find_credits(str(clip), duration_ms=(540 + scene_s) * 1000, is_episode=False, ffmpeg=ffmpeg,
                                       count_boxes=lambda p: pool.count_boxes(p, gpu=None, gpu_device_path=None),
                                       gpu=None, gpu_device_path=None)  # fmt: skip
    finally:
        pool.close_all()
    assert result.start_s is not None and abs(result.start_s - 420.0) <= 10.0
    if expected_end is None:
        assert result.end_s is None and result.end_rows == ()
    else:
        assert result.end_s is not None and abs(result.end_s - expected_end) <= 3.0
```

Run: `MEDIA_PREVIEW_TEXTDET_MODEL="$MARKERS_BENCH_DIR/textdet-model/ch_PP-OCRv4_det_infer.onnx" nice -n 19 /home/data/.venv/bin/python -m pytest --no-cov -n 0 -m integration tests/markers/credits/test_detector.py -q`
Expected: PASS. While planning, these clips gave start 421.0 s on the CPU helper both times (the first line is readable
a second after the cut to black; the gradients gave 0 boxes at luma ≥ 72), end None without the scene, and end 539.0 s
with it (last credit keyframe 537.7 s, refined over the 1 fps window). If a value is off by more than its tolerance,
report the key, fine and end rows (`result.key_rows[-40:]`, `result.end_rows`) instead of changing the tolerance.

- [ ] **Step 7: Commit**

Stage the detector, `store.py`, the three test files, `pipeline.py` and `tests/markers/conftest.py`; Architecture
Review on the staged diff; then `PATH="/home/data/.venv/bin:$PATH" git commit -m "feat(markers): credit text detector in the Intro & Credits pipeline"`.

---
## Task 10: API + Settings row live + Inspector lane

`[lane-parallel]` (after Task 8; beside Task 11, different files) — spec §7.2 ("ordered sources (… season audio, credit
text, …) with measured numbers in ⓘ copy"), §7.3 (Inspector evidence lanes), §0 and Global Constraints "UI checkpoint",
"UI copy" section of this plan, phase-2 pattern: `GET /api/markers/sources/local` and "Not available" with the reason
(phase 2 Task 13), Q1, Q3, preflight M7 (numbers from the product run).

**OWNER DECISION (2026-09-16), Q1 and Q3:** the tooltip says credits text can publish alone at Medium and that the skip
stops at the last credit when a scene follows; the Inspector lane shows a credits text answer's end when it has one.

**Numbers in the copy (M7):** the tooltip's "within 10 s on 59, more than 30 s early on 1, missed 4" are the fixture's
numbers (truth-centred refine rows). If Task 11 has landed when this task starts, use its `rule_j_80` from the app's own
GPU run instead (and the same numbers in the e2e test); otherwise ship these and Task 12 corrects both from Task 11.

**Files:**
- Modify: `media_preview_generator/web/routes/api_markers.py` (`marker_local_sources` ~403–415),
  `media_preview_generator/web/templates/settings.html` (section ⓘ ~474; the sources comment ~531–533; the
  `credits_text` row ~618–633; `loadMarkersLocalSources` ~1300–1316),
  `media_preview_generator/web/static/css/pages/settings.css` (the `.markers-source-soon` rules ~127–131),
  `tests/markers/test_api_markers.py` (`test_local_sources_status` ~703), `tests/e2e/test_intro_credits_settings.py`
  (`AVAILABLE`; the two coming-soon tests ~198–226; new credits text tests), `tests/e2e/test_intro_credits_inspector.py`
  (a `credits_text` payload and test)
- Test: those three test files

**Interfaces:**
- Consumes: Task 5 `textdet_helper.text_detection_status() -> tuple[bool, str]`.
- Produces: `GET /api/markers/sources/local` → `{"season_audio": {"available": bool, "ffmpeg": str | null, "message":
  str}, "credits_text": {"available": bool, "message": str}}` (Tasks 12 and 13 document and check it).

- [ ] **Step 1: Failing API test**

In `tests/markers/test_api_markers.py` replace `test_local_sources_status` with:

```python
@pytest.mark.parametrize(("found", "reason"), [("/usr/lib/jellyfin-ffmpeg/ffmpeg", ""), (None, "no chromaprint")])
@pytest.mark.parametrize(
    ("text_available", "text_reason"),
    [(True, ""), (False, "Needs the text detection model, which the Docker image includes; it isn't at /app/models/ch_PP-OCRv4_det_infer.onnx")],
)
def test_local_sources_status(client, servers, monkeypatch, found, reason, text_available, text_reason):
    from media_preview_generator.markers.audio import fingerprint
    from media_preview_generator.markers.credits import textdet_helper

    asked = []
    monkeypatch.setattr(fingerprint, "chromaprint_status", lambda configured: asked.append(configured) or (found, reason))
    monkeypatch.setattr(textdet_helper, "text_detection_status", lambda: (text_available, text_reason))
    resp = client.get("/api/markers/sources/local", headers=_api_headers())
    assert resp.get_json() == {
        "season_audio": {"available": found is not None, "ffmpeg": found, "message": reason},
        "credits_text": {"available": text_available, "message": text_reason},
    }
    # The ffmpeg jobs use: jellyfin-ffmpeg, then PATH (config._resolve_ffmpeg_path has no setting to pass).
    assert asked == [None]
```

Run: `/home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/markers/test_api_markers.py -q -k local_sources`
Expected: FAIL — the response has no `credits_text` key.

- [ ] **Step 2: Implement the API**

```python
@api.route("/markers/sources/local", methods=["GET"])
@api_token_required
def marker_local_sources():
    """Whether the local detectors can run in this container: season audio needs an ffmpeg with chromaprint, credit text
    ONNX Runtime, OpenCV and the text detection model.

    Returns:
        200 with ``{"season_audio": {"available", "ffmpeg", "message"}, "credits_text": {"available", "message"}}``;
        ``message`` says why when a source isn't available.
    """
    from ...markers.audio import fingerprint
    from ...markers.credits import textdet_helper

    # No setting to pass: jobs use jellyfin-ffmpeg, then ffmpeg on PATH, the order chromaprint_status tries with None.
    found, reason = fingerprint.chromaprint_status(None)
    text_available, text_reason = textdet_helper.text_detection_status()
    return jsonify(
        {
            "season_audio": {"available": found is not None, "ffmpeg": found, "message": reason},
            "credits_text": {"available": text_available, "message": text_reason},
        }
    )
```

Run: `/home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/markers/test_api_markers.py -q` — Expected: PASS.

- [ ] **Step 3: Failing e2e tests**

In `tests/e2e/test_intro_credits_settings.py`:

```python
AVAILABLE = {
    "season_audio": {"available": True, "ffmpeg": "/usr/lib/jellyfin-ffmpeg/ffmpeg", "message": ""},
    "credits_text": {"available": True, "message": ""},
}
```

Delete `test_coming_soon_sources_are_disabled_with_badge`. Rename `test_coming_soon_sources_round_trip_their_stored_values`
to `test_local_sources_round_trip_their_stored_values` (body unchanged except the comment: "Stored order puts the two
local sources first."). Add to `TestIntroCreditsSettings`:

```python
    def test_no_source_is_coming_soon(self, authed_page: Page, app_url: str) -> None:
        _open_settings_and_wait_for_local(authed_page, app_url, _default_markers())
        expect(authed_page.locator("#markersSourceList .markers-source-soon-badge")).to_have_count(0)
        expect(authed_page.locator("#markersSourceList .markers-source-soon")).to_have_count(0)
        for source_id in SOURCE_ORDER:
            switch = authed_page.locator(f"#markersSourceList .markers-source[data-id='{source_id}'] .markers-source-enabled")
            expect(switch).to_be_enabled()

    def test_credit_text_is_switchable_and_explains_its_numbers(self, authed_page: Page, app_url: str) -> None:
        captured = _open_settings_and_wait_for_local(authed_page, app_url, _default_markers())
        row = authed_page.locator("#markersSourceList .markers-source[data-id='credits_text']")
        expect(row.locator(".markers-source-unavailable")).to_be_hidden()
        expect(row.locator(".markers-source-reason")).to_be_hidden()
        expect(row).to_contain_text("Reads the end of the file · GPU when that's faster, otherwise CPU · about 10–30 s per file")
        tooltip = row.locator(".info-icon").evaluate("el => el.getAttribute('data-bs-original-title') || el.getAttribute('title')")
        assert tooltip == (
            "Finds where the credit roll starts from text on screen near the end of the file, and stops the skip at the "
            "last credit when a scene follows. Tested alone on 80 files: within 10 s on 59, more than 30 s early on 1, "
            'missed 4. At "High" another source has to agree; at "Medium" it can publish alone."
        )
        row.locator(".markers-source-enabled").click()
        sent = _wait_for_post(authed_page, captured, lambda m: next(s for s in m["sources"] if s["id"] == "credits_text")["enabled"] is False)
        assert [s["id"] for s in sent["sources"]] == SOURCE_ORDER

    def test_credit_text_unavailable_says_why_and_keeps_the_stored_choice(self, authed_page: Page, app_url: str) -> None:
        reason = "Needs the text detection model, which the Docker image includes; it isn't at /app/models/ch_PP-OCRv4_det_infer.onnx"
        local = {**AVAILABLE, "credits_text": {"available": False, "message": reason}}
        captured = _open_settings_and_wait_for_local(authed_page, app_url, _default_markers(), local=local)
        row = authed_page.locator("#markersSourceList .markers-source[data-id='credits_text']")
        expect(row.locator(".markers-source-unavailable")).to_have_text("Not available", timeout=5000)
        expect(row.locator(".markers-source-reason")).to_have_text(reason)
        expect(row.locator(".markers-source-enabled")).to_be_checked()
        season = authed_page.locator("#markersSourceList .markers-source[data-id='season_audio']")
        expect(season.locator(".markers-source-unavailable")).to_be_hidden()
        authed_page.locator("label[for='markersDetectRecap']").click()
        sent = _wait_for_post(authed_page, captured, lambda m: m["detect"]["recap"] is True)
        assert next(s for s in sent["sources"] if s["id"] == "credits_text")["enabled"] is True

    def test_an_answer_without_credit_text_leaves_its_row_as_is(self, authed_page: Page, app_url: str) -> None:
        # An app one version behind answers season audio only.
        _open_settings_and_wait_for_local(authed_page, app_url, _default_markers(), local={"season_audio": AVAILABLE["season_audio"]})
        row = authed_page.locator("#markersSourceList .markers-source[data-id='credits_text']")
        expect(row.locator(".markers-source-enabled")).to_be_enabled()
        expect(row.locator(".markers-source-unavailable")).to_be_hidden()
```

In `tests/e2e/test_intro_credits_inspector.py` add a payload next to `movie()` and a test in `TestIntroCreditsTab`:

```python
def movie_with_credit_text() -> dict:
    payload = movie()
    payload["decisions"]["credits"] = _decision("decided", ("credits", 6_912_000, 7_200_000), decided_by=["credits_text", "server_markers"])
    payload["evidence"] = [_evidence("chapters", None, None, None, "No chapters"), _evidence("credits_text", "credits", 6_912_000, None)]
    payload["servers"] = [_server("plex-1", "Plex", "plex", "up_to_date", [_marker("credits", 6_915_000, 7_200_000)])]
    return payload
```

```python
    def test_credit_text_has_its_own_lane_in_the_ending(self, authed_page: Page, app_url: str) -> None:
        inspector = _Inspector(authed_page, app_url, movie_with_credit_text())
        inspector.open_result()
        page = inspector.open_tab()
        expect(_lane(page, "ending", "Credit text")).to_contain_text("1:55:12 →")
        expect(_lane(page, "ending", "Credit text").locator(".mk-disagree")).to_have_count(0)

    def test_credit_text_with_a_scene_after_the_roll_shows_where_the_skip_ends(self, authed_page: Page, app_url: str) -> None:
        # Q3: an end 20 s before the end of a 2 h file ("1:59:40"); laneRange() writes "start–end" unless the end is
        # within 2 s of the end of the file.
        payload = movie_with_credit_text()
        payload["evidence"][-1] = _evidence("credits_text", "credits", 6_912_000, 7_180_000)
        payload["decisions"]["credits"] = _decision("decided", ("credits", 6_912_000, 7_180_000), decided_by=["credits_text"])
        inspector = _Inspector(authed_page, app_url, payload)
        inspector.open_result()
        page = inspector.open_tab()
        expect(_lane(page, "ending", "Credit text")).to_contain_text("1:55:12–1:59:40")
        expect(_lane(page, "ending", "Decision")).to_contain_text("1:55:12–1:59:40")

    def test_credit_text_that_found_nothing_says_so(self, authed_page: Page, app_url: str) -> None:
        payload = movie()
        payload["evidence"].append(_evidence("credits_text", None, None, None))
        inspector = _Inspector(authed_page, app_url, payload)
        inspector.open_result()
        page = inspector.open_tab()
        expect(_lane(page, "ending", "Credit text")).to_contain_text("Nothing found")
```

Run: `/home/data/.venv/bin/python -m pytest -m e2e -n 8 --no-cov tests/e2e/test_intro_credits_settings.py tests/e2e/test_intro_credits_inspector.py -q`
Expected: the new Settings tests FAIL (badge still shown, switch disabled); the three Inspector tests PASS already
(the lane is generic, and `laneRange` in `markers_inspector.js` already writes "start–end" for an end more than 2 s
before the end of the file) — keep them as regression tests. If the end cell fails, the lane isn't generic for ends:
report the lane's text before changing the script.

- [ ] **Step 4: Settings markup, script and CSS**

`settings.html`, the section ⓘ (~474): `title="Found from chapters, online databases, the theme tune a season's episodes
share, and the on-screen credit roll."`

The sources comment (~531): drop the sentence about "Coming soon" sources, keeping "Order is the order sources are
checked and which one's times win. Reordered by the grip (drag) or the ▲/▼ buttons (keyboard and touch)."

The row (replaces the whole `<li class="markers-source markers-source-soon" data-id="credits_text">…</li>`):

```html
                            <li class="markers-source" data-id="credits_text">
                                <span class="markers-source-grip" draggable="true" title="Drag to reorder" aria-hidden="true">⋮⋮</span>
                                <div class="markers-source-body">
                                    <span class="markers-source-name">On-screen credit text</span>
                                    <button type="button" class="info-icon ms-1" tabindex="0" data-bs-toggle="tooltip" data-bs-placement="top" title="Finds where the credit roll starts from text on screen near the end of the file, and stops the skip at the last credit when a scene follows. Tested alone on 80 files: within 10 s on 59, more than 30 s early on 1, missed 4. At &quot;High&quot; another source has to agree; at &quot;Medium&quot; it can publish alone."><i class="bi bi-info-circle"></i></button>
                                    <span class="badge bg-warning-subtle text-warning-emphasis border border-warning-subtle ms-1 markers-source-unavailable" hidden>Not available</span>
                                    <div class="small text-muted">Reads the end of the file · GPU when that's faster, otherwise CPU · about 10–30 s per file</div>
                                    <div class="small text-warning-emphasis markers-source-reason" hidden></div>
                                </div>
                                <div class="markers-source-controls">
                                    <div class="form-check form-switch markers-switch">
                                        <input class="form-check-input markers-source-enabled" type="checkbox" role="switch" aria-label="Use on-screen credit text" checked>
                                    </div>
                                    <button type="button" class="btn btn-sm btn-link markers-source-up" aria-label="Move on-screen credit text up"><i class="bi bi-caret-up-fill"></i></button>
                                    <button type="button" class="btn btn-sm btn-link markers-source-down" aria-label="Move on-screen credit text down"><i class="bi bi-caret-down-fill"></i></button>
                                </div>
                            </li>
```

(If Task 1 M4 measured a per-file range outside 10–30 s, use the measured range in the subtitle and in the test.)

`loadMarkersLocalSources` (replaces the function):

```javascript
async function loadMarkersLocalSources() {
    const rows = ['season_audio', 'credits_text']
        .map((id) => [id, document.querySelector(`#markersSourceList .markers-source[data-id="${id}"]`)])
        .filter(([, row]) => row);
    if (!rows.length) return;
    try {
        const response = await fetch('/api/markers/sources/local');
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        rows.forEach(([id, row]) => {
            const status = data && data[id];
            if (!status || typeof status.available !== 'boolean') return;
            // The switch keeps the stored choice: a job in another container (or after an image update) may run it.
            row.querySelector('.markers-source-unavailable').hidden = status.available;
            const reason = row.querySelector('.markers-source-reason');
            reason.textContent = status.available ? '' : String(status.message || '');
            reason.hidden = status.available;
        });
    } catch (_error) {
        // Unknown: leave the rows as they are.
    }
}
```

`settings.css`: delete the `/* Sources not built yet … */` comment and the `.markers-source-soon …` rule (no row uses the
class any more).

- [ ] **Step 5: Run the suites**

Run: `/home/data/.venv/bin/python -m pytest -m e2e -n 8 --no-cov tests/e2e/test_intro_credits_settings.py tests/e2e/test_intro_credits_inspector.py -q`
then `/home/data/.venv/bin/python -m pytest --no-cov tests/markers/test_api_markers.py tests/test_routes.py -q`
Expected: PASS.

- [ ] **Step 6: Owner checkpoint — screenshots before the commit**

Take four screenshots with Playwright against the e2e app (the tests' mocks): the Settings sources list with credits
text available, the same row "Not available" with a reason, and the Inspector ending window with the "Credit text"
lane open-ended and with an end.
Save them under `.superpowers/sdd/plan-phase3/ui/` (git-excluded) and send them to the owner together with the
worker-row step strings from Task 8 ("Reading the credits…", "Refining the credits start…", "Finding where the credits
end…"). Commit only after the owner confirms the look
and wording (or apply the requested wording to the markup and the tests; a change to the step strings adds
`media_preview_generator/markers/credits/detector.py`'s `READING_PHASE` / `REFINING_PHASE` / `REFINING_END_PHASE` to
this task's files, after Task 8 has landed).

- [ ] **Step 7: Commit**

Stage the six files; Architecture Review on the staged diff; then
`PATH="/home/data/.venv/bin:$PATH" git commit -m "feat(markers): on-screen credit text source live in Settings"`.

---
## Task 11: Harness — credits text vs Plex

`[lane-parallel]` (after Task 8; beside Task 10) — spec §10.2 (credits: the 80 files and the 205-movie set; required
before merging a detector change; results pasted in the PR), §12 phase 3 done-when ("harness ≥ §5.4 numbers with 0–1
early per 80"), roadmap phase 3 done-when ("combined credits decisions beat Plex's native credits markers on the same
files"), Q2, Q3, Q4, Q5, T-R2 (bench results recorded), C7, preflight I5 (gate), I6 (mirror the pipeline), I7
(frame-check epilogue cards and 10–30 s early answers), the harness README's data rules.

**OWNER DECISION (2026-09-16), Q4:** precision first, never looser than Plex — per set (the 80 = movies40 + tv40
together; the 205 movies), Medium wrong ≤ 2 % of the set's files rounded up (80 → 2, 205 → 5) and ≤ Plex's wrong; High
wrong ≤ 1 % rounded up (80 → 1, 205 → 3) and ≤ Plex's wrong (controller ruling, 2026-09-16: round up); Medium useful ≥
Plex useful; rule J alone ≥ 59 within 10 s and ≤ 1 early of 80.
**OWNER DECISION (2026-09-16), Q5:** rule J ships as measured; answers worth a look are frame-checked and adjudicated.
**OWNER DECISION (2026-09-16), Q3:** the rows count the answers with an end and the decisions that keep a scene.

**What it measures** (every row through the app's own `find_credits` and `decide()`):
- **Rule J alone** on the 80 files, spec metric: within 5/10/30 s, early/late > 30 s, none; per HDR kind (Task 1 M5).
  Gate: within 10 s ≥ 59 and early ≤ 1.
- **Credits text alone** on `movies40`, `tv40`, `movie_credit_truth` (the detector's answer, no rules), and **what the
  pipeline publishes** with it at High and at Medium, beside **Plex's first credits marker** (verdicts as
  `credits.judge_credits`: wrong = > 10 s early, late = > 30 s late). Chapters are left out: they are the truth, and
  these files stand for files without usable chapters. The rows mirror the pipeline (C7): a local detector runs only
  for credits the other sources leave undecided, and Plex's markers never decide alone (rule 7), so on these sets every
  file asks credits text; no chapter-veto row exists because a normal run never reads credit text for a
  chapter-decided file. Also reported: High decisions resting only on credits text + a server's marker (Q2's pair),
  answers with an end and decisions that keep a scene (Q3).
- **Gate per set** (`gate_checks`, Q4) on the 80 (movies40 + tv40 merged) and the 205, check by check.
- **Online cases** (`--online`): the 43 verified cases at the app's three source settings, credit text added the way
  the pipeline adds it: only to cases whose credits the online answers and Plex's markers leave undecided
  (`undecided_credits`), with that count reported.
- **Frame-check sheets** (`--sheets DIR`, local-only; the summary always lists which files and why): a 4×2 contact sheet
  around every credits text answer that is more than 10 s early (10–30 s early included), more than 30 s late, shaped
  like epilogue cards (`epilogue_like`), or has an end (a second sheet around the end), for Q5's adjudication.
- Both decode paths: `--decode gpu` (storage NVIDIA, text detection through the helper pool) is the reported run;
  `--decode cpu` on the 80 files proves the CPU worker path gives the same gate.

**Files:**
- Create: `tools/markers_eval/credits_text.py`, `tests/markers_eval/test_credits_text.py`,
  `docs/design/intro-credits/evidence/eval/phase3-harness.md`
- Modify: `tools/markers_eval/__main__.py` (new `credits-text` command), `tools/markers_eval/README.md` (sections
  "`credits-text`", "Text detection bench", "Rule J fixture")
- Test: `tests/markers_eval/test_credits_text.py`

**Interfaces:**
- Consumes: Task 8 `find_credits(...) -> CreditsTextResult` (`start_s`, `end_s`, `key_rows`, `fine_rows`, `end_rows`),
  `CREDITS_TEXT_VERSION`; Task 5 `TextDetectorPool`, `model_path()`; Task 4 `textdet.TextDetector`, `cpu_session`;
  Task 3 `rule_j.Row`, `coarse_start`, `is_credit`; phase 2 harness `credits.judge_credits`, `credits._truth`,
  `credits._name`, `plex.load_baseline`, `plex.first_marker`, `plex.server_candidates`, `cache.ProbeCache`,
  `decisions.ORDER`, `online.load_online`, `online.case_key`, `online.case_file`, `online.online_verdicts`,
  `online.tally`, `online.SETTINGS`, `data.evidence_dir`.
- Produces:
```python
# tools/markers_eval/credits_text.py
SPEC_WITHIN_10S = 59; SPEC_EARLY_MAX = 1; MEDIUM_WRONG_PERCENT = 2; HIGH_WRONG_PERCENT = 1
GATE_SETS = {"80": ("movies40", "tv40"), "205": ("movie_credit_truth",)}
@dataclass class RuleTally: files, within_5s, within_10s, within_30s, early, late, none: int
    def add(self, start_s: float | None, truth_s: float) -> None; def meets_spec(self) -> bool; def as_dict(self) -> dict
def text_candidates(start_s: float | None, end_s: float | None = None) -> list[Candidate]
def epilogue_like(key_rows: Sequence[Row], start_s: float | None) -> bool
@dataclass class TextRows: plex, text, high, medium: Counter; text_and_server_only: int; ends_found: int
    ends_published: Counter; files: list[dict]
def compare_text(files: list[dict], adjudicated: dict[str, dict], *, answers: Mapping[str, tuple[float | None, float | None]],
                 probe: Callable[[str], MediaProbe], baseline: Mapping[str, list[PlexMarker]], is_movie: bool) -> TextRows
def wrong_cap(files: int, percent: int) -> int
def gate_checks(rows: TextRows, files: int) -> dict[str, bool]
def beats_plex(rows: TextRows, files: int) -> bool
def merge_rows(parts: Iterable[TextRows]) -> TextRows
def sheet_reasons(detail: dict, key_rows: Sequence[Row]) -> list[str]
def undecided_credits(verdicts: Iterable[dict]) -> set[str]
def hdr_kind(path: str, *, ffprobe: str) -> str                   # "sdr" | "hdr10" | "dv5" | "dv_other"
def sheet_command(ffmpeg: str, path: str, around_s: float, out: str) -> list[str]
class CreditsTextCache:
    def __init__(self, root: Path, *, ffmpeg: str, decode: str, gpu_device: str | None,
                 count_boxes: Callable[[np.ndarray], list[int]], probe: Callable[[str], MediaProbe]) -> None
    def result(self, path: str, *, is_episode: bool) -> dict   # {"start_s", "end_s", "key", "fine", "end"}
def run_credits_text(*, decode: str, gpu_device: str, sets: tuple[str, ...], online: bool, cache_root: Path,
                     ffmpeg: str, ffprobe: str, baseline_path: Path, sheets_dir: Path | None) -> tuple[dict, dict, bool]
```

- [ ] **Step 1: Write the failing tests (synthetic data only)**

```python
# tests/markers_eval/test_credits_text.py
"""Credits text harness rows on synthetic files: the spec tally, the Plex comparison, the gate, the cache."""

from __future__ import annotations

from collections import Counter
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from media_preview_generator.markers.credits.detector import CREDITS_TEXT_VERSION, CreditsTextResult
from media_preview_generator.markers.probe import Chapter, MediaProbe
from tools.markers_eval import credits_text as ct
from tools.markers_eval.plex import PlexMarker

DUR = 6_000_000
FILES = [
    {"file": "/m/A (2001)/A.mkv", "credits_start": 5700.0},   # text right, Plex 40 s early: they disagree
    {"file": "/m/B (2002)/B.mkv", "credits_start": 5500.0},   # text 20 s early, Plex 15 s early: they agree
    {"file": "/m/C (2003)/C.mkv", "credits_start": 5600.0},   # no roll found, Plex right
    {"file": "/m/D (2004)/D.mkv", "credits_start": 5400.0},   # text and Plex agree 60 s late
    {"file": "/m/E (2005)/E.mkv", "credits_start": 5600.0},   # a scene after the roll: text ends at 5890 s (Q3)
]
ANSWERS = {"/m/A (2001)/A.mkv": (5702.0, None), "/m/B (2002)/B.mkv": (5480.0, None), "/m/C (2003)/C.mkv": (None, None),
           "/m/D (2004)/D.mkv": (5460.0, None), "/m/E (2005)/E.mkv": (5601.0, 5890.0)}  # fmt: skip
BASELINE = {
    "/m/A (2001)/A.mkv": [PlexMarker("credits", 5_660_000, DUR, True)],
    "/m/B (2002)/B.mkv": [PlexMarker("credits", 5_485_000, DUR, True)],
    "/m/C (2003)/C.mkv": [PlexMarker("credits", 5_601_000, DUR, True)],
    "/m/D (2004)/D.mkv": [PlexMarker("credits", 5_462_000, DUR, True)],
    "/m/E (2005)/E.mkv": [PlexMarker("credits", 5_603_000, DUR, True)],
}


def probe(path: str) -> MediaProbe:
    return MediaProbe(DUR, (Chapter(0, None, "Film"),))


def test_rule_tally_uses_the_spec_metric():
    tally = ct.RuleTally()
    for start, truth in ((100.0, 95.0), (60.0, 100.0), (150.0, 100.0), (None, 100.0), (104.0, 100.0)):
        tally.add(start, truth)
    assert tally.as_dict() == {"files": 5, "within_5s": 2, "within_10s": 2, "within_30s": 2, "early": 1, "late": 1, "none": 1}
    assert not tally.meets_spec()


def test_text_candidates():
    assert ct.text_candidates(None) == []
    (c,) = ct.text_candidates(5702.0004)
    assert (c.type.value, c.start_ms, c.end_ms, c.source.value) == ("credits", 5_702_000, None, "credits_text")
    (ended,) = ct.text_candidates(5702.0, 5890.0004)
    assert (ended.start_ms, ended.end_ms) == (5_702_000, 5_890_000)


def test_compare_text_rows_are_what_the_pipeline_publishes():
    rows = ct.compare_text(FILES, {}, answers=ANSWERS, probe=probe, baseline=BASELINE, is_movie=True)
    assert rows.plex == Counter(useful=2, wrong=2, late=1)
    assert rows.text == Counter(useful=2, wrong=1, missed=1, late=1)
    # A: 42 s apart → review; B: they agree while both early → wrong; C: Plex alone never decides → missed;
    # D: they agree 60 s late → late; E: they agree, and the text's end stops the skip before the scene.
    assert rows.high == rows.medium == Counter(useful=1, wrong=1, missed=2, late=1)
    assert rows.text_and_server_only == 3
    assert (rows.ends_found, rows.ends_published) == (1, Counter(high=1, medium=1))
    e = rows.files[-1]
    assert (e["high"], e["text_end"]) == ((5601.0, 5890.0), 5890.0)
    names = [f["name"] for f in rows.files]
    assert names == ["A (2001)", "B (2002)", "C (2003)", "D (2004)", "E (2005)"] and all("/" not in n for n in names)
    assert not any("chapters" in key for f in rows.files for key in f)  # chapters are the truth, never a row


@pytest.mark.parametrize(("files", "medium_cap", "high_cap"), [(80, 2, 1), (205, 5, 3), (100, 2, 1), (40, 1, 1)])
def test_wrong_caps_round_up(files, medium_cap, high_cap):
    assert (ct.wrong_cap(files, ct.MEDIUM_WRONG_PERCENT), ct.wrong_cap(files, ct.HIGH_WRONG_PERCENT)) == (medium_cap, high_cap)


def _rows(medium, high, plex):
    return ct.TextRows(plex=Counter(plex), text=Counter(), high=Counter(high), medium=Counter(medium))


@pytest.mark.parametrize(
    ("medium", "high", "plex", "files", "failing"),
    [
        ({"useful": 56, "wrong": 1}, {"useful": 39, "wrong": 0}, {"useful": 47, "wrong": 13}, 80, []),
        # Measured while planning on the 80: High's one wrong answer is inside the 1 % cap (rounded up: 1 of 80).
        ({"useful": 56, "wrong": 1}, {"useful": 39, "wrong": 1}, {"useful": 47, "wrong": 13}, 80, []),
        ({"useful": 56, "wrong": 1}, {"useful": 39, "wrong": 2}, {"useful": 47, "wrong": 13}, 80, ["High wrong <= 1 (1% of 80)"]),
        # Beats Plex on usefulness and wrong answers, but over the 2 % cap: precision first.
        ({"useful": 60, "wrong": 3}, {"useful": 40, "wrong": 0}, {"useful": 47, "wrong": 13}, 80, ["Medium wrong <= 2 (2% of 80)"]),
        # Inside the caps but looser than a Plex that is better on this set.
        ({"useful": 150, "wrong": 3}, {"useful": 100, "wrong": 1}, {"useful": 140, "wrong": 2}, 205, ["Medium wrong <= Plex wrong"]),
        ({"useful": 139, "wrong": 1}, {"useful": 100, "wrong": 2}, {"useful": 140, "wrong": 2}, 205, ["Medium useful >= Plex useful"]),
        ({"useful": 150, "wrong": 1}, {"useful": 100, "wrong": 4}, {"useful": 140, "wrong": 5}, 205, ["High wrong <= 3 (1% of 205)"]),
        ({"useful": 150, "wrong": 1}, {"useful": 100, "wrong": 2}, {"useful": 140, "wrong": 1}, 205, ["High wrong <= Plex wrong"]),
    ],
)  # fmt: skip
def test_gate_checks_name_every_failing_check(medium, high, plex, files, failing):
    checks = ct.gate_checks(_rows(medium, high, plex), files)
    assert [name for name, ok in checks.items() if not ok] == failing
    assert ct.beats_plex(_rows(medium, high, plex), files) is (failing == [])


def test_merge_rows_adds_the_80s_two_halves():
    movies = ct.compare_text(FILES[:2], {}, answers=ANSWERS, probe=probe, baseline=BASELINE, is_movie=True)
    tv = ct.compare_text(FILES[2:], {}, answers=ANSWERS, probe=probe, baseline=BASELINE, is_movie=True)
    merged = ct.merge_rows([movies, tv])
    whole = ct.compare_text(FILES, {}, answers=ANSWERS, probe=probe, baseline=BASELINE, is_movie=True)
    assert (merged.plex, merged.text, merged.high, merged.medium) == (whole.plex, whole.text, whole.high, whole.medium)
    assert (merged.text_and_server_only, merged.ends_found, merged.ends_published) == (3, 1, Counter(high=1, medium=1))
    assert len(merged.files) == 5


def _dark(t, boxes=0):
    return (float(t), boxes, 10.0)


STORY = [(float(t), 0, 120.0) for t in range(0, 90, 2)]


@pytest.mark.parametrize(
    ("rows", "start", "expected"),
    [
        ([*STORY, *[_dark(t, 3) for t in range(90, 160, 2)]], 90.0, False),                      # a plain roll
        ([*STORY, *[_dark(t, 1) for t in range(88, 100, 2)], *[_dark(t, 3) for t in range(100, 160, 2)]], 88.0, True),  # 12 s of cards
        ([*STORY, *[_dark(t, 1) for t in range(90, 100, 2)], *[_dark(t, 3) for t in range(100, 160, 2)]], 90.0, False),  # 10 s: not more
        ([*STORY, *[_dark(t, 2) for t in range(90, 100, 2)], *[_dark(t) for t in range(100, 130, 2)], *[_dark(t, 2) for t in range(130, 190, 2)]], 90.0, True),  # black gap
        ([*STORY, *[(float(t), 0, 120.0) for t in range(90, 100, 2)]], None, False),              # no answer
    ],
)  # fmt: skip
def test_epilogue_like(rows, start, expected):
    assert ct.epilogue_like(rows, start) is expected


@pytest.mark.parametrize(
    ("text", "end", "truth", "epilogue", "reasons"),
    [
        (5600.0, None, 5600.0, False, []),
        (5585.0, None, 5600.0, False, ["early 10-30 s"]),
        (5560.0, None, 5600.0, False, ["early >30 s"]),
        (5640.0, None, 5600.0, False, ["late >30 s"]),
        (5600.0, 5890.0, 5600.0, False, ["end kept"]),
        (5590.0, None, 5600.0, True, ["epilogue-like"]),
        (None, None, 5600.0, False, []),
    ],
)  # fmt: skip
def test_sheet_reasons(monkeypatch, text, end, truth, epilogue, reasons):
    monkeypatch.setattr(ct, "epilogue_like", lambda rows, start: epilogue)
    assert ct.sheet_reasons({"text": text, "text_end": end, "truth": truth}, []) == reasons


def test_the_pipeline_reads_credit_text_only_for_online_cases_left_undecided():
    verdicts = [
        {"key": "a", "type": "credits", "verdict": "useful"},
        {"key": "b", "type": "credits", "verdict": "missed"},
        {"key": "b", "type": "intro", "verdict": "useful"},
        {"key": "c", "type": "intro", "verdict": "missed"},
        {"key": "d", "type": "credits", "verdict": "wrong"},
    ]
    assert ct.undecided_credits(verdicts) == {"b"}


@pytest.mark.parametrize(
    ("stream", "kind"),
    [
        ({"color_transfer": "bt709"}, "sdr"),
        ({"color_transfer": "smpte2084"}, "hdr10"),
        ({"color_transfer": "arib-std-b67"}, "hdr10"),
        ({"color_transfer": "smpte2084", "side_data_list": [{"dv_profile": 8}]}, "dv_other"),
        ({"side_data_list": [{"dv_profile": 5}]}, "dv5"),
    ],
)
def test_hdr_kind(stream, kind):
    import json

    with patch.object(ct.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps({"streams": [stream]}), returncode=0)) as run:
        assert ct.hdr_kind("/m/A.mkv", ffprobe="/ff/ffprobe") == kind
    assert run.call_args.args[0][:2] == ["/ff/ffprobe", "-v"]


def test_sheet_command_tiles_the_80_s_around_an_answer():
    assert ct.sheet_command("/ff", "/m/A.mkv", 5702.0, "/tmp/s.jpg") == [
        "/ff", "-v", "error", "-ss", "5662.0", "-t", "80", "-i", "/m/A.mkv",
        "-vf", "fps=1/10,scale=320:-2,tile=4x2", "-frames:v", "1", "-y", "/tmp/s.jpg",
    ]  # fmt: skip


def test_cache_runs_the_app_once_per_identity_and_version(tmp_path, monkeypatch):
    media = tmp_path / "A.mkv"
    media.write_bytes(b"x")
    calls = []

    def find(path, **kwargs):
        calls.append(kwargs)
        return CreditsTextResult(5702.0, 5890.0, ((5700.0, 2, 12.0),), ((5701.0, 2, 12.0),), ((5890.0, 2, 12.0),))

    monkeypatch.setattr(ct, "find_credits", find)
    cache = ct.CreditsTextCache(tmp_path / "cache", ffmpeg="/ff", decode="gpu", gpu_device="cuda:0",
                                count_boxes=lambda p: [0] * len(p), probe=lambda p: MediaProbe(DUR, ()))  # fmt: skip
    first = cache.result(str(media), is_episode=False)
    second = cache.result(str(media), is_episode=False)
    assert first == second == {"start_s": 5702.0, "end_s": 5890.0, "key": [[5700.0, 2, 12.0]], "fine": [[5701.0, 2, 12.0]],
                               "end": [[5890.0, 2, 12.0]]}  # fmt: skip
    assert len(calls) == 1
    assert (calls[0]["duration_ms"], calls[0]["gpu"], calls[0]["gpu_device_path"], calls[0]["is_episode"]) == (DUR, "NVIDIA", "cuda:0", False)
    monkeypatch.setattr(ct, "CREDITS_TEXT_VERSION", CREDITS_TEXT_VERSION + 1)
    cache.result(str(media), is_episode=False)
    assert len(calls) == 2


def test_the_cache_refuses_data_folders():
    with pytest.raises(ValueError, match="/data"):
        ct.CreditsTextCache(__import__("pathlib").Path("/data/cache"), ffmpeg="/ff", decode="cpu", gpu_device=None,
                            count_boxes=lambda p: [], probe=lambda p: MediaProbe(DUR, ()))  # fmt: skip
```

Run: `/home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/markers_eval/test_credits_text.py -q`
Expected: FAIL — `ImportError: cannot import name 'credits_text'`.

The `compare_text`, gate, epilogue and sheet expectations were computed with this module against the branch's
`decide()` while planning (the whole `tests/markers_eval` folder: 85 passed). The gate cells pin the owner's Q4 caps
rounded up, including today's measured 80-file numbers passing with High's one wrong answer and a row that fails at
two.

- [ ] **Step 2: Implement `credits_text.py`**

```python
# tools/markers_eval/credits_text.py
"""On-screen credit text (spec §5.4) on the credits truth sets: rule J alone against the truth, and our decisions with
it against Plex's own credits markers (``python -m tools.markers_eval credits-text``).

The truth is each file's last credits chapter (3 movies fixed by frame checks, ``credits/adjudicated.json``), so the
rows leave chapters out: these files stand for files without usable chapters. The rows mirror the pipeline, which reads
credit text only for credits the other sources leave undecided (``pipeline._detector_pending``): Plex's markers never
decide alone (rule 7), so every compared file asks it; in the online cases a SkipDB or TheIntroDB decision doesn't.
Summaries hold counts and folder names only; details (``--json``) hold paths and stay local.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
import subprocess
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from media_preview_generator.markers.credits import rule_j
from media_preview_generator.markers.credits.detector import CREDITS_TEXT_VERSION, find_credits
from media_preview_generator.markers.decide import DecisionContext, DecisionStatus, decide
from media_preview_generator.markers.models import Candidate, MarkerType, Source
from media_preview_generator.markers.probe import MediaProbe

from .cache import ProbeCache
from .credits import _name, _truth, judge_credits
from .data import evidence_dir
from .decisions import ORDER
from .online import SETTINGS, case_file, case_key, load_online, online_verdicts, tally
from .plex import PlexMarker, first_marker, load_baseline, server_candidates

SPEC_WITHIN_10S = 59
SPEC_EARLY_MAX = 1
# Q4 gate (owner, 2026-09-16: precision first, never looser than Plex): wrong answers (a start more than 10 s early) at
# most this percentage of a set's files, rounded up (a single answer both sources place early must not fail a set of
# fewer than 100 by construction): 80 files → Medium 2, High 1; 205 files → Medium 5, High 3.
MEDIUM_WRONG_PERCENT = 2
HIGH_WRONG_PERCENT = 1
CREDITS = frozenset({MarkerType.CREDITS})
# The gate's sets: the 80 files (movies40 + tv40 together) and the 205 movies.
GATE_SETS = {"80": ("movies40", "tv40"), "205": ("movie_credit_truth",)}


@dataclass
class RuleTally:
    """Spec §5.4's metric for one set of answers."""

    files: int = 0
    within_5s: int = 0
    within_10s: int = 0
    within_30s: int = 0
    early: int = 0
    late: int = 0
    none: int = 0

    def add(self, start_s: float | None, truth_s: float) -> None:
        self.files += 1
        if start_s is None:
            self.none += 1
            return
        error = start_s - truth_s
        self.within_5s += abs(error) <= 5
        self.within_10s += abs(error) <= 10
        self.within_30s += abs(error) <= 30
        if abs(error) > 30:
            self.early += error < 0
            self.late += error > 0

    def meets_spec(self) -> bool:
        return self.within_10s >= SPEC_WITHIN_10S and self.early <= SPEC_EARLY_MAX

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def text_candidates(start_s: float | None, end_s: float | None = None) -> list[Candidate]:
    """What the detector stores for an answer (``end_s`` None: the skip runs to the end of the file, Q3)."""
    if start_s is None:
        return []
    end_ms = None if end_s is None else int(round(end_s * 1000))
    return [Candidate(MarkerType.CREDITS, int(round(start_s * 1000)), end_ms, Source.CREDITS_TEXT)]


def epilogue_like(key_rows: Sequence[rule_j.Row], start_s: float | None) -> bool:
    """Whether a frame check must look at an answer for epilogue cards (spec §5.4: they aren't credits; rule J can't
    tell them from a roll they touch, ``test_rule_j.TestEpilogueCards``).

    True when the run's first frame with 3+ text boxes comes more than 10 s after the answer (it starts on sparse text),
    or a credit keyframe in the run's first 30 s is followed by the next one 10 s or more later (cards joined to the roll over a
    gap or black).
    """
    coarse = rule_j.coarse_start(key_rows)
    if coarse is None or start_s is None:
        return False
    run = list(key_rows[coarse.index : coarse.end_index + 1])
    dense = next((row[0] for row in run if row[1] >= 3), None)
    if dense is not None and dense - start_s > 10:
        return True
    credit = [row for row in run if rule_j.is_credit(row)]
    return any(b[0] - a[0] >= 10 and a[0] < coarse.pts_s + 30 for a, b in zip(credit, credit[1:]))


@dataclass
class TextRows:
    """Verdict counts per row, the Q3 end counts, and per-file details.

    Attributes:
        plex: Plex's first credits marker.
        text: Credits text alone (the detector's answer, no decision rules).
        high: What the pipeline publishes at High with credits text and Plex's markers (chapters left out).
        medium: The same at Medium.
        text_and_server_only: High decisions resting only on credits text + a server's own marker (Q2's pair).
        ends_found: Credits text answers with an end (more than 30 s of the file after the roll, Q3).
        ends_published: Per row (``high``, ``medium``): decisions whose skip stops before the end of the file.
        files: Per-file details (paths: local-only).
    """

    plex: Counter = field(default_factory=Counter)
    text: Counter = field(default_factory=Counter)
    high: Counter = field(default_factory=Counter)
    medium: Counter = field(default_factory=Counter)
    text_and_server_only: int = 0
    ends_found: int = 0
    ends_published: Counter = field(default_factory=Counter)
    files: list[dict] = field(default_factory=list)


def _decided(candidates: list[Candidate], duration_ms: int, is_movie: bool, level: str) -> tuple[tuple[float, float] | None, tuple[str, ...], str]:
    decision = decide(candidates, DecisionContext(duration_ms, is_movie, level, CREDITS, ORDER), {})[MarkerType.CREDITS]
    if decision.status is not DecisionStatus.DECIDED:
        return None, (), decision.reason
    marker = decision.marker
    return (marker.start_ms / 1000, marker.end_ms / 1000), marker.decided_by, decision.reason


def compare_text(
    files: list[dict],
    adjudicated: dict[str, dict],
    *,
    answers: Mapping[str, tuple[float | None, float | None]],
    probe: Callable[[str], MediaProbe],
    baseline: Mapping[str, list[PlexMarker]],
    is_movie: bool,
) -> TextRows:
    """Plex's first credits marker, credits text alone, and what the pipeline publishes with both (High, Medium).

    Args:
        files: Truth rows (``file``, ``credits_start``).
        adjudicated: Truth fixed by frame checks, by file name.
        answers: The app's credits text ``(start, end)`` per file (start None: no roll found; end None: open-ended).
        probe: A file's duration.
        baseline: Plex's markers by file path.
        is_movie: The files are movies.

    Returns:
        The rows.
    """
    rows = TextRows()
    for entry in files:
        path, truth = entry["file"], _truth(entry, adjudicated)
        duration = probe(path).duration_ms or 0
        markers = baseline.get(path, [])
        plex = first_marker(markers, MarkerType.CREDITS)
        start_s, end_s = answers.get(path, (None, None))
        text = text_candidates(start_s, end_s)
        servers = server_candidates(markers, MarkerType.CREDITS)
        detail = {"file": path, "name": _name(path), "truth": truth, "text": start_s, "text_end": end_s,
                  "duration": duration / 1000, "plex": plex.start_ms / 1000 if plex else None}  # fmt: skip
        rows.plex[judge_credits(detail["plex"], truth)] += 1
        rows.text[judge_credits(start_s, truth)] += 1
        rows.ends_found += end_s is not None
        for level in ("high", "medium"):
            segment, decided_by, reason = _decided(text + servers, duration, is_movie, level)
            getattr(rows, level)[judge_credits(segment[0] if segment else None, truth)] += 1
            detail[level], detail[f"{level}_reason"] = segment, reason
            if segment is not None and segment[1] * 1000 < duration:
                rows.ends_published[level] += 1
            if level == "high" and set(decided_by) == {"credits_text", "server_markers"}:
                rows.text_and_server_only += 1
        rows.files.append(detail)
    return rows


def wrong_cap(files: int, percent: int) -> int:
    """The most wrong answers a set of ``files`` may have at ``percent``, rounded up (controller ruling, 2026-09-16)."""
    return math.ceil(files * percent / 100)


def gate_checks(rows: TextRows, files: int) -> dict[str, bool]:
    """Q4's gate for one set, check by check (owner, 2026-09-16: precision first, never looser than Plex)."""
    medium_cap, high_cap = wrong_cap(files, MEDIUM_WRONG_PERCENT), wrong_cap(files, HIGH_WRONG_PERCENT)
    return {
        "Medium useful >= Plex useful": rows.medium["useful"] >= rows.plex["useful"],
        f"Medium wrong <= {medium_cap} ({MEDIUM_WRONG_PERCENT}% of {files})": rows.medium["wrong"] <= medium_cap,
        "Medium wrong <= Plex wrong": rows.medium["wrong"] <= rows.plex["wrong"],
        f"High wrong <= {high_cap} ({HIGH_WRONG_PERCENT}% of {files})": rows.high["wrong"] <= high_cap,
        "High wrong <= Plex wrong": rows.high["wrong"] <= rows.plex["wrong"],
    }


def beats_plex(rows: TextRows, files: int) -> bool:
    """Whether a set passes every check of :func:`gate_checks`."""
    return all(gate_checks(rows, files).values())


def merge_rows(parts: Iterable[TextRows]) -> TextRows:
    """One set's rows from its parts (the 80 = movies40 + tv40)."""
    merged = TextRows()
    for part in parts:
        for name in ("plex", "text", "high", "medium", "ends_published"):
            getattr(merged, name).update(getattr(part, name))
        merged.text_and_server_only += part.text_and_server_only
        merged.ends_found += part.ends_found
        merged.files.extend(part.files)
    return merged


def sheet_reasons(detail: dict, key_rows: Sequence[rule_j.Row]) -> list[str]:
    """Why a file's credits text answer gets a frame-check sheet (Q5, I7); empty: it doesn't.

    Every answer more than 10 s early (10–30 s early included), more than 30 s late, shaped like epilogue cards, or
    with an end (to check the scene after it is really a scene).
    """
    start, truth = detail["text"], detail["truth"]
    if start is None:
        return []
    reasons = []
    if start < truth - 30:
        reasons.append("early >30 s")
    elif start < truth - 10:
        reasons.append("early 10-30 s")
    if start > truth + 30:
        reasons.append("late >30 s")
    if epilogue_like(key_rows, start):
        reasons.append("epilogue-like")
    if detail["text_end"] is not None:
        reasons.append("end kept")
    return reasons


def undecided_credits(verdicts: Iterable[dict]) -> set[str]:
    """Online cases whose credits the other sources leave undecided: the only ones the pipeline reads credit text for."""
    return {v["key"] for v in verdicts if v["type"] == MarkerType.CREDITS.value and v["verdict"] == "missed"}


def hdr_kind(path: str, *, ffprobe: str) -> str:
    """``sdr``, ``hdr10`` (PQ or HLG), ``dv5`` (Dolby Vision profile 5) or ``dv_other`` (ffprobe reads only)."""
    out = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=color_transfer:stream_side_data=dv_profile", "-of", "json", path],
        capture_output=True, text=True, timeout=60,
    ).stdout  # fmt: skip
    stream = (json.loads(out or "{}").get("streams") or [{}])[0]
    profiles = [s.get("dv_profile") for s in stream.get("side_data_list") or [] if "dv_profile" in s]
    if profiles:
        return "dv5" if 5 in profiles else "dv_other"
    return "hdr10" if stream.get("color_transfer") in ("smpte2084", "arib-std-b67") else "sdr"


def sheet_command(ffmpeg: str, path: str, around_s: float, out: str) -> list[str]:
    """A 4×2 contact sheet of the 80 s around a time, one frame every 10 s (for adjudicating an answer)."""
    return [ffmpeg, "-v", "error", "-ss", f"{max(0.0, around_s - 40):.1f}", "-t", "80", "-i", path,
            "-vf", "fps=1/10,scale=320:-2,tile=4x2", "-frames:v", "1", "-y", out]  # fmt: skip


class CreditsTextCache:
    """``result(path)``: the app's credits text rows and start for a file, computed once per identity and version."""

    def __init__(
        self,
        root: Path,
        *,
        ffmpeg: str,
        decode: str,
        gpu_device: str | None,
        count_boxes: Callable[[np.ndarray], list[int]],
        probe: Callable[[str], MediaProbe],
    ) -> None:
        """Create the cache (``root/credits_text``).

        Raises:
            ValueError: ``root`` is under /data*.
        """
        if str(root.resolve()).startswith("/data"):
            raise ValueError("the credits text cache must not live under /data*")
        self._root = root / "credits_text"
        self._root.mkdir(parents=True, exist_ok=True)
        self._ffmpeg, self._decode, self._gpu_device = ffmpeg, decode, gpu_device
        self._count_boxes, self._probe = count_boxes, probe

    def result(self, path: str, *, is_episode: bool) -> dict:
        """``{"start_s", "end_s", "key", "fine", "end"}`` for a file (from the cache when this identity, detector version,
        decode path and kind were read before)."""
        st = os.stat(path)
        key = f"{path}|{st.st_size}|{st.st_mtime_ns}|{CREDITS_TEXT_VERSION}|{self._decode}|{is_episode}"
        cached = self._root / (hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest() + ".json")
        if cached.exists():
            return json.loads(cached.read_text())
        gpu = "NVIDIA" if self._decode == "gpu" else None
        found = find_credits(
            path, duration_ms=self._probe(path).duration_ms, is_episode=is_episode, ffmpeg=self._ffmpeg,
            count_boxes=self._count_boxes, gpu=gpu, gpu_device_path=self._gpu_device if gpu else None,
        )  # fmt: skip
        data = {"start_s": found.start_s, "end_s": found.end_s, "key": [list(r) for r in found.key_rows],
                "fine": [list(r) for r in found.fine_rows], "end": [list(r) for r in found.end_rows]}  # fmt: skip
        cached.write_text(json.dumps(data))
        return data


def _counter(decode: str, gpu_device: str) -> tuple[Callable[[np.ndarray], list[int]], Callable[[], None]]:
    from media_preview_generator.markers.credits import textdet_helper

    pool = textdet_helper.TextDetectorPool()
    gpu = "NVIDIA" if decode == "gpu" else None
    return (lambda planes: pool.count_boxes(planes, gpu=gpu, gpu_device_path=gpu_device if gpu else None)), pool.close_all


def _rows_summary(rows: TextRows) -> dict:
    return {"plex": dict(sorted(rows.plex.items())), "text": dict(sorted(rows.text.items())),
            "high": dict(sorted(rows.high.items())), "medium": dict(sorted(rows.medium.items())),
            "text_and_server_only": rows.text_and_server_only, "ends_found": rows.ends_found,
            "ends_published": dict(sorted(rows.ends_published.items()))}  # fmt: skip


def run_credits_text(
    *,
    decode: str,
    gpu_device: str,
    sets: tuple[str, ...],
    online: bool,
    cache_root: Path,
    ffmpeg: str,
    ffprobe: str,
    baseline_path: Path,
    sheets_dir: Path | None,
) -> tuple[dict, dict, bool]:
    """Every row for the chosen sets (``80`` = movies40 + tv40, ``205`` = the 205-movie set).

    Returns:
        The summary (counts and names), details (paths; local-only), and whether the gate passed (rule J on the 80
        meets the spec, and each chosen set passes :func:`gate_checks`).
    """
    evidence = evidence_dir()

    def load(name: str) -> list | dict:
        return json.loads((evidence / f"credits/{name}.json").read_text())

    adjudicated = load("adjudicated")
    probes = ProbeCache(cache_root, ffprobe=ffprobe)
    baseline = load_baseline(baseline_path)
    count_boxes, close = _counter(decode, gpu_device)
    cache = CreditsTextCache(cache_root, ffmpeg=ffmpeg, decode=decode, gpu_device=gpu_device, count_boxes=count_boxes, probe=probes.probe)
    kinds_of = {"movies40": True, "tv40": False, "movie_credit_truth": True}
    summary: dict = {"decode": decode, "detector_version": CREDITS_TEXT_VERSION, "sets": {}, "gate": {}, "sheets": []}
    details: dict = {}
    rule = RuleTally()
    kinds: dict[str, RuleTally] = {}
    passed = True
    try:
        for group in (g for g in ("80", "205") if g in sets):
            parts = []
            for name in GATE_SETS[group]:
                is_movie = kinds_of[name]
                files = load(name)
                results = {f["file"]: cache.result(f["file"], is_episode=not is_movie) for f in files}
                answers = {path: (r["start_s"], r["end_s"]) for path, r in results.items()}
                if group == "80":
                    for f in files:
                        truth = _truth(f, adjudicated)
                        rule.add(answers[f["file"]][0], truth)
                        kinds.setdefault(hdr_kind(f["file"], ffprobe=ffprobe), RuleTally()).add(answers[f["file"]][0], truth)
                rows = compare_text(files, adjudicated, answers=answers, probe=probes.probe, baseline=baseline, is_movie=is_movie)
                summary["sets"][name] = {"files": len(files), **_rows_summary(rows)}
                details[name] = rows.files
                parts.append(rows)
                for f in rows.files:
                    reasons = sheet_reasons(f, [tuple(r) for r in results[f["file"]]["key"]])
                    if not reasons:
                        continue
                    summary["sheets"].append({"set": name, "name": f["name"], "reasons": reasons})
                    if sheets_dir is not None:
                        sheets_dir.mkdir(parents=True, exist_ok=True)
                        stem = f"{name}-{hashlib.sha1(f['file'].encode(), usedforsecurity=False).hexdigest()[:10]}"
                        subprocess.run(sheet_command(ffmpeg, f["file"], f["text"], str(sheets_dir / f"{stem}.jpg")), check=False)
                        if f["text_end"] is not None:
                            subprocess.run(sheet_command(ffmpeg, f["file"], f["text_end"], str(sheets_dir / f"{stem}-end.jpg")), check=False)
            merged = merge_rows(parts)
            files_in_group = sum(len(r.files) for r in parts)
            checks = gate_checks(merged, files_in_group)
            summary["gate"][group] = {"files": files_in_group, **_rows_summary(merged), "checks": checks}
            passed = passed and all(checks.values())
        if "80" in sets:
            summary["rule_j_80"] = {**rule.as_dict(), "meets_spec": rule.meets_spec()}
            summary["rule_j_80_by_kind"] = {k: v.as_dict() for k, v in sorted(kinds.items())}
            passed = passed and rule.meets_spec()
        if online:
            summary["online"], details["online"] = _online(evidence, baseline, cache)
    finally:
        close()
    return summary, details, passed


def _online(evidence: Path, baseline: Mapping[str, list[PlexMarker]], cache: CreditsTextCache) -> tuple[dict, dict]:
    """The verified online cases at the app's three source settings, credit text added the way the pipeline adds it:
    only to cases whose credits the online answers and Plex's markers leave undecided."""
    results, dump = load_online(evidence)
    found = {case_key(r["case"]): case_file(r["case"], baseline.keys()) for r in results}
    servers = {key: server_candidates(baseline[path], MarkerType.CREDITS) for key, path in found.items() if path}
    texts = {}
    for key, path in found.items():
        if path:
            answer = cache.result(path, is_episode=True)
            texts[key] = text_candidates(answer["start_s"], answer["end_s"])
    summary: dict = {"cases": len(results), "files_found": sum(1 for p in found.values() if p)}
    details: dict = {}
    for label, order, level in SETTINGS:
        before = online_verdicts(results, dump, order=order, level=level, extra=servers)
        asked = undecided_credits(before)
        extra = {key: servers[key] + (texts[key] if key in asked else []) for key in servers}
        verdicts = online_verdicts(results, dump, order=order, level=level, extra=extra)
        summary[label] = {t: dict(sorted(c.items())) for t, c in tally(verdicts).items()}
        summary[label]["credits_text_asked"] = len(asked & texts.keys())
        details[label] = verdicts
    return summary, details
```

(`online.SETTINGS`' three source orders already list `credits_text`, checked while planning.)

- [ ] **Step 3: The CLI command**

In `tools/markers_eval/__main__.py`:

```python
def cmd_credits_text(args: argparse.Namespace) -> int:
    from .credits_text import run_credits_text

    ffmpeg = args.ffmpeg or shutil.which("ffmpeg")
    if ffmpeg is None:
        sys.exit("No ffmpeg found (pass --ffmpeg)")
    root = Path(args.cache or os.environ.get("MARKERS_EVAL_CACHE") or Path.home() / ".cache/markers_eval")
    baseline = Path(args.plex_baseline) if args.plex_baseline else evidence_dir() / DEFAULT_BASELINE
    summary, details, passed = run_credits_text(
        decode=args.decode, gpu_device=args.gpu_device, sets=tuple(args.sets.split(",")), online=args.online,
        cache_root=root, ffmpeg=ffmpeg, ffprobe=ffprobe_path_for(ffmpeg), baseline_path=baseline,
        sheets_dir=Path(args.sheets) if args.sheets else None,
    )  # fmt: skip
    print(json.dumps(summary, indent=2))
    if args.json:
        Path(args.json).write_text(json.dumps({**summary, "details": details}, indent=1, default=str))
    return 0 if passed else 1
```

and in `main()` (add `import shutil` at the top):

```python
    text = sub.add_parser("credits-text", help="credit text (rule J) on the 80- and 205-file credits sets vs Plex")
    text.add_argument("--decode", choices=("gpu", "cpu"), default="gpu")
    text.add_argument("--gpu-device", default="cuda:0")
    text.add_argument("--sets", default="80,205", help="comma-separated: 80, 205")
    text.add_argument("--online", action="store_true", help="also the 43 verified online cases")
    text.add_argument("--sheets", help="write frame-check sheets (answers >10 s early, >30 s late, epilogue-like, with an end) to this local-only folder")
    text.add_argument("--ffmpeg")
    text.add_argument("--cache")
    text.add_argument("--plex-baseline")
    text.add_argument("--json", help="write details (local-only: holds file paths)")
    text.set_defaults(func=cmd_credits_text)
```

Run: `/home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/markers_eval -q` — Expected: PASS.

- [ ] **Step 4: Run the harness (storage, local data, one heavy run at a time)**

```bash
cd /home/data/workspace/plex_generate_vid_previews
export MEDIA_PREVIEW_TEXTDET_MODEL="$MARKERS_BENCH_DIR/textdet-model/ch_PP-OCRv4_det_infer.onnx"
nice -n 19 /home/data/.venv/bin/python -m tools.markers_eval credits-text --decode gpu --sets 80,205 --online \
  --sheets "$HOME/.cache/markers_eval/sheets-phase3" --json docs/design/intro-credits/evidence/eval/phase3_credits_gpu.json
nice -n 19 /home/data/.venv/bin/python -m tools.markers_eval credits-text --decode cpu --sets 80 \
  --json docs/design/intro-credits/evidence/eval/phase3_credits_cpu.json
```

Expected (the GPU run takes roughly 80 × 15 s + 205 × 17 s ≈ 1.3 h; the CPU run ≈ 80 × 35 s): exit 0 when the gate
passes. `rule_j_80` should land near the fixture's 59 / 1 / 8 / 4 (the app decodes the refine window itself, the
prototype decoded around the truth: small differences are real, report them). `summary["gate"]` lists every check
per set.

**If any gate check fails, stop and report** — don't tune rule J, loosen a cap, or change a decision rule. Report the
failing checks with their numbers per set, the files behind each wrong answer (names only) with their sheets'
adjudication, and whether they rest on credits text alone or on text + Plex. The planning numbers pass on the 80 with
one High wrong answer at the cap of 1 (one TV episode whose 23 s credits both sources place early): a second one fails
the set. The harness code and `phase3-harness.md` (with the failing checks shown) are still committed
in Step 6; the controller holds Task 12 until the owner has ruled. **If the CPU run misses the gate while the GPU run passes**, rerun the CPU decode with
`scale=320:180:flags=bilinear` in `frames._scale_filter` (a candidate match for scale_cuda) and report both; don't
change the product filter without the numbers.

- [ ] **Step 5: Write `phase3-harness.md` and the README**

`docs/design/intro-credits/evidence/eval/phase3-harness.md`: the date, commit, host; the bench compare lines from Task 4
Step 7; `rule_j_80` (GPU and CPU) against spec §5.4 and the fixture, and per HDR kind; for movies40, tv40,
movie_credit_truth the rows as a table (Plex / credits text alone / as the pipeline runs at High / at Medium), the
`text_and_server_only`, `ends_found` and `ends_published` counts; the gate per set (80, 205) check by check with the
caps; the online table with `credits_text_asked`; and the frame-check table: one row per file in `summary["sheets"]`
(set, movie/show name, reasons, answer − truth, end − duration when it has an end, and the adjudication the sheet
supports: "epilogue cards", "names over footage", "really early", "correct", "late", or for an end "scene kept",
"scene swallowed", "logo or black, not a scene"). Counts and names only.

`tools/markers_eval/README.md`: a "`credits-text`" section (what each row means, the gate, the two commands, run
times, `--sheets`), a "Text detection bench" section (Task 4's three commands and where the frames live), and a "Rule
J fixture" section (`python -m tools.markers_eval.credits_fixture`, what it anonymises).

`grep -nE '/data|\.mkv|\{(tmdb|tvdb|imdb)-' docs/design/intro-credits/evidence/eval/phase3-harness.md` must print
nothing; the two `phase3_credits_*.json` files are git-ignored (`evidence/**/*.json`).

- [ ] **Step 6: Commit**

Stage `credits_text.py`, `__main__.py`, the README, the test file and `phase3-harness.md`; Architecture Review on the
staged diff; then `PATH="/home/data/.venv/bin:$PATH" git commit -m "feat(markers-eval): credits text harness against Plex with the owner's gate"`.

---
## Task 12: Docs and spec amendments

`[sequential]` (after Tasks 9, 10, 11 and the milestone audit) — spec §0 working rule "Update this spec (not just code)
whenever a decision changes; add a dated line to §14", `.claude/rules/docs.md`, C1–C7, T-R1–T-R9, the owner's Q1–Q8
(Task 9 already wrote Q1–Q3 into §5.4/§5.5 and all eight answers into §14; this task doesn't repeat them), preflight
M7 (copy numbers from the product run), M17.

**OWNER DECISIONS (2026-09-16) this task documents for users:** Q1 (alone at Medium), Q3 (the skip stops at the last
credit when a scene follows; Emby skips to the end of the file), Q4 (the gate numbers the guide quotes come from Task
11), Q5 (rule J as measured), Q6 (AMD: self-test), Q8 (image size in the PR, not the guide).

Milestone audit first (execution model): three parallel opus auditors over `ba3c0f8..feat` for phase 3 by area —
detection (`markers/credits/*`, `processing/hwaccel.py`), pipeline and decisions (`pipeline.py`, `decide.py`, the
tests of both), UI/harness/Docker (`api_markers.py`, `settings.html`, `tools/markers_eval/credits_text.py`,
`Dockerfile`, `scripts/fetch_textdet_model.py`). Fix every HIGH and MED in the owning task's files before this task
starts; record parked LOWs in the ledger.

**Files:**
- Modify: `docs/design/intro-credits/spec.md`, `docs/design/intro-credits/plan-roadmap.md`,
  `docs/design/intro-credits/evidence/README.md`, `docs/reference.md`, `docs/guides.md`, `README.md`; only when Task
  11's numbers differ from the copy Task 10 shipped: `media_preview_generator/web/templates/settings.html` and
  `tests/e2e/test_intro_credits_settings.py` (run that e2e file before committing)
- Test: the grep checks in Step 7

**Interfaces:**
- Consumes: the owner's answers to Q1–Q8 (this plan and spec §14 after Task 9); Task 1's `phase3-measurements.md`;
  Task 11's `phase3-harness.md` (the app's own numbers, and the owner's ruling if a gate check failed); the shipped
  copy from Task 10.
- Produces: the spec and guides Task 13's lab rows and Task 14's PR body quote.

- [ ] **Step 1: spec §5.4**

- "Frames": add T-R4 (episodes 450 s, every other file 900 s), T-R5 (rows in decode order), T-R6 (rounding, and the
  `-copyts` start-time note), the end window (Q3: 1 fps over the 21 s from 1 s before the run's last credit keyframe,
  decoded only when more than 30 s of the file follows it), T-R7 (decode timeouts wait a day), and the
  refine note from C5: "The table's keyframe row was measured with the prototype's 10 s refine span; the app refines over
  20 s, which gives 59 / 1 early / 8 late / 4 none on the same rows (`tests/fixtures/markers/credits_rule_j_80.json.gz`)."
  Add the measured TV tail line from `phase3-measurements.md` M0.
- "Text detector": the rapidocr limit note from C6; "Post-processing is vendored in `markers/credits/textdet.py`
  (pyclipper kept for the unclip step, T-R2); identical box counts to rapidocr_onnxruntime 1.4.4 on the 289-frame bench
  (`evidence/eval/phase3-harness.md`)"; the pinned model and path; "arm64: no WebGPU EP wheel, CPU only"; the guard now
  reads "a 20-frame self-test on that device counts exactly the CPU's boxes, faster"; device choice per T-R3 with the
  storage BMC fact; the storage self-test numbers.
- Replace "Full rule tuning happens in phase 3 on a larger hand-checked set." with the owner's Q5 answer: "Rule J ships
  as measured; every answer more than 10 s early or 30 s late, shaped like epilogue cards, or with an end is
  frame-checked and adjudicated in `evidence/eval/phase3-harness.md`; tuning is a follow-up that must beat J on both
  sets with no more early answers."
- Add "Epilogue text cards touching the roll, or joined to it over black, become the start (pinned in
  `test_rule_j.TestEpilogueCards`); the harness frame-checks every answer shaped like that."
- Add the harness results in three lines (rule J alone on the 80, GPU and CPU decode; the gate per set check by check;
  ends found and scenes kept).

- [ ] **Step 2: spec §5.5, §6.4, §7, §8, §10, §12, §13**

- §5.5: rule 6 and §5.4's credits text paragraph already carry Q1–Q3 (Task 9); add nothing there unless the owner's
  Task 11 ruling changed a rule.
- §6.2 step 3 (C7): "A decided type doesn't ask a local detector again on a normal run (only a forced run, an answer
  of another version, or an answer the decision rests on does); a chapter decision is final for credits text."
- §6.4 item 7: the module name (T-R1), one shared CPU helper, idle exit and the 5 s replace margin (T-R8), the
  self-test's box equality, T-R3, a GPU helper that exits between requests moves its device to the CPU for the process
  lifetime, the process-group kill and pipe reaper, and (M17) "the availability check runs its subprocess once per
  process under a lock, from the first job or the Settings page: that first caller waits up to 30 s";
  item 10: "text detection is asked chunk by chunk (64 frames) and the decode checks for cancel between chunks".
- §7.2: the credit text row is live with "Not available" and the reasons (Task 10's copy); §7.3: the "Credit text"
  lane.
- §8: `MEDIA_PREVIEW_TEXTDET_MODEL` overrides the model path for development and the harness (not a setting).
- §10.1: textdet helper, frames, detector and decision-matrix tests exist; §10.2: `python -m tools.markers_eval
  credits-text` and the bench.
- §12 phase 3: "built" with the harness gate result (Task 14 adds "lab-proven" and the image digest).
- §13: AMD untested, self-test decides (Q6); Dolby Vision profile 5 credits text unmeasured if Task 1 M5 found none;
  Dawn's device choice on two-GPU hosts until Task 13 row 14 (T-R3); a lone text keyframe inside a scene within 24 s
  of the roll extends the run over it, so that end lands inside the scene (not measured; the harness sheets of files
  with an end show whether it happens).

- [ ] **Step 3: spec §0 and §14**

§0 Status: add "Phase 3 (credits text) is built: … (`plan-phase3.md`; ledger `.superpowers/sdd/plan-phase3/progress.md`)";
next: the phase-3 lab matrix.

§14, one dated line each (2026-09-16 or the day the ruling landed): "Phase 3 plan rulings" (T-R1–T-R9, one clause
each with its cost if wrong); "Contradictions resolved while planning phase 3" (C1–C7, one clause each); the owner's
ruling on any failed Task 11 gate check. (The owner's Q1–Q8 lines are Task 9's.)

- [ ] **Step 4: roadmap**

`plan-roadmap.md` phase 3: under the bullets, "Plan: `plan-phase3.md` (14 tasks)"; mark the roadmap's "unclip computed
analytically" clause superseded by T-R2 and "Docker: numpy, …" as including pyclipper; add Q5's answer to "Done when".
(Task 14 ticks the bullets.)

- [ ] **Step 5: user docs**

- `docs/guides.md`, Intro & Credits → "Sources and the publish rule" table: the "On-screen credit text" row becomes
  "Finds where the credit roll starts from text on screen in the last 15 minutes of a movie (7.5 of an episode), and
  stops the skip at the last credit when a scene follows the roll (Emby skips to the end of the file). Tested alone on
  80 files: within 10 s on N, more than 30 s early on N. At High another source has to agree; at Medium it can publish
  alone. Uses your GPU when a quick self-test shows it is faster than the CPU and finds the same text; otherwise the
  CPU (about 10–30 s per file)." The N values are `rule_j_80` from Task 11's GPU run of the app (M7), the range is
  Task 10's Settings subtitle; if either differs from what Task 10 shipped, update the Settings tooltip/subtitle and
  their e2e assertions in this task too (add `settings.html` and `tests/e2e/test_intro_credits_settings.py` to the
  staged files).
  Troubleshooting gains "On-screen credit text says Not available" (the four reasons and what to do: use the Docker
  image; the check runs again in 10 minutes) and "Credit text runs on the CPU although I have a GPU" (the log line
  `Credit text detection on <device>: CPU (<reason>)` and its reasons: slower than the CPU, Vulkan without a hardware GPU,
  no WebGPU device for that GPU, a helper failure; arm64 has no GPU path).
- `docs/reference.md`: the `sources[].id` note for `credits_text` ("runs where text detection is available, see
  `GET /api/markers/sources/local`; it decides credits alone only at `medium`" per Q1); the `GET
  /api/markers/sources/local` section's response gains `credits_text: {available, message}` with an example; a line
  under environment variables for developers: `MEDIA_PREVIEW_TEXTDET_MODEL`.
- `README.md` features: "Intro & Credits: … and the on-screen credit roll (GPU on any vendor, CPU fallback)".
- `docs/design/intro-credits/evidence/README.md`: rows for `credits/phase3/` (measurement scripts),
  `credits/phase3-measurements.md`, `eval/phase3-harness.md`, and the local-only harness caches.

- [ ] **Step 6: Screenshots already confirmed**

The guide gets no new screenshots in phase 3 (the owner's UI checkpoint in Task 10 covered the look). If the guide
references the Settings row image, reuse Task 10's confirmed screenshot.

- [ ] **Step 7: Checks and commit**

```bash
cd /home/data/workspace/plex_generate_vid_previews
grep -nE "Coming soon|isn't built|not built yet" docs/guides.md docs/reference.md docs/design/intro-credits/spec.md | grep -i credit   # nothing
grep -nE '/data_|\.mkv|\{(tmdb|tvdb|imdb)-' docs/design/intro-credits/spec.md docs/guides.md docs/reference.md | grep -v 'evidence/' | head   # nothing new
pre-commit run --files docs/design/intro-credits/spec.md docs/design/intro-credits/plan-roadmap.md docs/reference.md docs/guides.md README.md docs/design/intro-credits/evidence/README.md
```

Architecture Review on the staged diff (docs only; it checks comments-vs-code drift); then
`PATH="/home/data/.venv/bin:$PATH" git commit -m "docs(intro-credits): on-screen credit text in the spec and guides"`.

---
## Task 13: Phase-3 lab matrix (`phase3_matrix.py`)

`[sequential]` (after Tasks 7 and 12) `[high-risk]` — spec §10.3 (lab on storage, the app image against the lab
servers), §12 phase 3 done-when ("GPU path proven on storage NVIDIA and plex NVIDIA + Intel (self-test picks CPU on the
iGPU)"), roadmap owner checkpoints, Q3, Q7, T-R3, preflight I2 (row 5 kills the helper during a request), M3 (row 9),
M12 (rows 3, 10, plex premises); phase-2 lab lessons (ledger, Task 17 Architecture Review MEDs: every row asserts its
premise before its result; restores run in `finally`, including on SIGTERM; `docker stop` inside `try`; `MLAB_DIR`
guards; results text matches the JSON).

**OWNER DECISION (2026-09-16), Q7:** a throwaway container on `plex` is allowed for rows 12–15 only — the synthetic
movie, no `/data*` or Plex config mounts, no prod Plex access, removed right after. Nothing else in this task runs on
`plex`.
**OWNER DECISION (2026-09-16), Q3:** row 2 checks where the skip ends when a scene follows the roll; row 16 checks a
roll that runs to the end of the file.

**Lab rules:** lab servers on storage only; never the prod Plex. Rows 12–15 run on the `plex` host under Q7's terms
only; the container is removed after. Everything a row creates lives under `evidence/lab/synth/`. Tokens from `evidence/lab/env`,
scrubbed by `phase1_matrix.scrub`. One heavy step at a time; `nice -n 19` for encodes.

**Files:**
- Create: `docs/design/intro-credits/evidence/lab/synth_credits.sh`, `docs/design/intro-credits/evidence/lab/phase3_matrix.py`,
  `docs/design/intro-credits/evidence/lab/plex_rows.py`, `docs/design/intro-credits/evidence/lab/phase3-results.md`
- Modify: `docs/design/intro-credits/evidence/lab/app.sh` (`MLAB_APP_GPU=nvidia` adds the NVIDIA runtime and
  `/dev/dri`; `MLAB_APP_EXTRA_ENV` passes one `-e` for row 8), `docs/design/intro-credits/evidence/lab/up.sh` (mount
  `synth/Synth Credits (2024)` and `synth/Synth Credits Open (2025)` read-only for every server and the app under
  `/media/synth-credits`),
  `docs/design/intro-credits/evidence/README.md` (rows for the four new files)

**Interfaces:**
- Consumes (`phase1_matrix` as `p1`, signatures as on the branch): `ENV`, `LAB`, `RESULTS`, `SHOTS`, `VENV_PYTHON`,
  `APP`; `app(method, path, body=None) -> tuple[int, Any]`, `app_ok(method, path, body=None) -> Any`,
  `sh(*cmd, check=True, timeout=300) -> str`, `say(*parts)`, `now_iso() -> str`, `scrub(value)`,
  `wait_until(what, fn, timeout=300, every=2)`, `start_markers_job(body: dict) -> dict`,
  `wait_job(job_id, timeout=1800) -> dict`, `job_files(job_id) -> list[dict]`, `item_payload(path) -> dict`,
  `job_logs(job_id) -> list[str]`. (`phase2_matrix` as `p2`): `SYNTH_LIBRARIES` (name → (Plex type, collection,
  path)), `ALL_MARKER_SERVERS`, `configure()`, `rescan_until(name, paths, *, present)`, `set_publish_when(level)`,
  `run_job(body, timeout=1800) -> tuple[dict, list[dict]]`, `inspector_server(path, server_id) -> dict` (its
  `published` markers and `publish_message`), `EMBY_SERVERS`, `_rescan_best_effort(name, gone)`; `scale_mounts.sh`
  (row 10's path map). The app routes
  `GET /api/markers/sources/local` (Task 10's `credits_text`), `POST /api/system/rescan-gpus` (`gpus[]` with
  `device`, `name`, `type`), `GET/POST /api/settings` (`gpu_config`, `cpu_threads`, `markers`), `POST
  /api/jobs/<id>/cancel`; Task 5's log lines `Credit text detection on <key>: GPU (…)` / `: CPU (<reason>)` and the
  WARNING `… moves to the CPU for the rest of this run of the app: …`; Task 8's phase string "Reading the credits…";
  the worker's "couldn't process … on the GPU and is retrying on CPU".
- Produces: `results/p3-row-NN.json` (git-ignored) and `phase3-results.md` (Task 14 pastes its table into the PR).

- [ ] **Step 1: `synth_credits.sh`**

```bash
#!/bin/bash
# Synthetic movies for the credit text lab rows. Each: 420 s of text-free gradients, then 120 s of white names scrolling
# up over black (a line every 3 s). "Synth Credits" goes on with a 60 s scene after the roll (Q3: the skip ends at the
# last credit, ≈540 s); "Synth Credits Open" ends with the roll (the skip runs to the end). Truth: start 420 s.
#
#   ./synth_credits.sh          encode missing files
#   ./synth_credits.sh force    re-encode everything
#
# MLAB_DIR sets the lab folder that holds synth/ (default: this script's folder).
#
# Output (git-ignored; up.sh and app.sh mount both movie folders read-only under /media/synth-credits):
#   synth/Synth Credits (2024)/Synth Credits (2024).mkv                 H.264, keyframe every 2 s, 600 s
#   synth/Synth Credits Open (2025)/Synth Credits Open (2025).mkv       H.264, 540 s
#   synth/_staging/Synth Credits (2024) - AV1.mkv                       AV1 (Pascal has no AV1 NVDEC: the GPU-decode-failure row)
set -euo pipefail

readonly HERE="${MLAB_DIR:-$(cd "$(dirname "$0")" && pwd)}"
readonly SCENE_DIR="${HERE}/synth/Synth Credits (2024)"
readonly OPEN_DIR="${HERE}/synth/Synth Credits Open (2025)"
readonly STAGING_DIR="${HERE}/synth/_staging"
readonly FONT="/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
readonly FORCE="${1:-}"

command -v ffmpeg >/dev/null || { echo "ffmpeg not found" >&2; exit 1; }
[[ -f "$FONT" ]] || { echo "font not found: $FONT" >&2; exit 1; }
[[ -d "${HERE}/synth" ]] || { echo "no synth folder in ${HERE} (set MLAB_DIR to the lab folder)" >&2; exit 1; }
mkdir -p "$SCENE_DIR" "$OPEN_DIR" "$STAGING_DIR"

roll=""
for i in $(seq 0 39); do
    roll+="${roll:+,}drawtext=fontfile=${FONT}:text='CREDIT NAME ${i}':fontcolor=white:fontsize=24:x=(w-tw)/2:y=h-20*t+$((i * 60))"
done

# encode <out> <scene seconds, 0 for none> <codec args…>
encode() {
    local out="$1" scene_s="$2"; shift 2
    [[ -f "$out" && "$FORCE" != "force" ]] && return 0
    local inputs=(-f lavfi -i "gradients=size=640x360:rate=24:speed=0.02,trim=duration=420,setpts=PTS-STARTPTS"
                  -f lavfi -i "color=c=black:size=640x360:rate=24:duration=120,${roll}")
    local graph="[0:v][1:v]concat=n=2:v=1:a=0[v]"
    if (( scene_s > 0 )); then
        inputs+=(-f lavfi -i "gradients=size=640x360:rate=24:speed=0.05:seed=9,trim=duration=${scene_s},setpts=PTS-STARTPTS")
        graph="[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]"
    fi
    nice -n 19 ffmpeg -v error -y "${inputs[@]}" -filter_complex "$graph" -map "[v]" -g 48 "$@" "$out"
}

encode "${SCENE_DIR}/Synth Credits (2024).mkv" 60 -c:v libx264 -preset veryfast -pix_fmt yuv420p
encode "${OPEN_DIR}/Synth Credits Open (2025).mkv" 0 -c:v libx264 -preset veryfast -pix_fmt yuv420p
encode "${STAGING_DIR}/Synth Credits (2024) - AV1.mkv" 60 -c:v libsvtav1 -preset 10 -pix_fmt yuv420p
ls -la "$SCENE_DIR" "$OPEN_DIR" "$STAGING_DIR" | grep -i 'synth credits'
```

Run: `cd docs/design/intro-credits/evidence/lab && ./synth_credits.sh`
Expected: three files listed. Check the truth once with the app's code on the host (Task 8's integration test builds
the same two clips): start within 10 s of 420 s on both, end 540 ± 3 s on "Synth Credits", no end on "Synth Credits
Open".

- [ ] **Step 2: `app.sh` GPU and env options; `up.sh` mount**

`app.sh`, header comment gains two lines ("`MLAB_APP_GPU=nvidia` gives the app the NVIDIA runtime and /dev/dri;
`MLAB_APP_EXTRA_ENV=NAME=value` passes one extra environment variable (phase 3 row 8)"), and before
`docker run -d --name mlab-app`:

```bash
    GPU_ARGS=()
    if [[ "${MLAB_APP_GPU:-}" == "nvidia" ]]; then
        GPU_ARGS=(--runtime=nvidia -e NVIDIA_VISIBLE_DEVICES=all -e NVIDIA_DRIVER_CAPABILITIES=all --device /dev/dri:/dev/dri)
    fi
    EXTRA_ENV=()
    if [[ -n "${MLAB_APP_EXTRA_ENV:-}" ]]; then
        EXTRA_ENV=(-e "$MLAB_APP_EXTRA_ENV")
    fi
```

then `"${GPU_ARGS[@]}" "${EXTRA_ENV[@]}"` on the `docker run` line before `"${MV[@]}"`. (`set -u` with an empty array
expands fine on bash ≥ 4.4; storage has 5.2.)

`up.sh`: append to `MV`:

```bash
    -v "${HERE}/synth/Synth Credits (2024):/media/synth-credits/Synth Credits (2024):ro"
    -v "${HERE}/synth/Synth Credits Open (2025):/media/synth-credits/Synth Credits Open (2025):ro"
```

and extend the "Synth folders are mounted one show at a time" comment with "synth-credits = synth_credits.sh output
(two movies, one library)".
Every lab server and `mlab-app` then see the folder (app.sh sources `MV`); `_staging` stays invisible.

- [ ] **Step 3: `phase3_matrix.py` skeleton and shared helpers**

```python
#!/usr/bin/env python3
"""Phase 3 lab matrix for Intro & Credits (plan-phase3 Task 13): on-screen credit text, on phase 1 and 2's helpers.

    ./phase3_matrix.py configure              phase 2 configure + the Synth Credits library on every server
    ./phase3_matrix.py run 1 2 3 ...          run storage rows in the given order
    ./phase3_matrix.py rows                   list the rows

Rows 12–15 run on the plex host through plex_rows.py under the owner's Q7 terms (plan-phase3 Task 13 Step 6); this script refuses
them. Each row writes results/p3-row-NN.json (git-ignored) with its premise, checks and evidence; credentials are
scrubbed. A row whose premise doesn't hold fails, whatever its checks say. Rows put lab state back in `finally`, also
when the run is stopped (SIGTERM). MLAB_DIR sets the lab folder; MLAB_SHOTS where screenshots go; MLAB_APP_IMAGE the
image app.sh recreates the app from.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import phase1_matrix as p1
import phase2_matrix as p2
from phase1_matrix import app_ok, now_iso, say, scrub, sh, wait_until  # time and wait_until: rows 5–8

HERE = Path(__file__).resolve().parent
RESULTS = p1.RESULTS
CREDITS_ROOT = "/media/synth-credits"
MOVIE_FOLDER = f"{CREDITS_ROOT}/Synth Credits (2024)"
MOVIE = f"{MOVIE_FOLDER}/Synth Credits (2024).mkv"  # a 60 s scene follows the roll (Q3)
OPEN_MOVIE = f"{CREDITS_ROOT}/Synth Credits Open (2025)/Synth Credits Open (2025).mkv"  # the roll runs to the end
AV1_MOVIE = f"{MOVIE_FOLDER}/Synth Credits (2024) - AV1.mkv"
HOST_FOLDER = p1.LAB / "synth" / "Synth Credits (2024)"
STAGED_AV1 = p1.LAB / "synth" / "_staging" / "Synth Credits (2024) - AV1.mkv"
TRUTH_MS = 420_000
MOVIE_MS, OPEN_MOVIE_MS = 600_000, 540_000
END_WINDOW_MS = (536_000, 541_000)  # the last name leaves the screen at 540 s
BROKEN_MODEL_ENV = "MEDIA_PREVIEW_TEXTDET_MODEL=/nonexistent/model.onnx"
OWNER_GATED = frozenset({12, 13, 14, 15})
# Phase 2's configure and rescans walk this dict: the credits movie becomes one more synth library on every server.
p2.SYNTH_LIBRARIES["Synth Credits"] = ("movie", "movies", CREDITS_ROOT)


def write_result(row: int, title: str, result: str, evidence: dict, notes: list[str] | None = None) -> dict:
    RESULTS.mkdir(exist_ok=True)
    body = scrub({"row": row, "title": title, "result": result, "at": now_iso(), "notes": notes or [], **evidence})
    (RESULTS / f"p3-row-{row:02d}.json").write_text(json.dumps(body, indent=2, default=str) + "\n")
    say(f"p3 row {row}: {result} — {title}")
    for note in notes or []:
        say(f"  - {note}")
    return body


def checks_result(
    row: int, title: str, premise: dict[str, bool], checks: dict[str, bool], evidence: dict, notes: list[str] | None = None
) -> dict:
    """Pass when the premise and every check hold; a failed premise is "fail (premise)", never a pass."""
    lines = [f"premise {k}: {v}" for k, v in premise.items()] + [f"{k}: {v}" for k, v in checks.items()] + (notes or [])
    if not all(premise.values()):
        result = "fail (premise)"
    else:
        result = "pass" if all(checks.values()) else "fail"
    return write_result(row, title, result, {"premise": premise, "checks": checks, **evidence}, lines)


def credits_evidence(path: str) -> list[dict]:
    return [e for e in p1.item_payload(path)["evidence"] if e["source"] == "credits_text"]


def near_truth(rows: list[dict], tolerance_ms: int = 10_000) -> bool:
    return len(rows) == 1 and rows[0]["type"] == "credits" and abs(rows[0]["start_ms"] - TRUTH_MS) <= tolerance_ms


def app_log_lines(since: str, needle: str) -> list[str]:
    """mlab-app's log lines since an ISO time that contain ``needle`` (loguru writes to both streams)."""
    out = subprocess.run(["docker", "logs", "--since", since, "mlab-app"], capture_output=True, text=True, timeout=60)
    return [scrub(line) for line in (out.stdout + out.stderr).splitlines() if needle in line]


def recreate_app(*, gpu: bool, extra_env: str = "") -> None:
    """app.sh recreate with or without the NVIDIA runtime and one extra environment variable (MLAB_APP_IMAGE kept)."""
    env = {**os.environ, "MLAB_APP_GPU": "nvidia" if gpu else "", "MLAB_APP_EXTRA_ENV": extra_env}
    out = subprocess.run(["./app.sh", "recreate"], cwd=HERE, env=env, capture_output=True, text=True, timeout=300)
    if out.returncode != 0:
        raise RuntimeError(f"app.sh recreate -> {out.returncode}: {scrub(out.stderr[-2000:])}")
    say(f"mlab-app recreated (gpu={gpu}, extra env={'set' if extra_env else 'none'})")


def credits_job(label: str, *, force: bool, paths: tuple[str, ...] = (MOVIE,), timeout: float = 1800) -> tuple[dict, list[dict]]:
    return p2.run_job({"file_paths": list(paths), "library_name": f"Phase 3 {label}", "force": force}, timeout=timeout)


class ProcessSampler(threading.Thread):
    """Credit-text decodes and text detection helpers in mlab-app, every 0.5 s, until the block ends."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.decodes: list[str] = []
        self.helpers: dict[str, str] = {}
        self.peak_helpers = 0
        self.stop = threading.Event()

    def run(self) -> None:
        while not self.stop.is_set():
            out = sh("docker", "exec", "mlab-app", "ps", "-eo", "pid=,args=", check=False)
            running = 0
            for line in out.splitlines():
                pid, _, args = line.strip().partition(" ")
                if "ffmpeg" in args and "showinfo" in args and "rawvideo" in args and args not in self.decodes:
                    self.decodes.append(args)
                if "textdet_helper" in args and "--check" not in args:
                    running += 1
                    self.helpers.setdefault(pid, args)
            self.peak_helpers = max(self.peak_helpers, running)
            self.stop.wait(0.5)

    def webgpu_pids(self) -> list[str]:
        return [pid for pid, args in self.helpers.items() if "--backend webgpu" in args]

    def __enter__(self) -> ProcessSampler:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop.set()
        self.join(timeout=5)


def container_path(host_path: str) -> str | None:
    """Where the app sees a real file the scale mounts expose (``scale_mounts.sh`` ``-v host:container:ro`` lines)."""
    for line in (p1.LAB / "scale_mounts.sh").read_text().splitlines():
        match = re.search(r'-v "([^"]+):([^":]+):ro"', line)
        if match and (host_path == match.group(1) or host_path.startswith(match.group(1).rstrip("/") + "/")):
            return match.group(2) + host_path[len(match.group(1)) :]
    return None


class CpuOnly:
    """Every GPU off and one CPU worker for a block (a known CPU answer); the settings found are put back after."""

    def __enter__(self) -> None:
        settings = app_ok("GET", "/api/settings")
        self.before = {"gpu_config": settings.get("gpu_config") or [], "cpu_threads": settings.get("cpu_threads")}
        off = [{**entry, "enabled": False, "workers": 0} for entry in self.before["gpu_config"]]
        app_ok("POST", "/api/settings", {"gpu_config": off, "cpu_threads": 1})
        say("GPUs off, one CPU worker")

    def __exit__(self, *exc: object) -> None:
        app_ok("POST", "/api/settings", self.before)
        say("worker settings put back")


class GpuWorker:
    """One worker on the container's NVIDIA GPU and no CPU workers for a block; the settings found are put back after."""

    def __enter__(self) -> dict:
        settings = app_ok("GET", "/api/settings")
        self.before = {"gpu_config": settings.get("gpu_config") or [], "cpu_threads": settings.get("cpu_threads")}
        gpus = app_ok("POST", "/api/system/rescan-gpus")["gpus"]
        nvidia = next((g for g in gpus if g.get("type") == "NVIDIA" and g.get("status") != "failed"), None)
        if nvidia is None:
            raise RuntimeError(f"mlab-app sees no working NVIDIA GPU: {[(g.get('type'), g.get('status')) for g in gpus]}")
        entry = {"device": nvidia["device"], "name": nvidia.get("name", ""), "type": "NVIDIA", "enabled": True,
                 "workers": 1, "ffmpeg_threads": 2}  # fmt: skip
        app_ok("POST", "/api/settings", {"gpu_config": [entry], "cpu_threads": 0})
        say(f"one GPU worker on {nvidia['device']}, CPU workers off")
        return nvidia

    def __exit__(self, *exc: object) -> None:
        app_ok("POST", "/api/settings", self.before)
        say("worker settings put back")


def screenshot(page_script: str, shot: Path) -> dict:
    """Run a Playwright snippet (logged in, ``pg`` open) in the shared venv; returns its JSON line and the path."""
    p1.SHOTS.mkdir(parents=True, exist_ok=True)
    script = f"""
import asyncio, json, sys
from playwright.async_api import async_playwright
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(); pg = await b.new_page(viewport={{"width": 1500, "height": 1100}})
        await pg.goto("{p1.APP}/login"); await pg.fill("#token", sys.stdin.read().strip()); await pg.keyboard.press("Enter")
        await pg.wait_for_timeout(3000)
{page_script}
        await pg.screenshot(path="{shot}", full_page=True); await b.close()
asyncio.run(main())
"""
    out = subprocess.run([p1.VENV_PYTHON, "-c", script], input=p1.ENV["MLAB_APP_TOKEN"], capture_output=True, text=True,
                         timeout=180)  # fmt: skip
    last = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else "null"
    return {"screenshot": str(shot), "page": json.loads(last), "error": scrub(out.stderr[-500:]) if out.returncode else ""}


ROWS: dict[int, Any] = {}


def row(number: int):
    def register(fn):
        ROWS[number] = fn
        return fn

    return register


def configure() -> None:
    p2.configure()
    listed = p2.rescan_until("Synth Credits", [MOVIE, OPEN_MOVIE], present=True)
    for sid in p2.ALL_MARKER_SERVERS:
        app_ok("POST", f"/api/servers/{sid}/refresh-libraries")
    say("Synth Credits listed:", listed, "local sources:", app_ok("GET", "/api/markers/sources/local"))


def main(argv: list[str]) -> int:
    # A stopped run (SIGTERM) still runs the finally blocks that recreate the app and put lab state back.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    if not argv or argv[0] not in ("configure", "run", "rows"):
        print(__doc__)
        return 2
    if argv[0] == "configure":
        configure()
        return 0
    if argv[0] == "rows":
        for number, fn in sorted(ROWS.items()):
            print(f"{number:>2}  {(fn.__doc__ or '').strip().splitlines()[0]}")
        return 0
    failed = 0
    for number in [int(a) for a in argv[1:]]:
        if number in OWNER_GATED:
            say(f"row {number} runs on the plex host through plex_rows.py (Task 13 Step 6, Q7): not run here")
            continue
        failed += ROWS[number]()["result"] != "pass"
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

Run: `./phase3_matrix.py rows` (after Step 4 adds rows). Expected: the rows listed with their first docstring line.

- [ ] **Step 4: Rows 1–4 in full**

```python
@row(1)
def row_01_capability() -> dict:
    """Credits text is available in the CPU and the GPU container, and says why when the model is missing."""
    seen: dict[str, dict] = {}
    try:
        for name, gpu, extra in (("cpu", False, ""), ("gpu", True, ""), ("missing model", True, BROKEN_MODEL_ENV)):
            recreate_app(gpu=gpu, extra_env=extra)
            # text_detection_status runs its check on the first ask (up to 30 s), so this call waits for the answer.
            seen[name] = app_ok("GET", "/api/markers/sources/local")["credits_text"]
            if name == "missing model":
                shot = p1.SHOTS / "p3-row01-settings-unavailable.png"
                seen["page"] = screenshot(
                    '        await pg.goto("' + p1.APP + '/settings#markers"); await pg.wait_for_timeout(4000)\n'
                    "        row = pg.locator('li.markers-source[data-id=\"credits_text\"]').first\n"
                    "        print(json.dumps(await row.inner_text()))",
                    shot,
                )
    finally:
        recreate_app(gpu=True)
    premise = {"three containers answered": {"cpu", "gpu", "missing model"} <= seen.keys()}
    missing = seen.get("missing model", {})
    checks = {
        "CPU container: available": seen.get("cpu", {}).get("available") is True,
        "GPU container: available": seen.get("gpu", {}).get("available") is True,
        "missing model: not available": missing.get("available") is False,
        "missing model: the reason names the path": "/nonexistent/model.onnx" in missing.get("message", ""),
        "Settings row shows Not available": "Not available" in str(seen.get("page", {}).get("page")),
    }
    return checks_result(1, "Credit text capability and the Settings row", premise, checks, {"seen": seen})


@row(2)
def row_02_high_then_medium() -> dict:
    """High: credits text alone is Needs review, nothing published. Medium (Q1): published to all five servers, the skip
    ending at the last credit before the scene (Q3) on Plex and Jellyfin; Emby gets the start and says it skips to the
    end of the file (R1)."""
    evidence: dict[str, Any] = {}
    try:
        p2.set_publish_when("high")
        job_high, _ = credits_job("row 2 High", force=True)
        evidence["high"] = {"job": job_high["id"], "evidence": credits_evidence(MOVIE),
                            "decision": p1.item_payload(MOVIE)["decisions"]["credits"]}  # fmt: skip
        evidence["high_published"] = {sid: p2.inspector_server(MOVIE, sid).get("published") for sid in p2.ALL_MARKER_SERVERS}
        p2.set_publish_when("medium")
        job_medium, _ = credits_job("row 2 Medium", force=False)
        evidence["medium"] = {"job": job_medium["id"], "decision": p1.item_payload(MOVIE)["decisions"]["credits"]}
        evidence["servers"] = {sid: p2.inspector_server(MOVIE, sid) for sid in p2.ALL_MARKER_SERVERS}
        evidence["inspector"] = screenshot(
            '        await pg.goto("' + p1.APP + '/bif-viewer")\n'
            "        await pg.wait_for_function(\"() => document.querySelector('#serverSelect option[value=mlab-plex]')\", timeout=20000)\n"
            '        await pg.select_option("#serverSelect", "mlab-plex")\n'
            '        await pg.fill("#searchInput", "Synth Credits (2024)"); await pg.click("#searchBtn")\n'
            "        await pg.locator('.result-item').first.wait_for(timeout=30000)\n"
            "        await pg.locator('.result-item').first.click(); await pg.click('#inspectorMarkersTabBtn')\n"
            "        await pg.wait_for_timeout(3000)\n"
            "        lane = pg.locator('.mk-window[data-window=\"ending\"] .mk-lane[data-lane=\"Credit text\"]').first\n"
            "        print(json.dumps(await lane.inner_text()))",
            p1.SHOTS / "p3-row02-inspector.png",
        )
    finally:
        p2.set_publish_when("high")
    rows = evidence["high"]["evidence"] if "high" in evidence else []
    start = rows[0]["start_ms"] if rows else None
    end = rows[0]["end_ms"] if rows else None

    def mss(ms: int) -> str:
        return f"{ms // 60_000}:{ms // 1000 % 60:02d}"

    def published(sid: str) -> dict | None:
        markers = (evidence.get("servers", {}).get(sid) or {}).get("published") or []
        return next((m for m in markers if m["type"] == "credits"), None)

    premise = {
        "credits text answered within 10 s of 420 s": near_truth(rows),
        "the stored answer ends at the last credit (536–541 s)": end is not None and END_WINDOW_MS[0] <= end <= END_WINDOW_MS[1],
    }
    checks = {
        "High: credits needs review": evidence.get("high", {}).get("decision", {}).get("status") == "needs_review",
        "High: nothing of ours published": not any(evidence.get("high_published", {}).values()),
        "Medium: credits decided with the text's end": (evidence.get("medium", {}).get("decision", {}).get("marker") or {}).get("end_ms") == end,
        **{f"Medium: {sid} published start and end": (m := published(sid)) is not None and abs(m["start_ms"] - start) <= 1_000 and m["end_ms"] == end
           for sid in ("mlab-plex", "mlab-jellyfin", "mlab-jf12")},
        **{f"Medium: {sid} published the start and says it skips to the end": (m := published(sid)) is not None
           and abs(m["start_ms"] - start) <= 1_000
           and "Emby skips to the end of the file" in ((evidence["servers"][sid] or {}).get("publish_message") or "")
           for sid in p2.EMBY_SERVERS},
        "Inspector's Credit text lane shows start–end": start is not None and end is not None
        and any(f"{mss(start + d)}–{mss(end + e)}" in str(evidence.get("inspector", {}).get("page")) for d in (-1000, 0, 1000) for e in (-1000, 0, 1000)),
    }  # fmt: skip
    return checks_result(2, "Synthetic movie with a scene after the credits, at High then Medium", premise, checks, evidence)


@row(3)
def row_03_gpu_on_storage() -> dict:
    """On the P5000: the tail decode runs on CUDA, the helper keeps WebGPU after its self-test, same answer as the CPU."""
    with CpuOnly():  # a known CPU answer (row 1 leaves the app with its GPU; row 2's answer may be from either)
        credits_job("row 3 CPU", force=True)
    cpu_rows = credits_evidence(MOVIE)
    since = now_iso()
    with GpuWorker() as gpu, ProcessSampler() as sampler:
        job, _ = credits_job("row 3 GPU", force=True)
        worker_page = screenshot(
            '        await pg.goto("' + p1.APP + '/"); await pg.wait_for_timeout(2000)\n'
            "        print(json.dumps(await pg.locator('#workerStatusContainer').inner_text()))",
            p1.SHOTS / "p3-row03-workers.png",
        ) if sampler.decodes else {}
    gpu_rows = credits_evidence(MOVIE)
    log = app_log_lines(since, "Credit text detection on")
    premise = {
        "a GPU worker ran the job": job["status"] == "completed" and bool(gpu),
        "a credit-text decode was seen": bool(sampler.decodes),
        "the CPU-only run answered": near_truth(cpu_rows),
    }
    checks = {
        "decode used -hwaccel cuda and scale_cuda": any("-hwaccel cuda" in a and "scale_cuda=320:180" in a for a in sampler.decodes),
        "decode used -threads 2": all(" -threads 2 " in f" {a} " for a in sampler.decodes),
        "a webgpu helper ran": bool(sampler.webgpu_pids()),
        "the self-test kept the GPU": any(": GPU (" in line for line in log),
        "GPU answer within 2 s of the CPU answer": bool(gpu_rows and cpu_rows) and abs(gpu_rows[0]["start_ms"] - cpu_rows[0]["start_ms"]) <= 2_000,
        "GPU end within 2 s of the CPU end": bool(gpu_rows and cpu_rows) and None not in (gpu_rows[0]["end_ms"], cpu_rows[0]["end_ms"])
        and abs(gpu_rows[0]["end_ms"] - cpu_rows[0]["end_ms"]) <= 2_000,
    }  # fmt: skip
    evidence = {"job": job["id"], "decodes": sampler.decodes, "helpers": sampler.helpers, "log": log,
                "cpu": cpu_rows, "gpu": gpu_rows, "workers_page": worker_page}  # fmt: skip
    return checks_result(3, "GPU decode and WebGPU text detection on storage", premise, checks, evidence)


@row(4)
def row_04_gpu_decode_failure() -> dict:
    """AV1 on the P5000 (no AV1 NVDEC): the GPU decode fails, the worker reruns the file on the CPU, the answer lands."""
    target = HOST_FOLDER / STAGED_AV1.name
    since = now_iso()
    evidence: dict[str, Any] = {}
    try:
        shutil.copyfile(STAGED_AV1, target)
        evidence["listed"] = p2.rescan_until("Synth Credits", [AV1_MOVIE], present=True)
        with GpuWorker(), ProcessSampler() as sampler:
            job, _ = credits_job("row 4 AV1", force=True, paths=(AV1_MOVIE,))
        evidence.update(job=job["id"], decodes=sampler.decodes, rows=credits_evidence(AV1_MOVIE),
                        retry_log=app_log_lines(since, "on the GPU and is retrying on CPU"))  # fmt: skip
    finally:
        target.unlink(missing_ok=True)
        evidence["rescan_after_removal"] = p2._rescan_best_effort("Synth Credits", [AV1_MOVIE])
    decodes = evidence.get("decodes", [])
    gpu_first = next((i for i, a in enumerate(decodes) if "-hwaccel cuda" in a), None)
    premise = {"a CUDA decode of the AV1 file was tried": gpu_first is not None}
    checks = {
        "the worker logged the CPU retry": bool(evidence.get("retry_log")),
        "a CPU decode followed": gpu_first is not None and any("-hwaccel" not in a for a in decodes[gpu_first + 1 :]),
        "the answer is within 10 s of 420 s": near_truth(evidence.get("rows", [])),
    }
    return checks_result(4, "GPU decode failure reruns on the CPU", premise, checks, evidence)
```

The selectors are the branch's: the Settings row `li.markers-source[data-id="credits_text"]`, the Inspector lane
`.mk-window[data-window="ending"] .mk-lane[data-lane="Credit text"]` (as `tests/e2e/test_intro_credits_inspector.py`
uses), the dashboard's `#workerStatusContainer`. The lane check looks for the stored start as `m:ss` (±1 s for rounding).
The checks read text, so a wrong selector fails the row rather than passing it.

- [ ] **Step 5: Rows 5–11 and 16 (storage)**

Write each as a function in the style of rows 3–4 (premise first, restore in `finally`, `checks_result`):

| Row | Name | Premise (must hold) | Checks |
|---|---|---|---|
| 5 | Helper crash during a request → CPU for the rest of the run | inside `with GpuWorker(), ProcessSampler()`: a webgpu helper PID exists (run a forced job over `MOVIE` first if none; `sampler.webgpu_pids()`); `docker exec mlab-app kill -STOP <pid>`; start a forced job and wait until a credit-text decode shows (the job's first text request is then blocked on the stopped helper) | `kill -KILL <pid>` then: exactly one "moves to the CPU for the rest of this run of the app" WARNING since the row started; the job completes with `near_truth`; a second forced job starts no new webgpu PID (compare `sampler.webgpu_pids()` before and after). In `finally`, `kill -CONT` then `-KILL` the PID if it is still there, then `recreate_app(gpu=True)`: the device stays on the CPU for the app's lifetime, and rows 6–9 need the GPU helper again. Never `pkill -f` from the host. (Killing a helper while idle would test the between-requests path instead: Task 5 unit-tests that one) |
| 6 | Cancel mid-decode | a credit-text decode appears for a forced job (`ProcessSampler`, up to 60 s) | after `app_ok("POST", f"/api/jobs/{job['id']}/cancel")`: `wait_until` no decode process within 10 s (fresh `ps` via `sh`); job status `cancelled`; the `credits_text` row's `fetched_at` equals the value read before the job |
| 7 | Stored answer reused, a forced run reads again | `credits_evidence(MOVIE)` has one row (row 2 ran) | a normal job: no decode seen, `fetched_at` unchanged; a forced job: a decode seen, `fetched_at` newer |
| 8 | Detection unavailable → a stored answer can't decide | the stored row exists; `recreate_app(gpu=True, extra_env=BROKEN_MODEL_ENV)`; `GET /api/markers/sources/local` says `available: false`; `set_publish_when("medium")` | a normal job leaves `decisions.credits.status != "decided"` and the evidence row still stored; in `finally`: `recreate_app(gpu=True)`, then a normal job decides credits again (checked after the restore, recorded as its own check) and `set_publish_when("high")` |
| 9 | Resources during a GPU job | a forced job on `GpuWorker` with a decode seen and a webgpu helper PID seen | every decode has `-threads 2`; `sampler.peak_helpers <= 2`; the webgpu helper's `Threads:` from `docker exec mlab-app cat /proc/<pid>/status` is recorded in the notes (not a check: ONNX Runtime and Dawn start their own threads; `THREADS = 2` sets only the intra-op pool). No CPU-helper check here: a GPU worker whose self-test kept WebGPU starts no CPU helper |
| 10 | The app reproduces the harness on real movies | 10 movies from Task 11's GPU harness cache that the scale mounts expose, each mapped to the app's path with `container_path()` (skip any that returns None and pick the next); `publish_when` medium; online sources off for the row (restore in `finally`); the forced job runs inside `with GpuWorker()` (the harness cache is a GPU run) | each movie's `credits_text` `start_ms` equals the cache's start within 1 s, and its `end_ms` is None exactly when the cache's end is; results list movies by title only |
| 11 | Phase 1 and 2 regressions on the phase-3 image | `docker inspect mlab-app` image is `MLAB_APP_IMAGE` | `subprocess.run(["./phase2_matrix.py", "run", "1", "3", "8"])` and `["./phase1_matrix.py", "run", "2", "7"]` exit 0 (their own result files carry the detail) |
| 16 | A roll to the end of the file skips to the end (Q3) | a forced job over `OPEN_MOVIE` at `publish_when` medium stores one `credits_text` row within 10 s of 420 s | the stored row's `end_ms` is None; the decision's `marker.end_ms == OPEN_MOVIE_MS` (± 1 s: the probe's duration); Plex and both Jellyfins publish credits ending within 2 s of the end of the file; the Emby rows' `publish_message` has no "Emby skips to the end of the file"; no ffmpeg with `-t 21.000` after the tail decode shows in a `ProcessSampler` over the job (no end window decoded). `set_publish_when("high")` in `finally` |

- [ ] **Step 6: Rows 12–15 on the plex host (owner's OK given for these rows only, Q7)**

**OWNER DECISION (2026-09-16), Q7:** allowed for these four rows only, under the terms below. Before starting, tell the
owner when they run and that row 14 loads the TITAN RTX and the UHD 770 for about 40 s beside prod Plex's transcodes
(preflight M12); run them at a quiet time. One row at a time, in a throwaway container that mounts **no** `/data*`
path, no Plex config and no prod container's volumes, with no access to the prod Plex; nothing is written on plex
outside `/tmp/p3lab`, and the container and image are removed right after:

```bash
cd /home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/lab
trap 'ssh plex "docker rm -f p3lab >/dev/null 2>&1; rm -rf /tmp/p3lab; docker rmi media_preview_generator:intro-credits-p3 >/dev/null 2>&1"' EXIT
docker save media_preview_generator:intro-credits-p3 | ssh plex docker load
ssh plex 'mkdir -p /tmp/p3lab'
scp plex_rows.py "synth/Synth Credits (2024)/Synth Credits (2024).mkv" plex:/tmp/p3lab/
for r in 12 13 14 15; do
  ssh plex "docker run --rm --name p3lab --network none --cpus 2 --runtime=nvidia -e NVIDIA_VISIBLE_DEVICES=all -e NVIDIA_DRIVER_CAPABILITIES=all \
    --device /dev/dri:/dev/dri -v /tmp/p3lab:/work --entrypoint python3 media_preview_generator:intro-credits-p3 \
    /work/plex_rows.py $r" | tee "results/p3-row-$r.json"
done
# the EXIT trap removes the container, /tmp/p3lab and the image, also when a row fails or the loop is interrupted
```

Row 14 needs the host's view of the GPUs while the Intel helper self-tests: `plex_rows.py 14` repeats row 13's
self-test for 40 s; while it runs, in a second shell, `ssh plex 'docker top p3lab -eo pid,args; nvidia-smi pmon -c 5
-s u; timeout 5 intel_gpu_top -J -s 500 | head -60'` (pmon lists Vulkan "G" processes as well as compute ones). Paste
the output into the row's notes.

`plex_rows.py` (created here, stdlib + the image's app package only):

```python
#!/usr/bin/env python3
"""Phase 3 rows 12–15, run inside a throwaway app container on the plex host (plan-phase3 Task 13 Step 6).

    python3 plex_rows.py 12|13|14|15     prints one JSON result

Only the synthetic movie in /work is read; nothing is written outside the container.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from loguru import logger

from media_preview_generator.markers.credits.detector import find_credits
from media_preview_generator.markers.credits.textdet import synthetic_frames
from media_preview_generator.markers.credits.textdet_helper import TextDetectorPool

MOVIE = "/work/Synth Credits (2024).mkv"
LOG: list[str] = []
logger.add(lambda message: LOG.append(str(message).strip()), level="INFO")


def intel_render_node() -> str | None:
    """The Intel GPU's render node, by its sysfs vendor id (the host's node numbers aren't assumed)."""
    for vendor in sorted(Path("/sys/class/drm").glob("renderD*/device/vendor")):
        if vendor.read_text().strip() == "0x8086":
            return f"/dev/dri/{vendor.parent.parent.name}"
    return None


DEVICES = {"NVIDIA": "cuda:0", "INTEL": intel_render_node()}


def counts_row(vendor: str) -> dict:
    pool = TextDetectorPool()
    frames = synthetic_frames(20)
    try:
        started = time.monotonic()
        gpu_counts = pool.count_boxes(frames, gpu=vendor, gpu_device_path=DEVICES[vendor])
        backend = pool.backend_of(vendor, DEVICES[vendor])
        cpu_counts = pool.count_boxes(frames, gpu=None, gpu_device_path=None)
        return {"vendor": vendor, "backend": backend, "same_counts": gpu_counts == cpu_counts,
                "seconds": round(time.monotonic() - started, 1),
                "log": [line for line in LOG if "Credit text detection on" in line]}  # fmt: skip
    finally:
        pool.close_all()


def vaapi_row() -> dict:
    pool = TextDetectorPool()
    try:
        common = {"duration_ms": 600_000, "is_episode": False, "ffmpeg": "ffmpeg"}
        cpu = find_credits(MOVIE, count_boxes=lambda p: pool.count_boxes(p, gpu=None, gpu_device_path=None),
                           gpu=None, gpu_device_path=None, **common)  # fmt: skip
        intel = find_credits(
            MOVIE, count_boxes=lambda p: pool.count_boxes(p, gpu="INTEL", gpu_device_path=DEVICES["INTEL"]),
            gpu="INTEL", gpu_device_path=DEVICES["INTEL"], **common,
        )  # fmt: skip
        return {"cpu_start_s": cpu.start_s, "intel_start_s": intel.start_s, "cpu_end_s": cpu.end_s, "intel_end_s": intel.end_s,
                "within_2s": cpu.start_s is not None and intel.start_s is not None and abs(cpu.start_s - intel.start_s) <= 2
                and cpu.end_s is not None and intel.end_s is not None and abs(cpu.end_s - intel.end_s) <= 2}  # fmt: skip
    finally:
        pool.close_all()


def main(row: str) -> int:
    premise = {"an NVIDIA GPU is visible": Path("/dev/nvidiactl").exists(), "an Intel render node was found": DEVICES["INTEL"] is not None}
    if row != "12" and not premise["an Intel render node was found"]:
        print(json.dumps({"row": int(row), "premise": premise, "pass": False}, indent=1))
        return 1
    if row == "12":
        result = counts_row("NVIDIA")
        result["pass"] = premise["an NVIDIA GPU is visible"] and result["backend"] == "webgpu" and result["same_counts"]
    elif row == "13":
        result = counts_row("INTEL")
        result["pass"] = result["backend"] == "cpu" and any("slower than the CPU" in line for line in result["log"])
    elif row == "14":
        runs, deadline = [], time.monotonic() + 40
        while time.monotonic() < deadline:  # a new pool per run: every run starts a helper and self-tests again
            runs.append(counts_row("INTEL")["backend"])
        # Judged from the host's pmon / intel_gpu_top output pasted into the notes, not here.
        result = {"runs": runs, "pass": None}
    elif row == "15":
        result = vaapi_row()
        result["pass"] = result["within_2s"]
    else:
        print(__doc__)
        return 2
    print(json.dumps({"row": int(row), "premise": premise, "intel_node": DEVICES["INTEL"], **result}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
```

(`CreditsTextResult.start_s` / `end_s` are Task 8's; `backend_of` returns `"webgpu"`, `"cpu"` or None, Task 5.) Row 12
also needs the premise "an NVIDIA GPU is visible"; a row whose premise is false is a failure, not a skip. Row 14 passes
when no PID from `docker top p3lab` shows in `nvidia-smi pmon` and `intel_gpu_top` shows render work during the
self-tests; if a p3lab PID shows on the TITAN RTX, T-R3's cost happened: record it, and the spec §13 note stays with
the measured fact.

- [ ] **Step 7: Run, write `phase3-results.md`, commit**

```bash
cd /home/data/workspace/plex_generate_vid_previews
VER=$(/home/data/.venv/bin/python -m setuptools_scm)
mkdir -p "$MARKERS_BENCH_DIR/logs"
nice -n 19 docker build --build-arg SETUPTOOLS_SCM_PRETEND_VERSION="$VER" -t media_preview_generator:intro-credits-p3 . > "$MARKERS_BENCH_DIR/logs/build-p3-lab.log" 2>&1
cd docs/design/intro-credits/evidence/lab
./synth_credits.sh
docker rm -f mlab-plex mlab-jellyfin mlab-jf12 mlab-emby mlab-emby49 && ./up.sh   # new mount; volumes (claim, keys) kept
export MLAB_APP_IMAGE=media_preview_generator:intro-credits-p3
MLAB_APP_GPU=nvidia ./app.sh recreate
./phase3_matrix.py configure
./phase3_matrix.py run 1 2 3 4 5 6 7 8 9 10 11 16
```

Expected: 12 of 12 storage rows pass (rows 2–9 build on row 2's stored answer, so keep this order). If a row fails,
investigate (systematic debugging), fix in the owning task's files with its tests, rebuild, rerun that row and the
rows after it. `phase3-results.md`: the image digest, the order run, one line per row (premise, checks, evidence
pointers, screenshots under `MLAB_SHOTS`), rows 12–15 as "pending owner (Q7)" or their results, how to reset the lab
(`docker rm -f` the servers and `./up.sh`; `./app.sh recreate`; delete `results/p3-row-*.json`). Results JSON and
`synth/` stay git-ignored.

```bash
cd /home/data/workspace/plex_generate_vid_previews
git add docs/design/intro-credits/evidence/lab/{synth_credits.sh,phase3_matrix.py,plex_rows.py,phase3-results.md,app.sh,up.sh} \
        docs/design/intro-credits/evidence/README.md
git diff --cached --stat
grep -nE '/data_|\{(tmdb|tvdb|imdb)-' docs/design/intro-credits/evidence/lab/{phase3_matrix.py,plex_rows.py,phase3-results.md}   # nothing
```

Architecture Review on the staged diff; then
`PATH="/home/data/.venv/bin:$PATH" git commit -m "test(markers): phase 3 lab matrix for credit text"`.

---
## Task 14: PR, image, close-out

`[sequential]` (after Task 13) — spec §11 (branch, `build-docker` label, nothing merges until the owner says fully
tested), §10.2 (results pasted in the PR), roadmap owner checkpoints, memory "gh pr edit is broken here" (use REST),
preflight M15 (lab commands run from the lab folder).

- [ ] **Step 1: Branch up to date and full suites**

```bash
cd /home/data/workspace/plex_generate_vid_previews
git fetch origin
git merge-base --is-ancestor origin/dev HEAD || git merge origin/dev   # keep both sides' intent; Architecture Review on the merge
/home/data/.venv/bin/python -m pytest
/home/data/.venv/bin/python -m pytest -m e2e -n 8 --no-cov
/home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/markers_eval
MEDIA_PREVIEW_TEXTDET_MODEL="$MARKERS_BENCH_DIR/textdet-model/ch_PP-OCRv4_det_infer.onnx" nice -n 19 /home/data/.venv/bin/python -m pytest --no-cov -n 0 -m "integration" tests/markers/credits tests/test_processing_hwaccel.py -q
```

Expected: all PASS; note the counts and coverage.

- [ ] **Step 2: PR body (REST)**

Write `.superpowers/sdd/plan-phase3/pr241.md` (git-excluded) from the current body plus: what users get in phase 3 (credit text for files without
chapters or online answers, the skip stopping before a scene after the credits, GPU on any vendor with a self-test,
CPU fallback), phase status (1–3 done; 4 pending), the harness tables from `phase3-harness.md` (rule J alone on the
80, GPU and CPU; the gate per set check by check with the owner's caps; ends found and scenes kept; the frame-check
adjudication counts), the lab table from `phase3-results.md`, the owner's answers to Q1–Q8 (and any ruling on a failed
gate check), test counts, known limitations (AMD untested; DV profile 5 unmeasured if so; epilogue cards touching the
roll become the start; Dawn's device choice on two-GPU hosts if row 14 found a problem; arm64 CPU only; Emby skips to
the end of the file), how to try `pr-241`.
No paths, tokens or email addresses. End with the attribution lines from the session's system reminder.

```bash
gh api -X PATCH repos/stevezau/media_preview_generator/pulls/241 -f body="$(cat .superpowers/sdd/plan-phase3/pr241.md)"
```

- [ ] **Step 3: PR image**

```bash
gh run list --branch feat/markers-detection --limit 5    # the build-docker label is already on PR 241
docker pull ghcr.io/stevezau/media_preview_generator:pr-241
IMG=ghcr.io/stevezau/media_preview_generator:pr-241
docker run --rm --entrypoint python3 "$IMG" -m media_preview_generator.markers.credits.textdet_helper --check; echo "check exit $?"
docker run --rm --entrypoint sha256sum "$IMG" /app/models/ch_PP-OCRv4_det_infer.onnx
gh run list --workflow ci.yml --branch feat/markers-detection --limit 1   # the arm64 platform build is green
```

Expected: `check exit 0`, the pinned sha256, CI green including arm64. Record the digest. Then, from the lab folder:

```bash
cd /home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/lab
MLAB_APP_IMAGE=$IMG MLAB_APP_GPU=nvidia ./app.sh recreate
MLAB_APP_IMAGE=$IMG ./phase3_matrix.py run 1 2 3 16
```

Paste the four results under "PR image check" in `phase3-results.md`.

- [ ] **Step 4: Owner checkpoints (ask, don't assume)**

One short message: the results of the plex rows 12–15 (run under Q7's terms) and anything they found; any failed gate
check still waiting for a ruling; the frame-check adjudications that surprised (epilogue cards, ends inside a scene);
whether the phase-3 PR image may run as the side-by-side container on `plex` (roadmap checkpoint 2: Q7 covers rows
12–15 only, so this needs its own yes); the Emby catalog submission stays the owner's at the very end (owner,
2026-09-16). No releases without the word "release".

- [ ] **Step 5: Close-out**

- spec §0 Status: phase 3 lab-proven, the image digest, next: owner review, then phase 4; §12 phase 3 "done when" with
  the evidence; §14 dated lines for anything ruled in Step 4.
- `plan-roadmap.md`: tick the phase-3 bullets that shipped (mark the analytic unclip superseded by T-R2).
- `.superpowers/sdd/plan-phase3/progress.md`: final line with counts, digest, open owner items.
- Architecture Review; commit `docs(intro-credits): phase 3 close-out`; push.

Nothing merges into `dev`: the PR stays draft until the owner says the feature is fully tested.

---
## Parked items and lessons carried in

**Parked items marked "phase 3" in the phase-1 and phase-2 ledgers and plans:**
- Phase-2 plan, L165: anime "Ending" chapters not counted as credits, and files with two "End Credits" chapters (the
  rules pick the last). Not in phase 3: they are the chapter source's title rules, not credits text; phase 2 Task 15
  reports them (16 "Ending" titles in four anime seasons on the lab scale run). Credits text covers those files'
  credits when chapters don't decide. Proposed as a phase-4 chapter-rules item with its own harness rows.
- Phase-1 audit A LOW-1 (per-episode vendor webhook follow-ups not season-grouped, "parked for the phase 3 season
  step"): done in phase 2 Task 8.
- Phase-2 ledger "phase 3 plan when owner wants": this plan (owner, 2026-09-16: "plan and build now").

**Lessons from the phase-2 audits this plan applies:**
- A check that can time out must not flip a source off for the process: text detection is three-state like chromaprint
  (Task 5; ledger Lane D MED "has_chromaprint caches False after timeout").
- Unavailable detectors' stored answers may hold a type in review but never help decide it (Task 8; ledger Lane D
  IMPORTANT D6).
- No unbounded work on a thread that holds a slot or a request: decodes have a hard timeout and a kill; helper requests
  have timeouts; the availability check has a 30 s timeout (Tasks 5, 6; ledger Lane D HIGH "sweep stats run holding
  job-gate slot").
- Tests assert the argv and kwargs the code controls and every branch cell; mutation checks are part of each
  high-risk task's steps (ledger: Tasks 3, 7, D-lane reverts).
- Lab rows assert their premise and restore in `finally`, also on SIGTERM (Task 13; ledger Task 17 Architecture Review).
- The milestone audit is a task gate, not optional in speed mode (ledger: "after-Task-12 milestone audit was not run").
- Tokens, emails and real paths never reach committed files (ledger Task 5 cassette scrub): fixtures anonymised,
  harness summaries names-only (Tasks 3, 11).

**Lessons from the phase-3 pre-flight (`.superpowers/sdd/plan-phase3/preflight.md`) this revision applies:**
- A time limit bounds nothing if the thread then blocks closing a pipe a stuck process holds: kill the process group
  with a bounded wait and hand the handles to a daemon reaper (I1; Tasks 5, 6).
- A bounded queue's end-of-stream marker is put with the same unbounded retry as the data, or slow consumers turn
  finished work into timeouts (C2; Task 6).
- A file that timed out waits a day instead of costing a worker 600 s every run (phase-2 MED-1's shape; Task 8).
- Handing files between lanes through a session scratchpad fails for lanes, subagents and `/clear`: stable git-ignored
  paths only (I3).
- A gate relative to a weak baseline isn't precision: absolute caps per set as well (I5; Task 11).
- A harness row the product can't produce misleads: rows mirror when the pipeline asks a detector (I6; Task 11).
- Not applied (preflight M13, no clear single home): `hdr_kind` stays in both Task 1's evidence script and Task 11's
  harness, and `phase3_matrix.write_result/checks_result` stay separate from phase 2's (they need a premise and their
  own file prefix).
