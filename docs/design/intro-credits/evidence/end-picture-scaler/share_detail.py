"""Per-instant detail of one end-picture share, on one tree and device: each instant's correlation and flat stats.

    nice -n 19 python share_detail.py <tree> <nvidia|cpu> <target substring> <partner substring> [shares.pkl]

The share's key (times and offset) is taken from a run's shares pickle (default local/after_nvidia_shares.pkl).
"""

import pickle
import sys

import numpy as np

TREE, DEVICE, TARGET, PARTNER = sys.argv[1:5]
SHARES = sys.argv[5] if len(sys.argv) > 5 else "local/after_nvidia_shares.pkl"
sys.path.insert(0, TREE)
from media_preview_generator.markers.audio import end_picture as ep  # noqa: E402

assert ep.__file__.startswith(TREE)
key = next(k for k in pickle.load(open(SHARES, "rb")) if TARGET in k[0] and PARTNER in k[1])
target, partner, start, end, offset = key
gpu = ("NVIDIA", "cuda:0") if DEVICE == "nvidia" else (None, None)
reader = ep.Reader(ffmpeg="/usr/bin/ffmpeg", gpu=gpu[0], gpu_device_path=gpu[1])
times = ep.sample_times(start, end)
s_t, s_p = reader._read_starts(target), reader._read_starts(partner)
own_shift, partner_shift = s_t.audio_offset_s, offset + s_p.audio_offset_s
own = reader._decoded(target, s_t, times, own_shift)
theirs = reader._decoded(partner, s_p, times, partner_shift)
tag = f"{TREE.rstrip('/').rsplit('/', 1)[-1]}:{DEVICE}"
for t in times:
    x, y = ep._nearest(own, t + own_shift), ep._nearest(theirs, t + partner_shift)
    zx, zy = (x - x.mean()).ravel(), (y - y.mean()).ravel()
    r = float(zx @ zy / (np.linalg.norm(zx) * np.linalg.norm(zy)))
    print(f"{tag:18} t={t:6.2f} std {x.std():5.1f}/{y.std():5.1f} mean {x.mean():5.1f}/{y.mean():5.1f} "
          f"r={r:.3f} alike={ep.frames_alike(x, y)}")  # fmt: skip
print(tag, "share", ep.share_alike(times, own, own_shift, theirs, partner_shift))
