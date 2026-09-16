import numpy as np, ncnn, onnxruntime as ort
from PIL import Image
from rapidocr_onnxruntime.ch_ppocr_det.utils import DetPreProcess
img = np.array(Image.open("credits/s7.jpg").convert("RGB").resize((320, 180)))
pre = DetPreProcess(960, "max", [0.5]*3, [0.5]*3)(img)
print("prepro", pre.shape, pre.dtype, pre.min(), pre.max())
s = ort.InferenceSession("ncnnmodel/ch_PP-OCRv4_det_infer.onnx", providers=["CPUExecutionProvider"])
ref = s.run(None, {s.get_inputs()[0].name: pre})[0]
print("ort out", ref.shape, ref.min(), ref.max())
def run(fp16, gpu):
    net = ncnn.Net(); net.opt.use_vulkan_compute = gpu; net.opt.num_threads = 2
    for k in ("use_fp16_packed", "use_fp16_storage", "use_fp16_arithmetic"):
        setattr(net.opt, k, fp16)
    net.load_param("ncnnmodel/ch_PP_OCRv4_det_infer.ncnn.param"); net.load_model("ncnnmodel/ch_PP_OCRv4_det_infer.ncnn.bin")
    ex = net.create_extractor(); ex.input("in0", ncnn.Mat(np.ascontiguousarray(pre[0])))
    _, out = ex.extract("out0"); o = np.array(out)
    return o
for fp16 in (True, False):
    for gpu in (False, True):
        o = run(fp16, gpu)
        r = ref.reshape(o.shape) if o.size == ref.size else None
        print(f"fp16={fp16} gpu={gpu} out {o.shape} min {o.min():.3f} max {o.max():.3f}", "maxabsdiff %.4f" % np.abs(o - r).max() if r is not None else "size mismatch", "pixels>0.3 ort %d ncnn %d" % ((ref>0.3).sum(), (o>0.3).sum()))
