"""Side-by-side frames of a share's two files at given instants (read-only on the media), for a frame check.

    nice -n 19 python frame_pair.py <key file: target path, partner path> <offset s> <instant s> ...

Writes local/frames_<instant>.jpg (gitignored): the target at the instant, the partner at the instant plus the offset.
"""

import subprocess
import sys

target, partner = open(sys.argv[1]).read().splitlines()[:2]
offset = float(sys.argv[2])
for instant in sys.argv[3:]:
    t = float(instant)
    subprocess.run(
        ["/usr/bin/ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", target, "-ss", f"{t + offset:.3f}", "-i",
         partner, "-filter_complex", "[0:v]scale=480:-2[a];[1:v]scale=480:-2[b];[a][b]hstack", "-frames:v", "1",
         f"local/frames_{instant}.jpg"],
        check=True,
    )  # fmt: skip
