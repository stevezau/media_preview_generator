# Phase 3 pre-build measurements (credits text)

Task 1 of `docs/design/intro-credits/plan-phase3.md`. Host: storage (Quadro P5000 at `0000:02:00.0`, NVIDIA driver
580.178.04, 20 CPUs, host ffmpeg 8.0.1; `bench/host.txt`) unless a row says otherwise. Date: 2026-09-16. Counts and
timings only; the truth sets (`credits/{movies40,tv40,movie_credit_truth,adjudicated}.json`, `credits/f3.jsonl`) are
local-only.

Scripts live in `credits/phase3/`. Raw outputs are in `$MARKERS_BENCH_DIR`
(`docs/design/intro-credits/evidence/credits/bench/`, git-ignored), beside the Python 3.12 venv (`py312/`) and the
model (`textdet-model/`) that Tasks 4, 5, 7 and 11 read.

```bash
# From the repository root.
export MARKERS_BENCH_DIR="$PWD/docs/design/intro-credits/evidence/credits/bench"
export PYTHON=/path/to/dev-venv/bin/python   # the shared dev venv (Python 3.14, dev extras); M1's venv is separate
```

GPU sharing: during M3 and M4 another CUDA process (2006 MiB, 0 % utilisation when sampled; `bench/host.txt`) held
memory on the P5000.

## M0 planning numbers

The plan's "Measured while planning" section, re-run where a command reproduces it. Every re-run matched. Outputs:
`bench/m0-rule-j.txt`, `bench/m0-combinations.txt`.

Inputs: `credits/f3.jsonl`, `credits/adjudicated.json` (three frame-checked truth corrections) and
`lab/results/scale/prod_plex_{markers,parts}.json`. `eval_rules3.load()` silently falls back to the chapter truth when
`adjudicated.json` is missing; rule J then reads 57 within 10 s / 2 early instead of 59 / 1 (observed once, output not
saved: Task 1 had not linked the file into its worktree for that run).

Commands:

```bash
(cd docs/design/intro-credits/evidence && nice -n 19 "$PYTHON" credits/rule_j.py)
nice -n 19 "$PYTHON" docs/design/intro-credits/evidence/credits/phase3/measure_combinations.py
```

`rule_j.py` (refine span 10 s, keyframes of the tail): `n 80, ok5 51, ok10 59, ok30 66, early 1, late 9, none 4`.

`measure_combinations.py`:

| Refine span | Within 10 s | Early 10–30 s | Early > 30 s | Late > 30 s | None |
|---|---|---|---|---|---|
| 10 s | 59 | 2 | 1 | 9 | 4 |
| 15 s | 59 | 2 | 1 | 8 | 4 |
| 20 s | 59 | 2 | 1 | 8 | 4 |

| Row (80 files, span 20 s, through `decide()`) | Useful | Late | Wrong | Missed |
|---|---|---|---|---|
| Plex's first credits marker | 47 | 1 | 13 | 19 |
| Credits text alone | 65 | 8 | 3 | 4 |
| Credits text + Plex markers, High | 39 | 0 | 1 | 40 |
| Credits text + Plex markers, Medium (text may decide alone) | 56 | 0 | 1 | 23 |
| Credits text + Plex markers, Medium (text never alone) | 39 | 0 | 1 | 40 |

Last credit run end to the end of the file: 72 of 76 files with a run within 30 s, max 171 s.

Keyframe rows of `f3.jsonl` (a one-off read of the `key` rows, output in `bench/m0-keyframe-rows.txt`): median 231.5
rows per file (max 682), median keyframe gap 2.0 s, largest 83 s; 8 of 80 files have a row whose pts is lower than the
row before it (T-R5).

Copied from the plan without a re-run in Task 1 (their sources are named in the plan):

- **Rule J reproduction.** Keyframes of the tail 59 within 10 s / 66 within 30 s / 1 early / 9 late / 4 none of 80
  (refine span 10 s). With a refine span of 15 s or 20 s (spec §5.4 says 20 s): 59 / 1 early / 8 late / 4 none. Two TV
  answers start 10–30 s early (−21 s, −16 s); the one early movie is −34 s.
