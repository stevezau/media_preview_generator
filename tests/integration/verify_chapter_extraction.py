"""Real SDR/PQ/HLG extraction proof using locally generated synthetic clips.

Run with the project Python environment from the repository root:
``python tests/integration/verify_chapter_extraction.py``. Requires FFmpeg with
zscale, FFV1 and libmediainfo. Writes only /tmp/pr287-extraction-proof.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from media_preview_generator.output.plex_hash import calculate_plex_hash, get_source_fingerprint
from media_preview_generator.processing import chapters, ffmpeg_runner
from media_preview_generator.processing.chapters import MediaInfo, extract_chapter_frame
from media_preview_generator.processing.generator import CancellationError
from media_preview_generator.servers.plex_chapters import Chapter, ChapterTarget


def main():
    """Generate and decode three color-transfer fixtures with real FFmpeg."""
    root = Path("/tmp/pr287-extraction-proof")
    root.mkdir(exist_ok=True)
    cfg = SimpleNamespace(
        ffmpeg_path=os.environ.get("CHAPTER_PROOF_FFMPEG", "/usr/bin/ffmpeg"),
        ffmpeg_threads=2,
        thumbnail_quality=4,
        tonemap_algorithm="hable",
        log_level="INFO",
    )
    results = []
    for name, transfer in [("sdr", "bt709"), ("pq", "smpte2084"), ("hlg", "arib-std-b67")]:
        source = root / (name + ".mkv")
        output = root / (name + ".jpg")
        output.unlink(missing_ok=True)
        filt = (
            "format=yuv420p"
            if name == "sdr"
            else f"zscale=pin=bt709:tin=bt709:min=bt709:p=bt2020:t={transfer}:m=bt2020nc,format=yuv420p10le"
        )
        command = [
            cfg.ffmpeg_path,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=12",
            "-t",
            "2",
            "-vf",
            filt,
            "-c:v",
            "ffv1",
            "-threads",
            "1",
            "-filter_threads",
            "1",
            "-color_trc",
            transfer,
            "-color_primaries",
            "bt709" if name == "sdr" else "bt2020",
            "-colorspace",
            "bt709" if name == "sdr" else "bt2020nc",
            str(source),
        ]
        subprocess.run(command, check=True, capture_output=True)
        info = MediaInfo.parse(str(source))
        t = info.video_tracks[0]
        extract_chapter_frame(str(source), 1000, output, cfg)
        with Image.open(output) as im:
            im.load()
        with Image.open(output) as im:
            size = im.size
        assert size == (1280, 720)
        results.append(
            {
                "kind": name,
                "detected_transfer": t.transfer_characteristics,
                "size": size,
                "bytes": output.stat().st_size,
            }
        )
    processes = []
    popen = ffmpeg_runner.subprocess.Popen

    def record_process(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process

    ffmpeg_runner.subprocess.Popen = record_process
    try:
        try:
            extract_chapter_frame(
                str(root / "pq.mkv"), 1000, root / "cancelled.jpg", cfg, cancel_check=lambda: bool(processes)
            )
        except CancellationError:
            assert processes and all(process.poll() is not None for process in processes)
        else:
            raise AssertionError("Real FFmpeg was not cancelled")
    finally:
        ffmpeg_runner.subprocess.Popen = popen

    race_source = root / "race.mkv"
    shutil.copyfile(root / "pq.mkv", race_source)
    folder = root / "race-chapters"
    if folder.exists():
        shutil.rmtree(folder)
    target = ChapterTarget(
        1,
        1,
        1,
        calculate_plex_hash(race_source),
        str(race_source),
        race_source.stat().st_size,
        None,
        (Chapter(1, 1000, 2000, "", 1, 1),),
        "synthetic",
    )
    plan = chapters.ChapterPlan(
        object(),
        str(race_source),
        get_source_fingerprint(race_source),
        folder,
        {"version": 1, "width": 1280, "quality": 4, "tonemap": "hable"},
        target,
    )

    def extract_then_replace(*args, **kwargs):
        extract_chapter_frame(*args, **kwargs)
        with race_source.open("ab") as source_file:
            source_file.write(b"source replacement race")

    chapters.extract_chapter_frame = extract_then_replace
    try:
        race_outcome = chapters.publish_chapters(plan, cfg)
        assert race_outcome.status == "pending" and race_outcome.completed == 0
        assert not (folder / "chapter1.jpg").exists() and not plan.manifest_path.exists()
    finally:
        chapters.extract_chapter_frame = extract_chapter_frame
    report = {
        "ffmpeg_path": cfg.ffmpeg_path,
        "ffmpeg_version": subprocess.check_output([cfg.ffmpeg_path, "-version"], text=True).splitlines()[0],
        "color_transfers": results,
        "cancelled_processes_reaped": len(processes),
        "source_replacement_after_real_decode": race_outcome.to_dict(),
    }
    (root / "result.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


if __name__ == "__main__":
    main()
