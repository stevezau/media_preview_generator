"""Contact sheet of a stretch of a video (read only): frames every ``step`` seconds, labelled with their time."""

import subprocess
import sys

path, start, length, step, out = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4]), sys.argv[5]
cols = 8
rows = int(length / step / cols + 0.999)
vf = (
    f"fps=1/{step},scale=240:-2,"
    f"drawtext=text='%{{pts\\:hms\\:{start}}}':x=4:y=4:fontsize=18:fontcolor=yellow:box=1:boxcolor=black,"
    f"tile={cols}x{rows}"
)
subprocess.run(
    [
        "nice",
        "-n",
        "19",
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-ss",
        str(start),
        "-t",
        str(length),
        "-i",
        path,
        "-an",
        "-sn",
        "-vf",
        vf,
        "-frames:v",
        "1",
        out,
    ],
    check=True,
)
print(out)
