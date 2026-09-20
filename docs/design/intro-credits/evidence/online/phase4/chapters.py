"""Container chapters for every resolved anime file — the truth set AniSkip is scored against.

Read-only: ``ffprobe -show_chapters`` reads the container header and nothing else. Never writes under
the library.
"""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKERS = 6  # storage is shared; this is the whole reason for `nice -n 19` too


def probe(row: dict) -> dict:
    """One file's chapters, or an ``err`` entry."""
    try:
        done = subprocess.run(
            ["nice", "-n", "19", "ffprobe", "-v", "error", "-show_chapters", "-of", "json", row["file"]],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"file": row["file"], "err": str(exc)[:120]}
    # A file ffprobe can't open returns empty stdout, which would otherwise be recorded as "no chapters"
    # -- indistinguishable from a real chapterless file, and that count is published.
    if done.returncode != 0:
        return {"file": row["file"], "err": f"ffprobe exit {done.returncode}: {done.stderr.strip()[:100]}"}
    try:
        chapters = json.loads(done.stdout or "{}").get("chapters", [])
    except ValueError as exc:
        return {"file": row["file"], "err": f"unreadable ffprobe output: {exc}"}
    parsed, bad = [], 0
    for chapter in chapters:
        try:
            parsed.append(
                {
                    "name": chapter.get("tags", {}).get("title", ""),
                    "start_ms": int(float(chapter["start_time"]) * 1000),
                    "end_ms": int(float(chapter["end_time"]) * 1000),
                }
            )
        except (KeyError, ValueError):
            bad += 1
    out = {"file": row["file"], "chapters": parsed}
    if bad:
        out["unparsed_chapters"] = bad
    return out


def main() -> None:
    """Probe every resolved file and write ``anime_chapters.json``."""
    rows = json.loads((HERE / "resolved.json").read_text())
    with ThreadPoolExecutor(WORKERS) as pool:
        results = list(pool.map(probe, rows))
    (HERE / "anime_chapters.json").write_text(json.dumps(results))
    counts = Counter()
    for result in results:
        counts["ffprobe error" if "err" in result else ("chapters" if result["chapters"] else "no chapters")] += 1
    print(f"{len(results)} anime files probed: {dict(counts)}")


if __name__ == "__main__":
    main()
