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
        # onnxruntime-ep-webgpu ships a py3-none wheel (no ABI tag).
        wanted = "manylinux" in f["filename"] and any(tag in f["filename"] for tag in ("cp312", "abi3", "py3-none"))
        if f["packagetype"] == "bdist_wheel" and wanted:
            print(f"wheel {f['filename']} {f['size'] / 1e6:.1f} MB")
PY
site="$("$VENV/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
for pkg in onnxruntime onnxruntime_ep_webgpu cv2 opencv_python_headless.libs pyclipper numpy numpy.libs shapely shapely.libs; do
    [[ -e "$site/$pkg" ]] && du -sm "$site/$pkg"
done
"$VENV/bin/python" -c 'import onnxruntime, cv2, pyclipper, onnxruntime_ep_webgpu; print(onnxruntime.__version__, cv2.__version__)'
