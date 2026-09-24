"""Frame rate, audio rate and duration of every Bones S05-S07 video (read only)."""

import glob
import json
import subprocess

ROOT = "/data/TV Shows/Bones (2005) {tvdb-75682}"
out = {}
for season in ("05", "06", "07"):
    for path in sorted(glob.glob(f"{ROOT}/Season {season}/*.mkv")):
        proc = subprocess.run(
            [
                "nice",
                "-n",
                "19",
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,r_frame_rate,avg_frame_rate,sample_rate,channels:format=duration",
                "-of",
                "json",
                path,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        info = json.loads(proc.stdout)
        video = next(s for s in info["streams"] if s["codec_type"] == "video")
        audio = next((s for s in info["streams"] if s["codec_type"] == "audio"), {})
        out[path] = {
            "r": video.get("r_frame_rate"),
            "avg": video.get("avg_frame_rate"),
            "sr": audio.get("sample_rate"),
            "ch": audio.get("channels"),
            "dur": float(info["format"]["duration"]),
        }
        print(path.rsplit("/", 1)[1][:40], out[path])
json.dump(
    out,
    open(
        "/tmp/claude-1000/-home-data-workspace-plex-generate-vid-previews/221c1481-5d66-4e86-8c40-a12113e50030/scratchpad/bones/fps.json",
        "w",
    ),
    indent=1,
)
