"""Container chapters for every file in the three Task-15 populations.

Read-only: ``ffprobe -show_chapters`` reads the container header and nothing else, and nothing here
opens a file for writing under the library. Appends to ``chapters_probe.jsonl`` (git-ignored: real
paths), so an interrupted run resumes where it stopped.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "chapters_probe.jsonl"
WORKERS = 8  # storage is shared; this is the whole reason for `nice -n 19` too

_write_lock = threading.Lock()


def probe(row: dict) -> str:
    """One file's chapters, or an ``err`` entry."""
    file = row["file"]
    try:
        done = subprocess.run(
            ["nice", "-n", "19", "ffprobe", "-v", "error", "-show_chapters", "-of", "json", file],
            capture_output=True,
            text=True,
            timeout=90,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return json.dumps({"file": file, "pop": row["pop"], "err": str(exc)[:120]})
    # A file ffprobe can't open returns empty stdout, which would otherwise be recorded as "no
    # chapters" -- indistinguishable from a real chapterless file, and that count is published.
    if done.returncode != 0:
        return json.dumps({"file": file, "pop": row["pop"], "err": f"exit {done.returncode}"})
    try:
        chapters = json.loads(done.stdout or "{}").get("chapters", [])
    except ValueError as exc:
        return json.dumps({"file": file, "pop": row["pop"], "err": f"unreadable ffprobe output: {exc}"})
    parsed = []
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
            continue
    return json.dumps({"file": file, "pop": row["pop"], "chapters": parsed})


def main() -> None:
    """Probe every unprobed file in the population list."""
    rows = json.loads((HERE / "chapters_population.json").read_text())
    done_files = set()
    if OUT.exists():
        for line in OUT.read_text().splitlines():
            if line.strip():
                done_files.add(json.loads(line)["file"])
    todo = [r for r in rows if r["file"] not in done_files]
    print(f"{len(rows)} files, {len(done_files)} already probed, {len(todo)} to go", flush=True)

    counts = Counter()
    with OUT.open("a") as out, ThreadPoolExecutor(WORKERS) as pool:
        for n, line in enumerate(pool.map(probe, todo), 1):
            with _write_lock:
                out.write(line + "\n")
            entry = json.loads(line)
            counts["err" if "err" in entry else ("chapters" if entry["chapters"] else "none")] += 1
            if n % 500 == 0:
                out.flush()
                print(f"  {n}/{len(todo)} {dict(counts)}", flush=True)
    print(f"done: {dict(counts)}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
