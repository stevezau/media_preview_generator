"""Explicit, read-only loudness parity check against prepared real Plex lab items.

Run with --run --report /tmp/loudness-parity.json. Supply JSON on stdin containing
url, token, local_root, remote_root and item_ids. Keep credentials in memory: do
not save that input or put tokens on the command line. Each item must contain
only dedicated ``Loudness Proof*`` fixtures, at most 180 seconds long, and already
have native Plex loudness measurements. The harness does not run native analysis
or alter Plex metadata/preferences. It compares the current application's actual
analyzer with the real server's exposed measurements, including silent/short
tracks, and records differences rather than pretending floating-point results
must always be byte-identical. Run inside the app image to test its FFmpeg build.

The adjacent retained evidence describes the separate guarded-write, native
completion, mixed-track and normalized-playback acceptance experiments.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

import requests
from defusedxml import ElementTree as ET

from media_preview_generator.loudness.analyze import run
from media_preview_generator.markers.publishers.plex_db import TESTED_PMS_LABEL, is_tested_pms_version

FIELDS = ("loudness", "peak", "lra", "threshold", "gainOffset", "loudnessAnalysisVersion")


def delta(expected: str, actual: str) -> float | None:
    """Compare finite measurements; matching native infinities have zero delta."""
    left, right = float(expected), float(actual)
    if left == right:
        return 0.0
    if math.isfinite(left) and math.isfinite(right):
        return round(right - left, 6)
    return None


def main() -> int:
    """Validate the bounded fixture set and compare real measurements."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    args = parser.parse_args()
    if not args.run:
        parser.error("Pass --run to access the explicitly configured real lab")
    config = json.load(sys.stdin)
    url = str(config["url"]).rstrip("/")
    allowed_hosts = {"127.0.0.1", "localhost"}
    if os.environ.get("PLEX_LAB_HOST"):
        allowed_hosts.add(os.environ["PLEX_LAB_HOST"])
    if urlparse(url).hostname not in allowed_hosts:
        raise ValueError("This proof only accepts the local isolated Plex lab (set PLEX_LAB_HOST for a remote one)")
    ids = [str(int(value)) for value in config["item_ids"]]
    if not 1 <= len(ids) <= 20 or len(set(ids)) != len(ids):
        raise ValueError("Supply 1–20 distinct dedicated fixture IDs")
    local_root = Path(config["local_root"]).resolve(strict=True)
    remote_root = PurePosixPath(config["remote_root"])
    session = requests.Session()
    session.headers["X-Plex-Token"] = str(config["token"])

    def get(path: str) -> Any:
        response = session.get(url + path, timeout=20)
        response.raise_for_status()
        return ET.fromstring(response.content)

    identity = get("/identity")
    version = identity.get("version", "")
    if not is_tested_pms_version(version) or identity.get("claimed") != "1":
        raise ValueError(f"Use a claimed, tested Plex {TESTED_PMS_LABEL} lab")
    work: list[dict[str, Any]] = []
    for item_id in ids:
        root = get("/library/metadata/" + item_id)
        video = root.find("Video")
        if video is None or video.get("ratingKey") != item_id:
            raise ValueError("Fixture did not resolve to the requested video")
        duration = int(video.get("duration", "0"))
        if not 0 < duration <= 180_000:
            raise ValueError("Only bounded fixtures of at most 180 seconds are permitted")
        for part in video.iter("Part"):
            remote = PurePosixPath(part.get("file", ""))
            relative = remote.relative_to(remote_root)
            if not relative.parts or not relative.parts[0].startswith("Loudness Proof"):
                raise ValueError("Refusing a file outside the dedicated proof fixtures")
            local = (local_root / str(relative)).resolve(strict=True)
            if not local.is_relative_to(local_root):
                raise ValueError("Fixture resolves outside the declared local root")
            for stream in part.findall("Stream"):
                if stream.get("streamType") != "2":
                    continue
                if stream.get("canNormalizeLoudness") != "1" or any(stream.get(k) is None for k in FIELDS):
                    raise ValueError("Capture native analysis first; this fixture lacks complete Plex loudness")
                work.append(
                    {
                        "item_id": item_id,
                        "part_id": part.get("id"),
                        "stream_id": stream.get("id"),
                        "index": int(stream.get("index", "-1")),
                        "codec": stream.get("codec", ""),
                        "channels": int(stream.get("channels", "0")),
                        "sample_rate": int(stream.get("samplingRate", "0")),
                        "duration_ms": duration,
                        "native": {key: stream.get(key) for key in FIELDS},
                        "path": local,
                    }
                )
    if not 1 <= len(work) <= 40:
        raise ValueError("Supply 1–40 explicitly scoped audio streams")

    def measure(entry: dict[str, Any]) -> dict[str, Any]:
        measured = run(
            args.ffmpeg,
            str(entry["path"]),
            entry["index"],
            codec=entry["codec"],
            duration_ms=entry["duration_ms"],
        )
        result = {key: value for key, value in entry.items() if key != "path"}
        result["app"] = {key: measured["ln:" + key] for key in FIELDS}
        result["delta"] = {
            key: delta(entry["native"][key], result["app"][key]) for key in FIELDS if key != "loudnessAnalysisVersion"
        }
        return result

    with ThreadPoolExecutor(max_workers=3) as pool:
        measurements = list(pool.map(measure, work))
    report = {"plex_version": version, "items": len(ids), "streams": measurements}
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    differences = sum(any(value != 0 for value in row["delta"].values()) for row in measurements)
    print(f"Compared {len(measurements)} streams; {differences} have measured differences. Report: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