- **The one wrong at High** is a TV episode whose credits run 23 s: text −21 s and Plex −30 s agree.
- **Credits end.** Median gap from the last credit run to the end of the file 4.5 s. Under Q3 the 4 files beyond 30 s
  get an end: two with a right start keep 31 s and 42 s after the roll, one TV answer 13 s late keeps 59 s, and one
  wrong run (243 s late) keeps 171 s. On a generated movie (420 s gradients, 120 s of scrolling names, 60 s scene) the
  app's detector gave start 421.0 s and end 539.0 s (the last credit keyframe was 537.7 s); without the scene, end None.
- **Epilogue cards** (prototype `detect` and the port agree): 10 s of white-on-black cards touching the roll, or joined
  to it over 30 s of black, become the start (90 s instead of 100 s / 130 s); split from the roll by a 30 s lit scene
  they don't.
- **Preflight reproductions** (plan code): `frames.run_decode` with a 64-frame chunk taking 1.15 s lost its end marker
  on tails of 192, 256 and 320 keyframes and reported a 600 s timeout; a grandchild holding stdout kept
  `run_decode(timeout_s=2)` for 25 s; a GPU helper that exited between requests was restarted on the GPU silently.
- **Q4 gate on these numbers** (80 files, caps rounded up): Medium useful 56 ≥ Plex 47, Medium wrong 1 ≤ cap 2 and
  ≤ Plex 13, High wrong 1 ≤ cap 1 and ≤ Plex 13 → pass.
- **Tail lengths** (lab scale run chapter truth): TV credits (400 episodes) median 72 s, p95 267 s, 390 within 450 s
  (the 10 beyond are 462–463 s and chapter mislabels at 1,365–2,578 s); movies (102) p95 563 s, max 852 s, all within
  900 s.
- **rapidocr 1.4.4 pre-processing:** with `limit_type="max"` `TextDetector.get_preprocess` raises the limit to 960 for
  frames under 960 px, so a 320×180 frame keeps ratio 1.0 and is resized to 320×192. Session options:
  `log_severity_level=4`, `enable_cpu_mem_arena=False`, `ORT_ENABLE_ALL`, CPU provider
  `arena_extend_strategy=kSameAsRequested`.
- **Bench set:** `credits/gpu/extract.py` = `f3.jsonl[::10]` (8 files), CPU keyframes of the 120 s around the truth,
  `scale=320:180,format=gray` → 289 frames.
- **Storage GPU:** `nvidia-smi --query-gpu=index,name,pci.bus_id` → `0, Quadro P5000, 00000000:02:00.0`. Host ffmpeg
  has `libsvtav1`; Pascal has no AV1 NVDEC.
- **Planning dry runs:** Task 4's detector against rapidocr's verbatim post-processing (0 of 2,000 maps differ;
  area/length equal shapely on 20,000 boxes; 17.2 ms/frame on the CPU); Task 5's real-model integration on the storage
  host picked the P5000 by `pci_bus_id` and its self-test on 20 synthetic frames gave GPU 16.9 vs CPU 18.7 ms per frame
  (see M3: the adapter was the P5000 because it is the only hardware Vulkan device here, not because of the device
  picked).

## M1 packages

Command (wave 0, run by the controller before the lanes; not re-run in Task 1):

```bash
nice -n 19 docs/design/intro-credits/evidence/credits/phase3/measure_packages.sh "$MARKERS_BENCH_DIR/py312"
```

