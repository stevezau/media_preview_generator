import json, subprocess, numpy as np
W, H = 320, 180
items = [json.loads(l) for l in open("credits/f3.jsonl")][::10]
out = []
for it in items:
    start = max(0.0, it["truth"] - 60)
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-threads", "2", "-skip_frame", "nokey", "-ss", f"{start:.2f}", "-t", "120",
                          "-i", it["file"], "-an", "-sn", "-dn", "-fps_mode", "passthrough", "-vf", f"scale={W}:{H},format=gray",
                          "-f", "rawvideo", "-"], capture_output=True).stdout
    out.append(np.frombuffer(raw, np.uint8)[: len(raw) // (W * H) * W * H].reshape(-1, H, W))
np.save("ctest/frames.npy", np.concatenate(out)); print(np.concatenate(out).shape)
