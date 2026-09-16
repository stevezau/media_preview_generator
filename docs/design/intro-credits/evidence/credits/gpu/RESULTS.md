# GPU text detection on every vendor — results (2026-09-13, storage, Quadro P5000)

Same PP-OCRv4 det ONNX model, same RapidOCR pre/post-processing, 289 keyframes (320x180) from 8 credits tails.

| Engine | Where | ms/frame (whole det pipeline) | Same boxes as ORT CPU |
|---|---|---|---|
| ONNX Runtime CPU, 2 threads | host venv | 18.1 | reference |
| ONNX Runtime CUDA (onnxruntime-gpu 1.22 + cuDNN 9.5) | host venv | 7.7 (13-frame bench) | 100% |
| **ONNX Runtime WebGPU plugin 0.3.0 (Dawn → Vulkan)** | host venv | **13.5** | **100%** (prob map max diff 3e-5) |
| ONNX Runtime WebGPU, inside app image, NVIDIA runtime, caps=all | container | **430** — NVIDIA Vulkan ICD failed to load (`Could not get 'vkCreateInstance' … libGLX_nvidia.so.0`), silently used llvmpipe | 100% |
| same + app's Vulkan probe env (`__EGL_VENDOR_LIBRARY_FILENAMES=/usr/share/glvnd/egl_vendor.d/10_nvidia.json`) | container | **13.3** (CPU 18.7) | **100%** |
| ONNX Runtime WebGPU, container with no GPU passed | container | 437 (software Vulkan) | 100% |
| ncnn 1.0.20260526 Vulkan, model converted with pnnx | host venv | 7.3 | **broken**: conversion wrong (activations explode to 1e28 in the deepest branch; 2652 vs 163 text pixels). Not pursued. |

Sizes: onnxruntime-ep-webgpu 16 MB installed; onnxruntime CPU 62 MB (needed anyway).

Findings
- Vulkan route works in the app image on NVIDIA only with the app's existing Vulkan probe overrides; without them it
  silently runs on the CPU rasteriser at 24x slower. Must guard: use the GPU only when `get_vulkan_device_info()`
  reports a hardware device AND a short self-test beats CPU; otherwise CPU.
- WebGPU EP options found in the plugin: `powerPreference`, `deviceId`, `dawnBackendType` (device choice on
  multi-GPU hosts). EP device list comes from sysfs PCI data, not from Vulkan, so it can't prove a working GPU.
- Speed gain per file is small because decode dominates: movie ≈ 16.7 s (WebGPU text) vs 18.8 s (CPU text).
- **plex host (owner OK, throwaway container, removed after)**, same 289 frames, app image, 2 CPUs:
  - Intel UHD 770 (RPL-S, 0xa780), only card1+renderD128 passed, no NVIDIA runtime: app probe found the iGPU with no
    env overrides; **WebGPU 16.1 ms/frame vs CPU 8.0 ms/frame**, 100% identical boxes → iGPU is SLOWER than this CPU;
    the self-test guard must pick CPU here.
  - NVIDIA TITAN RTX (0x1e02), nvidia runtime + caps=all + app probe env (`VK_DRIVER_FILES=/etc/vulkan/icd.d/nvidia_icd.json`,
    `__EGL_VENDOR_LIBRARY_FILENAMES=/usr/share/glvnd/egl_vendor.d/10_nvidia.json`): **WebGPU 4.8 ms/frame vs CPU
    7.7 ms/frame**, 100% identical boxes.
- AMD: not tested (no hardware available — owner confirmed). Same Vulkan (RADV) path; default to CPU until proven.

Reproduce: `credits/gpu/extract.py` (frames.npy), then in the app image:
`docker run --rm --runtime=nvidia -e NVIDIA_VISIBLE_DEVICES=all -e NVIDIA_DRIVER_CAPABILITIES=all --device /dev/dri -e USE_APP_VULKAN_PROBE=1 -v <site-packages>:/pkgs:ro -v <dir with bench.py, frames.npy, model>:/work:ro -e PYTHONPATH=/pkgs --entrypoint python3 stevezzau/media_preview_generator:dev /work/bench.py`