Result: Python 3.12.13 venv, `1.30.0 5.0.0` (onnxruntime, cv2). Installed: onnxruntime 1.30.0,
onnxruntime-ep-webgpu 0.3.0, opencv-python-headless 5.0.0.93, pyclipper 1.4.0, numpy 2.5.3, shapely 2.1.2, plus
flatbuffers 25.12.19, protobuf 7.36.1, packaging 26.3 (onnxruntime's dependencies), six, PyYAML, Pillow, tqdm, and
rapidocr_onnxruntime 1.4.4 without dependencies.

Wheels on PyPI (Linux, cp312 / abi3 / py3-none; `bench/m1-wheels.txt`, the committed script's filter re-run):

| Package | x86_64 wheel | aarch64 wheel |
|---|---|---|
| onnxruntime 1.30.0 | 23.6 MB (`manylinux_2_28`) | 21.3 MB |
| onnxruntime-ep-webgpu 0.3.0 | 6.6 MB (`py3-none-manylinux_2_28`) | none (x86_64 only) |
| opencv-python-headless 5.0.0.93 | 61.2 MB (`manylinux_2_28`), 56.6 MB (`manylinux2014`) | 39.6 MB / 36.5 MB |
| pyclipper 1.4.0 | 1.0 MB | 1.0 MB |

Wave 0's script filtered on `cp312`/`abi3` and skipped the EP's `py3-none` wheel; the committed script matches
`py3-none` too, and its re-run lists the EP's 6.6 MB wheel.

Installed size, `du -sb` in Task 1 (`bench/m1-du-bytes.txt`); every MB in this document is 10^6 bytes. (Wave 0's
`du -sm` lines in `bench/wave0-m1-packages.txt` are rounded-up MiB and are not used here.)

| Directory | MB |
|---|---|
| onnxruntime | 63.9 |
| onnxruntime_ep_webgpu | 15.8 |
| cv2 | 74.5 |
| opencv_python_headless.libs | 84.7 |
| pyclipper | 3.5 |
| numpy | 30.8 |
| numpy.libs | 27.5 |
| shapely | 5.3 |
| shapely.libs | 5.8 |
| google (protobuf) | 1.4 |
| flatbuffers | 0.1 |
| packaging | 0.5 |

- **What the image adds** (onnxruntime + onnxruntime_ep_webgpu + cv2 + opencv_python_headless.libs + pyclipper;
  numpy 2.5.3 is already in the lab image, `bench/m1-image-baseline.txt`): installed **242.5 MB**, plus protobuf
  1.4 MB and flatbuffers 0.1 MB that the lab image doesn't have (packaging 26.3 is there).
- **x86_64 wheels of those packages:** 23.6 + 6.6 + 61.2 + 1.0 = **92.4 MB**.
- Installed + wheels: 242.5 + 92.4 ≈ **335 MB**, over Task 7's +250 MB when the builder's wheels stay in their own
  image layer; the installed size alone (244.0 MB with protobuf and flatbuffers) is within it.

## M2 model

Command (wave 0, run by the controller; not re-run): the Step 3 snippet of the plan with
`$MARKERS_BENCH_DIR/textdet-model`.

- URL: `https://files.pythonhosted.org/packages/ba/12/1e5497183bdbe782dbb91bad1d0d2297dba4d2831b2652657f7517bfc6df/rapidocr_onnxruntime-1.4.4-py3-none-any.whl`
- Wheel: 14,915,192 bytes, sha256 `971d7d5f223a7a808662229df1ef69893809d8457d834e6373d3854bc1782cbf`
- Model `rapidocr_onnxruntime/models/ch_PP-OCRv4_det_infer.onnx`: 4,745,517 bytes, sha256
  `d2a7720d45a54257208b1e13e36a8479894cb74155a5efe29462512d42f49da9`
- Both match the plan. Task 1 re-hashed the files on disk with `sha256sum` (`bench/m2-sha256.txt`): same values and
  sizes.
- Licence files in the wheel: **none** (`[]`). Task 4's notice cites the upstream licences instead.

## M3 WebGPU devices

Inside the lab app image `media_preview_generator:intro-credits` (Python 3.12.3, ffmpeg 8.1.2,
`bench/m1-image-baseline.txt`; libvulkan1 1.3.275, mesa-vulkan-drivers 25.2.8, `bench/m3-mesa-device-select.txt`), the
M1 venv's site-packages mounted at `/pkgs`.

```bash
cd docs/design/intro-credits/evidence/credits/phase3
B="$MARKERS_BENCH_DIR"
SITE="$("$B/py312/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
for gpu in with without; do
  extra=(); [[ $gpu == with ]] && extra=(--runtime=nvidia -e NVIDIA_VISIBLE_DEVICES=all -e NVIDIA_DRIVER_CAPABILITIES=all --device /dev/dri)
  nice -n 19 docker run --rm "${extra[@]}" -e USE_APP_VULKAN_PROBE=1 -e PYTHONPATH=/pkgs:/app \
    -v "$SITE":/pkgs:ro -v "$PWD":/work:ro -v "$B/textdet-model":/model:ro \
    --entrypoint python3 media_preview_generator:intro-credits /work/measure_webgpu_devices.py /model/ch_PP-OCRv4_det_infer.onnx \
    > "$B/m3-$gpu.txt" 2>&1 || true
done
# Third case, no Vulkan driver at all (ORT PR #29591's "no adapters"):
nice -n 19 docker run --rm -e VK_DRIVER_FILES=/nonexistent.json -e VK_ICD_FILENAMES=/nonexistent.json \
  -e USE_APP_VULKAN_PROBE=1 -e PYTHONPATH=/pkgs:/app \
  -v "$SITE":/pkgs:ro -v "$PWD":/work:ro -v "$B/textdet-model":/model:ro \
  --entrypoint python3 media_preview_generator:intro-credits /work/measure_webgpu_devices.py /model/ch_PP-OCRv4_det_infer.onnx \
  > "$B/m3-noadapter.txt" 2>&1 || true
```

Raw outputs: `bench/m3-with.txt`, `bench/m3-without.txt`, `bench/m3-noadapter.txt`. Those runs predate the provider
name on each "ok" line; `bench/m3-with-providers.txt` is a re-run of the current script with the NVIDIA runtime (same
device list, exit 0.1 s) and names the provider per line.

### Device list

The same two devices in all three cases (with the NVIDIA runtime, without it, and with no Vulkan driver), identical to
the host venv's list (`bench/m3-host-devices.txt`): consistent with sysfs enumeration rather than Vulkan.

```json
[
 {"type": "OrtHardwareDeviceType.GPU", "vendor": "", "vendor_id": "0x10de", "device_id": "0x1bb0",
  "metadata": {"Discrete": "1", "card_idx": "0", "pci_bus_id": "0000:02:00.0"},
  "ep_metadata": {"library_path": "/pkgs/onnxruntime_ep_webgpu/libonnxruntime_providers_webgpu.so", "version": "0.3.0"},
  "ep_options": {}},
 {"type": "OrtHardwareDeviceType.GPU", "vendor": "", "vendor_id": "0x1a03", "device_id": "0x2000",
  "metadata": {"card_idx": "1", "pci_bus_id": "0000:07:00.0"},
  "ep_metadata": {"library_path": "/pkgs/onnxruntime_ep_webgpu/libonnxruntime_providers_webgpu.so", "version": "0.3.0"},
  "ep_options": {}}
]
```

- PCI metadata key: **`pci_bus_id`**, format `0000:02:00.0` (4-digit domain, lower-case hex). `vendor` is empty.
- App Vulkan probe inside the image: with the NVIDIA runtime `Quadro P5000 (discrete)` via
  `__EGL_VENDOR_LIBRARY_FILENAMES=/usr/share/glvnd/egl_vendor.d/10_nvidia.json`; without it llvmpipe
  (`is_software=True`, no overrides); with no Vulkan driver `device=None`.

### Provider options (20-run average per session, the first session of a process includes shader compile)

| Device, options | NVIDIA runtime + probe env | No GPU passed (llvmpipe) | No Vulkan driver |
|---|---|---|---|
| 0 (P5000), `{}` | ok 74.6 ms (first session) | ok 1095.5 ms (first session) | EP fails → CPU 14.7 ms |
| 0, `powerPreference: high-performance` | ok 10.4 ms | ok 247.9 ms | EP fails → CPU 14.9 ms |
| 0, `deviceId: "0"` | ok 11.0 ms | ok 207.3 ms | EP fails → CPU 16.1 ms |
| 1 (ASPEED BMC VGA), `{}` | ok **10.7 ms** | ok 196.8 ms | EP fails → CPU 15.8 ms |
| 1, `powerPreference: high-performance` | ok 10.8 ms | ok 232.5 ms | EP fails → CPU 15.9 ms |
| 1, `deviceId: "1"` | **EP fails → CPU 17.9 ms** | EP fails → CPU 15.3 ms | EP fails → CPU 14.7 ms |

EP failure messages (ORT prints `EP Error … Falling back to ['CPUExecutionProvider'] and retrying.` and the
`InferenceSession` is created on the CPU without raising). The "EP fails → CPU" cells come from those messages; in
`bench/m3-with-providers.txt` the current script names the provider per line: `WebGpuExecutionProvider` on the five
other rows, `CPUExecutionProvider` on device 1 with `deviceId: "1"`.

- `deviceId` ≥ 1: `webgpu_context.cc:1207 … WebGPU EP custom context (contextId>0) must have custom WebGPU instance and
  device.` `deviceId` is a WebGPU context id, not an adapter index.
- No Vulkan driver: `webgpu_context.cc:101 … Failed to get a WebGPU adapter: No supported adapters`.

Option names the plugin library carries (`strings`, prefix `ep.webgpuexecutionprovider.`; `bench/m3-ep-options.txt`):
`dawnBackendType`,
`dawnProcTable`, `defaultBufferCacheMode`, `deviceId`, `enableGraphCapture`, `enableInt64`, `enablePIXCapture`,
`enableRobustness`, `forceCpuNodeNames`, `kvCacheQuantizationBits`, `maxNumPendingDispatches`,
`maxStorageBufferBindingSize`, `multiRotaryCacheConcatOffset`, `powerPreference`, `preferredLayout`, `preserveDevice`,
`queryResolveBufferCacheMode`, `sessionBufferPoolGenerations`, `storageBufferCacheMode`, `uniformBufferCacheMode`,
`validationMode`, `webgpuDevice`, `webgpuInstance`.

### Which physical adapter runs the session

The ASPEED row at P5000 speed prompted a check of the adapter each session actually uses: a one-off script
(`adapter_check.py`, kept out of the repo) created a session on one listed device, ran 5 warm-up and 50 timed runs,
printed `session.get_providers()`, and held the session open while the host read `nvidia-smi`'s process table
(condensed below; run in the same image with the same mounts, one container per row). Outputs:
`bench/m3-adapter-check.txt` (rows without `MESA_VK_DEVICE_SELECT`, and the first `10005:0` / `10de:1bb0` rows) and
`bench/m3-mesa-device-select.txt` (the `list` output and a re-run of the device-select rows on EP device 1).

```python
import json, sys, time
import numpy as np, onnxruntime as ort, onnxruntime_ep_webgpu as webgpu_ep
model, index, options = sys.argv[1], int(sys.argv[2]), json.loads(sys.argv[3])
ort.register_execution_provider_library("webgpu", webgpu_ep.get_library_path())
devices = [d for d in ort.get_ep_devices() if d.ep_name == webgpu_ep.get_ep_name()]
opts = ort.SessionOptions(); opts.intra_op_num_threads = 2
opts.add_provider_for_devices([devices[index]], options)
session = ort.InferenceSession(model, sess_options=opts)
feed = {session.get_inputs()[0].name: np.zeros((1, 3, 192, 320), dtype=np.float32)}
for _ in range(5): session.run(None, feed)
started = time.perf_counter()
for _ in range(50): session.run(None, feed)
print(session.get_providers(), round(1000 * (time.perf_counter() - started) / 50, 1))
print("HOLD", flush=True); time.sleep(10)
```

| Container env | Device | Providers | ms/frame | On the P5000 (`nvidia-smi`) |
|---|---|---|---|---|
| NVIDIA runtime + `__EGL_VENDOR_LIBRARY_FILENAMES` override | 0 (P5000) | WebGPU, CPU | 10.6 | yes, `python3` C+G 34 MiB |
| same | **1 (ASPEED)** | WebGPU, CPU | **12.4** | **yes**, C+G 34 MiB |
| NVIDIA runtime, **no** EGL override | 0 (P5000) | WebGPU, CPU | **215.9** | no (llvmpipe) |
| NVIDIA runtime + override, `powerPreference: low-power` | 0 | WebGPU, CPU | 10.0 | yes |
| same + `MESA_VK_DEVICE_SELECT=10005:0`, `MESA_VK_DEVICE_SELECT_FORCE_DEFAULT_DEVICE=1` | 0 (P5000) | WebGPU, CPU | **202.1** | no compute (G 1 MiB) |
| same + `MESA_VK_DEVICE_SELECT=10de:1bb0`, forced | 1 (ASPEED) | WebGPU, CPU | 9.2 | yes |
| same + `MESA_VK_DEVICE_SELECT=10005:0`, forced (re-run) | 1 (ASPEED) | WebGPU, CPU | 181.7 | — |
| same + `MESA_VK_DEVICE_SELECT=pci:0000:02:00.0`, forced (re-run) | 1 | WebGPU, CPU | 11.1 | — |
| same + `MESA_VK_DEVICE_SELECT=pci:0000:00:00.0`, forced (re-run) | 1 | WebGPU, CPU | 12.7 | — |
| no GPU passed | 0 (P5000) | WebGPU, CPU | 221.8 | no (llvmpipe) |
| no GPU passed, no Vulkan driver | 0 | **CPU only** | 16.6 | no |

`MESA_VK_DEVICE_SELECT=list` with the NVIDIA runtime lists `GPU 0: 10de:1bb0 "Quadro P5000" discrete GPU 0000:02:00.0`
and llvmpipe twice (`10005:0`, `0000:00:00.0`); the ASPEED BMC has no Vulkan device.

Findings for Task 5:

- **The EP device chosen doesn't choose the adapter.** A session on the ASPEED's EP device runs on the P5000. The
  plugin's README says so: "The WebGPU EP currently accepts one EP device and selects the physical GPU independently."
  `powerPreference` (`high-performance`, `low-power`) accepted, no change here (one hardware adapter).
- **The process's Vulkan env chooses it.** Without the probe's EGL override the NVIDIA runtime session runs on
  llvmpipe (≈ 20× slower). The mesa device-select layer (implicit in the image) pins a process to one Vulkan device:
  `10005:0` moved the session off the P5000 (twice), `10de:1bb0` put it back. `pci:` had no visible effect here (an
  address llvmpipe lists also ran at GPU speed); unproven until the plex host. A host with two hardware GPUs (plex:
  TITAN RTX + UHD 770) is where per-helper pinning has to be proven.
- **A failed EP is a silent CPU session.** No adapter, or `deviceId` ≥ 1: `InferenceSession` returns a CPU session.
  `session.get_providers()[0] == "WebGpuExecutionProvider"` tells them apart; the "ok" lines of the script don't.
- **Listed ≠ usable.** Without a GPU, and with no Vulkan driver at all, the EP still lists the P5000 with its
  `pci_bus_id`.

### Exit time

| Case | Child exit after its sessions were done | Container wall |
|---|---|---|
| NVIDIA runtime | exit 0, **0.1 s** | 14 s |
| No GPU passed (llvmpipe) | exit 0, **0.1 s** | 69 s |
| No Vulkan driver | exit 0, **0.1 s** | 7 s |

The shutdown hang of ORT PR #29591 was not reproduced with onnxruntime 1.30.0 + onnxruntime-ep-webgpu 0.3.0 in any of
the three cases.

### First runs of a session

One-off `warmup.py` (kept out of the repo; creates one WebGPU session on the first listed device, `{}` options, then
times 12 single runs; output in `bench/m3-warmup.txt`):

| Container | Session create | Run 1 | Runs 2–12 |
|---|---|---|---|
| NVIDIA runtime + EGL override | 460 ms | 1,221.7 ms | 9.4–10.4 ms |
| No GPU passed (llvmpipe) | 283 ms | 14,230.7 ms | 126.9–243.9 ms |

Only the first run pays the shader compile, which is why the first 20-run average above reads 74.6 ms. On a container
without a GPU the self-test spends ≈ 14 s on that first run before it can pick the CPU.

## M4 cost

Host venv (the M1 Python 3.12 venv, host ffmpeg 8.0.1), first 5 movies of `movies40.json` (900 s tail) and first 5
episodes of `tv40.json` (450 s tail), frames at 320×180. `decode_s` decodes the tail's keyframes, `detect_s` runs text
detection on them, `refine_s` decodes and detects two 21 s 1 fps windows placed around the truth start, `total_s` is
all three. The first window starts 20 s before the truth start, the second at start + 60 s (unclamped in these runs).

```bash
cd docs/design/intro-credits/evidence/credits/phase3
B="$MARKERS_BENCH_DIR"; M="$B/textdet-model/ch_PP-OCRv4_det_infer.onnx"
nice -n 19 "$B/py312/bin/python" measure_cost.py "$M" 5 5 cpu | tee "$B/m4-cpu.jsonl"
nice -n 19 taskset -c 0,1 "$B/py312/bin/python" measure_cost.py "$M" 5 5 cpu | tee "$B/m4-cpu-2cores.jsonl"
nice -n 19 "$B/py312/bin/python" measure_cost.py "$M" 5 5 gpu | tee "$B/m4-gpu.jsonl"
# Load check (movies only, normal priority):
taskset -c 0,1 "$B/py312/bin/python" measure_cost.py "$M" 5 0 cpu | tee "$B/m4-cpu-2cores-nice0.jsonl"
```

Storage was shared with the other phase-3 lanes' test runs: load average 16–30 on 20 CPUs throughout (08:17–08:36,
`bench/m4-load.txt`). The load check at normal priority came out 7–27 % faster per movie than the `nice -n 19` run, the
same shape.

The recorded rows don't name the detection session's provider or ffmpeg's return codes. The GPU rows rest on a
separate check with `adapter_check.py` in the same venv and setup (`bench/m3-adapter-check.txt`): providers
`WebGpuExecutionProvider` first, 9.7 ms/frame, the process on the P5000 in `nvidia-smi` (32 MiB). `measure_cost.py`
now records `provider` and `ffmpeg_codes` per row (a one-episode smoke run of each mode,
`bench/m4-smoke-{cpu,gpu}.jsonl`: `CPUExecutionProvider` / `WebGpuExecutionProvider`, codes `[0, 0, 0]`), and clamps
the second window's start to `min(start + 60, duration − 21)`; the recorded rows predate both changes. The other CUDA
process on the P5000 (2006 MiB) was there for these runs too.

Median / max seconds per file:

| Mode | Kind | Decode | Detection | Refine | **Total** |
|---|---|---|---|---|---|
| CPU, 2 threads | movie | 10.3 / 77.3 | 4.4 / 12.1 | 8.6 / 55.9 | **23.8 / 126.1** |
| CPU, 2 threads | episode | 8.7 / 10.1 | 4.7 / 5.7 | 5.5 / 7.1 | **20.3 / 20.5** |
| CPU pinned to 2 cores | movie | 9.5 / 80.2 | 4.2 / 13.5 | 7.0 / 59.5 | **21.3 / 134.2** |
| CPU pinned to 2 cores | episode | 8.9 / 9.8 | 4.7 / 5.7 | 5.8 / 8.0 | **20.3 / 21.5** |
| CPU pinned to 2 cores, normal priority | movie | 8.6 / 76.2 | 3.8 / 11.6 | 5.2 / 51.5 | **19.6 / 124.3** |
| GPU (NVDEC + WebGPU) | movie | 2.8 / 7.0 | 2.7 / 8.3 | 3.3 / 8.1 | **10.7 / 20.7** |
| GPU (NVDEC + WebGPU) | episode | 1.7 / 2.0 | 3.3 / 3.8 | 2.9 / 3.3 | **7.7 / 8.2** |

Per file, total seconds (no names; video stream class from `ffprobe`, `bench/m4-video-kinds.txt`):

| # | Kind | Video | Tail keyframes | Refine frames | CPU | CPU 2 cores | CPU 2 cores, nice 0 | GPU |
|---|---|---|---|---|---|---|---|---|
| 1 | movie | H.264 ≤ 1080p | 310 | 44 | 23.8 | 21.3 | 19.6 | 10.7 |
| 2 | movie | H.264 ≤ 1080p | 220 | 21 | 21.2 | 14.5 | 13.0 | 7.6 |
| 3 | movie | H.264 ≤ 1080p | 118 | 42 | 20.6 | 13.1 | 9.6 | 7.3 |
| 4 | movie | HEVC 2160p HDR10 | 89 | 42 | 73.9 | 79.7 | 68.2 | 16.2 |
| 5 | movie | HEVC 2160p | 671 | 42 | 126.1 | 134.2 | 124.3 | 20.7 |
| 6 | episode | H.264 1080p | 69 | 42 | 8.2 | 8.5 | — | 5.5 |
| 7 | episode | H.264 1080p | 313 | 22 | 20.3 | 21.1 | — | 8.2 |
| 8 | episode | H.264 1080p | 249 | 21 | 18.9 | 18.5 | — | 7.0 |
| 9 | episode | H.264 1080p | 267 | 38 | 20.4 | 20.3 | — | 7.9 |
| 10 | episode | H.264 1080p | 268 | 39 | 20.5 | 21.5 | — | 7.7 |

The second refine window (start + 60 s) falls past the end of the file on rows 2, 7 and 8 (21–22 refine frames) and
partly past it on rows 9 and 10 (38 / 39 frames), so `refine_s` covers less than two windows there. The gate result
below stands: both 2160p rows got the full 42 frames.

- **Settings range (Task 10):** CPU on 2 cores median 21.3 s per movie / 20.3 s per episode; GPU median 10.7 s /
  7.7 s: under 30 s per file at the medians; GPU episodes about 8 s.
- **Gate: tripped.** On 2 cores, 2 of 5 movies take more than 60 s: both 2160p HEVC (79.7 s and 134.2 s; 68.2 s and
  124.3 s at normal priority). All three ≤ 1080p movies stay under 22 s. Owner to be told before Task 10.
- **Why the 2160p rows are slow on the CPU:** decoding. Movie 5's tail has 671 keyframes (one per 1.3 s) and takes
  76–80 s to decode; movie 4's two 1 fps windows take 51–60 s because `fps=1` drops frames after the decoder, so 42 s
  of 2160p HEVC is decoded at full rate. Detection is at most 13.5 s.
- **Against spec §5.4:** GPU movie totals 7.3–10.7 s on ≤ 1080p (at or under the spec's 8–13 s) and 16.2 / 20.7 s
  on 2160p. CPU decode of a movie tail: median 9.5–10.3 s over the 5 movies, 76–80 s on the dense-keyframe 2160p
  file, against the spec's 26.5 s.

## M5 HDR kinds

```bash
nice -n 19 "$PYTHON" docs/design/intro-credits/evidence/credits/phase3/measure_hdr.py
```

`ffprobe` of the first video stream: Dolby Vision by `dv_profile` side data (profile 5 or other), else HDR10 when
`color_transfer` is `smpte2084` or `arib-std-b67`, else SDR; a non-zero exit or 60 s timeout counts as "unreadable".
Re-run with that check (`bench/m5-hdr.txt`): no unreadable file, and the same counts as the first run (observed
once, output not saved).

| Kind | 80 files | Rule J within 10 s | Early > 30 s | None | 205 movies |
|---|---|---|---|---|---|
| SDR | 58 | 47 | 1 | 3 | 123 |
| HDR10 (incl. HLG) | 8 | 5 | 0 | 0 | 39 |
| Dolby Vision profile 5 | 0 | — | — | — | 0 |
| Dolby Vision, other profiles | 14 | 7 | 0 | 1 | 43 |
| Total | 80 | 59 | 1 | 4 | 205 |

DV profile 5 credits text is unmeasured; the detector reads the base layer's luma like any file.
