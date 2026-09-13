import glob, sys, time
import numpy as np, onnxruntime as ort
from PIL import Image
from rapidocr_onnxruntime import RapidOCR
ort.preload_dlls()
imgs = [np.array(Image.open(p).convert("RGB").resize((320, 180))) for p in sorted(glob.glob("credits/s*.jpg"))]
for use_cuda in (True, False):
    ocr = RapidOCR(det_limit_side_len=320, det_limit_type="max", det_use_cuda=use_cuda, intra_op_num_threads=2, inter_op_num_threads=1)
    sess = ocr.text_det.infer.session if hasattr(ocr.text_det.infer, "session") else None
    print("cuda" if use_cuda else "cpu2", "providers:", sess.get_providers() if sess else "?")
    for _ in range(3): ocr.text_det(imgs[0])
    t = time.time(); counts = []
    for _ in range(10):
        for im in imgs:
            b, _ = ocr.text_det(im); counts.append(0 if b is None else len(b))
    n = 10 * len(imgs)
    print(f"  {1000*(time.time()-t)/n:.1f} ms/frame  boxes first pass: {counts[:len(imgs)]}")
