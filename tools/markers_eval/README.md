# Intro & Credits accuracy harness

Measures the marker detectors on real episodes with known truth (spec §10.2). It runs by hand on `storage`, never in
CI: the truth sets list real library paths and the runs read real media. The unit tests in `tests/markers_eval/` use
synthetic data only.

## Data and resource rules

Every run uses `nice -n 19`, one heavy job at a time on storage. Media files are only read, by ffprobe and ffmpeg.
Fingerprints are cached under `$MARKERS_EVAL_CACHE` (default `~/.cache/markers_eval`), never under `/data*`. The
truth files are local-only (git-ignored) in the main checkout's `docs/design/intro-credits/evidence/`; in a git
worktree, set `MARKERS_EVAL_EVIDENCE` to that folder. Committed summaries hold counts and show names only, never file
paths. Summaries go to `docs/design/intro-credits/evidence/eval/phase2-harness.md`.

## `reproduce`: the season matcher gate

Runs the app's v3 matcher (`markers/audio/matcher.py`) and the pure-Python reference
(`fp3_reference.py`) on the 118-episode intro eval (`evidence/eval/eval_results_v3.json`). The fingerprints come from
the app's own ffmpeg command. It checks three things:

- The port must return exactly what the reference returns for every episode.
- The useful / wrong / missed tally must be at least spec §5.3's 91 / 13 / 14. "Useful" means the end is within 5 s
  and the start within 15 s of the chapter truth.
- Drift lists the answers that differ from the stored v3 segments by more than two points. It is reported, but it
  doesn't fail the gate.

```bash
cd /home/data/workspace/plex_generate_vid_previews
nice -n 19 /home/data/.venv/bin/python -m tools.markers_eval reproduce --ffmpeg /usr/bin/ffmpeg \
  --json docs/design/intro-credits/evidence/eval/phase2_reproduce.json
```

Exit 0 means the gate passed. `--json` writes the details, which hold file paths, so keep that file local (the
evidence folder ignores `*.json`).

- `--full-folder`: matches every episode file in each season folder, which is what the app does. The default matches
  the eval's own file lists, at most 8 files per season, which is how §5.3 was measured. In this mode, drift also
  counts the answers that the larger group changed.
- `--no-reference`: skips the slow reference.
- `--cache DIR`: overrides the cache folder.

A cold cache fingerprints about 124 files (158 with `--full-folder`), one ffmpeg at a time, at 3–14 s each.
