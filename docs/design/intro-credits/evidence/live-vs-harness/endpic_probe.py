import os
import sys

CODE = os.environ["CODE"]
sys.path.insert(0, CODE)
import numpy as np  # noqa: E402

from media_preview_generator.markers.audio import end_picture as E  # noqa: E402

target, partner, start, end, offset = (
    sys.argv[1],
    sys.argv[2],
    float(sys.argv[3]),
    float(sys.argv[4]),
    float(sys.argv[5]),
)
r = E.Reader(ffmpeg="/usr/bin/ffmpeg")
times = E.sample_times(start, end)
a, b = r._read_starts(target), r._read_starts(partner)
own = r._decoded(target, a, times, a.audio_offset_s)
th = r._decoded(partner, b, times, offset + b.audio_offset_s)
for t in times:
    x = E._nearest(own, t + a.audio_offset_s)
    y = E._nearest(th, t + offset + b.audio_offset_s)
    if x is None or y is None:
        print(f"  t={t:7.2f} missing")
        continue
    zx, zy = (x - x.mean()).ravel(), (y - y.mean()).ravel()
    corr = float(zx @ zy / (np.linalg.norm(zx) * np.linalg.norm(zy) + 1e-9))
    print(
        f"  t={t:7.2f} alike={E.frames_alike(x, y)} card={E._card_picture(x)} std {x.std():5.1f}/{y.std():5.1f} mean {x.mean():5.1f}/{y.mean():5.1f} corr {corr:.2f}"
    )
print("share", E.share_alike(times, own, a.audio_offset_s, th, offset + b.audio_offset_s))
