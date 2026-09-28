"""Scratch: the app's pool on cuda:0 with the recognition model: self-test verdict, then GPU vs CPU reads of real cards."""

import json
import os
import sys
import time

CODE = "/home/data/workspace/plex_generate_vid_previews"
sys.path.insert(0, CODE)
os.chdir(CODE)
os.environ["MEDIA_PREVIEW_TEXTDET_MODEL"] = (
    "/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/card-read/local/models/ch_PP-OCRv4_det_infer.onnx"
)
os.environ["MEDIA_PREVIEW_TEXTREC_MODEL"] = (
    "/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/card-read/local/models/latin_PP-OCRv5_rec_mobile.onnx"
)
from media_preview_generator.markers.credits import cards, frames, textdet_helper  # noqa: E402

T = json.load(open("/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/card-read/local/targets.json"))
pool = textdet_helper.get_textdet_pool()
gpu = {"gpu": "NVIDIA", "gpu_device_path": "cuda:0"}
try:
    t0 = time.time()
    print("state", textdet_helper.text_detection_status(), flush=True)
    pool.detect_boxes(frames.np.zeros((1, 180, 320), dtype=frames.np.uint8), **gpu)
    print("backend", pool.backend_of("NVIDIA", "cuda:0"), f"{time.time() - t0:.1f}s", flush=True)
    same = diff = 0
    for key, when in [("abi", 6120), ("acc303", 2505), ("breach", 6244), ("sky", 5284), ("gandhari", 6489),
                      ("avengers", 8257), ("trainwreck", 4665), ("acc309", 2539)]:  # fmt: skip
        path = T[key][0]["path"]
        thin = frames.keyframe_thinning(path, "ffmpeg")
        got = {}
        for name, where in (("gpu", gpu), ("cpu", {"gpu": None, "gpu_device_path": None})):
            t1 = time.time()
            got[name] = frames.read_text_at(path, ffmpeg="ffmpeg", at_s=when, scale=cards.READ_SCALE,
                                            download_format=thin.download_format,
                                            read_text=lambda p, w=where: pool.read_text(p, **w), **gpu)  # fmt: skip
            got[name + "_s"] = time.time() - t1
        same += got["gpu"] == got["cpu"]
        diff += got["gpu"] != got["cpu"]
        print(key, "SAME" if got["gpu"] == got["cpu"] else "DIFF", f"gpu {got['gpu_s']:.1f}s cpu {got['cpu_s']:.1f}s",
              " | ".join(got["gpu"])[:120], flush=True)  # fmt: skip
        if got["gpu"] != got["cpu"]:
            print("   cpu:", " | ".join(got["cpu"])[:120])
    print("same", same, "diff", diff)
finally:
    pool.close_all()
